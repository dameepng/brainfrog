import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.memory import (
    generate_proactive_suggestion_prompt,
    generate_reflection_prompt,
    is_generic_suggestion,
)
from orchestrator import Orchestrator, PlanStep, RunConfig, StepResult
from typing import Any, Dict, cast
from system1.base import Answer, SystemOneClient


class MockS1(SystemOneClient):
    name = "mock_jev"

    def decide(self, state: Dict[str, Any], questions: Dict[str, Any]) -> Dict[str, Answer]:
        mock_ans = MagicMock(spec=Answer)
        mock_ans.choice = "core"
        mock_ans.confidence = 0.95
        mock_ans.noul = 0.95
        mock_ans.score = 0
        return {k: cast(Answer, mock_ans) for k in questions}


class MockS2:
    model = "mock-s2"
    provider_name = "mock"

    def __init__(self, reflect_response=None):
        self.reflect_response = reflect_response or '{"suggestion": null}'

    def write_code(self, step, task, file_contents, **kwargs):
        return {"app.py": "print('hello')\n"}

    def review_and_fix(self, task, step, file_contents, test_output, **kwargs):
        return {"app.py": "print('fixed')\n"}

    def draft_pr(self, task, changed_files, test_status):
        return {"title": "feat: test", "body": "test"}

    def _call(self, system, prompt, max_tokens=1000):
        return self.reflect_response


class TestProactiveSuggestions(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="brainfrog_proactive_test_")
        self.repo_dir = Path(self.test_dir).resolve()
        (self.repo_dir / "app.py").write_text("print('hello')\n", encoding="utf-8")

    def test_generic_suggestion_filter(self):
        self.assertTrue(is_generic_suggestion("Pastikan untuk selalu testing dengan baik sebelum deploy."))
        self.assertTrue(is_generic_suggestion("Always write tests for your code."))
        self.assertTrue(is_generic_suggestion("Pastikan kode rapi dan bersih."))
        self.assertFalse(is_generic_suggestion("Catatan: gambar yang baru ditambahkan masih hotlink ke Unsplash. Mau sekalian saya pindahkan ke asset lokal?"))
        self.assertFalse(is_generic_suggestion(None))
        self.assertFalse(is_generic_suggestion(""))

    def test_prompt_generators(self):
        prompt_clean = generate_proactive_suggestion_prompt("Add doctor card", "Add DoctorCard.tsx", "diff --git ...")
        self.assertIn("Proactive Suggestion rules", prompt_clean)
        self.assertIn("DoctorCard.tsx", prompt_clean)
        self.assertIn("diff --git", prompt_clean)

        prompt_combined = generate_reflection_prompt(
            "Add doctor card",
            retries=1,
            visual_fixed=False,
            include_proactive=True,
            step_desc="Add DoctorCard.tsx",
            diff_snippet="diff --git ...",
        )
        self.assertIn("1. Internal Learning", prompt_combined)
        self.assertIn("2. Proactive Suggestion for User", prompt_combined)

    def test_auto_reflect_clean_run_with_gap(self):
        sug_text = "Catatan: DoctorCard masih menggunakan static placeholder data. Mau sekalian saya buatkan mock data?"
        s2 = MockS2(reflect_response=json.dumps({"suggestion": sug_text}))
        cfg = RunConfig(repo_dir=self.repo_dir, task="add doctor card", test_command=["python", "--version"])
        orch = Orchestrator(MockS1(), s2, cfg)

        step = PlanStep("1", "add doctor card", ["app.py"])
        result = orch._auto_reflect("add doctor card", retries=0, visual_fixed=False, step=step, diff_snippet="diff ...")
        self.assertEqual(result, sug_text)

    def test_auto_reflect_clean_run_without_gap(self):
        s2 = MockS2(reflect_response=json.dumps({"suggestion": None}))
        cfg = RunConfig(repo_dir=self.repo_dir, task="add doctor card", test_command=["python", "--version"])
        orch = Orchestrator(MockS1(), s2, cfg)

        step = PlanStep("1", "add doctor card", ["app.py"])
        result = orch._auto_reflect("add doctor card", retries=0, visual_fixed=False, step=step, diff_snippet="diff ...")
        self.assertIsNone(result)

    def test_auto_reflect_discards_generic_suggestion(self):
        s2 = MockS2(reflect_response=json.dumps({"suggestion": "Catatan: pastikan untuk selalu testing dengan baik. Mau sekalian saya jalankan test?"}))
        cfg = RunConfig(repo_dir=self.repo_dir, task="add doctor card", test_command=["python", "--version"])
        orch = Orchestrator(MockS1(), s2, cfg)

        step = PlanStep("1", "add doctor card", ["app.py"])
        result = orch._auto_reflect("add doctor card", retries=0, visual_fixed=False, step=step, diff_snippet="diff ...")
        self.assertIsNone(result)

    def test_auto_reflect_combined_with_retries(self):
        sug_text = "Catatan: asset logo masih berisiko 404. Mau sekalian saya download ke local?"
        resp = {
            "learnings": [{"rule": "Double check image imports", "tags": ["frontend"]}],
            "suggestion": sug_text,
        }
        s2 = MockS2(reflect_response=json.dumps(resp))
        cfg = RunConfig(repo_dir=self.repo_dir, task="add doctor card", test_command=["python", "--version"])
        orch = Orchestrator(MockS1(), s2, cfg)

        step = PlanStep("1", "add doctor card", ["app.py"])
        result = orch._auto_reflect("add doctor card", retries=1, visual_fixed=False, error_summary="err", step=step, diff_snippet="diff ...")
        self.assertEqual(result, sug_text)

        # Verify learning was saved to workspace memory
        learnings_file = self.repo_dir / ".brainfrog" / "learnings.json"
        self.assertTrue(learnings_file.exists())
        data = json.loads(learnings_file.read_text(encoding="utf-8"))
        self.assertEqual(len(data["learnings"]), 1)
        self.assertEqual(data["learnings"][0]["rule"], "Double check image imports")

    def test_step_result_carries_suggestion(self):
        sug_text = "Catatan: fitur baru belum ada test unit. Mau sekalian saya buatkan test_feature.py?"
        step = PlanStep("1", "create feature", ["feature.py"])
        res = StepResult(step, "opened_pr", 0, "PR opened", suggestion=sug_text)
        self.assertEqual(res.suggestion, sug_text)
        self.assertEqual(res.outcome, "opened_pr")


if __name__ == "__main__":
    unittest.main()
