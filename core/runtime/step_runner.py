"""Structured Step Runner and Progress Tracker — Bounded Autonomous Build Loop.

Phase 4A Safety Invariants:
1. /build mode alone never authorizes execution.
2. Execution requires a valid, unconsumed approval contract bound to the correct
   workspace, session, session incarnation, plan ID, revision, content hash,
   operation digest, and approved targets.
3. Revalidate the plan and contract immediately before each mutating step.
4. A plan revision change invalidates the active execution authorization.
5. Every mutating step must stay within the approved targets and existing transaction boundary.
6. Never execute arbitrary plan text as shell commands. Use typed command/process runner.
7. Never automatically expand approved targets or permissions during execution.
8. Failed, timed-out, cancelled, or interrupted steps must stop the loop safely (stop-on-first-failure).
9. Persist enough structured progress to distinguish pending, running, passed, failed, timed_out,
   cancelled, and interrupted steps.
10. Recovery must not silently rerun a non-idempotent step whose completion status is ambiguous.
11. Do not mark a plan VERIFIED merely because implementation commands exited successfully.
    Require the existing acceptance-check mapping and successful verification evidence.
12. Do not bypass the existing approval service or introduce another orchestrator.
"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

from core.runtime.contract import ApprovedExecutionContract
from core.runtime.process import (
    _CURRENT_CANCEL_EVENT,
    _CURRENT_TASK_ID,
    run_hardened_subprocess,
    terminate_processes_for_task,
)
from core.runtime.workspace_lock import is_pid_running

try:
    import psutil  # type: ignore[import-untyped, import-not-found]
    _HAS_PSUTIL = True
except ImportError:
    psutil = None  # type: ignore[assignment]
    _HAS_PSUTIL = False

logger = logging.getLogger(__name__)


class StepExecutionStatus(str, Enum):
    """Lifecycle stages for an individual plan step execution."""

    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    SKIPPED = "skipped"


class AmbiguousStepExecutionError(RuntimeError):
    """Raised when recovery detects an uncompleted non-idempotent step.

    Fails closed to prevent blind duplicate execution of ambiguous mutations.
    """

    def __init__(self, step_id: str, message: str) -> None:
        super().__init__(f"Ambiguous non-idempotent step '{step_id}': {message}")
        self.step_id = step_id
        self.message = message


class ActiveProcessOwnershipError(AmbiguousStepExecutionError):
    """Raised when another live OS process actively holds execution ownership of a step or plan."""

    def __init__(self, step_id: str, owner_pid: Optional[int], message: str) -> None:
        super().__init__(step_id, f"Owned by active process PID {owner_pid}: {message}")
        self.owner_pid = owner_pid


class StepExecutionTimeoutError(TimeoutError):
    """Raised when a plan step exceeds its allocated execution timeout."""

    def __init__(self, step_id: str, timeout_seconds: float) -> None:
        super().__init__(f"Step '{step_id}' exceeded execution timeout of {timeout_seconds:.1f}s.")
        self.step_id = step_id
        self.timeout_seconds = timeout_seconds


class StaleExecutionAuthorizationError(PermissionError):
    """Raised when plan state or revision changes between steps, invalidating authorization."""

    pass


@dataclass
class StepRecoveryAudit:
    """Durable evidence audit of the recovery decision boundary for a plan step."""

    step_id: str
    status: StepExecutionStatus
    classification: str  # "completed", "retryable", "requiring_reconciliation"
    is_idempotent: bool
    owner_pid: Optional[int] = None
    is_owner_alive: bool = False
    pid_reused: bool = False
    transaction_id: Optional[str] = None
    transaction_status: Optional[str] = None
    transaction_verified: bool = False
    files_targeted: List[str] = field(default_factory=list)
    files_existing: List[str] = field(default_factory=list)
    has_workspace_mutations: bool = False
    mutation_timing: str = "none"  # "none", "before_mutation", "after_mutation"
    acceptance_verified: bool = False
    evidence_sufficient: bool = False
    decision_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "status": self.status.value if isinstance(self.status, StepExecutionStatus) else str(self.status),
            "classification": self.classification,
            "is_idempotent": self.is_idempotent,
            "owner_pid": self.owner_pid,
            "is_owner_alive": self.is_owner_alive,
            "pid_reused": self.pid_reused,
            "transaction_id": self.transaction_id,
            "transaction_status": self.transaction_status,
            "transaction_verified": self.transaction_verified,
            "files_targeted": list(self.files_targeted),
            "files_existing": list(self.files_existing),
            "has_workspace_mutations": self.has_workspace_mutations,
            "mutation_timing": self.mutation_timing,
            "acceptance_verified": self.acceptance_verified,
            "evidence_sufficient": self.evidence_sufficient,
            "decision_reason": self.decision_reason,
        }


@dataclass
class StepProgressRecord:
    """Structured progress and provenance record for a single step."""

    step_id: str
    description: str
    status: StepExecutionStatus = StepExecutionStatus.PENDING
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    error: Optional[str] = None
    is_idempotent: bool = False
    retry_count: int = 0
    max_retries: int = 0
    transaction_id: Optional[str] = None
    files: List[str] = field(default_factory=list)
    evidence: Optional[str] = None
    reconciled_by: Optional[str] = None
    reconciled_at: Optional[float] = None
    operator_attestation: Optional[str] = None
    reconciliation_type: Optional[str] = None
    transaction_verified: bool = False
    owner_pid: Optional[int] = None
    owner_create_time: Optional[float] = None
    lease_acquired_at: Optional[float] = None
    lease_timeout: float = 60.0
    lease_id: Optional[str] = None
    file_hashes: Dict[str, str] = field(default_factory=dict)
    requirement_digest: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "description": self.description,
            "status": self.status.value if isinstance(self.status, StepExecutionStatus) else str(self.status),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "is_idempotent": self.is_idempotent,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "transaction_id": self.transaction_id,
            "files": list(self.files),
            "evidence": self.evidence,
            "reconciled_by": self.reconciled_by,
            "reconciled_at": self.reconciled_at,
            "operator_attestation": self.operator_attestation,
            "reconciliation_type": self.reconciliation_type,
            "transaction_verified": self.transaction_verified,
            "owner_pid": self.owner_pid,
            "owner_create_time": self.owner_create_time,
            "lease_acquired_at": self.lease_acquired_at,
            "lease_timeout": self.lease_timeout,
            "lease_id": self.lease_id,
            "file_hashes": dict(self.file_hashes),
            "requirement_digest": self.requirement_digest,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> StepProgressRecord:
        if not isinstance(data, dict):
            raise ValueError("Step progress data must be a dictionary.")
        step_id = str(data.get("step_id", "")).strip()
        if not step_id:
            raise ValueError("Step progress record missing or empty 'step_id'.")
        raw_st = data.get("status")
        if raw_st is None:
            raise ValueError(f"Step '{step_id}' missing 'status' field.")
        try:
            st = StepExecutionStatus(raw_st)
        except ValueError:
            raise ValueError(f"Unknown or malformed step execution status for step '{step_id}': {raw_st!r}")

        owner_pid = None
        if data.get("owner_pid") is not None:
            try:
                owner_pid = int(data["owner_pid"])
            except (ValueError, TypeError):
                pass
        owner_create_time = None
        if data.get("owner_create_time") is not None:
            try:
                owner_create_time = float(data["owner_create_time"])
            except (ValueError, TypeError):
                pass
        lease_acquired_at = None
        if data.get("lease_acquired_at") is not None:
            try:
                lease_acquired_at = float(data["lease_acquired_at"])
            except (ValueError, TypeError):
                pass
        lease_timeout = 60.0
        if data.get("lease_timeout") is not None:
            try:
                lease_timeout = float(data["lease_timeout"])
            except (ValueError, TypeError):
                pass
        lease_id = str(data["lease_id"]) if data.get("lease_id") is not None else None
        file_hashes = dict(data.get("file_hashes", {})) if isinstance(data.get("file_hashes"), dict) else {}
        requirement_digest = data.get("requirement_digest")

        return cls(
            step_id=step_id,
            description=str(data.get("description", "")),
            status=st,
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            error=data.get("error"),
            is_idempotent=bool(data.get("is_idempotent", False)),
            retry_count=int(data.get("retry_count", 0)),
            max_retries=int(data.get("max_retries", 0)),
            transaction_id=data.get("transaction_id"),
            files=list(data.get("files", [])),
            evidence=data.get("evidence"),
            reconciled_by=data.get("reconciled_by"),
            reconciled_at=data.get("reconciled_at"),
            operator_attestation=data.get("operator_attestation"),
            reconciliation_type=data.get("reconciliation_type"),
            transaction_verified=bool(data.get("transaction_verified", False)),
            owner_pid=owner_pid,
            owner_create_time=owner_create_time,
            lease_acquired_at=lease_acquired_at,
            lease_timeout=lease_timeout,
            lease_id=lease_id,
            file_hashes=file_hashes,
            requirement_digest=requirement_digest,
        )



def _lock_file_handle(f: Any) -> None:
    """Acquire non-blocking exclusive lock on file handle."""
    if sys.platform == "win32":
        try:
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            pass
    else:
        try:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            pass


def _unlock_file_handle(f: Any) -> None:
    """Release exclusive lock on file handle."""
    if sys.platform == "win32":
        try:
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        try:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


def _is_file_in_active_use(path: Path) -> bool:
    """Test if a file is currently in active use or locked by another process."""
    if not path.exists():
        return False
    if sys.platform == "win32":
        try:
            with open(path, "r+b") as test_f:
                try:
                    import msvcrt
                    msvcrt.locking(test_f.fileno(), msvcrt.LK_NBLCK, 1)
                    msvcrt.locking(test_f.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    return True
            return False
        except PermissionError:
            return True
        except OSError:
            return True
    else:
        try:
            import fcntl
            with open(path, "r+b") as test_f:
                fcntl.flock(test_f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(test_f.fileno(), fcntl.LOCK_UN)
            return False
        except (BlockingIOError, PermissionError, OSError):
            return True


def _safe_atomic_write_json(file_path: Path, payload: Any) -> None:
    """Write json atomically using temporary file and atomic replace."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = file_path.with_suffix(f".tmp_{secrets.token_hex(6)}")
    encoded = json.dumps(payload, indent=2).encode("utf-8")

    with open(tmp_path, "wb") as f:
        _lock_file_handle(f)
        f.write(encoded)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
        _unlock_file_handle(f)

    # Bounded retry for Windows file locks
    for attempt in range(5):
        try:
            os.replace(str(tmp_path), str(file_path))
            return
        except (PermissionError, OSError) as exc:
            winerror = getattr(exc, "winerror", None)
            err_no = getattr(exc, "errno", None)
            if attempt == 4 or (winerror not in (5, 32) and err_no not in (errno.EACCES, errno.EBUSY)):
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass
                raise
            time.sleep(0.01 * (attempt + 1))


TRUSTED_IDEMPOTENT_ACTIONS = frozenset({
    "check",
    "inspect",
    "verify",
    "test",
    "read_check",
    "status_check",
    "read_only",
})

AUTHORIZED_READONLY_GIT_SUBCOMMANDS = frozenset({
    "status",
    "diff",
    "log",
    "rev-parse",
    "show",
})

FORBIDDEN_MUTATING_GIT_FLAGS = frozenset({
    "-o", "--output", "--apply", "-w", "--write", "--delete",
    "-c", "--git-dir", "--work-tree", "--exec-path",
    "--paginate", "--no-pager",
})


def is_step_safely_idempotent(step: Any, cwd: Optional[Path] = None) -> bool:
    """Validate whether a step is genuinely safe and idempotent based on actual effects.

    Strictly enforces Check 1 safety invariants:
    1. Must explicitly declare is_idempotent=True.
    2. Must NOT be an instance of PlanStep (which represents a code mutation step).
    3. Must NOT declare or contain any file targets or mutation markers.
    4. Action type must be in TRUSTED_IDEMPOTENT_ACTIONS or read_only=True.
    5. Commands:
       - Test runners (pytest, python -m unittest, python -m pytest, custom scripts)
         execute arbitrary user code that can mutate or delete workspace files.
         The runtime cannot prove their effects are safe to repeat, so automatic retry
         is DISABLED (max_retries = 0).
       - Shell execution (string command or allow_shell=True) is strictly forbidden.
       - Wrappers and indirect dispatch (sh, bash, cmd, powershell, python) are rejected.
       - Only fixed, strictly read-only Git inspection queries (status, diff, log, rev-parse, show)
         without mutating flags are eligible.
    """
    raw_claim = getattr(step, "is_idempotent", None)
    if raw_claim is None and isinstance(step, dict):
        raw_claim = step.get("is_idempotent")
    if not bool(raw_claim):
        return False

    # 1. Reject PlanStep instances (which represent System 2 code mutations)
    from system2 import PlanStep as Sys2PlanStep
    from orchestrator import PlanStep as OrchPlanStep
    step_cls = getattr(step, "__class__", None)
    if isinstance(step, (Sys2PlanStep, OrchPlanStep)) or (step_cls is not None and getattr(step_cls, "__name__", "") == "PlanStep"):
        return False

    # 2. Reject explicit mutation markers and target files
    for marker in ("new_files", "mutates_files", "modifies_code", "write_files", "target_files_to_modify"):
        val = getattr(step, marker, None)
        if val is None and isinstance(step, dict):
            val = step.get(marker)
        if val:
            return False

    # 3. Check action_type: MUST be in TRUSTED_IDEMPOTENT_ACTIONS
    # Invariant 6: Untrusted model labels like read_only=True alone must not authorize unknown action types
    action_type = str(
        getattr(step, "action_type", "")
        or (step.get("action_type") if isinstance(step, dict) else "")
        or getattr(step, "type", "")
        or (step.get("type") if isinstance(step, dict) else "")
    ).lower().strip()

    if action_type not in TRUSTED_IDEMPOTENT_ACTIONS:
        return False

    # 4. Check command / args if present
    cmd = getattr(step, "command", None)
    if cmd is None and isinstance(step, dict):
        cmd = step.get("command")
    if cmd is None:
        cmd = getattr(step, "cmd", None) or (step.get("cmd") if isinstance(step, dict) else None)
    if cmd is None:
        cmd = getattr(step, "args", None) or (step.get("args") if isinstance(step, dict) else None)

    if cmd is not None:
        # Shell strings or non-sequences are forbidden
        if isinstance(cmd, str):
            return False
        if not isinstance(cmd, (list, tuple)):
            return False
        if not cmd or not any(str(c).strip() for c in cmd):
            return False

        # Reject if shell is allowed
        allow_shell = bool(
            getattr(step, "allow_shell", False)
            or (step.get("allow_shell") if isinstance(step, dict) else False)
            or getattr(step, "shell", False)
            or (step.get("shell") if isinstance(step, dict) else False)
        )
        if allow_shell:
            return False

        # Validate via CommandPolicy
        from core.runtime.command_policy import DEFAULT_COMMAND_POLICY
        work_cwd = cwd or Path.cwd()
        policy_res = DEFAULT_COMMAND_POLICY.validate(cmd, work_cwd)
        if not policy_res.is_allowed:
            return False

        # Validate that the command is genuinely read-only and idempotent
        cmd_list = [str(c) for c in cmd]
        raw_exe = Path(cmd_list[0]).name.lower()
        if raw_exe.endswith(".exe"):
            raw_exe = raw_exe[:-4]

        # Check 1: Test runners and python scripts can run arbitrary code with side effects
        # (writing files, deleting files, database changes). The runtime cannot prove they
        # are side-effect-free based on executable name alone. Automatic retry is DISABLED.
        if raw_exe in ("pytest", "python", "python3"):
            return False

        if raw_exe == "git":
            # Check git subcommand and mutating flags
            subcmd = None
            for token in cmd_list[1:]:
                token_lower = token.lower()
                if any(token_lower == flag or token_lower.startswith(f"{flag}=") for flag in FORBIDDEN_MUTATING_GIT_FLAGS):
                    return False
                if not token.startswith("-") and subcmd is None:
                    subcmd = token_lower

            if subcmd not in AUTHORIZED_READONLY_GIT_SUBCOMMANDS:
                return False
        else:
            # Arbitrary binaries, shell interpreters, network tools (rm, curl, bash, powershell, etc.)
            return False

    return True


class PlanProgressTracker:
    """Durable progress tracker for plan steps.

    Persists progress records to `.brainfrog/plans/<plan_id>.progress.json`
    and reconciles them with plan metadata.
    """

    def __init__(self, workspace_dir: Path, plan_id: Optional[str] = None) -> None:
        self.workspace_dir = Path(workspace_dir).resolve()
        self.plan_id = plan_id
        self.plans_dir = self.workspace_dir / ".brainfrog" / "plans"
        self.locks_dir = self.workspace_dir / ".brainfrog" / "locks"
        self.progress_file = (self.plans_dir / f"{self.plan_id}.progress.json") if self.plan_id else None
        self._lock = threading.Lock()
        self._records: Dict[str, StepProgressRecord] = {}
        self._active_step_id: Optional[str] = None
        with self._get_lock():
            self._clean_stale_tmp_files()
            self._load()

    def _get_lock(self) -> Any:
        if not self.plan_id:
            return self._lock
        from core.runtime.session import _get_cross_process_session_lock
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        lock_file = self.locks_dir / f"plan_{self.plan_id}_progress.lock"
        return _get_cross_process_session_lock(lock_file, timeout=10.0)

    def _clean_stale_tmp_files(self) -> None:
        """Clean up abandoned temporary progress files older than 60 seconds.

        Strictly preserves active files and temporary files held open by live writers,
        even if older than 60 seconds. Checks both file age and active locks/handles.
        """
        if not self.plans_dir.exists():
            return
        pattern = f"{self.plan_id}.tmp_*" if self.plan_id else "*.tmp_*"
        try:
            now = time.time()
            for f in self.plans_dir.glob(pattern):
                try:
                    # Never delete a file that is in active use by any process
                    if _is_file_in_active_use(f):
                        continue
                    if (now - f.stat().st_mtime) > 60.0:
                        f.unlink(missing_ok=True)
                except OSError:
                    pass
        except Exception:
            pass

    def _load(self) -> None:
        """Load persisted progress from disk if present."""
        if not self.progress_file or not self.progress_file.exists():
            return
        try:
            content = self.progress_file.read_text(encoding="utf-8")
            data = json.loads(content)
        except Exception as exc:
            raise ValueError(f"Corrupt progress file for plan '{self.plan_id}': {exc}") from exc

        if not isinstance(data, dict):
            raise ValueError(f"Malformed progress file for plan '{self.plan_id}': root must be a dict.")

        file_plan_id = data.get("plan_id")
        if file_plan_id and self.plan_id and file_plan_id != self.plan_id:
            raise ValueError(
                f"Progress file plan_id mismatch: expected '{self.plan_id}', found '{file_plan_id}'."
            )

        if "steps" not in data or not isinstance(data["steps"], list):
            raise ValueError(f"Malformed progress file for plan '{self.plan_id}': missing 'steps' list.")

        for s in data["steps"]:
            if not isinstance(s, dict):
                raise ValueError(f"Malformed step record in progress file for plan '{self.plan_id}': {s!r}")
            rec = StepProgressRecord.from_dict(s)
            self._records[rec.step_id] = rec

    def _persist(self) -> None:
        """Persist current step progress atomically."""
        if not self.progress_file:
            return
        payload = {
            "plan_id": self.plan_id,
            "updated_at": time.time(),
            "steps": [r.to_dict() for r in self._records.values()],
        }
        _safe_atomic_write_json(self.progress_file, payload)

    def initialize_steps(self, steps: Sequence[Any]) -> None:
        """Initialize progress records for all plan steps if not yet tracked."""
        with self._get_lock():
            for s in steps:
                s_id = str(getattr(s, "id", "") or (s.get("id") if isinstance(s, dict) else str(s))).strip()
                s_desc = str(getattr(s, "description", "") or (s.get("description") if isinstance(s, dict) else str(s)))
                raw_files = getattr(s, "files", None) or (s.get("files") if isinstance(s, dict) else None) or []
                s_files = list(raw_files)
                s_idem = is_step_safely_idempotent(s, cwd=self.workspace_dir)
                if s_id and s_id not in self._records:
                    self._records[s_id] = StepProgressRecord(
                        step_id=s_id,
                        description=s_desc,
                        status=StepExecutionStatus.PENDING,
                        is_idempotent=s_idem,
                        files=s_files,
                    )
            self._persist()

    def _clear_active(self, step_id: str) -> None:
        if self._active_step_id == step_id:
            self._active_step_id = None

    def mark_running(
        self,
        step_id: str,
        is_idempotent: bool = False,
        max_retries: int = 0,
        owner_pid: Optional[int] = None,
        lease_timeout: float = 60.0,
    ) -> None:
        with self._get_lock():
            rec = self._records.get(step_id)
            if not rec:
                rec = StepProgressRecord(step_id=step_id, description=step_id)
                self._records[step_id] = rec
            rec.status = StepExecutionStatus.RUNNING
            rec.started_at = time.time()
            rec.is_idempotent = is_idempotent
            rec.max_retries = max_retries

            # Record process ownership and lease metadata
            effective_pid = owner_pid if owner_pid is not None else os.getpid()
            rec.owner_pid = effective_pid
            proc_create_time = None
            if _HAS_PSUTIL and psutil is not None:
                try:
                    proc_create_time = psutil.Process(effective_pid).create_time()
                except Exception:
                    pass
            rec.owner_create_time = proc_create_time
            rec.lease_acquired_at = time.time()
            rec.lease_timeout = lease_timeout
            rec.lease_id = secrets.token_hex(8)

            self._active_step_id = step_id
            self._persist()

    def mark_passed(
        self,
        step_id: str,
        transaction_id: Optional[str] = None,
        file_hashes: Optional[Dict[str, str]] = None,
        requirement_digest: Optional[str] = None,
    ) -> None:
        with self._get_lock():
            self._clear_active(step_id)
            rec = self._records.get(step_id)
            if rec:
                rec.status = StepExecutionStatus.PASSED
                rec.completed_at = time.time()
                rec.transaction_id = transaction_id
                rec.error = None
                rec.lease_id = None
                if file_hashes is not None:
                    rec.file_hashes = dict(file_hashes)
                elif rec.files:
                    computed_hashes: Dict[str, str] = {}
                    for f in rec.files:
                        fp = self.workspace_dir / f
                        if fp.exists() and fp.is_file():
                            try:
                                computed_hashes[f] = hashlib.sha256(fp.read_bytes()).hexdigest()
                            except Exception:
                                pass
                    rec.file_hashes = computed_hashes
                if requirement_digest is not None:
                    rec.requirement_digest = requirement_digest
                self._persist()

    def mark_failed(self, step_id: str, error: str) -> None:
        with self._get_lock():
            self._clear_active(step_id)
            rec = self._records.get(step_id)
            if rec:
                rec.status = StepExecutionStatus.FAILED
                rec.completed_at = time.time()
                rec.error = error
                rec.lease_id = None
                self._persist()

    def mark_timed_out(self, step_id: str, error: str) -> None:
        with self._get_lock():
            self._clear_active(step_id)
            rec = self._records.get(step_id)
            if rec:
                rec.status = StepExecutionStatus.TIMED_OUT
                rec.completed_at = time.time()
                rec.error = error
                rec.lease_id = None
                self._persist()

    def mark_cancelled(self, step_id: str, error: str) -> None:
        with self._get_lock():
            self._clear_active(step_id)
            rec = self._records.get(step_id)
            if rec:
                rec.status = StepExecutionStatus.CANCELLED
                rec.completed_at = time.time()
                rec.error = error
                rec.lease_id = None
                self._persist()

    def mark_interrupted(self, step_id: str, error: str) -> None:
        with self._get_lock():
            self._clear_active(step_id)
            rec = self._records.get(step_id)
            if rec:
                rec.status = StepExecutionStatus.INTERRUPTED
                rec.completed_at = time.time()
                rec.error = error
                rec.lease_id = None
                self._persist()

    def mark_skipped(self, step_id: str, reason: str = "Preceding step failed") -> None:
        with self._get_lock():
            self._clear_active(step_id)
            rec = self._records.get(step_id)
            if rec and rec.status == StepExecutionStatus.PENDING:
                rec.status = StepExecutionStatus.SKIPPED
                rec.error = reason
                rec.lease_id = None
                self._persist()

    def is_step_actively_owned(self, step_id: str) -> Tuple[bool, Optional[int], str]:
        """Check if a step is actively held by a live OS process under lease.

        Enforces Phase 4B.2 Invariants 3 & 4:
        - A RUNNING record must not be classified as abandoned solely because a new tracker
          instance has _active_step_id = None.
        - If another process may still own the execution, recovery must fail closed or establish
          ownership through a reliable process/lease mechanism.
        - Stale process/lease metadata and PID reuse are verified.

        Returns: (is_owned, owner_pid, reason)
        """
        with self._get_lock():
            rec = self._records.get(step_id)
            if not rec or rec.status != StepExecutionStatus.RUNNING:
                return (False, None, "not_running")

            if rec.owner_pid is None:
                if self._active_step_id == step_id:
                    return (True, os.getpid(), "active_in_current_instance")
                return (False, None, "no_owner_pid")

            owner_pid = rec.owner_pid

            # 1. Check PID reuse via process creation time
            if rec.owner_create_time is not None and _HAS_PSUTIL and psutil is not None:
                try:
                    proc = psutil.Process(owner_pid)
                    current_create_time = proc.create_time()
                    if abs(current_create_time - rec.owner_create_time) > 1.0:
                        logger.warning(
                            "PID reuse detected for step '%s' owner PID %d: expected create_time=%.1f, actual=%.1f.",
                            step_id, owner_pid, rec.owner_create_time, current_create_time,
                        )
                        return (False, owner_pid, "pid_reused")
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    return (False, owner_pid, "owner_process_dead")

            # 2. Check process liveness
            if not is_pid_running(owner_pid):
                return (False, owner_pid, "owner_process_dead")

            # 3. If owner is current process
            if owner_pid == os.getpid():
                if self._active_step_id == step_id:
                    return (True, owner_pid, "active_in_current_instance")
                return (False, owner_pid, "same_process_inactive")

            # 4. Another live OS process owns the step!
            now = time.time()
            lease_acquired = rec.lease_acquired_at or 0.0
            lease_timeout = rec.lease_timeout or 60.0
            if (now - lease_acquired) > lease_timeout:
                from core.runtime.workspace_lock import WorkspaceLock
                meta = WorkspaceLock(self.workspace_dir)._read_meta()
                if meta and meta.get("pid") == owner_pid and not WorkspaceLock(self.workspace_dir).is_stale(meta):
                    return (True, owner_pid, "active_holds_workspace_lock")
                # Even if lease expired, if process is still running, another process may still own execution
                return (True, owner_pid, "owner_alive_potential_conflict")

            return (True, owner_pid, "active_owner_process_alive")

    def reconcile_ambiguous_step(
        self,
        step_id: str,
        *,
        new_status: StepExecutionStatus,
        verification_evidence: Optional[str] = None,
        operator: str,
        operator_attestation: Optional[str] = None,
        transaction_id: Optional[str] = None,
        transaction_status: Optional[Any] = None,
        acceptance_verified: Optional[bool] = None,
        target_content_verified: Optional[bool] = None,
    ) -> StepProgressRecord:
        """Operator reconciliation for an interrupted or ambiguous step.

        Requires explicit operator identity and non-empty attestation/evidence.
        Fails closed on missing provenance.
        When marking PASSED, independently verifies transaction outcome if available,
        and strictly rejects reconciliation if:
        - Target file exists but target_content_verified is False (Invariant 1).
        - Transaction is committed but acceptance conditions are false (acceptance_verified is False) (Invariant 2).
        - Another live OS process holds execution ownership (Invariant 4).
        - The associated transaction was rolled back or failed.
        """
        if not operator or not operator.strip():
            raise ValueError("Operator identity must be provided for reconciliation")

        attestation = (operator_attestation or verification_evidence or "").strip()
        if not attestation:
            raise ValueError("Operator attestation or verification evidence must be provided for reconciliation")

        if new_status not in (StepExecutionStatus.PASSED, StepExecutionStatus.FAILED, StepExecutionStatus.CANCELLED):
            raise ValueError(f"Reconciliation target status must be terminal (PASSED, FAILED, or CANCELLED), got {new_status}")

        with self._get_lock():
            # Invariant 4: Check if another live process actively owns the step
            is_owned, owner_pid, reason = self.is_step_actively_owned(step_id)
            if is_owned and owner_pid != os.getpid():
                raise ActiveProcessOwnershipError(
                    step_id,
                    owner_pid,
                    f"Cannot reconcile step while active process PID {owner_pid} holds execution ownership ({reason}).",
                )

            rec = self._records.get(step_id)
            if not rec:
                raise KeyError(f"Step '{step_id}' not found in progress records")

            effective_tx_id = transaction_id or rec.transaction_id
            reconciliation_type = "operator_attestation"
            tx_verified = False

            if new_status == StepExecutionStatus.PASSED:
                # Invariant 1: Target file existence alone must never establish successful step completion
                if target_content_verified is False:
                    raise ValueError(
                        f"Cannot reconcile step '{step_id}' as PASSED: "
                        f"target file content verification failed (target file exists but contains incorrect content)."
                    )

                # Invariant 2: A committed transaction must not automatically imply intended outcome achieved
                if acceptance_verified is False:
                    raise ValueError(
                        f"Cannot reconcile step '{step_id}' as PASSED: "
                        f"acceptance conditions are false or unverified despite committed transaction."
                    )

                # Independent verification check against transaction state
                tx_st_str = ""
                tx_data = {}
                if transaction_status is not None:
                    tx_st_str = getattr(transaction_status, "value", str(transaction_status)).lower()
                elif effective_tx_id:
                    tx_path = self.workspace_dir / ".brainfrog" / "transactions" / f"{effective_tx_id}.json"
                    if tx_path.exists():
                        try:
                            tx_data = json.loads(tx_path.read_text(encoding="utf-8"))
                            tx_st_str = str(tx_data.get("status", "")).lower()
                        except Exception:
                            pass

                # If transaction was rolled back or failed, CANNOT reconcile as PASSED!
                if tx_st_str in ("rolled_back", "failed", "rolling_back", "cancelled"):
                    raise ValueError(
                        f"Cannot reconcile step '{step_id}' as PASSED: "
                        f"associated transaction '{effective_tx_id}' was rolled back or failed (status='{tx_st_str}')."
                    )

                # Check domain verification in transaction data if present
                domain_ver = tx_data.get("domain_verification")
                if domain_ver and isinstance(domain_ver, dict):
                    if domain_ver.get("passed") is False:
                        raise ValueError(
                            f"Cannot reconcile step '{step_id}' as PASSED: "
                            f"associated transaction '{effective_tx_id}' domain verification failed."
                        )

                if tx_st_str in ("verified",):
                    reconciliation_type = "verified_transaction"
                    tx_verified = True
                elif tx_st_str in ("committed", "committed_untested"):
                    # Committed transaction requires explicit verified acceptance conditions to be stamped verified
                    if acceptance_verified is True:
                        reconciliation_type = "verified_transaction"
                        tx_verified = True
                    else:
                        reconciliation_type = "operator_attestation"
                        tx_verified = False
                else:
                    reconciliation_type = "operator_attestation"
                    tx_verified = False

            rec.status = new_status
            rec.completed_at = time.time()
            rec.evidence = f"OPERATOR_ATTESTATION: {attestation}" if reconciliation_type == "operator_attestation" else attestation
            rec.operator_attestation = attestation
            rec.reconciled_by = operator.strip()
            rec.reconciled_at = time.time()
            rec.reconciliation_type = reconciliation_type
            rec.transaction_verified = tx_verified
            rec.lease_id = None
            if effective_tx_id:
                rec.transaction_id = effective_tx_id

            if new_status in (StepExecutionStatus.FAILED, StepExecutionStatus.CANCELLED):
                rec.error = f"Reconciled as {new_status.value} by operator {operator}: {attestation}"
            else:
                rec.error = None
            self._persist()
            return rec

    def get_records(self) -> List[StepProgressRecord]:
        with self._get_lock():
            return list(self._records.values())

    def get_record(self, step_id: str) -> Optional[StepProgressRecord]:
        with self._get_lock():
            return self._records.get(step_id)

    @property
    def active_step_id(self) -> Optional[str]:
        with self._get_lock():
            return self._active_step_id

    def is_step_active(self, step_id: str) -> bool:
        """Check if a step is actively executing in the current runner context or on the system."""
        with self._get_lock():
            rec = self._records.get(step_id)
            if not rec or rec.status != StepExecutionStatus.RUNNING:
                return False
            if self._active_step_id == step_id:
                return True
            if rec.owner_pid is not None and rec.owner_pid != os.getpid():
                is_owned, _, _ = self.is_step_actively_owned(step_id)
                return is_owned
            return False

    def _has_step_files_mutated(self, rec: StepProgressRecord) -> bool:
        """Check if any files targeted by the step exist or have been modified on disk."""
        for rel_f in rec.files:
            try:
                target_p = (self.workspace_dir / rel_f).resolve()
                if target_p.exists():
                    return True
            except Exception:
                pass
        return False

    def check_ambiguous_interrupted_step(self) -> Optional[StepProgressRecord]:
        """Check for non-idempotent or transaction-bearing steps left in ambiguous RUNNING/INTERRUPTED state.

        Fails closed with ActiveProcessOwnershipError if another live OS process actively holds execution ownership.
        """
        with self._get_lock():
            for rec in self._records.values():
                if rec.status == StepExecutionStatus.RUNNING:
                    is_owned, owner_pid, reason = self.is_step_actively_owned(rec.step_id)
                    if is_owned and owner_pid != os.getpid():
                        raise ActiveProcessOwnershipError(
                            rec.step_id,
                            owner_pid,
                            f"Step '{rec.step_id}' is actively executing under process PID {owner_pid} ({reason}).",
                        )
                if rec.status in (StepExecutionStatus.RUNNING, StepExecutionStatus.INTERRUPTED):
                    # Invariant 7: A workspace transaction committed before progress persistence
                    # must recover as ambiguous when the outcome cannot be independently established.
                    # Never blindly rerun the mutation.
                    if not rec.is_idempotent or rec.transaction_id or (rec.files and self._has_step_files_mutated(rec)):
                        return rec
        return None

    def audit_step_recovery(self, step_id: str) -> StepRecoveryAudit:
        """Audit the recovery decision boundary for a specific plan step with durable evidence.

        Enforces Phase 4B.2 Invariants:
        1. File existence alone must never establish successful step completion.
        2. A committed transaction must not automatically imply that the step's intended outcome was achieved.
        3. A RUNNING record must not be classified as abandoned solely because a new tracker instance has _active_step_id = None.
        4. If another process may still own the execution, recovery must fail closed or establish ownership.
        5. Non-idempotent steps require reconciliation; blind replay is forbidden.
        6. Idempotent retry eligibility is determined by trusted command policy.
        7. Step is PASSED only after actual execution result is durably recorded.
        8. Uncertainty about mutation outcome remains explicit in classification and summary.
        """
        with self._get_lock():
            rec = self._records.get(step_id)
            if not rec:
                raise KeyError(f"Step '{step_id}' not found in progress records")

            is_owned, owner_pid, ownership_reason = self.is_step_actively_owned(step_id)

            # Check PID reuse
            pid_reused = False
            is_owner_alive = False
            if rec.owner_pid is not None:
                if rec.owner_create_time is not None and _HAS_PSUTIL and psutil is not None:
                    try:
                        proc = psutil.Process(rec.owner_pid)
                        cur_time = proc.create_time()
                        if abs(cur_time - rec.owner_create_time) > 1.0:
                            pid_reused = True
                            is_owner_alive = False
                        else:
                            is_owner_alive = True
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        is_owner_alive = False
                else:
                    is_owner_alive = is_pid_running(rec.owner_pid)

            files_targeted = list(rec.files)
            files_existing = []
            for f in files_targeted:
                try:
                    p = (self.workspace_dir / f).resolve()
                    if p.exists():
                        files_existing.append(f)
                except Exception:
                    pass

            has_workspace_mutations = len(files_existing) > 0 or self._has_step_files_mutated(rec)

            mutation_timing = "none"
            if rec.status in (StepExecutionStatus.RUNNING, StepExecutionStatus.INTERRUPTED):
                if has_workspace_mutations or rec.transaction_id:
                    mutation_timing = "after_mutation"
                else:
                    mutation_timing = "before_mutation"
            elif rec.status == StepExecutionStatus.PASSED:
                mutation_timing = "after_mutation" if has_workspace_mutations else "none"

            # Transaction state
            tx_status = None
            tx_verified = rec.transaction_verified
            acceptance_verified = False
            if rec.transaction_id:
                tx_file = self.workspace_dir / ".brainfrog" / "transactions" / f"{rec.transaction_id}.json"
                if tx_file.exists():
                    try:
                        tx_data = json.loads(tx_file.read_text(encoding="utf-8"))
                        tx_status = tx_data.get("status")
                        domain_ver = tx_data.get("domain_verification") or {}
                        if domain_ver.get("passed") is True:
                            acceptance_verified = True
                        elif domain_ver.get("passed") is False:
                            acceptance_verified = False
                            tx_verified = False
                    except Exception:
                        pass

            if rec.status == StepExecutionStatus.PASSED:
                if rec.reconciliation_type == "verified_transaction" and tx_verified:
                    acceptance_verified = True
                elif rec.evidence and ("acceptance" in rec.evidence.lower() or "verified" in rec.evidence.lower()):
                    acceptance_verified = True
                elif rec.completed_at is not None and not rec.error:
                    acceptance_verified = True

            # Invariant 1: File existence ALONE must never establish successful step completion.
            # Invariant 2: A committed transaction must not automatically imply that intended outcome was achieved.
            classification = "requiring_reconciliation"
            evidence_sufficient = False
            decision_reason = ""

            if rec.status == StepExecutionStatus.PASSED:
                classification = "completed"
                evidence_sufficient = True
                decision_reason = "Step passed cleanly with recorded execution result."
            elif rec.status in (StepExecutionStatus.RUNNING, StepExecutionStatus.INTERRUPTED):
                if is_owned and is_owner_alive and rec.owner_pid != os.getpid():
                    classification = "requiring_reconciliation"
                    evidence_sufficient = False
                    decision_reason = f"Step actively owned by live process PID {rec.owner_pid} ({ownership_reason}); recovery must fail closed."
                elif not rec.is_idempotent or has_workspace_mutations or rec.transaction_id:
                    classification = "requiring_reconciliation"
                    evidence_sufficient = False
                    if has_workspace_mutations:
                        decision_reason = (
                            "Workspace mutations exist on disk for interrupted step; "
                            "file existence alone does not establish completion, requiring reconciliation."
                        )
                    elif rec.transaction_id:
                        decision_reason = (
                            "Transaction record exists for interrupted step; "
                            "committed transaction alone does not establish completion, requiring reconciliation."
                        )
                    else:
                        decision_reason = "Non-idempotent step was interrupted; blind replay is forbidden."
                else:
                    classification = "retryable"
                    evidence_sufficient = True
                    decision_reason = "Safe idempotent step with zero workspace mutations; eligible for bounded retry."
            else:
                classification = "failed"
                evidence_sufficient = True
                decision_reason = f"Step in terminal {rec.status.value} state."

            return StepRecoveryAudit(
                step_id=step_id,
                status=rec.status,
                classification=classification,
                is_idempotent=rec.is_idempotent,
                owner_pid=rec.owner_pid,
                is_owner_alive=is_owner_alive,
                pid_reused=pid_reused,
                transaction_id=rec.transaction_id,
                transaction_status=tx_status,
                transaction_verified=tx_verified,
                files_targeted=files_targeted,
                files_existing=files_existing,
                has_workspace_mutations=has_workspace_mutations,
                mutation_timing=mutation_timing,
                acceptance_verified=acceptance_verified,
                evidence_sufficient=evidence_sufficient,
                decision_reason=decision_reason,
            )

    def audit_recovery(self) -> List[StepRecoveryAudit]:
        """Audit recovery evidence for all tracked steps."""
        with self._get_lock():
            return [self.audit_step_recovery(s_id) for s_id in self._records.keys()]

    def get_execution_summary(self) -> Dict[str, List[str]]:
        """Return categorized step summary distinguishing completed, failed, and reconciliation-required work.

        Invariants enforced:
        1. completed, failed, and requiring_reconciliation are mutually exclusive and exhaustive for all persisted records.
        2. completed strictly contains steps with PASSED status.
        3. Interrupted or abandoned non-idempotent steps are placed in requiring_reconciliation (never automatically replayed).
        4. Interrupted idempotent steps remain visible in failed without requiring manual reconciliation.
        """
        with self._get_lock():
            completed: List[str] = []
            failed: List[str] = []
            requiring_reconciliation: List[str] = []

            for rec in self._records.values():
                if not isinstance(rec.status, StepExecutionStatus):
                    try:
                        rec.status = StepExecutionStatus(str(rec.status))
                    except ValueError:
                        raise ValueError(f"Unknown or malformed step execution status: {rec.status!r}")

                if rec.status == StepExecutionStatus.PASSED:
                    completed.append(rec.step_id)
                elif rec.status in (
                    StepExecutionStatus.FAILED,
                    StepExecutionStatus.TIMED_OUT,
                    StepExecutionStatus.CANCELLED,
                    StepExecutionStatus.SKIPPED,
                    StepExecutionStatus.PENDING,
                ):
                    failed.append(rec.step_id)
                elif rec.status in (StepExecutionStatus.RUNNING, StepExecutionStatus.INTERRUPTED):
                    if not rec.is_idempotent or rec.transaction_id or (rec.files and self._has_step_files_mutated(rec)):
                        requiring_reconciliation.append(rec.step_id)
                    else:
                        failed.append(rec.step_id)
                else:
                    raise ValueError(f"Unhandled step execution status: {rec.status!r}")

            return {
                "completed": completed,
                "failed": failed,
                "requiring_reconciliation": requiring_reconciliation,
            }


class StructuredStepRunner:
    """Bounded, sequential, fail-closed step runner for plan-bound execution."""

    def __init__(
        self,
        workspace_dir: Path,
        orchestrator: Any,
        contract: ApprovedExecutionContract,
        plan_id: Optional[str] = None,
        *,
        step_timeout: float = 60.0,
        tracker: Optional[PlanProgressTracker] = None,
    ) -> None:
        self.workspace_dir = Path(workspace_dir).resolve()
        self.orchestrator = orchestrator
        self.contract = contract
        self.plan_id = plan_id or getattr(contract, "plan_id", None)
        self.step_timeout = step_timeout
        self.tracker = tracker or PlanProgressTracker(self.workspace_dir, self.plan_id)

    def get_summary(self) -> Dict[str, List[str]]:
        """Return categorized step execution summary from tracker."""
        return self.tracker.get_execution_summary()

    def audit_recovery(self) -> List[StepRecoveryAudit]:
        """Audit recovery evidence for all tracked steps."""
        return self.tracker.audit_recovery()

    def audit_step_recovery(self, step_id: str) -> StepRecoveryAudit:
        """Audit recovery evidence for a specific step."""
        return self.tracker.audit_step_recovery(step_id)

    def revalidate_pre_mutation(self) -> None:
        """Strictly revalidate plan and contract immediately before each mutating step.

        Enforces Phase 4A Invariants 2, 3, 4:
        - Contract must remain unexpired and structurally valid.
        - Plan markdown file on disk must exist and match contract content_hash.
        - Plan revision on disk must match approved execution revision.
        - Plan status on disk must remain IN_PROGRESS.
        - Session ownership on disk must remain bound to contract session.
        """
        # 1. Contract integrity and basic capability validation
        self.contract.validate_integrity()
        if time.time() >= self.contract.expires_at:
            raise PermissionError("Contract has expired during execution")

        # If this contract is an ad-hoc mutation contract without plan binding, skip plan document checks
        target_plan_id = self.plan_id or self.contract.plan_id
        if not target_plan_id:
            return

        if self.contract.plan_id and self.plan_id and self.contract.plan_id != self.plan_id:
            raise StaleExecutionAuthorizationError(
                f"Contract plan_id '{self.contract.plan_id}' does not match runner plan_id '{self.plan_id}'"
            )

        # 2. Plan registry inspection
        from core.plans import PlanRegistry, PlanStatus
        reg = PlanRegistry(self.workspace_dir)
        plan_doc = reg.get_plan(target_plan_id)
        if plan_doc is None:
            raise StaleExecutionAuthorizationError(
                f"Bound plan '{target_plan_id}' vanished from workspace during execution."
            )

        if not plan_doc.path.exists():
            raise StaleExecutionAuthorizationError(
                f"Plan markdown file '{plan_doc.path}' does not exist on disk."
            )

        # 3. Check for out-of-band tampering or refinement
        actual_content = plan_doc.path.read_text(encoding="utf-8", errors="replace")
        actual_hash = hashlib.sha256(actual_content.encode("utf-8")).hexdigest()

        if getattr(plan_doc, "metadata_tampered", False):
            raise StaleExecutionAuthorizationError(
                f"Plan metadata on disk was modified out-of-band during execution! Active authorization invalidated."
            )

        if getattr(plan_doc, "metadata_malformed", False):
            raise StaleExecutionAuthorizationError(
                f"Plan metadata on disk is malformed or corrupted! Active authorization invalidated."
            )

        contract_meta_hash = getattr(self.contract, "plan_metadata_hash", None)
        has_arch_metadata = bool(
            getattr(plan_doc, "target_context", None)
            or getattr(plan_doc, "architecture_strategy", None)
            or getattr(plan_doc, "architectural_constraints", None)
            or getattr(plan_doc, "compatibility_contracts", None)
            or getattr(plan_doc, "behavior_invariants", None)
            or getattr(plan_doc, "migration_constraints", None)
            or getattr(plan_doc, "blocking_unknowns", None)
            or (isinstance(getattr(plan_doc, "metadata_hash", None), str) and getattr(plan_doc, "metadata_hash", "").strip())
        )
        if has_arch_metadata and not contract_meta_hash:
            raise StaleExecutionAuthorizationError(
                f"Execution contract for plan '{target_plan_id}' lacks required semantic metadata binding (plan_metadata_hash). "
                f"Stale or legacy approvals cannot execute without review and reapproval."
            )

        if contract_meta_hash and getattr(plan_doc, "metadata_hash", None) != contract_meta_hash:
            raise StaleExecutionAuthorizationError(
                f"Plan metadata on disk has been modified since approval! Active authorization invalidated."
            )

        if self.contract.plan_content_hash and actual_hash != self.contract.plan_content_hash:
            raise StaleExecutionAuthorizationError(
                f"Plan content on disk was modified during execution! "
                f"Contract hash: {self.contract.plan_content_hash[:8]}, disk hash: {actual_hash[:8]}. "
                "Authorization invalidated."
            )

        # 4. Check revision matches approved execution revision
        # Note: Upon READY -> IN_PROGRESS transition, revision advances to contract.plan_revision + 1
        expected_rev = self.contract.plan_revision + 1 if self.contract.plan_revision is not None else None
        if expected_rev is not None and plan_doc.revision != expected_rev and plan_doc.revision != self.contract.plan_revision:
            raise StaleExecutionAuthorizationError(
                f"Plan revision on disk ({plan_doc.revision}) changed during execution "
                f"(expected {expected_rev}). Active authorization invalidated."
            )

        # 5. Check status is strictly executable (READY or IN_PROGRESS)
        if plan_doc.status not in (PlanStatus.IN_PROGRESS, PlanStatus.READY):
            raise StaleExecutionAuthorizationError(
                f"Plan '{target_plan_id}' status on disk is '{plan_doc.status.value}', expected 'ready' or 'in_progress'."
            )

        # 6. Check session ownership
        plan_session = plan_doc.session_id or (
            plan_doc.to_metadata_dict().get("session_id") if hasattr(plan_doc, "to_metadata_dict") else ""
        )
        if not plan_session or plan_session.strip() != self.contract.session_id.strip():
            raise StaleExecutionAuthorizationError(
                f"Contract plan ownership mismatch: plan '{target_plan_id}' belongs to session '{plan_session}', "
                f"but contract is bound to session '{self.contract.session_id}'."
            )

        # 7. Re-run contract plan binding validation
        self.contract.validate_plan_binding(self.workspace_dir)

    def run_steps(self, steps: Sequence[Any]) -> List[Any]:
        """Execute steps sequentially with stop-on-first-failure and progress tracking."""
        # Check for crash recovery / ambiguous uncompleted steps before running
        ambiguous = self.tracker.check_ambiguous_interrupted_step()
        if ambiguous is not None:
            raise AmbiguousStepExecutionError(
                ambiguous.step_id,
                f"Step was interrupted in a non-idempotent state; blind rerun is forbidden.",
            )

        self.tracker.initialize_steps(steps)
        results: List[Any] = []

        for step in steps:
            raw_id = getattr(step, "id", None) if not isinstance(step, dict) else step.get("id")
            if raw_id is None or not str(raw_id).strip():
                raise ValueError("Step payload is malformed: missing or empty step 'id'.")
            step_id = str(raw_id).strip()
            rec = self.tracker.get_record(step_id)
            if rec and rec.status == StepExecutionStatus.PASSED:
                # Workspace drift & requirement staleness verification
                drift_detected = False
                drift_reason = ""
                raw_f = getattr(step, "files", None) or (step.get("files") if isinstance(step, dict) else []) or []
                if rec.file_hashes:
                    for f_rel, expected_h in rec.file_hashes.items():
                        fp = self.workspace_dir / f_rel
                        if not fp.exists():
                            drift_detected = True
                            drift_reason = f"Workspace drift: file '{f_rel}' verified in step '{step_id}' no longer exists on disk."
                            break
                        actual_h = hashlib.sha256(fp.read_bytes()).hexdigest()
                        if actual_h != expected_h:
                            drift_detected = True
                            drift_reason = f"Workspace drift: file '{f_rel}' verified in step '{step_id}' was modified outside runner (expected {expected_h[:8]}, got {actual_h[:8]})."
                            break
                elif raw_f:
                    for f_rel in raw_f:
                        fp = self.workspace_dir / f_rel
                        if not fp.exists():
                            drift_detected = True
                            drift_reason = f"Workspace drift: target file '{f_rel}' in step '{step_id}' does not exist on disk."
                            break

                if not drift_detected and rec.requirement_digest:
                    cur_digest = self._compute_step_requirement_digest(step)
                    if cur_digest and cur_digest != rec.requirement_digest:
                        drift_detected = True
                        drift_reason = f"Requirement changed for step '{step_id}' since previous verification."

                if drift_detected:
                    logger.warning(drift_reason)
                    self.tracker.mark_failed(step_id, drift_reason)
                else:
                    # Idempotent skip for already passed step
                    continue

            # Check cancellation token before step execution
            cancel_ev = _CURRENT_CANCEL_EVENT.get()
            if cancel_ev and cancel_ev.is_set():
                self.tracker.mark_cancelled(step_id, "Execution cancelled before step start")
                self._skip_remaining(steps, step_id, "Execution cancelled")
                raise KeyboardInterrupt("Execution cancelled by cancellation token")

            # Invariants 3 & 4: Revalidate plan and contract immediately before mutating step
            try:
                self.revalidate_pre_mutation()
            except BaseException as val_exc:
                self.tracker.mark_failed(step_id, str(val_exc))
                self._skip_remaining(steps, step_id, f"Step '{step_id}' authorization invalidated: {val_exc}")
                raise

            # Invariant: Blocking unknown check before step execution
            eff_plan_id = self.plan_id or (getattr(self.contract, "plan_id", None) if self.contract else None)
            if eff_plan_id:
                from core.plans import PlanRegistry, PlanStatus, BlockingUnknownError, format_blocking_unknown_explanation
                p_reg = PlanRegistry(self.workspace_dir)
                plan_doc = p_reg.get_plan(eff_plan_id)
                if plan_doc is not None:
                    raw_files = getattr(step, "files", None) or (step.get("files") if isinstance(step, dict) else []) or []
                    is_blocked, blk_items = plan_doc.has_unresolved_blocking_unknowns(step_id=step_id, files=raw_files)
                    if is_blocked:
                        err_msg = format_blocking_unknown_explanation(
                            step_id=step_id,
                            unknowns=blk_items,
                            step_files=raw_files,
                        )
                        self.tracker.mark_failed(step_id, err_msg)
                        self._skip_remaining(steps, step_id, f"Step '{step_id}' blocked by unresolved unknowns")
                        if plan_doc.status in (PlanStatus.IN_PROGRESS, PlanStatus.READY):
                            try:
                                p_reg.update_status(
                                    plan_id=plan_doc.id,
                                    new_status=PlanStatus.BLOCKED,
                                    actor="step_runner",
                                    reason=f"Step '{step_id}' halted: unresolved blocking unknowns",
                                    contract=self.contract,
                                )
                            except Exception:
                                pass
                        raise BlockingUnknownError(step_id, blk_items, err_msg, step_files=raw_files)

            # Mark step running
            is_idem = is_step_safely_idempotent(step, cwd=self.workspace_dir)
            max_retries = 1 if is_idem else 0
            self.tracker.mark_running(step_id, is_idempotent=is_idem, max_retries=max_retries)

            # Execute step with bounded timeout and retry logic
            attempt = 0
            step_result = None
            last_err: Optional[BaseException] = None

            while attempt <= max_retries:
                attempt += 1
                try:
                    step_result = self._execute_single_step(step, step_id)
                    break
                except BaseException as exc:
                    last_err = exc
                    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                        self.tracker.mark_cancelled(step_id, str(exc))
                        self._skip_remaining(steps, step_id, "Execution interrupted/cancelled")
                        raise
                    if isinstance(exc, StepExecutionTimeoutError):
                        self.tracker.mark_timed_out(step_id, str(exc))
                        self._skip_remaining(steps, step_id, f"Step '{step_id}' timed out")
                        raise
                    if attempt <= max_retries and is_idem:
                        if self._has_uncommitted_workspace_mutations():
                            logger.error(
                                "Aborting retry of step %s: workspace mutation detected during failed execution.",
                                step_id,
                            )
                            self.tracker.mark_failed(step_id, f"Workspace mutation detected during failed attempt: {exc}")
                            self._skip_remaining(steps, step_id, f"Step '{step_id}' mutated workspace despite idempotent claim")
                            raise RuntimeError(f"Step '{step_id}' mutated workspace during execution; retry aborted.") from exc
                        logger.warning("Retrying safe idempotent step %s (attempt %d/%d): %s", step_id, attempt, max_retries, exc)
                        time.sleep(0.05)
                        continue
                    else:
                        self.tracker.mark_failed(step_id, str(exc))
                        self._skip_remaining(steps, step_id, f"Step '{step_id}' failed: {exc}")
                        raise

            # Step passed cleanly
            tx_id = getattr(self.orchestrator.cfg.last_transaction_result, "transaction_id", None)
            step_file_hashes: Dict[str, str] = {}
            raw_f = getattr(step, "files", None) or (step.get("files") if isinstance(step, dict) else []) or []
            for f in raw_f:
                fp = self.workspace_dir / f
                if fp.exists() and fp.is_file():
                    try:
                        step_file_hashes[f] = hashlib.sha256(fp.read_bytes()).hexdigest()
                    except Exception:
                        pass
            step_req_digest = self._compute_step_requirement_digest(step)
            self.tracker.mark_passed(
                step_id,
                transaction_id=tx_id,
                file_hashes=step_file_hashes,
                requirement_digest=step_req_digest,
            )
            results.append(step_result)

        return results

    def _compute_step_requirement_digest(self, step: Any) -> str:
        """Compute deterministic digest of step specification and relevant architecture constraints."""
        desc = str(getattr(step, "description", "") or (step.get("description") if isinstance(step, dict) else ""))
        raw_f = getattr(step, "files", None) or (step.get("files") if isinstance(step, dict) else []) or []
        files = sorted(str(f) for f in raw_f)
        parts = [desc, "|".join(files)]
        eff_plan_id = self.plan_id or (getattr(self.contract, "plan_id", None) if self.contract else None)
        if eff_plan_id:
            try:
                from core.plans import PlanRegistry
                plan_doc = PlanRegistry(self.workspace_dir).get_plan(eff_plan_id)
                if plan_doc is not None:
                    ctx = plan_doc.get_step_architectural_context(step)
                    for cl in ctx.get("resolved_clarifications", []):
                        parts.append(f"{cl.get('id')}:{cl.get('answer')}")
                    for c in ctx.get("relevant_constraints", []):
                        parts.append(str(c))
            except Exception:
                pass
        payload = "::".join(parts).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _has_uncommitted_workspace_mutations(self) -> bool:
        """Check if any unstaged or untracked changes exist in workspace via git status."""
        try:
            import subprocess
            res = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.workspace_dir,
                capture_output=True,
                text=True,
                timeout=5.0,
            )
            if res.returncode == 0:
                lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
                # Ignore .brainfrog/ control files
                mutations = [
                    line for line in lines
                    if not any(line[3:].strip().startswith(p) for p in (".brainfrog", ".agents"))
                ]
                return len(mutations) > 0
        except Exception:
            pass
        return False

    def _execute_single_step(self, step: Any, step_id: str) -> Any:
        """Execute a single step synchronously within the current thread to maintain lock reentrancy."""
        started_at = time.time()
        if hasattr(self.orchestrator, "cfg"):
            self.orchestrator.cfg.step_timeout = self.step_timeout

        res = self.orchestrator._run_step(step)

        elapsed = time.time() - started_at
        if elapsed > self.step_timeout:
            task_id = _CURRENT_TASK_ID.get()
            if task_id:
                terminate_processes_for_task(task_id)
            raise StepExecutionTimeoutError(step_id, self.step_timeout)

        last_test = getattr(self.orchestrator.cfg, "last_test_result", None)
        if last_test is not None and getattr(last_test, "is_timeout", False):
            raise StepExecutionTimeoutError(step_id, self.step_timeout)

        return res

    def _skip_remaining(self, steps: Sequence[Any], failed_step_id: str, reason: str) -> None:
        """Mark all subsequent pending steps as skipped."""
        found = False
        for s in steps:
            s_id = str(getattr(s, "id", "") or (s.get("id") if isinstance(s, dict) else str(s)))
            if s_id == failed_step_id:
                found = True
                continue
            if found:
                self.tracker.mark_skipped(s_id, reason)
