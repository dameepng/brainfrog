"""Session modes, plan document management, sandbox security, and command permission enforcement.

BrainFrog operates in two primary session modes:
- BUILD (default): Standard execution mode allowing code changes, test running, and PR creation.
- PLAN: Read-only exploration, clarifying questions (grilling), PRD drafting, and implementation planning.
        In Plan mode, source files and mutating shell commands are strictly protected.
        Only plan documents under .brainfrog/plans/ may be created or updated.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

logger = logging.getLogger(__name__)

MODE_CHAT = "chat"
MODE_PLAN = "plan"
MODE_BUILD = "build"
VALID_MODES: Set[str] = {MODE_CHAT, MODE_PLAN, MODE_BUILD}
DEFAULT_MODE_REMOTE: str = MODE_PLAN
DEFAULT_MODE_LOCAL: str = MODE_BUILD
PLANS_DIR_REL = ".brainfrog/plans"

# -------------------------------------------------------------------------
# WAL Deterministic Fault Injection Hook (Phase 2C.2 Workstream A)
# -------------------------------------------------------------------------
_WAL_FAULT_HOOK: Optional[Callable[[str, Dict[str, Any]], None]] = None


def set_wal_fault_hook(hook: Optional[Callable[[str, Dict[str, Any]], None]]) -> None:
    """Set or clear a deterministic fault-injection hook for WAL crash testing."""
    global _WAL_FAULT_HOOK
    _WAL_FAULT_HOOK = hook


def _trigger_wal_fault(stage: str, context: Optional[Dict[str, Any]] = None) -> None:
    """Internal helper to invoke the WAL fault hook if registered."""
    if _WAL_FAULT_HOOK is not None:
        _WAL_FAULT_HOOK(stage, context or {})


def normalize_mode(raw: Optional[str]) -> Optional[str]:
    """Normalize and validate a mode string.

    Returns the canonical mode ('chat', 'plan', 'build') or None if invalid.
    """
    if raw is None or not isinstance(raw, str):
        return None
    val = raw.strip().lower()
    return val if val in VALID_MODES else None


# -------------------------------------------------------------------------
# Shell Safety Classifier (Effect-based Read-Only Enforcement)
# -------------------------------------------------------------------------
_GIT_READONLY_SUBCMDS: Set[str] = {
    "status", "diff", "log", "show", "branch", "remote", "config",
    "rev-parse", "describe", "tag", "ls-files", "grep", "version",
    "--version", "-v", "help", "--help", "check-ignore", "cat-file",
}

_SAFE_READONLY_BINARIES: Set[str] = {
    "dir", "ls", "cat", "type", "head", "tail", "grep", "findstr",
    "rg", "ag", "bat", "more", "less", "pwd", "echo", "which", "where",
    "wc", "nl", "file", "stat",
}

_DANGEROUS_MUTATING_BINARIES: Set[str] = {
    "rm", "del", "erase", "rmdir", "mkdir", "md", "touch", "cp", "copy",
    "mv", "move", "chmod", "chown", "curl", "wget", "kill", "taskkill",
    "format", "shutdown", "reboot",
}

_SAFE_INTERPRETER_FLAGS: Set[str] = {
    "-v", "--version", "-V", "-h", "--help",
}


def is_safe_readonly_command(cmd_str: str) -> Tuple[bool, str]:
    """Classify whether a shell command is strictly read-only and safe for Plan mode.

    Returns (is_safe, refusal_reason).
    """
    clean_cmd = cmd_str.strip()
    if not clean_cmd:
        return True, ""

    # 1. Detect output redirection operators (>, >>, &>, 1>, 2>, *>)
    # Using regex to look for redirection operators outside quotes
    if re.search(r"(?:>>?|\&>|[12]\>|\*\>)", clean_cmd):
        return False, "Operasi pengalihan output (redirection `>`, `>>`) berpotensi menulis ke file dan dilarang dalam mode Plan."

    # 2. Command chaining operators (;, &&, ||, &)
    # Split on chaining operators and validate each segment
    chain_parts = re.split(r";|&&|\|\||&", clean_cmd)
    if len(chain_parts) > 1:
        for part in chain_parts:
            part_clean = part.strip()
            if not part_clean:
                continue
            safe, reason = is_safe_readonly_command(part_clean)
            if not safe:
                return False, f"Bagian dari rantai perintah tidak aman: {reason}"
        return True, ""

    # 3. Pipelines (|)
    # Only allow pipelines if all segments are recognized read-only tools
    if "|" in clean_cmd:
        pipe_segments = clean_cmd.split("|")
        for seg in pipe_segments:
            seg_clean = seg.strip()
            if not seg_clean:
                continue
            safe, reason = is_safe_readonly_command(seg_clean)
            if not safe:
                return False, f"Pipeline berisi perintah non-read-only ({seg_clean}): {reason}"
        return True, ""

    # 4. Tokenize command
    try:
        # Use posix=False on Windows to preserve Windows path semantics, but handle quotes
        tokens = shlex.split(clean_cmd, posix=os.name != "nt")
    except Exception:
        tokens = clean_cmd.split()

    if not tokens:
        return True, ""

    raw_prog = tokens[0].lower().replace("/", "\\")
    prog_name = Path(raw_prog).name.lower()
    if prog_name.endswith((".exe", ".cmd", ".bat")):
        prog_name = prog_name[:-4]

    # Explicitly forbidden mutating utilities
    if prog_name in _DANGEROUS_MUTATING_BINARIES:
        return False, f"Perintah '{prog_name}' memodifikasi berkas/sistem dan dilarang dalam mode Plan."

    # Git command inspection
    if prog_name == "git":
        if len(tokens) < 2:
            return True, ""  # `git` alone prints help
        subcmd = tokens[1].lower()

        if subcmd not in _GIT_READONLY_SUBCMDS:
            return False, (
                f"Sub-perintah git '{subcmd}' dapat memodifikasi state repository. "
                f"Dalam mode Plan, hanya sub-perintah read-only yang diizinkan (status, diff, log, show, dll)."
            )

        # Disallow mutative flags in branch, remote, config, tag
        lower_args = [t.lower() for t in tokens[2:]]
        if subcmd == "branch":
            if any(flag in lower_args for flag in ("-d", "-D", "-m", "-M", "--delete", "--move")):
                return False, "Menghapus atau mengubah nama branch git dilarang dalam mode Plan."
        elif subcmd == "remote":
            if any(act in lower_args for act in ("add", "rename", "remove", "rm", "set-url", "prune")):
                return False, "Memodifikasi konfigurasi git remote dilarang dalam mode Plan."
        elif subcmd == "config":
            if not any(flag in lower_args for flag in ("--get", "-l", "--list", "--get-all", "--get-regexp", "-g")):
                return False, "Memodifikasi konfigurasi git config dilarang dalam mode Plan."
        elif subcmd == "tag":
            if any(flag in lower_args for flag in ("-d", "--delete", "-a", "-s")):
                return False, "Membuat atau menghapus git tag dilarang dalam mode Plan."

        return True, ""

    # Known safe inspection tools
    if prog_name in _SAFE_READONLY_BINARIES:
        return True, ""

    # Programming languages / build tools: must have version/help inspection flags only
    if prog_name in {"python", "python3", "py", "node", "npm", "npx", "cargo", "go", "dotnet", "java", "make", "bash", "sh", "powershell", "pwsh", "cmd"}:
        args = tokens[1:]
        if args and all(arg.lower() in _SAFE_INTERPRETER_FLAGS for arg in args):
            return True, ""
        if prog_name == "npm" and args and args[0].lower() in {"list", "ls", "view", "outdated"}:
            return True, ""
        return False, (
            f"Eksekusi interpreter/build runner '{prog_name}' dengan argumen '{' '.join(args)}' tidak dapat dipastikan read-only. "
            f"Dalam mode Plan, eksekusi skrip dilarang."
        )

    # Any unrecognized command cannot be confirmed read-only
    return False, (
        f"Perintah '{prog_name}' tidak terdaftar sebagai alat inspeksi read-only. "
        f"Dalam mode Plan, hanya perintah inspeksi aman yang diizinkan."
    )


# -------------------------------------------------------------------------
# Plan Lifecycle Contract & Domain Exceptions (Phase 2A)
# -------------------------------------------------------------------------
class PlanStatus(str, Enum):
    """Deterministic lifecycle status for implementation plans."""

    DRAFT = "draft"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    IMPLEMENTED = "implemented"
    VERIFIED = "verified"
    FAILED = "failed"
    SUPERSEDED = "superseded"


# Canonical allowed lifecycle transitions
ALLOWED_PLAN_TRANSITIONS: Dict[PlanStatus, Set[PlanStatus]] = {
    PlanStatus.DRAFT: {PlanStatus.READY, PlanStatus.SUPERSEDED, PlanStatus.FAILED},
    PlanStatus.READY: {PlanStatus.IN_PROGRESS, PlanStatus.BLOCKED, PlanStatus.DRAFT, PlanStatus.SUPERSEDED},
    PlanStatus.IN_PROGRESS: {PlanStatus.IMPLEMENTED, PlanStatus.BLOCKED, PlanStatus.FAILED, PlanStatus.SUPERSEDED},
    PlanStatus.BLOCKED: {PlanStatus.READY, PlanStatus.IN_PROGRESS, PlanStatus.FAILED, PlanStatus.SUPERSEDED},
    PlanStatus.IMPLEMENTED: {PlanStatus.VERIFIED, PlanStatus.FAILED, PlanStatus.BLOCKED, PlanStatus.IN_PROGRESS},
    PlanStatus.FAILED: {PlanStatus.READY, PlanStatus.DRAFT, PlanStatus.SUPERSEDED},
    PlanStatus.VERIFIED: {PlanStatus.SUPERSEDED},
    PlanStatus.SUPERSEDED: set(),
}


class PlanError(Exception):
    """Base exception for plan operations."""

    pass


class PlanNotFoundError(PlanError):
    """Raised when a plan cannot be found in the workspace."""

    pass


class PlanLifecycleError(PlanError):
    """Base exception for plan lifecycle errors."""

    pass


class InvalidPlanTransitionError(PlanLifecycleError):
    """Raised when an illegal plan status transition is requested."""

    pass


class InvalidPlanMetadataError(PlanError):
    """Raised when plan metadata is missing, malformed, or invalid."""

    pass


class AmbiguousPlanError(PlanError):
    """Raised when a query matches multiple candidate plans."""

    pass


class StalePlanStateError(PlanError):
    """Raised when a concurrent update conflicts with current plan revision."""

    pass


def format_blocking_unknown_explanation(
    step_id: str,
    unknowns: Sequence[Any],
    step_files: Optional[Sequence[str]] = None,
) -> str:
    """Format an informative, actionable explanation when an execution step is blocked.

    Explains:
    - Exactly which unresolved business/architectural decisions block the step.
    - Why each decision matters.
    - Affected components/files and step IDs.
    - Confirmation that zero mutations have been executed on the blocked step.
    - Explicit CLI command instructions to resolve the unknown.
    """
    missing_decisions = []
    for raw_u in unknowns:
        if isinstance(raw_u, dict):
            d = raw_u.get("decision") or raw_u.get("question") or str(raw_u)
        else:
            d = str(raw_u).strip()
        missing_decisions.append(d)

    lines = [
        f"⛔ Execution halted: Step '{step_id}' depends on unresolved blocking unknown(s): "
        f"{'; '.join(missing_decisions)}. Clarification required before proceeding.",
    ]
    for idx, raw_u in enumerate(unknowns, 1):
        if isinstance(raw_u, dict):
            u_id = raw_u.get("id") or f"blk_{idx}"
            d = raw_u.get("decision")
            q = raw_u.get("question") or d or "Unspecified decision"
            reason = raw_u.get("reason") or "Missing architectural or business requirement."
            aff_files = raw_u.get("affected_files") or (list(step_files) if step_files else [])
            aff_steps = raw_u.get("affected_steps") or [step_id]
        else:
            u_id = f"blk_{idx}"
            d = str(raw_u).strip()
            q = d
            reason = "Missing architectural or business requirement."
            aff_files = list(step_files) if step_files else []
            aff_steps = [step_id]

        lines.append(f"\n[{u_id}] Question: {q}")
        if d and d != q:
            lines.append(f"• Decision: {d}")
        lines.append(f"• Reason: {reason}")
        if aff_files:
            lines.append(f"• Affected component(s): {', '.join(str(f) for f in aff_files)}")
        if aff_steps:
            lines.append(f"• Affected step(s): {', '.join(str(s) for s in aff_steps)}")

    lines.append("\n⚠️ Status: BLOCKED — No implementation has been executed for this blocked step.")
    lines.append("Existing verified work has been preserved.")
    first_id = (unknowns[0].get("id") if unknowns and isinstance(unknowns[0], dict) else "blk_1") if unknowns else "unknown_id"
    lines.append("To resolve, provide an answer using:")
    lines.append(f"  /plan resolve {first_id} <your answer>")
    return "\n".join(lines)


class BlockingUnknownError(PlanLifecycleError):
    """Raised when an execution step cannot proceed due to unresolved blocking unknowns."""

    def __init__(
        self,
        step_id: str,
        unknowns: List[Any],
        message: Optional[str] = None,
        step_files: Optional[Sequence[str]] = None,
    ) -> None:
        self.step_id = step_id
        self.unknowns = unknowns
        self.step_files = step_files
        msg = message or format_blocking_unknown_explanation(step_id, unknowns, step_files=step_files)
        super().__init__(msg)



# -------------------------------------------------------------------------
# Lifecycle Actor Provenance Invariant (Phase 2C Workstream E)
# -------------------------------------------------------------------------
# The 'actor' parameter in PlanRegistry operations (update_status, verify_plan,
# validate_plan_transition) is strictly AUDIT METADATA for historical traceability.
# It is NEVER an authorization input or capability token.
#
# Authorization decisions in BrainFrog depend EXCLUSIVELY on:
# 1. Cryptographically verified ApprovedExecutionContract (session, revision, disk hash).
# 2. Hardened structured VerificationEvidence (output fingerprints, complete criteria coverage).
# 3. Canonical authenticated runtime context (message.user_id, session isolation).
#
# Supplying actor="verifier", actor="admin", or actor="canonical_runtime" DOES NOT
# grant execution or verification authority.


@dataclass(frozen=True)
class CriterionVerificationMapping:
    """Explicit typed mapping between declared acceptance criteria and verification commands."""

    criterion_id: str
    command: str
    check_name: str = ""
    check_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "command": self.command,
            "check_name": self.check_name or self.command,
            "check_id": self.check_id or f"chk_{uuid.uuid4().hex[:6]}",
        }

    @classmethod
    def from_dict(cls, data: Any) -> CriterionVerificationMapping:
        if isinstance(data, dict):
            crit = str(data.get("criterion_id") or data.get("criterion_ref") or data.get("criterion") or "").strip()
            cmd = str(data.get("command") or data.get("cmd") or "").strip()
            name = str(data.get("check_name") or data.get("name") or cmd).strip()
            cid = str(data.get("check_id") or f"chk_{uuid.uuid4().hex[:6]}").strip()
            if not crit or not cmd:
                raise ValueError("Criterion verification mapping must specify both 'criterion_id' and 'command'.")
            return cls(criterion_id=crit, command=cmd, check_name=name, check_id=cid)
        raise ValueError(f"Invalid verification mapping format: {type(data).__name__}")


@dataclass(frozen=True)
class VerificationCheckResult:
    """Individual verification check outcome bound to an acceptance criterion."""

    check_name: str
    command: str = ""
    status: str = "passed"
    output_fingerprint: str = ""
    timestamp: str = ""
    criterion_ref: str = ""
    check_id: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "check_name": self.check_name,
            "command": self.command,
            "status": self.status,
            "output_fingerprint": self.output_fingerprint,
            "timestamp": self.timestamp,
            "criterion_ref": self.criterion_ref,
            "check_id": self.check_id,
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, data: Any) -> VerificationCheckResult:
        if isinstance(data, str):
            clean = data.strip()
            return cls(check_name=clean, command=clean, status="passed")
        if not isinstance(data, dict):
            raise ValueError("Check result must be a dictionary or command string.")
        check_name = str(data.get("check_name") or data.get("name") or data.get("command") or "").strip()
        if not check_name:
            raise ValueError("Check name or command must be specified.")
        status = str(data.get("status", "passed")).strip().lower()
        criterion_ref = str(data.get("criterion_ref") or data.get("criterion_id") or data.get("criterion") or "").strip()
        check_id = str(data.get("check_id") or "").strip()
        return cls(
            check_name=check_name,
            command=str(data.get("command") or check_name).strip(),
            status=status,
            output_fingerprint=str(data.get("output_fingerprint", "")).strip(),
            timestamp=str(data.get("timestamp", "")).strip(),
            criterion_ref=criterion_ref,
            check_id=check_id,
            details=dict(data.get("details", {}) or {}),
        )


@dataclass
class PlanJournalEntry:
    """Write-ahead journal entry for crash-consistent plan operations."""

    journal_id: str
    plan_id: str
    filename: str
    stage: str  # "PREPARED", "COMMITTED", "ROLLED_BACK"
    created_at: str
    expected_revision: Optional[int] = None
    target_revision: int = 1
    target_status: str = "draft"
    new_md_hash: str = ""
    new_meta_dict: Dict[str, Any] = field(default_factory=dict)
    has_md_update: bool = False
    new_md_content: Optional[str] = None
    previous_md_content: Optional[str] = None
    previous_meta_dict: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "journal_id": self.journal_id,
            "plan_id": self.plan_id,
            "filename": self.filename,
            "stage": self.stage,
            "created_at": self.created_at,
            "expected_revision": self.expected_revision,
            "target_revision": self.target_revision,
            "target_status": self.target_status,
            "new_md_hash": self.new_md_hash,
            "new_meta_dict": self.new_meta_dict,
            "has_md_update": self.has_md_update,
            "new_md_content": self.new_md_content,
            "previous_md_content": self.previous_md_content,
            "previous_meta_dict": self.previous_meta_dict,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> PlanJournalEntry:
        return cls(
            journal_id=str(data["journal_id"]),
            plan_id=str(data["plan_id"]),
            filename=str(data["filename"]),
            stage=str(data.get("stage", "PREPARED")),
            created_at=str(data.get("created_at", "")),
            expected_revision=data.get("expected_revision"),
            target_revision=int(data.get("target_revision", 1)),
            target_status=str(data.get("target_status", "draft")),
            new_md_hash=str(data.get("new_md_hash", "")),
            new_meta_dict=dict(data.get("new_meta_dict", {})),
            has_md_update=bool(data.get("has_md_update", False)),
            new_md_content=data.get("new_md_content"),
            previous_md_content=data.get("previous_md_content"),
            previous_meta_dict=data.get("previous_meta_dict"),
        )


@dataclass(frozen=True)
class VerificationEvidence:
    """Structured, immutable verification evidence bound to exact plan state."""

    plan_id: str
    plan_revision: int
    plan_content_hash: str
    checks: Tuple[VerificationCheckResult, ...]
    timestamp: str = ""
    summary: str = ""
    verifier: str = "verifier"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "plan_revision": self.plan_revision,
            "plan_content_hash": self.plan_content_hash,
            "checks": [c.to_dict() for c in self.checks],
            "timestamp": self.timestamp,
            "summary": self.summary,
            "verifier": self.verifier,
        }

    @classmethod
    def from_dict(
        cls,
        data: Dict[str, Any],
        fallback_plan: Optional[Any] = None,
    ) -> VerificationEvidence:
        if not isinstance(data, dict):
            raise ValueError("Verification evidence must be a dictionary.")
        plan_id = str(data.get("plan_id") or (fallback_plan.id if fallback_plan else "")).strip()
        raw_rev = data.get("plan_revision")
        if raw_rev is not None:
            try:
                plan_revision = int(raw_rev)
            except (ValueError, TypeError):
                raise ValueError("plan_revision must be an integer.")
        elif fallback_plan:
            plan_revision = fallback_plan.revision
        else:
            plan_revision = 0

        content_hash = str(data.get("plan_content_hash") or (fallback_plan.content_hash if fallback_plan else "")).strip()

        raw_checks = data.get("checks", [])
        if not isinstance(raw_checks, (list, tuple)) or not raw_checks:
            raise ValueError("Verification evidence must contain non-empty checks list.")

        parsed_checks = []
        for c in raw_checks:
            parsed_checks.append(VerificationCheckResult.from_dict(c))

        return cls(
            plan_id=plan_id,
            plan_revision=plan_revision,
            plan_content_hash=content_hash,
            checks=tuple(parsed_checks),
            timestamp=str(data.get("timestamp", "")).strip(),
            summary=str(data.get("summary", "")).strip(),
            verifier=str(data.get("verifier", "verifier")).strip(),
        )


def validate_plan_transition(
    current_status: PlanStatus,
    target_status: PlanStatus,
    actor: str,
    reason: str,
    evidence: Optional[Dict[str, Any]] = None,
    plan: Optional[PlanDocument] = None,
    contract: Optional[Any] = None,
    repo_dir: Optional[Path] = None,
) -> None:
    """Validate requested plan status transition against the lifecycle state machine.

    Enforces fail-closed rules:
    - Current and target status must be valid PlanStatus enum values.
    - Transition must exist in ALLOWED_PLAN_TRANSITIONS.
    - Actor and concise reason are mandatory.
    - draft -> ready requires non-empty goal and steps, non-malformed metadata, and untampered content.
    - ready -> in_progress requires valid ApprovedExecutionContract if provided.
    - Transition to verified strictly requires valid, passing VerificationEvidence.
    """
    if not isinstance(current_status, PlanStatus):
        try:
            current_status = PlanStatus(str(current_status))
        except ValueError:
            raise InvalidPlanTransitionError(f"Current status '{current_status}' is not a valid PlanStatus.")

    if not isinstance(target_status, PlanStatus):
        try:
            target_status = PlanStatus(str(target_status))
        except ValueError:
            raise InvalidPlanTransitionError(f"Target status '{target_status}' is not a valid PlanStatus.")

    if not actor or not isinstance(actor, str) or not actor.strip():
        raise InvalidPlanTransitionError("Actor must be specified for any plan status transition.")
    if not reason or not isinstance(reason, str) or not reason.strip():
        raise InvalidPlanTransitionError("A concise reason must be specified for any plan status transition.")

    allowed_targets = ALLOWED_PLAN_TRANSITIONS.get(current_status, set())
    if target_status not in allowed_targets:
        raise InvalidPlanTransitionError(
            f"Illegal plan status transition: '{current_status.value}' -> '{target_status.value}'. "
            f"Allowed target states from '{current_status.value}': {[s.value for s in allowed_targets]}"
        )

    # Invariant: draft -> ready requires valid content
    if current_status == PlanStatus.DRAFT and target_status == PlanStatus.READY:
        if plan is not None:
            if not plan.goal or not plan.goal.strip():
                raise InvalidPlanTransitionError("Cannot mark plan as ready without an explicit goal.")
            if not plan.steps:
                raise InvalidPlanTransitionError("Cannot mark plan as ready without defined steps.")
            if getattr(plan, "metadata_missing", False) or getattr(plan, "metadata_malformed", False):
                raise InvalidPlanTransitionError("Cannot mark plan as ready when metadata is missing or malformed.")
            if getattr(plan, "metadata_tampered", False):
                raise InvalidPlanTransitionError("Cannot mark plan as ready: Metadata has been modified out-of-band.")
            if getattr(plan, "content_tampered", False):
                raise InvalidPlanTransitionError("Cannot mark plan as ready: Markdown content has been modified out-of-band.")

    # Invariant: ready -> in_progress strictly requires valid ApprovedExecutionContract
    if current_status == PlanStatus.READY and target_status == PlanStatus.IN_PROGRESS:
        bound_contract = contract or (evidence.get("contract") if isinstance(evidence, dict) else None)
        if bound_contract is None:
            raise InvalidPlanTransitionError(
                "Transition from 'ready' to 'in_progress' requires a valid, matching ApprovedExecutionContract."
            )
        if isinstance(bound_contract, dict):
            from core.runtime.contract import ApprovedExecutionContract
            try:
                bound_contract = ApprovedExecutionContract.from_dict(bound_contract)
            except Exception as e:
                raise PermissionError(f"Invalid execution contract provided for plan: {e}")
        ws_path = repo_dir or (plan.path.parent.parent if plan and plan.path else Path("."))
        bound_contract.validate_plan_binding(ws_path, expected_plan_id=plan.id if plan else None)

    # Invariant: transition to VERIFIED strictly requires verified evidence
    if target_status == PlanStatus.VERIFIED:
        if not evidence or not isinstance(evidence, dict):
            raise InvalidPlanTransitionError("Transition to 'verified' requires recorded verification evidence.")

        try:
            typed_ev = (
                evidence if isinstance(evidence, VerificationEvidence)
                else VerificationEvidence.from_dict(evidence, fallback_plan=plan)
            )
        except ValueError as ev_err:
            raise InvalidPlanTransitionError(f"Malformed verification evidence: {ev_err}")

        for chk in typed_ev.checks:
            if chk.status != "passed":
                raise InvalidPlanTransitionError(
                    f"Verification check '{chk.check_name}' has non-passing status: '{chk.status}'."
                )

        if plan is not None:
            if getattr(plan, "metadata_tampered", False):
                raise InvalidPlanTransitionError("Cannot verify plan: Metadata has been modified out-of-band.")
            if getattr(plan, "content_tampered", False):
                raise InvalidPlanTransitionError("Cannot verify plan: Markdown content has been modified out-of-band.")
            if typed_ev.plan_id and typed_ev.plan_id != plan.id:
                raise InvalidPlanTransitionError(
                    f"Verification evidence plan_id '{typed_ev.plan_id}' does not match plan '{plan.id}'."
                )
            if typed_ev.plan_revision and typed_ev.plan_revision != plan.revision:
                raise InvalidPlanTransitionError(
                    f"Verification evidence revision {typed_ev.plan_revision} does not match plan revision {plan.revision}."
                )
            if typed_ev.plan_content_hash and plan.content_hash and typed_ev.plan_content_hash != plan.content_hash:
                raise InvalidPlanTransitionError(
                    f"Verification evidence content hash does not match current plan content hash."
                )

            # Workstream D: Explicit Acceptance Criteria Coverage Validation
            raw_criteria = getattr(plan, "acceptance_criteria", None) or []
            declared_criteria = [str(c).strip() for c in raw_criteria if str(c).strip()]
            if not declared_criteria:
                raise InvalidPlanTransitionError(
                    f"Cannot verify plan '{plan.id}': plan has no declared acceptance criteria."
                )
            declared_set = set(declared_criteria)
            covered_criteria: Set[str] = set()

            seen_check_ids: Set[str] = set()
            for chk in typed_ev.checks:
                c_id = (getattr(chk, "check_id", None) or "").strip()
                if c_id:
                    if c_id in seen_check_ids:
                        raise InvalidPlanTransitionError(
                            f"Duplicate check identifier '{c_id}' in verification evidence."
                        )
                    seen_check_ids.add(c_id)
                c_ref = (getattr(chk, "criterion_ref", None) or "").strip()
                if not c_ref:
                    raise InvalidPlanTransitionError(
                        f"Verification check '{chk.check_name}' has no criterion reference. "
                        f"Explicit criterion mapping is required for verification."
                    )
                if c_ref not in declared_set:
                    raise InvalidPlanTransitionError(
                        f"Verification check '{chk.check_name}' references unknown criterion '{c_ref}'. "
                        f"Declared criteria: {sorted(declared_set)}."
                    )
                covered_criteria.add(c_ref)

            missing_criteria = declared_set - covered_criteria
            if missing_criteria:
                raise InvalidPlanTransitionError(
                    f"Incomplete verification coverage: missing checks for criteria: {sorted(missing_criteria)}."
                )


def _normalize_step_id(s: Any) -> str:
    clean = str(s).strip().lower()
    if clean.startswith("step_") or clean.startswith("step "):
        return clean[5:].strip()
    if clean.startswith("step"):
        return clean[4:].strip()
    return clean


def _safe_collection(val: Any) -> List[Any]:
    if isinstance(val, (list, tuple, set)):
        return list(val)
    if val is None:
        return []
    return [val]


def _safe_collection_str(val: Any) -> List[str]:
    return [str(item) for item in _safe_collection(val)]


def compute_metadata_semantic_digest(meta: Dict[str, Any]) -> str:
    """Compute deterministic SHA-256 digest of authoritative semantic plan metadata fields.

    Authoritative semantic fields are the plan specification fields consumed by BUILD:
    target_context, architecture_strategy, scope, non_scope, steps, blocking_unknowns,
    architectural_constraints, compatibility_contracts, behavior_invariants,
    migration_constraints, id, and session_id.

    Ephemeral lifecycle bookkeeping fields (revision, status, timestamps, transitions,
    evidence) are intentionally excluded so legitimate authorized lifecycle transitions
    (e.g., READY -> IN_PROGRESS) do not generate false tampering alerts. Revision and
    status validity are independently enforced by ApprovedExecutionContract.
    """
    authoritative = {
        "id": str(meta.get("id") or ""),
        "session_id": str(meta.get("session_id") or ""),
        "target_context": meta.get("target_context"),
        "architecture_strategy": meta.get("architecture_strategy"),
        "scope": sorted(_safe_collection_str(meta.get("scope"))),
        "non_scope": sorted(_safe_collection_str(meta.get("non_scope"))),
        "steps": meta.get("steps", []),
        "acceptance_criteria": sorted(_safe_collection_str(meta.get("acceptance_criteria"))),
        "verification_commands": sorted(_safe_collection_str(meta.get("verification_commands"))),
        "blocking_unknowns": meta.get("blocking_unknowns", []),
        "architectural_constraints": meta.get("architectural_constraints", []),
        "compatibility_contracts": meta.get("compatibility_contracts", []),
        "behavior_invariants": meta.get("behavior_invariants", []),
        "migration_constraints": meta.get("migration_constraints", []),
    }
    canon_bytes = json.dumps(authoritative, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canon_bytes).hexdigest()


# -------------------------------------------------------------------------
# Plan Document Model & Storage
# -------------------------------------------------------------------------
@dataclass
class PlanDocument:
    filename: str
    path: Path
    title: str
    goal: str
    acceptance_criteria: List[str] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    steps: List[Dict[str, Any]] = field(default_factory=list)
    relevant_files: List[str] = field(default_factory=list)
    commit_hash: str = ""
    git_status_snapshot: str = ""
    file_hashes: Dict[str, str] = field(default_factory=dict)
    created_at: str = ""
    content: str = ""
    verification_commands: List[str] = field(default_factory=list)
    id: str = ""
    session_id: str = ""
    status: PlanStatus = PlanStatus.DRAFT
    revision: int = 1
    updated_at: str = ""
    transitions: List[Dict[str, Any]] = field(default_factory=list)
    verification_history: List[Dict[str, Any]] = field(default_factory=list)
    failure_history: List[Dict[str, Any]] = field(default_factory=list)
    step_progress: List[Dict[str, Any]] = field(default_factory=list)
    metadata_missing: bool = False
    metadata_malformed: bool = False
    content_hash: str = ""
    content_tampered: bool = False
    metadata_hash: str = ""
    metadata_tampered: bool = False
    target_context: Optional[str] = None
    architecture_strategy: Optional[str] = None
    scope: List[str] = field(default_factory=list)
    non_scope: List[str] = field(default_factory=list)
    clarifying_questions: List[str] = field(default_factory=list)
    blocking_unknowns: List[Any] = field(default_factory=list)
    architectural_constraints: List[Any] = field(default_factory=list)
    compatibility_contracts: List[str] = field(default_factory=list)
    behavior_invariants: List[str] = field(default_factory=list)
    migration_constraints: List[str] = field(default_factory=list)

    @property
    def is_resumable(self) -> bool:
        """Return True if plan execution can be resumed."""
        return self.status in (PlanStatus.IN_PROGRESS, PlanStatus.BLOCKED)

    @property
    def is_executable(self) -> bool:
        """Return True if plan is eligible for consideration of execution approval."""
        return self.status in (PlanStatus.READY, PlanStatus.BLOCKED, PlanStatus.FAILED)

    @property
    def is_verifiable(self) -> bool:
        """Return True if plan implementation is complete and awaiting verification."""
        return self.status == PlanStatus.IMPLEMENTED

    @property
    def is_archivable(self) -> bool:
        """Return True if plan can be superseded/archived."""
        return self.status != PlanStatus.SUPERSEDED

    @property
    def has_valid_session_ownership(self) -> bool:
        """Return True if plan document has a valid, non-empty session_id."""
        return isinstance(self.session_id, str) and bool(self.session_id.strip())

    def can_transition_to(self, target: PlanStatus | str) -> bool:
        """Check whether a transition to target status is valid."""
        try:
            norm_target = target if isinstance(target, PlanStatus) else PlanStatus(target)
            return norm_target in ALLOWED_PLAN_TRANSITIONS.get(self.status, set())
        except ValueError:
            return False

    def get_unresolved_blocking_unknowns(
        self,
        step_id: Optional[str] = None,
        files: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Return a list of unresolved blocking unknowns affecting the specified step or files.

        A blocking unknown is considered unresolved if 'resolved' is not True.
        It affects the step if:
        1. No affected_steps or affected_files are specified (global blocking unknown).
        2. step_id is in affected_steps.
        3. Any of the step's files intersect with affected_files (including parent folders and wildcards).
        """
        if not self.blocking_unknowns:
            return []

        norm_step_id = _normalize_step_id(step_id) if step_id is not None else None
        norm_files = {Path(f).as_posix().lower() for f in (files or [])}

        unresolved: List[Dict[str, Any]] = []
        for idx, item in enumerate(self.blocking_unknowns, 1):
            if isinstance(item, str):
                entry = {
                    "id": f"blk_{idx}",
                    "decision": item.strip(),
                    "question": item.strip(),
                    "affected_steps": [],
                    "affected_files": [],
                    "resolved": False,
                }
            elif isinstance(item, dict):
                entry = dict(item)
                if not entry.get("id"):
                    entry["id"] = f"blk_{idx}"
                decision_val = entry.get("decision") or entry.get("question") or entry.get("missing_decision") or entry.get("description")
                if not decision_val or not str(decision_val).strip():
                    entry["decision"] = f"Malformed blocking unknown entry #{idx} without decision"
                    entry["resolved"] = False
                else:
                    entry["decision"] = str(decision_val).strip()
            else:
                entry = {
                    "id": f"blk_{idx}",
                    "decision": f"Malformed blocking unknown item #{idx}: {item!r}",
                    "question": f"Malformed item: {item!r}",
                    "affected_steps": [],
                    "affected_files": [],
                    "resolved": False,
                }

            if entry.get("resolved") is True:
                continue

            raw_steps = entry.get("affected_steps")
            aff_steps = {_normalize_step_id(s) for s in raw_steps} if isinstance(raw_steps, (list, tuple, set)) else set()
            raw_files = entry.get("affected_files")
            aff_files = {Path(f).as_posix().lower() for f in raw_files} if isinstance(raw_files, (list, tuple, set)) else set()

            is_global = not aff_steps and not aff_files
            applies = False
            if is_global:
                applies = True
            else:
                if norm_step_id is not None and norm_step_id in aff_steps:
                    applies = True
                if norm_files and aff_files:
                    is_file_match = bool(norm_files & aff_files) or any(
                        any(
                            nf == af
                            or nf.startswith(f"{af.rstrip('/')}/")
                            or fnmatch.fnmatch(nf, af)
                            for af in aff_files
                        )
                        for nf in norm_files
                    )
                    if is_file_match:
                        applies = True

            if applies:
                unresolved.append(entry)

        return unresolved

    def has_unresolved_blocking_unknowns(
        self,
        step_id: Optional[str] = None,
        files: Optional[Sequence[str]] = None,
    ) -> Tuple[bool, List[Dict[str, Any]]]:
        """Return (True, list_of_unknowns) if there are unresolved blocking unknowns for the step."""
        items = self.get_unresolved_blocking_unknowns(step_id=step_id, files=files)
        return len(items) > 0, items

    def get_blocking_questions(self) -> List[Dict[str, Any]]:
        """Return all blocking questions (both resolved and unresolved) in normalized structure."""
        if not self.blocking_unknowns:
            return []
        questions = []
        for idx, item in enumerate(self.blocking_unknowns, 1):
            if isinstance(item, str):
                questions.append({
                    "id": f"blk_{idx}",
                    "decision": item.strip(),
                    "question": item.strip(),
                    "reason": "Missing architectural or business requirement",
                    "affected_steps": [],
                    "affected_files": [],
                    "resolved": False,
                    "status": "unresolved",
                    "answer": None,
                    "answered_at": None,
                    "answered_by": None,
                    "plan_revision": self.revision,
                })
            elif isinstance(item, dict):
                entry = dict(item)
                if not entry.get("id"):
                    entry["id"] = f"blk_{idx}"
                q = entry.get("question") or entry.get("decision") or entry.get("missing_decision") or entry.get("description") or f"Unknown #{idx}"
                entry["question"] = str(q).strip()
                entry["decision"] = str(q).strip()
                entry["reason"] = str(entry.get("reason") or "Missing architectural or business requirement").strip()
                entry["affected_steps"] = list(entry.get("affected_steps") or [])
                entry["affected_files"] = list(entry.get("affected_files") or [])
                is_resolved = entry.get("resolved") is True
                entry["resolved"] = is_resolved
                entry["status"] = "resolved" if is_resolved else "unresolved"
                entry["answer"] = entry.get("answer")
                entry["answered_at"] = entry.get("answered_at")
                entry["answered_by"] = entry.get("answered_by")
                entry["plan_revision"] = entry.get("plan_revision") or self.revision
                questions.append(entry)
            else:
                questions.append({
                    "id": f"blk_{idx}",
                    "decision": f"Malformed unknown: {item!r}",
                    "question": f"Malformed unknown: {item!r}",
                    "reason": "Malformed item",
                    "affected_steps": [],
                    "affected_files": [],
                    "resolved": False,
                    "status": "unresolved",
                    "answer": None,
                    "answered_at": None,
                    "answered_by": None,
                    "plan_revision": self.revision,
                })
        return questions

    def _is_constraint_relevant_to_step(self, text: str, step_files: Set[str]) -> bool:
        """Determine whether an unstructured constraint text is relevant to step files."""
        norm_text = text.lower().replace("\\", "/")
        if not step_files:
            return True

        caller_invariants = (
            "caller", "callers", "consume", "client", "compatib", "invariant",
            "contract", "public api", "interface", "signature", "global",
        )
        if any(kw in norm_text for kw in caller_invariants):
            return True

        dir_prefixes = {
            p.as_posix().lower()
            for f in step_files
            for p in Path(f).parents
            if p.as_posix() != "."
        }

        mentions_step_file = any(
            f in norm_text or Path(f).name.lower() in norm_text
            for f in step_files
        ) or any(
            f"{d}/" in norm_text or f"/{d}/" in norm_text or norm_text.startswith(f"{d}/")
            for d in dir_prefixes
        )

        other_files = {
            Path(f).as_posix().lower() for f in self.relevant_files
        } - step_files

        mentions_other_file = any(
            f in norm_text or Path(f).name.lower() in norm_text
            for f in other_files
        )

        if mentions_step_file:
            return True
        if mentions_other_file and not mentions_step_file:
            return False

        return True

    def get_step_architectural_context(self, step: Any) -> Dict[str, Any]:
        """Extract bounded architectural context strictly relevant to the given step."""
        raw_id = getattr(step, "id", None) if not isinstance(step, dict) else step.get("id")
        step_id = str(raw_id).strip() if raw_id is not None else ""
        raw_files = getattr(step, "files", None) or (step.get("files") if isinstance(step, dict) else None) or []
        step_files = {Path(f).as_posix().lower() for f in raw_files}

        relevant_constraints: List[str] = []
        for c in self.architectural_constraints:
            if isinstance(c, dict):
                c_text = str(c.get("text") or c.get("description") or c.get("constraint") or "").strip()
                c_files = {Path(f).as_posix().lower() for f in (c.get("files") or c.get("target_files") or [])}
                c_steps = {_normalize_step_id(s) for s in (c.get("steps") or c.get("affected_steps") or [])}
                is_step_match = bool(step_id and _normalize_step_id(step_id) in c_steps)
                is_file_match = bool(step_files & c_files) or any(
                    any(
                        sf == cf
                        or sf.startswith(f"{cf.rstrip('/')}/")
                        or fnmatch.fnmatch(sf, cf)
                        for cf in c_files
                    )
                    for sf in step_files
                )
                if is_step_match:
                    relevant_constraints.append(c_text)
                elif c_files:
                    if is_file_match:
                        relevant_constraints.append(c_text)
                else:
                    if self._is_constraint_relevant_to_step(c_text, step_files):
                        relevant_constraints.append(c_text)
            elif isinstance(c, str):
                c_text = c.strip()
                if self._is_constraint_relevant_to_step(c_text, step_files):
                    relevant_constraints.append(c_text)

        # Step-specific constraints from the step payload itself
        if isinstance(step, dict) and step.get("constraints"):
            for sc in step["constraints"]:
                s_str = str(sc).strip()
                if s_str and s_str not in relevant_constraints:
                    relevant_constraints.append(s_str)
        elif hasattr(step, "constraints") and getattr(step, "constraints"):
            for sc in getattr(step, "constraints"):
                s_str = str(sc).strip()
                if s_str and s_str not in relevant_constraints:
                    relevant_constraints.append(s_str)

        # Extract resolved clarifications relevant to this step
        resolved_clarifications: List[Dict[str, Any]] = []
        for idx, item in enumerate(self.blocking_unknowns or [], 1):
            if isinstance(item, dict) and item.get("resolved") is True:
                clean_answer = str(item.get("answer") or "").strip()
                if not clean_answer:
                    continue
                uid = str(item.get("id") or f"blk_{idx}").strip()
                decision = str(item.get("decision") or item.get("question") or uid).strip()
                raw_aff_steps = item.get("affected_steps")
                aff_steps = {_normalize_step_id(s) for s in raw_aff_steps} if isinstance(raw_aff_steps, (list, tuple, set)) else set()
                raw_aff_files = item.get("affected_files")
                aff_files = {Path(f).as_posix().lower() for f in raw_aff_files} if isinstance(raw_aff_files, (list, tuple, set)) else set()

                is_global = not aff_steps and not aff_files
                applies = False
                if is_global:
                    applies = True
                else:
                    if step_id and _normalize_step_id(step_id) in aff_steps:
                        applies = True
                    if step_files and aff_files:
                        is_file_match = bool(step_files & aff_files) or any(
                            any(
                                sf == af
                                or sf.startswith(f"{af.rstrip('/')}/")
                                or fnmatch.fnmatch(sf, af)
                                for af in aff_files
                            )
                            for sf in step_files
                        )
                        if is_file_match:
                            applies = True
                if applies:
                    resolved_clarifications.append({
                        "id": uid,
                        "decision": decision,
                        "question": str(item.get("question") or decision).strip(),
                        "answer": clean_answer,
                        "requirement": f"[{uid}] {decision}: {clean_answer}",
                    })

        return {
            "target_context": self.target_context,
            "architecture_strategy": self.architecture_strategy,
            "relevant_constraints": relevant_constraints,
            "behavior_invariants": list(self.behavior_invariants),
            "compatibility_contracts": list(self.compatibility_contracts),
            "non_scope": list(self.non_scope),
            "migration_constraints": list(self.migration_constraints),
            "resolved_clarifications": resolved_clarifications,
        }

    def to_metadata_dict(self) -> Dict[str, Any]:
        """Convert document state into companion metadata sidecar dictionary."""
        return {
            "id": self.id,
            "title": self.title,
            "goal": self.goal,
            "status": self.status.value,
            "revision": self.revision,
            "acceptance_criteria": list(self.acceptance_criteria),
            "assumptions": list(self.assumptions),
            "steps": list(self.steps),
            "relevant_files": list(self.relevant_files),
            "verification_commands": list(self.verification_commands),
            "commit_hash": self.commit_hash,
            "git_status_snapshot": self.git_status_snapshot,
            "file_hashes": dict(self.file_hashes),
            "created_at": self.created_at,
            "updated_at": self.updated_at or self.created_at,
            "session_id": self.session_id,
            "transitions": list(self.transitions),
            "verification_history": list(self.verification_history),
            "failure_history": list(self.failure_history),
            "step_progress": list(self.step_progress),
            "content_hash": self.content_hash,
            "metadata_hash": self.metadata_hash,
            "target_context": self.target_context,
            "architecture_strategy": self.architecture_strategy,
            "scope": list(self.scope),
            "non_scope": list(self.non_scope),
            "clarifying_questions": list(self.clarifying_questions),
            "blocking_unknowns": list(self.blocking_unknowns),
            "architectural_constraints": list(self.architectural_constraints),
            "compatibility_contracts": list(self.compatibility_contracts),
            "behavior_invariants": list(self.behavior_invariants),
            "migration_constraints": list(self.migration_constraints),
        }


def is_valid_session_id(val: Any) -> bool:
    """Return True if value is a valid non-empty session identifier string."""
    return isinstance(val, str) and bool(val.strip())


@dataclass(frozen=True)
class PlanSummary:
    """Lightweight plan summary without loading full markdown content."""

    id: str
    title: str
    status: PlanStatus
    created_at: str
    updated_at: str
    filename: str
    path: Path
    goal: str = ""
    steps_count: int = 0
    metadata_missing: bool = False
    metadata_malformed: bool = False
    content_hash: str = ""
    content_tampered: bool = False


@dataclass(frozen=True)
class PlanSelectionResult:
    """Outcome of a deterministic plan selection operation."""

    selected: Optional[PlanDocument] = None
    candidates: Tuple[PlanSummary, ...] = ()
    status: str = "selected"  # "selected", "ambiguous", "none_found", "rejected"
    message: str = ""


def get_plans_dir(repo_dir: Path | str) -> Path:
    """Return the absolute path to the repository's plan directory."""
    return (Path(repo_dir) / PLANS_DIR_REL).resolve()


def get_journal_dir(repo_dir: Path | str) -> Path:
    """Return the absolute path to the repository's plan journal directory."""
    j_dir = get_plans_dir(repo_dir) / ".journal"
    j_dir.mkdir(parents=True, exist_ok=True)
    return j_dir


def recover_plan_journal(
    repo_dir: Path | str,
    plan_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Deterministically recover and reconcile incomplete transactions from WAL journal files.

    Invariants:
    1. Cross-process lock during recovery.
    2. Idempotent: repeated recovery invocations leave state identical.
    3. Fail-closed: If a journal is corrupt/unreadable, quarantine it; do not assume success.
    4. Two-phase evaluation:
       - Confirmed committed: if target .meta.json and .md both exist and match target_revision,
         target_status, and new_md_hash, write was complete before crash. Clean temp files and remove journal.
       - Interrupted / partial write:
         - If previous_meta_dict exists: roll back .md and .meta.json to previous snapshot.
         - If previous_meta_dict is None (interrupted creation): delete partial files.
         - Never overwrite a newer concurrent update (if on-disk revision > target_revision).
         - Never promote an incomplete plan to READY, IMPLEMENTED, or VERIFIED.
    """
    plans_dir = get_plans_dir(repo_dir)
    journal_dir = plans_dir / ".journal"
    if not journal_dir.exists():
        return []

    locks_dir = plans_dir / ".locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    from core.runtime.session import _get_cross_process_session_lock
    recovery_lock = _get_cross_process_session_lock(locks_dir / "journal_recovery.lock")

    actions_taken: List[Dict[str, Any]] = []

    with recovery_lock:
        journal_files = sorted(journal_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for j_path in journal_files:
            if j_path.name.startswith(".tmp_"):
                try:
                    j_path.unlink()
                except Exception:
                    pass
                continue

            try:
                raw_data = json.loads(j_path.read_text(encoding="utf-8"))
                entry = PlanJournalEntry.from_dict(raw_data)
            except Exception as parse_err:
                logger.error("Failed to parse journal file '%s': %s", j_path, parse_err)
                corrupt_path = j_path.with_suffix(".json.corrupt")
                try:
                    os.replace(j_path, corrupt_path)
                except Exception:
                    pass
                actions_taken.append({
                    "action": "quarantined_corrupt",
                    "path": str(j_path),
                    "error": str(parse_err),
                })
                continue

            if plan_id and entry.plan_id != plan_id:
                continue

            target_md = plans_dir / entry.filename
            target_meta = target_md.with_suffix(".meta.json")

            # Check if target state was already reached
            is_committed = False
            if target_meta.exists():
                try:
                    curr_meta = json.loads(target_meta.read_text(encoding="utf-8"))
                    curr_rev = int(curr_meta.get("revision", 0))
                    curr_status = str(curr_meta.get("status", ""))
                    if curr_rev > entry.target_revision:
                        # Newer concurrent update already completed: discard obsolete journal
                        j_path.unlink(missing_ok=True)
                        actions_taken.append({
                            "action": "discarded_stale_journal",
                            "plan_id": entry.plan_id,
                            "journal_id": entry.journal_id,
                        })
                        continue

                    if curr_rev == entry.target_revision:
                        if curr_status == entry.target_status and (
                            not entry.has_md_update or (target_md.exists() and compute_file_hash(target_md) == entry.new_md_hash)
                        ):
                            is_committed = True
                        else:
                            # Concurrent update reached target revision with different status or content.
                            # Do NOT overwrite newer concurrent state with previous snapshot!
                            j_path.unlink(missing_ok=True)
                            actions_taken.append({
                                "action": "discarded_conflicting_journal",
                                "plan_id": entry.plan_id,
                                "journal_id": entry.journal_id,
                            })
                            continue
                except Exception:
                    pass

            if is_committed:
                for tmp_f in plans_dir.glob(f".tmp_{target_md.stem}*"):
                    try:
                        tmp_f.unlink()
                    except Exception:
                        pass
                _trigger_wal_fault("before_recovery_commit_cleanup", {"journal_path": j_path, "plan_id": entry.plan_id})
                j_path.unlink(missing_ok=True)
                actions_taken.append({
                    "action": "confirmed_committed",
                    "plan_id": entry.plan_id,
                    "journal_id": entry.journal_id,
                })
            else:
                # Incomplete / interrupted write: roll back or abort creation
                if entry.previous_meta_dict:
                    temp_meta = plans_dir / f".tmp_recovery_{uuid.uuid4().hex[:8]}.json"
                    try:
                        with open(temp_meta, "w", encoding="utf-8") as f:
                            json.dump(entry.previous_meta_dict, f, indent=2, ensure_ascii=False)
                            f.flush()
                            try:
                                os.fsync(f.fileno())
                            except Exception:
                                pass
                        _trigger_wal_fault("before_recovery_rollback_meta", {"target_meta": target_meta, "plan_id": entry.plan_id})
                        if target_meta.exists():
                            try:
                                latest_m = json.loads(target_meta.read_text(encoding="utf-8"))
                                latest_rev = int(latest_m.get("revision", 0))
                                if latest_rev >= entry.target_revision:
                                    j_path.unlink(missing_ok=True)
                                    actions_taken.append({
                                        "action": "discarded_conflicting_journal",
                                        "plan_id": entry.plan_id,
                                        "journal_id": entry.journal_id,
                                    })
                                    continue
                            except Exception:
                                pass
                        os.replace(temp_meta, target_meta)
                        _trigger_wal_fault("during_recovery_rollback_meta", {"target_meta": target_meta, "plan_id": entry.plan_id})
                    finally:
                        if temp_meta.exists():
                            try:
                                temp_meta.unlink()
                            except Exception:
                                pass

                    if entry.has_md_update and entry.previous_md_content is not None:
                        # Protect external modifications to Markdown from being clobbered
                        current_disk_md = target_md.read_text(encoding="utf-8", errors="replace") if target_md.exists() else None
                        is_external_mod = (
                            current_disk_md is not None
                            and current_disk_md != entry.previous_md_content
                            and current_disk_md != (entry.new_md_content or "")
                        )
                        if is_external_mod:
                            actions_taken.append({
                                "action": "preserved_external_modification",
                                "plan_id": entry.plan_id,
                                "path": str(target_md),
                            })
                        else:
                            temp_md = plans_dir / f".tmp_recovery_{uuid.uuid4().hex[:8]}.md"
                            try:
                                with open(temp_md, "w", encoding="utf-8") as f:
                                    f.write(entry.previous_md_content)
                                    f.flush()
                                    try:
                                        os.fsync(f.fileno())
                                    except Exception:
                                        pass
                                os.replace(temp_md, target_md)
                                _trigger_wal_fault("during_recovery_rollback_md", {"target_md": target_md, "plan_id": entry.plan_id})
                            finally:
                                if temp_md.exists():
                                    try:
                                        temp_md.unlink()
                                    except Exception:
                                        pass

                    for tmp_f in plans_dir.glob(f".tmp_{target_md.stem}*"):
                        try:
                            tmp_f.unlink()
                        except Exception:
                            pass

                    _trigger_wal_fault("before_recovery_rollback_cleanup", {"journal_path": j_path, "plan_id": entry.plan_id})
                    j_path.unlink(missing_ok=True)
                    actions_taken.append({
                        "action": "rolled_back",
                        "plan_id": entry.plan_id,
                        "journal_id": entry.journal_id,
                        "restored_revision": entry.previous_meta_dict.get("revision"),
                    })
                else:
                    is_concurrent_plan = False
                    if target_meta.exists():
                        try:
                            curr_m = json.loads(target_meta.read_text(encoding="utf-8"))
                            if curr_m.get("id") != entry.plan_id or int(curr_m.get("revision", 1)) > entry.target_revision:
                                is_concurrent_plan = True
                        except Exception:
                            pass
                    if not is_concurrent_plan:
                        if target_md.exists():
                            disk_md = target_md.read_text(encoding="utf-8", errors="replace")
                            if disk_md == (entry.new_md_content or ""):
                                target_md.unlink(missing_ok=True)
                        if target_meta.exists():
                            target_meta.unlink(missing_ok=True)
                    for tmp_f in plans_dir.glob(f".tmp_{target_md.stem}*"):
                        try:
                            tmp_f.unlink()
                        except Exception:
                            pass
                    _trigger_wal_fault("before_recovery_abort_cleanup", {"journal_path": j_path, "plan_id": entry.plan_id})
                    j_path.unlink(missing_ok=True)
                    actions_taken.append({
                        "action": "aborted_creation",
                        "plan_id": entry.plan_id,
                        "journal_id": entry.journal_id,
                    })

    return actions_taken


def save_plan_document(
    repo_dir: Path,
    filename: str,
    content: str,
    metadata: Optional[Dict[str, Any]] = None,
    expected_revision: Optional[int] = None,
) -> Path:
    """Save a plan document into .brainfrog/plans/ with strict sandbox validation and WAL journal.

    Security checks enforced:
    - Path traversal attempts (../) are rejected.
    - Absolute paths outside .brainfrog/plans/ are rejected.
    - Symlinks pointing outside .brainfrog/plans/ are rejected.
    - Non-markdown/non-json extensions are rejected.
    - Companion metadata is enriched with deterministic lifecycle fields if absent.
    - Paired .md and .meta.json writes are protected by WAL journal.
    """
    plans_dir = get_plans_dir(repo_dir)
    plans_dir.mkdir(parents=True, exist_ok=True)
    journal_dir = get_journal_dir(repo_dir)

    allowed_exts = {".md", ".json", ".txt"}
    raw_path = Path(filename)
    if raw_path.suffix.lower() not in allowed_exts:
        raise PermissionError(
            f"Ekstensi file rencana tidak valid: '{raw_path.suffix}'. Hanya {allowed_exts} yang diizinkan."
        )

    # Check for symlink traversal before full resolve
    target_candidate = raw_path if raw_path.is_absolute() else (plans_dir / raw_path)
    curr = target_candidate
    while curr != plans_dir and curr != curr.parent:
        if curr.is_symlink():
            try:
                curr.resolve().relative_to(plans_dir)
            except ValueError:
                raise PermissionError(
                    f"Symlink traversal terdeteksi: symlink '{curr}' mengarah ke luar direktori rencana."
                )
        curr = curr.parent

    # Construct and resolve target path
    resolved_target = target_candidate.resolve()

    # Verify target stays inside plans_dir
    try:
        resolved_target.relative_to(plans_dir)
    except ValueError:
        raise PermissionError(
            f"Path traversal terdeteksi: path '{filename}' mengarah ke luar direktori rencana ({plans_dir})."
        )

    plan_stem = resolved_target.stem
    meta_filename = plan_stem + ".meta.json"
    meta_path = resolved_target.parent / meta_filename

    locks_dir = plans_dir / ".locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    from core.runtime.session import _get_cross_process_session_lock

    # Build companion metadata & resolve plan_id for locking
    meta_dict: Dict[str, Any] = dict(metadata or {})
    plan_id = meta_dict.get("id") or meta_dict.get("plan_id") or f"plan_{plan_stem}"

    plan_lock = _get_cross_process_session_lock(locks_dir / f"plan_{plan_id}.lock")
    with plan_lock:
        # Capture previous state if updating existing document under plan lock
        previous_md_content: Optional[str] = None
        previous_meta_dict: Optional[Dict[str, Any]] = None
        if resolved_target.exists():
            try:
                previous_md_content = resolved_target.read_text(encoding="utf-8")
            except Exception:
                pass
        if meta_path.exists():
            try:
                previous_meta_dict = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        if not meta_dict.get("id") and previous_meta_dict and previous_meta_dict.get("id"):
            plan_id = previous_meta_dict["id"]
        meta_dict["id"] = plan_id

        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        meta_dict["content_hash"] = content_hash

        now_iso = datetime.now(timezone.utc).isoformat()
        if "status" not in meta_dict:
            meta_dict["status"] = (
                previous_meta_dict.get("status", PlanStatus.DRAFT.value)
                if previous_meta_dict else PlanStatus.DRAFT.value
            )
        # Determine expected revision for Compare-And-Swap (CAS)
        expected_rev = (
            expected_revision
            if expected_revision is not None
            else meta_dict.get("expected_revision")
        )
        if expected_rev is None and "base_revision" in meta_dict:
            expected_rev = meta_dict.get("base_revision")

        disk_rev = int(previous_meta_dict.get("revision", 1)) if (previous_meta_dict and "revision" in previous_meta_dict) else None

        if previous_meta_dict is not None and disk_rev is not None:
            # 1. True Compare-And-Swap: if expected base revision is provided, disk MUST match exactly
            if expected_rev is not None and disk_rev != int(expected_rev):
                raise StalePlanStateError(
                    f"Plan '{plan_id}' revision conflict (CAS mismatch): expected revision {expected_rev}, "
                    f"but current disk revision is {disk_rev}."
                )
            # 2. Strict sequential revision check: if target revision is specified, it must be exactly disk_rev + 1
            if "revision" in meta_dict:
                target_rev = int(meta_dict["revision"])
                if target_rev <= disk_rev:
                    raise StalePlanStateError(
                        f"Plan '{plan_id}' revision conflict: current revision is {disk_rev}, "
                        f"cannot write revision {target_rev}."
                    )
                if expected_rev is not None and target_rev != int(expected_rev) + 1:
                    raise StalePlanStateError(
                        f"Plan '{plan_id}' revision conflict: target revision {target_rev} "
                        f"must be exactly base revision {expected_rev} + 1."
                    )
                if expected_rev is None and target_rev != disk_rev + 1:
                    raise StalePlanStateError(
                        f"Plan '{plan_id}' revision jump conflict: current revision is {disk_rev}, "
                        f"cannot write non-sequential revision {target_rev}."
                    )
            else:
                meta_dict["revision"] = disk_rev + 1
        elif "revision" not in meta_dict:
            meta_dict["revision"] = 1

        if "session_id" not in meta_dict and previous_meta_dict and previous_meta_dict.get("session_id"):
            meta_dict["session_id"] = previous_meta_dict["session_id"]
        elif "session_id" in meta_dict:
            raw_s = meta_dict["session_id"]
            meta_dict["session_id"] = raw_s.strip() if (isinstance(raw_s, str) and raw_s.strip()) else ""
        if "created_at" not in meta_dict:
            meta_dict["created_at"] = (
                previous_meta_dict.get("created_at", now_iso)
                if previous_meta_dict else now_iso
            )
        if "updated_at" not in meta_dict:
            meta_dict["updated_at"] = now_iso
        if "transitions" not in meta_dict:
            if previous_meta_dict and "transitions" in previous_meta_dict:
                meta_dict["transitions"] = list(previous_meta_dict["transitions"])
            else:
                meta_dict["transitions"] = [
                    {
                        "from_status": None,
                        "to_status": meta_dict["status"],
                        "timestamp": meta_dict["created_at"],
                        "actor": meta_dict.get("actor", "system"),
                        "reason": "Initial plan document creation",
                        "evidence": None,
                    }
                ]
        if "verification_history" not in meta_dict:
            meta_dict["verification_history"] = list(previous_meta_dict.get("verification_history", [])) if previous_meta_dict else []
        if "failure_history" not in meta_dict:
            meta_dict["failure_history"] = list(previous_meta_dict.get("failure_history", [])) if previous_meta_dict else []

        meta_dict["metadata_hash"] = compute_metadata_semantic_digest(meta_dict)

        # Write-Ahead Journal record
        journal_id = f"wal_{plan_id}_{int(datetime.now(timezone.utc).timestamp() * 1000)}_{uuid.uuid4().hex[:6]}"
        journal_entry = PlanJournalEntry(
            journal_id=journal_id,
            plan_id=plan_id,
            filename=resolved_target.name,
            stage="PREPARED",
            created_at=now_iso,
            expected_revision=previous_meta_dict.get("revision") if previous_meta_dict else None,
            target_revision=meta_dict["revision"],
            target_status=meta_dict["status"],
            new_md_hash=content_hash,
            new_meta_dict=meta_dict,
            has_md_update=True,
            new_md_content=content,
            previous_md_content=previous_md_content,
            previous_meta_dict=previous_meta_dict,
        )
        journal_path = journal_dir / f"{journal_id}.json"
        temp_journal = journal_dir / f".tmp_{journal_id}.json"

        recovery_lock = _get_cross_process_session_lock(locks_dir / "journal_recovery.lock")
        with recovery_lock:
            # Step 1: Write journal atomically
            try:
                with open(temp_journal, "w", encoding="utf-8") as f:
                    json.dump(journal_entry.to_dict(), f, indent=2, ensure_ascii=False)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except Exception:
                        pass
                os.replace(temp_journal, journal_path)
                _trigger_wal_fault("after_journal_publish", {"journal_path": journal_path, "plan_id": plan_id})
            finally:
                if temp_journal.exists():
                    try:
                        temp_journal.unlink()
                    except Exception:
                        pass

            # Step 2: Atomic write for markdown document
            temp_md = resolved_target.parent / f".tmp_{plan_stem}_{uuid.uuid4().hex[:8]}.md"
            try:
                with open(temp_md, "w", encoding="utf-8", newline="") as f:
                    f.write(content)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except Exception:
                        pass
                os.replace(temp_md, resolved_target)
                _trigger_wal_fault("after_md_replace", {"target_md": resolved_target, "plan_id": plan_id})
            finally:
                if temp_md.exists():
                    try:
                        temp_md.unlink()
                    except Exception:
                        pass

            # Step 3: Atomic write for metadata sidecar
            temp_meta = resolved_target.parent / f".tmp_{plan_stem}_{uuid.uuid4().hex[:8]}.json"
            try:
                with open(temp_meta, "w", encoding="utf-8") as f:
                    json.dump(meta_dict, f, indent=2, ensure_ascii=False)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except Exception:
                        pass
                os.replace(temp_meta, meta_path)
                _trigger_wal_fault("after_meta_replace", {"meta_path": meta_path, "plan_id": plan_id})
            finally:
                if temp_meta.exists():
                    try:
                        temp_meta.unlink()
                    except Exception:
                        pass

            # Step 4: Purge journal entry upon complete commit
            _trigger_wal_fault("before_journal_cleanup", {"journal_path": journal_path, "plan_id": plan_id})
            journal_path.unlink(missing_ok=True)

    return resolved_target


def compute_file_hash(file_path: Path) -> str:
    """Compute sha256 checksum of a file if it exists."""
    if not file_path.exists() or not file_path.is_file():
        return ""
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
        return hashlib.sha256(content.encode("utf-8")).hexdigest()
    except Exception:
        return ""


def get_latest_plan(repo_dir: Path) -> Optional[PlanDocument]:
    """Retrieve the most recent plan document from .brainfrog/plans/."""
    plans_dir = get_plans_dir(repo_dir)
    if not plans_dir.exists():
        return None

    md_files = [f for f in plans_dir.glob("*.md") if f.is_file()]
    if not md_files:
        return None

    # Sort by mtime descending
    md_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    latest_file = md_files[0]

    # Check for companion .meta.json
    meta_path = latest_file.with_suffix(".meta.json")
    metadata: Dict[str, Any] = {}
    metadata_missing = not meta_path.exists()
    metadata_malformed = False
    if meta_path.exists():
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            if not isinstance(metadata, dict):
                metadata = {}
                metadata_malformed = True
        except Exception:
            metadata = {}
            metadata_malformed = True

    content = latest_file.read_text(encoding="utf-8", errors="replace")
    disk_content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    rec_content_hash = metadata.get("content_hash", "")
    content_tampered = bool(rec_content_hash and rec_content_hash != disk_content_hash)

    # Extract goal and title from content if missing in metadata
    title = metadata.get("title") or latest_file.stem.replace("_", " ").title()
    goal = metadata.get("goal", "")
    if not goal:
        for line in content.splitlines():
            line_str = line.strip()
            if line_str.startswith("# "):
                title = line_str[2:].strip()
            elif line_str.lower().startswith(("- **goal", "- **tujuan", "goal:", "tujuan:")):
                goal = line_str.split(":", 1)[1].strip()
                break

    raw_status = metadata.get("status", PlanStatus.DRAFT.value)
    try:
        plan_status = PlanStatus(raw_status)
    except ValueError:
        plan_status = PlanStatus.DRAFT
        metadata_malformed = True

    if content_tampered and plan_status == PlanStatus.VERIFIED:
        plan_status = PlanStatus.DRAFT

    recorded_meta_hash = metadata.get("metadata_hash", "")
    metadata_tampered = False
    if recorded_meta_hash:
        computed_meta_hash = compute_metadata_semantic_digest(metadata)
        if computed_meta_hash != recorded_meta_hash:
            metadata_tampered = True

    raw_blocking = metadata.get("blocking_unknowns")
    if raw_blocking is not None:
        if not isinstance(raw_blocking, (list, tuple, set)):
            metadata_malformed = True
        elif any(not isinstance(x, (str, dict)) for x in raw_blocking):
            metadata_malformed = True
        elif any(isinstance(x, dict) and not any(str(x.get(k) or "").strip() for k in ("decision", "question", "missing_decision", "description")) for x in raw_blocking):
            metadata_malformed = True
    raw_arch = metadata.get("architectural_constraints")
    if raw_arch is not None and not isinstance(raw_arch, (list, tuple, set)):
        metadata_malformed = True
    raw_scope = metadata.get("scope")
    if raw_scope is not None and not isinstance(raw_scope, (list, tuple, set)):
        metadata_malformed = True

    plan_id = metadata.get("id") or metadata.get("plan_id") or f"plan_{latest_file.stem}"

    return PlanDocument(
        filename=latest_file.name,
        path=latest_file,
        title=title,
        goal=goal or title,
        acceptance_criteria=metadata.get("acceptance_criteria", []),
        assumptions=metadata.get("assumptions", []),
        steps=metadata.get("steps", []),
        relevant_files=metadata.get("relevant_files", []),
        commit_hash=metadata.get("commit_hash", ""),
        git_status_snapshot=metadata.get("git_status_snapshot", ""),
        file_hashes=metadata.get("file_hashes", {}),
        created_at=metadata.get("created_at", ""),
        content=content,
        verification_commands=(
            metadata.get("verification_commands", [])
            if isinstance(metadata.get("verification_commands"), list)
            else ([metadata["verification_commands"]] if metadata.get("verification_commands") else [])
        ),
        id=plan_id,
        status=plan_status,
        revision=int(metadata.get("revision", 1)),
        updated_at=metadata.get("updated_at", metadata.get("created_at", "")),
        transitions=metadata.get("transitions", []),
        verification_history=metadata.get("verification_history", []),
        failure_history=metadata.get("failure_history", []),
        metadata_missing=metadata_missing,
        metadata_malformed=metadata_malformed,
        content_hash=disk_content_hash,
        content_tampered=content_tampered,
        metadata_hash=recorded_meta_hash or compute_metadata_semantic_digest(metadata),
        metadata_tampered=metadata_tampered,
        target_context=metadata.get("target_context"),
        architecture_strategy=metadata.get("architecture_strategy"),
        scope=_safe_collection_str(metadata.get("scope")),
        non_scope=_safe_collection_str(metadata.get("non_scope")),
        clarifying_questions=_safe_collection_str(metadata.get("clarifying_questions")),
        blocking_unknowns=_safe_collection(metadata.get("blocking_unknowns")),
        architectural_constraints=_safe_collection(metadata.get("architectural_constraints")),
        compatibility_contracts=_safe_collection_str(metadata.get("compatibility_contracts")),
        behavior_invariants=_safe_collection_str(metadata.get("behavior_invariants")),
        migration_constraints=_safe_collection_str(metadata.get("migration_constraints")),
    )



# -------------------------------------------------------------------------
# Plan Registry (Phase 2A)
# -------------------------------------------------------------------------
class PlanRegistry:
    """Deterministic, recoverable Plan Registry scoped to a workspace.

    The single canonical source of truth is the plan directory (.brainfrog/plans/),
    specifically the companion metadata sidecars (<stem>.meta.json) and plan markdown files.
    """

    def __init__(self, repo_dir: Path | str) -> None:
        self.repo_dir = Path(repo_dir).resolve()
        self.plans_dir = get_plans_dir(self.repo_dir)
        self.locks_dir = self.plans_dir / ".locks"
        recover_plan_journal(self.repo_dir)

    def _get_lock(self, name: str = "registry") -> Any:
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        from core.runtime.session import _get_cross_process_session_lock
        return _get_cross_process_session_lock(self.locks_dir / f"{name}.lock")

    def list_plans(self, status_filter: Optional[PlanStatus | str] = None) -> List[PlanSummary]:
        """List all plans in the workspace without loading full markdown content.

        Sorted by last-updated timestamp descending, with deterministic tie-breaking on ID.
        """
        recover_plan_journal(self.repo_dir)
        if not self.plans_dir.exists():
            return []

        norm_filter: Optional[PlanStatus] = None
        if status_filter:
            try:
                norm_filter = status_filter if isinstance(status_filter, PlanStatus) else PlanStatus(status_filter)
            except ValueError:
                norm_filter = None

        summaries: List[PlanSummary] = []
        md_files = [f for f in self.plans_dir.glob("*.md") if f.is_file() and not f.name.startswith(".tmp_")]

        for md_path in md_files:
            meta_path = md_path.with_suffix(".meta.json")
            metadata_missing = not meta_path.exists()
            metadata_malformed = False
            meta_data: Dict[str, Any] = {}

            if meta_path.exists():
                try:
                    meta_text = meta_path.read_text(encoding="utf-8")
                    parsed = json.loads(meta_text)
                    if isinstance(parsed, dict):
                        meta_data = parsed
                    else:
                        metadata_malformed = True
                except Exception:
                    metadata_malformed = True

            plan_id = str(meta_data.get("id") or meta_data.get("plan_id") or f"plan_{md_path.stem}")
            title = str(meta_data.get("title") or md_path.stem.replace("_", " ").title())
            goal = str(meta_data.get("goal") or "")

            raw_status = meta_data.get("status", PlanStatus.DRAFT.value)
            try:
                status = PlanStatus(raw_status)
            except ValueError:
                status = PlanStatus.DRAFT
                metadata_malformed = True

            created_at = str(meta_data.get("created_at") or "")
            if not created_at:
                try:
                    stat_res = md_path.stat()
                    stat_birth = getattr(stat_res, "st_birthtime", None)
                    stat_time = stat_birth if stat_birth is not None else stat_res.st_ctime
                    created_at = datetime.fromtimestamp(stat_time, tz=timezone.utc).isoformat()
                except Exception:
                    created_at = ""

            updated_at = str(meta_data.get("updated_at") or created_at)
            if not updated_at:
                try:
                    updated_at = datetime.fromtimestamp(md_path.stat().st_mtime, tz=timezone.utc).isoformat()
                except Exception:
                    updated_at = created_at

            raw_steps = meta_data.get("steps", [])
            steps_count = len(raw_steps) if isinstance(raw_steps, list) else 0

            rec_content_hash = meta_data.get("content_hash", "")
            content_tampered = False
            if status == PlanStatus.VERIFIED:
                disk_hash = compute_file_hash(md_path)
                if not rec_content_hash or rec_content_hash != disk_hash:
                    content_tampered = True
                    status = PlanStatus.DRAFT
            elif rec_content_hash:
                disk_hash = compute_file_hash(md_path)
                if rec_content_hash != disk_hash:
                    content_tampered = True

            summary = PlanSummary(
                id=plan_id,
                title=title,
                status=status,
                created_at=created_at,
                updated_at=updated_at,
                filename=md_path.name,
                path=md_path,
                goal=goal,
                steps_count=steps_count,
                metadata_missing=metadata_missing,
                metadata_malformed=metadata_malformed,
                content_hash=rec_content_hash,
                content_tampered=content_tampered,
            )

            if norm_filter is not None and summary.status != norm_filter:
                continue

            summaries.append(summary)

        # Deterministic sort: updated_at descending, tie-break by ID ascending
        summaries.sort(key=lambda s: (s.updated_at or s.created_at or "", s.id), reverse=True)
        return summaries

    def _validate_safe_id(self, plan_id: str) -> None:
        """Reject path traversal or illegal identifier patterns."""
        if not plan_id or not isinstance(plan_id, str):
            raise PlanNotFoundError("Plan ID must be a non-empty string.")
        if ".." in plan_id or "/" in plan_id or "\\" in plan_id:
            raise PermissionError(f"Path traversal rejected in plan ID: '{plan_id}'")

    def get_plan(self, plan_id: str) -> Optional[PlanDocument]:
        """Retrieve full plan document by stable ID or filename stem."""
        self._validate_safe_id(plan_id)
        with self._get_lock(f"plan_{plan_id}"):
            recover_plan_journal(self.repo_dir, plan_id=plan_id)
            if not self.plans_dir.exists():
                return None

            # 1. Exact stem match fast path
            stem_candidates = [plan_id]
            if plan_id.startswith("plan_"):
                stem_candidates.append(plan_id[5:])
            else:
                stem_candidates.append(f"plan_{plan_id}")

            for stem in stem_candidates:
                md_path = self.plans_dir / f"{stem}.md"
                if md_path.exists() and md_path.is_file():
                    return self._load_plan_document(md_path)

            # 2. Search metadata sidecars for explicit id
            for meta_file in self.plans_dir.glob("*.meta.json"):
                if meta_file.name.startswith(".tmp_"):
                    continue
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    if isinstance(meta, dict):
                        if meta.get("id") == plan_id or meta.get("plan_id") == plan_id:
                            corresponding_md = meta_file.with_name(meta_file.stem[:-5] + ".md" if meta_file.stem.endswith(".meta") else meta_file.stem + ".md")
                            if corresponding_md.exists():
                                return self._load_plan_document(corresponding_md)
                except Exception:
                    pass

            return None

    def get_plan_by_filename(self, filename: str) -> Optional[PlanDocument]:
        """Retrieve full plan document by filename within .brainfrog/plans/."""
        raw_p = Path(filename)
        if raw_p.is_absolute():
            try:
                resolved = raw_p.resolve()
                resolved.relative_to(self.plans_dir)
                target = resolved
            except ValueError:
                raise PermissionError(f"Path traversal rejected: '{filename}' outside plans directory.")
        else:
            target = (self.plans_dir / filename).resolve()
            try:
                target.relative_to(self.plans_dir)
            except ValueError:
                raise PermissionError(f"Path traversal rejected: '{filename}' outside plans directory.")

        if not target.exists() or not target.is_file() or target.suffix.lower() != ".md":
            return None
        return self._load_plan_document(target)

    def get_plan_by_title(self, title: str) -> Optional[PlanDocument]:
        """Retrieve a plan by exact unambiguous title match."""
        clean_t = title.strip().lower()
        if not clean_t:
            return None
        summaries = self.list_plans()
        matches = [s for s in summaries if s.title.strip().lower() == clean_t]
        if len(matches) == 1:
            return self.get_plan(matches[0].id)
        if len(matches) > 1:
            raise AmbiguousPlanError(f"Multiple plans match title '{title}'. Use plan ID instead.")
        return None

    def _load_plan_document(self, md_path: Path) -> PlanDocument:
        """Internal helper to load PlanDocument with metadata sidecar reconciliation."""
        meta_path = md_path.with_suffix(".meta.json")
        metadata_missing = not meta_path.exists()
        metadata_malformed = False
        meta: Dict[str, Any] = {}

        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if not isinstance(meta, dict):
                    meta = {}
                    metadata_malformed = True
            except Exception:
                meta = {}
                metadata_malformed = True

        content = md_path.read_text(encoding="utf-8", errors="replace")
        disk_content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        recorded_content_hash = meta.get("content_hash", "")
        content_tampered = bool(recorded_content_hash and recorded_content_hash != disk_content_hash)

        recorded_meta_hash = meta.get("metadata_hash", "")
        metadata_tampered = False
        if recorded_meta_hash:
            computed_meta_hash = compute_metadata_semantic_digest(meta)
            if computed_meta_hash != recorded_meta_hash:
                metadata_tampered = True

        raw_blocking = meta.get("blocking_unknowns")
        if raw_blocking is not None:
            if not isinstance(raw_blocking, (list, tuple, set)):
                metadata_malformed = True
            elif any(not isinstance(x, (str, dict)) for x in raw_blocking):
                metadata_malformed = True
            elif any(isinstance(x, dict) and not any(str(x.get(k) or "").strip() for k in ("decision", "question", "missing_decision", "description")) for x in raw_blocking):
                metadata_malformed = True
        raw_arch = meta.get("architectural_constraints")
        if raw_arch is not None and not isinstance(raw_arch, (list, tuple, set)):
            metadata_malformed = True
        raw_scope = meta.get("scope")
        if raw_scope is not None and not isinstance(raw_scope, (list, tuple, set)):
            metadata_malformed = True

        # Extract title and goal from markdown if missing in metadata
        title = meta.get("title") or md_path.stem.replace("_", " ").title()
        goal = meta.get("goal", "")
        if not goal:
            for line in content.splitlines():
                line_str = line.strip()
                if line_str.startswith("# "):
                    title = line_str[2:].strip()
                elif line_str.lower().startswith(("- **goal", "- **tujuan", "goal:", "tujuan:")):
                    goal = line_str.split(":", 1)[1].strip()
                    break

        raw_status = meta.get("status", PlanStatus.DRAFT.value)
        try:
            plan_status = PlanStatus(raw_status)
        except ValueError:
            plan_status = PlanStatus.DRAFT
            metadata_malformed = True

        if content_tampered and plan_status == PlanStatus.VERIFIED:
            plan_status = PlanStatus.DRAFT

        step_progress: List[Dict[str, Any]] = meta.get("step_progress", [])
        prog_path = md_path.with_suffix(".progress.json")
        if prog_path.exists():
            try:
                p_data = json.loads(prog_path.read_text(encoding="utf-8"))
                if isinstance(p_data, dict) and isinstance(p_data.get("steps"), list):
                    step_progress = p_data["steps"]
            except Exception:
                pass

        plan_id = str(meta.get("id") or meta.get("plan_id") or f"plan_{md_path.stem}")

        return PlanDocument(
            filename=md_path.name,
            path=md_path,
            title=title,
            goal=goal or title,
            acceptance_criteria=meta.get("acceptance_criteria", []),
            assumptions=meta.get("assumptions", []),
            steps=meta.get("steps", []),
            relevant_files=meta.get("relevant_files", []),
            commit_hash=meta.get("commit_hash", ""),
            git_status_snapshot=meta.get("git_status_snapshot", ""),
            file_hashes=meta.get("file_hashes", {}),
            created_at=meta.get("created_at", ""),
            content=content,
            verification_commands=(
                meta.get("verification_commands", [])
                if isinstance(meta.get("verification_commands"), list)
                else ([meta["verification_commands"]] if meta.get("verification_commands") else [])
            ),
            id=plan_id,
            session_id=(
                meta["session_id"].strip()
                if (isinstance(meta.get("session_id"), str) and meta["session_id"].strip())
                else ""
            ),
            status=plan_status,
            revision=int(meta.get("revision", 1)),
            updated_at=meta.get("updated_at", meta.get("created_at", "")),
            transitions=meta.get("transitions", []),
            verification_history=meta.get("verification_history", []),
            failure_history=meta.get("failure_history", []),
            step_progress=step_progress,
            metadata_missing=metadata_missing,
            metadata_malformed=metadata_malformed,
            content_hash=disk_content_hash,
            content_tampered=content_tampered,
            metadata_hash=recorded_meta_hash or compute_metadata_semantic_digest(meta),
            metadata_tampered=metadata_tampered,
            target_context=meta.get("target_context"),
            architecture_strategy=meta.get("architecture_strategy"),
            scope=_safe_collection_str(meta.get("scope")),
            non_scope=_safe_collection_str(meta.get("non_scope")),
            clarifying_questions=_safe_collection_str(meta.get("clarifying_questions")),
            blocking_unknowns=_safe_collection(meta.get("blocking_unknowns")),
            architectural_constraints=_safe_collection(meta.get("architectural_constraints")),
            compatibility_contracts=_safe_collection_str(meta.get("compatibility_contracts")),
            behavior_invariants=_safe_collection_str(meta.get("behavior_invariants")),
            migration_constraints=_safe_collection_str(meta.get("migration_constraints")),
        )

    def update_status(
        self,
        plan_id: str,
        new_status: PlanStatus | str,
        actor: str,
        reason: str,
        evidence: Optional[Dict[str, Any]] = None,
        expected_revision: Optional[int] = None,
        contract: Optional[Any] = None,
    ) -> PlanDocument:
        """Transition a plan's lifecycle status through the canonical path with atomic locking and WAL."""
        self._validate_safe_id(plan_id)
        try:
            target_status = new_status if isinstance(new_status, PlanStatus) else PlanStatus(new_status)
        except ValueError:
            raise InvalidPlanTransitionError(f"Target status '{new_status}' is not a valid PlanStatus.")

        lock = self._get_lock(f"plan_{plan_id}")
        with lock:
            recover_plan_journal(self.repo_dir, plan_id=plan_id)
            plan = self.get_plan(plan_id)
            if plan is None:
                raise PlanNotFoundError(f"Plan '{plan_id}' does not exist in workspace '{self.repo_dir}'.")

            if expected_revision is not None and plan.revision != expected_revision:
                raise StalePlanStateError(
                    f"Stale plan revision for '{plan_id}': disk revision is {plan.revision}, expected {expected_revision}."
                )

            # Invariant: validate allowed transition, required evidence, and contract binding
            validate_plan_transition(
                current_status=plan.status,
                target_status=target_status,
                actor=actor,
                reason=reason,
                evidence=evidence,
                plan=plan,
                contract=contract,
                repo_dir=self.repo_dir,
            )

            previous_meta_dict = plan.to_metadata_dict()
            previous_revision = plan.revision

            now_iso = datetime.now(timezone.utc).isoformat()
            transition_record = {
                "from_status": plan.status.value,
                "to_status": target_status.value,
                "timestamp": now_iso,
                "actor": actor.strip(),
                "reason": reason.strip(),
                "evidence": evidence,
            }
            plan.transitions.append(transition_record)

            if target_status == PlanStatus.VERIFIED and evidence:
                plan.verification_history.append({
                    "timestamp": now_iso,
                    "actor": actor.strip(),
                    "reason": reason.strip(),
                    "evidence": evidence,
                })

            if target_status == PlanStatus.FAILED:
                plan.failure_history.append({
                    "timestamp": now_iso,
                    "actor": actor.strip(),
                    "reason": reason.strip(),
                    "evidence": evidence,
                })

            plan.status = target_status
            plan.revision += 1
            plan.updated_at = now_iso

            # Update content hash from disk to keep metadata in sync
            disk_content = plan.path.read_text(encoding="utf-8", errors="replace")
            plan.content_hash = hashlib.sha256(disk_content.encode("utf-8")).hexdigest()
            plan.content_tampered = False

            meta_dict = plan.to_metadata_dict()
            new_meta_hash = compute_metadata_semantic_digest(meta_dict)
            plan.metadata_hash = new_meta_hash
            plan.metadata_tampered = False
            meta_dict["metadata_hash"] = new_meta_hash
            meta_path = plan.path.with_suffix(".meta.json")

            # Write-Ahead Journal record
            journal_dir = get_journal_dir(self.repo_dir)
            journal_id = f"wal_{plan.id}_{int(datetime.now(timezone.utc).timestamp() * 1000)}_{uuid.uuid4().hex[:6]}"
            journal_entry = PlanJournalEntry(
                journal_id=journal_id,
                plan_id=plan.id,
                filename=plan.filename,
                stage="PREPARED",
                created_at=now_iso,
                expected_revision=previous_revision,
                target_revision=plan.revision,
                target_status=target_status.value,
                new_md_hash=plan.content_hash,
                new_meta_dict=meta_dict,
                has_md_update=False,
                previous_meta_dict=previous_meta_dict,
            )
            journal_path = journal_dir / f"{journal_id}.json"
            temp_journal = journal_dir / f".tmp_{journal_id}.json"

            with self._get_lock("journal_recovery"):
                try:
                    with open(temp_journal, "w", encoding="utf-8") as f:
                        json.dump(journal_entry.to_dict(), f, indent=2, ensure_ascii=False)
                        f.flush()
                        try:
                            os.fsync(f.fileno())
                        except Exception:
                            pass
                    os.replace(temp_journal, journal_path)
                    _trigger_wal_fault("after_journal_publish", {"journal_path": journal_path, "plan_id": plan.id})
                finally:
                    if temp_journal.exists():
                        try:
                            temp_journal.unlink()
                        except Exception:
                            pass

                # Atomically save updated companion metadata
                temp_meta = meta_path.parent / f".tmp_{meta_path.stem}_{uuid.uuid4().hex[:8]}.json"
                try:
                    with open(temp_meta, "w", encoding="utf-8") as f:
                        json.dump(meta_dict, f, indent=2, ensure_ascii=False)
                        f.flush()
                        try:
                            os.fsync(f.fileno())
                        except Exception:
                            pass
                    os.replace(temp_meta, meta_path)
                    _trigger_wal_fault("after_meta_replace", {"meta_path": meta_path, "plan_id": plan.id})
                finally:
                    if temp_meta.exists():
                        try:
                            temp_meta.unlink()
                        except Exception:
                            pass

                # Purge journal upon commit
                _trigger_wal_fault("before_journal_cleanup", {"journal_path": journal_path, "plan_id": plan.id})
                journal_path.unlink(missing_ok=True)

            return plan

    def list_blocking_questions(self, plan_id: str) -> List[Dict[str, Any]]:
        """List all blocking unknowns/questions for a plan with structured details.

        Returns list of dictionaries containing:
        id, question, decision, reason, affected_steps, affected_files,
        resolved, status ('unresolved' or 'resolved'), answer, answered_at,
        answered_by, and plan_revision.
        """
        self._validate_safe_id(plan_id)
        with self._get_lock(f"plan_{plan_id}"):
            recover_plan_journal(self.repo_dir, plan_id=plan_id)
            plan = self.get_plan(plan_id)
            if plan is None:
                raise PlanNotFoundError(f"Plan '{plan_id}' does not exist in workspace '{self.repo_dir}'.")
            return plan.get_blocking_questions()

    def _invalidate_affected_steps_progress(
        self,
        plan_id: str,
        affected_steps: Sequence[Any],
        unknown_id: str,
        affected_files: Optional[Sequence[str]] = None,
        plan: Optional[PlanDocument] = None,
    ) -> None:
        """Invalidate previously passed step progress records when clarification changes requirements.

        Guarantees:
        - Invalidate directly affected steps by ID.
        - Invalidate steps targeting affected files.
        - Invalidate transitive dependent steps (closure over dependencies/depends_on/requires).
        - Conservatively invalidate ambiguous steps touching affected files or downstream unisolated steps.
        """
        progress_file = self.plans_dir / f"{plan_id}.progress.json"
        if not progress_file.exists():
            return
        try:
            content = progress_file.read_text(encoding="utf-8")
            data = json.loads(content)
            if not isinstance(data, dict) or "steps" not in data or not isinstance(data["steps"], list):
                return

            if plan is None:
                plan = self.get_plan(plan_id)

            plan_steps = list(getattr(plan, "steps", []) or []) if plan else []
            norm_aff = {_normalize_step_id(s) for s in affected_steps}
            norm_files = {Path(f).as_posix().lower() for f in (affected_files or [])}

            # 1. Direct step invalidation
            invalidated_ids: Set[str] = set()
            is_global = not norm_aff and not norm_files

            for s in plan_steps:
                sid = _normalize_step_id(s.get("id") if isinstance(s, dict) else getattr(s, "id", ""))
                s_files = {Path(f).as_posix().lower() for f in (s.get("files") if isinstance(s, dict) else getattr(s, "files", [])) or []}
                if is_global or (sid in norm_aff) or (norm_files and (s_files & norm_files)):
                    invalidated_ids.add(sid)

            if not invalidated_ids and norm_aff:
                invalidated_ids.update(norm_aff)

            # 2. Transitive dependency invalidation (closure over dependencies/depends_on/requires)
            changed_graph = True
            while changed_graph:
                changed_graph = False
                for s in plan_steps:
                    sid = _normalize_step_id(s.get("id") if isinstance(s, dict) else getattr(s, "id", ""))
                    if sid in invalidated_ids:
                        continue
                    raw_deps = (
                        (s.get("dependencies") or s.get("depends_on") or s.get("requires") or [])
                        if isinstance(s, dict)
                        else (getattr(s, "dependencies", None) or getattr(s, "depends_on", None) or [])
                    )
                    deps = {_normalize_step_id(d) for d in raw_deps}
                    if deps & invalidated_ids:
                        invalidated_ids.add(sid)
                        changed_graph = True

            # 3. Ambiguous dependency handling: steps without explicit dependencies touching affected targets
            for s in plan_steps:
                sid = _normalize_step_id(s.get("id") if isinstance(s, dict) else getattr(s, "id", ""))
                if sid in invalidated_ids:
                    continue
                raw_deps = (
                    (s.get("dependencies") or s.get("depends_on") or s.get("requires") or [])
                    if isinstance(s, dict)
                    else (getattr(s, "dependencies", None) or getattr(s, "depends_on", None) or [])
                )
                if not raw_deps:
                    s_files = {Path(f).as_posix().lower() for f in (s.get("files") if isinstance(s, dict) else getattr(s, "files", [])) or []}
                    if norm_files and (s_files & norm_files):
                        invalidated_ids.add(sid)

            changed = False
            for s in data["steps"]:
                if isinstance(s, dict):
                    sid = _normalize_step_id(s.get("step_id", ""))
                    if sid in invalidated_ids and s.get("status") == "passed":
                        s["status"] = "pending"
                        s["error"] = f"Requirement invalidated by clarification '{unknown_id}'"
                        changed = True
            if changed:
                data["updated_at"] = time.time()
                temp_prog = progress_file.parent / f".tmp_{progress_file.stem}_{uuid.uuid4().hex[:8]}.json"
                try:
                    with open(temp_prog, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2, ensure_ascii=False)
                        f.flush()
                        try:
                            os.fsync(f.fileno())
                        except Exception:
                            pass
                    os.replace(temp_prog, progress_file)
                finally:
                    if temp_prog.exists():
                        try:
                            temp_prog.unlink()
                        except Exception:
                            pass
        except Exception:
            pass


    @staticmethod
    def _detect_plan_contradiction(
        answer: str,
        target_entry: Dict[str, Any],
        steps: Sequence[Any],
        acceptance_criteria: Sequence[str],
    ) -> Tuple[bool, str]:
        """Detect whether an answer directly contradicts existing step instructions or acceptance criteria."""
        import re
        clean_ans = answer.strip().lower()
        norm_aff_steps = {_normalize_step_id(s) for s in (target_entry.get("affected_steps") or [])}
        raw_aff_files = target_entry.get("affected_files") or []
        norm_aff_files = {Path(f).as_posix().lower() for f in raw_aff_files}

        dur_pattern = re.compile(r"\b(\d+)\s*(minute|min|hour|hr|second|sec|day|month|year)s?\b", re.IGNORECASE)
        ans_durations = dur_pattern.findall(clean_ans)

        ans_has_enable = bool(re.search(r"\b(enable|enabled|allow|allowed|true|yes)\b", clean_ans))
        ans_has_disable = bool(re.search(r"\b(disable|disabled|deny|denied|false|no|forbidden)\b", clean_ans))

        for s in steps:
            s_id = _normalize_step_id(s.get("id") if isinstance(s, dict) else getattr(s, "id", ""))
            is_step_aff = (not norm_aff_steps and not norm_aff_files) or (s_id in norm_aff_steps)
            if isinstance(s, dict) and norm_aff_files:
                s_files = {Path(f).as_posix().lower() for f in s.get("files", [])}
                if s_files & norm_aff_files:
                    is_step_aff = True

            if not is_step_aff:
                continue

            desc = str(s.get("description") if isinstance(s, dict) else getattr(s, "description", "")).lower()

            if ans_durations:
                step_durations = dur_pattern.findall(desc)
                for s_val, s_unit in step_durations:
                    norm_s_unit = s_unit[:3].lower()
                    for a_val, a_unit in ans_durations:
                        norm_a_unit = a_unit[:3].lower()
                        if norm_s_unit == norm_a_unit and s_val != a_val:
                            return True, f"Contradiction in step '{s_id}': step instructions specify {s_val} {s_unit}s, but answer specifies {a_val} {a_unit}s."

            if ans_has_enable and re.search(r"\b(disable|disabled|deny|forbidden)\b", desc):
                return True, f"Contradiction in step '{s_id}': step instructions specify disable/deny, but answer specifies enable/allow."
            if ans_has_disable and re.search(r"\b(enable|enabled|allow)\b", desc):
                return True, f"Contradiction in step '{s_id}': step instructions specify enable/allow, but answer specifies disable/deny."

        for crit in acceptance_criteria:
            c_str = crit.lower()
            if ans_durations:
                crit_durations = dur_pattern.findall(c_str)
                for c_val, c_unit in crit_durations:
                    norm_c_unit = c_unit[:3].lower()
                    for a_val, a_unit in ans_durations:
                        norm_a_unit = a_unit[:3].lower()
                        if norm_c_unit == norm_a_unit and c_val != a_val:
                            return True, f"Contradiction in acceptance criteria '{crit}': criteria specifies {c_val} {c_unit}s, but answer specifies {a_val} {a_unit}s."

        return False, ""

    def resolve_blocking_unknown(
        self,
        plan_id: str,
        unknown_id: str,
        answer: str,
        actor: str,
        expected_revision: Optional[int] = None,
        session_id: Optional[str] = None,
    ) -> PlanDocument:
        """Resolve an unresolved blocking unknown with an authorized user answer.

        Security & Integrity Guarantees:
        1. Validates safe plan and unknown IDs (rejects path traversal).
        2. Validates answer: must be non-empty string, length capped at 4000 chars.
        3. Treats answer as untrusted requirement data (no shell execution, no capability expansion).
        4. Session isolation: rejects cross-session modification if session_id does not match plan.session_id.
        5. Optimistic concurrency check: fails closed with StalePlanStateError if disk revision != expected_revision.
        6. Finds unknown by ID; fails if not found or already resolved.
        7. Records resolution metadata (resolved=True, answer, answered_at, answered_by, plan_revision).
        8. Binds answer into authoritative plan markdown and assumptions.
        9. Resets any affected steps in progress tracker if they were previously marked passed.
        10. Increments revision, recalculates content hash and metadata semantic digest.
        11. Persists atomically via Write-Ahead Journal (WAL).
        12. If plan was BLOCKED and all unknowns resolved, transitions plan to READY.
        13. Old ApprovedExecutionContract automatically invalidated by revision and metadata hash advance.
        """
        self._validate_safe_id(plan_id)
        if not unknown_id or not isinstance(unknown_id, str) or not unknown_id.strip():
            raise ValueError("Unknown ID must be a non-empty string.")
        if ".." in unknown_id or "/" in unknown_id or "\\" in unknown_id:
            raise PermissionError(f"Path traversal rejected in unknown ID: '{unknown_id}'")
        clean_uid = unknown_id.strip()

        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("Answer must be a non-empty string.")
        clean_answer = answer.strip()
        if len(clean_answer) > 4000:
            raise ValueError(f"Answer exceeds maximum allowed length of 4000 characters (got {len(clean_answer)}).")

        if not actor or not isinstance(actor, str) or not actor.strip():
            raise ValueError("Actor must be specified for unknown resolution.")
        clean_actor = actor.strip()

        lock = self._get_lock(f"plan_{plan_id}")
        with lock:
            recover_plan_journal(self.repo_dir, plan_id=plan_id)
            plan = self.get_plan(plan_id)
            if plan is None:
                raise PlanNotFoundError(f"Plan '{plan_id}' does not exist in workspace '{self.repo_dir}'.")

            # Session isolation check
            if session_id is not None and plan.session_id and plan.session_id.strip():
                if plan.session_id.strip() != session_id.strip():
                    raise PermissionError(
                        f"Cross-session access denied: plan '{plan_id}' belongs to session '{plan.session_id}', "
                        f"requested by session '{session_id}'."
                    )

            # Optimistic concurrency check
            if expected_revision is not None and plan.revision != expected_revision:
                raise StalePlanStateError(
                    f"Stale plan revision for '{plan_id}': disk revision is {plan.revision}, expected {expected_revision}."
                )

            # Find matching unknown
            raw_unknowns = list(getattr(plan, "blocking_unknowns", []) or [])
            target_idx: Optional[int] = None
            target_entry: Optional[Dict[str, Any]] = None

            for idx, item in enumerate(raw_unknowns, 1):
                cur_id = f"blk_{idx}"
                if isinstance(item, dict):
                    cur_id = str(item.get("id") or cur_id)
                if cur_id.lower() == clean_uid.lower() or _normalize_step_id(cur_id) == _normalize_step_id(clean_uid):
                    target_idx = idx - 1
                    if isinstance(item, dict):
                        target_entry = dict(item)
                    elif isinstance(item, str):
                        target_entry = {
                            "id": cur_id,
                            "decision": item.strip(),
                            "question": item.strip(),
                            "reason": "Missing architectural or business requirement",
                            "affected_steps": [],
                            "affected_files": [],
                            "resolved": False,
                        }
                    else:
                        target_entry = {
                            "id": cur_id,
                            "decision": str(item),
                            "question": str(item),
                            "reason": "Malformed item",
                            "affected_steps": [],
                            "affected_files": [],
                            "resolved": False,
                        }
                    break

            if target_entry is None or target_idx is None:
                raise ValueError(f"Unknown ID '{clean_uid}' not found in plan '{plan_id}'.")

            if target_entry.get("resolved") is True:
                raise InvalidPlanTransitionError(f"Unknown '{clean_uid}' in plan '{plan_id}' is already resolved.")

            now_iso = datetime.now(timezone.utc).isoformat()
            target_entry["resolved"] = True
            target_entry["answer"] = clean_answer
            target_entry["answered_at"] = now_iso
            target_entry["answered_by"] = clean_actor
            target_entry["plan_revision"] = plan.revision + 1
            raw_unknowns[target_idx] = target_entry
            plan.blocking_unknowns = raw_unknowns

            # Contradiction detection against step descriptions and acceptance criteria
            has_contradiction, contra_reason = self._detect_plan_contradiction(
                clean_answer,
                target_entry,
                getattr(plan, "steps", []) or [],
                getattr(plan, "acceptance_criteria", []) or [],
            )

            # Invalidate progress on affected steps if previously passed (with transitive closure)
            aff_steps = target_entry.get("affected_steps") or []
            aff_files = target_entry.get("affected_files") or []
            self._invalidate_affected_steps_progress(plan.id, aff_steps, clean_uid, affected_files=aff_files, plan=plan)

            # Bind answer into assumptions
            clarif_assumption = f"[Clarification {target_entry.get('id', clean_uid)}]: {clean_answer}"
            if clarif_assumption not in plan.assumptions:
                plan.assumptions.append(clarif_assumption)

            # Update affected steps with reconciled constraints/clarifications if no contradiction
            if not has_contradiction:
                reconciled_steps = []
                for s in getattr(plan, "steps", []) or []:
                    if isinstance(s, dict):
                        s_copy = dict(s)
                        s_id = _normalize_step_id(s_copy.get("id", ""))
                        s_files = {Path(f).as_posix().lower() for f in s_copy.get("files", [])}
                        is_aff = (not aff_steps and not aff_files) or (s_id in {_normalize_step_id(x) for x in aff_steps})
                        if aff_files and (s_files & {Path(f).as_posix().lower() for f in aff_files}):
                            is_aff = True
                        if is_aff:
                            cur_clarifs = list(s_copy.get("clarifications") or [])
                            cl_entry = {
                                "id": target_entry.get("id", clean_uid),
                                "decision": target_entry.get("decision", clean_uid),
                                "answer": clean_answer,
                            }
                            if cl_entry not in cur_clarifs:
                                cur_clarifs.append(cl_entry)
                            s_copy["clarifications"] = cur_clarifs
                            cur_constraints = list(s_copy.get("constraints") or [])
                            c_line = f"[Clarification {target_entry.get('id', clean_uid)}]: {clean_answer}"
                            if c_line not in cur_constraints:
                                cur_constraints.append(c_line)
                            s_copy["constraints"] = cur_constraints
                        reconciled_steps.append(s_copy)
                    else:
                        reconciled_steps.append(s)
                plan.steps = reconciled_steps

                # Reconcile acceptance criteria if not already reflecting requirement
                crit_text = f"Clarification ({target_entry.get('id', clean_uid)}): {clean_answer}"
                if not any(clean_answer in c for c in plan.acceptance_criteria):
                    plan.acceptance_criteria.append(crit_text)

            # Update plan markdown content with reviewable clarification entry
            disk_content = plan.path.read_text(encoding="utf-8", errors="replace") if plan.path.exists() else (plan.content or "")
            q_text = target_entry.get("question") or target_entry.get("decision") or clean_uid
            clarif_md_block = (
                f"\n\n### Clarification Resolution ({target_entry.get('id', clean_uid)})\n"
                f"- **Question:** {q_text}\n"
                f"- **Answer:** {clean_answer}\n"
                f"- **Resolved by:** {clean_actor} at {now_iso}\n"
            )
            if has_contradiction:
                clarif_md_block += f"- **Warning:** Contradiction detected with existing plan instructions: {contra_reason}\n"
            updated_content = disk_content.rstrip() + clarif_md_block + "\n"
            plan.content = updated_content

            previous_meta_dict = plan.to_metadata_dict()
            previous_revision = plan.revision
            previous_md_content = disk_content

            # Status transition evaluation
            all_resolved = not plan.get_unresolved_blocking_unknowns()
            old_status = plan.status
            target_status = plan.status

            if has_contradiction:
                # Contradictions require explicit plan revision; demote/keep in draft
                target_status = PlanStatus.DRAFT
                plan.status = PlanStatus.DRAFT
            elif plan.status == PlanStatus.BLOCKED and all_resolved:
                target_status = PlanStatus.READY
                plan.status = PlanStatus.READY

            plan.transitions.append({
                "from_status": old_status.value,
                "to_status": target_status.value,
                "timestamp": now_iso,
                "actor": clean_actor,
                "reason": (
                    f"Resolved blocking unknown '{clean_uid}' (CONTRADICTION DETECTED: plan placed in draft for revision)"
                    if has_contradiction
                    else f"Resolved blocking unknown '{clean_uid}'"
                ),
                "evidence": {
                    "unknown_id": clean_uid,
                    "answer_preview": clean_answer[:120],
                    "all_unknowns_resolved": all_resolved,
                    "contradiction_detected": has_contradiction,
                    "contradiction_reason": contra_reason if has_contradiction else None,
                },
            })

            plan.revision += 1
            plan.updated_at = now_iso

            # Recompute content hash and metadata semantic digest
            plan.content_hash = hashlib.sha256(updated_content.encode("utf-8")).hexdigest()
            plan.content_tampered = False

            meta_dict = plan.to_metadata_dict()
            new_meta_hash = compute_metadata_semantic_digest(meta_dict)
            plan.metadata_hash = new_meta_hash
            plan.metadata_tampered = False
            meta_dict["metadata_hash"] = new_meta_hash

            meta_path = plan.path.with_suffix(".meta.json")
            journal_dir = get_journal_dir(self.repo_dir)
            journal_id = f"wal_{plan.id}_{int(datetime.now(timezone.utc).timestamp() * 1000)}_{uuid.uuid4().hex[:6]}"
            journal_entry = PlanJournalEntry(
                journal_id=journal_id,
                plan_id=plan.id,
                filename=plan.filename,
                stage="PREPARED",
                created_at=now_iso,
                expected_revision=previous_revision,
                target_revision=plan.revision,
                target_status=target_status.value,
                new_md_hash=plan.content_hash,
                new_meta_dict=meta_dict,
                has_md_update=True,
                new_md_content=updated_content,
                previous_md_content=previous_md_content,
                previous_meta_dict=previous_meta_dict,
            )
            journal_path = journal_dir / f"{journal_id}.json"
            temp_journal = journal_dir / f".tmp_{journal_id}.json"

            with self._get_lock("journal_recovery"):
                try:
                    with open(temp_journal, "w", encoding="utf-8") as f:
                        json.dump(journal_entry.to_dict(), f, indent=2, ensure_ascii=False)
                        f.flush()
                        try:
                            os.fsync(f.fileno())
                        except Exception:
                            pass
                    os.replace(temp_journal, journal_path)
                    _trigger_wal_fault("after_journal_publish", {"journal_path": journal_path, "plan_id": plan.id})
                finally:
                    if temp_journal.exists():
                        try:
                            temp_journal.unlink()
                        except Exception:
                            pass

                # Atomic write for markdown document
                temp_md = plan.path.parent / f".tmp_{plan.path.stem}_{uuid.uuid4().hex[:8]}.md"
                try:
                    with open(temp_md, "w", encoding="utf-8", newline="") as f:
                        f.write(updated_content)
                        f.flush()
                        try:
                            os.fsync(f.fileno())
                        except Exception:
                            pass
                    os.replace(temp_md, plan.path)
                    _trigger_wal_fault("after_md_replace", {"target_md": plan.path, "plan_id": plan.id})
                finally:
                    if temp_md.exists():
                        try:
                            temp_md.unlink()
                        except Exception:
                            pass

                # Atomic write for metadata sidecar
                temp_meta = meta_path.parent / f".tmp_{meta_path.stem}_{uuid.uuid4().hex[:8]}.json"
                try:
                    with open(temp_meta, "w", encoding="utf-8") as f:
                        json.dump(meta_dict, f, indent=2, ensure_ascii=False)
                        f.flush()
                        try:
                            os.fsync(f.fileno())
                        except Exception:
                            pass
                    os.replace(temp_meta, meta_path)
                    _trigger_wal_fault("after_meta_replace", {"meta_path": meta_path, "plan_id": plan.id})
                finally:
                    if temp_meta.exists():
                        try:
                            temp_meta.unlink()
                        except Exception:
                            pass

                # Purge journal upon commit
                _trigger_wal_fault("before_journal_cleanup", {"journal_path": journal_path, "plan_id": plan.id})
                journal_path.unlink(missing_ok=True)

            return plan


    def verify_plan(
        self,
        plan_id: str,
        actor: str = "verifier",
        custom_commands: Optional[List[Any]] = None,
    ) -> Tuple[bool, VerificationEvidence, PlanDocument]:
        """Execute independent structured verification for an implemented plan.

        Enforces Workstream D invariants:
        - Typed explicit mapping between declared acceptance criteria and verification commands.
        - 100% acceptance criteria coverage: every declared criterion must have at least one check.
        - Verification checks execute through run_hardened_subprocess and command policy.
        - Checks cannot be unmapped, duplicate, dangling, or reference unknown criteria.
        - Output fingerprints, timestamps, check_id, criterion_ref, and verifier identity are recorded.
        - Fail closed: empty checks or incomplete coverage cannot yield VERIFIED.
        """
        self._validate_safe_id(plan_id)
        recover_plan_journal(self.repo_dir, plan_id=plan_id)
        plan = self.get_plan(plan_id)
        if plan is None:
            raise PlanNotFoundError(f"Plan '{plan_id}' does not exist in workspace '{self.repo_dir}'.")

        if plan.status != PlanStatus.IMPLEMENTED:
            raise InvalidPlanTransitionError(
                f"Cannot verify plan '{plan_id}': status is '{plan.status.value}', expected 'implemented'."
            )

        if plan.content_tampered:
            raise InvalidPlanTransitionError(
                f"Cannot verify plan '{plan_id}': Markdown content has been modified out-of-band."
            )

        raw_criteria = getattr(plan, "acceptance_criteria", None) or []
        declared_criteria = [str(c).strip() for c in raw_criteria if str(c).strip()]
        if not declared_criteria:
            raise InvalidPlanTransitionError(
                f"Cannot verify plan '{plan_id}': plan has no declared acceptance criteria."
            )
        declared_set = set(declared_criteria)

        cmds = custom_commands or plan.verification_commands
        if not cmds:
            raise InvalidPlanTransitionError(
                f"Plan '{plan_id}' has no verification commands defined and none were provided."
            )

        parsed_mappings: List[Dict[str, Any]] = []
        for cmd_item in cmds:
            if isinstance(cmd_item, CriterionVerificationMapping):
                parsed_mappings.append({
                    "criterion_ref": cmd_item.criterion_id.strip(),
                    "command": cmd_item.command.strip(),
                    "check_name": (cmd_item.check_name or cmd_item.command).strip(),
                    "check_id": cmd_item.check_id or f"chk_{uuid.uuid4().hex[:6]}",
                })
            elif isinstance(cmd_item, dict):
                crit = str(cmd_item.get("criterion_id") or cmd_item.get("criterion_ref") or cmd_item.get("criterion") or "").strip()
                cmd = str(cmd_item.get("command") or cmd_item.get("cmd") or "").strip()
                name = str(cmd_item.get("check_name") or cmd_item.get("name") or cmd).strip()
                cid = str(cmd_item.get("check_id") or f"chk_{uuid.uuid4().hex[:6]}").strip()
                parsed_mappings.append({
                    "criterion_ref": crit,
                    "command": cmd,
                    "check_name": name,
                    "check_id": cid,
                })
            elif isinstance(cmd_item, str):
                parsed_mappings.append({
                    "criterion_ref": "",
                    "command": cmd_item.strip(),
                    "check_name": cmd_item.strip(),
                    "check_id": f"chk_{uuid.uuid4().hex[:6]}",
                })

        covered_set: Set[str] = set()
        seen_check_ids: Set[str] = set()
        for m in parsed_mappings:
            c_ref = m["criterion_ref"]
            if not c_ref:
                raise InvalidPlanTransitionError(
                    f"Verification check '{m['check_name']}' has no criterion reference. "
                    f"Explicit criterion mapping is required for verification."
                )
            if c_ref not in declared_set:
                raise InvalidPlanTransitionError(
                    f"Verification check '{m['check_name']}' references unknown criterion '{c_ref}'. "
                    f"Declared criteria: {sorted(declared_set)}."
                )
            if m["check_id"] in seen_check_ids:
                raise InvalidPlanTransitionError(
                    f"Duplicate check identifier '{m['check_id']}' in verification mappings."
                )
            seen_check_ids.add(m["check_id"])
            covered_set.add(c_ref)

        missing_coverage = declared_set - covered_set
        if missing_coverage:
            raise InvalidPlanTransitionError(
                f"Incomplete verification coverage: missing checks for criteria: {sorted(missing_coverage)}."
            )

        from core.runtime.process import run_hardened_subprocess

        check_results: List[VerificationCheckResult] = []
        all_passed = True
        now_iso = datetime.now(timezone.utc).isoformat()

        for m in parsed_mappings:
            clean_cmd = m["command"]
            if not clean_cmd:
                continue
            try:
                cmd_tokens = shlex.split(clean_cmd, posix=os.name != "nt")
                proc = run_hardened_subprocess(
                    cmd_tokens,
                    cwd=self.repo_dir,
                    timeout=60.0,
                )
                output = proc.stdout + "\n" + proc.stderr
                out_fingerprint = hashlib.sha256(output.encode("utf-8")).hexdigest()
                status = "passed" if proc.returncode == 0 else "failed"
                if proc.returncode != 0:
                    all_passed = False

                check_results.append(
                    VerificationCheckResult(
                        check_name=m["check_name"],
                        command=clean_cmd,
                        status=status,
                        output_fingerprint=out_fingerprint,
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        criterion_ref=m["criterion_ref"],
                        check_id=m["check_id"],
                        details={
                            "returncode": proc.returncode,
                            "stdout": proc.stdout[:1000],
                            "stderr": proc.stderr[:1000],
                        },
                    )
                )
            except Exception as e:
                all_passed = False
                check_results.append(
                    VerificationCheckResult(
                        check_name=m["check_name"],
                        command=clean_cmd,
                        status="failed",
                        output_fingerprint="",
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        criterion_ref=m["criterion_ref"],
                        check_id=m["check_id"],
                        details={"error": str(e)},
                    )
                )

        if not check_results:
            raise InvalidPlanTransitionError(
                f"No valid verification checks were executed for plan '{plan_id}'."
            )

        evidence = VerificationEvidence(
            plan_id=plan.id,
            plan_revision=plan.revision,
            plan_content_hash=plan.content_hash,
            checks=tuple(check_results),
            timestamp=now_iso,
            summary="All mapped acceptance criteria checks passed (structural coverage)" if all_passed else "Verification checks failed",
            verifier=actor,
        )

        if all_passed:
            updated_plan = self.update_status(
                plan_id=plan.id,
                new_status=PlanStatus.VERIFIED,
                actor=actor,
                reason="Independent structured verification succeeded (acceptance criteria checks passed)",
                evidence=evidence.to_dict(),
                expected_revision=plan.revision,
            )
            return True, evidence, updated_plan
        else:
            updated_plan = self.update_status(
                plan_id=plan.id,
                new_status=PlanStatus.FAILED,
                actor=actor,
                reason="Structured verification checks failed",
                evidence=evidence.to_dict(),
                expected_revision=plan.revision,
            )
            return False, evidence, updated_plan

    def select_plan(
        self,
        query: Optional[str] = None,
        candidate_intent: bool = False,
        session_selected_id: Optional[str] = None,
    ) -> PlanSelectionResult:
        """Deterministic plan selection satisfying Phase D requirements.

        Semantics:
        1. Explicit valid plan ID: select that exact plan.
        2. Explicit unambiguous title: select the matching plan.
        3. No explicit reference and exactly one eligible candidate: select it only when caller's intent clearly requests selection.
        4. Multiple eligible candidates: return concise list and ask user to choose.
        5. No eligible candidates: return clear explanation and next action.
        6. Ambiguous, unknown, malformed, or cross-workspace IDs: do not guess or fall back to latest.
        """
        # 1. Explicit reference query
        if query and query.strip():
            clean_q = query.strip()
            # Try exact ID first
            try:
                plan_by_id = self.get_plan(clean_q)
                if plan_by_id is not None:
                    return PlanSelectionResult(
                        selected=plan_by_id,
                        status="selected",
                        message=f"Plan '{plan_by_id.id}' selected ({plan_by_id.title}).",
                    )
            except (PermissionError, PlanNotFoundError):
                return PlanSelectionResult(
                    status="rejected",
                    message=f"Invalid or unauthorized plan ID: '{clean_q}'.",
                )

            # Try exact unambiguous title match second
            try:
                summaries = self.list_plans()
                matching_title = [s for s in summaries if s.title.strip().lower() == clean_q.lower()]
                if len(matching_title) == 1:
                    plan_by_title = self.get_plan(matching_title[0].id)
                    return PlanSelectionResult(
                        selected=plan_by_title,
                        status="selected",
                        message=f"Plan '{matching_title[0].id}' selected by title match ('{matching_title[0].title}').",
                    )
                if len(matching_title) > 1:
                    return PlanSelectionResult(
                        candidates=tuple(matching_title),
                        status="ambiguous",
                        message=f"Multiple plans match title '{clean_q}'. Please choose by plan ID:",
                    )
            except Exception:
                pass

            # Fail closed: No ID match and no title match -> Do NOT guess or fall back!
            return PlanSelectionResult(
                status="none_found",
                message=f"No plan matching '{clean_q}' found in this workspace.",
            )

        # 2. No explicit query provided
        # Check if caller's intent clearly requests selection or continuation
        if candidate_intent:
            all_summaries = self.list_plans()
            # Eligible candidates: READY or IN_PROGRESS
            eligible = [s for s in all_summaries if s.status in (PlanStatus.READY, PlanStatus.IN_PROGRESS)]
            if len(eligible) == 1:
                selected_plan = self.get_plan(eligible[0].id)
                return PlanSelectionResult(
                    selected=selected_plan,
                    status="selected",
                    message=f"Selected single eligible candidate plan '{eligible[0].id}' ({eligible[0].title}).",
                )
            if len(eligible) > 1:
                return PlanSelectionResult(
                    candidates=tuple(eligible),
                    status="ambiguous",
                    message="Multiple eligible plans exist in this workspace. Please select a plan by ID:",
                )
            return PlanSelectionResult(
                status="none_found",
                message="No eligible plans (ready or in_progress) found in this workspace. Use `/mode plan` to create a plan.",
            )

        # 3. No explicit query and no selection intent: check session selected ID if present
        if session_selected_id:
            try:
                validated_session_plan = self.get_plan(session_selected_id)
                if validated_session_plan is not None:
                    return PlanSelectionResult(
                        selected=validated_session_plan,
                        status="selected",
                        message=f"Currently selected plan is '{validated_session_plan.id}' ({validated_session_plan.title}).",
                    )
            except Exception:
                pass

        return PlanSelectionResult(
            status="none_found",
            message="No plan selected. Use `/plans` to list plans or `/plan select <id>` to choose one.",
        )


def check_plan_staleness(repo_dir: Path, plan: PlanDocument) -> Tuple[bool, List[str]]:
    """Determine whether the repository has changed since the plan was created."""
    from core.runtime.process import run_hardened_subprocess

    reasons: List[str] = []

    # 1. Check Git HEAD commit hash
    try:
        head_proc = run_hardened_subprocess(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir,
            timeout=10.0,
        )
        if head_proc.returncode == 0:
            current_commit = head_proc.stdout.strip()
            if plan.commit_hash and current_commit and plan.commit_hash != current_commit:
                reasons.append(
                    f"Commit Git telah berubah dari {plan.commit_hash[:7]} menjadi {current_commit[:7]}"
                )
    except Exception:
        pass

    # 2. Check relevant files hash & existence
    for rel_path in plan.relevant_files:
        full_path = repo_dir / rel_path
        old_hash = plan.file_hashes.get(rel_path)
        if not full_path.exists():
            if old_hash:
                reasons.append(f"Berkas relevan '{rel_path}' dihapus sejak rencana dibuat.")
        else:
            current_hash = compute_file_hash(full_path)
            if old_hash and current_hash != old_hash:
                reasons.append(f"Berkas relevan '{rel_path}' dimodifikasi sejak rencana dibuat.")

    # 3. Check uncommitted changes touching relevant files
    try:
        status_proc = run_hardened_subprocess(
            ["git", "status", "--porcelain"],
            cwd=repo_dir,
            timeout=10.0,
        )
        if status_proc.returncode == 0:
            for line in status_proc.stdout.splitlines():
                parts = line.strip().split(maxsplit=1)
                if len(parts) == 2:
                    dirty_file = parts[1].strip()
                    if any(dirty_file == rf or dirty_file.startswith(rf.rstrip("/") + "/") for rf in plan.relevant_files):
                        reasons.append(f"Perubahan uncommitted terdeteksi pada '{dirty_file}'.")
    except Exception:
        pass

    # Deduplicate reasons while preserving order
    seen: Set[str] = set()
    deduped_reasons = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            deduped_reasons.append(r)

    return len(deduped_reasons) > 0, deduped_reasons


def format_step_architecture_context(context: Dict[str, Any]) -> str:
    """Format step-relevant architectural context into a bounded prompt block."""
    lines: List[str] = ["[Architectural Constraints & Boundaries]"]
    ctx = context.get("target_context")
    strat = context.get("architecture_strategy")
    if ctx or strat:
        parts = []
        if ctx:
            parts.append(f"Target Context: {ctx}")
        if strat:
            parts.append(f"Strategy: {strat}")
        lines.append(f"- {' | '.join(parts)}")

    constraints = context.get("relevant_constraints", [])
    if constraints:
        lines.append("- Relevant Constraints:")
        for c in constraints:
            lines.append(f"  * {c}")

    invariants = context.get("behavior_invariants", [])
    if invariants:
        lines.append("- Behavior & State Invariants:")
        for inv in invariants:
            lines.append(f"  * {inv}")

    contracts = context.get("compatibility_contracts", [])
    if contracts:
        lines.append("- Compatibility Contracts:")
        for cc in contracts:
            lines.append(f"  * {cc}")

    non_scope = context.get("non_scope", [])
    if non_scope:
        lines.append("- Out of Scope (Do NOT Modify):")
        for ns in non_scope:
            lines.append(f"  * {ns}")

    migration = context.get("migration_constraints", [])
    if migration:
        lines.append("- Migration & Rollback Constraints:")
        for mc in migration:
            lines.append(f"  * {mc}")

    clarifications = context.get("resolved_clarifications", [])
    if clarifications:
        lines.append("- Authoritative Clarifications & Resolved Requirements (SUPERCEDES CONFLICTING DEFAULTS):")
        for cl in clarifications:
            lines.append(f"  * [{cl['id']}] {cl['decision']}: {cl['answer']}")

    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def format_plan_handoff(plan: PlanDocument, repo_dir: Path) -> str:
    """Format the latest plan into context for Build mode."""
    is_stale, reasons = check_plan_staleness(repo_dir, plan)

    lines = [
        f"### Konteks Handoff Rencana Terakhir: {plan.title}",
        f"**File Rencana:** `.brainfrog/plans/{plan.filename}`",
        f"**Tujuan (Goal):** {plan.goal}",
    ]

    if plan.target_context or plan.architecture_strategy:
        arch_parts = []
        if plan.target_context:
            arch_parts.append(f"Konteks: `{plan.target_context}`")
        if plan.architecture_strategy:
            arch_parts.append(f"Strategi: `{plan.architecture_strategy}`")
        lines.append(f"\n**Konteks Arsitektur:** {' | '.join(arch_parts)}")

    if plan.scope:
        lines.append("\n**Batasan Perubahan (Scope):**")
        for sc in plan.scope:
            lines.append(f"- {sc}")

    if plan.non_scope:
        lines.append("\n**Di Luar Batasan (Non-Scope):**")
        for nsc in plan.non_scope:
            lines.append(f"- {nsc}")

    if plan.architectural_constraints:
        lines.append("\n**Batasan Arsitektur (Architectural Constraints):**")
        for ac in plan.architectural_constraints:
            c_str = ac.get("text", str(ac)) if isinstance(ac, dict) else str(ac)
            lines.append(f"- {c_str}")

    if plan.compatibility_contracts:
        lines.append("\n**Kontrak Kompatibilitas:**")
        for cc in plan.compatibility_contracts:
            lines.append(f"- {cc}")

    if plan.behavior_invariants:
        lines.append("\n**Behavior & State Invariants:**")
        for bi in plan.behavior_invariants:
            lines.append(f"- {bi}")

    if plan.migration_constraints:
        lines.append("\n**Batasan Migrasi & Rollback:**")
        for mc in plan.migration_constraints:
            lines.append(f"- {mc}")

    if plan.blocking_unknowns:
        lines.append("\n⚠️ **Keputusan Belum Terselesaikan (Blocking Unknowns):**")
        for bu in plan.blocking_unknowns:
            bu_str = bu.get("decision", bu.get("question", str(bu))) if isinstance(bu, dict) else str(bu)
            lines.append(f"- [BLOCKED] {bu_str}")

    if plan.clarifying_questions:
        lines.append("\n**Pertanyaan Klarifikasi:**")
        for cq in plan.clarifying_questions:
            lines.append(f"- {cq}")

    if plan.acceptance_criteria:
        lines.append("\n**Kriteria Penerimaan (Acceptance Criteria):**")
        for ac in plan.acceptance_criteria:
            lines.append(f"- [ ] {ac}")

    if plan.assumptions:
        lines.append("\n**Asumsi & Usulan Teknis:**")
        for asm in plan.assumptions:
            lines.append(f"- {asm}")

    if plan.relevant_files:
        lines.append(f"\n**File Terkait:** {', '.join(plan.relevant_files)}")

    if plan.steps:
        lines.append("\n**Rencana Langkah Implementasi:**")
        for s in plan.steps:
            if isinstance(s, dict):
                s_id = s.get("id", "")
                s_desc = s.get("description", "")
                s_files = s.get("files", [])
                files_note = f" (files: {', '.join(s_files)})" if s_files else ""
                lines.append(f"{s_id}. {s_desc}{files_note}")
            else:
                lines.append(f"- {s}")

    if getattr(plan, "verification_commands", None):
        lines.append(f"\n**Perintah Verifikasi (Verification Command):** {', '.join(plan.verification_commands)}")

    if is_stale:
        lines.append("\n⚠️ **PERINGATAN PERUBAHAN CODEBASE (STALE PLAN):**")
        lines.append("Repository telah mengalami perubahan sejak rencana ini disusun:")
        for r in reasons:
            lines.append(f"  - {r}")
        lines.append("Agent wajib memeriksa ulang bagian file yang terdampak sebelum mengeksekusi.")

    lines.append(
        "\n**Instruksi Adaptabilitas:** Jika instruksi terbaru dari pengguna mengubah scope atau prioritas, "
        "prioritaskan kebutuhan terbaru pengguna dan jangan mengikuti rencana lama secara buta."
    )

    return "\n".join(lines)
