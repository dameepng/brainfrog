"""Approved Execution Contract — Bounded Authorization for Side Effects.

Defines the immutable execution contract binding an approved operation to
its exact authorized filesystem side effects.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, FrozenSet, Optional, Set


def normalize_target_rel_path(repo_dir: Path, target: str) -> str:
    """Normalize an approved target path relative to repo_dir.

    Ensures separators are normalized, redundant segments resolved,
    and returns a canonical relative POSIX path.
    """
    if not target or not target.strip():
        return ""
    clean = target.replace("\\", "/").strip().lstrip("/")
    # Strip leading repo folder if accidentally prepended
    parts = clean.split("/")
    if parts and parts[0] == repo_dir.name and len(parts) > 1:
        clean = "/".join(parts[1:])
    try:
        cand = (repo_dir / clean).resolve()
        repo_res = repo_dir.resolve()
        if cand.is_relative_to(repo_res):
            return cand.relative_to(repo_res).as_posix()
    except Exception:
        pass
    return clean


@dataclass(frozen=True)
class ApprovedExecutionContract:
    """Immutable execution contract binding an approved operation to exact allowed filesystem targets.

    Enforces the security boundary between an approved remote operation and the actual
    files written to disk.
    """

    request_id: str
    action_type: str
    approved_targets: FrozenSet[str]
    operation_digest: str
    channel: str
    allow_remote_git_push: bool = False

    @property
    def is_remote(self) -> bool:
        """True if the contract originates from a remote channel (e.g. telegram, whatsapp)."""
        return self.channel.lower().strip() not in ("cli", "local", "terminal")

    def allows_remote_git_push(self) -> bool:
        """Deterministic check whether remote Git push is authorized.

        Security Invariant:
        An approval for a local file operation must never authorize a remote Git side effect.
        Remote channels (telegram, whatsapp) NEVER allow remote Git push.
        """
        if self.is_remote:
            return False
        return bool(self.allow_remote_git_push)

    @classmethod
    def from_approval_request(
        cls,
        app_req: Any,
        repo_dir: Optional[Path] = None,
    ) -> ApprovedExecutionContract:
        """Construct an immutable execution contract from a verified ApprovalRequest."""
        canon_op = app_req.canonical_operation
        raw_targets: Set[str] = set()

        # 1. Primary target
        if canon_op.target:
            raw_targets.add(str(canon_op.target).strip())

        # 2. Multi-file targets if provided in parameters (e.g. targets/files list)
        multi = canon_op.parameters.get("targets") or canon_op.parameters.get("files")
        if isinstance(multi, (list, tuple, set)):
            for t in multi:
                if t and str(t).strip():
                    raw_targets.add(str(t).strip())

        # Normalize approved targets
        normalized_targets: Set[str] = set()
        rdir = repo_dir.resolve() if repo_dir else Path.cwd().resolve()
        for t in raw_targets:
            norm = normalize_target_rel_path(rdir, t)
            if norm:
                normalized_targets.add(norm)

        return cls(
            request_id=str(app_req.request_id),
            action_type=str(canon_op.action_type).strip().lower(),
            approved_targets=frozenset(normalized_targets),
            operation_digest=str(app_req.operation_digest),
            channel=str(app_req.channel).strip().lower(),
            allow_remote_git_push=False,
        )

    def is_target_allowed(self, rel_path: str) -> bool:
        """Check whether a normalized relative path is within approved scope."""
        clean = rel_path.replace("\\", "/").strip().lstrip("/")
        if clean in self.approved_targets:
            return True
        if sys.platform == "win32":
            lower_clean = clean.lower()
            if any(lower_clean == t.lower() for t in self.approved_targets):
                return True
        return False
