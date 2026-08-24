"""The LLM solver: a manual tool-use loop over the Messages API.

The loop runs on the **host**; the code lives in the **container**; the boundary
between them is `docker exec`. That is the isolation model. The agent needs the
Anthropic API and the workspace must have no egress, so the network boundary
sits between the loop and the container rather than around both.

A manual loop rather than the SDK's tool runner: every request/response pair
has to be recorded to a cassette, which needs a transport seam the runner does
not expose.

What the agent is told: `description.md` and a shallow file tree. What it is
never told: any selector name, the test patch, the gold patch, or that a
guardrail suite exists at all.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from harness.core.bundle import Bundle
from harness.solvers.base import SolverContext, SolverResult
from harness.solvers.tools import (
    TOOL_DEFINITIONS,
    ToolBox,
    ToolCallRecord,
    event_payload,
    tool_result_block,
)
from harness.solvers.transport import Transport, Usage

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TURNS = 75
MAX_TOKENS = 16_000

SYSTEM_PROMPT = """You are a software engineer working in a checked-out repository. \
You have been given a bug report or feature request and must implement the change.

You are working through tools, on a real repository, with no network access. The \
only things that exist are the six tools you have been given: read_file, \
write_file, list_dir, run_bash, run_tests, and done.

How to work:

- Start by orienting yourself. Read the relevant source before changing it; the \
description names the behaviour, not always the file.
- Make the smallest change that actually fixes the problem, in the source, not in \
the tests.
- Verify your work. Run the repository's existing tests, read the failures, and \
improve. A change you have not run is a guess.
- If a test fails, read the traceback before editing again. Repeated blind edits \
are worse than one careful one.
- Preserve existing behaviour. Other tests depend on it, and breaking them counts \
against you.
- write_file replaces a file completely, so pass the whole new contents.
- Call done when you have made the change and verified it as well as you can. \
Summarise what you changed and why.

There is no need to commit anything. Your work is collected from the working tree."""


@dataclass
class AgentOutcome:
    """What the loop did, beyond the changes it made."""

    turns: int = 0
    cost_usd: float = 0.0
    usage: Usage = field(default_factory=Usage)
    stop_reason: str | None = None
    summary: str | None = None
    notes: list[str] = field(default_factory=list)
    calls: list[ToolCallRecord] = field(default_factory=list)


def build_file_tree(context: SolverContext, depth: int = 2) -> str:
    """A shallow tree of the repo, so the first turn is not spent on `ls`."""
    result = context.runtime.exec(
        context.container_id,
        [
            "find",
            context.repo_root,
            "-maxdepth",
            str(depth),
            "-not",
            "-path",
            "*/.git*",
            "-not",
            "-path",
            "*/__pycache__*",
            "-not",
            "-path",
            "*/node_modules*",
        ],
        timeout_s=60,
    )
    if not result.ok:
        return "(could not list the repository)"
    prefix = context.repo_root.rstrip("/") + "/"
    entries = sorted(
        line.replace(prefix, "", 1) for line in result.stdout.splitlines() if line != context.repo_root
    )
    return "\n".join(entries[:300]) or "(empty)"


def build_user_message(bundle: Bundle, file_tree: str, repo_root: str) -> str:
    """description.md plus a file tree. No selector names, ever."""
    return (
        f"{bundle.description.strip()}\n\n"
        f"---\n\n"
        f"The repository is checked out at `{repo_root}`, which is your working "
        f"directory. Its layout, two levels deep:\n\n"
        f"```\n{file_tree}\n```\n"
    )


class AgentSolver:
    """Implements `Solver` by driving a Messages API tool loop."""

    kind = "agent"

    def __init__(
        self,
        transport: Transport,
        *,
        model: str = DEFAULT_MODEL,
        max_turns: int = DEFAULT_MAX_TURNS,
        max_cost_usd: float | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        wall_clock_s: float | None = None,
    ) -> None:
        self.transport = transport
        self.model = model
        self.max_turns = max_turns
        self.max_cost_usd = max_cost_usd
        self.on_event = on_event or (lambda kind, payload: None)
        self.wall_clock_s = wall_clock_s
        self.outcome = AgentOutcome()

    def solve(self, context: SolverContext, bundle: Bundle) -> SolverResult:
        toolbox = ToolBox(
            runtime=context.runtime,
            container_id=context.container_id,
            repo_root=context.repo_root,
            spec=bundle.spec,
        )
        tree = build_file_tree(context)
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": build_user_message(bundle, tree, context.repo_root)}
        ]

        started = time.monotonic()
        outcome = self.outcome

        for turn in range(self.max_turns):
            if self._ceiling_hit(started, turn, outcome):
                break

            request = self._build_request(messages)
            exchange = self.transport.send(turn, request)

            usage = exchange.usage
            outcome.usage.input_tokens += usage.input_tokens
            outcome.usage.output_tokens += usage.output_tokens
            outcome.usage.cache_creation_input_tokens += usage.cache_creation_input_tokens
            outcome.usage.cache_read_input_tokens += usage.cache_read_input_tokens
            outcome.cost_usd += usage.cost_usd(self.model)
            outcome.turns = turn + 1
            outcome.stop_reason = exchange.stop_reason

            self.on_event(
                "assistant_turn",
                {
                    "turn": turn,
                    "stop_reason": exchange.stop_reason,
                    "text": _assistant_text(exchange.content)[:2000],
                    "cost_usd": round(outcome.cost_usd, 6),
                },
            )

            if exchange.stop_reason == "refusal":
                outcome.notes.append("the model declined to answer")
                break

            tool_uses = [b for b in exchange.content if b.get("type") == "tool_use"]
            if not tool_uses:
                # end_turn, max_tokens, or a bare text reply. Nothing left to do.
                if exchange.stop_reason == "max_tokens":
                    outcome.notes.append("a response hit the max_tokens ceiling")
                break

            # The assistant turn goes back verbatim -- thinking blocks included,
            # which the API requires when continuing on the same model.
            messages.append({"role": "assistant", "content": exchange.content})

            results = []
            finished = False
            for block in tool_uses:
                record = toolbox.execute(block.get("name", ""), block.get("input") or {})
                self.on_event("tool_call", {"turn": turn, **event_payload(record)})
                results.append(tool_result_block(record, str(block.get("id"))))
                if record.name == "done":
                    finished = True

            # Every result in one user message. Splitting them across several
            # teaches the model to stop making parallel calls.
            messages.append({"role": "user", "content": results})

            if finished:
                outcome.summary = toolbox.done_summary
                break
        else:
            outcome.notes.append(f"hit the turn ceiling ({self.max_turns} turns)")

        outcome.calls = toolbox.calls
        return SolverResult(
            kind=self.kind,
            model=self.model,
            turns=outcome.turns,
            cost_usd=round(outcome.cost_usd, 6),
            summary=outcome.summary or "the agent stopped without calling done",
            notes=outcome.notes,
        )

    # -- internals --------------------------------------------------------

    def _build_request(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            # The system prompt and tool list never change, so caching them
            # turns N turns of resent prefix into N cache reads.
            "system": [
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "tools": TOOL_DEFINITIONS,
            # ...but the system prompt is the part that does NOT grow. Caching
            # only that cached 1,109 tokens per turn while the conversation --
            # which reached hundreds of thousands of tokens over 37 turns -- was
            # re-sent at full price every single time. Measured on a real run:
            # 914,821 input tokens against 39,924 cache reads. The breakpoint
            # has to move to the END of the conversation so the whole prefix is
            # what gets reused.
            "messages": _with_conversation_cache_breakpoint(messages),
            "thinking": {"type": "adaptive"},
        }

    def _ceiling_hit(self, started: float, turn: int, outcome: AgentOutcome) -> bool:
        """Ceilings end the solve; grading proceeds on whatever diff exists."""
        if self.max_cost_usd is not None and outcome.cost_usd >= self.max_cost_usd:
            outcome.notes.append(
                f"hit the cost ceiling (${outcome.cost_usd:.4f} >= ${self.max_cost_usd:.2f})"
            )
            return True
        if self.wall_clock_s is not None and (time.monotonic() - started) >= self.wall_clock_s:
            outcome.notes.append(f"hit the wall-clock ceiling ({self.wall_clock_s:.0f}s)")
            return True
        return False


def _with_conversation_cache_breakpoint(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Mark the end of the conversation as cacheable.

    Caching is a prefix match, so a breakpoint on the final block makes every
    turn before it reusable. Applied to a shallow copy at request-build time:
    the loop's own `messages` list stays free of cache markers, so breakpoints
    never accumulate and the replay fingerprint (which ignores them) stays
    stable.
    """
    if not messages:
        return messages

    head, last = messages[:-1], dict(messages[-1])
    content = last.get("content")

    if isinstance(content, str):
        last["content"] = [
            {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
        ]
    elif isinstance(content, list) and content:
        blocks = [dict(b) if isinstance(b, dict) else b for b in content]
        if isinstance(blocks[-1], dict):
            blocks[-1]["cache_control"] = {"type": "ephemeral"}
        last["content"] = blocks
    else:
        return messages

    return [*head, last]


def _assistant_text(content: list[dict[str, Any]]) -> str:
    return "\n".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
