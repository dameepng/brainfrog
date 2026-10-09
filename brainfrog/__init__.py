"""BrainFrog Public API Surface.

Product Thesis:
    "Don't just trust the agent. Verify it."
"""
from __future__ import annotations

from brainfrog.sdk import (
    ApprovalRequiredError,
    BrainFrogClient,
    BrainFrogSDKError,
    EventType,
    ExecutionFailedError,
    InvalidRequestError,
    PermissionDeniedError,
    ProofArtifact,
    ProofPersistenceError,
    RecoveryRequiredError,
    RuntimeConfig,
    RuntimeUnavailableError,
    SDKEvent,
    TaskCancelledError,
    TaskHandle,
    TaskNotFoundError,
    TaskPlan,
    TaskRequest,
    TaskResult,
    TaskStatus,
    TaskStep,
    VerificationFailedError,
    WorkspaceBusyError,
)

__version__ = "0.1.0"

__all__ = [
    "BrainFrogClient",
    "TaskHandle",
    "RuntimeConfig",
    "TaskRequest",
    "TaskResult",
    "TaskPlan",
    "TaskStep",
    "TaskStatus",
    "ProofArtifact",
    "SDKEvent",
    "EventType",
    "BrainFrogSDKError",
    "InvalidRequestError",
    "PermissionDeniedError",
    "ApprovalRequiredError",
    "WorkspaceBusyError",
    "TaskNotFoundError",
    "TaskCancelledError",
    "ExecutionFailedError",
    "VerificationFailedError",
    "RecoveryRequiredError",
    "ProofPersistenceError",
    "RuntimeUnavailableError",
]
