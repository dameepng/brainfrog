"""Focused Regression Suite for Proof Discovery UX & Latest Target (Sprint 1.5A).

Tests:
1. Proof listing with multiple artifacts and mixed task outcomes (VERIFIED, FAILED, CANCELLED)
2. Empty proof store handling (exit 0 with clean informational message)
3. Missing optional fields in proof JSON handled gracefully
4. Corrupt / malformed proof artifact surfaced honestly in list-proofs without crashing
5. 'latest' target resolves deterministically to the newest proof
6. Tied timestamps resolved deterministically by secondary tiebreakers
7. Newest proof malformed fails closed (resolves to malformed file; never silently skips to older valid proof)
8. Empty proof store returns documented INCOMPLETE/MISSING outcome (exit 2) for 'latest'
9. Explicit task ID and JSON path still work unchanged
10. 'show-proof latest' and 'verify-proof latest' retain their distinct exit-code semantics
11. Path traversal attempts rejected and confined to proofs directory
12. Secret redaction applied to displayed values in list-proofs
13. CLI aliases (--list-proofs, --proofs, proofs, list-proofs, proof latest) remain fully compatible
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

import cli
from cli import list_proofs_cli, main as cli_main, show_proof_cli, verify_proof_cli
from core.runtime.proof import (
    ChangeRecord,
    ChangesEvidence,
    FileProofStore,
    GitProvenance,
    ProcessEvidence,
    ProofArtifact,
    ProofVerdict,
    ReproducibilityEvidence,
    TaskEvidence,
    VerificationEvidence,
)
from core.runtime.proof_markdown import render_proof_markdown
from core.runtime.proof_inspection import (
    ProofInspectionState,
    inspect_proof,
)
from core.runtime.proof_verifier import (
    DeterministicProofVerifier,
    ProofVerificationStatus,
)


class TestProofDiscoveryUX(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="test_proof_discovery_")
        self.workspace = Path(self.tmp_dir).resolve()

        # Initialize minimal git repo
        subprocess.run(["git", "init"], cwd=self.workspace, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=self.workspace, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "tester@test.com"], cwd=self.workspace, capture_output=True, check=True)
        (self.workspace / "README.md").write_text("# Repo\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.workspace, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=self.workspace, capture_output=True, check=True)
        res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.workspace, capture_output=True, text=True, check=True)
        self.commit_sha = res.stdout.strip()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _create_and_save_proof(
        self,
        task_id: str,
        verdict_status: str = "VERIFIED",
        tx_status: str = "COMMITTED",
        exit_code: int = 0,
        test_command: str = "python -m pytest",
        created_at: str = "2026-10-12T10:00:00Z",
        completed_at: str = "2026-10-12T10:01:00Z",
        description: str = "Task description",
    ) -> ProofArtifact:
        proof_store = FileProofStore(self.workspace)
        file_p = self.workspace / f"{task_id}.py"
        raw_bytes = f"print('{task_id}')\n".encode("utf-8")
        file_p.write_bytes(raw_bytes)
        h = hashlib.sha256(raw_bytes).hexdigest()
        files = [
            ChangeRecord(
                path=f"{task_id}.py",
                operation="CREATE",
                before_sha256=None,
                after_sha256=h,
                byte_count_delta=len(raw_bytes),
            )
        ]

        proof = ProofArtifact(
            version="1.0.0",
            task=TaskEvidence(
                task_id=task_id,
                session_id=f"session_{task_id}",
                description=description,
                created_at=created_at,
                completed_at=completed_at,
                duration_ms=60000,
            ),
            verdict=ProofVerdict(
                status=verdict_status,
                reason="Authoritative execution outcome",
            ),
            provenance=GitProvenance(
                is_git=True,
                repo_root=self.workspace.name,
                branch="main",
                initial_head_sha=self.commit_sha,
                final_head_sha=self.commit_sha,
                commit_sha=self.commit_sha,
            ),
            verification=VerificationEvidence(
                test_command=test_command,
                exit_code=exit_code,
                stdout_summary="1 passed in 0.05s",
                stderr_summary="",
                gate_checks={
                    "workspace_scope": True,
                    "test_suite": (exit_code == 0),
                    "transaction_integrity": (tx_status == "COMMITTED"),
                    "execution_integrity": True,
                    "trust_integrity": True,
                },
                process_evidence=ProcessEvidence(
                    task_id=task_id,
                    transaction_id=f"tx_{task_id}",
                    command=test_command,
                    returncode=exit_code,
                    status="success" if exit_code == 0 else "failed",
                    duration_ms=1000,
                ),
            ),
            changes=ChangesEvidence(
                transaction_id=f"tx_{task_id}",
                status=tx_status,
                files=files,
            ),
            reproducibility=ReproducibilityEvidence(
                command=test_command,
                expected_exit_code=exit_code,
                expected_file_hashes={f.path: f.after_sha256 for f in files if f.after_sha256},
            ),
        )
        md = render_proof_markdown(proof)
        proof_store.save(proof, markdown_content=md)
        return proof

    # -------------------------------------------------------------------------
    # 1. list-proofs with multiple artifacts and mixed outcomes
    # -------------------------------------------------------------------------
    def test_list_proofs_multiple_artifacts_mixed_outcomes(self) -> None:
        self._create_and_save_proof("task_01", verdict_status="VERIFIED", tx_status="COMMITTED", exit_code=0, created_at="2026-10-12T10:00:00Z", completed_at="2026-10-12T10:01:00Z")
        self._create_and_save_proof("task_02", verdict_status="FAILED", tx_status="ROLLED_BACK", exit_code=1, created_at="2026-10-12T10:05:00Z", completed_at="2026-10-12T10:06:00Z")
        self._create_and_save_proof("task_03", verdict_status="CANCELLED", tx_status="ROLLED_BACK", exit_code=130, created_at="2026-10-12T10:10:00Z", completed_at="2026-10-12T10:11:00Z")

        with cli.console.capture() as capture:
            code = list_proofs_cli(self.workspace)

        self.assertEqual(code, 0)
        captured = capture.get()
        self.assertIn("task_01", captured)
        self.assertIn("task_02", captured)
        self.assertIn("task_03", captured)
        self.assertIn("VERIFIED", captured)
        self.assertIn("FAILED", captured)
        self.assertIn("CANCELLED", captured)
        self.assertIn("COMMITTED", captured)
        self.assertIn("ROLLED_BACK", captured)

        # Confirm summary list order: newest first
        store = FileProofStore(self.workspace)
        summaries = store.list_summaries()
        self.assertEqual(len(summaries), 3)
        self.assertEqual(summaries[0]["task_id"], "task_03")
        self.assertEqual(summaries[1]["task_id"], "task_02")
        self.assertEqual(summaries[2]["task_id"], "task_01")

    # -------------------------------------------------------------------------
    # 2. list-proofs on empty store
    # -------------------------------------------------------------------------
    def test_list_proofs_empty_store(self) -> None:
        with cli.console.capture() as capture:
            code = list_proofs_cli(self.workspace)

        self.assertEqual(code, 0)
        captured = capture.get()
        self.assertIn("No proof artifacts found", captured)

    # -------------------------------------------------------------------------
    # 3. list-proofs with missing optional fields
    # -------------------------------------------------------------------------
    def test_list_proofs_missing_optional_fields(self) -> None:
        store = FileProofStore(self.workspace)
        store.proofs_dir.mkdir(parents=True, exist_ok=True)
        minimal_proof = {
            "version": "1.0.0",
            "task": {"task_id": "task_sparse", "description": "Sparse proof"},
            "verdict": {"status": "VERIFIED"},
        }
        (store.proofs_dir / "task_sparse.json").write_text(json.dumps(minimal_proof), encoding="utf-8")

        with cli.console.capture() as capture:
            code = list_proofs_cli(self.workspace)

        self.assertEqual(code, 0)
        captured = capture.get()
        self.assertIn("task_sparse", captured)
        self.assertIn("VERIFIED", captured)

        summaries = store.list_summaries()
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["task_id"], "task_sparse")
        self.assertEqual(summaries[0]["transaction_status"], "—")
        self.assertEqual(summaries[0]["test_command"], "—")

    # -------------------------------------------------------------------------
    # 4. list-proofs handles corrupt artifact honestly
    # -------------------------------------------------------------------------
    def test_list_proofs_corrupt_artifact_surfaced_honestly(self) -> None:
        self._create_and_save_proof("task_valid")
        store = FileProofStore(self.workspace)
        (store.proofs_dir / "task_corrupt.json").write_text("{malformed: json", encoding="utf-8")

        with cli.console.capture() as capture:
            code = list_proofs_cli(self.workspace)

        self.assertEqual(code, 0)
        captured = capture.get()
        self.assertIn("task_valid", captured)
        self.assertIn("task_corrupt", captured)
        self.assertIn("INVALID", captured)
        self.assertIn("Corrupted Proof Warning", captured)

        summaries = store.list_summaries()
        self.assertEqual(len(summaries), 2)
        corrupt_item = next(s for s in summaries if s["task_id"] == "task_corrupt")
        self.assertTrue(corrupt_item["is_corrupt"])
        self.assertEqual(corrupt_item["verdict_status"], "INVALID")

    # -------------------------------------------------------------------------
    # 5. 'latest' target resolves deterministically to newest proof
    # -------------------------------------------------------------------------
    def test_latest_target_resolves_to_newest_proof(self) -> None:
        self._create_and_save_proof("task_alpha", completed_at="2026-10-12T10:00:00Z")
        self._create_and_save_proof("task_beta", completed_at="2026-10-12T11:00:00Z")

        verifier = DeterministicProofVerifier(self.workspace)
        report = verifier.verify("latest")
        self.assertEqual(report.task_id, "task_beta")
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.overall_status, ProofVerificationStatus.VALID)

        inspect_res = inspect_proof(self.workspace, "latest")
        self.assertIsNotNone(inspect_res.proof)
        self.assertEqual(inspect_res.proof.task.task_id, "task_beta")
        self.assertEqual(inspect_res.exit_code, 0)
        self.assertEqual(inspect_res.state, ProofInspectionState.VERIFIED)

    # -------------------------------------------------------------------------
    # 6. 'latest' target deterministic on tied timestamps
    # -------------------------------------------------------------------------
    def test_latest_target_deterministic_tied_timestamps(self) -> None:
        store = FileProofStore(self.workspace)
        store.proofs_dir.mkdir(parents=True, exist_ok=True)

        proof_1 = {
            "version": "1.0.0",
            "task": {"task_id": "task_aaa", "description": "Tied A", "completed_at": "2026-10-12T10:00:00Z"},
            "verdict": {"status": "VERIFIED"},
            "verification": {"test_command": "true", "exit_code": 0},
            "changes": {"status": "COMMITTED"},
            "reproducibility": {"command": "true"},
        }
        proof_2 = {
            "version": "1.0.0",
            "task": {"task_id": "task_zzz", "description": "Tied Z", "completed_at": "2026-10-12T10:00:00Z"},
            "verdict": {"status": "VERIFIED"},
            "verification": {"test_command": "true", "exit_code": 0},
            "changes": {"status": "COMMITTED"},
            "reproducibility": {"command": "true"},
        }

        path_1 = store.proofs_dir / "task_aaa.json"
        path_2 = store.proofs_dir / "task_zzz.json"
        path_1.write_text(json.dumps(proof_1), encoding="utf-8")
        path_2.write_text(json.dumps(proof_2), encoding="utf-8")

        # Set identical filesystem timestamps
        os.utime(path_1, (1728727200, 1728727200))
        os.utime(path_2, (1728727200, 1728727200))

        # Tiebreaker prefers larger stem lexicographically (task_zzz > task_aaa)
        latest_path = store.get_latest_proof_path()
        self.assertIsNotNone(latest_path)
        self.assertEqual(latest_path.stem, "task_zzz")

    # -------------------------------------------------------------------------
    # 7. Newest proof malformed fails closed (never silently skips to older valid proof)
    # -------------------------------------------------------------------------
    def test_latest_target_newest_proof_malformed_fails_closed(self) -> None:
        older_proof = self._create_and_save_proof("task_older_valid", completed_at="2026-10-12T07:00:00Z")
        store = FileProofStore(self.workspace)
        older_path = store.proof_json_path("task_older_valid")
        # Ensure older file mtime is aligned with its completed_at (07:00:00 UTC)
        os.utime(older_path, (1791788400, 1791788400))

        malformed_path = store.proofs_dir / "task_newer_corrupt.json"
        malformed_path.write_text("{malformed: json", encoding="utf-8")
        # Ensure malformed file mtime is strictly newer (08:00:00 UTC)
        os.utime(malformed_path, (1791792000, 1791792000))

        # Must resolve to the newer malformed file, NOT skip to task_older_valid
        verifier = DeterministicProofVerifier(self.workspace)
        report = verifier.verify("latest")

        self.assertEqual(report.task_id, "task_newer_corrupt")
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(report.overall_status, ProofVerificationStatus.INVALID)
        self.assertTrue(any("syntax" in r.lower() or "malformed" in r.lower() for r in report.reasons))

        # Also inspect_proof fails closed
        inspect_res = inspect_proof(self.workspace, "latest")
        self.assertEqual(inspect_res.state, ProofInspectionState.INVALID)
        self.assertEqual(inspect_res.exit_code, 1)

    # -------------------------------------------------------------------------
    # 8. Empty proof store returns documented INCOMPLETE/MISSING outcome (exit 2) for 'latest'
    # -------------------------------------------------------------------------
    def test_latest_target_empty_store_fails_closed(self) -> None:
        verifier = DeterministicProofVerifier(self.workspace)
        report = verifier.verify("latest")

        self.assertEqual(report.exit_code, 2)
        self.assertEqual(report.overall_status, ProofVerificationStatus.INCOMPLETE)
        self.assertTrue(any("no proof artifacts found" in r.lower() for r in report.reasons))

        inspect_res = inspect_proof(self.workspace, "latest")
        self.assertEqual(inspect_res.exit_code, 2)
        self.assertEqual(inspect_res.state, ProofInspectionState.MISSING)
        self.assertTrue("no proof artifacts found" in (inspect_res.error_message or "").lower())
        self.assertEqual((inspect_res.error_message or "").lower().count("no proof artifacts found"), 1)

    # -------------------------------------------------------------------------
    # 9. Explicit task ID and JSON path still work unchanged
    # -------------------------------------------------------------------------
    def test_explicit_task_id_and_json_path_still_work(self) -> None:
        self._create_and_save_proof("task_explicit")

        verifier = DeterministicProofVerifier(self.workspace)
        rep1 = verifier.verify("task_explicit")
        self.assertEqual(rep1.exit_code, 0)
        self.assertEqual(rep1.task_id, "task_explicit")

        proof_path = str(self.workspace / ".brainfrog" / "proofs" / "task_explicit.json")
        rep2 = verifier.verify(proof_path)
        self.assertEqual(rep2.exit_code, 0)
        self.assertEqual(rep2.task_id, "task_explicit")

    # -------------------------------------------------------------------------
    # 10. 'show-proof latest' and 'verify-proof latest' exit-code semantics
    # -------------------------------------------------------------------------
    def test_latest_cli_show_proof_and_verify_proof_semantics(self) -> None:
        # Case A: Latest is VERIFIED -> both return 0
        self._create_and_save_proof("task_verified", verdict_status="VERIFIED", completed_at="2026-10-12T12:00:00Z")
        self.assertEqual(verify_proof_cli(self.workspace, "latest"), 0)
        self.assertEqual(show_proof_cli(self.workspace, "latest"), 0)

        # Case B: Newer task is FAILED -> both return 3
        self._create_and_save_proof("task_failed", verdict_status="FAILED", tx_status="ROLLED_BACK", exit_code=1, completed_at="2026-10-12T13:00:00Z")
        self.assertEqual(verify_proof_cli(self.workspace, "latest"), 3)
        self.assertEqual(show_proof_cli(self.workspace, "latest"), 3)

    # -------------------------------------------------------------------------
    # 11. Path traversal attempts rejected
    # -------------------------------------------------------------------------
    def test_path_traversal_attempts_rejected(self) -> None:
        verifier = DeterministicProofVerifier(self.workspace)

        # Attempting traversal does not trigger 'latest' resolution
        report = verifier.verify("latest/../outside")
        self.assertEqual(report.exit_code, 2)  # Missing, not resolved as 'latest'

        # Embedded null byte rejected
        report_null = verifier.verify("latest\0malicious")
        self.assertEqual(report_null.exit_code, 1)

    # -------------------------------------------------------------------------
    # 12. Secret redaction in list-proofs (task_id, verdict.status, changes.status)
    # -------------------------------------------------------------------------
    def test_secret_redaction_in_list_proofs(self) -> None:
        # A. Secret in task_id
        secret_task_id = "task_ghp_secretTokenValue1234567890"
        self._create_and_save_proof(secret_task_id)

        # B. Secret in non-standard verdict.status and changes.status
        store = FileProofStore(self.workspace)
        proof_with_secrets = {
            "version": "1.0.0",
            "task": {"task_id": "task_secret_status", "completed_at": "2026-10-12T10:00:00Z"},
            "verdict": {"status": "ghp_secretVerdictToken1234567890"},
            "verification": {"test_command": "true", "exit_code": 0},
            "changes": {"status": "ghp_secretChangesToken1234567890"},
        }
        (store.proofs_dir / "task_secret_status.json").write_text(json.dumps(proof_with_secrets), encoding="utf-8")

        with cli.console.capture() as capture:
            code = list_proofs_cli(self.workspace)

        self.assertEqual(code, 0)
        captured = capture.get()

        # Raw secrets must never appear
        self.assertNotIn("ghp_secretTokenValue1234567890", captured)
        self.assertNotIn("ghp_secretVerdictToken1234567890", captured)
        self.assertNotIn("ghp_secretChangesToken1234567890", captured)

        # Expected redaction markers must be present
        self.assertIn("[REDACTED_SECRET]", captured)

    # -------------------------------------------------------------------------
    # 13. CLI dispatch and aliases
    # -------------------------------------------------------------------------
    def test_cli_dispatch_and_aliases(self) -> None:
        self._create_and_save_proof("task_for_cli")

        # A. Positional list-proofs
        with patch("cli.list_proofs_cli", return_value=0) as mock_list, \
             patch("cli.find_git_root", return_value=self.workspace), \
             patch("cli.load_all_envs"):
            with patch.object(sys, "argv", ["brainfrog", "-r", str(self.workspace), "list-proofs"]):
                code = cli_main()
                self.assertEqual(code, 0)
                mock_list.assert_called_with(self.workspace)

        # B. Positional proofs alias
        with patch("cli.list_proofs_cli", return_value=0) as mock_list, \
             patch("cli.find_git_root", return_value=self.workspace), \
             patch("cli.load_all_envs"):
            with patch.object(sys, "argv", ["brainfrog", "-r", str(self.workspace), "proofs"]):
                code = cli_main()
                self.assertEqual(code, 0)
                mock_list.assert_called_with(self.workspace)

        # C. Flag --list-proofs
        with patch("cli.list_proofs_cli", return_value=0) as mock_list, \
             patch("cli.find_git_root", return_value=self.workspace), \
             patch("cli.load_all_envs"):
            with patch.object(sys, "argv", ["brainfrog", "-r", str(self.workspace), "--list-proofs"]):
                code = cli_main()
                self.assertEqual(code, 0)
                mock_list.assert_called_with(self.workspace)

        # D. Flag --proofs
        with patch("cli.list_proofs_cli", return_value=0) as mock_list, \
             patch("cli.find_git_root", return_value=self.workspace), \
             patch("cli.load_all_envs"):
            with patch.object(sys, "argv", ["brainfrog", "-r", str(self.workspace), "--proofs"]):
                code = cli_main()
                self.assertEqual(code, 0)
                mock_list.assert_called_with(self.workspace)

        # E. show-proof latest
        with patch("cli.show_proof_cli", return_value=0) as mock_show, \
             patch("cli.find_git_root", return_value=self.workspace), \
             patch("cli.load_all_envs"):
            with patch.object(sys, "argv", ["brainfrog", "-r", str(self.workspace), "show-proof", "latest"]):
                code = cli_main()
                self.assertEqual(code, 0)
                mock_show.assert_called_with(self.workspace, "latest", check_workspace=False)

        # F. verify-proof latest
        with patch("cli.verify_proof_cli", return_value=0) as mock_verify, \
             patch("cli.find_git_root", return_value=self.workspace), \
             patch("cli.load_all_envs"):
            with patch.object(sys, "argv", ["brainfrog", "-r", str(self.workspace), "verify-proof", "latest"]):
                code = cli_main()
                self.assertEqual(code, 0)
                mock_verify.assert_called_with(self.workspace, "latest", check_workspace=False)

        # G. proof latest alias
        with patch("cli.show_proof_cli", return_value=0) as mock_show, \
             patch("cli.find_git_root", return_value=self.workspace), \
             patch("cli.load_all_envs"):
            with patch.object(sys, "argv", ["brainfrog", "-r", str(self.workspace), "proof", "latest"]):
                code = cli_main()
                self.assertEqual(code, 0)
                mock_show.assert_called_with(self.workspace, "latest", check_workspace=False)

    # -------------------------------------------------------------------------
    # 14. Rich markup injection and bracket handling
    # -------------------------------------------------------------------------
    def test_list_proofs_rich_markup_injection_protection(self) -> None:
        store = FileProofStore(self.workspace)
        store.proofs_dir.mkdir(parents=True, exist_ok=True)

        proof_markup = {
            "version": "1.0.0",
            "task": {
                "task_id": "task_[test]",
                "completed_at": "2026-10-12T[unclosed_ts",
            },
            "verdict": {"status": "[bold]bad[/test]"},
            "verification": {"test_command": "true", "exit_code": 0},
            "changes": {"status": "[unmatched_tx"},
        }
        (store.proofs_dir / "task_[test].json").write_text(json.dumps(proof_markup), encoding="utf-8")

        with cli.console.capture() as capture:
            code = list_proofs_cli(self.workspace)

        self.assertEqual(code, 0)
        captured = capture.get()

        # Values must be displayed literally without raising MarkupError or being swallowed
        self.assertIn("task_[test]", captured)
        self.assertIn("[bold]bad[/test]", captured)
        self.assertIn("[unmatched_tx", captured)

    # -------------------------------------------------------------------------
    # 15. REPL slash commands routing
    # -------------------------------------------------------------------------
    def test_repl_slash_commands_proof_discovery(self) -> None:
        from cli import run_interactive
        from unittest.mock import MagicMock
        mock_session = MagicMock()
        mock_session.prompt.side_effect = ["/list-proofs", "/proofs", "/exit"]

        with patch("cli.list_proofs_cli", return_value=0) as mock_list, \
             patch("prompt_toolkit.shortcuts.PromptSession", return_value=mock_session), \
             patch.object(cli.console, "input", side_effect=["/list-proofs", "/proofs", "/exit"]), \
             patch("cli.ensure_git_remote"):
            try:
                run_interactive(self.workspace)
            except Exception:
                pass
            self.assertEqual(mock_list.call_count, 2)
            mock_list.assert_called_with(self.workspace)


if __name__ == "__main__":
    unittest.main()
