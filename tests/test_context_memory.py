"""test_context_memory.py — Unit tests for Context Window tracking & OpenCode-style metrics."""
import unittest

from system2.claude_client import (
    UsageTracker,
    get_model_context_limit,
    resolve_model_context_limit,
    MODEL_CONTEXT_LIMITS,
)
from cli import SLASH_COMMAND_COMPLETIONS


class TestContextMemory(unittest.TestCase):
    def test_model_context_limits(self):
        # Gemini limits with suffixes (-high, -medium, -low, -thinking)
        self.assertEqual(get_model_context_limit("gemini-3.8-flash-high"), 1_000_000)
        self.assertEqual(get_model_context_limit("gemini-3.8-flash-medium"), 1_000_000)
        self.assertEqual(get_model_context_limit("gemini-3.8-flash-low"), 1_000_000)
        self.assertEqual(get_model_context_limit("gemini-3.7-flash-high"), 1_000_000)
        self.assertEqual(get_model_context_limit("gemini-3.7-flash-thinking"), 1_000_000)
        self.assertEqual(get_model_context_limit("gemini-flash-thinking"), 1_000_000)
        self.assertEqual(get_model_context_limit("gemini-3.8-pro-high"), 2_000_000)
        self.assertEqual(get_model_context_limit("gemini-3.8-pro-medium"), 2_000_000)
        self.assertEqual(get_model_context_limit("gemini-3.1-pro-high"), 2_000_000)
        self.assertEqual(get_model_context_limit("gemini-custom-flash"), 1_000_000)
        self.assertEqual(get_model_context_limit("gemini-custom-pro"), 2_000_000)

        # Priority rule: Hybrid model containing both 'flash' and 'pro' prioritizes Flash (conservative 1M)
        self.assertEqual(get_model_context_limit("gemini-3.8-flash-pro-experimental"), 1_000_000)

        # Claude limits (standard & thinking suffixes)
        self.assertEqual(get_model_context_limit("claude-sonnet-5"), 200_000)
        self.assertEqual(get_model_context_limit("claude-3-5-sonnet-20241022"), 200_000)
        self.assertEqual(get_model_context_limit("claude-sonnet-4-6"), 200_000)
        self.assertEqual(get_model_context_limit("claude-opus-4-6-thinking"), 200_000)
        self.assertEqual(get_model_context_limit("sonnet-4-6"), 200_000)
        self.assertEqual(get_model_context_limit("haiku-3.5"), 200_000)

        # OpenAI / reasoning limits
        self.assertEqual(get_model_context_limit("o1"), 200_000)
        self.assertEqual(get_model_context_limit("o1-mini"), 200_000)
        self.assertEqual(get_model_context_limit("o3-mini"), 200_000)
        self.assertEqual(get_model_context_limit("gpt-4o"), 128_000)
        self.assertEqual(get_model_context_limit("gpt-oss-120b-medium"), 128_000)
        self.assertEqual(get_model_context_limit("deepseek-chat"), 64_000)

        # Fallback default with fallback detection
        lim_unknown, is_fallback_unknown = resolve_model_context_limit("unknown-model-xyz")
        self.assertEqual(lim_unknown, 128_000)
        self.assertTrue(is_fallback_unknown)

        lim_none, is_fallback_none = resolve_model_context_limit(None)
        self.assertEqual(lim_none, 128_000)
        self.assertTrue(is_fallback_none)

        self.assertEqual(get_model_context_limit("unknown-model-xyz"), 128_000)
        self.assertEqual(get_model_context_limit(None), 128_000)

    def test_usage_tracker_context_tracking(self):
        tracker = UsageTracker()

        # Initial state
        info = tracker.get_context_info("claude-sonnet-5")
        self.assertEqual(info["tokens"], 0)
        self.assertEqual(info["limit"], 200_000)
        self.assertEqual(info["percent"], 0.0)
        self.assertEqual(info["status"], "safe")
        self.assertEqual(info["status_label"], "Optimal")

        # Record a first turn with 10,000 input tokens (5% of 200k)
        tracker.record(10_000, 500)
        info1 = tracker.get_context_info("claude-sonnet-5")
        self.assertEqual(info1["tokens"], 10_000)
        self.assertEqual(info1["percent"], 5.0)
        self.assertEqual(info1["status"], "safe")
        self.assertIn("█", info1["bar"])

        # Reset task (between turns) should keep last_context_tokens intact for UI
        tracker.reset_task()
        info_post_turn = tracker.get_context_info("claude-sonnet-5")
        self.assertEqual(info_post_turn["tokens"], 10_000)
        self.assertEqual(tracker.last_task.input_tokens, 0)

        # Record a heavier turn reaching warning zone (120,000 / 200,000 = 60%)
        tracker.record(120_000, 2_000)
        info2 = tracker.get_context_info("claude-sonnet-5")
        self.assertEqual(info2["tokens"], 120_000)
        self.assertEqual(info2["percent"], 60.0)
        self.assertEqual(info2["status"], "warning")
        self.assertEqual(info2["status_label"], "Moderate")

        # Record a critical turn (>75%, e.g. 160,000 / 200,000 = 80%)
        tracker.record(160_000, 1_000)
        info3 = tracker.get_context_info("claude-sonnet-5")
        self.assertEqual(info3["tokens"], 160_000)
        self.assertEqual(info3["percent"], 80.0)
        self.assertEqual(info3["status"], "critical")
        self.assertEqual(info3["status_label"], "Kritis")

        # Test reset_session (/new command)
        tracker.reset_session()
        info_reset = tracker.get_context_info("claude-sonnet-5")
        self.assertEqual(info_reset["tokens"], 0)
        self.assertEqual(info_reset["percent"], 0.0)
        self.assertEqual(info_reset["status"], "safe")
        self.assertEqual(tracker.session.total_tokens, 0)

        # Context limit label tests for known vs fallback models
        info_flash = tracker.get_context_info("gemini-3.8-flash-high")
        self.assertEqual(info_flash["limit"], 1_000_000)
        self.assertEqual(info_flash["limit_k"], "1000k")
        self.assertFalse(info_flash["is_fallback"])

        info_unk = tracker.get_context_info("unknown-model-xyz")
        self.assertEqual(info_unk["limit"], 128_000)
        self.assertEqual(info_unk["limit_k"], "128k (est.)")
        self.assertTrue(info_unk["is_fallback"])

    def test_cli_command_completions(self):
        commands = [c[0] for c in SLASH_COMMAND_COMPLETIONS]
        self.assertIn("/new", commands)
        self.assertIn("/reset", commands)
        self.assertIn("/context", commands)
        self.assertIn("/tokens", commands)


if __name__ == "__main__":
    unittest.main()
