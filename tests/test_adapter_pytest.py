"""The pytest adapter: selector mapping, argv building, and reading a run."""

from __future__ import annotations

import pytest

from harness.adapters import get_adapter
from harness.adapters.junit import JunitParseError, parse_junit
from harness.adapters.pytest_adapter import PytestAdapter, selector_to_junit_key
from harness.core.errors import BundleInvalidError
from harness.core.results import Bucket, TestStatus
from harness.core.runtime import ExecResult

adapter = PytestAdapter()

TEMPLATE = "python -m pytest {selectors} --junitxml={out}"


def _exec(exit_code=0, stdout="", stderr="", timed_out=False):
    return ExecResult(
        argv=["python", "-m", "pytest"],
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=10,
        timed_out=timed_out,
    )


# -- selector mapping -----------------------------------------------------


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        ("tests/test_x.py::test_a", ("tests.test_x", "test_a")),
        ("tests/test_x.py::Klass::test_a", ("tests.test_x.Klass", "test_a")),
        ("tests/a/b/test_x.py::Klass::Nested::test_a", ("tests.a.b.test_x.Klass.Nested", "test_a")),
        # Parametrized ids keep their brackets in `name`, as pytest writes them.
        ("tests/test_x.py::test_a[1-2]", ("tests.test_x", "test_a[1-2]")),
        (
            "tests/unit/utils/test_urlutils.py::TestWiden::test_widen[a.b.c-expected0]",
            ("tests.unit.utils.test_urlutils.TestWiden", "test_widen[a.b.c-expected0]"),
        ),
        ("test_top_level.py::test_a", ("test_top_level", "test_a")),
    ],
)
def test_selector_to_junit_key(selector, expected):
    assert selector_to_junit_key(selector) == expected


# -- argv construction ----------------------------------------------------


def test_run_argv_expands_selectors_as_separate_arguments():
    argv = adapter.run_argv(TEMPLATE, ["tests/t.py::a", "tests/t.py::b"], "/tmp/o.xml")
    assert argv == [
        "python",
        "-m",
        "pytest",
        "tests/t.py::a",
        "tests/t.py::b",
        "--junitxml=/tmp/o.xml",
    ]


def test_a_selector_with_spaces_and_brackets_needs_no_quoting():
    # There is no shell to re-interpret it, which is the entire point.
    weird = "tests/t.py::test_a[a b; rm -rf /]"
    argv = adapter.run_argv(TEMPLATE, [weird], "/tmp/o.xml")
    assert weird in argv


def test_embedded_selectors_placeholder_is_rejected():
    with pytest.raises(ValueError, match="standalone argument"):
        adapter.run_argv("pytest --tests={selectors} --junitxml={out}", ["a"], "/o.xml")


# -- junit parsing --------------------------------------------------------

PASSING = """<testsuites><testsuite name="pytest" tests="2">
<testcase classname="tests.test_x" name="test_a" time="0.01"/>
<testcase classname="tests.test_x" name="test_b" time="0.02"/>
</testsuite></testsuites>"""

MIXED = """<testsuite name="pytest" tests="4">
<testcase classname="tests.test_x" name="test_pass" time="0.01"/>
<testcase classname="tests.test_x" name="test_fail" time="0.01">
  <failure message="assert 1 == 2">E  assert 1 == 2</failure>
</testcase>
<testcase classname="tests.test_x" name="test_err" time="0.01">
  <error message="fixture blew up">teardown</error>
</testcase>
<testcase classname="tests.test_x" name="test_skip" time="0.0">
  <skipped message="needs network"/>
</testcase>
</testsuite>"""


def test_parses_a_bare_testsuite_and_a_wrapped_one():
    assert len(parse_junit(PASSING)) == 2
    assert len(parse_junit(MIXED)) == 4


def test_parses_each_result_kind():
    by_name = {case.name: case for case in parse_junit(MIXED)}
    assert by_name["test_pass"].result == "passed"
    assert by_name["test_fail"].result == "failed"
    assert by_name["test_err"].result == "error"
    assert by_name["test_skip"].result == "skipped"
    assert "assert 1 == 2" in by_name["test_fail"].message


def test_empty_and_malformed_junit_raise():
    with pytest.raises(JunitParseError, match="empty"):
        parse_junit("   ")
    with pytest.raises(JunitParseError, match="malformed"):
        parse_junit("<testsuite><oops")


def test_oversized_junit_is_refused():
    with pytest.raises(JunitParseError, match="exceeds"):
        parse_junit("<testsuite/>" + "x" * (64 * 1024 * 1024))


# -- reading a whole run --------------------------------------------------


def _write(tmp_path, xml):
    path = tmp_path / "junit.xml"
    path.write_text(xml)
    return path


def test_every_requested_selector_gets_an_outcome(tmp_path):
    requested = {"tests/test_x.py::test_a": Bucket.F2P, "tests/test_x.py::test_b": Bucket.P2P}
    outcomes = adapter.parse(
        junit_path=_write(tmp_path, PASSING), exec_result=_exec(), requested=requested
    )
    assert [o.test_id for o in outcomes] == list(requested)
    assert all(o.status is TestStatus.PASSED for o in outcomes)


def test_statuses_come_from_the_file_not_the_exit_code(tmp_path):
    # A non-zero exit is normal at GUARDED, where f2p tests are meant to fail.
    requested = {
        "tests/test_x.py::test_pass": Bucket.P2P,
        "tests/test_x.py::test_fail": Bucket.F2P,
        "tests/test_x.py::test_err": Bucket.F2P,
        "tests/test_x.py::test_skip": Bucket.F2P,
    }
    outcomes = {
        o.test_id: o.status
        for o in adapter.parse(
            junit_path=_write(tmp_path, MIXED), exec_result=_exec(exit_code=1), requested=requested
        )
    }
    assert outcomes["tests/test_x.py::test_pass"] is TestStatus.PASSED
    assert outcomes["tests/test_x.py::test_fail"] is TestStatus.FAILED
    assert outcomes["tests/test_x.py::test_err"] is TestStatus.ERROR
    assert outcomes["tests/test_x.py::test_skip"] is TestStatus.SKIPPED


def test_a_selector_absent_from_the_results_is_not_found(tmp_path):
    outcomes = adapter.parse(
        junit_path=_write(tmp_path, PASSING),
        exec_result=_exec(exit_code=1),
        requested={"tests/test_x.py::test_gone": Bucket.F2P},
    )
    assert outcomes[0].status is TestStatus.NOT_FOUND


def test_a_timeout_is_a_timeout_not_a_failure(tmp_path):
    # Invariant 5: a container killed at the wall clock has said nothing about
    # the solution.
    outcomes = adapter.parse(
        junit_path=_write(tmp_path, PASSING),
        exec_result=_exec(timed_out=True),
        requested={"tests/test_x.py::test_a": Bucket.F2P},
    )
    assert outcomes[0].status is TestStatus.TIMEOUT


def test_a_missing_junit_file_is_infra_not_failure():
    outcomes = adapter.parse(
        junit_path=None,
        exec_result=_exec(exit_code=1, stderr="docker: OOM"),
        requested={"tests/test_x.py::test_a": Bucket.F2P},
    )
    assert outcomes[0].status is TestStatus.INFRA_ERROR
    assert "OOM" in outcomes[0].message


def test_a_malformed_junit_file_is_infra(tmp_path):
    outcomes = adapter.parse(
        junit_path=_write(tmp_path, "<broken"),
        exec_result=_exec(),
        requested={"tests/test_x.py::test_a": Bucket.F2P},
    )
    assert outcomes[0].status is TestStatus.INFRA_ERROR


def test_an_interrupted_run_reports_collection_error(tmp_path):
    # pytest exits 2 when collection breaks -- very often the solver's own
    # syntax error, which is worth telling apart from a failed assertion.
    outcomes = adapter.parse(
        junit_path=_write(tmp_path, "<testsuite/>"),
        exec_result=_exec(exit_code=2, stderr="ImportError"),
        requested={"tests/test_x.py::test_a": Bucket.F2P},
    )
    assert outcomes[0].status is TestStatus.COLLECTION_ERROR


def test_a_junit_collection_error_entry_is_classified(tmp_path):
    xml = """<testsuite><testcase classname="tests.test_x" name="tests/test_x.py">
      <error message="collection failure">ImportError: no module named foo</error>
    </testcase></testsuite>"""
    outcomes = adapter.parse(
        junit_path=_write(tmp_path, xml),
        exec_result=_exec(exit_code=2),
        requested={"tests/test_x.py::tests/test_x.py": Bucket.F2P},
    )
    assert outcomes[0].status is TestStatus.COLLECTION_ERROR


# -- registry -------------------------------------------------------------


def test_stub_adapters_expose_the_protocol_but_refuse_to_parse():
    stub = get_adapter("go")
    assert stub.smoke_argv() == ["go", "version"]
    assert stub.run_argv("go test {selectors} -out {out}", ["./x"], "/o.json")
    with pytest.raises(NotImplementedError, match="Only pytest"):
        stub.parse(junit_path=None, exec_result=_exec(), requested={})


def test_an_unknown_framework_is_a_bundle_error():
    with pytest.raises(BundleInvalidError) as caught:
        get_adapter("nosuchframework")
    # The fix must name what *is* supported, not just what is not.
    assert "pytest" in (caught.value.fix or "")
