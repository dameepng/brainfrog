"""Phase 14B-2: Cross-Process Approval Atomicity and TOCTOU Remediation Tests.

Validates the remediation of HIGH-01:
- Multi-process approval race condition & TOCTOU immunity.
- Cross-process OS-level file locking with FileApprovalStore.
- Exactly one winner across concurrent independent OS processes.
- Preservation of authorization checks (digest, session, channel, expiration).
- Crash safety: OS automatically releases locks if holding process dies.
- Preservation of thread-level concurrency within the same process.
"""
from __future__ import annotations

import hashlib
import multiprocessing
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.runtime.approval import (
    ApprovalRequest,
    ApprovalService,
    ApprovalStatus,
    CanonicalOperation,
    FileApprovalStore,
    _CrossProcessLock,
    _get_cross_process_lock,
)


# =============================================================================
# Top-Level Worker Functions (Pickle-safe for multiprocessing spawn on Windows)
# =============================================================================

def _process_worker_consume(
    approvals_dir_str: str,
    request_id: str,
    digest: str,
    session_id: str,
    channel: str,
    barrier: Any,
    queue: Any,
) -> None:
    """Worker executed in an independent OS process to attempt approval consumption."""
    store = FileApprovalStore(approvals_dir=Path(approvals_dir_str))
    if barrier is not None:
        try:
            barrier.wait(timeout=10.0)
        except Exception:
            pass

    ok, req, msg = store.claim_and_consume(
        request_id=request_id,
        expected_digest=digest,
        session_id=session_id,
        channel=channel,
    )
    status_str = req.status.value if req and hasattr(req, "status") else None
    queue.put((ok, status_str, msg))


def _process_stress_worker(
    worker_id: int,
    approvals_dir_str: str,
    task_queue: Any,
    result_queue: Any,
    start_barrier: Any,
    finish_barrier: Any,
) -> None:
    """Persistent worker running in an independent OS process for multi-iteration stress testing."""
    store = FileApprovalStore(approvals_dir=Path(approvals_dir_str))
    while True:
        task = task_queue.get()
        if task is None:
            break
        req_id, digest, session, channel = task
        if start_barrier is not None:
            try:
                start_barrier.wait(timeout=10.0)
            except Exception:
                pass

        ok, req, msg = store.claim_and_consume(
            request_id=req_id,
            expected_digest=digest,
            session_id=session,
            channel=channel,
        )
        status_str = req.status.value if req and hasattr(req, "status") else None
        result_queue.put((worker_id, ok, status_str, msg))

        if finish_barrier is not None:
            try:
                finish_barrier.wait(timeout=10.0)
            except Exception:
                pass


def _process_hybrid_worker(
    worker_id: int,
    approvals_dir_str: str,
    request_id: str,
    digest: str,
    session_id: str,
    channel: str,
    start_barrier: Any,
    result_queue: Any,
    threads_per_process: int = 4,
) -> None:
    """Worker process that spawns multiple concurrent threads within its address space."""
    store = FileApprovalStore(approvals_dir=Path(approvals_dir_str))
    thread_barrier = threading.Barrier(threads_per_process)

    def _thread_target(tid: int) -> None:
        if start_barrier is not None and tid == 0:
            try:
                start_barrier.wait(timeout=10.0)
            except Exception:
                pass
        try:
            thread_barrier.wait(timeout=10.0)
        except Exception:
            pass

        ok, req, msg = store.claim_and_consume(
            request_id=request_id,
            expected_digest=digest,
            session_id=session_id,
            channel=channel,
        )
        status_str = req.status.value if req and hasattr(req, "status") else None
        result_queue.put((worker_id, tid, ok, status_str, msg))

    threads = []
    for t_idx in range(threads_per_process):
        t = threading.Thread(target=_thread_target, args=(t_idx,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join(timeout=10.0)


# =============================================================================
# Phase 14B-2 Test Suite
# =============================================================================

class TestCrossProcessApprovalAtomicity(unittest.TestCase):
    """Test suite verifying cross-process mutual exclusion and TOCTOU immunity in FileApprovalStore."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="bf_race_test_")
        self.approvals_dir = Path(self.temp_dir) / ".brainfrog" / "approvals"
        self.store = FileApprovalStore(approvals_dir=self.approvals_dir)
        self.service = ApprovalService(store=self.store, two_man_rule_enabled=False)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_approved_request(
        self,
        target: str = "config.py",
        action: str = "write_files",
        session_id: str = "session_alpha",
        channel: str = "telegram",
        ttl_seconds: float = 300.0,
    ) -> Tuple[ApprovalRequest, CanonicalOperation]:
        op = CanonicalOperation(action_type=action, target=target)
        req = self.service.create_request(
            session_id=session_id,
            channel=channel,
            user_id="user_requester",
            conversation_id="conv_1",
            operation_type=action,
            canonical_operation=op,
            ttl_seconds=ttl_seconds,
        )
        ok, msg, approved = self.service.approve(req.request_id, "user_approver", channel, session_id)
        self.assertTrue(ok, f"Setup approval failed: {msg}")
        self.assertIsNotNone(approved)
        assert approved is not None
        self.assertEqual(approved.status, ApprovalStatus.APPROVED)
        return approved, op

    def test_two_process_consume_race(self) -> None:
        """Test A: Two independent OS processes racing to consume the same approval.

        Invariant: Exactly 1 succeeds, exactly 1 fails, final state is CONSUMED.
        """
        approved_req, op = self._create_approved_request()
        barrier = multiprocessing.Barrier(2)
        queue = multiprocessing.Queue()

        processes = []
        for _ in range(2):
            p = multiprocessing.Process(
                target=_process_worker_consume,
                args=(
                    str(self.approvals_dir),
                    approved_req.request_id,
                    op.compute_digest(),
                    "session_alpha",
                    "telegram",
                    barrier,
                    queue,
                ),
            )
            processes.append(p)
            p.start()

        for p in processes:
            p.join(timeout=10.0)

        results = []
        while not queue.empty():
            results.append(queue.get())

        self.assertEqual(len(results), 2)
        successes = [r for r in results if r[0] is True]
        failures = [r for r in results if r[0] is False]

        self.assertEqual(len(successes), 1, "Exactly one process must win the consumption race")
        self.assertEqual(len(failures), 1, "The competing process must fail deterministically")
        self.assertIn("already been consumed", failures[0][2])

        # Verify disk state
        reloaded = self.store.get(approved_req.request_id)
        self.assertIsNotNone(reloaded)
        assert reloaded is not None
        self.assertEqual(reloaded.status, ApprovalStatus.CONSUMED)

    def test_many_process_race_eight_workers(self) -> None:
        """Test B: 8 independent OS processes racing to consume the same approval.

        Invariant: Exactly 1 succeeds, 7 fail, final state is CONSUMED.
        """
        approved_req, op = self._create_approved_request()
        num_workers = 8
        barrier = multiprocessing.Barrier(num_workers)
        queue = multiprocessing.Queue()

        processes = []
        for _ in range(num_workers):
            p = multiprocessing.Process(
                target=_process_worker_consume,
                args=(
                    str(self.approvals_dir),
                    approved_req.request_id,
                    op.compute_digest(),
                    "session_alpha",
                    "telegram",
                    barrier,
                    queue,
                ),
            )
            processes.append(p)
            p.start()

        for p in processes:
            p.join(timeout=15.0)

        results = []
        while not queue.empty():
            results.append(queue.get())

        self.assertEqual(len(results), num_workers)
        successes = [r for r in results if r[0] is True]
        failures = [r for r in results if r[0] is False]

        self.assertEqual(len(successes), 1, "Exactly one winner among 8 concurrent processes")
        self.assertEqual(len(failures), 7, "All 7 losers must be rejected")
        for f in failures:
            self.assertIn("already been consumed", f[2])

        # Final persistent state verification
        final_req = self.store.get(approved_req.request_id)
        self.assertIsNotNone(final_req)
        assert final_req is not None
        self.assertEqual(final_req.status, ApprovalStatus.CONSUMED)

    def test_subsequent_process_consumption_rejected(self) -> None:
        """Test C: After winner consumes, a subsequent independent process attempts consumption."""
        approved_req, op = self._create_approved_request()

        # First consumption
        ok1, req1, msg1 = self.store.claim_and_consume(
            approved_req.request_id,
            op.compute_digest(),
            "session_alpha",
            "telegram",
        )
        self.assertTrue(ok1)
        self.assertIsNotNone(req1)
        assert req1 is not None
        self.assertEqual(req1.status, ApprovalStatus.CONSUMED)

        # Subsequent attempt by a separate OS process
        queue = multiprocessing.Queue()
        p = multiprocessing.Process(
            target=_process_worker_consume,
            args=(
                str(self.approvals_dir),
                approved_req.request_id,
                op.compute_digest(),
                "session_alpha",
                "telegram",
                None,
                queue,
            ),
        )
        p.start()
        p.join(timeout=5.0)

        res = queue.get()
        self.assertFalse(res[0], "Subsequent consumption must be denied")
        self.assertIn("already been consumed", res[2])

    def test_expired_request_multiprocess_race_zero_successes(self) -> None:
        """Test D: Multiple independent processes attempting to consume an expired approval."""
        # Create request with 0.05s TTL
        approved_req, op = self._create_approved_request(ttl_seconds=0.05)
        time.sleep(0.1)  # Ensure expiry

        num_workers = 4
        barrier = multiprocessing.Barrier(num_workers)
        queue = multiprocessing.Queue()

        processes = []
        for _ in range(num_workers):
            p = multiprocessing.Process(
                target=_process_worker_consume,
                args=(
                    str(self.approvals_dir),
                    approved_req.request_id,
                    op.compute_digest(),
                    "session_alpha",
                    "telegram",
                    barrier,
                    queue,
                ),
            )
            processes.append(p)
            p.start()

        for p in processes:
            p.join(timeout=10.0)

        results = []
        while not queue.empty():
            results.append(queue.get())

        successes = [r for r in results if r[0] is True]
        self.assertEqual(len(successes), 0, "No process may consume an expired approval")

        reloaded = self.store.get(approved_req.request_id)
        self.assertIsNotNone(reloaded)
        assert reloaded is not None
        self.assertEqual(reloaded.status, ApprovalStatus.EXPIRED)

    def test_rejected_request_multiprocess_race_zero_successes(self) -> None:
        """Test E: Multiple independent processes attempting to consume a rejected approval."""
        op = CanonicalOperation(action_type="write_files", target="config.py")
        req = self.service.create_request(
            session_id="session_alpha",
            channel="telegram",
            user_id="user_requester",
            conversation_id="conv_1",
            operation_type="write_files",
            canonical_operation=op,
        )
        self.service.reject(req.request_id, "user_approver", "telegram", "Denied by security admin")

        num_workers = 4
        barrier = multiprocessing.Barrier(num_workers)
        queue = multiprocessing.Queue()

        processes = []
        for _ in range(num_workers):
            p = multiprocessing.Process(
                target=_process_worker_consume,
                args=(
                    str(self.approvals_dir),
                    req.request_id,
                    op.compute_digest(),
                    "session_alpha",
                    "telegram",
                    barrier,
                    queue,
                ),
            )
            processes.append(p)
            p.start()

        for p in processes:
            p.join(timeout=10.0)

        results = []
        while not queue.empty():
            results.append(queue.get())

        successes = [r for r in results if r[0] is True]
        self.assertEqual(len(successes), 0, "No process may consume a rejected approval")

    def test_invalid_digest_multiprocess_race_zero_successes(self) -> None:
        """Test F: Multiple processes attempting consumption with forged operation digest."""
        approved_req, _ = self._create_approved_request()
        forged_digest = "0000000000000000000000000000000000000000000000000000000000000000"

        num_workers = 4
        barrier = multiprocessing.Barrier(num_workers)
        queue = multiprocessing.Queue()

        processes = []
        for _ in range(num_workers):
            p = multiprocessing.Process(
                target=_process_worker_consume,
                args=(
                    str(self.approvals_dir),
                    approved_req.request_id,
                    forged_digest,
                    "session_alpha",
                    "telegram",
                    barrier,
                    queue,
                ),
            )
            processes.append(p)
            p.start()

        for p in processes:
            p.join(timeout=10.0)

        results = []
        while not queue.empty():
            results.append(queue.get())

        successes = [r for r in results if r[0] is True]
        self.assertEqual(len(successes), 0, "Forged digest must be rejected across all processes")
        for f in results:
            self.assertIn("Operation digest mismatch", f[2])

    def test_wrong_session_channel_multiprocess_zero_successes(self) -> None:
        """Test G: Multiple processes attempting consumption from mismatched session or channel."""
        approved_req, op = self._create_approved_request()

        # Process with wrong session
        q1 = multiprocessing.Queue()
        p1 = multiprocessing.Process(
            target=_process_worker_consume,
            args=(
                str(self.approvals_dir),
                approved_req.request_id,
                op.compute_digest(),
                "session_attacker",
                "telegram",
                None,
                q1,
            ),
        )
        p1.start()
        p1.join(timeout=5.0)
        res1 = q1.get()
        self.assertFalse(res1[0])
        self.assertIn("Session mismatch", res1[2])

        # Process with wrong channel
        q2 = multiprocessing.Queue()
        p2 = multiprocessing.Process(
            target=_process_worker_consume,
            args=(
                str(self.approvals_dir),
                approved_req.request_id,
                op.compute_digest(),
                "session_alpha",
                "whatsapp",
                None,
                q2,
            ),
        )
        p2.start()
        p2.join(timeout=5.0)
        res2 = q2.get()
        self.assertFalse(res2[0])
        self.assertIn("Channel mismatch", res2[2])

    def test_process_crash_releases_lock(self) -> None:
        """Test H: If an external process crashes while holding a lock, OS automatically releases it."""
        lock_path = self.approvals_dir / ".locks" / "crash_test.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        child_script = (
            "import sys, time, os\n"
            "from pathlib import Path\n"
            "try: import msvcrt\n"
            "except ImportError: msvcrt = None\n"
            "try: import fcntl\n"
            "except ImportError: fcntl = None\n"
            "f = open(sys.argv[1], 'a+b')\n"
            "if msvcrt:\n"
            "    f.seek(0)\n"
            "    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)\n"
            "elif fcntl:\n"
            "    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "print('HELD', flush=True)\n"
            "time.sleep(0.5)\n"
            "os._exit(99)\n"  # Hard abort without __exit__
        )

        proc = subprocess.Popen(
            [sys.executable, "-c", child_script, str(lock_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertIsNotNone(proc.stdout)
        assert proc.stdout is not None
        line = proc.stdout.readline()
        self.assertIn("HELD", line)

        proc.wait(timeout=5.0)
        self.assertEqual(proc.returncode, 99)
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()

        # After crash, parent should be able to acquire lock immediately
        lock = _get_cross_process_lock(lock_path, timeout=1.0)
        acquired = False
        with lock:
            acquired = True
        self.assertTrue(acquired, "OS kernel must automatically release file lock on process termination")

    def test_stress_fifty_iterations_eight_processes(self) -> None:
        """Section 7: 50 independent race iterations x 8 concurrent processes.

        In each iteration:
        - 1 fresh approval request in APPROVED state.
        - 8 independent OS processes race to consume the same request simultaneously.
        - Exactly 1 process succeeds; exactly 7 processes receive deterministic failure.
        - Final persisted state is CONSUMED.
        - Cumulative across 50 iterations: exactly 50 successes, exactly 350 failures.
        """
        num_workers = 8
        num_iterations = 50

        task_queues = [multiprocessing.Queue() for _ in range(num_workers)]
        result_queue = multiprocessing.Queue()
        start_barrier = multiprocessing.Barrier(num_workers)
        finish_barrier = multiprocessing.Barrier(num_workers + 1)

        workers = []
        for wid in range(num_workers):
            p = multiprocessing.Process(
                target=_process_stress_worker,
                args=(
                    wid,
                    str(self.approvals_dir),
                    task_queues[wid],
                    result_queue,
                    start_barrier,
                    finish_barrier,
                ),
            )
            workers.append(p)
            p.start()

        total_successes = 0
        total_failures = 0

        try:
            for i in range(num_iterations):
                op = CanonicalOperation(action_type="write_files", target=f"stress_{i}.py")
                req = self.service.create_request(
                    session_id=f"session_{i}",
                    channel="telegram",
                    user_id=f"user_{i}",
                    conversation_id=f"conv_{i}",
                    operation_type="write_files",
                    canonical_operation=op,
                )
                ok_app, _, app_req = self.service.approve(req.request_id, "approver_admin", "telegram", f"session_{i}")
                self.assertTrue(ok_app)

                task = (req.request_id, op.compute_digest(), f"session_{i}", "telegram")
                for q in task_queues:
                    q.put(task)

                finish_barrier.wait(timeout=10.0)

                iter_results = []
                for _ in range(num_workers):
                    iter_results.append(result_queue.get(timeout=5.0))

                successes = [r for r in iter_results if r[1] is True]
                failures = [r for r in iter_results if r[1] is False]

                self.assertEqual(len(successes), 1, f"Iteration {i}: Expected exactly 1 success, got {len(successes)}")
                self.assertEqual(len(failures), num_workers - 1, f"Iteration {i}: Expected {num_workers - 1} failures, got {len(failures)}")
                for f in failures:
                    self.assertIn("already been consumed", f[3])

                total_successes += len(successes)
                total_failures += len(failures)

                reloaded = self.store.get(req.request_id)
                self.assertIsNotNone(reloaded)
                assert reloaded is not None
                self.assertEqual(reloaded.status, ApprovalStatus.CONSUMED)

            self.assertEqual(total_successes, num_iterations, f"Expected exactly {num_iterations} total successes")
            self.assertEqual(total_failures, num_iterations * (num_workers - 1), f"Expected exactly {num_iterations * (num_workers - 1)} total failures")

        finally:
            for q in task_queues:
                q.put(None)
            for p in workers:
                p.join(timeout=5.0)

    def test_stress_hundred_iterations_eight_processes(self) -> None:
        """Section 8: 100 independent race iterations x 8 concurrent processes.

        In each iteration:
        - 1 fresh approval request in APPROVED state.
        - 8 independent OS processes race to consume the same request simultaneously.
        - Exactly 1 process succeeds; exactly 7 processes receive deterministic failure.
        - Cumulative across 100 iterations: exactly 100 successes, exactly 700 failures.
        """
        num_workers = 8
        num_iterations = 100

        task_queues = [multiprocessing.Queue() for _ in range(num_workers)]
        result_queue = multiprocessing.Queue()
        start_barrier = multiprocessing.Barrier(num_workers)
        finish_barrier = multiprocessing.Barrier(num_workers + 1)

        workers = []
        for wid in range(num_workers):
            p = multiprocessing.Process(
                target=_process_stress_worker,
                args=(
                    wid,
                    str(self.approvals_dir),
                    task_queues[wid],
                    result_queue,
                    start_barrier,
                    finish_barrier,
                ),
            )
            workers.append(p)
            p.start()

        total_successes = 0
        total_failures = 0

        try:
            for i in range(num_iterations):
                op = CanonicalOperation(action_type="write_files", target=f"stress100_{i}.py")
                req = self.service.create_request(
                    session_id=f"session100_{i}",
                    channel="telegram",
                    user_id=f"user100_{i}",
                    conversation_id=f"conv100_{i}",
                    operation_type="write_files",
                    canonical_operation=op,
                )
                ok_app, _, app_req = self.service.approve(req.request_id, "approver_admin", "telegram", f"session100_{i}")
                self.assertTrue(ok_app)

                task = (req.request_id, op.compute_digest(), f"session100_{i}", "telegram")
                for q in task_queues:
                    q.put(task)

                finish_barrier.wait(timeout=10.0)

                iter_results = []
                for _ in range(num_workers):
                    iter_results.append(result_queue.get(timeout=5.0))

                successes = [r for r in iter_results if r[1] is True]
                failures = [r for r in iter_results if r[1] is False]

                self.assertEqual(len(successes), 1, f"Iteration {i}: Expected exactly 1 success, got {len(successes)}")
                self.assertEqual(len(failures), num_workers - 1, f"Iteration {i}: Expected {num_workers - 1} failures, got {len(failures)}")
                for f in failures:
                    self.assertIn("already been consumed", f[3])

                total_successes += len(successes)
                total_failures += len(failures)

                reloaded = self.store.get(req.request_id)
                self.assertIsNotNone(reloaded)
                assert reloaded is not None
                self.assertEqual(reloaded.status, ApprovalStatus.CONSUMED)

            self.assertEqual(total_successes, num_iterations, f"Expected exactly {num_iterations} total successes")
            self.assertEqual(total_failures, num_iterations * (num_workers - 1), f"Expected exactly {num_iterations * (num_workers - 1)} total failures")

        finally:
            for q in task_queues:
                q.put(None)
            for p in workers:
                p.join(timeout=5.0)

    def test_hybrid_four_processes_four_threads_race(self) -> None:
        """Section 14: 4 independent OS processes x 4 threads each (16 concurrent claimants).

        Guarantees that thread-level locking + process-level locking have no gaps.
        Invariant: Exactly 1 global winner, 15 failures, final state CONSUMED.
        """
        approved_req, op = self._create_approved_request()
        num_processes = 4
        threads_per_process = 4
        total_workers = num_processes * threads_per_process

        start_barrier = multiprocessing.Barrier(num_processes)
        result_queue = multiprocessing.Queue()

        processes = []
        for pid in range(num_processes):
            p = multiprocessing.Process(
                target=_process_hybrid_worker,
                args=(
                    pid,
                    str(self.approvals_dir),
                    approved_req.request_id,
                    op.compute_digest(),
                    "session_alpha",
                    "telegram",
                    start_barrier,
                    result_queue,
                    threads_per_process,
                ),
            )
            processes.append(p)
            p.start()

        for p in processes:
            p.join(timeout=15.0)

        results = []
        while not result_queue.empty():
            results.append(result_queue.get())

        self.assertEqual(len(results), total_workers)
        successes = [r for r in results if r[2] is True]
        failures = [r for r in results if r[2] is False]

        self.assertEqual(len(successes), 1, f"Expected exactly 1 global success among {total_workers} workers, got {len(successes)}")
        self.assertEqual(len(failures), total_workers - 1)

        final_req = self.store.get(approved_req.request_id)
        self.assertIsNotNone(final_req)
        assert final_req is not None
        self.assertEqual(final_req.status, ApprovalStatus.CONSUMED)


if __name__ == "__main__":
    unittest.main()
