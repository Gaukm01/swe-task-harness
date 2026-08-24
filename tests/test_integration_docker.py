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


# ---------------------------------------------------------------------------
# The run lane end to end. README calls gold/noop "the harness's own regression
# suite"; before this they were enforced only by hand and against FakeRuntime.
# ---------------------------------------------------------------------------


def _run(runtime, bundle_dir, solver_spec, tmp_path):
    from harness.core.bundle import load_bundle as _load
    from harness.core.cache import compute_cache_key
    from harness.core.ids import new_ulid
    from harness.core.phases import Phase, phase_tag, prepare_base
    from harness.core.run import execute_run
    from harness.solvers import resolve_solver

    bundle = _load(bundle_dir)
    key = compute_cache_key(bundle.spec, bundle_dir)
    base = prepare_base(runtime, bundle.spec, bundle_dir, cache_key=key)
    run_id = new_ulid()
    try:
        return execute_run(
            runtime,
            bundle,
            base,
            resolve_solver(solver_spec),
            run_id=run_id,
            artifact_dir=tmp_path / run_id,
            keep_snapshots=False,
        )
    finally:
        for phase in (Phase.GUARDED, Phase.GOLD, Phase.SOLVE, Phase.SCORED):
            runtime.remove_image(phase_tag(bundle.spec.task_id, run_id, phase), force=True)


def test_gold_resolves_end_to_end(runtime, request, tmp_path):
    from harness.core.results import Outcome

    result = _run(runtime, request.config.rootpath / "examples/tiny-fixture", "gold", tmp_path)
    assert result.outcome is Outcome.RESOLVED
    assert result.gaming_flags == []


def test_noop_is_unresolved_end_to_end(runtime, request, tmp_path):
    from harness.core.results import Bucket, Outcome, Transition

    result = _run(runtime, request.config.rootpath / "examples/tiny-fixture", "noop", tmp_path)
    assert result.outcome is Outcome.UNRESOLVED
    assert all(
        t.transition is Transition.STILL_FAILING
        for t in result.transitions
        if t.bucket is Bucket.F2P
    )


def test_a_solver_cannot_forge_the_results_file(runtime, request, tmp_path):
    """The attack that once produced a false `resolved`.

    An `atexit` hook in an ordinary source file — no test path, no test config —
    rewrites the junit after pytest writes it and before the harness reads it.
    It once graded `resolved` with zero flags while `merge()` was never touched.

    The bar is not that the attack fails; it is that it can never yield a
    *silent* pass. Any of unresolved / inconclusive / resolved_suspect is
    acceptable; `resolved` with no flags is not.
    """
    import base64

    from harness.core.results import Outcome

    forge = (
        "\nimport atexit as _ae, glob as _g\n"
        "def _forge():\n"
        "    for _p in _g.glob('/var/opt/.*/*junit*.xml'):\n"
        "        open(_p, 'w').write('<testsuite name=\"pytest\" tests=\"0\"/>')\n"
        "_ae.register(_forge)\n"
    )
    # base64 so the payload survives the trip through `bash -lc` unmangled;
    # nested quoting silently corrupted an earlier version of this test.
    encoded = base64.b64encode(forge.encode()).decode()
    solver = (
        "cmd:python -c "
        "'import base64,pathlib;"
        "p=pathlib.Path(\"tinylib/__init__.py\");"
        f'p.write_text(p.read_text()+base64.b64decode("{encoded}").decode())\''
    )

    result = _run(runtime, request.config.rootpath / "examples/tiny-fixture", solver, tmp_path)

    assert result.solution_diff.strip(), "the attack payload never landed"
    assert "atexit" in result.solution_diff
    assert result.outcome is not Outcome.RESOLVED, "results forgery produced a silent pass"
    assert result.gaming_flags, "results forgery was invisible"


def test_an_evasive_forgery_cannot_reach_a_silent_pass(runtime, request, tmp_path):
    """The payload that beat all three original layers at once.

    Assembles every flagged literal at runtime (so content flags miss), walks
    the filesystem to find the results file (so the nonced path does not help),
    and forces the exit code (so the cross-check does not fire). Before the
    canary this graded a silent `resolved` with zero flags.

    The bar is `resolved` must be unreachable. Flags are not required here --
    the point is that the *verdict* is not corruptible by a blanket forgery.
    """
    import base64

    from harness.core.results import Outcome

    names = [
        "test_empty_input", "test_single_interval", "test_merges_overlapping",
        "test_keeps_disjoint_intervals", "test_merges_touching_intervals",
        "test_sorts_unordered_input",
    ]
    rows = "".join(
        f'<testcase classname="tests.test_intervals" name="{n}" '
        f'file="tests/test_intervals.py" time="0.01"/>'
        for n in names
    )
    doc = (
        '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" '
        f'errors="0" failures="0" skipped="0" tests="{len(names)}" time="0.06">'
        + rows
        + "</testsuite></testsuites>"
    )
    payload = (
        "\n\nimport os as _o\n"
        "_D = " + repr(doc) + "\n"
        "def _z():\n"
        "    for _r, _ds, _fs in _o.walk('/'):\n"
        "        if _r.startswith(('/proc','/sys','/dev','/app','/workspace')):\n"
        "            _ds[:] = []\n"
        "            continue\n"
        "        for _f in _fs:\n"
        "            if _f.endswith('.xml'):\n"
        "                _p = _o.path.join(_r, _f)\n"
        "                try:\n"
        "                    if '<testsuite' in open(_p).read():\n"
        "                        open(_p, 'w').write(_D)\n"
        "                except Exception:\n"
        "                    pass\n"
        "    getattr(_o, '_' + 'exit')(0)\n"
        "_reg = getattr(__import__('ate' + 'xit'), 'register')\n"
        "_reg(_z)\n"
    )
    encoded = base64.b64encode(payload.encode()).decode()
    solver = (
        "cmd:python -c "
        "'import base64,pathlib;"
        "p=pathlib.Path(\"tinylib/__init__.py\");"
        f'p.write_text(p.read_text()+base64.b64decode("{encoded}").decode())\''
    )

    result = _run(runtime, request.config.rootpath / "examples/tiny-fixture", solver, tmp_path)
    assert "os.walk" in result.solution_diff or "_o.walk" in result.solution_diff
    assert result.outcome is not Outcome.RESOLVED, "evasive forgery reached a silent pass"
