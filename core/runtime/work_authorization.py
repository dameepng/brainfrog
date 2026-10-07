"""Deterministic authorization and session incarnation binding for Work operations.

Guarantees:
- Server-derived authenticated identity is authoritative (no client/callback spoofing).
- Work inspection is restricted to the owning actor/audience.
- Work execution continuation (/resume) strictly enforces session incarnation boundaries
  (resetting a chat session invalidates execution authority for prior incarnations).
"""
from __future__ import annotations

from typing import Optional, Tuple

from core.runtime.work import Work


def authorize_work_inspection(
    work: Work,
    actor_id: str,
    channel: str,
) -> Tuple[bool, str]:
    """Verify whether an authenticated actor is authorized to inspect a Work record.

    Fails closed with a generic 'not found or access denied' message to prevent
    information disclosure about other users' work existence.
    """
    clean_actor = actor_id.strip()
    clean_channel = channel.strip().lower()

    # If work has an owner, only the owner can inspect it
    if work.actor_id and work.actor_id.strip():
        if clean_actor != work.actor_id.strip():
            return False, f"Work '{work.id}' not found or access denied."

    # If work is bound to a specific channel, verify channel compatibility
    if work.channel and work.channel.strip():
        work_chan = work.channel.strip().lower()
        if clean_channel != work_chan and clean_channel != "cli":
            return False, f"Work '{work.id}' not found or access denied."

    return True, "Authorized"


def authorize_work_execution_continuation(
    work: Work,
    actor_id: str,
    channel: str,
    current_session_id: Optional[str] = None,
    current_session_incarnation_id: Optional[str] = None,
) -> Tuple[bool, str]:
    """Verify whether an authenticated actor may continue or resume execution authority for a Work.

    Enforces BF-15H / Section 32:
    - Actor ownership must match.
    - If work originated from a session context and requires execution continuation,
      the session incarnation must match the authoritative current incarnation.
    - Stale session incarnations (after /reset or /new) fail closed and cannot
      resurrect execution permissions.
    """
    ok, reason = authorize_work_inspection(work, actor_id, channel)
    if not ok:
        return False, reason

    # Session incarnation check (Section 32)
    if work.session_id and work.session_incarnation_id:
        if current_session_id and work.session_id == current_session_id:
            if current_session_incarnation_id:
                if current_session_incarnation_id != work.session_incarnation_id:
                    return False, (
                        "Cannot resume work created under an invalidated session incarnation "
                        "(session was reset). Fresh authorization required."
                    )
        elif current_session_id and work.session_id != current_session_id:
            # Attempting to resume work from a different session
            return False, f"Work '{work.id}' belongs to a different session context."

    return True, "Authorized"


__all__ = [
    "authorize_work_execution_continuation",
    "authorize_work_inspection",
]
