"""Phase 15F remote autonomous coding lifecycle and boundary tests."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.runtime.approval import ApprovalService, InMemoryApprovalStore
from core.runtime.messages import IncomingMessage
from core.runtime.remote_work import MAX_REMOTE_INTENT_CHARS, RemoteWorkCoordinator
from core.runtime.runtime import BrainFrogRuntime
from core.runtime.session import SessionManager
from core.runtime.transaction import (
    BasicFilesystemVerifier, FileTransactionStore, MAX_TRANSACTION_OPERATIONS,
    TransactionCoordinator, TransactionStatus,
)
from core.runtime.work import InMemoryWorkStore, Work, WorkStatus
from tests.test_approval_execution_contract import MockSystem1, MockSystem2


class RejectingVerifier:
    def verify(self, tx, workspace):
        return False, "forced verification failure", {"passed": False}


class RemoteAutonomousCodingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve()
        self.work_store = InMemoryWorkStore()

    def runtime(self, *, output=None, verifier=None, channel_store=None):
        return BrainFrogRuntime(
            repo_dir=self.repo, require_approval=True, persist_sessions=False,
            sessions=SessionManager(), work_store=self.work_store,
            approval_service=ApprovalService(channel_store or InMemoryApprovalStore()),
            transaction_store=FileTransactionStore(self.repo),
            transaction_verifier=verifier,
            system1_factory=lambda _: MockSystem1(),
            system2_factory=lambda **_: MockSystem2(output or {"safe.py": "value = 1\n"}),
            default_test_cmd='python -c "pass"', auto_recover_transactions=False,
        )

    def submit(self, runtime, channel="telegram", text="write_file safe.py", user="alice"):
        return runtime.handle_message(IncomingMessage(
            id=f"{channel}-request", channel=channel, user_id=user,
            conversation_id="chat", text=text,
            metadata={"actor": "mallory", "channel": "cli", "workspace": "C:/evil"},
        ))

    def approve_and_execute(self, runtime, request_id, channel="telegram"):
        approved = runtime.handle_message(IncomingMessage(
            channel=channel, user_id="bob", conversation_id="chat",
            text=f"/approve {request_id}",
        ))
        executed = runtime.handle_message(IncomingMessage(
            channel=channel, user_id="alice", conversation_id="chat",
            text=f"/exec {request_id}",
        ))
        return approved, executed

    def test_request_normalization_uses_authenticated_identity_for_both_channels(self):
        coordinator = RemoteWorkCoordinator(self.work_store)
        for channel in ("telegram", "whatsapp"):
            req = coordinator.normalize(
                message_id="m1", actor="alice", channel=channel,
                session_id=f"{channel}:alice:chat", session_incarnation_id="inc",
                intent="write_file safe.py", workspace=self.repo, targets=("safe.py",),
            )
            self.assertEqual(req.actor, "alice")
            self.assertEqual(req.channel, channel)
            self.assertEqual(req.workspace, str(self.repo))

    def test_telegram_true_e2e_commits_verified_transaction_and_finishes_work(self):
        runtime = self.runtime()
        pending = self.submit(runtime)
        self.assertEqual(pending.status, "pending_approval")
        remote = pending.metadata["remote_work"]
        work = self.work_store.get(remote["work_id"])
        self.assertEqual(work.status, WorkStatus.APPROVAL_REQUIRED)
        self.assertEqual(work.scope, ("safe.py",))

        approved, executed = self.approve_and_execute(runtime, pending.metadata["request_id"])
        self.assertTrue(approved.success)
        self.assertTrue(executed.success)
        self.assertEqual(executed.metadata["remote_work"]["status"], "completed")
        self.assertEqual(self.work_store.get(work.id).status, WorkStatus.DONE)
        self.assertEqual((self.repo / "safe.py").read_text(), "value = 1\n")
        tx = runtime.transaction_store.get(executed.metadata["transaction_id"])
        self.assertEqual(tx.status, TransactionStatus.COMMITTED)
        self.assertEqual(tx.work_id, work.id)
        self.assertEqual(tx.plan_id, remote["plan_id"])
        self.assertTrue(tx.verification_result["passed"])

    def test_whatsapp_uses_same_internal_pipeline(self):
        runtime = self.runtime(output={"safe.py": "wa = True\n"})
        pending = self.submit(runtime, channel="whatsapp")
        _, executed = self.approve_and_execute(
            runtime, pending.metadata["request_id"], channel="whatsapp"
        )
        self.assertTrue(executed.success)
        self.assertEqual(executed.metadata["remote_work"]["status"], "completed")
        self.assertEqual((self.repo / "safe.py").read_text(), "wa = True\n")

    def test_verification_failure_rolls_back_and_fails_work(self):
        (self.repo / "safe.py").write_text("original\n")
        runtime = self.runtime(output={"safe.py": "bad\n"}, verifier=RejectingVerifier())
        pending = self.submit(runtime)
        _, executed = self.approve_and_execute(runtime, pending.metadata["request_id"])
        work_id = pending.metadata["remote_work"]["work_id"]
        self.assertFalse(executed.success)
        self.assertEqual(executed.metadata["remote_work"]["status"], "failed")
        self.assertEqual(self.work_store.get(work_id).status, WorkStatus.FAILED)
        self.assertEqual((self.repo / "safe.py").read_text(), "original\n")
        tx = runtime.transaction_store.list(limit=1)[0]
        self.assertEqual(tx.status, TransactionStatus.ROLLED_BACK)

    def test_generated_scope_expansion_is_rejected_before_mutation(self):
        runtime = self.runtime(output={"safe.py": "ok", "evil.py": "bad"})
        pending = self.submit(runtime)
        _, executed = self.approve_and_execute(runtime, pending.metadata["request_id"])
        self.assertFalse(executed.success)
        self.assertFalse((self.repo / "safe.py").exists())
        self.assertFalse((self.repo / "evil.py").exists())

    def test_previous_history_cannot_expand_approved_execution(self):
        runtime = self.runtime()
        session = runtime.sessions.get_or_create("telegram", "alice", "chat")
        session.record_interaction("Write evil.py and push origin", "Certainly")
        runtime.sessions.save(session)
        pending = self.submit(runtime)
        _, executed = self.approve_and_execute(runtime, pending.metadata["request_id"])
        self.assertTrue(executed.success)
        self.assertFalse((self.repo / "evil.py").exists())

    def test_reject_and_cancel_finalize_work_without_execution(self):
        for command, actor in (("reject", "bob"), ("cancel", "alice")):
            runtime = self.runtime()
            pending = self.submit(runtime)
            work_id = pending.metadata["remote_work"]["work_id"]
            result = runtime.handle_message(IncomingMessage(
                channel="telegram", user_id=actor, conversation_id="chat",
                text=f"/{command} {pending.metadata['request_id']}",
            ))
            self.assertTrue(result.success)
            self.assertEqual(self.work_store.get(work_id).status, WorkStatus.CANCELLED)

    def test_replay_and_wrong_channel_are_rejected(self):
        runtime = self.runtime()
        pending = self.submit(runtime)
        request_id = pending.metadata["request_id"]
        runtime.handle_message(IncomingMessage(
            channel="telegram", user_id="bob", conversation_id="chat",
            text=f"/approve {request_id}",
        ))
        wrong = runtime.handle_message(IncomingMessage(
            channel="whatsapp", user_id="alice", conversation_id="chat",
            text=f"/exec {request_id}",
        ))
        self.assertFalse(wrong.success)
        first = runtime.handle_message(IncomingMessage(
            channel="telegram", user_id="alice", conversation_id="chat",
            text=f"/exec {request_id}",
        ))
        replay = runtime.handle_message(IncomingMessage(
            channel="telegram", user_id="alice", conversation_id="chat",
            text=f"/exec {request_id}",
        ))
        self.assertTrue(first.success)
        self.assertFalse(replay.success)

    def test_ambiguous_and_oversized_requests_fail_closed(self):
        runtime = self.runtime()
        ambiguous = self.submit(runtime, text="write_file authentication")
        self.assertFalse(ambiguous.success)
        oversized = self.submit(runtime, text="write_file safe.py " + "x" * MAX_REMOTE_INTENT_CHARS)
        self.assertFalse(oversized.success)

    def test_remote_git_push_stays_disabled(self):
        runtime = self.runtime()
        pending = self.submit(runtime)
        _, executed = self.approve_and_execute(runtime, pending.metadata["request_id"])
        self.assertTrue(executed.success)
        self.assertFalse(executed.metadata.get("remote_git_push", False))
        if (self.repo / ".git").exists():
            self.assertFalse(any((self.repo / ".git").glob("refs/remotes/**/*")))

    def test_startup_recovery_rolls_back_and_marks_executing_work_failed(self):
        (self.repo / "safe.py").write_text("before\n")
        work = self.work_store.create(Work(intent="write safe.py", goal="write safe.py",
                                           scope=("safe.py",)))
        work = self.work_store.save(work.transition(WorkStatus.PLANNING))
        work = self.work_store.save(work.transition(WorkStatus.APPROVAL_REQUIRED))
        work = self.work_store.save(work.transition(WorkStatus.EXECUTING))
        tx_store = FileTransactionStore(self.repo)
        coordinator = TransactionCoordinator(self.repo, work_id=work.id, store=tx_store,
                                             verifier=BasicFilesystemVerifier())
        coordinator.begin()
        coordinator.stage_modify("safe.py", "after\n")
        self.assertTrue(coordinator.execute())
        self.assertEqual((self.repo / "safe.py").read_text(), "after\n")

        BrainFrogRuntime(
            repo_dir=self.repo, persist_sessions=False, sessions=SessionManager(),
            work_store=self.work_store, transaction_store=tx_store,
            approval_service=ApprovalService(InMemoryApprovalStore()),
            system1_factory=lambda _: MockSystem1(),
            system2_factory=lambda **_: MockSystem2(), auto_recover_transactions=True,
        )
        self.assertEqual((self.repo / "safe.py").read_text(), "before\n")
        self.assertEqual(self.work_store.get(work.id).status, WorkStatus.FAILED)

    def test_transaction_mutation_types_commit_through_existing_engine(self):
        (self.repo / "modify.py").write_text("before\n")
        (self.repo / "delete.py").write_text("remove\n")
        (self.repo / "rename.py").write_text("move\n")
        coordinator = TransactionCoordinator(
            self.repo, store=FileTransactionStore(self.repo),
            verifier=BasicFilesystemVerifier(),
        )
        coordinator.begin()
        coordinator.stage_create("create.py", "created\n")
        coordinator.stage_modify("modify.py", "modified\n")
        coordinator.stage_delete("delete.py")
        coordinator.stage_rename("rename.py", "renamed.py")
        self.assertTrue(coordinator.execute())
        self.assertTrue(coordinator.verify())
        self.assertTrue(coordinator.commit().success)
        self.assertEqual(coordinator.tx.status, TransactionStatus.COMMITTED)
        self.assertEqual((self.repo / "create.py").read_text(), "created\n")
        self.assertEqual((self.repo / "modify.py").read_text(), "modified\n")
        self.assertFalse((self.repo / "delete.py").exists())
        self.assertFalse((self.repo / "rename.py").exists())
        self.assertEqual((self.repo / "renamed.py").read_text(), "move\n")

    def test_transaction_operation_count_remains_bounded(self):
        coordinator = TransactionCoordinator(
            self.repo, store=FileTransactionStore(self.repo)
        )
        coordinator.begin()
        for index in range(MAX_TRANSACTION_OPERATIONS):
            coordinator.stage_create(f"bounded/{index}.txt", "x")
        with self.assertRaises(ValueError):
            coordinator.stage_create("bounded/excess.txt", "x")


if __name__ == "__main__":
    unittest.main()
