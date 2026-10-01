"""Backward compatibility alias for core.config."""
import sys
from core.config import *  # noqa: F401, F403
from core import config as _mod

sys.modules[__name__] = _mod

