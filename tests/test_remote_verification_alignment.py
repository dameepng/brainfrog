"""Phase 1.1.4B Remote Verification Alignment adversarial regression suite.

Verifies the product invariant:
BrainFrog must never present a remote coding task as verified success
unless authoritative verification evidence supports that verdict.
Task -> Plan -> Change -> Verify -> Prove.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.runtime.approval import ApprovalService, InMemoryApprovalStore
from core.runtime.messages import IncomingMessage
from core.runtime.proof import FileProofStore, ProofArtifact
from core.runtime.proof_verifier import DeterministicProofVerifier, ProofVerificationStatus
from core.runtime.remote_work import RemoteWorkCoordinator
from core.runtime.runtime import BrainFrogRuntime
from core.runtime.session import SessionManager
from core.runtime.transaction import (
    FileTransactionStore,
    TransactionRecoveryManager,
    TransactionStatus,
)
from core.runtime.work import InMemoryWorkStore, Work, WorkFailure, WorkStatus
from core.runtime.work_continuation import resume_work
from tests.test_approval_execution_contract import MockSystem1, MockSystem2


class TestRemoteVerificationAlignment(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.repo = Path(self.temp_dir).resolve()
        self.work_store = InMemoryWorkStore()
        self.tx_store = FileTransactionStore(self.repo)
        self.approval_store = InMemoryApprovalStore()
        self.approval_service = ApprovalService(self.approval_store)
        self.session_mgr = SessionManager()
        (self.repo / "test_noop.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_runtime(
        self,
        output: dict[str, str] | None = None,
        default_test_cmd: str | None = 'python -m unittest test_noop.py',
        verifier=None,
    ) -> BrainFrogRuntime:
        out_files = output if output is not None else {"safe_mod.py": "def add(a, b): return a + b\n"}
        return BrainFrogRuntime(
            repo_dir=self.repo,
            require_approval=True,
            persist_sessions=False,
            sessions=self.session_mgr,
            work_store=self.work_store,
            approval_service=self.approval_service,
            transaction_store=self.tx_store,
            transaction_verifier=verifier,
            system1_factory=lambda _: MockSystem1(),
            system2_factory=lambda **_: MockSystem2(out_files),
            default_test_cmd=default_test_cmd,
            auto_recover_transactions=False,
        )

    def _submit(
        self,
        runtime: BrainFrogRuntime,
        channel: str = "telegram",
        text: str = "write_file safe_mod.py",
        user: str = "alice",
    ):
        return runtime.handle_message(
            IncomingMessage(
                id=f"{channel}-req-1",
                channel=channel,
                user_id=user,
                conversation_id="chat-1",
                text=text,
            )
        )

    def _approve_and_exec(
        self,
        runtime: BrainFrogRuntime,
        request_id: str,
        channel: str = "telegram",
        approver: str = "bob",
        executor: str = "alice",
    ):
        approved = runtime.handle_message(
            IncomingMessage(
                channel=channel,
                user_id=approver,
                conversation_id="chat-1",
                text=f"/approve {request_id}",
            )
        )
        executed = runtime.handle_message(
            IncomingMessage(
                channel=channel,
                user_id=executor,
                conversation_id="chat-1",
                text=f"/exec {request_id}",
            )
        )
        return approved, executed

    # -------------------------------------------------------------------------
    # 1. Valid remote task with matching proof transitions to DONE & passes
    # -------------------------------------------------------------------------
    def test_01_valid_remote_task_with_matching_proof_transitions_to_done(self) -> None:
        runtime = self._make_runtime()
        pending = self._submit(runtime, channel="telegram")
        self.assertEqual(pending.status, "pending_approval")
        req_id = pending.metadata["request_id"]
        work_id = pending.metadata["remote_work"]["work_id"]

        approved, executed = self._approve_and_exec(runtime, req_id, channel="telegram")
        self.assertTrue(approved.success)
        self.assertTrue(executed.success)
        self.assertEqual(executed.status, "completed")

        # RemoteWorkResult assertions
        rw_data = executed.metadata["remote_work"]
        self.assertEqual(rw_data["status"], "completed")
        self.assertTrue(rw_data["verification_passed"])
        self.assertIsNotNone(rw_data["proof_path"])
        self.assertTrue(Path(rw_data["proof_path"]).exists())

        # Work record assertions
        work = self.work_store.get(work_id)
        self.assertIsNotNone(work)
        self.assertEqual(work.status, WorkStatus.DONE)
        self.assertIsNotNone(work.verification_result)
        self.assertEqual(str(work.verification_result.status).upper(), "PASS")
        self.assertGreater(work.verification_result.passed, 0)
        self.assertEqual(work.verification_result.failed, 0)

        # DeterministicProofVerifier assertion
        proof_store = FileProofStore(self.repo)
        self.assertTrue(proof_store.exists(work_id))
        verifier = DeterministicProofVerifier(self.repo, verify_disk_integrity=True)
        rep = verifier.verify(work_id)
        self.assertEqual(rep.overall_status, ProofVerificationStatus.VALID)
        self.assertEqual(rep.verdict_status, "VERIFIED")

    # -------------------------------------------------------------------------
    # 2. Missing proof fails closed to FAILED
    # -------------------------------------------------------------------------
    def test_02_missing_proof_fails_closed_to_failed(self) -> None:
        runtime = self._make_runtime()
        pending = self._submit(runtime, channel="telegram")
        req_id = pending.metadata["request_id"]
        work_id = pending.metadata["remote_work"]["work_id"]

        approved, executed = self._approve_and_exec(runtime, req_id, channel="telegram")
        self.assertTrue(executed.success)

        proof_path = Path(executed.metadata["remote_work"]["proof_path"])
        self.assertTrue(proof_path.exists())
        proof_path.unlink()
        md_path = proof_path.with_suffix(".md")
        if md_path.exists():
            md_path.unlink()

        # Deterministic verification fails closed when proof is missing
        verifier = DeterministicProofVerifier(self.repo, verify_disk_integrity=True)
        rep = verifier.verify(work_id)
        self.assertNotEqual(rep.overall_status, ProofVerificationStatus.VALID)
        self.assertEqual(rep.overall_status, ProofVerificationStatus.INCOMPLETE)

        # In recovery/resume, missing proof for committed tx transitions to FAILED
        crashed_work = Work(
            actor_id="alice",
            channel="telegram",
            intent="write_file safe_mod.py",
            goal="write_file safe_mod.py",
            scope=("safe_mod.py",),
            status=WorkStatus.VERIFYING,
            transaction_id=executed.metadata["transaction_id"],
            resume_metadata={"workspace": str(self.repo), "task_id": work_id},
        )
        self.work_store.create(crashed_work)
        rec_mgr = TransactionRecoveryManager(self.repo, self.tx_store)
        res_ok, res_msg, res_work = resume_work(
            crashed_work.id,
            actor_id="alice",
            channel="telegram",
            work_store=self.work_store,
            transaction_store=self.tx_store,
            recovery_manager=rec_mgr,
        )
        self.assertFalse(res_ok)
        self.assertEqual(res_work.status, WorkStatus.FAILED)
        self.assertIn("UNVERIFIED_RECOVERY", getattr(res_work.failure, "code", ""))

    # -------------------------------------------------------------------------
    # 3. Invalid / FAILED proof fails closed (tampered disk file)
    # -------------------------------------------------------------------------
    def test_03_invalid_tampered_proof_fails_closed(self) -> None:
        runtime = self._make_runtime()
        pending = self._submit(runtime, channel="telegram")
        req_id = pending.metadata["request_id"]
        work_id = pending.metadata["remote_work"]["work_id"]

        _, executed = self._approve_and_exec(runtime, req_id, channel="telegram")
        self.assertTrue(executed.success)

        # Tamper with file on disk after verification
        target_file = self.repo / "safe_mod.py"
        target_file.write_text("corrupted content = True\n", encoding="utf-8")

        verifier = DeterministicProofVerifier(self.repo, verify_disk_integrity=True)
        rep = verifier.verify(work_id)
        self.assertEqual(rep.overall_status, ProofVerificationStatus.INVALID)
        self.assertTrue(any("mismatch" in c.message.lower() for c in rep.checks))

    # -------------------------------------------------------------------------
    # 4. Proof for another task rejected
    # -------------------------------------------------------------------------
    def test_04_proof_for_another_task_rejected(self) -> None:
        runtime = self._make_runtime()
        pending = self._submit(runtime, channel="telegram")
        req_id = pending.metadata["request_id"]
        work_id = pending.metadata["remote_work"]["work_id"]

        _, executed = self._approve_and_exec(runtime, req_id, channel="telegram")
        self.assertTrue(executed.success)

        # Attempt to verify non-existent or other task
        verifier = DeterministicProofVerifier(self.repo, verify_disk_integrity=True)
        rep = verifier.verify("foreign_work_task_id_999")
        self.assertNotEqual(rep.overall_status, ProofVerificationStatus.VALID)
        self.assertEqual(rep.overall_status, ProofVerificationStatus.INCOMPLETE)

    # -------------------------------------------------------------------------
    # 5. Wrong workspace or transaction rejected
    # -------------------------------------------------------------------------
    def test_05_wrong_workspace_or_transaction_rejected(self) -> None:
        runtime = self._make_runtime()
        pending = self._submit(runtime, channel="telegram")
        req_id = pending.metadata["request_id"]
        work_id = pending.metadata["remote_work"]["work_id"]

        _, executed = self._approve_and_exec(runtime, req_id, channel="telegram")
        self.assertTrue(executed.success)

        # Verifier pointing to different directory rejects proof
        other_temp = tempfile.mkdtemp()
        try:
            other_verifier = DeterministicProofVerifier(Path(other_temp), verify_disk_integrity=True)
            rep = other_verifier.verify(work_id)
            self.assertNotEqual(rep.overall_status, ProofVerificationStatus.VALID)
            self.assertEqual(rep.overall_status, ProofVerificationStatus.INCOMPLETE)
        finally:
            shutil.rmtree(other_temp, ignore_errors=True)

    # -------------------------------------------------------------------------
    # 6. Missing verification result prevents DONE
    # -------------------------------------------------------------------------
    def test_06_missing_verification_result_prevents_done(self) -> None:
        runtime = self._make_runtime()
        pending = self._submit(runtime, channel="telegram")
        work_id = pending.metadata["remote_work"]["work_id"]

        work = self.work_store.get(work_id)
        # Invariant: work must have verification_result to be authoritative DONE
        self.assertIsNone(work.verification_result)
        self.assertNotEqual(work.status, WorkStatus.DONE)

    # -------------------------------------------------------------------------
    # 7. Test command failure rolls back transaction and fails work
    # -------------------------------------------------------------------------
    def test_07_test_command_failure_rolls_back_and_fails_work(self) -> None:
        runtime = self._make_runtime(default_test_cmd='python -m unittest non_existent_test')
        pending = self._submit(runtime, channel="telegram")
        req_id = pending.metadata["request_id"]
        work_id = pending.metadata["remote_work"]["work_id"]

        approved, executed = self._approve_and_exec(runtime, req_id, channel="telegram")
        self.assertTrue(approved.success)
        self.assertFalse(executed.success)
        self.assertEqual(executed.status, "error")

        # Work record is FAILED
        work = self.work_store.get(work_id)
        self.assertEqual(work.status, WorkStatus.FAILED)
        self.assertIn("VERIFICATION_FAILED", getattr(work.failure, "code", ""))

        # RemoteWorkResult indicates failed
        rw_data = executed.metadata.get("remote_work") or {}
        self.assertEqual(rw_data.get("status"), "failed")
        self.assertFalse(rw_data.get("verification_passed", False))

        # Mutation rolled back from disk
        self.assertFalse((self.repo / "safe_mod.py").exists())

        # Transaction was rolled back
        tx = self.tx_store.list(limit=1)[0]
        self.assertEqual(tx.status, TransactionStatus.ROLLED_BACK)

    # -------------------------------------------------------------------------
    # 8. Proof persistence failure after commit marks work FAILED honestly
    # -------------------------------------------------------------------------
    def test_08_proof_persistence_failure_after_commit_marks_work_failed_honestly(self) -> None:
        runtime = self._make_runtime()
        pending = self._submit(runtime, channel="telegram")
        req_id = pending.metadata["request_id"]
        work_id = pending.metadata["remote_work"]["work_id"]

        # Mock FileProofStore.save to raise an exception
        with patch.object(FileProofStore, "save", side_effect=IOError("Simulated disk full")):
            approved, executed = self._approve_and_exec(runtime, req_id, channel="telegram")

        self.assertTrue(approved.success)
        self.assertFalse(executed.success)
        self.assertEqual(executed.status, "error")

        # Work record is FAILED with PROOF_PERSISTENCE_FAILED code
        work = self.work_store.get(work_id)
        self.assertEqual(work.status, WorkStatus.FAILED)
        self.assertEqual(getattr(work.failure, "code", ""), "PROOF_PERSISTENCE_FAILED")

        # Remote work reporting does NOT claim verified success and does NOT claim rollback
        rw_data = executed.metadata.get("remote_work") or {}
        self.assertEqual(rw_data.get("status"), "failed")
        self.assertFalse(rw_data.get("verification_passed", False))
        self.assertIn("proof persistence failed", rw_data.get("message", "").lower())
        self.assertNotIn("was rolled back", rw_data.get("message", "").lower())

    # -------------------------------------------------------------------------
    # 9. Crash during VERIFYING recovers safely
    # -------------------------------------------------------------------------
    def test_09_crash_during_verifying_recovers_safely(self) -> None:
        runtime = self._make_runtime()
        pending = self._submit(runtime, channel="telegram")
        req_id = pending.metadata["request_id"]
        work_id = pending.metadata["remote_work"]["work_id"]

        approved, executed = self._approve_and_exec(runtime, req_id, channel="telegram")
        self.assertTrue(executed.success)

        # Simulate crash: a work in VERIFYING status pointing to the proven transaction and task:
        crashed_work = Work(
            actor_id="alice",
            channel="telegram",
            intent="write_file safe_mod.py",
            goal="write_file safe_mod.py",
            scope=("safe_mod.py",),
            status=WorkStatus.VERIFYING,
            transaction_id=executed.metadata["transaction_id"],
            resume_metadata={"workspace": str(self.repo), "task_id": work_id, "transaction_id": executed.metadata["transaction_id"]},
        )
        self.work_store.create(crashed_work)

        rec_mgr = TransactionRecoveryManager(self.repo, self.tx_store)
        res_ok, res_msg, recovered_work = resume_work(
            crashed_work.id,
            actor_id="alice",
            channel="telegram",
            work_store=self.work_store,
            transaction_store=self.tx_store,
            recovery_manager=rec_mgr,
        )
        self.assertTrue(res_ok)
        self.assertEqual(recovered_work.status, WorkStatus.DONE)
        self.assertIsNotNone(recovered_work.verification_result)
        self.assertEqual(str(recovered_work.verification_result.status).upper(), "PASS")

    # -------------------------------------------------------------------------
    # 10. Duplicate execution and OCC safety
    # -------------------------------------------------------------------------
    def test_10_duplicate_execution_and_replay_rejected(self) -> None:
        runtime = self._make_runtime()
        pending = self._submit(runtime, channel="telegram")
        req_id = pending.metadata["request_id"]
        work_id = pending.metadata["remote_work"]["work_id"]

        approved, first_exec = self._approve_and_exec(runtime, req_id, channel="telegram")
        self.assertTrue(first_exec.success)

        # Attempt to /exec again with same request_id -> rejected (already consumed)
        second_exec = runtime.handle_message(
            IncomingMessage(
                channel="telegram",
                user_id="alice",
                conversation_id="chat-1",
                text=f"/exec {req_id}",
            )
        )
        self.assertFalse(second_exec.success)
        self.assertEqual(second_exec.status, "rejected")

        # Attempt execution on already proven task_id -> rejected under workspace lock
        duplicate_req = IncomingMessage(
            id="dup-req-1",
            channel="telegram",
            user_id="alice",
            conversation_id="chat-1",
            text="write_file safe_mod.py",
            metadata={"task_id": work_id},
        )
        dup_res = runtime.handle_message(duplicate_req)
        self.assertFalse(dup_res.success)

    # -------------------------------------------------------------------------
    # 11. Telegram and WhatsApp status/reporting consistency
    # -------------------------------------------------------------------------
    def test_11_telegram_and_whatsapp_status_consistency(self) -> None:
        for ch in ("telegram", "whatsapp"):
            runtime = self._make_runtime(output={"mod.py": f"# channel={ch}\n"})
            pending = self._submit(runtime, channel=ch, text="write_file mod.py")
            req_id = pending.metadata["request_id"]
            work_id = pending.metadata["remote_work"]["work_id"]

            approved, executed = self._approve_and_exec(runtime, req_id, channel=ch)
            self.assertTrue(approved.success)
            self.assertTrue(executed.success)

            rw = executed.metadata["remote_work"]
            self.assertEqual(rw["status"], "completed")
            self.assertTrue(rw["verification_passed"])
            self.assertTrue(Path(rw["proof_path"]).exists())

            work = self.work_store.get(work_id)
            self.assertEqual(work.status, WorkStatus.DONE)
            self.assertEqual(str(work.verification_result.status).upper(), "PASS")

            verifier = DeterministicProofVerifier(self.repo, verify_disk_integrity=True)
            rep = verifier.verify(work_id)
            self.assertEqual(rep.overall_status, ProofVerificationStatus.VALID)

    # -------------------------------------------------------------------------
    # 12. No false verified-success response before proof persistence
    # -------------------------------------------------------------------------
    def test_12_no_false_verified_success_response(self) -> None:
        runtime = self._make_runtime(default_test_cmd='python -m unittest non_existent_test')
        pending = self._submit(runtime, channel="telegram")
        req_id = pending.metadata["request_id"]

        approved, executed = self._approve_and_exec(runtime, req_id, channel="telegram")
        self.assertFalse(executed.success)
        self.assertNotEqual(executed.status, "completed")
        self.assertNotIn("completed successfully", executed.text.lower())
        rw = executed.metadata.get("remote_work") or {}
        self.assertFalse(rw.get("verification_passed", False))


if __name__ == "__main__":
    unittest.main()
