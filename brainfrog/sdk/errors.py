"""BrainFrog Phase 5 — SDK Typed Error Hierarchy.

Defines the minimal, complete error taxonomy for the BrainFrog Runtime SDK.
Distinguishes SDK transport/request errors from task execution and verification failures.

Product Thesis:
    "Don't just trust the agent. Verify it."
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class BrainFrogSDKError(Exception):
    """Base exception for all BrainFrog SDK errors."""

    def __init__(
        self,
        message: str,
        *,
        task_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.task_id = task_id
        self.details: Dict[str, Any] = details or {}

    def __str__(self) -> str:
        if self.task_id:
            return f"[{self.task_id}] {self.message}"
        return self.message


class InvalidRequestError(BrainFrogSDKError):
    """Raised when an SDK request is malformed, has invalid parameters, or violates invariants."""
    pass


class PermissionDeniedError(BrainFrogSDKError):
    """Raised when an operation is rejected by the runtime trust policy or channel constraints."""
    pass


class ApprovalRequiredError(BrainFrogSDKError):
    """Raised when an operation requires explicit human approval before execution."""

    def __init__(
        self,
        message: str,
        *,
        approval_id: str,
        operation_type: str = "",
        risk_class: str = "",
        task_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        merged_details = dict(details or {})
        merged_details.update({
            "approval_id": approval_id,
            "operation_type": operation_type,
            "risk_class": risk_class,
        })
        super().__init__(message, task_id=task_id, details=merged_details)
        self.approval_id = approval_id
        self.operation_type = operation_type
        self.risk_class = risk_class


class WorkspaceBusyError(BrainFrogSDKError):
    """Raised when the target workspace is locked by another active task or session."""
    pass


class TaskNotFoundError(BrainFrogSDKError):
    """Raised when a referenced task_id does not exist in active tasks or proof store."""
    pass


class TaskCancelledError(BrainFrogSDKError):
    """Raised when a task was cancelled by user request or runtime cancellation."""
    pass


class ExecutionFailedError(BrainFrogSDKError):
    """Raised when task execution fails before verification (e.g. orchestration or tool failure)."""
    pass


class VerificationFailedError(BrainFrogSDKError):
    """Raised when task execution passes but fails the authoritative VerificationGate."""
    pass


class RecoveryRequiredError(BrainFrogSDKError):
    """Raised when a transaction failure puts the workspace in a RECOVERY_REQUIRED state."""
    pass


class ProofPersistenceError(BrainFrogSDKError):
    """Raised when the runtime is unable to durably persist the authoritative proof artifact."""
    pass


class RuntimeUnavailableError(BrainFrogSDKError):
    """Raised when the BrainFrog runtime cannot be initialized or is unavailable."""
    pass
