"""Remote Approval and Two-Man Rule Domain Model for BrainFrog.

Provides a secure, channel-agnostic authorization mechanism for remote operations
that require explicit human approval without bypassing existing runtime security:
- CanonicalOperation: Deterministic representation with SHA-256 digest
- ApprovalRequest: State machine with cryptographically secure nonce and TTL
- ApprovalStatus: PENDING -> APPROVED -> CONSUMED (or REJECTED, EXPIRED, CANCELLED)
- Two-Man Rule: Requester != Approver enforcement
- Scoped Binding: Bound to exact operation digest, session, channel, and user
- Atomic Consumption: Single-use with TOCTOU race immunity
- Pluggable Store: InMemoryApprovalStore and FileApprovalStore (.brainfrog/approvals/)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import shutil
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.runtime.session import scrub_secrets

logger = logging.getLogger(__name__)

CURRENT_APPROVAL_SCHEMA_VERSION = 1
DEFAULT_APPROVAL_TTL_SECONDS = 300.0  # 5 minutes


class ApprovalStatus(str, Enum):
    """Lifecycle statuses for approval requests."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CONSUMED = "CONSUMED"
    CANCELLED = "CANCELLED"


class RiskClass(str, Enum):
    """Risk tiers for operations requiring approval."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass
class CanonicalOperation:
    """Deterministic, canonical representation of an operation to be authorized.

    Guarantees that approval is bound to the exact security-relevant parameters
    rather than ambiguous natural-language text.
    """

    action_type: str
    target: str
    parameters: Dict[str, Any] = field(default_factory=dict)

    def to_canonical_dict(self) -> Dict[str, Any]:
        """Return canonical sorted dictionary representation."""
        clean_params: Dict[str, Any] = {}
        for k in sorted(self.parameters.keys()):
            val = self.parameters[k]
            clean_params[str(k)] = val
        return {
            "action_type": str(self.action_type).strip().lower(),
            "target": str(self.target).strip(),
            "parameters": clean_params,
        }

    def to_canonical_json(self) -> str:
        """Serialize to deterministic, canonical UTF-8 JSON."""
        return json.dumps(
            self.to_canonical_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def compute_digest(self) -> str:
        """Compute SHA-256 digest over the canonical JSON representation."""
        return hashlib.sha256(self.to_canonical_json().encode("utf-8")).hexdigest()


def extract_canonical_operation(
    text: str,
    action: Any = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> CanonicalOperation:
    """Deterministically extract a CanonicalOperation from raw text and action."""
    import re

    meta = metadata or {}
    action_type = meta.get("action_type") or (
        action.value if hasattr(action, "value") else str(action or "generic_action")
    )
    action_type = str(action_type).strip().lower()

    target = meta.get("target") or meta.get("path")
    if not target:
        # Detect file paths in text (e.g. config.py, src/main.py, /path/to/file)
        match = re.search(r"([a-zA-Z0-9_\-\.\/\\]+\.[a-zA-Z0-9_\-]+)", text)
        if match:
            target = match.group(1).replace("\\", "/")
        else:
            # Detect environment or target keywords (e.g. production, staging)
            env_match = re.search(r"\b(production|prod|staging|test)\b", text, re.IGNORECASE)
            if env_match:
                target = env_match.group(1).lower()
            else:
                target = text.strip()

    params: Dict[str, Any] = {}
    for k in ("environment", "branch", "force", "mode", "command"):
        if k in meta:
            params[k] = meta[k]

    return CanonicalOperation(
        action_type=action_type,
        target=str(target).strip(),
        parameters=params,
    )


@dataclass
class ApprovalRequest:
    """Approval request bound to an exact operation, session, and channel."""

    request_id: str
    session_id: str
    channel: str
    user_id: str
    conversation_id: str
    operation_type: str
    canonical_operation: CanonicalOperation
    operation_digest: str
    risk_class: str
    created_at: float
    expires_at: float
    nonce: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    approver_id: Optional[str] = None
    approved_at: Optional[float] = None
    consumed_at: Optional[float] = None
    rejection_reason: Optional[str] = None
    schema_version: int = CURRENT_APPROVAL_SCHEMA_VERSION

    def is_expired(self, now: Optional[float] = None) -> bool:
        """Check if request validity window has expired."""
        current_time = now if now is not None else time.time()
        return current_time > self.expires_at

    def to_dict(self) -> Dict[str, Any]:
        """Serialize state to a JSON-safe dictionary with secret scrubbing."""
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "channel": self.channel,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "operation_type": self.operation_type,
            "canonical_operation": self.canonical_operation.to_canonical_dict(),
            "operation_digest": self.operation_digest,
            "risk_class": self.risk_class,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
            "status": self.status.value,
            "approver_id": self.approver_id,
            "approved_at": self.approved_at,
            "consumed_at": self.consumed_at,
            "rejection_reason": scrub_secrets(self.rejection_reason) if self.rejection_reason else None,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ApprovalRequest:
        """Instantiate an ApprovalRequest from a serialized dictionary."""
        version = int(data.get("schema_version", 1))
        if version > CURRENT_APPROVAL_SCHEMA_VERSION:
            raise ValueError(f"Unsupported approval schema version: {version}")

        op_data = data.get("canonical_operation", {})
        canon_op = CanonicalOperation(
            action_type=str(op_data.get("action_type", "")),
            target=str(op_data.get("target", "")),
            parameters=op_data.get("parameters", {}),
        )

        raw_status = str(data.get("status", ApprovalStatus.PENDING.value))
        try:
            status = ApprovalStatus(raw_status)
        except ValueError:
            status = ApprovalStatus.PENDING

        return cls(
            request_id=str(data["request_id"]),
            session_id=str(data["session_id"]),
            channel=str(data["channel"]),
            user_id=str(data["user_id"]),
            conversation_id=str(data["conversation_id"]),
            operation_type=str(data.get("operation_type", "")),
            canonical_operation=canon_op,
            operation_digest=str(data["operation_digest"]),
            risk_class=str(data.get("risk_class", RiskClass.MEDIUM.value)),
            created_at=float(data["created_at"]),
            expires_at=float(data["expires_at"]),
            nonce=str(data["nonce"]),
            status=status,
            approver_id=data.get("approver_id"),
            approved_at=float(data["approved_at"]) if data.get("approved_at") else None,
            consumed_at=float(data["consumed_at"]) if data.get("consumed_at") else None,
            rejection_reason=data.get("rejection_reason"),
            schema_version=version,
        )


class ApprovalStore(ABC):
    """Abstract persistence layer for approval requests."""

    @abstractmethod
    def save(self, request: ApprovalRequest) -> bool:
        raise NotImplementedError

    @abstractmethod
    def get(self, request_id: str) -> Optional[ApprovalRequest]:
        raise NotImplementedError

    @abstractmethod
    def delete(self, request_id: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def list_requests(self) -> List[ApprovalRequest]:
        raise NotImplementedError

    @abstractmethod
    def claim_and_consume(
        self,
        request_id: str,
        expected_digest: str,
        session_id: str,
        channel: str,
    ) -> Tuple[bool, Optional[ApprovalRequest], str]:
        """Atomically verify and consume an approved request in a single step."""
        raise NotImplementedError


class InMemoryApprovalStore(ApprovalStore):
    """Thread-safe in-memory approval store for unit testing."""

    def __init__(self) -> None:
        self._requests: Dict[str, ApprovalRequest] = {}
        self._lock = threading.RLock()

    def save(self, request: ApprovalRequest) -> bool:
        with self._lock:
            self._requests[request.request_id] = request
            return True

    def get(self, request_id: str) -> Optional[ApprovalRequest]:
        with self._lock:
            return self._requests.get(request_id)

    def delete(self, request_id: str) -> bool:
        with self._lock:
            if request_id in self._requests:
                del self._requests[request_id]
                return True
            return False

    def list_requests(self) -> List[ApprovalRequest]:
        with self._lock:
            return list(self._requests.values())

    def claim_and_consume(
        self,
        request_id: str,
        expected_digest: str,
        session_id: str,
        channel: str,
    ) -> Tuple[bool, Optional[ApprovalRequest], str]:
        with self._lock:
            req = self._requests.get(request_id)
            if req is None:
                return False, None, f"Approval request '{request_id}' not found."

            if req.status == ApprovalStatus.CONSUMED:
                return False, req, f"Approval request '{request_id}' has already been consumed (replay prevented)."

            if req.status == ApprovalStatus.REJECTED:
                return False, req, f"Approval request '{request_id}' was rejected."

            if req.status == ApprovalStatus.CANCELLED:
                return False, req, f"Approval request '{request_id}' was cancelled."

            if req.is_expired():
                req.status = ApprovalStatus.EXPIRED
                return False, req, f"Approval request '{request_id}' has expired."

            if req.status != ApprovalStatus.APPROVED:
                return False, req, f"Approval request '{request_id}' is not in APPROVED state (current: {req.status.value})."

            if req.operation_digest != expected_digest:
                return False, req, (
                    f"Operation digest mismatch: expected '{expected_digest}', "
                    f"approved '{req.operation_digest}' (operation substitution prevented)."
                )

            if req.session_id != session_id:
                return False, req, (
                    f"Session mismatch: request belongs to '{req.session_id}', "
                    f"attempted execution from '{session_id}'."
                )

            if req.channel.lower() != channel.lower():
                return False, req, (
                    f"Channel mismatch: request belongs to '{req.channel}', "
                    f"attempted execution from '{channel}'."
                )

            # Atomic one-time state transition
            req.status = ApprovalStatus.CONSUMED
            req.consumed_at = time.time()
            return True, req, "Approval successfully consumed."


class FileApprovalStore(ApprovalStore):
    """Filesystem-backed persistent approval store (.brainfrog/approvals/).

    Enforces:
    - Safe deterministic SHA-256 hashed filenames
    - Complete path traversal immunity
    - Atomic writes via tempfile + os.replace
    - Corruption quarantine to .brainfrog/approvals/corrupt/
    - Thread-safe atomic consumption with RLock
    """

    def __init__(self, repo_dir: Optional[Path] = None, approvals_dir: Optional[Path] = None) -> None:
        if approvals_dir is not None:
            self.approvals_dir = Path(approvals_dir).resolve()
        else:
            base = Path(repo_dir).resolve() if repo_dir else Path.cwd().resolve()
            self.approvals_dir = base / ".brainfrog" / "approvals"

        self.corrupt_dir = self.approvals_dir / "corrupt"
        self._lock = threading.RLock()
        self._ensure_directories()

    def _ensure_directories(self) -> None:
        try:
            self.approvals_dir.mkdir(parents=True, exist_ok=True)
            self.corrupt_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"Could not create approvals directory {self.approvals_dir}: {e}")

    def _get_approval_path(self, request_id: str) -> Path:
        """Generate safe deterministic hashed filename from request_id."""
        raw_hash = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32]
        target_path = (self.approvals_dir / f"{raw_hash}.json").resolve()
        if not target_path.is_relative_to(self.approvals_dir):
            raise ValueError(f"Path traversal detected for request_id: {request_id}")
        return target_path

    def _quarantine_file(self, file_path: Path, reason: str) -> None:
        """Safely quarantine a corrupted approval file."""
        try:
            self.corrupt_dir.mkdir(parents=True, exist_ok=True)
            timestamp = int(time.time())
            quarantined = self.corrupt_dir / f"{timestamp}_{file_path.name}"
            shutil.move(str(file_path), str(quarantined))
            logger.warning(f"Quarantined corrupt approval file '{file_path.name}' to '{quarantined.name}'. Reason: {reason}")
        except Exception as e:
            logger.error(f"Failed to quarantine corrupt file '{file_path}': {e}")

    def save(self, request: ApprovalRequest) -> bool:
        with self._lock:
            try:
                self._ensure_directories()
                target_file = self._get_approval_path(request.request_id)
                data = request.to_dict()
                content = json.dumps(data, indent=2, ensure_ascii=False)

                tmp_name = f".tmp_{hashlib.sha256(request.request_id.encode('utf-8')).hexdigest()[:16]}_{uuid.uuid4().hex[:8]}.json"
                tmp_file = self.approvals_dir / tmp_name

                with open(tmp_file, "w", encoding="utf-8") as f:
                    f.write(content)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except (AttributeError, OSError):
                        pass

                os.replace(tmp_file, target_file)
                return True
            except Exception as e:
                logger.error(f"Failed to save approval request '{request.request_id}': {e}")
                return False

    def get(self, request_id: str) -> Optional[ApprovalRequest]:
        with self._lock:
            try:
                target_file = self._get_approval_path(request_id)
                if not target_file.exists():
                    return None

                content = target_file.read_text(encoding="utf-8")
                if not content.strip():
                    self._quarantine_file(target_file, "Empty approval file")
                    return None

                data = json.loads(content)
                if not isinstance(data, dict):
                    self._quarantine_file(target_file, "Root JSON is not an object")
                    return None

                schema_ver = int(data.get("schema_version", 1))
                if schema_ver > CURRENT_APPROVAL_SCHEMA_VERSION:
                    logger.warning(f"Unsupported schema version {schema_ver} in '{target_file.name}'")
                    return None

                return ApprovalRequest.from_dict(data)
            except json.JSONDecodeError as jde:
                self._quarantine_file(target_file, f"Invalid JSON: {jde}")
                return None
            except Exception as e:
                logger.error(f"Error loading approval request '{request_id}': {e}")
                return None

    def delete(self, request_id: str) -> bool:
        with self._lock:
            try:
                target_file = self._get_approval_path(request_id)
                if target_file.exists():
                    target_file.unlink()
                    return True
                return False
            except Exception as e:
                logger.warning(f"Failed to delete approval file for '{request_id}': {e}")
                return False

    def list_requests(self) -> List[ApprovalRequest]:
        with self._lock:
            requests = []
            if not self.approvals_dir.exists():
                return requests

            for f in sorted(self.approvals_dir.glob("*.json")):
                if f.name.startswith(".tmp_"):
                    continue
                try:
                    content = f.read_text(encoding="utf-8")
                    data = json.loads(content)
                    if isinstance(data, dict) and "request_id" in data:
                        requests.append(ApprovalRequest.from_dict(data))
                except Exception:
                    pass
            return requests

    def claim_and_consume(
        self,
        request_id: str,
        expected_digest: str,
        session_id: str,
        channel: str,
    ) -> Tuple[bool, Optional[ApprovalRequest], str]:
        with self._lock:
            req = self.get(request_id)
            if req is None:
                return False, None, f"Approval request '{request_id}' not found."

            if req.status == ApprovalStatus.CONSUMED:
                return False, req, f"Approval request '{request_id}' has already been consumed (replay prevented)."

            if req.status == ApprovalStatus.REJECTED:
                return False, req, f"Approval request '{request_id}' was rejected."

            if req.status == ApprovalStatus.CANCELLED:
                return False, req, f"Approval request '{request_id}' was cancelled."

            if req.is_expired():
                req.status = ApprovalStatus.EXPIRED
                self.save(req)
                return False, req, f"Approval request '{request_id}' has expired."

            if req.status != ApprovalStatus.APPROVED:
                return False, req, f"Approval request '{request_id}' is not in APPROVED state (current: {req.status.value})."

            if req.operation_digest != expected_digest:
                return False, req, (
                    f"Operation digest mismatch: expected '{expected_digest}', "
                    f"approved '{req.operation_digest}' (operation substitution prevented)."
                )

            if req.session_id != session_id:
                return False, req, (
                    f"Session mismatch: request belongs to '{req.session_id}', "
                    f"attempted execution from '{session_id}'."
                )

            if req.channel.lower() != channel.lower():
                return False, req, (
                    f"Channel mismatch: request belongs to '{req.channel}', "
                    f"attempted execution from '{channel}'."
                )

            # Atomic one-time state transition
            req.status = ApprovalStatus.CONSUMED
            req.consumed_at = time.time()
            self.save(req)
            return True, req, "Approval successfully consumed."


class ApprovalService:
    """Channel-agnostic service coordinating approval lifecycle and policy enforcement."""

    def __init__(
        self,
        store: Optional[ApprovalStore] = None,
        default_ttl_seconds: float = DEFAULT_APPROVAL_TTL_SECONDS,
        two_man_rule_enabled: bool = True,
    ) -> None:
        self.store = store or FileApprovalStore()
        self.default_ttl_seconds = default_ttl_seconds
        self.two_man_rule_enabled = two_man_rule_enabled

    def create_request(
        self,
        session_id: str,
        channel: str,
        user_id: str,
        conversation_id: str,
        operation_type: str,
        canonical_operation: CanonicalOperation,
        risk_class: str = RiskClass.MEDIUM.value,
        ttl_seconds: Optional[float] = None,
    ) -> ApprovalRequest:
        """Create and persist a new PENDING approval request with secure nonce."""
        now = time.time()
        ttl = ttl_seconds or self.default_ttl_seconds
        expires_at = now + ttl

        # Cryptographically secure random identifiers
        request_id = f"req_{secrets.token_hex(4)}"
        nonce = secrets.token_hex(16)
        digest = canonical_operation.compute_digest()

        request = ApprovalRequest(
            request_id=request_id,
            session_id=session_id,
            channel=channel.lower().strip(),
            user_id=str(user_id).strip(),
            conversation_id=str(conversation_id).strip(),
            operation_type=operation_type,
            canonical_operation=canonical_operation,
            operation_digest=digest,
            risk_class=risk_class,
            created_at=now,
            expires_at=expires_at,
            nonce=nonce,
            status=ApprovalStatus.PENDING,
        )
        self.store.save(request)
        return request

    def approve(
        self,
        request_id: str,
        approver_id: str,
        channel: str,
        session_id: Optional[str] = None,
    ) -> Tuple[bool, str, Optional[ApprovalRequest]]:
        """Transition a request from PENDING -> APPROVED with two-man rule validation."""
        req = self.store.get(request_id)
        if req is None:
            return False, f"Approval request '{request_id}' not found.", None

        if req.is_expired():
            req.status = ApprovalStatus.EXPIRED
            self.store.save(req)
            return False, f"Approval request '{request_id}' has expired.", req

        if req.status != ApprovalStatus.PENDING:
            return False, f"Cannot approve request '{request_id}' with status {req.status.value}.", req

        # Channel verification
        if channel.lower().strip() != req.channel.lower():
            return False, f"Channel mismatch: request belongs to '{req.channel}', cannot approve from '{channel}'.", req

        # Session verification (if enforced)
        if session_id and session_id != req.session_id:
            return False, f"Session mismatch: request belongs to session '{req.session_id}'.", req

        # Two-man rule enforcement
        clean_approver = str(approver_id).strip()
        if self.two_man_rule_enabled and clean_approver == req.user_id:
            return False, "Two-man rule violation: Requester cannot approve their own request.", req

        req.status = ApprovalStatus.APPROVED
        req.approver_id = clean_approver
        req.approved_at = time.time()
        self.store.save(req)
        return True, f"Request '{request_id}' has been approved by user '{clean_approver}'.", req

    def reject(
        self,
        request_id: str,
        approver_id: str,
        channel: str,
        reason: str = "Rejected by human operator",
    ) -> Tuple[bool, str, Optional[ApprovalRequest]]:
        """Transition a request from PENDING -> REJECTED."""
        req = self.store.get(request_id)
        if req is None:
            return False, f"Approval request '{request_id}' not found.", None

        if req.status != ApprovalStatus.PENDING:
            return False, f"Cannot reject request '{request_id}' with status {req.status.value}.", req

        clean_approver = str(approver_id).strip()
        req.status = ApprovalStatus.REJECTED
        req.approver_id = clean_approver
        req.rejection_reason = reason
        self.store.save(req)
        return True, f"Request '{request_id}' has been rejected by user '{clean_approver}'.", req

    def cancel(
        self,
        request_id: str,
        requester_id: str,
    ) -> Tuple[bool, str, Optional[ApprovalRequest]]:
        """Allow the original requester to cancel their own pending request."""
        req = self.store.get(request_id)
        if req is None:
            return False, f"Approval request '{request_id}' not found.", None

        if req.status != ApprovalStatus.PENDING:
            return False, f"Cannot cancel request '{request_id}' with status {req.status.value}.", req

        if str(requester_id).strip() != req.user_id:
            return False, "Only the original requester can cancel a pending request.", req

        req.status = ApprovalStatus.CANCELLED
        self.store.save(req)
        return True, f"Request '{request_id}' has been cancelled.", req

    def verify_and_consume(
        self,
        request_id: str,
        expected_digest: str,
        session_id: str,
        channel: str,
    ) -> Tuple[bool, Optional[ApprovalRequest], str]:
        """Atomically verify and consume an approved request."""
        return self.store.claim_and_consume(
            request_id=request_id,
            expected_digest=expected_digest,
            session_id=session_id,
            channel=channel,
        )

    def format_approval_prompt(self, request: ApprovalRequest) -> str:
        """Format an approval request into a clean, human-readable prompt without leaking secrets."""
        ttl_left = max(0, int(request.expires_at - time.time()))
        mins, secs = divmod(ttl_left, 60)
        time_str = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"

        target_display = scrub_secrets(request.canonical_operation.target)
        action_display = scrub_secrets(request.canonical_operation.action_type)

        prompt = (
            "⚠️ **Approval Required**\n\n"
            f"• **Operation:** `{action_display}`\n"
            f"• **Target:** `{target_display}`\n"
            f"• **Risk Tier:** `{request.risk_class}`\n"
            f"• **Request ID:** `{request.request_id}`\n"
            f"• **Validity Window:** {time_str}\n\n"
            f"To authorize this exact operation:\n"
            f"`/approve {request.request_id}`\n\n"
            f"To reject:\n"
            f"`/reject {request.request_id}`"
        )
        return prompt
