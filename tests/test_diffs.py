"""Path extraction decides whether a patch stayed in its lane."""

from __future__ import annotations

from harness.core.diffs import looks_like_unified_diff, touched_paths

GIT_DIFF = """diff --git a/tinylib/intervals.py b/tinylib/intervals.py
index 1111111..2222222 100644
--- a/tinylib/intervals.py
+++ b/tinylib/intervals.py
@@ -1,3 +1,3 @@
-old
+new
"""

RENAME = """diff --git a/old/name.py b/new/name.py
similarity index 90%
rename from old/name.py
rename to new/name.py
"""

CREATION = """diff --git a/tests/test_new.py b/tests/test_new.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/tests/test_new.py
@@ -0,0 +1 @@
+assert True
"""


def test_extracts_paths():
    assert touched_paths(GIT_DIFF) == ["tinylib/intervals.py"]


def test_rename_reports_both_sides():
    # Both matter: the old path stops existing and the new one starts.
    assert touched_paths(RENAME) == ["new/name.py", "old/name.py"]


def test_creation_ignores_dev_null():
    assert touched_paths(CREATION) == ["tests/test_new.py"]


def test_falls_back_to_unified_headers_without_a_git_header():
    plain = "--- a/pkg/mod.py\n+++ b/pkg/mod.py\n@@ -1 +1 @@\n-a\n+b\n"
    assert touched_paths(plain) == ["pkg/mod.py"]


def test_multiple_files_are_sorted_and_deduped():
    assert touched_paths(GIT_DIFF + CREATION) == ["tests/test_new.py", "tinylib/intervals.py"]


def test_looks_like_a_diff():
    assert looks_like_unified_diff(GIT_DIFF)
    assert not looks_like_unified_diff("")
    assert not looks_like_unified_diff("   \n\n")
    assert not looks_like_unified_diff("Fix the merge bug\n\nSigned-off-by: someone\n")
