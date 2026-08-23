"""Per-test statuses, transitions, and the rules that map them to an outcome.

Two invariants are encoded here rather than described:

* **Only `passed` counts as success.** Every other status -- including
  `skipped` and `not_found` -- is a non-pass. A skipped guardrail test has not
  been fixed, and a selector that never appeared in the results is a broken
  bundle, not a free pass.
* **Failure kinds are never conflated.** `timeout` and `infra_error` are the
  harness or the machine failing, not the solution. They produce
  `inconclusive`, never `unresolved`. Folding them into `failed` would report
  a working patch as broken because a container ran out of memory.

Pure: no runtime, no filesystem, no framework knowledge.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TestStatus(StrEnum):
    """What happened to one test."""

    PASSED = "passed"
    # An assertion failed. The ordinary, informative kind of failure.
    FAILED = "failed"
    # Setup or teardown blew up -- the test body may never have run.
    ERROR = "error"
    # Import or collection broke. Very often the solver's own syntax error,
    # which is why it is worth telling apart from a failed assertion.
    COLLECTION_ERROR = "collection_error"
    # The selector never appeared in the results. A broken bundle or a solver
    # that deleted a test; never treated as a skip.
    NOT_FOUND = "not_found"
    # Explicitly skipped (including xfail). Not a pass.
    SKIPPED = "skipped"
    # Killed at the wall clock.
    TIMEOUT = "timeout"
    # Docker failed, the container OOMed, the junit file never appeared.
    INFRA_ERROR = "infra_error"

    @property
    def is_pass(self) -> bool:
        return self is TestStatus.PASSED

    @property
    def is_infra(self) -> bool:
        """True when the machine failed, not the solution.

        These are the statuses that must produce `inconclusive`. Everything
        else is a genuine statement about the code under test.
        """
        return self in (TestStatus.TIMEOUT, TestStatus.INFRA_ERROR)


class Bucket(StrEnum):
    """Which guarantee a selector carries."""

    F2P = "f2p"
    P2P = "p2p"


class Transition(StrEnum):
    """How one test moved between the baseline and the post-solution run."""

    # Was not passing, now passes. What a solved f2p test looks like.
    FIXED = "fixed"
    # Was not passing, still is not.
    STILL_FAILING = "still_failing"
    # Was passing, now is not. What a p2p regression looks like.
    REGRESSED = "regressed"
    # Was passing, still passes.
    HELD = "held"
    # Either side was infra. Cannot be read as a statement about the solution.
    INCONCLUSIVE = "inconclusive"


class Outcome(StrEnum):
    """The verdict for a whole run."""

    RESOLVED = "resolved"
    # Tests pass, but the diff touched something it should not have.
    RESOLVED_SUSPECT = "resolved_suspect"
    UNRESOLVED = "unresolved"
    # The harness or the machine failed. Never reported as the solution failing.
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class TestOutcome:
    """One test's result in one phase."""

    test_id: str
    bucket: Bucket
    status: TestStatus
    duration_ms: int = 0
    message: str | None = None

    @property
    def passed(self) -> bool:
        return self.status.is_pass


def classify_transition(baseline: TestStatus, post: TestStatus) -> Transition:
    """How a test moved from the baseline run to the post-solution run.

    Bucket-independent on purpose: `fixed` and `regressed` are statements about
    movement, and applying the same rule to both buckets means an f2p test that
    somehow passed at baseline is described honestly rather than being forced
    into the f2p narrative.
    """
    if baseline.is_infra or post.is_infra:
        return Transition.INCONCLUSIVE
    if baseline.is_pass:
        return Transition.HELD if post.is_pass else Transition.REGRESSED
    return Transition.FIXED if post.is_pass else Transition.STILL_FAILING


@dataclass(frozen=True)
class TestTransition:
    """One test, seen from both sides."""

    test_id: str
    bucket: Bucket
    baseline: TestStatus
    post: TestStatus
    transition: Transition
    message: str | None = None
    duration_ms: int = 0


def pair_outcomes(baseline: list[TestOutcome], post: list[TestOutcome]) -> list[TestTransition]:
    """Join a baseline run and a post run by test id.

    A selector present in one side and not the other is reported with
    `not_found` on the missing side rather than dropped, so a vanished test is
    visible instead of silently shrinking the denominator.
    """
    baseline_by_id = {outcome.test_id: outcome for outcome in baseline}
    post_by_id = {outcome.test_id: outcome for outcome in post}

    transitions: list[TestTransition] = []
    for test_id in sorted(set(baseline_by_id) | set(post_by_id)):
        before = baseline_by_id.get(test_id)
        after = post_by_id.get(test_id)
        bucket = (before or after).bucket  # type: ignore[union-attr]
        before_status = before.status if before else TestStatus.NOT_FOUND
        after_status = after.status if after else TestStatus.NOT_FOUND
        transitions.append(
            TestTransition(
                test_id=test_id,
                bucket=bucket,
                baseline=before_status,
                post=after_status,
                transition=classify_transition(before_status, after_status),
                message=(after.message if after else None) or (before.message if before else None),
                duration_ms=after.duration_ms if after else 0,
            )
        )
    return transitions


def derive_outcome(
    transitions: list[TestTransition], *, gaming_flags: list[str] | None = None
) -> Outcome:
    """The run's verdict.

    Order matters. `inconclusive` is checked first because an environment
    failure means the other counts cannot be trusted -- reporting `unresolved`
    off a partially-run suite would blame the solution for a broken machine.
    """
    if any(t.transition is Transition.INCONCLUSIVE for t in transitions):
        return Outcome.INCONCLUSIVE

    f2p = [t for t in transitions if t.bucket is Bucket.F2P]
    p2p = [t for t in transitions if t.bucket is Bucket.P2P]

    if not f2p:
        # Nothing proves a fix, so nothing can be called resolved.
        return Outcome.INCONCLUSIVE

    all_fixed = all(t.transition is Transition.FIXED for t in f2p)
    no_regressions = not any(t.transition is Transition.REGRESSED for t in p2p)

    if not (all_fixed and no_regressions):
        return Outcome.UNRESOLVED
    return Outcome.RESOLVED_SUSPECT if gaming_flags else Outcome.RESOLVED
