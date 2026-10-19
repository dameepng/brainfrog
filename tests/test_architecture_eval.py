"""test_architecture_eval.py — Unit tests for Phase 1 architecture evaluation infrastructure.

Validates:
- Evaluation cases JSON schema and completeness.
- Context taxonomy and strategy validity across all 10 scenarios.
- Uniqueness of scenario IDs.
- Deterministic RubricScore calculation and critical failure triggers.
- Baseline recording template generator and offline evaluation harness.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from evals.architecture_eval import (
    RUBRIC_DIMENSIONS,
    ArchitectureCase,
    ArchitectureStrategy,
    ContextType,
    CriticalFailure,
    ProvidedContext,
    RubricScore,
    load_architecture_cases,
    validate_case_schema,
)
from evals.baseline_runner import (
    evaluate_offline_recording,
    generate_recording_template,
)


class TestArchitectureEvalInfrastructure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_path = (
            Path(__file__).resolve().parent / "fixtures" / "evals" / "architecture_cases.json"
        )
        cls.cases = load_architecture_cases(cls.fixture_path)

    def test_fixture_file_exists(self):
        self.assertTrue(self.fixture_path.exists(), "architecture_cases.json must exist")

    def test_case_count_and_uniqueness(self):
        self.assertGreaterEqual(len(self.cases), 10, "Evaluation suite must have at least 10 scenarios")
        ids = [c.id for c in self.cases]
        self.assertEqual(len(ids), len(set(ids)), "Scenario IDs must be strictly unique")

    def test_taxonomy_coverage(self):
        contexts = {c.context_type_expected for c in self.cases}
        self.assertIn(ContextType.GREENFIELD, contexts)
        self.assertIn(ContextType.BROWNFIELD, contexts)
        self.assertIn(ContextType.MIXED, contexts)
        self.assertIn(ContextType.UNKNOWN, contexts)

    def test_strategies_validity(self):
        for c in self.cases:
            self.assertTrue(len(c.acceptable_strategies) > 0, f"Case {c.id} must have >= 1 acceptable strategy")
            for strat in c.acceptable_strategies:
                self.assertIsInstance(strat, ArchitectureStrategy)

    def test_schema_validator_catches_invalid_cases(self):
        # Missing required field
        invalid_1 = {"id": "SCN-TEST", "title": "Test"}
        ok, errs = validate_case_schema(invalid_1)
        self.assertFalse(ok)
        self.assertTrue(any("Missing required field" in e for e in errs))

        # Invalid context type
        invalid_2 = {
            "id": "SCN-TEST",
            "title": "Test",
            "context_type_expected": "totally_new",
            "prompt": "do something",
            "provided_context": {"repo_tree": ""},
            "expected_reasoning_properties": [],
            "acceptable_strategies": ["keep"],
            "disallowed_behaviors": [],
            "critical_failures": [],
        }
        ok, errs = validate_case_schema(invalid_2)
        self.assertFalse(ok)
        self.assertTrue(any("Invalid 'context_type_expected'" in e for e in errs))

        # Invalid strategy
        invalid_3 = {
            "id": "SCN-TEST",
            "title": "Test",
            "context_type_expected": "greenfield",
            "prompt": "do something",
            "provided_context": {"repo_tree": ""},
            "expected_reasoning_properties": [],
            "acceptable_strategies": ["magic_rewrite"],
            "disallowed_behaviors": [],
            "critical_failures": [],
        }
        ok, errs = validate_case_schema(invalid_3)
        self.assertFalse(ok)
        self.assertTrue(any("Invalid strategy" in e for e in errs))

    def test_rubric_dimensions_count(self):
        self.assertEqual(len(RUBRIC_DIMENSIONS), 9)

    def test_rubric_scoring_calculation(self):
        rubric = RubricScore(
            context_classification=3,
            evidence_grounding=2,
            compatibility_reasoning=3,
            scope_discipline=2,
            strategy_quality=2,
            uncertainty_handling=3,
            verification_quality=2,
            architecture_proportionality=3,
            execution_boundary_awareness=3,
        )
        self.assertEqual(rubric.total_score, 23)
        self.assertEqual(rubric.max_score, 27)
        self.assertAlmostEqual(rubric.percentage, (23 / 27) * 100, places=1)
        self.assertTrue(rubric.passed)

    def test_rubric_critical_failure_zeroes_score(self):
        rubric = RubricScore(
            context_classification=3,
            evidence_grounding=3,
            compatibility_reasoning=3,
            scope_discipline=3,
            strategy_quality=3,
            uncertainty_handling=3,
            verification_quality=3,
            architecture_proportionality=3,
            execution_boundary_awareness=3,
            critical_failures_triggered=[CriticalFailure.CF1_DESTRUCTIVE_REWRITE],
        )
        # Even with all 3s (27 points), triggering a critical failure causes an instant 0 and fail
        self.assertEqual(rubric.total_score, 0)
        self.assertEqual(rubric.percentage, 0.0)
        self.assertFalse(rubric.passed)

    def test_template_generation_and_offline_eval(self):
        tmpl = generate_recording_template(self.cases)
        self.assertEqual(tmpl["cases_count"], len(self.cases))
        self.assertEqual(len(tmpl["results"]), len(self.cases))

        # Fill in one result and evaluate offline
        with tempfile.TemporaryDirectory() as tmpdir:
            rec_file = Path(tmpdir) / "recording.json"
            tmpl["results"][0]["status"] = "completed"
            tmpl["results"][0]["rubric_scores"] = {dim: 2 for dim in RUBRIC_DIMENSIONS}
            rec_file.write_text(json.dumps(tmpl), encoding="utf-8")

            results = evaluate_offline_recording(rec_file)
            self.assertEqual(len(results), len(self.cases))
            first = results[0]
            self.assertEqual(first.rubric_score.total_score, 18)
            # 18 is below 19 threshold, so passed should be False
            self.assertFalse(first.rubric_score.passed)

            # If score is 19/27, it passes
            tmpl["results"][0]["rubric_scores"]["context_classification"] = 3
            rec_file.write_text(json.dumps(tmpl), encoding="utf-8")
            results2 = evaluate_offline_recording(rec_file)
            self.assertEqual(results2[0].rubric_score.total_score, 19)
            self.assertTrue(results2[0].rubric_score.passed)


    def test_scored_baseline_artifact_integrity(self):
        scored_path = (
            Path(__file__).resolve().parent.parent
            / "evals"
            / "results"
            / "architecture-baseline-gemini-3.8-flash-high-scored.json"
        )
        if not scored_path.exists():
            self.skipTest("Scored baseline artifact not yet generated")
        data = json.loads(scored_path.read_text(encoding="utf-8"))
        self.assertEqual(data["model"], "gemini-3.8-flash-high")
        self.assertEqual(data["provider"], "antigravity")
        self.assertEqual(len(data["cases"]), 10)
        self.assertEqual(data["aggregate"]["total_scenarios"], 10)
        self.assertEqual(data["aggregate"]["passed_count"], 9)
        self.assertEqual(data["aggregate"]["failed_count"], 1)
        self.assertEqual(data["aggregate"]["critical_failures_count"], 0)
        self.assertAlmostEqual(data["aggregate"]["mean_score"], 23.4, places=1)
        for c in data["cases"]:
            self.assertIn("rubric_scores", c)
            self.assertEqual(len(c["rubric_scores"]), 9)
            self.assertIn("evidence_notes", c)

    def test_raw_baseline_artifact_integrity_and_alignment(self):
        raw_path = (
            Path(__file__).resolve().parent.parent
            / "evals"
            / "results"
            / "architecture-baseline-gemini-3.8-flash-high.json"
        )
        if not raw_path.exists():
            self.skipTest("Raw baseline artifact not yet generated")
        raw_data = json.loads(raw_path.read_text(encoding="utf-8"))
        self.assertEqual(len(raw_data["results"]), 10)
        self.assertEqual(raw_data["metadata"]["model"], "gemini-3.8-flash-high")
        self.assertEqual(raw_data["metadata"]["provider"], "antigravity")

        fix_ids = [c.id for c in self.cases]
        raw_ids = [r["id"] for r in raw_data["results"]]
        self.assertEqual(fix_ids, raw_ids, "Raw scenario IDs must match canonical fixtures 1-to-1")

        for fc, rr in zip(self.cases, raw_data["results"]):
            self.assertEqual(fc.prompt.strip(), rr["prompt"].strip())
            self.assertEqual(rr["status"], "completed")
            self.assertGreater(len(rr["raw_output"]), 0, f"Raw output for {rr['id']} must be non-empty")
            self.assertIsInstance(rr.get("parsed_output"), dict)
            self.assertIsNone(rr.get("error"))
            self.assertIsNone(rr.get("json_parse_error"))

    def test_scored_baseline_rubric_sum_consistency(self):
        scored_path = (
            Path(__file__).resolve().parent.parent
            / "evals"
            / "results"
            / "architecture-baseline-gemini-3.8-flash-high-scored.json"
        )
        if not scored_path.exists():
            self.skipTest("Scored baseline artifact not yet generated")
        data = json.loads(scored_path.read_text(encoding="utf-8"))
        scores = []
        for c in data["cases"]:
            rubric = c["rubric_scores"]
            self.assertEqual(
                sum(rubric.values()),
                c["total_score"],
                f"Sum of rubric dimensions must equal total_score for {c['id']}",
            )
            scores.append(c["total_score"])

        self.assertAlmostEqual(sum(scores) / len(scores), data["aggregate"]["mean_score"], places=1)
        self.assertEqual(min(scores), data["aggregate"]["min_score"])
        self.assertEqual(max(scores), data["aggregate"]["max_score"])
        self.assertEqual(sum(1 for s in scores if s >= 19), data["aggregate"]["passed_count"])


if __name__ == "__main__":
    unittest.main()
