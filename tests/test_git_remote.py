"""test_git_remote.py — Tests for Git remote configuration, sanitization, and auto-push."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from orchestrator import (
    clean_git_remote_url,
    get_git_remote_url,
    configure_git_remote,
    Orchestrator,
    RunConfig,
)
from system2.claude_client import PlanStep


class TestGitRemoteOperations(unittest.TestCase):
    def test_clean_git_remote_url(self):
        # 1. Plain HTTPS URL
        self.assertEqual(
            clean_git_remote_url("https://github.com/dameepng/clinic-landing-page.git"),
            "https://github.com/dameepng/clinic-landing-page.git",
        )
        # 2. Full command copy-pasted with 'git remote add origin'
        self.assertEqual(
            clean_git_remote_url("git remote add origin https://github.com/dameepng/clinic-landing-page.git"),
            "https://github.com/dameepng/clinic-landing-page.git",
        )
        # 3. Full command with quotes
        self.assertEqual(
            clean_git_remote_url("git remote add origin 'https://github.com/dameepng/clinic-landing-page.git'"),
            "https://github.com/dameepng/clinic-landing-page.git",
        )
        # 4. SSH URL with 'git remote set-url'
        self.assertEqual(
            clean_git_remote_url("git remote set-url origin git@github.com:dameepng/testing.git"),
            "git@github.com:dameepng/testing.git",
        )
        # 5. File/local path
        self.assertEqual(
            clean_git_remote_url("git remote add origin C:/bare/repo.git"),
            "C:/bare/repo.git",
        )
        # 6. Empty string
        self.assertEqual(clean_git_remote_url(""), "")

    def test_scenario1_new_project_without_remote(self):
        """Scenario 1: Project dummy baru TANPA remote git sama sekali.
        User/CLI passes 'git remote add origin <url>' -> sanitizes and pushes cleanly.
        """
        with tempfile.TemporaryDirectory() as td_bare, tempfile.TemporaryDirectory() as td_work:
            bare_dir = Path(td_bare)
            work_dir = Path(td_work)

            # Create a bare remote repo
            subprocess.run(["git", "init", "--bare"], cwd=bare_dir, check=True, capture_output=True)

            # Create working repo without any remote
            subprocess.run(["git", "init"], cwd=work_dir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "TestUser"], cwd=work_dir, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=work_dir, check=True)
            subprocess.run(["git", "branch", "-M", "main"], cwd=work_dir, check=True, capture_output=True)

            # Initially no remote
            self.assertEqual(get_git_remote_url(work_dir, "origin"), "")

            # User input that reproduces the original bug: pasting the whole command
            raw_input = f"git remote add origin {bare_dir.as_uri()}"
            ok, clean_url = configure_git_remote(work_dir, raw_input, "origin")

            self.assertTrue(ok)
            self.assertEqual(clean_url, bare_dir.as_uri())
            self.assertEqual(get_git_remote_url(work_dir, "origin"), bare_dir.as_uri())

            # Commit a file and push
            (work_dir / "app.txt").write_text("hello world", encoding="utf-8")
            subprocess.run(["git", "add", "app.txt"], cwd=work_dir, check=True)
            subprocess.run(["git", "commit", "-m", "feat: initial commit"], cwd=work_dir, check=True)

            push_res = subprocess.run(
                ["git", "push", "-u", "origin", "main"],
                cwd=work_dir,
                capture_output=True,
                text=True,
            )
            # Push must succeed with 0 and NO protocol error
            self.assertEqual(push_res.returncode, 0)
            self.assertNotIn("fatal: protocol", push_res.stderr)

    def test_scenario2_project_already_has_remote_origin(self):
        """Scenario 2: Project yang SUDAH punya remote origin.
        Re-configuring remote must NOT fail with 'remote origin already exists'
        and must update via set-url seamlessly.
        """
        with tempfile.TemporaryDirectory() as td_bare1, tempfile.TemporaryDirectory() as td_bare2, tempfile.TemporaryDirectory() as td_work:
            bare1 = Path(td_bare1)
            bare2 = Path(td_bare2)
            work_dir = Path(td_work)

            subprocess.run(["git", "init", "--bare"], cwd=bare1, check=True, capture_output=True)
            subprocess.run(["git", "init", "--bare"], cwd=bare2, check=True, capture_output=True)

            subprocess.run(["git", "init"], cwd=work_dir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "TestUser"], cwd=work_dir, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=work_dir, check=True)
            subprocess.run(["git", "branch", "-M", "main"], cwd=work_dir, check=True, capture_output=True)

            # Step 1: Set remote to bare1
            ok1, url1 = configure_git_remote(work_dir, bare1.as_uri(), "origin")
            self.assertTrue(ok1)
            self.assertEqual(get_git_remote_url(work_dir, "origin"), bare1.as_uri())

            # Step 2: Edge case - re-run configure_git_remote when origin already exists
            # Input also includes the 'git remote add origin' prefix
            raw_update = f"git remote add origin {bare2.as_uri()}"
            ok2, url2 = configure_git_remote(work_dir, raw_update, "origin")

            # Must succeed via set-url fallback
            self.assertTrue(ok2)
            self.assertEqual(url2, bare2.as_uri())
            self.assertEqual(get_git_remote_url(work_dir, "origin"), bare2.as_uri())

            # Commit and push to verify remote works cleanly
            (work_dir / "index.js").write_text("console.log('hi');", encoding="utf-8")
            subprocess.run(["git", "add", "index.js"], cwd=work_dir, check=True)
            subprocess.run(["git", "commit", "-m", "feat: new file"], cwd=work_dir, check=True)

            push_res = subprocess.run(
                ["git", "push", "-u", "origin", "main"],
                cwd=work_dir,
                capture_output=True,
                text=True,
            )
            self.assertEqual(push_res.returncode, 0)
            self.assertNotIn("fatal: protocol", push_res.stderr)

    def test_scenario3_orchestrator_auto_repairs_corrupted_remote_before_push(self):
        """Scenario 3: Corrupted remote in .git/config is auto-repaired by orchestrator."""
        with tempfile.TemporaryDirectory() as td_bare, tempfile.TemporaryDirectory() as td_work:
            bare_dir = Path(td_bare)
            work_dir = Path(td_work)

            subprocess.run(["git", "init", "--bare"], cwd=bare_dir, check=True, capture_output=True)
            subprocess.run(["git", "init"], cwd=work_dir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "TestUser"], cwd=work_dir, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=work_dir, check=True)
            subprocess.run(["git", "branch", "-M", "main"], cwd=work_dir, check=True, capture_output=True)

            # Intentionally corrupt origin in .git/config like the user's issue
            bad_url = f"git remote add origin {bare_dir.as_uri()}"
            subprocess.run(["git", "remote", "add", "origin", bad_url], cwd=work_dir, check=True)

            # Initial commit
            (work_dir / "file.txt").write_text("initial", encoding="utf-8")
            subprocess.run(["git", "add", "file.txt"], cwd=work_dir, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=work_dir, check=True)

            # Mock Orchestrator run
            logs = []
            cfg = RunConfig(
                task="test auto push",
                repo_dir=work_dir,
                auto_pr=False,
                test_command=["python", "-c", "import sys; sys.exit(0)"],
            )
            mock_s1 = MagicMock()
            from system1 import Answer
            mock_s1.decide.return_value = {
                "diff_risk": Answer(choice="low", score=0, noul=0.1, confidence=0.9),
                "safe_to_proceed": Answer(choice="open_pr", score="low", noul=1.0, confidence=0.95),
            }
            mock_s2 = MagicMock()
            mock_s2.draft_pr.return_value = {"title": "feat: test change", "body": "details"}

            orch = Orchestrator(mock_s1, mock_s2, cfg, log_fn=lambda m: logs.append(m))
            step = PlanStep("1", "test step", ["file.txt"])

            # Touch a file to trigger commit & push in _finalize_pr
            (work_dir / "file.txt").write_text("updated content", encoding="utf-8")
            subprocess.run(["git", "add", "file.txt"], cwd=work_dir, check=True)

            res = orch._finalize_pr(step, retries=0, test_output="PASS")

            # Check that push succeeded and URL was repaired
            self.assertTrue(any("Successfully pushed to origin/main" in l for l in logs))
            self.assertFalse(any("fatal: protocol" in l for l in logs))
            self.assertEqual(get_git_remote_url(work_dir, "origin"), bare_dir.as_uri())


if __name__ == "__main__":
    unittest.main()
