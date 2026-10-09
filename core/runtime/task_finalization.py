"""Durable Task Finalization and Commit-Intent Protocol.

Guarantees:
- Persists task commit-intent before crossing the irreversible commit boundary.
- Records expected workspace, Git HEAD before commit, and resulting Git commit SHA.
- Detects commit-without-proof state on restart or re-execution.
- Blocks duplicate execution of tasks with unresolved commits (fails-closed with TaskRecoveryRequiredError).
- Provides deterministic recovery and status reporting without silent duplicate mutations.

Product Thesis:
    "Don't just trust the agent. Verify it."
"""
from __future__ import annotations

import errno
import json
import logging
import os
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from core.runtime.workspace_lock import is_pid_running

logger = logging.getLogger(__name__)


class CommitIntentStatus(str, Enum):
    """Lifecycle stages for task commit intent and finalization."""

    RECORDED = "RECORDED"
    COMMITTING = "COMMITTING"
    COMMITTED_PENDING_PROOF = "COMMITTED_PENDING_PROOF"
    FINALIZED = "FINALIZED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class IntentCorruptError(RuntimeError):
    """Raised when an intent file exists on disk but is malformed, unreadable, or invalid JSON."""

    def __init__(self, task_id: str, message: str, path: Optional[Path] = None) -> None:
        super().__init__(f"Finalization intent for '{task_id}' is corrupt: {message}")
        self.task_id = task_id
        self.message = message
        self.path = path


class TaskRecoveryRequiredError(RuntimeError):
    """Raised when an attempt is made to execute a task_id that has an unresolved commit intent."""

    def __init__(self, message: str, *, task_id: str, commit_sha: Optional[str] = None) -> None:
        super().__init__(message)
        self.task_id = task_id
        self.commit_sha = commit_sha


def safe_atomic_replace(
    src: Path,
    dst: Path,
    *,
    max_retries: int = 5,
    initial_delay: float = 0.005,
    backoff_factor: float = 2.0,
    replace_fn: Optional[Any] = None,
) -> None:
    """Atomically replace dst with src with bounded retry for transient Windows sharing collisions.

    On Windows, os.replace / Path.replace can transiently raise PermissionError (WinError 5: Access is denied)
    or OSError (WinError 32: Sharing violation) if another handle or antivirus scanner is momentarily probing
    the destination file.

    This function performs bounded backoff retries only on confirmed transient errors.
    On non-transient errors or when retries are exhausted, it raises the original error.
    """
    fn = replace_fn if replace_fn is not None else os.replace
    delay = initial_delay
    last_err: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        try:
            fn(str(src), str(dst))
            return
        except (PermissionError, OSError) as exc:
            last_err = exc
            winerror = getattr(exc, "winerror", None)
            err_no = getattr(exc, "errno", None)
            is_transient = (
                (winerror is not None and winerror in (5, 32))
                or (err_no is not None and err_no in (errno.EACCES, errno.EBUSY, getattr(errno, "ETXTBSY", 26)))
            )
            if not is_transient or attempt >= max_retries:
                raise
            time.sleep(delay)
            delay *= backoff_factor

    if last_err is not None:
        raise last_err


@dataclass
class CommitIntentRecord:
    """Durable record of a task's intent to commit and its finalization status."""

    task_id: str
    workspace: str
    status: CommitIntentStatus
    created_at: float
    git_head_before: Optional[str] = None
    commit_sha: Optional[str] = None
    transaction_id: Optional[str] = None
    pid: int = 0
    nonce: str = ""
    is_git: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "workspace": self.workspace,
            "status": self.status.value,
            "created_at": self.created_at,
            "git_head_before": self.git_head_before,
            "commit_sha": self.commit_sha,
            "transaction_id": self.transaction_id,
            "pid": self.pid,
            "nonce": self.nonce,
            "is_git": self.is_git,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CommitIntentRecord:
        raw_status = data.get("status", CommitIntentStatus.RECORDED.value)
        try:
            status = CommitIntentStatus(raw_status)
        except ValueError:
            status = CommitIntentStatus.RECOVERY_REQUIRED
        return cls(
            task_id=str(data["task_id"]),
            workspace=str(data.get("workspace", "")),
            status=status,
            created_at=float(data.get("created_at", 0.0)),
            git_head_before=data.get("git_head_before"),
            commit_sha=data.get("commit_sha"),
            transaction_id=data.get("transaction_id"),
            pid=int(data.get("pid", 0)),
            nonce=str(data.get("nonce", "")),
            is_git=bool(data.get("is_git", True)),
            metadata=dict(data.get("metadata", {})),
        )


class TaskFinalizationCoordinator:
    """Manages durable commit-intent records and crash-safe finalization protocol."""

    def __init__(self, workspace: Union[str, Path]) -> None:
        self.workspace = Path(workspace).resolve()
        self.finalization_dir = self.workspace / ".brainfrog" / "finalization"

    def _intent_path(self, task_id: str) -> Path:
        return self.finalization_dir / f"{task_id}.json"

    def _get_git_head(self) -> Optional[str]:
        """Resolve current Git HEAD SHA if workspace is a git repository."""
        if not (self.workspace / ".git").exists():
            return None
        try:
            res = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode == 0:
                sha = res.stdout.strip()
                return sha if sha else None
        except Exception:
            pass
        return None

    def record_commit_intent(
        self,
        task_id: str,
        *,
        transaction_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> CommitIntentRecord:
        """Atomically record intent to commit before crossing irreversible commit boundary."""
        self.finalization_dir.mkdir(parents=True, exist_ok=True)
        head_before = self._get_git_head()
        is_git = head_before is not None or (self.workspace / ".git").exists()
        nonce = secrets.token_hex(16)

        record = CommitIntentRecord(
            task_id=task_id,
            workspace=str(self.workspace),
            status=CommitIntentStatus.COMMITTING,
            created_at=time.time(),
            git_head_before=head_before,
            transaction_id=transaction_id,
            pid=os.getpid(),
            nonce=nonce,
            is_git=is_git,
            metadata=metadata or {},
        )
        self._write_record_atomic(record)
        return record

    def record_commit_success(
        self,
        task_id: str,
        commit_sha: Optional[str] = None,
    ) -> Optional[CommitIntentRecord]:
        """Record successful Git commit completion, awaiting durable proof persistence."""
        path = self._intent_path(task_id)
        if not path.exists():
            head_now = commit_sha or self._get_git_head()
            record = CommitIntentRecord(
                task_id=task_id,
                workspace=str(self.workspace),
                status=CommitIntentStatus.COMMITTED_PENDING_PROOF,
                created_at=time.time(),
                commit_sha=head_now,
                pid=os.getpid(),
                nonce=secrets.token_hex(16),
            )
            self._write_record_atomic(record)
            return record

        try:
            record = CommitIntentRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
            head_now = commit_sha or self._get_git_head()
            record.status = CommitIntentStatus.COMMITTED_PENDING_PROOF
            record.commit_sha = head_now
            self._write_record_atomic(record)
            return record
        except Exception as exc:
            logger.warning(f"Failed to update commit intent record for '{task_id}': {exc}")
            return None

    def get_intent(self, task_id: str) -> Optional[CommitIntentRecord]:
        """Read existing intent record for task_id if present without side-effects.

        Distinguishes missing vs corrupt:
        - If file does not exist, returns None.
        - If file exists but is unreadable, malformed JSON, not an object, or has an invalid schema,
          raises IntentCorruptError (fails closed).
        """
        path = self._intent_path(task_id)
        if not path.exists():
            return None
        try:
            raw_text = path.read_text(encoding="utf-8")
        except Exception as exc:
            raise IntentCorruptError(task_id, f"Unreadable intent file: {exc}", path=path) from exc

        try:
            data = json.loads(raw_text)
        except Exception as exc:
            raise IntentCorruptError(task_id, f"Malformed JSON in intent file: {exc}", path=path) from exc

        if not isinstance(data, dict):
            raise IntentCorruptError(task_id, f"Invalid schema: expected JSON object, got {type(data).__name__}", path=path)

        try:
            return CommitIntentRecord.from_dict(data)
        except Exception as exc:
            raise IntentCorruptError(task_id, f"Invalid intent schema: {exc}", path=path) from exc

    def finalize_proof(self, task_id: str) -> None:
        """Mark intent as finalized or clean up once authoritative proof is durably saved."""
        path = self._intent_path(task_id)
        if not path.exists():
            return
        try:
            path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning(f"Failed to clean finalized commit intent for '{task_id}': {exc}")

    def reconcile_intent(self, task_id: str) -> Optional[CommitIntentRecord]:
        """Reconcile finalization intent against authoritative proof store.

        If a valid, verifiable authoritative proof JSON exists for this task:
        - Verifies task_id matches.
        - Verifies Git commit SHA matches if recorded.
        - Verifies the proof represents a valid completed verdict ('VERIFIED').
        - Safely finalizes/removes the stale intent record.
        - Fails closed if proof is missing, unreadable, corrupt, or mismatched.

        Returns:
            The unresolved CommitIntentRecord if recovery is still required,
            or None if safely reconciled/finalized or no intent exists.
        """
        path = self._intent_path(task_id)
        if not path.exists():
            return None

        # Fails closed if intent is corrupt (IntentCorruptError propagates)
        record = self.get_intent(task_id)
        if record is None:
            return None

        # 1. If intent is COMMITTING and process died, check if Git HEAD moved
        if record.status == CommitIntentStatus.COMMITTING:
            if record.pid > 0 and not is_pid_running(record.pid):
                current_head = self._get_git_head()
                if record.git_head_before and current_head != record.git_head_before:
                    # Git HEAD moved before crash: commit succeeded!
                    record.status = CommitIntentStatus.COMMITTED_PENDING_PROOF
                    record.commit_sha = current_head
                    self._write_record_atomic(record)
                else:
                    # Git HEAD did not move: clean up uncommitted intent
                    try:
                        path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return None
            else:
                return record

        # 2. Check if an authoritative proof already exists and validates
        if record.status in (CommitIntentStatus.COMMITTED_PENDING_PROOF, CommitIntentStatus.RECOVERY_REQUIRED):
            from core.runtime.proof import FileProofStore
            proof_store = FileProofStore(self.workspace)
            if proof_store.exists(task_id):
                try:
                    proof = proof_store.load(task_id)
                    # Verify task_id matches
                    if proof.task.task_id != task_id:
                        logger.warning(
                            f"Proof for task '{task_id}' has mismatched task_id '{proof.task.task_id}'. Failing closed."
                        )
                        return record

                    # Verify commit SHA matches if recorded
                    if record.commit_sha:
                        prov = proof.provenance
                        final_sha = prov.final_head_sha if prov else None
                        if final_sha and final_sha != record.commit_sha:
                            logger.warning(
                                f"Proof for task '{task_id}' has commit SHA '{final_sha}' "
                                f"differing from intent '{record.commit_sha}'. Failing closed."
                            )
                            return record

                    # Verify completed verdict
                    if proof.verdict.status != "VERIFIED":
                        logger.warning(
                            f"Proof for task '{task_id}' has non-verified verdict '{proof.verdict.status}'. Failing closed."
                        )
                        return record

                    # Authoritative proof is valid and verified! Safely reconcile intent.
                    logger.info(
                        f"Authoritative proof verified for task '{task_id}'. Reconciling orphan intent record."
                    )
                    self.finalize_proof(task_id)
                    return None
                except Exception as exc:
                    logger.warning(
                        f"Authoritative proof for task '{task_id}' failed validation during intent reconciliation: {exc}. Failing closed."
                    )
                    return record

            return record

        return None

    def get_unresolved_intent(self, task_id: str) -> Optional[CommitIntentRecord]:
        """Check if an unresolved or ambiguous commit intent exists for task_id.

        Fails closed:
        - If commit_sha is recorded without proof -> returns record (blocks duplicate execution).
        - If interrupted mid-commit, verifies if git HEAD actually moved.
        - If authoritative proof JSON already exists and validates -> reconciles intent.
        - Cleans up stale non-committed intents if process is dead and git HEAD did not change.
        """
        return self.reconcile_intent(task_id)

    def _write_record_atomic(self, record: CommitIntentRecord) -> None:
        self.finalization_dir.mkdir(parents=True, exist_ok=True)
        target = self._intent_path(record.task_id)
        nonce = secrets.token_hex(6)
        tmp = self.finalization_dir / f".tmp_intent_{record.task_id}_{nonce}.json"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(record.to_dict(), f, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
            safe_atomic_replace(tmp, target)
        except Exception as exc:
            if tmp.exists():
                try:
                    tmp.unlink()
                except Exception:
                    pass
            raise IOError(f"Failed to persist commit intent for '{record.task_id}': {exc}") from exc


__all__ = [
    "CommitIntentRecord",
    "CommitIntentStatus",
    "IntentCorruptError",
    "safe_atomic_replace",
    "TaskFinalizationCoordinator",
    "TaskRecoveryRequiredError",
]
