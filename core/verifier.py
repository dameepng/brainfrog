"""Independent, standalone acceptance criteria verification engine for BrainFrog.

Product Positioning:
    "Agent says done. BrainFrog proves it."

This module provides a standalone, deterministic verification authority capable of
evaluating code changes produced by coding agents (Claude Code, OpenCode, Codex, etc.)
against explicit Markdown acceptance criteria specifications (spec.md).

Hardened Invariants (Phase 1B):
1. Standalone Startup: Zero TypeSafe API key or LLM initialization required.
2. Execution Trust Boundary: By default, untrusted repository build/test commands fail
   closed (BLOCKED) unless explicit operator authorization (--trust-local-code) or
   a secure container isolation backend is active.
3. Command Policy: Commands are strictly tokenized, shell wrappers/chaining are blocked,
   path traversals are rejected, and binary resolution is pinned to system executables.
4. Environment Sanitization: Host API keys, tokens, and cloud secrets are scrubbed from
   subprocess environments.
5. Deterministic Source Snapshot: Binds Git HEAD commit, tracked diffs, untracked inputs,
   and spec hash into a composite SHA-256 digest.
6. Drift Detection: Validates pre- and post-run workspace state to detect file modifications
   occurring during verification.
7. Atomic & Tamper-Evident Evidence: Generates collision-resistant, atomically finalized
   reports and provides standalone verification (verify_report_integrity).
8. Strict Status Semantics: PASS, FAIL, BLOCKED, NOT_RUN; unexecuted checks never silently pass.
"""

from __future__ import annotations

import collections
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from core.runtime.process import run_hardened_subprocess
from core.runtime.proof import sample_git_state
from core.runtime.secret_scrubbing import safe_scrub as redact_secrets
from core.runtime.browser_plan import parse_browser_plan
from core.runtime.browser_sandbox import BrowserExecutionResult, DockerBrowserSandbox
from core.runtime.sandbox import (
    DockerSandboxBackend,
    InfraErrorCategory,
    LocalTrustedExecutionBackend,
    SandboxPolicy,
    SandboxResourceLimits,
    SnapshotError,
    SnapshotManifest,
    TestResultAdapter,
    VerificationExecutionBackend,
    classify_runner,
    materialize_source_snapshot_with_manifest,
)


# =============================================================================
# STATUS & VERDICT ENUMS
# =============================================================================

class CriterionStatus(str, Enum):
    """Execution status for an individual acceptance criterion."""
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    NOT_RUN = "NOT_RUN"


class OverallVerdict(str, Enum):
    """Overall verification status for the entire specification run."""
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


class ExecutionTrustMode(str, Enum):
    """Trust and isolation boundary for command execution."""
    UNTRUSTED_FAIL_CLOSED = "UNTRUSTED_FAIL_CLOSED"
    TRUSTED_OPERATOR = "TRUSTED_OPERATOR"
    ISOLATED_SANDBOX = "ISOLATED_SANDBOX"


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class CriterionSpec:
    """Parsed acceptance criterion from spec.md."""
    criterion_id: str
    title: str
    description: str
    verification_type: str  # 'build', 'test', 'browser', 'unsupported'
    command: Optional[str] = None
    target: Optional[str] = None
    browser_plan: Optional[Any] = None
    browser_plan_raw: Optional[str] = None
    browser_plan_error: Optional[str] = None
    app_profile: Optional[str] = None


@dataclass
class SpecDocument:
    """Parsed specification document."""
    title: str
    goal: str
    non_goals: List[str]
    criteria: List[CriterionSpec]
    constraints: List[str]
    raw_content: str
    content_hash: str
    spec_path: Optional[str] = None


@dataclass
class CriterionResult:
    """Verification outcome and evidence for an individual criterion."""
    criterion_id: str
    title: str
    description: str
    verification_type: str
    status: CriterionStatus
    command: Optional[str] = None
    actual_executed_argv: Optional[List[str]] = None
    target: Optional[str] = None
    exit_code: Optional[int] = None
    duration_ms: float = 0.0
    started_at: str = ""
    finished_at: str = ""
    stdout: str = ""
    stderr: str = ""
    evidence_files: Dict[str, str] = field(default_factory=dict)
    evidence_hashes: Dict[str, str] = field(default_factory=dict)
    failure_reason: Optional[str] = None
    test_count: Optional[int] = None
    test_failure_count: Optional[int] = None
    execution_backend: Optional[str] = None
    container_image: Optional[str] = None
    container_image_digest: Optional[str] = None
    sandbox_policy: Optional[Dict[str, Any]] = None
    infrastructure_error_category: Optional[str] = None
    test_skipped_count: Optional[int] = None
    result_authoritative: bool = False
    materialized_source_snapshot_digest: Optional[str] = None
    observed_source_snapshot_digest: Optional[str] = None
    container_id: Optional[str] = None
    container_name: Optional[str] = None
    effective_container_config: Optional[Dict[str, Any]] = None
    harness_sha256: Optional[str] = None
    output_truncated: bool = False
    browser_plan_sha256: Optional[str] = None
    app_profile_id: Optional[str] = None
    assertion_count: Optional[int] = None
    assertion_failure_count: Optional[int] = None
    browser_evidence: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "title": self.title,
            "description": self.description,
            "verification_type": self.verification_type,
            "status": self.status.value,
            "command": self.command,
            "actual_executed_argv": self.actual_executed_argv,
            "target": self.target,
            "exit_code": self.exit_code,
            "duration_ms": round(self.duration_ms, 2),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "evidence_files": self.evidence_files,
            "evidence_hashes": self.evidence_hashes,
            "failure_reason": self.failure_reason,
            "test_count": self.test_count,
            "test_failure_count": self.test_failure_count,
            "execution_backend": self.execution_backend,
            "container_image": self.container_image,
            "container_image_digest": self.container_image_digest,
            "sandbox_policy": self.sandbox_policy,
            "infrastructure_error_category": self.infrastructure_error_category,
            "test_skipped_count": self.test_skipped_count,
            "result_authoritative": self.result_authoritative,
            "materialized_source_snapshot_digest": self.materialized_source_snapshot_digest,
            "observed_source_snapshot_digest": self.observed_source_snapshot_digest,
            "container_id": self.container_id,
            "container_name": self.container_name,
            "effective_container_config": self.effective_container_config,
            "harness_sha256": self.harness_sha256,
            "output_truncated": self.output_truncated,
            "browser_plan_sha256": self.browser_plan_sha256,
            "app_profile_id": self.app_profile_id,
            "assertion_count": self.assertion_count,
            "assertion_failure_count": self.assertion_failure_count,
            "browser_evidence": self.browser_evidence,
        }


@dataclass
class VerificationSummary:
    total: int
    passed: int
    failed: int
    blocked: int
    not_run: int

    def to_dict(self) -> Dict[str, int]:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "blocked": self.blocked,
            "not_run": self.not_run,
        }


@dataclass
class VerificationReport:
    """Comprehensive, machine-readable, and auditable verification report."""
    run_id: str
    created_at: str
    repository: str
    git_head_sha: Optional[str]
    is_dirty: bool
    dirty_files: List[str]
    spec_path: str
    spec_sha256: str
    overall_verdict: OverallVerdict
    summary: VerificationSummary
    criteria: List[CriterionResult]
    goal: str
    non_goals: List[str]
    constraints: List[str]
    report_dir: Path
    exit_code: int
    verifier_version: str = "0.1.0-alpha"
    source_snapshot_digest: str = ""
    execution_trust_mode: str = "UNTRUSTED_FAIL_CLOSED"
    execution_backend: str = "none"
    authorization_reference: Optional[str] = None
    workspace_drift_detected: bool = False
    live_workspace_post_snapshot_digest: str = ""
    materialized_source_snapshot_digest: Optional[str] = None
    materialized_file_count: Optional[int] = None
    snapshot_error: Optional[str] = None
    run_evidence_files: Dict[str, str] = field(default_factory=dict)
    run_evidence_hashes: Dict[str, str] = field(default_factory=dict)
    report_md_sha256: Optional[str] = None
    container_image_resolution: Optional[Dict[str, Any]] = None
    docker_engine_version: Optional[str] = None
    report_schema: str = "brainfrog.verification-report/1d"
    browser_backend: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "report_schema": self.report_schema,
            "verifier_version": self.verifier_version,
            "created_at": self.created_at,
            "repository": self.repository,
            "git_head_sha": self.git_head_sha,
            # Compatibility alias: identical to snapshot_identity.live_workspace_pre_snapshot_digest
            # (Git HEAD + tracked diff + untracked files + spec hash, sampled before execution).
            # It is NOT the digest of the bytes executed; see materialized_source_snapshot_digest.
            "source_snapshot_digest": self.source_snapshot_digest,
            "snapshot_identity": {
                "repository_head_sha": self.git_head_sha,
                "live_workspace_pre_snapshot_digest": self.source_snapshot_digest,
                "materialized_source_snapshot_digest": self.materialized_source_snapshot_digest,
                "materialized_file_count": self.materialized_file_count,
                "live_workspace_post_snapshot_digest": self.live_workspace_post_snapshot_digest,
                "snapshot_error": self.snapshot_error,
            },
            "run_evidence_files": self.run_evidence_files,
            "run_evidence_hashes": self.run_evidence_hashes,
            "report_md_sha256": self.report_md_sha256,
            "container_image_resolution": self.container_image_resolution,
            "docker_engine_version": self.docker_engine_version,
            "browser_backend": self.browser_backend,
            "workspace_status": {
                "is_dirty": self.is_dirty,
                "dirty_files": self.dirty_files,
            },
            "workspace_drift_detected": self.workspace_drift_detected,
            "spec_path": self.spec_path,
            "spec_sha256": self.spec_sha256,
            "execution_trust_mode": self.execution_trust_mode,
            "execution_backend": self.execution_backend,
            "authorization_reference": self.authorization_reference,
            "overall_verdict": self.overall_verdict.value,
            "exit_code": self.exit_code,
            "summary": self.summary.to_dict(),
            "goal": self.goal,
            "non_goals": self.non_goals,
            "constraints": self.constraints,
            "criteria": [c.to_dict() for c in self.criteria],
        }


# =============================================================================
# SPEC PARSER
# =============================================================================

def parse_spec_markdown(content: str, spec_path: Optional[Union[str, Path]] = None) -> SpecDocument:
    """Parse a Markdown specification document into structured SpecDocument.

    Enforces invariants:
    - Unique criterion IDs (e.g. AC-01, AC-02). Duplicate IDs raise ValueError.
    - Non-empty criteria list. Empty criteria raise ValueError.
    - Explicit verification method extraction.
    """
    if not content or not content.strip():
        raise ValueError("Specification document is empty.")

    clean_content = content.replace("\r\n", "\n")
    content_hash = hashlib.sha256(clean_content.encode("utf-8")).hexdigest()

    title = "Untitled Spec"
    goal_lines: List[str] = []
    non_goals: List[str] = []
    constraints: List[str] = []
    criteria: List[CriterionSpec] = []

    lines = clean_content.splitlines()
    current_section: Optional[str] = None
    current_ac: Optional[Dict[str, Any]] = None
    seen_ids: Set[str] = set()
    in_browser_plan: bool = False
    in_code_fence: bool = False
    plan_lines: List[str] = []

    def commit_current_ac() -> None:
        nonlocal in_browser_plan, in_code_fence, plan_lines
        if not current_ac:
            return
        cid = current_ac.get("id", "").strip()
        if not cid:
            return
        if cid in seen_ids:
            raise ValueError(f"Duplicate acceptance criterion ID '{cid}' in specification.")
        seen_ids.add(cid)

        # Finalize any pending browser plan collection
        if plan_lines:
            raw_plan_str = "\n".join(plan_lines).strip()
            current_ac["browser_plan_raw"] = raw_plan_str
            try:
                current_ac["browser_plan"] = parse_browser_plan(raw_plan_str)
            except Exception as pe:
                current_ac["browser_plan_error"] = str(pe)
            plan_lines = []
            in_browser_plan = False
            in_code_fence = False

        raw_vtype = (current_ac.get("verification_type") or "").strip().lower()
        vtype = "unsupported"
        if "build" in raw_vtype:
            vtype = "build"
        elif "test" in raw_vtype or "unit" in raw_vtype or "integration" in raw_vtype:
            vtype = "test"
        elif "browser" in raw_vtype or "ui" in raw_vtype or "frontend" in raw_vtype:
            vtype = "browser"

        desc = "\n".join(current_ac.get("desc_lines", [])).strip()
        criteria.append(
            CriterionSpec(
                criterion_id=cid,
                title=current_ac.get("title", cid).strip(),
                description=desc,
                verification_type=vtype,
                command=current_ac.get("command"),
                target=current_ac.get("target"),
                browser_plan=current_ac.get("browser_plan"),
                browser_plan_raw=current_ac.get("browser_plan_raw"),
                browser_plan_error=current_ac.get("browser_plan_error"),
                app_profile=current_ac.get("app_profile"),
            )
        )

    for line in lines:
        stripped = line.strip()

        # Document Title
        if stripped.startswith("# ") and not stripped.startswith("##"):
            m = re.match(r"^#\s*(?:Spec:\s*)?(.*)", stripped, re.IGNORECASE)
            if m:
                title = m.group(1).strip()
            continue

        # Level 2 Sections
        if stripped.startswith("## "):
            sec_header = stripped[3:].strip().lower()
            commit_current_ac()
            current_ac = None

            if "goal" in sec_header and "non" not in sec_header:
                current_section = "goal"
            elif "non-goal" in sec_header or "nongoal" in sec_header:
                current_section = "non_goals"
            elif "criteria" in sec_header or "acceptance" in sec_header:
                current_section = "criteria"
            elif "constraint" in sec_header:
                current_section = "constraints"
            else:
                current_section = "other"
            continue

        # Handle Sections
        if current_section == "goal":
            if stripped and not stripped.startswith("#"):
                goal_lines.append(stripped)
        elif current_section == "non_goals":
            if stripped.startswith(("-", "*")):
                item = stripped.lstrip("-* ").strip()
                if item:
                    non_goals.append(item)
        elif current_section == "constraints":
            if stripped.startswith(("-", "*")):
                item = stripped.lstrip("-* ").strip()
                if item:
                    constraints.append(item)
        elif current_section == "criteria":
            # Handle browser plan code blocks inside criteria
            if in_browser_plan and current_ac:
                if stripped.startswith("```"):
                    if in_code_fence:
                        # Close code fence and parse plan
                        in_code_fence = False
                        in_browser_plan = False
                        raw_plan_str = "\n".join(plan_lines).strip()
                        current_ac["browser_plan_raw"] = raw_plan_str
                        try:
                            current_ac["browser_plan"] = parse_browser_plan(raw_plan_str)
                        except Exception as pe:
                            current_ac["browser_plan_error"] = str(pe)
                        plan_lines = []
                        continue
                    else:
                        in_code_fence = True
                        continue
                elif in_code_fence:
                    plan_lines.append(line)
                    continue

            # Acceptance Criterion Header: ### AC-01 — Title or ### AC-01: Title
            if stripped.startswith("### "):
                commit_current_ac()
                in_browser_plan = False
                in_code_fence = False
                plan_lines = []
                ac_line = stripped[4:].strip()
                m_ac = re.match(r"^([A-Za-z0-9_-]+)\s*(?:[—–:]|\s+-\s+)\s*(.*)$", ac_line)
                if m_ac:
                    ac_id = m_ac.group(1).strip()
                    ac_title = m_ac.group(2).strip() or ac_id
                else:
                    tokens = ac_line.split(maxsplit=1)
                    ac_id = tokens[0].strip()
                    ac_title = tokens[1].strip() if len(tokens) > 1 else ac_id
                current_ac = {
                    "id": ac_id,
                    "title": ac_title,
                    "desc_lines": [],
                    "verification_type": "",
                    "command": None,
                    "target": None,
                }
                continue

            if current_ac:
                v_match = re.match(r"^\*\*(?:Verification|Method):\*\*\s*(.*)", stripped, re.IGNORECASE)
                if v_match:
                    current_ac["verification_type"] = v_match.group(1).strip()
                    continue

                cmd_match = re.match(r"^\*\*(?:Command|Cmd):\*\*\s*(.*)", stripped, re.IGNORECASE)
                if cmd_match:
                    current_ac["command"] = cmd_match.group(1).strip()
                    continue

                tgt_match = re.match(r"^\*\*(?:Target|Url|Path):\*\*\s*(.*)", stripped, re.IGNORECASE)
                if tgt_match:
                    current_ac["target"] = tgt_match.group(1).strip()
                    continue

                prof_match = re.match(r"^\*\*(?:App\s*Profile|Profile):\*\*\s*(.*)", stripped, re.IGNORECASE)
                if prof_match:
                    current_ac["app_profile"] = prof_match.group(1).strip()
                    continue

                bp_match = re.match(r"^\*\*(?:Browser\s*Plan|Plan):\*\*", stripped, re.IGNORECASE)
                if bp_match:
                    in_browser_plan = True
                    in_code_fence = False
                    plan_lines = []
                    continue

                if stripped:
                    current_ac["desc_lines"].append(stripped)

    commit_current_ac()

    if not criteria:
        raise ValueError("Specification contains no valid acceptance criteria (expected '### AC-XX').")

    return SpecDocument(
        title=title,
        goal="\n".join(goal_lines).strip(),
        non_goals=non_goals,
        criteria=criteria,
        constraints=constraints,
        raw_content=clean_content,
        content_hash=content_hash,
        spec_path=str(spec_path) if spec_path else None,
    )


# =============================================================================
# COMMAND AUTHORIZATION & EXECUTION BOUNDS
# =============================================================================

ALLOWED_VERIFICATION_BINARIES: Set[str] = {
    "pytest",
    "python",
    "python3",
    "unittest",
    "npm",
    "npx",
    "pnpm",
    "yarn",
    "node",
    "cargo",
    "go",
    "make",
}

DISALLOWED_TOKENS: Set[str] = {
    "&&",
    "||",
    ";",
    "|",
    ">",
    "<",
    "`",
    "$(",
    "eval",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
    "cmd",
    "cmd.exe",
    "bash",
    "sh",
    "zsh",
    "wscript",
    "cscript",
    "mshta",
}


def validate_verification_command(
    cmd_tokens: List[str],
    cwd: Path,
) -> Tuple[bool, Optional[str]]:
    """Validate verification command against security allowlist and boundaries."""
    if not cmd_tokens:
        return False, "Command is empty"

    raw_exe = cmd_tokens[0].lower().strip()
    clean_exe = Path(raw_exe).name
    if clean_exe.endswith(".exe"):
        clean_exe = clean_exe[:-4]

    # Prohibit shell wrapper injection & metacharacters
    for token in cmd_tokens:
        if token in DISALLOWED_TOKENS or any(dt in token for dt in ("&&", "||", ";", "|", "`", "$(", "\0")):
            return False, f"Command contains disallowed token or shell chaining: '{token}'"

    # Absolute path execution check
    if os.path.isabs(cmd_tokens[0]):
        # Only allow absolute path if it points to the current active Python executable
        active_py = Path(sys.executable).resolve()
        candidate = Path(cmd_tokens[0]).resolve()
        if candidate != active_py:
            return False, f"Direct invocation of non-system absolute executable '{cmd_tokens[0]}' is disallowed"

    if clean_exe not in ALLOWED_VERIFICATION_BINARIES:
        return False, f"Unauthorized executable: '{clean_exe}' is not in the verification allowlist ({sorted(ALLOWED_VERIFICATION_BINARIES)})"

    # Path traversal check on arguments
    real_cwd = Path(cwd).resolve()
    for token in cmd_tokens[1:]:
        if ".." in token:
            # Check if this resolves outside repository
            try:
                candidate = (real_cwd / token).resolve()
                if not candidate.is_relative_to(real_cwd):
                    return False, f"Path traversal attempt in argument: '{token}'"
            except (ValueError, Exception):
                return False, f"Invalid or traversing path argument: '{token}'"

    # Python interpreter bounds
    if clean_exe in ("python", "python3"):
        if "-c" in cmd_tokens:
            return False, "Python inline command execution (-c) is prohibited in verification commands"
        if "-i" in cmd_tokens:
            return False, "Python interactive mode (-i) is prohibited in verification commands"
        if "-m" in cmd_tokens:
            idx = cmd_tokens.index("-m")
            if idx + 1 < len(cmd_tokens):
                mod = cmd_tokens[idx + 1].lower()
                if mod not in ("pytest", "unittest", "py_compile", "compileall"):
                    return False, f"Unauthorized Python module 'python -m {mod}' (only pytest, unittest, py_compile, compileall allowed)"
            else:
                return False, "Missing module name following 'python -m'"

    # NPM / NPX bounds
    if clean_exe in ("npm", "pnpm", "yarn", "npx"):
        sub_action = cmd_tokens[1].lower() if len(cmd_tokens) > 1 else ""
        if sub_action not in ("test", "run", "build", "exec"):
            return False, f"Unauthorized npm subcommand '{sub_action}'; only test, run, and build are permitted"

    return True, None


# =============================================================================
# SOURCE SNAPSHOT DIGEST & DRIFT DETECTION
# =============================================================================

def compute_source_snapshot_digest(repo_dir: Path, spec_hash: str, exclude_dirs: Optional[List[str]] = None) -> str:
    """Compute deterministic composite digest binding HEAD, tracked diff, untracked inputs, and spec."""
    h = hashlib.sha256()
    head_sha, _ = sample_git_state(repo_dir)
    h.update(f"GIT_HEAD:{head_sha or 'none'}\n".encode("utf-8"))

    # Hash tracked diff
    try:
        diff_proc = run_hardened_subprocess(["git", "diff", "HEAD"], cwd=repo_dir, timeout=10.0)
        diff_text = diff_proc.stdout or ""
    except Exception:
        diff_text = ""
    h.update(f"TRACKED_DIFF:{hashlib.sha256(diff_text.encode('utf-8')).hexdigest()}\n".encode("utf-8"))

    # Hash untracked files (excluding .brainfrog, scratch, .git)
    untracked_lines: List[str] = []
    try:
        st_proc = run_hardened_subprocess(["git", "status", "--porcelain", "-uall"], cwd=repo_dir, timeout=10.0)
        for line in (st_proc.stdout or "").splitlines():
            if line.startswith("??"):
                rel_file = line[3:].strip()
                clean_rel = rel_file.replace("\\", "/")
                skip_prefixes = [".brainfrog", "scratch", ".git", ".tmp", "__pycache__", ".pytest_cache", ".coverage"]
                if exclude_dirs:
                    for ed in exclude_dirs:
                        skip_prefixes.append(ed.replace("\\", "/").lstrip("/"))
                if any(clean_rel == sp or clean_rel.startswith(sp.rstrip("/") + "/") for sp in skip_prefixes) or rel_file.endswith((".pyc", ".pyo")) or "/__pycache__/" in clean_rel:
                    continue
                f_path = repo_dir / rel_file
                if f_path.is_file():
                    try:
                        f_hash = hashlib.sha256(f_path.read_bytes()).hexdigest()
                        untracked_lines.append(f"{rel_file}:{f_hash}")
                    except Exception:
                        pass
    except Exception:
        pass

    untracked_lines.sort()
    untracked_blob = "\n".join(untracked_lines)
    h.update(f"UNTRACKED:{hashlib.sha256(untracked_blob.encode('utf-8')).hexdigest()}\n".encode("utf-8"))
    h.update(f"SPEC_HASH:{spec_hash}\n".encode("utf-8"))
    return h.hexdigest()


# =============================================================================
# REPORT INTEGRITY VERIFIER
# =============================================================================

def _bundle_file(report_root: Path, rel_path: Any) -> Optional[Path]:
    """Resolve an evidence path recorded in report.json, only if it stays inside the bundle."""
    if not isinstance(rel_path, str) or not rel_path or Path(rel_path).is_absolute() or ":" in rel_path:
        return None
    root = report_root.resolve()
    candidate = (root / rel_path).resolve()
    return candidate if candidate != root and candidate.is_relative_to(root) else None


def _report_structure_findings(manifest: Any) -> List[str]:
    """Reject report.json documents whose shape the integrity checks cannot safely interpret."""
    if not isinstance(manifest, dict):
        return ["report.json is not a JSON object."]
    out: List[str] = []
    criteria = manifest.get("criteria", [])
    if not isinstance(criteria, list) or not all(isinstance(c, dict) for c in criteria):
        return ["report.json 'criteria' must be a list of objects."]

    def _str_map(value: Any) -> bool:
        return isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())

    for i, crit in enumerate(criteria):
        for key in ("evidence_files", "evidence_hashes"):
            if key in crit and not _str_map(crit[key]):
                out.append(f"Criterion #{i + 1}: '{key}' must map names to strings.")
    for key in ("run_evidence_files", "run_evidence_hashes"):
        if manifest.get(key) is not None and not _str_map(manifest[key]):
            out.append(f"report.json '{key}' must map names to strings.")
    if manifest.get("snapshot_identity") is not None and not isinstance(manifest["snapshot_identity"], dict):
        out.append("report.json 'snapshot_identity' must be an object.")
    if manifest.get("report_md_sha256") is not None and not isinstance(manifest["report_md_sha256"], str):
        out.append("report.json 'report_md_sha256' must be a string.")
    return out


def verify_report_integrity(report_dir: Union[str, Path]) -> Tuple[bool, List[str]]:
    """Validate evidence authenticity and cryptographic integrity of a verification report.

    Checks:
    1. report.json exists and adheres to schema.
    2. Every recorded evidence file exists on disk.
    3. Every evidence file's SHA-256 matches its recorded hash.
    4. No extra unauthorized files exist in evidence directories.
    5. Detects missing or tampered files.
    """
    p = Path(report_dir)
    findings: List[str] = []

    if not p.is_dir():
        return False, [f"Report directory '{report_dir}' does not exist or is not a directory."]

    json_path = p / "report.json"
    if not json_path.is_file():
        return False, [f"Missing manifest file 'report.json' in '{report_dir}'."]

    try:
        manifest = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as err:
        return False, [f"Failed to parse 'report.json': {err}"]

    structure = _report_structure_findings(manifest)
    if structure:
        return False, structure

    criteria = manifest.get("criteria", [])
    expected_evidence_files: Set[str] = set()

    for crit in criteria:
        cid = crit.get("criterion_id", "unknown")
        ev_files = crit.get("evidence_files", {})
        ev_hashes = crit.get("evidence_hashes", {})

        for fname, rel_path in ev_files.items():
            expected_evidence_files.add(str(Path(rel_path).as_posix()))
            disk_file = _bundle_file(p, rel_path)
            if disk_file is None:
                findings.append(f"Criterion {cid}: Evidence path '{rel_path}' is outside the report bundle.")
                continue
            if not disk_file.is_file():
                findings.append(f"Criterion {cid}: Recorded evidence file '{rel_path}' is missing on disk.")
                continue

            expected_hash = ev_hashes.get(fname)
            if not expected_hash:
                findings.append(f"Criterion {cid}: Missing recorded hash for evidence file '{fname}'.")
                continue

            actual_hash = hashlib.sha256(disk_file.read_bytes()).hexdigest()
            if actual_hash != expected_hash:
                findings.append(
                    f"Criterion {cid}: Hash mismatch for '{rel_path}'. Recorded={expected_hash[:16]}..., Actual={actual_hash[:16]}..."
                )

    run_files = manifest.get("run_evidence_files") or {}
    run_hashes = manifest.get("run_evidence_hashes") or {}
    for fname, rel_path in run_files.items():
        expected_evidence_files.add(str(Path(rel_path).as_posix()))
        disk_file = _bundle_file(p, rel_path)
        if disk_file is None:
            findings.append(f"Run evidence path '{rel_path}' is outside the report bundle.")
            continue
        if not disk_file.is_file():
            findings.append(f"Run evidence file '{rel_path}' is missing on disk.")
            continue
        actual_hash = hashlib.sha256(disk_file.read_bytes()).hexdigest()
        if actual_hash != run_hashes.get(fname):
            findings.append(f"Run evidence hash mismatch for '{rel_path}'.")

    md_hash = manifest.get("report_md_sha256")
    if md_hash:
        md_file = p / "report.md"
        if not md_file.is_file():
            findings.append("report.md is missing.")
        elif hashlib.sha256(md_file.read_bytes()).hexdigest() != md_hash:
            findings.append("report.md hash does not match report_md_sha256.")

    identity = manifest.get("snapshot_identity") or {}
    materialized = identity.get("materialized_source_snapshot_digest")
    for crit in criteria:
        if crit.get("status") != "PASS" or crit.get("execution_backend") in (None, "none"):
            continue
        if crit.get("materialized_source_snapshot_digest") is None and materialized is None:
            continue  # pre-Phase-1C report without snapshot binding
        cid = crit.get("criterion_id", "unknown")
        if crit.get("materialized_source_snapshot_digest") != materialized:
            findings.append(f"Criterion {cid}: PASS bound to a different materialized snapshot than the report.")
        if crit.get("observed_source_snapshot_digest") != materialized:
            findings.append(f"Criterion {cid}: PASS without runner-observed digest matching the materialized snapshot.")

    for crit in criteria:
        findings.extend(_browser_evidence_findings(p, crit, materialized))

    # Check for unauthorized extra files in evidence/
    evidence_root = p / "evidence"
    if evidence_root.is_dir():
        for f in evidence_root.rglob("*"):
            if f.is_file():
                rel_posix = str(f.relative_to(p).as_posix())
                if rel_posix not in expected_evidence_files:
                    findings.append(f"Unauthorized extra file in evidence directory: '{rel_posix}'")

    is_valid = len(findings) == 0
    return is_valid, findings


BROWSER_REQUIRED_EVIDENCE = ("browser-assertions.json", "plan.json", "browser-run.json")


def _browser_evidence_findings(report_root: Path, crit: Dict[str, Any], materialized: Optional[str]) -> List[str]:
    """Cross-check executed browser criteria against their structured evidence.

    Applies only to criteria executed by the browser backend; Phase 1B/1C reports have none.
    """
    if crit.get("verification_type") != "browser" or crit.get("execution_backend") != "oci_browser_sandbox":
        return []
    cid = crit.get("criterion_id", "unknown")
    status = crit.get("status")
    files = crit.get("evidence_files") or {}
    hashes = crit.get("evidence_hashes") or {}
    out: List[str] = []
    if status not in ("PASS", "FAIL"):
        return out  # BLOCKED browser criteria carry whatever evidence was available
    for name in BROWSER_REQUIRED_EVIDENCE:
        if name not in files:
            out.append(f"Criterion {cid}: browser {status} without required evidence file '{name}'.")
    if out:
        return out

    def _load(name: str) -> Optional[Any]:
        f = _bundle_file(report_root, files[name])
        if f is None or not f.is_file() or hashlib.sha256(f.read_bytes()).hexdigest() != hashes.get(name):
            return None  # missing/tampered files are already reported by the generic hash check
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            out.append(f"Criterion {cid}: '{name}' is not valid JSON.")
            return None

    manifest = _load("browser-assertions.json")
    plan = _load("plan.json")
    if manifest is None or plan is None:
        return out
    if manifest.get("criterion_id") != cid:
        out.append(f"Criterion {cid}: browser assertion evidence references criterion '{manifest.get('criterion_id')}'.")
    if manifest.get("status") != status:
        out.append(f"Criterion {cid}: report status {status} != assertion evidence status {manifest.get('status')}.")
    plan_sha = hashlib.sha256(_bundle_file(report_root, files["plan.json"]).read_bytes()).hexdigest()
    if manifest.get("plan_sha256") != plan_sha or crit.get("browser_plan_sha256") != plan_sha:
        out.append(f"Criterion {cid}: browser plan hash binding mismatch.")
    if materialized is not None and manifest.get("source_snapshot_digest") != materialized:
        out.append(f"Criterion {cid}: browser evidence bound to a different source snapshot.")
    shot = manifest.get("screenshot") or {}
    if shot.get("captured"):
        rel = files.get("screenshot.png")
        if rel is None:
            out.append(f"Criterion {cid}: assertion evidence references a screenshot that is not in the report.")
        elif shot.get("sha256") != hashes.get("screenshot.png"):
            out.append(f"Criterion {cid}: screenshot hash differs between assertion evidence and report.")
    records = manifest.get("records") or []
    executed = [r for r in records if r.get("kind") == "assertion"]
    if any(r.get("criterion_id") != cid for r in records):
        out.append(f"Criterion {cid}: assertion records reference another criterion.")
    if status == "PASS":
        steps = plan.get("steps") or []
        if not executed or any(r.get("status") != "PASS" for r in records) or len(records) != len(steps):
            out.append(f"Criterion {cid}: browser PASS is not supported by fully passing executed assertions.")
        if not shot.get("captured") or "screenshot.png" not in files:
            out.append(f"Criterion {cid}: browser PASS without captured screenshot evidence.")
    elif not any(r.get("status") == "FAIL" for r in records):
        out.append(f"Criterion {cid}: browser FAIL without a failing step record.")
    if crit.get("assertion_count") != len(executed):
        out.append(f"Criterion {cid}: assertion count {crit.get('assertion_count')} != evidence ({len(executed)}).")
    return out


# =============================================================================
# OWNERSHIP-SAFE REPORT CLEANUP
# =============================================================================

def safe_cleanup_report(report_dir: Path, expected_run_id: str) -> bool:
    """Safely remove a designated report directory, enforcing strict run ID ownership."""
    rpath = Path(report_dir).resolve()
    if not rpath.exists():
        return True

    # Must match expected run ID name
    if rpath.name != expected_run_id:
        raise PermissionError(
            f"Ownership check failed: target directory '{rpath.name}' does not match expected run ID '{expected_run_id}'."
        )

    # Must be a child directory of a reports base, never the reports root or workspace root
    if rpath.parent.name != "reports" and not any(part.startswith("tmp") for part in rpath.parts):
        raise PermissionError(f"Refusing to delete directory '{rpath}' outside valid reports hierarchy.")

    shutil.rmtree(rpath, ignore_errors=True)
    return True


# =============================================================================
# STANDALONE VERIFIER ENGINE
# =============================================================================

class StandaloneVerifier:
    """Independent verification engine that evaluates spec.md without agent runtime."""

    def __init__(
        self,
        repo_dir: Path,
        timeout: float = 120.0,
        trust_local_code: Optional[bool] = None,
        execution_trust_mode: Optional[ExecutionTrustMode] = None,
        authorization_reference: Optional[str] = None,
        reports_base_dir: Optional[Path] = None,
        sandbox_mode: Optional[str] = None,
        sandbox_backend: Optional[VerificationExecutionBackend] = None,
        default_app_profile: str = "python-static",
        browser_backend: Optional[DockerBrowserSandbox] = None,
    ) -> None:
        self.repo_dir = Path(repo_dir).resolve()
        self.timeout = float(timeout)
        self.authorization_reference = str(authorization_reference) if authorization_reference else None
        self.sandbox_mode = sandbox_mode
        # Operator/API-supplied only; spec content has no path to this object.
        self.sandbox_backend = sandbox_backend
        self.default_app_profile = default_app_profile
        # Operator/API-supplied only, like sandbox_backend.
        self.browser_backend = browser_backend

        # Precedence: explicit trust mode > explicit operator trust > requested sandbox > fail closed.
        # trust_local_code=False with a sandbox request still uses the sandbox (it only declines host trust).
        if execution_trust_mode is not None:
            self.trust_mode = execution_trust_mode
        elif trust_local_code is True:
            self.trust_mode = ExecutionTrustMode.TRUSTED_OPERATOR
        elif sandbox_mode in ("auto", "docker"):
            self.trust_mode = ExecutionTrustMode.ISOLATED_SANDBOX
        else:
            self.trust_mode = ExecutionTrustMode.UNTRUSTED_FAIL_CLOSED

        self.trust_local_code = (self.trust_mode == ExecutionTrustMode.TRUSTED_OPERATOR)
        self.reports_base_dir = Path(reports_base_dir).resolve() if reports_base_dir else (self.repo_dir / ".brainfrog" / "reports")

    def _get_scrubbed_env(self) -> Dict[str, str]:
        """Produce an aggressively scrubbed environment for child processes."""
        safe_system_keys = {
            "PATH",
            "SYSTEMROOT",
            "SYSTEMDRIVE",
            "TEMP",
            "TMP",
            "COMSPEC",
            "PATHEXT",
            "WINDIR",
            "USERPROFILE",
            "HOMEDRIVE",
            "HOMEPATH",
            "LANG",
            "LC_ALL",
        }
        scrubbed: Dict[str, str] = {}
        for k, v in os.environ.items():
            k_upper = k.upper()
            if k_upper in safe_system_keys:
                scrubbed[k] = v
            # Drop credentials and tokens
            elif any(pat in k_upper for pat in ("KEY", "SECRET", "TOKEN", "AUTH", "PASS", "CREDENTIAL")):
                continue
            elif k_upper.startswith(("AWS_", "GITHUB_", "ANTHROPIC_", "OPENAI_", "TYPESAFE_", "GEMINI_", "GOOGLE_")):
                continue
            # Drop python injection vars
            elif k_upper in ("PYTHONPATH", "PYTHONHOME", "PYTEST_ADDOPTS", "LD_PRELOAD"):
                continue

        # Pin PYTHONPATH to target repository
        scrubbed["PYTHONPATH"] = str(self.repo_dir)
        return scrubbed

    def verify_spec(self, spec_path: Union[str, Path]) -> VerificationReport:
        """Execute independent acceptance verification against spec.md."""
        target_path = Path(spec_path)
        if not target_path.is_absolute():
            target_path = (self.repo_dir / target_path).resolve()

        # Path traversal guard
        try:
            target_path.relative_to(self.repo_dir)
        except ValueError:
            raise PermissionError(f"Specification path '{spec_path}' traverses outside workspace '{self.repo_dir}'")

        if not target_path.is_file():
            raise FileNotFoundError(f"Specification file '{spec_path}' not found.")

        raw_spec_content = target_path.read_text(encoding="utf-8")
        rel_spec_path = str(target_path.relative_to(self.repo_dir)).replace("\\", "/")
        spec_doc = parse_spec_markdown(raw_spec_content, spec_path=rel_spec_path)

        # 1. Sample Git provenance & Pre-Execution Source Snapshot
        git_head_sha, is_dirty = sample_git_state(self.repo_dir)
        dirty_files: List[str] = []
        try:
            st_res = run_hardened_subprocess(["git", "status", "--porcelain", "-uall"], cwd=self.repo_dir, timeout=5.0)
            if st_res.returncode == 0:
                dirty_files = [line[3:].strip() for line in st_res.stdout.splitlines() if line.strip()]
        except Exception:
            pass

        rel_reports_dir = str(self.reports_base_dir.resolve().relative_to(self.repo_dir.resolve())).replace("\\", "/") if self.reports_base_dir.resolve().is_relative_to(self.repo_dir.resolve()) else None
        excl_list = [rel_reports_dir] if rel_reports_dir else None
        pre_snapshot_digest = compute_source_snapshot_digest(self.repo_dir, spec_doc.content_hash, exclude_dirs=excl_list)

        # 2. Setup collision-resistant run identity & staging directory
        ts_slug = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        spec_short_hash = spec_doc.content_hash[:6]
        entropy_slug = secrets.token_hex(3)
        run_id = f"verify_{ts_slug}_{spec_short_hash}_{entropy_slug}"

        self.reports_base_dir.mkdir(parents=True, exist_ok=True)
        staging_dir = self.reports_base_dir / f".staging_{run_id}"
        staging_dir.mkdir(parents=True, exist_ok=True)
        evidence_root = staging_dir / "evidence"
        evidence_root.mkdir(parents=True, exist_ok=True)

        final_run_dir = self.reports_base_dir / run_id

        # 3. Determine Execution Trust Mode & Backend
        exec_backend: Optional[VerificationExecutionBackend] = None
        backend_unavailable_reason: Optional[str] = None
        image_resolution: Optional[Dict[str, Any]] = None
        engine_version: Optional[str] = None

        if self.trust_mode == ExecutionTrustMode.TRUSTED_OPERATOR:
            exec_trust_mode_str = "TRUSTED_OPERATOR"
            exec_backend = LocalTrustedExecutionBackend(self.repo_dir, timeout_seconds=self.timeout, runner=run_hardened_subprocess)
            exec_backend_str = "local_hardened_subprocess"
            auth_ref_str = self.authorization_reference or "operator_explicit_flag (--trust-local-code)"
        elif self.trust_mode == ExecutionTrustMode.ISOLATED_SANDBOX:
            exec_trust_mode_str = "ISOLATED_SANDBOX"
            docker_backend = self.sandbox_backend or DockerSandboxBackend()
            is_avail, backend_unavailable_reason = docker_backend.is_available()
            if is_avail and isinstance(docker_backend, DockerSandboxBackend):
                engine_version = docker_backend.engine_version()
                image_resolution = {"tag": docker_backend.image_name}
                try:
                    image_resolution = docker_backend.resolve_image()
                except Exception as img_err:  # recorded here; execute() re-checks and blocks
                    image_resolution["error"] = str(img_err)
            exec_backend = docker_backend if is_avail else None
            exec_backend_str = "oci_container_docker" if is_avail else "unavailable_sandbox"
            auth_ref_str = "container_isolation" if is_avail else None
        else:
            exec_trust_mode_str = "UNTRUSTED_FAIL_CLOSED"
            exec_backend_str = "none"
            auth_ref_str = None

        # Materialized once per run, lazily, outside the repository and outside reports.
        snapshot_state: Dict[str, Any] = {"manifest": None, "error": None, "root": None}

        def _ensure_snapshot() -> Optional[SnapshotManifest]:
            if snapshot_state["manifest"] is None and snapshot_state["error"] is None:
                root = Path(tempfile.mkdtemp(prefix="bf_snapshot_"))
                snapshot_state["root"] = root
                try:
                    snapshot_state["manifest"] = materialize_source_snapshot_with_manifest(
                        repo_dir=self.repo_dir,
                        target_snapshot_dir=root / "source",
                        exclude_dirs=excl_list,
                    )
                except (SnapshotError, OSError) as snap_err:
                    snapshot_state["error"] = f"Source snapshot could not be created faithfully: {snap_err}"
            return snapshot_state["manifest"]

        def _blocked(crit: CriterionSpec, crit_dir: Path, started_at: str, t0: float, reason_text: str,
                     fname: str, category: Optional[str], backend: Optional[str], command: Optional[str]) -> CriterionResult:
            reason_bytes = reason_text.encode("utf-8")
            reason_file = crit_dir / fname
            reason_file.write_bytes(reason_bytes)
            return CriterionResult(
                criterion_id=crit.criterion_id,
                title=crit.title,
                description=crit.description,
                verification_type=crit.verification_type,
                status=CriterionStatus.BLOCKED,
                command=command,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc).isoformat(),
                failure_reason=reason_text,
                execution_backend=backend,
                infrastructure_error_category=category,
                evidence_files={fname: str(reason_file.relative_to(staging_dir)).replace("\\", "/")},
                evidence_hashes={fname: hashlib.sha256(reason_bytes).hexdigest()},
            )

        # 4. Execute criteria checks
        criteria_results: List[CriterionResult] = []
        browser_state: Dict[str, Any] = {}

        try:
            self._execute_criteria(
                spec_doc, evidence_root, staging_dir, criteria_results, _blocked, _ensure_snapshot,
                snapshot_state, exec_backend, exec_backend_str, backend_unavailable_reason, run_id,
                browser_state=browser_state,
            )
        finally:
            if snapshot_state["root"] is not None:
                shutil.rmtree(snapshot_state["root"], ignore_errors=True)

        manifest_final: Optional[SnapshotManifest] = snapshot_state["manifest"]
        run_evidence_files: Dict[str, str] = {}
        run_evidence_hashes: Dict[str, str] = {}
        if manifest_final is not None:
            run_dir = evidence_root / "_run"
            run_dir.mkdir(parents=True, exist_ok=True)
            manifest_bytes = json.dumps(manifest_final.to_dict(), indent=2, sort_keys=True).encode("utf-8")
            (run_dir / "snapshot_manifest.json").write_bytes(manifest_bytes)
            run_evidence_files["snapshot_manifest.json"] = "evidence/_run/snapshot_manifest.json"
            run_evidence_hashes["snapshot_manifest.json"] = hashlib.sha256(manifest_bytes).hexdigest()

        # 5. Post-Execution Workspace Drift Detection
        post_snapshot_digest = compute_source_snapshot_digest(self.repo_dir, spec_doc.content_hash, exclude_dirs=excl_list)
        workspace_drift = (pre_snapshot_digest != post_snapshot_digest)

        # Check if spec itself mutated
        try:
            current_spec_hash = hashlib.sha256(target_path.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8")).hexdigest()
            if current_spec_hash != spec_doc.content_hash:
                workspace_drift = True
        except Exception:
            workspace_drift = True

        # 6. Compute overall verdict & exit code
        passed_cnt = sum(1 for c in criteria_results if c.status == CriterionStatus.PASS)
        failed_cnt = sum(1 for c in criteria_results if c.status == CriterionStatus.FAIL)
        blocked_cnt = sum(1 for c in criteria_results if c.status == CriterionStatus.BLOCKED)
        not_run_cnt = sum(1 for c in criteria_results if c.status == CriterionStatus.NOT_RUN)
        total_cnt = len(criteria_results)

        # If drift detected, any overall PASS must be invalidated
        if workspace_drift:
            overall = OverallVerdict.BLOCKED
            exit_code = 2
        elif failed_cnt > 0:
            overall = OverallVerdict.FAIL
            exit_code = 1
        elif blocked_cnt > 0 or not_run_cnt > 0:
            overall = OverallVerdict.BLOCKED
            exit_code = 2
        elif passed_cnt == total_cnt and total_cnt > 0:
            overall = OverallVerdict.PASS
            exit_code = 0
        else:
            overall = OverallVerdict.BLOCKED
            exit_code = 2

        summary = VerificationSummary(
            total=total_cnt,
            passed=passed_cnt,
            failed=failed_cnt,
            blocked=blocked_cnt,
            not_run=not_run_cnt,
        )

        report = VerificationReport(
            run_id=run_id,
            verifier_version="0.1.0-alpha",
            created_at=datetime.now(timezone.utc).isoformat(),
            repository=self.repo_dir.name,
            git_head_sha=git_head_sha,
            source_snapshot_digest=pre_snapshot_digest,
            is_dirty=is_dirty,
            dirty_files=dirty_files,
            workspace_drift_detected=workspace_drift,
            spec_path=rel_spec_path,
            spec_sha256=spec_doc.content_hash,
            execution_trust_mode=exec_trust_mode_str,
            execution_backend=exec_backend_str,
            authorization_reference=auth_ref_str,
            overall_verdict=overall,
            summary=summary,
            criteria=criteria_results,
            goal=spec_doc.goal,
            non_goals=spec_doc.non_goals,
            constraints=spec_doc.constraints,
            report_dir=final_run_dir,
            exit_code=exit_code,
            live_workspace_post_snapshot_digest=post_snapshot_digest,
            materialized_source_snapshot_digest=manifest_final.digest if manifest_final else None,
            materialized_file_count=len(manifest_final.files) if manifest_final else None,
            snapshot_error=snapshot_state["error"],
            run_evidence_files=run_evidence_files,
            run_evidence_hashes=run_evidence_hashes,
            container_image_resolution=image_resolution,
            docker_engine_version=engine_version or browser_state.get("engine_version"),
            browser_backend=browser_state.get("summary"),
        )

        # 7. Write structured reports into staging directory (report.md first so its hash is bound in report.json)
        md_bytes = self._format_markdown_report(report).encode("utf-8")
        (staging_dir / "report.md").write_bytes(md_bytes)
        report.report_md_sha256 = hashlib.sha256(md_bytes).hexdigest()

        json_path = staging_dir / "report.json"
        json_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")

        # 8. Atomically finalize report directory
        if final_run_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise FileExistsError(f"Collision detected: report directory '{final_run_dir}' already exists.")

        try:
            staging_dir.rename(final_run_dir)
        except Exception:
            # Fallback for cross-filesystem renames
            shutil.copytree(staging_dir, final_run_dir)
            shutil.rmtree(staging_dir, ignore_errors=True)

        return report

    def _execute_criteria(
        self,
        spec_doc: SpecDocument,
        evidence_root: Path,
        staging_dir: Path,
        criteria_results: List[CriterionResult],
        _blocked: Any,
        _ensure_snapshot: Any,
        snapshot_state: Dict[str, Any],
        exec_backend: Optional[VerificationExecutionBackend],
        exec_backend_str: str,
        backend_unavailable_reason: Optional[str],
        run_id: str,
        browser_state: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Evaluate every criterion, appending one CriterionResult each."""
        browser_state = browser_state if browser_state is not None else {}
        for crit in spec_doc.criteria:
            crit_dir = evidence_root / crit.criterion_id
            crit_dir.mkdir(parents=True, exist_ok=True)

            started_at = datetime.now(timezone.utc).isoformat()
            t0 = time.perf_counter()

            if crit.verification_type in ("build", "test"):
                if not crit.command:
                    res = _blocked(crit, crit_dir, started_at, t0, "Missing verification command in spec definition.",
                                   "reason.txt", None, None, None)
                    res.failure_reason = "Missing verification command"
                    criteria_results.append(res)
                    continue

                # Tokenize command
                try:
                    cmd_tokens = shlex.split(crit.command, posix=os.name != "nt")
                except ValueError as sh_err:
                    criteria_results.append(_blocked(crit, crit_dir, started_at, t0, f"Malformed command syntax: {sh_err}",
                                                     "policy_violation.txt", None, None, crit.command))
                    continue

                # Authorize command syntax and policy
                is_allowed, deny_reason = validate_verification_command(cmd_tokens, cwd=self.repo_dir)
                if not is_allowed:
                    criteria_results.append(_blocked(crit, crit_dir, started_at, t0, f"Command policy violation: {deny_reason}",
                                                     "policy_violation.txt", None, None, crit.command))
                    continue

                # Check Execution Trust Boundary
                if self.trust_mode == ExecutionTrustMode.UNTRUSTED_FAIL_CLOSED:
                    criteria_results.append(_blocked(
                        crit, crit_dir, started_at, t0,
                        "Secure execution backend unavailable for untrusted repository code. "
                        "Local unconfined execution requires explicit operator authorization via --trust-local-code.",
                        "trust_policy_block.txt", None, "none", crit.command))
                    continue

                # Sandbox requested but unavailable: BLOCKED, never a host fallback.
                if exec_backend is None:
                    criteria_results.append(_blocked(
                        crit, crit_dir, started_at, t0,
                        f"Isolated container sandbox backend unavailable: {backend_unavailable_reason or 'Docker offline'}. "
                        "Local execution requires explicit operator authorization via --trust-local-code.",
                        "trust_policy_block.txt", InfraErrorCategory.DOCKER_UNAVAILABLE, "unavailable_sandbox", crit.command))
                    continue

                # Only runners with a structured BrainFrog result adapter can establish PASS.
                if classify_runner(cmd_tokens) is None:
                    criteria_results.append(_blocked(
                        crit, crit_dir, started_at, t0,
                        f"Unsupported verification runner '{' '.join(cmd_tokens[:3])}': no structured result adapter, "
                        "so a zero exit status would not establish that this criterion was verified. Not executed.",
                        "unsupported_runner.txt", InfraErrorCategory.UNSUPPORTED_RUNNER, exec_backend_str, crit.command))
                    continue

                manifest = _ensure_snapshot()
                if manifest is None:
                    criteria_results.append(_blocked(
                        crit, crit_dir, started_at, t0, snapshot_state["error"] or "Source snapshot unavailable.",
                        "snapshot_error.txt", InfraErrorCategory.SNAPSHOT_ERROR, exec_backend_str, crit.command))
                    continue

                exec_res = exec_backend.execute(
                    source_snapshot_dir=manifest.snapshot_dir,
                    source_snapshot_digest=manifest.digest,
                    approved_argv=cmd_tokens,
                    policy=SandboxPolicy(),
                    limits=SandboxResourceLimits(timeout_seconds=self.timeout),
                    env=self._get_scrubbed_env(),
                    run_label=run_id,
                )

                ev_files: Dict[str, str] = {}
                ev_hashes: Dict[str, str] = {}

                def _write_evidence(name: str, data: bytes) -> None:
                    path = crit_dir / name
                    path.write_bytes(data)
                    ev_files[name] = str(path.relative_to(staging_dir)).replace("\\", "/")
                    ev_hashes[name] = hashlib.sha256(data).hexdigest()

                stdout_clean = redact_secrets(exec_res.stdout or "")
                stderr_clean = redact_secrets(exec_res.stderr or "")
                _write_evidence("stdout.log", stdout_clean.encode("utf-8"))
                _write_evidence("stderr.log", stderr_clean.encode("utf-8"))

                outcome = exec_res.outcome
                if outcome is None:  # defensive: backends always attach an outcome
                    outcome = TestResultAdapter.parse_outcome(cmd_tokens, 1, "", "")
                if outcome.runner_record is not None:
                    _write_evidence("runner_record.json",
                                    json.dumps(outcome.runner_record, indent=2, sort_keys=True).encode("utf-8"))
                if exec_res.effective_container_config is not None:
                    _write_evidence("container_config.json",
                                    json.dumps(exec_res.effective_container_config, indent=2, sort_keys=True).encode("utf-8"))

                status = CriterionStatus(outcome.verdict)
                fail_msg = outcome.reason
                if status == CriterionStatus.PASS and outcome.observed_source_digest != manifest.digest:
                    status = CriterionStatus.BLOCKED
                    fail_msg = "Runner-observed source digest does not match the materialized snapshot."
                if status == CriterionStatus.FAIL and exec_res.exit_code not in (None, 0):
                    fail_msg = f"Command exited with non-zero status {exec_res.exit_code}: {fail_msg}"

                criteria_results.append(
                    CriterionResult(
                        criterion_id=crit.criterion_id,
                        title=crit.title,
                        description=crit.description,
                        verification_type=crit.verification_type,
                        status=status,
                        command=crit.command,
                        actual_executed_argv=exec_res.executed_argv,
                        exit_code=exec_res.exit_code,
                        duration_ms=(time.perf_counter() - t0) * 1000.0,
                        started_at=started_at,
                        finished_at=datetime.now(timezone.utc).isoformat(),
                        stdout=stdout_clean,
                        stderr=stderr_clean,
                        evidence_files=ev_files,
                        evidence_hashes=ev_hashes,
                        failure_reason=fail_msg,
                        test_count=outcome.test_count,
                        test_failure_count=outcome.failure_count,
                        execution_backend=exec_res.backend_type,
                        container_image=exec_res.container_image,
                        container_image_digest=exec_res.container_image_digest,
                        sandbox_policy=exec_res.sandbox_policy_summary,
                        infrastructure_error_category=outcome.infrastructure_error_category
                        if exec_res.infrastructure_error_category is None else exec_res.infrastructure_error_category,
                        test_skipped_count=outcome.skipped_count,
                        result_authoritative=outcome.authoritative,
                        materialized_source_snapshot_digest=manifest.digest,
                        observed_source_snapshot_digest=outcome.observed_source_digest,
                        container_id=exec_res.container_id,
                        container_name=exec_res.container_name,
                        effective_container_config=exec_res.effective_container_config,
                        harness_sha256=exec_res.harness_sha256,
                        output_truncated=exec_res.output_truncated,
                    )
                )

            elif crit.verification_type == "browser":
                criteria_results.append(self._execute_browser_criterion(
                    crit, crit_dir, staging_dir, started_at, t0, _blocked, _ensure_snapshot, snapshot_state,
                    run_id, browser_state))

            else:
                dur = (time.perf_counter() - t0) * 1000.0
                finished_at = datetime.now(timezone.utc).isoformat()
                reason_text = f"Unsupported verification type '{crit.verification_type}'"
                criteria_results.append(
                    CriterionResult(
                        criterion_id=crit.criterion_id,
                        title=crit.title,
                        description=crit.description,
                        verification_type=crit.verification_type,
                        status=CriterionStatus.BLOCKED,
                        duration_ms=dur,
                        started_at=started_at,
                        finished_at=finished_at,
                        failure_reason=reason_text,
                    )
                )

    def _execute_browser_criterion(
        self,
        crit: CriterionSpec,
        crit_dir: Path,
        staging_dir: Path,
        started_at: str,
        t0: float,
        _blocked: Any,
        _ensure_snapshot: Any,
        snapshot_state: Dict[str, Any],
        run_id: str,
        browser_state: Dict[str, Any],
    ) -> CriterionResult:
        """Run one declarative browser criterion in the two-container OCI backend.

        There is no host-browser fallback: every path that cannot use isolated containers
        is BLOCKED, regardless of trust mode.
        """
        if crit.browser_plan_error:
            return _blocked(crit, crit_dir, started_at, t0, f"Invalid declarative browser plan: {crit.browser_plan_error}",
                            "browser_plan_error.txt", "invalid_browser_plan", None, None)
        plan = crit.browser_plan
        if plan is None:
            return _blocked(crit, crit_dir, started_at, t0,
                            "Browser criterion has no declarative browser plan (**Browser Plan:** JSON block); "
                            "nothing can be verified.", "browser_status.txt", "missing_browser_plan", None, None)
        if not plan.has_meaningful_assertions:  # defensive: the parser already rejects these
            return _blocked(crit, crit_dir, started_at, t0, "False browser proof rejected: plan contains zero assertions.",
                            "false_proof_rejected.txt", "invalid_browser_plan", None, None)
        if self.trust_mode == ExecutionTrustMode.UNTRUSTED_FAIL_CLOSED:
            return _blocked(crit, crit_dir, started_at, t0,
                            "Browser verification requires the isolated OCI backend (--sandbox auto|docker); "
                            "untrusted application code is never served or browsed on the host.",
                            "trust_policy_block.txt", None, "none", None)

        if "backend" not in browser_state:
            backend = self.browser_backend or DockerBrowserSandbox()
            avail, reason = backend.is_available()
            browser_state["backend"] = backend if avail else None
            browser_state["unavailable_reason"] = reason
            browser_state["summary"] = {"backend": "oci_browser_sandbox" if avail else "unavailable",
                                        "browser_image": backend.browser_image, "unavailable_reason": reason}
            if avail:
                browser_state["engine_version"] = backend.engine_version()
        backend = browser_state["backend"]
        if backend is None:
            reason = browser_state["unavailable_reason"] or "Docker offline"
            return _blocked(crit, crit_dir, started_at, t0,
                            f"Isolated browser verification backend unavailable: {reason}",
                            "trust_policy_block.txt", InfraErrorCategory.DOCKER_UNAVAILABLE, "unavailable_sandbox", None)

        manifest = _ensure_snapshot()
        if manifest is None:
            return _blocked(crit, crit_dir, started_at, t0, snapshot_state["error"] or "Source snapshot unavailable.",
                            "snapshot_error.txt", InfraErrorCategory.SNAPSHOT_ERROR, "oci_browser_sandbox", None)

        profile_id = crit.app_profile or self.default_app_profile
        b_res: BrowserExecutionResult = backend.execute_browser_criterion(
            criterion_id=crit.criterion_id,
            browser_plan=plan,
            source_snapshot_dir=manifest.snapshot_dir,
            source_snapshot_digest=manifest.digest,
            app_profile_id=profile_id,
            run_id=run_id,
        )

        ev_files: Dict[str, str] = {}
        ev_hashes: Dict[str, str] = {}

        def _write_evidence(name: str, data: bytes) -> None:
            path = crit_dir / name
            path.write_bytes(data)
            ev_files[name] = str(path.relative_to(staging_dir)).replace("\\", "/")
            ev_hashes[name] = hashlib.sha256(data).hexdigest()

        _write_evidence("plan.json", plan.canonical_json().encode("utf-8"))
        for name in sorted(b_res.artifacts):
            data = b_res.artifacts[name]
            if name.endswith(".log"):
                data = redact_secrets(data.decode("utf-8", errors="replace")).encode("utf-8")
            _write_evidence(name, data)
        _write_evidence("browser-run.json", json.dumps(b_res.run_metadata(), indent=2, sort_keys=True).encode("utf-8"))

        status = CriterionStatus(b_res.status) if b_res.status in ("PASS", "FAIL", "BLOCKED") else CriterionStatus.BLOCKED
        reason = b_res.failure_reason
        category = b_res.infrastructure_error_category
        if status == CriterionStatus.PASS and (b_res.observed_source_digest != manifest.digest
                                               or "screenshot.png" not in ev_files or b_res.assertions_executed < 1):
            status = CriterionStatus.BLOCKED
            reason = "Browser PASS lacked a matching observed snapshot digest, screenshot, or executed assertion."
            category = InfraErrorCategory.INCOMPLETE_RUNNER_RESULT

        shot = (b_res.assertion_manifest or {}).get("screenshot") or {}
        browser_evidence = {
            "failure_kind": b_res.failure_kind,
            "application_origin": (b_res.app_profile or {}).get("application_origin"),
            "browser_image": b_res.browser_image,
            "app_image": b_res.app_image,
            "browser_runtime": b_res.browser_runtime,
            "network": b_res.network,
            "summary": (b_res.assertion_manifest or {}).get("summary"),
            "screenshot": {
                "criterion_id": crit.criterion_id,
                "assertion_id": shot.get("assertion_id"),
                "source_snapshot_digest": manifest.digest,
                "browser_image_identity": (b_res.browser_image or {}).get("image_id"),
                "browser_version": (b_res.browser_runtime or {}).get("browser_version"),
                "viewport": plan.viewport.to_dict(),
                "device_scale_factor": plan.viewport.device_scale_factor,
                "application_origin_reference": (b_res.app_profile or {}).get("application_origin"),
                "captured_at": shot.get("captured_at"),
                "screenshot_relative_path": ev_files.get("screenshot.png"),
                "screenshot_sha256": ev_hashes.get("screenshot.png"),
                "omitted_reason": shot.get("omitted_reason"),
            },
            "cleanup": b_res.cleanup,
        }
        return CriterionResult(
            criterion_id=crit.criterion_id,
            title=crit.title,
            description=crit.description,
            verification_type=crit.verification_type,
            status=status,
            target=crit.target or plan.path,
            exit_code=b_res.exit_code,
            duration_ms=(time.perf_counter() - t0) * 1000.0,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc).isoformat(),
            evidence_files=ev_files,
            evidence_hashes=ev_hashes,
            failure_reason=reason,
            execution_backend="oci_browser_sandbox",
            container_image=(b_res.browser_image or {}).get("tag"),
            container_image_digest=(b_res.browser_image or {}).get("image_id"),
            infrastructure_error_category=category if status == CriterionStatus.BLOCKED else None,
            result_authoritative=status != CriterionStatus.BLOCKED,
            materialized_source_snapshot_digest=manifest.digest,
            observed_source_snapshot_digest=b_res.observed_source_digest,
            container_id=(b_res.browser_container or {}).get("container_id"),
            effective_container_config={"app": b_res.app_container, "browser": b_res.browser_container}
            if (b_res.app_container or b_res.browser_container) else None,
            browser_plan_sha256=plan.plan_sha256(),
            app_profile_id=profile_id,
            assertion_count=b_res.assertions_executed if b_res.assertion_manifest else 0,
            assertion_failure_count=b_res.assertions_failed if b_res.assertion_manifest else 0,
            browser_evidence=browser_evidence,
        )

    def _format_markdown_report(self, report: VerificationReport) -> str:
        """Format human-readable markdown verification report."""
        lines = [
            f"# BrainFrog Verification Report",
            f"",
            f"**Run ID:** `{report.run_id}`  ",
            f"**Verifier Version:** `{report.verifier_version}`  ",
            f"**Created At:** {report.created_at}  ",
            f"**Repository:** `{report.repository}`  ",
            f"**Git Revision (HEAD):** `{report.git_head_sha or 'unknown'}`  ",
            f"**Live Workspace Digest (pre):** `{report.source_snapshot_digest}`  ",
            f"**Materialized Source Snapshot Digest (executed):** `{report.materialized_source_snapshot_digest or 'none (no check executed)'}`  ",
            f"**Live Workspace Digest (post):** `{report.live_workspace_post_snapshot_digest}`  ",
            f"**Workspace Status:** {'`DIRTY`' if report.is_dirty else '`CLEAN`'}  ",
            f"**Execution Trust Mode:** `{report.execution_trust_mode}` ({report.execution_backend})  ",
        ]

        if report.authorization_reference:
            lines.append(f"**Authorization Reference:** `{report.authorization_reference}`  ")
        if report.container_image_resolution:
            img = report.container_image_resolution
            lines.append(f"**Container Image:** `{img.get('tag')}` -> `{img.get('image_id', img.get('error'))}` ({img.get('platform', '?')})  ")
        if report.docker_engine_version:
            lines.append(f"**Docker Engine:** `{report.docker_engine_version}`  ")
        if report.browser_backend:
            bb = report.browser_backend
            lines.append(f"**Browser Backend:** `{bb.get('backend')}` (image `{bb.get('browser_image')}`)  ")

        if report.workspace_drift_detected:
            lines.append(f"**ALERT:** ⚠️ `WORKSPACE_DRIFT_DETECTED` (Source mutated during test execution)  ")

        lines.extend([
            f"**Specification:** `{report.spec_path}` (SHA-256: `{report.spec_sha256[:16]}...`)  ",
            f"**Overall Verdict:** **`{report.overall_verdict.value}`** (Exit Code: {report.exit_code})  ",
            f"",
            f"## Summary",
            f"- **Total Criteria:** {report.summary.total}",
            f"- **Passed:** {report.summary.passed}",
            f"- **Failed:** {report.summary.failed}",
            f"- **Blocked:** {report.summary.blocked}",
            f"- **Not Run:** {report.summary.not_run}",
            f"",
            f"---",
            f"",
            f"## Criteria Verdicts",
            f"",
            f"| Criterion ID | Status | Type | Description | Duration | Evidence |",
            f"| :--- | :---: | :---: | :--- | :---: | :--- |",
        ])

        for c in report.criteria:
            icon = "✅" if c.status == CriterionStatus.PASS else ("❌" if c.status == CriterionStatus.FAIL else "⚠️")
            ev_links = ", ".join(f"[{f}](evidence/{c.criterion_id}/{f})" for f in c.evidence_files) if c.evidence_files else "none"
            lines.append(
                f"| `{c.criterion_id}` | {icon} **{c.status.value}** | `{c.verification_type}` | {c.title} | {c.duration_ms:.0f}ms | {ev_links} |"
            )
            if c.failure_reason:
                lines.append(f"  > **Reason:** {c.failure_reason}")
            if c.browser_evidence:
                be = c.browser_evidence
                rt = be.get("browser_runtime") or {}
                img = be.get("browser_image") or {}
                shot = be.get("screenshot") or {}
                lines.append(
                    f"  > browser={rt.get('browser_name')} {rt.get('browser_version')} playwright={rt.get('playwright_version')} "
                    f"image={(img.get('image_id') or '')[:19]} app_profile={c.app_profile_id} "
                    f"assertions={c.assertion_count} failed={c.assertion_failure_count} "
                    f"plan_sha256={(c.browser_plan_sha256 or '')[:16]} screenshot_sha256={(shot.get('screenshot_sha256') or 'none')[:16]}"
                )
            elif c.test_count is not None or c.infrastructure_error_category or c.container_id:
                lines.append(
                    f"  > tests={c.test_count} failures={c.test_failure_count} skipped={c.test_skipped_count} "
                    f"authoritative={c.result_authoritative} infra={c.infrastructure_error_category} "
                    f"container={(c.container_id or '')[:12] or 'n/a'}"
                )

        if report.goal:
            lines.extend(["", "## Goal", report.goal])

        if report.non_goals:
            lines.extend(["", "## Non-goals"])
            for ng in report.non_goals:
                lines.append(f"- {ng}")

        if report.constraints:
            lines.extend(["", "## Constraints"])
            for co in report.constraints:
                lines.append(f"- {co}")

        lines.append("")
        return "\n".join(lines)


# =============================================================================
# CONSOLE RENDERING
# =============================================================================

def render_verification_console(report: VerificationReport, console: Any = None) -> None:
    """Render clean, readable terminal output for verification report."""
    from rich.console import Console
    from rich.table import Table
    from rich import box

    c = console or Console(legacy_windows=False)

    rev_str = report.git_head_sha[:7] if report.git_head_sha else "unknown"
    ws_str = "[bold red]dirty[/bold red]" if report.is_dirty else "[bold green]clean[/bold green]"
    if report.is_dirty and report.dirty_files:
        ws_str += f" ([dim]{len(report.dirty_files)} modified/untracked files[/dim])"

    c.print()
    c.print("[bold #4EC9B0]BrainFrog Verification[/bold #4EC9B0]")
    c.print(f"Revision:  [bold]{rev_str}[/bold]")
    c.print(f"Workspace: {ws_str}")
    c.print(f"Workspace digest (pre):  [dim]{report.source_snapshot_digest[:16]}...[/dim]")
    if report.materialized_source_snapshot_digest:
        c.print(f"Executed snapshot:       [dim]{report.materialized_source_snapshot_digest[:16]}...[/dim] "
                f"({report.materialized_file_count} files)")
    if report.container_image_resolution and report.container_image_resolution.get("image_id"):
        img = report.container_image_resolution
        c.print(f"Image:     {img['tag']} -> [dim]{img['image_id'][:19]}...[/dim]")
    c.print(f"Trust:     [bold]{report.execution_trust_mode}[/bold] ({report.execution_backend})")
    browser_crits = [cr for cr in report.criteria if cr.browser_evidence]
    if report.browser_backend:
        if report.browser_backend.get("backend") == "oci_browser_sandbox":
            rt = next(((cr.browser_evidence or {}).get("browser_runtime") or {} for cr in browser_crits
                       if ((cr.browser_evidence or {}).get("browser_runtime") or {}).get("browser_version")), {})
            c.print("Backend:   [bold]OCI Browser Verification[/bold]")
            c.print(f"Browser:   Chromium {rt.get('browser_version', '?')} (Playwright {rt.get('playwright_version', '?')}, "
                    f"{report.browser_backend.get('browser_image')})")
            c.print("Application: isolated container on a private internal network")
        else:
            c.print(f"Browser:   [yellow]unavailable[/yellow] ({report.browser_backend.get('unavailable_reason')})")
    c.print(f"Spec:      {report.spec_path} ([dim]sha256:{report.spec_sha256[:8]}[/dim])")

    if report.execution_trust_mode == "TRUSTED_OPERATOR":
        c.print("[yellow]Notice: Operating in TRUSTED_OPERATOR mode. Repository scripts execute with local privileges.[/yellow]")

    if report.workspace_drift_detected:
        c.print("[bold red]ALERT: Workspace drift detected! Repository files changed during verification execution.[/bold red]")

    c.print()

    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    table.add_column("ID", style="bold cyan", width=10)
    table.add_column("Status", width=10)
    table.add_column("Details")

    for crit in report.criteria:
        if crit.status == CriterionStatus.PASS:
            st_styled = "[bold green]PASS[/bold green]"
        elif crit.status == CriterionStatus.FAIL:
            st_styled = "[bold red]FAIL[/bold red]"
        elif crit.status == CriterionStatus.BLOCKED:
            st_styled = "[bold yellow]BLOCKED[/bold yellow]"
        else:
            st_styled = "[bold dim]NOT_RUN[/bold dim]"

        detail = crit.title
        if crit.assertion_count is not None:
            detail += f" ({crit.assertion_count} assertions executed)"
        elif crit.test_count is not None:
            if crit.test_count > 0:
                detail += f" ({crit.test_count} executed)"
            elif crit.status == CriterionStatus.BLOCKED:
                detail += " (0 executed)"
        if crit.failure_reason:
            detail += f"\n[dim yellow]{crit.failure_reason}[/dim yellow]"

        table.add_row(crit.criterion_id, st_styled, detail)

    c.print(table)
    c.print()

    if report.overall_verdict == OverallVerdict.PASS:
        v_styled = "[bold green]PASS[/bold green]"
    elif report.overall_verdict == OverallVerdict.FAIL:
        v_styled = "[bold red]FAIL[/bold red]"
    else:
        v_styled = "[bold yellow]BLOCKED[/bold yellow]"

    c.print(f"Overall: {v_styled}")
    c.print(f"Passed:  {report.summary.passed}")
    c.print(f"Failed:  {report.summary.failed}")
    c.print(f"Blocked: {report.summary.blocked}")
    c.print()
    c.print("Report:")
    c.print(f"[underline].brainfrog/reports/{report.run_id}/report.md[/underline]")
    c.print(f"Evidence: .brainfrog/reports/{report.run_id}/evidence/")
    c.print()
