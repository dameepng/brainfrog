from __future__ import annotations

import os
from typing import Optional

from .claude_client import (
    MODEL_CONTEXT_LIMITS,
    PlanStep,
    UsageStats,
    UsageTracker,
    get_model_context_limit,
    resolve_model_context_limit,
    usage_tracker,
)
from .claude_client import System2Client as ClaudeSystem2Client
from .antigravity_client import AntigravitySystem2Client, find_antigravity_bin
from .openai_client import OpenAISystem2Client
from .json_utils import extract_json, _extract_json, repair_json_content

System2ClientType = ClaudeSystem2Client | AntigravitySystem2Client | OpenAISystem2Client


def get_system2_provider(explicit_provider: Optional[str] = None) -> str:
    """Determine the active System 2 provider.

    Returns 'antigravity' (Google Auth), 'claude' (Anthropic API),
    'openai', 'openrouter', or 'openai-compatible'.
    """
    if explicit_provider:
        prov = explicit_provider.lower().strip()
        if prov in ("gemini", "antigravity", "google"):
            return "antigravity"
        if prov in ("claude", "anthropic"):
            return "claude"
        if prov in ("openai", "gpt"):
            return "openai"
        if prov in ("openrouter", "or"):
            return "openrouter"
        if prov in ("openai-compatible", "custom", "vllm", "localai", "ollama"):
            return "openai-compatible"
        return prov

    env_prov = os.environ.get("SYSTEM2_PROVIDER", "").lower().strip()
    if env_prov in ("gemini", "antigravity", "google"):
        return "antigravity"
    if env_prov in ("claude", "anthropic"):
        return "claude"
    if env_prov in ("openai", "gpt"):
        return "openai"
    if env_prov in ("openrouter", "or"):
        return "openrouter"
    if env_prov in ("openai-compatible", "custom", "vllm", "localai", "ollama"):
        return "openai-compatible"

    # Auto-detection:
    # 1. OpenRouter key configured
    if os.environ.get("OPENROUTER_API_KEY") and not os.environ.get("ANTHROPIC_API_KEY"):
        return "openrouter"
    # 2. OpenAI key configured (and no Anthropic key)
    if os.environ.get("OPENAI_API_KEY") and not os.environ.get("ANTHROPIC_API_KEY"):
        return "openai"
    # 3. If ANTHROPIC_API_KEY is not set but Antigravity binary is available, use antigravity!
    if not os.environ.get("ANTHROPIC_API_KEY") and find_antigravity_bin():
        return "antigravity"

    return "claude"


def System2Client(
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    guidelines: str = "",
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
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

    if chosen_provider in ("openai", "openrouter", "openai-compatible"):
        return OpenAISystem2Client(
            model=model,
            api_key=api_key,
            base_url=base_url,
            guidelines=guidelines,
            provider_name=chosen_provider,
        )

    if model:
        return ClaudeSystem2Client(model=model, api_key=api_key, guidelines=guidelines)
    return ClaudeSystem2Client(api_key=api_key, guidelines=guidelines)


__all__ = [
    "PlanStep",
    "UsageStats",
    "UsageTracker",
    "usage_tracker",
    "System2Client",
    "System2ClientType",
    "ClaudeSystem2Client",
    "AntigravitySystem2Client",
    "OpenAISystem2Client",
    "get_system2_provider",
    "find_antigravity_bin",
    "get_model_context_limit",
    "resolve_model_context_limit",
    "MODEL_CONTEXT_LIMITS",
    "extract_json",
    "_extract_json",
    "repair_json_content",
]
