"""The test-adapter protocol.

Every framework answers the same three questions: how do I prove you are
installed, how do I run this set of selectors, and what did that produce?

One rule is fixed for all of them: results come from a machine-readable
artifact (pytest's junitxml, jest's `--json`, `go test -json`), never from
stdout. A solver can print anything it likes; it cannot forge a file the
harness writes to a path of its own choosing and reads back itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from harness.core.results import Bucket, TestOutcome
from harness.core.runtime import ExecResult


class TestAdapter(Protocol):
    """What a framework adapter must provide."""

    framework: str

    def smoke_argv(self) -> list[str]:
        """A cheap command proving the runner is installed and executable."""
        ...

    def run_argv(self, template: str, selectors: list[str], out_path: str) -> list[str]:
        """Build the argv for a test run. Never a shell string."""
        ...

    def parse(
        self,
        *,
        junit_path: Path | None,
        exec_result: ExecResult,
        requested: dict[str, Bucket],
    ) -> list[TestOutcome]:
        """Turn a finished run into one outcome per requested selector.

        Every requested selector must appear in the returned list. A selector
        with no corresponding result is `not_found`, never dropped -- silently
        shrinking the denominator would let a deleted test look like a pass.
        """
        ...
