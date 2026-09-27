"""Backward compatibility alias for core.memory."""
import sys
from core import memory as _mod

sys.modules[__name__] = _mod
