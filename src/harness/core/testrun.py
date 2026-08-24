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
from harness.core.bundle import TaskSpec
from harness.core.results import Bucket, TestOutcome
from harness.core.runtime import ContainerRuntime, ExecResult

# Inside the container, junit files go somewhere the repo tree never sees, so a
# `git add -A` for the solution diff cannot sweep them up.
#
# The directory name carries a per-run random nonce and sits OUTSIDE /tmp. A
# fixed path like /tmp/harness was trivially discoverable -- a solver could find
# it with `glob("/tmp/**/*junit*.xml")` and rewrite the file from an atexit hook
# registered in an ordinary source file, which runs after pytest writes the
# results and before the harness copies them out. That produced a `resolved`
# verdict from a solver that changed nothing. See `verify_exit_agreement` for
# the cross-check that backs this up, and DESIGN.md for the residual risk.
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


def group_by_file(requested: dict[str, Bucket]) -> dict[str, dict[str, Bucket]]:
    """Group selectors by the file they live in, preserving request order."""
    groups: dict[str, dict[str, Bucket]] = {}
    for selector, bucket in requested.items():
        groups.setdefault(selector.split("::", 1)[0], {})[selector] = bucket
    return groups


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
        argv = adapter.run_argv(spec.tests.run_cmd_template, list(group), container_junit)
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

        outcomes.extend(
            adapter.parse(
                junit_path=host_junit if copied else None,
                exec_result=exec_result,
                requested=group,
            )
        )

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
