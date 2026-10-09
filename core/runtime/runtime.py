"""BrainFrog unified agent runtime boundary."""
#Session-authority-checks-use-canonical-server-state-only.

from __future__ import annotations

from enum import Enum
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union, TYPE_CHECKING

from core.config import get_system1
from core.modules import load_module_map
from core.plans import format_plan_handoff, get_latest_plan
if TYPE_CHECKING:
    from orchestrator import Orchestrator, RunConfig, StepResult
from system2 import System2Client


def __getattr__(name: str) -> Any:
    if name in ("Orchestrator", "RunConfig", "StepResult"):
        import orchestrator
        val = getattr(orchestrator, name)
        globals()[name] = val
        return val
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")

from .messages import AgentEvent, IncomingMessage, OutgoingMessage
from .permissions import (
    ChannelTrustLevel,
    PermissionAction,
    PermissionPolicy,
    classify_request_action,
    evaluate_channel_action,
    get_default_policy,
)
from .session import (
    FileSessionStore,
    SessionManager,
    SessionState,
    SessionStore,
    StaleSessionStateError,
    scrub_secrets,
    session_manager,
)
from .session_authority import transition_with_current_session as twcs
from .approval import (
    ApprovalTransitionContext,
    ApprovalPayloadTooLargeError,
    ApprovalQuotaExceededError,
    ApprovalService,
    ApprovalStatus,
    ApprovalStore,
    FileApprovalStore,
    RiskClass,
    extract_canonical_operation,
)
from .approval_presentation import (
    ApprovalPresentation,
    parse_telegram_callback,
    telegram_approval_keyboard,
)
from .capabilities import Capabilities, FilesystemPolicy, NetworkPolicy
from .contract import ApprovedExecutionContract, operation_targets
from .transaction import (
    FileTransactionStore,
    RecoveryResult,
    TransactionRecoveryManager,
    TransactionStore,
    TransactionVerifier,
)
from .remote_work import RemoteWorkCoordinator, RemoteWorkRequest, RemoteWorkResult
from .planning import Planner
from .work import (
    FileWorkStore,
    InvalidWorkTransition,
    MAX_WORKS_PER_RESPONSE,
    StaleWorkRevisionError,
    Work,
    WorkFailure,
    WorkStatus,
    WorkStore,
)
from .work_authorization import (
    authorize_work_execution_continuation,
    authorize_work_inspection,
)
from .work_continuation import cancel_work, resume_work
from .work_presentation import format_work_detail, format_works_list
from .workspace_lock import WorkspaceLock, WorkspaceLockedError


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
        default_test_cmd: Optional[Union[str, List[str]]] = None,
        sessions: Optional[SessionManager] = None,
        session_store: Optional[SessionStore] = None,
        policy_provider: Optional[Callable[[str], PermissionPolicy]] = None,
        system1_factory: Optional[Callable[[str], Any]] = None,
        system2_factory: Optional[Callable[..., Any]] = None,
        persist_sessions: bool = True,
        approval_service: Optional[ApprovalService] = None,
        approval_store: Optional[ApprovalStore] = None,
        require_approval: bool = False,
        two_man_rule_enabled: bool = True,
        max_pending_per_session: Optional[int] = None,
        max_pending_per_requester: Optional[int] = None,
        max_pending_global: Optional[int] = None,
        max_payload_bytes: Optional[int] = None,
        max_terminal_retention: Optional[int] = None,
        max_history_entries: Optional[int] = None,
        max_history_bytes: Optional[int] = None,
        max_message_bytes: Optional[int] = None,
        session_ttl_seconds: Optional[float] = None,
        transaction_store: Optional[TransactionStore] = None,
        auto_recover_transactions: bool = True,
        work_store: Optional[WorkStore] = None,
        planner: Optional[Planner] = None,
        transaction_verifier: Optional[TransactionVerifier] = None,
    ) -> None:
        self.repo_dir = (repo_dir or Path.cwd()).resolve()
        self.default_backend = default_backend
        self.default_provider = default_provider
        self.default_model = default_model
        self.default_test_cmd = default_test_cmd

        session_mgr_kwargs: Dict[str, Any] = {}
        if max_history_entries is not None:
            session_mgr_kwargs["max_history_entries"] = max_history_entries
        if max_history_bytes is not None:
            session_mgr_kwargs["max_history_bytes"] = max_history_bytes
        if max_message_bytes is not None:
            session_mgr_kwargs["max_message_bytes"] = max_message_bytes
        if session_ttl_seconds is not None:
            session_mgr_kwargs["session_ttl_seconds"] = session_ttl_seconds

        if sessions is not None:
            self.sessions = sessions
        elif session_store is not None:
            self.sessions = SessionManager(store=session_store, **session_mgr_kwargs)
        elif persist_sessions:
            self.sessions = SessionManager(store=FileSessionStore(repo_dir=self.repo_dir), **session_mgr_kwargs)
        else:
            self.sessions = session_manager

        self.policy_provider = policy_provider or get_default_policy
        self.system1_factory = system1_factory or get_system1
        self.system2_factory = system2_factory or System2Client

        if approval_service is not None:
            self.approval_service = approval_service
        elif approval_store is not None:
            svc_kwargs: Dict[str, Any] = {"store": approval_store, "two_man_rule_enabled": two_man_rule_enabled}
            if max_pending_per_session is not None:
                svc_kwargs["max_pending_per_session"] = max_pending_per_session
            if max_pending_per_requester is not None:
                svc_kwargs["max_pending_per_requester"] = max_pending_per_requester
            if max_pending_global is not None:
                svc_kwargs["max_pending_global"] = max_pending_global
            if max_payload_bytes is not None:
                svc_kwargs["max_payload_bytes"] = max_payload_bytes
            self.approval_service = ApprovalService(**svc_kwargs)
        else:
            store_kwargs: Dict[str, Any] = {"repo_dir": self.repo_dir}
            if max_terminal_retention is not None:
                store_kwargs["max_terminal_retention"] = max_terminal_retention
            store = FileApprovalStore(**store_kwargs)
            svc_kwargs = {"store": store, "two_man_rule_enabled": two_man_rule_enabled}
            if max_pending_per_session is not None:
                svc_kwargs["max_pending_per_session"] = max_pending_per_session
            if max_pending_per_requester is not None:
                svc_kwargs["max_pending_per_requester"] = max_pending_per_requester
            if max_pending_global is not None:
                svc_kwargs["max_pending_global"] = max_pending_global
            if max_payload_bytes is not None:
                svc_kwargs["max_payload_bytes"] = max_payload_bytes
            self.approval_service = ApprovalService(**svc_kwargs)
        self.require_approval = require_approval
        if transaction_store is not None:
            self.transaction_store = transaction_store
        else:
            self.transaction_store = FileTransactionStore(self.repo_dir)

        self.auto_recover_transactions = auto_recover_transactions
        self.startup_recovery_results: List[RecoveryResult] = []
        if self.auto_recover_transactions:
            self.startup_recovery_results = self.recover_startup_transactions()
        if work_store is not None:
            self.work_store = work_store
        else:
            self.work_store = FileWorkStore(repo_dir=self.repo_dir)
        self.remote_work = RemoteWorkCoordinator(work_store=self.work_store, planner=planner)
        self.transaction_verifier = transaction_verifier
        self._reconcile_recovered_work()

    def recover_startup_transactions(self) -> List[RecoveryResult]:
        """Discover and recover incomplete transactions during runtime initialization."""
        mgr = TransactionRecoveryManager(workspace=self.repo_dir, store=self.transaction_store)
        return mgr.recover_incomplete(limit=50)

    def _reconcile_recovered_work(self) -> None:
        """Map deterministic transaction recovery outcomes back to persisted Work state."""
        for recovery in self.startup_recovery_results:
            tx = self.transaction_store.get(recovery.transaction_id)
            if tx is None or not tx.work_id:
                continue
            work = self.work_store.get(tx.work_id)
            if work is None or work.is_terminal:
                continue
            try:
                if recovery.final_status.value == "committed":
                    if work.status == WorkStatus.EXECUTING:
                        work = self.work_store.save(work.transition(WorkStatus.VERIFYING))
                    if work.status == WorkStatus.VERIFYING:
                        self.work_store.save(work.transition(WorkStatus.DONE))
                elif work.status in (WorkStatus.EXECUTING, WorkStatus.VERIFYING):
                    self.work_store.save(work.transition(
                        WorkStatus.FAILED,
                        failure=WorkFailure(
                            code="TRANSACTION_RECOVERED",
                            stage="recovery",
                            summary=f"In-flight transaction recovered ({recovery.final_status.value})",
                            retryable=True,
                        ),
                    ))
            except (ValueError, KeyError, InvalidWorkTransition, StaleWorkRevisionError):
                # Recovery is authoritative; an incompatible descriptive record fails closed.
                current = self.work_store.get(tx.work_id)
                if current and not current.is_terminal and current.status != WorkStatus.FAILED:
                    try:
                        self.work_store.save(current.transition(
                            WorkStatus.FAILED,
                            failure=WorkFailure(
                                code="RECOVERY_RECONCILIATION_FAILED",
                                stage="recovery",
                                summary="Incompatible state transition during startup recovery",
                                retryable=False,
                            ),
                        ))
                    except (ValueError, KeyError, InvalidWorkTransition, StaleWorkRevisionError):
                        pass

        # Reconcile all remaining active works across crash recovery matrix (Section 23)
        try:
            active_works = self.work_store.list_active(limit=100)
            for work in active_works:
                if work.is_terminal:
                    continue
                if work.transaction_id:
                    tx = self.transaction_store.get(work.transaction_id)
                    if tx is None:
                        if work.status in (WorkStatus.EXECUTING, WorkStatus.VERIFYING):
                            try:
                                self.work_store.save(work.transition(
                                    WorkStatus.FAILED,
                                    failure=WorkFailure(
                                        code="TRANSACTION_NOT_FOUND",
                                        stage="recovery",
                                        summary=f"Linked transaction '{work.transaction_id}' not found",
                                        retryable=False,
                                    ),
                                ))
                            except (ValueError, KeyError, InvalidWorkTransition, StaleWorkRevisionError):
                                pass
                        continue

                    if tx.status.value == "committed" and work.status in (WorkStatus.EXECUTING, WorkStatus.VERIFYING):
                        try:
                            if work.status == WorkStatus.EXECUTING:
                                work = self.work_store.save(work.transition(WorkStatus.VERIFYING))
                            if work.status == WorkStatus.VERIFYING:
                                self.work_store.save(work.transition(WorkStatus.DONE))
                        except (ValueError, KeyError, InvalidWorkTransition, StaleWorkRevisionError):
                            try:
                                current = self.work_store.get(work.id)
                                if current and not current.is_terminal and current.status != WorkStatus.FAILED:
                                    self.work_store.save(current.transition(
                                        WorkStatus.FAILED,
                                        failure=WorkFailure(
                                            code="RECOVERY_RECONCILIATION_FAILED",
                                            stage="recovery",
                                            summary="Failed to reconcile committed work state",
                                            retryable=False,
                                        ),
                                    ))
                            except (ValueError, KeyError, InvalidWorkTransition, StaleWorkRevisionError):
                                pass
                    elif tx.status.value in ("rolled_back", "failed") and work.status in (WorkStatus.EXECUTING, WorkStatus.VERIFYING):
                        try:
                            self.work_store.save(work.transition(
                                WorkStatus.FAILED,
                                failure=WorkFailure(
                                    code="TRANSACTION_INTERRUPTED",
                                    stage="recovery",
                                    summary=f"Linked transaction is {tx.status.value}",
                                    retryable=True,
                                ),
                            ))
                        except (ValueError, KeyError, InvalidWorkTransition, StaleWorkRevisionError):
                            try:
                                current = self.work_store.get(work.id)
                                if current and not current.is_terminal and current.status != WorkStatus.FAILED:
                                    self.work_store.save(current.transition(
                                        WorkStatus.FAILED,
                                        failure=WorkFailure(
                                            code="RECOVERY_RECONCILIATION_FAILED",
                                            stage="recovery",
                                            summary="Failed to reconcile interrupted work state",
                                            retryable=False,
                                        ),
                                    ))
                            except (ValueError, KeyError, InvalidWorkTransition, StaleWorkRevisionError):
                                pass
                else:
                    # Executing or verifying work without transaction binding must fail closed (Property 4)
                    if work.status in (WorkStatus.EXECUTING, WorkStatus.VERIFYING):
                        try:
                            self.work_store.save(work.transition(
                                WorkStatus.FAILED,
                                failure=WorkFailure(
                                    code="MISSING_TX",
                                    stage="recovery",
                                    summary="Executing or verifying work lacks transaction binding",
                                    retryable=False,
                                ),
                            ))
                        except (ValueError, KeyError, InvalidWorkTransition, StaleWorkRevisionError):
                            pass

                if work.status == WorkStatus.APPROVAL_REQUIRED and work.approval_request_id:
                    appr = self.approval_service.store.get(work.approval_request_id)
                    if appr is not None and appr.status in (ApprovalStatus.EXPIRED, ApprovalStatus.REJECTED, ApprovalStatus.CANCELLED):
                        try:
                            new_st = WorkStatus.CANCELLED if appr.status == ApprovalStatus.CANCELLED else WorkStatus.FAILED
                            self.work_store.save(work.transition(
                                new_st,
                                failure=WorkFailure(code="APPROVAL_INVALIDATED", stage="approval", summary=f"Approval request {appr.status.value}"),
                            ))
                        except (ValueError, KeyError, InvalidWorkTransition, StaleWorkRevisionError):
                            pass
        except Exception:
            pass

    def _resolve_test_cmd(self, custom_cmd: Optional[Union[str, List[str]]]) -> List[str]:
        if custom_cmd:
            if isinstance(custom_cmd, list):
                return list(custom_cmd)
            return custom_cmd.split()
        if self.default_test_cmd:
            if isinstance(self.default_test_cmd, list):
                return list(self.default_test_cmd)
            return self.default_test_cmd.split()
        # Default fallback test detection
        from cli import detect_default_test_cmd
        res = detect_default_test_cmd(self.repo_dir)
        if isinstance(res, list):
            return list(res)
        return res.split()

    def handle_message(
        self,
        message: IncomingMessage,
        on_event: Optional[Callable[[AgentEvent], None]] = None,
        log_fn: Optional[Callable[[str], None]] = None,
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
        initial_incarnation = session.session_incarnation_id

        # 2. Deterministic Permission Gate
        policy = self.policy_provider(effective_channel)
        requested_mode = message.metadata.get("mode") or session.active_mode

        raw_text = message.text.strip()

        callback_payload = message.metadata.get("telegram_callback_data")
        if callback_payload is not None:
            if effective_channel != "telegram":
                return OutgoingMessage(
                    text="Invalid approval callback channel.", events=events,
                    success=False, status="rejected", error="callback_channel_mismatch",
                )
            if callback_payload.startswith("bfw:"):
                from .work_presentation import parse_telegram_work_callback
                try:
                    cb_act, cb_wid = parse_telegram_work_callback(callback_payload)
                except ValueError as exc:
                    return OutgoingMessage(text="This work button is invalid.", events=events, success=False, status="rejected", error=str(exc))
                raw_text = f"/{cb_act} {cb_wid}"
            else:
                try:
                    callback_action, callback_request_id = parse_telegram_callback(callback_payload)
                except ValueError as exc:
                    emit("agent.approval.callback_rejected", {"result": "malformed"})
                    return OutgoingMessage(
                        text="This approval button is invalid.", events=events,
                        success=False, status="rejected", error=str(exc),
                    )

                canonical_request = self.approval_service.store.get(callback_request_id)
                canonical_session = (
                    self.sessions.get(canonical_request.session_id)
                    if canonical_request is not None else None
                )
                if (canonical_request is not None
                        and canonical_request.session_incarnation_id is not None
                        and canonical_session is None):
                    emit("agent.approval.callback_rejected", {
                        "request_id": callback_request_id,
                        "result": "canonical_session_missing",
                    })
                    return OutgoingMessage(
                        text="The canonical approval session is unavailable.", events=events,
                        success=False, status="rejected", error="canonical_session_missing",
                    )
                context = ApprovalTransitionContext(
                    actor_id=message.user_id,
                    channel=effective_channel,
                    session_id=session.session_id,
                    session_incarnation_id=session.session_incarnation_id,
                    conversation_id=message.conversation_id,
                    canonical_session_incarnation_id=(
                        canonical_session.session_incarnation_id if canonical_session else None
                    ),
                )
                ok, transition_message, transitioned = twcs(self.sessions, self.approval_service,
                    callback_request_id, callback_action, context
                )
                result = "accepted" if ok else "rejected"
                emit("agent.approval.callback", {
                    "request_id": callback_request_id,
                    "action": callback_action,
                    "result": result,
                    "actor": message.user_id,
                    "channel": effective_channel,
                    "session_id": session.session_id,
                })
                if ok and callback_action == "approve":
                    remote_result = self.remote_work.result(
                        callback_request_id, "accepted", "Work approved and ready for execution."
                    )
                    metadata = {"remote_work": remote_result.to_dict()} if remote_result else {}
                    return OutgoingMessage(
                        text=f"✓ Approval granted. Execution is authorized; use /exec {callback_request_id} to start it.",
                        events=events, success=True, status="completed", metadata=metadata,
                    )
                if ok:
                    self.remote_work.transition_for_approval(callback_request_id, WorkStatus.CANCELLED)
                    label = "Approval rejected." if callback_action == "reject" else "Approval cancelled."
                    return OutgoingMessage(text=label, events=events, success=True, status="completed")

                status = transitioned.status if transitioned else None
                if status == ApprovalStatus.EXPIRED:
                    safe_text = "⏱ This approval has expired."
                elif status in {
                    ApprovalStatus.APPROVED, ApprovalStatus.REJECTED,
                    ApprovalStatus.CANCELLED, ApprovalStatus.CONSUMED,
                }:
                    safe_text = "This approval is no longer actionable."
                elif "Two-man rule" in transition_message:
                    safe_text = "A second, independently authenticated approver is required."
                else:
                    safe_text = scrub_secrets(transition_message)
                return OutgoingMessage(
                    text=safe_text, events=events, success=False,
                    status="rejected", error=safe_text,
                )

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
                    "• `/works` — View recent work requests (e.g. `/works active`, `/works failed`)\n"
                    "• `/work <id>` — Inspect detailed status for a specific work unit\n"
                    "• `/resume <id>` — Deterministically resume incomplete work\n"
                    "• `/cancel <id>` — Cancel pending approval or active work unit\n"
                    "• `/transactions` — View recent execution transactions\n"
                    "• `/transaction <id>` — View details for a specific transaction\n"
                    "• `/recover <id>` — Deterministically recover an interrupted transaction\n"
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
                old_incarnation = session.session_incarnation_id
                self.approval_service.invalidate_session_approvals(
                    session_id=session.session_id,
                    session_incarnation_id=old_incarnation,
                    reason=f"session reset ({cmd})",
                )
                self.sessions.reset(session.session_id)
                emit("agent.session.reset", {"session_id": session.session_id, "command": cmd})
                return OutgoingMessage(text="🔄 Session context has been reset.", events=events, success=True, status="completed")

            if cmd == "/approve":
                if len(parts) < 2 or not parts[1].strip():
                    return OutgoingMessage(text="⚠️ Usage: `/approve <request_id>`", events=events, success=False, status="rejected")
                req_id = parts[1].strip()
                canonical_req = self.approval_service.store.get(req_id)
                canonical_session = self.sessions.get(canonical_req.session_id) if canonical_req else None
                ok, resp_msg, req_obj = twcs(self.sessions, self.approval_service,
                    req_id, "approve", ApprovalTransitionContext(
                        actor_id=message.user_id, channel=effective_channel,
                        session_id=session.session_id,
                        session_incarnation_id=session.session_incarnation_id,
                        conversation_id=message.conversation_id,
                        canonical_session_incarnation_id=(
                            canonical_session.session_incarnation_id if canonical_session else None
                        ),
                    ),
                )
                if ok:
                    remote_result = self.remote_work.result(
                        req_id, "accepted", "Work approved and ready for execution."
                    )
                    emit("agent.approved", {"request_id": req_id, "approver": message.user_id})
                    metadata = {"remote_work": remote_result.to_dict()} if remote_result else {}
                    suffix = f" Use `/exec {req_id}` to execute." if remote_result else ""
                    return OutgoingMessage(text=f"✅ {resp_msg}{suffix}", events=events,
                                           success=True, status="completed", metadata=metadata)
                else:
                    emit("agent.approval.failed", {"request_id": req_id, "reason": resp_msg})
                    return OutgoingMessage(text=f"❌ {resp_msg}", events=events, success=False, status="rejected", error=resp_msg)

            if cmd == "/reject":
                if len(parts) < 2 or not parts[1].strip():
                    return OutgoingMessage(text="⚠️ Usage: `/reject <request_id>`", events=events, success=False, status="rejected")
                req_id = parts[1].strip()
                canonical_req = self.approval_service.store.get(req_id)
                canonical_session = self.sessions.get(canonical_req.session_id) if canonical_req else None
                ok, resp_msg, req_obj = twcs(self.sessions, self.approval_service,
                    req_id, "reject", ApprovalTransitionContext(
                        actor_id=message.user_id, channel=effective_channel,
                        session_id=session.session_id,
                        session_incarnation_id=session.session_incarnation_id,
                        conversation_id=message.conversation_id,
                        canonical_session_incarnation_id=(
                            canonical_session.session_incarnation_id if canonical_session else None
                        ),
                    ),
                )
                if ok:
                    self.remote_work.transition_for_approval(req_id, WorkStatus.CANCELLED)
                    emit("agent.rejected", {"request_id": req_id, "approver": message.user_id})
                    return OutgoingMessage(text=f"🚫 {resp_msg}", events=events, success=True, status="completed")
                else:
                    return OutgoingMessage(text=f"❌ {resp_msg}", events=events, success=False, status="rejected", error=resp_msg)

            if cmd == "/works":
                subfilter = parts[1].strip() if len(parts) > 1 else None
                if effective_channel in ("cli", "local", "terminal"):
                    all_works = self.work_store.list_all(limit=MAX_WORKS_PER_RESPONSE)
                    if subfilter:
                        sf = subfilter.lower().strip()
                        if sf == "active":
                            actor_works = [w for w in all_works if not w.is_terminal]
                        elif sf == "failed":
                            actor_works = [w for w in all_works if w.status == WorkStatus.FAILED]
                        elif sf == "done":
                            actor_works = [w for w in all_works if w.status == WorkStatus.DONE]
                        else:
                            actor_works = [w for w in all_works if w.status.value == sf]
                    else:
                        actor_works = all_works
                else:
                    actor_works = self.work_store.list_for_actor(
                        actor_id=message.user_id,
                        status_filter=subfilter,
                        limit=MAX_WORKS_PER_RESPONSE,
                    )
                resp_text = format_works_list(
                    actor_works,
                    channel=effective_channel,
                    filter_name=subfilter,
                )
                emit("agent.work.listed", {"actor": message.user_id, "count": len(actor_works)})
                return OutgoingMessage(text=resp_text, events=events, success=True, status="completed")

            if cmd == "/work":
                if len(parts) < 2 or not parts[1].strip():
                    return OutgoingMessage(text="⚠️ Usage: `/work <work_id>`", events=events, success=False, status="rejected")
                target_work_id = parts[1].strip()
                work = self.work_store.get(target_work_id)
                if not work:
                    return OutgoingMessage(
                        text=f"❌ Work '{target_work_id}' not found or access denied.",
                        events=events, success=False, status="rejected", error="Not found or access denied",
                    )
                auth_ok, auth_reason = authorize_work_inspection(work, message.user_id, effective_channel)
                if not auth_ok:
                    return OutgoingMessage(
                        text=f"❌ Work '{target_work_id}' not found or access denied.",
                        events=events, success=False, status="rejected", error="Not found or access denied",
                    )
                appr_status = None
                if work.approval_request_id:
                    appr_obj = self.approval_service.store.get(work.approval_request_id)
                    if appr_obj:
                        appr_status = appr_obj.status.value
                tx_status = None
                if work.transaction_id:
                    tx_obj = self.transaction_store.get(work.transaction_id)
                    if tx_obj:
                        tx_status = tx_obj.status.value
                detail_text = format_work_detail(
                    work, channel=effective_channel,
                    approval_status=appr_status, transaction_status=tx_status,
                )
                emit("agent.work.inspected", {"work_id": target_work_id, "actor": message.user_id})
                return OutgoingMessage(text=detail_text, events=events, success=True, status="completed")

            if cmd == "/resume":
                if len(parts) < 2 or not parts[1].strip():
                    return OutgoingMessage(text="⚠️ Usage: `/resume <work_id>`", events=events, success=False, status="rejected")
                target_work_id = parts[1].strip()
                recovery_mgr = TransactionRecoveryManager(workspace=self.repo_dir, store=self.transaction_store)
                ok, resp_msg, work_obj = resume_work(
                    work_id=target_work_id,
                    actor_id=message.user_id,
                    channel=effective_channel,
                    session_id=session.session_id,
                    session_incarnation_id=session.session_incarnation_id,
                    work_store=self.work_store,
                    approval_service=self.approval_service,
                    transaction_store=self.transaction_store,
                    recovery_manager=recovery_mgr,
                )
                if ok:
                    emit("agent.work.resumed", {"work_id": target_work_id, "actor": message.user_id})
                    return OutgoingMessage(text=f"✅ {resp_msg}", events=events, success=True, status="completed")
                else:
                    emit("agent.work.resume_failed", {"work_id": target_work_id, "reason": resp_msg})
                    return OutgoingMessage(text=f"❌ {resp_msg}", events=events, success=False, status="rejected", error=resp_msg)

            if cmd == "/cancel":
                if len(parts) < 2 or not parts[1].strip():
                    return OutgoingMessage(text="⚠️ Usage: `/cancel <id>`", events=events, success=False, status="rejected")
                cancel_id = parts[1].strip()
                # Check if cancel_id is a Work ID
                if cancel_id.startswith("work_") or self.work_store.get(cancel_id) is not None:
                    ok, resp_msg, work_obj = cancel_work(
                        work_id=cancel_id,
                        actor_id=message.user_id,
                        channel=effective_channel,
                        work_store=self.work_store,
                        approval_service=self.approval_service,
                        transaction_store=self.transaction_store,
                    )
                    if ok:
                        emit("agent.work.cancelled", {"work_id": cancel_id, "requester": message.user_id})
                        return OutgoingMessage(text=f"🛑 {resp_msg}", events=events, success=True, status="completed")
                    else:
                        return OutgoingMessage(text=f"❌ {resp_msg}", events=events, success=False, status="rejected", error=resp_msg)

                req_id = cancel_id
                canonical_req = self.approval_service.store.get(req_id)
                canonical_session = self.sessions.get(canonical_req.session_id) if canonical_req else None
                ok, resp_msg, req_obj = twcs(self.sessions, self.approval_service,
                    req_id, "cancel", ApprovalTransitionContext(
                        actor_id=message.user_id, channel=effective_channel,
                        session_id=session.session_id,
                        session_incarnation_id=session.session_incarnation_id,
                        conversation_id=message.conversation_id,
                        canonical_session_incarnation_id=(
                            canonical_session.session_incarnation_id if canonical_session else None
                        ),
                    ),
                )
                if ok:
                    self.remote_work.transition_for_approval(req_id, WorkStatus.CANCELLED)
                    ctx = self.remote_work.context_for_approval(req_id)
                    if ctx:
                        w = self.work_store.get(ctx.work_id)
                        if w and not w.is_terminal:
                            try:
                                self.work_store.save(w.cancel("Approval cancelled"))
                            except Exception:
                                pass
                    emit("agent.cancelled", {"request_id": req_id, "requester": message.user_id})
                    return OutgoingMessage(text=f"🛑 {resp_msg}", events=events, success=True, status="completed")
                else:
                    return OutgoingMessage(text=f"❌ {resp_msg}", events=events, success=False, status="rejected", error=resp_msg)

            if cmd == "/approvals":
                pending = self.approval_service.store.list_requests(
                    session_id=session.session_id,
                    active_only=True,
                    limit=20,
                )
                pending = [
                    r for r in pending
                    if (r.session_incarnation_id is None or r.session_incarnation_id == session.session_incarnation_id)
                    and r.status == ApprovalStatus.PENDING
                    and not r.is_expired()
                ][:20]
                if not pending:
                    return OutgoingMessage(text="ℹ️ No pending approvals for this session.", events=events, success=True, status="completed")
                lines = ["📋 **Pending Approvals:**\n"]
                for r in pending:
                    lines.append(f"• `{r.request_id}` — {r.operation_type} (`{r.canonical_operation.target}`) [Risk: {r.risk_class}]")
                return OutgoingMessage(text="\n".join(lines), events=events, success=True, status="completed")

            if cmd == "/transactions":
                txs = self.transaction_store.list(session_id=session.session_id, limit=20)
                if not txs:
                    return OutgoingMessage(text="ℹ️ No transactions recorded for this session.", events=events, success=True, status="completed")
                lines = ["📋 **Recorded Transactions:**\n"]
                for tx in txs:
                    lines.append(f"• `{tx.id}` — `{tx.status.value}` ({len(tx.operations)} ops, created: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(tx.created_at))})")
                return OutgoingMessage(text="\n".join(lines), events=events, success=True, status="completed")

            if cmd == "/transaction":
                if len(parts) < 2 or not parts[1].strip():
                    return OutgoingMessage(text="⚠️ Usage: `/transaction <transaction_id>`", events=events, success=False, status="rejected")
                tx_id = parts[1].strip()
                tx = self.transaction_store.get(tx_id)
                if not tx:
                    return OutgoingMessage(text=f"❌ Transaction '{tx_id}' not found.", events=events, success=False, status="rejected")
                lines = [
                    f"📋 **Transaction Details (`{tx.id}`):**\n",
                    f"• Status: `{tx.status.value}`",
                    f"• Work ID: `{tx.work_id or 'none'}`",
                    f"• Plan ID: `{tx.plan_id or 'none'}`",
                    f"• Session ID: `{tx.session_id or 'none'}`",
                    f"• Operations: {len(tx.operations)}",
                ]
                for op in tx.operations:
                    op_type = getattr(op.operation_type, "value", str(op.operation_type))
                    op_stat = getattr(op.status, "value", str(op.status))
                    lines.append(f"  - `{op_type}`: `{op.path}` [{op_stat}]")
                if tx.error:
                    lines.append(f"• Error: `{tx.error}`")
                if tx.rollback_error:
                    lines.append(f"• Rollback Error: `{tx.rollback_error}`")
                return OutgoingMessage(text="\n".join(lines), events=events, success=True, status="completed")

            if cmd == "/recover":
                if len(parts) < 2 or not parts[1].strip():
                    return OutgoingMessage(text="⚠️ Usage: `/recover <transaction_id>`", events=events, success=False, status="rejected")
                tx_id = parts[1].strip()
                mgr = TransactionRecoveryManager(workspace=self.repo_dir, store=self.transaction_store)
                res = mgr.recover(tx_id)
                if res.recovered:
                    action_desc = "rolled back" if res.rolled_back else res.final_status.value
                    msg = f"✅ Transaction `{tx_id}` recovered ({action_desc})."
                    emit("agent.transaction.recovered", {"transaction_id": tx_id, "status": res.final_status.value})
                    return OutgoingMessage(text=msg, events=events, success=True, status="completed")
                else:
                    err_msg = res.error or "Recovery failed"
                    emit("agent.transaction.recovery_failed", {"transaction_id": tx_id, "error": err_msg})
                    return OutgoingMessage(text=f"❌ Recovery failed for transaction `{tx_id}`: {err_msg}", events=events, success=False, status="rejected", error=err_msg)

            if cmd in ("/exec", "/run"):
                tokens = raw_text.strip().split()
                if len(tokens) < 2 or not tokens[1].strip():
                    return OutgoingMessage(text="⚠️ Usage: `/exec <request_id>`", events=events, success=False, status="rejected")
                if len(tokens) > 2:
                    reason = "Additional arguments in /exec command are not permitted."
                    emit("agent.rejected", {"command": cmd, "reason": reason})
                    return OutgoingMessage(
                        text=f"⚠️ Usage: `/exec <request_id>` ({reason})",
                        events=events,
                        success=False,
                        status="rejected",
                        error=reason,
                    )
                exec_req_id = tokens[1].strip()
                target_req = self.approval_service.store.get(exec_req_id)
                if not target_req:
                    return OutgoingMessage(text=f"❌ Request '{exec_req_id}' not found.", events=events, success=False, status="rejected")
                raw_text = f"{target_req.canonical_operation.action_type} {target_req.canonical_operation.target}"

                # Sanitize metadata: discard untrusted incoming payload, use only approved canonical parameters
                trusted_meta = {}
                if "repo_dir" in message.metadata:
                    trusted_meta["repo_dir"] = message.metadata["repo_dir"]
                trusted_meta["approval_id"] = exec_req_id
                trusted_meta["expected_digest"] = target_req.operation_digest
                trusted_meta["action_type"] = target_req.canonical_operation.action_type
                trusted_meta["target"] = target_req.canonical_operation.target
                for k, v in target_req.canonical_operation.parameters.items():
                    trusted_meta[k] = v
                message.metadata.clear()
                message.metadata.update(trusted_meta)

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
        remote_coding_request = (
            policy.trust_level == ChannelTrustLevel.REMOTE_CHANNEL.value
            and action in (PermissionAction.WRITE_CODE, PermissionAction.WRITE_FILES)
        )
        remote_contract_required = remote_coding_request and allowed
        approval_enforced = bool(
            remote_contract_required
            or ((self.require_approval or message.metadata.get("require_approval"))
                and action in (PermissionAction.WRITE_CODE, PermissionAction.WRITE_FILES))
        )
        if approval_enforced and allowed:
            # Mutations requiring approval cross the approval/contract boundary.
            allowed = False
            reason = "Operation requires an approved execution contract."

        approval_id = message.metadata.get("approval_id") or message.metadata.get("request_id")
        approved_execution = False
        execution_contract: Optional[ApprovedExecutionContract] = None
        remote_context = None

        if approval_id:
            digest = message.metadata.get("expected_digest")
            if not digest:
                canonical_op = extract_canonical_operation(raw_text, action, message.metadata, repo_dir=Path(self.repo_dir))
                digest = canonical_op.compute_digest()
            is_valid, app_req, consume_reason = self.approval_service.verify_and_consume(
                request_id=approval_id,
                expected_digest=digest,
                session_id=session.session_id,
                channel=effective_channel,
                session_incarnation_id=session.session_incarnation_id,
                requester_id=message.user_id,
            )
            if not is_valid:
                emit("agent.approval.invalid", {"request_id": approval_id, "reason": consume_reason})
                return OutgoingMessage(
                    text=f"⚠️ Approval Verification Failed: {consume_reason}",
                    events=events,
                    success=False,
                    status="rejected",
                    error=consume_reason,
                )
            approved_execution = True
            if app_req is not None:
                try:
                    execution_contract = ApprovedExecutionContract.from_approval_request(
                        app_req, repo_dir=Path(self.repo_dir).resolve(),
                    )
                    execution_contract.validate(
                        actor=message.user_id, channel=effective_channel,
                        session_id=session.session_id,
                        session_incarnation_id=session.session_incarnation_id,
                        repo_dir=Path(self.repo_dir).resolve(),
                    )
                    execution_contract.capabilities.require("network.access")
                except (ValueError, PermissionError) as exc:
                    return OutgoingMessage(text=str(exc), events=events, success=False,
                                           status="rejected", error=str(exc))
                # Incoming metadata cannot replace the approved task or workspace.
                raw_text = f"{app_req.canonical_operation.action_type} {app_req.canonical_operation.target}"
                message.metadata.clear()
                action = classify_request_action(raw_text, {})
                remote_context = self.remote_work.context_for_approval(str(approval_id))
                if remote_context is not None:
                    self.remote_work.transition_for_approval(
                        str(approval_id), WorkStatus.EXECUTING
                    )
            emit("agent.approval.consumed", {
                "request_id": approval_id,
                "approver": app_req.approver_id if app_req else None,
            })

        if not allowed and not approved_execution:
            should_request_approval = bool(
                remote_contract_required
                or self.require_approval
                or message.metadata.get("require_approval")
            )
            if should_request_approval:
                canonical_op = extract_canonical_operation(raw_text, action, message.metadata, repo_dir=Path(self.repo_dir))

                # M-02 Remediation: Fail-closed on invalid or ambiguous filesystem targets
                is_fs_action = action in (
                    PermissionAction.WRITE_CODE,
                    PermissionAction.WRITE_FILES,
                    PermissionAction.READ_CODE,
                ) or canonical_op.action_type in ("write_code", "write_files", "write_file", "delete_file", "read_code")

                if is_fs_action:
                    if canonical_op.parameters.get("invalid_path"):
                        fail_reason = "Invalid target path: Path must be a safe, workspace-relative path."
                        emit("agent.rejected", {"action": action.value if hasattr(action, "value") else str(action), "reason": fail_reason})
                        return OutgoingMessage(
                            text=f"❌ {fail_reason}",
                            events=events,
                            success=False,
                            status="rejected",
                            error=fail_reason,
                        )
                    if not canonical_op.target or canonical_op.parameters.get("ambiguous"):
                        fail_reason = "Target is ambiguous or non-filesystem token. Please specify an exact workspace-relative path."
                        emit("agent.rejected", {"action": action.value if hasattr(action, "value") else str(action), "reason": fail_reason})
                        return OutgoingMessage(
                            text=f"⚠️ {fail_reason}",
                            events=events,
                            success=False,
                            status="rejected",
                            error=fail_reason,
                        )
                risk = (
                    RiskClass.CRITICAL.value
                    if action in (PermissionAction.SHELL_EXECUTION, PermissionAction.GIT_DESTRUCTIVE, PermissionAction.DEPLOYMENT)
                    else (RiskClass.HIGH.value if action == PermissionAction.CREDENTIAL_ACCESS else RiskClass.MEDIUM.value)
                )
                capabilities = self._approval_capabilities(canonical_op)
                pending_remote_work = None
                pending_remote_plan = None
                normalized_request: Optional[RemoteWorkRequest] = None
                if (
                    policy.trust_level == ChannelTrustLevel.REMOTE_CHANNEL.value
                    and effective_channel in ("telegram", "whatsapp")
                    and canonical_op.action_type in ("write_file", "write_files", "write_code")
                ):
                    targets = tuple(sorted(operation_targets(canonical_op.to_canonical_dict())))
                    try:
                        norm = self.remote_work.normalize(
                            message_id=message.id, actor=message.user_id,
                            channel=effective_channel, session_id=session.session_id,
                            session_incarnation_id=session.session_incarnation_id,
                            intent=raw_text, workspace=self.repo_dir, targets=targets,
                        )
                        normalized_request = norm
                        pending_remote_work, pending_remote_plan = self.remote_work.submit(
                            norm, capabilities
                        )
                    except (ValueError, PermissionError) as exc:
                        return OutgoingMessage(
                            text=f"Remote work rejected: {scrub_secrets(str(exc))}",
                            events=events, success=False, status="rejected",
                            error="remote_work_invalid",
                        )
                try:
                    app_req = self.approval_service.create_request(
                        session_id=session.session_id,
                        channel=effective_channel,
                        user_id=message.user_id,
                        conversation_id=message.conversation_id,
                        operation_type=action.value if hasattr(action, "value") else str(action),
                        canonical_operation=canonical_op,
                        risk_class=risk,
                        session_incarnation_id=session.session_incarnation_id,
                        capabilities=capabilities,
                        workspace_root=str(Path(self.repo_dir).resolve()),
                    )
                except ApprovalQuotaExceededError as qe:
                    if pending_remote_work is not None:
                        self.remote_work.work_store.save(
                            pending_remote_work.transition(WorkStatus.FAILED)
                        )
                    emit("agent.rejected", {"action": action.value if hasattr(action, "value") else str(action), "reason": str(qe)})
                    return OutgoingMessage(
                        text=f"⚠️ Approval quota exceeded: {qe}",
                        events=events,
                        success=False,
                        status="rejected",
                        error=str(qe),
                    )
                except ApprovalPayloadTooLargeError as pe:
                    if pending_remote_work is not None:
                        self.remote_work.work_store.save(
                            pending_remote_work.transition(WorkStatus.FAILED)
                        )
                    emit("agent.rejected", {"action": action.value if hasattr(action, "value") else str(action), "reason": str(pe)})
                    return OutgoingMessage(
                        text=f"❌ Approval payload too large: {pe}",
                        events=events,
                        success=False,
                        status="rejected",
                        error=str(pe),
                    )
                except (ValueError, PermissionError):
                    if pending_remote_work is not None:
                        self.remote_work.work_store.save(
                            pending_remote_work.transition(WorkStatus.FAILED)
                        )
                    return OutgoingMessage(
                        text="Approval policy rejected invalid targets, parameters, or credential material.",
                        events=events, success=False, status="rejected",
                    )
                emit("agent.approval.requested", {
                    "request_id": app_req.request_id,
                    "operation": canonical_op.to_canonical_dict(),
                })
                prompt_text = self.approval_service.format_approval_prompt(app_req)
                approval_keyboard = None
                if effective_channel == "telegram":
                    presentation = ApprovalPresentation.from_request(
                        app_req, two_man_rule=self.approval_service.two_man_rule_enabled
                    )
                    prompt_text = presentation.render_telegram()
                    approval_keyboard = telegram_approval_keyboard(app_req.request_id)
                remote_result = None
                if pending_remote_work is not None and pending_remote_plan is not None and normalized_request is not None:
                    self.remote_work.bind_approval(pending_remote_work.id, app_req.request_id)
                    req_id = normalized_request.request_id if normalized_request is not None else app_req.request_id
                    remote_result = RemoteWorkResult(
                        request_id=req_id,
                        work_id=pending_remote_work.id,
                        plan_id=pending_remote_plan.id,
                        status="approval_required",
                        message=f"Work {pending_remote_work.id} is ready for approval.",
                        approval_required=True,
                        approval_request_id=app_req.request_id,
                    )
                    prompt_text = f"{remote_result.message}\n\n{prompt_text}"
                return OutgoingMessage(
                    text=prompt_text,
                    events=events,
                    success=False,
                    status="pending_approval",
                    metadata={
                        "request_id": app_req.request_id,
                        "operation_digest": app_req.operation_digest,
                        **({"telegram_reply_markup": approval_keyboard} if approval_keyboard else {}),
                        **({"remote_work": remote_result.to_dict()} if remote_result else {}),
                    },
                )

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
        if approved_execution:
            effective_mode = "build"
        elif policy.trust_level == ChannelTrustLevel.REMOTE_CHANNEL.value:
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

        session.active_mode = effective_mode
        if provider:
            session.active_provider = provider
        if model:
            session.active_model = model
        if skill:
            session.active_skill = skill

        test_cmd_list = [] if execution_contract else self._resolve_test_cmd(message.metadata.get("test_cmd"))

        # Plan context resolution
        if approved_execution:
            # Under approved execution, plan_context is only trusted if explicitly authorized in the approval request
            plan_context = message.metadata.get("plan_context")
        else:
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
        custom_mod_map = message.metadata.get("module_map")
        map_path = Path(custom_mod_map) if custom_mod_map else None
        domains = {} if execution_contract else load_module_map(
            workspace_dir, map_path=map_path, task=raw_text, auto_create=can_auto_create
        )

        effective_task = raw_text
        if remote_context is not None:
            effective_task = remote_context.request.intent
        if not approved_execution and session.history:
            history_lines = []
            for turn in session.history[-3:]:
                u_text = turn.get("user", "").strip()
                a_text = turn.get("assistant", "").strip()
                if len(a_text) > 300:
                    a_text = a_text[:300] + "..."
                history_lines.append(f"User: {u_text}\nAssistant: {a_text}")
            if history_lines:
                effective_task = f"{raw_text}\n\n[Previous Conversation Context]\n" + "\n---\n".join(history_lines)

        is_remote_channel = (
            policy.trust_level == ChannelTrustLevel.REMOTE_CHANNEL.value
            or effective_channel.lower().strip() not in ("cli", "local", "terminal")
        )
        allow_remote_git_push = False if is_remote_channel else (
            execution_contract.allows_remote_git_push() if execution_contract else True
        )

        pr_risk_ceiling = str(message.metadata.get("pr_risk_ceiling", "medium"))
        min_domain_confidence = float(message.metadata.get("min_domain_confidence", 0.45))

        cfg_cls = globals().get("RunConfig")
        if cfg_cls is None:
            import orchestrator as _orch_mod
            cfg_cls = _orch_mod.RunConfig
            globals()["RunConfig"] = cfg_cls
        cfg = cfg_cls(
            repo_dir=workspace_dir,
            task=effective_task,
            test_command=test_cmd_list,
            max_retries=max_retries,
            auto_pr=auto_pr,
            pr_risk_ceiling=pr_risk_ceiling,
            domains=domains,
            min_domain_confidence=min_domain_confidence,
            skill=skill,
            mode=effective_mode,
            plan_context=plan_context,
            attached_images=list(message.attachments),
            execution_contract=execution_contract,
            origin_channel=effective_channel,
            allow_remote_git_push=allow_remote_git_push,
            actor=message.user_id,
            session_id=session.session_id,
            session_incarnation_id=session.session_incarnation_id,
            work_id=remote_context.work_id if remote_context else message.metadata.get("work_id"),
            plan_id=remote_context.plan_id if remote_context else message.metadata.get("plan_id"),
            task_id=message.metadata.get("task_id") or message.id,
            transaction_store=self.transaction_store,
            transaction_verifier=self.transaction_verifier,
            allowed_scope=message.metadata.get("allowed_scope"),
        )

        active_log_fn = log_fn or message.metadata.get("log_fn")
        cancel_ev = message.metadata.get("cancel_event")
        def log_runtime(msg: str) -> None:
            clean = msg.strip()
            if not clean:
                return
            if cancel_ev and cancel_ev.is_set():
                raise KeyboardInterrupt("Execution cancelled by cancellation token")
            if clean.startswith("=== Step") and clean.endswith("==="):
                emit("agent.step.started", {"step": clean.strip("= ")})
            elif "scope_gate results" in clean or "scope gate" in clean:
                emit("agent.system1.completed", {"log": clean})
            elif "planning task" in clean or "writing code" in clean or "PRD" in clean:
                emit("agent.system2.started", {"log": clean})
            emit("agent.log", {"message": msg})
            if active_log_fn:
                try:
                    active_log_fn(msg)
                except Exception:
                    pass

        # 6. Execute via existing Orchestrator under WorkspaceLock
        ws_lock = WorkspaceLock(
            workspace=workspace_dir,
            session_id=session.session_id,
            actor=message.user_id,
            timeout=float(message.metadata.get("lock_timeout", 10.0)),
        )
        try:
            ws_lock.acquire()
        except WorkspaceLockedError as lock_err:
            emit("agent.error", {"error": str(lock_err)})
            return OutgoingMessage(
                text=f"❌ Workspace Locked: {lock_err}",
                events=events,
                success=False,
                status="locked",
                error=str(lock_err),
                metadata={
                    "session_id": session.session_id,
                    "mode": effective_mode,
                    "locked": True,
                },
            )

        try:
            # Check proof store and cancellation under workspace lock before orchestrator runs
            task_id = message.metadata.get("task_id")
            if task_id:
                from .proof import FileProofStore
                proof_store = FileProofStore(workspace_dir)
                if proof_store.exists(task_id):
                    return OutgoingMessage(
                        text=f"Task '{task_id}' has already been executed with an authoritative proof.",
                        events=events,
                        success=False,
                        status="rejected",
                        error="Duplicate task ID already proven",
                        metadata={"session_id": session.session_id, "mode": effective_mode},
                    )
            if cancel_ev and cancel_ev.is_set():
                return OutgoingMessage(
                    text="⚠️ Task cancelled by user.",
                    events=events,
                    success=False,
                    status="cancelled",
                    error="Execution cancelled by cancellation token",
                    metadata={"session_id": session.session_id, "mode": effective_mode, "cancelled": True},
                )

            try:
                orch_cls = globals().get("Orchestrator")
                if orch_cls is None:
                    import orchestrator as _orch_mod
                    orch_cls = _orch_mod.Orchestrator
                    globals()["Orchestrator"] = orch_cls
                orchestrator = orch_cls(
                    system1=s1, system2=s2, config=cfg, log_fn=log_runtime,
                )
                results: List[StepResult] = orchestrator.run()
            except KeyboardInterrupt:
                emit("agent.cancelled", {"reason": "KeyboardInterrupt"})
                if remote_context is not None:
                    try:
                        self.remote_work.transition_for_approval(str(approval_id), WorkStatus.FAILED)
                    except Exception:
                        pass
                active_coord = getattr(cfg, "_active_coordinator", None)
                if active_coord is not None and getattr(active_coord, "transaction", None) is not None:
                    try:
                        active_coord.rollback(reason="Cancelled by KeyboardInterrupt")
                    except Exception:
                        pass
                session.record_interaction(user_text=raw_text, assistant_text="⚠️ Task cancelled by user.")
                try:
                    self.sessions.save(session)
                except Exception:
                    pass
                if message.metadata.get("catch_cancellation"):
                    return OutgoingMessage(
                        text="⚠️ Task cancelled by user.",
                        events=events,
                        success=False,
                        status="cancelled",
                        error="Execution cancelled by KeyboardInterrupt",
                        metadata={
                            "session_id": session.session_id,
                            "mode": effective_mode,
                            "cancelled": True,
                        },
                    )
                raise
            except Exception as e:
                err_msg = f"Orchestration Error: {e}"
                remote_result = None
                if remote_context is not None:
                    self.remote_work.transition_for_approval(str(approval_id), WorkStatus.FAILED)
                    txs = self.transaction_store.list(session_id=session.session_id, limit=20)
                    matching = next((tx for tx in txs if tx.work_id == remote_context.work_id), None)
                    if matching:
                        self.remote_work.bind_transaction(str(approval_id), matching.id)
                    remote_result = self.remote_work.result(
                        str(approval_id), "failed",
                        f"Orchestration Error: Work {remote_context.work_id} failed "
                        f"and was rolled back. {scrub_secrets(str(e))}",
                        error_code="execution_failed",
                    )
                emit("agent.error", {"error": err_msg})
                return OutgoingMessage(
                    text=(remote_result.message if remote_result else f"❌ {err_msg}"),
                    events=events,
                    success=False,
                    status="error",
                    error=err_msg,
                    metadata={
                        "session_id": session.session_id,
                        "mode": effective_mode,
                        **({"remote_work": remote_result.to_dict()} if remote_result else {}),
                    },
                )
        finally:
            ws_lock.release()

        emit("agent.completed", {"steps_count": len(results)})

        remote_result = None
        if remote_context is not None:
            tx_res = cfg.last_transaction_result
            if tx_res is None or not tx_res.success:
                self.remote_work.transition_for_approval(str(approval_id), WorkStatus.FAILED)
                remote_result = self.remote_work.result(
                    str(approval_id), "failed",
                    f"Work {remote_context.work_id} failed and was rolled back.",
                    error_code="verification_failed",
                )
            else:
                self.remote_work.bind_transaction(str(approval_id), tx_res.transaction_id)
                self.remote_work.transition_for_approval(str(approval_id), WorkStatus.VERIFYING)
                self.remote_work.transition_for_approval(str(approval_id), WorkStatus.DONE)
                remote_result = self.remote_work.result(
                    str(approval_id), "completed",
                    f"Work {remote_context.work_id} completed successfully.",
                )

        # 7. Format Outgoing Response Text
        reply_texts: List[str] = []
        is_success = True

        for r in results:
            detail_str = r.detail or ""
            desc_str = getattr(r.step, "description", "") if getattr(r, "step", None) else ""
            if r.outcome == "diagnosed" and detail_str:
                reply_texts.append(detail_str)
            elif r.outcome == "planned" and detail_str:
                reply_texts.append(detail_str)
                session.plan_context = detail_str
            elif r.outcome == "needs_clarification" and detail_str:
                reply_texts.append(f"❓ {detail_str}")
            elif r.outcome in ("opened_pr", "drafted_pr"):
                pr_info = f"✓ {desc_str}\n{detail_str}"
                reply_texts.append(pr_info)
            elif r.outcome in ("escalated", "abandoned"):
                is_success = False
                reply_texts.append(f"⚠️ {desc_str}: {r.outcome} — {detail_str}")
            elif detail_str:
                reply_texts.append(detail_str)

        final_text = scrub_secrets("\n\n".join(reply_texts).strip() or "Task completed.")
        if remote_result is not None:
            final_text = remote_result.message
            is_success = remote_result.status == "completed"

        # Check if session was reset while request was in-flight (F02 Guard)
        if session.session_incarnation_id != initial_incarnation:
            emit("agent.session.stale", {
                "session_id": session.session_id,
                "initial_incarnation": initial_incarnation,
                "current_incarnation": session.session_incarnation_id,
                "reason": "in_flight_reset",
            })
            stale_msg = "⚠️ Session state changed while this request was executing; result was not persisted."
            return OutgoingMessage(
                text=stale_msg,
                events=events,
                success=False,
                status="rejected",
                error=stale_msg,
                metadata={
                    "session_id": session.session_id,
                    "mode": effective_mode,
                    "stale_session": True,
                },
            )

        # Record in isolated session history and persist under optimistic concurrency control
        session.record_interaction(user_text=raw_text, assistant_text=final_text)
        try:
            self.sessions.save(session)
        except StaleSessionStateError as se:
            emit("agent.session.stale", {
                "session_id": session.session_id,
                "error": str(se),
            })
            stale_msg = "⚠️ Session state changed while this request was executing; result was not persisted."
            return OutgoingMessage(
                text=stale_msg,
                events=events,
                success=False,
                status="rejected",
                error=stale_msg,
                metadata={
                    "session_id": session.session_id,
                    "mode": effective_mode,
                    "stale_session": True,
                },
            )

        out_meta: Dict[str, Any] = {
            "session_id": session.session_id,
            "mode": effective_mode,
            "steps_count": len(results),
            "results": results,
            "model": getattr(s2, "model", model) or model,
            "provider": getattr(s2, "provider_name", provider) or provider,
        }
        if cfg.last_transaction_result:
            tx_res = cfg.last_transaction_result
            out_meta["transaction_id"] = tx_res.transaction_id
            out_meta["transaction_status"] = tx_res.status.value
            out_meta["transaction_success"] = tx_res.success
            out_meta["transaction_result"] = tx_res
        if getattr(cfg, "last_test_result", None) is not None:
            out_meta["test_result"] = cfg.last_test_result
        if remote_result is not None:
            out_meta["remote_work"] = remote_result.to_dict()

        return OutgoingMessage(
            text=final_text,
            events=events,
            success=is_success,
            status="completed" if is_success else "error",
            metadata=out_meta,
        )

    # Backward compatibility and usability alias
    process_message = handle_message

    def execute_autonomous_task(self, request: Any) -> Any:
        """Execute an autonomous task end-to-end through the verification-first workflow."""
        from core.runtime.workflow import execute_autonomous_task
        return execute_autonomous_task(self, request)

    @staticmethod
    def _approval_capabilities(operation):
        """Trusted policy proposal, persisted BEFORE human approval; ignores metadata grants."""
        if operation.action_type not in ("write_file", "write_files", "write_code"):
            return Capabilities()
        targets = tuple(sorted(operation_targets(operation.to_canonical_dict())))
        return Capabilities(filesystem=FilesystemPolicy(read=targets, write=targets),
                            network=NetworkPolicy(access=True))
