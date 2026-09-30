"""Small helpers for loading local environment settings."""

from __future__ import annotations

import os
from pathlib import Path


def load_env(path: Path) -> None:
    """Read KEY=value entries without overriding the process environment."""
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
