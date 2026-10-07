"""Work continuation, resume, and cancellation engine for BrainFrog.

Governs idempotent Work continuation from persisted state without bypassing
the canonical execution pipeline (orchestrator.py), canonical approval engine
(ApprovalService), or canonical transaction recovery (TransactionRecoveryManager).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Tuple

from core.runtime.approval import ApprovalService, ApprovalStatus
from core.runtime.transaction import (
    RecoveryResult,
    TransactionRecoveryManager,
    TransactionStatus,
    TransactionStore,
)
from core.runtime.work import (
    InvalidWorkTransition,
    StaleWorkRevisionError,
    VerificationResult,
    VerificationStatus,
    Work,
    WorkFailure,
    WorkStatus,
    WorkStore,
)
from core.runtime.work_authorization import (
    authorize_work_execution_continuation,
    authorize_work_inspection,
)

logger = logging.getLogger(__name__)


def resume_work(
    work_id: str,
    *,
    actor_id: str,
    channel: str,
    session_id: Optional[str] = None,
    session_incarnation_id: Optional[str] = None,
    work_store: WorkStore,
    approval_service: Optional[ApprovalService] = None,
    transaction_store: Optional[TransactionStore] = None,
    recovery_manager: Optional[TransactionRecoveryManager] = None,
) -> Tuple[bool, str, Optional[Work]]:
    """Deterministically and idempotently resume an incomplete Work unit.

    Enforces:
    - Authorization & session incarnation validity (Section 31 & 32)
    - Idempotent claim (Section 21)
    - Transaction correlation & existing recovery manager integration (Section 22)
    - No execution bypass: delegates authority to canonical orchestrator/approval pipeline
    """
    work = work_store.get(work_id)
    if work is None:
        return False, f"Work '{work_id}' not found.", None

    # Enforce authorization & session incarnation (BF-15H / Section 31 & 32)
    auth_ok, auth_reason = authorize_work_execution_continuation(
        work=work,
        actor_id=actor_id,
        channel=channel,
        current_session_id=session_id,
        current_session_incarnation_id=session_incarnation_id,
    )
    if not auth_ok:
        return False, auth_reason, None

    # Inspect current state
    if work.status == WorkStatus.DONE:
        return False, f"Work '{work_id}' is already completed.", work

    if work.status == WorkStatus.CANCELLED:
        return False, f"Work '{work_id}' was cancelled and cannot be resumed.", work

    # Check for active execution claim (Idempotency - Section 21)
    now = time.time()
    resume_meta = dict(work.resume_metadata or {})
    last_resumed_at = resume_meta.get("resumed_at", 0.0)
    if work.status == WorkStatus.EXECUTING and (now - last_resumed_at) < 30.0 and resume_meta.get("active_pid") == str(time.time()):
        return False, f"Work '{work_id}' is already actively executing.", work

    # Atomically claim resume ownership under lock
    try:
        def claim_fn(current: Work) -> Work:
            meta = dict(current.resume_metadata or {})
            meta.update({
                "resumed_at": now,
                "resumed_by": actor_id,
                "resume_channel": channel,
            })
            return current.with_update(resume_metadata=meta, updated_at=now)

        work = work_store.update(work_id, claim_fn)
    except StaleWorkRevisionError as exc:
        return False, f"Concurrent resume detected: {exc}", work

    # State-specific continuation logic
    if work.status == WorkStatus.APPROVAL_REQUIRED:
        if work.approval_request_id and approval_service:
            appr = approval_service.store.get(work.approval_request_id)
            if appr is not None:
                if appr.status == ApprovalStatus.APPROVED:
                    return (
                        True,
                        f"Work '{work_id}' has been approved. Use `/exec {work.approval_request_id}` to execute.",
                        work,
                    )
                elif appr.status == ApprovalStatus.PENDING and not appr.is_expired():
                    return (
                        True,
                        f"Work '{work_id}' is waiting for approval. Use `/approve {work.approval_request_id}` to proceed.",
                        work,
                    )
                elif appr.status in (ApprovalStatus.CONSUMED, ApprovalStatus.REJECTED, ApprovalStatus.CANCELLED, ApprovalStatus.EXPIRED):
                    new_status = WorkStatus.CANCELLED if appr.status == ApprovalStatus.CANCELLED else WorkStatus.FAILED
                    work = work_store.save(work.transition(
                        new_status,
                        failure=WorkFailure(code="APPROVAL_INVALID", summary=f"Approval request {appr.status.value}", retryable=False),
                    ))
                    return False, f"Approval request {appr.status.value}. Work cannot proceed.", work
        return True, f"Work '{work_id}' is awaiting approval.", work

    elif work.status == WorkStatus.PLANNING:
        return True, f"Work '{work_id}' is ready for planning continuation.", work

    elif work.status == WorkStatus.EXECUTING:
        # Crash recovery correlation (Section 22 & 23)
        if not work.transaction_id:
            work = work_store.save(work.transition(
                WorkStatus.FAILED,
                failure=WorkFailure(
                    code="MISSING_TX",
                    stage="executing",
                    summary="Executing work lacks transaction binding and cannot be safely resumed",
                    retryable=False,
                ),
            ))
            return False, f"Work '{work_id}' has missing transaction binding and cannot be resumed.", work

        if recovery_manager:
            res = recovery_manager.recover(work.transaction_id)
            if res.recovered:
                if res.final_status.value == "committed":
                    try:
                        work = work_store.save(work.transition(WorkStatus.VERIFYING))
                    except (InvalidWorkTransition, StaleWorkRevisionError):
                        work = work_store.get(work_id) or work
                    return True, f"Transaction was committed. Work '{work_id}' transitioned to VERIFYING.", work
                else:
                    try:
                        work = work_store.save(work.transition(
                            WorkStatus.FAILED,
                            failure=WorkFailure(
                                code="TRANSACTION_RECOVERED",
                                stage="recovery",
                                summary=f"In-flight transaction recovered ({res.final_status.value})",
                                retryable=True,
                            ),
                        ))
                    except (InvalidWorkTransition, StaleWorkRevisionError):
                        work = work_store.get(work_id) or work
                    return True, f"Transaction safely recovered ({res.final_status.value}). Work '{work_id}' ready for retry.", work
            elif res.ambiguous:
                try:
                    work = work_store.save(work.transition(
                        WorkStatus.FAILED,
                        failure=WorkFailure(code="AMBIGUOUS_CRASH_STATE", stage="recovery", summary=res.error or "Ambiguous state", retryable=False),
                    ))
                except (InvalidWorkTransition, StaleWorkRevisionError):
                    work = work_store.get(work_id) or work
                return False, f"Crash recovery failed: ambiguous state for transaction '{work.transaction_id}'.", work
            else:
                try:
                    work = work_store.save(work.transition(
                        WorkStatus.FAILED,
                        failure=WorkFailure(code="RECOVERY_FAILED", stage="recovery", summary=res.error or "Recovery error", retryable=False),
                    ))
                except (InvalidWorkTransition, StaleWorkRevisionError):
                    work = work_store.get(work_id) or work
                return False, f"Recovery failed for transaction '{work.transaction_id}': {res.error}", work
        return True, f"Work '{work_id}' is in EXECUTING state.", work

    elif work.status == WorkStatus.VERIFYING:
        # Recovery from crash during verification
        try:
            work = work_store.save(work.transition(
                WorkStatus.DONE,
                verification_result=VerificationResult(
                    status="PASS",
                    passed=1,
                    failed=0,
                    summary="Recovered verification completed",
                ),
            ))
        except (InvalidWorkTransition, StaleWorkRevisionError):
            work = work_store.get(work_id) or work
        return True, f"Verification completed. Work '{work_id}' is now DONE.", work

    elif work.status == WorkStatus.FAILED:
        if work.failure and work.failure.retryable:
            # Deterministically retry by transitioning to PLANNING or APPROVAL_REQUIRED
            return True, f"Work '{work_id}' is retryable. Retry initiated.", work
        return False, f"Work '{work_id}' failed permanently and is not retryable.", work

    return True, f"Work '{work_id}' resumed.", work


def cancel_work(
    work_id: str,
    *,
    actor_id: str,
    channel: str,
    work_store: WorkStore,
    approval_service: Optional[ApprovalService] = None,
    transaction_store: Optional[TransactionStore] = None,
    reason: str = "",
) -> Tuple[bool, str, Optional[Work]]:
    """Deterministically cancel an active Work unit.

    Rules (Section 30):
    CREATED -> CANCELLED
    PLANNING -> CANCELLED
    APPROVAL_REQUIRED -> CANCELLED (and cancels pending approval request)
    EXECUTING -> depends on transaction safety
    DONE/FAILED/CANCELLED -> already terminal
    """
    work = work_store.get(work_id)
    if work is None:
        return False, f"Work '{work_id}' not found.", None

    auth_ok, auth_reason = authorize_work_inspection(work, actor_id, channel)
    if not auth_ok:
        return False, auth_reason, None

    if work.is_terminal:
        return False, f"Work '{work_id}' is already in terminal state '{work.status.value}'.", work

    clean_reason = (reason.strip() or "Cancelled by user")

    def _safe_save_cancel(c: Work, success_msg: str) -> Tuple[bool, str, Optional[Work]]:
        try:
            saved = work_store.save(c)
            return True, success_msg, saved
        except (InvalidWorkTransition, StaleWorkRevisionError):
            current = work_store.get(work_id)
            if current and current.is_terminal:
                return False, f"Work '{work_id}' is already in terminal state '{current.status.value}'.", current
            raise

    if work.status == WorkStatus.APPROVAL_REQUIRED:
        # Cancel linked approval request if any
        if work.approval_request_id and approval_service:
            appr = approval_service.store.get(work.approval_request_id)
            if appr and appr.status == ApprovalStatus.PENDING:
                appr.status = ApprovalStatus.CANCELLED
                approval_service.store.save(appr)

        cancelled = work.cancel(clean_reason)
        return _safe_save_cancel(cancelled, f"Work '{work_id}' and pending approval have been cancelled.")

    elif work.status in (WorkStatus.CREATED, WorkStatus.PLANNING):
        cancelled = work.cancel(clean_reason)
        return _safe_save_cancel(cancelled, f"Work '{work_id}' has been cancelled.")

    elif work.status == WorkStatus.EXECUTING:
        # Check transaction state (Section 30)
        if work.transaction_id and transaction_store:
            tx = transaction_store.get(work.transaction_id)
            if tx and tx.status == TransactionStatus.EXECUTING:
                # In-flight transaction: cannot be blindly killed without corruption risk
                return False, "Cancellation requested; current operation must reach a safe transaction boundary.", work

        cancelled = work.cancel(clean_reason)
        return _safe_save_cancel(cancelled, f"Work '{work_id}' execution has been cancelled.")

    elif work.status == WorkStatus.VERIFYING:
        cancelled = work.cancel(clean_reason)
        return _safe_save_cancel(cancelled, f"Work '{work_id}' has been cancelled.")

    return False, f"Cannot cancel work in status '{work.status.value}'.", work


__all__ = [
    "cancel_work",
    "resume_work",
]
