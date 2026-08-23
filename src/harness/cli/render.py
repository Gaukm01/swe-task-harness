"""Terminal rendering helpers.

One rule: a failure is one line saying what broke, plus one line saying what to
do about it. Tracebacks appear only under `--debug`. Everything the user needs
to act on must survive being read in a hurry.
"""

from __future__ import annotations

from rich.console import Console
from rich.table import Table
from rich.text import Text

from harness.core.doctor import CheckStatus, DoctorReport
from harness.core.errors import HarnessError

# stderr for diagnostics, stdout for results -- so `task report --format json`
# stays pipeable once it lands.
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


def render_doctor(report: DoctorReport) -> None:
    """Render a preflight report as a status table plus actionable fixes."""
    table = Table(title="task doctor", title_justify="left", header_style="bold")
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

    console.print()
    if report.is_clean:
        warns = len(report.warnings)
        suffix = f" with {warns} warning{'s' if warns != 1 else ''}" if warns else ""
        console.print(Text(f"ready{suffix}.", style="bold green"))
    else:
        failed = ", ".join(c.name for c in report.failures)
        console.print(Text(f"not ready: {failed}", style="bold red"))
