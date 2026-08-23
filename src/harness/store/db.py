"""SQLite storage.

Plain SQL against a single `schema.sql`, applied idempotently on connect. No
ORM: six tables do not justify one, and a collaborator can open `harness.db`
with the `sqlite3` CLI and understand everything without reading Python.

The one behaviour worth stating: `begin_invocation` commits its row before the
command it describes does any work, and `end_invocation` updates it in a
`finally`. A crash, a `kill -9`, or an unhandled exception therefore still
leaves a row -- one with a NULL `ended_at`, which is itself the evidence that
the process died rather than exited.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from harness import __version__
from harness.core.ids import new_ulid

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

DEFAULT_DB_FILENAME = "harness.db"


def utc_now() -> str:
    """Current time as an ISO-8601 UTC string, the only time format stored."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Store:
    """A connection to the harness database."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        # WAL keeps a reader (`task ui`, `task runs`) from blocking a long run.
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA_PATH.read_text())

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    # -- invocations ------------------------------------------------------

    def begin_invocation(self, argv: list[str], cwd: str) -> str:
        """Record that a CLI call started. Returns its id."""
        invocation_id = new_ulid()
        self._conn.execute(
            "INSERT INTO invocations (id, argv, cwd, harness_version, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (invocation_id, json.dumps(argv), cwd, __version__, utc_now()),
        )
        return invocation_id

    def end_invocation(self, invocation_id: str, exit_code: int) -> None:
        """Record that a CLI call finished, with its exit code."""
        self._conn.execute(
            "UPDATE invocations SET ended_at = ?, exit_code = ? WHERE id = ?",
            (utc_now(), exit_code, invocation_id),
        )

    def get_invocation(self, invocation_id: str) -> sqlite3.Row | None:
        cursor = self._conn.execute("SELECT * FROM invocations WHERE id = ?", (invocation_id,))
        row: sqlite3.Row | None = cursor.fetchone()
        return row

    def latest_invocation(self, *, before: str | None = None) -> sqlite3.Row | None:
        """The most recent invocation, optionally excluding one id.

        `before` exists so `task log last` can skip the row for the `task log`
        call itself, which is otherwise always the newest and never what the
        user meant.
        """
        if before is None:
            cursor = self._conn.execute("SELECT * FROM invocations ORDER BY id DESC LIMIT 1")
        else:
            cursor = self._conn.execute(
                "SELECT * FROM invocations WHERE id != ? ORDER BY id DESC LIMIT 1", (before,)
            )
        row: sqlite3.Row | None = cursor.fetchone()
        return row

    def recent_invocations(self, limit: int = 20) -> list[sqlite3.Row]:
        cursor = self._conn.execute("SELECT * FROM invocations ORDER BY id DESC LIMIT ?", (limit,))
        return cursor.fetchall()

    # -- tasks ------------------------------------------------------------

    def upsert_task(
        self,
        *,
        task_id: str,
        bundle_path: str,
        bundle_digest: str,
        language: str,
        framework: str,
    ) -> None:
        """Register a task, or refresh what was last seen for it."""
        now = utc_now()
        self._conn.execute(
            """
            INSERT INTO tasks (task_id, bundle_path, bundle_digest, language, framework,
                               first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (task_id) DO UPDATE SET
                bundle_path   = excluded.bundle_path,
                bundle_digest = excluded.bundle_digest,
                language      = excluded.language,
                framework     = excluded.framework,
                last_seen_at  = excluded.last_seen_at
            """,
            (task_id, bundle_path, bundle_digest, language, framework, now, now),
        )

    def get_task(self, task_id: str) -> sqlite3.Row | None:
        cursor = self._conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        row: sqlite3.Row | None = cursor.fetchone()
        return row

    # -- events -----------------------------------------------------------

    def add_event(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        run_id: str | None = None,
        invocation_id: str | None = None,
        seq: int = 0,
    ) -> None:
        """Append an event. Notable harness moments and, later, agent turns."""
        self._conn.execute(
            "INSERT INTO events (run_id, invocation_id, seq, at, kind, payload) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, invocation_id, seq, utc_now(), kind, json.dumps(payload)),
        )

    def events_for_invocation(self, invocation_id: str) -> list[sqlite3.Row]:
        cursor = self._conn.execute(
            "SELECT * FROM events WHERE invocation_id = ? ORDER BY seq, id", (invocation_id,)
        )
        rows: list[sqlite3.Row] = cursor.fetchall()
        return rows

    # -- runs -------------------------------------------------------------

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        cursor = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        row: sqlite3.Row | None = cursor.fetchone()
        return row
