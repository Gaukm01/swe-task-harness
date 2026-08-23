"""CLI boundary: the surface exists and unimplemented commands fail honestly."""

from __future__ import annotations

from typer.testing import CliRunner

from harness import __version__
from harness.cli.main import app
from harness.core.errors import ExitCode

runner = CliRunner()


def test_version_flag():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == ExitCode.OK
    assert __version__ in result.stdout


def test_help_lists_the_designed_surface():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == ExitCode.OK
    for command in ("doctor", "init", "validate", "run", "lint", "runs", "report", "ui", "shell"):
        assert command in result.stdout


def test_unknown_command_is_a_usage_error():
    result = runner.invoke(app, ["nope"])
    assert result.exit_code == ExitCode.USAGE


def test_pending_command_raises_a_typed_error():
    # Typer's runner surfaces the exception rather than the exit code; main()'s
    # boundary is what maps it. Assert the contract at the point it is defined.
    from harness.core.errors import HarnessError

    result = runner.invoke(app, ["lint", "examples/tiny-fixture"])
    assert isinstance(result.exception, HarnessError)
    assert "M2" in (result.exception.fix or "")
