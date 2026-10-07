"""Formal Security Audit Regression Suite for Scoped Subagents (P1.3H Remediation).

Demonstrably verifies that all six findings are closed:
- SEC-P1.3-01: Approval bypass removal & strict ApprovedExecutionContract requirement
- SEC-P1.3-02: Authoritative session incarnation verification & stale incarnation rejection
- SEC-P1.3-03: Target scope escalation prevention & strict containment proofs
- SEC-P1.3-04: Unissued and tampered ApprovedExecutionContract rejection
- SEC-P1.3-05: ChildWork delegation persistence integrity & tampered contract fail-closed
- SEC-P1.3-06: Canonical session IDs with colons accepted while retaining path traversal rejection
- CROSS-FINDING: Subagent authority chain remains strictly monotonic end-to-end
"""
from __future__ import annotations

import inspect
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

from core.runtime.approval import ApprovalService, CanonicalOperation, InMemoryApprovalStore, RiskClass
from core.runtime.capabilities import (
    Capabilities,
    FilesystemPolicy,
    GitPolicy,
    NetworkPolicy,
    ShellPolicy,
)
from core.runtime.child_work import (
    ChildWork,
    ChildWorkBindingError,
    ChildWorkIntegrityError,
    get_child_work,
    save_child_work,
)
from core.runtime.contract import ApprovedExecutionContract
from core.runtime.delegation import (
    DelegationAttenuationError,
    DelegationContract,
    DelegationValidationError,
    is_path_subset,
)
from core.runtime.delegation_runtime import (
    DelegationBindingError,
    DelegationGroup,
    DelegationMode,
    DelegationRuntime,
    save_delegation_group,
)
from core.runtime.session import InMemorySessionStore, SessionManager, SessionState
from core.runtime.subagent import Subagent, SubagentStatus
from core.runtime.subagent_execution import (
    SubagentExecutionApprovalError,
    SubagentExecutionBindingError,
    SubagentExecutionCoordinator,
    SubagentExecutionRequest,
    SubagentExecutionResult,
    SubagentExecutionSessionStaleError,
)
from core.runtime.subagent_routing import (
    SubagentProfile,
    TaskRoutingRequest,
    TaskType,
    create_routed_delegation,
)
from core.runtime.transaction import InMemoryTransactionStore
from core.runtime.work import Work, WorkStatus
from core.runtime.work_store import FileWorkStore
from orchestrator import PlanStep, StepResult


class BaseSubagentSecurityTestCase(unittest.TestCase):
    """Shared fixture for P1.3H security audit regression verification."""

    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="brainfrog_sec_audit_"))
        self.repo_dir = self.temp_dir / "workspace"
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        self.store_dir = self.temp_dir / "work_store"
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.work_store = FileWorkStore(self.store_dir)

        self.actor = "user_lead"
        self.session_id = "cli:lead:default"
        self.session_incarnation_id = "inc_alpha_01"
        self.parent_work_id = "work_parent_001"
        self.now = time.time()

        # Parent work
        self.parent_caps = Capabilities(
            filesystem=FilesystemPolicy(read=("src/",), write=("src/",)),
            shell=ShellPolicy(execute=False),
            network=NetworkPolicy(access=False),
            git=GitPolicy(read=False, commit=False),
        )
        self.parent_work = Work(
            id=self.parent_work_id,
            intent="Root security audit task",
            goal="Harden agent authority",
            scope=("src/",),
            capabilities=self.parent_caps,
            status=WorkStatus.PLANNING,
            created_at=self.now,
            updated_at=self.now,
            actor_id=self.actor,
            channel="cli",
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
        )
        self.work_store.create(self.parent_work)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def issue_valid_contract(
        self,
        target_path: str = "src/auth.py",
        *,
        actor: Optional[str] = None,
        session_id: Optional[str] = None,
        session_incarnation_id: Optional[str] = None,
        channel: str = "cli",
    ) -> ApprovedExecutionContract:
        """Issue a real canonical ApprovedExecutionContract via ApprovalService."""
        caps = Capabilities(
            filesystem=FilesystemPolicy(read=(target_path,), write=(target_path,)),
            shell=ShellPolicy(execute=False),
            network=NetworkPolicy(access=False),
            git=GitPolicy(read=False, commit=False),
        )
        service = ApprovalService(InMemoryApprovalStore())
        action_type = "write_code"
        eff_actor = actor or self.actor
        eff_session = session_id or self.session_id
        eff_inc = session_incarnation_id or self.session_incarnation_id
        op = CanonicalOperation(action_type, target_path, {"targets": [target_path]})
        req = service.create_request(
            eff_session,
            channel,
            eff_actor,
            "chat",
            action_type,
            op,
            session_incarnation_id=eff_inc,
            capabilities=caps,
            workspace_root=str(self.repo_dir),
        )
        service.approve(req.request_id, "approver_lead", channel)
        service.verify_and_consume(
            req.request_id,
            req.operation_digest,
            eff_session,
            channel,
            session_incarnation_id=eff_inc,
            requester_id=eff_actor,
        )
        return ApprovedExecutionContract.from_approval_request(req, self.repo_dir)

    def create_child_hierarchy(
        self,
        child_id: str = "work_child_01",
        subagent_id: str = "sub_01",
        delegation_id: str = "delg_01",
        target_scope: tuple = ("src/auth.py",),
        delg_created_at: Optional[float] = None,
        child_created_at: Optional[float] = None,
    ) -> tuple[Subagent, DelegationContract, ChildWork]:
        """Create canonical subagent, delegation, and child work."""
        d_created = self.now if delg_created_at is None else float(delg_created_at)
        c_created = (self.now + 10.0) if child_created_at is None else float(child_created_at)

        sub = Subagent(
            subagent_id=subagent_id,
            parent_work_id=self.parent_work_id,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            actor=self.actor,
            role="coder",
            purpose="Subagent unit",
            created_at=d_created,
            updated_at=d_created,
        )
        delg = DelegationContract(
            delegation_id=delegation_id,
            parent_work_id=self.parent_work_id,
            child_subagent_id=subagent_id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            capabilities=Capabilities(
                filesystem=FilesystemPolicy(read=target_scope, write=target_scope),
                shell=ShellPolicy(execute=False),
                network=NetworkPolicy(access=False),
                git=GitPolicy(read=False, commit=False),
            ),
            target_scope=target_scope,
            expires_at=d_created + 600.0,
            created_at=d_created,
        )
        child = ChildWork.create(
            parent_work=self.parent_work,
            subagent=sub,
            delegation=delg,
            child_work_id=child_id,
            created_at=c_created,
        )
        save_child_work(self.work_store, child)
        return sub, delg, child


# =============================================================================
# Phase 1: SEC-P1.3-01 — Approval Bypass Remediation
# =============================================================================

class TestSecPhase1ApprovalBypass(BaseSubagentSecurityTestCase):
    """SEC-P1.3-01: Ensure no caller-controlled bypass exists and unapproved execution halts."""

    def test_execute_child_rejects_unapproved_execution_strictly(self) -> None:
        """Executing a child without an ApprovedExecutionContract must halt at APPROVAL_REQUIRED."""
        sub, delg, child = self.create_child_hierarchy()
        mock_runner = MagicMock()
        coordinator = SubagentExecutionCoordinator(
            work_store=self.work_store,
            repo_dir=self.repo_dir,
            orchestrator_runner=mock_runner,
        )
        req = SubagentExecutionRequest(
            parent_work_id=self.parent_work_id,
            child_work_id=child.id,
            subagent_id=sub.id,
            delegation_id=delg.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            task="Refactor auth",
            execution_contract=None,  # No approval contract
        )

        res = coordinator.execute_child(req)

        self.assertFalse(res.success)
        self.assertEqual(res.status, WorkStatus.APPROVAL_REQUIRED)
        self.assertFalse(mock_runner.called, "orchestrator_runner MUST NOT be called without approval")

        # Store status check
        stored = get_child_work(self.work_store, child.id)
        assert stored is not None
        self.assertEqual(stored.status, WorkStatus.APPROVAL_REQUIRED)

    def test_execute_group_rejects_unapproved_execution_strictly(self) -> None:
        """execute_group coordinate must not bypass approval when no contract is provided."""
        sub1, delg1, child1 = self.create_child_hierarchy("work_child_g1", "sub_g1", "delg_g1")
        sub2, delg2, child2 = self.create_child_hierarchy("work_child_g2", "sub_g2", "delg_g2")

        group = DelegationGroup(
            delegation_group_id="grp_sec_01",
            parent_work_id=self.parent_work_id,
            child_work_ids=(child1.id, child2.id),
            dependencies={},
            mode=DelegationMode.PARALLEL,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            actor=self.actor,
            created_at=self.now,
            updated_at=self.now,
        )
        save_delegation_group(self.work_store, group)

        mock_runner = MagicMock()
        coordinator = SubagentExecutionCoordinator(
            work_store=self.work_store,
            repo_dir=self.repo_dir,
            orchestrator_runner=mock_runner,
        )

        requests = {
            child1.id: SubagentExecutionRequest(
                parent_work_id=self.parent_work_id,
                child_work_id=child1.id,
                subagent_id=sub1.id,
                delegation_id=delg1.id,
                actor=self.actor,
                session_id=self.session_id,
                session_incarnation_id=self.session_incarnation_id,
                task="Task 1",
                execution_contract=None,
            ),
            child2.id: SubagentExecutionRequest(
                parent_work_id=self.parent_work_id,
                child_work_id=child2.id,
                subagent_id=sub2.id,
                delegation_id=delg2.id,
                actor=self.actor,
                session_id=self.session_id,
                session_incarnation_id=self.session_incarnation_id,
                task="Task 2",
                execution_contract=None,
            ),
        }

        agg, results = coordinator.execute_group(
            self.parent_work_id,
            group.delegation_group_id,
            requests,
        )

        self.assertFalse(mock_runner.called, "orchestrator_runner must not be called for unapproved group")
        for cid, r in results.items():
            self.assertEqual(r.status, WorkStatus.APPROVAL_REQUIRED)
            self.assertFalse(r.success)

    def test_no_approval_bypass_parameter_exists(self) -> None:
        """Verify require_approval_if_unapproved parameter does not exist on public APIs."""
        coordinator = SubagentExecutionCoordinator(work_store=self.work_store, repo_dir=self.repo_dir)
        child_sig = inspect.signature(coordinator.execute_child)
        group_sig = inspect.signature(coordinator.execute_group)

        self.assertNotIn(
            "require_approval_if_unapproved",
            child_sig.parameters,
            "execute_child must NOT have require_approval_if_unapproved parameter",
        )
        self.assertNotIn(
            "require_approval_if_unapproved",
            group_sig.parameters,
            "execute_group must NOT have require_approval_if_unapproved parameter",
        )


# =============================================================================
# Phase 2: SEC-P1.3-02 — Authoritative Session Incarnation
# =============================================================================

class TestSecPhase2SessionIncarnation(BaseSubagentSecurityTestCase):
    """SEC-P1.3-02: Ensure authoritative server session incarnation is enforced."""

    def test_coordinator_rejects_server_reset_session_incarnation(self) -> None:
        """Resetting session on server must reject requests with stale incarnation."""
        sessions = SessionManager(InMemorySessionStore())
        sess = sessions.get_or_create(self.session_id)
        inc_v1 = sess.session_incarnation_id

        # Update parent and create hierarchy under inc_v1
        self.parent_work = self.parent_work.with_update(session_incarnation_id=inc_v1)
        self.work_store.save(self.parent_work)
        self.session_incarnation_id = inc_v1

        sub, delg, child = self.create_child_hierarchy()

        # Server session resets: inc_v1 -> inc_v2
        sess.reset()
        sessions.save(sess)
        inc_v2 = sess.session_incarnation_id
        self.assertNotEqual(inc_v1, inc_v2)

        # Issue contract under inc_v1 (stale)
        contract_v1 = self.issue_valid_contract("src/auth.py", session_incarnation_id=inc_v1)
        req_stale = SubagentExecutionRequest(
            parent_work_id=self.parent_work_id,
            child_work_id=child.id,
            subagent_id=sub.id,
            delegation_id=delg.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=inc_v1,
            task="Task under stale session",
            execution_contract=contract_v1,
        )

        mock_runner = MagicMock()
        coordinator = SubagentExecutionCoordinator(
            work_store=self.work_store,
            repo_dir=self.repo_dir,
            session_manager=sessions,
            orchestrator_runner=mock_runner,
        )

        with self.assertRaises(SubagentExecutionSessionStaleError) as ctx:
            coordinator.execute_child(req_stale)

        self.assertIn("Session incarnation stale", str(ctx.exception))
        self.assertFalse(mock_runner.called, "Orchestrator runner must not be called after session reset")

    def test_current_session_incarnation_is_accepted(self) -> None:
        """Request matching the authoritative session incarnation must be accepted."""
        sessions = SessionManager(InMemorySessionStore())
        sess = sessions.get_or_create(self.session_id)
        current_inc = sess.session_incarnation_id

        # Update parent and create hierarchy matching current_inc
        self.parent_work = self.parent_work.with_update(session_incarnation_id=current_inc)
        self.work_store.save(self.parent_work)
        self.session_incarnation_id = current_inc

        sub, delg, child = self.create_child_hierarchy()
        contract = self.issue_valid_contract("src/auth.py", session_incarnation_id=current_inc)
        req = SubagentExecutionRequest(
            parent_work_id=self.parent_work_id,
            child_work_id=child.id,
            subagent_id=sub.id,
            delegation_id=delg.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=current_inc,
            task="Task under active session",
            execution_contract=contract,
        )

        mock_runner = MagicMock(return_value=[StepResult(PlanStep("1", "done", []), "opened_pr", 0, "OK")])
        coordinator = SubagentExecutionCoordinator(
            work_store=self.work_store,
            repo_dir=self.repo_dir,
            session_manager=sessions,
            orchestrator_runner=mock_runner,
        )

        res = coordinator.execute_child(req)
        self.assertTrue(res.success)
        self.assertEqual(res.status, WorkStatus.DONE)
        self.assertTrue(mock_runner.called)

    def test_client_cannot_override_authoritative_incarnation(self) -> None:
        """Even if child_work and request agree on a forged incarnation, server authority rejects."""
        sessions = SessionManager(InMemorySessionStore())
        sess = sessions.get_or_create(self.session_id)
        authoritative_inc = sess.session_incarnation_id

        # Adversary uses forged incarnation
        forged_inc = "forged_incarnation_999"
        self.parent_work = self.parent_work.with_update(session_incarnation_id=forged_inc)
        self.work_store.save(self.parent_work)
        self.session_incarnation_id = forged_inc
        sub, delg, child = self.create_child_hierarchy()

        contract = self.issue_valid_contract("src/auth.py", session_incarnation_id=forged_inc)
        req = SubagentExecutionRequest(
            parent_work_id=self.parent_work_id,
            child_work_id=child.id,
            subagent_id=sub.id,
            delegation_id=delg.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=forged_inc,
            task="Adversarial execution attempt",
            execution_contract=contract,
        )

        mock_runner = MagicMock()
        coordinator = SubagentExecutionCoordinator(
            work_store=self.work_store,
            repo_dir=self.repo_dir,
            session_manager=sessions,
            orchestrator_runner=mock_runner,
        )

        with self.assertRaises(SubagentExecutionSessionStaleError):
            coordinator.execute_child(req)

        self.assertFalse(mock_runner.called)


# =============================================================================
# Phase 3: SEC-P1.3-03 — Target Scope Escalation Prevention
# =============================================================================

class TestSecPhase3TargetScopeEscalation(BaseSubagentSecurityTestCase):
    """SEC-P1.3-03: Ensure target scopes cannot escape parent or profile containment."""

    def test_create_routed_delegation_rejects_unauthorized_target_scope(self) -> None:
        """Requesting targets outside parent authority must raise DelegationAttenuationError."""
        parent_caps = Capabilities(
            filesystem=FilesystemPolicy(read=("src/",), write=("src/",)),
            git=GitPolicy(read=False, commit=False),
        )
        profile = SubagentProfile(
            profile_id="coder",
            name="Coder",
            description="Software engineer",
            required_capabilities=Capabilities(
                filesystem=FilesystemPolicy(read=("src/",), write=("src/",)),
                git=GitPolicy(read=False, commit=False),
            ),
            supported_operations=("write_code",),
            supported_task_types=(TaskType.IMPLEMENTATION,),
            filesystem_scope=("src/**",),
        )
        # Attempt to target root-level file outside src/
        req = TaskRoutingRequest(
            task_id="task_sec_01",
            task_type=TaskType.IMPLEMENTATION,
            required_capabilities=Capabilities(),
            operation="write_code",
            target_scope=("unauthorized_target.py",),
        )

        with self.assertRaises(DelegationAttenuationError) as ctx:
            create_routed_delegation(
                delegation_id="delg_leak_01",
                parent_capabilities=parent_caps,
                profile=profile,
                request=req,
                parent_work_id=self.parent_work_id,
                child_subagent_id="sub_leak_01",
                actor=self.actor,
                session_id=self.session_id,
                session_incarnation_id=self.session_incarnation_id,
                expires_at=self.now + 600.0,
            )

        self.assertIn("exceeds parent filesystem scope", str(ctx.exception))

    def test_create_routed_delegation_accepts_target_inside_parent_scope(self) -> None:
        """Target strictly inside parent and profile scope succeeds."""
        parent_caps = Capabilities(
            filesystem=FilesystemPolicy(read=("src/",), write=("src/",)),
            git=GitPolicy(read=False, commit=False),
        )
        profile = SubagentProfile(
            profile_id="coder",
            name="Coder",
            description="Software engineer",
            required_capabilities=Capabilities(
                filesystem=FilesystemPolicy(read=("src/",), write=("src/",)),
                git=GitPolicy(read=False, commit=False),
            ),
            supported_operations=("write_code",),
            supported_task_types=(TaskType.IMPLEMENTATION,),
            filesystem_scope=("src/**",),
        )
        req = TaskRoutingRequest(
            task_id="task_sec_02",
            task_type=TaskType.IMPLEMENTATION,
            required_capabilities=Capabilities(
                filesystem=FilesystemPolicy(read=("src/foo.py",), write=("src/foo.py",)),
                git=GitPolicy(read=False, commit=False),
            ),
            operation="write_code",
            target_scope=("src/foo.py",),
        )

        delg = create_routed_delegation(
            delegation_id="delg_ok_01",
            parent_capabilities=parent_caps,
            profile=profile,
            request=req,
            parent_work_id=self.parent_work_id,
            child_subagent_id="sub_ok_01",
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            expires_at=self.now + 600.0,
        )

        self.assertEqual(delg.target_scope, ("src/foo.py",))

    def test_routed_target_scope_rejects_parent_escape(self) -> None:
        """Target containing path traversal syntax must be rejected fail-closed."""
        parent_caps = Capabilities(
            filesystem=FilesystemPolicy(read=("src/",), write=("src/",)),
            git=GitPolicy(read=False, commit=False),
        )
        profile = SubagentProfile(
            profile_id="coder",
            name="Coder",
            description="Software engineer",
            required_capabilities=Capabilities(
                filesystem=FilesystemPolicy(read=("src/",), write=("src/",)),
                git=GitPolicy(read=False, commit=False),
            ),
            supported_operations=("write_code",),
            supported_task_types=(TaskType.IMPLEMENTATION,),
            filesystem_scope=("src/**",),
        )
        # 1. Verification at TaskRoutingRequest boundary
        with self.assertRaises(DelegationAttenuationError) as ctx:
            TaskRoutingRequest(
                task_id="task_sec_03",
                task_type=TaskType.IMPLEMENTATION,
                required_capabilities=Capabilities(),
                operation="write_code",
                target_scope=("src/../secrets.txt",),
            )
        self.assertIn("traversal", str(ctx.exception).lower())

        # 2. Verification at create_routed_delegation attenuation boundary
        req = TaskRoutingRequest(
            task_id="task_sec_03",
            task_type=TaskType.IMPLEMENTATION,
            required_capabilities=Capabilities(),
            operation="write_code",
            target_scope=("src/foo.py",),
        )
        object.__setattr__(req, "target_scope", ("src/../secrets.txt",))

        with self.assertRaises(DelegationAttenuationError) as ctx2:
            create_routed_delegation(
                delegation_id="delg_escape_01",
                parent_capabilities=parent_caps,
                profile=profile,
                request=req,
                parent_work_id=self.parent_work_id,
                child_subagent_id="sub_escape_01",
                actor=self.actor,
                session_id=self.session_id,
                session_incarnation_id=self.session_incarnation_id,
                expires_at=self.now + 600.0,
            )

        self.assertIn("traversal", str(ctx2.exception).lower())

    def test_routed_target_scope_cannot_expand_parent_filesystem_scope(self) -> None:
        """Child cannot expand parent directory scope into full workspace wildcard '**'."""
        parent_caps = Capabilities(
            filesystem=FilesystemPolicy(read=("src/",), write=("src/",)),
            git=GitPolicy(read=False, commit=False),
        )
        profile = SubagentProfile(
            profile_id="admin_coder",
            name="Admin Coder",
            description="Broad profile",
            required_capabilities=Capabilities(
                filesystem=FilesystemPolicy(read=("src/",), write=("src/",)),
                git=GitPolicy(read=False, commit=False),
            ),
            supported_operations=("write_code",),
            supported_task_types=(TaskType.IMPLEMENTATION,),
            filesystem_scope=("**",),
        )
        req = TaskRoutingRequest(
            task_id="task_sec_04",
            task_type=TaskType.IMPLEMENTATION,
            required_capabilities=Capabilities(
                filesystem=FilesystemPolicy(read=("src/",), write=("src/",)),
                git=GitPolicy(read=False, commit=False),
            ),
            operation="write_code",
            target_scope=("**",),
        )

        with self.assertRaises(DelegationAttenuationError):
            create_routed_delegation(
                delegation_id="delg_wild_01",
                parent_capabilities=parent_caps,
                profile=profile,
                request=req,
                parent_work_id=self.parent_work_id,
                child_subagent_id="sub_wild_01",
                actor=self.actor,
                session_id=self.session_id,
                session_incarnation_id=self.session_incarnation_id,
                expires_at=self.now + 600.0,
            )

    def test_empty_capabilities_does_not_permit_arbitrary_targets(self) -> None:
        """Empty required_capabilities does NOT authorize uncontained targets."""
        parent_caps = Capabilities(
            filesystem=FilesystemPolicy(read=("src/",), write=("src/",)),
            git=GitPolicy(read=False, commit=False),
        )
        profile = SubagentProfile(
            profile_id="minimal",
            name="Minimal",
            description="Minimal profile",
            required_capabilities=Capabilities(),
            supported_operations=("write_code",),
            supported_task_types=(TaskType.IMPLEMENTATION,),
            filesystem_scope=("src/**",),
        )
        req = TaskRoutingRequest(
            task_id="task_sec_05",
            task_type=TaskType.IMPLEMENTATION,
            required_capabilities=Capabilities(),  # Empty!
            operation="write_code",
            target_scope=("config/passwords.json",),
        )

        with self.assertRaises(DelegationAttenuationError):
            create_routed_delegation(
                delegation_id="delg_empty_01",
                parent_capabilities=parent_caps,
                profile=profile,
                request=req,
                parent_work_id=self.parent_work_id,
                child_subagent_id="sub_empty_01",
                actor=self.actor,
                session_id=self.session_id,
                session_incarnation_id=self.session_incarnation_id,
                expires_at=self.now + 600.0,
            )


# =============================================================================
# Phase 4: SEC-P1.3-04 — Unissued ApprovedExecutionContract
# =============================================================================

class TestSecPhase4UnissuedContract(BaseSubagentSecurityTestCase):
    """SEC-P1.3-04: Ensure contracts without valid cryptographic issuance are strictly rejected."""

    def test_coordinator_rejects_unissued_contract(self) -> None:
        """Contract with empty authorization_json/digest must be rejected fail-closed."""
        sub, delg, child = self.create_child_hierarchy()
        unissued = ApprovedExecutionContract(
            request_id="appr_unissued_01",
            action_type="write_code",
            approved_targets=frozenset({"src/auth.py"}),
            operation_digest="op_digest_auth_01",
            channel="cli",
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            capabilities=delg.capabilities,
            authorization_json="",  # Empty!
            authorization_digest="",  # Empty!
            expires_at=self.now + 600.0,
        )

        req = SubagentExecutionRequest(
            parent_work_id=self.parent_work_id,
            child_work_id=child.id,
            subagent_id=sub.id,
            delegation_id=delg.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            task="Execute with unissued contract",
            execution_contract=unissued,
        )

        mock_runner = MagicMock()
        coordinator = SubagentExecutionCoordinator(
            work_store=self.work_store,
            repo_dir=self.repo_dir,
            orchestrator_runner=mock_runner,
        )

        with self.assertRaises(PermissionError) as ctx:
            coordinator.execute_child(req)

        self.assertIn("Unissued contract", str(ctx.exception))
        self.assertFalse(mock_runner.called)

    def test_tampered_authorization_json_is_rejected(self) -> None:
        """Tampering with authorization_json payload must fail cryptographic integrity."""
        sub, delg, child = self.create_child_hierarchy()
        valid_contract = self.issue_valid_contract("src/auth.py")

        # Tamper payload by modifying target
        payload = json.loads(valid_contract.authorization_json)
        payload["operation"]["target"] = "src/tampered.py"

        # 1. Direct instantiation with tampered JSON must fail integrity in __post_init__
        with self.assertRaises(ValueError) as ctx:
            ApprovedExecutionContract(
                request_id=valid_contract.request_id,
                action_type=valid_contract.action_type,
                approved_targets=valid_contract.approved_targets,
                operation_digest=valid_contract.operation_digest,
                channel=valid_contract.channel,
                actor=valid_contract.actor,
                session_id=valid_contract.session_id,
                session_incarnation_id=valid_contract.session_incarnation_id,
                capabilities=valid_contract.capabilities,
                workspace_root=valid_contract.workspace_root,
                authorization_json=json.dumps(payload),
                authorization_digest=valid_contract.authorization_digest,
                expires_at=valid_contract.expires_at,
            )
        self.assertIn("Contract integrity mismatch", str(ctx.exception))

        # 2. Mutating an existing contract must fail when validated by coordinator
        tampered_contract = self.issue_valid_contract("src/auth.py")
        object.__setattr__(tampered_contract, "authorization_json", json.dumps(payload))
        req = SubagentExecutionRequest(
            parent_work_id=self.parent_work_id,
            child_work_id=child.id,
            subagent_id=sub.id,
            delegation_id=delg.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            task="Tampered payload",
            execution_contract=tampered_contract,
        )
        coordinator = SubagentExecutionCoordinator(work_store=self.work_store, repo_dir=self.repo_dir)
        with self.assertRaises(ValueError) as ctx2:
            coordinator.execute_child(req)
        self.assertIn("Contract integrity mismatch", str(ctx2.exception))

    def test_tampered_authorization_digest_is_rejected(self) -> None:
        """Tampering with authorization_digest must fail verification."""
        sub, delg, child = self.create_child_hierarchy()
        valid_contract = self.issue_valid_contract("src/auth.py")

        # 1. Direct instantiation with tampered digest must fail integrity in __post_init__
        with self.assertRaises(ValueError) as ctx:
            ApprovedExecutionContract(
                request_id=valid_contract.request_id,
                action_type=valid_contract.action_type,
                approved_targets=valid_contract.approved_targets,
                operation_digest=valid_contract.operation_digest,
                channel=valid_contract.channel,
                actor=valid_contract.actor,
                session_id=valid_contract.session_id,
                session_incarnation_id=valid_contract.session_incarnation_id,
                capabilities=valid_contract.capabilities,
                workspace_root=valid_contract.workspace_root,
                authorization_json=valid_contract.authorization_json,
                authorization_digest="deadbeef" * 8,  # Tampered digest
                expires_at=valid_contract.expires_at,
            )
        self.assertIn("Contract integrity mismatch", str(ctx.exception))

        # 2. Mutating an existing contract must fail when validated by coordinator
        tampered_contract = self.issue_valid_contract("src/auth.py")
        object.__setattr__(tampered_contract, "authorization_digest", "deadbeef" * 8)
        req = SubagentExecutionRequest(
            parent_work_id=self.parent_work_id,
            child_work_id=child.id,
            subagent_id=sub.id,
            delegation_id=delg.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            task="Tampered digest",
            execution_contract=tampered_contract,
        )
        coordinator = SubagentExecutionCoordinator(work_store=self.work_store, repo_dir=self.repo_dir)
        with self.assertRaises(ValueError) as ctx2:
            coordinator.execute_child(req)
        self.assertIn("Contract integrity mismatch", str(ctx2.exception))

    def test_valid_issued_contract_is_accepted(self) -> None:
        """Legitimately issued and signed contract validates successfully."""
        contract = self.issue_valid_contract("src/auth.py")
        contract.validate(
            actor=self.actor,
            channel="cli",
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            repo_dir=self.repo_dir,
        )


# =============================================================================
# Phase 5: SEC-P1.3-05 — ChildWork Delegation Persistence
# =============================================================================

class TestSecPhase5DelegationPersistence(BaseSubagentSecurityTestCase):
    """SEC-P1.3-05: Ensure ChildWork reload reconstructs exact original delegation with matching digest."""

    def test_persisted_child_work_reconstruction_integrity(self) -> None:
        """Reconstructed ChildWork must preserve delegation created_at and identical digest."""
        t_delg = self.now - 200.0
        t_child = self.now - 100.0  # Intentionally different timestamps

        sub, delg, child = self.create_child_hierarchy(
            child_id="work_child_persist_01",
            delg_created_at=t_delg,
            child_created_at=t_child,
        )
        original_digest = delg.digest

        # Verify child work on disk
        loaded_cw = get_child_work(self.work_store, child.id)
        self.assertIsNotNone(loaded_cw)
        assert loaded_cw is not None

        # Must have reconstructed delegation
        self.assertIsNotNone(loaded_cw.delegation)
        assert loaded_cw.delegation is not None

        self.assertEqual(loaded_cw.delegation.created_at, t_delg)
        self.assertEqual(loaded_cw.created_at, t_child)
        self.assertEqual(loaded_cw.delegation.digest, original_digest)
        self.assertEqual(loaded_cw.delegation_digest, original_digest)

    def test_child_execution_survives_restart_with_same_delegation_digest(self) -> None:
        """ChildWork execution after a store reload executes under the exact original delegation."""
        t_delg = self.now - 200.0
        t_child = self.now - 100.0
        sub, delg, child = self.create_child_hierarchy(
            child_id="work_child_restart_01",
            delg_created_at=t_delg,
            child_created_at=t_child,
        )

        # Fresh work store pointing to same directory (simulating restart)
        restarted_store = FileWorkStore(self.store_dir)
        restarted_cw = get_child_work(restarted_store, child.id)
        self.assertIsNotNone(restarted_cw)
        assert restarted_cw is not None
        self.assertEqual(restarted_cw.delegation_digest, delg.digest)

        contract = self.issue_valid_contract("src/auth.py")
        req = SubagentExecutionRequest(
            parent_work_id=self.parent_work_id,
            child_work_id=restarted_cw.id,
            subagent_id=sub.id,
            delegation_id=delg.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=self.session_incarnation_id,
            task="Run after restart",
            execution_contract=contract,
        )

        mock_runner = MagicMock(return_value=[StepResult(PlanStep("1", "done", []), "opened_pr", 0, "OK")])
        coordinator = SubagentExecutionCoordinator(
            work_store=restarted_store,
            repo_dir=self.repo_dir,
            orchestrator_runner=mock_runner,
        )

        res = coordinator.execute_child(req)
        self.assertTrue(res.success)
        self.assertEqual(res.status, WorkStatus.DONE)

    def test_tampered_persisted_delegation_fails_closed(self) -> None:
        """Tampering with persisted delegation in the work store must fail closed on load."""
        sub, delg, child = self.create_child_hierarchy("work_child_tampered_01")

        # Directly tamper the JSON on disk at its canonical path
        work_file = self.work_store._get_work_path(child.id)
        self.assertTrue(work_file.exists())
        data = json.loads(work_file.read_text(encoding="utf-8"))

        # Modify delegation target_scope inside child_work metadata
        cw_meta = data["resume_metadata"]["child_work"]
        cw_meta["delegation"]["target_scope"] = ["src/evil_backdoor.py"]
        work_file.write_text(json.dumps(data), encoding="utf-8")

        # Attempt to load ChildWork from tampered store
        with self.assertRaises(ChildWorkIntegrityError) as ctx:
            get_child_work(self.work_store, child.id)

        self.assertIn("integrity violation", str(ctx.exception).lower())


# =============================================================================
# Phase 6: SEC-P1.3-06 — Canonical Session IDs
# =============================================================================

class TestSecPhase6CanonicalSessionIDs(BaseSubagentSecurityTestCase):
    """SEC-P1.3-06: Ensure colon-containing session IDs are permitted while path traversal is rejected."""

    def test_session_id_with_colons_accepted(self) -> None:
        """Colons in session IDs (e.g. cli:lead:default) must be accepted across all components."""
        colon_session = "cli:lead:default"

        # 1. Subagent
        sub = Subagent(
            subagent_id="sub_colon_01",
            parent_work_id=self.parent_work_id,
            session_id=colon_session,
            session_incarnation_id=self.session_incarnation_id,
            actor=self.actor,
            created_at=self.now,
            updated_at=self.now,
        )
        self.assertEqual(sub.session_id, colon_session)

        # 2. DelegationContract
        delg = DelegationContract(
            delegation_id="delg_colon_01",
            parent_work_id=self.parent_work_id,
            child_subagent_id=sub.id,
            actor=self.actor,
            session_id=colon_session,
            session_incarnation_id=self.session_incarnation_id,
            capabilities=self.parent_caps,
            target_scope=("src/auth.py",),
            expires_at=self.now + 600.0,
            created_at=self.now,
        )
        self.assertEqual(delg.session_id, colon_session)

        # 3. ChildWork
        child = ChildWork.create(
            parent_work=self.parent_work,
            subagent=sub,
            delegation=delg,
            child_work_id="work_child_colon_01",
            created_at=self.now,
        )
        self.assertEqual(child.session_id, colon_session)

        # 4. DelegationGroup
        grp = DelegationGroup(
            delegation_group_id="grp_colon_01",
            parent_work_id=self.parent_work_id,
            child_work_ids=(child.id,),
            dependencies={},
            mode=DelegationMode.SEQUENTIAL,
            session_id=colon_session,
            session_incarnation_id=self.session_incarnation_id,
            actor=self.actor,
            created_at=self.now,
            updated_at=self.now,
        )
        self.assertEqual(grp.session_id, colon_session)

        # 5. SubagentExecutionRequest
        req = SubagentExecutionRequest(
            parent_work_id=self.parent_work_id,
            child_work_id=child.id,
            subagent_id=sub.id,
            delegation_id=delg.id,
            actor=self.actor,
            session_id=colon_session,
            session_incarnation_id=self.session_incarnation_id,
            task="Colon session task",
        )
        self.assertEqual(req.session_id, colon_session)

    def test_session_id_path_traversal_still_rejected(self) -> None:
        """Session ID attempting path traversal must be strictly rejected."""
        for invalid_sess in ("../etc/passwd", "session/..", "..\\sess"):
            with self.subTest(invalid_sess=invalid_sess):
                with self.assertRaises((DelegationBindingError, ChildWorkBindingError, SubagentExecutionBindingError, ValueError)):
                    Subagent(
                        subagent_id="sub_bad_01",
                        parent_work_id=self.parent_work_id,
                        session_id=invalid_sess,
                        session_incarnation_id=self.session_incarnation_id,
                        actor=self.actor,
                    )

    def test_session_id_backslash_still_rejected(self) -> None:
        """Backslashes in session IDs must remain prohibited."""
        with self.assertRaises((DelegationBindingError, ChildWorkBindingError, SubagentExecutionBindingError, ValueError)):
            Subagent(
                subagent_id="sub_bad_02",
                parent_work_id=self.parent_work_id,
                session_id="session\\evil",
                session_incarnation_id=self.session_incarnation_id,
                actor=self.actor,
            )

    def test_session_id_control_characters_still_rejected(self) -> None:
        """Control characters and null bytes in session IDs must remain prohibited."""
        for invalid_sess in ("session\x00null", "session\nnewline", "session\rreturn"):
            with self.subTest(invalid_sess=invalid_sess):
                with self.assertRaises((DelegationBindingError, ChildWorkBindingError, SubagentExecutionBindingError, ValueError)):
                    Subagent(
                        subagent_id="sub_bad_03",
                        parent_work_id=self.parent_work_id,
                        session_id=invalid_sess,
                        session_incarnation_id=self.session_incarnation_id,
                        actor=self.actor,
                    )


# =============================================================================
# Cross-Finding: Monotonic Authority Chain
# =============================================================================

class TestCrossFindingMonotonicAuthorityChain(BaseSubagentSecurityTestCase):
    """End-to-end verification proving the entire authority chain remains monotonic."""

    def test_subagent_authority_chain_remains_monotonic(self) -> None:
        """Verify: Parent capability -> Attenuated Delegation -> ChildWork -> Approved Contract -> Transaction -> Orchestrator.

        At every stage:
        1. child authority <= parent authority
        2. no approval contract -> no execution
        3. stale incarnation -> no execution
        """
        sessions = SessionManager(InMemorySessionStore())
        sess = sessions.get_or_create(self.session_id)
        current_inc = sess.session_incarnation_id

        # Update parent work to current_inc
        self.parent_work = self.parent_work.with_update(session_incarnation_id=current_inc)
        self.work_store.save(self.parent_work)
        self.session_incarnation_id = current_inc

        # 1. Routing & attenuation
        profile = SubagentProfile(
            profile_id="security_coder",
            name="Security Coder",
            description="Specialized auth coder",
            required_capabilities=Capabilities(
                filesystem=FilesystemPolicy(read=("src/",), write=("src/",)),
                git=GitPolicy(read=False, commit=False),
            ),
            supported_operations=("write_code",),
            supported_task_types=(TaskType.IMPLEMENTATION,),
            filesystem_scope=("src/**",),
        )
        task_req = TaskRoutingRequest(
            task_id="task_mono_01",
            task_type=TaskType.IMPLEMENTATION,
            required_capabilities=Capabilities(
                filesystem=FilesystemPolicy(read=("src/auth.py",), write=("src/auth.py",)),
                git=GitPolicy(read=False, commit=False),
            ),
            operation="write_code",
            target_scope=("src/auth.py",),
        )

        delg = create_routed_delegation(
            delegation_id="delg_mono_01",
            parent_capabilities=self.parent_caps,
            profile=profile,
            request=task_req,
            parent_work_id=self.parent_work_id,
            child_subagent_id="sub_mono_01",
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=current_inc,
            expires_at=self.now + 600.0,
            created_at=self.now,
        )

        # Monotonicity check: child target scope ⊆ parent target scope
        for t in delg.target_scope:
            self.assertTrue(any(is_path_subset(t, pt) for pt in self.parent_caps.filesystem.write))

        # 2. ChildWork instantiation
        sub = Subagent(
            subagent_id="sub_mono_01",
            parent_work_id=self.parent_work_id,
            session_id=self.session_id,
            session_incarnation_id=current_inc,
            actor=self.actor,
            role=profile.name,
            purpose=profile.description,
            created_at=self.now,
            updated_at=self.now,
        )
        child = ChildWork.create(
            parent_work=self.parent_work,
            subagent=sub,
            delegation=delg,
            child_work_id="work_child_mono_01",
            created_at=self.now + 5.0,
        )
        save_child_work(self.work_store, child)

        # 3. Stage 1 Execution check: No approval contract -> MUST halt without execution
        mock_runner = MagicMock(return_value=[StepResult(PlanStep("1", "done", []), "opened_pr", 0, "OK")])
        tx_store = InMemoryTransactionStore()
        coordinator = SubagentExecutionCoordinator(
            work_store=self.work_store,
            repo_dir=self.repo_dir,
            session_manager=sessions,
            transaction_store=tx_store,
            orchestrator_runner=mock_runner,
        )

        req_unapproved = SubagentExecutionRequest(
            parent_work_id=self.parent_work_id,
            child_work_id=child.id,
            subagent_id=sub.id,
            delegation_id=delg.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=current_inc,
            task="Perform monotonic task",
            execution_contract=None,  # No contract
        )
        res_unapproved = coordinator.execute_child(req_unapproved)
        self.assertEqual(res_unapproved.status, WorkStatus.APPROVAL_REQUIRED)
        self.assertFalse(mock_runner.called)

        # 4. Stage 2 Execution check: Stale incarnation -> MUST raise error without execution
        contract_valid = self.issue_valid_contract("src/auth.py", session_incarnation_id=current_inc)
        sess.reset()
        sessions.save(sess)
        self.assertNotEqual(sess.session_incarnation_id, current_inc)

        req_stale = SubagentExecutionRequest(
            parent_work_id=self.parent_work_id,
            child_work_id=child.id,
            subagent_id=sub.id,
            delegation_id=delg.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=current_inc,  # Now stale!
            task="Perform monotonic task",
            execution_contract=contract_valid,
        )
        with self.assertRaises(SubagentExecutionSessionStaleError):
            coordinator.execute_child(req_stale)
        self.assertFalse(mock_runner.called)

        # 5. Stage 3 Execution check: Re-align with new incarnation -> succeeds through orchestrator
        new_inc = sess.session_incarnation_id
        # Update bindings to new_inc
        contract_new = self.issue_valid_contract("src/auth.py", session_incarnation_id=new_inc)
        sub_new = Subagent(
            subagent_id="sub_mono_02",
            parent_work_id=self.parent_work_id,
            session_id=self.session_id,
            session_incarnation_id=new_inc,
            actor=self.actor,
            created_at=self.now,
            updated_at=self.now,
        )
        delg_new = DelegationContract(
            delegation_id="delg_mono_02",
            parent_work_id=self.parent_work_id,
            child_subagent_id="sub_mono_02",
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=new_inc,
            capabilities=self.parent_caps,
            target_scope=("src/auth.py",),
            expires_at=self.now + 600.0,
            created_at=self.now,
        )
        self.parent_work = self.parent_work.with_update(session_incarnation_id=new_inc)
        self.work_store.save(self.parent_work)
        child_new = ChildWork.create(
            parent_work=self.parent_work,
            subagent=sub_new,
            delegation=delg_new,
            child_work_id="work_child_mono_02",
            created_at=self.now,
        )
        save_child_work(self.work_store, child_new)

        req_aligned = SubagentExecutionRequest(
            parent_work_id=self.parent_work_id,
            child_work_id=child_new.id,
            subagent_id=sub_new.id,
            delegation_id=delg_new.id,
            actor=self.actor,
            session_id=self.session_id,
            session_incarnation_id=new_inc,
            task="Perform monotonic task aligned",
            execution_contract=contract_new,
        )

        res_aligned = coordinator.execute_child(req_aligned)
        self.assertTrue(res_aligned.success)
        self.assertEqual(res_aligned.status, WorkStatus.DONE)
        self.assertTrue(mock_runner.called)


if __name__ == "__main__":
    unittest.main()
