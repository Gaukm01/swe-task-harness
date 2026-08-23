"""What must and must not invalidate a cached environment."""

from __future__ import annotations

from harness.core.bundle import load_bundle
from harness.core.cache import compute_cache_key, hash_build_context


def key_for(bundle):
    return compute_cache_key(load_bundle(bundle).spec, bundle)


def test_stable_for_an_unchanged_bundle(tiny_fixture):
    assert key_for(tiny_fixture) == key_for(tiny_fixture)


def test_editing_the_problem_statement_does_not_rebuild(bundle_copy):
    # The whole reason this is not bundle_digest: a prompt edit must be free, or
    # prompts stop getting edited.
    before = key_for(bundle_copy)
    (bundle_copy / "description.md").write_text("a completely different problem")
    assert key_for(bundle_copy) == before


def test_editing_the_test_patch_does_not_rebuild(bundle_copy):
    before = key_for(bundle_copy)
    (bundle_copy / "test_patch.diff").write_text(
        (bundle_copy / "test_patch.diff").read_text() + "\n# trailing\n"
    )
    assert key_for(bundle_copy) == before


def test_editing_the_gold_patch_does_not_rebuild(bundle_copy):
    before = key_for(bundle_copy)
    (bundle_copy / "patch.diff").write_text((bundle_copy / "patch.diff").read_text() + "\n")
    assert key_for(bundle_copy) == before


def test_editing_the_dockerfile_does_rebuild(bundle_copy):
    before = key_for(bundle_copy)
    (bundle_copy / "Dockerfile").write_text(
        (bundle_copy / "Dockerfile").read_text() + "\nENV EXTRA=1\n"
    )
    assert key_for(bundle_copy) != before


def test_editing_the_repo_does_rebuild(bundle_copy):
    # Missing this is the most confusing failure mode in a cached pipeline: the
    # edit appears to have had no effect at all.
    before = key_for(bundle_copy)
    source = bundle_copy / "repo" / "tinylib" / "intervals.py"
    source.write_text(source.read_text() + "\n# changed\n")
    assert key_for(bundle_copy) != before


def test_adding_a_repo_file_does_rebuild(bundle_copy):
    before = key_for(bundle_copy)
    (bundle_copy / "repo" / "tinylib" / "extra.py").write_text("X = 1\n")
    assert key_for(bundle_copy) != before


def test_the_harness_env_setup_version_is_part_of_the_key(tiny_fixture, monkeypatch):
    # Changing setup logic must invalidate old snapshots rather than silently
    # reuse them.
    before = key_for(tiny_fixture)
    monkeypatch.setattr("harness.core.cache.ENV_SETUP_VERSION", 999)
    assert key_for(tiny_fixture) != before


def test_a_different_base_commit_changes_the_key(tiny_fixture):
    spec = load_bundle(tiny_fixture).spec
    pinned = spec.model_copy(deep=True)
    pinned.repo = "https://example.com/x.git"
    pinned.base_commit = "b" * 40
    other = pinned.model_copy(deep=True)
    other.base_commit = "c" * 40
    assert compute_cache_key(pinned, tiny_fixture) != compute_cache_key(other, tiny_fixture)


def test_an_image_bundle_is_pinned_by_the_pulled_digest(tiny_fixture):
    spec = load_bundle(tiny_fixture).spec
    imaged = spec.model_copy(deep=True)
    imaged.environment.dockerfile = None
    imaged.environment.image = "python:3.11"
    one = compute_cache_key(imaged, tiny_fixture, base_image_digest="sha256:aaa")
    two = compute_cache_key(imaged, tiny_fixture, base_image_digest="sha256:bbb")
    # A republished mutable tag must not silently change what runs.
    assert one != two


def test_build_context_hash_ignores_task_only_files(bundle_copy):
    before = hash_build_context(bundle_copy)
    (bundle_copy / "description.md").write_text("different")
    (bundle_copy / "task.json").write_text((bundle_copy / "task.json").read_text() + "\n")
    assert hash_build_context(bundle_copy) == before
