"""Authoritative incarnation reads over the existing canonical session stores."""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Optional

from .session import FileSessionStore, InMemorySessionStore, SessionManager


def _in_memory_incarnation(store: InMemorySessionStore, session_id: str) -> Optional[str]:
    reset_incarnation = store._reset_incarnations.get(session_id)
    if reset_incarnation:
        return reset_incarnation
    session = store._storage.get(session_id)
    return session.session_incarnation_id if session is not None else None


def _file_incarnation(store: FileSessionStore, session_id: str) -> Optional[str]:
    reset_path = store._get_reset_meta_path(session_id)
    if reset_path.exists() and reset_path.is_file():
        try:
            data = json.loads(reset_path.read_text(encoding="utf-8"))
            incarnation = data.get("current_incarnation")
            if isinstance(incarnation, str) and incarnation:
                return incarnation
        except (OSError, ValueError, TypeError):
            return None

    session_path = store._get_session_path(session_id)
    if not session_path.exists() or not session_path.is_file():
        return None
    try:
        data = json.loads(session_path.read_text(encoding="utf-8"))
        incarnation = data.get("session_incarnation_id")
        return incarnation if isinstance(incarnation, str) and incarnation else None
    except (OSError, ValueError, TypeError):
        return None


def current_session_incarnation(
    manager: SessionManager, session_id: str
) -> Optional[str]:
    """Read current incarnation without trusting SessionManager's process cache."""
    store = manager.store
    if store is None:
        session = manager.get(session_id)
        return session.session_incarnation_id if session is not None else None

    if isinstance(store, InMemorySessionStore):
        with store._lock:
            return _in_memory_incarnation(store, session_id)

    if isinstance(store, FileSessionStore):
        with store._lock:
            lock = store._get_session_lock(session_id)
            with lock:
                return _file_incarnation(store, session_id)

    session = store.load(session_id)
    return session.session_incarnation_id if session is not None else None


def transition_with_current_session(manager, service, request_id, action, context, reason=None):
    """Run the canonical transition with a server-derived actor incarnation.

    For a distinct approver, the canonical session lock is held through the
    approval transition. This makes approval versus reset linearizable across
    processes: whichever obtains the session lock first wins.
    """
    request = service.store.get(request_id)
    store = manager.store

    def transition(incarnation: Optional[str]):
        checked_context = replace(
            context,
            authoritative_actor_incarnation_id=incarnation or "",
        )
        return service.transition(request_id, action, checked_context, reason=reason)

    # Requester resets already invalidate the request before rotating its
    # session. Avoid reversing that approval-lock/session-lock ordering here.
    if request is None or request.user_id == context.actor_id:
        return transition(current_session_incarnation(manager, context.session_id))

    if isinstance(store, InMemorySessionStore):
        with store._lock:
            return transition(_in_memory_incarnation(store, context.session_id))

    if isinstance(store, FileSessionStore):
        with store._lock:
            lock = store._get_session_lock(context.session_id)
            with lock:
                return transition(_file_incarnation(store, context.session_id))

    return transition(current_session_incarnation(manager, context.session_id))
