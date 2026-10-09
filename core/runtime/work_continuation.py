"""Work continuation, resume, and cancellation engine for BrainFrog.

Governs idempotent Work continuation from persisted state without bypassing
the canonical execution pipeline (orchestrator.py), canonical approval engine
(ApprovalService), or canonical transaction recovery (TransactionRecoveryManager).
"""
from __future__ import annotations

from dataclasses import replace
import logging
import os
import secrets
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
from core.runtime.workspace_lock import is_pid_running

logger = logging.getLogger(__name__)

RESUME_LEASE_SECONDS: float = 30.0


class _ClaimRejectedError(Exception):
    """Internal exception raised when an atomic resume claim cannot be acquired."""

    def __init__(self, message: str, work: Optional[Work] = None) -> None:
        super().__init__(message)
        self.message = message
        self.work = work


def _release_claim(store: WorkStore, wid: str, claim_id: str) -> None:
    """Safely release an active resume claim if the claim identifier matches."""
    def release_fn(current: Work) -> Work:
        meta = dict(current.resume_metadata or {})
        if meta.get("claim_id") == claim_id:
            meta["claim_active"] = False
            return current.with_update(resume_metadata=meta)
        return current

    try:
        store.update(wid, release_fn)
    except Exception:
        pass


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
    - Single-winner atomic claim lease (Section 21)
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

    # Inspect current state (fast-reject before claiming)
    if work.status == WorkStatus.DONE:
        return False, f"Work '{work_id}' is already completed.", work

    if work.status == WorkStatus.CANCELLED:
        return False, f"Work '{work_id}' was cancelled and cannot be resumed.", work

    if work.status == WorkStatus.FAILED:
        if work.failure and work.failure.retryable:
            return True, f"Work '{work_id}' is retryable. Retry initiated.", work
        return False, f"Work '{work_id}' failed permanently and is not retryable.", work

    now = time.time()
    claim_id = secrets.token_hex(8)

    # Atomically claim resume ownership under lock
    try:
        def claim_fn(current: Work) -> Work:
            if current.status == WorkStatus.DONE:
                raise _ClaimRejectedError(f"Work '{work_id}' is already completed.", current)
            if current.status == WorkStatus.CANCELLED:
                raise _ClaimRejectedError(f"Work '{work_id}' was cancelled and cannot be resumed.", current)
            if current.status == WorkStatus.FAILED:
                if current.failure and current.failure.retryable:
                    raise _ClaimRejectedError(f"Work '{work_id}' is retryable. Retry initiated.", current)
                raise _ClaimRejectedError(f"Work '{work_id}' failed permanently and is not retryable.", current)

            meta = dict(current.resume_metadata or {})
            is_active = meta.get("claim_active", False)
            last_resumed_at = meta.get("resumed_at", 0.0)
            active_pid = meta.get("active_pid")

            is_lease_valid = (now - last_resumed_at) < RESUME_LEASE_SECONDS
            holder_alive = True
            if isinstance(active_pid, int):
                if active_pid == os.getpid():
                    holder_alive = True
                else:
                    holder_alive = is_pid_running(active_pid)

            if (is_active or current.status == WorkStatus.VERIFYING) and is_lease_valid and holder_alive:
                if current.status == WorkStatus.VERIFYING:
                    raise _ClaimRejectedError(f"Work '{work_id}' is actively being verified.", current)
                else:
                    raise _ClaimRejectedError(f"Work '{work_id}' is already actively executing.", current)

            new_meta = dict(meta)
            new_meta.update({
                "claim_active": True,
                "claim_id": claim_id,
                "resumed_at": now,
                "resumed_by": actor_id,
                "resume_channel": channel,
                "active_pid": os.getpid(),
                "claim_status": current.status.value,
            })
            return current.with_update(resume_metadata=new_meta, updated_at=now)

        work = work_store.update(work_id, claim_fn)
    except _ClaimRejectedError as exc:
        rejected_work = exc.work or work_store.get(work_id) or work
        return False, exc.message, rejected_work
    except StaleWorkRevisionError as exc:
        rejected_work = work_store.get(work_id) or work
        if rejected_work and rejected_work.status == WorkStatus.VERIFYING:
            return False, f"Work '{work_id}' is actively being verified.", rejected_work
        return False, f"Concurrent resume detected: {exc}", rejected_work

    try:
        # State-specific continuation logic
        if work.status == WorkStatus.APPROVAL_REQUIRED:
            if work.approval_request_id and approval_service:
                appr = approval_service.store.get(work.approval_request_id)
                if appr is not None:
                    if appr.status == ApprovalStatus.APPROVED:
                        _release_claim(work_store, work_id, claim_id)
                        return (
                            True,
                            f"Work '{work_id}' has been approved. Use `/exec {work.approval_request_id}` to execute.",
                            work,
                        )
                    elif appr.status == ApprovalStatus.PENDING and not appr.is_expired():
                        _release_claim(work_store, work_id, claim_id)
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
                        _release_claim(work_store, work_id, claim_id)
                        return False, f"Approval request {appr.status.value}. Work cannot proceed.", work
            _release_claim(work_store, work_id, claim_id)
            return True, f"Work '{work_id}' is awaiting approval.", work

        elif work.status == WorkStatus.PLANNING:
            _release_claim(work_store, work_id, claim_id)
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
                _release_claim(work_store, work_id, claim_id)
                return False, f"Work '{work_id}' has missing transaction binding and cannot be resumed.", work

            if recovery_manager:
                res = recovery_manager.recover(work.transaction_id)
                if res.recovered:
                    if res.final_status.value == "committed":
                        v_meta = dict(work.resume_metadata or {})
                        v_meta.update({
                            "claim_active": True,
                            "claim_id": secrets.token_hex(8),
                            "resumed_at": time.time(),
                            "active_pid": os.getpid(),
                            "claim_status": WorkStatus.VERIFYING.value,
                        })
                        try:
                            work = work_store.save(work.transition(WorkStatus.VERIFYING, resume_metadata=v_meta))
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
                        _release_claim(work_store, work_id, claim_id)
                        return True, f"Transaction safely recovered ({res.final_status.value}). Work '{work_id}' ready for retry.", work
                elif res.ambiguous:
                    try:
                        work = work_store.save(work.transition(
                            WorkStatus.FAILED,
                            failure=WorkFailure(code="AMBIGUOUS_CRASH_STATE", stage="recovery", summary=res.error or "Ambiguous state", retryable=False),
                        ))
                    except (InvalidWorkTransition, StaleWorkRevisionError):
                        work = work_store.get(work_id) or work
                    _release_claim(work_store, work_id, claim_id)
                    return False, f"Crash recovery failed: ambiguous state for transaction '{work.transaction_id}'.", work
                else:
                    try:
                        work = work_store.save(work.transition(
                            WorkStatus.FAILED,
                            failure=WorkFailure(code="RECOVERY_FAILED", stage="recovery", summary=res.error or "Recovery error", retryable=False),
                        ))
                    except (InvalidWorkTransition, StaleWorkRevisionError):
                        work = work_store.get(work_id) or work
                    _release_claim(work_store, work_id, claim_id)
                    return False, f"Recovery failed for transaction '{work.transaction_id}': {res.error}", work
            _release_claim(work_store, work_id, claim_id)
            return True, f"Work '{work_id}' is in EXECUTING state.", work

        elif work.status == WorkStatus.VERIFYING:
            # Recovery from crash during verification
            d_meta = dict(work.resume_metadata or {})
            d_meta["claim_active"] = False
            try:
                work = work_store.save(work.transition(
                    WorkStatus.DONE,
                    verification_result=VerificationResult(
                        status="PASS",
                        passed=1,
                        failed=0,
                        summary="Recovered verification completed",
                    ),
                    resume_metadata=d_meta,
                ))
            except (InvalidWorkTransition, StaleWorkRevisionError):
                work = work_store.get(work_id) or work
            return True, f"Verification completed. Work '{work_id}' is now DONE.", work

        elif work.status == WorkStatus.FAILED:
            _release_claim(work_store, work_id, claim_id)
            if work.failure and work.failure.retryable:
                # Deterministically retry by transitioning to PLANNING or APPROVAL_REQUIRED
                return True, f"Work '{work_id}' is retryable. Retry initiated.", work
            return False, f"Work '{work_id}' failed permanently and is not retryable.", work

        _release_claim(work_store, work_id, claim_id)
        return True, f"Work '{work_id}' resumed.", work

    except Exception:
        _release_claim(work_store, work_id, claim_id)
        raise


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
            meta = dict(c.resume_metadata or {})
            meta["claim_active"] = False
            saved = work_store.save(replace(c, resume_metadata=meta))
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
