"""BrainFrog Phase 4 — Proof Artifact Implementation and Adversarial Tests.

Verifies the complete proof artifact lifecycle:
    TASK -> PLAN -> CHANGE -> TEST -> VERIFY -> PROVE

Tests cover:
- Section 20 Required Tests (Happy path, rollback, cancellation, recovery, persistence, redaction, provenance)
- Section 21 Adversarial Tests (Tampering resistance, anti-LLM override, scope violation, path privacy, fail-closed)
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from core.runtime import (
    BrainFrogRuntime,
    ChangeRecord,
    ChangesEvidence,
    FileProofStore,
    GitProvenance,
    HardenedProcessResult,
    ProcessEvidence,
    ProcessExitStatus,
    ProofAlreadyExistsError,
    ProofArtifact,
    ProofVerdict,
    ReproducibilityEvidence,
    TaskEvidence,
    TaskPlan,
    TaskRequest,
    TaskResult,
    TaskStep,
    TransactionCoordinator,
    TransactionResult,
    TransactionStatus,
    VerificationEvidence,
    VerificationGate,
    build_change_records,
    capture_bounded_output,
    capture_git_provenance,
    compute_proof_verdict,
    execute_autonomous_task,
    is_sensitive_path,
    normalize_workspace_relative_path,
    redact_secrets,
    render_proof_markdown,
)
from core.runtime.transaction import Transaction, TransactionOperation, OperationType, OperationStatus
from orchestrator import PlanStep, StepResult
from system1.base import Answer

FIXTURE_SRC = Path(__file__).resolve().parent / "fixtures" / "phase3_demo"


@pytest.fixture
def phase4_repo(tmp_path: Path) -> Path:
    """Create a temporary git repository seeded from phase3_demo fixture."""
    repo = tmp_path / "phase4_project"
    shutil.copytree(FIXTURE_SRC, repo)

    try:
        subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "BrainFrog Tester"], cwd=str(repo), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "tester@brainfrog.local"], cwd=str(repo), check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "chore: initial broken fixture commit"], cwd=str(repo), check=True, capture_output=True)
    except Exception:
        pass
    return repo


@pytest.fixture
def non_git_repo(tmp_path: Path) -> Path:
    """Create a workspace that is explicitly NOT a git repository."""
    repo = tmp_path / "non_git_project"
    repo.mkdir()
    (repo / "calc.py").write_text("def add(a, b): return a - b\n", encoding="utf-8")
    (repo / "test_calc.py").write_text(
        "from calc import add\ndef test_add(): assert add(2, 2) == 4\n",
        encoding="utf-8",
    )
    return repo


class TestPhase4ProofArtifact:
    """Comprehensive test suite for Phase 4 Proof Artifact generation and security."""

    # =========================================================================
    # 1. Section 20: Required Tests
    # =========================================================================

    def test_proof_generated_on_successful_task(self, phase4_repo: Path):
        """Verify complete proof artifact generation for an autonomous successful task."""
        fixed_code = (
            'class MobileNavbar:\n'
            '    def __init__(self) -> None:\n'
            '        self.is_open: bool = False\n'
            '        self.breakpoint: int = 768\n'
            '    def toggle(self) -> None:\n'
            '        self.is_open = not self.is_open\n'
            '    def render(self, viewport_width: int) -> str:\n'
            '        if viewport_width < self.breakpoint and self.is_open:\n'
            '            return "<nav class=\'navbar mobile mobile-menu-active\'></nav>"\n'
            '        return "<nav class=\'navbar desktop\'></nav>"\n'
        )

        mock_s1 = MagicMock()
        mock_s1.name = "jev_mock"
        mock_s1.decide.side_effect = [
            # Scope gate
            {
                "likely_domain": Answer(choice="ui", confidence=0.9),
                "change_type": Answer(choice="code_change", confidence=0.9),
                "is_sensitive": Answer(noul=0.0),
                "complexity": Answer(score="low", confidence=0.9),
                "needs_tests": Answer(noul=0.8),
            },
            # Post-test evaluation
            {
                "tests_passing": Answer(choice="yes", noul=1.0),
                "diff_complete": Answer(choice="yes", noul=1.0),
                "failure_fixable": Answer(choice="no", noul=0.0),
                "retry_concern": Answer(score="within_normal", confidence=0.95),
            },
        ]

        mock_s2 = MagicMock()
        mock_s2.provider_name = "claude_mock"
        mock_s2.plan_task.return_value = [
            PlanStep(id="1", description="Fix navbar toggle", files=["navbar.py"])
        ]
        mock_s2.write_code.return_value = {"navbar.py": fixed_code}

        runtime = BrainFrogRuntime(
            repo_dir=phase4_repo,
            persist_sessions=False,
            default_test_cmd=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
            system1_factory=lambda b: mock_s1,
            system2_factory=lambda **k: mock_s2,
        )

        task_id = "task_p4_success_001"
        request = TaskRequest(
            task_id=task_id,
            user_request="Fix mobile navbar toggle",
            workspace=phase4_repo,
            test_command=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
        )

        res: TaskResult = runtime.execute_autonomous_task(request)

        # 1. Authoritative TaskResult
        assert res.success is True
        assert res.verified is True
        assert res.status == "COMMITTED"
        assert res.proof_path is not None
        assert res.proof_markdown_path is not None

        # 2. Durable Artifact Inspection
        proof_store = FileProofStore(phase4_repo)
        artifact: ProofArtifact = proof_store.load(task_id)
        md_report = proof_store.load_markdown(task_id)

        assert artifact.version == "1.0.0"
        assert artifact.task.task_id == task_id
        assert artifact.task.description == "Fix mobile navbar toggle"
        assert artifact.verdict.status == "VERIFIED"
        assert artifact.verification.gate_checks.get("workspace_scope") is True
        assert artifact.verification.gate_checks.get("test_suite") is True
        assert artifact.verification.gate_checks.get("transaction_integrity") is True
        assert artifact.verification.exit_code == 0

        # Changes
        assert artifact.changes.status == "COMMITTED"
        assert len(artifact.changes.files) >= 1
        navbar_change = next((f for f in artifact.changes.files if f.path == "navbar.py"), None)
        assert navbar_change is not None
        assert navbar_change.operation == "MODIFY"
        assert navbar_change.after_sha256 is not None

        # Markdown report
        assert "# BrainFrog Verification Proof" in md_report
        assert "Verdict: `VERIFIED`" in md_report or "**Verdict:** `VERIFIED`" in md_report
        assert "navbar.py" in md_report

    def test_proof_generated_on_failed_test_with_rollback(self, phase4_repo: Path):
        """Verify proof artifact reflects FAILED, ROLLED_BACK, and clean disk restoration."""
        navbar_file = phase4_repo / "navbar.py"
        original_navbar = navbar_file.read_text(encoding="utf-8")

        mock_s1 = MagicMock()
        mock_s1.name = "jev_mock"
        mock_s1.decide.side_effect = [
            # Scope gate
            {
                "likely_domain": Answer(choice="ui", confidence=0.9),
                "change_type": Answer(choice="code_change", confidence=0.9),
                "is_sensitive": Answer(noul=0.0),
                "complexity": Answer(score="low", confidence=0.9),
                "needs_tests": Answer(noul=0.8),
            },
            # Post-test evaluation: tests failed
            {
                "tests_passing": Answer(choice="no", noul=0.0),
                "diff_complete": Answer(choice="no", noul=0.0),
                "failure_fixable": Answer(choice="no", noul=0.0),
                "retry_concern": Answer(score="exceeded_reasonable_limit", confidence=0.9),
            },
        ]

        mock_s2 = MagicMock()
        mock_s2.provider_name = "claude_mock"
        mock_s2.plan_task.return_value = [
            PlanStep(id="1", description="Attempt bad fix", files=["navbar.py"])
        ]
        # Mutation that still fails tests
        mock_s2.write_code.return_value = {"navbar.py": "# Bad fix\nclass MobileNavbar: pass\n"}

        runtime = BrainFrogRuntime(
            repo_dir=phase4_repo,
            persist_sessions=False,
            default_test_cmd=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
            system1_factory=lambda b: mock_s1,
            system2_factory=lambda **k: mock_s2,
        )

        task_id = "task_p4_failure_002"
        request = TaskRequest(
            task_id=task_id,
            user_request="Attempt failing fix",
            workspace=phase4_repo,
            test_command=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
            max_retries=1,
        )

        res: TaskResult = runtime.execute_autonomous_task(request)

        assert res.success is False
        assert res.verified is False
        assert res.status == "ROLLED_BACK"

        # Check disk was restored
        assert navbar_file.read_text(encoding="utf-8") == original_navbar

        # Check proof artifact
        proof_store = FileProofStore(phase4_repo)
        artifact = proof_store.load(task_id)
        assert artifact.verdict.status == "FAILED"
        assert artifact.changes.status == "ROLLED_BACK"

    def test_proof_records_cancellation(self, phase4_repo: Path):
        """Verify proof artifact records CANCELLED on user interrupt."""
        runtime = BrainFrogRuntime(repo_dir=phase4_repo, persist_sessions=False)

        task_id = "task_p4_cancel_003"
        request = TaskRequest(
            task_id=task_id,
            user_request="Task to be cancelled",
            workspace=phase4_repo,
        )

        with patch.object(runtime, "handle_message", side_effect=KeyboardInterrupt):
            res: TaskResult = runtime.execute_autonomous_task(request)

        assert res.success is False
        assert res.verified is False
        assert res.status == "CANCELLED"

        proof_store = FileProofStore(phase4_repo)
        artifact = proof_store.load(task_id)
        assert artifact.verdict.status == "CANCELLED"
        assert artifact.changes.status == "ROLLED_BACK"

    def test_proof_records_recovery_required(self, phase4_repo: Path):
        """Verify proof records RECOVERY_REQUIRED if rollback fails."""
        verdict = compute_proof_verdict(
            tx_status="failed",
            tests_passed=False,
            gate_passed=False,
            is_recovery_required=True,
            rollback_error="Disk full during rollback restoration",
        )
        assert verdict.status == "RECOVERY_REQUIRED"
        assert "Disk full" in verdict.reason

    def test_proof_atomic_write_crash_resilience(self, tmp_path: Path):
        """Verify temporary files are used and no partial JSON is created on crash."""
        store = FileProofStore(tmp_path)
        task_ev = TaskEvidence(
            task_id="task_atomic_001",
            session_id="s1",
            description="Atomic write test",
            created_at="2026-10-09T00:00:00Z",
            completed_at="2026-10-09T00:00:01Z",
            duration_ms=1000,
        )
        artifact = ProofArtifact(
            task=task_ev,
            verdict=ProofVerdict(status="VERIFIED", reason="OK"),
            provenance=None,
            verification=VerificationEvidence("pytest", 0, "ok", ""),
            changes=ChangesEvidence("tx_1", "COMMITTED", []),
            reproducibility=ReproducibilityEvidence("pytest", 0),
        )

        # Mock an error during write
        with patch("os.replace", side_effect=OSError("Disk failure")):
            with pytest.raises(IOError):
                store.save(artifact)

        # The final target JSON must NOT exist
        assert not store.proof_json_path("task_atomic_001").exists()

    def test_duplicate_task_id_cannot_overwrite_proof(self, tmp_path: Path):
        """Duplicate task_id must fail-closed with ProofAlreadyExistsError."""
        store = FileProofStore(tmp_path)
        task_ev = TaskEvidence(
            task_id="task_dup_001",
            session_id="s1",
            description="First write",
            created_at="2026-10-09T00:00:00Z",
            completed_at="2026-10-09T00:00:01Z",
            duration_ms=1000,
        )
        artifact = ProofArtifact(
            task=task_ev,
            verdict=ProofVerdict(status="VERIFIED", reason="OK"),
            provenance=None,
            verification=VerificationEvidence("pytest", 0, "ok", ""),
            changes=ChangesEvidence("tx_1", "COMMITTED", []),
            reproducibility=ReproducibilityEvidence("pytest", 0),
        )

        # First save succeeds
        store.save(artifact)
        assert store.proof_json_path("task_dup_001").exists()

        # Second save with same task_id raises ProofAlreadyExistsError
        with pytest.raises(ProofAlreadyExistsError):
            store.save(artifact, allow_overwrite=False)

        # Internal test overwrite succeeds when explicit
        store.save(artifact, allow_overwrite=True)

    def test_secret_redaction_in_proof(self):
        """Verify API keys, passwords, and tokens are scrubbed before persistence."""
        dirty_output = (
            "Running tests...\n"
            "Connecting to https://api.service.com with Bearer eyJhbGciOiJIUzI1NiJ9.test\n"
            "Using token ghp_123456789012345678901234567890123456\n"
            "Google key AIzaSyD1234567890123456789012345678901\n"
            "OpenAI sk-abcdefghijklmnopqrstuvwxyz123456\n"
            "Config: password=supersecretpassword and api_key=topsecretkey\n"
        )

        clean, _ = capture_bounded_output(dirty_output)

        assert "ghp_123456789012345678901234567890123456" not in clean
        assert "AIzaSyD1234567890123456789012345678901" not in clean
        assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in clean
        assert "supersecretpassword" not in clean
        assert "topsecretkey" not in clean
        assert "[REDACTED_SECRET]" in clean

    def test_sensitive_file_content_never_persisted(self):
        """Sensitive filenames are recognized to prohibit content leakage."""
        assert is_sensitive_path(".env") is True
        assert is_sensitive_path(".env.production") is True
        assert is_sensitive_path("id_rsa") is True
        assert is_sensitive_path("certs/server.pem") is True
        assert is_sensitive_path("config/secrets.json") is True
        assert is_sensitive_path("core/runtime/workflow.py") is False

    def test_proof_non_git_workspace(self, non_git_repo: Path):
        """Proof generation succeeds cleanly in workspaces with no Git repository."""
        prov = capture_git_provenance(non_git_repo)
        assert prov is None

        store = FileProofStore(non_git_repo)
        task_ev = TaskEvidence(
            task_id="task_nongit_001",
            session_id="s1",
            description="Non git task",
            created_at="2026-10-09T00:00:00Z",
            completed_at="2026-10-09T00:00:01Z",
            duration_ms=1000,
        )
        artifact = ProofArtifact(
            task=task_ev,
            verdict=ProofVerdict(status="VERIFIED", reason="OK"),
            provenance=None,
            verification=VerificationEvidence("pytest", 0, "ok", ""),
            changes=ChangesEvidence("tx_1", "COMMITTED", []),
            reproducibility=ReproducibilityEvidence("pytest", 0),
        )

        store.save(artifact)
        loaded = store.load("task_nongit_001")
        assert loaded.provenance is None
        assert loaded.to_dict()["provenance"] is None

    def test_git_provenance_captured(self, phase4_repo: Path):
        """Git repository metadata is captured when available."""
        prov = capture_git_provenance(phase4_repo)
        assert prov is not None
        assert prov.is_git is True
        assert prov.initial_head_sha is not None
        assert len(prov.initial_head_sha) == 40

    def test_anti_tampering_llm_cannot_set_verdict(self):
        """Even if model claims VERIFIED, runtime computes FAILED if tests exit non-zero."""
        # Simulated scenario: tests failed (exit 1), tx rolled back
        verdict = compute_proof_verdict(
            tx_status="rolled_back",
            tests_passed=False,
            gate_passed=False,
            process_status="non_zero_exit",
        )
        assert verdict.status == "FAILED"
        assert verdict.status != "VERIFIED"

    def test_proof_paths_are_workspace_relative(self, phase4_repo: Path):
        """All persisted file paths must be strictly workspace-relative."""
        rel = normalize_workspace_relative_path(phase4_repo / "navbar.py", phase4_repo)
        assert rel == "navbar.py"
        assert "\\" not in rel
        assert not rel.startswith("/")
        assert ":" not in rel

        # Absolute host path outside workspace must fail closed
        with pytest.raises(ValueError):
            normalize_workspace_relative_path(Path("C:/Windows/System32/calc.exe"), phase4_repo)

    def test_task_id_correlates_transaction_and_proof(self, phase4_repo: Path):
        """Task ID propagates into Transaction and ProofArtifact."""
        task_id = "task_correlation_001"
        coord = TransactionCoordinator(
            workspace=phase4_repo,
            task_id=task_id,
        )
        assert coord.tx.task_id == task_id
        serialized = coord.tx.to_dict()
        assert serialized["task_id"] == task_id

        # Backward compatibility for legacy transaction without task_id
        legacy_data = dict(serialized)
        del legacy_data["task_id"]
        tx_restored = Transaction.from_dict(legacy_data)
        assert tx_restored.task_id is None

    def test_process_duration_and_correlation_recorded(self, phase4_repo: Path):
        """Hardened subprocess captures started_at, completed_at, and duration_ms."""
        from core.runtime.process import run_hardened_subprocess

        res = run_hardened_subprocess(
            [sys.executable, "-c", "import time; time.sleep(0.05); print('hello')"],
            cwd=phase4_repo,
            timeout=5.0,
        )

        assert res.returncode == 0
        assert res.started_at is not None
        assert res.completed_at is not None
        assert res.duration_ms is not None
        assert res.duration_ms >= 40  # at least 40 ms

    # =========================================================================
    # 2. Section 21: Adversarial Tests
    # =========================================================================

    def test_adversarial_llm_claims_success_while_tests_fail(self):
        """Model output claiming success cannot override non-zero exit code."""
        verdict = compute_proof_verdict(
            tx_status="rolled_back",
            tests_passed=False,
            gate_passed=False,
            process_status=ProcessExitStatus.NON_ZERO_EXIT,
        )
        assert verdict.status == "FAILED"

    def test_adversarial_tx_committed_while_gate_fails(self):
        """Transaction status COMMITTED but gate checks fail -> verdict FAILED."""
        gate_details = {
            "workspace_scope": False,  # scope violated!
            "test_suite": True,
            "transaction_integrity": True,
            "execution_integrity": True,
            "trust_integrity": True,
        }
        verdict = compute_proof_verdict(
            tx_status="committed",
            tests_passed=True,
            gate_passed=False,
            gate_details=gate_details,
        )
        assert verdict.status == "FAILED"
        assert "workspace_scope" in verdict.reason

    def test_adversarial_test_passes_but_scope_check_fails(self):
        """Tests pass exit 0, but out-of-scope mutation detected -> verdict FAILED."""
        gate_details = {
            "workspace_scope": False,
            "test_suite": True,
            "transaction_integrity": True,
            "execution_integrity": True,
            "trust_integrity": True,
        }
        verdict = compute_proof_verdict(
            tx_status="committed",
            tests_passed=True,
            gate_passed=False,
            gate_details=gate_details,
        )
        assert verdict.status == "FAILED"

    def test_adversarial_rollback_fails(self):
        """Rollback failure must result in RECOVERY_REQUIRED, never VERIFIED or FAILED."""
        verdict = compute_proof_verdict(
            tx_status="failed",
            tests_passed=False,
            gate_passed=False,
            rollback_error="PermissionDenied on restore",
            is_recovery_required=True,
        )
        assert verdict.status == "RECOVERY_REQUIRED"

    def test_adversarial_proof_write_fails_for_verified_task_fails_closed(self, phase4_repo: Path):
        """If proof file persistence raises an exception, task must fail closed."""
        runtime = BrainFrogRuntime(repo_dir=phase4_repo, persist_sessions=False)
        request = TaskRequest(
            task_id="task_write_fail_001",
            user_request="Test write failure",
            workspace=phase4_repo,
        )

        mock_test_proc = HardenedProcessResult(args=["pytest"], returncode=0, stdout="1 passed")
        with patch.object(FileProofStore, "save", side_effect=IOError("Permission denied")):
            with patch.object(
                runtime,
                "handle_message",
                return_value=MagicMock(
                    success=True,
                    status="completed",
                    text="Done",
                    error=None,
                    metadata={
                        "transaction_status": "committed",
                        "transaction_success": True,
                        "test_result": mock_test_proc,
                    },
                ),
            ):
                with patch.object(VerificationGate, "verify", return_value=(True, {"test_suite": True}, [])):
                    res: TaskResult = runtime.execute_autonomous_task(request)

        # Invariant: Must fail closed! Cannot report success or verified if proof write failed.
        assert res.success is False
        assert res.verified is False
        assert res.status == "FAILED"
        assert "proof persistence" in (res.failure_reason or "").lower()

    def test_adversarial_malicious_secret_in_stdout(self):
        """Malicious bearer tokens and secrets embedded in stdout are redacted."""
        stdout = "Dumping test env: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.dummy and sk-123456789012345678901234"
        clean, _ = capture_bounded_output(stdout)
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in clean
        assert "sk-123456789012345678901234" not in clean
        assert "[REDACTED_SECRET]" in clean

    def test_adversarial_secret_in_stderr(self):
        """Credentials in stderr are redacted."""
        stderr = "Database connection failed with password=super_secret_db_pass; host=localhost"
        clean, _ = capture_bounded_output(stderr)
        assert "super_secret_db_pass" not in clean
        assert "[REDACTED_SECRET]" in clean

    def test_adversarial_path_traversal_rejected(self, phase4_repo: Path):
        """Path traversal '../secret.txt' is rejected."""
        with pytest.raises(ValueError):
            normalize_workspace_relative_path("../secret.txt", phase4_repo)

    def test_adversarial_arbitrary_verdict_string_rejected(self):
        """Invalid verdict strings raise ValueError."""
        with pytest.raises(ValueError):
            ProofVerdict(status="PARTIALLY_VERIFIED", reason="invalid")

    def test_adversarial_empty_task_id_rejected(self):
        """Empty task_id in TaskEvidence raises ValueError."""
        with pytest.raises(ValueError):
            TaskEvidence(
                task_id="",
                session_id="s1",
                description="desc",
                created_at="2026-10-09T00:00:00Z",
                completed_at="2026-10-09T00:00:01Z",
                duration_ms=1000,
            )
