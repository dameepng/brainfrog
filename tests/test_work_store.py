"""Unit and integration tests for Phase 15H WorkStore & FileWorkStore.

Validates:
- CRUD lifecycle, queries, and filters
- Atomic temp-file persistence and fsync
- Restart survivability across process restarts
- Path traversal immunity and Work ID validation
- Corruption quarantine and schema versioning
- Bounded resource limits and terminal record pruning
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from core.runtime.capabilities import Capabilities, FilesystemPolicy
from core.runtime.work import (
    CURRENT_WORK_SCHEMA_VERSION,
    InvalidWorkTransition,
    MAX_WORK_BYTES,
    VerificationResult,
    VerificationStatus,
    Work,
    WorkFailure,
    WorkStatus,
)
from core.runtime.work_store import (
    FileWorkStore,
    InMemoryWorkStore,
    StaleWorkRevisionError,
    WorkQuotaExceededError,
    validate_work_id,
)


class TestWorkIdValidation(unittest.TestCase):
    """Test path traversal immunity and identifier sanitization."""

    def test_valid_ids(self) -> None:
        self.assertEqual(validate_work_id("work_12345678"), "work_12345678")
        self.assertEqual(validate_work_id("work-abc-def"), "work-abc-def")
        self.assertEqual(validate_work_id("work_abc_123"), "work_abc_123")

    def test_invalid_and_traversal_ids(self) -> None:
        malicious = [
            "../etc/passwd",
            "..\\windows\\system32",
            "/absolute/path",
            "C:\\root",
            "\\\\unc\\share",
            "work\x00null",
            "work/sub",
            "work\\sub",
            "work:colon",
            "work*star",
            "work?mark",
            "work<arrow",
            "work>arrow",
            "work|pipe",
            "work\"quote",
            "",
            "   ",
            "a" * 200,  # too long
        ]
        for bad in malicious:
            with self.subTest(bad_id=bad):
                with self.assertRaises(ValueError):
                    validate_work_id(bad)


class TestFileWorkStore(unittest.TestCase):
    """Test filesystem-backed persistent Work storage."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.repo_dir = Path(self.temp_dir)
        self.store = FileWorkStore(repo_dir=self.repo_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_create_and_get(self) -> None:
        work = Work(
            id="work_test_001",
            intent="Implement caching layer",
            goal="Add Redis cache to API",
            actor_id="alice",
            channel="telegram",
            session_id="sess_123",
            session_incarnation_id="inc_456",
        )
        created = self.store.create(work)
        self.assertEqual(created.id, "work_test_001")

        retrieved = self.store.get("work_test_001")
        self.assertIsNotNone(retrieved)
        assert retrieved is not None
        self.assertEqual(retrieved.id, "work_test_001")
        self.assertEqual(retrieved.intent, "Implement caching layer")
        self.assertEqual(retrieved.actor_id, "alice")
        self.assertEqual(retrieved.channel, "telegram")
        self.assertEqual(retrieved.session_id, "sess_123")
        self.assertEqual(retrieved.session_incarnation_id, "inc_456")

    def test_create_duplicate_rejected(self) -> None:
        work = Work(id="work_dup_1", intent="First", goal="First")
        self.store.create(work)
        with self.assertRaises(ValueError):
            self.store.create(work)

    def test_get_not_found(self) -> None:
        self.assertIsNone(self.store.get("work_nonexistent"))

    def test_restart_survivability(self) -> None:
        """Verify persisted Work survives complete process/instance restarts."""
        work = Work(
            id="work_survive_1",
            intent="Database schema migration",
            goal="Add index to users table",
            actor_id="bob",
            channel="whatsapp",
            session_id="sess_bob_1",
            session_incarnation_id="inc_bob_1",
        )
        self.store.create(work)

        # Simulate complete restart by instantiating a brand-new FileWorkStore
        new_store = FileWorkStore(repo_dir=self.repo_dir)
        restored = new_store.get("work_survive_1")
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.id, "work_survive_1")
        self.assertEqual(restored.actor_id, "bob")
        self.assertEqual(restored.goal, "Add index to users table")

    def test_atomic_save_and_update(self) -> None:
        work = Work(id="work_atom_1", intent="Task", goal="Goal")
        self.store.create(work)

        # Update non-terminal work
        updated = self.store.update("work_atom_1", lambda w: w.transition(WorkStatus.PLANNING))
        self.assertEqual(updated.status, WorkStatus.PLANNING)
        self.assertEqual(updated.revision, 2)

        retrieved = self.store.get("work_atom_1")
        assert retrieved is not None
        self.assertEqual(retrieved.status, WorkStatus.PLANNING)
        self.assertEqual(retrieved.revision, 2)

    def test_terminal_partitioning(self) -> None:
        """Verify terminal works are physically stored in terminal/ directory."""
        work = Work(id="work_term_1", intent="Task", goal="Goal")
        self.store.create(work)

        # Transition to DONE
        planning = self.store.save(work.transition(WorkStatus.PLANNING))
        appr = self.store.save(planning.transition(WorkStatus.APPROVAL_REQUIRED))
        executing = self.store.save(appr.transition(WorkStatus.EXECUTING))
        verifying = self.store.save(executing.transition(WorkStatus.VERIFYING))
        done = self.store.save(verifying.transition(WorkStatus.DONE))

        self.assertTrue(done.is_terminal)

        # Active directory should not have it
        active_files = [f for f in self.store.works_dir.glob("*.json") if not f.name.startswith(".tmp_")]
        self.assertEqual(len(active_files), 0)

        # Terminal directory should have it
        terminal_files = [f for f in self.store.terminal_dir.glob("*.json") if not f.name.startswith(".tmp_")]
        self.assertEqual(len(terminal_files), 1)

        # Retrieval still succeeds
        retrieved = self.store.get("work_term_1")
        self.assertIsNotNone(retrieved)
        assert retrieved is not None
        self.assertEqual(retrieved.status, WorkStatus.DONE)

    def test_corruption_quarantine(self) -> None:
        """Verify corrupted or invalid JSON files are quarantined and do not crash the runtime."""
        # Create a valid work first
        work = Work(id="work_corrupt_test", intent="Task", goal="Goal")
        self.store.create(work)

        target_file = self.store._get_work_path("work_corrupt_test", terminal=False)
        self.assertTrue(target_file.exists())

        # Corrupt the file with invalid JSON content
        target_file.write_text("{ corrupt json not valid ...", encoding="utf-8")

        # Query should return None and quarantine file
        retrieved = self.store.get("work_corrupt_test")
        self.assertIsNone(retrieved)

        # Quarantined directory should have the file
        quarantined = list(self.store.corrupt_dir.glob("*_*.json"))
        self.assertGreaterEqual(len(quarantined), 1)

    def test_future_schema_version_fails_closed(self) -> None:
        """Verify unknown future schema version fails closed and is quarantined."""
        work = Work(id="work_future_ver", intent="Task", goal="Goal")
        self.store.create(work)

        target_file = self.store._get_work_path("work_future_ver", terminal=False)
        data = work.to_persistence_dict()
        data["schema_version"] = 999  # Future schema version
        target_file.write_text(json.dumps(data), encoding="utf-8")

        # Must fail closed and quarantine
        retrieved = self.store.get("work_future_ver")
        self.assertIsNone(retrieved)
        quarantined = list(self.store.corrupt_dir.glob("*_*.json"))
        self.assertGreaterEqual(len(quarantined), 1)

    def test_size_bounding(self) -> None:
        """Verify oversized records are rejected."""
        giant_goal = "X" * (MAX_WORK_BYTES + 1000)
        # Goal clamp in constructor keeps it bounded, but let's test exceeding byte limits
        work = Work(id="work_oversized", intent="A", goal="B")
        with self.assertRaises(ValueError):
            # Attempting to serialize with > MAX_WORK_BYTES
            object.__setattr__(work, "intent", "X" * (MAX_WORK_BYTES + 1000))
            self.store._write_work_to_file(work)

    def test_list_for_actor_and_filters(self) -> None:
        w1 = Work(id="work_a1", intent="Task 1", goal="Goal 1", actor_id="alice", status=WorkStatus.CREATED)
        w2 = Work(id="work_a2", intent="Task 2", goal="Goal 2", actor_id="alice", status=WorkStatus.DONE)
        w3 = Work(id="work_b1", intent="Task 3", goal="Goal 3", actor_id="bob", status=WorkStatus.CREATED)

        self.store.create(w1)
        self.store.create(w2)
        self.store.create(w3)

        # Alice's all works
        alice_all = self.store.list_for_actor("alice")
        self.assertEqual(len(alice_all), 2)
        ids = {w.id for w in alice_all}
        self.assertEqual(ids, {"work_a1", "work_a2"})

        # Alice's active works
        alice_active = self.store.list_for_actor("alice", status_filter="active")
        self.assertEqual(len(alice_active), 1)
        self.assertEqual(alice_active[0].id, "work_a1")

        # Alice's done works
        alice_done = self.store.list_for_actor("alice", status_filter="done")
        self.assertEqual(len(alice_done), 1)
        self.assertEqual(alice_done[0].id, "work_a2")

        # Bob's works
        bob_all = self.store.list_for_actor("bob")
        self.assertEqual(len(bob_all), 1)
        self.assertEqual(bob_all[0].id, "work_b1")

    def test_bounded_terminal_pruning(self) -> None:
        """Verify excess terminal records are pruned deterministically."""
        # Create store with low terminal retention of 3
        store = FileWorkStore(repo_dir=self.repo_dir, max_terminal_retention=3)

        for i in range(5):
            w = Work(id=f"work_term_prune_{i}", intent=f"Task {i}", goal=f"Goal {i}", status=WorkStatus.DONE)
            store.create(w)

        terminal_files = [f for f in store.terminal_dir.glob("*.json") if not f.name.startswith(".tmp_")]
        self.assertLessEqual(len(terminal_files), 3)


if __name__ == "__main__":
    unittest.main()
