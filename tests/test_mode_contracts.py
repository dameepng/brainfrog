"""Comprehensive test suite for BrainFrog Phase 1 Explicit Chat / Plan / Build Mode Contract.

Covers:
1. Mode Validation & Normalization
   - Valid canonical modes: 'chat', 'plan', 'build'
   - Case & whitespace normalization ('  CHAT  ' -> 'chat', etc.)
   - Explicit rejection of invalid modes ('super_admin', 'execute', etc.)
   - Safe migration of legacy session data missing active_mode
   - Safe handling of invalid persisted mode values

2. Session Isolation & Persistence
   - Switching mode persists across session reloads
   - Two sessions maintain independent modes without cross-talk
   - Switching mode does not change active_provider or active_model
   - Existing session fields (history, metadata) remain intact

3. User Command Interface (/mode)
   - /mode with no arguments returns current mode and supported options
   - /mode <target> validates, updates session, and persists
   - Invalid mode arguments are rejected with clear usage guidance
   - Command handling is deterministic and does not invoke LLM

4. Routing Contracts
   - Chat mode routes conversational requests through diagnose() without plan artifacts
   - Chat mode literal-output returns literal string without plan artifacts
   - Plan mode routes planning requests to plan_and_prd() and persists under .brainfrog/plans/
   - Plan mode conversational questions do not accidentally create plans
   - Build mode returns bounded not-yet-implemented response in Phase 1

5. Security & Permission Boundaries
   - Mode != Permission: modes never grant permissions by themselves
   - Remote channel restrictions (shell, arbitrary code write) remain strictly enforced in all modes
   - Unknown modes cannot fall back to a permissive mode
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

from core.plans import (
    DEFAULT_MODE_LOCAL,
    DEFAULT_MODE_REMOTE,
    MODE_BUILD,
    MODE_CHAT,
    MODE_PLAN,
    VALID_MODES,
    PlanRegistry,
    PlanStatus,
    get_plans_dir,
    normalize_mode,
    recover_plan_journal,
    save_plan_document,
)
from core.runtime.approval import ApprovalService, ApprovalStatus, CanonicalOperation, FileApprovalStore
from core.runtime.capabilities import Capabilities, FilesystemPolicy, NetworkPolicy
from core.runtime.contract import ApprovedExecutionContract
from core.runtime.messages import IncomingMessage, OutgoingMessage
from core.runtime.permissions import (
    ChannelTrustLevel,
    PermissionAction,
    PermissionPolicy,
    classify_request_action,
    evaluate_channel_action,
    get_default_policy,
)
from core.runtime.runtime import BrainFrogRuntime
from core.runtime.session import CURRENT_SESSION_SCHEMA_VERSION, FileSessionStore, SessionManager, SessionState
from orchestrator import (
    Orchestrator,
    PlanStep,
    RunConfig,
    StepResult,
    _write_files,
    extract_literal_output,
    is_explicit_planning_request,
    is_question_task,
)
from system1.base import Answer, SystemOneClient


def _synthetic_test_token(name: str = "KEY") -> str:
    """Return obviously fake synthetic token for test fixtures."""
    return f"synthetic_test_{name}_placeholder_value_12345"


class CapturingSystem2Client:
    """Mock System 2 client capturing calls."""

    provider_name = "claude"
    model = "claude-sonnet-5"

    def __init__(self, model: Optional[str] = None, provider: Optional[str] = None, **kwargs: Any) -> None:
        self.model = model or "claude-sonnet-5"
        self.provider = provider or "claude"
        self.diagnose_calls: List[str] = []
        self.plan_and_prd_calls: List[str] = []
        self.plan_generator = None
        self.planned_files: List[str] = ["auth_service.py"]

    def plan_and_prd(self, task: str, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        self.plan_and_prd_calls.append(task)
        if self.plan_generator is not None:
            return self.plan_generator(task, *args, **kwargs)
        return {
            "title": "Database Migration Plan",
            "markdown_doc": f"# Rencana Implementasi: {task}\n\n## Langkah\n1. Setup...",
            "problem": task,
            "goals": [task],
            "steps": [{"id": "1", "description": "Migrate schema", "files": ["schema.sql"]}],
            "relevant_files": ["schema.sql"],
        }

    def diagnose(self, task: str, *args: Any, **kwargs: Any) -> str:
        self.diagnose_calls.append(task)
        return f"Diagnosed answer for: {task}"

    def plan_task(self, task: str, *args: Any, **kwargs: Any) -> List[PlanStep]:
        files = list(self.planned_files)
        return [PlanStep("1", "Implementation step", files)]

    def write_code(self, step: Any, task: str, file_contents: Any, *args: Any, **kwargs: Any) -> Dict[str, str]:
        files = step.files if (step and getattr(step, "files", None)) else list(self.planned_files)
        return {f: "class AuthService:\n    pass\n" for f in files}

    def review_and_fix(self, task: str, step: Any, file_contents: Any, test_output: Any, *args: Any, **kwargs: Any) -> Dict[str, str]:
        files = step.files if (step and getattr(step, "files", None)) else list(self.planned_files)
        return {f: "class AuthService:\n    pass\n" for f in files}


class MockSystem1Client(SystemOneClient):
    """Deterministic System 1 double for testing."""

    name: str = "mock-s1"

    def __init__(self, change_type: str = "question_only", confidence: float = 0.90) -> None:
        self._change_type = change_type
        self._confidence = confidence

    def decide(self, state: Dict[str, Any], questions: Dict[str, Any]) -> Dict[str, Answer]:
        out = {}
        for q_name in questions:
            if "change_type" in q_name or "CHANGE_TYPE" in str(q_name):
                out[q_name] = Answer(self._change_type, 0.95)
            else:
                out[q_name] = Answer("unrelated", self._confidence)
        return out


class TestModeValidation(unittest.TestCase):
    """Tests for mode canonicalization, validation, and migration."""

    def test_valid_canonical_modes(self) -> None:
        self.assertEqual(VALID_MODES, {"chat", "plan", "build"})
        self.assertEqual(normalize_mode("chat"), "chat")
        self.assertEqual(normalize_mode("plan"), "plan")
        self.assertEqual(normalize_mode("build"), "build")

    def test_case_and_whitespace_normalization(self) -> None:
        self.assertEqual(normalize_mode("  CHAT  "), "chat")
        self.assertEqual(normalize_mode("Plan\n"), "plan")
        self.assertEqual(normalize_mode("\tBUILD\t"), "build")
        self.assertEqual(normalize_mode("Chat"), "chat")
        self.assertEqual(normalize_mode("BUILD"), "build")

    def test_invalid_modes_rejected(self) -> None:
        self.assertIsNone(normalize_mode("super_admin"))
        self.assertIsNone(normalize_mode("execute"))
        self.assertIsNone(normalize_mode("run"))
        self.assertIsNone(normalize_mode(""))
        self.assertIsNone(normalize_mode("   "))
        self.assertIsNone(normalize_mode(None))
        self.assertIsNone(normalize_mode(123))  # type: ignore

    def test_missing_mode_in_old_session_migrates_safely(self) -> None:
        # Remote telegram session defaults to 'plan'
        remote_legacy = {
            "schema_version": CURRENT_SESSION_SCHEMA_VERSION,
            "session_id": "telegram:user_1:conv_1",
            "channel": "telegram",
            "user_id": "user_1",
            "conversation_id": "conv_1",
        }
        sess_remote = SessionState.from_dict(remote_legacy)
        self.assertEqual(sess_remote.active_mode, "plan")

        # Local CLI session defaults to 'build'
        cli_legacy = {
            "schema_version": CURRENT_SESSION_SCHEMA_VERSION,
            "session_id": "cli:user_1:conv_1",
            "channel": "cli",
            "user_id": "user_1",
            "conversation_id": "conv_1",
        }
        sess_cli = SessionState.from_dict(cli_legacy)
        self.assertEqual(sess_cli.active_mode, "build")

    def test_invalid_persisted_mode_handling(self) -> None:
        corrupt_data = {
            "schema_version": CURRENT_SESSION_SCHEMA_VERSION,
            "session_id": "telegram:user_1:conv_1",
            "channel": "telegram",
            "user_id": "user_1",
            "conversation_id": "conv_1",
            "active_mode": "unauthorized_privileged_mode",
        }
        # Does not crash or grant unauthorized mode; falls back safely to 'plan'
        sess = SessionState.from_dict(corrupt_data)
        self.assertEqual(sess.active_mode, "plan")


class TestSessionIsolationAndPersistence(unittest.TestCase):
    """Tests verifying session isolation and persistence across reloads."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="bf_mode_sess_")
        self.store = FileSessionStore(self.temp_dir)
        self.manager = SessionManager(store=self.store)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_switching_mode_persists_across_reloads(self) -> None:
        sess = self.manager.get_or_create(channel="telegram", user_id="u1", conversation_id="c1")
        self.assertEqual(sess.active_mode, "plan")

        sess.active_mode = "chat"
        self.manager.save(sess)

        # Create brand-new manager with same store
        new_manager = SessionManager(store=self.store)
        reloaded = new_manager.get_or_create(channel="telegram", user_id="u1", conversation_id="c1")
        self.assertEqual(reloaded.active_mode, "chat")

        reloaded.active_mode = "build"
        new_manager.save(reloaded)

        reloaded_again = self.store.load(sess.session_id)
        self.assertIsNotNone(reloaded_again)
        self.assertEqual(reloaded_again.active_mode, "build")

    def test_two_sessions_hold_different_modes(self) -> None:
        sess_a = self.manager.get_or_create(channel="telegram", user_id="user_a", conversation_id="conv_a")
        sess_b = self.manager.get_or_create(channel="telegram", user_id="user_b", conversation_id="conv_b")

        sess_a.active_mode = "plan"
        sess_b.active_mode = "chat"
        self.manager.save(sess_a)
        self.manager.save(sess_b)

        reloaded_a = self.store.load(sess_a.session_id)
        reloaded_b = self.store.load(sess_b.session_id)

        self.assertIsNotNone(reloaded_a)
        self.assertIsNotNone(reloaded_b)
        self.assertEqual(reloaded_a.active_mode, "plan")
        self.assertEqual(reloaded_b.active_mode, "chat")

    def test_switching_mode_preserves_provider_model_and_history(self) -> None:
        sess = self.manager.get_or_create(channel="telegram", user_id="u1", conversation_id="c1")
        sess.active_provider = "sumopod"
        sess.active_model = "deepseek-v4.1-flash:netra"
        sess.record_interaction("User query", "Agent answer")
        self.manager.save(sess)

        # Switch mode
        sess.active_mode = "plan"
        self.manager.save(sess)

        reloaded = self.store.load(sess.session_id)
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.active_mode, "plan")
        self.assertEqual(reloaded.active_provider, "sumopod")
        self.assertEqual(reloaded.active_model, "deepseek-v4.1-flash:netra")
        self.assertEqual(len(reloaded.history), 1)
        self.assertEqual(reloaded.history[0]["user"], "User query")


class TestModeCommandsAndRuntime(unittest.TestCase):
    """Tests for the /mode slash command interface in BrainFrogRuntime."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="bf_mode_cmd_")
        self.store = FileSessionStore(self.temp_dir)
        self.sessions = SessionManager(store=self.store)
        self.mock_s2 = CapturingSystem2Client()
        self.runtime = BrainFrogRuntime(
            repo_dir=Path(self.temp_dir),
            sessions=self.sessions,
            system1_factory=lambda b: MockSystem1Client(),
            system2_factory=lambda model, provider: self.mock_s2,
            require_approval=False,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_mode_status_no_args(self) -> None:
        msg = IncomingMessage(
            id="msg_1",
            channel="telegram",
            user_id="user_1",
            conversation_id="conv_1",
            text="/mode",
        )
        resp = self.runtime.handle_message(msg)
        self.assertTrue(resp.success)
        self.assertIn("Active Mode:", resp.text)
        self.assertIn("chat", resp.text)
        self.assertIn("plan", resp.text)
        self.assertIn("build", resp.text)

    def test_mode_switch_to_plan(self) -> None:
        msg = IncomingMessage(
            id="msg_1",
            channel="telegram",
            user_id="user_1",
            conversation_id="conv_1",
            text="/mode plan",
        )
        resp = self.runtime.handle_message(msg)
        self.assertTrue(resp.success)
        self.assertIn("Mode switched to `plan`", resp.text)

        sess = self.sessions.get("telegram:user_1:conv_1")
        self.assertIsNotNone(sess)
        self.assertEqual(sess.active_mode, "plan")

    def test_mode_switch_invalid_mode_rejected(self) -> None:
        msg = IncomingMessage(
            id="msg_1",
            channel="telegram",
            user_id="user_1",
            conversation_id="conv_1",
            text="/mode invalid_mode",
        )
        resp = self.runtime.handle_message(msg)
        self.assertFalse(resp.success)
        self.assertIn("Unsupported mode 'invalid_mode'", resp.text)
        self.assertIn("Usage: `/mode <chat|plan|build>`", resp.text)

        sess = self.sessions.get("telegram:user_1:conv_1")
        self.assertIsNotNone(sess)
        self.assertEqual(sess.active_mode, "plan")  # Unchanged

    def test_mode_switch_is_inexpensive_no_llm_call(self) -> None:
        msg = IncomingMessage(
            id="msg_1",
            channel="telegram",
            user_id="user_1",
            conversation_id="conv_1",
            text="/mode plan",
        )
        self.runtime.handle_message(msg)
        self.assertEqual(len(self.mock_s2.diagnose_calls), 0)
        self.assertEqual(len(self.mock_s2.plan_and_prd_calls), 0)

    def test_invalid_metadata_mode_emits_event_and_falls_back(self) -> None:
        """Invalid metadata modes fall back safely and are observable via agent events."""
        msg = IncomingMessage(
            id="msg_meta_bad",
            channel="telegram",
            user_id="user_1",
            conversation_id="conv_1",
            text="/mode",
            metadata={"mode": "super_admin"},
        )
        resp = self.runtime.handle_message(msg)

        self.assertTrue(resp.success)
        self.assertIn("Active Mode:", resp.text)
        sess = self.sessions.get("telegram:user_1:conv_1")
        self.assertIsNotNone(sess)
        self.assertEqual(sess.active_mode, "plan")  # Session default preserved

        event_types = [e.type for e in (resp.events or [])]
        self.assertIn("agent.mode.invalid_metadata", event_types)

    def test_local_cli_build_without_contract_fails_closed(self) -> None:
        """Runtime-level R1 regression: CLI Build turn without a contract is bounded."""
        save_plan_document(
            Path(self.temp_dir),
            "20260101_todo.md",
            "# Todo Plan",
            metadata={
                "title": "Todo Plan",
                "goal": "Build a todo app",
                "steps": [{"id": "1", "description": "Create index.html", "files": ["index.html"]}],
            },
        )
        msg = IncomingMessage(
            id="msg_build_1",
            channel="cli",
            user_id="local",
            conversation_id="default",
            text="execute plan",
            metadata={"mode": "build"},
        )
        resp = self.runtime.handle_message(msg)

        self.assertTrue(resp.success)
        self.assertEqual(resp.status, "completed")
        self.assertIn(
            "Build mode is defined, but plan selection and controlled execution are not implemented yet",
            resp.text,
        )
        self.assertEqual(len(self.mock_s2.plan_and_prd_calls), 0)
        self.assertEqual(len(self.mock_s2.diagnose_calls), 0)
        self.assertFalse((Path(self.temp_dir) / "index.html").exists())


class TestRoutingContracts(unittest.TestCase):
    """Tests verifying behavior of Chat, Plan, and Build mode routing contracts."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="bf_routing_contracts_")
        self.repo_dir = Path(self.temp_dir)
        (self.repo_dir / "README.md").write_text("# Test Repo\n", encoding="utf-8")
        self.s2 = CapturingSystem2Client()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_chat_mode_question_does_not_generate_plan_artifact(self) -> None:
        cfg = RunConfig(
            repo_dir=self.repo_dir,
            task="Bagaimana arsitektur proyek ini?",
            mode="chat",
        )
        orch = Orchestrator(MockSystem1Client(change_type="question_only"), self.s2, cfg)
        results = orch.run()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].outcome, "diagnosed")
        self.assertEqual(len(self.s2.diagnose_calls), 1)
        self.assertEqual(len(self.s2.plan_and_prd_calls), 0)

        # Verify zero plans created
        plans_dir = get_plans_dir(self.repo_dir)
        self.assertFalse(plans_dir.exists())

    def test_chat_mode_literal_output_fast_path(self) -> None:
        cfg = RunConfig(
            repo_dir=self.repo_dir,
            task="balas hanya dengan teks: PONG",
            mode="chat",
        )
        orch = Orchestrator(MockSystem1Client(), self.s2, cfg)
        results = orch.run()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].outcome, "diagnosed")
        self.assertEqual(results[0].detail, "PONG")
        self.assertEqual(len(self.s2.diagnose_calls), 0)
        self.assertEqual(len(self.s2.plan_and_prd_calls), 0)

        plans_dir = get_plans_dir(self.repo_dir)
        self.assertFalse(plans_dir.exists())

    def test_plan_mode_generates_and_persists_plan(self) -> None:
        cfg = RunConfig(
            repo_dir=self.repo_dir,
            task="Buat rencana migrasi database ke PostgreSQL",
            mode="plan",
        )
        orch = Orchestrator(MockSystem1Client(change_type="feature_request"), self.s2, cfg)
        results = orch.run()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].outcome, "planned")
        self.assertEqual(len(self.s2.plan_and_prd_calls), 1)

        # Verify plan file saved in .brainfrog/plans/
        plans_dir = get_plans_dir(self.repo_dir)
        self.assertTrue(plans_dir.exists())
        plans = list(plans_dir.glob("*.md"))
        self.assertTrue(len(plans) >= 1)

    def test_plan_mode_conversational_question_does_not_generate_plan(self) -> None:
        cfg = RunConfig(
            repo_dir=self.repo_dir,
            task="Jelaskan struktur direktori project ini",
            mode="plan",
        )
        orch = Orchestrator(MockSystem1Client(change_type="question_only"), self.s2, cfg)
        results = orch.run()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].outcome, "diagnosed")
        self.assertEqual(len(self.s2.diagnose_calls), 1)
        self.assertEqual(len(self.s2.plan_and_prd_calls), 0)

        # Verify zero plans created
        plans_dir = get_plans_dir(self.repo_dir)
        self.assertFalse(plans_dir.exists())

    def test_build_mode_bounded_entry_point_not_implemented_in_phase_1(self) -> None:
        cfg = RunConfig(
            repo_dir=self.repo_dir,
            task="Implementasikan endpoint pembayaran",
            mode="build",
        )
        orch = Orchestrator(MockSystem1Client(change_type="feature_request"), self.s2, cfg)
        results = orch.run()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].outcome, "not_implemented")
        self.assertIn("Build mode is defined, but plan selection and controlled execution are not implemented yet", results[0].detail)
        # Verify no files were mutated
        self.assertEqual(len(self.s2.plan_and_prd_calls), 0)

    def test_build_mode_keyword_with_saved_plan_fails_closed_without_contract(self) -> None:
        """R1 regression: keyword triggers + saved plan must not auto-select or execute."""
        save_plan_document(
            self.repo_dir,
            "20260101_todo.md",
            "# Todo Plan",
            metadata={
                "title": "Todo Plan",
                "goal": "Build a todo app",
                "steps": [{"id": "1", "description": "Create index.html", "files": ["index.html"]}],
                "relevant_files": ["index.html"],
            },
        )

        for trigger in ("execute from plans.", "execute plan", "jalankan plan", "lanjutkan", "gass"):
            cfg = RunConfig(repo_dir=self.repo_dir, task=trigger, mode="build")
            orch = Orchestrator(MockSystem1Client(change_type="feature_request"), self.s2, cfg)
            results = orch.run()

            self.assertEqual(len(results), 1, trigger)
            self.assertEqual(results[0].outcome, "not_implemented", trigger)
            self.assertIn(
                "Build mode is defined, but plan selection and controlled execution are not implemented yet",
                results[0].detail,
                trigger,
            )

        # No planning, diagnosis, or mutation may be attempted from the saved plan
        self.assertEqual(len(self.s2.plan_and_prd_calls), 0)
        self.assertEqual(len(self.s2.diagnose_calls), 0)
        self.assertFalse((self.repo_dir / "index.html").exists())
        self.assertFalse((self.repo_dir / "app.js").exists())


class TestSecurityAndPermissions(unittest.TestCase):
    """Tests ensuring permission boundaries remain intact across modes."""

    def test_change_mode_permission_classification(self) -> None:
        self.assertEqual(classify_request_action("/mode"), PermissionAction.CHANGE_MODE)
        self.assertEqual(classify_request_action("/mode plan"), PermissionAction.CHANGE_MODE)
        self.assertEqual(classify_request_action("/mode chat"), PermissionAction.CHANGE_MODE)
        self.assertEqual(classify_request_action("/mode build"), PermissionAction.CHANGE_MODE)
        self.assertEqual(classify_request_action("switch mode plan"), PermissionAction.CHANGE_MODE)

    def test_mode_command_allowed_on_remote_channels(self) -> None:
        allowed, reason = evaluate_channel_action("telegram", PermissionAction.CHANGE_MODE)
        self.assertTrue(allowed, f"Expected CHANGE_MODE to be allowed on telegram: {reason}")

        allowed, reason = evaluate_channel_action("whatsapp", PermissionAction.CHANGE_MODE)
        self.assertTrue(allowed, f"Expected CHANGE_MODE to be allowed on whatsapp: {reason}")

    def test_build_mode_does_not_grant_remote_shell_or_code_edits(self) -> None:
        pol = get_default_policy("telegram")
        # Mode does not bypass remote policy
        self.assertFalse(pol.is_allowed(PermissionAction.SHELL_EXECUTION))
        self.assertFalse(pol.is_allowed(PermissionAction.WRITE_CODE))
        self.assertFalse(pol.is_allowed(PermissionAction.DEPLOYMENT))
        self.assertFalse(pol.is_allowed(PermissionAction.CREDENTIAL_ACCESS))

    def test_chat_mode_rejects_mutation_dispatch_points(self) -> None:
        """Chat mode must be rejected at code dispatch points (defense-in-depth)."""
        tmp_dir = tempfile.mkdtemp(prefix="bf_chat_guard_")
        try:
            repo = Path(tmp_dir)
            with self.assertRaises(PermissionError) as ctx:
                _write_files(repo, {"evil.py": "malicious"}, mode="chat")
            self.assertIn("dilarang dalam mode Chat", str(ctx.exception))
            self.assertFalse((repo / "evil.py").exists())

            cfg = RunConfig(repo_dir=repo, task="hack", mode="chat")
            orch = Orchestrator(MockSystem1Client(), CapturingSystem2Client(), cfg)
            with self.assertRaises(PermissionError) as ctx2:
                orch._run_step(PlanStep("1", "hack", ["evil.py"]))
            self.assertIn("dilarang dalam mode Chat", str(ctx2.exception))
            self.assertFalse((repo / "evil.py").exists())
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)



class TestPhase3AModeBoundaryAudit(unittest.TestCase):
    """Phase 3A: End-to-end behavioral boundary audit of /chat, /plan, and /build modes."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="bf_phase3a_audit_")
        self.repo_dir = Path(self.temp_dir).resolve()
        (self.repo_dir / "README.md").write_text("# Test Repo\n", encoding="utf-8")
        (self.repo_dir / "modules.json").write_text(json.dumps({"core": {"paths": ["main.py"]}}), encoding="utf-8")
        self.store = FileSessionStore(self.temp_dir)
        self.sessions = SessionManager(store=self.store)
        self.mock_s2 = CapturingSystem2Client()
        self.approval_store = FileApprovalStore(repo_dir=self.repo_dir)
        self.approval_service = ApprovalService(store=self.approval_store, two_man_rule_enabled=False)
        self.runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            sessions=self.sessions,
            approval_service=self.approval_service,
            system1_factory=lambda *a, **kw: MockSystem1Client(),
            system2_factory=lambda *a, **kw: self.mock_s2,
            require_approval=False,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # Audit Case 1: Explicit slash commands and aliases
    # -------------------------------------------------------------------------
    def test_explicit_slash_commands_mode_switching(self) -> None:
        # 1. /chat without args
        msg_chat = IncomingMessage(id="m1", channel="telegram", user_id="u1", conversation_id="c1", text="/chat")
        resp = self.runtime.handle_message(msg_chat)
        self.assertTrue(resp.success)
        self.assertEqual(resp.status, "completed")
        self.assertIn("Mode switched to `chat`", resp.text)
        sess = self.sessions.get("telegram:u1:c1")
        self.assertIsNotNone(sess)
        self.assertEqual(sess.active_mode, "chat")

        # 2. /build without args
        msg_build = IncomingMessage(id="m2", channel="telegram", user_id="u1", conversation_id="c1", text="/build")
        resp = self.runtime.handle_message(msg_build)
        self.assertTrue(resp.success)
        self.assertIn("Mode switched to `build`", resp.text)
        sess = self.sessions.get("telegram:u1:c1")
        self.assertEqual(sess.active_mode, "build")

        # 3. /plan without args
        msg_plan = IncomingMessage(id="m3", channel="telegram", user_id="u1", conversation_id="c1", text="/plan")
        resp = self.runtime.handle_message(msg_plan)
        self.assertTrue(resp.success)
        sess = self.sessions.get("telegram:u1:c1")
        self.assertEqual(sess.active_mode, "plan")

        # 4. /chat with query routes in chat mode
        msg_chat_q = IncomingMessage(id="m4", channel="cli", user_id="local", conversation_id="c2", text="/chat explain architecture")
        resp = self.runtime.handle_message(msg_chat_q)
        self.assertTrue(resp.success)
        self.assertIn("Diagnosed answer for: explain architecture", resp.text)
        self.assertEqual(len(self.mock_s2.diagnose_calls), 1)
        self.assertEqual(len(self.mock_s2.plan_and_prd_calls), 0)

        # 5. /build with task without contract fails closed
        msg_build_t = IncomingMessage(id="m5", channel="cli", user_id="local", conversation_id="c2", text="/build implement payment")
        resp = self.runtime.handle_message(msg_build_t)
        self.assertTrue(resp.success)
        self.assertIn("Build mode is defined, but plan selection and controlled execution are not implemented yet", resp.text)
        self.assertEqual(len(self.mock_s2.plan_and_prd_calls), 0)

        # 6. /plan with task routes to planning
        msg_plan_t = IncomingMessage(id="m6", channel="cli", user_id="local", conversation_id="c2", text="/plan create database architecture")
        resp = self.runtime.handle_message(msg_plan_t)
        self.assertTrue(resp.success)
        self.assertEqual(len(self.mock_s2.plan_and_prd_calls), 1)
        plans = list(get_plans_dir(self.repo_dir).glob("*.md"))
        self.assertTrue(len(plans) >= 1)

    # -------------------------------------------------------------------------
    # Audit Case 2: Natural-language and keyword routing into each mode
    # -------------------------------------------------------------------------
    def test_natural_language_and_keyword_routing_in_chat_mode(self) -> None:
        sess = self.sessions.get_or_create(channel="cli", user_id="local", conversation_id="c_chat")
        sess.active_mode = "chat"
        self.sessions.save(sess)

        # A. Conversational question in chat mode
        resp = self.runtime.handle_message(IncomingMessage(id="q1", channel="cli", user_id="local", conversation_id="c_chat", text="How is auth designed?"))
        self.assertTrue(resp.success)
        self.assertEqual(len(self.mock_s2.diagnose_calls), 1)
        self.assertEqual(len(self.mock_s2.plan_and_prd_calls), 0)
        self.assertFalse(get_plans_dir(self.repo_dir).exists())

        # B. Request implying implementation in chat mode (must not mutate code or create plans)
        resp2 = self.runtime.handle_message(IncomingMessage(id="q2", channel="cli", user_id="local", conversation_id="c_chat", text="create a new file auth.py with def login(): pass"))
        self.assertTrue(resp2.success)
        self.assertEqual(len(self.mock_s2.plan_and_prd_calls), 0)
        self.assertFalse((self.repo_dir / "auth.py").exists())
        self.assertFalse(get_plans_dir(self.repo_dir).exists())

        # C. Keyword execution triggers in chat mode
        for trigger in ("execute plan", "jalankan plan", "do it", "gass"):
            resp3 = self.runtime.handle_message(IncomingMessage(id="q3", channel="cli", user_id="local", conversation_id="c_chat", text=trigger))
            self.assertTrue(resp3.success)
            self.assertEqual(len(self.mock_s2.plan_and_prd_calls), 0)
            self.assertFalse((self.repo_dir / "auth.py").exists())

    def test_natural_language_and_keyword_routing_in_plan_mode(self) -> None:
        sess = self.sessions.get_or_create(channel="cli", user_id="local", conversation_id="c_plan")
        sess.active_mode = "plan"
        self.sessions.save(sess)

        # A. Conversational question in plan mode -> diagnosis, no plan
        resp = self.runtime.handle_message(IncomingMessage(id="p1", channel="cli", user_id="local", conversation_id="c_plan", text="Explain the directory layout"))
        self.assertTrue(resp.success)
        self.assertEqual(len(self.mock_s2.diagnose_calls), 1)
        self.assertEqual(len(self.mock_s2.plan_and_prd_calls), 0)
        self.assertFalse(get_plans_dir(self.repo_dir).exists())

        # B. Planning task in plan mode -> plan generated
        resp2 = self.runtime.handle_message(IncomingMessage(id="p2", channel="cli", user_id="local", conversation_id="c_plan", text="Plan the migration to PostgreSQL"))
        self.assertTrue(resp2.success)
        self.assertEqual(len(self.mock_s2.plan_and_prd_calls), 1)
        self.assertTrue(get_plans_dir(self.repo_dir).exists())

        # C. Keyword execution triggers in plan mode -> does not execute or mutate
        for trigger in ("execute plan", "jalankan plan", "lanjutkan", "gass"):
            resp3 = self.runtime.handle_message(IncomingMessage(id="p3", channel="cli", user_id="local", conversation_id="c_plan", text=trigger))
            self.assertTrue(resp3.success)
            self.assertFalse((self.repo_dir / "schema.sql").exists())

    def test_natural_language_and_keyword_routing_in_build_mode(self) -> None:
        sess = self.sessions.get_or_create(channel="cli", user_id="local", conversation_id="c_build")
        sess.active_mode = "build"
        self.sessions.save(sess)

        # Build mode without contract fails closed for any trigger or task
        for trigger in ("build user auth", "execute plan", "jalankan plan", "do it"):
            resp = self.runtime.handle_message(IncomingMessage(id="b1", channel="cli", user_id="local", conversation_id="c_build", text=trigger))
            self.assertTrue(resp.success)
            self.assertIn("Build mode is defined, but plan selection and controlled execution are not implemented yet", resp.text)
            self.assertFalse((self.repo_dir / "auth.py").exists())

    # -------------------------------------------------------------------------
    # Audit Case 3: Follow-up messages after switching modes
    # -------------------------------------------------------------------------
    def test_follow_up_messages_across_mode_transitions(self) -> None:
        # Start in chat
        self.runtime.handle_message(IncomingMessage(id="f1", channel="cli", user_id="local", conversation_id="c_follow", text="/chat"))
        sess = self.sessions.get("cli:local:c_follow")
        self.assertEqual(sess.active_mode, "chat")

        # Chat follow-up: question
        resp_q = self.runtime.handle_message(IncomingMessage(id="f2", channel="cli", user_id="local", conversation_id="c_follow", text="What is this?"))
        self.assertTrue(resp_q.success)
        self.assertEqual(len(self.mock_s2.diagnose_calls), 1)

        # Switch to plan
        self.runtime.handle_message(IncomingMessage(id="f3", channel="cli", user_id="local", conversation_id="c_follow", text="/plan"))
        sess = self.sessions.get("cli:local:c_follow")
        self.assertEqual(sess.active_mode, "plan")

        # Plan follow-up: task generates plan
        resp_plan = self.runtime.handle_message(IncomingMessage(id="f4", channel="cli", user_id="local", conversation_id="c_follow", text="Plan new feature"))
        self.assertTrue(resp_plan.success)
        self.assertEqual(len(self.mock_s2.plan_and_prd_calls), 1)

        # Switch back to chat
        self.runtime.handle_message(IncomingMessage(id="f5", channel="cli", user_id="local", conversation_id="c_follow", text="/chat"))
        sess = self.sessions.get("cli:local:c_follow")
        self.assertEqual(sess.active_mode, "chat")

        # Chat follow-up: plan request in chat mode must NOT generate new plan
        prev_plan_count = len(list(get_plans_dir(self.repo_dir).glob("*.md")))
        resp_chat_plan = self.runtime.handle_message(IncomingMessage(id="f6", channel="cli", user_id="local", conversation_id="c_follow", text="Plan new feature"))
        self.assertTrue(resp_chat_plan.success)
        new_plan_count = len(list(get_plans_dir(self.repo_dir).glob("*.md")))
        self.assertEqual(prev_plan_count, new_plan_count)

        # Verify session history and continuity
        sess_final = self.sessions.get("cli:local:c_follow")
        self.assertTrue(len(sess_final.history) >= 2)

    # -------------------------------------------------------------------------
    # Audit Case 4: Missing, stale, expired, mismatched, or replayed approval contracts
    # -------------------------------------------------------------------------
    def test_build_mode_rejects_missing_stale_expired_mismatched_replayed(self) -> None:
        sess = self.sessions.get_or_create(channel="cli", user_id="local", conversation_id="c_contract")
        sess.active_mode = "build"
        self.sessions.save(sess)

        # 1. Missing contract
        resp_missing = self.runtime.handle_message(IncomingMessage(
            id="c1", channel="cli", user_id="local", conversation_id="c_contract", text="write code into main.py",
        ))
        self.assertFalse((self.repo_dir / "index.html").exists())

        # Create a valid canonical approval request
        canon_op = CanonicalOperation(
            action_type="write_file",
            target="main.py",
            parameters={"path": "main.py", "content": "print('updated')\n"},
        )
        app_req = self.approval_service.create_request(
            session_id=sess.session_id,
            channel="cli",
            user_id="local",
            conversation_id="c_contract",
            operation_type="write_file",
            canonical_operation=canon_op,
            risk_class="medium",
            session_incarnation_id=sess.session_incarnation_id,
            workspace_root=str(self.repo_dir),
        )
        self.approval_service.approve(app_req.request_id, approver_id="approver_lead", channel="cli")

        # 2. Expired approval request
        app_req_exp = self.approval_service.create_request(
            session_id=sess.session_id,
            channel="cli",
            user_id="local",
            conversation_id="c_contract",
            operation_type="write_file",
            canonical_operation=canon_op,
            risk_class="medium",
            session_incarnation_id=sess.session_incarnation_id,
            workspace_root=str(self.repo_dir),
            ttl_seconds=-10.0,
        )
        self.approval_service.approve(app_req_exp.request_id, approver_id="approver_lead", channel="cli")
        resp_expired = self.runtime.handle_message(IncomingMessage(
            id="c2", channel="cli", user_id="local", conversation_id="c_contract", text=f"/exec {app_req_exp.request_id}",
        ))
        self.assertFalse(resp_expired.success)
        self.assertEqual(resp_expired.status, "rejected")
        self.assertIn("expired", resp_expired.text.lower())

        # 3. Mismatched session ID
        diff_sess = self.sessions.get_or_create(channel="cli", user_id="local", conversation_id="c_other")
        resp_mismatch_sess = self.runtime.handle_message(IncomingMessage(
            id="c3", channel="cli", user_id="local", conversation_id="c_other", text=f"/exec {app_req.request_id}",
        ))
        self.assertFalse(resp_mismatch_sess.success)
        self.assertEqual(resp_mismatch_sess.status, "rejected")

        # 4. Mismatched workspace
        other_dir = Path(tempfile.mkdtemp(prefix="bf_other_ws_"))
        try:
            (other_dir / "README.md").write_text("# Other\n", encoding="utf-8")
            self.runtime.workspace_registry.register("other_ws", other_dir)
            self.runtime.handle_message(IncomingMessage(
                id="c4_ws", channel="cli", user_id="local", conversation_id="c_contract", text="/workspace other_ws",
            ))
            resp_mismatch_ws = self.runtime.handle_message(IncomingMessage(
                id="c4", channel="cli", user_id="local", conversation_id="c_contract", text=f"/exec {app_req.request_id}",
            ))
            self.assertFalse(resp_mismatch_ws.success)
            self.assertIn("Workspace Mismatch", resp_mismatch_ws.text)
        finally:
            shutil.rmtree(other_dir, ignore_errors=True)

        # Switch back to default workspace
        self.runtime.handle_message(IncomingMessage(
            id="c4_reset", channel="cli", user_id="local", conversation_id="c_contract", text="/workspace default",
        ))

        # 5. Replayed approval contract
        is_valid, consumed_req, reason = self.approval_service.verify_and_consume(
            request_id=app_req.request_id,
            expected_digest=app_req.operation_digest,
            session_id=sess.session_id,
            channel="cli",
            session_incarnation_id=sess.session_incarnation_id,
            requester_id="local",
        )
        self.assertTrue(is_valid)
        self.assertEqual(consumed_req.status, ApprovalStatus.CONSUMED)

        # Second consumption (replay) must fail closed
        resp_replay = self.runtime.handle_message(IncomingMessage(
            id="c5", channel="cli", user_id="local", conversation_id="c_contract", text=f"/exec {app_req.request_id}",
        ))
        self.assertFalse(resp_replay.success)
        self.assertEqual(resp_replay.status, "rejected")
        self.assertIn("already been consumed", resp_replay.text.lower())

    # -------------------------------------------------------------------------
    # Audit Case 5: Tampered plan content between approval and execution
    # -------------------------------------------------------------------------
    def test_tampered_plan_content_fails_closed(self) -> None:
        p_path = save_plan_document(
            self.repo_dir, "20261018_secure.md", "# Original Content",
            metadata={"title": "Secure Plan", "status": "ready"},
        )
        reg = PlanRegistry(self.repo_dir)
        p_doc = reg.get_plan("20261018_secure")
        self.assertIsNotNone(p_doc)

        # Tamper markdown out of band
        p_path.write_text("# Maliciously Altered Content Out-of-band\n", encoding="utf-8")
        tampered_doc = reg.get_plan("20261018_secure")
        self.assertTrue(tampered_doc.content_tampered)

        # Contract referencing original hash fails validation
        contract = ApprovedExecutionContract(
            request_id="req_tamper_1",
            action_type="write_file",
            approved_targets=frozenset({"main.py"}),
            operation_digest="digest_123",
            channel="cli",
            actor="local",
            session_id="cli:local:c_tamper",
            session_incarnation_id="inc_1",
            workspace_root=str(self.repo_dir),
            plan_id="20261018_secure",
            plan_revision=tampered_doc.revision,
            plan_content_hash=p_doc.content_hash,
        )
        with self.assertRaises((ValueError, PermissionError)):
            contract.validate(
                actor="local", channel="cli", session_id="cli:local:c_tamper",
                session_incarnation_id="inc_1", repo_dir=self.repo_dir,
                expected_plan_id="20261018_secure",
            )

    # -------------------------------------------------------------------------
    # Audit Case 6: Mode changes during active session
    # -------------------------------------------------------------------------
    def test_mode_changes_during_active_session_preserves_state(self) -> None:
        sess = self.sessions.get_or_create(channel="telegram", user_id="u_cycle", conversation_id="c_cycle")
        sess.active_provider = "sumopod"
        sess.active_model = "deepseek-v4.1-flash:netra"
        sess.record_interaction("query 1", "answer 1")
        self.sessions.save(sess)

        modes = ["chat", "plan", "build", "chat", "plan", "build", "chat"]
        for target in modes:
            resp = self.runtime.handle_message(IncomingMessage(
                id=f"msg_cyc_{target}", channel="telegram", user_id="u_cycle",
                conversation_id="c_cycle", text=f"/mode {target}",
            ))
            self.assertTrue(resp.success)
            self.assertIn(f"Mode switched to `{target}`", resp.text)
            reloaded = self.sessions.get(sess.session_id)
            self.assertEqual(reloaded.active_mode, target)
            self.assertEqual(reloaded.active_provider, "sumopod")
            self.assertEqual(reloaded.active_model, "deepseek-v4.1-flash:netra")
            self.assertTrue(len(reloaded.history) >= 1)

    # -------------------------------------------------------------------------
    # Audit Case 7: Failure paths have zero mutation / staging side effects
    # -------------------------------------------------------------------------
    def test_failure_paths_preserve_workspace_cleanliness(self) -> None:
        def snapshot_dir(p: Path) -> Dict[str, str]:
            snap = {}
            for item in p.rglob("*"):
                rel = item.relative_to(p)
                if item.is_file() and not item.name.endswith(".lock") and not str(rel).startswith(".brainfrog"):
                    snap[str(rel)] = hashlib.sha256(item.read_bytes()).hexdigest()
            return snap

        before = snapshot_dir(self.repo_dir)

        # Rejected invalid mode
        self.runtime.handle_message(IncomingMessage(id="f1", channel="cli", user_id="local", conversation_id="c_fail", text="/mode super_admin"))
        # Denied destructive shell
        self.runtime.handle_message(IncomingMessage(id="f2", channel="telegram", user_id="u1", conversation_id="c_fail", text="!rm -rf src"))
        # Unapproved build mode turn
        self.runtime.handle_message(IncomingMessage(id="f3", channel="cli", user_id="local", conversation_id="c_fail", text="execute plan", metadata={"mode": "build"}))

        after = snapshot_dir(self.repo_dir)
        self.assertEqual(before, after, "Rejected requests mutated files on disk!")

    # -------------------------------------------------------------------------
    # Audit Case 8: Single shared runtime and permission gate enforcement
    # -------------------------------------------------------------------------
    def test_shared_runtime_and_permission_gate_enforcement(self) -> None:
        class DenyModePolicyProvider:
            def __call__(self, channel: str) -> PermissionPolicy:
                pol = get_default_policy(channel)
                pol.allowed_actions.discard(PermissionAction.CHANGE_MODE.value)
                return pol

        strict_runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            sessions=self.sessions,
            policy_provider=DenyModePolicyProvider(),
            system1_factory=lambda *a, **kw: MockSystem1Client(),
            system2_factory=lambda *a, **kw: self.mock_s2,
            require_approval=False,
        )

        for cmd in ("/mode chat", "/chat", "/build"):
            resp = strict_runtime.handle_message(IncomingMessage(
                id="strict_1", channel="telegram", user_id="u1", conversation_id="c_strict", text=cmd,
            ))
            self.assertFalse(resp.success)
            self.assertEqual(resp.status, "rejected")
            self.assertIn("Permission Denied", resp.text)


class TestPhase3BEndToEndWorkflowAndRefinement(unittest.TestCase):
    """Phase 3B: End-to-end plan refinement, approval handoff, and multi-channel workflow tests."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="bf_phase3b_e2e_")
        self.repo_dir = Path(self.temp_dir).resolve()
        (self.repo_dir / "README.md").write_text("# Phase 3B Test Project\n", encoding="utf-8")
        (self.repo_dir / "modules.json").write_text(json.dumps({"core": {"paths": ["main.py"]}}), encoding="utf-8")
        self.store = FileSessionStore(self.temp_dir)
        self.sessions = SessionManager(store=self.store)
        self.mock_s2 = CapturingSystem2Client()
        self.approval_store = FileApprovalStore(repo_dir=self.repo_dir)
        self.approval_service = ApprovalService(store=self.approval_store, two_man_rule_enabled=False)
        self.runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            sessions=self.sessions,
            approval_service=self.approval_service,
            system1_factory=lambda *a, **kw: MockSystem1Client(),
            system2_factory=lambda *a, **kw: self.mock_s2,
            require_approval=False,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_scenario_a_chat_to_plan_workflow(self) -> None:
        """Scenario A: Chat discussion across multiple messages -> explicit /plan -> plan generated without mutation."""
        # 1. Start in /chat
        resp_mode = self.runtime.handle_message(IncomingMessage(
            id="a1", channel="cli", user_id="local", conversation_id="c_scen_a", text="/chat"
        ))
        self.assertTrue(resp_mode.success)
        sess = self.sessions.get("cli:local:c_scen_a")
        self.assertEqual(sess.active_mode, "chat")

        # 2. Multi-message discussion in chat
        resp_q1 = self.runtime.handle_message(IncomingMessage(
            id="a2", channel="cli", user_id="local", conversation_id="c_scen_a", text="What is the recommended design for payments?"
        ))
        self.assertTrue(resp_q1.success)
        self.assertIn("Diagnosed answer for", resp_q1.text)

        resp_q2 = self.runtime.handle_message(IncomingMessage(
            id="a3", channel="cli", user_id="local", conversation_id="c_scen_a", text="Explain how webhooks should handle idempotency"
        ))
        self.assertTrue(resp_q2.success)
        self.assertEqual(len(self.mock_s2.diagnose_calls), 2)
        self.assertEqual(len(self.mock_s2.plan_and_prd_calls), 0)

        # 3. Confirm no plan artifact or source file created while in chat
        plans_dir = get_plans_dir(self.repo_dir)
        if plans_dir.exists():
            self.assertEqual(len(list(plans_dir.glob("*.md"))), 0)
        self.assertFalse((self.repo_dir / "payments.py").exists())

        # 4. Explicitly enter /plan and generate plan from clarified requirements
        resp_plan = self.runtime.handle_message(IncomingMessage(
            id="a4", channel="cli", user_id="local", conversation_id="c_scen_a", text="/plan draft payment service implementation plan"
        ))
        self.assertTrue(resp_plan.success)
        self.assertEqual(len(self.mock_s2.plan_and_prd_calls), 1)

        # 5. Confirm plan document exists and no implementation changes applied
        plans = list(plans_dir.glob("*.md"))
        self.assertEqual(len(plans), 1)
        self.assertFalse((self.repo_dir / "payments.py").exists())
        self.assertFalse((self.repo_dir / "schema.sql").exists())

        # Session mode is updated and selected_plan_id is set
        sess = self.sessions.get("cli:local:c_scen_a")
        self.assertEqual(sess.active_mode, "plan")
        self.assertIsNotNone(sess.selected_plan_id)

    def test_scenario_b_plan_refinement(self) -> None:
        """Scenario B: Generate plan -> revise requirements -> revision & hash update -> old approval invalidated."""
        # 1. Generate initial plan in plan mode
        resp_init = self.runtime.handle_message(IncomingMessage(
            id="b1", channel="cli", user_id="local", conversation_id="c_scen_b", text="/plan design order processing"
        ))
        self.assertTrue(resp_init.success)
        sess = self.sessions.get("cli:local:c_scen_b")
        p_id = sess.selected_plan_id
        self.assertIsNotNone(p_id)

        reg = PlanRegistry(self.repo_dir)
        plan_v1 = reg.get_plan(p_id)
        self.assertIsNotNone(plan_v1)
        self.assertEqual(plan_v1.revision, 1)
        hash_v1 = plan_v1.content_hash

        # 2. Issue an approval contract for v1
        canon_op = CanonicalOperation("write_files", "order.py", {"files": ["order.py"]})
        caps_b = Capabilities(
            filesystem=FilesystemPolicy(read=("order.py",), write=("order.py",)),
            network=NetworkPolicy(access=True),
        )
        app_req_v1 = self.approval_service.create_request(
            session_id=sess.session_id,
            channel="cli",
            user_id="local",
            conversation_id="c_scen_b",
            operation_type="write_files",
            canonical_operation=canon_op,
            session_incarnation_id=sess.session_incarnation_id,
            capabilities=caps_b,
            workspace_root=str(self.repo_dir),
            plan_id=plan_v1.id,
            plan_revision=plan_v1.revision,
            plan_content_hash=hash_v1,
        )
        self.approval_service.approve(app_req_v1.request_id, approver_id="approver_lead", channel="cli")
        is_val, consumed_v1, _ = self.approval_service.verify_and_consume(
            request_id=app_req_v1.request_id,
            expected_digest=app_req_v1.operation_digest,
            session_id=sess.session_id,
            channel="cli",
            session_incarnation_id=sess.session_incarnation_id,
            requester_id="local",
        )
        self.assertTrue(is_val)
        contract_v1 = ApprovedExecutionContract.from_approval_request(consumed_v1, self.repo_dir)
        # Validate contract_v1 matches current plan v1
        contract_v1.validate(
            actor="local", channel="cli", session_id=sess.session_id,
            session_incarnation_id=sess.session_incarnation_id, repo_dir=self.repo_dir,
            expected_plan_id=plan_v1.id,
        )

        # 3. Revise requirements or acceptance criteria in plan mode
        self.mock_s2.plan_generator = lambda task, *a, **kw: {
            "title": plan_v1.title,
            "markdown_doc": f"# {plan_v1.title} (Revised)\n\n## Kriteria Penerimaan\n- [ ] Idempotency key required\n- [ ] Max retry limit enforced\n",
            "problem": task,
            "goals": [task],
            "steps": [{"id": "1", "description": "Add idempotency keys", "files": ["order.py"]}],
            "relevant_files": ["order.py"],
            "acceptance_criteria": ["Idempotency key required", "Max retry limit enforced"],
        }
        resp_rev = self.runtime.handle_message(IncomingMessage(
            id="b2", channel="cli", user_id="local", conversation_id="c_scen_b", text="add idempotency keys to acceptance criteria"
        ))
        self.assertTrue(resp_rev.success)

        # 4. Confirm revision and content hash are updated consistently
        plan_v2 = reg.get_plan(plan_v1.id)
        self.assertIsNotNone(plan_v2)
        self.assertEqual(plan_v2.revision, 2)
        self.assertNotEqual(plan_v2.content_hash, hash_v1)

        # 5. Confirm previously issued approval cannot authorize changed plan content
        with self.assertRaises(PermissionError) as ctx:
            contract_v1.validate(
                actor="local", channel="cli", session_id=sess.session_id,
                session_incarnation_id=sess.session_incarnation_id, repo_dir=self.repo_dir,
                expected_plan_id=plan_v1.id,
            )
        err = str(ctx.exception)
        self.assertTrue("modified since approval" in err or "does not match approved revision" in err)

        # 6. Ensure selection of a plan is not treated as execution permission
        resp_unauth = self.runtime.handle_message(IncomingMessage(
            id="b3", channel="cli", user_id="local", conversation_id="c_scen_b", text="execute plan", metadata={"mode": "build"}
        ))
        self.assertTrue(resp_unauth.success)
        self.assertIn("Build mode is defined, but plan selection and controlled execution are not implemented yet", resp_unauth.text)
        self.assertFalse((self.repo_dir / "order.py").exists())

    def test_scenario_c_approval_handoff_to_build(self) -> None:
        """Scenario C: Obtain approval contract bound to plan -> execute in /build -> rejection of stale/tampered."""
        # 1. Create a plan and set status to READY
        resp_plan = self.runtime.handle_message(IncomingMessage(
            id="c1", channel="cli", user_id="local", conversation_id="c_scen_c", text="/plan create user auth module"
        ))
        self.assertTrue(resp_plan.success)
        sess = self.sessions.get("cli:local:c_scen_c")
        p_id = sess.selected_plan_id
        reg = PlanRegistry(self.repo_dir)
        p_doc = reg.get_plan(p_id)

        reg.update_status(p_doc.id, PlanStatus.READY, actor="lead", reason="Plan reviewed and approved for build")
        p_ready = reg.get_plan(p_doc.id)
        self.assertEqual(p_ready.status, PlanStatus.READY)

        # 2. Use approval workflow to obtain an approval contract
        target_file = "auth_service.py"
        canon_op = CanonicalOperation("write_files", target_file, {"files": [target_file]})
        caps_c = Capabilities(
            filesystem=FilesystemPolicy(read=(target_file,), write=(target_file,)),
            network=NetworkPolicy(access=True),
        )
        app_req = self.approval_service.create_request(
            session_id=sess.session_id,
            channel="cli",
            user_id="local",
            conversation_id="c_scen_c",
            operation_type="write_files",
            canonical_operation=canon_op,
            session_incarnation_id=sess.session_incarnation_id,
            capabilities=caps_c,
            workspace_root=str(self.repo_dir),
            plan_id=p_ready.id,
            plan_revision=p_ready.revision,
            plan_content_hash=p_ready.content_hash,
        )
        self.approval_service.approve(app_req.request_id, approver_id="approver_lead", channel="cli")

        # 3. Enter /build and execute via /exec
        resp_build = self.runtime.handle_message(IncomingMessage(
            id="c_build", channel="cli", user_id="local", conversation_id="c_scen_c", text="/build"
        ))
        self.assertTrue(resp_build.success)
        self.assertEqual(self.sessions.get("cli:local:c_scen_c").active_mode, "build")

        resp_exec = self.runtime.handle_message(IncomingMessage(
            id="c2", channel="cli", user_id="local", conversation_id="c_scen_c", text=f"/exec {app_req.request_id}"
        ))
        self.assertTrue(resp_exec.success)
        self.assertTrue((self.repo_dir / target_file).exists())
        self.assertIn("class AuthService", (self.repo_dir / target_file).read_text(encoding="utf-8"))

        # Plan transitioned READY -> IN_PROGRESS -> IMPLEMENTED
        p_post = reg.get_plan(p_ready.id)
        self.assertEqual(p_post.status, PlanStatus.IMPLEMENTED)

        # 4. Verify replayed approval contract is rejected
        resp_replay = self.runtime.handle_message(IncomingMessage(
            id="c3", channel="cli", user_id="local", conversation_id="c_scen_c", text=f"/exec {app_req.request_id}"
        ))
        self.assertFalse(resp_replay.success)
        self.assertEqual(resp_replay.status, "rejected")
        self.assertIn("already been consumed", resp_replay.text.lower())

        # 5. Verify tampered plan content rejected before staging
        resp_plan_tamper = self.runtime.handle_message(IncomingMessage(
            id="c_plan_t", channel="cli", user_id="local", conversation_id="c_scen_c", text="/plan create second plan for tamper"
        ))
        self.assertTrue(resp_plan_tamper.success)
        p_tamper_id = self.sessions.get("cli:local:c_scen_c").selected_plan_id
        p_tamper_doc = reg.get_plan(p_tamper_id)
        reg.update_status(p_tamper_doc.id, PlanStatus.READY, actor="lead", reason="Plan reviewed and approved for build")
        p_tamper_ready = reg.get_plan(p_tamper_doc.id)

        app_req_tamper = self.approval_service.create_request(
            session_id=sess.session_id, channel="cli", user_id="local", conversation_id="c_scen_c",
            operation_type="write_files", canonical_operation=canon_op,
            session_incarnation_id=sess.session_incarnation_id,
            capabilities=caps_c,
            workspace_root=str(self.repo_dir),
            plan_id=p_tamper_ready.id, plan_revision=p_tamper_ready.revision, plan_content_hash=p_tamper_ready.content_hash,
        )
        self.approval_service.approve(app_req_tamper.request_id, approver_id="approver_lead", channel="cli")

        # Switch back to build mode
        self.runtime.handle_message(IncomingMessage(
            id="c_build_2", channel="cli", user_id="local", conversation_id="c_scen_c", text="/build"
        ))

        # Tamper markdown out of band
        p_tamper_ready.path.write_text("# HACKED PLAN\n- malicious content", encoding="utf-8")
        resp_tamper_exec = self.runtime.handle_message(IncomingMessage(
            id="c4", channel="cli", user_id="local", conversation_id="c_scen_c", text=f"/exec {app_req_tamper.request_id}"
        ))
        self.assertFalse(resp_tamper_exec.success)
        self.assertEqual(resp_tamper_exec.status, "rejected")
        self.assertTrue("modified" in resp_tamper_exec.text.lower() or "tamper" in resp_tamper_exec.text.lower())

    def test_scenario_d_cross_mode_and_cross_channel_behavior(self) -> None:
        """Scenario D: Mode switches (/chat -> /plan -> /build -> /chat), CLI vs Telegram, malformed commands."""
        # 1. Follow-up messages across transitions
        r1 = self.runtime.handle_message(IncomingMessage(id="d1", channel="cli", user_id="local", conversation_id="c_d", text="/chat"))
        self.assertTrue(r1.success)
        self.assertEqual(self.sessions.get("cli:local:c_d").active_mode, "chat")

        r2 = self.runtime.handle_message(IncomingMessage(id="d2", channel="cli", user_id="local", conversation_id="c_d", text="/plan"))
        self.assertTrue(r2.success)
        self.assertEqual(self.sessions.get("cli:local:c_d").active_mode, "plan")

        r3 = self.runtime.handle_message(IncomingMessage(id="d3", channel="cli", user_id="local", conversation_id="c_d", text="/build"))
        self.assertTrue(r3.success)
        self.assertEqual(self.sessions.get("cli:local:c_d").active_mode, "build")

        r4 = self.runtime.handle_message(IncomingMessage(id="d4", channel="cli", user_id="local", conversation_id="c_d", text="/chat"))
        self.assertTrue(r4.success)
        self.assertEqual(self.sessions.get("cli:local:c_d").active_mode, "chat")

        # 2. Mid-session state preservation
        sess = self.sessions.get("cli:local:c_d")
        sess.active_provider = "openai"
        sess.active_model = "gpt-4o"
        self.sessions.save(sess)

        self.runtime.handle_message(IncomingMessage(id="d5", channel="cli", user_id="local", conversation_id="c_d", text="/plan"))
        self.runtime.handle_message(IncomingMessage(id="d6", channel="cli", user_id="local", conversation_id="c_d", text="/build"))
        sess_after = self.sessions.get("cli:local:c_d")
        self.assertEqual(sess_after.active_provider, "openai")
        self.assertEqual(sess_after.active_model, "gpt-4o")

        # 3. CLI vs Telegram safety boundary
        resp_tg_shell = self.runtime.handle_message(IncomingMessage(
            id="d7", channel="telegram", user_id="tg_user", conversation_id="c_tg", text="!cat /etc/passwd"
        ))
        self.assertFalse(resp_tg_shell.success)
        self.assertIn("Permission Denied", resp_tg_shell.text)

        # 4. Unknown or malformed mode command
        resp_bad = self.runtime.handle_message(IncomingMessage(
            id="d8", channel="cli", user_id="local", conversation_id="c_d", text="/mode turbo_exec"
        ))
        self.assertFalse(resp_bad.success)
        self.assertIn("Unsupported mode", resp_bad.text)
        self.assertEqual(self.sessions.get("cli:local:c_d").active_mode, "build")

        # 5. Natural-language requests: "gass", "jalankan plan", "execute plan"
        for phrase in ("gass", "jalankan plan", "execute plan"):
            resp_phrase = self.runtime.handle_message(IncomingMessage(
                id=f"d_nl_{phrase}", channel="cli", user_id="local", conversation_id="c_d", text=phrase
            ))
            # In build mode without contract, all natural language execution triggers must fail closed
            self.assertTrue(resp_phrase.success)
            self.assertIn("Build mode is defined, but plan selection and controlled execution are not implemented yet", resp_phrase.text)
            self.assertFalse((self.repo_dir / "index.html").exists())

    def test_scenario_e_failure_and_recovery(self) -> None:
        """Scenario E: Failures during planning, approval, and execution produce zero mutations & preserve user files."""
        # 1. Failure during planning (e.g. S2 exception)
        def raise_llm_error(*args: Any, **kwargs: Any) -> Dict[str, Any]:
            raise RuntimeError("Upstream LLM Rate Limit")

        self.mock_s2.plan_generator = raise_llm_error
        resp_fail_plan = self.runtime.handle_message(IncomingMessage(
            id="e1", channel="cli", user_id="local", conversation_id="c_scen_e", text="/plan design rate limiter"
        ))
        self.assertFalse(resp_fail_plan.success)
        self.assertEqual(resp_fail_plan.status, "error")
        self.assertIn("Upstream LLM Rate Limit", resp_fail_plan.text)
        self.mock_s2.plan_generator = None

        # 2. Pre-existing user files are untouched
        user_file = self.repo_dir / "user_code.py"
        user_file.write_text("def pre_existing(): return 42\n", encoding="utf-8")
        orig_user_hash = hashlib.sha256(user_file.read_bytes()).hexdigest()

        # 3. WAL recovery idempotence
        plans_dir = get_plans_dir(self.repo_dir)
        plans_dir.mkdir(parents=True, exist_ok=True)
        rec_count_1 = recover_plan_journal(self.repo_dir)
        rec_count_2 = recover_plan_journal(self.repo_dir)
        self.assertEqual(len(rec_count_1), 0)
        self.assertEqual(len(rec_count_2), 0)

        # 4. User file content hash strictly preserved
        self.assertEqual(hashlib.sha256(user_file.read_bytes()).hexdigest(), orig_user_hash)


if __name__ == "__main__":
    unittest.main()
