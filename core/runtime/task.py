"""Task Domain Model.

BrainFrog P1.4C — Task Domain.

Architectural Principles:
- TASK ≠ AUTHORITY
- TASK ≠ CAPABILITY
- TASK ≠ APPROVAL
- TASK ≠ EXECUTION CONTRACT
- TASK ≠ DELEGATION CONTRACT
- TASK ≠ AGENT PROFILE
- CLASSIFICATION ≠ AUTHORIZATION
- ROUTING ≠ AUTHORIZATION

A Task is the canonical domain representation of "work that needs to be done".
It is purely descriptive:
- It describes WHAT work is requested (objective, description, task_type, domain, skills, inputs).
- It NEVER grants, implies, encodes, or derives permission to perform that work.
- It contains ZERO execution authority (no capabilities, filesystem, shell, network,
  git, approval, transaction, or orchestrator authority).
- All execution authority remains strictly governed by the canonical P1.3 chain:
    ApprovedExecutionContract -> Transaction -> orchestrator.py
- Pure domain model: zero execution dependencies, zero filesystem mutation, zero subprocesses.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
import types
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple, Union

CURRENT_TASK_SCHEMA_VERSION = 1

# Explicit Resource Bounds
MAX_TASK_ID_CHARS = 128
MAX_OBJECTIVE_CHARS = 4096
MAX_DESCRIPTION_CHARS = 8192
MAX_TASK_TYPE_CHARS = 128
MAX_DOMAIN_CHARS = 128
MAX_REQUESTED_SKILLS_COUNT = 64
MAX_SKILL_CHARS = 128
MAX_CONTEXT_KEYS = 64
MAX_METADATA_KEYS = 64
MAX_KEY_CHARS = 128
MAX_NESTING_DEPTH = 5
MAX_STRING_VALUE_CHARS = 4096
MAX_TASK_SERIALIZED_BYTES = 64 * 1024  # 64 KB

# Identifier & Traversal Patterns
_INVALID_ID_CHARS = re.compile(r"[\x00-\x1f\x7f/\\*\?\"'<>\|;:~]|(?:\.\.)")
_DRIVE_LETTER_PATTERN = re.compile(r"^[a-zA-Z]:")

# Credential and Secret Detection Patterns
_SECRET_KEY_NAMES: FrozenSet[str] = frozenset({
    "password",
    "passwd",
    "secret",
    "credential",
    "credentials",
    "api_key",
    "apikey",
    "token",
    "access_token",
    "auth_token",
    "refresh_token",
    "private_key",
    "secret_key",
    "client_secret",
    "app_secret",
    "signing_secret",
    "jwt_secret",
    "authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
})
_FORBIDDEN_SECRET_NAMES = frozenset({
    "authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
})
_DESCRIPTIVE_KEY_SUFFIXES: Tuple[str, ...] = (
    "_count",
    "_budget",
    "_type",
    "_policy",
    "_limit",
    "_limits",
    "_format",
    "_length",
    "_algorithm",
    "_method",
    "_provider",
    "_strategy",
    "_status",
    "_name",
    "_id",
    "_version",
    "_url",
    "_path",
    "_spec",
    "_schema",
    "_rule",
    "_rules",
    "_description",
    "_mode",
    "_ttl",
    "_expiry",
    "_requirement",
    "_requirements",
)
_DESCRIPTIVE_KEY_PREFIXES: Tuple[str, ...] = (
    "count_",
    "max_",
    "min_",
    "total_",
    "number_of_",
    "has_",
    "is_",
    "requires_",
)


def _is_secret_bearing_key(key: str) -> bool:
    """Determine if a key is designated to carry credentials or secret values.

    Distinguishes actual secret-bearing keys (e.g. 'password', 'api_key', 'auth_token')
    from descriptive metadata keys (e.g. 'token_count', 'password_policy', 'credential_type').
    """
    clean = key.strip().lower()
    if clean in _FORBIDDEN_SECRET_NAMES:
        return True

    # Descriptive compound keys are not secret-bearing by name
    if clean.startswith(_DESCRIPTIVE_KEY_PREFIXES) or clean.endswith(_DESCRIPTIVE_KEY_SUFFIXES):
        return False

    # Check exact secret-bearing key names
    if clean in _SECRET_KEY_NAMES:
        return True

    # Check secret-bearing compound keys (e.g. 'user_password', 'openai_api_key', 'session_token')
    for term in (
        "password",
        "passwd",
        "secret",
        "credential",
        "api_key",
        "apikey",
        "auth_token",
        "access_token",
        "refresh_token",
        "token",
    ):
        if clean.endswith(term) or clean.endswith("_" + term) or clean.startswith(term + "_"):
            return True

    return False

_SECRET_VALUE_ASSIGN_PATTERN = re.compile(
    r"(?i)(?:password|api[_-]?key|access[_-]?token|secret|bearer)\s*[:=]\s*[^\s]+",
)
_PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY",
    re.IGNORECASE,
)
_TOKEN_PATTERNS = [
    re.compile(r"\b(?:sk-[a-zA-Z0-9_\-]{20,})\b"),  # OpenAI-style key
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,})\b"),  # GitHub token
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),  # Telegram bot token
    re.compile(r"(?i)\bbearer\s+[a-zA-Z0-9_\-\.]{12,}\b"),  # Bearer token
]

# Forbidden Authority-Shaped Keys in Task fields, context, and metadata
FORBIDDEN_AUTHORITY_KEYS: FrozenSet[str] = frozenset({
    "capability",
    "capabilities",
    "permission",
    "permissions",
    "authority",
    "authorities",
    "allow_shell",
    "allow_network",
    "allow_filesystem",
    "allow_filesystem_read",
    "allow_filesystem_write",
    "allow_git",
    "allow_git_read",
    "allow_git_commit",
    "allow_git_push",
    "allowed_tools",
    "allowed_commands",
    "allowed_files",
    "can_approve",
    "can_execute",
    "can_mutate",
    "approval_status",
    "approved",
    "authorized",
    "approved_execution_contract",
    "execution_contract",
    "delegation_contract",
    "actor_credentials",
    "session_credentials",
    "auth_tokens",
    "secrets",
    "executor",
    "orchestrator",
    "orchestrator_authority",
    "transaction_id",
    "transaction_authority",
    "session_authority",
    "shell_access",
    "network_access",
})


# =============================================================================
# Exception Hierarchy
# =============================================================================


class TaskError(ValueError):
    """Base exception for all Task domain errors."""
    pass


class TaskValidationError(TaskError):
    """Raised when task validation, constraints, or bounds fail."""
    pass


class TaskAuthorityViolationError(TaskError, PermissionError):
    """Raised when authority-like data or capabilities are passed to a Task."""
    pass


class TaskIntegrityError(TaskError):
    """Raised when task digest verification or tampering detection fails."""
    pass


class TaskSecretExposureError(TaskError, PermissionError):
    """Raised when sensitive credentials or secrets are detected in a Task."""
    pass


# =============================================================================
# Validation and Security Sanitization Helpers
# =============================================================================


def _reject_string_secrets(val: str, context: str) -> None:
    """Scan string value for high-risk token signatures or secret assignments."""
    if _PRIVATE_KEY_PATTERN.search(val):
        raise TaskSecretExposureError(
            f"Private key header pattern detected in Task {context}"
        )
    for pat in _TOKEN_PATTERNS:
        if pat.search(val):
            raise TaskSecretExposureError(
                f"High-entropy token pattern detected in Task {context}"
            )
    if _SECRET_VALUE_ASSIGN_PATTERN.search(val):
        raise TaskSecretExposureError(
            f"Secret assignment pattern detected in Task {context}"
        )


def _reject_secrets_recursive(value: Any, context: str = "field") -> None:
    """Recursively search for credentials or secrets in keys and values."""
    if isinstance(value, Mapping):
        for k, v in value.items():
            if isinstance(k, str):
                if _is_secret_bearing_key(k):
                    if v and not (isinstance(v, str) and (v.startswith("REDACTED") or v.startswith("[REDACTED"))):
                        raise TaskSecretExposureError(
                            f"Prohibited credential key '{k}' found in Task {context}"
                        )
                _reject_string_secrets(k, f"{context}.key({k})")
            _reject_secrets_recursive(v, context=f"{context}['{k}']")
    elif isinstance(value, (list, tuple, set, frozenset)):
        for i, item in enumerate(value):
            _reject_secrets_recursive(item, context=f"{context}[{i}]")
    elif isinstance(value, str):
        _reject_string_secrets(value, context)


def validate_task_id(task_id: Any) -> str:
    """Validate task ID string to prevent traversal, separators, drive letters, and injection.

    Task IDs are strictly identifiers, never file paths, module names, commands, or import paths.
    """
    if type(task_id) is not str:
        raise TaskValidationError(f"task_id must be a string, got {type(task_id)}")
    clean = task_id.strip()
    if not clean:
        raise TaskValidationError("task_id cannot be empty")
    if len(clean) > MAX_TASK_ID_CHARS:
        raise TaskValidationError(
            f"task_id length ({len(clean)}) exceeds maximum allowed ({MAX_TASK_ID_CHARS})"
        )
    if clean.startswith((".", "~", "/", "\\")):
        raise TaskValidationError(
            f"task_id cannot start with traversal or separator characters: '{clean}'"
        )
    if _DRIVE_LETTER_PATTERN.match(clean):
        raise TaskValidationError(
            f"task_id cannot start with drive letter pattern: '{clean}'"
        )
    if _INVALID_ID_CHARS.search(clean):
        raise TaskValidationError(
            f"Invalid characters or traversal detected in task_id: '{clean}'"
        )
    _reject_string_secrets(clean, "task_id")
    return clean


def _check_authority_key(key: str, context: str) -> None:
    """Reject keys that resemble execution capabilities, permissions, or authority."""
    clean_k = key.strip().lower()
    if clean_k in FORBIDDEN_AUTHORITY_KEYS:
        raise TaskAuthorityViolationError(
            f"Authority-like field '{key}' is forbidden in Task {context}"
        )
    # Check prefixes / suffixes indicating execution authority
    for forbidden in ("allow_", "can_", "grant_"):
        if clean_k.startswith(forbidden):
            for suffix in ("shell", "network", "exec", "write", "read", "push", "commit", "approve", "delete", "tool", "command"):
                if suffix in clean_k:
                    raise TaskAuthorityViolationError(
                        f"Authority-like permission '{key}' is forbidden in Task {context}"
                    )
    for forbidden_token in ("capability", "capabilities", "permission", "permissions"):
        if forbidden_token in clean_k:
            raise TaskAuthorityViolationError(
                f"Authority-like field '{key}' is forbidden in Task {context}"
            )


def _validate_and_normalize_string_tokens(
    raw: Any,
    *,
    collection_name: str,
    max_count: int,
    max_item_len: int,
) -> Tuple[str, ...]:
    """Validate, normalize, and sort a collection of non-empty bounded string tokens."""
    if type(raw) not in (list, tuple, set, frozenset):
        raise TaskValidationError(
            f"{collection_name} must be a sequence or set of strings, got {type(raw)}"
        )
    if len(raw) > max_count:
        raise TaskValidationError(
            f"{collection_name} count ({len(raw)}) exceeds maximum allowed ({max_count})"
        )
    clean_items: Set[str] = set()
    for item in raw:
        if type(item) is not str:
            raise TaskValidationError(
                f"Items in {collection_name} must be strings, got {type(item)}"
            )
        cleaned = item.strip().lower()
        if not cleaned:
            raise TaskValidationError(f"Items in {collection_name} cannot be empty")
        if len(cleaned) > max_item_len:
            raise TaskValidationError(
                f"Item in {collection_name} length ({len(cleaned)}) exceeds limit ({max_item_len})"
            )
        if re.search(r"[\x00-\x1f\x7f]", cleaned):
            raise TaskValidationError(
                f"Control characters forbidden in {collection_name} item: '{cleaned}'"
            )
        _reject_string_secrets(cleaned, f"{collection_name}[item]")
        clean_items.add(cleaned)
    return tuple(sorted(clean_items))


def _validate_and_freeze_mapping(
    raw: Any,
    *,
    mapping_name: str,
    max_keys: int,
    depth: int = 1,
) -> types.MappingProxyType[str, Any]:
    """Recursively validate JSON-serializable mapping, enforce bounds, and freeze into MappingProxyType."""
    if not isinstance(raw, Mapping):
        raise TaskValidationError(
            f"{mapping_name} must be a mapping/dict, got {type(raw)}"
        )
    if depth > MAX_NESTING_DEPTH:
        raise TaskValidationError(
            f"{mapping_name} nesting depth exceeds maximum limit of {MAX_NESTING_DEPTH}"
        )
    if len(raw) > max_keys:
        raise TaskValidationError(
            f"{mapping_name} keys count ({len(raw)}) exceeds maximum allowed ({max_keys})"
        )

    frozen_dict: Dict[str, Any] = {}
    for k, v in raw.items():
        if type(k) is not str:
            raise TaskValidationError(
                f"Keys in {mapping_name} must be strings, got {type(k)}"
            )
        clean_k = k.strip()
        if not clean_k:
            raise TaskValidationError(f"Keys in {mapping_name} cannot be empty")
        if len(clean_k) > MAX_KEY_CHARS:
            raise TaskValidationError(
                f"Key '{clean_k}' in {mapping_name} exceeds maximum length ({MAX_KEY_CHARS})"
            )
        if re.search(r"[\x00-\x1f\x7f]", clean_k):
            raise TaskValidationError(
                f"Control characters forbidden in key '{clean_k}' of {mapping_name}"
            )

        _check_authority_key(clean_k, mapping_name)

        frozen_dict[clean_k] = _validate_and_freeze_value(
            v,
            context=f"{mapping_name}['{clean_k}']",
            depth=depth + 1,
        )

    return types.MappingProxyType(frozen_dict)


def _validate_and_freeze_value(
    val: Any,
    *,
    context: str,
    depth: int,
) -> Any:
    """Validate that value is JSON-compatible and deeply immutable."""
    if val is None or isinstance(val, (bool, int, float)):
        if isinstance(val, float) and not math.isfinite(val):
            raise TaskValidationError(f"Non-finite float value not allowed in {context}")
        return val
    elif isinstance(val, str):
        if len(val) > MAX_STRING_VALUE_CHARS:
            raise TaskValidationError(
                f"String value in {context} exceeds maximum length of {MAX_STRING_VALUE_CHARS}"
            )
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", val):
            raise TaskValidationError(f"Control characters forbidden in string value of {context}")
        _reject_string_secrets(val, context)
        return val
    elif isinstance(val, Mapping):
        return _validate_and_freeze_mapping(
            val,
            mapping_name=context,
            max_keys=MAX_METADATA_KEYS,
            depth=depth,
        )
    elif isinstance(val, (list, tuple, set, frozenset)):
        if depth > MAX_NESTING_DEPTH:
            raise TaskValidationError(
                f"Nesting depth in {context} exceeds maximum limit of {MAX_NESTING_DEPTH}"
            )
        if len(val) > MAX_METADATA_KEYS:
            raise TaskValidationError(
                f"Collection length in {context} exceeds maximum limit of {MAX_METADATA_KEYS}"
            )
        return tuple(
            _validate_and_freeze_value(
                item,
                context=f"{context}[{i}]",
                depth=depth + 1,
            )
            for i, item in enumerate(val)
        )
    else:
        # Non-JSON types (functions, classes, modules, sockets, file handles, etc.) are strictly rejected
        raise TaskValidationError(
            f"Non-JSON-compatible type '{type(val).__name__}' is forbidden in {context}"
        )


def _unfreeze_for_json(val: Any) -> Any:
    """Convert deeply frozen MappingProxyType and tuples back to dicts/lists for canonical JSON dumping."""
    if isinstance(val, (types.MappingProxyType, dict)):
        return {k: _unfreeze_for_json(v) for k, v in val.items()}
    elif isinstance(val, (tuple, list, set, frozenset)):
        return [_unfreeze_for_json(x) for x in val]
    return val


# =============================================================================
# Task Domain Model
# =============================================================================


@dataclass(frozen=True, slots=True)
class Task:
    """Canonical domain representation of work requested to be done.

    Crucial Architectural Invariant:
    TASK ≠ AUTHORITY.
    A Task describes what work is requested (objective, description, taxonomy, domain,
    skills, input parameters, lineage, and descriptive metadata).
    It NEVER grants, implies, or derives runtime execution authority, filesystem access,
    shell execution, network access, approval status, transaction boundaries, or capability contracts.
    """

    task_id: str
    objective: str
    description: str = ""
    task_type: Optional[Union[str, Enum]] = None
    domain: Optional[str] = None
    requested_skills: Tuple[str, ...] = ()
    input_context: Mapping[str, Any] = field(default_factory=dict)
    parent_task_id: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = CURRENT_TASK_SCHEMA_VERSION
    created_at: float = field(default_factory=time.time)
    digest: str = ""

    def __post_init__(self) -> None:
        # 1. task_id
        clean_id = validate_task_id(self.task_id)
        object.__setattr__(self, "task_id", clean_id)

        # 2. schema_version
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise TaskValidationError("schema_version must be a positive integer")

        # 3. objective and description
        if type(self.objective) is not str:
            raise TaskValidationError(
                f"objective must be a string, got {type(self.objective)}"
            )
        if type(self.description) is not str:
            raise TaskValidationError(
                f"description must be a string, got {type(self.description)}"
            )

        clean_obj = self.objective.strip()
        clean_desc = self.description.strip()

        # If objective is empty but description was supplied, promote description
        if not clean_obj and clean_desc:
            clean_obj = clean_desc

        if not clean_obj:
            raise TaskValidationError("objective cannot be empty")
        if len(clean_obj) > MAX_OBJECTIVE_CHARS:
            raise TaskValidationError(
                f"objective length ({len(clean_obj)}) exceeds limit ({MAX_OBJECTIVE_CHARS})"
            )
        if len(clean_desc) > MAX_DESCRIPTION_CHARS:
            raise TaskValidationError(
                f"description length ({len(clean_desc)}) exceeds limit ({MAX_DESCRIPTION_CHARS})"
            )

        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", clean_obj):
            raise TaskValidationError("Control characters forbidden in objective")
        if clean_desc and re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", clean_desc):
            raise TaskValidationError("Control characters forbidden in description")

        _reject_string_secrets(clean_obj, "objective")
        if clean_desc:
            _reject_string_secrets(clean_desc, "description")

        object.__setattr__(self, "objective", clean_obj)
        object.__setattr__(self, "description", clean_desc)

        # 4. task_type
        if self.task_type is not None:
            if isinstance(self.task_type, Enum):
                tt_val = str(self.task_type.value)
            elif isinstance(self.task_type, str):
                tt_val = self.task_type
            else:
                raise TaskValidationError(
                    f"task_type must be a string or enum, got {type(self.task_type)}"
                )
            clean_tt = tt_val.strip().lower()
            if not clean_tt:
                raise TaskValidationError("task_type cannot be empty when provided")
            if len(clean_tt) > MAX_TASK_TYPE_CHARS:
                raise TaskValidationError(
                    f"task_type length ({len(clean_tt)}) exceeds limit ({MAX_TASK_TYPE_CHARS})"
                )
            if re.search(r"[\x00-\x1f\x7f]", clean_tt):
                raise TaskValidationError("Control characters forbidden in task_type")
            _reject_string_secrets(clean_tt, "task_type")
            object.__setattr__(self, "task_type", clean_tt)

        # 5. domain
        if self.domain is not None:
            if type(self.domain) is not str:
                raise TaskValidationError(
                    f"domain must be a string, got {type(self.domain)}"
                )
            clean_dom = self.domain.strip().lower()
            if not clean_dom:
                raise TaskValidationError("domain cannot be empty when provided")
            if len(clean_dom) > MAX_DOMAIN_CHARS:
                raise TaskValidationError(
                    f"domain length ({len(clean_dom)}) exceeds limit ({MAX_DOMAIN_CHARS})"
                )
            if re.search(r"[\x00-\x1f\x7f]", clean_dom):
                raise TaskValidationError("Control characters forbidden in domain")
            _reject_string_secrets(clean_dom, "domain")
            object.__setattr__(self, "domain", clean_dom)

        # 6. requested_skills
        clean_skills = _validate_and_normalize_string_tokens(
            self.requested_skills,
            collection_name="requested_skills",
            max_count=MAX_REQUESTED_SKILLS_COUNT,
            max_item_len=MAX_SKILL_CHARS,
        )
        object.__setattr__(self, "requested_skills", clean_skills)

        # 7. input_context (deeply immutable MappingProxyType, rejects authority and secrets)
        clean_context = _validate_and_freeze_mapping(
            self.input_context,
            mapping_name="input_context",
            max_keys=MAX_CONTEXT_KEYS,
        )
        object.__setattr__(self, "input_context", clean_context)

        # 8. parent_task_id
        if self.parent_task_id is not None:
            clean_parent = validate_task_id(self.parent_task_id)
            if clean_parent == clean_id:
                raise TaskValidationError("parent_task_id cannot be identical to task_id")
            object.__setattr__(self, "parent_task_id", clean_parent)

        # 9. metadata (deeply immutable MappingProxyType, rejects authority and secrets)
        clean_metadata = _validate_and_freeze_mapping(
            self.metadata,
            mapping_name="metadata",
            max_keys=MAX_METADATA_KEYS,
        )
        object.__setattr__(self, "metadata", clean_metadata)

        # 10. created_at (finite timestamp)
        if not isinstance(self.created_at, (int, float)) or not math.isfinite(self.created_at):
            raise TaskValidationError("created_at must be a valid finite number")
        object.__setattr__(self, "created_at", float(self.created_at))

        # 11. Secret rejection across context and metadata
        _reject_secrets_recursive(self.input_context, "input_context")
        _reject_secrets_recursive(self.metadata, "metadata")

        # 12. Total serialized size check
        serialized_size = len(self._compute_canonical_json().encode("utf-8"))
        if serialized_size > MAX_TASK_SERIALIZED_BYTES:
            raise TaskValidationError(
                f"Serialized task size ({serialized_size} bytes) exceeds limit ({MAX_TASK_SERIALIZED_BYTES} bytes)"
            )

        # 13. Digest computation & verification
        expected_digest = self.compute_digest()
        if not self.digest:
            object.__setattr__(self, "digest", expected_digest)
        elif self.digest != expected_digest:
            raise TaskIntegrityError(
                f"Task digest mismatch: expected '{expected_digest}', got '{self.digest}'"
            )

    @property
    def id(self) -> str:
        """Alias for task_id for ergonomic consistency."""
        return self.task_id

    def compute_digest(self) -> str:
        """Compute deterministic SHA-256 digest of semantic task fields.

        Excludes volatile execution fields (created_at, digest) to ensure reproducible
        digest calculation across sessions and environments.
        """
        canonical_json = self._compute_canonical_json()
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    def _compute_canonical_json(self) -> str:
        """Produce canonical JSON representation of semantic fields with sorted keys."""
        semantic_payload = {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "objective": self.objective,
            "description": self.description,
            "task_type": self.task_type,
            "domain": self.domain,
            "requested_skills": list(self.requested_skills),
            "input_context": _unfreeze_for_json(self.input_context),
            "parent_task_id": self.parent_task_id,
            "metadata": _unfreeze_for_json(self.metadata),
        }
        return json.dumps(semantic_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def to_dict(self) -> Dict[str, Any]:
        """Convert Task to a JSON-compatible dictionary."""
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "objective": self.objective,
            "description": self.description,
            "task_type": self.task_type,
            "domain": self.domain,
            "requested_skills": list(self.requested_skills),
            "input_context": _unfreeze_for_json(self.input_context),
            "parent_task_id": self.parent_task_id,
            "metadata": _unfreeze_for_json(self.metadata),
            "created_at": self.created_at,
            "digest": self.digest,
        }

    def to_json(self) -> str:
        """Serialize Task to a JSON string."""
        return json.dumps(self.to_dict(), sort_keys=True, indent=2, ensure_ascii=True)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Task:
        """Reconstruct a Task from a dictionary, enforcing strict validation."""
        if type(data) is not dict:
            raise TaskValidationError(f"Expected dictionary for Task.from_dict, got {type(data)}")

        # Check for smuggled authority fields in root
        for k in data.keys():
            _check_authority_key(k, "root")

        # Verify against unexpected extra fields
        known_fields = {
            "schema_version",
            "task_id",
            "objective",
            "description",
            "task_type",
            "domain",
            "requested_skills",
            "input_context",
            "parent_task_id",
            "metadata",
            "created_at",
            "digest",
        }
        unknown = set(data.keys()) - known_fields
        if unknown:
            raise TaskValidationError(f"Unrecognized fields in Task payload: {sorted(unknown)}")

        skills_raw = data.get("requested_skills", ())
        if not isinstance(skills_raw, (list, tuple, set, frozenset)):
            raise TaskValidationError("requested_skills must be a list or tuple of strings")

        context_raw = data.get("input_context", {})
        if not isinstance(context_raw, Mapping):
            raise TaskValidationError("input_context must be a mapping/dict")

        metadata_raw = data.get("metadata", {})
        if not isinstance(metadata_raw, Mapping):
            raise TaskValidationError("metadata must be a mapping/dict")

        return cls(
            task_id=data.get("task_id", ""),
            objective=data.get("objective", ""),
            description=data.get("description", ""),
            task_type=data.get("task_type"),
            domain=data.get("domain"),
            requested_skills=tuple(skills_raw),
            input_context=context_raw,
            parent_task_id=data.get("parent_task_id"),
            metadata=metadata_raw,
            schema_version=data.get("schema_version", CURRENT_TASK_SCHEMA_VERSION),
            created_at=data.get("created_at", time.time()),
            digest=data.get("digest", ""),
        )

    @classmethod
    def from_json(cls, json_str: str) -> Task:
        """Parse JSON string and construct validated Task."""
        if type(json_str) is not str:
            raise TaskValidationError(f"Expected string for Task.from_json, got {type(json_str)}")
        if len(json_str.encode("utf-8")) > MAX_TASK_SERIALIZED_BYTES:
            raise TaskValidationError("JSON string exceeds maximum allowed task payload size")
        try:
            parsed = json.loads(json_str)
        except json.JSONDecodeError as exc:
            raise TaskValidationError(f"Malformed JSON payload: {exc}") from exc
        return cls.from_dict(parsed)
