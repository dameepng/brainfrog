"""Adversarial regression tests for Phase 2B: Plan-Bound Execution & Verification.

Enforces the 15 required safety invariants:
1. Plan selection alone cannot execute.
2. READY without a valid contract cannot execute.
3. A contract for another plan is rejected.
4. A contract for another workspace or session is rejected.
5. Markdown edits after approval invalidate the old contract.
6. Revision changes invalidate stale approval.
7. A stale VERIFIED plan is detected after an out-of-band edit.
8. Failed, missing, stale, or incomplete evidence cannot produce VERIFIED.
9. IMPLEMENTED does not imply VERIFIED.
10. An invalid transition cannot bypass lifecycle validation.
11. Concurrent updates do not silently overwrite newer revisions.
12. Malformed metadata cannot authorize execution.
13. Every existing execution entry point uses the canonical authorization boundary.
14. Rejected authorization produces no workspace mutation.
15. Failure and rollback preserve unrelated pre-existing user changes.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.plans import (
    ALLOWED_PLAN_TRANSITIONS,
    InvalidPlanTransitionError,
    PlanDocument,
    PlanRegistry,
    PlanStatus,
    StalePlanStateError,
    VerificationCheckResult,
    VerificationEvidence,
    save_plan_document,
)
from core.runtime.approval import (
    ApprovalRequest,
    ApprovalService,
    ApprovalStatus,
    CanonicalOperation,
    InMemoryApprovalStore,
    RiskClass,
)
from core.runtime.capabilities import Capabilities, FilesystemPolicy, NetworkPolicy
from core.runtime.contract import ApprovedExecutionContract
from core.runtime.messages import IncomingMessage
from core.runtime.runtime import BrainFrogRuntime
from orchestrator import (
    Orchestrator,
    PlanStep,
    RunConfig,
    _write_files,
    validate_and_stage_files,
)
from system1.base import Answer, SystemOneClient


# -----------------------------------------------------------------------------
# Synthetic Test Doubles
# -----------------------------------------------------------------------------
class MockSystem1(SystemOneClient):
    name: str = "mock_s1"

    def decide(self, state: Dict[str, Any], questions: Dict[str, Any]) -> Dict[str, Answer]:
        return {
            k: Answer(choice="unrelated", score=0.1, noul=1.0, confidence=0.9)
            for k in questions
        }


class MockSystem2:
    def __init__(self, output_files: Optional[Dict[str, str]] = None) -> None:
        self.guidelines = ""
        self.provider_name = "mock_s2"
        self.model = "mock-model"
        self.output_files = output_files or {"src/safe.py": "# synthetic implementation"}

    def plan_task(self, task: str, focus_tree: str = "", **kwargs: Any) -> List[PlanStep]:
        return [PlanStep(id="1", description="Execute plan step", files=list(self.output_files.keys()))]

    def write_code(self, step: PlanStep, task: str, file_contents: Dict[str, str], **kwargs: Any) -> Dict[str, str]:
        return dict(self.output_files)

    def draft_pr(self, task: str, files_changed: List[str], test_summary: str = "", **kwargs: Any) -> Dict[str, str]:
        return {"title": f"feat: {task}", "body": "Synthetic PR"}


# -----------------------------------------------------------------------------
# Phase 2B Test Suite
# -----------------------------------------------------------------------------
class TestPhase2BPlanBoundExecution(unittest.TestCase):
    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="brainfrog_phase2b_")
        self.workspace = Path(self.test_dir).resolve()
        (self.workspace / ".brainfrog" / "plans").mkdir(parents=True, exist_ok=True)
        (self.workspace / "src").mkdir(parents=True, exist_ok=True)

        # Initialize mock git repo
        try:
            subprocess.run(["git", "init"], cwd=self.workspace, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "TestRunner"], cwd=self.workspace, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@synthetic.local"], cwd=self.workspace, check=True, capture_output=True)
            # Create synthetic initial commit
            init_file = self.workspace / "README.md"
            init_file.write_text("# Synthetic Workspace\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=self.workspace, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=self.workspace, check=True, capture_output=True)
        except Exception:
            pass

        self.registry = PlanRegistry(self.workspace)

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_sample_plan(
        self,
        plan_id: str = "plan_auth_01",
        title: str = "Auth Hardening Plan",
        goal: str = "Implement plan-bound execution",
        status: PlanStatus = PlanStatus.DRAFT,
        verification_commands: Optional[List[str]] = None,
        session_id: str = "sess_canonical_1",
    ) -> Path:
        content = f"# {title}\n- **Goal**: {goal}\n\n## Steps\n1. Add safe checks\n2. Verify state\n"
        meta = {
            "id": plan_id,
            "title": title,
            "goal": goal,
            "status": status.value,
            "revision": 1,
            "session_id": session_id,
            "steps": [{"id": 1, "description": "Add safe checks"}],
            "acceptance_criteria": ["Criteria 1", "Criteria 2"],
            "verification_commands": verification_commands or [
                {"criterion_id": "Criteria 1", "command": "python --version"},
                {"criterion_id": "Criteria 2", "command": "python --version"},
            ],
        }
        return save_plan_document(
            repo_dir=self.workspace,
            filename=f"{plan_id}.md",
            content=content,
            metadata=meta,
        )

    def _create_approved_contract(
        self,
        plan_doc: PlanDocument,
        workspace: Optional[Path] = None,
        session_id: str = "sess_canonical_1",
        session_incarnation: str = "inc_canonical_1",
        targets: Optional[List[str]] = None,
    ) -> ApprovedExecutionContract:
        ws = workspace or self.workspace
        approved_targets = targets or ["src/safe.py"]
        op = CanonicalOperation(
            action_type="write_files",
            target=approved_targets[0],
            parameters={"files": approved_targets},
        )
        caps = Capabilities(
            filesystem=FilesystemPolicy(read=tuple(approved_targets), write=tuple(approved_targets)),
            network=NetworkPolicy(access=True),
        )

        app_service = ApprovalService(store=InMemoryApprovalStore())
        app_req = app_service.create_request(
            session_id=session_id,
            channel="cli",
            user_id="local_user",
            conversation_id="conv_default",
            operation_type="write_files",
            canonical_operation=op,
            risk_class=RiskClass.MEDIUM.value,
            session_incarnation_id=session_incarnation,
            capabilities=caps,
            workspace_root=str(ws),
            plan_id=plan_doc.id,
            plan_revision=plan_doc.revision,
            plan_content_hash=plan_doc.content_hash,
        )
        app_req.status = ApprovalStatus.CONSUMED
        app_req.approver_id = "security_lead"
        app_req.approved_at = time.time()
        app_req.consumed_at = time.time()
        app_req.approval_digest = app_req.compute_approval_digest()
        return ApprovedExecutionContract.from_approval_request(app_req, repo_dir=ws)

    # -------------------------------------------------------------------------
    # Invariant 1: Plan selection alone cannot execute
    # -------------------------------------------------------------------------
    def test_plan_selection_alone_cannot_execute(self) -> None:
        """Selecting a plan sets session focus but does NOT authorize execution without contract."""
        self._create_sample_plan(plan_id="plan_sel_01")
        runtime = BrainFrogRuntime(
            repo_dir=self.workspace,
            require_approval=True,
            system1_factory=lambda _: MockSystem1(),
            system2_factory=lambda **_: MockSystem2(),
        )

        # Select the plan
        sel_msg = IncomingMessage(text="/plan select plan_sel_01", channel="cli", user_id="local", conversation_id="conv1")
        sel_resp = runtime.handle_message(sel_msg)
        self.assertTrue(sel_resp.success)
        self.assertIn("Plan selected", sel_resp.text)
        self.assertIn("does not authorize execution", sel_resp.text)

        # Attempt to write code without approved contract
        mutate_msg = IncomingMessage(
            text="write file src/unauthorized.py with content test",
            channel="cli",
            user_id="local",
            conversation_id="conv1",
        )
        mut_resp = runtime.handle_message(mutate_msg)
        self.assertFalse(mut_resp.success)
        self.assertFalse((self.workspace / "src" / "unauthorized.py").exists())

    # -------------------------------------------------------------------------
    # Invariant 2: READY without a valid contract cannot execute
    # -------------------------------------------------------------------------
    def test_ready_without_contract_cannot_execute(self) -> None:
        """A plan marked READY cannot execute or transition to IN_PROGRESS without valid contract."""
        self._create_sample_plan(plan_id="plan_ready_01", status=PlanStatus.DRAFT)
        plan = self.registry.update_status("plan_ready_01", PlanStatus.READY, actor="user", reason="Ready for execution")
        self.assertEqual(plan.status, PlanStatus.READY)

        # 1. Attempt to transition to IN_PROGRESS without contract
        with self.assertRaises(InvalidPlanTransitionError):
            self.registry.update_status(
                "plan_ready_01",
                PlanStatus.IN_PROGRESS,
                actor="user",
                reason="Starting execution without contract",
            )

        # 2. Attempt to transition to IN_PROGRESS with mismatched contract
        self._create_sample_plan(plan_id="other_plan")
        other_plan = self.registry.get_plan("other_plan")
        assert other_plan is not None
        other_contract = self._create_approved_contract(other_plan)
        with self.assertRaises(PermissionError):
            self.registry.update_status(
                "plan_ready_01",
                PlanStatus.IN_PROGRESS,
                actor="user",
                reason="Starting execution with mismatched contract",
                contract=other_contract,
            )

    # -------------------------------------------------------------------------
    # Invariant 3: A contract for another plan is rejected
    # -------------------------------------------------------------------------
    def test_contract_for_another_plan_is_rejected(self) -> None:
        """ApprovedExecutionContract minted for plan_A cannot be used for plan_B."""
        self._create_sample_plan(plan_id="plan_A")
        self._create_sample_plan(plan_id="plan_B")
        plan_a = self.registry.get_plan("plan_A")
        assert plan_a is not None

        contract_a = self._create_approved_contract(plan_a)

        # Validating contract_a against plan_B must raise PermissionError
        with self.assertRaises(PermissionError) as ctx:
            contract_a.validate(
                actor="local_user",
                channel="cli",
                session_id="sess_canonical_1",
                session_incarnation_id="inc_canonical_1",
                repo_dir=self.workspace,
                expected_plan_id="plan_B",
            )
        self.assertIn("plan mismatch", str(ctx.exception).lower())

    # -------------------------------------------------------------------------
    # Invariant 4: A contract for another workspace or session is rejected
    # -------------------------------------------------------------------------
    def test_contract_for_another_workspace_or_session_is_rejected(self) -> None:
        """Contract cannot cross workspace or session boundaries."""
        self._create_sample_plan(plan_id="plan_bound_01")
        plan = self.registry.get_plan("plan_bound_01")
        assert plan is not None

        contract = self._create_approved_contract(plan)

        # Other workspace
        other_ws = Path(tempfile.mkdtemp(prefix="other_ws_")).resolve()
        try:
            with self.assertRaises(PermissionError):
                contract.validate(
                    actor="local_user",
                    channel="cli",
                    session_id="sess_canonical_1",
                    session_incarnation_id="inc_canonical_1",
                    repo_dir=other_ws,
                )
        finally:
            shutil.rmtree(other_ws, ignore_errors=True)

        # Other session
        with self.assertRaises(PermissionError):
            contract.validate(
                actor="local_user",
                channel="cli",
                session_id="sess_alien_999",
                session_incarnation_id="inc_canonical_1",
                repo_dir=self.workspace,
            )

    # -------------------------------------------------------------------------
    # Invariant 5: Markdown edits after approval invalidate old contract
    # -------------------------------------------------------------------------
    def test_markdown_edits_after_approval_invalidate_old_contract(self) -> None:
        """Modifying the plan markdown out-of-band after contract creation invalidates contract."""
        self._create_sample_plan(plan_id="plan_tamper_01")
        plan = self.registry.get_plan("plan_tamper_01")
        assert plan is not None

        contract = self._create_approved_contract(plan)
        # Contract is initially valid
        contract.validate(
            actor="local_user",
            channel="cli",
            session_id="sess_canonical_1",
            session_incarnation_id="inc_canonical_1",
            repo_dir=self.workspace,
            expected_plan_id="plan_tamper_01",
        )

        # Out-of-band edit to Markdown
        plan.path.write_text("# Malicious Tamper\nInjecting backdoor step\n", encoding="utf-8")

        # Validation must now fail closed by recomputing disk fingerprint
        with self.assertRaises(PermissionError) as ctx:
            contract.validate(
                actor="local_user",
                channel="cli",
                session_id="sess_canonical_1",
                session_incarnation_id="inc_canonical_1",
                repo_dir=self.workspace,
                expected_plan_id="plan_tamper_01",
            )
        self.assertIn("modified", str(ctx.exception).lower())

    # -------------------------------------------------------------------------
    # Invariant 6: Revision changes invalidate stale approval
    # -------------------------------------------------------------------------
    def test_revision_changes_invalidate_stale_approval(self) -> None:
        """A contract created for revision 1 is rejected if the plan revision is bumped."""
        self._create_sample_plan(plan_id="plan_rev_01")
        plan = self.registry.get_plan("plan_rev_01")
        assert plan is not None

        contract = self._create_approved_contract(plan)  # Bound to revision 1

        # Bump plan revision in workspace
        self.registry.update_status("plan_rev_01", PlanStatus.READY, actor="user", reason="Bumped to ready")
        updated_plan = self.registry.get_plan("plan_rev_01")
        assert updated_plan is not None
        self.assertEqual(updated_plan.revision, 2)

        # Old contract must be rejected
        with self.assertRaises(PermissionError) as ctx:
            contract.validate(
                actor="local_user",
                channel="cli",
                session_id="sess_canonical_1",
                session_incarnation_id="inc_canonical_1",
                repo_dir=self.workspace,
                expected_plan_id="plan_rev_01",
            )
        self.assertIn("revision", str(ctx.exception).lower())

    # -------------------------------------------------------------------------
    # Invariant 7: Stale VERIFIED plan is detected after out-of-band edit
    # -------------------------------------------------------------------------
    def test_stale_verified_plan_detected_after_out_of_band_edit(self) -> None:
        """A plan marked VERIFIED that suffers an out-of-band edit loses VERIFIED status."""
        self._create_sample_plan(plan_id="plan_ver_01", status=PlanStatus.DRAFT)
        self.registry.update_status("plan_ver_01", PlanStatus.READY, actor="user", reason="Ready")
        plan = self.registry.get_plan("plan_ver_01")
        assert plan is not None
        contract = self._create_approved_contract(plan)
        self.registry.update_status("plan_ver_01", PlanStatus.IN_PROGRESS, actor="user", reason="Started", contract=contract)
        self.registry.update_status("plan_ver_01", PlanStatus.IMPLEMENTED, actor="user", reason="Implemented")

        # Run verification to legitimately reach VERIFIED
        passed, ev, v_plan = self.registry.verify_plan("plan_ver_01", actor="verifier")
        self.assertTrue(passed)
        self.assertEqual(v_plan.status, PlanStatus.VERIFIED)

        # Out-of-band edit to Markdown
        v_plan.path.write_text("# Altered after verification\nUnauthorized change\n", encoding="utf-8")

        # Loading must detect tampering and demote from VERIFIED
        reloaded = self.registry.get_plan("plan_ver_01")
        assert reloaded is not None
        self.assertTrue(reloaded.content_tampered)
        self.assertEqual(reloaded.status, PlanStatus.DRAFT)

        # list_plans must also demote it
        summaries = self.registry.list_plans()
        target_s = next(s for s in summaries if s.id == "plan_ver_01")
        self.assertTrue(target_s.content_tampered)
        self.assertEqual(target_s.status, PlanStatus.DRAFT)

    # -------------------------------------------------------------------------
    # Invariant 8: Failed, missing, stale, or incomplete evidence cannot produce VERIFIED
    # -------------------------------------------------------------------------
    def test_failed_missing_stale_or_incomplete_evidence_cannot_produce_verified(self) -> None:
        """Transition to VERIFIED strictly fails without valid, passing VerificationEvidence."""
        self._create_sample_plan(plan_id="plan_ev_01", status=PlanStatus.DRAFT)
        self.registry.update_status("plan_ev_01", PlanStatus.READY, actor="user", reason="Ready")
        plan = self.registry.get_plan("plan_ev_01")
        assert plan is not None
        contract = self._create_approved_contract(plan)
        self.registry.update_status("plan_ev_01", PlanStatus.IN_PROGRESS, actor="user", reason="Started", contract=contract)
        self.registry.update_status("plan_ev_01", PlanStatus.IMPLEMENTED, actor="user", reason="Implemented")
        plan = self.registry.get_plan("plan_ev_01")
        assert plan is not None

        # 1. Missing evidence
        with self.assertRaises(InvalidPlanTransitionError):
            self.registry.update_status("plan_ev_01", PlanStatus.VERIFIED, actor="verifier", reason="No evidence", evidence=None)

        # 2. Failed check
        failed_ev = VerificationEvidence(
            plan_id=plan.id,
            plan_revision=plan.revision,
            plan_content_hash=plan.content_hash,
            checks=(
                VerificationCheckResult(check_name="test_suite", command="pytest", status="failed"),
            ),
        )
        with self.assertRaises(InvalidPlanTransitionError):
            self.registry.update_status("plan_ev_01", PlanStatus.VERIFIED, actor="verifier", reason="Failed check", evidence=failed_ev.to_dict())

        # 3. Mismatched plan_id
        alien_ev = VerificationEvidence(
            plan_id="alien_plan",
            plan_revision=plan.revision,
            plan_content_hash=plan.content_hash,
            checks=(
                VerificationCheckResult(check_name="test_suite", command="pytest", status="passed"),
            ),
        )
        with self.assertRaises(InvalidPlanTransitionError):
            self.registry.update_status("plan_ev_01", PlanStatus.VERIFIED, actor="verifier", reason="Wrong plan", evidence=alien_ev.to_dict())

        # 4. Mismatched revision
        stale_rev_ev = VerificationEvidence(
            plan_id=plan.id,
            plan_revision=999,
            plan_content_hash=plan.content_hash,
            checks=(
                VerificationCheckResult(check_name="test_suite", command="pytest", status="passed"),
            ),
        )
        with self.assertRaises(InvalidPlanTransitionError):
            self.registry.update_status("plan_ev_01", PlanStatus.VERIFIED, actor="verifier", reason="Stale rev", evidence=stale_rev_ev.to_dict())

    # -------------------------------------------------------------------------
    # Invariant 9: IMPLEMENTED does not imply VERIFIED
    # -------------------------------------------------------------------------
    def test_implemented_does_not_imply_verified(self) -> None:
        """IMPLEMENTED and VERIFIED are semantically distinct states."""
        self._create_sample_plan(plan_id="plan_impl_01", status=PlanStatus.DRAFT)
        self.registry.update_status("plan_impl_01", PlanStatus.READY, actor="user", reason="Ready")
        plan = self.registry.get_plan("plan_impl_01")
        assert plan is not None
        contract = self._create_approved_contract(plan)
        self.registry.update_status("plan_impl_01", PlanStatus.IN_PROGRESS, actor="user", reason="In progress", contract=contract)
        plan = self.registry.update_status("plan_impl_01", PlanStatus.IMPLEMENTED, actor="user", reason="Executed")

        self.assertEqual(plan.status, PlanStatus.IMPLEMENTED)
        self.assertNotEqual(plan.status, PlanStatus.VERIFIED)
        self.assertTrue(plan.is_verifiable)
        self.assertFalse(plan.is_executable)

    # -------------------------------------------------------------------------
    # Invariant 10: An invalid transition cannot bypass lifecycle validation
    # -------------------------------------------------------------------------
    def test_invalid_transition_cannot_bypass_lifecycle(self) -> None:
        """Direct illegal transitions are rejected fail-closed."""
        self._create_sample_plan(plan_id="plan_life_01", status=PlanStatus.DRAFT)

        # DRAFT -> IMPLEMENTED is illegal
        with self.assertRaises(InvalidPlanTransitionError):
            self.registry.update_status("plan_life_01", PlanStatus.IMPLEMENTED, actor="user", reason="Skip")

        # DRAFT -> VERIFIED is illegal
        with self.assertRaises(InvalidPlanTransitionError):
            self.registry.update_status("plan_life_01", PlanStatus.VERIFIED, actor="user", reason="Skip")

    # -------------------------------------------------------------------------
    # Invariant 11: Concurrent updates do not silently overwrite newer revisions
    # -------------------------------------------------------------------------
    def test_concurrent_updates_do_not_silently_overwrite_newer_revisions(self) -> None:
        """Optimistic concurrency control rejects stale revision updates."""
        self._create_sample_plan(plan_id="plan_occ_01", status=PlanStatus.DRAFT)
        p1 = self.registry.get_plan("plan_occ_01")
        assert p1 is not None
        self.assertEqual(p1.revision, 1)

        # Actor B updates revision to 2
        self.registry.update_status("plan_occ_01", PlanStatus.READY, actor="actor_B", reason="Ready")

        # Actor A attempts update with stale expected_revision=1
        with self.assertRaises(StalePlanStateError):
            self.registry.update_status(
                "plan_occ_01",
                PlanStatus.SUPERSEDED,
                actor="actor_A",
                reason="Stale update",
                expected_revision=1,
            )

    # -------------------------------------------------------------------------
    # Invariant 12: Malformed metadata cannot authorize execution
    # -------------------------------------------------------------------------
    def test_malformed_metadata_cannot_authorize_execution(self) -> None:
        """Corrupt sidecar metadata prevents plan from reaching READY or authorizing execution."""
        plan_path = self._create_sample_plan(plan_id="plan_malform_01", status=PlanStatus.DRAFT)
        meta_path = plan_path.with_suffix(".meta.json")
        meta_path.write_text("{ corrupt json: [", encoding="utf-8")

        plan = self.registry.get_plan("plan_malform_01")
        assert plan is not None
        self.assertTrue(plan.metadata_malformed)

        # Cannot mark as READY
        with self.assertRaises(InvalidPlanTransitionError):
            self.registry.update_status("plan_malform_01", PlanStatus.READY, actor="user", reason="Ready")

    # -------------------------------------------------------------------------
    # Invariant 13: Every existing execution entry point uses canonical authorization boundary
    # -------------------------------------------------------------------------
    def test_every_execution_entry_point_uses_canonical_authorization_boundary(self) -> None:
        """Direct invocation of orchestrator staging and file writes rejects tampered plan contracts."""
        self._create_sample_plan(plan_id="plan_entry_01")
        plan = self.registry.get_plan("plan_entry_01")
        assert plan is not None

        contract = self._create_approved_contract(plan)

        # Tamper plan markdown
        plan.path.write_text("# Tampered Markdown\n", encoding="utf-8")

        # 1. validate_and_stage_files must fail closed
        with self.assertRaises(PermissionError):
            validate_and_stage_files(
                repo_dir=self.workspace,
                files={"src/safe.py": "content"},
                contract=contract,
                plan_id=plan.id,
            )

        # 2. _write_files must fail closed
        with self.assertRaises(PermissionError):
            _write_files(
                files={"src/safe.py": "content"},
                repo_dir=self.workspace,
                contract=contract,
                plan_id=plan.id,
            )

    # -------------------------------------------------------------------------
    # Invariant 14: Rejected authorization produces no workspace mutation
    # -------------------------------------------------------------------------
    def test_rejected_authorization_produces_no_workspace_mutation(self) -> None:
        """When plan validation fails, zero files are created or modified."""
        self._create_sample_plan(plan_id="plan_nomut_01")
        plan = self.registry.get_plan("plan_nomut_01")
        assert plan is not None

        contract = self._create_approved_contract(plan)
        # Invalidate by tampering
        plan.path.write_text("# Altered\n", encoding="utf-8")

        target_file = self.workspace / "src" / "should_not_exist.py"
        self.assertFalse(target_file.exists())

        try:
            validate_and_stage_files(
                repo_dir=self.workspace,
                files={"src/should_not_exist.py": "print('exploit')"},
                contract=contract,
                plan_id=plan.id,
            )
        except PermissionError:
            pass

        self.assertFalse(target_file.exists())

    # -------------------------------------------------------------------------
    # Invariant 15: Failure and rollback preserve unrelated pre-existing user changes
    # -------------------------------------------------------------------------
    def test_failure_and_rollback_preserve_unrelated_pre_existing_user_changes(self) -> None:
        """Transaction rollback does not destroy or revert unrelated uncommitted user files."""
        # User has an untracked, uncommitted file
        user_uncommitted = self.workspace / "my_precious_notes.txt"
        user_uncommitted.write_text("Secret user notes that must never be lost\n", encoding="utf-8")

        # User also modified an existing tracked file
        readme = self.workspace / "README.md"
        readme.write_text("# Synthetic Workspace (User Draft Edit)\n", encoding="utf-8")

        self._create_sample_plan(plan_id="plan_rollback_01", status=PlanStatus.READY)
        plan = self.registry.get_plan("plan_rollback_01")
        assert plan is not None
        contract = self._create_approved_contract(plan, targets=["src/fail.py"])

        # Create orchestrator configured to fail tests
        cfg = RunConfig(
            repo_dir=self.workspace,
            task="Simulate failure",
            execution_contract=contract,
            plan_id=plan.id,
            actor="local_user",
            session_id="sess_canonical_1",
            session_incarnation_id="inc_canonical_1",
            test_command=["python", "-c", "import sys; sys.exit(1)"],  # Force test failure & rollback
        )
        orch = Orchestrator(
            system1=MockSystem1(),
            system2=MockSystem2(output_files={"src/fail.py": "# failing code"}),
            config=cfg,
        )

        with self.assertRaises(RuntimeError):
            orch.run()

        # Verify rollback occurred for the target file
        self.assertFalse((self.workspace / "src" / "fail.py").exists())

        # CRITICAL INVARIANT: Pre-existing user changes remain completely intact!
        self.assertTrue(user_uncommitted.exists())
        self.assertEqual(user_uncommitted.read_text(encoding="utf-8"), "Secret user notes that must never be lost\n")
        self.assertEqual(readme.read_text(encoding="utf-8"), "# Synthetic Workspace (User Draft Edit)\n")


if __name__ == "__main__":
    unittest.main()
