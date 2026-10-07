"""Canonical Persistent WorkStore implementation for BrainFrog.

Provides:
- WorkStore: Abstract base interface
- InMemoryWorkStore: Thread-safe in-memory store
- FileWorkStore: Robust filesystem-backed persistent store in .brainfrog/works/
  with atomic replacement, fsync, SHA-256 hashed filenames, path traversal
  protection, cross-process locking, optimistic concurrency control (OCC),
  corruption quarantine, physical partitioning between active and terminal
  works, and bounded retention limits.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from core.runtime.approval import _CrossProcessLock, _get_cross_process_lock
from core.runtime.secret_scrubbing import scrub_secrets
from core.runtime.work import (
    ACTIVE_WORK_STATUSES,
    ALLOWED_TRANSITIONS,
    CURRENT_WORK_SCHEMA_VERSION,
    DEFAULT_MAX_TERMINAL_RETENTION,
    InvalidWorkTransition,
    MAX_ACTIVE_WORKS_PER_ACTOR,
    MAX_GLOBAL_WORKS,
    MAX_WORK_BYTES,
    MAX_WORKS_PER_RESPONSE,
    StaleWorkRevisionError,
    TERMINAL_WORK_STATUSES,
    Work,
    WorkStatus,
    WorkStore,
)

logger = logging.getLogger(__name__)


class WorkNotFoundError(KeyError):
    """Raised when a Work record with the requested ID is not found."""
    pass


class WorkQuotaExceededError(RuntimeError):
    """Raised when active or global Work limits are exceeded."""
    pass


_INVALID_ID_CHARS = re.compile(r"[\x00-\x1f\x7f/\\:*\?\"<>\|]|(?:\.\.)")


def validate_work_id(work_id: str) -> str:
    """Validate Work ID to prevent path traversal, drive letters, and malformed identifiers."""
    if type(work_id) is not str:
        raise ValueError(f"Work ID must be a string, got {type(work_id)}")
    clean = work_id.strip()
    if not clean or len(clean) > 128:
        raise ValueError("Work ID must be a non-empty string with length <= 128")
    if _INVALID_ID_CHARS.search(clean) or clean.startswith((".", "~", "/")):
        raise ValueError(f"Invalid characters or traversal detected in Work ID: '{clean}'")
    return clean


class FileWorkStore(WorkStore):
    """Filesystem-backed persistent Work store (.brainfrog/works/).

    Features:
    - Safe deterministic SHA-256 hashed filenames
    - Complete path traversal immunity
    - Atomic writes via tempfile + os.replace + fsync
    - Cross-process and cross-thread mutual exclusion via advisory locking
    - Physical partitioning between active/ and terminal/ records
    - Monotonic optimistic concurrency control (OCC) revision tracking
    - Corruption quarantine to .brainfrog/works/corrupt/
    - Deterministic bounded retention and quota protection
    """

    def __init__(
        self,
        repo_dir: Optional[Path] = None,
        works_dir: Optional[Path] = None,
        max_terminal_retention: int = DEFAULT_MAX_TERMINAL_RETENTION,
        max_active_per_actor: int = MAX_ACTIVE_WORKS_PER_ACTOR,
        max_global: int = MAX_GLOBAL_WORKS,
    ) -> None:
        if works_dir is not None:
            self.works_dir = Path(works_dir).resolve()
        else:
            base = Path(repo_dir).resolve() if repo_dir else Path.cwd().resolve()
            self.works_dir = base / ".brainfrog" / "works"

        self.terminal_dir = self.works_dir / "terminal"
        self.corrupt_dir = self.works_dir / "corrupt"
        self.locks_dir = self.works_dir / ".locks"
        self.max_terminal_retention = max_terminal_retention
        self.max_active_per_actor = max_active_per_actor
        self.max_global = max_global
        self._memory_cache: Dict[str, Work] = {}
        self._thread_lock = threading.RLock()

        self._ensure_directories()
        self._clean_stale_tmp_files()

    def _ensure_directories(self) -> None:
        try:
            self.works_dir.mkdir(parents=True, exist_ok=True)
            self.terminal_dir.mkdir(parents=True, exist_ok=True)
            self.corrupt_dir.mkdir(parents=True, exist_ok=True)
            self.locks_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"Could not create works directories in {self.works_dir}: {e}")

    def _clean_stale_tmp_files(self) -> None:
        """Remove any abandoned temporary files older than 60 seconds."""
        try:
            now = time.time()
            for d in (self.works_dir, self.terminal_dir):
                if not d.exists():
                    continue
                for f in d.glob(".tmp_*.json"):
                    try:
                        if (now - f.stat().st_mtime) > 60:
                            f.unlink(missing_ok=True)
                    except OSError:
                        pass
        except Exception:
            pass

    def _get_work_path(self, work_id: str, terminal: bool = False) -> Path:
        """Generate safe deterministic hashed filename from validated work_id."""
        clean_id = validate_work_id(work_id)
        raw_hash = hashlib.sha256(clean_id.encode("utf-8")).hexdigest()[:32]
        base_dir = self.terminal_dir if terminal else self.works_dir
        target_path = (base_dir / f"{raw_hash}.json").resolve()
        if not target_path.is_relative_to(self.works_dir):
            raise ValueError(f"Path traversal detected for work_id: {work_id}")
        return target_path

    def _get_lock_path(self, work_id: str) -> Path:
        """Generate safe deterministic lockfile path from validated work_id."""
        clean_id = validate_work_id(work_id)
        raw_hash = hashlib.sha256(clean_id.encode("utf-8")).hexdigest()[:32]
        target_path = (self.locks_dir / f"{raw_hash}.lock").resolve()
        if not target_path.is_relative_to(self.works_dir):
            raise ValueError(f"Path traversal detected for lockfile work_id: {work_id}")
        return target_path

    def _get_work_lock(self, work_id: str, timeout: float = 10.0) -> _CrossProcessLock:
        return _get_cross_process_lock(self._get_lock_path(work_id), timeout=timeout)

    def _get_quota_lock(self, timeout: float = 10.0) -> _CrossProcessLock:
        quota_lock_path = (self.locks_dir / "__work_quota__.lock").resolve()
        return _get_cross_process_lock(quota_lock_path, timeout=timeout)

    def _quarantine_file(self, file_path: Path, reason: str) -> None:
        """Safely quarantine a corrupted work record."""
        try:
            self.corrupt_dir.mkdir(parents=True, exist_ok=True)
            timestamp = int(time.time())
            quarantined = self.corrupt_dir / f"{timestamp}_{file_path.name}"
            shutil.move(str(file_path), str(quarantined))
            logger.warning(f"Quarantined corrupt work file '{file_path.name}' to '{quarantined.name}'. Reason: {reason}")
        except Exception as e:
            logger.error(f"Failed to quarantine corrupt file '{file_path}': {e}")

    def _read_work_from_file(self, file_path: Path) -> Optional[Work]:
        """Read and deserialize Work from disk, quarantining corrupt files."""
        if not file_path.exists() or file_path.is_dir():
            return None
        try:
            content = file_path.read_text(encoding="utf-8")
            if not content.strip():
                self._quarantine_file(file_path, "empty file")
                return None
            data = json.loads(content)
            if not isinstance(data, dict):
                self._quarantine_file(file_path, "JSON root is not a dictionary")
                return None
            return Work.from_dict(data)
        except json.JSONDecodeError as exc:
            self._quarantine_file(file_path, f"JSON decode error: {exc}")
            return None
        except Exception as exc:
            self._quarantine_file(file_path, f"Validation error: {exc}")
            return None

    def _write_work_to_file(self, work: Work, terminal: bool = False) -> Path:
        """Atomically persist Work to disk with fsync and size bounding."""
        data = work.to_persistence_dict()
        serialized = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
        encoded = serialized.encode("utf-8")
        if len(encoded) > MAX_WORK_BYTES:
            raise ValueError(
                f"Work record '{work.id}' size ({len(encoded)} bytes) exceeds maximum limit ({MAX_WORK_BYTES} bytes)"
            )

        target_path = self._get_work_path(work.id, terminal=terminal)
        parent_dir = target_path.parent
        parent_dir.mkdir(parents=True, exist_ok=True)

        tmp_token = secrets.token_hex(8)
        tmp_path = parent_dir / f".tmp_{tmp_token}.json"

        try:
            with open(tmp_path, "wb") as f:
                f.write(encoded)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(str(tmp_path), str(target_path))
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

        # If saving as terminal, ensure any old active file is cleaned up
        if terminal:
            active_path = self._get_work_path(work.id, terminal=False)
            if active_path.exists():
                try:
                    active_path.unlink()
                except OSError:
                    pass
        return target_path

    def create(self, work: Work) -> Work:
        if not isinstance(work, Work):
            raise TypeError("Expected Work instance")
        validate_work_id(work.id)

        with self._get_quota_lock():
            with self._get_work_lock(work.id):
                active_path = self._get_work_path(work.id, terminal=False)
                terminal_path = self._get_work_path(work.id, terminal=True)
                if active_path.exists() or terminal_path.exists():
                    raise ValueError(f"Work with id '{work.id}' already exists")

                # Quota enforcement
                if not work.is_terminal:
                    active_files = [f for f in self.works_dir.glob("*.json") if not f.name.startswith(".tmp_")]
                    if len(active_files) >= self.max_global:
                        raise WorkQuotaExceededError("Global active Work quota exceeded")

                    if work.actor_id:
                        actor_count = 0
                        for f in active_files:
                            w = self._read_work_from_file(f)
                            if w and w.actor_id == work.actor_id and not w.is_terminal:
                                actor_count += 1
                        if actor_count >= self.max_active_per_actor:
                            raise WorkQuotaExceededError(
                                f"Active Work quota exceeded for actor '{work.actor_id}'"
                            )

                self._write_work_to_file(work, terminal=work.is_terminal)
                if work.is_terminal:
                    self._prune_terminal_records()
                return work

    def get(self, work_id: str) -> Optional[Work]:
        try:
            validate_work_id(work_id)
        except ValueError:
            return None

        with self._get_work_lock(work_id):
            active_path = self._get_work_path(work_id, terminal=False)
            if active_path.exists():
                return self._read_work_from_file(active_path)

            terminal_path = self._get_work_path(work_id, terminal=True)
            if terminal_path.exists():
                return self._read_work_from_file(terminal_path)
            return None

    def save(self, work: Work, expected_revision: Optional[int] = None) -> Work:
        if not isinstance(work, Work):
            raise TypeError("Expected Work instance")
        validate_work_id(work.id)

        with self._get_work_lock(work.id):
            existing = self.get(work.id)
            if existing is None:
                raise KeyError(f"Work with id '{work.id}' does not exist")

            # Optimistic concurrency control (Section 12)
            if expected_revision is not None and existing.revision != expected_revision:
                raise StaleWorkRevisionError(
                    f"Revision conflict: expected {expected_revision}, but found {existing.revision}"
                )
            if work.revision > 1 and work.revision < existing.revision:
                raise StaleWorkRevisionError(
                    f"Stale revision: update has revision {work.revision}, but store has revision {existing.revision}"
                )

            # State transition validation
            if existing.status != work.status:
                allowed = ALLOWED_TRANSITIONS.get(existing.status, frozenset())
                is_explicit_cancel = (
                    work.status == WorkStatus.CANCELLED
                    and existing.status in (WorkStatus.CREATED, WorkStatus.PLANNING, WorkStatus.APPROVAL_REQUIRED)
                )
                if work.status not in allowed and not is_explicit_cancel:
                    raise InvalidWorkTransition(
                        f"Cannot transition Work in store from '{existing.status.value}' to '{work.status.value}'"
                    )
            elif existing.is_terminal and existing != work:
                raise InvalidWorkTransition(
                    f"Cannot modify terminal Work record in status '{existing.status.value}'"
                )

            new_rev = existing.revision + 1 if work.revision <= existing.revision else work.revision
            persisted = replace(work, revision=new_rev)
            self._write_work_to_file(persisted, terminal=persisted.is_terminal)

            if persisted.is_terminal:
                self._prune_terminal_records()
            return persisted

    def update(self, work_id: str, mutate_fn: Callable[[Work], Work]) -> Work:
        validate_work_id(work_id)
        with self._get_work_lock(work_id):
            existing = self.get(work_id)
            if existing is None:
                raise KeyError(f"Work with id '{work_id}' does not exist")
            updated = mutate_fn(existing)
            return self.save(updated, expected_revision=existing.revision)

    def delete(self, work_id: str) -> bool:
        try:
            validate_work_id(work_id)
        except ValueError:
            return False

        with self._get_work_lock(work_id):
            deleted = False
            for terminal in (False, True):
                path = self._get_work_path(work_id, terminal=terminal)
                if path.exists():
                    try:
                        path.unlink()
                        deleted = True
                    except OSError:
                        pass
            return deleted

    def list_all(self, limit: Optional[int] = None) -> List[Work]:
        results: List[Work] = []
        for d in (self.works_dir, self.terminal_dir):
            if not d.exists():
                continue
            for f in d.glob("*.json"):
                if f.name.startswith(".tmp_"):
                    continue
                w = self._read_work_from_file(f)
                if w is not None:
                    results.append(w)
                    if limit is not None and len(results) >= limit:
                        break
            if limit is not None and len(results) >= limit:
                break
        results.sort(key=lambda x: x.updated_at, reverse=True)
        return results[:limit] if limit is not None else results

    def list_active(self, limit: int = 50) -> List[Work]:
        results: List[Work] = []
        if not self.works_dir.exists():
            return results
        for f in self.works_dir.glob("*.json"):
            if f.name.startswith(".tmp_"):
                continue
            w = self._read_work_from_file(f)
            if w is not None and not w.is_terminal:
                results.append(w)
                if len(results) >= limit:
                    break
        results.sort(key=lambda x: x.updated_at, reverse=True)
        return results[:limit]

    def list_for_actor(
        self,
        actor_id: str,
        status_filter: Optional[str] = None,
        limit: int = MAX_WORKS_PER_RESPONSE,
    ) -> List[Work]:
        clean_actor = actor_id.strip()
        results: List[Work] = []
        sf = status_filter.lower().strip() if status_filter else None

        # Determine target directories to scan based on filter
        dirs = []
        if sf == "active":
            dirs = [self.works_dir]
        elif sf in ("failed", "done"):
            dirs = [self.terminal_dir]
        else:
            dirs = [self.works_dir, self.terminal_dir]

        for d in dirs:
            if not d.exists():
                continue
            for f in d.glob("*.json"):
                if f.name.startswith(".tmp_"):
                    continue
                w = self._read_work_from_file(f)
                if w is None or w.actor_id != clean_actor:
                    continue
                if sf:
                    if sf == "active" and w.is_terminal:
                        continue
                    elif sf == "failed" and w.status != WorkStatus.FAILED:
                        continue
                    elif sf == "done" and w.status != WorkStatus.DONE:
                        continue
                    elif sf not in ("active", "failed", "done", "all") and w.status.value != sf:
                        continue
                results.append(w)
                if len(results) >= (limit * 2):
                    break

        results.sort(key=lambda x: x.updated_at, reverse=True)
        return results[:limit]

    def _prune_terminal_records(self) -> int:
        try:
            if not self.terminal_dir.exists():
                return 0
            terminal_files = [f for f in self.terminal_dir.glob("*.json") if not f.name.startswith(".tmp_")]
            if len(terminal_files) <= self.max_terminal_retention:
                return 0
            terminal_files.sort(key=lambda p: p.stat().st_mtime)
            excess = len(terminal_files) - self.max_terminal_retention
            pruned = 0
            for f in terminal_files[:excess]:
                try:
                    f.unlink(missing_ok=True)
                    pruned += 1
                except OSError:
                    pass
            return pruned
        except Exception as e:
            logger.warning(f"Failed to prune terminal work files: {e}")
            return 0

    def cleanup_terminal(
        self,
        actor_id: Optional[str] = None,
        max_retention: int = DEFAULT_MAX_TERMINAL_RETENTION,
    ) -> int:
        with self._get_quota_lock():
            if not self.terminal_dir.exists():
                return 0
            clean_actor = actor_id.strip() if actor_id else None
            matching_files: List[Tuple[Path, float]] = []
            for f in self.terminal_dir.glob("*.json"):
                if f.name.startswith(".tmp_"):
                    continue
                w = self._read_work_from_file(f)
                if w is not None and (clean_actor is None or w.actor_id == clean_actor):
                    matching_files.append((f, w.updated_at))

            if len(matching_files) <= max_retention:
                return 0
            matching_files.sort(key=lambda pair: pair[1])
            excess = len(matching_files) - max_retention
            pruned = 0
            for f, _ in matching_files[:excess]:
                try:
                    f.unlink(missing_ok=True)
                    pruned += 1
                except OSError:
                    pass
            return pruned


# Export classes
from core.runtime.work import InMemoryWorkStore

__all__ = [
    "FileWorkStore",
    "InMemoryWorkStore",
    "StaleWorkRevisionError",
    "WorkNotFoundError",
    "WorkQuotaExceededError",
    "WorkStore",
    "validate_work_id",
]
