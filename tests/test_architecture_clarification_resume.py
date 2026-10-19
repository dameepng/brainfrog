"""Phase 4 Dedicated Architecture Clarification & Safe Execution Recovery Test Suite.

Verifies Phase 4 requirements:
1. Blocked step presentation & diagnosis:
   - Blocked step exposes the correct question and why it matters.
   - Distinguishes pending clarification from code failure.
   - Blocked steps perform zero mutations before resolution.
2. User answer resolution & validation:
   - Valid user answer resolves the correct unknown.
   - Empty or oversized answers are rejected.
   - Unknown ID cannot be resolved for the wrong plan.
   - Stale plan revision rejects resolution (optimistic concurrency).
   - Cross-session resolution is rejected.
   - Prompt injection / command payloads in answer cannot execute or grant permissions.
3. Authoritative plan binding:
   - Answer bound to plan metadata (assumptions, blocking_unknowns, markdown content).
   - Advance revision and recalculate semantic metadata hash.
   - Atomic WAL persistence with crash consistency.
4. Approval invalidation & reapproval:
   - Old execution contract becomes invalid immediately after resolution.
   - Answering a question does not itself authorize BUILD execution.
   - Fresh approval with updated contract permits execution to continue.
5. Checkpoint resumption:
   - Previously passed/verified steps are not replayed unnecessarily.
   - Resuming executes formerly blocked step once resolved and approved.
   - Changed requirements invalidate stale verification evidence on affected steps.
   - Unaffected verified steps remain preserved across crash/restart.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.plans import (
    BlockingUnknownError,
    PlanDocument,
    PlanRegistry,
    PlanStatus,
    StalePlanStateError,
    InvalidPlanTransitionError,
    PlanNotFoundError,
    compute_file_hash,
    compute_metadata_semantic_digest,
    format_blocking_unknown_explanation,
    save_plan_document,
)
from core.runtime.approval import (
    ApprovalRequest,
    ApprovalService,
    ApprovalStatus,
    CanonicalOperation,
    InMemoryApprovalStore,
    RiskClass,
)
from core.runtime.capabilities import Capabilities, FilesystemPolicy, NetworkPolicy
from core.runtime.contract import ApprovedExecutionContract
from core.runtime.messages import IncomingMessage
from core.runtime.runtime import BrainFrogRuntime
from core.runtime.session import SessionManager, SessionState
from core.runtime.step_runner import (
    PlanProgressTracker,
    StepExecutionStatus,
    StructuredStepRunner,
    StaleExecutionAuthorizationError,
)
from orchestrator import Orchestrator, RunConfig, StepResult
from system1.base import Answer, SystemOneClient


class SyntheticS1(SystemOneClient):
    name: str = "mock_s1"

    def decide(self, state: Dict[str, Any], questions: Dict[str, Any]) -> Dict[str, Answer]:
        return {k: Answer(choice="unrelated", score=0.1, noul=1.0, confidence=0.9) for k in questions}


class CapturingS2:
    def __init__(self, workspace_dir: Path) -> None:
        self.workspace_dir = workspace_dir
        self.executed_steps: List[str] = []

    def write_code(self, step: Any, task: str, *args: Any, **kwargs: Any) -> Dict[str, str]:
        step_id = str(getattr(step, "id", "") or (step.get("id") if isinstance(step, dict) else "1"))
        self.executed_steps.append(step_id)
        raw_files = getattr(step, "files", None) or (step.get("files") if isinstance(step, dict) else None) or []
        created: Dict[str, str] = {}
        for f in raw_files:
            target = self.workspace_dir / f
            target.parent.mkdir(parents=True, exist_ok=True)
            content = f"# Implemented step {step_id}\n"
            target.write_text(content, encoding="utf-8")
            created[f] = content
        return created


class TestArchitectureClarificationResume(unittest.TestCase):
    """Full test suite covering Phase 4 interactive clarification and recovery."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="bf_phase4_test_")
        self.repo_dir = Path(self.tmp_dir).resolve()
        (self.repo_dir / ".brainfrog" / "plans").mkdir(parents=True, exist_ok=True)
        (self.repo_dir / "src").mkdir(parents=True, exist_ok=True)

        # Initialize synthetic git repo
        subprocess.run(["git", "init"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.repo_dir, capture_output=True, check=True)

        # Initial commit so HEAD exists
        readme = self.repo_dir / "README.md"
        readme.write_text("# Test Repo\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=self.repo_dir, capture_output=True, check=True)

        self.registry = PlanRegistry(self.repo_dir)
        self.approval_service = ApprovalService(store=InMemoryApprovalStore())
        self.mock_s1 = SyntheticS1()
        self.mock_s2 = CapturingS2(self.repo_dir)

        self.runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            approval_service=self.approval_service,
        )
        self.sessions = self.runtime.sessions

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _create_test_plan(
        self,
        plan_id: str = "plan_auth_01",
        title: str = "Token Auth Implementation",
        status: PlanStatus = PlanStatus.READY,
        session_id: str = "cli:local:default",
        steps: Optional[List[Dict[str, Any]]] = None,
        blocking_unknowns: Optional[List[Any]] = None,
        clarifying_questions: Optional[List[str]] = None,
    ) -> PlanDocument:
        raw_steps = steps or [
            {"id": "1", "description": "Setup auth models", "files": ["src/models.py"]},
            {"id": "2", "description": "Implement token expiration", "files": ["src/tokens.py"]},
        ]
        raw_unknowns = blocking_unknowns if blocking_unknowns is not None else [
            {
                "id": "blk_token_ttl",
                "question": "Should tokens expire after 15 minutes, 1 hour, or another duration?",
                "decision": "Token expiration duration policy",
                "reason": "Security requirement for access token lifecycle",
                "affected_steps": ["2"],
                "affected_files": ["src/tokens.py"],
                "resolved": False,
            }
        ]
        save_plan_document(
            self.repo_dir,
            f"{plan_id}.md",
            f"# {title}\n- **Goal**: Implement token auth\n",
            metadata={
                "id": plan_id,
                "title": title,
                "goal": "Implement token auth",
                "status": status.value,
                "revision": 1,
                "session_id": session_id,
                "steps": raw_steps,
                "acceptance_criteria": ["Tokens expire per policy"],
                "blocking_unknowns": raw_unknowns,
                "clarifying_questions": clarifying_questions or [],
            },
        )
        p = self.registry.get_plan(plan_id)
        assert p is not None
        return p

    def _create_contract(
        self,
        plan: PlanDocument,
        targets: Optional[List[str]] = None,
        session_id: str = "cli:local:default",
        session_incarnation: str = "inc_p4_1",
        expires_in: float = 300.0,
    ) -> ApprovedExecutionContract:
        approved_targets = targets or ["src/models.py", "src/tokens.py"]
        op = CanonicalOperation(
            action_type="write_files",
            target=approved_targets[0],
            parameters={"files": approved_targets},
        )
        caps = Capabilities(
            filesystem=FilesystemPolicy(read=tuple(approved_targets), write=tuple(approved_targets)),
            network=NetworkPolicy(access=True),
        )
        app_req = self.approval_service.create_request(
            session_id=session_id,
            channel="cli",
            user_id="local",
            conversation_id="default",
            operation_type="write_files",
            canonical_operation=op,
            risk_class=RiskClass.MEDIUM.value,
            session_incarnation_id=session_incarnation,
            capabilities=caps,
            workspace_root=str(self.repo_dir),
            plan_id=plan.id,
            plan_revision=plan.revision,
            plan_content_hash=plan.content_hash,
            plan_metadata_hash=plan.metadata_hash,
        )
        app_req.status = ApprovalStatus.CONSUMED
        app_req.approver_id = "security_lead"
        app_req.approved_at = time.time()
        app_req.consumed_at = time.time()
        app_req.expires_at = time.time() + expires_in
        app_req.authorization_digest = app_req.compute_authorization_digest()
        app_req.approval_digest = app_req.compute_approval_digest()
        return ApprovedExecutionContract.from_approval_request(app_req, repo_dir=self.repo_dir)

    # -------------------------------------------------------------------------
    # 1. Blocked Step Diagnosis & Presentation
    # -------------------------------------------------------------------------
    def test_01_blocked_step_exposes_correct_question_and_reason(self) -> None:
        """1. A blocked step halts execution and presents the exact question, reason, and files."""
        plan = self._create_test_plan()
        contract = self._create_contract(plan)

        cfg = RunConfig(
            repo_dir=self.repo_dir,
            task="build auth",
            mode="build",
            execution_contract=contract,
        )
        orch = Orchestrator(self.mock_s1, self.mock_s2, cfg)
        runner = StructuredStepRunner(
            workspace_dir=self.repo_dir,
            orchestrator=orch,
            contract=contract,
            plan_id=plan.id,
        )

        # Step 1 (models.py) is unaffected and should run
        # Step 2 (tokens.py) depends on blk_token_ttl and must halt
        with self.assertRaises(BlockingUnknownError) as ctx:
            runner.run_steps(plan.steps)

        err_msg = str(ctx.exception)
        self.assertIn("blk_token_ttl", err_msg)
        self.assertIn("15 minutes", err_msg)
        self.assertIn("src/tokens.py", err_msg)
        self.assertIn("BLOCKED", err_msg)
        self.assertIn("/plan resolve blk_token_ttl", err_msg)

        # Plan status should have transitioned to BLOCKED
        updated_plan = self.registry.get_plan(plan.id)
        self.assertEqual(updated_plan.status, PlanStatus.BLOCKED)

    def test_13_blocked_steps_perform_zero_mutation_before_resolution(self) -> None:
        """13. Blocked steps perform zero mutation before resolution."""
        plan = self._create_test_plan()
        contract = self._create_contract(plan)

        cfg = RunConfig(repo_dir=self.repo_dir, task="build auth", mode="build", execution_contract=contract)
        orch = Orchestrator(self.mock_s1, self.mock_s2, cfg)
        runner = StructuredStepRunner(
            workspace_dir=self.repo_dir,
            orchestrator=orch,
            contract=contract,
            plan_id=plan.id,
        )

        try:
            runner.run_steps(plan.steps)
        except BlockingUnknownError:
            pass

        # Step 1 ran and created models.py
        self.assertTrue((self.repo_dir / "src" / "models.py").exists())
        # Step 2 was BLOCKED and must NOT have written tokens.py
        self.assertFalse((self.repo_dir / "src" / "tokens.py").exists())

    # -------------------------------------------------------------------------
    # 2. Unknown Resolution via PlanRegistry
    # -------------------------------------------------------------------------
    def test_02_valid_user_answer_resolves_correct_unknown(self) -> None:
        """2. A valid user answer resolves the correct unknown, binds it to the plan, and bumps revision."""
        plan = self._create_test_plan()
        orig_rev = plan.revision
        orig_hash = plan.metadata_hash

        updated = self.registry.resolve_blocking_unknown(
            plan_id=plan.id,
            unknown_id="blk_token_ttl",
            answer="Tokens must expire after 30 minutes with sliding window.",
            actor="alice",
        )

        self.assertEqual(updated.revision, orig_rev + 1)
        self.assertNotEqual(updated.metadata_hash, orig_hash)
        # Check unknown is marked resolved
        qs = updated.get_blocking_questions()
        resolved_q = next(q for q in qs if q["id"] == "blk_token_ttl")
        self.assertTrue(resolved_q["resolved"])
        self.assertEqual(resolved_q["status"], "resolved")
        self.assertEqual(resolved_q["answer"], "Tokens must expire after 30 minutes with sliding window.")
        self.assertEqual(resolved_q["answered_by"], "alice")

        # Check assumption was recorded
        self.assertTrue(any("30 minutes" in a for a in updated.assumptions))
        # Check markdown content has clarification section
        self.assertIn("Clarification Resolution (blk_token_ttl)", updated.content)

    def test_03_empty_or_invalid_answers_are_rejected(self) -> None:
        """3. Empty, whitespace-only, or oversized answers are rejected fail-closed."""
        plan = self._create_test_plan()

        # Empty answer
        with self.assertRaises(ValueError):
            self.registry.resolve_blocking_unknown(plan.id, "blk_token_ttl", "", actor="alice")

        # Whitespace answer
        with self.assertRaises(ValueError):
            self.registry.resolve_blocking_unknown(plan.id, "blk_token_ttl", "   \n\t  ", actor="alice")

        # Oversized answer (> 4000 chars)
        with self.assertRaises(ValueError):
            self.registry.resolve_blocking_unknown(plan.id, "blk_token_ttl", "A" * 4001, actor="alice")

        # Non-string answer
        with self.assertRaises(ValueError):
            self.registry.resolve_blocking_unknown(plan.id, "blk_token_ttl", 12345, actor="alice")  # type: ignore

    def test_04_unknown_id_cannot_be_resolved_for_wrong_plan(self) -> None:
        """4. Unknown ID cannot be resolved for a plan that doesn't contain it."""
        plan_a = self._create_test_plan(plan_id="plan_a", title="Plan A")
        plan_b = self._create_test_plan(plan_id="plan_b", title="Plan B", blocking_unknowns=[])

        with self.assertRaises(ValueError):
            self.registry.resolve_blocking_unknown(plan_b.id, "blk_token_ttl", "30 minutes", actor="alice")

    def test_05_stale_plan_revision_rejects_answer(self) -> None:
        """5. A stale plan revision rejects the answer (optimistic revision check)."""
        plan = self._create_test_plan()
        # Disk revision is 1, expect revision 99
        with self.assertRaises(StalePlanStateError):
            self.registry.resolve_blocking_unknown(
                plan.id,
                "blk_token_ttl",
                "30 minutes",
                actor="alice",
                expected_revision=99,
            )

    def test_06_concurrent_conflicting_answers_cannot_silently_overwrite(self) -> None:
        """6. Concurrent resolutions with stale expected_revision fail rather than overwrite."""
        plan = self._create_test_plan()

        # User A resolves at revision 1
        self.registry.resolve_blocking_unknown(
            plan.id, "blk_token_ttl", "30 minutes", actor="alice", expected_revision=1
        )

        # User B resolves with same base revision 1 (now stale, revision is 2)
        with self.assertRaises((StalePlanStateError, InvalidPlanTransitionError)):
            self.registry.resolve_blocking_unknown(
                plan.id, "blk_token_ttl", "60 minutes", actor="bob", expected_revision=1
            )

    def test_07_clarification_updates_semantic_metadata_binding(self) -> None:
        """7. Clarification updates the semantic metadata digest deterministically."""
        plan = self._create_test_plan()
        orig_digest = plan.metadata_hash

        updated = self.registry.resolve_blocking_unknown(
            plan.id, "blk_token_ttl", "15 minutes", actor="alice"
        )
        self.assertNotEqual(updated.metadata_hash, orig_digest)

        # Recomputed digest from loaded meta matches updated plan metadata_hash
        reloaded = self.registry.get_plan(plan.id)
        self.assertEqual(reloaded.metadata_hash, updated.metadata_hash)
        self.assertFalse(reloaded.metadata_tampered)

    # -------------------------------------------------------------------------
    # 3. Contract Invalidation & Reapproval Boundaries
    # -------------------------------------------------------------------------
    def test_08_old_execution_approvals_become_invalid_after_material_resolution(self) -> None:
        """8. Old execution contracts are invalidated immediately after plan resolution."""
        plan = self._create_test_plan()
        old_contract = self._create_contract(plan)

        # Contract initially validates cleanly
        old_contract.validate_plan_binding(self.repo_dir)

        # User resolves the unknown -> revision bumps, metadata hash changes
        self.registry.resolve_blocking_unknown(
            plan.id, "blk_token_ttl", "30 minutes", actor="alice"
        )

        # Old contract fails closed on pre-mutation validation
        with self.assertRaises(PermissionError) as ctx:
            old_contract.validate_plan_binding(self.repo_dir)
        self.assertIn("modified since approval", str(ctx.exception))

    def test_09_answering_a_question_does_not_authorize_build(self) -> None:
        """9. Answering a clarification question does NOT create or grant execution authorization."""
        plan = self._create_test_plan()
        self.registry.resolve_blocking_unknown(
            plan.id, "blk_token_ttl", "30 minutes", actor="alice"
        )

        # Attempt to run without a fresh ApprovedExecutionContract
        cfg_no_contract = RunConfig(
            repo_dir=self.repo_dir,
            task="execute plan",
            mode="build",
            execution_contract=None,
        )
        orch = Orchestrator(self.mock_s1, self.mock_s2, cfg_no_contract)
        results = orch.run()
        self.assertEqual(results[0].outcome, "not_implemented")
        # No mutations allowed
        self.assertFalse((self.repo_dir / "src" / "tokens.py").exists())

    def test_10_fresh_approval_permits_updated_plan_to_continue(self) -> None:
        """10. Fresh approval with updated revision and metadata hash permits execution to proceed."""
        plan = self._create_test_plan()
        updated_plan = self.registry.resolve_blocking_unknown(
            plan.id, "blk_token_ttl", "30 minutes", actor="alice"
        )

        # Issue fresh contract bound to updated_plan
        fresh_contract = self._create_contract(updated_plan)
        fresh_contract.validate_plan_binding(self.repo_dir)

        cfg = RunConfig(
            repo_dir=self.repo_dir,
            task="build auth",
            mode="build",
            execution_contract=fresh_contract,
        )
        orch = Orchestrator(self.mock_s1, self.mock_s2, cfg)
        runner = StructuredStepRunner(
            workspace_dir=self.repo_dir,
            orchestrator=orch,
            contract=fresh_contract,
            plan_id=updated_plan.id,
        )

        results = runner.run_steps(updated_plan.steps)
        self.assertEqual(len(results), 2)
        self.assertTrue((self.repo_dir / "src" / "tokens.py").exists())

    # -------------------------------------------------------------------------
    # 4. Checkpoint Resumption & Avoiding Redundant Execution
    # -------------------------------------------------------------------------
    def test_11_verified_steps_are_not_replayed_unnecessarily(self) -> None:
        """11. Resuming execution skips already completed and verified steps."""
        plan = self._create_test_plan()
        contract1 = self._create_contract(plan)

        cfg1 = RunConfig(repo_dir=self.repo_dir, task="build auth", mode="build", execution_contract=contract1)
        orch1 = Orchestrator(self.mock_s1, self.mock_s2, cfg1)
        runner1 = StructuredStepRunner(
            workspace_dir=self.repo_dir,
            orchestrator=orch1,
            contract=contract1,
            plan_id=plan.id,
        )

        # Initial run: Step 1 passes, Step 2 blocks
        try:
            runner1.run_steps(plan.steps)
        except BlockingUnknownError:
            pass

        self.assertEqual(self.mock_s2.executed_steps, ["1"])

        # Resolve unknown
        updated_plan = self.registry.resolve_blocking_unknown(
            plan.id, "blk_token_ttl", "30 minutes", actor="alice"
        )

        # Obtain fresh approval contract
        contract2 = self._create_contract(updated_plan)
        cfg2 = RunConfig(repo_dir=self.repo_dir, task="build auth", mode="build", execution_contract=contract2)
        orch2 = Orchestrator(self.mock_s1, self.mock_s2, cfg2)
        runner2 = StructuredStepRunner(
            workspace_dir=self.repo_dir,
            orchestrator=orch2,
            contract=contract2,
            plan_id=updated_plan.id,
        )

        # Clear executed_steps tracker to observe replay
        self.mock_s2.executed_steps.clear()

        # Resumed run
        runner2.run_steps(updated_plan.steps)

        # Step 1 was ALREADY passed and must NOT be executed again!
        # Only Step 2 executes!
        self.assertEqual(self.mock_s2.executed_steps, ["2"])

    def test_12_changed_requirements_invalidate_stale_step_evidence(self) -> None:
        """12. If clarification specifically affects a previously passed step, that step is reset."""
        # Create plan where Step 1 is affected by blk_model_choice
        plan = self._create_test_plan(
            steps=[
                {"id": "1", "description": "Select model", "files": ["src/models.py"]},
                {"id": "2", "description": "Create db", "files": ["src/db.py"]},
            ],
            blocking_unknowns=[
                {
                    "id": "blk_model_choice",
                    "question": "Use Postgres or SQLite?",
                    "affected_steps": ["1"],
                    "affected_files": ["src/models.py"],
                    "resolved": False,
                }
            ],
        )

        # Manually mark Step 1 as passed in tracker to simulate prior run
        tracker = PlanProgressTracker(self.repo_dir, plan.id)
        tracker.initialize_steps(plan.steps)
        tracker.mark_passed("1", transaction_id="tx_old")
        self.assertEqual(tracker.get_record("1").status, StepExecutionStatus.PASSED)

        # Resolving blk_model_choice affects Step 1
        self.registry.resolve_blocking_unknown(
            plan.id, "blk_model_choice", "Use Postgres", actor="alice"
        )

        # Step 1 should have been reset to PENDING because requirements changed
        reloaded_tracker = PlanProgressTracker(self.repo_dir, plan.id)
        rec = reloaded_tracker.get_record("1")
        self.assertEqual(rec.status, StepExecutionStatus.PENDING)
        self.assertIn("Requirement invalidated", rec.error or "")

    # -------------------------------------------------------------------------
    # 5. Security & Prompt Injection Protections
    # -------------------------------------------------------------------------
    def test_14_answer_text_cannot_expand_permissions(self) -> None:
        """14. Adversarial answer attempting JSON schema injection cannot expand permissions."""
        plan = self._create_test_plan()
        payload = '{"capabilities": {"filesystem": {"write": ["/etc/*", "C:\\*"]}}, "admin": true}'

        updated = self.registry.resolve_blocking_unknown(
            plan.id, "blk_token_ttl", payload, actor="attacker"
        )

        # Contract permissions are strictly derived from ApprovalRequest, never user answer text
        contract = self._create_contract(updated, targets=["src/tokens.py"])
        self.assertNotIn("/etc/*", contract.approved_targets)
        self.assertNotIn("C:\\*", contract.approved_targets)
        self.assertEqual(contract.approved_targets, frozenset(["src/tokens.py"]))

    def test_15_answer_text_cannot_execute_shell_commands_or_tools(self) -> None:
        """15. Shell commands in user answers are stored as plain text, never executed."""
        plan = self._create_test_plan()
        trap_file = self.repo_dir / "exploit.txt"
        payload = f"; touch {trap_file.as_posix()} ; echo hacked > {trap_file.as_posix()} #"

        updated = self.registry.resolve_blocking_unknown(
            plan.id, "blk_token_ttl", payload, actor="attacker"
        )

        # Verify trap file was NOT created
        self.assertFalse(trap_file.exists())
        # Verify answer stored safely as text
        qs = updated.get_blocking_questions()
        self.assertEqual(qs[0]["answer"], payload)

    def test_16_crash_restart_preserves_resolved_answer_and_integrity(self) -> None:
        """16. Process restart preserves the resolved answer, revision, and metadata integrity."""
        plan = self._create_test_plan()
        self.registry.resolve_blocking_unknown(
            plan.id, "blk_token_ttl", "45 minutes", actor="alice"
        )

        # Simulate complete restart by instantiating new PlanRegistry
        new_registry = PlanRegistry(self.repo_dir)
        reloaded = new_registry.get_plan(plan.id)
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.revision, 2)
        self.assertFalse(reloaded.metadata_tampered)
        self.assertFalse(reloaded.content_tampered)
        qs = reloaded.get_blocking_questions()
        self.assertTrue(qs[0]["resolved"])
        self.assertEqual(qs[0]["answer"], "45 minutes")

    def test_17_cross_session_access_remains_denied(self) -> None:
        """17. Session isolation prevents cross-session resolution of plan unknowns."""
        plan = self._create_test_plan(session_id="cli:alice:session1")

        # Session 2 tries to resolve session 1's plan
        with self.assertRaises(PermissionError):
            self.registry.resolve_blocking_unknown(
                plan.id,
                "blk_token_ttl",
                "30 minutes",
                actor="bob",
                session_id="cli:bob:session2",
            )

    def test_18_legacy_plans_remain_readable(self) -> None:
        """18. Legacy plans without blocking_unknowns remain fully readable."""
        # Create legacy plan without any blocking_unknowns or architectural fields
        save_plan_document(
            self.repo_dir,
            "legacy_plan.md",
            "# Legacy Plan\n- **Goal**: Legacy work\n",
            metadata={
                "id": "legacy_plan",
                "title": "Legacy Plan",
                "goal": "Legacy work",
                "status": "ready",
                "revision": 1,
                "steps": [{"id": "1", "description": "legacy step"}],
            },
        )
        loaded = self.registry.get_plan("legacy_plan")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.get_blocking_questions(), [])
        is_blocked, blk_items = loaded.has_unresolved_blocking_unknowns()
        self.assertFalse(is_blocked)
        self.assertEqual(blk_items, [])

    def test_20_normal_non_blocking_clarifications_do_not_halt_authorized_work(self) -> None:
        """20. Clarifying questions (non-blocking) do NOT halt authorized steps."""
        plan = self._create_test_plan(
            blocking_unknowns=[],
            clarifying_questions=["Would you prefer dark mode or light mode in the UI?"],
        )
        contract = self._create_contract(plan)

        cfg = RunConfig(repo_dir=self.repo_dir, task="build auth", mode="build", execution_contract=contract)
        orch = Orchestrator(self.mock_s1, self.mock_s2, cfg2 if 'cfg2' in locals() else cfg)
        runner = StructuredStepRunner(
            workspace_dir=self.repo_dir,
            orchestrator=orch,
            contract=contract,
            plan_id=plan.id,
        )

        # Should complete without error
        results = runner.run_steps(plan.steps)
        self.assertEqual(len(results), 2)
        self.assertTrue((self.repo_dir / "src" / "models.py").exists())
        self.assertTrue((self.repo_dir / "src" / "tokens.py").exists())

    # -------------------------------------------------------------------------
    # 6. Runtime Slash Command Family (/plan questions & /plan resolve)
    # -------------------------------------------------------------------------
    def test_21_runtime_plan_questions_and_resolve_commands(self) -> None:
        """21. /plan questions and /plan resolve commands execute through runtime messaging."""
        plan = self._create_test_plan()

        # Select plan
        resp_sel = self.runtime.handle_message(IncomingMessage(
            channel="cli", user_id="local", conversation_id="default",
            text=f"/plan select {plan.id}",
        ))
        self.assertTrue(resp_sel.success)

        # List questions
        resp_qs = self.runtime.handle_message(IncomingMessage(
            channel="cli", user_id="local", conversation_id="default",
            text="/plan questions",
        ))
        self.assertTrue(resp_qs.success)
        self.assertIn("blk_token_ttl", resp_qs.text)
        self.assertIn("UNRESOLVED", resp_qs.text)
        self.assertIn("15 minutes", resp_qs.text)

        # Resolve question
        resp_res = self.runtime.handle_message(IncomingMessage(
            channel="cli", user_id="local", conversation_id="default",
            text="/plan resolve blk_token_ttl Expire tokens after 1 hour",
        ))
        self.assertTrue(resp_res.success)
        self.assertIn("Blocking Unknown Resolved", resp_res.text)
        self.assertIn("Expire tokens after 1 hour", resp_res.text)
        self.assertIn("Security Notice", resp_res.text)

        # Re-list questions: now resolved
        resp_qs2 = self.runtime.handle_message(IncomingMessage(
            channel="cli", user_id="local", conversation_id="default",
            text="/plan questions",
        ))
        self.assertTrue(resp_qs2.success)
        self.assertIn("RESOLVED", resp_qs2.text)
        self.assertIn("Expire tokens after 1 hour", resp_qs2.text)

    # -------------------------------------------------------------------------
    # 7. Adversarial Scenarios
    # -------------------------------------------------------------------------
    def test_22_adversarial_mutate_plan_file_externally_then_resume(self) -> None:
        """Adversarial: Resolving unknown, then externally mutating plan file causes resume to fail closed."""
        plan = self._create_test_plan()
        updated_plan = self.registry.resolve_blocking_unknown(
            plan.id, "blk_token_ttl", "30 minutes", actor="alice"
        )
        fresh_contract = self._create_contract(updated_plan)

        # Adversary mutates plan markdown on disk behind BrainFrog's back
        plan_md = self.repo_dir / ".brainfrog" / "plans" / f"{plan.id}.md"
        plan_md.write_text("# MALICIOUS MODIFICATION\n", encoding="utf-8")

        # Resume should immediately fail closed with content tampering error
        with self.assertRaises(PermissionError):
            fresh_contract.validate_plan_binding(self.repo_dir)

    def test_23_adversarial_different_answer_submitted_after_approval(self) -> None:
        """Adversarial: Submitting a new answer after approval invalidates that approval."""
        plan = self._create_test_plan(
            blocking_unknowns=[
                {"id": "q1", "question": "Question 1", "resolved": False},
                {"id": "q2", "question": "Question 2", "resolved": False},
            ]
        )

        # Resolve Q1
        p_q1 = self.registry.resolve_blocking_unknown(plan.id, "q1", "Answer 1", actor="alice")
        # Contract approved for plan after Q1
        contract_q1 = self._create_contract(p_q1)
        contract_q1.validate_plan_binding(self.repo_dir)

        # Resolve Q2 (changes requirements again)
        self.registry.resolve_blocking_unknown(plan.id, "q2", "Answer 2", actor="alice")

        # Previous approval for Q1 is now stale and cannot execute
        with self.assertRaises(PermissionError):
            contract_q1.validate_plan_binding(self.repo_dir)


if __name__ == "__main__":
    unittest.main()
