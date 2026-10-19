"""Comprehensive Integration Tests for Phase 3: Architecture-Aware PLAN -> BUILD Handoff.

Enforces the 12 core handoff and security invariants:
1. An architecture constraint survives PLAN creation.
2. The same constraint survives serialization and reload.
3. The correct constraint appears in the approved BUILD step context.
4. An unrelated step does not receive unnecessary architecture context.
5. Old plans without architecture fields still load (backward compatibility).
6. A blocking unknown prevents only affected mutation steps.
7. Non-blocking unknowns do not halt unrelated authorized work.
8. A planned rewrite does not grant additional file permissions.
9. PLAN mode remains read-only.
10. Approved execution contract scope remains unchanged.
11. A modified plan or step cannot inherit stale authorization after resume.
12. Cancellation and failed-step handling remain safe.

Also includes representative synthetic scenarios:
- Greenfield implementation with minimal constraints.
- Brownfield API refactor requiring backward compatibility.
- Mixed-context provider addition with interface invariants.
- Bounded rewrite preserving external contracts.
- Stateful incremental data migration with rollback constraints.
- Unknown business rules requiring clarification.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.plans import (
    ALLOWED_PLAN_TRANSITIONS,
    BlockingUnknownError,
    InvalidPlanTransitionError,
    PlanDocument,
    PlanRegistry,
    PlanStatus,
    StalePlanStateError,
    format_plan_handoff,
    format_step_architecture_context,
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
from core.runtime.step_runner import (
    StepExecutionStatus,
    StructuredStepRunner,
    _CURRENT_CANCEL_EVENT,
)
from orchestrator import (
    Orchestrator,
    PlanStep,
    RunConfig,
    StepResult,
)
from system1.base import Answer, SystemOneClient


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
    """System 2 test double that records prompts and arguments received."""

    def __init__(
        self,
        output_files: Optional[Dict[str, str]] = None,
        plan_and_prd_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.guidelines = ""
        self.provider_name = "mock_s2"
        self.model = "mock-model"
        self.output_files = output_files or {"src/safe.py": "# synthetic implementation"}
        self.plan_and_prd_data = plan_and_prd_data or {}
        self.write_code_calls: List[Dict[str, Any]] = []

    def plan_and_prd(self, task: str, repo_tree: str, focus_files: Dict[str, str], **kwargs: Any) -> Dict[str, Any]:
        if self.plan_and_prd_data:
            return dict(self.plan_and_prd_data)
        return {
            "title": "Default Synthetic Plan",
            "is_small_task": False,
            "problem": "Synthetic problem",
            "goals": [task],
            "scope": ["src/safe.py"],
            "non_scope": ["src/forbidden.py"],
            "target_context": "brownfield",
            "architecture_strategy": "local_refactor",
            "architectural_constraints": ["Synthetic constraint"],
            "compatibility_contracts": ["Synthetic contract"],
            "behavior_invariants": ["Synthetic invariant"],
            "migration_constraints": ["Synthetic migration rule"],
            "blocking_unknowns": [],
            "clarifying_questions": [],
            "acceptance_criteria": ["Synthetic criterion 1"],
            "steps": [{"id": "1", "description": "Execute synthetic step", "files": ["src/safe.py"]}],
            "relevant_files": ["src/safe.py"],
            "markdown_doc": "# Default Synthetic Plan\n",
        }

    def plan_task(self, task: str, focus_tree: str = "", **kwargs: Any) -> List[PlanStep]:
        return [PlanStep(id="1", description="Execute plan step", files=list(self.output_files.keys()))]

    def write_code(self, step: Any, task: str, file_contents: Dict[str, str], **kwargs: Any) -> Dict[str, str]:
        self.write_code_calls.append({
            "step": step,
            "task": task,
            "file_contents": file_contents,
        })
        return dict(self.output_files)

    def draft_pr(self, task: str, files_changed: List[str], test_summary: str = "", **kwargs: Any) -> Dict[str, str]:
        return {"title": f"feat: {task}", "body": "Synthetic PR"}


# -----------------------------------------------------------------------------
# Base Test Setup
# -----------------------------------------------------------------------------
class ArchitecturePlanBuildHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="brainfrog_phase3_")
        self.workspace = Path(self.test_dir).resolve()
        (self.workspace / ".brainfrog" / "plans").mkdir(parents=True, exist_ok=True)
        (self.workspace / "api").mkdir(parents=True, exist_ok=True)
        (self.workspace / "docs").mkdir(parents=True, exist_ok=True)
        (self.workspace / "src").mkdir(parents=True, exist_ok=True)

        # Initialize synthetic git repository
        try:
            subprocess.run(["git", "init"], cwd=self.workspace, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "TestRunner"], cwd=self.workspace, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@synthetic.local"], cwd=self.workspace, check=True, capture_output=True)
            init_file = self.workspace / "README.md"
            init_file.write_text("# Synthetic Workspace\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=self.workspace, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=self.workspace, check=True, capture_output=True)
        except Exception:
            pass

        self.registry = PlanRegistry(self.workspace)

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_approved_contract(
        self,
        plan_doc: Optional[PlanDocument],
        targets: Optional[List[str]] = None,
        session_id: str = "sess_canonical_1",
        session_incarnation: str = "inc_canonical_1",
    ) -> ApprovedExecutionContract:
        assert plan_doc is not None, "plan_doc must be provided"
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
        app_req = app_service.create_request(
            session_id=session_id,
            channel="cli",
            user_id="local_user",
            conversation_id="conv_default",
            operation_type="write_files",
            canonical_operation=op,
            risk_class=RiskClass.MEDIUM.value,
            session_incarnation_id=session_incarnation,
            capabilities=caps,
            workspace_root=str(self.workspace),
            plan_id=plan_doc.id,
            plan_revision=plan_doc.revision,
            plan_content_hash=plan_doc.content_hash,
        )
        app_req.status = ApprovalStatus.CONSUMED
        app_req.approver_id = "security_lead"
        app_req.approved_at = time.time()
        app_req.consumed_at = time.time()
        app_req.approval_digest = app_req.compute_approval_digest()
        return ApprovedExecutionContract.from_approval_request(app_req, repo_dir=self.workspace)

    # -------------------------------------------------------------------------
    # Invariant 1: Architecture constraint survives PLAN creation
    # -------------------------------------------------------------------------
    def test_01_architecture_constraint_survives_plan_creation(self) -> None:
        """Architecture decisions and constraints survive PLAN creation via System 2."""
        plan_data = {
            "title": "Payment API Hardening",
            "is_small_task": False,
            "problem": "Payment signature compatibility must be preserved",
            "goals": ["Refactor payment client without breaking API v1"],
            "target_context": "brownfield",
            "architecture_strategy": "local_refactor",
            "scope": ["api/payment.py"],
            "non_scope": ["database/schema.sql"],
            "architectural_constraints": ["Payment API in api/payment.py must retain its public signature."],
            "compatibility_contracts": ["API v1 contract must be 100% backward compatible."],
            "behavior_invariants": ["Payment transactions cannot be dropped without an audit entry."],
            "migration_constraints": ["No database migration required in this phase."],
            "blocking_unknowns": [],
            "clarifying_questions": ["Is legacy HMAC authentication still required?"],
            "acceptance_criteria": ["Public payment method retains exact signature and return type."],
            "steps": [{"id": "1", "description": "Refactor payment handler", "files": ["api/payment.py"]}],
            "relevant_files": ["api/payment.py"],
        }
        s2 = CapturingSystem2(plan_and_prd_data=plan_data)
        cfg = RunConfig(
            task="Refactor payment client without breaking API v1",
            repo_dir=self.workspace,
            mode="plan",
            session_id="sess_canonical_1",
        )
        orch = Orchestrator(system1=SyntheticSystem1(), system2=s2, config=cfg)
        results = orch.run()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].outcome, "planned")
        plan_id = results[0].suggestion
        self.assertTrue(plan_id)

        # Verify plan on disk
        plan_doc = self.registry.get_plan(plan_id)
        self.assertIsNotNone(plan_doc)
        self.assertEqual(plan_doc.target_context, "brownfield")
        self.assertEqual(plan_doc.architecture_strategy, "local_refactor")
        self.assertEqual(plan_doc.scope, ["api/payment.py"])
        self.assertEqual(plan_doc.non_scope, ["database/schema.sql"])
        self.assertIn("Payment API in api/payment.py must retain its public signature.", plan_doc.architectural_constraints)
        self.assertIn("API v1 contract must be 100% backward compatible.", plan_doc.compatibility_contracts)
        self.assertIn("Payment transactions cannot be dropped without an audit entry.", plan_doc.behavior_invariants)
        self.assertIn("No database migration required in this phase.", plan_doc.migration_constraints)

    # -------------------------------------------------------------------------
    # Invariant 2: Constraints survive serialization and reload
    # -------------------------------------------------------------------------
    def test_02_constraint_survives_serialization_and_reload(self) -> None:
        """Architecture fields survive persistence to disk and reload via PlanRegistry."""
        plan_id = "plan_arch_serialize_01"
        meta = {
            "id": plan_id,
            "title": "Provider Addition",
            "goal": "Add secondary payment provider",
            "status": PlanStatus.READY.value,
            "revision": 1,
            "session_id": "sess_canonical_1",
            "target_context": "mixed",
            "architecture_strategy": "keep",
            "scope": ["src/provider.py"],
            "non_scope": ["src/core.py"],
            "architectural_constraints": ["Provider must implement BaseProvider interface."],
            "compatibility_contracts": ["Must not alter BaseProvider method signatures."],
            "behavior_invariants": ["Provider failure must raise ProviderUnavailableError."],
            "migration_constraints": [],
            "blocking_unknowns": [],
            "clarifying_questions": [],
            "acceptance_criteria": ["New provider passes interface conformance test."],
            "steps": [{"id": "1", "description": "Implement secondary provider", "files": ["src/provider.py"]}],
            "relevant_files": ["src/provider.py"],
        }
        content = "# Provider Addition\n\n- **Goal**: Add secondary payment provider\n"
        saved_path = save_plan_document(self.workspace, f"{plan_id}.md", content, metadata=meta)
        self.assertTrue(saved_path.exists())

        loaded_plan = self.registry.get_plan(plan_id)
        self.assertIsNotNone(loaded_plan)
        self.assertEqual(loaded_plan.target_context, "mixed")
        self.assertEqual(loaded_plan.architecture_strategy, "keep")
        self.assertEqual(loaded_plan.scope, ["src/provider.py"])
        self.assertEqual(loaded_plan.non_scope, ["src/core.py"])
        self.assertEqual(loaded_plan.architectural_constraints, ["Provider must implement BaseProvider interface."])
        self.assertEqual(loaded_plan.compatibility_contracts, ["Must not alter BaseProvider method signatures."])
        self.assertEqual(loaded_plan.behavior_invariants, ["Provider failure must raise ProviderUnavailableError."])

    # -------------------------------------------------------------------------
    # Invariant 3: Correct constraint appears in approved BUILD step context
    # -------------------------------------------------------------------------
    def test_03_correct_constraint_appears_in_approved_build_step_context(self) -> None:
        """A step modifying api/payment.py receives relevant payment constraints in System 2 context."""
        (self.workspace / "api" / "payment.py").write_text("# Existing payment implementation\n", encoding="utf-8")
        plan_id = "plan_arch_context_01"
        meta = {
            "id": plan_id,
            "title": "Payment Signature Hardening",
            "goal": "Preserve payment signature",
            "status": PlanStatus.READY.value,
            "revision": 1,
            "session_id": "sess_canonical_1",
            "target_context": "brownfield",
            "architecture_strategy": "local_refactor",
            "scope": ["api/payment.py"],
            "non_scope": ["database/schema.sql"],
            "architectural_constraints": ["Payment API in api/payment.py must retain its public signature."],
            "behavior_invariants": ["Persisted payment records cannot be dropped."],
            "compatibility_contracts": ["API v1 endpoint compatibility must be preserved."],
            "migration_constraints": [],
            "steps": [{"id": "1", "description": "Refactor payment", "files": ["api/payment.py"]}],
            "relevant_files": ["api/payment.py"],
        }
        content = "# Payment Signature Hardening\n"
        save_plan_document(self.workspace, f"{plan_id}.md", content, metadata=meta)
        plan_doc = self.registry.get_plan(plan_id)

        contract = self._create_approved_contract(plan_doc, targets=["api/payment.py"])
        s2 = CapturingSystem2(output_files={"api/payment.py": "# Updated payment implementation\n"})

        cfg = RunConfig(
            task="Refactor payment",
            repo_dir=self.workspace,
            mode="build",
            execution_contract=contract,
            plan_id=plan_id,
            session_id="sess_canonical_1",
            actor="local_user",
            session_incarnation_id="inc_canonical_1",
            test_command=["python", "--version"],
        )
        orch = Orchestrator(system1=SyntheticSystem1(), system2=s2, config=cfg)
        results = orch.run()

        self.assertEqual(len(results), 1)
        self.assertIn(results[0].outcome, ("completed", "verified"))
        self.assertEqual(len(s2.write_code_calls), 1)

        task_prompt = s2.write_code_calls[0]["task"]
        self.assertIn("[Architectural Constraints & Boundaries]", task_prompt)
        self.assertIn("Target Context: brownfield | Strategy: local_refactor", task_prompt)
        self.assertIn("Payment API in api/payment.py must retain its public signature.", task_prompt)
        self.assertIn("Persisted payment records cannot be dropped.", task_prompt)
        self.assertIn("database/schema.sql", task_prompt)

    # -------------------------------------------------------------------------
    # Invariant 4: Unrelated step does NOT receive unnecessary architecture context
    # -------------------------------------------------------------------------
    def test_04_unrelated_step_does_not_receive_unnecessary_architecture_context(self) -> None:
        """A step modifying docs/readme.md does not receive file-specific payment constraints."""
        (self.workspace / "api" / "payment.py").write_text("# Existing payment\n", encoding="utf-8")
        (self.workspace / "docs" / "readme.md").write_text("# Existing docs\n", encoding="utf-8")

        plan_id = "plan_arch_unrelated_01"
        meta = {
            "id": plan_id,
            "title": "Dual-Step Maintenance",
            "goal": "Refactor payment and update documentation",
            "status": PlanStatus.READY.value,
            "revision": 1,
            "session_id": "sess_canonical_1",
            "target_context": "brownfield",
            "architecture_strategy": "local_refactor",
            "scope": ["api/payment.py", "docs/readme.md"],
            "non_scope": ["database/schema.sql"],
            "architectural_constraints": [
                {"text": "Payment API in api/payment.py must retain its public signature.", "files": ["api/payment.py"]}
            ],
            "behavior_invariants": ["Persisted payment records cannot be dropped."],
            "compatibility_contracts": [],
            "migration_constraints": [],
            "steps": [
                {"id": "1", "description": "Refactor payment", "files": ["api/payment.py"]},
                {"id": "2", "description": "Update readme", "files": ["docs/readme.md"]},
            ],
            "relevant_files": ["api/payment.py", "docs/readme.md"],
        }
        content = "# Dual-Step Maintenance\n"
        save_plan_document(self.workspace, f"{plan_id}.md", content, metadata=meta)
        plan_doc = self.registry.get_plan(plan_id)

        # Execute only step 2 (docs/readme.md)
        contract = self._create_approved_contract(plan_doc, targets=["docs/readme.md"])
        s2 = CapturingSystem2(output_files={"docs/readme.md": "# Updated documentation\n"})

        cfg = RunConfig(
            task="Update readme",
            repo_dir=self.workspace,
            mode="build",
            execution_contract=contract,
            plan_id=plan_id,
            session_id="sess_canonical_1",
            actor="local_user",
            session_incarnation_id="inc_canonical_1",
            test_command=["python", "--version"],
        )
        orch = Orchestrator(system1=SyntheticSystem1(), system2=s2, config=cfg)
        step2 = PlanStep("2", "Update readme", ["docs/readme.md"])
        orch._run_step(step2)

        self.assertEqual(len(s2.write_code_calls), 1)
        task_prompt = s2.write_code_calls[0]["task"]
        # Payment signature constraint MUST NOT appear in the documentation step context
        self.assertNotIn("Payment API in api/payment.py must retain its public signature.", task_prompt)

    # -------------------------------------------------------------------------
    # Invariant 5: Old plans without architecture fields still load
    # -------------------------------------------------------------------------
    def test_05_old_plans_without_architecture_fields_still_load(self) -> None:
        """Legacy plans lacking Phase 3 fields load without error and return safe defaults."""
        plan_id = "plan_legacy_phase2_01"
        legacy_meta = {
            "id": plan_id,
            "title": "Legacy Implementation Plan",
            "goal": "Old plan without architecture fields",
            "status": PlanStatus.READY.value,
            "revision": 1,
            "session_id": "sess_canonical_1",
            "steps": [{"id": "1", "description": "Legacy step", "files": ["src/safe.py"]}],
            "relevant_files": ["src/safe.py"],
            "acceptance_criteria": ["Legacy criterion"],
            "assumptions": ["Legacy assumption"],
            "verification_commands": ["python --version"],
        }
        content = "# Legacy Plan\n- **Goal**: Old plan without architecture fields\n"
        save_plan_document(self.workspace, f"{plan_id}.md", content, metadata=legacy_meta)

        loaded_plan = self.registry.get_plan(plan_id)
        self.assertIsNotNone(loaded_plan)
        self.assertIsNone(loaded_plan.target_context)
        self.assertIsNone(loaded_plan.architecture_strategy)
        self.assertEqual(loaded_plan.scope, [])
        self.assertEqual(loaded_plan.non_scope, [])
        self.assertEqual(loaded_plan.blocking_unknowns, [])
        self.assertEqual(loaded_plan.architectural_constraints, [])
        self.assertEqual(loaded_plan.compatibility_contracts, [])
        self.assertEqual(loaded_plan.behavior_invariants, [])
        self.assertEqual(loaded_plan.migration_constraints, [])

        # Check has_unresolved_blocking_unknowns on legacy plan
        is_blocked, blk_items = loaded_plan.has_unresolved_blocking_unknowns()
        self.assertFalse(is_blocked)
        self.assertEqual(blk_items, [])

        # Check format_plan_handoff works cleanly on legacy plan
        formatted = format_plan_handoff(loaded_plan, self.workspace)
        self.assertIn("Legacy Implementation Plan", formatted)
        self.assertIn("Old plan without architecture fields", formatted)

    # -------------------------------------------------------------------------
    # Invariant 6: Blocking unknown prevents affected mutation steps
    # -------------------------------------------------------------------------
    def test_06_blocking_unknown_prevents_affected_mutation_steps(self) -> None:
        """A blocking unknown halts affected mutation step without running write_code or mutating files."""
        (self.workspace / "api" / "auth.py").write_text("# Initial auth\n", encoding="utf-8")
        (self.workspace / "api" / "gateway.py").write_text("# Initial gateway\n", encoding="utf-8")

        plan_id = "plan_blocking_unknown_01"
        meta = {
            "id": plan_id,
            "title": "Gateway Integration",
            "goal": "Integrate third-party gateway",
            "status": PlanStatus.READY.value,
            "revision": 1,
            "session_id": "sess_canonical_1",
            "target_context": "brownfield",
            "architecture_strategy": "incremental_migration",
            "scope": ["api/auth.py", "api/gateway.py"],
            "non_scope": [],
            "blocking_unknowns": [
                {
                    "id": "blk_gw_timeout",
                    "decision": "Gateway timeout threshold and retry backoff policy",
                    "question": "What is the external gateway SLA and retry limit?",
                    "affected_steps": ["2"],
                    "affected_files": ["api/gateway.py"],
                    "resolved": False,
                }
            ],
            "steps": [
                {"id": "1", "description": "Configure auth credentials", "files": ["api/auth.py"]},
                {"id": "2", "description": "Implement gateway client", "files": ["api/gateway.py"]},
            ],
            "relevant_files": ["api/auth.py", "api/gateway.py"],
        }
        content = "# Gateway Integration\n"
        save_plan_document(self.workspace, f"{plan_id}.md", content, metadata=meta)
        plan_doc = self.registry.get_plan(plan_id)

        # Step 1: Unaffected safe step proceeds cleanly
        contract1 = self._create_approved_contract(plan_doc, targets=["api/auth.py"])
        s2 = CapturingSystem2(output_files={"api/auth.py": "# Auth configured\n"})
        cfg1 = RunConfig(
            task="Configure auth",
            repo_dir=self.workspace,
            mode="build",
            execution_contract=contract1,
            plan_id=plan_id,
            session_id="sess_canonical_1",
            actor="local_user",
            session_incarnation_id="inc_canonical_1",
            test_command=["python", "--version"],
        )
        orch1 = Orchestrator(system1=SyntheticSystem1(), system2=s2, config=cfg1)
        step1 = PlanStep("1", "Configure auth credentials", ["api/auth.py"])
        res1 = orch1._run_step(step1)
        self.assertIn(res1.outcome, ("completed", "verified"))
        self.assertIn("Auth configured", (self.workspace / "api" / "auth.py").read_text(encoding="utf-8"))

        # Step 2: Affected by unresolved blocking unknown must fail closed
        contract2 = self._create_approved_contract(plan_doc, targets=["api/gateway.py"])
        s2_step2 = CapturingSystem2(output_files={"api/gateway.py": "# Speculative implementation\n"})
        cfg2 = RunConfig(
            task="Implement gateway",
            repo_dir=self.workspace,
            mode="build",
            execution_contract=contract2,
            plan_id=plan_id,
            session_id="sess_canonical_1",
            actor="local_user",
            session_incarnation_id="inc_canonical_1",
            test_command=["python", "--version"],
        )
        orch2 = Orchestrator(system1=SyntheticSystem1(), system2=s2_step2, config=cfg2)
        step2 = PlanStep("2", "Implement gateway client", ["api/gateway.py"])

        with self.assertRaises(BlockingUnknownError) as cm:
            orch2._run_step(step2)

        self.assertIn("Gateway timeout threshold and retry backoff policy", str(cm.exception))
        # Ensure write_code was NEVER called for step 2
        self.assertEqual(len(s2_step2.write_code_calls), 0)
        # Ensure file was not mutated
        self.assertEqual((self.workspace / "api" / "gateway.py").read_text(encoding="utf-8"), "# Initial gateway\n")

    # -------------------------------------------------------------------------
    # Invariant 7: Non-blocking unknowns do not halt unrelated authorized work
    # -------------------------------------------------------------------------
    def test_07_non_blocking_unknowns_do_not_halt_unrelated_authorized_work(self) -> None:
        """Non-blocking unknowns (e.g. assumptions, optional aesthetic questions) do not halt execution."""
        (self.workspace / "src" / "safe.py").write_text("# Initial code\n", encoding="utf-8")
        plan_id = "plan_non_blocking_01"
        meta = {
            "id": plan_id,
            "title": "Utility Update",
            "goal": "Update utility",
            "status": PlanStatus.READY.value,
            "revision": 1,
            "session_id": "sess_canonical_1",
            "target_context": "greenfield",
            "architecture_strategy": "keep",
            "scope": ["src/safe.py"],
            "non_scope": [],
            "assumptions": ["Assume UTF-8 encoding across all files", "Non-blocking: future caching strategy unspecified"],
            "clarifying_questions": ["Nice to have: should we add benchmark tests?"],
            "blocking_unknowns": [],  # No blocking unknowns!
            "steps": [{"id": "1", "description": "Implement utility", "files": ["src/safe.py"]}],
            "relevant_files": ["src/safe.py"],
        }
        content = "# Utility Update\n"
        save_plan_document(self.workspace, f"{plan_id}.md", content, metadata=meta)
        plan_doc = self.registry.get_plan(plan_id)

        contract = self._create_approved_contract(plan_doc, targets=["src/safe.py"])
        s2 = CapturingSystem2(output_files={"src/safe.py": "# Updated utility\n"})
        cfg = RunConfig(
            task="Implement utility",
            repo_dir=self.workspace,
            mode="build",
            execution_contract=contract,
            plan_id=plan_id,
            session_id="sess_canonical_1",
            actor="local_user",
            session_incarnation_id="inc_canonical_1",
            test_command=["python", "--version"],
        )
        orch = Orchestrator(system1=SyntheticSystem1(), system2=s2, config=cfg)
        step = PlanStep("1", "Implement utility", ["src/safe.py"])
        res = orch._run_step(step)

        self.assertIn(res.outcome, ("completed", "verified"))
        self.assertEqual(len(s2.write_code_calls), 1)

    # -------------------------------------------------------------------------
    # Invariant 8: Planned rewrite does not grant additional file permissions
    # -------------------------------------------------------------------------
    def test_08_planned_rewrite_does_not_grant_additional_file_permissions(self) -> None:
        """A plan specifying bounded_rewrite cannot write outside contract approved targets."""
        (self.workspace / "src" / "safe.py").write_text("# Old safe implementation\n", encoding="utf-8")
        (self.workspace / "src" / "forbidden.py").write_text("# Sensitive file\n", encoding="utf-8")

        plan_id = "plan_bounded_rewrite_01"
        meta = {
            "id": plan_id,
            "title": "Bounded Module Rewrite",
            "goal": "Rewrite module safely",
            "status": PlanStatus.READY.value,
            "revision": 1,
            "session_id": "sess_canonical_1",
            "target_context": "brownfield",
            "architecture_strategy": "bounded_rewrite",
            "scope": ["src/safe.py"],
            "non_scope": ["src/forbidden.py"],
            "steps": [{"id": "1", "description": "Rewrite module", "files": ["src/safe.py"]}],
            "relevant_files": ["src/safe.py"],
        }
        content = "# Bounded Module Rewrite\n"
        save_plan_document(self.workspace, f"{plan_id}.md", content, metadata=meta)
        plan_doc = self.registry.get_plan(plan_id)

        contract = self._create_approved_contract(plan_doc, targets=["src/safe.py"])
        # Malicious or buggy System 2 attempts to rewrite forbidden.py
        s2 = CapturingSystem2(output_files={
            "src/safe.py": "# New safe implementation\n",
            "src/forbidden.py": "# Tampered sensitive file\n",
        })
        cfg = RunConfig(
            task="Rewrite module",
            repo_dir=self.workspace,
            mode="build",
            execution_contract=contract,
            plan_id=plan_id,
            session_id="sess_canonical_1",
            actor="local_user",
            session_incarnation_id="inc_canonical_1",
            test_command=["python", "--version"],
        )
        orch = Orchestrator(system1=SyntheticSystem1(), system2=s2, config=cfg)
        step = PlanStep("1", "Rewrite module", ["src/safe.py"])

        with self.assertRaises(PermissionError) as cm:
            orch._run_step(step)

        self.assertIn("Scope violation", str(cm.exception))
        # Ensure forbidden file was not modified
        self.assertEqual((self.workspace / "src" / "forbidden.py").read_text(encoding="utf-8"), "# Sensitive file\n")

    # -------------------------------------------------------------------------
    # Invariant 9: PLAN mode remains read-only
    # -------------------------------------------------------------------------
    def test_09_plan_mode_remains_read_only(self) -> None:
        """PLAN mode cannot mutate source files or stage unapproved changes."""
        s2 = CapturingSystem2()
        cfg = RunConfig(
            task="Explore architecture and plan refactor",
            repo_dir=self.workspace,
            mode="plan",
            session_id="sess_canonical_1",
        )
        orch = Orchestrator(system1=SyntheticSystem1(), system2=s2, config=cfg)
        results = orch.run()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].outcome, "planned")
        # Ensure write_code was never called in plan mode
        self.assertEqual(len(s2.write_code_calls), 0)

        # Check git status remains clean (except for plan document in .brainfrog/plans/)
        st_res = subprocess.run(["git", "status", "--porcelain"], cwd=self.workspace, capture_output=True, text=True)
        untracked = [line for line in st_res.stdout.splitlines() if not line[3:].startswith(".brainfrog")]
        self.assertEqual(untracked, [])

    # -------------------------------------------------------------------------
    # Invariant 10: Approved execution contract scope remains unchanged
    # -------------------------------------------------------------------------
    def test_10_approved_execution_contract_scope_remains_unchanged(self) -> None:
        """Adding architectural metadata to plan does not widen contract approved targets."""
        plan_id = "plan_arch_immutable_01"
        meta = {
            "id": plan_id,
            "title": "Immutability Test",
            "goal": "Verify contract scope immutability",
            "status": PlanStatus.READY.value,
            "revision": 1,
            "session_id": "sess_canonical_1",
            "scope": ["src/safe.py", "src/extra.py"],
            "steps": [{"id": "1", "description": "Safe step", "files": ["src/safe.py"]}],
            "relevant_files": ["src/safe.py", "src/extra.py"],
        }
        content = "# Immutability Test\n"
        save_plan_document(self.workspace, f"{plan_id}.md", content, metadata=meta)
        plan_doc = self.registry.get_plan(plan_id)

        contract = self._create_approved_contract(plan_doc, targets=["src/safe.py"])
        # Contract targets are frozen and immutable
        self.assertEqual(contract.approved_targets, frozenset(["src/safe.py"]))
        self.assertNotIn("src/extra.py", contract.approved_targets)

    # -------------------------------------------------------------------------
    # Invariant 11: Modified plan/step cannot inherit stale authorization after resume
    # -------------------------------------------------------------------------
    def test_11_modified_plan_or_step_cannot_inherit_stale_authorization_after_resume(self) -> None:
        """Modifying a plan on disk invalidates existing contract approval upon resume."""
        (self.workspace / "src" / "safe.py").write_text("# Initial\n", encoding="utf-8")
        plan_id = "plan_stale_auth_01"
        meta = {
            "id": plan_id,
            "title": "Stale Auth Test",
            "goal": "Verify invalidation upon modification",
            "status": PlanStatus.READY.value,
            "revision": 1,
            "session_id": "sess_canonical_1",
            "steps": [{"id": "1", "description": "Safe step", "files": ["src/safe.py"]}],
            "relevant_files": ["src/safe.py"],
        }
        content = "# Stale Auth Test\n- **Goal**: Initial\n"
        save_plan_document(self.workspace, f"{plan_id}.md", content, metadata=meta)
        plan_doc = self.registry.get_plan(plan_id)

        contract = self._create_approved_contract(plan_doc, targets=["src/safe.py"])

        # Tamper / modify the plan markdown on disk
        tampered_content = "# Stale Auth Test\n- **Goal**: Tampered modified goal\n"
        (self.workspace / ".brainfrog" / "plans" / f"{plan_id}.md").write_text(tampered_content, encoding="utf-8")

        runner = StructuredStepRunner(
            workspace_dir=self.workspace,
            orchestrator=Orchestrator(system1=SyntheticSystem1(), system2=CapturingSystem2(), config=RunConfig(task="Test", repo_dir=self.workspace)),
            contract=contract,
            plan_id=plan_id,
        )

        with self.assertRaises(PermissionError) as cm:
            runner.revalidate_pre_mutation()

        self.assertIn("authorization invalidated", str(cm.exception).lower())

    # -------------------------------------------------------------------------
    # Invariant 12: Cancellation and failed-step handling remain safe
    # -------------------------------------------------------------------------
    def test_12_cancellation_and_failed_step_handling_remain_safe(self) -> None:
        """Cancellation skips subsequent steps and leaves workspace in a clean state."""
        (self.workspace / "src" / "step1.py").write_text("# Initial step1\n", encoding="utf-8")
        (self.workspace / "src" / "step2.py").write_text("# Initial step2\n", encoding="utf-8")

        plan_id = "plan_cancel_safe_01"
        meta = {
            "id": plan_id,
            "title": "Multi-Step Cancel Test",
            "goal": "Verify cancellation safety",
            "status": PlanStatus.READY.value,
            "revision": 1,
            "session_id": "sess_canonical_1",
            "steps": [
                {"id": "1", "description": "Step 1", "files": ["src/step1.py"]},
                {"id": "2", "description": "Step 2", "files": ["src/step2.py"]},
            ],
            "relevant_files": ["src/step1.py", "src/step2.py"],
        }
        content = "# Multi-Step Cancel Test\n"
        save_plan_document(self.workspace, f"{plan_id}.md", content, metadata=meta)
        plan_doc = self.registry.get_plan(plan_id)

        contract = self._create_approved_contract(plan_doc, targets=["src/step1.py", "src/step2.py"])
        s2 = CapturingSystem2(output_files={"src/step1.py": "# Mutated step1\n"})

        cancel_ev = threading.Event()
        cancel_token = _CURRENT_CANCEL_EVENT.set(cancel_ev)

        cfg = RunConfig(
            task="Multi-step test",
            repo_dir=self.workspace,
            mode="build",
            execution_contract=contract,
            plan_id=plan_id,
            session_id="sess_canonical_1",
            test_command=["python", "--version"],
        )
        orch = Orchestrator(system1=SyntheticSystem1(), system2=s2, config=cfg)
        runner = StructuredStepRunner(
            workspace_dir=self.workspace,
            orchestrator=orch,
            contract=contract,
            plan_id=plan_id,
        )

        # Trigger cancellation before running
        cancel_ev.set()

        steps = [
            PlanStep("1", "Step 1", ["src/step1.py"]),
            PlanStep("2", "Step 2", ["src/step2.py"]),
        ]

        with self.assertRaises(KeyboardInterrupt):
            runner.run_steps(steps)

        # Step 1 should be cancelled, Step 2 skipped
        rec1 = runner.tracker.get_record("1")
        rec2 = runner.tracker.get_record("2")
        self.assertIsNotNone(rec1)
        self.assertEqual(rec1.status, StepExecutionStatus.CANCELLED)
        self.assertIsNotNone(rec2)
        self.assertEqual(rec2.status, StepExecutionStatus.SKIPPED)

        _CURRENT_CANCEL_EVENT.reset(cancel_token)


# -----------------------------------------------------------------------------
# Representative Architectural Scenarios (Section 10)
# -----------------------------------------------------------------------------
class ArchitecturalScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="brainfrog_scenarios_")
        self.workspace = Path(self.test_dir).resolve()
        (self.workspace / ".brainfrog" / "plans").mkdir(parents=True, exist_ok=True)
        (self.workspace / "src").mkdir(parents=True, exist_ok=True)
        self.registry = PlanRegistry(self.workspace)

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_scenario_greenfield_minimal_constraints(self) -> None:
        """Greenfield utility carries minimal necessary constraints without synthetic bloat."""
        plan_doc = PlanDocument(
            filename="greenfield_util.md",
            path=self.workspace / ".brainfrog" / "plans" / "greenfield_util.md",
            title="Create Math Utility",
            goal="Add standalone gcd calculation helper",
            target_context="greenfield",
            architecture_strategy="keep",
            scope=["src/math_util.py"],
            acceptance_criteria=["gcd(12, 18) == 6", "gcd(0, 5) == 5"],
            steps=[{"id": "1", "description": "Implement gcd", "files": ["src/math_util.py"]}],
            relevant_files=["src/math_util.py"],
        )
        is_blocked, unknowns = plan_doc.has_unresolved_blocking_unknowns()
        self.assertFalse(is_blocked)
        arch_ctx = plan_doc.get_step_architectural_context(plan_doc.steps[0])
        self.assertEqual(arch_ctx["target_context"], "greenfield")
        self.assertEqual(arch_ctx["architecture_strategy"], "keep")
        self.assertEqual(arch_ctx["relevant_constraints"], [])

    def test_scenario_brownfield_api_compat(self) -> None:
        """Brownfield public API refactor enforces backward compatibility and caller signatures."""
        plan_doc = PlanDocument(
            filename="brownfield_api.md",
            path=self.workspace / ".brainfrog" / "plans" / "brownfield_api.md",
            title="Refactor Payment Service",
            goal="Modernize internal payment router while keeping public API unchanged",
            target_context="brownfield",
            architecture_strategy="local_refactor",
            scope=["src/payment_service.py"],
            non_scope=["src/database.py"],
            architectural_constraints=["PaymentService.charge(user_id, amount) public signature must not change."],
            compatibility_contracts=["All 14 existing caller unit tests must pass without modifications."],
            behavior_invariants=["Idempotency keys must be validated before charging."],
            steps=[{"id": "1", "description": "Refactor router", "files": ["src/payment_service.py"]}],
            relevant_files=["src/payment_service.py"],
        )
        step_ctx = plan_doc.get_step_architectural_context(plan_doc.steps[0])
        self.assertIn("PaymentService.charge(user_id, amount) public signature must not change.", step_ctx["relevant_constraints"])
        self.assertIn("All 14 existing caller unit tests must pass without modifications.", step_ctx["compatibility_contracts"])
        self.assertIn("Idempotency keys must be validated before charging.", step_ctx["behavior_invariants"])

    def test_scenario_bounded_rewrite_preserves_external_contracts(self) -> None:
        """Bounded rewrite identifies replacement boundaries and caller contracts."""
        plan_doc = PlanDocument(
            filename="bounded_rewrite.md",
            path=self.workspace / ".brainfrog" / "plans" / "bounded_rewrite.md",
            title="Rewrite Token Serializer",
            goal="Replace obsolete JWT parser with fast binary serializer",
            target_context="brownfield",
            architecture_strategy="bounded_rewrite",
            scope=["src/token_serializer.py"],
            non_scope=["src/auth_middleware.py"],
            architectural_constraints=["Must implement ITokenSerializer interface."],
            compatibility_contracts=["Deserialized token claims dictionary must match existing structure."],
            steps=[{"id": "1", "description": "Rewrite serializer", "files": ["src/token_serializer.py"]}],
            relevant_files=["src/token_serializer.py"],
        )
        step_ctx = plan_doc.get_step_architectural_context(plan_doc.steps[0])
        self.assertEqual(step_ctx["architecture_strategy"], "bounded_rewrite")
        self.assertIn("Must implement ITokenSerializer interface.", step_ctx["relevant_constraints"])

    def test_scenario_stateful_incremental_migration(self) -> None:
        """Incremental migration carries schema versioning and recovery rollback rules."""
        plan_doc = PlanDocument(
            filename="data_migration.md",
            path=self.workspace / ".brainfrog" / "plans" / "data_migration.md",
            title="Migrate User Settings Schema",
            goal="Add theme column with default value without dropping existing table",
            target_context="brownfield",
            architecture_strategy="incremental_migration",
            scope=["src/migrations/002_add_theme.sql"],
            migration_constraints=[
                "Dual-read compatibility window required for 1 release.",
                "Rollback script 002_rollback_theme.sql must be verified.",
            ],
            steps=[{"id": "1", "description": "Add migration", "files": ["src/migrations/002_add_theme.sql"]}],
            relevant_files=["src/migrations/002_add_theme.sql"],
        )
        step_ctx = plan_doc.get_step_architectural_context(plan_doc.steps[0])
        self.assertEqual(len(step_ctx["migration_constraints"]), 2)
        self.assertIn("Dual-read compatibility window required for 1 release.", step_ctx["migration_constraints"])

    def test_scenario_unknown_business_rules_blocks_only_dependent_steps(self) -> None:
        """Unknown business decisions halt only dependent steps while allowing independent safe steps."""
        plan_doc = PlanDocument(
            filename="pricing_plan.md",
            path=self.workspace / ".brainfrog" / "plans" / "pricing_plan.md",
            title="Tiered Pricing Feature",
            goal="Implement enterprise tier pricing and reporting",
            target_context="mixed",
            architecture_strategy="local_refactor",
            scope=["src/reporting.py", "src/pricing.py"],
            blocking_unknowns=[
                {
                    "id": "blk_pricing_formula",
                    "decision": "Enterprise discount tier calculation percentage",
                    "question": "What is the discount rate for > 10,000 users?",
                    "affected_steps": ["2"],
                    "affected_files": ["src/pricing.py"],
                    "resolved": False,
                }
            ],
            steps=[
                {"id": "1", "description": "Add basic usage report", "files": ["src/reporting.py"]},
                {"id": "2", "description": "Implement enterprise discount", "files": ["src/pricing.py"]},
            ],
            relevant_files=["src/reporting.py", "src/pricing.py"],
        )
        # Step 1 is NOT blocked
        blocked1, items1 = plan_doc.has_unresolved_blocking_unknowns(step_id="1", files=["src/reporting.py"])
        self.assertFalse(blocked1)
        self.assertEqual(items1, [])

        # Step 2 IS blocked
        blocked2, items2 = plan_doc.has_unresolved_blocking_unknowns(step_id="2", files=["src/pricing.py"])
        self.assertTrue(blocked2)
        self.assertEqual(len(items2), 1)
        self.assertEqual(items2[0]["decision"], "Enterprise discount tier calculation percentage")


if __name__ == "__main__":
    unittest.main()
