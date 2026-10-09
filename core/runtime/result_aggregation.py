"""Result Aggregation Domain Model and Deterministic Composition Runtime.

BrainFrog P1.3F — Result Aggregation.

Architectural Principles:
- A subagent is a WORKER, not an authority.
- Result aggregation is strictly an OBSERVATION / DATA COMPOSITION layer.
- It is NOT an authority layer.
- It holds NO execution authority, approval authority, or filesystem permissions.
- It cannot spawn orchestrators, mutate transactions, issue capabilities, or call LLMs.
- Canonical orchestrator.py remains the SOLE execution engine in BrainFrog.
- The canonical source of truth for child work results is persisted Work records in WorkStore.
- DelegationRuntime answers: "Which Child Work is eligible to proceed next?"
- Result Aggregator answers: "What did the Child Works produce, which succeeded/failed,
  and what bounded result should the Parent Work receive?"
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple, Union

from core.runtime.child_work import (
    ChildWork,
    get_child_work,
    get_child_work_ids,
    validate_child_work_id,
)
from core.runtime.contract import reject_secrets
from core.runtime.delegation_runtime import (
    DelegationGroup,
    get_delegation_group,
    validate_delegation_group_id,
)
from core.runtime.secret_scrubbing import scrub_secrets
from core.runtime.work import (
    ACTIVE_WORK_STATUSES,
    StaleWorkRevisionError,
    TERMINAL_WORK_STATUSES,
    Work,
    WorkFailure,
    WorkStatus,
    WorkStore,
)

CURRENT_RESULT_AGGREGATION_SCHEMA_VERSION = 1

# Resource bounds
MAX_AGGREGATE_CHILDREN = 50
MAX_SUMMARY_BYTES = 2048
MAX_FAILURE_MESSAGE_BYTES = 1024
MAX_ARTIFACTS_PER_CHILD = 20
MAX_TOTAL_ARTIFACT_REFERENCES = 100
MAX_AGGREGATE_BYTES = 64 * 1024  # 64 KB

_INVALID_ID_CHARS = re.compile(r"[\x00-\x1f\x7f/\\:*\?\"<>\|]|(?:\.\.)")


class ResultAggregationError(ValueError):
    """Base exception for result aggregation domain errors."""
    pass


class ResultAggregationBindingError(ResultAggregationError):
    """Raised when identity, parent, actor, session, or incarnation bindings mismatch."""
    pass


class ResultAggregationPolicyError(ResultAggregationError):
    """Raised when an unknown or invalid aggregation policy is specified."""
    pass


class ResultAggregationLimitError(ResultAggregationError):
    """Raised when resource limits are exceeded beyond bounded handling."""
    pass


class ResultAggregationIntegrityError(ResultAggregationError):
    """Raised when aggregate digest verification or tampering detection fails."""
    pass


def _validate_binding_field(val: Any, name: str) -> str:
    """Validate mandatory binding fields."""
    if type(val) is not str:
        raise ResultAggregationBindingError(f"{name} must be a string, got {type(val)}")
    clean = val.strip()
    if not clean:
        raise ResultAggregationBindingError(f"{name} cannot be empty")
    if _INVALID_ID_CHARS.search(clean) or clean.startswith((".", "~", "/", "\\")):
        raise ResultAggregationBindingError(f"Invalid characters or traversal in {name}: '{clean}'")
    return clean


class AggregationPolicy(str, Enum):
    """Deterministic policy governing child work result aggregation."""
    ALL_REQUIRED = "all_required"
    ALLOW_PARTIAL = "allow_partial"


class AggregateStatus(str, Enum):
    """Deterministic outcome statuses for an aggregated set of child works."""
    COMPLETE = "complete"
    PARTIAL = "partial"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


@dataclass(frozen=True)
class ArtifactReference:
    """Bounded, immutable data reference to an artifact produced by a child work.

    CRITICAL ARCHITECTURAL INVARIANT:
    An ArtifactReference is strictly inert DATA.
    It confers NO filesystem permissions, execution capabilities, or ambient authority.
    """
    artifact_id: str
    path: str
    mime_type: Optional[str] = None
    digest: Optional[str] = None
    size_bytes: Optional[int] = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.artifact_id) is not str or not self.artifact_id.strip():
            raise ResultAggregationBindingError("artifact_id must be a non-empty string")
        clean_id = self.artifact_id.strip()[:128]
        if _INVALID_ID_CHARS.search(clean_id) or clean_id.startswith((".", "~", "/", "\\")):
            raise ResultAggregationBindingError(f"Invalid characters in artifact_id: '{clean_id}'")
        object.__setattr__(self, "artifact_id", clean_id)

        if type(self.path) is not str or not self.path.strip():
            raise ResultAggregationBindingError("path must be a non-empty string")
        clean_path = self.path.strip()[:512]
        if ".." in clean_path or clean_path.startswith(("/", "\\")):
            raise ResultAggregationBindingError(
                f"Path traversal or absolute path forbidden in ArtifactReference: '{clean_path}'"
            )
        object.__setattr__(self, "path", clean_path)

        if self.mime_type is not None:
            if type(self.mime_type) is not str:
                raise ResultAggregationBindingError("mime_type must be a string or None")
            object.__setattr__(self, "mime_type", self.mime_type.strip()[:128])

        if self.digest is not None:
            if type(self.digest) is not str:
                raise ResultAggregationBindingError("digest must be a string or None")
            object.__setattr__(self, "digest", self.digest.strip()[:128])

        if self.size_bytes is not None:
            if not isinstance(self.size_bytes, int) or isinstance(self.size_bytes, bool) or self.size_bytes < 0:
                raise ResultAggregationBindingError("size_bytes must be a non-negative integer or None")
            object.__setattr__(self, "size_bytes", self.size_bytes)

        reject_secrets({
            "artifact_id": self.artifact_id,
            "path": self.path,
            "mime_type": self.mime_type or "",
            "digest": self.digest or "",
        })

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "schema_version": self.schema_version,
            "artifact_id": self.artifact_id,
            "path": self.path,
            "mime_type": self.mime_type,
            "digest": self.digest,
            "size_bytes": self.size_bytes,
        }
        reject_secrets(data)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ArtifactReference:
        if not isinstance(data, dict):
            raise ResultAggregationBindingError("ArtifactReference data must be a dictionary")
        reject_secrets(data)
        return cls(
            artifact_id=str(data.get("artifact_id", "")),
            path=str(data.get("path", "")),
            mime_type=data.get("mime_type"),
            digest=data.get("digest"),
            size_bytes=data.get("size_bytes"),
            schema_version=int(data.get("schema_version", 1)),
        )


@dataclass(frozen=True)
class ChildResult:
    """Bounded observable outcome of a single Child Work.

    Contains strictly the data required for aggregation and parent inspection.
    Holds NO execution authority, approval nonces, credentials, or security tokens.
    """
    child_work_id: str
    subagent_id: str
    delegation_id: str
    declared_order: int
    work_status: WorkStatus
    success: bool
    result_summary: str = ""
    artifact_references: Tuple[ArtifactReference, ...] = ()
    failure_code: Optional[str] = None
    failure_message: Optional[str] = None
    cancellation_reason: Optional[str] = None
    completed_at: Optional[float] = None
    verification_status: Optional[str] = None
    schema_version: int = CURRENT_RESULT_AGGREGATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        clean_cid = validate_child_work_id(self.child_work_id)
        object.__setattr__(self, "child_work_id", clean_cid)

        clean_sub = _validate_binding_field(self.subagent_id, "subagent_id")
        object.__setattr__(self, "subagent_id", clean_sub)

        clean_delg = _validate_binding_field(self.delegation_id, "delegation_id")
        object.__setattr__(self, "delegation_id", clean_delg)

        if not isinstance(self.declared_order, int) or isinstance(self.declared_order, bool) or self.declared_order < 0:
            raise ResultAggregationBindingError("declared_order must be a non-negative integer")

        if isinstance(self.work_status, str):
            try:
                object.__setattr__(self, "work_status", WorkStatus(self.work_status))
            except ValueError as exc:
                raise ResultAggregationBindingError(f"Invalid work status: {self.work_status}") from exc
        elif not isinstance(self.work_status, WorkStatus):
            raise ResultAggregationBindingError(f"work_status must be a WorkStatus enum, got {type(self.work_status)}")

        if not isinstance(self.success, bool):
            raise ResultAggregationBindingError("success must be a boolean")

        # Scrub and bound result summary
        clean_summary = scrub_secrets(self.result_summary or "")
        if len(clean_summary) > MAX_SUMMARY_BYTES:
            clean_summary = clean_summary[:MAX_SUMMARY_BYTES - 16] + "... [truncated]"
        object.__setattr__(self, "result_summary", clean_summary)

        # Artifact references validation and bounding
        if not isinstance(self.artifact_references, (tuple, list)):
            raise ResultAggregationBindingError("artifact_references must be a sequence")
        parsed_artifacts: List[ArtifactReference] = []
        for a in self.artifact_references:
            if isinstance(a, ArtifactReference):
                parsed_artifacts.append(a)
            elif isinstance(a, dict):
                parsed_artifacts.append(ArtifactReference.from_dict(a))
            else:
                raise ResultAggregationBindingError(f"Invalid artifact reference element: {type(a)}")
        if len(parsed_artifacts) > MAX_ARTIFACTS_PER_CHILD:
            parsed_artifacts = parsed_artifacts[:MAX_ARTIFACTS_PER_CHILD]
        object.__setattr__(self, "artifact_references", tuple(parsed_artifacts))

        # Failure fields
        if self.failure_code is not None:
            clean_fc = scrub_secrets(self.failure_code)[:64]
            object.__setattr__(self, "failure_code", clean_fc)

        if self.failure_message is not None:
            clean_fm = scrub_secrets(self.failure_message)
            if len(clean_fm) > MAX_FAILURE_MESSAGE_BYTES:
                clean_fm = clean_fm[:MAX_FAILURE_MESSAGE_BYTES - 16] + "... [truncated]"
            object.__setattr__(self, "failure_message", clean_fm)

        if self.cancellation_reason is not None:
            clean_cr = scrub_secrets(self.cancellation_reason)
            if len(clean_cr) > MAX_FAILURE_MESSAGE_BYTES:
                clean_cr = clean_cr[:MAX_FAILURE_MESSAGE_BYTES - 16] + "... [truncated]"
            object.__setattr__(self, "cancellation_reason", clean_cr)

        if self.completed_at is not None:
            if not isinstance(self.completed_at, (int, float)) or not math.isfinite(self.completed_at):
                raise ResultAggregationBindingError("completed_at must be a valid finite number")
            object.__setattr__(self, "completed_at", float(self.completed_at))

        if self.verification_status is not None:
            object.__setattr__(self, "verification_status", self.verification_status[:32])

        # Reject secrets across all text fields
        reject_secrets({
            "child_work_id": self.child_work_id,
            "subagent_id": self.subagent_id,
            "delegation_id": self.delegation_id,
            "result_summary": self.result_summary,
            "failure_code": self.failure_code or "",
            "failure_message": self.failure_message or "",
            "cancellation_reason": self.cancellation_reason or "",
        })

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "schema_version": self.schema_version,
            "child_work_id": self.child_work_id,
            "subagent_id": self.subagent_id,
            "delegation_id": self.delegation_id,
            "declared_order": self.declared_order,
            "work_status": self.work_status.value,
            "success": self.success,
            "result_summary": self.result_summary,
            "artifact_references": [a.to_dict() for a in self.artifact_references],
            "failure_code": self.failure_code,
            "failure_message": self.failure_message,
            "cancellation_reason": self.cancellation_reason,
            "completed_at": self.completed_at,
            "verification_status": self.verification_status,
        }
        reject_secrets(data)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ChildResult:
        if not isinstance(data, dict):
            raise ResultAggregationBindingError("ChildResult data must be a dictionary")
        reject_secrets(data)
        raw_artifacts = data.get("artifact_references") or ()
        parsed_arts = tuple(
            ArtifactReference.from_dict(a) if isinstance(a, dict) else a
            for a in raw_artifacts
        )
        return cls(
            child_work_id=str(data.get("child_work_id", "")),
            subagent_id=str(data.get("subagent_id", "")),
            delegation_id=str(data.get("delegation_id", "")),
            declared_order=int(data.get("declared_order", 0)),
            work_status=WorkStatus(str(data.get("work_status", "created"))),
            success=bool(data.get("success", False)),
            result_summary=str(data.get("result_summary", "")),
            artifact_references=parsed_arts,
            failure_code=data.get("failure_code"),
            failure_message=data.get("failure_message"),
            cancellation_reason=data.get("cancellation_reason"),
            completed_at=float(data["completed_at"]) if data.get("completed_at") is not None else None,
            verification_status=data.get("verification_status"),
            schema_version=int(data.get("schema_version", CURRENT_RESULT_AGGREGATION_SCHEMA_VERSION)),
        )


@dataclass(frozen=True)
class AggregateResult:
    """Bounded aggregate outcome representing the composed results of Child Works.

    CRITICAL ARCHITECTURAL INVARIANT:
    AggregateResult is strictly observational DATA for Parent Work consumption.
    It confers NO execution authority, approval authority, or capability grants.
    """
    parent_work_id: str
    delegation_group_id: Optional[str]
    policy: AggregationPolicy
    status: AggregateStatus
    success: bool
    total_children: int
    completed_children: int
    successful_children: int
    failed_children: int
    cancelled_children: int
    incomplete_children: int
    included_children: int
    omitted_children: int = 0
    truncated: bool = False
    children: Tuple[ChildResult, ...] = ()
    aggregate_digest: str = ""
    created_at: float = field(default_factory=time.time)
    schema_version: int = CURRENT_RESULT_AGGREGATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        clean_pid = _validate_binding_field(self.parent_work_id, "parent_work_id")
        object.__setattr__(self, "parent_work_id", clean_pid)

        if self.delegation_group_id is not None:
            clean_gid = validate_delegation_group_id(self.delegation_group_id)
            object.__setattr__(self, "delegation_group_id", clean_gid)

        if isinstance(self.policy, str):
            try:
                object.__setattr__(self, "policy", AggregationPolicy(self.policy))
            except ValueError as exc:
                raise ResultAggregationPolicyError(f"Invalid policy: {self.policy}") from exc
        elif not isinstance(self.policy, AggregationPolicy):
            raise ResultAggregationPolicyError(f"Invalid policy type: {type(self.policy)}")

        if isinstance(self.status, str):
            try:
                object.__setattr__(self, "status", AggregateStatus(self.status))
            except ValueError as exc:
                raise ResultAggregationPolicyError(f"Invalid status: {self.status}") from exc
        elif not isinstance(self.status, AggregateStatus):
            raise ResultAggregationPolicyError(f"Invalid status type: {type(self.status)}")

        if not isinstance(self.success, bool):
            raise ResultAggregationBindingError("success must be a boolean")

        # Validate counts
        for count_name in (
            "total_children", "completed_children", "successful_children",
            "failed_children", "cancelled_children", "incomplete_children",
            "included_children", "omitted_children"
        ):
            val = getattr(self, count_name)
            if not isinstance(val, int) or isinstance(val, bool) or val < 0:
                raise ResultAggregationBindingError(f"{count_name} must be a non-negative integer")

        if not isinstance(self.children, (tuple, list)):
            raise ResultAggregationBindingError("children must be a sequence of ChildResult")
        parsed_children = tuple(
            ChildResult.from_dict(c) if isinstance(c, dict) else c
            for c in self.children
        )
        object.__setattr__(self, "children", parsed_children)

        if not isinstance(self.created_at, (int, float)) or not math.isfinite(self.created_at):
            raise ResultAggregationBindingError("created_at must be a valid finite number")
        object.__setattr__(self, "created_at", float(self.created_at))

        # Compute digest if missing
        if not self.aggregate_digest:
            object.__setattr__(self, "aggregate_digest", self.compute_digest())

        # Secret rejection on summary representation
        reject_secrets(self.to_summary_dict())

    def compute_digest(self) -> str:
        """Compute deterministic SHA-256 digest over canonical aggregate state.

        Excludes volatile created_at timestamp so that identical states yield identical digests.
        """
        canonical_data = {
            "schema_version": self.schema_version,
            "parent_work_id": self.parent_work_id,
            "delegation_group_id": self.delegation_group_id,
            "policy": self.policy.value,
            "status": self.status.value,
            "success": self.success,
            "total_children": self.total_children,
            "completed_children": self.completed_children,
            "successful_children": self.successful_children,
            "failed_children": self.failed_children,
            "cancelled_children": self.cancelled_children,
            "incomplete_children": self.incomplete_children,
            "omitted_children": self.omitted_children,
            "truncated": self.truncated,
            "children": [
                {
                    "child_work_id": c.child_work_id,
                    "subagent_id": c.subagent_id,
                    "delegation_id": c.delegation_id,
                    "declared_order": c.declared_order,
                    "work_status": c.work_status.value,
                    "success": c.success,
                    "result_summary": c.result_summary,
                    "artifact_references": [
                        {
                            "artifact_id": a.artifact_id,
                            "path": a.path,
                            "mime_type": a.mime_type,
                            "digest": a.digest,
                            "size_bytes": a.size_bytes,
                        }
                        for a in c.artifact_references
                    ],
                    "failure_code": c.failure_code,
                    "failure_message": c.failure_message,
                    "cancellation_reason": c.cancellation_reason,
                    "verification_status": c.verification_status,
                }
                for c in self.children
            ],
        }
        canonical_json = json.dumps(canonical_data, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    def to_summary_dict(self) -> Dict[str, Any]:
        """Return a bounded, compact summary dictionary suitable for Parent Work metadata."""
        data = {
            "schema_version": self.schema_version,
            "parent_work_id": self.parent_work_id,
            "delegation_group_id": self.delegation_group_id,
            "policy": self.policy.value,
            "status": self.status.value,
            "success": self.success,
            "total_children": self.total_children,
            "completed_children": self.completed_children,
            "successful_children": self.successful_children,
            "failed_children": self.failed_children,
            "cancelled_children": self.cancelled_children,
            "incomplete_children": self.incomplete_children,
            "included_children": self.included_children,
            "omitted_children": self.omitted_children,
            "truncated": self.truncated,
            "aggregate_digest": self.aggregate_digest,
            "created_at": self.created_at,
        }
        reject_secrets(data)
        return data

    def to_dict(self) -> Dict[str, Any]:
        """Serialize full AggregateResult to dictionary, enforcing secret safety and bounds."""
        data = {
            "schema_version": self.schema_version,
            "parent_work_id": self.parent_work_id,
            "delegation_group_id": self.delegation_group_id,
            "policy": self.policy.value,
            "status": self.status.value,
            "success": self.success,
            "total_children": self.total_children,
            "completed_children": self.completed_children,
            "successful_children": self.successful_children,
            "failed_children": self.failed_children,
            "cancelled_children": self.cancelled_children,
            "incomplete_children": self.incomplete_children,
            "included_children": self.included_children,
            "omitted_children": self.omitted_children,
            "truncated": self.truncated,
            "aggregate_digest": self.aggregate_digest,
            "created_at": self.created_at,
            "children": [c.to_dict() for c in self.children],
        }
        reject_secrets(data)
        raw_bytes = len(json.dumps(data, sort_keys=True).encode("utf-8"))
        if raw_bytes > MAX_AGGREGATE_BYTES:
            raise ResultAggregationLimitError(
                f"AggregateResult serialized size ({raw_bytes} bytes) exceeds MAX_AGGREGATE_BYTES ({MAX_AGGREGATE_BYTES})"
            )
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> AggregateResult:
        if not isinstance(data, dict):
            raise ResultAggregationBindingError("AggregateResult data must be a dictionary")
        reject_secrets(data)
        raw_children = data.get("children") or ()
        parsed_children = tuple(
            ChildResult.from_dict(c) if isinstance(c, dict) else c
            for c in raw_children
        )
        return cls(
            parent_work_id=str(data.get("parent_work_id", "")),
            delegation_group_id=data.get("delegation_group_id"),
            policy=AggregationPolicy(str(data.get("policy", "all_required"))),
            status=AggregateStatus(str(data.get("status", "complete"))),
            success=bool(data.get("success", False)),
            total_children=int(data.get("total_children", 0)),
            completed_children=int(data.get("completed_children", 0)),
            successful_children=int(data.get("successful_children", 0)),
            failed_children=int(data.get("failed_children", 0)),
            cancelled_children=int(data.get("cancelled_children", 0)),
            incomplete_children=int(data.get("incomplete_children", 0)),
            included_children=int(data.get("included_children", len(parsed_children))),
            omitted_children=int(data.get("omitted_children", 0)),
            truncated=bool(data.get("truncated", False)),
            children=parsed_children,
            aggregate_digest=str(data.get("aggregate_digest", "")),
            created_at=float(data.get("created_at", time.time())),
            schema_version=int(data.get("schema_version", CURRENT_RESULT_AGGREGATION_SCHEMA_VERSION)),
        )


def build_child_result(
    child: Union[ChildWork, Work],
    declared_order: int = 0,
) -> ChildResult:
    """Extract and bound a deterministic ChildResult from a canonical ChildWork or child Work record."""
    if isinstance(child, ChildWork):
        w = child.work
        cid = child.child_work_id
        sid = child.subagent_id
        did = child.delegation_id
    elif isinstance(child, Work):
        w = child
        cid = child.id
        meta = child.resume_metadata or {}
        cmeta = meta.get("child_work") or {}
        sid = str(cmeta.get("subagent_id", ""))
        did = str(cmeta.get("delegation_id", ""))
    else:
        raise ResultAggregationBindingError(f"Expected ChildWork or Work instance, got {type(child)}")

    meta = w.resume_metadata or {}
    status = w.status

    # Extract summary, failure, and artifacts based on status
    if status == WorkStatus.DONE:
        success = True
        summary = meta.get("result_summary") or meta.get("summary")
        if not summary and w.verification_result:
            summary = w.verification_result.summary
        if not summary:
            summary = w.title or w.goal or "Child work completed successfully."
        failure_code = None
        failure_message = None
        cancellation_reason = None
    elif status == WorkStatus.FAILED:
        success = False
        failure_code = w.failure.code if w.failure else "EXECUTION_ERROR"
        failure_message = w.failure.summary if w.failure else "Child work failed."
        summary = failure_message
        cancellation_reason = None
    elif status == WorkStatus.CANCELLED:
        success = False
        failure_code = None
        failure_message = None
        cancellation_reason = w.cancellation_reason or "Child work cancelled."
        summary = cancellation_reason
    else:
        success = False
        failure_code = None
        failure_message = None
        cancellation_reason = None
        summary = f"Child work in progress ({status.value})."

    # Extract artifacts
    raw_artifacts = meta.get("artifact_references") or meta.get("artifacts") or ()
    parsed_artifacts: List[ArtifactReference] = []
    if isinstance(raw_artifacts, (list, tuple)):
        for item in raw_artifacts:
            if isinstance(item, ArtifactReference):
                parsed_artifacts.append(item)
            elif isinstance(item, dict):
                try:
                    parsed_artifacts.append(ArtifactReference.from_dict(item))
                except Exception:
                    continue
            elif isinstance(item, str):
                parsed_artifacts.append(
                    ArtifactReference(
                        artifact_id=f"art_{len(parsed_artifacts):02d}",
                        path=item,
                    )
                )

    verification_status = str(w.verification_result.status) if w.verification_result else None

    return ChildResult(
        child_work_id=cid,
        subagent_id=sid,
        delegation_id=did,
        declared_order=declared_order,
        work_status=status,
        success=success,
        result_summary=str(summary),
        artifact_references=tuple(parsed_artifacts),
        failure_code=failure_code,
        failure_message=failure_message,
        cancellation_reason=cancellation_reason,
        completed_at=w.completed_at,
        verification_status=verification_status,
    )


def aggregate_children(
    parent_work: Work,
    children: Sequence[Union[ChildWork, Work]],
    policy: Union[AggregationPolicy, str] = AggregationPolicy.ALL_REQUIRED,
    delegation_group: Optional[DelegationGroup] = None,
    delegation_group_id: Optional[str] = None,
    max_children: int = MAX_AGGREGATE_CHILDREN,
    current_time: Optional[float] = None,
) -> AggregateResult:
    """Deterministically aggregate child results for a Parent Work.

    Enforces:
    - Parent Work actor, session, and incarnation binding validation.
    - Child-to-parent identity correspondence (foreign children fail closed).
    - Policy semantics (ALL_REQUIRED vs ALLOW_PARTIAL).
    - Incomplete semantics (non-terminal child -> INCOMPLETE).
    - Bounded resources and explicit truncation metadata.
    - Idempotent and pure observational computation (no mutations).
    """
    now = time.time() if current_time is None else float(current_time)

    # 1. Validate Parent Work identity bindings
    parent_actor = parent_work.actor_id or getattr(parent_work, "actor", "")
    parent_session = parent_work.session_id or ""
    parent_inc = parent_work.session_incarnation_id or ""

    if not parent_actor:
        raise ResultAggregationBindingError("Parent work lacks an authenticated actor")
    if not parent_session:
        raise ResultAggregationBindingError("Parent work lacks a session binding")
    if not parent_inc:
        raise ResultAggregationBindingError("Parent work lacks a session incarnation binding")

    # 2. Validate DelegationGroup bindings if provided
    eff_group_id: Optional[str] = None
    if delegation_group is not None:
        if delegation_group.parent_work_id != parent_work.id:
            raise ResultAggregationBindingError(
                f"DelegationGroup parent mismatch: '{delegation_group.parent_work_id}' vs '{parent_work.id}'"
            )
        if delegation_group.actor != parent_actor:
            raise ResultAggregationBindingError(
                f"DelegationGroup actor mismatch: '{delegation_group.actor}' vs '{parent_actor}'"
            )
        if delegation_group.session_id != parent_session:
            raise ResultAggregationBindingError(
                f"DelegationGroup session mismatch: '{delegation_group.session_id}' vs '{parent_session}'"
            )
        if delegation_group.session_incarnation_id != parent_inc:
            raise ResultAggregationBindingError(
                f"DelegationGroup incarnation mismatch: '{delegation_group.session_incarnation_id}' vs '{parent_inc}'"
            )
        eff_group_id = delegation_group.delegation_group_id
    elif delegation_group_id is not None:
        eff_group_id = validate_delegation_group_id(delegation_group_id)

    # 3. Validate policy
    if isinstance(policy, str):
        try:
            eff_policy = AggregationPolicy(policy.lower())
        except (ValueError, TypeError) as exc:
            raise ResultAggregationPolicyError(f"Invalid aggregation policy: '{policy}'") from exc
    elif isinstance(policy, AggregationPolicy):
        eff_policy = policy
    else:
        raise ResultAggregationPolicyError(f"Invalid policy type: {type(policy)}")

    # 4. Validate EVERY child for parent binding and index by child_work_id
    children_by_id: Dict[str, Union[ChildWork, Work]] = {}
    for c in children:
        if isinstance(c, ChildWork):
            cid = c.child_work_id
            c_parent = c.parent_work_id
            c_actor = c.actor
            c_session = c.session_id
            c_inc = c.session_incarnation_id
        elif isinstance(c, Work):
            cid = c.id
            cmeta = (c.resume_metadata or {}).get("child_work") or {}
            c_parent = cmeta.get("parent_work_id") or c.parent_work_id or ""
            c_actor = cmeta.get("actor") or c.actor_id or ""
            c_session = cmeta.get("session_id") or c.session_id or ""
            c_inc = cmeta.get("session_incarnation_id") or c.session_incarnation_id or ""
        else:
            raise ResultAggregationBindingError(f"Expected ChildWork or Work, got {type(c)}")

        if c_parent != parent_work.id:
            raise ResultAggregationBindingError(
                f"Child work '{cid}' parent mismatch: '{c_parent}' does not match expected parent '{parent_work.id}'"
            )
        if c_actor != parent_actor:
            raise ResultAggregationBindingError(
                f"Child work '{cid}' actor mismatch: '{c_actor}' does not match expected actor '{parent_actor}'"
            )
        if c_session != parent_session:
            raise ResultAggregationBindingError(
                f"Child work '{cid}' session mismatch: '{c_session}' does not match expected session '{parent_session}'"
            )
        if c_inc != parent_inc:
            raise ResultAggregationBindingError(
                f"Child work '{cid}' incarnation mismatch: '{c_inc}' does not match expected incarnation '{parent_inc}'"
            )

        if delegation_group is not None and cid not in delegation_group.child_work_ids:
            raise ResultAggregationBindingError(
                f"Child work '{cid}' is not declared in delegation group '{delegation_group.delegation_group_id}'"
            )

        children_by_id[cid] = c

    # 5. Establish deterministic child ordering
    if delegation_group is not None:
        missing = [cid for cid in delegation_group.child_work_ids if cid not in children_by_id]
        if missing:
            raise ResultAggregationBindingError(
                f"Missing children declared in delegation group: {missing}"
            )
        ordered_children = [children_by_id[cid] for cid in delegation_group.child_work_ids]
    else:
        # Use parent resume_metadata child order if available, tie-breaking by child ID
        parent_child_ids = get_child_work_ids(parent_work)
        ordered_cids: List[str] = []
        for pcid in parent_child_ids:
            if pcid in children_by_id and pcid not in ordered_cids:
                ordered_cids.append(pcid)
        remaining_cids = sorted(k for k in children_by_id.keys() if k not in ordered_cids)
        ordered_cids.extend(remaining_cids)
        ordered_children = [children_by_id[cid] for cid in ordered_cids]

    # 6. Build ChildResults with stable declared_order
    all_child_results = tuple(
        build_child_result(c, declared_order=idx)
        for idx, c in enumerate(ordered_children)
    )

    # 7. Compute semantic status and counts over all children
    total_children = len(all_child_results)
    completed_children = sum(1 for c in all_child_results if c.work_status in TERMINAL_WORK_STATUSES)
    successful_children = sum(1 for c in all_child_results if c.work_status == WorkStatus.DONE)
    failed_children = sum(1 for c in all_child_results if c.work_status == WorkStatus.FAILED)
    cancelled_children = sum(1 for c in all_child_results if c.work_status == WorkStatus.CANCELLED)
    incomplete_children = sum(1 for c in all_child_results if c.work_status in ACTIVE_WORK_STATUSES)

    if total_children == 0:
        agg_status = AggregateStatus.COMPLETE
        success = True
    elif incomplete_children > 0:
        agg_status = AggregateStatus.INCOMPLETE
        success = False
    else:
        # All children are terminal
        if failed_children == 0 and cancelled_children == 0:
            agg_status = AggregateStatus.COMPLETE
            success = True
        elif successful_children == 0:
            agg_status = AggregateStatus.FAILED
            success = False
        else:
            # Mixed success and failure/cancellation
            if eff_policy == AggregationPolicy.ALL_REQUIRED:
                agg_status = AggregateStatus.FAILED
                success = False
            else:
                agg_status = AggregateStatus.PARTIAL
                success = False

    # 8. Bounding & Truncation
    if total_children > max_children:
        truncated = True
        included_children = max_children
        omitted_children = total_children - max_children
        bounded_children = all_child_results[:max_children]
    else:
        truncated = False
        included_children = total_children
        omitted_children = 0
        bounded_children = all_child_results

    return AggregateResult(
        parent_work_id=parent_work.id,
        delegation_group_id=eff_group_id,
        policy=eff_policy,
        status=agg_status,
        success=success,
        total_children=total_children,
        completed_children=completed_children,
        successful_children=successful_children,
        failed_children=failed_children,
        cancelled_children=cancelled_children,
        incomplete_children=incomplete_children,
        included_children=included_children,
        omitted_children=omitted_children,
        truncated=truncated,
        children=bounded_children,
        created_at=now,
    )


def aggregate_from_store(
    work_store: Any,
    parent_work_id: str,
    delegation_group_id: Optional[str] = None,
    policy: Union[AggregationPolicy, str] = AggregationPolicy.ALL_REQUIRED,
    max_children: int = MAX_AGGREGATE_CHILDREN,
    current_time: Optional[float] = None,
) -> AggregateResult:
    """Read canonical child work state from WorkStore and compute bounded aggregate."""
    parent = work_store.get(parent_work_id)
    if parent is None:
        raise KeyError(f"Parent work '{parent_work_id}' not found in WorkStore")

    group: Optional[DelegationGroup] = None
    if delegation_group_id is not None:
        group = get_delegation_group(work_store, parent_work_id, delegation_group_id)
        if group is None:
            raise KeyError(
                f"DelegationGroup '{delegation_group_id}' not found for parent '{parent_work_id}'"
            )
        target_child_ids = group.child_work_ids
    else:
        target_child_ids = get_child_work_ids(parent)

    children: List[ChildWork] = []
    for cid in target_child_ids:
        cw = get_child_work(work_store, cid)
        if cw is None:
            w = work_store.get(cid)
            if w is None:
                raise ResultAggregationBindingError(
                    f"Child work '{cid}' declared or referenced by parent not found in WorkStore"
                )
            cw = ChildWork.from_work(w)
        children.append(cw)

    return aggregate_children(
        parent,
        children,
        policy=policy,
        delegation_group=group,
        delegation_group_id=delegation_group_id,
        max_children=max_children,
        current_time=current_time,
    )


def attach_aggregate_to_parent(
    work_store: Any,
    parent_work_id: str,
    aggregate_result: AggregateResult,
    expected_revision: Optional[int] = None,
) -> Work:
    """Attach bounded AggregateResult summary to parent Work record in WorkStore via OCC."""
    parent = work_store.get(parent_work_id)
    if parent is None:
        raise KeyError(f"Parent work '{parent_work_id}' not found in WorkStore")

    if parent.id != aggregate_result.parent_work_id:
        raise ResultAggregationBindingError(
            f"AggregateResult parent ID mismatch: '{aggregate_result.parent_work_id}' vs '{parent.id}'"
        )

    meta = dict(parent.resume_metadata or {})
    aggregates = dict(meta.get("aggregates") or {})
    group_key = aggregate_result.delegation_group_id or "default"

    # Store strictly bounded summary dict in parent Work metadata
    aggregates[group_key] = aggregate_result.to_summary_dict()
    meta["aggregates"] = aggregates
    meta["latest_aggregate"] = aggregate_result.to_summary_dict()

    updated_parent = parent.with_update(resume_metadata=meta)
    saved_parent = work_store.save(updated_parent, expected_revision=expected_revision)
    return saved_parent


def aggregate_and_save(
    work_store: Any,
    parent_work_id: str,
    delegation_group_id: Optional[str] = None,
    policy: Union[AggregationPolicy, str] = AggregationPolicy.ALL_REQUIRED,
    expected_revision: Optional[int] = None,
    max_children: int = MAX_AGGREGATE_CHILDREN,
    current_time: Optional[float] = None,
) -> Tuple[Work, AggregateResult]:
    """Atomically aggregate child work outcomes and attach bounded summary to parent Work with OCC."""
    aggregate_res = aggregate_from_store(
        work_store,
        parent_work_id,
        delegation_group_id=delegation_group_id,
        policy=policy,
        max_children=max_children,
        current_time=current_time,
    )
    saved_parent = attach_aggregate_to_parent(
        work_store,
        parent_work_id,
        aggregate_res,
        expected_revision=expected_revision,
    )
    return saved_parent, aggregate_res


def get_aggregate_for_group(
    parent_work: Work,
    delegation_group_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Retrieve bounded aggregate summary dictionary for a delegation group from parent Work."""
    if not parent_work.resume_metadata:
        return None
    aggregates = parent_work.resume_metadata.get("aggregates") or {}
    group_key = delegation_group_id or "default"
    data = aggregates.get(group_key)
    return dict(data) if isinstance(data, dict) else None


def get_latest_aggregate(parent_work: Work) -> Optional[Dict[str, Any]]:
    """Retrieve the latest bounded aggregate summary dictionary from parent Work."""
    if not parent_work.resume_metadata:
        return None
    latest = parent_work.resume_metadata.get("latest_aggregate")
    return dict(latest) if isinstance(latest, dict) else None


class ResultAggregator:
    """Canonical Result Aggregator for Scoped Subagents.

    Architectural Invariants:
    - Pure observation / data composition layer.
    - Zero execution authority (cannot run shell, network, subprocess, Git, orchestrator, or LLM).
    - Cannot issue capabilities, create approvals, or consume approvals.
    - Source of truth is canonical WorkStore state.
    - Idempotent and deterministic.
    """
    aggregate = staticmethod(aggregate_children)
    aggregate_group = staticmethod(aggregate_from_store)
    from_store = staticmethod(aggregate_from_store)
    build_child_result = staticmethod(build_child_result)
    attach_to_parent = staticmethod(attach_aggregate_to_parent)
    aggregate_and_save = staticmethod(aggregate_and_save)
    get_aggregate_for_group = staticmethod(get_aggregate_for_group)
    get_latest_aggregate = staticmethod(get_latest_aggregate)


__all__ = [
    "CURRENT_RESULT_AGGREGATION_SCHEMA_VERSION",
    "MAX_AGGREGATE_CHILDREN",
    "MAX_SUMMARY_BYTES",
    "MAX_FAILURE_MESSAGE_BYTES",
    "MAX_ARTIFACTS_PER_CHILD",
    "MAX_TOTAL_ARTIFACT_REFERENCES",
    "MAX_AGGREGATE_BYTES",
    "AggregationPolicy",
    "AggregateStatus",
    "ArtifactReference",
    "ChildResult",
    "AggregateResult",
    "ResultAggregator",
    "ResultAggregationError",
    "ResultAggregationBindingError",
    "ResultAggregationPolicyError",
    "ResultAggregationLimitError",
    "ResultAggregationIntegrityError",
    "aggregate_children",
    "aggregate_from_store",
    "build_child_result",
    "attach_aggregate_to_parent",
    "aggregate_and_save",
    "get_aggregate_for_group",
    "get_latest_aggregate",
]
