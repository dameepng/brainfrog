from __future__ import annotations

import os
from typing import Optional

from .claude_client import (
    MODEL_CONTEXT_LIMITS,
    PlanStep,
    UsageStats,
    UsageTracker,
    get_model_context_limit,
    usage_tracker,
)
from .claude_client import System2Client as ClaudeSystem2Client
from .antigravity_client import AntigravitySystem2Client, find_antigravity_bin
from .json_utils import extract_json, _extract_json, repair_json_content


def get_system2_provider(explicit_provider: Optional[str] = None) -> str:
    """Determine the active System 2 provider.

    Returns 'antigravity' (Google Auth) or 'claude' (Anthropic API).
    """
    if explicit_provider:
        prov = explicit_provider.lower().strip()
        if prov in ("gemini", "antigravity", "google"):
            return "antigravity"
        return "claude"

    env_prov = os.environ.get("SYSTEM2_PROVIDER", "").lower().strip()
    if env_prov in ("gemini", "antigravity", "google"):
        return "antigravity"
    if env_prov == "claude":
        return "claude"

    # Auto-detection:
    # If ANTHROPIC_API_KEY is not set but Antigravity binary is available, use antigravity!
    if not os.environ.get("ANTHROPIC_API_KEY") and find_antigravity_bin():
        return "antigravity"

    return "claude"


def System2Client(
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    guidelines: str = "",
    provider: Optional[str] = None,
):
    """Factory to instantiate the appropriate System 2 client."""
    chosen_provider = get_system2_provider(provider)
    if chosen_provider == "antigravity":
        m = (
            model
            or os.environ.get("ANTIGRAVITY_MODEL")
            or os.environ.get("GEMINI_MODEL")
            or "gemini-3.8-flash-high"
        )
        return AntigravitySystem2Client(model=m, guidelines=guidelines)

    if model:
        return ClaudeSystem2Client(model=model, api_key=api_key, guidelines=guidelines)
    return ClaudeSystem2Client(api_key=api_key, guidelines=guidelines)


__all__ = [
    "PlanStep",
    "UsageStats",
    "UsageTracker",
    "usage_tracker",
    "System2Client",
    "ClaudeSystem2Client",
    "AntigravitySystem2Client",
    "get_system2_provider",
    "find_antigravity_bin",
    "extract_json",
    "_extract_json",
    "repair_json_content",
]
