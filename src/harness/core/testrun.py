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

from harness.adapters import get_adapter
from harness.core.bundle import TaskSpec
from harness.core.results import Bucket, TestOutcome
from harness.core.runtime import ContainerRuntime, ExecResult

# Inside the container, junit files go somewhere the repo tree never sees, so a
# `git add -A` for the solution diff cannot sweep them up and so a solver
# poking around the repo never finds one to forge.
CONTAINER_ARTIFACT_DIR = "/tmp/harness"


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


def run_selectors(
    runtime: ContainerRuntime,
    container_id: str,
    spec: TaskSpec,
    *,
    label: str,
    workdir: str,
    artifact_dir: Path,
    selectors: dict[str, Bucket] | None = None,
) -> TestRun:
    """Run the guardrail selectors and classify every one of them.

    A non-zero exit is expected and normal -- at GUARDED the f2p tests are
    supposed to fail. Only a timeout or a missing junit file is treated as the
    infrastructure failing, which is the distinction invariant 5 turns on.
    """
    adapter = get_adapter(spec.tests.framework)
    requested = selectors if selectors is not None else requested_map(spec)

    container_junit = f"{CONTAINER_ARTIFACT_DIR}/{label}-junit.xml"
    argv = adapter.run_argv(spec.tests.run_cmd_template, list(requested), container_junit)

    runtime.exec(container_id, ["mkdir", "-p", CONTAINER_ARTIFACT_DIR])
    exec_result = runtime.exec(container_id, argv, workdir=workdir, timeout_s=spec.tests.timeout_s)

    artifact_dir.mkdir(parents=True, exist_ok=True)
    host_junit = artifact_dir / f"{label}-junit.xml"
    copied = runtime.copy_out(container_id, container_junit, host_junit)

    (artifact_dir / f"{label}-stdout.txt").write_text(exec_result.stdout)
    (artifact_dir / f"{label}-stderr.txt").write_text(exec_result.stderr)

    outcomes = adapter.parse(
        junit_path=host_junit if copied else None,
        exec_result=exec_result,
        requested=requested,
    )
    return TestRun(
        label=label,
        outcomes=outcomes,
        exec_result=exec_result,
        junit_path=host_junit if copied else None,
        argv=argv,
    )
