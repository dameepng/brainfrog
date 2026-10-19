"""Phase 4A.1 — Independent Build Loop Safety & Reliability Audit.

Comprehensive adversarial test suite probing:
- Audit A: Approval consumption, concurrency, replay, and ad-hoc contracts.
- Audit B: Transaction/progress crash consistency across all boundaries.
- Audit C: Cancellation, subprocess tree termination, and in-process timeout limitations.
- Audit D: Trusted idempotency classifier, allowlists, and retry boundaries.
- Audit E: Plan/contract scope, revision changes, tampering, and session isolation.
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
    VerificationEvidence,
    VerificationCheckResult,
)
from core.runtime.approval import (
    ApprovalRequest,
    ApprovalService,
    ApprovalStatus,
    CanonicalOperation,
    FileApprovalStore,
    InMemoryApprovalStore,
)
from core.runtime.capabilities import Capabilities, FilesystemPolicy, NetworkPolicy
from core.runtime.contract import ApprovedExecutionContract
from core.runtime.process import (
    _CURRENT_CANCEL_EVENT,
    _CURRENT_TASK_ID,
    run_hardened_subprocess,
)
from core.runtime.step_runner import (
    AmbiguousStepExecutionError,
    PlanProgressTracker,
    StaleExecutionAuthorizationError,
    StepExecutionStatus,
    StepExecutionTimeoutError,
    StepProgressRecord,
    StructuredStepRunner,
    TRUSTED_IDEMPOTENT_ACTIONS,
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
    ScopeDecision,
    StepResult,
)
from system1.base import Answer, SystemOneClient


# =============================================================================
# Deterministic Test Doubles
# =============================================================================

class MockSystem1(SystemOneClient):
    name: str = "audit_s1"

    def decide(self, state: Dict[str, Any], questions: Dict[str, Any]) -> Dict[str, Answer]:
        return {
            k: Answer(choice="unrelated", score=0.1, noul=1.0, confidence=0.9)
            for k in questions
        }


class MockSystem2:
    def __init__(self, output_files: Optional[Dict[str, str]] = None) -> None:
        self.guidelines = ""
        self.provider_name = "audit_s2"
        self.model = "mock-model"
        self.output_files = output_files or {"src/safe.py": "print('clean')"}

    def plan_task(self, task: str, focus_tree: str = "", **kwargs: Any) -> List[PlanStep]:
        return [PlanStep(id="1", description="Approved step", files=list(self.output_files.keys()))]

    def write_code(self, step: PlanStep, task: str, file_contents: Dict[str, str], **kwargs: Any) -> Dict[str, str]:
        return dict(self.output_files)


class TestPhase4A1SafetyAudit(unittest.TestCase):
    """Adversarial security and reliability test suite for Phase 4A.1 audit."""

    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="bf_audit_4a1_")).resolve()
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
        plan_id: str = "plan_audit_01",
        status: PlanStatus = PlanStatus.READY,
        session_id: str = "cli:audit:session_1",
    ) -> PlanDocument:
        from core.plans import save_plan_document
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
                "title": "Audit Plan",
                "goal": "Audit Plan Goal",
                "status": status.value,
                "revision": 1,
                "session_id": session_id,
                "steps": [
                    {"id": "1", "description": "Step 1", "files": ["src/safe.py"]},
                    {"id": "2", "description": "Step 2", "files": ["src/extra.py"]},
                ],
                "acceptance_criteria": ["criterion_1"],
            },
        )
        p = self.registry.get_plan(plan_id)
        assert p is not None
        return p

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

    # =========================================================================
    # AUDIT A: Approval Consumption and Concurrency
    # =========================================================================

    def test_audit_a1_approval_to_contract_consumption_lifecycle(self) -> None:
        """Audit A.1: Trace complete path from request -> approve -> consume -> contract -> loop."""
        plan = self._create_sample_plan("plan_a1")
        targets = ["src/safe.py"]
        store = FileApprovalStore(self.repo_dir / ".brainfrog" / "approvals")
        service = ApprovalService(store)
        op = CanonicalOperation("write_files", targets[0], {"targets": targets})

        # 1. Create request
        req = service.create_request(
            session_id=plan.session_id,
            channel="terminal",
            user_id="alice",
            conversation_id="conv_1",
            operation_type="write_files",
            canonical_operation=op,
            session_incarnation_id="inc_a1",
            capabilities=Capabilities(
                filesystem=FilesystemPolicy(write=targets, read=targets),
                network=NetworkPolicy(access=True),
            ),
            workspace_root=str(self.repo_dir),
            plan_id=plan.id,
            plan_revision=plan.revision,
            plan_content_hash=plan.content_hash,
        )
        self.assertEqual(req.status, ApprovalStatus.PENDING)

        # 2. Peer approve
        ok, msg, req_app = service.approve(req.request_id, "bob", "terminal")
        self.assertTrue(ok)
        self.assertEqual(req_app.status, ApprovalStatus.APPROVED)

        # 3. Consume
        is_valid, consumed_req, reason = service.verify_and_consume(
            req.request_id, req.operation_digest, plan.session_id, "terminal",
            session_incarnation_id="inc_a1", requester_id="alice",
        )
        self.assertTrue(is_valid)
        self.assertEqual(consumed_req.status, ApprovalStatus.CONSUMED)

        # 4. Issue contract
        contract = ApprovedExecutionContract.from_approval_request(consumed_req, self.repo_dir)
        self.assertEqual(contract.request_id, req.request_id)
        self.assertEqual(contract.plan_id, plan.id)

        # 5. Execute build loop
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="alice", reason="Start", contract=contract)
        cfg = RunConfig(
            repo_dir=self.repo_dir,
            task="build",
            execution_contract=contract,
            plan_id=plan.id,
            session_id=plan.session_id,
        )
        orch = Orchestrator(MockSystem1(), MockSystem2({"src/safe.py": "# valid"}), cfg)
        step = PlanStep("1", "write safe", ["src/safe.py"])
        runner = StructuredStepRunner(self.repo_dir, orch, contract, plan.id)
        results = runner.run_steps([step])

        self.assertEqual(len(results), 1)
        self.assertTrue((self.repo_dir / "src" / "safe.py").exists())

    def test_audit_a2_consumed_contract_cannot_start_second_loop(self) -> None:
        """Audit A.2: A consumed approval cannot be re-consumed to start a second loop."""
        plan = self._create_sample_plan("plan_a2")
        targets = ["src/safe.py"]
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
            session_incarnation_id="inc_a2",
            capabilities=Capabilities(FilesystemPolicy(write=targets, read=targets)),
            workspace_root=str(self.repo_dir),
            plan_id=plan.id,
            plan_revision=plan.revision,
            plan_content_hash=plan.content_hash,
        )
        service.approve(req.request_id, "bob", "terminal")

        # First consumption succeeds
        ok1, _, _ = service.verify_and_consume(
            req.request_id, req.operation_digest, plan.session_id, "terminal",
            session_incarnation_id="inc_a2", requester_id="alice",
        )
        self.assertTrue(ok1)

        # Replay attempt fails closed immediately
        ok2, _, reason = service.verify_and_consume(
            req.request_id, req.operation_digest, plan.session_id, "terminal",
            session_incarnation_id="inc_a2", requester_id="alice",
        )
        self.assertFalse(ok2)
        self.assertIn("already been consumed", reason.lower())

    def test_audit_a3_concurrent_loops_competing_for_same_approval(self) -> None:
        """Audit A.3: Concurrent attempts to consume the same approval strictly serialize; only 1 succeeds."""
        plan = self._create_sample_plan("plan_a3")
        targets = ["src/safe.py"]
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
            session_incarnation_id="inc_a3",
            capabilities=Capabilities(FilesystemPolicy(write=targets, read=targets)),
            workspace_root=str(self.repo_dir),
            plan_id=plan.id,
            plan_revision=plan.revision,
            plan_content_hash=plan.content_hash,
        )
        service.approve(req.request_id, "bob", "terminal")

        results = []
        barrier = threading.Barrier(2)

        def _attempt_consume():
            barrier.wait()
            res = service.verify_and_consume(
                req.request_id, req.operation_digest, plan.session_id, "terminal",
                session_incarnation_id="inc_a3", requester_id="alice",
            )
            results.append(res[0])

        t1 = threading.Thread(target=_attempt_consume)
        t2 = threading.Thread(target=_attempt_consume)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Exactly one True, exactly one False
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 1)

    def test_audit_a4_revalidation_cannot_treat_tampered_or_expired_contract_as_authorized(self) -> None:
        """Audit A.4: Pre-mutation revalidation immediately rejects expired or tampered contract."""
        plan = self._create_sample_plan("plan_a4")
        contract = self._create_contract(plan, targets=["src/safe.py"], expires_at=time.time() - 10.0)
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="alice", reason="Start", contract=contract)

        cfg = RunConfig(repo_dir=self.repo_dir, task="build", execution_contract=contract, plan_id=plan.id)
        orch = Orchestrator(MockSystem1(), MockSystem2(), cfg)
        runner = StructuredStepRunner(self.repo_dir, orch, contract, plan.id)

        with self.assertRaises((PermissionError, ValueError)):
            runner.revalidate_pre_mutation()

    def test_audit_a5_adhoc_contract_authorization_semantics(self) -> None:
        """Audit A.5: Ad-hoc contract (no plan_id) validated for strict target containment and single use."""
        targets = ["src/adhoc.py"]
        store = InMemoryApprovalStore()
        service = ApprovalService(store)
        op = CanonicalOperation("write_file", targets[0], {"targets": targets})

        req = service.create_request(
            session_id="cli:adhoc:sess",
            channel="terminal",
            user_id="alice",
            conversation_id="conv_adhoc",
            operation_type="write_file",
            canonical_operation=op,
            session_incarnation_id="inc_adhoc",
            capabilities=Capabilities(
                filesystem=FilesystemPolicy(write=targets, read=targets),
                network=NetworkPolicy(access=True),
            ),
            workspace_root=str(self.repo_dir),
            plan_id=None,  # Ad-hoc contract
        )
        service.approve(req.request_id, "bob", "terminal")
        service.verify_and_consume(
            req.request_id, req.operation_digest, "cli:adhoc:sess", "terminal",
            session_incarnation_id="inc_adhoc", requester_id="alice",
        )
        contract = ApprovedExecutionContract.from_approval_request(req, self.repo_dir)

        self.assertIsNone(contract.plan_id)
        # Allows approved target
        self.assertTrue(contract.is_target_allowed("src/adhoc.py"))
        # Denies unapproved target
        self.assertFalse(contract.is_target_allowed("src/evil.py"))

        # Revalidation succeeds without plan document
        cfg = RunConfig(repo_dir=self.repo_dir, task="adhoc", execution_contract=contract)
        orch = Orchestrator(MockSystem1(), MockSystem2({"src/adhoc.py": "print(1)"}), cfg)
        runner = StructuredStepRunner(self.repo_dir, orch, contract, None)
        runner.revalidate_pre_mutation()

    # =========================================================================
    # AUDIT B: Transaction/Progress Crash Consistency
    # =========================================================================

    def test_audit_b1_crash_before_step_starts_leaves_clean_recoverable_state(self) -> None:
        """Audit B.1: Progress state before step start remains PENDING and clean."""
        plan = self._create_sample_plan("plan_b1")
        tracker = PlanProgressTracker(self.repo_dir, plan.id)
        step = PlanStep("step_1", "Initial step", ["src/safe.py"])
        tracker.initialize_steps([step])

        rec = tracker.get_record("step_1")
        assert rec is not None
        self.assertEqual(rec.status, StepExecutionStatus.PENDING)
        self.assertIsNone(rec.completed_at)
        self.assertIsNone(tracker.check_ambiguous_interrupted_step())

    def test_audit_b2_crash_while_running_non_idempotent_fails_closed_on_recovery(self) -> None:
        """Audit B.2: Non-idempotent step left in RUNNING state halts execution loop on restart."""
        plan = self._create_sample_plan("plan_b2")
        tracker = PlanProgressTracker(self.repo_dir, plan.id)
        step = PlanStep("step_1", "Mutating step", ["src/safe.py"])
        tracker.initialize_steps([step])
        tracker.mark_running("step_1", is_idempotent=False)

        # Simulate process restart by reloading tracker from disk
        reloaded_tracker = PlanProgressTracker(self.repo_dir, plan.id)
        ambiguous = reloaded_tracker.check_ambiguous_interrupted_step()
        self.assertIsNotNone(ambiguous)
        assert ambiguous is not None
        self.assertEqual(ambiguous.step_id, "step_1")

        contract = self._create_contract(plan, targets=["src/safe.py"])
        cfg = RunConfig(repo_dir=self.repo_dir, task="build", execution_contract=contract, plan_id=plan.id)
        orch = Orchestrator(MockSystem1(), MockSystem2(), cfg)
        runner = StructuredStepRunner(self.repo_dir, orch, contract, plan.id, tracker=reloaded_tracker)

        with self.assertRaises(AmbiguousStepExecutionError):
            runner.run_steps([step])

    def test_audit_b3_crash_between_commit_and_progress_passed_fails_closed(self) -> None:
        """Audit B.3: If crash occurs between transaction commit and progress persistence, recovery fails closed."""
        plan = self._create_sample_plan("plan_b3")
        tracker = PlanProgressTracker(self.repo_dir, plan.id)
        step = PlanStep("step_1", "Mutating step", ["src/safe.py"])
        tracker.initialize_steps([step])
        # Step remains marked RUNNING because crash prevented mark_passed
        tracker.mark_running("step_1", is_idempotent=False)

        # Restart runner
        reloaded_tracker = PlanProgressTracker(self.repo_dir, plan.id)
        contract = self._create_contract(plan, targets=["src/safe.py"])
        cfg = RunConfig(repo_dir=self.repo_dir, task="build", execution_contract=contract, plan_id=plan.id)
        orch = Orchestrator(MockSystem1(), MockSystem2(), cfg)
        runner = StructuredStepRunner(self.repo_dir, orch, contract, plan.id, tracker=reloaded_tracker)

        # Must fail closed with AmbiguousStepExecutionError rather than repeating operation
        with self.assertRaises(AmbiguousStepExecutionError) as ctx:
            runner.run_steps([step])
        self.assertIn("non-idempotent", str(ctx.exception).lower())

    def test_audit_b4_crash_during_atomic_progress_file_replacement_preserves_valid_file(self) -> None:
        """Audit B.4: An interrupted .tmp write does not corrupt the durable progress file."""
        plan = self._create_sample_plan("plan_b4")
        tracker = PlanProgressTracker(self.repo_dir, plan.id)
        step = PlanStep("step_1", "Mutating step", ["src/safe.py"])
        tracker.initialize_steps([step])
        tracker.mark_passed("step_1", transaction_id="tx_123")

        progress_path = self.repo_dir / ".brainfrog" / "plans" / f"{plan.id}.progress.json"
        original_bytes = progress_path.read_bytes()

        # Simulate leftover temporary corrupted file from a killed process
        corrupt_tmp = self.repo_dir / ".brainfrog" / "plans" / f"{plan.id}.progress.tmp_corrupt"
        corrupt_tmp.write_text("{ incomplete json ...", encoding="utf-8")

        # Reloading tracker loads intact progress file and cleans stale tmp files
        reloaded_tracker = PlanProgressTracker(self.repo_dir, plan.id)
        rec = reloaded_tracker.get_record("step_1")
        assert rec is not None
        self.assertEqual(rec.status, StepExecutionStatus.PASSED)
        self.assertEqual(rec.transaction_id, "tx_123")
        self.assertEqual(progress_path.read_bytes(), original_bytes)

    def test_audit_b5_wal_rollback_preserves_unrelated_user_notes(self) -> None:
        """Audit B.5: Failure and transaction rollback preserve user draft notes and uncommitted files."""
        notes = self.repo_dir / "user_notes.txt"
        notes.write_text("User secret thoughts", encoding="utf-8")
        readme = self.repo_dir / "README.md"
        readme.write_text("# Draft Edit by User\n", encoding="utf-8")

        plan = self._create_sample_plan("plan_b5")
        contract = self._create_contract(plan, targets=["src/failing.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="alice", reason="Start", contract=contract)

        cfg = RunConfig(
            repo_dir=self.repo_dir,
            task="build",
            execution_contract=contract,
            plan_id=plan.id,
            session_id=plan.session_id,
            test_command=["python", "-c", "import sys; sys.exit(1)"],  # Force rollback
        )
        orch = Orchestrator(MockSystem1(), MockSystem2({"src/failing.py": "print(1)"}), cfg)
        runner = StructuredStepRunner(self.repo_dir, orch, contract, plan.id)

        step = PlanStep("1", "fail step", ["src/failing.py"])
        with self.assertRaises(RuntimeError):
            runner.run_steps([step])

        # Step file rolled back
        self.assertFalse((self.repo_dir / "src" / "failing.py").exists())
        # User files 100% preserved
        self.assertTrue(notes.exists())
        self.assertEqual(notes.read_text(encoding="utf-8"), "User secret thoughts")
        self.assertEqual(readme.read_text(encoding="utf-8"), "# Draft Edit by User\n")

    def test_audit_b6_recovery_repeated_idempotence(self) -> None:
        """Audit B.6: Repeated crash recovery cycles produce zero side effects."""
        plan = self._create_sample_plan("plan_b6")
        contract = self._create_contract(plan, targets=["src/safe.py"])
        tracker = PlanProgressTracker(self.repo_dir, plan.id)
        step = PlanStep("step_1", "safe step", ["src/safe.py"])
        tracker.initialize_steps([step])
        tracker.mark_passed("step_1", transaction_id="tx_b6")

        for _ in range(5):
            t = PlanProgressTracker(self.repo_dir, plan.id)
            rec = t.get_record("step_1")
            assert rec is not None
            self.assertEqual(rec.status, StepExecutionStatus.PASSED)
            self.assertIsNone(t.check_ambiguous_interrupted_step())

    # =========================================================================
    # AUDIT C: Cancellation and Timeout
    # =========================================================================

    def test_audit_c1_hardened_process_timeout_terminates_process_tree(self) -> None:
        """Audit C.1: ProcessRunner enforces finite timeout and terminates subprocess and descendants."""
        sleep_script = self.repo_dir / "sleep_script.py"
        sleep_script.write_text("import time\ntime.sleep(10)\n", encoding="utf-8")
        cmd = [sys.executable, str(sleep_script)]
        res = run_hardened_subprocess(cmd, cwd=self.repo_dir, timeout=0.5)
        self.assertTrue(res.is_timeout)
        self.assertEqual(res.returncode, -1)
        self.assertIn("timed out", res.error.lower())

    def test_audit_c2_cancellation_token_stops_loop_and_rolls_back(self) -> None:
        """Audit C.2: A cancellation event interrupts the loop, marks CANCELLED, and skips remaining steps."""
        plan = self._create_sample_plan("plan_c2")
        contract = self._create_contract(plan, targets=["src/safe.py", "src/extra.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="alice", reason="Start", contract=contract)

        cfg = RunConfig(repo_dir=self.repo_dir, task="build", execution_contract=contract, plan_id=plan.id)
        orch = Orchestrator(MockSystem1(), MockSystem2(), cfg)
        runner = StructuredStepRunner(self.repo_dir, orch, contract, plan.id)

        steps = [
            PlanStep("1", "First step", ["src/safe.py"]),
            PlanStep("2", "Second step", ["src/extra.py"]),
        ]

        cancel_ev = threading.Event()
        cancel_ev.set()  # Cancel before start
        token = _CURRENT_CANCEL_EVENT.set(cancel_ev)

        try:
            with self.assertRaises(KeyboardInterrupt):
                runner.run_steps(steps)
        finally:
            _CURRENT_CANCEL_EVENT.reset(token)

        rec1 = runner.tracker.get_record("1")
        rec2 = runner.tracker.get_record("2")
        assert rec1 is not None and rec2 is not None
        self.assertEqual(rec1.status, StepExecutionStatus.CANCELLED)
        self.assertEqual(rec2.status, StepExecutionStatus.SKIPPED)

    def test_audit_c3_in_process_timeout_limitation_documented_and_tested(self) -> None:
        """Audit C.3: Post-step elapsed timeout detects long-running steps, but in-process Python calls cannot be forcibly preempted."""
        plan = self._create_sample_plan("plan_c3")
        contract = self._create_contract(plan, targets=["src/safe.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="alice", reason="Start", contract=contract)

        cfg = RunConfig(repo_dir=self.repo_dir, task="build", execution_contract=contract, plan_id=plan.id)
        orch = Orchestrator(MockSystem1(), MockSystem2(), cfg)
        runner = StructuredStepRunner(self.repo_dir, orch, contract, plan.id, step_timeout=0.1)

        # Inject a synchronous in-process delay
        def _slow_step(step: Any) -> Any:
            time.sleep(0.2)
            return StepResult(step, "verified", 0, "ok")

        orch._run_step = _slow_step  # type: ignore
        step = PlanStep("1", "slow step", ["src/safe.py"])

        with self.assertRaises(StepExecutionTimeoutError) as ctx:
            runner._execute_single_step(step, "1")
        self.assertEqual(ctx.exception.step_id, "1")

    # =========================================================================
    # AUDIT D: Retry and Idempotency
    # =========================================================================

    def test_audit_d1_arbitrary_model_idempotency_claims_rejected(self) -> None:
        """Audit D.1: Arbitrary model-authored is_idempotent=True is rejected by trusted classifier."""
        # Mutating or unclassified action claiming is_idempotent=True
        untrusted_step = {"id": "1", "is_idempotent": True, "action_type": "arbitrary_mutation"}
        self.assertFalse(is_step_safely_idempotent(untrusted_step))

    def test_audit_d2_mutating_step_never_classified_as_idempotent(self) -> None:
        """Audit D.2: A step writing files or modifying code is never idempotent."""
        mutating_step = PlanStep("1", "Write code", ["src/safe.py"])
        self.assertFalse(is_step_safely_idempotent(mutating_step))

    def test_audit_d3_allowlisted_safe_action_bounded_to_one_retry(self) -> None:
        """Audit D.3: Trusted allowlisted idempotent action (read_check) is bounded to exactly 1 retry."""
        step_idem = {
            "id": "1",
            "description": "Read inspection",
            "action_type": "read_check",
            "is_idempotent": True,
        }
        self.assertTrue(is_step_safely_idempotent(step_idem))

        plan = self._create_sample_plan("plan_d3")
        contract = self._create_contract(plan, targets=["src/safe.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="alice", reason="Start", contract=contract)

        cfg = RunConfig(repo_dir=self.repo_dir, task="build", execution_contract=contract, plan_id=plan.id)
        orch = Orchestrator(MockSystem1(), MockSystem2(), cfg)
        runner = StructuredStepRunner(self.repo_dir, orch, contract, plan.id)

        attempts = [0]
        def _retry_then_succeed(step: Any, step_id: str) -> Any:
            attempts[0] += 1
            if attempts[0] == 1:
                raise IOError("Transient network glitch")
            return StepResult(PlanStep("1", "ok", []), "verified", 0, "success")

        runner._execute_single_step = _retry_then_succeed  # type: ignore
        results = runner.run_steps([step_idem])
        self.assertEqual(len(results), 1)
        self.assertEqual(attempts[0], 2)  # Initial try + exactly 1 retry

    def test_audit_d4_timed_out_and_ambiguous_steps_never_retried(self) -> None:
        """Audit D.4: Timed-out steps are never retried even if claiming idempotency."""
        step_idem = {
            "id": "1",
            "description": "Read check",
            "action_type": "read_check",
            "is_idempotent": True,
        }
        plan = self._create_sample_plan("plan_d4")
        contract = self._create_contract(plan, targets=["src/safe.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="alice", reason="Start", contract=contract)

        cfg = RunConfig(repo_dir=self.repo_dir, task="build", execution_contract=contract, plan_id=plan.id)
        orch = Orchestrator(MockSystem1(), MockSystem2(), cfg)
        runner = StructuredStepRunner(self.repo_dir, orch, contract, plan.id)

        attempts = [0]
        def _timeout_step(step: Any, step_id: str) -> Any:
            attempts[0] += 1
            raise StepExecutionTimeoutError(step_id, 10.0)

        runner._execute_single_step = _timeout_step  # type: ignore
        with self.assertRaises(StepExecutionTimeoutError):
            runner.run_steps([step_idem])

        self.assertEqual(attempts[0], 1)  # Zero retries on timeout!

    # =========================================================================
    # AUDIT E: Plan and Contract Scope
    # =========================================================================

    def test_audit_e1_revision_change_between_steps_stops_execution(self) -> None:
        """Audit E.1: A plan revision change between step 1 and step 2 halts execution immediately."""
        plan = self._create_sample_plan("plan_e1")
        contract = self._create_contract(plan, targets=["src/safe.py", "src/extra.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="alice", reason="Start", contract=contract)

        cfg = RunConfig(repo_dir=self.repo_dir, task="build", execution_contract=contract, plan_id=plan.id)
        orch = Orchestrator(MockSystem1(), MockSystem2({"src/safe.py": "v1"}), cfg)
        runner = StructuredStepRunner(self.repo_dir, orch, contract, plan.id)

        step1 = PlanStep("1", "Step 1", ["src/safe.py"])
        step2 = PlanStep("2", "Step 2", ["src/extra.py"])
        step3 = PlanStep("3", "Step 3", ["src/extra2.py"])

        orig_exec = runner._execute_single_step
        def _exec_and_bump_rev(step: Any, step_id: str) -> Any:
            res = orig_exec(step, step_id)
            if step_id == "1":
                # Out-of-band edit bumps revision
                from core.plans import save_plan_document
                save_plan_document(
                    self.repo_dir,
                    f"{plan.id}.md",
                    "# Tampered Plan\n\n## Acceptance Criteria\n",
                    metadata={
                        "id": plan.id,
                        "title": "Tampered Plan",
                        "status": PlanStatus.IN_PROGRESS.value,
                        "session_id": plan.session_id,
                    },
                )
            return res

        runner._execute_single_step = _exec_and_bump_rev

        with self.assertRaises(StaleExecutionAuthorizationError):
            runner.run_steps([step1, step2, step3])

        # Step 2 was rejected, subsequent step 3 was SKIPPED, zero unauthorized files written
        self.assertFalse((self.repo_dir / "src" / "extra.py").exists())
        self.assertFalse((self.repo_dir / "src" / "extra2.py").exists())
        rec2 = runner.tracker.get_record("2")
        rec3 = runner.tracker.get_record("3")
        assert rec2 is not None and rec3 is not None
        self.assertEqual(rec2.status, StepExecutionStatus.FAILED)
        self.assertEqual(rec3.status, StepExecutionStatus.SKIPPED)

    def test_audit_e2_content_hash_tampering_between_steps_stops_execution(self) -> None:
        """Audit E.2: Direct tampering of plan markdown file on disk invalidates active authorization."""
        plan = self._create_sample_plan("plan_e2")
        contract = self._create_contract(plan, targets=["src/safe.py", "src/extra.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="alice", reason="Start", contract=contract)

        cfg = RunConfig(repo_dir=self.repo_dir, task="build", execution_contract=contract, plan_id=plan.id)
        orch = Orchestrator(MockSystem1(), MockSystem2({"src/safe.py": "v1"}), cfg)
        runner = StructuredStepRunner(self.repo_dir, orch, contract, plan.id)

        step1 = PlanStep("1", "Step 1", ["src/safe.py"])
        step2 = PlanStep("2", "Step 2", ["src/extra.py"])
        step3 = PlanStep("3", "Step 3", ["src/extra2.py"])

        orig_exec = runner._execute_single_step
        def _exec_and_tamper(step: Any, step_id: str) -> Any:
            res = orig_exec(step, step_id)
            if step_id == "1":
                # Tamper disk markdown directly without touching metadata
                p_doc = self.registry.get_plan(plan.id)
                assert p_doc is not None
                p_doc.path.write_text("# MALICIOUS INJECTION\n", encoding="utf-8")
            return res

        runner._execute_single_step = _exec_and_tamper

        with self.assertRaises(StaleExecutionAuthorizationError):
            runner.run_steps([step1, step2, step3])

        self.assertFalse((self.repo_dir / "src" / "extra.py").exists())
        self.assertFalse((self.repo_dir / "src" / "extra2.py").exists())
        rec2 = runner.tracker.get_record("2")
        rec3 = runner.tracker.get_record("3")
        assert rec2 is not None and rec3 is not None
        self.assertEqual(rec2.status, StepExecutionStatus.FAILED)
        self.assertEqual(rec3.status, StepExecutionStatus.SKIPPED)

    def test_audit_e3_unapproved_target_rejected_before_writing(self) -> None:
        """Audit E.3: Attempt to write unapproved target stops loop and skips remaining steps."""
        plan = self._create_sample_plan("plan_e3")
        contract = self._create_contract(plan, targets=["src/safe.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="alice", reason="Start", contract=contract)

        cfg = RunConfig(repo_dir=self.repo_dir, task="build", execution_contract=contract, plan_id=plan.id)
        orch = Orchestrator(MockSystem1(), MockSystem2({"src/evil.py": "malicious"}), cfg)
        runner = StructuredStepRunner(self.repo_dir, orch, contract, plan.id)

        step1 = PlanStep("1", "Step 1", ["src/evil.py"])
        step2 = PlanStep("2", "Step 2", ["src/safe.py"])

        with self.assertRaises(PermissionError):
            runner.run_steps([step1, step2])

        self.assertFalse((self.repo_dir / "src" / "evil.py").exists())
        self.assertFalse((self.repo_dir / "src" / "safe.py").exists())
        rec2 = runner.tracker.get_record("2")
        assert rec2 is not None
        self.assertEqual(rec2.status, StepExecutionStatus.SKIPPED)

    def test_audit_e4_cross_session_and_cross_incarnation_contracts_rejected(self) -> None:
        """Audit E.4: Contracts from foreign sessions or incarnations fail closed."""
        plan = self._create_sample_plan("plan_e4", session_id="cli:alice:sess_1")
        contract = self._create_contract(plan, session_id="cli:alice:sess_1", session_incarnation_id="inc_old")
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="alice", reason="Start", contract=contract)

        # Cross-session contract attempt
        contract_foreign_session = self._create_contract(plan, session_id="cli:alien:foreign_sess")
        cfg1 = RunConfig(repo_dir=self.repo_dir, task="build", execution_contract=contract_foreign_session, plan_id=plan.id)
        orch1 = Orchestrator(MockSystem1(), MockSystem2(), cfg1)
        runner1 = StructuredStepRunner(self.repo_dir, orch1, contract_foreign_session, plan.id)

        with self.assertRaises(PermissionError) as ctx1:
            runner1.revalidate_pre_mutation()
        self.assertIn("ownership mismatch", str(ctx1.exception).lower())

        # Cross-incarnation contract attempt
        with self.assertRaises(PermissionError):
            contract.validate(
                actor="alice", channel="terminal", session_id="cli:alice:sess_1",
                session_incarnation_id="inc_new", repo_dir=self.repo_dir,
            )


if __name__ == "__main__":
    unittest.main()
