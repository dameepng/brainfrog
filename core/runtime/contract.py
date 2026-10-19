"""Approved Execution Contract — Bounded Authorization for Side Effects.

Defines the immutable execution contract binding an approved operation to
its exact authorized filesystem side effects.
"""
from __future__ import annotations

import re
import json
import math
import time
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, FrozenSet, Optional

from core.runtime.capabilities import Capabilities, scope_path


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
    """Immutable snapshot of a consumed approval; capabilities never execute work."""

    request_id: str
    action_type: str
    approved_targets: FrozenSet[str]
    operation_digest: str
    channel: str
    allow_remote_git_push: bool = False
    capabilities: Capabilities = Capabilities()
    actor: str = ""
    session_id: str = ""
    session_incarnation_id: Optional[str] = None
    expires_at: float = 0.0
    authorization_json: str = ""
    authorization_digest: str = ""
    workspace_root: str = ""
    plan_id: Optional[str] = None
    plan_revision: Optional[int] = None
    plan_content_hash: Optional[str] = None
    plan_metadata_hash: Optional[str] = None

    def __post_init__(self):
        for name in ("request_id", "action_type", "operation_digest", "channel", "actor",
                     "session_id", "authorization_json", "authorization_digest", "workspace_root"):
            if type(getattr(self, name)) is not str:
                raise ValueError("Contract identity fields must be strings")
        if self.session_incarnation_id is not None and type(self.session_incarnation_id) is not str:
            raise ValueError("Invalid session incarnation")
        if self.plan_id is not None and type(self.plan_id) is not str:
            raise ValueError("Invalid plan ID")
        if self.plan_revision is not None and type(self.plan_revision) is not int:
            raise ValueError("Invalid plan revision")
        if self.plan_content_hash is not None and type(self.plan_content_hash) is not str:
            raise ValueError("Invalid plan content hash")
        if self.plan_metadata_hash is not None and type(self.plan_metadata_hash) is not str:
            raise ValueError("Invalid plan metadata hash")
        if type(self.approved_targets) not in (list, tuple, set, frozenset):
            raise ValueError("Invalid approved targets")
        if type(self.capabilities) is not Capabilities:
            raise ValueError("Malformed capabilities")
        object.__setattr__(self, "approved_targets", frozenset(
            scope_path(t) for t in self.approved_targets
        ))
        if set(self.capabilities.filesystem.write) - self.approved_targets:
            raise ValueError("Write scope exceeds approved targets")
        if type(self.allow_remote_git_push) is not bool:
            raise ValueError("Invalid Git policy")
        if type(self.expires_at) not in (int, float) or not math.isfinite(self.expires_at):
            raise ValueError("Invalid expiration")
        if self.authorization_json:
            object.__setattr__(self, "authorization_json", json.dumps(
                json.loads(self.authorization_json), sort_keys=True, separators=(",", ":"),
                ensure_ascii=False,
            ))
            self.validate_integrity()

    @property
    def canonical_operation(self):
        # Return a detached copy, never mutable authority held by the contract.
        from core.runtime.approval import CanonicalOperation
        if not self.authorization_json:
            return None
        op = json.loads(self.authorization_json)["operation"]
        return CanonicalOperation(**op)

    @property
    def is_remote(self) -> bool:
        return self.channel.lower().strip() not in ("cli", "local", "terminal")

    def allows_remote_git_push(self) -> bool:
        return (not self.is_remote and self.allow_remote_git_push
                and self.capabilities.git.push and self.capabilities.network.access)

    def validate_integrity(self):
        from core.runtime.approval import CanonicalOperation
        payload = json.loads(self.authorization_json)
        reject_secrets(payload)
        allowed_keys = {
            "schema_version", "request_id", "session_id", "session_incarnation_id", "channel",
            "user_id", "conversation_id", "operation_type", "operation", "operation_digest",
            "capabilities", "created_at", "expires_at", "nonce", "workspace_root",
        }
        if self.plan_id is not None or "plan_id" in payload:
            allowed_keys |= {"plan_id", "plan_revision", "plan_content_hash"}
            if self.plan_metadata_hash is not None or "plan_metadata_hash" in payload:
                allowed_keys.add("plan_metadata_hash")
        if type(payload) is not dict or set(payload) != allowed_keys or payload["schema_version"] != 2:
            raise ValueError("Malformed contract authorization")
        digest = CanonicalOperation("execution_contract_v2", "", payload).compute_digest()
        if digest != self.authorization_digest:
            raise ValueError("Contract integrity mismatch")
        op = payload["operation"]
        if type(op) is not dict or set(op) != {"action_type", "target", "parameters"}:
            raise ValueError("Malformed contract operation")
        if type(op["parameters"]) is not dict or set(op["parameters"]) - {"targets", "files"}:
            raise ValueError("Unsupported operation parameters in execution contract")
        if CanonicalOperation(**op).compute_digest() != self.operation_digest:
            raise ValueError("Operation digest mismatch")
        expected = {
            "request_id": self.request_id, "channel": self.channel,
            "user_id": self.actor, "session_id": self.session_id,
            "session_incarnation_id": self.session_incarnation_id,
            "expires_at": self.expires_at, "operation_digest": self.operation_digest,
            "capabilities": self.capabilities.to_dict(),
            "workspace_root": self.workspace_root,
        }
        if self.plan_id is not None:
            expected["plan_id"] = self.plan_id
            expected["plan_revision"] = self.plan_revision
            expected["plan_content_hash"] = self.plan_content_hash
            if self.plan_metadata_hash is not None:
                expected["plan_metadata_hash"] = self.plan_metadata_hash
        if any(payload.get(k) != v for k, v in expected.items()):
            raise ValueError("Contract binding mismatch")
        if op["action_type"] != self.action_type or self.allow_remote_git_push:
            raise ValueError("Contract operation or Git policy mismatch")
        targets = operation_targets(op)
        if targets != self.approved_targets:
            raise ValueError("Contract target mismatch")

    def validate_plan_binding(
        self,
        repo_dir: Path,
        expected_plan_id: Optional[str] = None,
    ) -> None:
        """Validate that the contract's bound plan matches disk state.

        Recomputes SHA-256 fingerprint from the actual plan markdown file on disk.
        Fails closed on:
        - Contract missing plan binding when expected_plan_id is given
        - Plan ID mismatch
        - Plan not found on disk
        - Disk plan revision != contract.plan_revision
        - Disk plan markdown content hash != contract.plan_content_hash
        """
        target_plan_id = expected_plan_id or self.plan_id
        if not target_plan_id:
            return

        if not self.plan_id:
            raise PermissionError(
                f"Execution contract (request_id={self.request_id}) is not bound to a plan, "
                f"but execution requested for plan '{expected_plan_id}'."
            )

        if expected_plan_id and self.plan_id != expected_plan_id:
            raise PermissionError(
                f"Contract plan mismatch: contract is bound to '{self.plan_id}', "
                f"but execution requested for plan '{expected_plan_id}'."
            )

        import hashlib
        from core.plans import PlanRegistry
        reg = PlanRegistry(repo_dir)
        plan_doc = reg.get_plan(self.plan_id)
        if plan_doc is None:
            raise PermissionError(f"Bound plan '{self.plan_id}' not found on disk in workspace '{repo_dir}'.")

        if not plan_doc.path.exists():
            raise PermissionError(f"Plan markdown file '{plan_doc.path}' does not exist on disk.")

        actual_content = plan_doc.path.read_text(encoding="utf-8", errors="replace")
        actual_hash = hashlib.sha256(actual_content.encode("utf-8")).hexdigest()

        if getattr(plan_doc, "metadata_tampered", False):
            raise PermissionError(
                f"Plan metadata on disk has been tampered with or modified out-of-band! Contract authorization invalidated."
            )

        if getattr(plan_doc, "metadata_malformed", False):
            raise PermissionError(
                f"Plan metadata on disk is malformed or corrupted! Contract authorization invalidated."
            )

        if self.plan_content_hash and actual_hash != self.plan_content_hash:
            raise PermissionError(
                f"Plan content on disk has been modified since approval! "
                f"Contract fingerprint: {self.plan_content_hash[:8]}, disk fingerprint: {actual_hash[:8]}."
            )

        has_arch_metadata = bool(
            getattr(plan_doc, "target_context", None)
            or getattr(plan_doc, "architecture_strategy", None)
            or getattr(plan_doc, "architectural_constraints", None)
            or getattr(plan_doc, "compatibility_contracts", None)
            or getattr(plan_doc, "behavior_invariants", None)
            or getattr(plan_doc, "migration_constraints", None)
            or getattr(plan_doc, "blocking_unknowns", None)
            or (isinstance(getattr(plan_doc, "metadata_hash", None), str) and getattr(plan_doc, "metadata_hash", "").strip())
        )
        if has_arch_metadata and not self.plan_metadata_hash:
            raise PermissionError(
                f"Execution contract for plan '{self.plan_id}' lacks required semantic metadata binding (plan_metadata_hash). "
                f"Stale or legacy approvals cannot execute without review and reapproval."
            )

        if self.plan_metadata_hash and getattr(plan_doc, "metadata_hash", None) != self.plan_metadata_hash:
            raise PermissionError(
                f"Plan metadata on disk has been modified since approval! "
                f"Contract metadata fingerprint: {self.plan_metadata_hash[:8]}, "
                f"disk metadata fingerprint: {getattr(plan_doc, 'metadata_hash', '')[:8]}."
            )

        if self.plan_revision is not None and plan_doc.revision != self.plan_revision:
            from core.plans import PlanStatus
            plan_status_val = plan_doc.status.value if isinstance(plan_doc.status, PlanStatus) else str(plan_doc.status)
            if not (plan_status_val == PlanStatus.IN_PROGRESS.value and plan_doc.revision == self.plan_revision + 1):
                raise PermissionError(
                    f"Plan revision on disk ({plan_doc.revision}) does not match approved revision ({self.plan_revision})."
                )

        # Invariant: Plan session ownership — unbound legacy plans fail closed; contract session isolation
        plan_session = plan_doc.session_id or (plan_doc.to_metadata_dict().get("session_id") if hasattr(plan_doc, "to_metadata_dict") else "")
        if not isinstance(plan_session, str) or not plan_session.strip():
            raise PermissionError(
                f"Plan '{self.plan_id}' is an unbound legacy plan without valid session ownership. "
                f"Execution rejected to prevent unauthorized execution."
            )
        if self.session_id and plan_session.strip() != self.session_id.strip():
            raise PermissionError(
                f"Contract plan ownership mismatch: plan '{self.plan_id}' belongs to session '{plan_session}', "
                f"but contract is bound to session '{self.session_id}'."
            )

    def validate(self, *, actor: str, channel: str, session_id: str,
                 session_incarnation_id: str, repo_dir: Path,
                 expected_plan_id: Optional[str] = None):
        if not self.authorization_json:
            raise PermissionError("Unissued contract")
        self.validate_integrity()
        if not all((actor, channel, session_id, session_incarnation_id)):
            raise PermissionError("Contract requires actor, channel, session and incarnation binding")
        if (actor, channel, session_id, session_incarnation_id) != (
                self.actor, self.channel, self.session_id, self.session_incarnation_id):
            raise PermissionError("Contract session/actor binding mismatch")
        if time.time() >= self.expires_at:
            raise PermissionError("Contract expired")
        if not self.workspace_root or Path(self.workspace_root).resolve() != repo_dir.resolve():
            raise PermissionError("Contract workspace mismatch")
        for target in (*self.approved_targets, *self.capabilities.filesystem.read,
                       *self.capabilities.filesystem.write):
            scope_path(target, repo_dir)
        self.capabilities.validate_executable()
        if self.action_type not in ("write_file", "write_files", "write_code"):
            raise PermissionError("Operation is not executable under Phase 15A policy")
        for target in self.approved_targets:
            self.capabilities.require("filesystem.write", target, repo_dir)
        self.validate_plan_binding(repo_dir, expected_plan_id=expected_plan_id)

    @classmethod
    def from_approval_request(cls, app_req: Any, repo_dir: Optional[Path] = None):
        from core.runtime.approval import ApprovalStatus
        if not app_req.integrity_valid() or app_req.status != ApprovalStatus.CONSUMED:
            raise PermissionError("Contract requires an intact consumed approval")
        payload = app_req.authorization_payload()
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        reject_secrets(payload)
        targets = operation_targets(payload["operation"])
        for target in targets:
            scope_path(target, repo_dir)
        return cls(
            request_id=app_req.request_id, action_type=app_req.canonical_operation.action_type,
            approved_targets=targets, operation_digest=app_req.operation_digest,
            channel=app_req.channel, capabilities=app_req.capabilities,
            actor=app_req.user_id, session_id=app_req.session_id,
            session_incarnation_id=app_req.session_incarnation_id,
            expires_at=app_req.expires_at, authorization_json=serialized,
            authorization_digest=app_req.authorization_digest,
            workspace_root=app_req.workspace_root,
            plan_id=getattr(app_req, "plan_id", None),
            plan_revision=getattr(app_req, "plan_revision", None),
            plan_content_hash=getattr(app_req, "plan_content_hash", None),
            plan_metadata_hash=getattr(app_req, "plan_metadata_hash", None),
        )

    def is_target_allowed(self, rel_path: str) -> bool:
        try:
            clean = scope_path(rel_path)
            self.capabilities.require("filesystem.write", clean)
            return clean in self.approved_targets
        except (ValueError, PermissionError):
            return False

    def require_filesystem(self, action: str, path: str, repo_dir: Path):
        if not self.authorization_json:
            raise PermissionError("Unissued contract")
        self.validate_integrity()
        if not all((self.actor, self.channel, self.session_id, self.session_incarnation_id)):
            raise PermissionError("Contract requires actor, channel, session and incarnation binding")
        if time.time() >= self.expires_at:
            raise PermissionError("Contract expired")
        if not self.workspace_root or Path(self.workspace_root).resolve() != repo_dir.resolve():
            raise PermissionError("Contract workspace mismatch")
        self.capabilities.require("filesystem." + action, path, repo_dir)
        if action == "write" and not self.is_target_allowed(path):
            raise PermissionError("File outside the approved scope")

    def to_dict(self):
        result = {f.name: getattr(self, f.name) for f in fields(self)}
        result["capabilities"] = self.capabilities.to_dict()
        result["approved_targets"] = sorted(self.approved_targets)
        reject_secrets(result)
        return result

    def to_canonical_json(self):
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data):
        if type(data) is not dict:
            raise ValueError("Contract payload must be a dictionary")
        allowed = {f.name for f in fields(cls)}
        if set(data) - allowed:
            raise ValueError("Unknown or missing contract fields")
        required = allowed - {"plan_id", "plan_revision", "plan_content_hash", "plan_metadata_hash"}
        if not (required <= set(data)):
            raise ValueError("Unknown or missing contract fields")
        values = dict(data)
        values.setdefault("plan_id", None)
        values.setdefault("plan_revision", None)
        values.setdefault("plan_content_hash", None)
        values.setdefault("plan_metadata_hash", None)
        values["capabilities"] = Capabilities.from_dict(values["capabilities"])
        reject_secrets(values["authorization_json"])
        result = cls(**values)
        if not result.authorization_json:
            raise ValueError("Cannot deserialize an unissued contract")
        if time.time() >= result.expires_at:
            raise ValueError("Contract expired")
        return result


def operation_targets(op):
    raw = [op["target"]] if op["target"] else []
    multi = op["parameters"].get("targets") or op["parameters"].get("files") or []
    if type(multi) not in (tuple, list):
        raise ValueError("Malformed operation targets")
    raw.extend(multi)
    if op["action_type"] not in ("write_file", "write_files", "write_code", "read_code"):
        return frozenset()
    return frozenset(scope_path(t) for t in raw)


def reject_secrets(value):
    from core.runtime.secret_scrubbing import scrub_secrets
    if isinstance(value, dict):
        for key, item in value.items():
            if (re.search(r"password|secret|credential|api[_-]?key|token", key, re.I)
                    or key.lower() in ("authorization", "cookie")):
                raise ValueError("Credential fields are forbidden in contracts")
            reject_secrets(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            reject_secrets(item)
    elif isinstance(value, str):
        if scrub_secrets(value) != value or re.search(
                r"(?i)(?:password|api[_-]?key|access[_-]?token|secret)\s*[:=]", value):
            raise ValueError("Credential material is forbidden in contracts")
