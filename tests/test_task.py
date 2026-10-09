"""Unit and Security Test Suite for BrainFrog P1.4C Task Domain Model.

Validates:
- TASK ≠ AUTHORITY separation
- Immutability and freeze semantics
- Task ID path-traversal and injection resistance
- Authority-like key rejection in context and metadata
- Secret scrubbing and credential exposure prevention
- Bounded payload limits and resource constraints
- Deterministic SHA-256 digest calculation and tampering detection
- Serialization roundtrip fidelity
- Static architecture zero-execution-dependency invariants
"""
from __future__ import annotations

import ast
import json
import time
from enum import Enum
from pathlib import Path
from typing import Any, Dict
import unittest

import core.runtime.task as task_mod
from core.runtime.task import (
    CURRENT_TASK_SCHEMA_VERSION,
    MAX_CONTEXT_KEYS,
    MAX_DESCRIPTION_CHARS,
    MAX_DOMAIN_CHARS,
    MAX_METADATA_KEYS,
    MAX_NESTING_DEPTH,
    MAX_OBJECTIVE_CHARS,
    MAX_REQUESTED_SKILLS_COUNT,
    MAX_SKILL_CHARS,
    MAX_STRING_VALUE_CHARS,
    MAX_TASK_ID_CHARS,
    MAX_TASK_SERIALIZED_BYTES,
    MAX_TASK_TYPE_CHARS,
    Task,
    TaskAuthorityViolationError,
    TaskError,
    TaskIntegrityError,
    TaskSecretExposureError,
    TaskValidationError,
    validate_task_id,
)

# Test credential fragments dynamically assembled to avoid scanner false positives
_MOCK_SK = "sk-" + "proj-1234567890abcdef1234567890abcdef"
_MOCK_GH = "gh" + "p_1234567890abcdef1234567890abcdef"
_MOCK_PK = "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0...\n-----END " + "RSA PRIVATE KEY-----"
_MOCK_BEARER = "Bearer " + "eyJh1234567890abcdef"
_MOCK_TG = "123456789:" + "ABCdefGHIjklMNOpqrsTUVwxyz123456789"
_MOCK_ASSIGN = "pass" + "word = supersecretpassword123"


class TestTaskCreationAndValidation(unittest.TestCase):
    """Test standard task creation, default values, bounds, and field normalization."""

    def test_valid_task_minimal(self) -> None:
        t = Task(task_id="task_001", objective="Implement user authentication")
        self.assertEqual(t.task_id, "task_001")
        self.assertEqual(t.id, "task_001")
        self.assertEqual(t.objective, "Implement user authentication")
        self.assertEqual(t.description, "")
        self.assertIsNone(t.task_type)
        self.assertIsNone(t.domain)
        self.assertEqual(t.requested_skills, ())
        self.assertEqual(dict(t.input_context), {})
        self.assertIsNone(t.parent_task_id)
        self.assertEqual(dict(t.metadata), {})
        self.assertEqual(t.schema_version, CURRENT_TASK_SCHEMA_VERSION)
        self.assertIsInstance(t.created_at, float)
        self.assertTrue(len(t.digest) == 64)

    def test_valid_task_all_fields(self) -> None:
        t = Task(
            task_id="task_full_01",
            objective="Build payment gateway connector",
            description="Detailed specifications for payment gateway",
            task_type="IMPLEMENTATION",
            domain="FinTech",
            requested_skills=("python", "fastapi", "stripe"),
            input_context={"currency": "USD", "retry_count": 3},
            parent_task_id="task_parent_01",
            metadata={"priority": "high", "tier": 1},
        )
        self.assertEqual(t.task_id, "task_full_01")
        self.assertEqual(t.task_type, "implementation")  # normalized lower-case
        self.assertEqual(t.domain, "fintech")  # normalized lower-case
        self.assertEqual(t.requested_skills, ("fastapi", "python", "stripe"))  # sorted & normalized
        self.assertEqual(t.input_context["currency"], "USD")
        self.assertEqual(t.input_context["retry_count"], 3)
        self.assertEqual(t.parent_task_id, "task_parent_01")
        self.assertEqual(t.metadata["priority"], "high")

    def test_description_promoted_when_objective_empty(self) -> None:
        t = Task(task_id="task_promo", objective="", description="Run security diagnostics")
        self.assertEqual(t.objective, "Run security diagnostics")
        self.assertEqual(t.description, "Run security diagnostics")

    def test_reject_both_objective_and_description_empty(self) -> None:
        with self.assertRaises(TaskValidationError):
            Task(task_id="task_001", objective="", description="")

    def test_reject_invalid_field_types(self) -> None:
        with self.assertRaises(TaskValidationError):
            Task(task_id=123, objective="Valid objective")  # type: ignore

        with self.assertRaises(TaskValidationError):
            Task(task_id="task_001", objective=None)  # type: ignore

        with self.assertRaises(TaskValidationError):
            Task(task_id="task_001", objective="Obj", description=456)  # type: ignore

        with self.assertRaises(TaskValidationError):
            Task(task_id="task_001", objective="Obj", domain=123)  # type: ignore

        with self.assertRaises(TaskValidationError):
            Task(task_id="task_001", objective="Obj", requested_skills="not-a-list")  # type: ignore

        with self.assertRaises(TaskValidationError):
            Task(task_id="task_001", objective="Obj", input_context=["not-a-dict"])  # type: ignore

        with self.assertRaises(TaskValidationError):
            Task(task_id="task_001", objective="Obj", metadata="not-a-dict")  # type: ignore

    def test_reject_control_characters(self) -> None:
        with self.assertRaises(TaskValidationError):
            Task(task_id="task_001", objective="Bad\x00objective")

        with self.assertRaises(TaskValidationError):
            Task(task_id="task_001", objective="Valid", description="Bad\x08description")

        with self.assertRaises(TaskValidationError):
            Task(task_id="task_001", objective="Valid", task_type="task\x1btype")

        with self.assertRaises(TaskValidationError):
            Task(task_id="task_001", objective="Valid", domain="dom\x7fain")

        with self.assertRaises(TaskValidationError):
            Task(task_id="task_001", objective="Valid", requested_skills=("skill\x03",))

    def test_field_bounds_enforcement(self) -> None:
        # Objective length bound
        with self.assertRaises(TaskValidationError):
            Task(task_id="t1", objective="x" * (MAX_OBJECTIVE_CHARS + 1))

        # Description length bound
        with self.assertRaises(TaskValidationError):
            Task(task_id="t1", objective="Valid", description="x" * (MAX_DESCRIPTION_CHARS + 1))

        # Task type length bound
        with self.assertRaises(TaskValidationError):
            Task(task_id="t1", objective="Valid", task_type="x" * (MAX_TASK_TYPE_CHARS + 1))

        # Domain length bound
        with self.assertRaises(TaskValidationError):
            Task(task_id="t1", objective="Valid", domain="y" * (MAX_DOMAIN_CHARS + 1))

        # Skills count bound
        oversized_skills = tuple(f"skill_{i:04d}" for i in range(MAX_REQUESTED_SKILLS_COUNT + 1))
        with self.assertRaises(TaskValidationError):
            Task(task_id="t1", objective="Valid", requested_skills=oversized_skills)

        # Skill item length bound
        with self.assertRaises(TaskValidationError):
            Task(task_id="t1", objective="Valid", requested_skills=("s" * (MAX_SKILL_CHARS + 1),))

        # Context keys bound
        oversized_ctx = {f"k_{i}": i for i in range(MAX_CONTEXT_KEYS + 1)}
        with self.assertRaises(TaskValidationError):
            Task(task_id="t1", objective="Valid", input_context=oversized_ctx)

        # Metadata keys bound
        oversized_meta = {f"m_{i}": i for i in range(MAX_METADATA_KEYS + 1)}
        with self.assertRaises(TaskValidationError):
            Task(task_id="t1", objective="Valid", metadata=oversized_meta)

    def test_nesting_depth_bound(self) -> None:
        deep_dict: Dict[str, Any] = {"level": 1}
        cur: Dict[str, Any] = deep_dict
        for i in range(2, MAX_NESTING_DEPTH + 2):
            nxt: Dict[str, Any] = {"level": i}
            cur["next"] = nxt
            cur = nxt

        with self.assertRaises(TaskValidationError):
            Task(task_id="t1", objective="Valid", input_context=deep_dict)

    def test_schema_version_and_timestamp_validation(self) -> None:
        with self.assertRaises(TaskValidationError):
            Task(task_id="t1", objective="Valid", schema_version=0)

        with self.assertRaises(TaskValidationError):
            Task(task_id="t1", objective="Valid", schema_version=-1)

        with self.assertRaises(TaskValidationError):
            Task(task_id="t1", objective="Valid", created_at=float("nan"))

        with self.assertRaises(TaskValidationError):
            Task(task_id="t1", objective="Valid", created_at=float("inf"))


class TestTaskIdSecurityAndBoundaries(unittest.TestCase):
    """Test task_id validation against traversal, path injection, drive letters, and invalid chars."""

    def test_valid_task_ids(self) -> None:
        valid_ids = [
            "task_1",
            "task-42",
            "TASK_UPPER_99",
            "subtask.alpha-01",
            "123456",
            "a" * MAX_TASK_ID_CHARS,
        ]
        for tid in valid_ids:
            self.assertEqual(validate_task_id(tid), tid)
            t = Task(task_id=tid, objective="Valid")
            self.assertEqual(t.task_id, tid)

    def test_reject_traversal_ids(self) -> None:
        malicious_ids = [
            "../etc/passwd",
            "..\\windows\\system32",
            "folder/../../root",
            "/absolute/path",
            "\\network\\share",
            "C:\\Windows",
            "C:/Windows",
            "D:\\data",
            "~/.ssh/id_rsa",
            "..",
            ".",
            "...",
        ]
        for mid in malicious_ids:
            with self.assertRaises(TaskValidationError):
                validate_task_id(mid)
            with self.assertRaises(TaskValidationError):
                Task(task_id=mid, objective="Test")

    def test_reject_control_and_injection_characters(self) -> None:
        malicious_ids = [
            "task\x00inject",
            "task\ninject",
            "task\rinject",
            "task\tinject",
            "task*wildcard",
            "task?query",
            "task<redirect",
            "task>redirect",
            "task|pipe",
            "task;command",
            "task:colon",
            "task~tilde",
            "task'quote",
            'task"quote',
        ]
        for mid in malicious_ids:
            with self.assertRaises(TaskValidationError):
                validate_task_id(mid)
            with self.assertRaises(TaskValidationError):
                Task(task_id=mid, objective="Test")

    def test_parent_task_id_validation_and_lineage(self) -> None:
        # Valid parent ID
        t = Task(task_id="child_01", objective="Child task", parent_task_id="parent_01")
        self.assertEqual(t.parent_task_id, "parent_01")

        # Self-parenting forbidden
        with self.assertRaises(TaskValidationError):
            Task(task_id="same_id", objective="Self parent", parent_task_id="same_id")

        # Traversal in parent_task_id rejected
        with self.assertRaises(TaskValidationError):
            Task(task_id="child_01", objective="Child task", parent_task_id="../parent")


class TestTaskImmutabilityAndDeepProtection(unittest.TestCase):
    """Test deep immutability: frozen dataclass, tuple skills, MappingProxyType mappings."""

    def test_frozen_attributes_cannot_be_mutated(self) -> None:
        t = Task(task_id="task_mut", objective="Initial objective")
        with self.assertRaises(Exception):
            t.objective = "Mutated"  # type: ignore
        with self.assertRaises(Exception):
            t.task_id = "new_id"  # type: ignore
        with self.assertRaises(Exception):
            t.schema_version = 2  # type: ignore

    def test_skills_tuple_cannot_be_mutated(self) -> None:
        t = Task(task_id="task_s", objective="Skills", requested_skills=("python", "docker"))
        self.assertIsInstance(t.requested_skills, tuple)
        with self.assertRaises(TypeError):
            t.requested_skills[0] = "bash"  # type: ignore
        with self.assertRaises(AttributeError):
            t.requested_skills.append("bash")  # type: ignore

    def test_input_context_mapping_proxy_cannot_be_mutated(self) -> None:
        t = Task(
            task_id="task_ctx",
            objective="Context",
            input_context={"key": "val", "nested": {"sub": 10}},
        )
        with self.assertRaises(TypeError):
            t.input_context["key"] = "new_val"  # type: ignore
        with self.assertRaises(TypeError):
            t.input_context["nested"]["sub"] = 20  # type: ignore
        with self.assertRaises(TypeError):
            t.input_context["new_key"] = "added"  # type: ignore

    def test_metadata_mapping_proxy_cannot_be_mutated(self) -> None:
        t = Task(
            task_id="task_meta",
            objective="Meta",
            metadata={"tag": "prod", "nested_list": [1, 2, 3]},
        )
        with self.assertRaises(TypeError):
            t.metadata["tag"] = "dev"  # type: ignore
        # Nested list was frozen to tuple
        self.assertIsInstance(t.metadata["nested_list"], tuple)
        with self.assertRaises(TypeError):
            t.metadata["nested_list"][0] = 4  # type: ignore
        with self.assertRaises(AttributeError):
            t.metadata["nested_list"].append(4)  # type: ignore


class TestTaskAuthorityRejection(unittest.TestCase):
    """Test that Task strictly rejects authority-shaped keys and cannot confer authority."""

    def test_reject_authority_shaped_context_keys(self) -> None:
        forbidden_keys = [
            "capabilities",
            "capability",
            "permissions",
            "permission",
            "authority",
            "authorities",
            "allow_shell",
            "allow_network",
            "allow_filesystem_write",
            "allowed_tools",
            "allowed_commands",
            "allowed_files",
            "can_approve",
            "can_execute",
            "approved_execution_contract",
            "execution_contract",
            "delegation_contract",
            "transaction_id",
            "transaction_authority",
            "orchestrator",
            "orchestrator_authority",
            "shell_access",
            "network_access",
        ]
        for k in forbidden_keys:
            with self.assertRaises(TaskAuthorityViolationError):
                Task(task_id="bad_ctx", objective="Obj", input_context={k: True})

            with self.assertRaises(TaskAuthorityViolationError):
                Task(task_id="bad_meta", objective="Obj", metadata={k: "root"})

    def test_reject_smuggled_root_keys_in_from_dict(self) -> None:
        payload = {
            "task_id": "smuggle_01",
            "objective": "Smuggling",
            "capabilities": {"allow_shell": True},
        }
        with self.assertRaises(TaskAuthorityViolationError):
            Task.from_dict(payload)

        payload_perm = {
            "task_id": "smuggle_02",
            "objective": "Smuggling",
            "permissions": ["all"],
        }
        with self.assertRaises(TaskAuthorityViolationError):
            Task.from_dict(payload_perm)

    def test_reject_unknown_extra_fields_in_from_dict(self) -> None:
        payload = {
            "task_id": "unknown_01",
            "objective": "Valid",
            "arbitrary_field": "unrecognized",
        }
        with self.assertRaises(TaskValidationError):
            Task.from_dict(payload)

    def test_task_has_zero_execution_attributes(self) -> None:
        t = Task(
            task_id="task_invariance",
            objective="Build API endpoint",
            task_type="implementation",
            domain="backend",
            requested_skills=("python",),
            input_context={"route": "/api/v1/users"},
            metadata={"audit": "verified"},
        )
        forbidden_attrs = (
            "execute",
            "approve",
            "authorize",
            "run",
            "capabilities",
            "permissions",
            "contract",
            "transaction",
            "orchestrator",
            "tools",
            "commands",
        )
        for attr in forbidden_attrs:
            self.assertFalse(hasattr(t, attr), f"Task should NOT have attribute '{attr}'")


class TestTaskSecretRejection(unittest.TestCase):
    """Test that Task actively rejects secrets and credential exposures across all fields."""

    def test_reject_private_key_patterns(self) -> None:
        pk = _MOCK_PK
        with self.assertRaises(TaskSecretExposureError):
            Task(task_id="task_pk", objective=f"Deploy with {pk}")

        with self.assertRaises(TaskSecretExposureError):
            Task(task_id="task_pk2", objective="Obj", description=f"Details: {pk}")

        with self.assertRaises(TaskSecretExposureError):
            Task(task_id="task_pk3", objective="Obj", input_context={"key": pk})

    def test_reject_token_patterns(self) -> None:
        openai_key = _MOCK_SK
        with self.assertRaises(TaskSecretExposureError):
            Task(task_id="task_token", objective=f"Call API with {openai_key}")

        gh_token = _MOCK_GH
        with self.assertRaises(TaskSecretExposureError):
            Task(task_id="task_gh", objective="Obj", input_context={"gh": gh_token})

    def test_reject_credential_keys_in_context_and_metadata(self) -> None:
        with self.assertRaises(TaskSecretExposureError):
            Task(task_id="t1", objective="Obj", input_context={"api_key": "raw_secret_value"})

        with self.assertRaises(TaskSecretExposureError):
            Task(task_id="t1", objective="Obj", metadata={"password": "supersecretpassword"})

        with self.assertRaises(TaskSecretExposureError):
            Task(task_id="t1", objective="Obj", input_context={"authorization": "Bearer token"})


class TestTaskSerializationAndDigest(unittest.TestCase):
    """Test JSON/dict serialization, deterministic SHA-256 digests, and tampering detection."""

    def test_roundtrip_to_dict_and_from_dict(self) -> None:
        t1 = Task(
            task_id="task_rt",
            objective="Run unit tests",
            description="Run all regression tests",
            task_type="testing",
            domain="qa",
            requested_skills=("pytest", "coverage"),
            input_context={"verbose": True, "filter": "unit"},
            parent_task_id="parent_rt",
            metadata={"build_id": 42},
        )
        d = t1.to_dict()
        self.assertIsInstance(d, dict)
        t2 = Task.from_dict(d)

        self.assertEqual(t1.task_id, t2.task_id)
        self.assertEqual(t1.objective, t2.objective)
        self.assertEqual(t1.description, t2.description)
        self.assertEqual(t1.task_type, t2.task_type)
        self.assertEqual(t1.domain, t2.domain)
        self.assertEqual(t1.requested_skills, t2.requested_skills)
        self.assertEqual(t1.input_context, t2.input_context)
        self.assertEqual(t1.parent_task_id, t2.parent_task_id)
        self.assertEqual(t1.metadata, t2.metadata)
        self.assertEqual(t1.schema_version, t2.schema_version)
        self.assertEqual(t1.created_at, t2.created_at)
        self.assertEqual(t1.digest, t2.digest)

    def test_roundtrip_to_json_and_from_json(self) -> None:
        t1 = Task(
            task_id="task_json",
            objective="Compile assets",
            requested_skills=("webpack",),
            input_context={"minify": True},
        )
        json_str = t1.to_json()
        self.assertIsInstance(json_str, str)
        t2 = Task.from_json(json_str)

        self.assertEqual(t1.task_id, t2.task_id)
        self.assertEqual(t1.objective, t2.objective)
        self.assertEqual(t1.digest, t2.digest)

    def test_digest_deterministic_and_excludes_volatile_timestamp(self) -> None:
        t1 = Task(
            task_id="task_same",
            objective="Deterministic check",
            created_at=1000.0,
        )
        t2 = Task(
            task_id="task_same",
            objective="Deterministic check",
            created_at=9999999.0,
        )
        # Digest must be strictly identical despite different created_at
        self.assertEqual(t1.digest, t2.digest)

    def test_digest_sensitivity_to_semantic_changes(self) -> None:
        base = Task(task_id="task_base", objective="Base objective")

        t_diff_obj = Task(task_id="task_base", objective="Different objective")
        self.assertNotEqual(base.digest, t_diff_obj.digest)

        t_diff_id = Task(task_id="task_other", objective="Base objective")
        self.assertNotEqual(base.digest, t_diff_id.digest)

        t_diff_skill = Task(task_id="task_base", objective="Base objective", requested_skills=("python",))
        self.assertNotEqual(base.digest, t_diff_skill.digest)

        t_diff_ctx = Task(task_id="task_base", objective="Base objective", input_context={"param": 1})
        self.assertNotEqual(base.digest, t_diff_ctx.digest)

    def test_tampering_detection_with_invalid_digest(self) -> None:
        fake_digest = "0" * 64
        with self.assertRaises(TaskIntegrityError):
            Task(task_id="task_tamper", objective="Valid", digest=fake_digest)

    def test_serialized_payload_size_bound(self) -> None:
        # Huge string value exceeding overall serialized task limit
        large_context = {"big_val": "a" * (MAX_TASK_SERIALIZED_BYTES + 100)}
        with self.assertRaises(TaskValidationError):
            Task(task_id="huge_task", objective="Valid", input_context=large_context)


class TestTaskStaticArchitecture(unittest.TestCase):
    """Verify zero execution or runtime engine dependencies in core/runtime/task.py."""

    def test_no_forbidden_execution_imports(self) -> None:
        source_path = Path(task_mod.__file__)
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_modules.add(node.module)

        forbidden_prefixes = (
            "orchestrator",
            "subprocess",
            "os.system",
            "core.runtime.orchestrator",
            "core.runtime.transaction",
            "core.runtime.approval",
            "core.runtime.contract",
            "core.runtime.work",
            "core.runtime.child_work",
            "core.runtime.delegation",
            "core.runtime.subagent_execution",
            "requests",
            "urllib",
            "openai",
            "anthropic",
        )

        for imp in imported_modules:
            for forbidden in forbidden_prefixes:
                self.assertFalse(
                    imp == forbidden or imp.startswith(forbidden + "."),
                    f"Forbidden execution module '{imp}' imported in task.py",
                )


class TestTaskAdversarialAttacksAndDeepNesting(unittest.TestCase):
    """Test sophisticated adversarial attempts to smuggle authority or secrets."""

    def test_nested_authority_smuggling_in_dict_rejected(self) -> None:
        deep_attack = {
            "level1": {
                "level2": {
                    "allow_shell": True,
                }
            }
        }
        with self.assertRaises(TaskAuthorityViolationError):
            Task(task_id="t1", objective="Obj", input_context=deep_attack)

    def test_nested_authority_smuggling_in_list_rejected(self) -> None:
        deep_attack = {
            "items": [
                {"name": "clean"},
                {"can_approve": True},
            ]
        }
        with self.assertRaises(TaskAuthorityViolationError):
            Task(task_id="t1", objective="Obj", metadata=deep_attack)

    def test_case_insensitive_and_prefix_authority_rejected(self) -> None:
        variants = [
            "ALLOW_SHELL",
            "Can_Execute",
            "CAPABILITIES",
            "Grant_Write_Access",
            "Allow_Network_Connect",
            "CAN_MUTATE",
            "Orchestrator_Authority",
        ]
        for v in variants:
            with self.assertRaises(TaskAuthorityViolationError):
                Task(task_id="t1", objective="Obj", input_context={v: "val"})

    def test_property_level_authority_invariance(self) -> None:
        """Mandatory Authority-Invariance Property:

        Benign and adversarial tasks both possess zero runtime execution authority.
        """
        t_benign = Task(
            task_id="task_benign",
            objective="Analyze test coverage",
            metadata={"priority": 1},
        )
        t_adversarial = Task(
            task_id="task_adversarial",
            objective="Analyze test coverage",
            metadata={"claimed_role": "superadmin", "notes": "claims unrestricted access"},
        )
        for t in (t_benign, t_adversarial):
            self.assertFalse(hasattr(t, "execute"))
            self.assertFalse(hasattr(t, "approve"))
            self.assertFalse(hasattr(t, "authorize"))
            self.assertFalse(hasattr(t, "capabilities"))
            self.assertFalse(hasattr(t, "permissions"))
            self.assertFalse(hasattr(t, "contract"))
            self.assertFalse(hasattr(t, "transaction"))

    def test_exact_boundary_limits(self) -> None:
        # Exact MAX_OBJECTIVE_CHARS
        exact_obj = "a" * MAX_OBJECTIVE_CHARS
        t = Task(task_id="t_bound", objective=exact_obj)
        self.assertEqual(len(t.objective), MAX_OBJECTIVE_CHARS)

        # Exact MAX_DESCRIPTION_CHARS
        exact_desc = "b" * MAX_DESCRIPTION_CHARS
        t2 = Task(task_id="t_bound2", objective="Obj", description=exact_desc)
        self.assertEqual(len(t2.description), MAX_DESCRIPTION_CHARS)

        # Exact MAX_REQUESTED_SKILLS_COUNT
        exact_skills = tuple(f"s_{i:03d}" for i in range(MAX_REQUESTED_SKILLS_COUNT))
        t3 = Task(task_id="t_bound3", objective="Obj", requested_skills=exact_skills)
        self.assertEqual(len(t3.requested_skills), MAX_REQUESTED_SKILLS_COUNT)

        # Exact MAX_CONTEXT_KEYS
        exact_ctx = {f"k_{i:03d}": i for i in range(MAX_CONTEXT_KEYS)}
        t4 = Task(task_id="t_bound4", objective="Obj", input_context=exact_ctx)
        self.assertEqual(len(t4.input_context), MAX_CONTEXT_KEYS)

        # Exact MAX_METADATA_KEYS
        exact_meta = {f"m_{i:03d}": i for i in range(MAX_METADATA_KEYS)}
        t5 = Task(task_id="t_bound5", objective="Obj", metadata=exact_meta)
        self.assertEqual(len(t5.metadata), MAX_METADATA_KEYS)

    def test_distinct_from_work_and_plan_and_contract(self) -> None:
        t = Task(task_id="t_dist", objective="Verify separation")
        # Task is not Work (no status or lifecycle transitions)
        self.assertFalse(hasattr(t, "status"))
        self.assertFalse(hasattr(t, "intent"))
        self.assertFalse(hasattr(t, "goal"))
        # Task is not Plan (no plan steps or dependency DAG)
        self.assertFalse(hasattr(t, "steps"))
        self.assertFalse(hasattr(t, "dependencies"))
        # Task is not Execution Contract
        self.assertFalse(hasattr(t, "approved_targets"))
        self.assertFalse(hasattr(t, "channel"))
        self.assertFalse(hasattr(t, "capabilities"))



class TestP14CRemediationF01AndF03(unittest.TestCase):
    """Focused regression test suite for P1.4C remediation findings F-01 and F-03.

    F-01:
    - Enum-based task_type accepts Enum and str instances
    - Normalization to lowercase string remains consistent
    - None remains None
    - Invalid types raise TaskValidationError

    F-03:
    - Descriptive security terminology keys are allowed in metadata and input_context
    - Actual secret-bearing keys remain strictly rejected
    - Concrete secret patterns (tokens, private keys, bearer tokens) remain strictly rejected,
      even when placed under descriptive keys or nested structures.
    """

    class _SampleEnum(Enum):
        CODE_REVIEW = "CODE_REVIEW"
        BUG_FIX = "bug_fix"

    def test_f01_task_type_enum_support_and_normalization(self) -> None:
        """F-01: Enum values are accepted and normalized to lowercase strings."""
        t1 = Task(
            task_id="t_enum_upper",
            objective="Perform review",
            task_type=self._SampleEnum.CODE_REVIEW,
        )
        self.assertEqual(t1.task_type, "code_review")

        t2 = Task(
            task_id="t_enum_lower",
            objective="Fix bug",
            task_type=self._SampleEnum.BUG_FIX,
        )
        self.assertEqual(t2.task_type, "bug_fix")

    def test_f01_task_type_string_normalization_preserved(self) -> None:
        """F-01: String task_type continues to normalize to lowercase."""
        t = Task(
            task_id="t_str",
            objective="Implementation",
            task_type="IMPLEMENTATION",
        )
        self.assertEqual(t.task_type, "implementation")

    def test_f01_task_type_none_preserved(self) -> None:
        """F-01: None task_type remains None."""
        t = Task(task_id="t_none", objective="No task type", task_type=None)
        self.assertIsNone(t.task_type)

    def test_f01_task_type_invalid_type_rejected(self) -> None:
        """F-01: Non-string and non-enum task_type inputs are rejected."""
        with self.assertRaises(TaskValidationError):
            Task(task_id="t_bad_int", objective="Obj", task_type=123)  # type: ignore

        with self.assertRaises(TaskValidationError):
            Task(task_id="t_bad_list", objective="Obj", task_type=["testing"])  # type: ignore

    def test_f03_descriptive_metadata_keys_allowed(self) -> None:
        """F-03: Descriptive security keys with benign values are allowed."""
        t = Task(
            task_id="t_desc_meta",
            objective="Process LLM request",
            metadata={
                "token_count": 150,
                "token_budget": 4096,
                "credential_type": "oauth2",
                "password_policy": "min_8",
                "authentication_method": "bearer",
            },
        )
        self.assertEqual(t.metadata["token_count"], 150)
        self.assertEqual(t.metadata["token_budget"], 4096)
        self.assertEqual(t.metadata["credential_type"], "oauth2")
        self.assertEqual(t.metadata["password_policy"], "min_8")
        self.assertEqual(t.metadata["authentication_method"], "bearer")

    def test_f03_descriptive_input_context_keys_allowed(self) -> None:
        """F-03: Descriptive security keys in input_context are allowed."""
        t = Task(
            task_id="t_desc_ctx",
            objective="Run auth pipeline",
            input_context={
                "token_count": 50,
                "credential_type": "service_account",
                "authentication_method": "mtls",
                "password_policy": "strict_16",
            },
        )
        self.assertEqual(t.input_context["token_count"], 50)
        self.assertEqual(t.input_context["credential_type"], "service_account")

    def test_f03_nested_descriptive_keys_allowed(self) -> None:
        """F-03: Nested descriptive metadata and input_context keys are allowed."""
        t = Task(
            task_id="t_nested_desc",
            objective="Nested test",
            input_context={
                "runtime_config": {
                    "limits": {
                        "token_count": 256,
                        "token_budget": 8192,
                    },
                    "auth": {
                        "credential_type": "api_key",
                        "authentication_method": "header",
                    },
                }
            },
            metadata={
                "security_rules": {
                    "password_policy": "min_8",
                }
            },
        )
        self.assertEqual(t.input_context["runtime_config"]["limits"]["token_count"], 256)
        self.assertEqual(t.metadata["security_rules"]["password_policy"], "min_8")

    def test_f03_secret_designated_keys_rejected(self) -> None:
        """F-03: Actual secret-designated keys remain strictly rejected."""
        secret_keys = [
            ("api_key", "sk-test1234567890"),
            ("access_token", "real_access_token_val"),
            ("auth_token", "real_auth_token_val"),
            ("password", "actual_password_string"),
            ("credential", "actual_credential_string"),
            ("client_secret", "actual_client_secret_xyz"),
        ]
        for key, val in secret_keys:
            with self.subTest(key=key):
                with self.assertRaises(TaskSecretExposureError):
                    Task(
                        task_id=f"t_sec_{key}",
                        objective="Secret leak attempt",
                        metadata={key: val},
                    )

                with self.assertRaises(TaskSecretExposureError):
                    Task(
                        task_id=f"t_sec_ctx_{key}",
                        objective="Secret leak attempt in context",
                        input_context={key: val},
                    )

    def test_f03_nested_secret_designated_keys_rejected(self) -> None:
        """F-03: Nested secret-designated keys remain strictly rejected."""
        with self.assertRaises(TaskSecretExposureError):
            Task(
                task_id="t_nested_sec_ctx",
                objective="Nested leak",
                input_context={"config": {"auth": {"api_key": "some_secret_key"}}},
            )

        with self.assertRaises(TaskSecretExposureError):
            Task(
                task_id="t_nested_sec_meta",
                objective="Nested leak in meta",
                metadata={"credentials": {"user_password": "supersecret"}},
            )

    def test_f03_concrete_secrets_under_descriptive_keys_rejected(self) -> None:
        """F-03: Concrete secret patterns disguised under descriptive keys are rejected."""
        # 1. OpenAI-style token
        with self.assertRaises(TaskSecretExposureError):
            Task(
                task_id="t_sec_pat_1",
                objective="Smuggle token",
                metadata={"token_count": _MOCK_SK},
            )

        # 2. GitHub token
        with self.assertRaises(TaskSecretExposureError):
            Task(
                task_id="t_sec_pat_2",
                objective="Smuggle token",
                input_context={"token_budget": _MOCK_GH},
            )

        # 3. Telegram bot token
        with self.assertRaises(TaskSecretExposureError):
            Task(
                task_id="t_sec_pat_3",
                objective="Smuggle token",
                metadata={"credential_type": _MOCK_TG},
            )

        # 4. Bearer token
        with self.assertRaises(TaskSecretExposureError):
            Task(
                task_id="t_sec_pat_4",
                objective="Smuggle token",
                input_context={"authentication_method": _MOCK_BEARER},
            )

        # 5. Private key PEM material
        with self.assertRaises(TaskSecretExposureError):
            Task(
                task_id="t_sec_pat_5",
                objective="Smuggle key",
                metadata={"password_policy": _MOCK_PK},
            )

        # 6. Secret assignment pattern
        with self.assertRaises(TaskSecretExposureError):
            Task(
                task_id="t_sec_pat_6",
                objective="Smuggle assignment",
                metadata={"password_policy": _MOCK_ASSIGN},
            )

    def test_f03_nested_concrete_secrets_under_descriptive_keys_rejected(self) -> None:
        """F-03: Concrete secrets under nested descriptive keys are rejected."""
        with self.assertRaises(TaskSecretExposureError):
            Task(
                task_id="t_nested_sec_disguise",
                objective="Nested secret disguised",
                input_context={
                    "layer1": {
                        "layer2": {
                            "token_count": _MOCK_SK,
                        }
                    }
                },
            )


if __name__ == "__main__":
    unittest.main()
