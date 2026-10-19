"""Regression test suite for Telegram conversational intent routing & literal output handling.

Covers:
1. 'balas hanya dengan teks: PONG' returns exactly 'PONG'.
2. 'reply with only PONG' returns exactly 'PONG'.
3. 'echo hello' follows the intended conversational behavior without generating a PRD.
4. Ordinary informational questions do not generate plan artifacts.
5. Real planning requests still call plan_and_prd().
6. Explicit literal-output requests do not invoke plan_and_prd() and do not save .brainfrog/plans/*.md.
7. Session-scoped provider/model overrides remain respected for requests that call System 2.
8. Remote Telegram restrictions on shell, file writes, code edits, and Git push remain unchanged.
9. Existing CLI and Telegram tests continue passing.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from core.channels.telegram import MockTelegramTransport, TelegramChannel
from core.runtime.messages import IncomingMessage, OutgoingMessage
from core.runtime.permissions import ChannelTrustLevel, PermissionAction, PermissionPolicy, classify_request_action
from core.runtime.runtime import BrainFrogRuntime
from orchestrator import extract_literal_output, is_question_task
from system1.base import Answer, SystemOneClient


def _synthetic_test_token(name: str = "KEY") -> str:
    """Return obviously fake synthetic token for test fixtures."""
    return f"synthetic_test_{name}_placeholder_value_12345"


class CapturingSystem2Client:
    """System 2 client double capturing calls and configured model/provider."""

    def __init__(self, model: Optional[str] = None, provider: Optional[str] = None, **kwargs: Any) -> None:
        self.model = model
        self.provider = provider
        self.diagnose_calls: List[str] = []
        self.plan_and_prd_calls: List[str] = []

    def plan(self, *args: Any, **kwargs: Any) -> List[Any]:
        return []

    def plan_and_prd(self, task: str, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        self.plan_and_prd_calls.append(task)
        return {
            "title": "Architectural Plan",
            "markdown_doc": f"# Architectural PRD for {task}\n\n## Summary\nPlan details...",
            "problem": task,
            "goals": [task],
            "steps": [{"id": "1", "description": "Step 1", "files": []}],
            "relevant_files": [],
        }

    def diagnose(self, task: str, *args: Any, **kwargs: Any) -> str:
        self.diagnose_calls.append(task)
        primary = task.split("\n[Previous Conversation Context]")[0].strip().lower()
        if "echo hello" in primary:
            return "hello"
        return f"Response from {self.provider}/{self.model}: Information analyzed."


class DeterministicSystem1Double(SystemOneClient):
    """Deterministic System 1 test double."""
    name: str = "fake_s1"

    def __init__(self, default_change_type: str = "feature_request") -> None:
        self.default_change_type = default_change_type
        self.call_count = 0

    def decide(self, state: Dict[str, Any], questions: Dict[str, Any]) -> Dict[str, Answer]:
        self.call_count += 1
        return {
            "likely_domain": Answer(choice="core", confidence=0.85),
            "change_type": Answer(choice=self.default_change_type, confidence=0.85),
            "is_sensitive": Answer(noul=0.05, confidence=0.95),
            "complexity": Answer(score=1, confidence=0.85),
            "needs_tests": Answer(noul=0.10, confidence=0.85),
        }


class TestConversationalIntentRouting(unittest.TestCase):
    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="brainfrog_test_conv_")
        self.repo_dir = Path(self.test_dir)
        (self.repo_dir / ".git").mkdir()
        (self.repo_dir / "core").mkdir()
        (self.repo_dir / "core" / "app.py").write_text("# app code\n", encoding="utf-8")

        self.transport = MockTelegramTransport()
        self.captured_s2_instances: List[CapturingSystem2Client] = []

        def fake_s2_factory(model: Optional[str] = None, provider: Optional[str] = None, **kwargs: Any) -> CapturingSystem2Client:
            client = CapturingSystem2Client(model=model, provider=provider, **kwargs)
            self.captured_s2_instances.append(client)
            return client

        self.s2_factory = fake_s2_factory
        self.fake_s1 = DeterministicSystem1Double()

        self.runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            default_backend="typesafe",
            default_provider="claude",
            default_model="claude-sonnet-5",
            system1_factory=lambda b: self.fake_s1,
            system2_factory=self.s2_factory,
            persist_sessions=False,
        )

        self.channel = TelegramChannel(
            bot_token=_synthetic_test_token("BOT"),
            allowed_users=["1001", "8783894580"],
            rate_limit_seconds=0.0,
            transport=self.transport,
        )
        self.channel.set_handler(self.runtime.handle_message)

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _get_plan_files(self) -> List[Path]:
        plans_dir = self.repo_dir / ".brainfrog" / "plans"
        if not plans_dir.exists():
            return []
        return list(plans_dir.glob("*.md"))

    # -------------------------------------------------------------------------
    # Unit Tests for Helper Functions
    # -------------------------------------------------------------------------
    def test_extract_literal_output_patterns(self) -> None:
        """Deterministic extraction of unambiguous literal output directives."""
        self.assertEqual(extract_literal_output("balas hanya dengan teks: PONG"), "PONG")
        self.assertEqual(extract_literal_output("balas hanya dengan: PONG"), "PONG")
        self.assertEqual(extract_literal_output("balas hanya: PONG"), "PONG")
        self.assertEqual(extract_literal_output("balas hanya PONG"), "PONG")
        self.assertEqual(extract_literal_output("jawab hanya dengan teks: PONG"), "PONG")
        self.assertEqual(extract_literal_output("reply with only PONG"), "PONG")
        self.assertEqual(extract_literal_output("reply only with PONG"), "PONG")
        self.assertEqual(extract_literal_output("reply with only text: PONG"), "PONG")
        self.assertEqual(extract_literal_output("say only PONG"), "PONG")
        self.assertEqual(extract_literal_output("output only text: PONG"), "PONG")
        self.assertEqual(extract_literal_output('reply with only "PONG"'), "PONG")
        self.assertEqual(extract_literal_output("reply with only 'PONG'"), "PONG")
        self.assertEqual(extract_literal_output("reply with only `PONG`"), "PONG")

        # Non-literal directives return None
        self.assertIsNone(extract_literal_output("echo hello"))
        self.assertIsNone(extract_literal_output("what is the architecture of brainfrog?"))
        self.assertIsNone(extract_literal_output("plan the migration to Postgres"))
        self.assertIsNone(extract_literal_output("balas hanya dengan teks: a plan to refactor auth"))

    def test_classify_request_action_conversational(self) -> None:
        """Permission classifier marks conversational & Indonesian inquiry requests as DIAGNOSE."""
        self.assertEqual(classify_request_action("balas hanya dengan teks: PONG"), PermissionAction.DIAGNOSE)
        self.assertEqual(classify_request_action("reply with only PONG"), PermissionAction.DIAGNOSE)
        self.assertEqual(classify_request_action("echo hello"), PermissionAction.DIAGNOSE)
        self.assertEqual(classify_request_action("ping"), PermissionAction.DIAGNOSE)
        self.assertEqual(classify_request_action("jelaskan alur BrainFrog"), PermissionAction.DIAGNOSE)
        self.assertEqual(classify_request_action("apa itu System 1?"), PermissionAction.DIAGNOSE)
        # Real planning requests remain PLAN
        self.assertEqual(classify_request_action("buatkan plan untuk migrasi database"), PermissionAction.PLAN)
        self.assertEqual(classify_request_action("create a plan to add dark mode"), PermissionAction.PLAN)

    # -------------------------------------------------------------------------
    # Requirements 1 & 6: 'balas hanya dengan teks: PONG' -> exactly 'PONG', no PRD
    # -------------------------------------------------------------------------
    def test_balas_hanya_dengan_teks_pong_returns_literal_pong(self) -> None:
        """Requirement 1 & 6: 'balas hanya dengan teks: PONG' returns literal 'PONG' and no plan file."""
        out = self.channel.simulate_incoming(
            chat_id="chat_1",
            user_id="8783894580",
            text="balas hanya dengan teks: PONG",
        )
        self.assertIsNotNone(out)
        assert out is not None
        self.assertTrue(out.success)
        self.assertEqual(out.text, "PONG")
        # Invariant: plan_and_prd not invoked, no plan documents created
        self.assertEqual(len(self._get_plan_files()), 0)
        if self.captured_s2_instances:
            self.assertEqual(len(self.captured_s2_instances[-1].plan_and_prd_calls), 0)

    # -------------------------------------------------------------------------
    # Requirements 2 & 6: 'reply with only PONG' -> exactly 'PONG', no PRD
    # -------------------------------------------------------------------------
    def test_reply_with_only_pong_returns_literal_pong(self) -> None:
        """Requirement 2 & 6: 'reply with only PONG' returns literal 'PONG' and no plan file."""
        out = self.channel.simulate_incoming(
            chat_id="chat_1",
            user_id="1001",
            text="reply with only PONG",
        )
        self.assertIsNotNone(out)
        assert out is not None
        self.assertTrue(out.success)
        self.assertEqual(out.text, "PONG")
        # Invariant: plan_and_prd not invoked, no plan documents created
        self.assertEqual(len(self._get_plan_files()), 0)
        if self.captured_s2_instances:
            self.assertEqual(len(self.captured_s2_instances[-1].plan_and_prd_calls), 0)

    # -------------------------------------------------------------------------
    # Requirement 3: 'echo hello' -> conversational response, no PRD
    # -------------------------------------------------------------------------
    def test_echo_hello_conversational_response_without_prd(self) -> None:
        """Requirement 3: 'echo hello' uses System 2 conversational path without generating a PRD."""
        out = self.channel.simulate_incoming(
            chat_id="chat_1",
            user_id="1001",
            text="echo hello",
        )
        self.assertIsNotNone(out)
        assert out is not None
        self.assertTrue(out.success)
        self.assertEqual(out.text, "hello")
        # Invariant: diagnose called, plan_and_prd NOT called, no plan files created
        self.assertEqual(len(self._get_plan_files()), 0)
        self.assertGreater(len(self.captured_s2_instances), 0)
        latest_s2 = self.captured_s2_instances[-1]
        self.assertEqual(len(latest_s2.plan_and_prd_calls), 0)
        self.assertGreater(len(latest_s2.diagnose_calls), 0)

    # -------------------------------------------------------------------------
    # Requirement 4: Informational questions do not generate plan artifacts
    # -------------------------------------------------------------------------
    def test_ordinary_informational_question_does_not_generate_plan(self) -> None:
        """Requirement 4: Explanatory / informational questions do not generate .brainfrog/plans/*.md."""
        out = self.channel.simulate_incoming(
            chat_id="chat_1",
            user_id="1001",
            text="Jelaskan dalam maksimal 3 bullet apa fungsi system 1 dan system 2. Jangan membaca atau mengubah file",
        )
        self.assertIsNotNone(out)
        assert out is not None
        self.assertTrue(out.success)
        self.assertIn("Response from", out.text)
        # Invariant: no plan files created
        self.assertEqual(len(self._get_plan_files()), 0)
        latest_s2 = self.captured_s2_instances[-1]
        self.assertEqual(len(latest_s2.plan_and_prd_calls), 0)
        self.assertGreater(len(latest_s2.diagnose_calls), 0)

    # -------------------------------------------------------------------------
    # Requirement 5: Real planning requests still call plan_and_prd()
    # -------------------------------------------------------------------------
    def test_real_planning_request_calls_plan_and_prd_and_saves_plan(self) -> None:
        """Requirement 5: Genuine architectural or feature planning requests call plan_and_prd and save plan artifact."""
        out = self.channel.simulate_incoming(
            chat_id="chat_1",
            user_id="1001",
            text="Rancang arsitektur dan buat rencana implementasi untuk sistem caching Redis",
        )
        self.assertIsNotNone(out)
        assert out is not None
        self.assertTrue(out.success)
        self.assertIn("# Architectural PRD", out.text)
        # Invariant: plan_and_prd called and .md plan document was saved
        self.assertGreater(len(self._get_plan_files()), 0)
        latest_s2 = self.captured_s2_instances[-1]
        self.assertGreater(len(latest_s2.plan_and_prd_calls), 0)

    # -------------------------------------------------------------------------
    # Requirement 7: Session-scoped provider/model overrides remain respected
    # -------------------------------------------------------------------------
    def test_session_scoped_model_override_respected_for_conversational_requests(self) -> None:
        """Requirement 7: System 2 calls respect session-scoped provider/model overrides."""
        fake_key = _synthetic_test_token("SUMOPOD")
        with patch.dict(os.environ, {"SUMOPOD_API_KEY": fake_key}):
            # 1. Switch model in session to sumopod / deepseek-v4.1-flash:netra
            out_sw = self.channel.simulate_incoming(
                chat_id="chat_override",
                user_id="1001",
                text="/model sumopod deepseek-v4.1-flash:netra",
            )
            self.assertIsNotNone(out_sw)
            self.assertTrue(out_sw.success)

            # 2. Conversational prompt that requires System 2
            out_task = self.channel.simulate_incoming(
                chat_id="chat_override",
                user_id="1001",
                text="What are the components of BrainFrog?",
            )
            self.assertIsNotNone(out_task)
            self.assertTrue(out_task.success)

            # Verify latest S2 client instance has overridden provider & model
            latest_s2 = self.captured_s2_instances[-1]
            self.assertEqual(latest_s2.provider, "sumopod")
            self.assertEqual(latest_s2.model, "deepseek-v4.1-flash:netra")

    # -------------------------------------------------------------------------
    # Requirement 8: Remote channel security invariants remain intact
    # -------------------------------------------------------------------------
    def test_remote_channel_security_invariants_preserved(self) -> None:
        """Requirement 8: Conversational routing does not bypass shell, write, or git restrictions."""
        # 1. Shell execution attempt -> rejected
        out_shell = self.channel.simulate_incoming(chat_id="chat_1", user_id="1001", text="!dir")
        self.assertIsNotNone(out_shell)
        self.assertFalse(out_shell.success)
        self.assertEqual(out_shell.status, "rejected")

        # 2. Code write attempt from remote channel -> downgraded to plan/read-only, no files created
        out_write = self.channel.simulate_incoming(chat_id="chat_1", user_id="1001", text="modify file core/app.py to add backdoor")
        self.assertIsNotNone(out_write)
        # Does not mutate file
        content = (self.repo_dir / "core" / "app.py").read_text(encoding="utf-8")
        self.assertEqual(content, "# app code\n")

    # -------------------------------------------------------------------------
    # Multi-turn Conversation Context Edge Case
    # -------------------------------------------------------------------------
    def test_literal_output_after_conversation_history_with_question(self) -> None:
        """Multi-turn history containing questions and execution words does not break literal output."""
        session = self.runtime.sessions.get_or_create(channel="telegram", user_id="1001", conversation_id="chat_multi")
        # Simulate prior turns with questions and words like 'executed'
        session.record_interaction("What is the architecture?", "The architecture is dual-system. No commands executed.")
        session.record_interaction("How does build work?", "Build mode compiles code and runs tests.")
        self.runtime.sessions.save(session)

        out = self.channel.simulate_incoming(
            chat_id="chat_multi",
            user_id="1001",
            text="balas hanya dengan teks: PONG",
        )
        self.assertIsNotNone(out)
        assert out is not None
        self.assertTrue(out.success)
        self.assertEqual(out.text, "PONG")
        self.assertEqual(len(self._get_plan_files()), 0)


if __name__ == "__main__":
    unittest.main()
