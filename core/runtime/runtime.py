"""BrainFrog Runtime — Unified Agent Runtime Boundary.

A thin, high-level facade connecting incoming channels (CLI, Telegram, WhatsApp)
to the existing System 1, System 2, and Orchestrator execution engines.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core.config import get_system1
from core.modules import load_module_map
from core.plans import format_plan_handoff, get_latest_plan
from orchestrator import Orchestrator, RunConfig, StepResult
from system2 import System2Client

from .messages import AgentEvent, IncomingMessage, OutgoingMessage
from .permissions import (
    ChannelTrustLevel,
    PermissionAction,
    PermissionPolicy,
    classify_request_action,
    evaluate_channel_action,
    get_default_policy,
)
from .session import SessionManager, SessionState, session_manager


def scrub_secrets(text: str) -> str:
    """Scrub sensitive keys, tokens, and credentials from outgoing text."""
    if not text:
        return text
    import re
    # Known secret environment variables
    secret_env_vars = [
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN",
        "TYPESAFE_API_KEY", "WHATSAPP_API_TOKEN", "CUSTOM_API_KEY",
        "OPENROUTER_API_KEY", "GH_TOKEN", "GITHUB_TOKEN",
    ]
    scrubbed = text
    for var in secret_env_vars:
        val = os.environ.get(var)
        if val and len(val) >= 6:
            scrubbed = scrubbed.replace(val, f"[{var}_REDACTED]")

    # Generic patterns for API keys and tokens
    scrubbed = re.sub(r"sk-[a-zA-Z0-9_\-]{20,}", "[REDACTED_API_KEY]", scrubbed)
    scrubbed = re.sub(r"\b\d{8,11}:[A-Za-z0-9_-]{30,40}\b", "[REDACTED_BOT_TOKEN]", scrubbed)
    scrubbed = re.sub(r"(?i)\bBearer\s+[a-zA-Z0-9_\-\.]{8,}\b", "Bearer [REDACTED_TOKEN]", scrubbed)
    return scrubbed


class BrainFrogRuntime:
    """Thin runtime boundary wrapping BrainFrog's dual-system architecture.

    Does NOT duplicate orchestrator logic. Delegates plan execution, retries,
    MCP browser verification, and git checkpointing directly to the existing
    `Orchestrator`.
    """

    def __init__(
        self,
        repo_dir: Optional[Path] = None,
        default_backend: str = "jev",
        default_provider: Optional[str] = None,
        default_model: Optional[str] = None,
        default_test_cmd: Optional[str] = None,
        sessions: Optional[SessionManager] = None,
        policy_provider: Optional[Callable[[str], PermissionPolicy]] = None,
        system1_factory: Optional[Callable[[str], Any]] = None,
        system2_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.repo_dir = (repo_dir or Path.cwd()).resolve()
        self.default_backend = default_backend
        self.default_provider = default_provider
        self.default_model = default_model
        self.default_test_cmd = default_test_cmd
        self.sessions = sessions or session_manager
        self.policy_provider = policy_provider or get_default_policy
        self.system1_factory = system1_factory or get_system1
        self.system2_factory = system2_factory or System2Client

    def _resolve_test_cmd(self, custom_cmd: Optional[str]) -> List[str]:
        if custom_cmd:
            return custom_cmd.split()
        if self.default_test_cmd:
            return self.default_test_cmd.split()
        # Default fallback test detection
        from cli import detect_default_test_cmd
        return detect_default_test_cmd(self.repo_dir).split()

    def handle_message(
        self,
        message: IncomingMessage,
        on_event: Optional[Callable[[AgentEvent], None]] = None,
    ) -> OutgoingMessage:
        """Process an incoming normalized message and return a normalized response."""
        events: List[AgentEvent] = []

        def emit(event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
            ev = AgentEvent(type=event_type, timestamp=time.time(), payload=payload or {})
            events.append(ev)
            if on_event:
                try:
                    on_event(ev)
                except Exception:
                    pass

        emit("agent.started", {"session_id": message.session_id, "channel": message.channel})

        # Anti-spoofing check: Verify claimed channel identity
        effective_channel = message.channel
        if message.channel in ("cli", "local", "terminal"):
            allowed_local_users = {"local", "cli", "system", "terminal", "default"}
            current_user = os.environ.get("USERNAME") or os.environ.get("USER")
            if current_user:
                allowed_local_users.add(current_user.lower())
            user_id_clean = message.user_id.lower().strip()
            if user_id_clean not in allowed_local_users:
                # Untrusted external user claiming local CLI -> downgrade to remote channel
                effective_channel = "remote_channel"

        # 1. Resolve Session State
        session: SessionState = self.sessions.get_or_create(
            channel=effective_channel,
            user_id=message.user_id,
            conversation_id=message.conversation_id,
            default_mode=message.metadata.get("mode", "build"),
        )

        # 2. Deterministic Permission Gate
        policy = self.policy_provider(effective_channel)
        requested_mode = message.metadata.get("mode") or session.active_mode

        raw_text = message.text.strip()

        # Handle slash commands for remote/all channels
        if raw_text.startswith("/"):
            parts = raw_text.split(maxsplit=1)
            cmd = parts[0].lower()

            if cmd in ("/help", "/start"):
                help_text = (
                    "🐸 **BrainFrog Agent Runtime**\n\n"
                    "Available commands:\n"
                    "• `/help` — Show available commands and instructions\n"
                    "• `/status` — View active session and runtime diagnostics\n"
                    "• `/doctor` — Run comprehensive system & configuration check\n"
                    "• `/reset` or `/new` — Reset current conversation session\n\n"
                    "💡 You can query the repository, request architecture analysis, or generate plans.\n"
                    "⚠️ Local CLI commands (`/undo`, `/diff`, `/preview`, `/screenshot`, `/paste`, shell execution) are restricted to local CLI."
                )
                return OutgoingMessage(text=help_text, events=events, success=True, status="completed")

            if cmd in ("/status", "/info"):
                status_text = (
                    f"🐸 **BrainFrog Status**\n"
                    f"• Channel: `{effective_channel}`\n"
                    f"• Session ID: `{session.session_id}`\n"
                    f"• Active Mode: `{session.active_mode}`\n"
                    f"• History Turns: `{len(session.history)}`\n"
                    f"• Trust Level: `{policy.trust_level}`"
                )
                return OutgoingMessage(text=status_text, events=events, success=True, status="completed")

            if cmd in ("/doctor", "/diagnose"):
                from .doctor import run_doctor_diagnostics
                checks = run_doctor_diagnostics(self.repo_dir)
                lines = ["🐸 **BrainFrog Doctor Diagnostics:**\n"]
                for c in checks:
                    icon = "✅" if c.status == "OK" else ("⚠️" if c.status == "WARN" else ("❌" if c.status == "FAIL" else "ℹ️"))
                    lines.append(f"{icon} **[{c.category}]** {c.name}: {c.detail}")
                return OutgoingMessage(text="\n".join(lines), events=events, success=True, status="completed")

            if cmd in ("/reset", "/new"):
                session.reset()
                return OutgoingMessage(text="🔄 Session context has been reset.", events=events, success=True, status="completed")

            if cmd in ("/undo", "/diff", "/preview", "/screenshot", "/paste", "/attach", "/images", "/clear-images"):
                reason = f"Command '{cmd}' is only available in the local CLI interface."
                emit("agent.rejected", {"command": cmd, "reason": reason})
                return OutgoingMessage(
                    text=f"⚠️ {reason}",
                    events=events,
                    success=False,
                    status="rejected",
                    error=reason,
                )

        action = classify_request_action(raw_text, message.metadata)

        allowed, reason = evaluate_channel_action(
            effective_channel, action, policy
        )
        if not allowed:
            emit("agent.rejected", {"action": action.value, "reason": reason})
            return OutgoingMessage(
                text=f"⚠️ Permission Denied: {reason}",
                events=events,
                success=False,
                status="rejected",
                error=reason,
            )

        # For remote channels, enforce read-only / plan mode if code editing is not permitted
        effective_mode = requested_mode
        if policy.trust_level == ChannelTrustLevel.REMOTE_CHANNEL.value:
            if not policy.allow_code_edits and requested_mode == "build":
                # Fallback to plan mode so remote users get safe architectural analysis & PRD
                effective_mode = "plan"
                emit("agent.mode.fallback", {"from": "build", "to": "plan", "reason": "remote_channel_read_only"})

        # 3. Resolve Workspace & Providers
        workspace_dir = Path(message.metadata.get("repo_dir") or self.repo_dir).resolve()
        backend = message.metadata.get("backend") or self.default_backend
        provider = message.metadata.get("provider") or session.active_provider or self.default_provider
        model = message.metadata.get("model") or session.active_model or self.default_model
        skill = message.metadata.get("skill") or session.active_skill
        auto_pr = bool(message.metadata.get("auto_pr", False)) and policy.allow_auto_pr
        max_retries = int(message.metadata.get("max_retries", 3))

        test_cmd_list = self._resolve_test_cmd(message.metadata.get("test_cmd"))

        # Plan context resolution
        plan_context = message.metadata.get("plan_context") or session.plan_context
        if effective_mode == "build" and not plan_context:
            try:
                lp = get_latest_plan(workspace_dir)
                if lp:
                    plan_context = format_plan_handoff(lp, workspace_dir)
            except Exception:
                pass

        # 4. Instantiate System 1 & System 2 Clients
        try:
            s1 = self.system1_factory(backend)
        except Exception as e:
            err_msg = f"System 1 Initialization Error: {e}"
            emit("agent.error", {"error": err_msg})
            return OutgoingMessage(text=f"❌ {err_msg}", events=events, success=False, error=err_msg)

        try:
            s2 = self.system2_factory(model=model, provider=provider)
        except Exception as e:
            err_msg = f"System 2 Initialization Error: {e}"
            emit("agent.error", {"error": err_msg})
            return OutgoingMessage(text=f"❌ {err_msg}", events=events, success=False, error=err_msg)

        # 5. Load Domains & Prepare Orchestrator Config
        can_auto_create = bool(effective_mode == "build" and policy.allow_code_edits)
        domains = load_module_map(workspace_dir, task=raw_text, auto_create=can_auto_create)

        effective_task = raw_text
        if session.history:
            history_lines = []
            for turn in session.history[-3:]:
                u_text = turn.get("user", "").strip()
                a_text = turn.get("assistant", "").strip()
                if len(a_text) > 300:
                    a_text = a_text[:300] + "..."
                history_lines.append(f"User: {u_text}\nAssistant: {a_text}")
            if history_lines:
                effective_task = f"{raw_text}\n\n[Previous Conversation Context]\n" + "\n---\n".join(history_lines)

        cfg = RunConfig(
            repo_dir=workspace_dir,
            task=effective_task,
            test_command=test_cmd_list,
            max_retries=max_retries,
            auto_pr=auto_pr,
            domains=domains,
            skill=skill,
            mode=effective_mode,
            plan_context=plan_context,
            attached_images=list(message.attachments),
        )

        def log_runtime(msg: str) -> None:
            clean = msg.strip()
            if not clean:
                return
            if clean.startswith("=== Step") and clean.endswith("==="):
                emit("agent.step.started", {"step": clean.strip("= ")})
            elif "scope_gate results" in clean or "scope gate" in clean:
                emit("agent.system1.completed", {"log": clean})
            elif "planning task" in clean or "writing code" in clean or "PRD" in clean:
                emit("agent.system2.started", {"log": clean})

        orchestrator = Orchestrator(
            system1=s1,
            system2=s2,
            config=cfg,
            log_fn=log_runtime,
        )

        # 6. Execute via existing Orchestrator
        try:
            results: List[StepResult] = orchestrator.run()
        except Exception as e:
            err_msg = f"Orchestration Error: {e}"
            emit("agent.error", {"error": err_msg})
            return OutgoingMessage(
                text=f"❌ {err_msg}",
                events=events,
                success=False,
                status="error",
                error=err_msg,
                metadata={
                    "session_id": session.session_id,
                    "mode": effective_mode,
                },
            )

        emit("agent.completed", {"steps_count": len(results)})

        # 7. Format Outgoing Response Text
        reply_texts: List[str] = []
        is_success = True

        for r in results:
            if r.outcome == "diagnosed" and r.detail:
                reply_texts.append(r.detail)
            elif r.outcome == "planned" and r.detail:
                reply_texts.append(r.detail)
                session.plan_context = r.detail
            elif r.outcome == "needs_clarification" and r.detail:
                reply_texts.append(f"❓ {r.detail}")
            elif r.outcome in ("opened_pr", "drafted_pr"):
                pr_info = f"✓ {r.step.description}\n{r.detail}"
                reply_texts.append(pr_info)
            elif r.outcome in ("escalated", "abandoned"):
                is_success = False
                reply_texts.append(f"⚠️ {r.step.description}: {r.outcome} — {r.detail}")
            elif r.detail:
                reply_texts.append(r.detail)

        final_text = scrub_secrets("\n\n".join(reply_texts).strip() or "Task completed.")

        # Record in isolated session history
        session.record_interaction(user_text=raw_text, assistant_text=final_text)

        return OutgoingMessage(
            text=final_text,
            events=events,
            success=is_success,
            status="completed" if is_success else "error",
            metadata={
                "session_id": session.session_id,
                "mode": effective_mode,
                "steps_count": len(results),
            },
        )
