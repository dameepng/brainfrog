"""Test suite for BrainFrog Phase 2A — Plan Registry & Lifecycle Foundation.

Covers:
1. Stable ID generation, uniqueness, and format.
2. Workspace isolation: strict containment within .brainfrog/plans/ and cross-workspace non-leakage.
3. Listing and retrieving plans (lightweight listing without markdown body).
4. Missing, malformed, or orphaned metadata sidecars.
5. Legacy plan compatibility (plain markdown, unversioned metadata).
6. Lifecycle transitions state machine (allowed vs illegal transitions).
7. Transition history and evidence preservation (verification history, failure history).
8. Verification gate requires evidence (never inferred from code-writing or zero exit code).
9. Optimistic concurrency control (OCC) and atomic persistence.
10. Deterministic plan selection contract (exact ID, title match, single eligible candidate, ambiguous candidates, no candidates, no guessing).
11. Selection invariants: selection never executes, mutates files, or mints execution contracts.
12. Runtime slash commands (/plans, /plan select, /plan show, /plan status, /plan clear).
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import MagicMock
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.plans import (
    ALLOWED_PLAN_TRANSITIONS,
    AmbiguousPlanError,
    InvalidPlanMetadataError,
    InvalidPlanTransitionError,
    PlanDocument,
    PlanNotFoundError,
    PlanRegistry,
    PlanSelectionResult,
    PlanStatus,
    PlanSummary,
    StalePlanStateError,
    format_plan_handoff,
    get_latest_plan,
    get_plans_dir,
    save_plan_document,
    validate_plan_transition,
)
from core.runtime.messages import IncomingMessage, OutgoingMessage
from core.runtime.runtime import BrainFrogRuntime
from core.runtime.session import SessionManager, SessionState


def _synthetic_test_token(name: str = "KEY") -> str:
    """Obviously fake synthetic token for test safety compliance."""
    return f"synthetic_test_{name}_placeholder_value_12345"


class TestPlanLifecycleStateMachine(unittest.TestCase):
    """Tests covering the lifecycle states, transitions, and evidence validation."""

    def test_canonical_lifecycle_states(self) -> None:
        expected_states = {
            "draft", "ready", "in_progress", "blocked",
            "implemented", "verified", "failed", "superseded",
        }
        actual_states = {s.value for s in PlanStatus}
        self.assertEqual(actual_states, expected_states)

    def test_allowed_transitions_table_integrity(self) -> None:
        # DRAFT
        self.assertEqual(
            ALLOWED_PLAN_TRANSITIONS[PlanStatus.DRAFT],
            {PlanStatus.READY, PlanStatus.SUPERSEDED, PlanStatus.FAILED},
        )
        # READY
        self.assertEqual(
            ALLOWED_PLAN_TRANSITIONS[PlanStatus.READY],
            {PlanStatus.IN_PROGRESS, PlanStatus.BLOCKED, PlanStatus.DRAFT, PlanStatus.SUPERSEDED},
        )
        # IN_PROGRESS
        self.assertEqual(
            ALLOWED_PLAN_TRANSITIONS[PlanStatus.IN_PROGRESS],
            {PlanStatus.IMPLEMENTED, PlanStatus.BLOCKED, PlanStatus.FAILED, PlanStatus.SUPERSEDED},
        )
        # BLOCKED
        self.assertEqual(
            ALLOWED_PLAN_TRANSITIONS[PlanStatus.BLOCKED],
            {PlanStatus.READY, PlanStatus.IN_PROGRESS, PlanStatus.FAILED, PlanStatus.SUPERSEDED},
        )
        # IMPLEMENTED
        self.assertEqual(
            ALLOWED_PLAN_TRANSITIONS[PlanStatus.IMPLEMENTED],
            {PlanStatus.VERIFIED, PlanStatus.FAILED, PlanStatus.BLOCKED, PlanStatus.IN_PROGRESS},
        )
        # FAILED
        self.assertEqual(
            ALLOWED_PLAN_TRANSITIONS[PlanStatus.FAILED],
            {PlanStatus.READY, PlanStatus.DRAFT, PlanStatus.SUPERSEDED},
        )
        # VERIFIED
        self.assertEqual(
            ALLOWED_PLAN_TRANSITIONS[PlanStatus.VERIFIED],
            {PlanStatus.SUPERSEDED},
        )
        # SUPERSEDED (terminal state)
        self.assertEqual(
            ALLOWED_PLAN_TRANSITIONS[PlanStatus.SUPERSEDED],
            set(),
        )

    def test_reject_illegal_transitions(self) -> None:
        # draft cannot jump directly to in_progress, implemented, or verified
        with self.assertRaises(InvalidPlanTransitionError):
            validate_plan_transition(PlanStatus.DRAFT, PlanStatus.IN_PROGRESS, actor="user", reason="test")

        with self.assertRaises(InvalidPlanTransitionError):
            validate_plan_transition(PlanStatus.DRAFT, PlanStatus.IMPLEMENTED, actor="user", reason="test")

        # superseded cannot transition to draft or any other state (terminal)
        with self.assertRaises(InvalidPlanTransitionError):
            validate_plan_transition(PlanStatus.SUPERSEDED, PlanStatus.DRAFT, actor="user", reason="test")

        with self.assertRaises(InvalidPlanTransitionError):
            validate_plan_transition(PlanStatus.DRAFT, PlanStatus.VERIFIED, actor="user", reason="test")

        # ready cannot jump directly to implemented or verified
        with self.assertRaises(InvalidPlanTransitionError):
            validate_plan_transition(PlanStatus.READY, PlanStatus.IMPLEMENTED, actor="user", reason="test")

        with self.assertRaises(InvalidPlanTransitionError):
            validate_plan_transition(PlanStatus.READY, PlanStatus.VERIFIED, actor="user", reason="test")

        # in_progress cannot jump directly to verified (must go through implemented)
        with self.assertRaises(InvalidPlanTransitionError):
            validate_plan_transition(PlanStatus.IN_PROGRESS, PlanStatus.VERIFIED, actor="user", reason="test")

        # verified cannot transition back to ready or in_progress
        with self.assertRaises(InvalidPlanTransitionError):
            validate_plan_transition(PlanStatus.VERIFIED, PlanStatus.READY, actor="user", reason="test")

        with self.assertRaises(InvalidPlanTransitionError):
            validate_plan_transition(PlanStatus.VERIFIED, PlanStatus.IN_PROGRESS, actor="user", reason="test")

    def test_transition_actor_and_reason_mandatory(self) -> None:
        with self.assertRaises(InvalidPlanTransitionError):
            validate_plan_transition(PlanStatus.DRAFT, PlanStatus.READY, actor="", reason="reviewed")

        with self.assertRaises(InvalidPlanTransitionError):
            validate_plan_transition(PlanStatus.DRAFT, PlanStatus.READY, actor="user", reason="   ")

    def test_draft_to_ready_requires_goal_and_steps(self) -> None:
        empty_plan = PlanDocument(
            filename="test.md",
            path=Path("/tmp/test.md"),
            title="Empty Plan",
            goal="",
            steps=[],
        )
        with self.assertRaises(InvalidPlanTransitionError):
            validate_plan_transition(PlanStatus.DRAFT, PlanStatus.READY, actor="user", reason="test", plan=empty_plan)

        plan_no_steps = PlanDocument(
            filename="test.md",
            path=Path("/tmp/test.md"),
            title="Plan with goal",
            goal="Refactor auth",
            steps=[],
        )
        with self.assertRaises(InvalidPlanTransitionError):
            validate_plan_transition(PlanStatus.DRAFT, PlanStatus.READY, actor="user", reason="test", plan=plan_no_steps)

    def test_verified_strictly_requires_verification_evidence(self) -> None:
        # Missing evidence entirely
        with self.assertRaises(InvalidPlanTransitionError):
            validate_plan_transition(
                PlanStatus.IMPLEMENTED,
                PlanStatus.VERIFIED,
                actor="test_runner",
                reason="all done",
                evidence=None,
            )

        # Empty evidence dictionary
        with self.assertRaises(InvalidPlanTransitionError):
            validate_plan_transition(
                PlanStatus.IMPLEMENTED,
                PlanStatus.VERIFIED,
                actor="test_runner",
                reason="all done",
                evidence={},
            )

        # Evidence without checks
        with self.assertRaises(InvalidPlanTransitionError):
            validate_plan_transition(
                PlanStatus.IMPLEMENTED,
                PlanStatus.VERIFIED,
                actor="test_runner",
                reason="all done",
                evidence={"checks": []},
            )

        # Valid evidence passes
        valid_evidence = {
            "checks": ["pytest tests/test_auth.py", "git diff --check"],
            "summary": "24 passed, 0 failed",
        }
        # Should not raise
        validate_plan_transition(
            PlanStatus.IMPLEMENTED,
            PlanStatus.VERIFIED,
            actor="test_runner",
            reason="All 24 unit and regression checks passed cleanly",
            evidence=valid_evidence,
        )


class TestPlanRegistryOperations(unittest.TestCase):
    """Tests covering PlanRegistry discovery, listing, atomic updates, and recovery."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="brainfrog_test_registry_")
        self.repo_dir = Path(self.test_dir).resolve()
        self.registry = PlanRegistry(self.repo_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_save_and_retrieve_plan_document(self) -> None:
        content = "# Feature Plan: Fast Auth\n\n- **Goal**: Implement fast auth\n\n1. Add token verification\n"
        meta = {
            "title": "Fast Auth",
            "goal": "Implement fast auth",
            "acceptance_criteria": ["Tokens validate within 5ms"],
            "steps": [{"id": "1", "description": "Add token verification", "files": ["auth.py"]}],
        }
        saved = save_plan_document(self.repo_dir, "20261018_fast_auth.md", content, metadata=meta)
        self.assertTrue(saved.exists())

        # Retrieve by stable ID
        plan_id = "plan_20261018_fast_auth"
        retrieved = self.registry.get_plan(plan_id)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.id, plan_id)
        self.assertEqual(retrieved.title, "Fast Auth")
        self.assertEqual(retrieved.status, PlanStatus.DRAFT)
        self.assertEqual(retrieved.revision, 1)
        self.assertEqual(len(retrieved.transitions), 1)

    def test_list_plans_returns_summaries_without_full_content(self) -> None:
        save_plan_document(
            self.repo_dir,
            "20261018_plan_a.md",
            "# Plan A\n" + ("Long description " * 1000),
            metadata={"title": "Plan A", "goal": "Goal A", "steps": [{"id": "1"}]},
        )
        save_plan_document(
            self.repo_dir,
            "20261018_plan_b.md",
            "# Plan B\n" + ("Long description " * 1000),
            metadata={"title": "Plan B", "goal": "Goal B", "steps": [{"id": "1"}, {"id": "2"}]},
        )

        summaries = self.registry.list_plans()
        self.assertEqual(len(summaries), 2)
        for s in summaries:
            self.assertIsInstance(s, PlanSummary)
            self.assertIn(s.id, ("plan_20261018_plan_a", "plan_20261018_plan_b"))
            self.assertFalse(hasattr(s, "content"))  # Content not in summary

    def test_legacy_plan_without_metadata_reconciles_safely(self) -> None:
        # Create a bare markdown file without companion .meta.json
        plans_dir = get_plans_dir(self.repo_dir)
        plans_dir.mkdir(parents=True, exist_ok=True)
        legacy_file = plans_dir / "legacy_migration.md"
        legacy_file.write_text("# Legacy Migration\n\n- **Goal**: Migrate schema\n\n1. Run migration\n", encoding="utf-8")

        plan = self.registry.get_plan("plan_legacy_migration")
        self.assertIsNotNone(plan)
        self.assertEqual(plan.title, "Legacy Migration")
        self.assertEqual(plan.goal, "Migrate schema")
        self.assertEqual(plan.status, PlanStatus.DRAFT)
        self.assertTrue(plan.metadata_missing)

        # Missing metadata plan cannot transition to ready without proper metadata
        with self.assertRaises(InvalidPlanTransitionError):
            self.registry.update_status(plan.id, PlanStatus.READY, actor="user", reason="Ready to go")

    def test_malformed_metadata_sidecar_fails_closed(self) -> None:
        plans_dir = get_plans_dir(self.repo_dir)
        plans_dir.mkdir(parents=True, exist_ok=True)
        md_file = plans_dir / "corrupted_plan.md"
        md_file.write_text("# Corrupted Plan\n\nGoal: Some goal\n", encoding="utf-8")
        meta_file = plans_dir / "corrupted_plan.meta.json"
        meta_file.write_text("{invalid json corrupt ...", encoding="utf-8")

        plan = self.registry.get_plan("plan_corrupted_plan")
        self.assertIsNotNone(plan)
        self.assertEqual(plan.status, PlanStatus.DRAFT)
        self.assertTrue(plan.metadata_malformed)

        # Malformed metadata cannot be made ready
        with self.assertRaises(InvalidPlanTransitionError):
            self.registry.update_status(plan.id, PlanStatus.READY, actor="user", reason="Approve")

    def test_unknown_status_in_metadata_fails_closed(self) -> None:
        plans_dir = get_plans_dir(self.repo_dir)
        plans_dir.mkdir(parents=True, exist_ok=True)
        md_file = plans_dir / "bad_status.md"
        md_file.write_text("# Bad Status Plan\n", encoding="utf-8")
        meta_file = plans_dir / "bad_status.meta.json"
        meta_file.write_text(json.dumps({
            "id": "plan_bad_status",
            "title": "Bad Status Plan",
            "status": "magically_executed",  # Unknown status
        }), encoding="utf-8")

        plan = self.registry.get_plan("plan_bad_status")
        self.assertIsNotNone(plan)
        # Never defaults to ready or execution-eligible!
        self.assertEqual(plan.status, PlanStatus.DRAFT)
        self.assertTrue(plan.metadata_malformed)

    def test_lifecycle_transitions_and_history_preservation(self) -> None:
        content = "# Lifecycle Test\n\n- **Goal**: Full lifecycle test\n"
        meta = {
            "title": "Lifecycle Test",
            "goal": "Full lifecycle test",
            "acceptance_criteria": ["Criteria 1"],
            "steps": [{"id": "1", "description": "Step 1"}],
        }
        save_plan_document(self.repo_dir, "20261018_lifecycle.md", content, metadata=meta)
        plan_id = "plan_20261018_lifecycle"

        # 1. draft -> ready
        p1 = self.registry.update_status(plan_id, PlanStatus.READY, actor="lead_dev", reason="Reviewed and signed off")
        self.assertEqual(p1.status, PlanStatus.READY)
        self.assertEqual(p1.revision, 2)
        self.assertEqual(len(p1.transitions), 2)

        # 2. ready -> in_progress (requires ApprovedExecutionContract)
        mock_contract = MagicMock()
        mock_contract.validate_plan_binding.return_value = True
        p2 = self.registry.update_status(
            plan_id,
            PlanStatus.IN_PROGRESS,
            actor="orchestrator",
            reason="Execution authorized",
            contract=mock_contract,
        )
        self.assertEqual(p2.status, PlanStatus.IN_PROGRESS)
        self.assertEqual(p2.revision, 3)

        # 3. in_progress -> implemented
        p3 = self.registry.update_status(plan_id, PlanStatus.IMPLEMENTED, actor="orchestrator", reason="Code edits committed")
        self.assertEqual(p3.status, PlanStatus.IMPLEMENTED)
        self.assertEqual(p3.revision, 4)

        # 4. implemented -> verified (requires evidence)
        evidence = {
            "checks": [
                {
                    "check_name": "pytest tests/test_core.py",
                    "command": "pytest tests/test_core.py",
                    "criterion_ref": "Criteria 1",
                    "status": "passed",
                }
            ],
            "summary": "All checks passed cleanly",
        }
        p4 = self.registry.update_status(plan_id, PlanStatus.VERIFIED, actor="verifier", reason="Quality gate pass", evidence=evidence)
        self.assertEqual(p4.status, PlanStatus.VERIFIED)
        self.assertEqual(p4.revision, 5)
        self.assertEqual(len(p4.verification_history), 1)
        self.assertEqual(p4.verification_history[0]["evidence"]["summary"], "All checks passed cleanly")

        # 5. verified -> superseded
        p5 = self.registry.update_status(plan_id, PlanStatus.SUPERSEDED, actor="lead_dev", reason="Replaced by Phase 2B plan")
        self.assertEqual(p5.status, PlanStatus.SUPERSEDED)
        self.assertEqual(p5.revision, 6)

        # History is fully preserved on disk
        reloaded = self.registry.get_plan(plan_id)
        self.assertEqual(len(reloaded.transitions), 6)
        self.assertEqual(len(reloaded.verification_history), 1)

    def test_failure_history_preservation(self) -> None:
        content = "# Failure Plan\n\n- **Goal**: Test failure recording\n"
        meta = {
            "title": "Failure Plan",
            "goal": "Test failure recording",
            "steps": [{"id": "1", "description": "Step 1"}],
        }
        save_plan_document(self.repo_dir, "20261018_failure.md", content, metadata=meta)
        plan_id = "plan_20261018_failure"

        self.registry.update_status(plan_id, PlanStatus.READY, actor="user", reason="Ready")
        mock_contract = MagicMock()
        mock_contract.validate_plan_binding.return_value = True
        self.registry.update_status(
            plan_id,
            PlanStatus.IN_PROGRESS,
            actor="runtime",
            reason="Starting",
            contract=mock_contract,
        )
        p_failed = self.registry.update_status(
            plan_id,
            PlanStatus.FAILED,
            actor="orchestrator",
            reason="Unit test regression detected in test_auth",
            evidence={"exit_code": 1, "test": "test_auth"},
        )
        self.assertEqual(p_failed.status, PlanStatus.FAILED)
        self.assertEqual(len(p_failed.failure_history), 1)
        self.assertEqual(p_failed.failure_history[0]["reason"], "Unit test regression detected in test_auth")

        # Failed plan can be reworked back to DRAFT or re-reviewed to READY
        p_reworked = self.registry.update_status(plan_id, PlanStatus.DRAFT, actor="user", reason="Reworking plan approach")
        self.assertEqual(p_reworked.status, PlanStatus.DRAFT)
        # Failure history remains preserved
        self.assertEqual(len(p_reworked.failure_history), 1)

    def test_optimistic_concurrency_control_rejects_stale_revision(self) -> None:
        save_plan_document(
            self.repo_dir,
            "20261018_occ.md",
            "# OCC Plan\n\nGoal: OCC\n",
            metadata={"title": "OCC Plan", "goal": "OCC", "steps": [{"id": "1"}]},
        )
        plan_id = "plan_20261018_occ"

        # First update advances revision from 1 to 2
        self.registry.update_status(plan_id, PlanStatus.READY, actor="actor_1", reason="Ready")

        # Second update attempting with stale expected_revision=1 must fail
        with self.assertRaises(StalePlanStateError):
            self.registry.update_status(
                plan_id,
                PlanStatus.IN_PROGRESS,
                actor="actor_2",
                reason="Starting",
                expected_revision=1,  # Stale! Disk is revision 2
            )

    def test_workspace_isolation_and_path_traversal(self) -> None:
        # Create second workspace
        second_dir = tempfile.mkdtemp(prefix="brainfrog_ws2_")
        ws2_path = Path(second_dir).resolve()
        reg2 = PlanRegistry(ws2_path)
        try:
            save_plan_document(
                self.repo_dir,
                "plan_ws1.md",
                "# Workspace 1 Plan\n",
                metadata={"title": "WS1 Plan", "goal": "WS1"},
            )
            save_plan_document(
                ws2_path,
                "plan_ws2.md",
                "# Workspace 2 Plan\n",
                metadata={"title": "WS2 Plan", "goal": "WS2"},
            )

            # WS1 registry sees only WS1 plan
            self.assertIsNotNone(self.registry.get_plan("plan_plan_ws1"))
            self.assertIsNone(self.registry.get_plan("plan_plan_ws2"))

            # WS2 registry sees only WS2 plan
            self.assertIsNotNone(reg2.get_plan("plan_plan_ws2"))
            self.assertIsNone(reg2.get_plan("plan_plan_ws1"))

            # Traversal attempts fail closed
            with self.assertRaises(PermissionError):
                self.registry.get_plan("../../secret_file")
            with self.assertRaises(PermissionError):
                self.registry.get_plan_by_filename("../outside.md")
        finally:
            shutil.rmtree(second_dir, ignore_errors=True)


class TestPlanSelectionContract(unittest.TestCase):
    """Tests covering deterministic plan selection rules and boundaries."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="brainfrog_test_selection_")
        self.repo_dir = Path(self.test_dir).resolve()
        self.registry = PlanRegistry(self.repo_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_select_by_explicit_valid_id(self) -> None:
        save_plan_document(
            self.repo_dir,
            "20261018_my_feature.md",
            "# My Feature\n\nGoal: Build feature\n",
            metadata={"title": "My Feature", "goal": "Build feature"},
        )
        res = self.registry.select_plan(query="plan_20261018_my_feature")
        self.assertEqual(res.status, "selected")
        self.assertIsNotNone(res.selected)
        self.assertEqual(res.selected.id, "plan_20261018_my_feature")

    def test_select_by_unambiguous_exact_title(self) -> None:
        save_plan_document(
            self.repo_dir,
            "20261018_db.md",
            "# Postgres Optimization\n\nGoal: Tune queries\n",
            metadata={"title": "Postgres Optimization", "goal": "Tune queries"},
        )
        res = self.registry.select_plan(query="Postgres Optimization")
        self.assertEqual(res.status, "selected")
        self.assertIsNotNone(res.selected)
        self.assertEqual(res.selected.title, "Postgres Optimization")

    def test_ambiguous_title_matches_return_candidates(self) -> None:
        save_plan_document(
            self.repo_dir,
            "20261018_plan1.md",
            "# Authentication Upgrade\n\nGoal: OAuth\n",
            metadata={"title": "Authentication Upgrade", "goal": "OAuth"},
        )
        save_plan_document(
            self.repo_dir,
            "20261018_plan2.md",
            "# Authentication Upgrade\n\nGoal: Passkeys\n",
            metadata={"title": "Authentication Upgrade", "goal": "Passkeys"},
        )
        res = self.registry.select_plan(query="Authentication Upgrade")
        self.assertEqual(res.status, "ambiguous")
        self.assertIsNone(res.selected)
        self.assertEqual(len(res.candidates), 2)

    def test_single_eligible_candidate_with_candidate_intent(self) -> None:
        save_plan_document(
            self.repo_dir,
            "20261018_active.md",
            "# Active Plan\n\nGoal: Active goal\n",
            metadata={"title": "Active Plan", "goal": "Active goal", "steps": [{"id": "1"}]},
        )
        # Mark as READY
        self.registry.update_status("plan_20261018_active", PlanStatus.READY, actor="user", reason="Ready")

        # Without candidate_intent -> does not guess
        res_no_intent = self.registry.select_plan(query="", candidate_intent=False)
        self.assertEqual(res_no_intent.status, "none_found")
        self.assertIsNone(res_no_intent.selected)

        # With candidate_intent -> selects single eligible candidate
        res_with_intent = self.registry.select_plan(query="", candidate_intent=True)
        self.assertEqual(res_with_intent.status, "selected")
        self.assertIsNotNone(res_with_intent.selected)
        self.assertEqual(res_with_intent.selected.id, "plan_20261018_active")

    def test_multiple_eligible_candidates_with_intent_returns_candidates(self) -> None:
        save_plan_document(
            self.repo_dir,
            "20261018_p1.md",
            "# Plan One\n\nGoal: Goal 1\n",
            metadata={"title": "Plan One", "goal": "Goal 1", "steps": [{"id": "1"}]},
        )
        save_plan_document(
            self.repo_dir,
            "20261018_p2.md",
            "# Plan Two\n\nGoal: Goal 2\n",
            metadata={"title": "Plan Two", "goal": "Goal 2", "steps": [{"id": "1"}]},
        )
        self.registry.update_status("plan_20261018_p1", PlanStatus.READY, actor="user", reason="Ready")
        self.registry.update_status("plan_20261018_p2", PlanStatus.READY, actor="user", reason="Ready")

        res = self.registry.select_plan(query="", candidate_intent=True)
        self.assertEqual(res.status, "ambiguous")
        self.assertIsNone(res.selected)
        self.assertEqual(len(res.candidates), 2)

    def test_no_eligible_candidates_returns_none_found_with_guidance(self) -> None:
        # Only DRAFT plans exist
        save_plan_document(
            self.repo_dir,
            "20261018_draft.md",
            "# Draft Only\n\nGoal: Draft\n",
            metadata={"title": "Draft Only", "goal": "Draft"},
        )
        res = self.registry.select_plan(query="", candidate_intent=True)
        self.assertEqual(res.status, "none_found")
        self.assertIn("/mode plan", res.message)

    def test_unknown_or_malformed_id_fails_closed_without_guessing(self) -> None:
        save_plan_document(
            self.repo_dir,
            "20261018_latest.md",
            "# Latest Plan\n\nGoal: Latest\n",
            metadata={"title": "Latest Plan", "goal": "Latest"},
        )
        # Searching for nonexistent ID does NOT fall back to latest plan
        res = self.registry.select_plan(query="nonexistent_plan_id")
        self.assertEqual(res.status, "none_found")
        self.assertIsNone(res.selected)

    def test_stale_session_selection_is_not_trusted(self) -> None:
        # Session references a plan that does not exist in workspace
        res = self.registry.select_plan(session_selected_id="deleted_or_foreign_plan_123")
        self.assertEqual(res.status, "none_found")
        self.assertIsNone(res.selected)


class TestRuntimePlanCommands(unittest.TestCase):
    """Tests covering /plans and /plan slash command handling in BrainFrogRuntime."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="brainfrog_test_runtime_plan_")
        self.repo_dir = Path(self.test_dir).resolve()
        self.runtime = BrainFrogRuntime(repo_dir=self.repo_dir, persist_sessions=False)

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_plans_list_empty_workspace(self) -> None:
        msg = IncomingMessage(text="/plans", channel="cli", user_id="local", conversation_id="conv1")
        resp = self.runtime.handle_message(msg)
        self.assertTrue(resp.success)
        self.assertIn("No plans found in workspace", resp.text)

    def test_plans_list_with_populated_workspace(self) -> None:
        save_plan_document(
            self.repo_dir,
            "20261018_api_v2.md",
            "# API v2\n\nGoal: Upgrade API\n",
            metadata={"title": "API v2", "goal": "Upgrade API", "steps": [{"id": "1"}]},
        )
        msg = IncomingMessage(text="/plans", channel="cli", user_id="local", conversation_id="conv1")
        resp = self.runtime.handle_message(msg)
        self.assertTrue(resp.success)
        self.assertIn("Implementation Plans", resp.text)
        self.assertIn("API v2", resp.text)
        self.assertIn("plan_20261018_api_v2", resp.text)

    def test_plan_select_stores_session_selection(self) -> None:
        save_plan_document(
            self.repo_dir,
            "20261018_cache.md",
            "# Redis Cache\n\nGoal: Cache layer\n",
            metadata={"title": "Redis Cache", "goal": "Cache layer"},
        )
        msg = IncomingMessage(text="/plan select plan_20261018_cache", channel="cli", user_id="local", conversation_id="conv1")
        resp = self.runtime.handle_message(msg)
        self.assertTrue(resp.success)
        self.assertIn("Plan selected", resp.text)

        # Verify session state persistence
        sess = self.runtime.sessions.get_or_create(channel="cli", user_id="local", conversation_id="conv1")
        self.assertEqual(sess.selected_plan_id, "plan_20261018_cache")

        # Verify /plan current shows selected plan
        msg_cur = IncomingMessage(text="/plan", channel="cli", user_id="local", conversation_id="conv1")
        resp_cur = self.runtime.handle_message(msg_cur)
        self.assertTrue(resp_cur.success)
        self.assertIn("Currently Selected Plan", resp_cur.text)
        self.assertIn("Redis Cache", resp_cur.text)

    def test_plan_selection_invariants_no_execution_or_contract(self) -> None:
        save_plan_document(
            self.repo_dir,
            "20261018_security.md",
            "# Security Hardening\n\nGoal: Harden boundaries\n",
            metadata={"title": "Security Hardening", "goal": "Harden boundaries"},
        )
        # Select plan
        msg = IncomingMessage(text="/plan select plan_20261018_security", channel="cli", user_id="local", conversation_id="conv1")
        resp = self.runtime.handle_message(msg)
        self.assertTrue(resp.success)

        # Plan selection MUST NOT create approval requests or mint execution contracts
        approvals = self.runtime.approval_service.store.list_requests()
        self.assertEqual(len(approvals), 0)

        # Plan selection MUST NOT modify any source files
        source_files = list(self.repo_dir.glob("*.py"))
        self.assertEqual(len(source_files), 0)

        # Plan status MUST remain DRAFT (selection does not mark in_progress)
        reg = PlanRegistry(self.repo_dir)
        plan = reg.get_plan("plan_20261018_security")
        self.assertEqual(plan.status, PlanStatus.DRAFT)

    def test_plan_status_command_transitions_lifecycle(self) -> None:
        save_plan_document(
            self.repo_dir,
            "20261018_task.md",
            "# Task A\n\nGoal: Goal A\n",
            metadata={"title": "Task A", "goal": "Goal A", "steps": [{"id": "1", "description": "Step 1"}]},
        )
        # Valid transition: draft -> ready
        msg = IncomingMessage(
            text="/plan status plan_20261018_task ready Reviewed by team lead",
            channel="cli",
            user_id="local",
            conversation_id="conv1",
        )
        resp = self.runtime.handle_message(msg)
        self.assertTrue(resp.success)
        self.assertIn("transitioned to `ready`", resp.text)

        # Invalid transition: ready -> verified (skips execution & verification evidence)
        msg_bad = IncomingMessage(
            text="/plan status plan_20261018_task verified Premature verification",
            channel="cli",
            user_id="local",
            conversation_id="conv1",
        )
        resp_bad = self.runtime.handle_message(msg_bad)
        self.assertFalse(resp_bad.success)
        self.assertIn("Status transition rejected", resp_bad.text)

    def test_plan_clear_selection(self) -> None:
        save_plan_document(
            self.repo_dir,
            "20261018_temp.md",
            "# Temp Plan\n\nGoal: Temp\n",
            metadata={"title": "Temp Plan", "goal": "Temp"},
        )
        self.runtime.handle_message(
            IncomingMessage(text="/plan select plan_20261018_temp", channel="cli", user_id="local", conversation_id="conv1")
        )
        sess = self.runtime.sessions.get_or_create(channel="cli", user_id="local", conversation_id="conv1")
        self.assertEqual(sess.selected_plan_id, "plan_20261018_temp")

        # Clear selection
        resp_clear = self.runtime.handle_message(
            IncomingMessage(text="/plan clear", channel="cli", user_id="local", conversation_id="conv1")
        )
        self.assertTrue(resp_clear.success)
        self.assertIsNone(sess.selected_plan_id)


if __name__ == "__main__":
    unittest.main()
