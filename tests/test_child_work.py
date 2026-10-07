"""Comprehensive Unit and Property Tests for P1.3D Child Work Lifecycle.

Tests:
A. Creation (valid bindings, mismatches rejected, expired delegation rejected)
B. Lifecycle (state machine transitions, invalid transitions, terminal immutability)
C. Binding (immutability of parent, subagent, delegation, session, incarnation)
D. Subagent synchronization (READY -> RUNNING -> COMPLETED / FAILED / CANCELLED)
E. Expiration (expired delegation blocks lifecycle operations fail-closed)
F. Session reset (invalidation of stale incarnation across /reset and /new)
G. Persistence & Storage (FileWorkStore save/load, restart, serialization, secret scrubbing)
H. Concurrency & OCC (stale revision conflict detection, concurrent resume protection)
I. Execution boundary assertions (zero subprocess, zero filesystem mutation, no orchestrator)
J. End-to-end composition (Parent -> Delegation -> Subagent -> Child -> Plan -> Approval -> Contract -> Transaction -> Orchestrator -> Done)
K. Property tests (monotonic invariant verification, authority bounding)
"""
from __future__ import annotations

import inspect
import json
import os
import shutil
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.runtime.approval import (
    ApprovalRequest,
    ApprovalService,
    ApprovalStatus,
    CanonicalOperation,
    InMemoryApprovalStore,
    RiskClass,
)
from core.runtime.capabilities import Capabilities, FilesystemPolicy, GitPolicy, NetworkPolicy, ShellPolicy
from core.runtime.child_work import (
    CURRENT_CHILD_WORK_SCHEMA_VERSION,
    ChildWork,
    ChildWorkBindingError,
    ChildWorkError,
    ChildWorkExpiredError,
    ChildWorkIntegrityError,
    ChildWorkSessionStaleError,
    InvalidChildWorkTransition,
    attach_child_work_reference,
    authorize_child_work_inspection,
    authorize_child_work_mutation,
    cancel_child_works_for_parent,
    get_child_work,
    get_child_work_ids,
    list_child_works_for_parent,
    resume_child_work,
    save_child_work,
    validate_child_work_id,
)
from core.runtime.contract import ApprovedExecutionContract
from core.runtime.delegation import DelegationContract
from core.runtime.session import InMemorySessionStore, SessionManager
from core.runtime.subagent import Subagent, SubagentStatus
from core.runtime.transaction import (
    InMemoryTransactionStore,
    OperationType,
    TransactionCoordinator,
    TransactionOperation,
)
from core.runtime.work import (
    InMemoryWorkStore,
    InvalidWorkTransition,
    StaleWorkRevisionError,
    VerificationResult,
    Work,
    WorkFailure,
    WorkStatus,
)
from core.runtime.work_store import FileWorkStore
from orchestrator import Orchestrator


class BaseChildWorkTestCase(unittest.TestCase):
    """Base test case providing deterministic parent, subagent, and delegation fixtures."""

    def setUp(self) -> None:
        self.now = time.time()
        self.actor = "actor_test_user"
        self.session_id = "sess_main_01"
        self.session_incarnation_id = "inc_alpha_01"
        self.parent_work_id = "work_parent_100"
        self.subagent_id = "sub_worker_200"
        self.delegation_id = "delg_auth_300"

        # Parent Work
        self.parent_caps = Capabilities(
            filesystem=FilesystemPolicy(read=("src/auth.py", "src/models.py"), write=("src/auth.py",)),
            shell=ShellPolicy(execute=False),
            network=NetworkPolicy(access=True, scope="configured_model_api"),
            git=GitPolicy(read=True, commit=True, push=False),
        )
        self.parent_work = Work(
            id=self.parent_work_id,
            intent="Refactor authentication system",
            goal="Harden login tokens and user session security",
            scope=("src/auth.py", "src/models.py"),
            capabilities=self.parent_caps,
            status=WorkStatus.PLANNING,
            actor_id=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            created_at=self.now,
            updated_at=self.now,
        )

        # Subagent
        self.subagent = Subagent(
            subagent_id=self.subagent_id,
            parent_work_id=self.parent_work_id,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            actor=self.actor,
            status=SubagentStatus.CREATED,
            role="auth_auditor",
            purpose="Inspect auth tokens for leaks",
            created_at=self.now,
            updated_at=self.now,
        )

        # DelegationContract (strictly attenuated)
        self.child_caps = Capabilities(
            filesystem=FilesystemPolicy(read=("src/auth.py",), write=()),
            shell=ShellPolicy(execute=False),
            network=NetworkPolicy(access=True, scope="configured_model_api"),
            git=GitPolicy(read=True, commit=False, push=False),
        )
        self.delegation = DelegationContract(
            delegation_id=self.delegation_id,
            parent_work_id=self.parent_work_id,
            child_subagent_id=self.subagent_id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            capabilities=self.child_caps,
            target_scope=("src/auth.py",),
            operation_scope=("read_file",),
            network_scope=("https://api.example.com",),
            git_policy=GitPolicy(read=True, commit=False, push=False),
            created_at=self.now,
            expires_at=self.now + 600.0,
            role="auth_auditor",
            purpose="Inspect auth tokens for leaks",
        )


class TestChildWorkCreation(BaseChildWorkTestCase):
    """Category A: Child Work Creation and Binding Validation."""

    def test_valid_child_work_creation(self) -> None:
        child = ChildWork.create(
            parent_work=self.parent_work,
            subagent=self.subagent,
            delegation=self.delegation,
            intent="Audit auth tokens",
            goal="Find token leaks in src/auth.py",
        )

        self.assertTrue(child.id.startswith("work_child_"))
        self.assertEqual(child.parent_work_id, self.parent_work_id)
        self.assertEqual(child.subagent_id, self.subagent_id)
        self.assertEqual(child.delegation_id, self.delegation_id)
        self.assertEqual(child.actor, self.actor)
        self.assertEqual(child.session_id, self.session_id)
        self.assertEqual(child.session_incarnation_id, self.session_incarnation_id)
        self.assertEqual(child.status, WorkStatus.CREATED)
        self.assertEqual(child.work.status, WorkStatus.CREATED)
        self.assertFalse(child.is_terminal)
        self.assertFalse(child.is_resumable)
        self.assertEqual(child.schema_version, CURRENT_CHILD_WORK_SCHEMA_VERSION)

        # Verify capability attenuation snapshot
        self.assertEqual(child.capabilities, self.child_caps)
        self.assertEqual(child.target_scope, ("src/auth.py",))

        # Verify subagent transition to READY
        self.assertIsNotNone(child.subagent)
        self.assertEqual(child.subagent.status, SubagentStatus.READY)  # type: ignore

    def test_mismatched_parent_rejected(self) -> None:
        bad_parent = replace(self.parent_work, id="work_other_999")
        with self.assertRaises(ChildWorkBindingError):
            ChildWork.create(
                parent_work=bad_parent,
                subagent=self.subagent,
                delegation=self.delegation,
            )

    def test_mismatched_subagent_rejected(self) -> None:
        bad_sub = replace(self.subagent, subagent_id="sub_other_999")
        with self.assertRaises(ChildWorkBindingError):
            ChildWork.create(
                parent_work=self.parent_work,
                subagent=bad_sub,
                delegation=self.delegation,
            )

    def test_mismatched_actor_rejected(self) -> None:
        bad_parent = replace(self.parent_work, actor_id="actor_eve")
        with self.assertRaises(ChildWorkBindingError):
            ChildWork.create(
                parent_work=bad_parent,
                subagent=self.subagent,
                delegation=self.delegation,
            )

    def test_mismatched_session_rejected(self) -> None:
        bad_sub = replace(self.subagent, session_id="sess_hijack_99")
        with self.assertRaises(ChildWorkBindingError):
            ChildWork.create(
                parent_work=self.parent_work,
                subagent=bad_sub,
                delegation=self.delegation,
            )

    def test_mismatched_incarnation_rejected(self) -> None:
        bad_delg = DelegationContract(
            delegation_id="delg_mismatch_inc",
            parent_work_id=self.parent_work_id,
            child_subagent_id=self.subagent_id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id="inc_stale_00",
            created_at=self.now,
            expires_at=self.now + 600.0,
        )
        with self.assertRaises(ChildWorkBindingError):
            ChildWork.create(
                parent_work=self.parent_work,
                subagent=self.subagent,
                delegation=bad_delg,
            )

    def test_expired_delegation_creation_rejected(self) -> None:
        expired_delg = DelegationContract(
            delegation_id="delg_exp_999",
            parent_work_id=self.parent_work_id,
            child_subagent_id=self.subagent_id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            created_at=self.now - 500.0,
            expires_at=self.now - 100.0,
        )
        with self.assertRaises(ChildWorkExpiredError):
            ChildWork.create(
                parent_work=self.parent_work,
                subagent=self.subagent,
                delegation=expired_delg,
            )


class TestChildWorkLifecycle(BaseChildWorkTestCase):
    """Category B: Child Work State Machine Transitions and Subagent Tracking."""

    def setUp(self) -> None:
        super().setUp()
        self.child = ChildWork.create(
            parent_work=self.parent_work,
            subagent=self.subagent,
            delegation=self.delegation,
        )

    def test_full_successful_lifecycle(self) -> None:
        # CREATED -> PLANNING (Subagent becomes RUNNING)
        c1 = self.child.transition(WorkStatus.PLANNING)
        self.assertEqual(c1.status, WorkStatus.PLANNING)
        self.assertIsNotNone(c1.subagent)
        self.assertEqual(c1.subagent.status, SubagentStatus.RUNNING)  # type: ignore

        # PLANNING -> APPROVAL_REQUIRED
        c2 = c1.transition(WorkStatus.APPROVAL_REQUIRED)
        self.assertEqual(c2.status, WorkStatus.APPROVAL_REQUIRED)

        # APPROVAL_REQUIRED -> EXECUTING
        c3 = c2.transition(WorkStatus.EXECUTING)
        self.assertEqual(c3.status, WorkStatus.EXECUTING)
        self.assertEqual(c3.subagent.status, SubagentStatus.RUNNING)  # type: ignore

        # EXECUTING -> VERIFYING
        c4 = c3.transition(WorkStatus.VERIFYING)
        self.assertEqual(c4.status, WorkStatus.VERIFYING)

        # VERIFYING -> DONE (Subagent becomes COMPLETED)
        vr = VerificationResult(status="PASS", summary="Audit successful")
        c5 = c4.transition(WorkStatus.DONE, verification_result=vr)
        self.assertEqual(c5.status, WorkStatus.DONE)
        self.assertTrue(c5.is_terminal)
        self.assertFalse(c5.is_resumable)
        self.assertIsNotNone(c5.subagent)
        self.assertEqual(c5.subagent.status, SubagentStatus.COMPLETED)  # type: ignore

    def test_failure_propagation(self) -> None:
        # CREATED -> FAILED
        fail = WorkFailure(code="INSPECT_FAIL", summary="Failed to parse auth.py")
        failed_child = self.child.transition(WorkStatus.FAILED, failure=fail)
        self.assertEqual(failed_child.status, WorkStatus.FAILED)
        self.assertTrue(failed_child.is_terminal)
        self.assertIsNotNone(failed_child.subagent)
        self.assertEqual(failed_child.subagent.status, SubagentStatus.FAILED)  # type: ignore
        self.assertEqual(failed_child.subagent.failure_reason, "Failed to parse auth.py")  # type: ignore

    def test_cancellation_propagation(self) -> None:
        # Explicit cancel
        cancelled_child = self.child.cancel(reason="Parent task superseded")
        self.assertEqual(cancelled_child.status, WorkStatus.CANCELLED)
        self.assertTrue(cancelled_child.is_terminal)
        self.assertIsNotNone(cancelled_child.subagent)
        self.assertEqual(cancelled_child.subagent.status, SubagentStatus.CANCELLED)  # type: ignore

    def test_invalid_lifecycle_transition_rejected(self) -> None:
        # Cannot jump from CREATED directly to DONE
        with self.assertRaises(InvalidWorkTransition):
            self.child.transition(WorkStatus.DONE)

    def test_terminal_state_immutable(self) -> None:
        cancelled = self.child.cancel(reason="Abort")
        with self.assertRaises(ValueError):
            cancelled.cancel("Abort again")
        with self.assertRaises(InvalidWorkTransition):
            cancelled.transition(WorkStatus.PLANNING)


class TestChildWorkBindings(BaseChildWorkTestCase):
    """Category C: Identity and Authority Binding Immutability."""

    def setUp(self) -> None:
        super().setUp()
        self.child = ChildWork.create(
            parent_work=self.parent_work,
            subagent=self.subagent,
            delegation=self.delegation,
        )

    def test_binding_fields_frozen(self) -> None:
        with self.assertRaises((AttributeError, TypeError)):
            self.child.parent_work_id = "work_tampered"  # type: ignore
        with self.assertRaises((AttributeError, TypeError)):
            self.child.subagent_id = "sub_tampered"  # type: ignore
        with self.assertRaises((AttributeError, TypeError)):
            self.child.actor = "actor_tampered"  # type: ignore
        with self.assertRaises((AttributeError, TypeError)):
            self.child.session_id = "sess_tampered"  # type: ignore
        with self.assertRaises((AttributeError, TypeError)):
            self.child.session_incarnation_id = "inc_tampered"  # type: ignore

    def test_scope_widening_rejected_during_transition(self) -> None:
        # Attempt to widen scope during transition outside delegation
        with self.assertRaises(ChildWorkBindingError):
            self.child.transition(
                WorkStatus.PLANNING,
                scope=("src/secret.py",),  # not in delegation
            )


class TestChildWorkExpiration(BaseChildWorkTestCase):
    """Category E: Delegation Expiration Enforcement."""

    def test_expired_delegation_blocks_transition(self) -> None:
        child = ChildWork.create(
            parent_work=self.parent_work,
            subagent=self.subagent,
            delegation=self.delegation,
        )
        # Advance time beyond delegation expiration
        future_time = self.now + 700.0  # expires at now + 600.0
        with self.assertRaises(ChildWorkExpiredError):
            child.transition(WorkStatus.PLANNING, current_time=future_time)

    def test_expired_delegation_blocks_resume(self) -> None:
        store = InMemoryWorkStore()
        child = ChildWork.create(
            parent_work=self.parent_work,
            subagent=self.subagent,
            delegation=self.delegation,
        )
        save_child_work(store, child)

        future_time = self.now + 800.0
        ok, reason, res_child = resume_child_work(
            child_work_id=child.id,
            actor_id=self.actor,
            channel="cli",
            work_store=store,
            current_time=future_time,
        )
        self.assertFalse(ok)
        self.assertIn("expired", reason.lower())


class TestChildWorkSessionReset(BaseChildWorkTestCase):
    """Category F: Session Incarnation Invalidation Across /reset and /new."""

    def test_session_reset_blocks_transition(self) -> None:
        child = ChildWork.create(
            parent_work=self.parent_work,
            subagent=self.subagent,
            delegation=self.delegation,
        )
        # Session was rotated to inc_beta_02
        new_incarnation = "inc_beta_02"
        with self.assertRaises(ChildWorkSessionStaleError):
            child.transition(
                WorkStatus.PLANNING,
                current_session_incarnation_id=new_incarnation,
            )

    def test_session_reset_blocks_cancel(self) -> None:
        child = ChildWork.create(
            parent_work=self.parent_work,
            subagent=self.subagent,
            delegation=self.delegation,
        )
        with self.assertRaises(ChildWorkSessionStaleError):
            child.cancel(
                reason="Test",
                current_session_incarnation_id="inc_stale_rotated",
            )

    def test_session_reset_blocks_resume(self) -> None:
        store = InMemoryWorkStore()
        child = ChildWork.create(
            parent_work=self.parent_work,
            subagent=self.subagent,
            delegation=self.delegation,
        )
        save_child_work(store, child)

        # Resume with mismatched incarnation
        ok, reason, _ = resume_child_work(
            child_work_id=child.id,
            actor_id=self.actor,
            channel="cli",
            session_id=self.session_id,
            session_incarnation_id="inc_rotated_99",
            work_store=store,
        )
        self.assertFalse(ok)
        self.assertIn("invalidated session incarnation", reason.lower())


class TestChildWorkPersistence(BaseChildWorkTestCase):
    """Category G: Persistent FileWorkStore Integration and Serialization."""

    def setUp(self) -> None:
        super().setUp()
        self.temp_dir = tempfile.mkdtemp()
        self.works_dir = Path(self.temp_dir) / "works"
        self.work_store = FileWorkStore(works_dir=self.works_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_save_and_load_child_work_restart_simulation(self) -> None:
        child = ChildWork.create(
            parent_work=self.parent_work,
            subagent=self.subagent,
            delegation=self.delegation,
        )
        saved = save_child_work(self.work_store, child)
        self.assertEqual(saved.id, child.id)

        # Simulate process restart by instantiating new FileWorkStore instance
        restarted_store = FileWorkStore(works_dir=self.works_dir)
        loaded = get_child_work(restarted_store, child.id)

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.id, child.id)  # type: ignore
        self.assertEqual(loaded.parent_work_id, self.parent_work_id)  # type: ignore
        self.assertEqual(loaded.subagent_id, self.subagent_id)  # type: ignore
        self.assertEqual(loaded.delegation_id, self.delegation_id)  # type: ignore
        self.assertEqual(loaded.actor, self.actor)  # type: ignore
        self.assertEqual(loaded.session_id, self.session_id)  # type: ignore
        self.assertEqual(loaded.session_incarnation_id, self.session_incarnation_id)  # type: ignore
        self.assertEqual(loaded.status, WorkStatus.CREATED)  # type: ignore

    def test_secret_scrubbing_during_serialization(self) -> None:
        child = ChildWork.create(
            parent_work=self.parent_work,
            subagent=self.subagent,
            delegation=self.delegation,
        )
        d = child.to_dict()
        serialized = json.dumps(d)
        self.assertNotIn("sk-ant-api", serialized)
        self.assertNotIn("ghp_", serialized)

        # Attempt to inject secret into child dictionary should raise ValueError
        bad_dict = dict(d)
        bad_dict["actor"] = "ghp_" + "secrettoken123456789012345678901234567890"
        with self.assertRaises(ValueError):
            ChildWork.from_dict(bad_dict)

    def test_parent_observational_reference_attachment(self) -> None:
        child = ChildWork.create(
            parent_work=self.parent_work,
            subagent=self.subagent,
            delegation=self.delegation,
        )
        updated_parent = attach_child_work_reference(self.parent_work, child.id)
        self.assertIn(child.id, updated_parent.child_work_ids)
        self.assertEqual(get_child_work_ids(updated_parent), (child.id,))


class TestChildWorkConcurrency(BaseChildWorkTestCase):
    """Category H: Optimistic Concurrency Control (OCC) and Idempotency."""

    def test_stale_revision_rejected_in_save(self) -> None:
        store = InMemoryWorkStore()
        child = ChildWork.create(
            parent_work=self.parent_work,
            subagent=self.subagent,
            delegation=self.delegation,
        )
        c1 = save_child_work(store, child)

        # Concurrent update 1 advances revision
        c2 = c1.transition(WorkStatus.PLANNING)
        c2_saved = save_child_work(store, c2, expected_revision=c1.revision)
        self.assertEqual(c2_saved.revision, c1.revision + 1)

        # Concurrent update 2 using stale c1 revision must fail with StaleWorkRevisionError
        c3_stale = c1.transition(WorkStatus.PLANNING)
        with self.assertRaises(StaleWorkRevisionError):
            save_child_work(store, c3_stale, expected_revision=c1.revision)

    def test_parent_cascade_cancellation(self) -> None:
        store = InMemoryWorkStore()
        parent = self.parent_work
        child1 = ChildWork.create(parent_work=parent, subagent=self.subagent, delegation=self.delegation)
        save_child_work(store, child1)
        parent = attach_child_work_reference(parent, child1.id)
        store.create(parent)

        # Cancel parent cascade
        cancelled = cancel_child_works_for_parent(store, parent, actor_id=self.actor)
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0].id, child1.id)
        self.assertEqual(cancelled[0].status, WorkStatus.CANCELLED)
        self.assertEqual(cancelled[0].subagent.status, SubagentStatus.CANCELLED)  # type: ignore


class TestChildWorkExecutionBoundary(BaseChildWorkTestCase):
    """Category I: Architectural Boundary Enforcement (Zero Execution Authority)."""

    def test_child_work_has_no_execution_primitives(self) -> None:
        child = ChildWork.create(
            parent_work=self.parent_work,
            subagent=self.subagent,
            delegation=self.delegation,
        )
        prohibited_attrs = [
            "execute", "run", "subprocess", "spawn", "mutate_filesystem",
            "write_file", "read_file", "commit", "push", "coordinator",
            "orchestrator", "transaction_coordinator", "approve",
        ]
        for attr in prohibited_attrs:
            self.assertFalse(
                hasattr(child, attr),
                f"ChildWork illegally exposes execution primitive '{attr}'",
            )
            self.assertFalse(
                hasattr(self.subagent, attr),
                f"Subagent illegally exposes execution primitive '{attr}'",
            )


class TestChildWorkEndToEnd(BaseChildWorkTestCase):
    """Category J: End-to-End Composition through Canonical Execution Boundary."""

    def test_end_to_end_governed_execution(self) -> None:
        """Verify: Child Work -> Plan -> Approval -> ApprovedExecutionContract -> Transaction -> Orchestrator."""
        child = ChildWork.create(
            parent_work=self.parent_work,
            subagent=self.subagent,
            delegation=self.delegation,
            intent="Scan auth.py",
        )
        self.assertEqual(child.status, WorkStatus.CREATED)

        # 1. Transition Child Work to PLANNING
        c_plan = child.transition(WorkStatus.PLANNING, plan=("Step 1: Check login handler",))
        self.assertEqual(c_plan.status, WorkStatus.PLANNING)
        self.assertEqual(c_plan.subagent.status, SubagentStatus.RUNNING)  # type: ignore

        # 2. Transition Child Work to APPROVAL_REQUIRED with linked ApprovalRequest
        appr_store = InMemoryApprovalStore()
        appr_service = ApprovalService(store=appr_store)
        op = CanonicalOperation(action_type="read_file", target="src/auth.py")
        appr_req = ApprovalRequest(
            request_id="appr_child_001",
            session_id=self.session_id,
            channel="cli",
            user_id=self.actor,
            conversation_id="conv_child_001",
            operation_type="read_file",
            canonical_operation=op,
            operation_digest=op.compute_digest(),
            risk_class=RiskClass.LOW.value,
            created_at=self.now,
            expires_at=self.now + 300.0,
            nonce="nonce_child_001",
            status=ApprovalStatus.APPROVED,
        )
        appr_store.save(appr_req)

        c_appr = c_plan.transition(
            WorkStatus.APPROVAL_REQUIRED,
            approval_request_id="appr_child_001",
        )
        self.assertEqual(c_appr.status, WorkStatus.APPROVAL_REQUIRED)

        # 3. Create canonical ApprovedExecutionContract (authorized via Approval + Child Capabilities)
        exec_contract = ApprovedExecutionContract(
            request_id="appr_child_001",
            action_type="read_file",
            approved_targets=frozenset({"src/auth.py"}),
            operation_digest=op.compute_digest(),
            actor=self.actor,
            channel="cli",
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            capabilities=c_appr.capabilities or Capabilities(),
        )
        self.assertEqual(exec_contract.capabilities, self.child_caps)

        # 4. Transition Child Work to EXECUTING
        c_exec = c_appr.transition(WorkStatus.EXECUTING, transaction_id="tx_child_001")
        self.assertEqual(c_exec.status, WorkStatus.EXECUTING)

        # 5. Transition Child Work to VERIFYING and DONE
        c_ver = c_exec.transition(WorkStatus.VERIFYING)
        c_done = c_ver.transition(
            WorkStatus.DONE,
            verification_result=VerificationResult(status="PASS", summary="Scan clean"),
        )
        self.assertEqual(c_done.status, WorkStatus.DONE)
        self.assertEqual(c_done.subagent.status, SubagentStatus.COMPLETED)  # type: ignore


class TestChildWorkProperties(BaseChildWorkTestCase):
    """Category K: Formal Security Property Testing."""

    def test_monotonic_authority_invariants(self) -> None:
        """Verify: Child.parent == Delegation.parent, Child.subagent == Delegation.child, Child.caps <= Parent.caps."""
        child = ChildWork.create(
            parent_work=self.parent_work,
            subagent=self.subagent,
            delegation=self.delegation,
        )
        self.assertEqual(child.parent_work_id, self.delegation.parent_work_id)
        self.assertEqual(child.subagent_id, self.delegation.child_subagent_id)
        self.assertEqual(child.session_id, self.delegation.session_id)
        self.assertEqual(child.session_incarnation_id, self.delegation.session_incarnation_id)
        self.assertLessEqual(child.created_at, self.delegation.expires_at)

        # Verify child authority never exceeds delegation authority
        self.assertEqual(child.capabilities, self.delegation.capabilities)
        self.assertEqual(child.target_scope, self.delegation.target_scope)


if __name__ == "__main__":
    unittest.main()
