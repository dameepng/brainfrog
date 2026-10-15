"""Focused Regression Tests for Verification Command Resolution UX.

Covers:
1. A detected command requiring the existing confirmation behavior.
2. An undetected command with a user-supplied command.
3. User cancellation.
4. Non-interactive execution with no detectable command.
5. A failed command.
6. A timed-out command.
7. A command that exits successfully but fails another verification invariant.
8. Proof correctly representing verified versus unverified outcomes.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

from rich.console import Console

import cli
from cli import (
    detect_default_test_cmd,
    execute_task,
    resolve_verification_command,
    review_and_confirm_plan,
)
from core.plans import PlanDocument
from core.runtime import (
    BrainFrogRuntime,
    ChangeRecord,
    ChangesEvidence,
    FileProofStore,
    HardenedProcessResult,
    ProcessEvidence,
    ProofArtifact,
    ProofVerdict,
    ReproducibilityEvidence,
    TaskEvidence,
    TaskRequest,
    TaskResult,
    VerificationEvidence,
    VerificationGate,
    compute_proof_verdict,
    execute_autonomous_task,
)
from core.runtime.transaction import (
    TransactionResult,
    TransactionStatus,
)


class TestVerificationCommandResolutionUX(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="test_vcmd_res_")
        self.repo_dir = Path(self.tmp_dir).resolve()

        # Initialize clean git repo
        subprocess.run(["git", "init"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "tester@test.com"], cwd=self.repo_dir, capture_output=True, check=True)
        (self.repo_dir / "README.md").write_text("# Test Repo\n", encoding="utf-8")
        (self.repo_dir / ".gitignore").write_text(".brainfrog/\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md", ".gitignore"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=self.repo_dir, capture_output=True, check=True)
        res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo_dir, capture_output=True, text=True, check=True)
        self.head_sha = res.stdout.strip()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # Test 1: Detected command requiring existing confirmation behavior
    # -------------------------------------------------------------------------
    def test_1_detected_command_requiring_existing_confirmation(self):
        """When a domain test command is auto-detected, resolve_verification_command uses it
        without asking the user to invent a command, and plan review confirms it."""
        # Create package.json indicating npm test
        (self.repo_dir / "package.json").write_text('{"name": "test-pkg", "scripts": {"test": "mocha"}}\n', encoding="utf-8")

        detected = detect_default_test_cmd(self.repo_dir)
        self.assertEqual(detected, "npm test")

        buf = io.StringIO()
        console = Console(file=buf, force_terminal=True, legacy_windows=False)

        # resolve_verification_command resolves immediately without invoking input_fn
        input_mock = MagicMock(return_value="should_not_be_called")
        cmd, cancelled = resolve_verification_command(
            self.repo_dir,
            configured_cmd=None,
            interactive=True,
            input_fn=input_mock,
            console=console,
        )
        self.assertEqual(cmd, "npm test")
        self.assertFalse(cancelled)
        input_mock.assert_not_called()

        # Existing confirmation in review_and_confirm_plan
        plan = PlanDocument(
            filename="plan_001.md",
            path=self.repo_dir / "plan_001.md",
            title="Add feature",
            goal="Add feature",
            relevant_files=["index.js"],
            verification_commands=["npm test"],
            commit_hash=self.head_sha,
        )
        confirm_buf = io.StringIO()
        confirm_console = Console(file=confirm_buf, force_terminal=True, legacy_windows=False)
        choice = review_and_confirm_plan(
            plan,
            self.repo_dir,
            active_test_cmd=cmd,
            console=confirm_console,
            input_fn=lambda _: "e",
            interactive=True,
        )
        self.assertEqual(choice, "execute")
        self.assertIn("npm test", confirm_buf.getvalue())

    # -------------------------------------------------------------------------
    # Test 2: Undetected command with user-supplied command
    # -------------------------------------------------------------------------
    def test_2_undetected_command_with_user_supplied_command(self):
        """When no command is detected, prompts user, displays the chosen command before execution,
        and executes with the supplied command."""
        # No test runner files present
        self.assertEqual(detect_default_test_cmd(self.repo_dir), "")

        buf = io.StringIO()
        console = Console(file=buf, force_terminal=True, legacy_windows=False)

        user_input = "python -m unittest tests/test_math.py"
        cmd, cancelled = resolve_verification_command(
            self.repo_dir,
            configured_cmd=None,
            task_description="Implement math module",
            interactive=True,
            input_fn=lambda _: user_input,
            console=console,
        )

        self.assertEqual(cmd, user_input)
        self.assertFalse(cancelled)

        output = buf.getvalue()
        # Informs user that BrainFrog could not identify a verification command
        self.assertIn("could not automatically identify a verification command", output)
        # Shows selected command before execution with hardened boundary info
        self.assertIn("Perintah verifikasi yang dipilih", output)
        self.assertIn(user_input, output)
        self.assertIn("Hardened Boundary", output)
        self.assertIn(str(self.repo_dir), output)

    # -------------------------------------------------------------------------
    # Test 3: User cancellation
    # -------------------------------------------------------------------------
    def test_3_user_cancellation(self):
        """When prompted for a test command, user types 'cancel' or 'c'; execution is cleanly aborted
        with exit code 130 and no files are modified."""
        self.assertEqual(detect_default_test_cmd(self.repo_dir), "")

        buf = io.StringIO()
        console = Console(file=buf, force_terminal=False, no_color=True, legacy_windows=False)

        cmd, cancelled = resolve_verification_command(
            self.repo_dir,
            interactive=True,
            input_fn=lambda _: "cancel",
            console=console,
        )
        self.assertIsNone(cmd)
        self.assertTrue(cancelled)
        self.assertIn("Eksekusi tugas dibatalkan oleh pengguna", buf.getvalue())

        # In execute_task, user cancellation returns exit code 130 immediately
        clean_before = list(self.repo_dir.rglob("*"))
        exit_code = execute_task(
            task="Add calculator feature",
            repo_dir=self.repo_dir,
            mode="build",
            is_interactive=True,
            verification_input_fn=lambda _: "c",
        )
        self.assertEqual(exit_code, 130)
        clean_after = list(self.repo_dir.rglob("*"))
        self.assertEqual(clean_before, clean_after)

    # -------------------------------------------------------------------------
    # Test 4: Non-interactive execution with no detectable command
    # -------------------------------------------------------------------------
    def test_4_non_interactive_execution_no_detectable_command(self):
        """In non-interactive execution with no detectable test command, BrainFrog never blocks,
        never invents a command, and preserves fail-closed unverified status (COMMITTED_UNTESTED)."""
        self.assertEqual(detect_default_test_cmd(self.repo_dir), "")

        buf = io.StringIO()
        console = Console(file=buf, force_terminal=True, legacy_windows=False)

        # resolve_verification_command does not block or invent a command
        cmd, cancelled = resolve_verification_command(
            self.repo_dir,
            interactive=False,
            input_fn=None,
            console=console,
        )
        self.assertIsNone(cmd)
        self.assertFalse(cancelled)
        output = buf.getvalue()
        self.assertIn("Verification Command Undetected", output)
        self.assertIn("COMMITTED_UNTESTED", output)
        self.assertIn("will NOT invent one", output)

        # Non-interactive execution in execute_task
        mock_runtime = MagicMock(spec=BrainFrogRuntime)
        untested_task_res = TaskResult(
            task_id="task_non_interactive_001",
            user_request="Build without test suite",
            success=False,
            verified=False,
            status="COMMITTED_UNTESTED",
            summary="Applied to disk untested",
            proof_artifact=None,
        )

        with patch("cli.execute_autonomous_task", return_value=untested_task_res):
            with patch("cli.print_task_verification_report"):
                exit_code = execute_task(
                    task="Build without test suite",
                    repo_dir=self.repo_dir,
                    runtime=mock_runtime,
                    mode="build",
                    is_interactive=False,
                    verification_input_fn=None,
                )

        # Task fails closed with exit code 1 because verified=False
        self.assertEqual(exit_code, 1)

    # -------------------------------------------------------------------------
    # Test 5: A failed command
    # -------------------------------------------------------------------------
    def test_5_failed_command(self):
        """A user-supplied command that exits non-zero causes verification gate failure,
        transaction rollback, and proof verdict FAILED."""
        failed_proc = HardenedProcessResult(
            args=["pytest", "tests/test_fail.py"],
            returncode=2,
            stdout="Running test suite...\nAssertionError: expected 4 but got 5",
            stderr="Traceback (most recent call last):\n...",
            is_timeout=False,
            is_cancelled=False,
        )

        tx_rb = TransactionResult(
            transaction_id="tx_test_5",
            status=TransactionStatus.ROLLED_BACK,
            committed=False,
            rolled_back=True,
            operations=tuple(),
        )

        # Gate verification
        gate_passed, v_details, errors = VerificationGate.verify(
            workspace=self.repo_dir,
            expected_files=["calc.py"],
            actual_files=["calc.py"],
            test_proc=failed_proc,
            tx_res=tx_rb,
        )
        self.assertFalse(gate_passed)
        self.assertFalse(v_details["test_suite"])
        self.assertFalse(v_details["transaction_integrity"])
        self.assertTrue(any("failed with exit code 2" in e for e in errors))

        # Proof verdict
        verdict = compute_proof_verdict(
            tx_status="rolled_back",
            tests_passed=False,
            gate_passed=gate_passed,
            process_status="success",
            gate_details=v_details,
        )
        self.assertEqual(verdict.status, "FAILED")

    # -------------------------------------------------------------------------
    # Test 6: A timed-out command
    # -------------------------------------------------------------------------
    def test_6_timed_out_command(self):
        """A test command that exceeds its execution budget is killed, gate evaluates timeout,
        and proof verdict records FAILED with timeout explanation."""
        timeout_proc = HardenedProcessResult(
            args=["pytest", "tests/test_infinite.py"],
            returncode=-15,
            stdout="Running slow stress test suite...",
            stderr="Execution timed out after 30.00s",
            is_timeout=True,
            is_cancelled=False,
        )

        tx_rb = TransactionResult(
            transaction_id="tx_test_6",
            status=TransactionStatus.ROLLED_BACK,
            committed=False,
            rolled_back=True,
            operations=tuple(),
        )

        gate_passed, v_details, errors = VerificationGate.verify(
            workspace=self.repo_dir,
            expected_files=["calc.py"],
            actual_files=["calc.py"],
            test_proc=timeout_proc,
            tx_res=tx_rb,
        )
        self.assertFalse(gate_passed)
        self.assertFalse(v_details["test_suite"])
        self.assertTrue(any("timed out" in e.lower() for e in errors))

        verdict = compute_proof_verdict(
            tx_status="rolled_back",
            tests_passed=False,
            gate_passed=False,
            process_status="timeout",
            gate_details=v_details,
        )
        self.assertEqual(verdict.status, "FAILED")
        self.assertIn("timed out", verdict.reason.lower())

    # -------------------------------------------------------------------------
    # Test 7: Command exits successfully but fails another verification invariant
    # -------------------------------------------------------------------------
    def test_7_command_exits_successfully_but_fails_another_verification_invariant(self):
        """A test command that exits 0 alone DOES NOT count as proof if another verification
        invariant (such as workspace mutation boundary) is violated."""
        pass_proc = HardenedProcessResult(
            args=["pytest", "-q"],
            returncode=0,
            stdout="1 passed in 0.02s",
            stderr="",
            is_timeout=False,
            is_cancelled=False,
        )

        tx_committed = TransactionResult(
            transaction_id="tx_test_7",
            status=TransactionStatus.COMMITTED,
            committed=True,
            rolled_back=False,
            operations=tuple(),
        )

        # Expected files was ['calc.py'], but actual touched files includes unauthorized 'secrets.env'
        gate_passed, v_details, errors = VerificationGate.verify(
            workspace=self.repo_dir,
            expected_files=["calc.py"],
            actual_files=["calc.py", "secrets.env"],
            test_proc=pass_proc,
            tx_res=tx_committed,
        )

        # Gate must fail due to scope violation even though test_proc exit code was 0!
        self.assertFalse(gate_passed)
        self.assertTrue(v_details["test_suite"])
        self.assertFalse(v_details["workspace_scope"])
        self.assertTrue(any("secrets.env" in e for e in errors))

        verdict = compute_proof_verdict(
            tx_status="committed",
            tests_passed=True,
            gate_passed=gate_passed,
            process_status="success",
            gate_details=v_details,
        )
        self.assertEqual(verdict.status, "FAILED")
        self.assertIn("workspace_scope", verdict.reason)

    # -------------------------------------------------------------------------
    # Test 8: Proof correctly representing verified versus unverified outcomes
    # -------------------------------------------------------------------------
    def test_8_proof_correctly_representing_verified_vs_unverified(self):
        """Verifies that proof artifacts accurately reflect verified versus unverified outcomes,
        recording command, cwd, exit code, timeout state, and gate checks without model tampering."""
        store = FileProofStore(self.repo_dir)

        # Case A: Verified Outcome
        proc_ev_verified = ProcessEvidence(
            task_id="task_verified_8a",
            transaction_id="tx_8a",
            command="pytest tests/test_feature.py",
            cwd=str(self.repo_dir),
            started_at="2026-10-15T12:00:00Z",
            completed_at="2026-10-15T12:00:02Z",
            duration_ms=2000,
            returncode=0,
            status="success",
            timed_out=False,
            cancelled=False,
        )
        proof_verified = ProofArtifact(
            task=TaskEvidence("task_verified_8a", "sess_8", "Add feature", "2026-10-15T12:00:00Z", "2026-10-15T12:00:02Z", 2000),
            verdict=ProofVerdict("VERIFIED", "All verification gate invariants satisfied and transaction committed cleanly."),
            verification=VerificationEvidence(
                test_command="pytest tests/test_feature.py",
                exit_code=0,
                stdout_summary="1 passed in 0.05s",
                stderr_summary="",
                gate_checks={"test_suite": True, "workspace_scope": True, "transaction_integrity": True},
                process_evidence=proc_ev_verified,
            ),
            changes=ChangesEvidence(transaction_id="tx_8a", status="COMMITTED", files=[
                ChangeRecord(path="feature.py", operation="CREATE", before_sha256=None, after_sha256="hash1", byte_count_delta=100)
            ]),
            reproducibility=ReproducibilityEvidence(command="pytest tests/test_feature.py"),
        )
        p_json, p_md = store.save(proof_verified)
        self.assertTrue(p_json.exists())

        loaded_verified = store.load("task_verified_8a")
        self.assertEqual(loaded_verified.verdict.status, "VERIFIED")
        self.assertEqual(loaded_verified.verification.exit_code, 0)
        self.assertEqual(loaded_verified.verification.process_evidence.cwd, str(self.repo_dir))
        self.assertFalse(loaded_verified.verification.process_evidence.timed_out)

        # Case B: Unverified Outcome (declined / skipped verification)
        proof_unverified = ProofArtifact(
            task=TaskEvidence("task_unverified_8b", "sess_8", "Add untested script", "2026-10-15T12:05:00Z", "2026-10-15T12:05:01Z", 1000),
            verdict=ProofVerdict("FAILED", "Verification gate check(s) failed: test_suite, transaction_integrity."),
            verification=VerificationEvidence(
                test_command="",
                exit_code=None,
                stdout_summary="",
                stderr_summary="",
                gate_checks={"test_suite": False, "workspace_scope": True, "transaction_integrity": False},
                process_evidence=None,
            ),
            changes=ChangesEvidence(transaction_id="tx_8b", status="COMMITTED_UNTESTED", files=[
                ChangeRecord(path="script.py", operation="CREATE", before_sha256=None, after_sha256="hash2", byte_count_delta=50)
            ]),
            reproducibility=ReproducibilityEvidence(command=""),
        )
        p_json_unv, p_md_unv = store.save(proof_unverified)
        self.assertTrue(p_json_unv.exists())

        loaded_unverified = store.load("task_unverified_8b")
        self.assertEqual(loaded_unverified.verdict.status, "FAILED")
        self.assertIsNone(loaded_unverified.verification.exit_code)
        self.assertFalse(loaded_unverified.verification.gate_checks["test_suite"])
        self.assertEqual(loaded_unverified.changes.status, "COMMITTED_UNTESTED")


if __name__ == "__main__":
    unittest.main()
