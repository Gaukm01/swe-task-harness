"""Reading file paths out of a unified diff.

Only enough parsing to answer one question: which repo-relative paths does this
patch touch? That answer drives three separate guarantees -- that the gold
patch does not smuggle in test changes, that the test patch touches only test
files, and (later) that a solution diff gets a gaming flag when it edits the
guardrails. Applying patches is Git's job, not this module's.
"""

from __future__ import annotations

import re

# `diff --git a/old b/new` is emitted by git for every file in a patch, including
# renames, creations, and deletions, so it alone is enough to enumerate paths.
# Quoted paths appear when a name contains spaces or non-ASCII bytes.
_DIFF_GIT = re.compile(r'^diff --git (?:"?a/(?P<a>.+?)"?) (?:"?b/(?P<b>.+?)"?)\s*$', re.MULTILINE)

# Fallback for hand-written patches that have no `diff --git` header.
_UNIFIED_HEADER = re.compile(r"^(?:---|\+\+\+) (?:[ab]/)?(?P<path>[^\t\n]+)", re.MULTILINE)

_DEV_NULL = "/dev/null"


def touched_paths(diff_text: str) -> list[str]:
    """Repo-relative paths a unified diff touches, sorted and de-duplicated.

    A rename reports both sides: the old path stops existing and the new one
    starts, and both matter when deciding whether a patch touched a test file.
    """
    paths: set[str] = set()
    for match in _DIFF_GIT.finditer(diff_text):
        paths.update(p for p in (match.group("a"), match.group("b")) if p and p != _DEV_NULL)

    if not paths:
        for match in _UNIFIED_HEADER.finditer(diff_text):
            path = match.group("path").strip()
            if path and path != _DEV_NULL:
                paths.add(path)

    return sorted(paths)


def looks_like_unified_diff(diff_text: str) -> bool:
    """True if the text is plausibly a unified diff.

    Deliberately shallow. A real syntax check means applying the patch, which
    needs the repo, so `task lint` catches the common authoring mistakes -- an
    empty file, a pasted commit message, a base64 blob -- and leaves genuine
    application failures to `task init`, where they can be reported against a
    real tree.
    """
    if not diff_text.strip():
        return False
    return bool(_DIFF_GIT.search(diff_text)) or bool(_UNIFIED_HEADER.search(diff_text))
