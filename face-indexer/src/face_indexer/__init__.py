"""Silver Jubilee local face-recognition prototype (YuNet + SFace)."""

from __future__ import annotations

import re
import sys

# Guard against shell-level PYTHONPATH leaking incompatible Python minor-version site-packages
# (e.g., Homebrew python3.14/3.15 C-extensions into this Python 3.12 virtualenv).
_CURRENT_PY_TAG = f"python{sys.version_info.major}.{sys.version_info.minor}"
_FOREIGN_PY_SITE_RE = re.compile(r"python\d+\.\d+[/\\]site-packages")
sys.path[:] = [
    entry
    for entry in sys.path
    if not (_FOREIGN_PY_SITE_RE.search(entry) and _CURRENT_PY_TAG not in entry)
]

from face_indexer.models import COMBINED_MODEL_VERSION  # noqa: E402

__all__ = ["COMBINED_MODEL_VERSION", "__version__"]
__version__ = "0.1.0"
