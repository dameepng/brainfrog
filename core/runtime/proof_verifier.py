"""Deterministic Proof Verification Interface for BrainFrog Proof Artifacts.

Implements BrainFrog Phase 6C: Deterministic Proof Verification Interface.
Product thesis: "Don't just trust the agent. Verify it."

Evaluates whether a persisted BrainFrog proof artifact is structurally valid,
internally consistent, and supported by concrete execution evidence.

The verifier does NOT trust:
- LLM assertions or text descriptions.
- A proof's self-declared 'VERIFIED' status.
- Markdown rendered from JSON (treated as derived presentation only).
- A test exit code without validating that test execution evidence is present.
- A Git commit SHA merely because the proof contains it.
- Plain hashes stored alongside files as proof of execution authenticity.

Status model:
- VALID: all required structural and cross-field consistency checks pass.
- INVALID: contradictory, malformed, tampered, or demonstrably inconsistent evidence.
- INCOMPLETE: required evidence is missing or cannot be independently established.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

from core.runtime.proof import (
    VALID_VERDICT_STATUSES,
    FileProofStore,
    ProofArtifact,
)
from core.runtime.task_finalization import (
    CommitIntentRecord,
    CommitIntentStatus,
    IntentCorruptError,
    TaskFinalizationCoordinator,
)

logger = logging.getLogger(__name__)

SUPPORTED_SCHEMA_VERSIONS: Set[str] = {"1.0.0"}
VALID_CHANGES_STATUSES: Set[str] = {"COMMITTED", "ROLLED_BACK", "FAILED", "CANCELLED"}
GIT_HEX_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")

MANDATORY_VERIFICATION_GATES: Tuple[str, ...] = (
    "workspace_scope",
    "test_suite",
    "transaction_integrity",
    "execution_integrity",
    "trust_integrity",
)


def is_safe_workspace_relative_path(path_str: str) -> bool:
    r"""Platform-independent validation ensuring path_str is a safe workspace-relative path.

    Rejects:
    - Non-string, empty, or whitespace-only paths.
    - Null bytes (`\0`).
    - Colons anywhere (Windows drive-absolute C:, drive-relative C:foo, NTFS alternate data streams).
    - Leading slash or backslash (POSIX root `/`, Windows root-relative `\`, UNC `//` or `\\`).
    - Consecutive separators (`//`, `\\`, `/\`, `\/`).
    - Traversal segments (`..`) regardless of slash convention.
    - Redundant empty segments or whitespace-only segments.
    - Segments with trailing whitespace or trailing dots (invalid/ambiguous on Windows NTFS).
    """
    if not isinstance(path_str, str) or not path_str or not path_str.strip():
        return False
    if "\0" in path_str:
        return False
    if ":" in path_str:
        return False
    if path_str.startswith("/") or path_str.startswith("\\"):
        return False
    if "//" in path_str or "\\\\" in path_str or "/\\" in path_str or "\\/" in path_str:
        return False

    norm = path_str.replace("\\", "/")
    parts = norm.split("/")
    depth = 0
    for part in parts:
        if not part or not part.strip():
            return False
        if part == "..":
            return False
        if part != part.rstrip():
            return False
        if part != "." and part.endswith("."):
            return False
        if part == ".":
            continue
        depth += 1

    return depth > 0


class ProofVerificationStatus(str, Enum):
    """Overall verdict states for proof verification."""

    VALID = "VALID"
    INVALID = "INVALID"
    INCOMPLETE = "INCOMPLETE"


@dataclass(frozen=True)
class VerificationCheckResult:
    """Individual verification check result."""

    check_name: str
    status: ProofVerificationStatus
    message: str
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProofVerificationReport:
    """Comprehensive report produced by the deterministic proof verifier."""

    target: str
    overall_status: ProofVerificationStatus
    task_id: Optional[str] = None
    verdict_status: Optional[str] = None
    transaction_status: Optional[str] = None
    checks: List[VerificationCheckResult] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    proof_path: Optional[str] = None
    provenance_summary: Optional[Dict[str, Any]] = None
    is_git: bool = False
    commit_sha: Optional[str] = None

    @property
    def is_valid(self) -> bool:
        return self.overall_status == ProofVerificationStatus.VALID

    @property
    def is_invalid(self) -> bool:
        return self.overall_status == ProofVerificationStatus.INVALID

    @property
    def is_incomplete(self) -> bool:
        return self.overall_status == ProofVerificationStatus.INCOMPLETE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target": self.target,
            "overall_status": self.overall_status.value,
            "task_id": self.task_id,
            "verdict_status": self.verdict_status,
            "transaction_status": self.transaction_status,
            "reasons": self.reasons,
            "proof_path": self.proof_path,
            "is_git": self.is_git,
            "commit_sha": self.commit_sha,
            "provenance_summary": self.provenance_summary,
            "checks": [
                {
                    "check_name": c.check_name,
                    "status": c.status.value,
                    "message": c.message,
                    "details": c.details,
                }
                for c in self.checks
            ],
        }


class DeterministicProofVerifier:
    """Deterministic, read-only proof verification engine."""

    def __init__(self, workspace: Union[str, Path]) -> None:
        self.workspace = Path(workspace).resolve()
        self.finalization = TaskFinalizationCoordinator(self.workspace)
        self.store = FileProofStore(self.workspace)

    def _resolve_target_path(self, target: Union[str, Path]) -> Tuple[Path, Optional[str]]:
        """Resolve target string or Path to an existing or expected JSON proof file."""
        target_str = str(target)
        if "\0" in target_str:
            raise ValueError("Target path contains embedded null bytes")
        target_path = Path(target)
        if target_path.is_file():
            resolved = target_path.resolve()
            return resolved, resolved.stem

        # If the target explicitly specifies a .json file path:
        if target_str.endswith(".json") or target_path.suffix == ".json":
            if target_path.is_absolute():
                resolved = target_path.resolve()
                return resolved, resolved.stem
            as_rel_in_ws = (self.workspace / target_path).resolve()
            if as_rel_in_ws.is_file():
                return as_rel_in_ws, as_rel_in_ws.stem
            as_in_store = (self.store.proofs_dir / target_path.name).resolve()
            if as_in_store.is_file():
                return as_in_store, as_in_store.stem
            # Missing explicit .json target: resolve path without double suffixing
            if target_path.parent != Path("."):
                return as_rel_in_ws, target_path.stem
            return as_in_store, target_path.stem

        # Treat as task_id or relative proof path within workspace
        as_json_in_store = self.store.proof_json_path(target_str)
        if as_json_in_store.exists():
            return as_json_in_store, as_json_in_store.stem

        as_rel_in_ws = (self.workspace / target_path).resolve()
        if as_rel_in_ws.exists() and as_rel_in_ws.is_file():
            return as_rel_in_ws, as_rel_in_ws.stem

        # Fallback path if task_id doesn't exist
        return as_json_in_store, as_json_in_store.stem

    def verify(self, target: Union[str, Path]) -> ProofVerificationReport:
        """Inspect and verify a proof artifact by task ID or filepath.

        Execution is strictly read-only: does not modify or delete any file.
        """
        target_str = str(target)
        if "\0" in target_str:
            safe_display = target_str.replace("\0", "\\x00")
            msg = f"Target path or task ID contains embedded null bytes (malformed path): '{safe_display}'"
            report = ProofVerificationReport(
                target=target_str,
                overall_status=ProofVerificationStatus.INVALID,
                task_id=None,
                proof_path=None,
            )
            report.checks.append(
                VerificationCheckResult(
                    check_name="target_path_safety",
                    status=ProofVerificationStatus.INVALID,
                    message=msg,
                )
            )
            report.reasons.append(msg)
            return report

        proof_path, derived_task_id = self._resolve_target_path(target)

        report = ProofVerificationReport(
            target=target_str,
            overall_status=ProofVerificationStatus.VALID,
            task_id=derived_task_id,
            proof_path=str(proof_path) if proof_path.exists() else None,
        )

        # -----------------------------------------------------------------
        # Check 1: File Existence and Readability
        # -----------------------------------------------------------------
        if not proof_path.exists():
            try:
                intent = self.finalization.get_intent(derived_task_id or str(target))
            except IntentCorruptError as ice:
                msg = (
                    f"Proof artifact JSON file not found at '{proof_path}', and task finalization intent "
                    f"file is corrupt: {ice.message}"
                )
                report.checks.append(
                    VerificationCheckResult(
                        check_name="finalization_intent_integrity",
                        status=ProofVerificationStatus.INVALID,
                        message=msg,
                        details={"path": str(ice.path) if ice.path else None},
                    )
                )
                report.reasons.append(msg)
                report.overall_status = ProofVerificationStatus.INVALID
                return report

            details: Dict[str, Any] = {}
            if intent is not None:
                details = {
                    "intent_task_id": intent.task_id,
                    "intent_status": intent.status.value,
                    "commit_sha": intent.commit_sha,
                }
                msg = (
                    f"Proof artifact JSON file not found at '{proof_path}', but task finalization intent "
                    f"exists in status '{intent.status.value}' (commit SHA: {intent.commit_sha}). "
                    "Transaction may have committed with failed proof persistence or pending recovery."
                )
            else:
                msg = f"Proof artifact JSON file not found at '{proof_path}'."

            report.checks.append(
                VerificationCheckResult(
                    check_name="file_existence",
                    status=ProofVerificationStatus.INCOMPLETE,
                    message=msg,
                    details=details,
                )
            )
            report.reasons.append(msg)
            report.overall_status = ProofVerificationStatus.INCOMPLETE
            return report

        try:
            raw_text = proof_path.read_text(encoding="utf-8")
        except Exception as read_err:
            report.checks.append(
                VerificationCheckResult(
                    check_name="file_readability",
                    status=ProofVerificationStatus.INVALID,
                    message=f"Failed to read proof file: {read_err}",
                )
            )
            report.reasons.append(f"Failed to read proof file: {read_err}")
            report.overall_status = ProofVerificationStatus.INVALID
            return report

        # -----------------------------------------------------------------
        # Check 2: JSON Parsing
        # -----------------------------------------------------------------
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as json_err:
            report.checks.append(
                VerificationCheckResult(
                    check_name="json_syntax",
                    status=ProofVerificationStatus.INVALID,
                    message=f"Malformed JSON syntax: {json_err}",
                )
            )
            report.reasons.append(f"Malformed JSON syntax: {json_err}")
            report.overall_status = ProofVerificationStatus.INVALID
            return report

        if not isinstance(data, dict):
            report.checks.append(
                VerificationCheckResult(
                    check_name="json_structure",
                    status=ProofVerificationStatus.INVALID,
                    message="Proof root JSON must be an object/dict.",
                )
            )
            report.reasons.append("Proof root JSON must be an object/dict.")
            report.overall_status = ProofVerificationStatus.INVALID
            return report

        report.checks.append(
            VerificationCheckResult(
                check_name="json_syntax",
                status=ProofVerificationStatus.VALID,
                message="Proof JSON is well-formed.",
            )
        )

        # -----------------------------------------------------------------
        # Check 3: Schema and Version Validity
        # -----------------------------------------------------------------
        schema_version = data.get("version")
        if not schema_version or schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            report.checks.append(
                VerificationCheckResult(
                    check_name="schema_version",
                    status=ProofVerificationStatus.INVALID,
                    message=f"Unsupported or missing schema version '{schema_version}'. Supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}",
                )
            )
            report.reasons.append(f"Unsupported schema version: '{schema_version}'.")
            report.overall_status = ProofVerificationStatus.INVALID
        else:
            report.checks.append(
                VerificationCheckResult(
                    check_name="schema_version",
                    status=ProofVerificationStatus.VALID,
                    message=f"Schema version '{schema_version}' is supported.",
                )
            )

        required_sections = ["task", "verdict", "verification", "changes", "reproducibility"]
        missing_sections = [sec for sec in required_sections if sec not in data or not isinstance(data[sec], dict)]
        if missing_sections:
            report.checks.append(
                VerificationCheckResult(
                    check_name="required_sections",
                    status=ProofVerificationStatus.INVALID,
                    message=f"Missing or invalid required sections: {missing_sections}",
                )
            )
            report.reasons.append(f"Missing required sections: {missing_sections}")
            report.overall_status = ProofVerificationStatus.INVALID
            return report

        # Extract primary sections
        task_sec = data["task"]
        verdict_sec = data["verdict"]
        verification_sec = data["verification"]
        changes_sec = data["changes"]
        provenance_sec = data.get("provenance")
        repro_sec = data.get("reproducibility", {})

        actual_task_id = str(task_sec.get("task_id", "")).strip()
        report.task_id = actual_task_id or derived_task_id
        claimed_verdict = str(verdict_sec.get("status", "")).strip()
        report.verdict_status = claimed_verdict
        claimed_changes_status = str(changes_sec.get("status", "")).strip().upper()
        report.transaction_status = claimed_changes_status

        # Validate verdict status value
        if claimed_verdict not in VALID_VERDICT_STATUSES:
            report.checks.append(
                VerificationCheckResult(
                    check_name="verdict_status_value",
                    status=ProofVerificationStatus.INVALID,
                    message=f"Unknown verdict status '{claimed_verdict}'. Must be one of {sorted(VALID_VERDICT_STATUSES)}.",
                )
            )
            report.reasons.append(f"Unknown verdict status: '{claimed_verdict}'.")
            report.overall_status = ProofVerificationStatus.INVALID
        else:
            report.checks.append(
                VerificationCheckResult(
                    check_name="verdict_status_value",
                    status=ProofVerificationStatus.VALID,
                    message=f"Verdict status '{claimed_verdict}' is a recognized value.",
                )
            )

        # Validate changes status value
        if claimed_changes_status not in VALID_CHANGES_STATUSES:
            report.checks.append(
                VerificationCheckResult(
                    check_name="changes_status_value",
                    status=ProofVerificationStatus.INVALID,
                    message=f"Unknown changes status '{claimed_changes_status}'. Must be one of {sorted(VALID_CHANGES_STATUSES)}.",
                )
            )
            report.reasons.append(f"Unknown changes status: '{claimed_changes_status}'.")
            report.overall_status = ProofVerificationStatus.INVALID

        # Validate required task fields
        if not actual_task_id or not str(task_sec.get("description", "")).strip():
            report.checks.append(
                VerificationCheckResult(
                    check_name="task_identity",
                    status=ProofVerificationStatus.INVALID,
                    message="Task evidence has empty task_id or empty description.",
                )
            )
            report.reasons.append("Task identity fields are empty or invalid.")
            report.overall_status = ProofVerificationStatus.INVALID

        # -----------------------------------------------------------------
        # Check 4: Task ID Cross-Field Consistency
        # -----------------------------------------------------------------
        filename_stem = proof_path.stem
        if derived_task_id and filename_stem != actual_task_id:
            report.checks.append(
                VerificationCheckResult(
                    check_name="task_id_filename_match",
                    status=ProofVerificationStatus.INVALID,
                    message=f"Proof filename stem '{filename_stem}' does not match task_id '{actual_task_id}'.",
                )
            )
            report.reasons.append(f"Filename stem '{filename_stem}' differs from internal task_id '{actual_task_id}'.")
            report.overall_status = ProofVerificationStatus.INVALID
        else:
            report.checks.append(
                VerificationCheckResult(
                    check_name="task_id_filename_match",
                    status=ProofVerificationStatus.VALID,
                    message="Proof filename stem matches internal task_id.",
                )
            )

        proc_ev = verification_sec.get("process_evidence")
        if isinstance(proc_ev, dict):
            proc_tid = str(proc_ev.get("task_id", "")).strip()
            if proc_tid and proc_tid != actual_task_id:
                report.checks.append(
                    VerificationCheckResult(
                        check_name="process_evidence_task_id",
                        status=ProofVerificationStatus.INVALID,
                        message=f"Process evidence task_id '{proc_tid}' does not match task_id '{actual_task_id}'.",
                    )
                )
                report.reasons.append(f"Process evidence task_id '{proc_tid}' contradicts task_id '{actual_task_id}'.")
                report.overall_status = ProofVerificationStatus.INVALID

        # -----------------------------------------------------------------
        # Check 5: Task Finalization Intent Cross-Reference
        # -----------------------------------------------------------------
        try:
            intent_record = self.finalization.get_intent(actual_task_id)
        except IntentCorruptError as ice:
            report.checks.append(
                VerificationCheckResult(
                    check_name="finalization_intent_integrity",
                    status=ProofVerificationStatus.INVALID,
                    message=f"Finalization intent record is corrupt or unreadable: {ice.message}",
                    details={"path": str(ice.path) if ice.path else None},
                )
            )
            report.reasons.append(f"Finalization intent file is corrupt or unreadable: {ice.message}")
            report.overall_status = ProofVerificationStatus.INVALID
            intent_record = None

        if intent_record is not None:
            # Check intent task_id
            if intent_record.task_id != actual_task_id:
                report.checks.append(
                    VerificationCheckResult(
                        check_name="finalization_intent_id",
                        status=ProofVerificationStatus.INVALID,
                        message=f"Finalization intent task_id '{intent_record.task_id}' differs from proof '{actual_task_id}'.",
                    )
                )
                report.reasons.append("Finalization intent task_id mismatch.")
                report.overall_status = ProofVerificationStatus.INVALID

            # Check intent commit_sha consistency against proof provenance
            if intent_record.commit_sha:
                proof_commit_sha = None
                if isinstance(provenance_sec, dict):
                    proof_commit_sha = provenance_sec.get("commit_sha") or provenance_sec.get("final_head_sha")
                if proof_commit_sha and proof_commit_sha != intent_record.commit_sha:
                    report.checks.append(
                        VerificationCheckResult(
                            check_name="commit_sha_intent_consistency",
                            status=ProofVerificationStatus.INVALID,
                            message=(
                                f"Authoritative commit intent SHA '{intent_record.commit_sha}' "
                                f"contradicts proof commit SHA '{proof_commit_sha}'."
                            ),
                        )
                    )
                    report.reasons.append(
                        f"Commit SHA mismatch: intent has '{intent_record.commit_sha}', proof has '{proof_commit_sha}'."
                    )
                    report.overall_status = ProofVerificationStatus.INVALID

            # An active unfinalized intent indicates the task finalization was never cleanly closed
            if intent_record.status in (
                CommitIntentStatus.COMMITTING,
                CommitIntentStatus.COMMITTED_PENDING_PROOF,
                CommitIntentStatus.RECOVERY_REQUIRED,
            ):
                if claimed_verdict == "VERIFIED":
                    report.checks.append(
                        VerificationCheckResult(
                            check_name="unresolved_intent_state",
                            status=ProofVerificationStatus.INVALID,
                            message=(
                                f"Proof claims VERIFIED, but workspace retains unresolved commit intent "
                                f"in state '{intent_record.status.value}'."
                            ),
                        )
                    )
                    report.reasons.append(
                        f"Unresolved commit intent in state '{intent_record.status.value}' contradicts VERIFIED status."
                    )
                    report.overall_status = ProofVerificationStatus.INVALID
                else:
                    report.checks.append(
                        VerificationCheckResult(
                            check_name="unresolved_intent_state",
                            status=ProofVerificationStatus.INCOMPLETE,
                            message=f"Workspace retains unresolved commit intent in state '{intent_record.status.value}'. Recovery required.",
                        )
                    )
                    report.reasons.append(
                        f"Commit intent unresolved: '{intent_record.status.value}'. Workspace requires recovery."
                    )
                    if report.overall_status != ProofVerificationStatus.INVALID:
                        report.overall_status = ProofVerificationStatus.INCOMPLETE

        # -----------------------------------------------------------------
        # Check 6: Verdict vs Transaction Outcome Consistency
        # -----------------------------------------------------------------
        if claimed_verdict == "VERIFIED":
            if claimed_changes_status != "COMMITTED":
                report.checks.append(
                    VerificationCheckResult(
                        check_name="verdict_transaction_consistency",
                        status=ProofVerificationStatus.INVALID,
                        message=f"Contradiction: Verdict is VERIFIED but changes status is '{claimed_changes_status}' (must be COMMITTED).",
                    )
                )
                report.reasons.append(f"VERIFIED verdict contradicts changes status '{claimed_changes_status}'.")
                report.overall_status = ProofVerificationStatus.INVALID
            else:
                report.checks.append(
                    VerificationCheckResult(
                        check_name="verdict_transaction_consistency",
                        status=ProofVerificationStatus.VALID,
                        message="VERIFIED verdict aligns with COMMITTED changes status.",
                    )
                )
        elif claimed_changes_status == "ROLLED_BACK":
            if claimed_verdict == "VERIFIED":
                report.checks.append(
                    VerificationCheckResult(
                        check_name="verdict_transaction_consistency",
                        status=ProofVerificationStatus.INVALID,
                        message="Contradiction: Changes were rolled back, but verdict claims VERIFIED.",
                    )
                )
                report.reasons.append("ROLLED_BACK changes contradict VERIFIED verdict.")
                report.overall_status = ProofVerificationStatus.INVALID
            else:
                report.checks.append(
                    VerificationCheckResult(
                        check_name="verdict_transaction_consistency",
                        status=ProofVerificationStatus.VALID,
                        message=f"Non-verified verdict '{claimed_verdict}' aligns with ROLLED_BACK changes status.",
                    )
                )
        elif claimed_changes_status == "COMMITTED" and claimed_verdict != "VERIFIED":
            report.checks.append(
                VerificationCheckResult(
                    check_name="verdict_transaction_consistency",
                    status=ProofVerificationStatus.INVALID,
                    message=f"Contradiction: Changes were COMMITTED, but verdict is '{claimed_verdict}' (must be VERIFIED to commit).",
                )
            )
            report.reasons.append(f"COMMITTED changes contradict non-verified verdict '{claimed_verdict}'.")
            report.overall_status = ProofVerificationStatus.INVALID

        # -----------------------------------------------------------------
        # Check 7: Test Command, Execution Evidence, and Gate Invariants
        # -----------------------------------------------------------------
        exit_code = verification_sec.get("exit_code")
        test_cmd = str(verification_sec.get("test_command", "")).strip()
        gate_checks = verification_sec.get("gate_checks") or {}

        # F-03: exit_code must not be boolean (in Python isinstance(False, int) is True!)
        if isinstance(exit_code, bool) or (exit_code is not None and not isinstance(exit_code, int)):
            report.checks.append(
                VerificationCheckResult(
                    check_name="exit_code_type",
                    status=ProofVerificationStatus.INVALID,
                    message=f"Malformed exit_code field: expected int, got {type(exit_code).__name__} ('{exit_code}').",
                )
            )
            report.reasons.append(f"Malformed exit_code type: {type(exit_code).__name__}.")
            report.overall_status = ProofVerificationStatus.INVALID

        if not isinstance(gate_checks, dict):
            report.checks.append(
                VerificationCheckResult(
                    check_name="gate_checks_structure",
                    status=ProofVerificationStatus.INVALID,
                    message="Verification gate_checks must be a dictionary.",
                )
            )
            report.reasons.append("Verification gate_checks must be a dictionary.")
            report.overall_status = ProofVerificationStatus.INVALID
        else:
            invalid_gates = {k: v for k, v in gate_checks.items() if not isinstance(v, bool)}
            if invalid_gates:
                report.checks.append(
                    VerificationCheckResult(
                        check_name="gate_checks_types",
                        status=ProofVerificationStatus.INVALID,
                        message=f"Tampered or invalid gate check values (must be boolean): {invalid_gates}.",
                    )
                )
                report.reasons.append(f"Invalid gate check values: {invalid_gates}.")
                report.overall_status = ProofVerificationStatus.INVALID

        # F-03: If test_suite gate is True, require:
        # - present integer exit_code of exactly 0 (not None, not bool, not non-zero)
        # - valid non-timeout, non-cancelled execution evidence
        # - non-empty test command
        # This applies across ALL verdicts (both VERIFIED and non-VERIFIED).
        test_gate_val = gate_checks.get("test_suite") if isinstance(gate_checks, dict) else None
        if test_gate_val is True:
            if exit_code is None or isinstance(exit_code, bool) or exit_code != 0:
                report.checks.append(
                    VerificationCheckResult(
                        check_name="test_suite_exit_code_consistency",
                        status=ProofVerificationStatus.INVALID,
                        message=(
                            f"Contradiction: 'test_suite' gate is True, but exit_code is "
                            f"{repr(exit_code)} (must be integer 0)."
                        ),
                    )
                )
                if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0:
                    report.reasons.append(
                        f"Non-zero test exit ({exit_code}) paired with passing test_suite gate check."
                    )
                else:
                    report.reasons.append(
                        f"'test_suite' gate is True but exit_code is {repr(exit_code)} (expected integer 0)."
                    )
                report.overall_status = ProofVerificationStatus.INVALID

            if not test_cmd:
                report.checks.append(
                    VerificationCheckResult(
                        check_name="test_suite_command_consistency",
                        status=ProofVerificationStatus.INVALID,
                        message="Contradiction: 'test_suite' gate is True, but test_command is empty.",
                    )
                )
                report.reasons.append("'test_suite' gate is True but test_command is empty.")
                report.overall_status = ProofVerificationStatus.INVALID

            if isinstance(proc_ev, dict):
                proc_timed_out = bool(proc_ev.get("timed_out", False))
                proc_cancelled = bool(proc_ev.get("cancelled", False))
                proc_code = proc_ev.get("returncode")
                if proc_timed_out or proc_cancelled or (proc_code is not None and (isinstance(proc_code, bool) or proc_code != 0)):
                    report.checks.append(
                        VerificationCheckResult(
                            check_name="test_suite_process_consistency",
                            status=ProofVerificationStatus.INVALID,
                            message=(
                                f"Contradiction: 'test_suite' gate is True, but process evidence indicates "
                                f"failure: returncode={proc_code}, timed_out={proc_timed_out}, cancelled={proc_cancelled}."
                            ),
                        )
                    )
                    report.reasons.append(
                        f"'test_suite' gate is True but process evidence indicates failure (returncode={proc_code}, timed_out={proc_timed_out}, cancelled={proc_cancelled})."
                    )
                    report.overall_status = ProofVerificationStatus.INVALID

        if claimed_verdict == "VERIFIED":
            # Invariant: A VERIFIED task must have executed tests cleanly
            if not test_cmd:
                report.checks.append(
                    VerificationCheckResult(
                        check_name="test_command_presence",
                        status=ProofVerificationStatus.INVALID,
                        message="Claimed VERIFIED verdict lacks any test command.",
                    )
                )
                report.reasons.append("VERIFIED verdict missing test command.")
                report.overall_status = ProofVerificationStatus.INVALID

            if exit_code != 0 or isinstance(exit_code, bool):
                report.checks.append(
                    VerificationCheckResult(
                        check_name="test_exit_code",
                        status=ProofVerificationStatus.INVALID,
                        message=f"Claimed VERIFIED verdict has invalid test exit code: {exit_code} (must be 0).",
                    )
                )
                report.reasons.append(f"VERIFIED verdict has test exit code {exit_code} (expected 0).")
                report.overall_status = ProofVerificationStatus.INVALID

            if not isinstance(proc_ev, dict):
                report.checks.append(
                    VerificationCheckResult(
                        check_name="process_evidence_presence",
                        status=ProofVerificationStatus.INVALID,
                        message="Claimed VERIFIED verdict lacks subprocess process evidence.",
                    )
                )
                report.reasons.append("VERIFIED verdict missing execution process evidence.")
                report.overall_status = ProofVerificationStatus.INVALID
            else:
                proc_cmd = str(proc_ev.get("command", "")).strip()
                proc_code = proc_ev.get("returncode")
                proc_timed_out = bool(proc_ev.get("timed_out", False))
                proc_cancelled = bool(proc_ev.get("cancelled", False))

                if not proc_cmd:
                    report.checks.append(
                        VerificationCheckResult(
                            check_name="process_evidence_command",
                            status=ProofVerificationStatus.INVALID,
                            message="Process evidence command string is empty.",
                        )
                    )
                    report.reasons.append("Empty process evidence command string.")
                    report.overall_status = ProofVerificationStatus.INVALID

                if proc_code != 0 or isinstance(proc_code, bool) or proc_timed_out or proc_cancelled:
                    report.checks.append(
                        VerificationCheckResult(
                            check_name="process_evidence_exit",
                            status=ProofVerificationStatus.INVALID,
                            message=(
                                f"Process evidence indicates failure: returncode={proc_code}, "
                                f"timed_out={proc_timed_out}, cancelled={proc_cancelled}."
                            ),
                        )
                    )
                    report.reasons.append(
                        f"Process evidence returncode={proc_code} contradicts VERIFIED verdict."
                    )
                    report.overall_status = ProofVerificationStatus.INVALID

            # F-04: Enforce all 5 canonical gates for VERIFIED verdict
            if isinstance(gate_checks, dict):
                missing_mandatory = [g for g in MANDATORY_VERIFICATION_GATES if g not in gate_checks]
                if missing_mandatory:
                    for g in missing_mandatory:
                        report.checks.append(
                            VerificationCheckResult(
                                check_name=f"mandatory_gate_{g}",
                                status=ProofVerificationStatus.INVALID,
                                message=f"Claimed VERIFIED verdict is missing mandatory verification gate '{g}'.",
                            )
                        )
                        report.reasons.append(f"Missing mandatory verification gate '{g}' for VERIFIED verdict.")
                    report.overall_status = ProofVerificationStatus.INVALID

                failed_gates = [k for k, v in gate_checks.items() if v is not True]
                if failed_gates:
                    report.checks.append(
                        VerificationCheckResult(
                            check_name="gate_invariants",
                            status=ProofVerificationStatus.INVALID,
                            message=f"Claimed VERIFIED verdict has failing verification gate invariant(s): {failed_gates}.",
                        )
                    )
                    report.reasons.append(f"Verification gate check(s) failed: {failed_gates}.")
                    report.overall_status = ProofVerificationStatus.INVALID
                elif not missing_mandatory:
                    report.checks.append(
                        VerificationCheckResult(
                            check_name="gate_invariants",
                            status=ProofVerificationStatus.VALID,
                            message=f"All {len(gate_checks)} verification gate invariant evaluations passed.",
                        )
                    )
            elif not gate_checks:
                report.checks.append(
                    VerificationCheckResult(
                        check_name="gate_invariants",
                        status=ProofVerificationStatus.INVALID,
                        message="Claimed VERIFIED verdict contains no gate invariant evaluations.",
                    )
                )
                report.reasons.append("Missing verification gate checks in proof.")
                report.overall_status = ProofVerificationStatus.INVALID

        # -----------------------------------------------------------------
        # Check 8: Changed Files Evidence and Integrity
        # -----------------------------------------------------------------
        files_list = changes_sec.get("files")
        if not isinstance(files_list, list):
            report.checks.append(
                VerificationCheckResult(
                    check_name="changes_files_structure",
                    status=ProofVerificationStatus.INVALID,
                    message="Changes evidence 'files' must be a list.",
                )
            )
            report.reasons.append("Changes evidence 'files' structure is invalid.")
            report.overall_status = ProofVerificationStatus.INVALID
        else:
            invalid_paths: List[str] = []
            for f in files_list:
                if not isinstance(f, dict):
                    invalid_paths.append("non-dict-entry")
                    continue
                p = f.get("path")
                if not isinstance(p, str) or not is_safe_workspace_relative_path(p):
                    invalid_paths.append(str(p))
            if invalid_paths:
                report.checks.append(
                    VerificationCheckResult(
                        check_name="change_records_paths",
                        status=ProofVerificationStatus.INVALID,
                        message=f"Detected non-workspace-relative or illegal path entries in changes: {invalid_paths}.",
                    )
                )
                report.reasons.append(
                    f"Illegal path entry in change records: Illegal or non-workspace-relative path: {invalid_paths}."
                )
                report.overall_status = ProofVerificationStatus.INVALID
            else:
                report.checks.append(
                    VerificationCheckResult(
                        check_name="change_records_paths",
                        status=ProofVerificationStatus.VALID,
                        message=f"All {len(files_list)} change record paths are valid workspace-relative paths.",
                    )
                )

        # -----------------------------------------------------------------
        # Check 9: Git Provenance Cross-Reference
        # -----------------------------------------------------------------
        if isinstance(provenance_sec, dict):
            is_git = bool(provenance_sec.get("is_git", False))
            report.is_git = is_git
            c_sha = provenance_sec.get("commit_sha") or provenance_sec.get("final_head_sha")
            report.commit_sha = c_sha
            report.provenance_summary = {
                "is_git": is_git,
                "branch": provenance_sec.get("branch"),
                "initial_head_sha": provenance_sec.get("initial_head_sha"),
                "final_head_sha": provenance_sec.get("final_head_sha"),
                "commit_sha": provenance_sec.get("commit_sha"),
            }

            # Validate SHA formatting
            for sha_field in ("initial_head_sha", "final_head_sha", "commit_sha"):
                sha_val = provenance_sec.get(sha_field)
                if sha_val and not GIT_HEX_SHA_PATTERN.match(str(sha_val)):
                    report.checks.append(
                        VerificationCheckResult(
                            check_name=f"git_sha_format_{sha_field}",
                            status=ProofVerificationStatus.INVALID,
                            message=f"Field '{sha_field}' is not a valid 40-character hex SHA: '{sha_val}'.",
                        )
                    )
                    report.reasons.append(f"Malformed Git SHA in '{sha_field}': '{sha_val}'.")
                    report.overall_status = ProofVerificationStatus.INVALID

            # If workspace has .git, verify whether commit SHA exists in object database
            if is_git and c_sha and (self.workspace / ".git").exists():
                try:
                    chk = subprocess.run(
                        ["git", "cat-file", "-e", f"{c_sha}^{{commit}}"],
                        cwd=self.workspace,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if chk.returncode != 0:
                        # Commit SHA does not exist in local Git database
                        report.checks.append(
                            VerificationCheckResult(
                                check_name="git_commit_existence",
                                status=ProofVerificationStatus.INCOMPLETE,
                                message=(
                                    f"Claimed Git commit SHA '{c_sha}' does not exist in local repository object database. "
                                    "Historical commit cannot be independently re-established."
                                ),
                            )
                        )
                        report.reasons.append(
                            f"Git commit '{c_sha}' not found in local repository. Historical claim unverified."
                        )
                        if report.overall_status != ProofVerificationStatus.INVALID:
                            report.overall_status = ProofVerificationStatus.INCOMPLETE
                    else:
                        report.checks.append(
                            VerificationCheckResult(
                                check_name="git_commit_existence",
                                status=ProofVerificationStatus.VALID,
                                message=f"Commit SHA '{c_sha}' verified present in repository object database.",
                            )
                        )
                except Exception as git_err:
                    report.checks.append(
                        VerificationCheckResult(
                            check_name="git_commit_existence",
                            status=ProofVerificationStatus.INCOMPLETE,
                            message=f"Git inspection failed: {git_err}",
                        )
                    )
                    if report.overall_status != ProofVerificationStatus.INVALID:
                        report.overall_status = ProofVerificationStatus.INCOMPLETE
            elif is_git and c_sha and not (self.workspace / ".git").exists():
                report.checks.append(
                    VerificationCheckResult(
                        check_name="git_commit_existence",
                        status=ProofVerificationStatus.INCOMPLETE,
                        message=(
                            f"Proof claims Git commit SHA '{c_sha}', but workspace has no .git repository. "
                            "Historical commit cannot be independently re-established."
                        ),
                    )
                )
                report.reasons.append(
                    f"Workspace is not a Git repository. Historical commit '{c_sha}' cannot be verified."
                )
                if report.overall_status != ProofVerificationStatus.INVALID:
                    report.overall_status = ProofVerificationStatus.INCOMPLETE

        # -----------------------------------------------------------------
        # Check 10: Derived Markdown Report Inspection (Read-Only)
        # -----------------------------------------------------------------
        md_path = self.store.proof_markdown_path(actual_task_id)
        if not md_path.exists():
            report.checks.append(
                VerificationCheckResult(
                    check_name="derived_markdown_companion",
                    status=ProofVerificationStatus.VALID,
                    message="Derived Markdown companion not present (non-fatal; authoritative JSON is intact).",
                    details={"markdown_path": str(md_path), "present": False},
                )
            )
        else:
            try:
                md_content = md_path.read_text(encoding="utf-8")
                if not md_content.strip():
                    report.checks.append(
                        VerificationCheckResult(
                            check_name="derived_markdown_companion",
                            status=ProofVerificationStatus.VALID,
                            message="Derived Markdown companion exists but is empty (non-fatal for authoritative JSON).",
                            details={"markdown_path": str(md_path), "empty": True},
                        )
                    )
                else:
                    report.checks.append(
                        VerificationCheckResult(
                            check_name="derived_markdown_companion",
                            status=ProofVerificationStatus.VALID,
                            message="Derived Markdown companion exists and is readable.",
                            details={"markdown_path": str(md_path), "bytes": len(md_content)},
                        )
                    )
            except Exception as md_err:
                report.checks.append(
                    VerificationCheckResult(
                        check_name="derived_markdown_companion",
                        status=ProofVerificationStatus.VALID,
                        message=f"Derived Markdown companion unreadable: {md_err} (non-fatal for authoritative JSON).",
                        details={"markdown_path": str(md_path), "error": str(md_err)},
                    )
                )

        # -----------------------------------------------------------------
        # Check 11: Offline Execution Authenticity Limitation Check
        # -----------------------------------------------------------------
        # Explicitly records that offline verification proves structural & evidential
        # consistency, not cryptographic/hardware authenticity of execution history.
        report.checks.append(
            VerificationCheckResult(
                check_name="execution_authenticity_boundary",
                status=ProofVerificationStatus.VALID,
                message="Structural evidence cross-validation complete. (Note: proofs are unsigned JSON; offline verification asserts internal consistency, not cryptographic hardware attestation).",
            )
        )

        return report


def verify_proof(
    workspace: Union[str, Path],
    target: Union[str, Path],
) -> ProofVerificationReport:
    """Convenience functional API to verify a proof artifact by task ID or path."""
    verifier = DeterministicProofVerifier(workspace=workspace)
    return verifier.verify(target)


__all__ = [
    "DeterministicProofVerifier",
    "MANDATORY_VERIFICATION_GATES",
    "ProofVerificationReport",
    "ProofVerificationStatus",
    "VerificationCheckResult",
    "is_safe_workspace_relative_path",
    "verify_proof",
]
