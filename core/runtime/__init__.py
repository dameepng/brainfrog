"""BrainFrog Agent Runtime Package."""
from __future__ import annotations

from .messages import AgentEvent, IncomingMessage, OutgoingMessage
from .permissions import (
    ChannelTrustLevel,
    PermissionAction,
    PermissionPolicy,
    REMOTE_PROHIBITED_ACTIONS,
    evaluate_channel_action,
    get_default_policy,
    resolve_channel_trust_level,
)
from .approval import (
    ApprovalRequest,
    ApprovalService,
    ApprovalStatus,
    CanonicalOperation,
    FileApprovalStore,
    InMemoryApprovalStore,
    RiskClass,
)
from .contract import ApprovedExecutionContract
from .runtime import BrainFrogRuntime
from .session import SessionManager, SessionState, session_manager

__all__ = [
    "AgentEvent",
    "IncomingMessage",
    "OutgoingMessage",
    "ChannelTrustLevel",
    "PermissionAction",
    "PermissionPolicy",
    "REMOTE_PROHIBITED_ACTIONS",
    "evaluate_channel_action",
    "get_default_policy",
    "resolve_channel_trust_level",
    "BrainFrogRuntime",
    "SessionState",
    "SessionManager",
    "session_manager",
    "ApprovalRequest",
    "ApprovalService",
    "ApprovalStatus",
    "CanonicalOperation",
    "FileApprovalStore",
    "InMemoryApprovalStore",
    "RiskClass",
    "ApprovedExecutionContract",
]
