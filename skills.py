"""Backward compatibility alias for core.skills."""
import sys
from core.skills import *  # noqa: F401, F403
from core import skills as _mod

sys.modules[__name__] = _mod
