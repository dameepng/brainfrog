"""BrainFrog SDK Package.

Product Thesis:
    "Don't just trust the agent. Verify it."
"""
from __future__ import annotations

from brainfrog.sdk.client import BrainFrogClient, TaskHandle
from brainfrog.sdk.errors import (
    ApprovalRequiredError,
    BrainFrogSDKError,
    ExecutionFailedError,
    InvalidRequestError,
    PermissionDeniedError,
    ProofPersistenceError,
    RecoveryRequiredError,
    RuntimeUnavailableError,
    TaskCancelledError,
    TaskNotFoundError,
    VerificationFailedError,
    WorkspaceBusyError,
)
from brainfrog.sdk.events import (
    EventBroker,
    EventType,
    SDKEvent,
)
from brainfrog.sdk.models import (
    ProofArtifact,
    RuntimeConfig,
    TaskPlan,
    TaskRequest,
    TaskResult,
    TaskStatus,
    TaskStep,
)

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
    "EventBroker",
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
