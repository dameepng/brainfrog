"""Unit and integration tests for Layer 1 MCP Client and Layer 2 Frontend Quality Gate."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.mcp_client import McpClient, McpToolResult
from core.frontend_quality_gate import (
    QualityGateResult,
    run_frontend_quality_gate,
)
from orchestrator import Orchestrator, RunConfig, StepResult
from system2 import PlanStep


class TestMcpClientGeneric(unittest.TestCase):
    """Layer 1: Generic MCP client tests."""

    def test_client_init(self):
        client = McpClient(command="node", args=["server.js"])
        self.assertEqual(client.command, "node")
        self.assertEqual(client.args, ["server.js"])
        self.assertFalse(client.is_connected)

    def test_tool_call_result_parsing(self):
        # Text block
        res_text = McpToolResult({"result": {"content": [{"type": "text", "text": "hello world"}]}})
        self.assertEqual(res_text.text, "hello world")
        self.assertIsNone(res_text.image_base64)
        self.assertFalse(res_text.is_error)

        # JSON text block
        res_json = McpToolResult({"result": {"content": [{"type": "text", "text": '{"exitCode": 0, "status": "ok"}'}]}})
        self.assertEqual(res_json.parsed_json, {"exitCode": 0, "status": "ok"})

        # Image block
        res_img = McpToolResult({"result": {"content": [{"type": "image", "data": "base64data", "mimeType": "image/png"}]}})
        self.assertEqual(res_img.image_base64, "base64data")


class TestFrontendQualityGate(unittest.TestCase):
    """Layer 2: Frontend Quality Gate domain logic tests."""

    def test_quality_gate_result_feedback_formatting(self):
        res = QualityGateResult(
            status="FAIL",
            build_output="SyntaxError: Unexpected token '<'",
            console_errors=[{"text": "Uncaught TypeError: Cannot read properties of undefined"}],
            failed_requests=[{"method": "GET", "url": "http://localhost:3000/api/missing", "status": 404}],
            reason="Build failed or console errors detected.",
        )
        self.assertFalse(res.passed)
        ctx = res.to_agent_context()

        self.assertIn("[Frontend Quality Gate Verification: FAIL]", ctx)
        self.assertIn("SyntaxError: Unexpected token", ctx)
        self.assertIn("Cannot read properties of undefined", ctx)
        self.assertIn("404", ctx)

    def test_quality_gate_result_feedback_missing_module(self):
        res = QualityGateResult(
            status="FAIL",
            build_output="Error: Cannot find module '@radix-ui/react-slot'\nRequire stack:\n- src/components/ui/button.tsx",
            reason="npm run build failed with exit code 1",
        )
        self.assertFalse(res.passed)
        ctx = res.to_agent_context()

        self.assertIn("[CRITICAL DEPENDENCY RULE]", ctx)
        self.assertIn("MISSING DEPENDENCY", ctx)
        self.assertIn("npm install", ctx)
        self.assertIn("whack-a-mole", ctx)

    def test_quality_gate_preflight_missing_script(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            res = run_frontend_quality_gate(
                project_cwd=tmp_dir,
                mcp_args=["/path/that/does/not/exist/index.js"],
            )
            self.assertEqual(res.status, "FAIL")
            self.assertIn("not found", res.reason)

    @patch("core.frontend_quality_gate.McpClient")
    def test_quality_gate_build_failure_stops_early(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        # Mock handshake
        mock_client.connect.return_value = None

        # Mock start_process (npm run build)
        mock_client.call_tool.side_effect = [
            # start_process build
            McpToolResult({"result": {"content": [{"type": "text", "text": '{"status": "started"}'}]}}),
            # read_process_output build -> exitCode 1 (error)
            McpToolResult({"result": {"content": [{"type": "text", "text": '{"exitCode": 1, "output": "Failed to compile tsx"}'}]}}),
            # stop_process in finally
            McpToolResult({"result": {"content": []}}),
            # close_instance in finally
            McpToolResult({"result": {"content": []}}),
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            (tmp_path / "package.json").write_text(
                json.dumps({"name": "test-app", "scripts": {"build": "next build", "dev": "next dev"}}),
                encoding="utf-8",
            )
            dummy_mcp = tmp_path / "dummy_mcp.js"
            dummy_mcp.write_text("// dummy", encoding="utf-8")

            res = run_frontend_quality_gate(
                project_cwd=tmp_path,
                mcp_args=[str(dummy_mcp)],
            )

            self.assertEqual(res.status, "FAIL")
            self.assertIn("npm run build failed with exit code 1", res.reason)
            self.assertIn("Failed to compile tsx", res.build_output)

            # Check cleanup was called in finally
            calls = [c[0][0] for c in mock_client.call_tool.call_args_list]
            self.assertIn("stop_process", calls)
            self.assertIn("close_instance", calls)
            mock_client.disconnect.assert_called()

    @patch("core.frontend_quality_gate.McpClient")
    def test_quality_gate_passes_cleanly(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.connect.return_value = None

        mock_client.call_tool.side_effect = [
            # 1. start_process build
            McpToolResult({"result": {"content": [{"type": "text", "text": '{"status": "started"}'}]}}),
            # 2. read_process_output build -> exitCode 0
            McpToolResult({"result": {"content": [{"type": "text", "text": '{"exitCode": 0, "output": "Compiled successfully"}'}]}}),
            # 3. start_process dev
            McpToolResult({"result": {"content": [{"type": "text", "text": '{"status": "started"}'}]}}),
            # 4. read_process_output dev check
            McpToolResult({"result": {"content": [{"type": "text", "text": '{"output": "Ready in 500ms on http://localhost:3000"}'}]}}),
            # 5. navigate dev_url
            McpToolResult({"result": {"content": [{"type": "text", "text": "OK"}]}}),
            # 6. get_console_logs -> empty list
            McpToolResult({"result": {"content": [{"type": "text", "text": "[]"}]}}),
            # 7. get_network_logs -> empty list
            McpToolResult({"result": {"content": [{"type": "text", "text": "[]"}]}}),
            # 8. screenshot
            McpToolResult({"result": {"content": [{"type": "image", "data": "dummy_base64_png", "mimeType": "image/png"}]}}),
            # 9. stop_process in finally
            McpToolResult({"result": {"content": []}}),
            # 10. close_instance in finally
            McpToolResult({"result": {"content": []}}),
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            (tmp_path / "package.json").write_text(
                json.dumps({"name": "test-app", "scripts": {"build": "next build", "dev": "next dev"}}),
                encoding="utf-8",
            )
            dummy_mcp = tmp_path / "dummy_mcp.js"
            dummy_mcp.write_text("// dummy", encoding="utf-8")

            res = run_frontend_quality_gate(
                project_cwd=tmp_path,
                mcp_args=[str(dummy_mcp)],
            )

            self.assertEqual(res.status, "PASS")
            self.assertTrue(res.passed)
            self.assertEqual(res.console_errors, [])
            self.assertEqual(res.failed_requests, [])
            self.assertEqual(res.screenshot_base64, "dummy_base64_png")


class TestOrchestratorMcpIntegration(unittest.TestCase):
    """Test orchestrator integration with frontend MCP quality gate."""

    def test_orchestrator_skips_gate_on_pure_backend_repo(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_path = Path(tmp_dir)
            (repo_path / "calc.py").write_text("def add(a, b): return a + b\n")

            cfg = RunConfig(
                repo_dir=repo_path,
                task="Fix calculator function",
                test_command=["python", "-c", "import sys; sys.exit(0)"],
            )
            orch = Orchestrator(MagicMock(), MagicMock(), cfg)

            step = PlanStep(id="1", description="Fix add function in calc.py", files=["calc.py"])
            res = orch._run_frontend_quality_gate(step, "Fix add function in calc.py", {"calc.py": "def add..."})
            self.assertIsNone(res)

    @patch("core.frontend_quality_gate.run_frontend_quality_gate")
    def test_orchestrator_triggers_gate_on_frontend_files(self, mock_run_gate):
        mock_run_gate.return_value = QualityGateResult(
            status="PASS",
            reason="Clean build and 0 errors",
            screenshot_base64="mock_shot",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_path = Path(tmp_dir)
            (repo_path / "package.json").write_text('{"name": "tea-shop"}')
            (repo_path / "src").mkdir()
            (repo_path / "src" / "App.tsx").write_text("export default function App() {}")

            # Mock MCP server file exists
            with patch("core.frontend_quality_gate.DEFAULT_MCP_SERVER_PATH", str(repo_path / "server.js")):
                (repo_path / "server.js").write_text("// dummy server")

                cfg = RunConfig(
                    repo_dir=repo_path,
                    task="Update landing page design",
                    test_command=["python", "-c", "import sys; sys.exit(0)"],
                )
                orch = Orchestrator(MagicMock(), MagicMock(), cfg)

                step = PlanStep(id="1", description="Update App.tsx", files=["src/App.tsx"])
                res = orch._run_frontend_quality_gate(step, "Update App.tsx", {"src/App.tsx": "..."})

                self.assertIsNotNone(res)
                self.assertEqual(res.status, "PASS")
                mock_run_gate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
