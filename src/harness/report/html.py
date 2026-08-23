"""Static HTML rendering from SQLite.

Read-only by construction: `task ui` reads the database and writes files. There
is no server, no JavaScript build step, and no control that triggers a run --
a reviewer double-clicks a file and sees everything, on a plane, a year from
now, with the harness uninstalled.

Everything a page needs is inlined, so a single `.html` file is the whole
artifact.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from harness import __version__
from harness.store.db import Store

TEMPLATE_DIR = Path(__file__).with_name("templates")

# HuggingFace renders one row per instance; linking there turns an opaque
# instance id into the actual issue a reviewer can read.
INSTANCE_URL = "https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro/viewer/default/test?q={query}"

# A diff long enough to bury the page is worth truncating; the full text is
# always on disk next to the report.
MAX_DIFF_LINES = 600


def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


@dataclass
class DiffLine:
    """One line of a unified diff, classed for colouring."""

    text: str
    cls: str


def diff_lines(diff: str) -> list[DiffLine]:
    """Class each diff line so added/removed/header read at a glance."""
    lines: list[DiffLine] = []
    for raw in diff.splitlines()[:MAX_DIFF_LINES]:
        if raw.startswith("+++") or raw.startswith("---") or raw.startswith("diff --git"):
            css = "h"
        elif raw.startswith("@@"):
            css = "h"
        elif raw.startswith("+"):
            css = "a"
        elif raw.startswith("-"):
            css = "d"
        else:
            css = ""
        lines.append(DiffLine(text=raw or " ", cls=css))

    total = len(diff.splitlines())
    if total > MAX_DIFF_LINES:
        lines.append(DiffLine(text=f"... [{total - MAX_DIFF_LINES} more lines]", cls="h"))
    return lines


def _summary_of(report: dict[str, Any]) -> dict[str, int]:
    summary = report.get("summary") or {}
    return {
        "f2p_fixed": int(summary.get("f2p_fixed") or 0),
        "f2p_total": int(summary.get("f2p_total") or 0),
        "p2p_regressed": int(summary.get("p2p_regressed") or 0),
        "p2p_total": int(summary.get("p2p_total") or 0),
    }


def _read_report(runs_dir: Path, run_id: str) -> dict[str, Any]:
    path = runs_dir / run_id / "report.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:  # pragma: no cover - defensive
        return {}


def _events_for(store: Store, run_id: str) -> list[dict[str, Any]]:
    """Agent tool calls, shaped for the timeline."""
    rows = store._conn.execute(  # noqa: SLF001 - the store is ours
        "SELECT * FROM events WHERE run_id = ? AND kind = 'tool_call' ORDER BY seq, id",
        (run_id,),
    ).fetchall()

    events: list[dict[str, Any]] = []
    for row in rows:
        payload = json.loads(row["payload"])
        arguments = payload.get("arguments") or {}
        preview = ""
        for key in ("path", "command", "selectors", "summary"):
            if key in arguments:
                preview = str(arguments[key])[:80]
                break
        events.append(
            {
                "turn": payload.get("turn", 0),
                "label": payload.get("tool", "?"),
                "preview": preview,
                "arguments": json.dumps(arguments, indent=2) if arguments else "",
                "result": payload.get("result", ""),
                "is_error": bool(payload.get("is_error")),
                "refused": bool(payload.get("refused")),
            }
        )
    return events


def build_run_context(store: Store, row: sqlite3.Row, runs_dir: Path) -> dict[str, Any]:
    """Everything one run page needs."""
    run_id = row["run_id"]
    report = _read_report(runs_dir, run_id)
    summary = _summary_of(report)

    diff_path = runs_dir / run_id / "solution.diff"
    diff = diff_path.read_text() if diff_path.is_file() else ""

    instance_id = ""
    task = store.get_task(row["task_id"])
    if task:
        bundle_task_json = Path(task["bundle_path"]) / "task.json"
        if bundle_task_json.is_file():
            try:
                instance_id = (
                    json.loads(bundle_task_json.read_text()).get("source", {}).get("instance_id")
                    or ""
                )
            except json.JSONDecodeError:  # pragma: no cover - defensive
                instance_id = ""

    return {
        "run_id": run_id,
        "task_id": row["task_id"],
        "solver_kind": row["solver_kind"],
        "solver_model": row["solver_model"],
        "outcome": row["outcome"],
        "bundle_digest": row["bundle_digest"],
        "image_digest": row["image_digest"],
        "turns": row["turns"],
        "cost_usd": row["cost_usd"],
        "gaming_flags": json.loads(row["gaming_flags"] or "[]"),
        "timings": json.loads(row["timings_ms"] or "{}"),
        "started_at": (row["started_at"] or "")[:19].replace("T", " "),
        "tests": report.get("tests", []),
        "notes": report.get("notes", []),
        "restored_test_paths": report.get("restored_test_paths", []),
        "restored_test_count": report.get("restored_test_count", 0),
        "diff_lines": diff_lines(diff),
        "events": _events_for(store, run_id),
        "instance_id": instance_id,
        "instance_url": INSTANCE_URL.format(query=instance_id) if instance_id else None,
        **summary,
    }


def render_site(store: Store, out_dir: Path, *, runs_dir: Path = Path("runs")) -> list[Path]:
    """Write index.html plus one page per run. Returns what it wrote."""
    out_dir.mkdir(parents=True, exist_ok=True)
    environment = _environment()
    generated_at = datetime.now(UTC).isoformat(timespec="seconds")

    contexts = [
        build_run_context(store, row, runs_dir) for row in store.list_runs(limit=500)
    ]

    written: list[Path] = []
    run_template = environment.get_template("run.html.j2")
    for context in contexts:
        path = out_dir / f"run-{context['run_id']}.html"
        path.write_text(
            run_template.render(run=context, version=__version__, generated_at=generated_at)
        )
        written.append(path)

    index = out_dir / "index.html"
    index.write_text(
        environment.get_template("index.html.j2").render(
            runs=contexts, version=__version__, generated_at=generated_at
        )
    )
    written.append(index)
    return written


def render_single_run(store: Store, run_id: str, out_path: Path, *, runs_dir: Path) -> Path:
    """Render one run's page, for `task report --format html`."""
    row = store.get_run(run_id)
    if row is None:
        raise FileNotFoundError(run_id)
    context = build_run_context(store, row, runs_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        _environment()
        .get_template("run.html.j2")
        .render(
            run=context,
            version=__version__,
            generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
    )
    return out_path
