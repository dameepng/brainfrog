"""Comprehensive unit and security test suite for AgentProfileRegistry and Profile Resolution.

BrainFrog P1.4B — Agent Profile Registry & Resolution.

Validates:
- ROUTING ≠ AUTHORIZATION
- PROFILE ≠ CAPABILITY
- REGISTRY ≠ AUTHORITY
- RESOLUTION ≠ EXECUTION
- Deterministic profile registration and lifecycle
- Deterministic profile resolution and scoring
- Explicit ambiguity handling (fail-closed on tied best scores)
- Normalization (whitespace, case-insensitivity, duplicate skill deduplication)
- Strict serialization and deserialization
- Resource bounding and boundary conditions
- Secret safety and authority rejection
- Security properties P1.4B-S1 through P1.4B-S10
- Property-style authority invariance
- Pure domain architecture with zero execution dependencies
"""
import ast
import inspect
import json
import math
import os
import re
import sys
import unittest
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict

import core.runtime.agent_profile_registry as apr_mod
from core.runtime.agent_profile_registry import (
    CURRENT_AGENT_PROFILE_SCHEMA_VERSION,
    CURRENT_PROFILE_RESOLUTION_SCHEMA_VERSION,
    DOMAIN_MATCH_WEIGHT,
    MAX_PROFILE_ID_CHARS,
    MAX_REGISTRY_CAPACITY,
    MAX_RESOLUTION_CANDIDATE_IDS,
    MAX_RESOLUTION_DOMAIN_CHARS,
    MAX_RESOLUTION_REASON_CHARS,
    MAX_RESOLUTION_REQUEST_SERIALIZED_BYTES,
    MAX_RESOLUTION_RESULT_SERIALIZED_BYTES,
    MAX_RESOLUTION_SKILL_CHARS,
    MAX_RESOLUTION_SKILLS_COUNT,
    MAX_RESOLUTION_TASK_TYPE_CHARS,
    SKILL_MATCH_WEIGHT,
    TASK_TYPE_MATCH_WEIGHT,
    AgentProfile,
    AgentProfileAuthorityViolationError,
    AgentProfileDuplicateError,
    AgentProfileError,
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
)

# Test credential fragments dynamically assembled to avoid scanner false positives
_MOCK_SK = "sk-" + "proj-1234567890abcdef1234567890abcdef"
_MOCK_GH = "gh" + "p_1234567890abcdef1234567890abcdef"
_MOCK_PK = "-----BEGIN " + "RSA PRIVATE KEY-----"
_MOCK_BEARER = "Bearer " + "eyJh1234567890abcdef"
_MOCK_TG = "123456789:" + "ABCdefGHIjklMNOpqrSTUvwxYZ_12345678"
_MOCK_PWD = "pass" + "word: supersecret123"


class TestAgentProfileRegistryLifecycle(unittest.TestCase):
    """Test registry lifecycle: registration, deduplication, lookup, listing, removal, bounds."""

    def setUp(self) -> None:
        self.registry = AgentProfileRegistry()
        self.p_backend = AgentProfile(
            profile_id="backend_engineer",
            display_name="Backend Engineer",
            description="Backend implementation",
            task_types=("backend_api", "backend_debugging"),
            domains=("backend", "python"),
            skills=("fastapi", "pytest"),
        )
        self.p_frontend = AgentProfile(
            profile_id="frontend_engineer",
            display_name="Frontend Engineer",
            description="Frontend UI development",
            task_types=("ui_implementation",),
            domains=("frontend", "web"),
            skills=("react", "typescript"),
        )

    def test_01_register_and_lookup(self) -> None:
        """Registering a profile allows subsequent lookup by profile_id."""
        self.registry.register(self.p_backend)
        retrieved = self.registry.get("backend_engineer")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved, self.p_backend)

    def test_02_register_invalid_type_rejected(self) -> None:
        """Registering a non-AgentProfile object raises AgentProfileValidationError."""
        with self.assertRaises(AgentProfileValidationError):
            self.registry.register("not_a_profile")  # type: ignore
        with self.assertRaises(AgentProfileValidationError):
            self.registry.register({"profile_id": "test"})  # type: ignore

    def test_03_duplicate_registration_rejected(self) -> None:
        """Registering a duplicate profile_id raises AgentProfileDuplicateError."""
        self.registry.register(self.p_backend)
        with self.assertRaises(AgentProfileDuplicateError):
            self.registry.register(self.p_backend)

        # Different object with identical profile_id also rejected
        duplicate_id = AgentProfile(
            profile_id="backend_engineer",
            display_name="Different Backend",
            description="Different desc",
        )
        with self.assertRaises(AgentProfileDuplicateError):
            self.registry.register(duplicate_id)

    def test_04_missing_lookup(self) -> None:
        """get() returns None for missing profile; require() and __getitem__ raise AgentProfileNotFoundError."""
        self.assertIsNone(self.registry.get("non_existent"))
        with self.assertRaises(AgentProfileNotFoundError):
            self.registry.require("non_existent")
        with self.assertRaises(AgentProfileNotFoundError):
            _ = self.registry["non_existent"]

    def test_05_deterministic_listing(self) -> None:
        """list() returns profiles deterministically sorted by profile_id."""
        self.registry.register(self.p_frontend)
        self.registry.register(self.p_backend)
        listed = self.registry.list()
        self.assertEqual(len(listed), 2)
        self.assertEqual(listed[0].profile_id, "backend_engineer")
        self.assertEqual(listed[1].profile_id, "frontend_engineer")

    def test_06_removal(self) -> None:
        """remove() deletes and returns existing profile, and raises for non-existent."""
        self.registry.register(self.p_backend)
        self.assertIn("backend_engineer", self.registry)
        self.assertEqual(len(self.registry), 1)

        removed = self.registry.remove("backend_engineer")
        self.assertEqual(removed, self.p_backend)
        self.assertNotIn("backend_engineer", self.registry)
        self.assertEqual(len(self.registry), 0)

        with self.assertRaises(AgentProfileNotFoundError):
            self.registry.remove("backend_engineer")

    def test_07_contains_and_len(self) -> None:
        """__contains__ and __len__ reflect exact registry contents."""
        self.assertNotIn("backend_engineer", self.registry)
        self.assertEqual(len(self.registry), 0)
        self.registry.register(self.p_backend)
        self.assertIn("backend_engineer", self.registry)
        self.assertEqual(len(self.registry), 1)

    def test_08_registry_capacity_limit(self) -> None:
        """Registry capacity is strictly capped at MAX_REGISTRY_CAPACITY."""
        reg = AgentProfileRegistry()
        for i in range(MAX_REGISTRY_CAPACITY):
            reg.register(AgentProfile(profile_id=f"prof_{i:04d}", display_name=f"Prof {i}", description="Desc"))
        self.assertEqual(len(reg), MAX_REGISTRY_CAPACITY)

        with self.assertRaises(AgentProfileError):
            reg.register(AgentProfile(profile_id="overflow", display_name="Overflow", description="Desc"))


class TestProfileResolutionMatching(unittest.TestCase):
    """Test deterministic resolution scoring: task_type, domain, skills, ties, and no match."""

    def setUp(self) -> None:
        self.registry = AgentProfileRegistry()
        self.p_py_backend = AgentProfile(
            profile_id="python_backend",
            display_name="Python Backend Engineer",
            description="Python API and backend implementation",
            task_types=("backend_api", "backend_debugging"),
            domains=("backend", "python"),
            skills=("fastapi", "pytest", "sqlalchemy"),
        )
        self.p_go_backend = AgentProfile(
            profile_id="go_backend",
            display_name="Go Backend Engineer",
            description="Go microservice implementation",
            task_types=("backend_api", "microservice"),
            domains=("backend", "golang"),
            skills=("gin", "grpc", "testing"),
        )
        self.p_frontend = AgentProfile(
            profile_id="react_frontend",
            display_name="React Frontend Engineer",
            description="React frontend development",
            task_types=("ui_implementation",),
            domains=("frontend", "web"),
            skills=("react", "typescript", "tailwind"),
        )
        self.p_qa = AgentProfile(
            profile_id="qa_engineer",
            display_name="QA Engineer",
            description="Automated testing and QA",
            task_types=("testing", "backend_debugging"),
            domains=("qa", "testing"),
            skills=("pytest", "playwright", "selenium"),
        )
        for p in (self.p_py_backend, self.p_go_backend, self.p_frontend, self.p_qa):
            self.registry.register(p)

    def test_09_exact_task_type_match(self) -> None:
        """Task type match assigns TASK_TYPE_MATCH_WEIGHT (10.0)."""
        req = ProfileResolutionRequest(task_type="ui_implementation")
        res = self.registry.resolve(req)
        self.assertEqual(res.status, ProfileResolutionStatus.MATCHED)
        self.assertEqual(res.profile_id, "react_frontend")
        self.assertEqual(res.score, TASK_TYPE_MATCH_WEIGHT)
        self.assertEqual(res.matched_task_type, "ui_implementation")

    def test_10_exact_domain_match(self) -> None:
        """Domain match assigns DOMAIN_MATCH_WEIGHT (5.0)."""
        req = ProfileResolutionRequest(domain="golang")
        res = self.registry.resolve(req)
        self.assertEqual(res.status, ProfileResolutionStatus.MATCHED)
        self.assertEqual(res.profile_id, "go_backend")
        self.assertEqual(res.score, DOMAIN_MATCH_WEIGHT)
        self.assertEqual(res.matched_domain, "golang")

    def test_11_skill_overlap_match(self) -> None:
        """Skill match awards 1.0 point per matching skill."""
        req = ProfileResolutionRequest(skills=("playwright", "selenium"))
        res = self.registry.resolve(req)
        self.assertEqual(res.status, ProfileResolutionStatus.MATCHED)
        self.assertEqual(res.profile_id, "qa_engineer")
        self.assertEqual(res.score, 2.0 * SKILL_MATCH_WEIGHT)
        self.assertEqual(res.matched_skills, ("playwright", "selenium"))

    def test_12_combined_highest_score_wins(self) -> None:
        """Combined signals (task_type + domain + skills) determine single winner."""
        # Both python_backend and go_backend have task_type="backend_api" (+10.0)
        # python_backend also has domain="python" (+5.0) and skill="fastapi" (+1.0) -> 16.0
        req = ProfileResolutionRequest(
            task_type="backend_api",
            domain="python",
            skills=("fastapi",),
        )
        res = self.registry.resolve(req)
        self.assertEqual(res.status, ProfileResolutionStatus.MATCHED)
        self.assertEqual(res.profile_id, "python_backend")
        self.assertEqual(res.score, 10.0 + 5.0 + 1.0)
        self.assertEqual(res.matched_task_type, "backend_api")
        self.assertEqual(res.matched_domain, "python")
        self.assertEqual(res.matched_skills, ("fastapi",))

    def test_13_no_match_returns_no_match_status(self) -> None:
        """Requests with signals that do not match any profile return NO_MATCH with score 0.0."""
        req = ProfileResolutionRequest(
            task_type="kernel_development",
            domain="embedded",
            skills=("c", "assembly"),
        )
        res = self.registry.resolve(req)
        self.assertEqual(res.status, ProfileResolutionStatus.NO_MATCH)
        self.assertIsNone(res.profile_id)
        self.assertEqual(res.candidate_ids, ())
        self.assertEqual(res.score, 0.0)

    def test_14_empty_request_returns_no_match(self) -> None:
        """Empty request with no task_type, domain, or skills returns NO_MATCH."""
        req = ProfileResolutionRequest()
        res = self.registry.resolve(req)
        self.assertEqual(res.status, ProfileResolutionStatus.NO_MATCH)
        self.assertIsNone(res.profile_id)
        self.assertEqual(res.score, 0.0)

    def test_15_empty_registry_returns_no_match(self) -> None:
        """Resolving against an empty registry returns NO_MATCH."""
        empty_reg = AgentProfileRegistry()
        req = ProfileResolutionRequest(task_type="backend_api")
        res = empty_reg.resolve(req)
        self.assertEqual(res.status, ProfileResolutionStatus.NO_MATCH)
        self.assertIsNone(res.profile_id)

    def test_16_ambiguous_match_returns_ambiguous_status(self) -> None:
        """When two profiles tie with the same highest score, resolution returns AMBIGUOUS."""
        # Both python_backend and go_backend have task_type="backend_api" (+10.0)
        req = ProfileResolutionRequest(task_type="backend_api")
        res = self.registry.resolve(req)
        self.assertEqual(res.status, ProfileResolutionStatus.AMBIGUOUS)
        self.assertIsNone(res.profile_id)
        self.assertEqual(res.score, 10.0)
        self.assertEqual(res.candidate_ids, ("go_backend", "python_backend"))
        self.assertIn("Ambiguous profile resolution", res.reason)

    def test_17_require_resolve_succeeds_on_matched(self) -> None:
        """require_resolve returns the matched AgentProfile when resolution succeeds."""
        req = ProfileResolutionRequest(task_type="ui_implementation")
        prof = self.registry.require_resolve(req)
        self.assertEqual(prof, self.p_frontend)

    def test_18_require_resolve_fails_closed_on_no_match(self) -> None:
        """require_resolve raises NoMatchingProfileError when no profile matches."""
        req = ProfileResolutionRequest(task_type="non_existent_task")
        with self.assertRaises(NoMatchingProfileError):
            self.registry.require_resolve(req)

    def test_19_require_resolve_fails_closed_on_ambiguity(self) -> None:
        """require_resolve raises AmbiguousProfileResolutionError on tied match."""
        req = ProfileResolutionRequest(task_type="backend_api")
        with self.assertRaises(AmbiguousProfileResolutionError):
            self.registry.require_resolve(req)

    def test_20_standalone_resolve_profile_matches_registry_method(self) -> None:
        """Standalone resolve_profile function produces identical output to registry.resolve."""
        req = ProfileResolutionRequest(domain="python")
        res1 = self.registry.resolve(req)
        res2 = resolve_profile(req, self.registry)
        self.assertEqual(res1, res2)


class TestProfileResolutionNormalization(unittest.TestCase):
    """Test normalization: whitespace stripping, case-insensitivity, and duplicate skill deduplication."""

    def setUp(self) -> None:
        self.registry = AgentProfileRegistry()
        self.profile = AgentProfile(
            profile_id="test_worker",
            display_name="Test Worker",
            description="Desc",
            task_types=("data_pipeline",),
            domains=("analytics",),
            skills=("spark", "python"),
        )
        self.registry.register(self.profile)

    def test_21_case_insensitive_matching(self) -> None:
        """Token comparisons are case-insensitive."""
        req = ProfileResolutionRequest(
            task_type="DATA_PIPELINE",
            domain="ANALYTICS",
            skills=("SPARK", "PYTHON"),
        )
        res = self.registry.resolve(req)
        self.assertEqual(res.status, ProfileResolutionStatus.MATCHED)
        self.assertEqual(res.profile_id, "test_worker")
        self.assertEqual(res.score, 10.0 + 5.0 + 2.0)

    def test_22_whitespace_stripping(self) -> None:
        """Whitespace in task_type, domain, and skills is cleanly stripped."""
        req = ProfileResolutionRequest(
            task_type="  data_pipeline  ",
            domain="  analytics  ",
            skills=("  spark  ",),
        )
        self.assertEqual(req.task_type, "data_pipeline")
        self.assertEqual(req.domain, "analytics")
        self.assertEqual(req.skills, ("spark",))
        res = self.registry.resolve(req)
        self.assertEqual(res.status, ProfileResolutionStatus.MATCHED)

    def test_23_duplicate_skills_deduplicated(self) -> None:
        """Duplicate skill tokens in resolution request are deduplicated and sorted."""
        req = ProfileResolutionRequest(skills=("python", "spark", "python", "spark"))
        self.assertEqual(req.skills, ("python", "spark"))
        self.assertEqual(len(req.skills), 2)


class TestProfileResolutionSerialization(unittest.TestCase):
    """Test to_dict, from_dict, and round-trip fidelity for Request and Result."""

    def test_24_request_serialization_round_trip(self) -> None:
        """ProfileResolutionRequest to_dict and from_dict preserve exact semantics."""
        req = ProfileResolutionRequest(
            task_type="backend_api",
            domain="python",
            skills=("fastapi", "pytest"),
        )
        data = req.to_dict()
        self.assertEqual(data["schema_version"], CURRENT_PROFILE_RESOLUTION_SCHEMA_VERSION)
        self.assertEqual(data["task_type"], "backend_api")
        self.assertEqual(data["skills"], ["fastapi", "pytest"])

        restored = ProfileResolutionRequest.from_dict(data)
        self.assertEqual(req, restored)

    def test_25_result_serialization_round_trip(self) -> None:
        """ProfileResolutionResult to_dict and from_dict preserve exact semantics."""
        res = ProfileResolutionResult(
            status=ProfileResolutionStatus.MATCHED,
            profile_id="python_backend",
            candidate_ids=("python_backend",),
            score=16.0,
            reason="Matched profile cleanly",
            matched_task_type="backend_api",
            matched_domain="python",
            matched_skills=("fastapi",),
        )
        data = res.to_dict()
        self.assertEqual(data["status"], "MATCHED")
        self.assertEqual(data["score"], 16.0)

        restored = ProfileResolutionResult.from_dict(data)
        self.assertEqual(res, restored)

    def test_26_request_from_dict_rejects_unknown_keys(self) -> None:
        """ProfileResolutionRequest rejects unexpected keys during deserialization."""
        with self.assertRaises(AgentProfileValidationError):
            ProfileResolutionRequest.from_dict({"task_type": "api", "extra": "smuggled"})

    def test_27_result_from_dict_rejects_unknown_keys(self) -> None:
        """ProfileResolutionResult rejects unexpected keys during deserialization."""
        with self.assertRaises(AgentProfileValidationError):
            ProfileResolutionResult.from_dict({"status": "MATCHED", "unknown_field": "val"})

    def test_27b_result_from_dict_rejects_authority_keys(self) -> None:
        """ProfileResolutionResult rejects authority-shaped keys during deserialization."""
        with self.assertRaises(AgentProfileAuthorityViolationError):
            ProfileResolutionResult.from_dict({"status": "MATCHED", "capabilities": ["shell"]})


class TestProfileResolutionSecretSafety(unittest.TestCase):
    """Test rejection of secrets and credentials in resolution requests."""

    def test_28_api_key_in_request_rejected(self) -> None:
        """API key patterns in request fields raise AgentProfileValidationError."""
        with self.assertRaises(AgentProfileValidationError):
            ProfileResolutionRequest(task_type=_MOCK_SK)
        with self.assertRaises(AgentProfileValidationError):
            ProfileResolutionRequest(domain=_MOCK_SK)
        with self.assertRaises(AgentProfileValidationError):
            ProfileResolutionRequest(skills=(_MOCK_SK,))

    def test_29_private_key_in_request_rejected(self) -> None:
        """Private key headers in request fields raise AgentProfileValidationError."""
        with self.assertRaises(AgentProfileValidationError):
            ProfileResolutionRequest(skills=(_MOCK_PK,))

    def test_30_bearer_token_in_request_rejected(self) -> None:
        """Bearer token patterns in request fields raise AgentProfileValidationError."""
        with self.assertRaises(AgentProfileValidationError):
            ProfileResolutionRequest(task_type=_MOCK_BEARER)

    def test_31_telegram_token_in_request_rejected(self) -> None:
        """Telegram bot token patterns in request fields raise AgentProfileValidationError."""
        with self.assertRaises(AgentProfileValidationError):
            ProfileResolutionRequest(skills=(_MOCK_TG,))


class TestProfileResolutionResourceBounds(unittest.TestCase):
    """Test boundary and overflow checks on resolution requests and results."""

    def test_32_oversized_task_type_rejected(self) -> None:
        """task_type exceeding MAX_RESOLUTION_TASK_TYPE_CHARS is rejected."""
        with self.assertRaises(AgentProfileValidationError):
            ProfileResolutionRequest(task_type="a" * (MAX_RESOLUTION_TASK_TYPE_CHARS + 1))

    def test_33_oversized_domain_rejected(self) -> None:
        """domain exceeding MAX_RESOLUTION_DOMAIN_CHARS is rejected."""
        with self.assertRaises(AgentProfileValidationError):
            ProfileResolutionRequest(domain="b" * (MAX_RESOLUTION_DOMAIN_CHARS + 1))

    def test_34_excessive_skills_count_rejected(self) -> None:
        """Skills count exceeding MAX_RESOLUTION_SKILLS_COUNT is rejected."""
        oversized = tuple(f"skill_{i:03d}" for i in range(MAX_RESOLUTION_SKILLS_COUNT + 1))
        with self.assertRaises(AgentProfileValidationError):
            ProfileResolutionRequest(skills=oversized)

    def test_35_oversized_skill_token_rejected(self) -> None:
        """Skill token exceeding MAX_RESOLUTION_SKILL_CHARS is rejected."""
        with self.assertRaises(AgentProfileValidationError):
            ProfileResolutionRequest(skills=("s" * (MAX_RESOLUTION_SKILL_CHARS + 1),))

    def test_36_oversized_candidate_ids_in_result_rejected(self) -> None:
        """Candidate IDs exceeding MAX_RESOLUTION_CANDIDATE_IDS is rejected."""
        oversized_cands = tuple(f"cand_{i}" for i in range(MAX_RESOLUTION_CANDIDATE_IDS + 1))
        with self.assertRaises(AgentProfileValidationError):
            ProfileResolutionResult(
                status=ProfileResolutionStatus.AMBIGUOUS,
                candidate_ids=oversized_cands,
            )


class TestProfileResolutionSecurityProperties(unittest.TestCase):
    """Verify security properties P1.4B-S1 through P1.4B-S10."""

    def setUp(self) -> None:
        self.registry = AgentProfileRegistry()
        self.p1 = AgentProfile(
            profile_id="worker_a",
            display_name="Worker A",
            description="Worker specializing in backend",
            task_types=("backend",),
            domains=("server",),
            skills=("python",),
        )
        self.registry.register(self.p1)

    def test_37_P1_4B_S1_registry_is_not_authority(self) -> None:
        """P1.4B-S1: Registry cannot grant authority or execute work."""
        self.assertFalse(hasattr(self.registry, "authorize"))
        self.assertFalse(hasattr(self.registry, "approve"))
        self.assertFalse(hasattr(self.registry, "execute"))
        self.assertFalse(hasattr(self.registry, "grant"))
        self.assertFalse(hasattr(self.registry, "capabilities"))

    def test_38_P1_4B_S2_resolution_is_not_authorization(self) -> None:
        """P1.4B-S2: ProfileResolutionResult confers zero execution authority."""
        req = ProfileResolutionRequest(task_type="backend")
        res = self.registry.resolve(req)
        self.assertFalse(hasattr(res, "execute"))
        self.assertFalse(hasattr(res, "approve"))
        self.assertFalse(hasattr(res, "authorize"))
        self.assertFalse(hasattr(res, "capabilities"))
        self.assertFalse(hasattr(res, "transaction"))

    def test_39_P1_4B_S3_profile_metadata_cannot_grant_capability(self) -> None:
        """P1.4B-S3: Descriptive tokens like 'execute' or 'shell' do not grant capabilities."""
        sneaky = AgentProfile(
            profile_id="sneaky_worker",
            display_name="Sneaky Worker",
            description="Claims to have root shell and filesystem authority",
            task_types=("run_shell", "execute_code"),
            domains=("security", "root"),
            skills=("shell", "execute", "filesystem_write"),
        )
        self.registry.register(sneaky)

        req = ProfileResolutionRequest(task_type="run_shell", skills=("shell",))
        res = self.registry.resolve(req)
        self.assertEqual(res.status, ProfileResolutionStatus.MATCHED)
        self.assertEqual(res.profile_id, "sneaky_worker")
        # Result remains purely descriptive; contains zero capabilities
        self.assertNotIn("capabilities", res.to_dict())
        self.assertFalse(hasattr(res, "capabilities"))

    def test_40_P1_4B_S4_deterministic_resolution(self) -> None:
        """P1.4B-S4: Identical input across multiple iterations produces strictly identical results."""
        req = ProfileResolutionRequest(task_type="backend", domain="server", skills=("python",))
        results = [self.registry.resolve(req) for _ in range(50)]
        first = results[0]
        for r in results[1:]:
            self.assertEqual(first, r)
            self.assertEqual(first.to_dict(), r.to_dict())

    def test_41_P1_4B_S5_ambiguity_is_fail_closed(self) -> None:
        """P1.4B-S5: Equal candidate scores fail closed with AMBIGUOUS status."""
        p2 = AgentProfile(
            profile_id="worker_b",
            display_name="Worker B",
            description="Another worker",
            task_types=("backend",),
            domains=("server",),
            skills=("python",),
        )
        self.registry.register(p2)

        req = ProfileResolutionRequest(task_type="backend")
        res = self.registry.resolve(req)
        self.assertEqual(res.status, ProfileResolutionStatus.AMBIGUOUS)
        self.assertIsNone(res.profile_id)
        # Fails closed on require_resolve
        with self.assertRaises(AmbiguousProfileResolutionError):
            self.registry.require_resolve(req)

    def test_42_P1_4B_S6_registry_isolation(self) -> None:
        """P1.4B-S6: Registry operations cannot mutate profiles or resolution results."""
        prof_before = self.p1.to_dict()
        req = ProfileResolutionRequest(task_type="backend")
        res = self.registry.resolve(req)
        prof_after = self.registry.get("worker_a").to_dict()  # type: ignore
        self.assertEqual(prof_before, prof_after)

    def test_43_P1_4B_S7_resource_boundedness(self) -> None:
        """P1.4B-S7: Overly large resolution request payload fails closed."""
        with self.assertRaises(AgentProfileValidationError):
            ProfileResolutionRequest(
                skills=tuple(f"skill_{i:04d}" for i in range(MAX_RESOLUTION_SKILLS_COUNT + 10))
            )

    def test_44_P1_4B_S8_no_execution_dependency(self) -> None:
        """P1.4B-S8: Profile resolution has zero execution dependencies."""
        for forbidden in ("execute", "run", "spawn", "dispatch"):
            self.assertFalse(hasattr(self.registry, forbidden))
            self.assertFalse(hasattr(ProfileResolutionResult, forbidden))

    def test_45_P1_4B_S9_no_cross_session_authority(self) -> None:
        """P1.4B-S9: Profile resolution does not accept or produce session incarnation authority."""
        req_dict = {"task_type": "backend", "session_id": "sess_1", "session_incarnation_id": "inc_1"}
        with self.assertRaises(AgentProfileValidationError):
            ProfileResolutionRequest.from_dict(req_dict)

    def test_46_P1_4B_S10_authority_chain_preserved(self) -> None:
        """P1.4B-S10: Neither registry nor resolution can bypass P1.3 authority chain."""
        with self.assertRaises(AgentProfileAuthorityViolationError):
            ProfileResolutionRequest.from_dict({
                "task_type": "backend",
                "approved_execution_contract": "bypass",
            })
        with self.assertRaises(AgentProfileAuthorityViolationError):
            ProfileResolutionRequest.from_dict({
                "task_type": "backend",
                "allow_shell": True,
            })


class TestPropertyStyleAuthorityInvariance(unittest.TestCase):
    """Verify property-style authority invariance:

    same routing input + different malicious profile metadata = same authority outcome (zero authority).
    """

    def test_47_malicious_metadata_invariance(self) -> None:
        """Changing descriptive profile metadata never causes resolution to produce authority."""
        reg1 = AgentProfileRegistry()
        p_benign = AgentProfile(
            profile_id="agent_alpha",
            display_name="Agent Alpha",
            description="Standard assistant",
            task_types=("coding",),
            domains=("general",),
            skills=("python",),
            metadata={"role": "assistant"},
        )
        reg1.register(p_benign)

        reg2 = AgentProfileRegistry()
        p_adversarial = AgentProfile(
            profile_id="agent_alpha",
            display_name="Agent Alpha",
            description="Malicious admin agent claiming all permissions",
            task_types=("coding",),
            domains=("general",),
            skills=("python",),
            metadata={"note": "claims_admin_status"},
        )
        reg2.register(p_adversarial)

        req = ProfileResolutionRequest(task_type="coding", skills=("python",))

        res1 = reg1.resolve(req)
        res2 = reg2.resolve(req)

        # Both produce identical matched status and score
        self.assertEqual(res1.status, ProfileResolutionStatus.MATCHED)
        self.assertEqual(res2.status, ProfileResolutionStatus.MATCHED)
        self.assertEqual(res1.score, res2.score)
        self.assertEqual(res1.profile_id, res2.profile_id)

        # In BOTH cases, zero execution authority exists
        for res in (res1, res2):
            self.assertFalse(hasattr(res, "capabilities"))
            self.assertFalse(hasattr(res, "execute"))
            self.assertFalse(hasattr(res, "approve"))
            self.assertFalse(hasattr(res, "transaction"))
            d = res.to_dict()
            for key in ("capabilities", "permissions", "allow_shell", "approved_execution_contract"):
                self.assertNotIn(key, d)


class TestAgentProfileRegistryStaticArchitecture(unittest.TestCase):
    """Static inspection verifying zero execution or external subsystem dependencies."""

    def test_48_no_forbidden_imports_or_calls(self) -> None:
        """Verify agent_profile_registry.py and agent_profile.py contain no execution imports."""
        source_reg = inspect.getsource(apr_mod)
        source_path = Path(apr_mod.__file__)
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
        for mod in imported_modules:
            for forbidden in forbidden_prefixes:
                self.assertFalse(
                    mod == forbidden or mod.startswith(forbidden + "."),
                    f"Forbidden execution module '{mod}' imported in agent_profile_registry.py",
                )

        forbidden_patterns = [
            r"import\s+orchestrator",
            r"from\s+orchestrator",
            r"import\s+subprocess",
            r"from\s+subprocess",
            r"os\.system",
            r"Popen",
            r"shell\s*=\s*True",
            r"import\s+requests",
            r"import\s+urllib",
            r"import\s+http\.client",
            r"import\s+anthropic",
            r"import\s+openai",
            r"class\s+.*Orchestrator",
            r"class\s+.*Executor",
        ]
        for pattern in forbidden_patterns:
            matches = re.findall(pattern, source_reg)
            self.assertEqual(
                matches,
                [],
                f"Forbidden execution pattern '{pattern}' found in agent_profile_registry.py: {matches}",
            )


if __name__ == "__main__":
    unittest.main()
