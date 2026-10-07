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
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple, Union, TYPE_CHECKING

from core.runtime.capabilities import Capabilities
from core.runtime.contract import reject_secrets
from core.runtime.secret_scrubbing import scrub_secrets

CURRENT_WORK_SCHEMA_VERSION = 1

# Bounded limits (Section 34)
MAX_WORK_TITLE_CHARS = 256
MAX_WORK_GOAL_CHARS = 4096
MAX_WORK_INTENT_CHARS = 16384
MAX_WORK_SUMMARY_CHARS = 4096
MAX_WORK_FAILURE_CHARS = 2048
MAX_WORK_DETAILS_CHARS = 4096
MAX_ACTIVE_WORKS_PER_ACTOR = 20
MAX_GLOBAL_WORKS = 500
MAX_WORK_BYTES = 256 * 1024  # 256 KB
MAX_WORKS_PER_RESPONSE = 20
DEFAULT_MAX_TERMINAL_RETENTION = 50


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


class VerificationStatus(str, Enum):
    """Verification outcome states."""
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"


class InvalidWorkTransition(ValueError):
    """Raised when an invalid state transition is attempted on a Work instance."""
    pass


class StaleWorkRevisionError(RuntimeError):
    """Raised when an optimistic concurrency control revision conflict occurs."""
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

ACTIVE_WORK_STATUSES: FrozenSet[WorkStatus] = frozenset({
    WorkStatus.CREATED,
    WorkStatus.PLANNING,
    WorkStatus.APPROVAL_REQUIRED,
    WorkStatus.EXECUTING,
    WorkStatus.VERIFYING,
})


@dataclass(frozen=True)
class VerificationResult:
    """First-class verification result model for Work completion."""
    status: Union[VerificationStatus, str] = VerificationStatus.PASS
    checks: Tuple[str, ...] = ()
    passed: int = 0
    failed: int = 0
    warnings: int = 0
    timestamp: float = field(default_factory=time.time)
    summary: str = ""

    def __post_init__(self) -> None:
        status_val = self.status.value if isinstance(self.status, VerificationStatus) else self.status.upper()
        if status_val not in ("PASS", "FAIL", "WARN"):
            raise ValueError(f"Invalid verification status: {self.status}")
        object.__setattr__(self, "status", status_val)

        if not isinstance(self.checks, (tuple, list)):
            raise ValueError("checks must be a sequence of strings")
        clean_checks = tuple(c[:MAX_WORK_SUMMARY_CHARS] for c in self.checks)
        object.__setattr__(self, "checks", clean_checks)

        if not isinstance(self.passed, int) or self.passed < 0:
            raise ValueError("passed must be a non-negative integer")
        if not isinstance(self.failed, int) or self.failed < 0:
            raise ValueError("failed must be a non-negative integer")
        if not isinstance(self.warnings, int) or self.warnings < 0:
            raise ValueError("warnings must be a non-negative integer")

        if not isinstance(self.timestamp, (int, float)) or math.isnan(self.timestamp) or math.isinf(self.timestamp):
            raise ValueError("timestamp must be a valid finite number")
        object.__setattr__(self, "timestamp", float(self.timestamp))

        clean_summary = self.summary[:MAX_WORK_SUMMARY_CHARS]
        object.__setattr__(self, "summary", clean_summary)

        reject_secrets({
            "status": self.status,
            "checks": self.checks,
            "summary": self.summary,
        })

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "status": self.status if isinstance(self.status, str) else self.status.value,
            "checks": list(self.checks),
            "passed": self.passed,
            "failed": self.failed,
            "warnings": self.warnings,
            "timestamp": self.timestamp,
            "summary": self.summary,
        }
        reject_secrets(data)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> VerificationResult:
        if type(data) is not dict:
            raise ValueError("Expected dictionary for VerificationResult.from_dict")
        reject_secrets(data)
        return cls(
            status=data.get("status", "PASS"),
            checks=tuple(data.get("checks") or ()),
            passed=int(data.get("passed", 0)),
            failed=int(data.get("failed", 0)),
            warnings=int(data.get("warnings", 0)),
            timestamp=float(data.get("timestamp", time.time())),
            summary=str(data.get("summary", "")),
        )


@dataclass(frozen=True)
class WorkFailure:
    """Structured failure representation for a Work unit."""
    code: str = "EXECUTION_ERROR"
    stage: str = "execution"
    summary: str = ""
    retryable: bool = False
    timestamp: float = field(default_factory=time.time)
    details: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        if type(self.code) is not str or not self.code.strip():
            raise ValueError("failure code must be a non-empty string")
        if type(self.stage) is not str:
            raise ValueError("failure stage must be a string")
        if type(self.summary) is not str:
            raise ValueError("failure summary must be a string")
        if not isinstance(self.retryable, bool):
            raise ValueError("retryable must be a boolean")

        if not isinstance(self.timestamp, (int, float)) or math.isnan(self.timestamp) or math.isinf(self.timestamp):
            raise ValueError("timestamp must be a valid finite number")
        object.__setattr__(self, "timestamp", float(self.timestamp))

        clean_code = self.code.strip()[:64]
        clean_stage = self.stage.strip()[:64]
        clean_summary = self.summary[:MAX_WORK_FAILURE_CHARS]
        object.__setattr__(self, "code", clean_code)
        object.__setattr__(self, "stage", clean_stage)
        object.__setattr__(self, "summary", clean_summary)

        reject_secrets({
            "code": self.code,
            "stage": self.stage,
            "summary": self.summary,
            "details": self.details or {},
        })

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "code": self.code,
            "stage": self.stage,
            "summary": self.summary,
            "retryable": self.retryable,
            "timestamp": self.timestamp,
            "details": self.details,
        }
        reject_secrets(data)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> WorkFailure:
        if type(data) is not dict:
            raise ValueError("Expected dictionary for WorkFailure.from_dict")
        reject_secrets(data)
        return cls(
            code=data.get("code", "EXECUTION_ERROR"),
            stage=data.get("stage", "execution"),
            summary=data.get("summary", ""),
            retryable=bool(data.get("retryable", False)),
            timestamp=float(data.get("timestamp", time.time())),
            details=data.get("details"),
        )


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

    # Phase 15H Persistent Work Schema Fields
    schema_version: int = CURRENT_WORK_SCHEMA_VERSION
    actor_id: Optional[str] = None
    channel: Optional[str] = None
    session_id: Optional[str] = None
    session_incarnation_id: Optional[str] = None
    title: str = ""
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    current_step_id: Optional[str] = None
    plan_id: Optional[str] = None
    approval_request_id: Optional[str] = None
    transaction_id: Optional[str] = None
    verification_result: Optional[VerificationResult] = None
    failure: Optional[WorkFailure] = None
    cancellation_reason: Optional[str] = None
    resume_metadata: Optional[Dict[str, Any]] = None
    revision: int = 1

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
        # Extended fields (Phase 15H)
        schema_version: int = CURRENT_WORK_SCHEMA_VERSION,
        actor_id: Optional[str] = None,
        channel: Optional[str] = None,
        session_id: Optional[str] = None,
        session_incarnation_id: Optional[str] = None,
        title: str = "",
        started_at: Optional[float] = None,
        completed_at: Optional[float] = None,
        current_step_id: Optional[str] = None,
        plan_id: Optional[str] = None,
        approval_request_id: Optional[str] = None,
        transaction_id: Optional[str] = None,
        verification_result: Optional[VerificationResult] = None,
        failure: Optional[WorkFailure] = None,
        cancellation_reason: Optional[str] = None,
        resume_metadata: Optional[Dict[str, Any]] = None,
        revision: int = 1,
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
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "actor_id", actor_id)
        object.__setattr__(self, "channel", channel)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "session_incarnation_id", session_incarnation_id)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "completed_at", completed_at)
        object.__setattr__(self, "current_step_id", current_step_id)
        object.__setattr__(self, "plan_id", plan_id)
        object.__setattr__(self, "approval_request_id", approval_request_id)
        object.__setattr__(self, "transaction_id", transaction_id)
        object.__setattr__(self, "verification_result", verification_result)
        object.__setattr__(self, "failure", failure)
        object.__setattr__(self, "cancellation_reason", cancellation_reason)
        object.__setattr__(self, "resume_metadata", resume_metadata)
        object.__setattr__(self, "revision", revision)
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

        if self.started_at is not None:
            if not isinstance(self.started_at, (int, float)) or math.isnan(self.started_at) or math.isinf(self.started_at):
                raise ValueError("started_at must be a valid finite number")
            object.__setattr__(self, "started_at", float(self.started_at))

        if self.completed_at is not None:
            if not isinstance(self.completed_at, (int, float)) or math.isnan(self.completed_at) or math.isinf(self.completed_at):
                raise ValueError("completed_at must be a valid finite number")
            object.__setattr__(self, "completed_at", float(self.completed_at))

        # Schema version validation
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise ValueError("schema_version must be a positive integer")

        # Revision validation
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("revision must be a positive integer")

        # Verification result validation
        if self.verification_result is not None and not isinstance(self.verification_result, VerificationResult):
            raise ValueError("verification_result must be a VerificationResult instance or None")

        # Failure validation
        if self.failure is not None and not isinstance(self.failure, WorkFailure):
            raise ValueError("failure must be a WorkFailure instance or None")

        # String bounds
        if len(self.title) > MAX_WORK_TITLE_CHARS:
            object.__setattr__(self, "title", self.title[:MAX_WORK_TITLE_CHARS])
        if len(self.goal) > MAX_WORK_GOAL_CHARS:
            object.__setattr__(self, "goal", self.goal[:MAX_WORK_GOAL_CHARS])
        if len(self.intent) > MAX_WORK_INTENT_CHARS:
            object.__setattr__(self, "intent", self.intent[:MAX_WORK_INTENT_CHARS])

        # Scrub and reject credentials across all descriptive fields (BF-15B-02)
        reject_secrets({
            "id": self.id,
            "intent": self.intent,
            "goal": self.goal,
            "scope": self.scope,
            "plan": self.plan,
            "title": self.title,
            "actor_id": self.actor_id or "",
            "cancellation_reason": self.cancellation_reason or "",
            "resume_metadata": self.resume_metadata or {},
        })

    @property
    def work_id(self) -> str:
        """Canonical work identifier alias."""
        return self.id

    @property
    def is_terminal(self) -> bool:
        """True if the Work is in a terminal state (DONE, FAILED, CANCELLED)."""
        return self.status in TERMINAL_WORK_STATUSES

    @property
    def is_resumable(self) -> bool:
        """True if the Work can be resumed from its current persisted state."""
        if self.status == WorkStatus.DONE:
            return False
        if self.status == WorkStatus.CANCELLED:
            return False
        if self.status in (WorkStatus.PLANNING, WorkStatus.APPROVAL_REQUIRED, WorkStatus.EXECUTING, WorkStatus.VERIFYING):
            return True
        if self.status == WorkStatus.FAILED:
            return self.failure is not None and self.failure.retryable
        return False

    @property
    def child_work_ids(self) -> Tuple[str, ...]:
        """Read-only observational references to delegated child works."""
        if not self.resume_metadata:
            return ()
        child_meta = self.resume_metadata.get("child_work_ids") or ()
        if isinstance(child_meta, (list, tuple)):
            return tuple(str(x) for x in child_meta)
        return ()

    @property
    def is_child_work(self) -> bool:
        """True if this work record represents a delegated child work."""
        if not self.resume_metadata:
            return False
        return "child_work" in self.resume_metadata

    @property
    def parent_work_id(self) -> Optional[str]:
        """Parent work ID if this is a delegated child work, else None."""
        if not self.resume_metadata:
            return None
        child_data = self.resume_metadata.get("child_work")
        if isinstance(child_data, dict):
            return child_data.get("parent_work_id")
        return None

    @property
    def delegation_group_ids(self) -> Tuple[str, ...]:
        """Read-only observational references to delegation groups managed by this parent Work."""
        if not self.resume_metadata:
            return ()
        raw = self.resume_metadata.get("delegation_groups") or {}
        if isinstance(raw, dict):
            return tuple(str(k) for k in raw.keys())
        return ()

    def transition(
        self,
        new_status: Union[WorkStatus, str],
        *,
        plan: Optional[Sequence[str]] = None,
        scope: Optional[Sequence[str]] = None,
        capabilities: Optional[Capabilities] = None,
        updated_at: Optional[float] = None,
        current_step_id: Optional[str] = None,
        plan_id: Optional[str] = None,
        approval_request_id: Optional[str] = None,
        transaction_id: Optional[str] = None,
        verification_result: Optional[VerificationResult] = None,
        failure: Optional[WorkFailure] = None,
        started_at: Optional[float] = None,
        completed_at: Optional[float] = None,
        cancellation_reason: Optional[str] = None,
        resume_metadata: Optional[Dict[str, Any]] = None,
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

        actual_started = self.started_at if started_at is None else started_at
        if target_status == WorkStatus.EXECUTING and actual_started is None:
            actual_started = now

        actual_completed = self.completed_at if completed_at is None else completed_at
        if target_status in TERMINAL_WORK_STATUSES and actual_completed is None:
            actual_completed = now

        return replace(
            self,
            status=target_status,
            plan=self.plan if plan is None else tuple(plan),
            scope=self.scope if scope is None else tuple(scope),
            capabilities=self.capabilities if capabilities is None else capabilities,
            updated_at=now,
            started_at=actual_started,
            completed_at=actual_completed,
            current_step_id=self.current_step_id if current_step_id is None else current_step_id,
            plan_id=self.plan_id if plan_id is None else plan_id,
            approval_request_id=self.approval_request_id if approval_request_id is None else approval_request_id,
            transaction_id=self.transaction_id if transaction_id is None else transaction_id,
            verification_result=self.verification_result if verification_result is None else verification_result,
            failure=self.failure if failure is None else failure,
            cancellation_reason=self.cancellation_reason if cancellation_reason is None else cancellation_reason,
            resume_metadata=self.resume_metadata if resume_metadata is None else resume_metadata,
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
        actor_id: Optional[str] = None,
        channel: Optional[str] = None,
        session_id: Optional[str] = None,
        session_incarnation_id: Optional[str] = None,
        title: Optional[str] = None,
        current_step_id: Optional[str] = None,
        plan_id: Optional[str] = None,
        approval_request_id: Optional[str] = None,
        transaction_id: Optional[str] = None,
        verification_result: Optional[VerificationResult] = None,
        failure: Optional[WorkFailure] = None,
        cancellation_reason: Optional[str] = None,
        resume_metadata: Optional[Dict[str, Any]] = None,
        revision: Optional[int] = None,
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
            plan=self.plan if plan is None else tuple(plan),
            scope=self.scope if scope is None else tuple(scope),
            capabilities=self.capabilities if capabilities is None else capabilities,
            updated_at=now,
            actor_id=self.actor_id if actor_id is None else actor_id,
            channel=self.channel if channel is None else channel,
            session_id=self.session_id if session_id is None else session_id,
            session_incarnation_id=self.session_incarnation_id if session_incarnation_id is None else session_incarnation_id,
            title=self.title if title is None else title,
            current_step_id=self.current_step_id if current_step_id is None else current_step_id,
            plan_id=self.plan_id if plan_id is None else plan_id,
            approval_request_id=self.approval_request_id if approval_request_id is None else approval_request_id,
            transaction_id=self.transaction_id if transaction_id is None else transaction_id,
            verification_result=self.verification_result if verification_result is None else verification_result,
            failure=self.failure if failure is None else failure,
            cancellation_reason=self.cancellation_reason if cancellation_reason is None else cancellation_reason,
            resume_metadata=self.resume_metadata if resume_metadata is None else resume_metadata,
            revision=self.revision if revision is None else revision,
        )

    def cancel(self, reason: str = "", *, updated_at: Optional[float] = None) -> Work:
        """Explicitly cancel a non-terminal Work unit.

        Rules (Section 30):
        CREATED -> CANCELLED
        PLANNING -> CANCELLED
        APPROVAL_REQUIRED -> CANCELLED
        """
        if self.is_terminal:
            raise ValueError(f"Cannot cancel terminal Work in status '{self.status.value}'")

        now = time.time() if updated_at is None else float(updated_at)
        if math.isnan(now) or math.isinf(now):
            raise ValueError("updated_at must be a valid finite number")

        return replace(
            self,
            status=WorkStatus.CANCELLED,
            cancellation_reason=reason.strip()[:MAX_WORK_SUMMARY_CHARS],
            completed_at=now,
            updated_at=now,
        )

    def to_dict(self, *, full: Optional[bool] = None) -> Dict[str, Any]:
        """Serialize state to a JSON-safe dictionary with stable fields and secret checks."""
        # Auto-detect whether caller expects legacy 9-key dict or versioned full dict
        has_extended = (
            self.actor_id is not None
            or self.channel is not None
            or self.session_id is not None
            or self.session_incarnation_id is not None
            or bool(self.title)
            or self.started_at is not None
            or self.completed_at is not None
            or self.current_step_id is not None
            or self.plan_id is not None
            or self.approval_request_id is not None
            or self.transaction_id is not None
            or self.verification_result is not None
            or self.failure is not None
            or self.cancellation_reason is not None
            or self.resume_metadata is not None
            or self.revision > 1
            or self.schema_version > 1
        )
        is_full = full if full is not None else has_extended

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

        if is_full:
            data.update({
                "schema_version": self.schema_version,
                "work_id": self.id,
                "actor_id": self.actor_id,
                "channel": self.channel,
                "session_id": self.session_id,
                "session_incarnation_id": self.session_incarnation_id,
                "title": self.title,
                "started_at": self.started_at,
                "completed_at": self.completed_at,
                "current_step_id": self.current_step_id,
                "plan_id": self.plan_id,
                "approval_request_id": self.approval_request_id,
                "transaction_id": self.transaction_id,
                "verification_result": self.verification_result.to_dict() if self.verification_result else None,
                "failure": self.failure.to_dict() if self.failure else None,
                "cancellation_reason": self.cancellation_reason,
                "resume_metadata": dict(self.resume_metadata) if self.resume_metadata else None,
                "revision": self.revision,
            })

        reject_secrets(data)
        return data

    def to_persistence_dict(self) -> Dict[str, Any]:
        """Always serialize full versioned schema for persistent storage."""
        return self.to_dict(full=True)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Work:
        """Deserialize a Work record from dictionary, enforcing strict validation."""
        if type(data) is not dict:
            raise ValueError("Expected dictionary for Work.from_dict")

        reject_secrets(data)

        expected_keys = {
            "id", "work_id", "intent", "goal", "scope", "plan",
            "capabilities", "status", "created_at", "updated_at",
            "schema_version", "actor_id", "channel", "session_id",
            "session_incarnation_id", "title", "started_at", "completed_at",
            "current_step_id", "plan_id", "approval_request_id",
            "transaction_id", "verification_result", "failure",
            "cancellation", "cancellation_reason", "resume_metadata", "revision",
        }
        extra_keys = set(data.keys()) - expected_keys
        if extra_keys:
            raise ValueError(f"Unknown work fields: {sorted(extra_keys)}")

        if "id" not in data and "work_id" not in data:
            raise ValueError("Missing required field in Work: 'id'")
        actual_id = data.get("id") or data.get("work_id")

        for req in ("intent", "goal", "status", "created_at", "updated_at"):
            if req not in data:
                raise ValueError(f"Missing required field in Work: '{req}'")

        schema_ver = data.get("schema_version", CURRENT_WORK_SCHEMA_VERSION)
        if type(schema_ver) is not int or schema_ver < 1:
            raise ValueError(f"Invalid schema version: {schema_ver}")
        if schema_ver > CURRENT_WORK_SCHEMA_VERSION:
            raise ValueError(f"Unsupported future schema version: {schema_ver}")

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

        ver_data = data.get("verification_result")
        verification_result: Optional[VerificationResult] = None
        if ver_data is not None:
            if type(ver_data) is not dict:
                raise ValueError("Malformed verification_result in Work")
            verification_result = VerificationResult.from_dict(ver_data)

        fail_data = data.get("failure")
        failure: Optional[WorkFailure] = None
        if fail_data is not None:
            if type(fail_data) is not dict:
                raise ValueError("Malformed failure in Work")
            failure = WorkFailure.from_dict(fail_data)

        cancellation_reason = data.get("cancellation_reason") or data.get("cancellation")

        return cls(
            id=actual_id,
            intent=data["intent"],
            goal=data["goal"],
            scope=tuple(data.get("scope") or ()),
            plan=tuple(data.get("plan") or ()),
            capabilities=capabilities,
            status=status,
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            schema_version=schema_ver,
            actor_id=data.get("actor_id"),
            channel=data.get("channel"),
            session_id=data.get("session_id"),
            session_incarnation_id=data.get("session_incarnation_id"),
            title=data.get("title", ""),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            current_step_id=data.get("current_step_id"),
            plan_id=data.get("plan_id"),
            approval_request_id=data.get("approval_request_id"),
            transaction_id=data.get("transaction_id"),
            verification_result=verification_result,
            failure=failure,
            cancellation_reason=cancellation_reason,
            resume_metadata=data.get("resume_metadata"),
            revision=int(data.get("revision", 1)),
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
    def save(self, work: Work, expected_revision: Optional[int] = None) -> Work:
        """Update an existing Work record. Raises KeyError if not found."""
        raise NotImplementedError

    @abstractmethod
    def update(self, work_id: str, mutate_fn: Callable[[Work], Work]) -> Work:
        """Atomically read, modify, and persist a Work record under lock."""
        raise NotImplementedError

    @abstractmethod
    def delete(self, work_id: str) -> bool:
        """Delete a Work record by id. Returns True if deleted, False if not found."""
        raise NotImplementedError

    @abstractmethod
    def list_all(self, limit: Optional[int] = None) -> List[Work]:
        """List all Work records currently in the store."""
        raise NotImplementedError

    @abstractmethod
    def list_for_actor(
        self,
        actor_id: str,
        status_filter: Optional[str] = None,
        limit: int = MAX_WORKS_PER_RESPONSE,
    ) -> List[Work]:
        """List Work records belonging to a specific actor."""
        raise NotImplementedError

    @abstractmethod
    def list_active(self, limit: int = 50) -> List[Work]:
        """List all non-terminal active Work records."""
        raise NotImplementedError

    @abstractmethod
    def cleanup_terminal(
        self,
        actor_id: Optional[str] = None,
        max_retention: int = DEFAULT_MAX_TERMINAL_RETENTION,
    ) -> int:
        """Prune excess terminal Work records to prevent unbounded disk growth."""
        raise NotImplementedError


class InMemoryWorkStore(WorkStore):
    """Thread-safe, isolated in-memory WorkStore implementation."""

    def __init__(
        self,
        max_active_per_actor: int = MAX_ACTIVE_WORKS_PER_ACTOR,
        max_global: int = MAX_GLOBAL_WORKS,
        max_terminal_retention: int = DEFAULT_MAX_TERMINAL_RETENTION,
    ) -> None:
        self._records: Dict[str, Work] = {}
        self._lock = threading.RLock()
        self.max_active_per_actor = max_active_per_actor
        self.max_global = max_global
        self.max_terminal_retention = max_terminal_retention

    def create(self, work: Work) -> Work:
        if not isinstance(work, Work):
            raise TypeError("Expected Work instance")
        with self._lock:
            if work.id in self._records:
                raise ValueError(f"Work with id '{work.id}' already exists")

            # Quota checks
            if len(self._records) >= self.max_global:
                raise RuntimeError("Global Work quota exceeded")

            if work.actor_id and not work.is_terminal:
                actor_active = sum(
                    1 for w in self._records.values()
                    if w.actor_id == work.actor_id and not w.is_terminal
                )
                if actor_active >= self.max_active_per_actor:
                    raise RuntimeError(f"Maximum active works exceeded for actor '{work.actor_id}'")

            self._records[work.id] = work
            return work

    def get(self, work_id: str) -> Optional[Work]:
        if not isinstance(work_id, str):
            raise TypeError("work_id must be a string")
        with self._lock:
            return self._records.get(work_id)

    def save(self, work: Work, expected_revision: Optional[int] = None) -> Work:
        if not isinstance(work, Work):
            raise TypeError("Expected Work instance")
        with self._lock:
            if work.id not in self._records:
                raise KeyError(f"Work with id '{work.id}' does not exist")
            existing = self._records[work.id]

            # Optimistic concurrency check (Section 12)
            if expected_revision is not None and existing.revision != expected_revision:
                raise StaleWorkRevisionError(
                    f"Revision conflict: expected {expected_revision}, but found {existing.revision}"
                )
            if work.revision > 1 and work.revision < existing.revision:
                raise StaleWorkRevisionError(
                    f"Stale revision: update has revision {work.revision}, but store has revision {existing.revision}"
                )

            # State transition validation
            if existing.status != work.status:
                allowed = ALLOWED_TRANSITIONS.get(existing.status, frozenset())
                # Exception: cancellation via cancel() method is allowed from CREATED/PLANNING/APPROVAL_REQUIRED
                is_explicit_cancel = (
                    work.status == WorkStatus.CANCELLED
                    and existing.status in (WorkStatus.CREATED, WorkStatus.PLANNING, WorkStatus.APPROVAL_REQUIRED)
                )
                if work.status not in allowed and not is_explicit_cancel:
                    raise InvalidWorkTransition(
                        f"Cannot transition Work in store from '{existing.status.value}' to '{work.status.value}'"
                    )
            elif existing.is_terminal and existing != work:
                raise InvalidWorkTransition(
                    f"Cannot modify terminal Work record in status '{existing.status.value}'"
                )

            # Monotonically increment revision if not already incremented
            new_rev = existing.revision + 1 if work.revision == existing.revision else work.revision
            persisted = replace(work, revision=new_rev)
            self._records[work.id] = persisted
            return persisted

    def update(self, work_id: str, mutate_fn: Callable[[Work], Work]) -> Work:
        with self._lock:
            existing = self._records.get(work_id)
            if existing is None:
                raise KeyError(f"Work with id '{work_id}' does not exist")
            updated = mutate_fn(existing)
            return self.save(updated, expected_revision=existing.revision)

    def delete(self, work_id: str) -> bool:
        if not isinstance(work_id, str):
            raise TypeError("work_id must be a string")
        with self._lock:
            if work_id in self._records:
                del self._records[work_id]
                return True
            return False

    def list_all(self, limit: Optional[int] = None) -> List[Work]:
        with self._lock:
            items = list(self._records.values())
            return items[:limit] if limit is not None else items

    def list_for_actor(
        self,
        actor_id: str,
        status_filter: Optional[str] = None,
        limit: int = MAX_WORKS_PER_RESPONSE,
    ) -> List[Work]:
        clean_actor = actor_id.strip()
        with self._lock:
            results = []
            for w in self._records.values():
                if w.actor_id != clean_actor:
                    continue
                if status_filter:
                    sf = status_filter.lower().strip()
                    if sf == "active" and w.is_terminal:
                        continue
                    elif sf == "failed" and w.status != WorkStatus.FAILED:
                        continue
                    elif sf == "done" and w.status != WorkStatus.DONE:
                        continue
                    elif sf not in ("active", "failed", "done", "all") and w.status.value != sf:
                        continue
                results.append(w)
                if len(results) >= limit:
                    break
            # Sort newest updated first
            results.sort(key=lambda x: x.updated_at, reverse=True)
            return results[:limit]

    def list_active(self, limit: int = 50) -> List[Work]:
        with self._lock:
            results = [w for w in self._records.values() if not w.is_terminal]
            results.sort(key=lambda x: x.updated_at, reverse=True)
            return results[:limit]

    def cleanup_terminal(
        self,
        actor_id: Optional[str] = None,
        max_retention: int = DEFAULT_MAX_TERMINAL_RETENTION,
    ) -> int:
        with self._lock:
            terminals = [
                (k, v) for k, v in self._records.items()
                if v.is_terminal and (actor_id is None or v.actor_id == actor_id.strip())
            ]
            if len(terminals) <= max_retention:
                return 0
            terminals.sort(key=lambda pair: pair[1].updated_at)
            excess = len(terminals) - max_retention
            pruned = 0
            for k, _ in terminals[:excess]:
                del self._records[k]
                pruned += 1
            return pruned


if TYPE_CHECKING:
    from core.runtime.work_store import FileWorkStore


def __getattr__(name: str) -> Any:
    if name == "FileWorkStore":
        from core.runtime.work_store import FileWorkStore
        return FileWorkStore
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")

__all__ = [
    "ACTIVE_WORK_STATUSES",
    "ALLOWED_TRANSITIONS",
    "CURRENT_WORK_SCHEMA_VERSION",
    "DEFAULT_MAX_TERMINAL_RETENTION",
    "FileWorkStore",
    "InMemoryWorkStore",
    "InvalidWorkTransition",
    "MAX_ACTIVE_WORKS_PER_ACTOR",
    "MAX_GLOBAL_WORKS",
    "MAX_WORK_BYTES",
    "MAX_WORK_DETAILS_CHARS",
    "MAX_WORK_FAILURE_CHARS",
    "MAX_WORK_GOAL_CHARS",
    "MAX_WORK_INTENT_CHARS",
    "MAX_WORK_SUMMARY_CHARS",
    "MAX_WORK_TITLE_CHARS",
    "MAX_WORKS_PER_RESPONSE",
    "StaleWorkRevisionError",
    "TERMINAL_WORK_STATUSES",
    "VerificationResult",
    "VerificationStatus",
    "Work",
    "WorkFailure",
    "WorkStatus",
    "WorkStore",
]
