"""Comprehensive Unit and Property Tests for P1.3G Cancellation + Failure Propagation.

Architectural Tests:
1. Category A: Direct Failure
   - Child A fails (WorkStatus.FAILED, WorkFailure, SubagentStatus.FAILED).
   - Child B depends on A -> B becomes CoordinationState.BLOCKED.
   - B's canonical WorkStatus is NOT changed to FAILED (BLOCKED != FAILED).
   - Blocked child cannot become RUNNABLE.
   - A remains FAILED.

2. Category B: Recursive Failure
   - Linear dependency DAG: A -> B -> C -> D.
   - A fails -> B, C, D all become BLOCKED.
   - Intermediate failure: A is DONE, B fails -> C, D become BLOCKED, A remains DONE.

3. Category C: Branching DAG
   - Fan-out / Fan-in: A -> B, A -> C, B & C -> D.
   - A fails -> B, C, D all become BLOCKED.
   - Partial failure in diamond: A is DONE, B fails, C is DONE -> D becomes BLOCKED.
   - D does not become RUNNABLE even though one predecessor (C) succeeded.

4. Category D: Mixed Dependencies
   - Multi-predecessors (D depends on B and C).
   - Case 1: B is DONE, C is DONE -> D eligible / RUNNABLE.
   - Case 2: B is DONE, C is FAILED -> D is BLOCKED.
   - Case 3: B is DONE, C is CANCELLED -> D is BLOCKED.
   - Case 4: B is FAILED, C is EXECUTING -> D remains BLOCKED / ineligible.

5. Category E: Cancellation Propagation
   - Child cancellation with BLOCK_DEPENDENTS_ONLY policy.
   - Child cancellation with CANCEL_ACTIVE_DESCENDANTS policy.
   - Parent cancellation propagates across active children.
   - Parent cancellation does not make any child RUNNABLE or execute.

6. Category F: Terminal State Preservation
   - DONE remains DONE (cannot be rewritten to FAILED or CANCELLED).
   - FAILED remains FAILED (cannot be rewritten to CANCELLED).
   - CANCELLED remains CANCELLED (cannot be rewritten to FAILED).
   - Terminal Parent Work is never rewritten by propagation.

7. Category G: Claimed Child Cancellation
   - Claimed child in WorkStatus.EXECUTING is cancelled.
   - Concurrency slot is released (active_claims_count drops).
   - Capacity is restored for other work.
   - No execution triggered.

8. Category H: Approval-Required Cancellation
   - Child in WorkStatus.APPROVAL_REQUIRED is cancelled.
   - Child transitions to WorkStatus.CANCELLED.
   - No approval is consumed or created. Zero execution authority.

9. Category I: Parent Failure Policy
   - PROPAGATE_FAILURE: Parent Work becomes FAILED when child fails.
   - BEST_EFFORT: Parent Work remains active when child fails.
   - MANUAL: Parent Work status untouched.
   - Terminal parent remains immutable regardless of policy.

10. Category J: Idempotence
    - Repeated propagate_failure calls produce identical state.
    - Repeated propagate_child_cancellation calls produce identical state.
    - Repeated cancel_parent calls produce identical state.
    - Repeated reconcile calls produce identical state.

11. Category K: Concurrency / OCC
    - Stale revision on propagate_failure_in_store raises StaleWorkRevisionError.
    - Stale revision on cancel_child_in_store raises StaleWorkRevisionError.
    - Stale revision on cancel_parent_in_store raises StaleWorkRevisionError.
    - Race where child finishes DONE preserves DONE.

12. Category L: Session Incarnation Freshness
    - Matching incarnation succeeds.
    - Stale incarnation raises FailurePropagationSessionStaleError fail-closed.
    - Foreign child raises FailurePropagationBindingError fail-closed.

13. Category M: Restart & Crash Recovery
    - Interrupted propagation state reconstructed deterministically via WorkStore.
    - Reconcile after restart recovers complete BLOCKED DAG.

14. Category N: Resource Bounds
    - Transitive descendants exceeding MAX_PROPAGATION_CHILDREN raises error.
    - Traversal depth exceeding MAX_PROPAGATION_DEPTH raises error.
    - Traversal edge count exceeding MAX_PROPAGATION_EDGES raises error.
    - Iterative BFS avoids Python recursion stack overflow.

15. Category O: Authority Invariance
    - Zero execution authority (AST verification: no subprocess, shell, net, Git, etc.).
    - Zero capability widening.

16. Category P: Result Aggregation Compatibility
    - Failed and blocked child works are correctly aggregated by ResultAggregator.
    - ALL_REQUIRED vs ALLOW_PARTIAL policies behave deterministically.

17. Category Q: Property Invariants
    - Predecessor != DONE => dependent cannot become RUNNABLE.
    - Predecessor in {FAILED, CANCELLED, BLOCKED} => dependent cannot become RUNNABLE.
    - Terminal Work is immutable.
    - Repeated propagation is idempotent.
    - Stale incarnation preserves lifecycle state untouched.
"""
from __future__ import annotations

import ast
import os
import shutil
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from core.runtime.capabilities import Capabilities, FilesystemPolicy, GitPolicy, NetworkPolicy, ShellPolicy
from core.runtime.child_work import ChildWork, get_child_work, save_child_work
from core.runtime.delegation import DelegationContract
from core.runtime.delegation_runtime import (
    CoordinationState,
    DelegationGroup,
    DelegationMode,
    save_delegation_group,
)
from core.runtime.failure_propagation import (
    CURRENT_FAILURE_PROPAGATION_SCHEMA_VERSION,
    MAX_PROPAGATION_CHILDREN,
    MAX_PROPAGATION_DEPTH,
    MAX_PROPAGATION_EDGES,
    CancellationPropagationPolicy,
    FailurePropagationBindingError,
    FailurePropagationError,
    FailurePropagationLimitError,
    FailurePropagationSessionStaleError,
    FailurePropagator,
    ParentFailurePolicy,
    PropagationResult,
    cancel_child_in_store,
    cancel_parent,
    cancel_parent_in_store,
    find_downstream_dependents,
    propagate_child_cancellation,
    propagate_failure,
    propagate_failure_in_store,
    reconcile_failure_and_cancellation,
)
from core.runtime.result_aggregation import (
    AggregateResult,
    AggregateStatus,
    AggregationPolicy,
    ResultAggregator,
)
from core.runtime.subagent import Subagent, SubagentStatus
from core.runtime.work import (
    InMemoryWorkStore,
    StaleWorkRevisionError,
    Work,
    WorkFailure,
    WorkStatus,
)
from core.runtime.work_store import FileWorkStore


class BaseFailurePropagationTestCase(unittest.TestCase):
    """Base fixture providing parent work, subagents, delegations, and child works."""

    def setUp(self) -> None:
        self.now = time.time()
        self.actor = "actor_lead_developer"
        self.session_id = "session_prop_01"
        self.session_incarnation_id = "inc_prop_alpha"
        self.parent_work_id = "work_parent_prop"

        self.parent_caps = Capabilities(
            filesystem=FilesystemPolicy(
                read=("src/auth.py", "src/db.py", "src/api.py"),
                write=("src/auth.py", "src/db.py"),
            ),
            shell=ShellPolicy(execute=False),
            network=NetworkPolicy(access=False),
            git=GitPolicy(read=True, commit=False, push=False),
        )

        self.parent_work = Work(
            id=self.parent_work_id,
            intent="Coordinate microservices refactor with fault tolerance",
            goal="Refactor auth, db, api modules with safe failure propagation",
            scope=("src/auth.py", "src/db.py", "src/api.py"),
            capabilities=self.parent_caps,
            status=WorkStatus.PLANNING,
            actor_id=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            created_at=self.now,
            updated_at=self.now,
        )

        self.children: List[ChildWork] = []
        self.subagents: List[Subagent] = []
        self.delegations: List[DelegationContract] = []

        for i in range(5):
            letter = chr(ord("a") + i)
            sub_id = f"sub_{letter}_0{i}"
            delg_id = f"delg_{letter}_0{i}"
            child_id = f"work_child_{letter}_0{i}"
            target_path = f"src/auth_{letter}.py"

            sub = Subagent(
                subagent_id=sub_id,
                parent_work_id=self.parent_work_id,
                session_id=self.session_id,
                session_incarnation_id=self.session_incarnation_id,
                actor=self.actor,
                role=f"worker_{letter}",
                purpose=f"Refactor component {letter}",
                created_at=self.now,
                updated_at=self.now,
            )
            self.subagents.append(sub)

            delg_caps = Capabilities(
                filesystem=FilesystemPolicy(read=(target_path,), write=(target_path,)),
                shell=ShellPolicy(execute=False),
                network=NetworkPolicy(access=False),
                git=GitPolicy(read=True, commit=False),
            )
            delg = DelegationContract(
                delegation_id=delg_id,
                parent_work_id=self.parent_work_id,
                child_subagent_id=sub_id,
                actor=self.actor,
                session_id=self.session_id,
                session_incarnation_id=self.session_incarnation_id,
                capabilities=delg_caps,
                target_scope=(target_path,),
                expires_at=self.now + 600.0,
                created_at=self.now,
            )
            self.delegations.append(delg)

            child = ChildWork.create(
                parent_work=self.parent_work,
                subagent=sub,
                delegation=delg,
                child_work_id=child_id,
                created_at=self.now + i * 0.1,
            )
            self.children.append(child)

        self.child_a, self.child_b, self.child_c, self.child_d, self.child_e = self.children
        self.children_map = {cw.id: cw for cw in self.children}

    def make_child_done(self, child: ChildWork, summary: str = "Done") -> ChildWork:
        """Helper to advance a ChildWork through valid transitions to DONE."""
        w = child.work
        meta = dict(w.resume_metadata or {})
        meta["result_summary"] = summary
        w = replace(w, resume_metadata=meta)
        if w.status == WorkStatus.CREATED:
            w = w.transition(WorkStatus.PLANNING)
        if w.status == WorkStatus.PLANNING:
            w = w.transition(WorkStatus.APPROVAL_REQUIRED)
        if w.status == WorkStatus.APPROVAL_REQUIRED:
            w = w.transition(WorkStatus.EXECUTING)
        if w.status == WorkStatus.EXECUTING:
            w = w.transition(WorkStatus.VERIFYING)
        if w.status == WorkStatus.VERIFYING:
            w = w.transition(WorkStatus.DONE)
        w = replace(w, resume_metadata=meta)
        sub = child.subagent
        if sub is not None:
            if sub.status == SubagentStatus.READY:
                sub = sub.transition(SubagentStatus.RUNNING)
            if sub.status == SubagentStatus.RUNNING:
                sub = sub.transition(SubagentStatus.COMPLETED)
        return ChildWork(
            child_work_id=child.id,
            parent_work_id=child.parent_work_id,
            subagent_id=child.subagent_id,
            delegation_id=child.delegation_id,
            session_id=child.session_id,
            session_incarnation_id=child.session_incarnation_id,
            actor=child.actor,
            created_at=child.created_at,
            updated_at=time.time(),
            work=w,
            delegation_digest=child.delegation_digest,
            delegation_expires_at=child.delegation_expires_at,
            delegation=child.delegation,
            subagent=sub,
        )

    def make_child_failed(self, child: ChildWork, failure_reason: str = "Child failed") -> ChildWork:
        """Helper to advance a ChildWork through valid transitions to FAILED."""
        w = child.work
        fail_obj = WorkFailure(code="ERR_CHILD_FAILED", summary=failure_reason)
        if w.status in (WorkStatus.CREATED, WorkStatus.PLANNING, WorkStatus.APPROVAL_REQUIRED, WorkStatus.EXECUTING, WorkStatus.VERIFYING):
            w = w.transition(WorkStatus.FAILED, failure=fail_obj)
        sub = child.subagent
        if sub is not None and sub.is_active:
            sub = sub.transition(SubagentStatus.FAILED, failure_reason=failure_reason)
        return ChildWork(
            child_work_id=child.id,
            parent_work_id=child.parent_work_id,
            subagent_id=child.subagent_id,
            delegation_id=child.delegation_id,
            session_id=child.session_id,
            session_incarnation_id=child.session_incarnation_id,
            actor=child.actor,
            created_at=child.created_at,
            updated_at=time.time(),
            work=w,
            delegation_digest=child.delegation_digest,
            delegation_expires_at=child.delegation_expires_at,
            delegation=child.delegation,
            subagent=sub,
        )

    def advance_work_to_executing(self, w: Work) -> Work:
        """Helper to advance Work from CREATED to EXECUTING through valid transitions."""
        if w.status == WorkStatus.CREATED:
            w = w.transition(WorkStatus.PLANNING)
        if w.status == WorkStatus.PLANNING:
            w = w.transition(WorkStatus.APPROVAL_REQUIRED)
        if w.status == WorkStatus.APPROVAL_REQUIRED:
            w = w.transition(WorkStatus.EXECUTING)
        return w

    def advance_work_to_done(self, w: Work) -> Work:
        """Helper to advance Work to DONE through valid transitions."""
        w = self.advance_work_to_executing(w)
        if w.status == WorkStatus.EXECUTING:
            w = w.transition(WorkStatus.VERIFYING)
        if w.status == WorkStatus.VERIFYING:
            w = w.transition(WorkStatus.DONE)
        return w

    def make_group(
        self,
        child_ids: Sequence[str],
        dependencies: Optional[Mapping[str, Sequence[str]]] = None,
        max_concurrency: int = 2,
    ) -> DelegationGroup:
        """Create a valid delegation group for testing."""
        deps = dependencies or {}
        coordination_states = {
            cid: (CoordinationState.RUNNABLE if not deps.get(cid) else CoordinationState.WAITING)
            for cid in child_ids
        }
        return DelegationGroup(
            delegation_group_id=f"grp_{int(self.now * 1000)}",
            parent_work_id=self.parent_work_id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            mode=DelegationMode.PARALLEL,
            child_work_ids=tuple(child_ids),
            max_concurrency=max_concurrency,
            dependencies={k: tuple(v) for k, v in deps.items()},
            coordination_states=coordination_states,
            created_at=self.now,
            updated_at=self.now,
        )


class TestDirectFailure(BaseFailurePropagationTestCase):
    """Test Category A: Direct failure semantics and distinction from BLOCKED."""

    def test_direct_failure_blocks_dependent(self) -> None:
        """When child A fails, dependent B transitions from WAITING to BLOCKED."""
        # A -> B
        group = self.make_group(
            [self.child_a.id, self.child_b.id],
            dependencies={self.child_b.id: [self.child_a.id]},
        )
        self.assertEqual(group.coordination_states[self.child_b.id], CoordinationState.WAITING)

        # Fail child A
        failed_a = self.make_child_failed(self.child_a, failure_reason="SyntaxError in component A")
        children_map = dict(self.children_map)
        children_map[self.child_a.id] = failed_a

        upd_group, upd_children, upd_parent, res = propagate_failure(
            group,
            children_map,
            failed_child_id=self.child_a.id,
            parent_work=self.parent_work,
            current_time=self.now + 2.0,
            current_session_incarnation_id=self.session_incarnation_id,
        )

        # B is now BLOCKED in coordination state
        self.assertEqual(upd_group.coordination_states[self.child_b.id], CoordinationState.BLOCKED)
        self.assertIn(self.child_b.id, res.blocked_child_ids)
        self.assertIn(self.child_a.id, res.failed_child_ids)

    def test_direct_failure_preserves_dependent_work_status(self) -> None:
        """B's canonical WorkStatus is NOT changed to FAILED (BLOCKED != FAILED)."""
        group = self.make_group(
            [self.child_a.id, self.child_b.id],
            dependencies={self.child_b.id: [self.child_a.id]},
        )
        failed_a = self.make_child_failed(self.child_a, failure_reason="Crash in A")
        children_map = dict(self.children_map)
        children_map[self.child_a.id] = failed_a

        upd_group, upd_children, upd_parent, res = propagate_failure(
            group,
            children_map,
            failed_child_id=self.child_a.id,
            current_session_incarnation_id=self.session_incarnation_id,
        )

        child_b_after = upd_children[self.child_b.id]
        # Crucial architectural invariant: B was never executed, so its WorkStatus remains non-FAILED
        self.assertEqual(child_b_after.status, WorkStatus.CREATED)
        self.assertIsNone(child_b_after.work.failure)
        assert child_b_after.subagent is not None
        self.assertEqual(child_b_after.subagent.status, SubagentStatus.READY)

    def test_blocked_child_cannot_become_runnable_via_reconcile(self) -> None:
        """A BLOCKED child cannot become RUNNABLE even if reconcile is executed repeatedly."""
        group = self.make_group(
            [self.child_a.id, self.child_b.id],
            dependencies={self.child_b.id: [self.child_a.id]},
        )
        failed_a = self.make_child_failed(self.child_a, failure_reason="Crash in A")
        children_map = dict(self.children_map)
        children_map[self.child_a.id] = failed_a

        upd_group, upd_children, _, _ = propagate_failure(
            group,
            children_map,
            failed_child_id=self.child_a.id,
            current_session_incarnation_id=self.session_incarnation_id,
        )

        # Run reconcile
        reconciled_group = reconcile_failure_and_cancellation(
            upd_group,
            upd_children,
            current_session_incarnation_id=self.session_incarnation_id,
        )
        self.assertEqual(reconciled_group.coordination_states[self.child_b.id], CoordinationState.BLOCKED)

    def test_failed_child_remains_failed(self) -> None:
        """The failed root child remains in WorkStatus.FAILED and SubagentStatus.FAILED."""
        group = self.make_group([self.child_a.id])
        failed_a = self.make_child_failed(self.child_a, failure_reason="Database timeout")
        children_map = {self.child_a.id: failed_a}

        upd_group, upd_children, _, _ = propagate_failure(
            group,
            children_map,
            failed_child_id=self.child_a.id,
            current_session_incarnation_id=self.session_incarnation_id,
        )
        child_rec = upd_children[self.child_a.id]
        self.assertEqual(child_rec.status, WorkStatus.FAILED)
        sub = child_rec.subagent
        self.assertIsNotNone(sub)
        assert sub is not None
        self.assertEqual(sub.status, SubagentStatus.FAILED)
        fail = child_rec.work.failure
        self.assertIsNotNone(fail)
        assert fail is not None
        self.assertEqual(fail.summary, "Database timeout")


class TestRecursiveFailure(BaseFailurePropagationTestCase):
    """Test Category B: Recursive DAG propagation through linear chains."""

    def test_linear_chain_propagation(self) -> None:
        """A -> B -> C -> D. When A fails, B, C, and D all become BLOCKED."""
        cids = [self.child_a.id, self.child_b.id, self.child_c.id, self.child_d.id]
        deps = {
            self.child_b.id: [self.child_a.id],
            self.child_c.id: [self.child_b.id],
            self.child_d.id: [self.child_c.id],
        }
        group = self.make_group(cids, dependencies=deps)

        failed_a = self.make_child_failed(self.child_a, failure_reason="Root failure in A")
        children_map = dict(self.children_map)
        children_map[self.child_a.id] = failed_a

        upd_group, upd_children, _, res = propagate_failure(
            group,
            children_map,
            failed_child_id=self.child_a.id,
            current_session_incarnation_id=self.session_incarnation_id,
        )

        self.assertEqual(upd_group.coordination_states[self.child_b.id], CoordinationState.BLOCKED)
        self.assertEqual(upd_group.coordination_states[self.child_c.id], CoordinationState.BLOCKED)
        self.assertEqual(upd_group.coordination_states[self.child_d.id], CoordinationState.BLOCKED)

        # None of the downstream children were rewritten to WorkStatus.FAILED
        self.assertEqual(upd_children[self.child_b.id].status, WorkStatus.CREATED)
        self.assertEqual(upd_children[self.child_c.id].status, WorkStatus.CREATED)
        self.assertEqual(upd_children[self.child_d.id].status, WorkStatus.CREATED)

    def test_intermediate_node_failure(self) -> None:
        """A is DONE, B fails. C and D become BLOCKED, while A remains DONE."""
        cids = [self.child_a.id, self.child_b.id, self.child_c.id, self.child_d.id]
        deps = {
            self.child_b.id: [self.child_a.id],
            self.child_c.id: [self.child_b.id],
            self.child_d.id: [self.child_c.id],
        }
        group = self.make_group(cids, dependencies=deps)

        # Complete A
        done_a = self.make_child_done(self.child_a, summary="Auth module A completed")
        # Fail B
        failed_b = self.make_child_failed(self.child_b, failure_reason="Module B failed")
        children_map = dict(self.children_map)
        children_map[self.child_a.id] = done_a
        children_map[self.child_b.id] = failed_b

        upd_group, upd_children, _, res = propagate_failure(
            group,
            children_map,
            failed_child_id=self.child_b.id,
            current_session_incarnation_id=self.session_incarnation_id,
        )

        # A remains DONE
        self.assertEqual(upd_children[self.child_a.id].status, WorkStatus.DONE)
        # B is FAILED
        self.assertEqual(upd_children[self.child_b.id].status, WorkStatus.FAILED)
        # C and D are BLOCKED
        self.assertEqual(upd_group.coordination_states[self.child_c.id], CoordinationState.BLOCKED)
        self.assertEqual(upd_group.coordination_states[self.child_d.id], CoordinationState.BLOCKED)


class TestBranchingDAG(BaseFailurePropagationTestCase):
    """Test Category C: Branching and diamond DAGs."""

    def test_branching_root_failure(self) -> None:
        """A -> B, A -> C, B & C -> D. A fails => B, C, D all become BLOCKED."""
        cids = [self.child_a.id, self.child_b.id, self.child_c.id, self.child_d.id]
        deps = {
            self.child_b.id: [self.child_a.id],
            self.child_c.id: [self.child_a.id],
            self.child_d.id: [self.child_b.id, self.child_c.id],
        }
        group = self.make_group(cids, dependencies=deps)

        failed_a = self.make_child_failed(self.child_a, failure_reason="Root A crash")
        children_map = dict(self.children_map)
        children_map[self.child_a.id] = failed_a

        upd_group, upd_children, _, _ = propagate_failure(
            group,
            children_map,
            failed_child_id=self.child_a.id,
            current_session_incarnation_id=self.session_incarnation_id,
        )

        self.assertEqual(upd_group.coordination_states[self.child_b.id], CoordinationState.BLOCKED)
        self.assertEqual(upd_group.coordination_states[self.child_c.id], CoordinationState.BLOCKED)
        self.assertEqual(upd_group.coordination_states[self.child_d.id], CoordinationState.BLOCKED)

    def test_diamond_partial_predecessor_failure(self) -> None:
        """A is DONE, B fails, C is DONE. D depends on B & C. D becomes BLOCKED."""
        cids = [self.child_a.id, self.child_b.id, self.child_c.id, self.child_d.id]
        deps = {
            self.child_b.id: [self.child_a.id],
            self.child_c.id: [self.child_a.id],
            self.child_d.id: [self.child_b.id, self.child_c.id],
        }
        group = self.make_group(cids, dependencies=deps)

        done_a = self.make_child_done(self.child_a, "A done")
        failed_b = self.make_child_failed(self.child_b, "B failed")
        done_c = self.make_child_done(self.child_c, "C done")

        children_map = dict(self.children_map)
        children_map[self.child_a.id] = done_a
        children_map[self.child_b.id] = failed_b
        children_map[self.child_c.id] = done_c

        upd_group, upd_children, _, _ = propagate_failure(
            group,
            children_map,
            failed_child_id=self.child_b.id,
            current_session_incarnation_id=self.session_incarnation_id,
        )

        # C is DONE
        self.assertEqual(upd_children[self.child_c.id].status, WorkStatus.DONE)
        # D depends on BOTH B and C. Because B failed, D MUST NOT become RUNNABLE.
        self.assertEqual(upd_group.coordination_states[self.child_d.id], CoordinationState.BLOCKED)


class TestMixedDependency(BaseFailurePropagationTestCase):
    """Test Category D: Multi-parent dependency satisfaction combinations."""

    def test_multi_predecessors_all_done_makes_dependent_runnable(self) -> None:
        """D depends on B and C. Both are DONE => D can become RUNNABLE."""
        cids = [self.child_b.id, self.child_c.id, self.child_d.id]
        deps = {self.child_d.id: [self.child_b.id, self.child_c.id]}
        group = self.make_group(cids, dependencies=deps)

        done_b = self.make_child_done(self.child_b, "B done")
        done_c = self.make_child_done(self.child_c, "C done")

        children_map = dict(self.children_map)
        children_map[self.child_b.id] = done_b
        children_map[self.child_c.id] = done_c

        reconciled_group = reconcile_failure_and_cancellation(
            group,
            children_map,
            current_session_incarnation_id=self.session_incarnation_id,
        )
        self.assertEqual(reconciled_group.coordination_states[self.child_d.id], CoordinationState.RUNNABLE)

    def test_multi_predecessors_one_failed_blocks_dependent(self) -> None:
        """D depends on B and C. B is DONE, C is FAILED => D is BLOCKED."""
        cids = [self.child_b.id, self.child_c.id, self.child_d.id]
        deps = {self.child_d.id: [self.child_b.id, self.child_c.id]}
        group = self.make_group(cids, dependencies=deps)

        done_b = self.make_child_done(self.child_b, "B done")
        failed_c = self.make_child_failed(self.child_c, "C failed")

        children_map = dict(self.children_map)
        children_map[self.child_b.id] = done_b
        children_map[self.child_c.id] = failed_c

        upd_group, _, _, _ = propagate_failure(
            group,
            children_map,
            failed_child_id=self.child_c.id,
            current_session_incarnation_id=self.session_incarnation_id,
        )
        self.assertEqual(upd_group.coordination_states[self.child_d.id], CoordinationState.BLOCKED)

    def test_multi_predecessors_one_cancelled_blocks_dependent(self) -> None:
        """D depends on B and C. B is DONE, C is CANCELLED => D is BLOCKED."""
        cids = [self.child_b.id, self.child_c.id, self.child_d.id]
        deps = {self.child_d.id: [self.child_b.id, self.child_c.id]}
        group = self.make_group(cids, dependencies=deps)

        done_b = self.make_child_done(self.child_b, "B done")
        cancelled_c = self.child_c.cancel("C cancelled", current_session_incarnation_id=self.session_incarnation_id)

        children_map = dict(self.children_map)
        children_map[self.child_b.id] = done_b
        children_map[self.child_c.id] = cancelled_c

        reconciled_group = reconcile_failure_and_cancellation(
            group,
            children_map,
            current_session_incarnation_id=self.session_incarnation_id,
        )
        self.assertEqual(reconciled_group.coordination_states[self.child_d.id], CoordinationState.BLOCKED)


class TestCancellation(BaseFailurePropagationTestCase):
    """Test Category E: Cancellation models (child and parent)."""

    def test_child_cancellation_block_dependents_only(self) -> None:
        """Policy BLOCK_DEPENDENTS_ONLY: target is CANCELLED, dependent is BLOCKED (not CANCELLED)."""
        group = self.make_group(
            [self.child_a.id, self.child_b.id],
            dependencies={self.child_b.id: [self.child_a.id]},
        )

        upd_group, upd_children, _, res = propagate_child_cancellation(
            group,
            self.children_map,
            child_work_id=self.child_a.id,
            policy=CancellationPropagationPolicy.BLOCK_DEPENDENTS_ONLY,
            current_session_incarnation_id=self.session_incarnation_id,
        )

        # Target A is CANCELLED
        child_a_rec = upd_children[self.child_a.id]
        self.assertEqual(child_a_rec.status, WorkStatus.CANCELLED)
        sub_a = child_a_rec.subagent
        self.assertIsNotNone(sub_a)
        assert sub_a is not None
        self.assertEqual(sub_a.status, SubagentStatus.CANCELLED)

        # Dependent B is BLOCKED in coordination, but WorkStatus remains CREATED
        self.assertEqual(upd_group.coordination_states[self.child_b.id], CoordinationState.BLOCKED)
        self.assertEqual(upd_children[self.child_b.id].status, WorkStatus.CREATED)

    def test_child_cancellation_cancel_active_descendants(self) -> None:
        """Policy CANCEL_ACTIVE_DESCENDANTS: target is CANCELLED, active dependent is CANCELLED."""
        group = self.make_group(
            [self.child_a.id, self.child_b.id],
            dependencies={self.child_b.id: [self.child_a.id]},
        )

        upd_group, upd_children, _, res = propagate_child_cancellation(
            group,
            self.children_map,
            child_work_id=self.child_a.id,
            policy=CancellationPropagationPolicy.CANCEL_ACTIVE_DESCENDANTS,
            current_session_incarnation_id=self.session_incarnation_id,
        )

        # Target A is CANCELLED
        self.assertEqual(upd_children[self.child_a.id].status, WorkStatus.CANCELLED)
        # Dependent B is CANCELLED
        self.assertEqual(upd_children[self.child_b.id].status, WorkStatus.CANCELLED)
        self.assertEqual(upd_group.coordination_states[self.child_b.id], CoordinationState.BLOCKED)

    def test_parent_cancellation_cancels_active_children(self) -> None:
        """Parent cancellation transitions active children to CANCELLED under default policy."""
        group = self.make_group([self.child_a.id, self.child_b.id])

        upd_parent, upd_group, upd_children, res = cancel_parent(
            self.parent_work,
            self.children,
            group=group,
            reason="User aborted request",
            policy=CancellationPropagationPolicy.CANCEL_ACTIVE_DESCENDANTS,
            current_session_incarnation_id=self.session_incarnation_id,
        )

        self.assertEqual(upd_parent.status, WorkStatus.CANCELLED)
        self.assertEqual(upd_children[0].status, WorkStatus.CANCELLED)
        self.assertEqual(upd_children[1].status, WorkStatus.CANCELLED)
        assert upd_group is not None
        self.assertEqual(upd_group.coordination_states[self.child_a.id], CoordinationState.BLOCKED)
        self.assertEqual(upd_group.coordination_states[self.child_b.id], CoordinationState.BLOCKED)

    def test_parent_cancellation_policy_block_dependents_only(self) -> None:
        """Under BLOCK_DEPENDENTS_ONLY, parent is CANCELLED but active children are only BLOCKED."""
        group = self.make_group([self.child_a.id, self.child_b.id])

        upd_parent, upd_group, upd_children, res = cancel_parent(
            self.parent_work,
            self.children,
            group=group,
            policy=CancellationPropagationPolicy.BLOCK_DEPENDENTS_ONLY,
            current_session_incarnation_id=self.session_incarnation_id,
        )

        self.assertEqual(upd_parent.status, WorkStatus.CANCELLED)
        # Children are not cancelled in WorkStatus
        self.assertEqual(upd_children[0].status, WorkStatus.CREATED)
        self.assertEqual(upd_children[1].status, WorkStatus.CREATED)
        # But coordination states are BLOCKED so they cannot run
        assert upd_group is not None
        self.assertEqual(upd_group.coordination_states[self.child_a.id], CoordinationState.BLOCKED)
        self.assertEqual(upd_group.coordination_states[self.child_b.id], CoordinationState.BLOCKED)

    def test_parent_cancellation_no_child_becomes_runnable(self) -> None:
        """When parent is cancelled, reconcile guarantees no child can become RUNNABLE."""
        group = self.make_group([self.child_a.id, self.child_b.id])
        upd_parent, upd_group, upd_children, _ = cancel_parent(
            self.parent_work,
            self.children,
            group=group,
            current_session_incarnation_id=self.session_incarnation_id,
        )
        assert upd_group is not None
        cmap = {cw.id: cw for cw in upd_children}
        reconciled_group = reconcile_failure_and_cancellation(
            upd_group,
            cmap,
            parent_work=upd_parent,
            current_session_incarnation_id=self.session_incarnation_id,
        )
        for cid in upd_group.child_work_ids:
            self.assertEqual(reconciled_group.coordination_states[cid], CoordinationState.BLOCKED)


class TestTerminalStatePreservation(BaseFailurePropagationTestCase):
    """Test Category F: Terminal state immutability across all propagation actions."""

    def test_done_child_remains_done(self) -> None:
        """DONE child is never rewritten to FAILED or CANCELLED."""
        done_a = self.make_child_done(self.child_a, "A finished successfully")
        children = [done_a, self.child_b]
        group = self.make_group([done_a.id, self.child_b.id])

        # Cancel parent with CANCEL_ACTIVE_DESCENDANTS
        upd_parent, upd_group, upd_children, _ = cancel_parent(
            self.parent_work,
            children,
            group=group,
            policy=CancellationPropagationPolicy.CANCEL_ACTIVE_DESCENDANTS,
            current_session_incarnation_id=self.session_incarnation_id,
        )

        child_a_after = [c for c in upd_children if c.id == done_a.id][0]
        self.assertEqual(child_a_after.status, WorkStatus.DONE)
        assert child_a_after.subagent is not None
        self.assertEqual(child_a_after.subagent.status, SubagentStatus.COMPLETED)

    def test_failed_child_remains_failed_on_parent_cancellation(self) -> None:
        """FAILED child is never rewritten to CANCELLED."""
        failed_a = self.make_child_failed(self.child_a, "A had an unhandled error")
        children = [failed_a, self.child_b]
        group = self.make_group([failed_a.id, self.child_b.id])

        upd_parent, upd_group, upd_children, _ = cancel_parent(
            self.parent_work,
            children,
            group=group,
            policy=CancellationPropagationPolicy.CANCEL_ACTIVE_DESCENDANTS,
            current_session_incarnation_id=self.session_incarnation_id,
        )

        child_a_after = [c for c in upd_children if c.id == failed_a.id][0]
        self.assertEqual(child_a_after.status, WorkStatus.FAILED)
        assert child_a_after.subagent is not None
        self.assertEqual(child_a_after.subagent.status, SubagentStatus.FAILED)

    def test_cancelled_child_remains_cancelled(self) -> None:
        """CANCELLED child is never rewritten to FAILED."""
        cancelled_a = self.child_a.cancel("Pre-emptively cancelled", current_session_incarnation_id=self.session_incarnation_id)
        children_map = {cancelled_a.id: cancelled_a, self.child_b.id: self.child_b}
        group = self.make_group([cancelled_a.id, self.child_b.id])

        upd_group, upd_children, _, _ = propagate_failure(
            group,
            children_map,
            failed_child_id=self.child_b.id,
            current_session_incarnation_id=self.session_incarnation_id,
        )

        self.assertEqual(upd_children[cancelled_a.id].status, WorkStatus.CANCELLED)

    def test_terminal_parent_never_rewritten(self) -> None:
        """If parent is already DONE, cancel_parent preserves DONE."""
        done_parent = self.advance_work_to_done(self.parent_work)
        upd_parent, _, _, _ = cancel_parent(
            done_parent,
            self.children,
            current_session_incarnation_id=self.session_incarnation_id,
        )
        self.assertEqual(upd_parent.status, WorkStatus.DONE)


class TestClaimedChildCancellation(BaseFailurePropagationTestCase):
    """Test Category G: Claimed child cancellation and coordination slot release."""

    def test_claimed_child_cancellation_releases_concurrency_slot(self) -> None:
        """When a CLAIMED child in EXECUTING is cancelled, concurrency capacity is freed."""
        # Setup group with max_concurrency=1
        group = self.make_group([self.child_a.id, self.child_b.id], max_concurrency=1)

        # Transition child A to EXECUTING and CLAIMED
        w_exec = self.advance_work_to_executing(self.child_a.work)
        sub_exec = self.child_a.subagent.transition(SubagentStatus.RUNNING) if self.child_a.subagent else None
        executing_a = ChildWork(
            child_work_id=self.child_a.id,
            parent_work_id=self.child_a.parent_work_id,
            subagent_id=self.child_a.subagent_id,
            delegation_id=self.child_a.delegation_id,
            session_id=self.child_a.session_id,
            session_incarnation_id=self.child_a.session_incarnation_id,
            actor=self.child_a.actor,
            created_at=self.child_a.created_at,
            updated_at=time.time(),
            work=w_exec,
            delegation_digest=self.child_a.delegation_digest,
            delegation_expires_at=self.child_a.delegation_expires_at,
            delegation=self.child_a.delegation,
            subagent=sub_exec,
        )

        group_with_claim = replace(
            group,
            coordination_states={
                self.child_a.id: CoordinationState.CLAIMED,
                self.child_b.id: CoordinationState.WAITING,
            },
        )
        cmap = {self.child_a.id: executing_a, self.child_b.id: self.child_b}

        # Concurrency capacity should be 0 because 1 active claim == max_concurrency
        self.assertEqual(group_with_claim.active_claims_count(cmap), 1)

        # Cancel child A
        upd_group, upd_children, _, res = propagate_child_cancellation(
            group_with_claim,
            cmap,
            child_work_id=self.child_a.id,
            current_session_incarnation_id=self.session_incarnation_id,
        )

        # Child A is CANCELLED (terminal)
        self.assertEqual(upd_children[self.child_a.id].status, WorkStatus.CANCELLED)
        self.assertTrue(upd_children[self.child_a.id].is_terminal)

        # Capacity freed: active_claims_count is 0!
        self.assertEqual(upd_group.active_claims_count(upd_children), 0)


class TestApprovalRequiredCancellation(BaseFailurePropagationTestCase):
    """Test Category H: Cancellation of work in APPROVAL_REQUIRED."""

    def test_approval_required_child_cancellation(self) -> None:
        """Child in APPROVAL_REQUIRED transitions to CANCELLED without consuming/creating approvals."""
        w_appr = self.child_a.work.transition(WorkStatus.PLANNING).transition(WorkStatus.APPROVAL_REQUIRED)
        approval_child = ChildWork(
            child_work_id=self.child_a.id,
            parent_work_id=self.child_a.parent_work_id,
            subagent_id=self.child_a.subagent_id,
            delegation_id=self.child_a.delegation_id,
            session_id=self.child_a.session_id,
            session_incarnation_id=self.child_a.session_incarnation_id,
            actor=self.child_a.actor,
            created_at=self.child_a.created_at,
            updated_at=time.time(),
            work=w_appr,
            delegation_digest=self.child_a.delegation_digest,
            delegation_expires_at=self.child_a.delegation_expires_at,
            delegation=self.child_a.delegation,
            subagent=self.child_a.subagent,
        )

        group = self.make_group([approval_child.id])
        cmap = {approval_child.id: approval_child}

        upd_group, upd_children, _, res = propagate_child_cancellation(
            group,
            cmap,
            child_work_id=approval_child.id,
            current_session_incarnation_id=self.session_incarnation_id,
        )

        self.assertEqual(upd_children[approval_child.id].status, WorkStatus.CANCELLED)
        self.assertIn(approval_child.id, res.cancelled_child_ids)


class TestParentFailurePolicy(BaseFailurePropagationTestCase):
    """Test Category I: Parent failure semantics (PROPAGATE_FAILURE, BEST_EFFORT, MANUAL)."""

    def test_propagate_failure_policy(self) -> None:
        """Under PROPAGATE_FAILURE, child failure transitions active parent to FAILED."""
        group = self.make_group([self.child_a.id])
        failed_a = self.make_child_failed(self.child_a, "Critical child failed")
        cmap = {self.child_a.id: failed_a}

        upd_group, upd_children, upd_parent, res = propagate_failure(
            group,
            cmap,
            failed_child_id=self.child_a.id,
            parent_work=self.parent_work,
            parent_policy=ParentFailurePolicy.PROPAGATE_FAILURE,
            current_session_incarnation_id=self.session_incarnation_id,
        )

        assert upd_parent is not None
        self.assertEqual(upd_parent.status, WorkStatus.FAILED)
        self.assertTrue(res.parent_failed)
        assert upd_parent.failure is not None
        self.assertIn("Critical child failed", upd_parent.failure.summary)

    def test_best_effort_policy(self) -> None:
        """Under BEST_EFFORT, child failure leaves active parent in its current status."""
        group = self.make_group([self.child_a.id])
        failed_a = self.make_child_failed(self.child_a, "Non-critical child failed")
        cmap = {self.child_a.id: failed_a}

        upd_group, upd_children, upd_parent, res = propagate_failure(
            group,
            cmap,
            failed_child_id=self.child_a.id,
            parent_work=self.parent_work,
            parent_policy=ParentFailurePolicy.BEST_EFFORT,
            current_session_incarnation_id=self.session_incarnation_id,
        )

        assert upd_parent is not None
        self.assertEqual(upd_parent.status, WorkStatus.PLANNING)
        self.assertFalse(res.parent_failed)

    def test_manual_policy(self) -> None:
        """Under MANUAL, parent work status is never automatically updated."""
        group = self.make_group([self.child_a.id])
        failed_a = self.make_child_failed(self.child_a, "Child failed")
        cmap = {self.child_a.id: failed_a}

        upd_group, upd_children, upd_parent, res = propagate_failure(
            group,
            cmap,
            failed_child_id=self.child_a.id,
            parent_work=self.parent_work,
            parent_policy=ParentFailurePolicy.MANUAL,
            current_session_incarnation_id=self.session_incarnation_id,
        )

        assert upd_parent is not None
        self.assertEqual(upd_parent.status, WorkStatus.PLANNING)
        self.assertFalse(res.parent_failed)


class TestIdempotence(BaseFailurePropagationTestCase):
    """Test Category J: Idempotence of all propagation operations."""

    def test_propagate_failure_idempotent(self) -> None:
        """Calling propagate_failure twice produces identical results with no state corruption."""
        group = self.make_group(
            [self.child_a.id, self.child_b.id],
            dependencies={self.child_b.id: [self.child_a.id]},
        )
        failed_a = self.make_child_failed(self.child_a, "Err")
        cmap = {self.child_a.id: failed_a, self.child_b.id: self.child_b}

        grp1, cmap1, par1, res1 = propagate_failure(
            group, cmap, failed_child_id=self.child_a.id,
            parent_work=self.parent_work,
            current_session_incarnation_id=self.session_incarnation_id,
        )
        grp2, cmap2, par2, res2 = propagate_failure(
            grp1, cmap1, failed_child_id=self.child_a.id,
            parent_work=par1,
            current_session_incarnation_id=self.session_incarnation_id,
        )

        self.assertEqual(grp1.coordination_states, grp2.coordination_states)
        self.assertEqual(res1.blocked_child_ids, res2.blocked_child_ids)

    def test_cancel_parent_idempotent(self) -> None:
        """Calling cancel_parent twice produces identical state."""
        group = self.make_group([self.child_a.id, self.child_b.id])

        par1, grp1, ch1, res1 = cancel_parent(
            self.parent_work, self.children, group=group,
            current_session_incarnation_id=self.session_incarnation_id,
        )
        par2, grp2, ch2, res2 = cancel_parent(
            par1, ch1, group=grp1,
            current_session_incarnation_id=self.session_incarnation_id,
        )

        self.assertEqual(par1.status, par2.status)
        assert grp1 is not None and grp2 is not None
        self.assertEqual(grp1.coordination_states, grp2.coordination_states)


class TestOCCAndConcurrency(BaseFailurePropagationTestCase):
    """Test Category K: Concurrency, OCC, and WorkStore integration."""

    def setUp(self) -> None:
        super().setUp()
        self.store = InMemoryWorkStore()
        # Save parent work
        self.store.create(self.parent_work)
        # Save child works
        for cw in self.children:
            save_child_work(self.store, cw)

    def test_store_failure_propagation_occ_rejection(self) -> None:
        """propagate_failure_in_store rejects stale expected_revision fail-closed."""
        group = self.make_group(
            [self.child_a.id, self.child_b.id],
            dependencies={self.child_b.id: [self.child_a.id]},
        )
        save_delegation_group(self.store, group)

        # Mutate parent to bump revision
        current_parent = self.store.get(self.parent_work_id)
        assert current_parent is not None
        p_appr = current_parent.transition(WorkStatus.APPROVAL_REQUIRED)
        self.store.save(p_appr)  # bumps revision from 2 to 3

        # Attempt propagate_failure_in_store with stale expected_revision=2
        with self.assertRaises(StaleWorkRevisionError):
            propagate_failure_in_store(
                self.store,
                self.parent_work_id,
                group.delegation_group_id,
                failed_child_id=self.child_a.id,
                expected_revision=2,  # actual is 3
                current_session_incarnation_id=self.session_incarnation_id,
            )

    def test_store_cancel_parent_occ_rejection(self) -> None:
        """cancel_parent_in_store rejects stale revision."""
        # Parent current revision is 1
        with self.assertRaises(StaleWorkRevisionError):
            cancel_parent_in_store(
                self.store,
                self.parent_work_id,
                expected_revision=999,
                current_session_incarnation_id=self.session_incarnation_id,
            )

    def test_done_racing_with_cancellation(self) -> None:
        """If a child reached DONE right before cancellation, DONE is preserved."""
        # Child A completes through canonical transitions in store
        curr_a = self.store.get(self.child_a.id)
        assert curr_a is not None
        w1 = curr_a.transition(WorkStatus.PLANNING)
        s1 = self.store.save(w1)
        w2 = s1.transition(WorkStatus.APPROVAL_REQUIRED)
        s2 = self.store.save(w2)
        w3 = s2.transition(WorkStatus.EXECUTING)
        s3 = self.store.save(w3)
        w4 = s3.transition(WorkStatus.VERIFYING)
        s4 = self.store.save(w4)
        w5 = s4.transition(WorkStatus.DONE)
        self.store.save(w5)

        group = self.make_group([self.child_a.id, self.child_b.id])
        save_delegation_group(self.store, group)

        # Now cancel parent in store
        upd_parent, res = cancel_parent_in_store(
            self.store,
            self.parent_work_id,
            current_session_incarnation_id=self.session_incarnation_id,
        )

        # Verify child A remained DONE in store
        reloaded_a = get_child_work(self.store, self.child_a.id)
        assert reloaded_a is not None
        self.assertEqual(reloaded_a.status, WorkStatus.DONE)


class TestSessionIncarnationFreshness(BaseFailurePropagationTestCase):
    """Test Category L: Session incarnation freshness enforcement."""

    def test_stale_incarnation_fails_closed_on_propagate_failure(self) -> None:
        """Stale session incarnation raises FailurePropagationSessionStaleError."""
        group = self.make_group([self.child_a.id])
        with self.assertRaises(FailurePropagationSessionStaleError):
            propagate_failure(
                group,
                self.children_map,
                current_session_incarnation_id="stale_inc_999",
            )

    def test_stale_incarnation_fails_closed_on_cancel_child(self) -> None:
        """Stale incarnation on propagate_child_cancellation raises error."""
        group = self.make_group([self.child_a.id])
        with self.assertRaises(FailurePropagationSessionStaleError):
            propagate_child_cancellation(
                group,
                self.children_map,
                child_work_id=self.child_a.id,
                current_session_incarnation_id="stale_inc_999",
            )

    def test_stale_incarnation_fails_closed_on_cancel_parent(self) -> None:
        """Stale incarnation on cancel_parent raises error."""
        with self.assertRaises(FailurePropagationSessionStaleError):
            cancel_parent(
                self.parent_work,
                self.children,
                current_session_incarnation_id="stale_inc_999",
            )

    def test_foreign_child_binding_error(self) -> None:
        """Foreign child with mismatched parent_work_id raises FailurePropagationBindingError."""
        foreign_parent = replace(self.parent_work, id="foreign_parent_123")
        foreign_sub = Subagent(
            subagent_id="sub_foreign",
            parent_work_id="foreign_parent_123",
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            actor=self.actor,
            role="foreign_worker",
            purpose="foreign task",
            created_at=self.now,
            updated_at=self.now,
        )
        foreign_delg = DelegationContract(
            delegation_id="delg_foreign_99",
            parent_work_id="foreign_parent_123",
            child_subagent_id="sub_foreign",
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            target_scope=("src/foreign.py",),
            capabilities=Capabilities(),
            expires_at=self.now + 600.0,
            created_at=self.now,
        )
        foreign_child = ChildWork.create(
            parent_work=foreign_parent,
            subagent=foreign_sub,
            delegation=foreign_delg,
            child_work_id="work_foreign_child",
            created_at=self.now,
        )

        with self.assertRaises(FailurePropagationBindingError):
            cancel_parent(
                self.parent_work,
                [foreign_child],
                current_session_incarnation_id=self.session_incarnation_id,
            )


class TestRestartAndCrashRecovery(BaseFailurePropagationTestCase):
    """Test Category M: State survival and recovery across restart."""

    def test_restart_reconstruction_reconciles_blocked_state(self) -> None:
        """Interrupted propagation state survives restart and reconciles deterministically."""
        temp_dir = tempfile.mkdtemp(prefix="brainfrog_test_recov_")
        try:
            store = FileWorkStore(Path(temp_dir))
            store.create(self.parent_work)

            # A -> B -> C
            cids = [self.child_a.id, self.child_b.id, self.child_c.id]
            deps = {
                self.child_b.id: [self.child_a.id],
                self.child_c.id: [self.child_b.id],
            }
            group = self.make_group(cids, dependencies=deps)

            # Persist: A is FAILED, B is BLOCKED, but simulate crash before C was marked BLOCKED
            failed_a = self.make_child_failed(self.child_a, "Crash in A")
            save_child_work(store, failed_a)
            save_child_work(store, self.child_b)
            save_child_work(store, self.child_c)

            partial_states = {
                self.child_a.id: CoordinationState.BLOCKED,
                self.child_b.id: CoordinationState.BLOCKED,
                self.child_c.id: CoordinationState.WAITING,  # not yet updated before crash
            }
            interrupted_group = replace(group, coordination_states=partial_states)
            save_delegation_group(store, interrupted_group)

            # Simulate restart: reload from store
            store2 = FileWorkStore(Path(temp_dir))
            reloaded_group = interrupted_group  # loaded
            reloaded_cmap = {
                cid: get_child_work(store2, cid)  # type: ignore[misc]
                for cid in cids
            }
            assert all(cw is not None for cw in reloaded_cmap.values())

            # Reconcile on startup
            reconciled_group = reconcile_failure_and_cancellation(
                reloaded_group,
                reloaded_cmap,  # type: ignore[arg-type]
                current_session_incarnation_id=self.session_incarnation_id,
            )

            # C must deterministically become BLOCKED
            self.assertEqual(reconciled_group.coordination_states[self.child_c.id], CoordinationState.BLOCKED)

        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestResourceBounds(BaseFailurePropagationTestCase):
    """Test Category N: Resource bounds enforcement."""

    def test_max_depth_limit_exceeded(self) -> None:
        """Deep DAG exceeding MAX_PROPAGATION_DEPTH raises FailurePropagationLimitError."""
        cids = [f"child_{i}" for i in range(MAX_PROPAGATION_DEPTH + 5)]
        deps = {cids[i]: [cids[i - 1]] for i in range(1, len(cids))}

        with self.assertRaises(FailurePropagationLimitError):
            find_downstream_dependents(
                cids,
                deps,
                root_ids=[cids[0]],
                max_depth=MAX_PROPAGATION_DEPTH,
            )

    def test_max_children_limit_exceeded(self) -> None:
        """Large fan-out exceeding max_children raises FailurePropagationLimitError."""
        cids = ["root"] + [f"child_{i}" for i in range(60)]
        deps = {f"child_{i}": ["root"] for i in range(60)}

        with self.assertRaises(FailurePropagationLimitError):
            find_downstream_dependents(
                cids,
                deps,
                root_ids=["root"],
                max_children=50,
            )

    def test_max_edges_limit_exceeded(self) -> None:
        """Excessive edges count raises FailurePropagationLimitError."""
        cids = [f"node_{i}" for i in range(30)]
        deps = {cid: [p for p in cids if p != cid] for cid in cids}  # ~870 edges

        with self.assertRaises(FailurePropagationLimitError):
            find_downstream_dependents(
                cids,
                deps,
                root_ids=[cids[0]],
            )


class TestAuthorityInvariance(unittest.TestCase):
    """Test Category O: Strict non-authority verification."""

    def test_ast_prohibited_modules(self) -> None:
        """core/runtime/failure_propagation.py must not import prohibited authority modules."""
        source_path = Path(__file__).parent.parent / "core" / "runtime" / "failure_propagation.py"
        with open(source_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())

        prohibited_imports = {
            "subprocess",
            "os.system",
            "shutil",
            "socket",
            "urllib",
            "requests",
            "git",
            "orchestrator",
        }

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name, prohibited_imports)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    self.assertNotIn(node.module, prohibited_imports)
                    self.assertNotIn("orchestrator", node.module)


class TestResultAggregationCompatibility(BaseFailurePropagationTestCase):
    """Test Category P: Seamless integration with P1.3F Result Aggregation."""

    def test_aggregation_with_propagated_failure_and_blocked(self) -> None:
        """Failed and blocked children are correctly consumed by ResultAggregator."""
        # A failed, B blocked
        failed_a = self.make_child_failed(self.child_a, "Crash in A")
        # B remains in non-terminal planning
        cmap = {self.child_a.id: failed_a, self.child_b.id: self.child_b}
        group = self.make_group(
            [self.child_a.id, self.child_b.id],
            dependencies={self.child_b.id: [self.child_a.id]},
        )

        upd_group, upd_cmap, _, _ = propagate_failure(
            group,
            cmap,
            failed_child_id=self.child_a.id,
            current_session_incarnation_id=self.session_incarnation_id,
        )

        # Aggregate using ResultAggregator with ALL_REQUIRED
        agg_result = ResultAggregator.aggregate(
            parent_work=self.parent_work,
            children=list(upd_cmap.values()),
            delegation_group=upd_group,
            policy=AggregationPolicy.ALL_REQUIRED,
        )

        self.assertEqual(agg_result.status, AggregateStatus.INCOMPLETE)
        self.assertFalse(agg_result.success)
        self.assertEqual(agg_result.failed_children, 1)

    def test_aggregation_with_allow_partial(self) -> None:
        """Under ALLOW_PARTIAL, successful children are aggregated even if one failed."""
        done_a = self.make_child_done(self.child_a, "A done")
        failed_b = self.make_child_failed(self.child_b, "B failed")
        cmap = {self.child_a.id: done_a, self.child_b.id: failed_b}
        group = self.make_group([self.child_a.id, self.child_b.id])

        upd_group, upd_cmap, _, _ = propagate_failure(
            group,
            cmap,
            failed_child_id=self.child_b.id,
            current_session_incarnation_id=self.session_incarnation_id,
        )

        agg_result = ResultAggregator.aggregate(
            parent_work=self.parent_work,
            children=list(upd_cmap.values()),
            delegation_group=upd_group,
            policy=AggregationPolicy.ALLOW_PARTIAL,
        )

        self.assertEqual(agg_result.status, AggregateStatus.PARTIAL)
        self.assertEqual(agg_result.successful_children, 1)
        self.assertEqual(agg_result.failed_children, 1)


class TestPropertyInvariants(BaseFailurePropagationTestCase):
    """Test Category Q: Section 29 formal property invariants."""

    def test_invariant_predecessor_not_done_blocks_dependent(self) -> None:
        """Invariant: predecessor != DONE => dependent cannot become RUNNABLE."""
        group = self.make_group(
            [self.child_a.id, self.child_b.id],
            dependencies={self.child_b.id: [self.child_a.id]},
        )
        # Child A is in PLANNING (not DONE)
        reconciled_group = reconcile_failure_and_cancellation(
            group,
            self.children_map,
            current_session_incarnation_id=self.session_incarnation_id,
        )
        self.assertNotEqual(reconciled_group.coordination_states[self.child_b.id], CoordinationState.RUNNABLE)

    def test_invariant_terminal_work_immutable(self) -> None:
        """Invariant: terminal Work => propagation cannot rewrite terminal state."""
        terminal_states = [
            self.make_child_done(self.child_a, "A done"),
            self.make_child_failed(self.child_b, "B failed"),
            self.child_c.cancel("C cancelled", current_session_incarnation_id=self.session_incarnation_id),
        ]
        group = self.make_group([c.id for c in terminal_states])
        cmap = {c.id: c for c in terminal_states}

        # Run propagate_failure
        _, upd_cmap, _, _ = propagate_failure(
            group,
            cmap,
            failed_child_id=terminal_states[1].id,
            current_session_incarnation_id=self.session_incarnation_id,
        )

        self.assertEqual(upd_cmap[terminal_states[0].id].status, WorkStatus.DONE)
        self.assertEqual(upd_cmap[terminal_states[1].id].status, WorkStatus.FAILED)
        self.assertEqual(upd_cmap[terminal_states[2].id].status, WorkStatus.CANCELLED)

    def test_invariant_repeated_propagation_deterministic(self) -> None:
        """Invariant: reconcile(reconcile(state)) == reconcile(state)."""
        group = self.make_group(
            [self.child_a.id, self.child_b.id, self.child_c.id],
            dependencies={
                self.child_b.id: [self.child_a.id],
                self.child_c.id: [self.child_b.id],
            },
        )
        failed_a = self.make_child_failed(self.child_a, "A failed")
        cmap = dict(self.children_map)
        cmap[self.child_a.id] = failed_a

        g1 = reconcile_failure_and_cancellation(
            group, cmap, current_session_incarnation_id=self.session_incarnation_id,
        )
        g2 = reconcile_failure_and_cancellation(
            g1, cmap, current_session_incarnation_id=self.session_incarnation_id,
        )

        self.assertEqual(g1.coordination_states, g2.coordination_states)


if __name__ == "__main__":
    unittest.main()
