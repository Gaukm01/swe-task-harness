"""Adapters wired behind the protocol but not implemented.

They exist so the shape of multi-language support is real rather than claimed,
and so `task lint` can warn honestly about a bundle that names one. Their smoke
probes work and their argv construction is shared; parsing is not written, and
DESIGN.md says so plainly rather than implying broader support than exists.
"""

from __future__ import annotations

from pathlib import Path

from harness.adapters.pytest_adapter import PytestAdapter
from harness.core.results import Bucket, TestOutcome
from harness.core.runtime import ExecResult

# Template expansion is framework-independent, so the stubs borrow it rather
# than duplicating a subtly different copy.
_expand = PytestAdapter().run_argv


class _StubAdapter:
    framework = "stub"
    smoke: list[str] = []

    def smoke_argv(self) -> list[str]:
        return self.smoke

    def run_argv(self, template: str, selectors: list[str], out_path: str) -> list[str]:
        return _expand(template, selectors, out_path)

    def parse(
        self,
        *,
        junit_path: Path | None,
        exec_result: ExecResult,
        requested: dict[str, Bucket],
    ) -> list[TestOutcome]:
        raise NotImplementedError(
            f"the {self.framework} adapter cannot parse results yet. "
            "Only pytest is fully implemented; see DESIGN.md."
        )


class GoAdapter(_StubAdapter):
    """`go test -json`. Smoke probe and argv only."""

    framework = "go"
    smoke = ["go", "version"]


class JestAdapter(_StubAdapter):
    """jest `--json`. Smoke probe and argv only."""

    framework = "jest"
    smoke = ["npx", "--no-install", "jest", "--version"]
