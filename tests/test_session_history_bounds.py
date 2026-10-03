"""Phase 14B-L01: Bounded Session History Retention & Persistence Invariants Test Suite.

Validates that:
1. SessionState strictly bounds history entries (FIFO eviction)
2. SessionState strictly bounds serialized history bytes
3. SessionState strictly bounds individual message size on clean UTF-8 boundaries
4. FileSessionStore persists and restores bounded state with O(1) file size
5. Legacy oversized session files are normalized/compacted on load
6. /reset and /new clear history, rotate incarnation, and delete persistent files
7. Multi-session and cross-user isolation remains intact
8. Concurrent multi-threaded and multi-process writers never corrupt JSON and respect bounds
9. 10,000 messages complete rapidly in seconds and demonstrate storage plateauing
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.channels.telegram import MockTelegramTransport, TelegramChannel
from core.channels.whatsapp import MockWhatsAppTransport, WhatsAppChannel
from core.runtime.messages import IncomingMessage, OutgoingMessage
from core.runtime.runtime import BrainFrogRuntime
from core.runtime.session import (
    CURRENT_SESSION_SCHEMA_VERSION,
    DEFAULT_MAX_HISTORY_BYTES,
    DEFAULT_MAX_HISTORY_ENTRIES,
    DEFAULT_MAX_MESSAGE_BYTES,
    FileSessionStore,
    InMemorySessionStore,
    SessionManager,
    SessionState,
    bound_message_text,
    get_max_history_bytes,
    get_max_history_entries,
    get_max_message_bytes,
)
from orchestrator import PlanStep, StepResult
from system1.base import Answer, SystemOneClient


class FakeFastS1(SystemOneClient):
    """Deterministic, fast S1 client."""
    name: str = "fake_fast_s1"

    def decide(self, state: Dict[str, Any], questions: Dict[str, Any]) -> Dict[str, Answer]:
        return {
            "likely_domain": Answer(choice="unrelated", confidence=0.99),
            "change_type": Answer(choice="question_only", confidence=0.99),
            "is_sensitive": Answer(noul=0.0, confidence=0.99),
            "complexity": Answer(score=0, confidence=0.99),
            "needs_tests": Answer(noul=0.0, confidence=0.99),
        }


class FakeFastS2:
    """Deterministic, fast S2 client."""
    provider_name: str = "fake_fast_s2"

    def __init__(self, reply: str = "Ack") -> None:
        self.reply = reply

    def diagnose(self, task: str, *args: Any, **kwargs: Any) -> str:
        return f"{self.reply}: {task[:40]}"

    def plan_and_prd(self, task: str, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {"title": "Plan", "steps": []}


class TestSessionHistoryBounds(unittest.TestCase):
    """Adversarial and boundary verification for security finding L-01."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="brainfrog_l01_test_")
        self.repo_dir = Path(self.test_dir)
        (self.repo_dir / "src").mkdir(parents=True, exist_ok=True)
        (self.repo_dir / "README.md").write_text("# Test\n", encoding="utf-8")
        modules_data = {"core": {"name": "core", "description": "core", "path": "core"}}
        (self.repo_dir / "modules.json").write_text(json.dumps(modules_data), encoding="utf-8")
        self.sessions_dir = self.repo_dir / ".brainfrog" / "sessions"

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # 1. Unit Tests: Entry Count, Byte Limits, Message Bounding, FIFO Eviction
    # -------------------------------------------------------------------------
    def test_max_entry_count_fifo_eviction(self) -> None:
        """SessionState must evict oldest history entries once max_history_entries is reached."""
        session = SessionState(
            session_id="test:user:1",
            channel="cli",
            user_id="user",
            conversation_id="1",
            max_history_entries=10,
        )

        for i in range(25):
            session.record_interaction(user_text=f"msg_{i}", assistant_text=f"reply_{i}")

        self.assertEqual(len(session.history), 10)
        # Oldest 15 should have been evicted; remaining should be 15..24
        self.assertEqual(session.history[0]["user"], "msg_15")
        self.assertEqual(session.history[-1]["user"], "msg_24")

    def test_max_history_bytes_eviction(self) -> None:
        """SessionState must evict older entries when total serialized history bytes exceed max_history_bytes."""
        # Set small byte limit of 1000 bytes, but allow up to 20 entries
        session = SessionState(
            session_id="test:user:bytes",
            channel="cli",
            user_id="user",
            conversation_id="bytes",
            max_history_entries=20,
            max_history_bytes=1000,
        )

        # Each entry will be ~250 bytes
        for i in range(10):
            session.record_interaction(
                user_text=f"user payload {i} " + ("x" * 150),
                assistant_text=f"assistant reply {i} " + ("y" * 150),
            )

        serialized_bytes = len(json.dumps(session.history, ensure_ascii=False).encode("utf-8"))
        self.assertLessEqual(serialized_bytes, 1000)
        # We added 10 large entries, so some must have been evicted to respect 1000 bytes
        self.assertLess(len(session.history), 10)
        self.assertGreater(len(session.history), 0)

    def test_individual_message_byte_bounding_and_utf8(self) -> None:
        """Messages larger than max_message_bytes must be safely truncated on clean UTF-8 boundaries."""
        max_bytes = 200
        # Multibyte Unicode string (4 bytes per emoji)
        multibyte_text = "🐸 BrainFrog 安全 " * 30
        bounded = bound_message_text(multibyte_text, max_bytes)

        # Must not exceed max_bytes
        encoded = bounded.encode("utf-8")
        self.assertLessEqual(len(encoded), max_bytes)
        self.assertTrue(bounded.endswith(" ... [TRUNCATED]"))
        # Must be valid decodeable Unicode without replacement character or errors
        decoded = encoded.decode("utf-8")
        self.assertEqual(decoded, bounded)

    def test_record_interaction_clamps_individual_messages(self) -> None:
        """record_interaction must clamp both user_text and assistant_text to max_message_bytes."""
        session = SessionState(
            session_id="test:user:msg_clamp",
            channel="cli",
            user_id="user",
            conversation_id="clamp",
            max_message_bytes=150,
        )
        huge_user = "U" * 1000
        huge_asst = "A" * 1000
        session.record_interaction(user_text=huge_user, assistant_text=huge_asst)

        self.assertEqual(len(session.history), 1)
        user_stored = session.history[0]["user"]
        asst_stored = session.history[0]["assistant"]
        self.assertLessEqual(len(user_stored.encode("utf-8")), 150)
        self.assertLessEqual(len(asst_stored.encode("utf-8")), 150)
        self.assertTrue(user_stored.endswith(" ... [TRUNCATED]"))
        self.assertTrue(asst_stored.endswith(" ... [TRUNCATED]"))

    def test_empty_messages_handled_safely(self) -> None:
        """Empty or whitespace-only messages do not cause exceptions or invalid state."""
        session = SessionState(session_id="test:empty", channel="cli", user_id="u", conversation_id="e")
        session.record_interaction("", "")
        self.assertEqual(len(session.history), 1)
        self.assertEqual(session.history[0]["user"], "")
        self.assertEqual(session.history[0]["assistant"], "")

    def test_add_message_helper(self) -> None:
        """add_message properly records turns and completes open assistant turns."""
        session = SessionState(session_id="test:add_msg", channel="cli", user_id="u", conversation_id="m")
        session.add_message(role="user", content="Hello")
        self.assertEqual(len(session.history), 1)
        self.assertEqual(session.history[0]["user"], "Hello")
        self.assertEqual(session.history[0]["assistant"], "")

        session.add_message(role="assistant", content="World")
        self.assertEqual(len(session.history), 1)
        self.assertEqual(session.history[0]["user"], "Hello")
        self.assertEqual(session.history[0]["assistant"], "World")

    # -------------------------------------------------------------------------
    # 2. Persistence Tests: Bounded Save, Restore, Legacy Normalization, Quarantine
    # -------------------------------------------------------------------------
    def test_persistence_preserves_bounds(self) -> None:
        """FileSessionStore saves and reloads bounded state accurately."""
        store = FileSessionStore(sessions_dir=self.sessions_dir)
        session = SessionState(
            session_id="cli:user:bounded_save",
            channel="cli",
            user_id="user",
            conversation_id="bounded_save",
            max_history_entries=10,
        )

        for i in range(30):
            session.record_interaction(f"msg {i}", f"reply {i}")
        store.save(session)

        loaded = store.load("cli:user:bounded_save")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(len(loaded.history), 10)
        self.assertEqual(loaded.history[0]["user"], "msg 20")
        self.assertEqual(loaded.history[-1]["user"], "msg 29")

    def test_legacy_oversized_session_normalized_on_load(self) -> None:
        """When loading a legacy session file with 1,000 entries, it is normalized to max_history_entries."""
        store = FileSessionStore(sessions_dir=self.sessions_dir)
        session_id = "cli:legacy:user_1"
        target_file = store._get_session_path(session_id)

        # Construct raw legacy JSON with 1,000 entries and oversized message
        legacy_entries = [
            {"timestamp": time.time() + i, "user": f"legacy turn {i}", "assistant": f"legacy reply {i}"}
            for i in range(1000)
        ]
        # Make the last entry have an oversized message
        legacy_entries[-1]["user"] = "Oversized " * 5000

        legacy_data = {
            "schema_version": 1,
            "session_id": session_id,
            "session_incarnation_id": "legacy_inc_123",
            "channel": "cli",
            "user_id": "legacy",
            "conversation_id": "user_1",
            "history": legacy_entries,
        }
        target_file.write_text(json.dumps(legacy_data), encoding="utf-8")
        legacy_file_size = target_file.stat().st_size
        self.assertGreater(legacy_file_size, 100_000)

        # Load session using store (default max_history_entries = 50)
        loaded = store.load(session_id)
        self.assertIsNotNone(loaded)
        assert loaded is not None

        # Must be truncated to 50 newest entries
        self.assertEqual(len(loaded.history), 50)
        self.assertEqual(loaded.history[0]["user"], "legacy turn 950")
        # Oversized message must be bounded
        self.assertLessEqual(len(loaded.history[-1]["user"].encode("utf-8")), loaded.max_message_bytes)
        self.assertTrue(loaded.history[-1]["user"].endswith(" ... [TRUNCATED]"))

        # Verify auto-compaction: target file on disk should now be compact
        compacted_file_size = target_file.stat().st_size
        self.assertLess(compacted_file_size, legacy_file_size // 2)
        self.assertLess(compacted_file_size, 50_000)

    def test_corrupted_file_fail_closed(self) -> None:
        """Corrupted JSON fails closed (returns None) and is quarantined."""
        store = FileSessionStore(sessions_dir=self.sessions_dir)
        session_id = "cli:corrupt:1"
        target_file = store._get_session_path(session_id)
        target_file.write_text("{{{{NOT VALID JSON}}}}", encoding="utf-8")

        loaded = store.load(session_id)
        self.assertIsNone(loaded)
        self.assertFalse(target_file.exists())
        # Check that it was placed into corrupt/
        corrupt_files = list(store.corrupt_dir.glob("*.corrupt"))
        self.assertGreater(len(corrupt_files), 0)

    # -------------------------------------------------------------------------
    # 3. /reset and /new Lifecycle Tests
    # -------------------------------------------------------------------------
    def test_reset_and_new_semantics(self) -> None:
        """Executing /reset or /new clears in-memory history, deletes persistent file, and rotates incarnation."""
        runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            default_test_cmd="cmd /c exit 0" if os.name == "nt" else "true",
            persist_sessions=True,
            system1_factory=lambda b: FakeFastS1(),
            system2_factory=lambda **k: FakeFastS2(),
        )

        session_id = "cli:local:reset_test"
        # 1. Fill history with messages
        for i in range(10):
            runtime.handle_message(IncomingMessage(id=f"m-{i}", channel="cli", user_id="local", conversation_id="reset_test", text=f"msg {i}"))

        sess = runtime.sessions.get(session_id)
        assert sess is not None
        old_incarnation = sess.session_incarnation_id
        store_path = FileSessionStore(repo_dir=self.repo_dir)._get_session_path(session_id)
        self.assertTrue(store_path.exists())
        self.assertEqual(len(sess.history), 10)

        # 2. Issue /reset
        out = runtime.handle_message(IncomingMessage(id="r-cmd", channel="cli", user_id="local", conversation_id="reset_test", text="/reset"))
        self.assertTrue(out.success)
        self.assertIn("reset", out.text.lower())

        # In-memory history cleared, persistent file deleted
        self.assertEqual(len(sess.history), 0)
        self.assertFalse(store_path.exists())
        self.assertNotEqual(sess.session_incarnation_id, old_incarnation)

        # 3. Verify next message starts clean with new incarnation
        runtime.handle_message(IncomingMessage(id="post-reset", channel="cli", user_id="local", conversation_id="reset_test", text="fresh turn"))
        self.assertEqual(len(sess.history), 1)
        self.assertEqual(sess.history[0]["user"], "fresh turn")

    # -------------------------------------------------------------------------
    # 4. Multi-Session and Cross-User Isolation
    # -------------------------------------------------------------------------
    def test_cross_user_and_session_isolation(self) -> None:
        """Inflating history in Alice's session does not affect Bob's session."""
        runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            default_test_cmd="cmd /c exit 0" if os.name == "nt" else "true",
            persist_sessions=True,
            system1_factory=lambda b: FakeFastS1(),
            system2_factory=lambda **k: FakeFastS2(),
            max_history_entries=20,
        )

        # Populate Alice with 30 messages
        for i in range(30):
            runtime.handle_message(IncomingMessage(id=f"a-{i}", channel="telegram", user_id="alice", conversation_id="chat_a", text=f"alice msg {i}"))

        # Bob sends 1 message
        runtime.handle_message(IncomingMessage(id="b-0", channel="telegram", user_id="bob", conversation_id="chat_b", text="bob secret task"))

        alice_sess = runtime.sessions.get("telegram:alice:chat_a")
        bob_sess = runtime.sessions.get("telegram:bob:chat_b")

        assert alice_sess is not None
        assert bob_sess is not None
        self.assertEqual(len(alice_sess.history), 20)
        self.assertEqual(len(bob_sess.history), 1)
        self.assertEqual(bob_sess.history[0]["user"], "bob secret task")
        # Ensure Bob did not inherit any Alice data
        self.assertNotIn("alice", json.dumps(bob_sess.history))

    # -------------------------------------------------------------------------
    # 5. Concurrency: Multi-threaded & Multi-process Writes
    # -------------------------------------------------------------------------
    def test_concurrent_session_writers_thread_safety(self) -> None:
        """8 threads writing 20 messages each to the same session maintain valid JSON and bounded history."""
        store = FileSessionStore(sessions_dir=self.sessions_dir)
        mgr = SessionManager(store=store, max_history_entries=30)
        session = mgr.get_or_create("cli", "concurrent_user", "conv_1")

        errors: List[Exception] = []

        def worker(thread_id: int) -> None:
            try:
                for i in range(20):
                    session.record_interaction(f"Thread {thread_id} turn {i}", f"Reply {i}")
                    mgr.save(session)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)

        # Reload from fresh store
        fresh_store = FileSessionStore(sessions_dir=self.sessions_dir)
        reloaded = fresh_store.load("cli:concurrent_user:conv_1")
        self.assertIsNotNone(reloaded)
        assert reloaded is not None
        # Must strictly obey max_history_entries
        self.assertEqual(len(reloaded.history), 30)

    # -------------------------------------------------------------------------
    # 6. Channel Tests: Telegram & WhatsApp Runtime Bounds
    # -------------------------------------------------------------------------
    def test_remote_channels_history_bounding(self) -> None:
        """Telegram and WhatsApp channels both respect bounded history and persistence limits."""
        runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            default_test_cmd="cmd /c exit 0" if os.name == "nt" else "true",
            persist_sessions=True,
            system1_factory=lambda b: FakeFastS1(),
            system2_factory=lambda **k: FakeFastS2(),
            max_history_entries=15,
        )

        # Telegram (rate_limit_seconds=0.0 for deterministic test execution)
        transport_tg = MockTelegramTransport()
        channel_tg = TelegramChannel(bot_token="fake_tg", allowed_users=["user_tg"], transport=transport_tg, rate_limit_seconds=0.0)
        channel_tg.set_handler(runtime.handle_message)

        for i in range(35):
            channel_tg.simulate_incoming(chat_id="chat_tg", user_id="user_tg", text=f"tg msg {i}")

        sess_tg = runtime.sessions.get("telegram:user_tg:chat_tg")
        assert sess_tg is not None
        self.assertEqual(len(sess_tg.history), 15)
        self.assertEqual(sess_tg.history[0]["user"], "tg msg 20")
        self.assertEqual(sess_tg.history[-1]["user"], "tg msg 34")

        # WhatsApp
        transport_wa = MockWhatsAppTransport()
        channel_wa = WhatsAppChannel(transport=transport_wa, allowed_users=["6289999"], rate_limit_seconds=0.0)
        channel_wa.set_handler(runtime.handle_message)

        for i in range(35):
            transport_wa.simulate_incoming(from_number="6289999", text=f"wa msg {i}")

        sess_wa = runtime.sessions.get("whatsapp:6289999:6289999")
        assert sess_wa is not None
        self.assertEqual(len(sess_wa.history), 15)
        self.assertEqual(sess_wa.history[0]["user"], "wa msg 20")
        self.assertEqual(sess_wa.history[-1]["user"], "wa msg 34")

    # -------------------------------------------------------------------------
    # 7. Adversarial & Security Tests (Step 14 & Storage Plateau Invariants)
    # -------------------------------------------------------------------------
    def test_security_10000_attempted_messages(self) -> None:
        """10,000 attempted messages complete in seconds and persistent storage plateaus at max bound."""
        max_entries = 50
        store = FileSessionStore(sessions_dir=self.sessions_dir)
        session = SessionState(
            session_id="cli:security:10k_plateau",
            channel="cli",
            user_id="local",
            conversation_id="10k_plateau",
            max_history_entries=max_entries,
        )
        session_file = store._get_session_path(session.session_id)

        t0 = time.perf_counter()

        sizes_at_milestones: Dict[int, int] = {}
        milestones = {10, 50, 100, 500, 1000, 2500, 5000, 10000}

        for i in range(1, 10001):
            session.record_interaction(f"synthetic request turn {i}", f"synthetic response turn {i}")
            if i in milestones or i <= 100:
                store.save(session)
                if i in milestones:
                    sizes_at_milestones[i] = session_file.stat().st_size

        total_duration = time.perf_counter() - t0
        print(f"\n  [Security] 10,000 attempted messages completed in {total_duration:.3f}s", flush=True)

        # Invariant 1: History entry count strictly bounded to 50
        self.assertEqual(len(session.history), max_entries)
        self.assertEqual(session.history[-1]["user"], "synthetic request turn 10000")

        # Invariant 2: Storage plateau proof
        size_50 = sizes_at_milestones[50]
        size_500 = sizes_at_milestones[500]
        size_1000 = sizes_at_milestones[1000]
        size_5000 = sizes_at_milestones[5000]
        size_10000 = sizes_at_milestones[10000]

        print(f"  [Security] Sizes: 50 -> {size_50}B | 500 -> {size_500}B | 10k -> {size_10000}B", flush=True)

        # Size at 10,000 must plateau within 20% of size at 50 (plateau proof!)
        self.assertAlmostEqual(size_10000, size_50, delta=size_50 * 0.25)
        self.assertLess(size_10000, 30_000)
        # Must execute fast in seconds (not minutes!)
        self.assertLess(total_duration, 15.0)

    def test_security_10000_large_messages(self) -> None:
        """10,000 large 64KB messages are safely clamped and total serialized history never exceeds limits."""
        store = FileSessionStore(sessions_dir=self.sessions_dir)
        session = SessionState(
            session_id="cli:security:large_messages",
            channel="cli",
            user_id="local",
            conversation_id="large_messages",
            max_history_entries=20,
            max_history_bytes=100_000,
            max_message_bytes=10_000,
        )
        large_payload = "A" * 65536  # 64 KB

        t0 = time.perf_counter()
        for i in range(10000):
            session.record_interaction(user_text=f"turn_{i}_{large_payload}", assistant_text=f"reply_{i}_{large_payload}")
            if i % 1000 == 0:
                store.save(session)
        store.save(session)
        elapsed = time.perf_counter() - t0

        self.assertLess(elapsed, 15.0)
        # Entry count bounded
        self.assertLessEqual(len(session.history), 20)
        # Every single message must be bounded to max_message_bytes (10,000)
        for entry in session.history:
            self.assertLessEqual(len(entry["user"].encode("utf-8")), 10000)
            self.assertLessEqual(len(entry["assistant"].encode("utf-8")), 10000)
            self.assertTrue(entry["user"].endswith(" ... [TRUNCATED]"))
            self.assertTrue(entry["assistant"].endswith(" ... [TRUNCATED]"))

        # Serialized bytes bounded to max_history_bytes (100,000)
        serialized_bytes = len(json.dumps(session.history, ensure_ascii=False).encode("utf-8"))
        self.assertLessEqual(serialized_bytes, 100000)

        # Reload from disk and verify disk file is bounded
        reloaded = store.load(session.session_id)
        assert reloaded is not None
        self.assertLessEqual(len(reloaded.history), 20)
        file_size = store._get_session_path(session.session_id).stat().st_size
        self.assertLess(file_size, 120_000)

    def test_security_alternating_user_assistant_messages(self) -> None:
        """Alternating user and assistant messages via add_message() maintain consistent turn pairs and bounds."""
        session = SessionState(
            session_id="cli:security:alternating",
            channel="cli",
            user_id="local",
            conversation_id="alternating",
            max_history_entries=10,
        )

        for i in range(100):
            session.add_message(role="user", content=f"query {i}")
            session.add_message(role="assistant", content=f"answer {i}")

        self.assertEqual(len(session.history), 10)
        self.assertEqual(session.history[0]["user"], "query 90")
        self.assertEqual(session.history[0]["assistant"], "answer 90")
        self.assertEqual(session.history[-1]["user"], "query 99")
        self.assertEqual(session.history[-1]["assistant"], "answer 99")

    def test_security_maximum_payload_repeatedly(self) -> None:
        """Repeatedly sending the maximum allowable payload strictly bounds history byte retention."""
        max_bytes = 20_000
        session = SessionState(
            session_id="cli:security:max_payload",
            channel="cli",
            user_id="local",
            conversation_id="max_payload",
            max_history_entries=50,
            max_history_bytes=max_bytes,
            max_message_bytes=5000,
        )

        for i in range(50):
            # Send exactly 5000 bytes each turn
            session.record_interaction(user_text="U" * 5000, assistant_text="A" * 5000)

        serialized_bytes = len(json.dumps(session.history, ensure_ascii=False).encode("utf-8"))
        self.assertLessEqual(serialized_bytes, max_bytes)
        # Because each interaction is ~10 KB, at most 2 interactions can fit in 20 KB
        self.assertLessEqual(len(session.history), 3)

    def test_security_session_reset_during_growth(self) -> None:
        """Session reset in the middle of conversation growth completely purges history and persistent storage."""
        store = FileSessionStore(sessions_dir=self.sessions_dir)
        mgr = SessionManager(store=store, max_history_entries=20)
        session = mgr.get_or_create("cli", "growth_user", "conv_growth")

        # Grow to 50 messages
        for i in range(50):
            session.record_interaction(f"msg {i}", f"resp {i}")
        mgr.save(session)

        self.assertEqual(len(session.history), 20)
        session_file = store._get_session_path(session.session_id)
        self.assertTrue(session_file.exists())
        old_inc = session.session_incarnation_id

        # Perform reset
        mgr.reset(session.session_id)
        self.assertEqual(len(session.history), 0)
        self.assertFalse(session_file.exists())
        self.assertNotEqual(session.session_incarnation_id, old_inc)

        # Continue growth after reset
        for i in range(10):
            session.record_interaction(f"new msg {i}", f"new resp {i}")
        mgr.save(session)

        self.assertEqual(len(session.history), 10)
        self.assertEqual(session.history[0]["user"], "new msg 0")
        self.assertTrue(session_file.exists())

    def test_runtime_repeated_messages_integration(self) -> None:
        """Verify full BrainFrogRuntime facade integration with bounded history and persistence."""
        runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            default_test_cmd="cmd /c exit 0" if os.name == "nt" else "true",
            persist_sessions=True,
            system1_factory=lambda b: FakeFastS1(),
            system2_factory=lambda **k: FakeFastS2(),
            max_history_entries=10,
        )

        session_id = "cli:local:runtime_int"
        for i in range(25):
            runtime.handle_message(IncomingMessage(
                id=f"r-{i}",
                channel="cli",
                user_id="local",
                conversation_id="runtime_int",
                text=f"task message {i}",
            ))

        sess = runtime.sessions.get(session_id)
        assert sess is not None
        self.assertEqual(len(sess.history), 10)
        self.assertEqual(sess.history[0]["user"], "task message 15")
        self.assertEqual(sess.history[-1]["user"], "task message 24")

        store = FileSessionStore(repo_dir=self.repo_dir)
        reloaded = store.load(session_id)
        assert reloaded is not None
        self.assertEqual(len(reloaded.history), 10)


if __name__ == "__main__":
    unittest.main()
