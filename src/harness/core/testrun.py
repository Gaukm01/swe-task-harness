"""Running a set of selectors inside a container and reading the result.

Sits between the phase machine and the adapters: the phase machine says "run
these selectors in this container", this module handles the plumbing (write the
junit somewhere private, exec, copy it out, hand it to the adapter), and the
adapter owns everything framework-specific.

Imports no runtime implementation -- only the protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import secrets

from harness.adapters import get_adapter
from typing import Any

from harness.core.bundle import TaskSpec
from harness.core.results import Bucket, TestOutcome, TestStatus
from harness.core.runtime import ContainerRuntime, ExecResult

# Inside the container, junit files go somewhere the repo tree never sees, so a
# `git add -A` for the solution diff cannot sweep them up.
#
# The directory name carries a per-run random nonce and sits OUTSIDE /tmp. A
# fixed path like /tmp/harness was trivially discoverable -- a solver could find
# it with `glob("/tmp/**/*junit*.xml")` and rewrite the file from an atexit hook
# registered in an ordinary source file, which runs after pytest writes the
# results and before the harness copies them out. That produced a `resolved`
# verdict from a solver that changed nothing. See `_verify_exit_agreement` in
# the pytest adapter for the cross-check that backs this up, and the integrity
# canary below plus DESIGN.md for the residual risk.
CONTAINER_ARTIFACT_ROOT = "/var/opt"

# Where patches are staged before `git apply`. Unlike results, patches are
# *inputs*: a solver reading one gains nothing, because by the time any patch
# is staged its own diff is already computed and immutable. A fixed path is
# fine here, and keeping it out of the repo is what actually matters -- a
# `git add -A` must not sweep it into solution.diff.
CONTAINER_SCRATCH_DIR = "/tmp/harness"


def new_artifact_dir(nonce: str | None = None) -> str:
    """A fresh, unguessable in-container directory for grading artifacts."""
    return f"{CONTAINER_ARTIFACT_ROOT}/.hz-{nonce or secrets.token_hex(12)}"


@dataclass
class TestRun:
    """One invocation of the test runner."""

    label: str
    outcomes: list[TestOutcome]
    exec_result: ExecResult
    junit_path: Path | None
    argv: list[str]

    @property
    def duration_ms(self) -> int:
        return self.exec_result.duration_ms


def requested_map(spec: TaskSpec) -> dict[str, Bucket]:
    """Every guardrail selector with its bucket, f2p first."""
    requested = {selector: Bucket.F2P for selector in spec.tests.fail_to_pass}
    for selector in spec.tests.pass_to_pass:
        requested.setdefault(selector, Bucket.P2P)
    return requested


@dataclass(frozen=True)
class Canary:
    """A harness-owned test that must fail, used to detect a forged results file.

    Every signal grading reads -- the junit file and the process exit code -- is
    produced inside a process the solver's own code runs in. pytest has to
    import the solution to measure it, so arbitrary attacker code executes
    inside the measuring process by construction, and an `atexit` hook can
    rewrite the results after pytest writes them and before the harness reads
    them. Randomising the path and flagging suspicious source only raise the
    cost of that; neither changes who owns the evidence.

    The canary changes what the attacker must know. It is one extra test,
    appended to *every* pytest invocation, whose correct result is `failed`. Any
    blanket forgery -- "delete every <failure>", "set failures=0", "replace the
    document" -- flips it to passing or drops it, and the run is reported
    `inconclusive` instead of passing.

    Two details make it hard to route around:

    * it lives **inside the guardrail tests' own directory**, so a forger cannot
      simply skip entries outside the repo tree;
    * its name carries a per-run nonce, so it cannot be recognised and spared
      without already knowing the hidden selector names.

    That last point routes the remaining attack through the guarantee this
    harness defends best: the graded selector names are never in the container.
    It is not a proof, and DESIGN.md says so.
    """

    node_id: str
    container_path: str
    source: str


def build_canary(spec: TaskSpec, repo_root: str, nonce: str) -> Canary | None:
    """Place a canary beside the guardrail tests. None if there is nowhere to put it."""
    selectors = spec.tests.selectors
    if not selectors:
        return None
    first = selectors[0].split("::", 1)[0]
    directory = first.rsplit("/", 1)[0] if "/" in first else ""
    token = nonce[:10]
    module = f"test_hz_{token}"
    relative = f"{directory}/{module}.py" if directory else f"{module}.py"
    return Canary(
        node_id=f"{relative}::test_hz_{token}",
        container_path=f"{repo_root}/{relative}",
        source=(
            "# Written by swe-task-harness. Must report as failed; if it does not,\n"
            "# the results file was rewritten and the run is graded inconclusive.\n"
            f"def test_hz_{token}():\n"
            f'    assert False, "harness integrity canary {token}"\n'
        ),
    )


def group_by_file(requested: dict[str, Bucket]) -> dict[str, dict[str, Bucket]]:
    """Group selectors by the file they live in, preserving request order."""
    groups: dict[str, dict[str, Bucket]] = {}
    for selector, bucket in requested.items():
        groups.setdefault(selector.split("::", 1)[0], {})[selector] = bucket
    return groups


# Re-running a group one selector at a time is only worth it for a group small
# enough that the cost stays trivial. Beyond this, report the group as-is.
MAX_ISOLATION_SELECTORS = 25


def _every_selector_missing(outcomes: list[TestOutcome]) -> bool:
    """True when nothing resolved -- the signature of an aborted invocation."""
    return bool(outcomes) and all(o.status is TestStatus.NOT_FOUND for o in outcomes)


def _isolate_selectors(
    runtime: ContainerRuntime,
    container_id: str,
    spec: TaskSpec,
    adapter: Any,
    asked: dict[str, Bucket],
    *,
    workdir: str,
    container_dir: str,
    artifact_dir: Path,
    suffix: str,
) -> list[TestOutcome]:
    """Re-run each selector alone, so a broken one cannot hide the working ones."""
    if len(asked) > MAX_ISOLATION_SELECTORS:
        return [
            TestOutcome(test_id=s, bucket=b, status=TestStatus.NOT_FOUND,
                        message="the test run resolved no selectors at all")
            for s, b in asked.items()
        ]

    isolated: list[TestOutcome] = []
    for index, (selector, bucket) in enumerate(asked.items()):
        junit = f"{container_dir}/{suffix}-iso{index}-junit.xml"
        argv = adapter.run_argv(spec.tests.run_cmd_template, [selector], junit)
        result = runtime.exec(container_id, argv, workdir=workdir, timeout_s=spec.tests.timeout_s)
        host = artifact_dir / f"{suffix}-iso{index}-junit.xml"
        copied = runtime.copy_out(container_id, junit, host)
        isolated.extend(
            adapter.parse(
                junit_path=host if copied else None,
                exec_result=result,
                requested={selector: bucket},
            )
        )
    return isolated


def _canary_applies(outcomes: list[TestOutcome]) -> bool:
    """Whether the canary's verdict is meaningful for this group.

    Forgery exists to turn failures into passes, so it is only worth checking
    when the group claims at least one pass. When nothing passed there is
    nothing a forger could have gained, and demanding the canary anyway
    produces false alarms: a group whose test file legitimately fails to import
    aborts before *any* selector runs -- the canary included -- and reporting
    that as tampering would turn a correct `unresolved` into `inconclusive`.
    Observed on a real instance whose fail-to-pass file imports a symbol the
    fix has not added yet, which is the normal baseline shape.
    """
    return any(o.status is TestStatus.PASSED for o in outcomes)


def _canary_held(outcome: TestOutcome | None) -> bool:
    """The canary must have run and failed. Anything else means tampering.

    An infra status is not treated as tampering -- a timeout or a missing junit
    already grades inconclusive through the normal path, and double-reporting
    it as forgery would be misleading.
    """
    if outcome is None:
        return False
    if outcome.status.is_infra:
        return True
    return outcome.status is TestStatus.FAILED


def run_selectors(
    runtime: ContainerRuntime,
    container_id: str,
    spec: TaskSpec,
    *,
    label: str,
    workdir: str,
    artifact_dir: Path,
    selectors: dict[str, Bucket] | None = None,
    artifact_nonce: str | None = None,
    canary: Canary | None = None,
) -> TestRun:
    """Run the guardrail selectors and classify every one of them.

    Selectors run **grouped by file, one invocation per file**. That is a
    correctness requirement, not an optimisation. pytest resolves every selector
    before running anything and aborts the whole invocation if any one of them
    cannot be resolved -- so a test file that does not import blocks every other
    selector, including pass-to-pass tests in unrelated files.

    And a test file that does not import is the *normal* baseline state for a
    fail-to-pass test that adds new API: it imports the symbol the fix is
    supposed to introduce. Without grouping, almost every real SWE-bench
    instance would report its entire suite as `not_found` at GUARDED.

    A non-zero exit is expected and normal -- at GUARDED the f2p tests are
    supposed to fail. Only a timeout or a missing junit file is treated as the
    infrastructure failing, which is the distinction invariant 5 turns on.
    """
    adapter = get_adapter(spec.tests.framework)
    requested = selectors if selectors is not None else requested_map(spec)
    groups = group_by_file(requested)

    container_dir = new_artifact_dir(artifact_nonce)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    runtime.exec(container_id, ["mkdir", "-p", container_dir])

    outcomes: list[TestOutcome] = []
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    results: list[ExecResult] = []
    first_junit: Path | None = None
    last_argv: list[str] = []

    for index, group in enumerate(groups.values()):
        suffix = label if len(groups) == 1 else f"{label}-{index}"
        container_junit = f"{container_dir}/{suffix}-junit.xml"
        # The canary rides along in every invocation, so a forgery of any one
        # group's results file has to deal with it.
        selectors_for_run = list(group) + ([canary.node_id] if canary else [])
        argv = adapter.run_argv(spec.tests.run_cmd_template, selectors_for_run, container_junit)
        last_argv = argv

        exec_result = runtime.exec(
            container_id, argv, workdir=workdir, timeout_s=spec.tests.timeout_s
        )
        results.append(exec_result)

        host_junit = artifact_dir / f"{suffix}-junit.xml"
        copied = runtime.copy_out(container_id, container_junit, host_junit)
        if copied and first_junit is None:
            first_junit = host_junit

        stdout_parts.append(f"$ {' '.join(argv)}\n{exec_result.stdout}")
        if exec_result.stderr.strip():
            stderr_parts.append(exec_result.stderr)

        asked = dict(group)
        if canary:
            asked[canary.node_id] = Bucket.P2P
        parsed = adapter.parse(
            junit_path=host_junit if copied else None,
            exec_result=exec_result,
            requested=asked,
        )

        # One unresolvable selector aborts the whole invocation, so a single
        # malformed id makes every sibling look missing. Seen on a real
        # instance whose dataset node ids were truncated mid-parameter: eleven
        # selectors reported `not_found` when nine were perfectly fine. Isolate
        # them so the report names the ones that are actually broken.
        if _every_selector_missing(parsed) and len(group) > 1:
            parsed = _isolate_selectors(
                runtime, container_id, spec, adapter, asked,
                workdir=workdir, container_dir=container_dir, artifact_dir=artifact_dir,
                suffix=suffix,
            )

        if canary:
            verdict = next((o for o in parsed if o.test_id == canary.node_id), None)
            parsed = [o for o in parsed if o.test_id != canary.node_id]
            if _canary_applies(parsed) and not _canary_held(verdict):
                reason = (
                    "the harness integrity canary did not report as failed "
                    f"({verdict.status.value if verdict else 'absent'}). The results file "
                    "does not reflect what the test run produced, so this run says nothing "
                    "about the solution."
                )
                parsed = [
                    TestOutcome(
                        test_id=o.test_id,
                        bucket=o.bucket,
                        status=TestStatus.INFRA_ERROR,
                        message=reason,
                    )
                    for o in parsed
                ]
        outcomes.extend(parsed)

    combined_stdout = "\n".join(stdout_parts)
    combined_stderr = "\n".join(stderr_parts)
    (artifact_dir / f"{label}-stdout.txt").write_text(combined_stdout)
    (artifact_dir / f"{label}-stderr.txt").write_text(combined_stderr)

    # Report the outcomes in the order they were requested, so f2p reads first
    # regardless of how the selectors happened to group.
    by_id = {o.test_id: o for o in outcomes}
    ordered = [by_id[s] for s in requested if s in by_id]

    merged = ExecResult(
        argv=last_argv,
        # The worst exit code across groups; a timeout anywhere is a timeout.
        exit_code=max((r.exit_code for r in results), default=0),
        stdout=combined_stdout,
        stderr=combined_stderr,
        duration_ms=sum(r.duration_ms for r in results),
        timed_out=any(r.timed_out for r in results),
    )
    return TestRun(
        label=label,
        outcomes=ordered,
        exec_result=merged,
        junit_path=first_junit,
        argv=last_argv,
    )
