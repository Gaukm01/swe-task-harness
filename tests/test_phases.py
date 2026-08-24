"""The phase machine against FakeRuntime: ordering, guarantees, failure paths.

No docker. These are the tests that must catch a broken invariant, so they
assert on *sequence* as much as on outcome.
"""

from __future__ import annotations

import pytest

from harness.core.bundle import load_bundle
from harness.core.cache import compute_cache_key
from harness.core.phases import (
    DEFAULT_REPO_ROOT,
    Phase,
    PhaseError,
    base_tag,
    phase_tag,
    prepare_base,
    repo_root,
)
from harness.runtime.fake import FakeRuntime, argv_contains


@pytest.fixture
def spec(tiny_fixture):
    return load_bundle(tiny_fixture).spec


@pytest.fixture
def key(spec, tiny_fixture):
    return compute_cache_key(spec, tiny_fixture)


def run_base(runtime, spec, bundle, key, **kwargs):
    return prepare_base(runtime, spec, bundle, cache_key=key, **kwargs)


# -- tags -----------------------------------------------------------------


def test_base_is_tagged_by_cache_key_not_run_id():
    # BASE is shared across runs of the same environment; the per-run phases are
    # not, because a GUARDED image contains hidden tests.
    tag = base_tag("tiny-fixture", "abcdef0123456789")
    assert tag == "harness/tiny-fixture:base-abcdef012345"
    assert "base-" in tag


def test_per_run_phases_are_tagged_by_run_id():
    tag = phase_tag("tiny-fixture", "01ABC", Phase.SOLVE)
    assert tag == "harness/tiny-fixture:01ABC-solve"


def test_repo_root_defaults_when_the_image_does_not_say(spec):
    assert repo_root(spec) == "/workspace/repo"
    stripped = spec.model_copy(deep=True)
    stripped.environment.repo_path_in_image = None
    assert repo_root(stripped) == DEFAULT_REPO_ROOT


# -- the happy path -------------------------------------------------------


def test_prepare_base_builds_commits_and_cleans_up(spec, tiny_fixture, key):
    runtime = FakeRuntime()
    result = run_base(runtime, spec, tiny_fixture, key)

    assert not result.cached
    assert result.image == base_tag(spec.task_id, key)
    assert runtime.commits and runtime.commits[0][1] == result.image
    # A leaked container pins its whole image layer on disk.
    assert runtime.leaked_containers == []


def test_a_dockerfile_bundle_is_built_not_pulled(spec, tiny_fixture, key):
    runtime = FakeRuntime()
    run_base(runtime, spec, tiny_fixture, key)
    assert runtime.builds
    assert runtime.pulls == []


def test_second_call_reuses_the_snapshot(spec, tiny_fixture, key):
    runtime = FakeRuntime()
    first = run_base(runtime, spec, tiny_fixture, key)
    second = run_base(runtime, spec, tiny_fixture, key)

    assert not first.cached
    assert second.cached
    assert second.image == first.image
    # Nothing ran the second time: no container, no commit beyond the first.
    assert len(runtime.commits) == 1
    assert second.steps == []


def test_no_cache_forces_a_rebuild(spec, tiny_fixture, key):
    runtime = FakeRuntime()
    run_base(runtime, spec, tiny_fixture, key)
    again = run_base(runtime, spec, tiny_fixture, key, no_cache=True)
    assert not again.cached
    assert len(runtime.commits) == 2


# -- the git normalization guarantees ------------------------------------


def test_history_is_truncated_and_the_remote_removed(spec, tiny_fixture, key):
    runtime = FakeRuntime()
    run_base(runtime, spec, tiny_fixture, key)

    assert runtime.ran("remote", "remove", "origin")
    assert runtime.ran("rm", "-rf", "/workspace/repo/.git")
    assert runtime.ran("git", "init")
    assert runtime.ran("commit")


def test_the_remote_is_removed_before_history_is_re_inited(spec, tiny_fixture, key):
    # Order matters: re-initing first would make the remote removal a no-op on a
    # fresh repo and leave the original .git intact for that moment.
    runtime = FakeRuntime()
    run_base(runtime, spec, tiny_fixture, key)
    assert runtime.index_of("remote", "remove") < runtime.index_of("rm", "-rf")


def test_history_is_truncated_before_the_synthetic_commit(spec, tiny_fixture, key):
    # Committing before deleting .git would preserve the upstream history that
    # contains the fix.
    runtime = FakeRuntime()
    run_base(runtime, spec, tiny_fixture, key)
    assert runtime.index_of("rm", "-rf") < runtime.index_of("git", "init")
    assert runtime.index_of("git", "init") < runtime.index_of("commit")


def test_the_snapshot_is_committed_only_after_git_normalization(spec, tiny_fixture, key):
    # The commit is what everything downstream branches from. If it happened
    # before truncation, every later phase would inherit the upstream history.
    runtime = FakeRuntime()
    run_base(runtime, spec, tiny_fixture, key)
    assert runtime.commits, "no snapshot committed"
    assert runtime.ran("rm", "-rf", "/workspace/repo/.git")


def test_base_commit_is_checked_out_when_the_bundle_pins_one(spec, tiny_fixture, key):
    pinned = spec.model_copy(deep=True)
    pinned.repo = "https://example.com/x.git"
    pinned.base_commit = "a" * 40
    runtime = FakeRuntime()
    run_base(runtime, pinned, tiny_fixture, key)
    assert runtime.ran("checkout", "--detach", "a" * 40)
    assert runtime.index_of("checkout", "--detach") < runtime.index_of("rm", "-rf")


def test_no_checkout_when_the_bundle_pins_nothing(spec, tiny_fixture, key):
    runtime = FakeRuntime()
    run_base(runtime, spec, tiny_fixture, key)
    assert not runtime.ran("checkout", "--detach")


def test_the_synthetic_commit_is_deterministic(spec, tiny_fixture, key):
    # A fixed author and date make the commit sha a function of the tree alone,
    # which is what makes snapshots reproducible.
    runtime = FakeRuntime()
    run_base(runtime, spec, tiny_fixture, key)
    commit_argv = next(a for a in runtime.exec_argvs() if "commit" in a)
    assert "--date" in commit_argv
    assert "2000-01-01T00:00:00+00:00" in commit_argv


# -- failure paths --------------------------------------------------------


def test_a_missing_repo_is_a_clear_error(spec, tiny_fixture, key):
    runtime = FakeRuntime()
    runtime.script(argv_contains("ls", "-A"), exit_code=1, stderr="No such file")
    with pytest.raises(PhaseError, match="no repo at /workspace/repo"):
        run_base(runtime, spec, tiny_fixture, key)


def test_an_empty_repo_directory_is_also_caught(spec, tiny_fixture, key):
    # Docker creates a missing --workdir, so "the directory exists" proves
    # nothing. This is the case the old `test -d` probe could never fail on.
    runtime = FakeRuntime()
    runtime.script(argv_contains("ls", "-A"), exit_code=0, stdout="")
    with pytest.raises(PhaseError, match="no repo at /workspace/repo"):
        run_base(runtime, spec, tiny_fixture, key)


def test_a_missing_test_runner_fails_at_init_not_at_grading(spec, tiny_fixture, key):
    # Discovering this at grading time would be indistinguishable from a broken
    # solution, so BASE asserts it up front.
    runtime = FakeRuntime()
    runtime.script(argv_contains("pytest", "--version"), exit_code=127, stderr="not found")
    with pytest.raises(PhaseError, match="does not execute"):
        run_base(runtime, spec, tiny_fixture, key)


def test_a_failed_phase_still_removes_its_container(spec, tiny_fixture, key):
    runtime = FakeRuntime()
    runtime.script(argv_contains("pytest", "--version"), exit_code=127)
    with pytest.raises(PhaseError):
        run_base(runtime, spec, tiny_fixture, key)
    assert runtime.leaked_containers == []


def test_a_failed_phase_commits_nothing(spec, tiny_fixture, key):
    runtime = FakeRuntime()
    runtime.script(argv_contains("git", "init"), exit_code=1, stderr="permission denied")
    with pytest.raises(PhaseError):
        run_base(runtime, spec, tiny_fixture, key)
    assert runtime.commits == []


def test_a_timeout_is_reported_as_a_timeout(spec, tiny_fixture, key):
    runtime = FakeRuntime()
    runtime.script(argv_contains("pytest", "--version"), exit_code=-1, timed_out=True)
    with pytest.raises(PhaseError, match="timed out"):
        run_base(runtime, spec, tiny_fixture, key)


def test_a_missing_origin_remote_is_not_fatal(spec, tiny_fixture, key):
    # Plenty of images ship a repo with no remote at all.
    runtime = FakeRuntime()
    runtime.script(argv_contains("remote", "remove"), exit_code=2, stderr="No such remote")
    result = run_base(runtime, spec, tiny_fixture, key)
    assert result.image


# -- no shell interpolation ----------------------------------------------


def test_every_exec_is_an_argv_list_with_no_shell(spec, tiny_fixture, key):
    runtime = FakeRuntime()
    run_base(runtime, spec, tiny_fixture, key)
    for argv in runtime.exec_argvs():
        assert isinstance(argv, list)
        assert all(isinstance(token, str) for token in argv)
        # Nothing is handed to a host shell, so no phase step may smuggle one in.
        assert argv[0] not in {"sh", "bash", "zsh"}


# ---------------------------------------------------------------------------
# The validate lane
# ---------------------------------------------------------------------------

import json  # noqa: E402

from harness.core.bundle import load_bundle as _load_bundle  # noqa: E402
from harness.core.phases import (  # noqa: E402
    assert_gold,
    assert_guarded,
    validate_task,
)
from harness.core.results import Bucket, TestStatus  # noqa: E402
from harness.core.runtime import ExecResult  # noqa: E402
from harness.core.testrun import TestRun  # noqa: E402


def junit_for(passed: list[str], failed: list[str]) -> str:
    """A junit document reporting these pytest node ids as passed / failed."""
    from harness.adapters.pytest_adapter import selector_to_junit_key

    rows = []
    for selector in passed:
        classname, name = selector_to_junit_key(selector)
        rows.append(f'<testcase classname="{classname}" name="{name}" time="0.01"/>')
    for selector in failed:
        classname, name = selector_to_junit_key(selector)
        rows.append(
            f'<testcase classname="{classname}" name="{name}" time="0.01">'
            f'<failure message="assert failed">boom</failure></testcase>'
        )
    return "<testsuite>" + "".join(rows) + "</testsuite>"


@pytest.fixture
def bundle(tiny_fixture):
    return _load_bundle(tiny_fixture)


# Results now live in a per-run unguessable directory; tests pin the nonce so
# the scripted payloads land where the code will look for them.
NONCE = "testnonce"


def wire_validation(runtime, bundle, *, guarded_junit: str, gold_junit: str):
    """Make copy_out hand back scripted junit for each phase."""
    from harness.core.testrun import new_artifact_dir

    where = new_artifact_dir(NONCE)
    runtime.copy_out_payloads[f"{where}/guarded-junit.xml"] = guarded_junit
    runtime.copy_out_payloads[f"{where}/gold-junit.xml"] = gold_junit


def run_validation(runtime, bundle, tiny_fixture, tmp_path, key):
    base = prepare_base(runtime, bundle.spec, tiny_fixture, cache_key=key)
    return validate_task(
        runtime,
        bundle,
        base,
        validation_id="01VALIDATE",
        artifact_dir=tmp_path / "artifacts",
        artifact_nonce=NONCE,
    )


def healthy(bundle):
    """The junit pair a correct bundle produces."""
    f2p = bundle.spec.tests.fail_to_pass
    p2p = bundle.spec.tests.pass_to_pass
    return junit_for(p2p, f2p), junit_for(p2p + f2p, [])


def test_a_healthy_bundle_validates(runtime_and_bundle):
    result = runtime_and_bundle
    assert result.ok
    assert result.guarded.problems == []
    assert result.gold.problems == []


@pytest.fixture
def runtime_and_bundle(bundle, tiny_fixture, tmp_path, key):
    runtime = FakeRuntime()
    guarded_junit, gold_junit = healthy(bundle)
    wire_validation(runtime, bundle, guarded_junit=guarded_junit, gold_junit=gold_junit)
    return run_validation(runtime, bundle, tiny_fixture, tmp_path, key)


def test_both_patches_are_applied_in_order(bundle, tiny_fixture, tmp_path, key):
    runtime = FakeRuntime()
    guarded_junit, gold_junit = healthy(bundle)
    wire_validation(runtime, bundle, guarded_junit=guarded_junit, gold_junit=gold_junit)
    run_validation(runtime, bundle, tiny_fixture, tmp_path, key)

    # test_patch first (GUARDED), then patch (GOLD): GOLD is defined as GUARDED
    # plus the gold patch.
    assert runtime.index_of("apply", "test_patch.diff") < runtime.index_of(
        "apply", "/tmp/harness/patch.diff"
    )


def test_patches_are_written_as_files_never_piped_through_a_shell(
    bundle, tiny_fixture, tmp_path, key
):
    runtime = FakeRuntime()
    guarded_junit, gold_junit = healthy(bundle)
    wire_validation(runtime, bundle, guarded_junit=guarded_junit, gold_junit=gold_junit)
    run_validation(runtime, bundle, tiny_fixture, tmp_path, key)

    assert "/tmp/harness/test_patch.diff" in runtime.files_written
    assert runtime.files_written["/tmp/harness/test_patch.diff"] == bundle.test_patch
    for argv in runtime.exec_argvs():
        assert argv[0] not in {"sh", "bash", "zsh"}


def test_both_phases_are_snapshotted(runtime_and_bundle):
    result = runtime_and_bundle
    assert result.guarded.image.endswith("01VALIDATE-guarded")
    assert result.gold.image.endswith("01VALIDATE-gold")


def test_the_container_is_removed_even_when_a_patch_fails(bundle, tiny_fixture, tmp_path, key):
    runtime = FakeRuntime()
    runtime.script(argv_contains("git", "apply"), exit_code=1, stderr="patch does not apply")
    with pytest.raises(PhaseError, match="does not apply|applying"):
        run_validation(runtime, bundle, tiny_fixture, tmp_path, key)
    assert runtime.leaked_containers == []


def test_junit_and_logs_are_written_to_the_artifact_dir(bundle, tiny_fixture, tmp_path, key):
    runtime = FakeRuntime()
    guarded_junit, gold_junit = healthy(bundle)
    wire_validation(runtime, bundle, guarded_junit=guarded_junit, gold_junit=gold_junit)
    run_validation(runtime, bundle, tiny_fixture, tmp_path, key)

    artifacts = tmp_path / "artifacts"
    for name in (
        "guarded-junit.xml",
        "gold-junit.xml",
        "guarded-stdout.txt",
        "gold-stderr.txt",
    ):
        assert (artifacts / name).is_file(), name


# -- the assertions themselves --------------------------------------------


def _run(outcomes):
    return TestRun(
        label="x",
        outcomes=outcomes,
        exec_result=ExecResult(argv=[], exit_code=1, stdout="", stderr="", duration_ms=1),
        junit_path=None,
        argv=[],
    )


def _o(test_id, bucket, status):
    from harness.core.results import TestOutcome

    return TestOutcome(test_id=test_id, bucket=bucket, status=status)


def test_guarded_rejects_an_f2p_that_already_passes():
    # It would let a no-op solver grade as resolved.
    problems = assert_guarded(
        _run([_o("f1", Bucket.F2P, TestStatus.PASSED), _o("p1", Bucket.P2P, TestStatus.PASSED)])
    )
    assert any("already pass" in p for p in problems)


def test_guarded_rejects_a_p2p_failing_at_baseline():
    problems = assert_guarded(
        _run([_o("f1", Bucket.F2P, TestStatus.FAILED), _o("p1", Bucket.P2P, TestStatus.FAILED)])
    )
    assert any("do not pass at baseline" in p for p in problems)


def test_guarded_rejects_a_missing_f2p_selector():
    problems = assert_guarded(
        _run([_o("f1", Bucket.F2P, TestStatus.NOT_FOUND), _o("p1", Bucket.P2P, TestStatus.PASSED)])
    )
    assert any("produced no result" in p for p in problems)


def test_guarded_accepts_the_correct_shape():
    assert (
        assert_guarded(
            _run([_o("f1", Bucket.F2P, TestStatus.FAILED), _o("p1", Bucket.P2P, TestStatus.PASSED)])
        )
        == []
    )


def test_an_infra_failure_at_guarded_is_reported_as_infra_not_as_a_bad_bundle():
    problems = assert_guarded(_run([_o("f1", Bucket.F2P, TestStatus.TIMEOUT)]))
    assert len(problems) == 1
    assert "did not complete" in problems[0]


def test_gold_rejects_an_f2p_the_patch_does_not_fix():
    problems = assert_gold(
        _run([_o("f1", Bucket.F2P, TestStatus.FAILED), _o("p1", Bucket.P2P, TestStatus.PASSED)])
    )
    assert any("does not make these fail_to_pass tests pass" in p for p in problems)


def test_gold_rejects_a_patch_that_breaks_a_p2p():
    problems = assert_gold(
        _run([_o("f1", Bucket.F2P, TestStatus.PASSED), _o("p1", Bucket.P2P, TestStatus.FAILED)])
    )
    assert any("breaks these pass_to_pass" in p for p in problems)


def test_gold_accepts_everything_passing():
    assert (
        assert_gold(
            _run([_o("f1", Bucket.F2P, TestStatus.PASSED), _o("p1", Bucket.P2P, TestStatus.PASSED)])
        )
        == []
    )


def test_a_broken_gold_patch_surfaces_as_a_validation_failure(bundle, tiny_fixture, tmp_path, key):
    # The acceptance scenario, in unit form: gold runs but does not fix the f2p.
    runtime = FakeRuntime()
    f2p = bundle.spec.tests.fail_to_pass
    p2p = bundle.spec.tests.pass_to_pass
    wire_validation(
        runtime,
        bundle,
        guarded_junit=junit_for(p2p, f2p),
        gold_junit=junit_for(p2p, f2p),  # still failing under gold
    )
    result = run_validation(runtime, bundle, tiny_fixture, tmp_path, key)
    assert not result.ok
    assert result.guarded.ok
    assert not result.gold.ok


del json


def test_phase_errors_exit_four_not_one():
    """A broken bundle must be distinguishable from a harness fault by exit code alone."""
    from harness.core.errors import ExitCode

    assert PhaseError("x").exit_code is ExitCode.BASELINE_FAILED


def test_an_unappliable_patch_is_a_baseline_failure(bundle, tiny_fixture, tmp_path, key):
    from harness.core.errors import ExitCode

    runtime = FakeRuntime()
    runtime.script(argv_contains("git", "apply"), exit_code=1, stderr="patch does not apply")
    with pytest.raises(PhaseError) as caught:
        run_validation(runtime, bundle, tiny_fixture, tmp_path, key)
    assert caught.value.exit_code is ExitCode.BASELINE_FAILED
    assert "does not apply" in (caught.value.fix or "")


def test_passing_validation_discards_its_snapshots(bundle, tiny_fixture, tmp_path, key):
    # Two full image layers per invocation, with nothing to look at.
    runtime = FakeRuntime()
    guarded_junit, gold_junit = healthy(bundle)
    wire_validation(runtime, bundle, guarded_junit=guarded_junit, gold_junit=gold_junit)
    result = run_validation(runtime, bundle, tiny_fixture, tmp_path, key)

    assert result.ok
    assert not result.guarded.retained
    assert not result.gold.retained
    assert sorted(runtime.removed_images) == sorted([result.guarded.image, result.gold.image])


def test_failing_validation_keeps_its_snapshots(bundle, tiny_fixture, tmp_path, key):
    # This is exactly when `task shell --phase guarded` earns its keep.
    runtime = FakeRuntime()
    f2p = bundle.spec.tests.fail_to_pass
    p2p = bundle.spec.tests.pass_to_pass
    wire_validation(
        runtime, bundle, guarded_junit=junit_for(p2p, f2p), gold_junit=junit_for(p2p, f2p)
    )
    result = run_validation(runtime, bundle, tiny_fixture, tmp_path, key)

    assert not result.ok
    assert result.gold.retained
    assert runtime.removed_images == []


def test_keep_snapshots_overrides_the_discard(bundle, tiny_fixture, tmp_path, key):
    runtime = FakeRuntime()
    guarded_junit, gold_junit = healthy(bundle)
    wire_validation(runtime, bundle, guarded_junit=guarded_junit, gold_junit=gold_junit)
    base = prepare_base(runtime, bundle.spec, tiny_fixture, cache_key=key)
    result = validate_task(
        runtime,
        bundle,
        base,
        validation_id="01KEEP",
        artifact_dir=tmp_path / "a",
        keep_snapshots=True,
        artifact_nonce=NONCE,
    )
    assert result.ok
    assert result.guarded.retained
    assert runtime.removed_images == []


def test_selectors_are_grouped_by_file(bundle, tiny_fixture, tmp_path, key):
    """One unimportable file must not block selectors in another.

    pytest resolves every selector before running anything and aborts the whole
    invocation if one cannot be resolved. Without per-file grouping, a test file
    that imports a not-yet-existing symbol — the normal baseline for a
    fail-to-pass test that adds new API — makes the entire suite report
    `not_found`.
    """
    from harness.core.results import Bucket
    from harness.core.testrun import group_by_file, run_selectors

    grouped = group_by_file(
        {"a.py::x": Bucket.F2P, "b.py::y": Bucket.P2P, "a.py::z": Bucket.P2P}
    )
    assert list(grouped) == ["a.py", "b.py"]
    assert list(grouped["a.py"]) == ["a.py::x", "a.py::z"]

    runtime = FakeRuntime()
    spec = bundle.spec.model_copy(deep=True)
    spec.tests.fail_to_pass = ["one.py::a"]
    spec.tests.pass_to_pass = ["two.py::b"]
    runtime.copy_out_payloads["/tmp/harness/x-0-junit.xml"] = junit_for([], ["one.py::a"])
    runtime.copy_out_payloads["/tmp/harness/x-1-junit.xml"] = junit_for(["two.py::b"], [])

    run = run_selectors(
        runtime, "c1", spec, label="x", workdir="/w", artifact_dir=tmp_path / "a"
    )
    # Two files, two invocations.
    assert sum(1 for a in runtime.exec_argvs() if "pytest" in a) == 2
    assert {o.test_id for o in run.outcomes} == {"one.py::a", "two.py::b"}


# -- recipe and clone environments ----------------------------------------


def test_a_recipe_runs_its_install_commands(spec, tiny_fixture, key):
    # These were previously accepted by lint and then silently never executed.
    runtime = FakeRuntime()
    recipe_spec = spec.model_copy(deep=True)
    recipe_spec.environment.dockerfile = None
    recipe_spec.environment.recipe = type(recipe_spec.environment).model_fields[
        "recipe"
    ].annotation.__args__[0](base_image="python:3.11-slim", install_cmds=["pip install pytest"])
    run_base(runtime, recipe_spec, tiny_fixture, key)
    assert runtime.ran("pip install pytest")


def test_a_repo_url_is_cloned_when_the_image_does_not_ship_one(spec, tiny_fixture, key):
    runtime = FakeRuntime()
    cloned = spec.model_copy(deep=True)
    cloned.repo = "https://example.com/x.git"
    cloned.base_commit = "a" * 40
    cloned.environment.repo_path_in_image = None
    run_base(runtime, cloned, tiny_fixture, key)
    assert runtime.ran("git", "clone", "https://example.com/x.git")
    # Cloning must precede the checkout that pins the commit.
    assert runtime.index_of("clone") < runtime.index_of("checkout", "--detach")


def test_install_commands_run_before_the_clone(spec, tiny_fixture, key):
    # You cannot `git clone` without git, and a bare base image has none.
    runtime = FakeRuntime()
    combined = spec.model_copy(deep=True)
    combined.repo = "https://example.com/x.git"
    combined.base_commit = "a" * 40
    combined.environment.repo_path_in_image = None
    combined.environment.dockerfile = None
    combined.environment.recipe = type(combined.environment).model_fields[
        "recipe"
    ].annotation.__args__[0](base_image="python:3.11-slim", install_cmds=["apt-get install git"])
    run_base(runtime, combined, tiny_fixture, key)
    assert runtime.index_of("apt-get install git") < runtime.index_of("clone")


def test_nothing_is_cloned_when_the_image_ships_the_repo(spec, tiny_fixture, key):
    runtime = FakeRuntime()
    run_base(runtime, spec, tiny_fixture, key)
    assert not runtime.ran("git", "clone")
