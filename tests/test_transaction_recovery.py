"""Unit and Integration Tests for Phase 15E Crash Recovery and Transaction Journal.

Tests cover:
- Durable transaction journal & write-ahead operation preparation
- Crash simulations at all critical interruption points:
  * Crash before first mutation
  * Crash during mutation (in-flight PREPARED state)
  * Crash after mutation (EXECUTED state)
  * Crash before verification
  * Crash after verification passed but before commit marker
  * Crash after durable commit marker
- Deterministic reverse-order rollback across all filesystem operation types:
  * create_file, modify_file, delete_file, rename_file
- Fail-closed ambiguity detection
- Idempotent recovery
- Resource bounds (max operations, max snapshot size, max serialized size, startup limit)
- BrainFrogRuntime startup discovery & recovery
- CLI slash command /recover
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.runtime.transaction import (
    MAX_FILE_SNAPSHOT_BYTES,
    MAX_SERIALIZED_TRANSACTION_BYTES,
    MAX_STARTUP_RECOVERY_LIMIT,
    MAX_TRANSACTION_OPERATIONS,
    BasicFilesystemVerifier,
    FileTransactionStore,
    InMemoryTransactionStore,
    OperationStatus,
    OperationType,
    RecoveryResult,
    Transaction,
    TransactionCoordinator,
    TransactionOperation,
    TransactionRecoveryManager,
    TransactionResourceLimitError,
    TransactionResult,
    TransactionStatus,
    recover_incomplete_transaction,
)
from core.runtime.runtime import BrainFrogRuntime
from core.runtime.messages import IncomingMessage
from core.runtime.session import InMemorySessionStore
from core.runtime.approval import InMemoryApprovalStore


class TestTransactionJournalDurability(unittest.TestCase):
    """Test durable journal writing, write-ahead operation states, and reload."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        self.workspace = Path(self.tmp_dir)
        self.store = FileTransactionStore(self.workspace)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_journal_persisted_survives_store_reload(self) -> None:
        tx = Transaction(
            id="tx-dur-1",
            work_id="work-1",
            plan_id="plan-1",
            status=TransactionStatus.STAGING,
            operations=[
                TransactionOperation(
                    id="op-1",
                    operation_type=OperationType.CREATE_FILE,
                    path="hello.txt",
                    status=OperationStatus.STAGED,
                    before_state={"exists": False},
                    after_state={"is_text": True, "content": "hello world"},
                )
            ],
        )
        self.assertTrue(self.store.save(tx))

        # Reload from a brand new store instance
        new_store = FileTransactionStore(self.workspace)
        loaded = new_store.get("tx-dur-1")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.id, "tx-dur-1")
        self.assertEqual(loaded.status, TransactionStatus.STAGING)
        self.assertEqual(len(loaded.operations), 1)
        self.assertEqual(loaded.operations[0].target, "hello.txt")

    def test_write_ahead_prepared_state_persisted(self) -> None:
        """Verify that coordinator persists PREPARED status to store before mutating file."""
        saved_statuses: List[OperationStatus] = []

        class InterceptStore(InMemoryTransactionStore):
            def save(self, tx: Transaction) -> bool:
                if tx.operations:
                    saved_statuses.append(tx.operations[0].status)
                return super().save(tx)

        intercept_store = InterceptStore()
        coordinator = TransactionCoordinator(
            workspace=self.workspace,
            store=intercept_store,
        )
        coordinator.stage_create("note.txt", "write-ahead test")
        coordinator.execute()

        # Must have observed PREPARED before EXECUTED
        self.assertIn(OperationStatus.PREPARED, saved_statuses)
        self.assertIn(OperationStatus.EXECUTED, saved_statuses)
        prep_idx = saved_statuses.index(OperationStatus.PREPARED)
        exec_idx = saved_statuses.index(OperationStatus.EXECUTED)
        self.assertLess(prep_idx, exec_idx)

    def test_corrupt_journal_fails_closed(self) -> None:
        """Verify that a corrupted JSON journal file does not crash the store and fails closed."""
        tx_dir = self.workspace / ".brainfrog" / "transactions"
        tx_dir.mkdir(parents=True, exist_ok=True)
        corrupt_file = tx_dir / "corrupted_record.json"
        corrupt_file.write_text("{this is not valid json!}", encoding="utf-8")

        mgr = TransactionRecoveryManager(workspace=self.workspace, store=self.store)
        res = mgr.recover("nonexistent_or_corrupt")
        self.assertFalse(res.recovered)
        self.assertEqual(res.final_status, TransactionStatus.FAILED)


class TestCrashScenarios(unittest.TestCase):
    """Simulate process crashes at critical interruption points."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        self.workspace = Path(self.tmp_dir)
        self.store = InMemoryTransactionStore()
        self.mgr = TransactionRecoveryManager(workspace=self.workspace, store=self.store)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_scenario_a_crash_before_first_mutation(self) -> None:
        """Interruption in STAGING state before any mutations took place."""
        tx = Transaction(
            id="tx-scen-a",
            workspace=str(self.workspace),
            status=TransactionStatus.STAGING,
            operations=[
                TransactionOperation(
                    id="op-1",
                    operation_type=OperationType.CREATE_FILE,
                    path="uncreated.txt",
                    status=OperationStatus.STAGED,
                    before_state={"exists": False},
                    after_state={"is_text": True, "content": "never wrote"},
                )
            ],
        )
        self.store.save(tx)

        res = self.mgr.recover("tx-scen-a")
        self.assertTrue(res.recovered)
        self.assertTrue(res.rolled_back)
        self.assertEqual(res.final_status, TransactionStatus.ROLLED_BACK)
        self.assertFalse((self.workspace / "uncreated.txt").exists())

    def test_scenario_b1_crash_during_mutation_file_was_written(self) -> None:
        """Interruption while operation was in-flight (PREPARED), but file was fully written."""
        target_file = self.workspace / "inflight.txt"
        target_file.write_text("inflight content", encoding="utf-8")

        tx = Transaction(
            id="tx-scen-b1",
            workspace=str(self.workspace),
            status=TransactionStatus.EXECUTING,
            operations=[
                TransactionOperation(
                    id="op-inflight",
                    operation_type=OperationType.CREATE_FILE,
                    path="inflight.txt",
                    status=OperationStatus.PREPARED,
                    before_state={"exists": False},
                    after_state={"is_text": True, "content": "inflight content"},
                )
            ],
        )
        self.store.save(tx)

        res = self.mgr.recover("tx-scen-b1")
        self.assertTrue(res.recovered)
        self.assertTrue(res.rolled_back)
        self.assertEqual(res.final_status, TransactionStatus.ROLLED_BACK)
        # Target was safely removed by rollback
        self.assertFalse(target_file.exists())

    def test_scenario_b2_crash_during_mutation_file_was_not_written(self) -> None:
        """Interruption while operation was in-flight (PREPARED), but file was never written."""
        target_file = self.workspace / "unwritten.txt"
        self.assertFalse(target_file.exists())

        tx = Transaction(
            id="tx-scen-b2",
            workspace=str(self.workspace),
            status=TransactionStatus.EXECUTING,
            operations=[
                TransactionOperation(
                    id="op-unwritten",
                    operation_type=OperationType.CREATE_FILE,
                    path="unwritten.txt",
                    status=OperationStatus.PREPARED,
                    before_state={"exists": False},
                    after_state={"is_text": True, "content": "some content"},
                )
            ],
        )
        self.store.save(tx)

        res = self.mgr.recover("tx-scen-b2")
        self.assertTrue(res.recovered)
        self.assertTrue(res.rolled_back)
        self.assertEqual(res.final_status, TransactionStatus.ROLLED_BACK)
        self.assertFalse(target_file.exists())

    def test_scenario_c_crash_after_mutation_executed(self) -> None:
        """Interruption after operation completed and EXECUTED was journaled."""
        original_file = self.workspace / "modify_me.txt"
        original_file.write_text("updated version", encoding="utf-8")

        tx = Transaction(
            id="tx-scen-c",
            workspace=str(self.workspace),
            status=TransactionStatus.EXECUTING,
            operations=[
                TransactionOperation(
                    id="op-modify",
                    operation_type=OperationType.MODIFY_FILE,
                    path="modify_me.txt",
                    status=OperationStatus.EXECUTED,
                    before_state={"exists": True, "is_text": True, "content": "original version"},
                    after_state={"is_text": True, "content": "updated version"},
                )
            ],
        )
        self.store.save(tx)

        res = self.mgr.recover("tx-scen-c")
        self.assertTrue(res.recovered)
        self.assertEqual(res.final_status, TransactionStatus.ROLLED_BACK)
        self.assertEqual(original_file.read_text(encoding="utf-8"), "original version")

    def test_scenario_d_crash_after_all_mutations_before_verification(self) -> None:
        """Interruption after all mutations executed, but before verification."""
        f1 = self.workspace / "f1.txt"
        f1.write_text("f1 new", encoding="utf-8")
        f2 = self.workspace / "f2.txt"
        f2.write_text("f2 new", encoding="utf-8")

        tx = Transaction(
            id="tx-scen-d",
            workspace=str(self.workspace),
            status=TransactionStatus.EXECUTING,
            operations=[
                TransactionOperation(
                    id="op-1",
                    operation_type=OperationType.CREATE_FILE,
                    path="f1.txt",
                    status=OperationStatus.EXECUTED,
                    before_state={"exists": False},
                    after_state={"is_text": True, "content": "f1 new"},
                ),
                TransactionOperation(
                    id="op-2",
                    operation_type=OperationType.CREATE_FILE,
                    path="f2.txt",
                    status=OperationStatus.EXECUTED,
                    before_state={"exists": False},
                    after_state={"is_text": True, "content": "f2 new"},
                ),
            ],
        )
        self.store.save(tx)

        res = self.mgr.recover("tx-scen-d")
        self.assertTrue(res.recovered)
        self.assertEqual(res.final_status, TransactionStatus.ROLLED_BACK)
        self.assertFalse(f1.exists())
        self.assertFalse(f2.exists())

    def test_scenario_e_crash_after_verification_passed_before_commit_marker(self) -> None:
        """Interruption after verification passed, but before durable commit marker.

        CRITICAL REQUIREMENT: Verification passing does NOT equal committed.
        Recovery MUST execute rollback.
        """
        f = self.workspace / "verified_file.txt"
        f.write_text("verified content", encoding="utf-8")

        tx = Transaction(
            id="tx-scen-e",
            workspace=str(self.workspace),
            status=TransactionStatus.VERIFYING,
            verification_result={"passed": True, "details": "all checks ok"},
            commit_marker=None,  # No commit marker!
            operations=[
                TransactionOperation(
                    id="op-1",
                    operation_type=OperationType.CREATE_FILE,
                    path="verified_file.txt",
                    status=OperationStatus.EXECUTED,
                    before_state={"exists": False},
                    after_state={"is_text": True, "content": "verified content"},
                )
            ],
        )
        self.store.save(tx)

        res = self.mgr.recover("tx-scen-e")
        self.assertTrue(res.recovered)
        self.assertTrue(res.rolled_back)
        self.assertEqual(res.final_status, TransactionStatus.ROLLED_BACK)
        # Verified file was rolled back because commit marker was never durably written
        self.assertFalse(f.exists())

    def test_scenario_f_crash_after_commit_marker_persisted(self) -> None:
        """Interruption after durable commit marker was persisted.

        CRITICAL REQUIREMENT: Transaction is considered committed and MUST NOT be rolled back.
        """
        f = self.workspace / "committed_file.txt"
        f.write_text("committed content", encoding="utf-8")

        tx = Transaction(
            id="tx-scen-f",
            workspace=str(self.workspace),
            status=TransactionStatus.VERIFYING,
            commit_marker={"marked_at": 123456.78, "status": "committed", "tx_id": "tx-scen-f"},
            operations=[
                TransactionOperation(
                    id="op-1",
                    operation_type=OperationType.CREATE_FILE,
                    path="committed_file.txt",
                    status=OperationStatus.EXECUTED,
                    before_state={"exists": False},
                    after_state={"is_text": True, "content": "committed content"},
                )
            ],
        )
        self.store.save(tx)

        res = self.mgr.recover("tx-scen-f")
        self.assertTrue(res.recovered)
        self.assertFalse(res.rolled_back)
        self.assertEqual(res.final_status, TransactionStatus.COMMITTED)
        # File must remain untouched on disk
        self.assertTrue(f.exists())
        self.assertEqual(f.read_text(encoding="utf-8"), "committed content")


class TestFilesystemRollbackAndAmbiguity(unittest.TestCase):
    """Test filesystem operations (create, modify, delete, rename) and ambiguous state handling."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        self.workspace = Path(self.tmp_dir)
        self.store = InMemoryTransactionStore()
        self.mgr = TransactionRecoveryManager(workspace=self.workspace, store=self.store)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_reverse_order_rollback_execution(self) -> None:
        """Verify rollback executes operations in strict reverse order (LIFO)."""
        call_order: List[str] = []

        # Create two files
        (self.workspace / "file_1.txt").write_text("data 1", encoding="utf-8")
        (self.workspace / "file_2.txt").write_text("data 2", encoding="utf-8")

        tx = Transaction(
            id="tx-reverse-order",
            workspace=str(self.workspace),
            status=TransactionStatus.EXECUTING,
            operations=[
                TransactionOperation(
                    id="op-first",
                    operation_type=OperationType.CREATE_FILE,
                    path="file_1.txt",
                    status=OperationStatus.EXECUTED,
                    before_state={"exists": False},
                    after_state={"is_text": True, "content": "data 1"},
                ),
                TransactionOperation(
                    id="op-second",
                    operation_type=OperationType.CREATE_FILE,
                    path="file_2.txt",
                    status=OperationStatus.EXECUTED,
                    before_state={"exists": False},
                    after_state={"is_text": True, "content": "data 2"},
                ),
            ],
        )
        self.store.save(tx)

        # Intercept rollback to verify order
        orig_inspect = self.mgr._inspect_target_state

        def logged_inspect(path: Path) -> Dict[str, Any]:
            call_order.append(path.name)
            return orig_inspect(path)

        self.mgr._inspect_target_state = logged_inspect  # type: ignore

        res = self.mgr.recover("tx-reverse-order")
        self.assertTrue(res.recovered)
        # file_2.txt must be inspected/rolled back before file_1.txt
        idx2 = call_order.index("file_2.txt")
        idx1 = call_order.index("file_1.txt")
        self.assertLess(idx2, idx1)

    def test_delete_and_rename_recovery(self) -> None:
        """Test rollback of delete_file and rename_file mutations."""
        # Setup deleted file rollback
        deleted_file = self.workspace / "deleted.txt"
        self.assertFalse(deleted_file.exists())

        # Setup renamed file rollback
        source_file = self.workspace / "source.txt"
        dest_file = self.workspace / "dest.txt"
        dest_file.write_text("renamed content", encoding="utf-8")
        self.assertFalse(source_file.exists())

        tx = Transaction(
            id="tx-del-rename",
            workspace=str(self.workspace),
            status=TransactionStatus.EXECUTING,
            operations=[
                TransactionOperation(
                    id="op-del",
                    operation_type=OperationType.DELETE_FILE,
                    path="deleted.txt",
                    status=OperationStatus.EXECUTED,
                    before_state={"exists": True, "is_text": True, "content": "restore me"},
                    after_state={"exists": False},
                ),
                TransactionOperation(
                    id="op-ren",
                    operation_type=OperationType.RENAME_FILE,
                    path="source.txt",
                    new_path="dest.txt",
                    status=OperationStatus.EXECUTED,
                    before_state={"exists": True, "is_text": True, "content": "renamed content"},
                    after_state={"new_target": "dest.txt"},
                ),
            ],
        )
        self.store.save(tx)

        res = self.mgr.recover("tx-del-rename")
        self.assertTrue(res.recovered)
        self.assertEqual(res.final_status, TransactionStatus.ROLLED_BACK)

        # Deleted file restored
        self.assertTrue(deleted_file.exists())
        self.assertEqual(deleted_file.read_text(encoding="utf-8"), "restore me")

        # Renamed file moved back to source
        self.assertTrue(source_file.exists())
        self.assertFalse(dest_file.exists())
        self.assertEqual(source_file.read_text(encoding="utf-8"), "renamed content")

    def test_ambiguous_filesystem_state_fails_closed(self) -> None:
        """Ambiguous state: file content matches NEITHER before nor after state.

        CRITICAL REQUIREMENT: Recovery must NOT guess or pretend success.
        It must transition to FAILED and preserve diagnostics.
        """
        f = self.workspace / "corrupted.txt"
        # Write arbitrary corrupted data
        f.write_text("mystery content that was never recorded anywhere", encoding="utf-8")

        tx = Transaction(
            id="tx-ambiguous",
            workspace=str(self.workspace),
            status=TransactionStatus.EXECUTING,
            operations=[
                TransactionOperation(
                    id="op-ambig",
                    operation_type=OperationType.MODIFY_FILE,
                    path="corrupted.txt",
                    status=OperationStatus.PREPARED,
                    before_state={"exists": True, "is_text": True, "content": "clean original"},
                    after_state={"is_text": True, "content": "clean updated"},
                )
            ],
        )
        self.store.save(tx)

        res = self.mgr.recover("tx-ambiguous")
        self.assertFalse(res.recovered)
        self.assertTrue(res.ambiguous)
        self.assertEqual(res.final_status, TransactionStatus.FAILED)
        self.assertIn("Ambiguous filesystem state", str(res.error))

        # Check persisted transaction in store
        stored = self.store.get("tx-ambiguous")
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.status, TransactionStatus.FAILED)

    def test_unknown_operation_type_fails_closed(self) -> None:
        """Unknown or unmapped operation type fails closed without arbitrary execution."""
        tx = Transaction(
            id="tx-unknown-op",
            workspace=str(self.workspace),
            status=TransactionStatus.EXECUTING,
            operations=[
                TransactionOperation.from_dict({
                    "id": "op-custom",
                    "type": "create_file",  # Valid for from_dict
                    "target": "custom.txt",
                    "status": "executed",
                })
            ],
        )
        # Artificially inject unknown type
        tx.operations[0].type = "shell_exec_unknown"  # type: ignore
        self.store.save(tx)

        res = self.mgr.recover("tx-unknown-op")
        self.assertFalse(res.recovered)
        self.assertEqual(res.final_status, TransactionStatus.FAILED)
        self.assertTrue(res.ambiguous)


class TestIdempotencyAndTerminalStates(unittest.TestCase):
    """Test idempotent recovery and preservation of terminal states."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        self.workspace = Path(self.tmp_dir)
        self.store = InMemoryTransactionStore()
        self.mgr = TransactionRecoveryManager(workspace=self.workspace, store=self.store)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_recovery_is_idempotent(self) -> None:
        """Calling recover() repeatedly produces identical safe outcomes."""
        f = self.workspace / "file.txt"
        f.write_text("test", encoding="utf-8")

        tx = Transaction(
            id="tx-idem",
            workspace=str(self.workspace),
            status=TransactionStatus.EXECUTING,
            operations=[
                TransactionOperation(
                    id="op-1",
                    operation_type=OperationType.CREATE_FILE,
                    path="file.txt",
                    status=OperationStatus.EXECUTED,
                    before_state={"exists": False},
                    after_state={"is_text": True, "content": "test"},
                )
            ],
        )
        self.store.save(tx)

        # First recovery
        res1 = self.mgr.recover("tx-idem")
        self.assertTrue(res1.recovered)
        self.assertEqual(res1.final_status, TransactionStatus.ROLLED_BACK)
        self.assertFalse(f.exists())

        # Second recovery on already rolled_back transaction
        res2 = self.mgr.recover("tx-idem")
        self.assertTrue(res2.recovered)
        self.assertEqual(res2.final_status, TransactionStatus.ROLLED_BACK)
        self.assertFalse(f.exists())

    def test_already_committed_transaction_never_rolled_back(self) -> None:
        """Committed transaction is preserved and not altered by recovery."""
        f = self.workspace / "done.txt"
        f.write_text("permanent data", encoding="utf-8")

        tx = Transaction(
            id="tx-committed-guard",
            workspace=str(self.workspace),
            status=TransactionStatus.COMMITTED,
            commit_marker={"status": "committed"},
        )
        self.store.save(tx)

        res = self.mgr.recover("tx-committed-guard")
        self.assertTrue(res.recovered)
        self.assertFalse(res.rolled_back)
        self.assertEqual(res.final_status, TransactionStatus.COMMITTED)
        self.assertTrue(f.exists())

    def test_already_failed_transaction_remains_failed(self) -> None:
        """Failed transaction remains failed and is not transitioned."""
        tx = Transaction(
            id="tx-already-failed",
            status=TransactionStatus.FAILED,
            failure_reason="Prior fatal error",
        )
        self.store.save(tx)

        res = self.mgr.recover("tx-already-failed")
        self.assertFalse(res.recovered)
        self.assertEqual(res.final_status, TransactionStatus.FAILED)
        self.assertEqual(res.error, "Prior fatal error")


class TestResourceBounds(unittest.TestCase):
    """Test resource bounds: max operations, max snapshot size, max serialized size, and startup limits."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        self.workspace = Path(self.tmp_dir)
        self.store = InMemoryTransactionStore()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_max_operations_limit_enforced(self) -> None:
        coordinator = TransactionCoordinator(
            workspace=self.workspace,
            store=self.store,
        )
        # Fill up to max
        for i in range(MAX_TRANSACTION_OPERATIONS):
            coordinator.stage_create(f"file_{i}.txt", f"data {i}")

        # Staging one more exceeds limit
        with self.assertRaises(TransactionResourceLimitError):
            coordinator.stage_create("overflow.txt", "overflow data")

    def test_max_file_snapshot_limit_enforced(self) -> None:
        # Create a file that exceeds MAX_FILE_SNAPSHOT_BYTES (simulated via coordinator staging)
        coordinator = TransactionCoordinator(
            workspace=self.workspace,
            store=self.store,
        )
        huge_payload = "x" * (MAX_FILE_SNAPSHOT_BYTES + 1024)
        with self.assertRaises(TransactionResourceLimitError):
            coordinator.stage_create("huge.txt", huge_payload)

    def test_max_serialized_transaction_size_enforced(self) -> None:
        file_store = FileTransactionStore(self.workspace)
        tx = Transaction(id="tx-huge")
        # Add a metadata payload exceeding MAX_SERIALIZED_TRANSACTION_BYTES
        tx.metadata["huge_data"] = "a" * (MAX_SERIALIZED_TRANSACTION_BYTES + 1024)
        with self.assertRaises(TransactionResourceLimitError):
            file_store.save(tx)

    def test_startup_recovery_limit(self) -> None:
        """Verify discover_incomplete respects the limit."""
        for i in range(MAX_STARTUP_RECOVERY_LIMIT + 10):
            self.store.save(Transaction(id=f"tx-inc-{i}", status=TransactionStatus.EXECUTING))

        mgr = TransactionRecoveryManager(workspace=self.workspace, store=self.store)
        discovered = mgr.discover_incomplete(limit=MAX_STARTUP_RECOVERY_LIMIT)
        self.assertEqual(len(discovered), MAX_STARTUP_RECOVERY_LIMIT)


class TestRuntimeStartupAndCommands(unittest.TestCase):
    """Test BrainFrogRuntime startup recovery integration and /recover slash command."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        self.workspace = Path(self.tmp_dir)
        self.tx_store = FileTransactionStore(self.workspace)
        self.session_store = InMemorySessionStore()
        self.approval_store = InMemoryApprovalStore()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_startup_discovers_and_recovers_incomplete_transactions(self) -> None:
        # Create an incomplete transaction on disk before runtime boots
        fpath = self.workspace / "crashed_on_boot.txt"
        fpath.write_text("dangling data", encoding="utf-8")

        tx = Transaction(
            id="tx-boot-crash",
            workspace=str(self.workspace),
            status=TransactionStatus.EXECUTING,
            operations=[
                TransactionOperation(
                    id="op-1",
                    operation_type=OperationType.CREATE_FILE,
                    path="crashed_on_boot.txt",
                    status=OperationStatus.EXECUTED,
                    before_state={"exists": False},
                    after_state={"is_text": True, "content": "dangling data"},
                )
            ],
        )
        self.tx_store.save(tx)

        # Boot runtime with auto_recover_transactions=True (default)
        runtime = BrainFrogRuntime(
            repo_dir=self.workspace,
            session_store=self.session_store,
            approval_store=self.approval_store,
            transaction_store=self.tx_store,
            auto_recover_transactions=True,
        )

        # Verify startup recovery ran and rolled back dangling file
        self.assertFalse(fpath.exists())
        self.assertEqual(len(runtime.startup_recovery_results), 1)
        res = runtime.startup_recovery_results[0]
        self.assertEqual(res.transaction_id, "tx-boot-crash")
        self.assertEqual(res.final_status, TransactionStatus.ROLLED_BACK)

        # Transaction record is now ROLLED_BACK
        recovered_tx = self.tx_store.get("tx-boot-crash")
        self.assertIsNotNone(recovered_tx)
        assert recovered_tx is not None
        self.assertEqual(recovered_tx.status, TransactionStatus.ROLLED_BACK)

    def test_recover_slash_command(self) -> None:
        # Create an incomplete transaction
        fpath = self.workspace / "slash_recover.txt"
        fpath.write_text("to be rolled back", encoding="utf-8")

        tx = Transaction(
            id="tx-cmd-rec",
            workspace=str(self.workspace),
            status=TransactionStatus.EXECUTING,
            operations=[
                TransactionOperation(
                    id="op-1",
                    operation_type=OperationType.CREATE_FILE,
                    path="slash_recover.txt",
                    status=OperationStatus.EXECUTED,
                    before_state={"exists": False},
                    after_state={"is_text": True, "content": "to be rolled back"},
                )
            ],
        )
        self.tx_store.save(tx)

        runtime = BrainFrogRuntime(
            repo_dir=self.workspace,
            session_store=self.session_store,
            approval_store=self.approval_store,
            transaction_store=self.tx_store,
            auto_recover_transactions=False,  # Don't auto-recover so we can invoke command
        )

        msg = IncomingMessage(
            text="/recover tx-cmd-rec",
            channel="cli",
            user_id="local",
        )
        out = runtime.process_message(msg)
        self.assertTrue(out.success)
        self.assertIn("recovered", out.text.lower())
        self.assertIn("tx-cmd-rec", out.text)
        self.assertFalse(fpath.exists())


if __name__ == "__main__":
    unittest.main()
