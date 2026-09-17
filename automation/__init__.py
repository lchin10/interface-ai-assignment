"""Record-once / replay-many computer-use automation."""
from __future__ import annotations

import os
from pathlib import Path


def load_env(path: str | Path = ".env") -> None:
    """Load KEY=VALUE lines from a .env file into the environment.

    Variables already set in the real environment win, and empty values are skipped so a blank
    placeholder never shadows anything. A missing file is fine.
    """
    path = Path(path)
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if sep and key and value:
            os.environ.setdefault(key, value)
