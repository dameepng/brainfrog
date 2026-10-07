"""Delegation Contract Domain Model and Deterministic Scope Attenuation.

BrainFrog P1.3B — Delegation Contract.

Architectural Invariants:
- A child authority MUST NEVER exceed parent authority (monotonic attenuation).
- A DelegationContract is a declarative boundary of allowed scope, NOT an execution engine.
- It holds NO execution authority, NO filesystem mutation capabilities, and NO subprocess primitives.
- It cannot approve operations or bypass ApprovedExecutionContract.
- The canonical execution path remains strictly:
    Approval -> ApprovedExecutionContract -> Transaction -> execution engine
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple, Union

from core.runtime.capabilities import Capabilities, FilesystemPolicy, GitPolicy, NetworkPolicy, ShellPolicy
from core.runtime.contract import ApprovedExecutionContract, reject_secrets
from core.runtime.secret_scrubbing import scrub_secrets

CURRENT_DELEGATION_SCHEMA_VERSION = 1
DEFAULT_DELEGATION_TTL_SECONDS = 300.0  # 5 minutes
MAX_DELEGATION_TTL_SECONDS = 3600.0  # 1 hour maximum bound

MAX_DELEGATION_ID_CHARS = 128
MAX_DELEGATION_ROLE_CHARS = 256
MAX_DELEGATION_PURPOSE_CHARS = 4096

_INVALID_ID_CHARS = re.compile(r"[\x00-\x1f\x7f/\\:*\?\"<>\|]|(?:\.\.)")
_ILLEGAL_PATH_CHARS = re.compile(r"[\x00-\x1f\x7f\"<>|]")



# Canonical set of recognized operation types (fail-closed against unknown operations)
KNOWN_OPERATIONS: FrozenSet[str] = frozenset({
    "read_file",
    "read_files",
    "read_code",
    "write_file",
    "write_files",
    "write_code",
    "edit_file",
    "delete_file",
    "run_shell_command",
    "shell_execution",
    "deploy_project",
    "deployment",
    "git_read",
    "git_commit",
    "git_push",
})


class DelegationError(ValueError):
    """Base exception for delegation domain errors."""
    pass


class DelegationAttenuationError(DelegationError):
    """Raised when requested child authority exceeds parent authority or fails closed."""
    pass


class DelegationIntegrityError(DelegationError):
    """Raised when delegation contract digest verification or tampering detection fails."""
    pass


class DelegationExpiredError(DelegationError, PermissionError):
    """Raised when an operation is attempted on an expired delegation contract."""
    pass


class DelegationValidationError(DelegationError):
    """Raised when delegation contract parameters or identity bindings are malformed."""
    pass


def validate_delegation_id(delg_id: str) -> str:
    """Validate delegation ID to prevent traversal, drive letters, and malformed identifiers."""
    if type(delg_id) is not str:
        raise DelegationValidationError(f"Delegation ID must be a string, got {type(delg_id)}")
    clean = delg_id.strip()
    if not clean or len(clean) > MAX_DELEGATION_ID_CHARS:
        raise DelegationValidationError(
            f"Delegation ID must be a non-empty string with length <= {MAX_DELEGATION_ID_CHARS}"
        )
    if _INVALID_ID_CHARS.search(clean) or clean.startswith((".", "~", "/", "\\")):
        raise DelegationValidationError(
            f"Invalid characters or traversal detected in Delegation ID: '{clean}'"
        )
    return clean


_INVALID_SESSION_ID_CHARS = re.compile(r"[\x00-\x1f\x7f/\\*\?\"<>\|]|(?:\.\.)")


def _validate_binding(value: Any, field_name: str) -> str:
    """Validate mandatory binding identifiers (parent_work_id, child_subagent_id, actor, etc.)."""
    if type(value) is not str:
        raise DelegationValidationError(f"{field_name} must be a string, got {type(value)}")
    clean = value.strip()
    if not clean:
        raise DelegationValidationError(f"{field_name} must be a non-empty string")
    regex = _INVALID_SESSION_ID_CHARS if field_name == "session_id" else _INVALID_ID_CHARS
    if regex.search(clean) or clean.startswith((".", "~", "/", "\\")):
        raise DelegationValidationError(
            f"Invalid characters or traversal detected in {field_name}: '{clean}'"
        )
    return clean


def normalize_target_path(path: str) -> str:
    """Clean and validate workspace-relative target or glob pattern."""
    if type(path) is not str:
        raise DelegationValidationError(f"Path target must be a string, got {type(path)}")
    clean = path.replace("\\", "/").strip()
    if not clean:
        raise DelegationValidationError("Target path must be a non-empty string")

    # Reject traversal, absolute paths, drive letters, UNC, and control chars
    if (
        clean.startswith(("/", "~", "."))
        or clean.startswith("//")
        or ":" in clean
        or _ILLEGAL_PATH_CHARS.search(clean)
    ):
        raise DelegationAttenuationError(f"Target path escapes workspace or contains illegal characters: '{path}'")

    # Normalize repeated separators e.g. src///auth -> src/auth
    clean = re.sub(r"/+", "/", clean)

    # Check segments for traversal and dot components
    segments = clean.split("/")
    if any(seg in ("..", ".") for seg in segments):
        raise DelegationAttenuationError(f"Path traversal or dot segment detected in target path: '{path}'")

    # Disallow targeting critical authorization and version control roots
    root_segment = segments[0].lower()
    if root_segment in (".git", ".brainfrog", ".codex", ".agents", ".aws"):
        raise DelegationAttenuationError(f"Target cannot touch internal system state: '{clean}'")

    return clean


def is_path_subset(child_path: str, parent_path: str) -> bool:
    """Determine if child_path is strictly contained within parent_path.

    Enforces mathematically monotonic containment (A ⊆ B and B ⊆ C => A ⊆ C):
    - child_path == parent_path -> True
    - parent '**' matches any valid workspace path
    - child '**' when parent != '**' -> False
    - parent 'dir/**' matches any path or glob within dir/
    - child 'dir/**' when parent is not '**' or 'dir/**' -> False
    - parent 'dir/*' matches direct children within dir/ only (never subdirectories or recursive globs)
    - parent explicit directory 'dir/' matches direct children, subdirectories, and descendant files (never recursive '**')
    - exact path (e.g. 'src/main.py', 'Makefile', 'src/auth') only matches exact same path
    - exact path cannot authorize sibling, descendant paths, or wildcards
    - descendant path cannot grant parent path (descendant -> parent is False)
    - sibling prefix confusion (e.g. src vs src_evil) -> False
    - traversal or illegal paths -> raises DelegationAttenuationError
    """
    c = normalize_target_path(child_path)
    p = normalize_target_path(parent_path)

    if c == p:
        return True

    # Full workspace glob '**'
    if p == "**":
        return True

    # Child requesting full workspace when parent is restricted
    if c == "**":
        return False

    # Parent recursive subtree 'dir/**'
    if p.endswith("/**"):
        p_prefix = p[:-3].rstrip("/")
        if not p_prefix:
            return True
        if c == p_prefix or c.startswith(p_prefix + "/"):
            return True
        return False

    # Child requesting recursive glob 'dir/**' when parent is NOT recursive glob
    if c.endswith("/**"):
        return False

    # Parent direct children glob 'dir/*'
    if p.endswith("/*"):
        p_prefix = p[:-2].rstrip("/")
        if c.startswith(p_prefix + "/"):
            sub = c[len(p_prefix) + 1:]
            if not sub or "*" in sub or "/" in sub:
                return False
            return True
        return False

    # Parent explicit directory scope 'dir/'
    if p.endswith("/"):
        p_prefix = p.rstrip("/")
        if c.endswith("/*"):
            c_prefix = c[:-2].rstrip("/")
            return c_prefix == p_prefix or c_prefix.startswith(p_prefix + "/")
        if c.endswith("/"):
            c_clean = c.rstrip("/")
            return c_clean.startswith(p_prefix + "/")
        if c.startswith(p_prefix + "/"):
            sub = c[len(p_prefix) + 1:]
            return "*" not in sub
        return False

    # Parent is an exact path (e.g. 'src/main.py', 'Makefile', 'Dockerfile', 'src/auth')
    # Exact path authorizes ONLY the exact same path (already evaluated by c == p).
    return False


def _parse_endpoint(endpoint: str) -> Tuple[Optional[str], str, Optional[int]]:
    """Parse endpoint into (scheme, host, port)."""
    if type(endpoint) is not str:
        raise DelegationValidationError(f"Network endpoint must be a string, got {type(endpoint)}")
    clean = endpoint.strip().lower()
    if not clean:
        raise DelegationValidationError("Network endpoint cannot be empty")

    if "@" in clean:
        raise DelegationAttenuationError("Network endpoint cannot contain userinfo/credentials")

    if any(c in clean for c in ('\0', '\r', '\n', '<', '>', '"', '|', ' ', '\t', '\\')):
        raise DelegationAttenuationError("Illegal characters in network endpoint")

    if clean in ("*", "configured_model_api"):
        return None, clean, None

    scheme = None
    rest = clean
    if clean.startswith("https://"):
        scheme = "https"
        rest = clean[8:]
    elif clean.startswith("http://"):
        scheme = "http"
        rest = clean[7:]
    elif "://" in clean:
        scheme, _, rest = clean.partition("://")
    else:
        # Bare host defaults canonically to https
        scheme = "https"

    # Reject or strip URI components outside capability model (fragment, query, path)
    # Fragment (#) and query (?) must be stripped, or must never become part of the hostname
    host_port = rest.partition("#")[0].partition("?")[0].rstrip("/").partition("/")[0]

    port: Optional[int] = None
    host = host_port
    if ":" in host_port:
        h, _, p_str = host_port.partition(":")
        host = h
        try:
            port = int(p_str)
            if port < 1 or port > 65535:
                raise DelegationValidationError(f"Port number out of range: {p_str}")
        except ValueError:
            raise DelegationValidationError(f"Invalid port in network endpoint: '{endpoint}'")
    else:
        port = 443 if scheme == "https" else (80 if scheme == "http" else None)

    if not host or any(c in host for c in ('/', '\\', '?', '#', '@', ':', ' ')):
        raise DelegationValidationError(f"Invalid host in network endpoint: '{endpoint}'")

    return scheme, host, port


def normalize_endpoint(endpoint: str) -> Tuple[Optional[str], str]:
    """Parse and normalize network endpoint into (scheme, host_and_port)."""
    scheme, host, port = _parse_endpoint(endpoint)
    if host in ("*", "configured_model_api"):
        return None, host
    host_out = f"{host}:{port}" if port is not None else host
    return scheme, host_out


def is_network_subset(child_endpoint: str, parent_endpoint: str) -> bool:
    """Determine if child_endpoint is strictly permitted by parent_endpoint."""
    c_raw = child_endpoint.strip().lower()
    p_raw = parent_endpoint.strip().lower()

    if not c_raw or not p_raw:
        return False

    if c_raw == p_raw:
        return True

    # Parent wildcard '*' permits any endpoint
    if p_raw == "*":
        return True

    # Child requesting '*' when parent is restricted is forbidden
    if c_raw == "*" and p_raw != "*":
        return False

    # Configured model API is a special closed scope
    if p_raw == "configured_model_api":
        return c_raw == "configured_model_api"
    if c_raw == "configured_model_api" and p_raw != "configured_model_api":
        return False

    c_scheme, c_host, c_port = _parse_endpoint(c_raw)
    p_scheme, p_host, p_port = _parse_endpoint(p_raw)

    # Scheme restrictions: child scheme must match parent scheme exactly
    if p_scheme and c_scheme != p_scheme:
        return False

    # Port restrictions: child port must match parent port exactly
    if p_port is not None and c_port != p_port:
        return False

    # Host matching:
    if p_host == c_host:
        return True

    # Child requesting wildcard host when parent has specific host -> DENY
    if c_host.startswith("*.") and not p_host.startswith("*."):
        return False

    # Wildcard subdomain matching (e.g. parent '*.example.com' allows 'api.example.com')
    if p_host.startswith("*."):
        parent_domain = p_host[2:]
        if c_host.endswith("." + parent_domain):
            if c_host.startswith("*."):
                child_domain = c_host[2:]
                return child_domain.endswith("." + parent_domain) or child_domain == parent_domain
            return True

    return False


def attenuate_filesystem_policy(
    parent_policy: FilesystemPolicy,
    requested_policy: Optional[FilesystemPolicy],
) -> FilesystemPolicy:
    """Attenuate child FilesystemPolicy from parent FilesystemPolicy."""
    if not isinstance(parent_policy, FilesystemPolicy):
        raise DelegationValidationError(f"parent_policy must be a FilesystemPolicy instance, got {type(parent_policy)}")
    if requested_policy is None:
        return parent_policy
    if not isinstance(requested_policy, FilesystemPolicy):
        raise DelegationValidationError(f"requested_policy must be a FilesystemPolicy instance, got {type(requested_policy)}")

    for r in requested_policy.read:
        if not parent_policy.read or not any(is_path_subset(r, pr) for pr in parent_policy.read):
            raise DelegationAttenuationError(
                f"Requested filesystem read scope '{r}' exceeds parent read scope {list(parent_policy.read)}"
            )

    for w in requested_policy.write:
        if not parent_policy.write or not any(is_path_subset(w, pw) for pw in parent_policy.write):
            raise DelegationAttenuationError(
                f"Requested filesystem write scope '{w}' exceeds parent write scope {list(parent_policy.write)}"
            )

    return FilesystemPolicy(read=requested_policy.read, write=requested_policy.write)


def attenuate_shell_policy(
    parent_policy: ShellPolicy,
    requested_policy: Optional[ShellPolicy],
) -> ShellPolicy:
    """Attenuate child ShellPolicy from parent ShellPolicy."""
    if not isinstance(parent_policy, ShellPolicy):
        raise DelegationValidationError(f"parent_policy must be a ShellPolicy instance, got {type(parent_policy)}")
    if requested_policy is None:
        return parent_policy
    if not isinstance(requested_policy, ShellPolicy):
        raise DelegationValidationError(f"requested_policy must be a ShellPolicy instance, got {type(requested_policy)}")

    if requested_policy.execute and not parent_policy.execute:
        raise DelegationAttenuationError("Child cannot gain shell execution authority when parent lacks it")

    return ShellPolicy(execute=requested_policy.execute)


def attenuate_network_policy(
    parent_policy: NetworkPolicy,
    requested_policy: Optional[NetworkPolicy],
) -> NetworkPolicy:
    """Attenuate child NetworkPolicy from parent NetworkPolicy."""
    if not isinstance(parent_policy, NetworkPolicy):
        raise DelegationValidationError(f"parent_policy must be a NetworkPolicy instance, got {type(parent_policy)}")
    if requested_policy is None:
        return parent_policy
    if not isinstance(requested_policy, NetworkPolicy):
        raise DelegationValidationError(f"requested_policy must be a NetworkPolicy instance, got {type(requested_policy)}")

    if requested_policy.access and not parent_policy.access:
        raise DelegationAttenuationError("Child cannot gain network access authority when parent lacks it")
    if requested_policy.scope != parent_policy.scope:
        raise DelegationAttenuationError(
            f"Requested network scope '{requested_policy.scope}' does not match parent scope '{parent_policy.scope}'"
        )

    return NetworkPolicy(access=requested_policy.access, scope=requested_policy.scope)


def attenuate_git_policy(
    parent_policy: GitPolicy,
    requested_policy: Optional[GitPolicy],
) -> GitPolicy:
    """Attenuate child GitPolicy from parent GitPolicy."""
    if not isinstance(parent_policy, GitPolicy):
        raise DelegationValidationError(f"parent_policy must be a GitPolicy instance, got {type(parent_policy)}")
    if requested_policy is None:
        return parent_policy
    if not isinstance(requested_policy, GitPolicy):
        raise DelegationValidationError(f"requested_policy must be a GitPolicy instance, got {type(requested_policy)}")

    if requested_policy.read and not parent_policy.read:
        raise DelegationAttenuationError("Child cannot gain Git read policy when parent lacks it")
    if requested_policy.commit and not parent_policy.commit:
        raise DelegationAttenuationError("Child cannot gain Git commit policy when parent lacks it")
    if requested_policy.push and not parent_policy.push:
        raise DelegationAttenuationError("Child cannot gain Git push policy when parent lacks it")

    return GitPolicy(read=requested_policy.read, commit=requested_policy.commit, push=requested_policy.push)


def attenuate_capabilities(
    parent_capabilities: Capabilities,
    requested_capabilities: Optional[Capabilities] = None,
) -> Capabilities:
    """Deterministically attenuate child Capabilities from parent Capabilities.

    Guarantees:
    - child.filesystem.read ⊆ parent.filesystem.read
    - child.filesystem.write ⊆ parent.filesystem.write
    - child.shell.execute <= parent.shell.execute
    - child.network.access <= parent.network.access
    - child.git.read <= parent.git.read
    - child.git.commit <= parent.git.commit
    - child.git.push <= parent.git.push
    - Parent instance is never mutated (immutability preserved).
    - Returns a new immutable Capabilities instance.
    - Fails closed on any expansion or unknown capability.
    """
    if not isinstance(parent_capabilities, Capabilities):
        raise DelegationValidationError(f"parent_capabilities must be a Capabilities instance, got {type(parent_capabilities)}")
    if requested_capabilities is None:
        return parent_capabilities
    if not isinstance(requested_capabilities, Capabilities):
        raise DelegationValidationError(f"requested_capabilities must be a Capabilities instance, got {type(requested_capabilities)}")

    fs = attenuate_filesystem_policy(parent_capabilities.filesystem, requested_capabilities.filesystem)
    shell = attenuate_shell_policy(parent_capabilities.shell, requested_capabilities.shell)
    net = attenuate_network_policy(parent_capabilities.network, requested_capabilities.network)
    git = attenuate_git_policy(parent_capabilities.git, requested_capabilities.git)

    return Capabilities(filesystem=fs, shell=shell, network=net, git=git)


def attenuate_target_scope(
    parent_targets: Sequence[str],
    requested_targets: Optional[Sequence[str]],
    repo_dir: Optional[Path] = None,
) -> Tuple[str, ...]:
    """Attenuate child target scope from parent target scope."""
    if type(parent_targets) not in (list, tuple, set, frozenset):
        raise DelegationValidationError("parent_targets must be a sequence of paths")
    p_targets = tuple(sorted({normalize_target_path(t) for t in parent_targets}))

    if requested_targets is None:
        return p_targets

    if type(requested_targets) not in (list, tuple, set, frozenset):
        raise DelegationValidationError("requested_targets must be a sequence of paths")

    child_targets_list: List[str] = []
    for t in requested_targets:
        norm_t = normalize_target_path(t)
        if not p_targets or not any(is_path_subset(norm_t, pt) for pt in p_targets):
            raise DelegationAttenuationError(
                f"Requested target scope '{t}' exceeds parent target scope {list(p_targets)}"
            )
        if repo_dir is not None:
            root = repo_dir.resolve()
            check_path = norm_t.rstrip("*").rstrip("/")
            if check_path:
                cand = (root / check_path).resolve()
                if not cand.is_relative_to(root):
                    raise DelegationAttenuationError(f"Target scope escapes workspace root: '{t}'")
        child_targets_list.append(norm_t)

    return tuple(sorted(set(child_targets_list)))


def attenuate_operation_scope(
    parent_operations: Sequence[str],
    requested_operations: Optional[Sequence[str]],
) -> Tuple[str, ...]:
    """Attenuate child operation scope from parent operation scope against KNOWN_OPERATIONS."""
    if type(parent_operations) not in (list, tuple, set, frozenset):
        raise DelegationValidationError("parent_operations must be a sequence of operation strings")
    clean_p_ops = tuple(sorted({o.strip().lower() for o in parent_operations if o.strip()}))

    if requested_operations is None:
        return clean_p_ops

    if type(requested_operations) not in (list, tuple, set, frozenset):
        raise DelegationValidationError("requested_operations must be a sequence of operation strings")

    c_ops_list: List[str] = []
    for op in requested_operations:
        if type(op) is not str:
            raise DelegationValidationError(f"Operation name must be a string, got {type(op)}")
        clean_op = op.strip().lower()
        if not clean_op:
            raise DelegationValidationError("Operation name cannot be empty")
        if clean_op not in KNOWN_OPERATIONS:
            raise DelegationAttenuationError(f"Unknown or unsupported operation: '{op}'")
        if not clean_p_ops or clean_op not in clean_p_ops:
            raise DelegationAttenuationError(
                f"Requested operation '{op}' exceeds parent operation scope {list(clean_p_ops)}"
            )
        c_ops_list.append(clean_op)

    return tuple(sorted(set(c_ops_list)))


def attenuate_network_scope(
    parent_endpoints: Sequence[str],
    requested_endpoints: Optional[Sequence[str]],
) -> Tuple[str, ...]:
    """Attenuate child network scope from parent network scope."""
    if type(parent_endpoints) not in (list, tuple, set, frozenset):
        raise DelegationValidationError("parent_endpoints must be a sequence of network endpoints")
    clean_p_net = tuple(sorted({n.strip().lower() for n in parent_endpoints if n.strip()}))

    if requested_endpoints is None:
        return clean_p_net

    if type(requested_endpoints) not in (list, tuple, set, frozenset):
        raise DelegationValidationError("requested_endpoints must be a sequence of network endpoints")

    c_net_list: List[str] = []
    for ep in requested_endpoints:
        if type(ep) is not str:
            raise DelegationValidationError(f"Network endpoint must be a string, got {type(ep)}")
        clean_ep = ep.strip().lower()
        if not clean_ep:
            raise DelegationValidationError("Network endpoint cannot be empty")
        if not clean_p_net or not any(is_network_subset(clean_ep, pe) for pe in clean_p_net):
            raise DelegationAttenuationError(
                f"Requested network scope '{ep}' exceeds parent network scope {list(clean_p_net)}"
            )
        c_net_list.append(clean_ep)

    return tuple(sorted(set(c_net_list)))


def attenuate_delegation(
    parent_authority: Union[DelegationContract, ApprovedExecutionContract, Capabilities, Any],
    *,
    child_subagent_id: str,
    requested_target_scope: Optional[Sequence[str]] = None,
    requested_operation_scope: Optional[Sequence[str]] = None,
    requested_network_scope: Optional[Sequence[str]] = None,
    requested_capabilities: Optional[Capabilities] = None,
    requested_git_policy: Optional[GitPolicy] = None,
    parent_work_id: Optional[str] = None,
    actor: Optional[str] = None,
    session_id: Optional[str] = None,
    session_incarnation_id: Optional[str] = None,
    ttl_seconds: Optional[float] = None,
    expires_at: Optional[float] = None,
    role: str = "",
    purpose: str = "",
    metadata: Optional[Dict[str, Any]] = None,
    delegation_id: Optional[str] = None,
    repo_dir: Optional[Path] = None,
) -> DelegationContract:
    """Deterministically derive an attenuated child DelegationContract."""
    return DelegationContract.derive(
        parent_authority=parent_authority,
        child_subagent_id=child_subagent_id,
        requested_target_scope=requested_target_scope,
        requested_operation_scope=requested_operation_scope,
        requested_network_scope=requested_network_scope,
        requested_capabilities=requested_capabilities,
        requested_git_policy=requested_git_policy,
        parent_work_id=parent_work_id,
        actor=actor,
        session_id=session_id,
        session_incarnation_id=session_incarnation_id,
        ttl_seconds=ttl_seconds,
        expires_at=expires_at,
        role=role,
        purpose=purpose,
        metadata=metadata,
        delegation_id=delegation_id,
        repo_dir=repo_dir,
    )


@dataclass(frozen=True)
class DelegationContract:
    """Immutable delegation contract defining strictly attenuated child authority.

    A DelegationContract is a declarative scope specification, NOT an execution engine.
    It satisfies:
    1. Monotonic authority attenuation (child_scope <= parent_scope).
    2. Deterministic cryptographic digest validation.
    3. Strict identity, session, and session-incarnation binding.
    4. Time-bounded validity with fail-closed expiration.
    5. Pure domain model: zero execution primitives, zero filesystem mutation.
    """
    delegation_id: str
    parent_work_id: str
    child_subagent_id: str
    actor: str
    session_id: str
    session_incarnation_id: str
    capabilities: Capabilities = field(default_factory=Capabilities)
    target_scope: Tuple[str, ...] = ()
    operation_scope: Tuple[str, ...] = ()
    network_scope: Tuple[str, ...] = ()
    git_policy: GitPolicy = field(default_factory=GitPolicy)
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    digest: str = ""
    schema_version: int = CURRENT_DELEGATION_SCHEMA_VERSION
    parent_delegation_id: Optional[str] = None
    role: str = ""
    purpose: str = ""
    metadata: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        # Validate ID formats
        object.__setattr__(self, "delegation_id", validate_delegation_id(self.delegation_id))
        object.__setattr__(self, "parent_work_id", _validate_binding(self.parent_work_id, "parent_work_id"))
        object.__setattr__(self, "child_subagent_id", _validate_binding(self.child_subagent_id, "child_subagent_id"))
        object.__setattr__(self, "actor", _validate_binding(self.actor, "actor"))
        object.__setattr__(self, "session_id", _validate_binding(self.session_id, "session_id"))
        object.__setattr__(
            self, "session_incarnation_id", _validate_binding(self.session_incarnation_id, "session_incarnation_id")
        )

        if self.parent_delegation_id is not None:
            object.__setattr__(
                self, "parent_delegation_id", validate_delegation_id(self.parent_delegation_id)
            )

        # Normalize target scope
        if type(self.target_scope) not in (list, tuple, set, frozenset):
            raise DelegationValidationError("target_scope must be a sequence of paths")
        clean_targets = tuple(sorted({normalize_target_path(t) for t in self.target_scope}))
        object.__setattr__(self, "target_scope", clean_targets)

        # Normalize operation scope
        if type(self.operation_scope) not in (list, tuple, set, frozenset):
            raise DelegationValidationError("operation_scope must be a sequence of action strings")
        clean_ops = tuple(sorted({o.strip().lower() for o in self.operation_scope if o.strip()}))
        object.__setattr__(self, "operation_scope", clean_ops)

        # Normalize network scope
        if type(self.network_scope) not in (list, tuple, set, frozenset):
            raise DelegationValidationError("network_scope must be a sequence of network endpoints")
        clean_net = tuple(sorted({n.strip().lower() for n in self.network_scope if n.strip()}))
        object.__setattr__(self, "network_scope", clean_net)

        # Validate types of capabilities and policies
        if not isinstance(self.capabilities, Capabilities):
            raise DelegationValidationError("capabilities must be a Capabilities instance")
        if not isinstance(self.git_policy, GitPolicy):
            raise DelegationValidationError("git_policy must be a GitPolicy instance")

        # Enforce canonical Git authority synchronization at contract creation
        if not self.digest and self.capabilities.git != self.git_policy:
            if self.git_policy != GitPolicy() and self.capabilities.git == GitPolicy():
                object.__setattr__(
                    self,
                    "capabilities",
                    Capabilities(
                        filesystem=self.capabilities.filesystem,
                        shell=self.capabilities.shell,
                        network=self.capabilities.network,
                        git=self.git_policy,
                    ),
                )
            elif self.capabilities.git != GitPolicy() and self.git_policy == GitPolicy():
                object.__setattr__(self, "git_policy", self.capabilities.git)
            else:
                effective_git = GitPolicy(
                    read=self.git_policy.read and self.capabilities.git.read,
                    commit=self.git_policy.commit and self.capabilities.git.commit,
                    push=self.git_policy.push and self.capabilities.git.push,
                )
                object.__setattr__(self, "git_policy", effective_git)
                object.__setattr__(
                    self,
                    "capabilities",
                    Capabilities(
                        filesystem=self.capabilities.filesystem,
                        shell=self.capabilities.shell,
                        network=self.capabilities.network,
                        git=effective_git,
                    ),
                )

        # Validate timestamps and expiration
        if not isinstance(self.created_at, (int, float)) or not math.isfinite(self.created_at):
            raise DelegationValidationError("created_at must be a valid finite number")
        object.__setattr__(self, "created_at", float(self.created_at))

        if not isinstance(self.expires_at, (int, float)) or not math.isfinite(self.expires_at):
            raise DelegationValidationError("expires_at must be a valid finite number")
        object.__setattr__(self, "expires_at", float(self.expires_at))

        if self.expires_at <= self.created_at:
            raise DelegationValidationError(
                f"expires_at ({self.expires_at}) must be strictly greater than created_at ({self.created_at})"
            )

        # Schema version
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise DelegationValidationError("schema_version must be a positive integer")

        # Bounds checks
        if type(self.role) is not str:
            raise DelegationValidationError("role must be a string")
        if len(self.role) > MAX_DELEGATION_ROLE_CHARS:
            object.__setattr__(self, "role", self.role[:MAX_DELEGATION_ROLE_CHARS])

        if type(self.purpose) is not str:
            raise DelegationValidationError("purpose must be a string")
        if len(self.purpose) > MAX_DELEGATION_PURPOSE_CHARS:
            object.__setattr__(self, "purpose", self.purpose[:MAX_DELEGATION_PURPOSE_CHARS])

        if self.metadata is not None:
            if type(self.metadata) is not dict:
                raise DelegationValidationError("metadata must be a dictionary when provided")
            object.__setattr__(self, "metadata", dict(self.metadata))

        # Strictly reject credential and secret patterns
        reject_secrets({
            "delegation_id": self.delegation_id,
            "parent_work_id": self.parent_work_id,
            "child_subagent_id": self.child_subagent_id,
            "actor": self.actor,
            "role": self.role,
            "purpose": self.purpose,
            "target_scope": self.target_scope,
            "operation_scope": self.operation_scope,
            "network_scope": self.network_scope,
            "metadata": self.metadata or {},
        })

        # Digest integrity calculation & verification
        expected_digest = self.compute_digest()
        if not self.digest:
            object.__setattr__(self, "digest", expected_digest)
        elif self.digest != expected_digest:
            raise DelegationIntegrityError(
                f"Delegation contract digest mismatch: expected '{expected_digest}', got '{self.digest}'"
            )

    @property
    def id(self) -> str:
        """Alias for delegation_id."""
        return self.delegation_id

    @property
    def is_expired(self) -> bool:
        """True if the contract has exceeded its expiration timestamp."""
        return time.time() >= self.expires_at

    def compute_digest(self) -> str:
        """Deterministically compute SHA-256 digest over all authority-bearing fields."""
        payload = {
            "schema_version": self.schema_version,
            "delegation_id": self.delegation_id,
            "parent_work_id": self.parent_work_id,
            "child_subagent_id": self.child_subagent_id,
            "actor": self.actor,
            "session_id": self.session_id,
            "session_incarnation_id": self.session_incarnation_id,
            "capabilities": self.capabilities.to_dict(),
            "target_scope": list(self.target_scope),
            "operation_scope": list(self.operation_scope),
            "network_scope": list(self.network_scope),
            "git_policy": self.git_policy.to_dict(),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "parent_delegation_id": self.parent_delegation_id,
        }
        canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    def validate_integrity(self) -> None:
        """Verify that contract digest matches all current authority-bearing fields."""
        expected = self.compute_digest()
        if self.digest != expected:
            raise DelegationIntegrityError(
                f"Delegation contract digest mismatch: expected '{expected}', got '{self.digest}'"
            )

    def validate(
        self,
        *,
        actor: str,
        session_id: str,
        session_incarnation_id: str,
        repo_dir: Optional[Path] = None,
        current_time: Optional[float] = None,
    ) -> None:
        """Strictly validate contract integrity, session/actor bindings, and non-expiration.

        Does NOT renew or extend expiration. Fails closed on any discrepancy.
        """
        self.validate_integrity()

        if not all((actor, session_id, session_incarnation_id)):
            raise DelegationValidationError("Validation requires actor, session_id, and session_incarnation_id")

        if (actor, session_id, session_incarnation_id) != (
            self.actor,
            self.session_id,
            self.session_incarnation_id,
        ):
            raise DelegationValidationError(
                "Contract binding mismatch: actor or session context differs from issued delegation"
            )

        now = time.time() if current_time is None else current_time
        if now >= self.expires_at:
            raise DelegationExpiredError(
                f"Delegation contract '{self.delegation_id}' expired at {self.expires_at} (current time: {now})"
            )

        if repo_dir is not None:
            root = repo_dir.resolve()
            for t in self.target_scope:
                cand = (root / t).resolve()
                if not cand.is_relative_to(root):
                    raise DelegationAttenuationError(f"Target scope escapes workspace root: '{t}'")

    def to_dict(self) -> Dict[str, Any]:
        """Serialize DelegationContract to dictionary, strictly enforcing secret scrubbing."""
        data: Dict[str, Any] = {
            "schema_version": self.schema_version,
            "delegation_id": self.delegation_id,
            "parent_work_id": self.parent_work_id,
            "child_subagent_id": self.child_subagent_id,
            "actor": self.actor,
            "session_id": self.session_id,
            "session_incarnation_id": self.session_incarnation_id,
            "capabilities": self.capabilities.to_dict(),
            "target_scope": list(self.target_scope),
            "operation_scope": list(self.operation_scope),
            "network_scope": list(self.network_scope),
            "git_policy": self.git_policy.to_dict(),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "digest": self.digest,
            "parent_delegation_id": self.parent_delegation_id,
            "role": self.role,
            "purpose": self.purpose,
            "metadata": dict(self.metadata) if self.metadata else None,
        }
        reject_secrets(data)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> DelegationContract:
        """Deserialize DelegationContract from dictionary, enforcing strict validation and integrity."""
        if type(data) is not dict:
            raise DelegationValidationError("Expected dictionary for DelegationContract.from_dict")

        reject_secrets(data)

        expected_keys = {
            "schema_version", "delegation_id", "parent_work_id", "child_subagent_id",
            "actor", "session_id", "session_incarnation_id", "capabilities",
            "target_scope", "operation_scope", "network_scope", "git_policy",
            "created_at", "expires_at", "digest", "parent_delegation_id",
            "role", "purpose", "metadata",
        }
        extra_keys = set(data.keys()) - expected_keys
        if extra_keys:
            raise DelegationValidationError(f"Unknown authority fields in delegation contract: {sorted(extra_keys)}")

        for req in (
            "delegation_id", "parent_work_id", "child_subagent_id", "actor",
            "session_id", "session_incarnation_id", "created_at", "expires_at", "digest"
        ):
            if req not in data or data[req] is None:
                raise DelegationValidationError(f"Missing required field in DelegationContract: '{req}'")

        caps_dict = data.get("capabilities") or {}
        caps = Capabilities.from_dict(caps_dict) if isinstance(caps_dict, dict) else Capabilities()

        git_dict = data.get("git_policy") or {}
        git = GitPolicy.from_dict(git_dict) if isinstance(git_dict, dict) else caps.git

        contract = cls(
            delegation_id=data["delegation_id"],
            parent_work_id=data["parent_work_id"],
            child_subagent_id=data["child_subagent_id"],
            actor=data["actor"],
            session_id=data["session_id"],
            session_incarnation_id=data["session_incarnation_id"],
            capabilities=caps,
            target_scope=tuple(data.get("target_scope") or ()),
            operation_scope=tuple(data.get("operation_scope") or ()),
            network_scope=tuple(data.get("network_scope") or ()),
            git_policy=git,
            created_at=float(data["created_at"]),
            expires_at=float(data["expires_at"]),
            digest=str(data["digest"]),
            schema_version=int(data.get("schema_version", CURRENT_DELEGATION_SCHEMA_VERSION)),
            parent_delegation_id=data.get("parent_delegation_id"),
            role=str(data.get("role", "")),
            purpose=str(data.get("purpose", "")),
            metadata=data.get("metadata"),
        )
        contract.validate_integrity()
        return contract

    @classmethod
    def derive(
        cls,
        *,
        parent_authority: Union[DelegationContract, ApprovedExecutionContract, Capabilities, Any],
        child_subagent_id: str,
        requested_target_scope: Optional[Sequence[str]] = None,
        requested_operation_scope: Optional[Sequence[str]] = None,
        requested_network_scope: Optional[Sequence[str]] = None,
        requested_capabilities: Optional[Capabilities] = None,
        requested_git_policy: Optional[GitPolicy] = None,
        parent_work_id: Optional[str] = None,
        actor: Optional[str] = None,
        session_id: Optional[str] = None,
        session_incarnation_id: Optional[str] = None,
        ttl_seconds: Optional[float] = None,
        expires_at: Optional[float] = None,
        role: str = "",
        purpose: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        delegation_id: Optional[str] = None,
        repo_dir: Optional[Path] = None,
    ) -> DelegationContract:
        """Deterministically derive a child DelegationContract with strictly attenuated authority.

        Enforces:
        - child_scope <= parent_scope across every capability dimension.
        - Monotonic authority (a child cannot derive broader authority than its parent contract).
        - Rejection (fails closed) if requested child authority exceeds parent authority.
        - Identity and session binding preservation.
        - Non-exceeding time-bounded expiration.
        """
        now = time.time()

        # 1. Extract and validate parent authority
        parent_delg_id: Optional[str] = None
        if isinstance(parent_authority, DelegationContract):
            parent_authority.validate_integrity()
            if parent_authority.is_expired:
                raise DelegationExpiredError(
                    f"Parent delegation contract '{parent_authority.delegation_id}' is expired"
                )
            p_work_id = parent_authority.parent_work_id
            p_actor = parent_authority.actor
            p_session_id = parent_authority.session_id
            p_session_inc = parent_authority.session_incarnation_id
            p_caps = parent_authority.capabilities
            p_targets = parent_authority.target_scope
            p_ops = parent_authority.operation_scope
            p_net = parent_authority.network_scope
            p_git = parent_authority.git_policy
            p_expires = parent_authority.expires_at
            parent_delg_id = parent_authority.delegation_id
            if p_git != GitPolicy() and p_caps.git == GitPolicy():
                p_caps = Capabilities(
                    filesystem=p_caps.filesystem,
                    shell=p_caps.shell,
                    network=p_caps.network,
                    git=p_git,
                )

        elif isinstance(parent_authority, ApprovedExecutionContract):
            parent_authority.validate_integrity()
            if parent_authority.expires_at > 0 and now >= parent_authority.expires_at:
                raise DelegationExpiredError("Parent execution contract is expired")
            p_work_id = parent_work_id or parent_authority.request_id
            p_actor = parent_authority.actor
            p_session_id = parent_authority.session_id
            p_session_inc = parent_authority.session_incarnation_id or ""
            p_caps = parent_authority.capabilities
            p_targets = tuple(sorted(parent_authority.approved_targets))
            p_ops = (parent_authority.action_type,) if parent_authority.action_type else ()
            p_net = ("configured_model_api",) if p_caps.network.access else ()
            p_git = p_caps.git
            p_expires = parent_authority.expires_at if parent_authority.expires_at > 0 else (now + DEFAULT_DELEGATION_TTL_SECONDS)

        elif isinstance(parent_authority, Capabilities):
            p_work_id = parent_work_id or ""
            p_actor = actor or ""
            p_session_id = session_id or ""
            p_session_inc = session_incarnation_id or ""
            p_caps = parent_authority
            p_targets = tuple(sorted(p_caps.filesystem.write or p_caps.filesystem.read))
            p_ops = ()
            p_net = ("configured_model_api",) if p_caps.network.access else ()
            p_git = p_caps.git
            p_expires = now + DEFAULT_DELEGATION_TTL_SECONDS

        elif hasattr(parent_authority, "capabilities") and hasattr(parent_authority, "id"):
            # Work domain model instance
            p_work_id = parent_authority.id
            p_actor = getattr(parent_authority, "actor_id", None) or getattr(parent_authority, "actor", "")
            p_session_id = getattr(parent_authority, "session_id", "") or ""
            p_session_inc = getattr(parent_authority, "session_incarnation_id", "") or ""
            p_caps = parent_authority.capabilities or Capabilities()
            p_targets = tuple(sorted(p_caps.filesystem.write or p_caps.filesystem.read))
            p_ops = ()
            p_net = ("configured_model_api",) if p_caps.network.access else ()
            p_git = p_caps.git
            p_expires = now + DEFAULT_DELEGATION_TTL_SECONDS

        else:
            raise DelegationValidationError(f"Unsupported parent authority type: {type(parent_authority)}")

        # 2. Bind and verify identity
        eff_work_id = parent_work_id if parent_work_id is not None else p_work_id
        if not eff_work_id:
            raise DelegationValidationError("parent_work_id is required")
        if parent_work_id and p_work_id and parent_work_id != p_work_id:
            raise DelegationAttenuationError(
                f"Parent work ID mismatch: requested '{parent_work_id}', parent authority has '{p_work_id}'"
            )

        eff_actor = actor if actor is not None else p_actor
        if not eff_actor:
            raise DelegationValidationError("actor identity is required")
        if actor and p_actor and actor != p_actor:
            raise DelegationAttenuationError(
                f"Actor identity mismatch: requested '{actor}', parent authority bound to '{p_actor}'"
            )

        eff_session_id = session_id if session_id is not None else p_session_id
        if not eff_session_id:
            raise DelegationValidationError("session_id is required")
        if session_id and p_session_id and session_id != p_session_id:
            raise DelegationAttenuationError("Cross-session delegation prohibited")

        eff_session_inc = session_incarnation_id if session_incarnation_id is not None else p_session_inc
        if not eff_session_inc:
            raise DelegationValidationError("session_incarnation_id is required")
        if session_incarnation_id and p_session_inc and session_incarnation_id != p_session_inc:
            raise DelegationAttenuationError("Stale or mismatched session incarnation prohibited")

        # 3. Attenuate Filesystem / Target Scope
        child_targets = attenuate_target_scope(p_targets, requested_target_scope, repo_dir=repo_dir)

        # 4. Attenuate Operations Scope
        child_ops = attenuate_operation_scope(p_ops, requested_operation_scope)

        # 5. Attenuate Network Scope
        child_net = attenuate_network_scope(p_net, requested_network_scope)

        # 6. Attenuate Capabilities
        child_caps = attenuate_capabilities(p_caps, requested_capabilities)

        # 7. Attenuate Git Policy
        child_git = attenuate_git_policy(p_git, requested_git_policy)
        if requested_capabilities is not None:
            child_git = attenuate_git_policy(child_git, child_caps.git)
        if child_caps.git != child_git:
            child_caps = Capabilities(
                filesystem=child_caps.filesystem,
                shell=child_caps.shell,
                network=child_caps.network,
                git=child_git,
            )

        # 8. Attenuate Expiration
        if ttl_seconds is not None:
            if ttl_seconds <= 0:
                raise DelegationValidationError("ttl_seconds must be positive")
            candidate_expires = now + ttl_seconds
        elif expires_at is not None:
            candidate_expires = expires_at
        else:
            candidate_expires = min(now + DEFAULT_DELEGATION_TTL_SECONDS, p_expires)

        if candidate_expires <= now:
            raise DelegationValidationError("Expiration timestamp must be in the future")
        if candidate_expires > p_expires:
            raise DelegationAttenuationError(
                f"Child expiration ({candidate_expires}) exceeds parent expiration ({p_expires})"
            )
        child_expires = candidate_expires

        actual_delg_id = delegation_id or f"delg_{secrets.token_hex(16)}"

        contract = cls(
            delegation_id=actual_delg_id,
            parent_work_id=eff_work_id,
            child_subagent_id=child_subagent_id,
            actor=eff_actor,
            session_id=eff_session_id,
            session_incarnation_id=eff_session_inc,
            capabilities=child_caps,
            target_scope=child_targets,
            operation_scope=child_ops,
            network_scope=child_net,
            git_policy=child_git,
            created_at=now,
            expires_at=child_expires,
            parent_delegation_id=parent_delg_id,
            role=role,
            purpose=purpose,
            metadata=metadata,
        )
        return contract


__all__ = [
    "CURRENT_DELEGATION_SCHEMA_VERSION",
    "DEFAULT_DELEGATION_TTL_SECONDS",
    "DelegationAttenuationError",
    "DelegationContract",
    "DelegationError",
    "DelegationExpiredError",
    "DelegationIntegrityError",
    "DelegationValidationError",
    "KNOWN_OPERATIONS",
    "MAX_DELEGATION_ID_CHARS",
    "MAX_DELEGATION_PURPOSE_CHARS",
    "MAX_DELEGATION_ROLE_CHARS",
    "MAX_DELEGATION_TTL_SECONDS",
    "attenuate_capabilities",
    "attenuate_delegation",
    "attenuate_filesystem_policy",
    "attenuate_git_policy",
    "attenuate_network_policy",
    "attenuate_network_scope",
    "attenuate_operation_scope",
    "attenuate_shell_policy",
    "attenuate_target_scope",
    "is_network_subset",
    "is_path_subset",
    "normalize_endpoint",
    "normalize_target_path",
    "validate_delegation_id",
]
