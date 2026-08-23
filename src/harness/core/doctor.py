"""Preflight check results and their aggregation.

Pure: this module knows what a check result looks like and how a list of them
maps to a process exit code. It performs no probing and imports nothing from
`runtime` -- the probes live in `runtime.probe`, which is what keeps this
logic unit-testable without Docker.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from harness.core.errors import ExitCode


class CheckStatus(StrEnum):
    """Outcome of a single preflight check.

    WARN never blocks. It marks a condition that will degrade a run -- slow
    emulation, tight disk, an absent API key -- but that still permits the
    zero-API-call development loop (`--solver gold` / `--solver noop`) to work.
    """

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


class Check(BaseModel):
    """One preflight check and what it found."""

    name: str
    status: CheckStatus
    detail: str
    fix: str | None = None
    # Exit code to use when this check is the reason the report is not clean.
    # Only meaningful for FAIL.
    exit_code: ExitCode = ExitCode.UNEXPECTED


class DoctorReport(BaseModel):
    """The full preflight result."""

    checks: list[Check] = Field(default_factory=list)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.status is CheckStatus.FAIL]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status is CheckStatus.WARN]

    @property
    def is_clean(self) -> bool:
        """Clean means nothing blocks a run. Warnings are still clean."""
        return not self.failures

    def exit_code(self) -> ExitCode:
        """The exit code for this report.

        The first failing check decides, so that a missing Docker daemon exits
        7 rather than being masked by a later unrelated failure. Checks are
        registered in dependency order for exactly this reason.
        """
        failures = self.failures
        if not failures:
            return ExitCode.OK
        return failures[0].exit_code
