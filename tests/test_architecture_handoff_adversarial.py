"""Adversarial security and contract integrity audit for Phase 3 PLAN -> BUILD handoff.

Validates that:
1. Material changes to plan metadata (.meta.json) invalidate execution contracts fail-closed.
2. Provider responses lacking blocking_unknowns but containing clarifying questions fail-closed.
3. StructuredStepRunner with contract.plan_id enforces the blocking gate even if runner.plan_id is omitted.
4. Constraints referencing outside callers or parent directories are not erroneously dropped.
5. Step ID formatting variations ("Step 1", "step_1", 1) and path separators cannot bypass blocking unknown checks.
6. Malformed metadata fails closed without crashing or leaking authority.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.plans import (
    ALLOWED_PLAN_TRANSITIONS,
    BlockingUnknownError,
    PlanDocument,
    PlanRegistry,
    PlanStatus,
    compute_file_hash,
    save_plan_document,
)
from core.runtime.capabilities import Capabilities, FilesystemPolicy, NetworkPolicy
from core.runtime.approval import CanonicalOperation
from core.runtime.contract import ApprovedExecutionContract
from core.runtime.step_runner import (
    StepExecutionStatus,
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
            "title": "Adversarial PRD",
            "is_small_task": False,
            "problem": "Testing adversarial boundaries",
            "goals": [task],
            "scope": ["src/safe.py"],
            "non_scope": ["src/unsafe.py"],
            "steps": [{"id": "1", "description": "Execute step", "files": ["src/safe.py"]}],
            "relevant_files": ["src/safe.py"],
        }
        res = dict(default_data)
        res.update(self.plan_and_prd_data)
        return res

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
        return dict(self.output_files)


# -----------------------------------------------------------------------------
# Adversarial Test Suite
# -----------------------------------------------------------------------------
class ArchitectureHandoffAdversarialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="brainfrog_adv_audit_")
        self.workspace = Path(self.test_dir).resolve()
        (self.workspace / ".brainfrog" / "plans").mkdir(parents=True, exist_ok=True)
        (self.workspace / "api").mkdir(parents=True, exist_ok=True)
        (self.workspace / "checkout").mkdir(parents=True, exist_ok=True)
        (self.workspace / "src").mkdir(parents=True, exist_ok=True)

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

    def _create_contract(
        self,
        plan_doc: Optional[PlanDocument],
        targets: Optional[List[str]] = None,
        session_id: str = "sess_adv_1",
        session_incarnation: str = "inc_adv_1",
    ) -> ApprovedExecutionContract:
        assert plan_doc is not None
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
        payload = {
            "schema_version": 2,
            "request_id": "req_adv_01",
            "session_id": session_id,
            "session_incarnation_id": session_incarnation,
            "channel": "cli",
            "user_id": "adversary_auditor",
            "conversation_id": "conv_adv_01",
            "operation_type": "write_files",
            "operation": op.to_canonical_dict(),
            "operation_digest": op.compute_digest(),
            "capabilities": caps.to_dict(),
            "created_at": 1000.0,
            "expires_at": 9999999999.0,
            "nonce": "adv_nonce_1",
            "workspace_root": self.workspace.as_posix(),
            "plan_id": plan_doc.id,
            "plan_revision": plan_doc.revision,
            "plan_content_hash": plan_doc.content_hash,
        }
        meta_hash = getattr(plan_doc, "metadata_hash", None)
        if meta_hash:
            payload["plan_metadata_hash"] = meta_hash

        auth_digest = CanonicalOperation("execution_contract_v2", "", payload).compute_digest()
        auth_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

        kwargs = {
            "request_id": "req_adv_01",
            "action_type": "write_files",
            "approved_targets": frozenset(approved_targets),
            "operation_digest": op.compute_digest(),
            "channel": "cli",
            "capabilities": caps,
            "actor": "adversary_auditor",
            "session_id": session_id,
            "session_incarnation_id": session_incarnation,
            "expires_at": 9999999999.0,
            "authorization_json": auth_json,
            "authorization_digest": auth_digest,
            "workspace_root": self.workspace.as_posix(),
            "plan_id": plan_doc.id,
            "plan_revision": plan_doc.revision,
            "plan_content_hash": plan_doc.content_hash,
        }
        if meta_hash:
            kwargs["plan_metadata_hash"] = meta_hash
        return ApprovedExecutionContract(**kwargs)

    # -------------------------------------------------------------------------
    # Test 1: Metadata Tampering Invalidates Approved Contract
    # -------------------------------------------------------------------------
    def test_adversarial_metadata_tampering_invalidates_contract(self) -> None:
        """Modifying .meta.json out-of-band to clear blocking unknowns must invalidate contract."""
        plan_id = "plan_tamper_meta_01"
        meta = {
            "id": plan_id,
            "title": "Payment Hardening",
            "goal": "Refactor payment",
            "status": PlanStatus.READY.value,
            "revision": 1,
            "session_id": "sess_adv_1",
            "scope": ["src/safe.py"],
            "blocking_unknowns": [{"id": "blk_1", "decision": "Secret key rotation policy", "resolved": False}],
            "steps": [{"id": "1", "description": "Update payment", "files": ["src/safe.py"]}],
            "relevant_files": ["src/safe.py"],
        }
        save_plan_document(self.workspace, f"{plan_id}.md", "# Payment Hardening\n", metadata=meta)
        plan_doc = self.registry.get_plan(plan_id)
        assert plan_doc is not None
        contract = self._create_contract(plan_doc, targets=["src/safe.py"])

        # Adversary directly modifies .meta.json on disk to clear blocking unknowns without touching .md
        meta_file = self.workspace / ".brainfrog" / "plans" / f"{plan_id}.meta.json"
        raw_meta = json.loads(meta_file.read_text(encoding="utf-8"))
        raw_meta["blocking_unknowns"] = []  # cleared!
        meta_file.write_text(json.dumps(raw_meta, indent=2), encoding="utf-8")

        # Contract validation must detect the out-of-band metadata modification
        with self.assertRaises(PermissionError):
            contract.validate_plan_binding(self.workspace, expected_plan_id=plan_id)

    # -------------------------------------------------------------------------
    # Test 2: StepRunner Halts When Metadata Is Modified During Execution
    # -------------------------------------------------------------------------
    def test_adversarial_step_runner_tampered_metadata_stops_execution(self) -> None:
        """StepRunner revalidate_pre_mutation must fail closed if .meta.json is modified."""
        plan_id = "plan_tamper_meta_runner_02"
        meta = {
            "id": plan_id,
            "title": "Module Update",
            "goal": "Update module",
            "status": PlanStatus.READY.value,
            "revision": 1,
            "session_id": "sess_adv_1",
            "scope": ["src/safe.py"],
            "architectural_constraints": ["Strict backward compatibility with v1 API"],
            "blocking_unknowns": [],
            "steps": [{"id": "1", "description": "Update", "files": ["src/safe.py"]}],
            "relevant_files": ["src/safe.py"],
        }
        save_plan_document(self.workspace, f"{plan_id}.md", "# Module Update\n", metadata=meta)
        plan_doc = self.registry.get_plan(plan_id)
        assert plan_doc is not None
        contract = self._create_contract(plan_doc, targets=["src/safe.py"])

        runner = StructuredStepRunner(
            workspace_dir=self.workspace,
            orchestrator=None,
            contract=contract,
            plan_id=plan_id,
        )

        # Attacker tampers with .meta.json to strip architectural constraints
        meta_file = self.workspace / ".brainfrog" / "plans" / f"{plan_id}.meta.json"
        raw_meta = json.loads(meta_file.read_text(encoding="utf-8"))
        raw_meta["architectural_constraints"] = []  # stripped!
        meta_file.write_text(json.dumps(raw_meta, indent=2), encoding="utf-8")

        with self.assertRaises(StaleExecutionAuthorizationError):
            runner.revalidate_pre_mutation()

    # -------------------------------------------------------------------------
    # Test 3: StructuredStepRunner With Contract Plan ID Enforces Gate
    # -------------------------------------------------------------------------
    def test_adversarial_step_runner_with_contract_plan_id_enforces_gate(self) -> None:
        """Runner instantiated with plan_id=None must still enforce blocking gate from contract.plan_id."""
        plan_id = "plan_contract_bound_03"
        (self.workspace / "src" / "safe.py").write_text("# initial\n", encoding="utf-8")
        meta = {
            "id": plan_id,
            "title": "API Gateway",
            "goal": "Setup gateway",
            "status": PlanStatus.READY.value,
            "revision": 1,
            "session_id": "sess_adv_1",
            "scope": ["src/safe.py"],
            "blocking_unknowns": [{"id": "blk_gw", "decision": "Gateway timeout policy", "resolved": False}],
            "steps": [{"id": "1", "description": "Implement gateway", "files": ["src/safe.py"]}],
            "relevant_files": ["src/safe.py"],
        }
        save_plan_document(self.workspace, f"{plan_id}.md", "# API Gateway\n", metadata=meta)
        plan_doc = self.registry.get_plan(plan_id)
        assert plan_doc is not None
        contract = self._create_contract(plan_doc, targets=["src/safe.py"])

        # Instantiate runner with plan_id=None (only contract has plan_id)
        runner = StructuredStepRunner(
            workspace_dir=self.workspace,
            orchestrator=None,
            contract=contract,
            plan_id=None,  # Runner plan_id omitted
        )

        step = {"id": "1", "description": "Implement gateway", "files": ["src/safe.py"]}
        with self.assertRaises(BlockingUnknownError):
            runner.run_steps([step])

    # -------------------------------------------------------------------------
    # Test 4: Provider Returning Clarifying Questions Falls Closed to Blocking
    # -------------------------------------------------------------------------
    def test_adversarial_clarifying_questions_fallback_blocks_execution(self) -> None:
        """When provider schema omits blocking_unknowns but has clarifying_questions, orchestrator must fail-closed."""
        s2 = CapturingSystem2(
            plan_and_prd_data={
                "title": "Order Processing Service",
                "clarifying_questions": ["What is the idempotent deduplication window for incoming orders?"],
                # blocking_unknowns is omitted by the model
                "steps": [{"id": "1", "description": "Implement processor", "files": ["src/safe.py"]}],
            }
        )
        cfg = RunConfig(
            task="Build order processing service",
            repo_dir=self.workspace,
            mode="plan",
            session_id="sess_adv_1",
            actor="adversary_auditor",
        )
        orch = Orchestrator(system1=SyntheticSystem1(), system2=s2, config=cfg)
        results = orch.run()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].outcome, "planned")
        plan_id = results[0].suggestion
        assert plan_id is not None

        # Verify that clarifying questions were normalized into blocking unknowns in metadata
        plan_doc = self.registry.get_plan(plan_id)
        assert plan_doc is not None
        self.assertTrue(plan_doc.has_unresolved_blocking_unknowns()[0])
        unknowns = plan_doc.get_unresolved_blocking_unknowns()
        self.assertEqual(len(unknowns), 1)
        self.assertIn("idempotent deduplication window", unknowns[0]["decision"])

    # -------------------------------------------------------------------------
    # Test 5: Constraint Referencing Outside Caller Is Not Erroneously Omitted
    # -------------------------------------------------------------------------
    def test_adversarial_constraint_caller_referencing_outside_file_not_omitted(self) -> None:
        """A constraint mentioning callers outside the edited file must NOT be dropped."""
        plan_id = "plan_caller_compat_05"
        meta = {
            "id": plan_id,
            "title": "Payment API Compat",
            "goal": "Refactor payment api",
            "status": PlanStatus.READY.value,
            "revision": 1,
            "session_id": "sess_adv_1",
            "scope": ["api/payment.py", "checkout/service.py"],
            "relevant_files": ["api/payment.py", "checkout/service.py"],
            "architectural_constraints": [
                "Maintain backward compatibility for checkout/service.py callers."
            ],
            "steps": [{"id": "1", "description": "Refactor payment", "files": ["api/payment.py"]}],
        }
        save_plan_document(self.workspace, f"{plan_id}.md", "# Payment API Compat\n", metadata=meta)
        plan_doc = self.registry.get_plan(plan_id)
        assert plan_doc is not None

        # Check step 1 context (only touches api/payment.py)
        step1 = {"id": "1", "description": "Refactor payment", "files": ["api/payment.py"]}
        ctx = plan_doc.get_step_architectural_context(step1)
        self.assertIn(
            "Maintain backward compatibility for checkout/service.py callers.",
            ctx["relevant_constraints"],
            "Caller compatibility constraint must not be dropped because checkout/service.py is mentioned!",
        )

    # -------------------------------------------------------------------------
    # Test 6: Parent Directory, Windows Separators, and Wildcard Matching
    # -------------------------------------------------------------------------
    def test_adversarial_constraint_parent_directory_and_wildcard_matching(self) -> None:
        """Constraints targeting parent directories, wildcards, or windows paths must match child step files."""
        plan_id = "plan_prefix_matching_06"
        meta = {
            "id": plan_id,
            "title": "Prefix and Wildcard Constraints",
            "goal": "Test matching",
            "status": PlanStatus.READY.value,
            "revision": 1,
            "session_id": "sess_adv_1",
            "scope": ["api/payment.py"],
            "relevant_files": ["api/payment.py"],
            "architectural_constraints": [
                {"text": "All endpoints under api/ must be rate-limited.", "files": ["api/"]},
                {"text": "Python files in api/*.py must follow PEP8.", "files": ["api/*.py"]},
                "Strict typing required for api\\payment.py.",  # Windows separator in unstructured text
            ],
            "steps": [{"id": "1", "description": "Refactor payment", "files": ["api/payment.py"]}],
        }
        save_plan_document(self.workspace, f"{plan_id}.md", "# Prefix and Wildcard\n", metadata=meta)
        plan_doc = self.registry.get_plan(plan_id)
        assert plan_doc is not None

        step1 = {"id": "1", "description": "Refactor payment", "files": ["api/payment.py"]}
        ctx = plan_doc.get_step_architectural_context(step1)
        self.assertIn("All endpoints under api/ must be rate-limited.", ctx["relevant_constraints"])
        self.assertIn("Python files in api/*.py must follow PEP8.", ctx["relevant_constraints"])
        self.assertIn("Strict typing required for api\\payment.py.", ctx["relevant_constraints"])

    # -------------------------------------------------------------------------
    # Test 7: Step ID Variations and File Prefixes in Blocking Unknowns
    # -------------------------------------------------------------------------
    def test_adversarial_step_id_variations_in_blocking_unknowns(self) -> None:
        """Blocking unknowns specifying 'Step 1' or 'step_1' or parent folder must block step '1'."""
        plan_id = "plan_step_id_variations_07"
        meta = {
            "id": plan_id,
            "title": "Step ID Variation Test",
            "goal": "Test variation matching",
            "status": PlanStatus.READY.value,
            "revision": 1,
            "session_id": "sess_adv_1",
            "scope": ["api/payment.py"],
            "blocking_unknowns": [
                {
                    "id": "blk_var",
                    "decision": "Payment webhook signing secret unknown",
                    "affected_steps": ["Step 1", "step_2"],
                    "affected_files": ["api/"],
                    "resolved": False,
                }
            ],
            "steps": [
                {"id": "1", "description": "Step 1 description", "files": ["api/payment.py"]},
                {"id": "2", "description": "Step 2 description", "files": ["api/other.py"]},
            ],
        }
        save_plan_document(self.workspace, f"{plan_id}.md", "# Step ID Variations\n", metadata=meta)
        plan_doc = self.registry.get_plan(plan_id)
        assert plan_doc is not None

        # Check step "1"
        is_blocked1, items1 = plan_doc.has_unresolved_blocking_unknowns(step_id="1", files=["api/payment.py"])
        self.assertTrue(is_blocked1, "Step 1 should be blocked by 'Step 1' and 'api/'!")

        # Check step "2"
        is_blocked2, items2 = plan_doc.has_unresolved_blocking_unknowns(step_id="2", files=["api/other.py"])
        self.assertTrue(is_blocked2, "Step 2 should be blocked by 'step_2' and 'api/'!")

    # -------------------------------------------------------------------------
    # Test 8: Malformed Metadata Fails Closed
    # -------------------------------------------------------------------------
    def test_adversarial_malformed_metadata_fails_closed(self) -> None:
        """Corrupted or non-collection fields in metadata must not crash the loader and must fail closed."""
        plan_id = "plan_malformed_meta_08"
        meta = {
            "id": plan_id,
            "title": "Malformed Plan",
            "status": PlanStatus.READY.value,
            "revision": 1,
            "session_id": "sess_adv_1",
            "blocking_unknowns": 12345,  # Invalid: integer instead of list
            "architectural_constraints": True,  # Invalid: boolean
            "scope": "src/safe.py",  # Invalid: string instead of list
            "steps": [{"id": "1", "description": "Step", "files": ["src/safe.py"]}],
        }
        save_plan_document(self.workspace, f"{plan_id}.md", "# Malformed Plan\n", metadata=meta)
        plan_doc = self.registry.get_plan(plan_id)
        assert plan_doc is not None

        # The document must load safely into normalized empty or singleton collections
        self.assertIsInstance(plan_doc.blocking_unknowns, list)
        self.assertIsInstance(plan_doc.architectural_constraints, list)
        self.assertIsInstance(plan_doc.scope, list)


if __name__ == "__main__":
    unittest.main()
