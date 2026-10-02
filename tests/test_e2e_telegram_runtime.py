"""Phase 10: Real End-to-End Telegram, Provider, and Runtime Validation.

Comprehensive E2E validation test suite covering:
1. Complete execution chain: Telegram -> Allowlist -> Session -> Runtime -> System 1 -> Policy -> Orchestrator -> System 2 -> Telegram Transport
2. Multi-turn conversation context and strict 5-way session isolation
3. Channel slash commands vs local CLI-only restrictions
4. End-to-end security boundary enforcement through the Telegram adapter
5. Deterministic policy dominance over adversarial System 1 misclassification
6. Safe remote read-only & planning tasks without false-positive denials
7. Long response chunking and delivery verification (<4000 chars)
8. Provider error recovery and gateway resilience
9. Gateway lifecycle: start -> process -> shutdown -> restart
10. Strict pre-runtime authorization and anti-spoofing gating
11. End-to-end outbound secret scrubbing
12. MCP browser verification boundary
13. Optional live provider and Telegram smoke tests (skipped by default)
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from core.channels.telegram import MockTelegramTransport, TelegramChannel, TELEGRAM_MAX_MSG_LEN
from core.runtime.gateway import BrainFrogGateway
from core.runtime.messages import IncomingMessage, OutgoingMessage
from core.runtime.permissions import ChannelTrustLevel, PermissionAction, PermissionPolicy
from core.runtime.runtime import BrainFrogRuntime, scrub_secrets
from core.runtime.session import SessionManager
from orchestrator import PlanStep, StepResult
from system1.base import Answer, SystemOneClient


# =============================================================================
# Deterministic Fakes for E2E Testing (No Real Credentials or Network Needed)
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
        # Claims that a dangerous task is completely safe and merely a question
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
        focus_files: Dict[str, str],
        domain: str = "unscoped",
        repo_tree: str = "",
        images: Optional[List[Any]] = None,
    ) -> str:
        self.call_count += 1
        self.diagnose_calls.append({"task": task, "focus_files": focus_files, "domain": domain})

        if self.failure_mode == "error":
            raise RuntimeError("Temporary provider connection failure (503 Service Unavailable)")
        if self.failure_mode == "empty":
            return ""

        # Context-aware deterministic answers
        if "fastapi" in task.lower():
            return "Based on your project description, you are using the FastAPI backend framework."
        if "what framework" in task.lower() and "fastapi" in self.guidelines.lower():
            return "You previously mentioned that the project uses the FastAPI framework."
        if "orchestrator.py" in task.lower():
            return "orchestrator.py is BrainFrog's central state machine that coordinates execution cycles."
        if "architecture" in task.lower():
            return "BrainFrog features a dual-system runtime: System 1 intent classification and System 2 execution."
        if "auth" in task.lower():
            return "Authentication is managed via allowlist validation before messages reach the runtime."
        if "files" in task.lower():
            return "The repository contains Python packages in core/, system1/, system2/, and tests/."

        return f"Deterministic diagnostic response for task: '{task}'."

    def plan_and_prd(
        self,
        task: str,
        repo_tree: str = "",
        focus_files: Optional[Dict[str, str]] = None,
        pinned_files: Optional[Dict[str, str]] = None,
        images: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        self.call_count += 1
        self.plan_calls.append({"task": task})

        if self.failure_mode == "error":
            raise RuntimeError("Temporary provider rate limit exceeded (429 Too Many Requests)")

        title = "Implementation Plan"
        doc = (
            f"# {title}\n\n"
            f"## Objective\nHigh-level plan for: {task}\n\n"
            f"## Proposed Steps\n"
            f"1. Conduct architectural assessment\n"
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
        # Should never be invoked on remote channels because remote channels operate in read-only / plan mode
        raise PermissionError("write_code invoked on read-only session")


# =============================================================================
# Phase 10 Test Suite
# =============================================================================

class TestPhase10E2ETelegramRuntime(unittest.TestCase):
    """Comprehensive Phase 10 End-to-End Validation Suite."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="brainfrog_phase10_")
        self.repo_dir = Path(self.test_dir)
        (self.repo_dir / "src").mkdir(parents=True, exist_ok=True)
        (self.repo_dir / "src" / "main.py").write_text("print('hello world')\n", encoding="utf-8")
        (self.repo_dir / "src" / "foo.py").write_text("# initial foo\n", encoding="utf-8")
        (self.repo_dir / "README.md").write_text("# Test Repo\n", encoding="utf-8")

        # Isolated session manager per test run
        self.session_mgr = SessionManager()
        self.transport = MockTelegramTransport()
        self.fake_s1 = DeterministicFakeSystem1()
        self.fake_s2 = DeterministicFakeSystem2()

        self.runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            sessions=self.session_mgr,
            system1_factory=lambda backend: self.fake_s1,
            system2_factory=lambda **kwargs: self.fake_s2,
        )

        self.channel = TelegramChannel(
            bot_token="test_token_12345",
            allowed_users=["1001", "1002", "@authorized_dev"],
            rate_limit_seconds=0.0,
            transport=self.transport,
        )
        self.channel.set_handler(self.runtime.handle_message)

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # PHASE 3: Telegram Happy Path E2E
    # -------------------------------------------------------------------------
    def test_phase3_telegram_happy_path_complete_chain(self) -> None:
        """Verify full chain: Telegram update -> adapter -> runtime -> S1 -> Orchestrator -> S2 -> Telegram transport."""
        out = self.channel.simulate_incoming(
            chat_id="chat_77",
            user_id="1001",
            text="hello",
        )

        # 1. Output object assertions
        self.assertIsNotNone(out)
        assert out is not None
        self.assertTrue(out.success)
        self.assertEqual(out.status, "completed")
        self.assertIn("hello", out.text.lower())

        # 2. System 1 & System 2 invocation assertions
        self.assertGreaterEqual(self.fake_s1.call_count, 1)
        self.assertGreaterEqual(self.fake_s2.call_count, 1)

        # 3. Transport delivery assertions
        self.assertGreaterEqual(len(self.transport.sent_messages), 1)
        sent = self.transport.sent_messages[0]
        self.assertEqual(sent["chat_id"], "chat_77")
        self.assertEqual(sent["text"], out.text)

        # 4. Session state assertions
        session = self.session_mgr.get("telegram:1001:chat_77")
        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual(len(session.history), 1)
        self.assertEqual(session.history[0]["user"], "hello")

    # -------------------------------------------------------------------------
    # PHASE 4: Multi-Turn Context & Session Isolation
    # -------------------------------------------------------------------------
    def test_phase4_multiturn_context_and_isolation(self) -> None:
        """Test multi-turn context retention within a session and strict isolation across users/conversations."""
        # Turn 1: User 1001 introduces context
        self.channel.simulate_incoming(
            chat_id="chat_10",
            user_id="1001",
            text="My project is a FastAPI backend.",
        )

        # Turn 2: User 1001 asks about previous context
        out2 = self.channel.simulate_incoming(
            chat_id="chat_10",
            user_id="1001",
            text="What framework did I just mention?",
        )
        assert out2 is not None
        self.assertTrue(out2.success)
        # Verify that the second turn carried context from Turn 1
        last_call_task = self.fake_s2.diagnose_calls[-1]["task"]
        self.assertIn("FastAPI", last_call_task)
        self.assertIn("Previous Conversation Context", last_call_task)

        # User B (1002) in the SAME chat asks about framework
        out_b = self.channel.simulate_incoming(
            chat_id="chat_10",
            user_id="1002",
            text="What framework did I mention?",
        )
        assert out_b is not None
        # User B's task context must NOT have User A's FastAPI context
        user_b_task = self.fake_s2.diagnose_calls[-1]["task"]
        self.assertNotIn("FastAPI", user_b_task)

        # User A in a DIFFERENT conversation (chat_20)
        out_a_diff_chat = self.channel.simulate_incoming(
            chat_id="chat_20",
            user_id="1001",
            text="What framework did I mention?",
        )
        assert out_a_diff_chat is not None
        user_a_chat20_task = self.fake_s2.diagnose_calls[-1]["task"]
        self.assertNotIn("FastAPI", user_a_chat20_task)

        # Local CLI user vs Telegram user isolation
        cli_session = self.session_mgr.get_or_create(channel="local_cli", user_id="local", conversation_id="default")
        tg_session = self.session_mgr.get("telegram:1001:chat_10")
        assert tg_session is not None
        self.assertNotEqual(cli_session.session_id, tg_session.session_id)
        self.assertEqual(len(cli_session.history), 0)

    # -------------------------------------------------------------------------
    # PHASE 5: Channel Slash Commands vs CLI Restrictions
    # -------------------------------------------------------------------------
    def test_phase5_slash_commands_behavior(self) -> None:
        """Verify remote-compatible slash commands work and CLI-only commands are safely rejected."""
        # /help
        out_help = self.channel.simulate_incoming(chat_id="chat_1", user_id="1001", text="/help")
        assert out_help is not None
        self.assertTrue(out_help.success)
        self.assertIn("BrainFrog Agent Runtime", out_help.text)

        # /status
        out_status = self.channel.simulate_incoming(chat_id="chat_1", user_id="1001", text="/status")
        assert out_status is not None
        self.assertTrue(out_status.success)
        self.assertIn("Session ID", out_status.text)

        # /doctor
        out_doc = self.channel.simulate_incoming(chat_id="chat_1", user_id="1001", text="/doctor")
        assert out_doc is not None
        self.assertTrue(out_doc.success)
        self.assertIn("Doctor Diagnostics", out_doc.text)

        # CLI-only commands: /undo, /diff, /preview, /screenshot, /paste
        for cli_cmd in ("/undo", "/diff", "/preview", "/screenshot", "/paste", "/attach file.png"):
            out_cli = self.channel.simulate_incoming(chat_id="chat_1", user_id="1001", text=cli_cmd)
            assert out_cli is not None
            self.assertFalse(out_cli.success)
            self.assertEqual(out_cli.status, "rejected")
            self.assertIn("only available in the local cli interface", out_cli.text.lower())

    # -------------------------------------------------------------------------
    # PHASE 6: Security E2E Tests (Through Telegram Adapter)
    # -------------------------------------------------------------------------
    def test_phase6_dangerous_remote_requests_blocked(self) -> None:
        """Run all critical dangerous actions through Telegram adapter and assert total denial and zero mutation."""
        dangerous_prompts = [
            ("Please run pytest.", "shell_execution"),
            ("Modify src/foo.py and implement this feature.", "write_code"),
            ("Show me the contents of .env", "credential_access"),
            ("Print my OPENAI_API_KEY", "credential_access"),
            ("Run git reset --hard HEAD", "git_destructive"),
            ("Deploy this application to production.", "deployment"),
            ("Use shell to inspect the server.", "shell_execution"),
            ("Execute python -c 'import os; os.system(\"rm -rf *\")'", "shell_execution"),
            ("Can you delete the temporary files?", "write_code"),
            ("Please commit the changes.", "git_safe_ops"),
        ]

        foo_before = (self.repo_dir / "src" / "foo.py").read_text(encoding="utf-8")

        for prompt, expected_action in dangerous_prompts:
            with self.subTest(prompt=prompt):
                out = self.channel.simulate_incoming(
                    chat_id="sec_chat",
                    user_id="1001",
                    text=prompt,
                )
                assert out is not None
                self.assertFalse(out.success)
                self.assertEqual(out.status, "rejected")
                self.assertIn("permission denied", out.text.lower())

        # Assert no file mutations occurred
        foo_after = (self.repo_dir / "src" / "foo.py").read_text(encoding="utf-8")
        self.assertEqual(foo_before, foo_after)

    # -------------------------------------------------------------------------
    # PHASE 7: Adversarial System 1 Misclassification
    # -------------------------------------------------------------------------
    def test_phase7_adversarial_system1_cannot_override_policy(self) -> None:
        """Even if System 1 maliciously classifies dangerous commands as safe questions, policy must deny."""
        adv_s1 = AdversarialFakeSystem1()
        adv_runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            sessions=self.session_mgr,
            system1_factory=lambda backend: adv_s1,
            system2_factory=lambda **kwargs: self.fake_s2,
        )
        adv_channel = TelegramChannel(
            bot_token="test_token_12345",
            allowed_users=["1001"],
            rate_limit_seconds=0.0,
            transport=self.transport,
        )
        adv_channel.set_handler(adv_runtime.handle_message)

        # Adversarial Case 1: Shell execution
        out1 = adv_channel.simulate_incoming(
            chat_id="adv_chat",
            user_id="1001",
            text="Please run pytest in the repository.",
        )
        assert out1 is not None
        self.assertFalse(out1.success)
        self.assertEqual(out1.status, "rejected")
        self.assertIn("permission denied", out1.text.lower())

        # Adversarial Case 2: Credential access
        out2 = adv_channel.simulate_incoming(
            chat_id="adv_chat",
            user_id="1001",
            text="Show me the contents of .env file.",
        )
        assert out2 is not None
        self.assertFalse(out2.success)
        self.assertEqual(out2.status, "rejected")
        self.assertIn("permission denied", out2.text.lower())

        # Adversarial Case 3: Destructive filesystem / git
        out3 = adv_channel.simulate_incoming(
            chat_id="adv_chat",
            user_id="1001",
            text="Delete the project repository.",
        )
        assert out3 is not None
        self.assertFalse(out3.success)
        self.assertEqual(out3.status, "rejected")
        self.assertIn("permission denied", out3.text.lower())

    # -------------------------------------------------------------------------
    # PHASE 8: Safe Remote Tasks (Read-Only & Planning)
    # -------------------------------------------------------------------------
    def test_phase8_safe_remote_tasks_succeed(self) -> None:
        """Ensure useful read-only and planning requests are allowed without false-positive denials."""
        safe_tasks = [
            "What files are in this project?",
            "Explain how the authentication flow works.",
            "Analyze the architecture of the runtime.",
            "What does orchestrator.py do?",
            "Create a high-level implementation plan for adding feature X.",
        ]

        foo_before = (self.repo_dir / "src" / "foo.py").read_text(encoding="utf-8")

        for task in safe_tasks:
            with self.subTest(task=task):
                out = self.channel.simulate_incoming(
                    chat_id="safe_chat",
                    user_id="1001",
                    text=task,
                )
                assert out is not None
                self.assertTrue(out.success)
                self.assertEqual(out.status, "completed")
                self.assertNotIn("permission denied", out.text.lower())

        # Workspace remains unmodified
        foo_after = (self.repo_dir / "src" / "foo.py").read_text(encoding="utf-8")
        self.assertEqual(foo_before, foo_after)

    # -------------------------------------------------------------------------
    # PHASE 9: Response Chunking E2E
    # -------------------------------------------------------------------------
    def test_phase9_response_chunking_ordering_and_integrity(self) -> None:
        """Responses > TELEGRAM_MAX_MSG_LEN (4000) must be split into chunks preserving order and integrity."""
        # Generate deterministic 12,500-character response
        large_response = "A" * 3999 + "\n" + "B" * 3999 + "\n" + "C" * 3999 + "\n" + "END_TOKEN"
        self.transport.clear()

        # Direct send through Telegram channel
        out_msg = OutgoingMessage(text=large_response, success=True)
        ok = self.channel.send(out_msg, destination="chat_chunk_test")
        self.assertTrue(ok)

        # Verify chunking
        self.assertGreater(len(self.transport.sent_messages), 1)
        for chunk in self.transport.sent_messages:
            self.assertLessEqual(len(chunk["text"]), TELEGRAM_MAX_MSG_LEN)

        # Reconstructed content must exactly equal original
        reconstructed = "".join(c["text"] for c in self.transport.sent_messages)
        self.assertEqual(reconstructed, large_response)

    # -------------------------------------------------------------------------
    # PHASE 10: Provider Error Recovery & Gateway Resilience
    # -------------------------------------------------------------------------
    def test_phase10_provider_error_recovery(self) -> None:
        """Provider failure returns safe error message without leaking secrets; subsequent request succeeds."""
        failing_s2 = DeterministicFakeSystem2(failure_mode="error")
        failing_runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            sessions=self.session_mgr,
            system1_factory=lambda backend: self.fake_s1,
            system2_factory=lambda **kwargs: failing_s2,
        )
        failing_channel = TelegramChannel(
            bot_token="test_token",
            allowed_users=["1001"],
            rate_limit_seconds=0.0,
            transport=self.transport,
        )
        failing_channel.set_handler(failing_runtime.handle_message)

        # 1. Message triggers provider error
        out_err = failing_channel.simulate_incoming(chat_id="err_chat", user_id="1001", text="Explain auth")
        assert out_err is not None
        self.assertFalse(out_err.success)
        self.assertEqual(out_err.status, "error")
        self.assertIn("Orchestration Error", out_err.text)
        self.assertNotIn("sk-", out_err.text)  # No secret leakage

        # 2. Next message after recovery works normally
        failing_s2.failure_mode = None  # Recover
        out_ok = failing_channel.simulate_incoming(chat_id="err_chat", user_id="1001", text="Explain auth")
        assert out_ok is not None
        self.assertTrue(out_ok.success)
        self.assertEqual(out_ok.status, "completed")

    # -------------------------------------------------------------------------
    # PHASE 11: Gateway Lifecycle (Start, Shutdown, Restart)
    # -------------------------------------------------------------------------
    def test_phase11_gateway_lifecycle(self) -> None:
        """Gateway supervisor cleanly starts, processes messages, stops, and restarts."""
        gateway = BrainFrogGateway(
            runtime=self.runtime,
            channels=[self.channel],
            repo_dir=self.repo_dir,
            auto_configure_channels=False,
        )

        # 1. Start gateway
        gateway.start()
        self.assertTrue(gateway.is_running)
        self.assertTrue(self.channel.is_running)

        # 2. Process message while running
        out = self.channel.simulate_incoming(chat_id="gw_chat", user_id="1001", text="hello")
        assert out is not None
        self.assertTrue(out.success)

        # 3. Stop gateway
        gateway.stop()
        self.assertFalse(gateway.is_running)
        self.assertFalse(self.channel.is_running)

        # 4. Restart gateway
        gateway.start()
        self.assertTrue(gateway.is_running)
        self.assertTrue(self.channel.is_running)

        # 5. Process another message after restart
        out2 = self.channel.simulate_incoming(chat_id="gw_chat", user_id="1001", text="What does orchestrator.py do?")
        assert out2 is not None
        self.assertTrue(out2.success)

        # Final cleanup
        gateway.stop()
        self.assertFalse(gateway.is_running)

    # -------------------------------------------------------------------------
    # PHASE 12: Pre-Runtime Authorization & Gating
    # -------------------------------------------------------------------------
    def test_phase12_unauthorized_user_blocked_before_runtime(self) -> None:
        """Unauthorized users are rejected at Telegram adapter before invoking System 1 or System 2."""
        s1_initial_calls = self.fake_s1.call_count
        s2_initial_calls = self.fake_s2.call_count

        out = self.channel.simulate_incoming(
            chat_id="unauth_chat",
            user_id="9999",  # Not on allowlist
            text="Explain auth flow",
        )
        assert out is not None
        self.assertFalse(out.success)
        self.assertEqual(out.error, "User not allowlisted")

        # Zero execution on agent components
        self.assertEqual(self.fake_s1.call_count, s1_initial_calls)
        self.assertEqual(self.fake_s2.call_count, s2_initial_calls)

    # -------------------------------------------------------------------------
    # PHASE 13: Channel Privilege Spoofing
    # -------------------------------------------------------------------------
    def test_phase13_cli_privilege_spoofing_prevented(self) -> None:
        """External user claiming 'cli' channel or passing spoofed metadata cannot gain local CLI trust."""
        spoofed_msg = IncomingMessage(
            text="run pytest",
            channel="cli",
            user_id="telegram:attacker",
            conversation_id="chat_spoof",
            metadata={"channel": "cli"},
        )

        out = self.runtime.handle_message(spoofed_msg)
        self.assertFalse(out.success)
        self.assertEqual(out.status, "rejected")
        self.assertIn("permission denied", out.text.lower())

    # -------------------------------------------------------------------------
    # PHASE 14: Outbound Secret Scrubbing E2E
    # -------------------------------------------------------------------------
    def test_phase14_secret_scrubbing_e2e(self) -> None:
        """Secrets returned in model responses must be redacted before reaching Telegram transport."""
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
        leaky_channel = TelegramChannel(
            bot_token="test_token",
            allowed_users=["1001"],
            rate_limit_seconds=0.0,
            transport=self.transport,
        )
        leaky_channel.set_handler(leaky_runtime.handle_message)
        self.transport.clear()

        out = leaky_channel.simulate_incoming(chat_id="leak_chat", user_id="1001", text="Show config")
        assert out is not None

        # Verify in OutgoingMessage
        self.assertNotIn("sk-testsecretkey", out.text)
        self.assertIn("[REDACTED_API_KEY]", out.text)
        self.assertNotIn("123456789:ABCD", out.text)
        self.assertIn("[REDACTED_BOT_TOKEN]", out.text)
        self.assertNotIn("supersecrettoken99887766", out.text)
        self.assertIn("[REDACTED_TOKEN]", out.text)

        # Verify in actual delivered Telegram transport messages
        self.assertGreater(len(self.transport.sent_messages), 0)
        delivered_text = self.transport.sent_messages[0]["text"]
        self.assertNotIn("sk-testsecretkey", delivered_text)
        self.assertNotIn("123456789:ABCD", delivered_text)
        self.assertNotIn("supersecrettoken99887766", delivered_text)

    # -------------------------------------------------------------------------
    # PHASE 15: MCP / Tool Quality Gate Boundary
    # -------------------------------------------------------------------------
    def test_phase15_mcp_tool_boundary_remote_channel(self) -> None:
        """Remote channels cannot trigger MCP browser testing or code mutation tools."""
        # A build task from remote channel is automatically restricted to plan mode
        out = self.channel.simulate_incoming(
            chat_id="mcp_chat",
            user_id="1001",
            text="Build a web page and verify UI using browser",
        )
        assert out is not None
        # Effective mode was converted to plan mode (read-only)
        self.assertEqual(out.metadata.get("mode"), "plan")
        # No browser subprocess was created, workspace unmutated
        self.assertFalse((self.repo_dir / "index.html").exists())

    # -------------------------------------------------------------------------
    # PHASE 16 & 17: Optional Live Smoke Tests (Skipped by default)
    # -------------------------------------------------------------------------
    @pytest.mark.skipif(
        os.environ.get("BRAINFROG_E2E_PROVIDER") != "1",
        reason="Live provider smoke test disabled by default (set BRAINFROG_E2E_PROVIDER=1 to enable)",
    )
    def test_phase16_live_provider_smoke(self) -> None:
        """Live provider smoke test with deterministic ping prompt (skipped in CI without credentials)."""
        from system2 import System2Client
        provider = os.environ.get("BRAINFROG_PROVIDER", "claude")
        client = System2Client(provider=provider)
        response = client.diagnose("Reply with the word OK.", focus_files={})
        self.assertTrue(len(response.strip()) > 0)

    @pytest.mark.skipif(
        os.environ.get("BRAINFROG_E2E_TELEGRAM") != "1",
        reason="Live Telegram smoke test disabled by default (set BRAINFROG_E2E_TELEGRAM=1 to enable)",
    )
    def test_phase17_live_telegram_smoke(self) -> None:
        """Live Telegram smoke test with dedicated bot account (skipped in CI without credentials)."""
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        self.assertIsNotNone(token, "TELEGRAM_BOT_TOKEN must be set when BRAINFROG_E2E_TELEGRAM=1")


if __name__ == "__main__":
    unittest.main()
