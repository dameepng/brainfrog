"""Backward compatibility alias for core.modules."""
import sys
from core.modules import *  # noqa: F401, F403
from core import modules as _mod

sys.modules[__name__] = _mod
