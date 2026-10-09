"""BrainFrog Phase 6B — Unified CLI Verification Vertical Slice Regression Suite.

Product Thesis:
    "Don't just trust the agent. Verify it."

Required Invariants Tested:
1. Primary CLI coding-task path invokes canonical verification pipeline.
2. Completed CLI coding task produces JSON and Markdown proof artifacts tied to same task ID.
3. CLI displays actual TaskResult verification verdict and returns appropriate exit codes (0 on VERIFIED, non-zero on UNVERIFIED/failure).
4. Missing test command cannot produce verified=True through synthetic evidence.
5. Model-reported success outcome (drafted_pr, opened_pr, verified) cannot substitute for executed test process.
6. Real test process returning non-zero fails verification and triggers transaction rollback.
7. Proof artifacts accurately distinguish tests that ran, tests that failed, and tests that did not run.
8. Git initial and final HEAD values are sampled at correct lifecycle points.
9. Existing conversational CLI behavior still works for paths intentionally outside autonomous coding execution.
10. Recovery-required and cancellation outcomes do not appear as ordinary verified success.
11. Realistic end-to-end CLI test in a temporary workspace.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import cli
from brainfrog.sdk.client import BrainFrogClient
from system1.base import Answer
from core.runtime import (
    BrainFrogRuntime,
    FileProofStore,
    HardenedProcessResult,
    IncomingMessage,
    OutgoingMessage,
    ProofArtifact,
    TaskFinalizationCoordinator,
    TaskPlan,
    TaskRecoveryRequiredError,
    TaskRequest,
    TaskReservationCoordinator,
    TaskResult,
    TaskStep,
    TransactionCoordinator,
    TransactionResult,
    TransactionStatus,
    VerificationGate,
    capture_git_provenance,
    execute_autonomous_task,
    sample_git_state,
)
from orchestrator import PlanStep, StepResult


class TestPhase6BCLIVerification(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.repo_dir = Path(self.tmp_dir.name).resolve()
        subprocess.run(["git", "init"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "BrainFrog Tester"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "tester@brainfrog.local"], cwd=self.repo_dir, capture_output=True, check=True)
        (self.repo_dir / "README.md").write_text("# Test Repo\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=self.repo_dir, capture_output=True, check=True)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_1_primary_cli_coding_task_path_invokes_canonical_verification_pipeline(self):
        """Invariant 1: Primary CLI coding-task path invokes canonical verification pipeline and reservations."""
        mock_runtime = MagicMock(spec=BrainFrogRuntime)
        test_proc = HardenedProcessResult(args=["pytest"], returncode=0, stdout="1 passed")
        tx_res = TransactionResult(
            transaction_id="tx_123",
            status=TransactionStatus.COMMITTED,
            committed=True,
            rolled_back=False,
            operations=tuple(),
        )

        mock_runtime.handle_message.return_value = OutgoingMessage(
            text="Task completed with verified changes",
            success=True,
            status="completed",
            metadata={
                "results": [StepResult(PlanStep("1", "write math.py", ["math.py"]), "drafted_pr", 0, "OK")],
                "test_result": test_proc,
                "transaction_result": tx_res,
                "transaction_id": "tx_123",
                "transaction_status": "committed",
                "transaction_success": True,
            },
        )

        with patch("cli.execute_autonomous_task", wraps=execute_autonomous_task) as spy_exec:
            exit_code = cli.execute_task(
                task="Implement add function in math.py",
                repo_dir=self.repo_dir,
                mode="build",
                test_cmd="python -c \"import sys; sys.exit(0)\"",
                runtime=mock_runtime,
            )

            # Canonical workflow was invoked
            spy_exec.assert_called_once()
            called_request = spy_exec.call_args[0][1]
            self.assertIsInstance(called_request, TaskRequest)
            self.assertEqual(called_request.workspace, self.repo_dir)
            self.assertTrue(called_request.task_id.startswith("task_"))
            self.assertEqual(called_request.channel, "cli")
            self.assertEqual(called_request.test_command, ["python", "-c", "import sys; sys.exit(0)"])
            self.assertEqual(exit_code, 0)

    def test_2_completed_cli_coding_task_produces_tied_json_and_md_proofs(self):
        """Invariant 2: Completed CLI coding task produces JSON and Markdown proof artifacts tied to same task ID."""
        mock_runtime = MagicMock(spec=BrainFrogRuntime)
        test_proc = HardenedProcessResult(args=["pytest"], returncode=0, stdout="2 passed")
        tx_res = TransactionResult(
            transaction_id="tx_proof_tied",
            status=TransactionStatus.COMMITTED,
            committed=True,
            rolled_back=False,
            operations=tuple(),
        )

        mock_runtime.handle_message.return_value = OutgoingMessage(
            text="Completed coding task",
            success=True,
            status="completed",
            metadata={
                "results": [StepResult(PlanStep("1", "update code", ["mod.py"]), "drafted_pr", 0, "Done")],
                "test_result": test_proc,
                "transaction_result": tx_res,
                "transaction_id": "tx_proof_tied",
                "transaction_status": "committed",
                "transaction_success": True,
            },
        )

        captured_task_id = []
        original_execute = execute_autonomous_task

        def intercept_autonomous(runtime, request):
            captured_task_id.append(request.task_id)
            return original_execute(runtime, request)

        with patch("cli.execute_autonomous_task", side_effect=intercept_autonomous):
            exit_code = cli.execute_task(
                task="Refactor module logic",
                repo_dir=self.repo_dir,
                test_cmd="python -c \"import sys; sys.exit(0)\"",
                runtime=mock_runtime,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(captured_task_id), 1)
        task_id = captured_task_id[0]

        store = FileProofStore(self.repo_dir)
        self.assertTrue(store.exists(task_id))

        proof = store.load(task_id)
        self.assertEqual(proof.task.task_id, task_id)
        self.assertEqual(proof.verdict.status, "VERIFIED")

        md_content = store.load_markdown(task_id)
        self.assertIsNotNone(md_content)
        self.assertIn(task_id, md_content)
        self.assertIn("VERIFIED", md_content)

    def test_3_cli_displays_actual_verdict_and_returns_exit_codes(self):
        """Invariant 3: CLI displays actual TaskResult verification verdict and returns appropriate exit codes."""
        # A. Passing verified task -> returns 0
        mock_runtime_pass = MagicMock(spec=BrainFrogRuntime)
        test_proc_pass = HardenedProcessResult(args=["pytest"], returncode=0, stdout="passed")
        tx_res_pass = TransactionResult(
            transaction_id="tx_pass",
            status=TransactionStatus.COMMITTED,
            committed=True,
            rolled_back=False,
            operations=tuple(),
        )
        mock_runtime_pass.handle_message.return_value = OutgoingMessage(
            text="Pass",
            success=True,
            status="completed",
            metadata={
                "results": [StepResult(PlanStep("1", "ok", []), "drafted_pr", 0, "OK")],
                "test_result": test_proc_pass,
                "transaction_result": tx_res_pass,
                "transaction_id": "tx_pass",
                "transaction_status": "committed",
                "transaction_success": True,
            },
        )

        with patch("cli.console.print") as mock_print:
            exit_code_pass = cli.execute_task(
                task="Fix bug in calculation",
                repo_dir=self.repo_dir,
                test_cmd="python -c \"import sys; sys.exit(0)\"",
                runtime=mock_runtime_pass,
            )
            self.assertEqual(exit_code_pass, 0)

        # B. Failing verification task -> returns 1
        mock_runtime_fail = MagicMock(spec=BrainFrogRuntime)
        test_proc_fail = HardenedProcessResult(args=["pytest"], returncode=1, stderr="failed assertion")
        tx_res_fail = TransactionResult(
            transaction_id="tx_fail",
            status=TransactionStatus.ROLLED_BACK,
            committed=False,
            rolled_back=True,
            operations=tuple(),
        )
        mock_runtime_fail.handle_message.return_value = OutgoingMessage(
            text="Fail",
            success=False,
            status="error",
            metadata={
                "results": [StepResult(PlanStep("1", "bad", []), "escalated", 0, "Failed")],
                "test_result": test_proc_fail,
                "transaction_result": tx_res_fail,
                "transaction_id": "tx_fail",
                "transaction_status": "rolled_back",
                "transaction_success": False,
            },
        )

        exit_code_fail = cli.execute_task(
            task="Fix bug in calculation",
            repo_dir=self.repo_dir,
            test_cmd="python -c \"import sys; sys.exit(1)\"",
            runtime=mock_runtime_fail,
        )
        self.assertEqual(exit_code_fail, 1)

    def test_4_missing_test_command_cannot_produce_verified_true(self):
        """Invariant 4: Missing test command cannot produce verified=True through synthetic evidence."""
        mock_runtime = MagicMock(spec=BrainFrogRuntime)
        tx_res = TransactionResult(
            transaction_id="tx_no_test",
            status=TransactionStatus.COMMITTED,
            committed=True,
            rolled_back=False,
            operations=tuple(),
        )

        # Model claims drafted_pr outcome, but no test_result is present
        mock_runtime.handle_message.return_value = OutgoingMessage(
            text="Applied edits without tests",
            success=True,
            status="completed",
            metadata={
                "results": [StepResult(PlanStep("1", "modify files", ["file.py"]), "drafted_pr", 0, "Done")],
                "test_result": None,
                "transaction_result": tx_res,
                "transaction_id": "tx_no_test",
                "transaction_status": "committed",
                "transaction_success": True,
            },
        )

        req = TaskRequest(
            task_id="task_fail_closed_no_tests",
            user_request="Modify file without running tests",
            workspace=self.repo_dir,
            test_command=None,
        )

        result: TaskResult = execute_autonomous_task(mock_runtime, req)

        self.assertFalse(result.verified)
        self.assertFalse(result.success)
        self.assertFalse(result.tests_passed)
        self.assertEqual(result.verification_details.get("test_suite"), False)

        store = FileProofStore(self.repo_dir)
        proof = store.load(req.task_id)
        self.assertEqual(proof.verdict.status, "FAILED")
        self.assertIn("test_suite", proof.verdict.reason)

    def test_5_model_reported_success_cannot_substitute_for_executed_test_process(self):
        """Invariant 5: Model-reported outcome (drafted_pr, opened_pr, verified) cannot substitute for executed test process."""
        mock_runtime = MagicMock(spec=BrainFrogRuntime)
        tx_res = TransactionResult(
            transaction_id="tx_synth_attempt",
            status=TransactionStatus.COMMITTED,
            committed=True,
            rolled_back=False,
            operations=tuple(),
        )

        # Model explicitly reports 'verified' outcome without executing a test process
        mock_runtime.handle_message.return_value = OutgoingMessage(
            text="Model claims verified outcome",
            success=True,
            status="completed",
            metadata={
                "results": [StepResult(PlanStep("1", "verified change", ["app.py"]), "verified", 0, "Trust me")],
                "test_result": None,
                "transaction_result": tx_res,
                "transaction_id": "tx_synth_attempt",
                "transaction_status": "committed",
                "transaction_success": True,
            },
        )

        req = TaskRequest(
            task_id="task_model_outcome_no_authority",
            user_request="Update app logic",
            workspace=self.repo_dir,
            test_command=None,
        )

        result: TaskResult = execute_autonomous_task(mock_runtime, req)

        # Must fail-closed: model outcome has ZERO verification authority
        self.assertFalse(result.verified)
        self.assertFalse(result.success)
        self.assertFalse(result.tests_passed)
        self.assertIsNotNone(result.proof_artifact)
        assert result.proof_artifact is not None
        self.assertIsNone(result.proof_artifact.verification.process_evidence)

    def test_6_real_test_returning_non_zero_fails_verification_and_rolls_back(self):
        """Invariant 6: Real test process returning non-zero fails verification and follows transaction rollback policy."""
        mock_runtime = MagicMock(spec=BrainFrogRuntime)
        failing_proc = HardenedProcessResult(args=["pytest"], returncode=2, stderr="AssertionError")
        rb_tx_res = TransactionResult(
            transaction_id="tx_failing_suite",
            status=TransactionStatus.ROLLED_BACK,
            committed=False,
            rolled_back=True,
            operations=tuple(),
        )

        mock_runtime.handle_message.return_value = OutgoingMessage(
            text="Tests failed; mutations rolled back",
            success=False,
            status="error",
            error="Test suite failed with exit code 2",
            metadata={
                "results": [StepResult(PlanStep("1", "modify code", ["code.py"]), "escalated", 1, "Failed")],
                "test_result": failing_proc,
                "transaction_result": rb_tx_res,
                "transaction_id": "tx_failing_suite",
                "transaction_status": "rolled_back",
                "transaction_success": False,
            },
        )

        req = TaskRequest(
            task_id="task_test_fail_rollback",
            user_request="Modify code with failing tests",
            workspace=self.repo_dir,
            test_command=["pytest"],
        )

        result: TaskResult = execute_autonomous_task(mock_runtime, req)

        self.assertFalse(result.verified)
        self.assertFalse(result.success)
        self.assertFalse(result.tests_passed)
        self.assertEqual(result.status, "ROLLED_BACK")
        self.assertEqual(result.verification_details.get("test_suite"), False)
        self.assertEqual(result.verification_details.get("transaction_integrity"), False)

        store = FileProofStore(self.repo_dir)
        proof = store.load(req.task_id)
        self.assertEqual(proof.verdict.status, "FAILED")
        self.assertEqual(proof.verification.exit_code, 2)
        self.assertEqual(proof.changes.status, "ROLLED_BACK")

    def test_7_proof_artifacts_distinguish_ran_failed_and_not_run(self):
        """Invariant 7: Proof artifacts accurately distinguish tests that ran, tests that failed, and tests that did not run."""
        store = FileProofStore(self.repo_dir)

        # Case A: Tests ran and passed
        proc_pass = HardenedProcessResult(args=["pytest", "-q"], returncode=0, stdout="Passed")
        req_pass = TaskRequest(task_id="task_test_ran_pass", user_request="Passing test", workspace=self.repo_dir)
        mock_rt_pass = MagicMock(spec=BrainFrogRuntime)
        mock_rt_pass.handle_message.return_value = OutgoingMessage(
            text="Pass", success=True, status="completed",
            metadata={
                "test_result": proc_pass,
                "transaction_id": "tx_p",
                "transaction_status": "committed",
                "transaction_success": True,
                "transaction_result": TransactionResult("tx_p", TransactionStatus.COMMITTED, True, False, tuple()),
            },
        )
        res_pass = execute_autonomous_task(mock_rt_pass, req_pass)
        self.assertTrue(res_pass.verified)
        p_pass = store.load("task_test_ran_pass")
        self.assertEqual(p_pass.verification.exit_code, 0)
        self.assertTrue(p_pass.verification.gate_checks["test_suite"])
        md_pass = store.load_markdown("task_test_ran_pass")
        self.assertIn("- **Exit Code:** `0`", md_pass)

        # Case B: Tests ran and failed
        proc_fail = HardenedProcessResult(args=["pytest", "-q"], returncode=1, stderr="Failed")
        req_fail = TaskRequest(task_id="task_test_ran_fail", user_request="Failing test", workspace=self.repo_dir)
        mock_rt_fail = MagicMock(spec=BrainFrogRuntime)
        mock_rt_fail.handle_message.return_value = OutgoingMessage(
            text="Fail", success=False, status="error",
            metadata={
                "test_result": proc_fail,
                "transaction_id": "tx_f",
                "transaction_status": "rolled_back",
                "transaction_success": False,
                "transaction_result": TransactionResult("tx_f", TransactionStatus.ROLLED_BACK, False, True, tuple()),
            },
        )
        res_fail = execute_autonomous_task(mock_rt_fail, req_fail)
        self.assertFalse(res_fail.verified)
        p_fail = store.load("task_test_ran_fail")
        self.assertEqual(p_fail.verification.exit_code, 1)
        self.assertFalse(p_fail.verification.gate_checks["test_suite"])
        md_fail = store.load_markdown("task_test_ran_fail")
        self.assertIn("- **Exit Code:** `1`", md_fail)

        # Case C: Tests not run
        req_norun = TaskRequest(task_id="task_test_not_run", user_request="No tests run", workspace=self.repo_dir)
        mock_rt_norun = MagicMock(spec=BrainFrogRuntime)
        mock_rt_norun.handle_message.return_value = OutgoingMessage(
            text="No tests", success=True, status="completed",
            metadata={
                "test_result": None,
                "transaction_id": "tx_n",
                "transaction_status": "committed",
                "transaction_success": True,
                "transaction_result": TransactionResult("tx_n", TransactionStatus.COMMITTED, True, False, tuple()),
            },
        )
        res_norun = execute_autonomous_task(mock_rt_norun, req_norun)
        self.assertFalse(res_norun.verified)
        p_norun = store.load("task_test_not_run")
        self.assertIsNone(p_norun.verification.exit_code)
        self.assertFalse(p_norun.verification.gate_checks["test_suite"])
        md_norun = store.load_markdown("task_test_not_run")
        self.assertIn("- **Test Command:** `(none)`", md_norun)
        self.assertIn("- **Exit Code:** `(not run)`", md_norun)

    def test_8_git_initial_and_final_head_values_sampled_at_correct_lifecycle_points(self):
        """Invariant 8: Git initial and final HEAD values are sampled at correct lifecycle points."""
        init_head, was_dirty = sample_git_state(self.repo_dir)
        self.assertIsNotNone(init_head)
        self.assertFalse(was_dirty)

        mock_runtime = MagicMock(spec=BrainFrogRuntime)

        # Simulate task execution creating a commit before task finalization
        def side_effect_handle_message(incoming):
            (self.repo_dir / "file.txt").write_text("mutation", encoding="utf-8")
            subprocess.run(["git", "add", "file.txt"], cwd=self.repo_dir, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "Task commit"], cwd=self.repo_dir, check=True, capture_output=True)
            return OutgoingMessage(
                text="Committed during execution",
                success=True,
                status="completed",
                metadata={
                    "test_result": HardenedProcessResult(args=["pytest"], returncode=0, stdout="pass"),
                    "transaction_id": "tx_git",
                    "transaction_status": "committed",
                    "transaction_success": True,
                    "transaction_result": TransactionResult("tx_git", TransactionStatus.COMMITTED, True, False, tuple()),
                },
            )

        mock_runtime.handle_message.side_effect = side_effect_handle_message

        req = TaskRequest(
            task_id="task_git_lifecycle_head",
            user_request="Commit during task",
            workspace=self.repo_dir,
            test_command=["pytest"],
        )

        result = execute_autonomous_task(mock_runtime, req)
        self.assertTrue(result.verified)

        store = FileProofStore(self.repo_dir)
        proof = store.load(req.task_id)
        prov = proof.provenance
        self.assertIsNotNone(prov)
        assert prov is not None

        # Prove initial and final HEAD are distinct and sampled at correct lifecycle points
        self.assertEqual(prov.initial_head_sha, init_head)
        self.assertNotEqual(prov.initial_head_sha, prov.final_head_sha)

        final_head_now, _ = sample_git_state(self.repo_dir)
        self.assertEqual(prov.final_head_sha, final_head_now)

    def test_9_conversational_cli_behavior_preserved_outside_autonomous_execution(self):
        """Invariant 9: Existing conversational CLI behavior still works for paths intentionally outside autonomous coding."""
        mock_runtime = MagicMock(spec=BrainFrogRuntime)
        mock_runtime.handle_message.return_value = OutgoingMessage(
            text="### Project Plan\n1. Analyze system\n2. Design architecture",
            success=True,
            status="completed",
            metadata={"results": [StepResult(PlanStep("0", "planning", []), "planned", 0, "Plan details")]},
        )

        # Plan mode explicitly outside autonomous coding execution
        exit_code = cli.execute_task(
            task="Create migration plan for database",
            repo_dir=self.repo_dir,
            mode="plan",
            runtime=mock_runtime,
        )

        self.assertEqual(exit_code, 0)
        mock_runtime.handle_message.assert_called_once()

        # No proofs generated for planning mode
        store = FileProofStore(self.repo_dir)
        proof_ids = store.list()
        self.assertEqual(len(proof_ids), 0)

    def test_10_recovery_required_and_cancellation_do_not_appear_verified(self):
        """Invariant 10: Recovery-required and cancellation outcomes do not accidentally appear as ordinary verified success."""
        # A. Recovery-required task
        mock_runtime = MagicMock(spec=BrainFrogRuntime)
        rec_tx_res = TransactionResult(
            transaction_id="tx_crashed",
            status=TransactionStatus.FAILED,
            committed=False,
            rolled_back=False,
            operations=tuple(),
            rollback_error="Incomplete rollback requiring manual recovery",
        )
        mock_runtime.handle_message.return_value = OutgoingMessage(
            text="Crash left transaction inconsistent",
            success=False,
            status="error",
            metadata={
                "test_result": None,
                "transaction_id": "tx_crashed",
                "transaction_status": "failed",
                "transaction_success": False,
                "transaction_result": rec_tx_res,
            },
        )

        req_rec = TaskRequest(
            task_id="task_rec_req",
            user_request="Crashed operation",
            workspace=self.repo_dir,
        )
        res_rec = execute_autonomous_task(mock_runtime, req_rec)
        self.assertFalse(res_rec.verified)
        self.assertFalse(res_rec.success)
        self.assertEqual(res_rec.status, "RECOVERY_REQUIRED")
        self.assertTrue(res_rec.recovery_required)

        # B. Cancellation task
        mock_runtime_cancel = MagicMock(spec=BrainFrogRuntime)
        mock_runtime_cancel.handle_message.side_effect = KeyboardInterrupt("Ctrl+C")

        req_cancel = TaskRequest(
            task_id="task_cancelled",
            user_request="Cancelled by user",
            workspace=self.repo_dir,
        )
        res_cancel = execute_autonomous_task(mock_runtime_cancel, req_cancel)
        self.assertFalse(res_cancel.verified)
        self.assertFalse(res_cancel.success)
        self.assertEqual(res_cancel.status, "CANCELLED")

    def test_11_realistic_e2e_cli_temporary_workspace(self):
        """Realistic end-to-end CLI execution test using temporary workspace and real subprocess tests."""
        # Setup fixture project with real python test
        (self.repo_dir / "calculator.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        test_file = self.repo_dir / "test_calc.py"
        test_file.write_text("from calculator import add\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add calculator"], cwd=self.repo_dir, check=True, capture_output=True)

        # Run real test process directly to confirm environment
        test_cmd_str = f"{sys.executable} -m pytest test_calc.py -q"
        proc = subprocess.run(test_cmd_str.split(), cwd=self.repo_dir, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)

        # Construct realistic runtime
        real_proc = HardenedProcessResult(args=test_cmd_str.split(), returncode=0, stdout=proc.stdout)
        tx_res = TransactionResult(
            transaction_id="tx_e2e_real",
            status=TransactionStatus.COMMITTED,
            committed=True,
            rolled_back=False,
            operations=tuple(),
        )

        mock_runtime = MagicMock(spec=BrainFrogRuntime)
        mock_runtime.handle_message.return_value = OutgoingMessage(
            text="Calculator verified",
            success=True,
            status="completed",
            metadata={
                "results": [StepResult(PlanStep("1", "verify calculator", ["calculator.py"]), "drafted_pr", 0, "OK")],
                "test_result": real_proc,
                "transaction_result": tx_res,
                "transaction_id": "tx_e2e_real",
                "transaction_status": "committed",
                "transaction_success": True,
            },
        )

        exit_code = cli.execute_task(
            task="Implement and verify calculator",
            repo_dir=self.repo_dir,
            mode="build",
            test_cmd=test_cmd_str,
            runtime=mock_runtime,
        )

        self.assertEqual(exit_code, 0)

        store = FileProofStore(self.repo_dir)
        proof_ids = store.list()
        self.assertEqual(len(proof_ids), 1)
        p = store.load(proof_ids[0])
        self.assertEqual(p.verdict.status, "VERIFIED")
        self.assertEqual(p.verification.exit_code, 0)
        self.assertIn("pytest", p.verification.test_command)

    def _create_mock_systems(self, target_file: str, new_content: str):
        mock_s1 = MagicMock()
        mock_s1.name = "jev_mock"
        mock_s1.decide.side_effect = [
            {
                "likely_domain": Answer(choice="core", confidence=0.9),
                "change_type": Answer(choice="code_change", confidence=0.9),
                "is_sensitive": Answer(noul=0.0),
                "complexity": Answer(score="low", confidence=0.9),
                "needs_tests": Answer(noul=0.8),
            },
            {
                "tests_passing": Answer(choice="yes", noul=1.0),
                "diff_complete": Answer(choice="yes", noul=1.0),
                "failure_fixable": Answer(choice="no", noul=0.0),
                "retry_concern": Answer(score="within_normal", confidence=0.95),
            },
            {
                "tests_passing": Answer(choice="yes", noul=1.0),
                "diff_complete": Answer(choice="yes", noul=1.0),
                "failure_fixable": Answer(choice="no", noul=0.0),
                "retry_concern": Answer(score="within_normal", confidence=0.95),
            },
        ]

        mock_s2 = MagicMock()
        mock_s2.provider_name = "claude_mock"
        mock_s2.plan_task.return_value = [
            PlanStep(id="1", description=f"Update {target_file}", files=[target_file])
        ]
        mock_s2.write_code.return_value = {
            target_file: new_content
        }
        mock_s2.review_and_fix.return_value = {
            target_file: new_content
        }
        return mock_s1, mock_s2

    def test_12_missing_test_command_rolls_back_mutations_cli_and_sdk(self):
        """Invariant 12: Missing test command causes transaction rollback and non-zero exit for CLI and SDK."""
        base_file = self.repo_dir / "target.py"
        base_file.write_text("initial = True\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo_dir, check=True)
        subprocess.run(["git", "commit", "-m", "baseline"], cwd=self.repo_dir, check=True)
        head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo_dir, capture_output=True, text=True, check=True).stdout.strip()

        # A. CLI Execution with missing test command
        s1, s2 = self._create_mock_systems("target.py", "initial = False\nmutated = True\n")
        runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            system1_factory=lambda _: s1,
            system2_factory=lambda **_: s2,
            default_test_cmd=None,
        )

        task_res_list = []
        orig_exec = cli.execute_autonomous_task
        def capture_exec(rt, req):
            res = orig_exec(rt, req)
            task_res_list.append(res)
            return res

        with patch("cli.execute_autonomous_task", side_effect=capture_exec):
            exit_code = cli.execute_task(
                task="Mutate target.py",
                repo_dir=self.repo_dir,
                mode="build",
                test_cmd=None,
                runtime=runtime,
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(base_file.read_text(encoding="utf-8"), "initial = True\n")
        head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo_dir, capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(head_before, head_after)

        self.assertEqual(len(task_res_list), 1)
        r = task_res_list[0]
        self.assertFalse(r.verified)
        self.assertFalse(r.success)
        self.assertEqual(r.status, "ROLLED_BACK")
        self.assertEqual(r.transaction_status, "rolled_back")

        store = FileProofStore(self.repo_dir)
        p = store.load(r.task_id)
        self.assertEqual(p.verdict.status, "FAILED")
        self.assertEqual(p.changes.status, "ROLLED_BACK")

        # B. SDK Execution with missing test command
        s1_sdk, s2_sdk = self._create_mock_systems("target.py", "initial = False\nmutated = True\n")
        runtime_sdk = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            system1_factory=lambda _: s1_sdk,
            system2_factory=lambda **_: s2_sdk,
            default_test_cmd=None,
        )
        client = BrainFrogClient(workspace=self.repo_dir, runtime=runtime_sdk, test_command=None)
        res_sdk = client.run_sync(task="SDK mutate target.py", test_command=None)
        client.close_sync()

        self.assertFalse(res_sdk.verified)
        self.assertFalse(res_sdk.success)
        self.assertEqual(res_sdk.status, "ROLLED_BACK")
        self.assertEqual(res_sdk.transaction_status, "rolled_back")
        self.assertEqual(base_file.read_text(encoding="utf-8"), "initial = True\n")

    def test_13_empty_test_command_rolls_back_mutations_cli_and_sdk(self):
        """Invariant 13: Explicit empty test command fails closed and rolls back mutations for CLI and SDK."""
        base_file = self.repo_dir / "app_logic.py"
        base_file.write_text("x = 10\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo_dir, check=True)
        subprocess.run(["git", "commit", "-m", "app logic baseline"], cwd=self.repo_dir, check=True)

        # A. CLI Execution with empty test command
        s1, s2 = self._create_mock_systems("app_logic.py", "x = 99\n")
        runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            system1_factory=lambda _: s1,
            system2_factory=lambda **_: s2,
            default_test_cmd="",
        )

        task_res_list = []
        orig_exec = cli.execute_autonomous_task
        def capture_exec(rt, req):
            res = orig_exec(rt, req)
            task_res_list.append(res)
            return res

        with patch("cli.execute_autonomous_task", side_effect=capture_exec):
            exit_code = cli.execute_task(
                task="Modify app_logic.py",
                repo_dir=self.repo_dir,
                mode="build",
                test_cmd="",
                runtime=runtime,
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(base_file.read_text(encoding="utf-8"), "x = 10\n")

        self.assertEqual(len(task_res_list), 1)
        r = task_res_list[0]
        self.assertFalse(r.verified)
        self.assertEqual(r.status, "ROLLED_BACK")
        self.assertEqual(r.transaction_status, "rolled_back")

        # B. SDK Execution with empty test command list
        s1_sdk, s2_sdk = self._create_mock_systems("app_logic.py", "x = 99\n")
        runtime_sdk = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            system1_factory=lambda _: s1_sdk,
            system2_factory=lambda **_: s2_sdk,
            default_test_cmd="",
        )
        client = BrainFrogClient(workspace=self.repo_dir, runtime=runtime_sdk, test_command=[])
        res_sdk = client.run_sync(task="SDK modify app_logic", test_command=[])
        client.close_sync()

        self.assertFalse(res_sdk.verified)
        self.assertEqual(res_sdk.status, "ROLLED_BACK")
        self.assertEqual(res_sdk.transaction_status, "rolled_back")
        self.assertEqual(base_file.read_text(encoding="utf-8"), "x = 10\n")

    def test_14_proof_persistence_failure_reports_committed_and_requires_recovery(self):
        """Invariant 14: Failure to persist proof after durable commit reports committed state, fails closed, and requires recovery."""
        (self.repo_dir / "mod.py").write_text("v = 1\n", encoding="utf-8")
        (self.repo_dir / "test_mod.py").write_text("def test_ok(): pass\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo_dir, check=True)
        subprocess.run(["git", "commit", "-m", "init mod"], cwd=self.repo_dir, check=True)

        test_cmd = f"{sys.executable} -m pytest test_mod.py"
        s1, s2 = self._create_mock_systems("mod.py", "v = 2\n")
        runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            system1_factory=lambda _: s1,
            system2_factory=lambda **_: s2,
            default_test_cmd=test_cmd,
        )

        task_res_list = []
        orig_exec = cli.execute_autonomous_task
        def capture_exec(rt, req):
            with patch.object(FileProofStore, "save", side_effect=IOError("Durable proof persistence error")):
                res = orig_exec(rt, req)
                task_res_list.append(res)
                return res

        with patch("cli.execute_autonomous_task", side_effect=capture_exec):
            exit_code = cli.execute_task(
                task="Update mod.py",
                repo_dir=self.repo_dir,
                mode="build",
                test_cmd=test_cmd,
                runtime=runtime,
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(len(task_res_list), 1)
        r = task_res_list[0]

        # Invariant: Must not claim verified, must record committed state, must require recovery
        self.assertFalse(r.verified)
        self.assertFalse(r.success)
        self.assertEqual(r.status, "FAILED")
        self.assertTrue(r.recovery_required)
        self.assertEqual(r.transaction_status, "committed")
        self.assertIn("mod.py", r.changed_files)
        self.assertNotIn("rolled back", (r.failure_reason or "").lower())

        # Invariant: Durable commit intent is preserved
        finalizer = TaskFinalizationCoordinator(self.repo_dir)
        intent = finalizer.get_intent(r.task_id)
        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(intent.status.value, "COMMITTED_PENDING_PROOF")

    def test_15_distinguish_transaction_outcome_from_task_verdict(self):
        """Invariant 15: TaskResult unambiguously distinguishes transaction outcome from task verification verdict."""
        # 1. Rolled back task has transaction_status=rolled_back and status=ROLLED_BACK
        rb_result = TaskResult(
            task_id="task_rb_test",
            user_request="Test task",
            success=False,
            verified=False,
            status="ROLLED_BACK",
            summary="Verification failed",
            transaction_status="rolled_back",
        )
        self.assertEqual(rb_result.status, "ROLLED_BACK")
        self.assertEqual(rb_result.transaction_status, "rolled_back")
        self.assertFalse(rb_result.verified)

        # 2. Committed unverified task has transaction_status=committed, but status=FAILED (never COMMITTED)
        failed_committed_result = TaskResult(
            task_id="task_fc_test",
            user_request="Test task",
            success=False,
            verified=False,
            status="FAILED",
            summary="Proof persistence failed",
            transaction_status="committed",
            recovery_required=True,
        )
        self.assertEqual(failed_committed_result.status, "FAILED")
        self.assertEqual(failed_committed_result.transaction_status, "committed")
        self.assertFalse(failed_committed_result.verified)
        self.assertTrue(failed_committed_result.recovery_required)

        # 3. Client get_task() on an unverified artifact never returns status=COMMITTED
        mock_proof = MagicMock()
        mock_proof.task.task_id = "task_probe_artifact"
        mock_proof.task.description = "Test probe"
        mock_proof.verdict.status = "FAILED"
        mock_proof.verdict.reason = "Test suite failed"
        mock_proof.changes.status = "COMMITTED"
        mock_proof.changes.transaction_id = "tx_probe"
        mock_proof.changes.files = []
        mock_proof.verification.exit_code = 1
        mock_proof.verification.stdout_summary = ""

        client = BrainFrogClient(workspace=self.repo_dir)
        with patch.object(client, "get_proof", return_value=mock_proof):
            reconstituted = client.get_task("task_probe_artifact")
            self.assertIsNotNone(reconstituted)
            assert reconstituted is not None
            self.assertFalse(reconstituted.verified)
            self.assertFalse(reconstituted.success)
            # Must NOT claim COMMITTED task status when verdict is FAILED
            self.assertEqual(reconstituted.status, "FAILED")
            self.assertEqual(reconstituted.transaction_status, "committed")
        client.close_sync()


if __name__ == "__main__":
    unittest.main()
