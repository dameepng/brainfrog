"""Unit tests for System 2 multi-provider support (Antigravity Google Auth + Claude)."""
import os
import unittest
from unittest.mock import MagicMock, patch

from system2 import (
    AntigravitySystem2Client,
    ClaudeSystem2Client,
    System2Client,
    find_antigravity_bin,
    get_system2_provider,
)


class TestSystem2Providers(unittest.TestCase):
    def test_provider_resolution(self):
        # Explicit arguments
        self.assertEqual(get_system2_provider("antigravity"), "antigravity")
        self.assertEqual(get_system2_provider("gemini"), "antigravity")
        self.assertEqual(get_system2_provider("google"), "antigravity")
        self.assertEqual(get_system2_provider("claude"), "claude")

        # Env variable overrides
        with patch.dict(os.environ, {"SYSTEM2_PROVIDER": "antigravity"}):
            self.assertEqual(get_system2_provider(), "antigravity")

        with patch.dict(os.environ, {"SYSTEM2_PROVIDER": "gemini"}):
            self.assertEqual(get_system2_provider(), "antigravity")

        with patch.dict(os.environ, {"SYSTEM2_PROVIDER": "claude"}):
            self.assertEqual(get_system2_provider(), "claude")

    def test_find_antigravity_bin(self):
        # Should discover the binary in the user's ~/.gemini/bin on this system
        binary = find_antigravity_bin()
        self.assertIsNotNone(binary)
        self.assertTrue(os.path.exists(binary))
        self.assertTrue(binary.lower().endswith("agy.exe") or binary.lower().endswith("agy"))

    def test_factory_creates_antigravity_client(self):
        client = System2Client(provider="antigravity", model="gemini-3.8-flash-high")
        self.assertIsInstance(client, AntigravitySystem2Client)
        self.assertEqual(client.model, "gemini-3.8-flash-high")
        self.assertEqual(client.provider_name, "antigravity")

    def test_factory_creates_claude_client(self):
        with patch("system2.claude_client.anthropic.Anthropic"):
            client = System2Client(provider="claude", model="claude-sonnet-5", api_key="sk-test")
            self.assertIsInstance(client, ClaudeSystem2Client)
            self.assertEqual(client.model, "claude-sonnet-5")
            self.assertEqual(client.provider_name, "claude")

    @patch("subprocess.Popen")
    def test_antigravity_call_execution(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (
            '{"status": "SUCCESS", "response": "hello from gemini", "usage": {"input_tokens": 10, "output_tokens": 20}}',
            "",
        )
        mock_popen.return_value = mock_proc

        client = AntigravitySystem2Client(model="gemini-3.8-flash-high")
        resp = client._call("Be helpful.", "Hello")
        self.assertEqual(resp, "hello from gemini")
        self.assertTrue(mock_popen.called)


if __name__ == "__main__":
    unittest.main()
