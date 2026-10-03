"""Unit tests for BrainFrogGateway multi-channel coordinator."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from core.channels.whatsapp import MockWhatsAppTransport, WhatsAppChannel
from core.runtime.gateway import BrainFrogGateway
from core.runtime.runtime import BrainFrogRuntime


class TestBrainFrogGateway(unittest.TestCase):
    def test_gateway_initialization_and_status(self):
        transport = MockWhatsAppTransport()
        wa_ch = WhatsAppChannel(transport=transport, allowed_users=["12345"])

        runtime = MagicMock(spec=BrainFrogRuntime)
        runtime.default_backend = "jev"
        runtime.default_provider = "claude"

        gateway = BrainFrogGateway(
            runtime=runtime,
            channels=[wa_ch],
            auto_configure_channels=False,
        )

        status = gateway.get_status()
        self.assertFalse(status["gateway_running"])
        self.assertEqual(status["channels_count"], 1)
        self.assertEqual(status["channels"][0]["name"], "whatsapp")
        self.assertFalse(status["channels"][0]["running"])

    def test_gateway_start_and_stop_lifecycle(self):
        transport = MockWhatsAppTransport()
        wa_ch = WhatsAppChannel(transport=transport)

        runtime = MagicMock(spec=BrainFrogRuntime)
        runtime.default_backend = "jev"
        runtime.default_provider = "claude"
        gateway = BrainFrogGateway(
            runtime=runtime,
            channels=[wa_ch],
            auto_configure_channels=False,
        )

        gateway.start()
        self.assertTrue(gateway.is_running)
        self.assertTrue(wa_ch.is_running)

        status = gateway.get_status()
        self.assertTrue(status["gateway_running"])
        self.assertTrue(status["channels"][0]["running"])

        gateway.stop()
        self.assertFalse(gateway.is_running)
        self.assertFalse(wa_ch.is_running)


if __name__ == "__main__":
    unittest.main()
