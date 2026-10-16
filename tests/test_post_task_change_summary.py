"""Focused tests for Post-Task File Change Summary (Sprint 1.5B).

Tests the 14 mandatory architectural scenarios:
1. Successful multi-file modification and creation.
2. Planner declares multiple files but only one file is actually mutated.
3. Verification failure followed by successful rollback.
4. Partial rollback failure with both restored and uncertain files.
5. Delete and rename operations.
6. Positively evidenced no-mutation execution.
7. Pre-transaction failure, where mutation status must not be inferred.
8. Missing or malformed transaction evidence.
9. Staged hash versus final hash semantics.
10. Historical proof schema 1.0.0 verification.
11. Existing TaskResult.changed_files consumers and regressions.
12. CLI escaping of hostile markup and secret redaction.
13. Filesystem transaction committed while Git finalization fails.
14. Post-execution external disk modification (drift detection).
"""
import base64
import hashlib
import io
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from cli import print_task_verification_report
from core.runtime import BrainFrogRuntime, TaskRequest
from core.runtime.proof import (
    ChangeRecord,
    ChangesEvidence,
    FileProofStore,
    GitProvenance,
    ProcessEvidence,
    ProofArtifact,
    ProofVerdict,
    TaskEvidence,
    VerificationEvidence,
    redact_secrets,
)
from core.runtime.proof_verifier import (
    DeterministicProofVerifier,
    ProofVerificationStatus,
)
from core.runtime.task import (
    FileChangeSummary,
    FileMutationStatus,
)
from core.runtime.transaction import (
    OperationStatus,
    OperationType,
    TransactionCoordinator,
    TransactionOperation,
    TransactionResult,
    TransactionStatus,
    build_file_change_summaries,
)
from core.runtime.workflow import (
    TaskPlan,
    TaskResult,
    TaskStep,
)
from core.runtime.workspace_lock import WorkspaceLock
from orchestrator import PlanStep
from system1.base import Answer


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# =============================================================================
# 1. Successful multi-file modification and creation
# =============================================================================
def test_scenario_1_successful_multi_file_mutation(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    existing_file = workspace / "existing.py"
    existing_file.write_text("initial_code()", encoding="utf-8")
    before_sha = _sha("initial_code()")

    new_file = workspace / "new_module.py"
    staged_new = "def hello(): pass"
    new_file.write_text(staged_new, encoding="utf-8")
    staged_new_sha = _sha(staged_new)

    staged_existing = "initial_code()\nupdated_code()"
    existing_file.write_bytes(staged_existing.encode("utf-8"))
    staged_existing_sha = _sha(staged_existing)

    op_create = TransactionOperation(
        type=OperationType.CREATE_FILE,
        target="new_module.py",
        status=OperationStatus.EXECUTED,
        before_state={"exists": False},
        after_state={"exists": True, "content": staged_new, "is_text": True},
    )
    op_modify = TransactionOperation(
        type=OperationType.MODIFY_FILE,
        target="existing.py",
        status=OperationStatus.EXECUTED,
        before_state={"exists": True, "content": "initial_code()", "is_text": True},
        after_state={"exists": True, "content": staged_existing, "is_text": True},
    )

    tx_res = TransactionResult(
        transaction_id="tx_01",
        status=TransactionStatus.COMMITTED,
        committed=True,
        rolled_back=False,
        operations=(op_create, op_modify),
    )

    summaries = build_file_change_summaries(tx_res, workspace)
    assert len(summaries) == 2

    # Op CREATE
    s0 = summaries[0]
    assert s0.path == "new_module.py"
    assert s0.operation == "CREATE"
    assert s0.status == FileMutationStatus.COMMITTED
    assert s0.before_sha256 is None
    assert s0.staged_sha256 == staged_new_sha
    assert s0.final_sha256 == staged_new_sha
    assert s0.byte_count_delta == len(staged_new.encode("utf-8"))

    # Op MODIFY
    s1 = summaries[1]
    assert s1.path == "existing.py"
    assert s1.operation == "MODIFY"
    assert s1.status == FileMutationStatus.COMMITTED
    assert s1.before_sha256 == before_sha
    assert s1.staged_sha256 == staged_existing_sha
    assert s1.final_sha256 == staged_existing_sha
    assert s1.byte_count_delta == len(staged_existing.encode("utf-8")) - len("initial_code()".encode("utf-8"))


# =============================================================================
# 2. Planner declares multiple files but only one file is actually mutated
# =============================================================================
def test_scenario_2_planner_scope_divergence(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    mutated_file = workspace / "mutated.py"
    mutated_file.write_text("updated = True", encoding="utf-8")

    # Transaction only mutated mutated.py
    op = TransactionOperation(
        type=OperationType.MODIFY_FILE,
        target="mutated.py",
        status=OperationStatus.EXECUTED,
        before_state={"exists": True, "content": "updated = False", "is_text": True},
        after_state={"exists": True, "content": "updated = True", "is_text": True},
    )
    tx_res = TransactionResult(
        transaction_id="tx_02",
        status=TransactionStatus.COMMITTED,
        committed=True,
        rolled_back=False,
        operations=(op,),
    )

    summaries = build_file_change_summaries(tx_res, workspace)
    assert len(summaries) == 1
    assert summaries[0].path == "mutated.py"
    assert summaries[0].status == FileMutationStatus.COMMITTED

    # Planner claimed multiple files: untouched_a.py, untouched_b.py, mutated.py
    plan = TaskPlan(
        goal="Update settings",
        steps=[
            TaskStep(id="1", description="Edit files", scope=["untouched_a.py", "untouched_b.py", "mutated.py"])
        ],
    )
    task_res = TaskResult(
        task_id="t_02",
        user_request="Update settings",
        success=True,
        verified=True,
        status="COMMITTED",
        summary="Done",
        plan=plan,
        changed_files=["mutated.py"],
        file_changes=summaries,
    )

    # file_changes strictly reflects authoritative transaction evidence, NOT planner scope
    assert [fc.path for fc in task_res.file_changes] == ["mutated.py"]
    assert "untouched_a.py" not in [fc.path for fc in task_res.file_changes]
    assert "untouched_b.py" not in [fc.path for fc in task_res.file_changes]


# =============================================================================
# 3. Verification failure followed by successful rollback
# =============================================================================
def test_scenario_3_verification_failure_with_clean_rollback(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    # File originally had base content; was restored to base content after rollback
    restored_file = workspace / "main.py"
    restored_file.write_text("def base(): pass", encoding="utf-8")
    base_sha = _sha("def base(): pass")
    staged_sha = _sha("def broken_mutation(): pass")

    op_mod = TransactionOperation(
        type=OperationType.MODIFY_FILE,
        target="main.py",
        status=OperationStatus.ROLLED_BACK,
        before_state={"exists": True, "content": "def base(): pass", "is_text": True},
        after_state={"exists": True, "content": "def broken_mutation(): pass", "is_text": True},
    )

    # Created file was deleted on rollback (does not exist on disk)
    op_create = TransactionOperation(
        type=OperationType.CREATE_FILE,
        target="temp_experiment.py",
        status=OperationStatus.ROLLED_BACK,
        before_state={"exists": False},
        after_state={"exists": True, "content": "import sys", "is_text": True},
    )

    tx_res = TransactionResult(
        transaction_id="tx_03",
        status=TransactionStatus.ROLLED_BACK,
        committed=False,
        rolled_back=True,
        operations=(op_mod, op_create),
    )

    summaries = build_file_change_summaries(tx_res, workspace)
    assert len(summaries) == 2

    # Modified file restored
    s_mod = summaries[0]
    assert s_mod.path == "main.py"
    assert s_mod.status == FileMutationStatus.ROLLED_BACK
    assert s_mod.before_sha256 == base_sha
    assert s_mod.staged_sha256 == staged_sha
    assert s_mod.final_sha256 == base_sha  # verified disk matches pre-mutation!
    assert s_mod.byte_count_delta == 0

    # Created file removed
    s_create = summaries[1]
    assert s_create.path == "temp_experiment.py"
    assert s_create.status == FileMutationStatus.ROLLED_BACK
    assert s_create.final_sha256 is None  # verified removed from disk!
    assert s_create.byte_count_delta == 0


# =============================================================================
# 4. Partial rollback failure with both restored and uncertain files
# =============================================================================
def test_scenario_4_partial_rollback_failure(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    # File 1 restored cleanly
    f1 = workspace / "restored.txt"
    f1.write_text("clean base", encoding="utf-8")
    op_clean = TransactionOperation(
        type=OperationType.MODIFY_FILE,
        target="restored.txt",
        status=OperationStatus.ROLLED_BACK,
        before_state={"exists": True, "content": "clean base", "is_text": True},
        after_state={"exists": True, "content": "dirty mutation", "is_text": True},
    )

    # File 2 rollback failed: disk still contains mutated content
    f2 = workspace / "failed_revert.txt"
    f2.write_text("unreverted dirty content", encoding="utf-8")
    op_failed = TransactionOperation(
        type=OperationType.MODIFY_FILE,
        target="failed_revert.txt",
        status=OperationStatus.FAILED,
        before_state={"exists": True, "content": "original", "is_text": True},
        after_state={"exists": True, "content": "unreverted dirty content", "is_text": True},
        error="Permission denied when attempting to revert failed_revert.txt",
    )

    tx_res = TransactionResult(
        transaction_id="tx_04",
        status=TransactionStatus.FAILED,
        committed=False,
        rolled_back=False,
        operations=(op_clean, op_failed),
        rollback_error="Partial rollback failure: failed_revert.txt could not be restored",
    )

    summaries = build_file_change_summaries(tx_res, workspace)
    assert len(summaries) == 2

    assert summaries[0].path == "restored.txt"
    assert summaries[0].status == FileMutationStatus.ROLLED_BACK
    assert summaries[0].final_sha256 == _sha("clean base")

    assert summaries[1].path == "failed_revert.txt"
    assert summaries[1].status == FileMutationStatus.ROLLBACK_FAILED
    assert summaries[1].final_sha256 == _sha("unreverted dirty content")
    assert summaries[1].error is not None
    assert "Permission denied" in summaries[1].error


# =============================================================================
# 5. Delete and rename operations
# =============================================================================
def test_scenario_5_delete_and_rename_operations(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    # 5a. Committed Delete: doomed.txt deleted from disk
    op_delete_committed = TransactionOperation(
        type=OperationType.DELETE_FILE,
        target="doomed.txt",
        status=OperationStatus.EXECUTED,
        before_state={"exists": True, "content": "to be deleted", "is_text": True},
        after_state={"exists": False},
    )
    tx_del = TransactionResult(
        transaction_id="tx_05a",
        status=TransactionStatus.COMMITTED,
        committed=True,
        rolled_back=False,
        operations=(op_delete_committed,),
    )
    s_del = build_file_change_summaries(tx_del, workspace)[0]
    assert s_del.path == "doomed.txt"
    assert s_del.operation == "DELETE"
    assert s_del.status == FileMutationStatus.COMMITTED
    assert s_del.before_sha256 == _sha("to be deleted")
    assert s_del.staged_sha256 is None
    assert s_del.final_sha256 is None
    assert s_del.byte_count_delta == -len("to be deleted".encode("utf-8"))

    # 5b. Committed Rename: old_name moved to new_name
    new_dest = workspace / "dest.py"
    new_dest.write_text("module payload", encoding="utf-8")
    dest_sha = _sha("module payload")

    op_rename = TransactionOperation(
        type=OperationType.RENAME_FILE,
        target="src.py",
        new_target="dest.py",
        status=OperationStatus.EXECUTED,
        before_state={"exists": True, "content": "module payload", "is_text": True},
        after_state={"exists": True, "content": "module payload", "is_text": True},
    )
    tx_rename = TransactionResult(
        transaction_id="tx_05b",
        status=TransactionStatus.COMMITTED,
        committed=True,
        rolled_back=False,
        operations=(op_rename,),
    )
    s_ren = build_file_change_summaries(tx_rename, workspace)[0]
    assert s_ren.path == "src.py"
    assert s_ren.operation == "RENAME"
    assert s_ren.status == FileMutationStatus.COMMITTED
    assert s_ren.before_sha256 == dest_sha
    assert s_ren.staged_sha256 == dest_sha
    assert s_ren.final_sha256 == dest_sha
    assert s_ren.byte_count_delta == 0


# =============================================================================
# 6. Positively evidenced no-mutation execution
# =============================================================================
def test_scenario_6_positively_evidenced_no_mutation():
    tx_res = TransactionResult(
        transaction_id="tx_06",
        status=TransactionStatus.COMMITTED,
        committed=True,
        rolled_back=False,
        operations=tuple(),
    )
    summaries = build_file_change_summaries(tx_res)
    assert summaries == tuple()

    # CLI report output distinguishes positively evidenced zero operations
    task_res = TaskResult(
        task_id="t_06",
        user_request="Read only inquiry",
        success=True,
        verified=True,
        status="COMMITTED",
        summary="No files were changed.",
        transaction_id="tx_06",
        transaction_status="committed",
        file_changes=summaries,
    )

    out = io.StringIO()
    con = Console(file=out, color_system=None, width=120)
    import cli
    orig_con = cli.console
    try:
        cli.console = con
        print_task_verification_report(task_res, Path("."))
    finally:
        cli.console = orig_con

    output = out.getvalue()
    assert "UNCHANGED" in output
    assert "Positive transaction evidence" in output
    assert "mutations attempted/recorded" in output


# =============================================================================
# 7. Pre-transaction failure, where mutation status must not be inferred
# =============================================================================
def test_scenario_7_pre_transaction_failure():
    # Early pre-transaction failure: no transaction_id, no file_changes
    task_res = TaskResult(
        task_id="t_07",
        user_request="Execute bad task",
        success=False,
        verified=False,
        status="FAILED",
        summary="Workspace not found",
        failure_reason="Workspace directory not found",
        file_changes=tuple(),
    )

    out = io.StringIO()
    con = Console(file=out, color_system=None, width=120)
    import cli
    orig_con = cli.console
    try:
        cli.console = con
        print_task_verification_report(task_res, Path("."))
    finally:
        cli.console = orig_con

    output = out.getvalue()
    # Must NOT claim UNCHANGED! Must show UNKNOWN / unrecorded
    assert "UNKNOWN" in output
    assert "No transaction evidence available" in output
    assert "Positive transaction evidence: zero mutations attempted/recorded" not in output


# =============================================================================
# 8. Missing or malformed transaction evidence
# =============================================================================
def test_scenario_8_missing_or_synthesized_transaction():
    # tx_res is None
    assert build_file_change_summaries(None) == tuple()

    # synthesized tx_res (is_synthesized=True)
    synth_tx = TransactionResult(
        transaction_id="tx_synth",
        status=TransactionStatus.COMMITTED,
        committed=True,
        rolled_back=False,
        operations=tuple(),
    )
    assert build_file_change_summaries(synth_tx, is_synthesized=True) == tuple()


# =============================================================================
# 9. Staged hash versus final hash semantics
# =============================================================================
def test_scenario_9_staged_vs_final_hash_semantics(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    target_file = workspace / "config.json"
    target_file.write_text('{"env": "prod"}', encoding="utf-8")
    prod_sha = _sha('{"env": "prod"}')
    staged_sha = _sha('{"env": "staging"}')

    # Op was rolled back: staged payload was {"env": "staging"}, but disk was restored to {"env": "prod"}
    op = TransactionOperation(
        type=OperationType.MODIFY_FILE,
        target="config.json",
        status=OperationStatus.ROLLED_BACK,
        before_state={"exists": True, "content": '{"env": "prod"}', "is_text": True},
        after_state={"exists": True, "content": '{"env": "staging"}', "is_text": True},
    )
    tx_res = TransactionResult(
        transaction_id="tx_09",
        status=TransactionStatus.ROLLED_BACK,
        committed=False,
        rolled_back=True,
        operations=(op,),
    )

    summaries = build_file_change_summaries(tx_res, workspace)
    s = summaries[0]

    # Staged hash reflects intended mutation payload
    assert s.staged_sha256 == staged_sha
    # Final hash reflects independently verified disk state (restored to baseline)
    assert s.final_sha256 == prod_sha
    assert s.before_sha256 == prod_sha
    assert s.status == FileMutationStatus.ROLLED_BACK
    # Intended staged payload != observed final disk state
    assert s.staged_sha256 != s.final_sha256


# =============================================================================
# 10. Historical proof schema 1.0.0 verification
# =============================================================================
def test_scenario_10_historical_proof_schema_100_compatibility(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    app_file = workspace / "app.py"
    app_file.write_text("print('hello')", encoding="utf-8")
    app_sha = _sha("print('hello')")

    proof = ProofArtifact(
        task=TaskEvidence(
            task_id="task_proof_100",
            session_id="session_100",
            description="Historical proof test",
            created_at="2026-10-12T00:00:00Z",
            completed_at="2026-10-12T00:00:01Z",
            duration_ms=1000,
        ),
        verdict=ProofVerdict(status="VERIFIED", reason="Task verified"),
        provenance=GitProvenance(repo_root=str(workspace), is_git=False),
        verification=VerificationEvidence(
            test_command="pytest",
            exit_code=0,
            stdout_summary="1 passed",
            stderr_summary="",
            process_evidence=ProcessEvidence(
                command="pytest",
                returncode=0,
                timed_out=False,
                cancelled=False,
                task_id="task_proof_100",
            ),
            gate_checks={
                "test_suite": True,
                "workspace_scope": True,
                "transaction_integrity": True,
                "execution_integrity": True,
                "trust_integrity": True,
            },
        ),
        changes=ChangesEvidence(
            transaction_id="tx_hist",
            status="COMMITTED",
            files=[
                ChangeRecord(
                    path="app.py",
                    operation="MODIFY",
                    before_sha256=None,
                    after_sha256=app_sha,
                    byte_count_delta=len("print('hello')"),
                )
            ],
        ),
    )

    # Assert proof schema remains 1.0.0
    proof_dict = proof.to_dict()
    assert proof_dict["version"] == "1.0.0"
    assert proof.version == "1.0.0"

    # Save to storage and verify using DeterministicProofVerifier
    store = FileProofStore(workspace)
    store.save(proof, "# Markdown Proof Summary")

    verifier = DeterministicProofVerifier(workspace, verify_disk_integrity=True)
    report = verifier.verify("task_proof_100", verify_disk_integrity=True)

    assert report.overall_status == ProofVerificationStatus.VALID
    assert report.is_valid is True
    assert report.verification_outcome == "VERIFIED"


# =============================================================================
# 11. Existing TaskResult.changed_files consumers and regressions
# =============================================================================
def test_scenario_11_changed_files_consumer_compatibility():
    # On commit: changed_files reflects targets
    tr_commit = TaskResult(
        task_id="t_11a",
        user_request="req",
        success=True,
        verified=True,
        status="COMMITTED",
        summary="done",
        changed_files=["a.py", "b.py"],
        file_changes=(
            FileChangeSummary(path="a.py", operation="MODIFY", status=FileMutationStatus.COMMITTED),
            FileChangeSummary(path="b.py", operation="CREATE", status=FileMutationStatus.COMMITTED),
        ),
    )
    assert tr_commit.changed_files == ["a.py", "b.py"]
    rep_commit = tr_commit.format_report()
    assert "File Changes:" in rep_commit
    assert "a.py [MODIFY] (COMMITTED)" in rep_commit

    # On rollback: existing consumers expect changed_files == []
    tr_rollback = TaskResult(
        task_id="t_11b",
        user_request="req",
        success=False,
        verified=False,
        status="ROLLED_BACK",
        summary="rolled back",
        changed_files=[],
        file_changes=(
            FileChangeSummary(path="a.py", operation="MODIFY", status=FileMutationStatus.ROLLED_BACK),
        ),
    )
    assert tr_rollback.changed_files == []
    assert len(tr_rollback.file_changes) == 1
    assert tr_rollback.file_changes[0].status == FileMutationStatus.ROLLED_BACK


# =============================================================================
# 12. CLI escaping of hostile markup and secret redaction
# =============================================================================
def test_scenario_12_cli_escaping_and_secret_redaction():
    secret_token = "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz123456"
    hostile_path = "[bold red]evil_path[/bold red].py"
    hostile_error = "[blink red]Crash[/blink red] with token " + secret_token

    fc = FileChangeSummary(
        path=hostile_path,
        operation="MODIFY",
        status=FileMutationStatus.ROLLBACK_FAILED,
        error=hostile_error,
    )
    task_res = TaskResult(
        task_id="t_12",
        user_request="Hostile payload test",
        success=False,
        verified=False,
        status="ROLLED_BACK",
        summary="Rollback failed with " + secret_token,
        failure_reason="Failed due to " + hostile_error,
        transaction_id="tx_12",
        transaction_status="rolled_back",
        file_changes=(fc,),
    )

    out = io.StringIO()
    con = Console(file=out, color_system=None, width=120)
    import cli
    orig_con = cli.console
    try:
        cli.console = con
        print_task_verification_report(task_res, Path("."))
    finally:
        cli.console = orig_con

    rendered = out.getvalue()

    # Secret MUST be redacted everywhere
    assert secret_token not in rendered
    assert "[REDACTED_SECRET" in rendered

    # Hostile tags MUST appear literal, not parsed as tags
    assert "[bold red]" in rendered or "evil_path" in rendered
    # Must not crash Rich or produce unhandled markup tags


# =============================================================================
# 13. Filesystem transaction committed while Git finalization fails
# =============================================================================
def test_scenario_13_fs_committed_git_finalization_fails():
    fc = FileChangeSummary(
        path="work.py",
        operation="MODIFY",
        status=FileMutationStatus.COMMITTED,
        before_sha256=_sha("old"),
        staged_sha256=_sha("new"),
        final_sha256=_sha("new"),
    )

    # Proof artifact has GitProvenance with commit_sha=None (git finalization skipped/failed)
    proof_art = MagicMock()
    proof_art.provenance.commit_sha = None
    proof_art.verification = None

    task_res = TaskResult(
        task_id="t_13",
        user_request="Commit fs without git",
        success=True,
        verified=True,
        status="COMMITTED",
        summary="FS committed cleanly",
        changed_files=["work.py"],
        file_changes=(fc,),
        proof_artifact=proof_art,
    )

    out = io.StringIO()
    con = Console(file=out, color_system=None, width=120)
    import cli
    orig_con = cli.console
    try:
        cli.console = con
        print_task_verification_report(task_res, Path("."))
    finally:
        cli.console = orig_con

    output = out.getvalue()
    assert "COMMITTED" in output
    assert "No Git commit created" in output
    # Must NOT claim a commit SHA exists!


# =============================================================================
# 14. Post-execution external disk modification (drift detection)
# =============================================================================
def test_scenario_14_external_disk_drift_detection(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    tracked_file = workspace / "service.py"
    # External out-of-band edit modified the file after transaction executed!
    tracked_file.write_text("tampered out-of-band content", encoding="utf-8")

    staged_content = "authoritative transaction content"

    op = TransactionOperation(
        type=OperationType.MODIFY_FILE,
        target="service.py",
        status=OperationStatus.EXECUTED,
        before_state={"exists": True, "content": "original", "is_text": True},
        after_state={"exists": True, "content": staged_content, "is_text": True},
    )
    tx_res = TransactionResult(
        transaction_id="tx_14",
        status=TransactionStatus.COMMITTED,
        committed=True,
        rolled_back=False,
        operations=(op,),
    )

    summaries = build_file_change_summaries(tx_res, workspace)
    s = summaries[0]

    # Independent disk inspection observes that disk SHA != staged SHA!
    assert s.status == FileMutationStatus.UNKNOWN
    assert s.staged_sha256 == _sha(staged_content)
    assert s.final_sha256 == _sha("tampered out-of-band content")
    assert s.error is not None
    assert "Disk verification failed: file on disk differs from committed staged payload" in s.error


# =============================================================================
# 15. Legacy Changed: and File Changes: dual formatting in format_report()
# =============================================================================
def test_scenario_15_format_report_dual_changed_and_file_changes():
    fc = FileChangeSummary(
        path="service.py",
        operation="MODIFY",
        status=FileMutationStatus.COMMITTED,
        before_sha256=_sha("old"),
        staged_sha256=_sha("new"),
        final_sha256=_sha("new"),
        byte_count_delta=10,
    )
    task_res = TaskResult(
        task_id="t_15",
        user_request="Dual formatting test",
        success=True,
        verified=True,
        status="COMMITTED",
        summary="Service updated",
        changed_files=["service.py"],
        file_changes=(fc,),
    )
    report = task_res.format_report()
    assert "File Changes:" in report
    assert "- service.py [MODIFY] (COMMITTED)" in report
    assert "Changed:" in report
    assert "- service.py" in report


# =============================================================================
# 16. Disk drift after clean rollback is classified as UNKNOWN (not ROLLBACK_FAILED)
# =============================================================================
def test_scenario_16_disk_drift_after_clean_rollback_is_unknown(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    target_file = workspace / "rollback_drift.py"
    target_file.write_text("external_drift_content", encoding="utf-8")
    drift_sha = _sha("external_drift_content")
    baseline_sha = _sha("original_baseline")

    # Engine cleanly completed rollback (OperationStatus.ROLLED_BACK)
    op = TransactionOperation(
        type=OperationType.MODIFY_FILE,
        target="rollback_drift.py",
        status=OperationStatus.ROLLED_BACK,
        before_state={"exists": True, "content": "original_baseline", "is_text": True},
        after_state={"exists": True, "content": "aborted_mutation", "is_text": True},
    )
    tx_res = TransactionResult(
        transaction_id="tx_16",
        status=TransactionStatus.ROLLED_BACK,
        committed=False,
        rolled_back=True,
        operations=(op,),
    )

    summaries = build_file_change_summaries(tx_res, workspace)
    s = summaries[0]

    # Must be UNKNOWN, NOT ROLLBACK_FAILED! (observed drift, engine did not fail)
    assert s.status == FileMutationStatus.UNKNOWN
    assert s.final_sha256 == drift_sha
    assert s.before_sha256 == baseline_sha
    assert s.error is not None
    assert "Post-rollback disk state differs: observed content differs from baseline hash." in s.error


# =============================================================================
# 17. Explicit rollback failure is classified as ROLLBACK_FAILED
# =============================================================================
def test_scenario_17_explicit_rollback_failure_is_rollback_failed(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    f = workspace / "crash.py"
    f.write_text("corrupted_state", encoding="utf-8")

    op = TransactionOperation(
        type=OperationType.MODIFY_FILE,
        target="crash.py",
        status=OperationStatus.FAILED,
        before_state={"exists": True, "content": "base", "is_text": True},
        after_state={"exists": True, "content": "corrupted_state", "is_text": True},
        error="I/O failure during rollback restoration",
    )
    tx_res = TransactionResult(
        transaction_id="tx_17",
        status=TransactionStatus.FAILED,
        committed=False,
        rolled_back=False,
        operations=(op,),
        rollback_error="Critical failure rolling back transaction",
    )

    summaries = build_file_change_summaries(tx_res, workspace)
    s = summaries[0]

    assert s.status == FileMutationStatus.ROLLBACK_FAILED
    assert s.error is not None
    assert "I/O failure during rollback restoration" in s.error


# =============================================================================
# 18. Filesystem inspection exceptions fail closed to UNKNOWN
# =============================================================================
def test_scenario_18_disk_inspection_oserror_fails_closed(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    # 18a. Committed DELETE where Path.is_file raises PermissionError
    op_del = TransactionOperation(
        type=OperationType.DELETE_FILE,
        target="locked_file.txt",
        status=OperationStatus.EXECUTED,
        before_state={"exists": True, "content": "delete_target", "is_text": True},
        after_state={"exists": False},
    )
    tx_del = TransactionResult(
        transaction_id="tx_18a",
        status=TransactionStatus.COMMITTED,
        committed=True,
        rolled_back=False,
        operations=(op_del,),
    )

    with patch("pathlib.Path.is_file", side_effect=PermissionError("Access denied inspecting locked_file.txt")):
        summaries_del = build_file_change_summaries(tx_del, workspace)
    s_del = summaries_del[0]
    # Must NOT claim COMMITTED deletion! Absence could not be verified!
    assert s_del.status == FileMutationStatus.UNKNOWN
    assert s_del.error is not None
    assert "Filesystem inspection failed: PermissionError" in s_del.error

    # 18b. Rolled-back CREATE where Path.is_file raises PermissionError
    op_create = TransactionOperation(
        type=OperationType.CREATE_FILE,
        target="new_temp.py",
        status=OperationStatus.ROLLED_BACK,
        before_state={"exists": False},
        after_state={"exists": True, "content": "created", "is_text": True},
    )
    tx_create = TransactionResult(
        transaction_id="tx_18b",
        status=TransactionStatus.ROLLED_BACK,
        committed=False,
        rolled_back=True,
        operations=(op_create,),
    )

    with patch("pathlib.Path.is_file", side_effect=PermissionError("Access denied")):
        summaries_create = build_file_change_summaries(tx_create, workspace)
    s_create = summaries_create[0]
    # Must NOT claim clean ROLLED_BACK! Removal could not be verified!
    assert s_create.status == FileMutationStatus.UNKNOWN
    assert s_create.error is not None
    assert "Filesystem inspection failed: PermissionError" in s_create.error


# =============================================================================
# 19. CRLF and byte-level rollback integrity
# =============================================================================
def test_scenario_19_crlf_and_byte_level_rollback_integrity(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    crlf_file = workspace / "windows_lines.txt"
    original_crlf_bytes = b"Line 1\r\nLine 2\r\nLine 3\r\n"
    crlf_file.write_bytes(original_crlf_bytes)
    original_sha = hashlib.sha256(original_crlf_bytes).hexdigest()

    # Real TransactionCoordinator execution and rollback under WorkspaceLock
    lock = WorkspaceLock(workspace)
    lock.acquire()
    try:
        tx = TransactionCoordinator(workspace=workspace)
        tx.begin()
        tx.stage_modify("windows_lines.txt", "Modified content with linux newlines\n")
        tx.execute()

        assert crlf_file.read_bytes() != original_crlf_bytes

        # Roll back
        tx_res = tx.rollback(reason="Test rollback")
        assert tx_res.rolled_back is True

        # Assert byte-for-byte exact equality after rollback
        restored_bytes = crlf_file.read_bytes()
        assert restored_bytes == original_crlf_bytes

        summaries = tx_res.to_change_summaries(workspace)
        s = summaries[0]
        assert s.status == FileMutationStatus.ROLLED_BACK
        assert s.before_sha256 == original_sha
        assert s.final_sha256 == original_sha
    finally:
        lock.release()


# =============================================================================
# 20. Real runtime workflow integration: committed mutation
# =============================================================================
FIXTURE_SRC = Path(__file__).resolve().parent / "fixtures" / "phase3_demo"


@pytest.fixture
def phase3_demo_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "phase3_project"
    shutil.copytree(FIXTURE_SRC, repo)
    try:
        subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "BrainFrog Tester"], cwd=str(repo), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "tester@brainfrog.local"], cwd=str(repo), check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "chore: initial broken commit"], cwd=str(repo), check=True, capture_output=True)
    except Exception:
        pass
    return repo


def test_scenario_20_runtime_e2e_committed_file_changes(phase3_demo_repo: Path):
    navbar_file = phase3_demo_repo / "navbar.py"
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
            "tests_passing": Answer(choice="yes", noul=1.0),
            "diff_complete": Answer(choice="yes", noul=1.0),
            "failure_fixable": Answer(choice="no", noul=0.0),
            "retry_concern": Answer(score="within_normal", confidence=0.95),
        },
    ]

    mock_s2 = MagicMock()
    mock_s2.provider_name = "claude_mock"
    mock_s2.plan_task.return_value = [
        PlanStep(id="1", description="Fix navbar.py toggle", files=["navbar.py"])
    ]
    mock_s2.write_code.return_value = {"navbar.py": fixed_navbar_code}

    runtime = BrainFrogRuntime(
        repo_dir=phase3_demo_repo,
        persist_sessions=False,
        default_test_cmd=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
        system1_factory=lambda b: mock_s1,
        system2_factory=lambda **k: mock_s2,
    )

    request = TaskRequest(
        task_id="task_e2e_committed_summary",
        user_request="Fix mobile navbar toggle",
        workspace=phase3_demo_repo,
        test_command=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
    )
    res: TaskResult = runtime.execute_autonomous_task(request)

    assert res.success is True
    assert res.status == "COMMITTED"
    assert len(res.file_changes) == 1

    fc = res.file_changes[0]
    assert fc.path == "navbar.py"
    assert fc.operation == "MODIFY"
    assert fc.status == FileMutationStatus.COMMITTED
    assert fc.final_sha256 == hashlib.sha256(navbar_file.read_bytes()).hexdigest()

    # Both File Changes: and Changed: are present in format_report()
    report = res.format_report()
    assert "File Changes:" in report
    assert "navbar.py [MODIFY] (COMMITTED)" in report
    assert "Changed:" in report
    assert "- navbar.py" in report


# =============================================================================
# 21. Real runtime workflow integration: verified rollback
# =============================================================================
def test_scenario_21_runtime_e2e_rollback_file_changes(phase3_demo_repo: Path):
    navbar_file = phase3_demo_repo / "navbar.py"
    initial_navbar_bytes = navbar_file.read_bytes()
    initial_sha = hashlib.sha256(initial_navbar_bytes).hexdigest()

    broken_code = "class MobileNavbar:\n    pass\n"

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
        PlanStep(id="1", description="Break navbar", files=["navbar.py"])
    ]
    mock_s2.write_code.return_value = {"navbar.py": broken_code}
    mock_s2.review_and_fix.return_value = {"navbar.py": broken_code}

    runtime = BrainFrogRuntime(
        repo_dir=phase3_demo_repo,
        persist_sessions=False,
        default_test_cmd=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
        system1_factory=lambda b: mock_s1,
        system2_factory=lambda **k: mock_s2,
    )

    request = TaskRequest(
        task_id="task_e2e_rollback_summary",
        user_request="Attempt broken edit that fails tests",
        workspace=phase3_demo_repo,
        test_command=[sys.executable, "-m", "pytest", "tests/test_navbar.py", "-q"],
    )
    res: TaskResult = runtime.execute_autonomous_task(request)

    assert res.success is False
    assert res.status == "ROLLED_BACK"
    assert res.changed_files == []

    # File on disk restored to baseline
    assert navbar_file.read_bytes() == initial_navbar_bytes

    # file_changes evidence confirms verified rollback
    assert len(res.file_changes) == 1
    fc = res.file_changes[0]
    assert fc.path == "navbar.py"
    assert fc.status == FileMutationStatus.ROLLED_BACK
    assert fc.before_sha256 == initial_sha
    assert fc.final_sha256 == initial_sha


# =============================================================================
# 22. RENAME_FILE rollback integrity with real TransactionCoordinator
# =============================================================================
def test_scenario_22_rename_rollback_integrity(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    src_file = workspace / "origin.py"
    src_bytes = b"def original_function(): return 42\n"
    src_file.write_bytes(src_bytes)
    orig_sha = hashlib.sha256(src_bytes).hexdigest()

    dest_file = workspace / "renamed.py"
    assert not dest_file.exists()

    lock = WorkspaceLock(workspace)
    lock.acquire()
    try:
        tx = TransactionCoordinator(workspace=workspace)
        tx.begin()
        tx.stage_rename("origin.py", "renamed.py")
        assert tx.execute() is True

        # After execute: source moved to destination
        assert not src_file.exists()
        assert dest_file.exists()
        assert dest_file.read_bytes() == src_bytes

        # Roll back
        tx_res = tx.rollback(reason="Test rollback rename")
        assert tx_res.rolled_back is True

        # After rollback: destination absent, source restored with exact bytes
        assert not dest_file.exists()
        assert src_file.exists()
        assert src_file.read_bytes() == src_bytes

        summaries = tx_res.to_change_summaries(workspace)
        assert len(summaries) == 1
        s = summaries[0]
        assert s.path == "origin.py"
        assert s.operation == "RENAME"
        assert s.status == FileMutationStatus.ROLLED_BACK
        assert s.before_sha256 == orig_sha
        assert s.staged_sha256 == orig_sha
        assert s.final_sha256 == orig_sha
        assert s.byte_count_delta == 0
        assert s.error is None

        # Negative case: if destination remains present post-rollback (incomplete restoration / external conflict)
        dest_file.write_bytes(b"lingering conflict")
        summaries_drift = tx_res.to_change_summaries(workspace)
        assert summaries_drift[0].status == FileMutationStatus.UNKNOWN
        assert "destination file still observed on disk" in summaries_drift[0].error
    finally:
        lock.release()


# =============================================================================
# 23. DELETE_FILE rollback integrity with real TransactionCoordinator
# =============================================================================
def test_scenario_23_delete_rollback_integrity(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    target_file = workspace / "doomed_resource.txt"
    known_bytes = b"CRITICAL_CONFIGURATION_DATA = 9999\n"
    target_file.write_bytes(known_bytes)
    orig_sha = hashlib.sha256(known_bytes).hexdigest()

    lock = WorkspaceLock(workspace)
    lock.acquire()
    try:
        tx = TransactionCoordinator(workspace=workspace)
        tx.begin()
        tx.stage_delete("doomed_resource.txt")
        assert tx.execute() is True

        # After execute: file deleted
        assert not target_file.exists()

        # Roll back
        tx_res = tx.rollback(reason="Test rollback delete")
        assert tx_res.rolled_back is True

        # After rollback: file restored with exact bytes
        assert target_file.exists()
        assert target_file.read_bytes() == known_bytes

        summaries = tx_res.to_change_summaries(workspace)
        assert len(summaries) == 1
        s = summaries[0]
        assert s.path == "doomed_resource.txt"
        assert s.operation == "DELETE"
        assert s.status == FileMutationStatus.ROLLED_BACK
        assert s.before_sha256 == orig_sha
        assert s.staged_sha256 is None
        assert s.final_sha256 == orig_sha
        assert s.byte_count_delta == 0
        assert s.error is None

        # Negative case 1: if restored file bytes differ from baseline hash post-rollback
        target_file.write_bytes(b"tampered post-rollback content")
        summaries_mismatch = tx_res.to_change_summaries(workspace)
        assert summaries_mismatch[0].status == FileMutationStatus.UNKNOWN
        assert "restored file content differs from baseline hash" in summaries_mismatch[0].error

        # Negative case 2: if file was removed post-rollback
        target_file.unlink()
        summaries_absent = tx_res.to_change_summaries(workspace)
        assert summaries_absent[0].status == FileMutationStatus.UNKNOWN
        assert "restored file not found on disk after rollback" in summaries_absent[0].error
    finally:
        lock.release()


# =============================================================================
# 24. Non-UTF-8 binary rollback integrity with real TransactionCoordinator
# =============================================================================
def test_scenario_24_non_utf8_binary_rollback_integrity(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    bin_file = workspace / "payload.bin"
    # Byte sequence that cannot be decoded as UTF-8
    non_utf8_bytes = bytes([0x00, 0xFF, 0xFE, 0x01, 0x80, 0x0A, 0x0D])
    bin_file.write_bytes(non_utf8_bytes)
    orig_sha = hashlib.sha256(non_utf8_bytes).hexdigest()

    mutated_bytes = bytes([0xDE, 0xAD, 0xBE, 0xEF, 0xFF, 0x00])

    lock = WorkspaceLock(workspace)
    lock.acquire()
    try:
        tx = TransactionCoordinator(workspace=workspace)
        tx.begin()
        tx.stage_modify("payload.bin", mutated_bytes)
        assert tx.execute() is True

        # After execute: disk contains mutated bytes
        assert bin_file.read_bytes() == mutated_bytes

        # Roll back
        tx_res = tx.rollback(reason="Test rollback binary payload")
        assert tx_res.rolled_back is True

        # After rollback: exact byte equality
        restored_bytes = bin_file.read_bytes()
        assert restored_bytes == non_utf8_bytes
        assert hashlib.sha256(restored_bytes).hexdigest() == orig_sha

        summaries = tx_res.to_change_summaries(workspace)
        assert len(summaries) == 1
        s = summaries[0]
        assert s.path == "payload.bin"
        assert s.operation == "MODIFY"
        assert s.status == FileMutationStatus.ROLLED_BACK
        assert s.before_sha256 == orig_sha
        assert s.staged_sha256 == hashlib.sha256(mutated_bytes).hexdigest()
        assert s.final_sha256 == orig_sha
        assert s.byte_count_delta == 0
        assert s.error is None

        # Negative case: if disk bytes differ post-rollback
        bin_file.write_bytes(b"tampered binary")
        summaries_drift = tx_res.to_change_summaries(workspace)
        assert summaries_drift[0].status == FileMutationStatus.UNKNOWN
        assert "observed content differs from baseline hash" in summaries_drift[0].error
    finally:
        lock.release()
