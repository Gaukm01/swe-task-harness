"""Bundle schema and lint rules, exercised against the real tiny fixture."""

from __future__ import annotations

import pytest

from harness.core.bundle import TaskSpec, compute_bundle_digest, lint_bundle, load_bundle
from harness.core.checks import CheckStatus
from harness.core.errors import BundleInvalidError, ExitCode

# -- the fixture itself is the reference bundle ---------------------------


def test_the_tiny_fixture_lints_clean(tiny_fixture):
    report = lint_bundle(tiny_fixture)
    assert report.is_clean, [c.model_dump() for c in report.failures]
    assert report.exit_code() is ExitCode.OK


def test_the_tiny_fixture_has_no_warnings(tiny_fixture):
    # The fixture is the reference bundle; anything it warns about would teach
    # an author the wrong lesson.
    report = lint_bundle(tiny_fixture)
    assert report.warnings == [], [c.model_dump() for c in report.warnings]


def test_load_bundle_reads_every_part(tiny_fixture):
    bundle = load_bundle(tiny_fixture)
    assert bundle.spec.task_id == "tiny-fixture"
    assert bundle.spec.tests.framework == "pytest"
    assert len(bundle.spec.tests.fail_to_pass) == 2
    assert len(bundle.spec.tests.pass_to_pass) == 4
    assert "merge()" in bundle.description
    assert bundle.patch.startswith("diff --git")
    assert bundle.test_patch.startswith("diff --git")
    assert len(bundle.digest) == 64


def test_selectors_property_puts_f2p_first(tiny_fixture):
    spec = load_bundle(tiny_fixture).spec
    assert spec.tests.selectors[: len(spec.tests.fail_to_pass)] == spec.tests.fail_to_pass


# -- digest ---------------------------------------------------------------


def test_digest_is_stable(tiny_fixture):
    assert compute_bundle_digest(tiny_fixture) == compute_bundle_digest(tiny_fixture)


def test_digest_changes_with_any_content(bundle_copy):
    before = compute_bundle_digest(bundle_copy)
    (bundle_copy / "description.md").write_text("something else")
    assert compute_bundle_digest(bundle_copy) != before


def test_digest_ignores_editor_droppings(bundle_copy):
    before = compute_bundle_digest(bundle_copy)
    (bundle_copy / ".DS_Store").write_bytes(b"\x00junk")
    (bundle_copy / "__pycache__").mkdir()
    (bundle_copy / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    assert compute_bundle_digest(bundle_copy) == before


def test_digest_is_sensitive_to_renames(bundle_copy):
    before = compute_bundle_digest(bundle_copy)
    (bundle_copy / "repo" / "tinylib" / "intervals.py").rename(
        bundle_copy / "repo" / "tinylib" / "renamed.py"
    )
    assert compute_bundle_digest(bundle_copy) != before


# -- schema validation ----------------------------------------------------


def _spec(**overrides):
    base = {
        "task_id": "ok-id",
        "language": "python",
        "environment": {"image": "python@sha256:abc", "repo_path_in_image": "/app"},
        "tests": {
            "framework": "pytest",
            "run_cmd_template": "pytest {selectors} --junitxml={out}",
            "fail_to_pass": ["tests/t.py::a"],
            "pass_to_pass": ["tests/t.py::b"],
            "test_path_globs": ["tests/**"],
        },
    }
    base.update(overrides)
    return base


def test_valid_minimal_spec():
    assert TaskSpec.model_validate(_spec()).task_id == "ok-id"


@pytest.mark.parametrize("bad_id", ["Tiny-Fixture", "tiny fixture", "-leading", "tiny/fixture", ""])
def test_task_id_must_be_usable_as_a_docker_tag(bad_id):
    # task_id is interpolated into harness/<task_id>:<run_id>-<phase>.
    with pytest.raises(ValueError, match="docker image tag|String should"):
        TaskSpec.model_validate(_spec(task_id=bad_id))


def test_environment_requires_exactly_one_source():
    with pytest.raises(ValueError, match="exactly one"):
        TaskSpec.model_validate(
            _spec(environment={"image": "x", "dockerfile": "D", "repo_path_in_image": "/app"})
        )
    with pytest.raises(ValueError, match="exactly one"):
        TaskSpec.model_validate(_spec(environment={"repo_path_in_image": "/app"}))


def test_repo_requires_a_base_commit():
    with pytest.raises(ValueError, match="base_commit is required"):
        TaskSpec.model_validate(_spec(repo="https://example.com/x.git"))


def test_a_bundle_that_neither_clones_nor_ships_a_repo_is_rejected():
    with pytest.raises(ValueError, match="repo_path_in_image"):
        TaskSpec.model_validate(_spec(environment={"image": "python@sha256:abc"}))


def test_fail_to_pass_must_not_be_empty():
    spec = _spec()
    spec["tests"]["fail_to_pass"] = []
    with pytest.raises(ValueError, match="fail_to_pass must not be empty"):
        TaskSpec.model_validate(spec)


def test_selectors_may_not_appear_in_both_buckets():
    spec = _spec()
    spec["tests"]["pass_to_pass"] = ["tests/t.py::a"]
    with pytest.raises(ValueError, match="both fail_to_pass and pass_to_pass"):
        TaskSpec.model_validate(spec)


@pytest.mark.parametrize("template", ["pytest {selectors}", "pytest --junitxml={out}", "pytest"])
def test_run_cmd_template_must_produce_structured_output(template):
    # No test status is ever parsed from stdout, so a template with no {out} is
    # unusable and a template with no {selectors} would run the whole suite.
    spec = _spec()
    spec["tests"]["run_cmd_template"] = template
    with pytest.raises(ValueError, match="run_cmd_template must contain"):
        TaskSpec.model_validate(spec)


def test_test_path_globs_must_not_be_empty():
    spec = _spec()
    spec["tests"]["test_path_globs"] = []
    with pytest.raises(ValueError, match="test_path_globs must not be empty"):
        TaskSpec.model_validate(spec)


def test_unknown_fields_are_rejected():
    # A typo in a hand-authored task.json must not be silently ignored.
    with pytest.raises(ValueError, match="Extra inputs"):
        TaskSpec.model_validate(_spec(timeout_s=30))


# -- lint failures --------------------------------------------------------


def _statuses(report):
    return {c.name: c.status for c in report.checks}


def test_lint_reports_a_missing_directory(tmp_path):
    report = lint_bundle(tmp_path / "nope")
    assert report.exit_code() is ExitCode.BUNDLE_INVALID


def test_lint_reports_every_missing_file(tmp_path):
    (tmp_path / "empty").mkdir()
    report = lint_bundle(tmp_path / "empty")
    failure = next(c for c in report.checks if c.name == "required files")
    for name in ("task.json", "description.md", "patch.diff", "test_patch.diff"):
        assert name in failure.detail


def test_lint_reports_broken_json(bundle_copy):
    (bundle_copy / "task.json").write_text("{ not json")
    report = lint_bundle(bundle_copy)
    assert not report.is_clean
    assert any(c.name == "task.json syntax" for c in report.failures)


def test_lint_collects_all_schema_errors_at_once(bundle_copy):
    # An author fixing a bundle wants the whole list, not one problem per run.
    (bundle_copy / "task.json").write_text('{"task_id": "x"}')
    report = lint_bundle(bundle_copy)
    assert len(report.failures) >= 3


def test_lint_rejects_an_empty_description(bundle_copy):
    (bundle_copy / "description.md").write_text("   \n")
    report = lint_bundle(bundle_copy)
    assert _statuses(report)["description.md"] is CheckStatus.FAIL


def test_lint_rejects_a_patch_that_is_not_a_diff(bundle_copy):
    (bundle_copy / "patch.diff").write_text("Fix the merge bug\n")
    report = lint_bundle(bundle_copy)
    assert _statuses(report)["patch.diff"] is CheckStatus.FAIL


def test_lint_rejects_a_missing_dockerfile(bundle_copy):
    (bundle_copy / "Dockerfile").unlink()
    report = lint_bundle(bundle_copy)
    assert _statuses(report)["environment.dockerfile"] is CheckStatus.FAIL


def test_lint_rejects_a_gold_patch_that_touches_tests(bundle_copy):
    # A gold patch that edits a test file is smuggling in the assertions it is
    # meant to satisfy.
    combined = (bundle_copy / "patch.diff").read_text() + (
        bundle_copy / "test_patch.diff"
    ).read_text()
    (bundle_copy / "patch.diff").write_text(combined)
    report = lint_bundle(bundle_copy)
    assert _statuses(report)["gold patch scope"] is CheckStatus.FAIL


def test_lint_rejects_a_test_patch_that_touches_source(bundle_copy):
    # Force-restore resets only paths matching test_path_globs, so a test patch
    # reaching outside them would silently lose changes at grading time.
    combined = (bundle_copy / "test_patch.diff").read_text() + (
        bundle_copy / "patch.diff"
    ).read_text()
    (bundle_copy / "test_patch.diff").write_text(combined)
    report = lint_bundle(bundle_copy)
    assert _statuses(report)["test patch scope"] is CheckStatus.FAIL


def test_lint_warns_about_an_unpinned_image(edit_task_json):
    bundle = edit_task_json(
        **{"environment": {"image": "python:3.11", "repo_path_in_image": "/app"}}
    )
    report = lint_bundle(bundle)
    assert _statuses(report)["environment.image"] is CheckStatus.WARN
    assert report.is_clean  # a warning must not block


def test_lint_warns_about_an_empty_pass_to_pass(edit_task_json):
    bundle = edit_task_json(**{"tests.pass_to_pass": []})
    report = lint_bundle(bundle)
    assert _statuses(report)["pass_to_pass"] is CheckStatus.WARN
    assert report.is_clean


def test_lint_warns_about_a_stubbed_framework(edit_task_json):
    bundle = edit_task_json(
        **{
            "tests.framework": "jest",
            "tests.run_cmd_template": "jest {selectors} --json --outputFile={out}",
        }
    )
    report = lint_bundle(bundle)
    assert _statuses(report)["test framework"] is CheckStatus.WARN


def test_lint_warns_when_a_f2p_selector_is_not_in_the_test_patch(edit_task_json):
    bundle = edit_task_json(**{"tests.fail_to_pass": ["tests/other.py::test_x"]})
    report = lint_bundle(bundle)
    assert _statuses(report)["fail_to_pass provenance"] is CheckStatus.WARN


# -- load_bundle stops at the first problem -------------------------------


def test_load_bundle_rejects_a_non_directory(tmp_path):
    with pytest.raises(BundleInvalidError) as caught:
        load_bundle(tmp_path / "missing")
    assert caught.value.exit_code is ExitCode.BUNDLE_INVALID


def test_load_bundle_reports_missing_files(tmp_path):
    (tmp_path / "b").mkdir()
    with pytest.raises(BundleInvalidError, match="missing"):
        load_bundle(tmp_path / "b")


def test_load_bundle_reports_bad_json_with_a_position(bundle_copy):
    (bundle_copy / "task.json").write_text("{\n  broken\n}")
    with pytest.raises(BundleInvalidError) as caught:
        load_bundle(bundle_copy)
    assert "line" in (caught.value.fix or "")
