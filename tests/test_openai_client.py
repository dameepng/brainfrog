"""Unit tests for OpenAISystem2Client and OpenAI-compatible provider resolution."""
from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from system2 import System2Client, get_system2_provider
from system2.openai_client import OpenAISystem2Client


class TestOpenAISystem2Client(unittest.TestCase):
    def test_provider_resolution(self):
        self.assertEqual(get_system2_provider("openai"), "openai")
        self.assertEqual(get_system2_provider("openrouter"), "openrouter")
        self.assertEqual(get_system2_provider("openai-compatible"), "openai-compatible")
        self.assertEqual(get_system2_provider("custom"), "openai-compatible")
        self.assertEqual(get_system2_provider("ollama"), "openai-compatible")
        self.assertEqual(get_system2_provider("claude"), "claude")
        self.assertEqual(get_system2_provider("antigravity"), "antigravity")

    def test_missing_api_key_raises_error(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                OpenAISystem2Client(provider_name="openai")
            self.assertIn("API key missing", str(ctx.exception))

    def test_client_initialization_with_custom_values(self):
        client = OpenAISystem2Client(
            model="my-custom-model",
            api_key="sk-testkey123",
            base_url="https://llm.internal.net/v1/",
            provider_name="custom",
        )
        self.assertEqual(client.model, "my-custom-model")
        self.assertEqual(client.api_key, "sk-testkey123")
        self.assertEqual(client.base_url, "https://llm.internal.net/v1")
        self.assertEqual(client.provider_name, "custom")

    def test_factory_instantiation(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-dummy-123"}):
            client = System2Client(provider="openai", model="gpt-4o-mini")
            self.assertIsInstance(client, OpenAISystem2Client)
            self.assertEqual(client.model, "gpt-4o-mini")

    @patch("requests.post")
    def test_plan_task_mocked(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{
                "message": {
                    "content": '{"steps": [{"id": "1", "description": "Update route", "files": ["src/route.py"]}]}'
                }
            }],
            "usage": {"prompt_tokens": 120, "completion_tokens": 40},
        }
        mock_post.return_value = mock_resp

        client = OpenAISystem2Client(
            api_key="sk-test",
            model="gpt-4o",
            base_url="https://api.openai.com/v1",
        )
        steps = client.plan_task("Add route", repo_tree="src/route.py")

        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].id, "1")
        self.assertEqual(steps[0].files, ["src/route.py"])
        self.assertTrue(mock_post.called)
        call_url = mock_post.call_args[0][0]
        self.assertEqual(call_url, "https://api.openai.com/v1/chat/completions")

    @patch("requests.post")
    def test_diagnose_mocked(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{
                "message": {
                    "content": "Sistem autentikasi menggunakan JWT token."
                }
            }],
            "usage": {"prompt_tokens": 80, "completion_tokens": 20},
        }
        mock_post.return_value = mock_resp

        client = OpenAISystem2Client(
            api_key="sk-test",
            model="gpt-4o",
        )
        answer = client.diagnose("Bagaimana auth bekerja?", focus_files={}, domain="auth")
        self.assertIn("JWT token", answer)

    @patch("requests.post")
    def test_redirect_responses_not_followed(self, mock_post):
        for code in (301, 302, 307, 308):
            with self.subTest(status_code=code):
                mock_post.reset_mock()
                mock_resp = MagicMock()
                mock_resp.status_code = code
                mock_resp.is_redirect = True
                mock_resp.headers = {"Location": "http://169.254.169.254/latest/meta-data/"}
                mock_post.return_value = mock_resp

                client = OpenAISystem2Client(api_key="mock-test-key", model="gpt-4o")
                with self.assertRaises(RuntimeError) as ctx:
                    client.diagnose("test redirect", focus_files={}, domain="auth")

                self.assertIn("redirected", str(ctx.exception).lower())
                self.assertIn("prohibited", str(ctx.exception).lower())
                self.assertEqual(mock_post.call_count, 1)
                self.assertFalse(mock_post.call_args[1].get("allow_redirects", True))

    @patch("requests.post")
    def test_non_redirect_200_remains_compatible(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.is_redirect = False
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        mock_post.return_value = mock_resp
        client = OpenAISystem2Client(api_key="mock-test-key", model="gpt-4o")
        res = client.diagnose("hello", focus_files={}, domain="auth")
        self.assertEqual(res, "ok")
        self.assertEqual(mock_post.call_count, 1)
        self.assertFalse(mock_post.call_args[1].get("allow_redirects", True))


if __name__ == "__main__":
    unittest.main()
