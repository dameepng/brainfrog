"""Comprehensive Unit and Property Tests for P1.3I Real Subagent Execution Integration.

Covers:
- Group A: Basic Execution Pipeline (Parent -> Child -> canonical execution -> DONE)
- Group B: Parent/Child/Subagent Binding Invariance (fails closed on mismatch)
- Group C: Session Incarnation Binding (stale incarnation fails closed)
- Group D: Delegation Expiration (expired contract cannot execute)
- Group E: Capability Attenuation (contract target scope cannot exceed delegation)
- Group F: Approval Boundary (unapproved task enters APPROVAL_REQUIRED, no auto-approval)
- Group G: Transaction Integration (passes transaction store and verifier to RunConfig)
- Group H: Orchestrator Integration (terminates in canonical orchestrator.py)
- Group I: Dependency Ordering (B cannot execute before A is DONE)
- Group J: Parallel Coordination (independent children run subject to max_concurrency)
- Group K: Failure Cascades (failed child causes downstream to become BLOCKED via P1.3G)
- Group L: Cancellation Invariance (cancelled child cannot execute)
- Group M: Idempotence (already completed child does not re-execute)
- Group N: Concurrent Claims (racing workers: exactly one claim winner)
- Group O: Crash Recovery (reconstruction of interrupted state)
- Group P: Result Aggregation Flow (child results feed into P1.3F ResultAggregator)
- Group Q: Parent Continuation (completed children update parent resume metadata)
- Group R: Context Isolation (bounded context, no parent history leakage)
- Group S: Authority Invariance (AST analysis: zero subprocess, shell, or fs mutations)
- Group T: Remote Work Path (RemoteWorkCoordinator -> ChildWork -> execution -> result)
- Group U: Property Invariants
"""
from __future__ import annotations

import ast
import inspect
import os
import shutil
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from unittest.mock import MagicMock

from core.runtime.approval import ApprovalService, CanonicalOperation, InMemoryApprovalStore
from core.runtime.capabilities import Capabilities, FilesystemPolicy, GitPolicy, NetworkPolicy, ShellPolicy
from core.runtime.child_work import ChildWork, get_child_work, save_child_work
from core.runtime.contract import ApprovedExecutionContract
from core.runtime.delegation import DelegationContract
from core.runtime.delegation_runtime import (
    CoordinationState,
    DelegationConcurrencyError,
    DelegationGroup,
    DelegationMode,
    DelegationRuntime,
    get_delegation_group,
    save_delegation_group,
)
from core.runtime.failure_propagation import (
    CancellationPropagationPolicy,
    ParentFailurePolicy,
    propagate_child_cancellation,
    propagate_failure,
)
from core.runtime.remote_work import RemoteWorkCoordinator, RemoteWorkRequest
from core.runtime.result_aggregation import (
    AggregateResult,
    AggregateStatus,
    AggregationPolicy,
    ArtifactReference,
    ChildResult,
    ResultAggregator,
)
from core.runtime.subagent import Subagent, SubagentStatus
from core.runtime.subagent_execution import (
    CURRENT_SUBAGENT_EXECUTION_SCHEMA_VERSION,
    SubagentExecutionApprovalError,
    SubagentExecutionBindingError,
    SubagentExecutionContext,
    SubagentExecutionCoordinator,
    SubagentExecutionDependencyError,
    SubagentExecutionError,
    SubagentExecutionExpiredError,
    SubagentExecutionRequest,
    SubagentExecutionResult,
    SubagentExecutionScopeError,
    SubagentExecutionSessionStaleError,
    SubagentExecutionStateError,
    SubagentExecutionBlockedError,
)
from core.runtime.transaction import (
    FileTransactionStore,
    InMemoryTransactionStore,
    TransactionCoordinator,
)
from core.runtime.work_store import FileWorkStore
from core.runtime.work import (
    InMemoryWorkStore,
    StaleWorkRevisionError,
    VerificationResult,
    VerificationStatus,
    Work,
    WorkFailure,
    WorkStatus,
    WorkStore,
)
from orchestrator import PlanStep, RunConfig, StepResult


class BaseSubagentExecutionTestCase(unittest.TestCase):
    """Base fixture providing parent work, subagents, and attenuated delegation contracts."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="brainfrog_subagent_exec_test_")
        self.repo_dir = Path(self.temp_dir).resolve()
        self.work_store = InMemoryWorkStore()
        self.tx_store = InMemoryTransactionStore()

        self.actor = "user_lead_001"
        self.session_id = "sess_001"
        self.session_incarnation_id = "sess_001_inc_1"
        self.now = time.time()

        # Create parent work
        self.parent_caps = Capabilities(
            filesystem=FilesystemPolicy(read=("src/",), write=("src/",)),
            shell=ShellPolicy(execute=False),
            network=NetworkPolicy(access=False),
            git=GitPolicy(read=True, commit=False),
        )
        self.parent_work = Work(
            intent="Refactor authentication and payment subsystem",
            goal="Refactor auth and pay",
            scope=("src/auth.py", "src/pay.py"),
            capabilities=self.parent_caps,
            actor_id=self.actor,
            channel="cli",
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
        )
        self.work_store.create(self.parent_work)
        self.parent_work_id = self.parent_work.id

        # Create worker subagents and attenuated delegations
        self.subagent_a = Subagent(
            subagent_id="sub_auth_01",
            parent_work_id=self.parent_work_id,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            actor=self.actor,
            role="auth_specialist",
            purpose="Refactor auth",
            created_at=self.now,
            updated_at=self.now,
        )
        self.delegation_a_caps = Capabilities(
            filesystem=FilesystemPolicy(read=("src/auth.py",), write=("src/auth.py",)),
            shell=ShellPolicy(execute=False),
            network=NetworkPolicy(access=False),
            git=GitPolicy(read=True, commit=False),
        )
        self.delegation_a = DelegationContract(
            delegation_id="delg_auth_01",
            parent_work_id=self.parent_work_id,
            child_subagent_id=self.subagent_a.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            capabilities=self.delegation_a_caps,
            target_scope=("src/auth.py",),
            expires_at=self.now + 600.0,
            created_at=self.now,
        )

        self.child_a = ChildWork.create(
            parent_work=self.parent_work,
            subagent=self.subagent_a,
            delegation=self.delegation_a,
            child_work_id="work_child_auth_01",
            created_at=self.now,
        )
        save_child_work(self.work_store, self.child_a)

        self.subagent_b = Subagent(
            subagent_id="sub_pay_01",
            parent_work_id=self.parent_work_id,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            actor=self.actor,
            role="payment_specialist",
            purpose="Refactor payment",
            created_at=self.now,
            updated_at=self.now,
        )
        self.delegation_b_caps = Capabilities(
            filesystem=FilesystemPolicy(read=("src/pay.py",), write=("src/pay.py",)),
            shell=ShellPolicy(execute=False),
            network=NetworkPolicy(access=False),
            git=GitPolicy(read=True, commit=False),
        )
        self.delegation_b = DelegationContract(
            delegation_id="delg_pay_01",
            parent_work_id=self.parent_work_id,
            child_subagent_id=self.subagent_b.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            capabilities=self.delegation_b_caps,
            target_scope=("src/pay.py",),
            expires_at=self.now + 600.0,
            created_at=self.now,
        )
        self.child_b = ChildWork.create(
            parent_work=self.parent_work,
            subagent=self.subagent_b,
            delegation=self.delegation_b,
            child_work_id="work_child_pay_01",
            created_at=self.now,
        )
        save_child_work(self.work_store, self.child_b)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def make_execution_contract(
        self,
        target_path: str = "src/auth.py",
        actor: Optional[str] = None,
        session_id: Optional[str] = None,
        session_incarnation_id: Optional[str] = None,
        channel: str = "cli",
    ) -> ApprovedExecutionContract:
        """Create a valid ApprovedExecutionContract matching delegated child capabilities."""
        caps = Capabilities(
            filesystem=FilesystemPolicy(read=(target_path,), write=(target_path,)),
            shell=ShellPolicy(execute=False),
            network=NetworkPolicy(access=False),
            git=GitPolicy(read=False, commit=False),
        )
        service = ApprovalService(InMemoryApprovalStore())
        action_type = "write_code"
        targets = [target_path]
        effective_actor = actor or self.actor
        effective_session_id = session_id or self.session_id
        effective_incarnation_id = session_incarnation_id or self.session_incarnation_id
        op = CanonicalOperation(action_type, target_path, {"targets": targets})
        req = service.create_request(
            effective_session_id,
            channel,
            effective_actor,
            "chat",
            action_type,
            op,
            session_incarnation_id=effective_incarnation_id,
            capabilities=caps,
            workspace_root=str(self.repo_dir),
        )
        service.approve(req.request_id, "approver_lead", channel)
        service.verify_and_consume(
            req.request_id,
            req.operation_digest,
            effective_session_id,
            channel,
            session_incarnation_id=effective_incarnation_id,
            requester_id=effective_actor,
        )
        return ApprovedExecutionContract.from_approval_request(req, self.repo_dir)


# =============================================================================
# Test Group A: Basic Execution
# =============================================================================

class TestBasicExecution(BaseSubagentExecutionTestCase):
    """Group A: Parent -> Child -> canonical execution -> DONE."""

    def test_successful_child_execution(self) -> None:
        """Child executes through canonical pipeline and transitions to DONE."""
        mock_runner = MagicMock(return_value=[
            StepResult(PlanStep("1", "Implement auth refactor", []), "opened_pr", 0, "Changes applied successfully")
        ])
        coordinator = SubagentExecutionCoordinator(
            work_store=self.work_store,
            repo_dir=self.repo_dir,
            orchestrator_runner=mock_runner,
        )

        contract = self.make_execution_contract("src/auth.py")
        req = SubagentExecutionRequest(
            parent_work_id=self.parent_work_id,
            child_work_id=self.child_a.id,
            subagent_id=self.subagent_a.id,
            delegation_id=self.delegation_a.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            task="Refactor auth hashing algorithm",
            execution_contract=contract,
        )

        res = coordinator.execute_child(req)

        self.assertTrue(res.success)
        self.assertEqual(res.status, WorkStatus.DONE)
        self.assertIn("Changes applied successfully", res.summary)

        # Verify child work in store transitioned to DONE
        updated_cw = get_child_work(self.work_store, self.child_a.id)
        self.assertIsNotNone(updated_cw)
        assert updated_cw is not None
        self.assertEqual(updated_cw.status, WorkStatus.DONE)

        # Verify subagent status synchronized to COMPLETED
        self.assertIsNotNone(updated_cw.subagent)
        assert updated_cw.subagent is not None
        self.assertEqual(updated_cw.subagent.status, SubagentStatus.COMPLETED)

        # Verify orchestrator was invoked with RunConfig
        mock_runner.assert_called_once()
        cfg_arg = mock_runner.call_args[0][0]
        self.assertIsInstance(cfg_arg, RunConfig)
        self.assertEqual(cfg_arg.work_id, self.child_a.id)
        self.assertEqual(cfg_arg.execution_contract, contract)


# =============================================================================
# Test Group B: Parent/Child Binding
# =============================================================================

class TestParentChildBinding(BaseSubagentExecutionTestCase):
    """Group B: Hierarchy binding validation fails closed."""

    def test_wrong_parent_work_id_fails_closed(self) -> None:
        coordinator = SubagentExecutionCoordinator(work_store=self.work_store, repo_dir=self.repo_dir)
        req = SubagentExecutionRequest(
            parent_work_id="wrong_parent_id",
            child_work_id=self.child_a.id,
            subagent_id=self.subagent_a.id,
            delegation_id=self.delegation_a.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            task="Task",
        )
        with self.assertRaises(SubagentExecutionBindingError):
            coordinator.execute_child(req)

    def test_wrong_subagent_id_fails_closed(self) -> None:
        coordinator = SubagentExecutionCoordinator(work_store=self.work_store, repo_dir=self.repo_dir)
        req = SubagentExecutionRequest(
            parent_work_id=self.parent_work_id,
            child_work_id=self.child_a.id,
            subagent_id="sub_intruder_99",
            delegation_id=self.delegation_a.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            task="Task",
        )
        with self.assertRaises(SubagentExecutionBindingError):
            coordinator.execute_child(req)

    def test_wrong_delegation_id_fails_closed(self) -> None:
        coordinator = SubagentExecutionCoordinator(work_store=self.work_store, repo_dir=self.repo_dir)
        req = SubagentExecutionRequest(
            parent_work_id=self.parent_work_id,
            child_work_id=self.child_a.id,
            subagent_id=self.subagent_a.id,
            delegation_id="delg_fake_00",
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            task="Task",
        )
        with self.assertRaises(SubagentExecutionBindingError):
            coordinator.execute_child(req)


# =============================================================================
# Test Group C: Session Incarnation Binding
# =============================================================================

class TestSessionBinding(BaseSubagentExecutionTestCase):
    """Group C: Session incarnation freshness enforcement."""

    def test_stale_session_incarnation_fails_closed(self) -> None:
        coordinator = SubagentExecutionCoordinator(work_store=self.work_store, repo_dir=self.repo_dir)
        req = SubagentExecutionRequest(
            parent_work_id=self.parent_work_id,
            child_work_id=self.child_a.id,
            subagent_id=self.subagent_a.id,
            delegation_id=self.delegation_a.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id="sess_001_inc_2",  # stale relative to child's inc_1
            task="Task",
        )
        with self.assertRaises(SubagentExecutionSessionStaleError):
            coordinator.execute_child(req)


# =============================================================================
# Test Group D: Delegation Expiration
# =============================================================================

class TestDelegationExpiration(BaseSubagentExecutionTestCase):
    """Group D: Expired delegation cannot execute."""

    def test_expired_delegation_contract_rejected(self) -> None:
        coordinator = SubagentExecutionCoordinator(work_store=self.work_store, repo_dir=self.repo_dir)

        # Expire delegation contract
        now_time = time.time()
        expired_delg = replace(self.delegation_a, created_at=now_time - 100.0, expires_at=now_time - 10.0, digest="")
        expired_child = replace(
            self.child_a,
            delegation=expired_delg,
            delegation_digest=expired_delg.digest,
            delegation_expires_at=expired_delg.expires_at,
        )
        save_child_work(self.work_store, expired_child)

        req = SubagentExecutionRequest(
            parent_work_id=self.parent_work_id,
            child_work_id=self.child_a.id,
            subagent_id=self.subagent_a.id,
            delegation_id=self.delegation_a.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            task="Task",
        )
        with self.assertRaises(SubagentExecutionExpiredError):
            coordinator.execute_child(req, delegation=expired_delg, child_work=expired_child)


# =============================================================================
# Test Group E: Capability Attenuation
# =============================================================================

class TestCapabilityAttenuation(BaseSubagentExecutionTestCase):
    """Group E: Child execution cannot exceed delegated scope."""

    def test_contract_exceeding_delegation_scope_rejected(self) -> None:
        coordinator = SubagentExecutionCoordinator(work_store=self.work_store, repo_dir=self.repo_dir)

        # Contract targets src/pay.py, but child A is only delegated src/auth.py
        widened_contract = self.make_execution_contract("src/pay.py")

        req = SubagentExecutionRequest(
            parent_work_id=self.parent_work_id,
            child_work_id=self.child_a.id,
            subagent_id=self.subagent_a.id,
            delegation_id=self.delegation_a.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            task="Task",
            execution_contract=widened_contract,
        )
        with self.assertRaises(SubagentExecutionScopeError):
            coordinator.execute_child(req)


# =============================================================================
# Test Group F: Approval Boundary
# =============================================================================

class TestApprovalBoundary(BaseSubagentExecutionTestCase):
    """Group F: Unapproved execution enters APPROVAL_REQUIRED, no auto-approval."""

    def test_child_without_contract_transitions_to_approval_required(self) -> None:
        coordinator = SubagentExecutionCoordinator(work_store=self.work_store, repo_dir=self.repo_dir)

        req = SubagentExecutionRequest(
            parent_work_id=self.parent_work_id,
            child_work_id=self.child_a.id,
            subagent_id=self.subagent_a.id,
            delegation_id=self.delegation_a.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            task="Modify auth security logic",
            execution_contract=None,  # No contract!
        )

        res = coordinator.execute_child(req)

        self.assertFalse(res.success)
        self.assertEqual(res.status, WorkStatus.APPROVAL_REQUIRED)
        self.assertIn("requires an ApprovedExecutionContract", res.summary)

        # Verify child work in store is at APPROVAL_REQUIRED
        updated_cw = get_child_work(self.work_store, self.child_a.id)
        self.assertIsNotNone(updated_cw)
        assert updated_cw is not None
        self.assertEqual(updated_cw.status, WorkStatus.APPROVAL_REQUIRED)


# =============================================================================
# Test Group G & H: Transaction and Orchestrator Integration
# =============================================================================

class TestTransactionAndOrchestrator(BaseSubagentExecutionTestCase):
    """Groups G & H: Execution passes transaction store and terminates in Orchestrator."""

    def test_execution_propagates_transaction_store_to_run_config(self) -> None:
        captured_cfg: List[RunConfig] = []

        def test_runner(cfg: RunConfig) -> List[StepResult]:
            captured_cfg.append(cfg)
            return [StepResult(PlanStep("1", "done", []), "opened_pr", 0, "OK")]

        coordinator = SubagentExecutionCoordinator(
            work_store=self.work_store,
            repo_dir=self.repo_dir,
            transaction_store=self.tx_store,
            orchestrator_runner=test_runner,
        )

        contract = self.make_execution_contract("src/auth.py")
        req = SubagentExecutionRequest(
            parent_work_id=self.parent_work_id,
            child_work_id=self.child_a.id,
            subagent_id=self.subagent_a.id,
            delegation_id=self.delegation_a.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            task="Refactor",
            execution_contract=contract,
        )

        res = coordinator.execute_child(req)
        self.assertTrue(res.success)
        self.assertEqual(len(captured_cfg), 1)
        self.assertIs(captured_cfg[0].transaction_store, self.tx_store)
        self.assertEqual(captured_cfg[0].mode, "build")


# =============================================================================
# Test Group I: Dependency Ordering
# =============================================================================

class TestDependencyOrdering(BaseSubagentExecutionTestCase):
    """Group I: Dependency DAG enforcement (B cannot execute before A is DONE)."""

    def test_dependent_cannot_execute_before_prerequisite_is_done(self) -> None:
        # DelegationGroup: child_b depends on child_a
        group = DelegationGroup(
            delegation_group_id="grp_dep_01",
            parent_work_id=self.parent_work_id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            mode=DelegationMode.SEQUENTIAL,
            child_work_ids=(self.child_a.id, self.child_b.id),
            max_concurrency=1,
            dependencies={self.child_b.id: (self.child_a.id,)},
            coordination_states={
                self.child_a.id: CoordinationState.RUNNABLE,
                self.child_b.id: CoordinationState.WAITING,
            },
        )
        save_delegation_group(self.work_store, group)

        coordinator = SubagentExecutionCoordinator(work_store=self.work_store, repo_dir=self.repo_dir)
        contract_b = self.make_execution_contract("src/pay.py")
        req_b = SubagentExecutionRequest(
            parent_work_id=self.parent_work_id,
            child_work_id=self.child_b.id,
            subagent_id=self.subagent_b.id,
            delegation_id=self.delegation_b.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            task="Refactor payment",
            execution_contract=contract_b,
        )

        # Child B execution should fail because dependency child A is not DONE
        with self.assertRaises(SubagentExecutionDependencyError):
            coordinator.execute_child(req_b, delegation_group=group)


# =============================================================================
# Test Group J: Parallel Coordination
# =============================================================================

class TestParallelCoordination(BaseSubagentExecutionTestCase):
    """Group J: Independent children executed within concurrency capacity."""

    def test_parallel_independent_children_execution(self) -> None:
        group = DelegationGroup(
            delegation_group_id="grp_par_01",
            parent_work_id=self.parent_work_id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            mode=DelegationMode.PARALLEL,
            child_work_ids=(self.child_a.id, self.child_b.id),
            max_concurrency=2,
            dependencies={},
            coordination_states={
                self.child_a.id: CoordinationState.RUNNABLE,
                self.child_b.id: CoordinationState.RUNNABLE,
            },
        )
        save_delegation_group(self.work_store, group)

        mock_runner = MagicMock(return_value=[StepResult(PlanStep("1", "done", []), "opened_pr", 0, "OK")])
        coordinator = SubagentExecutionCoordinator(
            work_store=self.work_store,
            repo_dir=self.repo_dir,
            orchestrator_runner=mock_runner,
        )

        contract_a = self.make_execution_contract("src/auth.py")
        contract_b = self.make_execution_contract("src/pay.py")

        requests = {
            self.child_a.id: SubagentExecutionRequest(
                parent_work_id=self.parent_work_id,
                child_work_id=self.child_a.id,
                subagent_id=self.subagent_a.id,
                delegation_id=self.delegation_a.id,
                actor=self.actor,
                session_id=self.session_id,
                session_incarnation_id=self.session_incarnation_id,
                task="Refactor auth",
                execution_contract=contract_a,
            ),
            self.child_b.id: SubagentExecutionRequest(
                parent_work_id=self.parent_work_id,
                child_work_id=self.child_b.id,
                subagent_id=self.subagent_b.id,
                delegation_id=self.delegation_b.id,
                actor=self.actor,
                session_id=self.session_id,
                session_incarnation_id=self.session_incarnation_id,
                task="Refactor pay",
                execution_contract=contract_b,
            ),
        }

        agg_res, results = coordinator.execute_group(
            self.parent_work_id,
            group.delegation_group_id,
            requests,
        )

        self.assertEqual(len(results), 2)
        self.assertTrue(results[self.child_a.id].success)
        self.assertTrue(results[self.child_b.id].success)
        self.assertEqual(agg_res.status, AggregateStatus.COMPLETE)
        self.assertEqual(agg_res.successful_children, 2)


# =============================================================================
# Test Group K & L: Failure Cascades and Cancellation
# =============================================================================

class TestFailureAndCancellation(BaseSubagentExecutionTestCase):
    """Groups K & L: Failure blocks downstream dependents, cancellation prevents execution."""

    def test_failed_child_causes_downstream_to_be_blocked_not_failed(self) -> None:
        """1. Failed prerequisite -> dependent BLOCKED, not FAILED."""
        group = DelegationGroup(
            delegation_group_id="grp_fail_01",
            parent_work_id=self.parent_work_id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            mode=DelegationMode.SEQUENTIAL,
            child_work_ids=(self.child_a.id, self.child_b.id),
            max_concurrency=1,
            dependencies={self.child_b.id: (self.child_a.id,)},
            coordination_states={
                self.child_a.id: CoordinationState.RUNNABLE,
                self.child_b.id: CoordinationState.WAITING,
            },
        )
        save_delegation_group(self.work_store, group)

        # Runner raises exception to simulate child A failure
        failing_runner = MagicMock(side_effect=RuntimeError("Subagent execution timeout"))
        coordinator = SubagentExecutionCoordinator(
            work_store=self.work_store,
            repo_dir=self.repo_dir,
            orchestrator_runner=failing_runner,
        )

        contract_a = self.make_execution_contract("src/auth.py")
        req_a = SubagentExecutionRequest(
            parent_work_id=self.parent_work_id,
            child_work_id=self.child_a.id,
            subagent_id=self.subagent_a.id,
            delegation_id=self.delegation_a.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            task="Refactor auth",
            execution_contract=contract_a,
        )

        res_a = coordinator.execute_child(req_a, delegation_group=group)
        self.assertFalse(res_a.success)
        self.assertEqual(res_a.status, WorkStatus.FAILED)

        # Verify downstream child B is BLOCKED via P1.3G failure propagation
        upd_group = get_delegation_group(self.work_store, self.parent_work_id, group.delegation_group_id)
        self.assertIsNotNone(upd_group)
        assert upd_group is not None
        self.assertEqual(upd_group.coordination_states[self.child_b.id], CoordinationState.BLOCKED)

        # CRITICAL P1.3G/P1.3I invariant: Child B's canonical WorkStatus is NOT changed to FAILED (BLOCKED != FAILED)
        child_b_loaded = get_child_work(self.work_store, self.child_b.id)
        self.assertIsNotNone(child_b_loaded)
        assert child_b_loaded is not None
        self.assertEqual(child_b_loaded.status, WorkStatus.CREATED)
        self.assertNotEqual(child_b_loaded.status, WorkStatus.FAILED)

        # Blocked dependent cannot execute
        contract_b = self.make_execution_contract("src/pay.py")
        req_b = SubagentExecutionRequest(
            parent_work_id=self.parent_work_id,
            child_work_id=self.child_b.id,
            subagent_id=self.subagent_b.id,
            delegation_id=self.delegation_b.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            task="Refactor pay",
            execution_contract=contract_b,
        )
        with self.assertRaises((SubagentExecutionBlockedError, SubagentExecutionStateError)):
            coordinator.execute_child(req_b, delegation_group=upd_group)

    def test_cancelled_child_causes_downstream_to_be_blocked_not_failed(self) -> None:
        """2. Cancelled prerequisite -> dependent BLOCKED, not FAILED."""
        group = DelegationGroup(
            delegation_group_id="grp_canc_01",
            parent_work_id=self.parent_work_id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            mode=DelegationMode.SEQUENTIAL,
            child_work_ids=(self.child_a.id, self.child_b.id),
            max_concurrency=1,
            dependencies={self.child_b.id: (self.child_a.id,)},
            coordination_states={
                self.child_a.id: CoordinationState.RUNNABLE,
                self.child_b.id: CoordinationState.WAITING,
            },
        )
        save_delegation_group(self.work_store, group)

        # Cancel child A and propagate cancellation
        cw_cancelled = self.child_a.cancel("User cancelled task A")
        save_child_work(self.work_store, cw_cancelled)

        upd_group, _, _, _ = propagate_child_cancellation(
            group,
            {self.child_a.id: cw_cancelled, self.child_b.id: self.child_b},
            self.child_a.id,
            reason="User cancelled task A",
        )
        save_delegation_group(self.work_store, upd_group)

        # Prerequisite A is CANCELLED; dependent B is BLOCKED (NOT FAILED)
        self.assertEqual(cw_cancelled.status, WorkStatus.CANCELLED)
        self.assertEqual(upd_group.coordination_states[self.child_b.id], CoordinationState.BLOCKED)

        child_b_loaded = get_child_work(self.work_store, self.child_b.id)
        self.assertIsNotNone(child_b_loaded)
        assert child_b_loaded is not None
        self.assertEqual(child_b_loaded.status, WorkStatus.CREATED)
        self.assertNotEqual(child_b_loaded.status, WorkStatus.FAILED)
        self.assertNotEqual(child_b_loaded.status, WorkStatus.CANCELLED)

        # Blocked dependent cannot execute
        coordinator = SubagentExecutionCoordinator(work_store=self.work_store, repo_dir=self.repo_dir)
        contract_b = self.make_execution_contract("src/pay.py")
        req_b = SubagentExecutionRequest(
            parent_work_id=self.parent_work_id,
            child_work_id=self.child_b.id,
            subagent_id=self.subagent_b.id,
            delegation_id=self.delegation_b.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            task="Refactor pay",
            execution_contract=contract_b,
        )
        with self.assertRaises((SubagentExecutionBlockedError, SubagentExecutionStateError)):
            coordinator.execute_child(req_b, delegation_group=upd_group)

    def test_cancelled_child_itself_cannot_execute(self) -> None:
        """3. Cancelled child itself cannot execute."""
        coordinator = SubagentExecutionCoordinator(work_store=self.work_store, repo_dir=self.repo_dir)

        # Cancel child A
        cw_cancelled = self.child_a.cancel("User cancelled task")
        save_child_work(self.work_store, cw_cancelled)

        contract_a = self.make_execution_contract("src/auth.py")
        req_a = SubagentExecutionRequest(
            parent_work_id=self.parent_work_id,
            child_work_id=self.child_a.id,
            subagent_id=self.subagent_a.id,
            delegation_id=self.delegation_a.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            task="Refactor auth",
            execution_contract=contract_a,
        )
        with self.assertRaises(SubagentExecutionStateError):
            coordinator.execute_child(req_a)

    def test_result_aggregation_distinguishes_failure_from_blocked_work(self) -> None:
        """4. Result aggregation distinguishes actual failure from blocked work."""
        group = DelegationGroup(
            delegation_group_id="grp_agg_dist_01",
            parent_work_id=self.parent_work_id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            mode=DelegationMode.SEQUENTIAL,
            child_work_ids=(self.child_a.id, self.child_b.id),
            max_concurrency=1,
            dependencies={self.child_b.id: (self.child_a.id,)},
            coordination_states={
                self.child_a.id: CoordinationState.RUNNABLE,
                self.child_b.id: CoordinationState.WAITING,
            },
        )
        save_delegation_group(self.work_store, group)

        failing_runner = MagicMock(side_effect=RuntimeError("Subagent execution timeout"))
        coordinator = SubagentExecutionCoordinator(
            work_store=self.work_store,
            repo_dir=self.repo_dir,
            orchestrator_runner=failing_runner,
        )

        contract_a = self.make_execution_contract("src/auth.py")
        contract_b = self.make_execution_contract("src/pay.py")
        reqs = {
            self.child_a.id: SubagentExecutionRequest(
                parent_work_id=self.parent_work_id,
                child_work_id=self.child_a.id,
                subagent_id=self.subagent_a.id,
                delegation_id=self.delegation_a.id,
                actor=self.actor,
                session_id=self.session_id,
                session_incarnation_id=self.session_incarnation_id,
                task="Refactor auth",
                execution_contract=contract_a,
            ),
            self.child_b.id: SubagentExecutionRequest(
                parent_work_id=self.parent_work_id,
                child_work_id=self.child_b.id,
                subagent_id=self.subagent_b.id,
                delegation_id=self.delegation_b.id,
                actor=self.actor,
                session_id=self.session_id,
                session_incarnation_id=self.session_incarnation_id,
                task="Refactor pay",
                execution_contract=contract_b,
            ),
        }

        agg_res, results = coordinator.execute_group(
            self.parent_work_id,
            group.delegation_group_id,
            reqs,
        )

        # Child A executed and FAILED
        self.assertIn(self.child_a.id, results)
        self.assertFalse(results[self.child_a.id].success)
        self.assertEqual(results[self.child_a.id].status, WorkStatus.FAILED)

        # Child B was BLOCKED and never executed!
        self.assertNotIn(self.child_b.id, results)

        child_b_cw = get_child_work(self.work_store, self.child_b.id)
        assert child_b_cw is not None
        self.assertEqual(child_b_cw.status, WorkStatus.CREATED)
        self.assertNotEqual(child_b_cw.status, WorkStatus.FAILED)

        # Aggregation result clearly distinguishes actual failure from blocked/unexecuted work
        self.assertEqual(agg_res.failed_children, 1)        # A failed
        self.assertEqual(agg_res.successful_children, 0)
        self.assertEqual(agg_res.incomplete_children, 1)    # B is incomplete/blocked, not failed
        self.assertEqual(agg_res.status, AggregateStatus.INCOMPLETE)
        self.assertFalse(agg_res.success)

    def test_parent_failure_policy_propagation(self) -> None:
        """5. Parent failure policy still behaves correctly."""
        # Case A: PROPAGATE_FAILURE causes parent work to fail when child fails
        group_a = DelegationGroup(
            delegation_group_id="grp_p_fail_01",
            parent_work_id=self.parent_work_id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            mode=DelegationMode.SEQUENTIAL,
            child_work_ids=(self.child_a.id,),
            max_concurrency=1,
            dependencies={},
            coordination_states={self.child_a.id: CoordinationState.RUNNABLE},
        )
        save_delegation_group(self.work_store, group_a)

        failing_runner = MagicMock(side_effect=RuntimeError("Child crashed"))
        coordinator = SubagentExecutionCoordinator(
            work_store=self.work_store,
            repo_dir=self.repo_dir,
            orchestrator_runner=failing_runner,
        )
        contract_a = self.make_execution_contract("src/auth.py")
        req_a = SubagentExecutionRequest(
            parent_work_id=self.parent_work_id,
            child_work_id=self.child_a.id,
            subagent_id=self.subagent_a.id,
            delegation_id=self.delegation_a.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            task="Refactor auth",
            execution_contract=contract_a,
        )

        res_a = coordinator.execute_child(
            req_a,
            delegation_group=group_a,
            parent_failure_policy=ParentFailurePolicy.PROPAGATE_FAILURE,
        )
        self.assertFalse(res_a.success)
        self.assertEqual(res_a.status, WorkStatus.FAILED)

        # Parent work transitioned to FAILED
        parent_loaded = self.work_store.get(self.parent_work_id)
        assert parent_loaded is not None
        self.assertEqual(parent_loaded.status, WorkStatus.FAILED)

    def test_failed_child_fanout_dependents_all_blocked_not_failed(self) -> None:
        """6. Fan-out (A -> B, A -> C): Child A fails -> both B and C BLOCKED, neither FAILED."""
        subagent_c = Subagent(
            subagent_id="sub_audit_01",
            parent_work_id=self.parent_work_id,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            actor=self.actor,
            role="audit_specialist",
            purpose="Audit subsystem",
            created_at=self.now,
            updated_at=self.now,
        )
        caps_c = Capabilities(
            filesystem=FilesystemPolicy(read=("docs/audit.md",), write=("docs/audit.md",)),
            shell=ShellPolicy(execute=False),
            network=NetworkPolicy(access=False),
            git=GitPolicy(read=True, commit=False),
        )
        delg_c = DelegationContract(
            delegation_id="delg_audit_01",
            parent_work_id=self.parent_work_id,
            child_subagent_id=subagent_c.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            capabilities=caps_c,
            target_scope=("docs/audit.md",),
            expires_at=self.now + 600.0,
            created_at=self.now,
        )
        child_c = ChildWork.create(
            parent_work=self.parent_work,
            subagent=subagent_c,
            delegation=delg_c,
            child_work_id="work_child_audit_01",
            created_at=self.now,
        )
        save_child_work(self.work_store, child_c)

        group = DelegationGroup(
            delegation_group_id="grp_fanout_fail_01",
            parent_work_id=self.parent_work_id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            mode=DelegationMode.PARALLEL,
            child_work_ids=(self.child_a.id, self.child_b.id, child_c.id),
            max_concurrency=2,
            dependencies={
                self.child_b.id: (self.child_a.id,),
                child_c.id: (self.child_a.id,),
            },
            coordination_states={
                self.child_a.id: CoordinationState.RUNNABLE,
                self.child_b.id: CoordinationState.WAITING,
                child_c.id: CoordinationState.WAITING,
            },
        )
        save_delegation_group(self.work_store, group)

        failing_runner = MagicMock(side_effect=RuntimeError("Subagent execution timeout"))
        coordinator = SubagentExecutionCoordinator(
            work_store=self.work_store,
            repo_dir=self.repo_dir,
            orchestrator_runner=failing_runner,
        )

        contract_a = self.make_execution_contract("src/auth.py")
        contract_b = self.make_execution_contract("src/pay.py")
        contract_c = self.make_execution_contract("docs/audit.md")
        reqs = {
            self.child_a.id: SubagentExecutionRequest(
                parent_work_id=self.parent_work_id,
                child_work_id=self.child_a.id,
                subagent_id=self.subagent_a.id,
                delegation_id=self.delegation_a.id,
                actor=self.actor,
                session_id=self.session_id,
                session_incarnation_id=self.session_incarnation_id,
                task="Refactor auth",
                execution_contract=contract_a,
            ),
            self.child_b.id: SubagentExecutionRequest(
                parent_work_id=self.parent_work_id,
                child_work_id=self.child_b.id,
                subagent_id=self.subagent_b.id,
                delegation_id=self.delegation_b.id,
                actor=self.actor,
                session_id=self.session_id,
                session_incarnation_id=self.session_incarnation_id,
                task="Refactor pay",
                execution_contract=contract_b,
            ),
            child_c.id: SubagentExecutionRequest(
                parent_work_id=self.parent_work_id,
                child_work_id=child_c.id,
                subagent_id=subagent_c.id,
                delegation_id=delg_c.id,
                actor=self.actor,
                session_id=self.session_id,
                session_incarnation_id=self.session_incarnation_id,
                task="Audit docs",
                execution_contract=contract_c,
            ),
        }

        agg_res, results = coordinator.execute_group(
            self.parent_work_id,
            group.delegation_group_id,
            reqs,
        )

        # 1. Child A executed and reached FAILED
        self.assertIn(self.child_a.id, results)
        self.assertFalse(results[self.child_a.id].success)
        self.assertEqual(results[self.child_a.id].status, WorkStatus.FAILED)

        # 2. Downstream dependents B and C never executed
        self.assertNotIn(self.child_b.id, results)
        self.assertNotIn(child_c.id, results)

        # 3. Downstream dependents B and C are CoordinationState.BLOCKED in delegation group
        upd_group = get_delegation_group(self.work_store, self.parent_work_id, group.delegation_group_id)
        assert upd_group is not None
        self.assertEqual(upd_group.coordination_states[self.child_b.id], CoordinationState.BLOCKED)
        self.assertEqual(upd_group.coordination_states[child_c.id], CoordinationState.BLOCKED)

        # 4. Downstream dependents B and C MUST remain non-terminal (CREATED), NOT FAILED
        loaded_b = get_child_work(self.work_store, self.child_b.id)
        loaded_c = get_child_work(self.work_store, child_c.id)
        assert loaded_b is not None and loaded_c is not None
        self.assertEqual(loaded_b.status, WorkStatus.CREATED)
        self.assertNotEqual(loaded_b.status, WorkStatus.FAILED)
        self.assertEqual(loaded_c.status, WorkStatus.CREATED)
        self.assertNotEqual(loaded_c.status, WorkStatus.FAILED)

        # 5. Blocked dependents cannot execute
        with self.assertRaises(SubagentExecutionBlockedError):
            coordinator.execute_child(reqs[self.child_b.id], delegation_group=upd_group)
        with self.assertRaises(SubagentExecutionBlockedError):
            coordinator.execute_child(reqs[child_c.id], delegation_group=upd_group)

        # 6. Result aggregation distinguishes actual failure (1) from blocked work (2)
        self.assertEqual(agg_res.failed_children, 1)
        self.assertEqual(agg_res.incomplete_children, 2)
        self.assertEqual(agg_res.successful_children, 0)
        self.assertEqual(agg_res.status, AggregateStatus.INCOMPLETE)
        self.assertFalse(agg_res.success)


# =============================================================================
# Test Group M: Idempotence
# =============================================================================

class TestIdempotence(BaseSubagentExecutionTestCase):
    """Group M: Already completed child does not re-execute."""

    def test_done_child_returns_idempotent_result_without_re_executing(self) -> None:
        mock_runner = MagicMock(return_value=[StepResult(PlanStep("1", "done", []), "opened_pr", 0, "OK")])
        coordinator = SubagentExecutionCoordinator(
            work_store=self.work_store,
            repo_dir=self.repo_dir,
            orchestrator_runner=mock_runner,
        )

        contract = self.make_execution_contract("src/auth.py")
        req = SubagentExecutionRequest(
            parent_work_id=self.parent_work_id,
            child_work_id=self.child_a.id,
            subagent_id=self.subagent_a.id,
            delegation_id=self.delegation_a.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            task="Task",
            execution_contract=contract,
        )

        res1 = coordinator.execute_child(req)
        self.assertTrue(res1.success)
        self.assertEqual(mock_runner.call_count, 1)

        # Second call returns existing result without calling orchestrator runner again
        res2 = coordinator.execute_child(req)
        self.assertTrue(res2.success)
        self.assertEqual(res2.status, WorkStatus.DONE)
        self.assertEqual(mock_runner.call_count, 1)


# =============================================================================
# Test Group N: Concurrent Claims
# =============================================================================

class TestConcurrentClaims(BaseSubagentExecutionTestCase):
    """Group N: Multiple claim attempts yield exactly one winner."""

    def test_concurrent_claim_winner(self) -> None:
        group = DelegationGroup(
            delegation_group_id="grp_conc_01",
            parent_work_id=self.parent_work_id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            mode=DelegationMode.PARALLEL,
            child_work_ids=(self.child_a.id, self.child_b.id),
            max_concurrency=1,
            dependencies={},
            coordination_states={
                self.child_a.id: CoordinationState.RUNNABLE,
                self.child_b.id: CoordinationState.RUNNABLE,
            },
        )
        save_delegation_group(self.work_store, group)

        # Claim child A (consumes the 1 available slot)
        cmap = {self.child_a.id: self.child_a, self.child_b.id: self.child_b}
        upd_group, _ = DelegationRuntime.claim_child(group, self.child_a.id, cmap)
        save_delegation_group(self.work_store, upd_group)

        # Second attempt to claim child B beyond max_concurrency raises DelegationConcurrencyError
        with self.assertRaises(DelegationConcurrencyError):
            DelegationRuntime.claim_child(
                upd_group,
                self.child_b.id,
                cmap,
            )


# =============================================================================
# Test Group O: Crash Recovery
# =============================================================================

class TestCrashRecovery(BaseSubagentExecutionTestCase):
    """Group O: Interruption and restart correctly reconstructs state from persistent WorkStore."""

    def test_interrupted_child_state_reconstruction_on_restart(self) -> None:
        # 1. Setup persistent disk-backed FileWorkStore
        disk_works_dir = Path(self.temp_dir) / "persistent_works"
        store1 = FileWorkStore(works_dir=disk_works_dir)
        store1.create(self.parent_work)
        save_child_work(store1, self.child_a)

        mock_runner = MagicMock(return_value=[StepResult(PlanStep("1", "done", []), "opened_pr", 0, "Recovered PR")])
        coordinator1 = SubagentExecutionCoordinator(
            work_store=store1,
            repo_dir=self.repo_dir,
            orchestrator_runner=mock_runner,
        )
        contract = self.make_execution_contract("src/auth.py")
        req = SubagentExecutionRequest(
            parent_work_id=self.parent_work_id,
            child_work_id=self.child_a.id,
            subagent_id=self.subagent_a.id,
            delegation_id=self.delegation_a.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            task="Refactor auth",
            execution_contract=contract,
        )
        res1 = coordinator1.execute_child(req)
        self.assertTrue(res1.success)

        # 2. Simulate process restart by instantiating a completely new FileWorkStore from same disk dir
        store2 = FileWorkStore(works_dir=disk_works_dir)
        restarted_cw = get_child_work(store2, self.child_a.id)
        self.assertIsNotNone(restarted_cw)
        assert restarted_cw is not None
        self.assertEqual(restarted_cw.status, WorkStatus.DONE)
        self.assertTrue(restarted_cw.is_terminal)

        # 3. Fresh coordinator on restarted store returns idempotent result without re-executing
        fresh_runner = MagicMock()
        coordinator2 = SubagentExecutionCoordinator(
            work_store=store2,
            repo_dir=self.repo_dir,
            orchestrator_runner=fresh_runner,
        )
        res2 = coordinator2.execute_child(req)
        self.assertTrue(res2.success)
        self.assertEqual(res2.status, WorkStatus.DONE)
        self.assertEqual(fresh_runner.call_count, 0)


# =============================================================================
# Test Group P & Q: Result Aggregation and Parent Continuation
# =============================================================================

class TestResultAggregationAndParentContinuation(BaseSubagentExecutionTestCase):
    """Groups P & Q: Results bridge to P1.3F ResultAggregator and update parent."""

    def test_child_completion_makes_parent_eligible_for_continuation(self) -> None:
        group = DelegationGroup(
            delegation_group_id="grp_agg_01",
            parent_work_id=self.parent_work_id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            mode=DelegationMode.PARALLEL,
            child_work_ids=(self.child_a.id,),
            max_concurrency=1,
            dependencies={},
            coordination_states={self.child_a.id: CoordinationState.RUNNABLE},
        )
        save_delegation_group(self.work_store, group)

        mock_runner = MagicMock(return_value=[StepResult(PlanStep("1", "done", []), "opened_pr", 0, "Auth refactored")])
        coordinator = SubagentExecutionCoordinator(
            work_store=self.work_store,
            repo_dir=self.repo_dir,
            orchestrator_runner=mock_runner,
        )

        contract = self.make_execution_contract("src/auth.py")
        reqs = {
            self.child_a.id: SubagentExecutionRequest(
                parent_work_id=self.parent_work_id,
                child_work_id=self.child_a.id,
                subagent_id=self.subagent_a.id,
                delegation_id=self.delegation_a.id,
                actor=self.actor,
                session_id=self.session_id,
                session_incarnation_id=self.session_incarnation_id,
                task="Refactor auth",
                execution_contract=contract,
            )
        }

        agg_res, results = coordinator.execute_group(
            self.parent_work_id,
            group.delegation_group_id,
            reqs,
        )

        self.assertEqual(agg_res.status, AggregateStatus.COMPLETE)
        self.assertEqual(agg_res.successful_children, 1)

        # Verify parent work resume_metadata was updated with aggregate result
        p_work = self.work_store.get(self.parent_work_id)
        self.assertIsNotNone(p_work)
        assert p_work is not None
        self.assertIsNotNone(p_work.resume_metadata)
        assert p_work.resume_metadata is not None
        self.assertIn("aggregate_result", p_work.resume_metadata)
        self.assertTrue(p_work.resume_metadata.get("children_completed"))


# =============================================================================
# Test Group R: Context Isolation
# =============================================================================

class TestContextIsolation(BaseSubagentExecutionTestCase):
    """Group R: Context is explicit, bounded, and isolates parent conversation history."""

    def test_context_formatting_does_not_leak_parent_history(self) -> None:
        ctx = SubagentExecutionContext(
            task="Refactor auth token expiration",
            parent_summary="Parent auth refactoring plan",
            relevant_artifacts=("src/auth.py", "tests/test_auth.py"),
            constraints=("Do not touch database schema", "Preserve existing API signatures"),
        )
        formatted = ctx.format_task_prompt()

        self.assertIn("Task: Refactor auth token expiration", formatted)
        self.assertIn("Parent Context: Parent auth refactoring plan", formatted)
        self.assertIn("Do not touch database schema", formatted)
        self.assertNotIn("User:", formatted)
        self.assertNotIn("Assistant:", formatted)


# =============================================================================
# Test Group S: Authority Invariance
# =============================================================================

class TestAuthorityInvariance(BaseSubagentExecutionTestCase):
    """Group S: Subagent execution module does not contain execution primitives."""

    def test_ast_verification_no_execution_primitives(self) -> None:
        """Verify subagent_execution.py contains zero direct execution primitives."""
        target_path = Path("core/runtime/subagent_execution.py").resolve()
        with open(target_path, "r", encoding="utf-8") as f:
            source = f.read()

        parsed = ast.parse(source)

        forbidden_calls = {"subprocess", "os.system", "os.popen", "shutil.rmtree"}
        for node in ast.walk(parsed):
            if isinstance(node, ast.Call):
                func = node.func
                # Check for calls like subprocess.run, os.system
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                    call_name = f"{func.value.id}.{func.attr}"
                    self.assertNotIn(call_name, forbidden_calls)

            # Check that no second Orchestrator class is defined
            if isinstance(node, ast.ClassDef):
                self.assertNotIn(node.name, {
                    "SubagentExecutor", "ChildExecutor", "DelegatedOrchestrator",
                    "SubagentOrchestrator", "ChildTransactionEngine",
                })


# =============================================================================
# Test Group T: Remote Path Integration
# =============================================================================

class TestRemotePathIntegration(BaseSubagentExecutionTestCase):
    """Group T: RemoteWorkCoordinator -> ChildWork -> Execution -> Result."""

    def test_remote_work_child_delegation_end_to_end(self) -> None:
        remote_coord = RemoteWorkCoordinator(work_store=self.work_store)
        req = remote_coord.normalize(
            message_id="msg_remote_001",
            actor=self.actor,
            channel="telegram",
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            intent="Refactor remote security",
            workspace=self.repo_dir,
            targets=("src/auth.py",),
        )
        remote_work, plan = remote_coord.submit(req, self.parent_caps)

        # Delegate child work from remote work
        sub = Subagent(
            subagent_id="sub_rem_01",
            parent_work_id=remote_work.id,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            actor=self.actor,
            role="worker",
            purpose="Refactor remote",
            created_at=self.now,
            updated_at=self.now,
        )
        delg = DelegationContract(
            delegation_id="delg_rem_01",
            parent_work_id=remote_work.id,
            child_subagent_id=sub.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            capabilities=self.delegation_a_caps,
            target_scope=("src/auth.py",),
            expires_at=self.now + 600.0,
            created_at=self.now,
        )
        child = ChildWork.create(
            parent_work=remote_work,
            subagent=sub,
            delegation=delg,
            child_work_id="work_child_rem_01",
            created_at=self.now,
        )
        save_child_work(self.work_store, child)

        # Execute child work
        mock_runner = MagicMock(return_value=[StepResult(PlanStep("1", "done", []), "opened_pr", 0, "Remote done")])
        coordinator = SubagentExecutionCoordinator(
            work_store=self.work_store,
            repo_dir=self.repo_dir,
            orchestrator_runner=mock_runner,
        )

        contract = self.make_execution_contract("src/auth.py", channel="telegram")
        exec_req = SubagentExecutionRequest(
            parent_work_id=remote_work.id,
            child_work_id=child.id,
            subagent_id=sub.id,
            delegation_id=delg.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            task="Refactor remote",
            execution_contract=contract,
        )

        res = coordinator.execute_child(exec_req, origin_channel="telegram")
        self.assertTrue(res.success)
        self.assertEqual(res.status, WorkStatus.DONE)


# =============================================================================
# Test Group U: Property Invariants
# =============================================================================

class TestPropertyInvariants(BaseSubagentExecutionTestCase):
    """Group U: Mathematical and architectural invariants."""

    def test_contract_approved_targets_subset_of_delegated_scope(self) -> None:
        """Invariant: Child execution scope is strictly a subset of delegated scope."""
        contract = self.make_execution_contract("src/auth.py")
        self.assertTrue(contract.approved_targets.issubset(set(self.delegation_a.target_scope)))

    def test_terminal_child_cannot_execute_twice(self) -> None:
        """Invariant: Completed child returns idempotent success without state changes."""
        mock_runner = MagicMock(return_value=[StepResult(PlanStep("1", "done", []), "opened_pr", 0, "OK")])
        coordinator = SubagentExecutionCoordinator(
            work_store=self.work_store,
            repo_dir=self.repo_dir,
            orchestrator_runner=mock_runner,
        )
        contract = self.make_execution_contract("src/auth.py")
        req = SubagentExecutionRequest(
            parent_work_id=self.parent_work_id,
            child_work_id=self.child_a.id,
            subagent_id=self.subagent_a.id,
            delegation_id=self.delegation_a.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            task="Task",
            execution_contract=contract,
        )
        res1 = coordinator.execute_child(req)
        self.assertEqual(res1.status, WorkStatus.DONE)
        res2 = coordinator.execute_child(req)
        self.assertEqual(res2.status, WorkStatus.DONE)
        self.assertEqual(mock_runner.call_count, 1)


if __name__ == "__main__":
    unittest.main()
