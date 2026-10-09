"""Bounded, secret-scrubbed, injection-safe presentation formatting for Work units.

Ensures that Work state representation never leaks secrets or injects arbitrary
formatting across Telegram, WhatsApp, and CLI interfaces.
"""
from __future__ import annotations

import hashlib
import re
import time
import unicodedata
from typing import Any, Dict, Optional, Sequence, Tuple

from core.runtime.secret_scrubbing import scrub_secrets
from core.runtime.work import Work, WorkStatus

MAX_WORK_TITLE_PRESENT_CHARS = 80
MAX_WORK_GOAL_PRESENT_CHARS = 160
MAX_WORK_SUMMARY_PRESENT_CHARS = 240
MAX_WORK_FAILURE_PRESENT_CHARS = 160
MAX_WORKS_PER_LIST_PRESENT = 20


def _clean(value: object) -> str:
    """Scrub credentials and strip control characters from presentation string."""
    if value is None:
        return ""
    scrubbed = scrub_secrets(str(value)).replace("\r", " ").replace("\n", " ")
    return "".join(
        char if not unicodedata.category(char).startswith("C") else " "
        for char in scrubbed
    ).strip()


def _bounded(value: object, limit: int) -> str:
    """Clamp string to limit with stable SHA-256 digest suffix if truncated."""
    clean = _clean(value)
    if len(clean) <= limit:
        return clean
    digest = hashlib.sha256(clean.encode("utf-8")).hexdigest()[:8]
    visible = max(8, limit - len(digest) - 6)
    prefix = visible // 2
    suffix = visible - prefix
    return f"{clean[:prefix]}…{clean[-suffix:]} [#{digest}]"


def _escape_telegram_markdown(value: str) -> str:
    """Escape Telegram Markdown special characters in plain text."""
    return re.sub(r"([\\_*\[\]()])", r"\\\1", _clean(value))


def _escape_telegram_code(value: str) -> str:
    """Sanitize content intended for inline code backticks in Telegram."""
    return _clean(value).replace("`", "'")


def format_work_status_badge(work: Work) -> str:
    """Generate user-facing descriptive status label."""
    if work.status == WorkStatus.DONE:
        if work.verification_result and str(work.verification_result.status).upper() == "WARN":
            return "DONE (with warnings)"
        return "DONE"
    elif work.status == WorkStatus.APPROVAL_REQUIRED:
        return "WAITING FOR APPROVAL"
    return work.status.value.upper()


def format_work_detail(
    work: Work,
    channel: str = "cli",
    approval_status: Optional[str] = None,
    transaction_status: Optional[str] = None,
) -> str:
    """Render comprehensive, secret-scrubbed detail view of a Work unit."""
    is_telegram = channel.lower() == "telegram"
    is_whatsapp = channel.lower() == "whatsapp"

    badge = format_work_status_badge(work)
    goal = _bounded(work.goal or work.intent or "No description", MAX_WORK_GOAL_PRESENT_CHARS)
    actor = _bounded(work.actor_id or "unknown", 40)
    created_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(work.created_at))
    updated_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(work.updated_at))

    # Resumability & Cancellation flags
    resumable_str = "YES" if work.is_resumable else "NO"
    cancellable_str = "YES" if not work.is_terminal else "NO"

    lines: list[str] = []
    if is_telegram:
        lines.append(f"📋 *Work Details:* `{_escape_telegram_code(work.id)}`")
        lines.append(f"• *Status:* `{_escape_telegram_code(badge)}`")
        lines.append(f"• *Goal:* {_escape_telegram_markdown(goal)}")
        lines.append(f"• *Actor:* `{_escape_telegram_code(actor)}` (Channel: `{_escape_telegram_code(work.channel or 'none')}`)")
        if work.plan:
            lines.append(f"• *Plan Steps:* {len(work.plan)} defined")
            if work.current_step_id:
                lines.append(f"• *Current Step:* `{_escape_telegram_code(_bounded(work.current_step_id, 40))}`")
        if work.approval_request_id:
            appr_text = approval_status or "linked"
            lines.append(f"• *Approval Request:* `{_escape_telegram_code(work.approval_request_id)}` ({_escape_telegram_markdown(appr_text)})")
        if work.transaction_id:
            tx_text = transaction_status or "linked"
            lines.append(f"• *Transaction:* `{_escape_telegram_code(work.transaction_id)}` ({_escape_telegram_markdown(tx_text)})")
        if work.verification_result:
            vr = work.verification_result
            lines.append(f"• *Verification:* `{_escape_telegram_code(str(vr.status).upper())}` ({vr.passed} passed, {vr.failed} failed, {vr.warnings} warn)")
        if work.failure:
            lines.append(f"• *Failure Code:* `{_escape_telegram_code(work.failure.code)}` [{_escape_telegram_code(work.failure.stage)}]")
            lines.append(f"• *Failure Summary:* {_escape_telegram_markdown(_bounded(work.failure.summary, MAX_WORK_FAILURE_PRESENT_CHARS))}")
        if work.cancellation_reason:
            lines.append(f"• *Cancelled:* {_escape_telegram_markdown(_bounded(work.cancellation_reason, 100))}")
        lines.append(f"• *Resumable:* `{resumable_str}` | *Cancellable:* `{cancellable_str}`")
        lines.append(f"• *Created:* `{created_str}` | *Updated:* `{updated_str}`")
    else:
        # WhatsApp or CLI plain text
        lines.append(f"📋 Work Details: {work.id}")
        lines.append(f"• Status: {badge}")
        lines.append(f"• Goal: {goal}")
        lines.append(f"• Actor: {actor} (Channel: {work.channel or 'none'})")
        if work.plan:
            lines.append(f"• Plan Steps: {len(work.plan)} defined")
            if work.current_step_id:
                lines.append(f"• Current Step: {_bounded(work.current_step_id, 40)}")
        if work.approval_request_id:
            appr_text = approval_status or "linked"
            lines.append(f"• Approval Request: {work.approval_request_id} ({appr_text})")
        if work.transaction_id:
            tx_text = transaction_status or "linked"
            lines.append(f"• Transaction: {work.transaction_id} ({tx_text})")
        if work.verification_result:
            vr = work.verification_result
            lines.append(f"• Verification: {str(vr.status).upper()} ({vr.passed} passed, {vr.failed} failed, {vr.warnings} warn)")
        if work.failure:
            lines.append(f"• Failure Code: {work.failure.code} [{work.failure.stage}]")
            lines.append(f"• Failure Summary: {_bounded(work.failure.summary, MAX_WORK_FAILURE_PRESENT_CHARS)}")
        if work.cancellation_reason:
            lines.append(f"• Cancelled: {_bounded(work.cancellation_reason, 100)}")
        lines.append(f"• Resumable: {resumable_str} | Cancellable: {cancellable_str}")
        lines.append(f"• Created: {created_str} | Updated: {updated_str}")

    return "\n".join(lines)


def format_works_list(
    works: Sequence[Work],
    channel: str = "cli",
    filter_name: Optional[str] = None,
) -> str:
    """Render bounded summary list of Work records."""
    is_telegram = channel.lower() == "telegram"
    title_suffix = f" ({filter_name})" if filter_name else ""

    if not works:
        return f"ℹ️ No work records found{title_suffix}."

    lines: list[str] = []
    if is_telegram:
        lines.append(f"📋 *Work Records{_escape_telegram_markdown(title_suffix)}:*\n")
        for w in works[:MAX_WORKS_PER_LIST_PRESENT]:
            badge = format_work_status_badge(w)
            goal = _bounded(w.goal or w.intent or "No description", 50)
            lines.append(f"• `{_escape_telegram_code(w.id)}` — *{_escape_telegram_markdown(badge)}*: {_escape_telegram_markdown(goal)}")
        if len(works) > MAX_WORKS_PER_LIST_PRESENT:
            lines.append(f"\n_… and {len(works) - MAX_WORKS_PER_LIST_PRESENT} more records (use /work <id> for details)._")
    else:
        lines.append(f"📋 Work Records{title_suffix}:\n")
        for w in works[:MAX_WORKS_PER_LIST_PRESENT]:
            badge = format_work_status_badge(w)
            goal = _bounded(w.goal or w.intent or "No description", 50)
            lines.append(f"• {w.id} — [{badge}]: {goal}")
        if len(works) > MAX_WORKS_PER_LIST_PRESENT:
            lines.append(f"\n… and {len(works) - MAX_WORKS_PER_LIST_PRESENT} more records (use /work <id> for details).")

    return "\n".join(lines)


_WORK_CALLBACK_RE = re.compile(r"\Abfw:([rcs]):([a-zA-Z0-9_\-]{3,64})\Z")


def parse_telegram_work_callback(data: str) -> Tuple[str, str]:
    """Parse and validate callback data for Telegram work buttons.

    Format: bfw:<action>:<work_id> where action is 'r' (resume), 'c' (cancel), or 's' (status).
    """
    if type(data) is not str:
        raise ValueError("Callback data must be a string")
    clean = data.strip()
    match = _WORK_CALLBACK_RE.match(clean)
    if not match:
        raise ValueError(f"Malformed work callback data: '{clean}'")
    action_code, work_id = match.groups()
    action_map = {"r": "resume", "c": "cancel", "s": "status"}
    return action_map[action_code], work_id


def telegram_work_keyboard(
    work_id: str,
    *,
    is_resumable: bool = False,
    is_cancellable: bool = False,
) -> Optional[Dict[str, Any]]:
    """Build bounded inline keyboard markup for interactive Telegram work actions."""
    buttons = []
    if is_resumable:
        buttons.append({"text": "▶️ Resume", "callback_data": f"bfw:r:{work_id}"})
    if is_cancellable:
        buttons.append({"text": "🛑 Cancel", "callback_data": f"bfw:c:{work_id}"})
    if not buttons:
        return None
    return {"inline_keyboard": [buttons]}


__all__ = [
    "format_work_detail",
    "format_work_status_badge",
    "format_works_list",
    "parse_telegram_work_callback",
    "telegram_work_keyboard",
]
