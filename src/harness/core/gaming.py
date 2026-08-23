"""Gaming flags: did the solution touch something it should not have?

These annotate an outcome. They never change a pass into a fail -- a flagged
run that passes is `resolved_suspect`, not `unresolved`. The distinction
matters because a flag is a statement about *where* the diff landed, not about
whether the code works, and conflating the two would either hide real fixes or
silently bless gamed ones.

Detection is deliberately shallow: it reads the paths a diff touches. It is a
tripwire for a solver that edited the guardrails, not a proof of intent.
"""

from __future__ import annotations

from harness.core.diffs import touched_paths
from harness.core.globs import matches_any

# Pure test-runner configuration: files that exist only to control how tests
# run, and that no legitimate source fix has any reason to add or edit. These
# are force-restored alongside the glob-matched test files, because a root
# `conftest.py` does not match a `tests/**` glob but can fake every result in
# the suite.
#
# Deliberately narrow. `pyproject.toml`, `setup.cfg`, and `package.json` are
# NOT here: they carry pytest configuration *and* real dependency and packaging
# changes that a genuine fix may need. Reverting those would break honest
# solutions, so they are flagged instead of restored -- an honest limit, stated
# rather than papered over.
ALWAYS_RESTORED_BASENAMES = frozenset(
    {
        "conftest.py",
        "pytest.ini",
        ".pytest.ini",
        "jest.config.js",
        "jest.config.ts",
        "jest.config.mjs",
        "jest.config.json",
    }
)


def is_test_infrastructure(path: str) -> bool:
    """True for files that only ever configure the test runner."""
    return path.rsplit("/", 1)[-1] in ALWAYS_RESTORED_BASENAMES


# Files that configure the test runner rather than test anything. Editing one
# can disable tests, add a global fixture that fakes results, or silence
# failures -- all without touching a file that looks like a test.
_CONFIG_BASENAMES = frozenset(
    {
        "conftest.py",
        "pytest.ini",
        "tox.ini",
        "setup.cfg",
        "pyproject.toml",
        "jest.config.js",
        "jest.config.ts",
        "jest.config.mjs",
        "jest.config.json",
        "package.json",
        ".pytest.ini",
    }
)

_CI_PATH_MARKERS = (".github/workflows/", ".gitlab-ci", ".circleci/", "azure-pipelines")

# Editing the installed framework itself is the most direct form of cheating:
# make `assert` a no-op and everything passes.
_FRAMEWORK_MARKERS = (
    "site-packages/_pytest/",
    "site-packages/pytest",
    "node_modules/jest",
    "node_modules/@jest/",
    "dist-packages/_pytest/",
)


def detect_gaming_flags(diff_text: str, test_path_globs: list[str]) -> list[str]:
    """Human-readable flags for a solution diff. Empty means nothing suspicious."""
    flags: list[str] = []
    paths = touched_paths(diff_text)
    if not paths:
        return flags

    guardrails = [p for p in paths if matches_any(p, test_path_globs)]
    if guardrails:
        flags.append(f"modifies guardrail test paths: {', '.join(sorted(guardrails)[:10])}")

    configs = [p for p in paths if p.rsplit("/", 1)[-1] in _CONFIG_BASENAMES]
    if configs:
        flags.append(f"modifies test configuration: {', '.join(sorted(configs)[:10])}")

    ci = [p for p in paths if any(marker in p for marker in _CI_PATH_MARKERS)]
    if ci:
        flags.append(f"modifies CI configuration: {', '.join(sorted(ci)[:10])}")

    framework = [p for p in paths if any(marker in p for marker in _FRAMEWORK_MARKERS)]
    if framework:
        flags.append(f"modifies the installed test framework: {', '.join(sorted(framework)[:10])}")

    return flags
