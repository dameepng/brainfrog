"""BrainFrog Phase 5 — SDK Typed Models.

Provides public request, result, status, and configuration models.
Reuses existing core domain models to preserve single-truth invariants.

Product Thesis:
    "Don't just trust the agent. Verify it."
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from core.runtime.proof import ProofArtifact
from core.runtime.workflow import (
    TaskPlan,
    TaskRequest,
    TaskResult,
    TaskStep,
)


class TaskStatus(str, Enum):
    """Authoritative task status classification."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMMITTED = "COMMITTED"
    ROLLED_BACK = "ROLLED_BACK"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    LOCKED = "LOCKED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


@dataclass
class RuntimeConfig:
    """Typed configuration surface for the BrainFrog SDK runtime client.

    Exposes only safe, real runtime configuration options.
    Does NOT expose security bypasses (e.g. disable_verification, trust_everything).
    """

    workspace: Union[str, Path]
    channel: str = "cli"
    user_id: str = "local"
    conversation_id: Optional[str] = None
    backend: str = "jev"
    provider: Optional[str] = None
    model: Optional[str] = None
    test_command: Optional[Sequence[str]] = None
    timeout_seconds: Optional[float] = None
    max_retries: int = 2
    require_approval: bool = False
    two_man_rule_enabled: bool = True
    auto_recover_transactions: bool = True
    persist_sessions: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.workspace:
            raise ValueError("RuntimeConfig requires a non-empty workspace path.")
        resolved = Path(self.workspace).resolve()
        self.workspace = resolved
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative.")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive if specified.")
        if not self.channel or not self.channel.strip():
            raise ValueError("channel must be a non-empty string.")
        if not self.user_id or not self.user_id.strip():
            raise ValueError("user_id must be a non-empty string.")


__all__ = [
    "TaskStatus",
    "RuntimeConfig",
    "TaskRequest",
    "TaskResult",
    "TaskPlan",
    "TaskStep",
    "ProofArtifact",
]
