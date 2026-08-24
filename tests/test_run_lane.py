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

from tests.test_phases import NONCE, healthy, junit_for, wire_validation

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


def test_force_restore_happens_before_the_test_patch_is_applied(
    bundle, tiny_fixture, key, tmp_path
):
    from harness.core.phases import grade_solution
    from harness.core.testrun import new_artifact_dir

    runtime = FakeRuntime()
    runtime.copy_out_payloads[f"{new_artifact_dir(NONCE)}/post-junit.xml"] = junit_for(
        bundle.spec.tests.pass_to_pass + bundle.spec.tests.fail_to_pass, []
    )
    base = prepare_base(runtime, bundle.spec, tiny_fixture, cache_key=key)
    # tmp_path, never the bundle: artifacts written into examples/ pollute the
    # deliverable and feed hash_build_context, silently changing the cache key.
    grade_solution(
        runtime, bundle, base, "", run_id="01RUN",
        artifact_dir=tmp_path / "grade", artifact_nonce=NONCE,
    )

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
            runtime,
            bundle,
            base,
            GoldSolver(),
            run_id="01RUN",
            artifact_dir=tmp_path / "a",
            artifact_nonce=NONCE,
        )
    # The gold patch was never applied: no solver ran.
    assert not runtime.ran("apply", "gold-solution.diff")


# -- outcomes -------------------------------------------------------------


def _run(runtime, bundle, tiny_fixture, key, tmp_path, solver, passed, failed):
    """Script an HONEST post-run: the given results, plus the canary correctly failing."""
    from harness.core.testrun import build_canary, new_artifact_dir

    guarded_junit, gold_junit = healthy(bundle)
    wire_validation(runtime, bundle, guarded_junit=guarded_junit, gold_junit=gold_junit)

    canary = build_canary(bundle.spec, "/workspace/repo", NONCE)
    runtime.copy_out_payloads[f"{new_artifact_dir(NONCE)}/post-junit.xml"] = junit_for(
        passed, [*failed, canary.node_id]
    )
    base = prepare_base(runtime, bundle.spec, tiny_fixture, cache_key=key)
    return execute_run(
        runtime,
        bundle,
        base,
        solver,
        run_id="01RUN",
        artifact_dir=tmp_path / "a",
        artifact_nonce=NONCE,
    )


def test_gold_resolves(bundle, tiny_fixture, key, tmp_path):
    runtime = FakeRuntime()
    everything = bundle.spec.tests.pass_to_pass + bundle.spec.tests.fail_to_pass
    result = _run(runtime, bundle, tiny_fixture, key, tmp_path, GoldSolver(), everything, [])
    assert result.outcome is Outcome.RESOLVED


def test_noop_is_unresolved_with_every_f2p_still_failing(bundle, tiny_fixture, key, tmp_path):
    from harness.core.results import Bucket, Transition

    runtime = FakeRuntime()
    f2p = bundle.spec.tests.fail_to_pass
    result = _run(
        runtime, bundle, tiny_fixture, key, tmp_path, NoopSolver(),
        bundle.spec.tests.pass_to_pass, f2p,
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


# -- results tampering ----------------------------------------------------


def test_result_forgery_shapes_are_flagged():
    """The junit-forgery attack lives in an ordinary source file.

    It touches no test path and no test config, so every path-based flag misses
    it. What gives it away is what the code does.
    """
    from harness.core.gaming import detect_content_flags

    attack = (
        "diff --git a/tinylib/__init__.py b/tinylib/__init__.py\n"
        "--- a/tinylib/__init__.py\n"
        "+++ b/tinylib/__init__.py\n"
        "+import atexit, glob\n"
        "+def _forge():\n"
        "+    for p in glob.glob('/var/opt/.*/*junit*.xml'):\n"
        "+        open(p, 'w').write(_DOC)\n"
        "+atexit.register(_forge)\n"
    )
    flags = detect_gaming_flags(attack, GLOBS)
    assert flags, "the forgery attack must not be invisible"
    assert any("atexit" in f for f in flags)
    assert any("junit" in f for f in flags)
    # And it must not fire on an honest fix.
    honest = (
        "diff --git a/tinylib/intervals.py b/tinylib/intervals.py\n"
        "+    ordered = sorted(intervals)\n"
        "+    return merged\n"
    )
    assert detect_content_flags(honest) == []


def test_the_results_path_is_unguessable_and_outside_the_repo():
    from harness.core.testrun import new_artifact_dir

    first, second = new_artifact_dir(), new_artifact_dir()
    assert first != second
    assert not first.startswith("/tmp")
    # Dot-prefixed: a plain `glob('/var/opt/**/*.xml')` will not descend into it.
    assert "/." in first


def test_the_exit_check_depends_on_guarded_rejecting_non_passing_p2p():
    """Pins a cross-module dependency that is easy to break silently.

    `_verify_exit_agreement` only fires when every selector in a group reports
    `passed`. That is not as narrow as it looks *because* `assert_guarded`
    refuses a baseline where any p2p is not passing. Relax that assertion and a
    group could legitimately contain a non-passing selector, disabling the
    forgery cross-check without a single test failing.
    """
    from harness.core.phases import assert_guarded
    from harness.core.results import Bucket, TestOutcome, TestStatus
    from harness.core.runtime import ExecResult
    from harness.core.testrun import TestRun

    def run(outcomes):
        return TestRun(
            label="x",
            outcomes=outcomes,
            exec_result=ExecResult(argv=[], exit_code=1, stdout="", stderr="", duration_ms=1),
            junit_path=None,
            argv=[],
        )

    for not_passing in (TestStatus.SKIPPED, TestStatus.FAILED, TestStatus.NOT_FOUND):
        problems = assert_guarded(
            run(
                [
                    TestOutcome(test_id="f", bucket=Bucket.F2P, status=TestStatus.FAILED),
                    TestOutcome(test_id="p", bucket=Bucket.P2P, status=not_passing),
                ]
            )
        )
        assert problems, f"a p2p reporting {not_passing.value} at baseline must be rejected"
