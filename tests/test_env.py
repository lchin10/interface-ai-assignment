from __future__ import annotations

import os

from automation import load_env

KEYS = ("T_KEY", "T_EXPORTED", "T_EMPTY", "T_KEEP", "T_SPACED", "T_SINGLE")


def test_load_env(tmp_path, monkeypatch):
    for key in KEYS:  # register every key so monkeypatch removes whatever load_env sets
        monkeypatch.setenv(key, "x")
        monkeypatch.delenv(key)
    monkeypatch.setenv("T_KEEP", "from-real-env")
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n\n"
        'T_KEY="sk-ant-test"\n'
        "export T_EXPORTED=1\n"
        "T_EMPTY=\n"
        "NO_EQUALS_SIGN\n"
        "T_KEEP=from-file\n"
        "  T_SPACED = two words  \n"
        "T_SINGLE='a=b'\n",
        encoding="utf-8",
    )
    load_env(env)
    assert os.environ["T_KEY"] == "sk-ant-test"
    assert os.environ["T_EXPORTED"] == "1"
    assert "T_EMPTY" not in os.environ, "a blank placeholder must not set anything"
    assert os.environ["T_KEEP"] == "from-real-env", "the real environment wins"
    assert os.environ["T_SPACED"] == "two words"
    assert os.environ["T_SINGLE"] == "a=b"
    assert "NO_EQUALS_SIGN" not in os.environ


def test_missing_env_file_is_fine(tmp_path):
    load_env(tmp_path / "nope.env")
