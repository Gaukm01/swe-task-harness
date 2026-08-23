"""ULIDs must sort chronologically, or `ORDER BY id` lies."""

from __future__ import annotations

from harness.core.ids import is_ulid, new_ulid


def test_shape():
    ulid = new_ulid()
    assert len(ulid) == 26
    assert is_ulid(ulid)


def test_uniqueness_within_one_millisecond():
    assert len({new_ulid(timestamp_ms=1_700_000_000_000) for _ in range(500)}) == 500


def test_ids_in_the_same_millisecond_still_sort_by_creation_order():
    # Independent randomness per id would make this ordering a coin flip, and
    # `task log last` plus the agent event sequence both depend on it.
    ids = [new_ulid(timestamp_ms=1_700_000_000_000) for _ in range(200)]
    assert ids == sorted(ids)


def test_ordering_holds_across_a_real_clock():
    ids = [new_ulid() for _ in range(200)]
    assert ids == sorted(ids)


def test_an_older_timestamp_cannot_rewind_the_sequence():
    newer = new_ulid(timestamp_ms=1_900_000_000_000)
    older = new_ulid(timestamp_ms=1_500_000_000_000)
    assert older > newer


def test_lexicographic_order_follows_time():
    earlier = new_ulid(timestamp_ms=1_700_000_000_000)
    later = new_ulid(timestamp_ms=1_700_000_000_001)
    much_later = new_ulid(timestamp_ms=1_800_000_000_000)
    assert earlier < later < much_later


def test_rejects_non_ulid_strings():
    assert not is_ulid("")
    assert not is_ulid("nope")
    # I, L, O, and U are excluded from Crockford base32.
    assert not is_ulid("I" * 26)
