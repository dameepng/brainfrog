"""BrainFrog Phase 4B.1 — Execution Summary & Recovery Adversarial Audit Suite.

Verifies:
1. Every possible step status appears in exactly the intended summary category (mutually exclusive and exhaustive).
2. Unknown or malformed status values fail closed.
3. Active RUNNING versus abandoned RUNNING after restart.
4. Interrupted idempotent and non-idempotent steps.
5. Two consecutive executions with different plans and sessions (strict isolation).
6. Failure and cancellation after an earlier step has committed.
7. Crash after workspace commit but before progress update (recovers as ambiguous).
8. Progress write failure after successful mutation fails closed.
9. Corrupt or stale progress file fails closed.
10. Reconciliation without independently verifiable transaction and acceptance evidence fails closed.
11. Execution summary must never imply VERIFIED from PASSED alone.
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
from typing import Any, Dict, List, Optional, Tuple
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
from core.runtime.process import _CURRENT_CANCEL_EVENT
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


class TestPhase4B1ExecutionRecoveryAudit(unittest.TestCase):
    """Adversarial test suite for Phase 4B.1 execution summary and recovery audit."""

    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="bf_audit_4b1_")).resolve()
        self.repo_dir = self.temp_dir / "workspace"
        self.repo_dir.mkdir(parents=True)
        self.plans_dir = self.repo_dir / ".brainfrog" / "plans"
        self.plans_dir.mkdir(parents=True)
        self.tx_dir = self.repo_dir / ".brainfrog" / "transactions"
        self.tx_dir.mkdir(parents=True)
        (self.repo_dir / "src").mkdir(parents=True)

        subprocess.run(["git", "init"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "AuditTester"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "audit@synthetic.local"], cwd=self.repo_dir, capture_output=True, check=True)
        (self.repo_dir / "README.md").write_text("# Audit Workspace\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "Init commit"], cwd=self.repo_dir, capture_output=True, check=True)

        self.registry = PlanRegistry(self.repo_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_sample_plan(
        self,
        plan_id: str = "plan_4b1_01",
        title: str = "Audit Plan",
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
            f"# {title}\n- **Goal**: Test Phase 4B.1 Audit\n",
            metadata={
                "id": plan_id,
                "title": title,
                "goal": "Test Phase 4B.1 Audit",
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
        session_incarnation: str = "inc_4b1_1",
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
    # Test 1: Every Possible Status Appears in Intended Summary Category
    # =========================================================================
    def test_all_statuses_partition_mutually_exclusive_and_exhaustive(self) -> None:
        """1. Every possible StepExecutionStatus appears in exactly one category (exhaustive and disjoint)."""
        plan = self._create_sample_plan("plan_partition_01")
        tracker = PlanProgressTracker(self.repo_dir, plan.id)

        # Build records exercising all 8 enum statuses (including both idempotent and non-idempotent variations)
        records_to_seed = [
            StepProgressRecord("s_passed", "passed step", status=StepExecutionStatus.PASSED),
            StepProgressRecord("s_failed", "failed step", status=StepExecutionStatus.FAILED),
            StepProgressRecord("s_timeout", "timeout step", status=StepExecutionStatus.TIMED_OUT),
            StepProgressRecord("s_cancelled", "cancelled step", status=StepExecutionStatus.CANCELLED),
            StepProgressRecord("s_skipped", "skipped step", status=StepExecutionStatus.SKIPPED),
            StepProgressRecord("s_pending", "pending step", status=StepExecutionStatus.PENDING),
            StepProgressRecord("s_run_idem", "running idempotent", status=StepExecutionStatus.RUNNING, is_idempotent=True),
            StepProgressRecord("s_run_nonidem", "running non-idempotent", status=StepExecutionStatus.RUNNING, is_idempotent=False),
            StepProgressRecord("s_int_idem", "interrupted idempotent", status=StepExecutionStatus.INTERRUPTED, is_idempotent=True),
            StepProgressRecord("s_int_nonidem", "interrupted non-idempotent", status=StepExecutionStatus.INTERRUPTED, is_idempotent=False),
        ]
        with tracker._get_lock():
            for r in records_to_seed:
                tracker._records[r.step_id] = r
            tracker._persist()

        summary = tracker.get_execution_summary()
        completed = set(summary["completed"])
        failed = set(summary["failed"])
        requiring_reconciliation = set(summary["requiring_reconciliation"])

        # Invariant 1A: Mutual exclusivity (pairwise disjoint)
        self.assertEqual(completed & failed, set(), "completed and failed must be disjoint")
        self.assertEqual(completed & requiring_reconciliation, set(), "completed and requiring_reconciliation must be disjoint")
        self.assertEqual(failed & requiring_reconciliation, set(), "failed and requiring_reconciliation must be disjoint")

        # Invariant 1B: Exhaustiveness (union equals all records)
        all_ids = {r.step_id for r in records_to_seed}
        union_all = completed | failed | requiring_reconciliation
        self.assertEqual(union_all, all_ids, "All records must be accounted for in summary")

        # Invariant 1C: Specific categorization
        self.assertEqual(completed, {"s_passed"})
        self.assertEqual(
            failed,
            {"s_failed", "s_timeout", "s_cancelled", "s_skipped", "s_pending", "s_run_idem", "s_int_idem"}
        )
        self.assertEqual(requiring_reconciliation, {"s_run_nonidem", "s_int_nonidem"})

    # =========================================================================
    # Test 2: Unknown or Malformed Status Values Fail Closed
    # =========================================================================
    def test_unknown_or_malformed_status_values_fail_closed(self) -> None:
        """2. Unknown or malformed status values fail closed with ValueError."""
        # 2A: from_dict rejects unknown string status
        with self.assertRaises(ValueError) as ctx1:
            StepProgressRecord.from_dict({"step_id": "s1", "status": "unknown_future_status"})
        self.assertIn("Unknown or malformed step execution status", str(ctx1.exception))

        # 2B: from_dict rejects missing step_id
        with self.assertRaises(ValueError) as ctx2:
            StepProgressRecord.from_dict({"step_id": "", "status": "passed"})
        self.assertIn("missing or empty 'step_id'", str(ctx2.exception))

        # 2C: from_dict rejects missing status
        with self.assertRaises(ValueError) as ctx3:
            StepProgressRecord.from_dict({"step_id": "s1"})
        self.assertIn("missing 'status' field", str(ctx3.exception))

        # 2D: get_execution_summary fails closed if record in memory has malformed status
        plan = self._create_sample_plan("plan_bad_status")
        tracker = PlanProgressTracker(self.repo_dir, plan.id)
        with tracker._get_lock():
            tracker._records["bad_step"] = StepProgressRecord("bad_step", "desc", status="invalid_status")  # type: ignore
        with self.assertRaises(ValueError) as ctx4:
            tracker.get_execution_summary()
        self.assertIn("Unknown or malformed step execution status", str(ctx4.exception))

    # =========================================================================
    # Test 3: Active RUNNING Versus Abandoned RUNNING After Restart
    # =========================================================================
    def test_active_running_versus_abandoned_running_after_restart(self) -> None:
        """3. Live runner active RUNNING is tracked actively; after restart, RUNNING is identified as abandoned."""
        plan = self._create_sample_plan("plan_active_vs_abandoned")
        contract, _, _ = self._create_contract(plan, targets=["src/mod1.py", "src/mod2.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="local", reason="Start", contract=contract)

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

        # Hook into execution: verify active state DURING step 1
        observed_active_during_run = []
        orig_execute = runner._execute_single_step
        def _inspect_during_step1(step: Any, step_id: str) -> Any:
            if step_id == "1":
                is_active = runner.tracker.is_step_active("1")
                active_id = runner.tracker.active_step_id
                observed_active_during_run.append((is_active, active_id))
            return orig_execute(step, step_id)

        runner._execute_single_step = _inspect_during_step1  # type: ignore
        runner.run_steps([PlanStep("1", "Implement mod1", ["src/mod1.py"])])

        self.assertEqual(observed_active_during_run, [(True, "1")], "Step 1 must be active during live execution")
        self.assertFalse(runner.tracker.is_step_active("1"), "Step 1 must no longer be active once passed")

        # Now simulate an abandoned RUNNING step after crash/restart
        plan_crash = self._create_sample_plan("plan_crashed_run")
        tracker_crashed = PlanProgressTracker(self.repo_dir, plan_crash.id)
        tracker_crashed.initialize_steps([{"id": "c1", "description": "Crashed step", "is_idempotent": False}])
        tracker_crashed.mark_running("c1", is_idempotent=False)

        # Simulate new process / restart
        tracker_restarted = PlanProgressTracker(self.repo_dir, plan_crash.id)
        self.assertFalse(tracker_restarted.is_step_active("c1"), "Step c1 must NOT be active after restart")
        self.assertIsNone(tracker_restarted.active_step_id, "No step should be active after restart")

        # Abandoned non-idempotent step is caught before rerun
        ambig = tracker_restarted.check_ambiguous_interrupted_step()
        self.assertIsNotNone(ambig)
        assert ambig is not None
        self.assertEqual(ambig.step_id, "c1")

    # =========================================================================
    # Test 4: Interrupted Idempotent and Non-Idempotent Steps
    # =========================================================================
    def test_interrupted_idempotent_and_non_idempotent_steps(self) -> None:
        """4. Idempotent interrupted step is retryable; non-idempotent step blocks restart with AmbiguousStepExecutionError."""
        plan_idem = self._create_sample_plan("plan_int_idem")
        contract_idem, _, _ = self._create_contract(plan_idem, targets=["src/mod1.py"])
        self.registry.update_status(plan_idem.id, PlanStatus.IN_PROGRESS, actor="local", reason="Start", contract=contract_idem)

        # Seed an interrupted idempotent step (valid read-only inspect action)
        tracker_idem = PlanProgressTracker(self.repo_dir, plan_idem.id)
        tracker_idem.initialize_steps([{"id": "1", "description": "Idempotent inspect step", "is_idempotent": True, "action_type": "inspect", "command": ["git", "status"]}])
        tracker_idem.mark_interrupted("1", "Process was SIGKILLed")

        summary_idem = tracker_idem.get_execution_summary()
        self.assertIn("1", summary_idem["failed"])
        self.assertEqual(summary_idem["requiring_reconciliation"], [])

        # Restart runner on idempotent step: check_ambiguous_interrupted_step returns None (safe to rerun)
        self.assertIsNone(tracker_idem.check_ambiguous_interrupted_step())

        # Non-idempotent counterpart
        plan_nonidem = self._create_sample_plan("plan_int_nonidem")
        contract_nonidem, _, _ = self._create_contract(plan_nonidem, targets=["src/mod1.py"])
        self.registry.update_status(plan_nonidem.id, PlanStatus.IN_PROGRESS, actor="local", reason="Start", contract=contract_nonidem)

        tracker_nonidem = PlanProgressTracker(self.repo_dir, plan_nonidem.id)
        tracker_nonidem.initialize_steps([{"id": "1", "description": "Non-idempotent step", "is_idempotent": False}])
        tracker_nonidem.mark_interrupted("1", "Process was SIGKILLed")

        summary_nonidem = tracker_nonidem.get_execution_summary()
        self.assertIn("1", summary_nonidem["requiring_reconciliation"])
        self.assertEqual(summary_nonidem["completed"], [])

        # Restart runner on non-idempotent step: fails closed with AmbiguousStepExecutionError
        cfg = RunConfig(
            repo_dir=self.repo_dir, task="build", mode="build",
            execution_contract=contract_nonidem, plan_id=plan_nonidem.id,
        )
        orch = Orchestrator(SyntheticS1Double(), SyntheticS2Double(), cfg)
        runner_nonidem = StructuredStepRunner(self.repo_dir, orch, contract_nonidem, plan_nonidem.id, tracker=tracker_nonidem)
        with self.assertRaises(AmbiguousStepExecutionError) as amb_ctx:
            runner_nonidem.run_steps([PlanStep("1", "Non-idempotent step", ["src/mod1.py"])])
        self.assertIn("blind rerun is forbidden", str(amb_ctx.exception).lower())

    # =========================================================================
    # Test 5: Two Consecutive Executions with Different Plans and Sessions
    # =========================================================================
    def test_two_consecutive_executions_with_different_plans_and_sessions(self) -> None:
        """5. Two executions with different plans and sessions maintain complete isolation."""
        plan_a = self._create_sample_plan("plan_session_a", session_id="cli:user_a:session_a")
        plan_b = self._create_sample_plan("plan_session_b", session_id="cli:user_b:session_b")

        contract_a, _, _ = self._create_contract(plan_a, targets=["src/mod1.py"], session_id="cli:user_a:session_a", session_incarnation="inc_a")
        contract_b, _, _ = self._create_contract(plan_b, targets=["src/mod2.py"], session_id="cli:user_b:session_b", session_incarnation="inc_b")

        self.registry.update_status(plan_a.id, PlanStatus.IN_PROGRESS, actor="user_a", reason="Start A", contract=contract_a)
        self.registry.update_status(plan_b.id, PlanStatus.IN_PROGRESS, actor="user_b", reason="Start B", contract=contract_b)

        # Run Plan A
        cfg_a = RunConfig(
            repo_dir=self.repo_dir, task="build A", mode="build",
            execution_contract=contract_a, plan_id=plan_a.id, session_id="cli:user_a:session_a",
        )
        orch_a = Orchestrator(SyntheticS1Double(), SyntheticS2Double(step_file_map={"1": {"src/mod1.py": "a = 1\n"}}), cfg_a)
        runner_a = StructuredStepRunner(self.repo_dir, orch_a, contract_a, plan_a.id)
        runner_a.run_steps([PlanStep("1", "Step A", ["src/mod1.py"])])

        # Run Plan B
        cfg_b = RunConfig(
            repo_dir=self.repo_dir, task="build B", mode="build",
            execution_contract=contract_b, plan_id=plan_b.id, session_id="cli:user_b:session_b",
        )
        orch_b = Orchestrator(SyntheticS1Double(), SyntheticS2Double(step_file_map={"2": {"src/mod2.py": "b = 2\n"}}), cfg_b)
        runner_b = StructuredStepRunner(self.repo_dir, orch_b, contract_b, plan_b.id)
        runner_b.run_steps([PlanStep("2", "Step B", ["src/mod2.py"])])

        # Verify summaries are strictly scoped to their own plans and sessions
        summary_a = runner_a.get_summary()
        summary_b = runner_b.get_summary()

        self.assertEqual(summary_a["completed"], ["1"])
        self.assertEqual(summary_b["completed"], ["2"])
        self.assertNotIn("2", summary_a["completed"])
        self.assertNotIn("1", summary_b["completed"])

        # Cross-session reuse is strictly forbidden
        runner_cross = StructuredStepRunner(self.repo_dir, orch_a, contract_b, plan_a.id)
        with self.assertRaises(StaleExecutionAuthorizationError) as cross_ctx:
            runner_cross.revalidate_pre_mutation()
        self.assertIn("Contract plan_id 'plan_session_b' does not match runner plan_id 'plan_session_a'", str(cross_ctx.exception))

    # =========================================================================
    # Test 6: Failure and Cancellation After an Earlier Step Has Committed
    # =========================================================================
    def test_failure_and_cancellation_after_earlier_step_committed(self) -> None:
        """6. Failure/cancellation in step 2 preserves step 1 in completed and puts step 2 in failed."""
        plan = self._create_sample_plan("plan_fail_after_commit")
        contract, _, _ = self._create_contract(plan, targets=["src/mod1.py", "src/mod2.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="local", reason="Start", contract=contract)

        cfg = RunConfig(
            repo_dir=self.repo_dir, task="build", mode="build",
            execution_contract=contract, plan_id=plan.id, session_id="cli:local:default",
        )
        orch = Orchestrator(SyntheticS1Double(), SyntheticS2Double(), cfg)
        runner = StructuredStepRunner(self.repo_dir, orch, contract, plan.id)

        # Step 1 commits normally; Step 2 fails
        orig_execute = runner._execute_single_step
        def _execute_with_step2_failure(step: Any, step_id: str) -> Any:
            if step_id == "2":
                raise RuntimeError("Step 2 network disconnect")
            return orig_execute(step, step_id)

        runner._execute_single_step = _execute_with_step2_failure  # type: ignore

        steps = [
            PlanStep("1", "Implement mod1", ["src/mod1.py"]),
            PlanStep("2", "Implement mod2", ["src/mod2.py"]),
        ]

        with self.assertRaises(RuntimeError) as err:
            runner.run_steps(steps)
        self.assertIn("Step 2 network disconnect", str(err.exception))

        # Check summary: step 1 was completed and committed; step 2 is failed
        summary = runner.get_summary()
        self.assertEqual(summary["completed"], ["1"])
        self.assertEqual(summary["failed"], ["2"])
        self.assertEqual(summary["requiring_reconciliation"], [])
        self.assertTrue((self.repo_dir / "src" / "mod1.py").exists())
        self.assertFalse((self.repo_dir / "src" / "mod2.py").exists())

    # =========================================================================
    # Test 7: Crash After Workspace Commit But Before Progress Update
    # =========================================================================
    def test_crash_after_workspace_commit_before_progress_update_recovers_as_ambiguous(self) -> None:
        """7. Transaction committed to workspace before progress update recovers as ambiguous; never blindly rerun."""
        plan = self._create_sample_plan("plan_crash_after_tx")
        contract, _, _ = self._create_contract(plan, targets=["src/mod1.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="local", reason="Start", contract=contract)

        tracker = PlanProgressTracker(self.repo_dir, plan.id)
        tracker.initialize_steps([PlanStep("1", "Mutate mod1", ["src/mod1.py"])])
        tracker.mark_running("1", is_idempotent=False)

        # Simulate that workspace transaction committed on disk
        tx_id = "tx_committed_before_crash_01"
        tx_file = self.tx_dir / f"{tx_id}.json"
        tx_file.write_text(json.dumps({"id": tx_id, "status": "committed"}), encoding="utf-8")
        (self.repo_dir / "src" / "mod1.py").write_text("# Committed file before crash\n", encoding="utf-8")

        # Progress file still has step 1 in RUNNING state (crash happened before mark_passed persisted)
        reloaded_tracker = PlanProgressTracker(self.repo_dir, plan.id)
        rec = reloaded_tracker.get_record("1")
        assert rec is not None
        self.assertEqual(rec.status, StepExecutionStatus.RUNNING)

        # Verification: check_ambiguous_interrupted_step detects unverified mutation on disk
        ambiguous = reloaded_tracker.check_ambiguous_interrupted_step()
        self.assertIsNotNone(ambiguous)
        assert ambiguous is not None
        self.assertEqual(ambiguous.step_id, "1")

        # Attempting to restart runner raises AmbiguousStepExecutionError (never blindly rerun)
        cfg = RunConfig(
            repo_dir=self.repo_dir, task="build", mode="build",
            execution_contract=contract, plan_id=plan.id, session_id="cli:local:default",
        )
        orch = Orchestrator(SyntheticS1Double(), SyntheticS2Double(), cfg)
        runner = StructuredStepRunner(self.repo_dir, orch, contract, plan.id, tracker=reloaded_tracker)

        with self.assertRaises(AmbiguousStepExecutionError) as amb_ctx:
            runner.run_steps([PlanStep("1", "Mutate mod1", ["src/mod1.py"])])
        self.assertIn("blind rerun is forbidden", str(amb_ctx.exception).lower())

    # =========================================================================
    # Test 8: Progress Write Failure After Successful Mutation
    # =========================================================================
    def test_progress_write_failure_after_successful_mutation_fails_closed(self) -> None:
        """8. If persisting progress fails after mutation, runner fails closed without fabricating PASSED."""
        plan = self._create_sample_plan("plan_progress_write_fail")
        contract, _, _ = self._create_contract(plan, targets=["src/mod1.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="local", reason="Start", contract=contract)

        cfg = RunConfig(
            repo_dir=self.repo_dir, task="build", mode="build",
            execution_contract=contract, plan_id=plan.id, session_id="cli:local:default",
        )
        orch = Orchestrator(SyntheticS1Double(), SyntheticS2Double(), cfg)
        runner = StructuredStepRunner(self.repo_dir, orch, contract, plan.id)

        # Simulate I/O failure during atomic progress persistence
        with patch("core.runtime.step_runner._safe_atomic_write_json", side_effect=OSError("Disk write I/O error")):
            with self.assertRaises(OSError) as io_ctx:
                runner.run_steps([PlanStep("1", "Mutate mod1", ["src/mod1.py"])])
            self.assertIn("Disk write I/O error", str(io_ctx.exception))

    # =========================================================================
    # Test 9: Corrupt or Stale Progress File Fails Closed
    # =========================================================================
    def test_corrupt_or_stale_progress_file_fails_closed(self) -> None:
        """9. Corrupted progress JSON or mismatched plan_id fails closed on load."""
        plan = self._create_sample_plan("plan_corrupt_file")
        prog_file = self.plans_dir / f"{plan.id}.progress.json"

        # 9A: Corrupted JSON content
        prog_file.write_bytes(b'{"truncated_json": [')
        with self.assertRaises(ValueError) as err1:
            PlanProgressTracker(self.repo_dir, plan.id)
        self.assertIn("Corrupt progress file", str(err1.exception))

        # 9B: Mismatched plan_id
        prog_file.write_text(json.dumps({"plan_id": "alien_plan_id", "steps": []}), encoding="utf-8")
        with self.assertRaises(ValueError) as err2:
            PlanProgressTracker(self.repo_dir, plan.id)
        self.assertIn("plan_id mismatch", str(err2.exception))

        # 9C: Malformed root (not a dict)
        prog_file.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
        with self.assertRaises(ValueError) as err3:
            PlanProgressTracker(self.repo_dir, plan.id)
        self.assertIn("root must be a dict", str(err3.exception))

    # =========================================================================
    # Test 10: Reconciliation Without Independently Verifiable Transaction/Acceptance
    # =========================================================================
    def test_reconciliation_without_independently_verifiable_evidence_fails_closed(self) -> None:
        """10. Reconciliation cannot promote to PASSED on rolled back transactions or promote plan to VERIFIED."""
        plan = self._create_sample_plan("plan_recon_audit")
        tracker = PlanProgressTracker(self.repo_dir, plan.id)
        tracker.initialize_steps([{"id": "1", "description": "Step 1", "is_idempotent": False}])
        tracker.mark_running("1")

        # 10A: Free-form text cannot override rolled-back transaction
        with self.assertRaises(ValueError) as ctx1:
            tracker.reconcile_ambiguous_step(
                "1",
                new_status=StepExecutionStatus.PASSED,
                operator="lead_auditor",
                operator_attestation="Claiming all passed despite rollback",
                transaction_status="rolled_back",
            )
        self.assertIn("rolled back or failed", str(ctx1.exception).lower())

        # 10B: Reconciling without verified transaction leaves transaction_verified=False
        rec = tracker.reconcile_ambiguous_step(
            "1",
            new_status=StepExecutionStatus.PASSED,
            operator="lead_auditor",
            operator_attestation="Manual disk inspection",
        )
        self.assertFalse(rec.transaction_verified)
        self.assertEqual(rec.reconciliation_type, "operator_attestation")

        # 10C: Operator attestation text can NEVER transition plan to VERIFIED without acceptance evidence
        contract_recon, _, _ = self._create_contract(plan, targets=["src/mod1.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="local", reason="Start", contract=contract_recon)
        self.registry.update_status(plan.id, PlanStatus.IMPLEMENTED, actor="local", reason="Implemented")

        with self.assertRaises(InvalidPlanTransitionError) as ctx2:
            self.registry.update_status(
                plan.id,
                PlanStatus.VERIFIED,
                actor="lead_auditor",
                reason="Attempting to use reconciliation text as verification",
                evidence=None,  # No VerificationEvidence
            )
        self.assertIn("Transition to 'verified' requires recorded verification evidence", str(ctx2.exception))

    # =========================================================================
    # Test 11: Execution Summary Must Never Imply VERIFIED from PASSED Alone
    # =========================================================================
    def test_execution_summary_never_implies_verified_from_passed_alone(self) -> None:
        """11. All steps PASSED in summary does NOT mark plan VERIFIED; plan remains IN_PROGRESS or IMPLEMENTED."""
        plan = self._create_sample_plan("plan_not_auto_verified")
        contract, _, _ = self._create_contract(plan, targets=["src/mod1.py", "src/mod2.py"])
        self.registry.update_status(plan.id, PlanStatus.IN_PROGRESS, actor="local", reason="Start", contract=contract)

        cfg = RunConfig(
            repo_dir=self.repo_dir, task="build", mode="build",
            execution_contract=contract, plan_id=plan.id, session_id="cli:local:default",
        )
        orch = Orchestrator(SyntheticS1Double(), SyntheticS2Double(), cfg)
        runner = StructuredStepRunner(self.repo_dir, orch, contract, plan.id)

        steps = [
            PlanStep("1", "Implement mod1", ["src/mod1.py"]),
            PlanStep("2", "Implement mod2", ["src/mod2.py"]),
        ]
        results = runner.run_steps(steps)
        self.assertEqual(len(results), 2)

        # Execution summary shows both completed
        summary = runner.get_summary()
        self.assertEqual(summary["completed"], ["1", "2"])

        # Plan status on disk is STILL strictly IN_PROGRESS
        doc_disk = self.registry.get_plan(plan.id)
        assert doc_disk is not None
        self.assertEqual(doc_disk.status, PlanStatus.IN_PROGRESS)
        self.assertNotEqual(doc_disk.status, PlanStatus.VERIFIED)

        # Even after transitioning to IMPLEMENTED, plan is NOT VERIFIED
        self.registry.update_status(plan.id, PlanStatus.IMPLEMENTED, actor="local", reason="Build loop done")
        doc_impl = self.registry.get_plan(plan.id)
        assert doc_impl is not None
        self.assertEqual(doc_impl.status, PlanStatus.IMPLEMENTED)
        self.assertNotEqual(doc_impl.status, PlanStatus.VERIFIED)


if __name__ == "__main__":
    unittest.main()
