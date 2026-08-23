"""The zero-API-call solvers.

`gold` and `noop` are the harness's own regression suite: `gold` must grade
`resolved` and `noop` must grade `unresolved` with every f2p `still_failing`.
Between them they exercise the entire run lane -- branch from BASE, produce a
diff, force-restore, grade, report -- without spending a token, which is what
makes the rest of the project iterable against a rate-limited key.
"""

from __future__ import annotations

from harness.core.bundle import Bundle
from harness.core.errors import SolverFailedError
from harness.solvers.base import SolverContext, SolverResult

# Where a solver's working files go: outside the repo, so `git add -A` cannot
# sweep them into solution.diff.
SOLVER_SCRATCH = "/tmp/harness"


class GoldSolver:
    """Applies the bundle's gold patch. Must grade `resolved`.

    This is the one solver that legitimately sees `patch.diff` -- it *is* the
    patch. It exists to prove the grading path reports success when success is
    real, which is the half of the harness that a broken-by-default system
    would never catch.
    """

    kind = "gold"

    def solve(self, context: SolverContext, bundle: Bundle) -> SolverResult:
        runtime = context.runtime
        patch_path = f"{SOLVER_SCRATCH}/gold-solution.diff"
        runtime.exec(context.container_id, ["mkdir", "-p", SOLVER_SCRATCH])
        runtime.write_file(context.container_id, patch_path, bundle.patch)

        result = runtime.exec(
            context.container_id,
            ["git", "-C", context.repo_root, "apply", "--whitespace=nowarn", patch_path],
            timeout_s=context.timeout_s,
        )
        if not result.ok:
            raise SolverFailedError(
                f"the gold solver could not apply patch.diff ({result.failure_summary()}).",
                fix="Run `task validate` -- the gold patch must apply to the base tree.",
            )
        return SolverResult(kind=self.kind, summary="applied the gold patch verbatim")


class NoopSolver:
    """Changes nothing. Must grade `unresolved`, every f2p `still_failing`.

    The more valuable of the two. A harness that reports success too easily is
    worse than one that fails loudly, and `noop` is what proves it does not --
    it produces an empty diff and must still be graded, reported, and recorded
    like any other run.
    """

    kind = "noop"

    def solve(self, context: SolverContext, bundle: Bundle) -> SolverResult:
        return SolverResult(kind=self.kind, summary="made no changes")


class CmdSolver:
    """Runs an arbitrary command inside the solve container as the solver.

    An escape hatch for trying a fix by hand, or for wiring in an external
    agent. The command is passed as a single argv element to `bash -lc`, so it
    is data to the host and shell only inside the container -- which is
    already network-isolated and disposable.
    """

    kind = "cmd"

    def __init__(self, command: str) -> None:
        self.command = command

    def solve(self, context: SolverContext, bundle: Bundle) -> SolverResult:
        result = context.runtime.exec(
            context.container_id,
            ["bash", "-lc", self.command],
            workdir=context.repo_root,
            timeout_s=context.timeout_s,
        )
        notes: list[str] = []
        if result.timed_out:
            # Not fatal: grading proceeds on whatever diff exists.
            notes.append(f"command timed out after {context.timeout_s}s")
        elif not result.ok:
            notes.append(f"command exited {result.exit_code}")
        return SolverResult(
            kind=self.kind, summary=f"ran: {self.command}", notes=notes
        )
