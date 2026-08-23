"""The container runtime protocol.

Declared in `core/` rather than in `runtime/` on purpose: the protocol is what
`core` *requires*, and Docker is one detail that happens to satisfy it. Putting
it here is what lets `core` import nothing from `runtime` while still being
fully typed against it.

`core/` receives an implementation of `ContainerRuntime` as a parameter and
imports nothing from the `runtime` package. That is what makes phase ordering, BASE
preparation, force-restore, and the path jail testable in milliseconds against
`FakeRuntime`, with no daemon and no images.

Two implementations exist: `DockerRuntime` (subprocess argv against the docker
CLI) and `FakeRuntime` (records calls, returns scripted results). A Podman
implementation would slot in here unchanged; it is not written.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ExecResult:
    """The outcome of one command run inside a container.

    `timed_out` is separate from a non-zero `exit_code` on purpose: a test run
    that was killed at the wall clock is `inconclusive`, never `failed`. Folding
    the two together is exactly what invariant 5 forbids.
    """

    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def failure_summary(self, limit: int = 400) -> str:
        """A one-line reason this command failed, for error messages."""
        if self.timed_out:
            return f"timed out after {self.duration_ms}ms"
        tail = (self.stderr or self.stdout).strip().splitlines()
        detail = tail[-1] if tail else "no output"
        return f"exit {self.exit_code}: {detail[:limit]}"


@dataclass(frozen=True)
class ContainerSpec:
    """How to create a container.

    The hardening fields default to unset because BASE preparation needs
    network (to install, to clone) and runs before any untrusted code exists.
    SOLVE and SCORED set them explicitly; see `hardened()`.
    """

    image: str
    name: str | None = None
    # "none" disconnects the container entirely. Required for SOLVE and SCORED.
    network: str | None = None
    user: str | None = None
    memory: str | None = None
    cpus: str | None = None
    pids_limit: int | None = None
    workdir: str | None = None
    platform: str | None = None
    env: dict[str, str] = field(default_factory=dict)

    def hardened(
        self,
        *,
        user: str | None = None,
        memory: str = "4g",
        cpus: str = "2",
        pids_limit: int = 512,
    ) -> ContainerSpec:
        """Return this spec with egress cut and resource ceilings applied.

        Used for SOLVE and SCORED. No network means the solver cannot fetch the
        upstream commit that contains the fix, which is what makes hidden-test
        protection hold against a capable adversary rather than a polite one.
        """
        from dataclasses import replace

        return replace(
            self,
            network="none",
            user=user or self.user,
            memory=memory,
            cpus=cpus,
            pids_limit=pids_limit,
        )


@dataclass(frozen=True)
class ImageInfo:
    """A tagged image known to the runtime."""

    ref: str
    image_id: str
    created_at: str
    size_bytes: int


@runtime_checkable
class ContainerRuntime(Protocol):
    """Everything the harness needs from a container engine.

    Deliberately small and free of docker vocabulary in its signatures, so the
    phase machine reads as phase logic rather than as CLI plumbing.
    """

    def image_exists(self, ref: str) -> bool:
        """True if the image is present locally."""
        ...

    def image_digest(self, ref: str) -> str | None:
        """The image's content digest, for recording in a manifest."""
        ...

    def pull(self, ref: str, *, platform: str | None = None) -> str:
        """Fetch an image and return the ref actually resolved."""
        ...

    def build(
        self,
        *,
        context_dir: Path,
        dockerfile: str,
        tag: str,
        platform: str | None = None,
        no_cache: bool = False,
    ) -> str:
        """Build an image from a context directory and return its tag."""
        ...

    def create(self, spec: ContainerSpec) -> str:
        """Create and start a container that stays alive for exec. Returns its id."""
        ...

    def exec(
        self,
        container_id: str,
        argv: list[str],
        *,
        workdir: str | None = None,
        user: str | None = None,
        env: dict[str, str] | None = None,
        timeout_s: int | None = None,
    ) -> ExecResult:
        """Run one command inside a running container.

        `argv` is a list, always. Nothing here is interpolated into a shell by
        the host -- when a shell is genuinely wanted, the caller passes
        `["bash", "-lc", command]` and the command is a single argv element:
        data, not host shell.
        """
        ...

    def commit(self, container_id: str, tag: str) -> str:
        """Snapshot a container as an image and return the tag."""
        ...

    def remove_container(self, container_id: str, *, force: bool = True) -> None:
        """Delete a container."""
        ...

    def remove_image(self, ref: str, *, force: bool = False) -> None:
        """Delete an image."""
        ...

    def copy_out(self, container_id: str, source: str, destination: Path) -> bool:
        """Copy a path out of a container. False if the source does not exist."""
        ...

    def write_file(self, container_id: str, path: str, content: str) -> None:
        """Write a file into a container without going through a shell."""
        ...

    def list_images(self, prefix: str) -> list[ImageInfo]:
        """Images whose ref starts with `prefix`. Used by `task gc`."""
        ...
