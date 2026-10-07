"""P1.3J Subagent Routing and Specialization Domain.

Provides deterministic routing from planned tasks to eligible subagent profiles
based on task requirements and declared capability constraints.

Architectural Principles:
- The router is DESCRIPTIVE selection logic only.
- The router holds ZERO execution authority, approval authority, or filesystem permissions.
- The router NEVER executes commands, spawns subprocesses, calls LLMs, or mutates files.
- The router CANNOT grant or expand capabilities.
- Selected profiles constrain, rather than expand, delegation authority.
- Final delegation authority is always bounded by: Parent ∩ Profile ∩ Task.
- orchestrator.py remains the SOLE execution engine in BrainFrog.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple, Union

from core.runtime.approval import RiskClass
from core.runtime.capabilities import (
    Capabilities,
    FilesystemPolicy,
    GitPolicy,
    NetworkPolicy,
    ShellPolicy,
    attenuate_capabilities,
)
from core.runtime.child_work import ChildWork, validate_child_work_id
from core.runtime.contract import reject_secrets
from core.runtime.delegation import (
    DelegationAttenuationError,
    DelegationContract,
    is_network_subset,
    is_path_subset,
    normalize_endpoint,
    normalize_target_path,
    validate_delegation_id,
)
from core.runtime.permissions import CANONICAL_ACTION_MAP
from core.runtime.secret_scrubbing import scrub_secrets
from core.runtime.subagent import Subagent, validate_subagent_id
from core.runtime.work import Work

CURRENT_SUBAGENT_ROUTING_SCHEMA_VERSION: int = 1

# Resource bounds
MAX_PROFILES: int = 64
MAX_CAPABILITIES_PER_PROFILE: int = 64
MAX_SUPPORTED_OPERATIONS: int = 32
MAX_SUPPORTED_TASK_TYPES: int = 16
MAX_TASK_REQUIREMENTS: int = 64
MAX_SCOPE_TARGETS: int = 64
MAX_NETWORK_ENDPOINTS: int = 16
MAX_METADATA_BYTES: int = 65536
MAX_PROFILE_ID_CHARS: int = 128
MAX_NAME_CHARS: int = 256
MAX_DESCRIPTION_CHARS: int = 2048
MAX_TASK_ID_CHARS: int = 128
MAX_OPERATION_CHARS: int = 64
MAX_REASON_CHARS: int = 1024

_INVALID_ID_CHARS = re.compile(r"[\x00-\x1f\x7f/\\:*\?\"<>\|]|(?:\.\.)")

_RISK_ORDER: Dict[RiskClass, int] = {
    RiskClass.LOW: 1,
    RiskClass.MEDIUM: 2,
    RiskClass.HIGH: 3,
    RiskClass.CRITICAL: 4,
}


# =============================================================================
# Exception Hierarchy
# =============================================================================

class SubagentRoutingError(Exception):
    """Base exception for subagent routing and profile errors."""
    pass


class NoEligibleSubagentError(SubagentRoutingError):
    """Raised when no registered profile satisfies task requirements."""
    pass


class ProfileValidationError(SubagentRoutingError):
    """Raised when a SubagentProfile is malformed or invalid."""
    pass


class DuplicateProfileError(SubagentRoutingError):
    """Raised when attempting to register a profile with an existing profile_id."""
    pass


class ProfileRegistryLimitError(SubagentRoutingError):
    """Raised when the profile registry exceeds maximum capacity."""
    pass


class TaskRoutingValidationError(SubagentRoutingError):
    """Raised when a TaskRoutingRequest is malformed or invalid."""
    pass


# =============================================================================
# Task Taxonomy
# =============================================================================

class TaskType(str, Enum):
    """Deterministic closed taxonomy of supported task classifications."""
    RESEARCH = "research"
    IMPLEMENTATION = "implementation"
    TESTING = "testing"
    REVIEW = "review"
    DOCUMENTATION = "documentation"
    REFACTOR = "refactor"
    VERIFICATION = "verification"


def parse_task_type(val: Union[TaskType, str]) -> TaskType:
    """Parse and normalize task type, failing closed on unknown values."""
    if isinstance(val, TaskType):
        return val
    if type(val) is not str:
        raise TaskRoutingValidationError(f"Task type must be a string or TaskType, got {type(val)}")
    clean = val.strip().lower()
    try:
        return TaskType(clean)
    except ValueError:
        raise TaskRoutingValidationError(
            f"Unknown task type '{val}'. Supported types: {[t.value for t in TaskType]}"
        )


def validate_profile_id(profile_id: str) -> str:
    """Validate profile ID against traversal, drive letters, and invalid chars."""
    if type(profile_id) is not str:
        raise ProfileValidationError(f"Profile ID must be a string, got {type(profile_id)}")
    clean = profile_id.strip()
    if not clean or len(clean) > MAX_PROFILE_ID_CHARS:
        raise ProfileValidationError(
            f"Profile ID must be a non-empty string <= {MAX_PROFILE_ID_CHARS} chars, got length {len(clean)}"
        )
    if _INVALID_ID_CHARS.search(clean) or clean.startswith((".", "~", "/", "\\")):
        raise ProfileValidationError(f"Invalid characters or traversal detected in profile ID: '{clean}'")
    return clean


def validate_task_id(task_id: str) -> str:
    """Validate task ID against traversal, drive letters, and invalid chars."""
    if type(task_id) is not str:
        raise TaskRoutingValidationError(f"Task ID must be a string, got {type(task_id)}")
    clean = task_id.strip()
    if not clean or len(clean) > MAX_TASK_ID_CHARS:
        raise TaskRoutingValidationError(
            f"Task ID must be a non-empty string <= {MAX_TASK_ID_CHARS} chars, got length {len(clean)}"
        )
    if _INVALID_ID_CHARS.search(clean) or clean.startswith((".", "~", "/", "\\")):
        raise TaskRoutingValidationError(f"Invalid characters or traversal detected in task ID: '{clean}'")
    return clean


def canonicalize_operation(op: str) -> str:
    """Normalize and resolve operation synonyms through canonical mapping."""
    if type(op) is not str:
        raise TaskRoutingValidationError(f"Operation must be a string, got {type(op)}")
    clean = op.strip().lower()
    if not clean or len(clean) > MAX_OPERATION_CHARS:
        raise TaskRoutingValidationError(f"Operation must be non-empty and <= {MAX_OPERATION_CHARS} chars")
    return CANONICAL_ACTION_MAP.get(clean, clean)


# =============================================================================
# SubagentProfile Domain Model
# =============================================================================

@dataclass(frozen=True)
class SubagentProfile:
    """Immutable profile describing the specialized capabilities and scope of a subagent role.

    Architectural Invariant:
    A SubagentProfile is PURE DECLARATIVE METADATA.
    It contains NO code, NO callables, NO subprocess strings, and NO execution handles.
    """
    profile_id: str
    name: str
    description: str
    required_capabilities: Capabilities
    supported_operations: Tuple[str, ...]
    supported_task_types: Tuple[TaskType, ...]
    max_risk_class: RiskClass = RiskClass.LOW
    network_scope: Tuple[str, ...] = ()
    filesystem_scope: Tuple[str, ...] = ()
    git_policy: GitPolicy = field(default_factory=GitPolicy)
    metadata: Dict[str, Any] = field(default_factory=dict)
    schema_version: int = CURRENT_SUBAGENT_ROUTING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        clean_id = validate_profile_id(self.profile_id)
        object.__setattr__(self, "profile_id", clean_id)

        if type(self.name) is not str or not self.name.strip():
            raise ProfileValidationError("Profile name must be a non-empty string")
        if len(self.name) > MAX_NAME_CHARS:
            raise ProfileValidationError(f"Profile name exceeds maximum length of {MAX_NAME_CHARS}")
        clean_name = scrub_secrets(self.name.strip())
        object.__setattr__(self, "name", clean_name)

        if type(self.description) is not str or not self.description.strip():
            raise ProfileValidationError("Profile description must be a non-empty string")
        if len(self.description) > MAX_DESCRIPTION_CHARS:
            raise ProfileValidationError(f"Profile description exceeds maximum length of {MAX_DESCRIPTION_CHARS}")
        clean_desc = scrub_secrets(self.description.strip())
        object.__setattr__(self, "description", clean_desc)

        if not isinstance(self.required_capabilities, Capabilities):
            raise ProfileValidationError(
                f"required_capabilities must be a Capabilities instance, got {type(self.required_capabilities)}"
            )

        if not isinstance(self.supported_operations, (tuple, list)):
            raise ProfileValidationError("supported_operations must be a sequence of strings")
        if len(self.supported_operations) > MAX_SUPPORTED_OPERATIONS:
            raise ProfileValidationError(
                f"supported_operations count {len(self.supported_operations)} exceeds limit {MAX_SUPPORTED_OPERATIONS}"
            )
        clean_ops = tuple(canonicalize_operation(op) for op in self.supported_operations)
        object.__setattr__(self, "supported_operations", clean_ops)

        if not isinstance(self.supported_task_types, (tuple, list)):
            raise ProfileValidationError("supported_task_types must be a sequence of TaskType")
        if len(self.supported_task_types) > MAX_SUPPORTED_TASK_TYPES:
            raise ProfileValidationError(
                f"supported_task_types count {len(self.supported_task_types)} exceeds limit {MAX_SUPPORTED_TASK_TYPES}"
            )
        clean_types = tuple(parse_task_type(t) for t in self.supported_task_types)
        object.__setattr__(self, "supported_task_types", clean_types)

        if not isinstance(self.max_risk_class, RiskClass):
            if type(self.max_risk_class) is str:
                try:
                    object.__setattr__(self, "max_risk_class", RiskClass(self.max_risk_class.strip().upper()))
                except ValueError:
                    raise ProfileValidationError(f"Invalid risk class: '{self.max_risk_class}'")
            else:
                raise ProfileValidationError(f"max_risk_class must be a RiskClass, got {type(self.max_risk_class)}")

        if not isinstance(self.network_scope, (tuple, list)):
            raise ProfileValidationError("network_scope must be a sequence of strings")
        if len(self.network_scope) > MAX_NETWORK_ENDPOINTS:
            raise ProfileValidationError(f"network_scope count exceeds limit {MAX_NETWORK_ENDPOINTS}")
        clean_net = tuple(normalize_endpoint(ep)[1] for ep in self.network_scope)
        object.__setattr__(self, "network_scope", clean_net)

        if not isinstance(self.filesystem_scope, (tuple, list)):
            raise ProfileValidationError("filesystem_scope must be a sequence of path patterns")
        if len(self.filesystem_scope) > MAX_SCOPE_TARGETS:
            raise ProfileValidationError(f"filesystem_scope count exceeds limit {MAX_SCOPE_TARGETS}")
        clean_fs = tuple(normalize_target_path(p) for p in self.filesystem_scope)
        object.__setattr__(self, "filesystem_scope", clean_fs)

        if not isinstance(self.git_policy, GitPolicy):
            raise ProfileValidationError(f"git_policy must be a GitPolicy instance, got {type(self.git_policy)}")

        if not isinstance(self.metadata, dict):
            raise ProfileValidationError(f"metadata must be a dictionary, got {type(self.metadata)}")
        meta_bytes = len(json.dumps(self.metadata).encode("utf-8"))
        if meta_bytes > MAX_METADATA_BYTES:
            raise ProfileValidationError(f"metadata size ({meta_bytes} bytes) exceeds limit {MAX_METADATA_BYTES}")
        reject_secrets(self.metadata)

        if type(self.schema_version) is not int or self.schema_version < 1:
            raise ProfileValidationError("schema_version must be a positive integer")

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "name": self.name,
            "description": self.description,
            "required_capabilities": self.required_capabilities.to_dict(),
            "supported_operations": list(self.supported_operations),
            "supported_task_types": [t.value for t in self.supported_task_types],
            "max_risk_class": self.max_risk_class.value,
            "network_scope": list(self.network_scope),
            "filesystem_scope": list(self.filesystem_scope),
            "git_policy": self.git_policy.to_dict(),
            "metadata": dict(self.metadata),
        }
        reject_secrets(data)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SubagentProfile:
        if not isinstance(data, dict):
            raise ProfileValidationError(f"Expected dict for SubagentProfile.from_dict, got {type(data)}")
        reject_secrets(data)

        req_caps = Capabilities.from_dict(data.get("required_capabilities", {}))
        git_pol = GitPolicy.from_dict(data.get("git_policy", {}))
        risk_str = str(data.get("max_risk_class", RiskClass.LOW.value))
        risk_enum = RiskClass(risk_str.upper())

        types_raw = data.get("supported_task_types") or ()
        task_types = tuple(parse_task_type(t) for t in types_raw)

        return cls(
            profile_id=str(data.get("profile_id", "")),
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            required_capabilities=req_caps,
            supported_operations=tuple(str(op) for op in data.get("supported_operations", ())),
            supported_task_types=task_types,
            max_risk_class=risk_enum,
            network_scope=tuple(str(ep) for ep in data.get("network_scope", ())),
            filesystem_scope=tuple(str(p) for p in data.get("filesystem_scope", ())),
            git_policy=git_pol,
            metadata=dict(data.get("metadata") or {}),
            schema_version=int(data.get("schema_version", CURRENT_SUBAGENT_ROUTING_SCHEMA_VERSION)),
        )


# =============================================================================
# Task Routing Request Domain Model
# =============================================================================

@dataclass(frozen=True)
class TaskRoutingRequest:
    """Immutable requirement specification for routing a planned task to a subagent profile.

    Answers: What capabilities, operations, scope, and risk does this task require?
    Does not execute anything.
    """
    task_id: str
    task_type: TaskType
    required_capabilities: Capabilities
    operation: str
    risk_class: RiskClass = RiskClass.LOW
    target_scope: Tuple[str, ...] = ()
    network_requirements: Tuple[str, ...] = ()
    filesystem_requirements: Tuple[str, ...] = ()
    git_requirements: GitPolicy = field(default_factory=GitPolicy)
    parent_capabilities: Optional[Capabilities] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        clean_id = validate_task_id(self.task_id)
        object.__setattr__(self, "task_id", clean_id)

        clean_type = parse_task_type(self.task_type)
        object.__setattr__(self, "task_type", clean_type)

        if not isinstance(self.required_capabilities, Capabilities):
            raise TaskRoutingValidationError(
                f"required_capabilities must be a Capabilities instance, got {type(self.required_capabilities)}"
            )

        clean_op = canonicalize_operation(self.operation)
        object.__setattr__(self, "operation", clean_op)

        if not isinstance(self.risk_class, RiskClass):
            if type(self.risk_class) is str:
                try:
                    object.__setattr__(self, "risk_class", RiskClass(self.risk_class.strip().upper()))
                except ValueError:
                    raise TaskRoutingValidationError(f"Invalid risk class: '{self.risk_class}'")
            else:
                raise TaskRoutingValidationError(f"risk_class must be a RiskClass, got {type(self.risk_class)}")

        if not isinstance(self.target_scope, (tuple, list)):
            raise TaskRoutingValidationError("target_scope must be a sequence of paths")
        if len(self.target_scope) > MAX_SCOPE_TARGETS:
            raise TaskRoutingValidationError(f"target_scope count exceeds limit {MAX_SCOPE_TARGETS}")
        clean_targets = tuple(normalize_target_path(p) for p in self.target_scope)
        object.__setattr__(self, "target_scope", clean_targets)

        if not isinstance(self.network_requirements, (tuple, list)):
            raise TaskRoutingValidationError("network_requirements must be a sequence of strings")
        if len(self.network_requirements) > MAX_NETWORK_ENDPOINTS:
            raise TaskRoutingValidationError(f"network_requirements count exceeds limit {MAX_NETWORK_ENDPOINTS}")
        clean_net = tuple(normalize_endpoint(ep)[1] for ep in self.network_requirements)
        object.__setattr__(self, "network_requirements", clean_net)

        if not isinstance(self.filesystem_requirements, (tuple, list)):
            raise TaskRoutingValidationError("filesystem_requirements must be a sequence of paths")
        if len(self.filesystem_requirements) > MAX_SCOPE_TARGETS:
            raise TaskRoutingValidationError(f"filesystem_requirements count exceeds limit {MAX_SCOPE_TARGETS}")
        clean_fs = tuple(normalize_target_path(p) for p in self.filesystem_requirements)
        object.__setattr__(self, "filesystem_requirements", clean_fs)

        if not isinstance(self.git_requirements, GitPolicy):
            raise TaskRoutingValidationError(
                f"git_requirements must be a GitPolicy instance, got {type(self.git_requirements)}"
            )

        if self.parent_capabilities is not None and not isinstance(self.parent_capabilities, Capabilities):
            raise TaskRoutingValidationError(
                f"parent_capabilities must be a Capabilities instance or None, got {type(self.parent_capabilities)}"
            )

        if not isinstance(self.metadata, dict):
            raise TaskRoutingValidationError(f"metadata must be a dictionary, got {type(self.metadata)}")
        reject_secrets(self.metadata)

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "task_id": self.task_id,
            "task_type": self.task_type.value,
            "required_capabilities": self.required_capabilities.to_dict(),
            "operation": self.operation,
            "risk_class": self.risk_class.value,
            "target_scope": list(self.target_scope),
            "network_requirements": list(self.network_requirements),
            "filesystem_requirements": list(self.filesystem_requirements),
            "git_requirements": self.git_requirements.to_dict(),
            "metadata": dict(self.metadata),
        }
        if self.parent_capabilities is not None:
            data["parent_capabilities"] = self.parent_capabilities.to_dict()
        reject_secrets(data)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TaskRoutingRequest:
        if not isinstance(data, dict):
            raise TaskRoutingValidationError(f"Expected dict for TaskRoutingRequest.from_dict, got {type(data)}")
        reject_secrets(data)

        req_caps = Capabilities.from_dict(data.get("required_capabilities", {}))
        git_req = GitPolicy.from_dict(data.get("git_requirements", {}))
        risk_str = str(data.get("risk_class", RiskClass.LOW.value))
        risk_enum = RiskClass(risk_str.upper())
        task_type = parse_task_type(str(data.get("task_type", "")))

        parent_caps = (
            Capabilities.from_dict(data["parent_capabilities"])
            if "parent_capabilities" in data and isinstance(data["parent_capabilities"], dict)
            else None
        )

        return cls(
            task_id=str(data.get("task_id", "")),
            task_type=task_type,
            required_capabilities=req_caps,
            operation=str(data.get("operation", "")),
            risk_class=risk_enum,
            target_scope=tuple(str(p) for p in data.get("target_scope", ())),
            network_requirements=tuple(str(ep) for ep in data.get("network_requirements", ())),
            filesystem_requirements=tuple(str(p) for p in data.get("filesystem_requirements", ())),
            git_requirements=git_req,
            parent_capabilities=parent_caps,
            metadata=dict(data.get("metadata") or {}),
        )


# =============================================================================
# SubagentRoutingResult Domain Model
# =============================================================================

def compute_routing_digest(
    task_id: str,
    selected_profile_id: str,
    matched_capabilities: Capabilities,
    candidate_count: int,
) -> str:
    """Compute a reproducible cryptographic digest representing the deterministic routing decision."""
    payload = {
        "task_id": task_id,
        "selected_profile_id": selected_profile_id,
        "matched_capabilities": matched_capabilities.to_dict(),
        "candidate_count": candidate_count,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SubagentRoutingResult:
    """Immutable result of a deterministic subagent routing evaluation.

    Safe for Work metadata persistence; contains zero secrets or execution handles.
    """
    task_id: str
    selected_profile_id: str
    matched_capabilities: Capabilities
    routing_reason: str
    candidate_count: int
    routing_digest: str
    selected_profile: Optional[SubagentProfile] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    schema_version: int = CURRENT_SUBAGENT_ROUTING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        clean_task_id = validate_task_id(self.task_id)
        object.__setattr__(self, "task_id", clean_task_id)

        clean_prof_id = validate_profile_id(self.selected_profile_id)
        object.__setattr__(self, "selected_profile_id", clean_prof_id)

        if not isinstance(self.matched_capabilities, Capabilities):
            raise SubagentRoutingError(
                f"matched_capabilities must be a Capabilities instance, got {type(self.matched_capabilities)}"
            )

        if type(self.routing_reason) is not str:
            raise SubagentRoutingError("routing_reason must be a string")
        clean_reason = scrub_secrets(self.routing_reason.strip()[:MAX_REASON_CHARS])
        object.__setattr__(self, "routing_reason", clean_reason)

        if type(self.candidate_count) is not int or self.candidate_count < 1:
            raise SubagentRoutingError("candidate_count must be a positive integer")

        if type(self.routing_digest) is not str or not self.routing_digest.strip():
            raise SubagentRoutingError("routing_digest must be a non-empty string")

        if not isinstance(self.metadata, dict):
            raise SubagentRoutingError("metadata must be a dictionary")
        reject_secrets(self.metadata)

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "selected_profile_id": self.selected_profile_id,
            "matched_capabilities": self.matched_capabilities.to_dict(),
            "routing_reason": self.routing_reason,
            "candidate_count": self.candidate_count,
            "routing_digest": self.routing_digest,
            "metadata": dict(self.metadata),
        }
        reject_secrets(data)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SubagentRoutingResult:
        if not isinstance(data, dict):
            raise SubagentRoutingError(f"Expected dict for SubagentRoutingResult.from_dict, got {type(data)}")
        reject_secrets(data)

        matched_caps = Capabilities.from_dict(data.get("matched_capabilities", {}))
        return cls(
            task_id=str(data.get("task_id", "")),
            selected_profile_id=str(data.get("selected_profile_id", "")),
            matched_capabilities=matched_caps,
            routing_reason=str(data.get("routing_reason", "")),
            candidate_count=int(data.get("candidate_count", 1)),
            routing_digest=str(data.get("routing_digest", "")),
            metadata=dict(data.get("metadata") or {}),
            schema_version=int(data.get("schema_version", CURRENT_SUBAGENT_ROUTING_SCHEMA_VERSION)),
        )


# =============================================================================
# Eligibility Evaluation Logic
# =============================================================================

def is_profile_eligible(profile: SubagentProfile, request: TaskRoutingRequest) -> Tuple[bool, str]:
    """Check if a SubagentProfile satisfies all requirements of a TaskRoutingRequest.

    Rules:
    1. Task type: request.task_type in profile.supported_task_types
    2. Operation: request.operation in profile.supported_operations
    3. Risk tier: task_risk <= profile.max_risk_class
    4. Capabilities: request.required_capabilities ⊆ profile.required_capabilities
    5. Filesystem scope: task targets ⊆ profile filesystem scope
    6. Network scope: task network requirements ⊆ profile network scope
    7. Git policy: task git requirements ⊆ profile git policy
    8. Parent authority bound: if parent_capabilities provided, profile capabilities ⊆ parent_capabilities

    Returns (is_eligible, reason_string).
    """
    # 1. Task type compatibility
    if request.task_type not in profile.supported_task_types:
        return False, f"Task type '{request.task_type.value}' not supported by profile '{profile.profile_id}'"

    # 2. Operation compatibility
    canon_op = canonicalize_operation(request.operation)
    if canon_op not in profile.supported_operations:
        return False, f"Operation '{canon_op}' not supported by profile '{profile.profile_id}'"

    # 3. Risk compatibility
    req_risk_val = _RISK_ORDER.get(request.risk_class, 99)
    prof_risk_val = _RISK_ORDER.get(profile.max_risk_class, 0)
    if req_risk_val > prof_risk_val:
        return (
            False,
            f"Task risk '{request.risk_class.value}' exceeds profile max risk '{profile.max_risk_class.value}'",
        )

    # 4. Capabilities subset check (required ⊆ profile)
    req_caps = request.required_capabilities
    prof_caps = profile.required_capabilities

    # Shell execution
    if req_caps.shell.execute and not prof_caps.shell.execute:
        return False, f"Profile '{profile.profile_id}' does not permit shell execution"

    # Network access
    if req_caps.network.access and not prof_caps.network.access:
        return False, f"Profile '{profile.profile_id}' does not permit network access"

    # Git permissions
    if req_caps.git.read and not (prof_caps.git.read or profile.git_policy.read):
        return False, f"Profile '{profile.profile_id}' lacks Git read capability"
    if req_caps.git.commit and not (prof_caps.git.commit or profile.git_policy.commit):
        return False, f"Profile '{profile.profile_id}' lacks Git commit capability"
    if req_caps.git.push and not (prof_caps.git.push or profile.git_policy.push):
        return False, f"Profile '{profile.profile_id}' lacks Git push capability"

    if request.git_requirements.read and not (prof_caps.git.read or profile.git_policy.read):
        return False, f"Profile '{profile.profile_id}' does not satisfy Git read requirement"
    if request.git_requirements.commit and not (prof_caps.git.commit or profile.git_policy.commit):
        return False, f"Profile '{profile.profile_id}' does not satisfy Git commit requirement"
    if request.git_requirements.push and not (prof_caps.git.push or profile.git_policy.push):
        return False, f"Profile '{profile.profile_id}' does not satisfy Git push requirement"

    # Filesystem read capability containment (required ⊆ profile)
    available_read_caps = set(prof_caps.filesystem.read)
    for r in req_caps.filesystem.read:
        if not any(is_path_subset(r, pr) for pr in available_read_caps):
            return False, f"Filesystem read target '{r}' not covered by profile scopes"

    # Filesystem write capability containment (required ⊆ profile)
    available_write_caps = set(prof_caps.filesystem.write)
    for w in req_caps.filesystem.write:
        if not any(is_path_subset(w, pw) for pw in available_write_caps):
            return False, f"Filesystem write target '{w}' not covered by profile scopes"

    # 5. Filesystem target scope containment (task_scope ⊆ profile_filesystem_scope)
    all_task_targets = set(request.target_scope) | set(request.filesystem_requirements)
    if all_task_targets:
        if not profile.filesystem_scope:
            return False, f"Profile '{profile.profile_id}' has no filesystem scope for requested targets"
        for target in all_task_targets:
            if not any(is_path_subset(target, s) for s in profile.filesystem_scope):
                return False, f"Task target '{target}' outside profile scope {list(profile.filesystem_scope)}"

        # If task performs mutating operation, targets must be writable
        is_mutating = canon_op in ("write_code", "write_files") or bool(req_caps.filesystem.write)
        if is_mutating:
            for target in all_task_targets:
                if not any(is_path_subset(target, pw) for pw in available_write_caps):
                    return False, f"Filesystem write target '{target}' not covered by profile scopes"
        else:
            available_read_or_write = available_read_caps | available_write_caps
            for target in all_task_targets:
                if not any(is_path_subset(target, pr) for pr in available_read_or_write):
                    return False, f"Filesystem read target '{target}' not covered by profile scopes"

    # 6. Network scope containment
    if request.network_requirements or req_caps.network.access:
        available_net = set(profile.network_scope)
        if prof_caps.network.access and prof_caps.network.scope:
            available_net.add(prof_caps.network.scope)
        for ep in request.network_requirements:
            if not any(is_network_subset(ep, p_ep) for p_ep in available_net):
                return False, f"Network requirement '{ep}' outside profile network scope {list(available_net)}"

    # 7. Parent capability upper bound (if parent_capabilities provided)
    if request.parent_capabilities is not None:
        try:
            # Profile's required capabilities must not exceed parent capabilities
            attenuate_capabilities(request.parent_capabilities, profile.required_capabilities)
        except DelegationAttenuationError as exc:
            return False, f"Profile '{profile.profile_id}' exceeds parent authority: {exc}"

    return True, "Eligible"


# =============================================================================
# Deterministic Ranking
# =============================================================================

def compute_profile_rank_key(
    profile: SubagentProfile,
    request: TaskRoutingRequest,
) -> Tuple[int, int, int, int, str]:
    """Compute a deterministic, cross-process stable sorting key for an eligible profile.

    Ranking Criteria (lower tuple value is ranked higher):
    1. Exact task-type match (primary task type index in supported_task_types).
    2. Smallest sufficient capability surface (principle of least privilege).
    3. Smallest sufficient scope surface (narrower scopes preferred).
    4. Lowest allowed risk tier (lower risk preferred).
    5. Stable profile_id tie-breaker (lexicographical).
    """
    # 1. Task type match index
    task_type_rank = (
        profile.supported_task_types.index(request.task_type)
        if request.task_type in profile.supported_task_types
        else 99
    )

    # 2. Capability surface score
    cap = profile.required_capabilities
    cap_surface = (
        (100 if cap.shell.execute else 0)
        + (100 if profile.git_policy.push else 0)
        + (50 if profile.git_policy.commit else 0)
        + (10 if profile.git_policy.read else 0)
        + (50 if cap.network.access else 0)
        + len(cap.filesystem.write) * 10
        + len(cap.filesystem.read) * 2
        + len(profile.supported_operations)
    )

    # 3. Scope surface score
    scope_surface = 0
    if "**" in profile.filesystem_scope:
        scope_surface += 1000
    else:
        scope_surface += len(profile.filesystem_scope) * 10
        for p in profile.filesystem_scope:
            scope_surface += len(p)
    scope_surface += len(profile.network_scope) * 5

    # 4. Risk score
    risk_score = _RISK_ORDER.get(profile.max_risk_class, 99)

    # 5. Stable profile ID tie-breaker
    return (task_type_rank, cap_surface, scope_surface, risk_score, profile.profile_id)


# =============================================================================
# SubagentProfileRegistry
# =============================================================================

class SubagentProfileRegistry:
    """Bounded, thread-safe in-memory registry of SubagentProfile definitions.

    Responsibilities:
    - Register and validate profiles during initialization.
    - Retrieve by profile ID.
    - Enforce unique profile IDs and global capacity limits.
    - Resolve eligible candidate profiles deterministically.
    """

    def __init__(self, profiles: Optional[Sequence[SubagentProfile]] = None) -> None:
        self._profiles: Dict[str, SubagentProfile] = {}
        if profiles:
            for p in profiles:
                self.register(p)

    def register(self, profile: SubagentProfile) -> None:
        if not isinstance(profile, SubagentProfile):
            raise ProfileValidationError(f"Expected SubagentProfile instance, got {type(profile)}")
        if len(self._profiles) >= MAX_PROFILES:
            raise ProfileRegistryLimitError(
                f"Profile registry reached maximum capacity limit of {MAX_PROFILES} profiles"
            )
        if profile.profile_id in self._profiles:
            raise DuplicateProfileError(
                f"Profile with ID '{profile.profile_id}' is already registered in registry"
            )
        self._profiles[profile.profile_id] = profile

    def get(self, profile_id: str) -> Optional[SubagentProfile]:
        return self._profiles.get(profile_id)

    def list_profiles(self) -> Tuple[SubagentProfile, ...]:
        """Return all registered profiles sorted deterministically by profile_id."""
        return tuple(self._profiles[pid] for pid in sorted(self._profiles.keys()))

    def resolve_eligible(self, request: TaskRoutingRequest) -> Tuple[SubagentProfile, ...]:
        """Return all profiles satisfying the task request, sorted deterministically by rank."""
        if not isinstance(request, TaskRoutingRequest):
            raise TaskRoutingValidationError(f"Expected TaskRoutingRequest, got {type(request)}")

        candidates: List[SubagentProfile] = []
        for pid in sorted(self._profiles.keys()):
            prof = self._profiles[pid]
            eligible, _ = is_profile_eligible(prof, request)
            if eligible:
                candidates.append(prof)

        # Deterministically sort candidates
        candidates.sort(key=lambda p: compute_profile_rank_key(p, request))
        return tuple(candidates)

    def __len__(self) -> int:
        return len(self._profiles)


def create_default_registry() -> SubagentProfileRegistry:
    """Instantiate the standard profile registry with canonical BrainFrog profiles."""
    profiles = [
        SubagentProfile(
            profile_id="researcher",
            name="Codebase Researcher",
            description="Read-only codebase search, inspection, diagnosis, and planning across workspace",
            required_capabilities=Capabilities(
                filesystem=FilesystemPolicy(read=("src/", "tests/", "docs/", "ui/"), write=()),
                network=NetworkPolicy(access=False),
                git=GitPolicy(read=True, commit=False, push=False),
            ),
            supported_operations=("read_code", "diagnose", "plan"),
            supported_task_types=(TaskType.RESEARCH, TaskType.REVIEW),
            max_risk_class=RiskClass.LOW,
            filesystem_scope=("src/**", "tests/**", "docs/**", "ui/**"),
        ),
        SubagentProfile(
            profile_id="backend_worker",
            name="Backend Implementation Worker",
            description="Backend logic, API handlers, algorithms, and core architecture implementation",
            required_capabilities=Capabilities(
                filesystem=FilesystemPolicy(
                    read=("src/", "tests/", "docs/"),
                    write=("src/",),
                ),
                network=NetworkPolicy(access=False),
                git=GitPolicy(read=True, commit=False, push=False),
            ),
            supported_operations=("write_code", "write_files", "read_code", "diagnose"),
            supported_task_types=(TaskType.IMPLEMENTATION, TaskType.REFACTOR),
            max_risk_class=RiskClass.MEDIUM,
            filesystem_scope=("src/**",),
        ),
        SubagentProfile(
            profile_id="frontend_worker",
            name="Frontend Implementation Worker",
            description="User interface components, templates, client styling, and frontend assets",
            required_capabilities=Capabilities(
                filesystem=FilesystemPolicy(
                    read=("src/", "ui/"),
                    write=("ui/",),
                ),
                network=NetworkPolicy(access=False),
                git=GitPolicy(read=True, commit=False, push=False),
            ),
            supported_operations=("write_code", "write_files", "read_code"),
            supported_task_types=(TaskType.IMPLEMENTATION, TaskType.REFACTOR),
            max_risk_class=RiskClass.MEDIUM,
            filesystem_scope=("ui/**",),
        ),
        SubagentProfile(
            profile_id="test_runner",
            name="Test and Verification Worker",
            description="Test suite authoring, test execution, regression verification, and test fixtures",
            required_capabilities=Capabilities(
                filesystem=FilesystemPolicy(
                    read=("src/", "tests/"),
                    write=("tests/",),
                ),
                network=NetworkPolicy(access=False),
                git=GitPolicy(read=True, commit=False, push=False),
            ),
            supported_operations=("run_tests", "read_code", "write_code", "write_files"),
            supported_task_types=(TaskType.TESTING, TaskType.VERIFICATION),
            max_risk_class=RiskClass.MEDIUM,
            filesystem_scope=("tests/**",),
        ),
        SubagentProfile(
            profile_id="reviewer",
            name="Code and Architecture Reviewer",
            description="Read-only architecture review, verification, safety checks, and diff analysis",
            required_capabilities=Capabilities(
                filesystem=FilesystemPolicy(read=("src/", "tests/", "docs/", "ui/"), write=()),
                network=NetworkPolicy(access=False),
                git=GitPolicy(read=True, commit=False, push=False),
            ),
            supported_operations=("read_code", "diagnose"),
            supported_task_types=(TaskType.REVIEW, TaskType.VERIFICATION),
            max_risk_class=RiskClass.LOW,
            filesystem_scope=("src/**", "tests/**", "docs/**", "ui/**"),
        ),
        SubagentProfile(
            profile_id="documentation_worker",
            name="Documentation Worker",
            description="Technical documentation authoring, markdown guides, and specifications",
            required_capabilities=Capabilities(
                filesystem=FilesystemPolicy(
                    read=("src/", "docs/"),
                    write=("docs/",),
                ),
                network=NetworkPolicy(access=False),
                git=GitPolicy(read=True, commit=False, push=False),
            ),
            supported_operations=("write_code", "write_files", "read_code"),
            supported_task_types=(TaskType.DOCUMENTATION,),
            max_risk_class=RiskClass.LOW,
            filesystem_scope=("docs/**",),
        ),
    ]
    return SubagentProfileRegistry(profiles)


# =============================================================================
# Deterministic SubagentRouter
# =============================================================================

class SubagentRouter:
    """Deterministic router mapping TaskRoutingRequest to eligible SubagentProfile.

    Architectural Invariants:
    - Pure selection function: TaskRequirements -> SubagentProfile.
    - Zero execution authority.
    - Never calls LLMs or interprets natural-language strings directly.
    - Never mutates Work records or filesystem state.
    - Fails closed with NoEligibleSubagentError when no candidate satisfies requirements.
    - Deterministic ranking ensures identical requests over identical registries yield identical results.
    """

    def __init__(self, registry: Optional[SubagentProfileRegistry] = None) -> None:
        self.registry = registry if registry is not None else create_default_registry()

    def route(
        self,
        request: TaskRoutingRequest,
        parent_capabilities: Optional[Capabilities] = None,
    ) -> SubagentRoutingResult:
        """Deterministically route a task request to the best matching SubagentProfile.

        Raises NoEligibleSubagentError if no registered profile satisfies the request.
        """
        if not isinstance(request, TaskRoutingRequest):
            raise TaskRoutingValidationError(f"Expected TaskRoutingRequest instance, got {type(request)}")

        # Override or set parent capabilities if passed
        effective_request = request
        if parent_capabilities is not None and request.parent_capabilities is None:
            effective_request = TaskRoutingRequest(
                task_id=request.task_id,
                task_type=request.task_type,
                required_capabilities=request.required_capabilities,
                operation=request.operation,
                risk_class=request.risk_class,
                target_scope=request.target_scope,
                network_requirements=request.network_requirements,
                filesystem_requirements=request.filesystem_requirements,
                git_requirements=request.git_requirements,
                parent_capabilities=parent_capabilities,
                metadata=request.metadata,
            )

        eligible = self.registry.resolve_eligible(effective_request)
        if not eligible:
            raise NoEligibleSubagentError(
                f"No eligible subagent profile found for task '{request.task_id}' "
                f"(type={request.task_type.value}, op={request.operation}, risk={request.risk_class.value})"
            )

        selected = eligible[0]

        # Matched capabilities represents the capabilities granted to this task
        # Must be bounded by: Profile Capabilities ∩ Task Capabilities
        matched_caps = request.required_capabilities

        reason = (
            f"Selected profile '{selected.profile_id}' ({selected.name}) for task '{request.task_id}' "
            f"[type={request.task_type.value}, op={request.operation}, risk={request.risk_class.value}] "
            f"from {len(eligible)} eligible profile(s)."
        )

        digest = compute_routing_digest(
            request.task_id,
            selected.profile_id,
            matched_caps,
            len(eligible),
        )

        return SubagentRoutingResult(
            task_id=request.task_id,
            selected_profile_id=selected.profile_id,
            matched_capabilities=matched_caps,
            routing_reason=reason,
            candidate_count=len(eligible),
            routing_digest=digest,
            selected_profile=selected,
        )


# =============================================================================
# Delegation Integration Helpers
# =============================================================================

def create_routed_delegation(
    parent_capabilities: Capabilities,
    profile: SubagentProfile,
    request: TaskRoutingRequest,
    *,
    delegation_id: str,
    parent_work_id: str,
    child_subagent_id: str,
    actor: str,
    session_id: str,
    session_incarnation_id: str,
    expires_at: float,
    created_at: Optional[float] = None,
) -> DelegationContract:
    """Create an attenuated DelegationContract strictly bounded by:
    Parent Capabilities ∩ Profile Capabilities ∩ Task Requirements.

    Guarantees:
    - Child capabilities never exceed parent capabilities.
    - Child capabilities never exceed profile capabilities.
    - Attenuation is computed deterministically via canonical attenuate_capabilities.
    - DelegationContract authority boundary is strictly enforced.
    """
    if not isinstance(parent_capabilities, Capabilities):
        raise TypeError(f"parent_capabilities must be a Capabilities instance, got {type(parent_capabilities)}")
    if not isinstance(profile, SubagentProfile):
        raise TypeError(f"profile must be a SubagentProfile instance, got {type(profile)}")
    if not isinstance(request, TaskRoutingRequest):
        raise TypeError(f"request must be a TaskRoutingRequest instance, got {type(request)}")

    # 1. Attenuate profile capabilities from parent capabilities
    profile_attenuated = attenuate_capabilities(
        parent_capabilities,
        profile.required_capabilities,
    )

    # 2. Attenuate requested task capabilities from profile_attenuated
    final_capabilities = attenuate_capabilities(
        profile_attenuated,
        request.required_capabilities,
    )

    # 3. Determine target scope bounded by parent and profile (SEC-P1.3-03)
    parent_fs_scopes = tuple(set(parent_capabilities.filesystem.read) | set(parent_capabilities.filesystem.write))
    profile_fs_scopes = tuple(profile.filesystem_scope)

    all_targets = set(request.target_scope) | set(request.filesystem_requirements)
    if not all_targets:
        # Fall back to write targets if available, else read targets
        all_targets = set(final_capabilities.filesystem.write or final_capabilities.filesystem.read)

    norm_targets = []
    for t in all_targets:
        norm_t = normalize_target_path(t)
        # 1. Child target scope ⊆ parent filesystem authority
        if not parent_fs_scopes or not any(is_path_subset(norm_t, ps) for ps in parent_fs_scopes):
            raise DelegationAttenuationError(
                f"Requested target scope '{t}' exceeds parent filesystem scope {list(parent_fs_scopes)}"
            )
        # 2. Child target scope ⊆ profile allowed filesystem scope
        if not profile_fs_scopes or not any(is_path_subset(norm_t, prof_s) for prof_s in profile_fs_scopes):
            raise DelegationAttenuationError(
                f"Requested target scope '{t}' exceeds profile filesystem scope {list(profile_fs_scopes)}"
            )
        norm_targets.append(norm_t)

    target_scope = tuple(sorted(set(norm_targets)))
    now = time.time() if created_at is None else float(created_at)

    return DelegationContract(
        delegation_id=delegation_id,
        parent_work_id=parent_work_id,
        child_subagent_id=child_subagent_id,
        actor=actor,
        session_id=session_id,
        session_incarnation_id=session_incarnation_id,
        capabilities=final_capabilities,
        target_scope=target_scope,
        expires_at=expires_at,
        created_at=now,
    )


def create_routed_child_work(
    parent_work: Work,
    profile: SubagentProfile,
    request: TaskRoutingRequest,
    *,
    child_work_id: str,
    subagent_id: Optional[str] = None,
    delegation_id: Optional[str] = None,
    expires_in_seconds: float = 600.0,
    created_at: Optional[float] = None,
) -> ChildWork:
    """Factory creating a canonical Subagent, DelegationContract, and ChildWork
    from a routed SubagentProfile and TaskRoutingRequest.
    """
    if not isinstance(parent_work, Work):
        raise TypeError(f"parent_work must be a Work instance, got {type(parent_work)}")
    if not isinstance(profile, SubagentProfile):
        raise TypeError(f"profile must be a SubagentProfile instance, got {type(profile)}")
    if not isinstance(request, TaskRoutingRequest):
        raise TypeError(f"request must be a TaskRoutingRequest instance, got {type(request)}")

    now = time.time() if created_at is None else float(created_at)
    sub_id = subagent_id or f"sub_{profile.profile_id}_{secrets.token_hex(4)}"
    delg_id = delegation_id or f"delg_{profile.profile_id}_{secrets.token_hex(4)}"

    # Create worker identity
    subagent = Subagent(
        subagent_id=sub_id,
        parent_work_id=parent_work.id,
        session_id=parent_work.session_id or "default_session",
        session_incarnation_id=parent_work.session_incarnation_id or "default_inc",
        actor=parent_work.actor_id or "default_actor",
        role=profile.name,
        purpose=profile.description,
        created_at=now,
        updated_at=now,
    )

    parent_caps = parent_work.capabilities or Capabilities()

    delegation = create_routed_delegation(
        parent_capabilities=parent_caps,
        profile=profile,
        request=request,
        delegation_id=delg_id,
        parent_work_id=parent_work.id,
        child_subagent_id=subagent.subagent_id,
        actor=subagent.actor,
        session_id=subagent.session_id,
        session_incarnation_id=subagent.session_incarnation_id,
        expires_at=now + expires_in_seconds,
        created_at=now,
    )

    return ChildWork.create(
        parent_work=parent_work,
        subagent=subagent,
        delegation=delegation,
        child_work_id=child_work_id,
        created_at=now,
    )
