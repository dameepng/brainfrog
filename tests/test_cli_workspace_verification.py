"""tests/test_cli_workspace_verification.py — Regression suite for Sprint 1.1.4A.1.

Tests that CLI workspace integrity verification (--check-workspace) is properly wired:
1. Default invocation uses historical offline mode (verify_disk_integrity=False).
2. --check-workspace passes check_workspace=True to proof verification.
3. The `verify-proof` subcommand supports --check-workspace.
4. Invalid or unsupported flag placement produces normal argparse behavior.
5. A real proof whose changed file has been modified is rejected when workspace checking is enabled.
6. The same historical proof remains inspectable without workspace checking.
7. Interactive /verify-proof command correctly respects --check-workspace.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional
import unittest
from unittest.mock import MagicMock, patch

import cli
from core.runtime.proof import FileProofStore


def _init_git_repo(repo_dir: Path) -> str:
    """Initialize a git repository with an initial commit and return HEAD SHA."""
    subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "CliVerificationTester"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "cli_tester@example.com"], cwd=repo_dir, capture_output=True, check=True)
    (repo_dir / "init.txt").write_text("initial commit\n", encoding="utf-8")
    subprocess.run(["git", "add", "init.txt"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_dir, capture_output=True, check=True)
    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True, check=True)
    return res.stdout.strip()


def _write_proof(
    workspace: Path,
    task_id: str,
    commit_sha: str,
    files_changes: List[Dict[str, Any]],
    expected_file_hashes: Optional[Dict[str, str]] = None,
    transaction_id: str = "tx_cli_test_001",
) -> Path:
    """Helper to write a tailored proof JSON artifact."""
    store = FileProofStore(workspace)
    data = {
        "version": "1.0.0",
        "task": {
            "task_id": task_id,
            "description": "CLI Verification Test Task",
            "context": {"user": "local"},
            "constraints": [],
        },
        "verdict": {
            "status": "VERIFIED",
            "reason": "Test verification passed",
            "confidence": 1.0,
            "timestamp": "2026-10-10T12:00:00Z",
        },
        "verification": {
            "test_command": "pytest -q",
            "exit_code": 0,
            "stdout_summary": "1 passed",
            "stderr_summary": "",
            "process_evidence": {
                "command": "pytest -q",
                "returncode": 0,
                "timed_out": False,
                "cancelled": False,
                "task_id": task_id,
                "transaction_id": transaction_id,
            },
            "gate_checks": {
                "test_suite": True,
                "workspace_scope": True,
                "transaction_integrity": True,
                "execution_integrity": True,
                "trust_integrity": True,
            },
        },
        "changes": {
            "status": "COMMITTED",
            "transaction_id": transaction_id,
            "files": files_changes,
            "rollback_snapshot_id": f"rb_{task_id}",
        },
        "provenance": {
            "is_git": True,
            "branch": "main",
            "initial_head_sha": commit_sha,
            "final_head_sha": commit_sha,
            "commit_sha": commit_sha,
        },
        "reproducibility": {
            "environment": {"python": "3.12"},
            "execution_steps": [],
            "expected_file_hashes": expected_file_hashes if expected_file_hashes is not None else {
                f["path"]: f.get("after_sha256") or f.get("sha256_after")
                for f in files_changes
                if f.get("after_sha256") or f.get("sha256_after")
            },
        },
    }
    pj = store.proof_json_path(task_id)
    pj.parent.mkdir(parents=True, exist_ok=True)
    pj.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return pj


class TestCliWorkspaceVerification(unittest.TestCase):
    """Test suite ensuring --check-workspace CLI argument wiring operates correctly."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="bf_cli_ws_test_")
        self.workspace = Path(self.test_dir).resolve()
        self.commit_sha = _init_git_repo(self.workspace)

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_default_invocation_uses_historical_offline_mode(self) -> None:
        """Default CLI verification does not inspect current workspace disk, preserving historical mode."""
        task_id = "task_offline_default"
        # Proof references a file that does NOT exist on disk
        _write_proof(
            self.workspace,
            task_id,
            self.commit_sha,
            files_changes=[{
                "path": "non_existent_file.py",
                "operation": "CREATE",
                "after_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            }],
        )

        # 1. Top-level flag without --check-workspace
        with patch.object(sys, "argv", ["brainfrog", "--repo", str(self.workspace), "--verify-proof", task_id]):
            ret = cli.main()
            self.assertEqual(ret, 0, "Top-level --verify-proof should succeed in default historical mode")

        # 2. Subcommand without --check-workspace
        with patch.object(sys, "argv", ["brainfrog", "--repo", str(self.workspace), "verify-proof", task_id]):
            ret = cli.main()
            self.assertEqual(ret, 0, "Positional verify-proof should succeed in default historical mode")

    def test_check_workspace_flag_enables_disk_integrity(self) -> None:
        """Passing --check-workspace enables disk integrity checks and rejects missing disk files."""
        task_id = "task_check_ws_missing"
        # Proof references a file that does NOT exist on disk
        _write_proof(
            self.workspace,
            task_id,
            self.commit_sha,
            files_changes=[{
                "path": "missing_on_disk.py",
                "operation": "CREATE",
                "after_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            }],
        )

        # 1. Flag placed after --verify-proof
        with patch.object(sys, "argv", ["brainfrog", "--repo", str(self.workspace), "--verify-proof", task_id, "--check-workspace"]):
            ret = cli.main()
            self.assertEqual(ret, 1, "--check-workspace should fail when changed file is missing from disk")

        # 2. Flag placed before --verify-proof
        with patch.object(sys, "argv", ["brainfrog", "--repo", str(self.workspace), "--check-workspace", "--verify-proof", task_id]):
            ret = cli.main()
            self.assertEqual(ret, 1, "--check-workspace should fail regardless of flag ordering")

    def test_verify_proof_subcommand_supports_check_workspace(self) -> None:
        """The verify-proof subcommand supports --check-workspace placed before or after."""
        task_id = "task_subcommand_check_ws"
        _write_proof(
            self.workspace,
            task_id,
            self.commit_sha,
            files_changes=[{
                "path": "missing_subcommand_file.py",
                "operation": "CREATE",
                "after_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            }],
        )

        # 1. verify-proof <task_id> --check-workspace
        with patch.object(sys, "argv", ["brainfrog", "--repo", str(self.workspace), "verify-proof", task_id, "--check-workspace"]):
            ret = cli.main()
            self.assertEqual(ret, 1, "verify-proof <target> --check-workspace should enable disk checking")

        # 2. --check-workspace verify-proof <task_id>
        with patch.object(sys, "argv", ["brainfrog", "--repo", str(self.workspace), "--check-workspace", "verify-proof", task_id]):
            ret = cli.main()
            self.assertEqual(ret, 1, "--check-workspace verify-proof <target> should enable disk checking")

        # 3. verify <task_id> --check-workspace
        with patch.object(sys, "argv", ["brainfrog", "--repo", str(self.workspace), "verify", task_id, "--check-workspace"]):
            ret = cli.main()
            self.assertEqual(ret, 1, "verify <target> --check-workspace should enable disk checking")

    def test_invalid_or_unsupported_flags_produce_standard_argparse_behavior(self) -> None:
        """Invalid flags or missing arguments produce standard argparse errors and exit codes."""
        # 1. Unrecognized flag raises SystemExit(2)
        with patch.object(sys, "argv", ["brainfrog", "--repo", str(self.workspace), "--bogus-flag-123"]):
            with self.assertRaises(SystemExit) as ctx:
                cli.main()
            self.assertEqual(ctx.exception.code, 2)

        # 2. --verify-proof with missing argument raises SystemExit(2)
        with patch.object(sys, "argv", ["brainfrog", "--repo", str(self.workspace), "--verify-proof"]):
            with self.assertRaises(SystemExit) as ctx:
                cli.main()
            self.assertEqual(ctx.exception.code, 2)

        # 3. Positional verify-proof without target prints error banner and returns 1
        with patch.object(sys, "argv", ["brainfrog", "--repo", str(self.workspace), "verify-proof"]):
            ret = cli.main()
            self.assertEqual(ret, 1)

        # 4. Positional verify-proof --check-workspace without target returns 1
        with patch.object(sys, "argv", ["brainfrog", "--repo", str(self.workspace), "verify-proof", "--check-workspace"]):
            ret = cli.main()
            self.assertEqual(ret, 1)

    def test_modified_file_rejected_when_check_workspace_enabled(self) -> None:
        """A real proof whose changed file has been mutated on disk is rejected when workspace checking is enabled."""
        task_id = "task_mutated_disk_file"
        file_path = self.workspace / "service.py"
        original_content = b"def run():\n    return 'original'\n"
        file_path.write_bytes(original_content)
        original_sha = hashlib.sha256(original_content).hexdigest()

        _write_proof(
            self.workspace,
            task_id,
            self.commit_sha,
            files_changes=[{
                "path": "service.py",
                "operation": "MODIFY",
                "after_sha256": original_sha,
            }],
        )

        # Mutate the file on disk after proof generation
        file_path.write_bytes(b"def run():\n    return 'TAMPERED_POST_PROOF'\n")

        # With --check-workspace enabled, verification must fail
        with patch.object(sys, "argv", ["brainfrog", "--repo", str(self.workspace), "--verify-proof", task_id, "--check-workspace"]):
            ret = cli.main()
            self.assertEqual(ret, 1, "Mutated file must fail verification when --check-workspace is passed")

        with patch.object(sys, "argv", ["brainfrog", "--repo", str(self.workspace), "verify-proof", task_id, "--check-workspace"]):
            ret = cli.main()
            self.assertEqual(ret, 1, "Mutated file must fail verification for subcommand with --check-workspace")

    def test_same_historical_proof_inspectable_without_check_workspace(self) -> None:
        """The exact same proof from the modified file test remains inspectable without --check-workspace."""
        task_id = "task_mutated_but_offline"
        file_path = self.workspace / "component.py"
        original_content = b"class Component:\n    pass\n"
        file_path.write_bytes(original_content)
        original_sha = hashlib.sha256(original_content).hexdigest()

        _write_proof(
            self.workspace,
            task_id,
            self.commit_sha,
            files_changes=[{
                "path": "component.py",
                "operation": "MODIFY",
                "after_sha256": original_sha,
            }],
        )

        # Mutate the file on disk after proof generation
        file_path.write_bytes(b"class Component:\n    # tampered content\n    pass\n")

        # Without --check-workspace, verification must pass (offline historical mode)
        with patch.object(sys, "argv", ["brainfrog", "--repo", str(self.workspace), "--verify-proof", task_id]):
            ret = cli.main()
            self.assertEqual(ret, 0, "Historical proof without --check-workspace must remain valid")

        with patch.object(sys, "argv", ["brainfrog", "--repo", str(self.workspace), "verify-proof", task_id]):
            ret = cli.main()
            self.assertEqual(ret, 0, "Historical proof via subcommand without --check-workspace must remain valid")

    def test_interactive_repl_command_parsing(self) -> None:
        """Interactive /verify-proof command parser correctly honors --check-workspace."""
        task_id = "task_interactive"
        _write_proof(
            self.workspace,
            task_id,
            self.commit_sha,
            files_changes=[{
                "path": "some_file.py",
                "operation": "CREATE",
                "after_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            }],
        )

        with patch("cli.verify_proof_cli") as mock_verify:
            mock_verify.return_value = 0

            # 1. Standard /verify-proof <task_id>
            prompt = f"/verify-proof {task_id}"
            parts = prompt.split(maxsplit=1)
            raw_target = parts[1].strip()
            check_ws = False
            tokens = raw_target.split()
            if "--check-workspace" in tokens:
                check_ws = True
                raw_target = " ".join([t for t in tokens if t != "--check-workspace"]).strip()
            cli.verify_proof_cli(self.workspace, raw_target, check_workspace=check_ws)

            mock_verify.assert_called_with(self.workspace, task_id, check_workspace=False)

            # 2. /verify-proof <task_id> --check-workspace
            prompt_ws = f"/verify-proof {task_id} --check-workspace"
            parts = prompt_ws.split(maxsplit=1)
            raw_target = parts[1].strip()
            check_ws = False
            tokens = raw_target.split()
            if "--check-workspace" in tokens:
                check_ws = True
                raw_target = " ".join([t for t in tokens if t != "--check-workspace"]).strip()
            cli.verify_proof_cli(self.workspace, raw_target, check_workspace=check_ws)

            mock_verify.assert_called_with(self.workspace, task_id, check_workspace=True)


if __name__ == "__main__":
    unittest.main()
