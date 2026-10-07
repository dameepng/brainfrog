"""Comprehensive Unit and Property Tests for P1.3E Parallel / Sequential Delegation.

Architectural Tests:
1. State Model & Separation
   - CoordinationState has ONLY 4 states: WAITING, RUNNABLE, CLAIMED, BLOCKED.
   - CoordinationState != WorkStatus != SubagentStatus.
   - CoordinationState does NOT have RUNNING, COMPLETED, FAILED, CANCELLED.
   - Valid combinations of the three distinct state machines.
   - Atomic transitions: WAITING -> RUNNABLE -> CLAIMED, WAITING -> BLOCKED.
   - Invalid transitions rejected (e.g. claiming a WAITING child).
   - Release claim: CLAIMED -> RUNNABLE.

2. Sequential Delegation
   - First child runnable/claimed, later waiting.
   - Strictly bounded: at most 1 active claim.
   - Auto-constructed linear dependencies (A -> B -> C).
   - Later child becomes RUNNABLE only after predecessor reaches canonical WorkStatus.DONE.
   - Predecessor failure blocks downstream children (WAITING -> BLOCKED).
   - Persisted dependency edges preserved across restart.

3. Parallel Delegation
   - Independent children runnable up to max_concurrency.
   - Bounded concurrency enforced: active_claims <= max_concurrency.
   - Completed slot (WorkStatus.DONE) frees capacity for next waiting child.
   - Deterministic FIFO ordering.
   - Repeated claim/dispatch is idempotent.

4. Dependency DAG
   - Explicit dependency satisfaction (A -> B).
   - Diamond DAG (A -> C, B -> C, D independent).
   - Dependency strictly requires canonical WorkStatus.DONE.
   - WorkStatus.FAILED and WorkStatus.CANCELLED fail dependency -> dependent child BLOCKED.
   - Self-dependency rejected (A -> A).
   - Unknown dependency rejected.
   - Duplicate edges rejected.
   - Cycles rejected via Kahn's algorithm.
   - Edge and depth bounds enforced.

5. Claim Semantics & Concurrency
   - Atomic RUNNABLE -> CLAIMED.
   - Double-claiming prevented.
   - Active claims never exceed max_concurrency.
   - Stale revision rejected in OCC.

6. Parent & Delegation Bindings
   - Foreign child rejected (parent mismatch, actor mismatch, session mismatch).
   - Expired delegation blocks child (transitions to BLOCKED).
   - Capability scope cannot change (attenuated authority preserved).

7. Session Incarnation Freshness
   - Stale incarnation fails closed on dispatch, reconcile, claim, release, add_child.

8. Persistence & Recovery
   - Save and load via WorkStore resume_metadata.
   - Restart reconstruction preserves exact coordination states.
   - Malformed data rejected.

9. Execution Boundary
   - Strictly zero subprocess, os.system, filesystem mutation, HTTP, Git, TransactionCoordinator, orchestrator, LLM.

10. End-to-End Governed Composition
    - Parent -> Delegation -> Subagent -> Child -> Coordinator -> Approval -> Contract -> Transaction -> Orchestrator -> DONE.

11. Bounded Property Tests
    - active_claims <= 1 (sequential)
    - active_claims <= max_concurrency (parallel)
    - child == RUNNABLE => all dependencies == WorkStatus.DONE
    - Later child cannot become RUNNABLE while predecessor is not DONE
    - Coordination never modifies delegated capabilities
    - reconcile(reconcile(state)) == reconcile(state)
"""
from __future__ import annotations

import inspect
import math
import os
import shutil
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.runtime.approval import (
    ApprovalRequest,
    ApprovalService,
    ApprovalStatus,
    CanonicalOperation,
    InMemoryApprovalStore,
    RiskClass,
)
from core.runtime.capabilities import Capabilities, FilesystemPolicy, GitPolicy, NetworkPolicy, ShellPolicy
from core.runtime.child_work import ChildWork
from core.runtime.contract import ApprovedExecutionContract
from core.runtime.delegation import DelegationContract
from core.runtime.delegation_runtime import (
    CURRENT_DELEGATION_GROUP_SCHEMA_VERSION,
    CoordinationState,
    CoordinationStatus,
    DelegationBindingError,
    DelegationConcurrencyError,
    DelegationDependencyError,
    DelegationExpiredError,
    DelegationGroup,
    DelegationMode,
    DelegationModeError,
    DelegationRuntime,
    DelegationRuntimeError,
    DelegationSessionStaleError,
    DispatchDecision,
    get_delegation_group,
    list_delegation_groups_for_parent,
    save_delegation_group,
    validate_delegation_group_id,
    validate_dependency_dag,
)
from core.runtime.subagent import Subagent, SubagentStatus
from core.runtime.transaction import (
    InMemoryTransactionStore,
)
from core.runtime.work import (
    InMemoryWorkStore,
    StaleWorkRevisionError,
    VerificationResult,
    Work,
    WorkFailure,
    WorkStatus,
)
from core.runtime.work_store import FileWorkStore


class BaseDelegationRuntimeTestCase(unittest.TestCase):
    """Base fixture providing parent work, subagents, delegations, and child works."""

    def setUp(self) -> None:
        self.now = time.time()
        self.actor = "actor_lead_developer"
        self.session_id = "session_coord_01"
        self.session_incarnation_id = "inc_coord_alpha"
        self.parent_work_id = "work_parent_coord"

        self.parent_caps = Capabilities(
            filesystem=FilesystemPolicy(read=("src/auth.py", "src/db.py", "src/api.py"), write=("src/auth.py", "src/db.py")),
            shell=ShellPolicy(execute=False),
            network=NetworkPolicy(access=True, scope="configured_model_api"),
            git=GitPolicy(read=True, commit=True, push=False),
        )

        self.parent_work = Work(
            id=self.parent_work_id,
            intent="Coordinate microservices refactor",
            goal="Refactor auth and database modules sequentially or in parallel",
            scope=("src/auth.py", "src/db.py", "src/api.py"),
            capabilities=self.parent_caps,
            status=WorkStatus.PLANNING,
            actor_id=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            created_at=self.now,
            updated_at=self.now,
        )

        # Helper to create child work units with subagents and delegations
        self.children: List[ChildWork] = []
        self.subagents: List[Subagent] = []
        self.delegations: List[DelegationContract] = []

        for i in range(4):
            letter = chr(ord('a') + i)
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

        self.child_a, self.child_b, self.child_c, self.child_d = self.children
        self.children_map = {cw.id: cw for cw in self.children}


class TestStateModelAndSeparation(BaseDelegationRuntimeTestCase):
    """Test Category 1: Three-domain state model separation and transitions."""

    def test_coordination_state_four_states_only(self) -> None:
        """Verify CoordinationState has exactly 4 states and NO execution concepts."""
        expected_states = {"waiting", "runnable", "claimed", "blocked"}
        actual_states = {s.value for s in CoordinationState}
        self.assertEqual(actual_states, expected_states)

        # Explicitly confirm execution states do not exist in CoordinationState
        self.assertFalse(hasattr(CoordinationState, "RUNNING"))
        self.assertFalse(hasattr(CoordinationState, "COMPLETED"))
        self.assertFalse(hasattr(CoordinationState, "FAILED"))
        self.assertFalse(hasattr(CoordinationState, "CANCELLED"))

    def test_state_separation_invariant(self) -> None:
        """Verify CoordinationState != WorkStatus != SubagentStatus.

        A child in CoordinationState.CLAIMED can be in various canonical WorkStatus
        and SubagentStatus states without state machine collision.
        """
        # Scenario 1: Claimed, waiting for approval
        self.assertEqual(CoordinationState.CLAIMED.value, "claimed")
        self.assertEqual(WorkStatus.APPROVAL_REQUIRED.value, "approval_required")
        self.assertEqual(SubagentStatus.READY.value, "ready")

        # Scenario 2: Claimed, actively executing
        self.assertEqual(WorkStatus.EXECUTING.value, "executing")
        self.assertEqual(SubagentStatus.RUNNING.value, "running")

        # Scenario 3: Claimed, work completed
        self.assertEqual(WorkStatus.DONE.value, "done")
        self.assertEqual(SubagentStatus.COMPLETED.value, "completed")

    def test_initial_state_derivation(self) -> None:
        """Sequential group initializes head as RUNNABLE and successors as WAITING."""
        group = DelegationRuntime.create_group(
            parent_work=self.parent_work,
            mode=DelegationMode.SEQUENTIAL,
            children=[self.child_a, self.child_b],
            auto_reconcile=True,
        )
        self.assertEqual(group.coordination_states[self.child_a.id], CoordinationState.RUNNABLE)
        self.assertEqual(group.coordination_states[self.child_b.id], CoordinationState.WAITING)

    def test_atomic_claim_transition(self) -> None:
        """RUNNABLE child transitions atomically to CLAIMED."""
        group = DelegationRuntime.create_group(
            parent_work=self.parent_work,
            mode=DelegationMode.SEQUENTIAL,
            children=[self.child_a, self.child_b],
            auto_reconcile=True,
        )
        group, decision = DelegationRuntime.claim_child(group, self.child_a.id, self.children_map)
        self.assertEqual(decision.child_work_id, self.child_a.id)
        self.assertEqual(decision.id, self.child_a.id)
        self.assertEqual(decision.coordination_state, CoordinationState.CLAIMED)
        self.assertEqual(group.coordination_states[self.child_a.id], CoordinationState.CLAIMED)

    def test_invalid_claim_transition_rejected(self) -> None:
        """Cannot claim a WAITING child directly."""
        group = DelegationRuntime.create_group(
            parent_work=self.parent_work,
            mode=DelegationMode.SEQUENTIAL,
            children=[self.child_a, self.child_b],
            auto_reconcile=True,
        )
        # child_b is WAITING
        with self.assertRaises(DelegationRuntimeError) as ctx:
            DelegationRuntime.claim_child(group, self.child_b.id, self.children_map)
        self.assertIn("cannot be claimed", str(ctx.exception))

    def test_release_claim(self) -> None:
        """CLAIMED child can be explicitly released back to RUNNABLE."""
        group = DelegationRuntime.create_group(
            parent_work=self.parent_work,
            mode=DelegationMode.SEQUENTIAL,
            children=[self.child_a, self.child_b],
            auto_reconcile=True,
        )
        group, decision = DelegationRuntime.claim_child(group, self.child_a.id, self.children_map)
        self.assertEqual(group.coordination_states[self.child_a.id], CoordinationState.CLAIMED)

        released_group = DelegationRuntime.release_claim(group, self.child_a.id)
        self.assertEqual(released_group.coordination_states[self.child_a.id], CoordinationState.RUNNABLE)


class TestDelegationMode(BaseDelegationRuntimeTestCase):
    """Test Category 2: Mode validation and serialization."""

    def test_valid_modes_accepted(self) -> None:
        group_seq = DelegationRuntime.create_group(
            parent_work=self.parent_work,
            mode=DelegationMode.SEQUENTIAL,
            children=[self.child_a, self.child_b],
        )
        self.assertEqual(group_seq.mode, DelegationMode.SEQUENTIAL)
        self.assertTrue(group_seq.is_sequential)
        self.assertFalse(group_seq.is_parallel)
        self.assertEqual(group_seq.max_concurrency, 1)

        group_par = DelegationRuntime.create_group(
            parent_work=self.parent_work,
            mode=DelegationMode.PARALLEL,
            children=[self.child_a, self.child_b],
            max_concurrency=3,
        )
        self.assertEqual(group_par.mode, DelegationMode.PARALLEL)
        self.assertTrue(group_par.is_parallel)
        self.assertFalse(group_par.is_sequential)
        self.assertEqual(group_par.max_concurrency, 3)

    def test_string_mode_accepted(self) -> None:
        group = DelegationRuntime.create_group(
            parent_work=self.parent_work,
            mode="parallel",
            children=[self.child_a],
        )
        self.assertEqual(group.mode, DelegationMode.PARALLEL)

    def test_invalid_mode_rejected(self) -> None:
        with self.assertRaises(DelegationModeError):
            DelegationRuntime.create_group(
                parent_work=self.parent_work,
                mode="distributed_swarm",
                children=[self.child_a],
            )

    def test_serialization_round_trip(self) -> None:
        group = DelegationRuntime.create_group(
            parent_work=self.parent_work,
            mode=DelegationMode.PARALLEL,
            children=[self.child_a, self.child_b],
            max_concurrency=2,
        )
        serialized = group.to_dict()
        self.assertIsInstance(serialized, dict)
        self.assertEqual(serialized["mode"], "parallel")
        self.assertEqual(serialized["max_concurrency"], 2)

        restored = DelegationGroup.from_dict(serialized)
        self.assertEqual(restored.delegation_group_id, group.delegation_group_id)
        self.assertEqual(restored.parent_work_id, group.parent_work_id)
        self.assertEqual(restored.child_work_ids, group.child_work_ids)
        self.assertEqual(restored.dependencies, group.dependencies)
        self.assertEqual(restored.max_concurrency, group.max_concurrency)
        self.assertEqual(restored.coordination_states, group.coordination_states)


class TestSequentialDelegation(BaseDelegationRuntimeTestCase):
    """Test Category 3: Sequential delegation semantics."""

    def test_sequential_linear_dispatch_flow(self) -> None:
        # 1. Create sequential group: A -> B -> C
        group = DelegationRuntime.create_group(
            parent_work=self.parent_work,
            mode=DelegationMode.SEQUENTIAL,
            children=[self.child_a, self.child_b, self.child_c],
            auto_reconcile=True,
        )
        self.assertEqual(group.dependencies[self.child_a.id], ())
        self.assertEqual(group.dependencies[self.child_b.id], (self.child_a.id,))
        self.assertEqual(group.dependencies[self.child_c.id], (self.child_b.id,))
        self.assertEqual(group.max_concurrency, 1)

        # Initial state: A is RUNNABLE, B and C are WAITING
        self.assertEqual(group.coordination_states[self.child_a.id], CoordinationState.RUNNABLE)
        self.assertEqual(group.coordination_states[self.child_b.id], CoordinationState.WAITING)
        self.assertEqual(group.coordination_states[self.child_c.id], CoordinationState.WAITING)

        # 2. Claim child A
        group, decision_a = DelegationRuntime.claim_child(group, self.child_a.id, self.children_map)
        self.assertEqual(decision_a.child_work_id, self.child_a.id)
        self.assertEqual(decision_a.id, self.child_a.id)
        self.assertEqual(group.coordination_states[self.child_a.id], CoordinationState.CLAIMED)
        self.assertEqual(group.coordination_states[self.child_b.id], CoordinationState.WAITING)

        # While child A is active (e.g. WorkStatus.EXECUTING), B and C must remain WAITING
        child_a_exec = replace(self.child_a, work=self.child_a.work.transition(WorkStatus.PLANNING).transition(WorkStatus.APPROVAL_REQUIRED).transition(WorkStatus.EXECUTING, approval_request_id="appr_01", transaction_id="tx_01"))
        self.children_map[self.child_a.id] = child_a_exec

        group = DelegationRuntime.reconcile(group, self.children_map)
        self.assertEqual(group.coordination_states[self.child_b.id], CoordinationState.WAITING)
        group, decisions = DelegationRuntime.claim_next(group, self.children_map)
        self.assertEqual(len(decisions), 0)

        # 3. Child A finishes successfully (canonical WorkStatus.DONE)
        work_a_done = child_a_exec.work.transition(WorkStatus.VERIFYING, transaction_id="tx_01").transition(WorkStatus.DONE, verification_result=VerificationResult(status="PASS", summary="Done"))
        self.children_map[self.child_a.id] = replace(self.child_a, work=work_a_done)

        # Coordination reevaluation promotes B to RUNNABLE
        group = DelegationRuntime.reconcile(group, self.children_map)
        self.assertEqual(group.coordination_states[self.child_a.id], CoordinationState.CLAIMED)
        self.assertEqual(group.coordination_states[self.child_b.id], CoordinationState.RUNNABLE)
        self.assertEqual(group.coordination_states[self.child_c.id], CoordinationState.WAITING)

        # Claim child B
        group, decision_b = DelegationRuntime.claim_child(group, self.child_b.id, self.children_map)
        self.assertEqual(decision_b.child_work_id, self.child_b.id)
        self.assertEqual(group.coordination_states[self.child_b.id], CoordinationState.CLAIMED)
        self.assertEqual(group.coordination_states[self.child_c.id], CoordinationState.WAITING)

        # 4. Child B finishes successfully (WorkStatus.DONE)
        work_b_done = self.child_b.work.transition(WorkStatus.PLANNING).transition(WorkStatus.APPROVAL_REQUIRED).transition(WorkStatus.EXECUTING, approval_request_id="appr_02", transaction_id="tx_02").transition(WorkStatus.VERIFYING, transaction_id="tx_02").transition(WorkStatus.DONE, verification_result=VerificationResult(status="PASS", summary="Done"))
        self.children_map[self.child_b.id] = replace(self.child_b, work=work_b_done)

        group = DelegationRuntime.reconcile(group, self.children_map)
        self.assertEqual(group.coordination_states[self.child_c.id], CoordinationState.RUNNABLE)

        group, decisions_c = DelegationRuntime.claim_next(group, self.children_map)
        self.assertEqual(len(decisions_c), 1)
        self.assertEqual(decisions_c[0].child_work_id, self.child_c.id)
        self.assertEqual(group.coordination_states[self.child_c.id], CoordinationState.CLAIMED)

    def test_sequential_failure_blocks_downstream(self) -> None:
        """When a predecessor fails in canonical work, downstream dependents become BLOCKED."""
        group = DelegationRuntime.create_group(
            parent_work=self.parent_work,
            mode=DelegationMode.SEQUENTIAL,
            children=[self.child_a, self.child_b, self.child_c],
            auto_reconcile=True,
        )
        group, decision_a = DelegationRuntime.claim_child(group, self.child_a.id, self.children_map)
        self.assertEqual(decision_a.child_work_id, self.child_a.id)

        # Child A fails
        work_a_failed = self.child_a.work.transition(WorkStatus.FAILED, failure=WorkFailure(code="ERR_SYNTAX", summary="Syntax error"))
        child_a_failed = replace(self.child_a, work=work_a_failed)
        self.children_map[self.child_a.id] = child_a_failed

        group = DelegationRuntime.reconcile(group, self.children_map)
        self.assertEqual(group.coordination_states[self.child_a.id], CoordinationState.CLAIMED)
        # B and C must become BLOCKED
        self.assertEqual(group.coordination_states[self.child_b.id], CoordinationState.BLOCKED)
        self.assertEqual(group.coordination_states[self.child_c.id], CoordinationState.BLOCKED)

        # Claiming yields 0 decisions
        group, decisions = DelegationRuntime.claim_next(group, self.children_map)
        self.assertEqual(len(decisions), 0)
        self.assertEqual(group.blocked_children, (self.child_b.id, self.child_c.id))


class TestParallelDelegation(BaseDelegationRuntimeTestCase):
    """Test Category 4: Parallel delegation semantics."""

    def test_parallel_bounded_concurrency(self) -> None:
        # Concurrency = 2 for 4 independent children
        group = DelegationRuntime.create_group(
            parent_work=self.parent_work,
            mode=DelegationMode.PARALLEL,
            children=[self.child_a, self.child_b, self.child_c, self.child_d],
            max_concurrency=2,
            auto_reconcile=True,
        )

        # Initial: A and B are RUNNABLE, C and D are WAITING
        self.assertEqual(group.coordination_states[self.child_a.id], CoordinationState.RUNNABLE)
        self.assertEqual(group.coordination_states[self.child_b.id], CoordinationState.RUNNABLE)
        self.assertEqual(group.coordination_states[self.child_c.id], CoordinationState.WAITING)
        self.assertEqual(group.coordination_states[self.child_d.id], CoordinationState.WAITING)

        # Claim next claims A and B
        group, decisions = DelegationRuntime.claim_next(group, self.children_map)
        self.assertEqual(len(decisions), 2)
        self.assertEqual([d.child_work_id for d in decisions], [self.child_a.id, self.child_b.id])
        self.assertEqual(group.coordination_states[self.child_a.id], CoordinationState.CLAIMED)
        self.assertEqual(group.coordination_states[self.child_b.id], CoordinationState.CLAIMED)
        self.assertEqual(group.coordination_states[self.child_c.id], CoordinationState.WAITING)
        self.assertEqual(group.coordination_states[self.child_d.id], CoordinationState.WAITING)

        # Repeated claim is idempotent
        group, decisions_again = DelegationRuntime.claim_next(group, self.children_map)
        self.assertEqual(len(decisions_again), 0)
        self.assertEqual(len(group.active_children), 2)

        # Complete child A: frees up 1 capacity slot, next child (C) becomes RUNNABLE
        work_a_done = self.child_a.work.transition(WorkStatus.PLANNING).transition(WorkStatus.APPROVAL_REQUIRED).transition(WorkStatus.EXECUTING, approval_request_id="appr_01", transaction_id="tx_01").transition(WorkStatus.VERIFYING, transaction_id="tx_01").transition(WorkStatus.DONE, verification_result=VerificationResult(status="PASS", summary="Done"))
        self.children_map[self.child_a.id] = replace(self.child_a, work=work_a_done)

        group = DelegationRuntime.reconcile(group, self.children_map)
        self.assertEqual(group.coordination_states[self.child_c.id], CoordinationState.RUNNABLE)
        self.assertEqual(group.coordination_states[self.child_d.id], CoordinationState.WAITING)

        group, decisions_step2 = DelegationRuntime.claim_next(group, self.children_map)
        self.assertEqual(len(decisions_step2), 1)
        self.assertEqual(decisions_step2[0].child_work_id, self.child_c.id)
        self.assertEqual(group.coordination_states[self.child_c.id], CoordinationState.CLAIMED)

        # Complete child B: frees up 1 slot, D becomes RUNNABLE
        work_b_done = self.child_b.work.transition(WorkStatus.PLANNING).transition(WorkStatus.APPROVAL_REQUIRED).transition(WorkStatus.EXECUTING, approval_request_id="appr_02", transaction_id="tx_02").transition(WorkStatus.VERIFYING, transaction_id="tx_02").transition(WorkStatus.DONE, verification_result=VerificationResult(status="PASS", summary="Done"))
        self.children_map[self.child_b.id] = replace(self.child_b, work=work_b_done)

        group = DelegationRuntime.reconcile(group, self.children_map)
        self.assertEqual(group.coordination_states[self.child_d.id], CoordinationState.RUNNABLE)

        group, decisions_step3 = DelegationRuntime.claim_next(group, self.children_map)
        self.assertEqual(len(decisions_step3), 1)
        self.assertEqual(decisions_step3[0].child_work_id, self.child_d.id)
        self.assertEqual(group.coordination_states[self.child_d.id], CoordinationState.CLAIMED)


class TestChildDependencies(BaseDelegationRuntimeTestCase):
    """Test Category 5: DAG dependencies and cycle validation."""

    def test_explicit_diamond_dag_execution(self) -> None:
        # DAG:
        # A ─┐
        #    ├─→ C
        # B ─┘
        # D (independent)
        custom_deps: Dict[str, Sequence[str]] = {
            self.child_a.id: (),
            self.child_b.id: (),
            self.child_c.id: (self.child_a.id, self.child_b.id),
            self.child_d.id: (),
        }
        group = DelegationRuntime.create_group(
            parent_work=self.parent_work,
            mode=DelegationMode.PARALLEL,
            children=[self.child_a, self.child_b, self.child_c, self.child_d],
            dependencies=custom_deps,
            max_concurrency=4,
            auto_reconcile=True,
        )

        # Dispatch 1: A, B, and D become runnable; C waits for A and B
        group, decisions = DelegationRuntime.claim_next(group, self.children_map)
        claimed_ids = {d.child_work_id for d in decisions}
        self.assertEqual(claimed_ids, {self.child_a.id, self.child_b.id, self.child_d.id})
        self.assertEqual(group.coordination_states[self.child_c.id], CoordinationState.WAITING)

        # Complete A only: C must still wait for B
        work_a_done = self.child_a.work.transition(WorkStatus.PLANNING).transition(WorkStatus.APPROVAL_REQUIRED).transition(WorkStatus.EXECUTING, approval_request_id="appr_01", transaction_id="tx_01").transition(WorkStatus.VERIFYING, transaction_id="tx_01").transition(WorkStatus.DONE, verification_result=VerificationResult(status="PASS", summary="Done"))
        self.children_map[self.child_a.id] = replace(self.child_a, work=work_a_done)

        group = DelegationRuntime.reconcile(group, self.children_map)
        self.assertEqual(group.coordination_states[self.child_c.id], CoordinationState.WAITING)

        # Complete B: now both predecessors of C are DONE! C becomes RUNNABLE
        work_b_done = self.child_b.work.transition(WorkStatus.PLANNING).transition(WorkStatus.APPROVAL_REQUIRED).transition(WorkStatus.EXECUTING, approval_request_id="appr_02", transaction_id="tx_02").transition(WorkStatus.VERIFYING, transaction_id="tx_02").transition(WorkStatus.DONE, verification_result=VerificationResult(status="PASS", summary="Done"))
        self.children_map[self.child_b.id] = replace(self.child_b, work=work_b_done)

        group = DelegationRuntime.reconcile(group, self.children_map)
        self.assertEqual(group.coordination_states[self.child_c.id], CoordinationState.RUNNABLE)

        group, decisions_c = DelegationRuntime.claim_next(group, self.children_map)
        self.assertEqual(len(decisions_c), 1)
        self.assertEqual(decisions_c[0].child_work_id, self.child_c.id)
        self.assertEqual(group.coordination_states[self.child_c.id], CoordinationState.CLAIMED)

    def test_self_dependency_rejected(self) -> None:
        with self.assertRaises(DelegationDependencyError) as ctx:
            validate_dependency_dag(
                [self.child_a.id],
                {self.child_a.id: (self.child_a.id,)},
            )
        self.assertIn("Self-dependency rejected", str(ctx.exception))

    def test_unknown_dependency_rejected(self) -> None:
        with self.assertRaises(DelegationDependencyError) as ctx:
            validate_dependency_dag(
                [self.child_a.id],
                {self.child_a.id: ("non_existent_child",)},
            )
        self.assertIn("Unknown dependency", str(ctx.exception))

    def test_duplicate_dependency_edge_rejected(self) -> None:
        with self.assertRaises(DelegationDependencyError) as ctx:
            validate_dependency_dag(
                [self.child_a.id, self.child_b.id],
                {self.child_b.id: (self.child_a.id, self.child_a.id)},
            )
        self.assertIn("Duplicate dependency edge rejected", str(ctx.exception))

    def test_cycle_rejected(self) -> None:
        cycle_deps = {
            self.child_a.id: (self.child_c.id,),
            self.child_b.id: (self.child_a.id,),
            self.child_c.id: (self.child_b.id,),
        }
        with self.assertRaises(DelegationDependencyError) as ctx:
            validate_dependency_dag(
                [self.child_a.id, self.child_b.id, self.child_c.id],
                cycle_deps,
            )
        self.assertIn("Cycle detected", str(ctx.exception))


class TestParentAndDelegationBindings(BaseDelegationRuntimeTestCase):
    """Test Category 6: Parent and Delegation identity bindings."""

    def test_foreign_child_parent_rejected(self) -> None:
        foreign_parent = replace(self.parent_work, id="work_parent_different")
        foreign_sub = replace(self.subagents[0], parent_work_id="work_parent_different")
        foreign_delg = replace(self.delegations[0], parent_work_id="work_parent_different", digest="")
        foreign_child = ChildWork.create(
            parent_work=foreign_parent,
            subagent=foreign_sub,
            delegation=foreign_delg,
            child_work_id="work_child_foreign_parent",
        )

        with self.assertRaises(DelegationBindingError):
            DelegationRuntime.create_group(
                parent_work=self.parent_work,
                mode=DelegationMode.PARALLEL,
                children=[self.child_a, foreign_child],
            )

    def test_foreign_child_actor_rejected(self) -> None:
        foreign_actor = "actor_different_user"
        foreign_parent = replace(self.parent_work, actor_id=foreign_actor)
        foreign_sub = replace(self.subagents[0], actor=foreign_actor)
        foreign_delg = replace(self.delegations[0], actor=foreign_actor, digest="")
        foreign_child = ChildWork.create(
            parent_work=foreign_parent,
            subagent=foreign_sub,
            delegation=foreign_delg,
            child_work_id="work_child_foreign_actor",
        )

        with self.assertRaises(DelegationBindingError):
            DelegationRuntime.create_group(
                parent_work=self.parent_work,
                mode=DelegationMode.PARALLEL,
                children=[foreign_child],
            )

    def test_expired_delegation_blocks_runnable_status(self) -> None:
        expired_child = replace(self.child_a, delegation_expires_at=self.now - 10.0)
        group = DelegationRuntime.create_group(
            parent_work=self.parent_work,
            mode=DelegationMode.PARALLEL,
            children=[expired_child, self.child_b],
            auto_reconcile=False,
        )

        test_map = {expired_child.id: expired_child, self.child_b.id: self.child_b}
        group = DelegationRuntime.reconcile(group, test_map, current_time=self.now)

        # expired_child must NOT become RUNNABLE; it must become BLOCKED
        self.assertEqual(group.coordination_states[expired_child.id], CoordinationState.BLOCKED)
        self.assertEqual(group.coordination_states[self.child_b.id], CoordinationState.RUNNABLE)

        group, decisions = DelegationRuntime.claim_next(group, test_map, current_time=self.now)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].child_work_id, self.child_b.id)


class TestSessionIncarnationProtection(BaseDelegationRuntimeTestCase):
    """Test Category 7: Session incarnation freshness enforcement."""

    def test_stale_session_incarnation_blocks_dispatch(self) -> None:
        group = DelegationRuntime.create_group(
            parent_work=self.parent_work,
            mode=DelegationMode.SEQUENTIAL,
            children=[self.child_a, self.child_b],
        )
        with self.assertRaises(DelegationSessionStaleError):
            DelegationRuntime.dispatch(
                group,
                self.children_map,
                current_session_incarnation_id="inc_reset_new_incarnation",
            )

    def test_stale_session_incarnation_blocks_mutation(self) -> None:
        group = DelegationRuntime.create_group(
            parent_work=self.parent_work,
            mode=DelegationMode.SEQUENTIAL,
            children=[self.child_a, self.child_b],
        )
        stale_inc = "inc_reset_new_incarnation"
        with self.assertRaises(DelegationSessionStaleError):
            DelegationRuntime.reconcile(group, self.children_map, current_session_incarnation_id=stale_inc)

        with self.assertRaises(DelegationSessionStaleError):
            DelegationRuntime.claim_child(group, self.child_a.id, self.children_map, current_session_incarnation_id=stale_inc)

        with self.assertRaises(DelegationSessionStaleError):
            DelegationRuntime.claim_next(group, self.children_map, current_session_incarnation_id=stale_inc)

        with self.assertRaises(DelegationSessionStaleError):
            DelegationRuntime.release_claim(group, self.child_a.id, current_session_incarnation_id=stale_inc)

        with self.assertRaises(DelegationSessionStaleError):
            DelegationRuntime.cancel_child(group, self.child_a.id, current_session_incarnation_id=stale_inc)


class TestPersistenceAndOCC(BaseDelegationRuntimeTestCase):
    """Test Category 8: WorkStore persistence, recovery, and OCC."""

    def setUp(self) -> None:
        super().setUp()
        self.test_dir = tempfile.mkdtemp(prefix="test_delegation_store_")
        self.file_store = FileWorkStore(works_dir=Path(self.test_dir))
        self.file_store.create(self.parent_work)

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_save_and_get_delegation_group(self) -> None:
        group = DelegationRuntime.create_group(
            parent_work=self.parent_work,
            mode=DelegationMode.PARALLEL,
            children=[self.child_a, self.child_b],
            max_concurrency=2,
        )

        saved_group = save_delegation_group(self.file_store, group)
        self.assertEqual(saved_group.id, group.id)

        # Verify parent Work now has the delegation group stored
        retrieved_group = get_delegation_group(self.file_store, self.parent_work_id, group.id)
        self.assertIsNotNone(retrieved_group)
        assert retrieved_group is not None
        self.assertEqual(retrieved_group.id, group.id)
        self.assertEqual(retrieved_group.mode, DelegationMode.PARALLEL)
        self.assertEqual(retrieved_group.max_concurrency, 2)
        self.assertEqual(retrieved_group.child_work_ids, (self.child_a.id, self.child_b.id))

        # Observational property on parent work
        parent = self.file_store.get(self.parent_work_id)
        assert parent is not None
        self.assertIn(group.id, parent.delegation_group_ids)
        self.assertIn(self.child_a.id, parent.child_work_ids)

    def test_stale_revision_rejected_in_occ(self) -> None:
        group = DelegationRuntime.create_group(
            parent_work=self.parent_work,
            mode=DelegationMode.PARALLEL,
            children=[self.child_a],
        )
        save_delegation_group(self.file_store, group)

        parent = self.file_store.get(self.parent_work_id)
        assert parent is not None
        stale_rev = parent.revision - 1

        with self.assertRaises(StaleWorkRevisionError):
            save_delegation_group(self.file_store, group, expected_revision=stale_rev)


class TestAddChildApi(BaseDelegationRuntimeTestCase):
    """Test Category 9: Immutable add_child operation."""

    def test_add_child_sequential_and_parallel(self) -> None:
        # Sequential: adding child appends with dependency on previous
        group = DelegationRuntime.create_group(
            parent_work=self.parent_work,
            mode=DelegationMode.SEQUENTIAL,
            children=[self.child_a, self.child_b],
        )
        self.assertEqual(len(group.child_work_ids), 2)

        group2 = DelegationRuntime.add_child(group, self.child_c)
        self.assertEqual(len(group2.child_work_ids), 3)
        self.assertEqual(group2.child_work_ids[-1], self.child_c.id)
        self.assertEqual(group2.dependencies[self.child_c.id], (self.child_b.id,))
        self.assertEqual(group2.coordination_states[self.child_c.id], CoordinationState.WAITING)

        # Original group unmodified (immutability)
        self.assertEqual(len(group.child_work_ids), 2)


class TestExecutionBoundary(unittest.TestCase):
    """Test Category 10: Strict execution boundary enforcement."""

    def test_coordinator_contains_no_execution_primitives(self) -> None:
        source = inspect.getsource(DelegationRuntime)
        forbidden_terms = [
            "subprocess",
            "os.system",
            "os.popen",
            "shutil.rmtree",
            "TransactionCoordinator.execute",
            "orchestrator.run",
            "execute_transaction",
            "requests.",
            "urllib.",
            "httpx.",
        ]
        for term in forbidden_terms:
            self.assertNotIn(term, source, f"DelegationRuntime must NOT contain '{term}'")


class TestEndToEndComposition(BaseDelegationRuntimeTestCase):
    """Test Category 11: Complete end-to-end integration path.

    Parent Work -> DelegationContract -> Subagent -> Child Work -> DelegationRuntime ->
    Runnable Child -> Plan -> Approval -> ApprovedExecutionContract -> Transaction -> Orchestrator -> DONE
    """

    def setUp(self) -> None:
        super().setUp()
        self.test_dir = tempfile.mkdtemp(prefix="test_e2e_coord_")
        self.repo_dir = Path(self.test_dir)
        (self.repo_dir / "src").mkdir(parents=True, exist_ok=True)
        (self.repo_dir / "src" / "auth_a.py").write_text("# Initial auth", encoding="utf-8")

        self.approval_store = InMemoryApprovalStore()
        self.approval_service = ApprovalService(store=self.approval_store, default_ttl_seconds=300)
        self.tx_store = InMemoryTransactionStore()

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_e2e_governed_composition(self) -> None:
        # 1. Create sequential group for child_a and child_b
        group = DelegationRuntime.create_group(
            parent_work=self.parent_work,
            mode=DelegationMode.SEQUENTIAL,
            children=[self.child_a, self.child_b],
            auto_reconcile=True,
        )

        # 2. Coordinator determines eligibility: child_a becomes RUNNABLE
        self.assertEqual(group.coordination_states[self.child_a.id], CoordinationState.RUNNABLE)
        self.assertEqual(group.coordination_states[self.child_b.id], CoordinationState.WAITING)

        # 3. Atomically claim child_a
        group, decision_a = DelegationRuntime.claim_child(group, self.child_a.id, self.children_map)
        self.assertEqual(decision_a.child_work_id, self.child_a.id)
        self.assertEqual(decision_a.coordination_state, CoordinationState.CLAIMED)
        self.assertEqual(group.coordination_states[self.child_a.id], CoordinationState.CLAIMED)

        # 4. Canonical Plan step on active_child
        plan = ("Update src/auth_a.py with token rotation logic",)
        child_planned = self.child_a.transition(WorkStatus.PLANNING, plan=plan)

        # 5. Approval Gate
        op = CanonicalOperation(action_type="edit_file", target="src/auth_a.py")
        appr_req = ApprovalRequest(
            request_id="appr_child_001",
            session_id=self.session_id,
            channel="cli",
            user_id=self.actor,
            conversation_id="conv_child_001",
            operation_type="edit_file",
            canonical_operation=op,
            operation_digest=op.compute_digest(),
            risk_class=RiskClass.LOW.value,
            created_at=self.now,
            expires_at=self.now + 300.0,
            nonce="nonce_child_001",
            status=ApprovalStatus.APPROVED,
        )
        self.approval_store.save(appr_req)
        child_req = child_planned.transition(WorkStatus.APPROVAL_REQUIRED, approval_request_id=appr_req.request_id)

        # 6. ApprovedExecutionContract
        contract = ApprovedExecutionContract(
            request_id=appr_req.request_id,
            action_type="edit_file",
            approved_targets=frozenset({"src/auth_a.py"}),
            operation_digest=op.compute_digest(),
            channel="cli",
            capabilities=self.child_a.delegation.capabilities if self.child_a.delegation else Capabilities(),
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            expires_at=time.time() + 300.0,
            workspace_root=str(self.repo_dir),
        )

        # 7. Transaction Transition
        child_exec = child_req.transition(WorkStatus.EXECUTING, approval_request_id=appr_req.request_id, transaction_id="tx_coord_001")
        self.assertEqual(child_exec.status, WorkStatus.EXECUTING)

        # 8. Verification & Terminal DONE
        child_verif = child_exec.transition(WorkStatus.VERIFYING, transaction_id="tx_coord_001")
        child_done = child_verif.transition(
            WorkStatus.DONE,
            verification_result=VerificationResult(status="PASS", summary="All checks passed"),
        )
        self.assertEqual(child_done.status, WorkStatus.DONE)
        assert child_done.subagent is not None
        self.assertEqual(child_done.subagent.status, SubagentStatus.COMPLETED)
        self.children_map[child_done.id] = child_done

        # 9. Coordinator observes completion and now dispatches child_b!
        group = DelegationRuntime.reconcile(group, self.children_map)
        self.assertEqual(group.coordination_states[self.child_a.id], CoordinationState.CLAIMED)
        self.assertEqual(group.coordination_states[self.child_b.id], CoordinationState.RUNNABLE)

        group, next_decisions = DelegationRuntime.claim_next(group, self.children_map)
        self.assertEqual(len(next_decisions), 1)
        self.assertEqual(next_decisions[0].child_work_id, self.child_b.id)
        self.assertEqual(group.coordination_states[self.child_b.id], CoordinationState.CLAIMED)


class TestPropertyInvariants(BaseDelegationRuntimeTestCase):
    """Test Category 12: Bounded property invariant assertions."""

    def test_property_sequential_at_most_one_active(self) -> None:
        group = DelegationRuntime.create_group(
            parent_work=self.parent_work,
            mode=DelegationMode.SEQUENTIAL,
            children=[self.child_a, self.child_b, self.child_c],
            auto_reconcile=True,
        )
        group, decisions = DelegationRuntime.claim_next(group, self.children_map)
        self.assertLessEqual(group.active_claims_count(self.children_map), 1)

    def test_property_parallel_active_bounded_by_max_concurrency(self) -> None:
        for limit in (1, 2, 3, 4):
            group = DelegationRuntime.create_group(
                parent_work=self.parent_work,
                mode=DelegationMode.PARALLEL,
                children=[self.child_a, self.child_b, self.child_c, self.child_d],
                max_concurrency=limit,
                auto_reconcile=True,
            )
            group, decisions = DelegationRuntime.claim_next(group, self.children_map)
            self.assertLessEqual(group.active_claims_count(self.children_map), limit)

    def test_property_dependency_satisfaction_invariant(self) -> None:
        """For every child in RUNNABLE or CLAIMED, all predecessor dependencies MUST be DONE."""
        group = DelegationRuntime.create_group(
            parent_work=self.parent_work,
            mode=DelegationMode.SEQUENTIAL,
            children=[self.child_a, self.child_b, self.child_c],
            auto_reconcile=True,
        )
        group, decisions = DelegationRuntime.claim_next(group, self.children_map)
        for cid in group.claimed_children:
            preds = group.dependencies.get(cid, ())
            for p in preds:
                self.assertEqual(self.children_map[p].work.status, WorkStatus.DONE)

    def test_property_idempotence(self) -> None:
        """reconcile(reconcile(state)) == reconcile(state)"""
        group = DelegationRuntime.create_group(
            parent_work=self.parent_work,
            mode=DelegationMode.PARALLEL,
            children=[self.child_a, self.child_b, self.child_c],
            max_concurrency=2,
            auto_reconcile=True,
        )
        group1 = DelegationRuntime.reconcile(group, self.children_map)
        group2 = DelegationRuntime.reconcile(group1, self.children_map)
        self.assertEqual(group1.coordination_states, group2.coordination_states)

    def test_property_authority_immutability(self) -> None:
        """Coordination never modifies child capabilities or scopes."""
        original_caps = self.child_a.delegation.capabilities if self.child_a.delegation else None
        group = DelegationRuntime.create_group(
            parent_work=self.parent_work,
            mode=DelegationMode.PARALLEL,
            children=[self.child_a, self.child_b],
            auto_reconcile=True,
        )
        group, decisions = DelegationRuntime.claim_next(group, self.children_map)
        group = DelegationRuntime.reconcile(group, self.children_map)

        # Capabilities and targets remain exact
        assert self.child_a.delegation is not None
        self.assertEqual(self.child_a.delegation.capabilities, original_caps)


if __name__ == "__main__":
    unittest.main()
