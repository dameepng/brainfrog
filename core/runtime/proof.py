"""Proof Artifact Domain Model, Verification Evidence Collection, and Atomic Persistence.

Implements BrainFrog Phase 4: Proof Artifact.
Product thesis: "Don't just trust the agent. Verify it."

Generates machine-readable (JSON) and human-readable (Markdown) proofs derived
strictly from authoritative runtime facts:
- Transaction status and journal operations
- Subprocess exit codes, bounded outputs, and execution duration
- VerificationGate invariant evaluations
- Optional Git provenance (non-Git safe)
- Secret redaction and strict workspace path privacy

LLM output has ZERO authority to set the final verdict.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Literal, Optional, Sequence, Set, Tuple, Union

logger = logging.getLogger(__name__)

# =============================================================================
# 1. Constants & Status Enums
# =============================================================================

VALID_VERDICT_STATUSES: FrozenSet[str] = frozenset({
    "VERIFIED",
    "FAILED",
    "CANCELLED",
    "RECOVERY_REQUIRED",
})

SENSITIVE_FILE_PATTERNS: Sequence[str] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*id_rsa*",
    "*id_ecdsa*",
    "*id_ed25519*",
    "*secret*",
    "*token*",
    "*credential*",
)

SECRET_PATTERNS: Sequence[Tuple[re.Pattern, str]] = (
    # GitHub Tokens
    (re.compile(r"ghp_[a-zA-Z0-9]{20,}", re.IGNORECASE), "[REDACTED_SECRET]"),
    (re.compile(r"github_pat_[a-zA-Z0-9_]{20,}", re.IGNORECASE), "[REDACTED_SECRET]"),
    # OpenAI / Anthropic / generic sk-
    (re.compile(r"sk-[a-zA-Z0-9_\-]{20,}", re.IGNORECASE), "[REDACTED_SECRET]"),
    # Google API Keys
    (re.compile(r"AIza[0-9A-Za-z-_]{30,}", re.IGNORECASE), "[REDACTED_SECRET]"),
    # Bearer tokens
    (re.compile(r"(Bearer\s+)[a-zA-Z0-9_\-\.]{15,}", re.IGNORECASE), r"\1[REDACTED_SECRET]"),
    # Key-value secret assignments
    (re.compile(r"((?:password|passwd|pwd)\s*[:=]\s*)[^\s,;&\"']+", re.IGNORECASE), r"\1[REDACTED_SECRET]"),
    (re.compile(r"((?:api[_-]?key)\s*[:=]\s*)[^\s,;&\"']+", re.IGNORECASE), r"\1[REDACTED_SECRET]"),
    (re.compile(r"((?:secret[_-]?key|secret)\s*[:=]\s*)[^\s,;&\"']+", re.IGNORECASE), r"\1[REDACTED_SECRET]"),
    (re.compile(r"((?:access[_-]?token|auth[_-]?token|token)\s*[:=]\s*)[^\s,;&\"']+", re.IGNORECASE), r"\1[REDACTED_SECRET]"),
    # Private Key blocks
    (re.compile(r"-----BEGIN [A-Z ]+ PRIVATE KEY-----.*?-----END [A-Z ]+ PRIVATE KEY-----", re.DOTALL), "[REDACTED_SECRET]"),
)


class ProofAlreadyExistsError(FileExistsError):
    """Raised when an attempt is made to overwrite an existing proof artifact without authorization."""
    pass


class ProofVerificationError(ValueError):
    """Raised when proof invariant validation fails."""
    pass


# =============================================================================
# 2. Secret Redaction & Path Normalization Helpers
# =============================================================================

def redact_secrets(text: Optional[str]) -> str:
    """Scrub sensitive credentials, tokens, and keys from text."""
    if not text:
        return ""
    result = text
    for pattern, replacement in SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def is_sensitive_path(path_str: str) -> bool:
    """Check if a filename or path matches known sensitive file patterns."""
    clean = path_str.replace("\\", "/").strip().lstrip("/")
    name = Path(clean).name.lower()
    for pat in SENSITIVE_FILE_PATTERNS:
        if fnmatch.fnmatch(name, pat.lower()):
            return True
        if fnmatch.fnmatch(clean.lower(), pat.lower()):
            return True
    return False


def normalize_workspace_relative_path(path: Union[str, Path], workspace: Path) -> str:
    """Ensure path is within workspace and return posix relative representation.

    Fail-closed: Raises ValueError if the path escapes the workspace or cannot
    be cleanly represented as a workspace-relative path.
    """
    ws_res = workspace.resolve()
    target_raw = Path(path)
    if target_raw.is_absolute():
        target_res = target_raw.resolve()
    else:
        target_res = (ws_res / target_raw).resolve()

    try:
        rel = target_res.relative_to(ws_res)
    except ValueError as exc:
        raise ValueError(
            f"Path '{path}' escapes workspace boundary '{ws_res}'. Absolute paths are rejected in proof artifacts."
        ) from exc

    rel_posix = rel.as_posix()
    if rel_posix.startswith("..") or "/../" in rel_posix or ":" in rel_posix:
        raise ValueError(f"Path '{path}' contains illegal path traversal components.")
    return rel_posix


def capture_bounded_output(
    raw_output: str,
    max_bytes: int = 5120,
    head_chars: int = 1000,
    tail_chars: int = 4000,
) -> Tuple[str, bool]:
    """Bound subprocess output stream to max size with head/tail retention."""
    if not raw_output:
        return "", False

    if len(raw_output) <= max_bytes:
        return redact_secrets(raw_output), False

    head = raw_output[:head_chars]
    tail = raw_output[-tail_chars:] if tail_chars > 0 else ""
    truncated_msg = f"\n... [TRUNCATED {len(raw_output) - (head_chars + tail_chars)} characters] ...\n"
    combined = head + truncated_msg + tail
    return redact_secrets(combined), True


# =============================================================================
# 3. Typed Proof Domain Models
# =============================================================================

@dataclass(frozen=True)
class ProofVerdict:
    """Authoritative verdict computed solely from deterministic runtime facts."""

    status: str
    reason: str

    def __post_init__(self) -> None:
        if self.status not in VALID_VERDICT_STATUSES:
            raise ValueError(
                f"Invalid verdict status: '{self.status}'. Must be one of {sorted(VALID_VERDICT_STATUSES)}."
            )
        if not self.reason:
            object.__setattr__(self, "reason", "Verdict recorded without explicit detail.")


@dataclass(frozen=True)
class TaskEvidence:
    """Structured identity and timing record of the executed task."""

    task_id: str
    session_id: str
    description: str
    created_at: str
    completed_at: str
    duration_ms: int

    def __post_init__(self) -> None:
        if not self.task_id:
            raise ValueError("TaskEvidence task_id must not be empty.")
        if not self.description:
            raise ValueError("TaskEvidence description must not be empty.")


@dataclass(frozen=True)
class GitProvenance:
    """Optional Git repository provenance at the time of execution."""

    is_git: bool
    repo_root: str
    branch: Optional[str] = None
    initial_head_sha: Optional[str] = None
    final_head_sha: Optional[str] = None
    was_dirty_before: bool = False
    commit_sha: Optional[str] = None


@dataclass(frozen=True)
class ProcessEvidence:
    """Subprocess execution facts capturing test or domain command attestation."""

    task_id: str
    transaction_id: Optional[str] = None
    command: str = ""
    started_at: str = ""
    completed_at: str = ""
    duration_ms: int = 0
    returncode: Optional[int] = None
    status: str = "success"
    timed_out: bool = False
    cancelled: bool = False
    truncated: bool = False


@dataclass(frozen=True)
class ChangeRecord:
    """Discrete file modification recorded from Transaction.operations."""

    path: str
    operation: str
    before_sha256: Optional[str] = None
    after_sha256: Optional[str] = None
    byte_count_delta: int = 0

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError("ChangeRecord path must not be empty.")
        if ":" in self.path or self.path.startswith("/"):
            raise ValueError(f"ChangeRecord path '{self.path}' must be workspace-relative.")


@dataclass(frozen=True)
class ChangesEvidence:
    """Collection of file changes derived strictly from Transaction journal."""

    transaction_id: Optional[str]
    status: str
    files: List[ChangeRecord] = field(default_factory=list)


@dataclass(frozen=True)
class VerificationEvidence:
    """Structured verification facts including gate checks and bounded output."""

    test_command: str
    exit_code: Optional[int]
    stdout_summary: str
    stderr_summary: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    gate_checks: Dict[str, bool] = field(default_factory=dict)
    process_evidence: Optional[ProcessEvidence] = None


@dataclass(frozen=True)
class ReproducibilityEvidence:
    """External replication hints for independent verifiers."""

    command: str
    expected_exit_code: int = 0
    expected_file_hashes: Dict[str, str] = field(default_factory=dict)


@dataclass
class ProofArtifact:
    """The canonical, durable proof artifact proving autonomous execution."""

    version: str = "1.0.0"
    task: TaskEvidence = field(default_factory=lambda: TaskEvidence("", "", "", "", "", 0))
    verdict: ProofVerdict = field(default_factory=lambda: ProofVerdict("FAILED", "Uninitialized"))
    provenance: Optional[GitProvenance] = None
    verification: VerificationEvidence = field(default_factory=lambda: VerificationEvidence("", None, "", ""))
    changes: ChangesEvidence = field(default_factory=lambda: ChangesEvidence(None, "UNKNOWN"))
    reproducibility: ReproducibilityEvidence = field(default_factory=lambda: ReproducibilityEvidence(""))

    def to_dict(self) -> Dict[str, Any]:
        """Convert artifact to clean JSON-serializable dictionary."""
        return {
            "$schema": "https://brainfrog.dev/schemas/proof.v1.json",
            "version": self.version,
            "task": asdict(self.task),
            "verdict": asdict(self.verdict),
            "provenance": asdict(self.provenance) if self.provenance is not None else None,
            "verification": asdict(self.verification),
            "changes": asdict(self.changes),
            "reproducibility": asdict(self.reproducibility),
        }

    def to_json(self, indent: int = 2) -> str:
        """Render JSON formatted artifact string."""
        return json.dumps(self.to_dict(), indent=indent, sort_keys=False)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ProofArtifact:
        """Construct typed ProofArtifact from serialized dictionary."""
        task_data = data["task"]
        task = TaskEvidence(
            task_id=task_data["task_id"],
            session_id=task_data.get("session_id", ""),
            description=task_data["description"],
            created_at=task_data.get("created_at", ""),
            completed_at=task_data.get("completed_at", ""),
            duration_ms=task_data.get("duration_ms", 0),
        )

        verdict_data = data["verdict"]
        verdict = ProofVerdict(
            status=verdict_data["status"],
            reason=verdict_data.get("reason", ""),
        )

        prov_data = data.get("provenance")
        provenance = (
            GitProvenance(
                is_git=prov_data.get("is_git", False),
                repo_root=prov_data.get("repo_root", ""),
                branch=prov_data.get("branch"),
                initial_head_sha=prov_data.get("initial_head_sha"),
                final_head_sha=prov_data.get("final_head_sha"),
                was_dirty_before=prov_data.get("was_dirty_before", False),
                commit_sha=prov_data.get("commit_sha"),
            )
            if prov_data
            else None
        )

        v_data = data["verification"]
        proc_data = v_data.get("process_evidence")
        proc_evidence = (
            ProcessEvidence(
                task_id=proc_data.get("task_id", task.task_id),
                transaction_id=proc_data.get("transaction_id"),
                command=proc_data.get("command", ""),
                started_at=proc_data.get("started_at", ""),
                completed_at=proc_data.get("completed_at", ""),
                duration_ms=proc_data.get("duration_ms", 0),
                returncode=proc_data.get("returncode"),
                status=proc_data.get("status", "success"),
                timed_out=proc_data.get("timed_out", False),
                cancelled=proc_data.get("cancelled", False),
                truncated=proc_data.get("truncated", False),
            )
            if proc_data
            else None
        )

        verification = VerificationEvidence(
            test_command=v_data.get("test_command", ""),
            exit_code=v_data.get("exit_code"),
            stdout_summary=v_data.get("stdout_summary", ""),
            stderr_summary=v_data.get("stderr_summary", ""),
            stdout_truncated=v_data.get("stdout_truncated", False),
            stderr_truncated=v_data.get("stderr_truncated", False),
            gate_checks=v_data.get("gate_checks", {}),
            process_evidence=proc_evidence,
        )

        c_data = data["changes"]
        files = [
            ChangeRecord(
                path=f["path"],
                operation=f["operation"],
                before_sha256=f.get("before_sha256"),
                after_sha256=f.get("after_sha256"),
                byte_count_delta=f.get("byte_count_delta", 0),
            )
            for f in c_data.get("files", [])
        ]
        changes = ChangesEvidence(
            transaction_id=c_data.get("transaction_id"),
            status=c_data.get("status", "UNKNOWN"),
            files=files,
        )

        r_data = data.get("reproducibility", {})
        reproducibility = ReproducibilityEvidence(
            command=r_data.get("command", ""),
            expected_exit_code=r_data.get("expected_exit_code", 0),
            expected_file_hashes=r_data.get("expected_file_hashes", {}),
        )

        return cls(
            version=data.get("version", "1.0.0"),
            task=task,
            verdict=verdict,
            provenance=provenance,
            verification=verification,
            changes=changes,
            reproducibility=reproducibility,
        )


# =============================================================================
# 4. Authoritative Verdict Computation
# =============================================================================

def compute_proof_verdict(
    tx_status: Union[str, Any],
    tests_passed: bool,
    gate_passed: bool,
    process_status: Union[str, Any] = "success",
    rollback_error: Optional[str] = None,
    is_recovery_required: bool = False,
    gate_details: Optional[Dict[str, bool]] = None,
) -> ProofVerdict:
    """Pure deterministic runtime function computing the final task verdict.

    Truth Table Invariants:
    1. RECOVERY_REQUIRED: If transaction failed, rollback crashed, or recovery is required.
    2. CANCELLED: If execution was cancelled by user/runtime.
    3. FAILED: If process timed out, tests failed, gate checks failed, or mutations rolled back.
    4. VERIFIED: Requires COMMITTED + tests passed + gate passed + normal process exit + all gate invariants true.
    """
    tx_val = getattr(tx_status, "value", None)
    tx_str = str(tx_val if tx_val is not None else (tx_status or "")).lower()
    proc_val = getattr(process_status, "value", None)
    proc_str = str(proc_val if proc_val is not None else (process_status or "")).lower()

    # Invariant 1: Recovery Required
    if is_recovery_required or rollback_error or tx_str in ("failed",):
        reason = rollback_error or "Transaction execution failed; filesystem left in non-clean state."
        return ProofVerdict(status="RECOVERY_REQUIRED", reason=reason)

    # Invariant 2: Cancellation
    if proc_str == "cancelled" or tx_str == "cancelled":
        return ProofVerdict(status="CANCELLED", reason="Execution was cancelled by user or runtime.")

    # Invariant 3: Subprocess Timeout
    if proc_str == "timeout":
        return ProofVerdict(status="FAILED", reason="Domain test suite execution timed out.")

    # Invariant 3.5: Failed Gate Details
    if gate_details and not all(gate_details.values()):
        failed_keys = [k for k, v in gate_details.items() if not v]
        return ProofVerdict(
            status="FAILED",
            reason=f"Verification gate check(s) failed: {', '.join(failed_keys)}.",
        )

    # Invariant 4: Normal Success (VERIFIED)
    if tx_str == "committed" and tests_passed and gate_passed and proc_str in ("success", "normal"):
        return ProofVerdict(
            status="VERIFIED",
            reason="All verification gate invariants satisfied and transaction committed cleanly.",
        )

    # Invariant 5: Safe Rollback
    if tx_str == "rolled_back":
        return ProofVerdict(
            status="FAILED",
            reason="Task did not satisfy verification criteria; filesystem mutations safely rolled back.",
        )

    # Invariant 6: Default Fail-Closed
    return ProofVerdict(
        status="FAILED",
        reason="Execution did not satisfy all authoritative verification requirements.",
    )


# =============================================================================
# 5. Git Provenance Capture
# =============================================================================

def sample_git_state(workspace: Path) -> Tuple[Optional[str], bool]:
    """Sample Git HEAD commit SHA and working-tree dirty status at a specific point in time."""
    ws_res = workspace.resolve()
    git_dir = ws_res / ".git"
    if not git_dir.exists():
        return None, False
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(ws_res),
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        if res.returncode != 0 or res.stdout.strip() != "true":
            return None, False

        sha_res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(ws_res),
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        head_sha = sha_res.stdout.strip() if sha_res.returncode == 0 else None

        stat_res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(ws_res),
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        was_dirty = bool(stat_res.stdout.strip()) if stat_res.returncode == 0 else False
        return head_sha, was_dirty
    except Exception as exc:
        logger.debug(f"Git state sampling skipped or failed gracefully: {exc}")
        return None, False


def capture_git_provenance(
    workspace: Path,
    initial_head_sha: Optional[str] = None,
    was_dirty_before: Optional[bool] = None,
    commit_sha: Optional[str] = None,
) -> Optional[GitProvenance]:
    """Capture Git provenance without breaking non-Git workspaces."""
    ws_res = workspace.resolve()
    git_dir = ws_res / ".git"
    if not git_dir.exists():
        return None

    try:
        # Check inside work tree
        res = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(ws_res),
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        if res.returncode != 0 or res.stdout.strip() != "true":
            return None

        # Root
        root_res = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(ws_res),
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        repo_root = root_res.stdout.strip() or ws_res.name

        # Branch
        br_res = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(ws_res),
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        branch = br_res.stdout.strip() if br_res.returncode == 0 else None

        # Head SHA (final sample)
        sha_res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(ws_res),
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        final_head_sha = sha_res.stdout.strip() if sha_res.returncode == 0 else None

        # Status porcelain
        if was_dirty_before is None:
            stat_res = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(ws_res),
                capture_output=True,
                text=True,
                timeout=5.0,
            )
            dirty = bool(stat_res.stdout.strip()) if stat_res.returncode == 0 else False
        else:
            dirty = was_dirty_before

        init_sha = initial_head_sha if initial_head_sha is not None else final_head_sha

        return GitProvenance(
            is_git=True,
            repo_root=Path(repo_root).name,
            branch=branch,
            initial_head_sha=init_sha,
            final_head_sha=final_head_sha,
            was_dirty_before=dirty,
            commit_sha=commit_sha,
        )
    except Exception as exc:
        logger.debug(f"Git provenance capture skipped or failed gracefully: {exc}")
        return None


# =============================================================================
# 6. Change Records Builder
# =============================================================================

def build_change_records(
    operations: Sequence[Any],
    workspace: Path,
) -> List[ChangeRecord]:
    """Construct ChangeRecord list strictly from Transaction operations."""
    records: List[ChangeRecord] = []
    ws_res = workspace.resolve()

    for op in operations:
        target_raw = getattr(op, "target", "")
        if not target_raw:
            continue
        rel_path = normalize_workspace_relative_path(target_raw, ws_res)
        op_type = getattr(op, "type", "modify")
        op_val = getattr(op_type, "value", None)
        op_type_raw = str(op_val if op_val is not None else op_type)
        op_lower = op_type_raw.lower()
        if "create" in op_lower:
            op_type_str = "CREATE"
        elif "modify" in op_lower:
            op_type_str = "MODIFY"
        elif "delete" in op_lower:
            op_type_str = "DELETE"
        elif "rename" in op_lower:
            op_type_str = "RENAME"
        else:
            op_type_str = op_type_raw.upper()

        before_state = getattr(op, "before_state", None) or {}
        after_state = getattr(op, "after_state", None) or {}

        before_hash = before_state.get("content_hash")
        before_bytes = before_state.get("size", 0)

        after_hash = after_state.get("content_hash")
        after_bytes = after_state.get("size", 0)

        # Compute hash if missing and content present
        if not before_hash and before_state.get("content"):
            before_hash = hashlib.sha256(before_state["content"].encode("utf-8")).hexdigest()
        if not after_hash and after_state.get("content"):
            after_hash = hashlib.sha256(after_state["content"].encode("utf-8")).hexdigest()

        # If committed file exists on disk, derive final state if after_state lacked hash
        file_disk = (ws_res / rel_path)
        if not after_hash and file_disk.exists() and file_disk.is_file():
            try:
                raw = file_disk.read_bytes()
                after_hash = hashlib.sha256(raw).hexdigest()
                after_bytes = len(raw)
            except Exception:
                pass

        byte_delta = int(after_bytes - before_bytes)

        records.append(
            ChangeRecord(
                path=rel_path,
                operation=op_type_str,
                before_sha256=before_hash,
                after_sha256=after_hash,
                byte_count_delta=byte_delta,
            )
        )

    return records


# =============================================================================
# 7. Atomic Proof Persistence (FileProofStore)
# =============================================================================

class FileProofStore:
    """Atomic, fail-closed filesystem storage for proof artifacts.

    Layout:
    <workspace>/.brainfrog/proofs/<task_id>.json
    <workspace>/.brainfrog/proofs/<task_id>.md

    Atomic Strategy:
    Write temporary file -> flush -> fsync -> close -> os.replace()
    """

    def __init__(self, workspace: Union[str, Path]) -> None:
        self.workspace = Path(workspace).resolve()
        self.proofs_dir = self.workspace / ".brainfrog" / "proofs"

    def proof_json_path(self, task_id: str) -> Path:
        """Resolve final JSON artifact path for task_id."""
        clean_id = Path(task_id).name
        return self.proofs_dir / f"{clean_id}.json"

    def proof_markdown_path(self, task_id: str) -> Path:
        """Resolve final Markdown report path for task_id."""
        clean_id = Path(task_id).name
        return self.proofs_dir / f"{clean_id}.md"

    def exists(self, task_id: str) -> bool:
        """Check if a proof artifact already exists on disk for task_id."""
        return self.proof_json_path(task_id).exists()

    def list(self) -> List[str]:
        """List all task IDs with authoritative proof JSON files."""
        if not self.proofs_dir.exists():
            return []
        return sorted([p.stem for p in self.proofs_dir.glob("*.json") if not p.name.startswith(".")])

    def list_proofs(self) -> List[ProofArtifact]:
        """List and load all ProofArtifact instances in storage."""
        return [self.load(tid) for tid in self.list()]

    def save(
        self,
        proof: ProofArtifact,
        markdown_content: Optional[str] = None,
        *,
        allow_overwrite: bool = False,
    ) -> Tuple[Path, Optional[Path]]:
        """Atomically persist ProofArtifact JSON and Markdown report.

        Rejects overwriting existing artifacts unless allow_overwrite=True.
        """
        task_id = proof.task.task_id
        if not task_id:
            raise ValueError("Cannot persist ProofArtifact with empty task_id.")

        self.proofs_dir.mkdir(parents=True, exist_ok=True)
        final_json = self.proof_json_path(task_id)
        final_md = self.proof_markdown_path(task_id) if markdown_content is not None else None

        # Duplicate protection: fail-closed if artifact already exists
        if final_json.exists() and not allow_overwrite:
            raise ProofAlreadyExistsError(
                f"Proof artifact for task '{task_id}' already exists at '{final_json}'. "
                "Silent overwriting of historical verification evidence is prohibited."
            )

        # 1. Staging phase: write and fsync both temporary files
        nonce = secrets.token_hex(6)
        tmp_json = self.proofs_dir / f".tmp_proof_{task_id}_{nonce}.json"
        tmp_md = self.proofs_dir / f".tmp_proof_{task_id}_{nonce}.md" if (markdown_content is not None and final_md is not None) else None
        json_payload = proof.to_json(indent=2)

        try:
            with open(tmp_json, "w", encoding="utf-8") as f:
                f.write(json_payload)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass

            if tmp_md is not None and markdown_content is not None:
                with open(tmp_md, "w", encoding="utf-8") as f:
                    f.write(markdown_content)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except Exception:
                        pass
        except Exception as exc:
            if tmp_json.exists():
                try:
                    tmp_json.unlink()
                except Exception:
                    pass
            if tmp_md is not None and tmp_md.exists():
                try:
                    tmp_md.unlink()
                except Exception:
                    pass
            raise IOError(f"Staging proof artifacts for '{task_id}' failed: {exc}") from exc

        # 2. Atomic commit phase: replace Markdown first, then JSON
        # Placing Markdown first guarantees that when exists() (which checks JSON) becomes true, Markdown is already present
        try:
            if tmp_md is not None and final_md is not None:
                os.replace(tmp_md, final_md)
            os.replace(tmp_json, final_json)
        except Exception as exc:
            if tmp_json.exists():
                try:
                    tmp_json.unlink()
                except Exception:
                    pass
            if tmp_md is not None and tmp_md.exists():
                try:
                    tmp_md.unlink()
                except Exception:
                    pass
            raise IOError(f"Atomic commitment of proof artifacts for '{task_id}' failed: {exc}") from exc

        return final_json, final_md

    def load(self, task_id: str) -> ProofArtifact:
        """Load and deserialize ProofArtifact from durable storage."""
        target = self.proof_json_path(task_id)
        if not target.exists():
            raise FileNotFoundError(f"Proof artifact for task '{task_id}' not found at '{target}'.")
        raw = target.read_text(encoding="utf-8")
        data = json.loads(raw)
        return ProofArtifact.from_dict(data)

    def load_markdown(self, task_id: str) -> str:
        """Load companion Markdown report for task_id, with deterministic self-healing."""
        target = self.proof_markdown_path(task_id)
        if target.exists():
            try:
                content = target.read_text(encoding="utf-8")
                if content and content.strip():
                    return content
                logger.warning(
                    f"Proof markdown companion for '{task_id}' at '{target}' is empty. "
                    "Attempting self-healing from authoritative JSON."
                )
            except (UnicodeDecodeError, OSError) as read_exc:
                logger.warning(
                    f"Proof markdown companion for '{task_id}' at '{target}' is unreadable ({read_exc}). "
                    "Attempting self-healing from authoritative JSON."
                )

        # Deterministic self-healing: if authoritative proof JSON exists, reconstruct Markdown report
        if self.proof_json_path(task_id).exists():
            # Load authoritative proof JSON.
            # Fail closed if JSON is corrupt or invalid without modifying or overwriting any file.
            proof = self.load(task_id)
            tmp_md: Optional[Path] = None
            try:
                from core.runtime.proof_markdown import render_proof_markdown
                rendered = render_proof_markdown(proof)
                nonce = secrets.token_hex(6)
                tmp_md = self.proofs_dir / f".tmp_heal_{task_id}_{nonce}.md"
                with open(tmp_md, "w", encoding="utf-8") as f:
                    f.write(rendered)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except Exception:
                        pass
                tmp_md.replace(target)
                return rendered
            except Exception as heal_exc:
                if tmp_md is not None and tmp_md.exists():
                    try:
                        tmp_md.unlink()
                    except Exception:
                        pass
                logger.warning(f"Self-healing companion proof markdown for '{task_id}' failed: {heal_exc}")
                raise

        raise FileNotFoundError(f"Proof markdown report for task '{task_id}' not found at '{target}'.")
