"""Closed, immutable capability vocabulary. This module never executes work."""
from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from typing import Optional, Tuple

from core.runtime.targets import classify_target_candidate


def scope_path(value: str, repo_dir: Optional[Path] = None) -> str:
    if type(value) is not str:
        raise ValueError("Filesystem scope must be a string")
    candidate = classify_target_candidate(value, repo_dir=repo_dir)
    if not candidate.is_valid:
        raise ValueError("Invalid filesystem capability target")
    normalized = candidate.normalized
    if normalized.split("/")[0].lower() in (".git", ".brainfrog", ".codex", ".agents", ".aws"):
        raise ValueError("Capability cannot target authorization or tool state")
    if repo_dir is not None:
        root = repo_dir.resolve()
        resolved = (root / normalized).resolve()
        if not resolved.is_relative_to(root):
            raise ValueError("Filesystem capability escapes workspace")
        if resolved.relative_to(root).parts and resolved.relative_to(root).parts[0].lower() in (
                ".git", ".brainfrog", ".codex", ".agents", ".aws"):
            raise ValueError("Capability resolves to authorization or tool state")
    return normalized


@dataclass(frozen=True)
class Policy:
    def __post_init__(self):
        for item in fields(self):
            if type(getattr(self, item.name)) is not bool:
                raise ValueError("Capability values must be booleans")

    def to_dict(self):
        return {item.name: getattr(self, item.name) for item in fields(self)}

    @classmethod
    def from_dict(cls, data):
        if type(data) is not dict or set(data) - {f.name for f in fields(cls)}:
            raise ValueError("Unknown or malformed capability policy")
        return cls(**data)


@dataclass(frozen=True)
class FilesystemPolicy(Policy):
    # Exact paths only in Phase 15A: no implicit directory recursion or globbing.
    read: Tuple[str, ...] = ()
    write: Tuple[str, ...] = ()

    def __post_init__(self):
        for name in ("read", "write"):
            values = getattr(self, name)
            if type(values) not in (list, tuple):
                raise ValueError("Filesystem capability must contain a path list")
            object.__setattr__(self, name, tuple(sorted({scope_path(v) for v in values})))

    def to_dict(self):
        return {"read": list(self.read), "write": list(self.write)}


@dataclass(frozen=True)
class ShellPolicy(Policy):
    execute: bool = False


@dataclass(frozen=True)
class NetworkPolicy(Policy):
    # Access is limited to application-configured model APIs, never arbitrary URLs.
    access: bool = False
    scope: str = "configured_model_api"

    def __post_init__(self):
        if (type(self.access) is not bool or type(self.scope) is not str
                or self.scope != "configured_model_api"):
            raise ValueError("Only configured model API network scope is supported")


@dataclass(frozen=True)
class GitPolicy(Policy):
    read: bool = False
    commit: bool = False
    push: bool = False


@dataclass(frozen=True)
class Capabilities:
    filesystem: FilesystemPolicy = FilesystemPolicy()
    shell: ShellPolicy = ShellPolicy()
    network: NetworkPolicy = NetworkPolicy()
    git: GitPolicy = GitPolicy()

    def __post_init__(self):
        for name, cls in self._types().items():
            if type(getattr(self, name)) is not cls:
                raise ValueError("Malformed capability structure")

    @staticmethod
    def _types():
        return {"filesystem": FilesystemPolicy, "shell": ShellPolicy,
                "network": NetworkPolicy, "git": GitPolicy}

    def to_dict(self):
        return {name: getattr(self, name).to_dict() for name in self._types()}

    @classmethod
    def from_dict(cls, data):
        if type(data) is not dict or set(data) - set(cls._types()):
            raise ValueError("Unknown or malformed capabilities")
        return cls(
            filesystem=FilesystemPolicy.from_dict(data.get("filesystem", {})),
            shell=ShellPolicy.from_dict(data.get("shell", {})),
            network=NetworkPolicy.from_dict(data.get("network", {})),
            git=GitPolicy.from_dict(data.get("git", {})),
        )

    def require(self, name: str, target: Optional[str] = None,
                repo_dir: Optional[Path] = None) -> None:
        if type(name) is not str:
            raise PermissionError("Invalid capability name")
        group, _, action = name.partition(".")
        policy = getattr(self, group, None) if group in self._types() else None
        if policy is None or action not in {f.name for f in fields(policy)} or action == "scope":
            raise PermissionError("Unknown capability")
        grant = getattr(policy, action)
        if group == "filesystem":
            if target is None:
                raise PermissionError("Filesystem capability requires a target path")
            try:
                normalized = scope_path(target, repo_dir)
            except ValueError as exc:
                raise PermissionError(str(exc)) from exc
            if normalized in grant:
                return
            raise PermissionError(f"Target '{normalized}' is outside the approved scope for {name}")
        elif grant is True:
            return
        raise PermissionError(f"Capability denied: {name}")

    def validate_executable(self):
        # Subprocesses can escape file scopes; Git hooks can run arbitrary code.
        if self.shell.execute or self.git.read or self.git.commit or self.git.push:
            raise PermissionError("Shell and Git contract execution are not supported in Phase 15A")


def attenuate_capabilities(
    parent_capabilities: Capabilities,
    requested_capabilities: Optional[Capabilities] = None,
) -> Capabilities:
    """Deterministically attenuate child Capabilities from parent Capabilities."""
    from core.runtime.delegation import attenuate_capabilities as _attenuate
    return _attenuate(parent_capabilities, requested_capabilities)
