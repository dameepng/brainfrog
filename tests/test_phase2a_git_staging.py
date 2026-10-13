"""Adversarial security test suite for Phase 2A Step 1: Git Staging Hardening.

Validates all required security invariants:
1. ADV-GIT-01: Unrelated untracked files remain untracked and excluded from agent staging.
2. ADV-GIT-02: Unrelated unstaged user modifications remain untouched in working tree.
3. ADV-GIT-03: Pre-existing staged changes outside agent targets trigger a safe rejection.
4. ADV-GIT-04: Pre-existing staged changes overlapping agent targets trigger a safe rejection.
5. ADV-GIT-05: Target deletion is staged correctly without staging unrelated files.
6. ADV-GIT-06: Target rename is staged correctly without staging unrelated files.
7. ADV-GIT-07: Paths containing spaces and platform-specific path separators are handled safely.
8. ADV-GIT-08: Path traversal attempts are rejected fail-closed.
9. ADV-GIT-09: Failure during staging preserves index without corrupting developer state.
10. ADV-GIT-10: Orchestrator _finalize_pr never stages untracked files into commits.
11. ADV-GIT-11: Reject .git metadata paths, worktree gitdirs, and option-like path components.
12. ADV-GIT-12: Rolled back, failed, or uncommitted transactions fail closed and are rejected.
13. ADV-GIT-13: Exceptions during pre-commit scan or finalizer cleanly unstage targets and preserve repo.
14. ADV-GIT-14: Historically tracked scratch files in repo do not contaminate index or block staging.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from core.runtime.git_staging import (
    GitStagingError,
    PathTraversalSecurityError,
    inspect_staged_index,
    inspect_unstaged_diff,
    resolve_canonical_repo_path,
    stage_authorized_targets,
    unstage_target_paths,
)
from core.runtime.transaction import (
    OperationStatus,
    OperationType,
    TransactionOperation,
    TransactionResult,
    TransactionStatus,
)
from orchestrator import Orchestrator, RunConfig
from system1.base import Answer
from system2 import PlanStep


class TestPhase2AGitStagingHardening(unittest.TestCase):
    """Adversarial tests proving zero-contamination Git staging."""

    def _init_repo(self, repo_dir: Path) -> None:
        """Initialize a local temporary git repository with initial commit."""
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "SecurityTester"], cwd=repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "tester@brainfrog.local"], cwd=repo_dir, check=True)
        subprocess.run(["git", "branch", "-M", "main"], cwd=repo_dir, check=True, capture_output=True)

        readme = repo_dir / "README.md"
        readme.write_text("# Test Repo\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True)
        subprocess.run(["git", "commit", "-m", "chore: initial commit"], cwd=repo_dir, check=True, capture_output=True)

    def test_adv_git_01_untracked_files_excluded_from_staging_and_commit(self):
        """ADV-GIT-01: Unrelated untracked files remain untracked and excluded from agent staging."""
        with tempfile.TemporaryDirectory() as td:
            repo_dir = Path(td)
            self._init_repo(repo_dir)

            # User creates untracked sensitive files
            secret_file = repo_dir / "secrets.env"
            secret_file.write_text("API_KEY=supersecret123\n", encoding="utf-8")
            notes_file = repo_dir / "scratch_notes.txt"
            notes_file.write_text("personal user notes\n", encoding="utf-8")

            # Agent mutates authorized target
            target_file = repo_dir / "src" / "feature.py"
            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_text("def hello(): return 'world'\n", encoding="utf-8")

            res = stage_authorized_targets(repo_dir, ["src/feature.py"])
            self.assertTrue(res.success, f"Staging failed: {res.error}")
            self.assertEqual(res.staged_paths, ("src/feature.py",))

            # Verify index contents
            staged = inspect_staged_index(repo_dir)
            self.assertEqual(staged, ["src/feature.py"])

            # Verify secrets and notes remain untracked (??)
            status_proc = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                check=True,
            )
            status_lines = status_proc.stdout.splitlines()
            self.assertIn("?? secrets.env", status_lines)
            self.assertIn("?? scratch_notes.txt", status_lines)
            self.assertIn("A  src/feature.py", status_lines)

            # Commit staged changes
            subprocess.run(["git", "commit", "-m", "feat: add feature"], cwd=repo_dir, check=True, capture_output=True)

            # Inspect commit tree: only feature.py must be committed
            show_proc = subprocess.run(
                ["git", "show", "--name-only", "--oneline", "HEAD"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                check=True,
            )
            committed_files = [line.strip() for line in show_proc.stdout.splitlines()[1:] if line.strip()]
            self.assertEqual(committed_files, ["src/feature.py"])

    def test_adv_git_02_unrelated_unstaged_user_modifications_remain_untouched(self):
        """ADV-GIT-02: Unrelated unstaged user modifications remain untouched in working tree."""
        with tempfile.TemporaryDirectory() as td:
            repo_dir = Path(td)
            self._init_repo(repo_dir)

            # Commit an existing user file
            user_file = repo_dir / "user_code.py"
            user_file.write_text("def user_func(): return 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "user_code.py"], cwd=repo_dir, check=True)
            subprocess.run(["git", "commit", "-m", "feat: user code"], cwd=repo_dir, check=True, capture_output=True)

            # User makes unstaged modification to user_code.py
            user_file.write_text("def user_func(): return 999  # WIP unstaged\n", encoding="utf-8")

            # Agent creates target file
            agent_file = repo_dir / "agent_tool.py"
            agent_file.write_text("def agent_run(): pass\n", encoding="utf-8")

            res = stage_authorized_targets(repo_dir, ["agent_tool.py"])
            self.assertTrue(res.success)
            self.assertEqual(res.staged_paths, ("agent_tool.py",))

            # User file must still be unstaged modified ( M user_code.py)
            status_proc = subprocess.run(["git", "status", "--porcelain"], cwd=repo_dir, capture_output=True, text=True, check=True)
            status_lines = status_proc.stdout.splitlines()
            self.assertIn(" M user_code.py", status_lines)
            self.assertIn("A  agent_tool.py", status_lines)

            # Commit agent change
            subprocess.run(["git", "commit", "-m", "feat: agent tool"], cwd=repo_dir, check=True, capture_output=True)

            # User unstaged modification is preserved exactly
            self.assertEqual(user_file.read_text(encoding="utf-8"), "def user_func(): return 999  # WIP unstaged\n")

    def test_adv_git_03_pre_existing_staged_outside_targets_rejected_fail_closed(self):
        """ADV-GIT-03: Pre-existing staged changes outside agent targets trigger a safe rejection."""
        with tempfile.TemporaryDirectory() as td:
            repo_dir = Path(td)
            self._init_repo(repo_dir)

            # User stages an unrelated draft file
            draft_file = repo_dir / "draft_spec.md"
            draft_file.write_text("User work in progress\n", encoding="utf-8")
            subprocess.run(["git", "add", "draft_spec.md"], cwd=repo_dir, check=True)

            # Agent attempts to stage its target
            target_file = repo_dir / "calculator.py"
            target_file.write_text("def add(a, b): return a + b\n", encoding="utf-8")

            res = stage_authorized_targets(repo_dir, ["calculator.py"])
            self.assertFalse(res.success)
            self.assertEqual(res.rejection_code, "contaminated_index")
            self.assertIn("draft_spec.md", res.error)

            # User draft MUST NOT be silently uncommitted or reset
            staged = inspect_staged_index(repo_dir)
            self.assertEqual(staged, ["draft_spec.md"])
            self.assertEqual(draft_file.read_text(encoding="utf-8"), "User work in progress\n")

    def test_adv_git_04_pre_existing_staged_overlapping_targets_rejected_fail_closed(self):
        """ADV-GIT-04: Pre-existing staged changes overlapping agent targets trigger a safe rejection."""
        with tempfile.TemporaryDirectory() as td:
            repo_dir = Path(td)
            self._init_repo(repo_dir)

            # User edits target and stages it
            target_file = repo_dir / "auth.py"
            target_file.write_text("def auth(): return 'user_staged'\n", encoding="utf-8")
            subprocess.run(["git", "add", "auth.py"], cwd=repo_dir, check=True)

            # Case A: Agent has transaction record
            op = TransactionOperation(
                type=OperationType.MODIFY_FILE,
                target="auth.py",
                status=OperationStatus.EXECUTED,
            )
            tx_res = TransactionResult(
                transaction_id="tx_test_001",
                status=TransactionStatus.COMMITTED,
                committed=True,
                rolled_back=False,
                operations=(op,),
            )

            res = stage_authorized_targets(repo_dir, [op], last_tx_result=tx_res)
            self.assertFalse(res.success)
            self.assertEqual(res.rejection_code, "pre_existing_staged_target")
            self.assertIn("auth.py", res.error)

            # Case B: Direct caller without transaction record, but disk diverged from index
            target_file.write_text("def auth(): return 'agent_diverged'\n", encoding="utf-8")
            res_b = stage_authorized_targets(repo_dir, ["auth.py"], last_tx_result=None)
            self.assertFalse(res_b.success)
            self.assertEqual(res_b.rejection_code, "pre_existing_staged_target")

            # Staged content in Git index remains uncorrupted
            show_proc = subprocess.run(
                ["git", "show", ":auth.py"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(show_proc.stdout, "def auth(): return 'user_staged'\n")

    def test_adv_git_05_target_deletion_staged_cleanly(self):
        """ADV-GIT-05: Target deletion is staged correctly without staging unrelated files."""
        with tempfile.TemporaryDirectory() as td:
            repo_dir = Path(td)
            self._init_repo(repo_dir)

            # Create and commit obsolete file
            del_file = repo_dir / "obsolete.py"
            del_file.write_text("# to delete\n", encoding="utf-8")
            subprocess.run(["git", "add", "obsolete.py"], cwd=repo_dir, check=True)
            subprocess.run(["git", "commit", "-m", "chore: add obsolete"], cwd=repo_dir, check=True, capture_output=True)

            # Create unrelated untracked file
            (repo_dir / "untracked.log").write_text("log data\n", encoding="utf-8")

            # Delete obsolete file on disk
            del_file.unlink()

            op = TransactionOperation(
                type=OperationType.DELETE_FILE,
                target="obsolete.py",
                status=OperationStatus.EXECUTED,
            )
            res = stage_authorized_targets(repo_dir, [op])
            self.assertTrue(res.success, f"Deletion staging failed: {res.error}")

            status_proc = subprocess.run(["git", "status", "--porcelain"], cwd=repo_dir, capture_output=True, text=True, check=True)
            status_lines = status_proc.stdout.splitlines()
            self.assertIn("D  obsolete.py", status_lines)
            self.assertIn("?? untracked.log", status_lines)

    def test_adv_git_06_target_rename_staged_cleanly(self):
        """ADV-GIT-06: Target rename is staged correctly without staging unrelated files."""
        with tempfile.TemporaryDirectory() as td:
            repo_dir = Path(td)
            self._init_repo(repo_dir)

            old_file = repo_dir / "old_name.py"
            old_file.write_text("class OldName: pass\n", encoding="utf-8")
            subprocess.run(["git", "add", "old_name.py"], cwd=repo_dir, check=True)
            subprocess.run(["git", "commit", "-m", "chore: add old file"], cwd=repo_dir, check=True, capture_output=True)

            (repo_dir / "scratch.tmp").write_text("temporary\n", encoding="utf-8")

            # Perform rename on disk
            new_file = repo_dir / "new_name.py"
            old_file.rename(new_file)

            op = TransactionOperation(
                type=OperationType.RENAME_FILE,
                target="old_name.py",
                new_target="new_name.py",
                status=OperationStatus.EXECUTED,
            )
            res = stage_authorized_targets(repo_dir, [op])
            self.assertTrue(res.success, f"Rename staging failed: {res.error}")

            status_proc = subprocess.run(["git", "status", "--porcelain"], cwd=repo_dir, capture_output=True, text=True, check=True)
            status_lines = status_proc.stdout.splitlines()
            # R  old_name.py -> new_name.py
            self.assertTrue(any("R  old_name.py -> new_name.py" in l or "R " in l for l in status_lines))
            self.assertIn("?? scratch.tmp", status_lines)

    def test_adv_git_07_paths_with_spaces_and_separators(self):
        """ADV-GIT-07: Paths containing spaces and platform-specific path separators are handled safely."""
        with tempfile.TemporaryDirectory() as td:
            repo_dir = Path(td)
            self._init_repo(repo_dir)

            spaced_dir = repo_dir / "my folder with spaces"
            spaced_dir.mkdir(parents=True, exist_ok=True)
            spaced_file = spaced_dir / "my script with spaces.py"
            spaced_file.write_text("print('spaced')\n", encoding="utf-8")

            (repo_dir / "other untracked.txt").write_text("untracked\n", encoding="utf-8")

            # Test Windows-style backslash resolution
            win_path = "my folder with spaces\\my script with spaces.py"
            canon = resolve_canonical_repo_path(repo_dir, win_path)
            self.assertEqual(canon, "my folder with spaces/my script with spaces.py")

            res = stage_authorized_targets(repo_dir, [win_path])
            self.assertTrue(res.success, f"Spaced staging failed: {res.error}")
            self.assertEqual(res.staged_paths, ("my folder with spaces/my script with spaces.py",))

            status_proc = subprocess.run(["git", "status", "--porcelain"], cwd=repo_dir, capture_output=True, text=True, check=True)
            status_lines = status_proc.stdout.splitlines()
            self.assertTrue(any("other untracked.txt" in l for l in status_lines))
            self.assertTrue(any("my script with spaces.py" in l for l in status_lines))

    def test_adv_git_08_path_traversal_rejected_fail_closed(self):
        """ADV-GIT-08: Path traversal attempts are rejected fail-closed."""
        with tempfile.TemporaryDirectory() as td:
            repo_dir = Path(td)
            self._init_repo(repo_dir)

            traversal_targets = [
                "../../outside.py",
                "../escape.txt",
                "subdir/../../escaped.py",
                "C:/Windows/System32/calc.exe",
                ".",
                "",
                "   ",
            ]
            for t in traversal_targets:
                res = stage_authorized_targets(repo_dir, [t])
                self.assertFalse(res.success, f"Expected rejection for traversal '{t}'")
                self.assertEqual(res.rejection_code, "path_traversal")

            # Git index must remain completely empty/clean
            self.assertEqual(inspect_staged_index(repo_dir), [])

    def test_adv_git_09_unstage_target_paths_preserves_other_index_entries(self):
        """ADV-GIT-09: Unstaging only removes specified targets and preserves developer index state."""
        with tempfile.TemporaryDirectory() as td:
            repo_dir = Path(td)
            self._init_repo(repo_dir)

            # Two files staged
            f1 = repo_dir / "preserve_me.txt"
            f1.write_text("preserve\n", encoding="utf-8")
            subprocess.run(["git", "add", "preserve_me.txt"], cwd=repo_dir, check=True)

            f2 = repo_dir / "unstage_me.txt"
            f2.write_text("unstage\n", encoding="utf-8")
            subprocess.run(["git", "add", "unstage_me.txt"], cwd=repo_dir, check=True)

            # Unstage ONLY f2
            unstage_target_paths(repo_dir, ["unstage_me.txt"])

            staged = inspect_staged_index(repo_dir)
            self.assertEqual(staged, ["preserve_me.txt"])
            self.assertTrue(f2.exists())

    def test_adv_git_10_orchestrator_finalize_pr_never_stages_untracked_files(self):
        """ADV-GIT-10: Orchestrator _finalize_pr never stages untracked files into commits."""
        with tempfile.TemporaryDirectory() as td_bare, tempfile.TemporaryDirectory() as td_work:
            bare_dir = Path(td_bare)
            work_dir = Path(td_work)

            subprocess.run(["git", "init", "--bare"], cwd=bare_dir, check=True, capture_output=True)
            self._init_repo(work_dir)
            subprocess.run(["git", "remote", "add", "origin", bare_dir.as_uri()], cwd=work_dir, check=True)
            subprocess.run(["git", "push", "-u", "origin", "main"], cwd=work_dir, check=True, capture_output=True)

            # Developer has untracked secret and scratch files
            (work_dir / ".env.production").write_text("DB_PASSWORD=secret987\n", encoding="utf-8")
            (work_dir / "user_draft.py").write_text("print('user draft')\n", encoding="utf-8")

            # Agent mutates service.py
            (work_dir / "service.py").write_text("def run(): return 42\n", encoding="utf-8")

            # Setup orchestrator with transaction result targeting service.py
            op = TransactionOperation(
                type=OperationType.CREATE_FILE,
                target="service.py",
                status=OperationStatus.EXECUTED,
            )
            tx_res = TransactionResult(
                transaction_id="tx_test_orch_01",
                status=TransactionStatus.COMMITTED,
                committed=True,
                rolled_back=False,
                operations=(op,),
            )

            cfg = RunConfig(
                task="implement service",
                repo_dir=work_dir,
                auto_pr=False,
                test_command=["python", "-c", "import sys; sys.exit(0)"],
                last_transaction_result=tx_res,
            )
            mock_s1 = MagicMock()
            mock_s1.decide.return_value = {
                "diff_risk": Answer(choice="low", score=0, noul=0.1, confidence=0.9),
                "safe_to_proceed": Answer(choice="draft_pr", score="low", noul=1.0, confidence=0.95),
            }
            mock_s2 = MagicMock()
            mock_s2.draft_pr.return_value = {"title": "feat: implement service", "body": "service details"}

            orch = Orchestrator(mock_s1, mock_s2, cfg)
            step = PlanStep("1", "implement service", ["service.py"])

            res = orch._finalize_pr(step, retries=0, test_output="PASS")
            self.assertIn(res.outcome, ("drafted_pr", "opened_pr"))

            # Inspect latest commit: MUST contain ONLY service.py
            show_proc = subprocess.run(
                ["git", "show", "--name-only", "--oneline", "HEAD"],
                cwd=work_dir,
                capture_output=True,
                text=True,
                check=True,
            )
            committed_files = [line.strip() for line in show_proc.stdout.splitlines()[1:] if line.strip()]
            self.assertEqual(committed_files, ["service.py"])

            # Verify untracked files remain untouched in working directory
            self.assertTrue((work_dir / ".env.production").exists())
            self.assertTrue((work_dir / "user_draft.py").exists())
            status_proc = subprocess.run(["git", "status", "--porcelain"], cwd=work_dir, capture_output=True, text=True, check=True)
            self.assertIn("?? user_draft.py", status_proc.stdout)

    def test_adv_git_11_reject_git_metadata_and_option_paths(self):
        """ADV-GIT-11: Reject .git metadata paths, worktree gitdirs, and option-like path components."""
        with tempfile.TemporaryDirectory() as td:
            repo_dir = Path(td)
            self._init_repo(repo_dir)

            # Test 1: Reject direct .git and descendants
            bad_git_paths = [
                ".git",
                ".git/config",
                ".git/hooks/pre-commit",
                "./.git/HEAD",
                "nested/.git/config",
            ]
            for p in bad_git_paths:
                with self.assertRaises(PathTraversalSecurityError, msg=f"Should reject {p}"):
                    resolve_canonical_repo_path(repo_dir, p)

            # Test 2: Reject option-like paths
            bad_option_paths = [
                "-p",
                "--amend",
                "-u",
                "--all",
                "src/-p",
                "src/--flag",
            ]
            for p in bad_option_paths:
                with self.assertRaises(PathTraversalSecurityError, msg=f"Should reject {p}"):
                    resolve_canonical_repo_path(repo_dir, p)

            # Test 3: Worktree gitdir pointer
            with tempfile.TemporaryDirectory() as td_worktree:
                wt_dir = Path(td_worktree)
                external_gitdir = repo_dir / "external_git_dir"
                external_gitdir.mkdir()
                # Create .git file pointing to external gitdir
                (wt_dir / ".git").write_text(f"gitdir: {external_gitdir}\n", encoding="utf-8")
                with self.assertRaises(PathTraversalSecurityError):
                    resolve_canonical_repo_path(wt_dir, str(external_gitdir / "config"))

            # Test 4: Legitimate paths with spaces and nested directories are preserved
            legit_path = "src/my nested dir/app code.py"
            resolved = resolve_canonical_repo_path(repo_dir, legit_path)
            self.assertEqual(resolved, "src/my nested dir/app code.py")

    def test_adv_git_12_uncommitted_transaction_rejected_fail_closed(self):
        """ADV-GIT-12: Rolled back, failed, or uncommitted transactions fail closed and are rejected."""
        with tempfile.TemporaryDirectory() as td:
            repo_dir = Path(td)
            self._init_repo(repo_dir)

            (repo_dir / "service.py").write_text("print('test')\n", encoding="utf-8")
            op = TransactionOperation(
                type=OperationType.CREATE_FILE,
                target="service.py",
                status=OperationStatus.EXECUTED,
            )

            # 1. Rolled-back transaction
            tx_rolled_back = TransactionResult(
                transaction_id="tx_rb",
                status=TransactionStatus.ROLLED_BACK,
                committed=False,
                rolled_back=True,
                operations=(op,),
            )
            res = stage_authorized_targets(repo_dir, ["service.py"], last_tx_result=tx_rolled_back)
            self.assertFalse(res.success)
            self.assertEqual(res.rejection_code, "uncommitted_transaction")

            # 2. Failed transaction
            tx_failed = TransactionResult(
                transaction_id="tx_failed",
                status=TransactionStatus.FAILED,
                committed=False,
                rolled_back=False,
                operations=(op,),
            )
            res2 = stage_authorized_targets(repo_dir, ["service.py"], last_tx_result=tx_failed)
            self.assertFalse(res2.success)
            self.assertEqual(res2.rejection_code, "uncommitted_transaction")

            # 3. Operations with non-EXECUTED status (e.g. STAGED or FAILED) are rejected
            op_failed = TransactionOperation(
                type=OperationType.CREATE_FILE,
                target="service.py",
                status=OperationStatus.FAILED,
            )
            res3 = stage_authorized_targets(repo_dir, [op_failed])
            self.assertFalse(res3.success)
            self.assertEqual(res3.rejection_code, "unauthorized_targets")

            # 4. Orchestrator production step guard prevents speculative step.files fallback
            cfg = RunConfig(task="test", repo_dir=repo_dir)
            mock_s1 = MagicMock()
            mock_s2 = MagicMock()
            orch = Orchestrator(mock_s1, mock_s2, cfg)
            orch._in_production_step = True
            step = PlanStep("1", "test", ["service.py"])
            # In production, without valid committed transaction, targets must be empty!
            targets = orch._get_authorized_targets_for_step(step, tx_result=None)
            self.assertEqual(targets, [])

    def test_adv_git_13_exception_in_scanner_triggers_clean_unstage(self):
        """ADV-GIT-13: Exceptions during pre-commit scan or finalizer cleanly unstage targets and preserve repo."""
        with tempfile.TemporaryDirectory() as td:
            repo_dir = Path(td)
            self._init_repo(repo_dir)

            (repo_dir / "feature.py").write_text("def f(): pass\n", encoding="utf-8")
            op = TransactionOperation(
                type=OperationType.CREATE_FILE,
                target="feature.py",
                status=OperationStatus.EXECUTED,
            )
            tx_res = TransactionResult(
                transaction_id="tx_exc",
                status=TransactionStatus.COMMITTED,
                committed=True,
                rolled_back=False,
                operations=(op,),
            )
            cfg = RunConfig(
                task="feature",
                repo_dir=repo_dir,
                last_transaction_result=tx_res,
            )
            mock_s1 = MagicMock()
            mock_s1.decide.return_value = {
                "diff_risk": Answer(choice="low", score=0, noul=0.1, confidence=0.9),
                "safe_to_proceed": Answer(choice="draft_pr", score="low", noul=1.0, confidence=0.95),
            }
            mock_s2 = MagicMock()
            mock_s2.draft_pr.return_value = {"title": "feat: feature", "body": ""}
            orch = Orchestrator(mock_s1, mock_s2, cfg)
            step = PlanStep("1", "feature", ["feature.py"])

            from unittest.mock import patch
            with patch("orchestrator.scan_staged_changes", side_effect=RuntimeError("Simulated scanner crash")):
                res = orch._finalize_pr(step, retries=0, test_output="PASS")
                self.assertEqual(res.outcome, "escalated")
                self.assertIn("Simulated scanner crash", res.detail)

            # Verify index was cleanly unstaged — zero staged files
            staged = inspect_staged_index(repo_dir)
            self.assertEqual(staged, [])

            # Verify file still exists on disk
            self.assertTrue((repo_dir / "feature.py").exists())

    def test_adv_git_14_tracked_scratch_files_do_not_contaminate(self):
        """ADV-GIT-14: Historically tracked scratch files in repo do not contaminate index or block staging."""
        with tempfile.TemporaryDirectory() as td:
            repo_dir = Path(td)
            self._init_repo(repo_dir)

            # Historical scratch file committed in repository
            scratch_file = repo_dir / ".brainfrog" / "scratch" / "legacy.txt"
            scratch_file.parent.mkdir(parents=True, exist_ok=True)
            scratch_file.write_text("legacy scratch\n", encoding="utf-8")
            subprocess.run(["git", "add", ".brainfrog/scratch/legacy.txt"], cwd=repo_dir, check=True)
            subprocess.run(["git", "commit", "-m", "chore: legacy scratch"], cwd=repo_dir, check=True, capture_output=True)

            # Now agent mutates only service.py
            (repo_dir / "service.py").write_text("def run(): pass\n", encoding="utf-8")
            op = TransactionOperation(
                type=OperationType.CREATE_FILE,
                target="service.py",
                status=OperationStatus.EXECUTED,
            )
            tx_res = TransactionResult(
                transaction_id="tx_scratch",
                status=TransactionStatus.COMMITTED,
                committed=True,
                rolled_back=False,
                operations=(op,),
            )

            cfg = RunConfig(
                task="service",
                repo_dir=repo_dir,
                last_transaction_result=tx_res,
            )
            mock_s1 = MagicMock()
            mock_s1.decide.return_value = {
                "diff_risk": Answer(choice="low", score=0, noul=0.1, confidence=0.9),
                "safe_to_proceed": Answer(choice="draft_pr", score="low", noul=1.0, confidence=0.95),
            }
            mock_s2 = MagicMock()
            mock_s2.draft_pr.return_value = {"title": "feat: service", "body": ""}
            orch = Orchestrator(mock_s1, mock_s2, cfg)
            step = PlanStep("1", "service", ["service.py"])

            # _finalize_pr must succeed without purge_tracked_sensitive_files corrupting index
            res = orch._finalize_pr(step, retries=0, test_output="PASS")
            self.assertIn(res.outcome, ("drafted_pr", "opened_pr"))

            # Latest commit must contain ONLY service.py, NOT scratch file
            show_proc = subprocess.run(
                ["git", "show", "--name-only", "--oneline", "HEAD"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                check=True,
            )
            committed_files = [line.strip() for line in show_proc.stdout.splitlines()[1:] if line.strip()]
            self.assertEqual(committed_files, ["service.py"])


if __name__ == "__main__":
    unittest.main()
