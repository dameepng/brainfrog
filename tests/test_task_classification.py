"""Unit and Security Test Suite for BrainFrog P1.4D Deterministic Task Classification.

Validates:
- CLASSIFICATION ≠ AUTHORIZATION invariant
- CLASSIFICATION ≠ ROUTING invariant
- Pure domain model: zero execution, zero network, zero LLM, zero subprocesses
- Deterministic repeatability: same Task -> identical TaskClassification
- Explicit task_type, domain, and requested_skills respected over inference
- Deterministic inference fallback for unclassified tasks
- Insufficient evidence fallback to UNKNOWN / None (no guessing)
- Case normalization and deterministic sorting of output collections
- Deep immutability and slots enforcement
- Deterministic SHA-256 digest computation (excluding volatile timestamp)
- Tampering detection and digest integrity verification
- Serialization roundtrip (to_dict / from_dict / to_json)
- Explicit resource bounds and control-character rejection
- Authority-like token and field rejection
- Concrete credential and secret pattern rejection
- Static architecture zero-execution / zero-authority import inspection
"""
from __future__ import annotations

import ast
import json
import time
from enum import Enum
from pathlib import Path
from typing import Any, Dict
import unittest

from core.runtime.task import Task
import core.runtime.task_classification as tc_mod
from core.runtime.task_classification import (
    CURRENT_TASK_CLASSIFICATION_SCHEMA_VERSION,
    MAX_EVIDENCE_CHARS,
    MAX_EVIDENCE_COUNT,
    MAX_SKILL_CHARS,
    MAX_SKILLS_COUNT,
    MAX_TASK_CLASSIFICATION_SERIALIZED_BYTES,
    ClassificationConfidence,
    DeterministicTaskClassifier,
    TaskClassification,
    TaskClassificationAuthorityViolationError,
    TaskClassificationError,
    TaskClassificationIntegrityError,
    TaskClassificationSecretExposureError,
    TaskClassificationValidationError,
    classify_task,
)

# Test credential fragments dynamically assembled to avoid scanner false positives
_MOCK_SK = "sk-" + "proj-1234567890abcdef1234567890abcdef"
_MOCK_GH = "gh" + "p_1234567890abcdef1234567890abcdef"
_MOCK_PK = "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0...\n-----END " + "RSA PRIVATE KEY-----"
_MOCK_BEARER = "Bearer " + "eyJh1234567890abcdef"
_MOCK_TG = "123456789:" + "ABCdefGHIjklMNOpqrsTUVwxyz123456789"
_MOCK_ASSIGN = "pass" + "word = supersecretpassword123"


class SampleEnum(Enum):
    TESTING = "testing"
    ANALYSIS = "ANALYSIS"


class TestTaskClassificationCreationAndValidation(unittest.TestCase):
    """Test standard construction, field validation, and resource bounds."""

    def test_minimal_valid_classification(self) -> None:
        tc = TaskClassification(task_id="t_min_01")
        self.assertEqual(tc.task_id, "t_min_01")
        self.assertIsNone(tc.task_type)
        self.assertIsNone(tc.domain)
        self.assertEqual(tc.skills, ())
        self.assertEqual(tc.confidence, ClassificationConfidence.UNKNOWN)
        self.assertEqual(tc.classifier_version, CURRENT_TASK_CLASSIFICATION_SCHEMA_VERSION)
        self.assertEqual(tc.evidence, ())
        self.assertFalse(tc.is_inferred)
        self.assertTrue(len(tc.digest) == 64)

    def test_full_valid_classification(self) -> None:
        tc = TaskClassification(
            task_id="t_full_01",
            task_type="implementation",
            domain="backend",
            skills=("fastapi", "python"),
            confidence=ClassificationConfidence.HIGH,
            evidence=("explicit:domain:backend", "explicit:task_type:implementation"),
            is_inferred=False,
        )
        self.assertEqual(tc.task_id, "t_full_01")
        self.assertEqual(tc.task_type, "implementation")
        self.assertEqual(tc.domain, "backend")
        self.assertEqual(tc.skills, ("fastapi", "python"))
        self.assertEqual(tc.confidence, ClassificationConfidence.HIGH)
        self.assertEqual(tc.evidence, ("explicit:domain:backend", "explicit:task_type:implementation"))
        self.assertFalse(tc.is_inferred)

    def test_case_normalization_and_sorting(self) -> None:
        tc = TaskClassification(
            task_id="t_norm_01",
            task_type="TESTING",
            domain="DATABASE",
            skills=("Python", "SQL", "pytest"),
            evidence=("evidence_Z", "evidence_A"),
        )
        self.assertEqual(tc.task_type, "testing")
        self.assertEqual(tc.domain, "database")
        self.assertEqual(tc.skills, ("pytest", "python", "sql"))
        self.assertEqual(tc.evidence, ("evidence_A", "evidence_Z"))

    def test_reject_control_characters(self) -> None:
        with self.assertRaises(TaskClassificationValidationError):
            TaskClassification(task_id="t_ctrl_1", task_type="test\x1btype")

        with self.assertRaises(TaskClassificationValidationError):
            TaskClassification(task_id="t_ctrl_2", domain="back\x00end")

        with self.assertRaises(TaskClassificationValidationError):
            TaskClassification(task_id="t_ctrl_3", skills=("py\ntest",))

        with self.assertRaises(TaskClassificationValidationError):
            TaskClassification(task_id="t_ctrl_4", evidence=("ev\x07id",))

    def test_reject_empty_string_items(self) -> None:
        with self.assertRaises(TaskClassificationValidationError):
            TaskClassification(task_id="t_empty_sk", skills=("",))

        with self.assertRaises(TaskClassificationValidationError):
            TaskClassification(task_id="t_empty_ev", evidence=("",))

    def test_reject_invalid_confidence(self) -> None:
        with self.assertRaises(TaskClassificationValidationError):
            TaskClassification(task_id="t_bad_conf", confidence="super_high")  # type: ignore

    def test_reject_oversized_fields(self) -> None:
        with self.assertRaises(TaskClassificationValidationError):
            TaskClassification(
                task_id="t_too_many_sk",
                skills=tuple(f"skill_{i:03d}" for i in range(MAX_SKILLS_COUNT + 1)),
            )

        with self.assertRaises(TaskClassificationValidationError):
            TaskClassification(
                task_id="t_too_many_ev",
                evidence=tuple(f"ev_{i:03d}" for i in range(MAX_EVIDENCE_COUNT + 1)),
            )

        with self.assertRaises(TaskClassificationValidationError):
            TaskClassification(task_id="t_long_sk", skills=("s" * (MAX_SKILL_CHARS + 1),))

        with self.assertRaises(TaskClassificationValidationError):
            TaskClassification(task_id="t_long_ev", evidence=("e" * (MAX_EVIDENCE_CHARS + 1),))


class TestTaskClassificationImmutability(unittest.TestCase):
    """Test immutability, freezing, and slots protection."""

    def test_frozen_attributes_cannot_be_mutated(self) -> None:
        tc = TaskClassification(task_id="t_freeze_01", task_type="testing")
        with self.assertRaises(Exception):
            tc.task_type = "refactor"  # type: ignore

        with self.assertRaises(Exception):
            tc.domain = "frontend"  # type: ignore

        with self.assertRaises(Exception):
            tc.confidence = ClassificationConfidence.LOW  # type: ignore

        with self.assertRaises(Exception):
            tc.digest = "hacked"  # type: ignore

    def test_slots_prevents_arbitrary_attributes(self) -> None:
        tc = TaskClassification(task_id="t_slots_01")
        with self.assertRaises((AttributeError, TypeError)):
            tc.custom_attr = "injected"  # type: ignore


class TestTaskClassificationDigestAndSerialization(unittest.TestCase):
    """Test deterministic digests, serialization roundtrips, and tampering detection."""

    def test_digest_deterministic(self) -> None:
        tc1 = TaskClassification(
            task_id="t_dig_01",
            task_type="debugging",
            domain="security",
            skills=("python", "owasp"),
            confidence=ClassificationConfidence.HIGH,
        )
        tc2 = TaskClassification(
            task_id="t_dig_01",
            task_type="debugging",
            domain="security",
            skills=("owasp", "python"),  # reverse order in input
            confidence=ClassificationConfidence.HIGH,
        )
        self.assertEqual(tc1.digest, tc2.digest)

    def test_timestamp_independence_and_no_time_dependency(self) -> None:
        """TaskClassification has zero timestamp or clock dependency in semantic identity."""
        tc = TaskClassification(task_id="t_ts_01")
        self.assertFalse(hasattr(tc, "created_at"))
        self.assertFalse(hasattr(tc, "timestamp"))

    def test_digest_sensitive_to_semantic_changes(self) -> None:
        tc1 = TaskClassification(task_id="t_chg_01", task_type="testing")
        tc2 = TaskClassification(task_id="t_chg_01", task_type="debugging")
        self.assertNotEqual(tc1.digest, tc2.digest)

        tc3 = TaskClassification(task_id="t_chg_01", task_type="testing", domain="backend")
        self.assertNotEqual(tc1.digest, tc3.digest)

    def test_tampering_detection(self) -> None:
        with self.assertRaises(TaskClassificationIntegrityError):
            TaskClassification(
                task_id="t_tamper_01",
                task_type="testing",
                digest="0" * 64,  # forged digest
            )

    def test_roundtrip_to_dict_and_from_dict(self) -> None:
        tc = TaskClassification(
            task_id="t_rt_01",
            task_type="review",
            domain="qa",
            skills=("pytest", "coverage"),
            confidence=ClassificationConfidence.MEDIUM,
            evidence=("explicit:task_type:review",),
            is_inferred=False,
        )
        d = tc.to_dict()
        self.assertIsInstance(d, dict)
        tc_restored = TaskClassification.from_dict(d)
        self.assertEqual(tc.task_id, tc_restored.task_id)
        self.assertEqual(tc.task_type, tc_restored.task_type)
        self.assertEqual(tc.domain, tc_restored.domain)
        self.assertEqual(tc.skills, tc_restored.skills)
        self.assertEqual(tc.confidence, tc_restored.confidence)
        self.assertEqual(tc.evidence, tc_restored.evidence)
        self.assertEqual(tc.is_inferred, tc_restored.is_inferred)
        self.assertEqual(tc.digest, tc_restored.digest)

    def test_roundtrip_to_json(self) -> None:
        tc = TaskClassification(task_id="t_json_01", task_type="refactor")
        raw_json = tc.to_json()
        parsed = json.loads(raw_json)
        tc_restored = TaskClassification.from_dict(parsed)
        self.assertEqual(tc.digest, tc_restored.digest)

    def test_from_dict_rejects_unknown_fields(self) -> None:
        payload = {"task_id": "t_unknown", "extra_injected": "malicious"}
        with self.assertRaises(TaskClassificationValidationError):
            TaskClassification.from_dict(payload)


class TestDeterministicTaskClassifier(unittest.TestCase):
    """Test classifier execution, explicit precedence, inference rules, and determinism."""

    def setUp(self) -> None:
        self.classifier = DeterministicTaskClassifier()

    def test_classifier_version_validation(self) -> None:
        with self.assertRaises(TaskClassificationValidationError):
            DeterministicTaskClassifier(classifier_version=0)
        with self.assertRaises(TaskClassificationValidationError):
            DeterministicTaskClassifier(classifier_version=-1)
        with self.assertRaises(TaskClassificationValidationError):
            DeterministicTaskClassifier(classifier_version="1")  # type: ignore

    def test_pure_determinism_same_task(self) -> None:
        task = Task(
            task_id="t_det_01",
            objective="Write unit tests for the authentication module",
            description="Use pytest to verify token handling",
        )
        res1 = self.classifier.classify(task)
        res2 = self.classifier.classify(task)
        self.assertEqual(res1, res2)
        self.assertEqual(res1.digest, res2.digest)

    def test_explicit_task_type_and_domain_preserved(self) -> None:
        task = Task(
            task_id="t_explicit_01",
            objective="Inspect the codebase",
            task_type="documentation",
            domain="frontend",
            requested_skills=("markdown", "html"),
        )
        result = self.classifier.classify(task)
        self.assertEqual(result.task_type, "documentation")
        self.assertEqual(result.domain, "frontend")
        self.assertIn("markdown", result.skills)
        self.assertIn("html", result.skills)
        self.assertEqual(result.confidence, ClassificationConfidence.EXPLICIT)
        self.assertIn("explicit:task_type:documentation", result.evidence)
        self.assertIn("explicit:domain:frontend", result.evidence)

    def test_explicit_enum_task_type_supported(self) -> None:
        task = Task(
            task_id="t_enum_01",
            objective="Run unit tests",
            task_type=SampleEnum.TESTING,
        )
        result = self.classifier.classify(task)
        self.assertEqual(result.task_type, "testing")
        self.assertIn("explicit:task_type:testing", result.evidence)

    def test_infer_testing_task_type(self) -> None:
        task = Task(
            task_id="t_inf_test",
            objective="Add comprehensive unit tests for the user service",
            description="Verify test coverage using pytest and assert outputs",
        )
        result = self.classifier.classify(task)
        self.assertEqual(result.task_type, "testing")
        self.assertTrue(result.is_inferred)
        self.assertIn("inferred:task_type:testing", result.evidence)

    def test_infer_debugging_task_type(self) -> None:
        task = Task(
            task_id="t_inf_debug",
            objective="Fix traceback crash in user session loop",
            description="Remediate exception caused by NoneType dereference bug",
        )
        result = self.classifier.classify(task)
        self.assertEqual(result.task_type, "debugging")
        self.assertTrue(result.is_inferred)
        self.assertIn("inferred:task_type:debugging", result.evidence)

    def test_infer_refactor_task_type(self) -> None:
        task = Task(
            task_id="t_inf_refactor",
            objective="Refactor and clean up the database query module",
            description="Restructure and decouple the legacy data layer",
        )
        result = self.classifier.classify(task)
        self.assertEqual(result.task_type, "refactor")
        self.assertTrue(result.is_inferred)

    def test_infer_documentation_task_type(self) -> None:
        task = Task(
            task_id="t_inf_doc",
            objective="Update README and write docstrings for API endpoints",
            description="Improve developer user guide and markdown docs",
        )
        result = self.classifier.classify(task)
        self.assertEqual(result.task_type, "documentation")
        self.assertTrue(result.is_inferred)

    def test_infer_backend_and_database_domains(self) -> None:
        task = Task(
            task_id="t_inf_dom_sql",
            objective="Write postgres migration script for user schema",
            description="Run SQL queries against the database",
        )
        result = self.classifier.classify(task)
        self.assertEqual(result.domain, "database")
        self.assertIn("inferred:domain:database", result.evidence)

    def test_infer_security_domain(self) -> None:
        task = Task(
            task_id="t_inf_dom_sec",
            objective="Audit auth tokens against CVE vulnerability",
            description="Sanitize inputs and check OWASP compliance",
        )
        result = self.classifier.classify(task)
        self.assertEqual(result.domain, "security")
        self.assertIn("inferred:domain:security", result.evidence)

    def test_infer_skills_from_text(self) -> None:
        task = Task(
            task_id="t_inf_skills",
            objective="Build a FastAPI service with Python and Docker",
            description="Use TypeScript and React for the frontend component",
        )
        result = self.classifier.classify(task)
        self.assertIn("fastapi", result.skills)
        self.assertIn("python", result.skills)
        self.assertIn("docker", result.skills)
        self.assertIn("typescript", result.skills)
        self.assertIn("react", result.skills)

    def test_unknown_fallback_for_vague_task(self) -> None:
        task = Task(
            task_id="t_vague_01",
            objective="Do some random work",
            description="No recognizable keywords whatsoever",
        )
        result = self.classifier.classify(task)
        self.assertIsNone(result.task_type)
        self.assertIsNone(result.domain)
        self.assertEqual(result.skills, ())
        self.assertEqual(result.confidence, ClassificationConfidence.UNKNOWN)
        self.assertIn("inferred:task_type:unknown", result.evidence)
        self.assertIn("inferred:domain:unknown", result.evidence)

    def test_convenience_function_matches_class(self) -> None:
        task = Task(
            task_id="t_conv_01",
            objective="Implement new feature for payment service",
        )
        res_class = self.classifier.classify(task)
        res_func = classify_task(task)
        self.assertEqual(res_class, res_func)
        self.assertEqual(res_class.digest, res_func.digest)


class TestTaskClassificationSecurityBoundary(unittest.TestCase):
    """Test strict security boundaries: zero authority, zero secrets, zero side-effects."""

    def test_authority_shaped_keys_rejected_in_from_dict(self) -> None:
        forbidden = [
            "allow_shell",
            "can_approve",
            "capabilities",
            "permissions",
            "approved_execution_contract",
            "execution_contract",
            "delegation_contract",
            "session_authority",
            "orchestrator_authority",
        ]
        for f in forbidden:
            with self.subTest(forbidden=f):
                payload = {"task_id": "t_auth_smuggle", f: True}
                with self.assertRaises(TaskClassificationAuthorityViolationError):
                    TaskClassification.from_dict(payload)

    def test_authority_shaped_values_rejected_in_fields(self) -> None:
        with self.assertRaises(TaskClassificationAuthorityViolationError):
            TaskClassification(task_id="t1", task_type="allow_shell_exec")

        with self.assertRaises(TaskClassificationAuthorityViolationError):
            TaskClassification(task_id="t2", domain="can_execute_code")

        with self.assertRaises(TaskClassificationAuthorityViolationError):
            TaskClassification(task_id="t3", skills=("grant_write_access",))

        with self.assertRaises(TaskClassificationAuthorityViolationError):
            TaskClassification(task_id="t4", evidence=("capability:root",))

    def test_concrete_secrets_rejected_in_fields(self) -> None:
        # OpenAI token
        with self.assertRaises(TaskClassificationSecretExposureError):
            TaskClassification(task_id="t1", task_type=_MOCK_SK)

        # GitHub token
        with self.assertRaises(TaskClassificationSecretExposureError):
            TaskClassification(task_id="t2", domain=_MOCK_GH)

        # Telegram bot token
        with self.assertRaises(TaskClassificationSecretExposureError):
            TaskClassification(task_id="t3", skills=(_MOCK_TG,))

        # Bearer token
        with self.assertRaises(TaskClassificationSecretExposureError):
            TaskClassification(task_id="t4", evidence=(_MOCK_BEARER,))

        # PEM Private key
        with self.assertRaises(TaskClassificationSecretExposureError):
            TaskClassification(task_id="t5", evidence=(_MOCK_PK,))

        # Secret assignment
        with self.assertRaises(TaskClassificationSecretExposureError):
            TaskClassification(task_id="t6", skills=(_MOCK_ASSIGN,))

    def test_task_classification_has_zero_execution_attributes(self) -> None:
        tc = TaskClassification(task_id="t_sec_clean")
        forbidden_attrs = [
            "execute", "run", "approve", "delegate", "capability", "capabilities",
            "contract", "execution_contract", "permission", "permissions", "token",
            "secret", "authority", "channel", "session",
        ]
        for attr in forbidden_attrs:
            self.assertFalse(hasattr(tc, attr))

    def test_static_architecture_zero_execution_imports(self) -> None:
        target_path = Path(tc_mod.__file__)
        tree = ast.parse(target_path.read_text(encoding="utf-8"))

        forbidden_modules = {
            "subprocess",
            "os.system",
            "orchestrator",
            "urllib",
            "requests",
            "openai",
            "system1",
            "system2",
            "core.runtime.capabilities",
            "core.runtime.delegation",
            "core.runtime.contract",
            "core.runtime.agent_profile",
            "core.runtime.agent_profile_registry",
        }

        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_modules.add(node.module)

        for mod in imported_modules:
            for forbidden in forbidden_modules:
                self.assertFalse(
                    mod == forbidden or mod.startswith(f"{forbidden}."),
                    f"Forbidden execution/routing/network module '{mod}' imported in task_classification.py",
                )


class TestTaskClassificationAdversarialAndProperty(unittest.TestCase):
    """Property-style and adversarial robustness tests for TaskClassification."""

    def setUp(self) -> None:
        self.classifier = DeterministicTaskClassifier()

    def test_property_deterministic_identity_repetition(self) -> None:
        """Property: classify(task) == classify(task) for all tasks across repeated calls."""
        tasks = [
            Task(task_id="t_prop_1", objective="Implement auth endpoint", task_type="implementation"),
            Task(task_id="t_prop_2", objective="Fix memory leak in websocket server", domain="backend"),
            Task(task_id="t_prop_3", objective="Run unit tests and verify coverage", requested_skills=("python", "pytest")),
            Task(task_id="t_prop_4", objective="Vague unknown goal"),
        ]
        for task in tasks:
            for _ in range(5):
                res1 = self.classifier.classify(task)
                res2 = self.classifier.classify(task)
                self.assertEqual(res1, res2)
                self.assertEqual(res1.digest, res2.digest)

    def test_property_shuffled_skills_invariance(self) -> None:
        """Property: Skill ordering in Task.requested_skills does not change output skills or digest."""
        t1 = Task(task_id="t_shuf_1", objective="Obj", requested_skills=("python", "fastapi", "docker"))
        t2 = Task(task_id="t_shuf_1", objective="Obj", requested_skills=("docker", "python", "fastapi"))
        t3 = Task(task_id="t_shuf_1", objective="Obj", requested_skills=("fastapi", "docker", "python"))

        c1 = self.classifier.classify(t1)
        c2 = self.classifier.classify(t2)
        c3 = self.classifier.classify(t3)

        self.assertEqual(c1.skills, c2.skills)
        self.assertEqual(c2.skills, c3.skills)
        self.assertEqual(c1.digest, c2.digest)
        self.assertEqual(c2.digest, c3.digest)

    def test_property_case_equivalence_invariance(self) -> None:
        """Property: Input casing variations produce identical normalized classifications."""
        t_lower = Task(task_id="t_case_1", objective="Obj", task_type="testing", domain="backend")
        t_upper = Task(task_id="t_case_1", objective="Obj", task_type="TESTING", domain="BACKEND")
        t_mixed = Task(task_id="t_case_1", objective="Obj", task_type="Testing", domain="BackEnd")

        c_lower = self.classifier.classify(t_lower)
        c_upper = self.classifier.classify(t_upper)
        c_mixed = self.classifier.classify(t_mixed)

        self.assertEqual(c_lower.task_type, "testing")
        self.assertEqual(c_upper.task_type, "testing")
        self.assertEqual(c_mixed.task_type, "testing")

        self.assertEqual(c_lower.domain, "backend")
        self.assertEqual(c_upper.domain, "backend")
        self.assertEqual(c_mixed.domain, "backend")

        self.assertEqual(c_lower.digest, c_upper.digest)
        self.assertEqual(c_upper.digest, c_mixed.digest)

    def test_benign_task_metadata_isolated_from_classification(self) -> None:
        """Task metadata and input_context do not pollute classification domain fields."""
        task = Task(
            task_id="t_meta_iso",
            objective="Analyze performance bottlenecks",
            metadata={"token_count": 100, "build_number": 42},
            input_context={"rate_limit": {"token_budget": 5000}},
        )
        tc = self.classifier.classify(task)
        self.assertFalse(hasattr(tc, "metadata"))
        self.assertFalse(hasattr(tc, "input_context"))
        self.assertEqual(tc.task_type, "analysis")

    def test_pathological_string_bounds_handled_deterministically(self) -> None:
        """Classification handles bounded long objectives and descriptions without crashing."""
        long_obj = "build " + ("feature " * 100)
        long_desc = "detail " + ("description " * 100)
        task = Task(task_id="t_patho_1", objective=long_obj, description=long_desc)
        tc = self.classifier.classify(task)
        self.assertEqual(tc.task_type, "implementation")
        self.assertTrue(len(tc.digest) == 64)


if __name__ == "__main__":
    unittest.main()

