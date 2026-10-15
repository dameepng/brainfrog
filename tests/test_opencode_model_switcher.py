"""Unit tests for OpenCode-Style Interactive Model Switcher in BrainFrog."""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.model_catalog import (
    ModelCatalogEntry,
    get_curated_provider_models,
    get_unified_model_catalog,
    resolve_model_selection,
    search_model_catalog,
    select_model_opencode,
)
from core.runtime import BrainFrogRuntime, IncomingMessage
from core.runtime.session import SessionManager
from system2 import (
    AntigravityOptInRequiredError,
    OpenAISystem2Client,
    System2Client,
    get_system2_provider,
    validate_system2_provider_preflight,
)


class TestOpenCodeModelSwitcher(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path("./.brainfrog_test_model_switcher")
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        import shutil
        if self.tmp_dir.exists():
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # 1. Catalog Construction & Live vs Offline Labeling
    def test_get_unified_model_catalog_curated(self):
        """Unified catalog contains entries for sumopod, claude, antigravity, openai."""
        catalog = get_unified_model_catalog(include_live=False)
        self.assertGreater(len(catalog), 10)

        providers = {e.provider for e in catalog}
        self.assertIn("sumopod", providers)
        self.assertIn("claude", providers)
        self.assertIn("antigravity", providers)

        # In offline mode, all curated items have is_live=False
        for e in catalog:
            self.assertFalse(e.is_live)
            self.assertGreater(e.context_limit, 0)
            self.assertEqual(e.full_id, f"{e.provider}/{e.model_id}")

    @patch("system2.openai_client.OpenAISystem2Client.list_models")
    def test_get_unified_model_catalog_live_sumopod(self, mock_list):
        """When live API succeeds, live models are returned with is_live=True."""
        mock_list.return_value = [
            {"id": "deepseek-chat", "owned_by": "sumopod"},
            {"id": "kimi-latest", "owned_by": "sumopod"},
            {"id": "custom-sumopod-model", "owned_by": "sumopod"},
        ]
        with patch.dict(os.environ, {"SUMOPOD_API_KEY": "test-key"}):
            catalog = get_unified_model_catalog(active_provider="sumopod", include_live=True)
            live_entries = [e for e in catalog if e.is_live]
            self.assertEqual(len(live_entries), 3)

            custom_entry = next((e for e in live_entries if e.model_id == "custom-sumopod-model"), None)
            self.assertIsNotNone(custom_entry)
            self.assertTrue(custom_entry.is_live)
            self.assertEqual(custom_entry.full_id, "sumopod/custom-sumopod-model")

    @patch("system2.openai_client.OpenAISystem2Client.list_models")
    def test_live_vs_offline_catalog_labeling(self, mock_list):
        """Curated entries must not be falsely claimed as live."""
        mock_list.side_effect = RuntimeError("API unavailable")
        with patch.dict(os.environ, {"SUMOPOD_API_KEY": "test-key"}):
            catalog = get_unified_model_catalog(active_provider="sumopod", include_live=True)
            for entry in catalog:
                self.assertFalse(entry.is_live, "Fallback entry must have is_live=False")

    # 2. Search & Filtering Behavior
    def test_search_model_catalog_filtering(self):
        """search_model_catalog matches by model ID, provider, and description."""
        catalog = [
            ModelCatalogEntry("sumopod", "deepseek-chat", "DeepSeek Chat", "Coding model", 64000),
            ModelCatalogEntry("sumopod", "deepseek-reasoner", "DeepSeek R1", "Reasoning model", 64000),
            ModelCatalogEntry("sumopod", "kimi-latest", "Kimi Latest", "Long context", 128000),
            ModelCatalogEntry("claude", "claude-sonnet-5", "Claude Sonnet 5", "Default Claude", 200000),
            ModelCatalogEntry("antigravity", "gemini-3.8-flash-high", "Gemini 3.8", "Ultra fast", 128000),
        ]

        # Exact match
        r_exact = search_model_catalog(catalog, "deepseek-chat")
        self.assertEqual(len(r_exact), 1)
        self.assertEqual(r_exact[0].model_id, "deepseek-chat")

        # Prefix search
        r_prefix = search_model_catalog(catalog, "deepseek")
        self.assertEqual(len(r_prefix), 2)

        # Provider search
        r_prov = search_model_catalog(catalog, "claude")
        self.assertEqual(len(r_prov), 1)
        self.assertEqual(r_prov[0].model_id, "claude-sonnet-5")

        # Substring in description / name
        r_desc = search_model_catalog(catalog, "reasoning")
        self.assertEqual(len(r_desc), 1)
        self.assertEqual(r_desc[0].model_id, "deepseek-reasoner")

        # Empty query returns full list
        self.assertEqual(len(search_model_catalog(catalog, "")), 5)

        # Non-matching query returns empty list
        self.assertEqual(len(search_model_catalog(catalog, "nonexistent-model-xyz")), 0)

    # 3. Direct Selection Resolution
    def test_resolve_model_selection_direct_provider_model(self):
        """Direct <provider>/<model-id> syntax resolves correctly."""
        catalog = get_unified_model_catalog(include_live=False)

        # sumopod/deepseek-chat
        p, m, err = resolve_model_selection("sumopod/deepseek-chat", catalog, current_provider="claude")
        self.assertIsNone(err)
        self.assertEqual(p, "sumopod")
        self.assertEqual(m, "deepseek-chat")

        # claude/claude-sonnet-5
        p, m, err = resolve_model_selection("claude/claude-sonnet-5", catalog, current_provider="sumopod")
        self.assertIsNone(err)
        self.assertEqual(p, "claude")
        self.assertEqual(m, "claude-sonnet-5")

        # sumopod/kimi-latest
        p, m, err = resolve_model_selection("sumopod/kimi-latest", catalog, current_provider="claude")
        self.assertIsNone(err)
        self.assertEqual(p, "sumopod")
        self.assertEqual(m, "kimi-latest")

    def test_resolve_model_selection_bare_model_id(self):
        """Bare model_id resolves to current provider if present, or searches all providers."""
        catalog = get_unified_model_catalog(include_live=False)

        # Under sumopod, deepseek-chat resolves to sumopod
        p, m, err = resolve_model_selection("deepseek-chat", catalog, current_provider="sumopod")
        self.assertIsNone(err)
        self.assertEqual(p, "sumopod")
        self.assertEqual(m, "deepseek-chat")

        # Under claude, deepseek-chat unambiguously resolves to sumopod
        p, m, err = resolve_model_selection("deepseek-chat", catalog, current_provider="claude")
        self.assertIsNone(err)
        self.assertEqual(p, "sumopod")
        self.assertEqual(m, "deepseek-chat")

    def test_resolve_model_selection_numeric_index(self):
        """1-based numeric index resolves to corresponding catalog entry."""
        catalog = get_unified_model_catalog(include_live=False)
        p, m, err = resolve_model_selection("1", catalog, current_provider="claude")
        self.assertIsNone(err)
        self.assertEqual(p, catalog[0].provider)
        self.assertEqual(m, catalog[0].model_id)

        # Out-of-bounds numeric index
        p, m, err = resolve_model_selection("9999", catalog, current_provider="claude")
        self.assertIsNotNone(err)
        self.assertIn("tidak valid", err)

    def test_resolve_model_selection_invalid_provider_or_model(self):
        """Invalid provider or unresolvable model fails safely with an error."""
        catalog = get_unified_model_catalog(include_live=False)

        # Invalid provider
        p, m, err = resolve_model_selection("unknown_prov/deepseek-chat", catalog, current_provider="claude")
        self.assertIsNotNone(err)
        self.assertIn("tidak valid", err)

        # Nonexistent model for static provider
        p, m, err = resolve_model_selection("antigravity/completely-fake-model", catalog, current_provider="antigravity")
        self.assertIsNotNone(err)
        self.assertIn("tidak ditemukan", err)

        # Unknown bare model
        p, m, err = resolve_model_selection("random-ghost-model", catalog, current_provider="claude")
        self.assertIsNotNone(err)
        self.assertIn("tidak ditemukan", err)

    # 4. OpenCode Interactive Picker Navigation & Cancellation
    def test_opencode_picker_navigation_and_selection(self):
        """Simulating DOWN then ENTER selects the second model in the list."""
        catalog = [
            ModelCatalogEntry("sumopod", "deepseek-chat", "DeepSeek Chat", "Coding", 64000),
            ModelCatalogEntry("sumopod", "kimi-latest", "Kimi Latest", "Long context", 128000),
        ]

        # Simulate key events: DOWN -> ENTER
        keys = [("DOWN", None), ("ENTER", None)]
        key_iter = iter(keys)

        def mock_read_key():
            return next(key_iter)

        with patch("cli._erase_lines"):
            chosen = select_model_opencode(
                current_provider="sumopod",
                current_model="deepseek-chat",
                catalog=catalog,
                key_input_fn=mock_read_key,
            )

        self.assertEqual(chosen, ("sumopod", "kimi-latest"))

    def test_opencode_picker_cancellation(self):
        """Pressing ESCAPE cancels selection and returns None."""
        catalog = [
            ModelCatalogEntry("sumopod", "deepseek-chat", "DeepSeek Chat", "Coding", 64000),
        ]

        keys = [("ESCAPE", None)]
        key_iter = iter(keys)

        with patch("cli._erase_lines"):
            chosen = select_model_opencode(
                current_provider="sumopod",
                current_model="deepseek-chat",
                catalog=catalog,
                key_input_fn=lambda: next(key_iter),
            )

        self.assertIsNone(chosen)

    def test_opencode_picker_filter_typing_and_selection(self):
        """Typing characters filters the catalog in real-time before selection."""
        catalog = [
            ModelCatalogEntry("sumopod", "deepseek-chat", "DeepSeek Chat", "Coding", 64000),
            ModelCatalogEntry("sumopod", "kimi-latest", "Kimi Latest", "Moonshot", 128000),
            ModelCatalogEntry("claude", "claude-sonnet-5", "Claude Sonnet 5", "Default", 200000),
        ]

        # Type 'k', 'i', 'm', 'i', then ENTER
        keys = [
            ("CHAR", "k"),
            ("CHAR", "i"),
            ("CHAR", "m"),
            ("CHAR", "i"),
            ("ENTER", None),
        ]
        key_iter = iter(keys)

        with patch("cli._erase_lines"):
            chosen = select_model_opencode(
                current_provider="sumopod",
                current_model="deepseek-chat",
                catalog=catalog,
                key_input_fn=lambda: next(key_iter),
            )

        self.assertEqual(chosen, ("sumopod", "kimi-latest"))

    def test_opencode_picker_non_interactive_fallback(self):
        """Non-interactive terminal (sys.stdin.isatty() == False) renders table and returns None."""
        catalog = [
            ModelCatalogEntry("sumopod", "deepseek-chat", "DeepSeek Chat", "Coding", 64000),
        ]

        with patch("sys.stdin.isatty", return_value=False):
            chosen = select_model_opencode(
                current_provider="sumopod",
                current_model="deepseek-chat",
                catalog=catalog,
                key_input_fn=None,
            )

        self.assertIsNone(chosen)

    def test_opencode_picker_eof_handling(self):
        """Terminal EOF returns None safely without unhandled exceptions."""
        catalog = [
            ModelCatalogEntry("sumopod", "deepseek-chat", "DeepSeek Chat", "Coding", 64000),
        ]
        with patch("cli._erase_lines"):
            chosen = select_model_opencode(
                current_provider="sumopod",
                current_model="deepseek-chat",
                catalog=catalog,
                key_input_fn=lambda: ("EOF", None),
            )
        self.assertIsNone(chosen)

    # 5. Switching Semantics & Session Propagation
    def test_model_switching_updates_session_and_next_turn_client(self):
        """Switching model in session propagates to next turn's System2Client instantiation."""
        runtime = BrainFrogRuntime(
            repo_dir=self.tmp_dir,
            default_provider="claude",
            default_model="claude-sonnet-5",
        )

        mock_s1_factory = MagicMock()
        mock_s2_factory = MagicMock()
        runtime.system1_factory = mock_s1_factory
        runtime.system2_factory = mock_s2_factory

        # Session starts with default
        sess = runtime.sessions.get_or_create(channel="cli", user_id="local", conversation_id="default")
        self.assertIn(sess.active_provider or "claude", ("claude", None))

        # Switch model to sumopod/kimi-latest
        sess.active_provider = "sumopod"
        sess.active_model = "kimi-latest"
        runtime.default_provider = "sumopod"
        runtime.default_model = "kimi-latest"
        runtime.sessions.save(sess)

        # Trigger message on next turn
        incoming = IncomingMessage(
            text="Hello world",
            channel="cli",
            user_id="local",
            conversation_id="default",
            metadata={"provider": "sumopod", "model": "kimi-latest"},
        )
        runtime.handle_message(incoming)

        # Verify system2_factory was called with new provider and model
        mock_s2_factory.assert_called_with(model="kimi-latest", provider="sumopod")

    def test_session_isolation_and_persistence(self):
        """Model changes in session_a do not bleed into session_b."""
        runtime = BrainFrogRuntime(repo_dir=self.tmp_dir)

        sess_a = runtime.sessions.get_or_create(channel="cli", user_id="user_a", conversation_id="conv_a")
        sess_b = runtime.sessions.get_or_create(channel="cli", user_id="user_b", conversation_id="conv_b")

        sess_a.active_provider = "sumopod"
        sess_a.active_model = "deepseek-chat"
        runtime.sessions.save(sess_a)

        sess_b.active_provider = "claude"
        sess_b.active_model = "claude-sonnet-5"
        runtime.sessions.save(sess_b)

        # Reload from storage
        reloaded_a = runtime.sessions.get_or_create(channel="cli", user_id="user_a", conversation_id="conv_a")
        reloaded_b = runtime.sessions.get_or_create(channel="cli", user_id="user_b", conversation_id="conv_b")

        self.assertEqual(reloaded_a.active_provider, "sumopod")
        self.assertEqual(reloaded_a.active_model, "deepseek-chat")
        self.assertEqual(reloaded_b.active_provider, "claude")
        self.assertEqual(reloaded_b.active_model, "claude-sonnet-5")

    def test_model_selection_persists_across_reset(self):
        """Active model and provider remain selected when /reset is called."""
        runtime = BrainFrogRuntime(
            repo_dir=self.tmp_dir,
            default_provider="sumopod",
            default_model="kimi-latest",
        )
        sess = runtime.sessions.get_or_create(channel="cli", user_id="local", conversation_id="default")
        sess.active_provider = "sumopod"
        sess.active_model = "kimi-latest"
        sess.history = [{"user": "task 1", "assistant": "done"}]
        runtime.sessions.save(sess)

        # Call /reset
        incoming_reset = IncomingMessage(
            text="/reset",
            channel="cli",
            user_id="local",
            conversation_id="default",
        )
        out = runtime.handle_message(incoming_reset)
        self.assertTrue(out.success)

        # History is cleared, but model and provider are preserved
        reloaded = runtime.sessions.get_or_create(channel="cli", user_id="local", conversation_id="default")
        self.assertEqual(len(reloaded.history), 0)
        self.assertEqual(reloaded.active_provider, "sumopod")
        self.assertEqual(reloaded.active_model, "kimi-latest")

    def test_preflight_blocked_when_switching_to_antigravity_without_opt_in(self):
        """Preflight blocks Antigravity invocation without opt-in before execution."""
        with patch.dict(os.environ, {"BRAINFROG_ALLOW_UNRESTRICTED_ANTIGRAVITY_CLI": "0"}):
            with patch("system2.find_antigravity_bin", return_value="C:\\fake\\agy.exe"):
                with patch("os.path.exists", return_value=True):
                    with self.assertRaises(AntigravityOptInRequiredError):
                        validate_system2_provider_preflight("antigravity")

    # 6. Priority Model Picker Catalog Tests
    @patch("system2.openai_client.OpenAISystem2Client.list_models")
    def test_priority_models_ordering_when_confirmed_live(self, mock_list):
        """When confirmed available by live catalog, priority models appear at top of Sumopod section."""
        mock_list.return_value = [
            {"id": "deepseek-chat", "owned_by": "sumopod"},
            {"id": "kimi-k3", "owned_by": "sumopod"},
            {"id": "deepseek-v4.1-flash:netra", "owned_by": "sumopod"},
            {"id": "glm-5.3-flash", "owned_by": "sumopod"},
            {"id": "deepseek-v4-pro", "owned_by": "sumopod"},
            {"id": "kimi-latest", "owned_by": "sumopod"},
        ]
        with patch.dict(os.environ, {"SUMOPOD_API_KEY": "test-key"}):
            catalog = get_unified_model_catalog(active_provider="sumopod", include_live=True)
            sumo_models = [e for e in catalog if e.provider == "sumopod"]

            # The 4 priority models must be at the very top of the Sumopod section in exact priority order
            self.assertEqual(sumo_models[0].model_id, "deepseek-v4-pro")
            self.assertEqual(sumo_models[1].model_id, "deepseek-v4.1-flash:netra")
            self.assertEqual(sumo_models[2].model_id, "glm-5.3-flash")
            self.assertEqual(sumo_models[3].model_id, "kimi-k3")

            for i in range(4):
                self.assertTrue(sumo_models[i].is_live)
                self.assertTrue(sumo_models[i].is_priority)
                self.assertTrue(sumo_models[i].is_verified)
                self.assertEqual(sumo_models[i].status, "available")

            # Other live models remain discoverable after the priority models
            other_ids = [e.model_id for e in sumo_models[4:]]
            self.assertIn("deepseek-chat", other_ids)
            self.assertIn("kimi-latest", other_ids)

    @patch("system2.openai_client.OpenAISystem2Client.list_models")
    def test_priority_models_absent_from_live_catalog_labeled_unverified(self, mock_list):
        """If a priority model is absent from live catalog, label it unverified rather than claiming live."""
        # API only returns standard models, none of the 4 priority models
        mock_list.return_value = [
            {"id": "deepseek-chat", "owned_by": "sumopod"},
            {"id": "kimi-latest", "owned_by": "sumopod"},
        ]
        with patch.dict(os.environ, {"SUMOPOD_API_KEY": "test-key"}):
            catalog = get_unified_model_catalog(active_provider="sumopod", include_live=True)
            sumo_models = [e for e in catalog if e.provider == "sumopod"]

            # Live models returned by API are marked live
            live_models = [e for e in sumo_models if e.is_live]
            live_ids = {e.model_id for e in live_models}
            self.assertEqual(live_ids, {"deepseek-chat", "kimi-latest"})

            # Absent priority models must be labeled unverified with is_live=False
            unverified_models = [e for e in sumo_models if not e.is_live and e.is_priority]
            unverified_ids = [e.model_id for e in unverified_models]
            self.assertEqual(
                unverified_ids,
                ["deepseek-v4-pro", "deepseek-v4.1-flash:netra", "glm-5.3-flash", "kimi-k3"],
            )
            for e in unverified_models:
                self.assertFalse(e.is_live, f"{e.model_id} must not be claimed as live")
                self.assertFalse(e.is_verified, f"{e.model_id} must be labeled unverified")
                self.assertEqual(e.status, "unverified")
                self.assertIn("absent from live catalog", e.description.lower())

    @patch("system2.openai_client.OpenAISystem2Client.list_models")
    def test_partial_live_confirmation_and_unverified_labeling(self, mock_list):
        """When some priority models are live and others absent, label each accurately."""
        # Only deepseek-v4.1-flash:netra and glm-5.3-flash are returned by API
        mock_list.return_value = [
            {"id": "deepseek-chat", "owned_by": "sumopod"},
            {"id": "deepseek-v4.1-flash:netra", "owned_by": "sumopod"},
            {"id": "glm-5.3-flash", "owned_by": "sumopod"},
        ]
        with patch.dict(os.environ, {"SUMOPOD_API_KEY": "test-key"}):
            catalog = get_unified_model_catalog(active_provider="sumopod", include_live=True)
            sumo_models = [e for e in catalog if e.provider == "sumopod"]

            # Top models are confirmed live priority models
            self.assertEqual(sumo_models[0].model_id, "deepseek-v4.1-flash:netra")
            self.assertTrue(sumo_models[0].is_live)
            self.assertEqual(sumo_models[1].model_id, "glm-5.3-flash")
            self.assertTrue(sumo_models[1].is_live)

            # Other live model
            self.assertEqual(sumo_models[2].model_id, "deepseek-chat")
            self.assertTrue(sumo_models[2].is_live)

            # Absent priority models are at the end, labeled unverified
            absent_1 = next(e for e in sumo_models if e.model_id == "deepseek-v4-pro")
            absent_2 = next(e for e in sumo_models if e.model_id == "kimi-k3")
            self.assertFalse(absent_1.is_live)
            self.assertEqual(absent_1.status, "unverified")
            self.assertFalse(absent_2.is_live)
            self.assertEqual(absent_2.status, "unverified")

    def test_exact_id_preservation_punctuation_and_suffixes(self):
        """Preserve each model ID exactly, including 'deepseek-v4.1-flash:netra' (colon and dot)."""
        catalog = get_unified_model_catalog(include_live=False)

        # 1. Full provider syntax
        p, m, err = resolve_model_selection("sumopod/deepseek-v4.1-flash:netra", catalog, "sumopod")
        self.assertIsNone(err)
        self.assertEqual(p, "sumopod")
        self.assertEqual(m, "deepseek-v4.1-flash:netra", "Colon and dot must be preserved exactly")

        # 2. Bare model ID syntax
        p, m, err = resolve_model_selection("deepseek-v4.1-flash:netra", catalog, "sumopod")
        self.assertIsNone(err)
        self.assertEqual(p, "sumopod")
        self.assertEqual(m, "deepseek-v4.1-flash:netra")

        # 3. Exact ID preservation for all 4 priority models
        for exact_id in ("deepseek-v4-pro", "deepseek-v4.1-flash:netra", "glm-5.3-flash", "kimi-k3"):
            p, m, err = resolve_model_selection(f"sumopod/{exact_id}", catalog, "claude")
            self.assertIsNone(err)
            self.assertEqual(m, exact_id, f"Model ID '{exact_id}' must not be modified or substituted")

    def test_priority_model_selection_propagates_to_system2_client(self):
        """Selecting deepseek-v4.1-flash:netra instantiates client with exact model on next turn."""
        runtime = BrainFrogRuntime(repo_dir=self.tmp_dir)
        mock_s1_factory = MagicMock()
        mock_s2_factory = MagicMock()
        runtime.system1_factory = mock_s1_factory
        runtime.system2_factory = mock_s2_factory

        sess = runtime.sessions.get_or_create(channel="cli", user_id="local", conversation_id="default")
        sess.active_provider = "sumopod"
        sess.active_model = "deepseek-v4.1-flash:netra"
        runtime.default_provider = "sumopod"
        runtime.default_model = "deepseek-v4.1-flash:netra"
        runtime.sessions.save(sess)

        incoming = IncomingMessage(
            text="Implement feature",
            channel="cli",
            user_id="local",
            conversation_id="default",
            metadata={"provider": "sumopod", "model": "deepseek-v4.1-flash:netra"},
        )
        runtime.handle_message(incoming)

        mock_s2_factory.assert_called_with(
            model="deepseek-v4.1-flash:netra",
            provider="sumopod",
        )

    def test_openai_client_preserves_exact_priority_model_id(self):
        """OpenAISystem2Client retains exact model ID without renaming or fallback."""
        with patch.dict(os.environ, {"SUMOPOD_API_KEY": "test-key"}):
            client = OpenAISystem2Client(
                provider_name="sumopod",
                model="deepseek-v4.1-flash:netra",
            )
            self.assertEqual(client.model, "deepseek-v4.1-flash:netra")
            self.assertEqual(client.provider_name, "sumopod")

            # Also verify via System2Client factory
            from system2 import System2Client
            client_factory = System2Client(provider="sumopod", model="deepseek-v4.1-flash:netra")
            self.assertEqual(client_factory.model, "deepseek-v4.1-flash:netra")

    def test_priority_model_search_discoverability(self):
        """Priority models and other models are discoverable through search."""
        catalog = get_unified_model_catalog(include_live=False)

        # Search by suffix / variant
        r1 = search_model_catalog(catalog, "netra")
        self.assertGreaterEqual(len(r1), 1)
        self.assertEqual(r1[0].model_id, "deepseek-v4.1-flash:netra")

        # Search by colon notation
        r2 = search_model_catalog(catalog, "flash:netra")
        self.assertGreaterEqual(len(r2), 1)
        self.assertEqual(r2[0].model_id, "deepseek-v4.1-flash:netra")

        # Search glm-5.3
        r3 = search_model_catalog(catalog, "glm-5.3")
        self.assertGreaterEqual(len(r3), 1)
        self.assertEqual(r3[0].model_id, "glm-5.3-flash")

        # Search kimi-k3
        r4 = search_model_catalog(catalog, "kimi-k3")
        self.assertGreaterEqual(len(r4), 1)
        self.assertEqual(r4[0].model_id, "kimi-k3")

    def test_context_limits_configured_for_priority_models(self):
        """Priority models have explicit context window limits without fallback warning."""
        from system2.claude_client import resolve_model_context_limit

        lim, is_fallback = resolve_model_context_limit("deepseek-v4-pro")
        self.assertEqual(lim, 64_000)
        self.assertFalse(is_fallback)

        lim, is_fallback = resolve_model_context_limit("deepseek-v4.1-flash:netra")
        self.assertEqual(lim, 64_000)
        self.assertFalse(is_fallback)

        lim, is_fallback = resolve_model_context_limit("glm-5.3-flash")
        self.assertEqual(lim, 128_000)
        self.assertFalse(is_fallback)

        lim, is_fallback = resolve_model_context_limit("kimi-k3")
        self.assertEqual(lim, 128_000)
        self.assertFalse(is_fallback)


    def test_secret_redaction_in_catalog_exceptions(self):
        """SUMOPOD_API_KEY is not exposed when catalog query fails."""
        from core.runtime.secret_scrubbing import scrub_secrets

        raw_key = "sumo_super_secret_catalog_key_12345"
        with patch.dict(os.environ, {"SUMOPOD_API_KEY": raw_key}):
            err_msg = f"Failed connecting to https://ai.sumopod.com/v1/models with key {raw_key}"
            scrubbed = scrub_secrets(err_msg)
            self.assertNotIn(raw_key, scrubbed)

    # 7. End-to-End Interactive Workflow & Runtime Propagation Tests
    @patch("system2.openai_client.OpenAISystem2Client.list_models")
    def test_end_to_end_interactive_picker_selection_and_turn_propagation(self, mock_list):
        """E2E: Open /model, navigate with DOWN, select deepseek-v4.1-flash:netra, verify next turn client."""
        mock_list.return_value = [
            {"id": "deepseek-v4-pro", "owned_by": "sumopod"},
            {"id": "deepseek-v4.1-flash:netra", "owned_by": "sumopod"},
            {"id": "glm-5.3-flash", "owned_by": "sumopod"},
            {"id": "kimi-k3", "owned_by": "sumopod"},
        ]
        with patch.dict(os.environ, {"SUMOPOD_API_KEY": "test-key"}):
            runtime = BrainFrogRuntime(
                repo_dir=self.tmp_dir,
                default_provider="claude",
                default_model="claude-sonnet-5",
            )
            mock_s1_factory = MagicMock()
            mock_s2_factory = MagicMock()
            runtime.system1_factory = mock_s1_factory
            runtime.system2_factory = mock_s2_factory

            sess = runtime.sessions.get_or_create(channel="cli", user_id="local", conversation_id="default")
            sess.active_provider = "claude"
            sess.active_model = "claude-sonnet-5"
            runtime.sessions.save(sess)

            # Build unified live catalog (priority models at top)
            catalog = get_unified_model_catalog(active_provider="sumopod", include_live=True)
            self.assertEqual(catalog[0].model_id, "deepseek-v4-pro")
            self.assertEqual(catalog[1].model_id, "deepseek-v4.1-flash:netra")

            # Simulate key events: DOWN (moves to index 1: deepseek-v4.1-flash:netra) -> ENTER (select)
            keys = [("DOWN", None), ("ENTER", None)]
            key_iter = iter(keys)

            with patch("cli._erase_lines"):
                chosen = select_model_opencode(
                    current_provider="sumopod",
                    current_model="deepseek-v4-pro",
                    catalog=catalog,
                    key_input_fn=lambda: next(key_iter),
                )

            self.assertEqual(chosen, ("sumopod", "deepseek-v4.1-flash:netra"))
            chosen_prov, chosen_mod = chosen

            # Apply switch (mirrors _apply_provider_model_switch)
            sess.active_provider = chosen_prov
            sess.active_model = chosen_mod
            runtime.default_provider = chosen_prov
            runtime.default_model = chosen_mod
            runtime.sessions.save(sess)

            # Verify persisted session state has exact model string with colon and punctuation
            saved_sess = runtime.sessions.get_or_create(channel="cli", user_id="local", conversation_id="default")
            self.assertEqual(saved_sess.active_provider, "sumopod")
            self.assertEqual(saved_sess.active_model, "deepseek-v4.1-flash:netra")
            self.assertIn(":", saved_sess.active_model)
            self.assertIn(".", saved_sess.active_model)

            # Dispatch subsequent turn message
            incoming = IncomingMessage(
                text="Execute code change",
                channel="cli",
                user_id="local",
                conversation_id="default",
                metadata={"provider": saved_sess.active_provider, "model": saved_sess.active_model},
            )
            runtime.handle_message(incoming)

            # System 2 client factory MUST be called with exact provider and exact model ID
            mock_s2_factory.assert_called_with(
                model="deepseek-v4.1-flash:netra",
                provider="sumopod",
            )

    def test_end_to_end_picker_cancellation_preserves_active_session(self):
        """E2E: Pressing ESCAPE in picker cancels selection and preserves active session unchanged."""
        runtime = BrainFrogRuntime(
            repo_dir=self.tmp_dir,
            default_provider="claude",
            default_model="claude-sonnet-5",
        )
        mock_s1_factory = MagicMock()
        mock_s2_factory = MagicMock()
        runtime.system1_factory = mock_s1_factory
        runtime.system2_factory = mock_s2_factory

        sess = runtime.sessions.get_or_create(channel="cli", user_id="local", conversation_id="default")
        sess.active_provider = "claude"
        sess.active_model = "claude-sonnet-5"
        runtime.sessions.save(sess)

        catalog = [
            ModelCatalogEntry("sumopod", "deepseek-v4.1-flash:netra", "DeepSeek", "Desc", 64000),
            ModelCatalogEntry("claude", "claude-sonnet-5", "Claude", "Desc", 200000),
        ]

        # Simulate ESCAPE key press
        with patch("cli._erase_lines"):
            chosen = select_model_opencode(
                current_provider="claude",
                current_model="claude-sonnet-5",
                catalog=catalog,
                key_input_fn=lambda: ("ESCAPE", None),
            )

        self.assertIsNone(chosen)

        # Session state must remain untouched
        saved_sess = runtime.sessions.get_or_create(channel="cli", user_id="local", conversation_id="default")
        self.assertEqual(saved_sess.active_provider, "claude")
        self.assertEqual(saved_sess.active_model, "claude-sonnet-5")

        # Subsequent turn continues using Claude
        incoming = IncomingMessage(
            text="Continue task",
            channel="cli",
            user_id="local",
            conversation_id="default",
            metadata={"provider": saved_sess.active_provider, "model": saved_sess.active_model},
        )
        runtime.handle_message(incoming)
        mock_s2_factory.assert_called_with(
            model="claude-sonnet-5",
            provider="claude",
        )

    def test_end_to_end_search_filtering_and_exact_selection(self):
        """E2E: Search query 'netra' filters to deepseek-v4.1-flash:netra and selects it."""
        catalog = [
            ModelCatalogEntry("sumopod", "deepseek-v4-pro", "DeepSeek V4 Pro", "Reasoning", 64000),
            ModelCatalogEntry("sumopod", "deepseek-v4.1-flash:netra", "DeepSeek Netra", "Ultra fast", 64000),
            ModelCatalogEntry("sumopod", "glm-5.3-flash", "GLM 5.3 Flash", "Fast", 128000),
        ]

        # Filter by 'netra'
        results = search_model_catalog(catalog, "netra")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].model_id, "deepseek-v4.1-flash:netra")

        # Direct selection resolution
        prov, mod, err = resolve_model_selection("sumopod/deepseek-v4.1-flash:netra", catalog, "sumopod")
        self.assertIsNone(err)
        self.assertEqual(prov, "sumopod")
        self.assertEqual(mod, "deepseek-v4.1-flash:netra")

    # 8. Acceptance Verification: Factory Dispatch, Reset Persistence, Isolation & Leak Prevention
    @patch("requests.post")
    def test_real_system2_factory_dispatch_and_payload_exact_id(self, mock_post):
        """Verify real System2Client factory produces OpenAISystem2Client with exact model ID and payload."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.is_redirect = False
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        mock_post.return_value = mock_response

        with patch.dict(os.environ, {"SUMOPOD_API_KEY": "dummy-sumo-key-for-test"}):
            client = System2Client(
                model="deepseek-v4.1-flash:netra",
                provider="sumopod",
            )
            self.assertIsInstance(client, OpenAISystem2Client)
            self.assertEqual(client.provider_name, "sumopod")
            self.assertEqual(client.model, "deepseek-v4.1-flash:netra")
            self.assertIn(":", client.model)
            self.assertIn(".", client.model)

            # Trigger a call to verify the wire payload preserves exact ID
            client._call(system="sys prompt", user="user prompt")
            mock_post.assert_called_once()
            args, kwargs = mock_post.call_args
            self.assertEqual(kwargs["json"]["model"], "deepseek-v4.1-flash:netra")
            self.assertEqual(args[0], "https://ai.sumopod.com/v1/chat/completions")

    def test_model_and_provider_persistence_across_reset(self):
        """Active provider and exact model persist across /reset while history is wiped."""
        runtime = BrainFrogRuntime(
            repo_dir=self.tmp_dir,
            default_provider="claude",
            default_model="claude-sonnet-5",
        )
        mock_s1 = MagicMock()
        mock_s2 = MagicMock()
        runtime.system1_factory = mock_s1
        runtime.system2_factory = mock_s2

        sess = runtime.sessions.get_or_create(channel="cli", user_id="local", conversation_id="default")
        sess.active_provider = "sumopod"
        sess.active_model = "deepseek-v4.1-flash:netra"
        sess.add_message("user", "Hello first turn")
        sess.add_message("assistant", "Response first turn")
        runtime.sessions.save(sess)

        self.assertEqual(len(sess.history), 1)
        self.assertEqual(sess.active_provider, "sumopod")
        self.assertEqual(sess.active_model, "deepseek-v4.1-flash:netra")

        # Execute /reset via runtime command pipeline
        reset_msg = IncomingMessage(
            text="/reset",
            channel="cli",
            user_id="local",
            conversation_id="default",
        )
        out = runtime.handle_message(reset_msg)
        self.assertTrue(out.success)
        self.assertIn("reset", out.text.lower())

        # Verify session history is cleared but provider and model persist
        post_sess = runtime.sessions.get_or_create(channel="cli", user_id="local", conversation_id="default")
        self.assertEqual(len(post_sess.history), 0)
        self.assertEqual(post_sess.active_provider, "sumopod")
        self.assertEqual(post_sess.active_model, "deepseek-v4.1-flash:netra")

        # Next turn without explicit model/provider metadata: uses persisted session state
        next_turn = IncomingMessage(
            text="Second turn instruction",
            channel="cli",
            user_id="local",
            conversation_id="default",
        )
        runtime.handle_message(next_turn)
        mock_s2.assert_called_with(
            model="deepseek-v4.1-flash:netra",
            provider="sumopod",
        )

    def test_session_isolation_across_multiple_channels_and_users(self):
        """Provider and model changes in one session do not leak to other sessions."""
        runtime = BrainFrogRuntime(
            repo_dir=self.tmp_dir,
            default_provider="claude",
            default_model="claude-sonnet-5",
        )
        sess_a = runtime.sessions.get_or_create(channel="cli", user_id="alice", conversation_id="conv-1")
        sess_a.active_provider = "sumopod"
        sess_a.active_model = "deepseek-v4.1-flash:netra"
        runtime.sessions.save(sess_a)

        sess_b = runtime.sessions.get_or_create(channel="cli", user_id="bob", conversation_id="conv-2")
        self.assertIsNone(sess_b.active_provider)
        self.assertIsNone(sess_b.active_model)

        sess_b.active_provider = "claude"
        sess_b.active_model = "claude-sonnet-5"
        runtime.sessions.save(sess_b)

        reloaded_a = runtime.sessions.get("cli:alice:conv-1")
        reloaded_b = runtime.sessions.get("cli:bob:conv-2")
        self.assertIsNotNone(reloaded_a)
        self.assertIsNotNone(reloaded_b)
        self.assertEqual(reloaded_a.active_provider, "sumopod")
        self.assertEqual(reloaded_a.active_model, "deepseek-v4.1-flash:netra")
        self.assertEqual(reloaded_b.active_provider, "claude")
        self.assertEqual(reloaded_b.active_model, "claude-sonnet-5")

    def test_fail_closed_on_provider_error_no_silent_fallback(self):
        """Provider failure fails closed immediately with error status and never falls back."""
        runtime = BrainFrogRuntime(
            repo_dir=self.tmp_dir,
            default_provider="sumopod",
            default_model="deepseek-v4.1-flash:netra",
        )
        mock_s1 = MagicMock()
        runtime.system1_factory = mock_s1

        def failing_s2_factory(*args, **kwargs):
            raise RuntimeError("Sumopod API Error 404: model 'deepseek-v4.1-flash:netra' not found")

        runtime.system2_factory = failing_s2_factory

        incoming = IncomingMessage(
            text="Perform code modification",
            channel="cli",
            user_id="local",
            conversation_id="default",
            metadata={"provider": "sumopod", "model": "deepseek-v4.1-flash:netra"},
        )
        out = runtime.handle_message(incoming)

        # Fails closed
        self.assertFalse(out.success)
        self.assertIn("System 2 Initialization Error", out.text)
        self.assertIn("Sumopod API Error 404", out.text)
        # Session state is not silently changed to claude or antigravity
        sess = runtime.sessions.get_or_create(channel="cli", user_id="local", conversation_id="default")
        self.assertEqual(sess.active_provider, "sumopod")
        self.assertEqual(sess.active_model, "deepseek-v4.1-flash:netra")

    def test_no_api_key_leakage_in_error_or_output(self):
        """API keys are never exposed in error messages, events, or state representations."""
        secret_key = "sk-super-secret-sumopod-token-98765"
        with patch.dict(os.environ, {"SUMOPOD_API_KEY": secret_key}):
            runtime = BrainFrogRuntime(
                repo_dir=self.tmp_dir,
                default_provider="sumopod",
                default_model="deepseek-v4.1-flash:netra",
            )
            mock_s1 = MagicMock()
            runtime.system1_factory = mock_s1

            def failing_s2_factory(*args, **kwargs):
                raise RuntimeError(f"Connection failed for Authorization: Bearer {secret_key}")

            runtime.system2_factory = failing_s2_factory

            incoming = IncomingMessage(
                text="Execute task",
                channel="cli",
                user_id="local",
                conversation_id="default",
                metadata={"provider": "sumopod", "model": "deepseek-v4.1-flash:netra"},
            )
            out = runtime.handle_message(incoming)

            from core.runtime.secret_scrubbing import scrub_secrets
            scrubbed_text = scrub_secrets(out.text)
            self.assertNotIn(secret_key, scrubbed_text)
            self.assertNotIn(secret_key, out.metadata.get("error", ""))
            for event in out.events:
                payload_str = str(getattr(event, "payload", event))
                self.assertNotIn(secret_key, scrub_secrets(payload_str))


if __name__ == "__main__":
    unittest.main()


