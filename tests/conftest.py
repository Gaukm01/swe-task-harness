from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "tiny-fixture"


@pytest.fixture
def tiny_fixture() -> Path:
    """The real tiny fixture bundle, read-only."""
    return FIXTURE


@pytest.fixture
def bundle_copy(tmp_path: Path) -> Path:
    """A writable copy of the tiny fixture, for tests that corrupt it."""
    destination = tmp_path / "bundle"
    shutil.copytree(FIXTURE, destination)
    return destination


@pytest.fixture
def edit_task_json(bundle_copy: Path):
    """Mutate the copy's task.json in place."""

    def _edit(**changes: object) -> Path:
        path = bundle_copy / "task.json"
        data = json.loads(path.read_text())
        for key, value in changes.items():
            cursor = data
            *parents, leaf = key.split(".")
            for parent in parents:
                cursor = cursor[parent]
            if value is _DELETE:
                cursor.pop(leaf, None)
            else:
                cursor[leaf] = value
        path.write_text(json.dumps(data, indent=2))
        return bundle_copy

    return _edit


class _Delete:
    pass


_DELETE = _Delete()


@pytest.fixture
def delete_sentinel() -> object:
    return _DELETE
