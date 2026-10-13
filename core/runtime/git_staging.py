"""Git Staging Hardening & Target-Only Isolation Module.

Enforces Phase 2A Security Invariants:
1. Target-Only Staging: Never execute broad staging (e.g. `git add -A`, `git add .`).
2. Contamination Protection: Reject staging if Git index contains pre-existing staged files outside authorized targets.
3. Overlapping Protection: Reject staging if Git index contains pre-existing staged files overlapping targets.
4. Non-Destructive Preservation: Never silently unstage, reset, stash, or overwrite developer changes on rejection.
5. Path Traversal Prevention: Ensure all target paths resolve strictly within the canonical repository root.
6. Mutation Integrity: Explicitly handle file creation, modification, deletion, and rename operations.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Sequence, Set, Tuple, Union

from core.runtime.transaction import (
    OperationStatus,
    OperationType,
    TransactionOperation,
    TransactionResult,
    TransactionStatus,
)


class GitStagingError(Exception):
    """Base exception for Git staging security violations."""


class PathTraversalSecurityError(GitStagingError):
    """Raised when a target path resolves outside the repository root."""


class StagedIndexContaminatedError(GitStagingError):
    """Raised when Git index contains pre-existing staged changes outside authorized targets."""


class PreExistingStagedTargetError(GitStagingError):
    """Raised when Git index contains pre-existing staged changes overlapping authorized targets."""


@dataclass(frozen=True)
class GitStageResult:
    """Outcome of an authorized Git staging operation."""

    success: bool
    staged_paths: Tuple[str, ...] = ()
    error: Optional[str] = None
    rejection_code: Optional[str] = None


def get_git_metadata_dir(repo_path: Path) -> Optional[Path]:
    """Find canonical path to Git metadata directory (.git directory or worktree gitdir)."""
    dot_git = repo_path / ".git"
    if not dot_git.exists():
        return None
    if dot_git.is_dir():
        return dot_git.resolve()
    if dot_git.is_file():
        try:
            content = dot_git.read_text(encoding="utf-8").strip()
            if content.startswith("gitdir:"):
                raw_dir = content.split("gitdir:", 1)[1].strip()
                gitdir_path = Path(raw_dir)
                if not gitdir_path.is_absolute():
                    return (repo_path / gitdir_path).resolve()
                return gitdir_path.resolve()
        except Exception:
            pass
    return None


def resolve_canonical_repo_path(repo_dir: Path | str, target: str) -> str:
    """Resolve and validate a target path strictly within repository boundaries.

    Returns normalized repository-relative POSIX path (e.g. 'src/app.py').
    Raises PathTraversalSecurityError if target escapes repository, contains option-like
    components, or points to internal .git metadata.
    """
    if not isinstance(target, str) or not target.strip():
        raise PathTraversalSecurityError("Target path must be a non-empty string.")

    norm_target = target.replace("\\", "/").strip()
    if "\0" in norm_target:
        raise PathTraversalSecurityError("Target path contains illegal null bytes.")

    # Reject option-like or dash-prefixed components (flag injection defense)
    for part in norm_target.split("/"):
        if part.startswith("-"):
            raise PathTraversalSecurityError(
                f"Target path '{target}' contains illegal option-like component '{part}' starting with '-'."
            )

    repo_path = Path(repo_dir).resolve()
    target_path = Path(norm_target)

    if target_path.is_absolute():
        resolved = target_path.resolve()
    else:
        resolved = (repo_path / target_path).resolve()

    try:
        rel = resolved.relative_to(repo_path)
    except ValueError:
        raise PathTraversalSecurityError(
            f"Path traversal detected: target '{target}' resolves to '{resolved}' outside repo '{repo_path}'"
        )

    rel_posix = rel.as_posix()
    if rel_posix in (".", ""):
        raise PathTraversalSecurityError("Target path cannot resolve to repository root directory itself.")

    # Reject internal .git directory or metadata descendants
    if rel.parts and ".git" in rel.parts:
        raise PathTraversalSecurityError(f"Target path '{target}' points to internal .git repository metadata.")

    # Reject targets resolving into worktree/submodule gitdir if .git is a file
    git_dir = get_git_metadata_dir(repo_path)
    if git_dir is not None:
        if resolved == git_dir:
            raise PathTraversalSecurityError(f"Target path '{target}' resolves to internal Git directory '{git_dir}'.")
        try:
            resolved.relative_to(git_dir)
            raise PathTraversalSecurityError(f"Target path '{target}' resolves to internal Git directory '{git_dir}'.")
        except ValueError:
            pass

    return rel_posix


def inspect_staged_index(repo_dir: Path | str) -> List[str]:
    """Return repository-relative POSIX paths of all files currently staged in Git index."""
    proc = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "-z"],
        cwd=str(repo_dir),
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise GitStagingError(f"Failed to inspect git index: {err}")

    raw_output = proc.stdout
    if not raw_output:
        return []

    entries = raw_output.split(b"\0")
    staged_paths: List[str] = []
    for entry in entries:
        if not entry:
            continue
        decoded = entry.decode("utf-8", errors="replace").strip().replace("\\", "/")
        if decoded:
            staged_paths.append(decoded)
    return sorted(staged_paths)


def inspect_unstaged_diff(repo_dir: Path | str, targets: Sequence[str]) -> List[str]:
    """Return repository-relative POSIX paths of targets having unstaged modifications relative to index."""
    if not targets:
        return []

    cmd = ["git", "diff", "--name-only", "-z", "--"] + list(targets)
    proc = subprocess.run(
        cmd,
        cwd=str(repo_dir),
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise GitStagingError(f"Failed to inspect unstaged git diff: {err}")

    raw_output = proc.stdout
    if not raw_output:
        return []

    entries = raw_output.split(b"\0")
    unstaged: List[str] = []
    for entry in entries:
        if not entry:
            continue
        decoded = entry.decode("utf-8", errors="replace").strip().replace("\\", "/")
        if decoded:
            unstaged.append(decoded)
    return sorted(unstaged)


def is_file_tracked_in_git(repo_dir: Path | str, rel_path: str) -> bool:
    """Return True if rel_path is tracked in Git index or HEAD."""
    proc = subprocess.run(
        ["git", "ls-files", "--", rel_path],
        cwd=str(repo_dir),
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return False
    return bool(proc.stdout.strip())


def extract_authorized_target_paths(
    repo_dir: Path | str,
    targets: Sequence[Union[str, TransactionOperation]],
) -> Tuple[Set[str], List[TransactionOperation], List[str]]:
    """Resolve and validate authorized targets into canonical relative paths.

    Returns:
        (canonical_paths_set, operation_records, string_targets)
    """
    canonical_paths: Set[str] = set()
    ops: List[TransactionOperation] = []
    str_targets: List[str] = []

    for item in targets:
        if isinstance(item, TransactionOperation):
            # Enforce that only successfully executed operations are authorized
            op_status = item.status.value if hasattr(item.status, "value") else str(item.status)
            if op_status != OperationStatus.EXECUTED.value:
                continue

            canon_target = resolve_canonical_repo_path(repo_dir, item.target)
            canonical_paths.add(canon_target)
            if item.new_target:
                canon_new = resolve_canonical_repo_path(repo_dir, item.new_target)
                canonical_paths.add(canon_new)
            ops.append(item)
        elif isinstance(item, str):
            canon_target = resolve_canonical_repo_path(repo_dir, item)
            canonical_paths.add(canon_target)
            str_targets.append(canon_target)
        else:
            raise TypeError(f"Unsupported target type in staging: {type(item)}")

    return canonical_paths, ops, str_targets


def stage_authorized_targets(
    repo_dir: Path | str,
    targets: Sequence[Union[str, TransactionOperation]],
    *,
    last_tx_result: Optional[TransactionResult] = None,
    allow_pre_staged_identical: bool = False,
) -> GitStageResult:
    """Safely stage ONLY authorized transaction targets into Git index.

    Strict Invariants:
    1. Zero broad staging (no `git add -A` or wildcards).
    2. Fails closed if Git index contains changes outside authorized targets.
    3. Fails closed if Git index contains pre-existing changes overlapping targets.
    4. Preserves developer index and working tree on rejection (no `git reset`).
    5. Safely stages file additions, modifications, deletions, and renames.
    """
    repo_path = Path(repo_dir).resolve()
    if not repo_path.exists() or not (repo_path / ".git").exists():
        return GitStageResult(
            success=False,
            error=f"Target directory '{repo_path}' is not a valid Git repository.",
            rejection_code="not_a_git_repo",
        )

    # 0. Validate transaction provenance if last_tx_result is provided
    if last_tx_result is not None:
        is_committed = getattr(last_tx_result, "committed", False) is True
        status = getattr(last_tx_result, "status", None)
        is_rolled_back = getattr(last_tx_result, "rolled_back", False) is True
        if not is_committed or status != TransactionStatus.COMMITTED or is_rolled_back:
            return GitStageResult(
                success=False,
                error=f"Transaction result '{getattr(last_tx_result, 'transaction_id', '?')}' is not committed (status={status}, rolled_back={is_rolled_back}). Staging rejected fail-closed.",
                rejection_code="uncommitted_transaction",
            )

    # 1. Resolve and validate canonical paths
    try:
        canonical_paths, ops, str_targets = extract_authorized_target_paths(repo_path, targets)
    except PathTraversalSecurityError as exc:
        return GitStageResult(
            success=False,
            error=str(exc),
            rejection_code="path_traversal",
        )

    # 2. Inspect current Git index state before mutating
    try:
        currently_staged = set(inspect_staged_index(repo_path))
    except GitStagingError as exc:
        return GitStageResult(
            success=False,
            error=str(exc),
            rejection_code="index_inspection_failed",
        )

    # 3. Contamination Check: reject if index contains staged changes outside authorized targets
    unrelated_staged = currently_staged - canonical_paths
    if unrelated_staged:
        return GitStageResult(
            success=False,
            error=f"Git index contains pre-existing staged changes outside authorized targets: {sorted(unrelated_staged)}",
            rejection_code="contaminated_index",
        )

    # 4. Overlapping Check: reject if index contains pre-existing staged changes on authorized targets
    overlapping_staged = currently_staged & canonical_paths
    if overlapping_staged:
        if last_tx_result is not None:
            # Transaction executed on disk; any pre-staged index changes are untrusted user changes
            return GitStageResult(
                success=False,
                error=f"Git index already contains pre-existing staged changes overlapping targets: {sorted(overlapping_staged)}",
                rejection_code="pre_existing_staged_target",
            )
        else:
            # Direct caller without transaction record: check if disk diverged from staged index
            try:
                diverged = inspect_unstaged_diff(repo_path, sorted(overlapping_staged))
            except GitStagingError as exc:
                return GitStageResult(
                    success=False,
                    error=str(exc),
                    rejection_code="index_inspection_failed",
                )
            if diverged:
                return GitStageResult(
                    success=False,
                    error=f"Target(s) have pre-existing staged changes in Git index that conflict with working tree: {sorted(diverged)}",
                    rejection_code="pre_existing_staged_target",
                )
            if not allow_pre_staged_identical:
                return GitStageResult(
                    success=False,
                    error=f"Git index already contains pre-existing staged changes overlapping targets: {sorted(overlapping_staged)}",
                    rejection_code="pre_existing_staged_target",
                )

    # If there are no targets to stage, verify index remains clean
    if not canonical_paths:
        if targets:
            return GitStageResult(
                success=False,
                error="No valid executed operations found among provided transaction targets.",
                rejection_code="unauthorized_targets",
            )
        return GitStageResult(success=True, staged_paths=())

    # 5. Execute safe, explicit target-only staging
    staged_so_far: List[str] = []
    try:
        # A. Process TransactionOperation records
        for op in ops:
            canon_target = resolve_canonical_repo_path(repo_path, op.target)
            target_file = repo_path / canon_target
            op_type_val = op.type.value if hasattr(op.type, "value") else str(op.type)

            if op_type_val == OperationType.DELETE_FILE.value:
                if is_file_tracked_in_git(repo_path, canon_target):
                    proc = subprocess.run(
                        ["git", "add", "--", canon_target],
                        cwd=str(repo_path),
                        capture_output=True,
                        check=False,
                    )
                    if proc.returncode != 0:
                        raise GitStagingError(f"Failed to stage deletion for '{canon_target}': {proc.stderr.decode()}")
                staged_so_far.append(canon_target)

            elif op_type_val == OperationType.RENAME_FILE.value and op.new_target:
                canon_new = resolve_canonical_repo_path(repo_path, op.new_target)
                proc = subprocess.run(
                    ["git", "add", "--", canon_target, canon_new],
                    cwd=str(repo_path),
                    capture_output=True,
                    check=False,
                )
                if proc.returncode != 0:
                    raise GitStagingError(
                        f"Failed to stage rename '{canon_target}' -> '{canon_new}': {proc.stderr.decode()}"
                    )
                staged_so_far.extend([canon_target, canon_new])

            else:  # CREATE_FILE, MODIFY_FILE
                if target_file.exists():
                    proc = subprocess.run(
                        ["git", "add", "--", canon_target],
                        cwd=str(repo_path),
                        capture_output=True,
                        check=False,
                    )
                    if proc.returncode != 0:
                        raise GitStagingError(f"Failed to stage file '{canon_target}': {proc.stderr.decode()}")
                    staged_so_far.append(canon_target)
                elif is_file_tracked_in_git(repo_path, canon_target):
                    # File was deleted on disk
                    proc = subprocess.run(
                        ["git", "add", "--", canon_target],
                        cwd=str(repo_path),
                        capture_output=True,
                        check=False,
                    )
                    if proc.returncode != 0:
                        raise GitStagingError(f"Failed to stage deletion for '{canon_target}': {proc.stderr.decode()}")
                    staged_so_far.append(canon_target)

        # B. Process string targets (when operations not provided)
        for rel_target in str_targets:
            target_file = repo_path / rel_target
            if target_file.exists() or is_file_tracked_in_git(repo_path, rel_target):
                proc = subprocess.run(
                    ["git", "add", "--", rel_target],
                    cwd=str(repo_path),
                    capture_output=True,
                    check=False,
                )
                if proc.returncode != 0:
                    raise GitStagingError(f"Failed to stage target '{rel_target}': {proc.stderr.decode()}")
                staged_so_far.append(rel_target)

    except Exception as exc:
        # Failure during staging: clean up ONLY what was staged in this attempt
        unstage_target_paths(repo_path, staged_so_far)
        return GitStageResult(
            success=False,
            error=f"Error executing explicit target staging: {exc}",
            rejection_code="staging_execution_failed",
        )

    # 6. Post-staging verification: guarantee no unrelated files were staged
    try:
        final_staged = set(inspect_staged_index(repo_path))
    except GitStagingError as exc:
        unstage_target_paths(repo_path, staged_so_far)
        return GitStageResult(
            success=False,
            error=f"Post-staging index verification failed: {exc}",
            rejection_code="verification_failed",
        )

    unexpected_staged = final_staged - canonical_paths
    if unexpected_staged:
        unstage_target_paths(repo_path, staged_so_far)
        return GitStageResult(
            success=False,
            error=f"Post-staging invariant violated: unexpected files entered index: {sorted(unexpected_staged)}",
            rejection_code="unexpected_files_staged",
        )

    return GitStageResult(
        success=True,
        staged_paths=tuple(sorted(canonical_paths & final_staged)),
    )


def unstage_target_paths(repo_dir: Path | str, targets: Sequence[str]) -> None:
    """Safely unstage ONLY the specified target paths without resetting unrelated files."""
    if not targets:
        return

    repo_path = Path(repo_dir).resolve()
    valid_rel_targets: List[str] = []
    for t in targets:
        try:
            rel = resolve_canonical_repo_path(repo_path, t)
            valid_rel_targets.append(rel)
        except Exception:
            continue

    if not valid_rel_targets:
        return

    # Check if HEAD exists in repository
    head_proc = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=str(repo_path),
        capture_output=True,
        check=False,
    )
    if head_proc.returncode == 0:
        subprocess.run(
            ["git", "reset", "HEAD", "--"] + valid_rel_targets,
            cwd=str(repo_path),
            capture_output=True,
            check=False,
        )
    else:
        # No HEAD yet (initial commit in fresh repo)
        subprocess.run(
            ["git", "rm", "--cached", "-r", "--quiet", "--"] + valid_rel_targets,
            cwd=str(repo_path),
            capture_output=True,
            check=False,
        )
