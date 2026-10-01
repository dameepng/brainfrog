"""Backward compatibility alias for security.auth_manager."""
import sys
from security.auth_manager import *  # noqa: F401, F403
from security import auth_manager as _mod

sys.modules[__name__] = _mod

