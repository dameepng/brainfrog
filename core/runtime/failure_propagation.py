"""Failure and Cancellation Propagation Runtime for Scoped Subagents.

BrainFrog P1.3G — Cancellation + Failure Propagation.

Architectural Principles:
- A subagent is a WORKER, not an authority.
- P1.3G is strictly a lifecycle propagation and eligibility coordination layer.
- It is NOT an authority layer:
    - Zero execution authority (cannot run shell, network, subprocess, Git, or LLM).
    - Zero capability issuance (cannot grant, widen, or mutate capabilities).
    - Zero approval mutation (cannot create, approve, or consume approvals).
    - Zero transaction mutation (cannot initiate transactions or commit filesystems).
- Canonical orchestrator.py remains the SOLE execution engine in BrainFrog.
- State domains remain strictly separate:
    1. WorkStatus        = canonical work lifecycle (CREATED -> ... -> DONE / FAILED / CANCELLED)
    2. SubagentStatus    = worker lifecycle (CREATED -> ... -> COMPLETED / FAILED / CANCELLED)
    3. CoordinationState = delegation eligibility only (WAITING, RUNNABLE, CLAIMED, BLOCKED)
- FAILED is distinct from BLOCKED:
    - FAILED: Work actually reached a failure outcome through execution.
    - BLOCKED: Work cannot become runnable because an upstream prerequisite did not succeed.
- Terminal states (DONE, FAILED, CANCELLED) are immutable and must never be rewritten.
"""
from __future__ import annotations

import collections
import math
import re
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple, Union

from core.runtime.child_work import (
    ChildWork,
    get_child_work,
    get_child_work_ids,
    save_child_work,
    validate_child_work_id,
)
from core.runtime.contract import reject_secrets
from core.runtime.delegation_runtime import (
    CoordinationState,
    DelegationGroup,
    get_delegation_group,
    save_delegation_group,
    validate_delegation_group_id,
)
from core.runtime.secret_scrubbing import scrub_secrets
from core.runtime.subagent import SubagentStatus
from core.runtime.work import (
    ACTIVE_WORK_STATUSES,
    StaleWorkRevisionError,
    TERMINAL_WORK_STATUSES,
    Work,
    WorkFailure,
    WorkStatus,
    WorkStore,
)

CURRENT_FAILURE_PROPAGATION_SCHEMA_VERSION = 1

# Resource bounds (consistent with P1.3E / P1.3F)
MAX_PROPAGATION_DEPTH = 20
MAX_PROPAGATION_EDGES = 200
MAX_PROPAGATION_CHILDREN = 50
MAX_REASON_CHARS = 1024

_INVALID_ID_CHARS = re.compile(r"[\x00-\x1f\x7f/\\:*\?\"<>\|]|(?:\.\.)")


class FailurePropagationError(ValueError):
    """Base exception for failure and cancellation propagation errors."""
    pass


class FailurePropagationBindingError(FailurePropagationError):
    """Raised when identity, parent, actor, session, or incarnation bindings mismatch."""
    pass


class FailurePropagationSessionStaleError(FailurePropagationError, PermissionError):
    """Raised when propagation is attempted under an invalidated or stale session incarnation."""
    pass


class FailurePropagationLimitError(FailurePropagationError):
    """Raised when dependency depth or edge traversal bounds are exceeded."""
    pass


class CancellationPropagationPolicy(str, Enum):
    """Policy governing cancellation propagation through dependent child works."""
    CANCEL_ACTIVE_DESCENDANTS = "cancel_active_descendants"
    BLOCK_DEPENDENTS_ONLY = "block_dependents_only"


class ParentFailurePolicy(str, Enum):
    """Policy governing whether child work failures propagate to Parent Work status."""
    PROPAGATE_FAILURE = "propagate_failure"
    BEST_EFFORT = "best_effort"
    MANUAL = "manual"


@dataclass(frozen=True)
class PropagationResult:
    """Immutable record of the consequences of a failure or cancellation propagation.

    CRITICAL ARCHITECTURAL INVARIANT:
    PropagationResult is strictly observational DATA.
    It confers NO execution authority, approval authority, or capability grants.
    """
    parent_work_id: str
    delegation_group_id: Optional[str]
    failed_child_ids: Tuple[str, ...]
    cancelled_child_ids: Tuple[str, ...]
    blocked_child_ids: Tuple[str, ...]
    parent_status: WorkStatus
    parent_failed: bool
    parent_cancelled: bool
    affected_child_count: int
    created_at: float
    schema_version: int = CURRENT_FAILURE_PROPAGATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.parent_work_id) is not str or not self.parent_work_id.strip():
            raise FailurePropagationBindingError("parent_work_id must be a non-empty string")
        clean_pid = self.parent_work_id.strip()[:128]
        if _INVALID_ID_CHARS.search(clean_pid) or clean_pid.startswith((".", "~", "/", "\\")):
            raise FailurePropagationBindingError(f"Invalid characters in parent_work_id: '{clean_pid}'")
        object.__setattr__(self, "parent_work_id", clean_pid)

        if self.delegation_group_id is not None:
            clean_gid = validate_delegation_group_id(self.delegation_group_id)
            object.__setattr__(self, "delegation_group_id", clean_gid)

        if isinstance(self.parent_status, str):
            try:
                object.__setattr__(self, "parent_status", WorkStatus(self.parent_status))
            except ValueError as exc:
                raise FailurePropagationError(f"Invalid parent_status: {self.parent_status}") from exc
        elif not isinstance(self.parent_status, WorkStatus):
            raise FailurePropagationError(f"parent_status must be WorkStatus, got {type(self.parent_status)}")

        if not isinstance(self.parent_failed, bool) or not isinstance(self.parent_cancelled, bool):
            raise FailurePropagationError("parent_failed and parent_cancelled must be booleans")

        if not isinstance(self.affected_child_count, int) or isinstance(self.affected_child_count, bool) or self.affected_child_count < 0:
            raise FailurePropagationError("affected_child_count must be a non-negative integer")

        if not isinstance(self.created_at, (int, float)) or not math.isfinite(self.created_at):
            raise FailurePropagationError("created_at must be a valid finite number")
        object.__setattr__(self, "created_at", float(self.created_at))

        reject_secrets(self.to_dict())

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "schema_version": self.schema_version,
            "parent_work_id": self.parent_work_id,
            "delegation_group_id": self.delegation_group_id,
            "failed_child_ids": list(self.failed_child_ids),
            "cancelled_child_ids": list(self.cancelled_child_ids),
            "blocked_child_ids": list(self.blocked_child_ids),
            "parent_status": self.parent_status.value,
            "parent_failed": self.parent_failed,
            "parent_cancelled": self.parent_cancelled,
            "affected_child_count": self.affected_child_count,
            "created_at": self.created_at,
        }
        reject_secrets(data)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> PropagationResult:
        if not isinstance(data, dict):
            raise FailurePropagationError("PropagationResult data must be a dictionary")
        reject_secrets(data)
        return cls(
            parent_work_id=str(data.get("parent_work_id", "")),
            delegation_group_id=data.get("delegation_group_id"),
            failed_child_ids=tuple(str(x) for x in data.get("failed_child_ids", ())),
            cancelled_child_ids=tuple(str(x) for x in data.get("cancelled_child_ids", ())),
            blocked_child_ids=tuple(str(x) for x in data.get("blocked_child_ids", ())),
            parent_status=WorkStatus(str(data.get("parent_status", "executing"))),
            parent_failed=bool(data.get("parent_failed", False)),
            parent_cancelled=bool(data.get("parent_cancelled", False)),
            affected_child_count=int(data.get("affected_child_count", 0)),
            created_at=float(data.get("created_at", time.time())),
            schema_version=int(data.get("schema_version", CURRENT_FAILURE_PROPAGATION_SCHEMA_VERSION)),
        )


def find_downstream_dependents(
    child_work_ids: Sequence[str],
    dependencies: Mapping[str, Sequence[str]],
    root_ids: Sequence[str],
    max_depth: int = MAX_PROPAGATION_DEPTH,
    max_children: int = MAX_PROPAGATION_CHILDREN,
) -> Tuple[str, ...]:
    """Iteratively compute topological downstream descendants of root_ids in dependency DAG.

    Guarantees:
    - Bounded iterative traversal (no Python recursion overflow).
    - Respects max_depth and max_children bounds fail-closed.
    - Deterministic ordering matching declared child_work_ids.
    """
    if not root_ids:
        return ()

    # Construct forward adjacency map (predecessor -> list of dependents)
    dependents_of: Dict[str, List[str]] = {cid: [] for cid in child_work_ids}
    total_edges = 0
    for cid, preds in dependencies.items():
        for p in preds:
            total_edges += 1
            if total_edges > MAX_PROPAGATION_EDGES:
                raise FailurePropagationLimitError(
                    f"Dependency edges count {total_edges} exceeds limit {MAX_PROPAGATION_EDGES}"
                )
            if p in dependents_of:
                dependents_of[p].append(cid)

    visited: Set[str] = set()
    queue: collections.deque[Tuple[str, int]] = collections.deque((r, 0) for r in root_ids)

    while queue:
        curr, depth = queue.popleft()
        if depth >= max_depth:
            if dependents_of.get(curr):
                raise FailurePropagationLimitError(
                    f"Dependency depth {depth + 1} exceeds maximum propagation depth {max_depth}"
                )
            continue
        for dep in dependents_of.get(curr, ()):
            if dep not in visited and dep not in root_ids:
                visited.add(dep)
                if len(visited) > max_children:
                    raise FailurePropagationLimitError(
                        f"Transitive descendant count {len(visited)} exceeds limit {max_children}"
                    )
                queue.append((dep, depth + 1))

    # Return deterministically in declared child_work_ids order
    return tuple(cid for cid in child_work_ids if cid in visited)


def propagate_failure(
    group: DelegationGroup,
    children_map: Mapping[str, ChildWork],
    failed_child_id: Optional[str] = None,
    *,
    parent_work: Optional[Work] = None,
    parent_policy: ParentFailurePolicy = ParentFailurePolicy.BEST_EFFORT,
    current_time: Optional[float] = None,
    current_session_incarnation_id: Optional[str] = None,
) -> Tuple[DelegationGroup, Dict[str, ChildWork], Optional[Work], PropagationResult]:
    """Deterministically propagate failure through dependency DAG.

    Rules:
    - Enforces session incarnation freshness fail-closed.
    - Direct failures remain FAILED.
    - Dependents of failed/cancelled prerequisites become BLOCKED in CoordinationState.
    - Dependents' WorkStatus is NOT changed to FAILED (BLOCKED != FAILED).
    - Terminal states (DONE, FAILED, CANCELLED) are NEVER rewritten.
    - Iterative DAG traversal guarantees fixed-point propagation without recursion overflow.
    - Parent consequences evaluated strictly according to ParentFailurePolicy.
    """
    now = time.time() if current_time is None else float(current_time)

    # 1. Enforce session incarnation freshness
    if current_session_incarnation_id is not None and current_session_incarnation_id != group.session_incarnation_id:
        raise FailurePropagationSessionStaleError(
            f"Session incarnation '{current_session_incarnation_id}' does not match group '{group.session_incarnation_id}'. Action rejected."
        )

    # 2. Validate failed child if specified
    if failed_child_id is not None and failed_child_id not in group.child_work_ids:
        raise FailurePropagationBindingError(
            f"Child work '{failed_child_id}' is not in delegation group '{group.delegation_group_id}'"
        )

    # 3. Collect all failed and cancelled children in the group
    failed_roots: List[str] = []
    cancelled_roots: List[str] = []

    for cid in group.child_work_ids:
        cw = children_map.get(cid)
        if cw is not None:
            if cw.status == WorkStatus.FAILED or cid == failed_child_id:
                failed_roots.append(cid)
            elif cw.status == WorkStatus.CANCELLED:
                cancelled_roots.append(cid)
        elif cid == failed_child_id:
            failed_roots.append(cid)

    # Also check if any child is already marked BLOCKED in coordination
    blocked_roots = [
        cid for cid in group.child_work_ids
        if group.coordination_states.get(cid) == CoordinationState.BLOCKED
    ]

    all_unmet_roots = list(set(failed_roots + cancelled_roots + blocked_roots))

    # 4. Find all downstream descendants that transitively depend on unmet roots
    transitive_blocked = find_downstream_dependents(
        group.child_work_ids,
        group.dependencies,
        all_unmet_roots,
    )

    # 5. Update CoordinationState: promote affected WAITING or RUNNABLE children to BLOCKED
    new_states = dict(group.coordination_states)
    newly_blocked: List[str] = []

    for cid in transitive_blocked:
        curr_state = new_states.get(cid, CoordinationState.WAITING)
        if curr_state in (CoordinationState.WAITING, CoordinationState.RUNNABLE):
            new_states[cid] = CoordinationState.BLOCKED
            newly_blocked.append(cid)

    # Ensure failed child's coordination slot is updated if needed
    if failed_child_id and new_states.get(failed_child_id) in (CoordinationState.WAITING, CoordinationState.RUNNABLE):
        new_states[failed_child_id] = CoordinationState.BLOCKED
        if failed_child_id not in newly_blocked:
            newly_blocked.append(failed_child_id)

    updated_group = replace(group, coordination_states=new_states, updated_at=now)
    updated_children = dict(children_map)

    # 6. Evaluate parent consequences
    updated_parent = parent_work
    parent_failed = False
    parent_status = parent_work.status if parent_work else WorkStatus.EXECUTING

    if parent_work is not None:
        if current_session_incarnation_id is not None and current_session_incarnation_id != parent_work.session_incarnation_id:
            raise FailurePropagationSessionStaleError(
                f"Session incarnation does not match parent work '{parent_work.id}'"
            )

        if parent_policy == ParentFailurePolicy.PROPAGATE_FAILURE:
            if failed_roots and not parent_work.is_terminal:
                first_failed = failed_roots[0]
                child_cw = children_map.get(first_failed)
                child_reason = child_cw.work.failure.summary if child_cw and child_cw.work and child_cw.work.failure else ""
                fail_summary = (
                    f"Delegated child work '{first_failed}' failed: {child_reason}"
                    if child_reason
                    else f"Delegated child work '{first_failed}' failed"
                )
                updated_parent = parent_work.transition(
                    WorkStatus.FAILED,
                    failure=WorkFailure(code="CHILD_WORK_FAILED", summary=fail_summary),
                    updated_at=now,
                )
                parent_failed = True
                parent_status = WorkStatus.FAILED

    all_blocked_ids = tuple(
        cid for cid in group.child_work_ids
        if new_states.get(cid) == CoordinationState.BLOCKED and cid not in failed_roots and cid not in cancelled_roots
    )

    result = PropagationResult(
        parent_work_id=group.parent_work_id,
        delegation_group_id=group.delegation_group_id,
        failed_child_ids=tuple(failed_roots),
        cancelled_child_ids=tuple(cancelled_roots),
        blocked_child_ids=all_blocked_ids,
        parent_status=parent_status,
        parent_failed=parent_failed,
        parent_cancelled=False,
        affected_child_count=len(newly_blocked),
        created_at=now,
    )

    return updated_group, updated_children, updated_parent, result


def propagate_child_cancellation(
    group: DelegationGroup,
    children_map: Mapping[str, ChildWork],
    child_work_id: str,
    *,
    reason: str = "Child work cancelled",
    policy: CancellationPropagationPolicy = CancellationPropagationPolicy.BLOCK_DEPENDENTS_ONLY,
    parent_work: Optional[Work] = None,
    current_time: Optional[float] = None,
    current_session_incarnation_id: Optional[str] = None,
) -> Tuple[DelegationGroup, Dict[str, ChildWork], Optional[Work], PropagationResult]:
    """Cancel a target child work and propagate cancellation/blocking to dependents.

    Policies:
    - BLOCK_DEPENDENTS_ONLY: Downstream dependents become BLOCKED in coordination, WorkStatus not cancelled.
    - CANCEL_ACTIVE_DESCENDANTS: Downstream dependents that are non-terminal are cancelled.
    In both policies:
    - Terminal states (DONE, FAILED, CANCELLED) are NEVER rewritten.
    - Concurrency capacity is released (terminal work does not consume capacity).
    - Idempotent and deterministic.
    """
    now = time.time() if current_time is None else float(current_time)

    # 1. Enforce session incarnation freshness
    if current_session_incarnation_id is not None and current_session_incarnation_id != group.session_incarnation_id:
        raise FailurePropagationSessionStaleError(
            f"Session incarnation '{current_session_incarnation_id}' does not match group. Action rejected."
        )

    if child_work_id not in group.child_work_ids:
        raise FailurePropagationBindingError(
            f"Child work '{child_work_id}' not found in delegation group '{group.delegation_group_id}'"
        )

    clean_reason = scrub_secrets(reason.strip()[:MAX_REASON_CHARS] or "Child work cancelled")
    updated_children = dict(children_map)
    cancelled_child_ids: List[str] = []

    # 2. Cancel target child if not terminal
    target_child = children_map.get(child_work_id)
    if target_child is not None:
        if not target_child.is_terminal:
            cancelled_target = target_child.cancel(
                reason=clean_reason,
                current_time=now,
                current_session_incarnation_id=current_session_incarnation_id,
            )
            updated_children[child_work_id] = cancelled_target
            cancelled_child_ids.append(child_work_id)
        elif target_child.status == WorkStatus.CANCELLED:
            cancelled_child_ids.append(child_work_id)

    # 3. Mark target child as BLOCKED in coordination state
    new_states = dict(group.coordination_states)
    new_states[child_work_id] = CoordinationState.BLOCKED

    # 4. Find all downstream dependents
    downstream = find_downstream_dependents(
        group.child_work_ids,
        group.dependencies,
        [child_work_id],
    )

    blocked_child_ids: List[str] = []

    # 5. Apply propagation policy to downstream dependents
    for dep_id in downstream:
        # Mark BLOCKED in coordination state
        new_states[dep_id] = CoordinationState.BLOCKED
        blocked_child_ids.append(dep_id)

        dep_child = updated_children.get(dep_id)
        if dep_child is not None and not dep_child.is_terminal:
            if policy == CancellationPropagationPolicy.CANCEL_ACTIVE_DESCENDANTS:
                dep_reason = f"Upstream child '{child_work_id}' was cancelled: {clean_reason}"
                cancelled_dep = dep_child.cancel(
                    reason=dep_reason,
                    current_time=now,
                    current_session_incarnation_id=current_session_incarnation_id,
                )
                updated_children[dep_id] = cancelled_dep
                if dep_id not in cancelled_child_ids:
                    cancelled_child_ids.append(dep_id)

    updated_group = replace(group, coordination_states=new_states, updated_at=now)

    result = PropagationResult(
        parent_work_id=group.parent_work_id,
        delegation_group_id=group.delegation_group_id,
        failed_child_ids=(),
        cancelled_child_ids=tuple(cancelled_child_ids),
        blocked_child_ids=tuple(blocked_child_ids),
        parent_status=parent_work.status if parent_work else WorkStatus.EXECUTING,
        parent_failed=False,
        parent_cancelled=False,
        affected_child_count=len(cancelled_child_ids) + len(blocked_child_ids),
        created_at=now,
    )

    return updated_group, updated_children, parent_work, result


def cancel_parent(
    parent_work: Work,
    children: Sequence[ChildWork],
    group: Optional[DelegationGroup] = None,
    *,
    reason: str = "Parent work cancelled",
    policy: CancellationPropagationPolicy = CancellationPropagationPolicy.CANCEL_ACTIVE_DESCENDANTS,
    current_time: Optional[float] = None,
    current_session_incarnation_id: Optional[str] = None,
) -> Tuple[Work, Optional[DelegationGroup], List[ChildWork], PropagationResult]:
    """Cancel Parent Work and propagate cancellation across dependent children.

    Rules:
    - Verifies session incarnation freshness fail-closed.
    - Transitions Parent Work to WorkStatus.CANCELLED (if not terminal).
    - If policy is CANCEL_ACTIVE_DESCENDANTS, active child works are cancelled.
    - Terminal child states (DONE, FAILED, CANCELLED) are NEVER rewritten.
    - Concurrency slots are released (terminal children do not consume capacity).
    - No child may become RUNNABLE as a consequence of cancellation.
    - Idempotent and deterministic.
    """
    now = time.time() if current_time is None else float(current_time)

    # 1. Enforce session incarnation freshness
    parent_inc = parent_work.session_incarnation_id or ""
    if current_session_incarnation_id is not None and current_session_incarnation_id != parent_inc:
        raise FailurePropagationSessionStaleError(
            f"Session incarnation '{current_session_incarnation_id}' does not match parent work '{parent_inc}'. Action rejected."
        )

    clean_reason = scrub_secrets(reason.strip()[:MAX_REASON_CHARS] or "Parent work cancelled")

    # 2. Cancel parent work if not terminal
    if not parent_work.is_terminal:
        updated_parent = parent_work.cancel(reason=clean_reason, updated_at=now)
    else:
        updated_parent = parent_work

    # 3. Propagate to child works
    updated_children: List[ChildWork] = []
    cancelled_cids: List[str] = []
    failed_cids: List[str] = []

    for cw in children:
        # Check binding correspondence
        if cw.parent_work_id != parent_work.id:
            raise FailurePropagationBindingError(
                f"Child work '{cw.id}' parent '{cw.parent_work_id}' does not match parent work '{parent_work.id}'"
            )

        if cw.status == WorkStatus.DONE:
            # Terminal success is preserved
            updated_children.append(cw)
        elif cw.status == WorkStatus.FAILED:
            # Terminal failure is preserved
            updated_children.append(cw)
            failed_cids.append(cw.id)
        elif cw.status == WorkStatus.CANCELLED:
            # Already cancelled is preserved
            updated_children.append(cw)
            cancelled_cids.append(cw.id)
        else:
            # Active child
            if policy == CancellationPropagationPolicy.CANCEL_ACTIVE_DESCENDANTS:
                cancelled_cw = cw.cancel(
                    reason=clean_reason,
                    current_time=now,
                    current_session_incarnation_id=current_session_incarnation_id,
                )
                updated_children.append(cancelled_cw)
                cancelled_cids.append(cw.id)
            else:
                updated_children.append(cw)

    # 4. Update delegation group if provided
    updated_group: Optional[DelegationGroup] = None
    blocked_cids: List[str] = []

    if group is not None:
        if group.parent_work_id != parent_work.id:
            raise FailurePropagationBindingError(
                f"DelegationGroup parent mismatch: '{group.parent_work_id}' vs '{parent_work.id}'"
            )

        new_states = dict(group.coordination_states)
        for cid in group.child_work_ids:
            # Children that are not DONE cannot run after parent cancellation
            matching_cw = next((c for c in updated_children if c.id == cid), None)
            if matching_cw is None or matching_cw.status != WorkStatus.DONE:
                new_states[cid] = CoordinationState.BLOCKED
                if cid not in cancelled_cids and cid not in failed_cids:
                    blocked_cids.append(cid)

        updated_group = replace(group, coordination_states=new_states, updated_at=now)

    result = PropagationResult(
        parent_work_id=parent_work.id,
        delegation_group_id=group.delegation_group_id if group else None,
        failed_child_ids=tuple(failed_cids),
        cancelled_child_ids=tuple(cancelled_cids),
        blocked_child_ids=tuple(blocked_cids),
        parent_status=updated_parent.status,
        parent_failed=updated_parent.status == WorkStatus.FAILED,
        parent_cancelled=updated_parent.status == WorkStatus.CANCELLED,
        affected_child_count=len(cancelled_cids) + len(blocked_cids),
        created_at=now,
    )

    return updated_parent, updated_group, updated_children, result


def reconcile_failure_and_cancellation(
    group: DelegationGroup,
    children_map: Mapping[str, ChildWork],
    *,
    parent_work: Optional[Work] = None,
    current_time: Optional[float] = None,
    current_session_incarnation_id: Optional[str] = None,
) -> DelegationGroup:
    """Deterministically reconcile failure and cancellation state after restart or crash recovery.

    Guarantees:
    - Fixed-point iterative convergence over dependency DAG.
    - Dependents of FAILED, CANCELLED, or BLOCKED nodes become BLOCKED.
    - If parent_work is CANCELLED, all active children become BLOCKED.
    - Promotes WAITING to RUNNABLE if and only if all dependencies are WorkStatus.DONE.
    - Idempotent and pure: repeated calls over identical state produce identical group.
    """
    now = time.time() if current_time is None else float(current_time)

    if current_session_incarnation_id is not None and current_session_incarnation_id != group.session_incarnation_id:
        raise FailurePropagationSessionStaleError("Session incarnation is stale")

    updated_states = dict(group.coordination_states)

    # Iterative fixed-point propagation up to max children count
    changed = True
    iterations = 0
    max_iter = len(group.child_work_ids) + 1

    while changed and iterations < max_iter:
        changed = False
        iterations += 1

        for cid in group.child_work_ids:
            curr = updated_states.get(cid, CoordinationState.WAITING)
            if curr in (CoordinationState.WAITING, CoordinationState.RUNNABLE):
                # If parent work is cancelled, block all active children
                if parent_work and parent_work.status == WorkStatus.CANCELLED:
                    updated_states[cid] = CoordinationState.BLOCKED
                    changed = True
                    continue

                # Check delegation expiration
                cw = children_map.get(cid)
                if cw is not None and cw.is_delegation_expired(current_time=now):
                    updated_states[cid] = CoordinationState.BLOCKED
                    changed = True
                    continue

                preds = group.dependencies.get(cid, ())
                has_unmet_pred = False
                all_preds_done = True if preds else False
                for p in preds:
                    p_state = updated_states.get(p)
                    p_work = children_map.get(p)
                    if p_state == CoordinationState.BLOCKED:
                        has_unmet_pred = True
                        all_preds_done = False
                        break
                    if p_work is not None and p_work.status in (WorkStatus.FAILED, WorkStatus.CANCELLED):
                        has_unmet_pred = True
                        all_preds_done = False
                        break
                    if p_work is None or p_work.status != WorkStatus.DONE:
                        all_preds_done = False

                if has_unmet_pred:
                    updated_states[cid] = CoordinationState.BLOCKED
                    changed = True
                elif all_preds_done and curr == CoordinationState.WAITING:
                    updated_states[cid] = CoordinationState.RUNNABLE
                    changed = True

    return replace(group, coordination_states=updated_states, updated_at=now)


# ==============================================================================
# Persistent Store Integration Helpers
# ==============================================================================


def propagate_failure_in_store(
    work_store: Any,
    parent_work_id: str,
    delegation_group_id: str,
    failed_child_id: Optional[str] = None,
    *,
    parent_policy: ParentFailurePolicy = ParentFailurePolicy.BEST_EFFORT,
    expected_revision: Optional[int] = None,
    current_time: Optional[float] = None,
    current_session_incarnation_id: Optional[str] = None,
) -> PropagationResult:
    """Propagate failure through WorkStore records with Optimistic Concurrency Control (OCC)."""
    parent = work_store.get(parent_work_id)
    if parent is None:
        raise KeyError(f"Parent work '{parent_work_id}' not found in WorkStore")

    group = get_delegation_group(work_store, parent_work_id, delegation_group_id)
    if group is None:
        raise KeyError(f"DelegationGroup '{delegation_group_id}' not found for parent '{parent_work_id}'")

    children_map: Dict[str, ChildWork] = {}
    for cid in group.child_work_ids:
        cw = get_child_work(work_store, cid)
        if cw is None:
            w = work_store.get(cid)
            if w is None:
                raise FailurePropagationBindingError(f"Child work '{cid}' not found in WorkStore")
            cw = ChildWork.from_work(w)
        children_map[cid] = cw

    upd_group, upd_children, upd_parent, result = propagate_failure(
        group,
        children_map,
        failed_child_id,
        parent_work=parent,
        parent_policy=parent_policy,
        current_time=current_time,
        current_session_incarnation_id=current_session_incarnation_id,
    )

    # Persist updated children
    for cid, cw in upd_children.items():
        save_child_work(work_store, cw)

    # Persist updated group
    save_delegation_group(work_store, upd_group, expected_revision=expected_revision)

    # Persist updated parent if mutated
    if upd_parent is not None and upd_parent != parent:
        fresh_parent = work_store.get(parent_work_id)
        if fresh_parent is not None and not fresh_parent.is_terminal:
            if upd_parent.is_terminal:
                fresh_parent = fresh_parent.transition(
                    upd_parent.status,
                    failure=upd_parent.failure,
                    cancellation_reason=upd_parent.cancellation_reason,
                    updated_at=upd_parent.updated_at,
                )
            else:
                fresh_parent = fresh_parent.transition(
                    upd_parent.status,
                    updated_at=upd_parent.updated_at,
                )
            work_store.save(fresh_parent)

    return result


def cancel_child_in_store(
    work_store: Any,
    parent_work_id: str,
    delegation_group_id: str,
    child_work_id: str,
    *,
    reason: str = "Child work cancelled",
    policy: CancellationPropagationPolicy = CancellationPropagationPolicy.BLOCK_DEPENDENTS_ONLY,
    expected_revision: Optional[int] = None,
    current_time: Optional[float] = None,
    current_session_incarnation_id: Optional[str] = None,
) -> Tuple[ChildWork, PropagationResult]:
    """Cancel a child work in WorkStore and propagate cancellation/blocking under OCC."""
    parent = work_store.get(parent_work_id)
    if parent is None:
        raise KeyError(f"Parent work '{parent_work_id}' not found in WorkStore")

    group = get_delegation_group(work_store, parent_work_id, delegation_group_id)
    if group is None:
        raise KeyError(f"DelegationGroup '{delegation_group_id}' not found for parent '{parent_work_id}'")

    children_map: Dict[str, ChildWork] = {}
    for cid in group.child_work_ids:
        cw = get_child_work(work_store, cid)
        if cw is None:
            w = work_store.get(cid)
            if w is None:
                raise FailurePropagationBindingError(f"Child work '{cid}' not found in WorkStore")
            cw = ChildWork.from_work(w)
        children_map[cid] = cw

    upd_group, upd_children, _, result = propagate_child_cancellation(
        group,
        children_map,
        child_work_id,
        reason=reason,
        policy=policy,
        parent_work=parent,
        current_time=current_time,
        current_session_incarnation_id=current_session_incarnation_id,
    )

    # Persist all affected children
    for cid, cw in upd_children.items():
        save_child_work(work_store, cw)

    # Persist updated delegation group
    save_delegation_group(work_store, upd_group, expected_revision=expected_revision)

    target_cw = upd_children[child_work_id]
    return target_cw, result


def cancel_parent_in_store(
    work_store: Any,
    parent_work_id: str,
    delegation_group_id: Optional[str] = None,
    *,
    reason: str = "Parent work cancelled",
    policy: CancellationPropagationPolicy = CancellationPropagationPolicy.CANCEL_ACTIVE_DESCENDANTS,
    expected_revision: Optional[int] = None,
    current_time: Optional[float] = None,
    current_session_incarnation_id: Optional[str] = None,
) -> Tuple[Work, PropagationResult]:
    """Cancel Parent Work and propagate cancellation across dependent children in WorkStore under OCC."""
    parent = work_store.get(parent_work_id)
    if parent is None:
        raise KeyError(f"Parent work '{parent_work_id}' not found in WorkStore")

    group: Optional[DelegationGroup] = None
    if delegation_group_id is not None:
        group = get_delegation_group(work_store, parent_work_id, delegation_group_id)
        if group is None:
            raise KeyError(f"DelegationGroup '{delegation_group_id}' not found for parent '{parent_work_id}'")
        target_cids = group.child_work_ids
    else:
        target_cids = get_child_work_ids(parent)

    children: List[ChildWork] = []
    for cid in target_cids:
        cw = get_child_work(work_store, cid)
        if cw is None:
            w = work_store.get(cid)
            if w is None:
                raise FailurePropagationBindingError(f"Child work '{cid}' not found in WorkStore")
            cw = ChildWork.from_work(w)
        children.append(cw)

    upd_parent, upd_group, upd_children, result = cancel_parent(
        parent,
        children,
        group=group,
        reason=reason,
        policy=policy,
        current_time=current_time,
        current_session_incarnation_id=current_session_incarnation_id,
    )

    # Persist cancelled/updated children
    for cw in upd_children:
        save_child_work(work_store, cw)

    # Persist updated delegation group if present
    if upd_group is not None:
        save_delegation_group(work_store, upd_group)

    # Persist parent work under OCC
    saved_parent = work_store.save(upd_parent, expected_revision=expected_revision)
    return saved_parent, result


class FailurePropagator:
    """Canonical Failure and Cancellation Propagation Runtime for Scoped Subagents.

    Architectural Invariants:
    - Pure lifecycle propagation layer.
    - Zero execution authority.
    - Terminal immutability preserved.
    - Idempotent and deterministic.
    """
    propagate_failure = staticmethod(propagate_failure)
    propagate_child_cancellation = staticmethod(propagate_child_cancellation)
    cancel_parent = staticmethod(cancel_parent)
    reconcile = staticmethod(reconcile_failure_and_cancellation)
    find_downstream_dependents = staticmethod(find_downstream_dependents)
    propagate_failure_in_store = staticmethod(propagate_failure_in_store)
    cancel_child_in_store = staticmethod(cancel_child_in_store)
    cancel_parent_in_store = staticmethod(cancel_parent_in_store)


__all__ = [
    "CURRENT_FAILURE_PROPAGATION_SCHEMA_VERSION",
    "MAX_PROPAGATION_DEPTH",
    "MAX_PROPAGATION_EDGES",
    "MAX_PROPAGATION_CHILDREN",
    "MAX_REASON_CHARS",
    "CancellationPropagationPolicy",
    "ParentFailurePolicy",
    "PropagationResult",
    "FailurePropagationError",
    "FailurePropagationBindingError",
    "FailurePropagationSessionStaleError",
    "FailurePropagationLimitError",
    "FailurePropagator",
    "find_downstream_dependents",
    "propagate_failure",
    "propagate_child_cancellation",
    "cancel_parent",
    "reconcile_failure_and_cancellation",
    "propagate_failure_in_store",
    "cancel_child_in_store",
    "cancel_parent_in_store",
]
