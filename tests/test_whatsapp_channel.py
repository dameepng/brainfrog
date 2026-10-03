"""Unit tests for WhatsApp channel adapter and transport abstraction."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from core.channels.whatsapp import MockWhatsAppTransport, WhatsAppChannel
from core.runtime.messages import IncomingMessage, OutgoingMessage


class TestWhatsAppChannel(unittest.TestCase):
    def test_allowlist_matching(self):
        ch = WhatsAppChannel(allowed_users=["+62812345678", "14155552671"])
        self.assertTrue(ch.is_allowed_user("+62812345678"))
        self.assertTrue(ch.is_allowed_user("62812345678"))
        self.assertTrue(ch.is_allowed_user("+14155552671"))
        self.assertFalse(ch.is_allowed_user("+62899999999"))

    def test_unauthorized_user_blocked(self):
        transport = MockWhatsAppTransport()
        ch = WhatsAppChannel(transport=transport, allowed_users=["+62812345678"])
        mock_handler = MagicMock()
        ch.set_handler(mock_handler)

        res = ch.handle_raw_message({
            "id": "msg-001",
            "from": "+62899999999",
            "text": "run dangerous script",
        })

        self.assertFalse(res.success)
        self.assertEqual(res.error, "User not allowlisted")
        self.assertFalse(mock_handler.called)

        # Verification message sent via transport
        self.assertEqual(len(transport.sent_messages), 1)
        self.assertIn("Akses Ditolak", transport.sent_messages[0]["text"])

    def test_authorized_user_routing_and_session(self):
        transport = MockWhatsAppTransport()
        ch = WhatsAppChannel(transport=transport, allowed_users=["+62812345678"])
        mock_handler = MagicMock()
        mock_handler.return_value = OutgoingMessage(text="Status: All services green.")
        ch.set_handler(mock_handler)

        res = ch.handle_raw_message({
            "id": "msg-002",
            "from": "+62812345678",
            "text": "What is system status?",
        })

        self.assertTrue(res.success)
        self.assertEqual(res.text, "Status: All services green.")
        self.assertTrue(mock_handler.called)

        incoming: IncomingMessage = mock_handler.call_args[0][0]
        self.assertEqual(incoming.channel, "whatsapp")
        self.assertEqual(incoming.user_id, "62812345678")
        self.assertEqual(incoming.session_id, "whatsapp:62812345678:62812345678")

        # Response delivered back
        self.assertEqual(len(transport.sent_messages), 1)
        self.assertEqual(transport.sent_messages[0]["text"], "Status: All services green.")

    def test_transport_lifecycle(self):
        transport = MockWhatsAppTransport()
        ch = WhatsAppChannel(transport=transport)
        ch.start()
        self.assertTrue(ch.is_running)
        ch.stop()
        self.assertFalse(ch.is_running)


if __name__ == "__main__":
    unittest.main()
