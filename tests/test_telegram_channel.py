"""Unit tests for Telegram channel adapter and allowlist gating."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from core.channels.telegram import TelegramChannel
from core.runtime.messages import IncomingMessage, OutgoingMessage


class TestTelegramChannel(unittest.TestCase):
    def test_allowlist_matching(self):
        ch = TelegramChannel(bot_token="test_token", allowed_users=["12345", "@valid_user"])

        self.assertTrue(ch.is_allowed_user({"id": 12345, "username": "other"}))
        self.assertTrue(ch.is_allowed_user({"id": 99999, "username": "valid_user"}))
        self.assertFalse(ch.is_allowed_user({"id": 67890, "username": "intruder"}))

    def test_empty_allowlist_blocks_all_by_default(self):
        ch = TelegramChannel(bot_token="test_token", allowed_users=[])
        self.assertFalse(ch.is_allowed_user({"id": 12345, "username": "anyone"}))

    @patch("core.channels.telegram.TelegramChannel._send_text")
    def test_unauthorized_user_rejection(self, mock_send):
        ch = TelegramChannel(bot_token="test_token", allowed_users=["1001"])
        mock_handler = MagicMock()
        ch.set_handler(mock_handler)

        raw_update = {
            "update_id": 1,
            "message": {
                "from": {"id": 9999, "username": "hacker"},
                "chat": {"id": 7777, "type": "private"},
                "text": "format hard drive",
            },
        }

        res = ch.process_update(raw_update)
        self.assertFalse(res.success)
        self.assertEqual(res.error, "User not allowlisted")
        self.assertFalse(mock_handler.called)
        self.assertTrue(mock_send.called)
        call_text = mock_send.call_args[0][1]
        self.assertIn("Akses Ditolak", call_text)

    @patch("core.channels.telegram.TelegramChannel._send_text")
    def test_authorized_user_normal_routing(self, mock_send):
        ch = TelegramChannel(bot_token="test_token", allowed_users=["1001"])
        mock_handler = MagicMock()
        mock_handler.return_value = OutgoingMessage(text="Code analyzed.")
        ch.set_handler(mock_handler)

        raw_update = {
            "update_id": 2,
            "message": {
                "from": {"id": 1001, "username": "developer"},
                "chat": {"id": 5555, "type": "private"},
                "text": "Explain auth flow",
            },
        }

        res = ch.process_update(raw_update)
        self.assertTrue(res.success)
        self.assertEqual(res.text, "Code analyzed.")
        self.assertTrue(mock_handler.called)
        incoming_msg = mock_handler.call_args[0][0]
        self.assertIsInstance(incoming_msg, IncomingMessage)
        self.assertEqual(incoming_msg.user_id, "1001")
        self.assertEqual(incoming_msg.conversation_id, "5555")
        self.assertEqual(incoming_msg.session_id, "telegram:1001:5555")
        self.assertTrue(mock_send.called)

    def test_start_stop_lifecycle(self):
        ch = TelegramChannel(bot_token="test_token", allowed_users=["1001"])
        with patch.object(ch, "_poll_worker"):
            ch.start()
            self.assertTrue(ch.is_running)
            ch.stop()
            self.assertFalse(ch.is_running)


if __name__ == "__main__":
    unittest.main()
