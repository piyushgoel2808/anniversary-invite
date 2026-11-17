"""Shared pytest configuration and fixtures."""

from __future__ import annotations

import re
import sys

# Ensure foreign shell PYTHONPATH entries (e.g. Homebrew python3.14/3.15) never shadow .venv
_CURRENT_PY_TAG = f"python{sys.version_info.major}.{sys.version_info.minor}"
_FOREIGN_PY_SITE_RE = re.compile(r"python\d+\.\d+[/\\]site-packages")
sys.path[:] = [
    entry
    for entry in sys.path
    if not (_FOREIGN_PY_SITE_RE.search(entry) and _CURRENT_PY_TAG not in entry)
]
