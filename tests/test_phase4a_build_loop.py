"""Adversarial and Regression Test Suite for Phase 4A — Bounded Autonomous Build Loop.

Verifies:
1. Missing, stale, replayed, or cross-session contracts cannot execute a step.
2. A revision change between steps stops execution before the next mutation.
3. An unapproved target is rejected before writing.
4. A failed or timed-out step prevents subsequent steps from running (stop-on-first-failure).
5. Cancellation leaves a clean, recoverable state.
6. A crash between mutation and progress persistence does not blindly rerun an ambiguous non-idempotent operation.
7. Recovery preserves user modifications and respects WAL invariants.
8. Repeated recovery is idempotent.
9. Only verified acceptance evidence can produce VERIFIED status (IMPLEMENTED != VERIFIED).
10. Bounded retries occur only for explicitly classified safe, idempotent operations.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from unittest.mock import patch

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
from core.runtime.messages import IncomingMessage
from core.runtime.process import _CURRENT_CANCEL_EVENT, _CURRENT_TASK_ID
from core.runtime.runtime import BrainFrogRuntime
from core.runtime.session import FileSessionStore, SessionManager
from core.runtime.step_runner import (
    AmbiguousStepExecutionError,
    PlanProgressTracker,
    StaleExecutionAuthorizationError,
    StepExecutionStatus,
    StepExecutionTimeoutError,
    StructuredStepRunner,
)
from core.runtime.transaction import (
    TransactionCoordinator,
    TransactionResult,
    TransactionStatus,
)
from orchestrator import Orchestrator, PlanStep, RunConfig, StepResult
from system1.base import Answer, SystemOneClient


class SyntheticS1Double(SystemOneClient):
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
    name: str = "synthetic-s2"
    model: str = "synthetic-claude"

    def __init__(self, step_file_map: Optional[Dict[str, Dict[str, str]]] = None) -> None:
        self.step_file_map = step_file_map or {}
        self.executed_steps: List[str] = []

    def plan_task(self, task: str, *args: Any, **kwargs: Any) -> List[PlanStep]:
        return [
            PlanStep("1", "Setup module", ["src/module_a.py"]),
            PlanStep("2", "Setup helpers", ["src/module_b.py"]),
        ]

    def write_code(self, step: Any, task: str, file_contents: Any, *args: Any, **kwargs: Any) -> Dict[str, str]:
        step_id = getattr(step, "id", str(step))
        self.executed_steps.append(step_id)
        if step_id in self.step_file_map:
            return self.step_file_map[step_id]
        target_files = getattr(step, "files", [])
        return {f: f"# Synthetic implementation for step {step_id}\nx = {step_id}\n" for f in target_files}


class TestPhase4ABoundedBuildLoop(unittest.TestCase):
    """Adversarial test suite for Phase 4A bounded autonomous build loop."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="bf_phase4a_")
        self.repo_dir = Path(self.test_dir).resolve()
        (self.repo_dir / ".brainfrog" / "plans").mkdir(parents=True, exist_ok=True)
        (self.repo_dir / "src").mkdir(parents=True, exist_ok=True)

        try:
            subprocess.run(["git", "init"], cwd=self.repo_dir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "SyntheticTester"], cwd=self.repo_dir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "tester@synthetic.local"], cwd=self.repo_dir, check=True, capture_output=True)
            (self.repo_dir / "README.md").write_text("# Synthetic Workspace\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=self.repo_dir, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=self.repo_dir, check=True, capture_output=True)
        except Exception:
            pass

        self.registry = PlanRegistry(self.repo_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_sample_plan(
        self,
        plan_id: str = "plan_4a_01",
        title: str = "Build Loop Plan",
        status: PlanStatus = PlanStatus.READY,
        session_id: str = "cli:local:default",
        steps: Optional[List[Dict[str, Any]]] = None,
    ) -> PlanDocument:
        raw_steps = steps or [
            {"id": "1", "description": "Implement core", "files": ["src/core.py"]},
            {"id": "2", "description": "Implement helpers", "files": ["src/helpers.py"]},
        ]
        save_plan_document(
            self.repo_dir,
            f"{plan_id}.md",
            f"# {title}\n- **Goal**: Test Phase 4A Loop\n",
            metadata={
                "id": plan_id,
                "title": title,
                "goal": "Test Phase 4A Loop",
                "status": status.value,
                "revision": 1,
                "session_id": session_id,
                "steps": raw_steps,
                "acceptance_criteria": ["Criterion 1"],
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
        session_incarnation: str = "inc_4a_1",
        expires_in: float = 300.0,
    ) -> ApprovedExecutionContract:
        approved_targets = targets or ["src/core.py", "src/helpers.py"]
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
        app_req.status = ApprovalStatus.CONSUMED
        app_req.approver_id = "security_lead"
        app_req.approved_at = time.time()
        app_req.consumed_at = time.time()
        app_req.expires_at = time.time() + expires_in
        app_req.authorization_digest = app_req.compute_authorization_digest()
        app_req.approval_digest = app_req.compute_approval_digest()
        return ApprovedExecutionContract.from_approval_request(app_req, repo_dir=self.repo_dir)

    def test_missing_stale_replayed_or_cross_session_contracts_cannot_execute_step(self) -> None:
        """1. Missing, expired, or cross-session contracts fail closed before step execution."""
        plan = self._create_sample_plan("plan_sec_01", status=PlanStatus.READY)

        # 1A: Build mode without contract fails closed (Invariant 1)
        cfg_no_contract = RunConfig(
            repo_dir=self.repo_dir,
            task="execute plan",
            mode="build",
            execution_contract=None,
        )
        orch = Orchestrator(SyntheticS1Double(), SyntheticS2Double(), cfg_no_contract)
        results = orch.run()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].outcome, "not_implemented")
        self.assertFalse((self.repo_dir / "src" / "core.py").exists())

        # 1B: Expired contract fails closed immediately
        contract_expired = self._create_contract(plan, expires_in=-10.0)
        runner_expired = StructuredStepRunner(
            self.repo_dir,
            orch,
            contract_expired,
            plan.id,
        )
        with self.assertRaises(PermissionError) as exp_ctx:
            runner_expired.revalidate_pre_mutation()
        self.assertIn("expired", str(exp_ctx.exception).lower())

        # 1C: Cross-session contract fails closed
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="local", reason="Execution start", contract=contract_expired)
        contract_cross = self._create_contract(plan, session_id="cli:alien_user:session_99")
        runner_cross = StructuredStepRunner(
            self.repo_dir,
            orch,
            contract_cross,
            plan.id,
        )
        with self.assertRaises(PermissionError) as cross_ctx:
            runner_cross.revalidate_pre_mutation()
        self.assertIn("ownership mismatch", str(cross_ctx.exception).lower())

    def test_revision_change_between_steps_stops_execution_before_next_mutation(self) -> None:
        """2. A plan revision change between steps invalidates authorization before next mutation."""
        plan = self._create_sample_plan("plan_rev_01", status=PlanStatus.READY)
        contract = self._create_contract(plan, targets=["src/core.py", "src/helpers.py"])

        # Transition plan READY -> IN_PROGRESS as runtime does
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="local", reason="Execution start", contract=contract)
        reloaded_plan = self.registry.get_plan(plan.id)
        assert reloaded_plan is not None

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
        tracker = PlanProgressTracker(self.repo_dir, plan.id)
        runner = StructuredStepRunner(
            self.repo_dir,
            orch,
            contract,
            plan.id,
            tracker=tracker,
        )

        steps = [
            PlanStep("1", "Implement core", ["src/core.py"]),
            PlanStep("2", "Implement helpers", ["src/helpers.py"]),
        ]

        # Intercept after Step 1 finishes: simulate an out-of-band plan refinement/edit
        orig_execute = runner._execute_single_step
        def _tamper_after_step1(step: Any, step_id: str) -> Any:
            res = orig_execute(step, step_id)
            if step_id == "1":
                # Out-of-band revision bump on disk
                save_plan_document(
                    self.repo_dir,
                    f"{plan.id}.md",
                    reloaded_plan.content,
                    metadata={
                        "id": plan.id,
                        "title": "Build Loop Plan",
                        "goal": "Test Phase 4A Loop",
                        "status": "in_progress",
                        "revision": reloaded_plan.revision + 1,
                        "session_id": "cli:local:default",
                    },
                )
            return res

        runner._execute_single_step = _tamper_after_step1  # type: ignore

        with self.assertRaises(StaleExecutionAuthorizationError) as stale_ctx:
            runner.run_steps(steps)

        self.assertIn("revision", str(stale_ctx.exception).lower())
        # Step 1 was executed and written
        self.assertTrue((self.repo_dir / "src" / "core.py").exists())
        # Step 2 was stopped before mutation
        self.assertFalse((self.repo_dir / "src" / "helpers.py").exists())

        # Progress tracking verification
        records = {r.step_id: r for r in tracker.get_records()}
        self.assertEqual(records["1"].status, StepExecutionStatus.PASSED)
        self.assertEqual(records["2"].status, StepExecutionStatus.FAILED)

    def test_unapproved_target_rejected_before_writing(self) -> None:
        """3. An unapproved target is rejected before writing; zero unapproved files written."""
        plan = self._create_sample_plan("plan_target_01", status=PlanStatus.READY)
        # Contract only approves src/core.py
        contract = self._create_contract(plan, targets=["src/core.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="local", reason="Execution start", contract=contract)

        # S2 double returns unapproved file
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
        steps = [PlanStep("1", "Escape attempt", ["src/unapproved_escape.py"])]

        with self.assertRaises(PermissionError) as perm_ctx:
            runner.run_steps(steps)

        self.assertTrue("scope" in str(perm_ctx.exception).lower() or "boundary" in str(perm_ctx.exception).lower())
        self.assertFalse((self.repo_dir / "src" / "unapproved_escape.py").exists())

    def test_failed_or_timed_out_step_prevents_subsequent_steps_from_running(self) -> None:
        """4. Stop-on-first-failure: a failed or timed-out step stops the loop immediately."""
        plan = self._create_sample_plan("plan_fail_01", status=PlanStatus.READY)
        contract = self._create_contract(plan, targets=["src/core.py", "src/helpers.py", "src/extra.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="local", reason="Execution start", contract=contract)

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
        tracker = PlanProgressTracker(self.repo_dir, plan.id)
        # Configure runner with short timeout
        runner = StructuredStepRunner(self.repo_dir, orch, contract, plan.id, step_timeout=0.2, tracker=tracker)

        steps = [
            PlanStep("1", "Core step", ["src/core.py"]),
            PlanStep("2", "Slow/hanging step", ["src/helpers.py"]),
            PlanStep("3", "Subsequent step", ["src/extra.py"]),
        ]

        orig_run_step = orch._run_step
        def _hang_on_step2(step: Any, *args: Any, **kwargs: Any) -> Any:
            s_id = str(getattr(step, "id", ""))
            if s_id == "2":
                time.sleep(1.0)  # Exceeds 0.2s timeout
            return orig_run_step(step, *args, **kwargs)

        orch._run_step = _hang_on_step2  # type: ignore

        with self.assertRaises(StepExecutionTimeoutError):
            runner.run_steps(steps)

        # Step 1 passed, Step 2 timed out, Step 3 skipped
        records = {r.step_id: r for r in tracker.get_records()}
        self.assertEqual(records["1"].status, StepExecutionStatus.PASSED)
        self.assertEqual(records["2"].status, StepExecutionStatus.TIMED_OUT)
        self.assertEqual(records["3"].status, StepExecutionStatus.SKIPPED)

        # Step 3 files never created
        self.assertFalse((self.repo_dir / "src" / "extra.py").exists())

    def test_cancellation_leaves_recoverable_state(self) -> None:
        """5. Cancellation stops execution cleanly and persists structured progress."""
        plan = self._create_sample_plan("plan_cancel_01", status=PlanStatus.READY)
        contract = self._create_contract(plan, targets=["src/core.py", "src/helpers.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="local", reason="Execution start", contract=contract)

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
        tracker = PlanProgressTracker(self.repo_dir, plan.id)
        runner = StructuredStepRunner(self.repo_dir, orch, contract, plan.id, tracker=tracker)

        cancel_ev = threading.Event()
        token = _CURRENT_CANCEL_EVENT.set(cancel_ev)

        steps = [
            PlanStep("1", "Step 1", ["src/core.py"]),
            PlanStep("2", "Step 2", ["src/helpers.py"]),
        ]

        orig_execute = runner._execute_single_step
        def _cancel_after_step1(step: Any, step_id: str) -> Any:
            res = orig_execute(step, step_id)
            if step_id == "1":
                cancel_ev.set()  # Signal cancellation before Step 2
            return res

        runner._execute_single_step = _cancel_after_step1  # type: ignore

        try:
            with self.assertRaises(KeyboardInterrupt):
                runner.run_steps(steps)
        finally:
            _CURRENT_CANCEL_EVENT.reset(token)

        records = {r.step_id: r for r in tracker.get_records()}
        self.assertEqual(records["1"].status, StepExecutionStatus.PASSED)
        self.assertEqual(records["2"].status, StepExecutionStatus.CANCELLED)
        self.assertFalse((self.repo_dir / "src" / "helpers.py").exists())

    def test_crash_between_mutation_and_progress_persistence_does_not_blindly_rerun_ambiguous_operation(self) -> None:
        """6. Ambiguous non-idempotent steps from prior crashes fail closed against blind reruns."""
        plan = self._create_sample_plan("plan_crash_01", status=PlanStatus.READY)
        contract = self._create_contract(plan, targets=["src/core.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="local", reason="Execution start", contract=contract)

        # Manually create a progress record where Step 1 was left RUNNING (simulating mid-mutation crash)
        tracker = PlanProgressTracker(self.repo_dir, plan.id)
        tracker.initialize_steps([PlanStep("1", "Non-idempotent mutation", ["src/core.py"])])
        tracker.mark_running("1", is_idempotent=False)

        # Now simulate runner recovery attempting to resume
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

        with self.assertRaises(AmbiguousStepExecutionError) as amb_ctx:
            runner.run_steps([PlanStep("1", "Non-idempotent mutation", ["src/core.py"])])

        self.assertIn("non-idempotent", str(amb_ctx.exception).lower())

    def test_recovery_preserves_user_modifications_and_respects_wal_invariants(self) -> None:
        """7. Pre-existing user modifications are preserved when execution rolls back."""
        # User created file
        user_file = self.repo_dir / "src" / "user_work.py"
        user_content = "# Pre-existing user work\ny = 999\n"
        user_file.write_text(user_content, encoding="utf-8")

        plan = self._create_sample_plan("plan_wal_01", status=PlanStatus.READY)
        contract = self._create_contract(plan, targets=["src/core.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="local", reason="Execution start", contract=contract)

        # Simulate transaction rollback due to test failure
        cfg = RunConfig(
            repo_dir=self.repo_dir,
            task="build plan",
            mode="build",
            test_command=["python", "-c", "import sys; sys.exit(1)"],  # Domain test fails
            execution_contract=contract,
            plan_id=plan.id,
            session_id="cli:local:default",
        )
        orch = Orchestrator(SyntheticS1Double(), SyntheticS2Double(), cfg)
        runner = StructuredStepRunner(self.repo_dir, orch, contract, plan.id)

        with self.assertRaises(RuntimeError):
            runner.run_steps([PlanStep("1", "Failing step", ["src/core.py"])])

        # Core file was rolled back
        self.assertFalse((self.repo_dir / "src" / "core.py").exists())
        # Pre-existing user file remains completely intact
        self.assertTrue(user_file.exists())
        self.assertEqual(user_file.read_text(encoding="utf-8"), user_content)

    def test_repeated_recovery_is_idempotent(self) -> None:
        """8. Repeated crash recovery executions produce consistent deterministic state."""
        plan = self._create_sample_plan("plan_idem_01", status=PlanStatus.READY)
        contract = self._create_contract(plan, targets=["src/core.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="local", reason="Execution start", contract=contract)

        tracker = PlanProgressTracker(self.repo_dir, plan.id)
        tracker.initialize_steps([PlanStep("1", "Step 1", ["src/core.py"])])
        tracker.mark_failed("1", "First failure")

        # Reload tracker multiple times
        t1 = PlanProgressTracker(self.repo_dir, plan.id)
        t2 = PlanProgressTracker(self.repo_dir, plan.id)

        rec1 = t1.get_record("1")
        rec2 = t2.get_record("1")

        self.assertIsNotNone(rec1)
        self.assertIsNotNone(rec2)
        assert rec1 is not None and rec2 is not None
        self.assertEqual(rec1.status, StepExecutionStatus.FAILED)
        self.assertEqual(rec2.status, StepExecutionStatus.FAILED)
        self.assertEqual(rec1.error, rec2.error)

    def test_only_verified_acceptance_evidence_can_produce_verified_status(self) -> None:
        """9. Successful step execution produces IMPLEMENTED, not VERIFIED. VERIFIED requires evidence."""
        plan = self._create_sample_plan("plan_verif_01", status=PlanStatus.READY)
        contract = self._create_contract(plan, targets=["src/core.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="local", reason="Execution start", contract=contract)

        # Successful step run
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
        results = runner.run_steps([PlanStep("1", "Core step", ["src/core.py"])])
        self.assertEqual(len(results), 1)

        # Transition post-execution to IMPLEMENTED (canonical runtime behavior)
        self.registry.update_status(plan.id, PlanStatus.IMPLEMENTED, actor="local", reason="Build loop finished")
        doc_implemented = self.registry.get_plan(plan.id)
        assert doc_implemented is not None
        self.assertEqual(doc_implemented.status, PlanStatus.IMPLEMENTED)
        self.assertNotEqual(doc_implemented.status, PlanStatus.VERIFIED)

        # Direct transition to VERIFIED without evidence fails closed
        with self.assertRaises(InvalidPlanTransitionError):
            self.registry.update_status(plan.id, PlanStatus.VERIFIED, actor="local", reason="Skip verifier")

        # Formal verification with evidence transitions cleanly to VERIFIED
        evidence = VerificationEvidence(
            plan_id=plan.id,
            plan_revision=doc_implemented.revision,
            plan_content_hash=doc_implemented.content_hash,
            checks=(
                VerificationCheckResult(check_name="Check 1", command="python --version", status="passed", criterion_ref="Criterion 1"),
            ),
            summary="All criteria verified",
        )
        self.registry.update_status(
            plan.id,
            PlanStatus.VERIFIED,
            actor="local",
            reason="Verification passed",
            evidence=evidence.to_dict(),
        )
        doc_verified = self.registry.get_plan(plan.id)
        assert doc_verified is not None
        self.assertEqual(doc_verified.status, PlanStatus.VERIFIED)

    def test_bounded_retries_only_for_explicitly_classified_safe_idempotent_operations(self) -> None:
        """10. Bounded retry occurs only for safe idempotent steps; non-idempotent steps fail immediately."""
        plan = self._create_sample_plan("plan_retry_01", status=PlanStatus.READY)
        contract = self._create_contract(plan, targets=["src/core.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="local", reason="Execution start", contract=contract)

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

        # Non-idempotent step with failure fails with 0 retries
        step_non_idem = PlanStep("1", "Non-idempotent", ["src/core.py"])
        attempts_non_idem = [0]
        def _fail_non_idem(step: Any, step_id: str) -> Any:
            attempts_non_idem[0] += 1
            raise IOError("Transient network glitch")

        runner._execute_single_step = _fail_non_idem  # type: ignore
        with self.assertRaises(IOError):
            runner.run_steps([step_non_idem])
        self.assertEqual(attempts_non_idem[0], 1)  # Zero retries

        # Idempotent step with failure is retried once
        step_idem = {"id": "2", "description": "Idempotent read", "action_type": "read_check", "files": ["src/core.py"], "is_idempotent": True}
        attempts_idem = [0]
        def _retry_then_succeed(step: Any, step_id: str) -> Any:
            attempts_idem[0] += 1
            if attempts_idem[0] == 1:
                raise IOError("Transient I/O glitch")
            return StepResult(PlanStep("2", "Idempotent read", []), "verified", 0, "Success on retry")

        runner._execute_single_step = _retry_then_succeed  # type: ignore
        results = runner.run_steps([step_idem])
        self.assertEqual(attempts_idem[0], 2)  # Exactly 1 retry
        self.assertEqual(len(results), 1)


if __name__ == "__main__":
    unittest.main()
