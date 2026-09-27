"""Backward compatibility alias for core.config."""
import sys
from core import config as _mod

sys.modules[__name__] = _mod
