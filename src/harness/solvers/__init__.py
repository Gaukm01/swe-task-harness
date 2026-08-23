"""Solver implementations behind one protocol."""

from __future__ import annotations

from harness.core.errors import UsageError
from harness.solvers.base import Solver, SolverContext, SolverResult
from harness.solvers.builtin import CmdSolver, GoldSolver, NoopSolver

__all__ = [
    "CmdSolver",
    "GoldSolver",
    "NoopSolver",
    "Solver",
    "SolverContext",
    "SolverResult",
    "resolve_solver",
]

_PENDING = {
    "agent": "M6",
    "replay": "M6",
}


def resolve_solver(spec: str) -> Solver:
    """Turn a `--solver` string into a solver.

    Accepts `gold`, `noop`, and `cmd:<command>`. `agent` and `replay:<run_id>`
    are recognized so the error names the milestone rather than looking like a
    typo.
    """
    kind, _, argument = spec.partition(":")
    kind = kind.strip()

    if kind == "gold":
        return GoldSolver()
    if kind == "noop":
        return NoopSolver()
    if kind == "cmd":
        if not argument.strip():
            raise UsageError(
                "--solver cmd: needs a command.",
                fix='Try --solver "cmd:python -c \'...\'".',
            )
        return CmdSolver(argument)
    if kind in _PENDING:
        raise UsageError(
            f"--solver {kind} is not implemented yet.",
            fix=f"It lands in milestone {_PENDING[kind]}. Available now: gold, noop, cmd:<command>.",
        )
    raise UsageError(
        f"unknown solver {spec!r}.",
        fix="Available: gold, noop, cmd:<command>.",
    )
