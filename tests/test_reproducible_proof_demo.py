"""Tests for BrainFrog Reproducible Proof Demo and Cross-Platform Cleanup.

Validates Sprint 1.3B.1:
1. Cross-platform cleanup of read-only files (e.g. Windows Git objects).
2. Cleanup failures are surfaced as clear RuntimeError exceptions rather than silently suppressed.
3. Pre-existing directories are never removed by cleanup.
4. Execution of the canonical workflow through BrainFrogRuntime in demo scenarios:
   - Scenario 1: Real code modification, test pass -> VERIFIED proof (exit code 0).
   - Scenario 2: Failing tests -> ROLLED_BACK transaction, FAILED proof (exit code 3).
   - Scenario 3: Post-proof tampering -> INVALID status, MISMATCH detected (exit code 1).
5. Real test exit codes strictly control task outcomes; failed tasks cannot appear verified.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from demo.reproducible_proof_demo import (
    cleanup_temporary_workspace,
    main,
    remove_readonly,
    run_scenario_1_verified,
    run_scenario_2_failed_rollback,
    run_scenario_3_tamper_detection,
    _init_git_workspace,
)
from core.runtime.proof_inspection import inspect_proof


class TestWindowsCleanup:
    """Tests for cross-platform temporary directory cleanup."""

    def test_cleanup_removes_readonly_files(self):
        """Read-only files (like Git object files on Windows) are unlocked and deleted."""
        temp_dir = Path(tempfile.mkdtemp(prefix="test_readonly_cleanup_"))
        try:
            sub_dir = temp_dir / "subdir" / "nested"
            sub_dir.mkdir(parents=True)
            ro_file = sub_dir / "readonly.txt"
            ro_file.write_text("read-only content", encoding="utf-8")

            # Mark file read-only
            os.chmod(ro_file, stat.S_IREAD)
            assert not (os.stat(ro_file).st_mode & stat.S_IWRITE)

            # Cleanup must succeed and remove directory completely
            cleanup_temporary_workspace(temp_dir)
            assert not temp_dir.exists()
        finally:
            if temp_dir.exists():
                cleanup_temporary_workspace(temp_dir)

    def test_cleanup_surfaces_persistent_failure(self):
        """Persistent deletion errors raise RuntimeError identifying the workspace."""
        temp_dir = Path(tempfile.mkdtemp(prefix="test_cleanup_fail_"))
        try:
            sample = temp_dir / "file.txt"
            sample.write_text("data", encoding="utf-8")

            # Simulate unrecoverable deletion failure where directory still exists
            with patch("shutil.rmtree", side_effect=PermissionError("Simulated locked file")):
                with pytest.raises(RuntimeError) as exc_info:
                    cleanup_temporary_workspace(temp_dir)

                err_msg = str(exc_info.value)
                assert "Failed to cleanly remove temporary workspace" in err_msg
                assert str(temp_dir) in err_msg
        finally:
            if temp_dir.exists():
                cleanup_temporary_workspace(temp_dir)

    def test_cleanup_never_removes_other_directories(self):
        """Cleanup strictly deletes the specified workspace and leaves siblings untouched."""
        target_dir = Path(tempfile.mkdtemp(prefix="test_target_ws_"))
        sibling_dir = Path(tempfile.mkdtemp(prefix="test_sibling_ws_"))
        try:
            (target_dir / "a.txt").write_text("a", encoding="utf-8")
            sibling_file = sibling_dir / "b.txt"
            sibling_file.write_text("b", encoding="utf-8")

            cleanup_temporary_workspace(target_dir)

            assert not target_dir.exists()
            assert sibling_dir.exists()
            assert sibling_file.exists()
            assert sibling_file.read_text(encoding="utf-8") == "b"
        finally:
            if target_dir.exists():
                cleanup_temporary_workspace(target_dir)
            if sibling_dir.exists():
                cleanup_temporary_workspace(sibling_dir)

    def test_cleanup_nonexistent_directory_noop(self):
        """Calling cleanup on a non-existent path is a safe no-op."""
        nonexistent = Path(tempfile.gettempdir()) / "nonexistent_dir_12345"
        cleanup_temporary_workspace(nonexistent)  # Should not raise


class TestDemoProductionWorkflow:
    """Tests proving the demo scenarios exercise the canonical production workflow."""

    @pytest.fixture
    def demo_workspace(self):
        temp_dir = Path(tempfile.mkdtemp(prefix="test_demo_workflow_"))
        _init_git_workspace(temp_dir)
        try:
            yield temp_dir
        finally:
            if temp_dir.exists():
                cleanup_temporary_workspace(temp_dir)

    def test_scenario_1_canonical_verified_success(self, demo_workspace: Path):
        """Scenario 1 runs through BrainFrogRuntime, executes real tests, and persists verified proof."""
        task_id = run_scenario_1_verified(demo_workspace)
        assert task_id == "task_demo_01_verified"

        # Verify durable proof was saved in canonical proof store
        proof_json = demo_workspace / ".brainfrog" / "proofs" / f"{task_id}.json"
        assert proof_json.exists()

        # Deterministic verification passes with exit code 0
        insp = inspect_proof(demo_workspace, task_id, check_workspace=True)
        assert insp.is_verified is True
        assert insp.exit_code == 0
        assert insp.workspace_integrity_passed is True

    def test_scenario_2_canonical_honest_failure_rollback(self, demo_workspace: Path):
        """Scenario 2 executes failing test through workflow, triggers rollback, and records failure."""
        # Setup initial verified state first
        task_1_id = run_scenario_1_verified(demo_workspace)
        pre_content = (demo_workspace / "math_utils.py").read_text(encoding="utf-8")

        # Run Scenario 2
        task_2_id = run_scenario_2_failed_rollback(demo_workspace)
        assert task_2_id == "task_demo_02_failed"

        # Disk content must be rolled back to pre-task state
        current_content = (demo_workspace / "math_utils.py").read_text(encoding="utf-8")
        assert current_content == pre_content

        # Proof of failure was persisted
        proof_json = demo_workspace / ".brainfrog" / "proofs" / f"{task_2_id}.json"
        assert proof_json.exists()

        # Proof inspection distinguishes honest failure (exit code 3) from invalid/tampered (exit code 1)
        insp = inspect_proof(demo_workspace, task_2_id, check_workspace=True)
        assert insp.is_failed is True
        assert insp.exit_code == 3
        assert insp.workspace_integrity_passed is True

    def test_scenario_3_post_proof_tamper_detection(self, demo_workspace: Path):
        """Scenario 3 detects disk mutation after proof generation via workspace integrity check."""
        task_1_id = run_scenario_1_verified(demo_workspace)

        # Mutate covered file
        run_scenario_3_tamper_detection(demo_workspace, task_1_id)

        # Re-verification must detect hash mismatch and return exit code 1
        insp = inspect_proof(demo_workspace, task_1_id, check_workspace=True)
        assert insp.is_invalid is True
        assert insp.exit_code == 1
        assert insp.workspace_integrity_passed is False

    def test_demo_main_entry_point(self, monkeypatch):
        """Demo main entry point executes all 3 scenarios and exits 0 with complete cleanup."""
        monkeypatch.setattr("sys.argv", ["reproducible_proof_demo.py"])
        exit_code = main()
        assert exit_code == 0
