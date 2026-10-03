"""BrainFrog Doctor — System, Provider, and Channel Diagnostics.

Audits runtime integrity, System 1, System 2, API credentials, MCP, memory,
skills, Telegram/WhatsApp configurations, and channel permissions.

CRITICAL SECURITY RULE:
Never exposes API keys, bot tokens, or private secrets in logs or output.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.memory import load_global_memory, load_workspace_memory
from core.runtime.permissions import ChannelTrustLevel, PermissionAction, get_default_policy
from core.skills import index_skills
from system2 import find_antigravity_bin, get_model_context_limit, get_system2_provider


@dataclass
class DiagnosticCheck:
    category: str
    name: str
    status: str  # "OK", "WARN", "FAIL", "INFO"
    detail: str


def redact_secret(val: Optional[str]) -> str:
    """Safely redact secret credentials."""
    if not val:
        return "[NOT CONFIGURED]"
    clean = val.strip()
    if len(clean) <= 8:
        return "[CONFIGURED: ***]"
    return f"{clean[:4]}...{clean[-4:]}"


def run_doctor_diagnostics(repo_dir: Optional[Path] = None) -> List[DiagnosticCheck]:
    """Execute complete health and configuration check for BrainFrog."""
    target_repo = (repo_dir or Path.cwd()).resolve()
    checks: List[DiagnosticCheck] = []

    # 1. Workspace & Git
    is_git = (target_repo / ".git").exists()
    checks.append(DiagnosticCheck(
        category="Workspace",
        name="Git Repository",
        status="OK" if is_git else "WARN",
        detail=str(target_repo) if is_git else f"{target_repo} is not a git repository",
    ))

    bf_dir = target_repo / ".brainfrog"
    checks.append(DiagnosticCheck(
        category="Workspace",
        name="Workspace Directory (.brainfrog)",
        status="OK" if bf_dir.exists() else "INFO",
        detail="Present" if bf_dir.exists() else "Not initialized (run /init or create rules)",
    ))

    # 2. System 1 (Jev / TypeSafe)
    ts_key = os.environ.get("TYPESAFE_API_KEY") or os.environ.get("JEV_API_KEY")
    checks.append(DiagnosticCheck(
        category="System 1",
        name="TypeSafe / Jev Decision Engine",
        status="OK" if ts_key else "FAIL",
        detail=f"Key: {redact_secret(ts_key)}" if ts_key else "Missing TYPESAFE_API_KEY in .env",
    ))

    # 3. System 2 (Generative AI)
    active_prov = get_system2_provider()
    checks.append(DiagnosticCheck(
        category="System 2",
        name="Active AI Provider",
        status="OK",
        detail=active_prov,
    ))

    if active_prov == "antigravity":
        bin_path = find_antigravity_bin()
        status = "OK" if bin_path else "FAIL"
        detail = bin_path or "Antigravity binary ('agy.exe') not found in ~/.gemini/bin or PATH"
        checks.append(DiagnosticCheck(
            category="System 2",
            name="Google Antigravity CLI",
            status=status,
            detail=detail,
        ))
        from security.auth_manager import get_active_account
        acct = get_active_account()
        checks.append(DiagnosticCheck(
            category="System 2",
            name="Active Google Auth Session",
            status="OK" if acct else "WARN",
            detail=acct or "No active Google login session detected",
        ))
    elif active_prov == "claude":
        c_key = os.environ.get("ANTHROPIC_API_KEY")
        checks.append(DiagnosticCheck(
            category="System 2",
            name="Anthropic API Authentication",
            status="OK" if c_key else "FAIL",
            detail=f"Key: {redact_secret(c_key)}",
        ))
    elif active_prov in ("openai", "openrouter", "openai-compatible"):
        o_key = (
            os.environ.get("OPENROUTER_API_KEY")
            if active_prov == "openrouter"
            else os.environ.get("OPENAI_API_KEY") or os.environ.get("BRAINFROG_MODEL_API_KEY")
        )
        checks.append(DiagnosticCheck(
            category="System 2",
            name=f"{active_prov.capitalize()} Authentication",
            status="OK" if o_key else "FAIL",
            detail=f"Key: {redact_secret(o_key)}",
        ))

    model_name = os.environ.get("ANTIGRAVITY_MODEL") or os.environ.get("ANTHROPIC_MODEL") or os.environ.get("OPENAI_MODEL") or "default"
    limit = get_model_context_limit(model_name)
    checks.append(DiagnosticCheck(
        category="System 2",
        name="Model Context Limit",
        status="OK",
        detail=f"{limit:,} tokens for model '{model_name}'",
    ))

    # 4. MCP & Quality Gate
    node_bin = shutil.which("node") or shutil.which("node.exe")
    checks.append(DiagnosticCheck(
        category="MCP Tools",
        name="Node.js Runtime",
        status="OK" if node_bin else "WARN",
        detail=node_bin or "Node.js not detected (required for MCP browser quality gate)",
    ))

    # 5. Memory & Continuous Learning
    ws_mem = load_workspace_memory(target_repo)
    gl_mem = load_global_memory()
    total_learnings = len(ws_mem.learnings) + len(gl_mem.learnings)
    checks.append(DiagnosticCheck(
        category="Memory",
        name="Continuous Learning Store",
        status="OK",
        detail=f"{total_learnings} total rules ({len(ws_mem.learnings)} workspace, {len(gl_mem.learnings)} global)",
    ))

    # 6. Modular Skills
    skills = index_skills(target_repo)
    checks.append(DiagnosticCheck(
        category="Skills",
        name="Modular Skills Catalog",
        status="OK",
        detail=f"{len(skills)} available skill(s) indexed",
    ))

    # 7. Telegram Channel
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_users = os.environ.get("TELEGRAM_ALLOWED_USERS", "").strip()
    tg_status = "OK" if tg_token and tg_users else ("WARN" if tg_token else "INFO")
    if tg_token:
        tg_detail = f"Token: {redact_secret(tg_token)}, Allowlist: {len(tg_users.split(','))} user(s)"
    else:
        tg_detail = "Disabled (TELEGRAM_BOT_TOKEN not set)"
    checks.append(DiagnosticCheck(
        category="Channels",
        name="Telegram Channel",
        status=tg_status,
        detail=tg_detail,
    ))

    # 8. WhatsApp Channel
    wa_enabled = os.environ.get("WHATSAPP_ENABLED", "").lower().strip() in ("1", "true", "yes")
    wa_users = os.environ.get("WHATSAPP_ALLOWED_USERS", "").strip()
    checks.append(DiagnosticCheck(
        category="Channels",
        name="WhatsApp Channel",
        status="OK" if wa_enabled else "INFO",
        detail=f"Enabled (allowlist: {len(wa_users.split(','))} number(s))" if wa_enabled else "Disabled",
    ))

    # 9. Channel Security Policy
    cli_policy = get_default_policy("cli")
    tg_policy = get_default_policy("telegram")
    sec_detail = (
        f"CLI: {cli_policy.trust_level} (allow_code_edits={cli_policy.allow_code_edits}) | "
        f"Telegram/WhatsApp: {tg_policy.trust_level} (allow_code_edits={tg_policy.allow_code_edits}, shell_prohibited=True)"
    )
    checks.append(DiagnosticCheck(
        category="Security",
        name="Channel Permission Policies",
        status="OK",
        detail=sec_detail,
    ))

    return checks
