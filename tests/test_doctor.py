"""Doctor aggregation: pure logic, no docker."""

from __future__ import annotations

from harness.core.doctor import Check, CheckStatus, DoctorReport
from harness.core.errors import ExitCode


def _check(name: str, status: CheckStatus, exit_code: ExitCode = ExitCode.UNEXPECTED) -> Check:
    return Check(name=name, status=status, detail="", exit_code=exit_code)


def test_all_ok_is_clean_and_exits_zero():
    report = DoctorReport(checks=[_check("a", CheckStatus.OK), _check("b", CheckStatus.OK)])
    assert report.is_clean
    assert report.exit_code() is ExitCode.OK


def test_warnings_do_not_block():
    # A warning means degraded, not broken: emulation and a missing API key must
    # still let the zero-API-call gold/noop loop run.
    report = DoctorReport(checks=[_check("arch", CheckStatus.WARN), _check("ok", CheckStatus.OK)])
    assert report.is_clean
    assert report.exit_code() is ExitCode.OK
    assert [c.name for c in report.warnings] == ["arch"]


def test_first_failure_decides_the_exit_code():
    # Checks are registered in dependency order, so a missing docker CLI must not
    # be masked by the downstream failure it caused.
    report = DoctorReport(
        checks=[
            _check("docker cli", CheckStatus.FAIL, ExitCode.DOCKER_UNAVAILABLE),
            _check("python", CheckStatus.FAIL, ExitCode.UNEXPECTED),
        ]
    )
    assert not report.is_clean
    assert report.exit_code() is ExitCode.DOCKER_UNAVAILABLE
    assert len(report.failures) == 2
