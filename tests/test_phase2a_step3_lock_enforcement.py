"""Adversarial and Lifecycle Test Suite for BrainFrog Phase 2A Step 3:
WorkspaceLock Lifecycle Extension & TransactionCoordinator Capability Enforcement.

Enforces Phase 2A Step 3 Invariants:
1. Unlocked direct calls to every coordinator mutation entry point are rejected.
2. A valid capability for workspace A cannot mutate workspace B.
3. Reentrant acquisition uses one OS handle and maintains lock until outermost release.
4. Inner release leaves outer capability active.
5. Outermost release revokes retained capability references.
6. A child async task attempting mutation after parent release is rejected.
7. Cross-thread capability use is rejected (fail-closed thread binding).
8. A second process cannot acquire the same workspace lock while the first process holds it.
9. Recovery against an actively locked workspace performs zero mutations.
10. Stale or malformed metadata does not cause the lock file to be unlinked before acquisition.
11. Recovery acquires the OS lock before reading or mutating recovery state.
12. Recovery rollback conflicts preserve external changes and return an ambiguous result.
13. An interrupted partial rollback is retryable only when recorded state and disk preconditions permit.
14. Proof persistence and transaction finalization remain inside the lock lifetime.
15. Exceptions, cancellation, and early returns do not leak locks or release prematurely.
16. Windows-specific locking behavior tests.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

from core.runtime.transaction import (
    FileTransactionStore,
    InMemoryTransactionStore,
    InvalidTransactionTransition,
    OperationStatus,
    OperationType,
    RecoveryResult,
    Transaction,
    TransactionCoordinator,
    TransactionOperation,
    TransactionRecoveryManager,
    TransactionResult,
    TransactionStatus,
)
from core.runtime.workspace_lock import (
    WorkspaceLock,
    WorkspaceLockCapability,
    WorkspaceLockedError,
    WorkspaceLockRevokedError,
    WorkspaceLockUnsupportedPlatformError,
    get_active_workspace_capability,
)


class TestPhase2AStep3LockEnforcement(unittest.TestCase):
    """Adversarial and lifecycle enforcement tests for Phase 2A Step 3."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp_dir.name).resolve()

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    # -------------------------------------------------------------------------
    # 1. Unlocked direct coordinator mutation entry points rejected
    # -------------------------------------------------------------------------
    def test_01_unlocked_direct_coordinator_mutations_rejected(self) -> None:
        """Every coordinator mutation entry point must reject calls without an active lock capability."""
        coord = TransactionCoordinator(self.workspace)

        # A. begin()
        with self.assertRaises(WorkspaceLockRevokedError):
            coord.begin()

        # B. stage_create()
        with self.assertRaises(WorkspaceLockRevokedError):
            coord.stage_create("file1.txt", "content1")

        # C. stage_modify()
        with self.assertRaises(WorkspaceLockRevokedError):
            coord.stage_modify("file1.txt", "content2")

        # D. stage_delete()
        with self.assertRaises(WorkspaceLockRevokedError):
            coord.stage_delete("file1.txt")

        # E. stage_rename()
        with self.assertRaises(WorkspaceLockRevokedError):
            coord.stage_rename("file1.txt", "file2.txt")

        # F. stage_write_batch()
        with self.assertRaises(WorkspaceLockRevokedError):
            coord.stage_write_batch([("batch.txt", "batch content")])

        # G. stage_operation()
        with self.assertRaises(WorkspaceLockRevokedError):
            coord.stage_operation(OperationType.CREATE_FILE, "raw_op.txt")

        # H. execute()
        with self.assertRaises(WorkspaceLockRevokedError):
            coord.execute()

        # I. verify()
        with self.assertRaises(WorkspaceLockRevokedError):
            coord.verify()

        # J. record_domain_verification()
        with self.assertRaises(WorkspaceLockRevokedError):
            coord.record_domain_verification(command="pytest", passed=True)

        # K. commit()
        with self.assertRaises(WorkspaceLockRevokedError):
            coord.commit()

        # L. rollback()
        with self.assertRaises(WorkspaceLockRevokedError):
            coord.rollback(reason="Test rollback")

        # M. Callers cannot manufacture authority by passing invalid objects
        fake_caps = [True, False, "granted", str(self.workspace), 1234, object()]
        for fake in fake_caps:
            with self.assertRaises((WorkspaceLockRevokedError, TypeError)):
                coord.begin(lock_capability=fake)

    # -------------------------------------------------------------------------
    # 2. Capability for workspace A cannot mutate workspace B
    # -------------------------------------------------------------------------
    def test_02_workspace_cross_binding_rejected(self) -> None:
        """A valid capability granted for workspace A must be rejected for workspace B."""
        with tempfile.TemporaryDirectory() as td_other:
            ws_other = Path(td_other).resolve()

            # Acquire lock on workspace A
            with WorkspaceLock(self.workspace) as lock_a:
                cap_a = lock_a.get_capability()

                # Attempt to mutate workspace B using ambient lock or explicitly passed cap_a
                coord_b = TransactionCoordinator(ws_other)
                with self.assertRaises(WorkspaceLockRevokedError) as ctx:
                    coord_b.begin(lock_capability=cap_a)
                self.assertIn("Workspace mismatch", str(ctx.exception))

                # Ambient contextVar check also fails for ws_other
                with self.assertRaises(WorkspaceLockRevokedError):
                    coord_b.stage_create("evil.txt", "evil")

    # -------------------------------------------------------------------------
    # 3. Reentrant acquisition uses single OS handle and maintains lock
    # -------------------------------------------------------------------------
    def test_03_reentrant_acquisition_uses_single_os_handle(self) -> None:
        """Reentrant acquisition reuses existing OS file handle and maintains lock until outermost release."""
        lock1 = WorkspaceLock(self.workspace)
        lock1.acquire()
        try:
            self.assertTrue(lock1.is_active())
            self.assertEqual(lock1._depth, 1)
            original_handle = lock1._file

            # Reentrant acquisition on the same instance
            lock1.acquire()
            self.assertEqual(lock1._depth, 2)
            self.assertIs(lock1._file, original_handle)

            # Reentrant acquisition via a separate instance in same execution context
            lock2 = WorkspaceLock(self.workspace)
            lock2.acquire()
            self.assertEqual(lock1._depth, 3)
            self.assertIs(lock2._parent_lock, lock1)

            # Release lock2 (inner release)
            lock2.release()
            self.assertEqual(lock1._depth, 2)
            self.assertTrue(lock1.is_active())

            # Release lock1 inner
            lock1.release()
            self.assertEqual(lock1._depth, 1)
            self.assertTrue(lock1.is_active())
        finally:
            # Outermost release
            lock1.release()
            self.assertFalse(lock1.is_active())
            self.assertEqual(lock1._depth, 0)

    # -------------------------------------------------------------------------
    # 4. Inner release leaves outer capability active
    # -------------------------------------------------------------------------
    def test_04_inner_release_leaves_outer_capability_active(self) -> None:
        """An inner release decrements depth but does not revoke the capability or release the OS lock."""
        with WorkspaceLock(self.workspace) as outer_lock:
            outer_cap = outer_lock.get_capability()
            self.assertTrue(outer_cap.is_live())

            with WorkspaceLock(self.workspace) as inner_lock:
                inner_cap = inner_lock.get_capability()
                self.assertTrue(inner_cap.is_live())
                self.assertIs(inner_cap, outer_cap)

            # Inner lock exited, outer must still be live and unrevoked
            self.assertTrue(outer_lock.is_active())
            self.assertTrue(outer_cap.is_live())
            self.assertFalse(outer_cap.is_revoked)

            # Mutations under outer lock still succeed
            coord = TransactionCoordinator(self.workspace)
            coord.begin()
            coord.stage_create("under_outer.txt", "valid")
            self.assertTrue(coord.execute())
            res = coord.commit()
            self.assertTrue(res.committed)

    # -------------------------------------------------------------------------
    # 5. Outermost release revokes retained capability references
    # -------------------------------------------------------------------------
    def test_05_outermost_release_revokes_retained_capability_references(self) -> None:
        """Retained capability references become revoked upon outermost lock release."""
        retained_cap: Optional[WorkspaceLockCapability] = None
        with WorkspaceLock(self.workspace) as lock:
            retained_cap = lock.get_capability()
            self.assertTrue(retained_cap.is_live())
            self.assertFalse(retained_cap.is_revoked)

        # Now outside lock scope
        assert retained_cap is not None
        self.assertTrue(retained_cap.is_revoked)
        with self.assertRaises(WorkspaceLockRevokedError):
            retained_cap.validate_live()

        # Attempting coordinator mutation with retained_cap must fail closed
        coord = TransactionCoordinator(self.workspace)
        with self.assertRaises(WorkspaceLockRevokedError):
            coord.begin(lock_capability=retained_cap)

    # -------------------------------------------------------------------------
    # 6. Child async task after parent release is rejected
    # -------------------------------------------------------------------------
    def test_06_child_async_task_after_parent_release_rejected(self) -> None:
        """An asynchronous child task attempting coordinator mutation after parent release is rejected."""
        async def run_scenario():
            captured_cap: Optional[WorkspaceLockCapability] = None

            async def child_task(cap: WorkspaceLockCapability):
                # Small yield to ensure parent scope has exited
                await asyncio.sleep(0.05)
                coord = TransactionCoordinator(self.workspace)
                # Should fail because cap is revoked and parent released OS lock
                coord.begin(lock_capability=cap)

            with WorkspaceLock(self.workspace) as lock:
                captured_cap = lock.get_capability()
                task = asyncio.create_task(child_task(captured_cap))

            # Parent scope exited, lock released
            with self.assertRaises(WorkspaceLockRevokedError):
                await task

        asyncio.run(run_scenario())

    # -------------------------------------------------------------------------
    # 7. Cross-thread capability use rejected (thread binding)
    # -------------------------------------------------------------------------
    def test_07_cross_thread_capability_use_rejected(self) -> None:
        """A capability obtained on Thread A cannot be used on Thread B (thread mismatch)."""
        with WorkspaceLock(self.workspace) as lock:
            cap = lock.get_capability()
            thread_error: List[Exception] = []

            def worker():
                try:
                    coord = TransactionCoordinator(self.workspace)
                    coord.begin(lock_capability=cap)
                except Exception as e:
                    thread_error.append(e)

            t = threading.Thread(target=worker)
            t.start()
            t.join(timeout=2.0)

            self.assertEqual(len(thread_error), 1)
            self.assertIsInstance(thread_error[0], WorkspaceLockRevokedError)
            self.assertIn("Thread mismatch", str(thread_error[0]))

    # -------------------------------------------------------------------------
    # 8. Multiprocess lock mutual exclusion
    # -------------------------------------------------------------------------
    def test_08_multiprocess_mutual_exclusion(self) -> None:
        """A second process cannot acquire the workspace lock while the first holds it."""
        ready_file = self.workspace / ".child_ready"
        exit_file = self.workspace / ".child_exit"

        child_code = f"""
import time
from pathlib import Path
from core.runtime.workspace_lock import WorkspaceLock

ws = Path({repr(str(self.workspace))})
ready = Path({repr(str(ready_file))})
exit_f = Path({repr(str(exit_file))})

lock = WorkspaceLock(ws, timeout=5.0)
lock.acquire()
try:
    ready.write_text("ready", encoding="utf-8")
    while not exit_f.exists():
        time.sleep(0.05)
finally:
    lock.release()
"""
        proc = subprocess.Popen([sys.executable, "-c", child_code])
        try:
            # Wait for child to acquire lock
            start = time.time()
            while not ready_file.exists() and (time.time() - start) < 5.0:
                time.sleep(0.05)
            self.assertTrue(ready_file.exists(), "Child process did not signal readiness in time")

            # Main process attempts acquisition with non-blocking timeout
            lock_main = WorkspaceLock(self.workspace, timeout=0.1)
            with self.assertRaises(WorkspaceLockedError):
                lock_main.acquire()

            # Signal child to exit and release lock
            exit_file.write_text("exit", encoding="utf-8")
            proc.wait(timeout=5.0)

            # Now main process must acquire cleanly
            lock_main.acquire()
            self.assertTrue(lock_main.is_active())
            lock_main.release()
        finally:
            if proc.poll() is None:
                proc.kill()

    # -------------------------------------------------------------------------
    # 9. Recovery against locked workspace performs zero mutations
    # -------------------------------------------------------------------------
    def test_09_recovery_against_locked_workspace_performs_zero_mutations(self) -> None:
        """Recovery attempted against an actively locked workspace returns fail-closed with zero mutations."""
        # 1. Set up a dirty file and journal
        dirty_file = self.workspace / "dirty.txt"
        dirty_file.write_text("agent dirty work", encoding="utf-8")

        store = FileTransactionStore(self.workspace)
        op = TransactionOperation(
            type=OperationType.CREATE_FILE,
            target="dirty.txt",
            status=OperationStatus.EXECUTED,
        )
        tx = Transaction(
            id="tx_locked_recovery",
            workspace=str(self.workspace),
            operations=[op],
            status=TransactionStatus.EXECUTING,
        )
        store.save(tx)

        # 2. Subprocess acquires and holds the workspace lock
        ready_file = self.workspace / ".holder_ready"
        exit_file = self.workspace / ".holder_exit"
        holder_code = f"""
import time
from pathlib import Path
from core.runtime.workspace_lock import WorkspaceLock

ws = Path({repr(str(self.workspace))})
ready = Path({repr(str(ready_file))})
exit_f = Path({repr(str(exit_file))})

lock = WorkspaceLock(ws, timeout=5.0)
lock.acquire()
try:
    ready.write_text("held", encoding="utf-8")
    while not exit_f.exists():
        time.sleep(0.05)
finally:
    lock.release()
"""
        proc = subprocess.Popen([sys.executable, "-c", holder_code])
        try:
            start = time.time()
            while not ready_file.exists() and (time.time() - start) < 5.0:
                time.sleep(0.05)
            self.assertTrue(ready_file.exists())

            # 3. Main process runs recovery
            recovery_mgr = TransactionRecoveryManager(self.workspace, store=store)
            res = recovery_mgr.recover(tx.id)

            # Invariant: Must fail closed, reporting ambiguity and zero rollbacks
            self.assertFalse(res.recovered)
            self.assertFalse(res.rolled_back)
            self.assertTrue(res.ambiguous)
            self.assertIn("actively locked", str(res.error))

            # Invariant: Filesystem was NOT touched
            self.assertTrue(dirty_file.exists())
            self.assertEqual(dirty_file.read_text(encoding="utf-8"), "agent dirty work")

            # Invariant: Transaction journal status was NOT altered to rolled back
            tx_after = store.get(tx.id)
            self.assertIsNotNone(tx_after)
            assert tx_after is not None
            self.assertEqual(tx_after.status, TransactionStatus.EXECUTING)
        finally:
            exit_file.write_text("done", encoding="utf-8")
            try:
                proc.wait(timeout=5.0)
            except Exception:
                proc.kill()

    # -------------------------------------------------------------------------
    # 10. Stale or malformed metadata never unlinks lock file before acquisition
    # -------------------------------------------------------------------------
    def test_10_stale_or_malformed_metadata_never_unlinks_lock_file(self) -> None:
        """Lock acquisition never unlinks workspace.lock before OS lock acquisition."""
        dot_bf = self.workspace / ".brainfrog"
        dot_bf.mkdir(parents=True, exist_ok=True)
        lock_file = dot_bf / "workspace.lock"
        meta_file = dot_bf / "workspace.lock.meta"

        # Pre-create lock file and malformed metadata
        lock_file.write_text("existing lock content", encoding="utf-8")
        meta_file.write_text("{malformed: bad json, expired_pid: 999999}", encoding="utf-8")

        lock = WorkspaceLock(self.workspace, timeout=1.0)
        acquired = lock.acquire()
        try:
            self.assertTrue(acquired)
            self.assertTrue(lock.is_active())
            self.assertTrue(lock_file.exists())

            # Metadata was repaired safely under the lock
            repaired_meta = json.loads(meta_file.read_text(encoding="utf-8"))
            self.assertEqual(repaired_meta["pid"], os.getpid())
            self.assertEqual(repaired_meta["workspace"], str(self.workspace))
        finally:
            lock.release()
            self.assertTrue(lock_file.exists(), "Lock file must remain on disk after release")

    # -------------------------------------------------------------------------
    # 11. Recovery acquires OS lock before reading or mutating state
    # -------------------------------------------------------------------------
    def test_11_recovery_acquires_lock_before_reading_or_mutating_state(self) -> None:
        """TransactionRecoveryManager.recover strictly acquires the OS lock prior to reading/rolling back."""
        store = InMemoryTransactionStore()
        op = TransactionOperation(
            type=OperationType.CREATE_FILE,
            target="recover_me.txt",
            after_state={"content": "to delete"},
            status=OperationStatus.EXECUTED,
        )
        tx = Transaction(
            id="tx_atomic_recover",
            workspace=str(self.workspace),
            operations=[op],
            status=TransactionStatus.EXECUTING,
        )
        store.save(tx)
        (self.workspace / "recover_me.txt").write_text("to delete", encoding="utf-8")

        lock_acquired_during_recover = False

        # Hook into recover to verify lock is held when reading/mutating
        orig_get = store.get
        def instrumented_get(tx_id: str):
            nonlocal lock_acquired_during_recover
            cap = get_active_workspace_capability(self.workspace)
            if cap is not None and cap.is_live():
                lock_acquired_during_recover = True
            return orig_get(tx_id)

        store.get = instrumented_get  # type: ignore

        mgr = TransactionRecoveryManager(self.workspace, store=store)
        res = mgr.recover(tx.id)

        self.assertTrue(res.recovered)
        self.assertTrue(res.rolled_back)
        self.assertTrue(lock_acquired_during_recover, "Lock was not held when reading transaction state")
        self.assertFalse((self.workspace / "recover_me.txt").exists())

    # -------------------------------------------------------------------------
    # 12. Recovery rollback conflicts preserve external changes
    # -------------------------------------------------------------------------
    def test_12_recovery_rollback_conflicts_preserve_external_changes(self) -> None:
        """When an external conflict is detected during recovery rollback, changes are preserved and ambiguous=True."""
        target = self.workspace / "user_edited.txt"
        target.write_text("user valuable changes", encoding="utf-8")

        store = InMemoryTransactionStore()
        op = TransactionOperation(
            type=OperationType.MODIFY_FILE,
            target="user_edited.txt",
            before_state={"content": "v1 initial"},
            after_state={"content": "v2 agent"},
            status=OperationStatus.EXECUTED,
        )
        tx = Transaction(
            id="tx_conflict_recovery",
            workspace=str(self.workspace),
            operations=[op],
            status=TransactionStatus.EXECUTING,
        )
        store.save(tx)

        mgr = TransactionRecoveryManager(self.workspace, store=store)
        res = mgr.recover(tx.id)

        self.assertFalse(res.recovered)
        self.assertFalse(res.rolled_back)
        self.assertTrue(res.ambiguous)
        # Content remains completely preserved
        self.assertEqual(target.read_text(encoding="utf-8"), "user valuable changes")

    # -------------------------------------------------------------------------
    # 13. Interrupted partial rollback retryability
    # -------------------------------------------------------------------------
    def test_13_interrupted_partial_rollback_retryability(self) -> None:
        """An interrupted rollback where first operation completed can be retried safely."""
        f1 = self.workspace / "f1.txt"
        f2 = self.workspace / "f2.txt"
        f1.write_text("agent created f1", encoding="utf-8")
        f2.write_text("agent created f2", encoding="utf-8")

        store = InMemoryTransactionStore()
        op1 = TransactionOperation(
            type=OperationType.CREATE_FILE,
            target="f1.txt",
            after_state={"content": "agent created f1"},
            status=OperationStatus.EXECUTED,
        )
        op2 = TransactionOperation(
            type=OperationType.CREATE_FILE,
            target="f2.txt",
            after_state={"content": "agent created f2"},
            status=OperationStatus.EXECUTED,
        )
        tx = Transaction(
            id="tx_interrupted_rollback",
            workspace=str(self.workspace),
            operations=[op1, op2],
            status=TransactionStatus.EXECUTING,
        )
        store.save(tx)

        # Simulate first operation (f2, since LIFO) rolled back and recorded ROLLED_BACK
        op2.status = OperationStatus.ROLLED_BACK
        f2.unlink()
        store.save(tx)

        # Run recovery: should skip op2 and roll back op1
        mgr = TransactionRecoveryManager(self.workspace, store=store)
        res = mgr.recover(tx.id)

        self.assertTrue(res.recovered)
        self.assertTrue(res.rolled_back)
        self.assertFalse(f1.exists())
        tx_after = store.get(tx.id)
        self.assertIsNotNone(tx_after)
        assert tx_after is not None
        self.assertEqual(tx_after.operations[0].status, OperationStatus.ROLLED_BACK)

    # -------------------------------------------------------------------------
    # 14. Proof persistence inside lock lifetime
    # -------------------------------------------------------------------------
    def test_14_proof_persistence_inside_lock_lifetime(self) -> None:
        """Proof generation, hashing, and writing remain strictly inside the active lock scope."""
        from core.runtime.workflow import TaskRequest, execute_autonomous_task

        lock_held_during_proof = False

        req = TaskRequest(
            task_id="task_proof_lock_check",
            user_request="Check proof locking",
            workspace=self.workspace,
        )

        with patch("core.runtime.workflow.FileProofStore.save") as mock_save:
            def instrumented_save(art, md):
                nonlocal lock_held_during_proof
                cap = get_active_workspace_capability(self.workspace)
                if cap is not None and cap.is_live():
                    lock_held_during_proof = True
                return (self.workspace / ".brainfrog" / "proofs" / "test.json", self.workspace / ".brainfrog" / "proofs" / "test.md")

            mock_save.side_effect = instrumented_save
            mock_runtime = MagicMock()
            from core.runtime.runtime import OutgoingMessage
            mock_runtime.handle_message.return_value = OutgoingMessage(
                text="success",
                success=True,
                status="completed",
            )
            try:
                execute_autonomous_task(mock_runtime, req)
            except Exception:
                pass

        self.assertTrue(lock_held_during_proof, "Proof save was executed outside the WorkspaceLock scope")

    # -------------------------------------------------------------------------
    # 15. Exceptions and cancellation do not leak locks
    # -------------------------------------------------------------------------
    def test_15_exceptions_and_cancellation_do_not_leak_locks(self) -> None:
        """Exceptions, cancellation, or early returns guarantee release in finally block without lock leak."""
        lock = WorkspaceLock(self.workspace)

        try:
            with lock:
                self.assertTrue(lock.is_active())
                raise KeyboardInterrupt("Simulated cancellation")
        except KeyboardInterrupt:
            pass

        self.assertFalse(lock.is_active())
        self.assertIsNone(get_active_workspace_capability(self.workspace))

        # Re-acquisition must succeed immediately without deadlocking
        acquired = lock.acquire(timeout=0.5)
        self.assertTrue(acquired)
        lock.release()

    # -------------------------------------------------------------------------
    # 16. Windows-specific locking behavior
    # -------------------------------------------------------------------------
    def test_16_windows_specific_locking_semantics(self) -> None:
        """On Windows, msvcrt locking must fail non-blockingly across handles on the same file."""
        if sys.platform != "win32":
            self.skipTest("Windows-specific locking test requires win32 platform")

        import msvcrt
        lock_file = self.workspace / ".brainfrog" / "workspace.lock"
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_file.touch()

        f1 = open(lock_file, "r+b")
        f2 = open(lock_file, "r+b")
        try:
            # Lock handle 1
            msvcrt.locking(f1.fileno(), msvcrt.LK_NBLCK, 1)

            # Handle 2 must raise OSError (errno 13 or 36)
            with self.assertRaises(OSError):
                msvcrt.locking(f2.fileno(), msvcrt.LK_NBLCK, 1)

            # Unlock handle 1
            msvcrt.locking(f1.fileno(), msvcrt.LK_UNLCK, 1)

            # Handle 2 can now lock
            msvcrt.locking(f2.fileno(), msvcrt.LK_NBLCK, 1)
            msvcrt.locking(f2.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            f1.close()
            f2.close()

    # -------------------------------------------------------------------------
    # 17. Unsupported platform fail-closed kernel locking
    # -------------------------------------------------------------------------
    def test_17_unsupported_platform_fails_closed_before_mutation(self) -> None:
        """When neither msvcrt nor fcntl is available, lock acquisition must fail closed immediately."""
        lock = WorkspaceLock(self.workspace)
        with patch("core.runtime.workspace_lock.msvcrt", None), \
             patch("core.runtime.workspace_lock._HAS_FCNTL", False):
            # Outermost acquisition must raise WorkspaceLockUnsupportedPlatformError
            with self.assertRaises(WorkspaceLockUnsupportedPlatformError) as ctx:
                lock.acquire()
            self.assertIn("neither 'msvcrt' nor 'fcntl.flock' is available", str(ctx.exception))
            self.assertFalse(lock.is_active())
            self.assertIsNone(get_active_workspace_capability(self.workspace))

            # No metadata or lock file should be treated as authoritative ownership
            self.assertFalse(lock.is_locked())

            # Attempting transaction mutations must fail closed without ambient authority
            coord = TransactionCoordinator(self.workspace)
            with self.assertRaises(WorkspaceLockRevokedError):
                coord.stage_create("unsupported.txt", "payload")

    # -------------------------------------------------------------------------
    # 18. Recovery state transition audit: Contention causes zero mutations
    # -------------------------------------------------------------------------
    def test_18_recovery_lock_contention_zero_journal_and_workspace_mutations(self) -> None:
        """When another process holds the workspace lock, recovery aborts with zero mutations."""
        store = InMemoryTransactionStore()
        test_file = self.workspace / "unrecovered.txt"
        test_file.write_text("initial disk content", encoding="utf-8")

        op = TransactionOperation(
            type=OperationType.CREATE_FILE,
            target="unrecovered.txt",
            after_state={"content": "agent mutated content"},
            status=OperationStatus.EXECUTED,
        )
        tx = Transaction(
            id="tx_contention_test",
            workspace=str(self.workspace),
            operations=[op],
            status=TransactionStatus.EXECUTING,
        )
        store.save(tx)
        orig_started = tx.started_at

        # Another process/context holds the workspace lock
        outer_lock = WorkspaceLock(self.workspace, session_id="holding_proc", actor="holder")
        outer_lock.acquire()
        try:
            mgr = TransactionRecoveryManager(self.workspace, store=store)
            res = mgr.recover(tx.id)

            self.assertFalse(res.recovered)
            self.assertFalse(res.rolled_back)
            self.assertTrue(res.ambiguous)
            self.assertIn("actively locked", res.error or "")

            # File content on disk is completely untouched
            self.assertEqual(test_file.read_text(encoding="utf-8"), "initial disk content")

            # Transaction record in store is completely untouched
            tx_in_store = store.get(tx.id)
            self.assertIsNotNone(tx_in_store)
            assert tx_in_store is not None
            self.assertEqual(tx_in_store.status, TransactionStatus.EXECUTING)
            self.assertEqual(tx_in_store.started_at, orig_started)
            self.assertEqual(tx_in_store.operations[0].status, OperationStatus.EXECUTED)
        finally:
            outer_lock.release()

    # -------------------------------------------------------------------------
    # 19. Recovery state transition audit: Terminal state immutability
    # -------------------------------------------------------------------------
    def test_19_terminal_states_remain_immutable_under_recovery(self) -> None:
        """All terminal transaction states are immutable and cannot be transitioned or mutated by recovery."""
        store = InMemoryTransactionStore()
        terminal_statuses = [
            TransactionStatus.COMMITTED,
            TransactionStatus.COMMITTED_UNTESTED,
            TransactionStatus.VERIFIED,
            TransactionStatus.ROLLED_BACK,
            TransactionStatus.FAILED,
        ]

        mgr = TransactionRecoveryManager(self.workspace, store=store)

        for status in terminal_statuses:
            with self.subTest(terminal_status=status):
                tx_id = f"tx_term_{status.value}"
                tx = Transaction(
                    id=tx_id,
                    workspace=str(self.workspace),
                    status=status,
                    failure_reason="Prior failure" if status == TransactionStatus.FAILED else None,
                )
                store.save(tx)

                # Direct transition attempt must fail
                self.assertTrue(tx.is_terminal)
                with self.assertRaises(InvalidTransactionTransition):
                    tx.transition(TransactionStatus.ROLLING_BACK)

                # Recovery attempt must preserve state without mutations
                res = mgr.recover(tx_id)
                self.assertEqual(res.final_status, status)
                self.assertEqual(res.previous_status, status)
                tx_after = store.get(tx_id)
                self.assertIsNotNone(tx_after)
                assert tx_after is not None
                self.assertEqual(tx_after.status, status)

    # -------------------------------------------------------------------------
    # 20. Recovery state transition audit: Conflict-failed transaction cannot be resumed
    # -------------------------------------------------------------------------
    def test_20_failed_conflict_transaction_cannot_be_resumed_as_normal_rollback(self) -> None:
        """A transaction marked FAILED because of an external conflict remains terminal and cannot be resumed."""
        store = InMemoryTransactionStore()
        conflict_file = self.workspace / "conflict.txt"
        conflict_file.write_text("user modified post-crash", encoding="utf-8")

        op = TransactionOperation(
            type=OperationType.CREATE_FILE,
            target="conflict.txt",
            after_state={"content": "agent text"},
            status=OperationStatus.FAILED,
            error="External mutation detected",
        )
        tx = Transaction(
            id="tx_failed_conflict",
            workspace=str(self.workspace),
            operations=[op],
            status=TransactionStatus.FAILED,
            failure_reason="External mutation detected on created file",
        )
        store.save(tx)

        mgr = TransactionRecoveryManager(self.workspace, store=store)
        res = mgr.recover(tx.id)

        # Must report terminal failure and ambiguous, NEVER rolled back or recovered
        self.assertFalse(res.recovered)
        self.assertFalse(res.rolled_back)
        self.assertTrue(res.ambiguous)
        self.assertEqual(res.final_status, TransactionStatus.FAILED)
        self.assertEqual(conflict_file.read_text(encoding="utf-8"), "user modified post-crash")

    # -------------------------------------------------------------------------
    # 21. Recovery state transition audit: Unverified cannot be promoted to VERIFIED
    # -------------------------------------------------------------------------
    def test_21_crash_between_git_commit_and_persistence_cannot_promote_to_verified(self) -> None:
        """If a commit marker claims 'verified' but initial status was not VERIFIED, finalize to COMMITTED_UNTESTED."""
        store = InMemoryTransactionStore()

        # Case A: Initial status was COMMITTED_UNTESTED (or EXECUTING) with marker 'verified'
        tx_unverified = Transaction(
            id="tx_marker_downgrade",
            workspace=str(self.workspace),
            status=TransactionStatus.EXECUTING,
            commit_marker={"status": "verified"},
        )
        store.save(tx_unverified)

        mgr = TransactionRecoveryManager(self.workspace, store=store)
        res = mgr.recover(tx_unverified.id)

        # Must be finalized to COMMITTED_UNTESTED, NOT VERIFIED!
        self.assertTrue(res.recovered)
        self.assertFalse(res.rolled_back)
        self.assertEqual(res.final_status, TransactionStatus.COMMITTED_UNTESTED)

        # Case B: Only if initial status was ALREADY VERIFIED can it remain VERIFIED
        tx_verified = Transaction(
            id="tx_marker_verified_legit",
            workspace=str(self.workspace),
            status=TransactionStatus.VERIFIED,
            commit_marker={"status": "verified"},
        )
        store.save(tx_verified)
        res_v = mgr.recover(tx_verified.id)
        self.assertEqual(res_v.final_status, TransactionStatus.VERIFIED)

    # -------------------------------------------------------------------------
    # 22. Recovery state transition audit: Repeated recovery attempts are idempotent
    # -------------------------------------------------------------------------
    def test_22_repeated_recovery_attempts_are_idempotent(self) -> None:
        """Repeated recovery calls on an interrupted transaction produce identical results and zero corruption."""
        store = InMemoryTransactionStore()
        f = self.workspace / "idem_file.txt"
        f.write_text("agent content", encoding="utf-8")

        op = TransactionOperation(
            type=OperationType.CREATE_FILE,
            target="idem_file.txt",
            after_state={"content": "agent content"},
            status=OperationStatus.EXECUTED,
        )
        tx = Transaction(
            id="tx_idem_test",
            workspace=str(self.workspace),
            operations=[op],
            status=TransactionStatus.EXECUTING,
        )
        store.save(tx)

        mgr = TransactionRecoveryManager(self.workspace, store=store)

        # First recovery restores/rolls back
        res1 = mgr.recover(tx.id)
        self.assertTrue(res1.recovered)
        self.assertTrue(res1.rolled_back)
        self.assertEqual(res1.final_status, TransactionStatus.ROLLED_BACK)
        self.assertFalse(f.exists())

        # Second recovery must be idempotent
        res2 = mgr.recover(tx.id)
        self.assertTrue(res2.recovered)
        self.assertTrue(res2.rolled_back)
        self.assertEqual(res2.final_status, TransactionStatus.ROLLED_BACK)
        self.assertFalse(f.exists())

        # Third recovery must also be idempotent
        res3 = mgr.recover(tx.id)
        self.assertEqual(res3.final_status, TransactionStatus.ROLLED_BACK)
        self.assertFalse(f.exists())


if __name__ == "__main__":
    unittest.main()

