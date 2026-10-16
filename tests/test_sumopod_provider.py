"""Unit tests for Sumopod System 2 Provider integration."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.runtime.doctor import run_doctor_diagnostics
from core.runtime.secret_scrubbing import scrub_text_patterns, scrub_secrets
from system2 import (
    AntigravityOptInRequiredError,
    AntigravitySystem2Client,
    ClaudeSystem2Client,
    System2Client,
    get_system2_provider,
    validate_system2_provider_preflight,
)
from system2.openai_client import (
    DEFAULT_SUMOPOD_BASE_URL,
    DEFAULT_SUMOPOD_MODEL,
    OpenAISystem2Client,
)


class TestSumopodProvider(unittest.TestCase):
    def test_provider_resolution_explicit(self):
        """Explicit provider name or alias resolves to 'sumopod'."""
        self.assertEqual(get_system2_provider("sumopod"), "sumopod")
        self.assertEqual(get_system2_provider("sumo"), "sumopod")
        self.assertEqual(get_system2_provider("SUMOPOD"), "sumopod")
        self.assertEqual(get_system2_provider("  sumo  "), "sumopod")

    def test_provider_resolution_env_var(self):
        """SYSTEM2_PROVIDER environment variable resolves to 'sumopod'."""
        with patch.dict(os.environ, {"SYSTEM2_PROVIDER": "sumopod"}, clear=True):
            self.assertEqual(get_system2_provider(), "sumopod")

        with patch.dict(os.environ, {"SYSTEM2_PROVIDER": "sumo"}, clear=True):
            self.assertEqual(get_system2_provider(), "sumopod")

        with patch.dict(os.environ, {"SYSTEM2_PROVIDER": "SUMOPOD"}, clear=True):
            self.assertEqual(get_system2_provider(), "sumopod")

    def test_provider_resolution_auto_detection(self):
        """Auto-detection selects 'sumopod' when SUMOPOD_API_KEY is present and ANTHROPIC_API_KEY is not."""
        with patch.dict(os.environ, {"SUMOPOD_API_KEY": "sumo-secret-test-key"}, clear=True):
            self.assertEqual(get_system2_provider(), "sumopod")

        # When ANTHROPIC_API_KEY is also present, Claude retains priority
        with patch.dict(os.environ, {
            "SUMOPOD_API_KEY": "sumo-secret-test-key",
            "ANTHROPIC_API_KEY": "sk-ant-test-key",
        }, clear=True):
            self.assertEqual(get_system2_provider(), "claude")

    def test_default_base_url_and_model(self):
        """Default base URL is https://ai.sumopod.com/v1 and default model is deepseek-chat."""
        self.assertEqual(DEFAULT_SUMOPOD_BASE_URL, "https://ai.sumopod.com/v1")
        self.assertEqual(DEFAULT_SUMOPOD_MODEL, "deepseek-chat")

        with patch.dict(os.environ, {"SUMOPOD_API_KEY": "sumo-key-12345"}, clear=True):
            client = System2Client(provider="sumopod")
            self.assertIsInstance(client, OpenAISystem2Client)
            self.assertEqual(client.provider_name, "sumopod")
            self.assertEqual(client.base_url, "https://ai.sumopod.com/v1")
            self.assertEqual(client.model, "deepseek-chat")
            self.assertEqual(client.api_key, "sumo-key-12345")

    def test_custom_model_and_base_url_env_and_args(self):
        """Custom SUMOPOD_MODEL, SUMOPOD_BASE_URL, and explicit arguments are respected."""
        # Via environment variables
        with patch.dict(os.environ, {
            "SUMOPOD_API_KEY": "sumo-key-12345",
            "SUMOPOD_MODEL": "deepseek-reasoner",
            "SUMOPOD_BASE_URL": "https://ai.sumopod.com/v1/",
        }, clear=True):
            client = OpenAISystem2Client(provider_name="sumopod")
            self.assertEqual(client.model, "deepseek-reasoner")
            self.assertEqual(client.base_url, "https://ai.sumopod.com/v1")

        # Explicit constructor args override environment variables
        with patch.dict(os.environ, {
            "SUMOPOD_API_KEY": "env-key",
            "SUMOPOD_MODEL": "deepseek-chat",
            "SUMOPOD_BASE_URL": "https://ai.sumopod.com/v1",
        }, clear=True):
            client = System2Client(
                provider="sumopod",
                model="deepseek-custom-v3",
                api_key="arg-key-999",
                base_url="https://alt.sumopod.com/v1/",
            )
            self.assertEqual(client.model, "deepseek-custom-v3")
            self.assertEqual(client.api_key, "arg-key-999")
            self.assertEqual(client.base_url, "https://alt.sumopod.com/v1")

    def test_missing_api_key_fails_clearly(self):
        """Missing SUMOPOD_API_KEY raises a clear RuntimeError and does not silently fall back."""
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                System2Client(provider="sumopod")
            err = str(ctx.exception)
            self.assertIn("SUMOPOD_API_KEY", err)
            self.assertIn("sumopod", err)

    def test_no_silent_fallback_when_sumopod_fails(self):
        """Sumopod errors do not silently fall back to another provider or model."""
        with patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "OPENAI_API_KEY": "sk-proj-test",
        }, clear=True):
            # Even when Claude and OpenAI keys exist, explicit sumopod provider must NOT fall back
            with self.assertRaises(RuntimeError) as ctx:
                System2Client(provider="sumopod")
            self.assertIn("SUMOPOD_API_KEY", str(ctx.exception))

    @patch("requests.post")
    def test_successful_mocked_plan_and_write_code(self, mock_post):
        """Sumopod executes plan_task and write_code against the expected endpoint and payload."""
        # 1. plan_task
        mock_resp_plan = MagicMock()
        mock_resp_plan.status_code = 200
        mock_resp_plan.is_redirect = False
        mock_resp_plan.json.return_value = {
            "choices": [{
                "message": {
                    "content": '{"steps": [{"id": "1", "description": "Create component", "files": ["src/App.tsx"]}]}'
                }
            }],
            "usage": {"prompt_tokens": 150, "completion_tokens": 50},
        }
        mock_post.return_value = mock_resp_plan

        client = OpenAISystem2Client(
            provider_name="sumopod",
            api_key="sumo-valid-key",
            model="deepseek-chat",
            base_url="https://ai.sumopod.com/v1",
        )
        steps = client.plan_task("Create new UI", repo_tree="src/App.tsx")
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].id, "1")
        self.assertEqual(steps[0].files, ["src/App.tsx"])

        # Verify HTTP invocation details
        self.assertEqual(mock_post.call_count, 1)
        call_args, call_kwargs = mock_post.call_args
        self.assertEqual(call_args[0], "https://ai.sumopod.com/v1/chat/completions")
        self.assertEqual(call_kwargs["headers"]["Authorization"], "Bearer sumo-valid-key")
        self.assertEqual(call_kwargs["json"]["model"], "deepseek-chat")
        self.assertFalse(call_kwargs.get("allow_redirects", True))

        # 2. write_code
        mock_resp_write = MagicMock()
        mock_resp_write.status_code = 200
        mock_resp_write.is_redirect = False
        mock_resp_write.json.return_value = {
            "choices": [{
                "message": {
                    "content": '{"files": {"src/App.tsx": "export const App = () => <div>Hello</div>;"}, "summary": "Initial component"}'
                }
            }],
            "usage": {"prompt_tokens": 200, "completion_tokens": 80},
        }
        mock_post.return_value = mock_resp_write

        files = client.write_code(steps[0], "Create new UI", file_contents={})
        self.assertIn("src/App.tsx", files)
        self.assertIn("Hello", files["src/App.tsx"])

    @patch("requests.post")
    def test_http_auth_error_401(self, mock_post):
        """HTTP 401 Unauthorized raises RuntimeError with status code and detail."""
        import requests

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.is_redirect = False
        mock_resp.json.return_value = {"error": {"message": "Invalid API key provided", "type": "auth_error"}}
        mock_resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=mock_resp)
        mock_post.return_value = mock_resp

        client = OpenAISystem2Client(provider_name="sumopod", api_key="invalid-key")
        with self.assertRaises(RuntimeError) as ctx:
            client.diagnose("test query", focus_files={}, domain="core")

        err = str(ctx.exception)
        self.assertIn("401", err)
        self.assertIn("Invalid API key", err)

    @patch("requests.post")
    def test_http_provider_error_500(self, mock_post):
        """HTTP 500 raises RuntimeError with status code."""
        import requests

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.is_redirect = False
        mock_resp.text = "Internal Server Error"
        mock_resp.json.side_effect = Exception("Not JSON")
        mock_resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=mock_resp)
        mock_post.return_value = mock_resp

        client = OpenAISystem2Client(provider_name="sumopod", api_key="valid-key")
        with self.assertRaises(RuntimeError) as ctx:
            client.diagnose("test query", focus_files={}, domain="core")

        self.assertIn("500", str(ctx.exception))
        self.assertIn("Internal Server Error", str(ctx.exception))

    @patch("requests.post")
    def test_transport_redirect_prohibited(self, mock_post):
        """Transport-level redirects from model endpoint are rejected."""
        mock_resp = MagicMock()
        mock_resp.status_code = 302
        mock_resp.is_redirect = True
        mock_resp.headers = {"Location": "https://malicious.example/redirect"}
        mock_post.return_value = mock_resp

        client = OpenAISystem2Client(provider_name="sumopod", api_key="valid-key")
        with self.assertRaises(RuntimeError) as ctx:
            client.diagnose("test query", focus_files={}, domain="core")

        err = str(ctx.exception).lower()
        self.assertIn("redirected", err)
        self.assertIn("prohibited", err)

    def test_secret_scrubbing_redacts_sumopod_key(self):
        """SUMOPOD_API_KEY is scrubbed from logs, exceptions, and proof artifacts."""
        import json
        raw_key = "sumo_live_secret_key_9876543210"
        with patch.dict(os.environ, {"SUMOPOD_API_KEY": raw_key}):
            # 1. Plain text log
            text = f"Connecting to Sumopod with key {raw_key} at runtime"
            scrubbed = scrub_text_patterns(text)
            self.assertNotIn(raw_key, scrubbed)
            self.assertIn("[SUMOPOD_API_KEY_REDACTED]", scrubbed)

            # 2. Exception string
            try:
                raise RuntimeError(f"Failed request Authorization: Bearer {raw_key}")
            except RuntimeError as exc:
                exc_scrubbed = scrub_secrets(str(exc))
                self.assertNotIn(raw_key, exc_scrubbed)

            # 3. JSON proof artifact structure
            proof_data = {
                "task_id": "task_12345",
                "env": {"SUMOPOD_API_KEY": raw_key},
                "log": f"Auth failed with {raw_key}",
            }
            json_str = json.dumps(proof_data)
            scrubbed_json = scrub_secrets(json_str)
            self.assertNotIn(raw_key, scrubbed_json)

    def test_doctor_diagnostics_sumopod(self):
        """Doctor diagnostics reports status of Sumopod provider correctly."""
        with patch.dict(os.environ, {
            "SYSTEM2_PROVIDER": "sumopod",
            "SUMOPOD_API_KEY": "sumo-doctor-key-123",
        }):
            checks = run_doctor_diagnostics(Path.cwd())
            auth_check = next((c for c in checks if c.name == "Sumopod Authentication"), None)
            self.assertIsNotNone(auth_check)
            self.assertEqual(auth_check.status, "OK")
            # Secret must not be in plain text in detail
            self.assertNotIn("sumo-doctor-key-123", auth_check.detail)

        with patch.dict(os.environ, {
            "SYSTEM2_PROVIDER": "sumopod",
        }):
            # Ensure SUMOPOD_API_KEY is unset
            os.environ.pop("SUMOPOD_API_KEY", None)
            checks = run_doctor_diagnostics(Path.cwd())
            auth_check = next((c for c in checks if c.name == "Sumopod Authentication"), None)
            self.assertIsNotNone(auth_check)
            self.assertEqual(auth_check.status, "FAIL")

    def test_preflight_passthrough_and_antigravity_isolation(self):
        """Sumopod passes validate_system2_provider_preflight while Antigravity remains fail-closed."""
        # Sumopod passes preflight
        res = validate_system2_provider_preflight("sumopod")
        self.assertEqual(res, "sumopod")

        # Antigravity security boundary without opt-in remains strictly blocked
        with patch.dict(os.environ, {"BRAINFROG_ALLOW_UNRESTRICTED_ANTIGRAVITY_CLI": "0"}):
            with patch("system2.find_antigravity_bin", return_value="C:\\fake\\agy.exe"):
                with patch("os.path.exists", return_value=True):
                    with self.assertRaises(AntigravityOptInRequiredError):
                        validate_system2_provider_preflight("antigravity")

    def test_existing_providers_unaffected(self):
        """Claude, OpenAI, OpenRouter, and Antigravity provider creation remains unchanged."""
        # Claude
        with patch("system2.claude_client.anthropic.Anthropic"):
            claude = System2Client(provider="claude", api_key="sk-ant-test")
            self.assertIsInstance(claude, ClaudeSystem2Client)

        # OpenAI
        openai_c = System2Client(provider="openai", api_key="sk-proj-test")
        self.assertIsInstance(openai_c, OpenAISystem2Client)
        self.assertEqual(openai_c.provider_name, "openai")
        self.assertEqual(openai_c.base_url, "https://api.openai.com/v1")

        # OpenRouter
        or_c = System2Client(provider="openrouter", api_key="sk-or-test")
        self.assertIsInstance(or_c, OpenAISystem2Client)
        self.assertEqual(or_c.provider_name, "openrouter")
        self.assertEqual(or_c.base_url, "https://openrouter.ai/api/v1")

    def test_multi_model_selection_deepseek_kimi_glm(self):
        """Verify model selection for DeepSeek, Kimi, and GLM model families via args and env."""
        with patch.dict(os.environ, {"SUMOPOD_API_KEY": "test-key"}):
            # 1. DeepSeek family
            c_ds_chat = System2Client(provider="sumopod", model="deepseek-chat")
            self.assertEqual(c_ds_chat.model, "deepseek-chat")

            c_ds_reasoner = System2Client(provider="sumopod", model="deepseek-reasoner")
            self.assertEqual(c_ds_reasoner.model, "deepseek-reasoner")

            # 2. Kimi family (Moonshot AI)
            c_kimi = System2Client(provider="sumopod", model="kimi-latest")
            self.assertEqual(c_kimi.model, "kimi-latest")

            c_moonshot = System2Client(provider="sumopod", model="moonshot-v1-32k")
            self.assertEqual(c_moonshot.model, "moonshot-v1-32k")

            # 3. GLM family (Zhipu AI)
            c_glm = System2Client(provider="sumopod", model="glm-4")
            self.assertEqual(c_glm.model, "glm-4")

            c_glm_flash = System2Client(provider="sumopod", model="glm-4-flash")
            self.assertEqual(c_glm_flash.model, "glm-4-flash")

            # 4. Selection via BRAINFROG_MODEL_NAME
            with patch.dict(os.environ, {"BRAINFROG_MODEL_NAME": "glm-4-plus"}):
                c_env = System2Client(provider="sumopod")
                self.assertEqual(c_env.model, "glm-4-plus")

    def test_context_limits_deepseek_kimi_glm(self):
        """Context limits for DeepSeek, Kimi, and GLM models are correctly resolved."""
        from system2.claude_client import get_model_context_limit

        self.assertEqual(get_model_context_limit("deepseek-chat"), 64_000)
        self.assertEqual(get_model_context_limit("deepseek-reasoner"), 64_000)
        self.assertEqual(get_model_context_limit("kimi-latest"), 128_000)
        self.assertEqual(get_model_context_limit("moonshot-v1-8k"), 8_000)
        self.assertEqual(get_model_context_limit("moonshot-v1-32k"), 32_000)
        self.assertEqual(get_model_context_limit("moonshot-v1-128k"), 128_000)
        self.assertEqual(get_model_context_limit("glm-4"), 128_000)
        self.assertEqual(get_model_context_limit("glm-4-flash"), 128_000)
        self.assertEqual(get_model_context_limit("glm-4-long"), 1_000_000)

    def test_reasoning_tokens_think_tag_stripping(self):
        """extract_json strips <think>...</think> reasoning traces emitted by R1/Kimi/GLM models."""
        from system2.json_utils import extract_json

        raw_output = (
            "<think>\n"
            "Let's consider the steps required.\n"
            "Should we do { 'bogus': 'fake' }?\n"
            "No, that would be invalid.\n"
            "</think>\n\n"
            "```json\n"
            "{\n"
            '  "steps": [{"id": "1", "description": "Valid step", "files": ["main.py"]}]\n'
            "}\n"
            "```"
        )
        parsed = extract_json(raw_output)
        self.assertIn("steps", parsed)
        self.assertEqual(parsed["steps"][0]["id"], "1")
        self.assertEqual(parsed["steps"][0]["files"], ["main.py"])

    def test_think_tag_malformed_and_edge_cases(self):
        """extract_json handles malformed, unclosed, empty, and multiple <think> tags safely."""
        from system2.json_utils import extract_json
        import json

        # 1. Unclosed <think> tag with subsequent markdown fence
        unclosed_with_fence = "<think>\nThinking cut off before tag closes\n```json\n{\"ok\": true}\n```"
        self.assertEqual(extract_json(unclosed_with_fence), {"ok": True})

        # 2. <think> only with no JSON at all fails safely
        think_only = "<think>I am just thinking without any plan or json output</think>"
        with self.assertRaises((ValueError, json.JSONDecodeError)):
            extract_json(think_only)

        # 3. Incomplete unclosed <think> with no JSON fails safely
        unclosed_no_json = "<think>I am just thinking and was cut off"
        with self.assertRaises((ValueError, json.JSONDecodeError)):
            extract_json(unclosed_no_json)

        # 4. Multiple separate <think> tags
        multi_think = (
            "<think>part 1</think>\n"
            "Interim text\n"
            "<think>part 2 with {invalid: json}</think>\n"
            '{"actual": "result"}'
        )
        self.assertEqual(extract_json(multi_think), {"actual": "result"})

        # 5. Empty <think></think>
        empty_think = "<think></think> {\"success\": 1}"
        self.assertEqual(extract_json(empty_think), {"success": 1})

    @patch("requests.post")
    def test_reasoning_content_fallback_in_message(self, mock_post):
        """When content is empty, reasoning_content in message is used as fallback."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.is_redirect = False
        mock_resp.json.return_value = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": '{"steps": [{"id": "1", "description": "Reasoned step", "files": ["src/logic.py"]}]}',
                }
            }],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }
        mock_post.return_value = mock_resp

        client = OpenAISystem2Client(provider_name="sumopod", api_key="test-key", model="deepseek-reasoner")
        steps = client.plan_task("Reasoned task", repo_tree="src/logic.py")
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].description, "Reasoned step")

    @patch("requests.post")
    def test_refusal_response_raises_runtime_error(self, mock_post):
        """Model refusal message raises an explicit RuntimeError."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.is_redirect = False
        mock_resp.json.return_value = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "refusal": "I cannot perform this operation due to policy.",
                }
            }]
        }
        mock_post.return_value = mock_resp

        client = OpenAISystem2Client(provider_name="sumopod", api_key="test-key")
        with self.assertRaises(RuntimeError) as ctx:
            client.diagnose("test query", focus_files={}, domain="core")
        self.assertIn("refused request", str(ctx.exception))
        self.assertIn("policy", str(ctx.exception))

    @patch("requests.post")
    def test_empty_content_raises_runtime_error(self, mock_post):
        """Empty content without reasoning raises an explicit RuntimeError."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.is_redirect = False
        mock_resp.json.return_value = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "   ",
                }
            }]
        }
        mock_post.return_value = mock_resp

        client = OpenAISystem2Client(provider_name="sumopod", api_key="test-key")
        with self.assertRaises(RuntimeError) as ctx:
            client.diagnose("test query", focus_files={}, domain="core")
        self.assertIn("returned empty content", str(ctx.exception))

    @patch("requests.get")
    def test_list_models_endpoint_success(self, mock_get):
        """list_models() fetches and parses GET /models endpoint successfully."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.is_redirect = False
        mock_resp.json.return_value = {
            "object": "list",
            "data": [
                {"id": "deepseek-chat", "object": "model", "owned_by": "sumopod"},
                {"id": "deepseek-reasoner", "object": "model", "owned_by": "sumopod"},
                {"id": "kimi-latest", "object": "model", "owned_by": "sumopod"},
                {"id": "glm-4", "object": "model", "owned_by": "sumopod"},
            ],
        }
        mock_get.return_value = mock_resp

        client = OpenAISystem2Client(provider_name="sumopod", api_key="test-key", base_url="https://ai.sumopod.com/v1")
        models = client.list_models()
        self.assertEqual(len(models), 4)
        model_ids = [m["id"] for m in models]
        self.assertIn("deepseek-chat", model_ids)
        self.assertIn("kimi-latest", model_ids)
        self.assertIn("glm-4", model_ids)

        # Verify call endpoint and headers
        self.assertEqual(mock_get.call_count, 1)
        args, kwargs = mock_get.call_args
        self.assertEqual(args[0], "https://ai.sumopod.com/v1/models")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-key")

    @patch("requests.get")
    def test_list_models_endpoint_http_error(self, mock_get):
        """list_models() raises RuntimeError with status code on HTTP error."""
        import requests

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.is_redirect = False
        mock_resp.json.return_value = {"error": "Not Found"}
        mock_resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=mock_resp)
        mock_get.return_value = mock_resp

        client = OpenAISystem2Client(provider_name="sumopod", api_key="test-key")
        with self.assertRaises(RuntimeError) as ctx:
            client.list_models()
        self.assertIn("404", str(ctx.exception))

    @patch("requests.get")
    def test_list_models_redirect_rejection(self, mock_get):
        """list_models() prohibits transport-level redirects."""
        mock_resp = MagicMock()
        mock_resp.status_code = 301
        mock_resp.is_redirect = True
        mock_resp.headers = {"Location": "http://169.254.169.254/models"}
        mock_get.return_value = mock_resp

        client = OpenAISystem2Client(provider_name="sumopod", api_key="test-key")
        with self.assertRaises(RuntimeError) as ctx:
            client.list_models()
        self.assertIn("redirected", str(ctx.exception).lower())
        self.assertIn("prohibited", str(ctx.exception).lower())

    def test_cli_show_models(self):
        """cli.show_models renders model catalog table without errors."""
        from cli import show_models

        # Curated fallback
        with patch.dict(os.environ, {"SYSTEM2_PROVIDER": "sumopod"}, clear=False):
            code = show_models(provider="sumopod")
            self.assertEqual(code, 0)

        # Mocked live models
        with patch.dict(os.environ, {"SYSTEM2_PROVIDER": "sumopod", "SUMOPOD_API_KEY": "test-key"}, clear=False):
            with patch("system2.openai_client.OpenAISystem2Client.list_models", return_value=[
                {"id": "deepseek-chat", "owned_by": "sumopod"},
                {"id": "kimi-latest", "owned_by": "sumopod"},
            ]):
                code = show_models(provider="sumopod")
                self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()

