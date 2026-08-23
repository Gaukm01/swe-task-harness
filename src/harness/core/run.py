"""The run lane orchestrator: validate -> solve -> grade.

Invariant 3 lives here. Baseline validation happens *inside* `task run`, not
only in `task validate`, and it aborts before the solver starts. Putting it in
the orchestrator rather than leaving it to the user means it cannot be
forgotten or skipped -- a run against a task whose baseline does not hold would
produce a verdict that means nothing.

Imports no runtime implementation, only the protocol.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from harness.core.bundle import Bundle
from harness.core.errors import BaselineValidationError
from harness.core.gaming import detect_gaming_flags
from harness.core.phases import (
    BaseResult,
    ValidationResult,
    grade_solution,
    run_solver,
    validate_task,
)
from harness.core.results import (
    Bucket,
    Outcome,
    TestOutcome,
    TestTransition,
    Transition,
    derive_outcome,
    pair_outcomes,
)
from harness.core.runtime import ContainerRuntime
from harness.core.testrun import TestRun
from harness.solvers.base import Solver, SolverResult


@dataclass
class Timings:
    """Wall-clock per stage, in milliseconds."""

    setup: int = 0
    validate: int = 0
    solve: int = 0
    grade: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "setup": self.setup,
            "validate": self.validate,
            "solve": self.solve,
            "grade": self.grade,
        }


@dataclass
class RunResult:
    """Everything one run produced."""

    run_id: str
    bundle: Bundle
    base: BaseResult
    validation: ValidationResult
    solution_diff: str
    solver: SolverResult
    solve_image: str
    scored_image: str
    baseline: list[TestOutcome]
    post: list[TestOutcome]
    transitions: list[TestTransition]
    outcome: Outcome
    gaming_flags: list[str]
    restored_test_paths: list[str]
    timings: Timings
    artifact_dir: Path
    post_run: TestRun | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, int]:
        f2p = [t for t in self.transitions if t.bucket is Bucket.F2P]
        p2p = [t for t in self.transitions if t.bucket is Bucket.P2P]
        return {
            "f2p_fixed": sum(1 for t in f2p if t.transition is Transition.FIXED),
            "f2p_total": len(f2p),
            "p2p_regressed": sum(1 for t in p2p if t.transition is Transition.REGRESSED),
            "p2p_total": len(p2p),
        }


class _Clock:
    """Millisecond stopwatch."""

    def __init__(self) -> None:
        self._start = time.monotonic()

    def lap(self) -> int:
        now = time.monotonic()
        elapsed = int((now - self._start) * 1000)
        self._start = now
        return elapsed


def execute_run(
    runtime: ContainerRuntime,
    bundle: Bundle,
    base: BaseResult,
    solver: Solver,
    *,
    run_id: str,
    artifact_dir: Path,
    cached_baseline: list[TestOutcome] | None = None,
    keep_snapshots: bool = True,
) -> RunResult:
    """Validate the baseline, run the solver, grade the result.

    `cached_baseline` lets a previously-validated (bundle, environment) pair
    skip re-running the guardrails. The cache key is checked by the caller;
    what matters here is that a run without a verified baseline is impossible
    to express -- there is no parameter that turns validation off.
    """
    clock = _Clock()
    timings = Timings(setup=clock.lap())

    validation = validate_task(
        runtime,
        bundle,
        base,
        validation_id=run_id,
        artifact_dir=artifact_dir / "pre",
        keep_snapshots=keep_snapshots,
    )
    timings.validate = clock.lap()

    if not validation.ok:
        # Abort before the solver starts. Grading against a baseline that does
        # not hold would produce a verdict about nothing.
        raise BaselineValidationError(
            f"{bundle.spec.task_id} failed baseline validation: {validation.problems[0]}",
            fix="Run `task validate` on the bundle and fix it before running a solver. "
            f"Artifacts are in {artifact_dir / 'pre'}.",
        )

    baseline = cached_baseline if cached_baseline is not None else validation.guarded.outcomes

    solve = run_solver(
        runtime, bundle, base, solver, run_id=run_id, keep_snapshot=keep_snapshots
    )
    timings.solve = clock.lap()

    post_run, restored, scored_image = grade_solution(
        runtime,
        bundle,
        base,
        solve.diff,
        run_id=run_id,
        artifact_dir=artifact_dir / "post",
        keep_snapshot=keep_snapshots,
    )
    timings.grade = clock.lap()

    transitions = pair_outcomes(baseline, post_run.outcomes)
    gaming_flags = detect_gaming_flags(solve.diff, bundle.spec.tests.test_path_globs)
    outcome = derive_outcome(transitions, gaming_flags=gaming_flags)

    notes = list(solve.solver.notes)
    if solve.is_empty:
        notes.append("the solver produced an empty diff")

    return RunResult(
        run_id=run_id,
        bundle=bundle,
        base=base,
        validation=validation,
        solution_diff=solve.diff,
        solver=solve.solver,
        solve_image=solve.image,
        scored_image=scored_image,
        baseline=baseline,
        post=post_run.outcomes,
        transitions=transitions,
        outcome=outcome,
        gaming_flags=gaming_flags,
        restored_test_paths=restored,
        timings=timings,
        artifact_dir=artifact_dir,
        post_run=post_run,
        notes=notes,
    )
