"""Tests for memory.py — Continuous Learning engine."""
import json
import time
from pathlib import Path
from unittest import TestCase, main

# Ensure the package root is importable
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

from memory import (
    Learning,
    MemoryStore,
    add_learning,
    clear_learnings,
    format_learnings_for_prompt,
    generate_reflection_prompt,
    get_relevant_learnings,
    infer_tags,
    load_memory,
    remove_learning,
    save_memory,
)


class TestLearningModel(TestCase):
    def test_round_trip(self):
        l = Learning(id="L-123", rule="Use dark mode", tags=["style", "frontend"], source="explicit", created_at=time.time())
        d = l.to_dict()
        l2 = Learning.from_dict(d)
        self.assertEqual(l.id, l2.id)
        self.assertEqual(l.rule, l2.rule)
        self.assertEqual(l.tags, l2.tags)

    def test_extra_fields_ignored(self):
        d = {"id": "L-1", "rule": "test", "tags": [], "source": "explicit", "unknown_field": 42}
        l = Learning.from_dict(d)
        self.assertEqual(l.id, "L-1")


class TestMemoryStore(TestCase):
    def test_round_trip(self):
        store = MemoryStore(learnings=[
            Learning(id="L-1", rule="Rule one", tags=["frontend"], source="explicit"),
            Learning(id="L-2", rule="Rule two", tags=["backend"], source="reflection"),
        ])
        d = store.to_dict()
        store2 = MemoryStore.from_dict(d)
        self.assertEqual(len(store2.learnings), 2)
        self.assertEqual(store2.learnings[0].id, "L-1")

    def test_empty(self):
        store = MemoryStore.from_dict({})
        self.assertEqual(len(store.learnings), 0)
        self.assertEqual(store.version, 1)


class TestPersistence(TestCase):
    def setUp(self):
        self.tmp_path = Path(__file__).parent / ".brainfrog" / "test_learnings.json"

    def tearDown(self):
        if self.tmp_path.exists():
            self.tmp_path.unlink()

    def test_save_and_load(self):
        store = MemoryStore(learnings=[
            Learning(id="L-test", rule="Test rule", tags=["test"], source="explicit")
        ])
        save_memory(store, self.tmp_path)
        self.assertTrue(self.tmp_path.exists())

        loaded = load_memory(self.tmp_path)
        self.assertEqual(len(loaded.learnings), 1)
        self.assertEqual(loaded.learnings[0].rule, "Test rule")

    def test_load_nonexistent(self):
        store = load_memory(Path("/nonexistent/path/learnings.json"))
        self.assertEqual(len(store.learnings), 0)

    def test_load_corrupt(self):
        self.tmp_path.parent.mkdir(parents=True, exist_ok=True)
        self.tmp_path.write_text("NOT VALID JSON", encoding="utf-8")
        store = load_memory(self.tmp_path)
        self.assertEqual(len(store.learnings), 0)


class TestInferTags(TestCase):
    def test_frontend(self):
        tags = infer_tags("Always use CSS Grid for layout")
        self.assertIn("frontend", tags)

    def test_backend(self):
        tags = infer_tags("Add proper API auth middleware")
        self.assertIn("backend", tags)

    def test_style(self):
        tags = infer_tags("Avoid card-ception nesting in UI")
        self.assertIn("style", tags)

    def test_multiple_tags(self):
        tags = infer_tags("Use CSS Grid layout with proper contrast and whitespace")
        self.assertIn("frontend", tags)
        self.assertIn("style", tags)

    def test_fallback_general(self):
        tags = infer_tags("Remember to follow the checklist")
        self.assertIn("general", tags)


class TestAddLearning(TestCase):
    def test_basic_add(self):
        store = MemoryStore()
        l = add_learning(store, rule="Use flexbox over float", source="explicit")
        self.assertEqual(len(store.learnings), 1)
        self.assertTrue(l.id.startswith("L-"))
        self.assertIn("frontend", l.tags)

    def test_dedup(self):
        store = MemoryStore()
        l1 = add_learning(store, rule="Always use dark mode for UI")
        l2 = add_learning(store, rule="Always use dark mode for UI design")
        # Should deduplicate via word overlap
        self.assertEqual(len(store.learnings), 1)
        self.assertEqual(l2.hit_count, 2)

    def test_explicit_tags(self):
        store = MemoryStore()
        l = add_learning(store, rule="Custom rule", tags=["custom", "special"])
        self.assertEqual(l.tags, ["custom", "special"])


class TestRemoveLearning(TestCase):
    def test_remove_existing(self):
        store = MemoryStore(learnings=[
            Learning(id="L-1", rule="Rule one", tags=["test"], source="explicit"),
            Learning(id="L-2", rule="Rule two", tags=["test"], source="explicit"),
        ])
        result = remove_learning(store, "L-1")
        self.assertTrue(result)
        self.assertEqual(len(store.learnings), 1)
        self.assertEqual(store.learnings[0].id, "L-2")

    def test_remove_nonexistent(self):
        store = MemoryStore(learnings=[
            Learning(id="L-1", rule="Rule one", tags=["test"], source="explicit"),
        ])
        result = remove_learning(store, "L-999")
        self.assertFalse(result)
        self.assertEqual(len(store.learnings), 1)


class TestClearLearnings(TestCase):
    def test_clear(self):
        store = MemoryStore(learnings=[
            Learning(id="L-1", rule="A", tags=[], source="explicit"),
            Learning(id="L-2", rule="B", tags=[], source="explicit"),
        ])
        count = clear_learnings(store)
        self.assertEqual(count, 2)
        self.assertEqual(len(store.learnings), 0)


class TestGetRelevantLearnings(TestCase):
    def setUp(self):
        self.store = MemoryStore(learnings=[
            Learning(id="L-1", rule="Use CSS Grid", tags=["frontend", "style"], source="explicit", hit_count=5),
            Learning(id="L-2", rule="Add error handling to API", tags=["backend"], source="reflection", hit_count=2),
            Learning(id="L-3", rule="Always run tests before commit", tags=["test", "git"], source="explicit", hit_count=10),
            Learning(id="L-4", rule="Use glassmorphism backdrop-filter", tags=["frontend", "style"], source="reflection", hit_count=1),
        ])

    def test_filter_by_task(self):
        results = get_relevant_learnings(self.store, task="Fix the CSS layout issues")
        # Should return frontend/style learnings
        ids = [l.id for l in results]
        self.assertIn("L-1", ids)
        self.assertIn("L-4", ids)

    def test_filter_by_files(self):
        results = get_relevant_learnings(self.store, touched_files=["style.css", "index.html"])
        ids = [l.id for l in results]
        self.assertIn("L-1", ids)

    def test_max_items(self):
        results = get_relevant_learnings(self.store, max_items=2)
        self.assertLessEqual(len(results), 2)

    def test_empty_store(self):
        results = get_relevant_learnings(MemoryStore())
        self.assertEqual(len(results), 0)


class TestFormatLearnings(TestCase):
    def test_format_basic(self):
        learnings = [
            Learning(id="L-1", rule="Use CSS Grid", tags=["frontend"], source="explicit"),
            Learning(id="L-2", rule="Dark mode preferred", tags=["style"], source="explicit"),
        ]
        result = format_learnings_for_prompt(learnings)
        self.assertIn("[Continuous Learning Memory", result)
        self.assertIn("Use CSS Grid", result)
        self.assertIn("Dark mode preferred", result)

    def test_empty(self):
        result = format_learnings_for_prompt([])
        self.assertEqual(result, "")

    def test_token_budget(self):
        learnings = [
            Learning(id=f"L-{i}", rule=f"Rule number {i} " * 20, tags=["general"], source="explicit")
            for i in range(100)
        ]
        result = format_learnings_for_prompt(learnings, max_tokens_approx=200)
        self.assertIn("omitted", result)


class TestReflectionPrompt(TestCase):
    def test_basic(self):
        prompt = generate_reflection_prompt("Fix button styling", retries=2, visual_fixed=True)
        self.assertIn("2 retry", prompt)
        self.assertIn("Visual Quality Gate", prompt)
        self.assertIn("JSON", prompt)

    def test_no_retries(self):
        prompt = generate_reflection_prompt("Fix button styling", retries=0, visual_fixed=False)
        self.assertNotIn("retry", prompt)


if __name__ == "__main__":
    main()
