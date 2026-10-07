"""Subagent Domain Model and Deterministic Worker Lifecycle.

P1.3A Scoped Subagent Domain Representation.

Architectural Principles:
- A subagent is a WORKER, not an authority.
- A subagent holds NO execution authority, approval authority, or filesystem permissions.
- A subagent cannot spawn orchestrators, mutate transactions, or bypass capability contracts.
- A subagent is permanently bound to exactly one parent Work, session, and authenticated actor.
- canonical orchestrator.py remains the SOLE execution engine in BrainFrog.
"""
from __future__ import annotations

import math
import re
import secrets
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple, Union

from core.runtime.contract import reject_secrets
from core.runtime.secret_scrubbing import scrub_secrets

CURRENT_SUBAGENT_SCHEMA_VERSION = 1

# Bounded limits (consistent with BrainFrog Work domain conventions)
MAX_SUBAGENT_ID_CHARS = 128
MAX_SUBAGENT_ROLE_CHARS = 256
MAX_SUBAGENT_PURPOSE_CHARS = 4096
MAX_SUBAGENT_FAILURE_CHARS = 2048

_INVALID_ID_CHARS = re.compile(r"[\x00-\x1f\x7f/\\:*\?\"<>\|]|(?:\.\.)")


class SubagentStatus(str, Enum):
    """Deterministic lifecycle statuses for a Subagent worker."""
    CREATED = "created"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InvalidSubagentTransition(ValueError):
    """Raised when an invalid state transition is attempted on a Subagent."""
    pass


class SubagentValidationError(ValueError):
    """Raised when Subagent construction or validation fails."""
    pass


ALLOWED_SUBAGENT_TRANSITIONS: Dict[SubagentStatus, FrozenSet[SubagentStatus]] = {
    SubagentStatus.CREATED: frozenset({
        SubagentStatus.READY,
        SubagentStatus.FAILED,
        SubagentStatus.CANCELLED,
    }),
    SubagentStatus.READY: frozenset({
        SubagentStatus.RUNNING,
        SubagentStatus.FAILED,
        SubagentStatus.CANCELLED,
    }),
    SubagentStatus.RUNNING: frozenset({
        SubagentStatus.COMPLETED,
        SubagentStatus.FAILED,
        SubagentStatus.CANCELLED,
    }),
    SubagentStatus.COMPLETED: frozenset(),
    SubagentStatus.FAILED: frozenset(),
    SubagentStatus.CANCELLED: frozenset(),
}

TERMINAL_SUBAGENT_STATUSES: FrozenSet[SubagentStatus] = frozenset({
    SubagentStatus.COMPLETED,
    SubagentStatus.FAILED,
    SubagentStatus.CANCELLED,
})

ACTIVE_SUBAGENT_STATUSES: FrozenSet[SubagentStatus] = frozenset({
    SubagentStatus.CREATED,
    SubagentStatus.READY,
    SubagentStatus.RUNNING,
})


def validate_subagent_id(subagent_id: str) -> str:
    """Validate Subagent ID to prevent path traversal, drive letters, and malformed identifiers."""
    if type(subagent_id) is not str:
        raise SubagentValidationError(f"Subagent ID must be a string, got {type(subagent_id)}")
    clean = subagent_id.strip()
    if not clean or len(clean) > MAX_SUBAGENT_ID_CHARS:
        raise SubagentValidationError(
            f"Subagent ID must be a non-empty string with length <= {MAX_SUBAGENT_ID_CHARS}"
        )
    if _INVALID_ID_CHARS.search(clean) or clean.startswith((".", "~", "/", "\\")):
        raise SubagentValidationError(
            f"Invalid characters or traversal detected in Subagent ID: '{clean}'"
        )
    return clean


_INVALID_SESSION_ID_CHARS = re.compile(r"[\x00-\x1f\x7f/\\*\?\"<>\|]|(?:\.\.)")


def _validate_binding_id(value: Any, field_name: str) -> str:
    """Validate required binding identifiers (parent_work_id, session_id, session_incarnation_id, actor)."""
    if type(value) is not str:
        raise SubagentValidationError(f"{field_name} must be a string, got {type(value)}")
    clean = value.strip()
    if not clean:
        raise SubagentValidationError(f"{field_name} must be a non-empty string")
    regex = _INVALID_SESSION_ID_CHARS if field_name == "session_id" else _INVALID_ID_CHARS
    if regex.search(clean) or clean.startswith((".", "~", "/", "\\")):
        raise SubagentValidationError(
            f"Invalid characters or traversal detected in {field_name}: '{clean}'"
        )
    return clean


@dataclass(frozen=True)
class Subagent:
    """Immutable domain model representing a scoped worker subordinate to a parent Work.

    A Subagent is a worker, NOT an authority:
    - It holds NO execution authority, approval authority, or filesystem permissions.
    - It cannot spawn orchestrators, mutate transactions, or bypass capability contracts.
    - It is permanently bound to a single parent Work, session, and authenticated actor.
    """
    subagent_id: str = field(default_factory=lambda: f"sub_{secrets.token_hex(16)}")
    parent_work_id: str = ""
    session_id: str = ""
    session_incarnation_id: str = ""
    actor: str = ""
    status: SubagentStatus = SubagentStatus.CREATED
    role: str = ""
    purpose: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    schema_version: int = CURRENT_SUBAGENT_SCHEMA_VERSION
    delegation_id: Optional[str] = None
    failure_reason: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    def __init__(
        self,
        subagent_id: Optional[str] = None,
        parent_work_id: str = "",
        session_id: str = "",
        session_incarnation_id: str = "",
        actor: Optional[str] = None,
        status: Union[SubagentStatus, str] = SubagentStatus.CREATED,
        role: str = "",
        purpose: str = "",
        created_at: Optional[float] = None,
        updated_at: Optional[float] = None,
        schema_version: int = CURRENT_SUBAGENT_SCHEMA_VERSION,
        delegation_id: Optional[str] = None,
        failure_reason: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        # Accepted aliases for BrainFrog consistency
        id: Optional[str] = None,
        actor_id: Optional[str] = None,
    ) -> None:
        actual_id = subagent_id if subagent_id is not None else id
        if actual_id is None:
            actual_id = f"sub_{secrets.token_hex(16)}"

        actual_actor = actor if actor is not None else actor_id
        if actual_actor is None:
            actual_actor = ""

        actual_role = role
        actual_purpose = purpose
        if actual_role and not actual_purpose:
            actual_purpose = actual_role
        elif actual_purpose and not actual_role:
            actual_role = actual_purpose

        now = time.time()
        actual_created = now if created_at is None else created_at
        actual_updated = now if updated_at is None else updated_at

        object.__setattr__(self, "subagent_id", actual_id)
        object.__setattr__(self, "parent_work_id", parent_work_id)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "session_incarnation_id", session_incarnation_id)
        object.__setattr__(self, "actor", actual_actor)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "role", actual_role)
        object.__setattr__(self, "purpose", actual_purpose)
        object.__setattr__(self, "created_at", actual_created)
        object.__setattr__(self, "updated_at", actual_updated)
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "delegation_id", delegation_id)
        object.__setattr__(self, "failure_reason", failure_reason)
        object.__setattr__(self, "metadata", dict(metadata) if metadata else None)
        self.__post_init__()

    def __post_init__(self) -> None:
        # Validate subagent ID
        validated_id = validate_subagent_id(self.subagent_id)
        object.__setattr__(self, "subagent_id", validated_id)

        # Validate mandatory parent Work binding
        clean_parent = _validate_binding_id(self.parent_work_id, "parent_work_id")
        object.__setattr__(self, "parent_work_id", clean_parent)

        # Validate mandatory Session binding
        clean_session = _validate_binding_id(self.session_id, "session_id")
        object.__setattr__(self, "session_id", clean_session)

        # Validate mandatory Session Incarnation binding
        clean_inc = _validate_binding_id(self.session_incarnation_id, "session_incarnation_id")
        object.__setattr__(self, "session_incarnation_id", clean_inc)

        # Validate mandatory Actor binding
        clean_actor = _validate_binding_id(self.actor, "actor")
        object.__setattr__(self, "actor", clean_actor)

        # Validate Status
        if isinstance(self.status, str):
            try:
                object.__setattr__(self, "status", SubagentStatus(self.status))
            except ValueError as exc:
                raise SubagentValidationError(f"Unknown or invalid subagent status: {self.status}") from exc
        elif not isinstance(self.status, SubagentStatus):
            raise SubagentValidationError(f"Subagent status must be a SubagentStatus enum, got {type(self.status)}")

        # Validate Timestamps
        if not isinstance(self.created_at, (int, float)) or math.isnan(self.created_at) or math.isinf(self.created_at):
            raise SubagentValidationError("created_at must be a valid finite number")
        object.__setattr__(self, "created_at", float(self.created_at))

        if not isinstance(self.updated_at, (int, float)) or math.isnan(self.updated_at) or math.isinf(self.updated_at):
            raise SubagentValidationError("updated_at must be a valid finite number")
        object.__setattr__(self, "updated_at", float(self.updated_at))

        # Schema version
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise SubagentValidationError("schema_version must be a positive integer")

        # Bounds checks
        if type(self.role) is not str:
            raise SubagentValidationError("role must be a string")
        if len(self.role) > MAX_SUBAGENT_ROLE_CHARS:
            object.__setattr__(self, "role", self.role[:MAX_SUBAGENT_ROLE_CHARS])

        if type(self.purpose) is not str:
            raise SubagentValidationError("purpose must be a string")
        if len(self.purpose) > MAX_SUBAGENT_PURPOSE_CHARS:
            object.__setattr__(self, "purpose", self.purpose[:MAX_SUBAGENT_PURPOSE_CHARS])

        if self.delegation_id is not None:
            if type(self.delegation_id) is not str or not self.delegation_id.strip():
                raise SubagentValidationError("delegation_id must be a non-empty string when provided")
            clean_del = self.delegation_id.strip()
            if _INVALID_ID_CHARS.search(clean_del):
                raise SubagentValidationError(f"Invalid characters in delegation_id: '{clean_del}'")
            object.__setattr__(self, "delegation_id", clean_del)

        if self.failure_reason is not None:
            if type(self.failure_reason) is not str:
                raise SubagentValidationError("failure_reason must be a string when provided")
            object.__setattr__(self, "failure_reason", self.failure_reason[:MAX_SUBAGENT_FAILURE_CHARS])

        # Strictly reject credentials across all descriptive fields (Property 10 invariant)
        reject_secrets({
            "subagent_id": self.subagent_id,
            "parent_work_id": self.parent_work_id,
            "session_id": self.session_id,
            "session_incarnation_id": self.session_incarnation_id,
            "actor": self.actor,
            "role": self.role,
            "purpose": self.purpose,
            "delegation_id": self.delegation_id,
            "failure_reason": self.failure_reason,
            "metadata": self.metadata or {},
        })

    @property
    def id(self) -> str:
        """Alias for subagent_id for BrainFrog ID consistency."""
        return self.subagent_id

    @property
    def actor_id(self) -> str:
        """Alias for actor for BrainFrog actor consistency."""
        return self.actor

    @property
    def is_terminal(self) -> bool:
        """True if the Subagent is in a terminal state (COMPLETED, FAILED, CANCELLED)."""
        return self.status in TERMINAL_SUBAGENT_STATUSES

    @property
    def is_active(self) -> bool:
        """True if the Subagent is in an active non-terminal state (CREATED, READY, RUNNING)."""
        return self.status in ACTIVE_SUBAGENT_STATUSES

    def transition(
        self,
        new_status: Union[SubagentStatus, str],
        *,
        failure_reason: Optional[str] = None,
        updated_at: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
        delegation_id: Optional[str] = None,
    ) -> Subagent:
        """Deterministically transition Subagent to a new status.

        Returns a new immutable Subagent record with the target status and updated_at.
        Raises InvalidSubagentTransition if the transition is prohibited by state machine rules.
        Terminal states (COMPLETED, FAILED, CANCELLED) cannot transition back into active states.
        """
        try:
            target_status = SubagentStatus(new_status) if isinstance(new_status, str) else new_status
            if not isinstance(target_status, SubagentStatus):
                raise SubagentValidationError(f"Invalid status type: {type(target_status)}")
        except (ValueError, TypeError) as exc:
            raise InvalidSubagentTransition(f"Unknown or invalid subagent status: {new_status}") from exc

        allowed = ALLOWED_SUBAGENT_TRANSITIONS.get(self.status, frozenset())
        if target_status not in allowed:
            raise InvalidSubagentTransition(
                f"Invalid transition from '{self.status.value}' to '{target_status.value}'"
            )

        now = time.time() if updated_at is None else float(updated_at)
        if math.isnan(now) or math.isinf(now):
            raise SubagentValidationError("updated_at must be a valid finite number")

        actual_failure = self.failure_reason if failure_reason is None else failure_reason
        actual_delegation = self.delegation_id if delegation_id is None else delegation_id
        actual_metadata = self.metadata if metadata is None else dict(metadata)

        return replace(
            self,
            status=target_status,
            updated_at=now,
            failure_reason=actual_failure,
            delegation_id=actual_delegation,
            metadata=actual_metadata,
        )

    transition_to = transition

    def with_update(
        self,
        *,
        role: Optional[str] = None,
        purpose: Optional[str] = None,
        failure_reason: Optional[str] = None,
        delegation_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        updated_at: Optional[float] = None,
    ) -> Subagent:
        """Return an updated copy of non-terminal Subagent without changing its status."""
        if self.is_terminal:
            raise ValueError(f"Cannot update terminal Subagent in status '{self.status.value}'")

        now = time.time() if updated_at is None else float(updated_at)
        if math.isnan(now) or math.isinf(now):
            raise SubagentValidationError("updated_at must be a valid finite number")

        return replace(
            self,
            role=self.role if role is None else role,
            purpose=self.purpose if purpose is None else purpose,
            failure_reason=self.failure_reason if failure_reason is None else failure_reason,
            delegation_id=self.delegation_id if delegation_id is None else delegation_id,
            metadata=self.metadata if metadata is None else dict(metadata),
            updated_at=now,
        )

    def fail(self, reason: str, *, updated_at: Optional[float] = None) -> Subagent:
        """Deterministically transition Subagent to FAILED status."""
        return self.transition(SubagentStatus.FAILED, failure_reason=reason, updated_at=updated_at)

    def cancel(self, reason: str = "", *, updated_at: Optional[float] = None) -> Subagent:
        """Deterministically transition Subagent to CANCELLED status."""
        return self.transition(SubagentStatus.CANCELLED, failure_reason=reason, updated_at=updated_at)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize Subagent to dictionary, strictly enforcing secret scrubbing."""
        data: Dict[str, Any] = {
            "subagent_id": self.subagent_id,
            "parent_work_id": self.parent_work_id,
            "session_id": self.session_id,
            "session_incarnation_id": self.session_incarnation_id,
            "actor": self.actor,
            "status": self.status.value,
            "role": self.role,
            "purpose": self.purpose,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "schema_version": self.schema_version,
            "delegation_id": self.delegation_id,
            "failure_reason": self.failure_reason,
            "metadata": dict(self.metadata) if self.metadata else None,
        }
        reject_secrets(data)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Subagent:
        """Deserialize a Subagent record from dictionary, enforcing strict validation."""
        if type(data) is not dict:
            raise SubagentValidationError("Expected dictionary for Subagent.from_dict")

        reject_secrets(data)

        expected_keys = {
            "subagent_id", "id", "parent_work_id", "session_id",
            "session_incarnation_id", "actor", "actor_id", "status",
            "role", "purpose", "created_at", "updated_at",
            "schema_version", "delegation_id", "failure_reason", "metadata",
        }
        extra_keys = set(data.keys()) - expected_keys
        if extra_keys:
            raise SubagentValidationError(f"Unknown subagent fields: {sorted(extra_keys)}")

        for req in ("parent_work_id", "session_id", "session_incarnation_id", "status", "created_at", "updated_at"):
            if req not in data or data[req] is None:
                raise SubagentValidationError(f"Missing required field in Subagent: '{req}'")

        if ("actor" not in data or data["actor"] is None) and ("actor_id" not in data or data["actor_id"] is None):
            raise SubagentValidationError("Missing required field in Subagent: 'actor'")

        return cls(
            subagent_id=data.get("subagent_id") or data.get("id"),
            parent_work_id=data["parent_work_id"],
            session_id=data["session_id"],
            session_incarnation_id=data["session_incarnation_id"],
            actor=data.get("actor") or data.get("actor_id"),
            status=data["status"],
            role=data.get("role", ""),
            purpose=data.get("purpose", ""),
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            schema_version=data.get("schema_version", CURRENT_SUBAGENT_SCHEMA_VERSION),
            delegation_id=data.get("delegation_id"),
            failure_reason=data.get("failure_reason"),
            metadata=data.get("metadata"),
        )


__all__ = [
    "ACTIVE_SUBAGENT_STATUSES",
    "ALLOWED_SUBAGENT_TRANSITIONS",
    "CURRENT_SUBAGENT_SCHEMA_VERSION",
    "InvalidSubagentTransition",
    "MAX_SUBAGENT_FAILURE_CHARS",
    "MAX_SUBAGENT_ID_CHARS",
    "MAX_SUBAGENT_PURPOSE_CHARS",
    "MAX_SUBAGENT_ROLE_CHARS",
    "Subagent",
    "SubagentStatus",
    "SubagentValidationError",
    "TERMINAL_SUBAGENT_STATUSES",
    "validate_subagent_id",
]
