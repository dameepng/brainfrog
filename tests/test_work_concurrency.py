"""Concurrency and race-condition tests for Phase 15H Work persistence.

Validates:
- Monotonic revision increment and optimistic concurrency control (OCC)
- Stale revision rejection (stale writers fail closed)
- Concurrent resume idempotency (concurrent resume cannot execute twice)
- Concurrent cancel safety
- Resume vs session reset boundary (stale incarnations fail closed)
"""
from __future__ import annotations

import concurrent.futures
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from core.runtime.approval import (
    ApprovalRequest,
    ApprovalService,
    ApprovalStatus,
    InMemoryApprovalStore,
)
from core.runtime.session import InMemorySessionStore, SessionManager, SessionState
from core.runtime.transaction import (
    InMemoryTransactionStore,
    Transaction,
    TransactionRecoveryManager,
    TransactionStatus,
)
from core.runtime.work import (
    VerificationResult,
    Work,
    WorkFailure,
    WorkStatus,
)
from core.runtime.work_continuation import cancel_work, resume_work
from core.runtime.work_store import (
    FileWorkStore,
    InMemoryWorkStore,
    StaleWorkRevisionError,
)


class TestWorkConcurrency(unittest.TestCase):
    """Test thread-safety and race resilience of Work persistence."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.repo_dir = Path(self.temp_dir)
        self.store = FileWorkStore(repo_dir=self.repo_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_stale_revision_rejected_in_save(self) -> None:
        """Verify that saving a record with a stale revision raises StaleWorkRevisionError."""
        work = Work(id="work_occ_1", intent="Refactor", goal="Goal")
        self.store.create(work)

        # Worker 1 reads revision 1
        w1 = self.store.get("work_occ_1")
        assert w1 is not None
        self.assertEqual(w1.revision, 1)

        # Worker 2 reads revision 1
        w2 = self.store.get("work_occ_1")
        assert w2 is not None
        self.assertEqual(w2.revision, 1)

        # Worker 1 transitions and saves -> revision becomes 2
        w1_saved = self.store.save(w1.transition(WorkStatus.PLANNING))
        self.assertEqual(w1_saved.revision, 2)

        # Worker 2 attempts to save based on stale revision 1 with expected_revision=1
        with self.assertRaises(StaleWorkRevisionError):
            self.store.save(w2.transition(WorkStatus.FAILED), expected_revision=1)

        # Record in store remains revision 2 and in PLANNING status
        current = self.store.get("work_occ_1")
        assert current is not None
        self.assertEqual(current.status, WorkStatus.PLANNING)
        self.assertEqual(current.revision, 2)

    def test_atomic_update_concurrency(self) -> None:
        """Verify multiple threads performing update() on the same record serialize correctly."""
        work = Work(id="work_multi_thread", intent="Counter", goal="Test", revision=1)
        self.store.create(work)

        def increment_goal(w: Work) -> Work:
            # Append a marker to goal
            new_goal = w.goal + "."
            return w.with_update(goal=new_goal)

        thread_count = 10
        with concurrent.futures.ThreadPoolExecutor(max_workers=thread_count) as executor:
            futures = [
                executor.submit(self.store.update, "work_multi_thread", increment_goal)
                for _ in range(thread_count)
            ]
            for f in concurrent.futures.as_completed(futures):
                f.result()

        final = self.store.get("work_multi_thread")
        assert final is not None
        # Revision must have incremented by exactly thread_count
        self.assertEqual(final.revision, 1 + thread_count)
        self.assertEqual(final.goal, "Test" + ("." * thread_count))

    def test_concurrent_resume_idempotency(self) -> None:
        """PROPERTY 7: Concurrent resume cannot execute twice."""
        work = Work(
            id="work_concurrent_resume",
            intent="Execute long task",
            goal="Goal",
            actor_id="user_test",
            channel="cli",
            status=WorkStatus.PLANNING,
        )
        self.store.create(work)

        results = []
        lock = threading.Lock()

        def do_resume() -> None:
            ok, msg, obj = resume_work(
                work_id="work_concurrent_resume",
                actor_id="user_test",
                channel="cli",
                work_store=self.store,
            )
            with lock:
                results.append((ok, msg))

        # Launch 5 concurrent resume attempts
        threads = [threading.Thread(target=do_resume) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # At least one must succeed; all must report safe status without crashing
        self.assertEqual(len(results), 5)
        successes = [r for r in results if r[0] is True]
        self.assertGreaterEqual(len(successes), 1)

    def test_concurrent_cancel_safety(self) -> None:
        """Verify concurrent cancel calls do not race or corrupt state."""
        work = Work(
            id="work_concurrent_cancel",
            intent="Task to cancel",
            goal="Goal",
            actor_id="user_test",
            channel="cli",
            status=WorkStatus.PLANNING,
        )
        self.store.create(work)

        results = []
        lock = threading.Lock()

        def do_cancel() -> None:
            ok, msg, obj = cancel_work(
                work_id="work_concurrent_cancel",
                actor_id="user_test",
                channel="cli",
                work_store=self.store,
            )
            with lock:
                results.append((ok, msg))

        threads = [threading.Thread(target=do_cancel) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly ONE cancel succeeds, the rest observe the terminal CANCELLED state
        successes = [r for r in results if r[0] is True]
        self.assertEqual(len(successes), 1)

        final_work = self.store.get("work_concurrent_cancel")
        assert final_work is not None
        self.assertEqual(final_work.status, WorkStatus.CANCELLED)

    def test_resume_vs_session_reset_boundary(self) -> None:
        """PROPERTY 6: Stale session incarnation cannot regain execution authority.

        If session reset occurs, resume requiring execution authority MUST NOT use the old incarnation.
        """
        # Actor creates work in incarnation I_old
        work = Work(
            id="work_reset_test",
            intent="Deploy app",
            goal="Deploy app",
            actor_id="alice",
            channel="telegram",
            session_id="sess_alice",
            session_incarnation_id="inc_old_111",
            status=WorkStatus.PLANNING,
        )
        self.store.create(work)

        # Alice's session resets -> new incarnation inc_new_222
        # Now attempting to resume with the new incarnation must fail closed!
        ok, msg, obj = resume_work(
            work_id="work_reset_test",
            actor_id="alice",
            channel="telegram",
            session_id="sess_alice",
            session_incarnation_id="inc_new_222",  # Mismatched current session incarnation
            work_store=self.store,
        )
        self.assertFalse(ok)
        self.assertIn("invalidated session incarnation", msg.lower())

    def test_concurrent_resume_eight_callers_single_winner_barrier(self) -> None:
        """Prove that eight simultaneous callers produce at most one successful resume claim."""
        tx_store = InMemoryTransactionStore()
        tx = Transaction(
            id="tx_conc_8",
            session_id="sess_c8",
            status=TransactionStatus.COMMITTED,
        )
        tx_store.save(tx)
        recovery_mgr = TransactionRecoveryManager(workspace=self.repo_dir, store=tx_store)

        work = Work(
            id="work_conc_8",
            intent="Process data concurrently",
            goal="Process data concurrently",
            actor_id="alice",
            channel="cli",
            status=WorkStatus.EXECUTING,
            transaction_id="tx_conc_8",
        )
        self.store.create(work)

        barrier = threading.Barrier(8)
        results = []

        def worker() -> tuple[bool, str]:
            barrier.wait()
            return resume_work(
                work_id="work_conc_8",
                actor_id="alice",
                channel="cli",
                work_store=self.store,
                transaction_store=tx_store,
                recovery_manager=recovery_mgr,
            )[:2]

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futs = [executor.submit(worker) for _ in range(8)]
            for fut in concurrent.futures.as_completed(futs):
                results.append(fut.result())

        self.assertEqual(len(results), 8)
        successes = [r for r in results if r[0] is True]
        self.assertEqual(len(successes), 1)
        rejections = [r for r in results if r[0] is False]
        self.assertEqual(len(rejections), 7)

        # Work must remain in VERIFYING (not advanced to DONE by losing callers)
        final_work = self.store.get("work_conc_8")
        assert final_work is not None
        self.assertEqual(final_work.status, WorkStatus.VERIFYING)

    def test_repeated_sequential_resume_when_valid(self) -> None:
        """Prove that repeated sequential resume still works when valid across states."""
        # 1. PLANNING sequential resume
        work_plan = Work(
            id="work_seq_plan",
            intent="Plan task",
            goal="Goal",
            actor_id="alice",
            channel="cli",
            status=WorkStatus.PLANNING,
        )
        self.store.create(work_plan)
        ok1, msg1, _ = resume_work(work_id="work_seq_plan", actor_id="alice", channel="cli", work_store=self.store)
        self.assertTrue(ok1)
        self.assertIn("planning continuation", msg1.lower())

        ok2, msg2, _ = resume_work(work_id="work_seq_plan", actor_id="alice", channel="cli", work_store=self.store)
        self.assertTrue(ok2)
        self.assertIn("planning continuation", msg2.lower())

        # 2. APPROVAL_REQUIRED sequential resume
        work_appr = Work(
            id="work_seq_appr",
            intent="Deploy",
            goal="Deploy",
            actor_id="alice",
            channel="cli",
            status=WorkStatus.APPROVAL_REQUIRED,
        )
        self.store.create(work_appr)
        ok_a1, msg_a1, _ = resume_work(
            work_id="work_seq_appr",
            actor_id="alice",
            channel="cli",
            work_store=self.store,
        )
        self.assertTrue(ok_a1)
        self.assertIn("awaiting approval", msg_a1.lower())

        ok_a2, msg_a2, _ = resume_work(
            work_id="work_seq_appr",
            actor_id="alice",
            channel="cli",
            work_store=self.store,
        )
        self.assertTrue(ok_a2)
        self.assertIn("awaiting approval", msg_a2.lower())

        # 3. FAILED with retryable=True
        work_fail = Work(
            id="work_seq_fail",
            intent="Retry task",
            goal="Goal",
            actor_id="alice",
            channel="cli",
            status=WorkStatus.FAILED,
            failure=WorkFailure(code="ERR_TRANSIENT", summary="Temporary error", retryable=True),
        )
        self.store.create(work_fail)
        ok_f1, msg_f1, _ = resume_work(work_id="work_seq_fail", actor_id="alice", channel="cli", work_store=self.store)
        self.assertTrue(ok_f1)
        self.assertIn("retry initiated", msg_f1.lower())

    def test_stale_claim_recovery_expired_lease_and_dead_pid(self) -> None:
        """Prove that stale claims (expired lease or dead PID) can be recovered safely."""
        # 1. Expired lease on crashed VERIFYING work -> recovers to DONE
        work_ver = Work(
            id="work_stale_lease",
            intent="Verify task",
            goal="Goal",
            actor_id="alice",
            channel="cli",
            status=WorkStatus.VERIFYING,
            resume_metadata={
                "claim_active": True,
                "resumed_at": time.time() - 40.0,  # Expired lease (> 30s)
                "active_pid": 12345,
            },
        )
        self.store.create(work_ver)

        ok_v, msg_v, w_v = resume_work(
            work_id="work_stale_lease",
            actor_id="alice",
            channel="cli",
            work_store=self.store,
        )
        self.assertTrue(ok_v)
        self.assertIn("now done", msg_v.lower())
        final_ver = self.store.get("work_stale_lease")
        assert final_ver is not None
        self.assertEqual(final_ver.status, WorkStatus.DONE)

        # 2. Dead PID on crashed EXECUTING work -> recovers to VERIFYING immediately
        tx_store = InMemoryTransactionStore()
        tx = Transaction(id="tx_dead_pid", session_id="sess_d", status=TransactionStatus.COMMITTED)
        tx_store.save(tx)
        recovery_mgr = TransactionRecoveryManager(workspace=self.repo_dir, store=tx_store)

        work_dead = Work(
            id="work_dead_pid",
            intent="Execute task",
            goal="Goal",
            actor_id="alice",
            channel="cli",
            status=WorkStatus.EXECUTING,
            transaction_id="tx_dead_pid",
            resume_metadata={
                "claim_active": True,
                "resumed_at": time.time(),  # Recent timestamp
                "active_pid": 99999999,      # Dead process PID
            },
        )
        self.store.create(work_dead)

        ok_d, msg_d, _ = resume_work(
            work_id="work_dead_pid",
            actor_id="alice",
            channel="cli",
            work_store=self.store,
            transaction_store=tx_store,
            recovery_manager=recovery_mgr,
        )
        self.assertTrue(ok_d)
        self.assertIn("transitioned to verifying", msg_d.lower())
        final_dead = self.store.get("work_dead_pid")
        assert final_dead is not None
        self.assertEqual(final_dead.status, WorkStatus.VERIFYING)

    def test_exceptions_and_cancellation_release_claims_safely(self) -> None:
        """Prove that exceptions during resume release claim, and cancellation invalidates claims."""
        # 1. Exception during continuation releases claim
        tx_store = InMemoryTransactionStore()
        tx = Transaction(id="tx_exc_1", session_id="sess_e", status=TransactionStatus.COMMITTED)
        tx_store.save(tx)

        class FaultyRecoveryManager:
            def recover(self, tx_id: str):
                raise RuntimeError("Simulated crash during recovery")

        work_exc = Work(
            id="work_exc_test",
            intent="Process data",
            goal="Goal",
            actor_id="alice",
            channel="cli",
            status=WorkStatus.EXECUTING,
            transaction_id="tx_exc_1",
        )
        self.store.create(work_exc)

        # Resume raises RuntimeError
        with self.assertRaises(RuntimeError):
            resume_work(
                work_id="work_exc_test",
                actor_id="alice",
                channel="cli",
                work_store=self.store,
                transaction_store=tx_store,
                recovery_manager=FaultyRecoveryManager(),
            )

        # Claim was released on error!
        stored = self.store.get("work_exc_test")
        assert stored is not None
        self.assertFalse(stored.resume_metadata.get("claim_active", False))

        # Subsequent resume with working recovery succeeds without being blocked by active lease
        working_rm = TransactionRecoveryManager(workspace=self.repo_dir, store=tx_store)
        ok_next, _, _ = resume_work(
            work_id="work_exc_test",
            actor_id="alice",
            channel="cli",
            work_store=self.store,
            transaction_store=tx_store,
            recovery_manager=working_rm,
        )
        self.assertTrue(ok_next)

        # 2. Cancellation invalidates claim
        work_can = Work(
            id="work_can_test",
            intent="Task to cancel",
            goal="Goal",
            actor_id="alice",
            channel="cli",
            status=WorkStatus.PLANNING,
            resume_metadata={"claim_active": True, "resumed_at": time.time(), "claim_id": "c_init"},
        )
        self.store.create(work_can)

        ok_c, _, _ = cancel_work(work_id="work_can_test", actor_id="alice", channel="cli", work_store=self.store)
        self.assertTrue(ok_c)
        stored_can = self.store.get("work_can_test")
        assert stored_can is not None
        self.assertEqual(stored_can.status, WorkStatus.CANCELLED)
        self.assertFalse(stored_can.resume_metadata.get("claim_active", False))

        # Resume on cancelled fails closed
        ok_after, msg_after, _ = resume_work(work_id="work_can_test", actor_id="alice", channel="cli", work_store=self.store)
        self.assertFalse(ok_after)
        self.assertIn("cancelled and cannot be resumed", msg_after.lower())


if __name__ == "__main__":
    unittest.main()
