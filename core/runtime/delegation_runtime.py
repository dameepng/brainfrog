"""Delegation Coordination Runtime for Sequential and Parallel Child Work.

BrainFrog P1.3E — Parallel / Sequential Delegation.

Architectural Principles:
- A subagent is a WORKER, not an authority.
- The coordinator coordinates ELIGIBILITY, NOT execution authority.
- There are THREE SEPARATE state domains:
    1. WorkStatus        = canonical work lifecycle (CREATED -> PLANNING -> ... -> DONE / FAILED / CANCELLED)
    2. SubagentStatus    = worker lifecycle (CREATED -> READY -> RUNNING -> COMPLETED / FAILED / CANCELLED)
    3. CoordinationState = delegation eligibility only (WAITING -> RUNNABLE -> CLAIMED, with BLOCKED)
- CoordinationState determines ELIGIBILITY.
- WorkStatus determines WORK LIFECYCLE.
- SubagentStatus determines WORKER LIFECYCLE.
- The coordinator does NOT perform:
    - filesystem mutation
    - shell execution
    - network execution
    - Git operations
    - transaction execution
    - approval execution
    - LLM execution
    - direct orchestrator calls
- Execution remains strictly:
    Child Work -> Plan -> Approval -> ApprovedExecutionContract -> Transaction -> orchestrator.py
- Canonical orchestrator.py remains the SOLE execution engine in BrainFrog.
"""
from __future__ import annotations

import collections
import math
import re
import secrets
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple, Union

from core.runtime.child_work import ChildWork, validate_child_work_id
from core.runtime.contract import reject_secrets
from core.runtime.delegation import DelegationContract
from core.runtime.secret_scrubbing import scrub_secrets
from core.runtime.work import StaleWorkRevisionError, Work, WorkStatus

CURRENT_DELEGATION_GROUP_SCHEMA_VERSION = 1

# Resource bounds
DEFAULT_MAX_CONCURRENCY = 4
MIN_CONCURRENCY = 1
MAX_SUBAGENT_CONCURRENCY = 16

MAX_CHILDREN_PER_GROUP = 50
MAX_DEPENDENCY_EDGES = 200
MAX_DEPENDENCY_DEPTH = 20
MAX_GROUP_ID_CHARS = 128
MAX_REASON_CHARS = 1024

_INVALID_ID_CHARS = re.compile(r"[\x00-\x1f\x7f/\\:*\?\"<>\|]|(?:\.\.)")
_INVALID_SESSION_ID_CHARS = re.compile(r"[\x00-\x1f\x7f/\\*\?\"<>\|]|(?:\.\.)")


class DelegationRuntimeError(ValueError):
    """Base exception for delegation coordination runtime errors."""
    pass


class DelegationModeError(DelegationRuntimeError):
    """Raised when an unknown or invalid delegation mode is specified."""
    pass


class DelegationBindingError(DelegationRuntimeError):
    """Raised when parent, child, actor, session, or incarnation bindings mismatch."""
    pass


class DelegationDependencyError(DelegationRuntimeError):
    """Raised when dependency validation fails (cycles, self-deps, unknown nodes, edge limits)."""
    pass


class DelegationConcurrencyError(DelegationRuntimeError):
    """Raised when concurrency limits are violated or configured invalidly."""
    pass


class DelegationSessionStaleError(DelegationRuntimeError, PermissionError):
    """Raised when a coordination operation is attempted with a stale session incarnation."""
    pass


class DelegationExpiredError(DelegationRuntimeError, PermissionError):
    """Raised when a child delegation contract is expired at coordination time."""
    pass


def validate_delegation_group_id(group_id: str) -> str:
    """Validate delegation group ID to prevent path traversal, drive letters, and malformed identifiers."""
    if type(group_id) is not str:
        raise DelegationBindingError(f"Delegation group ID must be a string, got {type(group_id)}")
    clean = group_id.strip()
    if not clean or len(clean) > MAX_GROUP_ID_CHARS:
        raise DelegationBindingError(
            f"Delegation group ID must be a non-empty string with length <= {MAX_GROUP_ID_CHARS}"
        )
    if _INVALID_ID_CHARS.search(clean) or clean.startswith((".", "~", "/", "\\")):
        raise DelegationBindingError(
            f"Invalid characters or traversal detected in Delegation group ID: '{clean}'"
        )
    return clean


def _validate_binding_field(val: Any, name: str) -> str:
    """Validate mandatory binding fields."""
    if type(val) is not str:
        raise DelegationBindingError(f"{name} must be a string, got {type(val)}")
    clean = val.strip()
    if not clean:
        raise DelegationBindingError(f"{name} cannot be empty")
    regex = _INVALID_SESSION_ID_CHARS if name == "session_id" else _INVALID_ID_CHARS
    if regex.search(clean) or clean.startswith((".", "~", "/", "\\")):
        raise DelegationBindingError(f"Invalid characters or traversal in {name}: '{clean}'")
    return clean


class DelegationMode(str, Enum):
    """Deterministic mode for coordinating child work units."""
    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"


class CoordinationState(str, Enum):
    """Coordination eligibility states for a child work unit in a delegation group.

    CRITICAL ARCHITECTURAL INVARIANT:
    These states represent coordination ELIGIBILITY ONLY.
    They do NOT represent execution or lifecycle.
    CoordinationState != WorkStatus != SubagentStatus.

    WAITING: Child exists but cannot currently be selected (prerequisites unmet or capacity full).
    RUNNABLE: All coordination prerequisites satisfied; eligible to be claimed by runtime.
    CLAIMED: Coordination slot atomically reserved; child has won the dispatch claim.
    BLOCKED: Prerequisite permanently unsatisfiable (e.g. dependency failed/cancelled or delegation expired).
    """
    WAITING = "waiting"
    RUNNABLE = "runnable"
    CLAIMED = "claimed"
    BLOCKED = "blocked"


# Backwards compatibility alias
CoordinationStatus = CoordinationState


@dataclass(frozen=True)
class DispatchDecision:
    """Immutable record of an atomic claim decision returned by the coordination runtime."""
    child_work_id: str
    coordination_state: CoordinationState
    claimed_at: float
    schema_version: int = 1

    @property
    def id(self) -> str:
        """Alias for child_work_id."""
        return self.child_work_id

    def to_dict(self) -> Dict[str, Any]:
        return {
            "child_work_id": self.child_work_id,
            "coordination_state": self.coordination_state.value,
            "claimed_at": self.claimed_at,
            "schema_version": self.schema_version,
        }


def validate_dependency_dag(
    child_work_ids: Sequence[str],
    dependencies: Mapping[str, Sequence[str]],
) -> None:
    """Deterministically validate that dependencies form a valid DAG over child_work_ids.

    Enforces:
    - Every dependent and predecessor exists in child_work_ids.
    - No self-dependencies (A -> A).
    - No duplicate edges.
    - Edge count limit (<= MAX_DEPENDENCY_EDGES).
    - Acyclicity via Kahn's algorithm (no cycles).
    - Longest path depth limit (<= MAX_DEPENDENCY_DEPTH).
    """
    child_set = set(child_work_ids)
    if len(child_set) != len(child_work_ids):
        raise DelegationBindingError("Duplicate child_work_ids provided to delegation group")

    total_edges = 0
    in_degree: Dict[str, int] = {cid: 0 for cid in child_work_ids}
    adj: Dict[str, List[str]] = {cid: [] for cid in child_work_ids}

    for child_id, preds in dependencies.items():
        if child_id not in child_set:
            raise DelegationDependencyError(
                f"Dependency defined for unknown child '{child_id}' not in child_work_ids"
            )
        seen_preds: Set[str] = set()
        for p in preds:
            if p == child_id:
                raise DelegationDependencyError(
                    f"Self-dependency rejected: child '{child_id}' cannot depend on itself"
                )
            if p not in child_set:
                raise DelegationDependencyError(
                    f"Unknown dependency: child '{child_id}' depends on non-existent child '{p}'"
                )
            if p in seen_preds:
                raise DelegationDependencyError(
                    f"Duplicate dependency edge rejected: child '{child_id}' depends on '{p}' multiple times"
                )
            seen_preds.add(p)
            adj[p].append(child_id)
            in_degree[child_id] += 1
            total_edges += 1

    if total_edges > MAX_DEPENDENCY_EDGES:
        raise DelegationDependencyError(
            f"Total dependency edges {total_edges} exceeds limit {MAX_DEPENDENCY_EDGES}"
        )

    # Kahn's algorithm for cycle detection and longest path (depth) calculation
    queue: collections.deque[str] = collections.deque(
        [cid for cid in child_work_ids if in_degree[cid] == 0]
    )
    depth: Dict[str, int] = {cid: 0 for cid in child_work_ids}
    visited_count = 0

    while queue:
        curr = queue.popleft()
        visited_count += 1
        curr_depth = depth[curr]
        for neighbor in adj[curr]:
            depth[neighbor] = max(depth[neighbor], curr_depth + 1)
            if depth[neighbor] > MAX_DEPENDENCY_DEPTH:
                raise DelegationDependencyError(
                    f"Dependency depth {depth[neighbor]} exceeds maximum allowed {MAX_DEPENDENCY_DEPTH}"
                )
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if visited_count != len(child_work_ids):
        # Find remaining nodes in cycles for descriptive error
        unresolved = [cid for cid, deg in in_degree.items() if deg > 0]
        raise DelegationDependencyError(
            f"Cycle detected in dependency graph involving children: {unresolved}"
        )


@dataclass(frozen=True)
class DelegationGroup:
    """Immutable coordination model representing a bounded group of child Work units."""
    delegation_group_id: str
    parent_work_id: str
    actor: str
    session_id: str
    session_incarnation_id: str
    mode: DelegationMode
    child_work_ids: Tuple[str, ...]
    dependencies: Dict[str, Tuple[str, ...]]
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    coordination_states: Dict[str, CoordinationState] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    schema_version: int = CURRENT_DELEGATION_GROUP_SCHEMA_VERSION

    def __post_init__(self) -> None:
        clean_gid = validate_delegation_group_id(self.delegation_group_id)
        object.__setattr__(self, "delegation_group_id", clean_gid)

        clean_parent = _validate_binding_field(self.parent_work_id, "parent_work_id")
        object.__setattr__(self, "parent_work_id", clean_parent)

        clean_actor = _validate_binding_field(self.actor, "actor")
        object.__setattr__(self, "actor", clean_actor)

        clean_session = _validate_binding_field(self.session_id, "session_id")
        object.__setattr__(self, "session_id", clean_session)

        clean_inc = _validate_binding_field(self.session_incarnation_id, "session_incarnation_id")
        object.__setattr__(self, "session_incarnation_id", clean_inc)

        # Mode validation
        if not isinstance(self.mode, DelegationMode):
            try:
                object.__setattr__(self, "mode", DelegationMode(self.mode))
            except (ValueError, TypeError) as exc:
                raise DelegationModeError(f"Invalid delegation mode: {self.mode}") from exc

        # Child IDs validation
        if not isinstance(self.child_work_ids, (tuple, list)):
            raise DelegationBindingError("child_work_ids must be a sequence of string identifiers")
        clean_child_ids = tuple(validate_child_work_id(cid) for cid in self.child_work_ids)
        if len(clean_child_ids) == 0:
            raise DelegationBindingError("DelegationGroup must contain at least one child work ID")
        if len(clean_child_ids) > MAX_CHILDREN_PER_GROUP:
            raise DelegationConcurrencyError(
                f"Child count {len(clean_child_ids)} exceeds limit {MAX_CHILDREN_PER_GROUP}"
            )
        if len(set(clean_child_ids)) != len(clean_child_ids):
            raise DelegationBindingError("Duplicate child_work_ids in delegation group")
        object.__setattr__(self, "child_work_ids", clean_child_ids)

        # Concurrency validation
        if not isinstance(self.max_concurrency, int) or isinstance(self.max_concurrency, bool):
            raise DelegationConcurrencyError("max_concurrency must be an integer")
        if self.mode == DelegationMode.SEQUENTIAL:
            if self.max_concurrency != 1:
                object.__setattr__(self, "max_concurrency", 1)
        else:
            if not (MIN_CONCURRENCY <= self.max_concurrency <= MAX_SUBAGENT_CONCURRENCY):
                raise DelegationConcurrencyError(
                    f"max_concurrency must be between {MIN_CONCURRENCY} and {MAX_SUBAGENT_CONCURRENCY}, got {self.max_concurrency}"
                )

        # Dependencies normalization and validation
        norm_deps: Dict[str, Tuple[str, ...]] = {}
        for cid in clean_child_ids:
            raw_p = self.dependencies.get(cid, ())
            if not isinstance(raw_p, (tuple, list)):
                raise DelegationDependencyError(f"Predecessors for '{cid}' must be a sequence of strings")
            norm_deps[cid] = tuple(raw_p)
        object.__setattr__(self, "dependencies", norm_deps)

        # Validate DAG
        validate_dependency_dag(clean_child_ids, norm_deps)

        # For sequential mode, verify explicit linear dependencies
        if self.mode == DelegationMode.SEQUENTIAL:
            for idx, cid in enumerate(clean_child_ids):
                expected = () if idx == 0 else (clean_child_ids[idx - 1],)
                actual = norm_deps.get(cid, ())
                if actual != expected:
                    raise DelegationDependencyError(
                        f"Sequential mode requires linear dependency {expected} for child '{cid}', got {actual}"
                    )

        # Child coordination states normalization
        norm_states: Dict[str, CoordinationState] = {}
        for cid in clean_child_ids:
            raw_s = self.coordination_states.get(cid, CoordinationState.WAITING)
            if not isinstance(raw_s, CoordinationState):
                try:
                    norm_states[cid] = CoordinationState(raw_s)
                except (ValueError, TypeError) as exc:
                    raise DelegationRuntimeError(f"Invalid coordination state '{raw_s}' for child '{cid}'") from exc
            else:
                norm_states[cid] = raw_s
        object.__setattr__(self, "coordination_states", norm_states)

        # Timestamps
        if not isinstance(self.created_at, (int, float)) or not math.isfinite(self.created_at):
            raise DelegationRuntimeError("created_at must be a valid finite number")
        object.__setattr__(self, "created_at", float(self.created_at))

        if not isinstance(self.updated_at, (int, float)) or not math.isfinite(self.updated_at):
            raise DelegationRuntimeError("updated_at must be a valid finite number")
        object.__setattr__(self, "updated_at", float(self.updated_at))

        # Secret scrubbing
        reject_secrets({
            "delegation_group_id": self.delegation_group_id,
            "parent_work_id": self.parent_work_id,
            "actor": self.actor,
            "session_id": self.session_id,
            "session_incarnation_id": self.session_incarnation_id,
        })

    @property
    def id(self) -> str:
        """Alias for delegation_group_id."""
        return self.delegation_group_id

    @property
    def is_sequential(self) -> bool:
        """True if the group coordinates children sequentially."""
        return self.mode == DelegationMode.SEQUENTIAL

    @property
    def is_parallel(self) -> bool:
        """True if the group coordinates children in parallel."""
        return self.mode == DelegationMode.PARALLEL

    @property
    def child_statuses(self) -> Dict[str, CoordinationState]:
        """Backwards-compatibility alias for coordination_states."""
        return self.coordination_states

    @property
    def waiting_children(self) -> Tuple[str, ...]:
        """Children currently waiting for dependencies or capacity."""
        return tuple(
            cid for cid in self.child_work_ids
            if self.coordination_states.get(cid) == CoordinationState.WAITING
        )

    @property
    def runnable_children(self) -> Tuple[str, ...]:
        """Children currently eligible to be claimed."""
        return tuple(
            cid for cid in self.child_work_ids
            if self.coordination_states.get(cid) == CoordinationState.RUNNABLE
        )

    @property
    def claimed_children(self) -> Tuple[str, ...]:
        """Children with active or completed execution claims."""
        return tuple(
            cid for cid in self.child_work_ids
            if self.coordination_states.get(cid) == CoordinationState.CLAIMED
        )

    @property
    def blocked_children(self) -> Tuple[str, ...]:
        """Children blocked by failed dependencies or expired delegations."""
        return tuple(
            cid for cid in self.child_work_ids
            if self.coordination_states.get(cid) == CoordinationState.BLOCKED
        )

    @property
    def active_children(self) -> Tuple[str, ...]:
        """Children with active claims in the coordination group."""
        return self.claimed_children

    def active_claims_count(self, children_map: Mapping[str, ChildWork]) -> int:
        """Return the number of CLAIMED children whose canonical Work is currently non-terminal."""
        count = 0
        for cid in self.child_work_ids:
            if self.coordination_states.get(cid) == CoordinationState.CLAIMED:
                cw = children_map.get(cid)
                if cw is not None and not cw.is_terminal:
                    count += 1
        return count

    def is_terminal(self, children_map: Mapping[str, ChildWork]) -> bool:
        """True if every child in the group has reached a terminal coordination or work state."""
        for cid in self.child_work_ids:
            state = self.coordination_states.get(cid)
            if state == CoordinationState.BLOCKED:
                continue
            cw = children_map.get(cid)
            if cw is None or not cw.is_terminal:
                return False
        return True

    def to_dict(self) -> Dict[str, Any]:
        """Serialize delegation group to a deterministic, schema-versioned dictionary."""
        return {
            "schema_version": self.schema_version,
            "delegation_group_id": self.delegation_group_id,
            "parent_work_id": self.parent_work_id,
            "actor": self.actor,
            "session_id": self.session_id,
            "session_incarnation_id": self.session_incarnation_id,
            "mode": self.mode.value,
            "child_work_ids": list(self.child_work_ids),
            "dependencies": {k: list(v) for k, v in self.dependencies.items()},
            "max_concurrency": self.max_concurrency,
            "coordination_states": {k: v.value for k, v in self.coordination_states.items()},
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> DelegationGroup:
        """Deserialize delegation group from a dictionary."""
        if not isinstance(data, dict):
            raise DelegationRuntimeError("DelegationGroup data must be a dictionary")
        reject_secrets(data)
        schema_v = int(data.get("schema_version", 1))
        if schema_v > CURRENT_DELEGATION_GROUP_SCHEMA_VERSION:
            raise DelegationRuntimeError(
                f"Unsupported delegation group schema version {schema_v} "
                f"(current max is {CURRENT_DELEGATION_GROUP_SCHEMA_VERSION})"
            )

        raw_deps = data.get("dependencies", {})
        norm_deps: Dict[str, Tuple[str, ...]] = {}
        if isinstance(raw_deps, dict):
            for k, v in raw_deps.items():
                if isinstance(v, (list, tuple)):
                    norm_deps[str(k)] = tuple(str(x) for x in v)

        # Handle both coordination_states and child_statuses (legacy)
        raw_states = data.get("coordination_states") or data.get("child_statuses") or {}
        norm_states: Dict[str, CoordinationState] = {}
        if isinstance(raw_states, dict):
            for k, v in raw_states.items():
                try:
                    norm_states[str(k)] = CoordinationState(str(v))
                except (ValueError, TypeError):
                    norm_states[str(k)] = CoordinationState.WAITING

        raw_children = data.get("child_work_ids", [])
        child_ids = tuple(str(x) for x in raw_children) if isinstance(raw_children, (list, tuple)) else ()

        return cls(
            delegation_group_id=str(data.get("delegation_group_id", "")),
            parent_work_id=str(data.get("parent_work_id", "")),
            actor=str(data.get("actor", "")),
            session_id=str(data.get("session_id", "")),
            session_incarnation_id=str(data.get("session_incarnation_id", "")),
            mode=DelegationMode(str(data.get("mode", "parallel"))),
            child_work_ids=child_ids,
            dependencies=norm_deps,
            max_concurrency=int(data.get("max_concurrency", DEFAULT_MAX_CONCURRENCY)),
            coordination_states=norm_states,
            created_at=float(data.get("created_at", 0.0)),
            updated_at=float(data.get("updated_at", 0.0)),
            schema_version=schema_v,
        )


class DelegationRuntime:
    """Canonical Coordination Runtime for Sequential and Parallel Child Works.

    Enforces:
    - Zero execution authority (coordination of eligibility only).
    - Separation of state domains:
        CoordinationState determines eligibility (WAITING, RUNNABLE, CLAIMED, BLOCKED).
        WorkStatus determines work lifecycle (CREATED, PLANNING, ... DONE).
        SubagentStatus determines worker lifecycle (CREATED, READY, ... COMPLETED).
    - Sequential delegation: at most one active claim (max_concurrency == 1).
    - Parallel delegation: bounded active claims (active_claims <= max_concurrency).
    - Dependency DAG satisfaction: child becomes RUNNABLE iff all dependencies are WorkStatus.DONE.
    - Idempotent atomic claims (RUNNABLE -> CLAIMED).
    - Session incarnation freshness (fail-closed across /reset and /new).
    - Delegation expiration enforcement (fail-closed -> BLOCKED).
    - Strict deterministic FIFO ordering.
    """

    @classmethod
    def create_group(
        cls,
        *,
        parent_work: Work,
        mode: Union[DelegationMode, str],
        children: Sequence[ChildWork],
        dependencies: Optional[Mapping[str, Sequence[str]]] = None,
        max_concurrency: Optional[int] = None,
        group_id: Optional[str] = None,
        created_at: Optional[float] = None,
        auto_reconcile: bool = True,
    ) -> DelegationGroup:
        """Create a new DelegationGroup bound to parent_work and children.

        Rules:
        - Mode is validated.
        - Every child must belong to the exact same parent_work_id, actor, session_id, and incarnation.
        - For sequential mode, linear dependencies are auto-constructed if not supplied.
        - For parallel mode, independent default dependencies are used if not supplied.
        - DAG validation is performed.
        - If auto_reconcile is True, initial eligibility (RUNNABLE vs WAITING) is evaluated.
        """
        now = time.time() if created_at is None else float(created_at)
        actual_group_id = validate_delegation_group_id(
            group_id or f"group_{secrets.token_hex(16)}"
        )

        # Validate parent bindings
        parent_id = parent_work.id
        parent_actor = parent_work.actor_id or getattr(parent_work, "actor", "")
        parent_session = parent_work.session_id or ""
        parent_inc = parent_work.session_incarnation_id or ""

        if not parent_actor:
            raise DelegationBindingError("Parent work lacks an authenticated actor")
        if not parent_session:
            raise DelegationBindingError("Parent work lacks a session binding")
        if not parent_inc:
            raise DelegationBindingError("Parent work lacks a session incarnation binding")

        if not children:
            raise DelegationBindingError("Delegation group requires at least one ChildWork")

        # Verify child bindings against parent
        child_work_ids: List[str] = []
        children_map: Dict[str, ChildWork] = {}
        for cw in children:
            if not isinstance(cw, ChildWork):
                raise DelegationBindingError(f"Expected ChildWork instance, got {type(cw)}")
            if cw.parent_work_id != parent_id:
                raise DelegationBindingError(
                    f"Child work '{cw.id}' parent '{cw.parent_work_id}' does not match group parent '{parent_id}'"
                )
            if cw.actor != parent_actor:
                raise DelegationBindingError(
                    f"Child work '{cw.id}' actor '{cw.actor}' does not match group actor '{parent_actor}'"
                )
            if cw.session_id != parent_session:
                raise DelegationBindingError(
                    f"Child work '{cw.id}' session '{cw.session_id}' does not match group session '{parent_session}'"
                )
            if cw.session_incarnation_id != parent_inc:
                raise DelegationBindingError(
                    f"Child work '{cw.id}' incarnation '{cw.session_incarnation_id}' does not match group incarnation '{parent_inc}'"
                )
            child_work_ids.append(cw.id)
            children_map[cw.id] = cw

        try:
            actual_mode = DelegationMode(mode) if isinstance(mode, str) else mode
            if not isinstance(actual_mode, DelegationMode):
                raise DelegationModeError(f"Invalid delegation mode: {mode}")
        except (ValueError, TypeError) as exc:
            raise DelegationModeError(f"Invalid delegation mode: {mode}") from exc

        actual_children_tuple = tuple(child_work_ids)

        # Build dependencies
        norm_deps: Dict[str, Tuple[str, ...]] = {}
        if actual_mode == DelegationMode.SEQUENTIAL:
            eff_concurrency = 1
            if dependencies is not None:
                for k, v in dependencies.items():
                    norm_deps[k] = tuple(v)
            else:
                for idx, cid in enumerate(actual_children_tuple):
                    norm_deps[cid] = () if idx == 0 else (actual_children_tuple[idx - 1],)
        else:
            eff_concurrency = (
                DEFAULT_MAX_CONCURRENCY if max_concurrency is None else max_concurrency
            )
            if dependencies is not None:
                for k, v in dependencies.items():
                    norm_deps[k] = tuple(v)
            else:
                for cid in actual_children_tuple:
                    norm_deps[cid] = ()

        # Initial coordination states: WAITING by default
        initial_states: Dict[str, CoordinationState] = {
            cid: CoordinationState.WAITING for cid in actual_children_tuple
        }

        group = DelegationGroup(
            delegation_group_id=actual_group_id,
            parent_work_id=parent_id,
            actor=parent_actor,
            session_id=parent_session,
            session_incarnation_id=parent_inc,
            mode=actual_mode,
            child_work_ids=actual_children_tuple,
            dependencies=norm_deps,
            max_concurrency=eff_concurrency,
            coordination_states=initial_states,
            created_at=now,
            updated_at=now,
        )

        if auto_reconcile:
            group = cls.reconcile(group, children_map, current_time=now)

        return group

    @classmethod
    def add_child(
        cls,
        group: DelegationGroup,
        child: ChildWork,
        dependencies: Optional[Sequence[str]] = None,
        *,
        current_time: Optional[float] = None,
        current_session_incarnation_id: Optional[str] = None,
    ) -> DelegationGroup:
        """Add a new ChildWork unit to an existing DelegationGroup immutably."""
        now = time.time() if current_time is None else float(current_time)
        if current_session_incarnation_id is not None and current_session_incarnation_id != group.session_incarnation_id:
            raise DelegationSessionStaleError("Session incarnation is stale")

        if not isinstance(child, ChildWork):
            raise DelegationBindingError(f"Expected ChildWork instance, got {type(child)}")
        if child.parent_work_id != group.parent_work_id:
            raise DelegationBindingError(
                f"Child work '{child.id}' parent '{child.parent_work_id}' does not match group parent '{group.parent_work_id}'"
            )
        if child.actor != group.actor:
            raise DelegationBindingError(
                f"Child work '{child.id}' actor '{child.actor}' does not match group actor '{group.actor}'"
            )
        if child.session_id != group.session_id:
            raise DelegationBindingError(
                f"Child work '{child.id}' session '{child.session_id}' does not match group session '{group.session_id}'"
            )
        if child.session_incarnation_id != group.session_incarnation_id:
            raise DelegationBindingError(
                f"Child work '{child.id}' incarnation '{child.session_incarnation_id}' does not match group incarnation '{group.session_incarnation_id}'"
            )
        if child.id in group.child_work_ids:
            raise DelegationBindingError(f"Child work '{child.id}' is already in delegation group")

        new_children = group.child_work_ids + (child.id,)
        if len(new_children) > MAX_CHILDREN_PER_GROUP:
            raise DelegationConcurrencyError(
                f"Child count {len(new_children)} exceeds limit {MAX_CHILDREN_PER_GROUP}"
            )

        new_deps = dict(group.dependencies)
        if group.mode == DelegationMode.SEQUENTIAL:
            if dependencies is not None:
                new_deps[child.id] = tuple(dependencies)
            else:
                last_child = group.child_work_ids[-1] if group.child_work_ids else None
                new_deps[child.id] = (last_child,) if last_child else ()
        else:
            new_deps[child.id] = tuple(dependencies or ())

        new_states = dict(group.coordination_states)
        new_states[child.id] = CoordinationState.WAITING

        return DelegationGroup(
            delegation_group_id=group.delegation_group_id,
            parent_work_id=group.parent_work_id,
            actor=group.actor,
            session_id=group.session_id,
            session_incarnation_id=group.session_incarnation_id,
            mode=group.mode,
            child_work_ids=new_children,
            dependencies=new_deps,
            max_concurrency=group.max_concurrency,
            coordination_states=new_states,
            created_at=group.created_at,
            updated_at=now,
            schema_version=group.schema_version,
        )

    @classmethod
    def reconcile(
        cls,
        group: DelegationGroup,
        children_map: Mapping[str, ChildWork],
        *,
        current_time: Optional[float] = None,
        current_session_incarnation_id: Optional[str] = None,
    ) -> DelegationGroup:
        """Deterministically evaluate dependencies, expiration, and capacity to update CoordinationState.

        Transition Rules:
        - Stale session incarnation fails closed with DelegationSessionStaleError.
        - CLAIMED children retain their claim state.
        - WAITING children whose delegation is expired become BLOCKED.
        - WAITING children with failed, cancelled, or blocked predecessors become BLOCKED.
        - WAITING children whose predecessors are all WorkStatus.DONE become RUNNABLE
          subject to available concurrency capacity (max_concurrency - active_claims - runnable_count).
        - RUNNABLE children whose delegation expired become BLOCKED.
        """
        now = time.time() if current_time is None else float(current_time)

        # Session Incarnation Freshness Enforcement
        if current_session_incarnation_id is not None:
            if current_session_incarnation_id != group.session_incarnation_id:
                raise DelegationSessionStaleError(
                    f"Delegation group session incarnation '{group.session_incarnation_id}' is stale. "
                    f"Current authoritative incarnation is '{current_session_incarnation_id}'. Action rejected."
                )

        updated_states = dict(group.coordination_states)

        # 1. Validate bindings of supplied children
        for cid in group.child_work_ids:
            cw = children_map.get(cid)
            if cw is not None:
                if cw.parent_work_id != group.parent_work_id:
                    raise DelegationBindingError(
                        f"Foreign child work '{cw.id}' with parent '{cw.parent_work_id}' rejected"
                    )
                if cw.session_incarnation_id != group.session_incarnation_id:
                    raise DelegationSessionStaleError(
                        f"Child work '{cw.id}' incarnation '{cw.session_incarnation_id}' does not match group '{group.session_incarnation_id}'"
                    )

        # 2. Check for BLOCKED conditions (failed dependencies or expired delegations)
        for cid in group.child_work_ids:
            curr = updated_states.get(cid, CoordinationState.WAITING)
            if curr in (CoordinationState.WAITING, CoordinationState.RUNNABLE):
                cw = children_map.get(cid)
                if cw is not None and cw.is_delegation_expired(current_time=now):
                    updated_states[cid] = CoordinationState.BLOCKED
                    continue

                preds = group.dependencies.get(cid, ())
                # If any predecessor is canonical FAILED or CANCELLED, or BLOCKED in coordination
                has_failed_pred = False
                for p in preds:
                    p_state = updated_states.get(p)
                    p_work = children_map.get(p)
                    if p_state == CoordinationState.BLOCKED:
                        has_failed_pred = True
                        break
                    if p_work is not None and p_work.status in (WorkStatus.FAILED, WorkStatus.CANCELLED):
                        has_failed_pred = True
                        break

                if has_failed_pred:
                    updated_states[cid] = CoordinationState.BLOCKED

        # 3. Compute capacity available for RUNNABLE state
        # Active claims = CLAIMED children whose canonical Work is NOT terminal
        active_claims = 0
        runnable_count = 0
        for cid in group.child_work_ids:
            s = updated_states.get(cid)
            if s == CoordinationState.CLAIMED:
                cw = children_map.get(cid)
                if cw is not None and not cw.is_terminal:
                    active_claims += 1
            elif s == CoordinationState.RUNNABLE:
                runnable_count += 1

        capacity = max(0, group.max_concurrency - active_claims - runnable_count)

        # 4. Promote eligible WAITING children to RUNNABLE in deterministic FIFO order
        if capacity > 0:
            for cid in group.child_work_ids:
                if capacity <= 0:
                    break
                if updated_states.get(cid) == CoordinationState.WAITING:
                    preds = group.dependencies.get(cid, ())
                    # Dependent child is eligible iff ALL predecessors have reached canonical WorkStatus.DONE
                    preds_done = True
                    for p in preds:
                        p_work = children_map.get(p)
                        if p_work is None or p_work.status != WorkStatus.DONE:
                            preds_done = False
                            break

                    if preds_done:
                        cw = children_map.get(cid)
                        if cw is not None and cw.is_delegation_expired(current_time=now):
                            updated_states[cid] = CoordinationState.BLOCKED
                            continue

                        updated_states[cid] = CoordinationState.RUNNABLE
                        capacity -= 1

        return replace(
            group,
            coordination_states=updated_states,
            updated_at=now,
        )

    @classmethod
    def claim_child(
        cls,
        group: DelegationGroup,
        child_work_id: str,
        children_map: Mapping[str, ChildWork],
        *,
        current_time: Optional[float] = None,
        current_session_incarnation_id: Optional[str] = None,
    ) -> Tuple[DelegationGroup, DispatchDecision]:
        """Atomically transition a single RUNNABLE child to CLAIMED.

        Enforces:
        - Child must be in RUNNABLE state.
        - Concurrency limits: active_claims < max_concurrency.
        - Session incarnation freshness.
        """
        now = time.time() if current_time is None else float(current_time)
        if current_session_incarnation_id is not None and current_session_incarnation_id != group.session_incarnation_id:
            raise DelegationSessionStaleError("Session incarnation is stale")

        if child_work_id not in group.child_work_ids:
            raise DelegationBindingError(f"Unknown child_work_id '{child_work_id}' in delegation group")

        curr = group.coordination_states.get(child_work_id)
        if curr == CoordinationState.CLAIMED:
            # Idempotent re-claim returns existing claim
            return group, DispatchDecision(
                child_work_id=child_work_id,
                coordination_state=CoordinationState.CLAIMED,
                claimed_at=group.updated_at,
            )

        if curr != CoordinationState.RUNNABLE:
            raise DelegationRuntimeError(
                f"Child '{child_work_id}' cannot be claimed: current coordination state is '{curr}', expected RUNNABLE"
            )

        # Check concurrency capacity
        active_claims = group.active_claims_count(children_map)
        if active_claims >= group.max_concurrency:
            raise DelegationConcurrencyError(
                f"Cannot claim child '{child_work_id}': active claims ({active_claims}) have reached max_concurrency ({group.max_concurrency})"
            )

        new_states = dict(group.coordination_states)
        new_states[child_work_id] = CoordinationState.CLAIMED

        new_group = replace(group, coordination_states=new_states, updated_at=now)
        decision = DispatchDecision(
            child_work_id=child_work_id,
            coordination_state=CoordinationState.CLAIMED,
            claimed_at=now,
        )
        return new_group, decision

    @classmethod
    def claim_next(
        cls,
        group: DelegationGroup,
        children_map: Mapping[str, ChildWork],
        *,
        current_time: Optional[float] = None,
        current_session_incarnation_id: Optional[str] = None,
    ) -> Tuple[DelegationGroup, Tuple[DispatchDecision, ...]]:
        """Atomically claim eligible RUNNABLE children up to available concurrency capacity."""
        now = time.time() if current_time is None else float(current_time)
        if current_session_incarnation_id is not None and current_session_incarnation_id != group.session_incarnation_id:
            raise DelegationSessionStaleError("Session incarnation is stale")

        active_claims = group.active_claims_count(children_map)
        available_slots = max(0, group.max_concurrency - active_claims)

        decisions: List[DispatchDecision] = []
        new_states = dict(group.coordination_states)

        for cid in group.child_work_ids:
            if available_slots <= 0:
                break
            if new_states.get(cid) == CoordinationState.RUNNABLE:
                new_states[cid] = CoordinationState.CLAIMED
                decisions.append(
                    DispatchDecision(
                        child_work_id=cid,
                        coordination_state=CoordinationState.CLAIMED,
                        claimed_at=now,
                    )
                )
                available_slots -= 1

        new_group = replace(group, coordination_states=new_states, updated_at=now)
        return new_group, tuple(decisions)

    @classmethod
    def dispatch(
        cls,
        group: DelegationGroup,
        children_map: Mapping[str, ChildWork],
        *,
        current_time: Optional[float] = None,
        current_session_incarnation_id: Optional[str] = None,
    ) -> Tuple[DelegationGroup, Tuple[DispatchDecision, ...]]:
        """Reconcile dependencies and claim all newly eligible children in one deterministic step."""
        reconciled_group = cls.reconcile(
            group,
            children_map,
            current_time=current_time,
            current_session_incarnation_id=current_session_incarnation_id,
        )
        return cls.claim_next(
            reconciled_group,
            children_map,
            current_time=current_time,
            current_session_incarnation_id=current_session_incarnation_id,
        )

    @classmethod
    def release_claim(
        cls,
        group: DelegationGroup,
        child_work_id: str,
        *,
        current_time: Optional[float] = None,
        current_session_incarnation_id: Optional[str] = None,
    ) -> DelegationGroup:
        """Release a previously claimed child back to RUNNABLE if work did not execute."""
        now = time.time() if current_time is None else float(current_time)
        if current_session_incarnation_id is not None and current_session_incarnation_id != group.session_incarnation_id:
            raise DelegationSessionStaleError("Session incarnation is stale")

        if child_work_id not in group.child_work_ids:
            raise DelegationBindingError(f"Unknown child_work_id '{child_work_id}' in delegation group")

        curr = group.coordination_states.get(child_work_id)
        if curr != CoordinationState.CLAIMED:
            return group

        new_states = dict(group.coordination_states)
        new_states[child_work_id] = CoordinationState.RUNNABLE
        return replace(group, coordination_states=new_states, updated_at=now)

    @classmethod
    def cancel_child(
        cls,
        group: DelegationGroup,
        child_work_id: str,
        reason: str = "",
        *,
        current_time: Optional[float] = None,
        current_session_incarnation_id: Optional[str] = None,
    ) -> DelegationGroup:
        """Mark a child as BLOCKED in coordination state and block dependent children."""
        now = time.time() if current_time is None else float(current_time)
        if current_session_incarnation_id is not None and current_session_incarnation_id != group.session_incarnation_id:
            raise DelegationSessionStaleError("Session incarnation is stale")

        if child_work_id not in group.child_work_ids:
            raise DelegationBindingError(f"Unknown child_work_id '{child_work_id}' in delegation group")

        new_states = dict(group.coordination_states)
        new_states[child_work_id] = CoordinationState.BLOCKED

        # Transitive blocking of downstream dependents
        def block_downstream(target: str) -> None:
            for cid in group.child_work_ids:
                if target in group.dependencies.get(cid, ()):
                    if new_states.get(cid) in (CoordinationState.WAITING, CoordinationState.RUNNABLE):
                        new_states[cid] = CoordinationState.BLOCKED
                        block_downstream(cid)

        block_downstream(child_work_id)
        return replace(group, coordination_states=new_states, updated_at=now)

    @classmethod
    def get_runnable_children(
        cls,
        group: DelegationGroup,
        children_map: Mapping[str, ChildWork],
    ) -> Tuple[ChildWork, ...]:
        """Return tuple of ChildWork objects currently in RUNNABLE coordination state."""
        return tuple(
            children_map[cid]
            for cid in group.child_work_ids
            if group.coordination_states.get(cid) == CoordinationState.RUNNABLE and cid in children_map
        )

    @classmethod
    def get_claimed_children(
        cls,
        group: DelegationGroup,
        children_map: Mapping[str, ChildWork],
    ) -> Tuple[ChildWork, ...]:
        """Return tuple of ChildWork objects currently in CLAIMED coordination state."""
        return tuple(
            children_map[cid]
            for cid in group.child_work_ids
            if group.coordination_states.get(cid) == CoordinationState.CLAIMED and cid in children_map
        )

    @classmethod
    def get_waiting_children(cls, group: DelegationGroup) -> Tuple[str, ...]:
        """Return IDs of children in WAITING coordination state."""
        return group.waiting_children

    @classmethod
    def get_blocked_children(cls, group: DelegationGroup) -> Tuple[str, ...]:
        """Return IDs of children in BLOCKED coordination state."""
        return group.blocked_children


# ==============================================================================
# Persistent Store Integration Helpers
# ==============================================================================


def save_delegation_group(
    work_store: Any,
    group: DelegationGroup,
    expected_revision: Optional[int] = None,
) -> DelegationGroup:
    """Persist a DelegationGroup by saving it inside the parent Work record's metadata.

    Preserves Optimistic Concurrency Control (OCC) and crash recovery compatibility.
    """
    parent = work_store.get(group.parent_work_id)
    if parent is None:
        raise KeyError(f"Parent work '{group.parent_work_id}' not found in WorkStore")

    meta = dict(parent.resume_metadata or {})
    groups_meta = dict(meta.get("delegation_groups") or {})
    groups_meta[group.delegation_group_id] = group.to_dict()
    meta["delegation_groups"] = groups_meta

    # Ensure observational child_work_ids references are attached to parent
    current_child_ids = list(meta.get("child_work_ids") or [])
    for cid in group.child_work_ids:
        if cid not in current_child_ids:
            current_child_ids.append(cid)
    meta["child_work_ids"] = current_child_ids

    updated_parent = parent.with_update(resume_metadata=meta)
    saved_parent = work_store.save(updated_parent, expected_revision=expected_revision)
    return group


def get_delegation_group(
    work_store: Any,
    parent_work_id: str,
    delegation_group_id: str,
) -> Optional[DelegationGroup]:
    """Retrieve and reconstruct a DelegationGroup from parent Work metadata."""
    parent = work_store.get(parent_work_id)
    if parent is None or not parent.resume_metadata:
        return None
    groups_dict = parent.resume_metadata.get("delegation_groups") or {}
    group_data = groups_dict.get(delegation_group_id)
    if not group_data or not isinstance(group_data, dict):
        return None
    try:
        return DelegationGroup.from_dict(group_data)
    except Exception:
        return None


def list_delegation_groups_for_parent(
    work_store: Any,
    parent_work_id: str,
) -> List[DelegationGroup]:
    """List all DelegationGroup instances belonging to a parent Work."""
    parent = work_store.get(parent_work_id)
    if parent is None or not parent.resume_metadata:
        return []
    groups_dict = parent.resume_metadata.get("delegation_groups") or {}
    results: List[DelegationGroup] = []
    for gdata in groups_dict.values():
        if isinstance(gdata, dict):
            try:
                results.append(DelegationGroup.from_dict(gdata))
            except Exception:
                continue
    return results


__all__ = [
    "CURRENT_DELEGATION_GROUP_SCHEMA_VERSION",
    "DEFAULT_MAX_CONCURRENCY",
    "MIN_CONCURRENCY",
    "MAX_SUBAGENT_CONCURRENCY",
    "MAX_CHILDREN_PER_GROUP",
    "MAX_DEPENDENCY_EDGES",
    "MAX_DEPENDENCY_DEPTH",
    "DelegationMode",
    "CoordinationState",
    "CoordinationStatus",
    "DispatchDecision",
    "DelegationGroup",
    "DelegationRuntime",
    "DelegationRuntimeError",
    "DelegationModeError",
    "DelegationBindingError",
    "DelegationDependencyError",
    "DelegationConcurrencyError",
    "DelegationSessionStaleError",
    "DelegationExpiredError",
    "validate_delegation_group_id",
    "validate_dependency_dag",
    "save_delegation_group",
    "get_delegation_group",
    "list_delegation_groups_for_parent",
]
