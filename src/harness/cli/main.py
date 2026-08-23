"""The `task` CLI: command surface, invocation logging, and the error boundary.

`main()` opens the store and writes the invocation row *before* Typer parses
anything, then closes it in a `finally`. That ordering is the point: a usage
error, an unhandled exception, and a crash all still leave a row describing
what was attempted. Doing it inside a Typer callback would miss every failure
that happens during argument parsing.

Implemented at M2: `doctor`, `lint`, `log`. The remaining commands are
registered with their final argument contracts and report the milestone that
lands them.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

import typer

from harness import __version__
from harness.cli.render import (
    console,
    err_console,
    render_check_report,
    render_error,
    render_invocation,
    render_unexpected,
)
from harness.core.bundle import TaskSpec, compute_bundle_digest, lint_bundle, load_bundle
from harness.core.errors import ExitCode, HarnessError, UsageError
from harness.runtime.probe import collect_doctor_report
from harness.store.db import DEFAULT_DB_FILENAME, Store

LAST = "last"


@dataclass
class CliState:
    """Process-wide state established by `main()` before Typer runs."""

    debug: bool = False
    db_path: Path = field(default_factory=lambda: Path(DEFAULT_DB_FILENAME))
    store: Store | None = None
    invocation_id: str | None = None

    def require_store(self) -> Store:
        """The store is opened in `main()`; commands may assume it exists."""
        if self.store is None:  # pragma: no cover - only reachable via direct app() calls
            self.store = Store(self.db_path)
        return self.store


state = CliState()

app = typer.Typer(
    name="task",
    help="Package SWE-bench-style coding tasks into containers, validate, solve, and grade them.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,  # the error boundary in main() owns exception rendering
)

BundleArg = Annotated[Path, typer.Argument(help="Path to a task bundle directory.")]


def _pending(command: str, milestone: str) -> None:
    """Registered-but-unimplemented command: fail honestly, name the milestone."""
    raise HarnessError(
        f"`task {command}` is not implemented yet.",
        fix=f"It lands in milestone {milestone}. Live so far: doctor, lint, log.",
    )


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    debug: Annotated[
        bool, typer.Option("--debug", help="Show full tracebacks instead of one-line errors.")
    ] = False,
    db: Annotated[Path | None, typer.Option("--db", help="Path to the harness database.")] = None,
    version: Annotated[
        bool, typer.Option("--version", help="Print the harness version and exit.")
    ] = False,
) -> None:
    """Root options shared by every command."""
    state.debug = debug
    if db is not None:
        state.db_path = db
    if version:
        console.print(f"swe-task-harness {__version__}")
        raise typer.Exit(ExitCode.OK)
    if ctx.invoked_subcommand is None:
        # Root options alone are not an invocation; show the surface and exit 2.
        console.print(ctx.get_help())
        raise typer.Exit(ExitCode.USAGE)


@app.command()
def doctor() -> None:
    """Check docker, disk, architecture, and credentials before anything else runs."""
    report = collect_doctor_report()
    render_check_report(report, title="task doctor", clean_message="ready")
    raise typer.Exit(int(report.exit_code()))


@app.command()
def lint(bundle: BundleArg) -> None:
    """Validate a bundle's schema and structure without touching docker."""
    report = lint_bundle(bundle)
    digest = compute_bundle_digest(bundle) if bundle.is_dir() else None
    render_check_report(
        report,
        title=f"task lint {bundle}",
        clean_message="bundle valid",
        footnote=f"bundle_digest sha256:{digest}" if digest else None,
    )

    if report.is_clean:
        # Registering the task here means `task log`/`task runs` have something to
        # join against before any container has been built.
        spec = _spec_or_none(bundle)
        if spec is not None:
            state.require_store().upsert_task(
                task_id=spec.task_id,
                bundle_path=str(bundle.resolve()),
                bundle_digest=digest or "",
                language=spec.language,
                framework=spec.tests.framework,
            )
    raise typer.Exit(int(report.exit_code()))


def _spec_or_none(bundle: Path) -> TaskSpec | None:
    """Re-read the spec after a clean lint. Cheap, and keeps lint's happy path linear."""
    try:
        return load_bundle(bundle).spec
    except HarnessError:  # pragma: no cover - a clean report means this cannot fail
        return None


@app.command("log")
def show_log(
    identifier: Annotated[
        str, typer.Argument(help="An invocation id, a run id, or 'last'.")
    ] = LAST,
) -> None:
    """Show the stored invocation or run, with its logs."""
    store = state.require_store()

    if identifier == LAST:
        # Skip this very invocation, which is by definition the newest row.
        record = store.latest_invocation(before=state.invocation_id)
        if record is None:
            raise UsageError(
                "No previous invocation recorded.",
                fix=f"Run any command first; every call writes a row to {state.db_path}.",
            )
    else:
        record = store.get_invocation(identifier)
        if record is None:
            if store.get_run(identifier) is not None:
                _pending("log <run_id>", "M5")
            raise UsageError(
                f"No invocation or run with id {identifier!r}.",
                fix="Use `task log last`, or copy an id from a previous command's output.",
            )

    events = store.events_for_invocation(record["id"])
    render_invocation(record, events)


@app.command()
def init(
    bundle: BundleArg,
    no_cache: Annotated[
        bool, typer.Option("--no-cache", help="Ignore any cached environment snapshot.")
    ] = False,
) -> None:
    """Build the task environment and snapshot it as the BASE phase."""
    _pending("init", "M3")


@app.command()
def validate(
    bundle: BundleArg,
    repeat: Annotated[
        int, typer.Option("--repeat", help="Run the guardrail assertions N times.")
    ] = 1,
) -> None:
    """Assert the GUARDED and GOLD phases hold: p2p pass, f2p fail then pass."""
    _pending("validate", "M4")


@app.command()
def run(
    bundle: BundleArg,
    solver: Annotated[
        str, typer.Option("--solver", help="gold | noop | agent | replay:<run_id> | cmd:<command>")
    ],
    model: Annotated[str | None, typer.Option("--model", help="Model for --solver agent.")] = None,
    max_turns: Annotated[int, typer.Option("--max-turns", help="Agent turn ceiling.")] = 75,
    max_cost_usd: Annotated[
        float | None, typer.Option("--max-cost-usd", help="Agent spend ceiling.")
    ] = None,
) -> None:
    """Validate the baseline, run a solver, grade the result, and write a report."""
    _pending("run", "M5")


@app.command()
def runs(
    task_id: Annotated[str | None, typer.Option("--task", help="Filter by task id.")] = None,
    outcome: Annotated[str | None, typer.Option("--outcome", help="Filter by run outcome.")] = None,
) -> None:
    """List recorded runs."""
    _pending("runs", "M8")


@app.command()
def report(
    run_id: Annotated[str, typer.Argument(help="The run to regenerate artifacts for.")],
    fmt: Annotated[str, typer.Option("--format", help="json | html")] = "json",
) -> None:
    """Regenerate the report artifacts for a recorded run."""
    _pending("report", "M5")


@app.command()
def ui(
    out: Annotated[Path | None, typer.Option("--out", help="Directory to write HTML into.")] = None,
) -> None:
    """Regenerate the static HTML index and run pages from the database."""
    _pending("ui", "M8")


@app.command()
def shell(
    run_id: Annotated[str, typer.Argument(help="The run whose snapshot to enter.")],
    phase: Annotated[str, typer.Option("--phase", help="base | guarded | gold | solve | scored")],
) -> None:
    """Open a shell inside a recorded phase snapshot."""
    _pending("shell", "M8")


@app.command("show-tests")
def show_tests(bundle: BundleArg) -> None:
    """Render a bundle's test patch and selector lists. Authoring aid."""
    _pending("show-tests", "M4")


@app.command("import")
def import_instance(
    instance_id: Annotated[str, typer.Option("--instance-id", help="SWE-Bench Pro instance id.")],
    out: Annotated[
        Path | None, typer.Option("--out", help="Directory to write the bundle to.")
    ] = None,
) -> None:
    """Convert a SWE-Bench Pro instance into a task bundle."""
    _pending("import", "M7")


@app.command()
def gc(
    days: Annotated[int, typer.Option("--days", help="Remove snapshots older than N days.")] = 7,
) -> None:
    """Remove old phase snapshots."""
    _pending("gc", "M8")


def _peek_db_path(argv: list[str]) -> Path:
    """Resolve --db before Typer parses anything.

    The invocation row has to be written before argument parsing, so that a
    usage error still gets recorded -- which means this one flag must be read
    by hand. Typer still declares `--db` so it appears in `--help` and is
    validated normally; both readers see the same argv and agree.
    """
    for index, token in enumerate(argv):
        if token == "--db" and index + 1 < len(argv):
            return Path(argv[index + 1])
        if token.startswith("--db="):
            return Path(token.split("=", 1)[1])
    return Path(os.environ.get("HARNESS_DB", DEFAULT_DB_FILENAME))


def _normalize_exit(code: object) -> int:
    """click exits with None for success and occasionally with a string."""
    if code is None:
        return int(ExitCode.OK)
    if isinstance(code, int):
        return code
    return int(ExitCode.UNEXPECTED)


def main() -> None:
    """Entry point. Owns invocation logging and the error boundary."""
    argv = sys.argv[1:]
    state.db_path = _peek_db_path(argv)

    store: Store | None = None
    invocation_id: str | None = None
    try:
        store = Store(state.db_path)
        state.store = store
        invocation_id = store.begin_invocation(argv=sys.argv, cwd=os.getcwd())
        state.invocation_id = invocation_id
    except Exception as error:  # noqa: BLE001 - an unusable store must not hide the command
        err_console.print(f"[yellow]warning:[/] could not open {state.db_path}: {error}")

    exit_code = int(ExitCode.OK)
    try:
        app()
    except SystemExit as sysexit:
        exit_code = _normalize_exit(sysexit.code)
    except HarnessError as error:
        render_error(error, debug=state.debug)
        exit_code = int(error.exit_code)
    except KeyboardInterrupt:
        err_console.print("[yellow]interrupted[/]")
        exit_code = int(ExitCode.UNEXPECTED)
    except Exception as error:  # noqa: BLE001 - the boundary is meant to be broad
        render_unexpected(error, debug=state.debug)
        exit_code = int(ExitCode.UNEXPECTED)
    finally:
        if store is not None and invocation_id is not None:
            try:
                store.end_invocation(invocation_id, exit_code)
                err_console.print(f"[dim]invocation {invocation_id}[/]")
            except Exception:  # noqa: BLE001 - never let bookkeeping change the exit code
                pass
            store.close()

    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
