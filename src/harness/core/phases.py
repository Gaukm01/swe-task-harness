"""The phase machine.

```
BASE ──┬─ validate lane ─> GUARDED ─> GOLD
       └─ run lane ──────> SOLVE ───> SCORED
```

SOLVE and SCORED branch from BASE, never from GUARDED or GOLD. Those two carry
guardrail test files on disk, so reusing them would hand the solver the hidden
tests. That is invariant 1, and it is enforced here by the fact that every
lane's entry point takes the BASE tag and nothing else can be passed instead.

This module imports nothing from `runtime`. It receives a `ContainerRuntime`
and calls the protocol, which is what lets the whole state machine be tested
against `FakeRuntime` with no daemon.

M3 implements BASE. The other four phases land in M4 and M5.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from harness.adapters import smoke_argv
from harness.core.bundle import TaskSpec
from harness.core.cache import CACHE_KEY_TAG_LEN
from harness.core.errors import HarnessError
from harness.core.runtime import ContainerRuntime, ContainerSpec, ExecResult

# Where the harness expects a repo to live. An image that ships the code
# somewhere else declares it as `environment.repo_path_in_image`, and the path
# jail is anchored to whatever this resolves to -- moving a prebuilt image's
# repo would break editable installs and hardcoded paths inside it.
DEFAULT_REPO_ROOT = "/workspace/repo"

# The synthetic identity for the root commit BASE prep creates. Fixed, so the
# same tree always produces the same commit and snapshots stay reproducible.
SYNTHETIC_AUTHOR_NAME = "swe-task-harness"
SYNTHETIC_AUTHOR_EMAIL = "harness@localhost"
SYNTHETIC_COMMIT_MESSAGE = "base snapshot"
# A fixed timestamp keeps the commit sha a function of the tree alone.
SYNTHETIC_COMMIT_DATE = "2000-01-01T00:00:00+00:00"


class Phase(StrEnum):
    """The five states a task passes through."""

    BASE = "base"
    GUARDED = "guarded"
    GOLD = "gold"
    SOLVE = "solve"
    SCORED = "scored"


def repo_root(spec: TaskSpec) -> str:
    """Where this task's repo lives inside its container."""
    return spec.environment.repo_path_in_image or DEFAULT_REPO_ROOT


def base_tag(task_id: str, cache_key: str) -> str:
    """Image tag for a BASE snapshot.

    Keyed by cache key rather than by run id: BASE is shared across every run of
    the same environment, and looking up "does this image exist" *is* the cache
    lookup. The per-run phases use the run id instead, because they are not
    shareable -- a GUARDED image contains hidden tests.
    """
    return f"harness/{task_id}:base-{cache_key[:CACHE_KEY_TAG_LEN]}"


def phase_tag(task_id: str, run_id: str, phase: Phase) -> str:
    """Image tag for a per-run phase snapshot."""
    return f"harness/{task_id}:{run_id}-{phase.value}"


class PhaseError(HarnessError):
    """A phase transition could not be completed."""


@dataclass
class StepLog:
    """One command a phase ran, kept for the run log."""

    label: str
    argv: list[str]
    exit_code: int
    duration_ms: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    # Best-effort steps are allowed to fail. Rendering them as failures would
    # train a reader to ignore red, which is worse than not showing them at all.
    optional: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def tolerated(self) -> bool:
        """Failed, but harmlessly so."""
        return self.optional and not self.ok


@dataclass
class BaseResult:
    """What `prepare_base` produced."""

    image: str
    cache_key: str
    repo_root: str
    cached: bool
    steps: list[StepLog] = field(default_factory=list)
    base_commit_sha: str | None = None

    @property
    def duration_ms(self) -> int:
        return sum(step.duration_ms for step in self.steps)


def _record(
    steps: list[StepLog], label: str, result: ExecResult, *, optional: bool = False
) -> ExecResult:
    steps.append(
        StepLog(
            label=label,
            argv=result.argv,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            stdout=result.stdout[-4000:],
            stderr=result.stderr[-4000:],
            timed_out=result.timed_out,
            optional=optional,
        )
    )
    return result


def _require(result: ExecResult, label: str, fix: str) -> ExecResult:
    if not result.ok:
        raise PhaseError(f"{label} failed ({result.failure_summary()}).", fix=fix)
    return result


def resolve_base_image(
    runtime: ContainerRuntime,
    spec: TaskSpec,
    bundle_dir: Path,
    *,
    tag: str,
    no_cache: bool = False,
) -> str:
    """Get the image the BASE phase starts from, building or pulling as needed."""
    environment = spec.environment
    if environment.image:
        if no_cache or not runtime.image_exists(environment.image):
            runtime.pull(environment.image, platform=environment.platform)
        return environment.image

    if environment.dockerfile:
        return runtime.build(
            context_dir=bundle_dir,
            dockerfile=environment.dockerfile,
            tag=f"{tag}-src",
            platform=environment.platform,
            no_cache=no_cache,
        )

    recipe = environment.recipe
    assert recipe is not None  # guaranteed by TaskSpec's exactly-one validator
    if no_cache or not runtime.image_exists(recipe.base_image):
        runtime.pull(recipe.base_image, platform=environment.platform)
    return recipe.base_image


def _git(root: str, *args: str) -> list[str]:
    """A git argv scoped to the repo, with no shell in sight."""
    return ["git", "-C", root, *args]


def prepare_repo_git(
    runtime: ContainerRuntime,
    container_id: str,
    spec: TaskSpec,
    steps: list[StepLog],
) -> str | None:
    """Normalize the repo's git state, and return the synthetic root commit sha.

    Four things happen here, in this order and for these reasons:

    1. **Check out `base_commit` detached.** Only meaningful when the image
       ships real history.
    2. **Remove the `origin` remote.** A solver with a remote and any network
       could fetch the upstream commit that contains the fix.
    3. **Delete `.git` and re-init.** Truncating history is the point: the
       commits after `base_commit` contain the very patch being asked for, and
       `git log`/`git show` would hand them over. Re-initialising also
       normalizes whatever state a prebuilt image happened to ship.
    4. **Commit the whole tree as one synthetic root commit.** Every later diff
       -- including `solution.diff` -- is then taken against a known clean
       baseline rather than against whatever the image's HEAD was.
    """
    root = repo_root(spec)

    if spec.base_commit:
        _require(
            _record(
                steps,
                f"checkout {spec.base_commit[:12]}",
                runtime.exec(container_id, _git(root, "checkout", "--detach", spec.base_commit)),
            ),
            f"checking out base commit {spec.base_commit[:12]}",
            fix="Confirm the commit exists in the image's clone of the repo.",
        )

    # Best-effort: an image whose repo has no origin is fine, so a failure here
    # is not fatal. The `.git` removal below is what actually guarantees it.
    _record(
        steps,
        "remove origin remote",
        runtime.exec(container_id, _git(root, "remote", "remove", "origin")),
        optional=True,
    )

    _require(
        _record(
            steps,
            "truncate history",
            runtime.exec(container_id, ["rm", "-rf", f"{root}/.git"]),
        ),
        "removing git history",
        fix=f"Check that {root} exists inside the image and is writable.",
    )

    for label, argv in (
        ("git init", _git(root, "init", "--quiet", "--initial-branch=main")),
        ("git config name", _git(root, "config", "user.name", SYNTHETIC_AUTHOR_NAME)),
        ("git config email", _git(root, "config", "user.email", SYNTHETIC_AUTHOR_EMAIL)),
        ("git add", _git(root, "add", "--all")),
    ):
        _require(
            _record(steps, label, runtime.exec(container_id, argv)),
            label,
            fix=f"Ensure git is installed in the image and {root} is writable.",
        )

    commit = _require(
        _record(
            steps,
            "synthetic root commit",
            runtime.exec(
                container_id,
                _git(
                    root,
                    "commit",
                    "--quiet",
                    "--allow-empty",
                    "--no-verify",
                    "--date",
                    SYNTHETIC_COMMIT_DATE,
                    "--message",
                    SYNTHETIC_COMMIT_MESSAGE,
                ),
                env={
                    "GIT_AUTHOR_DATE": SYNTHETIC_COMMIT_DATE,
                    "GIT_COMMITTER_DATE": SYNTHETIC_COMMIT_DATE,
                },
            ),
        ),
        "creating the synthetic base commit",
        fix=f"Ensure git is installed in the image and {root} is writable.",
    )
    del commit

    head = _record(
        steps, "resolve HEAD", runtime.exec(container_id, _git(root, "rev-parse", "HEAD"))
    )
    return head.stdout.strip() or None


def assert_test_runner_executes(
    runtime: ContainerRuntime,
    container_id: str,
    spec: TaskSpec,
    steps: list[StepLog],
) -> None:
    """BASE's assertion: the test runner is actually invokable in this image.

    Cheap, and it fails here with a clear message rather than at grading time
    where a missing runner would be indistinguishable from a broken solution.
    """
    argv = smoke_argv(spec.tests.framework)
    result = _record(
        steps,
        f"{spec.tests.framework} available",
        runtime.exec(container_id, argv, workdir=repo_root(spec), timeout_s=120),
    )
    if not result.ok:
        raise PhaseError(
            f"the {spec.tests.framework} runner does not execute in this image "
            f"({result.failure_summary()}).",
            fix=f"Install it in the image, or correct tests.framework. Tried: {' '.join(argv)}",
        )


def prepare_base(
    runtime: ContainerRuntime,
    spec: TaskSpec,
    bundle_dir: Path,
    *,
    cache_key: str,
    no_cache: bool = False,
) -> BaseResult:
    """Build the BASE snapshot, or report the cached one.

    BASE contains: the repo at `base_commit`, dependencies installed, git
    history truncated to a single synthetic commit, and no `origin` remote. It
    is the only phase that is cached and shared, because it is the only one
    that holds no task-specific test material.
    """
    root = repo_root(spec)
    tag = base_tag(spec.task_id, cache_key)

    if not no_cache and runtime.image_exists(tag):
        return BaseResult(image=tag, cache_key=cache_key, repo_root=root, cached=True)

    source_image = resolve_base_image(runtime, spec, bundle_dir, tag=tag, no_cache=no_cache)

    steps: list[StepLog] = []
    container_id = runtime.create(
        ContainerSpec(
            image=source_image,
            platform=spec.environment.platform,
            workdir=root,
        )
    )
    try:
        probe = _record(steps, "repo present", runtime.exec(container_id, ["test", "-d", root]))
        if not probe.ok:
            raise PhaseError(
                f"no repo at {root} inside {source_image}.",
                fix="Set environment.repo_path_in_image to where the image keeps the repo, "
                f"or have the image place it at {DEFAULT_REPO_ROOT}.",
            )

        head_sha = prepare_repo_git(runtime, container_id, spec, steps)
        assert_test_runner_executes(runtime, container_id, spec, steps)
        runtime.commit(container_id, tag)
    finally:
        # A leaked container holds its whole image layer on disk.
        runtime.remove_container(container_id, force=True)

    return BaseResult(
        image=tag,
        cache_key=cache_key,
        repo_root=root,
        cached=False,
        steps=steps,
        base_commit_sha=head_sha,
    )
