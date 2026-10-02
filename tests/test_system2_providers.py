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

        # Verify command flags include --disable-slash-commands
        call_args, call_kwargs = mock_popen.call_args
        cmd = call_args[0]
        self.assertIn("--disable-slash-commands", cmd)
        self.assertIn("--dangerously-skip-permissions", cmd)

        # Verify prompt includes dual anti-tool sandwich guard
        communicate_call = mock_proc.communicate.call_args
        sent_input = communicate_call[1].get("input", "")
        self.assertIn("CRITICAL INSTRUCTION: You are operating strictly as a stateless, non-interactive JSON generator", sent_input)
        self.assertIn("[CRITICAL FINAL CONSTRAINT]", sent_input)
        self.assertIn("REMINDER: ABSOLUTELY DO NOT CALL ANY TOOLS OR COMMANDS", sent_input)

        # Verify default timeout is 240s
        self.assertEqual(communicate_call[1].get("timeout"), 240.0)

    @patch("subprocess.Popen")
    def test_antigravity_timeout_env_override(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (
            '{"status": "SUCCESS", "response": "ok", "usage": {}}',
            "",
        )
        mock_popen.return_value = mock_proc

        client = AntigravitySystem2Client(model="gemini-3.8-flash-high")
        with patch.dict(os.environ, {"ANTIGRAVITY_TIMEOUT": "350"}):
            client._call("system", "user")

        communicate_call = mock_proc.communicate.call_args
        self.assertEqual(communicate_call[1].get("timeout"), 350.0)

    def test_model_selection_helpers(self):
        from cli import PROVIDER_MODELS, select_model_interactive, select_provider_interactive

        # Test catalog contains expected models
        self.assertIn("gemini-3.8-flash-high", [m[0] for m in PROVIDER_MODELS["antigravity"]])
        self.assertIn("gemini-3.1-pro-high", [m[0] for m in PROVIDER_MODELS["antigravity"]])
        self.assertIn("claude-sonnet-4-6", [m[0] for m in PROVIDER_MODELS["antigravity"]])

        # Test selecting by number (e.g. '1' -> gemini-3.8-flash-high)
        with patch("rich.console.Console.input", return_value="1"):
            chosen = select_model_interactive("antigravity", "gemini-3.8-flash-high")
            self.assertEqual(chosen, "gemini-3.8-flash-high")

        # Test selecting by number (e.g. '4' -> gemini-3.1-pro-high)
        with patch("rich.console.Console.input", return_value="4"):
            chosen = select_model_interactive("antigravity", "gemini-3.8-flash-high")
            self.assertEqual(chosen, "gemini-3.1-pro-high")

        # Test selecting provider by number ('1' -> antigravity, '2' -> claude)
        with patch("rich.console.Console.input", return_value="1"):
            prov = select_provider_interactive("claude")
            self.assertEqual(prov, "antigravity")

        with patch("rich.console.Console.input", return_value="2"):
            prov = select_provider_interactive("antigravity")
            self.assertEqual(prov, "claude")


if __name__ == "__main__":
    unittest.main()

