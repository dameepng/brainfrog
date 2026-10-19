"""Comprehensive test suite for Telegram System 2 Model Switching.

Covers:
1. /model and /models output formatting without credential exposure
2. Successful switching and verification that subsequent requests use selected model
3. Unsupported provider and unsupported model handling with actionable errors
4. Missing credentials and Antigravity opt-in boundary failures
5. Fails closed: Failed switches preserve previous model configuration
6. Unauthorized Telegram user rejection at channel boundary
7. Narrowly scoped permission gate (denied permission enforcement)
8. Strict session isolation between multiple users / chats
9. Secret scrubbing in error messages
10. Remote channel security invariants remain intact (no shell, no code modification)
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

from core.channels.telegram import MockTelegramTransport, TelegramChannel
from core.runtime.messages import IncomingMessage, OutgoingMessage
from core.runtime.model_switcher import (
    SUPPORTED_SYSTEM2_PROVIDERS,
    check_provider_credentials,
    format_model_status_markdown,
    format_models_markdown,
    get_canonical_provider,
    get_default_model_for_provider,
    is_model_supported_for_provider,
    switch_session_model,
)
from core.runtime.permissions import ChannelTrustLevel, PermissionAction, PermissionPolicy, get_default_policy
from core.runtime.runtime import BrainFrogRuntime
from core.runtime.session import SessionManager
from system1.base import Answer, SystemOneClient


# =============================================================================
# Deterministic Test Doubles
# =============================================================================

class DeterministicSystem1Double(SystemOneClient):
    """Deterministic System 1 fake returning question_only decision."""
    name: str = "fake_s1"

    def __init__(self, default_change_type: str = "question_only") -> None:
        self.default_change_type = default_change_type
        self.call_count = 0
        self.decide_calls: List[Dict[str, Any]] = []

    def decide(self, state: Dict[str, Any], questions: Dict[str, Any]) -> Dict[str, Answer]:
        self.call_count += 1
        self.decide_calls.append({"state": state, "questions": questions})
        return {
            "likely_domain": Answer(choice="unrelated", confidence=0.60),
            "change_type": Answer(choice=self.default_change_type, confidence=0.95),
            "is_sensitive": Answer(noul=0.05, confidence=0.95),
            "complexity": Answer(score=0, confidence=0.90),
            "needs_tests": Answer(noul=0.05, confidence=0.95),
        }


class CapturingSystem2Client:
    """System 2 client double capturing model and provider passed at instantiation."""
    def __init__(self, model: Optional[str] = None, provider: Optional[str] = None, **kwargs: Any) -> None:
        self.model = model
        self.provider = provider
        self.call_history: List[str] = []

    def plan(self, *args: Any, **kwargs: Any) -> List[Any]:
        return []

    def plan_and_prd(self, task: str, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {
            "title": "Test Plan",
            "markdown_doc": f"# Plan for {task}",
            "problem": task,
            "goals": [task],
            "steps": [],
            "relevant_files": [],
        }

    def diagnose(self, task: str, *args: Any, **kwargs: Any) -> str:
        self.call_history.append(task)
        return f"Response from {self.provider}/{self.model}: Task analyzed."


def _synthetic_test_token(name: str = "KEY") -> str:
    """Return obviously fake synthetic token for test fixtures."""
    return f"synthetic_test_{name}_placeholder_value_12345"


def _synthetic_openai_key(seed: str = "SYNTHETIC_OPENAI") -> str:
    """Construct dynamic synthetic OpenAI-style key for redaction testing."""
    prefix = "".join(["s", "k", "-", "proj", "-"])
    return f"{prefix}{seed}_val_12345"


# =============================================================================
# Test Suite
# =============================================================================

class TestTelegramModelSwitching(unittest.TestCase):
    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="brainfrog_test_tg_model_")
        self.repo_dir = Path(self.test_dir)
        (self.repo_dir / ".git").mkdir()

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
            allowed_users=["1001", "2002"],
            rate_limit_seconds=0.0,
            transport=self.transport,
        )
        self.channel.set_handler(self.runtime.handle_message)

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # 1. /model and /models Output
    # -------------------------------------------------------------------------
    def test_slash_models_output_structure(self) -> None:
        """/models lists supported providers and model choices without exposing credentials."""
        out = self.channel.simulate_incoming(chat_id="chat_1", user_id="1001", text="/models")
        self.assertIsNotNone(out)
        self.assertTrue(out.success)
        self.assertIn("BrainFrog System 2 Model Catalog", out.text)
        self.assertIn("Claude (Anthropic API)", out.text)
        self.assertIn("Google Antigravity", out.text)
        self.assertIn("OpenAI", out.text)
        self.assertIn("claude-sonnet-5", out.text)
        self.assertIn("gemini-3.8-flash-high", out.text)
        self.assertIn("gpt-4o", out.text)
        self.assertIn("deepseek-chat", out.text)
        # Ensure no credential literals appear
        self.assertNotIn("sk-ant-", out.text)
        self.assertNotIn("sk-proj-", out.text)

    def test_slash_models_filter_by_provider(self) -> None:
        """/models <provider> filters specifically to that provider."""
        out = self.channel.simulate_incoming(chat_id="chat_1", user_id="1001", text="/models claude")
        self.assertIsNotNone(out)
        self.assertTrue(out.success)
        self.assertIn("Claude (Anthropic API)", out.text)
        self.assertIn("claude-sonnet-5", out.text)
        self.assertNotIn("OpenAI (GPT API)", out.text)

        # Invalid provider filter returns actionable error
        out_bad = self.channel.simulate_incoming(chat_id="chat_1", user_id="1001", text="/models non_existent_prov")
        self.assertIsNotNone(out_bad)
        self.assertIn("Unknown provider", out_bad.text)
        self.assertIn("Supported providers:", out_bad.text)

    def test_slash_model_status_reporting(self) -> None:
        """/model with no args accurately reports effective model, provider, and default scope."""
        out = self.channel.simulate_incoming(chat_id="chat_1", user_id="1001", text="/model")
        self.assertIsNotNone(out)
        self.assertTrue(out.success)
        self.assertIn("BrainFrog System 2 Model Status", out.text)
        self.assertIn("claude", out.text)
        self.assertIn("claude-sonnet-5", out.text)
        self.assertIn("(system default)", out.text)
        self.assertIn("Context Window:", out.text)

    # -------------------------------------------------------------------------
    # 2. Successful Switching & Subsequent Request Verification
    # -------------------------------------------------------------------------
    def test_successful_model_switch_and_subsequent_request(self) -> None:
        """Switching via /model <provider> <model_id> sets session override used by next task."""
        fake_key = _synthetic_test_token("ANTHROPIC")
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": fake_key}):
            # 1. Switch model
            out = self.channel.simulate_incoming(
                chat_id="chat_1",
                user_id="1001",
                text="/model claude claude-3-5-sonnet-20241022",
            )
            self.assertIsNotNone(out)
            self.assertTrue(out.success)
            self.assertIn("System 2 model switched for this session", out.text)
            self.assertIn("claude-3-5-sonnet-20241022", out.text)

            # 2. /model now reports session override
            out_status = self.channel.simulate_incoming(chat_id="chat_1", user_id="1001", text="/model")
            self.assertIsNotNone(out_status)
            assert out_status is not None
            self.assertIn("(session override)", out_status.text)
            self.assertIn("claude-3-5-sonnet-20241022", out_status.text)

            # 3. /status also reflects session override
            out_diag = self.channel.simulate_incoming(chat_id="chat_1", user_id="1001", text="/status")
            self.assertIsNotNone(out_diag)
            assert out_diag is not None
            self.assertIn("Provider: `claude` (session override)", out_diag.text)
            self.assertIn("Model: `claude-3-5-sonnet-20241022`", out_diag.text)

            # 4. Next prompt to runtime uses the selected model
            task_out = self.channel.simulate_incoming(
                chat_id="chat_1",
                user_id="1001",
                text="What is the architecture of this repo?",
            )
            self.assertIsNotNone(task_out)
            self.assertGreater(len(self.captured_s2_instances), 0)
            latest_s2 = self.captured_s2_instances[-1]
            self.assertEqual(latest_s2.provider, "claude")
            self.assertEqual(latest_s2.model, "claude-3-5-sonnet-20241022")

    def test_switch_model_slash_format_syntax(self) -> None:
        """Supports '/model <provider>/<model_id>' format seamlessly."""
        fake_key = _synthetic_test_token("ANTHROPIC")
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": fake_key}):
            out = self.channel.simulate_incoming(
                chat_id="chat_1",
                user_id="1001",
                text="/model claude/claude-3-5-haiku-20241022",
            )
            self.assertIsNotNone(out)
            self.assertTrue(out.success)
            self.assertIn("claude-3-5-haiku-20241022", out.text)

    def test_revert_model_to_system_default(self) -> None:
        """/model default clears session override and reverts to system default."""
        fake_key = _synthetic_test_token("ANTHROPIC")
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": fake_key}):
            # Set override
            self.channel.simulate_incoming(chat_id="chat_1", user_id="1001", text="/model claude claude-3-5-haiku-20241022")
            # Reset
            out_reset = self.channel.simulate_incoming(chat_id="chat_1", user_id="1001", text="/model default")
            self.assertIsNotNone(out_reset)
            self.assertTrue(out_reset.success)
            self.assertIn("Model override cleared", out_reset.text)

            # Verify /model is back to system default
            out_status = self.channel.simulate_incoming(chat_id="chat_1", user_id="1001", text="/model")
            self.assertIsNotNone(out_status)
            assert out_status is not None
            self.assertIn("(system default)", out_status.text)
            self.assertIn("claude-sonnet-5", out_status.text)

    # -------------------------------------------------------------------------
    # 3. Unsupported Provider, Model & Fails-Closed Verification
    # -------------------------------------------------------------------------
    def test_unsupported_provider_fails_closed(self) -> None:
        """Switching to an unsupported provider returns actionable error and preserves previous model."""
        session = self.runtime.sessions.get_or_create(channel="telegram", user_id="1001", conversation_id="chat_1")
        session.active_provider = "claude"
        session.active_model = "claude-sonnet-5"
        self.runtime.sessions.save(session)

        out = self.channel.simulate_incoming(
            chat_id="chat_1",
            user_id="1001",
            text="/model unknown_llm gpt-4o",
        )
        self.assertIsNotNone(out)
        self.assertFalse(out.success)
        self.assertIn("Unsupported provider 'unknown_llm'", out.text)
        self.assertIn("Supported providers:", out.text)

        # Invariant: session state was preserved
        refreshed = self.runtime.sessions.get(session.session_id)
        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed.active_provider, "claude")
        self.assertEqual(refreshed.active_model, "claude-sonnet-5")

    def test_unsupported_model_for_provider_fails_closed(self) -> None:
        """Switching to an unknown model returns actionable error and preserves previous model."""
        session = self.runtime.sessions.get_or_create(channel="telegram", user_id="1001", conversation_id="chat_1")
        session.active_provider = "claude"
        session.active_model = "claude-sonnet-5"
        self.runtime.sessions.save(session)

        out = self.channel.simulate_incoming(
            chat_id="chat_1",
            user_id="1001",
            text="/model claude non-existent-model-xyz",
        )
        self.assertIsNotNone(out)
        self.assertFalse(out.success)
        self.assertIn("Unsupported model `non-existent-model-xyz` for provider `claude`", out.text)
        self.assertIn("Use `/models claude`", out.text)

        # Invariant: session state preserved
        refreshed = self.runtime.sessions.get(session.session_id)
        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed.active_provider, "claude")
        self.assertEqual(refreshed.active_model, "claude-sonnet-5")

    def test_missing_credentials_fails_closed(self) -> None:
        """Attempting to switch to a provider with missing credentials returns error and preserves state."""
        session = self.runtime.sessions.get_or_create(channel="telegram", user_id="1001", conversation_id="chat_1")
        session.active_provider = "claude"
        session.active_model = "claude-sonnet-5"
        self.runtime.sessions.save(session)

        # Ensure OpenAI credentials are absent
        with patch.dict(os.environ, {"OPENAI_API_KEY": "", "BRAINFROG_MODEL_API_KEY": ""}, clear=False):
            out = self.channel.simulate_incoming(
                chat_id="chat_1",
                user_id="1001",
                text="/model openai gpt-4o",
            )
            self.assertIsNotNone(out)
            self.assertFalse(out.success)
            self.assertIn("Cannot switch to `openai`", out.text)
            self.assertIn("Switching aborted; previous session model configuration was preserved", out.text)

            # Invariant: session model untouched
            refreshed = self.runtime.sessions.get(session.session_id)
            self.assertIsNotNone(refreshed)
            self.assertEqual(refreshed.active_provider, "claude")
            self.assertEqual(refreshed.active_model, "claude-sonnet-5")

    def test_antigravity_opt_in_missing_fails_closed(self) -> None:
        """Antigravity switch fails closed if opt-in environment variable is not enabled."""
        session = self.runtime.sessions.get_or_create(channel="telegram", user_id="1001", conversation_id="chat_1")
        session.active_provider = "claude"
        session.active_model = "claude-sonnet-5"
        self.runtime.sessions.save(session)

        with patch("core.runtime.model_switcher.find_antigravity_bin", return_value="C:/bin/agy.exe"), \
             patch("os.path.exists", return_value=True), \
             patch("core.runtime.model_switcher.is_antigravity_opt_in_enabled", return_value=False):
            out = self.channel.simulate_incoming(
                chat_id="chat_1",
                user_id="1001",
                text="/model antigravity gemini-3.8-flash-high",
            )
            self.assertIsNotNone(out)
            self.assertFalse(out.success)
            self.assertIn("Antigravity requires explicit opt-in", out.text)

            # Invariant: session model preserved
            refreshed = self.runtime.sessions.get(session.session_id)
            self.assertIsNotNone(refreshed)
            self.assertEqual(refreshed.active_provider, "claude")
            self.assertEqual(refreshed.active_model, "claude-sonnet-5")

    def test_syntax_errors_actionable(self) -> None:
        """Invalid syntax returns clear usage guidance without mutating state."""
        # 1. Missing model ID (only 1 arg)
        out1 = self.channel.simulate_incoming(chat_id="chat_1", user_id="1001", text="/model claude")
        self.assertIsNotNone(out1)
        self.assertFalse(out1.success)
        self.assertIn("Missing model ID", out1.text)
        self.assertIn("`/model <provider> <model_id>`", out1.text)

        # 2. Too many arguments
        out2 = self.channel.simulate_incoming(chat_id="chat_1", user_id="1001", text="/model claude sonnet extra_arg")
        self.assertIsNotNone(out2)
        self.assertFalse(out2.success)
        self.assertIn("Expected `<provider> <model_id>`", out2.text)

    # -------------------------------------------------------------------------
    # 4. Security: Allowlist & Narrowly Scoped Permission Gate
    # -------------------------------------------------------------------------
    def test_unauthorized_telegram_user_rejected(self) -> None:
        """Unallowlisted Telegram user is rejected before reaching model switching logic."""
        out = self.channel.simulate_incoming(
            chat_id="attacker_chat",
            user_id="9999",  # Not in allowed_users: ["1001", "2002"]
            text="/model claude claude-3-5-sonnet-20241022",
        )
        self.assertIsNotNone(out)
        self.assertFalse(out.success)
        self.assertEqual(out.status, "rejected")
        self.assertIn("Akun Anda tidak terdaftar dalam allowlist", self.transport.sent_messages[0]["text"])

    def test_policy_permission_denial(self) -> None:
        """When CHANGE_MODEL is stripped from policy, model switching is rejected with permission error."""
        # Create policy without CHANGE_MODEL
        restricted_policy = PermissionPolicy(
            channel="telegram",
            trust_level=ChannelTrustLevel.REMOTE_CHANNEL.value,
            allowed_actions={PermissionAction.READ_CODE.value, PermissionAction.PLAN.value},
            allow_code_edits=False,
        )
        custom_runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            policy_provider=lambda ch: restricted_policy,
            persist_sessions=False,
        )
        ch = TelegramChannel(
            bot_token=_synthetic_test_token("BOT"),
            allowed_users=["1001"],
            transport=self.transport,
        )
        ch.set_handler(custom_runtime.handle_message)

        out = ch.simulate_incoming(
            chat_id="chat_1",
            user_id="1001",
            text="/model claude claude-3-5-sonnet-20241022",
        )
        self.assertIsNotNone(out)
        self.assertFalse(out.success)
        self.assertIn("Permission Denied", out.text)
        self.assertIn("not permitted by policy", out.text)

    # -------------------------------------------------------------------------
    # 5. Strict Session Isolation
    # -------------------------------------------------------------------------
    def test_strict_session_isolation(self) -> None:
        """Switching model in one Telegram session does not affect another user's session."""
        fake_key = _synthetic_test_token("ANTHROPIC")
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": fake_key}):
            # User 1001 switches to claude-3-5-haiku
            out_u1 = self.channel.simulate_incoming(
                chat_id="chat_1001",
                user_id="1001",
                text="/model claude claude-3-5-haiku-20241022",
            )
            self.assertIsNotNone(out_u1)
            assert out_u1 is not None
            self.assertTrue(out_u1.success)

            # User 2002 checks model -> still on system default (claude-sonnet-5)
            out_u2 = self.channel.simulate_incoming(
                chat_id="chat_2002",
                user_id="2002",
                text="/model",
            )
            self.assertIsNotNone(out_u2)
            assert out_u2 is not None
            self.assertTrue(out_u2.success)
            self.assertIn("(system default)", out_u2.text)
            self.assertIn("claude-sonnet-5", out_u2.text)
            self.assertNotIn("claude-3-5-haiku-20241022", out_u2.text)

            # User 2002 switches to claude-3-7-sonnet
            out_u2_switch = self.channel.simulate_incoming(
                chat_id="chat_2002",
                user_id="2002",
                text="/model claude claude-3-7-sonnet-20250219",
            )
            self.assertIsNotNone(out_u2_switch)
            assert out_u2_switch is not None
            self.assertTrue(out_u2_switch.success)

            # Verify User 1001 is still on claude-3-5-haiku
            out_u1_check = self.channel.simulate_incoming(
                chat_id="chat_1001",
                user_id="1001",
                text="/model",
            )
            self.assertIsNotNone(out_u1_check)
            assert out_u1_check is not None
            self.assertIn("claude-3-5-haiku-20241022", out_u1_check.text)
            self.assertNotIn("claude-3-7-sonnet-20250219", out_u1_check.text)

    # -------------------------------------------------------------------------
    # 6. Secret Redaction
    # -------------------------------------------------------------------------
    def test_secret_scrubbing_in_error_messages(self) -> None:
        """Secrets appearing in low-level error strings are redacted before delivery."""
        sensitive_string = _synthetic_openai_key("REDACT_TEST")
        policy = get_default_policy("telegram")
        session = self.runtime.sessions.get_or_create(channel="telegram", user_id="1001", conversation_id="chat_1")

        with patch("core.runtime.model_switcher.check_provider_credentials", return_value=(False, f"Failed with {sensitive_string}")):
            ok, msg = switch_session_model(
                session=session,
                raw_args="openai gpt-4o",
                policy=policy,
            )
            self.assertFalse(ok)
            self.assertNotIn(sensitive_string, msg)
            self.assertIn("[REDACTED", msg)

    # -------------------------------------------------------------------------
    # 7. Remote-Channel Security Invariant Preservation
    # -------------------------------------------------------------------------
    def test_remote_channel_security_invariants_preserved(self) -> None:
        """Model switching capability does not enable shell execution or code modifications."""
        fake_key = _synthetic_test_token("ANTHROPIC")
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": fake_key}):
            # 1. Switch model successfully
            self.channel.simulate_incoming(chat_id="chat_1", user_id="1001", text="/model claude claude-3-5-sonnet-20241022")

            # 2. Attempt shell execution via ! command -> strictly prohibited
            out_shell = self.channel.simulate_incoming(chat_id="chat_1", user_id="1001", text="!pytest")
            self.assertIsNotNone(out_shell)
            self.assertFalse(out_shell.success)
            self.assertEqual(out_shell.status, "rejected")
            self.assertIn("strictly prohibited for remote channel", out_shell.error or out_shell.text)

            # 3. Code modification from remote channel without approval -> restricted / read-only
            build_runtime = BrainFrogRuntime(
                repo_dir=self.repo_dir,
                default_backend="typesafe",
                default_provider="claude",
                default_model="claude-sonnet-5",
                system1_factory=lambda b: DeterministicSystem1Double(default_change_type="build"),
                system2_factory=self.s2_factory,
                persist_sessions=False,
            )
            ch_build = TelegramChannel(
                bot_token=_synthetic_test_token("BOT"),
                allowed_users=["1001"],
                transport=self.transport,
            )
            ch_build.set_handler(build_runtime.handle_message)
            out_edit = ch_build.simulate_incoming(chat_id="chat_1", user_id="1001", text="Build a web page and verify UI using browser")
            self.assertIsNotNone(out_edit)
            # Default remote channel mode is downgraded to plan/read-only
            self.assertEqual(out_edit.metadata.get("mode"), "plan")
            self.assertFalse((self.repo_dir / "index.html").exists())

    # -------------------------------------------------------------------------
    # 8. Regression Tests: Slash-Command Routing & Punctuation Invariants
    # -------------------------------------------------------------------------
    def test_models_sumopod_never_falls_through_to_system1_clarification(self) -> None:
        """Sending /models sumopod directly returns model catalog and never triggers System 1 clarification."""
        fake_key = _synthetic_test_token("SUMOPOD")
        with patch.dict(os.environ, {"SUMOPOD_API_KEY": fake_key}):
            out = self.channel.simulate_incoming(
                chat_id="chat_sumo_1",
                user_id="1001",
                text="/models sumopod",
            )
            self.assertIsNotNone(out)
            assert out is not None
            self.assertTrue(out.success)
            self.assertEqual(out.status, "completed")
            # Invariant: System 1 must NOT be called for slash-command inspection
            self.assertEqual(self.fake_s1.call_count, 0)
            self.assertIn("BrainFrog System 2 Model Catalog", out.text)
            self.assertIn("Sumopod AI", out.text)
            self.assertIn("deepseek-chat", out.text)
            self.assertIn("deepseek-reasoner", out.text)
            self.assertIn("deepseek-v4.1-flash:netra", out.text)
            self.assertIn("✅ Ready", out.text)
            self.assertNotIn("clarification", out.text.lower())
            self.assertNotIn("not confident enough", out.text.lower())

        # Also verify when credentials are missing: returns informative catalog with status badge, never S1
        self.fake_s1.call_count = 0
        with patch.dict(os.environ, {}, clear=True):
            out_unconfigured = self.channel.simulate_incoming(
                chat_id="chat_sumo_1",
                user_id="1001",
                text="/models sumopod",
            )
            self.assertIsNotNone(out_unconfigured)
            assert out_unconfigured is not None
            self.assertTrue(out_unconfigured.success)
            self.assertEqual(self.fake_s1.call_count, 0)
            self.assertIn("Sumopod AI", out_unconfigured.text)
            self.assertIn("⚠️ SUMOPOD_API_KEY is not configured", out_unconfigured.text)
            self.assertNotIn("clarification", out_unconfigured.text.lower())

    def test_model_switching_with_punctuation_and_bot_mentions(self) -> None:
        """Commands with bot mentions, colons, dots, hyphens, and slashes never fall through to System 1."""
        sumo_key = _synthetic_test_token("SUMOPOD")
        or_key = _synthetic_test_token("OPENROUTER")
        with patch.dict(os.environ, {"SUMOPOD_API_KEY": sumo_key, "OPENROUTER_API_KEY": or_key}):
            # 1. Telegram bot mention syntax: /models@BrainFrogBot sumopod
            self.fake_s1.call_count = 0
            out_mention = self.channel.simulate_incoming(
                chat_id="chat_punc",
                user_id="1001",
                text="/models@BrainFrogBot sumopod",
            )
            self.assertIsNotNone(out_mention)
            assert out_mention is not None
            self.assertTrue(out_mention.success)
            self.assertEqual(self.fake_s1.call_count, 0)
            self.assertIn("Sumopod AI", out_mention.text)

            # 2. Punctuation in model ID: deepseek-v4.1-flash:netra (contains dots, hyphens, colon)
            self.fake_s1.call_count = 0
            out_sw = self.channel.simulate_incoming(
                chat_id="chat_punc",
                user_id="1001",
                text="/model@BrainFrogBot sumopod deepseek-v4.1-flash:netra",
            )
            self.assertIsNotNone(out_sw)
            assert out_sw is not None
            self.assertTrue(out_sw.success)
            self.assertEqual(self.fake_s1.call_count, 0)
            self.assertIn("deepseek-v4.1-flash:netra", out_sw.text)
            session = self.runtime.sessions.get("telegram:1001:chat_punc")
            assert session is not None
            self.assertEqual(session.active_provider, "sumopod")
            self.assertEqual(session.active_model, "deepseek-v4.1-flash:netra")

            # 3. Model ID with slashes, dots, and hyphens: anthropic/claude-3.5-sonnet
            self.fake_s1.call_count = 0
            out_or = self.channel.simulate_incoming(
                chat_id="chat_punc",
                user_id="1001",
                text="/model openrouter anthropic/claude-3.5-sonnet",
            )
            self.assertIsNotNone(out_or)
            assert out_or is not None
            self.assertTrue(out_or.success)
            self.assertEqual(self.fake_s1.call_count, 0)
            self.assertEqual(session.active_provider, "openrouter")
            self.assertEqual(session.active_model, "anthropic/claude-3.5-sonnet")

            # 4. Slash delimiter syntax: /model sumopod/deepseek-chat
            self.fake_s1.call_count = 0
            out_slash = self.channel.simulate_incoming(
                chat_id="chat_punc",
                user_id="1001",
                text="/model sumopod/deepseek-chat",
            )
            self.assertIsNotNone(out_slash)
            assert out_slash is not None
            self.assertTrue(out_slash.success)
            self.assertEqual(self.fake_s1.call_count, 0)
            self.assertEqual(session.active_provider, "sumopod")
            self.assertEqual(session.active_model, "deepseek-chat")

            # 5. Trailing punctuation in provider argument: /models sumopod: and /model sumopod: deepseek-reasoner
            self.fake_s1.call_count = 0
            out_colon_cat = self.channel.simulate_incoming(
                chat_id="chat_punc",
                user_id="1001",
                text="/models sumopod:",
            )
            self.assertIsNotNone(out_colon_cat)
            assert out_colon_cat is not None
            self.assertTrue(out_colon_cat.success)
            self.assertEqual(self.fake_s1.call_count, 0)
            self.assertIn("Sumopod AI", out_colon_cat.text)

            out_colon_sw = self.channel.simulate_incoming(
                chat_id="chat_punc",
                user_id="1001",
                text="/model sumopod: deepseek-reasoner",
            )
            self.assertIsNotNone(out_colon_sw)
            assert out_colon_sw is not None
            self.assertTrue(out_colon_sw.success)
            self.assertEqual(self.fake_s1.call_count, 0)
            self.assertEqual(session.active_model, "deepseek-reasoner")

            # 6. Provider with hyphen: openai-compatible
            self.fake_s1.call_count = 0
            out_hyphen_prov = self.channel.simulate_incoming(
                chat_id="chat_punc",
                user_id="1001",
                text="/models openai-compatible",
            )
            self.assertIsNotNone(out_hyphen_prov)
            assert out_hyphen_prov is not None
            self.assertTrue(out_hyphen_prov.success)
            self.assertEqual(self.fake_s1.call_count, 0)
            self.assertIn("OpenAI-Compatible", out_hyphen_prov.text)

    def test_permission_denial_for_both_model_and_models(self) -> None:
        """When CHANGE_MODEL permission is not granted, /model and /models return deterministic denial."""
        denied_policy = PermissionPolicy(
            channel="telegram",
            trust_level=ChannelTrustLevel.REMOTE_CHANNEL.value,
            allowed_actions={PermissionAction.READ_CODE.value, PermissionAction.PLAN.value},
            allow_code_edits=False,
            allow_auto_pr=False,
        )
        with patch.object(self.runtime, "policy_provider", return_value=denied_policy):
            # 1. /models denied
            self.fake_s1.call_count = 0
            out_models = self.channel.simulate_incoming(chat_id="chat_denied", user_id="1001", text="/models")
            self.assertIsNotNone(out_models)
            assert out_models is not None
            self.assertFalse(out_models.success)
            self.assertEqual(out_models.status, "rejected")
            self.assertIn("Permission Denied", out_models.text)
            self.assertEqual(self.fake_s1.call_count, 0)

            # 2. /model denied
            out_model = self.channel.simulate_incoming(chat_id="chat_denied", user_id="1001", text="/model")
            self.assertIsNotNone(out_model)
            assert out_model is not None
            self.assertFalse(out_model.success)
            self.assertEqual(out_model.status, "rejected")
            self.assertIn("Permission Denied", out_model.text)
            self.assertEqual(self.fake_s1.call_count, 0)

            # 3. /model switch denied
            out_switch = self.channel.simulate_incoming(chat_id="chat_denied", user_id="1001", text="/model sumopod deepseek-chat")
            self.assertIsNotNone(out_switch)
            assert out_switch is not None
            self.assertFalse(out_switch.success)
            self.assertEqual(out_switch.status, "rejected")
            self.assertIn("Permission Denied", out_switch.text)
            self.assertEqual(self.fake_s1.call_count, 0)

    def test_unknown_slash_command_preserves_safe_behavior(self) -> None:
        """Unknown slash command does not execute arbitrary code or shell, preserving safety."""
        out_unknown = self.channel.simulate_incoming(
            chat_id="chat_unknown",
            user_id="1001",
            text="/unknown_command_test",
        )
        self.assertIsNotNone(out_unknown)
        assert out_unknown is not None
        # Does not mutate workspace or execute shell
        self.assertFalse((self.repo_dir / "index.html").exists())
        self.assertFalse((self.repo_dir / "test.txt").exists())

    def test_existing_cli_behavior_preserved(self) -> None:
        """Canonical provider lookup and model defaults preserve existing CLI compatibility."""
        self.assertEqual(get_canonical_provider("claude"), "claude")
        self.assertEqual(get_canonical_provider("sumopod"), "sumopod")
        self.assertEqual(get_canonical_provider("openai-compatible"), "openai-compatible")
        self.assertEqual(get_canonical_provider("anthropic"), "claude")
        self.assertEqual(get_default_model_for_provider("sumopod"), "deepseek-chat")
        self.assertEqual(get_default_model_for_provider("claude"), "claude-sonnet-5")


if __name__ == "__main__":
    unittest.main()
