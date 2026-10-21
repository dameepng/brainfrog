"""Phase 1D: browser evidence assembly, screenshot manifests, and offline integrity checks.

The browser container is replaced by StubBrowserBackend here so that report assembly and
--check-report can be tested deterministically; real browser runs are covered by
tests/test_verifier_browser_integration.py.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from core.runtime.browser_plan import parse_browser_plan
from core.runtime.browser_sandbox import BrowserExecutionResult
from core.verifier import (
    CriterionStatus,
    ExecutionTrustMode,
    OverallVerdict,
    StandaloneVerifier,
    verify_report_integrity,
)
from tests.browser_verification_helpers import (
    FAKE_PNG,
    SIMPLE_PLAN,
    StubBrowserBackend,
    browser_criterion,
    spec_document,
)

SCREENSHOT_METADATA_KEYS = {
    "criterion_id", "assertion_id", "source_snapshot_digest", "browser_image_identity", "browser_version",
    "viewport", "device_scale_factor", "application_origin_reference", "captured_at",
    "screenshot_relative_path", "screenshot_sha256",
}


class _ReportCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bf_bevid_"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        (self.repo / "index.html").write_text("<form id='login-form'></form>", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_spec(self, criteria, outcomes, mode=ExecutionTrustMode.ISOLATED_SANDBOX, extra_files=None):
        (self.repo / "spec.md").write_text(spec_document(*criteria), encoding="utf-8")
        for name, body in (extra_files or {}).items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        self.backend = StubBrowserBackend(outcomes)
        verifier = StandaloneVerifier(repo_dir=self.repo, execution_trust_mode=mode, browser_backend=self.backend,
                                      reports_base_dir=self.tmp / "reports")
        report = verifier.verify_spec(self.repo / "spec.md")
        return report, self.tmp / "reports" / report.run_id

    @staticmethod
    def rewrite_json(path: Path, mutate) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        mutate(data)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @staticmethod
    def rehash(report_dir: Path, cid: str, name: str) -> None:
        """Attacker step: recompute report.json's hash for an edited evidence file."""
        rj = report_dir / "report.json"
        data = json.loads(rj.read_text(encoding="utf-8"))
        crit = next(c for c in data["criteria"] if c["criterion_id"] == cid)
        crit["evidence_hashes"][name] = hashlib.sha256((report_dir / crit["evidence_files"][name]).read_bytes()).hexdigest()
        rj.write_text(json.dumps(data, indent=2), encoding="utf-8")


class TestScreenshotEvidence(_ReportCase):
    def test_01_screenshot_manifest_and_evidence_files_recorded(self) -> None:
        report, rdir = self.run_spec([browser_criterion("AC-01", SIMPLE_PLAN)], {"AC-01": {}})
        crit = report.criteria[0]
        self.assertEqual(crit.status, CriterionStatus.PASS)
        for name in ("plan.json", "browser-assertions.json", "browser-run.json", "screenshot.png",
                     "browser-console.log", "browser-errors.log", "browser-network.log"):
            self.assertIn(name, crit.evidence_files)
            self.assertEqual(crit.evidence_hashes[name],
                             hashlib.sha256((rdir / crit.evidence_files[name]).read_bytes()).hexdigest())
        shot = crit.browser_evidence["screenshot"]
        self.assertTrue(SCREENSHOT_METADATA_KEYS <= set(shot), SCREENSHOT_METADATA_KEYS - set(shot))
        self.assertEqual(shot["screenshot_sha256"], hashlib.sha256(FAKE_PNG).hexdigest())
        self.assertEqual(shot["source_snapshot_digest"], report.materialized_source_snapshot_digest)
        self.assertEqual(shot["screenshot_relative_path"], "evidence/AC-01/screenshot.png")
        self.assertEqual(crit.assertion_count, 1)
        self.assertEqual(crit.browser_plan_sha256, parse_browser_plan(SIMPLE_PLAN).plan_sha256())
        self.assertEqual(verify_report_integrity(rdir), (True, []))
        saved = json.loads((rdir / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["report_schema"], "brainfrog.verification-report/1d")
        self.assertEqual(saved["criteria"][0]["browser_evidence"]["screenshot"]["screenshot_sha256"],
                         shot["screenshot_sha256"])

    def test_02_tampered_screenshot_detected(self) -> None:
        _, rdir = self.run_spec([browser_criterion("AC-01", SIMPLE_PLAN)], {"AC-01": {}})
        shot = rdir / "evidence" / "AC-01" / "screenshot.png"
        shot.write_bytes(shot.read_bytes() + b"tamper")
        ok, findings = verify_report_integrity(rdir)
        self.assertFalse(ok)
        self.assertTrue(any("screenshot.png" in f and "Hash mismatch" in f for f in findings), findings)

    def test_03_rehashed_screenshot_still_detected_by_assertion_binding(self) -> None:
        _, rdir = self.run_spec([browser_criterion("AC-01", SIMPLE_PLAN)], {"AC-01": {}})
        (rdir / "evidence" / "AC-01" / "screenshot.png").write_bytes(b"\x89PNG\r\n\x1a\nreplaced")
        self.rehash(rdir, "AC-01", "screenshot.png")
        ok, findings = verify_report_integrity(rdir)
        self.assertFalse(ok)
        self.assertTrue(any("screenshot hash differs" in f for f in findings), findings)

    def test_04_missing_assertion_file_detected(self) -> None:
        _, rdir = self.run_spec([browser_criterion("AC-01", SIMPLE_PLAN)], {"AC-01": {}})
        (rdir / "evidence" / "AC-01" / "browser-assertions.json").unlink()
        ok, findings = verify_report_integrity(rdir)
        self.assertFalse(ok)
        self.assertTrue(any("browser-assertions.json" in f and "missing" in f for f in findings), findings)
        # Removing the record of the file as well is caught by the required-evidence rule.
        self.rewrite_json(rdir / "report.json", lambda d: (d["criteria"][0]["evidence_files"].pop("browser-assertions.json"),
                                                           d["criteria"][0]["evidence_hashes"].pop("browser-assertions.json")))
        ok, findings = verify_report_integrity(rdir)
        self.assertFalse(ok)
        self.assertTrue(any("without required evidence file 'browser-assertions.json'" in f for f in findings), findings)

    def test_05_mismatched_criterion_reference_detected(self) -> None:
        _, rdir = self.run_spec([browser_criterion("AC-01", SIMPLE_PLAN), browser_criterion("AC-02", SIMPLE_PLAN)],
                                {"AC-01": {}, "AC-02": {}})
        # Swap AC-02's assertion evidence into AC-01 and fix up the outer hash.
        ac1 = rdir / "evidence" / "AC-01" / "browser-assertions.json"
        ac1.write_bytes((rdir / "evidence" / "AC-02" / "browser-assertions.json").read_bytes())
        self.rehash(rdir, "AC-01", "browser-assertions.json")
        ok, findings = verify_report_integrity(rdir)
        self.assertFalse(ok)
        self.assertTrue(any("references criterion 'AC-02'" in f for f in findings), findings)

    def test_06_status_upgrade_in_report_detected(self) -> None:
        _, rdir = self.run_spec([browser_criterion("AC-01", SIMPLE_PLAN)], {"AC-01": {"status": "FAIL"}})
        self.rewrite_json(rdir / "report.json", lambda d: d["criteria"][0].update(status="PASS"))
        ok, findings = verify_report_integrity(rdir)
        self.assertFalse(ok)
        self.assertTrue(any("!= assertion evidence status FAIL" in f for f in findings), findings)

    def test_07_pass_without_screenshot_is_not_pass(self) -> None:
        report, rdir = self.run_spec([browser_criterion("AC-01", SIMPLE_PLAN)], {"AC-01": {"screenshot": False}})
        crit = report.criteria[0]
        self.assertEqual(crit.status, CriterionStatus.BLOCKED)
        self.assertNotIn("screenshot.png", crit.evidence_files)
        self.assertTrue(verify_report_integrity(rdir)[0])

    def test_08_fail_without_screenshot_stays_fail_with_reason(self) -> None:
        report, rdir = self.run_spec([browser_criterion("AC-01", SIMPLE_PLAN)],
                                     {"AC-01": {"status": "FAIL", "screenshot": False}})
        crit = report.criteria[0]
        self.assertEqual(crit.status, CriterionStatus.FAIL)
        self.assertIsNone(crit.browser_evidence["screenshot"]["screenshot_sha256"])
        self.assertTrue(crit.browser_evidence["screenshot"]["omitted_reason"])
        self.assertEqual(verify_report_integrity(rdir), (True, []))


class TestEvidenceSubstitutionAndMetadata(_ReportCase):
    def test_01_screenshot_swapped_from_another_criterion_detected(self) -> None:
        _, rdir = self.run_spec([browser_criterion("AC-01", SIMPLE_PLAN), browser_criterion("AC-02", SIMPLE_PLAN)],
                                {"AC-01": {}, "AC-02": {"status": "FAIL"}})
        donor = rdir / "evidence" / "AC-02" / "screenshot.png"
        donor.write_bytes(donor.read_bytes() + b"distinct")  # make AC-02's image differ, then re-hash it
        self.rehash(rdir, "AC-02", "screenshot.png")
        (rdir / "evidence" / "AC-01" / "screenshot.png").write_bytes(donor.read_bytes())
        self.rehash(rdir, "AC-01", "screenshot.png")
        ok, findings = verify_report_integrity(rdir)
        self.assertFalse(ok)
        self.assertTrue(any("AC-01" in f and "screenshot hash differs" in f for f in findings), findings)

    def test_02_malformed_report_metadata_rejected_without_crashing(self) -> None:
        _, rdir = self.run_spec([browser_criterion("AC-01", SIMPLE_PLAN)], {"AC-01": {}})
        original = (rdir / "report.json").read_text(encoding="utf-8")
        bad_documents = {
            "not-an-object": "[]",
            "criteria-string": json.dumps({"criteria": "AC-01"}),
            "criteria-ints": json.dumps({"criteria": [1, 2]}),
            "evidence-list": json.dumps({"criteria": [{"criterion_id": "AC-01", "evidence_files": ["x"]}]}),
            "identity-string": json.dumps({"criteria": [], "snapshot_identity": "x"}),
            "run-files-list": json.dumps({"criteria": [], "run_evidence_files": ["x"]}),
            "md-hash-number": json.dumps({"criteria": [], "report_md_sha256": 5}),
            "not-json": "{ broken",
        }
        for name, doc in bad_documents.items():
            with self.subTest(case=name):
                (rdir / "report.json").write_text(doc, encoding="utf-8")
                ok, findings = verify_report_integrity(rdir)
                self.assertFalse(ok)
                self.assertTrue(findings)
        (rdir / "report.json").write_text(original, encoding="utf-8")
        self.assertEqual(verify_report_integrity(rdir), (True, []))

    def test_03_evidence_paths_cannot_leave_the_bundle(self) -> None:
        _, rdir = self.run_spec([browser_criterion("AC-01", SIMPLE_PLAN)], {"AC-01": {}})
        outside = self.tmp / "outside.txt"
        outside.write_text("host file", encoding="utf-8")
        digest = hashlib.sha256(outside.read_bytes()).hexdigest()
        for rel in ("../../outside.txt", str(outside), str(outside).replace("\\", "/")):
            with self.subTest(path=rel):
                def mutate(d, rel=rel):
                    d["criteria"][0]["evidence_files"]["extra.txt"] = rel
                    d["criteria"][0]["evidence_hashes"]["extra.txt"] = digest
                self.rewrite_json(rdir / "report.json", mutate)
                ok, findings = verify_report_integrity(rdir)
                self.assertFalse(ok)
                self.assertTrue(any("outside the report bundle" in f for f in findings), findings)
                self.rewrite_json(rdir / "report.json", lambda d: (d["criteria"][0]["evidence_files"].pop("extra.txt"),
                                                                   d["criteria"][0]["evidence_hashes"].pop("extra.txt")))

    def test_04_fully_reforged_consistent_bundle_is_not_detectable(self) -> None:
        """Documents the trust boundary: unsigned bundles are tamper-evident, not authentic.

        An attacker who rewrites the evidence AND every dependent hash and cross-reference
        consistently produces a bundle that --check-report accepts. This test must keep passing
        until reports are signed; it exists so the limitation cannot be silently forgotten.
        """
        _, rdir = self.run_spec([browser_criterion("AC-01", SIMPLE_PLAN)], {"AC-01": {"status": "FAIL"}})
        forged_png = b"\x89PNG\r\n\x1a\nforged"
        shot = rdir / "evidence" / "AC-01" / "screenshot.png"
        shot.write_bytes(forged_png)
        assertions = rdir / "evidence" / "AC-01" / "browser-assertions.json"

        def forge_manifest(m):
            m["status"] = "PASS"
            m["failure_reason"] = m["failure_kind"] = None
            for r in m["records"]:
                r["status"] = "PASS"
                r.pop("failure_kind", None)
                r.pop("failure_reason", None)
            m["screenshot"]["sha256"] = hashlib.sha256(forged_png).hexdigest()
        self.rewrite_json(assertions, forge_manifest)
        plan_steps = len(json.loads((rdir / "evidence" / "AC-01" / "plan.json").read_text(encoding="utf-8"))["steps"])
        self.assertEqual(len(json.loads(assertions.read_text(encoding="utf-8"))["records"]), plan_steps)

        def forge_report(d):
            c = d["criteria"][0]
            c["status"] = "PASS"
            c["failure_reason"] = None
            c["assertion_failure_count"] = 0
            for name in ("screenshot.png", "browser-assertions.json"):
                c["evidence_hashes"][name] = hashlib.sha256((rdir / c["evidence_files"][name]).read_bytes()).hexdigest()
            d["overall_verdict"] = "PASS"
            d["exit_code"] = 0
            d.pop("report_md_sha256", None)
        self.rewrite_json(rdir / "report.json", forge_report)
        self.assertEqual(verify_report_integrity(rdir), (True, []))


class TestBrowserVerdictSemantics(_ReportCase):
    def test_01_interrupted_browser_run_is_blocked(self) -> None:
        interrupted = BrowserExecutionResult(status="BLOCKED", duration_ms=5.0, failure_kind="browser_crashed",
                                             failure_reason="Browser runner produced 0 completion records",
                                             infrastructure_error_category="incomplete_runner_result")
        report, rdir = self.run_spec([browser_criterion("AC-01", SIMPLE_PLAN)], {"AC-01": {"result": interrupted}})
        crit = report.criteria[0]
        self.assertEqual(crit.status, CriterionStatus.BLOCKED)
        self.assertEqual(crit.infrastructure_error_category, "incomplete_runner_result")
        self.assertFalse(crit.result_authoritative)
        self.assertEqual(report.overall_verdict, OverallVerdict.BLOCKED)
        self.assertTrue(verify_report_integrity(rdir)[0])

    def test_02_unknown_runner_status_is_blocked(self) -> None:
        odd = BrowserExecutionResult(status="PASSED-ISH", duration_ms=1.0)
        report, _ = self.run_spec([browser_criterion("AC-01", SIMPLE_PLAN)], {"AC-01": {"result": odd}})
        self.assertEqual(report.criteria[0].status, CriterionStatus.BLOCKED)

    def test_03_source_snapshot_binding(self) -> None:
        report, rdir = self.run_spec([browser_criterion("AC-01", SIMPLE_PLAN), browser_criterion("AC-02", SIMPLE_PLAN)],
                                     {"AC-01": {}, "AC-02": {}})
        calls = self.backend.calls
        # Every browser criterion in a run is executed against the same materialized snapshot.
        self.assertEqual({c["digest"] for c in calls}, {report.materialized_source_snapshot_digest})
        self.assertEqual(len({c["snapshot_dir"] for c in calls}), 1)
        for crit in report.criteria:
            self.assertEqual(crit.observed_source_snapshot_digest, report.materialized_source_snapshot_digest)
        # Evidence claiming a different snapshot is rejected offline.
        path = rdir / "evidence" / "AC-01" / "browser-assertions.json"
        self.rewrite_json(path, lambda d: d.update(source_snapshot_digest="0" * 64))
        self.rehash(rdir, "AC-01", "browser-assertions.json")
        ok, findings = verify_report_integrity(rdir)
        self.assertFalse(ok)
        self.assertTrue(any("different source snapshot" in f for f in findings), findings)

    def test_04_observed_digest_mismatch_downgrades_pass(self) -> None:
        report, _ = self.run_spec([browser_criterion("AC-01", SIMPLE_PLAN)], {"AC-01": {"observed_digest": "9" * 64}})
        self.assertEqual(report.criteria[0].status, CriterionStatus.BLOCKED)
        self.assertIn("observed snapshot digest", report.criteria[0].failure_reason)

    def test_05_mixed_unit_build_and_browser_criteria(self) -> None:
        unit = ("### AC-10 — Unit tests\n**Verification:** Test command\n"
                "**Command:** python -m unittest discover -s tests\n\n")
        build = "### AC-11 — Compiles\n**Verification:** Build check\n**Command:** python -m compileall -q .\n\n"
        files = {"tests/__init__.py": "", "tests/test_ok.py":
                 "import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n"}
        cases = [
            ({"AC-01": {}, "AC-02": {}}, OverallVerdict.PASS, 0),
            ({"AC-01": {}, "AC-02": {"status": "FAIL"}}, OverallVerdict.FAIL, 1),
            ({"AC-01": {}, "AC-02": {"screenshot": False}}, OverallVerdict.BLOCKED, 2),
        ]
        for outcomes, verdict, code in cases:
            with self.subTest(outcomes=outcomes):
                report, rdir = self.run_spec(
                    [browser_criterion("AC-01", SIMPLE_PLAN), browser_criterion("AC-02", SIMPLE_PLAN), unit, build],
                    outcomes, mode=ExecutionTrustMode.TRUSTED_OPERATOR, extra_files=files)
                by_id = {c.criterion_id: c for c in report.criteria}
                self.assertEqual(by_id["AC-10"].status, CriterionStatus.PASS, by_id["AC-10"].failure_reason)
                self.assertEqual(by_id["AC-10"].test_count, 1)
                self.assertEqual(by_id["AC-11"].status, CriterionStatus.PASS, by_id["AC-11"].failure_reason)
                self.assertEqual((report.overall_verdict, report.exit_code), (verdict, code))
                self.assertEqual(verify_report_integrity(rdir), (True, []))
                shutil.rmtree(self.tmp / "reports", ignore_errors=True)

    def test_06_reports_without_browser_criteria_still_verify(self) -> None:
        unit = ("### AC-10 — Unit tests\n**Verification:** Test command\n"
                "**Command:** python -m unittest discover -s tests\n\n")
        files = {"tests/__init__.py": "", "tests/test_ok.py":
                 "import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n"}
        report, rdir = self.run_spec([unit], {}, mode=ExecutionTrustMode.TRUSTED_OPERATOR, extra_files=files)
        self.assertEqual(report.overall_verdict, OverallVerdict.PASS)
        self.assertIsNone(report.browser_backend)
        # A Phase 1C-shaped report.json (no 1D fields at all) is still accepted.
        self.rewrite_json(rdir / "report.json", lambda d: [d.pop(k) for k in ("report_schema", "browser_backend")] and
                          [c.pop(k) for c in d["criteria"] for k in ("browser_plan_sha256", "app_profile_id",
                                                                     "assertion_count", "assertion_failure_count",
                                                                     "browser_evidence")])
        self.assertEqual(verify_report_integrity(rdir), (True, []))


if __name__ == "__main__":
    unittest.main()
