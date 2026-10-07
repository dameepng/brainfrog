"""Subagent Execution Integration Runtime for BrainFrog.

BrainFrog P1.3I — Real Subagent Execution Integration.

Architectural Principles:
- A subagent is a WORKER, not an authority.
- Parent Work is the COORDINATOR.
- orchestrator.py remains the SOLE canonical execution engine in BrainFrog.
- The subagent execution layer is an ADAPTER and COORDINATOR:
    - Zero execution authority (cannot run shell, subprocess, Git, or network).
    - Zero filesystem mutations (does not call write_text, open("w"), os.replace, shutil).
    - Zero capability issuance (cannot grant, widen, or attenuate capabilities).
    - Zero approval bypassing (DelegationContract != ApprovedExecutionContract).
    - Zero transaction bypassing (all side effects flow through the canonical Transaction layer).
- Canonical execution pipeline:
    Parent Work
         │ delegation
         ▼
    DelegationContract
         │
         ▼
    ChildWork
         │
         ▼
    DelegationRuntime (scheduling & claim eligibility)
         │
         ▼
    Approval Boundary (ApprovalService / ApprovedExecutionContract)
         │
         ▼
    Transaction Layer (TransactionCoordinator & TransactionStore)
         │
         ▼
    orchestrator.py (sole execution engine)
         │
         ▼
    Verification (VerificationResult)
         │
         ▼
    ChildResult -> ResultAggregator (P1.3F)
         │
         ▼
    Parent Work continuation
"""
from __future__ import annotations

import ast
import inspect
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple, Union

from core.runtime.capabilities import Capabilities
from core.runtime.child_work import (
    ChildWork,
    get_child_work,
    save_child_work,
    validate_child_work_id,
)
from core.runtime.contract import ApprovedExecutionContract, reject_secrets
from core.runtime.delegation import (
    DelegationContract,
    DelegationExpiredError,
    is_path_subset,
    validate_delegation_id,
)
from core.runtime.delegation_runtime import (
    CoordinationState,
    DelegationConcurrencyError,
    DelegationGroup,
    DelegationMode,
    DelegationRuntime,
    DelegationSessionStaleError,
    get_delegation_group,
    save_delegation_group,
    validate_delegation_group_id,
)
from core.runtime.failure_propagation import (
    CancellationPropagationPolicy,
    ParentFailurePolicy,
    propagate_child_cancellation,
    propagate_failure,
    reconcile_failure_and_cancellation,
)
from core.runtime.result_aggregation import (
    AggregateResult,
    AggregateStatus,
    AggregationPolicy,
    ArtifactReference,
    ChildResult,
    ResultAggregator,
)
from core.runtime.secret_scrubbing import scrub_secrets
from core.runtime.subagent import Subagent, SubagentStatus, validate_subagent_id
from core.runtime.work import (
    ACTIVE_WORK_STATUSES,
    StaleWorkRevisionError,
    TERMINAL_WORK_STATUSES,
    VerificationResult,
    VerificationStatus,
    Work,
    WorkFailure,
    WorkStatus,
    WorkStore,
)

logger = logging.getLogger(__name__)

CURRENT_SUBAGENT_EXECUTION_SCHEMA_VERSION = 1

# Resource limits (Phase P1.3I)
MAX_SUBAGENT_TASK_CHARS = 16_384
MAX_PARENT_SUMMARY_CHARS = 4_096
MAX_CONTEXT_CONSTRAINTS = 20
MAX_CONSTRAINT_CHARS = 512
MAX_CONTEXT_ARTIFACTS = 20
MAX_ARTIFACT_REF_CHARS = 512
MAX_EXECUTION_METADATA_BYTES = 16 * 1024

_INVALID_ID_CHARS = re.compile(r"[\x00-\x1f\x7f/\\:*\?\"<>\|]|(?:\.\.)")
_INVALID_SESSION_ID_CHARS = re.compile(r"[\x00-\x1f\x7f/\\*\?\"<>\|]|(?:\.\.)")


# =============================================================================
# 1. Custom Domain Exceptions
# =============================================================================

class SubagentExecutionError(ValueError):
    """Base exception for subagent execution errors."""
    pass


class SubagentExecutionBindingError(SubagentExecutionError):
    """Raised when identity, parent, actor, session, or delegation bindings mismatch."""
    pass


class SubagentExecutionSessionStaleError(SubagentExecutionError, PermissionError):
    """Raised when execution is attempted under a stale or invalidated session incarnation."""
    pass


class SubagentExecutionExpiredError(SubagentExecutionError, PermissionError):
    """Raised when execution is attempted on an expired delegation contract."""
    pass


class SubagentExecutionDependencyError(SubagentExecutionError):
    """Raised when prerequisite child dependencies are unsatisfied or failed."""
    pass


class SubagentExecutionStateError(SubagentExecutionError):
    """Raised when ChildWork or coordination state is invalid for execution."""
    pass


class SubagentExecutionBlockedError(SubagentExecutionStateError):
    """Raised when ChildWork is BLOCKED due to upstream failure or cancellation."""
    pass


class SubagentExecutionScopeError(SubagentExecutionError, PermissionError):
    """Raised when child execution scope exceeds parent delegation authority."""
    pass


class SubagentExecutionApprovalError(SubagentExecutionError, PermissionError):
    """Raised when approval requirements are bypassed or unfulfilled."""
    pass


# =============================================================================
# 2. Bounded Context and Request Models
# =============================================================================

@dataclass(frozen=True)
class SubagentExecutionContext:
    """Minimal, bounded, secret-scrubbed context provided to a delegated child task.

    Guarantees:
    - Never injects entire parent conversation history or unrelated plans.
    - Bounded strings and collections.
    - Zero secret leakage.
    """
    task: str
    parent_summary: str = ""
    relevant_artifacts: Tuple[str, ...] = ()
    constraints: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.task) is not str or not self.task.strip():
            raise SubagentExecutionBindingError("SubagentExecutionContext.task must be a non-empty string")
        clean_task = scrub_secrets(self.task.strip()[:MAX_SUBAGENT_TASK_CHARS])
        object.__setattr__(self, "task", clean_task)

        if type(self.parent_summary) is not str:
            raise SubagentExecutionBindingError("SubagentExecutionContext.parent_summary must be a string")
        clean_summary = scrub_secrets(self.parent_summary.strip()[:MAX_PARENT_SUMMARY_CHARS])
        object.__setattr__(self, "parent_summary", clean_summary)

        if type(self.relevant_artifacts) not in (list, tuple):
            raise SubagentExecutionBindingError("SubagentExecutionContext.relevant_artifacts must be a sequence")
        clean_artifacts = tuple(
            scrub_secrets(a.strip()[:MAX_ARTIFACT_REF_CHARS])
            for a in self.relevant_artifacts[:MAX_CONTEXT_ARTIFACTS]
            if a.strip()
        )
        object.__setattr__(self, "relevant_artifacts", clean_artifacts)

        if type(self.constraints) not in (list, tuple):
            raise SubagentExecutionBindingError("SubagentExecutionContext.constraints must be a sequence")
        clean_constraints = tuple(
            scrub_secrets(c.strip()[:MAX_CONSTRAINT_CHARS])
            for c in self.constraints[:MAX_CONTEXT_CONSTRAINTS]
            if c.strip()
        )
        object.__setattr__(self, "constraints", clean_constraints)

        reject_secrets(self.to_dict())

    def format_task_prompt(self) -> str:
        """Format bounded task prompt for execution without leaking parent conversation history."""
        parts = [f"Task: {self.task}"]
        if self.parent_summary:
            parts.append(f"Parent Context: {self.parent_summary}")
        if self.constraints:
            parts.append("Constraints:\n" + "\n".join(f"- {c}" for c in self.constraints))
        if self.relevant_artifacts:
            parts.append("Relevant Artifacts:\n" + "\n".join(f"- {a}" for a in self.relevant_artifacts))
        return "\n\n".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "task": self.task,
            "parent_summary": self.parent_summary,
            "relevant_artifacts": list(self.relevant_artifacts),
            "constraints": list(self.constraints),
        }
        reject_secrets(data)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SubagentExecutionContext:
        if not isinstance(data, dict):
            raise SubagentExecutionBindingError("SubagentExecutionContext data must be a dictionary")
        reject_secrets(data)
        return cls(
            task=str(data.get("task", "")),
            parent_summary=str(data.get("parent_summary", "")),
            relevant_artifacts=tuple(str(x) for x in data.get("relevant_artifacts", ())),
            constraints=tuple(str(x) for x in data.get("constraints", ())),
        )


@dataclass(frozen=True)
class SubagentExecutionRequest:
    """Immutable execution request binding a ChildWork to canonical execution.

    Must be verified against canonical ChildWork and DelegationContract before execution.
    """
    parent_work_id: str
    child_work_id: str
    subagent_id: str
    delegation_id: str
    actor: str
    session_id: str
    session_incarnation_id: str
    task: str
    context: Optional[SubagentExecutionContext] = None
    execution_contract: Optional[ApprovedExecutionContract] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    schema_version: int = CURRENT_SUBAGENT_EXECUTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("parent_work_id", "child_work_id", "subagent_id", "delegation_id", "actor", "session_id", "session_incarnation_id"):
            val = getattr(self, name)
            if type(val) is not str or not val.strip():
                raise SubagentExecutionBindingError(f"{name} must be a non-empty string")
            clean_val = val.strip()
            regex = _INVALID_SESSION_ID_CHARS if name == "session_id" else _INVALID_ID_CHARS
            if regex.search(clean_val) or clean_val.startswith((".", "~", "/", "\\")):
                raise SubagentExecutionBindingError(f"Invalid characters or traversal in {name}: '{clean_val}'")
            object.__setattr__(self, name, clean_val)

        if type(self.task) is not str or not self.task.strip():
            raise SubagentExecutionBindingError("task must be a non-empty string")
        object.__setattr__(self, "task", scrub_secrets(self.task.strip()[:MAX_SUBAGENT_TASK_CHARS]))

        if self.execution_contract is not None and not isinstance(self.execution_contract, ApprovedExecutionContract):
            raise SubagentExecutionBindingError("execution_contract must be an ApprovedExecutionContract")

        reject_secrets(self.to_dict())

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "schema_version": self.schema_version,
            "parent_work_id": self.parent_work_id,
            "child_work_id": self.child_work_id,
            "subagent_id": self.subagent_id,
            "delegation_id": self.delegation_id,
            "actor": self.actor,
            "session_id": self.session_id,
            "session_incarnation_id": self.session_incarnation_id,
            "task": self.task,
            "context": self.context.to_dict() if self.context else None,
            "has_execution_contract": self.execution_contract is not None,
            "metadata": dict(self.metadata),
        }
        reject_secrets(data)
        return data


@dataclass(frozen=True)
class SubagentExecutionResult:
    """Bounded, secret-scrubbed result model produced from child execution.

    Directly bridges to P1.3F ChildResult for ResultAggregator composition.
    """
    child_work_id: str
    subagent_id: str
    parent_work_id: str
    status: WorkStatus
    success: bool
    summary: str
    delegation_id: str = ""
    failure: Optional[WorkFailure] = None
    artifact_references: Tuple[ArtifactReference, ...] = ()
    verification_status: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)
    schema_version: int = CURRENT_SUBAGENT_EXECUTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("child_work_id", "subagent_id", "parent_work_id"):
            val = getattr(self, name)
            if type(val) is not str or not val.strip():
                raise SubagentExecutionBindingError(f"{name} must be a non-empty string")
            clean_val = val.strip()
            object.__setattr__(self, name, clean_val)

        if isinstance(self.status, str):
            try:
                object.__setattr__(self, "status", WorkStatus(self.status))
            except ValueError as exc:
                raise SubagentExecutionError(f"Invalid WorkStatus: {self.status}") from exc
        elif not isinstance(self.status, WorkStatus):
            raise SubagentExecutionError(f"status must be WorkStatus, got {type(self.status)}")

        if not isinstance(self.success, bool):
            raise SubagentExecutionError("success must be a boolean")

        if type(self.summary) is not str:
            raise SubagentExecutionError("summary must be a string")
        object.__setattr__(self, "summary", scrub_secrets(self.summary.strip()[:MAX_PARENT_SUMMARY_CHARS]))

        if self.failure is not None and not isinstance(self.failure, WorkFailure):
            raise SubagentExecutionError(f"failure must be WorkFailure, got {type(self.failure)}")

        # Scrub string metadata to ensure secret safety
        clean_meta = {}
        for k, v in self.metadata.items():
            if isinstance(v, str):
                clean_meta[k] = scrub_secrets(v)
            else:
                clean_meta[k] = v
        object.__setattr__(self, "metadata", clean_meta)

    def to_child_result(self, declared_order: int = 0) -> ChildResult:
        """Convert into canonical P1.3F ChildResult for ResultAggregator."""
        return ChildResult(
            child_work_id=self.child_work_id,
            subagent_id=self.subagent_id,
            delegation_id=self.delegation_id or self.subagent_id,
            declared_order=declared_order,
            work_status=self.status,
            success=self.success,
            result_summary=self.summary if self.success else "",
            artifact_references=self.artifact_references,
            failure_code=self.failure.code if self.failure else None,
            failure_message=self.failure.summary if self.failure else (self.summary if not self.success else None),
            completed_at=self.created_at,
            verification_status=self.verification_status,
        )

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "schema_version": self.schema_version,
            "child_work_id": self.child_work_id,
            "subagent_id": self.subagent_id,
            "parent_work_id": self.parent_work_id,
            "status": self.status.value,
            "success": self.success,
            "summary": self.summary,
            "failure_code": self.failure.code if self.failure else None,
            "failure_summary": self.failure.summary if self.failure else None,
            "verification_status": self.verification_status,
            "artifact_count": len(self.artifact_references),
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }
        return data


# =============================================================================
# 3. Subagent Execution Coordinator
# =============================================================================

class SubagentExecutionCoordinator:
    """Coordinates lifecycle, validation, and canonical execution handoff for Subagents.

    CRITICAL ARCHITECTURAL INVARIANT:
    - SubagentExecutionCoordinator is an ADAPTER and COORDINATOR.
    - It is NOT an execution engine.
    - It does NOT mutate the filesystem.
    - It does NOT execute shell commands or call subprocess.
    - It does NOT grant execution authority.
    - All actual execution is delegated strictly to orchestrator.py.
    """

    def __init__(
        self,
        *,
        work_store: WorkStore,
        repo_dir: Path,
        system1: Optional[Any] = None,
        system2: Optional[Any] = None,
        system1_factory: Optional[Callable[[str], Any]] = None,
        system2_factory: Optional[Callable[..., Any]] = None,
        approval_service: Optional[Any] = None,
        transaction_store: Optional[Any] = None,
        transaction_verifier: Optional[Any] = None,
        session_manager: Optional[Any] = None,
        session_incarnation_resolver: Optional[Callable[[str], Optional[str]]] = None,
        orchestrator_runner: Optional[Callable[[Any], Any]] = None,
        default_test_cmd: Optional[List[str]] = None,
    ) -> None:
        self.work_store = work_store
        self.repo_dir = repo_dir.resolve()
        self.system1 = system1
        self.system2 = system2
        self.system1_factory = system1_factory
        self.system2_factory = system2_factory
        self.approval_service = approval_service
        self.transaction_store = transaction_store
        self.transaction_verifier = transaction_verifier
        self.session_manager = session_manager
        self.session_incarnation_resolver = session_incarnation_resolver
        self.orchestrator_runner = orchestrator_runner
        self.default_test_cmd = default_test_cmd or ["pytest", "-q"]

    def _get_authoritative_incarnation(self, session_id: str) -> Optional[str]:
        if self.session_incarnation_resolver is not None:
            return self.session_incarnation_resolver(session_id)
        if self.session_manager is not None:
            from core.runtime.session_authority import current_session_incarnation
            return current_session_incarnation(self.session_manager, session_id)
        return None

    # -------------------------------------------------------------------------
    # Pre-Execution Validation
    # -------------------------------------------------------------------------

    def validate_request(
        self,
        request: SubagentExecutionRequest,
        child_work: ChildWork,
        delegation: DelegationContract,
        delegation_group: Optional[DelegationGroup] = None,
        *,
        current_time: Optional[float] = None,
        origin_channel: str = "cli",
    ) -> None:
        """Validate all identity, hierarchy, capability, and scheduling bounds fail-closed.

        Enforces Section 7:
        1. ChildWork exists.
        2. ChildWork belongs to expected parent.
        3. ChildWork belongs to expected subagent.
        4. ChildWork belongs to expected delegation.
        5. actor matches.
        6. session_id matches.
        7. session_incarnation_id matches.
        8. DelegationContract is valid.
        9. DelegationContract has not expired.
        10. ChildWork is eligible / claimed according to DelegationRuntime.
        11. Dependencies are satisfied.
        12. ChildWork is not terminal.
        13. No cancellation/failure propagation has invalidated it (not BLOCKED).
        14. Execution contract target scope does not exceed delegation, parent, or profile scope.
        15. Child cannot widen parent capabilities.
        """
        now = time.time() if current_time is None else float(current_time)

        # 1. Identity & hierarchy validation
        if child_work.parent_work_id != request.parent_work_id:
            raise SubagentExecutionBindingError(
                f"Parent mismatch: ChildWork belongs to '{child_work.parent_work_id}', request has '{request.parent_work_id}'"
            )
        if child_work.subagent_id != request.subagent_id:
            raise SubagentExecutionBindingError(
                f"Subagent mismatch: ChildWork belongs to '{child_work.subagent_id}', request has '{request.subagent_id}'"
            )
        if child_work.delegation_id != request.delegation_id:
            raise SubagentExecutionBindingError(
                f"Delegation mismatch: ChildWork belongs to '{child_work.delegation_id}', request has '{request.delegation_id}'"
            )
        if child_work.actor != request.actor:
            raise SubagentExecutionBindingError(
                f"Actor mismatch: ChildWork belongs to '{child_work.actor}', request has '{request.actor}'"
            )
        if child_work.session_id != request.session_id:
            raise SubagentExecutionBindingError(
                f"Session mismatch: ChildWork belongs to '{child_work.session_id}', request has '{request.session_id}'"
            )
        if child_work.session_incarnation_id != request.session_incarnation_id:
            raise SubagentExecutionSessionStaleError(
                f"Session incarnation stale: ChildWork has '{child_work.session_incarnation_id}', "
                f"request has '{request.session_incarnation_id}'"
            )

        # Authoritative server session incarnation check (SEC-P1.3-02)
        if self.session_manager is not None or self.session_incarnation_resolver is not None:
            authoritative_incarnation = self._get_authoritative_incarnation(request.session_id)
            if authoritative_incarnation != request.session_incarnation_id:
                raise SubagentExecutionSessionStaleError(
                    f"Session incarnation stale: server authoritative incarnation is '{authoritative_incarnation}', "
                    f"but request has '{request.session_incarnation_id}'"
                )

        # 2. Delegation Contract integrity & expiration
        delegation.validate_integrity()
        if delegation.is_expired or (delegation.expires_at > 0 and now >= delegation.expires_at):
            raise SubagentExecutionExpiredError(
                f"Delegation contract '{delegation.delegation_id}' expired at {delegation.expires_at}"
            )

        # 3. Terminal state check
        if child_work.status == WorkStatus.CANCELLED:
            raise SubagentExecutionStateError(f"ChildWork '{child_work.id}' was cancelled and cannot execute")
        if child_work.status == WorkStatus.FAILED:
            raise SubagentExecutionStateError(f"ChildWork '{child_work.id}' is failed and cannot execute")

        # 4. Delegation Group / Scheduling eligibility & dependency check
        if delegation_group is not None:
            cstate = delegation_group.coordination_states.get(child_work.id)
            if cstate == CoordinationState.BLOCKED:
                raise SubagentExecutionBlockedError(
                    f"ChildWork '{child_work.id}' is BLOCKED due to upstream failure or cancellation"
                )

            # Verify declared dependencies
            deps = delegation_group.dependencies.get(child_work.id, ())
            for dep_id in deps:
                dep_cw = get_child_work(self.work_store, dep_id)
                if dep_cw is None:
                    raise SubagentExecutionDependencyError(
                        f"ChildWork '{child_work.id}' dependency '{dep_id}' was not found in store"
                    )
                if dep_cw.status != WorkStatus.DONE:
                    raise SubagentExecutionDependencyError(
                        f"ChildWork '{child_work.id}' dependency '{dep_id}' is not DONE (status: {dep_cw.status})"
                    )

        # 5. Scope Boundary check (Section 8, SEC-P1.3-04, SEC-P1.3-03)
        if request.execution_contract is not None:
            contract = request.execution_contract
            eff_channel = str(origin_channel or request.metadata.get("channel") or "cli")
            contract.validate(
                actor=request.actor,
                channel=eff_channel,
                session_id=request.session_id,
                session_incarnation_id=request.session_incarnation_id,
                repo_dir=self.repo_dir,
            )

            # 5a. Invariant: Contract approved targets must not exceed child delegation target scope
            for target in contract.approved_targets:
                if not any(is_path_subset(target, ds) for ds in delegation.target_scope):
                    raise SubagentExecutionScopeError(
                        f"Execution contract target '{target}' exceeds delegated target scope {delegation.target_scope}"
                    )

            # 5b. Invariant: Target outside parent capability or scope rejected
            parent_work = self.work_store.get(child_work.parent_work_id)
            if parent_work is not None:
                if parent_work.capabilities is not None:
                    parent_write = parent_work.capabilities.filesystem.write
                    for target in contract.approved_targets:
                        if not any(is_path_subset(target, pw) for pw in parent_write):
                            raise SubagentExecutionScopeError(
                                f"Execution contract target '{target}' exceeds parent capability write scope {parent_write}"
                            )
                    # 5c. Invariant: Child cannot widen capabilities beyond parent
                    from core.runtime.delegation import attenuate_capabilities
                    try:
                        attenuate_capabilities(parent_work.capabilities, delegation.capabilities)
                        attenuate_capabilities(parent_work.capabilities, contract.capabilities)
                    except Exception as exc:
                        raise SubagentExecutionScopeError(
                            f"Execution contract capabilities widen parent capabilities: {exc}"
                        ) from exc

                if parent_work.scope:
                    for target in contract.approved_targets:
                        if not any(is_path_subset(target, ps) for ps in parent_work.scope):
                            raise SubagentExecutionScopeError(
                                f"Execution contract target '{target}' exceeds parent scope {parent_work.scope}"
                            )

            # 5d. Invariant: Target outside profile scope rejected
            profile_scope = None
            if child_work.subagent and child_work.subagent.metadata:
                profile_scope = (
                    child_work.subagent.metadata.get("profile_scope")
                    or child_work.subagent.metadata.get("filesystem_scope")
                )
            if profile_scope is None and request.metadata:
                profile_scope = (
                    request.metadata.get("profile_scope")
                    or request.metadata.get("filesystem_scope")
                )
            if profile_scope is not None:
                for target in contract.approved_targets:
                    if not any(is_path_subset(target, pr_s) for pr_s in profile_scope):
                        raise SubagentExecutionScopeError(
                            f"Execution contract target '{target}' exceeds subagent profile scope {profile_scope}"
                        )

    # -------------------------------------------------------------------------
    # Child Work Execution
    # -------------------------------------------------------------------------

    def execute_child(
        self,
        request: SubagentExecutionRequest,
        *,
        delegation_group: Optional[DelegationGroup] = None,
        child_work: Optional[ChildWork] = None,
        delegation: Optional[DelegationContract] = None,
        auto_claim: bool = True,
        origin_channel: str = "cli",
        parent_failure_policy: ParentFailurePolicy = ParentFailurePolicy.BEST_EFFORT,
    ) -> SubagentExecutionResult:
        """Execute a validated ChildWork through the canonical execution pipeline.

        Execution Flow:
        1. Pre-execution validation.
        2. Idempotent return if already DONE.
        3. Atomically claim coordination slot via DelegationRuntime.
        4. Check approval boundary: if unapproved, transition to APPROVAL_REQUIRED.
        5. Transition to EXECUTING via canonical Work lifecycle.
        6. Invoke canonical orchestrator.py with RunConfig.
        7. Transition to DONE or FAILED based on verification and outcome.
        8. Release claim & synchronize worker status.
        9. If failed, trigger FailurePropagation to block downstream dependents.
        """
        now = time.time()

        # 1. Resolve ChildWork and Delegation
        cw = child_work or get_child_work(self.work_store, request.child_work_id)
        if cw is None:
            raise SubagentExecutionBindingError(f"ChildWork '{request.child_work_id}' not found in store")

        delg = delegation or cw.delegation
        if delg is None and cw.delegation_id:
            child_meta = (cw.work.resume_metadata or {}).get("child_work") or {}
            delg_dict = child_meta.get("delegation")
            if isinstance(delg_dict, dict):
                try:
                    from core.runtime.delegation import DelegationContract
                    delg = DelegationContract.from_dict(delg_dict)
                    delg.validate_integrity()
                except Exception:
                    delg = None

        if delg is None:
            raise SubagentExecutionBindingError(f"DelegationContract '{request.delegation_id}' not found on ChildWork")

        # 2. Idempotence Check (Section 15)
        if cw.status == WorkStatus.DONE:
            meta = dict(cw.work.resume_metadata or {})
            summary = meta.get("result_summary", "Child work already completed successfully.")
            return SubagentExecutionResult(
                child_work_id=cw.id,
                subagent_id=cw.subagent_id,
                parent_work_id=cw.parent_work_id,
                status=WorkStatus.DONE,
                success=True,
                summary=summary,
                verification_status="PASS",
                created_at=now,
            )

        # Crash Recovery Check: Reconcile executed transaction without re-execution (Section 15)
        if cw.status in (WorkStatus.EXECUTING, WorkStatus.VERIFYING):
            return self._reconcile_crashed_execution(cw, request, delg, now, fail_closed_if_missing=False)

        # 3. Validate Request & Bounds
        self.validate_request(request, cw, delg, delegation_group, current_time=now, origin_channel=origin_channel)

        # 4. Claim Coordination Slot (Section 14)
        active_group = delegation_group
        if active_group is not None:
            cstate = active_group.coordination_states.get(cw.id)
            if cstate == CoordinationState.WAITING:
                # Re-evaluate eligibility
                cmap = {cw.id: cw}
                active_group = DelegationRuntime.reconcile(active_group, cmap)
                cstate = active_group.coordination_states.get(cw.id)

            if cstate == CoordinationState.RUNNABLE and auto_claim:
                cmap = {cw.id: cw}
                active_group, _ = DelegationRuntime.claim_child(
                    active_group,
                    cw.id,
                    cmap,
                    current_session_incarnation_id=request.session_incarnation_id,
                )
                save_delegation_group(self.work_store, active_group)
            elif cstate != CoordinationState.CLAIMED:
                raise SubagentExecutionStateError(
                    f"ChildWork '{cw.id}' cannot execute: coordination state is '{cstate}', expected CLAIMED"
                )

        # 5. Approval Boundary Check (Section 10, SEC-P1.3-01)
        # A DelegationContract is NOT an ApprovedExecutionContract.
        # If no execution contract is supplied, transition to APPROVAL_REQUIRED and return without execution.
        if request.execution_contract is None:
            cw_appr = self._advance_to_approval_required(cw, request, delg, now)
            return SubagentExecutionResult(
                child_work_id=cw.id,
                subagent_id=cw.subagent_id,
                parent_work_id=cw.parent_work_id,
                status=WorkStatus.APPROVAL_REQUIRED,
                success=False,
                summary=f"ChildWork '{cw.id}' requires an ApprovedExecutionContract before execution.",
                created_at=now,
            )

        # 6. Advance Child Work to EXECUTING (Atomic OCC claim)
        try:
            cw_exec = self._advance_to_executing(cw, request, delg, now)
        except StaleWorkRevisionError:
            fresh_cw = get_child_work(self.work_store, cw.id)
            if fresh_cw is not None and fresh_cw.status in (WorkStatus.EXECUTING, WorkStatus.VERIFYING, WorkStatus.DONE):
                return SubagentExecutionResult(
                    child_work_id=cw.id,
                    subagent_id=cw.subagent_id,
                    parent_work_id=cw.parent_work_id,
                    status=fresh_cw.status,
                    success=(fresh_cw.status == WorkStatus.DONE),
                    summary=f"Concurrent execution claimed by another worker (status: {fresh_cw.status.value}).",
                    verification_status="PASS" if fresh_cw.status == WorkStatus.DONE else None,
                    created_at=now,
                )
            raise

        # 7. Construct canonical RunConfig and invoke orchestrator.py
        task_text = request.context.format_task_prompt() if request.context else request.task
        step_results: List[Any] = []
        exec_error: Optional[Exception] = None

        from orchestrator import RunConfig, StepResult
        cfg = RunConfig(
            repo_dir=self.repo_dir,
            task=task_text,
            test_command=self.default_test_cmd,
            execution_contract=request.execution_contract,
            origin_channel=origin_channel,
            actor=request.actor,
            session_id=request.session_id,
            session_incarnation_id=request.session_incarnation_id,
            work_id=cw_exec.id,
            transaction_store=self.transaction_store,
            transaction_verifier=self.transaction_verifier,
            mode="build",
        )

        try:
            if self.orchestrator_runner is not None:
                step_results = self.orchestrator_runner(cfg)
            else:
                from orchestrator import Orchestrator
                from system1.typesafe_client import TypeSafeSystemOne
                from system2 import System2Client

                s1 = self.system1 or (self.system1_factory("fast") if self.system1_factory else TypeSafeSystemOne())
                s2 = self.system2 or (self.system2_factory() if self.system2_factory else System2Client())
                orch = Orchestrator(system1=s1, system2=s2, config=cfg)
                step_results = orch.run()
        except Exception as exc:
            exec_error = exc
            logger.warning("Canonical orchestrator execution failed for child %s: %s", cw.id, exc)

        # 8. Complete Execution & Lifecycle Outcome
        has_step_failure = any(
            getattr(sr, "exit_code", 0) != 0
            or getattr(sr, "outcome", "") in ("abandoned", "escalated", "unverified", "failed")
            for sr in step_results
        )
        failed_verification = any(
            getattr(sr, "verification", None) is not None
            and getattr(getattr(sr, "verification"), "status", None) not in (VerificationStatus.PASS, "PASS")
            for sr in step_results
        )

        if exec_error is not None or has_step_failure or failed_verification:
            fail_reason = (
                f"Execution failed: {exec_error}"
                if exec_error is not None
                else ("Step execution failed" if has_step_failure else "Step verification failed")
            )
            cw_failed = self._advance_to_failed(cw_exec, fail_reason, delg, now)
            save_child_work(self.work_store, cw_failed)

            # Propagate failure across delegation group to block downstream dependents (P1.3G)
            if active_group is not None:
                all_children = {cw_failed.id: cw_failed}
                for cid in active_group.child_work_ids:
                    if cid != cw_failed.id:
                        other_cw = get_child_work(self.work_store, cid)
                        if other_cw:
                            all_children[cid] = other_cw
                p_work = self.work_store.get(cw_failed.parent_work_id)
                upd_group, _, upd_parent, _ = propagate_failure(
                    active_group,
                    all_children,
                    failed_child_id=cw_failed.id,
                    parent_work=p_work,
                    parent_policy=parent_failure_policy,
                    current_session_incarnation_id=request.session_incarnation_id,
                )
                save_delegation_group(self.work_store, upd_group)
                if upd_parent is not None and p_work is not None and upd_parent.status != p_work.status:
                    fresh_parent = self.work_store.get(cw_failed.parent_work_id)
                    if fresh_parent is not None and not fresh_parent.is_terminal:
                        if upd_parent.is_terminal:
                            fresh_parent = fresh_parent.transition(
                                upd_parent.status,
                                failure=upd_parent.failure,
                                cancellation_reason=upd_parent.cancellation_reason,
                                updated_at=now,
                            )
                        else:
                            fresh_parent = fresh_parent.transition(
                                upd_parent.status,
                                updated_at=now,
                            )
                        self.work_store.save(fresh_parent)

            fail_obj = cw_failed.work.failure or WorkFailure(code="ERR_EXECUTION_FAILED", summary=fail_reason)
            return SubagentExecutionResult(
                child_work_id=cw_failed.id,
                subagent_id=cw_failed.subagent_id,
                parent_work_id=cw_failed.parent_work_id,
                status=WorkStatus.FAILED,
                success=False,
                summary=fail_reason,
                failure=fail_obj,
                created_at=now,
            )

        # Succeeded: Transition EXECUTING -> VERIFYING -> DONE
        cw_done = self._advance_to_done(cw_exec, step_results, delg, now)

        # Release coordination slot in delegation group
        if active_group is not None:
            active_group = replace(active_group, updated_at=now)
            save_delegation_group(self.work_store, active_group)

        summary_text = f"Completed {len(step_results)} step(s) successfully."
        if step_results and hasattr(step_results[0], "detail") and step_results[0].detail:
            summary_text = str(step_results[0].detail)[:MAX_PARENT_SUMMARY_CHARS]

        return SubagentExecutionResult(
            child_work_id=cw_done.id,
            subagent_id=cw_done.subagent_id,
            parent_work_id=cw_done.parent_work_id,
            status=WorkStatus.DONE,
            success=True,
            summary=summary_text,
            verification_status="PASS",
            created_at=now,
        )

    # -------------------------------------------------------------------------
    # Multi-Child Delegation Coordination
    # -------------------------------------------------------------------------

    def execute_group(
        self,
        parent_work_id: str,
        delegation_group_id: str,
        requests_map: Mapping[str, SubagentExecutionRequest],
        *,
        max_iterations: int = 50,
        aggregation_policy: AggregationPolicy = AggregationPolicy.ALL_REQUIRED,
        origin_channel: str = "cli",
        parent_failure_policy: ParentFailurePolicy = ParentFailurePolicy.BEST_EFFORT,
    ) -> Tuple[AggregateResult, Dict[str, SubagentExecutionResult]]:
        """Coordinate execution of all eligible children in a DelegationGroup.

        Handles sequential and parallel modes according to declared dependencies
        and max_concurrency, updates parent work resume metadata with composed
        results, and guarantees zero second orchestrator creation.
        """
        group = get_delegation_group(self.work_store, parent_work_id, delegation_group_id)
        if group is None:
            raise SubagentExecutionBindingError(f"DelegationGroup '{delegation_group_id}' not found in store")

        parent_work = self.work_store.get(parent_work_id)
        if parent_work is None:
            raise SubagentExecutionBindingError(f"Parent Work '{parent_work_id}' not found in store")

        execution_results: Dict[str, SubagentExecutionResult] = {}
        iterations = 0

        while iterations < max_iterations:
            iterations += 1

            # 1. Load current children
            children_map: Dict[str, ChildWork] = {}
            for cid in group.child_work_ids:
                c = get_child_work(self.work_store, cid)
                if c is not None:
                    children_map[cid] = c

            # 2. Check if all children are terminal or blocked
            non_term = [
                cid for cid, cw in children_map.items()
                if not cw.is_terminal and group.coordination_states.get(cid) != CoordinationState.BLOCKED
            ]
            if not non_term:
                break

            # 3. Evaluate and claim eligible RUNNABLE children
            group = DelegationRuntime.reconcile(group, children_map)
            group, dispatch_decisions = DelegationRuntime.claim_next(
                group,
                children_map,
                current_session_incarnation_id=group.session_incarnation_id,
            )
            save_delegation_group(self.work_store, group)

            if not dispatch_decisions:
                # No runnable children eligible (either blocked, waiting, or concurrency exhausted)
                break

            # 4. Execute claimed children
            for decision in dispatch_decisions:
                cid = decision.child_work_id
                req = requests_map.get(cid)
                if req is None:
                    # Construct minimal default request from child data
                    cw_item = children_map[cid]
                    req = SubagentExecutionRequest(
                        parent_work_id=parent_work_id,
                        child_work_id=cid,
                        subagent_id=cw_item.subagent_id,
                        delegation_id=cw_item.delegation_id,
                        actor=cw_item.actor,
                        session_id=cw_item.session_id,
                        session_incarnation_id=cw_item.session_incarnation_id,
                        task=cw_item.work.intent or f"Execute child task {cid}",
                    )

                res = self.execute_child(
                    req,
                    delegation_group=group,
                    child_work=children_map.get(cid),
                    auto_claim=False,
                    origin_channel=origin_channel,
                    parent_failure_policy=parent_failure_policy,
                )
                execution_results[cid] = res

                # Reload group after child execution in case failure propagation modified states
                upd_group = get_delegation_group(self.work_store, parent_work_id, delegation_group_id)
                if upd_group is not None:
                    group = upd_group

        # Reload parent work in case parent_failure_policy updated it (e.g. PROPAGATE_FAILURE)
        fresh_parent = self.work_store.get(parent_work_id)
        if fresh_parent is not None:
            parent_work = fresh_parent

        # 5. Aggregate results via P1.3F ResultAggregator
        final_children: List[ChildWork] = []
        for cid in group.child_work_ids:
            c = get_child_work(self.work_store, cid)
            if c is not None:
                final_children.append(c)

        agg_result = ResultAggregator.aggregate(
            parent_work,
            final_children,
            policy=aggregation_policy,
            delegation_group=group,
        )

        # 6. Bridge to Parent Work continuation eligibility (Section 19)
        # Terminal parent records cannot be mutated
        if not parent_work.is_terminal:
            updated_parent = ResultAggregator.attach_to_parent(self.work_store, parent_work_id, agg_result)
            meta = dict(updated_parent.resume_metadata or {})
            meta["aggregate_result"] = agg_result.to_dict()
            meta["children_completed"] = (agg_result.status == AggregateStatus.COMPLETE)
            final_parent = updated_parent.with_update(resume_metadata=meta, updated_at=time.time())
            self.work_store.save(final_parent)

        return agg_result, execution_results

    # -------------------------------------------------------------------------
    # Canonical Lifecycle Transition Helpers
    # -------------------------------------------------------------------------

    def _advance_to_approval_required(
        self,
        cw: ChildWork,
        request: SubagentExecutionRequest,
        delg: DelegationContract,
        now: float,
    ) -> ChildWork:
        """Advance ChildWork through valid transitions to APPROVAL_REQUIRED, saving each step."""
        curr = cw
        if curr.status == WorkStatus.CREATED:
            curr = curr.transition(WorkStatus.PLANNING, delegation=delg, current_time=now)
            curr = save_child_work(self.work_store, curr, expected_revision=cw.work.revision)
        if curr.status == WorkStatus.PLANNING:
            curr = curr.transition(WorkStatus.APPROVAL_REQUIRED, delegation=delg, current_time=now)
            curr = save_child_work(self.work_store, curr, expected_revision=curr.work.revision)
        return curr

    def _advance_to_executing(
        self,
        cw: ChildWork,
        request: SubagentExecutionRequest,
        delg: DelegationContract,
        now: float,
    ) -> ChildWork:
        """Advance ChildWork through valid transitions to EXECUTING, saving each step with OCC."""
        curr = cw
        if curr.status == WorkStatus.CREATED:
            curr = curr.transition(WorkStatus.PLANNING, delegation=delg, current_time=now)
            curr = save_child_work(self.work_store, curr, expected_revision=cw.work.revision)
        if curr.status == WorkStatus.PLANNING:
            curr = curr.transition(WorkStatus.APPROVAL_REQUIRED, delegation=delg, current_time=now)
            curr = save_child_work(self.work_store, curr, expected_revision=curr.work.revision)
        if curr.status == WorkStatus.APPROVAL_REQUIRED:
            curr = curr.transition(WorkStatus.EXECUTING, delegation=delg, current_time=now)
            curr = save_child_work(self.work_store, curr, expected_revision=curr.work.revision)
        return curr

    def _advance_to_failed(
        self,
        cw: ChildWork,
        error_msg: str,
        delg: DelegationContract,
        now: float,
    ) -> ChildWork:
        """Advance ChildWork to FAILED with attached WorkFailure."""
        fail_obj = WorkFailure(code="ERR_SUBAGENT_EXECUTION_FAILED", summary=error_msg)
        curr = cw.transition(WorkStatus.FAILED, failure=fail_obj, delegation=delg, current_time=now)
        return save_child_work(self.work_store, curr)

    def _advance_to_done(
        self,
        cw: ChildWork,
        step_results: Sequence[Any],
        delg: DelegationContract,
        now: float,
    ) -> ChildWork:
        """Advance ChildWork through VERIFYING to DONE with VerificationResult, saving each step."""
        curr = cw
        if curr.status == WorkStatus.EXECUTING:
            curr = curr.transition(WorkStatus.VERIFYING, delegation=delg, current_time=now)
            curr = save_child_work(self.work_store, curr)
        if curr.status == WorkStatus.VERIFYING:
            v_res = VerificationResult(status=VerificationStatus.PASS, summary="Execution steps verified")
            curr = curr.transition(WorkStatus.DONE, verification_result=v_res, delegation=delg, current_time=now)
            curr = save_child_work(self.work_store, curr)
        return curr

    def _reconcile_crashed_execution(
        self,
        cw: ChildWork,
        request: Optional[SubagentExecutionRequest],
        delg: DelegationContract,
        now: float,
        *,
        fail_closed_if_missing: bool = True,
    ) -> SubagentExecutionResult:
        """Reconcile an interrupted/crashed execution without re-executing."""
        # 1. Locate existing transaction
        from core.runtime.transaction import BasicFilesystemVerifier, TransactionStatus
        tx_id = (
            cw.work.transaction_id
            or (cw.work.resume_metadata or {}).get("transaction_id")
        )
        tx = None
        if self.transaction_store is not None:
            if tx_id:
                cand = self.transaction_store.get(tx_id)
                if cand is not None and getattr(cand, "work_id", None) == cw.id:
                    tx = cand
            else:
                for candidate in self.transaction_store.list_all():
                    if getattr(candidate, "work_id", None) == cw.id:
                        tx = candidate
                        break

        # Invariant: Transaction must be in an executed/verifying/committed state to be reconciled
        if tx is not None and tx.status not in (
            TransactionStatus.EXECUTING,
            TransactionStatus.VERIFYING,
            TransactionStatus.COMMITTED,
        ):
            tx = None

        # 2. If no transaction exists:
        if tx is None:
            if not fail_closed_if_missing:
                # Concurrent call while another worker is actively executing
                return SubagentExecutionResult(
                    child_work_id=cw.id,
                    subagent_id=cw.subagent_id,
                    parent_work_id=cw.parent_work_id,
                    status=cw.status,
                    success=False,
                    summary=f"Concurrent execution in progress by another worker (status: {cw.status.value}).",
                    created_at=now,
                )
            cw_failed = self._advance_to_failed(
                cw,
                "Crash recovery failed: missing transaction for executing child work",
                delg,
                now,
            )
            return SubagentExecutionResult(
                child_work_id=cw_failed.id,
                subagent_id=cw_failed.subagent_id,
                parent_work_id=cw_failed.parent_work_id,
                status=WorkStatus.FAILED,
                success=False,
                summary="Crash recovery failed: missing transaction in store",
                failure=cw_failed.work.failure,
                created_at=now,
            )

        # 3. Check transaction verification
        verifier = self.transaction_verifier or BasicFilesystemVerifier()
        passed, err_msg, details = verifier.verify(tx, self.repo_dir)

        if not passed:
            cw_failed = self._advance_to_failed(
                cw,
                f"Crash recovery verification failed: {err_msg}",
                delg,
                now,
            )
            return SubagentExecutionResult(
                child_work_id=cw_failed.id,
                subagent_id=cw_failed.subagent_id,
                parent_work_id=cw_failed.parent_work_id,
                status=WorkStatus.FAILED,
                success=False,
                summary=f"Crash recovery verification failed: {err_msg}",
                failure=cw_failed.work.failure,
                created_at=now,
            )

        # 4. If verified, finalize transaction commit
        if tx.status != TransactionStatus.COMMITTED:
            tx.commit_marker = {
                "marked_at": now,
                "status": "committed",
                "transaction_id": tx.id,
                "work_id": cw.id,
            }
            if tx.status == TransactionStatus.EXECUTING:
                tx.transition(TransactionStatus.VERIFYING)
            if tx.status == TransactionStatus.VERIFYING:
                tx.transition(TransactionStatus.COMMITTED)
            if self.transaction_store is not None:
                self.transaction_store.save(tx)

        # 5. Advance ChildWork to DONE without re-executing
        cw_done = self._advance_to_done(cw, [], delg, now)
        return SubagentExecutionResult(
            child_work_id=cw_done.id,
            subagent_id=cw_done.subagent_id,
            parent_work_id=cw_done.parent_work_id,
            status=WorkStatus.DONE,
            success=True,
            summary="Crash recovery reconciled executed transaction to DONE without re-execution",
            verification_status="PASS",
            created_at=now,
        )

    def reconcile_child(
        self,
        child_work_id: str,
        *,
        current_time: Optional[float] = None,
    ) -> SubagentExecutionResult:
        """Reconcile an interrupted/crashed child work execution without re-execution."""
        cw = get_child_work(self.work_store, child_work_id)
        if cw is None:
            raise SubagentExecutionBindingError(f"ChildWork '{child_work_id}' not found in store")
        delg = cw.delegation
        if delg is None and cw.delegation_id:
            child_meta = (cw.work.resume_metadata or {}).get("child_work") or {}
            delg_dict = child_meta.get("delegation")
            if isinstance(delg_dict, dict):
                try:
                    from core.runtime.delegation import DelegationContract
                    delg = DelegationContract.from_dict(delg_dict)
                except Exception:
                    delg = None
        if delg is None:
            raise SubagentExecutionBindingError(f"DelegationContract not found on ChildWork '{child_work_id}'")
        now = time.time() if current_time is None else float(current_time)
        return self._reconcile_crashed_execution(cw, None, delg, now)


__all__ = [
    "CURRENT_SUBAGENT_EXECUTION_SCHEMA_VERSION",
    "MAX_SUBAGENT_TASK_CHARS",
    "MAX_PARENT_SUMMARY_CHARS",
    "MAX_CONTEXT_CONSTRAINTS",
    "MAX_CONSTRAINT_CHARS",
    "MAX_CONTEXT_ARTIFACTS",
    "MAX_ARTIFACT_REF_CHARS",
    "MAX_EXECUTION_METADATA_BYTES",
    "SubagentExecutionError",
    "SubagentExecutionBindingError",
    "SubagentExecutionSessionStaleError",
    "SubagentExecutionExpiredError",
    "SubagentExecutionDependencyError",
    "SubagentExecutionStateError",
    "SubagentExecutionScopeError",
    "SubagentExecutionApprovalError",
    "SubagentExecutionContext",
    "SubagentExecutionRequest",
    "SubagentExecutionResult",
    "SubagentExecutionCoordinator",
]
