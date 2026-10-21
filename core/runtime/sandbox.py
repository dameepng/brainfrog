"""Verification Execution Engine and Sandbox Abstraction for BrainFrog Phase 1C.

Provides:
1. VerificationExecutionBackend interface.
2. DockerSandboxBackend: Real OCI container execution with strict, inspected isolation.
3. LocalTrustedExecutionBackend: Hardened host execution for TRUSTED_OPERATOR mode.
4. TestResultAdapter: Verdicts derived from structured, nonce-bound harness records
   (core/runtime/verification_harness.py) rather than free-form runner text.
5. Materialized source snapshot builder whose digest covers the exact bytes supplied
   to the execution backend.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from core.runtime.command_policy import CommandPolicy
from core.runtime.process import run_hardened_subprocess
from core.runtime.secret_scrubbing import safe_scrub


HARNESS_SOURCE_PATH = Path(__file__).resolve().parent / "verification_harness.py"
HARNESS_RECORD_PREFIX = "@@BF_HARNESS@@ "
MAX_CAPTURED_OUTPUT_BYTES = 1024 * 1024


# =============================================================================
# DATA STRUCTURES & POLICIES
# =============================================================================

class InfraErrorCategory:
    """Machine-readable infrastructure failure categories (verdict BLOCKED, never FAIL)."""
    DOCKER_UNAVAILABLE = "docker_unavailable"
    IMAGE_MISSING = "image_missing"
    IMAGE_NOT_APPROVED = "image_not_approved"
    CONTAINER_CREATE_FAILED = "container_create_failed"
    CONTAINER_START_FAILED = "container_start_failed"
    MOUNT_UNAVAILABLE = "mount_unavailable"
    SECURITY_POLICY_NOT_ENFORCED = "security_policy_not_enforced"
    POLICY_WEAKENING_REFUSED = "policy_weakening_refused"
    CLEANUP_FAILED = "container_cleanup_failed"
    TIMEOUT = "timeout"
    RESOURCE_LIMIT = "resource_limit_termination"
    TOOLCHAIN_MISSING = "toolchain_missing"
    RUNNER_NOT_STARTED = "runner_not_started"
    INCOMPLETE_RUNNER_RESULT = "incomplete_runner_result"
    FORGED_RUNNER_RECORD = "forged_runner_record"
    OUTPUT_TRUNCATED = "output_truncated"
    SNAPSHOT_ERROR = "snapshot_error"
    SNAPSHOT_IDENTITY_MISMATCH = "snapshot_identity_mismatch"
    SNAPSHOT_MUTATED = "snapshot_mutated_during_execution"
    UNSUPPORTED_RUNNER = "unsupported_runner"
    RUNNER_USAGE_ERROR = "runner_usage_error"
    INTERNAL_ERROR = "internal_error"


class SandboxInfraError(RuntimeError):
    """Raised when sandbox infrastructure cannot run a check reliably."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


class SnapshotError(RuntimeError):
    """Raised when a source snapshot cannot be created faithfully."""


@dataclass
class SandboxResourceLimits:
    """Enforceable container execution limits."""
    cpus: float = 2.0
    memory_mb: int = 512
    pids_limit: int = 64
    timeout_seconds: float = 120.0
    workspace_tmpfs_mb: int = 256
    tmp_tmpfs_mb: int = 64


SANDBOX_UID_GID = "65532:65532"


@dataclass
class SandboxPolicy:
    """Isolation constraints applied to the verification sandbox.

    The fields exist for reporting. The Docker backend refuses to run with any
    weakened value; an untrusted spec has no path to construct this object.
    """
    allow_network: bool = False
    drop_capabilities: bool = True
    no_new_privileges: bool = True
    read_only_source_mount: bool = True
    read_only_root_filesystem: bool = True
    ephemeral_workspace: bool = True
    non_root_user: Optional[str] = SANDBOX_UID_GID

    def weakened_controls(self) -> List[str]:
        weakened = []
        if self.allow_network:
            weakened.append("allow_network")
        if not self.drop_capabilities:
            weakened.append("drop_capabilities")
        if not self.no_new_privileges:
            weakened.append("no_new_privileges")
        if not self.read_only_source_mount:
            weakened.append("read_only_source_mount")
        if not self.read_only_root_filesystem:
            weakened.append("read_only_root_filesystem")
        if not self.ephemeral_workspace:
            weakened.append("ephemeral_workspace")
        if self.non_root_user != SANDBOX_UID_GID:
            weakened.append("non_root_user")
        return weakened

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allow_network": self.allow_network,
            "drop_capabilities": self.drop_capabilities,
            "no_new_privileges": self.no_new_privileges,
            "read_only_source_mount": self.read_only_source_mount,
            "read_only_root_filesystem": self.read_only_root_filesystem,
            "ephemeral_workspace": self.ephemeral_workspace,
            "non_root_user": self.non_root_user,
        }


# =============================================================================
# TEST RESULT ADAPTERS (STRUCTURED RECORDS, ZERO-TEST & FORGERY RESISTANCE)
# =============================================================================

@dataclass
class ParsedCheckOutcome:
    """Interpreted outcome from test or build execution."""
    verdict: str  # "PASS", "FAIL", "BLOCKED", "NOT_RUN"
    test_count: Optional[int]
    failure_count: Optional[int]
    reason: Optional[str]
    infrastructure_error_category: Optional[str] = None
    skipped_count: Optional[int] = None
    authoritative: bool = False
    runner_record: Optional[Dict[str, Any]] = None
    observed_source_digest: Optional[str] = None

TestOutcome = ParsedCheckOutcome


def classify_runner(cmd_tokens: List[str]) -> Optional[str]:
    """Return the harness mode for a supported runner, or None when unsupported."""
    if len(cmd_tokens) < 3:
        return None
    exe = Path(cmd_tokens[0]).name.lower()
    if exe.endswith(".exe"):
        exe = exe[:-4]
    if exe not in ("python", "python3") or cmd_tokens[1] != "-m":
        return None
    module = cmd_tokens[2]
    if module in ("unittest", "compileall", "py_compile"):
        return module
    return None


def extract_harness_records(stdout: str, nonce: str) -> Tuple[List[Dict[str, Any]], int]:
    """Return (records with matching nonce, count of forged/invalid record lines)."""
    records: List[Dict[str, Any]] = []
    forged = 0
    for line in (stdout or "").splitlines():
        idx = line.find(HARNESS_RECORD_PREFIX.strip())
        if idx < 0:
            continue
        if not line.startswith(HARNESS_RECORD_PREFIX):
            forged += 1
            continue
        try:
            rec = json.loads(line[len(HARNESS_RECORD_PREFIX):])
        except ValueError:
            forged += 1
            continue
        if not isinstance(rec, dict) or not nonce or not secrets.compare_digest(str(rec.get("nonce", "")), nonce):
            forged += 1
            continue
        records.append(rec)
    return records, forged


class TestResultAdapter:
    """Derives verdicts from execution evidence.

    Authoritative path: a nonce-bound record from the BrainFrog harness. Without such a
    record, human-readable runner text is never sufficient for PASS.
    """

    @classmethod
    def parse_outcome(
        cls,
        cmd_tokens: List[str],
        exit_code: int,
        stdout: str,
        stderr: str,
        *,
        nonce: Optional[str] = None,
        expected_source_digest: Optional[str] = None,
        output_truncated: bool = False,
    ) -> ParsedCheckOutcome:
        mode = classify_runner(cmd_tokens)
        if mode is None:
            return ParsedCheckOutcome(
                verdict="BLOCKED",
                test_count=None,
                failure_count=None,
                reason=(
                    "Unsupported verification runner: no structured result adapter exists, so a zero "
                    "exit status cannot establish that the criterion was verified."
                ),
                infrastructure_error_category=InfraErrorCategory.UNSUPPORTED_RUNNER,
            )
        if nonce is None:
            return cls._parse_unstructured(mode, exit_code, stdout, stderr)
        return cls._parse_structured(mode, exit_code, stdout, nonce, expected_source_digest, output_truncated)

    @classmethod
    def _parse_unstructured(cls, mode: str, exit_code: int, stdout: str, stderr: str) -> ParsedCheckOutcome:
        """Legacy text-only evidence: may support FAIL, never PASS."""
        combined = (stdout or "") + "\n" + (stderr or "")
        ran = re.search(r"Ran\s+(\d+)\s+tests?", combined) if mode == "unittest" else None
        count = int(ran.group(1)) if ran else (0 if mode == "unittest" else None)
        if mode == "unittest" and ran and count == 0:
            return ParsedCheckOutcome("BLOCKED", 0, 0, "Zero tests discovered or executed (preventing false pass).")
        if exit_code != 0:
            fails = sum(int(m) for m in re.findall(r"(?:failures|errors)=(\d+)", combined)) or 1
            return ParsedCheckOutcome(
                "FAIL", count, fails,
                f"Runner exited with status {exit_code} (unstructured evidence; counts are informational)",
            )
        return ParsedCheckOutcome(
            "BLOCKED", count, None,
            "No authoritative runner record: free-form runner output cannot establish PASS.",
            infrastructure_error_category=InfraErrorCategory.RUNNER_NOT_STARTED,
        )

    @classmethod
    def _parse_structured(
        cls,
        mode: str,
        exit_code: int,
        stdout: str,
        nonce: str,
        expected_source_digest: Optional[str],
        output_truncated: bool,
    ) -> ParsedCheckOutcome:
        records, forged = extract_harness_records(stdout, nonce)
        started = [r for r in records if r.get("event") == "started"]
        complete = [r for r in records if r.get("event") == "complete"]

        def blocked(reason: str, category: str, **kw: Any) -> ParsedCheckOutcome:
            return ParsedCheckOutcome("BLOCKED", kw.pop("test_count", None), None, reason,
                                      infrastructure_error_category=category, **kw)

        if forged or len(started) > 1 or len(complete) > 1:
            return blocked(
                f"Forged or duplicated runner records detected ({forged} invalid, {len(started)} start, {len(complete)} complete).",
                InfraErrorCategory.FORGED_RUNNER_RECORD,
            )
        if not started:
            if output_truncated:
                return blocked("Output exceeded capture bound before runner records.", InfraErrorCategory.OUTPUT_TRUNCATED)
            return blocked(f"Verification harness never started (exit status {exit_code}).", InfraErrorCategory.RUNNER_NOT_STARTED)

        observed = started[0].get("source_digest")
        if expected_source_digest and observed != expected_source_digest:
            return blocked(
                f"Runner observed source digest {str(observed)[:16]}... but materialized snapshot is {expected_source_digest[:16]}...",
                InfraErrorCategory.SNAPSHOT_IDENTITY_MISMATCH,
                observed_source_digest=observed,
            )

        if not complete:
            if output_truncated:
                return blocked("Output exceeded capture bound; completion record lost.", InfraErrorCategory.OUTPUT_TRUNCATED,
                               observed_source_digest=observed)
            if exit_code != 0:
                return ParsedCheckOutcome(
                    "FAIL", None, None,
                    f"Runner aborted after repository code began executing (exit status {exit_code}, no completion record).",
                    observed_source_digest=observed,
                )
            return blocked(
                "Runner exited 0 without a completion record (possible early process exit); result is untrustworthy.",
                InfraErrorCategory.INCOMPLETE_RUNNER_RESULT,
                observed_source_digest=observed,
            )

        rec = complete[0]
        if rec.get("status") != "ran":
            return blocked(f"Runner rejected its arguments: {rec.get('detail')}", InfraErrorCategory.RUNNER_USAGE_ERROR,
                           observed_source_digest=observed)

        if mode == "unittest":
            return cls._unittest_verdict(rec, exit_code, output_truncated, observed)
        return cls._compile_verdict(rec, exit_code, output_truncated, observed)

    @staticmethod
    def _unittest_verdict(rec: Dict[str, Any], exit_code: int, truncated: bool, observed: Optional[str]) -> ParsedCheckOutcome:
        run = int(rec.get("tests_run") or 0)
        failures = int(rec.get("failures") or 0) + int(rec.get("errors") or 0) + int(rec.get("unexpected_successes") or 0)
        skipped = int(rec.get("skipped") or 0)
        common = dict(skipped_count=skipped, authoritative=True, runner_record=rec, observed_source_digest=observed)
        if failures > 0:
            return ParsedCheckOutcome("FAIL", run, failures, f"{failures} failure(s)/error(s) in {run} tests executed", **common)
        if run == 0:
            return ParsedCheckOutcome("BLOCKED", 0, 0, "Zero tests discovered or executed (preventing false pass).", **common)
        if skipped >= run:
            return ParsedCheckOutcome("BLOCKED", run, 0, f"All {run} tests were skipped; no assertion was executed.", **common)
        if rec.get("interrupted") or not rec.get("was_successful"):
            return ParsedCheckOutcome("BLOCKED", run, 0, "Runner reported an interrupted or unsuccessful run without failures.",
                                      infrastructure_error_category=InfraErrorCategory.INCOMPLETE_RUNNER_RESULT, **common)
        if exit_code != 0:
            return ParsedCheckOutcome("BLOCKED", run, 0, f"Runner record reports success but process exited {exit_code}.",
                                      infrastructure_error_category=InfraErrorCategory.INCOMPLETE_RUNNER_RESULT, **common)
        if truncated:
            return ParsedCheckOutcome("BLOCKED", run, 0, "Captured output was truncated; PASS not supported.",
                                      infrastructure_error_category=InfraErrorCategory.OUTPUT_TRUNCATED, **common)
        return ParsedCheckOutcome("PASS", run, 0, None, **common)

    @staticmethod
    def _compile_verdict(rec: Dict[str, Any], exit_code: int, truncated: bool, observed: Optional[str]) -> ParsedCheckOutcome:
        compiled = int(rec.get("files_compiled") or 0)
        errors = int(rec.get("compile_errors") or 0)
        missing = rec.get("missing_targets") or []
        common = dict(authoritative=True, runner_record=rec, observed_source_digest=observed)
        if missing:
            return ParsedCheckOutcome("FAIL", None, len(missing), f"Target compilation path(s) do not exist: {missing}", **common)
        if errors:
            return ParsedCheckOutcome("FAIL", None, errors, f"Syntax or compilation error in {errors} of {compiled} file(s)", **common)
        if compiled == 0:
            return ParsedCheckOutcome("BLOCKED", None, 0, "Zero source files compiled (preventing false pass).", **common)
        if exit_code != 0:
            return ParsedCheckOutcome("BLOCKED", None, 0, f"Runner record reports success but process exited {exit_code}.",
                                      infrastructure_error_category=InfraErrorCategory.INCOMPLETE_RUNNER_RESULT, **common)
        if truncated:
            return ParsedCheckOutcome("BLOCKED", None, 0, "Captured output was truncated; PASS not supported.",
                                      infrastructure_error_category=InfraErrorCategory.OUTPUT_TRUNCATED, **common)
        return ParsedCheckOutcome("PASS", None, 0, None, **common)


# =============================================================================
# MATERIALIZED SOURCE SNAPSHOT BUILDER
# =============================================================================

DEFAULT_SNAPSHOT_EXCLUDES: Set[str] = {
    ".git",
    ".brainfrog",
    "scratch",
    ".tmp",
    "__pycache__",
    ".pytest_cache",
    ".coverage",
    ".env",
    ".env.local",
    ".netrc",
    ".npmrc",
    ".pypirc",
}
SENSITIVE_SUFFIXES = (".pyc", ".pyo", ".key", ".pem", ".p12", ".pfx")
SENSITIVE_NAME_PREFIXES = ("id_rsa", "id_ed25519", "id_ecdsa", ".env.")


@dataclass
class SnapshotManifest:
    """Exact identity of the bytes materialized for execution."""
    snapshot_dir: Path
    digest: str
    files: Dict[str, str]
    skipped_symlinks: List[str]
    excluded: List[str]
    source_listing: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "digest_algorithm": "sha256(sorted '<relpath>:<len>:' || sha256(bytes))",
            "materialized_source_snapshot_digest": self.digest,
            "file_count": len(self.files),
            "source_listing": self.source_listing,
            "files": self.files,
            "skipped_symlinks": self.skipped_symlinks,
            "excluded": self.excluded,
        }


def _snapshot_digest(entries: List[Tuple[str, bytes]]) -> str:
    hasher = hashlib.sha256()
    for rel, data in sorted(entries, key=lambda e: e[0]):
        hasher.update(f"{rel}:{len(data)}:".encode("utf-8"))
        hasher.update(hashlib.sha256(data).digest())
    return hasher.hexdigest()


def compute_directory_digest(directory: Path) -> str:
    """Digest every regular, non-symlink file under directory (same algorithm as the harness)."""
    entries: List[Tuple[str, bytes]] = []
    root = Path(directory)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in filenames:
            p = Path(dirpath) / name
            if p.is_symlink() or not p.is_file():
                continue
            entries.append((p.relative_to(root).as_posix(), p.read_bytes()))
    return _snapshot_digest(entries)


def _is_excluded(rel_str: str, excludes: Set[str]) -> bool:
    parts = rel_str.split("/")
    if any(p in excludes for p in parts):
        return True
    if any(rel_str == ex or rel_str.startswith(ex + "/") for ex in excludes):
        return True
    name = parts[-1]
    return name.endswith(SENSITIVE_SUFFIXES) or name.startswith(SENSITIVE_NAME_PREFIXES)


def _list_source_files(repo_dir: Path) -> Tuple[List[str], str]:
    """List tracked + untracked-not-ignored files; returns (paths, listing method)."""
    probe = run_hardened_subprocess(["git", "rev-parse", "--show-toplevel"], cwd=repo_dir, timeout=10.0)
    if probe.returncode != 0:
        # Not a Git work tree (e.g. test fixtures): enumerate the directory itself.
        rels = []
        for dirpath, dirnames, filenames in os.walk(repo_dir, followlinks=False):
            for name in filenames + [d for d in dirnames if (Path(dirpath) / d).is_symlink()]:
                rels.append((Path(dirpath) / name).relative_to(repo_dir).as_posix())
        return rels, "filesystem_walk"

    listing = run_hardened_subprocess(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=repo_dir,
        timeout=30.0,
    )
    if listing.returncode != 0 or getattr(listing, "is_truncated", False) or getattr(listing, "is_timeout", False):
        raise SnapshotError(f"git ls-files failed (exit {listing.returncode}): {(listing.stderr or '').strip()[:200]}")
    rels = sorted({p.replace("\\", "/") for p in (listing.stdout or "").split("\0") if p})
    return rels, "git_ls_files_cached_others_exclude_standard"


def materialize_source_snapshot_with_manifest(
    repo_dir: Path,
    target_snapshot_dir: Path,
    exclude_dirs: Optional[List[str]] = None,
) -> SnapshotManifest:
    """Copy tracked (incl. staged/unstaged edits) and untracked-not-ignored files.

    Guarantees:
    - Git-ignored files are never copied (sensitive local files are usually ignored).
    - Credential-like files, .git, caches and BrainFrog reports are excluded.
    - Symlinks are never followed or copied (recorded in skipped_symlinks).
    - Read errors raise SnapshotError (never silently dropped).
    - After copying, live files are re-read; any difference raises SnapshotError
      (concurrent modification during snapshot creation).
    The returned digest is computed from the exact bytes written to target_snapshot_dir.
    """
    repo_dir = Path(repo_dir).resolve()
    target_snapshot_dir = Path(target_snapshot_dir).resolve()
    if target_snapshot_dir.exists() and any(target_snapshot_dir.iterdir()):
        raise SnapshotError(f"Snapshot target '{target_snapshot_dir}' is not empty.")
    target_snapshot_dir.mkdir(parents=True, exist_ok=True)

    excludes = set(DEFAULT_SNAPSHOT_EXCLUDES)
    for ed in exclude_dirs or []:
        excludes.add(ed.replace("\\", "/").strip("/"))
    try:
        rel_target = target_snapshot_dir.relative_to(repo_dir).as_posix()
        excludes.add(rel_target)
    except ValueError:
        pass

    rels, listing_method = _list_source_files(repo_dir)
    entries: List[Tuple[str, bytes]] = []
    files: Dict[str, str] = {}
    skipped_symlinks: List[str] = []
    excluded: List[str] = []

    for rel_str in rels:
        if _is_excluded(rel_str, excludes):
            excluded.append(rel_str)
            continue
        src_file = repo_dir / rel_str
        # Reject any path whose components include a symlink/junction (no traversal escapes).
        cur = repo_dir
        has_link = False
        for part in rel_str.split("/"):
            cur = cur / part
            if cur.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(cur)):
                has_link = True
                break
        if has_link:
            skipped_symlinks.append(rel_str)
            continue
        if not src_file.exists():
            continue  # tracked file deleted in the working tree: deletion is represented by absence
        if not src_file.is_file():
            continue
        try:
            data = src_file.read_bytes()
        except OSError as exc:
            raise SnapshotError(f"Cannot read source file '{rel_str}': {exc}") from exc
        dst_file = target_snapshot_dir / rel_str
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        dst_file.write_bytes(data)
        entries.append((rel_str, data))
        files[rel_str] = hashlib.sha256(data).hexdigest()

    # Concurrent drift detection: live bytes must still match what was copied.
    for rel_str, digest in files.items():
        try:
            live = hashlib.sha256((repo_dir / rel_str).read_bytes()).hexdigest()
        except OSError as exc:
            raise SnapshotError(f"Source file '{rel_str}' vanished during snapshot: {exc}") from exc
        if live != digest:
            raise SnapshotError(f"Source file '{rel_str}' changed while the snapshot was being created.")

    digest = _snapshot_digest(entries)
    if compute_directory_digest(target_snapshot_dir) != digest:
        raise SnapshotError("Materialized snapshot does not match the bytes read from the workspace.")

    return SnapshotManifest(
        snapshot_dir=target_snapshot_dir,
        digest=digest,
        files=files,
        skipped_symlinks=skipped_symlinks,
        excluded=excluded,
        source_listing=listing_method,
    )


def materialize_source_snapshot(
    repo_dir: Path,
    target_snapshot_dir: Path,
    exclude_dirs: Optional[List[str]] = None,
) -> Tuple[Path, str]:
    """Compatibility wrapper returning (snapshot_dir, materialized_source_snapshot_digest)."""
    manifest = materialize_source_snapshot_with_manifest(repo_dir, target_snapshot_dir, exclude_dirs)
    return manifest.snapshot_dir, manifest.digest


def stage_harness() -> Tuple[Path, str]:
    """Copy the harness into a fresh BrainFrog-owned directory; return (dir, sha256)."""
    data = HARNESS_SOURCE_PATH.read_bytes()
    harness_dir = Path(tempfile.mkdtemp(prefix="bf_harness_"))
    (harness_dir / "verification_harness.py").write_bytes(data)
    return harness_dir, hashlib.sha256(data).hexdigest()


# =============================================================================
# BACKEND ABSTRACTION
# =============================================================================

@dataclass
class ExecutionResult:
    """Raw execution result produced by an execution backend."""
    backend_type: str
    backend_version: str
    container_image: Optional[str]
    container_image_digest: Optional[str]
    container_id: Optional[str]
    source_snapshot_digest: str
    executed_argv: List[str]
    working_directory: str
    started_at: str
    finished_at: str
    duration_ms: float
    exit_code: Optional[int]
    stdout: str
    stderr: str
    test_count: Optional[int] = None
    test_failure_count: Optional[int] = None
    resource_limit_status: str = "ok"
    timeout_status: bool = False
    execution_error: Optional[str] = None
    sandbox_policy_summary: Dict[str, Any] = field(default_factory=dict)
    infrastructure_error_category: Optional[str] = None
    output_truncated: bool = False
    harness_sha256: Optional[str] = None
    container_name: Optional[str] = None
    effective_container_config: Optional[Dict[str, Any]] = None
    outcome: Optional[ParsedCheckOutcome] = None

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


class VerificationExecutionBackend:
    """Abstract execution backend for acceptance criteria."""

    backend_type = "abstract"

    def is_available(self) -> Tuple[bool, Optional[str]]:
        raise NotImplementedError

    def execute(
        self,
        source_snapshot_dir: Path,
        source_snapshot_digest: str,
        approved_argv: List[str],
        policy: SandboxPolicy,
        limits: SandboxResourceLimits,
        cwd_relative: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        run_label: Optional[str] = None,
    ) -> ExecutionResult:
        raise NotImplementedError

    def _blocked_result(
        self,
        category: str,
        message: str,
        source_snapshot_digest: str,
        approved_argv: List[str],
        started_at: str,
        t0: float,
        **extra: Any,
    ) -> ExecutionResult:
        outcome = ParsedCheckOutcome("BLOCKED", None, None, message, infrastructure_error_category=category)
        fields = dict(
            backend_type=self.backend_type,
            backend_version=extra.pop("backend_version", "unknown"),
            container_image=extra.pop("container_image", None),
            container_image_digest=extra.pop("container_image_digest", None),
            container_id=extra.pop("container_id", None),
            source_snapshot_digest=source_snapshot_digest,
            executed_argv=extra.pop("executed_argv", list(approved_argv)),
            working_directory=extra.pop("working_directory", ""),
            started_at=started_at,
            finished_at=datetime.now(timezone.utc).isoformat(),
            duration_ms=(time.perf_counter() - t0) * 1000.0,
            exit_code=extra.pop("exit_code", None),
            stdout=extra.pop("stdout", ""),
            stderr=extra.pop("stderr", message),
            execution_error=message,
            infrastructure_error_category=category,
            outcome=outcome,
        )
        fields.update(extra)
        return ExecutionResult(**fields)


# =============================================================================
# REAL OCI DOCKER SANDBOX BACKEND
# =============================================================================

# Operator-approved images. Specs cannot select images; only these tags may be resolved.
APPROVED_DOCKER_VERIFICATION_IMAGES = {
    "python": "python:3.12-slim",
    "python:3.12": "python:3.12-slim",
    "python:3.11": "python:3.11-slim",
}

DEFAULT_DOCKER_IMAGE = "python:3.12-slim"
BRAINFROG_OWNER_LABEL = "io.brainfrog.owner"
BRAINFROG_OWNER_VALUE = "brainfrog-verifier"
BRAINFROG_RUN_LABEL = "io.brainfrog.run"

# Environment keys the docker CLI itself needs to reach the engine. None of these is
# forwarded into containers: container env is only what build_container_create_args sets.
_DOCKER_CLI_ENV_KEYS = {
    "PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
    "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES",
    "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY",
    "XDG_RUNTIME_DIR",
}


def _run_bounded(
    argv: List[str],
    timeout: float,
    stdin_text: Optional[str] = None,
    max_bytes: int = MAX_CAPTURED_OUTPUT_BYTES,
    on_timeout: Optional[Callable[[], None]] = None,
) -> Tuple[Optional[int], str, str, bool, bool]:
    """Run a BrainFrog-constructed command with bounded capture.

    Returns (returncode, stdout, stderr, timed_out, truncated).
    """
    env = {k: v for k, v in os.environ.items() if k.upper() in _DOCKER_CLI_ENV_KEYS}
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        shell=False,
    )
    buffers: Dict[str, bytearray] = {"out": bytearray(), "err": bytearray()}
    truncated = {"flag": False}

    def _pump(stream: Any, key: str) -> None:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            buf = buffers[key]
            room = max_bytes - len(buf)
            if room > 0:
                buf.extend(chunk[:room])
            if len(chunk) > room:
                truncated["flag"] = True

    threads = [
        threading.Thread(target=_pump, args=(proc.stdout, "out"), daemon=True),
        threading.Thread(target=_pump, args=(proc.stderr, "err"), daemon=True),
    ]
    for t in threads:
        t.start()
    if stdin_text is not None:
        try:
            proc.stdin.write(stdin_text.encode("utf-8"))
            proc.stdin.close()
        except OSError:
            pass

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        if on_timeout:
            on_timeout()
        proc.kill()
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
    for t in threads:
        t.join(timeout=5)
    return (
        None if timed_out else proc.returncode,
        buffers["out"].decode("utf-8", errors="replace"),
        buffers["err"].decode("utf-8", errors="replace"),
        timed_out,
        truncated["flag"],
    )


class DockerSandboxBackend(VerificationExecutionBackend):
    """Executes checks inside disposable, hardened OCI containers via the Docker CLI.

    Lifecycle per check: docker create -> docker inspect (verify effective security
    configuration) -> docker start -a -i (nonce on stdin) -> docker inspect (exit/OOM)
    -> docker rm -f (only containers carrying this run's ownership label).
    """

    backend_type = "oci_container_docker"

    def __init__(
        self,
        image_name: str = DEFAULT_DOCKER_IMAGE,
        docker_executable: Optional[str] = None,
    ) -> None:
        if image_name not in set(APPROVED_DOCKER_VERIFICATION_IMAGES.values()):
            raise ValueError(f"Image '{image_name}' is not an operator-approved verification image.")
        self.image_name = image_name
        self.docker_exe = docker_executable or shutil.which("docker") or "docker"

    # -- docker CLI helpers ---------------------------------------------------

    def _docker(self, args: List[str], timeout: float = 30.0, stdin_text: Optional[str] = None,
                on_timeout: Optional[Callable[[], None]] = None) -> Tuple[Optional[int], str, str, bool, bool]:
        return _run_bounded([self.docker_exe] + args, timeout=timeout, stdin_text=stdin_text, on_timeout=on_timeout)

    def is_available(self) -> Tuple[bool, Optional[str]]:
        """Probe Docker CLI presence and engine connectivity (read-only)."""
        try:
            rc, out, err, timed_out, _ = self._docker(["version", "--format", "{{.Server.Version}}"], timeout=15.0)
        except FileNotFoundError:
            return False, "Docker CLI executable not found in PATH."
        except OSError as exc:
            return False, f"Docker CLI could not be launched: {exc}"
        if timed_out:
            return False, "Docker engine did not respond within 15 seconds."
        if rc == 0 and out.strip():
            return True, None
        msg = (err or out).strip()
        if "failed to connect" in msg or "daemon is running" in msg or "pipe" in msg:
            return False, "Docker CLI is installed, but the Docker daemon/engine is not running."
        return False, f"Docker daemon unavailable: {msg[:200]}"

    def engine_version(self) -> Optional[str]:
        rc, out, _, _, _ = self._docker(["version", "--format", "{{.Server.Version}}"], timeout=15.0)
        return out.strip() if rc == 0 else None

    def resolve_image(self) -> Dict[str, Any]:
        """Resolve the approved local tag to its immutable image ID. Never pulls."""
        rc, out, err, timed_out, _ = self._docker(
            ["image", "inspect", "--format", "{{json .}}", self.image_name], timeout=30.0
        )
        if timed_out:
            raise SandboxInfraError(InfraErrorCategory.DOCKER_UNAVAILABLE, "docker image inspect timed out")
        if rc != 0:
            raise SandboxInfraError(
                InfraErrorCategory.IMAGE_MISSING,
                f"Approved image '{self.image_name}' is not available locally and pulls are disabled: {(err or out).strip()[:200]}",
            )
        info = json.loads(out)
        image_id = info.get("Id", "")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise SandboxInfraError(InfraErrorCategory.IMAGE_MISSING, f"Unexpected image ID '{image_id}'")
        if info.get("Os") != "linux":
            raise SandboxInfraError(InfraErrorCategory.IMAGE_NOT_APPROVED, f"Image OS '{info.get('Os')}' is not linux")
        return {
            "tag": self.image_name,
            "image_id": image_id,
            "repo_digests": info.get("RepoDigests") or [],
            "platform": f"{info.get('Os')}/{info.get('Architecture')}",
        }

    def get_image_digest(self) -> Optional[str]:
        try:
            return self.resolve_image()["image_id"]
        except (SandboxInfraError, OSError, ValueError):
            return None

    @staticmethod
    def _host_path(p: Path) -> str:
        resolved = str(Path(p).resolve())
        if "," in resolved or "\n" in resolved or "=" in resolved:
            raise SandboxInfraError(InfraErrorCategory.MOUNT_UNAVAILABLE, f"Unsupported character in mount path '{resolved}'")
        return resolved

    def build_container_create_args(
        self,
        container_name: str,
        run_label: str,
        source_snapshot_dir: Path,
        harness_dir: Path,
        image_id: str,
        harness_argv: List[str],
        limits: SandboxResourceLimits,
    ) -> List[str]:
        """Build the fixed, hardened 'docker create' argument vector.

        No element of this vector is derived from spec content except harness_argv,
        which is the already policy-validated runner argument list placed after the image.
        """
        src = self._host_path(source_snapshot_dir)
        har = self._host_path(harness_dir)
        return [
            "create",
            "--interactive",
            "--pull=never",
            f"--name={container_name}",
            f"--label={BRAINFROG_OWNER_LABEL}={BRAINFROG_OWNER_VALUE}",
            f"--label={BRAINFROG_RUN_LABEL}={run_label}",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--user={SANDBOX_UID_GID}",
            f"--pids-limit={int(limits.pids_limit)}",
            f"--memory={int(limits.memory_mb)}m",
            f"--memory-swap={int(limits.memory_mb)}m",
            f"--cpus={float(limits.cpus)}",
            "--ipc=private",
            "--restart=no",
            "--log-driver=none",
            f"--mount=type=bind,source={src},target=/source,readonly",
            f"--mount=type=bind,source={har},target=/bf-harness,readonly",
            f"--tmpfs=/workspace:rw,nosuid,nodev,noexec,size={int(limits.workspace_tmpfs_mb)}m,uid=65532,gid=65532,mode=0700",
            f"--tmpfs=/tmp:rw,nosuid,nodev,noexec,size={int(limits.tmp_tmpfs_mb)}m,uid=65532,gid=65532,mode=0700",
            "--workdir=/workspace",
            "--env=HOME=/tmp",
            "--env=PYTHONDONTWRITEBYTECODE=1",
            image_id,
        ] + harness_argv

    @staticmethod
    def verify_effective_config(
        inspect: Dict[str, Any],
        limits: SandboxResourceLimits,
        source_dir: str,
        harness_dir: str,
    ) -> List[str]:
        """Compare the engine's effective container config with required controls."""
        hc = inspect.get("HostConfig") or {}
        cfg = inspect.get("Config") or {}
        v: List[str] = []
        if cfg.get("User") != SANDBOX_UID_GID:
            v.append(f"user={cfg.get('User')!r}")
        if hc.get("NetworkMode") != "none":
            v.append(f"network={hc.get('NetworkMode')!r}")
        if hc.get("ReadonlyRootfs") is not True:
            v.append("rootfs not read-only")
        if hc.get("Privileged"):
            v.append("privileged")
        if "no-new-privileges" not in (hc.get("SecurityOpt") or []) and "no-new-privileges:true" not in (hc.get("SecurityOpt") or []):
            v.append("no-new-privileges missing")
        if [c.upper() for c in (hc.get("CapDrop") or [])] != ["ALL"] or hc.get("CapAdd"):
            v.append(f"capabilities drop={hc.get('CapDrop')} add={hc.get('CapAdd')}")
        if hc.get("PidMode") not in ("", None) or hc.get("IpcMode") not in ("private",) or hc.get("UTSMode") not in ("", None):
            v.append(f"namespace sharing pid={hc.get('PidMode')!r} ipc={hc.get('IpcMode')!r} uts={hc.get('UTSMode')!r}")
        if hc.get("Memory") != int(limits.memory_mb) * 1024 * 1024:
            v.append(f"memory={hc.get('Memory')}")
        if hc.get("NanoCpus") != int(round(float(limits.cpus) * 1e9)):
            v.append(f"nano_cpus={hc.get('NanoCpus')}")
        if hc.get("PidsLimit") != int(limits.pids_limit):
            v.append(f"pids_limit={hc.get('PidsLimit')}")
        if hc.get("Devices"):
            v.append("devices mapped")
        mounts = inspect.get("Mounts") or []
        expected = {"/source": source_dir, "/bf-harness": harness_dir}
        seen = {}
        for m in mounts:
            dest = m.get("Destination")
            if dest not in expected:
                v.append(f"unexpected mount {dest}")
                continue
            if m.get("Type") != "bind" or m.get("RW") is not False:
                v.append(f"mount {dest} not a read-only bind")
            seen[dest] = m.get("Source", "")
        for dest, src in expected.items():
            if dest not in seen:
                v.append(f"missing mount {dest}")
            elif os.path.normcase(seen[dest].rstrip("\\/")) != os.path.normcase(src.rstrip("\\/")):
                v.append(f"mount {dest} source mismatch")
        if any("docker.sock" in str(m.get("Source", "")) or "docker_engine" in str(m.get("Source", "")) for m in mounts):
            v.append("docker socket mounted")
        tmpfs = hc.get("Tmpfs") or {}
        if set(tmpfs) != {"/workspace", "/tmp"}:
            v.append(f"tmpfs set {sorted(tmpfs)}")
        if hc.get("Binds"):
            v.append(f"legacy binds present {hc.get('Binds')}")
        env_keys = sorted(e.split("=", 1)[0] for e in (cfg.get("Env") or []))
        allowed_env = {"HOME", "PYTHONDONTWRITEBYTECODE", "PATH", "LANG", "GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256"}
        extra_env = [k for k in env_keys if k not in allowed_env]
        if extra_env:
            v.append(f"unexpected env {extra_env}")
        return v

    def _inspect(self, name: str) -> Optional[Dict[str, Any]]:
        rc, out, _, _, _ = self._docker(["container", "inspect", "--format", "{{json .}}", name], timeout=30.0)
        if rc != 0:
            return None
        try:
            return json.loads(out)
        except ValueError:
            return None

    def remove_owned_container(self, name: str, run_label: str) -> bool:
        """Remove a container only if it carries this run's BrainFrog ownership label.

        Returns True when no owned container with that name remains.
        """
        info = self._inspect(name)
        if info is None:
            return True
        labels = (info.get("Config") or {}).get("Labels") or {}
        if labels.get(BRAINFROG_OWNER_LABEL) != BRAINFROG_OWNER_VALUE or labels.get(BRAINFROG_RUN_LABEL) != run_label:
            return False  # not ours: never touch it
        self._docker(["rm", "--force", info.get("Id", name)], timeout=60.0)
        return self._inspect(name) is None

    def list_owned_containers(self, run_label: str) -> List[str]:
        rc, out, _, _, _ = self._docker(
            ["ps", "--all", "--quiet", "--no-trunc", "--filter", f"label={BRAINFROG_RUN_LABEL}={run_label}"], timeout=30.0
        )
        return [line.strip() for line in out.splitlines() if line.strip()] if rc == 0 else []

    @staticmethod
    def _summarize_config(inspect: Dict[str, Any]) -> Dict[str, Any]:
        hc = inspect.get("HostConfig") or {}
        cfg = inspect.get("Config") or {}
        return {
            "container_id": inspect.get("Id"),
            "image_id": inspect.get("Image"),
            "user": cfg.get("User"),
            "network_mode": hc.get("NetworkMode"),
            "readonly_rootfs": hc.get("ReadonlyRootfs"),
            "privileged": hc.get("Privileged"),
            "security_opt": hc.get("SecurityOpt"),
            "cap_drop": hc.get("CapDrop"),
            "cap_add": hc.get("CapAdd"),
            "memory_bytes": hc.get("Memory"),
            "memory_swap_bytes": hc.get("MemorySwap"),
            "nano_cpus": hc.get("NanoCpus"),
            "pids_limit": hc.get("PidsLimit"),
            "pid_mode": hc.get("PidMode"),
            "ipc_mode": hc.get("IpcMode"),
            "tmpfs": hc.get("Tmpfs"),
            "mounts": [
                {"type": m.get("Type"), "source": m.get("Source"), "destination": m.get("Destination"), "rw": m.get("RW")}
                for m in (inspect.get("Mounts") or [])
            ],
            "env_keys": sorted(e.split("=", 1)[0] for e in (cfg.get("Env") or [])),
            "labels": cfg.get("Labels"),
        }

    # -- execution ------------------------------------------------------------

    def execute(
        self,
        source_snapshot_dir: Path,
        source_snapshot_digest: str,
        approved_argv: List[str],
        policy: SandboxPolicy,
        limits: SandboxResourceLimits,
        cwd_relative: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,  # intentionally never forwarded into the container
        run_label: Optional[str] = None,
    ) -> ExecutionResult:
        t0 = time.perf_counter()
        started_at = datetime.now(timezone.utc).isoformat()
        base = dict(backend_version="docker-cli", container_image=self.image_name,
                    working_directory="/workspace", sandbox_policy_summary=policy.to_dict())

        weakened = policy.weakened_controls()
        if weakened:
            return self._blocked_result(InfraErrorCategory.POLICY_WEAKENING_REFUSED,
                                        f"Refusing to run with weakened sandbox controls: {weakened}",
                                        source_snapshot_digest, approved_argv, started_at, t0, **base)
        mode = classify_runner(approved_argv)
        if mode is None:
            return self._blocked_result(InfraErrorCategory.UNSUPPORTED_RUNNER,
                                        "Unsupported verification runner; not executed.",
                                        source_snapshot_digest, approved_argv, started_at, t0, **base)
        if cwd_relative:
            return self._blocked_result(InfraErrorCategory.RUNNER_USAGE_ERROR,
                                        "Per-criterion working directories are not supported in the sandbox.",
                                        source_snapshot_digest, approved_argv, started_at, t0, **base)

        avail, reason = self.is_available()
        if not avail:
            return self._blocked_result(InfraErrorCategory.DOCKER_UNAVAILABLE, reason or "Docker unavailable",
                                        source_snapshot_digest, approved_argv, started_at, t0,
                                        resource_limit_status="unavailable", **base)
        try:
            image = self.resolve_image()
        except SandboxInfraError as exc:
            return self._blocked_result(exc.category, str(exc), source_snapshot_digest, approved_argv, started_at, t0, **base)
        base["container_image_digest"] = image["image_id"]

        snapshot_dir = Path(source_snapshot_dir).resolve()
        if not snapshot_dir.is_dir():
            return self._blocked_result(InfraErrorCategory.MOUNT_UNAVAILABLE, f"Snapshot directory missing: {snapshot_dir}",
                                        source_snapshot_digest, approved_argv, started_at, t0, **base)

        run_label = run_label or f"bf_{secrets.token_hex(8)}"
        container_name = f"bf_verify_{run_label}_{secrets.token_hex(4)}"
        harness_dir, harness_sha = stage_harness()
        base.update(container_name=container_name, harness_sha256=harness_sha)
        state = {"created": False}
        try:
            result = self._run_container(snapshot_dir, source_snapshot_digest, approved_argv, mode, limits,
                                         run_label, container_name, harness_dir, image, base, state, started_at, t0)
        finally:
            # Runs on success, failure, timeout and cancellation (KeyboardInterrupt).
            cleaned = (not state["created"]) or self.remove_owned_container(container_name, run_label)
            shutil.rmtree(harness_dir, ignore_errors=True)
        if not cleaned:
            return self._blocked_result(InfraErrorCategory.CLEANUP_FAILED,
                                        f"BrainFrog-owned container '{container_name}' could not be removed.",
                                        source_snapshot_digest, approved_argv, started_at, t0, **base)
        return result

    def _run_container(
        self,
        snapshot_dir: Path,
        source_snapshot_digest: str,
        approved_argv: List[str],
        mode: str,
        limits: SandboxResourceLimits,
        run_label: str,
        container_name: str,
        harness_dir: Path,
        image: Dict[str, Any],
        base: Dict[str, Any],
        state: Dict[str, bool],
        started_at: str,
        t0: float,
    ) -> ExecutionResult:
        nonce = secrets.token_hex(32)
        harness_argv = ["python", "-I", "-B", "/bf-harness/verification_harness.py", mode,
                        "--nonce-stdin", "--source", "/source", "--workspace", "/workspace", "--"] + list(approved_argv[3:])
        try:
            create_args = self.build_container_create_args(
                container_name, run_label, snapshot_dir, harness_dir, image["image_id"], harness_argv, limits
            )
            base["executed_argv"] = [self.docker_exe] + create_args
            rc, out, err, timed_out, _ = self._docker(create_args, timeout=60.0)
            if timed_out or rc != 0:
                msg = (err or out).strip()
                category = InfraErrorCategory.CONTAINER_CREATE_FAILED
                if "No such image" in msg or "pull" in msg.lower():
                    category = InfraErrorCategory.IMAGE_MISSING
                elif "mount" in msg.lower() or "bind source" in msg.lower():
                    category = InfraErrorCategory.MOUNT_UNAVAILABLE
                return self._blocked_result(category, f"docker create failed: {msg[:300]}",
                                            source_snapshot_digest, approved_argv, started_at, t0, **base)
            state["created"] = True

            inspect = self._inspect(container_name)
            if inspect is None:
                return self._blocked_result(InfraErrorCategory.CONTAINER_CREATE_FAILED, "Created container could not be inspected.",
                                            source_snapshot_digest, approved_argv, started_at, t0, **base)
            base["container_id"] = inspect.get("Id")
            base["effective_container_config"] = self._summarize_config(inspect)
            violations = self.verify_effective_config(inspect, limits, str(snapshot_dir), str(Path(harness_dir).resolve()))
            if inspect.get("Image") != image["image_id"]:
                violations.append(f"container image {inspect.get('Image')} != resolved {image['image_id']}")
            if violations:
                return self._blocked_result(InfraErrorCategory.SECURITY_POLICY_NOT_ENFORCED,
                                            f"Effective container configuration violates sandbox policy: {violations}",
                                            source_snapshot_digest, approved_argv, started_at, t0, **base)

            def _kill() -> None:
                self._docker(["kill", inspect.get("Id", container_name)], timeout=30.0)

            rc, out, err, timed_out, truncated = self._docker(
                ["start", "--attach", "--interactive", container_name],
                timeout=limits.timeout_seconds,
                stdin_text=nonce + "\n",
                on_timeout=_kill,
            )
            post = self._inspect(container_name) or {}
            state = post.get("State") or {}
            stdout_clean = safe_scrub(out)
            stderr_clean = safe_scrub(err)
            base.update(stdout=stdout_clean, stderr=stderr_clean, output_truncated=truncated)

            if timed_out:
                return self._blocked_result(InfraErrorCategory.TIMEOUT,
                                            f"Container execution exceeded {limits.timeout_seconds}s and was killed.",
                                            source_snapshot_digest, approved_argv, started_at, t0,
                                            timeout_status=True, resource_limit_status="timeout_exceeded", **base)
            if state.get("OOMKilled"):
                return self._blocked_result(InfraErrorCategory.RESOURCE_LIMIT, "Container was OOM-killed (memory limit).",
                                            source_snapshot_digest, approved_argv, started_at, t0,
                                            exit_code=state.get("ExitCode"), resource_limit_status="oom_killed", **base)
            exit_code = state.get("ExitCode", rc)
            records, _ = extract_harness_records(out, nonce)
            harness_started = any(r.get("event") == "started" for r in records)
            if not harness_started and exit_code in (125, 126, 127):
                category = InfraErrorCategory.TOOLCHAIN_MISSING if exit_code in (126, 127) else InfraErrorCategory.CONTAINER_START_FAILED
                return self._blocked_result(category, f"Container failed to start runner (exit {exit_code}): {err.strip()[:300]}",
                                            source_snapshot_digest, approved_argv, started_at, t0, exit_code=exit_code, **base)
            if exit_code == 137:
                return self._blocked_result(InfraErrorCategory.RESOURCE_LIMIT, "Container was killed externally (exit 137).",
                                            source_snapshot_digest, approved_argv, started_at, t0, exit_code=exit_code,
                                            resource_limit_status="killed", **base)

            outcome = TestResultAdapter.parse_outcome(
                approved_argv, exit_code, out, err,
                nonce=nonce, expected_source_digest=source_snapshot_digest, output_truncated=truncated,
            )
            # The read-only snapshot must be byte-identical after execution.
            if compute_directory_digest(snapshot_dir) != source_snapshot_digest:
                return self._blocked_result(InfraErrorCategory.SNAPSHOT_MUTATED, "Materialized snapshot changed during execution.",
                                            source_snapshot_digest, approved_argv, started_at, t0, exit_code=exit_code, **base)

            return ExecutionResult(
                backend_type=self.backend_type,
                source_snapshot_digest=source_snapshot_digest,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc).isoformat(),
                duration_ms=(time.perf_counter() - t0) * 1000.0,
                exit_code=exit_code,
                test_count=outcome.test_count,
                test_failure_count=outcome.failure_count,
                infrastructure_error_category=outcome.infrastructure_error_category,
                outcome=outcome,
                **base,
            )
        except SandboxInfraError as exc:
            return self._blocked_result(exc.category, str(exc), source_snapshot_digest, approved_argv, started_at, t0, **base)
        except OSError as exc:
            return self._blocked_result(InfraErrorCategory.DOCKER_UNAVAILABLE, f"Docker CLI failure: {exc}",
                                        source_snapshot_digest, approved_argv, started_at, t0, **base)


# =============================================================================
# LOCAL TRUSTED EXECUTION BACKEND (TRUSTED_OPERATOR MODE)
# =============================================================================

class LocalTrustedExecutionBackend(VerificationExecutionBackend):
    """Executes checks on the host in TRUSTED_OPERATOR mode (no isolation).

    Uses the same harness and snapshot as the container backend: the harness copies
    the materialized snapshot into a fresh temporary workspace and runs there.
    """

    backend_type = "local_hardened_subprocess"

    def __init__(
        self,
        repo_dir: Path,
        timeout_seconds: float = 120.0,
        runner: Optional[Callable] = None,
    ) -> None:
        self.repo_dir = Path(repo_dir).resolve()
        self.timeout_seconds = timeout_seconds
        self.runner = runner or run_hardened_subprocess

    def is_available(self) -> Tuple[bool, Optional[str]]:
        return True, None

    def execute(
        self,
        source_snapshot_dir: Path,
        source_snapshot_digest: str,
        approved_argv: List[str],
        policy: SandboxPolicy,
        limits: SandboxResourceLimits,
        cwd_relative: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        run_label: Optional[str] = None,
    ) -> ExecutionResult:
        t0 = time.perf_counter()
        started_at = datetime.now(timezone.utc).isoformat()
        base = dict(backend_version="host_python", sandbox_policy_summary={"trusted_local": True})

        mode = classify_runner(approved_argv)
        if mode is None:
            return self._blocked_result(InfraErrorCategory.UNSUPPORTED_RUNNER, "Unsupported verification runner; not executed.",
                                        source_snapshot_digest, approved_argv, started_at, t0, **base)
        if cwd_relative:
            return self._blocked_result(InfraErrorCategory.RUNNER_USAGE_ERROR,
                                        "Per-criterion working directories are not supported.",
                                        source_snapshot_digest, approved_argv, started_at, t0, **base)

        harness_dir, harness_sha = stage_harness()
        work_root = Path(tempfile.mkdtemp(prefix="bf_local_ws_"))
        workspace = work_root / "workspace"
        workspace.mkdir()
        nonce = secrets.token_hex(32)
        nonce_file = harness_dir / f"nonce_{secrets.token_hex(4)}"
        nonce_file.write_text(nonce, encoding="utf-8")
        harness_path = harness_dir / "verification_harness.py"
        resolved_cmd = [sys.executable, str(harness_path), mode, "--nonce-file", str(nonce_file),
                        "--source", str(Path(source_snapshot_dir).resolve()), "--workspace", str(workspace),
                        "--"] + list(approved_argv[3:])
        base.update(executed_argv=resolved_cmd, working_directory=str(workspace), harness_sha256=harness_sha)

        if env is not None:
            scrubbed_env = dict(env)
        else:
            safe_keys = {"PATH", "SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "COMSPEC", "PATHEXT", "WINDIR"}
            scrubbed_env = {k: v for k, v in os.environ.items() if k.upper() in safe_keys}

        try:
            proc = self.runner(
                resolved_cmd,
                cwd=workspace,
                timeout=limits.timeout_seconds,
                env=scrubbed_env,
                max_output_bytes=MAX_CAPTURED_OUTPUT_BYTES,
                command_policy=CommandPolicy(allowed_script_paths=[harness_path]),
            )
            stdout_clean = safe_scrub(proc.stdout or "")
            stderr_clean = safe_scrub(proc.stderr or "")
            truncated = bool(getattr(proc, "is_truncated", False))
            base.update(stdout=stdout_clean, stderr=stderr_clean, output_truncated=truncated)
            if getattr(proc, "is_timeout", False):
                return self._blocked_result(InfraErrorCategory.TIMEOUT,
                                            f"Process exceeded {limits.timeout_seconds}s and was terminated.",
                                            source_snapshot_digest, approved_argv, started_at, t0, exit_code=proc.returncode,
                                            timeout_status=True, resource_limit_status="timeout_exceeded", **base)
            if getattr(proc, "error", None) and "denied by allowlist policy" in str(proc.error):
                return self._blocked_result(InfraErrorCategory.INTERNAL_ERROR, str(proc.error),
                                            source_snapshot_digest, approved_argv, started_at, t0, **base)

            outcome = TestResultAdapter.parse_outcome(
                approved_argv, proc.returncode, proc.stdout or "", proc.stderr or "",
                nonce=nonce, expected_source_digest=source_snapshot_digest, output_truncated=truncated,
            )
            if compute_directory_digest(Path(source_snapshot_dir)) != source_snapshot_digest:
                return self._blocked_result(InfraErrorCategory.SNAPSHOT_MUTATED, "Materialized snapshot changed during execution.",
                                            source_snapshot_digest, approved_argv, started_at, t0,
                                            exit_code=proc.returncode, **base)
            return ExecutionResult(
                backend_type=self.backend_type,
                container_image=None,
                container_image_digest=None,
                container_id=None,
                source_snapshot_digest=source_snapshot_digest,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc).isoformat(),
                duration_ms=(time.perf_counter() - t0) * 1000.0,
                exit_code=proc.returncode,
                test_count=outcome.test_count,
                test_failure_count=outcome.failure_count,
                infrastructure_error_category=outcome.infrastructure_error_category,
                outcome=outcome,
                **base,
            )
        except subprocess.TimeoutExpired:
            return self._blocked_result(InfraErrorCategory.TIMEOUT, f"Process exceeded {limits.timeout_seconds}s.",
                                        source_snapshot_digest, approved_argv, started_at, t0,
                                        timeout_status=True, resource_limit_status="timeout_exceeded", **base)
        finally:
            shutil.rmtree(harness_dir, ignore_errors=True)
            shutil.rmtree(work_root, ignore_errors=True)
