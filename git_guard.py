"""Backward compatibility alias for security.git_guard."""
import sys
from security.git_guard import *  # noqa: F401, F403
from security import git_guard as _mod

sys.modules[__name__] = _mod
