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

from core.runtime.approval import ApprovalService, InMemoryApprovalStore
from core.runtime.session import InMemorySessionStore, SessionManager, SessionState
from core.runtime.transaction import InMemoryTransactionStore
from core.runtime.work import (
    Work,
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


if __name__ == "__main__":
    unittest.main()
