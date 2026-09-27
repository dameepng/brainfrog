"""Backward compatibility alias for core.modules."""
import sys
from core import modules as _mod

sys.modules[__name__] = _mod
