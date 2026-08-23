"""The store's one hard guarantee: a crashed command still leaves a row."""

from __future__ import annotations

import json

import pytest

from harness.store.db import Store


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "harness.db") as opened:
        yield opened


def test_schema_applies_idempotently(tmp_path):
    path = tmp_path / "harness.db"
    Store(path).close()
    Store(path).close()  # would raise "table already exists" without IF NOT EXISTS
    with Store(path) as store:
        assert store.latest_invocation() is None


def test_creates_parent_directories(tmp_path):
    Store(tmp_path / "nested" / "deeper" / "harness.db").close()
    assert (tmp_path / "nested" / "deeper" / "harness.db").exists()


def test_begin_records_before_the_command_runs(store):
    invocation_id = store.begin_invocation(argv=["task", "doctor"], cwd="/tmp")
    row = store.get_invocation(invocation_id)
    assert row is not None
    assert json.loads(row["argv"]) == ["task", "doctor"]
    # The unfinished state is the whole point: a process killed here still has a row.
    assert row["ended_at"] is None
    assert row["exit_code"] is None


def test_end_records_the_exit_code(store):
    invocation_id = store.begin_invocation(argv=["task", "lint"], cwd="/tmp")
    store.end_invocation(invocation_id, 3)
    row = store.get_invocation(invocation_id)
    assert row["exit_code"] == 3
    assert row["ended_at"] is not None


def test_unknown_invocation_returns_none(store):
    assert store.get_invocation("NOPE") is None


def test_latest_invocation_can_exclude_itself(store):
    first = store.begin_invocation(argv=["task", "doctor"], cwd="/tmp")
    second = store.begin_invocation(argv=["task", "log", "last"], cwd="/tmp")
    assert store.latest_invocation()["id"] == second
    # `task log last` must report the command before it, not itself.
    assert store.latest_invocation(before=second)["id"] == first


def test_recent_invocations_are_newest_first(store):
    ids = [store.begin_invocation(argv=["task", "doctor"], cwd="/tmp") for _ in range(3)]
    assert [row["id"] for row in store.recent_invocations()] == list(reversed(ids))


def test_upsert_task_inserts_then_updates(store):
    for digest in ("aaa", "bbb"):
        store.upsert_task(
            task_id="tiny-fixture",
            bundle_path="/x",
            bundle_digest=digest,
            language="python",
            framework="pytest",
        )
    row = store.get_task("tiny-fixture")
    assert row["bundle_digest"] == "bbb"
    # first_seen must survive the update; last_seen tracks the newest sighting.
    assert row["first_seen_at"] <= row["last_seen_at"]


def test_events_are_returned_in_sequence(store):
    invocation_id = store.begin_invocation(argv=["task", "lint"], cwd="/tmp")
    for seq in (2, 0, 1):
        store.add_event(kind="phase", payload={"n": seq}, invocation_id=invocation_id, seq=seq)
    events = store.events_for_invocation(invocation_id)
    assert [json.loads(e["payload"])["n"] for e in events] == [0, 1, 2]
