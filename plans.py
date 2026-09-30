"""Backward compatibility alias for core.plans."""
import sys
from core.plans import *  # noqa: F401, F403
from core import plans as _mod

sys.modules[__name__] = _mod
