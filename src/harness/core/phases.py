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
"""

from __future__ import annotations

from dataclasses import dataclass, field
import secrets
from enum import StrEnum
from pathlib import Path

from harness.adapters import smoke_argv
from harness.core.bundle import Bundle, TaskSpec
from harness.core.cache import CACHE_KEY_TAG_LEN
from harness.core.errors import BaselineValidationError
from harness.core.results import Bucket, TestOutcome, TestStatus
from harness.core.runtime import ContainerRuntime, ContainerSpec, ExecResult
from harness.core.gaming import is_test_infrastructure
from harness.core.globs import matches_any
from harness.core.testrun import (
    CONTAINER_SCRATCH_DIR,
    TestRun,
    build_canary,
    run_selectors,
)
from harness.solvers.base import Solver, SolverContext, SolverResult

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


class PhaseError(BaselineValidationError):
    """A phase transition could not be completed.

    Exit code 4, not 1. Every way a phase can fail -- a patch that does not
    apply, a repo that is not where the bundle said, a test runner that is not
    installed -- means this task's baseline does not hold, and a caller must be
    able to tell that from a harness fault without reading the output. Genuine
    infrastructure failures raise DockerUnavailableError (7) instead.
    """


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


def clone_repo_if_needed(
    runtime: ContainerRuntime,
    container_id: str,
    spec: TaskSpec,
    steps: list[StepLog],
) -> None:
    """Clone the repo when the image does not already ship it.

    `environment.repo_path_in_image` set means the image carries the code and
    nothing is cloned. Otherwise `repo` is cloned to the default root. BASE
    preparation is the only phase permitted network access -- it runs before
    any solver exists, and the history it fetches is truncated moments later.
    """
    if spec.environment.repo_path_in_image or not spec.repo:
        return

    root = repo_root(spec)
    parent = root.rsplit("/", 1)[0] or "/"
    _record(steps, "make repo parent", runtime.exec(container_id, ["mkdir", "-p", parent]))
    _require(
        _record(
            steps,
            f"clone {spec.repo}",
            runtime.exec(container_id, ["git", "clone", "--quiet", spec.repo, root], timeout_s=1800),
        ),
        f"cloning {spec.repo}",
        fix="Check the repo URL, that git is installed in the base image "
        "(add it to recipe.install_cmds), and that the host has network access.",
    )


def run_install_cmds(
    runtime: ContainerRuntime,
    container_id: str,
    spec: TaskSpec,
    steps: list[StepLog],
) -> None:
    """Run a recipe's install commands inside the container.

    Each command is passed as a single argv element to `bash -lc`, exactly as
    the agent's `run_bash` tool is: the host shell never sees it. These strings
    come from the bundle, which is trusted input -- the threat model defends
    against the solver, not the task author.
    """
    recipe = spec.environment.recipe
    if recipe is None:
        return
    for index, command in enumerate(recipe.install_cmds):
        _require(
            _record(
                steps,
                f"install [{index + 1}/{len(recipe.install_cmds)}]",
                # No workdir: the repo does not exist yet at this point.
                runtime.exec(container_id, ["bash", "-lc", command], timeout_s=3600),
            ),
            f"install command {index + 1} ({command[:60]})",
            fix="Run it by hand in the base image to see the full output.",
        )


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
        )
    )
    try:
        # Install first, then clone: a recipe's whole purpose is turning a bare
        # base image into a working environment, and you cannot `git clone`
        # without git. Post-clone setup (`pip install -e .`) belongs in a
        # Dockerfile, which runs with the source already present.
        run_install_cmds(runtime, container_id, spec, steps)
        clone_repo_if_needed(runtime, container_id, spec, steps)

        # `ls -A`, not `test -d`, and the container is created with NO workdir.
        # Docker *creates* a missing --workdir, so a container started with
        # --workdir <root> always has <root> and this guard could never fire; a
        # misconfigured repo path then surfaced as a confusing git failure
        # several steps later instead of the clear error below. `ls -A` exits
        # non-zero when the path is missing and prints nothing when it is empty,
        # covering both cases with no shell.
        probe = _record(steps, "repo present", runtime.exec(container_id, ["ls", "-A", "--", root]))
        if not probe.ok or not probe.stdout.strip():
            raise PhaseError(
                f"no repo at {root} inside {source_image}.",
                fix="Set environment.repo_path_in_image to where the image keeps the repo, "
                "or set `repo` so the harness clones it, "
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


# ---------------------------------------------------------------------------
# The validate lane: BASE -> GUARDED -> GOLD
# ---------------------------------------------------------------------------


@dataclass
class PhaseAssertion:
    """What one phase asserted, and whether it held."""

    phase: Phase
    image: str
    run: TestRun
    problems: list[str] = field(default_factory=list)
    # False once the snapshot has been discarded. See validate_task.
    retained: bool = True

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def outcomes(self) -> list[TestOutcome]:
        return self.run.outcomes


@dataclass
class ValidationResult:
    """The full validate lane."""

    validation_id: str
    base: BaseResult
    guarded: PhaseAssertion
    gold: PhaseAssertion

    @property
    def ok(self) -> bool:
        return self.guarded.ok and self.gold.ok

    @property
    def problems(self) -> list[str]:
        return [*self.guarded.problems, *self.gold.problems]

    @property
    def blocked_by_infrastructure(self) -> bool:
        """True when validation failed because the tests could not be run at all.

        A bundle whose baseline does not hold and an environment that cannot
        execute the suite are different failures and deserve different exit
        codes. A test that segfaults the interpreter, or a run killed at the
        wall clock, says nothing about whether the task is well-formed --
        reporting it as `baseline validation failed` sends whoever reads it to
        fix a bundle that is fine.

        This mirrors what the SCORED phase already does: infrastructure that
        fails there grades `inconclusive`, never `unresolved`.
        """
        return any(
            outcome.status.is_infra
            for assertion in (self.guarded, self.gold)
            for outcome in assertion.outcomes
        )


def apply_patch(
    runtime: ContainerRuntime,
    container_id: str,
    spec: TaskSpec,
    *,
    patch_text: str,
    label: str,
    steps: list[StepLog],
) -> None:
    """Write a patch into the container and apply it with git.

    The patch is written as a file rather than piped through a shell heredoc:
    diff content is full of quotes, backslashes, and `$`, and the only way to
    be sure none of it is reinterpreted is for it never to touch a shell.
    """
    root = repo_root(spec)
    remote_path = f"{CONTAINER_SCRATCH_DIR}/{label}.diff"
    runtime.exec(container_id, ["mkdir", "-p", CONTAINER_SCRATCH_DIR])
    runtime.write_file(container_id, remote_path, patch_text)

    result = _record(
        steps,
        f"apply {label}",
        runtime.exec(
            container_id,
            _git(root, "apply", "--verbose", "--whitespace=nowarn", remote_path),
        ),
    )
    if not result.ok:
        raise PhaseError(
            f"applying {label} failed ({result.failure_summary()}).",
            fix=f"The patch does not apply to the base tree. Regenerate it against "
            f"{spec.base_commit or 'the base snapshot'} with `git diff`.",
        )


def _describe(outcomes: list[TestOutcome], statuses: set[TestStatus]) -> str:
    return ", ".join(
        f"{outcome.test_id} [{outcome.status.value}]"
        for outcome in outcomes
        if outcome.status in statuses
    )


def assert_guarded(run: TestRun) -> list[str]:
    """GUARDED must show every p2p passing and every f2p failing.

    An f2p test that already passes on the unfixed code proves nothing -- it
    would let a no-op solver grade as resolved. A p2p test that fails here means
    the baseline is broken before any solver has touched it.
    """
    problems: list[str] = []
    infra = [o for o in run.outcomes if o.status.is_infra]
    if infra:
        return [
            "the baseline test run did not complete: "
            + _describe(infra, {TestStatus.TIMEOUT, TestStatus.INFRA_ERROR})
        ]

    f2p = [o for o in run.outcomes if o.bucket is Bucket.F2P]
    p2p = [o for o in run.outcomes if o.bucket is Bucket.P2P]

    passing_f2p = [o for o in f2p if o.passed]
    if passing_f2p:
        problems.append(
            "these fail_to_pass tests already pass without the fix, so they prove nothing: "
            + ", ".join(o.test_id for o in passing_f2p)
        )

    missing_f2p = [o for o in f2p if o.status is TestStatus.NOT_FOUND]
    if missing_f2p:
        problems.append(
            "these fail_to_pass selectors produced no result: "
            + ", ".join(o.test_id for o in missing_f2p)
        )

    failing_p2p = [o for o in p2p if not o.passed]
    if failing_p2p:
        problems.append(
            "these pass_to_pass tests do not pass at baseline: "
            + _describe(failing_p2p, {o.status for o in failing_p2p})
        )
    return problems


def assert_gold(run: TestRun) -> list[str]:
    """GOLD must show everything passing.

    If the gold patch does not make the f2p tests pass, the task is
    unsatisfiable and no solver result from it would mean anything.
    """
    infra = [o for o in run.outcomes if o.status.is_infra]
    if infra:
        return [
            "the gold test run did not complete: "
            + _describe(infra, {TestStatus.TIMEOUT, TestStatus.INFRA_ERROR})
        ]

    problems: list[str] = []
    failing_f2p = [o for o in run.outcomes if o.bucket is Bucket.F2P and not o.passed]
    if failing_f2p:
        problems.append(
            "the gold patch does not make these fail_to_pass tests pass: "
            + _describe(failing_f2p, {o.status for o in failing_f2p})
        )

    failing_p2p = [o for o in run.outcomes if o.bucket is Bucket.P2P and not o.passed]
    if failing_p2p:
        problems.append(
            "the gold patch breaks these pass_to_pass tests: "
            + _describe(failing_p2p, {o.status for o in failing_p2p})
        )
    return problems


def validate_task(
    runtime: ContainerRuntime,
    bundle: Bundle,
    base: BaseResult,
    *,
    validation_id: str,
    artifact_dir: Path,
    keep_snapshots: bool = False,
    artifact_nonce: str | None = None,
) -> ValidationResult:
    """Run the validate lane: BASE -> GUARDED -> GOLD.

    GUARDED and GOLD chain inside one container, which is correct: GOLD is
    defined as GUARDED plus the gold patch. Note what does *not* happen here --
    nothing downstream branches from either image. SOLVE and SCORED start from
    BASE, because these two have the guardrail tests on disk.
    """
    spec = bundle.spec
    root = repo_root(spec)
    steps: list[StepLog] = []

    container_id = runtime.create(
        ContainerSpec(image=base.image, platform=spec.environment.platform, workdir=root)
    )
    try:
        apply_patch(
            runtime,
            container_id,
            spec,
            patch_text=bundle.test_patch,
            label="test_patch",
            steps=steps,
        )
        guarded_run = run_selectors(
            runtime,
            container_id,
            spec,
            label="guarded",
            workdir=root,
            artifact_dir=artifact_dir,
            artifact_nonce=artifact_nonce,
        )
        guarded_image = runtime.commit(
            container_id, phase_tag(spec.task_id, validation_id, Phase.GUARDED)
        )
        guarded = PhaseAssertion(
            phase=Phase.GUARDED,
            image=guarded_image,
            run=guarded_run,
            problems=assert_guarded(guarded_run),
        )

        apply_patch(
            runtime, container_id, spec, patch_text=bundle.patch, label="patch", steps=steps
        )
        gold_run = run_selectors(
            runtime,
            container_id,
            spec,
            label="gold",
            workdir=root,
            artifact_dir=artifact_dir,
            artifact_nonce=artifact_nonce,
        )
        gold_image = runtime.commit(
            container_id, phase_tag(spec.task_id, validation_id, Phase.GOLD)
        )
        gold = PhaseAssertion(
            phase=Phase.GOLD, image=gold_image, run=gold_run, problems=assert_gold(gold_run)
        )
    finally:
        runtime.remove_container(container_id, force=True)

    base.steps.extend(steps)
    result = ValidationResult(validation_id=validation_id, base=base, guarded=guarded, gold=gold)

    # Keep the evidence only when there is something to investigate. A passing
    # validation's snapshots are two full image layers per invocation with
    # nothing to look at; on a multi-gigabyte instance image, a handful of runs
    # fills a disk. A failing one is exactly when `task shell --phase guarded`
    # earns its keep, so those stay.
    if result.ok and not keep_snapshots:
        for assertion in (result.guarded, result.gold):
            runtime.remove_image(assertion.image, force=True)
            assertion.retained = False

    return result


# ---------------------------------------------------------------------------
# The run lane: BASE -> SOLVE -> SCORED
# ---------------------------------------------------------------------------


@dataclass
class SolveResult:
    """What the SOLVE phase produced."""

    image: str
    diff: str
    solver: SolverResult
    steps: list[StepLog] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.diff.strip()


def compute_solution_diff(
    runtime: ContainerRuntime, container_id: str, spec: TaskSpec, steps: list[StepLog]
) -> str:
    """Stage everything and diff it against the synthetic base commit.

    Invariant 7. The diff is computed by the harness, from the container's own
    git, against a commit the harness created -- never taken from whatever the
    solver claims it did. `git add -A` picks up new files and deletions, which
    a model-emitted patch routinely gets wrong.
    """
    root = repo_root(spec)
    _require(
        _record(steps, "stage solution", runtime.exec(container_id, _git(root, "add", "--all"))),
        "staging the solution",
        fix="The repo inside the solve container is not a usable git repo.",
    )
    result = _require(
        _record(
            steps,
            "compute solution.diff",
            # --binary: without it a solver that creates any binary file yields
            # "Binary files differ", which `git apply` then refuses -- losing the
            # entire run over a file the harness itself failed to encode.
            runtime.exec(container_id, _git(root, "diff", "--cached", "--binary", "HEAD")),
        ),
        "computing the solution diff",
        fix="The repo inside the solve container is not a usable git repo.",
    )
    return result.stdout


def run_solver(
    runtime: ContainerRuntime,
    bundle: Bundle,
    base: BaseResult,
    solver: Solver,
    *,
    run_id: str,
    keep_snapshot: bool = True,
) -> SolveResult:
    """The SOLVE phase: fresh from BASE, no network, solver runs, diff computed.

    Branches from BASE, never from GUARDED or GOLD -- those have the guardrail
    tests on disk, and starting from one would hand the solver the hidden
    tests. That is invariant 1, and it is why this takes `base` rather than a
    generic image reference.
    """
    spec = bundle.spec
    root = repo_root(spec)
    steps: list[StepLog] = []

    spec_for_container = ContainerSpec(
        image=base.image, platform=spec.environment.platform, workdir=root
    ).hardened()

    container_id = runtime.create(spec_for_container)
    try:
        context = SolverContext(
            runtime=runtime,
            container_id=container_id,
            repo_root=root,
            description=bundle.description,
            timeout_s=spec.tests.timeout_s,
        )
        solver_result = solver.solve(context, bundle)
        diff = compute_solution_diff(runtime, container_id, spec, steps)
        image = runtime.commit(container_id, phase_tag(spec.task_id, run_id, Phase.SOLVE))
    finally:
        runtime.remove_container(container_id, force=True)

    if not keep_snapshot:
        runtime.remove_image(image, force=True)

    return SolveResult(image=image, diff=diff, solver=solver_result, steps=steps)


def force_restore_tests(
    runtime: ContainerRuntime,
    container_id: str,
    spec: TaskSpec,
    steps: list[StepLog],
) -> list[str]:
    """Undo any change the solver made to a test file. Invariant 2.

    Two directions, both necessary:

    * **Restore.** Every tracked path matching `test_path_globs` is checked out
      from the synthetic base commit, undoing edits and deletions.
    * **Remove.** Any *untracked* file matching the globs is deleted. Without
      this, a solver could add a `conftest.py` with an autouse fixture that
      fakes results, and restoring tracked files alone would leave it in place.

    Scope is `test_path_globs` plus a narrow set of pure test-runner config
    files (`conftest.py`, `pytest.ini`, `jest.config.*`) -- a root `conftest.py`
    matches no `tests/**` glob but can fake every result in the suite. Files
    that mix test config with real project config (`pyproject.toml`,
    `setup.cfg`, `package.json`) are deliberately *not* restored, because a
    genuine fix may need to change them; those are covered by a gaming flag.

    Returns the paths it acted on, so the run log can show that it did.
    """
    root = repo_root(spec)
    globs = spec.tests.test_path_globs

    tracked = _record(
        steps,
        "list tracked files",
        runtime.exec(container_id, _git(root, "ls-tree", "-r", "--name-only", "HEAD")),
    )
    tracked_tests = [
        path
        for path in tracked.stdout.splitlines()
        if path and (matches_any(path, globs) or is_test_infrastructure(path))
    ]
    if tracked_tests:
        _require(
            _record(
                steps,
                f"restore {len(tracked_tests)} test file(s) from base",
                runtime.exec(container_id, _git(root, "checkout", "HEAD", "--", *tracked_tests)),
            ),
            "restoring test files from the base commit",
            fix="The solve container's repo is not in a usable git state.",
        )

    present = _record(
        steps,
        "list working tree",
        runtime.exec(
            container_id,
            _git(root, "ls-files", "--others", "--exclude-standard"),
        ),
    )
    added_tests = [
        path
        for path in present.stdout.splitlines()
        if path and (matches_any(path, globs) or is_test_infrastructure(path))
    ]
    if added_tests:
        # A solver-added conftest.py is the interesting case: it never appears
        # in HEAD, so a restore-only implementation would leave it running.
        _record(
            steps,
            f"remove {len(added_tests)} solver-added test file(s)",
            runtime.exec(
                container_id, ["rm", "-f", *[f"{root}/{path}" for path in added_tests]]
            ),
        )

    return sorted({*tracked_tests, *added_tests})


def _infra_run(spec: TaskSpec, reason: str) -> TestRun:
    """A post-run in which nothing could be measured. Grades `inconclusive`."""
    from harness.core.testrun import requested_map

    return TestRun(
        label="post",
        outcomes=[
            TestOutcome(test_id=selector, bucket=bucket, status=TestStatus.INFRA_ERROR,
                        message=reason)
            for selector, bucket in requested_map(spec).items()
        ],
        exec_result=ExecResult(argv=[], exit_code=-1, stdout="", stderr=reason, duration_ms=0),
        junit_path=None,
        argv=[],
    )


def grade_solution(
    runtime: ContainerRuntime,
    bundle: Bundle,
    base: BaseResult,
    solution_diff: str,
    *,
    run_id: str,
    artifact_dir: Path,
    keep_snapshot: bool = True,
    artifact_nonce: str | None = None,
) -> tuple[TestRun, list[str], str]:
    """The SCORED phase. Returns the test run, the restored paths, and the image.

    Order is the whole guarantee:

    1. fresh container **from BASE** -- not from SOLVE, which has whatever the
       solver left lying around, and not from GUARDED, which has the tests
    2. apply `solution.diff`
    3. **force-restore** the test files
    4. apply `test_patch.diff`
    5. run the selectors

    Steps 3 and 4 come after 2 on purpose. The solver's changes go on first so
    that its *source* edits are present, and are then overwritten wherever they
    touched a test.
    """
    spec = bundle.spec
    root = repo_root(spec)
    steps: list[StepLog] = []

    container_id = runtime.create(
        ContainerSpec(
            image=base.image, platform=spec.environment.platform, workdir=root
        ).hardened()
    )
    try:
        if solution_diff.strip():
            try:
                apply_patch(
                    runtime,
                    container_id,
                    spec,
                    patch_text=solution_diff,
                    label="solution",
                    steps=steps,
                )
            except PhaseError as error:
                # The baseline validated moments ago, so this is not a bad
                # bundle -- it is the harness failing to re-apply a diff it
                # computed itself (a binary file, a path the test patch also
                # creates). Blaming the task would be wrong and would discard
                # the run; report it as infrastructure and let grading finish.
                return (
                    _infra_run(spec, f"the solution diff could not be applied: {error.message}"),
                    [],
                    "",
                )
        restored = force_restore_tests(runtime, container_id, spec, steps)
        apply_patch(
            runtime,
            container_id,
            spec,
            patch_text=bundle.test_patch,
            label="test_patch",
            steps=steps,
        )
        # Written after force-restore and after the test patch, so nothing
        # removes it, and inside the guardrail tests' own directory so a forger
        # cannot skip it by filtering on path.
        canary = build_canary(spec, root, artifact_nonce or secrets.token_hex(8))
        if canary:
            _record(
                steps,
                "place integrity canary",
                runtime.exec(container_id, ["mkdir", "-p", canary.container_path.rsplit("/", 1)[0]]),
            )
            runtime.write_file(container_id, canary.container_path, canary.source)

        run = run_selectors(
            runtime,
            container_id,
            spec,
            label="post",
            workdir=root,
            artifact_dir=artifact_dir,
            artifact_nonce=artifact_nonce,
            canary=canary,
        )
        image = runtime.commit(container_id, phase_tag(spec.task_id, run_id, Phase.SCORED))
    finally:
        runtime.remove_container(container_id, force=True)

    if not keep_snapshot:
        runtime.remove_image(image, force=True)

    base.steps.extend(steps)
    return run, restored, image
