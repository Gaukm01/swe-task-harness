"""The run lane's load-bearing guarantees, against FakeRuntime.

Kept deliberately small: these are the invariants, not exhaustive coverage.
"""

from __future__ import annotations

import pytest

from harness.core.bundle import load_bundle
from harness.core.cache import compute_cache_key
from harness.core.errors import BaselineValidationError
from harness.core.gaming import detect_gaming_flags
from harness.core.phases import force_restore_tests, prepare_base, run_solver
from harness.core.results import Outcome
from harness.core.run import execute_run
from harness.runtime.fake import FakeRuntime, argv_contains
from harness.solvers import GoldSolver, NoopSolver, resolve_solver

from tests.test_phases import healthy, junit_for, wire_validation

GLOBS = ["tests/**", "**/test_*.py", "**/*_test.py"]


@pytest.fixture
def bundle(tiny_fixture):
    return load_bundle(tiny_fixture)


@pytest.fixture
def key(bundle, tiny_fixture):
    return compute_cache_key(bundle.spec, tiny_fixture)


# -- invariant 1: SOLVE branches from BASE --------------------------------


def test_solve_branches_from_base_not_from_guarded(bundle, tiny_fixture, key):
    # A GUARDED image has the guardrail tests on disk. Starting from one would
    # hand the solver the hidden tests.
    runtime = FakeRuntime()
    base = prepare_base(runtime, bundle.spec, tiny_fixture, cache_key=key)
    run_solver(runtime, bundle, base, NoopSolver(), run_id="01RUN")

    solve_container = runtime.containers["fake0002"]
    assert solve_container.spec.image == base.image
    assert "guarded" not in solve_container.spec.image


def test_the_solve_container_has_no_network(bundle, tiny_fixture, key):
    # Egress would let the solver fetch the upstream commit containing the fix.
    runtime = FakeRuntime()
    base = prepare_base(runtime, bundle.spec, tiny_fixture, cache_key=key)
    run_solver(runtime, bundle, base, NoopSolver(), run_id="01RUN")
    assert runtime.containers["fake0002"].spec.network == "none"


# -- invariant 7: the harness computes the diff ---------------------------


def test_the_diff_is_computed_by_the_harness(bundle, tiny_fixture, key):
    runtime = FakeRuntime()
    base = prepare_base(runtime, bundle.spec, tiny_fixture, cache_key=key)
    run_solver(runtime, bundle, base, NoopSolver(), run_id="01RUN")
    # `git add -A` then `git diff --cached HEAD`, never a solver-supplied patch.
    assert runtime.index_of("add", "--all") < runtime.index_of("diff", "--cached", "HEAD")


# -- invariant 2: force-restore -------------------------------------------


def test_force_restore_restores_tracked_tests_and_deletes_added_ones(bundle):
    runtime = FakeRuntime()
    container = runtime.create.__self__ and "c1"
    runtime.script(
        argv_contains("ls-tree"), stdout="tinylib/intervals.py\ntests/test_intervals.py\n"
    )
    runtime.script(argv_contains("ls-files", "--others"), stdout="conftest.py\ntests/test_new.py\n")

    restored = force_restore_tests(runtime, container, bundle.spec, [])

    assert runtime.ran("checkout", "HEAD", "--", "tests/test_intervals.py")
    # A solver-added conftest.py never appears in HEAD, so restore alone would
    # leave it running.
    assert runtime.ran("rm", "-f")
    assert "conftest.py" in restored
    assert "tests/test_new.py" in restored
    # Source files are the solver's to change.
    assert "tinylib/intervals.py" not in restored


def test_force_restore_happens_before_the_test_patch_is_applied(bundle, tiny_fixture, key):
    from harness.core.phases import grade_solution

    runtime = FakeRuntime()
    runtime.copy_out_payloads["/tmp/harness/post-junit.xml"] = junit_for(
        bundle.spec.tests.pass_to_pass + bundle.spec.tests.fail_to_pass, []
    )
    base = prepare_base(runtime, bundle.spec, tiny_fixture, cache_key=key)
    grade_solution(runtime, bundle, base, "", run_id="01RUN", artifact_dir=tiny_fixture / "x")

    assert runtime.index_of("checkout", "HEAD", "--") < runtime.index_of(
        "apply", "test_patch.diff"
    )


# -- invariant 3: validation cannot be skipped ----------------------------


def test_a_failing_baseline_aborts_before_the_solver_runs(bundle, tiny_fixture, key, tmp_path):
    runtime = FakeRuntime()
    f2p = bundle.spec.tests.fail_to_pass
    p2p = bundle.spec.tests.pass_to_pass
    # f2p already passing at GUARDED: the baseline does not hold.
    wire_validation(
        runtime, bundle, guarded_junit=junit_for(p2p + f2p, []), gold_junit=junit_for(p2p + f2p, [])
    )
    base = prepare_base(runtime, bundle.spec, tiny_fixture, cache_key=key)

    with pytest.raises(BaselineValidationError):
        execute_run(
            runtime, bundle, base, GoldSolver(), run_id="01RUN", artifact_dir=tmp_path / "a"
        )
    # The gold patch was never applied: no solver ran.
    assert not runtime.ran("apply", "gold-solution.diff")


# -- outcomes -------------------------------------------------------------


def _run(runtime, bundle, tiny_fixture, key, tmp_path, solver, post_junit):
    guarded_junit, gold_junit = healthy(bundle)
    wire_validation(runtime, bundle, guarded_junit=guarded_junit, gold_junit=gold_junit)
    runtime.copy_out_payloads["/tmp/harness/post-junit.xml"] = post_junit
    base = prepare_base(runtime, bundle.spec, tiny_fixture, cache_key=key)
    return execute_run(
        runtime, bundle, base, solver, run_id="01RUN", artifact_dir=tmp_path / "a"
    )


def test_gold_resolves(bundle, tiny_fixture, key, tmp_path):
    runtime = FakeRuntime()
    everything = bundle.spec.tests.pass_to_pass + bundle.spec.tests.fail_to_pass
    result = _run(
        runtime, bundle, tiny_fixture, key, tmp_path, GoldSolver(), junit_for(everything, [])
    )
    assert result.outcome is Outcome.RESOLVED


def test_noop_is_unresolved_with_every_f2p_still_failing(bundle, tiny_fixture, key, tmp_path):
    from harness.core.results import Bucket, Transition

    runtime = FakeRuntime()
    f2p = bundle.spec.tests.fail_to_pass
    result = _run(
        runtime,
        bundle,
        tiny_fixture,
        key,
        tmp_path,
        NoopSolver(),
        junit_for(bundle.spec.tests.pass_to_pass, f2p),
    )
    assert result.outcome is Outcome.UNRESOLVED
    assert all(
        t.transition is Transition.STILL_FAILING
        for t in result.transitions
        if t.bucket is Bucket.F2P
    )


# -- gaming flags ---------------------------------------------------------


def test_flags_fire_on_guardrail_and_config_edits():
    diff = "diff --git a/tests/test_x.py b/tests/test_x.py\n"
    assert detect_gaming_flags(diff, GLOBS)
    conftest = "diff --git a/conftest.py b/conftest.py\n"
    assert detect_gaming_flags(conftest, GLOBS)
    framework = (
        "diff --git a/usr/lib/python3/site-packages/_pytest/assertion.py "
        "b/usr/lib/python3/site-packages/_pytest/assertion.py\n"
    )
    assert detect_gaming_flags(framework, GLOBS)


def test_an_honest_source_fix_is_not_flagged():
    diff = "diff --git a/tinylib/intervals.py b/tinylib/intervals.py\n"
    assert detect_gaming_flags(diff, GLOBS) == []


# -- solver resolution ----------------------------------------------------


def test_solver_strings_resolve():
    assert resolve_solver("gold").kind == "gold"
    assert resolve_solver("noop").kind == "noop"
    assert resolve_solver("cmd:echo hi").kind == "cmd"


def test_an_unknown_solver_names_the_alternatives():
    from harness.core.errors import UsageError

    with pytest.raises(UsageError) as caught:
        resolve_solver("wat")
    assert "gold" in (caught.value.fix or "")
    assert "replay" in (caught.value.fix or "")
