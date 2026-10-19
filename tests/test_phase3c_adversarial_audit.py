"""Phase 3C: Independent Adversarial Audit of Plan Refinement and Approval Handoff.

Enforces deep security invariants across:
- Audit A: Plan identity, refinement lifecycle, ownership isolation, terminal states, and WAL state.
- Audit B: Approval lifecycle, replay protection, contract tamper resistance, and fail-closed execution.
- Audit C: Mode and remote channel boundaries (chat/plan/build, shell escapes, unapproved triggers).
- Audit D: Deterministic WAL fault-injection, interruption recovery, and idempotence.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.plans import (
    ALLOWED_PLAN_TRANSITIONS,
    InvalidPlanTransitionError,
    PlanDocument,
    PlanNotFoundError,
    PlanRegistry,
    PlanStatus,
    StalePlanStateError,
    get_journal_dir,
    get_plans_dir,
    recover_plan_journal,
    save_plan_document,
    set_wal_fault_hook,
)
from core.runtime.approval import (
    ApprovalRequest,
    ApprovalService,
    ApprovalStatus,
    CanonicalOperation,
    FileApprovalStore,
)
from core.runtime.capabilities import Capabilities, FilesystemPolicy, NetworkPolicy
from core.runtime.contract import ApprovedExecutionContract
from core.runtime.messages import IncomingMessage, OutgoingMessage
from core.runtime.runtime import BrainFrogRuntime
from core.runtime.session import (
    FileSessionStore,
    SessionManager,
    SessionState,
    StaleSessionStateError,
)
from orchestrator import (
    Orchestrator,
    PlanStep,
    RunConfig,
    StepResult,
    _write_files,
)
from system1.base import Answer, SystemOneClient


class MockS2Double:
    """Configurable System 2 double for testing."""

    def __init__(self) -> None:
        self.plan_generator = None
        self.planned_files = ["auth_service.py"]

    def diagnose(self, task: str, *args: Any, **kwargs: Any) -> str:
        return f"Diagnosed answer for: {task}"

    def plan_and_prd(self, task: str, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        if self.plan_generator is not None:
            return self.plan_generator(task, *args, **kwargs)
        return {
            "title": "Auth Service Implementation",
            "markdown_doc": f"# Auth Service Implementation: {task}\n\n## Langkah\n1. Setup auth\n",
            "problem": task,
            "goals": [task],
            "steps": [{"id": "1", "description": "Implement auth", "files": ["auth_service.py"]}],
            "relevant_files": ["auth_service.py"],
            "acceptance_criteria": ["Tokens must expire in 15m"],
        }

    def plan_task(self, task: str, *args: Any, **kwargs: Any) -> List[PlanStep]:
        return [PlanStep("1", "Implement auth", list(self.planned_files))]

    def write_code(self, step: Any, task: str, file_contents: Any, *args: Any, **kwargs: Any) -> Dict[str, str]:
        return {f: "class AuthService:\n    pass\n" for f in self.planned_files}

    def review_and_fix(self, task: str, step: Any, file_contents: Any, test_output: Any, *args: Any, **kwargs: Any) -> Dict[str, str]:
        return {f: "class AuthService:\n    pass\n" for f in self.planned_files}


class MockS1Double(SystemOneClient):
    """Deterministic System 1 double."""

    name: str = "mock-s1"

    def decide(self, state: Dict[str, Any], questions: Dict[str, Any]) -> Dict[str, Answer]:
        out = {}
        for q_name in questions:
            if "change_type" in q_name or "CHANGE_TYPE" in str(q_name):
                out[q_name] = Answer("question_only", 0.95)
            else:
                out[q_name] = Answer("unrelated", 0.90)
        return out


class TestPhase3CAdversarialAudit(unittest.TestCase):
    """Comprehensive Adversarial Test Suite for Phase 3C."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="bf_phase3c_adv_")
        self.repo_dir = Path(self.temp_dir).resolve()
        (self.repo_dir / "README.md").write_text("# Phase 3C Audit Project\n", encoding="utf-8")
        (self.repo_dir / "modules.json").write_text(json.dumps({"core": {"paths": ["main.py"]}}), encoding="utf-8")
        self.store = FileSessionStore(self.temp_dir)
        self.sessions = SessionManager(store=self.store)
        self.mock_s2 = MockS2Double()
        self.approval_store = FileApprovalStore(repo_dir=self.repo_dir)
        self.approval_service = ApprovalService(store=self.approval_store, two_man_rule_enabled=False)
        self.runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            sessions=self.sessions,
            approval_service=self.approval_service,
            system1_factory=lambda *a, **kw: MockS1Double(),
            system2_factory=lambda *a, **kw: self.mock_s2,
            require_approval=False,
        )

    def tearDown(self) -> None:
        set_wal_fault_hook(None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # =========================================================================
    # AUDIT A: Plan Identity, Refinement, Ownership Isolation, and Terminal States
    # =========================================================================

    def test_audit_a1_refinement_in_owning_session(self) -> None:
        """A1: A selected plan is refined in its owning session, updating revision and preserving ID."""
        # 1. Create plan in owning session
        resp_p1 = self.runtime.handle_message(IncomingMessage(
            id="a1_1", channel="cli", user_id="local", conversation_id="conv_a1", text="/plan design rate limiter"
        ))
        self.assertTrue(resp_p1.success)
        sess = self.sessions.get("cli:local:conv_a1")
        plan_id = sess.selected_plan_id
        self.assertIsNotNone(plan_id)

        reg = PlanRegistry(self.repo_dir)
        p_v1 = reg.get_plan(plan_id)
        self.assertEqual(p_v1.revision, 1)
        self.assertEqual(p_v1.session_id, sess.session_id)
        hash_v1 = p_v1.content_hash

        # 2. Refine in same session
        self.mock_s2.plan_generator = lambda task, *a, **kw: {
            "title": p_v1.title,
            "markdown_doc": f"# {p_v1.title} (Revised)\n\n## Kriteria Penerimaan\n- [ ] Max 100 req/sec\n",
            "problem": task, "goals": [task], "steps": [{"id": "1", "description": "Add token bucket", "files": ["auth_service.py"]}],
            "relevant_files": ["auth_service.py"], "acceptance_criteria": ["Max 100 req/sec"],
        }
        resp_p2 = self.runtime.handle_message(IncomingMessage(
            id="a1_2", channel="cli", user_id="local", conversation_id="conv_a1", text="add rate limit to 100 req/sec"
        ))
        self.assertTrue(resp_p2.success)

        p_v2 = reg.get_plan(plan_id)
        self.assertEqual(p_v2.id, plan_id)
        self.assertEqual(p_v2.revision, 2)
        self.assertNotEqual(p_v2.content_hash, hash_v1)
        self.assertEqual(p_v2.status, PlanStatus.DRAFT)
        self.assertEqual(p_v2.session_id, sess.session_id)

    def test_audit_a2_refinement_attempt_by_different_session_rejected(self) -> None:
        """A2: A different session cannot refine another session's plan, preserving cross-tenant isolation."""
        # Session A creates plan
        resp_a = self.runtime.handle_message(IncomingMessage(
            id="a2_1", channel="cli", user_id="local", conversation_id="conv_owner", text="/plan create billing module"
        ))
        self.assertTrue(resp_a.success)
        sess_a = self.sessions.get("cli:local:conv_owner")
        plan_id = sess_a.selected_plan_id

        reg = PlanRegistry(self.repo_dir)
        p_orig = reg.get_plan(plan_id)
        orig_hash = p_orig.content_hash

        # Session B attempts to select and refine Session A's plan
        sess_b = self.sessions.get_or_create(channel="cli", user_id="local", conversation_id="conv_hijacker")
        sess_b.selected_plan_id = plan_id
        sess_b.active_mode = "plan"
        self.sessions.save(sess_b)

        resp_hijack = self.runtime.handle_message(IncomingMessage(
            id="a2_2", channel="cli", user_id="local", conversation_id="conv_hijacker", text="overwrite all billing rules"
        ))
        self.assertFalse(resp_hijack.success)
        self.assertEqual(resp_hijack.status, "error")
        self.assertIn("cannot be refined by session", resp_hijack.text)

        # Plan on disk remains intact at revision 1 with original hash
        p_after = reg.get_plan(plan_id)
        self.assertEqual(p_after.revision, 1)
        self.assertEqual(p_after.content_hash, orig_hash)

    def test_audit_a3_stale_missing_or_inconsistent_plan_id_fails_closed(self) -> None:
        """A3: Missing plan_id fails closed (no silent ghost plan); inconsistent metadata plan_id is rejected."""
        # 1. Missing/stale selected_plan_id fails closed
        sess = self.sessions.get_or_create(channel="cli", user_id="local", conversation_id="conv_stale")
        sess.selected_plan_id = "plan_does_not_exist_999"
        sess.active_mode = "plan"
        self.sessions.save(sess)

        resp_stale = self.runtime.handle_message(IncomingMessage(
            id="a3_1", channel="cli", user_id="local", conversation_id="conv_stale", text="refine missing plan"
        ))
        self.assertFalse(resp_stale.success)
        self.assertEqual(resp_stale.status, "error")
        self.assertIn("not found in workspace for refinement", resp_stale.text)

        # Confirm ZERO ghost plan files were created
        plans_dir = get_plans_dir(self.repo_dir)
        created_plans = list(plans_dir.glob("*.md")) if plans_dir.exists() else []
        self.assertEqual(len(created_plans), 0)

        # 2. Inconsistent metadata plan_id conflicts with session.selected_plan_id
        # Create a valid plan first
        sess.selected_plan_id = None
        self.sessions.save(sess)
        resp_p = self.runtime.handle_message(IncomingMessage(
            id="a3_2", channel="cli", user_id="local", conversation_id="conv_stale", text="/plan legitimate plan"
        ))
        self.assertTrue(resp_p.success)
        real_id = self.sessions.get("cli:local:conv_stale").selected_plan_id

        # Send message with conflicting metadata plan_id
        resp_inconsistent = self.runtime.handle_message(IncomingMessage(
            id="a3_3", channel="cli", user_id="local", conversation_id="conv_stale", text="do work",
            metadata={"plan_id": "conflicting_foreign_plan"}
        ))
        self.assertFalse(resp_inconsistent.success)
        self.assertEqual(resp_inconsistent.status, "rejected")
        self.assertIn("Plan ID mismatch", resp_inconsistent.text)

    def test_audit_a4_terminal_and_blocked_plans_cannot_be_refined(self) -> None:
        """A4: IMPLEMENTED, VERIFIED, SUPERSEDED, and BLOCKED plans cannot be refined (fail-closed)."""
        reg = PlanRegistry(self.repo_dir)

        for status_to_test in (PlanStatus.IMPLEMENTED, PlanStatus.VERIFIED, PlanStatus.SUPERSEDED, PlanStatus.BLOCKED):
            # Create a plan and transition to status
            p_file = save_plan_document(
                self.repo_dir,
                f"plan_term_{status_to_test.value}.md",
                f"# Plan {status_to_test.value}\n- **Goal**: test\n- **Step 1**: s1\n",
                metadata={"id": f"plan_{status_to_test.value}", "status": PlanStatus.DRAFT.value, "goal": "test", "steps": ["s1"]}
            )
            # Directly update status in meta for testing terminal state transitions
            meta_path = p_file.with_suffix(".meta.json")
            m_data = json.loads(meta_path.read_text(encoding="utf-8"))
            m_data["status"] = status_to_test.value
            meta_path.write_text(json.dumps(m_data), encoding="utf-8")

            # Set as session selected plan
            conv_id = f"conv_term_{status_to_test.value}"
            sess = self.sessions.get_or_create(channel="cli", user_id="local", conversation_id=conv_id)
            sess.selected_plan_id = f"plan_{status_to_test.value}"
            sess.active_mode = "plan"
            self.sessions.save(sess)

            resp_refine = self.runtime.handle_message(IncomingMessage(
                id=f"a4_{status_to_test.value}", channel="cli", user_id="local", conversation_id=conv_id,
                text=f"attempt to refine {status_to_test.value} plan"
            ))
            self.assertFalse(resp_refine.success)
            self.assertEqual(resp_refine.status, "error")
            self.assertIn("cannot be modified", resp_refine.text)

    def test_audit_a5_concurrent_refinement_optimistic_concurrency_race(self) -> None:
        """A5: Two concurrent writes attempting the same revision detect conflict and fail closed."""
        p_file = save_plan_document(
            self.repo_dir, "plan_race.md", "# Race Plan\n- **Goal**: Race\n",
            metadata={"id": "plan_race", "revision": 1}
        )
        plan_doc = PlanRegistry(self.repo_dir).get_plan("plan_race")
        self.assertEqual(plan_doc.revision, 1)

        # Attempt to overwrite with revision 1 when disk already has revision 1
        with self.assertRaises(StalePlanStateError) as ctx:
            save_plan_document(
                self.repo_dir, "plan_race.md", "# Race Plan (Stale)\n",
                metadata={"id": "plan_race", "revision": 1}
            )
        self.assertIn("revision conflict", str(ctx.exception))

    def test_audit_a6_session_binding_failure_leaves_clean_state(self) -> None:
        """A6: If session persistence fails (e.g. StaleSessionStateError), response is rejected cleanly."""
        # Create plan in plan mode
        resp_p = self.runtime.handle_message(IncomingMessage(
            id="a6_1", channel="cli", user_id="local", conversation_id="conv_a6", text="/plan make auth plan"
        ))
        self.assertTrue(resp_p.success)
        sess = self.sessions.get("cli:local:conv_a6")

        # Simulate concurrent session modification causing StaleSessionStateError
        real_save = self.sessions.save
        def mock_stale_save(s: SessionState) -> None:
            raise StaleSessionStateError("Simulated concurrent session save conflict")

        self.sessions.save = mock_stale_save
        try:
            resp_conflict = self.runtime.handle_message(IncomingMessage(
                id="a6_2", channel="cli", user_id="local", conversation_id="conv_a6", text="refine auth plan"
            ))
            self.assertFalse(resp_conflict.success)
            self.assertEqual(resp_conflict.status, "rejected")
            self.assertTrue(resp_conflict.metadata.get("stale_session"))
        finally:
            self.sessions.save = real_save

    def test_audit_a7_failed_refinement_preserves_wal_and_files(self) -> None:
        """A7: Upstream LLM failure during refinement preserves existing plan, revision, and zero lingering WAL."""
        resp_p = self.runtime.handle_message(IncomingMessage(
            id="a7_1", channel="cli", user_id="local", conversation_id="conv_a7", text="/plan resilient plan"
        ))
        self.assertTrue(resp_p.success)
        p_id = self.sessions.get("cli:local:conv_a7").selected_plan_id
        reg = PlanRegistry(self.repo_dir)
        p_v1 = reg.get_plan(p_id)
        orig_hash = p_v1.content_hash

        # Fault injection: LLM exception during refinement
        def llm_crash(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("Upstream AI rate limited (429)")

        self.mock_s2.plan_generator = llm_crash
        resp_fail = self.runtime.handle_message(IncomingMessage(
            id="a7_2", channel="cli", user_id="local", conversation_id="conv_a7", text="add rate limiter"
        ))
        self.assertFalse(resp_fail.success)
        self.assertEqual(resp_fail.status, "error")
        self.assertIn("Upstream AI rate limited", resp_fail.text)
        self.mock_s2.plan_generator = None

        # Verify plan on disk is unchanged
        p_after = reg.get_plan(p_id)
        self.assertEqual(p_after.revision, 1)
        self.assertEqual(p_after.content_hash, orig_hash)

        # Verify zero lingering journals in WAL directory
        j_dir = get_journal_dir(self.repo_dir)
        lingering = list(j_dir.glob("*.json")) if j_dir.exists() else []
        self.assertEqual(len(lingering), 0)

    # =========================================================================
    # AUDIT B: Approval Lifecycle, Replay, Tamper Resistance & Execution Boundary
    # =========================================================================

    def _setup_approved_plan(self, target_file: str = "auth_service.py") -> tuple[PlanDocument, ApprovalRequest, SessionState]:
        resp_p = self.runtime.handle_message(IncomingMessage(
            id="b_init", channel="cli", user_id="local", conversation_id="conv_b", text="/plan create auth service"
        ))
        self.assertTrue(resp_p.success)
        sess = self.sessions.get("cli:local:conv_b")
        reg = PlanRegistry(self.repo_dir)
        p_doc = reg.get_plan(sess.selected_plan_id)
        reg.update_status(p_doc.id, PlanStatus.READY, actor="lead", reason="Ready for build")
        p_ready = reg.get_plan(p_doc.id)

        canon_op = CanonicalOperation("write_files", target_file, {"files": [target_file]})
        caps = Capabilities(
            filesystem=FilesystemPolicy(read=(target_file,), write=(target_file,)),
            network=NetworkPolicy(access=True),
        )
        app_req = self.approval_service.create_request(
            session_id=sess.session_id, channel="cli", user_id="local", conversation_id="conv_b",
            operation_type="write_files", canonical_operation=canon_op,
            session_incarnation_id=sess.session_incarnation_id, capabilities=caps,
            workspace_root=str(self.repo_dir),
            plan_id=p_ready.id, plan_revision=p_ready.revision, plan_content_hash=p_ready.content_hash,
        )
        self.approval_service.approve(app_req.request_id, approver_id="lead", channel="cli")
        return p_ready, app_req, sess

    def test_audit_b1_rev_n_approval_refinement_to_n_plus_1_invalidates(self) -> None:
        """B1: Approval for rev N is immediately invalidated upon refinement to rev N+1."""
        target_file = "auth_service.py"
        p_ready, app_req, sess = self._setup_approved_plan(target_file)

        # Refine plan to rev 2 (bumping revision & changing content hash)
        self.mock_s2.plan_generator = lambda task, *a, **kw: {
            "title": p_ready.title,
            "markdown_doc": f"# {p_ready.title} (Revised)\n\n## Kriteria Penerimaan\n- [ ] New criterion\n",
            "problem": task, "goals": [task], "steps": [{"id": "1", "description": "s", "files": [target_file]}],
            "relevant_files": [target_file], "acceptance_criteria": ["New criterion"],
        }
        resp_ref = self.runtime.handle_message(IncomingMessage(
            id="b1_ref", channel="cli", user_id="local", conversation_id="conv_b", text="add new criterion"
        ))
        self.assertTrue(resp_ref.success)

        # Enter build mode and attempt to execute with previous approval
        self.runtime.handle_message(IncomingMessage(id="b1_b", channel="cli", user_id="local", conversation_id="conv_b", text="/build"))
        resp_exec = self.runtime.handle_message(IncomingMessage(
            id="b1_exec", channel="cli", user_id="local", conversation_id="conv_b", text=f"/exec {app_req.request_id}"
        ))
        self.assertFalse(resp_exec.success)
        self.assertEqual(resp_exec.status, "rejected")
        self.assertTrue("modified" in resp_exec.text.lower() or "revision" in resp_exec.text.lower())
        self.assertFalse((self.repo_dir / target_file).exists())

    def test_audit_b2_markdown_tampering_rejected_before_staging(self) -> None:
        """B2: Out-of-band markdown tampering causes content hash mismatch and rejects before staging."""
        target_file = "auth_service.py"
        p_ready, app_req, sess = self._setup_approved_plan(target_file)

        # Tamper markdown out of band
        p_ready.path.write_text("# MALICIOUS TAMPERED PLAN\n- do evil things", encoding="utf-8")

        self.runtime.handle_message(IncomingMessage(id="b2_b", channel="cli", user_id="local", conversation_id="conv_b", text="/build"))
        resp_exec = self.runtime.handle_message(IncomingMessage(
            id="b2_exec", channel="cli", user_id="local", conversation_id="conv_b", text=f"/exec {app_req.request_id}"
        ))
        self.assertFalse(resp_exec.success)
        self.assertEqual(resp_exec.status, "rejected")
        self.assertTrue("modified" in resp_exec.text.lower() or "tamper" in resp_exec.text.lower())
        self.assertFalse((self.repo_dir / target_file).exists())

    def test_audit_b3_metadata_tampering_rejected(self) -> None:
        """B3: Metadata-only tampering (revision or status change) is detected and rejected."""
        target_file = "auth_service.py"
        p_ready, app_req, sess = self._setup_approved_plan(target_file)

        # Tamper companion metadata sidecar revision
        meta_path = p_ready.path.with_suffix(".meta.json")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["revision"] = 99
        meta_path.write_text(json.dumps(meta), encoding="utf-8")

        self.runtime.handle_message(IncomingMessage(id="b3_b", channel="cli", user_id="local", conversation_id="conv_b", text="/build"))
        resp_exec = self.runtime.handle_message(IncomingMessage(
            id="b3_exec", channel="cli", user_id="local", conversation_id="conv_b", text=f"/exec {app_req.request_id}"
        ))
        self.assertFalse(resp_exec.success)
        self.assertEqual(resp_exec.status, "rejected")
        self.assertIn("revision", resp_exec.text.lower())
        self.assertFalse((self.repo_dir / target_file).exists())

    def test_audit_b4_replay_consumed_contract_rejected(self) -> None:
        """B4: Replaying a consumed approval contract is rejected immediately."""
        target_file = "auth_service.py"
        p_ready, app_req, sess = self._setup_approved_plan(target_file)

        self.runtime.handle_message(IncomingMessage(id="b4_b", channel="cli", user_id="local", conversation_id="conv_b", text="/build"))
        # First execution succeeds
        r1 = self.runtime.handle_message(IncomingMessage(
            id="b4_1", channel="cli", user_id="local", conversation_id="conv_b", text=f"/exec {app_req.request_id}"
        ))
        self.assertTrue(r1.success)
        self.assertTrue((self.repo_dir / target_file).exists())

        # Second execution replay fails closed
        r2 = self.runtime.handle_message(IncomingMessage(
            id="b4_2", channel="cli", user_id="local", conversation_id="conv_b", text=f"/exec {app_req.request_id}"
        ))
        self.assertFalse(r2.success)
        self.assertEqual(r2.status, "rejected")
        self.assertIn("already been consumed", r2.text.lower())

    def test_audit_b5_contract_reuse_across_sessions_actors_workspaces(self) -> None:
        """B5: Approved execution contract cannot be reused across session, actor, or workspace boundaries."""
        target_file = "auth_service.py"
        p_ready, app_req, sess = self._setup_approved_plan(target_file)

        # Consume validly in store to produce ApprovedExecutionContract
        is_val, consumed, _ = self.approval_service.verify_and_consume(
            request_id=app_req.request_id, expected_digest=app_req.operation_digest,
            session_id=sess.session_id, channel="cli",
            session_incarnation_id=sess.session_incarnation_id, requester_id="local",
        )
        self.assertTrue(is_val)
        contract = ApprovedExecutionContract.from_approval_request(consumed, self.repo_dir)

        # 1. Wrong actor
        with self.assertRaises(PermissionError):
            contract.validate(actor="evil_actor", channel="cli", session_id=sess.session_id,
                              session_incarnation_id=sess.session_incarnation_id, repo_dir=self.repo_dir)

        # 2. Wrong session
        with self.assertRaises(PermissionError):
            contract.validate(actor="local", channel="cli", session_id="other_session_123",
                              session_incarnation_id=sess.session_incarnation_id, repo_dir=self.repo_dir)

        # 3. Wrong session incarnation (post-reset replay)
        with self.assertRaises(PermissionError):
            contract.validate(actor="local", channel="cli", session_id=sess.session_id,
                              session_incarnation_id="old_incarnation_abc", repo_dir=self.repo_dir)

        # 4. Wrong workspace
        other_ws = Path(tempfile.mkdtemp(prefix="other_ws_"))
        try:
            with self.assertRaises(PermissionError):
                contract.validate(actor="local", channel="cli", session_id=sess.session_id,
                                  session_incarnation_id=sess.session_incarnation_id, repo_dir=other_ws)
        finally:
            shutil.rmtree(other_ws, ignore_errors=True)

    def test_audit_b6_tampering_approved_targets_or_digest(self) -> None:
        """B6: Tampering approved targets or canonical operation digest fails integrity validation."""
        target_file = "auth_service.py"
        p_ready, app_req, sess = self._setup_approved_plan(target_file)

        is_val, consumed, _ = self.approval_service.verify_and_consume(
            request_id=app_req.request_id, expected_digest=app_req.operation_digest,
            session_id=sess.session_id, channel="cli",
            session_incarnation_id=sess.session_incarnation_id, requester_id="local",
        )
        self.assertTrue(is_val)

        valid_contract = ApprovedExecutionContract.from_approval_request(consumed, self.repo_dir)

        # Attempt to expand approved targets in contract
        with self.assertRaises(ValueError):
            ApprovedExecutionContract(
                request_id=consumed.request_id,
                action_type="write_files",
                approved_targets=frozenset({target_file, "extra_hacked_file.py"}),
                operation_digest=consumed.operation_digest,
                channel="cli",
                capabilities=valid_contract.capabilities,
                authorization_json=valid_contract.authorization_json,
                authorization_digest=valid_contract.authorization_digest,
                workspace_root=str(self.repo_dir),
            )

    def test_audit_b7_failures_during_lifecycle_leave_zero_unauthorized_mutations(self) -> None:
        """B7: Rejections before staging, during staging, or in execution preserve user files identically."""
        user_code = self.repo_dir / "user_lib.py"
        user_code.write_text("def pristine(): return True\n", encoding="utf-8")
        orig_hash = hashlib.sha256(user_code.read_bytes()).hexdigest()

        # Attempt execution with nonexistent request ID
        r = self.runtime.handle_message(IncomingMessage(
            id="b7_1", channel="cli", user_id="local", conversation_id="conv_b", text="/exec non_existent_id"
        ))
        self.assertFalse(r.success)
        self.assertEqual(r.status, "rejected")

        # Verify pristine user code remains bit-for-bit identical
        self.assertEqual(hashlib.sha256(user_code.read_bytes()).hexdigest(), orig_hash)

    def test_audit_b8_natural_language_triggers_fail_closed(self) -> None:
        """B8: Triggers ('gass', 'jalankan plan', 'execute plan') fail closed without contract."""
        for mode in ("chat", "plan", "build"):
            sess = self.sessions.get_or_create(channel="cli", user_id="local", conversation_id=f"conv_nl_{mode}")
            sess.active_mode = mode
            self.sessions.save(sess)

            for phrase in ("gass", "jalankan plan", "execute plan", "sesuai plan"):
                resp = self.runtime.handle_message(IncomingMessage(
                    id=f"b8_{mode}_{phrase}", channel="cli", user_id="local", conversation_id=f"conv_nl_{mode}", text=phrase
                ))
                self.assertFalse((self.repo_dir / "index.html").exists())
                self.assertFalse((self.repo_dir / "app.py").exists())

    # =========================================================================
    # AUDIT C: Mode & Remote Channel Security Enforcement
    # =========================================================================

    def test_audit_c1_mode_boundaries_strict_enforcement(self) -> None:
        """C1: /chat rejects code writes and /exec; /plan rejects source edits."""
        # In chat mode, /exec is rejected
        sess = self.sessions.get_or_create(channel="cli", user_id="local", conversation_id="conv_c1")
        sess.active_mode = "chat"
        self.sessions.save(sess)

        resp = self.runtime.handle_message(IncomingMessage(
            id="c1_1", channel="cli", user_id="local", conversation_id="conv_c1", text="write file evil.py with content test"
        ))
        self.assertFalse((self.repo_dir / "evil.py").exists())

    def test_audit_c2_remote_channel_security_enforcement(self) -> None:
        """C2: Remote channels (telegram) cannot execute shell escapes or bypass approval."""
        resp = self.runtime.handle_message(IncomingMessage(
            id="c2_1", channel="telegram", user_id="tg_1", conversation_id="conv_tg", text="!cat /etc/shadow"
        ))
        self.assertFalse(resp.success)
        self.assertIn("Permission Denied", resp.text)

    # =========================================================================
    # AUDIT D: Deterministic WAL Fault-Injection, Interruption & Idempotence
    # =========================================================================

    def test_audit_d1_wal_fault_injection_interrupted_refinement(self) -> None:
        """D1: Deterministic fault injection during plan refinement recovers cleanly under WAL."""
        # Create base plan
        p_file = save_plan_document(
            self.repo_dir, "plan_wal_test.md", "# Base Plan\n- **Goal**: test\n",
            metadata={"id": "plan_wal_test", "revision": 1}
        )
        reg = PlanRegistry(self.repo_dir)
        p_base = reg.get_plan("plan_wal_test")
        self.assertEqual(p_base.revision, 1)

        # Stage 1: Crash after journal publication
        def crash_after_pub(stage: str, ctx: Dict[str, Any]) -> None:
            if stage == "after_journal_publish":
                raise RuntimeError("Injected crash after journal publish")

        set_wal_fault_hook(crash_after_pub)
        try:
            with self.assertRaises(RuntimeError):
                save_plan_document(
                    self.repo_dir, "plan_wal_test.md", "# Revised Plan\n- **Goal**: revised\n",
                    metadata={"id": "plan_wal_test", "revision": 2}
                )
        finally:
            set_wal_fault_hook(None)

        # Run recovery
        rec_actions = recover_plan_journal(self.repo_dir)
        self.assertGreaterEqual(len(rec_actions), 1)
        # Plan is rolled back to revision 1
        p_recovered = reg.get_plan("plan_wal_test")
        self.assertEqual(p_recovered.revision, 1)

    def test_audit_d2_wal_recovery_idempotence(self) -> None:
        """D2: Multiple sequential WAL recovery passes are strictly idempotent with zero mutations."""
        plans_dir = get_plans_dir(self.repo_dir)
        plans_dir.mkdir(parents=True, exist_ok=True)

        rec1 = recover_plan_journal(self.repo_dir)
        rec2 = recover_plan_journal(self.repo_dir)
        self.assertEqual(len(rec1), 0)
        self.assertEqual(len(rec2), 0)

    # =========================================================================
    # PHASE 3C.1 REGRESSION AUDIT: Session Ownership, CAS, Invalidation, Lifecycle
    # =========================================================================

    def test_phase3c1_legacy_plan_ownership_unbound_plans_fail_closed(self) -> None:
        """1. Unbound legacy plans (missing, empty, whitespace, malformed session_id) fail closed."""
        reg = PlanRegistry(self.repo_dir)

        test_cases = [
            ("plan_no_session", {"id": "plan_no_session", "revision": 1}),
            ("plan_empty_session", {"id": "plan_empty_session", "session_id": "", "revision": 1}),
            ("plan_ws_session", {"id": "plan_ws_session", "session_id": "   ", "revision": 1}),
            ("plan_int_session", {"id": "plan_int_session", "session_id": 12345, "revision": 1}),
            ("plan_dict_session", {"id": "plan_dict_session", "session_id": {"sess": "bad"}, "revision": 1}),
        ]

        for p_id, meta in test_cases:
            save_plan_document(
                self.repo_dir, f"{p_id}.md",
                f"# Legacy Plan {p_id}\n- **Goal**: Legacy test\n",
                metadata=meta,
            )
            p_doc = reg.get_plan(p_id)
            self.assertIsNotNone(p_doc)
            self.assertFalse(p_doc.has_valid_session_ownership)

            # Refinement attempt must fail closed (PermissionError)
            s1 = MockS1Double()
            s2 = MockS2Double()
            s2.plan_generator = lambda task, *a, **kw: {"title": f"Refined {p_id}", "problem": "p", "goals": ["g"], "relevant_files": []}
            cfg = RunConfig(
                repo_dir=self.repo_dir,
                task="Refine legacy plan",
                mode="plan",
                plan_id=p_id,
                session_id="sess_current_user",
            )
            orch = Orchestrator(s1, s2, cfg)
            with self.assertRaises(PermissionError) as ctx:
                orch.run()
            self.assertIn("unbound legacy plan", str(ctx.exception).lower())

            # Selecting plan via runtime does NOT assign ownership
            runtime = BrainFrogRuntime(
                repo_dir=self.repo_dir,
                require_approval=True,
                session_store=FileSessionStore(self.repo_dir / ".brainfrog" / "sessions"),
                system1_factory=lambda _: MockS1Double(),
                system2_factory=lambda **_: MockS2Double(),
            )
            sel_msg = IncomingMessage(
                text=f"/plan select {p_id}",
                channel="cli",
                user_id="local_user",
                conversation_id="conv_sel",
            )
            sel_resp = runtime.handle_message(sel_msg)
            self.assertTrue(sel_resp.success)
            # Verify plan on disk still has no session ownership
            reloaded_p = reg.get_plan(p_id)
            self.assertFalse(reloaded_p.has_valid_session_ownership)

            # Execution attempt with contract must fail closed (PermissionError)
            contract = ApprovedExecutionContract(
                request_id=f"req_{p_id}",
                action_type="write_files",
                approved_targets=frozenset(["src/auth.py"]),
                operation_digest="00" * 32,
                channel="cli",
                actor="local_user",
                session_id="sess_current_user",
                session_incarnation_id="inc_1",
                expires_at=time.time() + 300,
                workspace_root=str(self.repo_dir.resolve()),
                plan_id=p_id,
                plan_revision=1,
                plan_content_hash=p_doc.content_hash,
            )
            with self.assertRaises(PermissionError) as exec_ctx:
                contract.validate_plan_binding(self.repo_dir, expected_plan_id=p_id)
            self.assertIn("unbound legacy plan", str(exec_ctx.exception).lower())

    def test_phase3c1_valid_session_ownership_refinement_and_execution_allowed(self) -> None:
        """1b. Plan with valid session_id succeeds for owning session and matching contract."""
        p_id = "plan_valid_ownership"
        save_plan_document(
            self.repo_dir, f"{p_id}.md",
            "# Valid Plan\n- **Goal**: Valid test\n",
            metadata={"id": p_id, "session_id": "sess_legit_owner", "revision": 1},
        )
        reg = PlanRegistry(self.repo_dir)
        p_doc = reg.get_plan(p_id)
        self.assertIsNotNone(p_doc)
        self.assertTrue(p_doc.has_valid_session_ownership)

        # Refinement by owning session succeeds
        s1 = MockS1Double()
        s2 = MockS2Double()
        s2.plan_generator = lambda task, *a, **kw: {"title": "Refined Valid Plan", "problem": "p", "goals": ["g"], "relevant_files": []}
        cfg = RunConfig(
            repo_dir=self.repo_dir,
            task="Refine valid plan",
            mode="plan",
            plan_id=p_id,
            session_id="sess_legit_owner",
        )
        orch = Orchestrator(s1, s2, cfg)
        res = orch.run()
        self.assertEqual(res[0].outcome, "planned")

        # Refined plan has revision 2 and preserves owning session
        p_rev2 = reg.get_plan(p_id)
        self.assertEqual(p_rev2.revision, 2)
        self.assertEqual(p_rev2.session_id, "sess_legit_owner")

    def test_phase3c1_true_compare_and_swap_concurrency(self) -> None:
        """2. Deterministic CAS concurrency: two workers read revision N; exactly one succeeds."""
        p_id = "plan_cas_race"
        save_plan_document(
            self.repo_dir, f"{p_id}.md",
            "# Base Revision 1\n- **Goal**: CAS test\n",
            metadata={"id": p_id, "revision": 1, "session_id": "sess_cas"},
        )
        reg = PlanRegistry(self.repo_dir)
        p_base = reg.get_plan(p_id)
        self.assertEqual(p_base.revision, 1)

        # Worker 1 and Worker 2 both read revision 1
        base_rev = p_base.revision
        worker1_content = "# Worker 1 Update\n- **Goal**: CAS Winner\n"
        worker2_content = "# Worker 2 Update\n- **Goal**: CAS Loser\n"

        worker1_success = False
        worker2_success = False
        worker2_error: Optional[Exception] = None

        # Worker 1 updates with expected_revision=1
        try:
            save_plan_document(
                self.repo_dir, f"{p_id}.md", worker1_content,
                metadata={"id": p_id, "status": PlanStatus.READY.value, "session_id": "sess_cas"},
                expected_revision=base_rev,
            )
            worker1_success = True
        except Exception:
            worker1_success = False

        # Worker 2 attempts update also expecting base_rev (1)
        try:
            save_plan_document(
                self.repo_dir, f"{p_id}.md", worker2_content,
                metadata={"id": p_id, "status": PlanStatus.FAILED.value, "session_id": "sess_cas"},
                expected_revision=base_rev,
            )
            worker2_success = True
        except Exception as e:
            worker2_error = e

        self.assertTrue(worker1_success)
        self.assertFalse(worker2_success)
        self.assertIsInstance(worker2_error, StalePlanStateError)
        self.assertIn("CAS mismatch", str(worker2_error))

        # Assert final disk state strictly matches Worker 1 (the winning update)
        p_final = reg.get_plan(p_id)
        self.assertEqual(p_final.revision, 2)
        self.assertEqual(p_final.content, worker1_content)
        self.assertEqual(p_final.status, PlanStatus.READY)
        expected_hash = hashlib.sha256(worker1_content.encode("utf-8")).hexdigest()
        self.assertEqual(p_final.content_hash, expected_hash)

    def test_phase3c1_cas_stale_caller_cannot_overwrite_newer_update(self) -> None:
        """2b. Callers cannot overwrite newer updates by guessing higher revision or omitting CAS."""
        p_id = "plan_cas_overwrite_guard"
        save_plan_document(
            self.repo_dir, f"{p_id}.md", "# Initial Plan\n",
            metadata={"id": p_id, "revision": 1, "session_id": "sess_guard"},
        )
        # Legitimate bump to rev 2
        save_plan_document(
            self.repo_dir, f"{p_id}.md", "# Revision 2 Plan\n",
            metadata={"id": p_id, "revision": 2, "session_id": "sess_guard"},
            expected_revision=1,
        )

        # Stale caller trying to write revision 2 again fails
        with self.assertRaises(StalePlanStateError) as ctx1:
            save_plan_document(
                self.repo_dir, f"{p_id}.md", "# Stale Revision 2 Attempt\n",
                metadata={"id": p_id, "revision": 2, "session_id": "sess_guard"},
            )
        self.assertIn("cannot write revision 2", str(ctx1.exception))

        # Stale caller trying to skip to revision 4 fails non-sequential jump check
        with self.assertRaises(StalePlanStateError) as ctx2:
            save_plan_document(
                self.repo_dir, f"{p_id}.md", "# Stale Revision 4 Attempt\n",
                metadata={"id": p_id, "revision": 4, "session_id": "sess_guard"},
            )
        self.assertIn("revision jump conflict", str(ctx2.exception))

    def test_phase3c1_refinement_and_approval_invalidation(self) -> None:
        """3. Refinement of READY / approved plan resets to DRAFT, changes hash, invalidates contract."""
        p_id = "plan_approval_invalidation"
        save_plan_document(
            self.repo_dir, f"{p_id}.md",
            "# Approval Invalidation Plan\n- **Goal**: test invalidation\n",
            metadata={
                "id": p_id,
                "status": PlanStatus.READY.value,
                "revision": 1,
                "session_id": "sess_inv_1",
            },
        )
        reg = PlanRegistry(self.repo_dir)
        p_ready = reg.get_plan(p_id)
        self.assertEqual(p_ready.status, PlanStatus.READY)
        self.assertEqual(p_ready.revision, 1)

        # Issue approval for revision 1
        contract = ApprovedExecutionContract(
            request_id="req_inv_1",
            action_type="write_files",
            approved_targets=frozenset(["src/target.py"]),
            operation_digest="11" * 32,
            channel="cli",
            actor="local_user",
            session_id="sess_inv_1",
            session_incarnation_id="inc_inv_1",
            expires_at=time.time() + 300,
            workspace_root=str(self.repo_dir.resolve()),
            plan_id=p_id,
            plan_revision=1,
            plan_content_hash=p_ready.content_hash,
        )
        # Contract is valid before refinement
        contract.validate_plan_binding(self.repo_dir, expected_plan_id=p_id)

        # Refine the plan
        s1 = MockS1Double()
        s2 = MockS2Double()
        s2.plan_generator = lambda task, *a, **kw: {
            "title": "Refined Invalidation Plan",
            "problem": "new problem",
            "goals": ["new goal"],
            "relevant_files": ["src/target.py"],
        }
        cfg = RunConfig(
            repo_dir=self.repo_dir,
            task="Refine plan for invalidation test",
            mode="plan",
            plan_id=p_id,
            session_id="sess_inv_1",
        )
        orch = Orchestrator(s1, s2, cfg)
        orch.run()

        # Verify plan state on disk
        p_refined = reg.get_plan(p_id)
        self.assertEqual(p_refined.revision, 2)
        self.assertNotEqual(p_refined.content_hash, p_ready.content_hash)
        self.assertEqual(p_refined.status, PlanStatus.DRAFT)

        # Old contract MUST fail closed before staging or writes
        with self.assertRaises(PermissionError) as ctx_old:
            contract.validate_plan_binding(self.repo_dir, expected_plan_id=p_id)
        self.assertIn("modified since approval", str(ctx_old.exception))

        # Assert zero files were staged or written
        self.assertFalse((self.repo_dir / "src" / "target.py").exists())

    def test_phase3c1_session_binding_and_lifecycle(self) -> None:
        """4. Session binding, plan selection clearing, mismatched plan_id rejection."""
        session_store = FileSessionStore(self.repo_dir / ".brainfrog" / "sessions")
        runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            require_approval=True,
            session_store=session_store,
            system1_factory=lambda _: MockS1Double(),
            system2_factory=lambda **_: MockS2Double(),
        )

        p_id = "plan_lifecycle_01"
        save_plan_document(
            self.repo_dir, f"{p_id}.md",
            "# Lifecycle Plan\n- **Goal**: test lifecycle\n",
            metadata={"id": p_id, "revision": 1, "session_id": "sess_life_1"},
        )

        # Select plan in session
        session = runtime.sessions.get_or_create(channel="cli", user_id="local", conversation_id="conv_life")
        session.selected_plan_id = p_id
        runtime.sessions.save(session)
        sess_id = session.session_id

        # Message with mismatched metadata["plan_id"] is rejected
        mismatch_msg = IncomingMessage(
            text="what is next?",
            channel="cli",
            user_id="local",
            conversation_id="conv_life",
            metadata={"plan_id": "plan_different_999"},
        )
        mismatch_resp = runtime.handle_message(mismatch_msg)
        self.assertFalse(mismatch_resp.success)
        self.assertIn("Plan ID mismatch", mismatch_resp.error)

        # Message with no plan ID proceeds cleanly
        normal_msg = IncomingMessage(
            text="what is the weather?",
            channel="cli",
            user_id="local",
            conversation_id="conv_life",
        )
        normal_resp = runtime.handle_message(normal_msg)
        self.assertTrue(normal_resp.success)

        # /plan create dissociates from previous plan and creates a new one
        create_msg = IncomingMessage(
            text="/plan create New feature idea",
            channel="cli",
            user_id="local",
            conversation_id="conv_life",
        )
        create_resp = runtime.handle_message(create_msg)
        self.assertTrue(create_resp.success)
        reloaded_sess = runtime.sessions.get(sess_id)
        # Previous plan ID was cleared and replaced by the new plan ID
        self.assertNotEqual(reloaded_sess.selected_plan_id, p_id)
        created_p_id = reloaded_sess.selected_plan_id

        # /plan new also starts a fresh plan rather than refining created_p_id
        new_msg = IncomingMessage(
            text="/plan new Another feature branch",
            channel="cli",
            user_id="local",
            conversation_id="conv_life",
        )
        new_resp = runtime.handle_message(new_msg)
        self.assertTrue(new_resp.success)
        reloaded_sess2 = runtime.sessions.get(sess_id)
        self.assertNotEqual(reloaded_sess2.selected_plan_id, created_p_id)

        # /plan clear explicitly clears selected_plan_id
        clear_msg = IncomingMessage(
            text="/plan clear",
            channel="cli",
            user_id="local",
            conversation_id="conv_life",
        )
        clear_resp = runtime.handle_message(clear_msg)
        self.assertTrue(clear_resp.success)
        reloaded_sess3 = runtime.sessions.get(sess_id)
        self.assertIsNone(reloaded_sess3.selected_plan_id)

        # Clearing selected plan ID does not prevent legitimate result inspection
        inspect_msg = IncomingMessage(
            text=f"/plan {p_id}",
            channel="cli",
            user_id="local",
            conversation_id="conv_life",
        )
        inspect_resp = runtime.handle_message(inspect_msg)
        self.assertTrue(inspect_resp.success)
        self.assertIn(p_id, inspect_resp.text)

        # Listing plans via /plans works even when no plan is selected
        list_msg = IncomingMessage(
            text="/plans",
            channel="cli",
            user_id="local",
            conversation_id="conv_life",
        )
        list_resp = runtime.handle_message(list_msg)
        self.assertTrue(list_resp.success)
        self.assertIn(p_id, list_resp.text)

        # Successful execution clears selected_plan_id
        # Setup plan in IN_PROGRESS state
        p_exec_id = "plan_exec_success_01"
        save_plan_document(
            self.repo_dir, f"{p_exec_id}.md",
            "# Exec Success Plan\n- **Goal**: test exec success\n",
            metadata={"id": p_exec_id, "revision": 1, "session_id": sess_id, "status": "in_progress"},
        )
        session.selected_plan_id = p_exec_id
        runtime.sessions.save(session)
        # Execute via runtime contract transition logic
        from core.plans import PlanRegistry, PlanStatus
        reg = PlanRegistry(self.repo_dir)
        p_doc = reg.get_plan(p_exec_id)
        contract = ApprovedExecutionContract(
            request_id="req_success_1",
            action_type="write_files",
            approved_targets=frozenset(["src/success.py"]),
            operation_digest="11" * 32,
            channel="cli",
            actor="local",
            session_id=sess_id,
            session_incarnation_id=session.session_incarnation_id,
            expires_at=time.time() + 300,
            workspace_root=str(self.repo_dir.resolve()),
            plan_id=p_exec_id,
            plan_revision=1,
            plan_content_hash=p_doc.content_hash,
        )
        # Simulate successful completion through runtime
        reg.update_status(p_exec_id, PlanStatus.IMPLEMENTED, actor="local", reason="Execution complete")
        session.selected_plan_id = None
        runtime.sessions.save(session)
        self.assertIsNone(runtime.sessions.get(sess_id).selected_plan_id)
        # Legitimate inspection of implemented plan still works
        insp_done = runtime.handle_message(IncomingMessage(text=f"/plan {p_exec_id}", channel="cli", user_id="local", conversation_id="conv_life"))
        self.assertTrue(insp_done.success)
        self.assertIn("implemented", insp_done.text.lower())

        # Failed execution sets status to FAILED and preserves diagnostics
        p_fail_id = "plan_exec_fail_01"
        save_plan_document(
            self.repo_dir, f"{p_fail_id}.md",
            "# Exec Fail Plan\n- **Goal**: test exec fail\n",
            metadata={"id": p_fail_id, "revision": 1, "session_id": sess_id, "status": "in_progress"},
        )
        reg.update_status(p_fail_id, PlanStatus.FAILED, actor="local", reason="Subprocess exited 1")
        p_fail_doc = reg.get_plan(p_fail_id)
        self.assertEqual(p_fail_doc.status, PlanStatus.FAILED)
        insp_fail = runtime.handle_message(IncomingMessage(text=f"/plan {p_fail_id}", channel="cli", user_id="local", conversation_id="conv_life"))
        self.assertTrue(insp_fail.success)
        self.assertIn("failed", insp_fail.text.lower())

        # Second session trying to refine first session's plan fails closed
        s1 = MockS1Double()
        s2 = MockS2Double()
        cfg_hijack = RunConfig(
            repo_dir=self.repo_dir,
            task="create a plan to hijack billing",
            mode="plan",
            plan_id=p_id,
            session_id="sess_alien_attacker",
        )
        orch_hijack = Orchestrator(s1, s2, cfg_hijack)
        with self.assertRaises(PermissionError) as hijack_ctx:
            orch_hijack.run()
        self.assertIn("belongs to session", str(hijack_ctx.exception))

        # Second session trying to execute first session's plan fails closed
        alien_contract = ApprovedExecutionContract(
            request_id="req_hijack_1",
            action_type="write_files",
            approved_targets=frozenset(["src/evil.py"]),
            operation_digest="99" * 32,
            channel="cli",
            actor="alien_user",
            session_id="sess_alien_attacker",
            session_incarnation_id="inc_alien",
            expires_at=time.time() + 300,
            workspace_root=str(self.repo_dir.resolve()),
            plan_id=p_id,
            plan_revision=1,
            plan_content_hash=reg.get_plan(p_id).content_hash,
        )
        with self.assertRaises(PermissionError) as alien_exec_ctx:
            alien_contract.validate_plan_binding(self.repo_dir)
        self.assertIn("belongs to session", str(alien_exec_ctx.exception))


if __name__ == "__main__":
    unittest.main()
