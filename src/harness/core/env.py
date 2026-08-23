"""Loading `ANTHROPIC_API_KEY` from the environment or a local `.env`.

The key is read from the process environment and nowhere else -- it is never
written to `task.json`, the database, a report, a cassette, or a log line. The
`.env` file is a convenience for getting it *into* the environment without
re-exporting it in every shell; a value already exported always wins.

Nothing here ever returns the key itself for display. `mask` exists so that
`task doctor` can prove a key is present and roughly which one, without putting
a secret on a terminal that may be screen-shared or pasted into an issue.
"""

from __future__ import annotations

import os
from pathlib import Path

API_KEY_VAR = "ANTHROPIC_API_KEY"
MODEL_VAR = "HARNESS_MODEL"
DOTENV_FILENAME = ".env"

# Anthropic keys carry this prefix. Checking it catches the common paste
# errors -- a truncated copy, a whole `export ...` line, a key with quotes --
# before they turn into an auth failure partway through a rate-limited run.
_KEY_PREFIX = "sk-ant-"
_MIN_PLAUSIBLE_LENGTH = 40


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse a minimal `.env`: `KEY=value`, `#` comments, optional `export`."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        value = value.strip()
        # Tolerate quoted values; people copy them out of shell snippets.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_dotenv(directory: Path | None = None) -> list[str]:
    """Load `.env` into the process environment. Returns the names it set.

    An existing environment variable is never overwritten: an explicit
    `export ANTHROPIC_API_KEY=...` must beat a stale file, or debugging "which
    key is it actually using" becomes guesswork.
    """
    path = (directory or Path.cwd()) / DOTENV_FILENAME
    if not path.is_file():
        return []

    applied: list[str] = []
    for key, value in parse_dotenv(path.read_text()).items():
        if value and not os.environ.get(key):
            os.environ[key] = value
            applied.append(key)
    return applied


def api_key() -> str | None:
    """The configured key, or None. Never logged."""
    value = os.environ.get(API_KEY_VAR, "").strip()
    return value or None


def mask(key: str) -> str:
    """A safe-to-display fingerprint: enough to identify, not enough to use."""
    if len(key) <= 12:
        return "*" * len(key)
    return f"{key[:11]}…{key[-4:]}"


def key_problem(key: str) -> str | None:
    """A human-readable reason a key looks malformed, or None if it looks fine."""
    if key != key.strip():
        return "has leading or trailing whitespace"
    if key.startswith(("'", '"')):
        return "is wrapped in quotes"
    if key.lower().startswith("export "):
        return "still includes the `export ` prefix"
    if not key.startswith(_KEY_PREFIX):
        return f"does not start with {_KEY_PREFIX!r}"
    if len(key) < _MIN_PLAUSIBLE_LENGTH:
        return "looks truncated"
    return None
