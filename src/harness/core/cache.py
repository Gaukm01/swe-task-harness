"""The environment snapshot cache key.

What goes in: everything that changes what the BASE image *contains* -- the
image source, the build context bytes, the repo and commit, the platform, and
the harness's own env-setup version.

What stays out, deliberately: the problem statement, the selectors, and the
test patch. Editing a prompt must not cost a rebuild, or development becomes
miserable and the prompt stops getting edited. The harness version *is*
included, so changing setup logic invalidates stale snapshots instead of
silently reusing them.

This is not `bundle_digest`. That one identifies the bundle exactly, for
provenance on a run; this one identifies an environment, for reuse.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from harness import ENV_SETUP_VERSION
from harness.core.bundle import (
    DESCRIPTION_MD,
    PATCH_DIFF,
    TASK_JSON,
    TEST_PATCH_DIFF,
    TaskSpec,
)

# Bundle files that describe the *task*, not the environment. Excluded from the
# build-context hash so editing a prompt does not force a rebuild. task.json is
# excluded too because the environment fields it holds are hashed explicitly
# below -- including it wholesale would make a selector edit invalidate the image.
_TASK_ONLY_FILES = frozenset({TASK_JSON, DESCRIPTION_MD, PATCH_DIFF, TEST_PATCH_DIFF})

_EXCLUDED_DIRS = frozenset({".git", "__pycache__", ".pytest_cache", ".ruff_cache"})
_EXCLUDED_NAMES = frozenset({".DS_Store"})

# How much of the key to show in an image tag. 12 hex chars is 48 bits: enough
# that an accidental collision across one machine's snapshots will not happen.
CACHE_KEY_TAG_LEN = 12


def hash_build_context(bundle_dir: Path) -> str:
    """Digest the files docker would actually send as build context.

    Without this, editing the fixture's `repo/` would reuse a stale BASE image
    and the change would appear to have had no effect -- the single most
    confusing failure mode in a cached build pipeline.
    """
    entries: list[tuple[str, str]] = []
    for file_path in sorted(bundle_dir.rglob("*")):
        if not file_path.is_file():
            continue
        relative = file_path.relative_to(bundle_dir)
        if _EXCLUDED_DIRS.intersection(relative.parts):
            continue
        if relative.name in _EXCLUDED_NAMES or relative.suffix == ".pyc":
            continue
        if relative.as_posix() in _TASK_ONLY_FILES:
            continue
        entries.append((relative.as_posix(), hashlib.sha256(file_path.read_bytes()).hexdigest()))

    digest = hashlib.sha256()
    for relative_path, file_hash in sorted(entries):
        digest.update(f"{relative_path}\0{file_hash}\0".encode())
    return digest.hexdigest()


def compute_cache_key(
    spec: TaskSpec,
    bundle_dir: Path,
    *,
    base_image_digest: str | None = None,
) -> str:
    """The cache key for this task's BASE environment.

    `base_image_digest` pins an `image`-sourced environment to the bytes that
    were actually pulled, so a mutable upstream tag being republished
    invalidates the snapshot rather than silently changing what runs.
    """
    environment = spec.environment
    material: dict[str, object] = {
        "env_setup_version": ENV_SETUP_VERSION,
        "platform": environment.platform,
        "repo": spec.repo,
        "base_commit": spec.base_commit,
        "repo_path_in_image": environment.repo_path_in_image,
    }

    if environment.image:
        material["source"] = "image"
        material["image"] = environment.image
        material["image_digest"] = base_image_digest
    elif environment.dockerfile:
        material["source"] = "dockerfile"
        material["dockerfile"] = environment.dockerfile
        material["context"] = hash_build_context(bundle_dir)
    else:
        recipe = environment.recipe
        assert recipe is not None  # guaranteed by TaskSpec's exactly-one validator
        material["source"] = "recipe"
        material["base_image"] = recipe.base_image
        material["install_cmds"] = recipe.install_cmds
        material["image_digest"] = base_image_digest

    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
