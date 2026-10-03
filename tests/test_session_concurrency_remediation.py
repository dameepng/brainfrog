"""Phase 14B-L01.2: Concurrency & Incarnation Remediation Test Suite.

Validates the remediation of:
- L01.1-F01: Stale snapshot lost update (Optimistic Concurrency Control)
- L01.1-F02: Stale save resurrecting /reset state (Incarnation & Reset Guard)

Guarantees:
- Invariant 1: No lost committed updates for serialized same-session writes
- Invariant 2: Reset is a hard state boundary
- Invariant 3: Old history cannot resurrect after reset
- Invariant 4: Old incarnation cannot resurrect after reset
- Invariant 5: H-02 remains closed (stale approvals cannot execute)
- Independent concurrency across different sessions (no global locks)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List

from core.runtime.messages import IncomingMessage, OutgoingMessage
from core.runtime.runtime import BrainFrogRuntime
from core.runtime.session import (
    FileSessionStore,
    InMemorySessionStore,
    SessionManager,
    SessionState,
    StaleSessionStateError,
)
from core.runtime.approval import (
    ApprovalService,
    ApprovalStatus,
    CanonicalOperation,
    FileApprovalStore,
)
from system1.base import Answer, SystemOneClient


class MockFastS1(SystemOneClient):
    """Deterministic, non-blocking S1 for concurrency tests."""

    name: str = "mock_fast_s1"

    def decide(self, state: Dict[str, Any], questions: Dict[str, Any]) -> Dict[str, Answer]:
        return {
            "likely_domain": Answer(choice="unrelated", confidence=0.9),
            "change_type": Answer(choice="question_only", confidence=0.95),
            "is_sensitive": Answer(noul=0.01, confidence=0.99),
            "complexity": Answer(score=0, confidence=0.95),
            "needs_tests": Answer(noul=0.01, confidence=0.99),
        }


class MockEchoS2:
    """Deterministic mock S2 that returns predictable responses."""

    provider_name: str = "mock_echo_s2"

    def __init__(self, reply: str = "Ack", delay: float = 0.0) -> None:
        self.reply = reply
        self.delay = delay

    def diagnose(self, task: str, *args: Any, **kwargs: Any) -> str:
        if self.delay > 0:
            time.sleep(self.delay)
        return f"{self.reply}: {task[:40]}"

    def plan_and_prd(self, task: str, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        if self.delay > 0:
            time.sleep(self.delay)
        return {"title": "Plan", "steps": []}


class TestSessionConcurrencyRemediation(unittest.TestCase):
    """Rigorous verification suite for Phase 14B-L01.2 Concurrency & Incarnation Remediation."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="bf_concurrency_test_")
        self.repo_dir = Path(self.temp_dir).resolve()
        self.sessions_dir = (self.repo_dir / ".brainfrog" / "sessions").resolve()
        self.approvals_dir = (self.repo_dir / ".brainfrog" / "approvals").resolve()

        (self.repo_dir / "src").mkdir(parents=True, exist_ok=True)
        (self.repo_dir / "README.md").write_text("# Test\n", encoding="utf-8")
        modules_data = {"core": {"name": "core", "description": "core", "path": "core"}}
        (self.repo_dir / "modules.json").write_text(json.dumps(modules_data), encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # =========================================================================
    # Step 10: Test A — Two Concurrent Writers (Lost Update Prevention)
    # =========================================================================
    def test_a_two_concurrent_writers_lost_update_prevented(self) -> None:
        """Test A: Writer A commits; Writer B with stale snapshot is rejected, preserving Writer A's committed update."""
        store = FileSessionStore(sessions_dir=self.sessions_dir)
        session_id = "cli:user_occ:conv_1"

        # 1. Initialize session on disk
        sess_init = SessionState(
            session_id=session_id,
            channel="cli",
            user_id="user_occ",
            conversation_id="conv_1",
        )
        sess_init.record_interaction("initial turn", "initial reply")
        store.save(sess_init)
        self.assertEqual(sess_init.revision, 1)

        # 2. Both Writer A and Writer B load the session at revision 1
        writer_a = store.load(session_id)
        writer_b = store.load(session_id)
        assert writer_a is not None and writer_b is not None
        self.assertEqual(writer_a.revision, 1)
        self.assertEqual(writer_b.revision, 1)

        # 3. Writer A commits Turn A
        writer_a.record_interaction("turn A [msg-A-101]", "reply A")
        self.assertTrue(store.save(writer_a))
        self.assertEqual(writer_a.revision, 2)

        # 4. Writer B attempts to commit Turn B using its stale revision 1 snapshot
        writer_b.record_interaction("turn B [msg-B-202]", "reply B")
        with self.assertRaises(StaleSessionStateError) as ctx:
            store.save(writer_b)
        self.assertIn("revision", str(ctx.exception).lower())

        # 5. Verify on disk: Writer A's Turn A is intact! Not lost or overwritten!
        reloaded = store.load(session_id)
        assert reloaded is not None
        self.assertEqual(reloaded.revision, 2)
        users = [t["user"] for t in reloaded.history]
        self.assertIn("turn A [msg-A-101]", users)
        self.assertNotIn("turn B [msg-B-202]", users)

        # 6. Writer B performs OCC retry: reloads latest state (rev 2), appends Turn B, commits -> rev 3
        writer_b_fresh = store.load(session_id)
        assert writer_b_fresh is not None
        self.assertEqual(writer_b_fresh.revision, 2)
        writer_b_fresh.record_interaction("turn B [msg-B-202]", "reply B")
        self.assertTrue(store.save(writer_b_fresh))
        self.assertEqual(writer_b_fresh.revision, 3)

        # 7. Verify both updates are committed
        final = store.load(session_id)
        assert final is not None
        self.assertEqual(final.revision, 3)
        final_users = [t["user"] for t in final.history]
        self.assertIn("turn A [msg-A-101]", final_users)
        self.assertIn("turn B [msg-B-202]", final_users)

    # =========================================================================
    # Step 10: Test B — 2 Concurrent Processes
    # =========================================================================
    def test_b_two_concurrent_processes(self) -> None:
        """Test B: 2 separate OS processes concurrently write unique turns to same session under OCC retry."""
        self._run_multiprocess_occ_test(num_workers=2, turns_per_worker=5)

    # =========================================================================
    # Step 10: Test C — 4 Concurrent Processes
    # =========================================================================
    def test_c_four_concurrent_processes(self) -> None:
        """Test C: 4 separate OS processes concurrently write unique turns to same session under OCC retry."""
        self._run_multiprocess_occ_test(num_workers=4, turns_per_worker=5)

    # =========================================================================
    # Step 10: Test D — 8 Concurrent Processes
    # =========================================================================
    def test_d_eight_concurrent_processes(self) -> None:
        """Test D: 8 separate OS processes concurrently write unique turns to same session under OCC retry."""
        self._run_multiprocess_occ_test(num_workers=8, turns_per_worker=4)

    def _run_multiprocess_occ_test(self, num_workers: int, turns_per_worker: int) -> None:
        """Helper to run N subprocesses concurrently writing to a single session."""
        store = FileSessionStore(sessions_dir=self.sessions_dir)
        session_id = f"cli:proc_test:shared_{num_workers}"

        # Initialize session
        init_sess = SessionState(
            session_id=session_id,
            channel="cli",
            user_id="proc_test",
            conversation_id=f"shared_{num_workers}",
            max_history_entries=100,
        )
        store.save(init_sess)

        worker_script = """
import sys, time
from pathlib import Path
from core.runtime.session import FileSessionStore, StaleSessionStateError

worker_id = int(sys.argv[1])
turns_count = int(sys.argv[2])
sessions_dir = Path(sys.argv[3])
session_id = sys.argv[4]

store = FileSessionStore(sessions_dir=sessions_dir)

for turn in range(turns_count):
    msg_id = f"proc-{worker_id}-turn-{turn}"
    committed = False
    for attempt in range(50):
        try:
            sess = store.load(session_id)
            if sess is None:
                time.sleep(0.02)
                continue
            sess.record_interaction(f"msg:{msg_id}", f"resp:{msg_id}")
            if store.save(sess):
                committed = True
                break
        except StaleSessionStateError:
            time.sleep(0.01 * (attempt % 5 + 1))
    if not committed:
        sys.stderr.write(f"Worker {worker_id} failed to commit {msg_id}\\n")
        sys.exit(1)

sys.exit(0)
"""
        python_bin = sys.executable
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).parent.parent.resolve())

        processes = []
        for w in range(num_workers):
            p = subprocess.Popen(
                [python_bin, "-c", worker_script, str(w), str(turns_per_worker), str(self.sessions_dir), session_id],
                cwd=str(self.repo_dir),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            processes.append((w, p))

        # Wait for all processes to complete
        for w, p in processes:
            stdout, stderr = p.communicate(timeout=60.0)
            self.assertEqual(p.returncode, 0, f"Worker {w} failed: {stderr}\n{stdout}")

        # Verify final logical state on disk
        final_sess = store.load(session_id)
        assert final_sess is not None
        expected_total_turns = num_workers * turns_per_worker
        self.assertEqual(len(final_sess.history), expected_total_turns)

        # Verify EVERY unique message ID was recorded without any lost update
        found_users = {t["user"] for t in final_sess.history}
        for w in range(num_workers):
            for t in range(turns_per_worker):
                expected_tag = f"msg:proc-{w}-turn-{t}"
                self.assertIn(expected_tag, found_users, f"Committed turn {expected_tag} was lost!")

    # =========================================================================
    # Step 11: Test E — Save After Reset Rejected (F02 Fix)
    # =========================================================================
    def test_e_save_after_reset_rejected(self) -> None:
        """Test E: Stale session save after /reset is rejected; old history and incarnation cannot resurrect."""
        store = FileSessionStore(sessions_dir=self.sessions_dir)
        mgr = SessionManager(store=store)
        session_id = "telegram:f02_user:chat_1"

        # 1. Establish session with Incarnation X and history
        session_x = mgr.get_or_create("telegram", "f02_user", "chat_1")
        session_x.record_interaction("Prompt under Incarnation X", "Response X")
        mgr.save(session_x)
        inc_x = session_x.session_incarnation_id
        session_file = store._get_session_path(session_id)
        self.assertTrue(session_file.exists())

        # Keep a stale copy of session_x in memory
        stale_x = store.load(session_id)
        assert stale_x is not None
        self.assertEqual(stale_x.session_incarnation_id, inc_x)

        # 2. Reset session (simulating /reset)
        mgr.reset(session_id)
        self.assertFalse(session_file.exists())

        # 3. New incarnation Y is established
        session_y = mgr.get_or_create("telegram", "f02_user", "chat_1")
        inc_y = session_y.session_incarnation_id
        self.assertNotEqual(inc_x, inc_y)

        # 4. Attempt to save the old stale state (Incarnation X)
        stale_x.record_interaction("Stale prompt after reset", "Stale reply")
        with self.assertRaises(StaleSessionStateError) as ctx:
            store.save(stale_x)
        self.assertIn("incarnation", str(ctx.exception).lower())

        # 5. Invariant: Old history absent, old incarnation absent
        self.assertFalse(session_file.exists())

        # 6. Save valid new incarnation Y
        session_y.record_interaction("Fresh turn under Incarnation Y", "Reply Y")
        mgr.save(session_y)
        self.assertTrue(session_file.exists())

        # 7. Reload and verify authoritative state is Incarnation Y only
        reloaded = store.load(session_id)
        assert reloaded is not None
        self.assertEqual(reloaded.session_incarnation_id, inc_y)
        self.assertEqual(len(reloaded.history), 1)
        self.assertEqual(reloaded.history[0]["user"], "Fresh turn under Incarnation Y")

    # =========================================================================
    # Step 11: Test F — Reset While Request Is In Flight
    # =========================================================================
    def test_f_reset_while_request_in_flight(self) -> None:
        """Test F: An in-flight request executing under incarnation X is safely rejected if /reset occurs mid-execution."""
        store = FileSessionStore(sessions_dir=self.sessions_dir)
        mgr = SessionManager(store=store)

        s1 = MockFastS1()
        s2 = MockEchoS2(delay=0.1)  # Artificial delay to guarantee race window

        runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            sessions=mgr,
            system1_factory=lambda b: s1,
            system2_factory=lambda **k: s2,
        )

        session_id = "cli:local:chat_race"

        # 1. Start with initial interaction
        msg1 = IncomingMessage(id="msg-1", text="Initial question", channel="cli", user_id="local", conversation_id="chat_race")
        res1 = runtime.handle_message(msg1)
        self.assertTrue(res1.success, f"Initial message failed: {res1.error}")

        sess = mgr.get(session_id)
        assert sess is not None
        inc_x = sess.session_incarnation_id

        # 2. Launch long request in background thread
        in_flight_response: List[OutgoingMessage] = []
        barrier = threading.Barrier(2)

        def worker_flight() -> None:
            # Signal ready, then send message that takes 100ms in S2
            barrier.wait()
            msg_flight = IncomingMessage(id="msg-inflight", text="Long in-flight task", channel="cli", user_id="local", conversation_id="chat_race")
            resp = runtime.handle_message(msg_flight)
            in_flight_response.append(resp)

        t = threading.Thread(target=worker_flight)
        t.start()

        # Wait until thread is about to send message
        barrier.wait()
        # Brief pause to ensure worker entered runtime.handle_message and acquired session_x
        time.sleep(0.04)

        # 3. Issue /reset in main thread while request is in flight
        reset_msg = IncomingMessage(id="msg-reset", text="/reset", channel="cli", user_id="local", conversation_id="chat_race")
        reset_resp = runtime.handle_message(reset_msg)
        self.assertTrue(reset_resp.success)
        self.assertIn("reset", reset_resp.text.lower())

        t.join(timeout=5.0)
        self.assertEqual(len(in_flight_response), 1)
        flight_out = in_flight_response[0]

        # 4. In-flight request MUST be safely rejected! Not persisted!
        self.assertFalse(flight_out.success)
        self.assertEqual(flight_out.status, "rejected")
        self.assertIn("Session state changed", flight_out.text)

        # 5. Invariant: Persistent storage does NOT resurrect old history or old incarnation
        final_sess = mgr.get(session_id)
        assert final_sess is not None
        self.assertNotEqual(final_sess.session_incarnation_id, inc_x)
        self.assertEqual(len(final_sess.history), 0)

    # =========================================================================
    # Step 11: Test G — Repeated Race (100 Iterations)
    # =========================================================================
    def test_g_repeated_race_100_iterations(self) -> None:
        """Test G: 100 repeated race iterations between in-flight save and /reset yield exactly 0 resurrection events."""
        store = FileSessionStore(sessions_dir=self.sessions_dir)
        mgr = SessionManager(store=store)

        s1 = MockFastS1()
        s2 = MockEchoS2(delay=0.005)

        runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            sessions=mgr,
            system1_factory=lambda b: s1,
            system2_factory=lambda **k: s2,
        )

        resurrection_count = 0

        for i in range(100):
            session_id = f"cli:local:iter_{i}"
            # 1. Create initial state
            m_init = IncomingMessage(id=f"init-{i}", text="hello", channel="cli", user_id="local", conversation_id=f"iter_{i}")
            runtime.handle_message(m_init)

            sess_before = mgr.get(session_id)
            assert sess_before is not None
            old_inc = sess_before.session_incarnation_id

            # 2. Race in-flight request vs /reset
            results: List[OutgoingMessage] = []

            def worker_race() -> None:
                m_race = IncomingMessage(id=f"race-{i}", text="race prompt", channel="cli", user_id="local", conversation_id=f"iter_{i}")
                out = runtime.handle_message(m_race)
                results.append(out)

            t = threading.Thread(target=worker_race)
            t.start()

            # Slight jitter then reset
            time.sleep(0.002)
            m_reset = IncomingMessage(id=f"reset-{i}", text="/reset", channel="cli", user_id="local", conversation_id=f"iter_{i}")
            runtime.handle_message(m_reset)

            t.join()

            # 3. Check for any resurrection
            fresh_store = FileSessionStore(sessions_dir=self.sessions_dir)
            target_file = fresh_store._get_session_path(session_id)
            if target_file.exists():
                disk_data = json.loads(target_file.read_text(encoding="utf-8"))
                if disk_data.get("session_incarnation_id") == old_inc:
                    resurrection_count += 1
                for h in disk_data.get("history", []):
                    if "hello" in h.get("user", ""):
                        resurrection_count += 1

        self.assertEqual(resurrection_count, 0, f"Detected {resurrection_count} resurrection events across 100 runs!")

    # =========================================================================
    # Step 12: Test H — H-02 Regression Test (Stale Approval Invalidation)
    # =========================================================================
    def test_h_approval_session_invalidation_h02_remains_closed(self) -> None:
        """Test H: Approvals bound to incarnation X cannot be claimed after /reset, and stale session save cannot restore them."""
        approval_store = FileApprovalStore(approvals_dir=self.approvals_dir)
        approval_svc = ApprovalService(store=approval_store)
        session_store = FileSessionStore(sessions_dir=self.sessions_dir)
        session_mgr = SessionManager(store=session_store)

        s1 = MockFastS1()
        s2 = MockEchoS2()

        runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            sessions=session_mgr,
            approval_service=approval_svc,
            system1_factory=lambda b: s1,
            system2_factory=lambda **k: s2,
        )

        session_id = "telegram:alpha_user:chat_h02"
        sess = session_mgr.get_or_create("telegram", "alpha_user", "chat_h02")
        inc_x = sess.session_incarnation_id

        # 1. Create an approval request bound to Incarnation X
        canonical_op = CanonicalOperation(action_type="write_code", target="core/security.py", parameters={"code": "pass"})
        req = approval_svc.create_request(
            session_id=session_id,
            channel="telegram",
            user_id="alpha_user",
            conversation_id="chat_h02",
            operation_type="write_code",
            canonical_operation=canonical_op,
            risk_class="HIGH",
            session_incarnation_id=inc_x,
        )
        self.assertEqual(req.session_incarnation_id, inc_x)

        # Approver approves it
        ok_app, msg_app, _ = approval_svc.approve(req.request_id, approver_id="security_admin", channel="telegram")
        self.assertTrue(ok_app)

        # 2. Reset the session via /reset
        reset_res = runtime.handle_message(IncomingMessage(id="cmd-reset", channel="telegram", user_id="alpha_user", conversation_id="chat_h02", text="/reset"))
        self.assertTrue(reset_res.success)

        # Current incarnation is now Y
        sess_y = session_mgr.get(session_id)
        assert sess_y is not None
        inc_y = sess_y.session_incarnation_id
        self.assertNotEqual(inc_x, inc_y)

        # 3. Attempt to claim old approval under current Incarnation Y -> MUST BE REJECTED!
        is_valid, _, reason = approval_svc.verify_and_consume(
            request_id=req.request_id,
            expected_digest=req.operation_digest,
            session_id=session_id,
            channel="telegram",
            session_incarnation_id=inc_y,
            requester_id="alpha_user",
        )
        self.assertFalse(is_valid)
        self.assertIn("cancelled", reason.lower())

        # 4. Attempt stale session save with Incarnation X -> MUST BE REJECTED!
        stale_state = SessionState(
            session_id=session_id,
            channel="telegram",
            user_id="alpha_user",
            conversation_id="chat_h02",
            session_incarnation_id=inc_x,
            _persisted_incarnation=inc_x,
        )
        with self.assertRaises(StaleSessionStateError):
            session_store.save(stale_state)

        # 5. Old approval remains permanently unusable
        current_req = approval_store.get(req.request_id)
        assert current_req is not None
        self.assertEqual(current_req.status, ApprovalStatus.CANCELLED)

    # =========================================================================
    # Step 13: Test I — Cross-Session Concurrency
    # =========================================================================
    def test_i_cross_session_concurrency_no_global_lock(self) -> None:
        """Test I: Sessions A, B, C, D write concurrently without blocking or contaminating each other."""
        store = FileSessionStore(sessions_dir=self.sessions_dir)
        mgr = SessionManager(store=store)

        session_ids = [f"telegram:user_{i}:chat_{i}" for i in range(4)]
        errors: List[Exception] = []

        def worker_session(sid: str) -> None:
            try:
                s = mgr.get_or_create("telegram", sid.split(":")[1], sid.split(":")[2])
                for turn in range(10):
                    s.record_interaction(f"prompt from {sid} turn {turn}", f"reply {turn}")
                    mgr.save(s)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker_session, args=(sid,)) for sid in session_ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)

        # Verify each session has exactly 10 turns and contains only its own data
        for sid in session_ids:
            s_loaded = store.load(sid)
            assert s_loaded is not None
            self.assertEqual(len(s_loaded.history), 10)
            for entry in s_loaded.history:
                self.assertIn(sid, entry["user"])

    # =========================================================================
    # Step 14: Test J — Restart Recovery and Concurrency
    # =========================================================================
    def test_j_restart_recovery_and_concurrency(self) -> None:
        """Test J: Persisted revision & incarnation survive restart; stale pre-restart snapshot cannot overwrite."""
        store1 = FileSessionStore(sessions_dir=self.sessions_dir)
        session_id = "cli:restart_user:conv_1"

        sess1 = SessionState(session_id=session_id, channel="cli", user_id="restart_user", conversation_id="conv_1")
        sess1.record_interaction("Turn 1", "Reply 1")
        store1.save(sess1)
        self.assertEqual(sess1.revision, 1)
        inc1 = sess1.session_incarnation_id

        # Keep a stale snapshot in memory
        stale_snapshot = SessionState(
            session_id=session_id,
            channel="cli",
            user_id="restart_user",
            conversation_id="conv_1",
            session_incarnation_id=inc1,
            revision=1,
            _persisted_revision=1,
            _persisted_incarnation=inc1,
        )

        # Advance session on disk to revision 2
        sess1.record_interaction("Turn 2", "Reply 2")
        store1.save(sess1)
        self.assertEqual(sess1.revision, 2)

        # Simulate complete process termination & restart
        store2 = FileSessionStore(sessions_dir=self.sessions_dir)
        loaded2 = store2.load(session_id)
        assert loaded2 is not None
        self.assertEqual(loaded2.revision, 2)
        self.assertEqual(loaded2.session_incarnation_id, inc1)
        self.assertEqual(len(loaded2.history), 2)

        # Stale snapshot from before restart tries to save (expected revision 1, disk is 2)
        stale_snapshot.record_interaction("Stale Turn", "Stale Reply")
        with self.assertRaises(StaleSessionStateError):
            store2.save(stale_snapshot)

        # Loaded session advances to revision 3
        loaded2.record_interaction("Turn 3", "Reply 3")
        self.assertTrue(store2.save(loaded2))
        self.assertEqual(loaded2.revision, 3)

        # Verify final disk state
        reloaded = store2.load(session_id)
        assert reloaded is not None
        self.assertEqual(reloaded.revision, 3)
        self.assertEqual(len(reloaded.history), 3)

    # =========================================================================
    # Step 15: Test K — Legacy Session Compatibility
    # =========================================================================
    def test_k_legacy_session_compatibility(self) -> None:
        """Test K: Legacy session JSON files without 'revision' are deterministically loaded as revision 1 and upgraded on save."""
        store = FileSessionStore(sessions_dir=self.sessions_dir)
        session_id = "cli:legacy_user:default"
        session_file = store._get_session_path(session_id)

        legacy_data = {
            "schema_version": 1,
            "session_id": session_id,
            "session_incarnation_id": "legacy_incarnation_12345678",
            "channel": "cli",
            "user_id": "legacy_user",
            "conversation_id": "default",
            "created_at": time.time() - 100,
            "last_active_at": time.time() - 50,
            "active_mode": "build",
            "history": [
                {"timestamp": time.time() - 90, "user": "legacy turn 1", "assistant": "legacy resp 1", "metadata": {}},
            ],
            "max_history_entries": 50,
            "max_history_bytes": 262144,
            "max_message_bytes": 32768,
        }
        session_file.write_text(json.dumps(legacy_data, indent=2), encoding="utf-8")

        # 1. Load legacy session: defaults revision to 1
        loaded = store.load(session_id)
        assert loaded is not None
        self.assertEqual(loaded.revision, 1)
        self.assertEqual(loaded._persisted_revision, 1)
        self.assertEqual(loaded.session_incarnation_id, "legacy_incarnation_12345678")

        # 2. Save upgraded session: monotonically advances to revision 2
        loaded.record_interaction("modern turn 2", "modern resp 2")
        self.assertTrue(store.save(loaded))
        self.assertEqual(loaded.revision, 2)

        # Verify disk now has revision 2
        disk_data = json.loads(session_file.read_text(encoding="utf-8"))
        self.assertEqual(disk_data.get("revision"), 2)

        # 3. An old snapshot holding expected revision 1 is rejected
        stale_legacy = SessionState(
            session_id=session_id,
            channel="cli",
            user_id="legacy_user",
            conversation_id="default",
            session_incarnation_id="legacy_incarnation_12345678",
            revision=1,
            _persisted_revision=1,
            _persisted_incarnation="legacy_incarnation_12345678",
        )
        stale_legacy.record_interaction("stale overwrite", "stale")
        with self.assertRaises(StaleSessionStateError):
            store.save(stale_legacy)

    # =========================================================================
    # Test L: In-Memory Store OCC & Reset Equivalence
    # =========================================================================
    def test_l_in_memory_store_occ_and_reset_equivalence(self) -> None:
        """Test L: InMemorySessionStore provides exact OCC and reset semantics matching FileSessionStore."""
        mem_store = InMemorySessionStore()
        session_id = "cli:mem_user:test"

        s1 = SessionState(session_id=session_id, channel="cli", user_id="mem_user", conversation_id="test")
        mem_store.save(s1)
        self.assertEqual(s1.revision, 1)

        # Snapshot A and B are independent cloned snapshots from load()
        snap_a = mem_store.load(session_id)
        snap_b = mem_store.load(session_id)
        assert snap_a is not None and snap_b is not None

        # A commits -> rev 2
        snap_a.record_interaction("turn A", "resp A")
        self.assertTrue(mem_store.save(snap_a))
        self.assertEqual(snap_a.revision, 2)

        # B stale commit -> rejected because disk revision is now 2, but snap_b expected 1
        snap_b.record_interaction("turn B", "resp B")
        with self.assertRaises(StaleSessionStateError):
            mem_store.save(snap_b)

        # Reset
        mem_store.reset(session_id, new_incarnation="new_inc_xyz")
        # Stale save with old incarnation -> rejected
        with self.assertRaises(StaleSessionStateError):
            mem_store.save(snap_a)


if __name__ == "__main__":
    unittest.main()
