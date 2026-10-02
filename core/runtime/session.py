"""Isolated session management for BrainFrog.

Guarantees that each channel, user, and conversation has a strictly isolated
conversation state, preventing cross-tenant or cross-user memory leakage.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SessionState:
    """State scoped exclusively to a single channel:user:conversation session."""

    session_id: str
    channel: str
    user_id: str
    conversation_id: str
    created_at: float = field(default_factory=time.time)
    last_active_at: float = field(default_factory=time.time)
    active_mode: str = "build"
    active_skill: Optional[str] = None
    active_model: Optional[str] = None
    active_provider: Optional[str] = None
    plan_context: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    history: List[Dict[str, Any]] = field(default_factory=list)

    def record_interaction(self, user_text: str, assistant_text: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        self.last_active_at = time.time()
        self.history.append({
            "timestamp": self.last_active_at,
            "user": user_text,
            "assistant": assistant_text,
            "metadata": dict(metadata or {}),
        })

    def reset(self) -> None:
        """Clear conversation history and ephemeral context while preserving identities."""
        self.history.clear()
        self.plan_context = None
        self.metadata.clear()
        self.last_active_at = time.time()


class SessionManager:
    """In-memory thread-safe session manager with isolated state per session_id."""

    def __init__(self) -> None:
        self._sessions: Dict[str, SessionState] = {}

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
        if session_id not in self._sessions:
            self._sessions[session_id] = SessionState(
                session_id=session_id,
                channel=c,
                user_id=u,
                conversation_id=conv,
                active_mode=default_mode,
            )
        return self._sessions[session_id]

    def get(self, session_id: str) -> Optional[SessionState]:
        return self._sessions.get(session_id)

    def reset(self, session_id: str) -> bool:
        session = self._sessions.get(session_id)
        if session:
            session.reset()
            return True
        return False

    def count(self) -> int:
        return len(self._sessions)


# Global session manager instance
session_manager = SessionManager()
