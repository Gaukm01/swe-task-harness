from tinylib import merge


def test_empty_input():
    assert merge([]) == []


def test_single_interval():
    assert merge([(1, 4)]) == [(1, 4)]


def test_merges_overlapping():
    assert merge([(1, 3), (2, 6)]) == [(1, 6)]


def test_keeps_disjoint_intervals():
    assert merge([(1, 2), (5, 6)]) == [(1, 2), (5, 6)]
