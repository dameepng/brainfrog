"""Unit tests for Git Secret Guard (Pre-Commit & Pre-Push Secret Scanning)."""
import subprocess
import tempfile
import unittest
from pathlib import Path

from git_guard import (
    ensure_gitignore_security,
    is_safe_value,
    purge_tracked_sensitive_files,
    redact,
    scan_file_path,
    scan_staged_changes,
    scan_text_content,
    unstage_staged_changes,
)

# Dynamically construct test tokens at runtime to prevent false-positive alerts
# from external static analysis scanners (GitGuardian, TruffleHog) on the repo itself
_DUMMY_HEX_1 = "".join(["a8f5", "b2c9", "d1e4", "f6a7", "b8c9", "d0e1", "f2a3", "b4c5"])
_DUMMY_HEX_2 = "".join(["0123456789abcdef", "0123456789abcdef"])
_DUMMY_B64 = "".join(["aZGT", "fZ+5", "4D7c", "UHnw", "5j3Q", "PVgz", "YEY/", "rcv5", "xiU4", "LDlg", "WJs="])
_DUMMY_ANTHROPIC = "-".join(["sk", "ant", "api03", "".join(["9a8b", "7c6d", "5e4f", "3a2b", "1c0d", "9e8f", "7a6b", "5c4d"])])
_DUMMY_GITHUB = "gh" + "p_" + "123456789012345678901234567890123456"
_VAR_ENC = "ENCRYPTION" + "_KEY"
_VAR_GH = "GITHUB" + "_TOKEN"
_FIELD_ENC = "encryption" + "Key"


class TestGitGuard(unittest.TestCase):
    def test_redact_masks_secret(self):
        self.assertEqual(redact("short"), "***")
        self.assertEqual(redact("1234567890abcdef"), "1234...cdef")

    def test_is_safe_value(self):
        self.assertTrue(is_safe_value("your_api_key_here"))
        self.assertTrue(is_safe_value("process.env.SECRET"))
        self.assertTrue(is_safe_value("dummy_test_placeholder"))
        self.assertFalse(is_safe_value(_DUMMY_HEX_1))

    def test_scan_file_path(self):
        self.assertIsNotNone(scan_file_path(".env"))
        self.assertIsNotNone(scan_file_path(".env.local"))
        self.assertIsNotNone(scan_file_path("certs/server.pem"))
        self.assertIsNotNone(scan_file_path("config/service-account.json"))
        self.assertIsNotNone(scan_file_path("keys/id_rsa"))

        # Excluded safe files
        self.assertIsNone(scan_file_path(".env.example"))
        self.assertIsNone(scan_file_path("config.template.json"))
        self.assertIsNone(scan_file_path("README.md"))
        self.assertIsNone(scan_file_path("src/main.ts"))

    def test_scan_text_content_catches_generic_encryption_key(self):
        code = f"\n        const crypto = require('crypto');\n        const {_VAR_ENC} = '{_DUMMY_HEX_1}';\n        "
        findings = scan_text_content("app/crypto.js", code)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].rule, "Generic Encryption Key")
        self.assertIn("a8f5...b4c5", findings[0].redacted_snippet)
        self.assertNotIn(_DUMMY_HEX_1, findings[0].redacted_snippet)

    def test_scan_text_content_catches_api_keys(self):
        anthropic_code = f"client = Anthropic(api_key='{_DUMMY_ANTHROPIC}')"
        findings = scan_text_content("agent.py", anthropic_code)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].rule, "Anthropic API Key")

        github_code = f"{_VAR_GH} = '{_DUMMY_GITHUB}'"
        findings_gh = scan_text_content("deploy.sh", github_code)
        self.assertEqual(len(findings_gh), 1)
        self.assertEqual(findings_gh[0].rule, "GitHub Personal Access Token")

    def test_scan_text_content_ignores_safe_placeholders(self):
        code = f"""
        const key = process.env.{_VAR_ENC} || 'your_secret_key_here';
        // const OLD_KEY = 'dummy_placeholder';
        """
        findings = scan_text_content("config.ts", code)
        self.assertEqual(len(findings), 0)

    def test_scan_staged_changes_in_git_repo(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_path = Path(tmp_dir)
            subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)

            # 1. Clean commit should pass
            clean_file = repo_path / "hello.py"
            clean_file.write_text("print('hello world')\n", encoding="utf-8")
            subprocess.run(["git", "add", "hello.py"], cwd=repo_path, check=True)
            res = scan_staged_changes(repo_path)
            self.assertTrue(res.is_clean)
            self.assertEqual(len(res.findings), 0)

            # 2. Staged secret file should be blocked
            bad_file = repo_path / "secret_service.ts"
            bad_file.write_text(f"const {_VAR_ENC} = '{_DUMMY_HEX_2}';\n", encoding="utf-8")
            subprocess.run(["git", "add", "secret_service.ts"], cwd=repo_path, check=True)
            res_bad = scan_staged_changes(repo_path)
            self.assertFalse(res_bad.is_clean)
            self.assertEqual(len(res_bad.findings), 1)
            self.assertEqual(res_bad.findings[0].rule, "Generic Encryption Key")

            # 3. Unstage works
            unstage_staged_changes(repo_path)
            # After reset, staged should be empty
            res_after = scan_staged_changes(repo_path)
            self.assertTrue(res_after.is_clean)

    def test_ensure_gitignore_security(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_path = Path(tmp_dir)
            gitignore = repo_path / ".gitignore"
            gitignore.write_text("node_modules/\n", encoding="utf-8")

            modified = ensure_gitignore_security(repo_path)
            self.assertTrue(modified)

            content = gitignore.read_text(encoding="utf-8")
            self.assertIn(".env", content)
            self.assertIn("*.pem", content)
            self.assertIn(".next/", content)
            self.assertIn(".brainfrog/scratch/", content)

            # Second call should be a no-op
            modified_again = ensure_gitignore_security(repo_path)
            self.assertFalse(modified_again)

    def test_scan_json_quoted_encryption_key(self):
        json_content = f'{{\n  "node": {{}},\n  "{_FIELD_ENC}": "{_DUMMY_B64}"\n}}'
        findings = scan_text_content(".next/server/server-reference-manifest.json", json_content)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].rule, "Generic Encryption Key")
        self.assertNotIn(_DUMMY_B64, findings[0].redacted_snippet)

    def test_scan_file_path_blocks_build_caches(self):
        self.assertIsNotNone(scan_file_path(".next/server/server-reference-manifest.json"))
        self.assertIsNotNone(scan_file_path(".brainfrog/scratch/preview.png"))
        self.assertIsNotNone(scan_file_path("node_modules/package/index.js"))

    def test_purge_tracked_sensitive_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_path = Path(tmp_dir)
            subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)

            # Track a file in .next and .brainfrog/scratch
            next_dir = repo_path / ".next" / "server"
            next_dir.mkdir(parents=True, exist_ok=True)
            dummy_manifest = next_dir / "test.json"
            dummy_manifest.write_text('{"ok": true}\n', encoding="utf-8")

            subprocess.run(["git", "add", "-A"], cwd=repo_path, check=True, capture_output=True)

            # Verify it is tracked
            tracked = subprocess.run(["git", "ls-files", ".next"], cwd=repo_path, capture_output=True, text=True)
            self.assertIn("test.json", tracked.stdout)

            # Purge
            untracked = purge_tracked_sensitive_files(repo_path)
            self.assertIn(".next", untracked)

            # File should still exist on disk
            self.assertTrue(dummy_manifest.exists())

            # But not tracked in git
            tracked_after = subprocess.run(["git", "ls-files", ".next"], cwd=repo_path, capture_output=True, text=True)
            self.assertEqual(tracked_after.stdout.strip(), "")

    def test_scan_commit_range(self):
        from security.git_guard import scan_commit_range
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_path = Path(tmp_dir)
            subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_path, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_path, check=True)

            # 1. Clean commit
            f1 = repo_path / "app.py"
            f1.write_text("print('hello')\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo_path, check=True)
            subprocess.run(["git", "commit", "-m", "initial clean commit"], cwd=repo_path, check=True)

            # 2. Secret commit
            f2 = repo_path / "secret.py"
            f2.write_text(f"{_VAR_GH} = '{_DUMMY_GITHUB}'\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo_path, check=True)
            subprocess.run(["git", "commit", "-m", "secret commit"], cwd=repo_path, check=True)

            # Scan clean range
            res_clean = scan_commit_range(repo_path, "HEAD~1..HEAD~1")
            self.assertTrue(res_clean.is_clean)

            # Scan dirty range
            res_dirty = scan_commit_range(repo_path, "HEAD~1..HEAD")
            self.assertFalse(res_dirty.is_clean)
            self.assertEqual(len(res_dirty.findings), 1)
            self.assertEqual(res_dirty.findings[0].rule, "GitHub Personal Access Token")

    def test_cli_main_entrypoint(self):
        from security.git_guard import main
        # Running on current clean repo should return 0
        code = main(["--staged"])
        self.assertEqual(code, 0)

    def test_scan_pre_push_clean_push_allowed(self):
        from security.git_guard import scan_pre_push
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            remote_dir = tmp_path / "remote.git"
            local_dir = tmp_path / "local"

            subprocess.run(["git", "init", "--bare", str(remote_dir)], check=True, capture_output=True)
            subprocess.run(["git", "init", str(local_dir)], check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=local_dir, check=True)
            subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=local_dir, check=True)
            subprocess.run(["git", "remote", "add", "origin", str(remote_dir)], cwd=local_dir, check=True)

            (local_dir / "README.md").write_text("# Repo\n", encoding="utf-8")
            subprocess.run(["git", "checkout", "-B", "main"], cwd=local_dir, check=True, capture_output=True)
            subprocess.run(["git", "add", "."], cwd=local_dir, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=local_dir, check=True)
            subprocess.run(["git", "push", "-u", "origin", "main"], cwd=local_dir, check=True, capture_output=True)

            # Add clean commit
            (local_dir / "clean.py").write_text("print('clean')\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=local_dir, check=True)
            subprocess.run(["git", "commit", "-m", "clean commit"], cwd=local_dir, check=True)

            local_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=local_dir, text=True).strip()
            remote_sha = subprocess.check_output(["git", "rev-parse", "origin/main"], cwd=local_dir, text=True).strip()

            stdin_input = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
            res = scan_pre_push(local_dir, "origin", str(remote_dir), stdin_content=stdin_input)
            self.assertTrue(res.is_clean)
            self.assertEqual(len(res.findings), 0)

    def test_scan_pre_push_outgoing_secret_blocked(self):
        from security.git_guard import scan_pre_push
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            remote_dir = tmp_path / "remote.git"
            local_dir = tmp_path / "local"

            subprocess.run(["git", "init", "--bare", str(remote_dir)], check=True, capture_output=True)
            subprocess.run(["git", "init", str(local_dir)], check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=local_dir, check=True)
            subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=local_dir, check=True)
            subprocess.run(["git", "remote", "add", "origin", str(remote_dir)], cwd=local_dir, check=True)

            (local_dir / "README.md").write_text("# Repo\n", encoding="utf-8")
            subprocess.run(["git", "checkout", "-B", "main"], cwd=local_dir, check=True, capture_output=True)
            subprocess.run(["git", "add", "."], cwd=local_dir, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=local_dir, check=True)
            subprocess.run(["git", "push", "-u", "origin", "main"], cwd=local_dir, check=True, capture_output=True)

            # Add secret commit
            (local_dir / "secret.py").write_text(f"{_VAR_GH} = '{_DUMMY_GITHUB}'\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=local_dir, check=True)
            subprocess.run(["git", "commit", "-m", "secret commit"], cwd=local_dir, check=True)

            local_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=local_dir, text=True).strip()
            remote_sha = subprocess.check_output(["git", "rev-parse", "origin/main"], cwd=local_dir, text=True).strip()

            stdin_input = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
            res = scan_pre_push(local_dir, "origin", str(remote_dir), stdin_content=stdin_input)
            self.assertFalse(res.is_clean)
            self.assertEqual(len(res.findings), 1)
            self.assertEqual(res.findings[0].rule, "GitHub Personal Access Token")

    def test_scan_pre_push_historical_findings_outside_range_do_not_block(self):
        from security.git_guard import scan_pre_push
        zero_sha = "0000000000000000000000000000000000000000"
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            remote_dir = tmp_path / "remote.git"
            local_dir = tmp_path / "local"

            subprocess.run(["git", "init", "--bare", str(remote_dir)], check=True, capture_output=True)
            subprocess.run(["git", "init", str(local_dir)], check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=local_dir, check=True)
            subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=local_dir, check=True)
            subprocess.run(["git", "remote", "add", "origin", str(remote_dir)], cwd=local_dir, check=True)

            (local_dir / "README.md").write_text("# Repo\n", encoding="utf-8")
            subprocess.run(["git", "checkout", "-B", "main"], cwd=local_dir, check=True, capture_output=True)
            subprocess.run(["git", "add", "."], cwd=local_dir, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=local_dir, check=True)
            subprocess.run(["git", "push", "-u", "origin", "main"], cwd=local_dir, check=True, capture_output=True)

            # Push a historical fixture commit to origin/main
            (local_dir / "historical.py").write_text(f"{_VAR_GH} = '{_DUMMY_GITHUB}'\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=local_dir, check=True)
            subprocess.run(["git", "commit", "-m", "historical secret"], cwd=local_dir, check=True)
            subprocess.run(["git", "push", "origin", "main"], cwd=local_dir, check=True, capture_output=True)

            # Create clean feature branch
            subprocess.run(["git", "checkout", "-b", "feat/clean-branch"], cwd=local_dir, check=True)
            (local_dir / "feature.py").write_text("def run(): pass\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=local_dir, check=True)
            subprocess.run(["git", "commit", "-m", "clean feature commit"], cwd=local_dir, check=True)

            local_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=local_dir, text=True).strip()
            stdin_input = f"refs/heads/feat/clean-branch {local_sha} refs/heads/feat/clean-branch {zero_sha}\n"

            res = scan_pre_push(local_dir, "origin", str(remote_dir), stdin_content=stdin_input)
            self.assertTrue(res.is_clean)
            self.assertEqual(len(res.findings), 0)

    def test_scan_pre_push_deletion_and_errors_fail_closed(self):
        from security.git_guard import scan_pre_push
        zero_sha = "0000000000000000000000000000000000000000"
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            remote_dir = tmp_path / "remote.git"
            local_dir = tmp_path / "local"

            subprocess.run(["git", "init", "--bare", str(remote_dir)], check=True, capture_output=True)
            subprocess.run(["git", "init", str(local_dir)], check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=local_dir, check=True)
            subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=local_dir, check=True)
            subprocess.run(["git", "remote", "add", "origin", str(remote_dir)], cwd=local_dir, check=True)

            (local_dir / "README.md").write_text("# Repo\n", encoding="utf-8")
            subprocess.run(["git", "checkout", "-B", "main"], cwd=local_dir, check=True, capture_output=True)
            subprocess.run(["git", "add", "."], cwd=local_dir, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=local_dir, check=True)
            subprocess.run(["git", "push", "-u", "origin", "main"], cwd=local_dir, check=True, capture_output=True)

            # Deletion: local sha is zero -> clean
            remote_sha = subprocess.check_output(["git", "rev-parse", "origin/main"], cwd=local_dir, text=True).strip()
            stdin_del = f"(delete) {zero_sha} refs/heads/main {remote_sha}\n"
            res_del = scan_pre_push(local_dir, "origin", str(remote_dir), stdin_content=stdin_del)
            self.assertTrue(res_del.is_clean)

            # Invalid sha: must fail closed
            stdin_err = f"refs/heads/main deadbeefdeadbeefdeadbeefdeadbeefdeadbeef refs/heads/main {remote_sha}\n"
            res_err = scan_pre_push(local_dir, "origin", str(remote_dir), stdin_content=stdin_err)
            self.assertFalse(res_err.is_clean)
            self.assertTrue(any("Error" in f.rule for f in res_err.findings))

    def test_pre_push_zero_leakage_in_terminal_output(self):
        import sys
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            remote_dir = tmp_path / "remote.git"
            local_dir = tmp_path / "local"

            subprocess.run(["git", "init", "--bare", str(remote_dir)], check=True, capture_output=True)
            subprocess.run(["git", "init", str(local_dir)], check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=local_dir, check=True)
            subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=local_dir, check=True)
            subprocess.run(["git", "remote", "add", "origin", str(remote_dir)], cwd=local_dir, check=True)

            (local_dir / "README.md").write_text("# Repo\n", encoding="utf-8")
            subprocess.run(["git", "checkout", "-B", "main"], cwd=local_dir, check=True, capture_output=True)
            subprocess.run(["git", "add", "."], cwd=local_dir, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=local_dir, check=True)
            subprocess.run(["git", "push", "-u", "origin", "main"], cwd=local_dir, check=True, capture_output=True)

            (local_dir / "secret.py").write_text(f"{_VAR_GH} = '{_DUMMY_GITHUB}'\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=local_dir, check=True)
            subprocess.run(["git", "commit", "-m", "leak"], cwd=local_dir, check=True)

            local_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=local_dir, text=True).strip()
            remote_sha = subprocess.check_output(["git", "rev-parse", "origin/main"], cwd=local_dir, text=True).strip()
            stdin_input = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"

            guard_script = str(Path(__file__).resolve().parent.parent / "git_guard.py")
            proc = subprocess.run(
                [sys.executable, guard_script, "--pre-push", "origin", str(remote_dir)],
                cwd=local_dir,
                input=stdin_input,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(proc.returncode, 1)
            output = proc.stdout + proc.stderr
            self.assertNotIn(_DUMMY_GITHUB, output)
            self.assertTrue("ghp_...3456" in output or "ghp_..." in output)

    def test_scan_pre_push_merge_commit_with_secret(self):
        from security.git_guard import scan_pre_push
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            remote_dir = tmp_path / "remote.git"
            local_dir = tmp_path / "local"

            subprocess.run(["git", "init", "--bare", str(remote_dir)], check=True, capture_output=True)
            subprocess.run(["git", "init", str(local_dir)], check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=local_dir, check=True)
            subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=local_dir, check=True)
            subprocess.run(["git", "remote", "add", "origin", str(remote_dir)], cwd=local_dir, check=True)

            # Base commit
            (local_dir / "file.txt").write_text("line 1\n", encoding="utf-8")
            subprocess.run(["git", "checkout", "-B", "main"], cwd=local_dir, check=True, capture_output=True)
            subprocess.run(["git", "add", "."], cwd=local_dir, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=local_dir, check=True)
            subprocess.run(["git", "push", "-u", "origin", "main"], cwd=local_dir, check=True, capture_output=True)

            # Branch A
            subprocess.run(["git", "checkout", "-b", "branch-a"], cwd=local_dir, check=True)
            (local_dir / "file.txt").write_text("line 1\nfrom branch a\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=local_dir, check=True)
            subprocess.run(["git", "commit", "-m", "commit-a"], cwd=local_dir, check=True)

            # Branch B
            subprocess.run(["git", "checkout", "main"], cwd=local_dir, check=True)
            subprocess.run(["git", "checkout", "-b", "branch-b"], cwd=local_dir, check=True)
            (local_dir / "file.txt").write_text("line 1\nfrom branch b\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=local_dir, check=True)
            subprocess.run(["git", "commit", "-m", "commit-b"], cwd=local_dir, check=True)
            subprocess.run(["git", "push", "-u", "origin", "branch-b"], cwd=local_dir, check=True, capture_output=True)
            remote_sha = subprocess.check_output(["git", "rev-parse", "origin/branch-b"], cwd=local_dir, text=True).strip()

            # Merge with conflict, resolved with secret
            subprocess.run(["git", "merge", "branch-a"], cwd=local_dir, capture_output=True)
            (local_dir / "file.txt").write_text(f"line 1\nSECRET = '{_DUMMY_GITHUB}'\n", encoding="utf-8")
            subprocess.run(["git", "add", "file.txt"], cwd=local_dir, check=True)
            subprocess.run(["git", "commit", "-m", "resolved merge with secret"], cwd=local_dir, check=True)
            local_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=local_dir, text=True).strip()

            # Push branch-b
            stdin_input = f"refs/heads/branch-b {local_sha} refs/heads/branch-b {remote_sha}\n"
            res = scan_pre_push(local_dir, "origin", str(remote_dir), stdin_content=stdin_input)
            self.assertFalse(res.is_clean)
            self.assertEqual(len(res.findings), 1)
            self.assertEqual(res.findings[0].rule, "GitHub Personal Access Token")

    def test_scan_pre_push_multiple_refs_in_single_push(self):
        from security.git_guard import scan_pre_push
        zero_sha = "0000000000000000000000000000000000000000"
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            remote_dir = tmp_path / "remote.git"
            local_dir = tmp_path / "local"

            subprocess.run(["git", "init", "--bare", str(remote_dir)], check=True, capture_output=True)
            subprocess.run(["git", "init", str(local_dir)], check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=local_dir, check=True)
            subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=local_dir, check=True)
            subprocess.run(["git", "remote", "add", "origin", str(remote_dir)], cwd=local_dir, check=True)

            # Base commit on main
            (local_dir / "README.md").write_text("# Repo\n", encoding="utf-8")
            subprocess.run(["git", "checkout", "-B", "main"], cwd=local_dir, check=True, capture_output=True)
            subprocess.run(["git", "add", "."], cwd=local_dir, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=local_dir, check=True)
            subprocess.run(["git", "push", "-u", "origin", "main"], cwd=local_dir, check=True, capture_output=True)
            main_sha = subprocess.check_output(["git", "rev-parse", "origin/main"], cwd=local_dir, text=True).strip()

            # Ref 1: Clean feature branch
            subprocess.run(["git", "checkout", "-b", "feat/clean"], cwd=local_dir, check=True)
            (local_dir / "clean.py").write_text("print('clean')\n", encoding="utf-8")
            subprocess.run(["git", "add", "clean.py"], cwd=local_dir, check=True)
            subprocess.run(["git", "commit", "-m", "clean feat"], cwd=local_dir, check=True)
            clean_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=local_dir, text=True).strip()

            # Ref 2: Branch to delete
            subprocess.run(["git", "checkout", "-b", "feat/to-delete"], cwd=local_dir, check=True)
            (local_dir / "temp.txt").write_text("temp\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=local_dir, check=True)
            subprocess.run(["git", "commit", "-m", "temp"], cwd=local_dir, check=True)
            subprocess.run(["git", "push", "-u", "origin", "feat/to-delete"], cwd=local_dir, check=True, capture_output=True)
            del_sha = subprocess.check_output(["git", "rev-parse", "origin/feat/to-delete"], cwd=local_dir, text=True).strip()

            # Ref 3: Leaky feature branch
            subprocess.run(["git", "checkout", "main"], cwd=local_dir, check=True)
            subprocess.run(["git", "checkout", "-b", "feat/leaky"], cwd=local_dir, check=True)
            (local_dir / "leak.py").write_text(f"{_VAR_GH} = '{_DUMMY_GITHUB}'\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=local_dir, check=True)
            subprocess.run(["git", "commit", "-m", "leak commit"], cwd=local_dir, check=True)
            leak_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=local_dir, text=True).strip()

            # Case A: Multi-ref push where all pushed refs are clean (main up-to-date, feat/clean new, feat/to-delete deleted)
            stdin_all_clean = (
                f"refs/heads/main {main_sha} refs/heads/main {main_sha}\n"
                f"refs/heads/feat/clean {clean_sha} refs/heads/feat/clean {zero_sha}\n"
                f"(delete) {zero_sha} refs/heads/feat/to-delete {del_sha}\n"
            )
            res_clean = scan_pre_push(local_dir, "origin", str(remote_dir), stdin_content=stdin_all_clean)
            self.assertTrue(res_clean.is_clean)
            self.assertEqual(len(res_clean.findings), 0)

            # Case B: Multi-ref push including the leaky ref
            stdin_mixed = stdin_all_clean + f"refs/heads/feat/leaky {leak_sha} refs/heads/feat/leaky {zero_sha}\n"
            res_mixed = scan_pre_push(local_dir, "origin", str(remote_dir), stdin_content=stdin_mixed)
            self.assertFalse(res_mixed.is_clean)
            self.assertEqual(len(res_mixed.findings), 1)
            self.assertEqual(res_mixed.findings[0].rule, "GitHub Personal Access Token")


if __name__ == "__main__":
    unittest.main()
