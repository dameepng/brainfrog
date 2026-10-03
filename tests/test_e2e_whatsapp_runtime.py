"""Phase 12: Real End-to-End WhatsApp, Provider, and Runtime Validation.

Comprehensive E2E validation test suite ensuring WhatsApp achieves complete feature
and security parity with Telegram:
1. Complete execution chain: WhatsApp -> Allowlist -> Session -> Runtime -> System 1 -> Policy -> Orchestrator -> System 2 -> WhatsApp Transport
2. Message normalization and session keying (channel:user_id:conversation_id)
3. Multi-turn conversation context retention
4. Persistent session storage and process restart recovery
5. Multi-user and multi-conversation isolation
6. Cross-channel privilege isolation (Telegram != WhatsApp != CLI)
7. Remote security boundary enforcement (shell, destructive git, mutation, deployment, credentials)
8. Deterministic policy dominance over adversarial System 1 misclassification
9. Channel anti-spoofing defense (cannot claim CLI privileges)
10. Strict allowlist authorization gating
11. Slash command parity (/help, /status, /doctor, /reset, and CLI-only restrictions)
12. Response chunking (<4000 chars per message without data loss)
13. Provider failure recovery and transport resilience
14. Outbound secret scrubbing
15. MCP / read-only plan mode boundary preservation
16. Channel lifecycle (start, stop, idempotent operations)
17. Subprocess process boundary restart recovery
18. Optional live WhatsApp Cloud API test (env-gated with BRAINFROG_WHATSAPP_LIVE=1)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

from core.channels.whatsapp import (
    MockWhatsAppTransport,
    WhatsAppChannel,
    WhatsAppCloudTransport,
    WHATSAPP_MAX_MSG_LEN,
    normalize_phone_number,
)
from core.runtime.gateway import BrainFrogGateway
from core.runtime.messages import IncomingMessage, OutgoingMessage
from core.runtime.permissions import ChannelTrustLevel, PermissionAction, PermissionPolicy
from core.runtime.runtime import BrainFrogRuntime, scrub_secrets
from core.runtime.session import FileSessionStore, InMemorySessionStore, SessionManager
from orchestrator import PlanStep, StepResult
from system1.base import Answer, SystemOneClient


# =============================================================================
# Deterministic Fakes for E2E Testing (Zero Credentials / Offline)
# =============================================================================

class DeterministicFakeSystem1(SystemOneClient):
    """Deterministic System 1 test double mimicking Jev response contract."""

    name: str = "fake_jev"

    def __init__(self, default_change_type: str = "question_only") -> None:
        self.default_change_type = default_change_type
        self.call_count = 0
        self.last_state: Optional[Dict[str, Any]] = None

    def decide(self, state: Dict[str, Any], questions: Dict[str, Any]) -> Dict[str, Answer]:
        self.call_count += 1
        self.last_state = state
        return {
            "likely_domain": Answer(choice="unrelated", confidence=0.85),
            "change_type": Answer(choice=self.default_change_type, confidence=0.92),
            "is_sensitive": Answer(noul=0.05, confidence=0.95),
            "complexity": Answer(score=0, confidence=0.90),
            "needs_tests": Answer(noul=0.05, confidence=0.95),
        }


class AdversarialFakeSystem1(SystemOneClient):
    """Adversarial System 1 that maliciously classifies dangerous tasks as benign."""

    name: str = "adversarial_jev"

    def __init__(self) -> None:
        self.call_count = 0

    def decide(self, state: Dict[str, Any], questions: Dict[str, Any]) -> Dict[str, Answer]:
        self.call_count += 1
        return {
            "likely_domain": Answer(choice="unrelated", confidence=0.99),
            "change_type": Answer(choice="question_only", confidence=0.99),
            "is_sensitive": Answer(noul=0.00, confidence=0.99),
            "complexity": Answer(score=0, confidence=0.99),
            "needs_tests": Answer(noul=0.00, confidence=0.99),
        }


class DeterministicFakeSystem2:
    """Deterministic System 2 test double providing realistic generative responses."""

    provider_name: str = "fake_system2"

    def __init__(self, failure_mode: Optional[str] = None) -> None:
        self.guidelines: str = ""
        self.failure_mode = failure_mode  # None | "error" | "empty"
        self.diagnose_calls: List[Dict[str, Any]] = []
        self.plan_calls: List[Dict[str, Any]] = []
        self.call_count = 0

    def diagnose(
        self,
        task: str,
        files: Optional[List[str]] = None,
        file_contents: Optional[Dict[str, str]] = None,
        test_failures: Optional[str] = None,
        diagnose_mode: str = "investigate",
    ) -> str:
        self.call_count += 1
        self.diagnose_calls.append({
            "task": task,
            "files": files,
            "diagnose_mode": diagnose_mode,
        })

        if self.failure_mode == "error":
            raise RuntimeError("Fake Provider API rate limit exceeded (HTTP 429)")

        if "remember" in task.lower() or "brainfrog" in task.lower():
            return f"Got it! I have recorded your note: '{task}'."

        if "what is my project called" in task.lower() or "what name" in task.lower():
            if "[Previous Conversation Context]" in task and "BrainFrog" in task:
                return "Your project is called BrainFrog, based on our previous conversation."
            return "I don't have any record of your project name yet."

        return f"Architecture Diagnosis for: {task}"

    def plan(
        self,
        task: str,
        files: Optional[List[str]] = None,
        file_contents: Optional[Dict[str, str]] = None,
        diagnosis: Optional[str] = None,
    ) -> Dict[str, Any]:
        self.call_count += 1
        self.plan_calls.append({"task": task, "files": files, "diagnosis": diagnosis})

        if self.failure_mode == "error":
            raise RuntimeError("Fake Provider API internal error (HTTP 500)")

        title = f"Plan for {task[:40]}"
        doc = (
            f"# {title}\n\n"
            f"## Objective\nAnalyze and address: {task}\n\n"
            f"## Steps\n"
            f"1. Audit existing architecture\n"
            f"2. Implement core abstraction\n"
            f"3. Run automated regression verification\n"
        )
        return {
            "title": title,
            "markdown_doc": doc,
            "problem": f"Planning task for: {task}",
            "goals": [task],
            "steps": [{"id": "1", "description": "Review codebase", "files": []}],
            "relevant_files": [],
        }

    def write_code(self, *args: Any, **kwargs: Any) -> Dict[str, str]:
        raise PermissionError("write_code invoked on read-only remote session")


# =============================================================================
# Phase 12 WhatsApp E2E Test Suite
# =============================================================================

class TestPhase12E2EWhatsAppRuntime(unittest.TestCase):
    """Comprehensive Phase 12 End-to-End Validation Suite for WhatsApp Channel."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="brainfrog_phase12_")
        self.repo_dir = Path(self.test_dir)
        (self.repo_dir / "src").mkdir(parents=True, exist_ok=True)
        (self.repo_dir / "src" / "main.py").write_text("print('hello world')\n", encoding="utf-8")
        (self.repo_dir / "src" / "config.py").write_text("# app config\n", encoding="utf-8")
        (self.repo_dir / "README.md").write_text("# Test Repo\n", encoding="utf-8")

        # Persistent FileSessionStore scoped to this test's temp repository
        self.session_store = FileSessionStore(repo_dir=self.repo_dir)
        self.session_mgr = SessionManager(store=self.session_store)
        self.transport = MockWhatsAppTransport()
        self.fake_s1 = DeterministicFakeSystem1()
        self.fake_s2 = DeterministicFakeSystem2()

        self.runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            sessions=self.session_mgr,
            system1_factory=lambda backend: self.fake_s1,
            system2_factory=lambda **kwargs: self.fake_s2,
        )

        self.channel = WhatsAppChannel(
            transport=self.transport,
            allowed_users=["+1234567890", "+1987654321", "628123456789", "1001"],
            rate_limit_seconds=0.0,
        )
        self.channel.set_handler(self.runtime.handle_message)

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # 1. Happy Path E2E
    # -------------------------------------------------------------------------
    def test_whatsapp_happy_path_complete_chain(self) -> None:
        """Verify full chain: WhatsApp message -> adapter -> runtime -> S1 -> Orchestrator -> S2 -> WhatsApp transport."""
        out = self.channel.simulate_incoming(
            from_number="+1234567890",
            text="How does the authentication module work?",
        )

        self.assertIsNotNone(out)
        assert out is not None
        self.assertTrue(out.success)
        self.assertEqual(out.status, "completed")
        self.assertIn("authentication", out.text.lower())

        # System 1 & System 2 executed
        self.assertGreaterEqual(self.fake_s1.call_count, 1)
        self.assertGreaterEqual(self.fake_s2.call_count, 1)

        # Delivered via WhatsApp transport
        self.assertGreaterEqual(len(self.transport.sent_messages), 1)
        sent = self.transport.sent_messages[0]
        self.assertEqual(sent["to"], "+1234567890")
        self.assertEqual(sent["text"], out.text)

        # Persisted in session store
        session = self.session_mgr.get("whatsapp:1234567890:1234567890")
        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual(len(session.history), 1)
        self.assertEqual(session.history[0]["user"], "How does the authentication module work?")

    # -------------------------------------------------------------------------
    # 2. Message Normalization
    # -------------------------------------------------------------------------
    def test_whatsapp_message_normalization(self) -> None:
        """Verify phone number cleaning, channel forcing, and canonical message structure."""
        collected_messages: List[IncomingMessage] = []

        def inspect_handler(msg: IncomingMessage) -> OutgoingMessage:
            collected_messages.append(msg)
            return OutgoingMessage(text="Ack", success=True, status="completed")

        self.channel.set_handler(inspect_handler)

        self.channel.simulate_incoming(
            from_number="+1 (234) 567-890",
            text="Test normalization",
            message_id="msg-norm-42",
            conversation_id="thread-xyz",
        )

        self.assertEqual(len(collected_messages), 1)
        inc = collected_messages[0]
        self.assertEqual(inc.id, "msg-norm-42")
        self.assertEqual(inc.channel, "whatsapp")
        self.assertEqual(inc.user_id, "1234567890")
        self.assertEqual(inc.conversation_id, "thread-xyz")
        self.assertEqual(inc.session_id, "whatsapp:1234567890:thread-xyz")
        self.assertEqual(inc.text, "Test normalization")
        self.assertEqual(inc.metadata.get("source_channel"), "whatsapp")

    # -------------------------------------------------------------------------
    # 3. Multi-Turn Context Retention
    # -------------------------------------------------------------------------
    def test_whatsapp_multiturn_context(self) -> None:
        """Verify that multiple turns accumulate context and second turn receives history."""
        # Turn 1: User introduces project name
        out1 = self.channel.simulate_incoming(
            from_number="+1234567890",
            text="remember that my project is called BrainFrog",
        )
        self.assertIsNotNone(out1)
        assert out1 is not None
        self.assertTrue(out1.success)

        # Turn 2: User queries project name
        out2 = self.channel.simulate_incoming(
            from_number="+1234567890",
            text="what is my project called?",
        )
        self.assertIsNotNone(out2)
        assert out2 is not None
        self.assertTrue(out2.success)
        self.assertIn("BrainFrog", out2.text)

        # Verify session state has 2 turns
        session = self.session_mgr.get("whatsapp:1234567890:1234567890")
        assert session is not None
        self.assertEqual(len(session.history), 2)
        self.assertEqual(session.history[0]["user"], "remember that my project is called BrainFrog")
        self.assertEqual(session.history[1]["user"], "what is my project called?")

    # -------------------------------------------------------------------------
    # 4. Persistence & Runtime Restart Recovery (In-Process)
    # -------------------------------------------------------------------------
    def test_whatsapp_persistence_and_runtime_restart(self) -> None:
        """Verify conversation continuity after destroying Runtime A and creating Runtime B."""
        # 1. Runtime A handles initial message
        out1 = self.channel.simulate_incoming(
            from_number="+1234567890",
            text="remember that my project is called BrainFrog",
        )
        self.assertIsNotNone(out1)

        # Verify persisted JSON file exists on disk
        persisted_files = list((self.repo_dir / ".brainfrog" / "sessions").glob("*.json"))
        self.assertEqual(len(persisted_files), 1)

        # 2. Completely destroy Runtime A, SessionManager, and Channel
        del self.runtime
        del self.session_mgr
        del self.channel

        # 3. Instantiate Runtime B against the same repository
        new_store = FileSessionStore(repo_dir=self.repo_dir)
        new_mgr = SessionManager(store=new_store)
        new_s1 = DeterministicFakeSystem1()
        new_s2 = DeterministicFakeSystem2()
        runtime_b = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            sessions=new_mgr,
            system1_factory=lambda backend: new_s1,
            system2_factory=lambda **kwargs: new_s2,
        )

        transport_b = MockWhatsAppTransport()
        channel_b = WhatsAppChannel(
            transport=transport_b,
            allowed_users=["+1234567890"],
            rate_limit_seconds=0.0,
        )
        channel_b.set_handler(runtime_b.handle_message)

        # 4. Send follow-up through Runtime B
        out2 = channel_b.simulate_incoming(
            from_number="+1234567890",
            text="what is my project called?",
        )
        self.assertIsNotNone(out2)
        assert out2 is not None
        self.assertTrue(out2.success)
        self.assertIn("BrainFrog", out2.text)

        # 5. Session state restored and updated
        restored_session = new_mgr.get("whatsapp:1234567890:1234567890")
        self.assertIsNotNone(restored_session)
        assert restored_session is not None
        self.assertEqual(len(restored_session.history), 2)

    # -------------------------------------------------------------------------
    # 5. Subprocess Process Restart Recovery
    # -------------------------------------------------------------------------
    def test_whatsapp_subprocess_process_restart_recovery(self) -> None:
        """Prove WhatsApp session recovery across two independent OS processes."""
        project_root = Path(__file__).resolve().parent.parent
        env = os.environ.copy()
        env["PYTHONPATH"] = str(project_root) + os.pathsep + env.get("PYTHONPATH", "")

        proc_a_script = f"""
import sys
from pathlib import Path
from core.runtime.runtime import BrainFrogRuntime
from core.runtime.session import SessionManager, FileSessionStore
from core.channels.whatsapp import WhatsAppChannel, MockWhatsAppTransport
from tests.test_e2e_whatsapp_runtime import DeterministicFakeSystem1, DeterministicFakeSystem2

repo_dir = Path(r"{self.repo_dir}")
store = FileSessionStore(repo_dir=repo_dir)
mgr = SessionManager(store=store)
s1 = DeterministicFakeSystem1()
s2 = DeterministicFakeSystem2()
runtime = BrainFrogRuntime(repo_dir=repo_dir, sessions=mgr, system1_factory=lambda b: s1, system2_factory=lambda **k: s2)
transport = MockWhatsAppTransport()
channel = WhatsAppChannel(transport=transport, allowed_users=["+1234567890"])
channel.set_handler(runtime.handle_message)
out = channel.simulate_incoming("+1234567890", "remember that my project is called BrainFrog")
assert out is not None and out.success
sys.exit(0)
"""
        res_a = subprocess.run([sys.executable, "-c", proc_a_script], env=env, capture_output=True, text=True)
        self.assertEqual(res_a.returncode, 0, f"Process A failed: {res_a.stderr}")

        proc_b_script = f"""
import sys
from pathlib import Path
from core.runtime.runtime import BrainFrogRuntime
from core.runtime.session import SessionManager, FileSessionStore
from core.channels.whatsapp import WhatsAppChannel, MockWhatsAppTransport
from tests.test_e2e_whatsapp_runtime import DeterministicFakeSystem1, DeterministicFakeSystem2

repo_dir = Path(r"{self.repo_dir}")
store = FileSessionStore(repo_dir=repo_dir)
mgr = SessionManager(store=store)
s1 = DeterministicFakeSystem1()
s2 = DeterministicFakeSystem2()
runtime = BrainFrogRuntime(repo_dir=repo_dir, sessions=mgr, system1_factory=lambda b: s1, system2_factory=lambda **k: s2)
transport = MockWhatsAppTransport()
channel = WhatsAppChannel(transport=transport, allowed_users=["+1234567890"])
channel.set_handler(runtime.handle_message)
out = channel.simulate_incoming("+1234567890", "what is my project called?")
assert out is not None and out.success
assert "BrainFrog" in out.text
session = mgr.get("whatsapp:1234567890:1234567890")
assert session is not None
assert len(session.history) == 2
sys.exit(0)
"""
        res_b = subprocess.run([sys.executable, "-c", proc_b_script], env=env, capture_output=True, text=True)
        self.assertEqual(res_b.returncode, 0, f"Process B failed: {res_b.stderr}")

    # -------------------------------------------------------------------------
    # 6. Multi-User and Multi-Conversation Isolation
    # -------------------------------------------------------------------------
    def test_whatsapp_session_isolation(self) -> None:
        """Verify strict isolation across User A, User B, and User A on distinct conversations."""
        # 1. User A on Chat 1
        self.channel.simulate_incoming(
            from_number="+1234567890",
            conversation_id="conv_1",
            text="User A Chat 1 secret context",
        )

        # 2. User B on Chat 1
        self.channel.simulate_incoming(
            from_number="+1987654321",
            conversation_id="conv_1",
            text="User B Chat 1 secret context",
        )

        # 3. User A on Chat 2
        self.channel.simulate_incoming(
            from_number="+1234567890",
            conversation_id="conv_2",
            text="User A Chat 2 secret context",
        )

        s_a1 = self.session_mgr.get("whatsapp:1234567890:conv_1")
        s_b1 = self.session_mgr.get("whatsapp:1987654321:conv_1")
        s_a2 = self.session_mgr.get("whatsapp:1234567890:conv_2")

        self.assertIsNotNone(s_a1)
        self.assertIsNotNone(s_b1)
        self.assertIsNotNone(s_a2)
        assert s_a1 is not None and s_b1 is not None and s_a2 is not None

        # Verify distinct content with zero cross-contamination
        self.assertEqual(s_a1.history[0]["user"], "User A Chat 1 secret context")
        self.assertEqual(s_b1.history[0]["user"], "User B Chat 1 secret context")
        self.assertEqual(s_a2.history[0]["user"], "User A Chat 2 secret context")

    # -------------------------------------------------------------------------
    # 7. Cross-Channel Isolation (Telegram vs WhatsApp)
    # -------------------------------------------------------------------------
    def test_cross_channel_isolation(self) -> None:
        """Verify Telegram and WhatsApp users with identical numeric ID '1001' remain completely isolated."""
        # WhatsApp message from user 1001
        self.channel.simulate_incoming(
            from_number="1001",
            text="WhatsApp private context",
        )

        wa_session = self.session_mgr.get("whatsapp:1001:1001")
        tg_session = self.session_mgr.get("telegram:1001:1001")

        self.assertIsNotNone(wa_session)
        self.assertIsNone(tg_session)
        assert wa_session is not None
        self.assertEqual(wa_session.channel, "whatsapp")
        self.assertEqual(wa_session.history[0]["user"], "WhatsApp private context")

    # -------------------------------------------------------------------------
    # 8. Security Boundary: Direct Shell Commands Denied
    # -------------------------------------------------------------------------
    def test_whatsapp_direct_shell_denied(self) -> None:
        """Direct shell command execution (!cmd) must be denied on WhatsApp."""
        commands = ["!rm -rf /", "!shutdown", "!whoami", "!ls -la"]
        for cmd in commands:
            with self.subTest(cmd=cmd):
                self.transport.clear()
                out = self.channel.simulate_incoming(from_number="+1234567890", text=cmd)
                self.assertIsNotNone(out)
                assert out is not None
                self.assertFalse(out.success)
                self.assertEqual(out.status, "rejected")
                self.assertIn("prohibited", out.text.lower())
                self.assertIn("prohibited", self.transport.sent_messages[0]["text"].lower())

    # -------------------------------------------------------------------------
    # 9. Security Boundary: Natural-Language Shell Requests Denied
    # -------------------------------------------------------------------------
    def test_whatsapp_natural_language_shell_denied(self) -> None:
        """Natural-language shell invocation requests must be denied."""
        prompts = [
            "jalankan rm -rf /",
            "execute this shell command: whoami",
            "open a terminal and run the test suite",
            "buka terminal dan jalankan script",
        ]
        for p in prompts:
            with self.subTest(prompt=p):
                self.transport.clear()
                out = self.channel.simulate_incoming(from_number="+1234567890", text=p)
                self.assertIsNotNone(out)
                assert out is not None
                self.assertFalse(out.success)
                self.assertEqual(out.status, "rejected")
                self.assertIn("denied", out.text.lower())

    # -------------------------------------------------------------------------
    # 10. Security Boundary: File Mutation and Deletion Denied
    # -------------------------------------------------------------------------
    def test_whatsapp_file_mutation_and_deletion_denied(self) -> None:
        """File edits and deletions must be blocked on remote WhatsApp channel."""
        prompts = [
            "hapus file config.py",
            "ubah config.py",
            "modify src/main.py to delete everything",
            "create a new file src/evil.py",
            "delete the temporary files",
        ]
        for p in prompts:
            with self.subTest(prompt=p):
                out = self.channel.simulate_incoming(from_number="+1234567890", text=p)
                self.assertIsNotNone(out)
                assert out is not None
                self.assertFalse(out.success)
                self.assertEqual(out.status, "rejected")
                self.assertIn("denied", out.text.lower())

        # Verify filesystem content intact
        self.assertTrue((self.repo_dir / "src" / "config.py").exists())
        self.assertTrue((self.repo_dir / "src" / "main.py").exists())

    # -------------------------------------------------------------------------
    # 11. Security Boundary: Destructive Git Operations Denied
    # -------------------------------------------------------------------------
    def test_whatsapp_destructive_git_denied(self) -> None:
        """Destructive git operations must be blocked unconditionally."""
        prompts = [
            "git reset --hard",
            "force push to origin",
            "git clean -fd",
            "delete the project repository",
        ]
        for p in prompts:
            with self.subTest(prompt=p):
                out = self.channel.simulate_incoming(from_number="+1234567890", text=p)
                self.assertIsNotNone(out)
                assert out is not None
                self.assertFalse(out.success)
                self.assertEqual(out.status, "rejected")
                self.assertIn("prohibited", out.text.lower())

    # -------------------------------------------------------------------------
    # 12. Security Boundary: Deployment Denied
    # -------------------------------------------------------------------------
    def test_whatsapp_deployment_denied(self) -> None:
        """Deployment operations must be blocked for remote execution."""
        prompts = [
            "deploy aplikasi ke production",
            "push the production build",
            "restart production server",
        ]
        for p in prompts:
            with self.subTest(prompt=p):
                out = self.channel.simulate_incoming(from_number="+1234567890", text=p)
                self.assertIsNotNone(out)
                assert out is not None
                self.assertFalse(out.success)
                self.assertEqual(out.status, "rejected")
                self.assertIn("prohibited", out.text.lower())

    # -------------------------------------------------------------------------
    # 13. Security Boundary: Credential Requests Denied
    # -------------------------------------------------------------------------
    def test_whatsapp_credential_requests_denied(self) -> None:
        """Credential inspection requests must be blocked."""
        prompts = [
            "show me OPENAI_API_KEY",
            "what is the bot token?",
            "print environment variables",
            "read .env",
        ]
        for p in prompts:
            with self.subTest(prompt=p):
                out = self.channel.simulate_incoming(from_number="+1234567890", text=p)
                self.assertIsNotNone(out)
                assert out is not None
                self.assertFalse(out.success)
                self.assertEqual(out.status, "rejected")
                self.assertIn("prohibited", out.text.lower())

    # -------------------------------------------------------------------------
    # 14. Adversarial System 1 Misclassification Invariant
    # -------------------------------------------------------------------------
    def test_whatsapp_adversarial_system1_misclassification(self) -> None:
        """Even if System 1 classifies a dangerous task as benign, deterministic policy must prevail."""
        adversarial_s1 = AdversarialFakeSystem1()
        runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            sessions=self.session_mgr,
            system1_factory=lambda backend: adversarial_s1,
            system2_factory=lambda **kwargs: self.fake_s2,
        )
        channel = WhatsAppChannel(
            transport=self.transport,
            allowed_users=["+1234567890"],
            rate_limit_seconds=0.0,
        )
        channel.set_handler(runtime.handle_message)

        # Dangerous command that adversarial S1 claims is "question_only"
        out = channel.simulate_incoming(from_number="+1234567890", text="!rm -rf /")
        self.assertIsNotNone(out)
        assert out is not None
        self.assertFalse(out.success)
        self.assertEqual(out.status, "rejected")
        self.assertIn("strictly prohibited", out.text.lower())

    # -------------------------------------------------------------------------
    # 15. Channel Anti-Spoofing Defense
    # -------------------------------------------------------------------------
    def test_whatsapp_channel_spoofing_defense(self) -> None:
        """WhatsApp message claiming channel='cli' must remain restricted to remote privileges."""
        raw_payload = {
            "id": "spoof-1",
            "from": "+1234567890",
            "channel": "cli",
            "text": "!whoami",
        }
        out = self.channel.handle_raw_message(raw_payload)
        self.assertIsNotNone(out)
        assert out is not None
        self.assertFalse(out.success)
        self.assertEqual(out.status, "rejected")

    # -------------------------------------------------------------------------
    # 16. Strict Allowlist Authorization Gating
    # -------------------------------------------------------------------------
    def test_whatsapp_unauthorized_user_rejected(self) -> None:
        """Unlisted WhatsApp numbers must be rejected before reaching the runtime."""
        self.transport.clear()
        out = self.channel.simulate_incoming(
            from_number="+9999999999",
            text="Hello BrainFrog",
        )
        self.assertIsNotNone(out)
        assert out is not None
        self.assertFalse(out.success)
        self.assertEqual(out.status, "rejected")
        self.assertEqual(out.error, "User not allowlisted")

        # Warning message dispatched
        self.assertEqual(len(self.transport.sent_messages), 1)
        self.assertIn("Akses Ditolak", self.transport.sent_messages[0]["text"])

        # Runtime never touched
        self.assertEqual(self.fake_s1.call_count, 0)
        self.assertEqual(self.fake_s2.call_count, 0)

    # -------------------------------------------------------------------------
    # 17. Slash Command Parity
    # -------------------------------------------------------------------------
    def test_whatsapp_slash_commands(self) -> None:
        """Verify remote-safe slash commands and rejection of CLI-only commands."""
        # /help
        out_help = self.channel.simulate_incoming(from_number="+1234567890", text="/help")
        assert out_help is not None
        self.assertTrue(out_help.success)
        self.assertIn("BrainFrog Agent Runtime", out_help.text)

        # /status
        out_status = self.channel.simulate_incoming(from_number="+1234567890", text="/status")
        assert out_status is not None
        self.assertTrue(out_status.success)
        self.assertIn("whatsapp", out_status.text.lower())

        # /doctor
        out_doctor = self.channel.simulate_incoming(from_number="+1234567890", text="/doctor")
        assert out_doctor is not None
        self.assertTrue(out_doctor.success)
        self.assertIn("Doctor Diagnostics", out_doctor.text)

        # CLI-only command (/undo)
        out_undo = self.channel.simulate_incoming(from_number="+1234567890", text="/undo")
        assert out_undo is not None
        self.assertFalse(out_undo.success)
        self.assertEqual(out_undo.status, "rejected")
        self.assertIn("only available in the local cli", out_undo.text.lower())

    # -------------------------------------------------------------------------
    # 18. /reset Removes Persistent Session State
    # -------------------------------------------------------------------------
    def test_whatsapp_reset_removes_persisted_session(self) -> None:
        """Verify /reset deletes the session file from disk and initializes a clean slate."""
        # Step 1: Create active session
        self.channel.simulate_incoming(from_number="+1234567890", text="Message 1")
        session_files = list((self.repo_dir / ".brainfrog" / "sessions").glob("*.json"))
        self.assertEqual(len(session_files), 1)

        # Step 2: Send /reset
        out_reset = self.channel.simulate_incoming(from_number="+1234567890", text="/reset")
        assert out_reset is not None
        self.assertTrue(out_reset.success)
        self.assertIn("reset", out_reset.text.lower())

        # Step 3: Verified deleted from disk
        session_files_after = list((self.repo_dir / ".brainfrog" / "sessions").glob("*.json"))
        self.assertEqual(len(session_files_after), 0)

        # Step 4: Next message starts with turn 1
        self.channel.simulate_incoming(from_number="+1234567890", text="Fresh message")
        fresh_session = self.session_mgr.get("whatsapp:1234567890:1234567890")
        assert fresh_session is not None
        self.assertEqual(len(fresh_session.history), 1)
        self.assertEqual(fresh_session.history[0]["user"], "Fresh message")

    # -------------------------------------------------------------------------
    # 19. Response Chunking (<4000 chars per chunk)
    # -------------------------------------------------------------------------
    def test_whatsapp_response_chunking(self) -> None:
        """Verify long responses (>4000 chars) are cleanly chunked across multiple WhatsApp messages."""
        long_response = "A" * 3800 + "B" * 3800 + "C" * 1500  # Total: 9100 chars
        fake_s2_long = MagicMock()
        fake_s2_long.diagnose.return_value = long_response

        runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            sessions=self.session_mgr,
            system1_factory=lambda backend: self.fake_s1,
            system2_factory=lambda **kwargs: fake_s2_long,
        )
        self.channel.set_handler(runtime.handle_message)
        self.transport.clear()

        out = self.channel.simulate_incoming(from_number="+1234567890", text="Generate report")
        assert out is not None
        self.assertTrue(out.success)

        # 9100 characters must produce exactly 3 chunks of <= 4000 chars
        self.assertEqual(len(self.transport.sent_messages), 3)
        for msg in self.transport.sent_messages:
            self.assertLessEqual(len(msg["text"]), WHATSAPP_MAX_MSG_LEN)

        # Reconstructed content has zero data loss
        reconstructed = "".join(m["text"] for m in self.transport.sent_messages)
        self.assertEqual(reconstructed, long_response)

    # -------------------------------------------------------------------------
    # 20. Provider Failure Recovery
    # -------------------------------------------------------------------------
    def test_whatsapp_provider_failure_recovery(self) -> None:
        """Verify gateway resilience when provider encounters errors."""
        failing_s2 = DeterministicFakeSystem2(failure_mode="error")
        runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            sessions=self.session_mgr,
            system1_factory=lambda backend: self.fake_s1,
            system2_factory=lambda **kwargs: failing_s2,
        )
        self.channel.set_handler(runtime.handle_message)

        # Turn 1: Provider fails
        out1 = self.channel.simulate_incoming(from_number="+1234567890", text="Analyze code")
        assert out1 is not None
        self.assertFalse(out1.success)
        self.assertEqual(out1.status, "error")
        self.assertIn("error", out1.text.lower())

        # Transport is alive and can process subsequent request
        failing_s2.failure_mode = None
        out2 = self.channel.simulate_incoming(from_number="+1234567890", text="Analyze code retry")
        assert out2 is not None
        self.assertTrue(out2.success)

    # -------------------------------------------------------------------------
    # 21. MCP / Read-Only Plan Mode Preservation
    # -------------------------------------------------------------------------
    def test_whatsapp_mcp_guard_and_plan_fallback(self) -> None:
        """Remote requests in build mode must automatically fallback to plan mode to protect workspace."""
        out = self.channel.simulate_incoming(
            from_number="+1234567890",
            text="Build a web page and verify UI using browser",
        )
        assert out is not None
        # Effective mode was converted to plan mode (read-only)
        self.assertEqual(out.metadata.get("mode"), "plan")
        # No browser subprocess was created, workspace unmutated
        self.assertFalse((self.repo_dir / "index.html").exists())

    # -------------------------------------------------------------------------
    # 22. Channel Lifecycle
    # -------------------------------------------------------------------------
    def test_whatsapp_lifecycle(self) -> None:
        """Verify start and stop methods behave cleanly and idempotently."""
        self.assertFalse(self.channel.is_running)

        self.channel.start()
        self.assertTrue(self.channel.is_running)

        # Idempotent start
        self.channel.start()
        self.assertTrue(self.channel.is_running)

        self.channel.stop()
        self.assertFalse(self.channel.is_running)

        # Idempotent stop
        self.channel.stop()
        self.assertFalse(self.channel.is_running)

    # -------------------------------------------------------------------------
    # 23. Rate Limiting Check
    # -------------------------------------------------------------------------
    def test_whatsapp_rate_limiting(self) -> None:
        """Verify per-user rate limiting triggers rejection."""
        rate_channel = WhatsAppChannel(
            transport=self.transport,
            allowed_users=["+1234567890"],
            rate_limit_seconds=10.0,
        )
        rate_channel.set_handler(self.runtime.handle_message)

        # Message 1 succeeds
        out1 = rate_channel.simulate_incoming(from_number="+1234567890", text="Message 1")
        assert out1 is not None
        self.assertTrue(out1.success)

        # Message 2 arrives immediately -> rejected
        out2 = rate_channel.simulate_incoming(from_number="+1234567890", text="Message 2")
        assert out2 is not None
        self.assertFalse(out2.success)
        self.assertEqual(out2.status, "rejected")
        self.assertEqual(out2.error, "Rate limit exceeded")

    # -------------------------------------------------------------------------
    # 24. Outbound Secret Scrubbing
    # -------------------------------------------------------------------------
    def test_whatsapp_outbound_secret_scrubbing(self) -> None:
        """Secrets returned in model responses must be redacted before reaching WhatsApp transport."""
        class LeakySystem2(DeterministicFakeSystem2):
            def diagnose(self, *args: Any, **kwargs: Any) -> str:
                return (
                    "Configuration status:\n"
                    "OPENAI_API_KEY=sk-testsecretkey1234567890abcdefghijklmn\n"
                    "TELEGRAM_BOT_TOKEN=123456789:ABCDefgh-ijklmnopqrstuvwxyz123456\n"
                    "Header: Bearer supersecrettoken99887766\n"
                )

        leaky_runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            sessions=self.session_mgr,
            system1_factory=lambda backend: self.fake_s1,
            system2_factory=lambda **kwargs: LeakySystem2(),
        )
        self.channel.set_handler(leaky_runtime.handle_message)
        self.transport.clear()

        out = self.channel.simulate_incoming(from_number="+1234567890", text="Audit secrets")
        assert out is not None

        # Secret was scrubbed in OutgoingMessage
        self.assertNotIn("sk-testsecretkey", out.text)
        self.assertIn("[REDACTED_API_KEY]", out.text)
        self.assertNotIn("123456789:ABCD", out.text)
        self.assertIn("[REDACTED_BOT_TOKEN]", out.text)
        self.assertNotIn("supersecrettoken99887766", out.text)
        self.assertIn("[REDACTED_TOKEN]", out.text)

        # Secret was scrubbed before transport send
        self.assertGreater(len(self.transport.sent_messages), 0)
        sent_text = self.transport.sent_messages[0]["text"]
        self.assertNotIn("sk-testsecretkey", sent_text)
        self.assertNotIn("123456789:ABCD", sent_text)
        self.assertNotIn("supersecrettoken99887766", sent_text)

    # -------------------------------------------------------------------------
    # 25. Optional Live WhatsApp Cloud API Smoke Test (Env-Gated)
    # -------------------------------------------------------------------------
    @unittest.skipIf(
        not os.environ.get("BRAINFROG_WHATSAPP_LIVE"),
        "Live WhatsApp smoke tests require BRAINFROG_WHATSAPP_LIVE=1 and Meta Cloud API credentials",
    )
    def test_live_whatsapp_cloud_transport_smoke(self) -> None:
        """Live Meta WhatsApp Cloud API smoke test (skipped by default)."""
        phone_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
        token = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
        test_recipient = os.environ.get("WHATSAPP_TEST_RECIPIENT", "")

        if not phone_id or not token or not test_recipient:
            self.skipTest("Missing live WhatsApp credentials.")

        transport = WhatsAppCloudTransport(phone_number_id=phone_id, access_token=token)
        ok = transport.send(test_recipient, "🐸 BrainFrog Live WhatsApp Verification Ping")
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
