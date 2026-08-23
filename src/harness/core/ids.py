"""ULID generation.

Run and invocation ids are ULIDs: a 48-bit millisecond timestamp followed by 80
random bits, Crockford base32, 26 characters. Lexicographic order matches
creation order, which means `ORDER BY run_id` in SQLite is chronological and no
separate sequence column is needed.

That ordering guarantee is why the generator is monotonic. Two ids minted in the
same millisecond with independent randomness sort arbitrarily, so `task log
last` and the agent's event sequence would both be free to report the wrong
order. Within a millisecond the random field is incremented instead of
redrawn -- the technique the ULID spec calls monotonicity.

Implemented here rather than pulled in as a dependency: it is a fixed published
encoding, and the monotonic behaviour is a property this codebase depends on
rather than one it wants to inherit from a library's defaults.
"""

from __future__ import annotations

import os
import threading
import time

# Crockford base32: no I, L, O, or U, so ids survive being read aloud or
# retyped from a terminal without transcription errors.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ENCODED_LEN = 26
_TIMESTAMP_BITS = 48
_RANDOM_BITS = 80
_RANDOM_MAX = (1 << _RANDOM_BITS) - 1

_lock = threading.Lock()
_last_timestamp_ms = -1
_last_random = 0


def _encode(timestamp_ms: int, random_bits: int) -> str:
    value = (timestamp_ms & ((1 << _TIMESTAMP_BITS) - 1)) << _RANDOM_BITS | random_bits
    out = [""] * _ENCODED_LEN
    for index in range(_ENCODED_LEN - 1, -1, -1):
        out[index] = _ALPHABET[value & 0x1F]
        value >>= 5
    return "".join(out)


def new_ulid(timestamp_ms: int | None = None) -> str:
    """Return a new ULID. `timestamp_ms` is injectable so tests stay deterministic.

    Monotonic within this process: ids minted in the same millisecond are
    strictly increasing. Across processes, two ids from the same millisecond
    may sort either way -- unavoidable without coordination, and irrelevant
    because separate `task` invocations are milliseconds apart at best.
    """
    global _last_timestamp_ms, _last_random

    if timestamp_ms is None:
        timestamp_ms = time.time_ns() // 1_000_000

    with _lock:
        if timestamp_ms == _last_timestamp_ms:
            if _last_random >= _RANDOM_MAX:
                # Exhausting 80 bits inside one millisecond is not reachable in
                # practice; borrow from the next millisecond rather than wrap.
                timestamp_ms += 1
                random_bits = int.from_bytes(os.urandom(10), "big")
            else:
                random_bits = _last_random + 1
        else:
            random_bits = int.from_bytes(os.urandom(10), "big")

        # Never let an injected older timestamp rewind the sequence.
        if timestamp_ms < _last_timestamp_ms:
            timestamp_ms = _last_timestamp_ms
            random_bits = min(_last_random + 1, _RANDOM_MAX)

        _last_timestamp_ms = timestamp_ms
        _last_random = random_bits

    return _encode(timestamp_ms, random_bits)


def is_ulid(candidate: str) -> bool:
    """True if `candidate` is shaped like a ULID."""
    return len(candidate) == _ENCODED_LEN and all(c in _ALPHABET for c in candidate)
