"""Child Work Domain Model and Deterministic Worker Lifecycle Integration.

BrainFrog P1.3D — Child Work Lifecycle.

Architectural Principles:
- A subagent is a WORKER, not an authority.
- A ChildWork binds a parent Work, Subagent, and DelegationContract into a
  deterministic, isolated Work lifecycle.
- ChildWork holds NO execution authority, approval authority, or filesystem permissions.
- It cannot spawn orchestrators, mutate transactions, or bypass capability contracts.
- The canonical execution path remains strictly:
    Child Work -> Plan -> Approval -> ApprovedExecutionContract -> Transaction -> orchestrator.py
- Canonical orchestrator.py remains the SOLE execution engine in BrainFrog.
"""
from __future__ import annotations

import math
import re
import secrets
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple, Union

from core.runtime.capabilities import Capabilities
from core.runtime.contract import reject_secrets
from core.runtime.delegation import DelegationContract, DelegationExpiredError, DelegationIntegrityError
from core.runtime.secret_scrubbing import scrub_secrets
from core.runtime.subagent import Subagent, SubagentStatus
from core.runtime.work import (
    ALLOWED_TRANSITIONS,
    CURRENT_WORK_SCHEMA_VERSION,
    InvalidWorkTransition,
    StaleWorkRevisionError,
    TERMINAL_WORK_STATUSES,
    VerificationResult,
    Work,
    WorkFailure,
    WorkStatus,
    WorkStore,
)
from core.runtime.work_authorization import (
    authorize_work_execution_continuation,
    authorize_work_inspection,
)
from core.runtime.work_continuation import cancel_work, resume_work

CURRENT_CHILD_WORK_SCHEMA_VERSION = 1
_INVALID_ID_CHARS = re.compile(r"[\x00-\x1f\x7f/\\:*\?\"<>\|]|(?:\.\.)")


class ChildWorkError(ValueError):
    """Base exception for child work domain errors."""
    pass


class ChildWorkBindingError(ChildWorkError):
    """Raised when identity or authority bindings between parent, subagent, and child work mismatch."""
    pass


class ChildWorkExpiredError(ChildWorkError, PermissionError):
    """Raised when a child work lifecycle operation is attempted on an expired delegation contract."""
    pass


class ChildWorkSessionStaleError(ChildWorkError, PermissionError):
    """Raised when an operation is attempted with an invalidated or stale session incarnation."""
    pass


class ChildWorkIntegrityError(ChildWorkError):
    """Raised when child work integrity, tampering, or digest verification fails."""
    pass


class InvalidChildWorkTransition(ChildWorkError):
    """Raised when an invalid child work or subagent lifecycle transition is attempted."""
    pass


def validate_child_work_id(work_id: str) -> str:
    """Validate child work ID to prevent path traversal, drive letters, and malformed identifiers."""
    if type(work_id) is not str:
        raise ChildWorkBindingError(f"Child work ID must be a string, got {type(work_id)}")
    clean = work_id.strip()
    if not clean or len(clean) > 128:
        raise ChildWorkBindingError("Child work ID must be a non-empty string with length <= 128")
    if _INVALID_ID_CHARS.search(clean) or clean.startswith((".", "~", "/", "\\")):
        raise ChildWorkBindingError(f"Invalid characters or traversal detected in Child work ID: '{clean}'")
    return clean


_INVALID_SESSION_ID_CHARS = re.compile(r"[\x00-\x1f\x7f/\\*\?\"<>\|]|(?:\.\.)")


def _validate_binding_field(val: Any, name: str) -> str:
    """Validate mandatory binding fields (parent_work_id, subagent_id, delegation_id, actor, session)."""
    if type(val) is not str:
        raise ChildWorkBindingError(f"{name} must be a string, got {type(val)}")
    clean = val.strip()
    if not clean:
        raise ChildWorkBindingError(f"{name} cannot be empty")
    regex = _INVALID_SESSION_ID_CHARS if name == "session_id" else _INVALID_ID_CHARS
    if regex.search(clean) or clean.startswith((".", "~", "/", "\\")):
        raise ChildWorkBindingError(f"Invalid characters or traversal in {name}: '{clean}'")
    return clean


@dataclass(frozen=True)
class ChildWork:
    """Immutable domain model representing a delegated child Work unit.

    A ChildWork permanently binds:
    - child_work_id: Unique identifier for this child unit (matches underlying work.id)
    - parent_work_id: The enclosing parent Work ID
    - subagent_id: The subordinate Subagent worker ID
    - delegation_id: The authorizing DelegationContract ID
    - session_id: The bound chat/client session
    - session_incarnation_id: The bound session incarnation (stale on /reset or /new)
    - actor: The authenticated user initiating the work
    - created_at: Creation timestamp
    - updated_at: Last update timestamp
    - work: Canonical existing Work model tracking the state machine
    - delegation_digest: Cryptographic digest of the authorizing delegation contract
    - delegation_expires_at: Expiration boundary of the authorizing delegation contract
    - delegation: Optional in-memory reference to the DelegationContract
    - subagent: Optional in-memory reference to the Subagent
    - schema_version: Schema version for child work representation
    """
    child_work_id: str
    parent_work_id: str
    subagent_id: str
    delegation_id: str
    session_id: str
    session_incarnation_id: str
    actor: str
    created_at: float
    updated_at: float
    work: Work
    delegation_digest: str = ""
    delegation_expires_at: float = 0.0
    delegation: Optional[DelegationContract] = None
    subagent: Optional[Subagent] = None
    schema_version: int = CURRENT_CHILD_WORK_SCHEMA_VERSION

    def __post_init__(self) -> None:
        # Validate ID formats
        clean_child_id = validate_child_work_id(self.child_work_id)
        object.__setattr__(self, "child_work_id", clean_child_id)

        clean_parent = _validate_binding_field(self.parent_work_id, "parent_work_id")
        object.__setattr__(self, "parent_work_id", clean_parent)

        clean_subagent = _validate_binding_field(self.subagent_id, "subagent_id")
        object.__setattr__(self, "subagent_id", clean_subagent)

        clean_delg = _validate_binding_field(self.delegation_id, "delegation_id")
        object.__setattr__(self, "delegation_id", clean_delg)

        clean_session = _validate_binding_field(self.session_id, "session_id")
        object.__setattr__(self, "session_id", clean_session)

        clean_inc = _validate_binding_field(self.session_incarnation_id, "session_incarnation_id")
        object.__setattr__(self, "session_incarnation_id", clean_inc)

        clean_actor = _validate_binding_field(self.actor, "actor")
        object.__setattr__(self, "actor", clean_actor)

        # Validate underlying work correspondence
        if not isinstance(self.work, Work):
            raise ChildWorkBindingError(f"work must be a canonical Work instance, got {type(self.work)}")
        if self.work.id != clean_child_id:
            raise ChildWorkBindingError(
                f"Child work ID mismatch with underlying Work id: '{clean_child_id}' vs '{self.work.id}'"
            )

        if self.work.actor_id and self.work.actor_id != clean_actor:
            raise ChildWorkBindingError(
                f"Actor mismatch between ChildWork and Work: '{clean_actor}' vs '{self.work.actor_id}'"
            )

        if self.work.session_id and self.work.session_id != clean_session:
            raise ChildWorkBindingError(
                f"Session mismatch between ChildWork and Work: '{clean_session}' vs '{self.work.session_id}'"
            )

        if self.work.session_incarnation_id and self.work.session_incarnation_id != clean_inc:
            raise ChildWorkBindingError(
                f"Incarnation mismatch between ChildWork and Work: '{clean_inc}' vs '{self.work.session_incarnation_id}'"
            )

        # Validate timestamps
        if not isinstance(self.created_at, (int, float)) or not math.isfinite(self.created_at):
            raise ChildWorkBindingError("created_at must be a valid finite number")
        object.__setattr__(self, "created_at", float(self.created_at))

        if not isinstance(self.updated_at, (int, float)) or not math.isfinite(self.updated_at):
            raise ChildWorkBindingError("updated_at must be a valid finite number")
        object.__setattr__(self, "updated_at", float(self.updated_at))

        if not isinstance(self.delegation_expires_at, (int, float)) or not math.isfinite(self.delegation_expires_at):
            raise ChildWorkBindingError("delegation_expires_at must be a valid finite number")
        object.__setattr__(self, "delegation_expires_at", float(self.delegation_expires_at))

        # Check in-memory delegation bindings if provided
        if self.delegation is not None:
            if not isinstance(self.delegation, DelegationContract):
                raise ChildWorkBindingError("delegation must be a DelegationContract instance")
            self.delegation.validate_integrity()
            if self.delegation.delegation_id != clean_delg:
                raise ChildWorkBindingError(
                    f"Delegation ID mismatch: contract has '{self.delegation.delegation_id}', expected '{clean_delg}'"
                )
            if self.delegation.parent_work_id != clean_parent:
                raise ChildWorkBindingError(
                    f"Parent work ID mismatch: delegation has '{self.delegation.parent_work_id}', expected '{clean_parent}'"
                )
            if self.delegation.child_subagent_id != clean_subagent:
                raise ChildWorkBindingError(
                    f"Subagent ID mismatch: delegation has '{self.delegation.child_subagent_id}', expected '{clean_subagent}'"
                )
            if self.delegation.actor != clean_actor:
                raise ChildWorkBindingError(
                    f"Actor mismatch: delegation has '{self.delegation.actor}', expected '{clean_actor}'"
                )
            if self.delegation.session_id != clean_session:
                raise ChildWorkBindingError(
                    f"Session mismatch: delegation has '{self.delegation.session_id}', expected '{clean_session}'"
                )
            if self.delegation.session_incarnation_id != clean_inc:
                raise ChildWorkBindingError(
                    f"Incarnation mismatch: delegation has '{self.delegation.session_incarnation_id}', expected '{clean_inc}'"
                )
            if self.delegation_digest and self.delegation.digest != self.delegation_digest:
                raise ChildWorkIntegrityError("Delegation digest mismatch with contract")

        # Check in-memory subagent bindings if provided
        if self.subagent is not None:
            if not isinstance(self.subagent, Subagent):
                raise ChildWorkBindingError("subagent must be a Subagent instance")
            if self.subagent.subagent_id != clean_subagent:
                raise ChildWorkBindingError(
                    f"Subagent ID mismatch: subagent has '{self.subagent.subagent_id}', expected '{clean_subagent}'"
                )
            if self.subagent.parent_work_id != clean_parent:
                raise ChildWorkBindingError(
                    f"Parent work ID mismatch in subagent: '{self.subagent.parent_work_id}' vs '{clean_parent}'"
                )
            if self.subagent.actor != clean_actor:
                raise ChildWorkBindingError(
                    f"Actor mismatch in subagent: '{self.subagent.actor}' vs '{clean_actor}'"
                )
            if self.subagent.session_id != clean_session:
                raise ChildWorkBindingError(
                    f"Session mismatch in subagent: '{self.subagent.session_id}' vs '{clean_session}'"
                )
            if self.subagent.session_incarnation_id != clean_inc:
                raise ChildWorkBindingError(
                    f"Incarnation mismatch in subagent: '{self.subagent.session_incarnation_id}' vs '{clean_inc}'"
                )

        # Secret scrubbing across identity and descriptive fields
        reject_secrets({
            "child_work_id": self.child_work_id,
            "parent_work_id": self.parent_work_id,
            "subagent_id": self.subagent_id,
            "delegation_id": self.delegation_id,
            "actor": self.actor,
            "session_id": self.session_id,
            "session_incarnation_id": self.session_incarnation_id,
        })

    @property
    def id(self) -> str:
        """Alias for child_work_id."""
        return self.child_work_id

    @property
    def work_id(self) -> str:
        """Alias for child_work_id."""
        return self.child_work_id

    @property
    def status(self) -> WorkStatus:
        """Current lifecycle status of the child work."""
        return self.work.status

    @property
    def is_terminal(self) -> bool:
        """True if the child work is in a terminal status (DONE, FAILED, CANCELLED)."""
        return self.work.is_terminal

    @property
    def is_resumable(self) -> bool:
        """True if the child work is in a resumable non-terminal status."""
        return self.work.is_resumable

    @property
    def capabilities(self) -> Optional[Capabilities]:
        """Attenuated capabilities granted to this child work."""
        return self.work.capabilities

    @property
    def target_scope(self) -> Tuple[str, ...]:
        """Attenuated target paths granted to this child work."""
        return self.work.scope

    @property
    def revision(self) -> int:
        """Optimistic concurrency revision of the underlying work record."""
        return self.work.revision

    def is_delegation_expired(self, current_time: Optional[float] = None) -> bool:
        """Check whether the authorizing delegation contract has expired."""
        now = time.time() if current_time is None else current_time
        if self.delegation_expires_at > 0 and now >= self.delegation_expires_at:
            return True
        if self.delegation is not None:
            return self.delegation.is_expired
        return False

    @classmethod
    def create(
        cls,
        *,
        parent_work: Work,
        subagent: Subagent,
        delegation: DelegationContract,
        intent: str = "",
        goal: str = "",
        child_work_id: Optional[str] = None,
        title: str = "",
        created_at: Optional[float] = None,
    ) -> ChildWork:
        """Deterministically create a child Work bound to parent Work, Subagent, and DelegationContract.

        Enforces:
        - Parent, subagent, and delegation identity correspondence.
        - Actor and session incarnation binding.
        - Delegation validity and non-expiration.
        - Child work scope strictly initialized from delegation (attenuated).
        - Subagent status synchronized to READY.
        """
        now = time.time() if created_at is None else float(created_at)

        # 1. Validate DelegationContract integrity and expiration
        delegation.validate_integrity()
        if delegation.is_expired or now >= delegation.expires_at:
            raise ChildWorkExpiredError(
                f"Delegation contract '{delegation.delegation_id}' expired at {delegation.expires_at} (current time: {now})"
            )

        # 2. Extract parent bindings
        parent_id = parent_work.id
        parent_actor = parent_work.actor_id or getattr(parent_work, "actor", "")
        parent_session = parent_work.session_id or ""
        parent_inc = parent_work.session_incarnation_id or ""

        if not parent_actor:
            raise ChildWorkBindingError("Parent work lacks an authenticated actor")
        if not parent_session:
            raise ChildWorkBindingError("Parent work lacks a session binding")
        if not parent_inc:
            raise ChildWorkBindingError("Parent work lacks a session incarnation binding")

        # 3. Verify Delegation bindings against Parent Work
        if delegation.parent_work_id != parent_id:
            raise ChildWorkBindingError(
                f"Delegation parent mismatch: contract has '{delegation.parent_work_id}', parent work is '{parent_id}'"
            )
        if delegation.actor != parent_actor:
            raise ChildWorkBindingError(
                f"Delegation actor mismatch: contract has '{delegation.actor}', parent work has '{parent_actor}'"
            )
        if delegation.session_id != parent_session:
            raise ChildWorkBindingError(
                f"Delegation session mismatch: contract has '{delegation.session_id}', parent work has '{parent_session}'"
            )
        if delegation.session_incarnation_id != parent_inc:
            raise ChildWorkBindingError(
                f"Delegation incarnation mismatch: contract has '{delegation.session_incarnation_id}', parent work has '{parent_inc}'"
            )

        # 4. Verify Subagent bindings against Parent Work and Delegation
        if subagent.parent_work_id != parent_id:
            raise ChildWorkBindingError(
                f"Subagent parent mismatch: subagent bound to '{subagent.parent_work_id}', parent work is '{parent_id}'"
            )
        if subagent.subagent_id != delegation.child_subagent_id:
            raise ChildWorkBindingError(
                f"Subagent ID mismatch: subagent is '{subagent.subagent_id}', delegation specifies '{delegation.child_subagent_id}'"
            )
        if subagent.actor != parent_actor:
            raise ChildWorkBindingError(
                f"Subagent actor mismatch: subagent bound to '{subagent.actor}', parent work has '{parent_actor}'"
            )
        if subagent.session_id != parent_session:
            raise ChildWorkBindingError(
                f"Subagent session mismatch: subagent bound to '{subagent.session_id}', parent work has '{parent_session}'"
            )
        if subagent.session_incarnation_id != parent_inc:
            raise ChildWorkBindingError(
                f"Subagent incarnation mismatch: subagent bound to '{subagent.session_incarnation_id}', parent work has '{parent_inc}'"
            )

        # 5. Formulate child work metadata
        actual_child_id = validate_child_work_id(child_work_id or f"work_child_{secrets.token_hex(16)}")
        eff_intent = intent.strip() or subagent.role or parent_work.intent
        eff_goal = goal.strip() or subagent.purpose or parent_work.goal
        eff_title = title.strip() or f"Subagent {subagent.role or subagent.subagent_id}"

        # 7. Synchronize Subagent status (CREATED -> READY)
        updated_subagent = subagent
        if subagent.status == SubagentStatus.CREATED:
            updated_subagent = subagent.transition(
                SubagentStatus.READY,
                delegation_id=delegation.delegation_id,
            )

        # Initialize resume_metadata with permanent child work binding details
        resume_meta = {
            "child_work": {
                "parent_work_id": parent_id,
                "subagent_id": subagent.subagent_id,
                "delegation_id": delegation.delegation_id,
                "actor": parent_actor,
                "session_id": parent_session,
                "session_incarnation_id": parent_inc,
                "delegation_digest": delegation.digest,
                "delegation_expires_at": delegation.expires_at,
                "subagent_status": updated_subagent.status.value,
                "subagent": updated_subagent.to_dict(),
                "delegation": delegation.to_dict(),
            },
            "delegation_digest": delegation.digest,
            "delegation_expires_at": delegation.expires_at,
        }

        # 6. Instantiate canonical Work model for child
        # Note: Child work receives ONLY the attenuated capabilities and target_scope from DelegationContract
        underlying_work = Work(
            id=actual_child_id,
            intent=eff_intent,
            goal=eff_goal,
            scope=delegation.target_scope,
            capabilities=delegation.capabilities,
            status=WorkStatus.CREATED,
            created_at=now,
            updated_at=now,
            actor_id=parent_actor,
            channel=parent_work.channel,
            session_id=parent_session,
            session_incarnation_id=parent_inc,
            title=eff_title,
            resume_metadata=resume_meta,
        )

        return cls(
            child_work_id=actual_child_id,
            parent_work_id=parent_id,
            subagent_id=subagent.subagent_id,
            delegation_id=delegation.delegation_id,
            session_id=parent_session,
            session_incarnation_id=parent_inc,
            actor=parent_actor,
            created_at=now,
            updated_at=now,
            work=underlying_work,
            delegation_digest=delegation.digest,
            delegation_expires_at=delegation.expires_at,
            delegation=delegation,
            subagent=updated_subagent,
        )

    def transition(
        self,
        new_status: Union[WorkStatus, str],
        *,
        current_time: Optional[float] = None,
        current_session_incarnation_id: Optional[str] = None,
        delegation: Optional[DelegationContract] = None,
        subagent: Optional[Subagent] = None,
        plan: Optional[Sequence[str]] = None,
        scope: Optional[Sequence[str]] = None,
        capabilities: Optional[Capabilities] = None,
        verification_result: Optional[VerificationResult] = None,
        failure: Optional[WorkFailure] = None,
        cancellation_reason: Optional[str] = None,
        plan_id: Optional[str] = None,
        approval_request_id: Optional[str] = None,
        transaction_id: Optional[str] = None,
        updated_at: Optional[float] = None,
    ) -> ChildWork:
        """Deterministically transition ChildWork to a new status while preserving authority boundaries.

        Rules:
        - Checks delegation expiration fail-closed.
        - Checks session incarnation freshness fail-closed.
        - Transitions underlying Work through canonical state machine.
        - Synchronizes Subagent lifecycle status monotonically:
            PLANNING/EXECUTING -> RUNNING
            DONE -> COMPLETED
            FAILED -> FAILED
            CANCELLED -> CANCELLED
        """
        now = time.time() if current_time is None else float(current_time)

        # 1. Delegation Expiration Enforcement
        eff_delg = delegation or self.delegation
        if eff_delg is not None:
            eff_delg.validate_integrity()
            if eff_delg.is_expired or now >= eff_delg.expires_at:
                raise ChildWorkExpiredError(
                    f"Delegation contract '{self.delegation_id}' expired at {eff_delg.expires_at} (current time: {now})"
                )
        elif self.delegation_expires_at > 0 and now >= self.delegation_expires_at:
            raise ChildWorkExpiredError(
                f"Delegation contract '{self.delegation_id}' expired at {self.delegation_expires_at} (current time: {now})"
            )

        # 2. Session Incarnation Freshness Enforcement
        if current_session_incarnation_id is not None:
            if current_session_incarnation_id != self.session_incarnation_id:
                raise ChildWorkSessionStaleError(
                    f"Child work session incarnation '{self.session_incarnation_id}' is stale. "
                    f"Current authoritative incarnation is '{current_session_incarnation_id}'. Action rejected."
                )

        # 3. Transition underlying canonical Work
        actual_updated = now if updated_at is None else float(updated_at)
        target_status = WorkStatus(new_status) if isinstance(new_status, str) else new_status

        # Prevent authority widening during transition: scope and capabilities cannot exceed original delegation
        if scope is not None and self.delegation is not None:
            from core.runtime.delegation import is_path_subset
            for s in scope:
                if not any(is_path_subset(s, ds) for ds in self.delegation.target_scope):
                    raise ChildWorkBindingError(f"Requested child scope '{s}' exceeds delegation target scope")

        new_work = self.work.transition(
            target_status,
            plan=plan,
            scope=scope,
            capabilities=capabilities,
            updated_at=actual_updated,
            current_step_id=self.work.current_step_id,
            plan_id=plan_id,
            approval_request_id=approval_request_id,
            transaction_id=transaction_id,
            verification_result=verification_result,
            failure=failure,
            cancellation_reason=cancellation_reason,
        )

        # 4. Synchronize Subagent status
        active_subagent = subagent or self.subagent
        updated_subagent = active_subagent
        if active_subagent is not None:
            if target_status in (WorkStatus.PLANNING, WorkStatus.EXECUTING):
                if active_subagent.status == SubagentStatus.READY:
                    updated_subagent = active_subagent.transition(SubagentStatus.RUNNING)
            elif target_status == WorkStatus.DONE:
                if active_subagent.status in (SubagentStatus.READY, SubagentStatus.RUNNING):
                    updated_subagent = active_subagent.transition(SubagentStatus.COMPLETED)
            elif target_status == WorkStatus.FAILED:
                if active_subagent.is_active:
                    fail_summary = failure.summary if failure else "Child work failed"
                    updated_subagent = active_subagent.transition(
                        SubagentStatus.FAILED, failure_reason=fail_summary
                    )
            elif target_status == WorkStatus.CANCELLED:
                if active_subagent.is_active:
                    updated_subagent = active_subagent.transition(
                        SubagentStatus.CANCELLED, failure_reason=cancellation_reason or "Child work cancelled"
                    )

        work_meta = dict(self.work.resume_metadata or {})
        if "child_work" in work_meta:
            cw_meta = dict(work_meta["child_work"])
            if updated_subagent is not None:
                cw_meta["subagent_status"] = updated_subagent.status.value
                cw_meta["subagent"] = updated_subagent.to_dict()
            if eff_delg is not None:
                cw_meta["delegation"] = eff_delg.to_dict()
                cw_meta["delegation_digest"] = eff_delg.digest
                cw_meta["delegation_expires_at"] = eff_delg.expires_at
                work_meta["delegation_digest"] = eff_delg.digest
                work_meta["delegation_expires_at"] = eff_delg.expires_at
            work_meta["child_work"] = cw_meta

        new_work = self.work.transition(
            target_status,
            plan=plan,
            scope=scope,
            capabilities=capabilities,
            updated_at=actual_updated,
            current_step_id=self.work.current_step_id,
            plan_id=plan_id,
            approval_request_id=approval_request_id,
            transaction_id=transaction_id,
            verification_result=verification_result,
            failure=failure,
            cancellation_reason=cancellation_reason,
            resume_metadata=work_meta,
        )

        return replace(
            self,
            work=new_work,
            updated_at=actual_updated,
            delegation=eff_delg,
            subagent=updated_subagent,
        )

    def cancel(
        self,
        reason: str = "",
        *,
        current_time: Optional[float] = None,
        current_session_incarnation_id: Optional[str] = None,
        subagent: Optional[Subagent] = None,
    ) -> ChildWork:
        """Deterministically cancel an active ChildWork unit."""
        now = time.time() if current_time is None else float(current_time)

        # Session incarnation check
        if current_session_incarnation_id is not None:
            if current_session_incarnation_id != self.session_incarnation_id:
                raise ChildWorkSessionStaleError(
                    f"Child work session incarnation '{self.session_incarnation_id}' is stale. "
                    f"Current authoritative incarnation is '{current_session_incarnation_id}'. Action rejected."
                )

        clean_reason = reason.strip() or "Child work cancelled"
        new_work = self.work.cancel(reason=clean_reason, updated_at=now)

        active_subagent = subagent or self.subagent
        updated_subagent = active_subagent
        if active_subagent is not None and active_subagent.is_active:
            updated_subagent = active_subagent.transition(
                SubagentStatus.CANCELLED, failure_reason=clean_reason
            )

        if updated_subagent is not None:
            work_meta = dict(new_work.resume_metadata or {})
            if "child_work" in work_meta:
                cw_meta = dict(work_meta["child_work"])
                cw_meta["subagent_status"] = updated_subagent.status.value
                cw_meta["subagent"] = updated_subagent.to_dict()
                work_meta["child_work"] = cw_meta
                new_work = replace(new_work, resume_metadata=work_meta)

        return replace(
            self,
            work=new_work,
            updated_at=now,
            subagent=updated_subagent,
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
        current_step_id: Optional[str] = None,
        plan_id: Optional[str] = None,
        approval_request_id: Optional[str] = None,
        transaction_id: Optional[str] = None,
        verification_result: Optional[VerificationResult] = None,
        failure: Optional[WorkFailure] = None,
        cancellation_reason: Optional[str] = None,
        resume_metadata: Optional[Dict[str, Any]] = None,
        revision: Optional[int] = None,
        subagent: Optional[Subagent] = None,
    ) -> ChildWork:
        """Return an updated copy of non-terminal ChildWork without changing its status."""
        now = time.time() if updated_at is None else float(updated_at)
        new_work = self.work.with_update(
            intent=intent,
            goal=goal,
            plan=plan,
            scope=scope,
            capabilities=capabilities,
            updated_at=now,
            current_step_id=current_step_id,
            plan_id=plan_id,
            approval_request_id=approval_request_id,
            transaction_id=transaction_id,
            verification_result=verification_result,
            failure=failure,
            cancellation_reason=cancellation_reason,
            resume_metadata=resume_metadata,
            revision=revision,
        )
        return replace(
            self,
            work=new_work,
            updated_at=now,
            subagent=subagent or self.subagent,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize ChildWork to dictionary, enforcing secret scrubbing."""
        data: Dict[str, Any] = {
            "schema_version": self.schema_version,
            "child_work_id": self.child_work_id,
            "parent_work_id": self.parent_work_id,
            "subagent_id": self.subagent_id,
            "delegation_id": self.delegation_id,
            "session_id": self.session_id,
            "session_incarnation_id": self.session_incarnation_id,
            "actor": self.actor,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.work.status.value,
            "delegation_digest": self.delegation_digest,
            "delegation_expires_at": self.delegation_expires_at,
            "work": self.work.to_dict(full=True),
            "subagent": self.subagent.to_dict() if self.subagent else None,
        }
        reject_secrets(data)
        return data

    @classmethod
    def from_dict(
        cls,
        data: Dict[str, Any],
        delegation: Optional[DelegationContract] = None,
        subagent: Optional[Subagent] = None,
    ) -> ChildWork:
        """Deserialize ChildWork from dictionary with integrity and binding validation."""
        if type(data) is not dict:
            raise ChildWorkBindingError("Expected dictionary for ChildWork.from_dict")

        reject_secrets(data)

        required_keys = (
            "child_work_id", "parent_work_id", "subagent_id", "delegation_id",
            "session_id", "session_incarnation_id", "actor", "created_at",
            "updated_at", "work"
        )
        for req in required_keys:
            if req not in data:
                raise ChildWorkBindingError(f"Missing required field in ChildWork: '{req}'")

        schema_ver = int(data.get("schema_version", CURRENT_CHILD_WORK_SCHEMA_VERSION))
        if schema_ver > CURRENT_CHILD_WORK_SCHEMA_VERSION:
            raise ChildWorkBindingError(f"Unsupported future ChildWork schema version: {schema_ver}")

        work_data = data["work"]
        if not isinstance(work_data, dict):
            raise ChildWorkBindingError("ChildWork.work must be a serialized Work dictionary")

        underlying_work = Work.from_dict(work_data)

        parsed_subagent = subagent
        sub_data = data.get("subagent")
        if parsed_subagent is None and isinstance(sub_data, dict):
            parsed_subagent = Subagent.from_dict(sub_data)

        return cls(
            child_work_id=data["child_work_id"],
            parent_work_id=data["parent_work_id"],
            subagent_id=data["subagent_id"],
            delegation_id=data["delegation_id"],
            session_id=data["session_id"],
            session_incarnation_id=data["session_incarnation_id"],
            actor=data["actor"],
            created_at=float(data["created_at"]),
            updated_at=float(data["updated_at"]),
            work=underlying_work,
            delegation_digest=str(data.get("delegation_digest", "")),
            delegation_expires_at=float(data.get("delegation_expires_at", 0.0)),
            delegation=delegation,
            subagent=parsed_subagent,
            schema_version=schema_ver,
        )

    @classmethod
    def from_work(
        cls,
        work: Work,
        delegation: Optional[DelegationContract] = None,
        subagent: Optional[Subagent] = None,
    ) -> ChildWork:
        """Reconstruct ChildWork from a persisted canonical Work instance."""
        if not isinstance(work, Work):
            raise ChildWorkBindingError(f"Expected Work instance, got {type(work)}")

        meta = work.resume_metadata or {}
        child_meta = meta.get("child_work")
        if not isinstance(child_meta, dict):
            raise ChildWorkBindingError(f"Work '{work.id}' is not a child work (missing child_work metadata)")

        required = ("parent_work_id", "subagent_id", "delegation_id")
        for req in required:
            if req not in child_meta or not child_meta[req]:
                raise ChildWorkBindingError(f"Child work metadata missing required key: '{req}'")

        actor = child_meta.get("actor") or work.actor_id or ""
        session_id = child_meta.get("session_id") or work.session_id or ""
        session_inc = child_meta.get("session_incarnation_id") or work.session_incarnation_id or ""
        digest = str(child_meta.get("delegation_digest", meta.get("delegation_digest", "")))
        expires_at = float(child_meta.get("delegation_expires_at", meta.get("delegation_expires_at", 0.0)))

        parsed_subagent = subagent
        if parsed_subagent is None:
            sub_dict = child_meta.get("subagent")
            if isinstance(sub_dict, dict):
                parsed_subagent = Subagent.from_dict(sub_dict)
            elif "subagent_id" in child_meta:
                status_val = child_meta.get("subagent_status", SubagentStatus.READY.value)
                parsed_subagent = Subagent(
                    subagent_id=child_meta["subagent_id"],
                    parent_work_id=child_meta["parent_work_id"],
                    session_id=session_id,
                    session_incarnation_id=session_inc,
                    actor=actor,
                    status=SubagentStatus(status_val),
                )

        parsed_delg = delegation
        if parsed_delg is None:
            delg_dict = child_meta.get("delegation")
            if isinstance(delg_dict, dict):
                from core.runtime.delegation import DelegationContract
                try:
                    parsed_delg = DelegationContract.from_dict(delg_dict)
                    parsed_delg.validate_integrity()
                except Exception as exc:
                    raise ChildWorkIntegrityError(f"Persisted delegation integrity violation: {exc}") from exc
                if digest and parsed_delg.digest != digest:
                    raise ChildWorkIntegrityError(
                        f"Persisted delegation digest mismatch: expected '{digest}', got '{parsed_delg.digest}'"
                    )
        elif parsed_delg is not None:
            try:
                parsed_delg.validate_integrity()
            except Exception as exc:
                raise ChildWorkIntegrityError(f"Delegation integrity violation: {exc}") from exc
            if digest and parsed_delg.digest != digest:
                raise ChildWorkIntegrityError(
                    f"Delegation digest mismatch: expected '{digest}', got '{parsed_delg.digest}'"
                )

        eff_digest = parsed_delg.digest if parsed_delg is not None else digest
        eff_expires_at = parsed_delg.expires_at if parsed_delg is not None else expires_at

        return cls(
            child_work_id=work.id,
            parent_work_id=child_meta["parent_work_id"],
            subagent_id=child_meta["subagent_id"],
            delegation_id=child_meta["delegation_id"],
            session_id=session_id,
            session_incarnation_id=session_inc,
            actor=actor,
            created_at=work.created_at,
            updated_at=work.updated_at,
            work=work,
            delegation_digest=eff_digest,
            delegation_expires_at=eff_expires_at,
            delegation=parsed_delg,
            subagent=parsed_subagent,
        )


# ==============================================================================
# Parent-Child Relationship and Observational Helpers
# ==============================================================================


def attach_child_work_reference(parent_work: Work, child_work_id: str) -> Work:
    """Return an updated parent Work containing an observational reference to child_work_id."""
    clean_id = validate_child_work_id(child_work_id)
    meta = dict(parent_work.resume_metadata or {})
    current_ids = list(meta.get("child_work_ids") or [])
    if clean_id not in current_ids:
        current_ids.append(clean_id)
    meta["child_work_ids"] = current_ids
    return parent_work.with_update(resume_metadata=meta)


def get_child_work_ids(work: Work) -> Tuple[str, ...]:
    """Extract bounded observational child work IDs from parent Work metadata."""
    if not work.resume_metadata:
        return ()
    raw = work.resume_metadata.get("child_work_ids") or ()
    if isinstance(raw, (list, tuple)):
        return tuple(str(x) for x in raw)
    return ()


# ==============================================================================
# Persistent Store Integration Helpers
# ==============================================================================


def save_child_work(
    work_store: Any,
    child_work: ChildWork,
    expected_revision: Optional[int] = None,
) -> ChildWork:
    """Persist ChildWork by saving its underlying canonical Work record to WorkStore.

    Preserves Optimistic Concurrency Control (OCC) and crash recovery compatibility.
    """
    existing = work_store.get(child_work.id)
    if existing is None:
        saved_work = work_store.create(child_work.work)
    else:
        saved_work = work_store.save(child_work.work, expected_revision=expected_revision)
    return replace(
        child_work,
        work=saved_work,
        updated_at=saved_work.updated_at,
    )


def get_child_work(
    work_store: Any,
    child_work_id: str,
    delegation: Optional[DelegationContract] = None,
    subagent: Optional[Subagent] = None,
) -> Optional[ChildWork]:
    """Retrieve and reconstruct ChildWork from WorkStore."""
    work = work_store.get(child_work_id)
    if work is None:
        return None
    try:
        return ChildWork.from_work(work, delegation=delegation, subagent=subagent)
    except ChildWorkBindingError:
        return None


def list_child_works_for_parent(
    work_store: Any,
    parent_work_id: str,
) -> List[ChildWork]:
    """List all ChildWork instances belonging to a parent Work."""
    clean_parent = _validate_binding_field(parent_work_id, "parent_work_id")
    parent = work_store.get(clean_parent)
    results: List[ChildWork] = []
    if parent is None:
        return results

    child_ids = get_child_work_ids(parent)
    for cid in child_ids:
        cw = get_child_work(work_store, cid)
        if cw is not None and cw.parent_work_id == clean_parent:
            results.append(cw)
    return results


def cancel_child_works_for_parent(
    work_store: Any,
    parent_work: Work,
    actor_id: str,
    channel: str = "cli",
    reason: str = "Parent work cancelled",
) -> List[ChildWork]:
    """Cancel all active child works belonging to a parent Work.

    Enforces deterministic cancellation propagation without side-effects or execution.
    """
    child_ids = get_child_work_ids(parent_work)
    cancelled_children: List[ChildWork] = []

    for cid in child_ids:
        child_work = get_child_work(work_store, cid)
        if child_work is not None and not child_work.is_terminal:
            cancelled_cw = child_work.cancel(reason=reason)
            saved_cw = save_child_work(work_store, cancelled_cw)
            cancelled_children.append(saved_cw)

    return cancelled_children


def resume_child_work(
    child_work_id: str,
    *,
    actor_id: str,
    channel: str,
    session_id: Optional[str] = None,
    session_incarnation_id: Optional[str] = None,
    work_store: Any = None,
    delegation: Optional[DelegationContract] = None,
    approval_service: Any = None,
    transaction_store: Any = None,
    recovery_manager: Any = None,
    current_time: Optional[float] = None,
) -> Tuple[bool, str, Optional[ChildWork]]:
    """Resume an incomplete ChildWork unit through the canonical work continuation pipeline.

    Enforces:
    - Delegation non-expiration fail-closed.
    - Session incarnation freshness fail-closed.
    - Actor authorization and IDOR protection.
    - No execution bypass (execution requires orchestrator).
    """
    now = time.time() if current_time is None else float(current_time)
    child = get_child_work(work_store, child_work_id, delegation=delegation)
    if child is None:
        return False, f"Child work '{child_work_id}' not found.", None

    # Check delegation expiration
    if child.is_delegation_expired(current_time=now):
        return False, f"Delegation contract '{child.delegation_id}' has expired. Fresh delegation required.", child

    # Resume via canonical work continuation engine
    ok, msg, res_work = resume_work(
        work_id=child_work_id,
        actor_id=actor_id,
        channel=channel,
        session_id=session_id,
        session_incarnation_id=session_incarnation_id,
        work_store=work_store,
        approval_service=approval_service,
        transaction_store=transaction_store,
        recovery_manager=recovery_manager,
    )
    if not ok or res_work is None:
        return False, msg, child

    updated_child = replace(child, work=res_work, updated_at=res_work.updated_at)
    return True, msg, updated_child


# ==============================================================================
# Authorization and IDOR Protection
# ==============================================================================


def authorize_child_work_inspection(
    child_work: ChildWork,
    actor_id: str,
    channel: str,
) -> Tuple[bool, str]:
    """Verify whether an authenticated actor is authorized to inspect a ChildWork record."""
    return authorize_work_inspection(child_work.work, actor_id, channel)


def authorize_child_work_mutation(
    child_work: ChildWork,
    actor_id: str,
    session_id: str,
    session_incarnation_id: str,
    current_time: Optional[float] = None,
) -> Tuple[bool, str]:
    """Verify whether an authenticated actor may mutate a ChildWork record."""
    ok, reason = authorize_work_inspection(child_work.work, actor_id, "cli")
    if not ok:
        return False, reason

    if child_work.session_id != session_id:
        return False, f"Child work '{child_work.id}' belongs to a different session context."

    if child_work.session_incarnation_id != session_incarnation_id:
        return False, "Cannot mutate work created under an invalidated session incarnation."

    if child_work.is_delegation_expired(current_time=current_time):
        return False, "Authorizing delegation contract has expired."

    return True, "Authorized"


__all__ = [
    "CURRENT_CHILD_WORK_SCHEMA_VERSION",
    "ChildWork",
    "ChildWorkBindingError",
    "ChildWorkError",
    "ChildWorkExpiredError",
    "ChildWorkIntegrityError",
    "ChildWorkSessionStaleError",
    "InvalidChildWorkTransition",
    "attach_child_work_reference",
    "authorize_child_work_inspection",
    "authorize_child_work_mutation",
    "cancel_child_works_for_parent",
    "get_child_work",
    "get_child_work_ids",
    "list_child_works_for_parent",
    "resume_child_work",
    "save_child_work",
    "validate_child_work_id",
]
