"""Backward compatibility alias for core.plans."""
import sys
from core import plans as _mod

sys.modules[__name__] = _mod
