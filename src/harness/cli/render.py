"""Terminal rendering helpers.

One rule: a failure is one line saying what broke, plus one line saying what to
do about it. Tracebacks appear only under `--debug`. Everything the user needs
to act on must survive being read in a hurry.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence

from rich.console import Console
from rich.table import Table
from rich.text import Text

from harness.core.checks import CheckReport, CheckStatus
from harness.core.errors import HarnessError

# stdout for results, stderr for diagnostics -- so `task report --format json`
# stays pipeable once it lands, and the invocation-id footer never corrupts it.
console = Console()
err_console = Console(stderr=True)

_STATUS_STYLE: dict[CheckStatus, tuple[str, str]] = {
    CheckStatus.OK: ("ok", "green"),
    CheckStatus.WARN: ("warn", "yellow"),
    CheckStatus.FAIL: ("FAIL", "bold red"),
}


def render_error(error: HarnessError, *, debug: bool = False) -> None:
    """Render a typed harness error: what broke, then what to do."""
    err_console.print(Text(f"error: {error.message}", style="bold red"))
    if error.fix:
        err_console.print(Text(f"  fix: {error.fix}", style="yellow"))
    err_console.print(
        Text(f"  exit code {int(error.exit_code)} ({error.exit_code.name.lower()})", style="dim")
    )
    if debug:
        err_console.print_exception(show_locals=False)


def render_unexpected(error: BaseException, *, debug: bool = False) -> None:
    """Render an error the harness did not anticipate."""
    err_console.print(Text(f"error: unexpected {type(error).__name__}: {error}", style="bold red"))
    if debug:
        err_console.print_exception(show_locals=False)
    else:
        err_console.print(
            Text("  fix: re-run with --debug for the full traceback.", style="yellow")
        )


def render_check_report(
    report: CheckReport,
    *,
    title: str,
    clean_message: str,
    footnote: str | None = None,
) -> None:
    """Render any list of checks as a status table plus actionable fixes."""
    table = Table(title=title, title_justify="left", header_style="bold")
    table.add_column("", width=4)
    table.add_column("check", style="bold")
    table.add_column("detail", overflow="fold")

    for check in report.checks:
        label, style = _STATUS_STYLE[check.status]
        table.add_row(Text(label, style=style), check.name, check.detail)

    console.print(table)

    actionable = [c for c in report.checks if c.status is not CheckStatus.OK and c.fix]
    if actionable:
        console.print()
        for check in actionable:
            _, style = _STATUS_STYLE[check.status]
            console.print(Text(f"{check.name}: ", style=style), end="")
            console.print(Text(check.fix or "", style="dim"))

    if footnote:
        console.print()
        # soft_wrap: a digest is one token. Wrapping it makes it unusable for
        # copy-paste and for anything grepping the output.
        console.print(Text(footnote, style="dim"), soft_wrap=True)

    console.print()
    if report.is_clean:
        warns = len(report.warnings)
        suffix = f" with {warns} warning{'s' if warns != 1 else ''}" if warns else ""
        console.print(Text(f"{clean_message}{suffix}.", style="bold green"))
    else:
        failed = ", ".join(c.name for c in report.failures)
        console.print(Text(f"not ok: {failed}", style="bold red"))


def render_invocation(record: sqlite3.Row, events: Sequence[sqlite3.Row] = ()) -> None:
    """Render one stored invocation row and any events it logged."""
    argv = json.loads(record["argv"])
    exit_code = record["exit_code"]

    if record["ended_at"] is None:
        # A row with no end is the signature of a process that died rather than exited.
        status = Text("did not finish", style="bold red")
    elif exit_code == 0:
        status = Text("exit 0", style="green")
    else:
        status = Text(f"exit {exit_code}", style="bold red")

    table = Table(title=f"invocation {record['id']}", title_justify="left", show_header=False)
    table.add_column("field", style="bold", no_wrap=True)
    table.add_column("value", overflow="fold")
    table.add_row("command", " ".join(argv))
    table.add_row("cwd", record["cwd"])
    table.add_row("harness", record["harness_version"])
    table.add_row("started", record["started_at"])
    table.add_row("ended", record["ended_at"] or "-")
    table.add_row("status", status)
    console.print(table)

    if not events:
        return
    console.print()
    event_table = Table(title="events", title_justify="left", header_style="bold")
    event_table.add_column("seq", justify="right")
    event_table.add_column("at")
    event_table.add_column("kind", style="bold")
    event_table.add_column("payload", overflow="fold")
    for event in events:
        event_table.add_row(str(event["seq"]), event["at"], event["kind"], event["payload"])
    console.print(event_table)
