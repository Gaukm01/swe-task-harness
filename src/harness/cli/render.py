"""Terminal rendering helpers.

One rule: a failure is one line saying what broke, plus one line saying what to
do about it. Tracebacks appear only under `--debug`. Everything the user needs
to act on must survive being read in a hurry.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from harness.core.bundle import Bundle
from harness.core.cache import CACHE_KEY_TAG_LEN
from harness.core.checks import CheckReport, CheckStatus
from harness.core.errors import HarnessError
from harness.core.phases import BaseResult, PhaseAssertion, ValidationResult
from harness.core.results import Bucket, TestStatus

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


def render_base_result(result: BaseResult, *, task_id: str, bundle_digest: str) -> None:
    """Render the outcome of `task init`."""
    table = Table(title=f"task init {task_id}", title_justify="left", show_header=False)
    table.add_column("field", style="bold", no_wrap=True)
    table.add_column("value", overflow="fold")
    table.add_row("phase", "BASE")
    table.add_row("image", result.image)
    table.add_row("repo root", result.repo_root)
    short_key = result.cache_key[:CACHE_KEY_TAG_LEN]
    table.add_row("cache key", f"{short_key}… (the image tag uses this prefix)")
    if result.base_commit_sha:
        table.add_row("base commit", f"{result.base_commit_sha} (synthetic root)")
    table.add_row(
        "source", Text("cached snapshot", style="cyan") if result.cached else "built just now"
    )
    console.print(table)

    if result.steps:
        console.print()
        step_table = Table(title="steps", title_justify="left", header_style="bold")
        step_table.add_column("", width=4)
        step_table.add_column("step", style="bold")
        step_table.add_column("ms", justify="right")
        for step in result.steps:
            if step.ok:
                status = Text("ok", style="green")
            elif step.tolerated:
                # Expected and harmless: an image whose repo never had a remote.
                status = Text("n/a", style="dim")
            else:
                status = Text("fail", style="bold red")
            step_table.add_row(status, step.label, str(step.duration_ms))
        console.print(step_table)

    console.print()
    console.print(Text(f"bundle_digest sha256:{bundle_digest}", style="dim"), soft_wrap=True)
    console.print()
    verb = "reused" if result.cached else "built"
    console.print(Text(f"BASE {verb} in {result.duration_ms}ms.", style="bold green"))


_STATUS_COLOUR: dict[TestStatus, str] = {
    TestStatus.PASSED: "green",
    TestStatus.FAILED: "red",
    TestStatus.ERROR: "red",
    TestStatus.COLLECTION_ERROR: "bold red",
    TestStatus.NOT_FOUND: "bold magenta",
    TestStatus.SKIPPED: "yellow",
    TestStatus.TIMEOUT: "bold yellow",
    TestStatus.INFRA_ERROR: "bold yellow",
}


def _status_text(status: TestStatus) -> Text:
    return Text(status.value, style=_STATUS_COLOUR.get(status, "white"))


def _outcome_table(assertion: PhaseAssertion, *, expectation: str) -> Table:
    table = Table(
        title=f"{assertion.phase.value.upper()} — {expectation}",
        title_justify="left",
        header_style="bold",
    )
    table.add_column("bucket", width=6)
    table.add_column("test")
    table.add_column("status")
    table.add_column("ms", justify="right")
    for outcome in assertion.outcomes:
        table.add_row(
            Text(outcome.bucket.value, style="cyan" if outcome.bucket is Bucket.F2P else "blue"),
            outcome.test_id,
            _status_text(outcome.status),
            str(outcome.duration_ms),
        )
    return table


def render_validation(result: ValidationResult, *, task_id: str, artifact_dir: Path) -> None:
    """Render the validate lane: what each phase asserted and whether it held."""
    header = Table(title=f"task validate {task_id}", title_justify="left", show_header=False)
    header.add_column("field", style="bold", no_wrap=True)
    header.add_column("value", overflow="fold")
    header.add_row("validation", result.validation_id)
    header.add_row("base image", result.base.image)
    for assertion in (result.guarded, result.gold):
        label = f"{assertion.phase.value} image"
        if assertion.retained:
            header.add_row(label, assertion.image)
        else:
            header.add_row(label, Text("discarded (validation passed)", style="dim"))
    header.add_row("artifacts", str(artifact_dir))
    console.print(header)

    console.print()
    console.print(_outcome_table(result.guarded, expectation="every p2p passes, every f2p fails"))
    console.print()
    console.print(_outcome_table(result.gold, expectation="everything passes"))

    failures = [
        (assertion, problem)
        for assertion in (result.guarded, result.gold)
        for problem in assertion.problems
    ]
    if failures:
        console.print()
        for assertion, problem in failures:
            console.print(Text(f"{assertion.phase.value}: ", style="bold red"), end="")
            console.print(Text(problem, style="red"))

    console.print()
    if result.ok:
        console.print(
            Text(
                f"validated: {task_id} holds at GUARDED and GOLD.",
                style="bold green",
            )
        )
    else:
        console.print(Text(f"not valid: {task_id}", style="bold red"))


def render_show_tests(bundle: Bundle) -> None:
    """Render the guardrail tests a bundle carries. The authoring aid."""
    spec = bundle.spec
    header = Table(title=f"task show-tests {spec.task_id}", title_justify="left", show_header=False)
    header.add_column("field", style="bold", no_wrap=True)
    header.add_column("value", overflow="fold")
    header.add_row("framework", spec.tests.framework)
    header.add_row("run command", spec.tests.run_cmd_template)
    header.add_row("timeout", f"{spec.tests.timeout_s}s")
    header.add_row("test path globs", ", ".join(spec.tests.test_path_globs))
    console.print(header)

    selectors = Table(title="selectors", title_justify="left", header_style="bold", expand=True)
    selectors.add_column("bucket", width=6)
    # The selector is the point of this table, so it gets the room: folding it
    # keeps the whole node id readable instead of eliding the part that differs.
    selectors.add_column("selector", overflow="fold", ratio=3)
    selectors.add_column("must", width=13, no_wrap=True)
    for selector in spec.tests.fail_to_pass:
        selectors.add_row(Text("f2p", style="cyan"), selector, "fail → pass")
    for selector in spec.tests.pass_to_pass:
        selectors.add_row(Text("p2p", style="blue"), selector, "pass → pass")
    console.print()
    console.print(selectors)

    console.print()
    console.print(Text("test_patch.diff", style="bold"))
    console.print(Syntax(bundle.test_patch, "diff", theme="ansi_dark", word_wrap=True))
