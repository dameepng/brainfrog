"""Unit tests for automated PR Proof screenshot generation and frontend change detection."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.frontend_quality_gate import QualityGateResult, is_frontend_change
from core.pr_proof import (
    attach_pr_proof_to_body,
    format_pr_proof_markdown,
    get_github_repo_info,
    get_pr_changed_files,
    publish_proof_to_assets_branch,
)
from orchestrator import Orchestrator, RunConfig, StepResult
from system2 import PlanStep


class TestFrontendChangeDetection(unittest.TestCase):
    """Test deterministic frontend vs backend file detection."""

    def test_frontend_files_detected(self):
        self.assertTrue(is_frontend_change(["index.html"]))
        self.assertTrue(is_frontend_change(["style.css"]))
        self.assertTrue(is_frontend_change(["src/App.jsx"]))
        self.assertTrue(is_frontend_change(["components/Button.tsx"]))
        self.assertTrue(is_frontend_change(["views/Home.vue"]))
        self.assertTrue(is_frontend_change(["src/styles/theme.scss"]))
        self.assertTrue(is_frontend_change(["src/components/Modal.svelte"]))
        self.assertTrue(is_frontend_change(["assets/main.less"]))

    def test_backend_only_files_skipped(self):
        self.assertFalse(is_frontend_change(["calc.py", "main.py"]))
        self.assertFalse(is_frontend_change(["server.go", "api.rs", "App.java"]))
        self.assertFalse(is_frontend_change(["orchestrator.py", "core/frontend_quality_gate.py"]))
        self.assertFalse(is_frontend_change(["backend/views.py"]))
        self.assertFalse(is_frontend_change(["tests/test_ui.py"]))

    def test_documentation_and_config_skipped(self):
        self.assertFalse(is_frontend_change(["README.md"]))
        self.assertFalse(is_frontend_change(["docs/frontend-migration-plan.md"]))
        self.assertFalse(is_frontend_change(["documentation/ui-architecture.md"]))
        self.assertFalse(is_frontend_change(["pyproject.toml", "package.json", "tsconfig.json"]))
        self.assertFalse(is_frontend_change([".github/workflows/ci.yml"]))

    def test_ignored_directories_skipped(self):
        self.assertFalse(is_frontend_change(["node_modules/react/index.tsx"]))
        self.assertFalse(is_frontend_change(["node_modules/@types/react/index.d.ts"]))
        self.assertFalse(is_frontend_change(["dist/bundle.css"]))
        self.assertFalse(is_frontend_change(["build/static/main.js"]))
        self.assertFalse(is_frontend_change([".brainfrog/scratch/preview.png"]))

    def test_mixed_frontend_and_backend_detected(self):
        # Edge case: PR that touches both frontend AND backend must be detected
        self.assertTrue(is_frontend_change(["backend/api.py", "src/App.tsx"]))
        self.assertTrue(is_frontend_change(["calc.py", "index.html", "README.md"]))

    def test_new_frontend_files_detected(self):
        # Edge case: newly added frontend file
        self.assertTrue(is_frontend_change(["components/Navbar.tsx"]))
        self.assertTrue(is_frontend_change(["new_folder/Banner.vue"]))

    def test_pure_css_style_change_detected(self):
        # Edge case: pure styling change without logic
        self.assertTrue(is_frontend_change(["styles/custom.css"]))
        self.assertTrue(is_frontend_change(["public/theme.scss"]))


class TestPrProofGeneration(unittest.TestCase):
    """Test PR proof screenshot generation, Markdown formatting, and body updates."""

    def test_format_pr_proof_markdown(self):
        md = format_pr_proof_markdown(
            owner="dameepng",
            repo="brainfrog",
            commit_sha="a1b2c3d4e5f678901234567890abcdef12345678",
            desktop_rel="pr-8/123456789/desktop.png",
            mobile_rel="pr-8/123456789/mobile.png",
        )
        self.assertIn("## 📸 Proof", md)
        self.assertIn("| Desktop | Mobile |", md)
        self.assertIn("https://raw.githubusercontent.com/dameepng/brainfrog/a1b2c3d4e5f678901234567890abcdef12345678/pr-8/123456789/desktop.png", md)
        self.assertIn("https://raw.githubusercontent.com/dameepng/brainfrog/a1b2c3d4e5f678901234567890abcdef12345678/pr-8/123456789/mobile.png", md)

    def test_attach_pr_proof_skips_when_no_frontend_change(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_path = Path(tmp_dir)
            body = "Fixed calculation bug in calc.py"
            new_body, attached = attach_pr_proof_to_body(
                pr_body=body,
                repo_dir=repo_path,
                branch="feat/calc-fix",
                changed_files=["calc.py", "README.md"],
            )
            self.assertFalse(attached)
            self.assertEqual(new_body, body)
            self.assertNotIn("## 📸 Proof", new_body)

    @patch("core.pr_proof.publish_proof_to_assets_branch")
    def test_attach_pr_proof_generates_proof_when_frontend_change(self, mock_publish):
        mock_publish.return_value = (
            "a1b2c3d4e5f678901234567890abcdef12345678",
            "feat-hero-banner/1700000000/desktop.png",
            "feat-hero-banner/1700000000/mobile.png",
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_path = Path(tmp_dir)
            body = "Updated landing page hero banner"

            dummy_png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
            gate_res = QualityGateResult(
                status="PASS",
                screenshot_base64=dummy_png,
                screenshot_desktop_base64=dummy_png,
                screenshot_mobile_base64=dummy_png,
            )

            new_body, attached = attach_pr_proof_to_body(
                pr_body=body,
                repo_dir=repo_path,
                branch="feat/hero-banner",
                gate_result=gate_res,
                changed_files=["src/App.tsx"],
            )

            self.assertTrue(attached)
            self.assertIn("## 📸 Proof", new_body)
            self.assertIn("| Desktop | Mobile |", new_body)
            self.assertIn("https://raw.githubusercontent.com/", new_body)
            self.assertIn("/a1b2c3d4e5f678901234567890abcdef12345678/feat-hero-banner/1700000000/desktop.png", new_body)
            self.assertIn("/a1b2c3d4e5f678901234567890abcdef12345678/feat-hero-banner/1700000000/mobile.png", new_body)
            mock_publish.assert_called_once()


class TestOrchestratorProofIntegration(unittest.TestCase):
    """Test orchestrator skips quality gate and proof on backend changes."""

    def test_orchestrator_skips_gate_on_backend_file_even_with_package_json(self):
        """Even if repo has package.json, gate MUST skip if changed files are backend-only."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_path = Path(tmp_dir)
            (repo_path / "package.json").write_text('{"name": "fullstack-app"}')
            (repo_path / "calc.py").write_text("def add(a, b): return a + b\n")

            cfg = RunConfig(
                repo_dir=repo_path,
                task="Fix add function in calc.py",
                test_command=["python", "-c", "import sys; sys.exit(0)"],
            )
            orch = Orchestrator(MagicMock(), MagicMock(), cfg)

            step = PlanStep(id="1", description="Fix add function in calc.py", files=["calc.py"])
            res = orch._run_frontend_quality_gate(step, "Fix add function in calc.py", {"calc.py": "def add..."})
            self.assertIsNone(res)

    @patch("core.pr_proof.publish_proof_to_assets_branch")
    def test_open_pr_skips_proof_for_backend_only(self, mock_publish):
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_path = Path(tmp_dir)
            cfg = RunConfig(
                repo_dir=repo_path,
                task="Update backend API",
                test_command=["python", "-c", "import sys; sys.exit(0)"],
            )
            orch = Orchestrator(MagicMock(), MagicMock(), cfg)
            step = PlanStep(id="1", description="Update API", files=["api.py"])

            pr_copy = {"title": "fix: update api", "body": "Summary of backend changes."}

            with patch("core.pr_proof.get_pr_changed_files", return_value=["api.py"]):
                # Mock git operations
                with patch("orchestrator._run") as mock_run:
                    mock_proc = MagicMock()
                    mock_proc.returncode = 0
                    mock_proc.stdout = "https://github.com/dameepng/brainfrog/pull/1"
                    mock_proc.stderr = ""
                    mock_run.return_value = mock_proc

                    orch._open_pr(step, pr_copy, gate_result=None)

                    # Proof section must NOT be present
                    self.assertNotIn("## 📸 Proof", pr_copy["body"])
                    mock_publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
