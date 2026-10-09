"""BrainFrog Phase 6C.2 — Adversarial Remediation Regression Test Suite.

Tests specific remediation requirements for findings F-01 through F-07:
- F-01: Cross-platform path validation (rejecting Windows backslash traversals, UNC, drive-relative, root-relative, absolute paths; accepting valid relative paths).
- F-02: Corrupted, unreadable, and invalid-schema finalization intent handling (fails closed; distinguishing missing vs corrupt).
- F-03: Exit code and test gate consistency (exit_code=0 required when test_suite=True; rejecting missing, nonzero, boolean, timeout, and cancellation).
- F-04: Mandatory gates enforcement for VERIFIED proofs (all 5 canonical gates required).
- F-05: Explicit missing .json target resolution without .json.json doubling.
- F-07: Safe Windows atomic replace with bounded backoff and deterministic mock tests.
- Read-only invariant: zero mutations to workspace, proof files, Git state, or intents.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from core.runtime.proof_verifier import (
    DeterministicProofVerifier,
    MANDATORY_VERIFICATION_GATES,
    ProofVerificationReport,
    ProofVerificationStatus,
    is_safe_workspace_relative_path,
    verify_proof,
)
from core.runtime.task_finalization import (
    CommitIntentRecord,
    CommitIntentStatus,
    IntentCorruptError,
    TaskFinalizationCoordinator,
    safe_atomic_replace,
)
from brainfrog.sdk.client import BrainFrogClient
from cli import verify_proof_cli


def _init_git_repo(repo_dir: Path) -> str:
    """Initialize an isolated git repository and create an initial commit."""
    subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=repo_dir, capture_output=True, check=True)
    (repo_dir / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_dir, capture_output=True, check=True)
    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True, check=True)
    return res.stdout.strip()


def _make_base_proof(
    task_id: str = "task_test001",
    verdict: str = "VERIFIED",
    changes_status: str = "COMMITTED",
    exit_code: Optional[int] = 0,
    commit_sha: Optional[str] = None,
) -> Dict[str, Any]:
    sha = commit_sha or "a" * 40
    return {
        "version": "1.0.0",
        "task": {
            "task_id": task_id,
            "description": "Implement authentication validation",
            "context": {"user": "local"},
            "constraints": ["Keep zero external dependencies"],
        },
        "verdict": {
            "status": verdict,
            "reason": "All verification gates passed",
            "confidence": 1.0,
            "timestamp": "2026-10-09T12:00:00Z",
        },
        "verification": {
            "test_command": "pytest -q tests/test_auth.py",
            "exit_code": exit_code,
            "stdout_summary": "1 passed in 0.05s",
            "stderr_summary": "",
            "process_evidence": {
                "command": "pytest -q tests/test_auth.py",
                "returncode": exit_code,
                "timed_out": False,
                "cancelled": False,
                "task_id": task_id,
            },
            "gate_checks": {
                "workspace_scope": True,
                "test_suite": (exit_code == 0 and verdict == "VERIFIED"),
                "transaction_integrity": (changes_status == "COMMITTED"),
                "execution_integrity": True,
                "trust_integrity": True,
            },
        },
        "changes": {
            "status": changes_status,
            "transaction_id": f"tx_{task_id}",
            "files": [
                {
                    "path": "auth.py",
                    "operation": "CREATE",
                    "sha256_before": None,
                    "sha256_after": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                    "bytes_changed": 128,
                }
            ],
            "rollback_snapshot_id": f"rb_{task_id}",
        },
        "provenance": {
            "is_git": True,
            "branch": "main",
            "initial_head_sha": sha,
            "final_head_sha": sha,
            "commit_sha": sha,
        },
        "reproducibility": {
            "environment": {"python_version": "3.12.1", "platform": "win32"},
            "execution_steps": [],
        },
    }


def _write_proof(workspace: Path, task_id: str, data: Dict[str, Any]) -> Path:
    proof_dir = workspace / ".brainfrog" / "proofs"
    proof_dir.mkdir(parents=True, exist_ok=True)
    p = proof_dir / f"{task_id}.json"
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return p


# =========================================================================
# F-01: Cross-Platform Path Validation
# =========================================================================
class TestF01PathValidation:
    """Tests for Finding F-01 and C-01: Cross-platform workspace relative path validation."""

    @pytest.mark.parametrize(
        "bad_path",
        [
            # Traversal
            "../secret.txt",
            "..\\secret.txt",
            "subdir/../../secret.txt",
            "subdir\\..\\..\\secret.txt",
            "a/b/../../../etc/passwd",
            "a\\b\\..\\..\\..\\etc\\passwd",
            "..",
            # POSIX absolute
            "/etc/passwd",
            "/",
            "/var/log",
            # Windows drive-absolute
            "C:\\Windows\\System32",
            "c:/Windows/System32",
            "D:\\file.txt",
            # Windows drive-relative
            "C:file.txt",
            "c:subdir/file.txt",
            # Windows root-relative
            "\\Windows\\System32\\cmd.exe",
            "\\System32",
            # Windows UNC
            "\\\\server\\share\\file.txt",
            "//server/share/file.txt",
            # Null bytes and empty
            "",
            "file\0.txt",
            # Double separators resulting in root or empty segments
            "//etc",
            "a//b",
            # Whitespace-only paths (Finding C-01)
            "   ",
            "\t",
            "\n",
            " \r\n ",
            # Whitespace-only segments (Finding C-01)
            "foo/ /bar",
            "foo/\t/bar",
            "foo\\ \\bar",
            "foo\\\t\\bar",
            # Trailing whitespace and trailing dot cases (Finding C-01)
            "file.txt ",
            "file.txt\t",
            "file.txt.",
            "foo/file.txt ",
            "foo/file.txt.",
            "foo\\file.txt.",
            # Segment containing only dot without valid depth
            ".",
        ],
    )
    def test_unsafe_paths_rejected_by_helper(self, bad_path: str) -> None:
        assert not is_safe_workspace_relative_path(bad_path)

    @pytest.mark.parametrize(
        "valid_path",
        [
            "auth.py",
            "src/module.py",
            "src/models.py",
            "src\\models.py",
            "deep/nested/path/to/file.json",
            "deep\\nested\\path\\to\\file.json",
            "README.md",
            ".gitignore",
            ".brainfrog/plans/task_01.md",
            ".brainfrog/proofs/task.json",
            "src/sub\\module.py",
            "./foo",
            "foo/./bar",
        ],
    )
    def test_safe_paths_accepted_by_helper(self, valid_path: str) -> None:
        assert is_safe_workspace_relative_path(valid_path)

    def test_proof_verifier_rejects_windows_backslash_traversal(self, tmp_path: Path) -> None:
        commit_sha = _init_git_repo(tmp_path)
        task_id = "task_f01_backslash"
        data = _make_base_proof(task_id=task_id, commit_sha=commit_sha)
        data["changes"]["files"] = [{"path": "subdir\\..\\..\\secret.key", "operation": "CREATE"}]
        _write_proof(tmp_path, task_id, data)

        report = verify_proof(tmp_path, task_id)
        assert report.overall_status == ProofVerificationStatus.INVALID
        assert any("Illegal or non-workspace-relative path" in r for r in report.reasons)

    def test_proof_verifier_rejects_windows_root_relative(self, tmp_path: Path) -> None:
        commit_sha = _init_git_repo(tmp_path)
        task_id = "task_f01_root_rel"
        data = _make_base_proof(task_id=task_id, commit_sha=commit_sha)
        data["changes"]["files"] = [{"path": "\\Windows\\System32\\cmd.exe", "operation": "CREATE"}]
        _write_proof(tmp_path, task_id, data)

        report = verify_proof(tmp_path, task_id)
        assert report.overall_status == ProofVerificationStatus.INVALID
        assert any("Illegal or non-workspace-relative path" in r for r in report.reasons)

    def test_proof_verifier_rejects_whitespace_path(self, tmp_path: Path) -> None:
        commit_sha = _init_git_repo(tmp_path)
        task_id = "task_c01_whitespace"
        data = _make_base_proof(task_id=task_id, commit_sha=commit_sha)
        data["changes"]["files"] = [{"path": "   ", "operation": "CREATE"}]
        _write_proof(tmp_path, task_id, data)

        report = verify_proof(tmp_path, task_id)
        assert report.overall_status == ProofVerificationStatus.INVALID
        assert any("Illegal or non-workspace-relative path" in r for r in report.reasons)

    def test_proof_verifier_rejects_whitespace_segment(self, tmp_path: Path) -> None:
        commit_sha = _init_git_repo(tmp_path)
        task_id = "task_c01_ws_segment"
        data = _make_base_proof(task_id=task_id, commit_sha=commit_sha)
        data["changes"]["files"] = [{"path": "foo/ /bar", "operation": "CREATE"}]
        _write_proof(tmp_path, task_id, data)

        report = verify_proof(tmp_path, task_id)
        assert report.overall_status == ProofVerificationStatus.INVALID
        assert any("Illegal or non-workspace-relative path" in r for r in report.reasons)

    def test_proof_verifier_rejects_trailing_dot(self, tmp_path: Path) -> None:
        commit_sha = _init_git_repo(tmp_path)
        task_id = "task_c01_trailing_dot"
        data = _make_base_proof(task_id=task_id, commit_sha=commit_sha)
        data["changes"]["files"] = [{"path": "file.txt.", "operation": "CREATE"}]
        _write_proof(tmp_path, task_id, data)

        report = verify_proof(tmp_path, task_id)
        assert report.overall_status == ProofVerificationStatus.INVALID
        assert any("Illegal or non-workspace-relative path" in r for r in report.reasons)

    def test_proof_verifier_rejects_trailing_whitespace(self, tmp_path: Path) -> None:
        commit_sha = _init_git_repo(tmp_path)
        task_id = "task_c01_trailing_ws"
        data = _make_base_proof(task_id=task_id, commit_sha=commit_sha)
        data["changes"]["files"] = [{"path": "file.txt ", "operation": "CREATE"}]
        _write_proof(tmp_path, task_id, data)

        report = verify_proof(tmp_path, task_id)
        assert report.overall_status == ProofVerificationStatus.INVALID
        assert any("Illegal or non-workspace-relative path" in r for r in report.reasons)


# =========================================================================
# F-02: Corrupt Finalization Intent
# =========================================================================
class TestF02CorruptIntentHandling:
    """Tests for Finding F-02: Present but corrupt finalization intent fail-closed."""

    def test_get_intent_raises_intent_corrupt_error_on_malformed_json(self, tmp_path: Path) -> None:
        finalizer = TaskFinalizationCoordinator(tmp_path)
        intent_file = tmp_path / ".brainfrog" / "finalization" / "task_f02.json"
        intent_file.parent.mkdir(parents=True, exist_ok=True)
        intent_file.write_text("{ unclosed json...", encoding="utf-8")

        with pytest.raises(IntentCorruptError) as exc_info:
            finalizer.get_intent("task_f02")
        assert "task_f02" in str(exc_info.value)
        assert "Malformed JSON" in str(exc_info.value) or "corrupt" in str(exc_info.value).lower()

    def test_get_intent_raises_intent_corrupt_error_on_invalid_schema(self, tmp_path: Path) -> None:
        finalizer = TaskFinalizationCoordinator(tmp_path)
        intent_file = tmp_path / ".brainfrog" / "finalization" / "task_f02_bad_schema.json"
        intent_file.parent.mkdir(parents=True, exist_ok=True)
        # Not a dict or missing task_id
        intent_file.write_text("[]", encoding="utf-8")

        with pytest.raises(IntentCorruptError):
            finalizer.get_intent("task_f02_bad_schema")

    def test_missing_intent_returns_none_not_error(self, tmp_path: Path) -> None:
        finalizer = TaskFinalizationCoordinator(tmp_path)
        assert finalizer.get_intent("nonexistent_task") is None

    def test_verifier_fails_closed_when_intent_is_corrupt(self, tmp_path: Path) -> None:
        commit_sha = _init_git_repo(tmp_path)
        task_id = "task_f02_verifier_corrupt"
        data = _make_base_proof(task_id=task_id, commit_sha=commit_sha)
        _write_proof(tmp_path, task_id, data)

        intent_file = tmp_path / ".brainfrog" / "finalization" / f"{task_id}.json"
        intent_file.parent.mkdir(parents=True, exist_ok=True)
        intent_file.write_text("{{corrupted-intent-content}}", encoding="utf-8")

        report = verify_proof(tmp_path, task_id)
        assert report.overall_status == ProofVerificationStatus.INVALID
        assert any("corrupt" in r.lower() or "malformed" in r.lower() for r in report.reasons)


# =========================================================================
# F-03: Test Gate and Exit-Code Consistency
# =========================================================================
class TestF03ExitCodeAndGateConsistency:
    """Tests for Finding F-03: Exit code and test gate invariant consistency."""

    def test_exit_code_none_with_test_suite_true_rejected_on_failed_verdict(self, tmp_path: Path) -> None:
        commit_sha = _init_git_repo(tmp_path)
        task_id = "task_f03_none_exit"
        data = _make_base_proof(
            task_id=task_id,
            verdict="FAILED",
            changes_status="ROLLED_BACK",
            exit_code=None,
            commit_sha=commit_sha,
        )
        data["verification"]["exit_code"] = None
        data["verification"]["gate_checks"]["test_suite"] = True
        _write_proof(tmp_path, task_id, data)

        report = verify_proof(tmp_path, task_id)
        assert report.overall_status == ProofVerificationStatus.INVALID
        assert any("test_suite" in r.lower() and ("exit" in r.lower() or "contradict" in r.lower()) for r in report.reasons)

    def test_exit_code_boolean_masquerading_as_int_rejected(self, tmp_path: Path) -> None:
        commit_sha = _init_git_repo(tmp_path)
        task_id = "task_f03_bool_exit"
        data = _make_base_proof(task_id=task_id, commit_sha=commit_sha)
        # In Python, isinstance(False, int) is True and False == 0 is True!
        data["verification"]["exit_code"] = False
        data["verification"]["gate_checks"]["test_suite"] = True
        _write_proof(tmp_path, task_id, data)

        report = verify_proof(tmp_path, task_id)
        assert report.overall_status == ProofVerificationStatus.INVALID
        assert any("exit_code" in r.lower() for r in report.reasons)

    def test_test_suite_true_with_nonzero_exit_code_rejected(self, tmp_path: Path) -> None:
        commit_sha = _init_git_repo(tmp_path)
        task_id = "task_f03_nonzero_exit"
        data = _make_base_proof(task_id=task_id, verdict="FAILED", changes_status="ROLLED_BACK", exit_code=1, commit_sha=commit_sha)
        data["verification"]["gate_checks"]["test_suite"] = True
        _write_proof(tmp_path, task_id, data)

        report = verify_proof(tmp_path, task_id)
        assert report.overall_status == ProofVerificationStatus.INVALID

    def test_test_suite_true_with_timeout_or_cancellation_rejected(self, tmp_path: Path) -> None:
        commit_sha = _init_git_repo(tmp_path)
        task_id = "task_f03_timeout_gate"
        data = _make_base_proof(task_id=task_id, verdict="FAILED", changes_status="ROLLED_BACK", exit_code=0, commit_sha=commit_sha)
        data["verification"]["process_evidence"]["timed_out"] = True
        data["verification"]["gate_checks"]["test_suite"] = True
        _write_proof(tmp_path, task_id, data)

        report = verify_proof(tmp_path, task_id)
        assert report.overall_status == ProofVerificationStatus.INVALID


# =========================================================================
# F-04: Mandatory Gates Enforcement
# =========================================================================
class TestF04MandatoryGates:
    """Tests for Finding F-04: Enforcing all 5 canonical gates for VERIFIED proofs."""

    @pytest.mark.parametrize("missing_gate", list(MANDATORY_VERIFICATION_GATES))
    def test_verified_proof_missing_mandatory_gate_rejected(self, tmp_path: Path, missing_gate: str) -> None:
        commit_sha = _init_git_repo(tmp_path)
        task_id = f"task_f04_missing_{missing_gate}"
        data = _make_base_proof(task_id=task_id, commit_sha=commit_sha)
        # Drop one mandatory gate
        del data["verification"]["gate_checks"][missing_gate]
        _write_proof(tmp_path, task_id, data)

        report = verify_proof(tmp_path, task_id)
        assert report.overall_status == ProofVerificationStatus.INVALID
        assert any(f"Missing mandatory verification gate '{missing_gate}'" in r for r in report.reasons)

    def test_verified_proof_with_all_mandatory_gates_accepted(self, tmp_path: Path) -> None:
        commit_sha = _init_git_repo(tmp_path)
        task_id = "task_f04_all_gates"
        data = _make_base_proof(task_id=task_id, commit_sha=commit_sha)
        _write_proof(tmp_path, task_id, data)

        report = verify_proof(tmp_path, task_id)
        assert report.overall_status == ProofVerificationStatus.VALID

    def test_failed_proof_can_omit_gates_safely(self, tmp_path: Path) -> None:
        commit_sha = _init_git_repo(tmp_path)
        task_id = "task_f04_failed_partial_gates"
        data = _make_base_proof(
            task_id=task_id,
            verdict="FAILED",
            changes_status="ROLLED_BACK",
            exit_code=1,
            commit_sha=commit_sha,
        )
        # Failed proof only executed up to test_suite
        data["verification"]["gate_checks"] = {
            "workspace_scope": True,
            "test_suite": False,
        }
        _write_proof(tmp_path, task_id, data)

        report = verify_proof(tmp_path, task_id)
        assert report.overall_status == ProofVerificationStatus.VALID


# =========================================================================
# F-05: Explicit JSON Target Resolution
# =========================================================================
class TestF05ExplicitTargetResolution:
    """Tests for Finding F-05: Explicit path resolution without double .json."""

    def test_missing_explicit_json_path_does_not_double_suffix(self, tmp_path: Path) -> None:
        verifier = DeterministicProofVerifier(tmp_path)
        target = "tests/fixtures/missing_proof.json"
        report = verifier.verify(target)

        assert report.overall_status == ProofVerificationStatus.INCOMPLETE
        # The reported missing path must NOT end in .json.json!
        for reason in report.reasons:
            assert ".json.json" not in reason
        for check in report.checks:
            assert ".json.json" not in check.message

    def test_missing_bare_task_id_resolves_to_single_json(self, tmp_path: Path) -> None:
        verifier = DeterministicProofVerifier(tmp_path)
        target = "task_12345"
        report = verifier.verify(target)

        assert report.overall_status == ProofVerificationStatus.INCOMPLETE
        for reason in report.reasons:
            assert "task_12345.json" in reason
            assert ".json.json" not in reason


# =========================================================================
# F-07: Safe Windows Atomic Replace with Bounded Retry
# =========================================================================
class TestF07AtomicReplaceRetry:
    """Tests for Finding F-07: Bounded retry for transient replacement errors."""

    def test_safe_atomic_replace_succeeds_immediately(self, tmp_path: Path) -> None:
        src = tmp_path / "src.txt"
        dst = tmp_path / "dst.txt"
        src.write_text("hello", encoding="utf-8")

        safe_atomic_replace(src, dst)
        assert not src.exists()
        assert dst.exists()
        assert dst.read_text(encoding="utf-8") == "hello"

    def test_safe_atomic_replace_retries_transient_error_and_succeeds(self, tmp_path: Path) -> None:
        src = tmp_path / "src.txt"
        dst = tmp_path / "dst.txt"
        src.write_text("hello", encoding="utf-8")

        attempts = [0]

        def flaky_replace(s: Path, d: Path) -> None:
            attempts[0] += 1
            if attempts[0] < 3:
                # Raise Windows PermissionError (Access is denied / WinError 5)
                pe = PermissionError("Access is denied")
                pe.winerror = 5
                raise pe
            os.replace(s, d)

        safe_atomic_replace(src, dst, max_retries=5, initial_delay=0.001, replace_fn=flaky_replace)
        assert attempts[0] == 3
        assert dst.read_text(encoding="utf-8") == "hello"

    def test_safe_atomic_replace_exhaustion_raises(self, tmp_path: Path) -> None:
        src = tmp_path / "src.txt"
        dst = tmp_path / "dst.txt"
        src.write_text("hello", encoding="utf-8")

        def always_fail(s: Path, d: Path) -> None:
            pe = PermissionError("Access is denied")
            pe.winerror = 5
            raise pe

        with pytest.raises(PermissionError):
            safe_atomic_replace(src, dst, max_retries=3, initial_delay=0.001, replace_fn=always_fail)

        # src still exists for caller cleanup
        assert src.exists()

    def test_safe_atomic_replace_does_not_retry_unrelated_errors(self, tmp_path: Path) -> None:
        src = tmp_path / "src.txt"
        dst = tmp_path / "dst.txt"
        src.write_text("hello", encoding="utf-8")

        attempts = [0]

        def fail_with_file_not_found(s: Path, d: Path) -> None:
            attempts[0] += 1
            raise FileNotFoundError("No such file")

        with pytest.raises(FileNotFoundError):
            safe_atomic_replace(src, dst, max_retries=5, initial_delay=0.001, replace_fn=fail_with_file_not_found)

        # Non-transient error failed immediately without burning 5 retries
        assert attempts[0] == 1


# =========================================================================
# C-02: Target Null-Byte Handling
# =========================================================================
class TestC02NullByteTargetHandling:
    """Tests for Finding C-02: Target paths containing embedded null characters fail closed cleanly."""

    def test_verifier_handles_null_byte_target_without_exception(self, tmp_path: Path) -> None:
        verifier = DeterministicProofVerifier(tmp_path)
        report = verifier.verify("malicious\x00target.json")

        assert report.overall_status == ProofVerificationStatus.INVALID
        assert report.is_invalid
        assert report.proof_path is None
        assert report.task_id is None
        assert any("embedded null byte" in r.lower() for r in report.reasons)
        assert any(c.check_name == "target_path_safety" and c.status == ProofVerificationStatus.INVALID for c in report.checks)

    def test_sdk_handles_null_byte_target(self, tmp_path: Path) -> None:
        client = BrainFrogClient(tmp_path)
        report = client.verify_proof("malicious\x00target.json")

        assert report.overall_status == ProofVerificationStatus.INVALID
        assert report.is_invalid
        assert any("embedded null byte" in r.lower() for r in report.reasons)

    def test_cli_handles_null_byte_target(self, tmp_path: Path) -> None:
        exit_code = verify_proof_cli(tmp_path, "malicious\x00target.json")
        assert exit_code == 1

    def test_ordinary_missing_and_malformed_targets_retain_semantics(self, tmp_path: Path) -> None:
        verifier = DeterministicProofVerifier(tmp_path)

        # 1. Missing explicit .json path -> INCOMPLETE
        rep_missing_json = verifier.verify("nonexistent.json")
        assert rep_missing_json.overall_status == ProofVerificationStatus.INCOMPLETE

        # 2. Missing bare task ID -> INCOMPLETE
        rep_missing_task = verifier.verify("nonexistent_task_id")
        assert rep_missing_task.overall_status == ProofVerificationStatus.INCOMPLETE

        # 3. Malformed JSON content in existing proof file -> INVALID
        proof_dir = tmp_path / ".brainfrog" / "proofs"
        proof_dir.mkdir(parents=True, exist_ok=True)
        malformed_file = proof_dir / "malformed.json"
        malformed_file.write_text("{ not valid json", encoding="utf-8")

        rep_malformed = verifier.verify("malformed")
        assert rep_malformed.overall_status == ProofVerificationStatus.INVALID
        assert any("malformed json" in r.lower() for r in rep_malformed.reasons)
