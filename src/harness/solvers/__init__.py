"""Solver implementations behind one protocol."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from harness.core.errors import UsageError
from harness.solvers.agent import DEFAULT_MAX_TURNS, DEFAULT_MODEL, AgentSolver
from harness.solvers.base import Solver, SolverContext, SolverResult
from harness.solvers.builtin import CmdSolver, GoldSolver, NoopSolver
from harness.solvers.transport import (
    AnthropicTransport,
    CassetteWriter,
    ReplayTransport,
)

__all__ = [
    "DEFAULT_MAX_TURNS",
    "DEFAULT_MODEL",
    "AgentSolver",
    "CmdSolver",
    "GoldSolver",
    "NoopSolver",
    "Solver",
    "SolverContext",
    "SolverResult",
    "resolve_solver",
]


def resolve_solver(
    spec: str,
    *,
    model: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    max_cost_usd: float | None = None,
    cassette_dir: Path | None = None,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
    wall_clock_s: float | None = None,
) -> Solver:
    """Turn a `--solver` string into a solver.

    `gold`, `noop`, and `cmd:` need none of the keyword arguments; they exist
    for `agent` and `replay:<run_id>`, which are the only two that involve a
    model at all.
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

    if kind == "agent":
        transport = AnthropicTransport(
            cassettes=CassetteWriter(cassette_dir) if cassette_dir else None
        )
        return AgentSolver(
            transport,
            model=model or DEFAULT_MODEL,
            max_turns=max_turns,
            max_cost_usd=max_cost_usd,
            on_event=on_event,
            wall_clock_s=wall_clock_s,
        )

    if kind == "replay":
        run_id = argument.strip()
        if not run_id:
            raise UsageError(
                "--solver replay: needs a run id.",
                fix="Try --solver replay:<run_id>. List runs with `task runs`.",
            )
        directory = Path("runs") / run_id / "llm"
        transport = ReplayTransport(directory)
        return AgentSolver(
            # The recording fixes the model, so replay reports the model that
            # actually produced it rather than whatever --model says today.
            transport,
            model=model or _model_from_cassettes(directory),
            max_turns=max_turns,
            max_cost_usd=max_cost_usd,
            on_event=on_event,
        )

    raise UsageError(
        f"unknown solver {spec!r}.",
        fix="Available: gold, noop, agent, replay:<run_id>, cmd:<command>.",
    )


def _model_from_cassettes(directory: Path) -> str:
    """The model a recording was made with."""
    import json

    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text())
        model = (payload.get("request") or {}).get("model")
        if model:
            return str(model)
    return DEFAULT_MODEL
