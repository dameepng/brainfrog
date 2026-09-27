"""Tests for real System 1 (Jev / TypeSafe) client and backend selection."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from core.config import get_system1
from system1 import (
    Answer,
    ChoiceQuestion,
    JevSystemOne,
    NoulQuestion,
    ScoreQuestion,
    TypeSafeSystemOne,
)
from system1.typesafe_client import _question_to_json


class TestSystem1Jev(unittest.TestCase):
    def test_alias(self):
        self.assertIs(JevSystemOne, TypeSafeSystemOne)

    def test_question_serialization(self):
        choice = ChoiceQuestion(
            instructions="Pick one",
            criteria={"a": "first choice", "b": "second choice"},
        )
        self.assertEqual(
            _question_to_json(choice),
            {"type": "choice", "instructions": "Pick one", "criteria": {"a": "first choice", "b": "second choice"}},
        )

        score = ScoreQuestion(instructions="Rate this", scale="1-5")
        self.assertEqual(
            _question_to_json(score),
            {"type": "score", "instructions": "Rate this", "scale": "1-5"},
        )

        noul = NoulQuestion(instructions="Extract entity")
        self.assertEqual(
            _question_to_json(noul),
            {"type": "noul", "instructions": "Extract entity"},
        )

    @patch.dict("os.environ", {}, clear=True)
    def test_init_missing_key_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            TypeSafeSystemOne()
        self.assertIn("Jev API key not found", str(ctx.exception))

    @patch.dict("os.environ", {"JEV_API_KEY": "test_jev_key"}, clear=True)
    def test_init_with_jev_key(self):
        client = TypeSafeSystemOne()
        self.assertEqual(client.api_key, "test_jev_key")
        self.assertEqual(client.name, "jev")

    @patch.dict("os.environ", {"TYPESAFE_API_KEY": "test_typesafe_key"}, clear=True)
    def test_get_system1_backends(self):
        client_default = get_system1()
        self.assertIsInstance(client_default, TypeSafeSystemOne)

        client_jev = get_system1("jev")
        self.assertIsInstance(client_jev, TypeSafeSystemOne)

        client_typesafe = get_system1("typesafe")
        self.assertIsInstance(client_typesafe, TypeSafeSystemOne)

        client_auto = get_system1("auto")
        self.assertIsInstance(client_auto, TypeSafeSystemOne)

    def test_get_system1_mock_raises_error(self):
        with self.assertRaises(ValueError) as ctx:
            get_system1("mock")
        self.assertIn("Mock System 1 has been removed", str(ctx.exception))

    def test_get_system1_unknown_raises_error(self):
        with self.assertRaises(ValueError) as ctx:
            get_system1("invalid_xyz")
        self.assertIn("Unknown backend", str(ctx.exception))

    @patch("requests.post")
    def test_decide_payload_and_response(self, mock_post):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "answers": {
                "route": {
                    "choice": "backend",
                    "confidence": 0.95,
                },
                "risk": {
                    "score": "low",
                    "confidence": 0.88,
                },
            }
        }
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        client = TypeSafeSystemOne(api_key="fake_key_123")
        state = {"task": "fix auth bug"}
        questions = {
            "route": ChoiceQuestion(instructions="Route task", criteria={"backend": "Backend", "frontend": "Frontend"}),
        }

        answers = client.decide(state, questions)

        self.assertIn("route", answers)
        self.assertEqual(answers["route"].choice, "backend")
        self.assertEqual(answers["route"].confidence, 0.95)
        self.assertIn("risk", answers)
        self.assertEqual(answers["risk"].score, "low")

        # Verify call arguments
        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer fake_key_123")
        self.assertEqual(kwargs["json"]["state"], state)
        self.assertIn("route", kwargs["json"]["questions"])


if __name__ == "__main__":
    unittest.main()
