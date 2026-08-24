"""The agent's complete tool set, and the jail around it.

Six tools exist. Nothing else does -- there is no web search, no package
install, no way to read outside the repo, because those tools are simply never
defined. That is the containment model: not a policy the model is asked to
respect, but a surface that does not exist.

Two checks are enforced on every call, on the harness side of the boundary:

* **Path jail.** Every path is resolved with `realpath` *inside the container*
  and must land under the repo root. Resolving inside matters -- a symlink
  pointing at `/etc` resolves differently there than on the host, and the
  container's answer is the one that counts.
* **Guardrail refusal.** `run_tests` will not run a selector naming a graded
  test. Refusals are logged as events rather than hidden, so a trajectory shows
  what the agent tried.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from harness.core.bundle import TaskSpec
from harness.core.runtime import ContainerRuntime

# Truncation limits. A tool result that fills the context window is worse than
# useless -- it evicts the code the agent needs to see next.
MAX_READ_BYTES = 60_000
MAX_OUTPUT_CHARS = 20_000
MAX_LIST_ENTRIES = 400

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": (
            "Read a file from the repository. Returns the file's contents with 1-based "
            "line numbers. Use the optional line range for large files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file, relative to the repository root.",
                },
                "start_line": {"type": "integer", "description": "First line to read (1-based)."},
                "end_line": {"type": "integer", "description": "Last line to read, inclusive."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write a file in the repository, creating or overwriting it. You must supply "
            "the file's complete new contents, not a diff or a fragment."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file, relative to the repository root.",
                },
                "content": {"type": "string", "description": "The file's full new contents."},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_dir",
        "description": "List the entries of a directory in the repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path relative to the repository root. "
                    "Use '.' for the root.",
                }
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_bash",
        "description": (
            "Run a shell command in the repository root. There is no network access. "
            "Use this to search the code, inspect the environment, or run tests directly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to run."}
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_tests",
        "description": (
            "Run the repository's existing test suite and return the result. Pass selectors "
            "to narrow it down, or omit them to run everything visible."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "selectors": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Test selectors to run. Omit to run the whole visible suite.",
                }
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "done",
        "description": (
            "Declare the task finished. Call this once you have made the change and "
            "verified it as best you can."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "What you changed and why, in a few sentences.",
                }
            },
            "required": ["summary"],
            "additionalProperties": False,
        },
    },
]


@dataclass
class ToolCallRecord:
    """One tool call, for the events table and the run page."""

    name: str
    arguments: dict[str, Any]
    result: str
    is_error: bool = False
    refused: bool = False


@dataclass
class ToolBox:
    """Executes tool calls against a container, enforcing the jail."""

    runtime: ContainerRuntime
    container_id: str
    repo_root: str
    spec: TaskSpec
    timeout_s: int = 120
    calls: list[ToolCallRecord] = field(default_factory=list)
    done_summary: str | None = None

    # -- the jail ---------------------------------------------------------

    def _resolve(self, path: str) -> tuple[str | None, str | None]:
        """Resolve a path inside the container. Returns (absolute, error)."""
        if not path or path.startswith("-"):
            return None, f"invalid path {path!r}"

        candidate = path if path.startswith("/") else f"{self.repo_root}/{path}"
        # Resolved by the container, not the host: a symlink means whatever the
        # container says it means, and that is the filesystem being written to.
        result = self.runtime.exec(
            self.container_id, ["realpath", "-m", "--", candidate], timeout_s=30
        )
        if not result.ok:
            return None, f"could not resolve {path!r}"

        resolved = result.stdout.strip()
        if resolved != self.repo_root and not resolved.startswith(f"{self.repo_root}/"):
            return None, (
                f"path {path!r} resolves to {resolved}, which is outside the repository "
                f"({self.repo_root}). Only files inside the repository may be accessed."
            )
        return resolved, None

    def _relative(self, absolute: str) -> str:
        return absolute[len(self.repo_root) :].lstrip("/")

    # -- dispatch ---------------------------------------------------------

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolCallRecord:
        handlers = {
            "read_file": self._read_file,
            "write_file": self._write_file,
            "list_dir": self._list_dir,
            "run_bash": self._run_bash,
            "run_tests": self._run_tests,
            "done": self._done,
        }
        handler = handlers.get(name)
        if handler is None:
            record = ToolCallRecord(
                name=name,
                arguments=arguments,
                result=f"no such tool {name!r}. Available: {', '.join(handlers)}",
                is_error=True,
            )
        else:
            try:
                record = handler(arguments)
            except Exception as error:  # noqa: BLE001 - model input is untrusted
                # Tool arguments come from a model and can be any shape at all.
                # A malformed one must cost a single turn and be reported back
                # so the model can correct itself -- never propagate out and
                # kill a run whose container is about to be torn down, losing
                # the diff and the report with it.
                record = ToolCallRecord(
                    name=name,
                    arguments=arguments,
                    result=f"{type(error).__name__}: {error}. Check the argument types.",
                    is_error=True,
                )
        self.calls.append(record)
        return record

    # -- tools ------------------------------------------------------------

    def _read_file(self, arguments: dict[str, Any]) -> ToolCallRecord:
        path = str(arguments.get("path", ""))
        resolved, error = self._resolve(path)
        if error:
            return ToolCallRecord("read_file", arguments, error, is_error=True)
        assert resolved is not None

        result = self.runtime.exec(
            self.container_id, ["cat", "--", resolved], timeout_s=self.timeout_s
        )
        if not result.ok:
            return ToolCallRecord(
                "read_file", arguments, f"could not read {path}: {result.stderr.strip()}", True
            )

        lines = result.stdout.splitlines()
        # Models routinely emit stringified numbers; coerce rather than crash.
        start = max(1, _as_int(arguments.get("start_line"), 1))
        end = _as_int(arguments.get("end_line"), len(lines))
        window = lines[start - 1 : end]
        numbered = "\n".join(f"{start + i:6d}  {line}" for i, line in enumerate(window))
        return ToolCallRecord("read_file", arguments, _truncate(numbered, MAX_READ_BYTES))

    def _write_file(self, arguments: dict[str, Any]) -> ToolCallRecord:
        path = str(arguments.get("path", ""))
        content = arguments.get("content")
        if not isinstance(content, str):
            return ToolCallRecord("write_file", arguments, "content must be a string", True)

        resolved, error = self._resolve(path)
        if error:
            return ToolCallRecord("write_file", arguments, error, is_error=True)
        assert resolved is not None

        relative = self._relative(resolved)
        if self._is_guardrail_path(relative):
            # Force-restore would undo this anyway; refusing here says so out
            # loud instead of letting the agent believe it worked.
            message = (
                f"refused: {relative} is a test file. Tests are restored before grading, "
                "so editing them cannot help. Change the source instead."
            )
            return ToolCallRecord("write_file", arguments, message, True, refused=True)

        parent = resolved.rsplit("/", 1)[0]
        self.runtime.exec(self.container_id, ["mkdir", "-p", parent], timeout_s=30)
        self.runtime.write_file(self.container_id, resolved, content)
        return ToolCallRecord(
            "write_file", arguments, f"wrote {len(content)} bytes to {relative}"
        )

    def _list_dir(self, arguments: dict[str, Any]) -> ToolCallRecord:
        path = str(arguments.get("path", "."))
        resolved, error = self._resolve(path)
        if error:
            return ToolCallRecord("list_dir", arguments, error, is_error=True)
        assert resolved is not None

        result = self.runtime.exec(
            self.container_id, ["ls", "-1Ap", "--", resolved], timeout_s=self.timeout_s
        )
        if not result.ok:
            return ToolCallRecord(
                "list_dir", arguments, f"could not list {path}: {result.stderr.strip()}", True
            )
        entries = result.stdout.splitlines()[:MAX_LIST_ENTRIES]
        return ToolCallRecord("list_dir", arguments, "\n".join(entries) or "(empty)")

    def _run_bash(self, arguments: dict[str, Any]) -> ToolCallRecord:
        command = str(arguments.get("command", "")).strip()
        if not command:
            return ToolCallRecord("run_bash", arguments, "command must not be empty", True)

        # One argv element. The host shell never sees this string; only the
        # container's bash does, and that container has no network and is
        # thrown away afterwards.
        result = self.runtime.exec(
            self.container_id,
            ["bash", "-lc", command],
            workdir=self.repo_root,
            timeout_s=self.timeout_s,
        )
        if result.timed_out:
            return ToolCallRecord(
                "run_bash", arguments, f"command timed out after {self.timeout_s}s", True
            )
        body = _combine(result.stdout, result.stderr)
        return ToolCallRecord(
            "run_bash",
            arguments,
            f"exit {result.exit_code}\n{_truncate(body, MAX_OUTPUT_CHARS)}",
            is_error=result.exit_code != 0,
        )

    def _run_tests(self, arguments: dict[str, Any]) -> ToolCallRecord:
        raw = arguments.get("selectors") or []
        selectors = [str(s) for s in raw] if isinstance(raw, list) else []

        refused = [s for s in selectors if self._is_guardrail_selector(s)]
        if refused:
            message = (
                "refused: "
                + ", ".join(refused)
                + " name graded tests, which are not visible during solving. Run the "
                "repository's own tests instead."
            )
            return ToolCallRecord("run_tests", arguments, message, True, refused=True)

        argv = ["python", "-m", "pytest", "-q", "--no-header", *selectors]
        result = self.runtime.exec(
            self.container_id, argv, workdir=self.repo_root, timeout_s=self.spec.tests.timeout_s
        )
        if result.timed_out:
            return ToolCallRecord("run_tests", arguments, "the test run timed out", True)
        body = _combine(result.stdout, result.stderr)
        return ToolCallRecord(
            "run_tests",
            arguments,
            f"exit {result.exit_code}\n{_truncate(body, MAX_OUTPUT_CHARS)}",
            is_error=result.exit_code != 0,
        )

    def _done(self, arguments: dict[str, Any]) -> ToolCallRecord:
        summary = str(arguments.get("summary", "")).strip()
        self.done_summary = summary or "(no summary given)"
        return ToolCallRecord("done", arguments, "acknowledged")

    # -- guardrail knowledge ----------------------------------------------

    def _is_guardrail_path(self, relative: str) -> bool:
        from harness.core.gaming import is_test_infrastructure
        from harness.core.globs import matches_any

        return matches_any(relative, self.spec.tests.test_path_globs) or is_test_infrastructure(
            relative
        )

    def _is_guardrail_selector(self, selector: str) -> bool:
        """True when a selector names a graded test.

        Matched against the exact node IDs, not against whole files. The p2p
        tests genuinely live in the repo and the agent should be able to run
        them -- blocking their file would block the visible suite. What is
        refused is the harness confirming *which* ids are graded.
        """
        return selector.strip() in set(self.spec.tests.selectors)


def _as_int(value: object, default: int) -> int:
    """Coerce a model-supplied number, falling back rather than raising."""
    if value is None or value == "":
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return default


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    dropped = len(text) - limit
    return text[:limit] + f"\n... [{dropped} characters truncated]"


def _combine(stdout: str, stderr: str) -> str:
    parts = [p for p in (stdout.strip(), stderr.strip()) if p]
    return "\n".join(parts) or "(no output)"


def tool_result_block(record: ToolCallRecord, tool_use_id: str) -> dict[str, Any]:
    """The `tool_result` content block for one executed call."""
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": record.result,
    }
    if record.is_error:
        block["is_error"] = True
    return block


def event_payload(record: ToolCallRecord) -> dict[str, Any]:
    """A compact, storable view of one tool call."""
    return {
        "tool": record.name,
        "arguments": _shrink(record.arguments),
        "result": _truncate(record.result, 2000),
        "is_error": record.is_error,
        "refused": record.refused,
    }


def _shrink(arguments: dict[str, Any]) -> dict[str, Any]:
    """Keep argument previews small -- write_file content can be enormous."""
    out: dict[str, Any] = {}
    for key, value in arguments.items():
        if isinstance(value, str) and len(value) > 500:
            out[key] = value[:500] + f"... [{len(value) - 500} more]"
        else:
            out[key] = value
    return out
