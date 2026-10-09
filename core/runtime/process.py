"""Hardened Subprocess Runner — Bounded, Recoverable, and Fail-Closed Execution.

Provides canonical subprocess execution for autonomous coding:
- Mandatory bounded timeouts (default 120s, no None defaults).
- Complete process-tree termination on timeout or cancellation (Windows taskkill / POSIX killpg).
- Disconnected stdin (stdin=subprocess.DEVNULL) preventing interactive prompt deadlocks.
- Protected environment (CI=1, GIT_TERMINAL_PROMPT=0).
- Bounded output memory consumption (head/tail retention, max 5 MB per stream).
- Typed exit semantics distinguishing success, non-zero exit, timeout, cancellation, and truncation.
"""
from __future__ import annotations

import collections
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import contextvars
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Union

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    psutil = None  # type: ignore
    _HAS_PSUTIL = False

DEFAULT_SUBPROCESS_TIMEOUT: float = 120.0
DEFAULT_MAX_OUTPUT_BYTES: int = 5 * 1024 * 1024  # 5 MB per stream

_CURRENT_TASK_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("_current_task_id", default=None)
_CURRENT_CANCEL_EVENT: contextvars.ContextVar[Optional[threading.Event]] = contextvars.ContextVar("_current_cancel_event", default=None)

_ACTIVE_TASK_PROCESSES: Dict[str, Set[subprocess.Popen]] = collections.defaultdict(set)
_PROCESS_REGISTRY_LOCK = threading.Lock()


def register_active_process(task_id: str, proc: subprocess.Popen) -> None:
    """Register an active subprocess for a task."""
    with _PROCESS_REGISTRY_LOCK:
        _ACTIVE_TASK_PROCESSES[task_id].add(proc)


def unregister_active_process(task_id: str, proc: subprocess.Popen) -> None:
    """Unregister an active subprocess for a task."""
    with _PROCESS_REGISTRY_LOCK:
        _ACTIVE_TASK_PROCESSES[task_id].discard(proc)
        if not _ACTIVE_TASK_PROCESSES[task_id]:
            _ACTIVE_TASK_PROCESSES.pop(task_id, None)


def terminate_processes_for_task(task_id: str) -> int:
    """Terminate all active subprocesses registered under a task_id."""
    with _PROCESS_REGISTRY_LOCK:
        procs = list(_ACTIVE_TASK_PROCESSES.get(task_id, set()))
    count = 0
    for proc in procs:
        try:
            terminate_process_tree(proc)
            count += 1
        except Exception:
            pass
    return count



class ProcessExitStatus(str, Enum):
    """Canonical typed exit status for hardened subprocess execution."""

    SUCCESS = "success"
    NON_ZERO_EXIT = "non_zero_exit"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    EXECUTION_ERROR = "execution_error"


class HardenedProcessResult(subprocess.CompletedProcess):
    """Structured, typed outcome of a hardened subprocess execution."""

    def __init__(
        self,
        args: Any,
        returncode: int,
        stdout: str = "",
        stderr: str = "",
        *,
        is_timeout: bool = False,
        is_cancelled: bool = False,
        is_truncated: bool = False,
        error: Optional[str] = None,
        started_at: Optional[float] = None,
        completed_at: Optional[float] = None,
        duration_ms: Optional[int] = None,
    ) -> None:
        super().__init__(args, returncode, stdout, stderr)
        self.is_timeout = is_timeout
        self.is_cancelled = is_cancelled
        self.is_truncated = is_truncated
        self.error = error
        self.started_at = started_at
        self.completed_at = completed_at
        self.duration_ms = duration_ms

    @property
    def status(self) -> ProcessExitStatus:
        """Deterministic typed classification of the process exit."""
        if self.is_cancelled:
            return ProcessExitStatus.CANCELLED
        if self.is_timeout:
            return ProcessExitStatus.TIMEOUT
        if self.error and "Spawn Error" in self.error:
            return ProcessExitStatus.EXECUTION_ERROR
        if self.returncode == 0:
            return ProcessExitStatus.SUCCESS
        return ProcessExitStatus.NON_ZERO_EXIT

    @property
    def success(self) -> bool:
        return self.status == ProcessExitStatus.SUCCESS

    @property
    def is_success(self) -> bool:
        return self.success

    @property
    def is_non_zero_exit(self) -> bool:
        return self.status == ProcessExitStatus.NON_ZERO_EXIT

    @property
    def is_execution_error(self) -> bool:
        return self.status == ProcessExitStatus.EXECUTION_ERROR

    @property
    def is_error(self) -> bool:
        return self.error is not None

    def __repr__(self) -> str:
        flags = [self.status.value.upper()]
        if self.is_truncated:
            flags.append("TRUNCATED")
        flag_str = f" [{', '.join(flags)}]"
        return f"<HardenedProcessResult exit={self.returncode}{flag_str} len_stdout={len(self.stdout)} len_stderr={len(self.stderr)}>"


class _BoundedStreamCollector(threading.Thread):
    """Thread collecting stream bytes into a bounded buffer with head & tail retention."""

    def __init__(self, stream: Optional[Any], max_bytes: int):
        super().__init__(daemon=True)
        self.stream = stream
        self.max_bytes = max_bytes
        self.output_text: str = ""
        self.is_truncated: bool = False

    def run(self) -> None:
        if not self.stream:
            return

        head_budget = max(1024, self.max_bytes // 2)
        tail_budget = max(1024, self.max_bytes // 2)
        head_chunks: List[bytes] = []
        tail_chunks: collections.deque[bytes] = collections.deque()
        head_len = 0
        tail_len = 0
        total_len = 0

        try:
            while True:
                chunk = self.stream.read(65536)
                if not chunk:
                    break
                chunk_len = len(chunk)
                total_len += chunk_len

                if head_len < head_budget:
                    needed = head_budget - head_len
                    if chunk_len <= needed:
                        head_chunks.append(chunk)
                        head_len += chunk_len
                    else:
                        head_chunks.append(chunk[:needed])
                        head_len += needed
                        rem = chunk[needed:]
                        tail_chunks.append(rem)
                        tail_len += len(rem)
                else:
                    tail_chunks.append(chunk)
                    tail_len += chunk_len

                while tail_len > tail_budget and tail_chunks:
                    popped = tail_chunks.popleft()
                    tail_len -= len(popped)
        except Exception:
            pass

        if total_len > self.max_bytes:
            self.is_truncated = True
            head_bytes = b"".join(head_chunks)
            tail_bytes = b"".join(tail_chunks)
            head_str = head_bytes.decode("utf-8", errors="replace")
            tail_str = tail_bytes.decode("utf-8", errors="replace")
            self.output_text = (
                f"{head_str}\n\n"
                f"... [OUTPUT TRUNCATED: {total_len} bytes generated, capped at {self.max_bytes} bytes] ...\n\n"
                f"{tail_str}"
            )
        else:
            all_bytes = b"".join(head_chunks) + b"".join(tail_chunks)
            self.output_text = all_bytes.decode("utf-8", errors="replace")


def terminate_process_tree(proc: subprocess.Popen, job_handle: Optional[Any] = None) -> None:
    """Terminate the process and all its child/descendant processes.

    Terminates parent, children, and grandchildren across Windows and POSIX,
    drains/closes pipes, and ensures no lingering orphan processes remain.
    """
    pid = proc.pid

    # 1. Inspect and capture all descendant processes via psutil if available
    descendants: List[Any] = []
    if _HAS_PSUTIL and psutil is not None:
        try:
            parent = psutil.Process(pid)
            descendants = parent.children(recursive=True)
        except Exception:
            pass

    # 2. Windows Job Object termination (guarantees terminating descendants even if parent already exited)
    effective_job_handle = job_handle or getattr(proc, "_job_handle", None)
    if effective_job_handle is not None and sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.TerminateJobObject(effective_job_handle, 1)
            ctypes.windll.kernel32.CloseHandle(effective_job_handle)
        except Exception:
            pass

    # 3. Platform-specific process tree termination
    try:
        if sys.platform == "win32":
            # Forceful tree termination on Windows via taskkill /F /T
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                    timeout=5.0,
                )
            except Exception:
                pass
        else:
            # POSIX process-group kill
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGKILL)
            except Exception:
                pass
    except Exception:
        pass

    # 4. Explicitly kill discovered psutil descendants if any survived
    if descendants:
        for child in descendants:
            try:
                child.kill()
            except Exception:
                pass
        if _HAS_PSUTIL and psutil is not None:
            try:
                psutil.wait_procs(descendants, timeout=2.0)
            except Exception:
                pass

    # 5. Terminate and reap the direct parent process
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=2.0)
    except Exception:
        pass

    # 6. Close pipe streams to prevent handle leakage or hung pipe readers
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream:
            try:
                stream.close()
            except Exception:
                pass


def run_hardened_subprocess(
    cmd: Union[List[str], Sequence[str], str],
    cwd: Union[Path, str],
    *,
    timeout: float = DEFAULT_SUBPROCESS_TIMEOUT,
    env: Optional[Dict[str, str]] = None,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    allow_shell: Optional[bool] = None,
    cancel_event: Optional[threading.Event] = None,
    task_id: Optional[str] = None,
) -> HardenedProcessResult:
    """Run a subprocess with bounded timeouts, tree termination, and output limits.

    Fails-closed on timeouts, interactive prompts, and excessive output.
    Rejects timeout=None to enforce bounded autonomous execution.
    """
    if timeout is None or timeout <= 0:
        raise ValueError(
            f"Autonomous subprocess execution requires a finite, positive timeout; got timeout={timeout!r}"
        )
    effective_timeout = float(timeout)
    cwd_path = Path(cwd).resolve()

    effective_task_id = task_id or _CURRENT_TASK_ID.get()
    effective_cancel_event = cancel_event or _CURRENT_CANCEL_EVENT.get()

    # Environment protection: Prevent interactive prompts in automated runs
    merged_env = os.environ.copy()
    merged_env["CI"] = "1"
    merged_env["GIT_TERMINAL_PROMPT"] = "0"
    if env:
        merged_env.update(env)

    # Shell resolution: Default to shell=True on Windows if binary is a .cmd/.bat or list invocation
    use_shell = allow_shell if allow_shell is not None else (sys.platform == "win32")

    # Command array normalization
    effective_cmd = list(cmd) if isinstance(cmd, (list, tuple)) else cmd

    started_at = time.time()

    # Pre-execution validation
    is_empty_cmd = False
    if effective_cmd is None:
        is_empty_cmd = True
    elif isinstance(effective_cmd, (list, tuple)) and not effective_cmd:
        is_empty_cmd = True
    elif isinstance(effective_cmd, str) and not effective_cmd.strip():
        is_empty_cmd = True

    if is_empty_cmd:
        completed_at = time.time()
        return HardenedProcessResult(
            args=cmd,
            returncode=1,
            stdout="",
            stderr="Subprocess Error: Empty command provided",
            error="Empty command provided",
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=int((completed_at - started_at) * 1000),
        )

    popen_kwargs: Dict[str, Any] = {
        "cwd": str(cwd_path),
        "stdin": subprocess.DEVNULL,  # Prevent hanging on interactive prompts
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": merged_env,
        "shell": use_shell,
    }

    if sys.platform != "win32":
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(effective_cmd, **popen_kwargs)
    except Exception as exc:
        completed_at = time.time()
        return HardenedProcessResult(
            args=cmd,
            returncode=1,
            stdout="",
            stderr=f"Subprocess Spawn Error: {exc}",
            error=f"Subprocess Spawn Error: {exc}",
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=int((completed_at - started_at) * 1000),
        )

    if effective_task_id:
        register_active_process(effective_task_id, proc)

    # Windows Job Object attachment for leak-proof descendant containment
    job_handle: Optional[Any] = None
    if sys.platform == "win32":
        try:
            import ctypes
            import ctypes.wintypes as wt

            class _IO_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("ReadOp", ctypes.c_uint64),
                    ("WriteOp", ctypes.c_uint64),
                    ("OtherOp", ctypes.c_uint64),
                    ("ReadTr", ctypes.c_uint64),
                    ("WriteTr", ctypes.c_uint64),
                    ("OtherTr", ctypes.c_uint64),
                ]

            class _BASIC_LIMIT(ctypes.Structure):
                _fields_ = [
                    ("ProcTime", ctypes.c_int64),
                    ("JobTime", ctypes.c_int64),
                    ("LimitFlags", wt.DWORD),
                    ("MinWS", ctypes.c_size_t),
                    ("MaxWS", ctypes.c_size_t),
                    ("ActiveProc", wt.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("Priority", wt.DWORD),
                    ("Sched", wt.DWORD),
                ]

            class _EXT_LIMIT(ctypes.Structure):
                _fields_ = [
                    ("Basic", _BASIC_LIMIT),
                    ("Io", _IO_COUNTERS),
                    ("ProcMem", ctypes.c_size_t),
                    ("JobMem", ctypes.c_size_t),
                    ("PeakProcMem", ctypes.c_size_t),
                    ("PeakJobMem", ctypes.c_size_t),
                ]

            job_handle = ctypes.windll.kernel32.CreateJobObjectW(None, None)
            if job_handle:
                info = _EXT_LIMIT()
                info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                ctypes.windll.kernel32.SetInformationJobObject(job_handle, 9, ctypes.byref(info), ctypes.sizeof(info))
                raw_handle = getattr(proc, "_handle", None)
                if raw_handle is not None:
                    ctypes.windll.kernel32.AssignProcessToJobObject(job_handle, int(raw_handle))
                setattr(proc, "_job_handle", job_handle)
        except Exception:
            job_handle = None

    # Collect bounded streams in background threads
    stdout_collector = _BoundedStreamCollector(proc.stdout, max_bytes=max_output_bytes)
    stderr_collector = _BoundedStreamCollector(proc.stderr, max_bytes=max_output_bytes)
    stdout_collector.start()
    stderr_collector.start()

    deadline = started_at + effective_timeout
    poll_step = 0.1
    timed_out = False

    try:
        while True:
            if effective_cancel_event is not None and effective_cancel_event.is_set():
                terminate_process_tree(proc, job_handle=job_handle)
                stdout_collector.join(timeout=2.0)
                stderr_collector.join(timeout=2.0)
                raise KeyboardInterrupt("Subprocess cancelled by cancellation token.")

            time_left = deadline - time.time()
            if time_left <= 0:
                timed_out = True
                terminate_process_tree(proc, job_handle=job_handle)
                stdout_collector.join(timeout=2.0)
                stderr_collector.join(timeout=2.0)
                completed_at = time.time()
                return HardenedProcessResult(
                    args=cmd,
                    returncode=-1,
                    stdout=stdout_collector.output_text,
                    stderr=stderr_collector.output_text,
                    is_timeout=True,
                    is_truncated=stdout_collector.is_truncated or stderr_collector.is_truncated,
                    error=f"Process timed out after {effective_timeout:.1f}s",
                    started_at=started_at,
                    completed_at=completed_at,
                    duration_ms=int((completed_at - started_at) * 1000),
                )

            try:
                proc.wait(timeout=min(poll_step, max(0.01, time_left)))
                break
            except subprocess.TimeoutExpired:
                continue

    except (KeyboardInterrupt, BaseException):
        terminate_process_tree(proc, job_handle=job_handle)
        stdout_collector.join(timeout=2.0)
        stderr_collector.join(timeout=2.0)
        raise
    finally:
        if effective_task_id:
            unregister_active_process(effective_task_id, proc)
        if job_handle is not None and sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.kernel32.CloseHandle(job_handle)
            except Exception:
                pass


    stdout_collector.join(timeout=5.0)
    stderr_collector.join(timeout=5.0)

    exit_code = proc.returncode if proc.returncode is not None else 0
    err_msg = None if exit_code == 0 else f"Process exited with code {exit_code}"
    completed_at = time.time()
    duration_ms = int((completed_at - started_at) * 1000)

    return HardenedProcessResult(
        args=cmd,
        returncode=exit_code,
        stdout=stdout_collector.output_text,
        stderr=stderr_collector.output_text,
        is_timeout=False,
        is_truncated=stdout_collector.is_truncated or stderr_collector.is_truncated,
        error=err_msg,
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=duration_ms,
    )
