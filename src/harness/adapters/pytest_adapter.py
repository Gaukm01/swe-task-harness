"""The pytest adapter.

The only fully implemented adapter. M4 adds test execution and junitxml
parsing; M3 needs only the smoke probe that BASE asserts against.
"""

from __future__ import annotations


class PytestAdapter:
    """Implements `TestAdapter` for pytest."""

    framework = "pytest"

    def smoke_argv(self) -> list[str]:
        # `--version` runs the plugin machinery, so it fails loudly when the
        # environment has pytest installed but broken -- which a bare
        # `which pytest` would call healthy.
        return ["python", "-m", "pytest", "--version"]
