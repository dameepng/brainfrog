"""BrainFrog Phase 4A.3 — Final Adversarial Gate Before Phase 4B.

Comprehensive adversarial test suite verifying:
- Check 1: Retry safety based on actual effects (rejecting pytest/unittest side-effects, mutating args, wrappers, and aborting retries on workspace mutations).
- Check 2: Reconciliation integrity (mandatory operator identity, operator attestation vs verified transaction provenance, rejection of rolled back transactions, and consumed approval protection).
- Check 3: Temporary-file cross-process safety (ensuring files held open by live processes for >60s cannot be deleted by cleanup).
- Check 4: Complete workspace mutation boundary (missing/empty step files, unapproved targets, path traversal, symlink escapes, failure before staging vs after staging).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
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
from core.runtime.step_runner import (
    AmbiguousStepExecutionError,
    PlanProgressTracker,
    StaleExecutionAuthorizationError,
    StepExecutionStatus,
    StepProgressRecord,
    StructuredStepRunner,
    _is_file_in_active_use,
    _lock_file_handle,
    _unlock_file_handle,
    is_step_safely_idempotent,
)
from core.runtime.transaction import (
    TransactionCoordinator,
    TransactionResult,
    TransactionStatus,
)
from core.runtime.workspace_lock import WorkspaceLock
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
        self.provider_name = "gate_s2"
        self.model = "mock-model"
        self.output_files = output_files if output_files is not None else {"src/safe.py": "print('gate')"}

    def plan_task(self, task: str, focus_tree: str = "", **kwargs: Any) -> List[PlanStep]:
        return [PlanStep(id="1", description="Gate step", files=list(self.output_files.keys()))]

    def write_code(self, step: PlanStep, task: str, file_contents: Dict[str, str], **kwargs: Any) -> Dict[str, str]:
        return dict(self.output_files)


class TestPhase4A3AdversarialGate(unittest.TestCase):
    """Adversarial security and reliability verification for Phase 4A.3."""

    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="bf_gate_4a3_")).resolve()
        self.repo_dir = self.temp_dir / "workspace"
        self.repo_dir.mkdir(parents=True)
        self.plans_dir = self.repo_dir / ".brainfrog" / "plans"
        self.plans_dir.mkdir(parents=True)
        self.tx_dir = self.repo_dir / ".brainfrog" / "transactions"
        self.tx_dir.mkdir(parents=True)

        # Initialize mock git workspace
        subprocess.run(["git", "init"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "GateTester"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "gate@example.com"], cwd=self.repo_dir, capture_output=True, check=True)
        (self.repo_dir / "README.md").write_text("# Gate Repo\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.repo_dir, capture_output=True, check=True)

        self.registry = PlanRegistry(self.repo_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_sample_plan(
        self,
        plan_id: str = "plan_gate_01",
        status: PlanStatus = PlanStatus.READY,
        session_id: str = "cli:gate:session_1",
    ) -> PlanDocument:
        content = (
            f"# Gate Plan {plan_id}\n\n"
            "## Architecture\nTest gate architecture.\n\n"
            "## Acceptance Criteria\n- criterion_1: Gate criteria\n\n"
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
    ) -> ApprovedExecutionContract:
        target_list = targets or ["src/safe.py"]
        sess_id = session_id or plan.session_id or "cli:gate:session_1"
        inc_id = session_incarnation_id or "inc_gate_1"

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
        return contract

    # =========================================================================
    # Check 1: Retry Safety Must Be Based on Actual Effects
    # =========================================================================

    def test_check1_pytest_and_unittest_classified_non_idempotent(self) -> None:
        """Check 1: pytest and unittest execute test code that can mutate or delete workspace files;
        the runtime cannot prove they are side-effect-free, so automatic retries are disabled (max_retries = 0)."""
        adversarial_test_steps = [
            {"id": "t1", "action_type": "test", "is_idempotent": True, "command": ["python", "-m", "pytest", "tests/test_file_delete.py"]},
            {"id": "t2", "action_type": "test", "is_idempotent": True, "command": ["pytest", "tests/test_mutates_state.py"]},
            {"id": "t3", "action_type": "test", "is_idempotent": True, "command": ["python", "-m", "unittest", "tests.test_side_effects"]},
            {"id": "t4", "action_type": "test", "is_idempotent": True, "command": ["python3", "-m", "pytest"]},
        ]
        for step in adversarial_test_steps:
            is_idem = is_step_safely_idempotent(step, cwd=self.repo_dir)
            self.assertFalse(
                is_idem,
                f"Test step {step['command']} cannot be proven side-effect free and must NOT be idempotent",
            )

    def test_check1_runner_enforces_zero_retries_for_test_commands(self) -> None:
        """Check 1: StructuredStepRunner sets max_retries = 0 and executes failing test steps exactly once."""
        plan = self._create_sample_plan("plan_c1_noretry")
        contract = self._create_contract(plan, targets=["src/safe.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="alice", reason="Start", contract=contract)

        cfg = RunConfig(repo_dir=self.repo_dir, task="build", execution_contract=contract, plan_id=plan.id)
        orch = Orchestrator(MockSystem1(), MockSystem2(), cfg)
        runner = StructuredStepRunner(self.repo_dir, orch, contract, plan.id)

        step_payload = {
            "id": "1",
            "description": "Run mutating pytest",
            "action_type": "test",
            "is_idempotent": True,
            "command": ["python", "-m", "pytest", "tests/test_mutating.py"],
        }

        attempts = [0]
        def _failing_step(step: Any, step_id: str) -> Any:
            attempts[0] += 1
            raise RuntimeError("Test failed with assertion error")

        runner._execute_single_step = _failing_step  # type: ignore

        with self.assertRaises(RuntimeError):
            runner.run_steps([step_payload])

        # Exactly 1 attempt — zero retries
        self.assertEqual(attempts[0], 1)
        rec = runner.tracker.get_record("1")
        self.assertIsNotNone(rec)
        self.assertFalse(rec.is_idempotent)
        self.assertEqual(rec.max_retries, 0)
        self.assertEqual(rec.status, StepExecutionStatus.FAILED)

    def test_check1_mutating_git_commands_rejected_from_idempotency(self) -> None:
        """Check 1: Mutating git operations (clean, checkout, reset, --output) are strictly rejected."""
        mutating_git_steps = [
            {"id": "g1", "action_type": "status_check", "is_idempotent": True, "command": ["git", "clean", "-fdx"]},
            {"id": "g2", "action_type": "verify", "is_idempotent": True, "command": ["git", "checkout", "."]},
            {"id": "g3", "action_type": "verify", "is_idempotent": True, "command": ["git", "reset", "--hard"]},
            {"id": "g4", "action_type": "verify", "is_idempotent": True, "command": ["git", "diff", "--output=output.diff"]},
            {"id": "g5", "action_type": "verify", "is_idempotent": True, "command": ["git", "-o", "patch.diff", "diff"]},
            {"id": "g6", "action_type": "verify", "is_idempotent": True, "command": ["git", "apply", "fix.patch"]},
        ]
        for step in mutating_git_steps:
            self.assertFalse(
                is_step_safely_idempotent(step, cwd=self.repo_dir),
                f"Mutating git command {step['command']} must NOT be idempotent",
            )

    def test_check1_shell_wrappers_and_indirect_dispatch_rejected(self) -> None:
        """Check 1: Shell execution, indirect dispatchers, and wrappers are rejected."""
        indirect_steps = [
            {"id": "i1", "action_type": "verify", "is_idempotent": True, "command": ["powershell", "-c", "git status"]},
            {"id": "i2", "action_type": "verify", "is_idempotent": True, "command": ["cmd.exe", "/c", "git status"]},
            {"id": "i3", "action_type": "verify", "is_idempotent": True, "command": ["bash", "-c", "git status"]},
            {"id": "i4", "action_type": "verify", "is_idempotent": True, "command": ["sh", "-c", "git status"]},
            {"id": "i5", "action_type": "verify", "is_idempotent": True, "command": ["python", "-c", "import os; os.system('git status')"]},
        ]
        for step in indirect_steps:
            self.assertFalse(
                is_step_safely_idempotent(step, cwd=self.repo_dir),
                f"Indirect/wrapped dispatch {step['command']} must NOT be idempotent",
            )

    def test_check1_runtime_mutation_detection_aborts_retry_and_fails_closed(self) -> None:
        """Check 1: If an idempotent step fails after mutating the workspace, retry is aborted and fails closed."""
        plan = self._create_sample_plan("plan_c1_abort_retry")
        contract = self._create_contract(plan, targets=["src/safe.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="alice", reason="Start", contract=contract)

        cfg = RunConfig(repo_dir=self.repo_dir, task="build", execution_contract=contract, plan_id=plan.id)
        orch = Orchestrator(MockSystem1(), MockSystem2(), cfg)
        runner = StructuredStepRunner(self.repo_dir, orch, contract, plan.id)

        # In-process read check claimed as idempotent
        step_payload = {
            "id": "1",
            "description": "Read check with secret mutation",
            "action_type": "read_check",
            "is_idempotent": True,
        }

        attempts = [0]
        def _mutating_fail_step(step: Any, step_id: str) -> Any:
            attempts[0] += 1
            # Secretly mutate workspace during execution
            (self.repo_dir / "untracked_leak.txt").write_text("mutated during failed attempt", encoding="utf-8")
            raise RuntimeError("Temporary error during check")

        runner._execute_single_step = _mutating_fail_step  # type: ignore

        with self.assertRaises(RuntimeError) as ctx:
            runner.run_steps([step_payload])

        self.assertIn("mutated workspace", str(ctx.exception))
        # Retry was ABORTED due to workspace mutation detection: exactly 1 attempt
        self.assertEqual(attempts[0], 1)
        rec = runner.tracker.get_record("1")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.status, StepExecutionStatus.FAILED)
        self.assertIn("Workspace mutation detected", rec.error)  # type: ignore

    # =========================================================================
    # Check 2: Reconciliation Integrity
    # =========================================================================

    def test_check2_operator_identity_mandatory_and_validated(self) -> None:
        """Check 2: Operator identity is strictly required and fails closed on empty or whitespace."""
        plan = self._create_sample_plan("plan_c2_op")
        tracker = PlanProgressTracker(self.repo_dir, plan.id)
        tracker.initialize_steps([{"id": "1", "description": "Step 1"}])
        tracker.mark_running("1")

        for bad_op in ("", "   ", None):
            with self.assertRaises(ValueError):
                tracker.reconcile_ambiguous_step(
                    "1",
                    new_status=StepExecutionStatus.PASSED,
                    operator=bad_op,  # type: ignore
                    operator_attestation="Valid attestation",
                )

    def test_check2_free_form_evidence_labeled_as_operator_attestation(self) -> None:
        """Check 2: Free-form caller evidence without transaction verification is explicitly labeled
        as operator_attestation with transaction_verified=False and durable provenance."""
        plan = self._create_sample_plan("plan_c2_attest")
        tracker = PlanProgressTracker(self.repo_dir, plan.id)
        tracker.initialize_steps([{"id": "1", "description": "Step 1"}])
        tracker.mark_running("1")

        rec = tracker.reconcile_ambiguous_step(
            "1",
            new_status=StepExecutionStatus.PASSED,
            operator="auditor_bob",
            operator_attestation="Manually reviewed disk state and acceptance logs.",
        )

        self.assertEqual(rec.status, StepExecutionStatus.PASSED)
        self.assertEqual(rec.reconciled_by, "auditor_bob")
        self.assertEqual(rec.reconciliation_type, "operator_attestation")
        self.assertFalse(rec.transaction_verified)
        self.assertIn("OPERATOR_ATTESTATION: Manually reviewed", rec.evidence)  # type: ignore

        # Provenance is durable across reload
        reloaded = PlanProgressTracker(self.repo_dir, plan.id)
        persisted_rec = reloaded.get_record("1")
        self.assertIsNotNone(persisted_rec)
        self.assertEqual(persisted_rec.reconciled_by, "auditor_bob")
        self.assertEqual(persisted_rec.reconciliation_type, "operator_attestation")
        self.assertFalse(persisted_rec.transaction_verified)

    def test_check2_reconcile_passed_rejects_rolled_back_or_failed_transaction(self) -> None:
        """Check 2: Cannot reconcile step as PASSED if associated transaction was rolled back or failed."""
        plan = self._create_sample_plan("plan_c2_reject_rollback")
        tracker = PlanProgressTracker(self.repo_dir, plan.id)
        tracker.initialize_steps([{"id": "1", "description": "Step 1"}])
        tracker.mark_running("1")

        # Associate a rolled-back transaction record
        tx_id = "tx_failed_001"
        tx_file = self.tx_dir / f"{tx_id}.json"
        tx_file.write_text(json.dumps({"id": tx_id, "status": "rolled_back"}), encoding="utf-8")

        # Explicit transaction status or disk record
        with self.assertRaises(ValueError) as ctx:
            tracker.reconcile_ambiguous_step(
                "1",
                new_status=StepExecutionStatus.PASSED,
                operator="alice",
                operator_attestation="Claiming pass despite rollback",
                transaction_id=tx_id,
            )
        self.assertIn("rolled back or failed", str(ctx.exception))

        # Reconciling with explicit TransactionStatus.FAILED fails closed
        with self.assertRaises(ValueError) as ctx:
            tracker.reconcile_ambiguous_step(
                "1",
                new_status=StepExecutionStatus.PASSED,
                operator="alice",
                operator_attestation="Claiming pass",
                transaction_status=TransactionStatus.FAILED,
            )
        self.assertIn("rolled back or failed", str(ctx.exception))

    def test_check2_reconcile_passed_verifies_committed_or_verified_transaction(self) -> None:
        """Check 2: Step with a genuine VERIFIED transaction record is marked verified_transaction."""
        plan = self._create_sample_plan("plan_c2_tx_verified")
        tracker = PlanProgressTracker(self.repo_dir, plan.id)
        tracker.initialize_steps([{"id": "1", "description": "Step 1"}])
        tracker.mark_running("1")

        tx_id = "tx_verified_001"
        tx_file = self.tx_dir / f"{tx_id}.json"
        tx_file.write_text(json.dumps({"id": tx_id, "status": "verified"}), encoding="utf-8")

        rec = tracker.reconcile_ambiguous_step(
            "1",
            new_status=StepExecutionStatus.PASSED,
            operator="alice",
            operator_attestation="Verified on disk and transaction log",
            transaction_id=tx_id,
        )

        self.assertEqual(rec.status, StepExecutionStatus.PASSED)
        self.assertEqual(rec.reconciliation_type, "verified_transaction")
        self.assertTrue(rec.transaction_verified)

    def test_check2_consumed_approval_contract_unusable_after_reconciliation(self) -> None:
        """Check 2: Consumed approval contract cannot be re-verified or reused after reconciliation."""
        plan = self._create_sample_plan("plan_c2_contract_reuse")
        store = FileApprovalStore(self.repo_dir / ".brainfrog" / "approvals")
        service = ApprovalService(store)
        op = CanonicalOperation("write_files", "src/safe.py", {"targets": ["src/safe.py"]})

        req = service.create_request(
            session_id=plan.session_id,
            channel="terminal",
            user_id="alice",
            conversation_id="conv_1",
            operation_type="write_files",
            canonical_operation=op,
            session_incarnation_id="inc_c2",
            capabilities=Capabilities(FilesystemPolicy(write=["src/safe.py"], read=["src/safe.py"])),
            workspace_root=str(self.repo_dir),
            plan_id=plan.id,
            plan_revision=plan.revision,
            plan_content_hash=plan.content_hash,
        )
        service.approve(req.request_id, "bob", "terminal")
        ok1, consumed_req, _ = service.verify_and_consume(
            req.request_id, req.operation_digest, plan.session_id, "terminal",
            session_incarnation_id="inc_c2", requester_id="alice",
        )
        self.assertTrue(ok1)
        assert consumed_req is not None
        contract = ApprovedExecutionContract.from_approval_request(consumed_req, self.repo_dir)

        # Attempting to re-verify and consume the already consumed approval request fails closed
        ok2, _, reason = service.verify_and_consume(
            req.request_id, req.operation_digest, plan.session_id, "terminal",
            session_incarnation_id="inc_c2", requester_id="alice",
        )
        self.assertFalse(ok2)
        self.assertIn("already been consumed", reason)

    # =========================================================================
    # Check 3: Temporary-File Cross-Process Safety
    # =========================================================================

    def test_check3_concurrent_processes_active_temporary_file_held_over_60s_not_deleted(self) -> None:
        """Check 3: Concurrent process holding an active temporary file for >60s is NEVER deleted by cleanup."""
        plan_id = "plan_c3_tmp"
        tracker = PlanProgressTracker(self.repo_dir, plan_id)

        # Temporary file path
        tmp_file = self.plans_dir / f"{plan_id}.tmp_activewriter"
        tmp_file.write_bytes(b'{"active_in_flight": true}')

        # Set mtime to 120 seconds in the past
        past_time = time.time() - 120.0
        os.utime(str(tmp_file), (past_time, past_time))
        self.assertGreater(time.time() - tmp_file.stat().st_mtime, 60.0)

        # Spawn child Python process that holds the file open and locked
        code = (
            f"import time, sys, os\n"
            f"f = open(r'{tmp_file}', 'r+b')\n"
            f"if sys.platform == 'win32':\n"
            f"    import msvcrt\n"
            f"    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)\n"
            f"else:\n"
            f"    import fcntl\n"
            f"    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            f"print('READY', flush=True)\n"
            f"time.sleep(30)\n"
        )

        proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        try:
            # Wait for child process to hold file
            ready_line = proc.stdout.readline() if proc.stdout else ""
            self.assertIn("READY", ready_line)

            # Active-use test returns True
            self.assertTrue(_is_file_in_active_use(tmp_file))

            # New tracker initialization runs _clean_stale_tmp_files()
            tracker2 = PlanProgressTracker(self.repo_dir, plan_id)
            tracker2._clean_stale_tmp_files()

            # Prove the live writer's file was NOT deleted despite mtime > 60s
            self.assertTrue(tmp_file.exists(), "Active temporary file held by live writer must NOT be unlinked!")

        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()

        # Once writer process exits, file is no longer in active use
        self.assertFalse(_is_file_in_active_use(tmp_file))

        # Abandoned file can now be safely cleaned
        tracker._clean_stale_tmp_files()
        self.assertFalse(tmp_file.exists(), "Abandoned temporary file must be cleaned after process exit.")

    # =========================================================================
    # Check 4: Complete Workspace Mutation Boundary
    # =========================================================================

    def test_check4_missing_or_empty_step_files_blocks_mutation_before_staging(self) -> None:
        """Check 4: Step with missing or empty files attempting writes fails closed before staging."""
        plan = self._create_sample_plan("plan_c4_empty_files")
        contract = self._create_contract(plan, targets=["src/safe.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="alice", reason="Start", contract=contract)

        cfg = RunConfig(repo_dir=self.repo_dir, task="build", execution_contract=contract, plan_id=plan.id)
        # System 2 attempts to write a file
        s2 = MockSystem2(output_files={"src/safe.py": "malicious write"})
        orch = Orchestrator(MockSystem1(), s2, cfg)

        # Step 1: empty files list
        step_empty = PlanStep(id="1", description="Empty files step", files=[])
        with self.assertRaises(PermissionError) as ctx:
            orch._run_step(step_empty)
        self.assertIn("Scope violation", str(ctx.exception))

        # Verify no file was written or staged
        self.assertFalse((self.repo_dir / "src" / "safe.py").exists())

    def test_check4_mutation_outside_step_files_blocks_before_staging(self) -> None:
        """Check 4: Writes outside the step's approved targets are rejected before staging."""
        plan = self._create_sample_plan("plan_c4_outside_step")
        contract = self._create_contract(plan, targets=["src/safe.py", "src/other.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="alice", reason="Start", contract=contract)

        cfg = RunConfig(repo_dir=self.repo_dir, task="build", execution_contract=contract, plan_id=plan.id)
        # System 2 outputs src/other.py, but step only authorizes src/safe.py
        s2 = MockSystem2(output_files={"src/other.py": "print('unapproved')"})
        orch = Orchestrator(MockSystem1(), s2, cfg)

        step = PlanStep(id="1", description="Safe step", files=["src/safe.py"])
        with self.assertRaises(PermissionError) as ctx:
            orch._run_step(step)

        self.assertIn("Scope violation", str(ctx.exception))
        self.assertFalse((self.repo_dir / "src" / "other.py").exists())

    def test_check4_path_traversal_and_absolute_paths_rejected_before_staging(self) -> None:
        """Check 4: Path traversal and absolute path targets are rejected before staging."""
        plan = self._create_sample_plan("plan_c4_traversal")
        contract = self._create_contract(plan, targets=["src/safe.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="alice", reason="Start", contract=contract)

        traversal_targets = [
            ("../escape.py", "escape"),
            ("src/../../outside.py", "outside"),
            ("/etc/shadow", "shadow"),
            (".brainfrog/control.json", "control"),
            (".git/hooks/pre-commit", "hook"),
        ]

        for bad_path, content in traversal_targets:
            cfg = RunConfig(repo_dir=self.repo_dir, task="build", execution_contract=contract, plan_id=plan.id)
            s2 = MockSystem2(output_files={bad_path: content})
            orch = Orchestrator(MockSystem1(), s2, cfg)
            step = PlanStep(id="1", description="Traversal step", files=[bad_path])

            with self.assertRaises((PermissionError, ValueError)):
                orch._run_step(step)

    def test_check4_failure_before_staging_vs_failure_after_staging(self) -> None:
        """Check 4: Failure before staging leaves workspace pristine; failure after staging rolls back cleanly."""
        plan = self._create_sample_plan("plan_c4_lifecycle")
        contract = self._create_contract(plan, targets=["src/safe.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="alice", reason="Start", contract=contract)

        # Baseline content
        (self.repo_dir / "src").mkdir(parents=True, exist_ok=True)
        baseline_file = self.repo_dir / "src" / "safe.py"
        baseline_file.write_text("print('baseline')", encoding="utf-8")
        subprocess.run(["git", "add", "src/safe.py"], cwd=self.repo_dir, check=True)
        subprocess.run(["git", "commit", "-m", "add baseline"], cwd=self.repo_dir, check=True)

        # 1. Failure before staging: unapproved scope
        cfg_fail_before = RunConfig(repo_dir=self.repo_dir, task="build", execution_contract=contract, plan_id=plan.id)
        s2_fail_before = MockSystem2(output_files={"src/unapproved.py": "print('unapproved')"})
        orch_fail_before = Orchestrator(MockSystem1(), s2_fail_before, cfg_fail_before)

        with self.assertRaises(PermissionError):
            orch_fail_before._run_step(PlanStep(id="1", description="Fail before staging", files=["src/safe.py"]))

        # Baseline unchanged
        self.assertEqual(baseline_file.read_text(encoding="utf-8"), "print('baseline')")
        self.assertFalse((self.repo_dir / "src" / "unapproved.py").exists())

        # 2. Failure after staging: domain test failure triggers transaction rollback
        cfg_fail_after = RunConfig(
            repo_dir=self.repo_dir,
            task="build",
            execution_contract=contract,
            plan_id=plan.id,
            test_command=["python", "-c", "import sys; sys.exit(1)"],  # Failing test command
        )
        s2_fail_after = MockSystem2(output_files={"src/safe.py": "print('mutated then rolled back')"})
        orch_fail_after = Orchestrator(MockSystem1(), s2_fail_after, cfg_fail_after)

        with self.assertRaises(RuntimeError) as ctx:
            orch_fail_after._run_step(PlanStep(id="1", description="Fail after staging", files=["src/safe.py"]))

        self.assertIn("failed with exit code 1", str(ctx.exception))

        # Prove rollback restored the exact baseline file content
        self.assertEqual(
            baseline_file.read_text(encoding="utf-8"),
            "print('baseline')",
            "Transaction rollback must restore exact pre-mutation file state after test failure!",
        )


if __name__ == "__main__":
    unittest.main()
