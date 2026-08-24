"""An in-memory `ContainerRuntime` for unit tests.

The point of this class is that the phase machine, BASE preparation,
force-restore ordering, and the path jail can all be tested without Docker, in
milliseconds, including their failure paths -- a container that OOMs, an exec
that times out, a commit that fails on a full disk. Those paths are the ones
that matter most and are the hardest to provoke against a real daemon.

Commands are matched by a predicate against the argv list, so a test says
"whatever runs `git init` fails" rather than pinning an exact string.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from harness.core.runtime import ContainerSpec, ExecResult, ImageInfo

Matcher = Callable[[list[str]], bool]


def argv_contains(*needles: str) -> Matcher:
    """Match an argv that contains all of `needles` as consecutive-ish tokens."""

    def _match(argv: list[str]) -> bool:
        joined = " ".join(argv)
        return all(needle in joined for needle in needles)

    return _match


@dataclass
class ScriptedExec:
    """A canned response for any exec whose argv matches."""

    matcher: Matcher
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_ms: int = 1
    # None means "applies to every matching call"; a number limits how many times.
    remaining: int | None = None


@dataclass
class FakeContainer:
    """A container that was created but never really existed."""

    container_id: str
    spec: ContainerSpec
    removed: bool = False


class FakeRuntime:
    """Records what was asked of it and replays scripted answers."""

    def __init__(self, *, existing_images: set[str] | None = None) -> None:
        self.images: set[str] = set(existing_images or set())
        self.containers: dict[str, FakeContainer] = {}
        self.execs: list[tuple[str, list[str]]] = []
        self.commits: list[tuple[str, str]] = []
        self.builds: list[tuple[Path, str, str]] = []
        self.pulls: list[str] = []
        self.removed_images: list[str] = []
        self.files_written: dict[str, str] = {}
        self.copied_out: list[tuple[str, str, Path]] = []
        self.scripted: list[ScriptedExec] = []
        self._next_id = 0
        # Populated by copy_out so a test can hand back junit XML.
        self.copy_out_payloads: dict[str, str] = {}

    # -- scripting --------------------------------------------------------

    def script(
        self,
        matcher: Matcher,
        *,
        exit_code: int = 0,
        stdout: str = "",
        stderr: str = "",
        timed_out: bool = False,
        times: int | None = None,
    ) -> None:
        """Make matching execs return this instead of a bare success."""
        self.scripted.append(
            ScriptedExec(
                matcher=matcher,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                timed_out=timed_out,
                remaining=times,
            )
        )

    def exec_argvs(self) -> list[list[str]]:
        """Every argv exec'd, in order. The assertion surface for phase ordering."""
        return [argv for _, argv in self.execs]

    def ran(self, *needles: str) -> bool:
        """True if some exec matched all the needles."""
        matcher = argv_contains(*needles)
        return any(matcher(argv) for argv in self.exec_argvs())

    def index_of(self, *needles: str) -> int:
        """Position of the first exec matching the needles, or -1.

        Used to assert ordering: force-restore must happen before the test run,
        and 'happens before' is the whole guarantee.
        """
        matcher = argv_contains(*needles)
        for index, argv in enumerate(self.exec_argvs()):
            if matcher(argv):
                return index
        return -1

    # -- ContainerRuntime -------------------------------------------------

    def image_exists(self, ref: str) -> bool:
        return ref in self.images

    def image_digest(self, ref: str) -> str | None:
        return f"sha256:fake-{ref}" if ref in self.images else None

    def pull(self, ref: str, *, platform: str | None = None) -> str:
        self.pulls.append(ref)
        self.images.add(ref)
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
        self.builds.append((context_dir, dockerfile, tag))
        self.images.add(tag)
        return tag

    def create(self, spec: ContainerSpec) -> str:
        self._next_id += 1
        container_id = f"fake{self._next_id:04d}"
        self.containers[container_id] = FakeContainer(container_id, spec)
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
        self.execs.append((container_id, argv))
        for scripted in self.scripted:
            if not scripted.matcher(argv):
                continue
            if scripted.remaining is not None:
                if scripted.remaining <= 0:
                    continue
                scripted.remaining -= 1
            return ExecResult(
                argv=argv,
                exit_code=scripted.exit_code,
                stdout=scripted.stdout,
                stderr=scripted.stderr,
                duration_ms=scripted.duration_ms,
                timed_out=scripted.timed_out,
            )
        return ExecResult(
            argv=argv, exit_code=0, stdout=self._default_stdout(argv), stderr="", duration_ms=1
        )

    def _default_stdout(self, argv: list[str]) -> str:
        """What an unscripted command prints.

        The fake models a *healthy* container: one whose image actually
        contains a repo. So a directory listing comes back non-empty, and a
        test that wants to model a missing or empty repo scripts that
        explicitly. The alternative -- an empty default -- meant every test
        silently modelled a broken image.
        """
        if argv and argv[0] == "ls":
            return "src\ntests\nREADME.md\n"
        return ""

    def commit(self, container_id: str, tag: str) -> str:
        self.commits.append((container_id, tag))
        self.images.add(tag)
        return tag

    def remove_container(self, container_id: str, *, force: bool = True) -> None:
        if container_id in self.containers:
            self.containers[container_id].removed = True

    def remove_image(self, ref: str, *, force: bool = False) -> None:
        self.removed_images.append(ref)
        self.images.discard(ref)

    def copy_out(self, container_id: str, source: str, destination: Path) -> bool:
        self.copied_out.append((container_id, source, destination))
        payload = self.copy_out_payloads.get(source)
        if payload is None:
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(payload)
        return True

    def write_file(self, container_id: str, path: str, content: str) -> None:
        self.files_written[path] = content

    def list_images(self, prefix: str) -> list[ImageInfo]:
        return [
            ImageInfo(ref=ref, image_id=f"id-{ref}", created_at="", size_bytes=0)
            for ref in sorted(self.images)
            if ref.startswith(prefix)
        ]

    # -- assertions helpers ----------------------------------------------

    @property
    def leaked_containers(self) -> list[str]:
        """Containers that were created and never removed."""
        return [cid for cid, container in self.containers.items() if not container.removed]
