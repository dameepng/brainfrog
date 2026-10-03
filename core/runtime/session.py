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

logger = logging.getLogger(__name__)

CURRENT_SESSION_SCHEMA_VERSION = 1


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
    created_at: float = field(default_factory=time.time)
    last_active_at: float = field(default_factory=time.time)
    active_mode: str = "build"
    active_skill: Optional[str] = None
    active_model: Optional[str] = None
    active_provider: Optional[str] = None
    plan_context: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    history: List[Dict[str, Any]] = field(default_factory=list)

    def record_interaction(
        self,
        user_text: str,
        assistant_text: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.last_active_at = time.time()
        self.history.append({
            "timestamp": self.last_active_at,
            "user": user_text,
            "assistant": assistant_text,
            "metadata": dict(metadata or {}),
        })

    def reset(self) -> None:
        """Clear conversation history and ephemeral context and allocate a new incarnation."""
        self.history.clear()
        self.plan_context = None
        self.metadata.clear()
        self.last_active_at = time.time()
        self.session_incarnation_id = secrets.token_hex(16)

    def to_dict(self) -> Dict[str, Any]:
        """Convert session state to JSON-serializable dictionary with schema version."""
        return {
            "schema_version": CURRENT_SESSION_SCHEMA_VERSION,
            "session_id": self.session_id,
            "session_incarnation_id": self.session_incarnation_id,
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
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SessionState:
        """Reconstruct SessionState from dictionary with strict schema version validation."""
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

        return cls(
            session_id=str(data["session_id"]),
            session_incarnation_id=str(incarnation),
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
            history=list(data.get("history", [])),
        )


class SessionStore(ABC):
    """Abstract storage backend for session state persistence."""

    @abstractmethod
    def load(self, session_id: str) -> Optional[SessionState]:
        """Load session state by session_id."""
        raise NotImplementedError

    @abstractmethod
    def save(self, session: SessionState) -> bool:
        """Atomically persist a session state."""
        raise NotImplementedError

    @abstractmethod
    def delete(self, session_id: str) -> bool:
        """Delete persisted session state."""
        raise NotImplementedError

    @abstractmethod
    def list_sessions(self) -> List[str]:
        """List all persisted session IDs."""
        raise NotImplementedError


class InMemorySessionStore(SessionStore):
    """Ephemeral in-memory session store."""

    def __init__(self) -> None:
        self._storage: Dict[str, SessionState] = {}
        self._lock = threading.RLock()

    def load(self, session_id: str) -> Optional[SessionState]:
        with self._lock:
            return self._storage.get(session_id)

    def save(self, session: SessionState) -> bool:
        with self._lock:
            self._storage[session.session_id] = session
            return True

    def delete(self, session_id: str) -> bool:
        with self._lock:
            return bool(self._storage.pop(session_id, None))

    def list_sessions(self) -> List[str]:
        with self._lock:
            return list(self._storage.keys())


class FileSessionStore(SessionStore):
    """Filesystem-backed atomic, corruption-tolerant session store in .brainfrog/sessions/."""

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
        self._lock = threading.RLock()
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        try:
            self.sessions_dir.mkdir(parents=True, exist_ok=True)
            if hasattr(os, "chmod") and os.name != "nt":
                os.chmod(self.sessions_dir, 0o700)
        except Exception as e:
            logger.warning(f"Could not create session directory '{self.sessions_dir}': {e}")

    def _get_session_path(self, session_id: str) -> Path:
        """Derive deterministic, traversal-safe fixed filename from session_id."""
        safe_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
        target = (self.sessions_dir / f"{safe_hash}.json").resolve()
        # Security invariant: assert path remains strictly inside sessions_dir
        if not str(target).startswith(str(self.sessions_dir)):
            raise PermissionError(f"Path traversal detected in session_id: {session_id}")
        return target

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

    def save(self, session: SessionState) -> bool:
        """Atomically persist session state with credential scrubbing and fsync."""
        with self._lock:
            try:
                self._ensure_dirs()
                target_file = self._get_session_path(session.session_id)
                data = session.to_dict()

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
                return True
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

                return SessionState.from_dict(data)
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
        """Remove persisted session file from disk."""
        with self._lock:
            try:
                target_file = self._get_session_path(session_id)
                if target_file.exists():
                    target_file.unlink()
                    return True
                return False
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

    def cleanup_stale_sessions(self, max_age_seconds: float) -> int:
        """Optional cleanup helper for sessions inactive longer than max_age_seconds."""
        with self._lock:
            cleaned = 0
            now = time.time()
            if not self.sessions_dir.exists():
                return 0
            for f in self.sessions_dir.glob("*.json"):
                if f.name.startswith(".tmp_"):
                    continue
                try:
                    content = f.read_text(encoding="utf-8")
                    data = json.loads(content)
                    last_act = float(data.get("last_active_at", 0))
                    if (now - last_act) > max_age_seconds:
                        f.unlink()
                        cleaned += 1
                except Exception:
                    pass
            return cleaned


class SessionManager:
    """Thread-safe session manager with isolated state and pluggable persistence."""

    def __init__(self, store: Optional[SessionStore] = None) -> None:
        self.store = store
        self._sessions: Dict[str, SessionState] = {}
        self._lock = threading.RLock()

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
            )
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

    def save(self, session: SessionState) -> bool:
        with self._lock:
            self._sessions[session.session_id] = session
            if self.store is not None:
                return self.store.save(session)
            return True

    def reset(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.get(session_id)
            if session:
                session.reset()
            if self.store is not None:
                self.store.delete(session_id)
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
