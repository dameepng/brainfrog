"""Focused Regression Suite for Proof Inspection UX (Sprint 1.2).

Tests:
1. Valid verified proof inspection (VERIFIED, exit code 0)
2. Valid failed/rolled-back proof inspection (FAILED, exit code 3)
3. Missing proof artifact inspection (MISSING, exit code 2)
4. Malformed JSON proof artifact inspection (INVALID, exit code 1)
5. Inconsistent / contradictory proof inspection (INVALID, exit code 1)
6. Workspace disk integrity verification (MATCH -> 0, MISMATCH -> 1)
7. Secret redaction in inspection outputs (GitHub tokens, API keys, passwords)
8. Sensitive file pattern protection (.env, credentials)
9. CLI argument parsing and dispatch (--show-proof, show-proof <id>, --check-workspace)
10. Cross-platform path safety and null-byte rejection
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cli
from cli import main as cli_main, show_proof_cli, verify_proof_cli
from core.runtime.proof import (
    ChangeRecord,
    ChangesEvidence,
    FileProofStore,
    GitProvenance,
    ProcessEvidence,
    ProofArtifact,
    ProofVerdict,
    ReproducibilityEvidence,
    TaskEvidence,
    VerificationEvidence,
)
from core.runtime.proof_markdown import render_proof_markdown
from core.runtime.proof_inspection import (
    ProofInspectionResult,
    ProofInspectionState,
    format_proof_inspection_text,
    inspect_proof,
    print_proof_inspection,
)


class TestProofInspectionUX(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="test_proof_inspection_")
        self.workspace = Path(self.tmp_dir).resolve()

        # Initialize minimal git repo
        subprocess.run(["git", "init"], cwd=self.workspace, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=self.workspace, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "tester@test.com"], cwd=self.workspace, capture_output=True, check=True)
        (self.workspace / "README.md").write_text("# Repo\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.workspace, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=self.workspace, capture_output=True, check=True)
        res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.workspace, capture_output=True, text=True, check=True)
        self.commit_sha = res.stdout.strip()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _create_and_save_proof(
        self,
        task_id: str,
        verdict_status: str = "VERIFIED",
        tx_status: str = "COMMITTED",
        exit_code: int = 0,
        files: list = None,
        stdout: str = "1 passed",
        stderr: str = "",
        gate_passed: bool = True,
        description: str = "Test task description",
    ) -> ProofArtifact:
        proof_store = FileProofStore(self.workspace)
        if files is None:
            # Default changed file
            file_p = self.workspace / "src.py"
            raw_bytes = b"print('hello')\n"
            file_p.write_bytes(raw_bytes)
            h = hashlib.sha256(raw_bytes).hexdigest()
            files = [
                ChangeRecord(
                    path="src.py",
                    operation="CREATE",
                    before_sha256=None,
                    after_sha256=h,
                    byte_count_delta=len(raw_bytes),
                )
            ]

        proof = ProofArtifact(
            task=TaskEvidence(
                task_id=task_id,
                session_id="sess_001",
                description=description,
                created_at="2026-10-12T00:00:00Z",
                completed_at="2026-10-12T00:00:01Z",
                duration_ms=1000,
            ),
            verdict=ProofVerdict(
                status=verdict_status,
                reason="Verification checks completed.",
            ),
            provenance=GitProvenance(
                is_git=True,
                repo_root=self.workspace.name,
                branch="master",
                initial_head_sha=self.commit_sha,
                final_head_sha=self.commit_sha,
                commit_sha=self.commit_sha,
            ),
            verification=VerificationEvidence(
                test_command="pytest",
                exit_code=exit_code,
                stdout_summary=stdout,
                stderr_summary=stderr,
                gate_checks={
                    "workspace_scope": True,
                    "test_suite": gate_passed,
                    "transaction_integrity": True,
                    "execution_integrity": True,
                    "trust_integrity": True,
                },
                process_evidence=ProcessEvidence(
                    task_id=task_id,
                    transaction_id="tx_test",
                    command="pytest",
                    returncode=exit_code,
                    status="success" if exit_code == 0 else "failed",
                    duration_ms=1000,
                ),
            ),
            changes=ChangesEvidence(
                transaction_id="tx_test",
                status=tx_status,
                files=files,
            ),
            reproducibility=ReproducibilityEvidence(
                command="pytest",
                expected_exit_code=exit_code,
                expected_file_hashes={f.path: f.after_sha256 for f in files if f.after_sha256},
            ),
        )
        md = render_proof_markdown(proof)
        proof_store.save(proof, markdown_content=md)
        return proof

    def test_01_valid_verified_proof_inspection(self) -> None:
        """Scenario 1: Valid VERIFIED proof returns state VERIFIED and exit code 0."""
        task_id = "task_01_verified"
        self._create_and_save_proof(task_id, verdict_status="VERIFIED", tx_status="COMMITTED", exit_code=0)

        result = inspect_proof(self.workspace, task_id)
        self.assertEqual(result.state, ProofInspectionState.VERIFIED)
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.is_verified)
        self.assertIsNotNone(result.proof)
        self.assertEqual(result.proof.verdict.status, "VERIFIED")

        # Test CLI wrapper
        cli_code = show_proof_cli(self.workspace, task_id)
        self.assertEqual(cli_code, 0)

    def test_02_valid_failed_proof_inspection(self) -> None:
        """Scenario 2: Valid FAILED proof returns state FAILED and exit code 3."""
        task_id = "task_02_failed"
        self._create_and_save_proof(
            task_id,
            verdict_status="FAILED",
            tx_status="ROLLED_BACK",
            exit_code=1,
            files=[],
            gate_passed=False,
            stderr="AssertionError: 1 != 2",
        )

        result = inspect_proof(self.workspace, task_id)
        self.assertEqual(result.state, ProofInspectionState.FAILED)
        self.assertEqual(result.exit_code, 3)
        self.assertTrue(result.is_failed)
        self.assertIsNotNone(result.proof)
        self.assertEqual(result.proof.verdict.status, "FAILED")

        # Test CLI wrapper
        cli_code = show_proof_cli(self.workspace, task_id)
        self.assertEqual(cli_code, 3)

    def test_03_missing_proof_inspection(self) -> None:
        """Scenario 3: Missing proof returns state MISSING and exit code 2."""
        result = inspect_proof(self.workspace, "task_does_not_exist")
        self.assertEqual(result.state, ProofInspectionState.MISSING)
        self.assertEqual(result.exit_code, 2)
        self.assertTrue(result.is_missing)
        self.assertIsNone(result.proof)

        # Test CLI wrapper
        cli_code = show_proof_cli(self.workspace, "task_does_not_exist")
        self.assertEqual(cli_code, 2)

    def test_04_malformed_json_proof(self) -> None:
        """Scenario 4: Corrupted/malformed JSON returns state INVALID and exit code 1."""
        task_id = "task_04_malformed"
        store = FileProofStore(self.workspace)
        store.proofs_dir.mkdir(parents=True, exist_ok=True)
        json_path = store.proof_json_path(task_id)
        json_path.write_text("{ broken json: not valid ...", encoding="utf-8")

        result = inspect_proof(self.workspace, task_id)
        self.assertEqual(result.state, ProofInspectionState.INVALID)
        self.assertEqual(result.exit_code, 1)
        self.assertTrue(result.is_invalid)

        cli_code = show_proof_cli(self.workspace, task_id)
        self.assertEqual(cli_code, 1)

    def test_05_tampered_or_contradictory_proof(self) -> None:
        """Scenario 5: Self-declared VERIFIED with test exit code 1 fails closed as INVALID (code 1)."""
        task_id = "task_05_contradictory"
        # Create proof claiming VERIFIED, but with exit code 1
        proof = self._create_and_save_proof(task_id, verdict_status="VERIFIED", tx_status="COMMITTED", exit_code=1)
        # Modify on disk to force contradiction
        store = FileProofStore(self.workspace)
        data = proof.to_dict()
        data["verification"]["exit_code"] = 1
        store.proof_json_path(task_id).write_text(json.dumps(data), encoding="utf-8")

        result = inspect_proof(self.workspace, task_id)
        self.assertEqual(result.state, ProofInspectionState.INVALID)
        self.assertEqual(result.exit_code, 1)
        self.assertTrue(result.is_invalid)

    def test_06_workspace_disk_integrity_verification(self) -> None:
        """Scenario 6: Disk modification after proof generation detected by --check-workspace."""
        task_id = "task_06_integrity"
        self._create_and_save_proof(task_id, verdict_status="VERIFIED", tx_status="COMMITTED", exit_code=0)

        # A. Without --check-workspace: workspace disk is not evaluated
        res_no_check = inspect_proof(self.workspace, task_id, check_workspace=False)
        self.assertEqual(res_no_check.state, ProofInspectionState.VERIFIED)
        self.assertFalse(res_no_check.workspace_integrity_checked)

        # B. With --check-workspace when files match: PASS (exit code 0)
        res_match = inspect_proof(self.workspace, task_id, check_workspace=True)
        self.assertEqual(res_match.state, ProofInspectionState.VERIFIED)
        self.assertTrue(res_match.workspace_integrity_checked)
        self.assertTrue(res_match.workspace_integrity_passed)
        self.assertEqual(res_match.exit_code, 0)

        # C. Tamper with file on disk after proof generation:
        (self.workspace / "src.py").write_text("print('MUTATED AFTER PROOF')\n", encoding="utf-8")
        res_tampered = inspect_proof(self.workspace, task_id, check_workspace=True)
        self.assertEqual(res_tampered.state, ProofInspectionState.INVALID)
        self.assertTrue(res_tampered.workspace_integrity_checked)
        self.assertFalse(res_tampered.workspace_integrity_passed)
        self.assertEqual(res_tampered.exit_code, 1)

    def test_07_secret_redaction_in_inspection(self) -> None:
        """Scenario 7: Credentials and secret patterns in descriptions, stdout/stderr, and reasons are scrubbed."""
        task_id = "task_07_secrets"
        secret_token = "ghp_1234567890abcdef1234567890"
        secret_stdout = f"Token: {secret_token} and sk-ant-api03-abcdef1234567890"
        secret_stderr = "password: SuperSecretPassword123"
        task_desc = f"Configure deploy pipeline using {secret_token} for production release"
        self._create_and_save_proof(
            task_id,
            description=task_desc,
            stdout=secret_stdout,
            stderr=secret_stderr,
        )

        result = inspect_proof(self.workspace, task_id)
        # Verify inspection semantics and exit codes are preserved
        self.assertEqual(result.state, ProofInspectionState.VERIFIED)
        self.assertEqual(result.exit_code, 0)

        # 1. Plain-text rendering path
        text_output = format_proof_inspection_text(result)

        # Secrets must NOT appear in output
        self.assertNotIn(secret_token, text_output)
        self.assertNotIn("sk-ant-api03-abcdef1234567890", text_output)
        self.assertNotIn("SuperSecretPassword123", text_output)
        self.assertIn("[REDACTED_SECRET]", text_output)
        # Surrounding non-sensitive description remains readable
        self.assertIn("Configure deploy pipeline using", text_output)
        self.assertIn("for production release", text_output)

        # 2. Rich terminal rendering path
        import io
        from rich.console import Console

        buf = io.StringIO()
        rich_console = Console(file=buf, force_terminal=False, color_system=None, width=120)
        print_proof_inspection(result, console=rich_console)
        rich_output = buf.getvalue()

        # Secrets must NOT appear in Rich output
        self.assertNotIn(secret_token, rich_output)
        self.assertNotIn("sk-ant-api03-abcdef1234567890", rich_output)
        self.assertNotIn("SuperSecretPassword123", rich_output)
        self.assertIn("[REDACTED_SECRET]", rich_output)
        # Surrounding non-sensitive description remains readable
        self.assertIn("Configure deploy pipeline using", rich_output)
        self.assertIn("for production release", rich_output)

    def test_08_sensitive_file_protection(self) -> None:
        """Scenario 8: Sensitive files (.env, keys) are marked protected and never dumped."""
        task_id = "task_08_sensitive"
        env_file = self.workspace / ".env"
        env_file.write_text("SECRET_KEY=12345\n", encoding="utf-8")
        h = hashlib.sha256(b"SECRET_KEY=12345\n").hexdigest()

        files = [
            ChangeRecord(
                path=".env",
                operation="CREATE",
                before_sha256=None,
                after_sha256=h,
                byte_count_delta=17,
            )
        ]
        self._create_and_save_proof(task_id, files=files)

        result = inspect_proof(self.workspace, task_id)
        text_output = format_proof_inspection_text(result)

        self.assertIn(".env", text_output)
        self.assertNotIn("SECRET_KEY=12345", text_output)

    def test_09_cli_argument_dispatch(self) -> None:
        """Scenario 9: CLI positional and flag options route correctly to show_proof_cli."""
        task_id = "task_09_cli"
        self._create_and_save_proof(task_id, verdict_status="VERIFIED", tx_status="COMMITTED", exit_code=0)

        # 1. Positional command: brainfrog show-proof <task_id> -r <ws>
        with patch("sys.argv", ["brainfrog", "-r", str(self.workspace), "show-proof", task_id]):
            exit_code = cli_main()
            self.assertEqual(exit_code, 0)

        # 2. Flag command: brainfrog --show-proof <task_id> -r <ws>
        with patch("sys.argv", ["brainfrog", "-r", str(self.workspace), "--show-proof", task_id]):
            exit_code = cli_main()
            self.assertEqual(exit_code, 0)

        # 3. Positional with --check-workspace
        with patch("sys.argv", ["brainfrog", "-r", str(self.workspace), "show-proof", task_id, "--check-workspace"]):
            exit_code = cli_main()
            self.assertEqual(exit_code, 0)

        # 4. Missing target argument -> exit code 2
        with patch("sys.argv", ["brainfrog", "-r", str(self.workspace), "show-proof"]):
            exit_code = cli_main()
            self.assertEqual(exit_code, 2)

        # 5. Slash aliases: /show-proof <task_id> and bare /show-proof
        with patch("sys.argv", ["brainfrog", "-r", str(self.workspace), "/show-proof", task_id]):
            exit_code = cli_main()
            self.assertEqual(exit_code, 0)

        with patch("sys.argv", ["brainfrog", "-r", str(self.workspace), "/show-proof"]):
            exit_code = cli_main()
            self.assertEqual(exit_code, 2)

        # 6. /proof and proof aliases: /proof <task_id> and bare /proof
        with patch("sys.argv", ["brainfrog", "-r", str(self.workspace), "proof", task_id]):
            exit_code = cli_main()
            self.assertEqual(exit_code, 0)

        with patch("sys.argv", ["brainfrog", "-r", str(self.workspace), "proof"]):
            exit_code = cli_main()
            self.assertEqual(exit_code, 2)

        with patch("sys.argv", ["brainfrog", "-r", str(self.workspace), "/proof", task_id]):
            exit_code = cli_main()
            self.assertEqual(exit_code, 0)

        with patch("sys.argv", ["brainfrog", "-r", str(self.workspace), "/proof"]):
            exit_code = cli_main()
            self.assertEqual(exit_code, 2)

        # 7. Unrelated commands must not be captured by show-proof / proof prefix matching
        with patch("cli.execute_task") as mock_exec:
            mock_exec.return_value = 0
            with patch("sys.argv", ["brainfrog", "-r", str(self.workspace), "proofreading"]):
                exit_code = cli_main()
                self.assertEqual(exit_code, 0)
                mock_exec.assert_called_once()
                self.assertIn("proofreading", mock_exec.call_args[1]["task"])

        with patch("cli.execute_task") as mock_exec:
            mock_exec.return_value = 0
            with patch("sys.argv", ["brainfrog", "-r", str(self.workspace), "show-proof-extra"]):
                exit_code = cli_main()
                self.assertEqual(exit_code, 0)
                mock_exec.assert_called_once()
                self.assertIn("show-proof-extra", mock_exec.call_args[1]["task"])

    def test_10_cross_platform_path_safety_and_null_bytes(self) -> None:
        """Scenario 10: Null bytes and malformed paths are rejected with exit code 1."""
        result = inspect_proof(self.workspace, "malicious\x00task")
        self.assertEqual(result.state, ProofInspectionState.INVALID)
        self.assertEqual(result.exit_code, 1)

        result_empty = inspect_proof(self.workspace, "")
        self.assertEqual(result_empty.state, ProofInspectionState.MISSING)
        self.assertEqual(result_empty.exit_code, 2)

    def test_11_repl_proof_aliases(self) -> None:
        """Scenario 11: REPL handles /proof and /show-proof aliases without submitting them as tasks."""
        task_id = "task_11_repl"
        self._create_and_save_proof(task_id, verdict_status="VERIFIED", tx_status="COMMITTED", exit_code=0)

        # A. Bare /proof displays usage error (Missing Target), not submitted to execute_task
        with patch("prompt_toolkit.shortcuts.PromptSession", side_effect=Exception("Disabled for test")), \
             patch.object(cli.console, "input", side_effect=["/proof", "/exit"]), \
             patch("cli.print_banner_box") as mock_banner, \
             patch("cli.execute_task") as mock_exec, \
             patch("cli.ensure_git_remote"):
            cli.run_interactive(initial_repo=self.workspace)
            mock_exec.assert_not_called()
            self.assertTrue(any(c[1].get("title") == "Missing Target" for c in mock_banner.call_args_list))

        # B. Bare /show-proof displays usage error (Missing Target), not submitted to execute_task
        with patch("prompt_toolkit.shortcuts.PromptSession", side_effect=Exception("Disabled for test")), \
             patch.object(cli.console, "input", side_effect=["/show-proof", "/exit"]), \
             patch("cli.print_banner_box") as mock_banner, \
             patch("cli.execute_task") as mock_exec, \
             patch("cli.ensure_git_remote"):
            cli.run_interactive(initial_repo=self.workspace)
            mock_exec.assert_not_called()
            self.assertTrue(any(c[1].get("title") == "Missing Target" for c in mock_banner.call_args_list))

        # C. /proof <task_id> calls show_proof_cli
        with patch("prompt_toolkit.shortcuts.PromptSession", side_effect=Exception("Disabled for test")), \
             patch.object(cli.console, "input", side_effect=[f"/proof {task_id}", "/exit"]), \
             patch("cli.show_proof_cli") as mock_show, \
             patch("cli.execute_task") as mock_exec, \
             patch("cli.ensure_git_remote"):
            cli.run_interactive(initial_repo=self.workspace)
            mock_exec.assert_not_called()
            mock_show.assert_called_once_with(self.workspace, task_id, check_workspace=False)

        # D. /show-proof <task_id> calls show_proof_cli
        with patch("prompt_toolkit.shortcuts.PromptSession", side_effect=Exception("Disabled for test")), \
             patch.object(cli.console, "input", side_effect=[f"/show-proof {task_id}", "/exit"]), \
             patch("cli.show_proof_cli") as mock_show, \
             patch("cli.execute_task") as mock_exec, \
             patch("cli.ensure_git_remote"):
            cli.run_interactive(initial_repo=self.workspace)
            mock_exec.assert_not_called()
            mock_show.assert_called_once_with(self.workspace, task_id, check_workspace=False)

        # E. Unrelated commands like /proofreading or /show-proof-extra must NOT trigger show_proof_cli or Missing Target
        with patch("prompt_toolkit.shortcuts.PromptSession", side_effect=Exception("Disabled for test")), \
             patch.object(cli.console, "input", side_effect=["/proofreading", "/exit"]), \
             patch("cli.show_proof_cli") as mock_show, \
             patch("cli.print_banner_box") as mock_banner, \
             patch("cli.execute_task") as mock_exec, \
             patch("cli.ensure_git_remote"):
            cli.run_interactive(initial_repo=self.workspace)
            mock_show.assert_not_called()
            self.assertFalse(any(c[1].get("title") == "Missing Target" for c in mock_banner.call_args_list))

class TestVerifyProofCLISemantics(TestProofInspectionUX):
    """Regression tests for Sprint 1.4B: verify-proof verdict semantics."""

    def test_verify_proof_cli_verified_success(self) -> None:
        """1. Verified successful proof -> exit 0."""
        task_id = "task_verify_01_success"
        self._create_and_save_proof(
            task_id,
            verdict_status="VERIFIED",
            tx_status="COMMITTED",
            exit_code=0,
            gate_passed=True,
        )
        code = verify_proof_cli(self.workspace, task_id)
        self.assertEqual(code, 0)

        # Also via cli_main positional and flag
        with patch("sys.argv", ["brainfrog", "-r", str(self.workspace), "verify-proof", task_id]):
            self.assertEqual(cli_main(), 0)
        with patch("sys.argv", ["brainfrog", "-r", str(self.workspace), "--verify-proof", task_id]):
            self.assertEqual(cli_main(), 0)

    def test_verify_proof_cli_structurally_valid_failed_proof(self) -> None:
        """2. Structurally valid FAILED proof -> nonzero exit (3, matching show-proof FAILED)."""
        task_id = "task_verify_02_failed"
        self._create_and_save_proof(
            task_id,
            verdict_status="FAILED",
            tx_status="ROLLED_BACK",
            exit_code=1,
            gate_passed=False,
            stdout="0 passed, 1 failed",
        )
        code = verify_proof_cli(self.workspace, task_id)
        self.assertEqual(code, 3)

        # Also via cli_main
        with patch("sys.argv", ["brainfrog", "-r", str(self.workspace), "verify-proof", task_id]):
            self.assertEqual(cli_main(), 3)
        with patch("sys.argv", ["brainfrog", "-r", str(self.workspace), "--verify-proof", task_id]):
            self.assertEqual(cli_main(), 3)

    def test_verify_proof_cli_tampered_workspace_disk(self) -> None:
        """3. Tampered workspace disk -> exit 1 (INVALID)."""
        task_id = "task_verify_03_tampered"
        self._create_and_save_proof(
            task_id,
            verdict_status="VERIFIED",
            tx_status="COMMITTED",
            exit_code=0,
        )
        # Mutate file on disk
        (self.workspace / "src.py").write_text("tampered content\n", encoding="utf-8")
        code = verify_proof_cli(self.workspace, task_id, check_workspace=True)
        self.assertEqual(code, 1)

    def test_verify_proof_cli_incomplete_missing_proof(self) -> None:
        """4. Incomplete / missing proof -> exit 2 (INCOMPLETE / MISSING)."""
        code = verify_proof_cli(self.workspace, "nonexistent_task_id")
        self.assertEqual(code, 2)

    def test_verify_proof_cli_malformed_json(self) -> None:
        """5. Malformed JSON proof -> exit 1 (INVALID)."""
        proofs_dir = self.workspace / ".brainfrog" / "proofs"
        proofs_dir.mkdir(parents=True, exist_ok=True)
        (proofs_dir / "bad_syntax.json").write_text("{malformed: json", encoding="utf-8")

        code = verify_proof_cli(self.workspace, "bad_syntax")
        self.assertEqual(code, 1)

    def test_verify_proof_cli_unknown_verdict_fails_closed(self) -> None:
        """6. Unknown recorded task verdict -> fail closed with exit 1 (INVALID)."""
        task_id = "task_verify_06_unknown"
        self._create_and_save_proof(
            task_id,
            verdict_status="VERIFIED",
            tx_status="COMMITTED",
            exit_code=0,
        )
        proof_file = self.workspace / ".brainfrog" / "proofs" / f"{task_id}.json"
        data = json.loads(proof_file.read_text(encoding="utf-8"))
        data["verdict"]["status"] = "SURPRISE_SUCCESS"
        proof_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

        code = verify_proof_cli(self.workspace, task_id)
        self.assertEqual(code, 1)

    def test_verify_proof_cli_missing_verdict_fails_closed(self) -> None:
        """7. Missing recorded task verdict -> fail closed with exit 1 (INVALID)."""
        task_id = "task_verify_07_empty_verdict"
        self._create_and_save_proof(
            task_id,
            verdict_status="VERIFIED",
            tx_status="COMMITTED",
            exit_code=0,
        )
        proof_file = self.workspace / ".brainfrog" / "proofs" / f"{task_id}.json"
        data = json.loads(proof_file.read_text(encoding="utf-8"))
        data["verdict"]["status"] = ""
        proof_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

        code = verify_proof_cli(self.workspace, task_id)
        self.assertEqual(code, 1)

    def test_verify_proof_cli_missing_target_usage(self) -> None:
        """8. Missing target argument -> usage message and exit 1."""
        with patch("sys.argv", ["brainfrog", "-r", str(self.workspace), "verify-proof"]):
            self.assertEqual(cli_main(), 1)


if __name__ == "__main__":
    unittest.main()
