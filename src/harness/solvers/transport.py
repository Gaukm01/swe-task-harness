"""Transports: live API, cassette replay, and a stub for tests.

The agent loop never touches the Anthropic SDK directly. It calls
`Transport.send(turn, request)` and gets a message back. Three implementations
exist, and the loop cannot tell them apart.

That boundary is what makes offline iteration affordable. The API key is
rate-limited to a couple of real runs, so one live run records every exchange to
`runs/<id>/llm/NNN.json`, and everything downstream -- parsing, grading,
reporting, the UI -- is then iterated against the recording for free. It is
also a genuine reproducibility artifact: the committed example run can be
re-graded by someone with no key at all.

Replay is keyed by turn index and verifies the request it was given matches the
one that was recorded. A divergence fails loudly rather than quietly serving a
stale response, because a silently-wrong replay is worse than no replay.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from harness.core.errors import SolverFailedError

# Cassette filenames are zero-padded so `ls` orders them like the conversation.
CASSETTE_PATTERN = "{index:03d}.json"

# Anthropic list pricing, USD per million tokens (cached 2026-06-24).
# Used only to report what a run cost; it never gates anything except the
# optional --max-cost-usd ceiling.
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-fable-5": (10.00, 50.00),
}

# Cache writes cost ~1.25x input; cache reads ~0.1x.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10


@dataclass
class Usage:
    """Token accounting for one exchange."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def cost_usd(self, model: str) -> float:
        """What this exchange cost, at list price."""
        rates = PRICING.get(model)
        if rates is None:
            # An unknown model is a pricing gap, not an error. Report zero and
            # let --max-cost-usd be the thing that notices.
            return 0.0
        input_rate, output_rate = rates
        million = 1_000_000
        return (
            self.input_tokens * input_rate
            + self.cache_creation_input_tokens * input_rate * CACHE_WRITE_MULTIPLIER
            + self.cache_read_input_tokens * input_rate * CACHE_READ_MULTIPLIER
            + self.output_tokens * output_rate
        ) / million


@dataclass
class Exchange:
    """One request/response pair, as stored in a cassette."""

    turn: int
    request: dict[str, Any]
    response: dict[str, Any]

    @property
    def usage(self) -> Usage:
        raw = self.response.get("usage") or {}
        return Usage(
            input_tokens=int(raw.get("input_tokens") or 0),
            output_tokens=int(raw.get("output_tokens") or 0),
            cache_creation_input_tokens=int(raw.get("cache_creation_input_tokens") or 0),
            cache_read_input_tokens=int(raw.get("cache_read_input_tokens") or 0),
        )

    @property
    def stop_reason(self) -> str | None:
        reason = self.response.get("stop_reason")
        return str(reason) if reason is not None else None

    @property
    def content(self) -> list[dict[str, Any]]:
        blocks = self.response.get("content") or []
        return [b for b in blocks if isinstance(b, dict)]


class Transport(Protocol):
    """Something that can answer a Messages API request."""

    def send(self, turn: int, request: dict[str, Any]) -> Exchange:
        """Return the exchange for this turn."""
        ...


def _request_fingerprint(request: dict[str, Any]) -> str:
    """A stable summary of a request, for divergence detection.

    Compares the parts that determine the response -- model, system prompt,
    tool names, and the message sequence -- rather than the raw dict, so that
    an irrelevant addition (a new sampling knob, a cache breakpoint moving)
    does not read as a divergence.
    """
    messages = request.get("messages") or []
    shape = {
        "model": request.get("model"),
        "system": _text_of(request.get("system")),
        "tools": sorted(t.get("name", "") for t in (request.get("tools") or [])),
        "messages": [
            {"role": m.get("role"), "blocks": _block_shapes(m.get("content"))} for m in messages
        ],
    }
    return json.dumps(shape, sort_keys=True, default=str)


def _text_of(system: Any) -> str:
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "".join(block.get("text", "") for block in system if isinstance(block, dict))
    return ""


def _block_shapes(content: Any) -> list[str]:
    """Describe a message's blocks in a form that survives re-encoding.

    A plain string and a list holding exactly one text block are the same
    message; only the encoding differs. That distinction matters because the
    prompt-cache breakpoint rewrites string content into a one-block list to
    attach `cache_control`, which silently invalidated every cassette recorded
    before that change -- including the flagship agent run. Divergence detection
    must fire on a changed *prompt*, never on a changed representation of the
    same prompt.

    Only the single-block case is collapsed. Inside a multi-block list (an
    assistant turn of thinking + text + tool_use) the bare type name is kept,
    because that is what recordings contain and widening it would break them
    the other way.
    """
    if isinstance(content, str):
        return [f"text:{len(content)}"]
    if not isinstance(content, list):
        return []

    if len(content) == 1:
        only = content[0]
        as_dict = only if isinstance(only, dict) else None
        kind = str(as_dict.get("type", "?")) if as_dict else str(getattr(only, "type", "?"))
        if kind == "text":
            text = as_dict.get("text", "") if as_dict else getattr(only, "text", "")
            return [f"text:{len(text)}"]

    shapes = []
    for block in content:
        if isinstance(block, dict):
            shapes.append(str(block.get("type", "?")))
        else:
            shapes.append(str(getattr(block, "type", "?")))
    return shapes


class CassetteWriter:
    """Persists exchanges to `runs/<id>/llm/NNN.json`."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)

    def write(self, exchange: Exchange) -> Path:
        path = self.directory / CASSETTE_PATTERN.format(index=exchange.turn)
        path.write_text(
            json.dumps(
                {
                    "turn": exchange.turn,
                    "fingerprint": _request_fingerprint(exchange.request),
                    "request": exchange.request,
                    "response": exchange.response,
                },
                indent=2,
                default=str,
            )
            + "\n"
        )
        return path


class AnthropicTransport:
    """The live transport. The only thing in the harness that spends money."""

    def __init__(self, *, api_key: str | None = None, cassettes: CassetteWriter | None = None):
        try:
            import anthropic
        except ImportError as error:  # pragma: no cover - dependency is declared
            raise SolverFailedError(
                "the `anthropic` package is not installed.",
                fix="Run `uv sync` to install it.",
            ) from error

        self._anthropic = anthropic

        # The SDK defers auth resolution to request time and then raises a bare
        # TypeError. Checking here turns that into a typed error with a fix,
        # before a container has been built.
        from harness.core.env import api_key as configured_key

        if not (api_key or configured_key()):
            raise SolverFailedError(
                "no Anthropic credentials are configured.",
                fix="Put ANTHROPIC_API_KEY in .env (see .env.example), or export it. "
                "`task doctor` shows what it found.",
            )

        try:
            self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        except Exception as error:  # noqa: BLE001 - surfaced as a typed harness error
            raise SolverFailedError(
                f"could not create an Anthropic client: {error}",
                fix="Set ANTHROPIC_API_KEY, or run `ant auth login`. `task doctor` reports it.",
            ) from error
        self.cassettes = cassettes

    def send(self, turn: int, request: dict[str, Any]) -> Exchange:
        anthropic = self._anthropic
        try:
            message = self.client.messages.create(**request)
        except anthropic.RateLimitError as error:
            raise SolverFailedError(
                "the Anthropic API rate-limited this run.",
                fix="Wait and re-run, or replay a recorded run with `--solver replay:<run_id>`.",
            ) from error
        except anthropic.AuthenticationError as error:
            raise SolverFailedError(
                "the Anthropic API rejected the credentials.",
                fix="Check ANTHROPIC_API_KEY, or run `ant auth login`.",
            ) from error
        except anthropic.APIStatusError as error:
            raise SolverFailedError(
                f"the Anthropic API returned {error.status_code}: {error.message}",
                fix="Re-run; if it persists, check the model id and request shape.",
            ) from error
        except TypeError as error:
            raise SolverFailedError(
                f"the Anthropic client could not authenticate: {error}",
                fix="Set ANTHROPIC_API_KEY in .env, then run `task doctor --check-api`.",
            ) from error
        except anthropic.APIConnectionError as error:
            raise SolverFailedError(
                f"could not reach the Anthropic API: {error}",
                fix="Check network access on the host. The container has none by design.",
            ) from error

        exchange = Exchange(turn=turn, request=request, response=message.model_dump(mode="json"))
        if self.cassettes:
            # Written before the loop acts on it, so a crash mid-turn still
            # leaves a replayable recording of everything up to that point.
            self.cassettes.write(exchange)
        return exchange


@dataclass
class ReplayTransport:
    """Replays a recorded run. Zero API calls, zero cost."""

    directory: Path
    strict: bool = True
    _loaded: dict[int, dict[str, Any]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if not self.directory.is_dir():
            raise SolverFailedError(
                f"no cassettes at {self.directory}.",
                fix="Replay needs a recorded run. List runs with `task runs`.",
            )
        for path in sorted(self.directory.glob("*.json")):
            payload = json.loads(path.read_text())
            self._loaded[int(payload["turn"])] = payload
        if not self._loaded:
            raise SolverFailedError(
                f"{self.directory} contains no cassettes.",
                fix="The recorded run stored no exchanges. Re-record it.",
            )

    @property
    def turns(self) -> int:
        return len(self._loaded)

    def send(self, turn: int, request: dict[str, Any]) -> Exchange:
        payload = self._loaded.get(turn)
        if payload is None:
            raise SolverFailedError(
                f"replay ran out of cassettes at turn {turn} (recorded {self.turns}).",
                fix="The loop diverged from the recording -- it asked for more turns than "
                "were recorded. Re-record the run.",
            )

        if self.strict:
            recorded = payload.get("fingerprint")
            current = _request_fingerprint(request)
            if recorded is not None and recorded != current:
                # Loud, not silent. A replay that quietly serves a stale
                # response would make every downstream artifact a fiction.
                raise SolverFailedError(
                    f"replay diverged from the recording at turn {turn}.",
                    fix="The prompt or tool set changed since this run was recorded, so the "
                    "responses no longer correspond. Re-record it with `--solver agent`.",
                )

        return Exchange(turn=turn, request=request, response=payload["response"])


@dataclass
class StubTransport:
    """A scripted transport for tests. Never calls the API."""

    responses: list[dict[str, Any]]
    seen: list[dict[str, Any]] = field(default_factory=list)

    def send(self, turn: int, request: dict[str, Any]) -> Exchange:
        self.seen.append(request)
        if turn >= len(self.responses):
            raise AssertionError(f"StubTransport has no response for turn {turn}")
        return Exchange(turn=turn, request=request, response=self.responses[turn])


def text_response(text: str, **usage: int) -> dict[str, Any]:
    """A scripted assistant turn with no tool calls."""
    return {
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5, **usage},
        "model": "stub",
    }


def tool_response(name: str, arguments: dict[str, Any], *, block_id: str = "tu_1") -> dict[str, Any]:
    """A scripted assistant turn that calls one tool."""
    return {
        "content": [{"type": "tool_use", "id": block_id, "name": name, "input": arguments}],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "model": "stub",
    }
