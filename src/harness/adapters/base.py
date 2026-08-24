"""The test-adapter protocol.

Every framework answers the same three questions: how do I prove you are
installed, how do I run this set of selectors, and what did that produce?

One rule is fixed for all of them: results come from a machine-readable
artifact (pytest's junitxml, jest's `--json`, `go test -json`), never from
stdout. A solver can print anything it likes, so stdout is not evidence.

The results file is *not* beyond reach, and it is important to say so
plainly. It is written inside the same container the solver's code runs in, so
an `atexit` hook in an ordinary source file can rewrite it after pytest
finishes and before the harness reads it. That produced a false `resolved` once.
Three things now stand in the way, none of them a proof:

1. the file lives in a per-run, dot-prefixed directory outside the repo and
   outside `/tmp`, so it is not found by a casual glob;
2. `PytestAdapter._verify_exit_agreement` compares the file's verdict against
   the process exit code, which the container cannot control -- a clean sweep
   alongside a non-zero exit is reported as `infra_error`;
3. `core.gaming` flags the code shapes such tampering needs.

An attacker who defeats all three still lands on `resolved_suspect`, never a
silent `resolved`. See DESIGN.md for the threat model.
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
