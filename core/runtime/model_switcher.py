"""System 2 Model Switcher & Catalog Management for BrainFrog Runtime.

Provides session-scoped System 2 model configuration, provider credential
validation, and safe command formatting without credential exposure.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from core.runtime.permissions import PermissionAction, PermissionPolicy
from core.runtime.secret_scrubbing import scrub_secrets
from core.runtime.session import SessionState
from system2 import (
    AntigravityOptInRequiredError,
    OPT_IN_ENV_VAR,
    System2Client,
    find_antigravity_bin,
    get_model_context_limit,
    get_system2_provider,
    is_antigravity_opt_in_enabled,
    validate_system2_provider_preflight,
)

# Canonical set of supported System 2 providers
SUPPORTED_SYSTEM2_PROVIDERS: Set[str] = {
    "claude",
    "antigravity",
    "openai",
    "openrouter",
    "sumopod",
    "openai-compatible",
}

# Provider display names
PROVIDER_DISPLAY_NAMES: Dict[str, str] = {
    "claude": "Claude (Anthropic API)",
    "antigravity": "Google Antigravity (Gemini)",
    "openai": "OpenAI (GPT API)",
    "openrouter": "OpenRouter API",
    "sumopod": "Sumopod AI",
    "openai-compatible": "OpenAI-Compatible (Custom/Local)",
}

# Curated catalog of supported models per provider
CURATED_PROVIDER_MODELS: Dict[str, List[Tuple[str, str, str]]] = {
    "claude": [
        ("claude-sonnet-5", "Claude Sonnet 5", "Default recommended model"),
        ("claude-3-5-sonnet-20241022", "Claude 3.5 Sonnet", "Standard Sonnet release"),
        ("claude-3-5-haiku-20241022", "Claude 3.5 Haiku", "Fast & lightweight"),
        ("claude-3-7-sonnet-20250219", "Claude 3.7 Sonnet", "Hybrid reasoning model"),
        ("claude-3-opus-20240229", "Claude 3 Opus", "Deep reasoning model"),
    ],
    "antigravity": [
        ("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)", "Ultra fast, high reasoning (Default)"),
        ("gemini-3.8-flash-medium", "Gemini 3.8 Flash (Medium)", "Balanced speed & performance"),
        ("gemini-3.7-flash-high", "Gemini 3.7 Flash (High)", "High reasoning"),
        ("gemini-3.1-pro-high", "Gemini 3.1 Pro (High)", "Complex system architecture & deep coding"),
        ("claude-sonnet-4-6", "Claude Sonnet 4.6 (Thinking)", "Anthropic Sonnet via Google Auth"),
        ("claude-opus-4-6-thinking", "Claude Opus 4.6 (Thinking)", "Anthropic Opus via Google Auth"),
        ("gpt-oss-120b-medium", "GPT-OSS 120B (Medium)", "Open weight model"),
    ],
    "openai": [
        ("gpt-4o", "GPT-4o", "High-intelligence flagship model (Default)"),
        ("gpt-4o-mini", "GPT-4o Mini", "Fast and affordable small model"),
        ("o1", "OpenAI o1", "Advanced reasoning model"),
        ("o3-mini", "OpenAI o3-mini", "Fast reasoning model"),
    ],
    "openrouter": [
        ("anthropic/claude-3.5-sonnet", "Claude 3.5 Sonnet", "Anthropic flagship via OpenRouter"),
        ("openai/gpt-4o", "GPT-4o", "OpenAI flagship via OpenRouter"),
        ("deepseek/deepseek-r1", "DeepSeek R1", "Open weights reasoning model"),
        ("meta-llama/llama-3.3-70b-instruct", "Llama 3.3 70B", "Fast open-weight model"),
    ],
    "sumopod": [
        ("deepseek-chat", "DeepSeek Chat (V3)", "High performance coding & reasoning (Default)"),
        ("deepseek-reasoner", "DeepSeek Reasoner (R1)", "DeepSeek R1 reasoning & logic thinking"),
        ("deepseek-v4-pro", "DeepSeek V4 Pro", "Priority reasoning model"),
        ("deepseek-v4.1-flash:netra", "DeepSeek V4.1 Flash Netra", "Priority fast reasoning"),
        ("glm-5.3-flash", "GLM-5.3 Flash", "Ultra-fast low-latency GLM"),
        ("kimi-k3", "Kimi K3", "Long-context reasoning"),
        ("kimi-latest", "Kimi Latest", "Long context & high speed coding"),
        ("moonshot-v1-8k", "Moonshot v1 8K", "Moonshot base 8K model"),
        ("moonshot-v1-32k", "Moonshot v1 32K", "Moonshot medium 32K context model"),
        ("moonshot-v1-128k", "Moonshot v1 128K", "Moonshot 128K long context model"),
        ("glm-4", "GLM-4", "Zhipu flagship general coding & reasoning"),
        ("glm-4-flash", "GLM-4 Flash", "Ultra-fast low-latency GLM model"),
        ("glm-4-plus", "GLM-4 Plus", "Enhanced reasoning GLM-4 variant"),
        ("glm-4-long", "GLM-4 Long", "1M long context GLM model"),
    ],
    "openai-compatible": [
        ("custom-model", "Custom Model", "Generic OpenAI-compatible model from .env"),
    ],
}


def get_canonical_provider(raw_provider: Optional[str]) -> Optional[str]:
    """Resolve user input string to canonical provider name if supported."""
    if not raw_provider or not raw_provider.strip():
        return None
    canonical = get_system2_provider(raw_provider)
    if canonical in SUPPORTED_SYSTEM2_PROVIDERS:
        return canonical
    return None


def get_default_model_for_provider(provider: str) -> str:
    """Return default model ID configured for the given provider."""
    prov = get_canonical_provider(provider) or provider
    if prov == "claude":
        return os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
    if prov == "antigravity":
        return (
            os.environ.get("ANTIGRAVITY_MODEL")
            or os.environ.get("GEMINI_MODEL")
            or "gemini-3.8-flash-high"
        )
    if prov == "openai":
        return os.environ.get("OPENAI_MODEL", "gpt-4o")
    if prov == "openrouter":
        return os.environ.get("OPENROUTER_MODEL", "anthropic/claude-3.5-sonnet")
    if prov == "sumopod":
        return os.environ.get("SUMOPOD_MODEL", "deepseek-chat")
    if prov == "openai-compatible":
        return os.environ.get("BRAINFROG_MODEL_NAME", "custom-model")
    return "default"


def check_provider_credentials(
    provider: str,
    model: Optional[str] = None,
    system2_factory: Optional[Callable[..., Any]] = None,
) -> Tuple[bool, str]:
    """Verify that credentials and preflight configuration for the provider are available.

    Never exposes actual keys or secrets in return values.
    Returns:
        (is_valid: bool, status_or_error_message: str)
    """
    prov = get_canonical_provider(provider) or provider
    if prov not in SUPPORTED_SYSTEM2_PROVIDERS:
        return False, f"Unsupported provider '{provider}'."

    # 1. Claude (Anthropic API)
    if prov == "claude":
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not key or key.startswith("sk-ant-..."):
            return False, "ANTHROPIC_API_KEY is not configured in environment or .env"
        return True, "Configured"

    # 2. Antigravity (Google Auth CLI)
    if prov == "antigravity":
        bin_path = find_antigravity_bin()
        if not bin_path or not os.path.exists(bin_path):
            return False, "Antigravity CLI binary ('agy.exe') not found in PATH or ~/.gemini/bin"
        if not is_antigravity_opt_in_enabled():
            return (
                False,
                f"Antigravity requires explicit opt-in ({OPT_IN_ENV_VAR}=1) due to host tool execution boundary",
            )
        return True, "Configured"

    # 3. Sumopod AI
    if prov == "sumopod":
        key = os.environ.get("SUMOPOD_API_KEY") or os.environ.get("BRAINFROG_MODEL_API_KEY")
        if not key or not key.strip():
            return False, "SUMOPOD_API_KEY is not configured in environment or .env"
        return True, "Configured"

    # 4. OpenRouter
    if prov == "openrouter":
        key = (
            os.environ.get("OPENROUTER_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("BRAINFROG_MODEL_API_KEY")
        )
        if not key or not key.strip():
            return False, "OPENROUTER_API_KEY is not configured in environment or .env"
        return True, "Configured"

    # 5. OpenAI
    if prov == "openai":
        key = (
            os.environ.get("OPENAI_API_KEY")
            or os.environ.get("BRAINFROG_MODEL_API_KEY")
            or os.environ.get("CUSTOM_API_KEY")
        )
        if not key or not key.strip():
            return False, "OPENAI_API_KEY is not configured in environment or .env"
        return True, "Configured"

    # 6. OpenAI-Compatible (Custom / Local)
    if prov == "openai-compatible":
        key = (
            os.environ.get("BRAINFROG_MODEL_API_KEY")
            or os.environ.get("CUSTOM_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
        )
        if not key or not key.strip():
            return False, "BRAINFROG_MODEL_API_KEY or CUSTOM_API_KEY is not configured"
        return True, "Configured"

    return True, "Configured"


def get_supported_models_for_provider(provider: str) -> List[Tuple[str, str, int]]:
    """Return list of (model_id, display_name, context_limit) for provider."""
    prov = get_canonical_provider(provider) or provider
    curated = CURATED_PROVIDER_MODELS.get(prov, [])
    results: List[Tuple[str, str, int]] = []
    seen: Set[str] = set()

    for mid, name, _ in curated:
        mid_clean = mid.strip()
        if mid_clean.lower() not in seen:
            seen.add(mid_clean.lower())
            limit = get_model_context_limit(mid_clean)
            results.append((mid_clean, name, limit))

    # Also add custom model from env if applicable
    if prov == "openai-compatible":
        custom_env_model = os.environ.get("BRAINFROG_MODEL_NAME")
        if custom_env_model and custom_env_model.strip().lower() not in seen:
            mid_c = custom_env_model.strip()
            seen.add(mid_c.lower())
            results.append((mid_c, f"Custom ({mid_c})", get_model_context_limit(mid_c)))

    return results


def is_model_supported_for_provider(provider: str, model_id: str) -> bool:
    """Validate whether model_id is a known, supported model for the given provider."""
    prov = get_canonical_provider(provider) or provider
    clean_mid = (model_id or "").strip().lower()
    if not clean_mid:
        return False

    # Check curated list
    models = get_supported_models_for_provider(prov)
    for mid, _, _ in models:
        if mid.lower() == clean_mid:
            return True

    # For OpenAI-compatible endpoints (Ollama, vLLM, LocalAI), allow valid identifier syntax
    if prov == "openai-compatible" and not any(c in clean_mid for c in " \t\r\n/\\"):
        return True

    return False


def format_models_markdown(provider_filter: Optional[str] = None) -> str:
    """Format markdown response listing supported providers and models for Telegram."""
    if provider_filter and provider_filter.strip():
        clean_filter = provider_filter.strip(" :,;")
        canon = get_canonical_provider(clean_filter)
        if not canon:
            supported_str = ", ".join(sorted(SUPPORTED_SYSTEM2_PROVIDERS))
            return (
                f"❌ Unknown provider '{provider_filter}'.\n"
                f"Supported providers: `{supported_str}`.\n"
                "Use `/models` to list all providers."
            )
        target_providers = [canon]
    else:
        target_providers = ["claude", "antigravity", "openai", "openrouter", "sumopod", "openai-compatible"]

    lines = ["🤖 **BrainFrog System 2 Model Catalog**\n"]

    for prov in target_providers:
        if not prov:
            continue
        display_name = PROVIDER_DISPLAY_NAMES.get(prov, prov.capitalize())
        is_ok, cred_desc = check_provider_credentials(prov)
        status_tag = "✅ Ready" if is_ok else f"⚠️ {cred_desc}"

        lines.append(f"**{display_name}** (`{prov}`) — {status_tag}")
        models = get_supported_models_for_provider(prov)
        for mid, name, limit in models:
            limit_k = f"{limit // 1000}k" if limit >= 1000 else str(limit)
            lines.append(f"• `{mid}` — {name} ({limit_k} tokens)")
        lines.append("")

    lines.append("💡 **Switch command:** `/model <provider> <model_id>`")
    lines.append("*Example:* `/model claude claude-3-5-sonnet-20241022`")
    lines.append("💡 **Reset to default:** `/model default`")
    return "\n".join(lines).strip()


def format_model_status_markdown(
    session: SessionState,
    default_provider: Optional[str] = None,
    default_model: Optional[str] = None,
) -> str:
    """Format markdown response reporting effective model and provider for the session."""
    eff_prov = (
        session.active_provider
        or default_provider
        or get_system2_provider()
    )
    canon_prov = get_canonical_provider(eff_prov) or eff_prov
    prov_scope = "session override" if session.active_provider else "system default"

    eff_model = (
        session.active_model
        or default_model
        or get_default_model_for_provider(canon_prov)
    )
    model_scope = "session override" if session.active_model else "system default"

    is_ok, cred_status = check_provider_credentials(canon_prov, model=eff_model)
    status_tag = "✅ Ready" if is_ok else f"⚠️ {cred_status}"
    limit = get_model_context_limit(eff_model)

    lines = [
        "🤖 **BrainFrog System 2 Model Status**",
        f"• **Provider:** `{canon_prov}` *({prov_scope})*",
        f"• **Model:** `{eff_model}` *({model_scope})*",
        f"• **Context Window:** {limit:,} tokens",
        f"• **Status:** {status_tag}\n",
        "💡 Use `/models` to view available choices.",
        "💡 Use `/model <provider> <model_id>` to switch model for this session.",
    ]
    if session.active_provider or session.active_model:
        lines.append("💡 Use `/model default` to revert this session to system default.")

    return "\n".join(lines)


def switch_session_model(
    session: SessionState,
    raw_args: str,
    policy: PermissionPolicy,
    default_provider: Optional[str] = None,
    default_model: Optional[str] = None,
    system2_factory: Optional[Callable[..., Any]] = None,
) -> Tuple[bool, str]:
    """Validate and apply a session-scoped System 2 model override.

    Guarantees:
    - Verifies policy permission (CHANGE_MODEL).
    - Validates provider and model against supported catalogs.
    - Validates credentials and preflight boundaries.
    - If validation fails, previous session state is preserved completely.
    - Never exposes secrets in returned error messages.

    Returns:
        (success: bool, user_message: str)
    """
    # 1. Permission Gate
    if not policy.is_allowed(PermissionAction.CHANGE_MODEL):
        err = f"Model configuration changes are not permitted by policy for channel '{policy.channel}'."
        return False, f"⛔ Permission Denied: {err}"

    tokens = [t.strip() for t in (raw_args or "").split() if t.strip()]

    # 2. Revert / Reset command
    if len(tokens) == 1 and tokens[0].lower() in ("default", "reset", "clear"):
        session.active_provider = None
        session.active_model = None
        eff_prov = default_provider or get_system2_provider()
        eff_mod = default_model or get_default_model_for_provider(eff_prov)
        return (
            True,
            f"✅ Model override cleared. Session reverted to system default:\n"
            f"• **Provider:** `{eff_prov}`\n"
            f"• **Model:** `{eff_mod}`",
        )

    # 3. Parse <provider> and <model_id>
    raw_prov: Optional[str] = None
    raw_mid: Optional[str] = None

    if len(tokens) == 2:
        raw_prov = tokens[0].strip(" :,;")
        raw_mid = tokens[1].strip(" ,;")
    elif len(tokens) == 1 and "/" in tokens[0]:
        parts = tokens[0].split("/", 1)
        raw_prov = parts[0].strip(" :,;")
        raw_mid = parts[1].strip(" ,;")
    elif len(tokens) == 1:
        return (
            False,
            "⚠️ Invalid syntax: Missing model ID.\n"
            "**Usage:** `/model <provider> <model_id>`\n"
            "*Example:* `/model claude claude-3-5-sonnet-20241022`\n"
            "Use `/models` to list supported providers and models.",
        )
    else:
        return (
            False,
            "⚠️ Invalid syntax: Expected `<provider> <model_id>`.\n"
            "**Usage:** `/model <provider> <model_id>`\n"
            "*Example:* `/model claude claude-3-5-sonnet-20241022`\n"
            "Use `/models` to list supported choices.",
        )

    # 4. Validate Provider
    canon_prov = get_canonical_provider(raw_prov)
    if not canon_prov:
        supported_str = ", ".join(sorted(SUPPORTED_SYSTEM2_PROVIDERS))
        return (
            False,
            f"❌ Unsupported provider '{raw_prov}'.\n"
            f"Supported providers: `{supported_str}`.\n"
            "Use `/models` to view available models per provider.",
        )

    # 5. Validate Model for Provider
    if not is_model_supported_for_provider(canon_prov, raw_mid):
        return (
            False,
            f"❌ Unsupported model `{raw_mid}` for provider `{canon_prov}`.\n"
            f"Use `/models {canon_prov}` to see valid models for this provider.",
        )

    # 6. Validate Credentials & Configuration (Fails closed without changing state)
    is_valid, cred_err = check_provider_credentials(
        canon_prov, model=raw_mid, system2_factory=system2_factory
    )
    if not is_valid:
        scrubbed_err = scrub_secrets(cred_err)
        return (
            False,
            f"❌ Cannot switch to `{canon_prov}`: {scrubbed_err}.\n"
            "Switching aborted; previous session model configuration was preserved.",
        )

    # 7. Apply Session-Scoped Override (Preserves previous configuration on any failure)
    matched_models = [
        mid for mid, _, _ in get_supported_models_for_provider(canon_prov)
        if mid.lower() == raw_mid.lower()
    ]
    canonical_mid = matched_models[0] if matched_models else raw_mid

    session.active_provider = canon_prov
    session.active_model = canonical_mid

    limit = get_model_context_limit(canonical_mid)
    return (
        True,
        f"✅ System 2 model switched for this session:\n"
        f"• **Provider:** `{canon_prov}`\n"
        f"• **Model:** `{canonical_mid}`\n"
        f"• **Context Window:** {limit:,} tokens\n\n"
        "Subsequent requests in this Telegram session will use this model.",
    )
