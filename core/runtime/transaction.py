"""Transaction Domain and Atomic Execution Engine with Rollback.

Defines the transactional execution lifecycle, operation journaling,
pre-mutation snapshotting, verification before commit, reverse rollback,
and crash recovery.

Architectural Invariant:
orchestrator.py remains the SOLE execution engine.
Transaction does NOT grant authority, does NOT create contracts or approvals,
and does NOT execute shell commands or autonomous tools.
It is an execution primitive used BY the orchestrator to achieve atomic filesystem mutations.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import os
import re
import secrets
import shutil
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Protocol, Sequence, Set, Tuple, Union

from core.runtime.contract import ApprovedExecutionContract, reject_secrets
from core.runtime.session import scrub_secrets

logger = logging.getLogger(__name__)


# =============================================================================
# 1. Resource Limits & Enums
# =============================================================================

# Resource Bounds (Phase 15E)
MAX_TRANSACTION_OPERATIONS: int = 100
MAX_SERIALIZED_TRANSACTION_BYTES: int = 5 * 1024 * 1024  # 5 MB
MAX_FILE_SNAPSHOT_BYTES: int = 5 * 1024 * 1024  # 5 MB
MAX_STARTUP_RECOVERY_LIMIT: int = 50


class TransactionResourceLimitError(ValueError):
    """Raised when a transaction or operation exceeds configured resource limits."""
    pass


class TransactionStatus(str, Enum):
    """Lifecycle states for a Transaction."""

    CREATED = "created"
    STAGING = "staging"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    COMMITTED = "committed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


TERMINAL_TRANSACTION_STATUSES: FrozenSet[TransactionStatus] = frozenset({
    TransactionStatus.COMMITTED,
    TransactionStatus.ROLLED_BACK,
    TransactionStatus.FAILED,
})


ALLOWED_TRANSACTION_TRANSITIONS: Dict[TransactionStatus, FrozenSet[TransactionStatus]] = {
    TransactionStatus.CREATED: frozenset({
        TransactionStatus.STAGING,
        TransactionStatus.ROLLING_BACK,
        TransactionStatus.FAILED,
    }),
    TransactionStatus.STAGING: frozenset({
        TransactionStatus.EXECUTING,
        TransactionStatus.ROLLING_BACK,
        TransactionStatus.FAILED,
    }),
    TransactionStatus.EXECUTING: frozenset({
        TransactionStatus.VERIFYING,
        TransactionStatus.ROLLING_BACK,
        TransactionStatus.FAILED,
    }),
    TransactionStatus.VERIFYING: frozenset({
        TransactionStatus.COMMITTED,
        TransactionStatus.ROLLING_BACK,
        TransactionStatus.FAILED,
    }),
    TransactionStatus.ROLLING_BACK: frozenset({
        TransactionStatus.ROLLED_BACK,
        TransactionStatus.FAILED,
    }),
    TransactionStatus.COMMITTED: frozenset(),
    TransactionStatus.ROLLED_BACK: frozenset(),
    TransactionStatus.FAILED: frozenset(),
}


class InvalidTransactionTransition(ValueError):
    """Raised when an illegal transition is attempted on a Transaction."""
    pass


class OperationType(str, Enum):
    """Types of mutations supported by the Transaction Engine."""

    CREATE_FILE = "create_file"
    MODIFY_FILE = "modify_file"
    DELETE_FILE = "delete_file"
    RENAME_FILE = "rename_file"


class OperationStatus(str, Enum):
    """Execution status of an individual operation within a transaction."""

    STAGED = "staged"
    PREPARED = "prepared"
    EXECUTED = "executed"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


def _val(x: Any) -> str:
    """Safely extract string value from Enum or raw string."""
    if isinstance(x, Enum):
        return str(x.value)
    return str(x)


# =============================================================================
# 2. Transaction Operation Model
# =============================================================================

@dataclass
class TransactionOperation:
    """Represents a discrete filesystem mutation within a transaction.

    Holds pre-mutation snapshot (before_state), staged payload (after_state),
    and rollback restoration instructions (rollback_state).
    """

    type: Union[OperationType, str] = OperationType.CREATE_FILE
    target: str = ""
    id: str = field(default_factory=lambda: f"op_{secrets.token_hex(8)}")
    new_target: Optional[str] = None  # Destination for rename_file
    status: Union[OperationStatus, str] = OperationStatus.STAGED
    before_state: Optional[Dict[str, Any]] = None
    after_state: Optional[Dict[str, Any]] = None
    rollback_state: Optional[Dict[str, Any]] = None
    result: Optional[str] = None
    error: Optional[str] = None

    def __init__(
        self,
        type: Optional[Union[OperationType, str]] = None,
        target: Optional[str] = None,
        id: Optional[str] = None,
        new_target: Optional[str] = None,
        status: Union[OperationStatus, str] = OperationStatus.STAGED,
        before_state: Optional[Dict[str, Any]] = None,
        after_state: Optional[Dict[str, Any]] = None,
        rollback_state: Optional[Dict[str, Any]] = None,
        result: Optional[str] = None,
        error: Optional[str] = None,
        *,
        operation_type: Optional[Union[OperationType, str]] = None,
        path: Optional[str] = None,
        new_path: Optional[str] = None,
    ) -> None:
        resolved_type = type if type is not None else operation_type
        if resolved_type is None:
            raise ValueError("Operation type is required")
        if isinstance(resolved_type, str):
            try:
                self.type = OperationType(resolved_type)
            except ValueError:
                self.type = resolved_type
        else:
            self.type = resolved_type

        resolved_target = target if target is not None else path
        if not resolved_target or not isinstance(resolved_target, str):
            raise ValueError("Operation target must be a non-empty string path")
        self.target = resolved_target.replace("\\", "/").strip().lstrip("/")

        self.id = id or f"op_{secrets.token_hex(8)}"
        resolved_new = new_target if new_target is not None else new_path
        self.new_target = resolved_new.replace("\\", "/").strip().lstrip("/") if resolved_new is not None else None
        if isinstance(status, str):
            try:
                self.status = OperationStatus(status)
            except ValueError:
                self.status = status
        else:
            self.status = status
        self.before_state = before_state
        self.after_state = after_state
        self.rollback_state = rollback_state
        self.result = result
        self.error = error

    @property
    def operation_type(self) -> Union[OperationType, str]:
        return self.type

    @property
    def path(self) -> str:
        return self.target

    @property
    def new_path(self) -> Optional[str]:
        return self.new_target

    def to_dict(self) -> Dict[str, Any]:
        """Serialize operation to JSON-safe dictionary."""
        data: Dict[str, Any] = {
            "id": self.id,
            "type": _val(self.type),
            "target": self.target,
            "new_target": self.new_target,
            "status": _val(self.status),
            "before_state": self.before_state,
            "after_state": self.after_state,
            "rollback_state": self.rollback_state,
            "result": self.result,
            "error": self.error,
        }
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TransactionOperation:
        """Deserialize operation with validation."""
        if type(data) is not dict:
            raise ValueError("Expected dictionary for TransactionOperation.from_dict")
        raw_type = data["type"]
        try:
            op_type: Union[OperationType, str] = OperationType(raw_type)
        except ValueError:
            op_type = str(raw_type)
        raw_status = data.get("status", OperationStatus.STAGED.value)
        try:
            op_status: Union[OperationStatus, str] = OperationStatus(raw_status)
        except ValueError:
            op_status = str(raw_status)
        return cls(
            id=data.get("id", f"op_{secrets.token_hex(8)}"),
            type=op_type,
            target=data["target"],
            new_target=data.get("new_target"),
            status=op_status,
            before_state=data.get("before_state"),
            after_state=data.get("after_state"),
            rollback_state=data.get("rollback_state"),
            result=data.get("result"),
            error=data.get("error"),
        )


# =============================================================================
# 3. Transaction Model & Result
# =============================================================================

@dataclass
class Transaction:
    """Represents the execution lifecycle and journal for a batch of operations.

    Transaction coordinates execution atomicity. It is descriptive and operational,
    not authoritative. It cannot authorize or mint execution contracts.
    """

    id: str = field(default_factory=lambda: f"tx_{secrets.token_hex(16)}")
    work_id: Optional[str] = None
    plan_id: Optional[str] = None
    contract_id: Optional[str] = None
    session_id: Optional[str] = None
    workspace: str = ""
    status: TransactionStatus = TransactionStatus.CREATED
    operations: List[TransactionOperation] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    failure_reason: Optional[str] = None
    rollback_reason: Optional[str] = None
    verification_result: Optional[Dict[str, Any]] = None
    commit_marker: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.status, str):
            self.status = TransactionStatus(self.status)
        if not self.id or not isinstance(self.id, str):
            raise ValueError("Transaction id must be a non-empty string")
        if not isinstance(self.started_at, (int, float)) or math.isnan(self.started_at):
            raise ValueError("started_at must be a valid finite number")
        if not isinstance(self.updated_at, (int, float)) or math.isnan(self.updated_at):
            raise ValueError("updated_at must be a valid finite number")

    @property
    def is_terminal(self) -> bool:
        """True if transaction is in a terminal state (COMMITTED, ROLLED_BACK, FAILED)."""
        return self.status in TERMINAL_TRANSACTION_STATUSES

    @property
    def error(self) -> Optional[str]:
        return self.failure_reason

    @property
    def rollback_error(self) -> Optional[str]:
        return self.rollback_reason

    @property
    def committed_at(self) -> Optional[float]:
        return self.completed_at if self.status == TransactionStatus.COMMITTED else None

    @property
    def rolled_back_at(self) -> Optional[float]:
        return self.completed_at if self.status == TransactionStatus.ROLLED_BACK else None

    @property
    def created_at(self) -> float:
        return self.started_at

    def transition(
        self,
        new_status: Union[TransactionStatus, str],
        *,
        reason: Optional[str] = None,
        verification_result: Optional[Dict[str, Any]] = None,
        now: Optional[float] = None,
    ) -> Transaction:
        """Validate and apply a lifecycle transition."""
        target_status = TransactionStatus(new_status) if isinstance(new_status, str) else new_status
        if self.is_terminal:
            raise InvalidTransactionTransition(
                f"Cannot transition terminal transaction '{self.id}' from '{self.status.value}' to '{target_status.value}'"
            )

        allowed = ALLOWED_TRANSACTION_TRANSITIONS.get(self.status, frozenset())
        if target_status not in allowed and target_status != self.status:
            raise InvalidTransactionTransition(
                f"Illegal transaction transition for '{self.id}': '{self.status.value}' -> '{target_status.value}'"
            )

        current_time = float(now) if now is not None else time.time()
        prior_status = self.status
        self.status = target_status
        self.updated_at = current_time

        if target_status in TERMINAL_TRANSACTION_STATUSES and self.completed_at is None:
            self.completed_at = current_time

        if reason:
            if target_status == TransactionStatus.ROLLING_BACK:
                if self.failure_reason is None:
                    self.failure_reason = reason
                else:
                    self.rollback_reason = reason
            elif target_status == TransactionStatus.ROLLED_BACK:
                self.rollback_reason = reason
            elif target_status == TransactionStatus.FAILED:
                if prior_status == TransactionStatus.ROLLING_BACK:
                    self.rollback_reason = reason
                else:
                    self.failure_reason = reason

        if verification_result is not None:
            self.verification_result = verification_result

        return self

    def to_dict(self) -> Dict[str, Any]:
        """Serialize transaction to JSON-safe dictionary with secret scrubbing."""
        data: Dict[str, Any] = {
            "id": self.id,
            "work_id": self.work_id,
            "plan_id": self.plan_id,
            "contract_id": self.contract_id,
            "session_id": self.session_id,
            "workspace": self.workspace,
            "status": self.status.value,
            "operations": [op.to_dict() for op in self.operations],
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "failure_reason": scrub_secrets(self.failure_reason) if self.failure_reason else None,
            "rollback_reason": scrub_secrets(self.rollback_reason) if self.rollback_reason else None,
            "verification_result": self.verification_result,
            "commit_marker": self.commit_marker,
            "metadata": {k: scrub_secrets(str(v)) if isinstance(v, str) else v for k, v in self.metadata.items()},
            "error": scrub_secrets(self.failure_reason) if self.failure_reason else None,
        }
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Transaction:
        """Deserialize transaction from dictionary."""
        if type(data) is not dict:
            raise ValueError("Expected dictionary for Transaction.from_dict")
        raw_ops = data.get("operations", [])
        operations = [TransactionOperation.from_dict(o) for o in raw_ops]
        return cls(
            id=data["id"],
            work_id=data.get("work_id"),
            plan_id=data.get("plan_id"),
            contract_id=data.get("contract_id"),
            session_id=data.get("session_id"),
            workspace=data.get("workspace", ""),
            status=TransactionStatus(data["status"]),
            operations=operations,
            started_at=float(data["started_at"]),
            updated_at=float(data["updated_at"]),
            completed_at=float(data["completed_at"]) if data.get("completed_at") is not None else None,
            failure_reason=data.get("failure_reason") or data.get("error"),
            rollback_reason=data.get("rollback_reason"),
            verification_result=data.get("verification_result"),
            commit_marker=data.get("commit_marker"),
            metadata=data.get("metadata", {}),
        )


@dataclass(frozen=True)
class RecoveryResult:
    """Structured immutable outcome of a crash recovery attempt."""

    transaction_id: str
    previous_status: TransactionStatus
    final_status: TransactionStatus
    recovered: bool
    rolled_back: bool
    ambiguous: bool = False
    error: Optional[str] = None
    operations_recovered: int = 0

    @property
    def success(self) -> bool:
        return self.recovered


@dataclass(frozen=True)
class TransactionResult:
    """Structured immutable outcome of a transaction execution or rollback."""

    transaction_id: str
    status: TransactionStatus
    committed: bool
    rolled_back: bool
    operations: Tuple[TransactionOperation, ...]
    verification: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    rollback_error: Optional[str] = None
    recovery: Optional[RecoveryResult] = None

    @property
    def success(self) -> bool:
        return self.committed


# =============================================================================
# 4. Verification Abstraction
# =============================================================================

class TransactionVerifier(Protocol):
    """Protocol for post-execution verification before transaction commit."""

    def verify(self, tx: Transaction, workspace: Path) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        """Verify the post-execution workspace state.

        Returns (passed, optional_error_message, details_dictionary).
        """
        ...


class BasicFilesystemVerifier:
    """Deterministic default verifier validating filesystem state after execution."""

    def verify(self, tx: Transaction, workspace: Path) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        details: Dict[str, Any] = {"verified_operations": len(tx.operations), "checks": []}
        for op in tx.operations:
            if op.status != OperationStatus.EXECUTED:
                continue

            target_path = (workspace / op.target).resolve()
            if not target_path.is_relative_to(workspace.resolve()):
                return False, f"Target path '{op.target}' escapes workspace boundary", details

            if op.type in (OperationType.CREATE_FILE, OperationType.MODIFY_FILE):
                if not target_path.exists() or not target_path.is_file():
                    err = f"Expected file '{op.target}' does not exist after execution"
                    details["checks"].append({"target": op.target, "passed": False, "reason": err})
                    return False, err, details

                # Verify content matches after_state if recorded
                if op.after_state and "content" in op.after_state:
                    expected = op.after_state["content"]
                    actual = target_path.read_text(encoding="utf-8", errors="replace")
                    if actual != expected:
                        err = f"Content of '{op.target}' does not match expected staged state"
                        details["checks"].append({"target": op.target, "passed": False, "reason": err})
                        return False, err, details

            elif op.type == OperationType.DELETE_FILE:
                if target_path.exists():
                    err = f"Deleted file '{op.target}' still exists on disk"
                    details["checks"].append({"target": op.target, "passed": False, "reason": err})
                    return False, err, details

            elif op.type == OperationType.RENAME_FILE:
                if op.new_target is None:
                    return False, f"Rename operation '{op.id}' has no new_target specified", details
                new_path = (workspace / op.new_target).resolve()
                if not new_path.is_relative_to(workspace.resolve()):
                    return False, f"Rename destination '{op.new_target}' escapes workspace", details
                if not new_path.exists():
                    err = f"Renamed destination file '{op.new_target}' does not exist"
                    details["checks"].append({"target": op.new_target, "passed": False, "reason": err})
                    return False, err, details
                if target_path.exists():
                    err = f"Original renamed file '{op.target}' still exists after rename"
                    details["checks"].append({"target": op.target, "passed": False, "reason": err})
                    return False, err, details

            details["checks"].append({"target": op.target, "type": _val(op.type), "passed": True})

        return True, None, details


# =============================================================================
# 5. Transaction Store & Crash Recovery
# =============================================================================

class TransactionStore(Protocol):
    """Protocol for persisting and retrieving transactions."""

    def save(self, tx: Transaction) -> bool: ...
    def get(self, tx_id: str) -> Optional[Transaction]: ...
    def delete(self, tx_id: str) -> bool: ...
    def list_all(self) -> List[Transaction]: ...
    def list_incomplete(self) -> List[Transaction]: ...
    def recoverable_transactions(self, limit: Optional[int] = None) -> List[Transaction]: ...
    def list(self, session_id: Optional[str] = None, limit: Optional[int] = None) -> List[Transaction]: ...


class InMemoryTransactionStore:
    """Thread-safe in-memory store for unit testing."""

    def __init__(self) -> None:
        self._records: Dict[str, Transaction] = {}
        self._lock = threading.RLock()

    def save(self, tx: Transaction) -> bool:
        with self._lock:
            # Check serialized size limit
            data = tx.to_dict()
            serialized = json.dumps(data)
            if len(serialized.encode("utf-8")) > MAX_SERIALIZED_TRANSACTION_BYTES:
                raise TransactionResourceLimitError(
                    f"Transaction '{tx.id}' serialized payload exceeds limit ({MAX_SERIALIZED_TRANSACTION_BYTES} bytes)"
                )
            # Store a copy to preserve immutability
            self._records[tx.id] = Transaction.from_dict(data)
            return True

    def get(self, tx_id: str) -> Optional[Transaction]:
        with self._lock:
            record = self._records.get(tx_id)
            return Transaction.from_dict(record.to_dict()) if record else None

    def delete(self, tx_id: str) -> bool:
        with self._lock:
            return self._records.pop(tx_id, None) is not None

    def list_all(self) -> List[Transaction]:
        with self._lock:
            return [Transaction.from_dict(r.to_dict()) for r in self._records.values()]

    def list_incomplete(self) -> List[Transaction]:
        with self._lock:
            return [
                Transaction.from_dict(r.to_dict())
                for r in self._records.values()
                if not r.is_terminal
            ]

    def recoverable_transactions(self, limit: Optional[int] = None) -> List[Transaction]:
        with self._lock:
            incomplete = self.list_incomplete()
            incomplete.sort(key=lambda t: t.started_at)
            if limit is not None:
                incomplete = incomplete[:limit]
            return incomplete

    def list(self, session_id: Optional[str] = None, limit: Optional[int] = None) -> List[Transaction]:
        txs = self.list_all()
        if session_id:
            txs = [t for t in txs if t.session_id == session_id]
        txs.sort(key=lambda t: t.started_at, reverse=True)
        if limit is not None:
            txs = txs[:limit]
        return txs


class FileTransactionStore:
    """File-backed transaction store using atomic JSON writes in .brainfrog/transactions/."""

    def __init__(self, workspace: Union[str, Path]) -> None:
        self.workspace = Path(workspace).resolve()
        self.tx_dir = self.workspace / ".brainfrog" / "transactions"
        self.tx_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _get_path(self, tx_id: str) -> Path:
        raw_hash = hashlib.sha256(tx_id.encode("utf-8")).hexdigest()[:32]
        target = (self.tx_dir / f"{raw_hash}.json").resolve()
        if not target.is_relative_to(self.tx_dir):
            raise ValueError(f"Path traversal detected in transaction ID: {tx_id}")
        return target

    def save(self, tx: Transaction) -> bool:
        with self._lock:
            target_path = self._get_path(tx.id)
            tmp_path = self.tx_dir / f".tmp_{tx.id}_{secrets.token_hex(4)}.json"
            try:
                data = tx.to_dict()
                serialized = json.dumps(data, indent=2, sort_keys=True)
                if len(serialized.encode("utf-8")) > MAX_SERIALIZED_TRANSACTION_BYTES:
                    raise TransactionResourceLimitError(
                        f"Transaction '{tx.id}' serialized payload exceeds limit ({MAX_SERIALIZED_TRANSACTION_BYTES} bytes)"
                    )
                with open(tmp_path, "w", encoding="utf-8") as f:
                    f.write(serialized)
                    f.flush()
                    os.fsync(f.fileno())
                tmp_path.replace(target_path)
                return True
            except TransactionResourceLimitError:
                if tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
                raise
            except Exception as e:
                logger.error(f"Failed to persist transaction '{tx.id}': {e}")
                if tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
                return False

    def get(self, tx_id: str) -> Optional[Transaction]:
        with self._lock:
            try:
                target_path = self._get_path(tx_id)
            except ValueError:
                return None
            if not target_path.exists():
                return None
            try:
                data = json.loads(target_path.read_text(encoding="utf-8"))
                return Transaction.from_dict(data)
            except Exception as e:
                logger.error(f"Failed to load transaction '{tx_id}': {e}")
                return None

    def delete(self, tx_id: str) -> bool:
        with self._lock:
            try:
                target_path = self._get_path(tx_id)
            except ValueError:
                return False
            if target_path.exists():
                target_path.unlink(missing_ok=True)
                return True
            return False

    def list_all(self) -> List[Transaction]:
        with self._lock:
            results: List[Transaction] = []
            for file in self.tx_dir.glob("*.json"):
                if file.name.startswith(".tmp_"):
                    continue
                try:
                    data = json.loads(file.read_text(encoding="utf-8"))
                    results.append(Transaction.from_dict(data))
                except Exception:
                    continue
            return results

    def list_incomplete(self) -> List[Transaction]:
        return [tx for tx in self.list_all() if not tx.is_terminal]

    def recoverable_transactions(self, limit: Optional[int] = None) -> List[Transaction]:
        incomplete = self.list_incomplete()
        incomplete.sort(key=lambda t: t.started_at)
        if limit is not None:
            incomplete = incomplete[:limit]
        return incomplete

    def list(self, session_id: Optional[str] = None, limit: Optional[int] = None) -> List[Transaction]:
        txs = self.list_all()
        if session_id:
            txs = [t for t in txs if t.session_id == session_id]
        txs.sort(key=lambda t: t.started_at, reverse=True)
        if limit is not None:
            txs = txs[:limit]
        return txs


# =============================================================================
# 6. Transaction Coordinator & Execution Engine
# =============================================================================

class TransactionCoordinator:
    """Coordinates atomic staging, execution, verification, and rollback of filesystem operations.

    Operates strictly within the boundary of an authorized workspace and execution contract.
    Does not provide autonomous tools or shell authority.
    """

    def __init__(
        self,
        workspace: Union[str, Path],
        contract: Optional[ApprovedExecutionContract] = None,
        work_id: Optional[str] = None,
        plan_id: Optional[str] = None,
        session_id: Optional[str] = None,
        tx_id: Optional[str] = None,
        store: Optional[TransactionStore] = None,
        verifier: Optional[TransactionVerifier] = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.contract = contract
        self.store = store
        self.verifier = verifier or BasicFilesystemVerifier()

        self.tx = Transaction(
            id=tx_id or f"tx_{secrets.token_hex(16)}",
            work_id=work_id,
            plan_id=plan_id,
            contract_id=getattr(contract, "request_id", None) if contract else None,
            session_id=session_id,
            workspace=str(self.workspace),
            status=TransactionStatus.CREATED,
        )

    @property
    def current_transaction(self) -> Transaction:
        return self.tx

    def stage_operation(
        self,
        type: Union[OperationType, str],
        target: str,
        after_state: Any = None,
        new_target: Optional[str] = None,
    ) -> TransactionOperation:
        """Stage an operation by type with automatic begin() if needed."""
        if len(self.tx.operations) >= MAX_TRANSACTION_OPERATIONS:
            raise TransactionResourceLimitError(
                f"Cannot stage operation: transaction '{self.tx.id}' reached maximum operation limit ({MAX_TRANSACTION_OPERATIONS})"
            )
        if self.tx.status == TransactionStatus.CREATED:
            self.begin()
        op_type = OperationType(type) if isinstance(type, str) else type
        if op_type == OperationType.CREATE_FILE:
            return self.stage_create(target, after_state or "")
        elif op_type == OperationType.MODIFY_FILE:
            return self.stage_modify(target, after_state or "")
        elif op_type == OperationType.DELETE_FILE:
            return self.stage_delete(target)
        elif op_type == OperationType.RENAME_FILE:
            dest = new_target or str(after_state)
            return self.stage_rename(target, dest)
        raise ValueError(f"Unsupported operation type: {type}")

    def _resolve_and_validate(self, rel_path: str) -> Path:
        """Normalize path and assert workspace containment and contract authority."""
        if not isinstance(rel_path, str) or not rel_path.strip():
            raise PermissionError("Path validation denied: empty or invalid path.")
        if "\x00" in rel_path:
            raise PermissionError(f"Path validation denied: null byte in path '{rel_path}'.")

        clean = rel_path.replace("\\", "/").strip().lstrip("/")
        target_path = (self.workspace / clean).resolve()

        if not target_path.is_relative_to(self.workspace):
            raise PermissionError(
                f"Path traversal denied: path '{rel_path}' escapes workspace '{self.workspace}'."
            )

        if self.contract is not None:
            self.contract.require_filesystem("write", clean, self.workspace)

        return target_path

    def _capture_snapshot(self, path: Path) -> Dict[str, Any]:
        """Capture the current filesystem state of a target for rollback."""
        if not path.exists():
            return {"exists": False}

        if path.is_file():
            size = path.stat().st_size
            if size > MAX_FILE_SNAPSHOT_BYTES:
                raise TransactionResourceLimitError(
                    f"File '{path}' size ({size} bytes) exceeds snapshot limit ({MAX_FILE_SNAPSHOT_BYTES} bytes)"
                )
            try:
                # Attempt reading as UTF-8 string
                text = path.read_text(encoding="utf-8")
                return {"exists": True, "is_text": True, "content": text}
            except UnicodeDecodeError:
                # Binary fallback
                raw_bytes = path.read_bytes()
                return {
                    "exists": True,
                    "is_text": False,
                    "content_b64": base64.b64encode(raw_bytes).decode("ascii"),
                }
        return {"exists": True, "is_dir": True}

    def begin(self) -> Transaction:
        """Begin the transaction and transition to STAGING."""
        self.tx.transition(TransactionStatus.STAGING)
        if self.store:
            self.store.save(self.tx)
        return self.tx

    def _check_op_limit(self) -> None:
        if len(self.tx.operations) >= MAX_TRANSACTION_OPERATIONS:
            raise TransactionResourceLimitError(
                f"Cannot stage operation: transaction '{self.tx.id}' reached maximum operation limit ({MAX_TRANSACTION_OPERATIONS})"
            )

    def stage_create(self, target: str, content: Union[str, bytes]) -> TransactionOperation:
        """Stage a file creation mutation."""
        self._check_op_limit()
        if self.tx.status == TransactionStatus.CREATED:
            self.begin()
        if self.tx.status != TransactionStatus.STAGING:
            raise InvalidTransactionTransition("Cannot stage operations outside STAGING state")

        content_bytes_len = len(content.encode("utf-8") if isinstance(content, str) else content)
        if content_bytes_len > MAX_FILE_SNAPSHOT_BYTES:
            raise TransactionResourceLimitError(
                f"Content size ({content_bytes_len} bytes) for target '{target}' exceeds snapshot limit ({MAX_FILE_SNAPSHOT_BYTES} bytes)"
            )

        target_path = self._resolve_and_validate(target)
        before = self._capture_snapshot(target_path)

        after: Dict[str, Any]
        if isinstance(content, str):
            after = {"is_text": True, "content": content}
        else:
            after = {"is_text": False, "content_b64": base64.b64encode(content).decode("ascii")}

        rollback = {"action": "delete", "target": target}
        op = TransactionOperation(
            type=OperationType.CREATE_FILE,
            target=target,
            status=OperationStatus.STAGED,
            before_state=before,
            after_state=after,
            rollback_state=rollback,
        )
        self.tx.operations.append(op)
        if self.store:
            self.store.save(self.tx)
        return op

    def stage_modify(self, target: str, content: Union[str, bytes]) -> TransactionOperation:
        """Stage a file modification mutation."""
        self._check_op_limit()
        if self.tx.status == TransactionStatus.CREATED:
            self.begin()
        if self.tx.status != TransactionStatus.STAGING:
            raise InvalidTransactionTransition("Cannot stage operations outside STAGING state")

        content_bytes_len = len(content.encode("utf-8") if isinstance(content, str) else content)
        if content_bytes_len > MAX_FILE_SNAPSHOT_BYTES:
            raise TransactionResourceLimitError(
                f"Content size ({content_bytes_len} bytes) for target '{target}' exceeds snapshot limit ({MAX_FILE_SNAPSHOT_BYTES} bytes)"
            )

        target_path = self._resolve_and_validate(target)
        before = self._capture_snapshot(target_path)

        after: Dict[str, Any]
        if isinstance(content, str):
            after = {"is_text": True, "content": content}
        else:
            after = {"is_text": False, "content_b64": base64.b64encode(content).decode("ascii")}

        rollback = {"action": "restore", "target": target, "state": before}
        op = TransactionOperation(
            type=OperationType.MODIFY_FILE,
            target=target,
            status=OperationStatus.STAGED,
            before_state=before,
            after_state=after,
            rollback_state=rollback,
        )
        self.tx.operations.append(op)
        if self.store:
            self.store.save(self.tx)
        return op

    def stage_delete(self, target: str) -> TransactionOperation:
        """Stage a file deletion mutation."""
        self._check_op_limit()
        if self.tx.status == TransactionStatus.CREATED:
            self.begin()
        if self.tx.status != TransactionStatus.STAGING:
            raise InvalidTransactionTransition("Cannot stage operations outside STAGING state")

        target_path = self._resolve_and_validate(target)
        before = self._capture_snapshot(target_path)

        rollback = {"action": "restore", "target": target, "state": before}
        op = TransactionOperation(
            type=OperationType.DELETE_FILE,
            target=target,
            status=OperationStatus.STAGED,
            before_state=before,
            after_state={"exists": False},
            rollback_state=rollback,
        )
        self.tx.operations.append(op)
        if self.store:
            self.store.save(self.tx)
        return op

    def stage_rename(self, target: str, new_target: str) -> TransactionOperation:
        """Stage a file rename mutation."""
        self._check_op_limit()
        if self.tx.status == TransactionStatus.CREATED:
            self.begin()
        if self.tx.status != TransactionStatus.STAGING:
            raise InvalidTransactionTransition("Cannot stage operations outside STAGING state")

        _ = self._resolve_and_validate(target)
        _ = self._resolve_and_validate(new_target)
        before = self._capture_snapshot(self.workspace / target)

        rollback = {"action": "rename", "source": new_target, "destination": target}
        op = TransactionOperation(
            type=OperationType.RENAME_FILE,
            target=target,
            new_target=new_target,
            status=OperationStatus.STAGED,
            before_state=before,
            after_state={"new_target": new_target},
            rollback_state=rollback,
        )
        self.tx.operations.append(op)
        if self.store:
            self.store.save(self.tx)
        return op

    def stage_write_batch(self, files: Dict[str, Union[str, bytes]]) -> List[TransactionOperation]:
        """Stage a batch of file writes, automatically discerning create vs modify."""
        staged: List[TransactionOperation] = []
        for path_str, content in files.items():
            target_path = self._resolve_and_validate(path_str)
            clean_rel = target_path.relative_to(self.workspace).as_posix()
            if target_path.exists():
                op = self.stage_modify(clean_rel, content)
            else:
                op = self.stage_create(clean_rel, content)
            staged.append(op)
        return staged

    def execute(self) -> bool:
        """Execute all staged operations sequentially.

        If any operation fails, immediately triggers reverse rollback.
        """
        self.tx.transition(TransactionStatus.EXECUTING)
        if self.store:
            self.store.save(self.tx)

        for op in self.tx.operations:
            if op.status != OperationStatus.STAGED:
                continue

            target_path = self.workspace / op.target
            try:
                # WRITE-AHEAD: Persist PREPARED operation state before filesystem mutation
                op.status = OperationStatus.PREPARED
                if self.store:
                    self.store.save(self.tx)

                if op.type in (OperationType.CREATE_FILE, OperationType.MODIFY_FILE):
                    if not op.after_state:
                        raise ValueError(f"Operation '{op.id}' has no after_state staged")
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    if op.after_state.get("is_text", True):
                        target_path.write_text(op.after_state["content"], encoding="utf-8")
                    else:
                        raw = base64.b64decode(op.after_state["content_b64"].encode("ascii"))
                        target_path.write_bytes(raw)

                elif op.type == OperationType.DELETE_FILE:
                    if target_path.exists():
                        target_path.unlink()

                elif op.type == OperationType.RENAME_FILE:
                    if not op.new_target:
                        raise ValueError(f"Rename operation '{op.id}' lacks new_target")
                    dest_path = self.workspace / op.new_target
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(target_path), str(dest_path))

                # MUTATION COMPLETE: Persist EXECUTED status to durable journal
                op.status = OperationStatus.EXECUTED
                op.result = "success"
                if self.store:
                    self.store.save(self.tx)

            except Exception as exc:
                err_msg = f"Operation '{op.id}' ({_val(op.type)} on '{op.target}') failed: {exc}"
                logger.error(err_msg)
                op.status = OperationStatus.FAILED
                op.error = err_msg
                self.tx.failure_reason = err_msg
                if self.store:
                    self.store.save(self.tx)

                # Reverse rollback on execution failure
                self.rollback(reason=err_msg)
                return False

        return True

    def verify(self) -> bool:
        """Verify the post-execution state before committing.

        If verification fails, triggers reverse rollback.
        """
        self.tx.transition(TransactionStatus.VERIFYING)
        if self.store:
            self.store.save(self.tx)

        passed, err_msg, details = self.verifier.verify(self.tx, self.workspace)
        self.tx.verification_result = details

        if not passed:
            reason = f"Verification failed: {err_msg}"
            logger.warning(f"Transaction '{self.tx.id}' verification failed: {reason}")
            self.tx.failure_reason = reason
            if self.store:
                self.store.save(self.tx)
            self.rollback(reason=reason)
            return False

        # Record verified state in durable journal
        details["passed"] = True
        self.tx.verification_result = details
        if self.store:
            self.store.save(self.tx)
        return True

    def commit(self) -> TransactionResult:
        """Commit the verified transaction to COMMITTED state.

        Writes a durable commit marker before transitioning to COMMITTED.
        """
        marker_time = time.time()
        self.tx.commit_marker = {
            "marked_at": marker_time,
            "status": "committed",
            "transaction_id": self.tx.id,
            "work_id": self.tx.work_id,
            "plan_id": self.tx.plan_id,
        }
        if self.store:
            self.store.save(self.tx)

        self.tx.transition(TransactionStatus.COMMITTED)
        if self.store:
            self.store.save(self.tx)

        return TransactionResult(
            transaction_id=self.tx.id,
            status=self.tx.status,
            committed=True,
            rolled_back=False,
            operations=tuple(self.tx.operations),
            verification=self.tx.verification_result,
        )

    def run(self) -> TransactionResult:
        """Execute complete transactional lifecycle: execute -> verify -> commit (or rollback)."""
        if not self.execute():
            return TransactionResult(
                transaction_id=self.tx.id,
                status=self.tx.status,
                committed=False,
                rolled_back=(self.tx.status == TransactionStatus.ROLLED_BACK),
                operations=tuple(self.tx.operations),
                error=self.tx.failure_reason,
                rollback_error=self.tx.rollback_reason,
            )
        if not self.verify():
            return TransactionResult(
                transaction_id=self.tx.id,
                status=self.tx.status,
                committed=False,
                rolled_back=(self.tx.status == TransactionStatus.ROLLED_BACK),
                operations=tuple(self.tx.operations),
                error=self.tx.failure_reason,
                rollback_error=self.tx.rollback_reason,
            )
        return self.commit()

    def _rollback_operation(self, op: TransactionOperation) -> bool:
        """Roll back a single executed operation."""
        target_path = self.workspace / op.target
        if op.type == OperationType.CREATE_FILE:
            # Rollback creation: delete created file
            if target_path.exists():
                target_path.unlink()

        elif op.type in (OperationType.MODIFY_FILE, OperationType.DELETE_FILE):
            # Rollback modify/delete: restore previous content
            before = op.before_state or {}
            if not before.get("exists", False):
                if target_path.exists():
                    target_path.unlink()
            else:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                if before.get("is_text", True):
                    target_path.write_text(before.get("content", ""), encoding="utf-8")
                else:
                    raw = base64.b64decode(before.get("content_b64", "").encode("ascii"))
                    target_path.write_bytes(raw)

        elif op.type == OperationType.RENAME_FILE:
            # Rollback rename: move from destination back to source
            if op.new_target:
                dest_path = self.workspace / op.new_target
                if dest_path.exists():
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(dest_path), str(target_path))

        op.status = OperationStatus.ROLLED_BACK
        op.result = "rolled_back"
        return True

    def rollback(self, reason: str = "Rollback requested") -> TransactionResult:
        """Roll back all executed operations in strict reverse order (LIFO)."""
        self.tx.transition(TransactionStatus.ROLLING_BACK, reason=reason)
        if self.store:
            self.store.save(self.tx)

        rollback_errors: List[str] = []
        # Rollback in strict reverse order: last executed -> first executed
        executed_ops = [op for op in self.tx.operations if op.status == OperationStatus.EXECUTED]
        for op in reversed(executed_ops):
            try:
                self._rollback_operation(op)
                if self.store:
                    self.store.save(self.tx)

            except Exception as r_exc:
                r_err = f"Failed to rollback operation '{op.id}' ({_val(op.type)} on '{op.target}'): {r_exc}"
                logger.error(r_err)
                rollback_errors.append(r_err)
                op.error = (op.error or "") + f" | Rollback error: {r_exc}"
                if self.store:
                    self.store.save(self.tx)

        if rollback_errors:
            combined_rb_err = "; ".join(rollback_errors)
            self.tx.transition(TransactionStatus.FAILED, reason=combined_rb_err)
            if self.store:
                self.store.save(self.tx)
            return TransactionResult(
                transaction_id=self.tx.id,
                status=self.tx.status,
                committed=False,
                rolled_back=False,
                operations=tuple(self.tx.operations),
                verification=self.tx.verification_result,
                error=self.tx.failure_reason,
                rollback_error=combined_rb_err,
            )

        self.tx.transition(TransactionStatus.ROLLED_BACK)
        if self.store:
            self.store.save(self.tx)
        return TransactionResult(
            transaction_id=self.tx.id,
            status=self.tx.status,
            committed=False,
            rolled_back=True,
            operations=tuple(self.tx.operations),
            verification=self.tx.verification_result,
            error=self.tx.failure_reason,
        )


class TransactionRecoveryManager:
    """Manages deterministic crash recovery for interrupted or abandoned transactions.

    Responsibilities:
    - Discover incomplete transactions in store.
    - Inspect durable journals and write-ahead operation states.
    - Distinguish between verified-only and durably-committed transactions.
    - Execute reverse-order rollback for supported filesystem mutations.
    - Detect ambiguous filesystem states and fail closed without guessing.
    - Ensure idempotent recovery across multiple crashes/restarts.

    Strict Architectural Boundaries:
    - Does NOT call LLM (System 1 / System 2).
    - Does NOT create Plans or ApprovalRequests.
    - Does NOT mint ApprovedExecutionContract or capabilities.
    - Does NOT execute arbitrary shell commands or git push.
    - Solely reconciles local filesystem and transaction journal state.
    """

    def __init__(
        self,
        workspace: Union[str, Path],
        store: TransactionStore,
        verifier: Optional[TransactionVerifier] = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.store = store
        self.verifier = verifier or BasicFilesystemVerifier()

    def discover_incomplete(self, limit: Optional[int] = MAX_STARTUP_RECOVERY_LIMIT) -> List[Transaction]:
        """Discover incomplete transactions requiring recovery."""
        incomplete = self.store.list_incomplete()
        incomplete.sort(key=lambda t: t.started_at)
        if limit is not None:
            incomplete = incomplete[:limit]
        return incomplete

    def _inspect_target_state(self, path: Path) -> Dict[str, Any]:
        """Inspect the current on-disk state of a target path."""
        if not path.exists():
            return {"exists": False}
        if path.is_file():
            try:
                content = path.read_text(encoding="utf-8")
                return {"exists": True, "is_text": True, "content": content}
            except UnicodeDecodeError:
                raw_bytes = path.read_bytes()
                return {
                    "exists": True,
                    "is_text": False,
                    "content_b64": base64.b64encode(raw_bytes).decode("ascii"),
                }
        return {"exists": True, "is_dir": True}

    def _states_equal(self, actual: Dict[str, Any], expected: Optional[Dict[str, Any]]) -> bool:
        """Compare actual filesystem state with expected recorded state dictionary."""
        if expected is None:
            return False
        expected_exists = expected.get("exists")
        if expected_exists is None:
            # If expected specifies content or directory, it expects the file to exist
            expected_exists = bool(
                expected.get("content") is not None
                or expected.get("content_b64") is not None
                or expected.get("is_text") is not None
                or expected.get("is_dir") is not None
            )
        actual_exists = bool(actual.get("exists", False))
        if actual_exists != expected_exists:
            return False
        if not actual_exists:
            return True
        if actual.get("is_dir") or expected.get("is_dir"):
            return bool(actual.get("is_dir") == expected.get("is_dir"))
        if actual.get("is_text") and expected.get("is_text", True):
            return actual.get("content") == expected.get("content")
        if not actual.get("is_text") and not expected.get("is_text", False):
            return actual.get("content_b64") == expected.get("content_b64")
        return False

    def _restore_snapshot(self, target_path: Path, before_state: Optional[Dict[str, Any]]) -> None:
        """Restore target path to recorded before_state snapshot."""
        if not before_state or not before_state.get("exists", False):
            if target_path.exists():
                target_path.unlink()
            return

        target_path.parent.mkdir(parents=True, exist_ok=True)
        if before_state.get("is_text", True):
            target_path.write_text(before_state.get("content", ""), encoding="utf-8")
        else:
            raw = base64.b64decode(before_state.get("content_b64", "").encode("ascii"))
            target_path.write_bytes(raw)

    def recover(self, transaction_id: str) -> RecoveryResult:
        """Deterministically recover an interrupted transaction.

        Returns structured RecoveryResult with final status and rollback details.
        """
        tx = self.store.get(transaction_id)
        if not tx:
            return RecoveryResult(
                transaction_id=transaction_id,
                previous_status=TransactionStatus.FAILED,
                final_status=TransactionStatus.FAILED,
                recovered=False,
                rolled_back=False,
                error=f"Transaction '{transaction_id}' not found in store",
            )

        initial_status = tx.status

        # 1. Idempotency check: Already terminal
        if tx.is_terminal:
            return RecoveryResult(
                transaction_id=tx.id,
                previous_status=initial_status,
                final_status=initial_status,
                recovered=(initial_status in (TransactionStatus.COMMITTED, TransactionStatus.ROLLED_BACK)),
                rolled_back=(initial_status == TransactionStatus.ROLLED_BACK),
                error=tx.failure_reason if initial_status == TransactionStatus.FAILED else None,
            )

        # 2. Check durable commit marker
        # If durable commit marker was persisted, transaction was committed before shutdown.
        if tx.commit_marker is not None:
            logger.info(
                f"Transaction '{tx.id}' has durable commit marker: finalizing status to COMMITTED without rollback."
            )
            if tx.status != TransactionStatus.COMMITTED:
                tx.transition(TransactionStatus.COMMITTED)
                self.store.save(tx)
            return RecoveryResult(
                transaction_id=tx.id,
                previous_status=initial_status,
                final_status=TransactionStatus.COMMITTED,
                recovered=True,
                rolled_back=False,
            )

        # 3. Incomplete transaction without commit marker -> deterministic rollback
        logger.warning(
            f"Recovering incomplete transaction '{tx.id}' in state '{tx.status.value}' — executing reverse rollback..."
        )
        if tx.status != TransactionStatus.ROLLING_BACK:
            tx.transition(
                TransactionStatus.ROLLING_BACK,
                reason=f"Crash recovery from incomplete state '{initial_status.value}'",
            )
            self.store.save(tx)

        ops_recovered = 0

        # Rollback operations in strict reverse order (LIFO)
        for op in reversed(tx.operations):
            # Check supported operation types
            if op.type not in (
                OperationType.CREATE_FILE,
                OperationType.MODIFY_FILE,
                OperationType.DELETE_FILE,
                OperationType.RENAME_FILE,
            ):
                err = f"Unknown or unsupported operation type '{op.type}' for op '{op.id}'"
                logger.error(err)
                op.status = OperationStatus.FAILED
                op.error = err
                tx.transition(TransactionStatus.FAILED, reason=err)
                self.store.save(tx)
                return RecoveryResult(
                    transaction_id=tx.id,
                    previous_status=initial_status,
                    final_status=TransactionStatus.FAILED,
                    recovered=False,
                    rolled_back=False,
                    ambiguous=True,
                    error=err,
                    operations_recovered=ops_recovered,
                )

            # Skip already rolled back operations
            if op.status == OperationStatus.ROLLED_BACK:
                continue

            target_path = self.workspace / op.target
            actual = self._inspect_target_state(target_path)

            # STAGED operation: Mutation never prepared or started
            if op.status == OperationStatus.STAGED:
                if not self._states_equal(actual, op.before_state):
                    err = f"Ambiguous filesystem state for staged op '{op.id}': '{op.target}' modified externally"
                    logger.error(err)
                    op.status = OperationStatus.FAILED
                    op.error = err
                    tx.transition(TransactionStatus.FAILED, reason=err)
                    self.store.save(tx)
                    return RecoveryResult(
                        transaction_id=tx.id,
                        previous_status=initial_status,
                        final_status=TransactionStatus.FAILED,
                        recovered=False,
                        rolled_back=False,
                        ambiguous=True,
                        error=err,
                        operations_recovered=ops_recovered,
                    )
                op.status = OperationStatus.ROLLED_BACK
                self.store.save(tx)
                continue

            # PREPARED operation: Mutation was in-flight when process crashed
            if op.status == OperationStatus.PREPARED:
                if op.type == OperationType.CREATE_FILE:
                    if self._states_equal(actual, op.before_state):
                        # Mutation never touched disk; file still absent
                        op.status = OperationStatus.ROLLED_BACK
                        self.store.save(tx)
                    elif self._states_equal(actual, op.after_state):
                        # Mutation was fully written; delete to rollback
                        try:
                            if target_path.exists():
                                target_path.unlink()
                            op.status = OperationStatus.ROLLED_BACK
                            self.store.save(tx)
                            ops_recovered += 1
                        except Exception as exc:
                            err = f"Failed to rollback in-flight create_file '{op.id}': {exc}"
                            op.status = OperationStatus.FAILED
                            op.error = err
                            tx.transition(TransactionStatus.FAILED, reason=err)
                            self.store.save(tx)
                            return RecoveryResult(
                                transaction_id=tx.id,
                                previous_status=initial_status,
                                final_status=TransactionStatus.FAILED,
                                recovered=False,
                                rolled_back=False,
                                ambiguous=False,
                                error=err,
                                operations_recovered=ops_recovered,
                            )
                    else:
                        # Ambiguous: content matches neither before nor after
                        err = f"Ambiguous filesystem state for in-flight create_file '{op.id}' on '{op.target}'"
                        logger.error(err)
                        op.status = OperationStatus.FAILED
                        op.error = err
                        tx.transition(TransactionStatus.FAILED, reason=err)
                        self.store.save(tx)
                        return RecoveryResult(
                            transaction_id=tx.id,
                            previous_status=initial_status,
                            final_status=TransactionStatus.FAILED,
                            recovered=False,
                            rolled_back=False,
                            ambiguous=True,
                            error=err,
                            operations_recovered=ops_recovered,
                        )

                elif op.type == OperationType.MODIFY_FILE:
                    if self._states_equal(actual, op.before_state):
                        # Mutation never touched disk; file in before state
                        op.status = OperationStatus.ROLLED_BACK
                        self.store.save(tx)
                    elif self._states_equal(actual, op.after_state):
                        # Mutation was fully applied; restore before state
                        try:
                            self._restore_snapshot(target_path, op.before_state)
                            op.status = OperationStatus.ROLLED_BACK
                            self.store.save(tx)
                            ops_recovered += 1
                        except Exception as exc:
                            err = f"Failed to rollback in-flight modify_file '{op.id}': {exc}"
                            op.status = OperationStatus.FAILED
                            op.error = err
                            tx.transition(TransactionStatus.FAILED, reason=err)
                            self.store.save(tx)
                            return RecoveryResult(
                                transaction_id=tx.id,
                                previous_status=initial_status,
                                final_status=TransactionStatus.FAILED,
                                recovered=False,
                                rolled_back=False,
                                ambiguous=False,
                                error=err,
                                operations_recovered=ops_recovered,
                            )
                    else:
                        # Ambiguous: content matches neither before nor after
                        err = f"Ambiguous filesystem state for in-flight modify_file '{op.id}' on '{op.target}'"
                        logger.error(err)
                        op.status = OperationStatus.FAILED
                        op.error = err
                        tx.transition(TransactionStatus.FAILED, reason=err)
                        self.store.save(tx)
                        return RecoveryResult(
                            transaction_id=tx.id,
                            previous_status=initial_status,
                            final_status=TransactionStatus.FAILED,
                            recovered=False,
                            rolled_back=False,
                            ambiguous=True,
                            error=err,
                            operations_recovered=ops_recovered,
                        )

                elif op.type == OperationType.DELETE_FILE:
                    if self._states_equal(actual, op.before_state):
                        op.status = OperationStatus.ROLLED_BACK
                        self.store.save(tx)
                    elif self._states_equal(actual, {"exists": False}):
                        try:
                            self._restore_snapshot(target_path, op.before_state)
                            op.status = OperationStatus.ROLLED_BACK
                            self.store.save(tx)
                            ops_recovered += 1
                        except Exception as exc:
                            err = f"Failed to rollback in-flight delete_file '{op.id}': {exc}"
                            op.status = OperationStatus.FAILED
                            op.error = err
                            tx.transition(TransactionStatus.FAILED, reason=err)
                            self.store.save(tx)
                            return RecoveryResult(
                                transaction_id=tx.id,
                                previous_status=initial_status,
                                final_status=TransactionStatus.FAILED,
                                recovered=False,
                                rolled_back=False,
                                ambiguous=False,
                                error=err,
                                operations_recovered=ops_recovered,
                            )
                    else:
                        err = f"Ambiguous filesystem state for in-flight delete_file '{op.id}' on '{op.target}'"
                        logger.error(err)
                        op.status = OperationStatus.FAILED
                        op.error = err
                        tx.transition(TransactionStatus.FAILED, reason=err)
                        self.store.save(tx)
                        return RecoveryResult(
                            transaction_id=tx.id,
                            previous_status=initial_status,
                            final_status=TransactionStatus.FAILED,
                            recovered=False,
                            rolled_back=False,
                            ambiguous=True,
                            error=err,
                            operations_recovered=ops_recovered,
                        )

                elif op.type == OperationType.RENAME_FILE:
                    if not op.new_target:
                        err = f"Rename op '{op.id}' lacks new_target"
                        op.status = OperationStatus.FAILED
                        tx.transition(TransactionStatus.FAILED, reason=err)
                        self.store.save(tx)
                        return RecoveryResult(
                            transaction_id=tx.id,
                            previous_status=initial_status,
                            final_status=TransactionStatus.FAILED,
                            recovered=False,
                            rolled_back=False,
                            ambiguous=True,
                            error=err,
                            operations_recovered=ops_recovered,
                        )
                    dest_path = self.workspace / op.new_target
                    src_exists = target_path.exists()
                    dest_exists = dest_path.exists()
                    if src_exists and not dest_exists:
                        # Rename did not take place
                        op.status = OperationStatus.ROLLED_BACK
                        self.store.save(tx)
                    elif dest_exists and not src_exists:
                        # Rename took place; move back
                        try:
                            target_path.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(dest_path), str(target_path))
                            op.status = OperationStatus.ROLLED_BACK
                            self.store.save(tx)
                            ops_recovered += 1
                        except Exception as exc:
                            err = f"Failed to rollback in-flight rename_file '{op.id}': {exc}"
                            op.status = OperationStatus.FAILED
                            op.error = err
                            tx.transition(TransactionStatus.FAILED, reason=err)
                            self.store.save(tx)
                            return RecoveryResult(
                                transaction_id=tx.id,
                                previous_status=initial_status,
                                final_status=TransactionStatus.FAILED,
                                recovered=False,
                                rolled_back=False,
                                ambiguous=False,
                                error=err,
                                operations_recovered=ops_recovered,
                            )
                    else:
                        err = f"Ambiguous filesystem state for rename '{op.id}': src={src_exists}, dest={dest_exists}"
                        logger.error(err)
                        op.status = OperationStatus.FAILED
                        op.error = err
                        tx.transition(TransactionStatus.FAILED, reason=err)
                        self.store.save(tx)
                        return RecoveryResult(
                            transaction_id=tx.id,
                            previous_status=initial_status,
                            final_status=TransactionStatus.FAILED,
                            recovered=False,
                            rolled_back=False,
                            ambiguous=True,
                            error=err,
                            operations_recovered=ops_recovered,
                        )
                continue

            # EXECUTED operation: Mutation definitely completed
            if op.status == OperationStatus.EXECUTED:
                try:
                    if op.type == OperationType.CREATE_FILE:
                        if target_path.exists():
                            target_path.unlink()
                            ops_recovered += 1
                    elif op.type in (OperationType.MODIFY_FILE, OperationType.DELETE_FILE):
                        self._restore_snapshot(target_path, op.before_state)
                        ops_recovered += 1
                    elif op.type == OperationType.RENAME_FILE:
                        if op.new_target:
                            dest_path = self.workspace / op.new_target
                            if dest_path.exists():
                                target_path.parent.mkdir(parents=True, exist_ok=True)
                                shutil.move(str(dest_path), str(target_path))
                                ops_recovered += 1

                    op.status = OperationStatus.ROLLED_BACK
                    op.result = "rolled_back"
                    self.store.save(tx)
                except Exception as exc:
                    err = f"Failed to rollback executed op '{op.id}': {exc}"
                    logger.error(err)
                    op.status = OperationStatus.FAILED
                    op.error = err
                    tx.transition(TransactionStatus.FAILED, reason=err)
                    self.store.save(tx)
                    return RecoveryResult(
                        transaction_id=tx.id,
                        previous_status=initial_status,
                        final_status=TransactionStatus.FAILED,
                        recovered=False,
                        rolled_back=False,
                        ambiguous=False,
                        error=err,
                        operations_recovered=ops_recovered,
                    )

        # 4. Finalize rollback state
        tx.transition(TransactionStatus.ROLLED_BACK)
        self.store.save(tx)
        return RecoveryResult(
            transaction_id=tx.id,
            previous_status=initial_status,
            final_status=TransactionStatus.ROLLED_BACK,
            recovered=True,
            rolled_back=True,
            operations_recovered=ops_recovered,
        )

    def recover_incomplete(self, limit: Optional[int] = MAX_STARTUP_RECOVERY_LIMIT) -> List[RecoveryResult]:
        """Discover and recover all incomplete transactions."""
        incomplete = self.discover_incomplete(limit=limit)
        return [self.recover(tx.id) for tx in incomplete]


def recover_incomplete_transaction(
    tx_or_id: Union[Transaction, str],
    workspace_or_store: Union[TransactionStore, Path, str],
    store_or_workspace: Optional[Union[TransactionStore, Path, str]] = None,
) -> TransactionResult:
    """Recover an interrupted or incomplete transaction after process restart.

    Delegates to TransactionRecoveryManager for deterministic write-ahead recovery.
    Rolls back any partially executed mutations in reverse order,
    marking the transaction as ROLLED_BACK (or FAILED if rollback fails or state is ambiguous).
    Never marks an incomplete transaction as COMMITTED without a durable commit marker.
    """
    if isinstance(tx_or_id, Transaction):
        tx_id = tx_or_id.id
    else:
        tx_id = str(tx_or_id)

    if hasattr(workspace_or_store, "get"):
        store: Optional[TransactionStore] = workspace_or_store  # type: ignore
        ws: Optional[Union[Path, str]] = store_or_workspace  # type: ignore
    else:
        ws = workspace_or_store  # type: ignore
        store = store_or_workspace  # type: ignore

    if store is None:
        raise ValueError("A TransactionStore must be provided for recovery")

    ws_path = Path(ws or ".").resolve()
    existing = store.get(tx_id)
    if existing and existing.workspace:
        ws_path = Path(existing.workspace).resolve()

    if isinstance(tx_or_id, Transaction) and (existing is None):
        store.save(tx_or_id)

    mgr = TransactionRecoveryManager(workspace=ws_path, store=store)
    rec_result = mgr.recover(tx_id)

    final_tx = store.get(tx_id) or (tx_or_id if isinstance(tx_or_id, Transaction) else None)
    if final_tx is None:
        final_tx = Transaction(id=tx_id, status=rec_result.final_status)

    return TransactionResult(
        transaction_id=final_tx.id,
        status=final_tx.status,
        committed=(final_tx.status == TransactionStatus.COMMITTED),
        rolled_back=(final_tx.status == TransactionStatus.ROLLED_BACK),
        operations=tuple(final_tx.operations),
        verification=final_tx.verification_result,
        error=final_tx.failure_reason,
        rollback_error=final_tx.rollback_reason,
        recovery=rec_result,
    )


__all__ = [
    "ALLOWED_TRANSACTION_TRANSITIONS",
    "BasicFilesystemVerifier",
    "FileTransactionStore",
    "InMemoryTransactionStore",
    "InvalidTransactionTransition",
    "MAX_FILE_SNAPSHOT_BYTES",
    "MAX_SERIALIZED_TRANSACTION_BYTES",
    "MAX_STARTUP_RECOVERY_LIMIT",
    "MAX_TRANSACTION_OPERATIONS",
    "OperationStatus",
    "OperationType",
    "RecoveryResult",
    "TERMINAL_TRANSACTION_STATUSES",
    "Transaction",
    "TransactionCoordinator",
    "TransactionOperation",
    "TransactionRecoveryManager",
    "TransactionResourceLimitError",
    "TransactionResult",
    "TransactionStatus",
    "TransactionStore",
    "TransactionVerifier",
    "recover_incomplete_transaction",
]
