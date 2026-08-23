"""The one integration test: a real BASE snapshot of the tiny fixture.

Marked `docker` and excluded from the default suite. Run with:

    uv run pytest -m docker

Everything else about the phase machine is covered against FakeRuntime in
milliseconds; this exists to prove the DockerRuntime translation layer is real
and that the fixture image genuinely satisfies BASE's assertions.
"""

from __future__ import annotations

import pytest

from harness.core.bundle import load_bundle
from harness.core.cache import compute_cache_key
from harness.core.phases import base_tag, prepare_base, repo_root
from harness.runtime.docker import DockerRuntime

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def runtime():
    docker = DockerRuntime()
    if not docker.available():
        pytest.skip("docker daemon not available")
    return docker


@pytest.fixture(scope="module")
def built(runtime, request):
    bundle = request.config.rootpath / "examples" / "tiny-fixture"
    spec = load_bundle(bundle).spec
    key = compute_cache_key(spec, bundle)
    result = prepare_base(runtime, spec, bundle, cache_key=key)
    yield spec, bundle, key, result
    runtime.remove_image(base_tag(spec.task_id, key), force=True)
    runtime.remove_image(f"{base_tag(spec.task_id, key)}-src", force=True)


def test_base_snapshot_exists(runtime, built):
    spec, _, key, result = built
    assert result.image == base_tag(spec.task_id, key)
    assert runtime.image_exists(result.image)


def test_the_snapshot_has_exactly_one_commit(runtime, built):
    """History truncation is the guarantee; this is what proves it."""
    spec, _, _, result = built
    from harness.core.runtime import ContainerSpec

    cid = runtime.create(ContainerSpec(image=result.image, workdir=repo_root(spec)))
    try:
        log = runtime.exec(cid, ["git", "-C", repo_root(spec), "rev-list", "--count", "HEAD"])
        assert log.ok, log.stderr
        assert log.stdout.strip() == "1", f"expected a single synthetic commit, got {log.stdout!r}"

        remotes = runtime.exec(cid, ["git", "-C", repo_root(spec), "remote"])
        assert remotes.stdout.strip() == "", f"a remote survived: {remotes.stdout!r}"

        status = runtime.exec(cid, ["git", "-C", repo_root(spec), "status", "--porcelain"])
        assert status.stdout.strip() == "", "the tree should be clean at BASE"
    finally:
        runtime.remove_container(cid, force=True)


def test_the_test_runner_executes_in_the_snapshot(runtime, built):
    from harness.core.runtime import ContainerSpec

    spec, _, _, result = built
    cid = runtime.create(ContainerSpec(image=result.image, workdir=repo_root(spec)))
    try:
        probe = runtime.exec(cid, ["python", "-m", "pytest", "--version"])
        assert probe.ok, probe.stderr
    finally:
        runtime.remove_container(cid, force=True)


def test_reusing_the_snapshot_runs_nothing(runtime, built):
    spec, bundle, key, _ = built
    again = prepare_base(runtime, spec, bundle, cache_key=key)
    assert again.cached
    assert again.steps == []


# ---------------------------------------------------------------------------
# The validate lane against a real container
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def validated(runtime, built, tmp_path_factory):
    from harness.core.bundle import load_bundle as _load
    from harness.core.ids import new_ulid
    from harness.core.phases import Phase, phase_tag, validate_task

    spec, bundle_dir, _key, base = built
    bundle = _load(bundle_dir)
    validation_id = new_ulid()
    artifacts = tmp_path_factory.mktemp("validate")
    result = validate_task(
        runtime, bundle, base, validation_id=validation_id, artifact_dir=artifacts
    )
    yield result, artifacts

    for phase in (Phase.GUARDED, Phase.GOLD):
        runtime.remove_image(phase_tag(spec.task_id, validation_id, phase), force=True)


def test_the_fixture_validates_against_real_pytest(validated):
    result, _ = validated
    assert result.ok, result.problems


def test_guarded_shows_f2p_failing_and_p2p_passing(validated):
    from harness.core.results import Bucket

    result, _ = validated
    outcomes = result.guarded.outcomes
    assert all(not o.passed for o in outcomes if o.bucket is Bucket.F2P)
    assert all(o.passed for o in outcomes if o.bucket is Bucket.P2P)


def test_gold_shows_everything_passing(validated):
    result, _ = validated
    assert all(o.passed for o in result.gold.outcomes)


def test_failure_messages_come_from_junit_not_stdout(validated):
    from harness.core.results import Bucket

    result, _ = validated
    failing = [o for o in result.guarded.outcomes if o.bucket is Bucket.F2P]
    # The real pytest assertion diff, parsed out of the junit XML.
    assert any("assert" in (o.message or "") for o in failing)


def test_junit_artifacts_are_kept(validated):
    _, artifacts = validated
    assert (artifacts / "guarded-junit.xml").is_file()
    assert (artifacts / "gold-junit.xml").is_file()
