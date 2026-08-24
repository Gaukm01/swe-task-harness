"""SWE-Bench Pro instance -> task bundle.

The dataset is 731 rows on HuggingFace with 16 fields. Four of them are not
metadata at all -- `problem_statement`, `requirements`, and `interface` become
`description.md`, and the two patch fields become the two diffs -- so importing
is a transform, not a copy.

Three quirks are handled here because they were verified against the live
dataset rather than assumed:

* `fail_to_pass` / `pass_to_pass` are **not consistently JSON**. Some rows are
  Python reprs with single quotes. Both forms appear in the same row.
* `instance_id` is 65-120 characters with uppercase and `__`, which no Docker
  tag accepts, so `task_id` is sanitized and the original kept in `source`.
* `dockerhub_tag` is truncated to 128 characters and is therefore *not*
  derivable from `instance_id`. It must be used verbatim.
"""

from __future__ import annotations

import ast
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.core.bundle import (
    DESCRIPTION_MD,
    PATCH_DIFF,
    TASK_JSON,
    TEST_PATCH_DIFF,
    TASK_ID_RE,
)
from harness.core.diffs import touched_paths
from harness.core.errors import UsageError

DATASET = "ScaleAI/SWE-bench_Pro"
ROWS_ENDPOINT = "https://datasets-server.huggingface.co/rows"
PAGE_SIZE = 100
RATE_LIMIT_RETRIES = 5
RATE_LIMIT_BACKOFF_S = 6
TOTAL_ROWS_GUESS = 731

# Every instance ships a prebuilt image under this repository.
IMAGE_REPOSITORY = "jefzda/sweap-images"

# The dataset's images are amd64 only.
PLATFORM = "linux/amd64"

# Generous, because these run under emulation on Apple Silicon.
DEFAULT_TIMEOUT_S = 600

_LANGUAGE_MAP = {
    "python": "python",
    "py": "python",
    "go": "go",
    "js": "js",
    "javascript": "js",
    "ts": "ts",
    "typescript": "ts",
}

_FRAMEWORK_BY_LANGUAGE = {
    "python": "pytest",
    "go": "go",
    "js": "jest",
    "ts": "jest",
}

_RUN_TEMPLATE = {
    "pytest": "python -m pytest {selectors} --junitxml={out}",
    "go": "go test -json {selectors} > {out}",
    "jest": "npx jest {selectors} --json --outputFile={out}",
}

# Fallback globs, unioned with the instance's own declared test files.
_GENERIC_TEST_GLOBS = ["tests/**", "test/**", "**/test_*.py", "**/*_test.py", "**/*.test.js"]


def decode_selector_list(raw: str | list[str] | None) -> list[str]:
    """Decode a selector list that may be JSON, a Python repr, or already a list.

    Verified necessary: one qutebrowser row had `pass_to_pass` as valid JSON
    and `fail_to_pass` as a Python repr with single quotes.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(item) for item in raw]

    text = raw.strip()
    if not text:
        return []
    for parse in (json.loads, ast.literal_eval):
        try:
            value = parse(text)
        except (ValueError, SyntaxError):
            continue
        if isinstance(value, list):
            return [str(item) for item in value]
    # A single bare selector is still a selector.
    return [text]


def sanitize_task_id(instance_id: str) -> str:
    """A Docker-tag-safe task id derived from an instance id.

    Instance ids look like
    `instance_qutebrowser__qutebrowser-f91ace96...-v059c6fd...`: uppercase,
    double underscores, and 65-120 characters. Docker repository names allow
    only lowercase alphanumerics, dots, dashes, and underscores.
    """
    cleaned = instance_id.lower()
    cleaned = cleaned.removeprefix("instance_")
    cleaned = re.sub(r"[^a-z0-9._-]+", "-", cleaned)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-._")

    # Docker tags cap at 128; keep well under and keep the *tail*, which holds
    # the commit shas that distinguish two instances of the same repo.
    if len(cleaned) > 60:
        head, tail = cleaned[:24].rstrip("-"), cleaned[-32:].lstrip("-")
        cleaned = f"{head}-{tail}"
    if not cleaned or not TASK_ID_RE.match(cleaned):
        cleaned = "instance-" + re.sub(r"[^a-z0-9]+", "", instance_id.lower())[:40]
    return cleaned


def decode_text_field(raw: str | None) -> str:
    """Decode a prose field that the dataset stores JSON-encoded.

    Verified against the live dataset: `problem_statement`, `requirements`, and
    `interface` arrive wrapped in double quotes with literal backslash-n and
    *zero* real newlines. Written straight to description.md they become an
    unreadable single-line wall -- which is the agent's only input, so it would
    quietly halve the quality of every imported task.

    Only decoded when the value actually looks JSON-encoded, so a field that is
    already plain prose is left exactly as it is.
    """
    if raw is None:
        return ""
    text = raw.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            return raw
        if isinstance(decoded, str):
            return decoded
    return raw


def build_description(row: dict[str, Any]) -> str:
    """problem_statement + requirements + interface, in that order.

    All three, because published baselines prompt with all three: requirements
    encode test-grounded details (exact routes, strings) the agent cannot
    otherwise know, and interface names the expected symbols, which avoids
    false negatives where a correct fix used a different name.
    """
    sections = [
        ("", decode_text_field(row.get("problem_statement")).strip()),
        ("## Requirements", decode_text_field(row.get("requirements")).strip()),
        ("## Interface", decode_text_field(row.get("interface")).strip()),
    ]
    parts = []
    for heading, body in sections:
        if not body:
            continue
        parts.append(f"{heading}\n\n{body}".strip() if heading else body)
    return "\n\n".join(parts) + "\n"


def build_test_path_globs(row: dict[str, Any], test_patch: str) -> list[str]:
    """Which paths count as guardrail tests.

    The union of three sources, because each misses something the others
    catch: the instance's own `selected_test_files_to_run`, every path the test
    patch touches, and generic fallbacks. Getting this wrong breaks
    force-restore, the path jail, and the gaming flags at once.
    """
    globs: list[str] = []
    for path in decode_selector_list(row.get("selected_test_files_to_run")):
        if path and path not in globs:
            globs.append(path)
    for path in touched_paths(test_patch):
        if path not in globs:
            globs.append(path)
    for glob in _GENERIC_TEST_GLOBS:
        if glob not in globs:
            globs.append(glob)
    return globs


@dataclass
class ImportedBundle:
    """Where a bundle was written and what it describes."""

    path: Path
    task_id: str
    instance_id: str
    language: str
    fail_to_pass: int
    pass_to_pass: int
    image: str


def fetch_rows(offset: int, length: int = PAGE_SIZE, *, timeout: int = 60) -> list[dict[str, Any]]:
    """One page of the dataset, via the datasets-server HTTP API.

    The HTTP API rather than the `datasets` library: this needs one instance,
    not a multi-hundred-megabyte parquet download, and it adds no dependency.
    The `/filter` endpoint repeatedly returned "index is loading" and
    "corrupted", so paging `/rows` and filtering client-side is what actually
    works -- 731 rows is eight requests.
    """
    query = urllib.parse.urlencode(
        {
            "dataset": DATASET,
            "config": "default",
            "split": "test",
            "offset": offset,
            "length": length,
        }
    )
    # The datasets server rate-limits a burst of page requests with a 429.
    # Scanning 731 rows is eight requests, and a survey does several scans, so
    # this is reachable in normal use -- back off rather than failing the import.
    last: Exception | None = None
    for attempt in range(RATE_LIMIT_RETRIES):
        try:
            with urllib.request.urlopen(f"{ROWS_ENDPOINT}?{query}", timeout=timeout) as response:
                payload = json.loads(response.read())
            break
        except urllib.error.HTTPError as error:
            last = error
            if error.code != 429:
                raise UsageError(
                    f"the HuggingFace datasets server returned {error.code}: {error.reason}",
                    fix="Check the dataset name and split, then retry.",
                ) from error
            time.sleep(RATE_LIMIT_BACKOFF_S * (attempt + 1))
        except urllib.error.URLError as error:
            raise UsageError(
                f"could not reach the HuggingFace datasets server: {error}",
                fix="Check network access on the host, then retry.",
            ) from error
    else:
        raise UsageError(
            f"the HuggingFace datasets server kept rate-limiting us: {last}",
            fix=f"Wait a minute and retry; it allows a burst then throttles.",
        )

    rows = payload.get("rows") or []
    return [item.get("row", {}) for item in rows]


def find_instance(instance_id: str, *, progress: Any = None) -> dict[str, Any]:
    """Page the dataset until the instance turns up."""
    offset = 0
    while offset < TOTAL_ROWS_GUESS + PAGE_SIZE:
        rows = fetch_rows(offset)
        if not rows:
            break
        for row in rows:
            if row.get("instance_id") == instance_id:
                return row
        if progress:
            progress(offset + len(rows))
        offset += len(rows)
    raise UsageError(
        f"no instance {instance_id!r} in {DATASET}.",
        fix="Check the id against the dataset viewer at "
        f"https://huggingface.co/datasets/{DATASET}",
    )


def survey(limit: int = TOTAL_ROWS_GUESS, *, progress: Any = None) -> list[dict[str, Any]]:
    """Summarize instances, cheapest-looking first. An authoring aid."""
    summaries: list[dict[str, Any]] = []
    offset = 0
    while offset < limit:
        rows = fetch_rows(offset, min(PAGE_SIZE, limit - offset))
        if not rows:
            break
        for row in rows:
            summaries.append(
                {
                    "instance_id": row.get("instance_id", ""),
                    "repo": row.get("repo", ""),
                    "language": row.get("repo_language", ""),
                    "f2p": len(decode_selector_list(row.get("fail_to_pass"))),
                    "p2p": len(decode_selector_list(row.get("pass_to_pass"))),
                    "patch_bytes": len(row.get("patch") or ""),
                    "dockerhub_tag": row.get("dockerhub_tag", ""),
                }
            )
        if progress:
            progress(offset + len(rows))
        offset += len(rows)
    return summaries


def build_task_json(row: dict[str, Any], *, repo_path_in_image: str) -> dict[str, Any]:
    """The task.json for one instance."""
    instance_id = str(row.get("instance_id", ""))
    language = _LANGUAGE_MAP.get(str(row.get("repo_language", "")).lower(), "python")
    framework = _FRAMEWORK_BY_LANGUAGE[language]
    test_patch = str(row.get("test_patch") or "")

    fail_to_pass = decode_selector_list(row.get("fail_to_pass"))
    pass_to_pass = decode_selector_list(row.get("pass_to_pass"))
    # A selector in both buckets fails schema validation, and the dataset does
    # occasionally overlap them. f2p wins: it is the one that proves a fix.
    pass_to_pass = [s for s in pass_to_pass if s not in set(fail_to_pass)]

    tag = str(row.get("dockerhub_tag") or "")
    return {
        "task_id": sanitize_task_id(instance_id),
        # An owner/name, not a URL -- the dataset stores `NodeBB/NodeBB`.
        "repo": f"https://github.com/{row.get('repo')}" if row.get("repo") else None,
        "base_commit": str(row.get("base_commit") or "") or None,
        "language": language,
        "environment": {
            # Used verbatim: the tag is truncated to 128 chars and is NOT
            # derivable from instance_id.
            "image": f"{IMAGE_REPOSITORY}:{tag}",
            "platform": PLATFORM,
            "repo_path_in_image": repo_path_in_image,
        },
        "tests": {
            "framework": framework,
            "run_cmd_template": _RUN_TEMPLATE[framework],
            "fail_to_pass": fail_to_pass,
            "pass_to_pass": pass_to_pass,
            "test_path_globs": build_test_path_globs(row, test_patch),
            "timeout_s": DEFAULT_TIMEOUT_S,
        },
        "source": {
            "dataset": DATASET,
            "instance_id": instance_id,
            "dockerhub_tag": tag,
        },
    }


def write_bundle(
    row: dict[str, Any], out_dir: Path, *, repo_path_in_image: str = "/app"
) -> ImportedBundle:
    """Write a bundle directory for one instance."""
    task_json = build_task_json(row, repo_path_in_image=repo_path_in_image)
    bundle_dir = out_dir / task_json["task_id"]
    bundle_dir.mkdir(parents=True, exist_ok=True)

    (bundle_dir / TASK_JSON).write_text(json.dumps(task_json, indent=2) + "\n")
    (bundle_dir / DESCRIPTION_MD).write_text(build_description(row))
    (bundle_dir / PATCH_DIFF).write_text(_ensure_trailing_newline(row.get("patch") or ""))
    (bundle_dir / TEST_PATCH_DIFF).write_text(
        _ensure_trailing_newline(row.get("test_patch") or "")
    )

    return ImportedBundle(
        path=bundle_dir,
        task_id=task_json["task_id"],
        instance_id=str(row.get("instance_id", "")),
        language=task_json["language"],
        fail_to_pass=len(task_json["tests"]["fail_to_pass"]),
        pass_to_pass=len(task_json["tests"]["pass_to_pass"]),
        image=task_json["environment"]["image"],
    )


def _ensure_trailing_newline(text: str) -> str:
    """git apply rejects a patch whose final line has no newline."""
    return text if text.endswith("\n") else text + "\n"
