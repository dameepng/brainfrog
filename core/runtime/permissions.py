"""Channel-aware security and permission boundary for BrainFrog.

Defines trust levels (LOCAL_CLI vs REMOTE_CHANNEL) and enforces strict permission
boundaries to prevent remote channels (Telegram, WhatsApp) from executing arbitrary
shell commands, modifying credentials, or performing destructive operations.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Set, Tuple


class ChannelTrustLevel(str, Enum):
    LOCAL_CLI = "local_cli"
    REMOTE_CHANNEL = "remote_channel"


class PermissionAction(str, Enum):
    READ_CODE = "read_code"
    DIAGNOSE = "diagnose"
    PLAN = "plan"
    WRITE_CODE = "write_code"
    WRITE_FILES = "write_files"
    RUN_TESTS = "run_tests"
    GIT_SAFE_OPS = "git_safe_ops"
    GIT_DESTRUCTIVE = "git_destructive"
    DESTRUCTIVE_GIT = "destructive_git"
    SHELL_EXECUTION = "shell_execution"
    RUN_SHELL_COMMAND = "run_shell_command"
    CREDENTIAL_ACCESS = "credential_access"
    ACCESS_CREDENTIALS = "access_credentials"
    DEPLOYMENT = "deployment"
    DEPLOY_PROJECT = "deploy_project"


# Canonical mapping for synonyms
CANONICAL_ACTION_MAP: Dict[str, str] = {
    PermissionAction.RUN_SHELL_COMMAND.value: PermissionAction.SHELL_EXECUTION.value,
    PermissionAction.SHELL_EXECUTION.value: PermissionAction.SHELL_EXECUTION.value,
    PermissionAction.DESTRUCTIVE_GIT.value: PermissionAction.GIT_DESTRUCTIVE.value,
    PermissionAction.GIT_DESTRUCTIVE.value: PermissionAction.GIT_DESTRUCTIVE.value,
    PermissionAction.ACCESS_CREDENTIALS.value: PermissionAction.CREDENTIAL_ACCESS.value,
    PermissionAction.CREDENTIAL_ACCESS.value: PermissionAction.CREDENTIAL_ACCESS.value,
    PermissionAction.DEPLOY_PROJECT.value: PermissionAction.DEPLOYMENT.value,
    PermissionAction.DEPLOYMENT.value: PermissionAction.DEPLOYMENT.value,
    PermissionAction.WRITE_FILES.value: PermissionAction.WRITE_CODE.value,
    PermissionAction.WRITE_CODE.value: PermissionAction.WRITE_CODE.value,
}


# Actions that are unconditionally denied to remote messaging channels
REMOTE_PROHIBITED_ACTIONS: Set[str] = {
    PermissionAction.SHELL_EXECUTION.value,
    PermissionAction.RUN_SHELL_COMMAND.value,
    PermissionAction.GIT_DESTRUCTIVE.value,
    PermissionAction.DESTRUCTIVE_GIT.value,
    PermissionAction.CREDENTIAL_ACCESS.value,
    PermissionAction.ACCESS_CREDENTIALS.value,
    PermissionAction.DEPLOYMENT.value,
    PermissionAction.DEPLOY_PROJECT.value,
}


@dataclass
class PermissionPolicy:
    """Security policy governing what actions an interface/channel can perform."""

    channel: str
    trust_level: str
    allowed_actions: Set[str] = field(default_factory=set)
    allow_code_edits: bool = False
    allow_auto_pr: bool = False

    def is_allowed(self, action: str | PermissionAction) -> bool:
        act = action.value if isinstance(action, PermissionAction) else str(action)
        canon = CANONICAL_ACTION_MAP.get(act, act)

        if self.trust_level == ChannelTrustLevel.REMOTE_CHANNEL.value:
            if canon in REMOTE_PROHIBITED_ACTIONS or act in REMOTE_PROHIBITED_ACTIONS:
                return False
            if canon == PermissionAction.WRITE_CODE.value:
                return self.allow_code_edits
            if canon == PermissionAction.RUN_TESTS.value:
                return self.allow_code_edits

        return (
            act in self.allowed_actions
            or canon in self.allowed_actions
        )


def resolve_channel_trust_level(channel: str) -> str:
    """Resolve trust tier for a channel name."""
    clean = (channel or "").lower().strip()
    if clean in ("cli", "local", "terminal"):
        return ChannelTrustLevel.LOCAL_CLI.value
    return ChannelTrustLevel.REMOTE_CHANNEL.value


def get_default_policy(channel: str, allow_code_edits: bool = False) -> PermissionPolicy:
    """Return default security policy for a channel."""
    trust = resolve_channel_trust_level(channel)
    if trust == ChannelTrustLevel.LOCAL_CLI.value:
        allowed = {
            PermissionAction.READ_CODE.value,
            PermissionAction.DIAGNOSE.value,
            PermissionAction.PLAN.value,
            PermissionAction.WRITE_CODE.value,
            PermissionAction.WRITE_FILES.value,
            PermissionAction.RUN_TESTS.value,
            PermissionAction.GIT_SAFE_OPS.value,
            PermissionAction.SHELL_EXECUTION.value,
            PermissionAction.RUN_SHELL_COMMAND.value,
            PermissionAction.CREDENTIAL_ACCESS.value,
            PermissionAction.ACCESS_CREDENTIALS.value,
            PermissionAction.DEPLOYMENT.value,
            PermissionAction.DEPLOY_PROJECT.value,
        }
        return PermissionPolicy(
            channel=channel,
            trust_level=trust,
            allowed_actions=allowed,
            allow_code_edits=True,
            allow_auto_pr=True,
        )

    # Remote messaging channels (Telegram, WhatsApp) start with read/diagnose/plan only
    allowed = {
        PermissionAction.READ_CODE.value,
        PermissionAction.DIAGNOSE.value,
        PermissionAction.PLAN.value,
    }
    if allow_code_edits:
        allowed.add(PermissionAction.WRITE_CODE.value)
        allowed.add(PermissionAction.WRITE_FILES.value)
        allowed.add(PermissionAction.RUN_TESTS.value)
        allowed.add(PermissionAction.GIT_SAFE_OPS.value)

    return PermissionPolicy(
        channel=channel,
        trust_level=trust,
        allowed_actions=allowed,
        allow_code_edits=allow_code_edits,
        allow_auto_pr=False,
    )


def classify_request_action(
    text: str,
    metadata: Optional[Dict[str, Any]] = None,
    s1_decision: Optional[Any] = None,
) -> PermissionAction:
    """Deterministically classify the requested action from text and metadata.

    Security rule:
    System 1 may classify intent or risk, but this deterministic classifier
    ensures requests for shell, credentials, destructive git, or deployment
    are pinned to their exact PermissionAction so policy can deny them.
    """
    import re

    meta = metadata or {}
    req = meta.get("requested_action") or meta.get("action")
    if req:
        if isinstance(req, PermissionAction):
            return req
        req_clean = str(req).lower().strip()
        for pa in PermissionAction:
            if pa.value == req_clean or pa.name.lower() == req_clean:
                return pa

    clean = (text or "").strip().lower()

    # 1. Shell commands (!command or explicit phrasing)
    if clean.startswith("!"):
        return PermissionAction.SHELL_EXECUTION

    shell_patterns = [
        r"\b(?:run|execute|call)\s+(?:pytest|test|tests|npm|bash|sh|cmd|powershell|pwsh|python|node|cargo|go|make|script)\b",
        r"\b(?:use|open)\s+(?:the\s+)?(?:terminal|shell|console|command\s+line)\b",
        r"\bexecute\s+git\s+status\b",
        r"\brun\s+(?:the\s+)?build\s+command\b",
        r"\brun\s+(?:a\s+)?shell\b",
    ]
    for pat in shell_patterns:
        if re.search(pat, clean):
            return PermissionAction.SHELL_EXECUTION

    # 2. Destructive git & destructive filesystem
    destructive_patterns = [
        r"\bgit\s+(?:reset\s+--hard|push\s+(?:--force|-f)|clean\s+-fd|branch\s+-D)\b",
        r"\b(?:delete|destroy|remove|wipe)\s+(?:the\s+)?(?:project|repo|repository|codebase|all\s+files)\b",
        r"\brm\s+-rf\b",
        r"\bdelete\s+the\s+project\s+repository\b",
    ]
    for pat in destructive_patterns:
        if re.search(pat, clean):
            return PermissionAction.GIT_DESTRUCTIVE

    # 3. Credential access
    cred_patterns = [
        r"\b(?:show|read|print|get|reveal|leak|display|cat)\s+(?:me\s+)?(?:the\s+)?(?:.*?)?(?:api[_\s]?key|secret|token|password|credentials?|\.env|env\s+vars?|environment\s+variables?)\b",
        r"\b(?:what\s+is|show)\s+(?:the\s+)?(?:telegram\s+bot\s+token|openai\s+api\s+key|anthropic\s+api\s+key)\b",
        r"\bread\s+\.env\b",
        r"\bprint\s+environment\s+variables\b",
        r"\bshow\s+telegram\s+bot\s+token\b",
    ]
    for pat in cred_patterns:
        if re.search(pat, clean):
            return PermissionAction.CREDENTIAL_ACCESS

    # 4. Deployment
    deploy_patterns = [
        r"\b(?:deploy|push\s+to\s+production|push\s+the\s+production|restart\s+production|restart\s+prod|deploy\s+the\s+project)\b",
        r"\bdeploy\s+the\s+project\b",
        r"\bpush\s+the\s+production\s+build\b",
    ]
    for pat in deploy_patterns:
        if re.search(pat, clean):
            return PermissionAction.DEPLOYMENT

    # 5. File modifications / code mutation / deletions
    write_patterns = [
        r"\b(?:modify|edit|update|rewrite|create|write|delete|remove)\s+(?:the\s+)?(?:file|files|src\/|app\/|code|temporary\s+files|temp\s+files|[a-zA-Z0-9_\-\.\/]+\.(?:py|js|ts|json|html|css|yaml|yml|md|txt))\b",
        r"\b(?:create\s+a\s+new\s+file|rewrite\s+this\s+configuration|apply\s+the\s+requested\s+code\s+changes|delete\s+the\s+temporary\s+files)\b",
    ]
    for pat in write_patterns:
        if re.search(pat, clean):
            return PermissionAction.WRITE_CODE

    # 6. Git operations (commit, checkout, staging)
    git_patterns = [
        r"\b(?:git\s+commit|git\s+add|git\s+checkout|commit\s+(?:the\s+)?changes?|make\s+a\s+commit)\b",
    ]
    for pat in git_patterns:
        if re.search(pat, clean):
            return PermissionAction.GIT_SAFE_OPS

    # 7. Fallback based on question vs plan vs read
    if clean.endswith("?") or any(clean.startswith(w) for w in ("how ", "what ", "why ", "explain ", "describe ", "list ")):
        return PermissionAction.DIAGNOSE

    return PermissionAction.PLAN


def evaluate_channel_action(
    channel: str,
    action: str | PermissionAction,
    policy: Optional[PermissionPolicy] = None,
) -> Tuple[bool, str]:
    """Evaluate whether an action is permitted for a given channel.

    Returns:
        (is_allowed: bool, reason_or_status: str)
    """
    act = action.value if isinstance(action, PermissionAction) else str(action)
    canon = CANONICAL_ACTION_MAP.get(act, act)
    pol = policy or get_default_policy(channel)

    if not pol.is_allowed(act):
        if canon in REMOTE_PROHIBITED_ACTIONS or act in REMOTE_PROHIBITED_ACTIONS:
            return (
                False,
                f"Action '{act}' is strictly prohibited for remote channel '{channel}' "
                "for security protection.",
            )
        if canon == PermissionAction.WRITE_CODE.value and not pol.allow_code_edits:
            return (
                False,
                f"Code modification is restricted on remote channel '{channel}'. "
                "Remote channel is operating in read-only / diagnostic mode.",
            )
        return (
            False,
            f"Action '{act}' is not permitted by policy for channel '{channel}'.",
        )

    return True, "Allowed"
