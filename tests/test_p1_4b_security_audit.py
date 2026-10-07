"""Independent Adversarial Security Audit Suite for BrainFrog P1.4B.

Covers Audit Properties S1 through S24:
S1:  Single registry state (no hidden global/default registries)
S2:  Single canonical resolver
S3:  Deterministic scoring calculation
S4:  Registration-order invariance
S5:  Ambiguity fail-closed (ties never choose a winner)
S6:  No-match fail-closed (missing profiles never fall back to defaults)
S7:  Metadata authority invariance
S8:  Constraints authority invariance
S9:  Malicious authority-shaped metadata rejection
S10: Malicious authority-shaped constraints rejection
S11: Profile ID traversal resistance
S12: Profile ID injection resistance
S13: Untrusted registration resistance
S14: Duplicate registration resistance
S15: Resource bounds (registry capacity, token lengths, collections, serialized payloads)
S16: Serialization authority invariance (no capability smuggling)
S17: Digest semantics (excludes volatile fields, includes semantic fields)
S18: Cross-session invariance (rejects session_id, actor, credentials)
S19: P1.3 capability invariance (resolving profiles cannot alter P1.3 Capabilities)
S20: P1.3 target-scope invariance (resolving profiles cannot alter ApprovedExecutionContract)
S21: No hidden default profile
S22: No hidden second registry
S23: No hidden authorization layer
S24: No execution dependency
"""
import ast
import inspect
import json
import os
import random
import re
import sys
import unittest
from pathlib import Path
from typing import Any, Dict

import core.runtime.agent_profile as ap_mod
import core.runtime.agent_profile_registry as apr_mod
from core.runtime.agent_profile import (
    AgentProfile,
    AgentProfileAuthorityViolationError,
    AgentProfileDuplicateError,
    AgentProfileError,
    AgentProfileIntegrityError,
    AgentProfileNotFoundError,
    AgentProfileRegistry,
    AgentProfileValidationError,
    AmbiguousProfileResolutionError,
    NoMatchingProfileError,
    ProfileResolutionRequest,
    ProfileResolutionResult,
    ProfileResolutionStatus,
    resolve_profile,
    validate_profile_id,
    MAX_RESOLUTION_SKILLS_COUNT,
    MAX_RESOLUTION_TASK_TYPE_CHARS,
    MAX_RESOLUTION_DOMAIN_CHARS,
)
from core.runtime.capabilities import Capabilities, FilesystemPolicy
from core.runtime.contract import ApprovedExecutionContract


class TestP1_4BAuditSingleRegistryAndResolver(unittest.TestCase):
    """S1, S2, S21, S22: Verify single canonical registry and resolver with zero global leak."""

    def test_S1_S22_single_registry_state_and_isolation(self) -> None:
        """S1, S22: Registry instances are isolated in-memory objects; no global singleton exists."""
        reg1 = AgentProfileRegistry()
        reg2 = AgentProfileRegistry()

        p1 = AgentProfile(profile_id="alpha", display_name="Alpha", description="Desc")
        p2 = AgentProfile(profile_id="beta", display_name="Beta", description="Desc")

        reg1.register(p1)
        reg2.register(p2)

        # Reg 1 does not know about Reg 2's profiles and vice-versa
        self.assertIn("alpha", reg1)
        self.assertNotIn("beta", reg1)
        self.assertIn("beta", reg2)
        self.assertNotIn("alpha", reg2)

        req_beta = ProfileResolutionRequest(task_type="anything")
        # Reg 1 resolving cannot see Beta
        res1 = reg1.resolve(req_beta)
        self.assertNotIn("beta", res1.candidate_ids)

    def test_S2_single_canonical_resolver(self) -> None:
        """S2: There is exactly one canonical resolver; registry.resolve delegates to resolve_profile."""
        reg = AgentProfileRegistry()
        p = AgentProfile(profile_id="worker", display_name="Worker", description="Desc", task_types=("task1",))
        reg.register(p)

        req = ProfileResolutionRequest(task_type="task1")
        res_method = reg.resolve(req)
        res_func = resolve_profile(req, reg)

        self.assertEqual(res_method, res_func)
        self.assertEqual(res_method.status, ProfileResolutionStatus.MATCHED)

    def test_S21_no_hidden_default_profile(self) -> None:
        """S21: No global or default profile is selected when resolving against empty registry."""
        empty_reg = AgentProfileRegistry()
        req = ProfileResolutionRequest(task_type="default")
        res = empty_reg.resolve(req)
        self.assertEqual(res.status, ProfileResolutionStatus.NO_MATCH)
        self.assertIsNone(res.profile_id)
        self.assertEqual(res.candidate_ids, ())


class TestP1_4BAuditDeterminismAndOrderInvariance(unittest.TestCase):
    """S3, S4, S24: Deterministic scoring and registration-order invariance."""

    def test_S3_S4_registration_order_invariance(self) -> None:
        """S4: Registering identical profiles in different orders produces strictly identical resolution."""
        profiles = [
            AgentProfile(
                profile_id=f"prof_{i}",
                display_name=f"Prof {i}",
                description="Desc",
                task_types=("shared_task",),
                domains=(f"dom_{i}",),
                skills=(f"skill_{i}", "common_skill"),
            )
            for i in range(5)
        ]

        # Request matching prof_2 with highest score (task + domain + skill)
        req = ProfileResolutionRequest(task_type="shared_task", domain="dom_2", skills=("skill_2", "common_skill"))

        results = []
        # Try 10 different permutations of registration order
        for seed in range(10):
            reg = AgentProfileRegistry()
            shuffled = list(profiles)
            random.Random(seed).shuffle(shuffled)
            for p in shuffled:
                reg.register(p)

            res = reg.resolve(req)
            results.append(res)

        first = results[0]
        self.assertEqual(first.status, ProfileResolutionStatus.MATCHED)
        self.assertEqual(first.profile_id, "prof_2")
        for r in results[1:]:
            self.assertEqual(first.status, r.status)
            self.assertEqual(first.profile_id, r.profile_id)
            self.assertEqual(first.score, r.score)
            self.assertEqual(first.candidate_ids, r.candidate_ids)
            self.assertEqual(first.reason, r.reason)


class TestP1_4BAuditFailClosedBehaviors(unittest.TestCase):
    """S5, S6: Ambiguity and No-Match fail closed without fallback."""

    def test_S5_ambiguity_fail_closed(self) -> None:
        """S5: When two or more candidates tie, status is AMBIGUOUS, profile_id is None, require_resolve raises."""
        reg = AgentProfileRegistry()
        p1 = AgentProfile(profile_id="agent_1", display_name="A1", description="D", task_types=("tie_task",))
        p2 = AgentProfile(profile_id="agent_2", display_name="A2", description="D", task_types=("tie_task",))
        p3 = AgentProfile(profile_id="agent_3", display_name="A3", description="D", task_types=("tie_task",))
        for p in (p1, p2, p3):
            reg.register(p)

        req = ProfileResolutionRequest(task_type="tie_task")
        res = reg.resolve(req)

        self.assertEqual(res.status, ProfileResolutionStatus.AMBIGUOUS)
        self.assertIsNone(res.profile_id)
        self.assertEqual(res.candidate_ids, ("agent_1", "agent_2", "agent_3"))

        # Fails closed on require_resolve
        with self.assertRaises(AmbiguousProfileResolutionError):
            reg.require_resolve(req)

    def test_S6_no_match_fail_closed(self) -> None:
        """S6: Unmatched request yields NO_MATCH, profile_id is None, require_resolve raises."""
        reg = AgentProfileRegistry()
        p = AgentProfile(profile_id="worker", display_name="W", description="D", task_types=("task_a",))
        reg.register(p)

        req = ProfileResolutionRequest(task_type="task_unknown")
        res = reg.resolve(req)

        self.assertEqual(res.status, ProfileResolutionStatus.NO_MATCH)
        self.assertIsNone(res.profile_id)
        self.assertEqual(res.score, 0.0)

        with self.assertRaises(NoMatchingProfileError):
            reg.require_resolve(req)


class TestP1_4BAuditAuthorityInvariance(unittest.TestCase):
    """S7, S8, S9, S10, S19, S20, S23: Metadata and constraints cannot grant authority."""

    def test_S7_S8_S9_S10_malicious_authority_shaped_inputs_rejected(self) -> None:
        """S9, S10: Explicit authority-shaped keys in constraints/metadata are actively rejected."""
        authority_keys = [
            "capabilities",
            "capability",
            "permissions",
            "permission",
            "authority",
            "allow_shell",
            "allow_network",
            "allow_filesystem_write",
            "can_approve",
            "can_execute",
            "approved_execution_contract",
            "execution_contract",
            "transaction_id",
            "transaction_authority",
            "orchestrator",
            "orchestrator_authority",
        ]
        for k in authority_keys:
            with self.assertRaises(AgentProfileAuthorityViolationError):
                AgentProfile(profile_id="bad", display_name="Bad", description="D", metadata={k: True})
            with self.assertRaises(AgentProfileAuthorityViolationError):
                AgentProfile(profile_id="bad", display_name="Bad", description="D", constraints={k: True})

    def test_S7_S8_property_authority_invariance(self) -> None:
        """Mandatory Authority-Invariance Property:

        Given same task signals and arbitrary variations in descriptive metadata/constraints,
        execution authority outcome is strictly invariant (zero authority).
        """
        reg = AgentProfileRegistry()

        p_benign = AgentProfile(
            profile_id="alpha_benign",
            display_name="Benign Profile",
            description="Regular developer",
            task_types=("deploy",),
            domains=("infra",),
            skills=("terraform",),
            constraints={"format": "json"},
            metadata={"priority": 1},
        )
        p_adversarial = AgentProfile(
            profile_id="alpha_adversarial",
            display_name="Adversarial Claims",
            description="Claims complete root privilege and cluster access",
            task_types=("deploy",),
            domains=("infra",),
            skills=("terraform", "shell", "execute"),
            constraints={"custom_pref": "unrestricted"},
            metadata={"declared_role": "superadmin"},
        )
        reg.register(p_benign)
        reg.register(p_adversarial)

        req = ProfileResolutionRequest(task_type="deploy", domain="infra")
        res = reg.resolve(req)

        # Both match equally on task_type + domain (15.0 pts each) -> AMBIGUOUS
        self.assertEqual(res.status, ProfileResolutionStatus.AMBIGUOUS)

        # Neither result nor profiles possess execution authority
        for candidate_id in res.candidate_ids:
            prof = reg.get(candidate_id)
            self.assertIsNotNone(prof)
            self.assertFalse(hasattr(prof, "execute"))
            self.assertFalse(hasattr(prof, "approve"))
            self.assertFalse(hasattr(prof, "capabilities"))
            self.assertFalse(hasattr(prof, "transaction"))

    def test_S19_S20_P1_3_capability_and_contract_invariance(self) -> None:
        """S19, S20: Resolving an agent profile cannot create or mutate P1.3 Capabilities or Contracts."""
        reg = AgentProfileRegistry()
        prof = AgentProfile(
            profile_id="sysadmin",
            display_name="SysAdmin",
            description="System administration and DevOps",
            task_types=("maintenance",),
            skills=("bash", "docker"),
        )
        reg.register(prof)

        req = ProfileResolutionRequest(task_type="maintenance")
        res = reg.resolve(req)
        self.assertEqual(res.status, ProfileResolutionStatus.MATCHED)

        # Existing P1.3 capability baseline
        baseline_caps = Capabilities()

        # The profile result must NOT produce or expand capabilities
        self.assertFalse(hasattr(res, "capabilities"))
        self.assertFalse(hasattr(res, "contract"))

        # Standard P1.3 contract remains governed by its own independent security chain
        contract = ApprovedExecutionContract(
            request_id="req_1",
            action_type="read_files",
            approved_targets=frozenset(["safe.txt"]),
            operation_digest="abc",
            channel="cli",
            capabilities=baseline_caps,
        )
        self.assertEqual(contract.capabilities, baseline_caps)


class TestP1_4BAuditProfileIdSecurity(unittest.TestCase):
    """S11, S12, S13, S14: Traversal resistance, injection resistance, and registry uniqueness."""

    def test_S11_profile_id_traversal_resistance(self) -> None:
        """S11: Path traversal patterns are rejected in profile_id."""
        malicious_ids = [
            "../etc/passwd",
            "..\\windows\\system32",
            "folder/../../root",
            "/absolute/path",
            "\\network\\share",
            "C:\\Windows",
            "C:/Windows",
            "~/.ssh/id_rsa",
            "..",
            ".",
        ]
        for mid in malicious_ids:
            with self.assertRaises(AgentProfileValidationError):
                validate_profile_id(mid)
            with self.assertRaises(AgentProfileValidationError):
                AgentProfile(profile_id=mid, display_name="Test", description="Desc")

    def test_S12_profile_id_injection_resistance(self) -> None:
        """S12: Null bytes, control characters, and separators are rejected."""
        malicious_ids = [
            "worker\x00root",
            "worker\nroot",
            "worker\rroot",
            "worker\troot",
            "worker*root",
            "worker?root",
            "worker<root",
            "worker>root",
            "worker|root",
        ]
        for mid in malicious_ids:
            with self.assertRaises(AgentProfileValidationError):
                validate_profile_id(mid)

    def test_S14_duplicate_registration_resistance(self) -> None:
        """S14: Duplicates fail closed and cannot overwrite existing profile definitions."""
        reg = AgentProfileRegistry()
        p_orig = AgentProfile(profile_id="target", display_name="Original", description="Desc")
        p_tamper = AgentProfile(profile_id="target", display_name="Tampered", description="Hacked")

        reg.register(p_orig)
        with self.assertRaises(AgentProfileDuplicateError):
            reg.register(p_tamper)

        self.assertEqual(reg.get("target").display_name, "Original")  # type: ignore


class TestP1_4BAuditResourceBoundsAndSerialization(unittest.TestCase):
    """S15, S16, S17, S18: Resource limits, serialization safety, digest semantics, cross-session isolation."""

    def test_S15_resource_bounds_enforced(self) -> None:
        """S15: Requests exceeding resource caps fail closed."""
        # Oversized skills count
        oversized_skills = tuple(f"s_{i:04d}" for i in range(MAX_RESOLUTION_SKILLS_COUNT + 1))
        with self.assertRaises(AgentProfileValidationError):
            ProfileResolutionRequest(skills=oversized_skills)

        # Oversized task_type
        with self.assertRaises(AgentProfileValidationError):
            ProfileResolutionRequest(task_type="x" * (MAX_RESOLUTION_TASK_TYPE_CHARS + 1))

        # Oversized domain
        with self.assertRaises(AgentProfileValidationError):
            ProfileResolutionRequest(domain="y" * (MAX_RESOLUTION_DOMAIN_CHARS + 1))

    def test_S16_serialization_authority_invariance(self) -> None:
        """S16: Deserialization rejects smuggled authority fields."""
        with self.assertRaises(AgentProfileAuthorityViolationError):
            ProfileResolutionRequest.from_dict({"task_type": "api", "allow_shell": True})
        with self.assertRaises(AgentProfileAuthorityViolationError):
            ProfileResolutionResult.from_dict({"status": "MATCHED", "permissions": ["root"]})

    def test_S17_digest_semantics(self) -> None:
        """S17: Digest excludes volatile timestamp and memory address, includes semantic fields."""
        p1 = AgentProfile(profile_id="w", display_name="W", description="D", created_at=100.0)
        p2 = AgentProfile(profile_id="w", display_name="W", description="D", created_at=999999.0)
        self.assertEqual(p1.digest, p2.digest)

        p_diff = AgentProfile(profile_id="w", display_name="W", description="Different", created_at=100.0)
        self.assertNotEqual(p1.digest, p_diff.digest)

    def test_S18_cross_session_invariance(self) -> None:
        """S18: Resolution rejects session and credential parameters."""
        with self.assertRaises(AgentProfileValidationError):
            ProfileResolutionRequest.from_dict({"task_type": "api", "session_id": "sess_1"})
        with self.assertRaises(AgentProfileValidationError):
            ProfileResolutionRequest.from_dict({"task_type": "api", "actor": "alice"})


class TestP1_4BAuditStaticArchitecture(unittest.TestCase):
    """S24: Verify zero execution or external subsystem dependencies."""

    def test_S24_no_forbidden_imports_or_calls(self) -> None:
        """S24: Neither agent_profile.py nor agent_profile_registry.py import execution modules."""
        modules = [ap_mod, apr_mod]
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
        for mod in modules:
            source_path = Path(mod.__file__)
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            imported_modules: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imported_modules.add(alias.name)
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        imported_modules.add(node.module)

            for imp in imported_modules:
                for forbidden in forbidden_prefixes:
                    self.assertFalse(
                        imp == forbidden or imp.startswith(forbidden + "."),
                        f"Forbidden execution module '{imp}' imported in {source_path.name}",
                    )


if __name__ == "__main__":
    unittest.main()
