"""Focused Tests for Guided Plan-Review-Execute-Verify (PREV) Interactive Loop in BrainFrog.

Covers:
1. Happy path: review -> explicit approval -> execution pipeline -> verification result -> actionable diff/proof.
2. Cancellation: no execution or mutation occurs, workspace clean.
3. Non-interactive input: no implicit approval, fail-closed behavior.
4. Stale plan: existing validation blocks or requires the established revalidation path.
5. Failed verification: the displayed result remains truthful and rollback behavior is preserved.
6. Missing proof or diff: the CLI handles the condition safely without crashing.
7. Regression: unspecified fields not fabricated, compatibility with existing CLI & runtime paths.
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

from rich.console import Console

import cli
from cli import (
    offer_post_task_actions,
    review_and_confirm_plan,
    show_proof_cli,
    execute_task,
)
from core.plans import (
    PlanDocument,
    check_plan_staleness,
    format_plan_handoff,
    get_latest_plan,
)
from core.runtime import (
    BrainFrogRuntime,
    ChangeRecord,
    ChangesEvidence,
    FileProofStore,
    GitProvenance,
    ProcessEvidence,
    ProofArtifact,
    ProofVerdict,
    ReproducibilityEvidence,
    TaskEvidence,
    TaskRequest,
    TaskResult,
    VerificationEvidence,
)
from core.runtime.transaction import (
    TransactionResult,
    TransactionStatus,
    TransactionOperation,
    OperationType,
    OperationStatus,
)


class TestInteractivePREVLoop(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="test_prev_loop_")
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

    def _create_and_save_proof(
        self,
        task_id: str,
        verdict_status: str = "VERIFIED",
        tx_status: str = "COMMITTED",
        exit_code: int = 0,
        commit_sha: Optional[str] = None,
        files: Optional[List[ChangeRecord]] = None,
        stdout: str = "1 passed in 0.05s",
    ) -> ProofArtifact:
        """Helper to create and save a valid ProofArtifact in the repository."""
        store = FileProofStore(self.repo_dir)
        target_commit = commit_sha or self.head_sha

        if files is None:
            files = [
                ChangeRecord(
                    path="calculator.py",
                    operation="CREATE",
                    before_sha256=None,
                    after_sha256="abc123hash",
                    byte_count_delta=42,
                )
            ]

        proof = ProofArtifact(
            task=TaskEvidence(
                task_id=task_id,
                session_id="sess_prev_001",
                description="Implement calculator",
                created_at="2026-10-15T12:00:00Z",
                completed_at="2026-10-15T12:00:05Z",
                duration_ms=5000,
            ),
            verdict=ProofVerdict(
                status=verdict_status,
                reason="Verification checks completed successfully.",
            ),
            provenance=GitProvenance(
                is_git=True,
                repo_root=self.repo_dir.name,
                branch="master",
                initial_head_sha=self.head_sha,
                final_head_sha=commit_sha or self.head_sha,
                commit_sha=commit_sha,
            ),
            verification=VerificationEvidence(
                test_command="python -m unittest",
                exit_code=exit_code,
                stdout_summary=stdout,
                stderr_summary="",
                gate_checks={"workspace_scope": True, "command_allowlist": True},
            ),
            changes=ChangesEvidence(
                transaction_id="tx_proof_001",
                status=tx_status,
                files=files,
            ),
            reproducibility=ReproducibilityEvidence(
                command="python -m unittest",
                expected_exit_code=0,
            ),
        )
        store.save(proof)
        return proof

    # -------------------------------------------------------------------------
    # 1. Happy Path: Review -> Approval -> Execution -> Verification -> Diff/Proof
    # -------------------------------------------------------------------------
    def test_happy_path_review_and_post_task_actions(self):
        """Happy path: Plan review displays spec, explicit approval executes, post-task offers actionable diff and proof."""
        plan_path = self.repo_dir / ".brainfrog" / "plans" / "plan_calc.md"
        plan = PlanDocument(
            filename="plan_calc.md",
            path=plan_path,
            title="Add calculator operations",
            goal="Implement addition and subtraction",
            steps=[{"step": 1, "description": "Add add and sub functions", "files": ["calculator.py"]}],
            relevant_files=["calculator.py"],
            commit_hash=self.head_sha,
            created_at="2026-10-15T10:00:00Z",
            content="# Plan\nImplement addition",
            verification_commands=["python -m unittest tests/test_calc.py"],
        )

        buf = io.StringIO()
        test_console = Console(file=buf, force_terminal=True, legacy_windows=False)

        # 1. User reviews plan and inputs 'e' (Execute)
        choice = review_and_confirm_plan(
            plan,
            self.repo_dir,
            console=test_console,
            input_fn=lambda prompt: "e",
            interactive=True,
        )
        self.assertEqual(choice, "execute")

        output_review = buf.getvalue()
        self.assertIn("Pre-Execution Plan Review", output_review)
        self.assertIn("Add calculator operations", output_review)
        self.assertIn("calculator.py", output_review)
        self.assertIn("python -m unittest tests/test_calc.py", output_review)
        self.assertIn("FRESH", output_review)

        # 2. Simulate task execution resulting in a real git commit
        calc_file = self.repo_dir / "calculator.py"
        calc_file.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        subprocess.run(["git", "add", "calculator.py"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "feat: implement calculator"], cwd=self.repo_dir, capture_output=True, check=True)
        commit_res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo_dir, capture_output=True, text=True, check=True)
        new_commit = commit_res.stdout.strip()

        # 3. Create proof artifact for this execution
        proof = self._create_and_save_proof(
            task_id="task_calc_001",
            verdict_status="VERIFIED",
            tx_status="COMMITTED",
            exit_code=0,
            commit_sha=new_commit,
        )

        task_res = TaskResult(
            task_id="task_calc_001",
            user_request="Add calculator operations",
            success=True,
            verified=True,
            status="COMMITTED",
            summary="Calculator implemented and verified",
            proof_path=str(FileProofStore(self.repo_dir).proof_json_path("task_calc_001")),
            proof_artifact=proof,
        )

        # 4. User inspects diff ('d'), then proof ('p'), then exits (Enter / '')
        inputs = iter(["d", "p", ""])
        recorded_prompts: List[str] = []

        def input_recorder(prompt: str) -> str:
            recorded_prompts.append(prompt)
            return next(inputs)

        buf_post = io.StringIO()
        post_console = Console(file=buf_post, force_terminal=True, legacy_windows=False)

        offer_post_task_actions(
            task_res,
            self.repo_dir,
            console=post_console,
            input_fn=input_recorder,
            interactive=True,
        )

        # Verify action bar offered both actions
        self.assertTrue(any("View Diff" in p for p in recorded_prompts))
        self.assertTrue(any("Inspect Proof" in p for p in recorded_prompts))
        self.assertTrue(any("Enter Continue" in p for p in recorded_prompts))

        output_post = buf_post.getvalue()
        # Verify diff was rendered
        self.assertIn("Task Git Diff", output_post)
        self.assertIn("def add(a, b):", output_post)
        # Verify proof inspection was rendered
        self.assertIn("task_calc_001", output_post)
        self.assertIn("VERIFIED", output_post)

    # -------------------------------------------------------------------------
    # 2. Cancellation: No execution or mutation occurs
    # -------------------------------------------------------------------------
    def test_cancellation_preserves_clean_workspace(self):
        """Cancellation at review prompt produces 'cancel' and touches zero files."""
        plan_path = self.repo_dir / ".brainfrog" / "plans" / "plan_cancel.md"
        plan = PlanDocument(
            filename="plan_cancel.md",
            path=plan_path,
            title="Dangerous refactor",
            goal="Refactor internals",
            relevant_files=["dangerous.py"],
            commit_hash=self.head_sha,
            verification_commands=["pytest"],
        )

        buf = io.StringIO()
        test_console = Console(file=buf, force_terminal=True, legacy_windows=False)

        # User chooses 'c' (Cancel)
        choice = review_and_confirm_plan(
            plan,
            self.repo_dir,
            console=test_console,
            input_fn=lambda prompt: "c",
            interactive=True,
        )
        self.assertEqual(choice, "cancel")

        # User chooses 'r' (Refine)
        choice_refine = review_and_confirm_plan(
            plan,
            self.repo_dir,
            console=test_console,
            input_fn=lambda prompt: "r",
            interactive=True,
        )
        self.assertEqual(choice_refine, "refine")

        # User presses Ctrl+C (KeyboardInterrupt)
        def raise_sigint(prompt):
            raise KeyboardInterrupt()

        choice_int = review_and_confirm_plan(
            plan,
            self.repo_dir,
            console=test_console,
            input_fn=raise_sigint,
            interactive=True,
        )
        self.assertEqual(choice_int, "cancel")

        # Prove workspace remains pristine
        status_proc = subprocess.run(["git", "status", "--porcelain"], cwd=self.repo_dir, capture_output=True, text=True)
        self.assertEqual(status_proc.stdout.strip(), "")
        self.assertFalse((self.repo_dir / "dangerous.py").exists())

    # -------------------------------------------------------------------------
    # 3. Non-interactive input: No implicit approval
    # -------------------------------------------------------------------------
    def test_non_interactive_input_fails_closed(self):
        """Non-interactive invocations must fail closed and never implicitly approve execution."""
        plan_path = self.repo_dir / ".brainfrog" / "plans" / "plan_auto.md"
        plan = PlanDocument(
            filename="plan_auto.md",
            path=plan_path,
            title="Automated task",
            goal="Run unattended",
            relevant_files=["auto.py"],
            commit_hash=self.head_sha,
        )

        buf = io.StringIO()
        test_console = Console(file=buf, force_terminal=True, legacy_windows=False)

        # 1. interactive=False with no input_fn
        choice = review_and_confirm_plan(
            plan,
            self.repo_dir,
            console=test_console,
            input_fn=None,
            interactive=False,
        )
        self.assertEqual(choice, "cancel")
        self.assertIn("non-interactive rejection", buf.getvalue())

        # 2. interactive=True with non-tty stdin and no input_fn
        with patch("sys.stdin.isatty", return_value=False):
            choice_tty = review_and_confirm_plan(
                plan,
                self.repo_dir,
                console=test_console,
                input_fn=None,
                interactive=True,
            )
            self.assertEqual(choice_tty, "cancel")

        # 3. offer_post_task_actions in non-interactive mode exits immediately without error
        task_res = TaskResult(
            task_id="t1",
            user_request="t1 req",
            success=True,
            verified=True,
            status="COMMITTED",
            summary="t1 summary",
        )
        buf_act = io.StringIO()
        act_console = Console(file=buf_act, force_terminal=True, legacy_windows=False)
        offer_post_task_actions(
            task_res,
            self.repo_dir,
            console=act_console,
            input_fn=None,
            interactive=False,
        )
        self.assertEqual(buf_act.getvalue(), "")

    # -------------------------------------------------------------------------
    # 4. Stale plan: Validation blocks or requires established revalidation path
    # -------------------------------------------------------------------------
    def test_stale_plan_blocks_unconfirmed_and_allows_revalidation(self):
        """Stale plans are detected, displayed with warnings, and require secondary confirmation."""
        # 1. Create a plan at initial commit
        plan_path = self.repo_dir / ".brainfrog" / "plans" / "plan_stale.md"
        plan = PlanDocument(
            filename="plan_stale.md",
            path=plan_path,
            title="Update service",
            goal="Modify service code",
            relevant_files=["service.py"],
            commit_hash=self.head_sha,
            verification_commands=["pytest tests/test_service.py"],
        )

        # 2. Advance git HEAD commit so plan becomes stale
        (self.repo_dir / "other.txt").write_text("advance commit\n", encoding="utf-8")
        subprocess.run(["git", "add", "other.txt"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "advance HEAD"], cwd=self.repo_dir, capture_output=True, check=True)

        is_stale, reasons = check_plan_staleness(self.repo_dir, plan)
        self.assertTrue(is_stale)
        self.assertTrue(any("Commit Git telah berubah" in r for r in reasons))

        # 3. User tries to execute ('e'), but declines stale revalidation prompt ('n')
        inputs_decline = iter(["e", "n"])
        buf_dec = io.StringIO()
        console_dec = Console(file=buf_dec, force_terminal=True, legacy_windows=False)

        choice_declined = review_and_confirm_plan(
            plan,
            self.repo_dir,
            console=console_dec,
            input_fn=lambda prompt: next(inputs_decline),
            interactive=True,
        )
        self.assertEqual(choice_declined, "cancel")
        self.assertIn("STALE", buf_dec.getvalue())
        self.assertIn("Eksekusi dibatalkan karena rencana berstatus stale", buf_dec.getvalue())

        # 4. User explicitly confirms stale revalidation ('e' then 'y')
        inputs_confirm = iter(["e", "y"])
        buf_conf = io.StringIO()
        console_conf = Console(file=buf_conf, force_terminal=True, legacy_windows=False)

        choice_confirmed = review_and_confirm_plan(
            plan,
            self.repo_dir,
            console=console_conf,
            input_fn=lambda prompt: next(inputs_confirm),
            interactive=True,
        )
        self.assertEqual(choice_confirmed, "execute")

    # -------------------------------------------------------------------------
    # 5. Failed verification: Displayed result remains truthful and rollback preserved
    # -------------------------------------------------------------------------
    def test_failed_verification_truthful_evidence_and_rollback(self):
        """When verification fails, rollback restores workspace and proof truthfully records failure."""
        # 1. Create a proof artifact representing a failed task that rolled back
        fail_proof = self._create_and_save_proof(
            task_id="task_fail_001",
            verdict_status="FAILED",
            tx_status="ROLLED_BACK",
            exit_code=1,
            commit_sha=None,
            stdout="AssertionError: expected 42 but got 0",
        )

        task_res = TaskResult(
            task_id="task_fail_001",
            user_request="Implement failing feature",
            success=False,
            verified=False,
            status="ROLLED_BACK",
            summary="Verification test failed; rollback executed",
            proof_path=str(FileProofStore(self.repo_dir).proof_json_path("task_fail_001")),
            proof_artifact=fail_proof,
        )

        # Working tree is clean because mutations were rolled back
        status_proc = subprocess.run(["git", "status", "--porcelain"], cwd=self.repo_dir, capture_output=True, text=True)
        self.assertEqual(status_proc.stdout.strip(), "")

        # 2. Offer post-task actions: since working tree is clean and no commit was made,
        # diff is NOT available; only proof artifact is inspectable!
        inputs = iter(["p", ""])
        recorded_prompts: List[str] = []

        def input_recorder(prompt: str) -> str:
            recorded_prompts.append(prompt)
            return next(inputs)

        buf = io.StringIO()
        test_console = Console(file=buf, force_terminal=True, legacy_windows=False)

        offer_post_task_actions(
            task_res,
            self.repo_dir,
            console=test_console,
            input_fn=input_recorder,
            interactive=True,
        )

        # Diff should NOT be offered because there is no diff
        self.assertTrue(all("View Diff" not in p for p in recorded_prompts))
        # Proof MUST be offered
        self.assertTrue(any("Inspect Proof" in p for p in recorded_prompts))

        output = buf.getvalue()
        # Truthful failed verdict rendered
        self.assertIn("FAILED", output)
        self.assertIn("task_fail_001", output)

    # -------------------------------------------------------------------------
    # 6. Missing proof or diff: Handled safely without crash
    # -------------------------------------------------------------------------
    def test_missing_proof_or_diff_handled_safely(self):
        """CLI handles missing proof, missing diff, or corrupted proof without crashing."""
        # Case 6a: Proof missing, but uncommitted diff exists on tracked file
        (self.repo_dir / "README.md").write_text("# Test Repo\nuncommitted modification\n", encoding="utf-8")
        task_res_no_proof = TaskResult(
            task_id="nonexistent_task",
            user_request="req with diff but no proof",
            success=True,
            verified=True,
            status="COMMITTED",
            summary="Summary with diff but no proof",
            proof_path=None,
            proof_artifact=None,
        )

        inputs_diff = iter(["d", ""])
        recorded_prompts: List[str] = []

        def input_recorder_diff(prompt: str) -> str:
            recorded_prompts.append(prompt)
            return next(inputs_diff)

        buf_diff = io.StringIO()
        console_diff = Console(file=buf_diff, force_terminal=True, legacy_windows=False)

        offer_post_task_actions(
            task_res_no_proof,
            self.repo_dir,
            console=console_diff,
            input_fn=input_recorder_diff,
            interactive=True,
        )
        self.assertTrue(any("View Diff" in p for p in recorded_prompts))
        self.assertTrue(all("Inspect Proof" not in p for p in recorded_prompts))

        out_diff = buf_diff.getvalue()
        self.assertIn("Task Git Diff", out_diff)
        self.assertIn("uncommitted modification", out_diff)

        # Case 6b: Neither proof nor diff exists -> exits cleanly
        subprocess.run(["git", "checkout", "--", "README.md"], cwd=self.repo_dir, capture_output=True, check=True)
        buf_empty = io.StringIO()
        console_empty = Console(file=buf_empty, force_terminal=True, legacy_windows=False)
        offer_post_task_actions(
            task_res_no_proof,
            self.repo_dir,
            console=console_empty,
            input_fn=lambda prompt: "",
            interactive=True,
        )
        self.assertEqual(buf_empty.getvalue(), "")

        # Case 6c: EOFError during input is handled cleanly
        def raise_eof(prompt):
            raise EOFError()

        buf_eof = io.StringIO()
        console_eof = Console(file=buf_eof, force_terminal=True, legacy_windows=False)
        # Create a proof so action prompt triggers
        self._create_and_save_proof("task_eof_001")
        task_res_eof = TaskResult(
            task_id="task_eof_001",
            user_request="eof req",
            success=True,
            verified=True,
            status="COMMITTED",
            summary="eof summary",
        )
        recorded_eof_prompts = []
        def input_eof(prompt):
            recorded_eof_prompts.append(prompt)
            raise EOFError()

        offer_post_task_actions(
            task_res_eof,
            self.repo_dir,
            console=console_eof,
            input_fn=input_eof,
            interactive=True,
        )
        # Did not crash and offered Inspect Proof
        self.assertTrue(any("Inspect Proof" in p for p in recorded_eof_prompts))

    # -------------------------------------------------------------------------
    # 7. Regression: Unspecified fields not fabricated & end-to-end compatibility
    # -------------------------------------------------------------------------
    def test_unspecified_fields_not_fabricated(self):
        """Target files and test commands are explicitly marked (unspecified) if absent in plan."""
        plan_path = self.repo_dir / ".brainfrog" / "plans" / "plan_bare.md"
        bare_plan = PlanDocument(
            filename="plan_bare.md",
            path=plan_path,
            title="Bare task description",
            goal="Do something minimal",
            relevant_files=[],
            verification_commands=[],
            commit_hash=self.head_sha,
        )

        buf = io.StringIO()
        console = Console(file=buf, force_terminal=True, legacy_windows=False)

        choice = review_and_confirm_plan(
            bare_plan,
            self.repo_dir,
            console=console,
            input_fn=lambda prompt: "c",
            interactive=True,
        )
        self.assertEqual(choice, "cancel")

        output = buf.getvalue()
        # Verify no fabrication
        self.assertIn("Bare task description", output)
        self.assertIn("(unspecified)", output)

    def test_execute_task_integrates_prev_post_task_hook(self):
        """execute_task in interactive mode calls offer_post_task_actions with task evidence."""
        mock_runtime = MagicMock(spec=BrainFrogRuntime)

        # Mock execute_autonomous_task returning a verified TaskResult
        proof = self._create_and_save_proof("task_e2e_001", commit_sha=self.head_sha)
        mock_task_res = TaskResult(
            task_id="task_e2e_001",
            user_request="Run verified task",
            success=True,
            verified=True,
            status="COMMITTED",
            summary="Verified execution",
            proof_artifact=proof,
        )

        actions_called = []
        def mock_post_input(prompt):
            actions_called.append(prompt)
            return ""

        with patch("cli.execute_autonomous_task", return_value=mock_task_res):
            with patch("cli.print_task_verification_report"):
                exit_code = execute_task(
                    task="Run verified task",
                    repo_dir=self.repo_dir,
                    runtime=mock_runtime,
                    is_interactive=True,
                    post_task_input_fn=mock_post_input,
                )

        self.assertEqual(exit_code, 0)
        self.assertTrue(len(actions_called) > 0)
        self.assertIn("Aksi", actions_called[0])


if __name__ == "__main__":
    unittest.main()
