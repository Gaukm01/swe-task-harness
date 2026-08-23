"""Merging of closed integer intervals."""

from __future__ import annotations

Interval = tuple[int, int]


def merge(intervals: list[Interval]) -> list[Interval]:
    """Merge overlapping intervals into the smallest equivalent set.

    Each interval is a ``(start, end)`` pair with ``start <= end``. Intervals
    that overlap are combined into a single span.

    >>> merge([(1, 3), (2, 6)])
    [(1, 6)]
    """
    if not intervals:
        return []

    merged: list[list[int]] = [list(intervals[0])]
    for start, end in intervals[1:]:
        last = merged[-1]
        if start < last[1]:
            last[1] = max(last[1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]
