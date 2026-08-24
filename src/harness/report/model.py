"""`report.json` -- the committed deliverable artifact.

Shaped so someone with no access to the machine that produced it can tell what
happened: which bundle, which image, which solver, what each test did on both
sides, and why the verdict is what it is.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from harness.core.run import RunResult

# How many restored paths to name in the report before summarizing.
RESTORED_SAMPLE = 20


class SolverReport(BaseModel):
    kind: str
    model: str | None = None
    turns: int = 0
    cost_usd: float = 0.0
    summary: str | None = None


class TestReport(BaseModel):
    id: str
    bucket: str
    baseline: str
    post: str
    transition: str
    message: str | None = None
    duration_ms: int = 0


class RunReport(BaseModel):
    run_id: str
    task_id: str
    bundle_digest: str
    # The local BASE snapshot tag. Reproducible only on this machine.
    image: str
    # The upstream image this was built from, and the environment cache key.
    # Without these, someone holding only report.json cannot pin the environment.
    upstream_image: str | None = None
    cache_key: str | None = None
    base_commit: str | None = None
    solver: SolverReport
    outcome: str
    gaming_flags: list[str] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)
    tests: list[TestReport] = Field(default_factory=list)
    timings_ms: dict[str, int] = Field(default_factory=dict)
    artifacts: dict[str, str] = Field(default_factory=dict)
    # Not in the original sketch, but a report that says `unresolved` without
    # saying "the solver produced an empty diff" wastes the reader's time.
    notes: list[str] = Field(default_factory=list)
    # Evidence that force-restore ran, and on what. A count plus a sample: a
    # real repo has thousands of test files (ansible restores ~10k), and
    # enumerating them turned this artifact into 272KB of path list. The point
    # is that it ran and how widely, not a manifest.
    restored_test_count: int = 0
    restored_test_paths: list[str] = Field(default_factory=list)


def build_report(result: RunResult, *, artifacts: dict[str, str] | None = None) -> RunReport:
    """Turn a finished run into the report artifact."""
    return RunReport(
        run_id=result.run_id,
        task_id=result.bundle.spec.task_id,
        bundle_digest=result.bundle.digest,
        image=result.base.image,
        upstream_image=result.bundle.spec.environment.image,
        cache_key=result.base.cache_key,
        base_commit=result.base.base_commit_sha or result.bundle.spec.base_commit,
        solver=SolverReport(
            kind=result.solver.kind,
            model=result.solver.model,
            turns=result.solver.turns,
            cost_usd=result.solver.cost_usd,
            summary=result.solver.summary,
        ),
        outcome=result.outcome.value,
        gaming_flags=result.gaming_flags,
        summary=result.summary,
        tests=[
            TestReport(
                id=t.test_id,
                bucket=t.bucket.value,
                baseline=t.baseline.value,
                post=t.post.value,
                transition=t.transition.value,
                message=t.message,
                duration_ms=t.duration_ms,
            )
            for t in result.transitions
        ],
        timings_ms=result.timings.as_dict(),
        artifacts=artifacts or {},
        notes=result.notes,
        restored_test_count=len(result.restored_test_paths),
        restored_test_paths=result.restored_test_paths[:RESTORED_SAMPLE],
    )


def write_report(report: RunReport, path: Path) -> Path:
    """Write report.json. Stable key order, trailing newline, greppable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2) + "\n")
    return path
