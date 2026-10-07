"""Agent Profile Domain Model and Deterministic Registry.

BrainFrog P1.4A — Agent Profile Domain & Registry.

Architectural Principles:
- AGENT PROFILE ≠ AUTHORIZATION
- REGISTRY ≠ AUTHORIZATION
- An AgentProfile describes specialization (skills, domains, task types, preferences, constraints).
- An AgentProfile NEVER grants execution authority (no filesystem, shell, network, Git,
  approval, transaction, or orchestrator authority).
- All execution authority remains strictly governed by the canonical P1.3 chain:
    Parent Capability -> DelegationContract -> ApprovedExecutionContract -> Transaction -> orchestrator.py
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

from core.runtime.secret_scrubbing import scrub_secrets

CURRENT_AGENT_PROFILE_SCHEMA_VERSION = 1
CURRENT_PROFILE_RESOLUTION_SCHEMA_VERSION = 1

# Explicit Resource Bounds
MAX_PROFILE_ID_CHARS = 128
MAX_DISPLAY_NAME_CHARS = 256
MAX_DESCRIPTION_CHARS = 4096
MAX_TASK_TYPES_COUNT = 64
MAX_TASK_TYPE_CHARS = 128
MAX_DOMAINS_COUNT = 32
MAX_DOMAIN_CHARS = 128
MAX_SKILLS_COUNT = 128
MAX_SKILL_CHARS = 128
MAX_CONSTRAINTS_COUNT = 64
MAX_CONSTRAINT_KEY_CHARS = 128
MAX_METADATA_KEYS = 64
MAX_METADATA_KEY_CHARS = 128
MAX_METADATA_DEPTH = 5
MAX_STRING_VALUE_CHARS = 4096
MAX_PROFILE_SERIALIZED_BYTES = 64 * 1024  # 64 KB
MAX_REGISTRY_CAPACITY = 500

# Resolution Resource Bounds
MAX_RESOLUTION_TASK_TYPE_CHARS = 128
MAX_RESOLUTION_DOMAIN_CHARS = 128
MAX_RESOLUTION_SKILLS_COUNT = 64
MAX_RESOLUTION_SKILL_CHARS = 128
MAX_RESOLUTION_CANDIDATE_IDS = 64
MAX_RESOLUTION_REASON_CHARS = 1024
MAX_RESOLUTION_REQUEST_SERIALIZED_BYTES = 16 * 1024  # 16 KB
MAX_RESOLUTION_RESULT_SERIALIZED_BYTES = 32 * 1024   # 32 KB

# Deterministic Matching Weights
TASK_TYPE_MATCH_WEIGHT = 10.0
DOMAIN_MATCH_WEIGHT = 5.0
SKILL_MATCH_WEIGHT = 1.0

# Identifier & Traversal Patterns
_INVALID_ID_CHARS = re.compile(r"[\x00-\x1f\x7f/\\*\?\"<>\|]|(?:\.\.)")
_DRIVE_LETTER_PATTERN = re.compile(r"^[a-zA-Z]:")

# Credential and Secret Detection Patterns
_SECRET_KEY_PATTERN = re.compile(
    r"password|secret|credential|api[_-]?key|token|auth[_-]?token|access[_-]?token",
    re.IGNORECASE,
)
_FORBIDDEN_SECRET_NAMES = frozenset({
    "authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
})
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

# Forbidden Authority-Shaped Keys in constraints & metadata
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
    "can_approve",
    "can_execute",
    "can_mutate",
    "approved_execution_contract",
    "execution_contract",
    "transaction_id",
    "transaction_authority",
    "orchestrator",
    "orchestrator_authority",
    "session_authority",
})


# =============================================================================
# Exception Hierarchy
# =============================================================================


class AgentProfileError(ValueError):
    """Base exception for all agent profile domain errors."""
    pass


class AgentProfileValidationError(AgentProfileError):
    """Raised when profile validation, constraints, or bounds fail."""
    pass


class AgentProfileAuthorityViolationError(AgentProfileError, PermissionError):
    """Raised when authority-like data is passed to an AgentProfile."""
    pass


class AgentProfileIntegrityError(AgentProfileError):
    """Raised when profile digest verification or tampering detection fails."""
    pass


class AgentProfileDuplicateError(AgentProfileError):
    """Raised when attempting to register a duplicate profile ID."""
    pass


class AgentProfileNotFoundError(AgentProfileError, KeyError):
    """Raised when a requested profile is not present in the registry."""
    pass


class AmbiguousProfileResolutionError(AgentProfileError):
    """Raised when profile resolution yields two or more candidates with equal top score."""
    pass


class NoMatchingProfileError(AgentProfileError, KeyError):
    """Raised when profile resolution finds no profile matching the requested criteria."""
    pass


# =============================================================================
# Validation and Freezing Helpers
# =============================================================================


def _reject_string_secrets(text: str, context: str) -> None:
    """Validate that a string does not contain credential patterns or private keys."""
    if _PRIVATE_KEY_PATTERN.search(text):
        raise AgentProfileValidationError(f"Private key header forbidden in {context}")
    if _SECRET_VALUE_ASSIGN_PATTERN.search(text):
        raise AgentProfileValidationError(f"Credential assignment forbidden in {context}")
    for pat in _TOKEN_PATTERNS:
        if pat.search(text):
            raise AgentProfileValidationError(f"Credential token forbidden in {context}")
    if scrub_secrets(text) != text:
        raise AgentProfileValidationError(f"Secret material forbidden in {context}")


def _reject_secrets_recursive(value: Any, context: str = "profile") -> None:
    """Recursively validate that no credential keys or values exist in collections/mappings."""
    if isinstance(value, Mapping):
        for k, v in value.items():
            if isinstance(k, str):
                clean_k = k.strip().lower()
                if clean_k in _FORBIDDEN_SECRET_NAMES or _SECRET_KEY_PATTERN.search(clean_k):
                    raise AgentProfileValidationError(
                        f"Credential key '{k}' is forbidden in {context}"
                    )
            _reject_secrets_recursive(v, context=f"{context}['{k}']")
    elif isinstance(value, (list, tuple, set, frozenset)):
        for i, item in enumerate(value):
            _reject_secrets_recursive(item, context=f"{context}[{i}]")
    elif isinstance(value, str):
        _reject_string_secrets(value, context)


def validate_profile_id(profile_id: Any) -> str:
    """Validate profile ID string to prevent traversal, separators, drive letters, and secrets."""
    if type(profile_id) is not str:
        raise AgentProfileValidationError(f"profile_id must be a string, got {type(profile_id)}")
    clean = profile_id.strip()
    if not clean:
        raise AgentProfileValidationError("profile_id cannot be empty")
    if len(clean) > MAX_PROFILE_ID_CHARS:
        raise AgentProfileValidationError(
            f"profile_id length ({len(clean)}) exceeds maximum allowed ({MAX_PROFILE_ID_CHARS})"
        )
    if clean.startswith((".", "~", "/", "\\")):
        raise AgentProfileValidationError(
            f"profile_id cannot start with traversal or separator characters: '{clean}'"
        )
    if _DRIVE_LETTER_PATTERN.match(clean):
        raise AgentProfileValidationError(
            f"profile_id cannot start with drive letter pattern: '{clean}'"
        )
    if _INVALID_ID_CHARS.search(clean):
        raise AgentProfileValidationError(
            f"Invalid characters or traversal detected in profile_id: '{clean}'"
        )
    _reject_string_secrets(clean, "profile_id")
    return clean


def _validate_and_normalize_string_collection(
    raw: Any,
    *,
    collection_name: str,
    max_count: int,
    max_item_len: int,
) -> Tuple[str, ...]:
    """Validate and sort a collection of non-empty bounded string tokens."""
    if type(raw) not in (list, tuple, set, frozenset):
        raise AgentProfileValidationError(
            f"{collection_name} must be a sequence or set of strings, got {type(raw)}"
        )
    if len(raw) > max_count:
        raise AgentProfileValidationError(
            f"{collection_name} count ({len(raw)}) exceeds maximum allowed ({max_count})"
        )
    clean_items: Set[str] = set()
    for item in raw:
        if type(item) is not str:
            raise AgentProfileValidationError(
                f"Items in {collection_name} must be strings, got {type(item)}"
            )
        cleaned = item.strip()
        if not cleaned:
            raise AgentProfileValidationError(f"Items in {collection_name} cannot be empty")
        if len(cleaned) > max_item_len:
            raise AgentProfileValidationError(
                f"Item in {collection_name} length ({len(cleaned)}) exceeds limit ({max_item_len})"
            )
        if re.search(r"[\x00-\x1f\x7f]", cleaned):
            raise AgentProfileValidationError(
                f"Control characters forbidden in {collection_name} item: '{cleaned}'"
            )
        clean_items.add(cleaned)
    return tuple(sorted(clean_items))


def _check_authority_key(key: str, context: str) -> None:
    """Reject keys that resemble execution capabilities, permissions, or authority."""
    clean_k = key.strip().lower()
    if clean_k in FORBIDDEN_AUTHORITY_KEYS:
        raise AgentProfileAuthorityViolationError(
            f"Authority-like field '{key}' is forbidden in AgentProfile {context}"
        )
    # Check prefixes / suffixes indicating execution authority
    for forbidden in ("allow_", "can_", "grant_"):
        if clean_k.startswith(forbidden):
            for suffix in ("shell", "network", "exec", "write", "read", "push", "commit", "approve", "delete"):
                if suffix in clean_k:
                    raise AgentProfileAuthorityViolationError(
                        f"Authority-like permission '{key}' is forbidden in AgentProfile {context}"
                    )
    for forbidden_token in ("capability", "capabilities", "permission", "permissions"):
        if forbidden_token in clean_k:
            raise AgentProfileAuthorityViolationError(
                f"Authority-like permission '{key}' is forbidden in AgentProfile {context}"
            )


def _validate_and_freeze_mapping(
    raw: Any,
    *,
    mapping_name: str,
    max_keys: int,
    depth: int = 1,
    reject_authority: bool = True,
) -> types.MappingProxyType[str, Any]:
    """Recursively validate JSON-serializable mapping, enforce bounds, and freeze into MappingProxyType."""
    if not isinstance(raw, Mapping):
        raise AgentProfileValidationError(
            f"{mapping_name} must be a mapping/dict, got {type(raw)}"
        )
    if depth > MAX_METADATA_DEPTH:
        raise AgentProfileValidationError(
            f"{mapping_name} nesting depth exceeds maximum limit of {MAX_METADATA_DEPTH}"
        )
    if len(raw) > max_keys:
        raise AgentProfileValidationError(
            f"{mapping_name} keys count ({len(raw)}) exceeds maximum allowed ({max_keys})"
        )

    frozen_dict: Dict[str, Any] = {}
    for k, v in raw.items():
        if type(k) is not str:
            raise AgentProfileValidationError(
                f"Keys in {mapping_name} must be strings, got {type(k)}"
            )
        clean_k = k.strip()
        if not clean_k:
            raise AgentProfileValidationError(f"Keys in {mapping_name} cannot be empty")
        if len(clean_k) > MAX_METADATA_KEY_CHARS:
            raise AgentProfileValidationError(
                f"Key '{clean_k}' in {mapping_name} exceeds maximum length ({MAX_METADATA_KEY_CHARS})"
            )
        if re.search(r"[\x00-\x1f\x7f]", clean_k):
            raise AgentProfileValidationError(
                f"Control characters forbidden in key '{clean_k}' of {mapping_name}"
            )

        if reject_authority:
            _check_authority_key(clean_k, mapping_name)

        frozen_dict[clean_k] = _validate_and_freeze_value(
            v,
            context=f"{mapping_name}['{clean_k}']",
            depth=depth + 1,
            reject_authority=reject_authority,
        )

    return types.MappingProxyType(frozen_dict)


def _validate_and_freeze_value(
    val: Any,
    *,
    context: str,
    depth: int,
    reject_authority: bool,
) -> Any:
    """Validate that value is JSON-compatible and deeply immutable."""
    if val is None or isinstance(val, (bool, int, float)):
        if isinstance(val, float) and not math.isfinite(val):
            raise AgentProfileValidationError(f"Non-finite float value not allowed in {context}")
        return val
    elif isinstance(val, str):
        if len(val) > MAX_STRING_VALUE_CHARS:
            raise AgentProfileValidationError(
                f"String value in {context} exceeds maximum length of {MAX_STRING_VALUE_CHARS}"
            )
        return val
    elif isinstance(val, Mapping):
        return _validate_and_freeze_mapping(
            val,
            mapping_name=context,
            max_keys=MAX_METADATA_KEYS,
            depth=depth,
            reject_authority=reject_authority,
        )
    elif isinstance(val, (list, tuple, set, frozenset)):
        if depth > MAX_METADATA_DEPTH:
            raise AgentProfileValidationError(
                f"Nesting depth in {context} exceeds maximum limit of {MAX_METADATA_DEPTH}"
            )
        if len(val) > MAX_METADATA_KEYS:
            raise AgentProfileValidationError(
                f"Collection length in {context} exceeds maximum limit of {MAX_METADATA_KEYS}"
            )
        return tuple(
            _validate_and_freeze_value(
                item,
                context=f"{context}[{i}]",
                depth=depth + 1,
                reject_authority=reject_authority,
            )
            for i, item in enumerate(val)
        )
    else:
        # Non-JSON types (functions, classes, modules, sockets, file handles, etc.) are strictly rejected
        raise AgentProfileValidationError(
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
# AgentProfile Domain Model
# =============================================================================


@dataclass(frozen=True)
class AgentProfile:
    """Immutable domain representation of an agent's specialization profile.

    Crucial Architectural Invariant:
    AGENT PROFILE ≠ AUTHORIZATION.
    An AgentProfile describes specialization metadata (task types, domains, skills,
    constraints, descriptive metadata). It never confers runtime execution authority,
    filesystem access, network privileges, shell commands, approval powers, or transaction ownership.
    """

    profile_id: str
    display_name: str
    description: str
    task_types: Tuple[str, ...] = ()
    domains: Tuple[str, ...] = ()
    skills: Tuple[str, ...] = ()
    constraints: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = CURRENT_AGENT_PROFILE_SCHEMA_VERSION
    created_at: float = field(default_factory=time.time)
    digest: str = ""

    def __post_init__(self) -> None:
        # 1. Profile ID
        clean_id = validate_profile_id(self.profile_id)
        object.__setattr__(self, "profile_id", clean_id)

        # 2. Schema version
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise AgentProfileValidationError("schema_version must be a positive integer")

        # 3. Display name
        if type(self.display_name) is not str:
            raise AgentProfileValidationError(
                f"display_name must be a string, got {type(self.display_name)}"
            )
        clean_name = self.display_name.strip()
        if not clean_name:
            raise AgentProfileValidationError("display_name cannot be empty")
        if len(clean_name) > MAX_DISPLAY_NAME_CHARS:
            raise AgentProfileValidationError(
                f"display_name length ({len(clean_name)}) exceeds limit ({MAX_DISPLAY_NAME_CHARS})"
            )
        if re.search(r"[\x00-\x1f\x7f]", clean_name):
            raise AgentProfileValidationError("Control characters forbidden in display_name")
        object.__setattr__(self, "display_name", clean_name)

        # 4. Description
        if type(self.description) is not str:
            raise AgentProfileValidationError(
                f"description must be a string, got {type(self.description)}"
            )
        clean_desc = self.description.strip()
        if len(clean_desc) > MAX_DESCRIPTION_CHARS:
            raise AgentProfileValidationError(
                f"description length ({len(clean_desc)}) exceeds limit ({MAX_DESCRIPTION_CHARS})"
            )
        object.__setattr__(self, "description", clean_desc)

        # 5. Task types (bounded immutable tuple of sorted unique tokens)
        clean_tasks = _validate_and_normalize_string_collection(
            self.task_types,
            collection_name="task_types",
            max_count=MAX_TASK_TYPES_COUNT,
            max_item_len=MAX_TASK_TYPE_CHARS,
        )
        object.__setattr__(self, "task_types", clean_tasks)

        # 6. Domains (bounded immutable tuple of sorted unique tokens)
        clean_domains = _validate_and_normalize_string_collection(
            self.domains,
            collection_name="domains",
            max_count=MAX_DOMAINS_COUNT,
            max_item_len=MAX_DOMAIN_CHARS,
        )
        object.__setattr__(self, "domains", clean_domains)

        # 7. Skills (bounded immutable tuple of sorted unique tokens)
        clean_skills = _validate_and_normalize_string_collection(
            self.skills,
            collection_name="skills",
            max_count=MAX_SKILLS_COUNT,
            max_item_len=MAX_SKILL_CHARS,
        )
        object.__setattr__(self, "skills", clean_skills)

        # 8. Constraints (deeply immutable MappingProxyType, rejects authority fields)
        clean_constraints = _validate_and_freeze_mapping(
            self.constraints,
            mapping_name="constraints",
            max_keys=MAX_CONSTRAINTS_COUNT,
            reject_authority=True,
        )
        object.__setattr__(self, "constraints", clean_constraints)

        # 9. Metadata (deeply immutable MappingProxyType, rejects authority fields)
        clean_metadata = _validate_and_freeze_mapping(
            self.metadata,
            mapping_name="metadata",
            max_keys=MAX_METADATA_KEYS,
            reject_authority=True,
        )
        object.__setattr__(self, "metadata", clean_metadata)

        # 10. created_at (finite timestamp)
        if not isinstance(self.created_at, (int, float)) or not math.isfinite(self.created_at):
            raise AgentProfileValidationError("created_at must be a valid finite number")
        object.__setattr__(self, "created_at", float(self.created_at))

        # 11. Secret rejection across all fields
        _reject_profile_secrets(self)

        # 12. Total serialized size check
        serialized_size = len(self._compute_canonical_json().encode("utf-8"))
        if serialized_size > MAX_PROFILE_SERIALIZED_BYTES:
            raise AgentProfileValidationError(
                f"Serialized profile size ({serialized_size} bytes) exceeds limit ({MAX_PROFILE_SERIALIZED_BYTES} bytes)"
            )

        # 13. Digest computation & verification
        expected_digest = self.compute_digest()
        if not self.digest:
            object.__setattr__(self, "digest", expected_digest)
        elif self.digest != expected_digest:
            raise AgentProfileIntegrityError(
                f"AgentProfile digest mismatch: expected '{expected_digest}', got '{self.digest}'"
            )

    @property
    def id(self) -> str:
        """Convenience alias for profile_id."""
        return self.profile_id

    def _compute_canonical_json(self) -> str:
        """Produce canonical JSON representation of semantic profile fields.

        Volatile created_at and object address are strictly excluded from semantic identity.
        """
        payload = {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "display_name": self.display_name,
            "description": self.description,
            "task_types": list(self.task_types),
            "domains": list(self.domains),
            "skills": list(self.skills),
            "constraints": _unfreeze_for_json(self.constraints),
            "metadata": _unfreeze_for_json(self.metadata),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def compute_digest(self) -> str:
        """Compute SHA-256 digest over canonical semantic representation."""
        return hashlib.sha256(self._compute_canonical_json().encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        """Strict, deterministic serialization to dictionary."""
        return {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "display_name": self.display_name,
            "description": self.description,
            "task_types": list(self.task_types),
            "domains": list(self.domains),
            "skills": list(self.skills),
            "constraints": _unfreeze_for_json(self.constraints),
            "metadata": _unfreeze_for_json(self.metadata),
            "created_at": self.created_at,
            "digest": self.digest,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> AgentProfile:
        """Strict deserialization from dictionary."""
        if type(data) is not dict:
            raise AgentProfileValidationError(
                f"Expected dict for profile deserialization, got {type(data)}"
            )

        ALLOWED_KEYS = {
            "schema_version",
            "profile_id",
            "display_name",
            "description",
            "task_types",
            "domains",
            "skills",
            "constraints",
            "metadata",
            "created_at",
            "digest",
        }
        REQUIRED_KEYS = {
            "profile_id",
            "display_name",
            "description",
        }

        extra_keys = set(data.keys()) - ALLOWED_KEYS
        if extra_keys:
            # Explicitly detect attempted authority smuggling
            for k in extra_keys:
                _check_authority_key(k, "deserialization")
            raise AgentProfileValidationError(
                f"Unknown or unexpected keys in profile data: {sorted(extra_keys)}"
            )

        missing_keys = REQUIRED_KEYS - set(data.keys())
        if missing_keys:
            raise AgentProfileValidationError(
                f"Missing required keys in profile data: {sorted(missing_keys)}"
            )

        return cls(
            profile_id=data["profile_id"],
            display_name=data["display_name"],
            description=data["description"],
            task_types=data.get("task_types", ()),
            domains=data.get("domains", ()),
            skills=data.get("skills", ()),
            constraints=data.get("constraints", {}),
            metadata=data.get("metadata", {}),
            schema_version=data.get("schema_version", CURRENT_AGENT_PROFILE_SCHEMA_VERSION),
            created_at=data.get("created_at", time.time()),
            digest=data.get("digest", ""),
        )


def _reject_profile_secrets(profile: AgentProfile) -> None:
    """Validate that none of the profile fields contain credential or secret patterns."""
    payload = {
        "profile_id": profile.profile_id,
        "display_name": profile.display_name,
        "description": profile.description,
        "task_types": list(profile.task_types),
        "domains": list(profile.domains),
        "skills": list(profile.skills),
        "constraints": _unfreeze_for_json(profile.constraints),
        "metadata": _unfreeze_for_json(profile.metadata),
    }
    _reject_secrets_recursive(payload)


# =============================================================================
# AgentProfileRegistry
# =============================================================================


class AgentProfileRegistry:
    """Bounded, deterministic in-memory registry of AgentProfile definitions.

    The registry answers only: 'Which agent profiles exist?'
    It does NOT answer: 'What is this agent allowed to execute?'
    REGISTRY ≠ AUTHORIZATION.
    """

    def __init__(self, profiles: Optional[Sequence[AgentProfile]] = None) -> None:
        self._profiles: Dict[str, AgentProfile] = {}
        if profiles:
            for p in profiles:
                self.register(p)

    def register(self, profile: AgentProfile) -> None:
        """Register an immutable AgentProfile. Fails if profile_id already exists."""
        if not isinstance(profile, AgentProfile):
            raise AgentProfileValidationError(
                f"Expected AgentProfile instance, got {type(profile)}"
            )
        if len(self._profiles) >= MAX_REGISTRY_CAPACITY:
            raise AgentProfileError(
                f"Registry capacity limit reached ({MAX_REGISTRY_CAPACITY} profiles)"
            )
        if profile.profile_id in self._profiles:
            raise AgentProfileDuplicateError(
                f"Profile '{profile.profile_id}' is already registered in registry"
            )
        self._profiles[profile.profile_id] = profile

    def get(self, profile_id: str) -> Optional[AgentProfile]:
        """Look up a profile by profile_id. Returns None if not found."""
        if type(profile_id) is not str:
            raise AgentProfileValidationError(
                f"profile_id must be a string, got {type(profile_id)}"
            )
        return self._profiles.get(profile_id.strip())

    def require(self, profile_id: str) -> AgentProfile:
        """Look up profile by profile_id or raise AgentProfileNotFoundError if missing."""
        profile = self.get(profile_id)
        if profile is None:
            raise AgentProfileNotFoundError(
                f"Profile '{profile_id}' not found in registry"
            )
        return profile

    def __getitem__(self, profile_id: str) -> AgentProfile:
        return self.require(profile_id)

    def list(self) -> Tuple[AgentProfile, ...]:
        """Return all registered profiles sorted deterministically by profile_id."""
        return tuple(self._profiles[pid] for pid in sorted(self._profiles.keys()))

    def remove(self, profile_id: str) -> AgentProfile:
        """Remove and return a profile. Raises AgentProfileNotFoundError if not found."""
        if type(profile_id) is not str:
            raise AgentProfileValidationError(
                f"profile_id must be a string, got {type(profile_id)}"
            )
        clean_id = profile_id.strip()
        if clean_id not in self._profiles:
            raise AgentProfileNotFoundError(
                f"Profile '{clean_id}' not found in registry"
            )
        return self._profiles.pop(clean_id)

    def __contains__(self, profile_id: str) -> bool:
        if type(profile_id) is not str:
            return False
        return profile_id.strip() in self._profiles

    def __len__(self) -> int:
        return len(self._profiles)

    def resolve(self, request: ProfileResolutionRequest) -> ProfileResolutionResult:
        """Resolve a ProfileResolutionRequest against registered profiles deterministically."""
        return resolve_profile(request, self)

    def require_resolve(self, request: ProfileResolutionRequest) -> AgentProfile:
        """Resolve request or raise NoMatchingProfileError / AmbiguousProfileResolutionError."""
        result = self.resolve(request)
        if result.status == ProfileResolutionStatus.MATCHED:
            if result.profile_id is None:
                raise AgentProfileError("Resolution reported MATCHED but profile_id is None")
            return self.require(result.profile_id)
        elif result.status == ProfileResolutionStatus.AMBIGUOUS:
            raise AmbiguousProfileResolutionError(result.reason)
        else:
            raise NoMatchingProfileError(result.reason)


# =============================================================================
# Profile Resolution Domain Models and Matching Engine
# =============================================================================


class ProfileResolutionStatus(str, Enum):
    """Status outcomes for deterministic profile resolution.

    - MATCHED: Exactly one profile scored highest with score > 0.
    - NO_MATCH: No registered profiles matched requested task_type, domain, or skills.
    - AMBIGUOUS: Two or more profiles tied for highest score; resolution fails closed.
    """
    MATCHED = "MATCHED"
    NO_MATCH = "NO_MATCH"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class ProfileResolutionRequest:
    """Bounded, immutable specification of task requirements for profile resolution.

    Architectural Invariant:
    A ProfileResolutionRequest is purely descriptive specialization metadata.
    It NEVER conveys, requests, or grants execution authority.
    """
    task_type: Optional[str] = None
    domain: Optional[str] = None
    skills: Tuple[str, ...] = ()
    schema_version: int = CURRENT_PROFILE_RESOLUTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise AgentProfileValidationError("schema_version must be a positive integer")

        # Validate task_type
        if self.task_type is not None:
            if type(self.task_type) is not str:
                raise AgentProfileValidationError(
                    f"task_type must be a string or None, got {type(self.task_type)}"
                )
            clean_task = self.task_type.strip()
            if not clean_task:
                clean_task = None
            else:
                if len(clean_task) > MAX_RESOLUTION_TASK_TYPE_CHARS:
                    raise AgentProfileValidationError(
                        f"task_type length ({len(clean_task)}) exceeds limit ({MAX_RESOLUTION_TASK_TYPE_CHARS})"
                    )
                if re.search(r"[\x00-\x1f\x7f]", clean_task):
                    raise AgentProfileValidationError("Control characters forbidden in task_type")
                _reject_string_secrets(clean_task, "task_type")
            object.__setattr__(self, "task_type", clean_task)

        # Validate domain
        if self.domain is not None:
            if type(self.domain) is not str:
                raise AgentProfileValidationError(
                    f"domain must be a string or None, got {type(self.domain)}"
                )
            clean_dom = self.domain.strip()
            if not clean_dom:
                clean_dom = None
            else:
                if len(clean_dom) > MAX_RESOLUTION_DOMAIN_CHARS:
                    raise AgentProfileValidationError(
                        f"domain length ({len(clean_dom)}) exceeds limit ({MAX_RESOLUTION_DOMAIN_CHARS})"
                    )
                if re.search(r"[\x00-\x1f\x7f]", clean_dom):
                    raise AgentProfileValidationError("Control characters forbidden in domain")
                _reject_string_secrets(clean_dom, "domain")
            object.__setattr__(self, "domain", clean_dom)

        # Validate and normalize skills collection
        clean_skills = _validate_and_normalize_string_collection(
            self.skills,
            collection_name="skills",
            max_count=MAX_RESOLUTION_SKILLS_COUNT,
            max_item_len=MAX_RESOLUTION_SKILL_CHARS,
        )
        for s in clean_skills:
            _reject_string_secrets(s, "skills")
        object.__setattr__(self, "skills", clean_skills)

        # Total serialized size check
        serialized_size = len(json.dumps(self.to_dict()).encode("utf-8"))
        if serialized_size > MAX_RESOLUTION_REQUEST_SERIALIZED_BYTES:
            raise AgentProfileValidationError(
                f"Serialized request size ({serialized_size} bytes) exceeds limit ({MAX_RESOLUTION_REQUEST_SERIALIZED_BYTES} bytes)"
            )

    def to_dict(self) -> Dict[str, Any]:
        """Strict deterministic dictionary serialization."""
        return {
            "schema_version": self.schema_version,
            "task_type": self.task_type,
            "domain": self.domain,
            "skills": list(self.skills),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ProfileResolutionRequest:
        """Strict deserialization from dictionary."""
        if type(data) is not dict:
            raise AgentProfileValidationError(
                f"Expected dict for request deserialization, got {type(data)}"
            )
        ALLOWED_KEYS = {"schema_version", "task_type", "domain", "skills"}
        extra_keys = set(data.keys()) - ALLOWED_KEYS
        if extra_keys:
            for k in extra_keys:
                _check_authority_key(k, "resolution request deserialization")
            raise AgentProfileValidationError(
                f"Unknown or unexpected keys in request data: {sorted(extra_keys)}"
            )
        return cls(
            task_type=data.get("task_type"),
            domain=data.get("domain"),
            skills=data.get("skills", ()),
            schema_version=data.get("schema_version", CURRENT_PROFILE_RESOLUTION_SCHEMA_VERSION),
        )


@dataclass(frozen=True)
class ProfileResolutionResult:
    """Bounded, immutable result of deterministic profile resolution.

    Architectural Invariant:
    A ProfileResolutionResult is non-authoritative diagnostic output.
    It confers ZERO execution capability, authority, approvals, or transactions.
    """
    status: ProfileResolutionStatus
    profile_id: Optional[str] = None
    candidate_ids: Tuple[str, ...] = ()
    score: float = 0.0
    reason: str = ""
    matched_task_type: Optional[str] = None
    matched_domain: Optional[str] = None
    matched_skills: Tuple[str, ...] = ()
    schema_version: int = CURRENT_PROFILE_RESOLUTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.status, ProfileResolutionStatus):
            raise AgentProfileValidationError(f"Invalid status: {self.status}")

        if self.profile_id is not None:
            if type(self.profile_id) is not str:
                raise AgentProfileValidationError("profile_id must be a string or None")
            clean_id = self.profile_id.strip()
            if not clean_id:
                raise AgentProfileValidationError("profile_id cannot be empty when provided")
            object.__setattr__(self, "profile_id", clean_id)

        if type(self.candidate_ids) not in (list, tuple, set, frozenset):
            raise AgentProfileValidationError("candidate_ids must be a sequence or set")
        if len(self.candidate_ids) > MAX_RESOLUTION_CANDIDATE_IDS:
            raise AgentProfileValidationError(
                f"candidate_ids count exceeds maximum ({MAX_RESOLUTION_CANDIDATE_IDS})"
            )
        clean_cands = tuple(sorted(str(c).strip() for c in self.candidate_ids if str(c).strip()))
        object.__setattr__(self, "candidate_ids", clean_cands)

        if not isinstance(self.score, (int, float)) or not math.isfinite(self.score):
            raise AgentProfileValidationError("score must be a finite number")
        object.__setattr__(self, "score", float(self.score))

        clean_reason = scrub_secrets(str(self.reason).strip())
        if len(clean_reason) > MAX_RESOLUTION_REASON_CHARS:
            clean_reason = clean_reason[:MAX_RESOLUTION_REASON_CHARS]
        object.__setattr__(self, "reason", clean_reason)

        if self.matched_task_type is not None:
            clean_mtt = str(self.matched_task_type).strip() or None
            object.__setattr__(self, "matched_task_type", clean_mtt)

        if self.matched_domain is not None:
            clean_mdom = str(self.matched_domain).strip() or None
            object.__setattr__(self, "matched_domain", clean_mdom)

        if type(self.matched_skills) not in (list, tuple, set, frozenset):
            raise AgentProfileValidationError("matched_skills must be a sequence or set")
        clean_mskills = tuple(sorted(str(s).strip() for s in self.matched_skills if str(s).strip()))
        object.__setattr__(self, "matched_skills", clean_mskills)

        serialized_size = len(json.dumps(self.to_dict()).encode("utf-8"))
        if serialized_size > MAX_RESOLUTION_RESULT_SERIALIZED_BYTES:
            raise AgentProfileValidationError(
                f"Serialized result size ({serialized_size} bytes) exceeds limit ({MAX_RESOLUTION_RESULT_SERIALIZED_BYTES} bytes)"
            )

    def to_dict(self) -> Dict[str, Any]:
        """Strict deterministic dictionary serialization."""
        return {
            "schema_version": self.schema_version,
            "status": self.status.value,
            "profile_id": self.profile_id,
            "candidate_ids": list(self.candidate_ids),
            "score": self.score,
            "reason": self.reason,
            "matched_task_type": self.matched_task_type,
            "matched_domain": self.matched_domain,
            "matched_skills": list(self.matched_skills),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ProfileResolutionResult:
        """Strict deserialization from dictionary."""
        if type(data) is not dict:
            raise AgentProfileValidationError(
                f"Expected dict for result deserialization, got {type(data)}"
            )
        ALLOWED_KEYS = {
            "schema_version",
            "status",
            "profile_id",
            "candidate_ids",
            "score",
            "reason",
            "matched_task_type",
            "matched_domain",
            "matched_skills",
        }
        extra_keys = set(data.keys()) - ALLOWED_KEYS
        if extra_keys:
            for k in extra_keys:
                _check_authority_key(k, "resolution result deserialization")
            raise AgentProfileValidationError(
                f"Unknown or unexpected keys in result data: {sorted(extra_keys)}"
            )
        raw_status = data.get("status")
        try:
            status = ProfileResolutionStatus(raw_status)
        except (ValueError, KeyError) as exc:
            raise AgentProfileValidationError(f"Invalid resolution status: {raw_status}") from exc
        return cls(
            status=status,
            profile_id=data.get("profile_id"),
            candidate_ids=data.get("candidate_ids", ()),
            score=data.get("score", 0.0),
            reason=data.get("reason", ""),
            matched_task_type=data.get("matched_task_type"),
            matched_domain=data.get("matched_domain"),
            matched_skills=data.get("matched_skills", ()),
            schema_version=data.get("schema_version", CURRENT_PROFILE_RESOLUTION_SCHEMA_VERSION),
        )


def resolve_profile(
    request: ProfileResolutionRequest,
    registry: AgentProfileRegistry,
) -> ProfileResolutionResult:
    """Deterministically resolve a ProfileResolutionRequest against registered profiles.

    Scoring formula:
    - Task type match: +10.0 points
    - Domain match: +5.0 points
    - Skill match: +1.0 point per matching skill token

    Invariants:
    - Zero execution authority conferred.
    - Zero subprocess, filesystem, network, or LLM calls.
    - Completely deterministic (no random, no clock, no hash randomization).
    - Ties fail closed with AMBIGUOUS status.
    """
    if not isinstance(request, ProfileResolutionRequest):
        raise AgentProfileValidationError(
            f"Expected ProfileResolutionRequest, got {type(request)}"
        )
    if not isinstance(registry, AgentProfileRegistry):
        raise AgentProfileValidationError(
            f"Expected AgentProfileRegistry, got {type(registry)}"
        )

    # Empty request guard: no criteria requested -> NO_MATCH
    if request.task_type is None and request.domain is None and not request.skills:
        return ProfileResolutionResult(
            status=ProfileResolutionStatus.NO_MATCH,
            profile_id=None,
            candidate_ids=(),
            score=0.0,
            reason="Empty resolution request specified no task_type, domain, or skills.",
        )

    # Registry listing is deterministically sorted by profile_id
    profiles = registry.list()
    if not profiles:
        return ProfileResolutionResult(
            status=ProfileResolutionStatus.NO_MATCH,
            profile_id=None,
            candidate_ids=(),
            score=0.0,
            reason="Profile registry is empty.",
        )

    scored_candidates: List[Dict[str, Any]] = []

    for profile in profiles:
        task_score = 0.0
        matched_tt: Optional[str] = None
        if request.task_type is not None:
            req_tt_lower = request.task_type.lower()
            found_tt = next((tt for tt in profile.task_types if tt.lower() == req_tt_lower), None)
            if found_tt is not None:
                task_score = TASK_TYPE_MATCH_WEIGHT
                matched_tt = found_tt

        domain_score = 0.0
        matched_dom: Optional[str] = None
        if request.domain is not None:
            req_dom_lower = request.domain.lower()
            found_dom = next((d for d in profile.domains if d.lower() == req_dom_lower), None)
            if found_dom is not None:
                domain_score = DOMAIN_MATCH_WEIGHT
                matched_dom = found_dom

        skill_score = 0.0
        matched_skills: Tuple[str, ...] = ()
        if request.skills:
            p_skills_map = {s.lower(): s for s in profile.skills}
            overlap = [p_skills_map[rs.lower()] for rs in request.skills if rs.lower() in p_skills_map]
            if overlap:
                matched_skills = tuple(sorted(set(overlap)))
                skill_score = len(matched_skills) * SKILL_MATCH_WEIGHT

        total_score = task_score + domain_score + skill_score
        if total_score > 0.0:
            scored_candidates.append({
                "profile": profile,
                "score": total_score,
                "matched_tt": matched_tt,
                "matched_dom": matched_dom,
                "matched_skills": matched_skills,
            })

    if not scored_candidates:
        return ProfileResolutionResult(
            status=ProfileResolutionStatus.NO_MATCH,
            profile_id=None,
            candidate_ids=(),
            score=0.0,
            reason="No registered profile matched any requested task_type, domain, or skills.",
        )

    max_score = max(c["score"] for c in scored_candidates)
    top_candidates = [c for c in scored_candidates if c["score"] == max_score]

    if len(top_candidates) == 1:
        best = top_candidates[0]
        p_id = best["profile"].profile_id
        matched_skills_str = ", ".join(best["matched_skills"]) if best["matched_skills"] else "none"
        reason = (
            f"Profile '{p_id}' matched with score {max_score:g} "
            f"(task_type: {best['matched_tt']}, domain: {best['matched_dom']}, skills: {matched_skills_str})"
        )
        return ProfileResolutionResult(
            status=ProfileResolutionStatus.MATCHED,
            profile_id=p_id,
            candidate_ids=(p_id,),
            score=max_score,
            reason=reason,
            matched_task_type=best["matched_tt"],
            matched_domain=best["matched_dom"],
            matched_skills=best["matched_skills"],
        )

    # 2 or more candidates tied with equal best score -> fail closed with AMBIGUOUS
    tied_ids = tuple(sorted(c["profile"].profile_id for c in top_candidates))
    reason = (
        f"Ambiguous profile resolution: {len(top_candidates)} profiles tied with score {max_score:g}: {list(tied_ids)}"
    )
    return ProfileResolutionResult(
        status=ProfileResolutionStatus.AMBIGUOUS,
        profile_id=None,
        candidate_ids=tied_ids,
        score=max_score,
        reason=reason,
    )


__all__ = [
    "AgentProfile",
    "AgentProfileRegistry",
    "ProfileResolutionRequest",
    "ProfileResolutionResult",
    "ProfileResolutionStatus",
    "AgentProfileError",
    "AgentProfileValidationError",
    "AgentProfileAuthorityViolationError",
    "AgentProfileIntegrityError",
    "AgentProfileDuplicateError",
    "AgentProfileNotFoundError",
    "AmbiguousProfileResolutionError",
    "NoMatchingProfileError",
    "resolve_profile",
    "CURRENT_AGENT_PROFILE_SCHEMA_VERSION",
    "CURRENT_PROFILE_RESOLUTION_SCHEMA_VERSION",
    "MAX_PROFILE_ID_CHARS",
    "MAX_DISPLAY_NAME_CHARS",
    "MAX_DESCRIPTION_CHARS",
    "MAX_TASK_TYPES_COUNT",
    "MAX_TASK_TYPE_CHARS",
    "MAX_DOMAINS_COUNT",
    "MAX_DOMAIN_CHARS",
    "MAX_SKILLS_COUNT",
    "MAX_SKILL_CHARS",
    "MAX_CONSTRAINTS_COUNT",
    "MAX_CONSTRAINT_KEY_CHARS",
    "MAX_METADATA_KEYS",
    "MAX_METADATA_KEY_CHARS",
    "MAX_METADATA_DEPTH",
    "MAX_PROFILE_SERIALIZED_BYTES",
    "MAX_REGISTRY_CAPACITY",
    "MAX_RESOLUTION_TASK_TYPE_CHARS",
    "MAX_RESOLUTION_DOMAIN_CHARS",
    "MAX_RESOLUTION_SKILLS_COUNT",
    "MAX_RESOLUTION_SKILL_CHARS",
    "MAX_RESOLUTION_CANDIDATE_IDS",
    "MAX_RESOLUTION_REASON_CHARS",
    "MAX_RESOLUTION_REQUEST_SERIALIZED_BYTES",
    "MAX_RESOLUTION_RESULT_SERIALIZED_BYTES",
    "TASK_TYPE_MATCH_WEIGHT",
    "DOMAIN_MATCH_WEIGHT",
    "SKILL_MATCH_WEIGHT",
    "FORBIDDEN_AUTHORITY_KEYS",
    "validate_profile_id",
]
