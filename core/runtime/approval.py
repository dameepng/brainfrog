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
import math
import os
import secrets
import shutil
import threading
import time
import uuid
from abc import ABC, abstractmethod
from contextlib import nullcontext
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.runtime.targets import (
    TargetClassification,
    classify_target_candidate,
    extract_deterministic_targets,
)

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

from core.runtime.session import scrub_secrets
from core.runtime.capabilities import Capabilities

logger = logging.getLogger(__name__)

CURRENT_APPROVAL_SCHEMA_VERSION = 2
DEFAULT_APPROVAL_TTL_SECONDS = 300.0  # 5 minutes
DEFAULT_MAX_PENDING_PER_SESSION = 20
DEFAULT_MAX_PENDING_PER_REQUESTER = 200
DEFAULT_MAX_PENDING_GLOBAL = 1000
DEFAULT_MAX_TERMINAL_RETENTION = 200
DEFAULT_MAX_PAYLOAD_BYTES = 64 * 1024  # 64 KB
DEFAULT_MAX_CLEANUP_BATCH = 50


class ApprovalQuotaExceededError(ValueError):
    """Raised when an approval request exceeds session, user, or global quota."""

    pass


class ApprovalPayloadTooLargeError(ValueError):
    """Raised when an approval request payload exceeds maximum permitted byte size."""

    pass


class ApprovalStatus(str, Enum):
    """Lifecycle statuses for approval requests."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CONSUMED = "CONSUMED"
    CANCELLED = "CANCELLED"


ACTIVE_APPROVAL_STATUSES = frozenset({ApprovalStatus.PENDING, ApprovalStatus.APPROVED})
TERMINAL_APPROVAL_STATUSES = frozenset({
    ApprovalStatus.REJECTED,
    ApprovalStatus.EXPIRED,
    ApprovalStatus.CONSUMED,
    ApprovalStatus.CANCELLED,
})


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
            clean_params[k] = val
        return {
            "action_type": self.action_type.strip().lower(),
            "target": self.target.strip(),
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
    repo_dir: Optional[Path] = None,
) -> CanonicalOperation:
    """Deterministically extract a CanonicalOperation from raw text and action.

    Remediates M-02 (Heuristic Target Extraction):
    - Rejects version numbers, URLs, packages, issue references, and numbers from filesystem targets.
    - Deterministically validates metadata targets against workspace filesystem rules.
    - Detects ambiguous or non-filesystem tokens and fails closed (target='').
    - Disallows path traversal, drive letters, and UNC paths.
    """
    import re

    meta = metadata or {}
    rdir = repo_dir
    if not rdir and "repo_dir" in meta and meta["repo_dir"]:
        try:
            rdir = Path(meta["repo_dir"])
        except Exception:
            rdir = None

    action_type = meta.get("action_type") or (
        action.value if hasattr(action, "value") else str(action or "generic_action")
    )
    action_type = str(action_type).strip().lower()

    params: Dict[str, Any] = {}
    for k in ("environment", "branch", "force", "mode", "command"):
        if k in meta:
            params[k] = meta[k]

    # Non-filesystem action types have their own explicit target extraction
    if action_type in ("deployment", "deploy_project"):
        raw_target = meta.get("target") or meta.get("path")
        if not raw_target:
            env_match = re.search(r"\b(production|prod|staging|test)\b", text, re.IGNORECASE)
            target = env_match.group(1).lower() if env_match else ""
        else:
            target = str(raw_target).strip().lower()
        return CanonicalOperation(
            action_type=action_type,
            target=target,
            parameters=params,
        )

    if action_type in ("shell_execution", "run_shell_command"):
        raw_target = meta.get("target") or meta.get("command") or text.strip()
        return CanonicalOperation(
            action_type=action_type,
            target=str(raw_target).strip(),
            parameters=params,
        )

    # For filesystem actions (write_code, write_files, read_code, etc.) and generic actions:
    # 1. Check if metadata provided explicit target/path
    meta_target = meta.get("target") or meta.get("path")
    if meta_target:
        cand = classify_target_candidate(str(meta_target).strip(), repo_dir=rdir)
        if cand.is_valid:
            target = cand.normalized
        else:
            # Metadata target is invalid or non-filesystem token -> fail closed
            target = ""
            if cand.classification == TargetClassification.INVALID_PATH:
                params["invalid_path"] = True
            elif cand.classification in (TargetClassification.AMBIGUOUS, TargetClassification.NON_FILESYSTEM_TOKEN):
                params["ambiguous"] = True
            params["target_classification"] = cand.classification.value
            params["target_reason"] = cand.reason
    else:
        # 2. Extract deterministically from text
        valid_targets, candidates = extract_deterministic_targets(text, repo_dir=rdir)
        if valid_targets:
            target = valid_targets[0]
            if len(valid_targets) > 1:
                params["targets"] = valid_targets
        else:
            target = ""
            if any(c.classification == TargetClassification.INVALID_PATH for c in candidates):
                params["invalid_path"] = True
            else:
                params["ambiguous"] = True

    # Multi-targets in metadata
    multi = meta.get("targets") or meta.get("files")
    if isinstance(multi, (list, tuple, set)):
        verified_multi: List[str] = []
        for t in multi:
            tc = classify_target_candidate(str(t).strip(), repo_dir=rdir)
            if tc.is_valid and tc.normalized not in verified_multi:
                verified_multi.append(tc.normalized)
        if verified_multi:
            params["targets"] = verified_multi
            if not target:
                target = verified_multi[0]

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
    session_incarnation_id: Optional[str] = None
    schema_version: int = CURRENT_APPROVAL_SCHEMA_VERSION
    capabilities: Capabilities = field(default_factory=Capabilities)
    authorization_digest: str = ""
    workspace_root: str = ""
    approval_digest: str = ""

    def authorization_payload(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "session_incarnation_id": self.session_incarnation_id,
            "channel": self.channel,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "operation_type": self.operation_type,
            "operation": self.canonical_operation.to_canonical_dict(),
            "operation_digest": self.operation_digest,
            "capabilities": self.capabilities.to_dict(),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
            "workspace_root": self.workspace_root,
        }

    def compute_authorization_digest(self) -> str:
        return CanonicalOperation("execution_contract_v2", "", self.authorization_payload()).compute_digest()

    def integrity_valid(self) -> bool:
        try:
            return (
                self.schema_version == CURRENT_APPROVAL_SCHEMA_VERSION
                and type(self.capabilities) is Capabilities
                and self.operation_digest == self.canonical_operation.compute_digest()
                and bool(self.authorization_digest)
                and secrets.compare_digest(self.authorization_digest, self.compute_authorization_digest())
                and (self.status not in (ApprovalStatus.APPROVED, ApprovalStatus.CONSUMED)
                     or (bool(self.approver_id) and bool(self.approval_digest)
                         and secrets.compare_digest(self.approval_digest, self.compute_approval_digest())))
            )
        except (ValueError, TypeError, AttributeError):
            return False

    def compute_approval_digest(self) -> str:
        return CanonicalOperation("approval_v2", self.authorization_digest, {
            "approver_id": self.approver_id, "approved_at": self.approved_at,
        }).compute_digest()

    def is_expired(self, now: Optional[float] = None) -> bool:
        """Check if request validity window has expired."""
        current_time = now if now is not None else time.time()
        return current_time > self.expires_at

    def to_dict(self) -> Dict[str, Any]:
        """Serialize state to a JSON-safe dictionary with secret scrubbing."""
        from core.runtime.contract import reject_secrets
        reject_secrets(self.authorization_payload())
        return {
            "schema_version": self.schema_version,
            "capabilities": self.capabilities.to_dict(),
            "authorization_digest": self.authorization_digest,
            "workspace_root": self.workspace_root,
            "approval_digest": self.approval_digest,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "session_incarnation_id": self.session_incarnation_id,
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
        version = data.get("schema_version", 1)
        if type(version) is not int or version not in (1, CURRENT_APPROVAL_SCHEMA_VERSION):
            raise ValueError(f"Unsupported approval schema version: {version}")
        if set(data) - set(cls.__dataclass_fields__):
            raise ValueError("Unknown approval fields")

        op_data = data.get("canonical_operation", {})
        if type(op_data) is not dict or set(op_data) - {"action_type", "target", "parameters"}:
            raise ValueError("Unknown or malformed canonical operation fields")
        if type(op_data.get("parameters", {})) is not dict:
            raise ValueError("Malformed canonical operation parameters")
        canon_op = CanonicalOperation(
            action_type=str(op_data.get("action_type", "")),
            target=str(op_data.get("target", "")),
            parameters=op_data.get("parameters", {}),
        )

        raw_status = str(data.get("status", ApprovalStatus.PENDING.value))
        try:
            status = ApprovalStatus(raw_status)
        except ValueError:
            raise ValueError("Invalid approval status") from None

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
            session_incarnation_id=data.get("session_incarnation_id"),
            schema_version=version,
            capabilities=Capabilities.from_dict(data.get("capabilities", {})),
            authorization_digest=data.get("authorization_digest", ""),
            workspace_root=data.get("workspace_root", ""),
            approval_digest=data.get("approval_digest", ""),
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
    def list_requests(
        self,
        session_id: Optional[str] = None,
        requester_id: Optional[str] = None,
        channel: Optional[str] = None,
        limit: Optional[int] = None,
        active_only: bool = True,
    ) -> List[ApprovalRequest]:
        raise NotImplementedError

    @abstractmethod
    def claim_and_consume(
        self,
        request_id: str,
        expected_digest: str,
        session_id: str,
        channel: str,
        session_incarnation_id: Optional[str] = None,
        requester_id: Optional[str] = None,
    ) -> Tuple[bool, Optional[ApprovalRequest], str]:
        """Atomically verify and consume an approved request in a single step."""
        raise NotImplementedError

    def _get_quota_lock(self, timeout: float = 10.0) -> Any:
        return nullcontext()

    def _get_request_lock(self, request_id: str, timeout: float = 10.0) -> Any:
        return nullcontext()

    def cleanup_expired(self, max_items: int = DEFAULT_MAX_CLEANUP_BATCH) -> int:
        return 0


class InMemoryApprovalStore(ApprovalStore):
    """Thread-safe in-memory approval store for unit testing."""

    def __init__(self, max_terminal_retention: int = DEFAULT_MAX_TERMINAL_RETENTION) -> None:
        self._requests: Dict[str, ApprovalRequest] = {}
        self._lock = threading.RLock()
        self.max_terminal_retention = max_terminal_retention

    def _get_quota_lock(self, timeout: float = 10.0) -> Any:
        return self._lock

    def cleanup_expired(self, max_items: int = DEFAULT_MAX_CLEANUP_BATCH) -> int:
        with self._lock:
            now = time.time()
            cleaned = 0
            for req in list(self._requests.values()):
                if cleaned >= max_items:
                    break
                if req.status in ACTIVE_APPROVAL_STATUSES and req.is_expired(now):
                    req.status = ApprovalStatus.EXPIRED
                    cleaned += 1
            return cleaned

    def save(self, request: ApprovalRequest) -> bool:
        with self._lock:
            self._requests[request.request_id] = request
            terminal_keys = [
                k for k, r in self._requests.items()
                if r.status in TERMINAL_APPROVAL_STATUSES
            ]
            if len(terminal_keys) > self.max_terminal_retention:
                excess = len(terminal_keys) - self.max_terminal_retention
                for k in terminal_keys[:excess]:
                    del self._requests[k]
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

    def list_requests(
        self,
        session_id: Optional[str] = None,
        requester_id: Optional[str] = None,
        channel: Optional[str] = None,
        limit: Optional[int] = None,
        active_only: bool = True,
    ) -> List[ApprovalRequest]:
        with self._lock:
            results: List[ApprovalRequest] = []
            for req in list(self._requests.values()):
                if limit is not None and len(results) >= limit:
                    break
                if session_id is not None and req.session_id != session_id:
                    continue
                if requester_id is not None and req.user_id != requester_id.strip():
                    continue
                if channel is not None and req.channel.lower() != channel.lower().strip():
                    continue
                if active_only:
                    if req.status not in ACTIVE_APPROVAL_STATUSES or req.is_expired():
                        continue
                results.append(req)
            return results

    def claim_and_consume(
        self,
        request_id: str,
        expected_digest: str,
        session_id: str,
        channel: str,
        session_incarnation_id: Optional[str] = None,
        requester_id: Optional[str] = None,
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

            if not req.integrity_valid():
                return False, req, "Approval integrity mismatch or legacy authorization (fail closed)."

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

            if requester_id is not None and req.user_id != requester_id.strip():
                return False, req, (
                    f"Requester mismatch: request belongs to user '{req.user_id}', "
                    f"attempted execution by '{requester_id}'."
                )

            # Session incarnation validation (HIGH-02)
            if session_incarnation_id is not None:
                if not req.session_incarnation_id:
                    return False, req, (
                        f"Legacy approval '{request_id}' lacks session incarnation binding (execution rejected)."
                    )
                if req.session_incarnation_id != session_incarnation_id:
                    return False, req, (
                        f"Session incarnation mismatch: request belongs to incarnation '{req.session_incarnation_id}', "
                        f"attempted execution in '{session_incarnation_id}' (stale approval rejected)."
                    )
            elif req.session_incarnation_id is not None:
                return False, req, (
                    f"Session incarnation mismatch: request requires incarnation binding '{req.session_incarnation_id}', "
                    f"but no session incarnation was provided."
                )

            # Atomic one-time state transition
            req.status = ApprovalStatus.CONSUMED
            req.consumed_at = time.time()
            return True, req, "Approval successfully consumed."


class _CrossProcessLock:
    """OS-level advisory file lock for cross-process synchronization.

    Provides cross-process and cross-thread mutual exclusion:
    - Windows: msvcrt.locking with byte-range locking on an open file descriptor
    - POSIX: fcntl.flock with LOCK_EX / LOCK_NB
    - Re-entrant per thread within the same process
    - Automatic OS cleanup if holding process terminates or crashes
    - Configurable acquisition timeout and polling backoff
    """

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
        """Acquire the cross-process lock with re-entrancy and timeout."""
        cur_thread = threading.get_ident()
        if self._owner_thread == cur_thread:
            self._depth += 1
            return True

        start_time = time.monotonic()
        acquired = self._thread_lock.acquire(timeout=self.timeout)
        if not acquired:
            raise TimeoutError(
                f"Timed out after {self.timeout}s waiting for thread lock on {self.lock_path.name}"
            )

        try:
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
                        if self._file:
                            try:
                                self._file.close()
                            except Exception:
                                pass
                            self._file = None
                        raise TimeoutError(
                            f"Timed out after {self.timeout}s waiting for lock on {self.lock_path.name}"
                        )
                    time.sleep(self.poll_interval)

            self._owner_thread = cur_thread
            self._depth = 1
            return True
        except Exception:
            if self._file:
                try:
                    self._file.close()
                except Exception:
                    pass
                self._file = None
            self._thread_lock.release()
            raise

    def release(self) -> None:
        """Release the cross-process lock."""
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

    def __enter__(self) -> _CrossProcessLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()


_PROCESS_LOCKS: Dict[str, _CrossProcessLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


def _get_cross_process_lock(lock_path: Path, timeout: float = 10.0) -> _CrossProcessLock:
    """Get or create the process-singleton _CrossProcessLock for a given canonical path."""
    resolved_key = str(lock_path.resolve())
    with _PROCESS_LOCKS_GUARD:
        if resolved_key not in _PROCESS_LOCKS:
            _PROCESS_LOCKS[resolved_key] = _CrossProcessLock(lock_path, timeout=timeout)
        return _PROCESS_LOCKS[resolved_key]


class FileApprovalStore(ApprovalStore):
    """Filesystem-backed persistent approval store (.brainfrog/approvals/).

    Enforces:
    - Safe deterministic SHA-256 hashed filenames
    - Complete path traversal immunity
    - Atomic writes via tempfile + os.replace
    - Corruption quarantine to .brainfrog/approvals/corrupt/
    - Cross-process and cross-thread mutual exclusion on claim/consume
    - Physical partitioning between active/ and terminal/ approvals
    - Deterministic bounded terminal retention and expired cleanup
    """

    def __init__(
        self,
        repo_dir: Optional[Path] = None,
        approvals_dir: Optional[Path] = None,
        max_terminal_retention: int = DEFAULT_MAX_TERMINAL_RETENTION,
    ) -> None:
        if approvals_dir is not None:
            self.approvals_dir = Path(approvals_dir).resolve()
        else:
            base = Path(repo_dir).resolve() if repo_dir else Path.cwd().resolve()
            self.approvals_dir = base / ".brainfrog" / "approvals"

        self.terminal_dir = self.approvals_dir / "terminal"
        self.corrupt_dir = self.approvals_dir / "corrupt"
        self.locks_dir = self.approvals_dir / ".locks"
        self.max_terminal_retention = max_terminal_retention
        self._lock = threading.RLock()
        self._ensure_directories()
        self._clean_stale_tmp_files()

    def _ensure_directories(self) -> None:
        try:
            self.approvals_dir.mkdir(parents=True, exist_ok=True)
            self.terminal_dir.mkdir(parents=True, exist_ok=True)
            self.corrupt_dir.mkdir(parents=True, exist_ok=True)
            self.locks_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"Could not create approvals directory {self.approvals_dir}: {e}")

    def _clean_stale_tmp_files(self) -> None:
        """Remove any abandoned temporary files older than 60 seconds."""
        try:
            now = time.time()
            for d in (self.approvals_dir, self.terminal_dir):
                if not d.exists():
                    continue
                for f in d.glob(".tmp_*.json"):
                    try:
                        if (now - f.stat().st_mtime) > 60:
                            f.unlink(missing_ok=True)
                    except OSError:
                        pass
        except Exception:
            pass

    def _get_approval_path(self, request_id: str, terminal: bool = False) -> Path:
        """Generate safe deterministic hashed filename from request_id."""
        raw_hash = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32]
        base_dir = self.terminal_dir if terminal else self.approvals_dir
        target_path = (base_dir / f"{raw_hash}.json").resolve()
        if not target_path.is_relative_to(self.approvals_dir):
            raise ValueError(f"Path traversal detected for request_id: {request_id}")
        return target_path

    def _get_lock_path(self, request_id: str) -> Path:
        """Generate safe deterministic lockfile path from request_id."""
        raw_hash = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32]
        target_path = (self.locks_dir / f"{raw_hash}.lock").resolve()
        if not target_path.is_relative_to(self.approvals_dir):
            raise ValueError(f"Path traversal detected for lockfile request_id: {request_id}")
        return target_path

    def _get_request_lock(self, request_id: str, timeout: float = 10.0) -> _CrossProcessLock:
        """Obtain the process-singleton cross-process lock for a given request_id."""
        return _get_cross_process_lock(self._get_lock_path(request_id), timeout=timeout)

    def _get_quota_lock(self, timeout: float = 10.0) -> _CrossProcessLock:
        """Obtain the process-singleton cross-process lock for store-wide quota operations."""
        quota_lock_path = (self.locks_dir / "__store_quota__.lock").resolve()
        return _get_cross_process_lock(quota_lock_path, timeout=timeout)

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

    def _prune_terminal_records(self) -> int:
        """Bounded pruning of terminal approval records to prevent unbounded disk growth."""
        try:
            if not self.terminal_dir.exists():
                return 0
            terminal_files = [f for f in self.terminal_dir.glob("*.json") if not f.name.startswith(".tmp_")]
            if len(terminal_files) <= self.max_terminal_retention:
                return 0
            terminal_files.sort(key=lambda p: p.stat().st_mtime)
            excess = len(terminal_files) - self.max_terminal_retention
            pruned = 0
            for f in terminal_files[:excess]:
                try:
                    f.unlink(missing_ok=True)
                    lock_file = self.locks_dir / f"{f.stem}.lock"
                    if lock_file.exists():
                        try:
                            lock_file.unlink(missing_ok=True)
                        except OSError:
                            pass
                    pruned += 1
                except OSError:
                    pass
            return pruned
        except Exception as e:
            logger.warning(f"Error during terminal records pruning: {e}")
            return 0

    def cleanup_expired(self, max_items: int = DEFAULT_MAX_CLEANUP_BATCH) -> int:
        """Bounded garbage collection of expired active approvals.

        Inspects active approvals, transitions expired requests to EXPIRED (which moves them
        to the terminal directory), and caps total work per invocation at max_items.
        """
        with self._lock:
            if not self.approvals_dir.exists():
                return 0
            now = time.time()
            cleaned = 0
            for f in self.approvals_dir.glob("*.json"):
                if f.name.startswith(".tmp_"):
                    continue
                if cleaned >= max_items:
                    break
                try:
                    content = f.read_text(encoding="utf-8")
                    data = json.loads(content)
                    expires_at = float(data.get("expires_at", 0))
                    if expires_at and expires_at < now:
                        req_id = data.get("request_id")
                        if req_id:
                            req = self.get(req_id)
                            if req and req.is_expired(now):
                                req.status = ApprovalStatus.EXPIRED
                                self.save(req)
                                cleaned += 1
                except Exception:
                    pass
            return cleaned

    def save(self, request: ApprovalRequest) -> bool:
        lock = self._get_request_lock(request.request_id)
        try:
            with lock:
                with self._lock:
                    self._ensure_directories()
                    raw_hash = hashlib.sha256(request.request_id.encode("utf-8")).hexdigest()[:32]
                    is_terminal = request.status in TERMINAL_APPROVAL_STATUSES
                    dest_dir = self.terminal_dir if is_terminal else self.approvals_dir
                    target_file = dest_dir / f"{raw_hash}.json"
                    other_file = (self.approvals_dir if is_terminal else self.terminal_dir) / f"{raw_hash}.json"

                    data = request.to_dict()
                    content = json.dumps(data, indent=2, ensure_ascii=False)

                    tmp_name = f".tmp_{raw_hash[:16]}_{uuid.uuid4().hex[:8]}.json"
                    tmp_file = dest_dir / tmp_name

                    with open(tmp_file, "w", encoding="utf-8") as f:
                        f.write(content)
                        f.flush()
                        try:
                            os.fsync(f.fileno())
                        except (AttributeError, OSError):
                            pass

                    # Unlink from other directory if transitioning
                    if other_file.exists():
                        try:
                            other_file.unlink(missing_ok=True)
                        except OSError:
                            pass

                    os.replace(tmp_file, target_file)

                    if is_terminal:
                        self._prune_terminal_records()

                    return True
        except Exception as e:
            logger.error(f"Failed to save approval request '{request.request_id}': {e}")
            return False

    def get(self, request_id: str) -> Optional[ApprovalRequest]:
        with self._lock:
            # Check active first
            target_file = self._get_approval_path(request_id, terminal=False)
            if not target_file.exists():
                # Check terminal
                target_file = self._get_approval_path(request_id, terminal=True)
                if not target_file.exists():
                    return None

            try:
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
        lock = self._get_request_lock(request_id)
        try:
            with lock:
                with self._lock:
                    deleted = False
                    active_file = self._get_approval_path(request_id, terminal=False)
                    if active_file.exists():
                        active_file.unlink(missing_ok=True)
                        deleted = True
                    terminal_file = self._get_approval_path(request_id, terminal=True)
                    if terminal_file.exists():
                        terminal_file.unlink(missing_ok=True)
                        deleted = True
                    return deleted
        except Exception as e:
            logger.warning(f"Failed to delete approval file for '{request_id}': {e}")
            return False

    def list_requests(
        self,
        session_id: Optional[str] = None,
        requester_id: Optional[str] = None,
        channel: Optional[str] = None,
        limit: Optional[int] = None,
        active_only: bool = True,
    ) -> List[ApprovalRequest]:
        with self._lock:
            requests: List[ApprovalRequest] = []
            if not self.approvals_dir.exists():
                return requests

            # Active requests reside in self.approvals_dir (non-recursive glob)
            candidate_files = [f for f in sorted(self.approvals_dir.glob("*.json")) if not f.name.startswith(".tmp_")]
            if not active_only and self.terminal_dir.exists():
                candidate_files.extend(
                    [f for f in sorted(self.terminal_dir.glob("*.json")) if not f.name.startswith(".tmp_")]
                )

            for f in candidate_files:
                if limit is not None and len(requests) >= limit:
                    break
                try:
                    content = f.read_text(encoding="utf-8")
                    data = json.loads(content)
                    if not isinstance(data, dict) or "request_id" not in data:
                        continue

                    if session_id is not None and data.get("session_id") != session_id:
                        continue
                    if requester_id is not None and data.get("user_id") != requester_id.strip():
                        continue
                    if channel is not None and str(data.get("channel", "")).lower() != channel.lower().strip():
                        continue

                    req = ApprovalRequest.from_dict(data)
                    if active_only:
                        if req.status not in ACTIVE_APPROVAL_STATUSES or req.is_expired():
                            continue

                    requests.append(req)
                except Exception:
                    pass

            return requests

    def claim_and_consume(
        self,
        request_id: str,
        expected_digest: str,
        session_id: str,
        channel: str,
        session_incarnation_id: Optional[str] = None,
        requester_id: Optional[str] = None,
    ) -> Tuple[bool, Optional[ApprovalRequest], str]:
        lock = self._get_request_lock(request_id)
        try:
            with lock:
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

                    if not req.integrity_valid():
                        return False, req, "Approval integrity mismatch or legacy authorization (fail closed)."

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

                    if requester_id is not None and req.user_id != requester_id.strip():
                        return False, req, (
                            f"Requester mismatch: request belongs to user '{req.user_id}', "
                            f"attempted execution by '{requester_id}'."
                        )

                    # Session incarnation validation (HIGH-02)
                    if session_incarnation_id is not None:
                        if not req.session_incarnation_id:
                            return False, req, (
                                f"Legacy approval '{request_id}' lacks session incarnation binding (execution rejected)."
                            )
                        if req.session_incarnation_id != session_incarnation_id:
                            return False, req, (
                                f"Session incarnation mismatch: request belongs to incarnation '{req.session_incarnation_id}', "
                                f"attempted execution in '{session_incarnation_id}' (stale approval rejected)."
                            )
                    elif req.session_incarnation_id is not None:
                        return False, req, (
                            f"Session incarnation mismatch: request requires incarnation binding '{req.session_incarnation_id}', "
                            f"but no session incarnation was provided."
                        )

                    # Atomic one-time state transition
                    req.status = ApprovalStatus.CONSUMED
                    req.consumed_at = time.time()
                    self.save(req)
                    return True, req, "Approval successfully consumed."
        except TimeoutError as te:
            logger.warning(f"Timeout acquiring lock for request '{request_id}': {te}")
            return False, None, f"Lock timeout: request '{request_id}' is currently locked by another process."


class ApprovalService:
    """Channel-agnostic service coordinating approval lifecycle and policy enforcement."""

    def __init__(
        self,
        store: Optional[ApprovalStore] = None,
        default_ttl_seconds: float = DEFAULT_APPROVAL_TTL_SECONDS,
        two_man_rule_enabled: bool = True,
        max_pending_per_session: int = DEFAULT_MAX_PENDING_PER_SESSION,
        max_pending_per_requester: int = DEFAULT_MAX_PENDING_PER_REQUESTER,
        max_pending_global: int = DEFAULT_MAX_PENDING_GLOBAL,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
    ) -> None:
        self.store = store or FileApprovalStore()
        self.default_ttl_seconds = default_ttl_seconds
        self.two_man_rule_enabled = two_man_rule_enabled
        self.max_pending_per_session = max_pending_per_session
        self.max_pending_per_requester = max_pending_per_requester
        self.max_pending_global = max_pending_global
        self.max_payload_bytes = max_payload_bytes

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
        session_incarnation_id: Optional[str] = None,
        capabilities: Optional[Capabilities] = None,
        workspace_root: str = "",
    ) -> ApprovalRequest:
        """Create and persist a new PENDING approval request with secure nonce and quota bounds."""
        now = time.time()
        ttl = self.default_ttl_seconds if ttl_seconds is None else ttl_seconds
        if type(ttl) not in (int, float) or not math.isfinite(ttl):
            raise ValueError("Invalid approval expiration")
        expires_at = now + ttl

        # Cryptographically secure random identifiers
        request_id = f"req_{secrets.token_hex(4)}"
        nonce = secrets.token_hex(16)
        digest = canonical_operation.compute_digest()

        request = ApprovalRequest(
            request_id=request_id,
            session_id=session_id,
            channel=channel.lower().strip(),
            user_id=user_id.strip(),
            conversation_id=conversation_id.strip(),
            operation_type=operation_type,
            canonical_operation=canonical_operation,
            operation_digest=digest,
            risk_class=risk_class,
            created_at=now,
            expires_at=expires_at,
            nonce=nonce,
            status=ApprovalStatus.PENDING,
            session_incarnation_id=session_incarnation_id,
            capabilities=capabilities if capabilities is not None else Capabilities(),
            workspace_root=str(Path(workspace_root).resolve()) if workspace_root else "",
        )
        if type(request.capabilities) is not Capabilities:
            raise ValueError("Capabilities must be a validated domain type")
        request.authorization_digest = request.compute_authorization_digest()
        from core.runtime.contract import reject_secrets
        reject_secrets(request.authorization_payload())

        # 1. Deterministic Payload Size Bound (serialized JSON)
        serialized_bytes = len(json.dumps(request.to_dict(), ensure_ascii=False).encode("utf-8"))
        if serialized_bytes > self.max_payload_bytes:
            raise ApprovalPayloadTooLargeError(
                f"Approval payload size ({serialized_bytes} bytes) exceeds limit "
                f"({self.max_payload_bytes} bytes)."
            )

        # 2. Atomic Quota Enforcement under store quota lock
        quota_lock_ctx = (
            self.store._get_quota_lock()
            if hasattr(self.store, "_get_quota_lock")
            else nullcontext()
        )
        with quota_lock_ctx:
            # Perform bounded expired cleanup to release stale quota slots
            if hasattr(self.store, "cleanup_expired"):
                self.store.cleanup_expired(max_items=DEFAULT_MAX_CLEANUP_BATCH)

            active_requests = self.store.list_requests(active_only=True)

            # Global quota check
            global_count = len(active_requests)
            if global_count >= self.max_pending_global:
                raise ApprovalQuotaExceededError(
                    f"Global pending approval quota exceeded "
                    f"({global_count}/{self.max_pending_global})."
                )

            # Session quota check
            session_count = sum(1 for r in active_requests if r.session_id == session_id and not r.is_expired())
            if session_count >= self.max_pending_per_session:
                raise ApprovalQuotaExceededError(
                    f"Pending approval quota exceeded for session '{session_id}' "
                    f"({session_count}/{self.max_pending_per_session})."
                )

            # Requester quota check
            clean_user = user_id.strip()
            requester_count = sum(1 for r in active_requests if r.user_id == clean_user and not r.is_expired())
            if requester_count >= self.max_pending_per_requester:
                raise ApprovalQuotaExceededError(
                    f"Pending approval quota exceeded for requester '{clean_user}' "
                    f"({requester_count}/{self.max_pending_per_requester})."
                )

            if not self.store.save(request):
                raise IOError(f"Failed to persist approval request '{request.request_id}'")

        return request

    def approve(
        self,
        request_id: str,
        approver_id: str,
        channel: str,
        session_id: Optional[str] = None,
    ) -> Tuple[bool, str, Optional[ApprovalRequest]]:
        """Transition a request from PENDING -> APPROVED with two-man rule validation."""
        lock_ctx = (
            self.store._get_request_lock(request_id)
            if hasattr(self.store, "_get_request_lock")
            else nullcontext()
        )
        with lock_ctx:
            req = self.store.get(request_id)
            if req is None:
                return False, f"Approval request '{request_id}' not found.", None

            if req.is_expired():
                req.status = ApprovalStatus.EXPIRED
                self.store.save(req)
                return False, f"Approval request '{request_id}' has expired.", req

            if req.status != ApprovalStatus.PENDING:
                return False, f"Cannot approve request '{request_id}' with status {req.status.value}.", req

            if not req.integrity_valid():
                return False, "Approval integrity mismatch or legacy authorization.", req

            # Channel verification
            if channel.lower().strip() != req.channel.lower():
                return False, f"Channel mismatch: request belongs to '{req.channel}', cannot approve from '{channel}'.", req

            # Session verification (if enforced)
            if session_id and session_id != req.session_id:
                return False, f"Session mismatch: request belongs to session '{req.session_id}'.", req

            # Two-man rule enforcement
            clean_approver = approver_id.strip()
            if not clean_approver:
                return False, "Approver identity is required.", req
            if self.two_man_rule_enabled and clean_approver == req.user_id:
                return False, "Two-man rule violation: Requester cannot approve their own request.", req

            req.status = ApprovalStatus.APPROVED
            req.approver_id = clean_approver
            req.approved_at = time.time()
            req.approval_digest = req.compute_approval_digest()
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
        lock_ctx = (
            self.store._get_request_lock(request_id)
            if hasattr(self.store, "_get_request_lock")
            else nullcontext()
        )
        with lock_ctx:
            req = self.store.get(request_id)
            if req is None:
                return False, f"Approval request '{request_id}' not found.", None

            if req.status != ApprovalStatus.PENDING:
                return False, f"Cannot reject request '{request_id}' with status {req.status.value}.", req

            clean_approver = approver_id.strip()
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
        lock_ctx = (
            self.store._get_request_lock(request_id)
            if hasattr(self.store, "_get_request_lock")
            else nullcontext()
        )
        with lock_ctx:
            req = self.store.get(request_id)
            if req is None:
                return False, f"Approval request '{request_id}' not found.", None

            if req.status != ApprovalStatus.PENDING:
                return False, f"Cannot cancel request '{request_id}' with status {req.status.value}.", req

            if requester_id.strip() != req.user_id:
                return False, "Only the original requester can cancel a pending request.", req

            req.status = ApprovalStatus.CANCELLED
            self.store.save(req)
            return True, f"Request '{request_id}' has been cancelled.", req

    def invalidate_session_approvals(
        self,
        session_id: str,
        session_incarnation_id: Optional[str] = None,
        reason: str = "Session reset",
    ) -> int:
        """Cancel/invalidate all outstanding (PENDING and APPROVED) approvals for a session."""
        invalidated = 0
        for req in self.store.list_requests(session_id=session_id, active_only=True):
            if req.session_id != session_id:
                continue
            if session_incarnation_id is not None and req.session_incarnation_id and req.session_incarnation_id != session_incarnation_id:
                continue
            if req.status not in ACTIVE_APPROVAL_STATUSES:
                continue

            lock_ctx = (
                self.store._get_request_lock(req.request_id)
                if hasattr(self.store, "_get_request_lock")
                else nullcontext()
            )
            with lock_ctx:
                current = self.store.get(req.request_id)
                if current and current.status in ACTIVE_APPROVAL_STATUSES:
                    current.status = ApprovalStatus.CANCELLED
                    current.rejection_reason = f"Cancelled due to {reason}"
                    self.store.save(current)
                    invalidated += 1
        return invalidated

    def verify_and_consume(
        self,
        request_id: str,
        expected_digest: str,
        session_id: str,
        channel: str,
        session_incarnation_id: Optional[str] = None,
        requester_id: Optional[str] = None,
    ) -> Tuple[bool, Optional[ApprovalRequest], str]:
        """Atomically verify and consume an approved request."""
        return self.store.claim_and_consume(
            request_id=request_id,
            expected_digest=expected_digest,
            session_id=session_id,
            channel=channel,
            session_incarnation_id=session_incarnation_id,
            requester_id=requester_id,
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
            f"Capabilities: `{json.dumps(request.capabilities.to_dict(), sort_keys=True)}`\n"
            "Network access, if granted, covers configured model APIs only. "
            "Shell and Git execution are unsupported under this contract.\n"
            f"`/approve {request.request_id}`\n\n"
            f"To reject:\n"
            f"`/reject {request.request_id}`"
        )
        return prompt
