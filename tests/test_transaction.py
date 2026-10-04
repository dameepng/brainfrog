"""Unit, security, and integration tests for Phase 15D Transactional Execution Engine & Rollback."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.runtime.capabilities import Capabilities, FilesystemPolicy
from core.runtime.contract import ApprovedExecutionContract
from core.runtime.messages import IncomingMessage
from core.runtime.runtime import BrainFrogRuntime
from core.runtime.transaction import (
    ALLOWED_TRANSACTION_TRANSITIONS,
    BasicFilesystemVerifier,
    FileTransactionStore,
    InMemoryTransactionStore,
    InvalidTransactionTransition,
    OperationStatus,
    OperationType,
    Transaction,
    TransactionCoordinator,
    TransactionOperation,
    TransactionResult,
    TransactionStatus,
    TransactionVerifier,
    recover_incomplete_transaction,
)
from orchestrator import RunConfig, _write_files


class TestTransactionStatusAndTransitions(unittest.TestCase):
    """Test the 8-state deterministic transaction state machine."""

    def test_status_enum_values(self) -> None:
        expected = {
            "created", "staging", "executing", "verifying",
            "committed", "rolling_back", "rolled_back", "failed",
        }
        actual = {s.value for s in TransactionStatus}
        self.assertEqual(actual, expected)

    def test_happy_path_transitions(self) -> None:
        tx = Transaction()
        self.assertEqual(tx.status, TransactionStatus.CREATED)

        tx.transition(TransactionStatus.STAGING)
        self.assertEqual(tx.status, TransactionStatus.STAGING)

        tx.transition(TransactionStatus.EXECUTING)
        self.assertEqual(tx.status, TransactionStatus.EXECUTING)

        tx.transition(TransactionStatus.VERIFYING)
        self.assertEqual(tx.status, TransactionStatus.VERIFYING)

        tx.transition(TransactionStatus.COMMITTED)
        self.assertEqual(tx.status, TransactionStatus.COMMITTED)
        self.assertIsNotNone(tx.committed_at)

    def test_staging_cancel_to_failed(self) -> None:
        tx = Transaction()
        tx.transition(TransactionStatus.STAGING)
        tx.transition(TransactionStatus.FAILED, reason="user abort")
        self.assertEqual(tx.status, TransactionStatus.FAILED)
        self.assertEqual(tx.error, "user abort")

    def test_execution_failure_with_clean_rollback(self) -> None:
        tx = Transaction()
        tx.transition(TransactionStatus.STAGING)
        tx.transition(TransactionStatus.EXECUTING)
        tx.transition(TransactionStatus.ROLLING_BACK, reason="disk write failed")
        self.assertEqual(tx.status, TransactionStatus.ROLLING_BACK)
        self.assertEqual(tx.error, "disk write failed")

        tx.transition(TransactionStatus.ROLLED_BACK)
        self.assertEqual(tx.status, TransactionStatus.ROLLED_BACK)
        self.assertIsNotNone(tx.rolled_back_at)

    def test_execution_failure_with_rollback_failure(self) -> None:
        tx = Transaction()
        tx.transition(TransactionStatus.STAGING)
        tx.transition(TransactionStatus.EXECUTING)
        tx.transition(TransactionStatus.ROLLING_BACK, reason="syntax error")
        tx.transition(TransactionStatus.FAILED, reason="rollback permission denied")
        self.assertEqual(tx.status, TransactionStatus.FAILED)
        self.assertEqual(tx.rollback_error, "rollback permission denied")

    def test_verification_failure_with_rollback(self) -> None:
        tx = Transaction()
        tx.transition(TransactionStatus.STAGING)
        tx.transition(TransactionStatus.EXECUTING)
        tx.transition(TransactionStatus.VERIFYING)
        tx.transition(TransactionStatus.ROLLING_BACK, reason="test suite failed")
        tx.transition(TransactionStatus.ROLLED_BACK)
        self.assertEqual(tx.status, TransactionStatus.ROLLED_BACK)

    def test_terminal_state_immutability(self) -> None:
        # COMMITTED cannot transition
        tx_c = Transaction()
        tx_c.transition(TransactionStatus.STAGING)
        tx_c.transition(TransactionStatus.EXECUTING)
        tx_c.transition(TransactionStatus.VERIFYING)
        tx_c.transition(TransactionStatus.COMMITTED)
        for st in TransactionStatus:
            with self.assertRaises(InvalidTransactionTransition):
                tx_c.transition(st)

        # ROLLED_BACK cannot transition
        tx_r = Transaction()
        tx_r.transition(TransactionStatus.STAGING)
        tx_r.transition(TransactionStatus.EXECUTING)
        tx_r.transition(TransactionStatus.ROLLING_BACK)
        tx_r.transition(TransactionStatus.ROLLED_BACK)
        for st in TransactionStatus:
            with self.assertRaises(InvalidTransactionTransition):
                tx_r.transition(st)

        # FAILED cannot transition
        tx_f = Transaction()
        tx_f.transition(TransactionStatus.FAILED)
        for st in TransactionStatus:
            with self.assertRaises(InvalidTransactionTransition):
                tx_f.transition(st)

    def test_invalid_skip_transitions(self) -> None:
        tx = Transaction()
        with self.assertRaises(InvalidTransactionTransition):
            tx.transition(TransactionStatus.COMMITTED)

        with self.assertRaises(InvalidTransactionTransition):
            tx.transition(TransactionStatus.EXECUTING)

        with self.assertRaises(InvalidTransactionTransition):
            tx.transition(TransactionStatus.VERIFYING)

        tx.transition(TransactionStatus.STAGING)
        with self.assertRaises(InvalidTransactionTransition):
            tx.transition(TransactionStatus.COMMITTED)


class TestTransactionDataModelAndSerialization(unittest.TestCase):
    """Test Transaction and Operation serialization and data validation."""

    def test_operation_dict_roundtrip(self) -> None:
        op = TransactionOperation(
            id="op-1",
            operation_type=OperationType.MODIFY_FILE,
            path="src/main.py",
            before_state={"exists": True, "content": "print('hello')"},
            after_state={"is_text": True, "content": "print('world')"},
            status=OperationStatus.STAGED,
        )
        d = op.to_dict()
        op2 = TransactionOperation.from_dict(d)
        self.assertEqual(op.id, op2.id)
        self.assertEqual(op.operation_type, op2.operation_type)
        self.assertEqual(op.path, op2.path)
        self.assertEqual(op.before_state, op2.before_state)
        self.assertEqual(op.after_state, op2.after_state)
        self.assertEqual(op.status, op2.status)

    def test_transaction_dict_roundtrip(self) -> None:
        tx = Transaction(
            id="tx-123",
            work_id="w-456",
            plan_id="p-789",
            session_id="s-999",
            operations=[
                TransactionOperation(
                    id="op-1",
                    operation_type=OperationType.CREATE_FILE,
                    path="test.py",
                    after_state={"is_text": True, "content": "x = 1"},
                    status=OperationStatus.EXECUTED,
                )
            ],
            status=TransactionStatus.EXECUTING,
        )
        d = tx.to_dict()
        tx2 = Transaction.from_dict(d)
        self.assertEqual(tx.id, tx2.id)
        self.assertEqual(tx.work_id, tx2.work_id)
        self.assertEqual(tx.plan_id, tx2.plan_id)
        self.assertEqual(tx.session_id, tx2.session_id)
        self.assertEqual(len(tx2.operations), 1)
        self.assertEqual(tx2.operations[0].path, "test.py")
        self.assertEqual(tx2.status, TransactionStatus.EXECUTING)

    def test_secret_scrubbing_in_transaction(self) -> None:
        dummy_secret = "".join(["g", "h", "p", "_", "123456789012345678901234567890123456"])
        tx = Transaction(
            id="tx-sec",
            failure_reason=f"Authentication failed with {dummy_secret}",
            metadata={"token": dummy_secret},
        )
        d = tx.to_dict()
        self.assertNotIn(dummy_secret, d["failure_reason"])
        self.assertNotIn(dummy_secret, json.dumps(d["metadata"]))


class TestTransactionCoordinatorFilesystemOperations(unittest.TestCase):
    """Test single and multi-file transactional filesystem operations."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        self.workspace = Path(self.tmp_dir)
        self.coordinator = TransactionCoordinator(self.workspace)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_create_file_operation(self) -> None:
        target = self.workspace / "new_file.txt"
        op = self.coordinator.stage_operation(
            OperationType.CREATE_FILE,
            "new_file.txt",
            after_state="created content",
        )
        self.assertFalse(target.exists())

        # Execute
        ok = self.coordinator.execute()
        self.assertTrue(ok)
        self.assertTrue(target.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), "created content")
        self.assertEqual(op.status, OperationStatus.EXECUTED)

        # Rollback op deletes created file
        rb_res = self.coordinator.rollback()
        self.assertTrue(rb_res.rolled_back)
        self.assertFalse(target.exists())
        self.assertEqual(op.status, OperationStatus.ROLLED_BACK)

    def test_modify_file_operation(self) -> None:
        target = self.workspace / "existing.txt"
        target.write_text("initial version", encoding="utf-8")

        op = self.coordinator.stage_operation(
            OperationType.MODIFY_FILE,
            "existing.txt",
            after_state="updated version",
        )
        self.assertIsNotNone(op.before_state)
        assert op.before_state is not None
        self.assertEqual(op.before_state.get("content"), "initial version")

        ok = self.coordinator.execute()
        self.assertTrue(ok)
        self.assertEqual(target.read_text(encoding="utf-8"), "updated version")

        # Rollback restores original version
        rb_res = self.coordinator.rollback()
        self.assertTrue(rb_res.rolled_back)
        self.assertEqual(target.read_text(encoding="utf-8"), "initial version")

    def test_delete_file_operation(self) -> None:
        target = self.workspace / "doomed.txt"
        target.write_text("farewell world", encoding="utf-8")

        op = self.coordinator.stage_operation(
            OperationType.DELETE_FILE,
            "doomed.txt",
        )
        self.assertIsNotNone(op.before_state)
        assert op.before_state is not None
        self.assertEqual(op.before_state.get("content"), "farewell world")

        ok = self.coordinator.execute()
        self.assertTrue(ok)
        self.assertFalse(target.exists())

        # Rollback restores file and content
        rb_res = self.coordinator.rollback()
        self.assertTrue(rb_res.rolled_back)
        self.assertTrue(target.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), "farewell world")

    def test_rename_file_operation(self) -> None:
        src = self.workspace / "old_name.txt"
        dst = self.workspace / "new_name.txt"
        src.write_text("file content", encoding="utf-8")

        op = self.coordinator.stage_operation(
            OperationType.RENAME_FILE,
            "old_name.txt",
            new_target="new_name.txt",
        )
        self.assertIsNotNone(op.before_state)
        assert op.before_state is not None
        self.assertTrue(op.before_state.get("exists"))

        ok = self.coordinator.execute()
        self.assertTrue(ok)
        self.assertFalse(src.exists())
        self.assertTrue(dst.exists())
        self.assertEqual(dst.read_text(encoding="utf-8"), "file content")

        # Rollback moves it back
        rb_res = self.coordinator.rollback()
        self.assertTrue(rb_res.rolled_back)
        self.assertTrue(src.exists())
        self.assertFalse(dst.exists())


class TestTransactionCoordinatorAtomicityAndRollback(unittest.TestCase):
    """Test multi-operation execution, failure atomicity, and LIFO rollback order."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        self.workspace = Path(self.tmp_dir)
        self.store = InMemoryTransactionStore()
        self.coordinator = TransactionCoordinator(self.workspace, store=self.store)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_successful_multi_op_commit(self) -> None:
        # Existing file
        (self.workspace / "file1.txt").write_text("orig1", encoding="utf-8")

        self.coordinator.stage_operation(OperationType.MODIFY_FILE, "file1.txt", after_state="mod1")
        self.coordinator.stage_operation(OperationType.CREATE_FILE, "file2.txt", after_state="created2")

        result = self.coordinator.run()
        self.assertTrue(result.success)
        self.assertEqual(result.status, TransactionStatus.COMMITTED)
        self.assertEqual(len(result.operations), 2)
        self.assertEqual((self.workspace / "file1.txt").read_text(encoding="utf-8"), "mod1")
        self.assertEqual((self.workspace / "file2.txt").read_text(encoding="utf-8"), "created2")

        # Verifying store
        saved = self.store.get(result.transaction_id)
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(saved.status, TransactionStatus.COMMITTED)

    def test_atomic_rollback_on_step_failure(self) -> None:
        # Setup pre-existing files
        (self.workspace / "fileA.txt").write_text("origA", encoding="utf-8")
        (self.workspace / "fileB.txt").write_text("origB", encoding="utf-8")

        # Op 1: Modify fileA
        self.coordinator.stage_operation(OperationType.MODIFY_FILE, "fileA.txt", after_state="newA")
        # Op 2: Create fileC
        self.coordinator.stage_operation(OperationType.CREATE_FILE, "fileC.txt", after_state="newC")
        # Op 3: Bad op that will fail execution
        bad_op = TransactionOperation(
            id="bad-op",
            operation_type=OperationType.MODIFY_FILE,
            path="nonexistent_sub/never_existed.txt",
            after_state=None,  # triggers error during execution
        )
        self.coordinator.current_transaction.operations.append(bad_op)

        result = self.coordinator.run()
        self.assertFalse(result.success)
        self.assertEqual(result.status, TransactionStatus.ROLLED_BACK)
        self.assertTrue("failed" in (result.error or "").lower() or "error" in (result.error or "").lower())

        # Assert all mutations were cleanly rolled back
        self.assertEqual((self.workspace / "fileA.txt").read_text(encoding="utf-8"), "origA")
        self.assertFalse((self.workspace / "fileC.txt").exists())

    def test_reverse_order_lifo_rollback(self) -> None:
        rollback_order: List[str] = []

        class TrackingCoordinator(TransactionCoordinator):
            def _rollback_operation(self, op: TransactionOperation) -> bool:
                rollback_order.append(op.id)
                return super()._rollback_operation(op)

        coord = TrackingCoordinator(self.workspace, store=self.store)
        op1 = coord.stage_operation(OperationType.CREATE_FILE, "f1.txt", after_state="1")
        op2 = coord.stage_operation(OperationType.CREATE_FILE, "f2.txt", after_state="2")

        # Inject 3rd op that fails execution
        bad_op = TransactionOperation(
            id="op-fail",
            operation_type=OperationType.MODIFY_FILE,
            path="missing.txt",
            after_state=None,
        )
        coord.current_transaction.operations.append(bad_op)

        res = coord.run()
        self.assertFalse(res.success)
        # Rollback order should be op2 then op1 (LIFO)
        self.assertEqual(rollback_order, [op2.id, op1.id])


class TestTransactionVerificationAndFailure(unittest.TestCase):
    """Test verification gating before commit and rollback handling."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        self.workspace = Path(self.tmp_dir)
        self.store = InMemoryTransactionStore()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_verification_failure_triggers_rollback(self) -> None:
        class FailingVerifier(TransactionVerifier):
            def verify(self, tx: Transaction, workspace: Path) -> tuple[bool, Optional[str], Dict[str, Any]]:
                return False, "Verification assertion failed: checksum mismatch", {"failed": True}

        coord = TransactionCoordinator(
            self.workspace,
            store=self.store,
            verifier=FailingVerifier(),
        )
        coord.stage_operation(OperationType.CREATE_FILE, "verified_file.txt", after_state="hello")

        res = coord.run()
        self.assertFalse(res.success)
        self.assertEqual(res.status, TransactionStatus.ROLLED_BACK)
        self.assertIn("checksum mismatch", res.error or "")
        # File should have been rolled back (deleted)
        self.assertFalse((self.workspace / "verified_file.txt").exists())

    def test_rollback_failure_sets_failed_status_and_preserves_both_errors(self) -> None:
        class BrokenRollbackCoordinator(TransactionCoordinator):
            def _rollback_operation(self, op: TransactionOperation) -> bool:
                raise PermissionError("Disk locked during rollback")

        coord = BrokenRollbackCoordinator(self.workspace, store=self.store)
        coord.stage_operation(OperationType.CREATE_FILE, "locked.txt", after_state="content")

        # Inject failing execution op
        bad_op = TransactionOperation(
            id="bad-op",
            operation_type=OperationType.MODIFY_FILE,
            path="missing_file.txt",
            after_state=None,
        )
        coord.current_transaction.operations.append(bad_op)

        res = coord.run()
        self.assertFalse(res.success)
        self.assertEqual(res.status, TransactionStatus.FAILED)
        self.assertIsNotNone(coord.current_transaction.error)
        self.assertIsNotNone(coord.current_transaction.rollback_error)
        self.assertIn("Disk locked", coord.current_transaction.rollback_error or "")


class TestTransactionStores(unittest.TestCase):
    """Test InMemoryTransactionStore and FileTransactionStore."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        self.repo_dir = Path(self.tmp_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_in_memory_store_operations(self) -> None:
        store = InMemoryTransactionStore()
        tx1 = Transaction(id="tx-1", session_id="s1", status=TransactionStatus.COMMITTED)
        tx2 = Transaction(id="tx-2", session_id="s1", status=TransactionStatus.EXECUTING)
        tx3 = Transaction(id="tx-3", session_id="s2", status=TransactionStatus.ROLLED_BACK)

        store.save(tx1)
        store.save(tx2)
        store.save(tx3)

        tx1_loaded = store.get("tx-1")
        self.assertIsNotNone(tx1_loaded)
        assert tx1_loaded is not None
        self.assertEqual(tx1_loaded.id, tx1.id)
        self.assertIsNone(store.get("nonexistent"))

        s1_txs = store.list(session_id="s1")
        self.assertEqual(len(s1_txs), 2)

        incomplete = store.list_incomplete()
        self.assertEqual(len(incomplete), 1)
        self.assertEqual(incomplete[0].id, "tx-2")

    def test_file_transaction_store_persistence(self) -> None:
        store = FileTransactionStore(self.repo_dir)
        tx = Transaction(
            id="tx-persist-1",
            work_id="w-1",
            status=TransactionStatus.COMMITTED,
            operations=[
                TransactionOperation(
                    id="op-1",
                    operation_type=OperationType.CREATE_FILE,
                    path="foo.txt",
                    after_state={"is_text": True, "content": "bar"},
                    status=OperationStatus.EXECUTED,
                )
            ],
        )
        store.save(tx)

        # Load with a fresh store instance
        store2 = FileTransactionStore(self.repo_dir)
        loaded = store2.get("tx-persist-1")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.id, "tx-persist-1")
        self.assertEqual(loaded.work_id, "w-1")
        self.assertEqual(loaded.status, TransactionStatus.COMMITTED)
        self.assertEqual(len(loaded.operations), 1)
        self.assertEqual(loaded.operations[0].path, "foo.txt")


class TestCrashRecovery(unittest.TestCase):
    """Test recovery of interrupted transactions."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        self.workspace = Path(self.tmp_dir)
        self.store = InMemoryTransactionStore()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_recover_incomplete_transaction_rolls_back(self) -> None:
        # File was created before the crash
        fpath = self.workspace / "crashed_file.txt"
        fpath.write_text("intermediate state", encoding="utf-8")

        # Transaction left in EXECUTING state
        tx = Transaction(
            id="tx-crashed",
            workspace=str(self.workspace),
            status=TransactionStatus.EXECUTING,
            operations=[
                TransactionOperation(
                    id="op-1",
                    operation_type=OperationType.CREATE_FILE,
                    path="crashed_file.txt",
                    after_state={"is_text": True, "content": "intermediate state"},
                    status=OperationStatus.EXECUTED,
                )
            ],
        )
        self.store.save(tx)

        recovered = recover_incomplete_transaction(tx, self.workspace, self.store)
        self.assertEqual(recovered.status, TransactionStatus.ROLLED_BACK)
        # CRITICAL: Never marked as COMMITTED
        self.assertNotEqual(recovered.status, TransactionStatus.COMMITTED)
        # File was removed by rollback
        self.assertFalse(fpath.exists())

        # Store was updated
        stored_tx = self.store.get("tx-crashed")
        self.assertIsNotNone(stored_tx)
        assert stored_tx is not None
        self.assertEqual(stored_tx.status, TransactionStatus.ROLLED_BACK)

    def test_recovery_ignores_committed_transaction(self) -> None:
        tx = Transaction(
            id="tx-done",
            status=TransactionStatus.COMMITTED,
        )
        self.store.save(tx)
        res = recover_incomplete_transaction(tx, self.workspace, self.store)
        self.assertEqual(res.status, TransactionStatus.COMMITTED)


class TestOrchestratorTransactionalWrites(unittest.TestCase):
    """Test orchestrator._write_files transactional integration and invariants."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        self.workspace = Path(self.tmp_dir)
        self.store = InMemoryTransactionStore()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_write_files_success_under_transaction(self) -> None:
        files = {
            "module_a.py": "def a(): pass\n",
            "module_b.py": "def b(): pass\n",
        }
        res = _write_files(
            repo_dir=self.workspace,
            files=files,
            mode="build",
            work_id="work-001",
            plan_id="plan-001",
            store=self.store,
        )
        self.assertIsInstance(res, TransactionResult)
        self.assertTrue(res.success)
        self.assertEqual(res.status, TransactionStatus.COMMITTED)
        self.assertTrue((self.workspace / "module_a.py").exists())
        self.assertTrue((self.workspace / "module_b.py").exists())

        # Check transaction store
        saved = self.store.get(res.transaction_id)
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(saved.work_id, "work-001")
        self.assertEqual(saved.plan_id, "plan-001")
        self.assertEqual(saved.status, TransactionStatus.COMMITTED)

    def test_write_files_plan_mode_fails_closed_without_transaction(self) -> None:
        files = {"bad.py": "print(1)"}
        with self.assertRaises(PermissionError):
            _write_files(
                repo_dir=self.workspace,
                files=files,
                mode="plan",
                store=self.store,
            )
        self.assertFalse((self.workspace / "bad.py").exists())
        self.assertEqual(len(self.store.list()), 0)

    def test_write_files_path_traversal_fails_closed(self) -> None:
        files = {"../../outside.txt": "evil"}
        with self.assertRaises(PermissionError):
            _write_files(
                repo_dir=self.workspace,
                files=files,
                mode="build",
                store=self.store,
            )
        self.assertFalse((self.workspace.parent / "outside.txt").exists())


class TestRuntimeTransactionVisibilityAndCommands(unittest.TestCase):
    """Test runtime /transactions and /transaction <id> visibility."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        self.repo_dir = Path(self.tmp_dir)
        self.tx_store = InMemoryTransactionStore()
        self.runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            persist_sessions=False,
            transaction_store=self.tx_store,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_transactions_command_empty(self) -> None:
        msg = IncomingMessage(
            text="/transactions",
            channel="cli",
            user_id="local",
        )
        out = self.runtime.process_message(msg)
        self.assertTrue(out.success)
        self.assertIn("No transactions recorded", out.text)

    def test_transactions_command_with_records(self) -> None:
        # Prepopulate a transaction for the session
        session = self.runtime.sessions.get_or_create("cli", "local")
        tx = Transaction(
            id="tx-hist-1",
            session_id=session.session_id,
            status=TransactionStatus.COMMITTED,
            operations=[
                TransactionOperation(
                    id="op-1",
                    operation_type=OperationType.CREATE_FILE,
                    path="foo.txt",
                    after_state={"is_text": True, "content": "bar"},
                    status=OperationStatus.EXECUTED,
                )
            ],
        )
        self.tx_store.save(tx)

        msg = IncomingMessage(
            text="/transactions",
            channel="cli",
            user_id="local",
        )
        out = self.runtime.process_message(msg)
        self.assertTrue(out.success)
        self.assertIn("Recorded Transactions", out.text)
        self.assertIn("tx-hist-1", out.text)
        self.assertIn("committed", out.text)

    def test_transaction_detail_command(self) -> None:
        tx = Transaction(
            id="tx-detail-1",
            work_id="w-abc",
            plan_id="p-xyz",
            status=TransactionStatus.COMMITTED,
            operations=[
                TransactionOperation(
                    id="op-1",
                    operation_type=OperationType.CREATE_FILE,
                    path="created.py",
                    after_state={"is_text": True, "content": "print(1)"},
                    status=OperationStatus.EXECUTED,
                )
            ],
        )
        self.tx_store.save(tx)

        # Query existing transaction
        msg = IncomingMessage(
            text="/transaction tx-detail-1",
            channel="cli",
            user_id="local",
        )
        out = self.runtime.process_message(msg)
        self.assertTrue(out.success)
        self.assertIn("Transaction Details (`tx-detail-1`)", out.text)
        self.assertIn("Status: `committed`", out.text)
        self.assertIn("Work ID: `w-abc`", out.text)
        self.assertIn("Plan ID: `p-xyz`", out.text)
        self.assertIn("create_file", out.text)

        # Query non-existent transaction
        msg_missing = IncomingMessage(
            text="/transaction tx-unknown",
            channel="cli",
            user_id="local",
        )
        out_missing = self.runtime.process_message(msg_missing)
        self.assertFalse(out_missing.success)
        self.assertIn("not found", out_missing.text)


if __name__ == "__main__":
    unittest.main()
