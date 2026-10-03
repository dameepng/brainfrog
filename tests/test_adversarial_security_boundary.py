"""Adversarial Security Boundary & Permission Hardening Test Suite (v2).

Validates the non-negotiable security invariants for BrainFrog:
1. Deterministic Permission Policy is authoritative over LLM / System 1 output.
2. Remote channels (Telegram, WhatsApp) cannot execute shell commands, mutate files,
   run destructive git, access credentials, or trigger deployments.
3. Indirect phrasing (without '!') cannot bypass shell prohibitions.
4. Remote build requests cannot mutate source code files (workspace before == after).
5. Pre-runtime authentication/allowlist rejects unauthorized users before reaching the agent.
6. Cross-channel spoofing (claiming 'cli' channel from remote user) is rejected.
7. Strict 5-way session isolation across channels, users, and conversations.
8. Secret redaction prevents credential leakage.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.channels.telegram import TelegramChannel
from core.channels.whatsapp import MockWhatsAppTransport, WhatsAppChannel
from core.runtime.messages import IncomingMessage, OutgoingMessage
from core.runtime.permissions import (
    ChannelTrustLevel,
    PermissionAction,
    PermissionPolicy,
    classify_request_action,
    evaluate_channel_action,
    get_default_policy,
)
from core.runtime.runtime import BrainFrogRuntime, scrub_secrets
from core.runtime.session import SessionManager, session_manager
from orchestrator import PlanStep, StepResult


class TestAdversarialSecurityBoundary(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="brainfrog_sec_test_")
        self.repo_dir = Path(self.test_dir)
        (self.repo_dir / "src").mkdir(parents=True, exist_ok=True)
        (self.repo_dir / "src" / "main.py").write_text("print('hello world')\n", encoding="utf-8")
        (self.repo_dir / "src" / "utils.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")
        (self.repo_dir / "package.json").write_text('{"name": "test-pkg"}\n', encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # =========================================================================
    # PHASE 1 & 7: System 1 Misclassification Invariant
    # =========================================================================
    @patch("core.runtime.runtime.Orchestrator")
    def test_phase1_and_7_system1_misclassification_cannot_escalate_privilege(self, mock_orch_cls):
        """Even if System 1 classifies a dangerous request as benign (intent='safe_read', risk='low'),

        the deterministic Permission Policy MUST STILL DENY it.
        """
        runtime = BrainFrogRuntime(repo_dir=self.repo_dir)

        # Adversarial request: Delete repository
        msg = IncomingMessage(
            text="Delete the project repository.",
            channel="telegram",
            user_id="user_adv_1",
            conversation_id="chat_1",
        )

        out = runtime.handle_message(msg)

        # Policy must deny; Orchestrator must NEVER be called
        self.assertFalse(out.success)
        self.assertEqual(out.status, "rejected")
        self.assertIn("strictly prohibited", out.text.lower())
        mock_orch_cls.assert_not_called()

    # =========================================================================
    # PHASE 2: Direct Remote Security Tests
    # =========================================================================
    @patch("core.runtime.runtime.Orchestrator")
    def test_phase2_run_shell_command_direct_rejected(self, mock_orch_cls):
        runtime = BrainFrogRuntime(repo_dir=self.repo_dir)
        msg = IncomingMessage(
            text="run_shell_command",
            channel="telegram",
            user_id="user_legit_1",
            conversation_id="chat_1",
            metadata={"requested_action": PermissionAction.RUN_SHELL_COMMAND.value},
        )
        out = runtime.handle_message(msg)
        self.assertFalse(out.success)
        self.assertEqual(out.status, "rejected")
        self.assertIn("strictly prohibited", out.text.lower())
        mock_orch_cls.assert_not_called()

    @patch("core.runtime.runtime.Orchestrator")
    def test_phase2_destructive_git_direct_rejected(self, mock_orch_cls):
        runtime = BrainFrogRuntime(repo_dir=self.repo_dir)
        msg = IncomingMessage(
            text="destructive_git",
            channel="telegram",
            user_id="user_legit_1",
            conversation_id="chat_1",
            metadata={"requested_action": PermissionAction.DESTRUCTIVE_GIT.value},
        )
        out = runtime.handle_message(msg)
        self.assertFalse(out.success)
        self.assertEqual(out.status, "rejected")
        self.assertIn("strictly prohibited", out.text.lower())
        mock_orch_cls.assert_not_called()

    @patch("core.runtime.runtime.Orchestrator")
    def test_phase2_write_files_direct_rejected(self, mock_orch_cls):
        runtime = BrainFrogRuntime(repo_dir=self.repo_dir)
        msg = IncomingMessage(
            text="write_files",
            channel="telegram",
            user_id="user_legit_1",
            conversation_id="chat_1",
            metadata={"requested_action": PermissionAction.WRITE_FILES.value},
        )
        out = runtime.handle_message(msg)
        self.assertFalse(out.success)
        self.assertEqual(out.status, "rejected")
        self.assertIn("restricted", out.text.lower())
        mock_orch_cls.assert_not_called()

    @patch("core.runtime.runtime.Orchestrator")
    def test_phase2_access_credentials_direct_rejected(self, mock_orch_cls):
        runtime = BrainFrogRuntime(repo_dir=self.repo_dir)
        msg = IncomingMessage(
            text="access_credentials",
            channel="telegram",
            user_id="user_legit_1",
            conversation_id="chat_1",
            metadata={"requested_action": PermissionAction.ACCESS_CREDENTIALS.value},
        )
        out = runtime.handle_message(msg)
        self.assertFalse(out.success)
        self.assertEqual(out.status, "rejected")
        self.assertIn("strictly prohibited", out.text.lower())
        mock_orch_cls.assert_not_called()

    @patch("core.runtime.runtime.Orchestrator")
    def test_phase2_deploy_project_direct_rejected(self, mock_orch_cls):
        runtime = BrainFrogRuntime(repo_dir=self.repo_dir)
        msg = IncomingMessage(
            text="deploy_project",
            channel="telegram",
            user_id="user_legit_1",
            conversation_id="chat_1",
            metadata={"requested_action": PermissionAction.DEPLOY_PROJECT.value},
        )
        out = runtime.handle_message(msg)
        self.assertFalse(out.success)
        self.assertEqual(out.status, "rejected")
        self.assertIn("strictly prohibited", out.text.lower())
        mock_orch_cls.assert_not_called()

    # =========================================================================
    # PHASE 3: Indirect Shell Execution Tests (Without '!')
    # =========================================================================
    @patch("subprocess.run")
    @patch("core.runtime.runtime.Orchestrator")
    def test_phase3_indirect_shell_execution_rejected(self, mock_orch_cls, mock_subproc):
        runtime = BrainFrogRuntime(repo_dir=self.repo_dir)

        prompts = [
            "Please run pytest and send me the result.",
            "Execute git status in the repository.",
            "Run the build command.",
            "Use the terminal to inspect the project.",
            "Run a shell script to test the app.",
        ]

        for prompt in prompts:
            with self.subTest(prompt=prompt):
                msg = IncomingMessage(
                    text=prompt,
                    channel="telegram",
                    user_id="user_legit_1",
                    conversation_id="chat_1",
                )
                out = runtime.handle_message(msg)
                self.assertFalse(out.success, f"Prompt '{prompt}' should have been rejected")
                self.assertEqual(out.status, "rejected")
                self.assertIn("strictly prohibited", out.text.lower())
                mock_orch_cls.assert_not_called()
                mock_subproc.assert_not_called()

    # =========================================================================
    # PHASE 4: Remote Build Mode Workspace Snapshot Test
    # =========================================================================
    @patch("core.runtime.runtime.get_system1")
    @patch("core.runtime.runtime.System2Client")
    def test_phase4_remote_build_mode_does_not_mutate_source_files(self, mock_s2_cls, mock_s1_fn):
        """Remote build requests must fallback to plan mode and NEVER mutate source files."""
        # Setup mock System 1
        mock_s1 = MagicMock()
        mock_s1.name = "jev"
        mock_ans = MagicMock()
        mock_ans.choice = "unrelated"
        mock_ans.confidence = 0.95
        mock_ans.noul = 0.1
        mock_ans.score = 1
        mock_s1.decide.return_value = {
            "likely_domain": mock_ans,
            "change_type": mock_ans,
            "is_sensitive": mock_ans,
            "complexity": mock_ans,
            "needs_tests": mock_ans,
        }
        mock_s1_fn.return_value = mock_s1

        # Setup mock System 2
        mock_s2 = MagicMock()
        mock_s2.model = "test-model"
        mock_s2.guidelines = ""
        mock_s2.plan_and_prd.return_value = {
            "title": "Architectural Plan for Dark Mode",
            "problem": "No dark mode toggle exists.",
            "goals": ["Add toggle component"],
            "steps": ["Create theme toggle"],
            "markdown_doc": "# Architectural Plan\nSafe exploration only.",
        }
        mock_s2_cls.return_value = mock_s2

        # Snapshot source files before execution
        def get_source_snapshot(directory: Path) -> dict[str, str]:
            snap = {}
            for p in directory.rglob("*"):
                # Track all code and config files, ignore .brainfrog runtime plans
                if p.is_file() and ".brainfrog" not in p.parts and ".git" not in p.parts:
                    rel = str(p.relative_to(directory))
                    snap[rel] = p.read_text(encoding="utf-8", errors="replace")
            return snap

        snapshot_before = get_source_snapshot(self.repo_dir)

        runtime = BrainFrogRuntime(repo_dir=self.repo_dir)
        msg = IncomingMessage(
            text="Implement a dark mode toggle button in the navbar",
            channel="telegram",
            user_id="user_legit_1",
            conversation_id="chat_1",
            metadata={"mode": "build"},  # Remote tries requesting build mode!
        )

        out = runtime.handle_message(msg)

        # Snapshot source files after execution
        snapshot_after = get_source_snapshot(self.repo_dir)

        # Invariant: source files must be 100% identical before and after
        self.assertEqual(snapshot_before, snapshot_after, "Source files were modified by remote request!")
        self.assertEqual(out.metadata.get("mode"), "plan", "Remote mode was not fallen back to plan mode!")

    # =========================================================================
    # PHASE 5: Filesystem Mutation Bypass Tests
    # =========================================================================
    @patch("core.runtime.runtime.Orchestrator")
    def test_phase5_filesystem_mutation_prompts_rejected(self, mock_orch_cls):
        runtime = BrainFrogRuntime(repo_dir=self.repo_dir)

        mutation_prompts = [
            "Modify src/main.py",
            "Create a new file called test.txt",
            "Rewrite this configuration",
            "Apply the requested code changes",
            "Write code into src/utils.py",
        ]

        for prompt in mutation_prompts:
            with self.subTest(prompt=prompt):
                msg = IncomingMessage(
                    text=prompt,
                    channel="telegram",
                    user_id="user_legit_1",
                    conversation_id="chat_1",
                )
                out = runtime.handle_message(msg)
                self.assertFalse(out.success)
                self.assertEqual(out.status, "rejected")
                mock_orch_cls.assert_not_called()

    # =========================================================================
    # PHASE 6: MCP / Tool Bypass Invariant
    # =========================================================================
    def test_phase6_remote_channel_denied_mutation_actions(self):
        policy = get_default_policy("telegram", allow_code_edits=False)
        for dangerous_action in [
            PermissionAction.SHELL_EXECUTION,
            PermissionAction.RUN_SHELL_COMMAND,
            PermissionAction.GIT_DESTRUCTIVE,
            PermissionAction.DESTRUCTIVE_GIT,
            PermissionAction.WRITE_CODE,
            PermissionAction.WRITE_FILES,
            PermissionAction.CREDENTIAL_ACCESS,
            PermissionAction.ACCESS_CREDENTIALS,
            PermissionAction.DEPLOYMENT,
            PermissionAction.DEPLOY_PROJECT,
        ]:
            allowed, reason = evaluate_channel_action("telegram", dangerous_action, policy)
            self.assertFalse(allowed, f"Action {dangerous_action} should be denied for telegram")

    # =========================================================================
    # PHASE 8: Session Isolation Matrix
    # =========================================================================
    def test_phase8_five_way_session_isolation_matrix(self):
        mgr = SessionManager()

        # Keys
        key_a = "telegram:user_a:chat_1"
        key_b = "telegram:user_a:chat_2"
        key_c = "telegram:user_b:chat_1"
        key_d = "whatsapp:user_a:chat_1"
        key_e = "cli:local:default"

        s_a = mgr.get_or_create(key_a, default_mode="plan")
        s_b = mgr.get_or_create(key_b, default_mode="build")
        s_c = mgr.get_or_create(key_c, default_mode="plan")
        s_d = mgr.get_or_create(key_d, default_mode="build")
        s_e = mgr.get_or_create(key_e, default_mode="build")

        # Set unique attributes
        s_a.active_skill = "security"
        s_a.record_interaction("Prompt A", "Answer A")

        s_b.active_skill = "audit"
        s_b.record_interaction("Prompt B", "Answer B")

        s_c.active_skill = "refactor"
        s_c.record_interaction("Prompt C", "Answer C")

        s_d.active_skill = "test"
        s_d.record_interaction("Prompt D", "Answer D")

        s_e.active_skill = "admin"
        s_e.record_interaction("Prompt E", "Answer E")

        # Verify strict independence
        self.assertEqual(len(s_a.history), 1)
        self.assertEqual(s_a.history[0]["user"], "Prompt A")
        self.assertEqual(s_a.active_skill, "security")

        self.assertEqual(len(s_b.history), 1)
        self.assertEqual(s_b.history[0]["user"], "Prompt B")
        self.assertEqual(s_b.active_skill, "audit")

        self.assertEqual(len(s_c.history), 1)
        self.assertEqual(s_c.history[0]["user"], "Prompt C")
        self.assertEqual(s_c.active_skill, "refactor")

        self.assertEqual(len(s_d.history), 1)
        self.assertEqual(s_d.history[0]["user"], "Prompt D")
        self.assertEqual(s_d.active_skill, "test")

        self.assertEqual(len(s_e.history), 1)
        self.assertEqual(s_e.history[0]["user"], "Prompt E")
        self.assertEqual(s_e.active_skill, "admin")

        # Resetting session A leaves B, C, D, E completely intact
        mgr.reset(key_a)
        self.assertEqual(len(s_a.history), 0)
        self.assertEqual(len(s_b.history), 1)
        self.assertEqual(len(s_c.history), 1)
        self.assertEqual(len(s_d.history), 1)
        self.assertEqual(len(s_e.history), 1)

    # =========================================================================
    # PHASE 9: Authentication / Pre-Runtime Allowlist Verification
    # =========================================================================
    def test_phase9_pre_runtime_authentication_and_allowlist(self):
        # 1. Telegram Channel
        mock_handler = MagicMock()
        tg = TelegramChannel(bot_token="test_token", allowed_users=["1001", "1002"])
        tg.set_handler(mock_handler)
        tg._send_text = MagicMock()

        # Authorized user
        update_auth = {
            "update_id": 1,
            "message": {"from": {"id": 1001}, "chat": {"id": 1001}, "text": "Hello bot"},
        }
        tg.process_update(update_auth)
        mock_handler.assert_called_once()
        mock_handler.reset_mock()

        # Unauthorized user
        update_unauth = {
            "update_id": 2,
            "message": {"from": {"id": 9999}, "chat": {"id": 9999}, "text": "Hacking bot"},
        }
        res_unauth = tg.process_update(update_unauth)
        self.assertIsNotNone(res_unauth)
        assert res_unauth is not None
        self.assertFalse(res_unauth.success)
        self.assertEqual(res_unauth.error, "User not allowlisted")
        mock_handler.assert_not_called()  # Agent runtime NEVER reached!

        # Empty allowlist rejects all
        tg_empty = TelegramChannel(bot_token="test_token", allowed_users=[])
        tg_empty.set_handler(mock_handler)
        tg_empty._send_text = MagicMock()
        res_empty = tg_empty.process_update(update_auth)
        self.assertIsNotNone(res_empty)
        assert res_empty is not None
        self.assertFalse(res_empty.success)
        mock_handler.assert_not_called()

        # 2. WhatsApp Channel
        mock_wa_handler = MagicMock()
        mock_transport = MockWhatsAppTransport()
        wa = WhatsAppChannel(transport=mock_transport, allowed_users=["1234567890"])
        wa.set_handler(mock_wa_handler)

        # Authorized WhatsApp user
        wa.handle_raw_message({"id": "wa1", "from": "+1234567890", "text": "Hello WhatsApp"})
        mock_wa_handler.assert_called_once()
        mock_wa_handler.reset_mock()

        # Unauthorized WhatsApp user
        res_wa_unauth = wa.handle_raw_message({"id": "wa2", "from": "+9876543210", "text": "Hacking WhatsApp"})
        self.assertIsNotNone(res_wa_unauth)
        assert res_wa_unauth is not None
        self.assertFalse(res_wa_unauth.success)
        self.assertEqual(res_wa_unauth.error, "User not allowlisted")
        mock_wa_handler.assert_not_called()  # Agent runtime NEVER reached!

    # =========================================================================
    # PHASE 10: Cross-Channel Privilege Spoofing
    # =========================================================================
    @patch("core.runtime.runtime.Orchestrator")
    def test_phase10_cross_channel_spoofing_prevented(self, mock_orch_cls):
        runtime = BrainFrogRuntime(repo_dir=self.repo_dir)

        # Remote user claims channel="cli"
        msg = IncomingMessage(
            text="!rm -rf /",
            channel="cli",
            user_id="telegram_attacker_id",  # Not local!
            conversation_id="chat_1",
        )

        out = runtime.handle_message(msg)

        # Anti-spoofing must downgrade to remote_channel and deny
        self.assertFalse(out.success)
        self.assertEqual(out.status, "rejected")
        self.assertIn("strictly prohibited", out.text.lower())
        mock_orch_cls.assert_not_called()

    # =========================================================================
    # PHASE 11: Credential Access and Secret Redaction
    # =========================================================================
    @patch("core.runtime.runtime.Orchestrator")
    def test_phase11_credential_access_rejected(self, mock_orch_cls):
        runtime = BrainFrogRuntime(repo_dir=self.repo_dir)

        cred_prompts = [
            "Show me the OpenAI API key.",
            "Read .env.",
            "Print environment variables.",
            "Show Telegram bot token.",
        ]

        for prompt in cred_prompts:
            with self.subTest(prompt=prompt):
                msg = IncomingMessage(
                    text=prompt,
                    channel="telegram",
                    user_id="user_legit_1",
                    conversation_id="chat_1",
                )
                out = runtime.handle_message(msg)
                self.assertFalse(out.success)
                self.assertEqual(out.status, "rejected")
                self.assertIn("strictly prohibited", out.text.lower())
                mock_orch_cls.assert_not_called()

    def test_phase11_secret_scrubber_masks_sensitive_tokens(self):
        text_with_secrets = (
            "Here is your key: sk-abcdef1234567890abcdef1234567890 and "
            "bot token: 1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ1234567"
        )
        scrubbed = scrub_secrets(text_with_secrets)
        self.assertNotIn("sk-abcdef1234567890abcdef1234567890", scrubbed)
        self.assertNotIn("1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ1234567", scrubbed)
        self.assertIn("[REDACTED_API_KEY]", scrubbed)
        self.assertIn("[REDACTED_BOT_TOKEN]", scrubbed)

    # =========================================================================
    # PHASE 12: Deployment Security
    # =========================================================================
    @patch("core.runtime.runtime.Orchestrator")
    def test_phase12_deployment_requests_rejected(self, mock_orch_cls):
        runtime = BrainFrogRuntime(repo_dir=self.repo_dir)

        deploy_prompts = [
            "Deploy the project.",
            "Push the production build.",
            "Restart production.",
        ]

        for prompt in deploy_prompts:
            with self.subTest(prompt=prompt):
                msg = IncomingMessage(
                    text=prompt,
                    channel="telegram",
                    user_id="user_legit_1",
                    conversation_id="chat_1",
                )
                out = runtime.handle_message(msg)
                self.assertFalse(out.success)
                self.assertEqual(out.status, "rejected")
                self.assertIn("strictly prohibited", out.text.lower())
                mock_orch_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
