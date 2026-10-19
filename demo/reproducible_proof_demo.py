"""Reproducible Demo for BrainFrog Verification Proof UX.

Implements BrainFrog Sprint 1.2 & 1.3B: Reproducible Demo with Canonical Workflow.
Product thesis: "Don't just trust the agent. Verify it."

Demonstrates three concrete, verifiable scenarios in an isolated workspace:
1. Scenario 1: Successful code modification verified by real unittest test suite -> VERIFIED proof
2. Scenario 2: Failing test suite with truthful failure reporting and clean rollback -> FAILED proof
3. Scenario 3: Post-proof workspace disk modification detected by integrity check -> INVALID / MISMATCH

Usage:
    python demo/reproducible_proof_demo.py [--workspace /path/to/dir]
"""
from __future__ import annotations

import argparse
import dataclasses
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure UTF-8 output on Windows consoles
if sys.platform == "win32":
    try:
        for _stream in (sys.stdout, sys.stderr):
            _reconfig = getattr(_stream, "reconfigure", None)
            if callable(_reconfig):
                _reconfig(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Ensure package root is on sys.path
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.runtime import (
    BrainFrogRuntime,
    TaskRequest,
    TaskResult,
)
from core.runtime.proof_inspection import inspect_proof, print_proof_inspection
from system1.base import Answer
from system2 import PlanStep


def remove_readonly(func: Any, path: str, exc: Any) -> None:
    """Clear the read-only attribute on Windows files/directories and retry removal."""
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        func(path)
    except Exception:
        raise


def cleanup_temporary_workspace(ws: Path) -> None:
    """Clean up temporary workspace reliably across platforms.

    Handles read-only files (e.g. .git/objects on Windows).
    Surfaces a clear RuntimeError if cleanup fails so failures are not silently ignored.
    """
    if not ws.exists():
        return
    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(ws, onexc=remove_readonly)
        else:
            shutil.rmtree(ws, onerror=lambda f, p, e: remove_readonly(f, p, e[1]))
    except Exception as exc:
        if ws.exists():
            raise RuntimeError(
                f"Failed to cleanly remove temporary workspace at '{ws}': {exc}"
            ) from exc


class DeterministicDemoSystem1:
    """Deterministic System 1 client for reproducible demo execution."""

    def __init__(self, name: str = "jev_demo_deterministic") -> None:
        self.name = name

    def decide(self, state: Dict[str, Any], questions: Dict[str, Any]) -> Dict[str, Answer]:
        answers: Dict[str, Answer] = {}
        for q_name in questions:
            if q_name == "likely_domain":
                answers[q_name] = Answer(choice="math", confidence=0.95)
            elif q_name == "change_type":
                answers[q_name] = Answer(choice="code_change", confidence=0.95)
            elif q_name == "is_sensitive":
                answers[q_name] = Answer(noul=0.0)
            elif q_name == "complexity":
                answers[q_name] = Answer(score="low", confidence=0.95)
            elif q_name == "needs_tests":
                answers[q_name] = Answer(noul=0.8)
            elif q_name == "tests_passing":
                test_passed = bool(state.get("test_passed", True))
                answers[q_name] = Answer(
                    choice="yes" if test_passed else "no",
                    noul=1.0 if test_passed else 0.0,
                )
            elif q_name == "diff_complete":
                answers[q_name] = Answer(choice="yes", noul=1.0)
            elif q_name == "failure_fixable":
                answers[q_name] = Answer(choice="no", noul=0.0)
            elif q_name == "retry_concern":
                answers[q_name] = Answer(score="within_normal", confidence=0.95)
            elif q_name == "diff_risk":
                answers[q_name] = Answer(score=0, choice="low", confidence=0.95)
            elif q_name == "safe_to_proceed":
                answers[q_name] = Answer(noul=1.0, choice="yes")
        return answers


class DeterministicDemoSystem2:
    """Deterministic System 2 client for proposing code modifications."""

    def __init__(
        self,
        plan_desc: str,
        code_proposal: Dict[str, str],
        retry_proposal: Optional[Dict[str, str]] = None,
        pr_title: str = "feat(math): update math_utils",
    ) -> None:
        self.provider_name = "claude_demo_deterministic"
        self.guidelines = ""
        self._plan_desc = plan_desc
        self._code_proposal = code_proposal
        self._retry_proposal = retry_proposal or code_proposal
        self._pr_title = pr_title

    def plan_task(self, *args: Any, **kwargs: Any) -> List[PlanStep]:
        return [
            PlanStep(
                id="1",
                description=self._plan_desc,
                files=list(self._code_proposal.keys()),
            )
        ]

    def write_code(self, *args: Any, **kwargs: Any) -> Dict[str, str]:
        return dict(self._code_proposal)

    def review_and_fix(self, *args: Any, **kwargs: Any) -> Dict[str, str]:
        return dict(self._retry_proposal)

    def draft_pr(self, *args: Any, **kwargs: Any) -> Dict[str, str]:
        return {
            "title": self._pr_title,
            "body": f"Automated PR proposal for: {self._plan_desc}",
        }


def _init_git_workspace(ws: Path) -> str:
    """Initialize a git repository in the workspace."""
    subprocess.run(["git", "init"], cwd=ws, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "BrainFrog Demo"], cwd=ws, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "demo@brainfrog.dev"], cwd=ws, capture_output=True, check=True)

    readme = ws / "README.md"
    readme.write_text("# Math Demo Workspace\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=ws, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=ws, capture_output=True, check=True)

    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ws, capture_output=True, text=True, check=True)
    return res.stdout.strip()


def _execute_with_approval(runtime: BrainFrogRuntime, task_req: TaskRequest, target_file: str) -> TaskResult:
    """Drive the canonical approval -> contract -> execution flow (Phase 1.6, two-man rule).

    Build-mode mutation fails closed without an ApprovedExecutionContract, so the demo
    faithfully shows the production flow: request approval, approve as a distinct local
    operator identity, then execute the approved operation.
    """
    approval_meta = dict(task_req.metadata or {})
    approval_meta.update({
        "require_approval": True,
        "requested_action": "write_code",
        "target": target_file,
        "user_id": "local",
    })

    gated_res = runtime.execute_autonomous_task(dataclasses.replace(task_req, metadata=approval_meta))
    if gated_res.status != "REJECTED":
        raise RuntimeError(f"Expected approval gating before execution, got status={gated_res.status}")
    print("Approval required: exact operation queued for human sign-off.")

    pending = [
        req for req in runtime.approval_service.store.list_requests(active_only=True)
        if getattr(req.canonical_operation, "target", "") == target_file
        and str(getattr(req.status, "value", req.status)).lower() == "pending"
    ]
    if not pending:
        raise RuntimeError("Expected a pending approval request after gating")
    request = max(pending, key=lambda req: getattr(req, "created_at", 0.0))

    ok, msg, _ = runtime.approval_service.approve(
        request_id=request.request_id,
        approver_id="operator",
        channel=task_req.channel or "cli",
        session_id=request.session_id,
    )
    if not ok:
        raise RuntimeError(f"Approval transition failed: {msg}")
    print(f"Approved request {request.request_id[:12]}... by distinct operator identity 'operator'.")

    approved_meta = dict(approval_meta)
    approved_meta["approval_id"] = request.request_id
    return runtime.execute_autonomous_task(dataclasses.replace(task_req, metadata=approved_meta))


def run_scenario_1_verified(ws: Path) -> str:
    """Scenario 1: Successful change with actual test command and VERIFIED proof."""
    print("\n" + "=" * 70)
    print("[SCENARIO 1] Successful change with real test suite -> VERIFIED Proof")
    print("=" * 70)

    task_id = "task_demo_01_verified"

    # 1. Setup initial project files
    src_file = ws / "math_utils.py"
    src_file.write_text("def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")

    tests_dir = ws / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    test_file = tests_dir / "test_math_utils.py"
    test_file.write_text(
        "import unittest\n"
        "from math_utils import add, multiply\n\n"
        "class TestMath(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n"
        "    def test_multiply(self):\n"
        "        self.assertEqual(multiply(3, 4), 12)\n\n"
        "if __name__ == '__main__':\n"
        "    unittest.main()\n",
        encoding="utf-8",
    )

    subprocess.run(["git", "add", "."], cwd=ws, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "Initial test suite requiring multiply"], cwd=ws, capture_output=True, check=True)

    test_cmd = [sys.executable, "-m", "unittest", "tests/test_math_utils.py"]

    # 2. Wire and execute via canonical BrainFrogRuntime workflow
    s1 = DeterministicDemoSystem1()
    s2 = DeterministicDemoSystem2(
        plan_desc="Implement multiply function in math_utils.py",
        code_proposal={
            "math_utils.py": (
                "def add(a: int, b: int) -> int:\n    return a + b\n\n"
                "def multiply(a: int, b: int) -> int:\n    return a * b\n"
            )
        },
        pr_title="feat(math): implement multiply function",
    )
    runtime = BrainFrogRuntime(
        repo_dir=ws,
        persist_sessions=False,
        default_test_cmd=test_cmd,
        system1_factory=lambda b: s1,
        system2_factory=lambda **k: s2,
    )

    task_req = TaskRequest(
        task_id=task_id,
        user_request="Implement multiply function in math_utils.py with passing unit tests",
        workspace=ws,
        test_command=test_cmd,
    )

    print(f"Executing canonical production workflow for '{task_id}' via BrainFrogRuntime...")
    task_res: TaskResult = _execute_with_approval(runtime, task_req, "math_utils.py")

    print(f"Workflow outcome: status={task_res.status}, success={task_res.success}, verified={task_res.verified}")
    assert task_res.success is True, f"Expected task to succeed, got: {task_res.summary}"
    assert task_res.verified is True, f"Expected task to be verified, got: {task_res.failure_reason}"
    assert task_res.status == "COMMITTED"
    assert task_res.proof_path is not None, "Expected proof artifact to be generated and persisted"

    # 3. Run proof inspection UX
    res = inspect_proof(ws, task_id, check_workspace=True)
    print_proof_inspection(res)

    assert res.is_verified is True
    assert res.exit_code == 0
    return task_id


def run_scenario_2_failed_rollback(ws: Path) -> str:
    """Scenario 2: Failing test with truthful failure reporting and rollback behavior."""
    print("\n" + "=" * 70)
    print("[SCENARIO 2] Failing test suite -> Truthful Failure & Rollback")
    print("=" * 70)

    task_id = "task_demo_02_failed"

    # Pre-task state of math_utils.py
    src_file = ws / "math_utils.py"
    pre_content = src_file.read_text(encoding="utf-8")

    # Update test file to require divide function
    test_file = ws / "tests" / "test_math_utils.py"
    test_file.write_text(
        "import unittest\n"
        "from math_utils import add, multiply, divide\n\n"
        "class TestMath(unittest.TestCase):\n"
        "    def test_divide(self):\n"
        "        self.assertEqual(divide(10, 2), 5)\n\n"
        "if __name__ == '__main__':\n"
        "    unittest.main()\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "tests/test_math_utils.py"], cwd=ws, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "Require divide function in tests"], cwd=ws, capture_output=True, check=True)

    test_cmd = [sys.executable, "-m", "unittest", "tests/test_math_utils.py"]

    # Wire System 2 that proposes buggy code returning 0
    s1 = DeterministicDemoSystem1()
    s2_fail = DeterministicDemoSystem2(
        plan_desc="Implement divide function (proposes buggy implementation)",
        code_proposal={
            "math_utils.py": (
                pre_content + "\ndef divide(a: int, b: int) -> int:\n    return 0  # BUGGY: always returns 0\n"
            )
        },
        retry_proposal={
            "math_utils.py": (
                pre_content + "\ndef divide(a: int, b: int) -> int:\n    return 0  # STILL BUGGY\n"
            )
        },
        pr_title="feat(math): add buggy divide function",
    )

    runtime = BrainFrogRuntime(
        repo_dir=ws,
        persist_sessions=False,
        default_test_cmd=test_cmd,
        system1_factory=lambda b: s1,
        system2_factory=lambda **k: s2_fail,
    )

    task_req = TaskRequest(
        task_id=task_id,
        user_request="Implement divide function (fails unit tests and rolls back)",
        workspace=ws,
        test_command=test_cmd,
        max_retries=1,
    )

    print(f"Executing canonical production workflow for '{task_id}' via BrainFrogRuntime...")
    task_res: TaskResult = _execute_with_approval(runtime, task_req, "math_utils.py")

    print(f"Workflow outcome: status={task_res.status}, success={task_res.success}, verified={task_res.verified}")
    assert task_res.success is False, "Expected task to fail due to failing unit tests"
    assert task_res.verified is False
    assert task_res.status == "ROLLED_BACK", f"Expected transaction to be ROLLED_BACK, got: {task_res.status}"
    assert task_res.proof_path is not None, "Expected durable proof of failure to be persisted"

    # Confirm disk state is cleanly restored by TransactionCoordinator rollback
    current_content = src_file.read_text(encoding="utf-8")
    assert current_content == pre_content, "Rollback failed to restore pre-task file content!"
    print("Confirmed: Workspace filesystem was cleanly rolled back.")

    # Run proof inspection
    res = inspect_proof(ws, task_id, check_workspace=True)
    print_proof_inspection(res)

    assert res.is_failed is True
    assert res.exit_code == 3
    return task_id


def run_scenario_3_tamper_detection(ws: Path, verified_task_id: str) -> None:
    """Scenario 3: Post-proof workspace modification detected by integrity verification."""
    print("\n" + "=" * 70)
    print("[SCENARIO 3] Post-proof file modification -> Integrity MISMATCH Detected")
    print("=" * 70)

    src_file = ws / "math_utils.py"
    print(f"Modifying '{src_file}' on disk after proof generation...")
    src_file.write_text(
        "# UNVERIFIED MODIFICATION AFTER PROOF GENERATION\n"
        "def multiply(a, b):\n    return 999999\n",
        encoding="utf-8",
    )

    print("Running proof inspection with workspace disk integrity verification (--check-workspace)...")
    res = inspect_proof(ws, verified_task_id, check_workspace=True)
    print_proof_inspection(res)

    print(f"Re-verification status: {res.report.overall_status.value}")
    print(f"Workspace integrity passed: {res.workspace_integrity_passed}")
    print(f"Process exit code: {res.exit_code}")

    assert res.is_invalid is True
    assert res.workspace_integrity_passed is False
    assert res.exit_code == 1
    print("Confirmed: Disk modification detected! Proof verification returned INVALID (exit code 1).")


def main() -> int:
    parser = argparse.ArgumentParser(description="BrainFrog Proof UX Reproducible Demo")
    parser.add_argument("--workspace", default=None, help="Path to workspace directory (default: clean tempdir)")
    args = parser.parse_args()

    clean_up = False
    if args.workspace:
        ws = Path(args.workspace).resolve()
        ws.mkdir(parents=True, exist_ok=True)
    else:
        temp_dir = tempfile.mkdtemp(prefix="brainfrog_proof_demo_")
        ws = Path(temp_dir).resolve()
        clean_up = True

    try:
        print(f"Using isolated demo workspace: {ws}")
        _init_git_workspace(ws)

        # Run the 3 scenarios sequentially
        task_1_id = run_scenario_1_verified(ws)
        task_2_id = run_scenario_2_failed_rollback(ws)
        run_scenario_3_tamper_detection(ws, task_1_id)

        print("\n" + "=" * 70)
        print("✓ All 3 Reproducible Demo Scenarios Executed and Verified Successfully!")
        print("=" * 70)
        return 0
    finally:
        if clean_up and ws.exists():
            cleanup_temporary_workspace(ws)


if __name__ == "__main__":
    sys.exit(main())
