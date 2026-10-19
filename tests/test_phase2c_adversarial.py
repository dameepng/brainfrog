"""BrainFrog Phase 2C Adversarial, Reliability & Fail-Closed Regression Suite.

Validates the Phase 2C product invariants:
1. Workstream A: Fail-Closed Lifecycle Start (Audit finding BF-2B-01).
   - READY -> IN_PROGRESS failure rejects execution before staging or mutations.
   - Zero partial writes; workspace remains byte-identical.
   - Stale contracts cannot proceed.
2. Workstream B: Durable Plan Registry Write-Ahead Journal & Idempotent Recovery.
   - Interrupted writes at each boundary (.journal, .md, .meta.json) recover safely.
   - Idempotent recovery.
   - Recovery never promotes unprovenanced plans or clobbers newer revisions.
   - Corrupt journals fail closed.
3. Workstream C: Plan Identity Binding at Every Execution Boundary (Audit finding BF-2B-03).
   - Cross-plan contract mismatches fail closed at runtime, orchestrator, staging, and write boundaries.
4. Workstream D: Explicit Verification Evidence Mapping (Audit finding BF-2B-02).
   - 100% acceptance criteria coverage strictly enforced.
   - Missing, dangling, unknown, or unmapped criteria fail closed.
5. Workstream E: Lifecycle Actor Provenance (Audit finding BF-2B-04).
   - Actor labels are strictly audit metadata and cannot grant execution or verification authority.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

from core.plans import (
    CriterionVerificationMapping,
    InvalidPlanTransitionError,
    PlanDocument,
    PlanJournalEntry,
    PlanNotFoundError,
    PlanRegistry,
    PlanStatus,
    StalePlanStateError,
    VerificationCheckResult,
    VerificationEvidence,
    get_journal_dir,
    get_plans_dir,
    recover_plan_journal,
    save_plan_document,
    set_wal_fault_hook,
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
from core.runtime.session import SessionManager
from orchestrator import (
    Orchestrator,
    PlanStep,
    RunConfig,
    _write_files,
    validate_and_stage_files,
)
from system1.base import Answer, SystemOneClient


# -----------------------------------------------------------------------------
# Synthetic Test Doubles (Adhering to Safe Test Fixtures Rule)
# -----------------------------------------------------------------------------
class SyntheticSystem1(SystemOneClient):
    name: str = "synthetic_s1"

    def decide(self, state: Dict[str, Any], questions: Dict[str, Any]) -> Dict[str, Answer]:
        return {
            k: Answer(choice="unrelated", score=0.1, noul=1.0, confidence=0.9)
            for k in questions
        }


class SyntheticSystem2:
    def __init__(self, output_files: Optional[Dict[str, str]] = None) -> None:
        self.guidelines = ""
        self.provider_name = "synthetic_s2"
        self.model = "synthetic-model"
        self.output_files = output_files or {"src/safe_module.py": "# synthetic safe code\n"}

    def plan_task(self, task: str, focus_tree: str = "", **kwargs: Any) -> List[PlanStep]:
        return [PlanStep(id="1", description="Safe execution", files=list(self.output_files.keys()))]

    def write_code(self, step: PlanStep, task: str, file_contents: Dict[str, str], **kwargs: Any) -> Dict[str, str]:
        return dict(self.output_files)

    def draft_pr(self, task: str, files_changed: List[str], test_summary: str = "", **kwargs: Any) -> Dict[str, str]:
        return {"title": f"feat: {task}", "body": "Synthetic PR"}


# -----------------------------------------------------------------------------
# Phase 2C Adversarial Test Suite
# -----------------------------------------------------------------------------
class TestPhase2CAdversarialHardening(unittest.TestCase):
    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="brainfrog_phase2c_")
        self.workspace = Path(self.test_dir).resolve()
        (self.workspace / ".brainfrog" / "plans").mkdir(parents=True, exist_ok=True)
        (self.workspace / "src").mkdir(parents=True, exist_ok=True)

        # Initialize mock git workspace
        try:
            subprocess.run(["git", "init"], cwd=self.workspace, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "SyntheticTester"], cwd=self.workspace, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "tester@synthetic.local"], cwd=self.workspace, check=True, capture_output=True)
            init_file = self.workspace / "README.md"
            init_file.write_text("# Synthetic Workspace\nInitial clean state.\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=self.workspace, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=self.workspace, check=True, capture_output=True)
        except Exception:
            pass

        self.registry = PlanRegistry(self.workspace)

    def tearDown(self) -> None:
        set_wal_fault_hook(None)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_sample_plan(
        self,
        plan_id: str = "plan_p2c_01",
        title: str = "Reliability Plan",
        goal: str = "Verify fail-closed and crash recovery",
        status: PlanStatus = PlanStatus.DRAFT,
        acceptance_criteria: Optional[List[str]] = None,
        verification_commands: Optional[List[Any]] = None,
        session_id: str = "cli:local:default",
    ) -> Path:
        content = f"# {title}\n- **Goal**: {goal}\n\n## Steps\n1. Check invariants\n2. Reconcile\n"
        crit = acceptance_criteria or ["Criterion A", "Criterion B"]
        cmds = verification_commands or [
            {"criterion_id": "Criterion A", "command": "python --version"},
            {"criterion_id": "Criterion B", "command": "python --version"},
        ]
        meta = {
            "id": plan_id,
            "title": title,
            "goal": goal,
            "status": status.value,
            "revision": 1,
            "session_id": session_id,
            "steps": [{"id": 1, "description": "Check invariants"}],
            "acceptance_criteria": crit,
            "verification_commands": cmds,
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
        session_id: str = "cli:local:default",
        session_incarnation: str = "inc_p2c_1",
        targets: Optional[List[str]] = None,
    ) -> ApprovedExecutionContract:
        ws = workspace or self.workspace
        approved_targets = targets or ["src/safe_module.py"]
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

    def _register_approved_request(
        self,
        runtime: BrainFrogRuntime,
        plan_doc: PlanDocument,
        channel: str = "cli",
        user_id: str = "local",
        conversation_id: str = "default",
        targets: Optional[List[str]] = None,
    ) -> Any:
        session = runtime.sessions.get_or_create(channel, user_id, conversation_id)
        approved_targets = targets or ["src/safe_module.py"]
        op = CanonicalOperation(
            action_type="write_files",
            target=approved_targets[0],
            parameters={"files": approved_targets},
        )
        caps = Capabilities(
            filesystem=FilesystemPolicy(read=tuple(approved_targets), write=tuple(approved_targets)),
            network=NetworkPolicy(access=True),
        )
        app_req = runtime.approval_service.create_request(
            session_id=session.session_id,
            channel=channel,
            user_id=user_id,
            conversation_id=conversation_id,
            operation_type="write_files",
            canonical_operation=op,
            risk_class=RiskClass.MEDIUM.value,
            session_incarnation_id=session.session_incarnation_id,
            capabilities=caps,
            workspace_root=str(self.workspace),
            plan_id=plan_doc.id,
            plan_revision=plan_doc.revision,
            plan_content_hash=plan_doc.content_hash,
        )
        app_req.status = ApprovalStatus.APPROVED
        app_req.approver_id = "security_lead"
        app_req.approved_at = time.time()
        app_req.approval_digest = app_req.compute_approval_digest()
        runtime.approval_service.store.save(app_req)
        return app_req

    # =========================================================================
    # WORKSTREAM A: Fail-Closed Lifecycle Start (Audit finding BF-2B-01)
    # =========================================================================
    def test_failed_ready_to_in_progress_transition_rejects_execution_cleanly(self) -> None:
        """A plan-bound execution fails closed if READY -> IN_PROGRESS transition fails.

        Proves:
        1. Execution is rejected immediately.
        2. No target files are written.
        3. Pre-existing files and user changes remain byte-identical.
        """
        self._create_sample_plan("plan_fail_start", status=PlanStatus.DRAFT)
        self.registry.update_status("plan_fail_start", PlanStatus.READY, actor="user", reason="Ready")
        plan = self.registry.get_plan("plan_fail_start")
        assert plan is not None

        # Create an unrelated user file in the workspace
        unrelated = self.workspace / "src" / "unrelated_user_work.py"
        unrelated_content = "# user code before execution\nx = 42\n"
        unrelated.write_text(unrelated_content, encoding="utf-8")

        runtime = BrainFrogRuntime(
            repo_dir=self.workspace,
            persist_sessions=False,
            sessions=SessionManager(),
            system1_factory=lambda *args, **kwargs: SyntheticSystem1(),
            system2_factory=lambda *args, **kwargs: SyntheticSystem2(output_files={"src/safe_module.py": "# newly generated\n"}),
        )
        app_req = self._register_approved_request(runtime, plan, targets=["src/safe_module.py"])

        # Force the lifecycle transition to fail (simulate lock contention / I/O error)
        with patch("core.plans.PlanRegistry.update_status", side_effect=IOError("Simulated disk error")):
            msg = IncomingMessage(
                text=f"/exec {app_req.request_id}",
                channel="cli",
                user_id="local",
                conversation_id="default",
            )
            response = runtime.handle_message(msg)

        # Must be rejected fail-closed
        self.assertEqual(response.status, "rejected")
        self.assertFalse(response.success)
        self.assertIn("Simulated disk error", response.error)

        # Target file must NOT be written
        target_path = self.workspace / "src" / "safe_module.py"
        self.assertFalse(target_path.exists())

        # Unrelated user changes must remain byte-identical
        self.assertEqual(unrelated.read_text(encoding="utf-8"), unrelated_content)

        # Plan status must remain READY (never silently marked IN_PROGRESS)
        reloaded_plan = self.registry.get_plan("plan_fail_start")
        assert reloaded_plan is not None
        self.assertEqual(reloaded_plan.status, PlanStatus.READY)

    def test_stale_contract_cannot_proceed_if_transition_fails(self) -> None:
        """A stale contract rejected at the lifecycle boundary cannot proceed to mutations."""
        self._create_sample_plan("plan_stale_start", status=PlanStatus.DRAFT)
        self.registry.update_status("plan_stale_start", PlanStatus.READY, actor="user", reason="Ready")
        plan = self.registry.get_plan("plan_stale_start")
        assert plan is not None

        runtime = BrainFrogRuntime(
            repo_dir=self.workspace,
            persist_sessions=False,
            sessions=SessionManager(),
            system1_factory=lambda *args, **kwargs: SyntheticSystem1(),
            system2_factory=lambda *args, **kwargs: SyntheticSystem2(output_files={"src/safe_module.py": "# generated\n"}),
        )
        app_req = self._register_approved_request(runtime, plan)

        # Stale the plan revision out-of-band by marking it BLOCKED then READY again
        self.registry.update_status("plan_stale_start", PlanStatus.BLOCKED, actor="user", reason="Blocked")
        self.registry.update_status("plan_stale_start", PlanStatus.READY, actor="user", reason="Re-ready")
        updated_plan = self.registry.get_plan("plan_stale_start")
        assert updated_plan is not None
        self.assertEqual(updated_plan.revision, 4)

        msg = IncomingMessage(
            text=f"/exec {app_req.request_id}",
            channel="cli",
            user_id="local",
            conversation_id="default",
        )
        response = runtime.handle_message(msg)

        # Fails closed on stale revision
        self.assertEqual(response.status, "rejected")
        self.assertFalse(response.success)
        self.assertFalse((self.workspace / "src" / "safe_module.py").exists())

    def test_valid_approved_execution_succeeds(self) -> None:
        """When lifecycle transition to IN_PROGRESS succeeds, authorized mutations proceed."""
        self._create_sample_plan("plan_valid_start", status=PlanStatus.DRAFT)
        self.registry.update_status("plan_valid_start", PlanStatus.READY, actor="user", reason="Ready")
        plan = self.registry.get_plan("plan_valid_start")
        assert plan is not None

        runtime = BrainFrogRuntime(
            repo_dir=self.workspace,
            persist_sessions=False,
            sessions=SessionManager(),
            system1_factory=lambda *args, **kwargs: SyntheticSystem1(),
            system2_factory=lambda *args, **kwargs: SyntheticSystem2(output_files={"src/safe_module.py": "# successfully written\n"}),
        )
        app_req = self._register_approved_request(runtime, plan, targets=["src/safe_module.py"])

        msg = IncomingMessage(
            text=f"/exec {app_req.request_id}",
            channel="cli",
            user_id="local",
            conversation_id="default",
        )
        response = runtime.handle_message(msg)

        self.assertEqual(response.status, "completed")
        self.assertTrue(response.success)
        target_path = self.workspace / "src" / "safe_module.py"
        self.assertTrue(target_path.exists())
        self.assertEqual(target_path.read_text(encoding="utf-8"), "# successfully written\n")

        # Plan transitioned to IN_PROGRESS upon start, then IMPLEMENTED upon step completion
        reloaded = self.registry.get_plan("plan_valid_start")
        assert reloaded is not None
        self.assertEqual(reloaded.status, PlanStatus.IMPLEMENTED)

    # =========================================================================
    # WORKSTREAM B: Durable Plan Registry Journal and Recovery
    # =========================================================================
    def test_wal_journal_recovery_after_crash_before_md_write(self) -> None:
        """Interruption after journal write (PREPARED) before .md write aborts creation cleanly."""
        journal_dir = get_journal_dir(self.workspace)
        journal_entry = PlanJournalEntry(
            journal_id="wal_test_abort_01",
            plan_id="plan_aborted_creation",
            filename="plan_aborted_creation.md",
            stage="PREPARED",
            created_at=datetime.now(timezone.utc).isoformat(),
            target_revision=1,
            target_status="draft",
            new_md_hash="fake_hash",
            new_meta_dict={"id": "plan_aborted_creation", "revision": 1, "status": "draft"},
            has_md_update=True,
            new_md_content="# Aborted Plan\n",
            previous_md_content=None,
            previous_meta_dict=None,
        )
        j_path = journal_dir / "wal_test_abort_01.json"
        j_path.write_text(json.dumps(journal_entry.to_dict()), encoding="utf-8")

        # Before recovery: journal exists, plan does not
        self.assertTrue(j_path.exists())

        # Perform recovery
        actions = recover_plan_journal(self.workspace)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "aborted_creation")

        # Journal purged, no partial files left
        self.assertFalse(j_path.exists())
        self.assertIsNone(self.registry.get_plan("plan_aborted_creation"))

    def test_wal_journal_recovery_after_crash_before_meta_write(self) -> None:
        """Interruption after .md was updated but before .meta.json was updated rolls back to previous snapshot."""
        # Create initial valid plan
        self._create_sample_plan("plan_wal_rb", title="Original Title", status=PlanStatus.DRAFT)
        plan = self.registry.get_plan("plan_wal_rb")
        assert plan is not None
        original_md = plan.path.read_text(encoding="utf-8")
        original_meta = plan.to_metadata_dict()

        # Simulate interrupted update:
        # 1. Write journal record with previous snapshot
        journal_dir = get_journal_dir(self.workspace)
        journal_entry = PlanJournalEntry(
            journal_id="wal_test_rb_01",
            plan_id="plan_wal_rb",
            filename=plan.filename,
            stage="PREPARED",
            created_at=datetime.now(timezone.utc).isoformat(),
            expected_revision=1,
            target_revision=2,
            target_status="ready",
            new_md_hash="corrupt_or_partial_hash",
            new_meta_dict={"id": "plan_wal_rb", "revision": 2, "status": "ready"},
            has_md_update=True,
            new_md_content="# Partial Mutated Content\n",
            previous_md_content=original_md,
            previous_meta_dict=original_meta,
        )
        j_path = journal_dir / "wal_test_rb_01.json"
        j_path.write_text(json.dumps(journal_entry.to_dict()), encoding="utf-8")

        # 2. Simulate partial write: md was overwritten, but meta was not
        plan.path.write_text("# Partial Mutated Content\n", encoding="utf-8")

        # Recovery must roll back both .md and .meta.json to the original snapshot
        actions = recover_plan_journal(self.workspace)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "rolled_back")

        # Plan content restored to original
        reloaded = self.registry.get_plan("plan_wal_rb")
        assert reloaded is not None
        self.assertEqual(reloaded.content, original_md)
        self.assertEqual(reloaded.revision, 1)
        self.assertEqual(reloaded.status, PlanStatus.DRAFT)
        self.assertFalse(j_path.exists())

    def test_wal_journal_recovery_after_crash_after_meta_write_before_journal_delete(self) -> None:
        """Interruption after both .md and .meta.json were replaced confirms committed state and purges journal."""
        self._create_sample_plan("plan_wal_commit", title="Committed Plan", status=PlanStatus.READY)
        plan = self.registry.get_plan("plan_wal_commit")
        assert plan is not None

        # Simulate journal remaining after successful write
        journal_dir = get_journal_dir(self.workspace)
        journal_entry = PlanJournalEntry(
            journal_id="wal_test_commit_01",
            plan_id="plan_wal_commit",
            filename=plan.filename,
            stage="PREPARED",
            created_at=datetime.now(timezone.utc).isoformat(),
            expected_revision=1,
            target_revision=1,
            target_status="ready",
            new_md_hash=plan.content_hash,
            new_meta_dict=plan.to_metadata_dict(),
            has_md_update=True,
            new_md_content=plan.content,
        )
        j_path = journal_dir / "wal_test_commit_01.json"
        j_path.write_text(json.dumps(journal_entry.to_dict()), encoding="utf-8")

        actions = recover_plan_journal(self.workspace)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "confirmed_committed")
        self.assertFalse(j_path.exists())

    def test_repeated_recovery_idempotence(self) -> None:
        """Repeated invocations of recover_plan_journal are safe and idempotent."""
        self._create_sample_plan("plan_idempotent", status=PlanStatus.DRAFT)
        # First call on clean workspace
        actions1 = recover_plan_journal(self.workspace)
        self.assertEqual(actions1, [])

        # Second call
        actions2 = recover_plan_journal(self.workspace)
        self.assertEqual(actions2, [])

    def test_recovery_does_not_clobber_newer_concurrent_update(self) -> None:
        """Recovery never rolls back a plan that has already committed a newer revision."""
        self._create_sample_plan("plan_newer_rev", status=PlanStatus.DRAFT)
        self.registry.update_status("plan_newer_rev", PlanStatus.READY, actor="user", reason="Bump rev")
        plan = self.registry.get_plan("plan_newer_rev")
        assert plan is not None
        self.assertEqual(plan.revision, 2)

        # Stale journal for revision 1 -> 2
        journal_dir = get_journal_dir(self.workspace)
        stale_journal = PlanJournalEntry(
            journal_id="wal_stale_rev_01",
            plan_id="plan_newer_rev",
            filename=plan.filename,
            stage="PREPARED",
            created_at=datetime.now(timezone.utc).isoformat(),
            expected_revision=0,
            target_revision=1,  # Older than current disk revision (2)
            target_status="draft",
            new_md_hash="old_hash",
            new_meta_dict={"id": "plan_newer_rev", "revision": 1, "status": "draft"},
            has_md_update=False,
            previous_meta_dict={"id": "plan_newer_rev", "revision": 0, "status": "draft"},
        )
        j_path = journal_dir / "wal_stale_rev_01.json"
        j_path.write_text(json.dumps(stale_journal.to_dict()), encoding="utf-8")

        actions = recover_plan_journal(self.workspace)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "discarded_stale_journal")

        # Plan remains at revision 2, READY
        reloaded = self.registry.get_plan("plan_newer_rev")
        assert reloaded is not None
        self.assertEqual(reloaded.revision, 2)
        self.assertEqual(reloaded.status, PlanStatus.READY)

    def test_corrupt_journal_fails_closed(self) -> None:
        """A malformed/corrupt journal file is quarantined and does not crash registry operations."""
        journal_dir = get_journal_dir(self.workspace)
        corrupt_j = journal_dir / "wal_corrupt.json"
        corrupt_j.write_text("{ unparseable json @@ invalid", encoding="utf-8")

        actions = recover_plan_journal(self.workspace)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "quarantined_corrupt")
        self.assertFalse(corrupt_j.exists())
        self.assertTrue((journal_dir / "wal_corrupt.json.corrupt").exists())

    # =========================================================================
    # WORKSTREAM C: Plan Identity Binding at Every Execution Boundary (Audit finding BF-2B-03)
    # =========================================================================
    def test_cross_plan_contract_mismatch_rejected_at_runtime(self) -> None:
        """Adversarial execution passing Contract(Plan A) while targeting Plan B is rejected at runtime."""
        self._create_sample_plan("plan_A", status=PlanStatus.READY)
        self._create_sample_plan("plan_B", status=PlanStatus.READY)
        plan_a = self.registry.get_plan("plan_A")
        assert plan_a is not None

        runtime = BrainFrogRuntime(
            repo_dir=self.workspace,
            persist_sessions=False,
            sessions=SessionManager(),
            system1_factory=lambda *args, **kwargs: SyntheticSystem1(),
            system2_factory=lambda *args, **kwargs: SyntheticSystem2(),
        )
        app_req = self._register_approved_request(runtime, plan_a)

        # Select plan_B in session
        session = runtime.sessions.get_or_create("cli", "local", "default")
        session.selected_plan_id = "plan_B"
        runtime.sessions.save(session)

        msg = IncomingMessage(
            text=f"/exec {app_req.request_id}",
            channel="cli",
            user_id="local",
            conversation_id="default",
        )
        response = runtime.handle_message(msg)

        # Runtime detects mismatch between session selected plan (plan_B) and contract (plan_A)
        self.assertEqual(response.status, "rejected")
        self.assertFalse(response.success)
        self.assertIn("plan mismatch", response.error.lower())
        self.assertFalse((self.workspace / "src" / "safe_module.py").exists())

    def test_cross_plan_contract_mismatch_rejected_at_orchestrator(self) -> None:
        """Adversarial attempt to run Orchestrator with contract for Plan A and cfg.plan_id = Plan B fails closed."""
        self._create_sample_plan("plan_A", status=PlanStatus.READY)
        self._create_sample_plan("plan_B", status=PlanStatus.READY)
        plan_a = self.registry.get_plan("plan_A")
        assert plan_a is not None

        contract_a = self._create_approved_contract(plan_a)

        cfg = RunConfig(
            repo_dir=self.workspace,
            task="Malicious cross-plan run",
            plan_id="plan_B",  # Cross-plan adversarial injection!
            session_id=contract_a.session_id,
            session_incarnation_id=contract_a.session_incarnation_id,
            actor="local_user",
            origin_channel="cli",
            execution_contract=contract_a,
        )
        orch = Orchestrator(
            system1=SyntheticSystem1(),
            system2=SyntheticSystem2(),
            config=cfg,
        )

        with self.assertRaises(PermissionError) as ctx:
            orch.run()
        self.assertIn("plan mismatch", str(ctx.exception).lower())
        self.assertFalse((self.workspace / "src" / "safe_module.py").exists())

    def test_cross_plan_contract_mismatch_rejected_at_validate_and_stage_files(self) -> None:
        """validate_and_stage_files rejects contract for Plan A when expected_plan_id = Plan B."""
        self._create_sample_plan("plan_A", status=PlanStatus.READY)
        plan_a = self.registry.get_plan("plan_A")
        assert plan_a is not None
        contract_a = self._create_approved_contract(plan_a)

        with self.assertRaises(PermissionError) as ctx:
            validate_and_stage_files(
                self.workspace,
                {"src/safe_module.py": "# test\n"},
                contract=contract_a,
                plan_id="plan_B",  # Mismatched!
            )
        self.assertIn("plan mismatch", str(ctx.exception).lower())

    def test_cross_plan_contract_mismatch_rejected_at_write_files(self) -> None:
        """_write_files rejects contract for Plan A when plan_id = Plan B."""
        self._create_sample_plan("plan_A", status=PlanStatus.READY)
        plan_a = self.registry.get_plan("plan_A")
        assert plan_a is not None
        contract_a = self._create_approved_contract(plan_a)

        with self.assertRaises(PermissionError) as ctx:
            _write_files(
                self.workspace,
                {"src/safe_module.py": "# test\n"},
                mode="build",
                contract=contract_a,
                plan_id="plan_B",  # Mismatched!
            )
        self.assertIn("plan mismatch", str(ctx.exception).lower())
        self.assertFalse((self.workspace / "src" / "safe_module.py").exists())

    # =========================================================================
    # WORKSTREAM D: Explicit Verification Evidence Mapping (Audit finding BF-2B-02)
    # =========================================================================
    def test_verification_complete_coverage_succeeds(self) -> None:
        """Explicit 100% criteria coverage promotes IMPLEMENTED plan to VERIFIED."""
        self._create_sample_plan(
            "plan_cov_ok",
            status=PlanStatus.DRAFT,
            acceptance_criteria=["Criterion 1", "Criterion 2"],
            verification_commands=[
                {"criterion_id": "Criterion 1", "command": "python --version"},
                {"criterion_id": "Criterion 2", "command": "python --version"},
            ],
        )
        self.registry.update_status("plan_cov_ok", PlanStatus.READY, actor="user", reason="Ready")
        plan = self.registry.get_plan("plan_cov_ok")
        assert plan is not None
        contract = self._create_approved_contract(plan)
        self.registry.update_status("plan_cov_ok", PlanStatus.IN_PROGRESS, actor="user", reason="Start", contract=contract)
        self.registry.update_status("plan_cov_ok", PlanStatus.IMPLEMENTED, actor="user", reason="Impl done")

        passed, ev, v_plan = self.registry.verify_plan("plan_cov_ok", actor="verifier")
        self.assertTrue(passed)
        self.assertEqual(v_plan.status, PlanStatus.VERIFIED)
        self.assertEqual(len(ev.checks), 2)
        self.assertEqual(ev.checks[0].criterion_ref, "Criterion 1")
        self.assertEqual(ev.checks[1].criterion_ref, "Criterion 2")
        self.assertTrue(ev.checks[0].check_id.startswith("chk_"))

    def test_verification_incomplete_coverage_rejected(self) -> None:
        """Incomplete acceptance criteria coverage fails closed and rejects verification."""
        self._create_sample_plan(
            "plan_cov_inc",
            status=PlanStatus.DRAFT,
            acceptance_criteria=["Crit Alpha", "Crit Beta"],
            verification_commands=[
                {"criterion_id": "Crit Alpha", "command": "python --version"},
                # Crit Beta is missing!
            ],
        )
        self.registry.update_status("plan_cov_inc", PlanStatus.READY, actor="user", reason="Ready")
        plan = self.registry.get_plan("plan_cov_inc")
        assert plan is not None
        contract = self._create_approved_contract(plan)
        self.registry.update_status("plan_cov_inc", PlanStatus.IN_PROGRESS, actor="user", reason="Start", contract=contract)
        self.registry.update_status("plan_cov_inc", PlanStatus.IMPLEMENTED, actor="user", reason="Impl done")

        with self.assertRaises(InvalidPlanTransitionError) as ctx:
            self.registry.verify_plan("plan_cov_inc", actor="verifier")
        self.assertIn("Incomplete verification coverage", str(ctx.exception))
        self.assertIn("Crit Beta", str(ctx.exception))

        # Plan status must remain IMPLEMENTED, not VERIFIED
        reloaded = self.registry.get_plan("plan_cov_inc")
        assert reloaded is not None
        self.assertEqual(reloaded.status, PlanStatus.IMPLEMENTED)

    def test_verification_unknown_criterion_rejected(self) -> None:
        """Check referencing unknown criterion is rejected."""
        self._create_sample_plan(
            "plan_unk_crit",
            status=PlanStatus.DRAFT,
            acceptance_criteria=["Declared Criterion"],
            verification_commands=[
                {"criterion_id": "Nonexistent Criterion", "command": "python --version"},
            ],
        )
        self.registry.update_status("plan_unk_crit", PlanStatus.READY, actor="user", reason="Ready")
        plan = self.registry.get_plan("plan_unk_crit")
        assert plan is not None
        contract = self._create_approved_contract(plan)
        self.registry.update_status("plan_unk_crit", PlanStatus.IN_PROGRESS, actor="user", reason="Start", contract=contract)
        self.registry.update_status("plan_unk_crit", PlanStatus.IMPLEMENTED, actor="user", reason="Impl done")

        with self.assertRaises(InvalidPlanTransitionError) as ctx:
            self.registry.verify_plan("plan_unk_crit", actor="verifier")
        self.assertIn("unknown criterion", str(ctx.exception).lower())

    def test_verification_missing_criterion_ref_rejected(self) -> None:
        """Legacy check without criterion_ref is rejected fail-closed."""
        self._create_sample_plan(
            "plan_missing_crit",
            status=PlanStatus.DRAFT,
            acceptance_criteria=["Declared Criterion"],
            verification_commands=["python --version"],  # plain string with no criterion
        )
        self.registry.update_status("plan_missing_crit", PlanStatus.READY, actor="user", reason="Ready")
        plan = self.registry.get_plan("plan_missing_crit")
        assert plan is not None
        contract = self._create_approved_contract(plan)
        self.registry.update_status("plan_missing_crit", PlanStatus.IN_PROGRESS, actor="user", reason="Start", contract=contract)
        self.registry.update_status("plan_missing_crit", PlanStatus.IMPLEMENTED, actor="user", reason="Impl done")

        with self.assertRaises(InvalidPlanTransitionError) as ctx:
            self.registry.verify_plan("plan_missing_crit", actor="verifier")
        self.assertIn("no criterion reference", str(ctx.exception).lower())

    def test_verification_duplicate_check_id_rejected(self) -> None:
        """Duplicate check IDs are rejected to prevent ambiguity."""
        self._create_sample_plan(
            "plan_dup_chk",
            status=PlanStatus.DRAFT,
            acceptance_criteria=["Criterion 1"],
            verification_commands=[
                {"criterion_id": "Criterion 1", "command": "python --version", "check_id": "chk_dup_1"},
                {"criterion_id": "Criterion 1", "command": "python --version", "check_id": "chk_dup_1"},
            ],
        )
        self.registry.update_status("plan_dup_chk", PlanStatus.READY, actor="user", reason="Ready")
        plan = self.registry.get_plan("plan_dup_chk")
        assert plan is not None
        contract = self._create_approved_contract(plan)
        self.registry.update_status("plan_dup_chk", PlanStatus.IN_PROGRESS, actor="user", reason="Start", contract=contract)
        self.registry.update_status("plan_dup_chk", PlanStatus.IMPLEMENTED, actor="user", reason="Impl done")

        with self.assertRaises(InvalidPlanTransitionError) as ctx:
            self.registry.verify_plan("plan_dup_chk", actor="verifier")
        self.assertIn("duplicate check identifier", str(ctx.exception).lower())

    # =========================================================================
    # WORKSTREAM E: Lifecycle Actor Provenance (Audit finding BF-2B-04)
    # =========================================================================
    def test_arbitrary_actor_label_cannot_bypass_approval_contract(self) -> None:
        """Arbitrary actor labels (e.g. 'admin', 'root') cannot authorize IN_PROGRESS without contract."""
        self._create_sample_plan("plan_actor_sec", status=PlanStatus.DRAFT)
        self.registry.update_status("plan_actor_sec", PlanStatus.READY, actor="any_user", reason="Ready")

        for malicious_actor in ["admin", "root", "system", "orchestrator", "canonical_runtime", "sudo"]:
            with self.assertRaises(InvalidPlanTransitionError) as ctx:
                self.registry.update_status(
                    "plan_actor_sec",
                    PlanStatus.IN_PROGRESS,
                    actor=malicious_actor,
                    reason="Attempting unauthorized transition",
                    contract=None,
                )
            self.assertIn("requires a valid, matching approvedexecutioncontract", str(ctx.exception).lower())

    def test_arbitrary_actor_label_cannot_bypass_verification_evidence(self) -> None:
        """Arbitrary actor labels cannot force VERIFIED status without valid verification evidence."""
        self._create_sample_plan("plan_actor_ver", status=PlanStatus.DRAFT)
        self.registry.update_status("plan_actor_ver", PlanStatus.READY, actor="user", reason="Ready")
        plan = self.registry.get_plan("plan_actor_ver")
        assert plan is not None
        contract = self._create_approved_contract(plan)
        self.registry.update_status("plan_actor_ver", PlanStatus.IN_PROGRESS, actor="user", reason="Start", contract=contract)
        self.registry.update_status("plan_actor_ver", PlanStatus.IMPLEMENTED, actor="user", reason="Impl done")

        for malicious_actor in ["verifier", "chief_qa", "auditor", "admin"]:
            with self.assertRaises(InvalidPlanTransitionError) as ctx:
                self.registry.update_status(
                    "plan_actor_ver",
                    PlanStatus.VERIFIED,
                    actor=malicious_actor,
                    reason="Attempting fake verification",
                    evidence=None,
                )
            self.assertIn("requires recorded verification evidence", str(ctx.exception).lower())

    def test_actor_is_preserved_as_audit_metadata(self) -> None:
        """Actor string is recorded faithfully in metadata transitions and history for auditability."""
        self._create_sample_plan("plan_audit_trace", status=PlanStatus.DRAFT)
        p = self.registry.update_status("plan_audit_trace", PlanStatus.READY, actor="auditor_jane", reason="Signed off")
        self.assertEqual(p.transitions[-1]["actor"], "auditor_jane")
        self.assertEqual(p.transitions[-1]["reason"], "Signed off")

    # =========================================================================
    # WORKSTREAM A (Phase 2C.2): Deterministic WAL Fault-Injection Suite
    # =========================================================================
    def test_wal_fault_injection_creation_crash_after_journal_publish(self) -> None:
        """Interrupted plan creation after journal publication cleans up partial state and leaves no ghost plan."""
        def crash_hook(stage: str, ctx: Dict[str, Any]) -> None:
            if stage == "after_journal_publish":
                raise RuntimeError("Crash after journal published")

        set_wal_fault_hook(crash_hook)
        try:
            with self.assertRaises(RuntimeError):
                save_plan_document(self.workspace, "new_plan_01.md", "# New Plan\n- **Goal**: Test\n", metadata={"id": "new_plan_01"})
        finally:
            set_wal_fault_hook(None)

        j_files = list(get_journal_dir(self.workspace).glob("*.json"))
        self.assertGreaterEqual(len(j_files), 1)

        new_reg = PlanRegistry(self.workspace)
        self.assertIsNone(new_reg.get_plan("new_plan_01"))
        self.assertEqual(len(list(get_journal_dir(self.workspace).glob("*.json"))), 0)

    def test_wal_fault_injection_creation_crash_after_md_replace(self) -> None:
        """Interrupted plan creation after markdown replacement unlinks partial markdown and purges journal."""
        def crash_hook(stage: str, ctx: Dict[str, Any]) -> None:
            if stage == "after_md_replace":
                raise RuntimeError("Crash after markdown replaced")

        set_wal_fault_hook(crash_hook)
        try:
            with self.assertRaises(RuntimeError):
                save_plan_document(self.workspace, "new_plan_02.md", "# New Plan 02\n- **Goal**: Test 02\n", metadata={"id": "new_plan_02"})
        finally:
            set_wal_fault_hook(None)

        new_reg = PlanRegistry(self.workspace)
        self.assertIsNone(new_reg.get_plan("new_plan_02"))
        self.assertFalse((self.workspace / ".brainfrog" / "plans" / "new_plan_02.md").exists())
        self.assertFalse((self.workspace / ".brainfrog" / "plans" / "new_plan_02.meta.json").exists())

    def test_wal_fault_injection_update_crash_after_journal_publish(self) -> None:
        """Interrupted plan update after journal publish rolls back cleanly to previous revision."""
        self._create_sample_plan("plan_upd_01", title="Original Title", status=PlanStatus.DRAFT)
        orig_plan = self.registry.get_plan("plan_upd_01")
        assert orig_plan is not None
        self.assertEqual(orig_plan.revision, 1)

        def crash_hook(stage: str, ctx: Dict[str, Any]) -> None:
            if stage == "after_journal_publish":
                raise RuntimeError("Crash after journal publish")

        set_wal_fault_hook(crash_hook)
        try:
            with self.assertRaises(RuntimeError):
                self.registry.update_status("plan_upd_01", PlanStatus.READY, actor="user", reason="Bump to ready")
        finally:
            set_wal_fault_hook(None)

        new_reg = PlanRegistry(self.workspace)
        reloaded = new_reg.get_plan("plan_upd_01")
        assert reloaded is not None
        self.assertEqual(reloaded.revision, 1)
        self.assertEqual(reloaded.status, PlanStatus.DRAFT)

    def test_wal_fault_injection_update_crash_after_md_replace(self) -> None:
        """Interrupted plan update after markdown replace rolls back markdown to previous content."""
        self._create_sample_plan("plan_upd_02", title="Original Content", status=PlanStatus.DRAFT)
        orig_plan = self.registry.get_plan("plan_upd_02")
        assert orig_plan is not None
        original_md = orig_plan.content

        def crash_hook(stage: str, ctx: Dict[str, Any]) -> None:
            if stage == "after_md_replace":
                raise RuntimeError("Crash after markdown replaced")

        set_wal_fault_hook(crash_hook)
        try:
            with self.assertRaises(RuntimeError):
                save_plan_document(
                    self.workspace,
                    "plan_upd_02.md",
                    "# Overwritten Content\n- **Goal**: New goal\n",
                    metadata={"id": "plan_upd_02", "revision": 2},
                )
        finally:
            set_wal_fault_hook(None)

        new_reg = PlanRegistry(self.workspace)
        reloaded = new_reg.get_plan("plan_upd_02")
        assert reloaded is not None
        self.assertEqual(reloaded.revision, 1)
        self.assertEqual(reloaded.content, original_md)

    def test_wal_fault_injection_update_crash_after_meta_replace(self) -> None:
        """Interrupted plan update after metadata replace confirms committed transaction."""
        self._create_sample_plan("plan_upd_03", title="Rev 1 Plan", status=PlanStatus.DRAFT)

        def crash_hook(stage: str, ctx: Dict[str, Any]) -> None:
            if stage == "after_meta_replace":
                raise RuntimeError("Crash after meta replaced")

        set_wal_fault_hook(crash_hook)
        try:
            with self.assertRaises(RuntimeError):
                self.registry.update_status("plan_upd_03", PlanStatus.READY, actor="user", reason="Bump to ready")
        finally:
            set_wal_fault_hook(None)

        new_reg = PlanRegistry(self.workspace)
        reloaded = new_reg.get_plan("plan_upd_03")
        assert reloaded is not None
        self.assertEqual(reloaded.revision, 2)
        self.assertEqual(reloaded.status, PlanStatus.READY)
        self.assertEqual(len(list(get_journal_dir(self.workspace).glob("*.json"))), 0)

    def test_wal_fault_injection_recovery_interrupted_during_rollback(self) -> None:
        """Recovery interrupted during rollback survives and completes cleanly on subsequent restart."""
        self._create_sample_plan("plan_multi_crash", title="Multi Crash", status=PlanStatus.DRAFT)
        orig = self.registry.get_plan("plan_multi_crash")
        assert orig is not None

        def crash_hook1(stage: str, ctx: Dict[str, Any]) -> None:
            if stage == "after_md_replace":
                raise RuntimeError("First crash")

        set_wal_fault_hook(crash_hook1)
        try:
            with self.assertRaises(RuntimeError):
                save_plan_document(
                    self.workspace,
                    "plan_multi_crash.md",
                    "# Partially Written\n",
                    metadata={"id": "plan_multi_crash", "revision": 2},
                )
        finally:
            set_wal_fault_hook(None)

        def crash_hook2(stage: str, ctx: Dict[str, Any]) -> None:
            if stage == "during_recovery_rollback_meta":
                raise RuntimeError("Second crash during recovery")

        set_wal_fault_hook(crash_hook2)
        try:
            with self.assertRaises(RuntimeError):
                recover_plan_journal(self.workspace)
        finally:
            set_wal_fault_hook(None)

        new_reg = PlanRegistry(self.workspace)
        reloaded = new_reg.get_plan("plan_multi_crash")
        assert reloaded is not None
        self.assertEqual(reloaded.revision, 1)
        self.assertEqual(reloaded.content, orig.content)
        self.assertEqual(len(list(get_journal_dir(self.workspace).glob("*.json"))), 0)

    def test_recovery_does_not_clobber_external_markdown_modification(self) -> None:
        """Recovery preserves user modifications to markdown made out-of-band while recovery was pending."""
        self._create_sample_plan("plan_ext_mod", title="Original", status=PlanStatus.DRAFT)

        def crash_hook(stage: str, ctx: Dict[str, Any]) -> None:
            if stage == "after_md_replace":
                raise RuntimeError("Crash")

        set_wal_fault_hook(crash_hook)
        try:
            with self.assertRaises(RuntimeError):
                save_plan_document(
                    self.workspace,
                    "plan_ext_mod.md",
                    "# Overwritten by Agent\n",
                    metadata={"id": "plan_ext_mod", "revision": 2},
                )
        finally:
            set_wal_fault_hook(None)

        md_path = self.workspace / ".brainfrog" / "plans" / "plan_ext_mod.md"
        user_custom_text = "# User Custom Manual Edits (Important Work!)\n- Do not delete!\n"
        md_path.write_text(user_custom_text, encoding="utf-8")

        actions = recover_plan_journal(self.workspace)
        action_names = [a["action"] for a in actions]
        self.assertIn("preserved_external_modification", action_names)

        disk_content = md_path.read_text(encoding="utf-8")
        self.assertEqual(disk_content, user_custom_text)

    # =========================================================================
    # PHASE 2C.3 ADVERSARIAL CASES: Invariant Conflict & Concurrency Audits
    # =========================================================================
    def test_recovery_preserves_external_md_when_rev_matches_target_rev(self) -> None:
        """When curr_rev == target_revision but disk Markdown differs from previous and new, preserve external edits."""
        self._create_sample_plan("plan_adv_rev_match", title="Original Title", status=PlanStatus.DRAFT)
        plan = self.registry.get_plan("plan_adv_rev_match")
        assert plan is not None
        self.assertEqual(plan.revision, 1)

        # Forward transaction reached target revision (rev 2) in metadata, but crashed before journal cleanup
        def crash_hook(stage: str, ctx: Dict[str, Any]) -> None:
            if stage == "before_journal_cleanup":
                raise RuntimeError("Crash before journal cleanup")

        set_wal_fault_hook(crash_hook)
        try:
            with self.assertRaises(RuntimeError):
                save_plan_document(
                    self.workspace,
                    "plan_adv_rev_match.md",
                    "# Expected Target Rev 2 Content\n- **Goal**: Expected Target\n",
                    metadata={"id": "plan_adv_rev_match", "revision": 2, "status": "draft"},
                )
        finally:
            set_wal_fault_hook(None)

        # An external user edit occurs out-of-band: Markdown content differs from both rev 1 and rev 2
        md_path = self.workspace / ".brainfrog" / "plans" / "plan_adv_rev_match.md"
        external_edit_text = "# Out-Of-Band Manual Fix\n- Modified by user out of band.\n"
        md_path.write_text(external_edit_text, encoding="utf-8")

        # Now run recovery
        actions = recover_plan_journal(self.workspace)
        action_names = [a["action"] for a in actions]
        self.assertIn("discarded_conflicting_journal", action_names)

        # Verify disk Markdown was NOT clobbered by rollback or forward content
        self.assertEqual(md_path.read_text(encoding="utf-8"), external_edit_text)

        # Verify PlanRegistry loads plan and marks content_tampered = True
        reloaded = self.registry.get_plan("plan_adv_rev_match")
        assert reloaded is not None
        self.assertEqual(reloaded.revision, 2)
        self.assertTrue(reloaded.content_tampered)
        self.assertEqual(len(list(get_journal_dir(self.workspace).glob("*.json"))), 0)

    def test_recovery_discards_conflicting_journal_when_metadata_status_or_hash_conflicts(self) -> None:
        """When curr_rev == target_revision but status or content hash conflicts, discard journal without overwriting."""
        self._create_sample_plan("plan_adv_conflict", title="Rev 1 Plan", status=PlanStatus.DRAFT)
        plan = self.registry.get_plan("plan_adv_conflict")
        assert plan is not None

        # Manually construct journal targeting revision 2 with status READY
        journal_dir = get_journal_dir(self.workspace)
        journal_id = "wal_conflict_test_01"
        entry = PlanJournalEntry(
            journal_id=journal_id,
            plan_id="plan_adv_conflict",
            filename="plan_adv_conflict.md",
            stage="PREPARED",
            created_at=datetime.now(timezone.utc).isoformat(),
            expected_revision=1,
            target_revision=2,
            target_status=PlanStatus.READY.value,
            new_md_hash=hashlib.sha256(b"# Target Content\n").hexdigest(),
            has_md_update=False,
            previous_meta_dict=plan.to_metadata_dict(),
        )
        j_file = journal_dir / f"{journal_id}.json"
        j_file.write_text(json.dumps(entry.to_dict(), indent=2), encoding="utf-8")

        # But on disk, metadata was updated to revision 2 with status FAILED (conflicting status)
        meta_path = self.workspace / ".brainfrog" / "plans" / "plan_adv_conflict.meta.json"
        disk_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        disk_meta["revision"] = 2
        disk_meta["status"] = PlanStatus.FAILED.value
        meta_path.write_text(json.dumps(disk_meta, indent=2), encoding="utf-8")

        # Recover
        actions = recover_plan_journal(self.workspace)
        action_names = [a["action"] for a in actions]
        self.assertIn("discarded_conflicting_journal", action_names)

        # Disk status should remain FAILED and revision 2, NOT rolled back to DRAFT or overwritten
        reloaded_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertEqual(reloaded_meta["revision"], 2)
        self.assertEqual(reloaded_meta["status"], PlanStatus.FAILED.value)
        self.assertEqual(len(list(journal_dir.glob("*.json"))), 0)

    def test_recovery_newer_external_md_edit_while_metadata_remains_at_target_rev(self) -> None:
        """External Markdown edit while metadata remains at target revision is preserved and detected as tampered."""
        self._create_sample_plan("plan_adv_ext_md_target", title="Rev 1", status=PlanStatus.DRAFT)

        def crash_hook(stage: str, ctx: Dict[str, Any]) -> None:
            if stage == "before_journal_cleanup":
                raise RuntimeError("Crash before journal cleanup")

        set_wal_fault_hook(crash_hook)
        try:
            with self.assertRaises(RuntimeError):
                save_plan_document(
                    self.workspace,
                    "plan_adv_ext_md_target.md",
                    "# Rev 2 Content\n- **Goal**: Goal 2\n",
                    metadata={"id": "plan_adv_ext_md_target", "revision": 2, "status": "draft"},
                )
        finally:
            set_wal_fault_hook(None)

        # Developer edits markdown directly in workspace while recovery is pending
        md_path = self.workspace / ".brainfrog" / "plans" / "plan_adv_ext_md_target.md"
        dev_edit = "# Direct Developer Hotfix\n- Urgent change made in IDE\n"
        md_path.write_text(dev_edit, encoding="utf-8")

        # Recovery runs
        actions = recover_plan_journal(self.workspace)
        action_names = [a["action"] for a in actions]
        self.assertIn("discarded_conflicting_journal", action_names)

        # Developer edit is strictly intact
        self.assertEqual(md_path.read_text(encoding="utf-8"), dev_edit)

        # Registry loads and flags content_tampered
        reloaded = self.registry.get_plan("plan_adv_ext_md_target")
        assert reloaded is not None
        self.assertTrue(reloaded.content_tampered)
        self.assertEqual(reloaded.content, dev_edit)

    def test_recovery_concurrent_update_between_inspection_and_replacement(self) -> None:
        """Concurrent update between recovery inspection and replacement is NOT clobbered by stale rollback."""
        self._create_sample_plan("plan_adv_race", title="Rev 1 Base", status=PlanStatus.DRAFT)
        orig_plan = self.registry.get_plan("plan_adv_race")
        assert orig_plan is not None

        # Crash after markdown replace during update to rev 2 (uncommitted tx)
        def crash_hook1(stage: str, ctx: Dict[str, Any]) -> None:
            if stage == "after_md_replace":
                raise RuntimeError("Crash after md replace")

        set_wal_fault_hook(crash_hook1)
        try:
            with self.assertRaises(RuntimeError):
                save_plan_document(
                    self.workspace,
                    "plan_adv_race.md",
                    "# Rev 2 Intermediate\n",
                    metadata={"id": "plan_adv_race", "revision": 2},
                )
        finally:
            set_wal_fault_hook(None)

        meta_path = self.workspace / ".brainfrog" / "plans" / "plan_adv_race.meta.json"
        md_path = self.workspace / ".brainfrog" / "plans" / "plan_adv_race.md"

        # Simulate concurrent process updating the plan to revision 3 before recovery completes rollback
        concurrent_meta = {
            "id": "plan_adv_race",
            "title": "Rev 3 Concurrent Update",
            "goal": "Concurrent Work",
            "status": PlanStatus.READY.value,
            "revision": 3,
            "content_hash": hashlib.sha256(b"# Rev 3 Concurrent Content\n").hexdigest(),
        }
        concurrent_md = "# Rev 3 Concurrent Content\n"

        def concurrent_race_hook(stage: str, ctx: Dict[str, Any]) -> None:
            if stage == "before_recovery_rollback_meta":
                # Simulate a concurrent update that wrote revision 3 while recovery was paused
                meta_path.write_text(json.dumps(concurrent_meta, indent=2), encoding="utf-8")
                md_path.write_text(concurrent_md, encoding="utf-8")

        set_wal_fault_hook(concurrent_race_hook)
        try:
            actions = recover_plan_journal(self.workspace)
        finally:
            set_wal_fault_hook(None)

        action_names = [a["action"] for a in actions]
        self.assertIn("discarded_conflicting_journal", action_names)

        # Verify disk state has NOT been rolled back to revision 1
        loaded = self.registry.get_plan("plan_adv_race")
        assert loaded is not None
        self.assertEqual(loaded.revision, 3)
        self.assertEqual(loaded.status, PlanStatus.READY)
        self.assertEqual(loaded.content, concurrent_md)

    def test_recovery_repeatedly_interrupted_across_all_phases(self) -> None:
        """Recovery repeatedly interrupted across multiple phases survives and converges to consistent state."""
        self._create_sample_plan("plan_adv_loop_crash", title="Initial Stable", status=PlanStatus.DRAFT)
        orig = self.registry.get_plan("plan_adv_loop_crash")
        assert orig is not None

        # Step 1: Create uncommitted journal by crashing after md replace
        def crash_init(stage: str, ctx: Dict[str, Any]) -> None:
            if stage == "after_md_replace":
                raise RuntimeError("Crash during write")

        set_wal_fault_hook(crash_init)
        try:
            with self.assertRaises(RuntimeError):
                save_plan_document(
                    self.workspace,
                    "plan_adv_loop_crash.md",
                    "# Broken Content\n",
                    metadata={"id": "plan_adv_loop_crash", "revision": 2},
                )
        finally:
            set_wal_fault_hook(None)

        # Crash recovery phase 1: crash after rollback meta
        def crash_rec1(stage: str, ctx: Dict[str, Any]) -> None:
            if stage == "during_recovery_rollback_meta":
                raise RuntimeError("Recovery Crash 1")

        set_wal_fault_hook(crash_rec1)
        try:
            with self.assertRaises(RuntimeError):
                recover_plan_journal(self.workspace)
        finally:
            set_wal_fault_hook(None)

        # Crash recovery phase 2: crash after rollback md
        def crash_rec2(stage: str, ctx: Dict[str, Any]) -> None:
            if stage == "during_recovery_rollback_md":
                raise RuntimeError("Recovery Crash 2")

        set_wal_fault_hook(crash_rec2)
        try:
            with self.assertRaises(RuntimeError):
                recover_plan_journal(self.workspace)
        finally:
            set_wal_fault_hook(None)

        # Crash recovery phase 3: crash before cleanup
        def crash_rec3(stage: str, ctx: Dict[str, Any]) -> None:
            if stage == "before_recovery_rollback_cleanup":
                raise RuntimeError("Recovery Crash 3")

        set_wal_fault_hook(crash_rec3)
        try:
            with self.assertRaises(RuntimeError):
                recover_plan_journal(self.workspace)
        finally:
            set_wal_fault_hook(None)

        # Final restart and recovery
        final_actions = recover_plan_journal(self.workspace)
        self.assertGreaterEqual(len(final_actions), 1)

        # Confirm convergence: clean revision 1, original content, 0 journals, 0 tmp files
        reloaded = self.registry.get_plan("plan_adv_loop_crash")
        assert reloaded is not None
        self.assertEqual(reloaded.revision, 1)
        self.assertEqual(reloaded.content, orig.content)
        self.assertEqual(reloaded.status, PlanStatus.DRAFT)
        self.assertEqual(len(list(get_journal_dir(self.workspace).glob("*.json"))), 0)
        self.assertEqual(len(list((self.workspace / ".brainfrog" / "plans").glob(".tmp_*"))), 0)

    # =========================================================================
    # PHASE 2C.4: Concurrency Invariant & Lock Order Audits
    # =========================================================================
    def test_concurrent_writers_same_plan_serialize_cleanly(self) -> None:
        """Concurrent calls to save_plan_document and update_status serialize cleanly without deadlock."""
        import threading
        self._create_sample_plan("plan_conc_same", title="Base Plan", status=PlanStatus.DRAFT)
        plan = self.registry.get_plan("plan_conc_same")
        assert plan is not None

        errors: List[Exception] = []

        def worker_save() -> None:
            try:
                for i in range(5):
                    save_plan_document(
                        self.workspace,
                        "plan_conc_same.md",
                        f"# Plan Conc Same\n- **Goal**: Iter {i}\n",
                        metadata={"id": "plan_conc_same", "title": f"Title {i}"},
                    )
            except Exception as e:
                errors.append(e)

        def worker_update() -> None:
            try:
                for _ in range(5):
                    # Toggle between DRAFT and DRAFT (or read and touch metadata)
                    p = self.registry.get_plan("plan_conc_same")
                    if p:
                        save_plan_document(
                            self.workspace,
                            "plan_conc_same.md",
                            p.content,
                            metadata={"id": "plan_conc_same", "title": p.title, "goal": p.goal},
                        )
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=worker_save)
        t2 = threading.Thread(target=worker_update)
        t1.start()
        t2.start()
        t1.join(timeout=10.0)
        t2.join(timeout=10.0)

        self.assertFalse(t1.is_alive(), "worker_save deadlocked or timed out")
        self.assertFalse(t2.is_alive(), "worker_update deadlocked or timed out")
        self.assertEqual(errors, [])

        final_plan = self.registry.get_plan("plan_conc_same")
        assert final_plan is not None
        self.assertGreaterEqual(final_plan.revision, 2)
        self.assertEqual(len(list(get_journal_dir(self.workspace).glob("*.json"))), 0)

    def test_concurrent_writers_different_plans_no_deadlock(self) -> None:
        """Concurrent writes to distinct plans serialize their WAL phase without deadlock."""
        import threading
        self._create_sample_plan("plan_diff_a", title="Plan A", status=PlanStatus.DRAFT)
        self._create_sample_plan("plan_diff_b", title="Plan B", status=PlanStatus.DRAFT)

        errors: List[Exception] = []

        def worker_a() -> None:
            try:
                for i in range(5):
                    save_plan_document(
                        self.workspace,
                        "plan_diff_a.md",
                        f"# Plan A Content {i}\n",
                        metadata={"id": "plan_diff_a"},
                    )
            except Exception as e:
                errors.append(e)

        def worker_b() -> None:
            try:
                for i in range(5):
                    save_plan_document(
                        self.workspace,
                        "plan_diff_b.md",
                        f"# Plan B Content {i}\n",
                        metadata={"id": "plan_diff_b"},
                    )
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=worker_a)
        t2 = threading.Thread(target=worker_b)
        t1.start()
        t2.start()
        t1.join(timeout=10.0)
        t2.join(timeout=10.0)

        self.assertFalse(t1.is_alive(), "worker_a deadlocked")
        self.assertFalse(t2.is_alive(), "worker_b deadlocked")
        self.assertEqual(errors, [])

        pa = self.registry.get_plan("plan_diff_a")
        pb = self.registry.get_plan("plan_diff_b")
        assert pa is not None and pb is not None
        self.assertEqual(len(list(get_journal_dir(self.workspace).glob("*.json"))), 0)

    def test_out_of_band_external_edit_tamper_detection_fail_closed(self) -> None:
        """Out-of-band external edit bypassing BrainFrog locks is detected upon load and fails closed."""
        self._create_sample_plan("plan_oob_tamper", title="Verified Base", status=PlanStatus.READY)
        plan = self.registry.get_plan("plan_oob_tamper")
        assert plan is not None
        contract = self._create_approved_contract(plan)
        self.registry.update_status("plan_oob_tamper", PlanStatus.IN_PROGRESS, actor="user", reason="Start", contract=contract)
        self.registry.update_status("plan_oob_tamper", PlanStatus.IMPLEMENTED, actor="user", reason="Impl done")

        # Create valid verification evidence and verify
        ev = {
            "plan_id": "plan_oob_tamper",
            "plan_revision": 3,
            "plan_content_hash": plan.content_hash,
            "checks": [
                {"check_name": "Check A", "command": "python --version", "status": "passed", "criterion_ref": "Criterion A", "check_id": "chk_oob_1"},
                {"check_name": "Check B", "command": "python --version", "status": "passed", "criterion_ref": "Criterion B", "check_id": "chk_oob_2"},
            ],
        }
        verified_plan = self.registry.update_status("plan_oob_tamper", PlanStatus.VERIFIED, actor="verifier", reason="Pass", evidence=ev)
        self.assertEqual(verified_plan.status, PlanStatus.VERIFIED)

        # An out-of-band external editor modifies the Markdown file directly (no BrainFrog lock acquired)
        md_file = self.workspace / ".brainfrog" / "plans" / "plan_oob_tamper.md"
        md_file.write_text("# Malicious Out-Of-Band Edit\n- Backdoor inserted here\n", encoding="utf-8")

        # When loaded through PlanRegistry, tamper detection immediately demotes VERIFIED to DRAFT
        tampered_plan = self.registry.get_plan("plan_oob_tamper")
        assert tampered_plan is not None
        self.assertTrue(tampered_plan.content_tampered)
        self.assertEqual(tampered_plan.status, PlanStatus.DRAFT)

        # Transition to READY or IN_PROGRESS fails closed
        with self.assertRaises(InvalidPlanTransitionError) as ctx:
            self.registry.update_status("plan_oob_tamper", PlanStatus.READY, actor="user", reason="Try ready")
        self.assertIn("modified out-of-band", str(ctx.exception).lower())

    # =========================================================================
    # WORKSTREAM B (Phase 2C.2): Verification Claim Boundaries & Invariants
    # =========================================================================
    def test_verification_evidence_duplicate_check_id_rejected_at_transition(self) -> None:
        """Transition to VERIFIED with duplicate check_id in evidence dictionary fails closed."""
        self._create_sample_plan("plan_dup_chk", status=PlanStatus.READY)
        plan = self.registry.get_plan("plan_dup_chk")
        assert plan is not None
        contract = self._create_approved_contract(plan)
        self.registry.update_status("plan_dup_chk", PlanStatus.IN_PROGRESS, actor="user", reason="Start", contract=contract)
        self.registry.update_status("plan_dup_chk", PlanStatus.IMPLEMENTED, actor="user", reason="Impl done")

        dup_evidence = {
            "plan_id": "plan_dup_chk",
            "plan_revision": 3,
            "plan_content_hash": plan.content_hash,
            "checks": [
                {"check_name": "Check 1", "command": "python --version", "status": "passed", "criterion_ref": "Criterion A", "check_id": "chk_dup_01"},
                {"check_name": "Check 2", "command": "python --version", "status": "passed", "criterion_ref": "Criterion B", "check_id": "chk_dup_01"},
            ],
        }
        with self.assertRaises(InvalidPlanTransitionError) as ctx:
            self.registry.update_status(
                "plan_dup_chk",
                PlanStatus.VERIFIED,
                actor="verifier",
                reason="Try verify with dup check id",
                evidence=dup_evidence,
            )
        self.assertIn("duplicate check identifier 'chk_dup_01'", str(ctx.exception).lower())

    def test_verification_user_facing_message_claims_structural_coverage(self) -> None:
        """Runtime /plan verify command explicitly states structural coverage boundary without claiming semantic proof."""
        self._create_sample_plan("plan_msg_boundary", status=PlanStatus.READY)
        plan = self.registry.get_plan("plan_msg_boundary")
        assert plan is not None
        contract = self._create_approved_contract(plan)
        self.registry.update_status("plan_msg_boundary", PlanStatus.IN_PROGRESS, actor="user", reason="Start", contract=contract)
        self.registry.update_status("plan_msg_boundary", PlanStatus.IMPLEMENTED, actor="user", reason="Impl done")

        runtime = BrainFrogRuntime(
            repo_dir=self.workspace,
            persist_sessions=False,
            sessions=SessionManager(),
            system1_factory=lambda *args, **kwargs: SyntheticSystem1(),
            system2_factory=lambda *args, **kwargs: SyntheticSystem2(),
        )
        msg = IncomingMessage(
            text="/plan verify plan_msg_boundary",
            channel="cli",
            user_id="local",
            conversation_id="default",
        )
        out = runtime.handle_message(msg)
        self.assertTrue(out.success)
        self.assertEqual(out.status, "completed")
        self.assertIn("structural criteria checks", out.text.lower())
        self.assertIn("domain semantic correctness requires code review", out.text.lower())


if __name__ == "__main__":
    unittest.main()
