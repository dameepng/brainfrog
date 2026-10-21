"""Phase 1D: declarative browser plan parsing and strict validation."""
from __future__ import annotations

import json
import unittest

from core.runtime.browser_plan import (
    ASSERTION_ACTIONS,
    MAX_PLAN_BUDGET_MS,
    MAX_SELECTOR_LENGTH,
    MAX_TOTAL_STEPS,
    BrowserPlanValidationError,
    parse_browser_plan,
    parse_browser_plan_data,
)
from core.verifier import parse_spec_markdown
from tests.browser_verification_helpers import browser_criterion, spec_document


def _plan(steps, **top):
    return json.dumps(dict({"path": "/login", "steps": steps}, **top))


VISIBLE = {"action": "expect_visible", "selector": "#login-form"}


class TestBrowserPlanParsing(unittest.TestCase):
    def test_01_valid_plan_parses_with_assertions_and_actions(self) -> None:
        plan = parse_browser_plan(_plan([
            VISIBLE,
            {"action": "fill", "selector": "#email", "value": "not-an-email"},
            {"action": "click", "selector": "#submit"},
            {"action": "expect_text", "selector": "#email-error", "contains": "Invalid email"},
        ], viewport={"width": 1280, "height": 720}))
        self.assertEqual(plan.path, "/login")
        self.assertEqual((plan.viewport.width, plan.viewport.height), (1280, 720))
        self.assertEqual([s.action for s in plan.steps], ["expect_visible", "fill", "click", "expect_text"])
        self.assertEqual(plan.assertion_count, 2)
        self.assertTrue(plan.steps[0].is_assertion)
        self.assertFalse(plan.steps[1].is_assertion)
        self.assertFalse(plan.steps[2].is_assertion)

    def test_02_canonical_hash_is_stable_and_content_sensitive(self) -> None:
        a = parse_browser_plan('{"steps":[{"selector":"#a","action":"expect_visible"}],"path":"/x"}')
        b = parse_browser_plan('{\n "path": "/x",\n "steps": [{"action": "expect_visible", "selector": "#a"}]\n}')
        c = parse_browser_plan('{"path": "/y", "steps": [{"action": "expect_visible", "selector": "#a"}]}')
        self.assertEqual(a.plan_sha256(), b.plan_sha256())
        self.assertNotEqual(a.plan_sha256(), c.plan_sha256())
        self.assertEqual(json.loads(a.canonical_json())["steps"][0]["timeout_ms"], 5000)

    def test_03_invalid_documents_rejected(self) -> None:
        for bad in ("", "   ", "not json", "[]", '"string"', _plan([VISIBLE], extra=1)):
            with self.subTest(bad=bad):
                with self.assertRaises(BrowserPlanValidationError):
                    parse_browser_plan(bad)

    def test_04_unknown_action_and_unknown_keys_rejected(self) -> None:
        for step in ({"action": "hover", "selector": "#a"},
                     {"action": "Click", "selector": "#a"},
                     {"action": "click", "selector": "#a", "force": True},
                     {"action": "expect_visible", "selector": "#a", "script": "x"},
                     {"selector": "#a"}):
            with self.subTest(step=step):
                with self.assertRaises(BrowserPlanValidationError):
                    parse_browser_plan(_plan([step, VISIBLE]))

    def test_05_arbitrary_code_actions_rejected(self) -> None:
        for action in ("evaluate", "execute_script", "javascript", "eval", "script", "shell", "python",
                       "arbitrary_network_request", "exec", "download", "upload", "screenshot"):
            with self.subTest(action=action):
                with self.assertRaisesRegex(BrowserPlanValidationError, "forbidden action"):
                    parse_browser_plan(_plan([{"action": action, "selector": "#a"}, VISIBLE]))

    def test_06_external_and_unsafe_urls_rejected(self) -> None:
        bad_paths = [
            "https://example.com", "http://app:8080/login", "//example.com/x", "javascript:alert(1)",
            "file:///etc/passwd", "data:text/html,hi", "login", "", "/a b", "/\\\\evil", "/..", "/a/../b",
            "/%2e%2e/secret", "/a\x00b", "/" + "a" * 600,
        ]
        for p in bad_paths:
            with self.subTest(path=p):
                with self.assertRaises(BrowserPlanValidationError):
                    parse_browser_plan(json.dumps({"path": p, "steps": [VISIBLE]}))
                with self.assertRaises(BrowserPlanValidationError):
                    parse_browser_plan(_plan([{"action": "goto_relative", "path": p}, VISIBLE]))
                with self.assertRaises(BrowserPlanValidationError):
                    parse_browser_plan(_plan([{"action": "expect_url_path", "path": p}]))

    def test_07_relative_paths_enforced_and_accepted(self) -> None:
        for p in ("/", "/login", "/a/b.html", "/search?q=x&page=2", "/page#section"):
            with self.subTest(path=p):
                plan = parse_browser_plan(json.dumps({"path": p, "steps": [VISIBLE]}))
                self.assertEqual(plan.path, p)

    def test_08_selector_validation(self) -> None:
        good = ["#login-form", "form > input[name=email]", ".card .field-error", "button[type='submit']",
                "ul#widgets li:nth-child(2)"]
        for sel in good:
            with self.subTest(selector=sel):
                parse_browser_plan(_plan([{"action": "expect_visible", "selector": sel}]))
        bad = ["", "   ", "text=Sign in", "xpath=//button", "//button", "..", "(//a)[1]", "#a >> text=x",
               "internal:role=button", "<script>", "javascript:alert(1)", "a" * (MAX_SELECTOR_LENGTH + 1),
               "#a\nb", 42, None]
        for sel in bad:
            with self.subTest(selector=sel):
                with self.assertRaises(BrowserPlanValidationError):
                    parse_browser_plan(_plan([{"action": "expect_visible", "selector": sel}]))

    def test_09_viewport_bounds_and_types(self) -> None:
        parse_browser_plan(_plan([VISIBLE], viewport={"width": 375, "height": 667, "device_scale_factor": 2}))
        for vp in ({"width": 100, "height": 720}, {"width": 5000, "height": 720}, {"width": 1280, "height": 100},
                   {"width": 1280, "height": 5000}, {"width": True, "height": 720}, {"width": "1280", "height": 720},
                   {"width": 1280, "height": 720, "device_scale_factor": 0.5},
                   {"width": 1280, "height": 720, "device_scale_factor": 4},
                   {"width": 1280, "height": 720, "zoom": 2}, [1280, 720]):
            with self.subTest(viewport=vp):
                with self.assertRaises(BrowserPlanValidationError):
                    parse_browser_plan(_plan([VISIBLE], viewport=vp))

    def test_10_timeout_and_budget_bounds(self) -> None:
        for t in (0, 50, 15001, -1, "1000", True, 2.5):
            with self.subTest(timeout=t):
                with self.assertRaises(BrowserPlanValidationError):
                    parse_browser_plan(_plan([dict(VISIBLE, timeout_ms=t)]))
        many = [dict(VISIBLE, timeout_ms=15000)] * 8  # 15s navigation + 120s of steps
        with self.assertRaisesRegex(BrowserPlanValidationError, "exceeds the per-criterion limit"):
            parse_browser_plan(_plan(many))
        ok = parse_browser_plan(_plan([dict(VISIBLE, timeout_ms=15000)] * 7))
        self.assertLessEqual(ok.budget_ms, MAX_PLAN_BUDGET_MS)

    def test_11_step_count_bound(self) -> None:
        with self.assertRaisesRegex(BrowserPlanValidationError, "exceeding limit"):
            parse_browser_plan(_plan([dict(VISIBLE, timeout_ms=100)] * (MAX_TOTAL_STEPS + 1)))

    def test_12_assertion_versus_action_distinction(self) -> None:
        plan = parse_browser_plan(_plan([
            {"action": "goto_relative", "path": "/login"},
            {"action": "fill", "selector": "#email", "value": "a@b.test"},
            {"action": "click", "selector": "#submit"},
            {"action": "select_option", "selector": "#plan", "value": "basic"},
            {"action": "check_checkbox", "selector": "#terms"},
            {"action": "expect_url_path", "path": "/dashboard"},
            {"action": "expect_count", "selector": ".widget", "count": 3},
            {"action": "expect_hidden", "selector": "#spinner"},
            {"action": "expect_no_horizontal_overflow"},
        ]))
        self.assertEqual([s.is_assertion for s in plan.steps], [False] * 5 + [True] * 4)
        self.assertEqual(ASSERTION_ACTIONS, {"expect_visible", "expect_hidden", "expect_text", "expect_count",
                                             "expect_url_path", "expect_no_horizontal_overflow"})

    def test_13_zero_assertions_rejected_as_false_proof(self) -> None:
        for steps in ([], [{"action": "goto_relative", "path": "/dashboard"}],
                      [{"action": "click", "selector": "#submit"}, {"action": "fill", "selector": "#e", "value": "x"}]):
            with self.subTest(steps=steps):
                with self.assertRaisesRegex(BrowserPlanValidationError, "False browser proof rejected"):
                    parse_browser_plan(_plan(steps))

    def test_14_assertion_parameter_validation(self) -> None:
        bad_steps = [
            {"action": "expect_text", "selector": "#a"},
            {"action": "expect_text", "selector": "#a", "contains": "x", "equals": "x"},
            {"action": "expect_text", "selector": "#a", "contains": ""},
            {"action": "expect_text", "selector": "#a", "contains": 5},
            {"action": "expect_text", "selector": "#a", "contains": "x" * 1001},
            {"action": "expect_count", "selector": "#a"},
            {"action": "expect_count", "selector": "#a", "count": -1},
            {"action": "expect_count", "selector": "#a", "count": "3"},
            {"action": "expect_count", "selector": "#a", "count": True},
            {"action": "expect_url_path"},
            {"action": "fill", "selector": "#a"},
            {"action": "fill", "selector": "#a", "value": 12345},
            {"action": "fill", "selector": "#a", "value": "x" * 1001},
            {"action": "select_option", "selector": "#a", "value": ["a"]},
            {"action": "expect_no_horizontal_overflow", "selector": "#a"},
        ]
        for step in bad_steps:
            with self.subTest(step=step):
                with self.assertRaises(BrowserPlanValidationError):
                    parse_browser_plan(_plan([step, VISIBLE]))

    def test_15_step_ids_validated_and_unique(self) -> None:
        plan = parse_browser_plan(_plan([dict(VISIBLE, id="form-visible")]))
        self.assertEqual(plan.steps[0].step_id, "form-visible")
        for steps in ([dict(VISIBLE, id="bad id")], [dict(VISIBLE, id="x"), dict(VISIBLE, id="x")]):
            with self.subTest(steps=steps):
                with self.assertRaises(BrowserPlanValidationError):
                    parse_browser_plan(_plan(steps))

    def test_16_parse_data_requires_object(self) -> None:
        with self.assertRaises(BrowserPlanValidationError):
            parse_browser_plan_data(["not", "an", "object"])


class TestSpecMarkdownBrowserPlans(unittest.TestCase):
    def test_01_markdown_plan_and_profile_extracted(self) -> None:
        doc = parse_spec_markdown(spec_document(
            browser_criterion("AC-01", _plan([VISIBLE]), profile="python-static"),
            "### AC-02 — Unit tests\n**Verification:** Test command\n**Command:** python -m unittest\n",
        ))
        ac1, ac2 = doc.criteria
        self.assertEqual(ac1.verification_type, "browser")
        self.assertIsNotNone(ac1.browser_plan)
        self.assertIsNone(ac1.browser_plan_error)
        self.assertEqual(ac1.app_profile, "python-static")
        self.assertEqual(ac1.browser_plan.steps[0].selector, "#login-form")
        self.assertEqual(ac2.verification_type, "test")
        self.assertIsNone(ac2.browser_plan)

    def test_02_invalid_plan_is_recorded_not_raised(self) -> None:
        doc = parse_spec_markdown(spec_document(
            browser_criterion("AC-01", _plan([{"action": "evaluate", "selector": "document.cookie"}, VISIBLE])),
            browser_criterion("AC-02", "{ not json"),
        ))
        self.assertIsNone(doc.criteria[0].browser_plan)
        self.assertIn("forbidden action", doc.criteria[0].browser_plan_error)
        self.assertIn("Invalid JSON", doc.criteria[1].browser_plan_error)

    def test_03_browser_criterion_without_plan_has_no_plan(self) -> None:
        doc = parse_spec_markdown(spec_document("### AC-01 — Looks right\n**Verification:** Browser\n"))
        self.assertEqual(doc.criteria[0].verification_type, "browser")
        self.assertIsNone(doc.criteria[0].browser_plan)
        self.assertIsNone(doc.criteria[0].browser_plan_error)


if __name__ == "__main__":
    unittest.main()
