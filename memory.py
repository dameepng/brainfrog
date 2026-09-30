"""Backward compatibility alias for core.memory."""
import sys
from core.memory import *  # noqa: F401, F403
from core import memory as _mod

sys.modules[__name__] = _mod
