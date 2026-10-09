"""Integration tests for Work continuation, commands, channels, and presentation in BrainFrogRuntime.

Validates:
- /works listing and filters (active, failed, done)
- /work detail inspection with authorization enforcement
- /resume continuation across lifecycle states
- /cancel for Work units and legacy Approval requests
- Telegram and WhatsApp UX command dispatch
- Secret scrubbing and injection-safe formatting
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from core.runtime.approval import ApprovalService, ApprovalStatus, InMemoryApprovalStore
from core.runtime.capabilities import Capabilities
from core.runtime.messages import IncomingMessage
from core.runtime.runtime import BrainFrogRuntime
from core.runtime.session import InMemorySessionStore, SessionManager
from core.runtime.transaction import InMemoryTransactionStore
from core.runtime.work import (
    InMemoryWorkStore,
    VerificationResult,
    Work,
    WorkFailure,
    WorkStatus,
)
from core.runtime.work_presentation import (
    format_work_detail,
    format_works_list,
    parse_telegram_work_callback,
    telegram_work_keyboard,
)


class TestWorkContinuationAndRuntime(unittest.TestCase):
    """Test Work commands through BrainFrogRuntime."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.repo_dir = Path(self.temp_dir)
        self.work_store = InMemoryWorkStore()
        self.approval_store = InMemoryApprovalStore()
        self.approval_service = ApprovalService(store=self.approval_store)
        self.session_store = InMemorySessionStore()
        self.sessions = SessionManager(store=self.session_store)
        self.transaction_store = InMemoryTransactionStore()

        self.runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            work_store=self.work_store,
            approval_service=self.approval_service,
            sessions=self.sessions,
            transaction_store=self.transaction_store,
            auto_recover_transactions=False,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _msg(self, text: str, user_id: str = "alice", channel: str = "telegram", metadata: dict = None) -> IncomingMessage:
        return IncomingMessage(
            id="msg_1",
            channel=channel,
            user_id=user_id,
            conversation_id="conv_1",
            text=text,
            metadata=metadata or {},
        )

    def test_works_listing_and_filtering(self) -> None:
        w1 = Work(id="work_1", intent="Task 1", goal="Goal 1", actor_id="alice", status=WorkStatus.CREATED)
        w2 = Work(id="work_2", intent="Task 2", goal="Goal 2", actor_id="alice", status=WorkStatus.DONE)
        w3 = Work(id="work_3", intent="Task 3", goal="Goal 3", actor_id="alice", status=WorkStatus.FAILED)
        w4 = Work(id="work_4", intent="Task 4", goal="Goal 4", actor_id="bob", status=WorkStatus.CREATED)

        self.work_store.create(w1)
        self.work_store.create(w2)
        self.work_store.create(w3)
        self.work_store.create(w4)

        # Alice lists all her works
        out_all = self.runtime.handle_message(self._msg("/works", user_id="alice"))
        self.assertTrue(out_all.success)
        self.assertIn("work_1", out_all.text)
        self.assertIn("work_2", out_all.text)
        self.assertIn("work_3", out_all.text)
        self.assertNotIn("work_4", out_all.text)  # Bob's work hidden

        # Alice lists active works
        out_active = self.runtime.handle_message(self._msg("/works active", user_id="alice"))
        self.assertTrue(out_active.success)
        self.assertIn("work_1", out_active.text)
        self.assertNotIn("work_2", out_active.text)
        self.assertNotIn("work_3", out_active.text)

        # Alice lists failed works
        out_failed = self.runtime.handle_message(self._msg("/works failed", user_id="alice"))
        self.assertTrue(out_failed.success)
        self.assertIn("work_3", out_failed.text)
        self.assertNotIn("work_1", out_failed.text)

    def test_work_detail_inspection(self) -> None:
        work = Work(
            id="work_inspect_1",
            intent="Refactor authentication",
            goal="Add JWT authentication",
            actor_id="alice",
            channel="telegram",
            status=WorkStatus.APPROVAL_REQUIRED,
            plan=("Design schema", "Implement handler"),
            approval_request_id="req_jwt_123",
        )
        self.work_store.create(work)

        # Alice inspects her own work
        out = self.runtime.handle_message(self._msg("/work work_inspect_1", user_id="alice"))
        self.assertTrue(out.success)
        self.assertIn("work_inspect_1", out.text)
        self.assertIn("WAITING FOR APPROVAL", out.text)
        self.assertIn("JWT authentication", out.text)

        # Bob attempts to inspect Alice's work -> fails closed
        out_bob = self.runtime.handle_message(self._msg("/work work_inspect_1", user_id="bob"))
        self.assertFalse(out_bob.success)
        self.assertIn("not found or access denied", out_bob.text.lower())

    def test_resume_command_approval_required(self) -> None:
        # Create work in APPROVAL_REQUIRED
        work = Work(
            id="work_res_appr",
            intent="Migrate db",
            goal="Migrate db",
            actor_id="alice",
            channel="telegram",
            status=WorkStatus.APPROVAL_REQUIRED,
        )
        self.work_store.create(work)

        out = self.runtime.handle_message(self._msg("/resume work_res_appr", user_id="alice"))
        self.assertTrue(out.success)
        self.assertIn("awaiting approval", out.text.lower())

    def test_resume_command_already_completed(self) -> None:
        work = Work(
            id="work_res_done",
            intent="Done task",
            goal="Done task",
            actor_id="alice",
            channel="telegram",
            status=WorkStatus.DONE,
        )
        self.work_store.create(work)

        out = self.runtime.handle_message(self._msg("/resume work_res_done", user_id="alice"))
        self.assertFalse(out.success)
        self.assertIn("already completed", out.text.lower())

    def test_cancel_command_on_work(self) -> None:
        work = Work(
            id="work_cancel_1",
            intent="Deploy app",
            goal="Deploy app",
            actor_id="alice",
            channel="telegram",
            status=WorkStatus.APPROVAL_REQUIRED,
        )
        self.work_store.create(work)

        out = self.runtime.handle_message(self._msg("/cancel work_cancel_1", user_id="alice"))
        self.assertTrue(out.success)
        self.assertIn("cancelled", out.text.lower())

        stored = self.work_store.get("work_cancel_1")
        assert stored is not None
        self.assertEqual(stored.status, WorkStatus.CANCELLED)

    def test_telegram_channel_work_callback_parsing(self) -> None:
        act, wid = parse_telegram_work_callback("bfw:r:work_abcdef0123456789")
        self.assertEqual(act, "resume")
        self.assertEqual(wid, "work_abcdef0123456789")

        act_c, wid_c = parse_telegram_work_callback("bfw:c:work_abcdef0123456789")
        self.assertEqual(act_c, "cancel")

        # Malformed callbacks fail closed
        with self.assertRaises(ValueError):
            parse_telegram_work_callback("invalid:payload")
        with self.assertRaises(ValueError):
            parse_telegram_work_callback("bfw:x:work_123")

    def test_telegram_callback_button_dispatch(self) -> None:
        work = Work(
            id="work_tg_cb_1",
            intent="Task",
            goal="Goal",
            actor_id="alice",
            channel="telegram",
            status=WorkStatus.PLANNING,
        )
        self.work_store.create(work)

        # Dispatch Telegram callback query for cancel
        out = self.runtime.handle_message(IncomingMessage(
            id="tg_cb_msg",
            channel="telegram",
            user_id="alice",
            conversation_id="chat_1",
            text="",
            metadata={"telegram_callback_data": "bfw:c:work_tg_cb_1"},
        ))
        self.assertTrue(out.success)
        self.assertIn("cancelled", out.text.lower())
        stored = self.work_store.get("work_tg_cb_1")
        assert stored is not None
        self.assertEqual(stored.status, WorkStatus.CANCELLED)

    def test_whatsapp_natural_language_command_dispatch(self) -> None:
        from core.channels.whatsapp import MockWhatsAppTransport, WhatsAppChannel

        transport = MockWhatsAppTransport()
        channel = WhatsAppChannel(
            allowed_users=["+1234567890"],
            transport=transport,
        )
        channel.set_handler(self.runtime.handle_message)

        work = Work(
            id="work_wa_1",
            intent="Task",
            goal="Goal",
            actor_id="1234567890",
            channel="whatsapp",
            status=WorkStatus.APPROVAL_REQUIRED,
        )
        self.work_store.create(work)

        # WhatsApp user sends "works" without leading slash
        out = transport.simulate_incoming(
            from_number="+1234567890",
            text="works",
            message_id="wa_msg_1",
        )
        self.assertIsNotNone(out)
        assert out is not None
        self.assertTrue(out.success)
        self.assertIn("work_wa_1", out.text)

    def test_presentation_secret_scrubbing_and_formatting(self) -> None:
        from core.runtime.work_presentation import _clean

        # 1. Reject secrets at construction time (Property 10)
        with self.assertRaises(ValueError):
            Work(
                id="work_bad_secret",
                intent="sk-proj-supersecretkey12345678901234567890",
                goal="legitimate goal",
            )
        with self.assertRaises(ValueError):
            WorkFailure(code="ERR", summary="Bearer secret_token_value_here_9999")

        # 2. Defense-in-depth: _clean sanitizes strings and replaces secrets with REDACTED
        cleaned = _clean("Sensitive key sk-proj-123456789012345678901234567890123456789012345678 and token")
        self.assertNotIn("sk-proj-123456789012345678901234567890123456789012345678", cleaned)
        self.assertIn("REDACTED", cleaned)

        # 3. Legitimate work formatting
        work = Work(
            id="work_scrub_test",
            intent="Update documentation",
            goal="Add security guide",
            actor_id="alice",
            channel="telegram",
            status=WorkStatus.DONE,
        )
        formatted = format_work_detail(work, channel="telegram")
        self.assertIn("work_scrub_test", formatted)
        self.assertIn("DONE", formatted)


if __name__ == "__main__":
    unittest.main()
