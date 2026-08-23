# `merge()` returns wrong results for touching and unordered intervals

## Problem

`tinylib.merge()` reduces a list of closed integer intervals to the smallest
equivalent set. Two cases come back wrong.

**Touching intervals are not combined.** `[1, 2]` and `[2, 3]` share the
endpoint `2`, so together they describe the single continuous span `[1, 3]`.
`merge()` leaves them as two separate intervals.

```python
>>> merge([(1, 2), (2, 3)])
[(1, 2), (2, 3)]     # expected [(1, 3)]
```

**Unordered input produces nonsense.** The implementation walks the list
positionally and assumes it arrives sorted by start. Given input that is not,
it merges intervals that do not overlap and silently drops others.

```python
>>> merge([(5, 6), (1, 3)])
[(5, 6)]             # expected [(1, 3), (5, 6)]
```

Nothing in the function's documented contract says the caller must pre-sort,
and callers do not.

## Requirements

- Intervals that overlap **or touch at an endpoint** must be combined into one
  interval. `[1, 2]` and `[2, 3]` become `[1, 3]`.
- The input list may arrive in any order. The result must be correct
  regardless, and must be returned sorted ascending by start.
- The input list must not be mutated.
- An empty input returns an empty list.
- Behaviour on already-sorted, strictly overlapping input is correct today and
  must not change.

## Interface

The public entry point is unchanged:

```python
def merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]: ...
```

It is exported from the package root as `tinylib.merge`. Each interval is a
`(start, end)` pair with `start <= end`. The return value is a list of
`(start, end)` tuples.
