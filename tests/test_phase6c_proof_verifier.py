"""Phase 6C Deterministic Proof Verification Interface — Adversarial Regression Suite.

Product Thesis: "Don't just trust the agent. Verify it."

Tests all 13 adversarial scenarios:
1. A valid verified proof.
2. A valid failed/rolled-back proof.
3. Malformed JSON and unsupported schema versions.
4. Missing test evidence with a self-declared VERIFIED verdict.
5. A nonzero test exit paired with a passing gate.
6. Task ID mismatch (filename vs task_id, process_evidence vs task_id).
7. Commit SHA mismatch against available authoritative intent.
8. An unresolved commit intent (unfinalized state in workspace).
9. Committed transaction with failed proof persistence.
10. Contradictory task status and transaction status.
11. Missing or corrupt derived Markdown.
12. Tampered proof fields and unknown status values.
13. Proofs whose historical claims cannot be independently re-established.
And CLI entrypoints and zero-mutation assertions.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from core.runtime.proof_verifier import (
    DeterministicProofVerifier,
    ProofVerificationReport,
    ProofVerificationStatus,
    verify_proof,
)
from core.runtime.task_finalization import (
    CommitIntentRecord,
    CommitIntentStatus,
    TaskFinalizationCoordinator,
)
from brainfrog.sdk.client import BrainFrogClient
from cli import verify_proof_cli, main as cli_main


# -------------------------------------------------------------------------
# Test Helpers & Fixtures
# -------------------------------------------------------------------------
def _init_git_repo(repo_dir: Path) -> str:
    """Initialize an isolated git repository and create an initial commit."""
    subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=repo_dir, capture_output=True, check=True)
    (repo_dir / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_dir, capture_output=True, check=True)
    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True, check=True)
    return res.stdout.strip()


def _snapshot_workspace(workspace: Path) -> Dict[str, str]:
    """Capture relative paths and sha256 hashes of all files in workspace to assert zero mutations."""
    snapshot: Dict[str, str] = {}
    for root, _, files in os.walk(workspace):
        for f in files:
            p = Path(root) / f
            rel = str(p.relative_to(workspace))
            try:
                content = p.read_bytes()
                snapshot[rel] = hashlib.sha256(content).hexdigest()
            except Exception:
                pass
    return snapshot


def _assert_zero_workspace_mutations(workspace: Path, before: Dict[str, str]) -> None:
    """Assert workspace file contents and presence are completely unmodified."""
    after = _snapshot_workspace(workspace)
    assert after == before, f"Workspace mutated during proof verification! Diff: {set(after.items()) ^ set(before.items())}"


def _make_base_proof(
    task_id: str = "task_test001",
    verdict: str = "VERIFIED",
    changes_status: str = "COMMITTED",
    exit_code: int = 0,
    commit_sha: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a standard, structurally complete baseline proof dictionary."""
    sha = commit_sha or "a" * 40
    return {
        "version": "1.0.0",
        "task": {
            "task_id": task_id,
            "description": "Implement authentication validation",
            "context": {"user": "local"},
            "constraints": ["Keep zero external dependencies"],
        },
        "verdict": {
            "status": verdict,
            "reason": "All verification gates passed",
            "confidence": 1.0,
            "timestamp": "2026-10-09T12:00:00Z",
        },
        "verification": {
            "test_command": "pytest -q tests/test_auth.py",
            "exit_code": exit_code,
            "stdout_summary": "1 passed in 0.05s",
            "stderr_summary": "",
            "process_evidence": {
                "command": "pytest -q tests/test_auth.py",
                "returncode": exit_code,
                "timed_out": False,
                "cancelled": False,
                "task_id": task_id,
            },
            "gate_checks": {
                "test_suite": (exit_code == 0 and verdict == "VERIFIED"),
                "workspace_scope": True,
                "transaction_integrity": (changes_status == "COMMITTED"),
                "execution_integrity": True,
                "trust_integrity": True,
            },
        },
        "changes": {
            "status": changes_status,
            "transaction_id": f"tx_{task_id}",
            "files": [
                {
                    "path": "auth.py",
                    "operation": "CREATE",
                    "sha256_before": None,
                    "sha256_after": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                    "bytes_changed": 128,
                }
            ],
            "rollback_snapshot_id": f"rb_{task_id}",
        },
        "provenance": {
            "is_git": True,
            "branch": "main",
            "initial_head_sha": sha,
            "final_head_sha": sha,
            "commit_sha": sha,
        },
        "reproducibility": {
            "environment": {"python_version": "3.12.1", "platform": "win32"},
            "execution_steps": [
                {"step_index": 1, "description": "Write auth.py"},
                {"step_index": 2, "description": "Execute pytest"},
            ],
        },
    }


def _write_proof(workspace: Path, task_id: str, data: Dict[str, Any]) -> Path:
    """Save proof JSON to .brainfrog/proofs/<task_id>.json."""
    proof_dir = workspace / ".brainfrog" / "proofs"
    proof_dir.mkdir(parents=True, exist_ok=True)
    p = proof_dir / f"{task_id}.json"
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return p


def _write_intent(
    workspace: Path,
    task_id: str,
    status: CommitIntentStatus,
    commit_sha: Optional[str] = None,
) -> Path:
    """Save finalization commit intent record directly to .brainfrog/finalization/<task_id>.json."""
    intent_dir = workspace / ".brainfrog" / "finalization"
    intent_dir.mkdir(parents=True, exist_ok=True)
    record = CommitIntentRecord(
        task_id=task_id,
        workspace=str(workspace),
        status=status,
        created_at=time.time(),
        commit_sha=commit_sha,
        pid=12345,
        nonce="testnonce123",
        is_git=True,
    )
    p = intent_dir / f"{task_id}.json"
    p.write_text(json.dumps(record.to_dict(), indent=2), encoding="utf-8")
    return p


# -------------------------------------------------------------------------
# 1. Valid Verified Proof
# -------------------------------------------------------------------------
def test_case_1_valid_verified_proof(tmp_path: Path) -> None:
    commit_sha = _init_git_repo(tmp_path)
    task_id = "task_001_valid"
    data = _make_base_proof(task_id=task_id, commit_sha=commit_sha)
    _write_proof(tmp_path, task_id, data)

    # Derived Markdown companion is also present
    md_path = tmp_path / ".brainfrog" / "proofs" / f"{task_id}.md"
    md_path.write_text(f"# Task Proof {task_id}\n\nVerdict: VERIFIED\n", encoding="utf-8")

    before = _snapshot_workspace(tmp_path)

    # API verification
    verifier = DeterministicProofVerifier(tmp_path)
    report = verifier.verify(task_id)

    assert report.overall_status == ProofVerificationStatus.VALID
    assert report.is_valid is True
    assert report.is_invalid is False
    assert report.is_incomplete is False
    assert report.task_id == task_id
    assert report.verdict_status == "VERIFIED"
    assert report.transaction_status == "COMMITTED"
    assert len(report.reasons) == 0

    # CLI verification exit code
    exit_code = verify_proof_cli(tmp_path, task_id)
    assert exit_code == 0

    _assert_zero_workspace_mutations(tmp_path, before)


# -------------------------------------------------------------------------
# 2. Valid Failed / Rolled-Back Proof
# -------------------------------------------------------------------------
def test_case_2_valid_failed_rolled_back_proof(tmp_path: Path) -> None:
    commit_sha = _init_git_repo(tmp_path)
    task_id = "task_002_failed_rollback"
    data = _make_base_proof(
        task_id=task_id,
        verdict="FAILED",
        changes_status="ROLLED_BACK",
        exit_code=1,
        commit_sha=commit_sha,
    )
    data["verdict"]["reason"] = "Tests failed with exit code 1; changes cleanly rolled back."
    data["verification"]["gate_checks"]["test_suite"] = False
    data["verification"]["gate_checks"]["transaction_integrity"] = True

    _write_proof(tmp_path, task_id, data)
    before = _snapshot_workspace(tmp_path)

    verifier = DeterministicProofVerifier(tmp_path)
    report = verifier.verify(task_id)

    assert report.overall_status == ProofVerificationStatus.VALID
    assert report.is_valid is True
    assert report.verdict_status == "FAILED"
    assert report.transaction_status == "ROLLED_BACK"

    exit_code = verify_proof_cli(tmp_path, task_id)
    assert exit_code == 0

    _assert_zero_workspace_mutations(tmp_path, before)


# -------------------------------------------------------------------------
# 3. Malformed JSON and Unsupported Schema Versions
# -------------------------------------------------------------------------
def test_case_3_malformed_json_and_unsupported_schema(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    proof_dir = tmp_path / ".brainfrog" / "proofs"
    proof_dir.mkdir(parents=True, exist_ok=True)

    # 3a. Malformed JSON syntax
    bad_json_path = proof_dir / "task_malformed.json"
    bad_json_path.write_text("{ unquoted_key: 123, broken... }", encoding="utf-8")

    # 3b. Non-dict root
    array_json_path = proof_dir / "task_array.json"
    array_json_path.write_text("[1, 2, 3]", encoding="utf-8")

    # 3c. Unsupported schema version
    task_id = "task_bad_version"
    data = _make_base_proof(task_id=task_id)
    data["version"] = "99.0.0"
    version_path = _write_proof(tmp_path, task_id, data)

    before = _snapshot_workspace(tmp_path)

    report_bad = verify_proof(tmp_path, bad_json_path)
    assert report_bad.overall_status == ProofVerificationStatus.INVALID
    assert any("Malformed JSON syntax" in r for r in report_bad.reasons)
    assert verify_proof_cli(tmp_path, str(bad_json_path)) == 1

    report_array = verify_proof(tmp_path, array_json_path)
    assert report_array.overall_status == ProofVerificationStatus.INVALID
    assert any("root JSON must be an object/dict" in r for r in report_array.reasons)
    assert verify_proof_cli(tmp_path, str(array_json_path)) == 1

    report_version = verify_proof(tmp_path, version_path)
    assert report_version.overall_status == ProofVerificationStatus.INVALID
    assert any("Unsupported schema version" in r for r in report_version.reasons)
    assert verify_proof_cli(tmp_path, task_id) == 1

    _assert_zero_workspace_mutations(tmp_path, before)


# -------------------------------------------------------------------------
# 4. Missing Test Evidence with a Self-Declared VERIFIED Verdict
# -------------------------------------------------------------------------
def test_case_4_missing_test_evidence_with_self_declared_verified(tmp_path: Path) -> None:
    commit_sha = _init_git_repo(tmp_path)

    # 4a. Missing test command with VERIFIED
    t1 = "task_004_no_cmd"
    d1 = _make_base_proof(task_id=t1, verdict="VERIFIED", commit_sha=commit_sha)
    d1["verification"]["test_command"] = ""
    _write_proof(tmp_path, t1, d1)

    # 4b. Missing process evidence with VERIFIED
    t2 = "task_004_no_proc"
    d2 = _make_base_proof(task_id=t2, verdict="VERIFIED", commit_sha=commit_sha)
    d2["verification"]["process_evidence"] = None
    _write_proof(tmp_path, t2, d2)

    before = _snapshot_workspace(tmp_path)

    r1 = verify_proof(tmp_path, t1)
    assert r1.overall_status == ProofVerificationStatus.INVALID
    assert any("missing test command" in r.lower() for r in r1.reasons)
    assert verify_proof_cli(tmp_path, t1) == 1

    r2 = verify_proof(tmp_path, t2)
    assert r2.overall_status == ProofVerificationStatus.INVALID
    assert any("missing execution process evidence" in r.lower() for r in r2.reasons)
    assert verify_proof_cli(tmp_path, t2) == 1

    _assert_zero_workspace_mutations(tmp_path, before)


# -------------------------------------------------------------------------
# 5. Nonzero Test Exit Paired with Passing Gate
# -------------------------------------------------------------------------
def test_case_5_nonzero_exit_paired_with_passing_gate(tmp_path: Path) -> None:
    commit_sha = _init_git_repo(tmp_path)
    task_id = "task_005_inconsistent_exit"
    data = _make_base_proof(
        task_id=task_id,
        verdict="FAILED",
        changes_status="ROLLED_BACK",
        exit_code=2,
        commit_sha=commit_sha,
    )
    # Tampered invariant: exit code is 2, but test_suite gate is claiming True!
    data["verification"]["gate_checks"]["test_suite"] = True
    _write_proof(tmp_path, task_id, data)

    before = _snapshot_workspace(tmp_path)

    report = verify_proof(tmp_path, task_id)
    assert report.overall_status == ProofVerificationStatus.INVALID
    assert any("Non-zero test exit (2) paired with passing test_suite" in r for r in report.reasons)
    assert verify_proof_cli(tmp_path, task_id) == 1

    _assert_zero_workspace_mutations(tmp_path, before)


# -------------------------------------------------------------------------
# 6. Task ID Mismatch
# -------------------------------------------------------------------------
def test_case_6_task_id_mismatch(tmp_path: Path) -> None:
    commit_sha = _init_git_repo(tmp_path)

    # 6a. Filename vs internal task_id mismatch
    data1 = _make_base_proof(task_id="task_internal_id", commit_sha=commit_sha)
    p1 = tmp_path / ".brainfrog" / "proofs" / "task_external_stem.json"
    p1.parent.mkdir(parents=True, exist_ok=True)
    p1.write_text(json.dumps(data1), encoding="utf-8")

    # 6b. Process evidence task_id mismatch
    t2 = "task_006_proc_mismatch"
    data2 = _make_base_proof(task_id=t2, commit_sha=commit_sha)
    data2["verification"]["process_evidence"]["task_id"] = "completely_different_id"
    _write_proof(tmp_path, t2, data2)

    before = _snapshot_workspace(tmp_path)

    r1 = verify_proof(tmp_path, p1)
    assert r1.overall_status == ProofVerificationStatus.INVALID
    assert any("differs from internal task_id" in r for r in r1.reasons)
    assert verify_proof_cli(tmp_path, str(p1)) == 1

    r2 = verify_proof(tmp_path, t2)
    assert r2.overall_status == ProofVerificationStatus.INVALID
    assert any("contradicts task_id" in r for r in r2.reasons)
    assert verify_proof_cli(tmp_path, t2) == 1

    _assert_zero_workspace_mutations(tmp_path, before)


# -------------------------------------------------------------------------
# 7. Commit SHA Mismatch Against Authoritative Intent
# -------------------------------------------------------------------------
def test_case_7_commit_sha_mismatch_against_intent(tmp_path: Path) -> None:
    commit_sha = _init_git_repo(tmp_path)
    task_id = "task_007_sha_mismatch"

    intent_sha = "1" * 40
    _write_intent(tmp_path, task_id, CommitIntentStatus.COMMITTED_PENDING_PROOF, commit_sha=intent_sha)

    # Proof claims a different SHA
    proof_sha = "2" * 40
    data = _make_base_proof(task_id=task_id, commit_sha=proof_sha)
    _write_proof(tmp_path, task_id, data)

    before = _snapshot_workspace(tmp_path)

    report = verify_proof(tmp_path, task_id)
    assert report.overall_status == ProofVerificationStatus.INVALID
    assert any("Commit SHA mismatch" in r for r in report.reasons)
    assert verify_proof_cli(tmp_path, task_id) == 1

    _assert_zero_workspace_mutations(tmp_path, before)


# -------------------------------------------------------------------------
# 8. Unresolved Commit Intent
# -------------------------------------------------------------------------
def test_case_8_unresolved_commit_intent(tmp_path: Path) -> None:
    commit_sha = _init_git_repo(tmp_path)
    task_id = "task_008_unresolved"

    _write_intent(tmp_path, task_id, CommitIntentStatus.RECOVERY_REQUIRED, commit_sha=commit_sha)
    data = _make_base_proof(task_id=task_id, verdict="VERIFIED", commit_sha=commit_sha)
    _write_proof(tmp_path, task_id, data)

    # What if non-verified task has an unfinalized intent? It should be INCOMPLETE (recovery required)
    t_inc = "task_008_inc"
    _write_intent(tmp_path, t_inc, CommitIntentStatus.COMMITTING, commit_sha=commit_sha)
    d_inc = _make_base_proof(task_id=t_inc, verdict="FAILED", changes_status="ROLLED_BACK", commit_sha=commit_sha)
    _write_proof(tmp_path, t_inc, d_inc)

    before = _snapshot_workspace(tmp_path)

    report = verify_proof(tmp_path, task_id)
    assert report.overall_status == ProofVerificationStatus.INVALID
    assert any("contradicts VERIFIED status" in r for r in report.reasons)
    assert verify_proof_cli(tmp_path, task_id) == 1

    r_inc = verify_proof(tmp_path, t_inc)
    assert r_inc.overall_status == ProofVerificationStatus.INCOMPLETE
    assert any("Workspace requires recovery" in r for r in r_inc.reasons)
    assert verify_proof_cli(tmp_path, t_inc) == 2

    _assert_zero_workspace_mutations(tmp_path, before)


# -------------------------------------------------------------------------
# 9. Committed Transaction with Failed Proof Persistence
# -------------------------------------------------------------------------
def test_case_9_committed_transaction_with_failed_proof_persistence(tmp_path: Path) -> None:
    commit_sha = _init_git_repo(tmp_path)
    task_id = "task_009_missing_proof"

    # Transaction committed and intent recorded, but proof JSON was never persisted
    _write_intent(tmp_path, task_id, CommitIntentStatus.COMMITTED_PENDING_PROOF, commit_sha=commit_sha)

    before = _snapshot_workspace(tmp_path)

    report = verify_proof(tmp_path, task_id)
    assert report.overall_status == ProofVerificationStatus.INCOMPLETE
    assert report.is_incomplete is True
    assert any("Proof artifact JSON file not found" in r for r in report.reasons)
    assert any("COMMITTED_PENDING_PROOF" in r for r in report.reasons)

    # CLI exit code for INCOMPLETE is 2
    exit_code = verify_proof_cli(tmp_path, task_id)
    assert exit_code == 2

    _assert_zero_workspace_mutations(tmp_path, before)


# -------------------------------------------------------------------------
# 10. Contradictory Task Status and Transaction Status
# -------------------------------------------------------------------------
def test_case_10_contradictory_task_and_transaction_status(tmp_path: Path) -> None:
    commit_sha = _init_git_repo(tmp_path)

    # 10a. VERIFIED with ROLLED_BACK
    t1 = "task_010_verified_rolled_back"
    d1 = _make_base_proof(task_id=t1, verdict="VERIFIED", changes_status="ROLLED_BACK", commit_sha=commit_sha)
    _write_proof(tmp_path, t1, d1)

    # 10b. FAILED with COMMITTED
    t2 = "task_010_failed_committed"
    d2 = _make_base_proof(task_id=t2, verdict="FAILED", changes_status="COMMITTED", commit_sha=commit_sha)
    _write_proof(tmp_path, t2, d2)

    before = _snapshot_workspace(tmp_path)

    r1 = verify_proof(tmp_path, t1)
    assert r1.overall_status == ProofVerificationStatus.INVALID
    assert any("contradicts changes status" in r.lower() or "contradicts verified" in r.lower() for r in r1.reasons)
    assert verify_proof_cli(tmp_path, t1) == 1

    r2 = verify_proof(tmp_path, t2)
    assert r2.overall_status == ProofVerificationStatus.INVALID
    assert any("contradict" in r.lower() for r in r2.reasons)
    assert verify_proof_cli(tmp_path, t2) == 1

    _assert_zero_workspace_mutations(tmp_path, before)


# -------------------------------------------------------------------------
# 11. Missing or Corrupt Derived Markdown Companion
# -------------------------------------------------------------------------
def test_case_11_missing_or_corrupt_derived_markdown(tmp_path: Path) -> None:
    commit_sha = _init_git_repo(tmp_path)
    task_id = "task_011_md_corruption"
    data = _make_base_proof(task_id=task_id, commit_sha=commit_sha)
    _write_proof(tmp_path, task_id, data)

    # 11a. Derived markdown is missing -> Still VALID!
    md_path = tmp_path / ".brainfrog" / "proofs" / f"{task_id}.md"
    if md_path.exists():
        md_path.unlink()

    before = _snapshot_workspace(tmp_path)
    r1 = verify_proof(tmp_path, task_id)
    assert r1.overall_status == ProofVerificationStatus.VALID

    # 11b. Derived markdown is corrupt / binary garbage -> Still VALID!
    md_path.write_bytes(b"\x00\xff\xfe\x00\x12\x34\x56\x78")
    before_corrupt = _snapshot_workspace(tmp_path)
    r2 = verify_proof(tmp_path, task_id)
    assert r2.overall_status == ProofVerificationStatus.VALID
    _assert_zero_workspace_mutations(tmp_path, before_corrupt)

    # 11c. Derived markdown is empty -> Still VALID!
    md_path.write_text("", encoding="utf-8")
    before_empty = _snapshot_workspace(tmp_path)
    r3 = verify_proof(tmp_path, task_id)
    assert r3.overall_status == ProofVerificationStatus.VALID
    assert verify_proof_cli(tmp_path, task_id) == 0
    _assert_zero_workspace_mutations(tmp_path, before_empty)


# -------------------------------------------------------------------------
# 12. Tampered Proof Fields and Unknown Status Values
# -------------------------------------------------------------------------
def test_case_12_tampered_proof_fields_and_unknown_status(tmp_path: Path) -> None:
    commit_sha = _init_git_repo(tmp_path)

    # 12a. Unknown verdict status
    t1 = "task_012_bad_verdict"
    d1 = _make_base_proof(task_id=t1, commit_sha=commit_sha)
    d1["verdict"]["status"] = "CONFIRMED_BY_LLM"
    _write_proof(tmp_path, t1, d1)

    # 12b. Unknown changes status
    t2 = "task_012_bad_changes_status"
    d2 = _make_base_proof(task_id=t2, commit_sha=commit_sha)
    d2["changes"]["status"] = "UNSURE"
    _write_proof(tmp_path, t2, d2)

    # 12c. Illegal / traversal path in change records
    t3 = "task_012_path_traversal"
    d3 = _make_base_proof(task_id=t3, commit_sha=commit_sha)
    d3["changes"]["files"].append({"path": "../../sensitive.key", "operation": "CREATE"})
    _write_proof(tmp_path, t3, d3)

    # 12d. Tampered non-int exit_code
    t4 = "task_012_bad_exit_type"
    d4 = _make_base_proof(task_id=t4, commit_sha=commit_sha)
    d4["verification"]["exit_code"] = "zero"
    _write_proof(tmp_path, t4, d4)

    # 12e. Tampered non-bool gate check value
    t5 = "task_012_bad_gate_type"
    d5 = _make_base_proof(task_id=t5, commit_sha=commit_sha)
    d5["verification"]["gate_checks"]["test_suite"] = "passed"
    _write_proof(tmp_path, t5, d5)

    before = _snapshot_workspace(tmp_path)

    r1 = verify_proof(tmp_path, t1)
    assert r1.overall_status == ProofVerificationStatus.INVALID
    assert any("Unknown verdict status" in r for r in r1.reasons)

    r2 = verify_proof(tmp_path, t2)
    assert r2.overall_status == ProofVerificationStatus.INVALID
    assert any("Unknown changes status" in r for r in r2.reasons)

    r3 = verify_proof(tmp_path, t3)
    assert r3.overall_status == ProofVerificationStatus.INVALID
    assert any("Illegal path entry in change records" in r for r in r3.reasons)

    r4 = verify_proof(tmp_path, t4)
    assert r4.overall_status == ProofVerificationStatus.INVALID
    assert any("Malformed exit_code type" in r for r in r4.reasons)

    r5 = verify_proof(tmp_path, t5)
    assert r5.overall_status == ProofVerificationStatus.INVALID
    assert any("Invalid gate check values" in r for r in r5.reasons)

    _assert_zero_workspace_mutations(tmp_path, before)


# -------------------------------------------------------------------------
# 13. Historical Claims Cannot Be Independently Re-Established
# -------------------------------------------------------------------------
def test_case_13_historical_claims_cannot_be_reestablished(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    task_id = "task_013_fake_git_sha"

    # SHA is valid 40 hex chars, but does not exist in local git object database
    fake_sha = "f" * 40
    data = _make_base_proof(task_id=task_id, commit_sha=fake_sha)
    _write_proof(tmp_path, task_id, data)

    before = _snapshot_workspace(tmp_path)

    report = verify_proof(tmp_path, task_id)
    # Must NOT promote to VALID! Must be INCOMPLETE!
    assert report.overall_status == ProofVerificationStatus.INCOMPLETE
    assert report.is_incomplete is True
    assert any("not found in local repository. Historical claim unverified." in r for r in report.reasons)

    exit_code = verify_proof_cli(tmp_path, task_id)
    assert exit_code == 2

    _assert_zero_workspace_mutations(tmp_path, before)


# -------------------------------------------------------------------------
# SDK and CLI Routing Integration Tests
# -------------------------------------------------------------------------
def test_sdk_client_verify_proof_method(tmp_path: Path) -> None:
    commit_sha = _init_git_repo(tmp_path)
    task_id = "task_sdk_verify_test"
    data = _make_base_proof(task_id=task_id, commit_sha=commit_sha)
    proof_path = _write_proof(tmp_path, task_id, data)

    client = BrainFrogClient(tmp_path)
    # Test verify_proof by task_id
    rep1 = client.verify_proof(task_id)
    assert rep1.overall_status == ProofVerificationStatus.VALID

    # Test verify_proof by explicit file path
    rep2 = client.verify_proof(proof_path)
    assert rep2.overall_status == ProofVerificationStatus.VALID


def test_cli_routing_flags_and_subcommands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    commit_sha = _init_git_repo(tmp_path)
    task_id = "task_cli_route_test"
    data = _make_base_proof(task_id=task_id, commit_sha=commit_sha)
    _write_proof(tmp_path, task_id, data)

    # 1. Flag: --verify-proof <task_id>
    monkeypatch.setattr(sys, "argv", ["brainfrog", "--repo", str(tmp_path), "--verify-proof", task_id])
    assert cli_main() == 0

    # 2. Positional: verify-proof <task_id>
    monkeypatch.setattr(sys, "argv", ["brainfrog", "--repo", str(tmp_path), "verify-proof", task_id])
    assert cli_main() == 0

    # 3. Positional: verify <task_id>
    monkeypatch.setattr(sys, "argv", ["brainfrog", "--repo", str(tmp_path), "verify", task_id])
    assert cli_main() == 0
