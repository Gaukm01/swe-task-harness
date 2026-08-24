"""`ContainerRuntime` over the docker CLI.

Every call is `subprocess.run(argv_list, shell=False)`. No f-string ever
becomes a command line. This is invariant 6, and it is structural here rather
than a matter of discipline: there is no code path in this module that accepts
a string and runs it, so there is nothing for an agent-supplied path or a
repo name with a semicolon in it to escape from.

The CLI is used rather than the SDK because every command the harness runs is
then a line a human can paste into their own terminal when a container
misbehaves -- which, when debugging someone else's prebuilt image, is worth
more than typed return values.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from harness.core.errors import DockerUnavailableError
from harness.core.runtime import ContainerSpec, ExecResult, ImageInfo

# Long enough for an image pull over a slow link; `build` overrides it.
DEFAULT_TIMEOUT_S = 900

# The command a phase container runs so it stays alive to be exec'd into.
# A literal integer rather than `sleep infinity`, which is a GNU extension.
_KEEPALIVE = ("sleep", "2147483647")


@dataclass(frozen=True)
class CommandOutcome:
    """Raw result of a docker CLI call on the host."""

    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False


class DockerRuntime:
    """The real runtime. Implements `ContainerRuntime`."""

    def __init__(self, *, binary: str = "docker", log: list[list[str]] | None = None) -> None:
        self.binary = binary
        # Every argv is appended here, so a failing run can show exactly what ran.
        self.log: list[list[str]] = [] if log is None else log

    # -- plumbing ---------------------------------------------------------

    def _run(
        self,
        args: list[str],
        *,
        timeout_s: int | None = DEFAULT_TIMEOUT_S,
        stdin_bytes: bytes | None = None,
    ) -> CommandOutcome:
        argv = [self.binary, *args]
        self.log.append(argv)
        started = time.monotonic()
        try:
            proc = subprocess.run(  # noqa: S603 - argv list, shell=False by construction
                argv,
                capture_output=True,
                text=stdin_bytes is None,
                input=stdin_bytes,
                timeout=timeout_s,
                shell=False,
                check=False,
            )
        except FileNotFoundError as error:
            raise DockerUnavailableError(
                f"`{self.binary}` is not on PATH.",
                fix="Install Docker Desktop or the docker engine, then run `task doctor`.",
            ) from error
        except subprocess.TimeoutExpired:
            elapsed = int((time.monotonic() - started) * 1000)
            return CommandOutcome(argv, -1, "", "", elapsed, timed_out=True)

        elapsed = int((time.monotonic() - started) * 1000)
        stdout = (
            proc.stdout if isinstance(proc.stdout, str) else proc.stdout.decode(errors="replace")
        )
        stderr = (
            proc.stderr if isinstance(proc.stderr, str) else proc.stderr.decode(errors="replace")
        )
        return CommandOutcome(argv, proc.returncode, stdout, stderr, elapsed)

    def _require(self, outcome: CommandOutcome, what: str, fix: str) -> CommandOutcome:
        """Raise a typed error unless a docker call succeeded."""
        if outcome.timed_out:
            raise DockerUnavailableError(f"{what} timed out.", fix=fix)
        if outcome.exit_code != 0:
            detail = (outcome.stderr or outcome.stdout).strip().splitlines()
            reason = detail[-1] if detail else f"exit {outcome.exit_code}"
            raise DockerUnavailableError(f"{what} failed: {reason}", fix=fix)
        return outcome

    # -- images -----------------------------------------------------------

    def image_exists(self, ref: str) -> bool:
        return self._run(["image", "inspect", ref], timeout_s=60).exit_code == 0

    def image_digest(self, ref: str) -> str | None:
        outcome = self._run(
            ["image", "inspect", ref, "--format", "{{index .RepoDigests 0}}|{{.Id}}"],
            timeout_s=60,
        )
        if outcome.exit_code != 0:
            return None
        repo_digest, _, image_id = outcome.stdout.strip().partition("|")
        # A locally built image has no RepoDigest; its content id still pins it.
        return repo_digest or image_id or None

    def pull(self, ref: str, *, platform: str | None = None) -> str:
        args = ["pull"]
        if platform:
            args += ["--platform", platform]
        args.append(ref)
        self._require(
            self._run(args),
            f"pulling {ref}",
            fix="Check the image reference and your network, then retry.",
        )
        return ref

    def build(
        self,
        *,
        context_dir: Path,
        dockerfile: str,
        tag: str,
        platform: str | None = None,
        no_cache: bool = False,
    ) -> str:
        args = ["build", "-f", str(context_dir / dockerfile), "-t", tag]
        if platform:
            args += ["--platform", platform]
        if no_cache:
            args.append("--no-cache")
        args.append(str(context_dir))
        self._require(
            self._run(args),
            f"building {tag}",
            fix=f"Build it by hand to see the full output: docker build -f "
            f"{context_dir / dockerfile} {context_dir}",
        )
        return tag

    def remove_image(self, ref: str, *, force: bool = False) -> None:
        args = ["image", "rm"]
        if force:
            args.append("--force")
        args.append(ref)
        self._run(args, timeout_s=120)

    def list_images(self, prefix: str) -> list[ImageInfo]:
        outcome = self._run(
            ["image", "ls", "--format", "{{json .}}", "--filter", f"reference={prefix}*"],
            timeout_s=120,
        )
        if outcome.exit_code != 0:
            return []
        images: list[ImageInfo] = []
        for line in outcome.stdout.splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:  # pragma: no cover - defensive
                continue
            images.append(
                ImageInfo(
                    ref=f"{item.get('Repository', '')}:{item.get('Tag', '')}",
                    image_id=item.get("ID", ""),
                    created_at=item.get("CreatedAt", ""),
                    size_bytes=0,
                )
            )
        return images

    # -- containers -------------------------------------------------------

    def create(self, spec: ContainerSpec) -> str:
        args = ["create", "--init"]
        if spec.name:
            args += ["--name", spec.name]
        if spec.platform:
            args += ["--platform", spec.platform]
        if spec.network:
            args += ["--network", spec.network]
        if spec.user:
            args += ["--user", spec.user]
        if spec.memory:
            args += ["--memory", spec.memory]
        if spec.cpus:
            args += ["--cpus", spec.cpus]
        if spec.pids_limit:
            args += ["--pids-limit", str(spec.pids_limit)]
        if spec.drop_capabilities:
            args += ["--cap-drop", "ALL"]
        if spec.no_new_privileges:
            args += ["--security-opt", "no-new-privileges"]
        if spec.workdir:
            args += ["--workdir", spec.workdir]
        for key, value in sorted(spec.env.items()):
            args += ["--env", f"{key}={value}"]
        # Override any ENTRYPOINT the image ships so the keepalive actually runs.
        args += ["--entrypoint", _KEEPALIVE[0], spec.image, *_KEEPALIVE[1:]]

        outcome = self._require(
            self._run(args, timeout_s=120),
            f"creating a container from {spec.image}",
            fix="Run `task doctor`; if the image is remote, check it exists and matches "
            "your platform.",
        )
        container_id = outcome.stdout.strip().splitlines()[-1]
        self._require(
            self._run(["start", container_id], timeout_s=120),
            f"starting container {container_id[:12]}",
            fix="Inspect it with `docker logs " + container_id[:12] + "`.",
        )
        return container_id

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
        args = ["exec"]
        if workdir:
            args += ["--workdir", workdir]
        if user:
            args += ["--user", user]
        for key, value in sorted((env or {}).items()):
            args += ["--env", f"{key}={value}"]
        args.append(container_id)
        args.extend(argv)

        outcome = self._run(args, timeout_s=timeout_s)
        return ExecResult(
            argv=argv,
            exit_code=outcome.exit_code,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            duration_ms=outcome.duration_ms,
            timed_out=outcome.timed_out,
        )

    def commit(self, container_id: str, tag: str) -> str:
        self._require(
            self._run(["commit", container_id, tag], timeout_s=600),
            f"committing {container_id[:12]} as {tag}",
            fix="Check available disk with `task doctor`; commits fail when it runs out.",
        )
        return tag

    def remove_container(self, container_id: str, *, force: bool = True) -> None:
        args = ["rm"]
        if force:
            args.append("--force")
        args.append(container_id)
        self._run(args, timeout_s=120)

    # -- file movement ----------------------------------------------------

    def copy_out(self, container_id: str, source: str, destination: Path) -> bool:
        destination.parent.mkdir(parents=True, exist_ok=True)
        outcome = self._run(["cp", f"{container_id}:{source}", str(destination)], timeout_s=300)
        return outcome.exit_code == 0

    def write_file(self, container_id: str, path: str, content: str) -> None:
        """Write a file into a container via `docker cp`.

        Not `bash -c 'echo ... > file'`: content with quotes, newlines, or a `$`
        would need escaping, and getting that wrong is how injection bugs
        happen. A tar stream has no such failure mode.
        """
        target = Path(path)
        with tempfile.TemporaryDirectory() as staging:
            payload = Path(staging) / target.name
            payload.write_text(content)
            self._require(
                self._run(
                    ["cp", "--archive", str(payload), f"{container_id}:{path}"], timeout_s=120
                ),
                f"writing {path} into {container_id[:12]}",
                fix="Check the destination directory exists inside the container.",
            )

    # -- preflight --------------------------------------------------------

    def available(self) -> bool:
        """True if the daemon answers. Cheap enough to call before a long run."""
        if shutil.which(self.binary) is None:
            return False
        return (
            self._run(["version", "--format", "{{.Server.Version}}"], timeout_s=30).exit_code == 0
        )
