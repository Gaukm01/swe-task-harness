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

import itertools
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
    render_base_result,
    render_check_report,
    render_error,
    render_invocation,
    render_run_result,
    render_show_tests,
    render_unexpected,
    render_validation,
)
from harness.core.bundle import (
    Bundle,
    TaskSpec,
    compute_bundle_digest,
    lint_bundle,
    load_bundle,
)
from harness.core.cache import compute_cache_key
from harness.core.errors import (
    BaselineValidationError,
    ExitCode,
    HarnessError,
    UsageError,
)
from harness.core.ids import new_ulid
from harness.core.phases import BaseResult, Phase, prepare_base, validate_task
from harness.core.results import Bucket, TestOutcome, TestStatus
from harness.core.run import execute_run
from harness.report import build_report, write_report
from harness.solvers import DEFAULT_MAX_TURNS, resolve_solver
from harness.runtime.docker import DockerRuntime
from harness.runtime.probe import collect_doctor_report
from harness.store.db import DEFAULT_DB_FILENAME, Store, utc_now

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
    loaded, result = _ensure_base(bundle, no_cache=no_cache)
    render_base_result(result, task_id=loaded.spec.task_id, bundle_digest=loaded.digest)


@app.command()
def validate(
    bundle: BundleArg,
    repeat: Annotated[
        int, typer.Option("--repeat", help="Run the guardrail assertions N times.")
    ] = 1,
    keep_snapshots: Annotated[
        bool,
        typer.Option(
            "--keep-snapshots",
            help="Keep the GUARDED/GOLD images even when validation passes.",
        ),
    ] = False,
) -> None:
    """Assert the GUARDED and GOLD phases hold: p2p pass, f2p fail then pass."""
    if repeat != 1:
        raise UsageError(
            "--repeat is not implemented.",
            fix="Flake detection is on the cut list, after M8. Run `task validate` again "
            "by hand if you need a second opinion.",
        )

    loaded, base = _ensure_base(bundle)
    runtime = DockerRuntime()
    validation_id = new_ulid()
    artifact_dir = Path("runs") / validation_id / "validate"

    result = validate_task(
        runtime,
        loaded,
        base,
        validation_id=validation_id,
        artifact_dir=artifact_dir,
        keep_snapshots=keep_snapshots,
    )

    store = state.require_store()
    for assertion in (result.guarded, result.gold):
        store.add_event(
            kind="phase",
            payload={
                "phase": assertion.phase.value,
                "task_id": loaded.spec.task_id,
                "image": assertion.image,
                "ok": assertion.ok,
                "problems": assertion.problems,
            },
            invocation_id=state.invocation_id,
        )

    render_validation(result, task_id=loaded.spec.task_id, artifact_dir=artifact_dir)

    if not result.ok:
        # Exit 4 exists so a wrapper can tell "this bundle is broken" from
        # "this solver failed" without reading any output.
        raise BaselineValidationError(
            f"{loaded.spec.task_id} did not validate: {result.problems[0]}",
            fix="Fix the bundle or the fixture, then re-run `task validate`. "
            f"Artifacts, including junit XML, are in {artifact_dir}.",
        )


@app.command()
def run(
    bundle: BundleArg,
    solver: Annotated[
        str, typer.Option("--solver", help="gold | noop | agent | replay:<run_id> | cmd:<command>")
    ],
    model: Annotated[str | None, typer.Option("--model", help="Model for --solver agent.")] = None,
    max_turns: Annotated[
        int, typer.Option("--max-turns", help="Agent turn ceiling.")
    ] = DEFAULT_MAX_TURNS,
    max_cost_usd: Annotated[
        float | None, typer.Option("--max-cost-usd", help="Agent spend ceiling.")
    ] = None,
    no_cache: Annotated[
        bool, typer.Option("--no-cache", help="Ignore cached snapshots and baselines.")
    ] = False,
) -> None:
    """Validate the baseline, run a solver, grade the result, and write a report."""
    loaded, base = _ensure_base(bundle, no_cache=no_cache)
    store = state.require_store()

    run_id = new_ulid()
    artifact_dir = Path("runs") / run_id
    started_at = utc_now()

    # The run row is written before the solver starts, for the same reason the
    # invocation row is: events reference it as they happen, and a run killed
    # halfway should still leave a readable trajectory rather than a foreign-key
    # error. `record_run` upserts, so the final write updates this row.
    store.record_run(
        run_id=run_id,
        invocation_id=state.invocation_id,
        task_id=loaded.spec.task_id,
        bundle_digest=loaded.digest,
        image_digest=base.image,
        cache_key=base.cache_key,
        solver_kind=solver.partition(":")[0],
        solver_model=model,
        phase_reached=Phase.BASE.value,
        outcome=None,
        gaming_flags=[],
        turns=0,
        cost_usd=0.0,
        timings_ms={},
        started_at=started_at,
    )

    # Agent turns land in `events` as they happen.
    sequence = itertools.count()

    def record_event(kind: str, payload: dict[str, object]) -> None:
        store.add_event(
            kind=kind,
            payload=payload,
            run_id=run_id,
            invocation_id=state.invocation_id,
            seq=next(sequence),
        )

    solver_impl = resolve_solver(
        solver,
        model=model,
        max_turns=max_turns,
        max_cost_usd=max_cost_usd,
        cassette_dir=artifact_dir / "llm",
        on_event=record_event,
        wall_clock_s=float(loaded.spec.tests.timeout_s * 4),
    )

    # Invariant 3: a cached baseline is acceptable only when the bundle digest
    # *and* the environment cache key both match. Anything else re-validates.
    cached = (
        None
        if no_cache
        else store.find_validation(
            bundle_digest=loaded.digest, cache_key=base.cache_key
        )
    )
    cached_baseline = _decode_baseline(cached) if cached else None

    runtime = DockerRuntime()
    result = execute_run(
        runtime,
        loaded,
        base,
        solver_impl,
        run_id=run_id,
        artifact_dir=artifact_dir,
        cached_baseline=cached_baseline,
    )

    if cached_baseline is None:
        store.record_validation(
            bundle_digest=loaded.digest,
            cache_key=base.cache_key,
            outcomes=[
                {
                    "test_id": o.test_id,
                    "bucket": o.bucket.value,
                    "status": o.status.value,
                    "duration_ms": o.duration_ms,
                    "message": o.message,
                }
                for o in result.baseline
            ],
        )

    diff_path = artifact_dir / "solution.diff"
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    diff_path.write_text(result.solution_diff)

    report = build_report(
        result,
        artifacts={"solution_diff": str(diff_path), "report_json": str(artifact_dir / "report.json")},
    )
    report_path = write_report(report, artifact_dir / "report.json")

    _persist_run(store, result, started_at, report_path, diff_path)
    render_run_result(result, report_path=report_path)


def _decode_baseline(rows: list[dict[str, object]]) -> list[TestOutcome]:
    """Rebuild baseline outcomes from a cached validation event."""
    return [
        TestOutcome(
            test_id=str(row["test_id"]),
            bucket=Bucket(str(row["bucket"])),
            status=TestStatus(str(row["status"])),
            duration_ms=int(row.get("duration_ms") or 0),
            message=(str(row["message"]) if row.get("message") else None),
        )
        for row in rows
    ]


def _persist_run(store: Store, result: object, started_at: str, report_path: Path, diff_path: Path) -> None:
    """Write the run, its per-test results, and its artifacts to SQLite."""
    import hashlib

    from harness.core.run import RunResult

    assert isinstance(result, RunResult)
    store.record_run(
        run_id=result.run_id,
        invocation_id=state.invocation_id,
        task_id=result.bundle.spec.task_id,
        bundle_digest=result.bundle.digest,
        image_digest=result.base.image,
        cache_key=result.base.cache_key,
        solver_kind=result.solver.kind,
        solver_model=result.solver.model,
        phase_reached=Phase.SCORED.value,
        outcome=result.outcome.value,
        gaming_flags=result.gaming_flags,
        turns=result.solver.turns,
        cost_usd=result.solver.cost_usd,
        timings_ms=result.timings.as_dict(),
        started_at=started_at,
    )
    for phase, outcomes in (("pre", result.baseline), ("post", result.post)):
        store.record_test_results(
            result.run_id,
            phase,
            [(o.test_id, o.bucket.value, o.status.value, o.duration_ms, o.message) for o in outcomes],
        )
    for kind, path in (("report_json", report_path), ("solution_diff", diff_path)):
        store.record_artifact(
            run_id=result.run_id,
            kind=kind,
            path=str(path),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )


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
    """Print the stored report for a recorded run."""
    if fmt == "html":
        _pending("report --format html", "M8")
    if fmt != "json":
        raise UsageError(f"unknown format {fmt!r}.", fix="Use --format json (html lands in M8).")

    stored = state.require_store().get_run(run_id)
    if stored is None:
        raise UsageError(
            f"no run with id {run_id!r}.",
            fix="List recorded runs with `task runs`.",
        )
    path = Path("runs") / run_id / "report.json"
    if not path.is_file():
        raise UsageError(
            f"{path} is missing.",
            fix="The run is recorded but its artifacts were deleted. Re-run the task.",
        )
    # stdout only, so it pipes into jq cleanly.
    console.print_json(path.read_text())


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
    """Render a bundle's test patch and selector lists. Authoring aid.

    The bundle format stores guardrail tests as a diff, which cannot be read as
    files. This is the mitigation for that: it shows exactly what will be
    applied and which selectors decide the outcome.
    """
    loaded = load_bundle(bundle)
    render_show_tests(loaded)


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


def _ensure_base(bundle: Path, *, no_cache: bool = False) -> tuple[Bundle, BaseResult]:
    """Load the bundle, register the task, and make sure a BASE snapshot exists.

    Shared by `init` and `validate`. `validate` cannot be run against a bundle
    that has not been built, and making the user run `init` first would be
    friction with no safety value -- the cache makes the second call free.
    """
    loaded = load_bundle(bundle)
    spec = loaded.spec

    state.require_store().upsert_task(
        task_id=spec.task_id,
        bundle_path=str(bundle.resolve()),
        bundle_digest=loaded.digest,
        language=spec.language,
        framework=spec.tests.framework,
    )

    runtime = DockerRuntime()
    # Resolve the pulled digest first when the environment names an image, so a
    # republished mutable tag invalidates the cache instead of silently changing
    # what runs.
    base_image_digest = (
        runtime.image_digest(spec.environment.image) if spec.environment.image else None
    )
    cache_key = compute_cache_key(spec, bundle, base_image_digest=base_image_digest)
    result = prepare_base(runtime, spec, bundle, cache_key=cache_key, no_cache=no_cache)

    state.require_store().add_event(
        kind="phase",
        payload={
            "phase": Phase.BASE.value,
            "task_id": spec.task_id,
            "image": result.image,
            "cache_key": cache_key,
            "cached": result.cached,
            "duration_ms": result.duration_ms,
        },
        invocation_id=state.invocation_id,
    )
    return loaded, result


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
