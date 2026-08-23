"""The `task` CLI: command surface and the error boundary.

Only `doctor` is implemented at M1. The remaining commands are registered so
`task --help` shows the full designed surface and so the argument contracts are
fixed before the milestones that fill them in; each one exits with a message
naming the milestone that lands it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from harness import __version__
from harness.cli.render import console, render_doctor, render_error, render_unexpected
from harness.core.errors import ExitCode, HarnessError
from harness.runtime.probe import collect_doctor_report


@dataclass
class CliState:
    """Flags from the root callback that commands and the error boundary need."""

    debug: bool = False


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
        fix=f"It lands in milestone {milestone}. `task doctor` is the only command live at M1.",
    )


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    debug: Annotated[
        bool, typer.Option("--debug", help="Show full tracebacks instead of one-line errors.")
    ] = False,
    version: Annotated[
        bool, typer.Option("--version", help="Print the harness version and exit.")
    ] = False,
) -> None:
    """Root options shared by every command."""
    state.debug = debug
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
    render_doctor(report)
    raise typer.Exit(int(report.exit_code()))


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
def lint(bundle: BundleArg) -> None:
    """Validate a bundle's schema and structure without touching docker."""
    _pending("lint", "M2")


@app.command("log")
def show_log(
    identifier: Annotated[str, typer.Argument(help="An invocation id or run id.")],
) -> None:
    """Show the stored invocation or run, with its logs."""
    _pending("log", "M2")


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


def main() -> None:
    """Entry point. Owns the error boundary so every failure maps to its exit code."""
    try:
        app()
    except HarnessError as error:
        render_error(error, debug=state.debug)
        raise SystemExit(int(error.exit_code)) from error
    except KeyboardInterrupt:
        raise SystemExit(int(ExitCode.UNEXPECTED)) from None
    except Exception as error:  # noqa: BLE001 - the boundary is meant to be broad
        render_unexpected(error, debug=state.debug)
        raise SystemExit(int(ExitCode.UNEXPECTED)) from error


if __name__ == "__main__":
    main()
