"""tests/test_recovery_verification_integrity.py — Regression Test Suite for Sprint 1.1.

Validates the invariant:
"No task may be reported as VERIFIED solely because the agent previously claimed success
or because a recovery path synthesized a passing result."

Covers 10 mandatory verification recovery integrity scenarios:
1. Recovery from VERIFYING with no evidence.
2. Recovery with valid, matching evidence.
3. Evidence belonging to a different task, workspace, or revision.
4. Corrupt or unreadable evidence.
5. Expired lease with a dead worker.
6. Expired lease with a worker that is still alive.
7. Cancellation during recovery.
8. Concurrent resume attempts.
9. Proof persistence failure after commit.
10. No false DONE or VERIFIED state on any failure path.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Any, Dict, Optional
import unittest

from core.runtime.approval import ApprovalService
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
from core.runtime.proof_verifier import (
    DeterministicProofVerifier,
    ProofVerificationStatus,
)
from core.runtime.transaction import (
    FileTransactionStore,
    InMemoryTransactionStore,
    OperationStatus,
    OperationType,
    RecoveryResult,
    Transaction,
    TransactionOperation,
    TransactionRecoveryManager,
    TransactionStatus,
)
from core.runtime.work import (
    FileWorkStore,
    InMemoryWorkStore,
    StaleWorkRevisionError,
    VerificationResult,
    VerificationStatus,
    Work,
    WorkFailure,
    WorkStatus,
)
from core.runtime.work_continuation import cancel_work, resume_work
from core.runtime.runtime import BrainFrogRuntime
from core.runtime.child_work import ChildWork, save_child_work, get_child_work
from core.runtime.delegation import DelegationContract
from core.runtime.subagent import Subagent
from core.runtime.capabilities import (
    Capabilities,
    FilesystemPolicy,
    ShellPolicy,
    NetworkPolicy,
    GitPolicy,
)
from core.runtime.subagent_execution import (
    SubagentExecutionCoordinator,
)


def _init_git_repo(repo_dir: Path) -> str:
    """Initialize a git repository with an initial commit and return HEAD SHA."""
    subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=repo_dir, capture_output=True, check=True)
    (repo_dir / "app.py").write_text("# initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_dir, capture_output=True, check=True)
    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True, check=True)
    return res.stdout.strip()


def _write_valid_proof(
    repo_dir: Path,
    task_id: str,
    commit_sha: str,
    transaction_id: Optional[str] = "tx_001",
    test_command: str = "pytest -q tests/test_app.py",
    verdict_status: str = "VERIFIED",
    changes_status: str = "COMMITTED",
) -> Path:
    """Write a structurally complete proof that passes DeterministicProofVerifier."""
    store = FileProofStore(repo_dir)
    app_disk = repo_dir / "app.py"
    import hashlib
    app_sha = hashlib.sha256(app_disk.read_bytes()).hexdigest() if app_disk.exists() else "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    data = {
        "version": "1.0.0",
        "task": {
            "task_id": task_id,
            "description": "Implement bugfix",
            "context": {"user": "local"},
            "constraints": [],
        },
        "verdict": {
            "status": verdict_status,
            "reason": "All 5 gates passed cleanly" if verdict_status == "VERIFIED" else "Verification failed",
            "confidence": 1.0,
            "timestamp": "2026-10-10T10:00:00Z",
        },
        "verification": {
            "test_command": test_command,
            "exit_code": 0 if verdict_status == "VERIFIED" else 1,
            "stdout_summary": "1 passed" if verdict_status == "VERIFIED" else "1 failed",
            "stderr_summary": "",
            "process_evidence": {
                "command": test_command,
                "returncode": 0 if verdict_status == "VERIFIED" else 1,
                "timed_out": False,
                "cancelled": False,
                "task_id": task_id,
                "transaction_id": transaction_id,
            },
            "gate_checks": {
                "test_suite": (verdict_status == "VERIFIED"),
                "workspace_scope": True,
                "transaction_integrity": True,
                "execution_integrity": True,
                "trust_integrity": True,
            },
        },
        "changes": {
            "status": changes_status,
            "transaction_id": transaction_id,
            "files": [
                {
                    "path": "app.py",
                    "operation": "MODIFY",
                    "sha256_before": None,
                    "sha256_after": app_sha,
                    "after_sha256": app_sha,
                    "bytes_changed": 10,
                }
            ],
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
            "expected_file_hashes": {"app.py": app_sha},
        },
    }
    pj = store.proof_json_path(task_id)
    pj.parent.mkdir(parents=True, exist_ok=True)
    pj.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return pj


class TestRecoveryVerificationIntegrity(unittest.TestCase):
    """Test suite ensuring recovery never synthesizes a passing verification result."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="bf_recovery_test_")
        self.repo_dir = Path(self.test_dir).resolve()
        self.commit_sha = _init_git_repo(self.repo_dir)
        self.work_store = FileWorkStore(repo_dir=self.repo_dir)
        self.tx_store = FileTransactionStore(workspace=self.repo_dir)
        self.recovery_mgr = TransactionRecoveryManager(workspace=self.repo_dir, store=self.tx_store)

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # Scenario 1: Recovery from VERIFYING with no evidence fails closed
    # -------------------------------------------------------------------------
    def test_scenario_1_recovery_from_verifying_with_no_evidence_fails_closed(self) -> None:
        """Work in VERIFYING state without evidence must transition to FAILED, never DONE."""
        work = Work(
            id="work_no_evidence",
            intent="Critical task",
            status=WorkStatus.VERIFYING,
            actor_id="alice",
            channel="cli",
            resume_metadata={"workspace": str(self.repo_dir)},
        )
        self.work_store.create(work)

        ok, msg, res_work = resume_work(
            work_id="work_no_evidence",
            actor_id="alice",
            channel="cli",
            work_store=self.work_store,
            recovery_manager=self.recovery_mgr,
        )

        self.assertFalse(ok)
        self.assertIn("no authoritative verification evidence", msg.lower())
        final_work = self.work_store.get("work_no_evidence")
        assert final_work is not None
        self.assertEqual(final_work.status, WorkStatus.FAILED)
        assert final_work.failure is not None
        self.assertEqual(final_work.failure.code, "UNVERIFIED_RECOVERY")
        self.assertIsNone(final_work.verification_result)

    # -------------------------------------------------------------------------
    # Scenario 2: Recovery with valid, matching evidence succeeds
    # -------------------------------------------------------------------------
    def test_scenario_2_recovery_with_valid_matching_evidence_succeeds(self) -> None:
        """Work in VERIFYING state with a valid, verified proof artifact advances to DONE."""
        task_id = "task_valid_002"
        tx_id = "tx_valid_002"
        _write_valid_proof(self.repo_dir, task_id, self.commit_sha, transaction_id=tx_id)

        work = Work(
            id="work_valid_evidence",
            intent="Critical task",
            status=WorkStatus.VERIFYING,
            actor_id="alice",
            channel="cli",
            transaction_id=tx_id,
            resume_metadata={
                "workspace": str(self.repo_dir),
                "task_id": task_id,
            },
        )
        self.work_store.create(work)

        ok, msg, res_work = resume_work(
            work_id="work_valid_evidence",
            actor_id="alice",
            channel="cli",
            work_store=self.work_store,
            recovery_manager=self.recovery_mgr,
        )

        self.assertTrue(ok)
        self.assertIn("now done", msg.lower())
        final_work = self.work_store.get("work_valid_evidence")
        assert final_work is not None
        self.assertEqual(final_work.status, WorkStatus.DONE)
        assert final_work.verification_result is not None
        self.assertEqual(final_work.verification_result.status, VerificationStatus.PASS)
        self.assertIn(task_id, final_work.verification_result.summary)

    # -------------------------------------------------------------------------
    # Scenario 3: Evidence belonging to different task, workspace, or revision
    # -------------------------------------------------------------------------
    def test_scenario_3_evidence_mismatch_fails_closed(self) -> None:
        """Evidence with mismatched transaction ID fails closed and transitions to FAILED."""
        task_id = "task_mismatch_003"
        _write_valid_proof(self.repo_dir, task_id, self.commit_sha, transaction_id="tx_other")

        work = Work(
            id="work_mismatch",
            intent="Mismatched task",
            status=WorkStatus.VERIFYING,
            actor_id="alice",
            channel="cli",
            transaction_id="tx_expected_003",
            resume_metadata={
                "workspace": str(self.repo_dir),
                "task_id": task_id,
            },
        )
        self.work_store.create(work)

        ok, msg, res_work = resume_work(
            work_id="work_mismatch",
            actor_id="alice",
            channel="cli",
            work_store=self.work_store,
            recovery_manager=self.recovery_mgr,
        )

        self.assertFalse(ok)
        self.assertIn("does not match", msg.lower())
        final_work = self.work_store.get("work_mismatch")
        assert final_work is not None
        self.assertEqual(final_work.status, WorkStatus.FAILED)
        assert final_work.failure is not None
        self.assertEqual(final_work.failure.code, "EVIDENCE_MISMATCH")

    # -------------------------------------------------------------------------
    # Scenario 4: Corrupt or unreadable evidence
    # -------------------------------------------------------------------------
    def test_scenario_4_corrupt_evidence_fails_closed(self) -> None:
        """Corrupted proof artifact JSON fails closed and transitions to FAILED."""
        proof_store = FileProofStore(self.repo_dir)
        pj = proof_store.proof_json_path("task_corrupt_004")
        pj.parent.mkdir(parents=True, exist_ok=True)
        pj.write_text("{ broken invalid json ...", encoding="utf-8")

        work = Work(
            id="work_corrupt",
            intent="Corrupt proof task",
            status=WorkStatus.VERIFYING,
            actor_id="alice",
            channel="cli",
            resume_metadata={
                "workspace": str(self.repo_dir),
                "task_id": "task_corrupt_004",
            },
        )
        self.work_store.create(work)

        ok, msg, res_work = resume_work(
            work_id="work_corrupt",
            actor_id="alice",
            channel="cli",
            work_store=self.work_store,
            recovery_manager=self.recovery_mgr,
        )

        self.assertFalse(ok)
        self.assertIn("proof verification failed", msg.lower())
        final_work = self.work_store.get("work_corrupt")
        assert final_work is not None
        self.assertEqual(final_work.status, WorkStatus.FAILED)
        assert final_work.failure is not None
        self.assertEqual(final_work.failure.code, "UNVERIFIED_RECOVERY")

    # -------------------------------------------------------------------------
    # Scenario 5: Expired lease with a dead worker
    # -------------------------------------------------------------------------
    def test_scenario_5_expired_lease_with_dead_worker(self) -> None:
        """Expired lease with dead PID reclaims safely, and fails closed if no proof exists."""
        work = Work(
            id="work_dead_worker",
            intent="Crashed worker task",
            status=WorkStatus.VERIFYING,
            actor_id="alice",
            channel="cli",
            resume_metadata={
                "workspace": str(self.repo_dir),
                "claim_active": True,
                "resumed_at": time.time() - 60.0,  # Expired lease
                "active_pid": 99999999,  # Definitely non-existent dead PID
            },
        )
        self.work_store.create(work)

        ok, msg, res_work = resume_work(
            work_id="work_dead_worker",
            actor_id="alice",
            channel="cli",
            work_store=self.work_store,
            recovery_manager=self.recovery_mgr,
        )

        self.assertFalse(ok)
        self.assertIn("no authoritative verification evidence", msg.lower())
        final_work = self.work_store.get("work_dead_worker")
        assert final_work is not None
        self.assertEqual(final_work.status, WorkStatus.FAILED)
        self.assertEqual(final_work.failure.code, "UNVERIFIED_RECOVERY")

    # -------------------------------------------------------------------------
    # Scenario 6: Expired lease with a worker that is still alive
    # -------------------------------------------------------------------------
    def test_scenario_6_stale_worker_cannot_overwrite_newer_owner(self) -> None:
        """When newer owner reclaims an expired lease, stale worker's save is rejected by OCC."""
        # Step 1: Initial work claimed by Worker 1
        work_orig = Work(
            id="work_stale_occ",
            intent="Stale worker concurrency",
            status=WorkStatus.VERIFYING,
            actor_id="alice",
            channel="cli",
            resume_metadata={
                "workspace": str(self.repo_dir),
                "claim_active": True,
                "claim_id": "worker_1_token",
                "resumed_at": time.time() - 45.0,  # Expired lease (> 30s)
                "active_pid": os.getpid(),  # Live PID
            },
            revision=2,
        )
        self.work_store.create(work_orig)

        # Worker 1 holds a reference to work_orig with revision=2
        worker_1_snapshot = self.work_store.get("work_stale_occ")
        assert worker_1_snapshot is not None
        self.assertEqual(worker_1_snapshot.revision, 2)

        # Step 2: Worker 2 resumes (lease expired). Reclaims ownership, failing closed due to no proof.
        ok, msg, res_work = resume_work(
            work_id="work_stale_occ",
            actor_id="bob",
            channel="cli",
            work_store=self.work_store,
            recovery_manager=self.recovery_mgr,
        )
        self.assertFalse(ok)

        # Store is now at revision >= 3 and in status FAILED
        current_store_work = self.work_store.get("work_stale_occ")
        assert current_store_work is not None
        self.assertGreater(current_store_work.revision, 2)
        self.assertEqual(current_store_work.status, WorkStatus.FAILED)

        # Step 3: Stale Worker 1 wakes up and attempts to save its stale state (revision=2)
        stale_transition = worker_1_snapshot.transition(
            WorkStatus.DONE,
            verification_result=VerificationResult(status=VerificationStatus.PASS, summary="Stale pass"),
        )
        with self.assertRaises(StaleWorkRevisionError):
            self.work_store.save(stale_transition)

        # Confirm store still has FAILED state from newer owner
        final_work = self.work_store.get("work_stale_occ")
        assert final_work is not None
        self.assertEqual(final_work.status, WorkStatus.FAILED)

    # -------------------------------------------------------------------------
    # Scenario 7: Cancellation during recovery
    # -------------------------------------------------------------------------
    def test_scenario_7_cancellation_during_recovery(self) -> None:
        """Cancelled work cannot be resumed or marked DONE by any recovery path."""
        work = Work(
            id="work_cancel_rec",
            intent="Cancel test",
            status=WorkStatus.APPROVAL_REQUIRED,
            actor_id="alice",
            channel="cli",
            resume_metadata={"workspace": str(self.repo_dir)},
        )
        self.work_store.create(work)

        # Cancel work
        can_ok, can_msg, can_work = cancel_work(
            work_id="work_cancel_rec",
            actor_id="alice",
            channel="cli",
            work_store=self.work_store,
            reason="User abort",
        )
        self.assertTrue(can_ok)
        assert can_work is not None
        self.assertEqual(can_work.status, WorkStatus.CANCELLED)

        # Attempt to resume cancelled work
        ok, msg, res_work = resume_work(
            work_id="work_cancel_rec",
            actor_id="alice",
            channel="cli",
            work_store=self.work_store,
            recovery_manager=self.recovery_mgr,
        )
        self.assertFalse(ok)
        self.assertIn("cannot be resumed", msg.lower())
        final_work = self.work_store.get("work_cancel_rec")
        assert final_work is not None
        self.assertEqual(final_work.status, WorkStatus.CANCELLED)

    # -------------------------------------------------------------------------
    # Scenario 8: Concurrent resume attempts
    # -------------------------------------------------------------------------
    def test_scenario_8_concurrent_resume_attempts(self) -> None:
        """Only one concurrent resume attempt can claim the lease and process recovery."""
        task_id = "task_concurrent_008"
        tx_id = "tx_concurrent_008"
        _write_valid_proof(self.repo_dir, task_id, self.commit_sha, transaction_id=tx_id)

        work = Work(
            id="work_concurrent",
            intent="Concurrent test",
            status=WorkStatus.VERIFYING,
            actor_id="alice",
            channel="cli",
            transaction_id=tx_id,
            resume_metadata={
                "workspace": str(self.repo_dir),
                "task_id": task_id,
            },
        )
        self.work_store.create(work)

        results = []
        def run_resume():
            return resume_work(
                work_id="work_concurrent",
                actor_id="alice",
                channel="cli",
                work_store=self.work_store,
                recovery_manager=self.recovery_mgr,
            )[:2]

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futs = [executor.submit(run_resume) for _ in range(8)]
            for f in concurrent.futures.as_completed(futs):
                results.append(f.result())

        # Exactly 1 success (transitioned to DONE with verified proof)
        successes = [r for r in results if r[0] is True]
        self.assertEqual(len(successes), 1)

        # Exactly 7 rejections (work is already completed or active)
        rejections = [r for r in results if r[0] is False]
        self.assertEqual(len(rejections), 7)

        final_work = self.work_store.get("work_concurrent")
        assert final_work is not None
        self.assertEqual(final_work.status, WorkStatus.DONE)

    # -------------------------------------------------------------------------
    # Scenario 9: Proof persistence failure after commit
    # -------------------------------------------------------------------------
    def test_scenario_9_proof_persistence_failure_after_commit(self) -> None:
        """When transaction committed but proof is absent, recovery fails closed, never DONE."""
        tx = Transaction(
            id="tx_comm_no_proof",
            session_id="sess_p9",
            status=TransactionStatus.COMMITTED,
            work_id="work_comm_no_proof",
        )
        self.tx_store.save(tx)

        work = Work(
            id="work_comm_no_proof",
            intent="Committed but unproven",
            status=WorkStatus.EXECUTING,
            actor_id="alice",
            channel="cli",
            transaction_id="tx_comm_no_proof",
            resume_metadata={"workspace": str(self.repo_dir)},
        )
        self.work_store.create(work)

        # 1. Recover in-flight committed transaction -> transitions EXECUTING to VERIFYING
        ok1, msg1, w1 = resume_work(
            work_id="work_comm_no_proof",
            actor_id="alice",
            channel="cli",
            work_store=self.work_store,
            transaction_store=self.tx_store,
            recovery_manager=self.recovery_mgr,
        )
        self.assertTrue(ok1)
        self.assertIn("transitioned to verifying", msg1.lower())
        assert w1 is not None
        self.assertEqual(w1.status, WorkStatus.VERIFYING)

        # 2. Simulate worker crash during verification (dead PID / expired lease)
        curr = self.work_store.get("work_comm_no_proof")
        assert curr is not None
        meta = dict(curr.resume_metadata or {})
        meta["active_pid"] = 99999999
        meta["claim_active"] = False
        meta["resumed_at"] = time.time() - 3600
        self.work_store.save(curr.with_update(resume_metadata=meta))

        # Re-attempting resume on crashed VERIFYING with missing proof fails closed!
        ok2, msg2, w2 = resume_work(
            work_id="work_comm_no_proof",
            actor_id="alice",
            channel="cli",
            work_store=self.work_store,
            transaction_store=self.tx_store,
            recovery_manager=self.recovery_mgr,
        )
        self.assertFalse(ok2)
        self.assertIn("no authoritative verification evidence", msg2.lower())
        final_work = self.work_store.get("work_comm_no_proof")
        assert final_work is not None
        self.assertEqual(final_work.status, WorkStatus.FAILED)
        self.assertEqual(final_work.failure.code, "UNVERIFIED_RECOVERY")

    # -------------------------------------------------------------------------
    # Scenario 10: No false DONE or VERIFIED state on any failure path
    # -------------------------------------------------------------------------
    def test_scenario_10_no_false_done_or_verified_on_any_failure_path(self) -> None:
        """Comprehensive matrix: under no error condition may a task become DONE or VERIFIED."""
        failure_matrix = [
            # (proof_exists, verdict_status, exit_code, expected_failure_code)
            (False, None, None, "UNVERIFIED_RECOVERY"),
            (True, "FAILED", 1, "UNVERIFIED_RECOVERY"),
            (True, "CANCELLED", None, "UNVERIFIED_RECOVERY"),
            (True, "RECOVERY_REQUIRED", 1, "UNVERIFIED_RECOVERY"),
        ]

        for i, (proof_exists, v_status, exit_c, exp_fail) in enumerate(failure_matrix):
            wid = f"work_matrix_{i}"
            tid = f"task_matrix_{i}"
            txid = f"tx_matrix_{i}"

            if proof_exists and v_status is not None:
                _write_valid_proof(
                    self.repo_dir,
                    tid,
                    self.commit_sha,
                    transaction_id=txid,
                    verdict_status=v_status,
                )

            w = Work(
                id=wid,
                intent=f"Matrix {i}",
                status=WorkStatus.VERIFYING,
                actor_id="alice",
                channel="cli",
                transaction_id=txid,
                resume_metadata={
                    "workspace": str(self.repo_dir),
                    "task_id": tid,
                },
            )
            self.work_store.create(w)

            ok, msg, res = resume_work(
                work_id=wid,
                actor_id="alice",
                channel="cli",
                work_store=self.work_store,
                recovery_manager=self.recovery_mgr,
            )

            self.assertFalse(ok, f"Matrix {i} unexpectedly returned ok=True")
            persisted = self.work_store.get(wid)
            assert persisted is not None
            self.assertNotEqual(persisted.status, WorkStatus.DONE, f"Matrix {i} false DONE")
            self.assertEqual(persisted.status, WorkStatus.FAILED, f"Matrix {i} expected FAILED")
            assert persisted.failure is not None
            self.assertEqual(persisted.failure.code, exp_fail)
            # Must not have a passing verification result
            if persisted.verification_result is not None:
                self.assertNotEqual(persisted.verification_result.status, VerificationStatus.PASS)

    # =========================================================================
    # Phase D — Targeted Regression Tests (1 - 12)
    # =========================================================================

    def test_regression_01_structurally_valid_proof_with_failed_verdict_and_rolled_back_tx(self) -> None:
        """Requirement 1: A structurally VALID proof with verdict FAILED and transaction ROLLED_BACK."""
        task_id = "task_p0_01"
        tx_id = "tx_p0_01"
        _write_valid_proof(
            self.repo_dir,
            task_id,
            self.commit_sha,
            transaction_id=tx_id,
            verdict_status="FAILED",
            changes_status="ROLLED_BACK",
        )
        # Verify that DeterministicProofVerifier reports VALID overall structure
        verifier = DeterministicProofVerifier(self.repo_dir)
        rep = verifier.verify(task_id)
        self.assertEqual(rep.overall_status, ProofVerificationStatus.VALID)
        self.assertEqual(rep.verdict_status, "FAILED")

        work = Work(
            id="work_p0_01",
            intent="Regression 1",
            status=WorkStatus.VERIFYING,
            actor_id="alice",
            channel="cli",
            transaction_id=tx_id,
            resume_metadata={"workspace": str(self.repo_dir), "task_id": task_id},
        )
        self.work_store.create(work)

        ok, msg, res_work = resume_work(
            work_id="work_p0_01",
            actor_id="alice",
            channel="cli",
            work_store=self.work_store,
            recovery_manager=self.recovery_mgr,
        )
        self.assertFalse(ok)
        self.assertIn("proof verification failed", msg.lower())
        persisted = self.work_store.get("work_p0_01")
        assert persisted is not None
        self.assertEqual(persisted.status, WorkStatus.FAILED)
        assert persisted.failure is not None
        self.assertEqual(persisted.failure.code, "UNVERIFIED_RECOVERY")
        self.assertIsNone(persisted.verification_result)

    def test_regression_02_valid_proof_task_a_supplied_while_recovering_task_b(self) -> None:
        """Requirement 2: A valid proof belonging to task A supplied while recovering task B."""
        task_a = "task_p0_02_A"
        tx_a = "tx_p0_02_A"
        proof_a_path = _write_valid_proof(self.repo_dir, task_a, self.commit_sha, transaction_id=tx_a)

        work_b = Work(
            id="work_p0_02_B",
            intent="Regression 2",
            status=WorkStatus.VERIFYING,
            actor_id="alice",
            channel="cli",
            transaction_id="tx_p0_02_B",
            resume_metadata={
                "workspace": str(self.repo_dir),
                "task_id": "task_p0_02_B",
                "proof_path": str(proof_a_path),
            },
        )
        self.work_store.create(work_b)

        ok, msg, res_work = resume_work(
            work_id="work_p0_02_B",
            actor_id="alice",
            channel="cli",
            work_store=self.work_store,
            recovery_manager=self.recovery_mgr,
        )
        self.assertFalse(ok)
        self.assertIn("does not match", msg.lower())
        persisted = self.work_store.get("work_p0_02_B")
        assert persisted is not None
        self.assertEqual(persisted.status, WorkStatus.FAILED)
        assert persisted.failure is not None
        self.assertEqual(persisted.failure.code, "EVIDENCE_MISMATCH")

    def test_regression_03_work_with_tx_id_paired_with_null_proof_tx_id(self) -> None:
        """Requirement 3: A Work with a transaction ID paired with a proof whose transaction ID is null."""
        task_id = "task_p0_03"
        _write_valid_proof(
            self.repo_dir,
            task_id,
            self.commit_sha,
            transaction_id=None,
        )
        work = Work(
            id="work_p0_03",
            intent="Regression 3",
            status=WorkStatus.VERIFYING,
            actor_id="alice",
            channel="cli",
            transaction_id="tx_expected_03",
            resume_metadata={"workspace": str(self.repo_dir), "task_id": task_id},
        )
        self.work_store.create(work)

        ok, msg, res_work = resume_work(
            work_id="work_p0_03",
            actor_id="alice",
            channel="cli",
            work_store=self.work_store,
            recovery_manager=self.recovery_mgr,
        )
        self.assertFalse(ok)
        self.assertIn("does not match", msg.lower())
        persisted = self.work_store.get("work_p0_03")
        assert persisted is not None
        self.assertEqual(persisted.status, WorkStatus.FAILED)
        assert persisted.failure is not None
        self.assertEqual(persisted.failure.code, "EVIDENCE_MISMATCH")

    def test_regression_04_cross_workspace_proof_presented_during_recovery_of_work_in_workspace_b(self) -> None:
        """Requirement 4: A proof from Workspace A presented during recovery of a Work in Workspace B."""
        temp_dir_a = tempfile.mkdtemp(prefix="bf_ws_a_")
        try:
            ws_a = Path(temp_dir_a).resolve()
            sha_a = _init_git_repo(ws_a)
            task_id = "task_p0_04"
            tx_id = "tx_p0_04"
            proof_a_path = _write_valid_proof(ws_a, task_id, sha_a, transaction_id=tx_id)

            rec_mgr_a = TransactionRecoveryManager(workspace=ws_a, store=FileTransactionStore(workspace=ws_a))
            work_b = Work(
                id="work_p0_04",
                intent="Regression 4",
                status=WorkStatus.VERIFYING,
                actor_id="alice",
                channel="cli",
                transaction_id=tx_id,
                resume_metadata={"workspace": str(self.repo_dir), "task_id": task_id},
            )
            self.work_store.create(work_b)

            ok, msg, res_work = resume_work(
                work_id="work_p0_04",
                actor_id="alice",
                channel="cli",
                work_store=self.work_store,
                recovery_manager=rec_mgr_a,
            )
            self.assertFalse(ok)
            self.assertIn("workspace mismatch", msg.lower())
            persisted = self.work_store.get("work_p0_04")
            assert persisted is not None
            self.assertEqual(persisted.status, WorkStatus.FAILED)
            assert persisted.failure is not None
            self.assertEqual(persisted.failure.code, "EVIDENCE_MISMATCH")
        finally:
            shutil.rmtree(temp_dir_a, ignore_errors=True)

    def test_regression_05_startup_reconciliation_of_failed_proof(self) -> None:
        """Requirement 5: Startup reconciliation of a FAILED proof fails closed."""
        task_id = "task_p0_05"
        tx_id = "tx_p0_05"
        _write_valid_proof(
            self.repo_dir,
            task_id,
            self.commit_sha,
            transaction_id=tx_id,
            verdict_status="FAILED",
            changes_status="ROLLED_BACK",
        )
        tx = Transaction(id=tx_id, work_id="work_p0_05", workspace=str(self.repo_dir), status=TransactionStatus.COMMITTED)
        self.tx_store.save(tx)

        work = Work(
            id="work_p0_05",
            intent="Regression 5",
            status=WorkStatus.VERIFYING,
            transaction_id=tx_id,
            actor_id="alice",
            channel="cli",
            resume_metadata={"task_id": task_id, "workspace": str(self.repo_dir)},
        )
        self.work_store.create(work)

        runtime = BrainFrogRuntime(repo_dir=self.repo_dir, persist_sessions=False, auto_recover_transactions=False)
        runtime.startup_recovery_results = [
            RecoveryResult(
                transaction_id=tx_id,
                previous_status=TransactionStatus.COMMITTED,
                final_status=TransactionStatus.COMMITTED,
                recovered=True,
                rolled_back=False,
            )
        ]
        runtime._reconcile_recovered_work()

        final_work = runtime.work_store.get("work_p0_05")
        assert final_work is not None
        self.assertEqual(final_work.status, WorkStatus.FAILED)
        assert final_work.failure is not None
        self.assertEqual(final_work.failure.code, "UNVERIFIED_RECOVERY")
        self.assertIn("proof verification failed", final_work.failure.summary.lower())

    def test_regression_06_child_work_crash_recovery_with_missing_verification_evidence(self) -> None:
        """Requirement 6: ChildWork crash recovery with missing verification evidence fails closed."""
        now = time.time()
        parent = Work(id="p_cw_06", intent="Parent", actor_id="alice", channel="cli", session_id="sess_06", session_incarnation_id="inc_06")
        self.work_store.create(parent)
        sub = Subagent(subagent_id="sub_06", parent_work_id=parent.id, session_id="sess_06", session_incarnation_id="inc_06", actor="alice", role="role", purpose="purpose", created_at=now, updated_at=now)
        caps = Capabilities(filesystem=FilesystemPolicy(read=("app.py",), write=("app.py",)), shell=ShellPolicy(execute=False), network=NetworkPolicy(access=False), git=GitPolicy(read=True, commit=False))
        delg = DelegationContract(delegation_id="delg_06", parent_work_id=parent.id, child_subagent_id=sub.id, actor="alice", session_id="sess_06", session_incarnation_id="inc_06", capabilities=caps, target_scope=("app.py",), expires_at=now + 600, created_at=now)
        cw = ChildWork.create(parent_work=parent, subagent=sub, delegation=delg, child_work_id="cw_p0_06", created_at=now)
        save_child_work(self.work_store, cw)

        cw = save_child_work(self.work_store, cw.transition(WorkStatus.PLANNING))
        cw = save_child_work(self.work_store, cw.transition(WorkStatus.APPROVAL_REQUIRED))
        cw = save_child_work(self.work_store, cw.transition(WorkStatus.EXECUTING, transaction_id="tx_cw_06"))

        op = TransactionOperation(type=OperationType.MODIFY_FILE, target="app.py", status=OperationStatus.EXECUTED, before_state={"exists": True}, after_state={"exists": True})
        tx = Transaction(id="tx_cw_06", work_id=cw.id, workspace=str(self.repo_dir), status=TransactionStatus.EXECUTING, operations=[op], verification_result=None)
        self.tx_store.save(tx)

        coord = SubagentExecutionCoordinator(work_store=self.work_store, repo_dir=self.repo_dir, transaction_store=self.tx_store)
        res = coord.reconcile_child(cw.id)

        self.assertFalse(res.success)
        self.assertEqual(res.status, WorkStatus.FAILED)
        loaded_cw = self.work_store.get(cw.id)
        assert loaded_cw is not None
        self.assertEqual(loaded_cw.status, WorkStatus.FAILED)
        assert loaded_cw.failure is not None
        self.assertEqual(loaded_cw.failure.code, "UNVERIFIED_CHILD_RECOVERY")

    def test_regression_07_child_work_crash_recovery_with_invalid_evidence(self) -> None:
        """Requirement 7: ChildWork crash recovery with invalid evidence fails closed."""
        now = time.time()
        parent = Work(id="p_cw_07", intent="Parent", actor_id="alice", channel="cli", session_id="sess_07", session_incarnation_id="inc_07")
        self.work_store.create(parent)
        sub = Subagent(subagent_id="sub_07", parent_work_id=parent.id, session_id="sess_07", session_incarnation_id="inc_07", actor="alice", role="role", purpose="purpose", created_at=now, updated_at=now)
        caps = Capabilities(filesystem=FilesystemPolicy(read=("app.py",), write=("app.py",)), shell=ShellPolicy(execute=False), network=NetworkPolicy(access=False), git=GitPolicy(read=True, commit=False))
        delg = DelegationContract(delegation_id="delg_07", parent_work_id=parent.id, child_subagent_id=sub.id, actor="alice", session_id="sess_07", session_incarnation_id="inc_07", capabilities=caps, target_scope=("app.py",), expires_at=now + 600, created_at=now)
        cw = ChildWork.create(parent_work=parent, subagent=sub, delegation=delg, child_work_id="cw_p0_07", created_at=now)
        save_child_work(self.work_store, cw)

        cw = save_child_work(self.work_store, cw.transition(WorkStatus.PLANNING))
        cw = save_child_work(self.work_store, cw.transition(WorkStatus.APPROVAL_REQUIRED))
        cw = save_child_work(self.work_store, cw.transition(WorkStatus.EXECUTING, transaction_id="tx_cw_07"))

        op = TransactionOperation(type=OperationType.MODIFY_FILE, target="app.py", status=OperationStatus.EXECUTED, before_state={"exists": True}, after_state={"exists": True})
        tx = Transaction(
            id="tx_cw_07",
            work_id=cw.id,
            workspace=str(self.repo_dir),
            status=TransactionStatus.EXECUTING,
            operations=[op],
            verification_result={"status": "FAIL", "passed": False, "failed": 1},
        )
        self.tx_store.save(tx)

        coord = SubagentExecutionCoordinator(work_store=self.work_store, repo_dir=self.repo_dir, transaction_store=self.tx_store)
        res = coord.reconcile_child(cw.id)

        self.assertFalse(res.success)
        self.assertEqual(res.status, WorkStatus.FAILED)
        loaded_cw = self.work_store.get(cw.id)
        assert loaded_cw is not None
        self.assertEqual(loaded_cw.status, WorkStatus.FAILED)
        assert loaded_cw.failure is not None
        self.assertEqual(loaded_cw.failure.code, "UNVERIFIED_CHILD_RECOVERY")

    def test_regression_08_final_save_occ_collision_where_durable_work_remains_verifying(self) -> None:
        """Requirement 8: A final-save OCC collision where the durable Work remains VERIFYING."""
        task_id = "task_p0_08"
        tx_id = "tx_p0_08"
        _write_valid_proof(self.repo_dir, task_id, self.commit_sha, transaction_id=tx_id)

        work = Work(
            id="work_p0_08",
            intent="Regression 8",
            status=WorkStatus.VERIFYING,
            actor_id="alice",
            channel="cli",
            transaction_id=tx_id,
            resume_metadata={"workspace": str(self.repo_dir), "task_id": task_id},
        )
        self.work_store.create(work)

        orig_save = self.work_store.save
        def mock_save(work_obj, expected_revision=None):
            if work_obj.status == WorkStatus.DONE:
                raise StaleWorkRevisionError("Simulated final save collision")
            return orig_save(work_obj, expected_revision=expected_revision)

        self.work_store.save = mock_save

        ok, msg, res_work = resume_work(
            work_id="work_p0_08",
            actor_id="alice",
            channel="cli",
            work_store=self.work_store,
            recovery_manager=self.recovery_mgr,
        )
        self.assertFalse(ok)
        self.assertIn("conflict", msg.lower())
        persisted = self.work_store.get("work_p0_08")
        assert persisted is not None
        self.assertEqual(persisted.status, WorkStatus.VERIFYING)
        self.assertIsNone(persisted.verification_result)

    def test_regression_09_valid_proof_with_matching_task_transaction_workspace_and_verified_verdict(self) -> None:
        """Requirement 9: A valid proof with matching task, transaction, workspace, and VERIFIED verdict."""
        task_id = "task_p0_09"
        tx_id = "tx_p0_09"
        _write_valid_proof(self.repo_dir, task_id, self.commit_sha, transaction_id=tx_id, verdict_status="VERIFIED")

        work = Work(
            id="work_p0_09",
            intent="Regression 9",
            status=WorkStatus.VERIFYING,
            actor_id="alice",
            channel="cli",
            transaction_id=tx_id,
            resume_metadata={"workspace": str(self.repo_dir), "task_id": task_id},
        )
        self.work_store.create(work)

        ok, msg, res_work = resume_work(
            work_id="work_p0_09",
            actor_id="alice",
            channel="cli",
            work_store=self.work_store,
            recovery_manager=self.recovery_mgr,
        )
        self.assertTrue(ok)
        self.assertIn("now done", msg.lower())
        persisted = self.work_store.get("work_p0_09")
        assert persisted is not None
        self.assertEqual(persisted.status, WorkStatus.DONE)
        assert persisted.verification_result is not None
        self.assertEqual(persisted.verification_result.status, VerificationStatus.PASS)

    def test_regression_10_corrupt_or_missing_proof_evidence(self) -> None:
        """Requirement 10: Corrupt or missing proof evidence fails closed."""
        # Part A: Missing
        work_missing = Work(
            id="work_p0_10_miss",
            intent="Regression 10 Missing",
            status=WorkStatus.VERIFYING,
            actor_id="alice",
            channel="cli",
            resume_metadata={"workspace": str(self.repo_dir), "task_id": "non_existent_10"},
        )
        self.work_store.create(work_missing)
        ok1, msg1, _ = resume_work(work_id="work_p0_10_miss", actor_id="alice", channel="cli", work_store=self.work_store, recovery_manager=self.recovery_mgr)
        self.assertFalse(ok1)
        persisted1 = self.work_store.get("work_p0_10_miss")
        assert persisted1 is not None
        self.assertEqual(persisted1.status, WorkStatus.FAILED)
        self.assertEqual(persisted1.failure.code, "UNVERIFIED_RECOVERY")

        # Part B: Corrupted
        pj = FileProofStore(self.repo_dir).proof_json_path("corrupt_task_10")
        pj.parent.mkdir(parents=True, exist_ok=True)
        pj.write_text("{ not json at all...", encoding="utf-8")
        work_corrupt = Work(
            id="work_p0_10_corr",
            intent="Regression 10 Corrupt",
            status=WorkStatus.VERIFYING,
            actor_id="alice",
            channel="cli",
            resume_metadata={"workspace": str(self.repo_dir), "task_id": "corrupt_task_10"},
        )
        self.work_store.create(work_corrupt)
        ok2, msg2, _ = resume_work(work_id="work_p0_10_corr", actor_id="alice", channel="cli", work_store=self.work_store, recovery_manager=self.recovery_mgr)
        self.assertFalse(ok2)
        persisted2 = self.work_store.get("work_p0_10_corr")
        assert persisted2 is not None
        self.assertEqual(persisted2.status, WorkStatus.FAILED)
        self.assertEqual(persisted2.failure.code, "UNVERIFIED_RECOVERY")

    def test_regression_11_concurrent_recovery_attempts_and_stale_worker_writes(self) -> None:
        """Requirement 11: Concurrent recovery attempts and stale-worker writes."""
        task_id = "task_p0_11"
        tx_id = "tx_p0_11"
        _write_valid_proof(self.repo_dir, task_id, self.commit_sha, transaction_id=tx_id)

        work = Work(
            id="work_p0_11",
            intent="Regression 11",
            status=WorkStatus.VERIFYING,
            actor_id="alice",
            channel="cli",
            transaction_id=tx_id,
            resume_metadata={"workspace": str(self.repo_dir), "task_id": task_id},
            revision=2,
        )
        self.work_store.create(work)

        results = []
        def attempt():
            return resume_work(work_id="work_p0_11", actor_id="alice", channel="cli", work_store=self.work_store, recovery_manager=self.recovery_mgr)[:2]

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
            futs = [ex.submit(attempt) for _ in range(5)]
            for f in concurrent.futures.as_completed(futs):
                results.append(f.result())

        self.assertEqual(len([r for r in results if r[0] is True]), 1)
        persisted = self.work_store.get("work_p0_11")
        assert persisted is not None
        self.assertEqual(persisted.status, WorkStatus.DONE)

        # Stale worker write
        stale_w = work.transition(WorkStatus.FAILED, failure=WorkFailure(code="STALE", summary="stale"))
        with self.assertRaises(StaleWorkRevisionError):
            self.work_store.save(stale_w, expected_revision=2)

    def test_regression_12_proof_persistence_or_finalization_failure_after_transaction_commit(self) -> None:
        """Requirement 12: Proof persistence or finalization failure after transaction commit."""
        tx_id = "tx_p0_12"
        tx = Transaction(id=tx_id, work_id="work_p0_12", workspace=str(self.repo_dir), status=TransactionStatus.COMMITTED)
        self.tx_store.save(tx)

        work = Work(
            id="work_p0_12",
            intent="Regression 12",
            status=WorkStatus.EXECUTING,
            actor_id="alice",
            channel="cli",
            transaction_id=tx_id,
            resume_metadata={"workspace": str(self.repo_dir)},
        )
        self.work_store.create(work)

        # Resuming in EXECUTING with committed tx transitions to VERIFYING
        ok1, msg1, w1 = resume_work(work_id="work_p0_12", actor_id="alice", channel="cli", work_store=self.work_store, transaction_store=self.tx_store, recovery_manager=self.recovery_mgr)
        self.assertTrue(ok1)
        self.assertEqual(w1.status, WorkStatus.VERIFYING)

        # Simulate crash during verification (worker dead / lease expired)
        curr = self.work_store.get("work_p0_12")
        assert curr is not None
        meta = dict(curr.resume_metadata or {})
        meta["active_pid"] = 99999999
        meta["claim_active"] = False
        meta["resumed_at"] = time.time() - 3600
        self.work_store.save(curr.with_update(resume_metadata=meta))

        # Subsequent resume with missing proof fails closed
        ok2, msg2, w2 = resume_work(work_id="work_p0_12", actor_id="alice", channel="cli", work_store=self.work_store, transaction_store=self.tx_store, recovery_manager=self.recovery_mgr)
        self.assertFalse(ok2)
        persisted = self.work_store.get("work_p0_12")
        assert persisted is not None
        self.assertEqual(persisted.status, WorkStatus.FAILED)
        self.assertEqual(persisted.failure.code, "UNVERIFIED_RECOVERY")


if __name__ == "__main__":
    unittest.main()
