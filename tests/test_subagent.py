"""Unit tests for P1.3A Scoped Subagent Domain Model and Lifecycle.

Validates:
- Construction and mandatory bindings (parent Work, session, actor)
- Deterministic ID validation and path-traversal rejection
- Strict field validation and secret scrubbing
- Deterministic finite state machine lifecycle transitions
- Terminal state immutability
- Serialization to_dict / from_dict round-trip and extra key rejection
- Strict architectural security boundaries (no execution authority)
"""
from __future__ import annotations

import inspect
import math
import re
import unittest
from typing import Any, Dict

import core.runtime.subagent as subagent_mod
from core.runtime.subagent import (
    ACTIVE_SUBAGENT_STATUSES,
    ALLOWED_SUBAGENT_TRANSITIONS,
    CURRENT_SUBAGENT_SCHEMA_VERSION,
    InvalidSubagentTransition,
    Subagent,
    SubagentStatus,
    SubagentValidationError,
    TERMINAL_SUBAGENT_STATUSES,
    validate_subagent_id,
)


class TestSubagentConstruction(unittest.TestCase):
    """Test Subagent construction, bindings, defaults, and immutability."""

    def test_valid_construction_with_required_bindings(self) -> None:
        sub = Subagent(
            parent_work_id="work_12345",
            session_id="sess_abc",
            session_incarnation_id="inc_001",
            actor="alice",
            role="code_reviewer",
            purpose="Review pull request changes",
        )
        self.assertTrue(sub.subagent_id.startswith("sub_"))
        self.assertEqual(sub.id, sub.subagent_id)
        self.assertEqual(sub.parent_work_id, "work_12345")
        self.assertEqual(sub.session_id, "sess_abc")
        self.assertEqual(sub.session_incarnation_id, "inc_001")
        self.assertEqual(sub.actor, "alice")
        self.assertEqual(sub.actor_id, "alice")
        self.assertEqual(sub.status, SubagentStatus.CREATED)
        self.assertEqual(sub.role, "code_reviewer")
        self.assertEqual(sub.purpose, "Review pull request changes")
        self.assertEqual(sub.schema_version, CURRENT_SUBAGENT_SCHEMA_VERSION)
        self.assertIsNone(sub.delegation_id)
        self.assertIsNone(sub.failure_reason)
        self.assertIsNone(sub.metadata)
        self.assertTrue(sub.is_active)
        self.assertFalse(sub.is_terminal)
        self.assertIsInstance(sub.created_at, float)
        self.assertIsInstance(sub.updated_at, float)

    def test_custom_subagent_id(self) -> None:
        sub = Subagent(
            subagent_id="sub_custom_99",
            parent_work_id="work_12345",
            session_id="sess_abc",
            session_incarnation_id="inc_001",
            actor="alice",
        )
        self.assertEqual(sub.subagent_id, "sub_custom_99")
        self.assertEqual(sub.id, "sub_custom_99")

    def test_aliases_id_and_actor_id(self) -> None:
        sub = Subagent(
            id="sub_alias_id",
            parent_work_id="work_12345",
            session_id="sess_abc",
            session_incarnation_id="inc_001",
            actor_id="bob",
        )
        self.assertEqual(sub.subagent_id, "sub_alias_id")
        self.assertEqual(sub.actor, "bob")

    def test_role_and_purpose_normalization(self) -> None:
        # If role provided without purpose: purpose defaults to role
        sub1 = Subagent(
            parent_work_id="work_1",
            session_id="sess_1",
            session_incarnation_id="inc_1",
            actor="alice",
            role="tester",
        )
        self.assertEqual(sub1.role, "tester")
        self.assertEqual(sub1.purpose, "tester")

        # If purpose provided without role: role defaults to purpose
        sub2 = Subagent(
            parent_work_id="work_1",
            session_id="sess_1",
            session_incarnation_id="inc_1",
            actor="alice",
            purpose="run integration tests",
        )
        self.assertEqual(sub2.role, "run integration tests")
        self.assertEqual(sub2.purpose, "run integration tests")

    def test_immutable_frozen_dataclass(self) -> None:
        sub = Subagent(
            parent_work_id="work_1",
            session_id="sess_1",
            session_incarnation_id="inc_1",
            actor="alice",
        )
        with self.assertRaises((AttributeError, TypeError)):
            sub.status = SubagentStatus.READY  # type: ignore

        with self.assertRaises((AttributeError, TypeError)):
            sub.actor = "mallory"  # type: ignore


class TestSubagentValidation(unittest.TestCase):
    """Test fail-closed validation of Subagent identity, bindings, and data."""

    def test_reject_empty_or_whitespace_subagent_id(self) -> None:
        for bad_id in ("", "   ", "\t"):
            with self.assertRaises(SubagentValidationError):
                Subagent(
                    subagent_id=bad_id,
                    parent_work_id="work_1",
                    session_id="sess_1",
                    session_incarnation_id="inc_1",
                    actor="alice",
                )

    def test_reject_malformed_or_traversal_subagent_id(self) -> None:
        for bad_id in ("../evil", "..\\evil", "sub/123", "sub\\123", "sub:stream", "sub*1", "sub?2", ".hidden", "~home"):
            with self.assertRaises(SubagentValidationError):
                Subagent(
                    subagent_id=bad_id,
                    parent_work_id="work_1",
                    session_id="sess_1",
                    session_incarnation_id="inc_1",
                    actor="alice",
                )

    def test_reject_missing_or_empty_parent_work_id(self) -> None:
        for bad_parent in ("", "   ", None, 12345):
            with self.assertRaises(SubagentValidationError):
                Subagent(
                    parent_work_id=bad_parent,  # type: ignore
                    session_id="sess_1",
                    session_incarnation_id="inc_1",
                    actor="alice",
                )

    def test_reject_parent_work_id_path_traversal(self) -> None:
        for bad_parent in ("../work_evil", "/etc/passwd", "work:123"):
            with self.assertRaises(SubagentValidationError):
                Subagent(
                    parent_work_id=bad_parent,
                    session_id="sess_1",
                    session_incarnation_id="inc_1",
                    actor="alice",
                )

    def test_reject_missing_or_empty_session_id(self) -> None:
        for bad_sess in ("", "   ", None, 123):
            with self.assertRaises(SubagentValidationError):
                Subagent(
                    parent_work_id="work_1",
                    session_id=bad_sess,  # type: ignore
                    session_incarnation_id="inc_1",
                    actor="alice",
                )

    def test_reject_missing_or_empty_session_incarnation_id(self) -> None:
        for bad_inc in ("", "   ", None, 456):
            with self.assertRaises(SubagentValidationError):
                Subagent(
                    parent_work_id="work_1",
                    session_id="sess_1",
                    session_incarnation_id=bad_inc,  # type: ignore
                    actor="alice",
                )

    def test_reject_missing_or_empty_actor(self) -> None:
        for bad_actor in ("", "   ", None, 789):
            with self.assertRaises(SubagentValidationError):
                Subagent(
                    parent_work_id="work_1",
                    session_id="sess_1",
                    session_incarnation_id="inc_1",
                    actor=bad_actor,  # type: ignore
                )

    def test_reject_invalid_status(self) -> None:
        for bad_status in ("EXECUTING", "INVALID", "planning", 99, None):
            with self.assertRaises(SubagentValidationError):
                Subagent(
                    parent_work_id="work_1",
                    session_id="sess_1",
                    session_incarnation_id="inc_1",
                    actor="alice",
                    status=bad_status,  # type: ignore
                )

    def test_reject_malformed_timestamps(self) -> None:
        for bad_ts in (float("nan"), float("inf"), float("-inf"), "yesterday", [123], {}):
            with self.assertRaises(SubagentValidationError):
                Subagent(
                    parent_work_id="work_1",
                    session_id="sess_1",
                    session_incarnation_id="inc_1",
                    actor="alice",
                    created_at=bad_ts,  # type: ignore
                )
            with self.assertRaises(SubagentValidationError):
                Subagent(
                    parent_work_id="work_1",
                    session_id="sess_1",
                    session_incarnation_id="inc_1",
                    actor="alice",
                    updated_at=bad_ts,  # type: ignore
                )

    def test_reject_credentials_and_secrets(self) -> None:
        # Secret in role
        with self.assertRaises(ValueError):
            Subagent(
                parent_work_id="work_1",
                session_id="sess_1",
                session_incarnation_id="inc_1",
                actor="alice",
                role="sk-proj-" + "123456789012345678901234567890123456789012345678",
            )

        # Secret in purpose
        with self.assertRaises(ValueError):
            Subagent(
                parent_work_id="work_1",
                session_id="sess_1",
                session_incarnation_id="inc_1",
                actor="alice",
                purpose="Use token Bearer secret_token_value_9999",
            )

        # Secret in metadata
        with self.assertRaises(ValueError):
            Subagent(
                parent_work_id="work_1",
                session_id="sess_1",
                session_incarnation_id="inc_1",
                actor="alice",
                metadata={"api_key": "supersecret"},
            )

        # Secret in failure_reason
        with self.assertRaises(ValueError):
            Subagent(
                parent_work_id="work_1",
                session_id="sess_1",
                session_incarnation_id="inc_1",
                actor="alice",
                failure_reason="Failed authenticating with password: 123",
            )


class TestSubagentLifecycle(unittest.TestCase):
    """Test deterministic state machine transitions and terminal-state immutability."""

    def _make_subagent(self, status: SubagentStatus = SubagentStatus.CREATED) -> Subagent:
        return Subagent(
            subagent_id="sub_test_01",
            parent_work_id="work_01",
            session_id="sess_01",
            session_incarnation_id="inc_01",
            actor="alice",
            status=status,
            created_at=100.0,
            updated_at=100.0,
        )

    def test_valid_lifecycle_transitions(self) -> None:
        # 1. CREATED -> READY
        s0 = self._make_subagent(SubagentStatus.CREATED)
        s1 = s0.transition(SubagentStatus.READY, updated_at=110.0)
        self.assertEqual(s1.status, SubagentStatus.READY)
        self.assertEqual(s1.updated_at, 110.0)
        self.assertTrue(s1.is_active)
        self.assertFalse(s1.is_terminal)

        # 2. READY -> RUNNING
        s2 = s1.transition(SubagentStatus.RUNNING, updated_at=120.0)
        self.assertEqual(s2.status, SubagentStatus.RUNNING)
        self.assertEqual(s2.updated_at, 120.0)
        self.assertTrue(s2.is_active)
        self.assertFalse(s2.is_terminal)

        # 3. RUNNING -> COMPLETED
        s3 = s2.transition(SubagentStatus.COMPLETED, updated_at=130.0)
        self.assertEqual(s3.status, SubagentStatus.COMPLETED)
        self.assertEqual(s3.updated_at, 130.0)
        self.assertFalse(s3.is_active)
        self.assertTrue(s3.is_terminal)

    def test_valid_failure_transitions(self) -> None:
        # From CREATED -> FAILED
        sc = self._make_subagent(SubagentStatus.CREATED).fail("Init failed")
        self.assertEqual(sc.status, SubagentStatus.FAILED)
        self.assertEqual(sc.failure_reason, "Init failed")
        self.assertTrue(sc.is_terminal)

        # From READY -> FAILED
        sr = self._make_subagent(SubagentStatus.READY).fail("Resource unavailable")
        self.assertEqual(sr.status, SubagentStatus.FAILED)
        self.assertEqual(sr.failure_reason, "Resource unavailable")
        self.assertTrue(sr.is_terminal)

        # From RUNNING -> FAILED
        srun = self._make_subagent(SubagentStatus.RUNNING).fail("Crash during execution")
        self.assertEqual(srun.status, SubagentStatus.FAILED)
        self.assertEqual(srun.failure_reason, "Crash during execution")
        self.assertTrue(srun.is_terminal)

    def test_valid_cancellation_transitions(self) -> None:
        # From CREATED -> CANCELLED
        sc = self._make_subagent(SubagentStatus.CREATED).cancel("Parent work cancelled")
        self.assertEqual(sc.status, SubagentStatus.CANCELLED)
        self.assertEqual(sc.failure_reason, "Parent work cancelled")
        self.assertTrue(sc.is_terminal)

        # From READY -> CANCELLED
        sr = self._make_subagent(SubagentStatus.READY).cancel("User cancelled")
        self.assertEqual(sr.status, SubagentStatus.CANCELLED)
        self.assertTrue(sr.is_terminal)

        # From RUNNING -> CANCELLED
        srun = self._make_subagent(SubagentStatus.RUNNING).cancel("Aborted")
        self.assertEqual(srun.status, SubagentStatus.CANCELLED)
        self.assertTrue(srun.is_terminal)

    def test_invalid_lifecycle_transitions_fail_closed(self) -> None:
        s_created = self._make_subagent(SubagentStatus.CREATED)
        with self.assertRaises(InvalidSubagentTransition):
            s_created.transition(SubagentStatus.RUNNING)
        with self.assertRaises(InvalidSubagentTransition):
            s_created.transition(SubagentStatus.COMPLETED)

        s_ready = self._make_subagent(SubagentStatus.READY)
        with self.assertRaises(InvalidSubagentTransition):
            s_ready.transition(SubagentStatus.CREATED)
        with self.assertRaises(InvalidSubagentTransition):
            s_ready.transition(SubagentStatus.COMPLETED)

        s_running = self._make_subagent(SubagentStatus.RUNNING)
        with self.assertRaises(InvalidSubagentTransition):
            s_running.transition(SubagentStatus.CREATED)
        with self.assertRaises(InvalidSubagentTransition):
            s_running.transition(SubagentStatus.READY)

    def test_terminal_state_immutability(self) -> None:
        for term_status in (SubagentStatus.COMPLETED, SubagentStatus.FAILED, SubagentStatus.CANCELLED):
            sub = self._make_subagent(term_status)
            self.assertTrue(sub.is_terminal)
            self.assertFalse(sub.is_active)

            # Cannot transition terminal subagent to any status
            for target in SubagentStatus:
                with self.assertRaises(InvalidSubagentTransition):
                    sub.transition(target)

            # with_update on terminal subagent raises ValueError
            with self.assertRaises(ValueError):
                sub.with_update(role="new role")

    def test_with_update_on_active_subagent(self) -> None:
        s0 = self._make_subagent(SubagentStatus.READY)
        updated = s0.with_update(
            role="updated_role",
            purpose="updated_purpose",
            delegation_id="del_99",
            metadata={"priority": "high"},
            updated_at=150.0,
        )
        self.assertEqual(updated.status, SubagentStatus.READY)
        self.assertEqual(updated.role, "updated_role")
        self.assertEqual(updated.purpose, "updated_purpose")
        self.assertEqual(updated.delegation_id, "del_99")
        self.assertEqual(updated.metadata, {"priority": "high"})
        self.assertEqual(updated.updated_at, 150.0)


class TestSubagentSerialization(unittest.TestCase):
    """Test deterministic serialization, round-trip equality, and extra key rejection."""

    def test_round_trip_serialization_equality(self) -> None:
        original = Subagent(
            subagent_id="sub_round_trip_01",
            parent_work_id="work_p123",
            session_id="sess_s456",
            session_incarnation_id="inc_i789",
            actor="alice",
            status=SubagentStatus.RUNNING,
            role="linter",
            purpose="Run codebase linter checks",
            created_at=1700000000.0,
            updated_at=1700000050.0,
            schema_version=1,
            delegation_id="del_ref_42",
            failure_reason=None,
            metadata={"env": "testing", "max_files": 10},
        )
        serialized = original.to_dict()
        self.assertIsInstance(serialized, dict)
        self.assertEqual(serialized["subagent_id"], "sub_round_trip_01")
        self.assertEqual(serialized["status"], "running")
        self.assertEqual(serialized["delegation_id"], "del_ref_42")

        deserialized = Subagent.from_dict(serialized)
        self.assertEqual(deserialized, original)

    def test_from_dict_rejects_non_dict_input(self) -> None:
        for bad_input in ("not_a_dict", [1, 2, 3], None, 12345):
            with self.assertRaises(SubagentValidationError):
                Subagent.from_dict(bad_input)  # type: ignore

    def test_from_dict_rejects_unknown_extra_fields(self) -> None:
        data = {
            "subagent_id": "sub_valid_1",
            "parent_work_id": "work_1",
            "session_id": "sess_1",
            "session_incarnation_id": "inc_1",
            "actor": "alice",
            "status": "created",
            "created_at": 100.0,
            "updated_at": 100.0,
            "contract": {"type": "ApprovedExecutionContract"},  # INJECTED AUTHORITY KEY
        }
        with self.assertRaises(SubagentValidationError):
            Subagent.from_dict(data)

    def test_from_dict_rejects_missing_required_fields(self) -> None:
        valid_dict = {
            "subagent_id": "sub_1",
            "parent_work_id": "work_1",
            "session_id": "sess_1",
            "session_incarnation_id": "inc_1",
            "actor": "alice",
            "status": "created",
            "created_at": 100.0,
            "updated_at": 100.0,
        }
        for req in ("parent_work_id", "session_id", "session_incarnation_id", "status", "created_at", "updated_at"):
            corrupted = dict(valid_dict)
            del corrupted[req]
            with self.assertRaises(SubagentValidationError):
                Subagent.from_dict(corrupted)

        # Missing actor
        corrupted = dict(valid_dict)
        del corrupted["actor"]
        with self.assertRaises(SubagentValidationError):
            Subagent.from_dict(corrupted)

        # Explicit None in required fields
        for req in ("parent_work_id", "session_id", "session_incarnation_id", "status", "created_at", "updated_at", "actor"):
            corrupted = dict(valid_dict)
            corrupted[req] = None
            with self.assertRaises(SubagentValidationError):
                Subagent.from_dict(corrupted)

    def test_from_dict_rejects_secrets(self) -> None:
        data = {
            "subagent_id": "sub_1",
            "parent_work_id": "work_1",
            "session_id": "sess_1",
            "session_incarnation_id": "inc_1",
            "actor": "alice",
            "status": "created",
            "created_at": 100.0,
            "updated_at": 100.0,
            "role": "sk-proj-" + "supersecretkey12345678901234567890",
        }
        with self.assertRaises(ValueError):
            Subagent.from_dict(data)


class TestSubagentSecurityBoundaries(unittest.TestCase):
    """Prove that Subagent holds NO execution authority and contains no execution engines."""

    def test_subagent_module_has_no_execution_imports(self) -> None:
        src = inspect.getsource(subagent_mod)

        # Disallowed execution primitives
        prohibited_terms = [
            "orchestrator",
            "Orchestrator",
            "subprocess",
            "os.system",
            "os.popen",
            "shutil",
            "ApprovalService",
            "ApprovalStore",
            "TransactionCoordinator",
            "ApprovedExecutionContract",
            "ExecutionContract",
        ]
        for term in prohibited_terms:
            pattern = rf"\b{re.escape(term)}\b"
            matches = [line for line in src.splitlines() if re.search(pattern, line) and not line.strip().startswith(("#", '"""', "'''", "*", "-"))]
            self.assertEqual(matches, [], f"Prohibited authority reference '{term}' found in subagent.py: {matches}")

    def test_subagent_class_has_no_execution_methods(self) -> None:
        for m in dir(Subagent):
            if m.startswith("_"):
                continue
            # Must not have execution or mutation methods
            for forbidden in ("run", "exec", "execute", "mutate", "apply", "shell", "cmd", "fork", "spawn"):
                self.assertNotIn(
                    forbidden,
                    m.lower(),
                    f"Subagent class exposes suspicious execution method '{m}'",
                )

    def test_subagent_dataclass_has_no_authority_attributes(self) -> None:
        sub = Subagent(
            parent_work_id="work_1",
            session_id="sess_1",
            session_incarnation_id="inc_1",
            actor="alice",
        )
        sub_dict = sub.to_dict()
        # Verify absence of authority tokens
        self.assertNotIn("contract", sub_dict)
        self.assertNotIn("token", sub_dict)
        self.assertNotIn("permissions", sub_dict)
        self.assertNotIn("approved_contract", sub_dict)
        self.assertNotIn("capability", sub_dict)


if __name__ == "__main__":
    unittest.main()
