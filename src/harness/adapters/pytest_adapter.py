"""The pytest adapter. The only fully implemented one.

The interesting problem here is mapping a pytest node ID back to a junit
`<testcase>`. junit records `classname` and `name`, not node IDs, so
`tests/test_x.py::TestFoo::test_bar` arrives as
`classname="tests.test_x.TestFoo" name="test_bar"`. Reversing that is ambiguous
-- you cannot tell where the module path ends and the class begins -- so the
mapping runs *forwards* instead: each requested selector is translated into the
`(classname, name)` pair pytest would have written, and looked up.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from harness.adapters.junit import JunitCase, JunitParseError, read_junit
from harness.core.results import Bucket, TestOutcome, TestStatus
from harness.core.runtime import ExecResult

# pytest's documented exit codes.
EXIT_OK = 0
EXIT_TESTS_FAILED = 1
EXIT_INTERRUPTED = 2
EXIT_INTERNAL_ERROR = 3
EXIT_USAGE_ERROR = 4
EXIT_NO_TESTS_COLLECTED = 5

_JUNIT_RESULT_TO_STATUS = {
    "passed": TestStatus.PASSED,
    "failed": TestStatus.FAILED,
    "error": TestStatus.ERROR,
    "skipped": TestStatus.SKIPPED,
}


def selector_to_junit_key(selector: str) -> tuple[str, str]:
    """The `(classname, name)` pytest writes for a node ID.

    `tests/test_x.py::test_a`            -> ("tests.test_x", "test_a")
    `tests/test_x.py::Klass::test_a`     -> ("tests.test_x.Klass", "test_a")
    `tests/test_x.py::test_a[1-2]`       -> ("tests.test_x", "test_a[1-2]")

    Parametrized ids keep their brackets in `name`, which is what pytest does.
    """
    file_part, _, rest = selector.partition("::")
    module = file_part.removesuffix(".py").strip("/").replace("/", ".")
    if not rest:
        # A whole-file selector has no single testcase; match on the module.
        return (module, "")
    parts = rest.split("::")
    if len(parts) == 1:
        return (module, parts[0])
    return (".".join([module, *parts[:-1]]), parts[-1])


class PytestAdapter:
    """Implements `TestAdapter` for pytest."""

    framework = "pytest"

    def smoke_argv(self) -> list[str]:
        # `--version` runs the plugin machinery, so an environment with pytest
        # installed but broken fails here -- which `which pytest` would call healthy.
        return ["python", "-m", "pytest", "--version"]

    def run_argv(self, template: str, selectors: list[str], out_path: str) -> list[str]:
        """Expand the bundle's `run_cmd_template` into an argv list.

        `shlex.split` parses the template into tokens; it never runs anything.
        `{selectors}` expands to one argv element per selector, so a node ID
        containing spaces or brackets needs no quoting and cannot be
        re-interpreted by anything downstream -- there is no shell to
        re-interpret it.
        """
        argv: list[str] = []
        for token in shlex.split(template):
            if token == "{selectors}":
                argv.extend(selectors)
            elif "{out}" in token:
                argv.append(token.replace("{out}", out_path))
            elif "{selectors}" in token:
                # e.g. `--tests={selectors}` cannot expand to N arguments.
                raise ValueError(
                    "run_cmd_template must use {selectors} as a standalone argument, "
                    f"not embedded in {token!r}"
                )
            else:
                argv.append(token)
        return argv

    def parse(
        self,
        *,
        junit_path: Path | None,
        exec_result: ExecResult,
        requested: dict[str, Bucket],
    ) -> list[TestOutcome]:
        """One outcome per requested selector, in the order requested."""
        missing_status, missing_note = self._status_for_missing(junit_path, exec_result)

        cases: dict[tuple[str, str], JunitCase] = {}
        # Files pytest could not import at all. Every selector inside one is a
        # collection error, not a missing selector -- and telling those apart
        # matters: a module that fails to import because the fix does not exist
        # yet is a *correct* baseline, while a genuinely absent selector is a
        # broken bundle.
        uncollectable: set[str] = set()
        if missing_status is None:
            assert junit_path is not None
            try:
                for parsed in read_junit(junit_path):
                    # Later entries win: a rerun of the same id reports its last state.
                    cases[parsed.key] = parsed
                    if parsed.is_collection_error and parsed.file:
                        uncollectable.add(parsed.file)
            except JunitParseError as error:
                missing_status = TestStatus.INFRA_ERROR
                missing_note = str(error)

        outcomes: list[TestOutcome] = []
        for selector, bucket in requested.items():
            case = cases.get(selector_to_junit_key(selector))
            if case is None:
                selector_file = selector.split("::", 1)[0]
                if missing_status is None and selector_file in uncollectable:
                    outcomes.append(
                        TestOutcome(
                            test_id=selector,
                            bucket=bucket,
                            status=TestStatus.COLLECTION_ERROR,
                            message=f"{selector_file} failed to import",
                        )
                    )
                    continue
                outcomes.append(
                    TestOutcome(
                        test_id=selector,
                        bucket=bucket,
                        status=missing_status or self._absent_status(exec_result),
                        message=missing_note or self._absent_note(exec_result),
                    )
                )
                continue

            status = _JUNIT_RESULT_TO_STATUS.get(case.result, TestStatus.ERROR)
            if case.is_collection_error:
                status = TestStatus.COLLECTION_ERROR
            outcomes.append(
                TestOutcome(
                    test_id=selector,
                    bucket=bucket,
                    status=status,
                    duration_ms=case.duration_ms,
                    message=case.message,
                )
            )
        return outcomes

    # -- how to read an absent result -------------------------------------

    def _status_for_missing(
        self, junit_path: Path | None, exec_result: ExecResult
    ) -> tuple[TestStatus | None, str | None]:
        """A run-wide reason every selector is missing, if there is one.

        Distinguishing these is the whole point of invariant 5: a container
        that was killed at the wall clock has said nothing about the solution,
        and must not be recorded as tests failing.
        """
        if exec_result.timed_out:
            return TestStatus.TIMEOUT, f"test run timed out after {exec_result.duration_ms}ms"
        if junit_path is None or not junit_path.is_file():
            return (
                TestStatus.INFRA_ERROR,
                "no junit file was produced: "
                + (exec_result.stderr or exec_result.stdout or "no output").strip()[:500],
            )
        return None, None

    def _absent_status(self, exec_result: ExecResult) -> TestStatus:
        """Why a single selector is missing from an otherwise fine junit file."""
        if exec_result.exit_code in (EXIT_INTERRUPTED, EXIT_INTERNAL_ERROR):
            # pytest bailed during collection; the module never imported.
            return TestStatus.COLLECTION_ERROR
        # EXIT_USAGE_ERROR and EXIT_NO_TESTS_COLLECTED both mean pytest could
        # not resolve the selector -- a stale bundle or a deleted test.
        return TestStatus.NOT_FOUND

    def _absent_note(self, exec_result: ExecResult) -> str:
        detail = (exec_result.stderr or exec_result.stdout or "").strip()
        tail = detail.splitlines()[-3:] if detail else []
        return (
            f"selector produced no result (pytest exit {exec_result.exit_code})"
            + (f": {' | '.join(tail)}" if tail else "")
        )[:1000]
