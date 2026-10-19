"""BrainFrog Phase 4B — Bounded Autonomous Execution End-to-End Tests.

Verifies the bounded autonomous execution increment on top of the Phase 4A runtime:
1. Successful two-step execution under existing contract.
2. Failure in step one stops execution immediately.
3. Failure in step two preserves step one's committed work.
4. Cancellation between steps stops loop safely.
5. Contract expiry before mutation stops execution safely.
6. Plan tampering before mutation invalidates authorization safely.
7. An unapproved target introduced by generated output is rejected before writing.
8. Restart after an interrupted non-idempotent step fails closed (no blind replay).
9. Attempted reuse of a consumed approval contract fails closed.
10. Acceptance check exits successfully (exit 0) but does not establish intended result.
11. Reconciliation without independently verifiable evidence is stamped as operator attestation
    and cannot override a rolled-back transaction.
12. Execution results cleanly distinguish completed, failed, and reconciliation-required work.
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
    CriterionVerificationMapping,
    InvalidPlanTransitionError,
    PlanDocument,
    PlanRegistry,
    PlanStatus,
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
from core.runtime.process import _CURRENT_CANCEL_EVENT, _CURRENT_TASK_ID
from core.runtime.step_runner import (
    AmbiguousStepExecutionError,
    PlanProgressTracker,
    StaleExecutionAuthorizationError,
    StepExecutionStatus,
    StepProgressRecord,
    StructuredStepRunner,
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


class TestPhase4BAutonomousExecution(unittest.TestCase):
    """Adversarial end-to-end tests for Phase 4B Bounded Autonomous Execution."""

    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="bf_phase4b_")).resolve()
        self.repo_dir = self.temp_dir / "workspace"
        self.repo_dir.mkdir(parents=True)
        self.plans_dir = self.repo_dir / ".brainfrog" / "plans"
        self.plans_dir.mkdir(parents=True)
        self.tx_dir = self.repo_dir / ".brainfrog" / "transactions"
        self.tx_dir.mkdir(parents=True)
        (self.repo_dir / "src").mkdir(parents=True)

        # Initialize synthetic git repository
        subprocess.run(["git", "init"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "SyntheticTester"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "tester@synthetic.local"], cwd=self.repo_dir, capture_output=True, check=True)
        (self.repo_dir / "README.md").write_text("# Synthetic Workspace\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=self.repo_dir, capture_output=True, check=True)

        self.registry = PlanRegistry(self.repo_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_sample_plan(
        self,
        plan_id: str = "plan_4b_01",
        title: str = "Bounded Autonomous Plan",
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
            f"# {title}\n- **Goal**: Test Phase 4B Autonomous Loop\n",
            metadata={
                "id": plan_id,
                "title": title,
                "goal": "Test Phase 4B Autonomous Loop",
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
        session_incarnation: str = "inc_4b_1",
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
    # Test 1: Successful Two-Step Execution
    # =========================================================================
    def test_e2e_successful_two_step_execution(self) -> None:
        """1. Explicitly approved plan executes sequentially; mutations stay in bounds; progress persisted."""
        plan = self._create_sample_plan("plan_success_01", status=PlanStatus.READY)
        contract, _, _ = self._create_contract(plan, targets=["src/mod1.py", "src/mod2.py"])

        # Transition READY -> IN_PROGRESS with contract
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="local", reason="Start execution", contract=contract)
        plan_in_progress = self.registry.get_plan(plan.id)
        assert plan_in_progress is not None

        s2 = SyntheticS2Double()
        cfg = RunConfig(
            repo_dir=self.repo_dir,
            task="build plan",
            mode="build",
            execution_contract=contract,
            plan_id=plan.id,
            session_id="cli:local:default",
        )
        orch = Orchestrator(SyntheticS1Double(), s2, cfg)
        runner = StructuredStepRunner(self.repo_dir, orch, contract, plan.id)

        steps = [
            PlanStep("1", "Implement mod1", ["src/mod1.py"]),
            PlanStep("2", "Implement mod2", ["src/mod2.py"]),
        ]

        # Execute two steps sequentially
        results = runner.run_steps(steps)
        self.assertEqual(len(results), 2)

        # 1. Verify mutations are within approved targets and present on disk
        self.assertTrue((self.repo_dir / "src" / "mod1.py").exists())
        self.assertTrue((self.repo_dir / "src" / "mod2.py").exists())
        self.assertIn("step 1", (self.repo_dir / "src" / "mod1.py").read_text(encoding="utf-8"))
        self.assertIn("step 2", (self.repo_dir / "src" / "mod2.py").read_text(encoding="utf-8"))

        # 2. Verify progress file is persisted and accurately records both steps as PASSED
        progress_file = self.plans_dir / f"{plan.id}.progress.json"
        self.assertTrue(progress_file.exists())
        data = json.loads(progress_file.read_text(encoding="utf-8"))
        step_statuses = {s["step_id"]: s["status"] for s in data["steps"]}
        self.assertEqual(step_statuses["1"], "passed")
        self.assertEqual(step_statuses["2"], "passed")

        # 3. Verify execution summary cleanly distinguishes completed, failed, and reconciliation
        summary = runner.get_summary()
        self.assertEqual(summary["completed"], ["1", "2"])
        self.assertEqual(summary["failed"], [])
        self.assertEqual(summary["requiring_reconciliation"], [])

    # =========================================================================
    # Test 2: Failure in Step One Stops Execution Immediately
    # =========================================================================
    def test_e2e_failure_in_step_one_stops_execution(self) -> None:
        """2. Failure in step one stops execution immediately; step two is marked SKIPPED."""
        plan = self._create_sample_plan("plan_fail_step1", status=PlanStatus.READY)
        contract, _, _ = self._create_contract(plan, targets=["src/mod1.py", "src/mod2.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="local", reason="Start execution", contract=contract)

        s2 = SyntheticS2Double()
        cfg = RunConfig(
            repo_dir=self.repo_dir,
            task="build plan",
            mode="build",
            execution_contract=contract,
            plan_id=plan.id,
            session_id="cli:local:default",
        )
        orch = Orchestrator(SyntheticS1Double(), s2, cfg)
        runner = StructuredStepRunner(self.repo_dir, orch, contract, plan.id)

        # Force step 1 to raise an execution failure
        def _failing_execute(step: Any, step_id: str) -> Any:
            if step_id == "1":
                raise RuntimeError("Simulated crash in step 1")
            return None

        runner._execute_single_step = _failing_execute  # type: ignore

        steps = [
            PlanStep("1", "Implement mod1", ["src/mod1.py"]),
            PlanStep("2", "Implement mod2", ["src/mod2.py"]),
        ]

        with self.assertRaises(RuntimeError) as ctx:
            runner.run_steps(steps)
        self.assertIn("Simulated crash in step 1", str(ctx.exception))

        # Step 2 was never executed or written
        self.assertFalse((self.repo_dir / "src" / "mod2.py").exists())

        # Progress tracker recorded step 1 FAILED and step 2 SKIPPED
        summary = runner.get_summary()
        self.assertEqual(summary["completed"], [])
        self.assertIn("1", summary["failed"])
        self.assertIn("2", summary["failed"])
        self.assertEqual(summary["requiring_reconciliation"], [])

        rec1 = runner.tracker.get_record("1")
        rec2 = runner.tracker.get_record("2")
        assert rec1 is not None and rec2 is not None
        self.assertEqual(rec1.status, StepExecutionStatus.FAILED)
        self.assertEqual(rec2.status, StepExecutionStatus.SKIPPED)

    # =========================================================================
    # Test 3: Failure in Step Two Preserves Step One
    # =========================================================================
    def test_e2e_failure_in_step_two_preserves_step_one(self) -> None:
        """3. Failure in step two preserves step one's committed work; step one PASSED, step two FAILED."""
        plan = self._create_sample_plan("plan_fail_step2", status=PlanStatus.READY)
        contract, _, _ = self._create_contract(plan, targets=["src/mod1.py", "src/mod2.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="local", reason="Start execution", contract=contract)

        s2 = SyntheticS2Double()
        cfg = RunConfig(
            repo_dir=self.repo_dir,
            task="build plan",
            mode="build",
            execution_contract=contract,
            plan_id=plan.id,
            session_id="cli:local:default",
        )
        orch = Orchestrator(SyntheticS1Double(), s2, cfg)
        runner = StructuredStepRunner(self.repo_dir, orch, contract, plan.id)

        # Allow step 1 to execute normally; fail step 2
        orig_execute = runner._execute_single_step
        def _failing_step2(step: Any, step_id: str) -> Any:
            if step_id == "2":
                raise RuntimeError("Step 2 mutation failed")
            return orig_execute(step, step_id)

        runner._execute_single_step = _failing_step2  # type: ignore

        steps = [
            PlanStep("1", "Implement mod1", ["src/mod1.py"]),
            PlanStep("2", "Implement mod2", ["src/mod2.py"]),
        ]

        with self.assertRaises(RuntimeError) as ctx:
            runner.run_steps(steps)
        self.assertIn("Step 2 mutation failed", str(ctx.exception))

        # Step 1 was preserved
        self.assertTrue((self.repo_dir / "src" / "mod1.py").exists())
        self.assertFalse((self.repo_dir / "src" / "mod2.py").exists())

        # Progress verification
        summary = runner.get_summary()
        self.assertEqual(summary["completed"], ["1"])
        self.assertEqual(summary["failed"], ["2"])
        self.assertEqual(summary["requiring_reconciliation"], [])

    # =========================================================================
    # Test 4: Cancellation Between Steps Stops Execution Safely
    # =========================================================================
    def test_e2e_cancellation_between_steps(self) -> None:
        """4. Cancellation token set between steps stops loop; step 1 PASSED, step 2 CANCELLED."""
        plan = self._create_sample_plan("plan_cancel_01", status=PlanStatus.READY)
        contract, _, _ = self._create_contract(plan, targets=["src/mod1.py", "src/mod2.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="local", reason="Start execution", contract=contract)

        s2 = SyntheticS2Double()
        cfg = RunConfig(
            repo_dir=self.repo_dir,
            task="build plan",
            mode="build",
            execution_contract=contract,
            plan_id=plan.id,
            session_id="cli:local:default",
        )
        orch = Orchestrator(SyntheticS1Double(), s2, cfg)
        runner = StructuredStepRunner(self.repo_dir, orch, contract, plan.id)

        import threading
        cancel_event = threading.Event()
        token = _CURRENT_CANCEL_EVENT.set(cancel_event)

        try:
            orig_execute = runner._execute_single_step
            def _cancel_after_step1(step: Any, step_id: str) -> Any:
                res = orig_execute(step, step_id)
                if step_id == "1":
                    cancel_event.set()  # Signal cancellation right after step 1
                return res

            runner._execute_single_step = _cancel_after_step1  # type: ignore

            steps = [
                PlanStep("1", "Implement mod1", ["src/mod1.py"]),
                PlanStep("2", "Implement mod2", ["src/mod2.py"]),
            ]

            with self.assertRaises(KeyboardInterrupt) as ctx:
                runner.run_steps(steps)
            self.assertIn("cancelled", str(ctx.exception).lower())

            # Step 1 was committed; Step 2 was cancelled before mutation
            self.assertTrue((self.repo_dir / "src" / "mod1.py").exists())
            self.assertFalse((self.repo_dir / "src" / "mod2.py").exists())

            rec1 = runner.tracker.get_record("1")
            rec2 = runner.tracker.get_record("2")
            assert rec1 is not None and rec2 is not None
            self.assertEqual(rec1.status, StepExecutionStatus.PASSED)
            self.assertEqual(rec2.status, StepExecutionStatus.CANCELLED)
        finally:
            _CURRENT_CANCEL_EVENT.reset(token)

    # =========================================================================
    # Test 5: Contract Expiry or Plan Tampering Before Mutation
    # =========================================================================
    def test_e2e_contract_expiry_before_mutation(self) -> None:
        """5A. Contract expiry before mutation stops execution with PermissionError."""
        plan = self._create_sample_plan("plan_expire_01", status=PlanStatus.READY)
        contract, _, _ = self._create_contract(plan, targets=["src/mod1.py"], expires_in=-10.0)

        cfg = RunConfig(
            repo_dir=self.repo_dir,
            task="build plan",
            mode="build",
            execution_contract=contract,
            plan_id=plan.id,
            session_id="cli:local:default",
        )
        orch = Orchestrator(SyntheticS1Double(), SyntheticS2Double(), cfg)
        runner = StructuredStepRunner(self.repo_dir, orch, contract, plan.id)

        steps = [PlanStep("1", "Implement mod1", ["src/mod1.py"])]

        with self.assertRaises(PermissionError) as exp_ctx:
            runner.run_steps(steps)
        self.assertIn("expired", str(exp_ctx.exception).lower())
        self.assertFalse((self.repo_dir / "src" / "mod1.py").exists())

        rec = runner.tracker.get_record("1")
        assert rec is not None
        self.assertEqual(rec.status, StepExecutionStatus.FAILED)

    def test_e2e_plan_tampering_before_mutation(self) -> None:
        """5B. Plan content or revision tampering before mutation raises StaleExecutionAuthorizationError."""
        plan = self._create_sample_plan("plan_tamper_01", status=PlanStatus.READY)
        contract, _, _ = self._create_contract(plan, targets=["src/mod1.py", "src/mod2.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="local", reason="Start execution", contract=contract)
        plan_in_progress = self.registry.get_plan(plan.id)
        assert plan_in_progress is not None

        s2 = SyntheticS2Double()
        cfg = RunConfig(
            repo_dir=self.repo_dir,
            task="build plan",
            mode="build",
            execution_contract=contract,
            plan_id=plan.id,
            session_id="cli:local:default",
        )
        orch = Orchestrator(SyntheticS1Double(), s2, cfg)
        runner = StructuredStepRunner(self.repo_dir, orch, contract, plan.id)

        # Tamper with markdown file on disk after step 1
        orig_execute = runner._execute_single_step
        def _tamper_plan_after_step1(step: Any, step_id: str) -> Any:
            res = orig_execute(step, step_id)
            if step_id == "1":
                # Directly modify the plan markdown file out-of-band to simulate external tampering
                tampered_content = plan_in_progress.content + "\n- Injected unauthorized requirement\n"
                (self.plans_dir / f"{plan.id}.md").write_text(tampered_content, encoding="utf-8")
            return res

        runner._execute_single_step = _tamper_plan_after_step1  # type: ignore

        steps = [
            PlanStep("1", "Implement mod1", ["src/mod1.py"]),
            PlanStep("2", "Implement mod2", ["src/mod2.py"]),
        ]

        with self.assertRaises(StaleExecutionAuthorizationError) as ctx:
            runner.run_steps(steps)
        self.assertIn("modified during execution", str(ctx.exception).lower())

        # Step 1 was committed; Step 2 was halted before mutation
        self.assertTrue((self.repo_dir / "src" / "mod1.py").exists())
        self.assertFalse((self.repo_dir / "src" / "mod2.py").exists())

    # =========================================================================
    # Test 6: Unapproved Target Introduced by Generated Output
    # =========================================================================
    def test_e2e_unapproved_target_introduced_by_generated_output(self) -> None:
        """6. Unapproved target in generated output rejected before writing with PermissionError."""
        plan = self._create_sample_plan("plan_unapproved_01", status=PlanStatus.READY)
        # Contract only authorizes src/mod1.py
        contract, _, _ = self._create_contract(plan, targets=["src/mod1.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="local", reason="Start execution", contract=contract)

        # S2 double attempts to write an unapproved file
        s2 = SyntheticS2Double(step_file_map={
            "1": {"src/unapproved_escape.py": "# malicious write\n"}
        })
        cfg = RunConfig(
            repo_dir=self.repo_dir,
            task="build plan",
            mode="build",
            execution_contract=contract,
            plan_id=plan.id,
            session_id="cli:local:default",
        )
        orch = Orchestrator(SyntheticS1Double(), s2, cfg)
        runner = StructuredStepRunner(self.repo_dir, orch, contract, plan.id)

        steps = [PlanStep("1", "Implement mod1", ["src/mod1.py"])]

        with self.assertRaises(PermissionError) as ctx:
            runner.run_steps(steps)
        self.assertIn("unapproved_escape.py", str(ctx.exception))

        # Ensure unapproved file was NEVER written to workspace
        self.assertFalse((self.repo_dir / "src" / "unapproved_escape.py").exists())
        self.assertFalse((self.repo_dir / "src" / "mod1.py").exists())

    # =========================================================================
    # Test 7: Restart After Interrupted Non-Idempotent Step
    # =========================================================================
    def test_e2e_restart_after_interrupted_non_idempotent_step(self) -> None:
        """7. Interrupted non-idempotent step left in RUNNING state blocks restart with AmbiguousStepExecutionError."""
        plan = self._create_sample_plan("plan_ambig_01", status=PlanStatus.READY)
        contract, _, _ = self._create_contract(plan, targets=["src/mod1.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="local", reason="Start execution", contract=contract)

        # Pre-seed progress file with an interrupted non-idempotent step
        tracker = PlanProgressTracker(self.repo_dir, plan.id)
        tracker.initialize_steps([{"id": "1", "description": "Mutating step", "is_idempotent": False}])
        tracker.mark_running("1", is_idempotent=False)

        # Execution summary flags step as requiring reconciliation
        summary = tracker.get_execution_summary()
        self.assertIn("1", summary["requiring_reconciliation"])
        self.assertEqual(summary["completed"], [])

        # Attempt to restart execution without reconciling
        cfg = RunConfig(
            repo_dir=self.repo_dir,
            task="build plan",
            mode="build",
            execution_contract=contract,
            plan_id=plan.id,
            session_id="cli:local:default",
        )
        orch = Orchestrator(SyntheticS1Double(), SyntheticS2Double(), cfg)
        runner = StructuredStepRunner(self.repo_dir, orch, contract, plan.id, tracker=tracker)

        steps = [PlanStep("1", "Mutating step", ["src/mod1.py"])]

        with self.assertRaises(AmbiguousStepExecutionError) as ctx:
            runner.run_steps(steps)
        self.assertIn("blind rerun is forbidden", str(ctx.exception).lower())

    # =========================================================================
    # Test 8: Attempted Reuse of Consumed Contract Fails Closed
    # =========================================================================
    def test_e2e_attempted_reuse_of_consumed_contract(self) -> None:
        """8. Resumed execution cannot reuse a consumed approval contract (replay prevented)."""
        plan = self._create_sample_plan("plan_reuse_01", status=PlanStatus.READY)
        approved_targets = ["src/mod1.py"]
        op = CanonicalOperation("write_files", approved_targets[0], {"targets": approved_targets})
        caps = Capabilities(
            filesystem=FilesystemPolicy(read=tuple(approved_targets), write=tuple(approved_targets)),
            network=NetworkPolicy(access=True),
        )
        app_service = ApprovalService(store=InMemoryApprovalStore())
        app_req = app_service.create_request(
            session_id="cli:local:default",
            channel="cli",
            user_id="local",
            conversation_id="default",
            operation_type="write_files",
            canonical_operation=op,
            risk_class=RiskClass.MEDIUM.value,
            session_incarnation_id="inc_4b_reuse",
            capabilities=caps,
            workspace_root=str(self.repo_dir),
            plan_id=plan.id,
            plan_revision=plan.revision,
            plan_content_hash=plan.content_hash,
        )
        app_service.approve(app_req.request_id, "security_lead", "cli")

        # First consumption succeeds
        ok1, consumed_req, reason1 = app_service.verify_and_consume(
            app_req.request_id,
            app_req.operation_digest,
            "cli:local:default",
            "cli",
            session_incarnation_id="inc_4b_reuse",
            requester_id="local",
        )
        self.assertTrue(ok1)
        self.assertIsNotNone(consumed_req)

        # Attempting second consumption (replay) fails closed
        ok2, consumed2, reason2 = app_service.verify_and_consume(
            app_req.request_id,
            app_req.operation_digest,
            "cli:local:default",
            "cli",
            session_incarnation_id="inc_4b_reuse",
            requester_id="local",
        )
        self.assertFalse(ok2)
        self.assertIsNotNone(consumed2)
        assert consumed2 is not None
        self.assertEqual(consumed2.status, ApprovalStatus.CONSUMED)
        self.assertIn("already been consumed", reason2.lower())

    # =========================================================================
    # Test 9: Acceptance Check Exits Successfully But Does Not Establish Result
    # =========================================================================
    def test_e2e_acceptance_check_exits_successfully_without_establishing_intended_result(self) -> None:
        """9. Exit 0 command unmapped to acceptance criteria cannot transition plan to VERIFIED."""
        plan = self._create_sample_plan(
            "plan_accept_01",
            status=PlanStatus.READY,
            criteria=["Criterion 1: Unit tests passing", "Criterion 2: Coverage threshold met"],
        )
        contract, _, _ = self._create_contract(plan, targets=["src/mod1.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="local", reason="Start execution", contract=contract)

        # Transition to IMPLEMENTED
        self.registry.update_status(plan.id, PlanStatus.IMPLEMENTED, actor="local", reason="Implementation complete")
        doc_impl = self.registry.get_plan(plan.id)
        assert doc_impl is not None

        # 9A: Exit 0 command with no criterion_ref fails closed
        unmapped_evidence = {
            "plan_id": plan.id,
            "plan_revision": doc_impl.revision,
            "plan_content_hash": doc_impl.content_hash,
            "checks": [
                {
                    "check_name": "Generic Shell Exit Zero",
                    "command": "python -c 'exit(0)'",
                    "status": "passed",
                    "criterion_ref": "",  # Missing criterion ref
                }
            ],
            "summary": "Shell exited 0",
        }
        with self.assertRaises(InvalidPlanTransitionError) as ctx1:
            self.registry.update_status(
                plan.id,
                PlanStatus.VERIFIED,
                actor="local",
                reason="Verification attempt",
                evidence=unmapped_evidence,
            )
        self.assertIn("no criterion reference", str(ctx1.exception).lower())

        # 9B: Exit 0 command only covering 1 of 2 criteria fails closed
        partial_evidence = {
            "plan_id": plan.id,
            "plan_revision": doc_impl.revision,
            "plan_content_hash": doc_impl.content_hash,
            "checks": [
                {
                    "check_name": "Check 1 Only",
                    "command": "python -c 'exit(0)'",
                    "status": "passed",
                    "criterion_ref": "Criterion 1: Unit tests passing",
                }
            ],
            "summary": "Partial verification",
        }
        with self.assertRaises(InvalidPlanTransitionError) as ctx2:
            self.registry.update_status(
                plan.id,
                PlanStatus.VERIFIED,
                actor="local",
                reason="Verification attempt",
                evidence=partial_evidence,
            )
        self.assertTrue(
            "incomplete verification coverage" in str(ctx2.exception).lower()
            or "missing checks for criteria" in str(ctx2.exception).lower()
        )

        # Plan remains IMPLEMENTED, never falsely marked VERIFIED
        doc_final = self.registry.get_plan(plan.id)
        assert doc_final is not None
        self.assertEqual(doc_final.status, PlanStatus.IMPLEMENTED)

    # =========================================================================
    # Test 10: Reconciliation Without Independently Verifiable Evidence
    # =========================================================================
    def test_e2e_reconciliation_without_independently_verifiable_evidence(self) -> None:
        """10. Operator attestation stamped with transaction_verified=False and rejected on rollback."""
        plan = self._create_sample_plan("plan_recon_01", status=PlanStatus.READY)
        tracker = PlanProgressTracker(self.repo_dir, plan.id)
        tracker.initialize_steps([{"id": "1", "description": "Step 1", "is_idempotent": False}])
        tracker.mark_running("1")

        # 10A: Reconciling without operator identity fails closed
        with self.assertRaises(ValueError) as err1:
            tracker.reconcile_ambiguous_step(
                "1",
                new_status=StepExecutionStatus.PASSED,
                operator="",
                operator_attestation="Valid attestation",
            )
        self.assertIn("Operator identity", str(err1.exception))

        # 10B: Reconciling without attestation fails closed
        with self.assertRaises(ValueError) as err2:
            tracker.reconcile_ambiguous_step(
                "1",
                new_status=StepExecutionStatus.PASSED,
                operator="operator_alice",
                operator_attestation="",
            )
        self.assertIn("attestation", str(err2.exception).lower())

        # 10C: Attestation without verified transaction is recorded as operator_attestation
        rec = tracker.reconcile_ambiguous_step(
            "1",
            new_status=StepExecutionStatus.PASSED,
            operator="operator_alice",
            operator_attestation="Manually checked file contents",
        )
        self.assertEqual(rec.status, StepExecutionStatus.PASSED)
        self.assertEqual(rec.reconciliation_type, "operator_attestation")
        self.assertFalse(rec.transaction_verified)
        self.assertIn("OPERATOR_ATTESTATION", rec.evidence or "")

        # 10D: If associated transaction rolled back or failed, CANNOT reconcile as PASSED
        tracker.mark_running("1")
        with self.assertRaises(ValueError) as err_tx:
            tracker.reconcile_ambiguous_step(
                "1",
                new_status=StepExecutionStatus.PASSED,
                operator="operator_alice",
                operator_attestation="Attempting to override rollback",
                transaction_id="tx_rolled_back_01",
                transaction_status="rolled_back",
            )
        self.assertIn("rolled back or failed", str(err_tx.exception).lower())

    # =========================================================================
    # Test 11: Execution Summary Clearly Distinguishes Work Categories
    # =========================================================================
    def test_e2e_results_clearly_distinguish_completed_failed_reconciliation(self) -> None:
        """11. get_execution_summary clearly partitions completed, failed, and reconciliation-required work."""
        plan = self._create_sample_plan("plan_summary_01", status=PlanStatus.READY)
        tracker = PlanProgressTracker(self.repo_dir, plan.id)
        steps = [
            {"id": "step_passed", "description": "Passed step", "is_idempotent": False},
            {"id": "step_failed", "description": "Failed step", "is_idempotent": False},
            {"id": "step_timeout", "description": "Timed out step", "is_idempotent": False},
            {"id": "step_cancelled", "description": "Cancelled step", "is_idempotent": False},
            {"id": "step_skipped", "description": "Skipped step", "is_idempotent": False},
            {"id": "step_ambig", "description": "Interrupted non-idempotent step", "is_idempotent": False},
        ]
        tracker.initialize_steps(steps)

        tracker.mark_passed("step_passed")
        tracker.mark_failed("step_failed", "Error msg")
        tracker.mark_timed_out("step_timeout", "Timed out")
        tracker.mark_cancelled("step_cancelled", "Cancelled")
        tracker.mark_skipped("step_skipped", "Preceding step failed")
        tracker.mark_running("step_ambig", is_idempotent=False)

        summary = tracker.get_execution_summary()
        self.assertEqual(summary["completed"], ["step_passed"])
        self.assertEqual(
            set(summary["failed"]),
            {"step_failed", "step_timeout", "step_cancelled", "step_skipped"},
        )
        self.assertEqual(summary["requiring_reconciliation"], ["step_ambig"])


if __name__ == "__main__":
    unittest.main()
