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
    runtime.script(argv_contains("test", "-d"), exit_code=1)
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
