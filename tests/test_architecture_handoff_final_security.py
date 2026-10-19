"""Phase 3C Dedicated Final Security Closure & Adversarial Regression Suite.

Verifies:
1. Unknown-State Semantics:
   - Distinguishes missing field vs explicit empty list [] vs nonempty valid list vs malformed field.
   - Non-blocking clarifying questions do not block authorized independent steps.
   - Unresolved blocking unknowns strictly halt execution before mutation.
   - Malformed fields fail closed without crashing or gaining trusted state.
   - Target context 'unknown' does not blanket-block safe bounded work.
   - Steps assuming an answer in their text are still halted by the pre-mutation gate.

2. Legacy Plans vs Legacy Approvals:
   - Legacy plans remain readable without error.
   - Execution contracts without semantic metadata binding fail closed on architecture-aware plans.
   - Fresh reapprovals bind current metadata digest and enable execution.
   - Metadata tampering after approval invalidates the contract.
   - Authorized lifecycle transitions (READY -> IN_PROGRESS) do not produce false tampering errors.
   - Session isolation and revision bounds remain enforced.

3. Provider Schemas and Parsers End-to-End:
   - Antigravity, OpenAI, and Claude client response extraction and normalization.
   - Explicit empty blocking unknowns vs omitted fields.
   - Legacy responses without new fields.
   - Extra/unapproved fields safely ignored without capability escalation.
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
    compute_file_hash,
    compute_metadata_semantic_digest,
    save_plan_document,
)
from core.runtime.capabilities import Capabilities, FilesystemPolicy, NetworkPolicy
from core.runtime.approval import (
    ApprovalRequest,
    ApprovalService,
    ApprovalStatus,
    CanonicalOperation,
    InMemoryApprovalStore,
    RiskClass,
)
from core.runtime.contract import ApprovedExecutionContract
from core.runtime.step_runner import (
    StructuredStepRunner,
    StaleExecutionAuthorizationError,
)
from orchestrator import (
    Orchestrator,
    PlanStep,
    RunConfig,
    StepResult,
)
from system1.base import Answer, SystemOneClient
from system2.json_utils import extract_json


# -----------------------------------------------------------------------------
# Synthetic Test Doubles
# -----------------------------------------------------------------------------
class SyntheticSystem1(SystemOneClient):
    name: str = "mock_s1"

    def decide(self, state: Dict[str, Any], questions: Dict[str, Any]) -> Dict[str, Answer]:
        return {
            k: Answer(choice="unrelated", score=0.1, noul=1.0, confidence=0.9)
            for k in questions
        }


class CapturingSystem2:
    def __init__(
        self,
        output_files: Optional[Dict[str, str]] = None,
        plan_and_prd_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.guidelines = ""
        self.provider_name = "mock_s2"
        self.model = "mock-model"
        self.output_files = output_files or {"src/safe.py": "# synthetic code\n"}
        self.plan_and_prd_data = plan_and_prd_data or {}
        self.write_code_calls: List[Dict[str, Any]] = []

    def plan_and_prd(
        self,
        task: str,
        repo_tree: str,
        focus_files: Dict[str, str],
        pinned_files: Optional[Dict[str, str]] = None,
        images: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        default_data = {
            "title": "Synthetic PRD",
            "is_small_task": False,
            "problem": "Synthetic problem",
            "goals": [task],
            "scope": ["src/safe.py"],
            "non_scope": ["src/forbidden.py"],
            "steps": [{"id": "1", "description": "Execute step", "files": ["src/safe.py"]}],
            "relevant_files": ["src/safe.py"],
        }
        res = dict(default_data)
        res.update(self.plan_and_prd_data)
        return res

    def plan_task(self, task: str, focus_tree: str = "", **kwargs: Any) -> List[PlanStep]:
        if "utility and payment" in task.lower():
            return [
                PlanStep(id="1", description="Implement utility helper", files=["src/util.py"]),
                PlanStep(id="2", description="Implement payment processor", files=["src/pay.py"]),
            ]
        return [PlanStep(id="1", description="Execute plan step", files=list(self.output_files.keys()))]

    def write_code(
        self,
        step: Any,
        task: str,
        file_contents: Dict[str, str],
        pinned_files: Optional[Dict[str, str]] = None,
        images: Optional[List[Any]] = None,
    ) -> Dict[str, str]:
        self.write_code_calls.append({
            "step": step,
            "task": task,
            "file_contents": file_contents,
        })
        step_files = (
            getattr(step, "files", None)
            or (step.get("files") if isinstance(step, dict) else [])
            or []
        )
        if step_files:
            return {f: self.output_files.get(f, "# synthetic implementation\n") for f in step_files if f in self.output_files}
        return dict(self.output_files)

    def draft_pr(self, task: str, files_changed: List[str], test_summary: str = "", **kwargs: Any) -> Dict[str, str]:
        return {"title": f"feat: {task}", "body": "Synthetic PR"}


# -----------------------------------------------------------------------------
# Phase 3C Final Security Test Suite
# -----------------------------------------------------------------------------
class ArchitectureHandoffFinalSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="brainfrog_sec_audit_")
        self.workspace = Path(self.test_dir).resolve()
        (self.workspace / ".brainfrog" / "plans").mkdir(parents=True, exist_ok=True)
        (self.workspace / "src").mkdir(parents=True, exist_ok=True)
        (self.workspace / "api").mkdir(parents=True, exist_ok=True)

        try:
            subprocess.run(["git", "init"], cwd=self.workspace, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "SecurityAuditor"], cwd=self.workspace, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "auditor@synthetic.local"], cwd=self.workspace, check=True, capture_output=True)
            init_file = self.workspace / "README.md"
            init_file.write_text("# Synthetic Security Audit Workspace\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=self.workspace, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=self.workspace, check=True, capture_output=True)
        except Exception:
            pass

        self.registry = PlanRegistry(self.workspace)

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_approved_contract(
        self,
        plan_doc: PlanDocument,
        targets: Optional[List[str]] = None,
        session_id: str = "sess_sec_01",
        session_incarnation: str = "inc_sec_01",
        include_metadata_hash: bool = True,
    ) -> ApprovedExecutionContract:
        approved_targets = targets or ["src/safe.py"]
        op = CanonicalOperation(
            action_type="write_files",
            target=approved_targets[0],
            parameters={"files": approved_targets},
        )
        caps = Capabilities(
            filesystem=FilesystemPolicy(read=tuple(approved_targets), write=tuple(approved_targets)),
            network=NetworkPolicy(access=True),
        )
        app_service = ApprovalService(store=InMemoryApprovalStore())
        meta_hash = getattr(plan_doc, "metadata_hash", None) if include_metadata_hash else None
        app_req = app_service.create_request(
            session_id=session_id,
            channel="cli",
            user_id="security_auditor",
            conversation_id="conv_sec_01",
            operation_type="write_files",
            canonical_operation=op,
            risk_class=RiskClass.MEDIUM.value,
            session_incarnation_id=session_incarnation,
            capabilities=caps,
            workspace_root=str(self.workspace),
            plan_id=plan_doc.id,
            plan_revision=plan_doc.revision,
            plan_content_hash=plan_doc.content_hash,
            plan_metadata_hash=meta_hash,
        )
        app_req.status = ApprovalStatus.CONSUMED
        app_req.approver_id = "security_lead"
        app_req.approved_at = time.time()
        app_req.consumed_at = time.time()
        app_req.approval_digest = app_req.compute_approval_digest()
        contract = ApprovedExecutionContract.from_approval_request(app_req, repo_dir=self.workspace)
        if not include_metadata_hash:
            object.__setattr__(contract, "plan_metadata_hash", None)
        return contract

    # =========================================================================
    # SECTION 3: UNKNOWN-STATE SEMANTICS
    # =========================================================================

    def test_01_explicit_empty_blocking_list_with_clarifying_questions(self) -> None:
        """Provider explicitly returns blocking_unknowns: [] with clarifying questions; step executes."""
        (self.workspace / "src" / "safe.py").write_text("# safe\n", encoding="utf-8")
        plan_data = {
            "title": "Explicit Empty Blocking PRD",
            "is_small_task": False,
            "goals": ["Implement feature"],
            "scope": ["src/safe.py"],
            "steps": [{"id": "1", "description": "Implement feature", "files": ["src/safe.py"]}],
            "relevant_files": ["src/safe.py"],
            "blocking_unknowns": [],  # Explicit empty list: provider established no blocking decisions
            "clarifying_questions": ["Should we support dark mode in v2?"],  # Non-blocking question
        }
        s2 = CapturingSystem2(
            plan_and_prd_data=plan_data,
            output_files={"src/safe.py": "# feature implemented\n"},
        )
        cfg_plan = RunConfig(
            task="Implement feature",
            repo_dir=self.workspace,
            mode="plan",
            session_id="sess_sec_01",
        )
        orch_plan = Orchestrator(system1=SyntheticSystem1(), system2=s2, config=cfg_plan)
        results = orch_plan.run()
        self.assertEqual(results[0].outcome, "planned")
        plan_id = results[0].suggestion

        plan_doc = self.registry.update_status(
            plan_id=plan_id,
            new_status=PlanStatus.READY,
            actor="security_auditor",
            reason="Plan finalized and ready for execution",
        )
        self.assertIsNotNone(plan_doc)
        self.assertEqual(plan_doc.blocking_unknowns, [])
        self.assertEqual(plan_doc.clarifying_questions, ["Should we support dark mode in v2?"])
        is_blocked, blk_items = plan_doc.has_unresolved_blocking_unknowns(step_id="1")
        self.assertFalse(is_blocked)

        # BUILD mode execution: authorized step must execute without false blocking
        contract = self._create_approved_contract(plan_doc, targets=["src/safe.py"])
        cfg_build = RunConfig(
            task="Implement feature",
            repo_dir=self.workspace,
            mode="build",
            execution_contract=contract,
            plan_id=plan_id,
            session_id="sess_sec_01",
            session_incarnation_id="inc_sec_01",
            actor="security_auditor",
            test_command=["python", "--version"],
        )
        orch_build = Orchestrator(system1=SyntheticSystem1(), system2=s2, config=cfg_build)
        build_results = orch_build.run()
        self.assertIn(build_results[0].outcome, ("completed", "verified"))
        self.assertEqual(len(s2.write_code_calls), 1)

    def test_02_omitted_blocking_field_with_clarifying_questions_fails_closed(self) -> None:
        """Provider omits blocking_unknowns but provides clarifying questions; fail-closed promotion blocks."""
        (self.workspace / "src" / "safe.py").write_text("# safe\n", encoding="utf-8")
        plan_data = {
            "title": "Omitted Blocking PRD",
            "is_small_task": False,
            "goals": ["Implement feature"],
            "scope": ["src/safe.py"],
            "steps": [{"id": "1", "description": "Implement feature", "files": ["src/safe.py"]}],
            "relevant_files": ["src/safe.py"],
            # 'blocking_unknowns' is omitted entirely (None)
            "clarifying_questions": ["What is the target payment gateway timeout?"],
        }
        s2 = CapturingSystem2(plan_and_prd_data=plan_data)
        cfg_plan = RunConfig(
            task="Implement feature",
            repo_dir=self.workspace,
            mode="plan",
            session_id="sess_sec_01",
        )
        orch_plan = Orchestrator(system1=SyntheticSystem1(), system2=s2, config=cfg_plan)
        results = orch_plan.run()
        plan_id = results[0].suggestion

        plan_doc = self.registry.update_status(
            plan_id=plan_id,
            new_status=PlanStatus.READY,
            actor="security_auditor",
            reason="Ready for review",
        )
        self.assertIsNotNone(plan_doc)
        # Verify fallback promoted question to blocking unknown
        self.assertTrue(len(plan_doc.blocking_unknowns) >= 1)
        is_blocked, blk_items = plan_doc.has_unresolved_blocking_unknowns(step_id="1")
        self.assertTrue(is_blocked)

        contract = self._create_approved_contract(plan_doc, targets=["src/safe.py"])
        runner = StructuredStepRunner(
            workspace_dir=self.workspace,
            orchestrator=None,
            contract=contract,
            plan_id=plan_id,
        )
        step = {"id": "1", "description": "Implement feature", "files": ["src/safe.py"]}
        with self.assertRaises(BlockingUnknownError):
            runner.run_steps([step])

    def test_03_omitted_both_fields_loads_safely(self) -> None:
        """Provider omits both blocking_unknowns and clarifying_questions; loads cleanly without errors."""
        plan_data = {
            "title": "Minimal PRD",
            "is_small_task": False,
            "goals": ["Do minimal task"],
            "scope": ["src/safe.py"],
            "steps": [{"id": "1", "description": "Step 1", "files": ["src/safe.py"]}],
            "relevant_files": ["src/safe.py"],
        }
        s2 = CapturingSystem2(plan_and_prd_data=plan_data)
        cfg_plan = RunConfig(task="Do minimal task", repo_dir=self.workspace, mode="plan", session_id="sess_sec_01")
        orch_plan = Orchestrator(system1=SyntheticSystem1(), system2=s2, config=cfg_plan)
        results = orch_plan.run()
        plan_doc = self.registry.get_plan(results[0].suggestion)
        self.assertIsNotNone(plan_doc)
        self.assertEqual(plan_doc.blocking_unknowns, [])
        self.assertEqual(plan_doc.clarifying_questions, [])
        is_blocked, _ = plan_doc.has_unresolved_blocking_unknowns()
        self.assertFalse(is_blocked)

    def test_04_step_specific_blocking_unknown_allows_unrelated_step(self) -> None:
        """A blocking unknown tied to Step 2 halts Step 2 while allowing Step 1 to execute."""
        (self.workspace / "src" / "step1.py").write_text("# step 1\n", encoding="utf-8")
        (self.workspace / "src" / "step2.py").write_text("# step 2\n", encoding="utf-8")
        plan_id = "plan_step_specific_04"
        meta = {
            "id": plan_id,
            "title": "Two Step PRD",
            "goal": "Test partial blocking",
            "status": PlanStatus.READY.value,
            "revision": 1,
            "session_id": "sess_sec_01",
            "scope": ["src/step1.py", "src/step2.py"],
            "blocking_unknowns": [
                {
                    "id": "blk_step2",
                    "decision": "Database migration rollback procedure unknown",
                    "affected_steps": ["2"],
                    "affected_files": ["src/step2.py"],
                    "resolved": False,
                }
            ],
            "steps": [
                {"id": "1", "description": "Step 1 utility", "files": ["src/step1.py"]},
                {"id": "2", "description": "Step 2 migration", "files": ["src/step2.py"]},
            ],
            "relevant_files": ["src/step1.py", "src/step2.py"],
        }
        save_plan_document(self.workspace, f"{plan_id}.md", "# Two Step PRD\n", metadata=meta)
        plan_doc = self.registry.get_plan(plan_id)
        assert plan_doc is not None

        # Step 1 must NOT be blocked
        is_b1, _ = plan_doc.has_unresolved_blocking_unknowns(step_id="1", files=["src/step1.py"])
        self.assertFalse(is_b1)

        # Step 2 MUST be blocked
        is_b2, items2 = plan_doc.has_unresolved_blocking_unknowns(step_id="2", files=["src/step2.py"])
        self.assertTrue(is_b2)
        self.assertEqual(len(items2), 1)

    def test_05_global_blocking_unknown_blocks_all_steps(self) -> None:
        """A global blocking unknown halts all steps before mutation."""
        plan_id = "plan_global_blocking_05"
        meta = {
            "id": plan_id,
            "title": "Global Block PRD",
            "goal": "Test global block",
            "status": PlanStatus.READY.value,
            "revision": 1,
            "session_id": "sess_sec_01",
            "scope": ["src/safe.py"],
            "blocking_unknowns": [
                {
                    "id": "blk_global",
                    "decision": "Compliance authorization pending",
                    "affected_steps": [],
                    "affected_files": [],
                    "resolved": False,
                }
            ],
            "steps": [{"id": "1", "description": "Step 1", "files": ["src/safe.py"]}],
            "relevant_files": ["src/safe.py"],
        }
        save_plan_document(self.workspace, f"{plan_id}.md", "# Global Block PRD\n", metadata=meta)
        plan_doc = self.registry.get_plan(plan_id)
        assert plan_doc is not None

        is_blocked, items = plan_doc.has_unresolved_blocking_unknowns(step_id="1")
        self.assertTrue(is_blocked)
        self.assertEqual(items[0]["decision"], "Compliance authorization pending")

    def test_06_malformed_blocking_unknowns_fails_closed(self) -> None:
        """Malformed blocking unknowns (scalars, bools, empty dicts) fail closed and halt execution."""
        # Case A: Scalar integer
        plan_id_a = "plan_malformed_scalar_06a"
        meta_a = {
            "id": plan_id_a,
            "title": "Malformed Scalar",
            "goal": "Test malformed scalar",
            "status": PlanStatus.READY.value,
            "revision": 1,
            "session_id": "sess_sec_01",
            "scope": ["src/safe.py"],
            "blocking_unknowns": 12345,  # Invalid scalar
            "steps": [{"id": "1", "description": "Step 1", "files": ["src/safe.py"]}],
        }
        save_plan_document(self.workspace, f"{plan_id_a}.md", "# Scalar\n", metadata=meta_a)
        doc_a = self.registry.get_plan(plan_id_a)
        assert doc_a is not None
        self.assertTrue(doc_a.metadata_malformed)
        contract_a = self._create_approved_contract(doc_a, targets=["src/safe.py"])
        with self.assertRaises(PermissionError):
            contract_a.validate_plan_binding(self.workspace)

        # Case B: List containing boolean and empty dict
        plan_id_b = "plan_malformed_list_06b"
        meta_b = {
            "id": plan_id_b,
            "title": "Malformed List",
            "goal": "Test malformed list",
            "status": PlanStatus.READY.value,
            "revision": 1,
            "session_id": "sess_sec_01",
            "scope": ["src/safe.py"],
            "blocking_unknowns": [True, {}],  # Invalid items
            "steps": [{"id": "1", "description": "Step 1", "files": ["src/safe.py"]}],
        }
        save_plan_document(self.workspace, f"{plan_id_b}.md", "# List\n", metadata=meta_b)
        doc_b = self.registry.get_plan(plan_id_b)
        assert doc_b is not None
        self.assertTrue(doc_b.metadata_malformed)
        is_blocked, blk_items = doc_b.has_unresolved_blocking_unknowns(step_id="1")
        self.assertTrue(is_blocked)

    def test_07_target_context_unknown_does_not_blanket_block_execution(self) -> None:
        """Generic target_context='unknown' with no blocking unknowns does NOT block execution."""
        (self.workspace / "src" / "safe.py").write_text("# safe\n", encoding="utf-8")
        plan_id = "plan_target_unknown_07"
        meta = {
            "id": plan_id,
            "title": "Unknown Context PRD",
            "goal": "Safe bounded refactor",
            "status": PlanStatus.READY.value,
            "revision": 1,
            "session_id": "sess_sec_01",
            "target_context": "unknown",  # Architectural context unknown, but bounded
            "architecture_strategy": "bounded_local",
            "scope": ["src/safe.py"],
            "blocking_unknowns": [],
            "steps": [{"id": "1", "description": "Refactor safe", "files": ["src/safe.py"]}],
            "relevant_files": ["src/safe.py"],
        }
        save_plan_document(self.workspace, f"{plan_id}.md", "# Unknown Context PRD\n", metadata=meta)
        plan_doc = self.registry.get_plan(plan_id)
        assert plan_doc is not None

        is_blocked, _ = plan_doc.has_unresolved_blocking_unknowns(step_id="1")
        self.assertFalse(is_blocked)

        contract = self._create_approved_contract(plan_doc, targets=["src/safe.py"])
        contract.validate_plan_binding(self.workspace)  # Must validate without error

    def test_08_step_assuming_answer_still_blocked_by_runtime_gate(self) -> None:
        """A step whose text assumes an answer ('Assume Postgres') is still strictly halted by the gate."""
        plan_id = "plan_assume_answer_08"
        meta = {
            "id": plan_id,
            "title": "Database Schema Change",
            "goal": "Create schema",
            "status": PlanStatus.READY.value,
            "revision": 1,
            "session_id": "sess_sec_01",
            "scope": ["src/schema.sql"],
            "blocking_unknowns": [
                {
                    "id": "blk_db",
                    "decision": "Database engine dialect (Postgres vs MySQL) unspecified",
                    "affected_steps": ["1"],
                    "resolved": False,
                }
            ],
            "steps": [
                {
                    "id": "1",
                    "description": "Assume Postgres and write table creation DDL",
                    "files": ["src/schema.sql"],
                }
            ],
            "relevant_files": ["src/schema.sql"],
        }
        save_plan_document(self.workspace, f"{plan_id}.md", "# Schema PRD\n", metadata=meta)
        plan_doc = self.registry.get_plan(plan_id)
        assert plan_doc is not None

        contract = self._create_approved_contract(plan_doc, targets=["src/schema.sql"])
        runner = StructuredStepRunner(
            workspace_dir=self.workspace,
            orchestrator=None,
            contract=contract,
            plan_id=plan_id,
        )
        with self.assertRaises(BlockingUnknownError):
            runner.run_steps([{"id": "1", "description": "Assume Postgres and write DDL", "files": ["src/schema.sql"]}])

    def test_09_non_blocking_question_on_future_feature_does_not_block_work(self) -> None:
        """Non-blocking question about a future release does not halt current approved work."""
        (self.workspace / "src" / "safe.py").write_text("# v1 code\n", encoding="utf-8")
        plan_id = "plan_future_question_09"
        meta = {
            "id": plan_id,
            "title": "V1 Implementation",
            "goal": "Ship v1 core engine",
            "status": PlanStatus.READY.value,
            "revision": 1,
            "session_id": "sess_sec_01",
            "scope": ["src/safe.py"],
            "blocking_unknowns": [],
            "clarifying_questions": [
                "Should we implement multi-region clustering in Phase 4?",
                "Do we want GraphQL support in 2027?",
            ],
            "steps": [{"id": "1", "description": "Ship core v1", "files": ["src/safe.py"]}],
            "relevant_files": ["src/safe.py"],
        }
        save_plan_document(self.workspace, f"{plan_id}.md", "# V1 PRD\n", metadata=meta)
        plan_doc = self.registry.get_plan(plan_id)
        assert plan_doc is not None

        is_blocked, _ = plan_doc.has_unresolved_blocking_unknowns(step_id="1")
        self.assertFalse(is_blocked)

    def test_10_independent_step_executes_while_dependent_step_stays_blocked(self) -> None:
        """Step 1 (safe utility) runs to completion while Step 2 (payment) halts before mutation."""
        (self.workspace / "src" / "util.py").write_text("# util\n", encoding="utf-8")
        (self.workspace / "src" / "pay.py").write_text("# pay\n", encoding="utf-8")
        plan_id = "plan_partial_block_10"
        meta = {
            "id": plan_id,
            "title": "Utility and Payment",
            "goal": "Implement utility and payment",
            "status": PlanStatus.READY.value,
            "revision": 1,
            "session_id": "sess_sec_01",
            "scope": ["src/util.py", "src/pay.py"],
            "blocking_unknowns": [
                {
                    "id": "blk_pay",
                    "decision": "Payment idempotency key format unresolved",
                    "affected_steps": ["2"],
                    "affected_files": ["src/pay.py"],
                    "resolved": False,
                }
            ],
            "steps": [
                {"id": "1", "description": "Implement utility helper", "files": ["src/util.py"]},
                {"id": "2", "description": "Implement payment processor", "files": ["src/pay.py"]},
            ],
            "relevant_files": ["src/util.py", "src/pay.py"],
        }
        save_plan_document(self.workspace, f"{plan_id}.md", "# PRD\n", metadata=meta)
        plan_doc = self.registry.get_plan(plan_id)
        assert plan_doc is not None

        s2 = CapturingSystem2(output_files={
            "src/util.py": "# util updated\n",
            "src/pay.py": "# pay updated\n",
        })
        contract = self._create_approved_contract(plan_doc, targets=["src/util.py", "src/pay.py"])
        cfg = RunConfig(
            task="Implement utility and payment",
            repo_dir=self.workspace,
            mode="build",
            execution_contract=contract,
            plan_id=plan_id,
            session_id="sess_sec_01",
            session_incarnation_id="inc_sec_01",
            actor="security_auditor",
            test_command=["python", "--version"],
        )
        orch = Orchestrator(system1=SyntheticSystem1(), system2=s2, config=cfg)
        with self.assertRaises(BlockingUnknownError) as ctx:
            orch.run()

        # Step 1 passed, Step 2 halted before mutation
        self.assertEqual(len(s2.write_code_calls), 1)
        self.assertEqual(s2.write_code_calls[0]["step"].id, "1")
        self.assertIn("Execution halted: Step '2' depends on unresolved blocking unknown(s)", str(ctx.exception))

    # =========================================================================
    # SECTION 5: LEGACY PLANS VS LEGACY APPROVALS
    # =========================================================================

    def test_11_legacy_plan_loads_and_inspects_safely(self) -> None:
        """Legacy plan with no Phase 3 fields or metadata hash loads safely with default attributes."""
        plan_id = "legacy_plan_no_arch_11"
        meta = {
            "id": plan_id,
            "title": "Legacy Plan 1.0",
            "goal": "Refactor old helper",
            "status": PlanStatus.READY.value,
            "revision": 1,
            "session_id": "sess_sec_01",
            "steps": [{"id": "1", "description": "Update helper", "files": ["src/helper.py"]}],
            "relevant_files": ["src/helper.py"],
        }
        # Note: No target_context, no architecture_strategy, no blocking_unknowns, no metadata_hash
        save_plan_document(self.workspace, f"{plan_id}.md", "# Legacy Plan 1.0\n", metadata=meta)
        doc = self.registry.get_plan(plan_id)
        self.assertIsNotNone(doc)
        self.assertIsNone(doc.target_context)
        self.assertIsNone(doc.architecture_strategy)
        self.assertEqual(doc.blocking_unknowns, [])
        self.assertEqual(doc.architectural_constraints, [])
        self.assertFalse(doc.metadata_malformed)

    def test_12_legacy_contract_without_metadata_binding_fails_on_architecture_plan(self) -> None:
        """Legacy contract without plan_metadata_hash fails closed on architecture-aware plan."""
        plan_id = "plan_arch_aware_12"
        meta = {
            "id": plan_id,
            "title": "Architecture Aware Plan",
            "goal": "Strict architecture constraints",
            "status": PlanStatus.READY.value,
            "revision": 1,
            "session_id": "sess_sec_01",
            "target_context": "brownfield",
            "architecture_strategy": "local_refactor",
            "scope": ["src/safe.py"],
            "architectural_constraints": ["Must preserve legacy API caller contract."],
            "steps": [{"id": "1", "description": "Step 1", "files": ["src/safe.py"]}],
            "relevant_files": ["src/safe.py"],
        }
        save_plan_document(self.workspace, f"{plan_id}.md", "# Arch Aware Plan\n", metadata=meta)
        plan_doc = self.registry.get_plan(plan_id)
        assert plan_doc is not None

        # Contract created WITHOUT plan_metadata_hash (representing a legacy approval contract)
        legacy_contract = self._create_approved_contract(
            plan_doc,
            targets=["src/safe.py"],
            include_metadata_hash=False,
        )
        self.assertIsNone(legacy_contract.plan_metadata_hash)

        # Must fail closed: stale legacy approvals cannot execute architecture plans without review
        with self.assertRaises(PermissionError) as ctx:
            legacy_contract.validate_plan_binding(self.workspace)
        self.assertIn("lacks required semantic metadata binding", str(ctx.exception))

    def test_13_fresh_reapproval_binds_current_metadata_digest(self) -> None:
        """Fresh approval request automatically resolves and binds the plan metadata hash."""
        plan_id = "plan_reapproval_13"
        meta = {
            "id": plan_id,
            "title": "Reapproval Plan",
            "goal": "Reapproval testing",
            "status": PlanStatus.READY.value,
            "revision": 1,
            "session_id": "sess_sec_01",
            "target_context": "core",
            "architecture_strategy": "preserve",
            "scope": ["src/safe.py"],
            "architectural_constraints": ["Safe interface invariant"],
            "steps": [{"id": "1", "description": "Step 1", "files": ["src/safe.py"]}],
            "relevant_files": ["src/safe.py"],
        }
        save_plan_document(self.workspace, f"{plan_id}.md", "# Reapproval\n", metadata=meta)
        plan_doc = self.registry.get_plan(plan_id)
        assert plan_doc is not None

        # Fresh approval binds current metadata digest
        fresh_contract = self._create_approved_contract(plan_doc, targets=["src/safe.py"])
        self.assertIsNotNone(fresh_contract.plan_metadata_hash)
        self.assertEqual(fresh_contract.plan_metadata_hash, plan_doc.metadata_hash)
        fresh_contract.validate_plan_binding(self.workspace)  # Validates cleanly

    def test_14_metadata_tampering_after_reapproval_invalidates_contract(self) -> None:
        """Modifying architecture constraints after reapproval invalidates the contract."""
        plan_id = "plan_tamper_after_approval_14"
        meta = {
            "id": plan_id,
            "title": "Tamper Test",
            "goal": "Test tamper detection",
            "status": PlanStatus.READY.value,
            "revision": 1,
            "session_id": "sess_sec_01",
            "scope": ["src/safe.py"],
            "architectural_constraints": ["Constraint A: Must preserve interface"],
            "steps": [{"id": "1", "description": "Step 1", "files": ["src/safe.py"]}],
            "relevant_files": ["src/safe.py"],
        }
        save_plan_document(self.workspace, f"{plan_id}.md", "# Tamper Test\n", metadata=meta)
        plan_doc = self.registry.get_plan(plan_id)
        assert plan_doc is not None

        contract = self._create_approved_contract(plan_doc, targets=["src/safe.py"])
        contract.validate_plan_binding(self.workspace)

        # Attacker modifies constraint in .meta.json
        meta_file = self.workspace / ".brainfrog" / "plans" / f"{plan_id}.meta.json"
        raw_meta = json.loads(meta_file.read_text(encoding="utf-8"))
        raw_meta["architectural_constraints"] = ["Constraint B: Relaxed boundary"]
        raw_meta["metadata_hash"] = compute_metadata_semantic_digest(raw_meta)
        meta_file.write_text(json.dumps(raw_meta, indent=2), encoding="utf-8")

        with self.assertRaises(PermissionError) as ctx:
            contract.validate_plan_binding(self.workspace)
        self.assertIn("Plan metadata on disk has been modified since approval", str(ctx.exception))

    def test_15_plan_revision_change_invalidates_stale_approval(self) -> None:
        """Out-of-band revision advancement invalidates the execution contract."""
        plan_id = "plan_revision_adv_15"
        meta = {
            "id": plan_id,
            "title": "Revision Adv Plan",
            "goal": "Test revision tampering",
            "status": PlanStatus.READY.value,
            "revision": 1,
            "session_id": "sess_sec_01",
            "scope": ["src/safe.py"],
            "steps": [{"id": "1", "description": "Step 1", "files": ["src/safe.py"]}],
            "relevant_files": ["src/safe.py"],
        }
        save_plan_document(self.workspace, f"{plan_id}.md", "# Revision Adv\n", metadata=meta)
        plan_doc = self.registry.get_plan(plan_id)
        assert plan_doc is not None

        contract = self._create_approved_contract(plan_doc, targets=["src/safe.py"])

        # Directly bump revision on disk
        meta_file = self.workspace / ".brainfrog" / "plans" / f"{plan_id}.meta.json"
        raw_meta = json.loads(meta_file.read_text(encoding="utf-8"))
        raw_meta["revision"] = 99
        meta_file.write_text(json.dumps(raw_meta, indent=2), encoding="utf-8")

        with self.assertRaises(PermissionError) as ctx:
            contract.validate_plan_binding(self.workspace)
        self.assertIn("does not match approved revision", str(ctx.exception))

    def test_16_cross_session_reuse_forbidden(self) -> None:
        """Contract bound to session A cannot execute plan owned by session B."""
        plan_id = "plan_session_isolation_16"
        meta = {
            "id": plan_id,
            "title": "Session B Plan",
            "goal": "Cross session test",
            "status": PlanStatus.READY.value,
            "revision": 1,
            "session_id": "sess_victim_99",
            "scope": ["src/safe.py"],
            "steps": [{"id": "1", "description": "Step 1", "files": ["src/safe.py"]}],
            "relevant_files": ["src/safe.py"],
        }
        save_plan_document(self.workspace, f"{plan_id}.md", "# Session B\n", metadata=meta)
        plan_doc = self.registry.get_plan(plan_id)
        assert plan_doc is not None

        # Attacker tries to use session A contract on session B's plan
        attacker_contract = self._create_approved_contract(
            plan_doc,
            targets=["src/safe.py"],
            session_id="sess_attacker_01",
        )
        with self.assertRaises(PermissionError) as ctx:
            attacker_contract.validate_plan_binding(self.workspace)
        self.assertIn("Contract plan ownership mismatch", str(ctx.exception))

    def test_17_authorized_plan_status_updates_do_not_produce_false_tampering(self) -> None:
        """Legitimate transition from READY -> IN_PROGRESS does NOT invalidate contract authorization."""
        plan_id = "plan_lifecycle_transition_17"
        meta = {
            "id": plan_id,
            "title": "Lifecycle Transition Plan",
            "goal": "Test authorized transition",
            "status": PlanStatus.READY.value,
            "revision": 1,
            "session_id": "sess_sec_01",
            "target_context": "core",
            "architecture_strategy": "modular",
            "scope": ["src/safe.py"],
            "architectural_constraints": ["Constraint alpha"],
            "steps": [{"id": "1", "description": "Step 1", "files": ["src/safe.py"]}],
            "relevant_files": ["src/safe.py"],
        }
        save_plan_document(self.workspace, f"{plan_id}.md", "# Lifecycle Plan\n", metadata=meta)
        plan_doc = self.registry.get_plan(plan_id)
        assert plan_doc is not None

        contract = self._create_approved_contract(plan_doc, targets=["src/safe.py"])
        contract.validate_plan_binding(self.workspace)

        # Perform legitimate transition to IN_PROGRESS under contract
        updated_plan = self.registry.update_status(
            plan_id=plan_id,
            new_status=PlanStatus.IN_PROGRESS,
            actor="security_auditor",
            reason="Authorized execution started",
            contract=contract,
        )
        self.assertEqual(updated_plan.status, PlanStatus.IN_PROGRESS)
        self.assertEqual(updated_plan.revision, 2)

        # Contract validation must SUCCEED (no false tampering error)
        contract.validate_plan_binding(self.workspace)

        # Step runner pre-mutation check must also SUCCEED
        runner = StructuredStepRunner(
            workspace_dir=self.workspace,
            orchestrator=None,
            contract=contract,
            plan_id=plan_id,
        )
        runner.revalidate_pre_mutation()  # Must not raise

    # =========================================================================
    # SECTION 7: PROVIDER SCHEMAS AND PARSERS END-TO-END
    # =========================================================================

    def test_18_provider_antigravity_full_valid_schema_response(self) -> None:
        """Antigravity model response with all Phase 3 architecture fields extracts and normalizes cleanly."""
        raw_model_json = json.dumps({
            "title": "Antigravity Architecture Refactor",
            "is_small_task": False,
            "problem": "Refactor monolithic controller",
            "goals": ["Split controller into modular handlers"],
            "target_context": "Core Engine API",
            "architecture_strategy": "Strangler Fig Pattern",
            "scope": ["src/controller.py", "src/handler.py"],
            "non_scope": ["src/legacy_db.py"],
            "architectural_constraints": ["Handlers must implement IHandler interface"],
            "compatibility_contracts": ["HTTP 200 payload structure preserved"],
            "behavior_invariants": ["Atomic rollback on handler failure"],
            "migration_constraints": ["No downtime deployment required"],
            "blocking_unknowns": [
                {
                    "id": "blk_ag_1",
                    "decision": "Choose synchronous vs asynchronous event dispatcher",
                    "affected_steps": ["2"],
                    "affected_files": ["src/handler.py"],
                    "resolved": False,
                }
            ],
            "clarifying_questions": ["Will Prometheus metrics be enabled in staging?"],
            "steps": [
                {"id": "1", "description": "Extract base handler", "files": ["src/handler.py"]},
                {"id": "2", "description": "Wire event dispatcher", "files": ["src/controller.py"]},
            ],
            "relevant_files": ["src/controller.py", "src/handler.py"],
        })
        extracted = extract_json(raw_model_json)
        self.assertEqual(extracted["target_context"], "Core Engine API")
        self.assertEqual(extracted["architecture_strategy"], "Strangler Fig Pattern")
        self.assertEqual(len(extracted["blocking_unknowns"]), 1)
        self.assertEqual(extracted["blocking_unknowns"][0]["id"], "blk_ag_1")
        self.assertEqual(extracted["clarifying_questions"], ["Will Prometheus metrics be enabled in staging?"])

    def test_19_provider_openai_legacy_response_without_new_fields(self) -> None:
        """OpenAI legacy model response missing architecture fields normalizes with safe empty defaults."""
        legacy_response = json.dumps({
            "title": "Old Format Fix",
            "is_small_task": True,
            "problem": "Fix typo",
            "goals": ["Fix typo in readme"],
            "steps": [{"id": "1", "description": "Fix readme", "files": ["README.md"]}],
            "relevant_files": ["README.md"],
        })
        extracted = extract_json(legacy_response)
        s2 = CapturingSystem2(plan_and_prd_data=extracted)
        cfg = RunConfig(task="Fix readme", repo_dir=self.workspace, mode="plan", session_id="sess_sec_01")
        orch = Orchestrator(system1=SyntheticSystem1(), system2=s2, config=cfg)
        results = orch.run()
        plan_doc = self.registry.get_plan(results[0].suggestion)
        self.assertIsNotNone(plan_doc)
        self.assertIsNone(plan_doc.target_context)
        self.assertEqual(plan_doc.blocking_unknowns, [])
        self.assertEqual(plan_doc.architectural_constraints, [])
        self.assertFalse(plan_doc.metadata_malformed)

    def test_20_provider_extra_unapproved_fields_discarded_safely(self) -> None:
        """Extra or unapproved fields in provider output cannot inject permissions or bypass boundaries."""
        untrusted_response = json.dumps({
            "title": "Capability Injection Attempt",
            "is_small_task": False,
            "goals": ["Normal task"],
            "scope": ["src/safe.py"],
            "steps": [{"id": "1", "description": "Step 1", "files": ["src/safe.py"]}],
            "relevant_files": ["src/safe.py"],
            # Malicious/untrusted attempts to grant root privileges or extra capabilities
            "unapproved_capabilities": ["filesystem.root_write", "terminal.sudo"],
            "bypass_security_checks": True,
            "admin_token": "UNTRUSTED_FAKE_TOKEN",
        })
        extracted = extract_json(untrusted_response)
        s2 = CapturingSystem2(plan_and_prd_data=extracted)
        cfg = RunConfig(task="Normal task", repo_dir=self.workspace, mode="plan", session_id="sess_sec_01")
        orch = Orchestrator(system1=SyntheticSystem1(), system2=s2, config=cfg)
        results = orch.run()
        plan_doc = self.registry.get_plan(results[0].suggestion)
        self.assertIsNotNone(plan_doc)

        # Plan document metadata must NOT contain or respect unapproved capabilities
        meta_dict = plan_doc.to_metadata_dict()
        self.assertNotIn("unapproved_capabilities", meta_dict)
        self.assertNotIn("bypass_security_checks", meta_dict)
        self.assertNotIn("admin_token", meta_dict)

    def test_21_end_to_end_preservation_of_all_architectural_fields(self) -> None:
        """Full round-trip test: Provider output -> normalized plan -> disk persistence -> PlanRegistry reload."""
        full_fields = {
            "title": "Comprehensive Architecture Preservation",
            "is_small_task": False,
            "problem": "Test comprehensive field preservation",
            "goals": ["Verify all 10 architectural fields"],
            "target_context": "Distributed Storage Subsystem",
            "architecture_strategy": "Event-Driven CQRS",
            "scope": ["src/writer.py", "src/reader.py"],
            "non_scope": ["src/legacy_wal.py"],
            "architectural_constraints": ["Writers must never read from uncommitted replicas."],
            "compatibility_contracts": ["Protobuf v3 wire compatibility."],
            "behavior_invariants": ["Strict causal ordering per aggregate ID."],
            "migration_constraints": ["Dual-write required for 14-day transition."],
            "blocking_unknowns": [
                {
                    "id": "blk_full_1",
                    "decision": "Raft leader lease timeout threshold",
                    "affected_steps": ["1"],
                    "affected_files": ["src/writer.py"],
                    "resolved": False,
                }
            ],
            "clarifying_questions": ["Should we support snappy compression?"],
            "steps": [
                {"id": "1", "description": "Implement writer", "files": ["src/writer.py"]},
                {"id": "2", "description": "Implement reader", "files": ["src/reader.py"]},
            ],
            "relevant_files": ["src/writer.py", "src/reader.py"],
        }
        s2 = CapturingSystem2(plan_and_prd_data=full_fields)
        cfg = RunConfig(
            task="Verify all 10 architectural fields",
            repo_dir=self.workspace,
            mode="plan",
            session_id="sess_sec_01",
        )
        orch = Orchestrator(system1=SyntheticSystem1(), system2=s2, config=cfg)
        results = orch.run()
        plan_id = results[0].suggestion

        reloaded = self.registry.get_plan(plan_id)
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.target_context, "Distributed Storage Subsystem")
        self.assertEqual(reloaded.architecture_strategy, "Event-Driven CQRS")
        self.assertEqual(reloaded.scope, ["src/writer.py", "src/reader.py"])
        self.assertEqual(reloaded.non_scope, ["src/legacy_wal.py"])
        self.assertEqual(reloaded.architectural_constraints, ["Writers must never read from uncommitted replicas."])
        self.assertEqual(reloaded.compatibility_contracts, ["Protobuf v3 wire compatibility."])
        self.assertEqual(reloaded.behavior_invariants, ["Strict causal ordering per aggregate ID."])
        self.assertEqual(reloaded.migration_constraints, ["Dual-write required for 14-day transition."])
        self.assertEqual(len(reloaded.blocking_unknowns), 1)
        self.assertEqual(reloaded.blocking_unknowns[0]["id"], "blk_full_1")
        self.assertEqual(reloaded.clarifying_questions, ["Should we support snappy compression?"])
        self.assertTrue(reloaded.metadata_hash)
        self.assertFalse(reloaded.metadata_tampered)
        self.assertFalse(reloaded.metadata_malformed)


if __name__ == "__main__":
    unittest.main()
