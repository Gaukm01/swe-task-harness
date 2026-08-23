"""Typed exceptions and the exit-code taxonomy.

Every failure mode the CLI can exit with has exactly one exception class and
exactly one exit code. The CLI boundary (`cli.main`) catches `HarnessError`,
renders one line plus a concrete suggested fix, and exits with `err.exit_code`.
Raw tracebacks are shown only under `--debug`.

Exit codes are part of the tool's contract -- scripts and CI depend on them, so
they must never be renumbered or reused.
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    """Process exit codes. Documented in README; do not renumber."""

    OK = 0
    UNEXPECTED = 1
    USAGE = 2
    BUNDLE_INVALID = 3
    BASELINE_FAILED = 4
    SOLVER_FAILED = 5
    GRADING_INCONCLUSIVE = 6
    DOCKER_UNAVAILABLE = 7


class HarnessError(Exception):
    """Base for every expected harness failure.

    `message` is the one-line problem statement. `fix` is a concrete next
    action for the user -- a command to run or a field to correct -- not a
    restatement of the problem. Subclasses pin `exit_code`.
    """

    exit_code: ExitCode = ExitCode.UNEXPECTED

    def __init__(self, message: str, fix: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.fix = fix


class UsageError(HarnessError):
    """Bad invocation: missing/conflicting flags, unreadable path, unknown solver."""

    exit_code = ExitCode.USAGE


class BundleInvalidError(HarnessError):
    """A task bundle failed schema or structural validation."""

    exit_code = ExitCode.BUNDLE_INVALID


class BaselineValidationError(HarnessError):
    """GUARDED/GOLD assertions did not hold, so no run may proceed.

    Raised by the validate lane and by `task run`'s in-orchestrator baseline
    re-verification. A task whose baseline does not hold cannot grade anything
    meaningfully, so this aborts before a solver starts.
    """

    exit_code = ExitCode.BASELINE_FAILED


class SolverFailedError(HarnessError):
    """The solver itself errored out (not: the solver produced a wrong patch).

    A solver that runs to completion and fails the tests is a normal
    `unresolved` outcome, not this.
    """

    exit_code = ExitCode.SOLVER_FAILED


class GradingInconclusiveError(HarnessError):
    """Grading could not produce a verdict because the infrastructure failed.

    Timeouts, OOM kills, a missing junit file. Never reported as the solution
    failing -- that distinction is invariant 5.
    """

    exit_code = ExitCode.GRADING_INCONCLUSIVE


class DockerUnavailableError(HarnessError):
    """The container runtime is missing, not running, or not responding."""

    exit_code = ExitCode.DOCKER_UNAVAILABLE
