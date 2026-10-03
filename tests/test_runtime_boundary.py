"""Unit tests for BrainFrogRuntime boundary and session isolation."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

from core.runtime.messages import IncomingMessage
from core.runtime.runtime import BrainFrogRuntime
from core.runtime.session import SessionManager
from orchestrator import PlanStep, StepResult


class TestRuntimeBoundary(unittest.TestCase):
    def test_session_isolation_strictness(self):
        mgr = SessionManager()
        s_user1 = mgr.get_or_create("telegram", "user_100", "chat_a")
        s_user2 = mgr.get_or_create("telegram", "user_200", "chat_a")

        s_user1.record_interaction("what is project A?", "Project A is an API.")
        s_user2.record_interaction("what is secret B?", "Secret B is confidential.")

        # User 1 history must NOT be present in User 2
        self.assertEqual(len(s_user1.history), 1)
        self.assertEqual(len(s_user2.history), 1)
        self.assertEqual(s_user1.history[0]["user"], "what is project A?")
        self.assertEqual(s_user2.history[0]["user"], "what is secret B?")

        # Resetting User 1 must not alter User 2
        mgr.reset(s_user1.session_id)
        self.assertEqual(len(s_user1.history), 0)
        self.assertEqual(len(s_user2.history), 1)

    def test_remote_shell_execution_rejected(self):
        runtime = BrainFrogRuntime(repo_dir=Path.cwd(), persist_sessions=False)
        msg = IncomingMessage(
            text="!rm -rf /",
            channel="telegram",
            user_id="user_attacker",
            conversation_id="chat_1",
        )
        out = runtime.handle_message(msg)
        self.assertFalse(out.success)
        self.assertIn("strictly prohibited", out.error or "")
        self.assertIn("strictly prohibited", out.text)

    @patch("core.runtime.runtime.get_system1")
    @patch("core.runtime.runtime.System2Client")
    @patch("core.runtime.runtime.Orchestrator")
    def test_runtime_normal_flow_and_events(self, mock_orch_cls, mock_s2_cls, mock_s1_fn):
        mock_orch = MagicMock()
        mock_orch.run.return_value = [
            StepResult(PlanStep("0", "diagnose", []), "diagnosed", 0, "Here is the architectural diagnosis.")
        ]
        mock_orch_cls.return_value = mock_orch

        runtime = BrainFrogRuntime(repo_dir=Path.cwd(), persist_sessions=False)
        msg = IncomingMessage(
            text="How does auth work?",
            channel="cli",
            user_id="local_dev",
            conversation_id="default",
        )
        collected_events = []
        out = runtime.handle_message(msg, on_event=lambda ev: collected_events.append(ev))

        self.assertTrue(out.success)
        self.assertEqual(out.text, "Here is the architectural diagnosis.")
        self.assertTrue(len(collected_events) > 0)
        event_types = [e.type for e in collected_events]
        self.assertIn("agent.started", event_types)
        self.assertIn("agent.completed", event_types)


if __name__ == "__main__":
    unittest.main()
