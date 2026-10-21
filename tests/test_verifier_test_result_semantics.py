# BrainFrog Verification-First Phase 1C: Test Result Semantics & Zero-Test False Pass Prevention
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from core.runtime.sandbox import TestResultAdapter, TestOutcome
from core.verifier import StandaloneVerifier, CriterionStatus, OverallVerdict


class TestVerifierTestResultSemantics(unittest.TestCase):
    """Rigorous tests ensuring zero-test suites never pass and test counts are parsed reliably."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="bf_test_semantics_")
        self.repo_dir = Path(self.tmp_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_01_unittest_zero_tests_produces_blocked_never_pass(self) -> None:
        """CRITICAL: unittest returning exit code 0 with 'Ran 0 tests' must produce BLOCKED, never PASS."""
        stdout = ""
        stderr = "Ran 0 tests in 0.000s\n\nOK\n"
        cmd = ["python", "-m", "unittest", "discover", "-s", "tests"]

        outcome = TestResultAdapter.parse_outcome(cmd, exit_code=0, stdout=stdout, stderr=stderr)

        self.assertEqual(outcome.verdict, "BLOCKED")
        self.assertEqual(outcome.test_count, 0)
        self.assertIn("Zero tests discovered or executed", outcome.reason)

    def test_02_unittest_text_success_alone_never_produces_pass(self) -> None:
        """Phase 1C: 'Ran 5 tests ... OK' text without a nonce-bound harness record is not PASS.

        Free-form text is repository-controlled and can be forged; PASS now requires a
        structured record (see test_verifier_phase1c_blockers for the PASS path).
        """
        stdout = ""
        stderr = ".....\n----------------------------------------------------------------------\nRan 5 tests in 0.012s\n\nOK\n"
        cmd = ["python", "-m", "unittest", "discover"]

        outcome = TestResultAdapter.parse_outcome(cmd, exit_code=0, stdout=stdout, stderr=stderr)

        self.assertNotEqual(outcome.verdict, "PASS")
        self.assertEqual(outcome.verdict, "BLOCKED")
        self.assertFalse(outcome.authoritative)
        self.assertIn("free-form", outcome.reason.lower())

    def test_03_unittest_failing_assertions_produces_fail_with_counts(self) -> None:
        """unittest returning exit code 1 with failure count produces FAIL."""
        stdout = ""
        stderr = "..F..\n======================================================================\nFAIL: test_foo\n----------------------------------------------------------------------\nRan 5 tests in 0.010s\n\nFAILED (failures=1)\n"
        cmd = ["python", "-m", "unittest", "test_foo.py"]

        outcome = TestResultAdapter.parse_outcome(cmd, exit_code=1, stdout=stdout, stderr=stderr)

        self.assertEqual(outcome.verdict, "FAIL")
        self.assertEqual(outcome.test_count, 5)
        self.assertEqual(outcome.failure_count, 1)

    def test_04_unittest_errors_produces_fail_with_counts(self) -> None:
        """unittest returning exit code 1 with error count produces FAIL."""
        stdout = ""
        stderr = ".E.\n======================================================================\nERROR: test_err\n----------------------------------------------------------------------\nRan 3 tests in 0.005s\n\nFAILED (errors=1)\n"
        cmd = ["python", "-m", "unittest", "test_err.py"]

        outcome = TestResultAdapter.parse_outcome(cmd, exit_code=1, stdout=stdout, stderr=stderr)

        self.assertEqual(outcome.verdict, "FAIL")
        self.assertEqual(outcome.test_count, 3)
        self.assertEqual(outcome.failure_count, 1)

    def test_05_compileall_text_success_alone_never_produces_pass(self) -> None:
        """Phase 1C: compileall text without a harness record (file count unknown) is not PASS."""
        stdout = "Listing '.'...\nCompiling './foo.py'...\n"
        stderr = ""
        cmd = ["python", "-m", "compileall", "."]

        outcome = TestResultAdapter.parse_outcome(cmd, exit_code=0, stdout=stdout, stderr=stderr)

        self.assertEqual(outcome.verdict, "BLOCKED")
        self.assertFalse(outcome.authoritative)

    def test_06_compileall_syntax_error_produces_fail(self) -> None:
        """compileall returning exit code 1 with syntax error produces FAIL."""
        stdout = "Listing '.'...\nCompiling './bad.py'...\n*** SyntaxError: invalid syntax"
        stderr = "Sorry: IndentationError: unexpected indent (bad.py, line 2)"
        cmd = ["python", "-m", "compileall", "-q", "."]

        outcome = TestResultAdapter.parse_outcome(cmd, exit_code=1, stdout=stdout, stderr=stderr)

        self.assertEqual(outcome.verdict, "FAIL")
        self.assertIn("status 1", outcome.reason.lower())

    def test_07_unrecognized_runner_does_not_falsely_claim_test_coverage(self) -> None:
        """Unsupported runner exiting 0 claims neither test coverage nor PASS (requirement H)."""
        stdout = "Build succeeded."
        stderr = ""
        cmd = ["python", "-m", "custom_tool"]

        outcome = TestResultAdapter.parse_outcome(cmd, exit_code=0, stdout=stdout, stderr=stderr)

        self.assertEqual(outcome.verdict, "BLOCKED")
        self.assertIsNone(outcome.test_count)
        self.assertEqual(outcome.infrastructure_error_category, "unsupported_runner")

    def test_08_end_to_end_zero_test_spec_produces_blocked_verdict(self) -> None:
        """End-to-end StandaloneVerifier execution of a zero-test directory produces BLOCKED, never PASS."""
        # Create an empty tests directory with no test methods
        tests_subdir = self.repo_dir / "tests"
        tests_subdir.mkdir(parents=True, exist_ok=True)
        (tests_subdir / "__init__.py").write_text("", encoding="utf-8")

        spec_text = """# Spec: Zero Tests Protection
## Acceptance Criteria
### AC-01 - Empty test discovery
**Verification:** Test command
**Command:** python -m unittest discover -s tests
"""
        spec_file = self.repo_dir / "spec.md"
        spec_file.write_text(spec_text, encoding="utf-8")

        # Run with trusted local code to observe real process output
        verifier = StandaloneVerifier(repo_dir=self.repo_dir, trust_local_code=True)
        report = verifier.verify_spec(spec_file)

        crit = report.criteria[0]
        self.assertEqual(crit.status, CriterionStatus.BLOCKED, "Zero-test suite must produce BLOCKED, never PASS")
        self.assertEqual(crit.test_count, 0)
        self.assertIn("Zero tests discovered or executed", crit.failure_reason)
        self.assertEqual(report.overall_verdict, OverallVerdict.BLOCKED)


if __name__ == "__main__":
    unittest.main()
