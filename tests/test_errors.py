"""The exit-code taxonomy is a contract; these tests pin it."""

from __future__ import annotations

import pytest

from harness.core.errors import (
    BaselineValidationError,
    BundleInvalidError,
    DockerUnavailableError,
    ExitCode,
    GradingInconclusiveError,
    HarnessError,
    SolverFailedError,
    UsageError,
)


def test_exit_codes_are_stable():
    # Renumbering these breaks every caller's CI. Locked.
    assert ExitCode.OK == 0
    assert ExitCode.UNEXPECTED == 1
    assert ExitCode.USAGE == 2
    assert ExitCode.BUNDLE_INVALID == 3
    assert ExitCode.BASELINE_FAILED == 4
    assert ExitCode.SOLVER_FAILED == 5
    assert ExitCode.GRADING_INCONCLUSIVE == 6
    assert ExitCode.DOCKER_UNAVAILABLE == 7


@pytest.mark.parametrize(
    ("error_cls", "expected"),
    [
        (UsageError, ExitCode.USAGE),
        (BundleInvalidError, ExitCode.BUNDLE_INVALID),
        (BaselineValidationError, ExitCode.BASELINE_FAILED),
        (SolverFailedError, ExitCode.SOLVER_FAILED),
        (GradingInconclusiveError, ExitCode.GRADING_INCONCLUSIVE),
        (DockerUnavailableError, ExitCode.DOCKER_UNAVAILABLE),
    ],
)
def test_each_error_pins_its_exit_code(error_cls, expected):
    assert error_cls("boom").exit_code is expected


def test_base_error_carries_message_and_fix():
    error = HarnessError("it broke", fix="unbreak it")
    assert error.exit_code is ExitCode.UNEXPECTED
    assert error.message == "it broke"
    assert error.fix == "unbreak it"
    assert str(error) == "it broke"
