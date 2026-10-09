"""Workspace Lock — Repository-Level Mutual Exclusion.

Guarantees that concurrent autonomous sessions or tasks targeting the same
workspace repository do not collide on filesystem mutations, git state, or transactions.

Requirements:
- Target workspace X -> LOCKED
- Concurrent attempt on workspace X -> WAIT / REJECT (raises WorkspaceLockedError)
- Concurrent attempt on unrelated workspace Y -> ALLOWED
- Lock released on success, failure, and cancellation.
- Recovers safely from stale locks (dead PID or expired TTL).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Union

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

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    psutil = None  # type: ignore
    _HAS_PSUTIL = False

DEFAULT_LOCK_TIMEOUT: float = 10.0
DEFAULT_STALE_TTL_SECONDS: float = 3600.0  # 1 hour


class WorkspaceLockedError(RuntimeError):
    """Raised when an attempt to acquire a workspace lock fails due to an active concurrent holder."""

    def __init__(
        self,
        message: str,
        *,
        workspace: Path,
        holder_session: Optional[str] = None,
        holder_pid: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.workspace = workspace
        self.holder_session = holder_session
        self.holder_pid = holder_pid


def is_pid_running(pid: int) -> bool:
    """Check if a process with the specified PID is currently alive on the host system."""
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        else:
            os.kill(pid, 0)
            return True
    except (OSError, ProcessLookupError, PermissionError):
        return False
    except Exception:
        return False


class WorkspaceLock:
    """Advisory, cross-process repository mutual exclusion lock with owner metadata."""

    def __init__(
        self,
        workspace: Union[str, Path],
        *,
        session_id: Optional[str] = None,
        actor: Optional[str] = None,
        timeout: float = DEFAULT_LOCK_TIMEOUT,
        stale_ttl: float = DEFAULT_STALE_TTL_SECONDS,
        poll_interval: float = 0.05,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.session_id = session_id or "default"
        self.actor = actor or "system"
        self.timeout = timeout
        self.stale_ttl = stale_ttl
        self.poll_interval = poll_interval

        self.dot_brainfrog = self.workspace / ".brainfrog"
        self.lock_file = self.dot_brainfrog / "workspace.lock"
        self.meta_file = self.dot_brainfrog / "workspace.lock.meta"

        self._thread_lock = threading.RLock()
        self._depth = 0
        self._file: Optional[Any] = None

    def _read_meta(self) -> Optional[Dict[str, Any]]:
        if not self.meta_file.exists():
            return None
        try:
            return json.loads(self.meta_file.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _write_meta(self) -> None:
        try:
            self.dot_brainfrog.mkdir(parents=True, exist_ok=True)
            create_time = None
            if _HAS_PSUTIL and psutil is not None:
                try:
                    create_time = psutil.Process(os.getpid()).create_time()
                except Exception:
                    pass
            meta = {
                "pid": os.getpid(),
                "process_create_time": create_time,
                "session_id": self.session_id,
                "actor": self.actor,
                "acquired_at": time.time(),
                "workspace": str(self.workspace),
            }
            tmp_meta = self.dot_brainfrog / f".tmp_lock_meta_{os.getpid()}_{int(time.time())}.json"
            tmp_meta.write_text(json.dumps(meta), encoding="utf-8")
            tmp_meta.replace(self.meta_file)
        except Exception as e:
            logger.warning(f"Could not persist workspace lock metadata: {e}")

    def _clear_meta(self) -> None:
        try:
            self.meta_file.unlink(missing_ok=True)
        except Exception:
            pass

    def is_stale(self, meta: Optional[Dict[str, Any]]) -> bool:
        """Determine if active lock metadata belongs to a dead process or has exceeded TTL."""
        if not meta:
            return False
        pid = meta.get("pid")
        acquired_at = meta.get("acquired_at", 0)
        proc_create_time = meta.get("process_create_time")

        # 1. TTL expiration
        if (time.time() - acquired_at) > self.stale_ttl:
            return True

        # 2. Dead process and PID reuse check
        if isinstance(pid, int):
            # Stronger check: PID reuse detection via psutil process create_time
            if _HAS_PSUTIL and psutil is not None and proc_create_time is not None:
                try:
                    current_proc_time = psutil.Process(pid).create_time()
                    if abs(current_proc_time - proc_create_time) > 1.0:
                        logger.warning(
                            f"PID reuse detected for lock holder PID {pid} (expected create_time={proc_create_time}, actual={current_proc_time}). Treating lock as stale."
                        )
                        return True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    return True

            if pid == os.getpid():
                return False
            if not is_pid_running(pid):
                return True

        return False

    def is_locked(self) -> bool:
        """Check whether the workspace lock is currently held by an active process."""
        with self._thread_lock:
            if self._depth > 0:
                return True
        meta = self._read_meta()
        if meta and not self.is_stale(meta):
            return True
        return False

    def acquire(self, timeout: Optional[float] = None) -> bool:
        """Acquire the workspace lock with bounded polling and stale recovery."""
        effective_timeout = timeout if timeout is not None else self.timeout

        with self._thread_lock:
            if self._depth > 0:
                self._depth += 1
                return True

            self.dot_brainfrog.mkdir(parents=True, exist_ok=True)
            start_time = time.monotonic()

            while True:
                # Check stale lock
                meta = self._read_meta()
                if meta and self.is_stale(meta):
                    logger.warning(
                        f"Detected stale workspace lock for '{self.workspace}' (holder PID {meta.get('pid')}). Clearing stale lock."
                    )
                    self._clear_meta()
                    try:
                        self.lock_file.unlink(missing_ok=True)
                    except Exception:
                        pass

                # Attempt OS lock
                try:
                    if self._file is None or self._file.closed:
                        self._file = open(self.lock_file, "a+b")
                    fd = self._file.fileno()

                    if msvcrt is not None:
                        self._file.seek(0)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    elif _HAS_FCNTL:
                        getattr(fcntl, "flock")(fd, _LOCK_EX | _LOCK_NB)

                    # Acquired successfully
                    self._write_meta()
                    self._depth = 1
                    return True
                except (OSError, IOError):
                    if self._file and not self._file.closed:
                        try:
                            self._file.close()
                        except Exception:
                            pass
                        self._file = None

                    elapsed = time.monotonic() - start_time
                    if elapsed >= effective_timeout:
                        cur_meta = self._read_meta() or {}
                        holder_session = cur_meta.get("session_id")
                        holder_pid = cur_meta.get("pid")
                        raise WorkspaceLockedError(
                            f"Workspace '{self.workspace}' is locked by session '{holder_session}' (PID {holder_pid}). "
                            f"Timed out after {effective_timeout:.1f}s waiting for lock release.",
                            workspace=self.workspace,
                            holder_session=holder_session,
                            holder_pid=holder_pid,
                        )
                    time.sleep(self.poll_interval)

    def release(self) -> None:
        """Release the workspace lock and clear ownership metadata."""
        with self._thread_lock:
            if self._depth <= 0:
                return

            self._depth -= 1
            if self._depth > 0:
                return

            self._clear_meta()

            if self._file is not None:
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

            try:
                self.lock_file.unlink(missing_ok=True)
            except Exception:
                pass

    def __enter__(self) -> WorkspaceLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()
