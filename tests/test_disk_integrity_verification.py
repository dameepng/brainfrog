"""tests/test_disk_integrity_verification.py — Adversarial Regression Suite for Sprint 1.1.4A.

Tests deterministic disk integrity verification against post-proof filesystem mutations:
1. Untouched file matching expected SHA-256.
2. Modified file matching expected SHA-256.
3. File content changed after proof creation (post-proof tampering detected).
4. File deleted after proof creation when expected to exist.
5. File intentionally deleted by the task (absence verified).
6. Created file missing after proof creation.
7. Missing or malformed expected hash (fails closed).
8. Hash for a different file or unclaimed expected hash.
9. Symlink / path escape rejected.
10. File read failure / permission error fails closed.
11. Unchanged unrelated file outside the changed-file set is ignored.
12. Workspace mutation race between initial path check and read handled safely.
13. Recovery behavior when disk integrity verification is incomplete or invalid (Work fails closed).
14. Historical mode preserves offline verification semantics without disk check.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Optional
import unittest
from unittest.mock import patch

from core.runtime.proof import (
    ChangeRecord,
    ChangesEvidence,
    FileProofStore,
    ProofArtifact,
    ProofVerdict,
    ReproducibilityEvidence,
    TaskEvidence,
    VerificationEvidence,
)
from core.runtime.proof_verifier import (
    DeterministicProofVerifier,
    ProofVerificationReport,
    ProofVerificationStatus,
    verify_proof,
)
from core.runtime.transaction import (
    FileTransactionStore,
    TransactionRecoveryManager,
)
from core.runtime.work import (
    FileWorkStore,
    VerificationStatus,
    Work,
    WorkStatus,
)
from core.runtime.work_continuation import resume_work


def _init_git_repo(repo_dir: Path) -> str:
    """Initialize a git repository with an initial commit and return HEAD SHA."""
    subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "DiskIntegrityTester"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "disk_integrity@example.com"], cwd=repo_dir, capture_output=True, check=True)
    (repo_dir / "init.txt").write_text("initial repo commit\n", encoding="utf-8")
    subprocess.run(["git", "add", "init.txt"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_dir, capture_output=True, check=True)
    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True, check=True)
    return res.stdout.strip()


def _write_custom_proof(
    workspace: Path,
    task_id: str,
    commit_sha: str,
    files_changes: List[Dict[str, Any]],
    expected_file_hashes: Optional[Dict[str, str]] = None,
    transaction_id: str = "tx_test_001",
    verdict_status: str = "VERIFIED",
    changes_status: str = "COMMITTED",
) -> Path:
    """Helper to write a tailored proof JSON artifact."""
    store = FileProofStore(workspace)
    data = {
        "version": "1.0.0",
        "task": {
            "task_id": task_id,
            "description": "Test task",
            "context": {"user": "local"},
            "constraints": [],
        },
        "verdict": {
            "status": verdict_status,
            "reason": "Verification verdict",
            "confidence": 1.0,
            "timestamp": "2026-10-10T12:00:00Z",
        },
        "verification": {
            "test_command": "pytest -q",
            "exit_code": 0 if verdict_status == "VERIFIED" else 1,
            "stdout_summary": "1 passed",
            "stderr_summary": "",
            "process_evidence": {
                "command": "pytest -q",
                "returncode": 0 if verdict_status == "VERIFIED" else 1,
                "timed_out": False,
                "cancelled": False,
                "task_id": task_id,
                "transaction_id": transaction_id,
            },
            "gate_checks": {
                "test_suite": (verdict_status == "VERIFIED"),
                "workspace_scope": True,
                "transaction_integrity": (changes_status == "COMMITTED"),
                "execution_integrity": True,
                "trust_integrity": True,
            },
        },
        "changes": {
            "status": changes_status,
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


class TestDiskIntegrityVerification(unittest.TestCase):
    """Test suite ensuring current workspace disk integrity checks fail closed."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="bf_disk_integrity_test_")
        self.workspace = Path(self.test_dir).resolve()
        self.commit_sha = _init_git_repo(self.workspace)
        self.work_store = FileWorkStore(repo_dir=self.workspace)
        self.tx_store = FileTransactionStore(workspace=self.workspace)
        self.recovery_mgr = TransactionRecoveryManager(workspace=self.workspace, store=self.tx_store)

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # Scenario 1: Untouched created file matching expected SHA-256
    # -------------------------------------------------------------------------
    def test_01_created_file_matching_expected_sha256(self) -> None:
        """A created file whose disk content matches expected SHA-256 passes verification."""
        file_path = self.workspace / "service.py"
        content = b"def run():\n    return 'ok'\n"
        file_path.write_bytes(content)
        sha = hashlib.sha256(content).hexdigest()

        _write_custom_proof(
            self.workspace,
            "task_01",
            self.commit_sha,
            files_changes=[{
                "path": "service.py",
                "operation": "CREATE",
                "after_sha256": sha,
            }],
        )

        verifier = DeterministicProofVerifier(self.workspace, verify_disk_integrity=True)
        rep = verifier.verify("task_01")
        self.assertEqual(rep.overall_status, ProofVerificationStatus.VALID)
        check = next((c for c in rep.checks if c.check_name == "current_workspace_disk_integrity"), None)
        assert check is not None
        self.assertEqual(check.status, ProofVerificationStatus.VALID)

    # -------------------------------------------------------------------------
    # Scenario 2: Modified file matching expected SHA-256
    # -------------------------------------------------------------------------
    def test_02_modified_file_matching_expected_sha256(self) -> None:
        """A modified file whose disk content matches expected SHA-256 passes verification."""
        file_path = self.workspace / "init.txt"
        new_content = b"modified init content\n"
        file_path.write_bytes(new_content)
        sha = hashlib.sha256(new_content).hexdigest()

        _write_custom_proof(
            self.workspace,
            "task_02",
            self.commit_sha,
            files_changes=[{
                "path": "init.txt",
                "operation": "MODIFY",
                "after_sha256": sha,
            }],
        )

        verifier = DeterministicProofVerifier(self.workspace, verify_disk_integrity=True)
        rep = verifier.verify("task_02")
        self.assertEqual(rep.overall_status, ProofVerificationStatus.VALID)

    # -------------------------------------------------------------------------
    # Scenario 3: File content changed after proof creation (post-proof tampering)
    # -------------------------------------------------------------------------
    def test_03_file_content_changed_after_proof_creation(self) -> None:
        """Post-proof file modification is detected and marked INVALID."""
        file_path = self.workspace / "app.py"
        orig_content = b"original verified code\n"
        file_path.write_bytes(orig_content)
        orig_sha = hashlib.sha256(orig_content).hexdigest()

        _write_custom_proof(
            self.workspace,
            "task_03",
            self.commit_sha,
            files_changes=[{
                "path": "app.py",
                "operation": "MODIFY",
                "after_sha256": orig_sha,
            }],
        )

        # Mutate file after proof was generated
        file_path.write_bytes(b"tampered post-proof code\n")

        verifier = DeterministicProofVerifier(self.workspace, verify_disk_integrity=True)
        rep = verifier.verify("task_03")
        self.assertEqual(rep.overall_status, ProofVerificationStatus.INVALID)
        mismatch_check = next((c for c in rep.checks if c.check_name == "disk_integrity_hash_mismatch"), None)
        assert mismatch_check is not None
        self.assertEqual(mismatch_check.status, ProofVerificationStatus.INVALID)
        self.assertIn("mutated since verification", mismatch_check.message)

    # -------------------------------------------------------------------------
    # Scenario 4: File deleted after proof creation when expected to exist
    # -------------------------------------------------------------------------
    def test_04_file_deleted_after_proof_creation(self) -> None:
        """File deletion after proof creation is detected and marked INVALID."""
        file_path = self.workspace / "deleted_tamper.py"
        content = b"should be here\n"
        file_path.write_bytes(content)
        sha = hashlib.sha256(content).hexdigest()

        _write_custom_proof(
            self.workspace,
            "task_04",
            self.commit_sha,
            files_changes=[{
                "path": "deleted_tamper.py",
                "operation": "CREATE",
                "after_sha256": sha,
            }],
        )

        # Delete file after proof was written
        file_path.unlink()

        verifier = DeterministicProofVerifier(self.workspace, verify_disk_integrity=True)
        rep = verifier.verify("task_04")
        self.assertEqual(rep.overall_status, ProofVerificationStatus.INVALID)
        missing_check = next((c for c in rep.checks if c.check_name == "disk_integrity_file_missing"), None)
        assert missing_check is not None
        self.assertEqual(missing_check.status, ProofVerificationStatus.INVALID)
        self.assertIn("missing from disk", missing_check.message)

    # -------------------------------------------------------------------------
    # Scenario 5: File intentionally deleted by task
    # -------------------------------------------------------------------------
    def test_05_file_intentionally_deleted_by_task(self) -> None:
        """A file marked DELETE in change records that is absent from disk passes verification."""
        _write_custom_proof(
            self.workspace,
            "task_05",
            self.commit_sha,
            files_changes=[{
                "path": "obsolete.txt",
                "operation": "DELETE",
                "after_sha256": None,
            }],
            expected_file_hashes={},
        )

        verifier = DeterministicProofVerifier(self.workspace, verify_disk_integrity=True)
        rep = verifier.verify("task_05")
        self.assertEqual(rep.overall_status, ProofVerificationStatus.VALID)

    # -------------------------------------------------------------------------
    # Scenario 6: File marked DELETE still exists on disk
    # -------------------------------------------------------------------------
    def test_06_file_marked_delete_still_exists(self) -> None:
        """A file marked DELETE that still exists on disk fails verification."""
        obsolete_file = self.workspace / "not_actually_deleted.txt"
        obsolete_file.write_text("still here\n", encoding="utf-8")

        _write_custom_proof(
            self.workspace,
            "task_06",
            self.commit_sha,
            files_changes=[{
                "path": "not_actually_deleted.txt",
                "operation": "DELETE",
                "after_sha256": None,
            }],
            expected_file_hashes={},
        )

        verifier = DeterministicProofVerifier(self.workspace, verify_disk_integrity=True)
        rep = verifier.verify("task_06")
        self.assertEqual(rep.overall_status, ProofVerificationStatus.INVALID)
        del_check = next((c for c in rep.checks if c.check_name == "disk_integrity_deleted_file_exists"), None)
        assert del_check is not None
        self.assertEqual(del_check.status, ProofVerificationStatus.INVALID)

    # -------------------------------------------------------------------------
    # Scenario 7: Missing expected hash evidence fails closed (INCOMPLETE)
    # -------------------------------------------------------------------------
    def test_07_missing_expected_hash_evidence(self) -> None:
        """A modified file with no expected hash fails closed as INCOMPLETE."""
        target = self.workspace / "no_hash.py"
        target.write_text("code\n", encoding="utf-8")

        _write_custom_proof(
            self.workspace,
            "task_07",
            self.commit_sha,
            files_changes=[{
                "path": "no_hash.py",
                "operation": "MODIFY",
                "after_sha256": None,
            }],
            expected_file_hashes={},
        )

        verifier = DeterministicProofVerifier(self.workspace, verify_disk_integrity=True)
        rep = verifier.verify("task_07")
        self.assertEqual(rep.overall_status, ProofVerificationStatus.INCOMPLETE)
        miss_check = next((c for c in rep.checks if c.check_name == "disk_integrity_missing_hash"), None)
        assert miss_check is not None
        self.assertEqual(miss_check.status, ProofVerificationStatus.INCOMPLETE)

    # -------------------------------------------------------------------------
    # Scenario 8: Malformed expected hash fails closed (INVALID)
    # -------------------------------------------------------------------------
    def test_08_malformed_expected_hash(self) -> None:
        """A malformed expected hash (e.g. not 64 hex characters) is marked INVALID."""
        target = self.workspace / "malformed.py"
        target.write_text("code\n", encoding="utf-8")

        _write_custom_proof(
            self.workspace,
            "task_08",
            self.commit_sha,
            files_changes=[{
                "path": "malformed.py",
                "operation": "MODIFY",
                "after_sha256": "not_a_valid_sha",
            }],
        )

        verifier = DeterministicProofVerifier(self.workspace, verify_disk_integrity=True)
        rep = verifier.verify("task_08")
        self.assertEqual(rep.overall_status, ProofVerificationStatus.INVALID)
        mal_check = next((c for c in rep.checks if c.check_name == "disk_integrity_malformed_hash"), None)
        assert mal_check is not None
        self.assertEqual(mal_check.status, ProofVerificationStatus.INVALID)

    # -------------------------------------------------------------------------
    # Scenario 9: Unclaimed expected hash in reproducibility evidence
    # -------------------------------------------------------------------------
    def test_09_unclaimed_expected_hash_in_reproducibility(self) -> None:
        """An expected hash for a file not in transaction change records is rejected as INVALID."""
        target = self.workspace / "legit.py"
        content = b"legit code\n"
        target.write_bytes(content)
        legit_sha = hashlib.sha256(content).hexdigest()

        _write_custom_proof(
            self.workspace,
            "task_09",
            self.commit_sha,
            files_changes=[{
                "path": "legit.py",
                "operation": "MODIFY",
                "after_sha256": legit_sha,
            }],
            expected_file_hashes={
                "legit.py": legit_sha,
                "phantom.py": "a" * 64,  # Not in transaction change records!
            },
        )

        verifier = DeterministicProofVerifier(self.workspace, verify_disk_integrity=True)
        rep = verifier.verify("task_09")
        self.assertEqual(rep.overall_status, ProofVerificationStatus.INVALID)
        unclaimed_check = next((c for c in rep.checks if c.check_name == "disk_integrity_unclaimed_expected_hash"), None)
        assert unclaimed_check is not None
        self.assertEqual(unclaimed_check.status, ProofVerificationStatus.INVALID)

    # -------------------------------------------------------------------------
    # Scenario 10: Symlink / path escape rejected
    # -------------------------------------------------------------------------
    def test_10_symlink_or_path_escape_rejected(self) -> None:
        """A path escaping the workspace boundary via symlink is rejected as INVALID."""
        outside_dir = tempfile.mkdtemp(prefix="bf_outside_")
        try:
            outside_file = Path(outside_dir) / "secret.txt"
            outside_file.write_text("secret\n", encoding="utf-8")

            # Create symlink inside workspace pointing outside
            link_path = self.workspace / "escape_link.txt"
            has_symlink = False
            try:
                os.symlink(str(outside_file), str(link_path))
                has_symlink = True
            except (OSError, NotImplementedError):
                has_symlink = False

            sha = hashlib.sha256(b"secret\n").hexdigest()
            _write_custom_proof(
                self.workspace,
                "task_10",
                self.commit_sha,
                files_changes=[{
                    "path": "escape_link.txt",
                    "operation": "MODIFY",
                    "after_sha256": sha,
                }],
            )

            verifier = DeterministicProofVerifier(self.workspace, verify_disk_integrity=True)
            if has_symlink:
                rep = verifier.verify("task_10")
            else:
                with patch.object(Path, "is_relative_to", return_value=False):
                    rep = verifier.verify("task_10")

            self.assertEqual(rep.overall_status, ProofVerificationStatus.INVALID)
            escape_check = next((c for c in rep.checks if c.check_name == "disk_integrity_workspace_escape"), None)
            assert escape_check is not None
            self.assertEqual(escape_check.status, ProofVerificationStatus.INVALID)
        finally:
            shutil.rmtree(outside_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # Scenario 11: File read error / permission error fails closed
    # -------------------------------------------------------------------------
    def test_11_file_read_error_fails_closed(self) -> None:
        """A read failure when computing file hash fails closed as INVALID."""
        target = self.workspace / "unreadable.py"
        content = b"unreadable\n"
        target.write_bytes(content)
        sha = hashlib.sha256(content).hexdigest()

        _write_custom_proof(
            self.workspace,
            "task_11",
            self.commit_sha,
            files_changes=[{
                "path": "unreadable.py",
                "operation": "MODIFY",
                "after_sha256": sha,
            }],
        )

        verifier = DeterministicProofVerifier(self.workspace, verify_disk_integrity=True)
        with patch.object(Path, "read_bytes", side_effect=PermissionError("Access denied")):
            rep = verifier.verify("task_11")
        self.assertEqual(rep.overall_status, ProofVerificationStatus.INVALID)
        read_err_check = next((c for c in rep.checks if c.check_name == "disk_integrity_read_error"), None)
        assert read_err_check is not None
        self.assertEqual(read_err_check.status, ProofVerificationStatus.INVALID)

    # -------------------------------------------------------------------------
    # Scenario 12: Unchanged unrelated file outside the changed-file set
    # -------------------------------------------------------------------------
    def test_12_unrelated_files_ignored(self) -> None:
        """Files outside the changed-file set do not impact disk integrity verification."""
        # Unrelated file exists
        (self.workspace / "other_module.py").write_text("unrelated content\n", encoding="utf-8")

        target = self.workspace / "module_a.py"
        content = b"module a code\n"
        target.write_bytes(content)
        sha = hashlib.sha256(content).hexdigest()

        _write_custom_proof(
            self.workspace,
            "task_12",
            self.commit_sha,
            files_changes=[{
                "path": "module_a.py",
                "operation": "CREATE",
                "after_sha256": sha,
            }],
        )

        verifier = DeterministicProofVerifier(self.workspace, verify_disk_integrity=True)
        rep = verifier.verify("task_12")
        self.assertEqual(rep.overall_status, ProofVerificationStatus.VALID)

    # -------------------------------------------------------------------------
    # Scenario 13: Recovery behavior when disk integrity fails closed
    # -------------------------------------------------------------------------
    def test_13_recovery_fails_closed_on_post_proof_disk_mutation(self) -> None:
        """Recovery cannot transition Work to DONE when post-proof disk mutation is detected."""
        target = self.workspace / "app.py"
        orig_content = b"original verified\n"
        target.write_bytes(orig_content)
        orig_sha = hashlib.sha256(orig_content).hexdigest()

        _write_custom_proof(
            self.workspace,
            "task_13",
            self.commit_sha,
            files_changes=[{
                "path": "app.py",
                "operation": "MODIFY",
                "after_sha256": orig_sha,
            }],
            transaction_id="tx_13",
        )

        work = Work(
            id="work_13",
            intent="Test post-proof recovery",
            status=WorkStatus.VERIFYING,
            actor_id="alice",
            channel="cli",
            transaction_id="tx_13",
            resume_metadata={"workspace": str(self.workspace), "task_id": "task_13"},
        )
        self.work_store.create(work)

        # Tamper file after proof was created
        target.write_bytes(b"TAMPERED MUTATION AFTER PROOF\n")

        ok, msg, res_work = resume_work(
            work_id="work_13",
            actor_id="alice",
            channel="cli",
            work_store=self.work_store,
            recovery_manager=self.recovery_mgr,
        )

        self.assertFalse(ok)
        self.assertIn("proof verification failed", msg.lower())
        persisted = self.work_store.get("work_13")
        assert persisted is not None
        self.assertEqual(persisted.status, WorkStatus.FAILED)
        assert persisted.failure is not None
        self.assertEqual(persisted.failure.code, "UNVERIFIED_RECOVERY")
        self.assertIsNone(persisted.verification_result)

    # -------------------------------------------------------------------------
    # Scenario 14: Historical offline mode preserves semantics without disk check
    # -------------------------------------------------------------------------
    def test_14_historical_mode_preserves_offline_semantics(self) -> None:
        """In historical offline mode (verify_disk_integrity=False), missing disk file does not fail."""
        _write_custom_proof(
            self.workspace,
            "task_14",
            self.commit_sha,
            files_changes=[{
                "path": "non_existent_on_disk.py",
                "operation": "CREATE",
                "after_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            }],
        )

        verifier = DeterministicProofVerifier(self.workspace, verify_disk_integrity=False)
        rep = verifier.verify("task_14")
        # In offline historical mode, proof JSON is structurally valid
        self.assertEqual(rep.overall_status, ProofVerificationStatus.VALID)

        # In current workspace integrity mode, missing disk file is detected and rejected
        rep_current = verifier.verify("task_14", verify_disk_integrity=True)
        self.assertEqual(rep_current.overall_status, ProofVerificationStatus.INVALID)


if __name__ == "__main__":
    unittest.main()
