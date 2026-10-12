"""Reproducible Demo for BrainFrog Verification Proof UX.

Implements BrainFrog Sprint 1.2: Reproducible Demo.
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
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Tuple

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

from core.runtime.process import run_hardened_subprocess
from core.runtime.proof import (
    ChangeRecord,
    ChangesEvidence,
    FileProofStore,
    ProcessEvidence,
    ProofArtifact,
    ProofVerdict,
    ReproducibilityEvidence,
    TaskEvidence,
    VerificationEvidence,
    capture_bounded_output,
    compute_proof_verdict,
)
from core.runtime.proof_inspection import inspect_proof, print_proof_inspection
from core.runtime.proof_markdown import render_proof_markdown
from core.runtime.task_finalization import TaskFinalizationCoordinator
from core.runtime.transaction import (
    FileTransactionStore,
    TransactionCoordinator,
    TransactionStatus,
)


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


def run_scenario_1_verified(ws: Path, commit_sha: str) -> str:
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

    # 2. Stage changes within an isolated TransactionCoordinator
    tx_store = FileTransactionStore(ws)
    coord = TransactionCoordinator(ws, store=tx_store, task_id=task_id)

    # Apply implementation of multiply
    new_code = (
        "def add(a: int, b: int) -> int:\n    return a + b\n\n"
        "def multiply(a: int, b: int) -> int:\n    return a * b\n"
    )
    coord.stage_modify("math_utils.py", new_code)
    executed = coord.execute()
    assert executed is True

    # 3. Execute real test suite via subprocess
    test_cmd = [sys.executable, "-m", "unittest", "tests/test_math_utils.py"]
    test_res = run_hardened_subprocess(test_cmd, cwd=ws, timeout=10.0)

    print(f"Executed test command: {' '.join(test_cmd)}")
    print(f"Test exit code: {test_res.returncode}")
    print(f"Captured stdout:\n{test_res.stdout.strip()}")

    assert test_res.returncode == 0, f"Expected real unittest to pass, got exit code {test_res.returncode}"

    # 4. Verify and commit transaction
    verified = coord.verify()
    assert verified is True
    tx_res = coord.commit()
    assert tx_res.committed is True

    # 5. Build and save durable proof artifact
    proof_store = FileProofStore(ws)
    finalizer = TaskFinalizationCoordinator(ws)
    finalizer.record_commit_intent(task_id, transaction_id=tx_res.transaction_id)
    finalizer.record_commit_success(task_id, commit_sha=commit_sha)

    from core.runtime.proof import build_change_records, capture_git_provenance
    change_records = build_change_records(tx_res.operations, ws)
    git_prov = capture_git_provenance(ws, initial_head_sha=commit_sha, commit_sha=commit_sha)

    verdict = compute_proof_verdict(
        tx_status="COMMITTED",
        tests_passed=True,
        gate_passed=True,
        gate_details={
            "workspace_scope": True,
            "test_suite": True,
            "transaction_integrity": True,
            "execution_integrity": True,
            "trust_integrity": True,
        },
    )

    proof = ProofArtifact(
        task=TaskEvidence(
            task_id=task_id,
            session_id="session_demo_01",
            description="Implement multiply function in math_utils.py with passing unit tests",
            created_at="2026-10-12T00:00:00Z",
            completed_at="2026-10-12T00:00:02Z",
            duration_ms=1850,
        ),
        verdict=verdict,
        provenance=git_prov,
        verification=VerificationEvidence(
            test_command="python -m unittest tests/test_math_utils.py",
            exit_code=0,
            stdout_summary=test_res.stdout.strip(),
            stderr_summary=test_res.stderr.strip(),
            gate_checks={
                "workspace_scope": True,
                "test_suite": True,
                "transaction_integrity": True,
                "execution_integrity": True,
                "trust_integrity": True,
            },
            process_evidence=ProcessEvidence(
                task_id=task_id,
                transaction_id=tx_res.transaction_id,
                command="python -m unittest tests/test_math_utils.py",
                returncode=0,
                status="success",
                duration_ms=1850,
            ),
        ),
        changes=ChangesEvidence(
            transaction_id=tx_res.transaction_id,
            status="COMMITTED",
            files=change_records,
        ),
        reproducibility=ReproducibilityEvidence(
            command="python -m unittest tests/test_math_utils.py",
            expected_exit_code=0,
            expected_file_hashes={r.path: r.after_sha256 for r in change_records if r.after_sha256},
        ),
    )

    md_content = render_proof_markdown(proof)
    proof_store.save(proof, markdown_content=md_content)
    finalizer.finalize_proof(task_id)

    # 6. Run proof inspection
    res = inspect_proof(ws, task_id, check_workspace=True)
    print_proof_inspection(res)

    assert res.is_verified is True
    assert res.exit_code == 0
    return task_id


def run_scenario_2_failed_rollback(ws: Path, commit_sha: str) -> str:
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

    # Begin transaction
    tx_store = FileTransactionStore(ws)
    coord = TransactionCoordinator(ws, store=tx_store, task_id=task_id)

    # Stage buggy implementation returning 0
    faulty_code = pre_content + "\ndef divide(a: int, b: int) -> int:\n    return 0  # BUGGY\n"
    coord.stage_modify("math_utils.py", faulty_code)
    executed = coord.execute()
    assert executed is True

    # Execute real test suite
    test_cmd = [sys.executable, "-m", "unittest", "tests/test_math_utils.py"]
    test_res = run_hardened_subprocess(test_cmd, cwd=ws, timeout=10.0)

    print(f"Executed test command: {' '.join(test_cmd)}")
    print(f"Test exit code: {test_res.returncode}")
    print(f"Captured stderr:\n{test_res.stderr.strip()}")

    assert test_res.returncode != 0, "Test suite was expected to fail"

    # Roll back transaction because tests failed
    tx_res = coord.rollback(reason=f"Tests failed with exit code {test_res.returncode}")
    assert tx_res.rolled_back is True

    # Confirm disk state is cleanly restored
    current_content = src_file.read_text(encoding="utf-8")
    assert current_content == pre_content, "Rollback failed to restore pre-task file content!"
    print("Confirmed: Workspace filesystem was cleanly rolled back.")

    # Record proof of failure
    proof_store = FileProofStore(ws)

    from core.runtime.proof import build_change_records, capture_git_provenance
    change_records = build_change_records(tx_res.operations, ws)
    git_prov = capture_git_provenance(ws, initial_head_sha=commit_sha, commit_sha=commit_sha)

    verdict = compute_proof_verdict(
        tx_status="ROLLED_BACK",
        tests_passed=False,
        gate_passed=False,
        gate_details={
            "workspace_scope": True,
            "test_suite": False,
            "transaction_integrity": True,
            "execution_integrity": True,
            "trust_integrity": True,
        },
    )

    proof = ProofArtifact(
        task=TaskEvidence(
            task_id=task_id,
            session_id="session_demo_02",
            description="Implement divide function (fails unit tests and rolls back)",
            created_at="2026-10-12T00:01:00Z",
            completed_at="2026-10-12T00:01:02Z",
            duration_ms=1420,
        ),
        verdict=verdict,
        provenance=git_prov,
        verification=VerificationEvidence(
            test_command="python -m unittest tests/test_math_utils.py",
            exit_code=test_res.returncode,
            stdout_summary=test_res.stdout.strip(),
            stderr_summary=test_res.stderr.strip(),
            gate_checks={
                "workspace_scope": True,
                "test_suite": False,
                "transaction_integrity": True,
                "execution_integrity": True,
                "trust_integrity": True,
            },
            process_evidence=ProcessEvidence(
                task_id=task_id,
                transaction_id=tx_res.transaction_id,
                command="python -m unittest tests/test_math_utils.py",
                returncode=test_res.returncode,
                status="failed",
                duration_ms=1420,
            ),
        ),
        changes=ChangesEvidence(
            transaction_id=tx_res.transaction_id,
            status="ROLLED_BACK",
            files=[],
        ),
        reproducibility=ReproducibilityEvidence(
            command="python -m unittest tests/test_math_utils.py",
            expected_exit_code=1,
        ),
    )

    md_content = render_proof_markdown(proof)
    proof_store.save(proof, markdown_content=md_content)

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
        commit_sha = _init_git_workspace(ws)

        # Run the 3 scenarios sequentially
        task_1_id = run_scenario_1_verified(ws, commit_sha)
        task_2_id = run_scenario_2_failed_rollback(ws, commit_sha)
        run_scenario_3_tamper_detection(ws, task_1_id)

        print("\n" + "=" * 70)
        print("✓ All 3 Reproducible Demo Scenarios Executed and Verified Successfully!")
        print("=" * 70)
        return 0
    finally:
        if clean_up and ws.exists():
            shutil.rmtree(ws, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
