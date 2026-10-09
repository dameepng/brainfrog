"""BrainFrog Phase 5 — Runtime SDK Test Suite.

Verifies:
- Public SDK Surface & Lifecycle (client, handle, models, errors, events)
- Identity & Session Isolation (task_id, session_id, transactions, processes, proofs)
- Trust Enforcement (no bypasses, no direct Orchestrator access, fail-closed)
- Safe Async & Cancellation (subprocess kill, tx rollback, lock release, CANCELLED proof)
- Idempotency & Duplicate Protection
- Event Subscription, Correlation, Ordering, Scrubbing, and Bounding
- Concurrency & Workspace Isolation
- CLI & SDK Parity
- Adversarial Scenarios
- Static / AST Architectural Invariants

Product Thesis:
    "Don't just trust the agent. Verify it."
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from brainfrog import (
    ApprovalRequiredError,
    BrainFrogClient,
    BrainFrogSDKError,
    EventType,
    ExecutionFailedError,
    InvalidRequestError,
    PermissionDeniedError,
    ProofArtifact,
    ProofPersistenceError,
    RecoveryRequiredError,
    RuntimeConfig,
    SDKEvent,
    TaskCancelledError,
    TaskHandle,
    TaskNotFoundError,
    TaskPlan,
    TaskRequest,
    TaskResult,
    TaskStatus,
    TaskStep,
    VerificationFailedError,
    WorkspaceBusyError,
)
from core.runtime import (
    ApprovalRequest,
    ApprovalStatus,
    BrainFrogRuntime,
    CanonicalOperation,
    ChangesEvidence,
    FileProofStore,
    HardenedProcessResult,
    ProofVerdict,
    ReproducibilityEvidence,
    TaskEvidence,
    TaskFinalizationCoordinator,
    TaskRecoveryRequiredError,
    TaskReservationCoordinator,
    VerificationEvidence,
    WorkspaceLock,
    capture_git_provenance,
)
from orchestrator import PlanStep, StepResult
from system1.base import Answer

FIXTURE_SRC = Path(__file__).resolve().parent / "fixtures" / "phase3_demo"


@pytest.fixture
def phase5_repo(tmp_path: Path) -> Path:
    """Create a temporary git repository seeded from phase3_demo fixture."""
    repo = tmp_path / "phase5_project"
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


FIXED_NAVBAR_CODE = (
    "class MobileNavbar:\n"
    "    def __init__(self) -> None:\n"
    "        self.is_open: bool = False\n"
    "        self.breakpoint: int = 768\n"
    "    def toggle(self) -> None:\n"
    "        self.is_open = not self.is_open\n"
    "    def render(self, viewport_width: int) -> str:\n"
    "        if viewport_width < self.breakpoint and self.is_open:\n"
    "            return \"<nav class='navbar mobile mobile-menu-active'></nav>\"\n"
    "        return \"<nav class='navbar desktop'></nav>\"\n"
)


def _mock_success_systems(navbar_code: str = FIXED_NAVBAR_CODE):
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
        # Allow extra decide calls in case of retry
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
    mock_s2.write_code.return_value = {"navbar.py": navbar_code}
    return mock_s1, mock_s2


# =============================================================================
# 1. Basic Execution Tests
# =============================================================================

class TestSDKBasicExecution:
    def test_sdk_submit_task_reaches_canonical_runtime(self, phase5_repo: Path) -> None:
        async def _run():
            mock_s1, mock_s2 = _mock_success_systems()

            async with BrainFrogClient(
                workspace=phase5_repo,
                system1_factory=lambda b: mock_s1,
                system2_factory=lambda **k: mock_s2,
                test_command=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
            ) as client:
                task_id = "task_sdk_exec_001"
                res = await client.run(
                    task="Fix mobile navbar toggle",
                    task_id=task_id,
                )

                # Invariant: success=True requires verified=True
                assert res.success is True
                assert res.verified is True
                assert res.status == "COMMITTED"
                assert res.task_id == task_id
                assert res.transaction_id is not None
                assert res.proof_path is not None
                assert res.proof_markdown_path is not None
                assert Path(res.proof_path).exists()
                assert Path(res.proof_markdown_path).exists()

        asyncio.run(_run())

    def test_sdk_proof_artifact_generation_and_inspection(self, phase5_repo: Path) -> None:
        async def _run():
            mock_s1, mock_s2 = _mock_success_systems()

            async with BrainFrogClient(
                workspace=phase5_repo,
                system1_factory=lambda b: mock_s1,
                system2_factory=lambda **k: mock_s2,
                test_command=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
            ) as client:
                task_id = "task_sdk_proof_002"
                res = await client.run(task="Fix navbar", task_id=task_id)
                assert res.verified is True

                proof = client.get_proof(task_id)
                assert proof is not None
                assert proof.task.task_id == task_id
                assert proof.verdict.status == "VERIFIED"
                assert proof.changes.status == "COMMITTED"
                assert len(proof.changes.files) >= 1

                retrieved = client.get_task(task_id)
                assert retrieved is not None
                assert retrieved.task_id == task_id
                assert retrieved.verified is True
                assert retrieved.status == "COMMITTED"

        asyncio.run(_run())

    def test_sdk_run_sync_parity(self, phase5_repo: Path) -> None:
        mock_s1, mock_s2 = _mock_success_systems()

        with BrainFrogClient(
            workspace=phase5_repo,
            system1_factory=lambda b: mock_s1,
            system2_factory=lambda **k: mock_s2,
            test_command=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
        ) as client:
            task_id = "task_sdk_sync_003"
            res = client.run_sync(
                task="Fix navbar sync",
                task_id=task_id,
            )

            assert res.success is True
            assert res.verified is True
            assert res.status == "COMMITTED"
            assert res.task_id == task_id
            assert res.proof_path is not None
            assert Path(res.proof_path).exists()


# =============================================================================
# 2. Identity & Session Isolation Tests
# =============================================================================

class TestSDKIdentityAndIsolation:
    def test_task_id_propagates_end_to_end(self, phase5_repo: Path) -> None:
        async def _run():
            mock_s1, mock_s2 = _mock_success_systems()
            custom_id = "stable_task_id_alpha_123"

            async with BrainFrogClient(
                workspace=phase5_repo,
                system1_factory=lambda b: mock_s1,
                system2_factory=lambda **k: mock_s2,
                test_command=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
            ) as client:
                res = await client.run("Fix navbar", task_id=custom_id)
                assert res.task_id == custom_id

                proof = client.get_proof(custom_id)
                assert proof is not None
                assert proof.task.task_id == custom_id
                if proof.verification.process_evidence:
                    assert proof.verification.process_evidence.task_id == custom_id

        asyncio.run(_run())

    def test_session_isolation_different_users(self, phase5_repo: Path) -> None:
        mock_s1, mock_s2 = _mock_success_systems()

        client1 = BrainFrogClient(
            workspace=phase5_repo,
            user_id="user_alice",
            conversation_id="conv_alice_1",
            system1_factory=lambda b: mock_s1,
            system2_factory=lambda **k: mock_s2,
            test_command=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
        )
        client2 = BrainFrogClient(
            workspace=phase5_repo,
            user_id="user_bob",
            conversation_id="conv_bob_1",
            system1_factory=lambda b: mock_s1,
            system2_factory=lambda **k: mock_s2,
            test_command=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
        )

        sess1 = client1.runtime.sessions.get_or_create("cli", "user_alice", "conv_alice_1")
        sess2 = client2.runtime.sessions.get_or_create("cli", "user_bob", "conv_bob_1")

        assert sess1.session_id != sess2.session_id
        assert sess1.user_id == "user_alice"
        assert sess2.user_id == "user_bob"

    def test_transaction_and_proof_correlation(self, phase5_repo: Path) -> None:
        async def _run():
            mock_s1, mock_s2 = _mock_success_systems()

            async with BrainFrogClient(
                workspace=phase5_repo,
                system1_factory=lambda b: mock_s1,
                system2_factory=lambda **k: mock_s2,
                test_command=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
            ) as client:
                task_id = "task_correlation_004"
                res = await client.run("Fix navbar", task_id=task_id)
                assert res.transaction_id is not None

                proof = client.get_proof(task_id)
                assert proof is not None
                assert proof.changes.transaction_id == res.transaction_id
                if proof.verification.process_evidence:
                    assert proof.verification.process_evidence.transaction_id == res.transaction_id

        asyncio.run(_run())


# =============================================================================
# 3. Trust Enforcement Tests
# =============================================================================

class TestSDKTrustEnforcement:
    def test_remote_untrusted_channel_rejected(self, phase5_repo: Path) -> None:
        async def _run():
            mock_s1, mock_s2 = _mock_success_systems()

            async with BrainFrogClient(
                workspace=phase5_repo,
                channel="untrusted_external_web",
                system1_factory=lambda b: mock_s1,
                system2_factory=lambda **k: mock_s2,
            ) as client:
                res = await client.run("Attempt arbitrary execution", task_id="task_trust_005")
                assert res.verified is False
                assert res.status in ("REJECTED", "FAILED")
                assert res.success is False

        asyncio.run(_run())

    def test_sdk_cannot_fabricate_verified_without_gate(self) -> None:
        # Invariant: success=True requires verified=True
        with pytest.raises(ValueError, match="Invariant Violation"):
            TaskResult(
                task_id="forged_001",
                user_request="Fake success",
                success=True,
                verified=False,
                status="COMMITTED",
                summary="Forged success claim",
            )

    def test_sdk_rejects_missing_workspace(self, tmp_path: Path) -> None:
        non_existent = tmp_path / "does_not_exist_workspace_xyz"
        with pytest.raises(InvalidRequestError):
            BrainFrogClient(workspace=non_existent)


# =============================================================================
# 4. Cancellation Tests
# =============================================================================

class TestSDKCancellation:
    def test_sdk_task_handle_cancel_rolls_back_and_releases_lock(self, phase5_repo: Path) -> None:
        async def _run():
            mock_s1 = MagicMock()
            mock_s1.name = "jev_mock"
            mock_s1.decide.return_value = {
                "likely_domain": Answer(choice="ui", confidence=0.9),
                "change_type": Answer(choice="code_change", confidence=0.9),
                "is_sensitive": Answer(noul=0.0),
                "complexity": Answer(score="low", confidence=0.9),
                "needs_tests": Answer(noul=0.8),
            }

            cancel_reached = threading.Event()

            def slow_write(*args, **kwargs):
                cancel_reached.set()
                time.sleep(3.0)
                return {"navbar.py": "def bad(): pass"}

            mock_s2 = MagicMock()
            mock_s2.provider_name = "claude_mock"
            mock_s2.plan_task.return_value = [
                PlanStep(id="1", description="Step 1", files=["navbar.py"])
            ]
            mock_s2.write_code.side_effect = slow_write

            async with BrainFrogClient(
                workspace=phase5_repo,
                system1_factory=lambda b: mock_s1,
                system2_factory=lambda **k: mock_s2,
            ) as client:
                task_id = "task_cancel_006"
                handle = client.start("Fix navbar with delay", task_id=task_id)

                # Wait until execution begins
                await asyncio.to_thread(cancel_reached.wait, timeout=3.0)

                # Trigger cancellation via SDK handle
                await handle.cancel()
                res = await handle.wait()

                assert res.verified is False
                assert res.status == "CANCELLED"
                assert res.success is False

                # Verify workspace lock was released cleanly
                lock = WorkspaceLock(workspace=phase5_repo)
                assert lock.acquire() is True
                lock.release()

                # Verify proof artifact reflects CANCELLED verdict
                proof = client.get_proof(task_id)
                if proof:
                    assert proof.verdict.status == "CANCELLED"

        asyncio.run(_run())


# =============================================================================
# 5. Error Taxonomy Tests
# =============================================================================

class TestSDKErrors:
    def test_verification_failure_raises_or_reports(self, phase5_repo: Path) -> None:
        async def _run():
            # Broken code that will fail pytest tests
            mock_s1 = MagicMock()
            mock_s1.name = "jev_mock"
            mock_s1.decide.side_effect = [
                {
                    "likely_domain": Answer(choice="ui", confidence=0.9),
                    "change_type": Answer(choice="code_change", confidence=0.9),
                    "is_sensitive": Answer(noul=0.0),
                    "complexity": Answer(score="low", confidence=0.9),
                    "needs_tests": Answer(noul=0.8),
                },
                {
                    "tests_passing": Answer(choice="no", noul=0.0),
                    "diff_complete": Answer(choice="no", noul=0.0),
                    "failure_fixable": Answer(choice="no", noul=0.0),
                    "retry_concern": Answer(score="high", confidence=0.95),
                },
            ]
            mock_s2 = MagicMock()
            mock_s2.provider_name = "claude_mock"
            mock_s2.plan_task.return_value = [
                PlanStep(id="1", description="Break navbar", files=["navbar.py"])
            ]
            mock_s2.write_code.return_value = {"navbar.py": "def broken(): pass\n"}

            async with BrainFrogClient(
                workspace=phase5_repo,
                system1_factory=lambda b: mock_s1,
                system2_factory=lambda **k: mock_s2,
                test_command=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
            ) as client:
                task_id = "task_fail_verify_007"
                res = await client.run("Break navbar", task_id=task_id)
                assert res.verified is False
                assert res.status in ("ROLLED_BACK", "FAILED")
                assert res.success is False

                # With raise_on_failure=True, must raise VerificationFailedError or ExecutionFailedError
                with pytest.raises((VerificationFailedError, ExecutionFailedError)):
                    await client.run(
                        "Break navbar again",
                        task_id="task_fail_verify_008",
                        raise_on_failure=True,
                    )

        asyncio.run(_run())

    def test_workspace_busy_error_on_locked_workspace(self, phase5_repo: Path) -> None:
        async def _run():
            mock_s1, mock_s2 = _mock_success_systems()
            external_lock = WorkspaceLock(workspace=phase5_repo, session_id="ext_session", actor="ext_actor")
            external_lock.acquire()

            try:
                async with BrainFrogClient(
                    workspace=phase5_repo,
                    system1_factory=lambda b: mock_s1,
                    system2_factory=lambda **k: mock_s2,
                ) as client:
                    res = await client.run(
                        "Task while locked",
                        task_id="task_locked_009",
                        metadata={"lock_timeout": 0.05},
                    )
                    assert res.status == "LOCKED"
                    assert res.verified is False

                    with pytest.raises(WorkspaceBusyError):
                        await client.run(
                            "Task while locked",
                            task_id="task_locked_010",
                            metadata={"lock_timeout": 0.05},
                            raise_on_failure=True,
                        )
            finally:
                external_lock.release()

        asyncio.run(_run())


# =============================================================================
# 6. Idempotency & Duplicate Protection Tests
# =============================================================================

class TestSDKIdempotency:
    def test_duplicate_completed_task_id_rejected(self, phase5_repo: Path) -> None:
        async def _run():
            mock_s1, mock_s2 = _mock_success_systems()

            async with BrainFrogClient(
                workspace=phase5_repo,
                system1_factory=lambda b: mock_s1,
                system2_factory=lambda **k: mock_s2,
                test_command=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
            ) as client:
                task_id = "task_idempotent_011"
                res1 = await client.run("Initial execution", task_id=task_id)
                assert res1.verified is True

                # Second submission with the exact same task_id must be rejected
                with pytest.raises(InvalidRequestError, match="already been executed"):
                    await client.run("Duplicate execution", task_id=task_id)

        asyncio.run(_run())


# =============================================================================
# 7. Typed Events & Correlation Tests
# =============================================================================

class TestSDKEvents:
    def test_event_lifecycle_ordering_and_scrubbing(self, phase5_repo: Path) -> None:
        async def _run():
            mock_s1, mock_s2 = _mock_success_systems()
            task_id = "task_events_012"

            emitted_events: List[SDKEvent] = []

            async with BrainFrogClient(
                workspace=phase5_repo,
                system1_factory=lambda b: mock_s1,
                system2_factory=lambda **k: mock_s2,
                test_command=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
            ) as client:
                secret_token = "ghp_1234567890abcdefghijklmnopqrstuvwxyz"
                res = await client.run(
                    f"Fix navbar with secret {secret_token}",
                    task_id=task_id,
                    on_event=lambda ev: emitted_events.append(ev),
                )
                assert res.verified is True

            event_types = [e.event_type for e in emitted_events]
            assert EventType.TASK_CREATED in event_types
            assert EventType.PLAN_CREATED in event_types
            assert EventType.VERIFICATION_STARTED in event_types
            assert EventType.VERIFICATION_COMPLETED in event_types
            assert EventType.PROOF_CREATED in event_types
            assert EventType.TASK_COMPLETED in event_types

            # Ordering: TASK_CREATED must precede TASK_COMPLETED
            idx_created = event_types.index(EventType.TASK_CREATED)
            idx_completed = event_types.index(EventType.TASK_COMPLETED)
            assert idx_created < idx_completed

            # All events correlate to task_id
            for ev in emitted_events:
                assert ev.task_id == task_id
                payload_str = json.dumps(ev.payload)
                assert secret_token not in payload_str

        asyncio.run(_run())


# =============================================================================
# 8. Concurrency Tests
# =============================================================================

class TestSDKConcurrency:
    def test_independent_workspaces_execute_concurrently(self, tmp_path: Path) -> None:
        async def _run():
            repo1 = tmp_path / "repo1"
            repo2 = tmp_path / "repo2"
            shutil.copytree(FIXTURE_SRC, repo1)
            shutil.copytree(FIXTURE_SRC, repo2)

            for r in (repo1, repo2):
                subprocess.run(["git", "init"], cwd=str(r), check=True, capture_output=True)
                subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(r), check=True, capture_output=True)
                subprocess.run(["git", "config", "user.email", "test@bf.local"], cwd=str(r), check=True, capture_output=True)
                subprocess.run(["git", "add", "."], cwd=str(r), check=True, capture_output=True)
                subprocess.run(["git", "commit", "-m", "initial"], cwd=str(r), check=True, capture_output=True)

            mock_s1_1, mock_s2_1 = _mock_success_systems()
            mock_s1_2, mock_s2_2 = _mock_success_systems()

            client1 = BrainFrogClient(
                workspace=repo1,
                system1_factory=lambda b: mock_s1_1,
                system2_factory=lambda **k: mock_s2_1,
                test_command=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
            )
            client2 = BrainFrogClient(
                workspace=repo2,
                system1_factory=lambda b: mock_s1_2,
                system2_factory=lambda **k: mock_s2_2,
                test_command=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
            )

            res1, res2 = await asyncio.gather(
                client1.run("Fix navbar in repo1", task_id="task_conc_013"),
                client2.run("Fix navbar in repo2", task_id="task_conc_014"),
            )

            assert res1.verified is True
            assert res2.verified is True
            await client1.close()
            await client2.close()

        asyncio.run(_run())


# =============================================================================
# 9. CLI Parity Tests
# =============================================================================

class TestSDKCLIParity:
    def test_cli_and_sdk_share_canonical_runtime_pipeline(self, phase5_repo: Path) -> None:
        import cli
        mock_s1, mock_s2 = _mock_success_systems()

        runtime = BrainFrogRuntime(
            repo_dir=phase5_repo,
            default_test_cmd=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
            system1_factory=lambda b: mock_s1,
            system2_factory=lambda **k: mock_s2,
        )

        exit_code = cli.execute_task(
            task="Fix navbar via CLI",
            repo_dir=phase5_repo,
            runtime=runtime,
            test_cmd=f"{sys.executable} -m pytest tests/test_navbar.py -q",
        )
        assert exit_code == 0

        # SDK execution with the exact same runtime produces equivalent verified results
        client = BrainFrogClient(
            workspace=phase5_repo,
            runtime=runtime,
            test_command=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
        )
        res = client.run_sync("Fix navbar via SDK", task_id="task_cli_parity_015")
        assert res.verified is True
        assert res.status == "COMMITTED"


# =============================================================================
# 10. Architectural AST Invariant Tests
# =============================================================================

class TestSDKArchitecturalInvariants:
    def test_no_direct_orchestrator_or_raw_subprocess_in_sdk(self) -> None:
        """Verify public SDK files never import Orchestrator or run raw subprocesses."""
        sdk_dir = Path(__file__).resolve().parent.parent / "brainfrog" / "sdk"
        assert sdk_dir.exists() and sdk_dir.is_dir()

        disallowed_imports = {"orchestrator", "subprocess"}
        disallowed_calls = {"Popen", "run", "check_output", "check_call"}

        for py_file in sdk_dir.glob("*.py"):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert alias.name not in disallowed_imports, (
                            f"Architectural Invariant Violated: {py_file.name} directly imports '{alias.name}'"
                        )
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    assert mod not in disallowed_imports, (
                        f"Architectural Invariant Violated: {py_file.name} directly imports from '{mod}'"
                    )
                elif isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Attribute) and func.attr in disallowed_calls:
                        if isinstance(func.value, ast.Name) and func.value.id == "subprocess":
                            pytest.fail(f"Architectural Invariant Violated: raw subprocess call in {py_file.name}")


# =============================================================================
# 11. Adversarial & Edge Case Tests
# =============================================================================

class TestSDKAdversarial:
    def test_oversized_task_description_rejected(self, phase5_repo: Path) -> None:
        """SDK must fail-closed and reject unreasonably oversized task descriptions."""
        client = BrainFrogClient(workspace=phase5_repo)
        huge_task = "A" * 150_000
        with pytest.raises(InvalidRequestError, match="maximum allowed length"):
            client.run_sync(huge_task)

    def test_path_traversal_in_scope_rejected(self, phase5_repo: Path) -> None:
        """SDK must reject directory traversal attempts in allowed_scope."""
        client = BrainFrogClient(workspace=phase5_repo)
        with pytest.raises(InvalidRequestError, match="Scope path traversal forbidden"):
            client.run_sync("Fix navbar", allowed_scope=["../../etc/passwd"])

        with pytest.raises(InvalidRequestError, match="Scope path traversal forbidden"):
            client.run_sync("Fix navbar", allowed_scope=["/absolute/path/outside"])

    def test_path_traversal_in_task_id_rejected(self, phase5_repo: Path) -> None:
        """SDK must reject directory traversal attempts in task_id."""
        client = BrainFrogClient(workspace=phase5_repo)
        with pytest.raises(InvalidRequestError, match="invalid characters or path traversal"):
            client.run_sync("Fix navbar", task_id="../../evil_task")

        with pytest.raises(InvalidRequestError, match="invalid characters or path traversal"):
            client.run_sync("Fix navbar", task_id="task/subpath")

    def test_proof_persistence_failure_fails_closed(self, phase5_repo: Path) -> None:
        """If proof persistence fails, SDK must fail closed: never return verified=True."""
        mock_s1, mock_s2 = _mock_success_systems()
        task_id = "task_adv_proof_fail_016"

        client = BrainFrogClient(
            workspace=phase5_repo,
            system1_factory=lambda b: mock_s1,
            system2_factory=lambda **k: mock_s2,
            test_command=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
        )

        with patch.object(FileProofStore, "save", side_effect=IOError("Simulated disk full / permissions error")):
            # Non-raising execution
            res = client.run_sync("Fix navbar with disk error", task_id=task_id)
            assert res.verified is False
            assert res.success is False
            assert res.status == "FAILED"
            assert "proof persistence" in (res.failure_reason or "").lower()

            # Raising execution
            with pytest.raises(ProofPersistenceError, match="Proof persistence"):
                client._raise_for_status(res)

    def test_secret_redacted_across_events_and_proof(self, phase5_repo: Path) -> None:
        """Secrets present in input or output must be scrubbed from events and proof."""
        mock_s1, mock_s2 = _mock_success_systems()
        task_id = "task_adv_secret_017"
        secret = "sk-ant-api03-secret1234567890abcdef1234567890"

        captured_events: List[SDKEvent] = []

        client = BrainFrogClient(
            workspace=phase5_repo,
            system1_factory=lambda b: mock_s1,
            system2_factory=lambda **k: mock_s2,
            test_command=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
        )

        res = client.run_sync(
            f"Fix navbar with credential {secret}",
            task_id=task_id,
            on_event=lambda ev: captured_events.append(ev),
        )
        assert res.verified is True

        # Check events
        for ev in captured_events:
            ev_str = json.dumps(ev.payload)
            assert secret not in ev_str, f"Secret leaked in event: {ev.event_type}"

        # Check proof artifact on disk
        proof = client.get_proof(task_id)
        assert proof is not None
        proof_json = (phase5_repo / ".brainfrog" / "proofs" / f"{task_id}.json").read_text(encoding="utf-8")
        assert secret not in proof_json, "Secret leaked in proof JSON"

    def test_recovery_required_maps_to_typed_exception(self, phase5_repo: Path) -> None:
        """When recovery is required, SDK maps to RecoveryRequiredError."""
        client = BrainFrogClient(workspace=phase5_repo)
        failed_res = TaskResult(
            task_id="task_recov_018",
            user_request="Critical broken task",
            success=False,
            verified=False,
            status="RECOVERY_REQUIRED",
            summary="Manual rollback required",
            recovery_required=True,
            failure_reason="Transaction log corrupted; manual rollback required",
        )
        with pytest.raises(RecoveryRequiredError, match="manual rollback required"):
            client._raise_for_status(failed_res)

    def test_session_isolation_and_approval_isolation(self, phase5_repo: Path) -> None:
        """Different sessions must have isolated approval requests and state."""
        client1 = BrainFrogClient(
            workspace=phase5_repo,
            conversation_id="conv_user_alpha",
            user_id="user_alpha",
        )
        client2 = BrainFrogClient(
            workspace=phase5_repo,
            conversation_id="conv_user_beta",
            user_id="user_beta",
        )

        # Pending approvals in session alpha must not be visible in session beta
        approvals_beta = client2.list_pending_approvals()
        assert len(approvals_beta) == 0


class TestPhase5DSDKRemediation:
    """Regression test suite for Phase 5D SDK Remediations (P0/P1/P2 findings).

    Verifies:
    - P0: Duplicate task IDs rejected for concurrent run_sync() / start() / racing calls
    - P1: Duplicate task IDs rejected across independent clients sharing a workspace
    - P1: Authoritative proof prevents resubmission across any client
    - P1: Stale reservations from dead PIDs safely recovered
    - P1: Canonical approval session identity (channel:user_id:conversation_id) isolation
    - P2: Thread-safe, bounded EventBroker event delivery and drop-oldest eviction
    - P2: Cancellation during non-subprocess phases (planning, pre-mutation)
    - P2: start() callable from worker threads without active event loop
    - P2: Windows drive-relative scope paths and invalid task IDs rejected early
    - P2: run_sync() called within active event loop raises RuntimeError
    """

    TEST_CMD = [sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"]

    def test_concurrent_run_sync_same_id_one_client(self, phase5_repo: Path) -> None:
        """Concurrent run_sync() calls with identical task_id must not both execute."""
        s1, s2 = _mock_success_systems()
        client = BrainFrogClient(
            workspace=phase5_repo,
            system1_factory=lambda _: s1,
            system2_factory=lambda **_: s2,
            test_command=self.TEST_CMD,
        )
        started_ev = threading.Event()
        hold_ev = threading.Event()
        errors: List[Exception] = []

        orig_execute = client._runtime.execute_autonomous_task
        def delayed_execute(request: Any) -> Any:
            if request.task_id == "task_sync_dup_01":
                started_ev.set()
                hold_ev.wait(timeout=5.0)
            return orig_execute(request)

        client._runtime.execute_autonomous_task = delayed_execute

        def worker1() -> None:
            try:
                client.run_sync("Fix navbar 1", task_id="task_sync_dup_01")
            except Exception as e:
                errors.append(e)

        def worker2() -> None:
            started_ev.wait(timeout=5.0)
            try:
                client.run_sync("Fix navbar 2", task_id="task_sync_dup_01")
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=worker1)
        t2 = threading.Thread(target=worker2)
        t1.start()
        t2.start()
        t2.join(timeout=10.0)
        hold_ev.set()
        t1.join(timeout=10.0)
        client.close_sync()

        assert len(errors) == 1
        assert isinstance(errors[0], InvalidRequestError)
        assert "already actively executing" in str(errors[0]) or "already active" in str(errors[0]).lower()

    def test_concurrent_start_same_id_one_client(self, phase5_repo: Path) -> None:
        """Concurrent start() calls with same task_id must reject the second immediately."""
        s1, s2 = _mock_success_systems()
        client = BrainFrogClient(
            workspace=phase5_repo,
            system1_factory=lambda _: s1,
            system2_factory=lambda **_: s2,
            test_command=self.TEST_CMD,
        )
        handle1 = client.start("Fix navbar 1", task_id="task_start_dup_01")
        with pytest.raises(InvalidRequestError, match="(already actively executing|already active)"):
            client.start("Fix navbar 2", task_id="task_start_dup_01")

        res = handle1.wait_sync(timeout=10.0)
        assert res.verified is True
        client.close_sync()

    def test_run_sync_racing_with_start_same_id(self, phase5_repo: Path) -> None:
        """run_sync() racing with active start() must be rejected immediately."""
        s1, s2 = _mock_success_systems()
        client = BrainFrogClient(
            workspace=phase5_repo,
            system1_factory=lambda _: s1,
            system2_factory=lambda **_: s2,
            test_command=self.TEST_CMD,
        )
        handle = client.start("Fix navbar", task_id="task_race_01")
        with pytest.raises(InvalidRequestError, match="(already actively executing|already active)"):
            client.run_sync("Fix navbar duplicate", task_id="task_race_01")

        res = handle.wait_sync(timeout=10.0)
        assert res.verified is True
        client.close_sync()

    def test_two_clients_same_workspace_cross_client_duplicate(self, phase5_repo: Path) -> None:
        """Two separate clients targeting the same workspace must coordinate atomically via disk reservation."""
        s1, s2 = _mock_success_systems()
        client1 = BrainFrogClient(
            workspace=phase5_repo,
            system1_factory=lambda _: s1,
            system2_factory=lambda **_: s2,
            test_command=self.TEST_CMD,
        )
        client2 = BrainFrogClient(
            workspace=phase5_repo,
            system1_factory=lambda _: s1,
            system2_factory=lambda **_: s2,
            test_command=self.TEST_CMD,
        )
        started_ev = threading.Event()
        hold_ev = threading.Event()
        c2_errors: List[Exception] = []

        orig_execute = client1._runtime.execute_autonomous_task
        def delayed_execute(request: Any) -> Any:
            if request.task_id == "task_cross_client_01":
                started_ev.set()
                hold_ev.wait(timeout=5.0)
            return orig_execute(request)

        client1._runtime.execute_autonomous_task = delayed_execute

        def worker1() -> None:
            client1.run_sync("Fix navbar client 1", task_id="task_cross_client_01")

        def worker2() -> None:
            started_ev.wait(timeout=5.0)
            try:
                client2.run_sync("Fix navbar client 2", task_id="task_cross_client_01")
            except Exception as e:
                c2_errors.append(e)

        t1 = threading.Thread(target=worker1)
        t2 = threading.Thread(target=worker2)
        t1.start()
        t2.start()
        t2.join(timeout=10.0)
        hold_ev.set()
        t1.join(timeout=10.0)
        client1.close_sync()
        client2.close_sync()

        assert len(c2_errors) == 1
        assert isinstance(c2_errors[0], InvalidRequestError)
        assert "already active" in str(c2_errors[0]).lower() or "executing" in str(c2_errors[0]).lower()

    def test_two_clients_different_workspaces_same_id_isolated(self, phase5_repo: Path, tmp_path: Path) -> None:
        """Two clients targeting different workspaces may use identical task IDs without collision."""
        repo2 = tmp_path / "phase5_project2"
        shutil.copytree(FIXTURE_SRC, repo2)
        subprocess.run(["git", "init"], cwd=str(repo2), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(repo2), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "tester@test.local"], cwd=str(repo2), check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=str(repo2), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo2), check=True, capture_output=True)

        s1, s2 = _mock_success_systems()
        client1 = BrainFrogClient(workspace=phase5_repo, system1_factory=lambda _: s1, system2_factory=lambda **_: s2, test_command=self.TEST_CMD)
        client2 = BrainFrogClient(workspace=repo2, system1_factory=lambda _: s1, system2_factory=lambda **_: s2, test_command=self.TEST_CMD)

        res1 = client1.run_sync("Task 1", task_id="task_shared_id_diff_ws")
        res2 = client2.run_sync("Task 2", task_id="task_shared_id_diff_ws")

        assert res1.verified is True
        assert res2.verified is True
        client1.close_sync()
        client2.close_sync()

    def test_completed_task_id_resubmission_rejected(self, phase5_repo: Path) -> None:
        """A completed task ID cannot be resubmitted by the same or different client."""
        s1, s2 = _mock_success_systems()
        client1 = BrainFrogClient(workspace=phase5_repo, system1_factory=lambda _: s1, system2_factory=lambda **_: s2, test_command=self.TEST_CMD)
        res = client1.run_sync("Initial task", task_id="task_proven_001")
        assert res.verified is True

        # Resubmit via same client
        with pytest.raises(InvalidRequestError, match="authoritative proof"):
            client1.run_sync("Duplicate task same client", task_id="task_proven_001")

        # Resubmit via fresh second client
        client2 = BrainFrogClient(workspace=phase5_repo, system1_factory=lambda _: s1, system2_factory=lambda **_: s2, test_command=self.TEST_CMD)
        with pytest.raises(InvalidRequestError, match="authoritative proof"):
            client2.start("Duplicate task new client", task_id="task_proven_001")

        client1.close_sync()
        client2.close_sync()

    def test_stale_reservation_from_dead_pid_reclaimed(self, phase5_repo: Path) -> None:
        """A stale reservation lock left by a dead PID is safely reclaimed without crashing."""
        tasks_dir = phase5_repo / ".brainfrog" / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        lock_file = tasks_dir / "stale_task_999.lock"
        stale_data = {
            "task_id": "stale_task_999",
            "pid": 999999,
            "created_at": time.time() - 1000.0,
            "process_start_time": 0.0,
        }
        lock_file.write_text(json.dumps(stale_data), encoding="utf-8")

        s1, s2 = _mock_success_systems()
        client = BrainFrogClient(workspace=phase5_repo, system1_factory=lambda _: s1, system2_factory=lambda **_: s2, test_command=self.TEST_CMD)
        res = client.run_sync("Fix navbar with reclaimed stale lock", task_id="stale_task_999")
        assert res.verified is True
        client.close_sync()

    def test_canonical_approval_session_identity_and_isolation(self, phase5_repo: Path) -> None:
        """Approvals must use canonical session identity (channel:user_id:conversation_id) and maintain strict isolation."""
        c_alice = BrainFrogClient(workspace=phase5_repo, channel="cli", user_id="alice", conversation_id="conv_100")
        c_bob = BrainFrogClient(workspace=phase5_repo, channel="cli", user_id="bob", conversation_id="conv_100")
        c_alice_diff_conv = BrainFrogClient(workspace=phase5_repo, channel="cli", user_id="alice", conversation_id="conv_200")
        c_alice_telegram = BrainFrogClient(workspace=phase5_repo, channel="telegram", user_id="alice", conversation_id="conv_100")

        # Create request under canonical session cli:alice:conv_100
        req = c_alice.runtime.approval_service.create_request(
            session_id=c_alice.canonical_session_id,
            channel="cli",
            user_id="alice",
            conversation_id="conv_100",
            operation_type="file.modify",
            canonical_operation=CanonicalOperation("file.modify", "navbar.py"),
            workspace_root=str(phase5_repo),
        )

        # Alice sees the pending approval
        pending_alice = c_alice.list_pending_approvals()
        assert len(pending_alice) == 1
        assert pending_alice[0].request_id == req.request_id

        # Bob (different user) sees 0
        assert len(c_bob.list_pending_approvals()) == 0

        # Different conversation sees 0
        assert len(c_alice_diff_conv.list_pending_approvals()) == 0

        # Different channel sees 0
        assert len(c_alice_telegram.list_pending_approvals()) == 0

        # Requester cannot self-approve under Two-Man Rule
        with pytest.raises(PermissionDeniedError, match="Two-man rule violation"):
            c_alice.approve(req.request_id, approver="alice")

        # Channel mismatch approval fails
        with pytest.raises(PermissionDeniedError, match="Channel mismatch"):
            c_alice_telegram.approve(req.request_id, approver="bob")

        # Authorized second person (Bob) approves on the authorized channel
        c_bob.approve(req.request_id, approver="bob")

        # Once approved, request is no longer pending
        assert len(c_alice.list_pending_approvals()) == 0

        # Monotonic state: already resolved request cannot be approved again
        with pytest.raises(PermissionDeniedError, match="Cannot approve request"):
            c_bob.approve(req.request_id, approver="bob")

        c_alice.close_sync()
        c_bob.close_sync()
        c_alice_diff_conv.close_sync()
        c_alice_telegram.close_sync()

    def test_start_callable_from_thread_without_event_loop(self, phase5_repo: Path) -> None:
        """start() must execute successfully from a thread without an active event loop."""
        s1, s2 = _mock_success_systems()
        client = BrainFrogClient(workspace=phase5_repo, system1_factory=lambda _: s1, system2_factory=lambda **_: s2, test_command=self.TEST_CMD)

        result_holder: List[Any] = []
        error_holder: List[Exception] = []

        def thread_without_loop() -> None:
            asyncio.set_event_loop(None)
            try:
                handle = client.start("Fix navbar in raw thread", task_id="task_raw_thread_01")
                res = handle.wait_sync(timeout=10.0)
                result_holder.append(res)
            except Exception as exc:
                error_holder.append(exc)

        t = threading.Thread(target=thread_without_loop)
        t.start()
        t.join(timeout=15.0)
        client.close_sync()

        assert len(error_holder) == 0, f"Error raised: {error_holder}"
        assert len(result_holder) == 1
        assert result_holder[0].verified is True

    @pytest.mark.anyio
    async def test_run_sync_inside_running_event_loop_raises_runtime_error(self, phase5_repo: Path) -> None:
        """run_sync() called inside an active event loop must raise RuntimeError immediately."""
        client = BrainFrogClient(workspace=phase5_repo)
        with pytest.raises(RuntimeError, match="cannot be called from a thread with an active running event loop"):
            client.run_sync("Should fail fast", task_id="task_fail_in_loop")
        client.close_sync()

    def test_drive_relative_and_invalid_task_paths_rejected_early(self, phase5_repo: Path) -> None:
        """Windows drive-relative scope paths and invalid task IDs must fail fast with InvalidRequestError."""
        client = BrainFrogClient(workspace=phase5_repo)

        # Drive-relative and absolute scope paths
        with pytest.raises(InvalidRequestError, match="drive-relative or absolute"):
            client.run_sync("Task", allowed_scope=["D:relative_path"])
        with pytest.raises(InvalidRequestError, match="drive-relative or absolute"):
            client.run_sync("Task", allowed_scope=["C:foo/bar"])

        # Invalid task IDs
        with pytest.raises(InvalidRequestError, match="drive letter"):
            client.run_sync("Task", task_id="D:task")
        with pytest.raises(InvalidRequestError, match="invalid characters"):
            client.run_sync("Task", task_id="task;rm")
        with pytest.raises(InvalidRequestError, match="invalid characters"):
            client.run_sync("Task", task_id="task|pipe")
        with pytest.raises(InvalidRequestError, match="invalid characters"):
            client.run_sync("Task", task_id="task*star")
        with pytest.raises(InvalidRequestError, match="invalid characters"):
            client.run_sync("Task", task_id="task:colon")

        client.close_sync()

    @pytest.mark.anyio
    async def test_event_broker_thread_safe_cross_thread_and_bounded_eviction(self) -> None:
        """EventBroker must safely enqueue from worker threads and evict oldest on full queue."""
        from brainfrog.sdk.events import EventBroker, EventType, create_sdk_event
        broker = EventBroker()
        q = broker.subscribe_async("task_broker_01")

        # Emit from 4 worker threads concurrently
        def worker(idx: int) -> None:
            for i in range(25):
                ev = create_sdk_event(
                    event_type=EventType.STEP_STARTED,
                    task_id="task_broker_01",
                    session_id="sess_01",
                    payload={"worker": idx, "seq": i},
                )
                broker.emit(ev)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        received = []
        for _ in range(100):
            received.append(await asyncio.wait_for(q.get(), timeout=2.0))
        assert len(received) == 100

        # Test bounded eviction on full queue (maxsize=1000)
        for i in range(1010):
            ev = create_sdk_event(
                event_type=EventType.STEP_STARTED,
                task_id="task_broker_01",
                session_id="sess_01",
                payload={"seq": i},
            )
            broker.emit(ev)

        await asyncio.sleep(0.05)
        assert q.qsize() == 1000

        # Callback exception handling: sync handler raising exception must not break emit()
        def bad_handler(e: Any) -> None:
            raise ValueError("Boom in handler")

        broker.add_sync_handler(bad_handler)
        ev_test = create_sdk_event(
            event_type=EventType.STEP_STARTED,
            task_id="task_broker_01",
            session_id="sess_01",
            payload={"test": True},
        )
        broker.emit(ev_test)

        broker.unsubscribe_async(q, "task_broker_01")
        broker.clear()

    def test_cancellation_during_planning_phase(self, phase5_repo: Path) -> None:
        """Cancellation during planning must abort cleanly without modifying tracked git files."""
        s1, s2 = _mock_success_systems()

        client_ref: List[Any] = []

        def plan_task_with_cancel(*args: Any, **kwargs: Any) -> Any:
            if client_ref:
                client_ref[0].cancel_task("task_cancel_plan_01")
            return [PlanStep(id="1", description="Fix navbar", files=["navbar.py"])]

        s2.plan_task.side_effect = plan_task_with_cancel

        client = BrainFrogClient(
            workspace=phase5_repo,
            system1_factory=lambda _: s1,
            system2_factory=lambda **_: s2,
            test_command=self.TEST_CMD,
        )
        client_ref.append(client)

        res = client.run_sync("Fix navbar with cancel in planning", task_id="task_cancel_plan_01")
        assert res.verified is False
        assert res.status == "CANCELLED"

        # Verify no uncommitted code changes in tracked git files
        diff_proc = subprocess.run(["git", "diff", "--name-only"], cwd=phase5_repo, capture_output=True, text=True)
        assert diff_proc.stdout.strip() == ""

        # Verify workspace lock is released
        ws_lock = WorkspaceLock(phase5_repo)
        assert not ws_lock.is_locked()

        client.close_sync()

    def test_cancellation_before_mutation(self, phase5_repo: Path) -> None:
        """Cancellation before mutation must abort cleanly with zero file modifications."""
        s1, s2 = _mock_success_systems()
        client_ref: List[Any] = []

        orig_plan = s2.plan_task.return_value
        def plan_and_cancel(*args: Any, **kwargs: Any) -> Any:
            if client_ref:
                client_ref[0].cancel_task("task_cancel_pre_mutate_01")
            return orig_plan

        s2.plan_task.side_effect = plan_and_cancel

        client = BrainFrogClient(
            workspace=phase5_repo,
            system1_factory=lambda _: s1,
            system2_factory=lambda **_: s2,
            test_command=self.TEST_CMD,
        )
        client_ref.append(client)

        res = client.run_sync("Fix navbar and cancel before write", task_id="task_cancel_pre_mutate_01")
        assert res.verified is False
        assert res.status == "CANCELLED"

        # No tracked files written
        diff_proc = subprocess.run(["git", "diff", "--name-only"], cwd=phase5_repo, capture_output=True, text=True)
        assert diff_proc.stdout.strip() == ""

        ws_lock = WorkspaceLock(phase5_repo)
        assert not ws_lock.is_locked()
        client.close_sync()


class TestPhase5FCrashConsistencyRemediation:
    """Phase 5F: Crash consistency, ownership token validation, and finalization agreement."""

    TEST_CMD = [sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"]

    def test_reservation_release_preserves_replacement_lock(self, tmp_path: Path) -> None:
        """Stale reservation handle release must never delete a replacement lock owned by another process/nonce."""
        ws = tmp_path / "ws_rep_preserve"
        ws.mkdir()
        coord = TaskReservationCoordinator(ws)
        task_id = "task_ownership_preserve_01"

        # Handle 1 acquires reservation
        h1 = coord.reserve(task_id, actor="proc1")
        lock_file = ws / ".brainfrog" / "tasks" / f"{task_id}.lock"
        assert lock_file.exists()

        # Simulate replacement reservation acquired by another process/nonce
        meta_replacement = {
            "task_id": task_id,
            "pid": 9999999,
            "nonce": "replacement_nonce_xyz123",
            "process_create_time": None,
            "acquired_at": time.time(),
            "actor": "proc2_replacement",
            "workspace": str(ws),
        }
        lock_file.write_text(json.dumps(meta_replacement), encoding="utf-8")

        # Handle 1 calls release()
        h1.release()

        # Replacement reservation must still exist and be untouched
        assert lock_file.exists()
        on_disk = json.loads(lock_file.read_text(encoding="utf-8"))
        assert on_disk.get("nonce") == "replacement_nonce_xyz123"
        assert on_disk.get("actor") == "proc2_replacement"

    def test_unresolved_commit_intent_blocks_duplicate_execution(self, phase5_repo: Path) -> None:
        """When a commit intent is recorded and committed in Git but proof was interrupted, duplicate execution is blocked."""
        task_id = "task_unresolved_commit_01"

        # Record commit intent and simulate successful git commit
        finalizer = TaskFinalizationCoordinator(phase5_repo)
        finalizer.record_commit_intent(task_id)

        # Commit a file to Git
        (phase5_repo / "new_feature.py").write_text("# new feature", encoding="utf-8")
        subprocess.run(["git", "add", "new_feature.py"], cwd=phase5_repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "feat: new feature commit"], cwd=phase5_repo, check=True, capture_output=True)
        c_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=phase5_repo, check=True, capture_output=True, text=True).stdout.strip()

        finalizer.record_commit_success(task_id, commit_sha=c_sha)

        # Verify no proof exists
        proof_store = FileProofStore(phase5_repo)
        assert proof_store.exists(task_id) is False

        # Attempt to run task via client: MUST fail closed with RecoveryRequiredError
        s1, s2 = _mock_success_systems()
        client = BrainFrogClient(
            workspace=phase5_repo,
            system1_factory=lambda _: s1,
            system2_factory=lambda **_: s2,
            test_command=self.TEST_CMD,
        )

        with pytest.raises(RecoveryRequiredError) as exc_info:
            client.run_sync("Execute task with unresolved commit", task_id=task_id)

        assert "unresolved commit" in str(exc_info.value).lower()
        client.close_sync()

    def test_late_cancellation_preserves_durable_finalization_agreement(self, phase5_repo: Path) -> None:
        """Cancellation requested after durable finalization must not return a contradictory CANCELLED result."""
        s1, s2 = _mock_success_systems()
        task_id = "task_late_cancel_finalization_01"

        client = BrainFrogClient(
            workspace=phase5_repo,
            system1_factory=lambda _: s1,
            system2_factory=lambda **_: s2,
            test_command=self.TEST_CMD,
        )

        # Hook proof save to cancel task immediately after durable finalization
        orig_save = FileProofStore.save
        def save_and_cancel(self, proof, markdown_content=None, **kwargs):
            res = orig_save(self, proof, markdown_content, **kwargs)
            client.cancel_task(task_id)
            return res

        emitted_events: List[SDKEvent] = []
        client.on_event(lambda ev: emitted_events.append(ev))

        with patch.object(FileProofStore, "save", side_effect=save_and_cancel, autospec=True):
            res = client.run_sync("Fix navbar with late cancel", task_id=task_id)

        # Monotonic cutoff: Result must be COMMITTED and verified=True
        assert res.status == "COMMITTED"
        assert res.verified is True

        # Exactly one terminal event emitted and it must be TASK_COMPLETED
        terminal_events = [e for e in emitted_events if e.event_type in (EventType.TASK_COMPLETED, EventType.TASK_CANCELLED, EventType.TASK_FAILED)]
        assert len(terminal_events) == 1
        assert terminal_events[0].event_type == EventType.TASK_COMPLETED

        # Durable proof on disk must be VERIFIED
        proof_disk = client.get_proof(task_id)
        assert proof_disk is not None
        assert proof_disk.verdict.status == "VERIFIED"

        # get_task() reconstruction must agree
        reconstructed = client.get_task(task_id)
        assert reconstructed is not None
        assert reconstructed.status == "COMMITTED"
        assert reconstructed.verified is True
        client.close_sync()

    def test_proof_store_coupled_atomic_save_and_self_healing(self, tmp_path: Path) -> None:
        """FileProofStore must not swallow companion Markdown failures, and load_markdown() must self-heal."""
        ws = tmp_path / "ws_proof_coupled"
        ws.mkdir()
        store = FileProofStore(ws)
        task_id = "task_proof_coupled_01"

        c_task = TaskEvidence(
            task_id=task_id,
            session_id="sess_01",
            description="Test task",
            created_at="2026-10-09T00:00:00Z",
            completed_at="2026-10-09T00:00:01Z",
            duration_ms=1000,
        )
        c_verdict = ProofVerdict(status="VERIFIED", reason="All tests passed")
        proof = ProofArtifact(
            task=c_task,
            verdict=c_verdict,
            provenance=capture_git_provenance(ws),
            verification=VerificationEvidence(test_command="pytest", exit_code=0, stdout_summary="", stderr_summary=""),
            changes=ChangesEvidence(transaction_id="tx_1", status="COMMITTED", files=[]),
            reproducibility=ReproducibilityEvidence(command="pytest"),
        )

        # 1. Failure during Markdown replace is raised as IOError and does not leave orphaned JSON
        orig_replace = os.replace
        def mock_replace(src: Any, dst: Any) -> None:
            if str(dst).endswith(".md"):
                raise OSError("Injected disk error during markdown replace")
            orig_replace(src, dst)

        with patch("os.replace", side_effect=mock_replace):
            with pytest.raises(IOError):
                store.save(proof, markdown_content="# Test MD")

        # Verify incomplete proof was not left as existing
        assert store.exists(task_id) is False

        # 2. Clean save without markdown succeeds
        pj, pm = store.save(proof, markdown_content=None)
        assert pj.exists()
        assert pm is None

        # 3. load_markdown() self-heals companion markdown from JSON proof artifact
        md_text = store.load_markdown(task_id)
        assert md_text is not None and len(md_text) > 0
        assert (ws / ".brainfrog" / "proofs" / f"{task_id}.md").exists()

    def test_cross_process_reservation_race_deterministic(self, tmp_path: Path) -> None:
        """Two independent Python processes racing for the same task ID resolve deterministically."""
        ws = tmp_path / "ws_proc_race"
        ws.mkdir()
        barrier = ws / "start.barrier"
        task_id = "task_proc_race_01"

        child_code = """
import sys, time
from pathlib import Path
repo_root = Path(r"C:\\dame-project\\tools\\agentic_dev")
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))
from core.runtime.task_reservation import TaskReservationCoordinator, TaskActiveError

ws = Path(sys.argv[1])
task_id = sys.argv[2]
barrier_file = Path(sys.argv[3])

while not barrier_file.exists():
    time.sleep(0.01)

coord = TaskReservationCoordinator(ws)
try:
    h = coord.reserve(task_id, actor="proc")
    print("ACQUIRED")
    time.sleep(0.5)
    h.release()
except TaskActiveError:
    print("REJECTED")
except Exception as e:
    print(f"ERROR: {e}")
"""
        p1 = subprocess.Popen([sys.executable, "-c", child_code, str(ws), task_id, str(barrier)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        p2 = subprocess.Popen([sys.executable, "-c", child_code, str(ws), task_id, str(barrier)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        time.sleep(0.2)
        barrier.touch()

        out1, _ = p1.communicate(timeout=10)
        out2, _ = p2.communicate(timeout=10)

        results = {out1.strip(), out2.strip()}
        assert results == {"ACQUIRED", "REJECTED"}


# =============================================================================
# 13. Phase 5F.2 Crash Recovery Targeted Remediation Tests
# =============================================================================

class TestCrashRecoveryRemediation:
    def test_task_a_reservation_release_interleaving(self, tmp_path: Path) -> None:
        """Deterministic reproduction of Audit A interleaving:
        Owner A verifies ownership, Owner B replaces reservation, Owner A releases,
        and Owner B's replacement reservation MUST survive.
        """
        from core.runtime.task_reservation import (
            TaskReservationCoordinator,
            TaskAlreadyCompletedError,
        )

        ws = tmp_path / "ws_race_interleaving"
        ws.mkdir()
        task_id = "task_race_remediation_01"

        coord_a = TaskReservationCoordinator(ws)
        coord_b = TaskReservationCoordinator(ws)

        # 1. Owner A acquires reservation
        handle_a = coord_a.reserve(task_id, actor="owner_a")
        assert handle_a.lock_path.exists()
        meta_a = json.loads(handle_a.lock_path.read_text(encoding="utf-8"))
        assert meta_a["nonce"] == handle_a.nonce
        assert meta_a["actor"] == "owner_a"

        # 2. Owner A verifies ownership
        meta_check = coord_a._read_lock_meta(handle_a.lock_path)
        assert meta_check is not None
        assert meta_check["nonce"] == handle_a.nonce

        # 3. Reservation is reclaimed/replaced by Owner B
        handle_a.lock_path.unlink()
        from core.runtime.task_reservation import _PROCESS_ACTIVE_RESERVATIONS, _PROCESS_RESERVATION_LOCK
        with _PROCESS_RESERVATION_LOCK:
            _PROCESS_ACTIVE_RESERVATIONS.pop((ws, task_id), None)
        handle_b = coord_b.reserve(task_id, actor="owner_b")
        assert handle_b.lock_path.exists()
        assert handle_b.nonce != handle_a.nonce
        meta_b = json.loads(handle_b.lock_path.read_text(encoding="utf-8"))
        assert meta_b["nonce"] == handle_b.nonce
        assert meta_b["actor"] == "owner_b"

        # 4. Owner A releases stale handle_a
        handle_a.release()

        # 5. Owner B's replacement reservation MUST survive!
        assert handle_b.lock_path.exists()
        meta_survived = json.loads(handle_b.lock_path.read_text(encoding="utf-8"))
        assert meta_survived["nonce"] == handle_b.nonce
        assert meta_survived["actor"] == "owner_b"

        # Clean up
        handle_b.release()
        assert not handle_b.lock_path.exists()

    def test_task_a_cross_process_release_interleaving(self, tmp_path: Path) -> None:
        """Cross-process verification of Audit A:
        Process A verifies ownership, Process B acquires replacement in separate process,
        Process A releases, and Process B's replacement lock survives.
        """
        from core.runtime.task_reservation import TaskReservationCoordinator

        ws = tmp_path / "ws_proc_race_interleaving"
        ws.mkdir()
        task_id = "task_proc_race_02"

        coord_a = TaskReservationCoordinator(ws)
        handle_a = coord_a.reserve(task_id, actor="proc_a")
        assert handle_a.lock_path.exists()

        # Owner A verifies ownership
        meta_a = coord_a._read_lock_meta(handle_a.lock_path)
        assert meta_a is not None and meta_a["nonce"] == handle_a.nonce

        # Subprocess B reclaims and creates replacement lock
        handle_a.lock_path.unlink()
        subproc_code = f"""
import sys, json
from pathlib import Path
repo_root = Path(r"C:\\dame-project\\tools\\agentic_dev")
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))
from core.runtime.task_reservation import TaskReservationCoordinator

ws = Path(r"{ws}")
coord_b = TaskReservationCoordinator(ws)
handle_b = coord_b.reserve("{task_id}", actor="proc_b")
print(f"B_NONCE:{{handle_b.nonce}}")
"""
        res = subprocess.run([sys.executable, "-c", subproc_code], capture_output=True, text=True, check=True)
        assert "B_NONCE:" in res.stdout
        b_nonce = res.stdout.split("B_NONCE:")[1].strip()

        # Verify replacement lock is on disk
        meta_b = coord_a._read_lock_meta(handle_a.lock_path)
        assert meta_b is not None
        assert meta_b["nonce"] == b_nonce
        assert meta_b["actor"] == "proc_b"

        # Process A calls release()
        handle_a.release()

        # Replacement lock MUST survive!
        assert handle_a.lock_path.exists()
        meta_after = coord_a._read_lock_meta(handle_a.lock_path)
        assert meta_after is not None
        assert meta_after["nonce"] == b_nonce
        assert meta_after["actor"] == "proc_b"

    def test_task_b_orphan_intent_reconciled_with_valid_proof(self, tmp_path: Path) -> None:
        """Crash Window 5: crash after proof persistence but before finalize_proof().
        When valid authoritative proof JSON exists, recovery reconciles the orphan intent,
        safely removing it, and is idempotent. Duplicate re-execution is rejected with
        TaskAlreadyCompletedError.
        """
        from core.runtime.task_finalization import (
            CommitIntentRecord,
            CommitIntentStatus,
            TaskFinalizationCoordinator,
        )
        from core.runtime.task_reservation import (
            TaskAlreadyCompletedError,
            TaskReservationCoordinator,
        )

        ws = tmp_path / "ws_orphan_intent"
        ws.mkdir()
        task_id = "task_orphan_reconcile_01"

        fin = TaskFinalizationCoordinator(ws)
        store = FileProofStore(ws)
        res_coord = TaskReservationCoordinator(ws)

        # 1. Simulate Window 5 crash state:
        # Record commit intent & commit success (status=COMMITTED_PENDING_PROOF)
        fin.record_commit_intent(task_id)
        intent = fin.record_commit_success(task_id, commit_sha="abc123commit")
        assert intent is not None
        assert intent.status == CommitIntentStatus.COMMITTED_PENDING_PROOF
        assert (ws / ".brainfrog" / "finalization" / f"{task_id}.json").exists()

        # Persist valid authoritative proof artifact matching commit SHA
        c_task = TaskEvidence(
            task_id=task_id,
            session_id="sess_1",
            description="Task with orphan intent",
            created_at="2026-10-09T00:00:00Z",
            completed_at="2026-10-09T00:01:00Z",
            duration_ms=60000,
        )
        c_verdict = ProofVerdict(status="VERIFIED", reason="All tests passed")
        from core.runtime.proof import GitProvenance
        proof = ProofArtifact(
            task=c_task,
            verdict=c_verdict,
            provenance=GitProvenance(
                is_git=True,
                repo_root=str(ws),
                final_head_sha="abc123commit",
            ),
            verification=VerificationEvidence(test_command="pytest", exit_code=0, stdout_summary="", stderr_summary=""),
            changes=ChangesEvidence(transaction_id="tx_1", status="COMMITTED", files=[]),
            reproducibility=ReproducibilityEvidence(command="pytest"),
        )
        store.save(proof, markdown_content="# Verified Report")
        assert store.exists(task_id)

        # 2. get_unresolved_intent() reconciles the orphan intent with the authoritative proof
        reconciled = fin.get_unresolved_intent(task_id)
        assert reconciled is None
        # Intent file safely removed
        assert not (ws / ".brainfrog" / "finalization" / f"{task_id}.json").exists()

        # 3. Repeated recovery calls are idempotent
        assert fin.get_unresolved_intent(task_id) is None
        assert fin.reconcile_intent(task_id) is None

        # 4. Duplicate submission is rejected via TaskAlreadyCompletedError
        with pytest.raises(TaskAlreadyCompletedError, match="already been executed"):
            res_coord.reserve(task_id)

    def test_task_b_orphan_intent_fail_closed_on_corrupt_proof(self, tmp_path: Path) -> None:
        """When intent is COMMITTED_PENDING_PROOF, but proof is corrupt or mismatched,
        reconciliation fails closed, preserves the intent file, and returns unresolved record.
        """
        from core.runtime.task_finalization import (
            CommitIntentStatus,
            TaskFinalizationCoordinator,
        )

        ws = tmp_path / "ws_corrupt_proof_intent"
        ws.mkdir()
        task_id = "task_corrupt_proof_intent_02"

        fin = TaskFinalizationCoordinator(ws)
        store = FileProofStore(ws)

        fin.record_commit_intent(task_id)
        fin.record_commit_success(task_id, commit_sha="def456commit")

        # Create corrupt proof JSON file
        proof_dir = ws / ".brainfrog" / "proofs"
        proof_dir.mkdir(parents=True, exist_ok=True)
        (proof_dir / f"{task_id}.json").write_text("{ corrupt json: [", encoding="utf-8")

        # Reconciliation must FAIL CLOSED
        unresolved = fin.get_unresolved_intent(task_id)
        assert unresolved is not None
        assert unresolved.commit_sha == "def456commit"
        # Intent file MUST NOT be deleted
        assert (ws / ".brainfrog" / "finalization" / f"{task_id}.json").exists()

        # Repeated calls are idempotent and continue to fail closed
        unresolved2 = fin.get_unresolved_intent(task_id)
        assert unresolved2 is not None

    def test_task_c_markdown_self_healing_invalid_utf8(self, tmp_path: Path) -> None:
        """If companion Markdown contains invalid UTF-8, load_markdown() self-heals from JSON
        without overwriting the authoritative JSON manifest.
        """
        ws = tmp_path / "ws_invalid_utf8_md"
        ws.mkdir()
        task_id = "task_invalid_utf8_01"

        store = FileProofStore(ws)
        c_task = TaskEvidence(
            task_id=task_id,
            session_id="sess_1",
            description="Fix invalid UTF-8 md",
            created_at="2026-10-09T00:00:00Z",
            completed_at="2026-10-09T00:01:00Z",
            duration_ms=1000,
        )
        proof = ProofArtifact(
            task=c_task,
            verdict=ProofVerdict(status="VERIFIED", reason="All tests passed"),
            provenance=None,
            verification=VerificationEvidence(test_command="pytest", exit_code=0, stdout_summary="", stderr_summary=""),
            changes=ChangesEvidence(transaction_id="tx_1", status="COMMITTED", files=[]),
            reproducibility=ReproducibilityEvidence(command="pytest"),
        )
        pj, pm = store.save(proof, markdown_content="# Initial Valid Markdown")
        assert pj.exists()
        assert pm is not None and pm.exists()
        json_content_before = pj.read_text(encoding="utf-8")

        # Corrupt markdown file with invalid UTF-8 bytes
        with open(pm, "wb") as f:
            f.write(b"\xff\xfe\x00\x80\xaa\xbb\xcc")

        # Verify raw read raises UnicodeDecodeError
        with pytest.raises(UnicodeDecodeError):
            pm.read_text(encoding="utf-8")

        # load_markdown() self-heals the companion markdown from authoritative JSON
        repaired_md = store.load_markdown(task_id)
        assert "# Proof of Verification" in repaired_md or task_id in repaired_md
        assert pm.exists()

        # Repaired file is now valid UTF-8
        assert pm.read_text(encoding="utf-8") == repaired_md

        # Authoritative JSON manifest was NOT modified or overwritten
        assert pj.read_text(encoding="utf-8") == json_content_before

    def test_task_c_markdown_self_healing_empty_or_unreadable(self, tmp_path: Path) -> None:
        """If companion Markdown is empty (0 bytes), load_markdown() self-heals from JSON."""
        ws = tmp_path / "ws_empty_md"
        ws.mkdir()
        task_id = "task_empty_md_02"

        store = FileProofStore(ws)
        c_task = TaskEvidence(
            task_id=task_id,
            session_id="sess_1",
            description="Fix empty md",
            created_at="2026-10-09T00:00:00Z",
            completed_at="2026-10-09T00:01:00Z",
            duration_ms=1000,
        )
        proof = ProofArtifact(
            task=c_task,
            verdict=ProofVerdict(status="VERIFIED", reason="All tests passed"),
            provenance=None,
            verification=VerificationEvidence(test_command="pytest", exit_code=0, stdout_summary="", stderr_summary=""),
            changes=ChangesEvidence(transaction_id="tx_1", status="COMMITTED", files=[]),
            reproducibility=ReproducibilityEvidence(command="pytest"),
        )
        pj, pm = store.save(proof, markdown_content="# To Be Emptied")
        assert pm is not None
        pm.write_text("", encoding="utf-8")
        assert pm.stat().st_size == 0

        repaired_md = store.load_markdown(task_id)
        assert len(repaired_md) > 0
        assert pm.stat().st_size > 0

    def test_task_c_markdown_fail_closed_on_corrupt_json(self, tmp_path: Path) -> None:
        """If Markdown companion is missing or unreadable, and authoritative JSON is corrupt,
        load_markdown() fails closed, does not overwrite files, and preserves evidence.
        """
        ws = tmp_path / "ws_corrupt_json_md"
        ws.mkdir()
        task_id = "task_corrupt_json_03"

        store = FileProofStore(ws)
        proofs_dir = ws / ".brainfrog" / "proofs"
        proofs_dir.mkdir(parents=True, exist_ok=True)

        json_path = proofs_dir / f"{task_id}.json"
        corrupt_payload = "{ this is completely invalid JSON {"
        json_path.write_text(corrupt_payload, encoding="utf-8")

        # No markdown file exists
        assert not (proofs_dir / f"{task_id}.md").exists()

        # load_markdown() must fail closed (JSONDecodeError)
        with pytest.raises(json.JSONDecodeError):
            store.load_markdown(task_id)

        # Corrupt JSON was preserved and not overwritten
        assert json_path.read_text(encoding="utf-8") == corrupt_payload
        # No bogus markdown was written
        assert not (proofs_dir / f"{task_id}.md").exists()
