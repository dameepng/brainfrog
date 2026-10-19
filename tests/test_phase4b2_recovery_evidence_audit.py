"""BrainFrog Phase 4B.2 — Recovery Evidence & Active-Process Audit Test Suite.

Verifies:
1. File existence alone must never establish successful step completion.
2. A committed transaction must not automatically imply that the step's intended outcome was achieved.
3. A RUNNING record must not be classified as abandoned solely because a new tracker instance has _active_step_id = None.
4. If another process may still own the execution, recovery must fail closed or establish ownership through a reliable process/lease mechanism.
5. Recovery must not replay a non-idempotent step without explicit reconciliation and a fresh valid approval where required by the existing contract.
6. Idempotent retry eligibility must be determined by trusted command policy, not an untrusted model-generated label alone.
7. A step may be marked PASSED only after its actual execution result is durably recorded. A plan may be VERIFIED only through existing acceptance-evidence requirements.
8. Any uncertainty about mutation outcome must remain explicit in persisted progress and the execution summary.
9. Preserve the canonical orchestrator, approval contract, transaction coordinator, and existing workspace protections.

Adversarial Tests:
- Target file exists but contains incorrect content.
- Transaction is committed but acceptance conditions are false.
- Crash before mutation versus crash after mutation.
- Tracker restart while the original process is still alive.
- Stale process/lease metadata and PID reuse.
- Progress write fails after a mutation.
- Duplicate recovery attempts from two processes.
- Reconciliation with missing or unverifiable evidence.
- Fresh approval requirements after an ambiguous non-idempotent step.
- Unknown operation type or untrusted idempotency claims.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

from core.plans import (
    InvalidPlanTransitionError,
    PlanDocument,
    PlanRegistry,
    PlanStatus,
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
from core.runtime.step_runner import (
    ActiveProcessOwnershipError,
    AmbiguousStepExecutionError,
    PlanProgressTracker,
    StepExecutionStatus,
    StepProgressRecord,
    StepRecoveryAudit,
    StructuredStepRunner,
    _safe_atomic_write_json,
    is_step_safely_idempotent,
)
from core.runtime.transaction import (
    TransactionCoordinator,
    TransactionResult,
    TransactionStatus,
)
from core.runtime.workspace_lock import WorkspaceLock, is_pid_running
from orchestrator import (
    Orchestrator,
    PlanStep,
    RunConfig,
    StepResult,
)
from system1.base import Answer, SystemOneClient


class SyntheticS1Double(SystemOneClient):
    """Deterministic System 1 test double."""
    name: str = "synthetic-s1"

    def decide(self, state: Dict[str, Any], questions: Dict[str, Any]) -> Dict[str, Answer]:
        out = {}
        for q_name in questions:
            if "change_type" in q_name:
                out[q_name] = Answer("code_change", 0.95)
            else:
                out[q_name] = Answer("unrelated", 0.90)
        return out


class SyntheticS2Double:
    """Deterministic System 2 test double."""
    name: str = "synthetic-s2"
    model: str = "synthetic-claude"

    def __init__(self, step_file_map: Optional[Dict[str, Dict[str, str]]] = None) -> None:
        self.step_file_map = step_file_map or {}
        self.executed_steps: List[str] = []

    def plan_task(self, task: str, *args: Any, **kwargs: Any) -> List[PlanStep]:
        return [
            PlanStep("1", "Setup module 1", ["src/mod1.py"]),
            PlanStep("2", "Setup module 2", ["src/mod2.py"]),
        ]

    def write_code(self, step: Any, task: str, file_contents: Any, *args: Any, **kwargs: Any) -> Dict[str, str]:
        step_id = getattr(step, "id", str(step))
        self.executed_steps.append(step_id)
        if step_id in self.step_file_map:
            return self.step_file_map[step_id]
        target_files = getattr(step, "files", [])
        return {f: f"# Synthetic implementation for step {step_id}\nx = {step_id}\n" for f in target_files}


class TestPhase4B2RecoveryEvidenceAudit(unittest.TestCase):
    """Adversarial test suite for Phase 4B.2 Recovery Evidence & Active-Process Audit."""

    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="bf_audit_4b2_")).resolve()
        self.repo_dir = self.temp_dir / "workspace"
        self.repo_dir.mkdir(parents=True)
        self.plans_dir = self.repo_dir / ".brainfrog" / "plans"
        self.plans_dir.mkdir(parents=True)
        self.tx_dir = self.repo_dir / ".brainfrog" / "transactions"
        self.tx_dir.mkdir(parents=True)
        (self.repo_dir / "src").mkdir(parents=True)

        subprocess.run(["git", "init"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "AuditTester4B2"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "audit4b2@synthetic.local"], cwd=self.repo_dir, capture_output=True, check=True)
        (self.repo_dir / "README.md").write_text("# Audit Workspace Phase 4B.2\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "Init commit"], cwd=self.repo_dir, capture_output=True, check=True)

        self.registry = PlanRegistry(self.repo_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_sample_plan(
        self,
        plan_id: str = "plan_4b2_01",
        title: str = "Recovery Audit Plan",
        status: PlanStatus = PlanStatus.READY,
        session_id: str = "cli:local:default",
        steps: Optional[List[Dict[str, Any]]] = None,
        criteria: Optional[List[str]] = None,
    ) -> PlanDocument:
        raw_steps = steps or [
            {"id": "1", "description": "Implement mod1", "files": ["src/mod1.py"]},
            {"id": "2", "description": "Implement mod2", "files": ["src/mod2.py"]},
        ]
        raw_criteria = criteria or ["Criterion 1: mod1 working", "Criterion 2: mod2 working"]
        save_plan_document(
            self.repo_dir,
            f"{plan_id}.md",
            f"# {title}\n- **Goal**: Test Phase 4B.2 Recovery Evidence Audit\n",
            metadata={
                "id": plan_id,
                "title": title,
                "goal": "Test Phase 4B.2 Recovery Evidence Audit",
                "status": status.value,
                "revision": 1,
                "session_id": session_id,
                "steps": raw_steps,
                "acceptance_criteria": raw_criteria,
            },
        )
        p = self.registry.get_plan(plan_id)
        assert p is not None
        return p

    def _create_contract(
        self,
        plan: PlanDocument,
        targets: Optional[List[str]] = None,
        session_id: str = "cli:local:default",
        session_incarnation: str = "inc_4b2_1",
        expires_in: float = 300.0,
    ) -> Tuple[ApprovedExecutionContract, ApprovalService, ApprovalRequest]:
        approved_targets = targets or ["src/mod1.py", "src/mod2.py"]
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
            user_id="local",
            conversation_id="default",
            operation_type="write_files",
            canonical_operation=op,
            risk_class=RiskClass.MEDIUM.value,
            session_incarnation_id=session_incarnation,
            capabilities=caps,
            workspace_root=str(self.repo_dir),
            plan_id=plan.id,
            plan_revision=plan.revision,
            plan_content_hash=plan.content_hash,
        )
        app_service.approve(app_req.request_id, "security_lead", "cli")
        ok, consumed_req, reason = app_service.verify_and_consume(
            app_req.request_id,
            app_req.operation_digest,
            session_id,
            "cli",
            session_incarnation_id=session_incarnation,
            requester_id="local",
        )
        assert ok and consumed_req is not None, f"Failed to consume approval: {reason}"
        consumed_req.expires_at = time.time() + expires_in
        consumed_req.authorization_digest = consumed_req.compute_authorization_digest()
        consumed_req.approval_digest = consumed_req.compute_approval_digest()
        contract = ApprovedExecutionContract.from_approval_request(consumed_req, repo_dir=self.repo_dir)
        return contract, app_service, consumed_req

    # =========================================================================
    # Test 1: Target File Exists But Contains Incorrect Content
    # =========================================================================
    def test_target_file_exists_with_incorrect_content_never_establishes_completion(self) -> None:
        """1. Target file exists on disk with incorrect content; file existence alone must never establish completion."""
        plan = self._create_sample_plan("plan_bad_content")
        tracker = PlanProgressTracker(self.repo_dir, plan.id)
        tracker.initialize_steps([{"id": "1", "description": "Implement mod1", "files": ["src/mod1.py"], "is_idempotent": False}])
        tracker.mark_running("1", is_idempotent=False)

        # File exists on disk, but has incorrect / truncated content
        (self.repo_dir / "src" / "mod1.py").write_text("# TRUNCATED / CORRUPT CONTENT\n", encoding="utf-8")

        # Invariant 1: File existence alone must never establish step completion
        audit = tracker.audit_step_recovery("1")
        self.assertIn("src/mod1.py", audit.files_existing)
        self.assertNotEqual(audit.classification, "completed", "File existence alone must never establish completed classification")
        self.assertEqual(audit.classification, "requiring_reconciliation")
        self.assertFalse(audit.evidence_sufficient)
        self.assertIn("file existence alone does not establish completion", audit.decision_reason.lower())

        # Attempting reconciliation as PASSED with target_content_verified=False fails closed
        with self.assertRaises(ValueError) as ctx:
            tracker.reconcile_ambiguous_step(
                "1",
                new_status=StepExecutionStatus.PASSED,
                operator="lead_auditor",
                operator_attestation="Claiming completed because file exists",
                target_content_verified=False,
            )
        self.assertIn("content verification failed", str(ctx.exception).lower())

        # Execution summary keeps step in requiring_reconciliation, never in completed
        summary = tracker.get_execution_summary()
        self.assertNotIn("1", summary["completed"])
        self.assertIn("1", summary["requiring_reconciliation"])

    # =========================================================================
    # Test 2: Committed Transaction With False Acceptance Conditions
    # =========================================================================
    def test_committed_transaction_with_false_acceptance_conditions_fails_closed(self) -> None:
        """2. Transaction is committed on disk, but acceptance conditions are false; fails closed."""
        plan = self._create_sample_plan("plan_tx_false_accept")
        tracker = PlanProgressTracker(self.repo_dir, plan.id)
        tracker.initialize_steps([{"id": "1", "description": "Implement mod1", "files": ["src/mod1.py"], "is_idempotent": False}])
        tracker.mark_running("1", is_idempotent=False)

        # Create committed transaction with domain_verification passed=False
        tx_id = "tx_committed_unverified_01"
        tx_file = self.tx_dir / f"{tx_id}.json"
        tx_payload = {
            "id": tx_id,
            "status": "committed",
            "domain_verification": {
                "command": ["pytest"],
                "passed": False,
                "returncode": 1,
            },
        }
        tx_file.write_text(json.dumps(tx_payload), encoding="utf-8")
        rec = tracker.get_record("1")
        assert rec is not None
        rec.transaction_id = tx_id
        tracker._persist()

        # Invariant 2: Committed transaction does not imply intended outcome achieved
        audit = tracker.audit_step_recovery("1")
        self.assertEqual(audit.transaction_status, "committed")
        self.assertFalse(audit.acceptance_verified)
        self.assertFalse(audit.evidence_sufficient)
        self.assertEqual(audit.classification, "requiring_reconciliation")

        # Explicitly attempting to reconcile as PASSED with acceptance_verified=False fails closed
        with self.assertRaises(ValueError) as ctx1:
            tracker.reconcile_ambiguous_step(
                "1",
                new_status=StepExecutionStatus.PASSED,
                operator="lead_auditor",
                operator_attestation="Committed transaction but tests failed",
                acceptance_verified=False,
                transaction_id=tx_id,
            )
        self.assertIn("acceptance conditions are false", str(ctx1.exception).lower())

        # Attempting reconciliation referencing the transaction whose domain_verification failed also fails closed
        with self.assertRaises(ValueError) as ctx2:
            tracker.reconcile_ambiguous_step(
                "1",
                new_status=StepExecutionStatus.PASSED,
                operator="lead_auditor",
                operator_attestation="Attempting to reconcile using tx with failed domain verification",
                transaction_id=tx_id,
            )
        self.assertIn("domain verification failed", str(ctx2.exception).lower())

    # =========================================================================
    # Test 3: Crash Before Mutation Versus Crash After Mutation
    # =========================================================================
    def test_crash_before_mutation_versus_crash_after_mutation(self) -> None:
        """3. Distinguishes crash before mutation (clean workspace) vs crash after mutation (workspace modified)."""
        # 3A: Crash before mutation (idempotent inspect step)
        plan_before = self._create_sample_plan("plan_crash_before")
        tracker_before = PlanProgressTracker(self.repo_dir, plan_before.id)
        tracker_before.initialize_steps([
            {"id": "b1", "description": "Safe git status", "action_type": "verify", "is_idempotent": True, "command": ["git", "status"]}
        ])
        tracker_before.mark_running("b1", is_idempotent=True)

        audit_before = tracker_before.audit_step_recovery("b1")
        self.assertEqual(audit_before.mutation_timing, "before_mutation")
        self.assertFalse(audit_before.has_workspace_mutations)
        self.assertEqual(audit_before.classification, "retryable")
        self.assertTrue(audit_before.evidence_sufficient)

        # 3B: Crash after mutation (non-idempotent code step)
        plan_after = self._create_sample_plan("plan_crash_after")
        tracker_after = PlanProgressTracker(self.repo_dir, plan_after.id)
        tracker_after.initialize_steps([
            {"id": "a1", "description": "Write mod1", "files": ["src/mod1.py"], "is_idempotent": False}
        ])
        tracker_after.mark_running("a1", is_idempotent=False)
        (self.repo_dir / "src" / "mod1.py").write_text("# mutated code\n", encoding="utf-8")

        audit_after = tracker_after.audit_step_recovery("a1")
        self.assertEqual(audit_after.mutation_timing, "after_mutation")
        self.assertTrue(audit_after.has_workspace_mutations)
        self.assertEqual(audit_after.classification, "requiring_reconciliation")
        self.assertFalse(audit_after.evidence_sufficient)

        # Runner refuses blind rerun of step with mutations
        contract, _, _ = self._create_contract(plan_after, targets=["src/mod1.py"])
        cfg = RunConfig(repo_dir=self.repo_dir, task="build", mode="build", execution_contract=contract, plan_id=plan_after.id)
        runner = StructuredStepRunner(self.repo_dir, Orchestrator(SyntheticS1Double(), SyntheticS2Double(), cfg), contract, plan_after.id, tracker=tracker_after)
        with self.assertRaises(AmbiguousStepExecutionError) as err_ctx:
            runner.run_steps([PlanStep("a1", "Write mod1", ["src/mod1.py"])])
        self.assertIn("blind rerun is forbidden", str(err_ctx.exception).lower())

    # =========================================================================
    # Test 4: Tracker Restart While Original Process Is Still Alive
    # =========================================================================
    def test_tracker_restart_while_original_process_still_alive_fails_closed(self) -> None:
        """4. Invariant 3 & 4: RUNNING step owned by live OS process is NOT abandoned; fails closed."""
        plan = self._create_sample_plan("plan_live_owner")

        # Spawn a live background process to act as the original executing process
        live_proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            live_pid = live_proc.pid

            # Original process marked step running under its PID
            tracker_orig = PlanProgressTracker(self.repo_dir, plan.id)
            tracker_orig.initialize_steps([{"id": "1", "description": "Active step", "is_idempotent": False}])
            tracker_orig.mark_running("1", is_idempotent=False, owner_pid=live_pid, lease_timeout=60.0)

            # In a new tracker instance (simulating restart or external inspector)
            tracker_new = PlanProgressTracker(self.repo_dir, plan.id)
            self.assertIsNone(tracker_new.active_step_id, "_active_step_id must be None on new instance")

            # Invariant 3: Must NOT classify as abandoned solely because _active_step_id is None
            self.assertTrue(tracker_new.is_step_active("1"), "Step must be identified as active because owner is alive")
            is_owned, owner_pid, reason = tracker_new.is_step_actively_owned("1")
            self.assertTrue(is_owned)
            self.assertEqual(owner_pid, live_pid)

            # Invariant 4: Recovery fails closed with ActiveProcessOwnershipError
            with self.assertRaises(ActiveProcessOwnershipError) as act_ctx:
                tracker_new.check_ambiguous_interrupted_step()
            self.assertEqual(act_ctx.exception.owner_pid, live_pid)
            self.assertIn(f"PID {live_pid}", str(act_ctx.exception))

            # Attempting reconciliation while other process is live fails closed
            with self.assertRaises(ActiveProcessOwnershipError) as recon_ctx:
                tracker_new.reconcile_ambiguous_step(
                    "1",
                    new_status=StepExecutionStatus.PASSED,
                    operator="lead_auditor",
                    operator_attestation="Attempting reconciliation while owner alive",
                )
            self.assertEqual(recon_ctx.exception.owner_pid, live_pid)

            # Audit identifies active ownership
            audit = tracker_new.audit_step_recovery("1")
            self.assertTrue(audit.is_owner_alive)
            self.assertEqual(audit.owner_pid, live_pid)
            self.assertFalse(audit.pid_reused)
            self.assertEqual(audit.classification, "requiring_reconciliation")
            self.assertIn(f"PID {live_pid}", audit.decision_reason)

        finally:
            live_proc.terminate()
            live_proc.wait(timeout=5.0)

        # Once the live process has terminated, owner is dead: ActiveProcessOwnershipError is NOT raised
        tracker_after_death = PlanProgressTracker(self.repo_dir, plan.id)
        is_owned_dead, _, reason_dead = tracker_after_death.is_step_actively_owned("1")
        self.assertFalse(is_owned_dead)
        self.assertEqual(reason_dead, "owner_process_dead")

        # Now check_ambiguous_interrupted_step returns the record (ambiguous) instead of raising ActiveProcessOwnershipError
        ambig = tracker_after_death.check_ambiguous_interrupted_step()
        self.assertIsNotNone(ambig)
        assert ambig is not None
        self.assertEqual(ambig.step_id, "1")

    # =========================================================================
    # Test 5: Stale Process/Lease Metadata and PID Reuse
    # =========================================================================
    def test_stale_process_lease_metadata_and_pid_reuse(self) -> None:
        """5. Dead process PID or reused PID detected; avoids false active ownership blocks."""
        plan = self._create_sample_plan("plan_stale_pid")
        tracker = PlanProgressTracker(self.repo_dir, plan.id)
        tracker.initialize_steps([{"id": "1", "description": "Step 1", "is_idempotent": False}])

        # 5A: Stale non-existent PID
        dead_pid = 99999999
        tracker.mark_running("1", is_idempotent=False, owner_pid=dead_pid)
        is_owned, owner_pid, reason = tracker.is_step_actively_owned("1")
        self.assertFalse(is_owned)
        self.assertEqual(reason, "owner_process_dead")

        audit = tracker.audit_step_recovery("1")
        self.assertFalse(audit.is_owner_alive)
        self.assertFalse(audit.pid_reused)

        # 5B: PID reuse detection (create_time mismatch)
        # Seed record with current PID but fake create_time from the distant past
        rec = tracker.get_record("1")
        assert rec is not None
        rec.owner_pid = os.getpid()
        rec.owner_create_time = 1000.0  # Mismatched timestamp
        tracker._persist()

        # Mock psutil Process creation time to simulate PID reuse by another process
        class MockProc:
            def create_time(self) -> float:
                return 2000.0  # 1000 seconds later

        with patch("core.runtime.step_runner.psutil.Process", return_value=MockProc()):
            audit_reused = tracker.audit_step_recovery("1")
            self.assertTrue(audit_reused.pid_reused)
            self.assertFalse(audit_reused.is_owner_alive)

            is_owned_reused, _, reason_reused = tracker.is_step_actively_owned("1")
            self.assertFalse(is_owned_reused)
            self.assertEqual(reason_reused, "pid_reused")

    # =========================================================================
    # Test 6: Progress Write Fails After a Mutation
    # =========================================================================
    def test_progress_write_failure_after_mutation_fails_closed(self) -> None:
        """6. Invariant 8: Progress write failure after mutation fails closed without fabricating PASSED."""
        plan = self._create_sample_plan("plan_write_fail_4b2")
        contract, _, _ = self._create_contract(plan, targets=["src/mod1.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="local", reason="Start", contract=contract)

        cfg = RunConfig(
            repo_dir=self.repo_dir, task="build", mode="build",
            execution_contract=contract, plan_id=plan.id, session_id="cli:local:default",
        )
        orch = Orchestrator(SyntheticS1Double(), SyntheticS2Double(), cfg)
        runner = StructuredStepRunner(self.repo_dir, orch, contract, plan.id)

        write_count = [0]
        orig_write = _safe_atomic_write_json

        def _fail_on_post_mutation_write(path: Any, payload: Any) -> None:
            write_count[0] += 1
            # 1 = initialize_steps, 2 = mark_running, 3 = mark_passed (after mutation)
            if write_count[0] >= 3:
                raise OSError("Disk write failed after mutation")
            orig_write(path, payload)

        with patch("core.runtime.step_runner._safe_atomic_write_json", side_effect=_fail_on_post_mutation_write):
            with self.assertRaises(OSError) as io_ctx:
                runner.run_steps([PlanStep("1", "Mutate mod1", ["src/mod1.py"])])
            self.assertIn("Disk write failed after mutation", str(io_ctx.exception))

        # Workspace file was mutated
        self.assertTrue((self.repo_dir / "src" / "mod1.py").exists())

        # Re-inspect progress on disk: never fabricated PASSED, preserved as RUNNING requiring reconciliation
        reloaded_tracker = PlanProgressTracker(self.repo_dir, plan.id)
        audit = reloaded_tracker.audit_step_recovery("1")
        self.assertEqual(audit.status, StepExecutionStatus.RUNNING)
        self.assertEqual(audit.mutation_timing, "after_mutation")
        self.assertTrue(audit.has_workspace_mutations)
        self.assertEqual(audit.classification, "requiring_reconciliation")
        self.assertFalse(audit.evidence_sufficient)

    # =========================================================================
    # Test 7: Duplicate Recovery Attempts From Two Processes
    # =========================================================================
    def test_duplicate_recovery_attempts_from_two_processes(self) -> None:
        """7. Concurrent recovery attempts are mutually excluded and recognize terminal reconciled state."""
        plan = self._create_sample_plan("plan_dup_recovery")
        tracker = PlanProgressTracker(self.repo_dir, plan.id)
        tracker.initialize_steps([{"id": "1", "description": "Interrupted step", "is_idempotent": False}])
        tracker.mark_interrupted("1", "Process was crashed")

        results = []
        errors = []

        def _recovery_worker(worker_id: str) -> None:
            try:
                local_tracker = PlanProgressTracker(self.repo_dir, plan.id)
                rec = local_tracker.reconcile_ambiguous_step(
                    "1",
                    new_status=StepExecutionStatus.FAILED,
                    operator=f"operator_{worker_id}",
                    operator_attestation=f"Reconciled by worker {worker_id}",
                )
                results.append((worker_id, rec.status, rec.reconciled_by))
            except Exception as exc:
                errors.append((worker_id, exc))

        t1 = threading.Thread(target=_recovery_worker, args=("worker_A",))
        t2 = threading.Thread(target=_recovery_worker, args=("worker_B",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Both workers executed safely without data corruption
        self.assertEqual(len(results), 2)
        # Final status on disk is strictly FAILED
        final_tracker = PlanProgressTracker(self.repo_dir, plan.id)
        final_rec = final_tracker.get_record("1")
        assert final_rec is not None
        self.assertEqual(final_rec.status, StepExecutionStatus.FAILED)
        self.assertIsNone(final_rec.lease_id, "Lease must be cleared on reconciliation")

    # =========================================================================
    # Test 8: Reconciliation With Missing or Unverifiable Evidence
    # =========================================================================
    def test_reconciliation_with_missing_or_unverifiable_evidence_fails_closed(self) -> None:
        """8. Reconciliation without operator identity, attestation, or on rolled back tx fails closed."""
        plan = self._create_sample_plan("plan_recon_evidence")
        tracker = PlanProgressTracker(self.repo_dir, plan.id)
        tracker.initialize_steps([{"id": "1", "description": "Step 1", "is_idempotent": False}])
        tracker.mark_interrupted("1", "Process crash")

        # 8A: Missing operator identity
        with self.assertRaises(ValueError) as ctx1:
            tracker.reconcile_ambiguous_step(
                "1",
                new_status=StepExecutionStatus.PASSED,
                operator="",
                operator_attestation="Valid attestation text",
            )
        self.assertIn("Operator identity", str(ctx1.exception))

        # 8B: Missing attestation / evidence
        with self.assertRaises(ValueError) as ctx2:
            tracker.reconcile_ambiguous_step(
                "1",
                new_status=StepExecutionStatus.PASSED,
                operator="lead_auditor",
                operator_attestation="   ",
            )
        self.assertIn("attestation or verification evidence", str(ctx2.exception).lower())

        # 8C: Non-terminal status target
        with self.assertRaises(ValueError) as ctx3:
            tracker.reconcile_ambiguous_step(
                "1",
                new_status=StepExecutionStatus.RUNNING,
                operator="lead_auditor",
                operator_attestation="Attempting to set non-terminal status",
            )
        self.assertIn("target status must be terminal", str(ctx3.exception).lower())

        # 8D: Cannot reconcile as PASSED if associated transaction rolled back
        with self.assertRaises(ValueError) as ctx4:
            tracker.reconcile_ambiguous_step(
                "1",
                new_status=StepExecutionStatus.PASSED,
                operator="lead_auditor",
                operator_attestation="Attempting to override rollback",
                transaction_status="rolled_back",
            )
        self.assertIn("rolled back or failed", str(ctx4.exception).lower())

    # =========================================================================
    # Test 9: Fresh Approval Required After Ambiguous Non-Idempotent Step
    # =========================================================================
    def test_fresh_approval_required_after_ambiguous_non_idempotent_step(self) -> None:
        """9. Invariant 5: Consumed approval cannot be reused after interruption; fresh approval required."""
        plan = self._create_sample_plan("plan_fresh_approval")
        contract, app_service, app_req = self._create_contract(plan, targets=["src/mod1.py", "src/mod2.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="local", reason="Start", contract=contract)

        tracker = PlanProgressTracker(self.repo_dir, plan.id)
        tracker.initialize_steps([
            {"id": "1", "description": "Mutate mod1", "files": ["src/mod1.py"], "is_idempotent": False},
            {"id": "2", "description": "Mutate mod2", "files": ["src/mod2.py"], "is_idempotent": False},
        ])
        tracker.mark_interrupted("1", "Process SIGKILL")

        # Step 1 is reconciled by operator as failed
        tracker.reconcile_ambiguous_step(
            "1",
            new_status=StepExecutionStatus.FAILED,
            operator="lead_operator",
            operator_attestation="Step 1 interrupted and marked failed",
        )

        # Attempting to re-consume the consumed contract for subsequent execution fails closed
        ok, _, reason = app_service.verify_and_consume(
            app_req.request_id,
            app_req.operation_digest,
            "cli:local:default",
            "cli",
            session_incarnation_id="inc_4b2_1",
            requester_id="local",
        )
        self.assertFalse(ok, "Re-consumption of consumed approval contract must fail closed")
        self.assertIn("already been consumed", reason.lower())

        # A fresh approval must be requested, approved, and consumed
        new_op = CanonicalOperation("write_files", "src/mod2.py", {"files": ["src/mod2.py"]})
        new_req = app_service.create_request(
            session_id="cli:local:default",
            channel="cli",
            user_id="local",
            conversation_id="default",
            operation_type="write_files",
            canonical_operation=new_op,
            risk_class=RiskClass.MEDIUM.value,
            session_incarnation_id="inc_4b2_2",
            capabilities=Capabilities(filesystem=FilesystemPolicy(write=("src/mod2.py",))),
            workspace_root=str(self.repo_dir),
            plan_id=plan.id,
            plan_revision=plan.revision,
            plan_content_hash=plan.content_hash,
        )
        app_service.approve(new_req.request_id, "security_lead", "cli")
        ok_fresh, consumed_fresh, _ = app_service.verify_and_consume(
            new_req.request_id,
            new_req.operation_digest,
            "cli:local:default",
            "cli",
            session_incarnation_id="inc_4b2_2",
            requester_id="local",
        )
        self.assertTrue(ok_fresh)
        assert consumed_fresh is not None
        self.assertEqual(consumed_fresh.status, ApprovalStatus.CONSUMED)

    # =========================================================================
    # Test 10: Unknown Operation Type or Untrusted Idempotency Claims
    # =========================================================================
    def test_unknown_operation_type_and_untrusted_idempotency_claims_rejected(self) -> None:
        """10. Invariant 6: Untrusted model labels or unknown action types fail idempotency check."""
        adversarial_untrusted_steps = [
            {"id": "u1", "action_type": "unknown_custom_plugin", "is_idempotent": True},
            {"id": "u2", "action_type": "arbitrary_eval", "is_idempotent": True, "command": ["rm", "-rf", "src"]},
            {"id": "u3", "action_type": "write_files", "is_idempotent": True, "files": ["src/mod1.py"]},
            {"id": "u4", "action_type": "status_check", "is_idempotent": True, "command": "python -c 'exit(0)'"},  # String cmd
            {"id": "u5", "action_type": "read_check", "is_idempotent": True, "mutates_files": True},
            {"id": "u6", "action_type": "verify", "is_idempotent": True, "command": ["git", "clean", "-fdx"]},  # Mutating git
            {"id": "u7", "action_type": "verify", "is_idempotent": True, "command": ["python", "-m", "pytest"]},  # Test runner
            PlanStep("u8", "PlanStep mutation", ["src/mod1.py"]),
        ]

        for step in adversarial_untrusted_steps:
            s_id = getattr(step, "id", None) if not isinstance(step, dict) else step.get("id")
            self.assertFalse(
                is_step_safely_idempotent(step, cwd=self.repo_dir),
                f"Step {s_id} with untrusted idempotency claim must be rejected as NOT idempotent",
            )


if __name__ == "__main__":
    unittest.main()
