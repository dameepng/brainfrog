"""Phase 11: Persistent Runtime State & Session Recovery Test Suite.

Validates that BrainFrog runtime and session states survive process restarts:
1. SessionState serialization & deserialization (round-trip, schema versioning)
2. Safe hashed filenames and complete path traversal immunity
3. Atomic file writes and Windows-compatible replace primitives
4. Corrupted session quarantine without crashing the runtime
5. Session lifecycle: create -> save -> restore -> reset -> delete
6. Multi-session and cross-channel isolation
7. Thread-safe concurrent session persistence
8. Secret redaction on persistent storage (no credential leakage to disk)
9. Runtime A -> destroy -> Runtime B conversation recovery
10. Subprocess-based actual process boundary restart verification
11. Telegram adapter restart E2E continuity
12. Filesystem failure graceful degradation (in-memory fallback)
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
from typing import Any, Dict, List, Optional

from core.channels.telegram import MockTelegramTransport, TelegramChannel
from core.runtime.messages import IncomingMessage, OutgoingMessage
from core.runtime.runtime import BrainFrogRuntime, scrub_secrets
from core.runtime.session import (
    CURRENT_SESSION_SCHEMA_VERSION,
    FileSessionStore,
    InMemorySessionStore,
    SessionManager,
    SessionState,
)
from system1.base import Answer, SystemOneClient


class DeterministicMockSystem1(SystemOneClient):
    """Deterministic S1 for persistent session tests."""

    name: str = "mock_s1"

    def decide(self, state: Dict[str, Any], questions: Dict[str, Any]) -> Dict[str, Answer]:
        return {
            "likely_domain": Answer(choice="unrelated", confidence=0.85),
            "change_type": Answer(choice="question_only", confidence=0.90),
            "is_sensitive": Answer(noul=0.05, confidence=0.95),
            "complexity": Answer(score=0, confidence=0.90),
            "needs_tests": Answer(noul=0.05, confidence=0.95),
        }


class ContextEchoSystem2:
    """Deterministic S2 that echoes conversation context for verification."""

    provider_name: str = "context_echo_s2"

    def __init__(self) -> None:
        self.guidelines: str = ""
        self.last_task: str = ""

    def diagnose(self, task: str, focus_files: Dict[str, str], *args: Any, **kwargs: Any) -> str:
        self.last_task = task
        if "Conversation Context" in task or "Conversation History" in task:
            return f"Acknowledged previous context in task:\n{task}"
        return f"Echo response for: {task}"

    def plan_and_prd(self, task: str, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        self.last_task = task
        return {
            "title": "Persistent Plan",
            "markdown_doc": f"# Plan for: {task}",
            "steps": [],
        }


class TestPhase11PersistentSessions(unittest.TestCase):
    """Phase 11 comprehensive persistent session test suite."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="brainfrog_phase11_")
        self.repo_dir = Path(self.test_dir)
        (self.repo_dir / "src").mkdir(parents=True, exist_ok=True)
        (self.repo_dir / "src" / "main.py").write_text("print('hello')\n", encoding="utf-8")
        self.sessions_dir = self.repo_dir / ".brainfrog" / "sessions"

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # 1. Serialization Round-Trip & Schema Version
    # -------------------------------------------------------------------------
    def test_session_state_serialization_roundtrip(self) -> None:
        """Verify to_dict and from_dict preserve all conversation and runtime fields."""
        original = SessionState(
            session_id="telegram:user_1:chat_1",
            channel="telegram",
            user_id="user_1",
            conversation_id="chat_1",
            active_mode="plan",
            active_skill="python-dev",
            active_model="gemini-3.8-flash",
            active_provider="antigravity",
            plan_context="Previous plan data",
            metadata={"priority": "high", "flags": [1, 2]},
        )
        original.record_interaction("Hello agent", "Hello user, how can I help?")

        data = original.to_dict()
        self.assertEqual(data["schema_version"], CURRENT_SESSION_SCHEMA_VERSION)
        self.assertEqual(data["session_id"], "telegram:user_1:chat_1")
        self.assertEqual(len(data["history"]), 1)

        # JSON serialize & deserialize to ensure strictly JSON-compatible
        json_str = json.dumps(data)
        loaded_data = json.loads(json_str)

        restored = SessionState.from_dict(loaded_data)
        self.assertEqual(restored.session_id, original.session_id)
        self.assertEqual(restored.channel, original.channel)
        self.assertEqual(restored.user_id, original.user_id)
        self.assertEqual(restored.conversation_id, original.conversation_id)
        self.assertEqual(restored.active_mode, "plan")
        self.assertEqual(restored.active_skill, "python-dev")
        self.assertEqual(restored.active_model, "gemini-3.8-flash")
        self.assertEqual(restored.active_provider, "antigravity")
        self.assertEqual(restored.plan_context, "Previous plan data")
        self.assertEqual(restored.metadata, {"priority": "high", "flags": [1, 2]})
        self.assertEqual(len(restored.history), 1)
        self.assertEqual(restored.history[0]["user"], "Hello agent")
        self.assertEqual(restored.history[0]["assistant"], "Hello user, how can I help?")

    def test_unsupported_schema_version_rejected(self) -> None:
        """Loading a session with an unsupported higher schema version raises ValueError."""
        data = {
            "schema_version": CURRENT_SESSION_SCHEMA_VERSION + 1,
            "session_id": "test:session:1",
        }
        with self.assertRaises(ValueError) as ctx:
            SessionState.from_dict(data)
        self.assertIn("Unsupported session schema version", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 2. File Naming & Path Traversal Immunity
    # -------------------------------------------------------------------------
    def test_safe_hashed_filename_and_path_traversal_immunity(self) -> None:
        """Verify filenames are deterministic SHA-256 hashes and reject path traversal attacks."""
        store = FileSessionStore(sessions_dir=self.sessions_dir)
        session_id = "telegram:user_42:chat_99"

        path = store._get_session_path(session_id)
        self.assertEqual(path.suffix, ".json")
        self.assertEqual(len(path.stem), 32)  # 32-character hex hash
        self.assertTrue(path.resolve().is_relative_to(self.sessions_dir.resolve()))

        # Adversarial path traversal attempts in session_id
        adversarial_keys = [
            "../../etc/passwd",
            "../../../evil_path",
            "C:\\Windows\\System32\\cmd.exe",
            "telegram:user:../../escaped",
            "/var/log/session",
        ]
        for bad_key in adversarial_keys:
            with self.subTest(bad_key=bad_key):
                safe_path = store._get_session_path(bad_key)
                # Filename is strictly the hash of the string, staying inside sessions_dir
                self.assertEqual(len(safe_path.stem), 32)
                self.assertTrue(safe_path.resolve().is_relative_to(self.sessions_dir.resolve()))
                self.assertNotEqual(safe_path.resolve(), Path("/etc/passwd").resolve())

    # -------------------------------------------------------------------------
    # 3. Atomic Writes & State Integrity
    # -------------------------------------------------------------------------
    def test_atomic_write_creates_valid_json(self) -> None:
        """Atomic write writes through a temporary file and atomically replaces target."""
        store = FileSessionStore(sessions_dir=self.sessions_dir)
        session = SessionState(
            session_id="telegram:1001:chat_10",
            channel="telegram",
            user_id="1001",
            conversation_id="chat_10",
        )
        session.record_interaction("User prompt", "Assistant answer")

        ok = store.save(session)
        self.assertTrue(ok)

        # Inspect persisted file on disk directly
        path = store._get_session_path(session.session_id)
        self.assertTrue(path.exists())
        self.assertTrue(path.is_file())

        content = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(content["schema_version"], 1)
        self.assertEqual(content["session_id"], "telegram:1001:chat_10")
        self.assertEqual(content["history"][0]["user"], "User prompt")

        # Verify no temporary files remain
        temp_files = list(self.sessions_dir.glob(".tmp_*"))
        self.assertEqual(len(temp_files), 0)

    # -------------------------------------------------------------------------
    # 4. Corruption Handling & Quarantine
    # -------------------------------------------------------------------------
    def test_corrupted_json_quarantined_gracefully(self) -> None:
        """Corrupted session files (invalid JSON, truncated) are quarantined without crashing."""
        store = FileSessionStore(sessions_dir=self.sessions_dir)
        session_id = "telegram:corrupt:1"
        path = store._get_session_path(session_id)

        # Write invalid truncated JSON
        path.write_text('{"schema_version": 1, "session_id": "telegram:corrupt:1", "his', encoding="utf-8")

        loaded = store.load(session_id)
        self.assertIsNone(loaded)

        # The corrupted file should have been moved to corrupt/
        corrupt_files = list(store.corrupt_dir.glob("*.corrupt"))
        self.assertEqual(len(corrupt_files), 1)
        self.assertFalse(path.exists())

    def test_empty_file_quarantined_gracefully(self) -> None:
        """Empty session files are quarantined and return None."""
        store = FileSessionStore(sessions_dir=self.sessions_dir)
        session_id = "telegram:empty:1"
        path = store._get_session_path(session_id)
        path.write_text("", encoding="utf-8")

        loaded = store.load(session_id)
        self.assertIsNone(loaded)
        self.assertTrue(len(list(store.corrupt_dir.glob("*.corrupt"))) >= 1)

    # -------------------------------------------------------------------------
    # 5. Session Reset & Deletion
    # -------------------------------------------------------------------------
    def test_session_reset_removes_persisted_file(self) -> None:
        """Calling reset removes the persisted JSON file from disk."""
        store = FileSessionStore(sessions_dir=self.sessions_dir)
        mgr = SessionManager(store=store)

        session = mgr.get_or_create("telegram", "1001", "chat_reset")
        session.record_interaction("Prompt", "Response")
        mgr.save(session)

        path = store._get_session_path(session.session_id)
        self.assertTrue(path.exists())

        # Reset session
        mgr.reset(session.session_id)
        self.assertFalse(path.exists())
        self.assertEqual(len(session.history), 0)

    # -------------------------------------------------------------------------
    # 6. Multi-Session Isolation
    # -------------------------------------------------------------------------
    def test_multi_session_disk_isolation(self) -> None:
        """Distinct sessions produce separate files without history contamination."""
        store = FileSessionStore(sessions_dir=self.sessions_dir)
        mgr = SessionManager(store=store)

        s_a1 = mgr.get_or_create("telegram", "user_a", "chat_1")
        s_a2 = mgr.get_or_create("telegram", "user_a", "chat_2")
        s_b1 = mgr.get_or_create("telegram", "user_b", "chat_1")
        s_cli = mgr.get_or_create("cli", "local", "default")

        s_a1.record_interaction("A in Chat 1", "Response A1")
        s_a2.record_interaction("A in Chat 2", "Response A2")
        s_b1.record_interaction("B in Chat 1", "Response B1")
        s_cli.record_interaction("CLI prompt", "CLI response")

        mgr.save(s_a1)
        mgr.save(s_a2)
        mgr.save(s_b1)
        mgr.save(s_cli)

        # Re-load in a completely new manager instance
        fresh_mgr = SessionManager(store=FileSessionStore(sessions_dir=self.sessions_dir))
        loaded_a1 = fresh_mgr.get("telegram:user_a:chat_1")
        loaded_a2 = fresh_mgr.get("telegram:user_a:chat_2")
        loaded_b1 = fresh_mgr.get("telegram:user_b:chat_1")
        loaded_cli = fresh_mgr.get("cli:local:default")

        assert loaded_a1 is not None
        assert loaded_a2 is not None
        assert loaded_b1 is not None
        assert loaded_cli is not None

        self.assertEqual(loaded_a1.history[0]["user"], "A in Chat 1")
        self.assertEqual(loaded_a2.history[0]["user"], "A in Chat 2")
        self.assertEqual(loaded_b1.history[0]["user"], "B in Chat 1")
        self.assertEqual(loaded_cli.history[0]["user"], "CLI prompt")

    # -------------------------------------------------------------------------
    # 7. Thread-Safe Concurrency
    # -------------------------------------------------------------------------
    def test_concurrent_session_writes(self) -> None:
        """Concurrent interactions across threads maintain state integrity without corruption."""
        store = FileSessionStore(sessions_dir=self.sessions_dir)
        mgr = SessionManager(store=store)

        session = mgr.get_or_create("telegram", "concurrent_user", "chat_1")
        errors: List[Exception] = []

        def worker(thread_idx: int) -> None:
            try:
                for i in range(10):
                    session.record_interaction(f"Msg from thread {thread_idx} turn {i}", "Reply")
                    mgr.save(session)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)

        # File on disk must be completely valid JSON and readable
        fresh_store = FileSessionStore(sessions_dir=self.sessions_dir)
        reloaded = fresh_store.load("telegram:concurrent_user:chat_1")
        assert reloaded is not None
        self.assertEqual(len(reloaded.history), 40)

    # -------------------------------------------------------------------------
    # 8. Persistence Security (Secret Redaction on Disk)
    # -------------------------------------------------------------------------
    def test_secrets_redacted_before_disk_write(self) -> None:
        """Secrets present in user prompts or metadata must be sanitized before saving to disk."""
        store = FileSessionStore(sessions_dir=self.sessions_dir)
        session = SessionState(
            session_id="telegram:sec_test:1",
            channel="telegram",
            user_id="sec_test",
            conversation_id="1",
        )
        raw_secret_user = "My API key is sk-1234567890abcdefghijklmnopqr and token is 123456789:ABCDefgh-ijklmnopqrstuvwxyz123456"
        raw_secret_asst = "Header: Bearer supersecrettoken12345678"
        session.record_interaction(raw_secret_user, raw_secret_asst)

        store.save(session)

        # Inspect disk file directly
        path = store._get_session_path(session.session_id)
        raw_disk_text = path.read_text(encoding="utf-8")

        self.assertNotIn("sk-1234567890", raw_disk_text)
        self.assertIn("[REDACTED_API_KEY]", raw_disk_text)
        self.assertNotIn("123456789:ABCD", raw_disk_text)
        self.assertIn("[REDACTED_BOT_TOKEN]", raw_disk_text)
        self.assertNotIn("supersecrettoken12345678", raw_disk_text)
        self.assertIn("[REDACTED_TOKEN]", raw_disk_text)

    # -------------------------------------------------------------------------
    # 9. In-Process Runtime Restart Recovery (Runtime A -> Destroy -> Runtime B)
    # -------------------------------------------------------------------------
    def test_runtime_instance_restart_recovery(self) -> None:
        """Destroying Runtime A and instantiating Runtime B restores conversation continuity."""
        mock_s1 = DeterministicMockSystem1()
        echo_s2_a = ContextEchoSystem2()

        # Step 1: Runtime A receives Turn 1
        runtime_a = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            system1_factory=lambda b: mock_s1,
            system2_factory=lambda **k: echo_s2_a,
        )
        msg_1 = IncomingMessage(
            text="My project uses FastAPI backend.",
            channel="telegram",
            user_id="user_alpha",
            conversation_id="chat_restart",
        )
        out_1 = runtime_a.handle_message(msg_1)
        self.assertTrue(out_1.success)

        # Step 2: Completely destroy Runtime A and its SessionManager
        del runtime_a

        # Step 3: Instantiate fresh Runtime B from the same repo_dir
        echo_s2_b = ContextEchoSystem2()
        runtime_b = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            system1_factory=lambda b: mock_s1,
            system2_factory=lambda **k: echo_s2_b,
        )

        msg_2 = IncomingMessage(
            text="What framework did I mention earlier?",
            channel="telegram",
            user_id="user_alpha",
            conversation_id="chat_restart",
        )
        out_2 = runtime_b.handle_message(msg_2)
        self.assertTrue(out_2.success)

        # Verify Runtime B restored Turn 1 and included it in effective task
        self.assertIn("FastAPI", echo_s2_b.last_task)
        self.assertIn("Previous Conversation Context", echo_s2_b.last_task)

        # Verify SessionState history now has 2 turns
        session_b = runtime_b.sessions.get("telegram:user_alpha:chat_restart")
        assert session_b is not None
        self.assertEqual(len(session_b.history), 2)

    # -------------------------------------------------------------------------
    # 10. Subprocess-Based Process Boundary Recovery
    # -------------------------------------------------------------------------
    def test_subprocess_process_restart_recovery(self) -> None:
        """Verify session persistence across separate operating system processes."""
        python_bin = sys.executable
        project_root = Path(__file__).resolve().parent.parent
        env = os.environ.copy()
        current_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{project_root}{os.pathsep}{current_pp}" if current_pp else str(project_root)

        # Process 1: Start, handle message, persist, and terminate
        script_1 = f"""
import sys
from pathlib import Path
from core.runtime.runtime import BrainFrogRuntime
from core.runtime.messages import IncomingMessage
from tests.test_persistent_sessions import DeterministicMockSystem1, ContextEchoSystem2

repo_dir = Path(r"{self.repo_dir}")
s1 = DeterministicMockSystem1()
s2 = ContextEchoSystem2()
runtime = BrainFrogRuntime(repo_dir=repo_dir, system1_factory=lambda b: s1, system2_factory=lambda **k: s2)
msg = IncomingMessage(
    text="The project codename is ProjectHydra.",
    channel="telegram",
    user_id="proc_user",
    conversation_id="chat_p",
)
out = runtime.handle_message(msg)
assert out.success, "Process 1 failed"
sys.exit(0)
"""
        proc1 = subprocess.run(
            [python_bin, "-c", script_1],
            cwd=str(self.repo_dir),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc1.returncode, 0, f"Process 1 stdout/stderr: {proc1.stdout} / {proc1.stderr}")

        # Process 2: Completely new process, load persisted session, verify context
        script_2 = f"""
import sys
from pathlib import Path
from core.runtime.runtime import BrainFrogRuntime
from core.runtime.messages import IncomingMessage
from tests.test_persistent_sessions import DeterministicMockSystem1, ContextEchoSystem2

repo_dir = Path(r"{self.repo_dir}")
s1 = DeterministicMockSystem1()
s2 = ContextEchoSystem2()
runtime = BrainFrogRuntime(repo_dir=repo_dir, system1_factory=lambda b: s1, system2_factory=lambda **k: s2)
msg = IncomingMessage(
    text="What is the project codename?",
    channel="telegram",
    user_id="proc_user",
    conversation_id="chat_p",
)
out = runtime.handle_message(msg)
assert out.success, "Process 2 failed"
session = runtime.sessions.get("telegram:proc_user:chat_p")
assert session is not None, "Session not found"
assert len(session.history) == 2, f"Expected 2 turns, got {{len(session.history)}}"
assert "ProjectHydra" in session.history[0]["user"], "Turn 1 missing"
sys.exit(0)
"""
        proc2 = subprocess.run(
            [python_bin, "-c", script_2],
            cwd=str(self.repo_dir),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc2.returncode, 0, f"Process 2 stdout/stderr: {proc2.stdout} / {proc2.stderr}")

    # -------------------------------------------------------------------------
    # 11. Telegram Adapter Restart E2E
    # -------------------------------------------------------------------------
    def test_telegram_channel_restart_e2e(self) -> None:
        """Simulate Telegram user chatting, restarting adapter and runtime, continuing conversation."""
        transport_a = MockTelegramTransport()
        mock_s1 = DeterministicMockSystem1()
        s2_a = ContextEchoSystem2()

        runtime_a = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            system1_factory=lambda b: mock_s1,
            system2_factory=lambda **k: s2_a,
        )
        channel_a = TelegramChannel(
            bot_token="test_token",
            allowed_users=["1001"],
            rate_limit_seconds=0.0,
            transport=transport_a,
        )
        channel_a.set_handler(runtime_a.handle_message)

        # Message 1
        out_1 = channel_a.simulate_incoming(chat_id="chat_tg", user_id="1001", text="My nickname is BrainFrog.")
        assert out_1 is not None
        self.assertTrue(out_1.success)

        # Destroy adapter & runtime A
        channel_a.stop()
        del channel_a
        del runtime_a

        # Re-instantiate adapter & runtime B
        transport_b = MockTelegramTransport()
        s2_b = ContextEchoSystem2()
        runtime_b = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            system1_factory=lambda b: mock_s1,
            system2_factory=lambda **k: s2_b,
        )
        channel_b = TelegramChannel(
            bot_token="test_token",
            allowed_users=["1001"],
            rate_limit_seconds=0.0,
            transport=transport_b,
        )
        channel_b.set_handler(runtime_b.handle_message)

        # Message 2
        out_2 = channel_b.simulate_incoming(chat_id="chat_tg", user_id="1001", text="What nickname did I give you?")
        assert out_2 is not None
        self.assertTrue(out_2.success)

        # Assert context was restored
        self.assertIn("BrainFrog", s2_b.last_task)
        self.assertIn("Previous Conversation Context", s2_b.last_task)

    # -------------------------------------------------------------------------
    # 12. Filesystem Failure Graceful Degradation
    # -------------------------------------------------------------------------
    def test_filesystem_failure_graceful_degradation(self) -> None:
        """When disk write fails, runtime logs warning and continues in-memory without crashing."""
        store = FileSessionStore(sessions_dir=self.sessions_dir)

        # Make store.save fail artificially
        store.save = lambda s: False  # Simulate disk full / permission denied

        mgr = SessionManager(store=store)
        runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            sessions=mgr,
            system1_factory=lambda b: DeterministicMockSystem1(),
            system2_factory=lambda **k: ContextEchoSystem2(),
        )

        msg = IncomingMessage(
            text="Hello while disk is unwritable",
            channel="telegram",
            user_id="user_disk_fail",
            conversation_id="chat_disk_fail",
        )
        out = runtime.handle_message(msg)

        # Must not crash! Task succeeds in-memory
        self.assertTrue(out.success)
        self.assertEqual(out.status, "completed")
        session = mgr.get("telegram:user_disk_fail:chat_disk_fail")
        assert session is not None
        self.assertEqual(len(session.history), 1)


if __name__ == "__main__":
    unittest.main()
