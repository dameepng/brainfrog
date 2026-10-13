"""BrainFrog Workspace Registry & Multi-Workspace Isolation.

Provides deterministic, fail-closed project workspace registry:
- Explicit pre-registered aliases only (e.g. 'clinic' -> 'C:\\dame-project\\tools\\clinic-landing-page')
- Rejection of arbitrary user-supplied filesystem paths, relative traversals, or slashes
- Canonical path resolution and filesystem existence verification
- Fail-closed isolation ensuring remote messaging channels never break containment boundaries
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

MAX_WORKSPACE_ALIAS_LENGTH = 64
MAX_WORKSPACES_COUNT = 100
WORKSPACE_ALIAS_REGEX = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


class WorkspaceRegistryError(RuntimeError):
    """Base exception for workspace registry errors."""

    pass


class WorkspaceNotFoundError(WorkspaceRegistryError):
    """Raised when a requested workspace alias is not registered or directory is missing."""

    pass


class WorkspaceValidationError(WorkspaceRegistryError):
    """Raised when a workspace alias or configuration payload fails validation."""

    pass


def validate_workspace_alias(alias: str) -> str:
    """Validate and normalize a workspace alias.

    Must contain only alphanumeric characters, hyphens, and underscores.
    Arbitrary paths, path separators (/ or \\), traversal tokens (..), or empty strings
    are strictly rejected to prevent path injection.
    """
    if not isinstance(alias, str):
        raise WorkspaceValidationError(f"Workspace alias must be a string, got {type(alias).__name__}")

    norm = alias.strip().lower()
    if not norm:
        raise WorkspaceValidationError("Workspace alias cannot be empty.")

    if len(norm) > MAX_WORKSPACE_ALIAS_LENGTH:
        raise WorkspaceValidationError(
            f"Workspace alias exceeds maximum length of {MAX_WORKSPACE_ALIAS_LENGTH} characters."
        )

    if not WORKSPACE_ALIAS_REGEX.match(norm):
        raise WorkspaceValidationError(
            f"Invalid workspace alias '{alias}'. Aliases must contain only alphanumeric characters, "
            "hyphens, or underscores (no path separators, dots, or spaces allowed)."
        )

    return norm


@dataclass(frozen=True)
class WorkspaceEntry:
    """Canonical representation of a registered project workspace."""

    alias: str
    path: Path
    display_name: str
    read_only: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert entry to JSON-serializable dictionary."""
        return {
            "alias": self.alias,
            "path": str(self.path),
            "display_name": self.display_name,
            "read_only": self.read_only,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(
        cls,
        data: Dict[str, Any],
        check_exists: bool = True,
        base_dir: Optional[Path] = None,
    ) -> WorkspaceEntry:
        """Construct entry from dictionary with strict validation."""
        if not isinstance(data, dict):
            raise WorkspaceValidationError(f"Expected dict for WorkspaceEntry, got {type(data).__name__}")

        raw_alias = data.get("alias")
        if not raw_alias:
            raise WorkspaceValidationError("Missing required 'alias' in workspace definition.")
        alias = validate_workspace_alias(str(raw_alias))

        raw_path = data.get("path")
        if not raw_path:
            raise WorkspaceValidationError(f"Missing required 'path' for workspace alias '{alias}'.")

        p = Path(raw_path)
        if not p.is_absolute() and base_dir is not None:
            res_path = (Path(base_dir) / p).resolve()
        else:
            res_path = p.resolve()

        if check_exists:
            if not res_path.exists():
                raise WorkspaceNotFoundError(f"Registered workspace path does not exist: {res_path}")
            if not res_path.is_dir():
                raise WorkspaceValidationError(f"Registered workspace path is not a directory: {res_path}")

        display_name = str(data.get("display_name") or res_path.name or alias).strip()
        read_only = bool(data.get("read_only", False))
        metadata = dict(data.get("metadata", {}))

        return cls(
            alias=alias,
            path=res_path,
            display_name=display_name,
            read_only=read_only,
            metadata=metadata,
        )


class WorkspaceRegistry:
    """Server-side registry of permitted workspaces.

    Enforces fail-closed selection:
    - Only pre-registered aliases can be selected.
    - Rejects arbitrary user-supplied paths.
    - Never falls back silently to another workspace on invalid selection.
    - Rejects paths pointing to the control-plane .brainfrog directory.
    - Resolves relative paths deterministically against the gateway repository root.
    """

    def __init__(
        self,
        entries: Optional[Dict[str, WorkspaceEntry]] = None,
        default_alias: str = "default",
        control_plane_dir: Optional[Path] = None,
        base_dir: Optional[Path] = None,
    ) -> None:
        self._entries: Dict[str, WorkspaceEntry] = {}
        self._default_alias = default_alias.strip().lower()
        self._base_dir = base_dir.resolve() if base_dir else None
        if control_plane_dir:
            self._control_plane_dir = control_plane_dir.resolve()
        elif self._base_dir:
            self._control_plane_dir = (self._base_dir / ".brainfrog").resolve()
        else:
            self._control_plane_dir = None

        if entries:
            for alias, entry in entries.items():
                norm = validate_workspace_alias(alias)
                self._entries[norm] = entry

    @property
    def default_alias(self) -> str:
        return self._default_alias

    @property
    def control_plane_dir(self) -> Optional[Path]:
        return self._control_plane_dir

    @property
    def base_dir(self) -> Optional[Path]:
        return self._base_dir

    def _validate_not_control_plane(self, target_path: Path) -> None:
        """Reject paths resolving to the control-plane .brainfrog directory or any of its descendants."""
        res_target = target_path.resolve()
        if self._control_plane_dir is not None:
            cp_dir = self._control_plane_dir.resolve()
            if res_target == cp_dir:
                raise WorkspaceValidationError(
                    f"Target workspace path '{res_target}' resolves to the BrainFrog control plane '{cp_dir}' and cannot be registered."
                )
            try:
                if res_target.is_relative_to(cp_dir):
                    raise WorkspaceValidationError(
                        f"Target workspace path '{res_target}' resolves inside the BrainFrog control plane '{cp_dir}' and cannot be registered."
                    )
            except AttributeError:
                if cp_dir in res_target.parents:
                    raise WorkspaceValidationError(
                        f"Target workspace path '{res_target}' resolves inside the BrainFrog control plane '{cp_dir}' and cannot be registered."
                    )
        # Even if control_plane_dir wasn't explicitly initialized, any path ending in .brainfrog
        # or having a .brainfrog segment is a control-plane directory.
        if res_target.name == ".brainfrog" or any(part == ".brainfrog" for part in res_target.parts):
            raise WorkspaceValidationError(
                f"Target workspace path '{res_target}' resolves to or inside a BrainFrog control plane directory and cannot be registered."
            )

    def register(
        self,
        alias: str,
        path: Union[str, Path],
        display_name: Optional[str] = None,
        read_only: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
        check_exists: bool = True,
        allow_overwrite: bool = False,
    ) -> WorkspaceEntry:
        """Register a project workspace under a validated alias.

        Raises:
            WorkspaceValidationError: If alias is invalid, duplicate, inside control-plane, or capacity exceeded.
            WorkspaceNotFoundError: If path does not exist and check_exists is True.
        """
        norm_alias = validate_workspace_alias(alias)

        if not str(path).strip():
            raise WorkspaceValidationError("Workspace path cannot be empty.")

        p = Path(path)
        if not p.is_absolute() and self._base_dir is not None:
            res_path = (self._base_dir / p).resolve()
        else:
            res_path = p.resolve()

        self._validate_not_control_plane(res_path)

        if check_exists:
            if not res_path.exists():
                raise WorkspaceNotFoundError(f"Target workspace directory does not exist: {res_path}")
            if not res_path.is_dir():
                raise WorkspaceValidationError(f"Target workspace path is not a directory: {res_path}")

        if norm_alias in self._entries and not allow_overwrite and norm_alias != self._default_alias:
            raise WorkspaceValidationError(f"Duplicate workspace alias '{norm_alias}' already registered.")

        if len(self._entries) >= MAX_WORKSPACES_COUNT and norm_alias not in self._entries:
            raise WorkspaceValidationError(
                f"Workspace registry exceeded capacity limit of {MAX_WORKSPACES_COUNT} entries."
            )

        if norm_alias == self._default_alias and self._control_plane_dir is None:
            self._control_plane_dir = (res_path / ".brainfrog").resolve()
            if self._base_dir is None:
                self._base_dir = res_path

        entry = WorkspaceEntry(
            alias=norm_alias,
            path=res_path,
            display_name=display_name or res_path.name or norm_alias,
            read_only=read_only,
            metadata=dict(metadata or {}),
        )
        self._entries[norm_alias] = entry
        return entry

    def get(self, alias: str) -> Optional[WorkspaceEntry]:
        """Look up a registered workspace entry by alias."""
        try:
            norm_alias = validate_workspace_alias(alias)
        except WorkspaceValidationError:
            return None
        return self._entries.get(norm_alias)

    def resolve(self, alias: str) -> Path:
        """Resolve a registered workspace alias to its canonical Path.

        Raises:
            WorkspaceNotFoundError: If alias is not registered or path is missing.
        """
        entry = self.get(alias)
        if not entry:
            raise WorkspaceNotFoundError(f"Workspace alias '{alias}' is not registered.")

        if not entry.path.exists() or not entry.path.is_dir():
            raise WorkspaceNotFoundError(
                f"Registered workspace path for '{alias}' is missing on disk: {entry.path}"
            )

        return entry.path

    def list_workspaces(self) -> List[WorkspaceEntry]:
        """Return all registered workspaces, sorted deterministically."""
        entries = list(self._entries.values())
        # Default alias first, then alphabetically by alias
        entries.sort(key=lambda e: (0 if e.alias == self._default_alias else 1, e.alias))
        return entries

    def to_dict(self) -> Dict[str, Any]:
        """Serialize registered workspaces to dictionary."""
        return {
            "default_alias": self._default_alias,
            "workspaces": [e.to_dict() for e in self.list_workspaces()],
        }

    def load_from_dict(
        self,
        data: Dict[str, Any],
        check_exists: bool = True,
        base_dir: Optional[Path] = None,
    ) -> None:
        """Load workspace registrations from a configuration dictionary."""
        if not isinstance(data, dict):
            raise WorkspaceValidationError(f"Expected dict for workspace config, got {type(data).__name__}")

        effective_base = (base_dir or self._base_dir)

        if "default_alias" in data:
            self._default_alias = validate_workspace_alias(str(data["default_alias"]))

        seen_aliases: set[str] = set()

        raw_list = data.get("workspaces", [])
        if isinstance(raw_list, list):
            for item in raw_list:
                if not isinstance(item, dict):
                    raise WorkspaceValidationError(f"Workspace definition must be a dict, got {type(item).__name__}")
                raw_alias = item.get("alias")
                if not raw_alias:
                    raise WorkspaceValidationError("Missing required 'alias' in workspace definition.")
                norm = validate_workspace_alias(str(raw_alias))
                if norm in seen_aliases:
                    raise WorkspaceValidationError(f"Duplicate workspace alias '{norm}' encountered in configuration payload.")
                seen_aliases.add(norm)
                entry = WorkspaceEntry.from_dict(item, check_exists=check_exists, base_dir=effective_base)
                self._validate_not_control_plane(entry.path)
                if norm in self._entries and norm != self._default_alias:
                    raise WorkspaceValidationError(f"Conflicting duplicate workspace alias '{norm}' already registered.")
                self._entries[norm] = entry
        elif isinstance(raw_list, dict):
            for k, v in raw_list.items():
                norm = validate_workspace_alias(k)
                if norm in seen_aliases:
                    raise WorkspaceValidationError(f"Duplicate workspace alias '{norm}' encountered in configuration payload.")
                seen_aliases.add(norm)
                if isinstance(v, str):
                    p_val = Path(v)
                    if not p_val.is_absolute() and effective_base is not None:
                        res_val = (effective_base / p_val).resolve()
                    else:
                        res_val = p_val.resolve()
                    self._validate_not_control_plane(res_val)
                    if norm in self._entries and norm != self._default_alias:
                        raise WorkspaceValidationError(f"Conflicting duplicate workspace alias '{norm}' already registered.")
                    self.register(norm, res_val, check_exists=check_exists)
                elif isinstance(v, dict):
                    payload = {"alias": norm, **v}
                    entry = WorkspaceEntry.from_dict(payload, check_exists=check_exists, base_dir=effective_base)
                    self._validate_not_control_plane(entry.path)
                    if norm in self._entries and norm != self._default_alias:
                        raise WorkspaceValidationError(f"Conflicting duplicate workspace alias '{norm}' already registered.")
                    self._entries[norm] = entry
                else:
                    raise WorkspaceValidationError(f"Invalid workspace definition for '{norm}': expected str or dict.")
        else:
            raise WorkspaceValidationError(f"'workspaces' must be a list or dict, got {type(raw_list).__name__}")

    def load_from_file(
        self,
        config_path: Union[str, Path],
        check_exists: bool = True,
        base_dir: Optional[Path] = None,
    ) -> None:
        """Load workspace registrations from a JSON configuration file."""
        p = Path(config_path).resolve()
        if not p.exists():
            raise WorkspaceNotFoundError(f"Workspace configuration file not found: {p}")

        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            raise WorkspaceValidationError(f"Failed to parse workspace config file '{p}': {exc}") from exc

        effective_base = base_dir or self._base_dir or p.parent
        self.load_from_dict(data, check_exists=check_exists, base_dir=effective_base)

    @classmethod
    def create_default(
        cls,
        repo_dir: Path,
        config_path: Optional[Path] = None,
        check_exists: bool = True,
    ) -> WorkspaceRegistry:
        """Create a default registry rooted at repo_dir and auto-load configured workspaces.

        Discovery order:
        1. Register 'default' -> repo_dir.resolve()
        2. Explicit config_path (if provided)
        3. repo_dir / '.brainfrog' / 'workspaces.json' (if present)
        4. Environment variable BRAINFROG_WORKSPACES_FILE (if present)
        5. Environment variable BRAINFROG_WORKSPACES (JSON or alias=path pairs)
        """
        resolved_repo = Path(repo_dir).resolve()
        cp_dir = (resolved_repo / ".brainfrog").resolve()
        registry = cls(
            default_alias="default",
            control_plane_dir=cp_dir,
            base_dir=resolved_repo,
        )

        # 1. Register canonical repo_dir as 'default'
        registry.register(
            alias="default",
            path=resolved_repo,
            display_name=f"{resolved_repo.name} (default)",
            check_exists=check_exists,
            allow_overwrite=True,
        )

        # 2. Explicit config file
        if config_path:
            if not config_path.exists():
                raise WorkspaceNotFoundError(f"Configured workspace file '{config_path}' does not exist.")
            registry.load_from_file(config_path, check_exists=check_exists, base_dir=resolved_repo)

        # 3. .brainfrog/workspaces.json in repo_dir
        local_cfg = resolved_repo / ".brainfrog" / "workspaces.json"
        if local_cfg.exists():
            registry.load_from_file(local_cfg, check_exists=check_exists, base_dir=resolved_repo)

        # 4. Environment variable BRAINFROG_WORKSPACES_FILE
        env_file = os.environ.get("BRAINFROG_WORKSPACES_FILE", "").strip()
        if env_file:
            env_file_path = Path(env_file).resolve()
            if not env_file_path.exists():
                raise WorkspaceNotFoundError(f"BRAINFROG_WORKSPACES_FILE '{env_file}' does not exist.")
            registry.load_from_file(env_file_path, check_exists=check_exists, base_dir=resolved_repo)

        # 5. Environment variable BRAINFROG_WORKSPACES
        env_workspaces = os.environ.get("BRAINFROG_WORKSPACES", "").strip()
        if env_workspaces:
            if env_workspaces.startswith("{"):
                try:
                    parsed = json.loads(env_workspaces)
                except Exception as e:
                    raise WorkspaceValidationError(f"Failed to parse BRAINFROG_WORKSPACES JSON: {e}") from e
                registry.load_from_dict(parsed, check_exists=check_exists, base_dir=resolved_repo)
            else:
                delimiters = ";" if ";" in env_workspaces else ","
                seen_env_aliases: set[str] = set()
                for pair in env_workspaces.split(delimiters):
                    pair = pair.strip()
                    if not pair:
                        continue
                    if "=" not in pair:
                        raise WorkspaceValidationError(f"Invalid BRAINFROG_WORKSPACES entry '{pair}'. Expected alias=path.")
                    k, v = pair.split("=", 1)
                    alias_norm = validate_workspace_alias(k)
                    if alias_norm in seen_env_aliases:
                        raise WorkspaceValidationError(f"Duplicate workspace alias '{alias_norm}' encountered in BRAINFROG_WORKSPACES.")
                    seen_env_aliases.add(alias_norm)
                    p_val = Path(v.strip())
                    if not p_val.is_absolute() and resolved_repo is not None:
                        res_val = (resolved_repo / p_val).resolve()
                    else:
                        res_val = p_val.resolve()
                    registry.register(alias_norm, res_val, check_exists=check_exists)

        return registry
