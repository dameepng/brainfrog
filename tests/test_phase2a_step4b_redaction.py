"""Tests for BrainFrog Phase 2A Step 4B — Subprocess Output Redaction.

Demonstrates security-first testing invariants:
1. Synthetic API keys do not appear in persisted proof artifacts (.json or .md).
2. Bearer tokens do not appear in logs, diagnostics, or surfaced errors.
3. Passwords and keys in environment assignments (stdout/stderr) are redacted.
4. Redaction applies symmetrically to both stdout and stderr.
5. Bounded output limits and truncation markers are preserved alongside redaction.
6. Fail-closed behavior guarantees that redaction errors never fall back to raw output.
7. Ordinary, nonsensitive diagnostic text is preserved without destruction.
8. Secrets do not reach LLM prompts (System 1/2 state, retry prompts, auto-reflection).
9. Existing proof-verification semantics remain intact.

SYNTHETIC SECRETS ONLY. Never access or print real credentials.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.runtime.process import HardenedProcessResult, ProcessExitStatus, run_hardened_subprocess
from core.runtime.proof import (
    ChangeRecord,
    ChangesEvidence,
    FileProofStore,
    ProofArtifact,
    ProofVerdict,
    ProcessEvidence,
    ReproducibilityEvidence,
    TaskEvidence,
    VerificationEvidence,
    redact_secrets,
)
from core.runtime.proof_verifier import (
    DeterministicProofVerifier,
    ProofVerificationStatus,
)
from core.runtime.secret_scrubbing import safe_scrub, scrub_secrets
from core.runtime.transaction import (
    Transaction,
    TransactionCoordinator,
    TransactionResult,
    TransactionStatus,
)
from orchestrator import Orchestrator, PlanStep, RunConfig, StepResult
from system1.base import Answer
from core.runtime.workflow import TaskResult


class TestSubprocessOutputRedaction(unittest.TestCase):
    """Adversarial tests for subprocess output redaction."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="brainfrog_step4b_test_")
        self.workspace = Path(self.test_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_synthetic_api_keys_redacted_from_persisted_proof(self) -> None:
        """Synthetic API keys must not appear in persisted proof JSON or Markdown."""
        fake_openai_key = "sk-proj-FAKEKEY1234567890abcdef1234567890abcdef"
        fake_google_key = "AIzaSy" + "FAKEKEY98765432101234567890abcdef"  # gitguardian:ignore
        fake_gh_pat = "github_pat_11AAAAAAA01234567890abcdefghijklmnopqrstuvwxyz"
        fake_bot_token = "1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ1234567"

        raw_stdout = (
            f"Running integration tests...\n"
            f"DEBUG: Loaded OpenAI API Key: {fake_openai_key}\n"
            f"DEBUG: Loaded Google API Key: {fake_google_key}\n"
            f"DEBUG: Loaded GitHub Token: {fake_gh_pat}\n"
            f"DEBUG: Loaded Telegram Bot Token: {fake_bot_token}\n"
            f"3 passed in 0.12s\n"
        )

        v_evidence = VerificationEvidence(
            test_command="python -m pytest",
            exit_code=0,
            stdout_summary=raw_stdout,
            stderr_summary="",
        )

        # Dataclass post-init automatically scrubs
        self.assertNotIn(fake_openai_key, v_evidence.stdout_summary)
        self.assertNotIn(fake_google_key, v_evidence.stdout_summary)
        self.assertNotIn(fake_gh_pat, v_evidence.stdout_summary)
        self.assertNotIn(fake_bot_token, v_evidence.stdout_summary)

        task_ev = TaskEvidence(
            task_id="task_sec_001",
            session_id="session_sec_001",
            description="Verify secret redaction in proof persistence",
            created_at="2026-10-14T00:00:00Z",
            completed_at="2026-10-14T00:00:01Z",
            duration_ms=1000,
        )
        artifact = ProofArtifact(
            version="1.0.0",
            task=task_ev,
            verdict=ProofVerdict(status="VERIFIED", reason="Tests passed cleanly."),
            verification=v_evidence,
            changes=ChangesEvidence(transaction_id="tx_sec_001", status="COMMITTED"),
            reproducibility=ReproducibilityEvidence(command="python -m pytest"),
        )

        store = FileProofStore(self.workspace)
        md_text = f"# Proof for {task_ev.task_id}\n\nOutput:\n```\n{raw_stdout}\n```\n"
        pj, pm = store.save(artifact, md_text)

        # Read directly from disk
        saved_json = pj.read_text(encoding="utf-8")
        self.assertNotIn(fake_openai_key, saved_json)
        self.assertNotIn(fake_google_key, saved_json)
        self.assertNotIn(fake_gh_pat, saved_json)
        self.assertNotIn(fake_bot_token, saved_json)
        self.assertIn("[REDACTED_SECRET]", saved_json)

        self.assertIsNotNone(pm)
        saved_md = pm.read_text(encoding="utf-8")
        self.assertNotIn(fake_openai_key, saved_md)
        self.assertNotIn(fake_google_key, saved_md)
        self.assertNotIn(fake_gh_pat, saved_md)
        self.assertNotIn(fake_bot_token, saved_md)

    def test_bearer_token_redacted_from_logs_and_diagnostics(self) -> None:
        """Bearer tokens must not leak into log callbacks or surfaced process results."""
        fake_bearer = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.syntheticpayload12345.sig"
        fake_auth_header = "Authorization: Bearer secret_access_token_999888"

        logged_messages = []
        cfg = RunConfig(
            repo_dir=self.workspace,
            task="Test logging redaction",
            test_command=["pytest"],
        )
        orch = Orchestrator(MagicMock(), MagicMock(), cfg, log_fn=lambda msg: logged_messages.append(msg))

        orch._log(f"Process failed with header: {fake_bearer}")
        orch._log(f"HTTP response dump: {fake_auth_header}")

        combined_logs = "\n".join(logged_messages)
        self.assertNotIn(fake_bearer, combined_logs)
        self.assertNotIn("secret_access_token_999888", combined_logs)
        self.assertIn("[REDACTED", combined_logs)

    def test_passwords_and_env_assignments_redacted_in_stdout_and_stderr(self) -> None:
        """Environment variable assignments and passwords in stdout/stderr must be redacted."""
        fake_db_pass = "P@ssw0rd_Super_Secret_999!"
        fake_api_key = "abc123xyz" + "SecretKey987654"  # gitguardian:ignore
        fake_auth_token = "token_value_abc_45678"

        test_stdout = (
            f"export DB_PASSWORD={fake_db_pass}\n"
            f"SET API_KEY={fake_api_key}\n"
            f'{{"auth_token": "{fake_auth_token}"}}\n'
        )
        test_stderr = (
            f"Error connecting to database with password={fake_db_pass}\n"
            f"Failed token validation: token: '{fake_auth_token}'\n"
        )

        res = HardenedProcessResult(
            args=["fake_runner"],
            returncode=1,
            stdout=test_stdout,
            stderr=test_stderr,
            duration_ms=50,
        )

        # Raw stdout/stderr remain intact for internal exit-code / control evaluation
        self.assertEqual(res.stdout, test_stdout)
        self.assertEqual(res.stderr, test_stderr)

        # Sanitized representations are strictly redacted
        self.assertNotIn(fake_db_pass, res.sanitized_stdout)
        self.assertNotIn(fake_api_key, res.sanitized_stdout)
        self.assertNotIn(fake_auth_token, res.sanitized_stdout)

        self.assertNotIn(fake_db_pass, res.sanitized_stderr)
        self.assertNotIn(fake_auth_token, res.sanitized_stderr)

        self.assertNotIn(fake_db_pass, res.sanitized_output)
        self.assertNotIn(fake_api_key, res.sanitized_output)
        self.assertNotIn(fake_auth_token, res.sanitized_output)

    def test_pem_private_key_redacted(self) -> None:
        """PEM Private keys must be completely scrubbed."""
        fake_key = (
            "-----BEGIN " + "RSA PRIVATE KEY-----\n"  # gitguardian:ignore
            "MIIEowIBAAKCAQEA0Y1+syntheticdatafakekeydata1234567890abcdef====\n"
            "-----END " + "RSA PRIVATE KEY-----"
        )
        res = HardenedProcessResult(
            args=["fake_test"],
            returncode=0,
            stdout=f"SSL certificate generated:\n{fake_key}\nAll OK",
            stderr="",
            duration_ms=10,
        )

        self.assertNotIn("MIIEowIBAAKCAQEA0Y1", res.sanitized_stdout)
        self.assertNotIn("syntheticdatafakekeydata", res.sanitized_stdout)
        self.assertIn("[REDACTED", res.sanitized_stdout)

    def test_truncated_output_remains_bounded_and_marked(self) -> None:
        """Bounded output capture retains truncation marker and size bounds while scrubbing."""
        from core.runtime.proof import capture_bounded_output

        fake_key = "sk-proj-FAKETOKEN1234567890abcdef"
        large_noisy_output = (f"Line with secret {fake_key}\n" * 500)  # ~17.5 KB

        bounded_text, was_truncated = capture_bounded_output(large_noisy_output)
        self.assertTrue(was_truncated)
        self.assertIn("TRUNCATED", bounded_text)
        self.assertLessEqual(len(bounded_text.encode("utf-8")), 5120 + 200)

        # Redact the bounded text
        redacted = redact_secrets(bounded_text)
        self.assertNotIn(fake_key, redacted)
        self.assertIn("TRUNCATED", redacted)
        self.assertIn("[REDACTED_SECRET]", redacted)

    def test_redaction_failure_fails_closed_in_safe_scrub(self) -> None:
        """Redaction failure must fail closed and never fall back to raw sensitive text."""
        raw_secret_text = "CRITICAL SECRET: sk-abcdef1234567890abcdef1234567890"

        with patch("core.runtime.secret_scrubbing.scrub_secrets", side_effect=ValueError("Simulated Regex Bomb")):
            scrubbed = safe_scrub(raw_secret_text)
            self.assertNotIn("sk-abcdef1234567890abcdef1234567890", scrubbed)
            self.assertIn("[REDACTION_FAILED: ValueError]", scrubbed)

    def test_proof_persistence_fails_closed_on_redaction_error(self) -> None:
        """FileProofStore.save must abort and fail closed if redaction fails."""
        task_ev = TaskEvidence(
            task_id="task_fail_closed_001",
            session_id="session_001",
            description="Testing fail-closed persistence",
            created_at="2026-10-14T00:00:00Z",
            completed_at="2026-10-14T00:00:01Z",
            duration_ms=100,
        )
        artifact = ProofArtifact(
            version="1.0.0",
            task=task_ev,
            verdict=ProofVerdict(status="VERIFIED", reason="Clean"),
            verification=VerificationEvidence("pytest", 0, "All passed", ""),
            changes=ChangesEvidence("tx_1", "COMMITTED"),
            reproducibility=ReproducibilityEvidence("pytest"),
        )
        store = FileProofStore(self.workspace)

        with patch("core.runtime.proof.redact_secrets", side_effect=RuntimeError("Scrubbing crash")):
            with self.assertRaises(IOError) as ctx:
                store.save(artifact, "# Markdown Report")
            self.assertIn("Proof redaction failed closed", str(ctx.exception))

        # Ensure no temporary or permanent proof files were created
        self.assertFalse(store.exists("task_fail_closed_001"))
        tmp_files = list(store.proofs_dir.glob("*.tmp*")) if store.proofs_dir.exists() else []
        self.assertEqual(len(tmp_files), 0)

    def test_ordinary_nonsensitive_diagnostics_remain_useful(self) -> None:
        """Ordinary non-sensitive output must not be destroyed or falsely redacted."""
        ordinary_output = (
            "============================= test session starts =============================\n"
            "rootdir: C:\\dame-project\\tools\\agentic_dev\n"
            "collected 14 items\n\n"
            "tests/test_math.py ....                                                 [ 28%]\n"
            "tests/test_calc.py ..........                                           [100%]\n\n"
            "============================== 14 passed in 0.45s ==============================\n"
        )
        scrubbed = safe_scrub(ordinary_output)
        self.assertEqual(scrubbed, ordinary_output)

        proof_scrubbed = redact_secrets(ordinary_output)
        self.assertEqual(proof_scrubbed, ordinary_output)

    def test_secrets_do_not_reach_llm_prompts(self) -> None:
        """Secrets in subprocess outputs must not leak into System 1/2 prompts."""
        fake_secret_key = "sk-proj-LLMLEAKTEST1234567890abcdef12345"
        mock_output = f"AssertionError: failed comparing config with key {fake_secret_key}\nexit 1"

        fail_proc = HardenedProcessResult(
            args=["pytest"],
            returncode=1,
            stdout=mock_output,
            stderr="",
            duration_ms=100,
        )

        mock_s1 = MagicMock()
        mock_s1.name = "jev_mock"
        captured_state = {}

        def record_state(state, questions):
            captured_state.update(state)
            return {
                "tests_passing": Answer(choice="no", noul=0.0),
                "diff_complete": Answer(choice="yes", noul=1.0),
                "failure_fixable": Answer(choice="yes", noul=1.0),
                "retry_concern": Answer(score="within_normal", confidence=0.9),
            }

        mock_s1.decide.side_effect = record_state

        captured_retry_prompts = []
        mock_s2 = MagicMock()
        mock_s2.review_and_fix.side_effect = lambda task, step, current, prompt: (
            captured_retry_prompts.append(prompt) or {}
        )

        cfg = RunConfig(
            repo_dir=self.workspace,
            task="Fix the broken test",
            test_command=["pytest"],
            max_retries=1,
        )
        orch = Orchestrator(mock_s1, mock_s2, cfg)

        step = PlanStep(id="1", description="Fix secret check", files=[])

        # Patch execution to return fail_proc
        with patch.object(orch, "_run_test_suite", return_value=fail_proc):
            with patch.object(orch, "_stage_and_execute_transaction") as mock_stage:
                mock_coord = MagicMock()
                mock_coord.tx.status = TransactionStatus.VERIFYING
                mock_coord.tx.is_terminal = False
                mock_coord.rollback.return_value = TransactionResult("tx1", TransactionStatus.ROLLED_BACK, False, True, (), None, None)
                mock_stage.return_value = mock_coord

                orch._run_step(step)

        # 1. Check System 1 state payload
        test_summary = captured_state.get("execution", {}).get("test_summary", "")
        self.assertNotIn(fake_secret_key, test_summary)
        self.assertIn("[REDACTED", test_summary)

        # 2. Check System 2 retry prompt
        self.assertTrue(len(captured_retry_prompts) > 0)
        for prompt in captured_retry_prompts:
            self.assertNotIn(fake_secret_key, prompt)
            self.assertIn("[REDACTED", prompt)

    def test_existing_proof_verification_semantics_intact(self) -> None:
        """Redacted proof artifacts must cleanly satisfy DeterministicProofVerifier."""
        task_id = "task_verify_001"
        proc_ev = ProcessEvidence(
            task_id=task_id,
            command="python -m pytest tests/test_ok.py",
            returncode=0,
            timed_out=False,
            cancelled=False,
        )
        v_evidence = VerificationEvidence(
            test_command="python -m pytest tests/test_ok.py",
            exit_code=0,
            stdout_summary="1 passed in 0.05s\nToken: [REDACTED_SECRET]",
            stderr_summary="",
            gate_checks={
                "test_suite": True,
                "workspace_scope": True,
                "transaction_integrity": True,
                "execution_integrity": True,
                "trust_integrity": True,
            },
            process_evidence=proc_ev,
        )
        task_ev = TaskEvidence(
            task_id=task_id,
            session_id="session_001",
            description="Autonomous verified task",
            created_at="2026-10-14T00:00:00Z",
            completed_at="2026-10-14T00:00:01Z",
            duration_ms=250,
        )
        artifact = ProofArtifact(
            version="1.0.0",
            task=task_ev,
            verdict=ProofVerdict(status="VERIFIED", reason="All invariants passed"),
            verification=v_evidence,
            changes=ChangesEvidence(transaction_id="tx_v1", status="COMMITTED", files=[]),
            reproducibility=ReproducibilityEvidence(command="python -m pytest tests/test_ok.py"),
        )

        store = FileProofStore(self.workspace)
        pj, _ = store.save(artifact, "# Verification Report")

        verifier = DeterministicProofVerifier(self.workspace)
        report = verifier.verify(task_id)

        self.assertEqual(report.overall_status, ProofVerificationStatus.VALID)
        self.assertEqual(len(report.reasons), 0)

    def test_finding1_task_result_test_output_sanitized(self) -> None:
        """Finding 1: TaskResult.test_output and related fields must be sanitized."""
        fake_secret = "sk-proj-FINDING1SECRET998877665544332211"
        fake_db_pass = 'DB_PASSWORD="super_secret_db_pass_123"'
        raw_output = f"Running tests...\nFailure in test: {fake_secret}\nEnv: {fake_db_pass}\n2 failed"

        # Direct construction
        tr = TaskResult(
            task_id="task_res_001",
            user_request="Implement auth",
            success=False,
            verified=False,
            status="FAILED",
            summary=f"Run failed: {fake_secret}",
            test_output=raw_output,
            failure_reason=f"Secret leak: {fake_secret}",
        )
        self.assertNotIn(fake_secret, tr.test_output)
        self.assertNotIn("super_secret_db_pass_123", tr.test_output)
        self.assertNotIn(fake_secret, tr.summary)
        self.assertNotIn(fake_secret, tr.failure_reason)
        self.assertIn("[REDACTED", tr.test_output)
        self.assertIn("2 failed", tr.test_output)

    def test_finding2_preserve_valid_json_during_redaction(self) -> None:
        """Finding 2: Valid JSON must remain syntactically valid JSON after redaction."""
        fake_key = "sk-proj-JSONKEY1234567890abcdef1234567890"
        fake_pass = "my_super_secret_password_123"
        fake_token = "tok_synthetic_access_token_999"

        cases = [
            '{"password": "my_super_secret_password_123"}',
            '{"DB_PASSWORD": "my_super_secret_password_123"}',
            '{"token": "tok_synthetic_access_token_999", "message": "ordinary output"}',
            '{"nested": {"auth_token": "tok_synthetic_access_token_999", "data": [1, 2, {"password": "my_super_secret_password_123"}]}}',
            f'{{"message": "Diagnostic log containing {fake_key}", "code": 500}}',
            '{"escaped": "Line 1\\nLine 2\\t\\"quoted string\\""}',
            f'["{fake_key}", {{"password": "{fake_pass}"}}]',
        ]

        for raw_json in cases:
            # Test scrub_secrets
            clean_json = scrub_secrets(raw_json)
            parsed = json.loads(clean_json)
            self.assertNotIn(fake_key, clean_json)
            self.assertNotIn(fake_pass, clean_json)
            self.assertNotIn(fake_token, clean_json)
            if "message" in parsed and isinstance(parsed, dict) and "ordinary output" in raw_json:
                self.assertEqual(parsed["message"], "ordinary output")

            # Test redact_secrets from proof module
            clean_proof_json = redact_secrets(raw_json)
            parsed_proof = json.loads(clean_proof_json)
            self.assertNotIn(fake_key, clean_proof_json)
            self.assertNotIn(fake_pass, clean_proof_json)
            self.assertNotIn(fake_token, clean_proof_json)

    def test_finding3_pipeline_non_interference_and_complex_values(self) -> None:
        """Finding 3: Prevent interference between pipelines on quoted complex values."""
        cases = [
            ('export DB_PASSWORD="secret:with:colons"', 'export DB_PASSWORD="[REDACTED]"'),
            ('export DB_PASSWORD="secret=with=equals"', 'export DB_PASSWORD="[REDACTED]"'),
            ('export DB_PASSWORD="secret,with,commas"', 'export DB_PASSWORD="[REDACTED]"'),
            ('export DB_PASSWORD="secret with spaces"', 'export DB_PASSWORD="[REDACTED]"'),
            ('export DB_PASSWORD="secret\\"with\\"quotes"', 'export DB_PASSWORD="[REDACTED]"'),
            ('export DB_PASSWORD="secret\\\\with\\\\backslashes"', 'export DB_PASSWORD="[REDACTED]"'),
            ('export DB_PASSWORD=\'secret:with:colons\'', 'export DB_PASSWORD=\'[REDACTED]\''),
            ('export DB_PASSWORD=secret123', 'export DB_PASSWORD=[REDACTED]'),
            ('password: ' + 'plainsecret123', 'password: [REDACTED]'),  # gitguardian:ignore
            ('Authorization: Bearer synthetic_token_123', 'Authorization: Bearer [REDACTED]'),
        ]

        from core.runtime.session import scrub_secrets as session_scrub

        for raw, expected in cases:
            # Test session scrub
            s_res = session_scrub(raw)
            self.assertNotIn("secret:", s_res)
            self.assertNotIn("secret=", s_res)
            self.assertNotIn("secret123", s_res)
            self.assertNotIn("synthetic_token_123", s_res)
            self.assertEqual(s_res, expected)

            # Test secret_scrubbing scrub
            c_res = scrub_secrets(raw)
            self.assertEqual(c_res, expected)

            # Test proof redact_secrets
            p_res = redact_secrets(raw)
            self.assertNotIn("secret:", p_res)
            self.assertNotIn("secret=", p_res)
            self.assertNotIn("secret123", p_res)
            self.assertNotIn("synthetic_token_123", p_res)
            self.assertIn("[REDACTED_SECRET]", p_res)

    def test_finding4_proof_save_fails_closed_without_partial_artifacts(self) -> None:
        """Finding 4: FileProofStore.save fails closed on redaction error without leaving partial files."""
        task_id = "task_f4_failclosed"
        task_ev = TaskEvidence(
            task_id=task_id,
            session_id="session_f4",
            description="Testing fail-closed proof persistence",
            created_at="2026-10-14T00:00:00Z",
            completed_at="2026-10-14T00:00:01Z",
            duration_ms=100,
        )
        artifact = ProofArtifact(
            version="1.0.0",
            task=task_ev,
            verdict=ProofVerdict(status="VERIFIED", reason="Clean"),
            verification=VerificationEvidence("pytest", 0, "All passed", ""),
            changes=ChangesEvidence("tx_1", "COMMITTED"),
            reproducibility=ReproducibilityEvidence("pytest"),
        )
        store = FileProofStore(self.workspace)

        # 1. Independent JSON redaction failure
        with patch("core.runtime.proof.redact_secrets", side_effect=[RuntimeError("JSON Scrub Fail"), "Clean MD"]):
            with self.assertRaises(IOError) as ctx:
                store.save(artifact, "# Markdown Report")
            self.assertIn("Proof redaction failed closed", str(ctx.exception))

        # Assert no temporary or final artifacts
        self.assertFalse(store.exists(task_id))
        tmp_files = list(store.proofs_dir.glob(".tmp_proof_*")) if store.proofs_dir.exists() else []
        self.assertEqual(len(tmp_files), 0)

        # 2. Independent Markdown redaction failure
        with patch("core.runtime.proof.redact_secrets", side_effect=['{"clean": "json"}', RuntimeError("Markdown Scrub Fail")]):
            with self.assertRaises(IOError) as ctx:
                store.save(artifact, "# Markdown Report")
            self.assertIn("Proof redaction failed closed", str(ctx.exception))

        # Assert no temporary or final artifacts
        self.assertFalse(store.exists(task_id))
        tmp_files = list(store.proofs_dir.glob(".tmp_proof_*")) if store.proofs_dir.exists() else []
        self.assertEqual(len(tmp_files), 0)

        # 3. Invalid JSON produced by redaction failure
        with patch("core.runtime.proof.redact_secrets", side_effect=["[MALFORMED_JSON_RESULT", "# MD"]):
            with self.assertRaises(IOError) as ctx:
                store.save(artifact, "# Markdown Report")
            self.assertIn("Proof redaction failed closed", str(ctx.exception))

        self.assertFalse(store.exists(task_id))
        tmp_files = list(store.proofs_dir.glob(".tmp_proof_*")) if store.proofs_dir.exists() else []
        self.assertEqual(len(tmp_files), 0)


class TestProofArtifactReplacementRecovery(unittest.TestCase):
    """Adversarial tests for proof artifact replacement failure recovery.

    Verifies:
    1. Markdown replacement failure does not leave partial files.
    2. JSON replacement failure after Markdown succeeds does not leave orphaned Markdown.
    3. Overwriting existing JSON and Markdown files works cleanly.
    4. Overwrite failure during Markdown replacement preserves pre-existing artifacts.
    5. Overwrite failure during JSON replacement restores pre-existing Markdown.
    6. Backup failure fails closed while preserving pre-existing artifacts.
    7. Catastrophic restore failure preserves backup without destroying the only valid copy.
    8. Redaction failure occurs before disk staging and leaves no artifacts.
    """

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="brainfrog_recovery_test_")
        self.workspace = Path(self.test_dir)
        self.store = FileProofStore(self.workspace)

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _make_proof(self, task_id: str, desc: str = "Test") -> ProofArtifact:
        return ProofArtifact(
            version="1.0.0",
            task=TaskEvidence(
                task_id=task_id,
                session_id=f"sess_{task_id}",
                description=desc,
                created_at="2026-10-14T00:00:00Z",
                completed_at="2026-10-14T00:00:01Z",
                duration_ms=50,
            ),
            verdict=ProofVerdict(status="VERIFIED", reason="Clean"),
            verification=VerificationEvidence("pytest", 0, "pass", ""),
            changes=ChangesEvidence("tx_1", "COMMITTED"),
            reproducibility=ReproducibilityEvidence("pytest"),
        )

    def test_fresh_save_markdown_replacement_failure(self) -> None:
        task_id = "task_fresh_md_fail"
        proof = self._make_proof(task_id)
        final_json = self.store.proof_json_path(task_id)
        final_md = self.store.proof_markdown_path(task_id)

        real_replace = os.replace

        def fake_replace(src, dst):
            if str(src).endswith(".md") and "tmp_proof" in str(src):
                raise OSError("Disk full during markdown replacement")
            return real_replace(src, dst)

        with patch("os.replace", side_effect=fake_replace):
            with self.assertRaises(IOError) as ctx:
                self.store.save(proof, "# Fresh MD")
            self.assertIn("Proof markdown replacement", str(ctx.exception))

        self.assertFalse(self.store.exists(task_id))
        self.assertFalse(final_json.exists())
        self.assertFalse(final_md.exists())

        # No temp or backup files remain
        remaining = [p.name for p in self.store.proofs_dir.glob(".*") if p.is_file()]
        self.assertEqual(remaining, [])

    def test_fresh_save_json_replacement_failure_after_markdown_succeeds(self) -> None:
        task_id = "task_fresh_json_fail"
        proof = self._make_proof(task_id)
        final_json = self.store.proof_json_path(task_id)
        final_md = self.store.proof_markdown_path(task_id)

        real_replace = os.replace

        def fake_replace(src, dst):
            if str(src).endswith(".json") and "tmp_proof" in str(src):
                raise OSError("Disk full during json replacement")
            return real_replace(src, dst)

        with patch("os.replace", side_effect=fake_replace):
            with self.assertRaises(IOError) as ctx:
                self.store.save(proof, "# Fresh MD")
            self.assertIn("Proof JSON replacement", str(ctx.exception))

        # Crucial check: Markdown replacement succeeded first, but JSON failed.
        # Recovery must have unlinked final_md so no orphaned Markdown exists!
        self.assertFalse(self.store.exists(task_id))
        self.assertFalse(final_json.exists())
        self.assertFalse(final_md.exists())

        remaining = [p.name for p in self.store.proofs_dir.glob(".*") if p.is_file()]
        self.assertEqual(remaining, [])

    def test_overwrite_existing_artifacts_success(self) -> None:
        task_id = "task_overwrite_success"
        proof_v1 = self._make_proof(task_id, "v1")
        self.store.save(proof_v1, "# Markdown v1")

        final_json = self.store.proof_json_path(task_id)
        final_md = self.store.proof_markdown_path(task_id)
        self.assertIn("v1", final_md.read_text(encoding="utf-8"))

        proof_v2 = self._make_proof(task_id, "v2")
        self.store.save(proof_v2, "# Markdown v2", allow_overwrite=True)

        self.assertTrue(self.store.exists(task_id))
        self.assertIn("v2", final_md.read_text(encoding="utf-8"))
        self.assertIn("v2", final_json.read_text(encoding="utf-8"))

        remaining = [p.name for p in self.store.proofs_dir.glob(".*") if p.is_file()]
        self.assertEqual(remaining, [])

    def test_overwrite_markdown_replacement_failure_preserves_originals(self) -> None:
        task_id = "task_overwrite_md_fail"
        proof_v1 = self._make_proof(task_id, "v1")
        self.store.save(proof_v1, "# Markdown v1")

        final_json = self.store.proof_json_path(task_id)
        final_md = self.store.proof_markdown_path(task_id)

        real_replace = os.replace

        def fake_replace(src, dst):
            if str(src).endswith(".md") and "tmp_proof" in str(src):
                raise OSError("Simulated md replacement failure")
            return real_replace(src, dst)

        proof_v2 = self._make_proof(task_id, "v2")
        with patch("os.replace", side_effect=fake_replace):
            with self.assertRaises(IOError) as ctx:
                self.store.save(proof_v2, "# Markdown v2", allow_overwrite=True)
            self.assertIn("Proof markdown replacement", str(ctx.exception))

        # Originals preserved intact
        self.assertTrue(self.store.exists(task_id))
        self.assertIn("v1", final_md.read_text(encoding="utf-8"))
        self.assertIn("v1", final_json.read_text(encoding="utf-8"))

        remaining = [p.name for p in self.store.proofs_dir.glob(".*") if p.is_file()]
        self.assertEqual(remaining, [])

    def test_overwrite_json_replacement_failure_restores_original_markdown(self) -> None:
        task_id = "task_overwrite_json_fail"
        proof_v1 = self._make_proof(task_id, "v1")
        self.store.save(proof_v1, "# Markdown v1")

        final_json = self.store.proof_json_path(task_id)
        final_md = self.store.proof_markdown_path(task_id)

        real_replace = os.replace

        def fake_replace(src, dst):
            if str(src).endswith(".json") and "tmp_proof" in str(src):
                raise OSError("Simulated json replacement failure")
            return real_replace(src, dst)

        proof_v2 = self._make_proof(task_id, "v2")
        with patch("os.replace", side_effect=fake_replace):
            with self.assertRaises(IOError) as ctx:
                self.store.save(proof_v2, "# Markdown v2", allow_overwrite=True)
            self.assertIn("Proof JSON replacement", str(ctx.exception))

        # Markdown was restored to v1 from backup, and JSON remained v1!
        self.assertTrue(self.store.exists(task_id))
        self.assertIn("v1", final_md.read_text(encoding="utf-8"))
        self.assertIn("v1", final_json.read_text(encoding="utf-8"))

        remaining = [p.name for p in self.store.proofs_dir.glob(".*") if p.is_file()]
        self.assertEqual(remaining, [])

    def test_overwrite_backup_failure_preserves_originals(self) -> None:
        task_id = "task_overwrite_bak_fail"
        proof_v1 = self._make_proof(task_id, "v1")
        self.store.save(proof_v1, "# Markdown v1")

        final_json = self.store.proof_json_path(task_id)
        final_md = self.store.proof_markdown_path(task_id)

        proof_v2 = self._make_proof(task_id, "v2")
        with patch("shutil.copy2", side_effect=OSError("Backup disk full")):
            with self.assertRaises(IOError) as ctx:
                self.store.save(proof_v2, "# Markdown v2", allow_overwrite=True)
            self.assertIn("Creating proof backups", str(ctx.exception))

        # Originals preserved intact
        self.assertTrue(self.store.exists(task_id))
        self.assertIn("v1", final_md.read_text(encoding="utf-8"))
        self.assertIn("v1", final_json.read_text(encoding="utf-8"))

        remaining = [p.name for p in self.store.proofs_dir.glob(".*") if p.is_file()]
        self.assertEqual(remaining, [])

    def test_restore_failure_preserves_backup_without_destroying_only_valid_copy(self) -> None:
        task_id = "task_restore_fail"
        proof_v1 = self._make_proof(task_id, "v1")
        self.store.save(proof_v1, "# Markdown v1")

        final_json = self.store.proof_json_path(task_id)
        final_md = self.store.proof_markdown_path(task_id)

        real_replace = os.replace

        def fake_replace(src, dst):
            if str(src).endswith(".json") and "tmp_proof" in str(src):
                raise OSError("JSON replacement failure")
            if "bak_proof" in str(src):
                # Simulate catastrophic failure during restore where final_md is missing
                if Path(dst).exists():
                    Path(dst).unlink()
                raise OSError("Markdown restore failed")
            return real_replace(src, dst)

        proof_v2 = self._make_proof(task_id, "v2")
        with patch("os.replace", side_effect=fake_replace):
            with self.assertRaises(IOError):
                self.store.save(proof_v2, "# Markdown v2", allow_overwrite=True)

        # Because final_md is missing and restore failed, bak_md MUST NOT have been unlinked!
        bak_files = list(self.store.proofs_dir.glob(".bak_proof_*_*.md"))
        self.assertEqual(len(bak_files), 1)
        self.assertIn("v1", bak_files[0].read_text(encoding="utf-8"))

    def test_redaction_failure_before_disk_staging_no_artifacts(self) -> None:
        task_id = "task_redaction_pre_staging"
        proof = self._make_proof(task_id)

        with patch("core.runtime.proof.redact_secrets", side_effect=RuntimeError("Secret scrub fail")):
            with self.assertRaises(IOError) as ctx:
                self.store.save(proof, "# MD")
            self.assertIn("Proof redaction failed closed", str(ctx.exception))

        self.assertFalse(self.store.exists(task_id))
        all_files = list(self.store.proofs_dir.iterdir()) if self.store.proofs_dir.exists() else []
        self.assertEqual(len(all_files), 0)

    def test_scrub_secrets_preserves_compact_json_and_whitespace_when_no_secrets(self) -> None:
        """Regression test: scrub_secrets must not mutate JSON whitespace or format when no secrets are present."""
        from core.runtime.secret_scrubbing import scrub_secrets

        compact_json = '{"version":"1.0.0","capabilities":["fs:read","git:commit"],"deterministic":true}'
        scrubbed = scrub_secrets(compact_json)
        self.assertEqual(scrubbed, compact_json)

        formatted_json = '{\n    "key": "value"\n}'
        self.assertEqual(scrub_secrets(formatted_json), formatted_json)


if __name__ == "__main__":
    unittest.main()
