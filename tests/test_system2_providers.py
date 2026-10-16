"""Unit tests for System 2 multi-provider support (Antigravity Google Auth + Claude)."""
import os
import unittest
from unittest.mock import MagicMock, patch

from system2 import (
    AntigravityOptInRequiredError,
    AntigravitySystem2Client,
    ClaudeSystem2Client,
    OPT_IN_ENV_VAR,
    System2Client,
    find_antigravity_bin,
    get_system2_provider,
    validate_system2_provider_preflight,
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

        client = AntigravitySystem2Client(model="gemini-3.8-flash-high", allow_unrestricted=True)
        resp = client._call("Be helpful.", "Hello")
        self.assertEqual(resp, "hello from gemini")
        self.assertTrue(mock_popen.called)

        # Verify command flags include --disable-slash-commands and --sandbox, NOT --dangerously-skip-permissions
        call_args, call_kwargs = mock_popen.call_args
        cmd = call_args[0]
        self.assertIn("--disable-slash-commands", cmd)
        self.assertIn("--sandbox", cmd)
        self.assertNotIn("--dangerously-skip-permissions", cmd)

        # Verify prompt includes dual anti-tool sandwich guard
        communicate_call = mock_proc.communicate.call_args
        sent_input = communicate_call[1].get("input", "")
        self.assertIn("CRITICAL INSTRUCTION: You are operating strictly as a stateless, non-interactive JSON generator", sent_input)
        self.assertIn("[CRITICAL FINAL CONSTRAINT]", sent_input)
        self.assertIn("REMINDER: ABSOLUTELY DO NOT CALL ANY TOOLS OR COMMANDS", sent_input)

        # Verify default timeout is 240s
        self.assertEqual(communicate_call[1].get("timeout"), 240.0)

    def test_antigravity_fails_closed_by_default_without_opt_in(self):
        from system2.antigravity_client import AntigravityOptInRequiredError

        client = AntigravitySystem2Client(model="gemini-3.8-flash-high")
        with self.assertRaises(AntigravityOptInRequiredError) as ctx:
            client._call("system", "user")
        self.assertIn("invocation failed closed", str(ctx.exception))
        self.assertIn("does not provide a technically enforced restricted tool execution boundary", str(ctx.exception))

    @patch("subprocess.Popen")
    def test_antigravity_succeeds_with_opt_in_env_var(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (
            '{"status": "SUCCESS", "response": "ok", "usage": {}}',
            "",
        )
        mock_popen.return_value = mock_proc

        with patch.dict(os.environ, {"BRAINFROG_ALLOW_UNRESTRICTED_ANTIGRAVITY_CLI": "1"}):
            client = AntigravitySystem2Client(model="gemini-3.8-flash-high")
            resp = client._call("system", "user")
            self.assertEqual(resp, "ok")

    @patch("subprocess.Popen")
    def test_antigravity_rejects_malformed_json_fails_closed(self, mock_popen):
        from system2.antigravity_client import AntigravityOutputError

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (
            "NOT_VALID_JSON_OUTPUT_AT_ALL",
            "",
        )
        mock_popen.return_value = mock_proc

        client = AntigravitySystem2Client(model="gemini-3.8-flash-high", allow_unrestricted=True)
        with self.assertRaises(AntigravityOutputError) as ctx:
            client._call("system", "user")
        self.assertIn("malformed JSON output", str(ctx.exception))

    @patch("subprocess.Popen")
    def test_antigravity_rejects_empty_output(self, mock_popen):
        from system2.antigravity_client import AntigravityOutputError

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = ("", "")
        mock_popen.return_value = mock_proc

        client = AntigravitySystem2Client(model="gemini-3.8-flash-high", allow_unrestricted=True)
        with self.assertRaises(AntigravityOutputError) as ctx:
            client._call("system", "user")
        self.assertIn("produced empty output", str(ctx.exception))

    @patch("subprocess.Popen")
    def test_antigravity_rejects_unexpected_schema(self, mock_popen):
        from system2.antigravity_client import AntigravityOutputError

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = ('[{"status": "SUCCESS"}]', "")
        mock_popen.return_value = mock_proc

        client = AntigravitySystem2Client(model="gemini-3.8-flash-high", allow_unrestricted=True)
        with self.assertRaises(AntigravityOutputError) as ctx:
            client._call("system", "user")
        self.assertIn("non-dictionary JSON", str(ctx.exception))

    @patch("subprocess.Popen")
    def test_antigravity_timeout_raises_typed_error(self, mock_popen):
        import subprocess
        from system2.antigravity_client import AntigravityTimeoutError

        mock_proc = MagicMock()
        mock_proc.communicate.side_effect = subprocess.TimeoutExpired(cmd=["agy"], timeout=10.0)
        mock_popen.return_value = mock_proc

        client = AntigravitySystem2Client(model="gemini-3.8-flash-high", allow_unrestricted=True)
        with self.assertRaises(AntigravityTimeoutError) as ctx:
            client._call("system", "user")
        self.assertIn("timed out after", str(ctx.exception))

    @patch("subprocess.Popen")
    def test_antigravity_rejects_tool_calls_in_response(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (
            '{"status": "SUCCESS", "response": "", "tool_calls": [{"name": "run_command", "args": {}}]}',
            "",
        )
        mock_popen.return_value = mock_proc

        client = AntigravitySystem2Client(model="gemini-3.8-flash-high", allow_unrestricted=True)
        with self.assertRaises(RuntimeError) as ctx:
            client._call("system", "user")
        self.assertIn("unauthorized tool calls", str(ctx.exception))

    @patch("subprocess.Popen")
    def test_antigravity_rejects_non_success_status(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (
            '{"status": "ERROR", "response": "failure occurred"}',
            "",
        )
        mock_popen.return_value = mock_proc

        client = AntigravitySystem2Client(model="gemini-3.8-flash-high", allow_unrestricted=True)
        with self.assertRaises(RuntimeError) as ctx:
            client._call("system", "user")
        self.assertIn("non-success status 'ERROR'", str(ctx.exception))

    @patch("subprocess.Popen")
    def test_antigravity_timeout_env_override(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (
            '{"status": "SUCCESS", "response": "ok", "usage": {}}',
            "",
        )
        mock_popen.return_value = mock_proc

        client = AntigravitySystem2Client(model="gemini-3.8-flash-high", allow_unrestricted=True)
        with patch.dict(os.environ, {"ANTIGRAVITY_TIMEOUT": "350"}):
            client._call("system", "user")

        communicate_call = mock_proc.communicate.call_args
        self.assertEqual(communicate_call[1].get("timeout"), 350.0)

    def test_auto_detection_does_not_select_antigravity_without_opt_in(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "", "BRAINFROG_ALLOW_UNRESTRICTED_ANTIGRAVITY_CLI": ""}, clear=True):
            with patch("system2.find_antigravity_bin", return_value="C:\\fake\\agy.exe"):
                prov = get_system2_provider()
                self.assertEqual(prov, "claude")

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

    def test_validate_system2_provider_preflight_antigravity_blocked_without_opt_in(self):
        with patch.dict(os.environ, {OPT_IN_ENV_VAR: ""}, clear=False):
            with patch("system2.find_antigravity_bin", return_value="C:\\fake\\agy.exe"), \
                 patch("os.path.exists", return_value=True):
                with self.assertRaises(AntigravityOptInRequiredError) as ctx:
                    validate_system2_provider_preflight("antigravity")
                self.assertIn("invocation failed closed", str(ctx.exception))
                self.assertIn("does not provide a technically enforced restricted tool execution boundary", str(ctx.exception))

    def test_validate_system2_provider_preflight_antigravity_allowed_with_opt_in(self):
        with patch.dict(os.environ, {OPT_IN_ENV_VAR: "1"}, clear=False):
            with patch("system2.find_antigravity_bin", return_value="C:\\fake\\agy.exe"), \
                 patch("os.path.exists", return_value=True):
                result = validate_system2_provider_preflight("antigravity")
                self.assertEqual(result, "antigravity")

    def test_validate_system2_provider_preflight_antigravity_missing_binary(self):
        with patch("system2.find_antigravity_bin", return_value=None):
            with self.assertRaises(FileNotFoundError):
                validate_system2_provider_preflight("antigravity")

    def test_validate_system2_provider_preflight_non_antigravity_passthrough(self):
        self.assertEqual(validate_system2_provider_preflight("claude"), "claude")
        self.assertEqual(validate_system2_provider_preflight("openai"), "openai")
        self.assertEqual(validate_system2_provider_preflight("openrouter"), "openrouter")

    def test_claude_credentials_do_not_silently_override_explicit_antigravity(self):
        """Explicit Antigravity configuration fails closed even when ANTHROPIC_API_KEY is available."""
        with patch.dict(os.environ, {"SYSTEM2_PROVIDER": "antigravity", "ANTHROPIC_API_KEY": "sk-ant-test-key", OPT_IN_ENV_VAR: ""}, clear=False):
            with patch("system2.find_antigravity_bin", return_value="C:\\fake\\agy.exe"), \
                 patch("os.path.exists", return_value=True):
                prov = get_system2_provider()
                self.assertEqual(prov, "antigravity")
                with self.assertRaises(AntigravityOptInRequiredError):
                    validate_system2_provider_preflight(prov)

    def test_explicit_antigravity_without_opt_in_rejected_before_workflow_side_effects(self):
        """cli.execute_task rejects un-opted Antigravity before reservation or workspace locking."""
        import tempfile
        from pathlib import Path
        import cli

        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_path = Path(tmp_dir)
            with patch.dict(os.environ, {OPT_IN_ENV_VAR: ""}, clear=False):
                with patch("core.runtime.TaskReservationCoordinator") as mock_coord, \
                     patch("core.runtime.BrainFrogRuntime") as mock_runtime, \
                     patch("system2.find_antigravity_bin", return_value="C:\\fake\\agy.exe"), \
                     patch("os.path.exists", return_value=True):
                    exit_code = cli.execute_task(
                        task="update something",
                        repo_dir=repo_path,
                        provider="antigravity",
                    )
                    self.assertEqual(exit_code, 1)
                    mock_coord.assert_not_called()
                    mock_runtime.assert_not_called()

    def test_execute_autonomous_task_preflight_rejects_before_locking(self):
        """execute_autonomous_task raises AntigravityOptInRequiredError before WorkspaceLock acquisition."""
        import tempfile
        from pathlib import Path
        from core.runtime.workflow import TaskRequest, execute_autonomous_task

        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_path = Path(tmp_dir)
            mock_runtime = MagicMock()
            mock_runtime.default_provider = "antigravity"
            req = TaskRequest(
                task_id="task_test_preflight",
                user_request="fix dental landing page",
                workspace=repo_path,
                metadata={"provider": "antigravity"},
            )

            with patch.dict(os.environ, {OPT_IN_ENV_VAR: ""}, clear=False):
                with patch("system2.find_antigravity_bin", return_value="C:\\fake\\agy.exe"), \
                     patch("os.path.exists", return_value=True), \
                     patch("core.runtime.workflow.WorkspaceLock") as mock_lock, \
                     patch("core.runtime.workflow.sample_git_state") as mock_sample:
                    with self.assertRaises(AntigravityOptInRequiredError):
                        execute_autonomous_task(mock_runtime, req)
                    mock_lock.assert_not_called()
                    mock_sample.assert_not_called()


if __name__ == "__main__":
    unittest.main()
