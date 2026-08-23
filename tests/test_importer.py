"""SWE-Bench Pro import: the dataset quirks, verified against real values."""

from __future__ import annotations

import json

import pytest

from harness.core.bundle import lint_bundle, load_bundle
from harness.importers.swebench_pro import (
    build_description,
    build_task_json,
    decode_selector_list,
    decode_text_field,
    sanitize_task_id,
    write_bundle,
)

# Shaped exactly like a real row, including the encodings that bite.
ROW = {
    "instance_id": "instance_ansible__ansible-12734fa21c08a0ce8c84e533abdc560db2eb1955-v7eee245",
    "repo": "ansible/ansible",
    "base_commit": "d" * 40,
    "repo_language": "python",
    "patch": "diff --git a/lib/x.py b/lib/x.py\n--- a/lib/x.py\n+++ b/lib/x.py\n",
    "test_patch": "diff --git a/test/units/test_x.py b/test/units/test_x.py\n",
    # Prose fields arrive JSON-encoded, with literal backslash-n.
    "problem_statement": '"# Title\\n\\nBody text."',
    "requirements": '"- must do the thing"',
    "interface": '"Type: Function\\n\\nName: `f`"',
    # These two disagree about encoding in the same row.
    "fail_to_pass": "['test/units/test_x.py::test_a']",
    "pass_to_pass": '["test/units/test_x.py::test_b"]',
    "selected_test_files_to_run": '["test/units/test_x.py"]',
    "dockerhub_tag": "ansible.ansible-truncated-tag",
}


def test_prose_fields_are_decoded():
    # Undecoded, description.md is a single unreadable line -- and it is the
    # agent's only input.
    description = build_description(ROW)
    assert description.count("\n") > 4
    assert '\\n' not in description
    assert not description.startswith('"')
    assert "# Title" in description


def test_plain_prose_is_left_alone():
    assert decode_text_field("already plain\nprose") == "already plain\nprose"
    assert decode_text_field(None) == ""


def test_both_selector_encodings_decode():
    # Verified: one real row had pass_to_pass as JSON and fail_to_pass as a
    # Python repr.
    assert decode_selector_list("['a::b']") == ["a::b"]
    assert decode_selector_list('["a::b"]') == ["a::b"]
    assert decode_selector_list(None) == []


def test_task_id_is_docker_safe():
    # Docker's repository grammar permits `__` as a separator, so what matters
    # is TASK_ID_RE, lowercase, and a length that leaves room for the
    # `:<run_id>-<phase>` suffix inside the 128-char tag limit.
    from harness.core.bundle import TASK_ID_RE

    task_id = sanitize_task_id(ROW["instance_id"])
    assert TASK_ID_RE.match(task_id), task_id
    assert task_id == task_id.lower()
    assert len(task_id) <= 60


def test_task_ids_of_two_instances_of_one_repo_differ():
    # The tail carries the commit shas; truncating from the front would
    # collide every ansible instance into one id.
    other = dict(ROW, instance_id=ROW["instance_id"].replace("12734fa", "abcdef0"))
    assert sanitize_task_id(ROW["instance_id"]) != sanitize_task_id(other["instance_id"])


def test_the_dockerhub_tag_is_used_verbatim():
    # It is truncated to 128 chars and is NOT derivable from instance_id.
    spec = build_task_json(ROW, repo_path_in_image="/app")
    assert spec["environment"]["image"].endswith(ROW["dockerhub_tag"])


def test_selectors_never_land_in_both_buckets():
    row = dict(ROW, pass_to_pass='["test/units/test_x.py::test_a"]')
    spec = build_task_json(row, repo_path_in_image="/app")
    assert spec["tests"]["pass_to_pass"] == []
    assert spec["tests"]["fail_to_pass"] == ["test/units/test_x.py::test_a"]


def test_test_globs_include_the_instances_own_files():
    spec = build_task_json(ROW, repo_path_in_image="/app")
    assert "test/units/test_x.py" in spec["tests"]["test_path_globs"]


def test_an_imported_bundle_lints_clean(tmp_path):
    imported = write_bundle(ROW, tmp_path)
    report = lint_bundle(imported.path)
    assert report.is_clean, [c.model_dump() for c in report.failures]
    bundle = load_bundle(imported.path)
    assert bundle.spec.language == "python"
    assert bundle.spec.tests.framework == "pytest"
    assert json.loads((imported.path / "task.json").read_text())["source"]["instance_id"]


def test_patches_always_end_with_a_newline(tmp_path):
    # `git apply` rejects a patch whose final line has no newline.
    row = dict(ROW, patch="diff --git a/x b/x\n--- a/x\n+++ b/x")
    imported = write_bundle(row, tmp_path)
    assert (imported.path / "patch.diff").read_text().endswith("\n")
