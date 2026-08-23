"""The task bundle: schema, loading, digest, and structural validation.

A bundle is a directory:

    <task>/
      task.json          metadata, validated against TaskSpec below
      description.md     problem_statement + requirements + interface
      patch.diff         gold patch          (never shown to the agent)
      test_patch.diff    guardrail tests     (never shown to the agent)

Pure with respect to Docker: this module reads files and validates them, and
imports nothing from `runtime`. Everything `task lint` reports is decided here,
so the rules are testable against fixture directories with no daemon running.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from harness.core.checks import Check, CheckReport, CheckStatus
from harness.core.diffs import looks_like_unified_diff, touched_paths
from harness.core.errors import BundleInvalidError, ExitCode
from harness.core.globs import matches_any

TASK_JSON = "task.json"
DESCRIPTION_MD = "description.md"
PATCH_DIFF = "patch.diff"
TEST_PATCH_DIFF = "test_patch.diff"

REQUIRED_FILES = (TASK_JSON, DESCRIPTION_MD, PATCH_DIFF, TEST_PATCH_DIFF)

# task_id is interpolated into the image tag `harness/<task_id>:<run_id>-<phase>`.
# Docker repository names are lowercase and limited to these characters, so an
# id that violates this fails at `docker commit` time -- much later, and much
# more confusingly -- unless it is rejected here.
TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

# Frameworks with a real adapter versus ones stubbed behind the same protocol.
FULLY_SUPPORTED_FRAMEWORKS = frozenset({"pytest"})

# Files that never belong in a content digest.
_DIGEST_EXCLUDED_NAMES = frozenset({".DS_Store"})
_DIGEST_EXCLUDED_DIRS = frozenset({".git", "__pycache__", ".pytest_cache", ".ruff_cache"})


class Recipe(BaseModel):
    """Build an environment from a base image plus install commands."""

    model_config = ConfigDict(extra="forbid")

    base_image: str
    install_cmds: list[str] = Field(default_factory=list)


class Environment(BaseModel):
    """Where the task's container image comes from.

    Exactly one of `image`, `dockerfile`, or `recipe` must be set. The order is
    a preference order, not a fallback chain: a pinned image is reproducible, a
    Dockerfile is reproducible given its context, and a recipe is the least
    reproducible of the three.
    """

    model_config = ConfigDict(extra="forbid")

    image: str | None = None
    dockerfile: str | None = None
    recipe: Recipe | None = None
    # None means "build for the host architecture". The importer sets
    # linux/amd64 because SWE-Bench Pro images are amd64 only; the tiny fixture
    # leaves it unset so the development loop stays native and fast.
    platform: str | None = None
    # Set when the image already contains the repo, in which case nothing is cloned.
    repo_path_in_image: str | None = None

    @property
    def sources(self) -> list[str]:
        """Which of the three environment sources are populated."""
        present = []
        if self.image:
            present.append("image")
        if self.dockerfile:
            present.append("dockerfile")
        if self.recipe:
            present.append("recipe")
        return present


class Tests(BaseModel):
    """How to run the task's tests, and which ones decide the outcome."""

    model_config = ConfigDict(extra="forbid")

    framework: Literal["pytest", "go", "jest"]
    # Must contain {selectors} and {out}: the harness never parses stdout, so a
    # template without a structured-output path cannot produce a gradeable run.
    run_cmd_template: str
    fail_to_pass: list[str]
    pass_to_pass: list[str] = Field(default_factory=list)
    test_path_globs: list[str]
    timeout_s: int = Field(default=600, gt=0)

    @property
    def selectors(self) -> list[str]:
        """Every guardrail selector, f2p first."""
        return [*self.fail_to_pass, *self.pass_to_pass]


class Source(BaseModel):
    """Provenance, when the bundle was generated from a dataset."""

    model_config = ConfigDict(extra="forbid")

    dataset: str | None = None
    instance_id: str | None = None
    dockerhub_tag: str | None = None


class TaskSpec(BaseModel):
    """`task.json`, validated.

    `extra="forbid"` throughout: a misspelled key in a hand-authored task.json
    would otherwise be silently ignored and show up as a mystery at grading
    time, which is the worst place to discover it.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    repo: str | None = None
    # Null is legitimate when the image ships the repo with no upstream history
    # to check out. BASE prep re-inits git with a synthetic commit regardless,
    # so a bundle that never clones has nothing to pin.
    base_commit: str | None = None
    language: Literal["python", "go", "js", "ts"]
    environment: Environment
    tests: Tests
    source: Source | None = None

    @model_validator(mode="after")
    def _check_consistency(self) -> TaskSpec:
        if not TASK_ID_RE.match(self.task_id):
            raise ValueError(
                f"task_id {self.task_id!r} must match {TASK_ID_RE.pattern} "
                "so it is usable as a docker image tag"
            )

        sources = self.environment.sources
        if len(sources) != 1:
            found = ", ".join(sources) if sources else "none"
            raise ValueError(
                f"environment must set exactly one of image, dockerfile, or recipe (found: {found})"
            )

        if self.repo and not self.base_commit:
            raise ValueError("base_commit is required when repo is set, or the clone is unpinned")

        if not self.repo and not self.environment.repo_path_in_image:
            raise ValueError(
                "set repo (to clone) or environment.repo_path_in_image "
                "(when the image already ships the repo)"
            )

        if not self.tests.fail_to_pass:
            raise ValueError("tests.fail_to_pass must not be empty: nothing would prove a fix")

        overlap = sorted(set(self.tests.fail_to_pass) & set(self.tests.pass_to_pass))
        if overlap:
            raise ValueError(
                f"selectors appear in both fail_to_pass and pass_to_pass: {', '.join(overlap)}"
            )

        for field in ("{selectors}", "{out}"):
            if field not in self.tests.run_cmd_template:
                raise ValueError(
                    f"tests.run_cmd_template must contain {field}; "
                    "the harness grades structured output, never parsed stdout"
                )

        if not self.tests.test_path_globs:
            raise ValueError(
                "tests.test_path_globs must not be empty: it defines which files are "
                "force-restored before grading and which edits raise a gaming flag"
            )
        return self


class Bundle(BaseModel):
    """A loaded bundle: its spec, its text files, and its content digest."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    path: Path
    spec: TaskSpec
    description: str
    patch: str
    test_patch: str
    digest: str


def compute_bundle_digest(bundle_dir: Path) -> str:
    """sha256 over the canonicalized contents of a bundle directory.

    Canonical form is the sorted sequence of (relative POSIX path, sha256 of
    bytes) for every file, so the digest is stable across filesystems and
    checkout order and changes if any byte of any file changes. Editor and VCS
    droppings are excluded -- they are not part of the task.

    This identifies the bundle for provenance and is deliberately *not* the
    environment cache key, which excludes the prompt and test patch so that
    editing a problem statement does not force an image rebuild.
    """
    entries: list[tuple[str, str]] = []
    for file_path in sorted(bundle_dir.rglob("*")):
        if not file_path.is_file():
            continue
        relative = file_path.relative_to(bundle_dir)
        if _DIGEST_EXCLUDED_DIRS.intersection(relative.parts):
            continue
        if relative.name in _DIGEST_EXCLUDED_NAMES or relative.suffix == ".pyc":
            continue
        entries.append((relative.as_posix(), hashlib.sha256(file_path.read_bytes()).hexdigest()))

    digest = hashlib.sha256()
    for relative_path, file_hash in sorted(entries):
        digest.update(relative_path.encode())
        digest.update(b"\0")
        digest.update(file_hash.encode())
        digest.update(b"\0")
    return digest.hexdigest()


def load_bundle(bundle_dir: Path) -> Bundle:
    """Load and validate a bundle, or raise BundleInvalidError.

    This is the strict entry point used by `init`, `validate`, and `run`.
    `task lint` reports every problem at once instead; this one stops at the
    first, because the callers cannot proceed either way.
    """
    if not bundle_dir.is_dir():
        raise BundleInvalidError(
            f"{bundle_dir} is not a directory.",
            fix="Point at a bundle directory containing task.json.",
        )

    missing = [name for name in REQUIRED_FILES if not (bundle_dir / name).is_file()]
    if missing:
        raise BundleInvalidError(
            f"{bundle_dir} is missing {', '.join(missing)}.",
            fix=f"A bundle needs all of: {', '.join(REQUIRED_FILES)}.",
        )

    raw = (bundle_dir / TASK_JSON).read_text()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise BundleInvalidError(
            f"{bundle_dir / TASK_JSON} is not valid JSON: {error}.",
            fix=f"Fix the syntax at line {error.lineno}, column {error.colno}.",
        ) from error

    try:
        spec = TaskSpec.model_validate(parsed)
    except ValidationError as error:
        raise BundleInvalidError(
            f"{bundle_dir / TASK_JSON} failed validation: {_first_error(error)}.",
            fix="Run `task lint` on the bundle for the full list of problems.",
        ) from error

    return Bundle(
        path=bundle_dir,
        spec=spec,
        description=(bundle_dir / DESCRIPTION_MD).read_text(),
        patch=(bundle_dir / PATCH_DIFF).read_text(),
        test_patch=(bundle_dir / TEST_PATCH_DIFF).read_text(),
        digest=compute_bundle_digest(bundle_dir),
    )


def _first_error(error: ValidationError) -> str:
    """Render the first pydantic error as `field: message`."""
    first = error.errors()[0]
    location = ".".join(str(part) for part in first["loc"]) or "task.json"
    return f"{location}: {first['msg']}"


def _fail(name: str, detail: str, fix: str) -> Check:
    return Check(
        name=name,
        status=CheckStatus.FAIL,
        detail=detail,
        fix=fix,
        exit_code=ExitCode.BUNDLE_INVALID,
    )


def lint_bundle(bundle_dir: Path) -> CheckReport:
    """Check a bundle exhaustively and report every problem found.

    Unlike `load_bundle`, this keeps going after a failure wherever a later
    check is still meaningful -- an author fixing a bundle wants the whole list,
    not one problem per invocation.
    """
    report = CheckReport()

    if not bundle_dir.is_dir():
        report.add(
            _fail(
                "bundle directory",
                f"{bundle_dir} is not a directory",
                "Point at a bundle directory containing task.json.",
            )
        )
        return report

    present = [name for name in REQUIRED_FILES if (bundle_dir / name).is_file()]
    missing = [name for name in REQUIRED_FILES if name not in present]
    if missing:
        report.add(
            _fail(
                "required files",
                f"missing {', '.join(missing)}",
                f"A bundle needs all of: {', '.join(REQUIRED_FILES)}.",
            )
        )
    else:
        report.add(
            Check(name="required files", status=CheckStatus.OK, detail=", ".join(REQUIRED_FILES))
        )

    spec = _lint_task_json(bundle_dir, report)
    _lint_description(bundle_dir, report)
    patch_text, test_patch_text = _lint_diffs(bundle_dir, report)

    if spec is not None:
        _lint_environment(bundle_dir, spec, report)
        _lint_framework(spec, report)
        _lint_patch_separation(spec, patch_text, test_patch_text, report)

    return report


def _lint_task_json(bundle_dir: Path, report: CheckReport) -> TaskSpec | None:
    path = bundle_dir / TASK_JSON
    if not path.is_file():
        return None
    try:
        parsed = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        report.add(
            _fail(
                "task.json syntax",
                f"invalid JSON: {error.msg}",
                f"Fix the syntax at line {error.lineno}, column {error.colno}.",
            )
        )
        return None

    try:
        spec = TaskSpec.model_validate(parsed)
    except ValidationError as error:
        for item in error.errors():
            location = ".".join(str(part) for part in item["loc"]) or TASK_JSON
            report.add(
                _fail(
                    f"task.json: {location}",
                    item["msg"],
                    "Correct the field in task.json; see the bundle format in the README.",
                )
            )
        return None

    detail = f"{spec.task_id} · {spec.language} · {spec.tests.framework}"
    report.add(Check(name="task.json schema", status=CheckStatus.OK, detail=detail))
    report.add(
        Check(
            name="selectors",
            status=CheckStatus.OK,
            detail=(
                f"{len(spec.tests.fail_to_pass)} fail_to_pass · "
                f"{len(spec.tests.pass_to_pass)} pass_to_pass"
            ),
        )
    )
    if not spec.tests.pass_to_pass:
        report.add(
            Check(
                name="pass_to_pass",
                status=CheckStatus.WARN,
                detail="empty: no test guards against regressions",
                fix="Add existing tests that must keep passing, or a solver can break the repo "
                "and still be graded resolved.",
            )
        )
    return spec


def _lint_description(bundle_dir: Path, report: CheckReport) -> None:
    path = bundle_dir / DESCRIPTION_MD
    if not path.is_file():
        return
    text = path.read_text().strip()
    if not text:
        report.add(
            _fail(
                DESCRIPTION_MD,
                "empty",
                "description.md is the agent's only input; an empty one makes the task unsolvable.",
            )
        )
        return
    report.add(
        Check(
            name=DESCRIPTION_MD,
            status=CheckStatus.OK,
            detail=f"{len(text)} chars · {len(text.splitlines())} lines",
        )
    )


def _lint_diffs(bundle_dir: Path, report: CheckReport) -> tuple[str, str]:
    texts: dict[str, str] = {}
    for name, role in ((PATCH_DIFF, "gold patch"), (TEST_PATCH_DIFF, "guardrail tests")):
        path = bundle_dir / name
        if not path.is_file():
            texts[name] = ""
            continue
        text = path.read_text()
        texts[name] = text
        if not looks_like_unified_diff(text):
            report.add(
                _fail(
                    name,
                    "empty or not a unified diff",
                    f"{name} must be a unified diff ({role}). "
                    "Generate it with `git diff`, not by hand.",
                )
            )
            continue
        paths = touched_paths(text)
        report.add(
            Check(
                name=name,
                status=CheckStatus.OK,
                detail=f"{len(paths)} file{'s' if len(paths) != 1 else ''}: {', '.join(paths)}",
            )
        )
    return texts[PATCH_DIFF], texts[TEST_PATCH_DIFF]


def _lint_environment(bundle_dir: Path, spec: TaskSpec, report: CheckReport) -> None:
    env = spec.environment
    if env.dockerfile:
        dockerfile = bundle_dir / env.dockerfile
        if not dockerfile.is_file():
            report.add(
                _fail(
                    "environment.dockerfile",
                    f"{env.dockerfile} does not exist in the bundle",
                    "The dockerfile path is resolved relative to the bundle directory.",
                )
            )
            return
        detail = f"dockerfile {env.dockerfile}"
    elif env.image:
        detail = f"image {env.image}"
        if "@sha256:" not in env.image:
            report.add(
                Check(
                    name="environment.image",
                    status=CheckStatus.WARN,
                    detail=f"{env.image} is not pinned by digest",
                    fix="A mutable tag makes runs unreproducible. Prefer image@sha256:...",
                )
            )
    else:
        recipe = spec.environment.recipe
        assert recipe is not None  # guaranteed by TaskSpec's exactly-one validator
        detail = f"recipe from {recipe.base_image} ({len(recipe.install_cmds)} install cmds)"

    if env.platform:
        detail += f" · platform {env.platform}"
    report.add(Check(name="environment", status=CheckStatus.OK, detail=detail))


def _lint_framework(spec: TaskSpec, report: CheckReport) -> None:
    framework = spec.tests.framework
    if framework in FULLY_SUPPORTED_FRAMEWORKS:
        report.add(
            Check(
                name="test framework", status=CheckStatus.OK, detail=f"{framework} (full adapter)"
            )
        )
        return
    report.add(
        Check(
            name="test framework",
            status=CheckStatus.WARN,
            detail=f"{framework} adapter is a stub",
            fix="Only pytest has a full adapter. go and jest are wired behind the same "
            "protocol but do not run tests yet.",
        )
    )


def _lint_patch_separation(
    spec: TaskSpec, patch_text: str, test_patch_text: str, report: CheckReport
) -> None:
    """The two patches must not overlap in what they touch.

    This is the check most worth having. If the gold patch edits a test file,
    then `patch.diff` is smuggling in the very assertions it is meant to
    satisfy. If the test patch edits a source file, force-restore -- which
    resets only paths matching `test_path_globs` -- would silently drop part of
    it at grading time and the run would be graded against a different tree
    than the one that was validated.
    """
    globs = spec.tests.test_path_globs

    gold_test_files = [p for p in touched_paths(patch_text) if matches_any(p, globs)]
    if gold_test_files:
        report.add(
            _fail(
                "gold patch scope",
                f"patch.diff touches test paths: {', '.join(gold_test_files)}",
                "The gold patch must contain only the fix. Move test changes into "
                "test_patch.diff, or narrow tests.test_path_globs if these are not tests.",
            )
        )
    elif patch_text:
        report.add(
            Check(name="gold patch scope", status=CheckStatus.OK, detail="touches no test paths")
        )

    test_patch_paths = touched_paths(test_patch_text)
    non_test_files = [p for p in test_patch_paths if not matches_any(p, globs)]
    if non_test_files:
        report.add(
            _fail(
                "test patch scope",
                f"test_patch.diff touches non-test paths: {', '.join(non_test_files)}",
                "Force-restore resets only paths matching tests.test_path_globs, so these "
                "edits would be lost at grading. Widen the globs or move the changes.",
            )
        )
    elif test_patch_paths:
        report.add(
            Check(
                name="test patch scope",
                status=CheckStatus.OK,
                detail=f"all {len(test_patch_paths)} path(s) match test_path_globs",
            )
        )

    _lint_selector_files(spec, test_patch_paths, report)


def _lint_selector_files(spec: TaskSpec, test_patch_paths: list[str], report: CheckReport) -> None:
    """Every fail_to_pass selector should name a file the test patch introduces.

    A f2p test that already exists at base is suspicious: it would have to be
    failing in the untouched repo, which usually means a stale selector rather
    than a real guardrail. Warn rather than fail -- a test patch can legitimately
    make an existing test newly meaningful.
    """
    if not test_patch_paths:
        return
    patched = set(test_patch_paths)
    orphans = [
        selector
        for selector in spec.tests.fail_to_pass
        if _selector_file(selector) and _selector_file(selector) not in patched
    ]
    if orphans:
        report.add(
            Check(
                name="fail_to_pass provenance",
                status=CheckStatus.WARN,
                detail=f"{len(orphans)} selector(s) name files the test patch does not touch: "
                f"{', '.join(orphans)}",
                fix="These tests must already fail in the untouched repo. Confirm with "
                "`task validate`, or correct the selector.",
            )
        )


def _selector_file(selector: str) -> str | None:
    """The file part of a framework-native selector, if it has one."""
    head = selector.split("::", 1)[0].strip()
    return head or None
