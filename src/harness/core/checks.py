"""Check results and their aggregation, shared by `task doctor` and `task lint`.

Pure: this module knows what a check result looks like and how a list of them
maps to a process exit code. It performs no probing, touches no filesystem, and
imports nothing from `runtime` -- which is what keeps the aggregation logic
unit-testable with no Docker and no bundle on disk.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from harness.core.errors import ExitCode


class CheckStatus(StrEnum):
    """Outcome of a single check.

    WARN never blocks. It marks a condition that will degrade a run -- slow
    emulation, tight disk, an absent API key, a stubbed test framework -- but
    that still permits the zero-API-call development loop (`--solver gold` /
    `--solver noop`) to work.
    """

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


class Check(BaseModel):
    """One check and what it found."""

    name: str
    status: CheckStatus
    detail: str
    fix: str | None = None
    # Exit code to use when this check is the reason the report is not clean.
    # Only meaningful for FAIL.
    exit_code: ExitCode = ExitCode.UNEXPECTED


class CheckReport(BaseModel):
    """A list of checks plus the rule that turns them into an exit code."""

    checks: list[Check] = Field(default_factory=list)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.status is CheckStatus.FAIL]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status is CheckStatus.WARN]

    @property
    def is_clean(self) -> bool:
        """Clean means nothing blocks. Warnings are still clean."""
        return not self.failures

    def exit_code(self) -> ExitCode:
        """The exit code for this report.

        The first failing check decides, so that a root cause (a missing Docker
        daemon, an unparseable task.json) exits with its own code rather than
        being masked by a later check that failed for the same reason. Checks
        are registered in dependency order for exactly this reason.
        """
        failures = self.failures
        if not failures:
            return ExitCode.OK
        return failures[0].exit_code

    def add(self, check: Check) -> Check:
        """Append a check and return it, so callers can branch on its status."""
        self.checks.append(check)
        return check
