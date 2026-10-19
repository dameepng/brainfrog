"""BrainFrog unified agent runtime boundary."""
#Session-authority-checks-use-canonical-server-state-only.

from __future__ import annotations

from enum import Enum
import hashlib
import logging
import os
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union, TYPE_CHECKING

logger = logging.getLogger(__name__)

from core.config import get_system1
from core.modules import load_module_map
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
    TransactionStatus,
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
    VerificationResult,
    VerificationStatus,
    Work,
    WorkFailure,
    WorkStatus,
    WorkStore,
)
from .proof import (
    ChangeRecord,
    ChangesEvidence,
    FileProofStore,
    ProcessEvidence,
    ProofArtifact,
    ReproducibilityEvidence,
    TaskEvidence,
    VerificationEvidence,
    build_change_records,
    capture_bounded_output,
    capture_git_provenance,
    compute_proof_verdict,
    normalize_workspace_relative_path,
    redact_secrets,
)
from .proof_markdown import render_proof_markdown
from .proof_verifier import DeterministicProofVerifier, ProofVerificationStatus
from .workflow import VerificationGate
from .work_authorization import (
    authorize_work_execution_continuation,
    authorize_work_inspection,
)
from .work_continuation import cancel_work, resume_work
from .work_presentation import format_work_detail, format_works_list
from .workspace_lock import WorkspaceLock, WorkspaceLockedError
from .workspace_registry import (
    WorkspaceEntry,
    WorkspaceNotFoundError,
    WorkspaceRegistry,
    WorkspaceValidationError,
    validate_workspace_alias,
)


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
        workspace_registry: Optional[WorkspaceRegistry] = None,
    ) -> None:
        self.repo_dir = (repo_dir or Path.cwd()).resolve()
        if workspace_registry is not None:
            self.workspace_registry = workspace_registry
        else:
            self.workspace_registry = WorkspaceRegistry.create_default(repo_dir=self.repo_dir)
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
        incomplete = self.transaction_store.list_incomplete()
        results: List[RecoveryResult] = []
        for tx in incomplete[:50]:
            if not tx.workspace or not tx.workspace.strip():
                results.append(RecoveryResult(
                    transaction_id=tx.id,
                    previous_status=tx.status,
                    final_status=TransactionStatus.FAILED,
                    recovered=False,
                    rolled_back=False,
                    error=f"Transaction '{tx.id}' has no recorded workspace. Recovery cannot proceed safely.",
                ))
                continue
            recorded_ws = Path(tx.workspace).resolve()
            if not recorded_ws.exists() or not recorded_ws.is_dir():
                results.append(RecoveryResult(
                    transaction_id=tx.id,
                    previous_status=tx.status,
                    final_status=TransactionStatus.FAILED,
                    recovered=False,
                    rolled_back=False,
                    error=f"Recorded workspace directory '{tx.workspace}' for transaction '{tx.id}' does not exist on disk.",
                ))
                continue
            mgr = TransactionRecoveryManager(workspace=recorded_ws, store=self.transaction_store)
            results.append(mgr.recover(tx.id))
        return results

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
                        from core.runtime.proof import FileProofStore
                        from core.runtime.proof_verifier import DeterministicProofVerifier, ProofVerificationStatus

                        # Resolve canonical workspace from trusted metadata
                        meta_ws = (work.resume_metadata or {}).get("workspace")
                        if not meta_ws and tx and tx.workspace:
                            meta_ws = tx.workspace
                        if not meta_ws and work.approval_request_id:
                            appr = self.approval_service.store.get(work.approval_request_id)
                            if appr and appr.workspace_root:
                                meta_ws = appr.workspace_root
                        if not meta_ws or not Path(meta_ws).exists() or not Path(meta_ws).is_dir():
                            logger.warning(
                                f"Skipping proof verification for work '{work.id}': cannot establish canonical workspace."
                            )
                            continue
                        ws_dir = Path(meta_ws).resolve()

                        canonical_task_id = (work.resume_metadata or {}).get("task_id") or work.id
                        proof_store = FileProofStore(ws_dir)
                        if proof_store.exists(canonical_task_id):
                            verifier = DeterministicProofVerifier(ws_dir, verify_disk_integrity=True)
                            rep = verifier.verify(canonical_task_id)
                            proof_obj = None
                            try:
                                proof_obj = proof_store.load(canonical_task_id)
                            except Exception:
                                pass

                            # Strict verification contract:
                            # 1. Structural validity
                            # 2. Authoritative verdict must be VERIFIED
                            # 3. Task identity must match canonical task ID
                            # 4. Transaction binding must match if work has transaction_id
                            # 5. Transaction status must be COMMITTED
                            changes_obj = getattr(proof_obj, "changes", None) if proof_obj is not None else None
                            is_valid = (
                                rep.overall_status == ProofVerificationStatus.VALID
                                and rep.verdict_status == "VERIFIED"
                                and proof_obj is not None
                                and getattr(proof_obj, "verdict", None) is not None
                                and getattr(proof_obj.verdict, "status", None) == "VERIFIED"
                                and changes_obj is not None
                                and getattr(changes_obj, "status", None) == "COMMITTED"
                                and getattr(proof_obj, "task", None) is not None
                                and getattr(proof_obj.task, "task_id", None) == canonical_task_id
                            )
                            if is_valid and (work.transaction_id or (changes_obj and getattr(changes_obj, "transaction_id", None))):
                                if not changes_obj or not getattr(changes_obj, "transaction_id", None) or changes_obj.transaction_id != work.transaction_id:
                                    is_valid = False
                                if tx.id != work.transaction_id or recovery.transaction_id != work.transaction_id:
                                    is_valid = False

                            if is_valid:
                                passed_c = len([c for c in rep.checks if c.status == ProofVerificationStatus.VALID])
                                v_res = VerificationResult(
                                    status=VerificationStatus.PASS,
                                    passed=passed_c,
                                    failed=0,
                                    summary=f"Authoritative proof verified ({canonical_task_id})",
                                )
                                self.work_store.save(work.transition(WorkStatus.DONE, verification_result=v_res))
                            else:
                                fail_summary = (
                                    f"Startup recovery: proof verification failed ({rep.overall_status.value}, verdict: {rep.verdict_status})"
                                    if rep.overall_status != ProofVerificationStatus.VALID or rep.verdict_status != "VERIFIED"
                                    else "Startup recovery: proof identity or transaction binding mismatch"
                                )
                                self.work_store.save(work.transition(
                                    WorkStatus.FAILED,
                                    failure=WorkFailure(
                                        code="UNVERIFIED_RECOVERY" if "identity" not in fail_summary else "EVIDENCE_MISMATCH",
                                        stage="recovery",
                                        summary=fail_summary,
                                        retryable=False,
                                    ),
                                ))
                        else:
                            self.work_store.save(work.transition(
                                WorkStatus.FAILED,
                                failure=WorkFailure(
                                    code="UNVERIFIED_RECOVERY",
                                    stage="recovery",
                                    summary="Startup recovery: committed transaction lacks authoritative verification proof",
                                    retryable=False,
                                ),
                            ))
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
                                from core.runtime.proof import FileProofStore
                                from core.runtime.proof_verifier import DeterministicProofVerifier, ProofVerificationStatus

                                # Resolve canonical workspace from trusted metadata
                                meta_ws = (work.resume_metadata or {}).get("workspace")
                                if not meta_ws and tx and tx.workspace:
                                    meta_ws = tx.workspace
                                if not meta_ws and work.approval_request_id:
                                    appr = self.approval_service.store.get(work.approval_request_id)
                                    if appr and appr.workspace_root:
                                        meta_ws = appr.workspace_root
                                if not meta_ws or not Path(meta_ws).exists() or not Path(meta_ws).is_dir():
                                    logger.warning(
                                        f"Skipping proof verification for active work '{work.id}': cannot establish canonical workspace."
                                    )
                                    continue
                                ws_dir = Path(meta_ws).resolve()

                                canonical_task_id = (work.resume_metadata or {}).get("task_id") or work.id
                                proof_store = FileProofStore(ws_dir)
                                if proof_store.exists(canonical_task_id):
                                    verifier = DeterministicProofVerifier(ws_dir, verify_disk_integrity=True)
                                    rep = verifier.verify(canonical_task_id)
                                    proof_obj = None
                                    try:
                                        proof_obj = proof_store.load(canonical_task_id)
                                    except Exception:
                                        pass

                                    changes_obj = getattr(proof_obj, "changes", None) if proof_obj is not None else None
                                    is_valid = (
                                        rep.overall_status == ProofVerificationStatus.VALID
                                        and rep.verdict_status == "VERIFIED"
                                        and proof_obj is not None
                                        and getattr(proof_obj, "verdict", None) is not None
                                        and getattr(proof_obj.verdict, "status", None) == "VERIFIED"
                                        and changes_obj is not None
                                        and getattr(changes_obj, "status", None) == "COMMITTED"
                                        and getattr(proof_obj, "task", None) is not None
                                        and getattr(proof_obj.task, "task_id", None) == canonical_task_id
                                    )
                                    if is_valid and (work.transaction_id or (changes_obj and getattr(changes_obj, "transaction_id", None))):
                                        if not changes_obj or not getattr(changes_obj, "transaction_id", None) or changes_obj.transaction_id != work.transaction_id:
                                            is_valid = False
                                        if tx.id != work.transaction_id:
                                            is_valid = False

                                    if is_valid:
                                        passed_c = len([c for c in rep.checks if c.status == ProofVerificationStatus.VALID])
                                        v_res = VerificationResult(
                                            status=VerificationStatus.PASS,
                                            passed=passed_c,
                                            failed=0,
                                            summary=f"Authoritative proof verified ({canonical_task_id})",
                                        )
                                        self.work_store.save(work.transition(WorkStatus.DONE, verification_result=v_res))
                                    else:
                                        fail_summary = (
                                            f"Startup recovery: proof verification failed ({rep.overall_status.value}, verdict: {rep.verdict_status})"
                                            if rep.overall_status != ProofVerificationStatus.VALID or rep.verdict_status != "VERIFIED"
                                            else "Startup recovery: proof identity or transaction binding mismatch"
                                        )
                                        self.work_store.save(work.transition(
                                            WorkStatus.FAILED,
                                            failure=WorkFailure(
                                                code="UNVERIFIED_RECOVERY" if "identity" not in fail_summary else "EVIDENCE_MISMATCH",
                                                stage="recovery",
                                                summary=fail_summary,
                                                retryable=False,
                                            ),
                                        ))
                                else:
                                    self.work_store.save(work.transition(
                                        WorkStatus.FAILED,
                                        failure=WorkFailure(
                                            code="UNVERIFIED_RECOVERY",
                                            stage="recovery",
                                            summary="Startup recovery: committed transaction lacks authoritative verification proof",
                                            retryable=False,
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

    def _resolve_test_cmd(
        self,
        custom_cmd: Optional[Union[str, List[str]]],
        workspace_dir: Optional[Path] = None,
    ) -> List[str]:
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
        res = detect_default_test_cmd(workspace_dir or self.repo_dir)
        if isinstance(res, list):
            return list(res)
        return res.split()

    def get_session_workspace(
        self,
        session: SessionState,
        message: Optional[IncomingMessage] = None,
    ) -> Path:
        """Resolve effective target workspace for a session deterministically and fail-closed.

        Precedence:
        1. Explicit repo_dir in message metadata from trusted local CLI channel.
        2. Session active_workspace resolved via workspace_registry (fails closed if missing/invalid).
        3. Registry default workspace or fallback to self.repo_dir.
        """
        if message is not None and message.metadata.get("repo_dir"):
            if message.channel in ("cli", "local", "terminal"):
                return Path(message.metadata["repo_dir"]).resolve()

        if session.active_workspace:
            return self.workspace_registry.resolve(session.active_workspace)

        default_entry = self.workspace_registry.get(self.workspace_registry.default_alias)
        if default_entry:
            if not default_entry.path.exists() or not default_entry.path.is_dir():
                raise WorkspaceNotFoundError(f"Default workspace path does not exist on disk: {default_entry.path}")
            return default_entry.path
        if not self.repo_dir.exists() or not self.repo_dir.is_dir():
            raise WorkspaceNotFoundError(f"Installation repository directory does not exist on disk: {self.repo_dir}")
        return self.repo_dir

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
            default_mode=message.metadata.get("mode"),
        )
        initial_incarnation = session.session_incarnation_id

        # 2. Deterministic Permission Gate
        policy = self.policy_provider(effective_channel)
        from core.plans import normalize_mode
        raw_meta_mode = message.metadata.get("mode")
        if raw_meta_mode is not None:
            norm_meta = normalize_mode(raw_meta_mode)
            if norm_meta is not None:
                requested_mode = norm_meta
            else:
                requested_mode = session.active_mode
                emit("agent.mode.invalid_metadata", {
                    "session_id": session.session_id,
                    "channel": effective_channel,
                    "raw_mode": str(raw_meta_mode)[:80],
                    "fallback_mode": session.active_mode,
                })
        else:
            requested_mode = session.active_mode

        meta_plan_id = message.metadata.get("plan_id")
        if meta_plan_id and session.selected_plan_id and str(meta_plan_id).strip() != str(session.selected_plan_id).strip():
            reason = f"Plan ID mismatch: message requested plan '{meta_plan_id}', but session has '{session.selected_plan_id}' selected."
            emit("agent.rejected", {"reason": reason})
            return OutgoingMessage(text=f"⚠️ {reason}", events=events, success=False, status="rejected", error=reason)

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
            raw_cmd = parts[0].lower()
            cmd = raw_cmd.split("@")[0].rstrip(":")

            if cmd in ("/help", "/start"):
                help_text = (
                    "🐸 **BrainFrog Agent Runtime**\n\n"
                    "Available commands:\n"
                    "• `/help` — Show available commands and instructions\n"
                    "• `/status` — View active session and runtime diagnostics\n"
                    "• `/mode` — View active session mode or switch (`/mode <chat|plan|build>`)\n"
                    "• `/chat` — Switch to Chat mode (`/chat` or `/chat <message>`)\n"
                    "• `/plan` — Switch to Plan mode or manage plans (`/plan`, `/plan select <id>`)\n"
                    "• `/build` — Switch to Build mode (`/build` or `/build <task>`)\n"
                    "• `/model` — View active System 2 model or switch (`/model <provider> <model_id>`)\n"
                    "• `/models` — List supported providers and available models\n"
                    "• `/doctor` — Run comprehensive system & configuration check\n"
                    "• `/workspaces` — List registered project workspaces\n"
                    "• `/workspace <alias>` — Select active workspace for current session\n"
                    "• `/plans` — List implementation plans in active workspace\n"
                    "• `/plan` — View, select, or inspect plans (`/plan <id>`, `/plan select <id>`)\n"
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
                from .model_switcher import get_default_model_for_provider
                from system2 import get_system2_provider
                current_ws = session.active_workspace or self.workspace_registry.default_alias
                ws_entry = self.workspace_registry.get(current_ws)
                ws_path = str(ws_entry.path) if ws_entry else str(self.repo_dir)
                eff_prov = session.active_provider or self.default_provider or get_system2_provider()
                eff_model = session.active_model or self.default_model or get_default_model_for_provider(eff_prov)
                prov_scope = "session override" if session.active_provider else "system default"
                model_scope = "session override" if session.active_model else "system default"
                status_text = (
                    f"🐸 **BrainFrog Status**\n"
                    f"• Channel: `{effective_channel}`\n"
                    f"• Session ID: `{session.session_id}`\n"
                    f"• Workspace: `{current_ws}` (`{ws_path}`)\n"
                    f"• Selected Plan: `{session.selected_plan_id or 'none'}`\n"
                    f"• Provider: `{eff_prov}` ({prov_scope})\n"
                    f"• Model: `{eff_model}` ({model_scope})\n"
                    f"• Active Mode: `{session.active_mode}`\n"
                    f"• History Turns: `{len(session.history)}`\n"
                    f"• Trust Level: `{policy.trust_level}`"
                )
                return OutgoingMessage(text=status_text, events=events, success=True, status="completed")

            # Deterministic System 2 model inspection and configuration
            if cmd in ("/models", "/model"):
                if not policy.is_allowed(PermissionAction.CHANGE_MODEL):
                    err = f"Model configuration is not permitted by policy for channel '{policy.channel}'."
                    emit("agent.rejected", {"command": cmd, "reason": err})
                    return OutgoingMessage(
                        text=f"⛔ Permission Denied: {err}",
                        events=events,
                        success=False,
                        status="rejected",
                        error=err,
                    )

                if cmd == "/models":
                    from .model_switcher import format_models_markdown
                    sub_parts = parts[1].split() if len(parts) > 1 else []
                    prov_filter = sub_parts[0].strip(" :,;") if sub_parts else None
                    text = format_models_markdown(provider_filter=prov_filter)
                    return OutgoingMessage(text=text, events=events, success=True, status="completed")

                if cmd == "/model":
                    from .model_switcher import format_model_status_markdown, switch_session_model
                    sub_text = parts[1].strip() if len(parts) > 1 else ""
                    if not sub_text:
                        status_str = format_model_status_markdown(
                            session=session,
                            default_provider=self.default_provider,
                            default_model=self.default_model,
                        )
                        return OutgoingMessage(text=status_str, events=events, success=True, status="completed")

                    ok, reply = switch_session_model(
                        session=session,
                        raw_args=sub_text,
                        policy=policy,
                        default_provider=self.default_provider,
                        default_model=self.default_model,
                        system2_factory=self.system2_factory,
                    )
                    if ok:
                        self.sessions.save(session)
                        emit("agent.model.switched", {
                            "session_id": session.session_id,
                            "provider": session.active_provider,
                            "model": session.active_model,
                            "actor": message.user_id,
                            "channel": effective_channel,
                        })
                        return OutgoingMessage(text=reply, events=events, success=True, status="completed")
                    else:
                        emit("agent.model.switch_rejected", {
                            "session_id": session.session_id,
                            "raw_args": sub_text,
                            "error": reply,
                            "actor": message.user_id,
                            "channel": effective_channel,
                        })
                        return OutgoingMessage(text=reply, events=events, success=False, status="rejected", error=reply)

            # Deterministic Session Mode inspection and configuration (/mode, /modes, /chat, /build)
            if cmd in ("/mode", "/modes", "/chat", "/build"):
                if not policy.is_allowed(PermissionAction.CHANGE_MODE):
                    err = f"Mode configuration is not permitted by policy for channel '{policy.channel}'."
                    emit("agent.rejected", {"command": cmd, "reason": err})
                    return OutgoingMessage(
                        text=f"⛔ Permission Denied: {err}",
                        events=events,
                        success=False,
                        status="rejected",
                        error=err,
                    )

                from core.plans import normalize_mode
                if cmd == "/chat":
                    target_mode = "chat"
                    sub_text = parts[1].strip() if len(parts) > 1 else ""
                elif cmd == "/build":
                    target_mode = "build"
                    sub_text = parts[1].strip() if len(parts) > 1 else ""
                else:
                    sub_parts = parts[1].split() if len(parts) > 1 else []
                    if not sub_parts:
                        status_text = (
                            f"🎯 **Active Mode:** `{session.active_mode}`\n\n"
                            "Supported modes:\n"
                            "• `chat` — Normal conversation & informational assistance (no plans or code mutations)\n"
                            "• `plan` — Implementation planning; creates and persists plans under `.brainfrog/plans/`\n"
                            "• `build` — Controlled implementation of existing plans (Phase 1 preview)\n\n"
                            "Usage: `/mode <chat|plan|build>`"
                        )
                        return OutgoingMessage(text=status_text, events=events, success=True, status="completed")

                    raw_target = sub_parts[0]
                    target_mode = normalize_mode(raw_target)
                    sub_text = ""
                    if not target_mode:
                        err_msg = (
                            f"❌ Unsupported mode '{raw_target}'. Supported modes are: `chat`, `plan`, `build`.\n"
                            "Usage: `/mode <chat|plan|build>`"
                        )
                        emit("agent.mode.switch_rejected", {
                            "session_id": session.session_id,
                            "raw_target": raw_target,
                            "reason": "unsupported_mode",
                            "actor": message.user_id,
                            "channel": effective_channel,
                        })
                        return OutgoingMessage(text=err_msg, events=events, success=False, status="rejected", error=err_msg)

                old_mode = session.active_mode
                session.active_mode = target_mode
                self.sessions.save(session)
                emit("agent.mode.switched", {
                    "session_id": session.session_id,
                    "from": old_mode,
                    "to": target_mode,
                    "actor": message.user_id,
                    "channel": effective_channel,
                })
                if not sub_text:
                    reply = f"✅ Mode switched to `{target_mode}`."
                    return OutgoingMessage(text=reply, events=events, success=True, status="completed")

                raw_text = sub_text
                requested_mode = target_mode

            if cmd in ("/doctor", "/diagnose"):
                from .doctor import run_doctor_diagnostics
                try:
                    target_ws = self.get_session_workspace(session, message)
                except WorkspaceNotFoundError as ws_err:
                    return OutgoingMessage(
                        text=f"❌ Workspace Error: {ws_err}",
                        events=events,
                        success=False,
                        status="rejected",
                        error=str(ws_err),
                    )
                checks = run_doctor_diagnostics(target_ws)
                ws_name = session.active_workspace or self.workspace_registry.default_alias
                lines = [f"🐸 **BrainFrog Doctor Diagnostics (`{ws_name}`):**\n"]
                for c in checks:
                    icon = "✅" if c.status == "OK" else ("⚠️" if c.status == "WARN" else ("❌" if c.status == "FAIL" else "ℹ️"))
                    lines.append(f"{icon} **[{c.category}]** {c.name}: {c.detail}")
                return OutgoingMessage(text="\n".join(lines), events=events, success=True, status="completed")

            if cmd == "/workspaces":
                workspaces = self.workspace_registry.list_workspaces()
                if not workspaces:
                    return OutgoingMessage(
                        text="ℹ️ No workspaces registered in configuration.",
                        events=events, success=True, status="completed",
                    )
                current_alias = session.active_workspace or self.workspace_registry.default_alias
                lines = ["📂 **Registered Workspaces:**\n"]
                for ws in workspaces:
                    active_marker = " *(active)*" if ws.alias == current_alias else ""
                    lines.append(f"• `{ws.alias}` — `{ws.path}`{active_marker}")
                lines.append("\nUse `/workspace <alias>` to select a workspace for this session.")
                return OutgoingMessage(text="\n".join(lines), events=events, success=True, status="completed")

            if cmd == "/workspace":
                sub_parts = parts[1].split() if len(parts) > 1 else []
                subcmd = sub_parts[0].lower() if sub_parts else ""

                if not subcmd or subcmd == "current":
                    current_alias = session.active_workspace or self.workspace_registry.default_alias
                    entry = self.workspace_registry.get(current_alias)
                    path_str = str(entry.path) if entry else str(self.repo_dir)
                    status_text = (
                        f"📂 **Active Workspace:** `{current_alias}`\n"
                        f"• Path: `{path_str}`\n\n"
                        "Use `/workspace <alias>` to switch workspace, `/workspaces` to list available,\n"
                        "or `/workspace add <alias> <path>` to register a new workspace (CLI only)."
                    )
                    return OutgoingMessage(text=status_text, events=events, success=True, status="completed")

                if subcmd == "add":
                    # Gate registration to local CLI administrators
                    if effective_channel not in ("cli", "local", "terminal"):
                        err_msg = "Workspace registration is restricted to local CLI administrators."
                        return OutgoingMessage(
                            text=f"❌ {err_msg}",
                            events=events,
                            success=False,
                            status="rejected",
                            error=err_msg,
                        )
                    raw_args = parts[1].split(maxsplit=2)
                    if len(raw_args) < 3 or not raw_args[1].strip() or not raw_args[2].strip():
                        return OutgoingMessage(
                            text="⚠️ Usage: `/workspace add <alias> <absolute-path>`",
                            events=events,
                            success=False,
                            status="rejected",
                            error="Invalid usage",
                        )
                    raw_alias = raw_args[1].strip()
                    raw_path = raw_args[2].strip()
                    try:
                        target_alias = validate_workspace_alias(raw_alias)
                        p_val = Path(raw_path)
                        if not p_val.is_absolute():
                            return OutgoingMessage(
                                text="❌ Workspace path must be an absolute path.",
                                events=events,
                                success=False,
                                status="rejected",
                                error="Path not absolute",
                            )
                        res_p = p_val.resolve()
                        if not res_p.exists() or not res_p.is_dir():
                            return OutgoingMessage(
                                text=f"❌ Workspace target directory does not exist: {res_p}",
                                events=events,
                                success=False,
                                status="rejected",
                                error="Directory does not exist",
                            )
                        self.workspace_registry._validate_not_control_plane(res_p)
                        self.workspace_registry.register(target_alias, res_p, persist=True)
                    except (WorkspaceValidationError, WorkspaceNotFoundError) as ws_err:
                        return OutgoingMessage(
                            text=f"❌ Registration failed: {ws_err}",
                            events=events,
                            success=False,
                            status="rejected",
                            error=str(ws_err),
                        )
                    emit("agent.workspace.registered", {
                        "alias": target_alias,
                        "path": str(res_p),
                        "actor": message.user_id,
                    })
                    return OutgoingMessage(
                        text=f"✅ Workspace `{target_alias}` registered at `{res_p}`.",
                        events=events,
                        success=True,
                        status="completed",
                    )

                if subcmd == "remove":
                    # Gate removal to local CLI administrators
                    if effective_channel not in ("cli", "local", "terminal"):
                        err_msg = "Workspace removal is restricted to local CLI administrators."
                        return OutgoingMessage(
                            text=f"❌ {err_msg}",
                            events=events,
                            success=False,
                            status="rejected",
                            error=err_msg,
                        )
                    if len(sub_parts) < 2 or not sub_parts[1].strip():
                        return OutgoingMessage(
                            text="⚠️ Usage: `/workspace remove <alias>`",
                            events=events,
                            success=False,
                            status="rejected",
                            error="Invalid usage",
                        )
                    raw_alias = sub_parts[1].strip()
                    try:
                        target_alias = validate_workspace_alias(raw_alias)
                        self.workspace_registry.unregister(target_alias, persist=True)
                    except (WorkspaceValidationError, WorkspaceNotFoundError) as ws_err:
                        return OutgoingMessage(
                            text=f"❌ Removal failed: {ws_err}",
                            events=events,
                            success=False,
                            status="rejected",
                            error=str(ws_err),
                        )
                    if session.active_workspace == target_alias:
                        session.active_workspace = None
                        session.selected_plan_id = None
                        self.sessions.save(session)
                    emit("agent.workspace.removed", {
                        "alias": target_alias,
                        "actor": message.user_id,
                    })
                    return OutgoingMessage(
                        text=f"✅ Workspace registration `{target_alias}` removed. Project files on disk were not deleted.",
                        events=events,
                        success=True,
                        status="completed",
                    )

                raw_alias = subcmd
                try:
                    target_alias = validate_workspace_alias(raw_alias)
                except WorkspaceValidationError as val_err:
                    emit("agent.workspace.rejected", {
                        "session_id": session.session_id,
                        "alias": raw_alias,
                        "reason": str(val_err),
                    })
                    return OutgoingMessage(
                        text=f"❌ {val_err}",
                        events=events,
                        success=False,
                        status="rejected",
                        error=str(val_err),
                    )

                entry = self.workspace_registry.get(target_alias)
                if not entry:
                    err_msg = f"Workspace alias '{target_alias}' is not registered. Use `/workspaces` to view available workspaces."
                    emit("agent.workspace.rejected", {
                        "session_id": session.session_id,
                        "alias": target_alias,
                        "reason": "unregistered_alias",
                    })
                    return OutgoingMessage(
                        text=f"❌ {err_msg}",
                        events=events,
                        success=False,
                        status="rejected",
                        error=err_msg,
                    )

                if not entry.path.exists() or not entry.path.is_dir():
                    err_msg = f"Registered workspace directory for '{target_alias}' does not exist on disk."
                    emit("agent.workspace.rejected", {
                        "session_id": session.session_id,
                        "alias": target_alias,
                        "reason": "directory_missing",
                    })
                    return OutgoingMessage(
                        text=f"❌ {err_msg}",
                        events=events,
                        success=False,
                        status="rejected",
                        error=err_msg,
                    )

                # Invalidate incompatible pending approvals for this session if switching to a different canonical path
                old_alias = session.active_workspace or self.workspace_registry.default_alias
                old_entry = self.workspace_registry.get(old_alias)
                if old_entry and old_entry.path.resolve() != entry.path.resolve():
                    for req in self.approval_service.store.list_requests(session_id=session.session_id, active_only=True):
                        if req.status == ApprovalStatus.PENDING and req.workspace_root:
                            if Path(req.workspace_root).resolve() != entry.path.resolve():
                                lock_ctx = (
                                    self.approval_service.store._get_request_lock(req.request_id)
                                    if hasattr(self.approval_service.store, "_get_request_lock")
                                    else nullcontext()
                                )
                                with lock_ctx:
                                    cur = self.approval_service.store.get(req.request_id)
                                    if cur and cur.status == ApprovalStatus.PENDING:
                                        cur.status = ApprovalStatus.CANCELLED
                                        cur.rejection_reason = f"Cancelled due to workspace switch to '{entry.alias}'"
                                        self.approval_service.store.save(cur)

                session.active_workspace = entry.alias
                session.selected_plan_id = None
                self.sessions.save(session)

                emit("agent.workspace.selected", {
                    "session_id": session.session_id,
                    "workspace": entry.alias,
                    "path": str(entry.path),
                    "actor": message.user_id,
                })
                resp_text = f"✅ Workspace switched to `{entry.alias}` (`{entry.path}`)."
                return OutgoingMessage(text=resp_text, events=events, success=True, status="completed")

            if cmd == "/plans":
                try:
                    target_ws = self.get_session_workspace(session, message)
                except WorkspaceNotFoundError as ws_err:
                    return OutgoingMessage(
                        text=f"❌ Workspace Error: {ws_err}",
                        events=events,
                        success=False,
                        status="rejected",
                        error=str(ws_err),
                    )
                from core.plans import PlanRegistry
                registry = PlanRegistry(target_ws)
                sub_filter = parts[1].strip() if len(parts) > 1 else None
                plans = registry.list_plans(status_filter=sub_filter)
                ws_name = session.active_workspace or self.workspace_registry.default_alias
                if not plans:
                    filter_msg = f" matching '{sub_filter}'" if sub_filter else ""
                    return OutgoingMessage(
                        text=f"ℹ️ No plans found in workspace `{ws_name}`{filter_msg}.\nUse `/mode plan` to draft a plan.",
                        events=events,
                        success=True,
                        status="completed",
                    )
                lines = [f"📋 **Implementation Plans (`{ws_name}`):**\n"]
                for p in plans:
                    sel_marker = " *(selected)*" if session.selected_plan_id == p.id else ""
                    malformed_marker = " ⚠️(malformed metadata)" if p.metadata_malformed else (" ⚠️(missing metadata)" if p.metadata_missing else "")
                    lines.append(f"• `{p.id}` — **{p.title}** [`{p.status.value}`]{sel_marker}{malformed_marker} (steps: {p.steps_count})")
                lines.append("\nUse `/plan select <id>` to select a plan, or `/plan <id>` to view details.")
                emit("agent.plans.listed", {
                    "session_id": session.session_id,
                    "count": len(plans),
                    "workspace": ws_name,
                })
                return OutgoingMessage(text="\n".join(lines), events=events, success=True, status="completed")

            if cmd == "/plan":
                try:
                    target_ws = self.get_session_workspace(session, message)
                except WorkspaceNotFoundError as ws_err:
                    return OutgoingMessage(
                        text=f"❌ Workspace Error: {ws_err}",
                        events=events,
                        success=False,
                        status="rejected",
                        error=str(ws_err),
                    )
                from core.plans import (
                    PlanRegistry,
                    PlanStatus,
                    PlanNotFoundError,
                    InvalidPlanTransitionError,
                    StalePlanStateError,
                    AmbiguousPlanError,
                )
                registry = PlanRegistry(target_ws)
                ws_name = session.active_workspace or self.workspace_registry.default_alias
                raw_args = parts[1].strip() if len(parts) > 1 else ""

                if not raw_args or raw_args.lower() in ("current", "selected"):
                    if not raw_args and session.active_mode != "plan":
                        old_mode = session.active_mode
                        session.active_mode = "plan"
                        self.sessions.save(session)
                        emit("agent.mode.switched", {
                            "session_id": session.session_id,
                            "from": old_mode,
                            "to": "plan",
                            "actor": message.user_id,
                            "channel": effective_channel,
                        })
                    res = registry.select_plan(session_selected_id=session.selected_plan_id)
                    if res.selected:
                        p = res.selected
                        status_str = (
                            f"📌 **Currently Selected Plan (`{ws_name}`):**\n"
                            f"• ID: `{p.id}`\n"
                            f"• Title: **{p.title}**\n"
                            f"• Status: `{p.status.value}`\n"
                            f"• Goal: {p.goal}\n"
                            f"• Steps: {len(p.steps)}\n"
                            f"• Acceptance Criteria: {len(p.acceptance_criteria)}\n"
                            f"• Resumable: {'Yes' if p.is_resumable else 'No'}\n"
                            f"• Executable: {'Yes' if p.is_executable else 'No'} (requires ApprovedExecutionContract)\n"
                            f"• Verifiable: {'Yes' if p.is_verifiable else 'No'}\n\n"
                            "Use `/plan <id>` to view full details, or `/plans` to list all plans."
                        )
                        return OutgoingMessage(text=status_str, events=events, success=True, status="completed")
                    else:
                        return OutgoingMessage(
                            text=f"ℹ️ No plan is currently selected for this session.\nUse `/plans` to list plans or `/plan select <id>` to select one.",
                            events=events,
                            success=True,
                            status="completed",
                        )

                sub_parts = raw_args.split(maxsplit=2)
                sub_action = sub_parts[0].lower()

                if sub_action in ("select", "use", "choose"):
                    target_query = sub_parts[1].strip() if len(sub_parts) > 1 else ""
                    if not target_query:
                        return OutgoingMessage(
                            text="⚠️ Usage: `/plan select <plan_id>`",
                            events=events,
                            success=False,
                            status="rejected",
                            error="Missing plan identifier",
                        )
                    sel_res = registry.select_plan(query=target_query, candidate_intent=False)
                    if sel_res.status == "selected" and sel_res.selected:
                        p = sel_res.selected
                        session.selected_plan_id = p.id
                        self.sessions.save(session)
                        emit("agent.plan.selected", {
                            "session_id": session.session_id,
                            "plan_id": p.id,
                            "workspace": ws_name,
                            "actor": message.user_id,
                        })
                        reply = (
                            f"✅ Plan selected: `{p.id}` (**{p.title}**) [status: `{p.status.value}`].\n"
                            "ℹ️ Plan selection sets session focus but does not authorize execution or modify files."
                        )
                        return OutgoingMessage(text=reply, events=events, success=True, status="completed")
                    elif sel_res.status == "ambiguous":
                        lines = [f"⚠️ {sel_res.message}\n"]
                        for c in sel_res.candidates:
                            lines.append(f"• `{c.id}` — **{c.title}** [`{c.status.value}`]")
                        return OutgoingMessage(text="\n".join(lines), events=events, success=False, status="rejected", error="Ambiguous plan match")
                    else:
                        return OutgoingMessage(text=f"❌ {sel_res.message}", events=events, success=False, status="rejected", error=sel_res.message)

                if sub_action in ("unselect", "clear", "deselect"):
                    old_id = session.selected_plan_id
                    session.selected_plan_id = None
                    self.sessions.save(session)
                    emit("agent.plan.unselected", {
                        "session_id": session.session_id,
                        "previous_plan_id": old_id,
                        "actor": message.user_id,
                    })
                    return OutgoingMessage(text="🔄 Plan selection cleared for this session.", events=events, success=True, status="completed")

                if sub_action in ("status", "transition"):
                    if len(sub_parts) < 3:
                        return OutgoingMessage(
                            text="⚠️ Usage: `/plan status <plan_id> <target_status> [reason]`",
                            events=events,
                            success=False,
                            status="rejected",
                            error="Invalid usage",
                        )
                    p_id = sub_parts[1].strip()
                    tail = sub_parts[2].strip().split(maxsplit=1)
                    target_st = tail[0].strip()
                    reason_text = tail[1].strip() if len(tail) > 1 else f"Manual status update by {message.user_id}"

                    try:
                        updated_plan = registry.update_status(
                            plan_id=p_id,
                            new_status=target_st,
                            actor=message.user_id,
                            reason=reason_text,
                        )
                        emit("agent.plan.status_changed", {
                            "session_id": session.session_id,
                            "plan_id": updated_plan.id,
                            "new_status": updated_plan.status.value,
                            "actor": message.user_id,
                        })
                        return OutgoingMessage(
                            text=f"✅ Plan `{updated_plan.id}` transitioned to `{updated_plan.status.value}` (revision: {updated_plan.revision}).",
                            events=events,
                            success=True,
                            status="completed",
                        )
                    except (InvalidPlanTransitionError, PlanNotFoundError, StalePlanStateError) as trans_err:
                        return OutgoingMessage(
                            text=f"❌ Status transition rejected: {trans_err}",
                            events=events,
                            success=False,
                            status="rejected",
                            error=str(trans_err),
                        )

                if sub_action == "verify":
                    target_query = sub_parts[1].strip() if len(sub_parts) > 1 else (session.selected_plan_id or "")
                    if not target_query:
                        return OutgoingMessage(
                            text="⚠️ Usage: `/plan verify <plan_id>` (or select a plan first with `/plan select <id>`).",
                            events=events,
                            success=False,
                            status="rejected",
                            error="No plan specified for verification",
                        )
                    try:
                        passed, evidence, updated_plan = registry.verify_plan(
                            plan_id=target_query,
                            actor=message.user_id,
                        )
                        if passed:
                            emit("agent.plan.verified", {
                                "session_id": session.session_id,
                                "plan_id": updated_plan.id,
                                "revision": updated_plan.revision,
                                "workspace": ws_name,
                                "actor": message.user_id,
                            })
                            rep_lines = [
                                f"✅ **Plan Verification Succeeded:** `{updated_plan.id}` (**{updated_plan.title}**)",
                                f"• Status: `{updated_plan.status.value}` (revision: {updated_plan.revision})",
                                f"• Verifier: `{evidence.verifier}`",
                                f"• Timestamp: {evidence.timestamp}",
                                f"• Checks: {len(evidence.checks)} passed (structural criteria checks)",
                                f"• Scope: All declared acceptance criteria checks passed. (Note: Proves structural check execution; domain semantic correctness requires code review.)",
                            ]
                            for chk in evidence.checks:
                                rep_lines.append(f"  - `{chk.command}`: **{chk.status}**")
                            return OutgoingMessage(text="\n".join(rep_lines), events=events, success=True, status="completed")
                        else:
                            emit("agent.plan.verification_failed", {
                                "session_id": session.session_id,
                                "plan_id": updated_plan.id,
                                "revision": updated_plan.revision,
                                "workspace": ws_name,
                                "actor": message.user_id,
                            })
                            rep_lines = [
                                f"❌ **Plan Verification Failed:** `{updated_plan.id}` (**{updated_plan.title}**)",
                                f"• Status: `{updated_plan.status.value}` (revision: {updated_plan.revision})",
                                f"• Diagnostics preserved in failure history.",
                            ]
                            for chk in evidence.checks:
                                rep_lines.append(f"  - `{chk.command}`: **{chk.status}**")
                            return OutgoingMessage(text="\n".join(rep_lines), events=events, success=False, status="failed", error="Verification checks failed")
                    except (InvalidPlanTransitionError, PlanNotFoundError, StalePlanStateError) as v_err:
                        return OutgoingMessage(
                            text=f"❌ Verification rejected: {v_err}",
                            events=events,
                            success=False,
                            status="rejected",
                            error=str(v_err),
                        )

                if sub_action in ("questions", "unknowns", "clarifications"):
                    target_query = sub_parts[1].strip() if len(sub_parts) > 1 else (session.selected_plan_id or "")
                    if not target_query:
                        return OutgoingMessage(
                            text="⚠️ No plan specified. Use `/plan questions <plan_id>` or select a plan first with `/plan select <id>`.",
                            events=events,
                            success=False,
                            status="rejected",
                            error="No plan specified",
                        )
                    try:
                        questions = registry.list_blocking_questions(target_query)
                        if not questions:
                            return OutgoingMessage(
                                text=f"ℹ️ Plan `{target_query}` has no blocking questions or unknowns.",
                                events=events,
                                success=True,
                                status="completed",
                            )
                        lines = [f"📋 **Blocking Unknowns & Questions for Plan `{target_query}`:**\n"]
                        unresolved_count = 0
                        for q in questions:
                            status_icon = "❓ UNRESOLVED" if not q.get("resolved") else "✅ RESOLVED"
                            if not q.get("resolved"):
                                unresolved_count += 1
                            lines.append(f"• **[{q.get('id')}]** `{status_icon}` (Revision: {q.get('plan_revision', 1)})")
                            lines.append(f"  **Question:** {q.get('question') or q.get('decision')}")
                            if q.get("reason"):
                                lines.append(f"  **Why it matters:** {q.get('reason')}")
                            if q.get("affected_steps"):
                                lines.append(f"  **Affected Steps:** {', '.join(str(s) for s in q.get('affected_steps'))}")
                            if q.get("affected_files"):
                                lines.append(f"  **Affected Files:** {', '.join(str(f) for f in q.get('affected_files'))}")
                            if q.get("resolved"):
                                lines.append(f"  **Answer:** {q.get('answer')} (resolved by `{q.get('answered_by')}` at {str(q.get('answered_at', ''))[:19]})")
                            lines.append("")

                        if unresolved_count > 0:
                            lines.append("To resolve an unknown, provide an answer using:")
                            lines.append("  `/plan resolve <unknown_id> <your answer>`")
                        else:
                            lines.append("All blocking questions for this plan are resolved.")

                        emit("agent.plan.questions_listed", {
                            "session_id": session.session_id,
                            "plan_id": target_query,
                            "count": len(questions),
                            "unresolved_count": unresolved_count,
                            "actor": message.user_id,
                        })
                        return OutgoingMessage(text="\n".join(lines).strip(), events=events, success=True, status="completed")
                    except (PlanNotFoundError, PermissionError) as q_err:
                        return OutgoingMessage(
                            text=f"❌ Error listing questions: {q_err}",
                            events=events,
                            success=False,
                            status="rejected",
                            error=str(q_err),
                        )

                if sub_action in ("resolve", "answer"):
                    after_action = raw_args.split(maxsplit=1)[1].strip() if len(raw_args.split(maxsplit=1)) > 1 else ""
                    if not after_action:
                        return OutgoingMessage(
                            text="⚠️ Usage: `/plan resolve <unknown_id> <answer>` (or `/plan resolve <plan_id> <unknown_id> <answer>`).",
                            events=events,
                            success=False,
                            status="rejected",
                            error="Missing parameters for resolve",
                        )

                    tokens = after_action.split(maxsplit=2)
                    target_plan_id: Optional[str] = None
                    target_uid: Optional[str] = None
                    answer_text: Optional[str] = None

                    # Check if first token is an existing plan ID in the registry
                    if len(tokens) >= 3 and registry.get_plan(tokens[0]) is not None:
                        target_plan_id = tokens[0]
                        target_uid = tokens[1]
                        answer_text = tokens[2]
                    else:
                        target_plan_id = session.selected_plan_id
                        target_uid = tokens[0]
                        answer_text = after_action.split(maxsplit=1)[1].strip() if len(tokens) > 1 else ""

                    if not target_plan_id:
                        return OutgoingMessage(
                            text="⚠️ No plan selected. Usage: `/plan resolve <plan_id> <unknown_id> <answer>` or select a plan first with `/plan select <plan_id>`.",
                            events=events,
                            success=False,
                            status="rejected",
                            error="No plan specified",
                        )

                    if not target_uid or not answer_text:
                        return OutgoingMessage(
                            text="⚠️ Usage: `/plan resolve <unknown_id> <answer>` (both unknown ID and answer are required).",
                            events=events,
                            success=False,
                            status="rejected",
                            error="Missing unknown_id or answer",
                        )

                    try:
                        updated_plan = registry.resolve_blocking_unknown(
                            plan_id=target_plan_id,
                            unknown_id=target_uid,
                            answer=answer_text,
                            actor=message.user_id,
                            session_id=session.session_id,
                        )
                        emit("agent.plan.unknown_resolved", {
                            "session_id": session.session_id,
                            "plan_id": updated_plan.id,
                            "unknown_id": target_uid,
                            "actor": message.user_id,
                            "revision": updated_plan.revision,
                        })
                        resp_lines = [
                            f"✅ **Blocking Unknown Resolved:** `{target_uid}` in Plan `{updated_plan.id}` (**{updated_plan.title}**)",
                            f"• **Recorded Answer:** {answer_text}",
                            f"• **Updated Revision:** {updated_plan.revision}",
                            f"• **Plan Status:** `{updated_plan.status.value}`",
                            f"• **Assumptions Updated:** Answer bound to authoritative plan specification.",
                            "",
                            "🔒 **Security Notice:** Previous execution approvals have been invalidated due to plan requirement changes.",
                            "Answering a clarification does NOT grant execution authorization.",
                            f"Before BUILD mutations can resume, review the updated plan with `/plan show {updated_plan.id}` and obtain fresh user approval.",
                        ]
                        return OutgoingMessage(text="\n".join(resp_lines), events=events, success=True, status="completed")
                    except (ValueError, PlanNotFoundError, StalePlanStateError, PermissionError, InvalidPlanTransitionError) as res_err:
                        return OutgoingMessage(
                            text=f"❌ Failed to resolve unknown: {res_err}",
                            events=events,
                            success=False,
                            status="rejected",
                            error=str(res_err),
                        )

                if sub_action in ("show", "view", "info", "get") or not sub_action.startswith("-"):
                    target_query = sub_parts[1].strip() if sub_action in ("show", "view", "info", "get") and len(sub_parts) > 1 else raw_args
                    plan_obj = registry.get_plan(target_query)
                    if plan_obj is None:
                        try:
                            plan_obj = registry.get_plan_by_title(target_query)
                        except AmbiguousPlanError as amb_err:
                            return OutgoingMessage(text=f"❌ {amb_err}", events=events, success=False, status="rejected", error=str(amb_err))
                    if plan_obj is not None:
                        lines = [
                            f"📋 **Plan Details:** `{plan_obj.id}`\n",
                            f"• **Title:** {plan_obj.title}",
                            f"• **Status:** `{plan_obj.status.value}` (revision: {plan_obj.revision})",
                            f"• **Goal:** {plan_obj.goal}",
                            f"• **File:** `.brainfrog/plans/{plan_obj.filename}`",
                            f"• **Resumable:** {'Yes' if plan_obj.is_resumable else 'No'}",
                            f"• **Executable:** {'Yes' if plan_obj.is_executable else 'No'} (requires ApprovedExecutionContract)",
                            f"• **Verifiable:** {'Yes' if plan_obj.is_verifiable else 'No'}",
                            f"• **Created:** {plan_obj.created_at}",
                            f"• **Updated:** {plan_obj.updated_at}",
                        ]
                        if plan_obj.acceptance_criteria:
                            lines.append("\n**Acceptance Criteria:**")
                            for ac in plan_obj.acceptance_criteria:
                                lines.append(f"- [ ] {ac}")
                        if plan_obj.steps:
                            lines.append("\n**Steps:**")
                            for s in plan_obj.steps:
                                if isinstance(s, dict):
                                    lines.append(f"{s.get('id', '-')}. {s.get('description', '')}")
                                else:
                                    lines.append(f"- {s}")
                        if plan_obj.transitions:
                            lines.append(f"\n**Lifecycle History ({len(plan_obj.transitions)} transitions):**")
                            for t in plan_obj.transitions[-5:]:
                                lines.append(f"- `{t.get('from_status')}` ➔ `{t.get('to_status')}` by `{t.get('actor')}` ({t.get('timestamp', '')[:19]}): {t.get('reason')}")
                        if plan_obj.verification_history:
                            lines.append(f"\n**Verification Evidence:** {len(plan_obj.verification_history)} records")
                        if plan_obj.failure_history:
                            lines.append(f"\n**Failure History:** {len(plan_obj.failure_history)} records")

                        return OutgoingMessage(text="\n".join(lines), events=events, success=True, status="completed")
                    elif sub_action in ("show", "view", "info", "get"):
                        return OutgoingMessage(
                            text=f"❌ Plan '{target_query}' not found in workspace `{ws_name}`.\nUse `/plans` to list available plans.",
                            events=events,
                            success=False,
                            status="rejected",
                            error="Plan not found",
                        )
                    else:
                        # User typed `/plan <task>` that does not match an existing plan ID; switch to plan mode and route task to planning
                        if sub_action in ("create", "new", "init", "start"):
                            session.selected_plan_id = None
                            raw_text = raw_args.split(maxsplit=1)[1].strip() if len(sub_parts) > 1 else raw_args
                        else:
                            raw_text = raw_args

                        if session.active_mode != "plan":
                            old_mode = session.active_mode
                            session.active_mode = "plan"
                            emit("agent.mode.switched", {
                                "session_id": session.session_id,
                                "from": old_mode,
                                "to": "plan",
                                "actor": message.user_id,
                                "channel": effective_channel,
                            })
                        self.sessions.save(session)
                        requested_mode = "plan"

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
                work = self.work_store.get(target_work_id)
                if not work:
                    return OutgoingMessage(text=f"❌ Work '{target_work_id}' not found.", events=events, success=False, status="rejected")

                # Establish trusted recorded workspace from work/tx metadata
                recorded_ws_str = (work.resume_metadata or {}).get("workspace")
                if not recorded_ws_str and work.transaction_id:
                    tx_obj = self.transaction_store.get(work.transaction_id)
                    if tx_obj and tx_obj.workspace:
                        recorded_ws_str = tx_obj.workspace
                if not recorded_ws_str and work.approval_request_id:
                    appr_obj = self.approval_service.store.get(work.approval_request_id)
                    if appr_obj and appr_obj.workspace_root:
                        recorded_ws_str = appr_obj.workspace_root

                # For active executing/verifying states, fail closed if workspace is missing or invalid
                recovery_mgr = None
                if work.status not in (WorkStatus.DONE, WorkStatus.CANCELLED, WorkStatus.APPROVAL_REQUIRED):
                    if not recorded_ws_str or not str(recorded_ws_str).strip():
                        err_msg = f"Work '{target_work_id}' has no recorded workspace. Resume cannot establish canonical workspace."
                        emit("agent.work.resume_failed", {"work_id": target_work_id, "reason": err_msg})
                        return OutgoingMessage(text=f"❌ Resume failed: {err_msg}", events=events, success=False, status="rejected", error=err_msg)

                    recorded_ws = Path(recorded_ws_str).resolve()
                    if not recorded_ws.exists() or not recorded_ws.is_dir():
                        err_msg = f"Recorded workspace '{recorded_ws_str}' for work '{target_work_id}' does not exist on disk."
                        emit("agent.work.resume_failed", {"work_id": target_work_id, "reason": err_msg})
                        return OutgoingMessage(text=f"❌ Resume failed: {err_msg}", events=events, success=False, status="rejected", error=err_msg)

                    recovery_mgr = TransactionRecoveryManager(workspace=recorded_ws, store=self.transaction_store)
                elif recorded_ws_str and str(recorded_ws_str).strip():
                    recorded_ws = Path(recorded_ws_str).resolve()
                    if recorded_ws.exists() and recorded_ws.is_dir():
                        recovery_mgr = TransactionRecoveryManager(workspace=recorded_ws, store=self.transaction_store)
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
                tx = self.transaction_store.get(tx_id)
                if not tx:
                    return OutgoingMessage(text=f"❌ Transaction '{tx_id}' not found.", events=events, success=False, status="rejected")

                if not tx.workspace or not tx.workspace.strip():
                    err_msg = f"Transaction '{tx_id}' has no recorded workspace. Recovery cannot proceed safely."
                    emit("agent.transaction.recovery_failed", {"transaction_id": tx_id, "error": err_msg})
                    return OutgoingMessage(text=f"❌ Recovery failed: {err_msg}", events=events, success=False, status="rejected", error=err_msg)

                recorded_ws = Path(tx.workspace).resolve()
                if not recorded_ws.exists() or not recorded_ws.is_dir():
                    err_msg = f"Recorded workspace directory '{tx.workspace}' for transaction '{tx_id}' does not exist on disk."
                    emit("agent.transaction.recovery_failed", {"transaction_id": tx_id, "error": err_msg})
                    return OutgoingMessage(text=f"❌ Recovery failed: {err_msg}", events=events, success=False, status="rejected", error=err_msg)

                mgr = TransactionRecoveryManager(workspace=recorded_ws, store=self.transaction_store)
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

                # Validate workspace alignment: active session workspace must match approved workspace root
                try:
                    active_session_ws = self.get_session_workspace(session, message)
                except WorkspaceNotFoundError as ws_err:
                    return OutgoingMessage(text=f"❌ Workspace Error: {ws_err}", events=events, success=False, status="rejected", error=str(ws_err))

                approved_ws = Path(target_req.workspace_root).resolve()
                if active_session_ws.resolve() != approved_ws:
                    err_msg = (
                        f"Approval Workspace Mismatch: Request '{exec_req_id}' was approved for workspace "
                        f"'{approved_ws}', but active session workspace is '{active_session_ws}'. "
                        "Execution cannot cross workspace boundaries."
                    )
                    emit("agent.approval.invalid", {"request_id": exec_req_id, "reason": err_msg})
                    return OutgoingMessage(text=f"⚠️ {err_msg}", events=events, success=False, status="rejected", error=err_msg)

                raw_text = f"{target_req.canonical_operation.action_type} {target_req.canonical_operation.target}"

                # Sanitize metadata: discard untrusted incoming payload, use only approved canonical parameters
                trusted_meta = {}
                if "repo_dir" in message.metadata and message.channel in ("cli", "local", "terminal"):
                    trusted_meta["repo_dir"] = message.metadata["repo_dir"]
                trusted_meta["approval_id"] = exec_req_id
                trusted_meta["expected_digest"] = target_req.operation_digest
                trusted_meta["action_type"] = target_req.canonical_operation.action_type
                trusted_meta["target"] = target_req.canonical_operation.target
                trusted_meta["workspace_root"] = str(approved_ws)
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
            target_req = self.approval_service.store.get(str(approval_id))
            if not target_req:
                return OutgoingMessage(text=f"❌ Request '{approval_id}' not found.", events=events, success=False, status="rejected")
            appr_ws = Path(target_req.workspace_root).resolve()

            # Validate active session workspace matches approved workspace root
            try:
                active_session_ws = self.get_session_workspace(session, message)
            except WorkspaceNotFoundError as ws_err:
                return OutgoingMessage(text=f"❌ Workspace Error: {ws_err}", events=events, success=False, status="rejected", error=str(ws_err))

            if active_session_ws.resolve() != appr_ws:
                err_msg = (
                    f"Approval Workspace Mismatch: Request '{approval_id}' was approved for workspace "
                    f"'{appr_ws}', but active session workspace is '{active_session_ws}'. "
                    "Execution cannot cross workspace boundaries."
                )
                emit("agent.approval.invalid", {"request_id": approval_id, "reason": err_msg})
                return OutgoingMessage(text=f"⚠️ {err_msg}", events=events, success=False, status="rejected", error=err_msg)

            digest = message.metadata.get("expected_digest")
            if not digest:
                canonical_op = extract_canonical_operation(raw_text, action, message.metadata, repo_dir=appr_ws)
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
                        app_req, repo_dir=appr_ws,
                    )
                    expected_p_id = session.selected_plan_id if (execution_contract and execution_contract.plan_id) else (app_req.plan_id if app_req else None)
                    execution_contract.validate(
                        actor=message.user_id, channel=effective_channel,
                        session_id=session.session_id,
                        session_incarnation_id=session.session_incarnation_id,
                        repo_dir=active_session_ws.resolve(),
                        expected_plan_id=expected_p_id,
                    )
                    execution_contract.capabilities.require("network.access")
                except (ValueError, PermissionError) as exc:
                    return OutgoingMessage(text=str(exc), events=events, success=False,
                                           status="rejected", error=str(exc))

                # Transition bound plan READY -> IN_PROGRESS on authorized execution start (Fail-Closed)
                if execution_contract and execution_contract.plan_id:
                    try:
                        from core.plans import InvalidPlanTransitionError, PlanNotFoundError, PlanRegistry, PlanStatus
                        p_reg = PlanRegistry(active_session_ws.resolve())
                        target_p = p_reg.get_plan(execution_contract.plan_id)
                        if target_p is None:
                            raise PlanNotFoundError(f"Bound plan '{execution_contract.plan_id}' does not exist in workspace.")
                        if target_p.content_tampered:
                            raise InvalidPlanTransitionError(
                                f"Cannot execute plan '{execution_contract.plan_id}': Markdown content has been modified out-of-band."
                            )
                        if target_p.status == PlanStatus.READY:
                            p_reg.update_status(
                                plan_id=execution_contract.plan_id,
                                new_status=PlanStatus.IN_PROGRESS,
                                actor=message.user_id,
                                reason="Authorized execution started under ApprovedExecutionContract",
                                contract=execution_contract,
                            )
                        elif target_p.status != PlanStatus.IN_PROGRESS:
                            raise InvalidPlanTransitionError(
                                f"Cannot execute plan '{execution_contract.plan_id}': status is '{target_p.status.value}', expected 'ready' or 'in_progress'."
                            )
                    except Exception as plan_trans_err:
                        logger.error("Could not transition plan %s to IN_PROGRESS: %s", execution_contract.plan_id, plan_trans_err)
                        return OutgoingMessage(
                            text=f"❌ Execution rejected: plan lifecycle transition failed: {plan_trans_err}",
                            events=events,
                            success=False,
                            status="rejected",
                            error=str(plan_trans_err),
                        )

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
                try:
                    target_ws = self.get_session_workspace(session, message)
                except WorkspaceNotFoundError as ws_err:
                    emit("agent.rejected", {"action": action.value if hasattr(action, "value") else str(action), "reason": str(ws_err)})
                    return OutgoingMessage(
                        text=f"❌ Workspace Error: {ws_err}",
                        events=events,
                        success=False,
                        status="rejected",
                        error=str(ws_err),
                    )

                canonical_op = extract_canonical_operation(raw_text, action, message.metadata, repo_dir=target_ws)

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
                            intent=raw_text, workspace=target_ws, targets=targets,
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
                bound_plan_id = None
                bound_plan_rev = None
                bound_plan_hash = None
                bound_plan_meta_hash = None
                target_plan_id = message.metadata.get("plan_id") or session.selected_plan_id
                if target_plan_id:
                    try:
                        from core.plans import PlanRegistry
                        t_reg = PlanRegistry(target_ws)
                        p_doc = t_reg.get_plan(target_plan_id)
                        if p_doc is not None and not p_doc.content_tampered and not p_doc.metadata_malformed:
                            bound_plan_id = p_doc.id
                            bound_plan_rev = p_doc.revision
                            bound_plan_hash = p_doc.content_hash
                            bound_plan_meta_hash = getattr(p_doc, "metadata_hash", None)
                    except Exception:
                        pass

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
                        workspace_root=str(target_ws.resolve()),
                        plan_id=bound_plan_id,
                        plan_revision=bound_plan_rev,
                        plan_content_hash=bound_plan_hash,
                        plan_metadata_hash=bound_plan_meta_hash,
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
            if requested_mode == "build":
                # In Phase 1, Build mode is not implemented for remote execution without an approved contract
                emit("agent.mode.build_unimplemented", {
                    "session_id": session.session_id,
                    "channel": effective_channel,
                })
                return OutgoingMessage(
                    text="🔨 Build mode is defined, but plan selection and controlled execution are not implemented yet. Use `/mode plan` to draft an implementation plan or `/mode chat` for conversation.",
                    events=events,
                    success=True,
                    status="completed",
                )

        # 3. Resolve Workspace & Providers
        try:
            workspace_dir = self.get_session_workspace(session, message)
        except WorkspaceNotFoundError as ws_err:
            emit("agent.error", {"error": str(ws_err)})
            return OutgoingMessage(
                text=f"❌ Workspace Error: {ws_err}",
                events=events,
                success=False,
                status="rejected",
                error=str(ws_err),
            )
        backend = message.metadata.get("backend") or self.default_backend
        provider = message.metadata.get("provider") or session.active_provider or self.default_provider
        model = message.metadata.get("model") or session.active_model or self.default_model
        skill = message.metadata.get("skill") or session.active_skill
        auto_pr = bool(message.metadata.get("auto_pr", False)) and policy.allow_auto_pr
        max_retries = int(message.metadata.get("max_retries", 3))

        # Session active_mode remains isolated per session and is not mutated by execution
        if message.metadata.get("provider"):
            session.active_provider = message.metadata.get("provider")
        if message.metadata.get("model"):
            session.active_model = message.metadata.get("model")
        if skill:
            session.active_skill = skill

        test_cmd_list = self._resolve_test_cmd(message.metadata.get("test_cmd"), workspace_dir=workspace_dir)

        # Plan context resolution
        if approved_execution:
            # Under approved execution, plan_context is only trusted if explicitly authorized in the approval request
            plan_context = message.metadata.get("plan_context")
        else:
            plan_context = message.metadata.get("plan_context") or session.plan_context
            if not plan_context and session.selected_plan_id:
                try:
                    from core.plans import PlanRegistry, format_plan_handoff
                    reg = PlanRegistry(workspace_dir)
                    sel_doc = reg.get_plan(session.selected_plan_id)
                    if sel_doc is not None:
                        plan_context = format_plan_handoff(sel_doc, workspace_dir)
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
            from system2 import validate_system2_provider_preflight
            validate_system2_provider_preflight(provider)
            s2 = self.system2_factory(model=model, provider=provider)
        except Exception as e:
            err_msg = f"System 2 Initialization Error: {e}"
            emit("agent.error", {"error": err_msg})
            return OutgoingMessage(text=f"❌ {err_msg}", events=events, success=False, error=err_msg)

        # 5. Load Domains & Prepare Orchestrator Config
        can_auto_create = bool(effective_mode == "build" and policy.allow_code_edits and execution_contract is not None)
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
            plan_id=(
                execution_contract.plan_id
                if execution_contract is not None
                else (remote_context.plan_id if remote_context else (message.metadata.get("plan_id") or session.selected_plan_id))
            ),
            task_id=remote_context.work_id if remote_context else (message.metadata.get("task_id") or message.id),
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

        task_id_for_lock = remote_context.work_id if remote_context else (message.metadata.get("task_id") if message.metadata else None)
        ws_lock = WorkspaceLock(
            workspace=workspace_dir,
            session_id=session.session_id,
            actor=message.user_id,
            task_id=task_id_for_lock,
            timeout=float(message.metadata.get("lock_timeout", 10.0)) if message.metadata else 10.0,
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
            task_id_to_check = remote_context.work_id if remote_context else message.metadata.get("task_id")
            if task_id_to_check:
                proof_store = FileProofStore(workspace_dir)
                if proof_store.exists(task_id_to_check):
                    return OutgoingMessage(
                        text=f"Task '{task_id_to_check}' has already been executed with an authoritative proof.",
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
                if execution_contract and execution_contract.plan_id:
                    try:
                        from core.plans import PlanRegistry, PlanStatus
                        p_reg = PlanRegistry(workspace_dir)
                        curr_p = p_reg.get_plan(execution_contract.plan_id)
                        if curr_p and curr_p.status == PlanStatus.IN_PROGRESS:
                            p_reg.update_status(
                                plan_id=execution_contract.plan_id,
                                new_status=PlanStatus.FAILED,
                                actor=message.user_id,
                                reason="Execution cancelled by user",
                            )
                    except Exception:
                        pass
                if remote_context is not None:
                    try:
                        self.remote_work.transition_for_approval(
                            str(approval_id),
                            WorkStatus.FAILED,
                            failure=WorkFailure(
                                code="EXECUTION_CANCELLED",
                                stage="execution",
                                summary="Execution cancelled by user/runtime",
                                retryable=False,
                            ),
                        )
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
                if execution_contract and execution_contract.plan_id:
                    try:
                        from core.plans import PlanRegistry, PlanStatus
                        p_reg = PlanRegistry(workspace_dir)
                        curr_p = p_reg.get_plan(execution_contract.plan_id)
                        if curr_p and curr_p.status == PlanStatus.IN_PROGRESS:
                            p_reg.update_status(
                                plan_id=execution_contract.plan_id,
                                new_status=PlanStatus.FAILED,
                                actor=message.user_id,
                                reason=f"Orchestration Exception: {e}",
                            )
                    except Exception:
                        pass
                err_meta: Dict[str, Any] = {
                    "session_id": session.session_id,
                    "mode": effective_mode,
                }
                # Preserve authoritative transaction/test outcomes produced before the
                # failure so callers can distinguish rollback from an untracked error.
                err_tx = getattr(cfg, "last_transaction_result", None)
                if err_tx is not None:
                    err_meta["transaction_id"] = err_tx.transaction_id
                    err_meta["transaction_status"] = err_tx.status.value
                    err_meta["transaction_success"] = err_tx.success
                    err_meta["transaction_result"] = err_tx
                err_test = getattr(cfg, "last_test_result", None)
                if err_test is not None:
                    err_meta["test_result"] = err_test
                remote_result = None
                if remote_context is not None:
                    is_verif_fail = (
                        (cfg.last_transaction_result and cfg.last_transaction_result.rolled_back)
                        or "verification" in str(e).lower()
                        or "test" in str(e).lower()
                    )
                    self.remote_work.transition_for_approval(
                        str(approval_id),
                        WorkStatus.FAILED,
                        failure=WorkFailure(
                            code="VERIFICATION_FAILED" if is_verif_fail else "EXECUTION_FAILED",
                            stage="execution",
                            summary=f"Orchestration Error: {scrub_secrets(str(e))}",
                            retryable=False,
                        ),
                    )
                    txs = self.transaction_store.list(session_id=session.session_id, limit=20)
                    matching = next((tx for tx in txs if tx.work_id == remote_context.work_id), None)
                    if matching:
                        self.remote_work.bind_transaction(str(approval_id), matching.id)
                    remote_result = self.remote_work.result(
                        str(approval_id), "failed",
                        f"Orchestration Error: Work {remote_context.work_id} failed "
                        f"and was rolled back. {scrub_secrets(str(e))}",
                        error_code="verification_failed" if is_verif_fail else "execution_failed",
                        verification_passed=False,
                    )
                emit("agent.error", {"error": err_msg})
                return OutgoingMessage(
                    text=(remote_result.message if remote_result else f"❌ {err_msg}"),
                    events=events,
                    success=False,
                    status="error",
                    error=err_msg,
                    metadata={
                        **err_meta,
                        **({"remote_work": remote_result.to_dict()} if remote_result else {}),
                    },
                )

            emit("agent.completed", {"steps_count": len(results)})

            remote_result = None
            if remote_context is not None:
                tx_res = cfg.last_transaction_result
                test_proc = getattr(cfg, "last_test_result", None)
                work_id = remote_context.work_id

                if tx_res is None or not tx_res.success:
                    fail_summary = f"Work {work_id} failed and was rolled back."
                    if tx_res and getattr(tx_res, "error", None):
                        fail_summary += f" {tx_res.error}"
                    self.remote_work.transition_for_approval(
                        str(approval_id),
                        WorkStatus.FAILED,
                        failure=WorkFailure(
                            code="VERIFICATION_FAILED" if (tx_res and tx_res.rolled_back) else "EXECUTION_FAILED",
                            stage="execution",
                            summary=fail_summary,
                            retryable=False,
                        ),
                    )
                    remote_result = self.remote_work.result(
                        str(approval_id),
                        "failed",
                        fail_summary,
                        error_code="verification_failed",
                        verification_passed=False,
                    )
                else:
                    self.remote_work.bind_transaction(str(approval_id), tx_res.transaction_id)
                    resume_meta = {
                        "workspace": str(workspace_dir),
                        "task_id": work_id,
                        "transaction_id": tx_res.transaction_id,
                    }
                    self.remote_work.transition_for_approval(
                        str(approval_id),
                        WorkStatus.VERIFYING,
                        resume_metadata=resume_meta,
                    )

                    tx_ops = getattr(tx_res, "operations", []) or []
                    changed_files = [op.target for op in tx_ops]
                    expected_files = list(execution_contract.approved_targets) if execution_contract else changed_files

                    gate_passed, v_details, gate_errors = VerificationGate.verify(
                        workspace=workspace_dir,
                        expected_files=expected_files,
                        actual_files=changed_files,
                        test_proc=test_proc,
                        tx_res=tx_res,
                        tracked_pids=None,
                        channel=effective_channel,
                        contract=execution_contract,
                    )

                    tests_passed = bool(test_proc and test_proc.returncode == 0 and not getattr(test_proc, "is_timeout", False))
                    proc_stat = getattr(test_proc, "status", "success") if test_proc else "failed"
                    proof_verdict = compute_proof_verdict(
                        tx_status="committed",
                        tests_passed=tests_passed,
                        gate_passed=gate_passed,
                        process_status=proc_stat,
                        rollback_error=None,
                        is_recovery_required=False,
                        gate_details=v_details,
                    )

                    change_records = build_change_records(tx_ops, workspace_dir)
                    if not change_records and changed_files:
                        for cf in changed_files:
                            try:
                                cf_rel = normalize_workspace_relative_path(cf, workspace_dir)
                                cf_disk = workspace_dir / cf_rel
                                cf_hash = hashlib.sha256(cf_disk.read_bytes()).hexdigest() if cf_disk.exists() else None
                                change_records.append(
                                    ChangeRecord(
                                        path=cf_rel,
                                        operation="MODIFY",
                                        before_sha256=None,
                                        after_sha256=cf_hash,
                                        byte_count_delta=0,
                                    )
                                )
                            except Exception:
                                pass

                    now_iso = datetime.now(timezone.utc).isoformat()
                    task_evidence = TaskEvidence(
                        task_id=work_id,
                        session_id=session.session_id,
                        description=redact_secrets(remote_context.request.intent),
                        created_at=now_iso,
                        completed_at=now_iso,
                        duration_ms=0,
                    )

                    raw_stdout = getattr(test_proc, "stdout", "") or ""
                    raw_stderr = getattr(test_proc, "stderr", "") or ""
                    stdout_bounded, stdout_trunc = capture_bounded_output(raw_stdout)
                    stderr_bounded, stderr_trunc = capture_bounded_output(raw_stderr)
                    test_cmd_str = " ".join(cfg.test_command) if isinstance(cfg.test_command, (list, tuple)) else str(cfg.test_command or "")

                    proc_evidence = None
                    if test_proc is not None:
                        p_start_raw = getattr(test_proc, "started_at", None)
                        p_comp_raw = getattr(test_proc, "completed_at", None)
                        p_start_iso = datetime.fromtimestamp(p_start_raw, tz=timezone.utc).isoformat() if isinstance(p_start_raw, (int, float)) else ""
                        p_comp_iso = datetime.fromtimestamp(p_comp_raw, tz=timezone.utc).isoformat() if isinstance(p_comp_raw, (int, float)) else ""
                        p_stat_val = getattr(test_proc.status, "value", str(test_proc.status))
                        proc_evidence = ProcessEvidence(
                            task_id=work_id,
                            transaction_id=tx_res.transaction_id,
                            command=test_cmd_str,
                            cwd=str(workspace_dir),
                            started_at=p_start_iso,
                            completed_at=p_comp_iso,
                            duration_ms=getattr(test_proc, "duration_ms", 0) or 0,
                            returncode=test_proc.returncode,
                            status=str(p_stat_val),
                            timed_out=getattr(test_proc, "is_timeout", False),
                            cancelled=getattr(test_proc, "is_cancelled", False),
                            truncated=getattr(test_proc, "is_truncated", False) or stdout_trunc or stderr_trunc,
                        )

                    v_evidence = VerificationEvidence(
                        test_command=test_cmd_str,
                        exit_code=test_proc.returncode if test_proc else None,
                        stdout_summary=stdout_bounded,
                        stderr_summary=stderr_bounded,
                        stdout_truncated=stdout_trunc,
                        stderr_truncated=stderr_trunc,
                        gate_checks=v_details,
                        process_evidence=proc_evidence,
                    )

                    changes_evidence = ChangesEvidence(
                        transaction_id=tx_res.transaction_id,
                        status="COMMITTED",
                        files=change_records,
                    )

                    reproducibility = ReproducibilityEvidence(
                        command=test_cmd_str,
                        expected_exit_code=0,
                        expected_file_hashes={r.path: r.after_sha256 for r in change_records if r.after_sha256},
                    )

                    from core.runtime.task_finalization import TaskFinalizationCoordinator
                    intent_record = TaskFinalizationCoordinator(workspace_dir).get_intent(work_id)
                    commit_sha = intent_record.commit_sha if intent_record else None

                    proof_artifact = ProofArtifact(
                        version="1.0.0",
                        task=task_evidence,
                        verdict=proof_verdict,
                        provenance=capture_git_provenance(
                            workspace_dir,
                            commit_sha=commit_sha,
                        ),
                        verification=v_evidence,
                        changes=changes_evidence,
                        reproducibility=reproducibility,
                    )

                    proof_store = FileProofStore(workspace_dir)
                    proof_path_str: Optional[str] = None
                    store_failed = False
                    store_err_msg = ""
                    try:
                        md_text = render_proof_markdown(proof_artifact)
                        pj, pm = proof_store.save(proof_artifact, md_text)
                        proof_path_str = str(pj)
                        TaskFinalizationCoordinator(workspace_dir).finalize_proof(work_id)
                    except Exception as store_exc:
                        store_failed = True
                        store_err_msg = str(store_exc)

                    if store_failed or proof_path_str is None:
                        fail_summary = f"Task execution succeeded and committed, but proof persistence failed: {store_err_msg}. Recovery required."
                        self.remote_work.transition_for_approval(
                            str(approval_id),
                            WorkStatus.FAILED,
                            failure=WorkFailure(
                                code="PROOF_PERSISTENCE_FAILED",
                                stage="proof",
                                summary=fail_summary,
                                retryable=False,
                            ),
                            resume_metadata=resume_meta,
                        )
                        remote_result = self.remote_work.result(
                            str(approval_id),
                            "failed",
                            fail_summary,
                            error_code="proof_persistence_failed",
                            verification_passed=False,
                        )
                    else:
                        verifier = DeterministicProofVerifier(workspace_dir, verify_disk_integrity=True)
                        rep = verifier.verify(work_id)
                        proof_obj = None
                        try:
                            proof_obj = proof_store.load(work_id)
                        except Exception:
                            pass

                        is_proof_valid = (
                            rep.overall_status == ProofVerificationStatus.VALID
                            and rep.verdict_status == "VERIFIED"
                            and proof_verdict.status == "VERIFIED"
                            and proof_obj is not None
                            and proof_obj.verdict.status == "VERIFIED"
                            and proof_obj.changes.status in ("COMMITTED", "VERIFIED")
                            and proof_obj.task.task_id == work_id
                            and proof_obj.changes.transaction_id == tx_res.transaction_id
                        )

                        if is_proof_valid:
                            passed_c = len([c for c in rep.checks if c.status == ProofVerificationStatus.VALID])
                            v_res = VerificationResult(
                                status=VerificationStatus.PASS,
                                passed=passed_c,
                                failed=0,
                                summary=f"Authoritative proof verified ({work_id})",
                            )
                            self.remote_work.transition_for_approval(
                                str(approval_id),
                                WorkStatus.DONE,
                                verification_result=v_res,
                                resume_metadata=resume_meta,
                            )
                            remote_result = self.remote_work.result(
                                str(approval_id),
                                "completed",
                                f"Work {work_id} completed successfully.",
                                proof_path=proof_path_str,
                                verification_passed=True,
                            )
                        else:
                            fail_reasons = []
                            if rep.overall_status != ProofVerificationStatus.VALID:
                                fail_reasons.append(f"verifier status: {rep.overall_status.value}")
                            if proof_verdict.status != "VERIFIED":
                                fail_reasons.append(f"verdict status: {proof_verdict.status}")
                            if gate_errors:
                                fail_reasons.extend(gate_errors)
                            fail_summary = f"Verification failed for work {work_id}: {'; '.join(fail_reasons) or 'Evidence mismatch'}."
                            self.remote_work.transition_for_approval(
                                str(approval_id),
                                WorkStatus.FAILED,
                                failure=WorkFailure(
                                    code="VERIFICATION_FAILED",
                                    stage="verifying",
                                    summary=fail_summary,
                                    retryable=False,
                                ),
                                resume_metadata=resume_meta,
                            )
                            remote_result = self.remote_work.result(
                                str(approval_id),
                                "failed",
                                fail_summary,
                                error_code="verification_failed",
                                proof_path=proof_path_str,
                                verification_passed=False,
                            )

        finally:
            ws_lock.release()

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
                if getattr(r, "suggestion", None):
                    session.selected_plan_id = str(r.suggestion)
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
            is_success = remote_result.status == "completed" and remote_result.verification_passed

        # Phase 2B.2: IN_PROGRESS -> IMPLEMENTED on successful execution; FAILED on failure
        if execution_contract and execution_contract.plan_id:
            try:
                from core.plans import PlanRegistry, PlanStatus
                p_reg = PlanRegistry(workspace_dir)
                curr_p = p_reg.get_plan(execution_contract.plan_id)
                if curr_p and curr_p.status == PlanStatus.IN_PROGRESS:
                    if is_success:
                        p_reg.update_status(
                            plan_id=execution_contract.plan_id,
                            new_status=PlanStatus.IMPLEMENTED,
                            actor=message.user_id,
                            reason="Plan execution completed successfully",
                        )
                        session.selected_plan_id = None
                    else:
                        p_reg.update_status(
                            plan_id=execution_contract.plan_id,
                            new_status=PlanStatus.FAILED,
                            actor=message.user_id,
                            reason=f"Plan execution failed: {final_text[:200]}",
                        )
            except Exception as plan_trans_err:
                logger.warning("Failed to transition plan %s post-execution: %s", execution_contract.plan_id, plan_trans_err)

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
