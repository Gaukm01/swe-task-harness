"""The agent loop against a stub transport. Never calls the API."""

from __future__ import annotations

import json

import pytest

from harness.core.bundle import load_bundle
from harness.core.errors import SolverFailedError
from harness.solvers import resolve_solver
from harness.solvers.agent import SYSTEM_PROMPT, AgentSolver, build_user_message
from harness.solvers.base import SolverContext
from harness.solvers.tools import ToolBox
from harness.solvers.transport import (
    CassetteWriter,
    ReplayTransport,
    StubTransport,
    Usage,
    text_response,
    tool_response,
)
from harness.runtime.fake import FakeRuntime, argv_contains


@pytest.fixture
def bundle(tiny_fixture):
    return load_bundle(tiny_fixture)


@pytest.fixture
def runtime():
    r = FakeRuntime()
    # realpath: echo the argument back, so the jail sees a plausible resolution.
    return r


def make_context(runtime, root="/workspace/repo"):
    return SolverContext(
        runtime=runtime, container_id="c1", repo_root=root, description="d", timeout_s=60
    )


def script_realpath(runtime, resolved):
    runtime.script(argv_contains("realpath"), stdout=resolved)


# -- the prompt leaks nothing ---------------------------------------------


def test_the_prompt_never_contains_a_selector_name(bundle):
    message = build_user_message(bundle, "tinylib/\ntests/", "/workspace/repo")
    combined = SYSTEM_PROMPT + message
    for selector in bundle.spec.tests.selectors:
        assert selector not in combined
        assert selector.split("::")[-1] not in combined


def test_the_prompt_never_contains_the_patches(bundle):
    message = build_user_message(bundle, "tree", "/workspace/repo")
    assert bundle.patch not in message
    assert bundle.test_patch not in message
    assert message.startswith(bundle.description.strip()[:40])


# -- the path jail ---------------------------------------------------------


def test_reading_outside_the_repo_is_refused(runtime, bundle):
    script_realpath(runtime, "/etc/passwd")
    box = ToolBox(runtime, "c1", "/workspace/repo", bundle.spec)
    record = box.execute("read_file", {"path": "../../etc/passwd"})
    assert record.is_error
    assert "outside the repository" in record.result


def test_a_symlink_escape_is_refused(runtime, bundle):
    # The container resolves it, and the container's answer is what counts.
    script_realpath(runtime, "/root/.ssh/id_rsa")
    box = ToolBox(runtime, "c1", "/workspace/repo", bundle.spec)
    record = box.execute("read_file", {"path": "innocent-looking-link"})
    assert record.is_error
    assert "outside the repository" in record.result


def test_writing_a_test_file_is_refused(runtime, bundle):
    script_realpath(runtime, "/workspace/repo/tests/test_intervals.py")
    box = ToolBox(runtime, "c1", "/workspace/repo", bundle.spec)
    record = box.execute("write_file", {"path": "tests/test_intervals.py", "content": "pass"})
    assert record.refused
    assert runtime.files_written == {}


def test_writing_a_conftest_is_refused(runtime, bundle):
    script_realpath(runtime, "/workspace/repo/conftest.py")
    box = ToolBox(runtime, "c1", "/workspace/repo", bundle.spec)
    record = box.execute("write_file", {"path": "conftest.py", "content": "x"})
    assert record.refused


def test_writing_source_is_allowed(runtime, bundle):
    script_realpath(runtime, "/workspace/repo/tinylib/intervals.py")
    box = ToolBox(runtime, "c1", "/workspace/repo", bundle.spec)
    record = box.execute("write_file", {"path": "tinylib/intervals.py", "content": "code"})
    assert not record.is_error
    assert runtime.files_written["/workspace/repo/tinylib/intervals.py"] == "code"


def test_running_a_graded_selector_answers_no_differently(runtime, bundle):
    """Refusing a graded selector was an oracle.

    An agent probing one id at a time and watching for "refused" could
    enumerate the entire graded set -- confirming precisely what the refusal
    existed to hide. It protected nothing either: fail-to-pass tests are not in
    the SOLVE container at all, so the only selectors it could ever match were
    the pass-to-pass ones, which are ordinary visible repo tests runnable
    through `run_bash` anyway.

    So it runs, like any other selector. The harness records the attempt; the
    model learns nothing from it.
    """
    box = ToolBox(runtime, "c1", "/workspace/repo", bundle.spec)
    graded = bundle.spec.tests.fail_to_pass[0]
    record = box.execute("run_tests", {"selectors": [graded]})

    assert not record.refused
    assert runtime.ran("pytest"), "a graded selector must run like any other"
    assert "refused" not in record.result
    # Recorded for the trajectory, not reflected back to the model.
    assert record.probed_graded


def test_an_ungraded_selector_is_indistinguishable_from_a_graded_one(runtime, bundle):
    box = ToolBox(runtime, "c1", "/workspace/repo", bundle.spec)
    graded = box.execute("run_tests", {"selectors": [bundle.spec.tests.pass_to_pass[0]]})
    ungraded = box.execute("run_tests", {"selectors": ["tests/test_intervals.py::test_other"]})
    # Same shape of answer: nothing in the reply tells the two apart.
    assert graded.is_error == ungraded.is_error
    assert graded.result == ungraded.result


def test_running_the_visible_suite_is_allowed(runtime, bundle):
    box = ToolBox(runtime, "c1", "/workspace/repo", bundle.spec)
    record = box.execute("run_tests", {"selectors": ["tests/test_intervals.py"]})
    assert not record.refused
    assert runtime.ran("pytest")


def test_an_unknown_tool_is_an_error_not_a_crash(runtime, bundle):
    box = ToolBox(runtime, "c1", "/workspace/repo", bundle.spec)
    record = box.execute("exfiltrate", {})
    assert record.is_error
    assert "no such tool" in record.result


# -- the loop --------------------------------------------------------------


def test_the_loop_runs_tools_then_stops_on_done(runtime, bundle):
    script_realpath(runtime, "/workspace/repo/tinylib/intervals.py")
    transport = StubTransport(
        [
            tool_response("write_file", {"path": "tinylib/intervals.py", "content": "fixed"}),
            tool_response("done", {"summary": "fixed the merge bug"}, block_id="tu_2"),
        ]
    )
    solver = AgentSolver(transport, model="claude-sonnet-4-6")
    result = solver.solve(make_context(runtime), bundle)

    assert result.turns == 2
    assert result.summary == "fixed the merge bug"
    assert runtime.files_written["/workspace/repo/tinylib/intervals.py"] == "fixed"


def test_the_loop_stops_when_the_model_stops_calling_tools(runtime, bundle):
    transport = StubTransport([text_response("I think it is fine as is.")])
    result = AgentSolver(transport).solve(make_context(runtime), bundle)
    assert result.turns == 1


def test_the_turn_ceiling_is_enforced(runtime, bundle):
    transport = StubTransport([tool_response("list_dir", {"path": "."})] * 10)
    script_realpath(runtime, "/workspace/repo")
    result = AgentSolver(transport, max_turns=3).solve(make_context(runtime), bundle)
    assert result.turns == 3
    assert any("turn ceiling" in note for note in result.notes)


def test_the_cost_ceiling_is_enforced(runtime, bundle):
    expensive = tool_response("list_dir", {"path": "."})
    expensive["usage"] = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    script_realpath(runtime, "/workspace/repo")
    result = AgentSolver(
        StubTransport([expensive] * 5), model="claude-sonnet-4-6", max_cost_usd=1.0
    ).solve(make_context(runtime), bundle)
    assert result.turns < 5
    assert any("cost ceiling" in note for note in result.notes)


def test_tool_results_go_back_in_one_user_message(runtime, bundle):
    script_realpath(runtime, "/workspace/repo")
    transport = StubTransport(
        [tool_response("list_dir", {"path": "."}), text_response("done looking")]
    )
    AgentSolver(transport).solve(make_context(runtime), bundle)

    second_request = transport.seen[1]
    trailing = second_request["messages"][-1]
    assert trailing["role"] == "user"
    assert all(block["type"] == "tool_result" for block in trailing["content"])


def test_events_are_emitted_for_every_turn_and_call(runtime, bundle):
    script_realpath(runtime, "/workspace/repo")
    seen = []
    transport = StubTransport(
        [tool_response("list_dir", {"path": "."}), text_response("ok")]
    )
    AgentSolver(transport, on_event=lambda k, p: seen.append((k, p))).solve(
        make_context(runtime), bundle
    )
    kinds = [k for k, _ in seen]
    assert "assistant_turn" in kinds
    assert "tool_call" in kinds


# -- cost accounting -------------------------------------------------------


def test_cost_uses_list_pricing():
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert usage.cost_usd("claude-sonnet-4-6") == pytest.approx(18.0)
    assert usage.cost_usd("claude-opus-5") == pytest.approx(30.0)


def test_cache_reads_are_cheaper_than_fresh_input():
    fresh = Usage(input_tokens=1_000_000).cost_usd("claude-sonnet-4-6")
    cached = Usage(cache_read_input_tokens=1_000_000).cost_usd("claude-sonnet-4-6")
    assert cached < fresh


def test_an_unknown_model_reports_zero_rather_than_crashing():
    assert Usage(input_tokens=10).cost_usd("some-future-model") == 0.0


# -- cassettes and replay --------------------------------------------------


def test_a_recording_replays_to_the_same_result(runtime, bundle, tmp_path):
    script_realpath(runtime, "/workspace/repo/tinylib/intervals.py")
    responses = [
        tool_response("write_file", {"path": "tinylib/intervals.py", "content": "fixed"}),
        tool_response("done", {"summary": "did the thing"}, block_id="tu_2"),
    ]

    # Record.
    writer = CassetteWriter(tmp_path / "llm")
    recording = StubTransport(responses)
    original_send = recording.send

    def send_and_record(turn, request):
        exchange = original_send(turn, request)
        writer.write(exchange)
        return exchange

    recording.send = send_and_record  # type: ignore[method-assign]
    first = AgentSolver(recording, model="claude-sonnet-4-6").solve(make_context(runtime), bundle)

    # Replay, offline.
    replayed = AgentSolver(
        ReplayTransport(tmp_path / "llm"), model="claude-sonnet-4-6"
    ).solve(make_context(FakeRuntime()), bundle)

    assert replayed.turns == first.turns
    assert replayed.summary == first.summary
    assert replayed.cost_usd == first.cost_usd


def test_replay_fails_loudly_when_the_prompt_changed(runtime, bundle, tmp_path):
    writer = CassetteWriter(tmp_path / "llm")
    exchange_dir = tmp_path / "llm"
    from harness.solvers.transport import Exchange

    writer.write(
        Exchange(
            turn=0,
            request={"model": "m", "system": "OLD PROMPT", "tools": [], "messages": []},
            response=text_response("hi"),
        )
    )
    transport = ReplayTransport(exchange_dir)
    with pytest.raises(SolverFailedError, match="diverged"):
        transport.send(0, {"model": "m", "system": "NEW PROMPT", "tools": [], "messages": []})


def test_replay_without_a_recording_is_a_clear_error(tmp_path):
    with pytest.raises(SolverFailedError, match="no cassettes"):
        ReplayTransport(tmp_path / "nope")


def test_cassettes_are_ordered_by_turn(tmp_path):
    from harness.solvers.transport import Exchange

    writer = CassetteWriter(tmp_path)
    for turn in range(12):
        writer.write(Exchange(turn=turn, request={}, response=text_response("x")))
    names = sorted(p.name for p in tmp_path.glob("*.json"))
    # Zero-padded, so lexical order is conversation order.
    assert names[0] == "000.json"
    assert names[-1] == "011.json"
    assert json.loads((tmp_path / "011.json").read_text())["turn"] == 11


# -- solver resolution -----------------------------------------------------


def test_replay_needs_a_run_id():
    from harness.core.errors import UsageError

    with pytest.raises(UsageError, match="needs a run id"):
        resolve_solver("replay:")


def test_reading_a_large_file_says_how_to_read_the_rest(runtime, bundle):
    """A bare "[N characters truncated]" is why a run once died without an edit.

    The agent re-read the head of a 2,700-line file six times because nothing
    told it the file's size or that ranges existed.
    """
    script_realpath(runtime, "/workspace/repo/big.py")
    runtime.script(
        argv_contains("cat"), stdout="\n".join(f"line {i} " + "x" * 200 for i in range(4000))
    )
    box = ToolBox(runtime, "c1", "/workspace/repo", bundle.spec)
    record = box.execute("read_file", {"path": "big.py"})

    assert "4000 lines total" in record.result
    assert "start_line=" in record.result
    assert "grep -n" in record.result


def test_a_small_file_just_reports_its_size(runtime, bundle):
    script_realpath(runtime, "/workspace/repo/small.py")
    runtime.script(argv_contains("cat"), stdout="a = 1\nb = 2\n")
    box = ToolBox(runtime, "c1", "/workspace/repo", bundle.spec)
    record = box.execute("read_file", {"path": "small.py"})
    assert "small.py: 2 lines" in record.result
    assert "too large" not in record.result


def test_the_conversation_prefix_is_cached_not_just_the_system_prompt(runtime, bundle):
    """Measured on a real run: 914,821 input tokens against 39,924 cache reads.

    A breakpoint on the system prompt alone caches the one part that never
    grows. The conversation has to carry the breakpoint for the prefix to be
    reusable.
    """
    script_realpath(runtime, "/workspace/repo")
    transport = StubTransport(
        [tool_response("list_dir", {"path": "."}), text_response("done")]
    )
    AgentSolver(transport).solve(make_context(runtime), bundle)

    for request in transport.seen:
        last = request["messages"][-1]
        assert isinstance(last["content"], list), "the final block must be markable"
        assert last["content"][-1].get("cache_control"), "no breakpoint on the conversation"


def test_cache_breakpoints_do_not_accumulate_in_the_loops_history(runtime, bundle):
    # Applied to a copy at request time; otherwise every turn adds a marker and
    # the request eventually exceeds the four-breakpoint limit.
    script_realpath(runtime, "/workspace/repo")
    transport = StubTransport(
        [tool_response("list_dir", {"path": "."})] * 3 + [text_response("done")]
    )
    AgentSolver(transport).solve(make_context(runtime), bundle)

    final = transport.seen[-1]["messages"]
    marked = sum(
        1
        for m in final
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("cache_control")
    )
    assert marked == 1, f"expected exactly one breakpoint, found {marked}"


def test_a_golden_cassette_still_replays(tmp_path):
    """Guards the failure mode that broke every recording at once.

    Recording and replaying with the same code only proves self-consistency.
    When the cache breakpoint reshaped string content into a one-block list, the
    fingerprint changed for every cassette ever recorded — and the round-trip
    test still passed. This pins the *recorded* shape instead.
    """
    from harness.solvers.transport import _request_fingerprint

    # A turn-0 request exactly as older cassettes stored it: string content.
    recorded_request = {
        "model": "claude-sonnet-4-6",
        "system": [{"type": "text", "text": "SYS"}],
        "tools": [{"name": "read_file"}],
        "messages": [{"role": "user", "content": "hello world"}],
    }
    recorded = _request_fingerprint(recorded_request)

    # The same message after the cache breakpoint decorates it.
    decorated = dict(recorded_request)
    decorated["messages"] = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hello world", "cache_control": {"type": "ephemeral"}}
            ],
        }
    ]
    assert _request_fingerprint(decorated) == recorded, "re-encoding must not read as divergence"

    # A genuinely different prompt still must diverge.
    changed = dict(recorded_request)
    changed["messages"] = [{"role": "user", "content": "hello worlds"}]
    assert _request_fingerprint(changed) != recorded


def test_a_multi_block_assistant_turn_keeps_bare_type_names():
    """Recordings store bare type names inside multi-block lists; widening breaks them."""
    from harness.solvers.transport import _block_shapes

    blocks = [
        {"type": "thinking", "thinking": "..."},
        {"type": "text", "text": "hi"},
        {"type": "tool_use", "id": "t", "name": "x", "input": {}},
    ]
    assert _block_shapes(blocks) == ["thinking", "text", "tool_use"]
