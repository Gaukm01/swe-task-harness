"""Check aggregation: pure logic, no docker, no bundle."""

from __future__ import annotations

from harness.core.checks import Check, CheckReport, CheckStatus
from harness.core.errors import ExitCode


def _check(name, status, exit_code=ExitCode.UNEXPECTED):
    return Check(name=name, status=status, detail="", exit_code=exit_code)


def test_all_ok_is_clean_and_exits_zero():
    report = CheckReport(checks=[_check("a", CheckStatus.OK), _check("b", CheckStatus.OK)])
    assert report.is_clean
    assert report.exit_code() is ExitCode.OK


def test_warnings_do_not_block():
    # A warning means degraded, not broken: emulation, a missing API key, and a
    # stubbed framework must all still let the gold/noop loop run.
    report = CheckReport(checks=[_check("arch", CheckStatus.WARN), _check("ok", CheckStatus.OK)])
    assert report.is_clean
    assert report.exit_code() is ExitCode.OK
    assert [c.name for c in report.warnings] == ["arch"]


def test_first_failure_decides_the_exit_code():
    # Checks are registered in dependency order, so a root cause must not be
    # masked by the downstream failure it caused.
    report = CheckReport(
        checks=[
            _check("docker cli", CheckStatus.FAIL, ExitCode.DOCKER_UNAVAILABLE),
            _check("python", CheckStatus.FAIL, ExitCode.UNEXPECTED),
        ]
    )
    assert not report.is_clean
    assert report.exit_code() is ExitCode.DOCKER_UNAVAILABLE
    assert len(report.failures) == 2


def test_add_returns_the_check():
    report = CheckReport()
    added = report.add(_check("x", CheckStatus.OK))
    assert added.name == "x"
    assert report.checks == [added]
