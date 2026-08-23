"""Classification and outcome derivation. The invariants live here."""

from __future__ import annotations

import pytest

from harness.core.results import (
    Bucket,
    Outcome,
    TestOutcome,
    TestStatus,
    TestTransition,
    Transition,
    classify_transition,
    derive_outcome,
    pair_outcomes,
)

NON_PASS = [
    TestStatus.FAILED,
    TestStatus.ERROR,
    TestStatus.COLLECTION_ERROR,
    TestStatus.NOT_FOUND,
    TestStatus.SKIPPED,
]


# -- only `passed` counts as success --------------------------------------


def test_only_passed_is_a_pass():
    assert TestStatus.PASSED.is_pass
    for status in NON_PASS + [TestStatus.TIMEOUT, TestStatus.INFRA_ERROR]:
        assert not status.is_pass, status


def test_skipped_is_not_a_pass():
    # A skipped guardrail test has not been fixed.
    assert not TestStatus.SKIPPED.is_pass


def test_not_found_is_not_a_pass():
    # A selector that never appeared is a broken bundle, not a free pass.
    assert not TestStatus.NOT_FOUND.is_pass


def test_only_timeout_and_infra_are_infrastructure():
    assert TestStatus.TIMEOUT.is_infra
    assert TestStatus.INFRA_ERROR.is_infra
    for status in NON_PASS + [TestStatus.PASSED]:
        assert not status.is_infra, status


# -- transitions ----------------------------------------------------------


@pytest.mark.parametrize(
    ("baseline", "post", "expected"),
    [
        (TestStatus.FAILED, TestStatus.PASSED, Transition.FIXED),
        (TestStatus.FAILED, TestStatus.FAILED, Transition.STILL_FAILING),
        (TestStatus.PASSED, TestStatus.FAILED, Transition.REGRESSED),
        (TestStatus.PASSED, TestStatus.PASSED, Transition.HELD),
        # A test the solver deleted is still_failing, not fixed.
        (TestStatus.FAILED, TestStatus.NOT_FOUND, Transition.STILL_FAILING),
        # A skip does not count as a fix.
        (TestStatus.FAILED, TestStatus.SKIPPED, Transition.STILL_FAILING),
        # The solver broke the module outright.
        (TestStatus.PASSED, TestStatus.COLLECTION_ERROR, Transition.REGRESSED),
    ],
)
def test_transitions(baseline, post, expected):
    assert classify_transition(baseline, post) is expected


@pytest.mark.parametrize("infra", [TestStatus.TIMEOUT, TestStatus.INFRA_ERROR])
def test_infrastructure_on_either_side_is_inconclusive(infra):
    # The machine failing says nothing about the solution.
    assert classify_transition(infra, TestStatus.PASSED) is Transition.INCONCLUSIVE
    assert classify_transition(TestStatus.FAILED, infra) is Transition.INCONCLUSIVE


# -- pairing --------------------------------------------------------------


def _outcome(test_id, bucket, status):
    return TestOutcome(test_id=test_id, bucket=bucket, status=status)


def test_pairing_joins_by_test_id():
    baseline = [_outcome("a", Bucket.F2P, TestStatus.FAILED)]
    post = [_outcome("a", Bucket.F2P, TestStatus.PASSED)]
    (paired,) = pair_outcomes(baseline, post)
    assert paired.transition is Transition.FIXED


def test_a_test_missing_from_the_post_run_is_visible():
    # Dropping it would silently shrink the denominator.
    baseline = [_outcome("a", Bucket.P2P, TestStatus.PASSED)]
    (paired,) = pair_outcomes(baseline, [])
    assert paired.post is TestStatus.NOT_FOUND
    assert paired.transition is Transition.REGRESSED


def test_a_test_only_in_the_post_run_is_visible():
    (paired,) = pair_outcomes([], [_outcome("a", Bucket.F2P, TestStatus.PASSED)])
    assert paired.baseline is TestStatus.NOT_FOUND
    assert paired.transition is Transition.FIXED


# -- outcome derivation ---------------------------------------------------


def _transition(test_id, bucket, transition):
    return TestTransition(
        test_id=test_id,
        bucket=bucket,
        baseline=TestStatus.FAILED,
        post=TestStatus.PASSED,
        transition=transition,
    )


def test_all_f2p_fixed_and_no_regressions_is_resolved():
    transitions = [
        _transition("f1", Bucket.F2P, Transition.FIXED),
        _transition("p1", Bucket.P2P, Transition.HELD),
    ]
    assert derive_outcome(transitions) is Outcome.RESOLVED


def test_a_gaming_flag_downgrades_to_suspect():
    # Tests pass, but the diff touched something it should not have.
    transitions = [_transition("f1", Bucket.F2P, Transition.FIXED)]
    assert derive_outcome(transitions, gaming_flags=["touched tests/"]) is Outcome.RESOLVED_SUSPECT


def test_one_unfixed_f2p_is_unresolved():
    transitions = [
        _transition("f1", Bucket.F2P, Transition.FIXED),
        _transition("f2", Bucket.F2P, Transition.STILL_FAILING),
    ]
    assert derive_outcome(transitions) is Outcome.UNRESOLVED


def test_a_p2p_regression_is_unresolved_even_if_every_f2p_is_fixed():
    transitions = [
        _transition("f1", Bucket.F2P, Transition.FIXED),
        _transition("p1", Bucket.P2P, Transition.REGRESSED),
    ]
    assert derive_outcome(transitions) is Outcome.UNRESOLVED


def test_infrastructure_beats_everything():
    # Reporting `unresolved` off a partially-run suite would blame the solution
    # for a broken machine.
    transitions = [
        _transition("f1", Bucket.F2P, Transition.FIXED),
        _transition("p1", Bucket.P2P, Transition.INCONCLUSIVE),
    ]
    assert derive_outcome(transitions) is Outcome.INCONCLUSIVE


def test_infrastructure_beats_a_gaming_flag_too():
    transitions = [_transition("f1", Bucket.F2P, Transition.INCONCLUSIVE)]
    assert derive_outcome(transitions, gaming_flags=["x"]) is Outcome.INCONCLUSIVE


def test_no_f2p_at_all_cannot_be_resolved():
    # Nothing would prove a fix.
    transitions = [_transition("p1", Bucket.P2P, Transition.HELD)]
    assert derive_outcome(transitions) is Outcome.INCONCLUSIVE
