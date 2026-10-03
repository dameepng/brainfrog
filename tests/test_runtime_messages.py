"""Unit tests for normalized runtime messages and events."""
from __future__ import annotations

import unittest
from core.runtime.messages import AgentEvent, IncomingMessage, OutgoingMessage


class TestRuntimeMessages(unittest.TestCase):
    def test_incoming_message_defaults(self):
        msg = IncomingMessage(text="halo brainfrog")
        self.assertEqual(msg.text, "halo brainfrog")
        self.assertEqual(msg.channel, "cli")
        self.assertEqual(msg.user_id, "local")
        self.assertEqual(msg.conversation_id, "default")
        self.assertEqual(msg.session_id, "cli:local:default")
        self.assertTrue(len(msg.id) > 0)
        self.assertIsInstance(msg.timestamp, float)
        self.assertEqual(msg.attachments, [])
        self.assertEqual(msg.metadata, {})

    def test_session_id_isolation(self):
        msg1 = IncomingMessage(text="task 1", channel="telegram", user_id="111", conversation_id="chat_a")
        msg2 = IncomingMessage(text="task 2", channel="telegram", user_id="222", conversation_id="chat_a")
        msg3 = IncomingMessage(text="task 3", channel="whatsapp", user_id="111", conversation_id="chat_a")

        self.assertEqual(msg1.session_id, "telegram:111:chat_a")
        self.assertEqual(msg2.session_id, "telegram:222:chat_a")
        self.assertEqual(msg3.session_id, "whatsapp:111:chat_a")

        # Distinct sessions must not match
        self.assertNotEqual(msg1.session_id, msg2.session_id)
        self.assertNotEqual(msg1.session_id, msg3.session_id)

    def test_incoming_message_serialization(self):
        msg = IncomingMessage(
            id="msg-123",
            text="check auth flow",
            channel="telegram",
            user_id="user_99",
            conversation_id="conv_88",
            timestamp=1700000000.0,
            attachments=["img1.png"],
            metadata={"mode": "plan"},
        )
        d = msg.to_dict()
        self.assertEqual(d["id"], "msg-123")
        self.assertEqual(d["channel"], "telegram")
        self.assertEqual(d["metadata"]["mode"], "plan")

        restored = IncomingMessage.from_dict(d)
        self.assertEqual(restored.id, "msg-123")
        self.assertEqual(restored.session_id, "telegram:user_99:conv_88")
        self.assertEqual(restored.attachments, ["img1.png"])

    def test_agent_event_serialization(self):
        event = AgentEvent(
            type="agent.step.started",
            timestamp=1700000001.0,
            payload={"step_id": "1", "description": "Update config"},
        )
        d = event.to_dict()
        self.assertEqual(d["type"], "agent.step.started")
        self.assertEqual(d["payload"]["step_id"], "1")

        restored = AgentEvent.from_dict(d)
        self.assertEqual(restored.type, "agent.step.started")
        self.assertEqual(restored.payload["description"], "Update config")

    def test_outgoing_message_serialization(self):
        ev = AgentEvent(type="agent.completed", payload={"steps": 2})
        out = OutgoingMessage(
            text="Task completed successfully.",
            metadata={"cost_usd": 0.02},
            events=[ev],
            success=True,
        )
        d = out.to_dict()
        self.assertTrue(d["success"])
        self.assertEqual(len(d["events"]), 1)
        self.assertEqual(d["events"][0]["type"], "agent.completed")

        restored = OutgoingMessage.from_dict(d)
        self.assertEqual(restored.text, "Task completed successfully.")
        self.assertTrue(restored.success)
        self.assertEqual(len(restored.events), 1)
        self.assertIsInstance(restored.events[0], AgentEvent)
        self.assertEqual(restored.events[0].type, "agent.completed")


if __name__ == "__main__":
    unittest.main()
