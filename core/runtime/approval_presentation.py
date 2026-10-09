"""Bounded, non-authoritative approval views for interactive channels."""
from __future__ import annotations

import re
import hashlib
import json
import time
import unicodedata
from dataclasses import dataclass
from typing import Tuple

from .approval import ApprovalRequest
from .secret_scrubbing import scrub_secrets

TELEGRAM_CALLBACK_MAX_BYTES = 64
TELEGRAM_APPROVAL_MAX_CHARS = 3500
MAX_OPERATION_CHARS = 160
MAX_TARGETS = 8
MAX_TARGET_CHARS = 160
MAX_CAPABILITIES = 12
MAX_CAPABILITY_CHARS = 80
MAX_ACTOR_CHARS = 80
TARGET_FINGERPRINT_CHARS = 16

_CALLBACK_RE = re.compile(r"\Abf:([arc]):(req_[0-9a-f]{8})\Z")


def _clean(value: object) -> str:
    scrubbed = scrub_secrets(str(value)).replace("\r", " ").replace("\n", " ")
    return "".join(
        char if not unicodedata.category(char).startswith("C") else " "
        for char in scrubbed
    ).strip()


def _bounded(value: object, limit: int) -> str:
    clean = _clean(value)
    if len(clean) <= limit:
        return clean
    digest = hashlib.sha256(clean.encode("utf-8")).hexdigest()[:12]
    visible = max(8, limit - len(digest) - 6)
    prefix = visible // 2
    suffix = visible - prefix
    return f"{clean[:prefix]}…{clean[-suffix:]} [#{digest}]"


def _escape_telegram_markdown(value: str) -> str:
    return re.sub(r"([\\_*\[\]()`])", r"\\\1", value)


def _canonical_targets(request: ApprovalRequest) -> Tuple[str, ...]:
    raw_targets = request.canonical_operation.parameters.get("targets", ())
    targets = [request.canonical_operation.target]
    if isinstance(raw_targets, (list, tuple)):
        targets.extend(raw_targets)
    return tuple(sorted({str(target) for target in targets if str(target).strip()}))


def _scope_fingerprint(targets: Tuple[str, ...]) -> str:
    payload = json.dumps(targets, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:TARGET_FINGERPRINT_CHARS]


def _granted_capabilities(request: ApprovalRequest) -> Tuple[str, ...]:
    caps = request.capabilities
    result = []
    if caps.filesystem.read:
        result.append("filesystem.read")
    if caps.filesystem.write:
        result.append("filesystem.write")
    if caps.network.access:
        result.append("network.access")
    if caps.shell.execute:
        result.append("shell.execute")
    if caps.git.read:
        result.append("git.read")
    if caps.git.commit:
        result.append("git.commit")
    if caps.git.push:
        result.append("git.push")
    return tuple(result[:MAX_CAPABILITIES])


@dataclass(frozen=True)
class ApprovalPresentation:
    """A secret-scrubbed view model. It contains no approval authority."""

    request_id: str
    operation: str
    risk_class: str
    targets: Tuple[str, ...]
    target_count: int
    scope_fingerprint: str
    capabilities: Tuple[str, ...]
    requester: str
    channel: str
    expires_at: float
    two_man_rule: bool
    status: str

    @classmethod
    def from_request(
        cls, request: ApprovalRequest, *, two_man_rule: bool
    ) -> "ApprovalPresentation":
        canonical_targets = _canonical_targets(request)
        display_targets = tuple(
            _bounded(target, MAX_TARGET_CHARS)
            for target in canonical_targets[:MAX_TARGETS]
        )
        return cls(
            request_id=request.request_id,
            operation=_bounded(request.canonical_operation.action_type, MAX_OPERATION_CHARS),
            risk_class=_bounded(request.risk_class, 24),
            targets=display_targets,
            target_count=len(canonical_targets),
            scope_fingerprint=_scope_fingerprint(canonical_targets),
            capabilities=tuple(
                _bounded(item, MAX_CAPABILITY_CHARS)
                for item in _granted_capabilities(request)
            ),
            requester=_bounded(request.user_id, MAX_ACTOR_CHARS),
            channel=_bounded(request.channel, 24),
            expires_at=request.expires_at,
            two_man_rule=two_man_rule,
            status=request.status.value,
        )

    def render_telegram(self, *, now: float | None = None) -> str:
        remaining = max(0, int(self.expires_at - (time.time() if now is None else now)))
        mins, secs = divmod(remaining, 60)
        expiry = f"{mins}m {secs}s" if mins else f"{secs}s"
        target_lines = [
            f"• {_escape_telegram_markdown(item)}" for item in self.targets
        ] or ["• none"]
        hidden_count = self.target_count - len(self.targets)
        if hidden_count:
            target_lines.append(f"+{hidden_count} additional targets")
        capability_lines = [
            f"• {_escape_telegram_markdown(item)}" for item in self.capabilities
        ] or ["• none"]
        two_man = "Required (requester cannot self-approve)" if self.two_man_rule else "Not required"
        text = "\n".join(
            [
                "🧠 BrainFrog — Approval Required",
                "",
                "Operation",
                _escape_telegram_markdown(self.operation or "unspecified"),
                "",
                f"Risk: {self.risk_class}",
                f"Targets: {self.target_count}",
                *target_lines,
                f"Scope fingerprint: {self.scope_fingerprint}",
                "",
                "Capabilities",
                *capability_lines,
                "",
                f"Requested by: {_escape_telegram_markdown(self.requester)}",
                f"Expires: {expiry}",
                f"Two-person rule: {two_man}",
                f"Status: {self.status}",
                f"Request: `{self.request_id}`",
                "",
                f"Command fallback: `/approve {self.request_id}`",
                f"Reject fallback: `/reject {self.request_id}`",
            ]
        )
        if len(text) > TELEGRAM_APPROVAL_MAX_CHARS:
            raise ValueError("Approval presentation exceeds Telegram size limit")
        return text


def encode_telegram_callback(action: str, request_id: str) -> str:
    codes = {"approve": "a", "reject": "r", "cancel": "c"}
    if action not in codes or not re.fullmatch(r"req_[0-9a-f]{8}", request_id):
        raise ValueError("Invalid approval callback")
    payload = f"bf:{codes[action]}:{request_id}"
    if len(payload.encode("utf-8")) > TELEGRAM_CALLBACK_MAX_BYTES:
        raise ValueError("Approval callback exceeds Telegram limit")
    return payload


def parse_telegram_callback(payload: object) -> Tuple[str, str]:
    if not isinstance(payload, str) or len(payload.encode("utf-8")) > TELEGRAM_CALLBACK_MAX_BYTES:
        raise ValueError("Malformed approval callback")
    match = _CALLBACK_RE.fullmatch(payload)
    if not match:
        raise ValueError("Malformed approval callback")
    actions = {"a": "approve", "r": "reject", "c": "cancel"}
    return actions[match.group(1)], match.group(2)


def telegram_approval_keyboard(request_id: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "✓ Approve", "callback_data": encode_telegram_callback("approve", request_id)},
                {"text": "✕ Reject", "callback_data": encode_telegram_callback("reject", request_id)},
            ],
            [{"text": "Cancel", "callback_data": encode_telegram_callback("cancel", request_id)}],
        ]
    }
