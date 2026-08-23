"""CLI boundary: the surface, the exit codes, and invocation logging."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from harness import __version__
from harness.cli.main import app
from harness.core.bundle import compute_bundle_digest
from harness.core.errors import ExitCode, HarnessError
from harness.store.db import Store

runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "examples" / "tiny-fixture"


def run_cli(*args: str, db: Path) -> subprocess.CompletedProcess[str]:
    """Invoke the real entry point in a subprocess.

    Invocation logging lives in `main()`, outside Typer, so that a usage error
    still gets a row. Only a real process exercises that path.
    """
    return subprocess.run(
        [sys.executable, "-m", "harness.cli.main", "--db", str(db), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )


# -- surface --------------------------------------------------------------


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
    assert runner.invoke(app, ["nope"]).exit_code == ExitCode.USAGE


def test_pending_command_raises_a_typed_error():
    result = runner.invoke(app, ["report", "01SOMERUN"])
    assert isinstance(result.exception, HarnessError)
    assert "M5" in (result.exception.fix or "")


# -- lint -----------------------------------------------------------------


def test_lint_on_the_tiny_fixture_exits_zero(tmp_path):
    result = run_cli("lint", str(FIXTURE), db=tmp_path / "h.db")
    assert result.returncode == ExitCode.OK, result.stdout + result.stderr
    assert "bundle valid" in result.stdout
    # The digest must survive rendering unwrapped, or it cannot be copied out.
    assert compute_bundle_digest(FIXTURE) in result.stdout


def test_lint_registers_the_task(tmp_path):
    db = tmp_path / "h.db"
    run_cli("lint", str(FIXTURE), db=db)
    with Store(db) as store:
        row = store.get_task("tiny-fixture")
    assert row is not None
    assert row["language"] == "python"
    assert row["framework"] == "pytest"


def test_lint_on_a_broken_bundle_exits_three(tmp_path, bundle_copy):
    (bundle_copy / "task.json").write_text("{ broken")
    result = run_cli("lint", str(bundle_copy), db=tmp_path / "h.db")
    assert result.returncode == ExitCode.BUNDLE_INVALID


def test_a_failed_lint_does_not_register_the_task(tmp_path, bundle_copy):
    db = tmp_path / "h.db"
    (bundle_copy / "description.md").write_text("")
    run_cli("lint", str(bundle_copy), db=db)
    with Store(db) as store:
        assert store.get_task("tiny-fixture") is None


# -- invocation logging ---------------------------------------------------


def test_every_call_writes_an_invocation_row(tmp_path):
    db = tmp_path / "h.db"
    result = run_cli("lint", str(FIXTURE), db=db)
    with Store(db) as store:
        rows = store.recent_invocations()
    assert len(rows) == 1
    assert rows[0]["exit_code"] == 0
    assert rows[0]["ended_at"] is not None
    # The id is echoed so a user can pass it to `task log`.
    assert rows[0]["id"] in result.stderr


def test_a_usage_error_is_still_recorded(tmp_path):
    # The row is written before Typer parses anything, precisely so this works.
    db = tmp_path / "h.db"
    result = run_cli("nope", db=db)
    assert result.returncode == ExitCode.USAGE
    with Store(db) as store:
        rows = store.recent_invocations()
    assert len(rows) == 1
    assert rows[0]["exit_code"] == ExitCode.USAGE


def test_a_failing_command_records_its_exit_code(tmp_path, bundle_copy):
    db = tmp_path / "h.db"
    (bundle_copy / "task.json").write_text("{ broken")
    run_cli("lint", str(bundle_copy), db=db)
    with Store(db) as store:
        assert store.recent_invocations()[0]["exit_code"] == ExitCode.BUNDLE_INVALID


# -- log ------------------------------------------------------------------


def test_log_returns_a_row_for_a_command_that_did_nothing(tmp_path):
    """The M2 acceptance check."""
    db = tmp_path / "h.db"
    first = run_cli("--version", db=db)
    invocation_id = _last_id(db)

    result = run_cli("log", invocation_id, db=db)
    assert result.returncode == ExitCode.OK, result.stdout + result.stderr
    assert invocation_id in result.stdout
    assert "--version" in result.stdout
    assert "exit 0" in result.stdout
    assert first.returncode == ExitCode.OK


def test_log_last_skips_its_own_invocation(tmp_path):
    db = tmp_path / "h.db"
    run_cli("doctor", db=db)
    result = run_cli("log", "last", db=db)
    assert result.returncode == ExitCode.OK
    assert "doctor" in result.stdout
    assert "log" not in result.stdout.split("command")[1].split("\n")[0]


def test_log_on_an_unknown_id_is_a_usage_error(tmp_path):
    result = run_cli("log", "NOSUCHID", db=tmp_path / "h.db")
    assert result.returncode == ExitCode.USAGE


def test_log_with_no_prior_invocation_is_a_usage_error(tmp_path):
    assert run_cli("log", "last", db=tmp_path / "h.db").returncode == ExitCode.USAGE


def _last_id(db: Path) -> str:
    with Store(db) as store:
        return store.recent_invocations()[0]["id"]
