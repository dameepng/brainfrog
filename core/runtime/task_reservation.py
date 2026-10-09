"""Atomic Task ID Reservation and Idempotency Coordination.

Guarantees:
- Single execution per task_id across threads and processes.
- Atomic reservation before any execution, planning, or code mutation.
- Stale reservation recovery for crashed or dead processes.
- Protection against duplicate execution of completed/proven tasks.
- Proper cleanup on completion, cancellation, and client close.

Product Thesis:
    "Don't just trust the agent. Verify it."
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

from core.runtime.proof import FileProofStore, ProofAlreadyExistsError
from core.runtime.task_finalization import (
    CommitIntentStatus,
    IntentCorruptError,
    TaskFinalizationCoordinator,
    TaskRecoveryRequiredError,
)
from core.runtime.workspace_lock import is_pid_running

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    psutil = None  # type: ignore
    _HAS_PSUTIL = False

try:
    import msvcrt
except ImportError:
    msvcrt = None

try:
    import fcntl
    _HAS_FCNTL = hasattr(fcntl, "flock")
except ImportError:
    fcntl = None
    _HAS_FCNTL = False

_LOCK_EX = getattr(fcntl, "LOCK_EX", 2)
_LOCK_NB = getattr(fcntl, "LOCK_NB", 4)
_LOCK_UN = getattr(fcntl, "LOCK_UN", 8)

logger = logging.getLogger(__name__)


class _TaskCoordinationLock:
    """Advisory cross-process mutual exclusion latch for task reservation mutations.

    Protects critical sections during reservation acquisition, stale reclamation,
    and handle release to ensure check-and-delete / check-and-create operations
    are atomic with respect to other processes.
    """

    def __init__(
        self,
        coord_path: Path,
        thread_lock: threading.RLock,
        timeout: float = 5.0,
        poll_interval: float = 0.02,
    ) -> None:
        self.coord_path = coord_path
        self._thread_lock = thread_lock
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._file: Optional[Any] = None

    def __enter__(self) -> _TaskCoordinationLock:
        self._thread_lock.acquire()
        start_time = time.monotonic()
        self.coord_path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                self._file = open(self.coord_path, "a+b")
                fd = self._file.fileno()
                if msvcrt is not None:
                    self._file.seek(0)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                elif _HAS_FCNTL:
                    getattr(fcntl, "flock")(fd, _LOCK_EX | _LOCK_NB)
                return self
            except (OSError, IOError):
                if self._file and not self._file.closed:
                    try:
                        self._file.close()
                    except Exception:
                        pass
                    self._file = None
                elapsed = time.monotonic() - start_time
                if elapsed >= self.timeout:
                    self._thread_lock.release()
                    raise RuntimeError(
                        f"Timeout ({self.timeout:.1f}s) acquiring task reservation coordination lock at '{self.coord_path}'."
                    )
                time.sleep(self.poll_interval)

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        try:
            if self._file is not None and not self._file.closed:
                try:
                    fd = self._file.fileno()
                    if msvcrt is not None:
                        self._file.seek(0)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    elif _HAS_FCNTL:
                        getattr(fcntl, "flock")(fd, _LOCK_UN)
                except Exception:
                    pass
                finally:
                    try:
                        self._file.close()
                    except Exception:
                        pass
                    self._file = None
        finally:
            self._thread_lock.release()


# Identifier validation pattern: reject control chars, path separators, wildcards, quotes, redirects, pipes, drive letters
_INVALID_TASK_ID_CHARS = re.compile(r"[\x00-\x1f\x7f/\\*?\"'<>|;:~]|(?:\.\.)")
_DRIVE_LETTER_PATTERN = re.compile(r"^[a-zA-Z]:")
MAX_TASK_ID_LENGTH = 128
DEFAULT_RESERVATION_STALE_TTL_SECONDS = 7200.0  # 2 hours

# Process-wide active reservation registry across coordinator instances within the same process
_PROCESS_ACTIVE_RESERVATIONS: Dict[Tuple[Path, str], TaskReservationHandle] = {}
_PROCESS_RESERVATION_LOCK = threading.RLock()


class TaskActiveError(RuntimeError):
    """Raised when an attempt is made to execute a task_id that is currently active."""
    pass


class TaskAlreadyCompletedError(RuntimeError):
    """Raised when an attempt is made to execute a task_id that already has an authoritative proof."""
    pass


def validate_task_id(task_id: str) -> str:
    """Validate task_id against strict path safety and format invariants."""
    if not isinstance(task_id, str):
        raise ValueError("Task ID must be a string.")
    clean = task_id.strip()
    if not clean:
        raise ValueError("Task ID must not be empty.")
    if len(clean) > MAX_TASK_ID_LENGTH:
        raise ValueError(
            f"Task ID exceeds maximum allowed length of {MAX_TASK_ID_LENGTH} characters: '{clean[:32]}...'"
        )
    if _DRIVE_LETTER_PATTERN.match(clean):
        raise ValueError(f"Task ID cannot start with a drive letter specification: '{clean}'")
    if _INVALID_TASK_ID_CHARS.search(clean):
        raise ValueError(f"Task ID contains invalid characters or path traversal: '{clean}'")
    return clean


class TaskReservationHandle:
    """Handle to an active task reservation with unforgeable ownership token."""

    def __init__(
        self,
        coordinator: TaskReservationCoordinator,
        task_id: str,
        lock_path: Path,
        nonce: str,
        pid: int,
    ) -> None:
        self._coordinator = coordinator
        self.task_id = task_id
        self.lock_path = lock_path
        self.nonce = nonce
        self.pid = pid
        self._released = False

    @property
    def is_released(self) -> bool:
        return self._released

    def release(self) -> None:
        """Release this task reservation safely and idempotently."""
        if not self._released:
            self._released = True
            self._coordinator._release_handle(self)

    def __enter__(self) -> TaskReservationHandle:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()


class TaskReservationCoordinator:
    """Atomic task ID reservation manager scoped per workspace."""

    def __init__(
        self,
        workspace: Union[str, Path],
        *,
        stale_ttl: float = DEFAULT_RESERVATION_STALE_TTL_SECONDS,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.tasks_dir = self.workspace / ".brainfrog" / "tasks"
        self.stale_ttl = stale_ttl
        self._thread_lock = threading.RLock()
        self._local_active: Dict[str, TaskReservationHandle] = {}
        self._proof_store = FileProofStore(self.workspace)
        self._finalization = TaskFinalizationCoordinator(self.workspace)

    def _task_lock(self, task_id: str) -> _TaskCoordinationLock:
        coord_path = self.tasks_dir / f".{task_id}.coord"
        return _TaskCoordinationLock(coord_path, self._thread_lock)

    def _get_lock_path(self, task_id: str) -> Path:
        return self.tasks_dir / f"{task_id}.lock"

    def is_proven(self, task_id: str) -> bool:
        """Check if an authoritative proof artifact exists for task_id."""
        return self._proof_store.exists(task_id)

    def is_active(self, task_id: str) -> bool:
        """Check if task_id is actively executing on disk or in-process."""
        with _PROCESS_RESERVATION_LOCK:
            active_h = _PROCESS_ACTIVE_RESERVATIONS.get((self.workspace, task_id))
            if active_h is not None and not active_h.is_released:
                return True

        lock_path = self._get_lock_path(task_id)
        if not lock_path.exists():
            return False

        meta = self._read_lock_meta(lock_path)
        if meta and self._is_stale(meta):
            with self._task_lock(task_id):
                meta_now = self._read_lock_meta(lock_path)
                if meta_now and self._is_stale(meta_now):
                    self._clean_stale_lock(lock_path)
                    return False
                elif not lock_path.exists():
                    return False
        return True

    def _read_lock_meta(self, lock_path: Path) -> Optional[Dict[str, Any]]:
        try:
            if lock_path.exists():
                return json.loads(lock_path.read_text(encoding="utf-8"))
        except Exception:
            pass
        return None

    def _is_stale(self, meta: Dict[str, Any]) -> bool:
        acquired_at = meta.get("acquired_at", 0)
        if (time.time() - acquired_at) > self.stale_ttl:
            return True

        pid = meta.get("pid")
        if isinstance(pid, int):
            proc_create_time = meta.get("process_create_time")
            if _HAS_PSUTIL and psutil is not None and proc_create_time is not None:
                try:
                    current_proc_time = psutil.Process(pid).create_time()
                    if abs(current_proc_time - proc_create_time) > 1.0:
                        return True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    return True
            if pid == os.getpid():
                # Same process: check process-wide active registry for this workspace & task_id
                task_id = meta.get("task_id")
                if task_id:
                    with _PROCESS_RESERVATION_LOCK:
                        active_h = _PROCESS_ACTIVE_RESERVATIONS.get((self.workspace, task_id))
                        if active_h is not None and not active_h.is_released:
                            return False
                return True
            if not is_pid_running(pid):
                return True
        return False

    def _clean_stale_lock(self, lock_path: Path) -> None:
        try:
            lock_path.unlink(missing_ok=True)
            logger.info(f"Reclaimed stale task reservation at {lock_path}")
        except Exception:
            pass

    def reserve(self, task_id: str, actor: str = "system") -> TaskReservationHandle:
        """Atomically reserve a task_id for execution.

        Fails-closed if:
        1. Task ID is invalid or contains traversal.
        2. Task ID already has an authoritative proof artifact (TaskAlreadyCompletedError).
        3. Task ID is already actively reserved (TaskActiveError).
        """
        clean_id = validate_task_id(task_id)

        with self._task_lock(clean_id):
            # 1. Reject if authoritative proof already exists on disk
            if self._proof_store.exists(clean_id):
                raise TaskAlreadyCompletedError(
                    f"Task '{clean_id}' has already been executed with an authoritative proof. "
                    "Duplicate task submissions are rejected to prevent proof tampering."
                )

            # 1b. Reject if task has an unresolved commit intent from a previous run
            try:
                unresolved = self._finalization.get_unresolved_intent(clean_id)
            except IntentCorruptError as ice:
                raise TaskRecoveryRequiredError(
                    f"Task '{clean_id}' has a corrupt finalization intent file: {ice.message}. "
                    "Recovery or manual inspection is required before this task can proceed; duplicate execution is blocked.",
                    task_id=clean_id,
                ) from ice
            if unresolved is not None:
                raise TaskRecoveryRequiredError(
                    f"Task '{clean_id}' has an unresolved commit ('{unresolved.commit_sha}') from an interrupted run without final proof. "
                    "Recovery is required before this task can proceed; duplicate execution is blocked.",
                    task_id=clean_id,
                    commit_sha=unresolved.commit_sha,
                )

            # 2. Reject if currently active across any coordinator in this process
            with _PROCESS_RESERVATION_LOCK:
                active_h = _PROCESS_ACTIVE_RESERVATIONS.get((self.workspace, clean_id))
                if active_h is not None and not active_h.is_released:
                    raise TaskActiveError(f"Task '{clean_id}' is already actively executing.")

            self.tasks_dir.mkdir(parents=True, exist_ok=True)
            lock_path = self._get_lock_path(clean_id)

            # 3. Check existing on-disk reservation
            if lock_path.exists():
                meta = self._read_lock_meta(lock_path)
                if meta and self._is_stale(meta):
                    self._clean_stale_lock(lock_path)
                else:
                    holder_pid = meta.get("pid") if meta else "unknown"
                    raise TaskActiveError(
                        f"Task '{clean_id}' is already actively executing in process {holder_pid}."
                    )

            # 4. Atomic file creation using 'x' mode (O_CREAT | O_EXCL)
            proc_create_time = None
            if _HAS_PSUTIL and psutil is not None:
                try:
                    proc_create_time = psutil.Process(os.getpid()).create_time()
                except Exception:
                    pass

            nonce = secrets.token_hex(16)
            meta_data = {
                "task_id": clean_id,
                "pid": os.getpid(),
                "nonce": nonce,
                "process_create_time": proc_create_time,
                "acquired_at": time.time(),
                "actor": actor,
                "workspace": str(self.workspace),
            }

            try:
                with open(lock_path, "x", encoding="utf-8") as f:
                    json.dump(meta_data, f, indent=2)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except Exception:
                        pass
            except FileExistsError:
                # Concurrent race condition: another thread/process created it first
                existing_meta = self._read_lock_meta(lock_path)
                holder_pid = existing_meta.get("pid") if existing_meta else "concurrent"
                raise TaskActiveError(
                    f"Task '{clean_id}' was just reserved by another process/thread ({holder_pid})."
                )

            handle = TaskReservationHandle(self, clean_id, lock_path, nonce=nonce, pid=os.getpid())
            self._local_active[clean_id] = handle
            with _PROCESS_RESERVATION_LOCK:
                _PROCESS_ACTIVE_RESERVATIONS[(self.workspace, clean_id)] = handle
            return handle

    def _release_handle(self, handle: TaskReservationHandle) -> None:
        with self._thread_lock:
            if self._local_active.get(handle.task_id) is handle:
                self._local_active.pop(handle.task_id, None)
            with _PROCESS_RESERVATION_LOCK:
                cur_h = _PROCESS_ACTIVE_RESERVATIONS.get((self.workspace, handle.task_id))
                if cur_h is handle:
                    _PROCESS_ACTIVE_RESERVATIONS.pop((self.workspace, handle.task_id), None)
        try:
            with self._task_lock(handle.task_id):
                if handle.lock_path.exists():
                    meta = self._read_lock_meta(handle.lock_path)
                    if meta is not None:
                        disk_nonce = meta.get("nonce")
                        disk_pid = meta.get("pid")
                        disk_task_id = meta.get("task_id")
                        if disk_task_id == handle.task_id and disk_pid == handle.pid and disk_nonce == handle.nonce:
                            handle.lock_path.unlink(missing_ok=True)
                        else:
                            logger.warning(
                                f"Reservation on disk for '{handle.task_id}' no longer belongs to this handle "
                                f"(disk: pid={disk_pid}, nonce={disk_nonce}; handle: pid={handle.pid}, nonce={handle.nonce}). "
                                "Preserving replacement reservation."
                            )
        except Exception as exc:
            logger.warning(f"Failed to remove task reservation file for '{handle.task_id}': {exc}")

    def release_all(self) -> None:
        """Release all reservations currently held by this coordinator."""
        with self._thread_lock:
            handles = list(self._local_active.values())
            for h in handles:
                h.release()


__all__ = [
    "TaskActiveError",
    "TaskAlreadyCompletedError",
    "TaskRecoveryRequiredError",
    "TaskReservationCoordinator",
    "TaskReservationHandle",
    "validate_task_id",
]
