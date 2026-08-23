"""The glob matcher gates force-restore, the path jail, and the gaming flags."""

from __future__ import annotations

import pytest

from harness.core.globs import matches, matches_any

SPEC_GLOBS = ["tests/**", "**/test_*.py", "**/*_test.py"]


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("tests/test_a.py", "tests/**", True),
        ("tests/unit/deep/test_a.py", "tests/**", True),
        ("src/tests/test_a.py", "tests/**", False),
        # `**/` must match zero directories, or a top-level test file is missed.
        ("test_a.py", "**/test_*.py", True),
        ("src/pkg/test_a.py", "**/test_*.py", True),
        ("src/pkg/a_test.py", "**/*_test.py", True),
        # A single star must not cross a separator.
        ("src/pkg/mod.py", "src/*.py", False),
        ("src/mod.py", "src/*.py", True),
        ("src/a.py", "src/?.py", True),
        ("src/ab.py", "src/?.py", False),
        # Anchored at both ends: a prefix match is not a match.
        ("tests_helper.py", "tests/**", False),
        ("a/tests/x.py", "tests/**", False),
    ],
)
def test_matching(path, pattern, expected):
    assert matches(path, pattern) is expected


def test_leading_dot_slash_is_normalized():
    assert matches("./tests/test_a.py", "tests/**")


def test_dots_in_names_are_literal():
    # A naive translation turns `.` into "any character".
    assert not matches("srcXmod.py", "src.py")


def test_matches_any_over_the_spec_default_globs():
    assert matches_any("tests/test_intervals.py", SPEC_GLOBS)
    assert matches_any("pkg/thing_test.py", SPEC_GLOBS)
    assert not matches_any("tinylib/intervals.py", SPEC_GLOBS)
    assert not matches_any("README.md", SPEC_GLOBS)
