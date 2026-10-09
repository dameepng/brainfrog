"""BrainFrog Phase 3 — End-to-End Autonomous Coding Workflow Tests.

Verifies the complete verification-first autonomous coding pipeline:
    TaskRequest -> Plan -> Mutation -> Tests -> VerificationGate -> TaskResult

Tests:
1. Real E2E Autonomous Success (navbar bug fix, tests pass, commits)
2. Real E2E Autonomous Failure (tests fail, rolls back to ROLLED_BACK)
3. Preservation of Pre-existing User Changes on Failure
4. Code-side Scope Enforcement (unexpected/unrelated file mutations rejected)
5. Task-level Allowed Scope Boundary Enforcement
6. Verification Gate Failure Blocks Commit (tests pass but verification fails)
7. Cancellation Lifecycle Containment
8. Phase 1 Trust Boundary Invariant Regression
9. Strict TaskResult Invariant (success=True requires verified=True)
10. Observability Report Formatting
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from core.runtime import (
    BrainFrogRuntime,
    ChannelTrustLevel,
    HardenedProcessResult,
    IncomingMessage,
    TaskPlan,
    TaskRequest,
    TaskResult,
    TaskStep,
    TransactionCoordinator,
    TransactionStatus,
    VerificationGate,
    execute_autonomous_task,
)
from orchestrator import Orchestrator, PlanStep, RunConfig, StepResult
from system1.base import Answer


FIXTURE_SRC = Path(__file__).resolve().parent / "fixtures" / "phase3_demo"


@pytest.fixture
def phase3_repo(tmp_path: Path) -> Path:
    """Create a temporary git repository seeded from phase3_demo fixture."""
    repo = tmp_path / "phase3_project"
    shutil.copytree(FIXTURE_SRC, repo)

    # Initialize git repo in the fixture for authentic workspace behavior
    try:
        subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "BrainFrog Tester"], cwd=str(repo), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "tester@brainfrog.local"], cwd=str(repo), check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "chore: initial broken fixture commit"], cwd=str(repo), check=True, capture_output=True)
    except Exception:
        pass
    return repo


class TestPhase3AutonomousWorkflowE2E:
    """Comprehensive Phase 3 Verification-First Workflow Test Suite."""

    def test_fixture_is_broken_initially(self, phase3_repo: Path):
        """Baseline check: The fixture's tests fail before BrainFrog applies any fix."""
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
            cwd=str(phase3_repo),
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0
        assert "FAILED" in proc.stdout or "failed" in proc.stdout

    def test_e2e_autonomous_success_flow(self, phase3_repo: Path):
        """Scenario 1: End-to-end autonomous fix, tests pass, verified and committed."""
        navbar_file = phase3_repo / "navbar.py"
        user_file = phase3_repo / "unrelated_user.py"
        original_user_content = user_file.read_text(encoding="utf-8")

        runtime = BrainFrogRuntime(
            repo_dir=phase3_repo,
            persist_sessions=False,
            default_test_cmd=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
        )

        # Fixed code that correctly toggles state
        fixed_navbar_code = (
            '"""Mobile navbar component with working toggle."""\n\n'
            'class MobileNavbar:\n'
            '    def __init__(self) -> None:\n'
            '        self.is_open: bool = False\n'
            '        self.breakpoint: int = 768\n\n'
            '    def toggle(self) -> None:\n'
            '        self.is_open = not self.is_open\n\n'
            '    def render(self, viewport_width: int) -> str:\n'
            '        if viewport_width < self.breakpoint and self.is_open:\n'
            '            return "<nav class=\'navbar mobile mobile-menu-active\'></nav>"\n'
            '        return "<nav class=\'navbar desktop\'></nav>"\n'
        )

        # Mock S1 (Jev)
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

        # Mock S2 (Claude)
        mock_s2 = MagicMock()
        mock_s2.provider_name = "claude_mock"
        mock_s2.plan_task.return_value = [
            PlanStep(id="1", description="Fix toggle in navbar.py", files=["navbar.py"])
        ]
        mock_s2.write_code.return_value = {
            "navbar.py": fixed_navbar_code,
        }

        runtime = BrainFrogRuntime(
            repo_dir=phase3_repo,
            persist_sessions=False,
            default_test_cmd=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
            system1_factory=lambda b: mock_s1,
            system2_factory=lambda **k: mock_s2,
        )

        request = TaskRequest(
            task_id="task_phase3_success",
            user_request="Fix the broken mobile navbar toggle and make tests pass",
            workspace=phase3_repo,
            test_command=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
        )
        res: TaskResult = runtime.execute_autonomous_task(request)

        # 1. Verification & Outcome assertions
        assert res.success is True
        assert res.verified is True
        assert res.status == "COMMITTED"
        assert res.tests_passed is True
        assert "navbar.py" in res.changed_files

        # 2. Filesystem state assertions
        assert "self.is_open = not self.is_open" in navbar_file.read_text(encoding="utf-8")
        assert user_file.read_text(encoding="utf-8") == original_user_content

        # 3. Independent test suite verification: tests now genuinely pass on disk!
        verify_proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
            cwd=str(phase3_repo),
            capture_output=True,
            text=True,
        )
        assert verify_proc.returncode == 0
        assert "passed" in verify_proc.stdout

        # 4. Observability formatting check
        report = res.format_report()
        assert "Status: VERIFIED" in report
        assert "Plan:" in report
        assert "Changed:" in report
        assert "navbar.py" in report
        assert "Transaction:\ncommitted" in report or "Transaction:\nCOMMITTED" in report

    def test_e2e_autonomous_failure_rolls_back(self, phase3_repo: Path):
        """Scenario 2: When tests fail and retries exhaust, changes roll back cleanly to ROLLED_BACK."""
        navbar_file = phase3_repo / "navbar.py"
        original_navbar_content = navbar_file.read_text(encoding="utf-8")
        user_file = phase3_repo / "unrelated_user.py"
        original_user_content = user_file.read_text(encoding="utf-8")

        runtime = BrainFrogRuntime(
            repo_dir=phase3_repo,
            persist_sessions=False,
            default_test_cmd=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
        )

        mock_s1 = MagicMock()
        mock_s1.name = "jev_mock"
        mock_s1.decide.return_value = {
            "likely_domain": Answer(choice="ui", confidence=0.9),
            "change_type": Answer(choice="code_change", confidence=0.9),
            "is_sensitive": Answer(noul=0.0),
            "complexity": Answer(score="low", confidence=0.9),
            "needs_tests": Answer(noul=0.8),
            "tests_passing": Answer(choice="no", noul=0.0),
            "diff_complete": Answer(choice="no", noul=0.0),
            "failure_fixable": Answer(choice="no", noul=0.0),
            "retry_concern": Answer(score="high", confidence=0.0),
        }

        mock_s2 = MagicMock()
        mock_s2.provider_name = "claude_mock"
        mock_s2.plan_task.return_value = [
            PlanStep(id="1", description="Attempt fix in navbar.py", files=["navbar.py"])
        ]
        # Ineffective change that still fails domain tests
        mock_s2.write_code.return_value = {
            "navbar.py": original_navbar_content + "\n# Still broken comment\n",
        }
        mock_s2.review_and_fix.return_value = {
            "navbar.py": original_navbar_content + "\n# Still broken retry\n",
        }

        runtime = BrainFrogRuntime(
            repo_dir=phase3_repo,
            persist_sessions=False,
            default_test_cmd=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
            system1_factory=lambda b: mock_s1,
            system2_factory=lambda **k: mock_s2,
        )

        request = TaskRequest(
            task_id="task_phase3_fail",
            user_request="Attempt navbar fix",
            workspace=phase3_repo,
            test_command=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
            max_retries=1,
        )
        res: TaskResult = runtime.execute_autonomous_task(request)

        # Assertions
        assert res.success is False
        assert res.verified is False
        assert res.status == "ROLLED_BACK"
        assert res.tests_passed is False

        # Filesystem restored: broken mutation completely rolled back!
        assert navbar_file.read_text(encoding="utf-8") == original_navbar_content
        assert user_file.read_text(encoding="utf-8") == original_user_content

    def test_e2e_pre_existing_user_modifications_preserved_on_failure(self, phase3_repo: Path):
        """Scenario 3: Pre-existing user modifications survive autonomous failure and rollback."""
        user_notes = phase3_repo / "user_notes.py"
        user_dirty_text = "DIRTY_USER_WORK_IN_PROGRESS = 12345\n"
        user_notes.write_text(user_dirty_text, encoding="utf-8")

        navbar_file = phase3_repo / "navbar.py"
        original_navbar = navbar_file.read_text(encoding="utf-8")

        mock_s1 = MagicMock()
        mock_s1.decide.return_value = {
            "likely_domain": Answer(choice="ui", confidence=0.9),
            "change_type": Answer(choice="code_change", confidence=0.9),
            "is_sensitive": Answer(noul=0.0),
            "complexity": Answer(score="low", confidence=0.9),
            "needs_tests": Answer(noul=0.8),
            "tests_passing": Answer(choice="no", noul=0.0),
            "diff_complete": Answer(choice="no", noul=0.0),
            "failure_fixable": Answer(choice="no", noul=0.0),
            "retry_concern": Answer(score="high", confidence=0.0),
        }

        mock_s2 = MagicMock()
        mock_s2.plan_task.return_value = [
            PlanStep(id="1", description="Modify navbar", files=["navbar.py"])
        ]
        mock_s2.write_code.return_value = {
            "navbar.py": "def broken(): pass",
        }

        runtime = BrainFrogRuntime(
            repo_dir=phase3_repo,
            persist_sessions=False,
            default_test_cmd=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
            system1_factory=lambda b: mock_s1,
            system2_factory=lambda **k: mock_s2,
        )

        request = TaskRequest(
            task_id="task_user_preserve",
            user_request="Modify navbar",
            workspace=phase3_repo,
            test_command=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
            max_retries=1,
        )
        res: TaskResult = runtime.execute_autonomous_task(request)

        assert res.success is False
        assert res.status == "ROLLED_BACK"
        # User uncommitted file was NOT wiped out by git clean/checkout
        assert user_notes.read_text(encoding="utf-8") == user_dirty_text
        assert navbar_file.read_text(encoding="utf-8") == original_navbar

    def test_e2e_code_side_scope_enforcement_rejects_unplanned_file(self, phase3_repo: Path):
        """Scenario 4: If model attempts to modify a file outside the planned step scope, code rejects it."""
        unrelated_file = phase3_repo / "unrelated_user.py"
        original_unrelated = unrelated_file.read_text(encoding="utf-8")

        mock_s1 = MagicMock()
        mock_s1.decide.return_value = {
            "likely_domain": Answer(choice="ui", confidence=0.9),
            "change_type": Answer(choice="code_change", confidence=0.9),
            "is_sensitive": Answer(noul=0.0),
            "complexity": Answer(score="low", confidence=0.9),
            "needs_tests": Answer(noul=0.8),
        }

        mock_s2 = MagicMock()
        # Step scope only permits navbar.py
        mock_s2.plan_task.return_value = [
            PlanStep(id="1", description="Fix navbar", files=["navbar.py"])
        ]
        # Rogue model attempts to modify unrelated_user.py as well
        mock_s2.write_code.return_value = {
            "navbar.py": "x = 1",
            "unrelated_user.py": "COMPROMISED_CODE = True",
        }

        runtime = BrainFrogRuntime(
            repo_dir=phase3_repo,
            persist_sessions=False,
            default_test_cmd=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
            system1_factory=lambda b: mock_s1,
            system2_factory=lambda **k: mock_s2,
        )

        request = TaskRequest(
            task_id="task_scope_violation",
            user_request="Fix navbar",
            workspace=phase3_repo,
            test_command=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
        )
        res = runtime.execute_autonomous_task(request)

        # Fails closed, never commits
        assert res.success is False
        assert res.verified is False
        assert res.status in ("FAILED", "ROLLED_BACK")
        # Unrelated file was untouched!
        assert unrelated_file.read_text(encoding="utf-8") == original_unrelated

    def test_e2e_allowed_scope_task_level_enforcement(self, phase3_repo: Path):
        """Scenario 5: Task-level allowed_scope prevents writes to non-whitelisted paths."""
        mock_s1 = MagicMock()
        mock_s1.decide.return_value = {
            "likely_domain": Answer(choice="ui", confidence=0.9),
            "change_type": Answer(choice="code_change", confidence=0.9),
            "is_sensitive": Answer(noul=0.0),
            "complexity": Answer(score="low", confidence=0.9),
            "needs_tests": Answer(noul=0.8),
        }

        mock_s2 = MagicMock()
        mock_s2.plan_task.return_value = [
            PlanStep(id="1", description="Touch auth", files=["auth.py"])
        ]
        mock_s2.write_code.return_value = {
            "auth.py": "TOKEN = 123",
        }

        runtime = BrainFrogRuntime(
            repo_dir=phase3_repo,
            persist_sessions=False,
            default_test_cmd=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
            system1_factory=lambda b: mock_s1,
            system2_factory=lambda **k: mock_s2,
        )

        request = TaskRequest(
            task_id="task_whitelist_scope",
            user_request="Modify auth",
            workspace=phase3_repo,
            # Whitelist ONLY navbar.py
            allowed_scope=["navbar.py"],
            test_command=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
        )
        res = runtime.execute_autonomous_task(request)

        assert res.success is False
        assert res.verified is False
        assert not (phase3_repo / "auth.py").exists()

    def test_e2e_verification_gate_failure_blocks_commit(self, phase3_repo: Path):
        """Scenario 6: When tests pass but verification fails, transaction rolls back without commit."""
        class FailingVerifier:
            def verify(self, tx: Any, workspace: Path) -> tuple[bool, str, dict[str, Any]]:
                return False, "Filesystem consistency checksum mismatch", {}

        mock_s1 = MagicMock()
        mock_s1.decide.return_value = {
            "likely_domain": Answer(choice="ui", confidence=0.9),
            "change_type": Answer(choice="code_change", confidence=0.9),
            "is_sensitive": Answer(noul=0.0),
            "complexity": Answer(score="low", confidence=0.9),
            "needs_tests": Answer(noul=0.8),
            "tests_passing": Answer(choice="yes", noul=1.0),
            "diff_complete": Answer(choice="yes", noul=1.0),
            "failure_fixable": Answer(choice="no", noul=0.0),
            "retry_concern": Answer(score="within_normal", confidence=0.95),
        }

        mock_s2 = MagicMock()
        mock_s2.plan_task.return_value = [
            PlanStep(id="1", description="Fix navbar", files=["navbar.py"])
        ]
        mock_s2.write_code.return_value = {
            "navbar.py": "class MobileNavbar: pass",
        }

        runtime = BrainFrogRuntime(
            repo_dir=phase3_repo,
            persist_sessions=False,
            transaction_verifier=FailingVerifier(),
            default_test_cmd=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
            system1_factory=lambda b: mock_s1,
            system2_factory=lambda **k: mock_s2,
        )

        with patch("orchestrator._run", return_value=HardenedProcessResult(["pytest"], 0, "OK", "")):
            request = TaskRequest(
                task_id="task_verify_fail",
                user_request="Fix navbar",
                workspace=phase3_repo,
            )
            res = runtime.execute_autonomous_task(request)

        assert res.success is False
        assert res.verified is False
        assert res.status == "ROLLED_BACK"

    def test_e2e_cancellation_during_execution(self, phase3_repo: Path):
        """Scenario 7: Cancellation terminates process tree, rolls back transaction, releases lock."""
        mock_s1 = MagicMock()
        mock_s2 = MagicMock()
        runtime = BrainFrogRuntime(
            repo_dir=phase3_repo,
            persist_sessions=False,
            system1_factory=lambda b: mock_s1,
            system2_factory=lambda **k: mock_s2,
        )

        with patch("orchestrator.Orchestrator.run", side_effect=KeyboardInterrupt("User cancelled")):
            request = TaskRequest(
                task_id="task_cancelled",
                user_request="Long task",
                workspace=phase3_repo,
                metadata={"catch_cancellation": True},
            )
            res = runtime.execute_autonomous_task(request)

        assert res.success is False
        assert res.verified is False
        assert res.status == "CANCELLED"

    def test_e2e_phase1_trust_boundary_enforced(self, phase3_repo: Path):
        """Scenario 8: Unauthorized remote channel request is rejected before reaching orchestrator."""
        runtime = BrainFrogRuntime(
            repo_dir=phase3_repo,
            persist_sessions=False,
        )

        request = TaskRequest(
            task_id="task_remote_untrusted",
            user_request="git push origin main --force",
            workspace=phase3_repo,
            channel="telegram",  # Remote channel
            metadata={"trust_level": ChannelTrustLevel.REMOTE_CHANNEL.value},
        )
        res = runtime.execute_autonomous_task(request)

        assert res.success is False
        assert res.verified is False
        assert res.status == "REJECTED"

    def test_task_result_invariant_never_success_without_verification(self):
        """Scenario 9: Core invariant: TaskResult cannot be instantiated with success=True and verified=False."""
        with pytest.raises(ValueError, match="Autonomous coding task cannot report success=True when verified=False"):
            TaskResult(
                task_id="t1",
                user_request="req",
                success=True,
                verified=False,  # ILLEGAL COMBINATION
                status="COMMITTED",
                summary="done",
            )

    def test_task_plan_and_step_contracts(self):
        """Scenario 10: TaskPlan and TaskStep maintain immutable input validation."""
        with pytest.raises(ValueError, match="TaskStep id must not be empty"):
            TaskStep(id="", description="desc")

        with pytest.raises(ValueError, match="TaskPlan goal must not be empty"):
            TaskPlan(goal="", steps=[TaskStep(id="1", description="desc")])

        with pytest.raises(ValueError, match="TaskPlan must contain at least one step"):
            TaskPlan(goal="goal", steps=[])
