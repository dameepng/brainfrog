"""Unit tests for Git Secret Guard (Pre-Commit & Pre-Push Secret Scanning)."""
import subprocess
import tempfile
import unittest
from pathlib import Path

from git_guard import (
    ensure_gitignore_security,
    is_safe_value,
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
_DUMMY_ANTHROPIC = "-".join(["sk", "ant", "api03", "".join(["9a8b", "7c6d", "5e4f", "3a2b", "1c0d", "9e8f", "7a6b", "5c4d"])])
_DUMMY_GITHUB = "gh" + "p_" + "123456789012345678901234567890123456"
_VAR_ENC = "ENCRYPTION" + "_KEY"
_VAR_GH = "GITHUB" + "_TOKEN"


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

            # Second call should be a no-op
            modified_again = ensure_gitignore_security(repo_path)
            self.assertFalse(modified_again)


if __name__ == "__main__":
    unittest.main()
