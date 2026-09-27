"""BrainFrog Test Suite Package.

Ensures repo root is added to sys.path so test modules and core packages
are immediately discoverable regardless of execution method.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
