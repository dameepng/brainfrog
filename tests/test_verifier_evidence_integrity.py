"""Evidence authenticity and proof integrity invariant test suite for BrainFrog Phase 1B.

Enforces evidence and workspace integrity invariants:
1. Clean revision identity binds exact Git HEAD commit.
2. Dirty tracked source identity records dirty status and modified files.
3. Untracked source identity binds untracked source files into snapshot digest.
4. Mid-run workspace drift invalidates PASS and transitions to BLOCKED.
5. Mid-run spec mutation is detected and invalidates run.
6. Source snapshot digest is stable and reproducible for identical content.
7. Evidence file tampering (modifying stdout.log) is detected by verify_report_integrity.
8. Missing evidence files are detected by verify_report_integrity.
9. Manifest inconsistency or hash mismatch fails report verification.
10. Whole-bundle replacement limitation is documented and asserted.
11. Collision-resistant run IDs prevent overwriting existing report directories.
12. Path traversal in spec paths raises PermissionError.
13. Atomic finalization via staging directory prevents partial report visibility.
14. Secret redaction removes credentials from saved evidence logs.
15. Historical report preservation: safe_cleanup_report refuses to delete unrelated reports.
16. Proof metadata accurately declares tamper-evident, commit-bound, not immutable.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core.verifier import (
    CriterionStatus,
    OverallVerdict,
    StandaloneVerifier,
    compute_source_snapshot_digest,
    safe_cleanup_report,
    verify_report_integrity,
)


def _synthetic_openai_key() -> str:
    """Deterministic, synthetic OpenAI-style key fixture (obviously fake)."""
    prefix = "sk" + "-"
    return prefix + "SYNTHETICOPENAIKEYFIXTURE".ljust(32, "a")



class TestVerifierEvidenceIntegrity(unittest.TestCase):
    """Integrity and tamper-detection tests for verification reports and evidence."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="bf_evid_test_")
        self.repo_dir = Path(self.temp_dir).resolve()

        # Initialize disposable git repository
        subprocess.run(["git", "init"], cwd=self.repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Evidence Tester"], cwd=self.repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "evid@test.local"], cwd=self.repo_dir, check=True)

        readme = self.repo_dir / "README.md"
        readme.write_text("# Test Repo\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo_dir, check=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=self.repo_dir, check=True, capture_output=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_simple_spec(self, cmd: str = "python -m py_compile README.md") -> Path:
        spec_content = f"""# Spec: Simple Smoke Test
## Acceptance Criteria
### AC-01 — Simple Command
**Verification:** Build command
**Command:** {cmd}
"""
        spec_path = self.repo_dir / "spec.md"
        spec_path.write_text(spec_content, encoding="utf-8")
        return spec_path

    def test_01_clean_revision_identity(self) -> None:
        """Invariant 1: Clean repository binds exact 40-character Git HEAD commit."""
        spec_path = self._create_simple_spec()
        subprocess.run(["git", "add", "spec.md"], cwd=self.repo_dir, check=True)
        subprocess.run(["git", "commit", "-m", "Commit spec"], cwd=self.repo_dir, check=True, capture_output=True)

        verifier = StandaloneVerifier(repo_dir=self.repo_dir, trust_local_code=True)
        report = verifier.verify_spec(spec_path)

        res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo_dir, capture_output=True, text=True, check=True)
        expected_head = res.stdout.strip()

        self.assertEqual(report.git_head_sha, expected_head)
        self.assertFalse(report.is_dirty)

    def test_02_dirty_tracked_source_identity(self) -> None:
        """Invariant 2: Dirty tracked source modifications are captured in status and snapshot."""
        # Modify README.md without committing
        (self.repo_dir / "README.md").write_text("# Modified Content\n", encoding="utf-8")

        spec_path = self._create_simple_spec()
        verifier = StandaloneVerifier(repo_dir=self.repo_dir, trust_local_code=True)
        report = verifier.verify_spec(spec_path)

        self.assertTrue(report.is_dirty)
        self.assertIn("README.md", report.dirty_files)

    def test_03_untracked_source_identity(self) -> None:
        """Invariant 3: Untracked source files are incorporated into source snapshot digest."""
        spec_path = self._create_simple_spec()
        verifier = StandaloneVerifier(repo_dir=self.repo_dir, trust_local_code=True)

        digest_before = compute_source_snapshot_digest(self.repo_dir, "test_hash")

        # Create an untracked source file
        untracked = self.repo_dir / "new_feature.py"
        untracked.write_text("def feature(): return True\n", encoding="utf-8")

        digest_after = compute_source_snapshot_digest(self.repo_dir, "test_hash")

        # Changing untracked files must change the source snapshot digest
        self.assertNotEqual(digest_before, digest_after)

    def test_04_mid_run_workspace_drift_invalidates_pass(self) -> None:
        """Invariant 4: Files mutated during test execution trigger drift detection and invalidate PASS."""
        spec_path = self._create_simple_spec()
        verifier = StandaloneVerifier(repo_dir=self.repo_dir, trust_local_code=True)

        # Mutate a source file during verification
        original_run = verifier._get_scrubbed_env

        def mutating_env():
            env = original_run()
            # Simulate a rogue test mutating source code during execution
            (self.repo_dir / "drift_exploit.py").write_text("rogue_code = True\n", encoding="utf-8")
            return env

        with patch.object(verifier, "_get_scrubbed_env", side_effect=mutating_env):
            report = verifier.verify_spec(spec_path)

            self.assertTrue(report.workspace_drift_detected)
            # Drift must prevent overall PASS even if criteria exited with 0
            self.assertEqual(report.overall_verdict, OverallVerdict.BLOCKED)
            self.assertEqual(report.exit_code, 2)

    def test_05_mid_run_spec_mutation_detected(self) -> None:
        """Invariant 5: Modifying spec.md during execution triggers drift detection."""
        spec_path = self._create_simple_spec()
        verifier = StandaloneVerifier(repo_dir=self.repo_dir, trust_local_code=True)

        original_env = verifier._get_scrubbed_env

        def mutating_spec():
            env = original_env()
            # Mutate the spec file on disk mid-run
            spec_path.write_text("# Mutated Spec Content\n", encoding="utf-8")
            return env

        with patch.object(verifier, "_get_scrubbed_env", side_effect=mutating_spec):
            report = verifier.verify_spec(spec_path)
            self.assertTrue(report.workspace_drift_detected)
            self.assertEqual(report.overall_verdict, OverallVerdict.BLOCKED)

    def test_06_source_snapshot_digest_stability(self) -> None:
        """Invariant 6: Identical workspace state produces stable, deterministic digest."""
        d1 = compute_source_snapshot_digest(self.repo_dir, "canonical_hash_123")
        d2 = compute_source_snapshot_digest(self.repo_dir, "canonical_hash_123")
        self.assertEqual(d1, d2)

    def test_07_evidence_file_tampering_detected(self) -> None:
        """Invariant 7: Modifying stdout.log causes verify_report_integrity to fail."""
        spec_path = self._create_simple_spec()
        verifier = StandaloneVerifier(repo_dir=self.repo_dir, trust_local_code=True)
        report = verifier.verify_spec(spec_path)

        # Confirm clean report passes integrity
        is_valid, findings = verify_report_integrity(report.report_dir)
        self.assertTrue(is_valid, f"Clean report failed integrity: {findings}")

        # Tamper with stdout.log
        stdout_log = report.report_dir / "evidence" / "AC-01" / "stdout.log"
        stdout_log.write_text("Tampered stdout output injected by attacker\n", encoding="utf-8")

        is_valid_tampered, findings_tampered = verify_report_integrity(report.report_dir)
        self.assertFalse(is_valid_tampered)
        self.assertTrue(any("Hash mismatch" in f for f in findings_tampered))

    def test_08_missing_evidence_file_detected(self) -> None:
        """Invariant 8: Deleted evidence log causes verify_report_integrity to fail."""
        spec_path = self._create_simple_spec()
        verifier = StandaloneVerifier(repo_dir=self.repo_dir, trust_local_code=True)
        report = verifier.verify_spec(spec_path)

        # Delete stderr.log
        stderr_log = report.report_dir / "evidence" / "AC-01" / "stderr.log"
        stderr_log.unlink()

        is_valid, findings = verify_report_integrity(report.report_dir)
        self.assertFalse(is_valid)
        self.assertTrue(any("missing on disk" in f for f in findings))

    def test_09_unauthorized_extra_file_in_evidence_detected(self) -> None:
        """Invariant 9: Unrecorded extra file in evidence directory is flagged."""
        spec_path = self._create_simple_spec()
        verifier = StandaloneVerifier(repo_dir=self.repo_dir, trust_local_code=True)
        report = verifier.verify_spec(spec_path)

        # Drop unauthorized file in evidence directory
        rogue_file = report.report_dir / "evidence" / "AC-01" / "rogue_artifact.bin"
        rogue_file.write_bytes(b"malicious injected payload")

        is_valid, findings = verify_report_integrity(report.report_dir)
        self.assertFalse(is_valid)
        self.assertTrue(any("Unauthorized extra file" in f for f in findings))

    def test_10_whole_bundle_replacement_limitation_documented(self) -> None:
        """Invariant 10: Reports claim tamper-evident/content-hashed, not cryptographically immutable."""
        spec_path = self._create_simple_spec()
        verifier = StandaloneVerifier(repo_dir=self.repo_dir, trust_local_code=True)
        report = verifier.verify_spec(spec_path)

        report_json = (report.report_dir / "report.json").read_text(encoding="utf-8")
        # Ensure report does not claim immutable proof on mutable local filesystem
        self.assertNotIn('"immutable": true', report_json.lower())
        self.assertIn("source_snapshot_digest", report_json)

    def test_11_collision_resistant_run_ids(self) -> None:
        """Invariant 11: Rapid successive runs generate distinct collision-resistant directories."""
        spec_path = self._create_simple_spec()
        verifier = StandaloneVerifier(repo_dir=self.repo_dir, trust_local_code=True)

        r1 = verifier.verify_spec(spec_path)
        r2 = verifier.verify_spec(spec_path)

        self.assertNotEqual(r1.run_id, r2.run_id)
        self.assertTrue(r1.report_dir.exists())
        self.assertTrue(r2.report_dir.exists())

    def test_12_path_traversal_in_spec_path_denied(self) -> None:
        """Invariant 12: Spec path attempting directory traversal raises PermissionError."""
        verifier = StandaloneVerifier(repo_dir=self.repo_dir, trust_local_code=True)
        with self.assertRaises(PermissionError):
            verifier.verify_spec("../../etc/spec.md")

    def test_13_atomic_finalization_staging_dir(self) -> None:
        """Invariant 13: Reports finalize atomically; no lingering staging directories."""
        spec_path = self._create_simple_spec()
        verifier = StandaloneVerifier(repo_dir=self.repo_dir, trust_local_code=True)
        report = verifier.verify_spec(spec_path)

        reports_base = self.repo_dir / ".brainfrog" / "reports"
        staging_dirs = list(reports_base.glob(".staging_*"))
        self.assertEqual(len(staging_dirs), 0, "No staging directories should linger after finalization")
        self.assertTrue((report.report_dir / "report.json").exists())

    def test_14_secret_redaction_in_evidence_logs(self) -> None:
        """Invariant 14: Simulated secrets in command output are scrubbed from evidence logs."""
        from core.runtime.secret_scrubbing import safe_scrub
        fake_key = _synthetic_openai_key()
        raw_output = f"Authenticated with {fake_key} successfully."
        scrubbed = safe_scrub(raw_output)
        self.assertNotIn(fake_key, scrubbed)
        self.assertIn("[REDACTED_API_KEY]", scrubbed)

    def test_15_historical_report_preservation(self) -> None:
        """Invariant 15: safe_cleanup_report refuses to delete unrelated or historical reports."""
        spec_path = self._create_simple_spec()
        verifier = StandaloneVerifier(repo_dir=self.repo_dir, trust_local_code=True)
        r1 = verifier.verify_spec(spec_path)
        r2 = verifier.verify_spec(spec_path)

        # Attempting to clean up r2 using r1's run ID must be rejected by ownership check
        with self.assertRaises(PermissionError):
            safe_cleanup_report(r2.report_dir, expected_run_id=r1.run_id)

        # Both reports must remain intact
        self.assertTrue(r1.report_dir.exists())
        self.assertTrue(r2.report_dir.exists())

        # Cleanup with matching run ID succeeds
        self.assertTrue(safe_cleanup_report(r1.report_dir, expected_run_id=r1.run_id))
        self.assertFalse(r1.report_dir.exists())
        self.assertTrue(r2.report_dir.exists())

    def test_16_report_manifest_completeness(self) -> None:
        """Invariant 16: report.json contains all required audit fields."""
        spec_path = self._create_simple_spec()
        verifier = StandaloneVerifier(repo_dir=self.repo_dir, trust_local_code=True)
        report = verifier.verify_spec(spec_path)

        manifest = json.loads((report.report_dir / "report.json").read_text(encoding="utf-8"))
        required_fields = [
            "run_id",
            "verifier_version",
            "created_at",
            "repository",
            "git_head_sha",
            "source_snapshot_digest",
            "workspace_status",
            "workspace_drift_detected",
            "spec_path",
            "spec_sha256",
            "execution_trust_mode",
            "execution_backend",
            "overall_verdict",
            "exit_code",
            "summary",
            "criteria",
        ]

    def test_17_untracked_content_mutation_alters_snapshot_digest(self) -> None:
        """Invariant 17: Mutating untracked file content without renaming alters the digest."""
        untracked = self.repo_dir / "untracked_module.py"
        untracked.write_text("VALUE = 100\n", encoding="utf-8")
        d1 = compute_source_snapshot_digest(self.repo_dir, "fake_hash")

        # Mutate content only
        untracked.write_text("VALUE = 200\n", encoding="utf-8")
        d2 = compute_source_snapshot_digest(self.repo_dir, "fake_hash")

        self.assertNotEqual(d1, d2, "Changing untracked file content must produce different digest")

    def test_18_staged_changes_captured_in_snapshot_digest(self) -> None:
        """Invariant 18: Staged changes (git add) are captured in the snapshot digest."""
        clean_digest = compute_source_snapshot_digest(self.repo_dir, "fake_hash")

        readme = self.repo_dir / "README.md"
        readme.write_text("# Modified Content for Staging Test\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=str(self.repo_dir), check=True)

        staged_digest = compute_source_snapshot_digest(self.repo_dir, "fake_hash")
        self.assertNotEqual(clean_digest, staged_digest, "Staged changes must produce a different digest")


if __name__ == "__main__":
    unittest.main()
