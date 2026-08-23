"""The test-adapter protocol.

Each supported framework answers the same questions: how do I invoke you, and
what did the run produce? Only the smoke probe is needed at M3, which is why
that is all this protocol declares so far -- running tests and parsing
structured output land in M4, when there is a grading path to validate the
shape against.

One rule is already fixed and will not move: adapters return structured
results parsed from a machine-readable artifact (pytest's junitxml, `go test
-json`, jest's `--json`). Nothing is ever scraped from stdout.
"""

from __future__ import annotations

from typing import Protocol


class TestAdapter(Protocol):
    """What a framework adapter must provide."""

    framework: str

    def smoke_argv(self) -> list[str]:
        """A cheap command proving the runner is installed and executable."""
        ...
