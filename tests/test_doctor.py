"""Unit tests for BrainFrog Doctor diagnostics and zero secret exposure."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from core.runtime.doctor import redact_secret, run_doctor_diagnostics


class TestBrainFrogDoctor(unittest.TestCase):
    def test_redact_secret_utility(self):
        self.assertEqual(redact_secret(None), "[NOT CONFIGURED]")
        self.assertEqual(redact_secret(""), "[NOT CONFIGURED]")
        self.assertEqual(redact_secret("short"), "[CONFIGURED: ***]")
        
        long_secret = "sk-ant-api03-1234567890abcdef"
        redacted = redact_secret(long_secret)
        self.assertEqual(redacted, "sk-a...cdef")
        self.assertNotIn("1234567890", redacted)

    def test_doctor_diagnostics_coverage(self):
        repo_dir = Path.cwd()
        checks = run_doctor_diagnostics(repo_dir)

        self.assertTrue(len(checks) >= 8)
        categories = {c.category for c in checks}
        self.assertIn("Workspace", categories)
        self.assertIn("System 1", categories)
        self.assertIn("System 2", categories)
        self.assertIn("Memory", categories)
        self.assertIn("Skills", categories)
        self.assertIn("Channels", categories)
        self.assertIn("Security", categories)

    def test_zero_secret_leakage_in_diagnostics(self):
        fake_env = {
            "TYPESAFE_API_KEY": "ts-live-secret-key-123456789",
            "ANTHROPIC_API_KEY": "sk-ant-api-key-987654321",
            "TELEGRAM_BOT_TOKEN": "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ",
            "OPENAI_API_KEY": "sk-proj-super-secret-openai-token",
        }
        with patch.dict(os.environ, fake_env):
            checks = run_doctor_diagnostics(Path.cwd())
            for c in checks:
                # Sensitive substrings must never appear raw in details
                self.assertNotIn("secret-key-123456789", c.detail)
                self.assertNotIn("key-987654321", c.detail)
                self.assertNotIn("ABCdefGHIjklMNOpqrSTUvwxYZ", c.detail)
                self.assertNotIn("super-secret-openai", c.detail)

    def test_show_doctor_cli_helper(self):
        from cli import show_doctor
        exit_code = show_doctor(Path.cwd())
        self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
