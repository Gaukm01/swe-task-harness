"""Credential loading. The key must never leak into output."""

from __future__ import annotations

import pytest

from harness.core.env import api_key, key_problem, load_dotenv, mask, parse_dotenv

REAL_SHAPE = "sk-ant-api03-" + "x" * 80


def test_parses_the_shapes_people_actually_paste():
    parsed = parse_dotenv(
        '# comment\n\n'
        'export ANTHROPIC_API_KEY="sk-ant-quoted"\n'
        "HARNESS_MODEL='claude-opus-5'\n"
        "BARE=value\n"
        "not-an-assignment\n"
    )
    assert parsed["ANTHROPIC_API_KEY"] == "sk-ant-quoted"
    assert parsed["HARNESS_MODEL"] == "claude-opus-5"
    assert parsed["BARE"] == "value"


def test_an_exported_variable_beats_the_file(tmp_path, monkeypatch):
    # Otherwise "which key is it using" becomes guesswork.
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=from-file\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-shell")
    load_dotenv(tmp_path)
    assert api_key() == "from-shell"


def test_the_file_is_used_when_nothing_is_exported(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(f"ANTHROPIC_API_KEY={REAL_SHAPE}\n")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert load_dotenv(tmp_path) == ["ANTHROPIC_API_KEY"]
    assert api_key() == REAL_SHAPE


def test_a_missing_file_is_not_an_error(tmp_path):
    assert load_dotenv(tmp_path) == []


def test_an_empty_value_is_not_treated_as_set(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=\n")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    load_dotenv(tmp_path)
    assert api_key() is None


def test_mask_never_reveals_the_key():
    masked = mask(REAL_SHAPE)
    assert REAL_SHAPE not in masked
    assert len(masked) < 25
    # Enough to tell two keys apart, not enough to use one.
    assert masked.startswith("sk-ant-api")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (REAL_SHAPE, None),
        ("sk-ant-short", "looks truncated"),
        ('"sk-ant-x"', "is wrapped in quotes"),
        ("export sk-ant-x", "still includes the `export ` prefix"),
        ("ghp_notananthropickey", "does not start with 'sk-ant-'"),
    ],
)
def test_paste_errors_are_named(value, expected):
    # Catching these at doctor time beats discovering them partway through a
    # rate-limited live run.
    assert key_problem(value) == expected
