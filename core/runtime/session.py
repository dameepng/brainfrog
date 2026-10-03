"""Isolated and persistent session management for BrainFrog.

Guarantees that each channel, user, and conversation has a strictly isolated
conversation state, preventing cross-tenant or cross-user memory leakage.
Provides filesystem-backed atomic persistence in .brainfrog/sessions/
ensuring sessions survive process restarts.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

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

CURRENT_SESSION_SCHEMA_VERSION = 1


class StaleSessionStateError(RuntimeError):
    """Raised when an attempt to persist session state conflicts with concurrent state changes or reset."""

    pass


# Production History Policy Defaults (L-01 Hard Bounds)
DEFAULT_MAX_HISTORY_ENTRIES = 50
DEFAULT_MAX_HISTORY_BYTES = 256 * 1024  # 256 KB
DEFAULT_MAX_MESSAGE_BYTES = 32 * 1024   # 32 KB
DEFAULT_SESSION_TTL_SECONDS = 7 * 86400.0  # 7 days


def get_max_history_entries() -> int:
    """Return configured maximum session history entries (FIFO eviction)."""
    try:
        val = int(os.environ.get("BRAINFROG_MAX_HISTORY_ENTRIES", DEFAULT_MAX_HISTORY_ENTRIES))
        return max(1, val)
    except (ValueError, TypeError):
        return DEFAULT_MAX_HISTORY_ENTRIES


def get_max_history_bytes() -> int:
    """Return configured maximum serialized history bytes."""
    try:
        val = int(os.environ.get("BRAINFROG_MAX_HISTORY_BYTES", DEFAULT_MAX_HISTORY_BYTES))
        return max(1024, val)
    except (ValueError, TypeError):
        return DEFAULT_MAX_HISTORY_BYTES


def get_max_message_bytes() -> int:
    """Return configured maximum size in bytes for a single history message."""
    try:
        val = int(os.environ.get("BRAINFROG_MAX_MESSAGE_BYTES", DEFAULT_MAX_MESSAGE_BYTES))
        return max(256, val)
    except (ValueError, TypeError):
        return DEFAULT_MAX_MESSAGE_BYTES


def get_session_ttl_seconds() -> float:
    """Return configured session TTL in seconds."""
    try:
        val = float(os.environ.get("BRAINFROG_SESSION_TTL_SECONDS", DEFAULT_SESSION_TTL_SECONDS))
        return max(0.0, val)
    except (ValueError, TypeError):
        return float(DEFAULT_SESSION_TTL_SECONDS)


def bound_message_text(text: str, max_bytes: int) -> str:
    """Deterministically clamp message text to at most max_bytes on a clean UTF-8 boundary.

    Preserves valid Unicode code points and never partially cuts multi-byte characters.
    """
    if not text:
        return text
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    suffix = " ... [TRUNCATED]"
    suffix_bytes = suffix.encode("utf-8")
    if max_bytes <= len(suffix_bytes):
        return encoded[:max_bytes].decode("utf-8", errors="ignore")
    slice_len = max_bytes - len(suffix_bytes)
    truncated = encoded[:slice_len].decode("utf-8", errors="ignore")
    return truncated + suffix


class _CrossProcessLock:
    """OS-level advisory file lock for cross-process and cross-thread synchronization."""

    def __init__(
        self,
        lock_path: Path,
        timeout: float = 10.0,
        poll_interval: float = 0.01,
    ) -> None:
        self.lock_path = lock_path
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._thread_lock = threading.RLock()
        self._owner_thread: Optional[int] = None
        self._depth: int = 0
        self._file: Optional[Any] = None

    def acquire(self) -> bool:
        cur_thread = threading.get_ident()
        if self._owner_thread == cur_thread:
            self._depth += 1
            return True

        start_time = time.monotonic()
        acquired = self._thread_lock.acquire(timeout=self.timeout)
        if not acquired:
            raise TimeoutError(f"Timed out after {self.timeout}s waiting for thread lock on {self.lock_path.name}")

        try:
            if self._file is None or self._file.closed:
                self.lock_path.parent.mkdir(parents=True, exist_ok=True)
                self._file = open(self.lock_path, "a+b")
            fd = self._file.fileno()

            while True:
                try:
                    if msvcrt is not None:
                        self._file.seek(0)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    elif _HAS_FCNTL:
                        getattr(fcntl, "flock")(fd, _LOCK_EX | _LOCK_NB)
                    break
                except (OSError, IOError):
                    if (time.monotonic() - start_time) >= self.timeout:
                        raise TimeoutError(f"Timed out after {self.timeout}s waiting for lock on {self.lock_path.name}")
                    time.sleep(self.poll_interval)

            self._owner_thread = cur_thread
            self._depth = 1
            return True
        except Exception:
            self._thread_lock.release()
            raise

    def release(self) -> None:
        cur_thread = threading.get_ident()
        if self._owner_thread != cur_thread:
            return

        self._depth -= 1
        if self._depth > 0:
            return

        self._owner_thread = None
        try:
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
        finally:
            self._thread_lock.release()

    def close(self) -> None:
        """Close open lock file handle if any."""
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None

    def __del__(self) -> None:
        self.close()

    def __enter__(self) -> _CrossProcessLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()


_SESSION_LOCKS: Dict[str, _CrossProcessLock] = {}
_SESSION_LOCKS_GUARD = threading.Lock()


def _get_cross_process_session_lock(lock_path: Path, timeout: float = 10.0) -> _CrossProcessLock:
    """Get or create process-singleton _CrossProcessLock for the given canonical lock path."""
    resolved_key = str(lock_path.resolve())
    with _SESSION_LOCKS_GUARD:
        if resolved_key not in _SESSION_LOCKS:
            _SESSION_LOCKS[resolved_key] = _CrossProcessLock(lock_path, timeout=timeout)
        return _SESSION_LOCKS[resolved_key]


def scrub_secrets(text: str) -> str:
    """Scrub sensitive keys, tokens, and credentials from text."""
    if not text:
        return text
    secret_env_vars = [
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN",
        "TYPESAFE_API_KEY", "WHATSAPP_API_TOKEN", "CUSTOM_API_KEY",
        "OPENROUTER_API_KEY", "GH_TOKEN", "GITHUB_TOKEN",
    ]
    scrubbed = text
    for var in secret_env_vars:
        val = os.environ.get(var)
        if val and len(val) >= 6:
            scrubbed = scrubbed.replace(val, f"[{var}_REDACTED]")

    scrubbed = re.sub(r"sk-(?:proj-)?[a-zA-Z0-9_\-]{20,}", "[REDACTED_API_KEY]", scrubbed)
    scrubbed = re.sub(r"\b\d{8,11}:[A-Za-z0-9_-]{30,40}\b", "[REDACTED_BOT_TOKEN]", scrubbed)
    scrubbed = re.sub(r"(?i)\bBearer\s+[a-zA-Z0-9_\-\.]{8,}\b", "Bearer [REDACTED_TOKEN]", scrubbed)
    scrubbed = re.sub(r"(?:ghp|gho|ghu|ghs|ghr)_[a-zA-Z0-9]{20,}|github_pat_[a-zA-Z0-9_]{30,}", "[REDACTED_TOKEN]", scrubbed)
    return scrubbed


@dataclass
class SessionState:
    """State scoped exclusively to a single channel:user:conversation session."""

    session_id: str
    channel: str
    user_id: str
    conversation_id: str
    session_incarnation_id: str = field(default_factory=lambda: secrets.token_hex(16))
    revision: int = 1
    created_at: float = field(default_factory=time.time)
    last_active_at: float = field(default_factory=time.time)
    active_mode: str = "build"
    active_skill: Optional[str] = None
    active_model: Optional[str] = None
    active_provider: Optional[str] = None
    plan_context: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    history: List[Dict[str, Any]] = field(default_factory=list)
    max_history_entries: int = field(default_factory=get_max_history_entries)
    max_history_bytes: int = field(default_factory=get_max_history_bytes)
    max_message_bytes: int = field(default_factory=get_max_message_bytes)
    _persisted_revision: Optional[int] = field(default=None, repr=False, compare=False)
    _persisted_incarnation: Optional[str] = field(default=None, repr=False, compare=False)

    def _enforce_history_bounds(self) -> None:
        """Enforce hard upper bounds on history length and total serialized bytes (FIFO eviction)."""
        # 1. Enforce entry count bound
        while len(self.history) > self.max_history_entries:
            self.history.pop(0)

        if not self.history:
            return

        # 2. Enforce byte bound
        # Fast path check: if sum of user + assistant string lengths is safely below limit, skip serialization
        approx_chars = sum(len(e.get("user", "")) + len(e.get("assistant", "")) for e in self.history)
        if approx_chars * 2 < self.max_history_bytes:
            return

        def _history_bytes() -> int:
            return len(json.dumps(self.history, ensure_ascii=False).encode("utf-8"))

        while len(self.history) > 1 and _history_bytes() > self.max_history_bytes:
            self.history.pop(0)

        # Single entry still exceeds byte budget: clamp its fields
        if len(self.history) == 1 and _history_bytes() > self.max_history_bytes:
            single = self.history[0]
            per_field_limit = max(256, self.max_history_bytes // 3)
            single["user"] = bound_message_text(str(single.get("user", "")), per_field_limit)
            single["assistant"] = bound_message_text(str(single.get("assistant", "")), per_field_limit)

    def record_interaction(
        self,
        user_text: str,
        assistant_text: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.last_active_at = time.time()
        bounded_user = bound_message_text(user_text, self.max_message_bytes)
        bounded_asst = bound_message_text(assistant_text, self.max_message_bytes)
        clean_meta = {}
        if metadata:
            for k, v in metadata.items():
                if isinstance(v, str):
                    clean_meta[k] = bound_message_text(v, self.max_message_bytes)
                else:
                    clean_meta[k] = v
        self.history.append({
            "timestamp": self.last_active_at,
            "user": bounded_user,
            "assistant": bounded_asst,
            "metadata": clean_meta,
        })
        self._enforce_history_bounds()

    def add_message(
        self,
        role: str = "user",
        content: str = "",
        text: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        """Add a role-based message turn to history with automatic bounding."""
        body = text if text is not None else content
        if role == "user":
            self.record_interaction(user_text=body, assistant_text="", metadata=metadata)
        elif role == "assistant":
            if self.history and not self.history[-1].get("assistant"):
                self.history[-1]["assistant"] = bound_message_text(body, self.max_message_bytes)
                if metadata:
                    self.history[-1].setdefault("metadata", {}).update(metadata)
                self._enforce_history_bounds()
            else:
                self.record_interaction(user_text="", assistant_text=body, metadata=metadata)
        else:
            self.record_interaction(user_text=f"[{role}] {body}", assistant_text="", metadata=metadata)

    def reset(self) -> None:
        """Clear conversation history and ephemeral context and allocate a new incarnation."""
        self.history.clear()
        self.plan_context = None
        self.metadata.clear()
        self.last_active_at = time.time()
        self.session_incarnation_id = secrets.token_hex(16)
        self.revision = 1
        self._persisted_revision = None
        self._persisted_incarnation = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert session state to JSON-serializable dictionary with schema version and policy bounds."""
        self._enforce_history_bounds()
        return {
            "schema_version": CURRENT_SESSION_SCHEMA_VERSION,
            "session_id": self.session_id,
            "session_incarnation_id": self.session_incarnation_id,
            "revision": self.revision,
            "channel": self.channel,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "created_at": self.created_at,
            "last_active_at": self.last_active_at,
            "active_mode": self.active_mode,
            "active_skill": self.active_skill,
            "active_model": self.active_model,
            "active_provider": self.active_provider,
            "plan_context": self.plan_context,
            "metadata": dict(self.metadata),
            "history": list(self.history),
            "max_history_entries": self.max_history_entries,
            "max_history_bytes": self.max_history_bytes,
            "max_message_bytes": self.max_message_bytes,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SessionState:
        """Reconstruct SessionState from dictionary with schema validation and legacy history normalization."""
        version = data.get("schema_version")
        if version is None or not isinstance(version, (int, float)):
            raise ValueError("Missing or invalid 'schema_version' in session payload.")
        if version > CURRENT_SESSION_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported session schema version: {version}. "
                f"Current supported version is {CURRENT_SESSION_SCHEMA_VERSION}."
            )
        if "session_id" not in data:
            raise ValueError("Missing required 'session_id' in session payload.")

        incarnation = data.get("session_incarnation_id")
        if not incarnation or not str(incarnation).strip():
            incarnation = secrets.token_hex(16)

        try:
            revision = int(data.get("revision", 1))
        except (ValueError, TypeError):
            revision = 1

        max_entries = int(data.get("max_history_entries") or get_max_history_entries())
        max_bytes = int(data.get("max_history_bytes") or get_max_history_bytes())
        max_msg_bytes = int(data.get("max_message_bytes") or get_max_message_bytes())

        raw_history = data.get("history", [])
        if not isinstance(raw_history, list):
            raw_history = []

        # Legacy normalization: truncate to newest entries
        if len(raw_history) > max_entries:
            raw_history = raw_history[-max_entries:]

        normalized_history: List[Dict[str, Any]] = []
        for entry in raw_history:
            if isinstance(entry, dict):
                norm_entry = dict(entry)
                norm_entry["user"] = bound_message_text(str(norm_entry.get("user", "")), max_msg_bytes)
                norm_entry["assistant"] = bound_message_text(str(norm_entry.get("assistant", "")), max_msg_bytes)
                normalized_history.append(norm_entry)

        session = cls(
            session_id=str(data["session_id"]),
            session_incarnation_id=str(incarnation),
            revision=revision,
            channel=str(data.get("channel", "cli")),
            user_id=str(data.get("user_id", "local")),
            conversation_id=str(data.get("conversation_id", "default")),
            created_at=float(data.get("created_at", time.time())),
            last_active_at=float(data.get("last_active_at", time.time())),
            active_mode=str(data.get("active_mode", "build")),
            active_skill=data.get("active_skill"),
            active_model=data.get("active_model"),
            active_provider=data.get("active_provider"),
            plan_context=data.get("plan_context"),
            metadata=dict(data.get("metadata", {})),
            history=normalized_history,
            max_history_entries=max_entries,
            max_history_bytes=max_bytes,
            max_message_bytes=max_msg_bytes,
            _persisted_revision=revision,
            _persisted_incarnation=str(incarnation),
        )
        session._enforce_history_bounds()
        return session


class SessionStore(ABC):
    """Abstract storage backend for session state persistence."""

    @abstractmethod
    def load(self, session_id: str) -> Optional[SessionState]:
        """Load session state by session_id."""
        raise NotImplementedError

    @abstractmethod
    def save(self, session: SessionState, force: bool = False) -> bool:
        """Atomically persist a session state."""
        raise NotImplementedError

    @abstractmethod
    def delete(self, session_id: str) -> bool:
        """Delete persisted session state."""
        raise NotImplementedError

    def reset(self, session_id: str, new_incarnation: Optional[str] = None) -> bool:
        """Reset session storage, establishing an authoritative post-reset boundary."""
        return self.delete(session_id)

    @abstractmethod
    def list_sessions(self) -> List[str]:
        """List all persisted session IDs."""
        raise NotImplementedError

    @abstractmethod
    def cleanup_stale_sessions(self, max_age_seconds: float, limit: int = 100) -> int:
        """Clean up inactive sessions older than max_age_seconds."""
        raise NotImplementedError


class InMemorySessionStore(SessionStore):
    """Ephemeral in-memory session store."""

    def __init__(self) -> None:
        self._storage: Dict[str, SessionState] = {}
        self._reset_incarnations: Dict[str, str] = {}
        self._lock = threading.RLock()

    def load(self, session_id: str) -> Optional[SessionState]:
        with self._lock:
            cached = self._storage.get(session_id)
            if cached is None:
                return None
            return SessionState.from_dict(cached.to_dict())

    def save(self, session: SessionState, force: bool = False) -> bool:
        with self._lock:
            session._enforce_history_bounds()
            if not force:
                # 1. Reset boundary verification (F02 Guard)
                if session.session_id in self._reset_incarnations:
                    active_inc = self._reset_incarnations[session.session_id]
                    if active_inc != session.session_incarnation_id:
                        raise StaleSessionStateError(
                            f"Stale session incarnation: session '{session.session_id}' was reset. "
                            f"Authoritative incarnation is '{active_inc}', attempted save with '{session.session_incarnation_id}'."
                        )

                # 2. Existing session guard (F01 & F02 Guard)
                if session.session_id in self._storage:
                    current = self._storage[session.session_id]
                    if current.session_incarnation_id != session.session_incarnation_id:
                        raise StaleSessionStateError(
                            f"Stale session incarnation: current incarnation '{current.session_incarnation_id}' != '{session.session_incarnation_id}'"
                        )
                    expected_rev = session._persisted_revision if session._persisted_revision is not None else session.revision
                    if current.revision != expected_rev:
                        raise StaleSessionStateError(
                            f"Stale session revision: current revision {current.revision} != expected {expected_rev}"
                        )
                    session.revision = current.revision + 1
                else:
                    if session._persisted_incarnation is not None:
                        raise StaleSessionStateError(
                            f"Session '{session.session_id}' was deleted or reset while request was in flight."
                        )
                    if session.revision <= 0:
                        session.revision = 1

            self._storage[session.session_id] = session
            session._persisted_revision = session.revision
            session._persisted_incarnation = session.session_incarnation_id
            self._reset_incarnations.pop(session.session_id, None)
            return True

    def reset(self, session_id: str, new_incarnation: Optional[str] = None) -> bool:
        with self._lock:
            self._storage.pop(session_id, None)
            inc = new_incarnation or secrets.token_hex(16)
            self._reset_incarnations[session_id] = inc
            return True

    def delete(self, session_id: str) -> bool:
        with self._lock:
            self._reset_incarnations.pop(session_id, None)
            return bool(self._storage.pop(session_id, None))

    def list_sessions(self) -> List[str]:
        with self._lock:
            return list(self._storage.keys())

    def cleanup_stale_sessions(self, max_age_seconds: float, limit: int = 100) -> int:
        with self._lock:
            if max_age_seconds <= 0:
                return 0
            now = time.time()
            stale_keys = [
                k for k, s in self._storage.items()
                if (now - s.last_active_at) > max_age_seconds
            ][:limit]
            for k in stale_keys:
                self._storage.pop(k, None)
            return len(stale_keys)


class FileSessionStore(SessionStore):
    """Filesystem-backed atomic, corruption-tolerant, bounded session store in .brainfrog/sessions/."""

    def __init__(
        self,
        repo_dir: Optional[Path | str] = None,
        sessions_dir: Optional[Path | str] = None,
    ) -> None:
        if sessions_dir is not None:
            self.sessions_dir = Path(sessions_dir).resolve()
        else:
            base = Path(repo_dir or Path.cwd()).resolve()
            self.sessions_dir = (base / ".brainfrog" / "sessions").resolve()

        self.corrupt_dir = (self.sessions_dir / "corrupt").resolve()
        self.locks_dir = (self.sessions_dir / ".locks").resolve()
        self._lock = threading.RLock()
        self._ensure_dirs()
        self._clean_stale_tmp_files()

    def _ensure_dirs(self) -> None:
        try:
            self.sessions_dir.mkdir(parents=True, exist_ok=True)
            self.corrupt_dir.mkdir(parents=True, exist_ok=True)
            self.locks_dir.mkdir(parents=True, exist_ok=True)
            if hasattr(os, "chmod") and os.name != "nt":
                os.chmod(self.sessions_dir, 0o700)
                os.chmod(self.locks_dir, 0o700)
        except Exception as e:
            logger.warning(f"Could not create session directory '{self.sessions_dir}': {e}")

    def _clean_stale_tmp_files(self) -> None:
        """Remove abandoned temporary files older than 60 seconds."""
        try:
            if not self.sessions_dir.exists():
                return
            now = time.time()
            for f in self.sessions_dir.glob(".tmp_*.json"):
                try:
                    if (now - f.stat().st_mtime) > 60:
                        f.unlink(missing_ok=True)
                except OSError:
                    pass
        except Exception:
            pass

    def _get_session_path(self, session_id: str) -> Path:
        """Derive deterministic, traversal-safe fixed filename from session_id."""
        safe_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
        target = (self.sessions_dir / f"{safe_hash}.json").resolve()
        # Security invariant: assert path remains strictly inside sessions_dir
        if not str(target).startswith(str(self.sessions_dir)):
            raise PermissionError(f"Path traversal detected in session_id: {session_id}")
        return target

    def _get_lock_path(self, session_id: str) -> Path:
        """Derive safe, traversal-free lockfile path for cross-process synchronization."""
        safe_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
        target = (self.locks_dir / f"{safe_hash}.lock").resolve()
        if not str(target).startswith(str(self.sessions_dir)):
            raise PermissionError(f"Path traversal detected in session lock: {session_id}")
        return target

    def _get_reset_meta_path(self, session_id: str) -> Path:
        """Derive safe path for session reset metadata/tombstone."""
        safe_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
        target = (self.locks_dir / f"{safe_hash}.reset").resolve()
        if not str(target).startswith(str(self.sessions_dir)):
            raise PermissionError(f"Path traversal detected in session reset meta: {session_id}")
        return target

    def _get_session_lock(self, session_id: str, timeout: float = 10.0) -> _CrossProcessLock:
        return _get_cross_process_session_lock(self._get_lock_path(session_id), timeout=timeout)

    def _quarantine(self, file_path: Path, reason: str) -> None:
        """Safely isolate corrupted session file to corrupt/ without crashing runtime."""
        try:
            self.corrupt_dir.mkdir(parents=True, exist_ok=True)
            quarantine_name = f"{file_path.stem}_{int(time.time())}_{reason}.corrupt"
            quarantine_path = self.corrupt_dir / quarantine_name
            if file_path.exists():
                os.replace(file_path, quarantine_path)
                logger.warning(
                    f"Quarantined corrupted session file '{file_path.name}' to '{quarantine_name}' (reason: {reason})"
                )
        except Exception as e:
            logger.warning(f"Failed to quarantine corrupted file '{file_path}': {e}")

    def reset(self, session_id: str, new_incarnation: Optional[str] = None) -> bool:
        """Reset persisted session, establishing an authoritative post-reset boundary."""
        with self._lock:
            lock = self._get_session_lock(session_id)
            with lock:
                try:
                    target_file = self._get_session_path(session_id)
                    if target_file.exists():
                        target_file.unlink()

                    inc = new_incarnation or secrets.token_hex(16)
                    meta_path = self._get_reset_meta_path(session_id)
                    meta_payload = {
                        "session_id": session_id,
                        "current_incarnation": inc,
                        "reset_at": time.time(),
                        "revision": 1,
                    }
                    safe_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
                    meta_temp = self.locks_dir / f".tmp_reset_{safe_hash}_{uuid.uuid4().hex[:8]}.json"
                    meta_temp.write_text(json.dumps(meta_payload), encoding="utf-8")
                    os.replace(meta_temp, meta_path)
                    return True
                except Exception as e:
                    logger.warning(f"Failed to reset session storage for '{session_id}': {e}")
                    return False

    def save(self, session: SessionState, force: bool = False) -> bool:
        """Atomically persist session state with OCC freshness verification, credential scrubbing, fsync, and process lock."""
        with self._lock:
            lock = self._get_session_lock(session.session_id)
            with lock:
                try:
                    if not self.sessions_dir.exists():
                        self._ensure_dirs()
                    session._enforce_history_bounds()
                    target_file = self._get_session_path(session.session_id)
                    meta_path = self._get_reset_meta_path(session.session_id)

                    if not force:
                        # 1. Reset boundary verification (F02 Guard)
                        if meta_path.exists():
                            try:
                                meta_content = json.loads(meta_path.read_text(encoding="utf-8"))
                                active_inc = meta_content.get("current_incarnation")
                                if active_inc and active_inc != session.session_incarnation_id:
                                    raise StaleSessionStateError(
                                        f"Stale session incarnation: session '{session.session_id}' was reset. "
                                        f"Authoritative incarnation is '{active_inc}', attempted save with '{session.session_incarnation_id}'."
                                    )
                            except json.JSONDecodeError:
                                pass

                        # 2. Disk freshness and optimistic concurrency verification (F01 & F02 Guard)
                        if target_file.exists() and target_file.is_file():
                            try:
                                disk_content = target_file.read_text(encoding="utf-8")
                                if disk_content.strip():
                                    disk_data = json.loads(disk_content)
                                    if isinstance(disk_data, dict):
                                        disk_inc = str(disk_data.get("session_incarnation_id", ""))
                                        if disk_inc and disk_inc != session.session_incarnation_id:
                                            raise StaleSessionStateError(
                                                f"Stale session incarnation: current disk incarnation '{disk_inc}' != '{session.session_incarnation_id}'"
                                            )
                                        disk_rev = int(disk_data.get("revision", 1))
                                        expected_rev = session._persisted_revision if session._persisted_revision is not None else session.revision
                                        if disk_rev != expected_rev:
                                            raise StaleSessionStateError(
                                                f"Stale session revision: current disk revision {disk_rev} != expected {expected_rev}"
                                            )
                                        # Monotonically advance revision
                                        session.revision = disk_rev + 1
                            except (json.JSONDecodeError, ValueError) as e:
                                if isinstance(e, StaleSessionStateError):
                                    raise
                                self._quarantine(target_file, "corrupt_on_save")
                                session.revision = (session.revision or 1) + 1
                        else:
                            # Target file does not exist on disk
                            if session._persisted_incarnation is not None:
                                # Session was previously persisted, but is now missing (reset or deleted)
                                raise StaleSessionStateError(
                                    f"Session '{session.session_id}' was reset or deleted while request was in flight."
                                )
                            if session.revision <= 0:
                                session.revision = 1

                    data = session.to_dict()
                    data["revision"] = session.revision

                    # Security invariant: Scrub credentials from persisted history and metadata
                    clean_history = []
                    for entry in data.get("history", []):
                        clean_history.append({
                            "timestamp": entry.get("timestamp"),
                            "user": scrub_secrets(str(entry.get("user", ""))),
                            "assistant": scrub_secrets(str(entry.get("assistant", ""))),
                            "metadata": {
                                k: scrub_secrets(str(v)) if isinstance(v, str) else v
                                for k, v in entry.get("metadata", {}).items()
                            },
                        })
                    data["history"] = clean_history

                    json_content = json.dumps(data, indent=2, ensure_ascii=False)
                    temp_file = self.sessions_dir / f".tmp_{target_file.stem}_{uuid.uuid4().hex[:8]}.json"

                    with open(temp_file, "w", encoding="utf-8") as f:
                        f.write(json_content)
                        f.flush()
                        try:
                            os.fsync(f.fileno())
                        except Exception:
                            pass

                    if hasattr(os, "chmod") and os.name != "nt":
                        try:
                            os.chmod(temp_file, 0o600)
                        except Exception:
                            pass

                    os.replace(temp_file, target_file)

                    # Update internal tracking
                    session._persisted_revision = session.revision
                    session._persisted_incarnation = session.session_incarnation_id

                    # Clean up reset tombstone once new state is committed to disk
                    if meta_path.exists():
                        try:
                            meta_path.unlink()
                        except Exception:
                            pass

                    return True
                except StaleSessionStateError:
                    raise
                except Exception as e:
                    logger.warning(f"Failed to persist session '{session.session_id}': {e}")
                    try:
                        if "temp_file" in locals() and temp_file.exists():
                            temp_file.unlink()
                    except Exception:
                        pass
                    return False

    def load(self, session_id: str) -> Optional[SessionState]:
        """Load and deserialize session state with comprehensive corruption recovery."""
        with self._lock:
            lock = self._get_session_lock(session_id)
            with lock:
                try:
                    target_file = self._get_session_path(session_id)
                    if not target_file.exists() or not target_file.is_file():
                        return None

                    content = target_file.read_text(encoding="utf-8")
                    if not content.strip():
                        self._quarantine(target_file, "empty_file")
                        return None

                    data = json.loads(content)
                    if not isinstance(data, dict):
                        self._quarantine(target_file, "non_dict_json")
                        return None

                    session = SessionState.from_dict(data)
                    session._persisted_revision = session.revision
                    session._persisted_incarnation = session.session_incarnation_id

                    # Auto-compact legacy oversized files immediately upon load
                    if len(data.get("history", [])) > session.max_history_entries:
                        try:
                            data_compact = session.to_dict()
                            data_compact["revision"] = session.revision
                            json_content = json.dumps(data_compact, indent=2, ensure_ascii=False)
                            temp_file = self.sessions_dir / f".tmp_{target_file.stem}_{uuid.uuid4().hex[:8]}.json"
                            with open(temp_file, "w", encoding="utf-8") as f:
                                f.write(json_content)
                                f.flush()
                                try:
                                    os.fsync(f.fileno())
                                except Exception:
                                    pass
                            os.replace(temp_file, target_file)
                        except Exception as e:
                            logger.warning(f"Failed to auto-compact legacy session '{session_id}': {e}")
                    return session
                except json.JSONDecodeError:
                    target_file = self._get_session_path(session_id)
                    self._quarantine(target_file, "invalid_json")
                    return None
                except ValueError as ve:
                    target_file = self._get_session_path(session_id)
                    self._quarantine(target_file, "invalid_schema")
                    logger.warning(f"Session '{session_id}' has invalid schema: {ve}")
                    return None
                except Exception as e:
                    logger.warning(f"Unexpected error loading session '{session_id}': {e}")
                    return None

    def delete(self, session_id: str) -> bool:
        """Remove persisted session file and any reset markers from disk."""
        with self._lock:
            lock = self._get_session_lock(session_id)
            with lock:
                try:
                    target_file = self._get_session_path(session_id)
                    deleted = False
                    if target_file.exists():
                        target_file.unlink()
                        deleted = True
                    meta_path = self._get_reset_meta_path(session_id)
                    if meta_path.exists():
                        meta_path.unlink()
                    return deleted
                except Exception as e:
                    logger.warning(f"Failed to delete session file for '{session_id}': {e}")
                    return False

    def list_sessions(self) -> List[str]:
        """Return list of all valid persisted session IDs."""
        with self._lock:
            sessions = []
            if not self.sessions_dir.exists():
                return sessions
            for f in sorted(self.sessions_dir.glob("*.json")):
                if f.name.startswith(".tmp_"):
                    continue
                try:
                    content = f.read_text(encoding="utf-8")
                    data = json.loads(content)
                    if isinstance(data, dict) and "session_id" in data:
                        sessions.append(str(data["session_id"]))
                except Exception:
                    pass
            return sessions

    def cleanup_stale_sessions(self, max_age_seconds: float, limit: int = 100) -> int:
        """Bounded cleanup helper for sessions inactive longer than max_age_seconds."""
        if max_age_seconds <= 0:
            return 0
        with self._lock:
            cleaned = 0
            now = time.time()
            if not self.sessions_dir.exists():
                return 0
            for f in sorted(self.sessions_dir.glob("*.json")):
                if f.name.startswith(".tmp_"):
                    continue
                try:
                    mtime = f.stat().st_mtime
                    if (now - mtime) < max_age_seconds:
                        continue
                    content = f.read_text(encoding="utf-8")
                    data = json.loads(content)
                    last_act = float(data.get("last_active_at", mtime))
                    if (now - last_act) > max_age_seconds:
                        session_id = str(data.get("session_id", ""))
                        if session_id:
                            try:
                                lock = self._get_session_lock(session_id, timeout=1.0)
                                with lock:
                                    f.unlink(missing_ok=True)
                                    cleaned += 1
                            except TimeoutError:
                                # Active lock held; skip
                                continue
                        else:
                            f.unlink(missing_ok=True)
                            cleaned += 1
                        if cleaned >= limit:
                            break
                except Exception:
                    pass
            return cleaned


class SessionManager:
    """Thread-safe session manager with isolated state and pluggable persistence."""

    def __init__(
        self,
        store: Optional[SessionStore] = None,
        max_history_entries: Optional[int] = None,
        max_history_bytes: Optional[int] = None,
        max_message_bytes: Optional[int] = None,
        session_ttl_seconds: Optional[float] = None,
    ) -> None:
        self.store = store
        self._sessions: Dict[str, SessionState] = {}
        self._lock = threading.RLock()
        self.max_history_entries = max_history_entries or get_max_history_entries()
        self.max_history_bytes = max_history_bytes or get_max_history_bytes()
        self.max_message_bytes = max_message_bytes or get_max_message_bytes()
        self.session_ttl_seconds = session_ttl_seconds if session_ttl_seconds is not None else get_session_ttl_seconds()
        self._last_cleanup_time: float = 0.0

    def _maybe_cleanup_stale(self) -> None:
        """Run a throttled, bounded cleanup pass if session TTL is configured."""
        if self.store is None or self.session_ttl_seconds <= 0:
            return
        now = time.time()
        if (now - self._last_cleanup_time) < 3600.0:
            return
        self._last_cleanup_time = now
        try:
            self.store.cleanup_stale_sessions(self.session_ttl_seconds, limit=50)
        except Exception:
            pass

    def get_or_create(
        self,
        channel: str,
        user_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        default_mode: str = "build",
    ) -> SessionState:
        if user_id is None and conversation_id is None and ":" in channel:
            parts = channel.split(":", 2)
            if len(parts) == 3:
                c, u, conv = parts[0], parts[1], parts[2]
            elif len(parts) == 2:
                c, u, conv = parts[0], parts[1], "default"
            else:
                c, u, conv = channel, "local", "default"
        else:
            c = channel
            u = user_id or "default"
            conv = conversation_id or "default"

        session_id = f"{c}:{u}:{conv}"

        with self._lock:
            self._maybe_cleanup_stale()

            if session_id in self._sessions:
                return self._sessions[session_id]

            if self.store is not None:
                loaded = self.store.load(session_id)
                if loaded is not None:
                    self._sessions[session_id] = loaded
                    return loaded

            session = SessionState(
                session_id=session_id,
                channel=c,
                user_id=u,
                conversation_id=conv,
                active_mode=default_mode,
                max_history_entries=self.max_history_entries,
                max_history_bytes=self.max_history_bytes,
                max_message_bytes=self.max_message_bytes,
            )
            # Adopt active reset incarnation if present in store
            if isinstance(self.store, FileSessionStore):
                meta_path = self.store._get_reset_meta_path(session_id)
                if meta_path.exists():
                    try:
                        meta_data = json.loads(meta_path.read_text(encoding="utf-8"))
                        if meta_data.get("current_incarnation"):
                            session.session_incarnation_id = meta_data["current_incarnation"]
                    except Exception:
                        pass
            elif isinstance(self.store, InMemorySessionStore):
                if session_id in self.store._reset_incarnations:
                    session.session_incarnation_id = self.store._reset_incarnations[session_id]

            self._sessions[session_id] = session
            if self.store is not None:
                self.store.save(session)
            return session

    def get(self, session_id: str) -> Optional[SessionState]:
        with self._lock:
            if session_id in self._sessions:
                return self._sessions[session_id]
            if self.store is not None:
                loaded = self.store.load(session_id)
                if loaded is not None:
                    self._sessions[session_id] = loaded
                    return loaded
            return None

    def save(self, session: SessionState, force: bool = False) -> bool:
        with self._lock:
            self._sessions[session.session_id] = session
            if self.store is not None:
                if force:
                    try:
                        return self.store.save(session, force=True)
                    except TypeError:
                        return self.store.save(session)
                return self.store.save(session)
            return True

    def reset(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.get(session_id)
            if session:
                session.reset()
                new_inc = session.session_incarnation_id
            else:
                new_inc = secrets.token_hex(16)
            if self.store is not None:
                self.store.reset(session_id, new_incarnation=new_inc)
            return True

    def delete(self, session_id: str) -> bool:
        with self._lock:
            self._sessions.pop(session_id, None)
            if self.store is not None:
                self.store.delete(session_id)
            return True

    def count(self) -> int:
        with self._lock:
            if self.store is not None:
                persisted_keys = set(self.store.list_sessions())
                memory_keys = set(self._sessions.keys())
                return len(persisted_keys | memory_keys)
            return len(self._sessions)

    def list_session_ids(self) -> List[str]:
        with self._lock:
            if self.store is not None:
                persisted_keys = set(self.store.list_sessions())
                memory_keys = set(self._sessions.keys())
                return sorted(persisted_keys | memory_keys)
            return sorted(self._sessions.keys())


# Global session manager instance (ephemeral in-memory default for CLI backward compatibility)
session_manager = SessionManager()
