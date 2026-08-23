"""Glob matching for repo-relative POSIX paths.

`fnmatch` treats `*` as matching across separators and has no notion of `**`,
so `tests/*` would match `tests/a/b.py` and `**/test_*.py` would not match a
top-level `test_x.py`. Both are wrong for deciding whether a path is a
guardrail test file, and that decision gates the force-restore step, the path
jail, and the gaming flags -- so the matcher is written out explicitly here.

Supported syntax:

* `**/` -- zero or more leading directories
* `**`  -- anything, separators included
* `*`   -- anything within a single path segment
* `?`   -- one character within a single path segment
"""

from __future__ import annotations

import re
from functools import lru_cache


@lru_cache(maxsize=512)
def _compile(pattern: str) -> re.Pattern[str]:
    """Translate one glob into an anchored regex."""
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        char = pattern[i]
        if char == "*":
            if pattern.startswith("**/", i):
                # Zero or more directories: `**/test_*.py` must match `test_x.py` too.
                out.append("(?:.*/)?")
                i += 3
                continue
            if pattern.startswith("**", i):
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        i += 1
    return re.compile("".join(out) + r"\Z")


def matches(path: str, pattern: str) -> bool:
    """True if a repo-relative POSIX path matches one glob."""
    normalized = path[2:] if path.startswith("./") else path
    return _compile(pattern).fullmatch(normalized) is not None


def matches_any(path: str, patterns: list[str]) -> bool:
    """True if the path matches at least one of the globs."""
    return any(matches(path, p) for p in patterns)
