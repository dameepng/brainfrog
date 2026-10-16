"""Workspace Lock — Repository-Level Mutual Exclusion and Capability Authority.

Guarantees that concurrent autonomous sessions or tasks targeting the same
workspace repository do not collide on filesystem mutations, git state, or transactions.

Requirements:
- Target workspace X -> LOCKED
- Concurrent attempt on workspace X -> WAIT / REJECT (raises WorkspaceLockedError)
- Concurrent attempt on unrelated workspace Y -> ALLOWED
- Lock released on success, failure, and cancellation.
- Exclusive OS kernel lock is the sole authority for mutual exclusion.
- Lock file is NEVER unlinked before obtaining exclusive OS-lock ownership.
- Nested and reentrant acquisitions reuse the existing logical acquisition and OS handle.
- Reentrant inner releases leave parent OS lock and capabilities active.
- Outermost release revokes capabilities before unlocking OS lock.
- Capability binds to workspace, PID, thread, and governance identifiers.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from contextvars import ContextVar, Token
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
        self.workspace = Path(workspace).resolve()
        self.holder_session = holder_session
        self.holder_pid = holder_pid


class WorkspaceLockRevokedError(WorkspaceLockedError):
    """Raised when an operation is attempted using a revoked, expired, or invalid lock capability."""

    def __init__(self, message: str, *, workspace: Optional[Path] = None) -> None:
        super().__init__(
            message,
            workspace=workspace or Path("."),
            holder_session=None,
            holder_pid=None,
        )


class WorkspaceLockUnsupportedPlatformError(NotImplementedError):
    """Raised when the host platform does not support exclusive kernel locking via msvcrt or fcntl."""

    def __init__(self, message: str, *, workspace: Optional[Path] = None) -> None:
        super().__init__(message)
        self.workspace = Path(workspace).resolve() if workspace else None


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


class WorkspaceLockCapability:
    """Internal runtime capability token representing verified live OS workspace lock ownership.

    This is not cryptographically unforgeable; it is an internal runtime integrity token
    bound to the live OS file lock, canonical workspace path, process PID, owning thread,
    and governance identifiers (session_id, session_incarnation, task_id, actor).
    """

    def __init__(
        self,
        lock: WorkspaceLock,
        workspace: Path,
        pid: int,
        thread_ident: int,
        *,
        session_id: Optional[str] = None,
        session_incarnation: Optional[int] = None,
        task_id: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> None:
        self._lock = lock
        self.workspace = workspace.resolve()
        self.pid = pid
        self.thread_ident = thread_ident
        self.session_id = session_id
        self.session_incarnation = session_incarnation
        self.task_id = task_id
        self.actor = actor
        self._revoked = False

    @property
    def is_revoked(self) -> bool:
        return self._revoked

    def revoke(self) -> None:
        """Mark this capability as revoked."""
        self._revoked = True

    def validate_live(
        self,
        workspace: Optional[Union[str, Path]] = None,
        session_id: Optional[str] = None,
        session_incarnation: Optional[int] = None,
        task_id: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> bool:
        """Validate that this capability is currently valid, unrevoked, and bound to the caller context.

        Fails closed (raises WorkspaceLockRevokedError) if:
        - The capability has been revoked.
        - The OS lock is no longer actively held by this execution.
        - The process PID or thread identifier does not match.
        - Canonical workspace does not match.
        - Governance bindings (if provided and bound) do not match.
        """
        if self._revoked:
            raise WorkspaceLockRevokedError(
                "Workspace lock capability has been revoked (outermost lock released).",
                workspace=self.workspace,
            )

        if os.getpid() != self.pid:
            raise WorkspaceLockRevokedError(
                f"Process mismatch: capability belongs to PID {self.pid}, current PID is {os.getpid()}.",
                workspace=self.workspace,
            )

        if threading.get_ident() != self.thread_ident:
            raise WorkspaceLockRevokedError(
                f"Thread mismatch: capability belongs to thread {self.thread_ident}, current thread is {threading.get_ident()}.",
                workspace=self.workspace,
            )

        if not self._lock.is_active():
            raise WorkspaceLockRevokedError(
                "OS workspace lock is no longer actively held by this execution.",
                workspace=self.workspace,
            )

        if workspace is not None:
            canonical_req = Path(workspace).resolve()
            if canonical_req != self.workspace:
                raise WorkspaceLockRevokedError(
                    f"Workspace mismatch: capability bound to '{self.workspace}', requested for '{canonical_req}'.",
                    workspace=canonical_req,
                )

        if self.session_id and session_id:
            sess_match = (
                self.session_id == session_id
                or self.session_id.endswith(":" + session_id)
                or session_id.endswith(":" + self.session_id)
            )
            if not sess_match:
                raise WorkspaceLockRevokedError(
                    f"Session mismatch: capability bound to session '{self.session_id}', requested '{session_id}'.",
                    workspace=self.workspace,
                )

        if (
            self.session_incarnation is not None
            and session_incarnation is not None
            and self.session_incarnation != session_incarnation
        ):
            raise WorkspaceLockRevokedError(
                f"Session incarnation mismatch: capability bound to incarnation {self.session_incarnation}, requested {session_incarnation}.",
                workspace=self.workspace,
            )

        if self.task_id and task_id and self.task_id != task_id:
            raise WorkspaceLockRevokedError(
                f"Task ID mismatch: capability bound to task '{self.task_id}', requested '{task_id}'.",
                workspace=self.workspace,
            )

        if self.actor and actor and self.actor != actor:
            raise WorkspaceLockRevokedError(
                f"Actor mismatch: capability bound to actor '{self.actor}', requested '{actor}'.",
                workspace=self.workspace,
            )

        return True

    def is_live(self) -> bool:
        """Non-raising check of whether the capability is currently live for this thread."""
        try:
            return self.validate_live()
        except Exception:
            return False

    def __repr__(self) -> str:
        state = "REVOKED" if self._revoked else "ACTIVE"
        return f"<WorkspaceLockCapability {state} workspace={self.workspace} pid={self.pid} thread={self.thread_ident}>"


_ACTIVE_WORKSPACE_CAPABILITY: ContextVar[Optional[WorkspaceLockCapability]] = ContextVar(
    "_ACTIVE_WORKSPACE_CAPABILITY", default=None
)


def get_active_workspace_capability(
    workspace: Optional[Union[str, Path]] = None,
) -> Optional[WorkspaceLockCapability]:
    """Retrieve the active workspace lock capability in the current context, if live."""
    cap = _ACTIVE_WORKSPACE_CAPABILITY.get()
    if cap is None:
        return None
    try:
        cap.validate_live(workspace=workspace)
        return cap
    except Exception:
        return None


class WorkspaceLock:
    """Advisory, cross-process repository mutual exclusion lock with owner metadata and capability tracking."""

    def __init__(
        self,
        workspace: Union[str, Path],
        *,
        session_id: Optional[str] = None,
        session_incarnation: Optional[int] = None,
        task_id: Optional[str] = None,
        actor: Optional[str] = None,
        timeout: float = DEFAULT_LOCK_TIMEOUT,
        stale_ttl: float = DEFAULT_STALE_TTL_SECONDS,
        poll_interval: float = 0.05,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.session_id = session_id if (session_id and session_id.strip()) else None
        self.session_incarnation = session_incarnation
        self.task_id = task_id if (task_id and task_id.strip()) else None
        self.actor = actor if (actor and actor.strip()) else None
        self.timeout = timeout
        self.stale_ttl = stale_ttl
        self.poll_interval = poll_interval

        self.dot_brainfrog = self.workspace / ".brainfrog"
        self.lock_file = self.dot_brainfrog / "workspace.lock"
        self.meta_file = self.dot_brainfrog / "workspace.lock.meta"

        self._thread_lock = threading.RLock()
        self._depth = 0
        self._has_os_lock = False
        self._file: Optional[Any] = None
        self._active_capability: Optional[WorkspaceLockCapability] = None
        self._context_token: Optional[Token[Optional[WorkspaceLockCapability]]] = None
        self._parent_lock: Optional[WorkspaceLock] = None

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
                "session_id": self.session_id or "unbound",
                "session_incarnation": self.session_incarnation,
                "task_id": self.task_id or "unbound",
                "actor": self.actor or "system",
                "acquired_at": time.time(),
                "workspace": str(self.workspace),
            }
            tmp_meta = self.dot_brainfrog / f".tmp_lock_meta_{os.getpid()}_{threading.get_ident()}_{int(time.time() * 1000)}.json"
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
        """Determine if lock metadata belongs to a dead process or has exceeded TTL."""
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
            if _HAS_PSUTIL and psutil is not None and proc_create_time is not None:
                try:
                    current_proc_time = psutil.Process(pid).create_time()
                    if abs(current_proc_time - proc_create_time) > 1.0:
                        logger.warning(
                            f"PID reuse detected for lock holder PID {pid} (expected create_time={proc_create_time}, actual={current_proc_time}). Treating lock metadata as stale."
                        )
                        return True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    return True

            if pid == os.getpid():
                return False
            if not is_pid_running(pid):
                return True

        return False

    def is_active(self) -> bool:
        """Report live internal ownership consistently.

        Does not treat an open file handle alone as proof of OS-lock ownership.
        """
        with self._thread_lock:
            if self._parent_lock is not None:
                return self._parent_lock.is_active()
            return (
                self._depth > 0
                and self._has_os_lock
                and self._file is not None
                and not self._file.closed
                and self._active_capability is not None
                and not self._active_capability.is_revoked
            )

    def is_locked(self) -> bool:
        """Check whether the workspace lock is currently held by an active process."""
        with self._thread_lock:
            if self.is_active():
                return True
        meta = self._read_meta()
        if meta and not self.is_stale(meta):
            return True
        return False

    @property
    def capability(self) -> Optional[WorkspaceLockCapability]:
        """Return the active WorkspaceLockCapability if the lock is currently held, else None."""
        with self._thread_lock:
            if self.is_active():
                return self._active_capability
            return None

    def get_capability(self) -> WorkspaceLockCapability:
        """Return the active WorkspaceLockCapability or fail closed."""
        cap = self.capability
        if cap is None:
            raise WorkspaceLockRevokedError(
                f"WorkspaceLock for '{self.workspace}' is not actively held.",
                workspace=self.workspace,
            )
        return cap

    def _increment_depth(self) -> None:
        with self._thread_lock:
            self._depth += 1

    def _decrement_depth(self) -> None:
        with self._thread_lock:
            if self._depth > 0:
                self._depth -= 1

    def acquire(self, timeout: Optional[float] = None) -> bool:
        """Acquire the workspace lock with bounded polling and atomic OS lock acquisition.

        Invariants enforced:
        - OS lock is the true authority; lock file is NEVER unlinked before OS lock acquisition.
        - Reentrant calls on the same lock reuse handle and increment depth.
        - Reentrant calls across different instances within the same context reuse the parent handle.
        """
        effective_timeout = timeout if timeout is not None else self.timeout

        with self._thread_lock:
            # 1. Reentrancy on the same instance
            if self._depth > 0 and self.is_active():
                self._depth += 1
                return True

            # 2. Reentrancy via ambient context for same canonical workspace, thread, and matching session/task
            ambient_cap = _ACTIVE_WORKSPACE_CAPABILITY.get()
            if ambient_cap is not None and ambient_cap.is_live():
                session_matches = (
                    self.session_id is None
                    or ambient_cap.session_id is None
                    or self.session_id == ambient_cap.session_id
                    or (self.session_id is not None and ambient_cap.session_id is not None and (
                        self.session_id.endswith(":" + ambient_cap.session_id)
                        or ambient_cap.session_id.endswith(":" + self.session_id)
                    ))
                )
                task_matches = (
                    self.task_id is None
                    or ambient_cap.task_id is None
                    or self.task_id == ambient_cap.task_id
                )
                is_same_execution = (
                    ambient_cap.workspace == self.workspace
                    and ambient_cap.pid == os.getpid()
                    and ambient_cap.thread_ident == threading.get_ident()
                    and session_matches
                    and task_matches
                )
                if is_same_execution:
                    self._parent_lock = ambient_cap._lock
                    self._parent_lock._increment_depth()
                    self._depth = 1
                    self._active_capability = ambient_cap
                    return True

            # 3. Outermost acquisition: acquire actual OS lock
            if msvcrt is None and not _HAS_FCNTL:
                raise WorkspaceLockUnsupportedPlatformError(
                    f"Kernel-level workspace locking is unsupported on this platform: "
                    f"neither 'msvcrt' nor 'fcntl.flock' is available. Failing closed for workspace '{self.workspace}'.",
                    workspace=self.workspace,
                )

            self.dot_brainfrog.mkdir(parents=True, exist_ok=True)
            start_time = time.monotonic()

            while True:
                try:
                    if self._file is None or self._file.closed:
                        self._file = open(self.lock_file, "a+b")

                    fd = self._file.fileno()

                    if msvcrt is not None:
                        # Ensure at least 1 byte exists in file to lock on Windows
                        if os.fstat(fd).st_size == 0:
                            self._file.write(b"\0")
                            self._file.flush()
                        self._file.seek(0)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    elif _HAS_FCNTL:
                        getattr(fcntl, "flock")(fd, _LOCK_EX | _LOCK_NB)
                    else:
                        raise WorkspaceLockUnsupportedPlatformError(
                            f"Kernel-level workspace locking is unsupported on this platform: "
                            f"neither 'msvcrt' nor 'fcntl.flock' is available. Failing closed for workspace '{self.workspace}'.",
                            workspace=self.workspace,
                        )

                    # EXCLUSIVE OS LOCK ACQUIRED!
                    self._has_os_lock = True
                    self._depth = 1

                    # Refresh or repair metadata while holding exclusive OS lock
                    self._write_meta()

                    # Construct and publish capability
                    self._active_capability = WorkspaceLockCapability(
                        lock=self,
                        workspace=self.workspace,
                        pid=os.getpid(),
                        thread_ident=threading.get_ident(),
                        session_id=self.session_id,
                        session_incarnation=self.session_incarnation,
                        task_id=self.task_id,
                        actor=self.actor,
                    )
                    self._context_token = _ACTIVE_WORKSPACE_CAPABILITY.set(self._active_capability)
                    return True

                except (OSError, IOError) as lock_exc:
                    # Clean up failed handle
                    if self._file is not None and not self._file.closed:
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
                            f"Timed out after {effective_timeout:.2f}s waiting for lock release.",
                            workspace=self.workspace,
                            holder_session=holder_session,
                            holder_pid=holder_pid,
                        )
                    time.sleep(self.poll_interval)

    def release(self) -> None:
        """Release the workspace lock, safely decrementing nesting or performing outermost teardown."""
        with self._thread_lock:
            if self._depth <= 0:
                return

            # If nested through parent lock
            if self._parent_lock is not None:
                self._depth = 0
                parent = self._parent_lock
                self._parent_lock = None
                self._active_capability = None
                parent._decrement_depth()
                return

            # Root lock inner release
            self._depth -= 1
            if self._depth > 0:
                return

            # Outermost release:
            # 1. Revoke capability first so no further mutations can use it
            if self._active_capability is not None:
                self._active_capability.revoke()

            # 2. Reset ContextVar
            if self._context_token is not None:
                try:
                    _ACTIVE_WORKSPACE_CAPABILITY.reset(self._context_token)
                except Exception:
                    pass
                self._context_token = None

            # 3. Clean metadata while still holding the OS lock
            self._clear_meta()

            # 4. Release OS lock and close file handle
            if self._file is not None:
                try:
                    if self._has_os_lock and not self._file.closed:
                        fd = self._file.fileno()
                        if msvcrt is not None:
                            self._file.seek(0)
                            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                        elif _HAS_FCNTL:
                            getattr(fcntl, "flock")(fd, _LOCK_UN)
                except Exception as exc:
                    logger.warning(f"Error releasing OS lock on '{self.lock_file}': {exc}")
                finally:
                    self._has_os_lock = False
                    try:
                        self._file.close()
                    except Exception:
                        pass
                    self._file = None

            self._active_capability = None

    def __enter__(self) -> WorkspaceLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()
