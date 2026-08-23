"""The solver protocol.

A solver gets a running container branched from BASE and makes changes to the
repo inside it. That is all it does. It does not report what it changed --
the harness computes `solution.diff` itself with `git add -A && git diff
--cached HEAD` after the solver returns (invariant 7). A model-emitted patch is
never trusted, and the codebase is never copied out to the host.

Solvers see: the repo, the problem description, and a shell. Solvers never see:
selector names, `test_patch.diff`, `patch.diff`, the bundle mount, git remotes,
git history past the synthetic base commit, or a network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from harness.core.bundle import Bundle
from harness.core.runtime import ContainerRuntime


@dataclass
class SolverContext:
    """Everything a solver is allowed to know."""

    runtime: ContainerRuntime
    container_id: str
    repo_root: str
    # description.md only. Never the patches, never the selectors.
    description: str
    timeout_s: int


@dataclass
class SolverResult:
    """What a solver reports about its own run.

    Deliberately not the diff. The harness computes that itself.
    """

    kind: str
    model: str | None = None
    turns: int = 0
    cost_usd: float = 0.0
    summary: str | None = None
    # Non-fatal notes worth surfacing: a ceiling hit, a refused tool call.
    notes: list[str] = field(default_factory=list)


class Solver(Protocol):
    """Something that attempts a task."""

    kind: str

    def solve(self, context: SolverContext, bundle: Bundle) -> SolverResult:
        """Modify the repo inside the container. Return metadata, not a patch."""
        ...
