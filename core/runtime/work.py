"""Work Domain Model and Deterministic State Machine.

Represents a user's requested unit of work from intent formulation through
completion. Work is a descriptive domain state model, NOT an execution engine.
All execution remains exclusively governed by canonical orchestrator.py.
"""
from __future__ import annotations

import math
import secrets
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple, Union

from core.runtime.capabilities import Capabilities
from core.runtime.contract import reject_secrets


class WorkStatus(str, Enum):
    """Deterministic lifecycle statuses for a Work unit."""
    CREATED = "created"
    PLANNING = "planning"
    APPROVAL_REQUIRED = "approval_required"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InvalidWorkTransition(ValueError):
    """Raised when an invalid state transition is attempted on a Work instance."""
    pass


ALLOWED_TRANSITIONS: Dict[WorkStatus, FrozenSet[WorkStatus]] = {
    WorkStatus.CREATED: frozenset({WorkStatus.PLANNING, WorkStatus.FAILED}),
    WorkStatus.PLANNING: frozenset({WorkStatus.APPROVAL_REQUIRED, WorkStatus.FAILED}),
    WorkStatus.APPROVAL_REQUIRED: frozenset({
        WorkStatus.EXECUTING,
        WorkStatus.CANCELLED,
        WorkStatus.FAILED,
    }),
    WorkStatus.EXECUTING: frozenset({WorkStatus.VERIFYING, WorkStatus.FAILED}),
    WorkStatus.VERIFYING: frozenset({WorkStatus.DONE, WorkStatus.FAILED}),
    WorkStatus.DONE: frozenset(),
    WorkStatus.FAILED: frozenset(),
    WorkStatus.CANCELLED: frozenset(),
}

TERMINAL_WORK_STATUSES: FrozenSet[WorkStatus] = frozenset({
    WorkStatus.DONE,
    WorkStatus.FAILED,
    WorkStatus.CANCELLED,
})


@dataclass(frozen=True)
class Work:
    """Immutable domain model describing a requested piece of work.

    Work tracks descriptive lifecycle state only. It does not confer execution
    authority, bypass approval boundaries, or execute code.
    """
    id: str = field(default_factory=lambda: f"work_{secrets.token_hex(16)}")
    intent: str = ""
    goal: str = ""
    scope: Tuple[str, ...] = ()
    plan: Tuple[str, ...] = ()
    capabilities: Optional[Capabilities] = None
    status: WorkStatus = WorkStatus.CREATED
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def __init__(
        self,
        id: Optional[Union[str, WorkStatus]] = None,
        intent: str = "",
        goal: str = "",
        scope: Union[Tuple[str, ...], Sequence[str]] = (),
        plan: Union[Tuple[str, ...], Sequence[str]] = (),
        capabilities: Optional[Capabilities] = None,
        status: Union[WorkStatus, str] = WorkStatus.CREATED,
        created_at: Optional[float] = None,
        updated_at: Optional[float] = None,
    ) -> None:
        actual_id = f"work_{secrets.token_hex(16)}" if id is None else id
        actual_status = status
        if isinstance(id, WorkStatus):
            actual_status = id
            actual_id = f"work_{secrets.token_hex(16)}"

        now = time.time()
        actual_created = now if created_at is None else created_at
        actual_updated = now if updated_at is None else updated_at

        object.__setattr__(self, "id", actual_id)
        object.__setattr__(self, "intent", intent)
        object.__setattr__(self, "goal", goal)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "plan", plan)
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "status", actual_status)
        object.__setattr__(self, "created_at", actual_created)
        object.__setattr__(self, "updated_at", actual_updated)
        self.__post_init__()

    def __post_init__(self) -> None:
        # Support shorthand instantiation Work(WorkStatus.CREATED)
        if isinstance(self.id, WorkStatus):
            object.__setattr__(self, "status", self.id)
            object.__setattr__(self, "id", f"work_{secrets.token_hex(16)}")

        if type(self.id) is not str or not self.id.strip():
            raise ValueError("Work id must be a non-empty string")

        if type(self.intent) is not str:
            raise ValueError("Work intent must be a string")

        if type(self.goal) is not str:
            raise ValueError("Work goal must be a string")

        # Scope validation & normalization
        if not isinstance(self.scope, (tuple, list, set, frozenset)):
            raise ValueError("Work scope must be a sequence of strings")
        for s in self.scope:
            if type(s) is not str:
                raise ValueError("All scope items must be strings")
        object.__setattr__(self, "scope", tuple(self.scope))

        # Plan validation & normalization
        if not isinstance(self.plan, (tuple, list, set, frozenset)):
            raise ValueError("Work plan must be a sequence of strings")
        for p in self.plan:
            if type(p) is not str:
                raise ValueError("All plan items must be strings")
        object.__setattr__(self, "plan", tuple(self.plan))

        # Status validation & normalization
        if isinstance(self.status, str):
            try:
                object.__setattr__(self, "status", WorkStatus(self.status))
            except ValueError as exc:
                raise ValueError(f"Unknown or invalid work status: {self.status}") from exc
        elif not isinstance(self.status, WorkStatus):
            raise ValueError(f"Work status must be a WorkStatus enum, got {type(self.status)}")

        # Capabilities validation
        if self.capabilities is not None and not isinstance(self.capabilities, Capabilities):
            raise ValueError("Work capabilities must be a Capabilities instance or None")

        # Timestamps validation
        if not isinstance(self.created_at, (int, float)) or math.isnan(self.created_at) or math.isinf(self.created_at):
            raise ValueError("created_at must be a valid finite number")
        object.__setattr__(self, "created_at", float(self.created_at))

        if not isinstance(self.updated_at, (int, float)) or math.isnan(self.updated_at) or math.isinf(self.updated_at):
            raise ValueError("updated_at must be a valid finite number")
        object.__setattr__(self, "updated_at", float(self.updated_at))

        # Scrub and reject credentials across all descriptive fields (BF-15B-02)
        reject_secrets({
            "id": self.id,
            "intent": self.intent,
            "goal": self.goal,
            "scope": self.scope,
            "plan": self.plan,
        })

    @property
    def is_terminal(self) -> bool:
        """True if the Work is in a terminal state (DONE, FAILED, CANCELLED)."""
        return self.status in TERMINAL_WORK_STATUSES

    def transition(
        self,
        new_status: Union[WorkStatus, str],
        *,
        plan: Optional[Sequence[str]] = None,
        scope: Optional[Sequence[str]] = None,
        capabilities: Optional[Capabilities] = None,
        updated_at: Optional[float] = None,
    ) -> Work:
        """Deterministically transition Work to a new status.

        Returns a new immutable Work record with the target status and updated_at.
        Raises InvalidWorkTransition if the transition is prohibited by state machine rules.
        """
        try:
            target_status = WorkStatus(new_status) if isinstance(new_status, str) else new_status
            if not isinstance(target_status, WorkStatus):
                raise ValueError(f"Invalid status type: {type(target_status)}")
        except (ValueError, TypeError) as exc:
            raise InvalidWorkTransition(f"Unknown or invalid work status: {new_status}") from exc

        allowed = ALLOWED_TRANSITIONS.get(self.status, frozenset())
        if target_status not in allowed:
            raise InvalidWorkTransition(
                f"Invalid transition from '{self.status.value}' to '{target_status.value}'"
            )

        now = time.time() if updated_at is None else float(updated_at)
        if math.isnan(now) or math.isinf(now):
            raise ValueError("updated_at must be a valid finite number")

        return replace(
            self,
            status=target_status,
            plan=self.plan if plan is None else tuple(str(p) for p in plan),
            scope=self.scope if scope is None else tuple(str(s) for s in scope),
            capabilities=self.capabilities if capabilities is None else capabilities,
            updated_at=now,
        )

    def with_update(
        self,
        *,
        intent: Optional[str] = None,
        goal: Optional[str] = None,
        plan: Optional[Sequence[str]] = None,
        scope: Optional[Sequence[str]] = None,
        capabilities: Optional[Capabilities] = None,
        updated_at: Optional[float] = None,
    ) -> Work:
        """Return an updated copy of non-terminal Work without changing its status."""
        if self.is_terminal:
            raise ValueError(f"Cannot update terminal Work in status '{self.status.value}'")

        now = time.time() if updated_at is None else float(updated_at)
        if math.isnan(now) or math.isinf(now):
            raise ValueError("updated_at must be a valid finite number")

        return replace(
            self,
            intent=self.intent if intent is None else intent,
            goal=self.goal if goal is None else goal,
            plan=self.plan if plan is None else tuple(str(p) for p in plan),
            scope=self.scope if scope is None else tuple(str(s) for s in scope),
            capabilities=self.capabilities if capabilities is None else capabilities,
            updated_at=now,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize state to a JSON-safe dictionary with stable fields and secret checks."""
        data: Dict[str, Any] = {
            "id": self.id,
            "intent": self.intent,
            "goal": self.goal,
            "scope": list(self.scope),
            "plan": list(self.plan),
            "capabilities": self.capabilities.to_dict() if self.capabilities is not None else None,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        reject_secrets(data)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Work:
        """Deserialize a Work record from dictionary, enforcing strict validation."""
        if type(data) is not dict:
            raise ValueError("Expected dictionary for Work.from_dict")

        reject_secrets(data)

        expected_keys = {
            "id", "intent", "goal", "scope", "plan",
            "capabilities", "status", "created_at", "updated_at",
        }
        extra_keys = set(data.keys()) - expected_keys
        if extra_keys:
            raise ValueError(f"Unknown work fields: {sorted(extra_keys)}")

        for req in ("id", "intent", "goal", "status", "created_at", "updated_at"):
            if req not in data:
                raise ValueError(f"Missing required field in Work: '{req}'")

        caps_data = data.get("capabilities")
        capabilities: Optional[Capabilities] = None
        if caps_data is not None:
            if type(caps_data) is not dict:
                raise ValueError("Malformed capabilities in Work")
            capabilities = Capabilities.from_dict(caps_data)

        status_raw = data["status"]
        if type(status_raw) is not str:
            raise ValueError("Work status must be a string")
        try:
            status = WorkStatus(status_raw)
        except ValueError as exc:
            raise ValueError(f"Invalid work status: '{status_raw}'") from exc

        return cls(
            id=data["id"],
            intent=data["intent"],
            goal=data["goal"],
            scope=tuple(data.get("scope") or ()),
            plan=tuple(data.get("plan") or ()),
            capabilities=capabilities,
            status=status,
            created_at=data["created_at"],
            updated_at=data["updated_at"],
        )


class WorkStore(ABC):
    """Abstract interface for storing and retrieving Work records."""

    @abstractmethod
    def create(self, work: Work) -> Work:
        """Store a new Work record. Raises ValueError if work.id already exists."""
        raise NotImplementedError

    @abstractmethod
    def get(self, work_id: str) -> Optional[Work]:
        """Retrieve a Work record by id, or None if not found."""
        raise NotImplementedError

    @abstractmethod
    def save(self, work: Work) -> Work:
        """Update an existing Work record. Raises KeyError if not found."""
        raise NotImplementedError

    @abstractmethod
    def delete(self, work_id: str) -> bool:
        """Delete a Work record by id. Returns True if deleted, False if not found."""
        raise NotImplementedError

    @abstractmethod
    def list_all(self) -> List[Work]:
        """List all Work records currently in the store."""
        raise NotImplementedError


class InMemoryWorkStore(WorkStore):
    """Thread-safe, isolated in-memory WorkStore implementation."""

    def __init__(self) -> None:
        self._records: Dict[str, Work] = {}
        self._lock = threading.RLock()

    def create(self, work: Work) -> Work:
        if not isinstance(work, Work):
            raise TypeError("Expected Work instance")
        with self._lock:
            if work.id in self._records:
                raise ValueError(f"Work with id '{work.id}' already exists")
            self._records[work.id] = work
            return work

    def get(self, work_id: str) -> Optional[Work]:
        if not isinstance(work_id, str):
            raise TypeError("work_id must be a string")
        with self._lock:
            return self._records.get(work_id)

    def save(self, work: Work) -> Work:
        if not isinstance(work, Work):
            raise TypeError("Expected Work instance")
        with self._lock:
            if work.id not in self._records:
                raise KeyError(f"Work with id '{work.id}' does not exist")
            existing = self._records[work.id]
            if existing.status != work.status:
                allowed = ALLOWED_TRANSITIONS.get(existing.status, frozenset())
                if work.status not in allowed:
                    raise InvalidWorkTransition(
                        f"Cannot transition Work in store from '{existing.status.value}' to '{work.status.value}'"
                    )
            elif existing.is_terminal and existing != work:
                raise InvalidWorkTransition(
                    f"Cannot modify terminal Work record in status '{existing.status.value}'"
                )
            self._records[work.id] = work
            return work

    def delete(self, work_id: str) -> bool:
        if not isinstance(work_id, str):
            raise TypeError("work_id must be a string")
        with self._lock:
            if work_id in self._records:
                del self._records[work_id]
                return True
            return False

    def list_all(self) -> List[Work]:
        with self._lock:
            return list(self._records.values())

