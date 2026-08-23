"""Adapters wired behind the protocol but not implemented.

They exist so the shape of multi-language support is real rather than claimed,
and so `task lint` can warn honestly about a bundle that names one. Their smoke
probes work; running and parsing tests does not, and DESIGN.md says so.
"""

from __future__ import annotations


class GoAdapter:
    """Implements `TestAdapter` for `go test -json`. Smoke probe only."""

    framework = "go"

    def smoke_argv(self) -> list[str]:
        return ["go", "version"]


class JestAdapter:
    """Implements `TestAdapter` for jest `--json`. Smoke probe only."""

    framework = "jest"

    def smoke_argv(self) -> list[str]:
        return ["npx", "--no-install", "jest", "--version"]
