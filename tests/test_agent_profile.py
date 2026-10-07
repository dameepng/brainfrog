"""Comprehensive unit and security test suite for AgentProfile and AgentProfileRegistry.

BrainFrog P1.4A — Agent Profile Domain & Registry.

Validates:
- AGENT PROFILE ≠ AUTHORIZATION
- REGISTRY ≠ AUTHORIZATION
- Domain construction, validation, and bounding
- Deep immutability of top-level and nested structures
- Deterministic cryptographic digest calculation
- Deterministic serialization and deserialization round-trip
- Rejection of secret material
- Rejection of authority-shaped metadata and constraints
- In-memory AgentProfileRegistry semantics (registration, deduplication, lookup, removal, listing)
- Zero execution dependencies and pure domain architecture
- Security properties P1.4A-S1 through P1.4A-S8
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

import core.runtime.agent_profile as ap_mod
from core.runtime.agent_profile import (
    CURRENT_AGENT_PROFILE_SCHEMA_VERSION,
    MAX_CONSTRAINTS_COUNT,
    MAX_DESCRIPTION_CHARS,
    MAX_DISPLAY_NAME_CHARS,
    MAX_DOMAIN_CHARS,
    MAX_DOMAINS_COUNT,
    MAX_METADATA_DEPTH,
    MAX_METADATA_KEYS,
    MAX_PROFILE_ID_CHARS,
    MAX_PROFILE_SERIALIZED_BYTES,
    MAX_REGISTRY_CAPACITY,
    MAX_SKILL_CHARS,
    MAX_SKILLS_COUNT,
    MAX_TASK_TYPE_CHARS,
    MAX_TASK_TYPES_COUNT,
    AgentProfile,
    AgentProfileAuthorityViolationError,
    AgentProfileDuplicateError,
    AgentProfileError,
    AgentProfileIntegrityError,
    AgentProfileNotFoundError,
    AgentProfileRegistry,
    AgentProfileValidationError,
    validate_profile_id,
)

# Test credential fragments dynamically assembled to avoid scanner false positives
_MOCK_SK = "sk-" + "proj-1234567890abcdef1234567890abcdef"
_MOCK_GH = "gh" + "p_1234567890abcdef1234567890abcdef"
_MOCK_PK = "-----BEGIN " + "RSA PRIVATE KEY-----"
_MOCK_BEARER = "Bearer " + "eyJh1234567890abcdef"
_MOCK_PWD = "pass" + "word: supersecret123"
_MOCK_SECRET = "sec" + "ret: confidential"
_MOCK_TG = "123456789:" + "ABCdefGHIjklMNOpqrSTUvwxYZ_12345678"


class TestAgentProfileDomainConstruction(unittest.TestCase):
    """Test standard instantiation and default values for AgentProfile."""

    def test_01_valid_profile(self) -> None:
        """Construct a standard valid profile with core fields."""
        prof = AgentProfile(
            profile_id="backend_engineer",
            display_name="Backend Engineer",
            description="Specialized in backend APIs and data modeling",
            task_types=("backend_implementation", "api_design"),
            domains=("backend", "database"),
            skills=("python", "fastapi", "postgresql"),
            constraints={"preferred_language": "python"},
            metadata={"tier": "primary"},
        )
        self.assertEqual(prof.profile_id, "backend_engineer")
        self.assertEqual(prof.id, "backend_engineer")
        self.assertEqual(prof.display_name, "Backend Engineer")
        self.assertEqual(prof.description, "Specialized in backend APIs and data modeling")
        self.assertEqual(prof.task_types, ("api_design", "backend_implementation"))
        self.assertEqual(prof.domains, ("backend", "database"))
        self.assertEqual(prof.skills, ("fastapi", "postgresql", "python"))
        self.assertEqual(prof.constraints["preferred_language"], "python")
        self.assertEqual(prof.metadata["tier"], "primary")
        self.assertEqual(prof.schema_version, CURRENT_AGENT_PROFILE_SCHEMA_VERSION)
        self.assertTrue(len(prof.digest) == 64)

    def test_02_minimal_profile(self) -> None:
        """Construct a minimal profile with only required fields."""
        prof = AgentProfile(
            profile_id="general_assistant",
            display_name="General Assistant",
            description="General purpose helper",
        )
        self.assertEqual(prof.profile_id, "general_assistant")
        self.assertEqual(prof.task_types, ())
        self.assertEqual(prof.domains, ())
        self.assertEqual(prof.skills, ())
        self.assertEqual(len(prof.constraints), 0)
        self.assertEqual(len(prof.metadata), 0)
        self.assertTrue(prof.created_at > 0)
        self.assertTrue(len(prof.digest) == 64)

    def test_03_all_fields_populated(self) -> None:
        """Construct a profile with every field populated and verified."""
        t0 = 1700000000.0
        prof = AgentProfile(
            profile_id="security_reviewer",
            display_name="Security Reviewer",
            description="Reviews code for security vulnerabilities",
            task_types=("security_review", "audit"),
            domains=("security", "crypto"),
            skills=("bandit", "semgrep", "owasp"),
            constraints={"review_depth": "deep", "max_files": 50},
            metadata={"version": "1.0", "active": True},
            schema_version=1,
            created_at=t0,
        )
        self.assertEqual(prof.created_at, t0)
        self.assertEqual(prof.schema_version, 1)
        self.assertEqual(prof.constraints["review_depth"], "deep")
        self.assertEqual(prof.metadata["active"], True)


class TestAgentProfileValidation(unittest.TestCase):
    """Test validation rules for identifiers, strings, and collections."""

    def test_04_empty_profile_id(self) -> None:
        """Empty or whitespace-only profile IDs must be rejected."""
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(profile_id="", display_name="Test", description="Desc")
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(profile_id="   ", display_name="Test", description="Desc")

    def test_05_invalid_profile_id_types(self) -> None:
        """Non-string profile IDs must be rejected."""
        for invalid in (123, None, ["id"], {"id": 1}):
            with self.assertRaises(AgentProfileValidationError):
                AgentProfile(profile_id=invalid, display_name="Test", description="Desc")  # type: ignore

    def test_06_traversal_profile_id(self) -> None:
        """Path traversal patterns in profile_id must fail closed."""
        traversals = ("..", "../agent", "agent/sub", "agent\\sub", "~agent", "/root", "\\sys")
        for bad_id in traversals:
            with self.assertRaises(AgentProfileValidationError):
                AgentProfile(profile_id=bad_id, display_name="Test", description="Desc")

    def test_07_control_characters_in_profile_id(self) -> None:
        """Null bytes and control characters in profile_id must be rejected."""
        for bad_id in ("agent\x00id", "agent\nid", "agent\tid", "agent?test", "agent*"):
            with self.assertRaises(AgentProfileValidationError):
                AgentProfile(profile_id=bad_id, display_name="Test", description="Desc")

    def test_08_drive_letter_pattern_rejected(self) -> None:
        """Windows drive letter patterns must be rejected in profile_id."""
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(profile_id="C:agent", display_name="Test", description="Desc")
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(profile_id="d:profile", display_name="Test", description="Desc")

    def test_09_oversized_strings(self) -> None:
        """Oversized profile strings must be rejected."""
        # Oversized profile_id
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(profile_id="a" * (MAX_PROFILE_ID_CHARS + 1), display_name="Test", description="Desc")
        # Oversized display_name
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(profile_id="valid", display_name="a" * (MAX_DISPLAY_NAME_CHARS + 1), description="Desc")
        # Empty display_name
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(profile_id="valid", display_name="   ", description="Desc")
        # Oversized description
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(profile_id="valid", display_name="Test", description="a" * (MAX_DESCRIPTION_CHARS + 1))

    def test_10_oversized_collections(self) -> None:
        """Collections exceeding count bounds must be rejected."""
        # task_types > 64
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(
                profile_id="test",
                display_name="Test",
                description="Desc",
                task_types=[f"task_{i}" for i in range(MAX_TASK_TYPES_COUNT + 1)],
            )
        # domains > 32
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(
                profile_id="test",
                display_name="Test",
                description="Desc",
                domains=[f"domain_{i}" for i in range(MAX_DOMAINS_COUNT + 1)],
            )
        # skills > 128
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(
                profile_id="test",
                display_name="Test",
                description="Desc",
                skills=[f"skill_{i}" for i in range(MAX_SKILLS_COUNT + 1)],
            )

    def test_11_collection_item_length_and_content(self) -> None:
        """Items in collections must be bounded non-empty strings without control characters."""
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(profile_id="test", display_name="Test", description="Desc", skills=("s" * (MAX_SKILL_CHARS + 1),))
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(profile_id="test", display_name="Test", description="Desc", skills=("",))
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(profile_id="test", display_name="Test", description="Desc", skills=("skill\x00bad",))
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(profile_id="test", display_name="Test", description="Desc", skills=(123,))  # type: ignore

    def test_12_malformed_metadata_types(self) -> None:
        """Non-JSON-serializable objects in metadata must fail closed."""
        def dummy_func():
            pass

        class DummyClass:
            pass

        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(profile_id="test", display_name="Test", description="Desc", metadata={"fn": dummy_func})
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(profile_id="test", display_name="Test", description="Desc", metadata={"cls": DummyClass})
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(profile_id="test", display_name="Test", description="Desc", metadata={"obj": DummyClass()})
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(profile_id="test", display_name="Test", description="Desc", metadata="not_a_dict")  # type: ignore

    def test_13_metadata_nesting_depth_limit(self) -> None:
        """Metadata exceeding nesting depth of 5 must be rejected."""
        deep = {"a": {"b": {"c": {"d": {"e": {"f": "too_deep"}}}}}}
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(profile_id="test", display_name="Test", description="Desc", metadata=deep)

    def test_14_non_finite_floats_in_metadata(self) -> None:
        """NaN and Inf floats in metadata must fail closed."""
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(profile_id="test", display_name="Test", description="Desc", metadata={"v": float("nan")})
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(profile_id="test", display_name="Test", description="Desc", metadata={"v": float("inf")})


class TestAgentProfileImmutability(unittest.TestCase):
    """Test deep immutability of AgentProfile instances."""

    def test_15_top_level_attribute_mutation_prevented(self) -> None:
        """Modifying any top-level attribute must raise FrozenInstanceError."""
        prof = AgentProfile(profile_id="test", display_name="Test", description="Desc")
        with self.assertRaises(Exception):
            prof.display_name = "New Name"  # type: ignore
        with self.assertRaises(Exception):
            prof.profile_id = "new_id"  # type: ignore
        with self.assertRaises(Exception):
            prof.schema_version = 2  # type: ignore

    def test_16_nested_task_types_mutation_prevented(self) -> None:
        """task_types is an immutable tuple without mutation methods."""
        prof = AgentProfile(profile_id="test", display_name="Test", description="Desc", task_types=("a", "b"))
        self.assertIsInstance(prof.task_types, tuple)
        self.assertFalse(hasattr(prof.task_types, "add"))
        self.assertFalse(hasattr(prof.task_types, "append"))
        self.assertFalse(hasattr(prof.task_types, "extend"))

    def test_17_nested_skills_mutation_prevented(self) -> None:
        """skills is an immutable tuple without mutation methods."""
        prof = AgentProfile(profile_id="test", display_name="Test", description="Desc", skills=("python",))
        self.assertIsInstance(prof.skills, tuple)
        self.assertFalse(hasattr(prof.skills, "add"))
        self.assertFalse(hasattr(prof.skills, "append"))

    def test_18_nested_metadata_mutation_prevented(self) -> None:
        """metadata is wrapped in MappingProxyType and prevents in-place mutation."""
        prof = AgentProfile(
            profile_id="test",
            display_name="Test",
            description="Desc",
            metadata={"key": "val", "nested": {"sub": 1}},
        )
        self.assertIsInstance(prof.metadata, MappingProxyType)
        with self.assertRaises(TypeError):
            prof.metadata["key"] = "tampered"  # type: ignore
        with self.assertRaises(TypeError):
            prof.metadata["new_key"] = "tampered"  # type: ignore
        with self.assertRaises(TypeError):
            prof.metadata["nested"]["sub"] = 2  # type: ignore

    def test_19_nested_constraints_mutation_prevented(self) -> None:
        """constraints is wrapped in MappingProxyType and prevents in-place mutation."""
        prof = AgentProfile(
            profile_id="test",
            display_name="Test",
            description="Desc",
            constraints={"pref": "python"},
        )
        self.assertIsInstance(prof.constraints, MappingProxyType)
        with self.assertRaises(TypeError):
            prof.constraints["pref"] = "tampered"  # type: ignore
        with self.assertRaises(TypeError):
            prof.constraints["new"] = "val"  # type: ignore

    def test_20_external_collection_mutation_does_not_affect_profile(self) -> None:
        """Modifying the input dict or list after passing to constructor has zero effect."""
        raw_skills = ["python", "rust"]
        raw_meta = {"env": "prod"}
        raw_constraints = {"limit": 10}
        prof = AgentProfile(
            profile_id="test",
            display_name="Test",
            description="Desc",
            skills=raw_skills,
            metadata=raw_meta,
            constraints=raw_constraints,
        )
        raw_skills.append("malicious")
        raw_meta["env"] = "tampered"
        raw_constraints["limit"] = 999

        self.assertNotIn("malicious", prof.skills)
        self.assertEqual(prof.metadata["env"], "prod")
        self.assertEqual(prof.constraints["limit"], 10)


class TestAgentProfileDigest(unittest.TestCase):
    """Test cryptographic SHA-256 digest determinism and sensitivity."""

    def test_21_digest_is_deterministic(self) -> None:
        """Identical inputs produce identical digests."""
        p1 = AgentProfile(
            profile_id="worker",
            display_name="Worker",
            description="Desc",
            skills=("python", "fastapi"),
            metadata={"a": 1, "b": 2},
        )
        p2 = AgentProfile(
            profile_id="worker",
            display_name="Worker",
            description="Desc",
            skills=("fastapi", "python"),  # Order in constructor doesn't matter (sorted)
            metadata={"b": 2, "a": 1},  # Dict key order doesn't matter
        )
        self.assertEqual(p1.digest, p2.digest)

    def test_22_volatile_timestamp_does_not_change_digest(self) -> None:
        """Volatile created_at timestamp does not alter the semantic digest."""
        p1 = AgentProfile(profile_id="worker", display_name="Worker", description="Desc", created_at=100.0)
        p2 = AgentProfile(profile_id="worker", display_name="Worker", description="Desc", created_at=999999.0)
        self.assertEqual(p1.digest, p2.digest)

    def test_23_semantic_field_changes_alter_digest(self) -> None:
        """Modifying any semantic field changes the digest."""
        base = AgentProfile(
            profile_id="worker",
            display_name="Worker",
            description="Desc",
            task_types=("dev",),
            domains=("backend",),
            skills=("py",),
            constraints={"pref": "a"},
            metadata={"m": 1},
        )
        # Profile ID
        p_id = AgentProfile(
            profile_id="worker_alt",
            display_name="Worker",
            description="Desc",
            task_types=("dev",),
            domains=("backend",),
            skills=("py",),
            constraints={"pref": "a"},
            metadata={"m": 1},
        )
        self.assertNotEqual(base.digest, p_id.digest)

        # Display name
        p_name = AgentProfile(
            profile_id="worker",
            display_name="Worker Alt",
            description="Desc",
            task_types=("dev",),
            domains=("backend",),
            skills=("py",),
            constraints={"pref": "a"},
            metadata={"m": 1},
        )
        self.assertNotEqual(base.digest, p_name.digest)

        # Description
        p_desc = AgentProfile(
            profile_id="worker",
            display_name="Worker",
            description="Desc Alt",
            task_types=("dev",),
            domains=("backend",),
            skills=("py",),
            constraints={"pref": "a"},
            metadata={"m": 1},
        )
        self.assertNotEqual(base.digest, p_desc.digest)

        # Task types
        p_tasks = AgentProfile(
            profile_id="worker",
            display_name="Worker",
            description="Desc",
            task_types=("dev", "test"),
            domains=("backend",),
            skills=("py",),
            constraints={"pref": "a"},
            metadata={"m": 1},
        )
        self.assertNotEqual(base.digest, p_tasks.digest)

        # Domains
        p_dom = AgentProfile(
            profile_id="worker",
            display_name="Worker",
            description="Desc",
            task_types=("dev",),
            domains=("backend", "cloud"),
            skills=("py",),
            constraints={"pref": "a"},
            metadata={"m": 1},
        )
        self.assertNotEqual(base.digest, p_dom.digest)

        # Skills
        p_skills = AgentProfile(
            profile_id="worker",
            display_name="Worker",
            description="Desc",
            task_types=("dev",),
            domains=("backend",),
            skills=("py", "rust"),
            constraints={"pref": "a"},
            metadata={"m": 1},
        )
        self.assertNotEqual(base.digest, p_skills.digest)

        # Constraints
        p_cons = AgentProfile(
            profile_id="worker",
            display_name="Worker",
            description="Desc",
            task_types=("dev",),
            domains=("backend",),
            skills=("py",),
            constraints={"pref": "b"},
            metadata={"m": 1},
        )
        self.assertNotEqual(base.digest, p_cons.digest)

        # Metadata
        p_meta = AgentProfile(
            profile_id="worker",
            display_name="Worker",
            description="Desc",
            task_types=("dev",),
            domains=("backend",),
            skills=("py",),
            constraints={"pref": "a"},
            metadata={"m": 2},
        )
        self.assertNotEqual(base.digest, p_meta.digest)

    def test_24_tampered_digest_fails_integrity(self) -> None:
        """Providing an explicit digest that does not match computed digest raises AgentProfileIntegrityError."""
        with self.assertRaises(AgentProfileIntegrityError):
            AgentProfile(
                profile_id="test",
                display_name="Test",
                description="Desc",
                digest="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            )


class TestAgentProfileSerialization(unittest.TestCase):
    """Test to_dict, from_dict, and round-trip fidelity."""

    def test_25_round_trip_serialization(self) -> None:
        """to_dict and from_dict round-trip must preserve exact profile semantics."""
        orig = AgentProfile(
            profile_id="db_specialist",
            display_name="Database Specialist",
            description="Database query optimization and schema design",
            task_types=("query_tuning", "schema_migration"),
            domains=("database", "backend"),
            skills=("postgres", "sql", "explain_analyze"),
            constraints={"engine": "postgres"},
            metadata={"version": 2},
            created_at=1700000000.0,
        )
        data = orig.to_dict()
        self.assertIsInstance(data, dict)
        self.assertEqual(data["profile_id"], "db_specialist")
        self.assertEqual(data["schema_version"], CURRENT_AGENT_PROFILE_SCHEMA_VERSION)

        restored = AgentProfile.from_dict(data)
        self.assertEqual(orig, restored)
        self.assertEqual(orig.digest, restored.digest)

    def test_26_from_dict_malformed_input(self) -> None:
        """from_dict must reject non-dict input."""
        for invalid in ("not_dict", 123, None, ["a"]):
            with self.assertRaises(AgentProfileValidationError):
                AgentProfile.from_dict(invalid)  # type: ignore

    def test_27_from_dict_missing_required_keys(self) -> None:
        """Missing required keys in deserialization dict must be rejected."""
        valid_data = {
            "profile_id": "test",
            "display_name": "Test",
            "description": "Desc",
        }
        for req in ("profile_id", "display_name", "description"):
            bad = dict(valid_data)
            del bad[req]
            with self.assertRaises(AgentProfileValidationError):
                AgentProfile.from_dict(bad)

    def test_28_from_dict_unexpected_keys(self) -> None:
        """Unexpected top-level keys in deserialization dict must fail closed."""
        valid_data = {
            "profile_id": "test",
            "display_name": "Test",
            "description": "Desc",
            "unexpected_field": "exploit",
        }
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile.from_dict(valid_data)


class TestAgentProfileSecretSafety(unittest.TestCase):
    """Test that credential and secret material is rejected in all profile fields."""

    def test_29_api_key_rejected(self) -> None:
        """API key patterns must be rejected in all fields."""
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(profile_id="test", display_name="Test", description="Desc", metadata={"key": _MOCK_SK})
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(profile_id="test", display_name=f"Worker {_MOCK_SK}", description="Desc")
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(profile_id="test", display_name="Test", description=f"Uses {_MOCK_SK}")

    def test_30_github_token_rejected(self) -> None:
        """GitHub token patterns must be rejected."""
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(profile_id="test", display_name="Test", description="Desc", metadata={"token": _MOCK_GH})

    def test_31_private_key_rejected(self) -> None:
        """Private key headers must be rejected."""
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(profile_id="test", display_name="Test", description="Desc", constraints={"pk": _MOCK_PK})

    def test_32_bearer_token_rejected(self) -> None:
        """Bearer token patterns must be rejected."""
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(profile_id="test", display_name="Test", description="Desc", metadata={"auth": _MOCK_BEARER})

    def test_33_password_like_fields_rejected(self) -> None:
        """Password assignment patterns must be rejected."""
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(profile_id="test", display_name="Test", description="Desc", metadata={"cred": _MOCK_PWD})
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(profile_id="test", display_name="Test", description="Desc", constraints={"cred": _MOCK_SECRET})

    def test_33b_telegram_token_rejected(self) -> None:
        """Telegram bot token patterns must be rejected."""
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(profile_id="test", display_name="Test", description="Desc", metadata={"bot": _MOCK_TG})


class TestAgentProfileAuthorityBoundaries(unittest.TestCase):
    """Verify that AgentProfile cannot grant, smuggle, or execute authority (AGENT PROFILE ≠ AUTHORIZATION)."""

    def test_34_authority_keys_in_metadata_rejected(self) -> None:
        """Explicit authority fields like capabilities or permissions must be rejected."""
        authority_keys = (
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
            "orchestrator",
        )
        for key in authority_keys:
            with self.assertRaises(AgentProfileAuthorityViolationError):
                AgentProfile(profile_id="test", display_name="Test", description="Desc", metadata={key: ["read"]})
            with self.assertRaises(AgentProfileAuthorityViolationError):
                AgentProfile(profile_id="test", display_name="Test", description="Desc", constraints={key: True})

    def test_35_authority_smuggling_in_deserialization_rejected(self) -> None:
        """Attempting to smuggle authority keys in serialized dictionary must fail closed."""
        data = {
            "profile_id": "test",
            "display_name": "Test",
            "description": "Desc",
            "capabilities": ["shell", "filesystem.write"],
        }
        with self.assertRaises(AgentProfileAuthorityViolationError):
            AgentProfile.from_dict(data)

    def test_36_profile_has_no_execution_primitives(self) -> None:
        """AgentProfile must not expose execute(), run(), or transaction/approval methods."""
        prof = AgentProfile(profile_id="test", display_name="Test", description="Desc")
        self.assertFalse(hasattr(prof, "execute"))
        self.assertFalse(hasattr(prof, "run"))
        self.assertFalse(hasattr(prof, "approve"))
        self.assertFalse(hasattr(prof, "transaction"))
        self.assertFalse(hasattr(prof, "commit"))
        self.assertFalse(hasattr(prof, "rollback"))
        self.assertFalse(hasattr(prof, "capabilities"))
        self.assertFalse(hasattr(prof, "permission"))


class TestAgentProfileRegistry(unittest.TestCase):
    """Test deterministic in-memory AgentProfileRegistry operations."""

    def setUp(self) -> None:
        self.registry = AgentProfileRegistry()
        self.p_backend = AgentProfile(
            profile_id="backend_engineer",
            display_name="Backend Engineer",
            description="Backend implementation",
            domains=("backend",),
            skills=("python",),
        )
        self.p_frontend = AgentProfile(
            profile_id="frontend_engineer",
            display_name="Frontend Engineer",
            description="Frontend components",
            domains=("frontend",),
            skills=("react",),
        )

    def test_37_register_and_lookup(self) -> None:
        """Registering a profile allows subsequent lookup by profile_id."""
        self.registry.register(self.p_backend)
        retrieved = self.registry.get("backend_engineer")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved, self.p_backend)

    def test_38_duplicate_registration_rejected(self) -> None:
        """Registering a profile with an existing profile_id must raise AgentProfileDuplicateError."""
        self.registry.register(self.p_backend)
        with self.assertRaises(AgentProfileDuplicateError):
            self.registry.register(self.p_backend)

        # Different profile with same profile_id also rejected
        duplicate_id = AgentProfile(
            profile_id="backend_engineer",
            display_name="Another Backend",
            description="Duplicate ID",
        )
        with self.assertRaises(AgentProfileDuplicateError):
            self.registry.register(duplicate_id)

    def test_39_missing_lookup(self) -> None:
        """Looking up non-existent profile returns None via get(), or raises via require()."""
        self.assertIsNone(self.registry.get("non_existent"))
        with self.assertRaises(AgentProfileNotFoundError):
            self.registry.require("non_existent")
        with self.assertRaises(AgentProfileNotFoundError):
            _ = self.registry["non_existent"]

    def test_40_deterministic_listing(self) -> None:
        """list() returns profiles deterministically sorted by profile_id."""
        # Register in reverse alphabetical order
        self.registry.register(self.p_frontend)
        self.registry.register(self.p_backend)

        listed = self.registry.list()
        self.assertEqual(len(listed), 2)
        self.assertEqual(listed[0].profile_id, "backend_engineer")
        self.assertEqual(listed[1].profile_id, "frontend_engineer")

    def test_41_removal(self) -> None:
        """remove() deletes and returns existing profile, or raises for missing."""
        self.registry.register(self.p_backend)
        self.assertIn("backend_engineer", self.registry)
        self.assertEqual(len(self.registry), 1)

        removed = self.registry.remove("backend_engineer")
        self.assertEqual(removed, self.p_backend)
        self.assertNotIn("backend_engineer", self.registry)
        self.assertEqual(len(self.registry), 0)

        with self.assertRaises(AgentProfileNotFoundError):
            self.registry.remove("backend_engineer")

    def test_42_registry_isolation(self) -> None:
        """Modifying initial collection passed to registry constructor does not affect registry."""
        initial_list = [self.p_backend]
        reg = AgentProfileRegistry(initial_list)
        initial_list.append(self.p_frontend)
        self.assertEqual(len(reg), 1)
        self.assertNotIn("frontend_engineer", reg)

    def test_43_registry_capacity_limit(self) -> None:
        """Registry rejects registrations once MAX_REGISTRY_CAPACITY is reached."""
        reg = AgentProfileRegistry()
        for i in range(MAX_REGISTRY_CAPACITY):
            reg.register(AgentProfile(profile_id=f"prof_{i:04d}", display_name=f"Prof {i}", description="Desc"))
        self.assertEqual(len(reg), MAX_REGISTRY_CAPACITY)

        with self.assertRaises(AgentProfileError):
            reg.register(AgentProfile(profile_id="overflow", display_name="Overflow", description="Desc"))

    def test_44_registry_has_no_authority_methods(self) -> None:
        """Registry answers only 'what profiles exist' and never confers authority."""
        self.assertFalse(hasattr(self.registry, "authorize"))
        self.assertFalse(hasattr(self.registry, "approve"))
        self.assertFalse(hasattr(self.registry, "execute"))
        self.assertFalse(hasattr(self.registry, "grant"))
        self.assertFalse(hasattr(self.registry, "capabilities"))


class TestAgentProfileResourceLimits(unittest.TestCase):
    """Test boundary and overflow conditions for resource limits."""

    def test_45_exact_boundary_values_accepted(self) -> None:
        """Values exactly at boundary limits are accepted cleanly."""
        max_id = "a" * MAX_PROFILE_ID_CHARS
        max_name = "b" * MAX_DISPLAY_NAME_CHARS
        max_desc = "c" * MAX_DESCRIPTION_CHARS
        prof = AgentProfile(
            profile_id=max_id,
            display_name=max_name,
            description=max_desc,
            task_types=tuple(f"t_{i:02d}" for i in range(MAX_TASK_TYPES_COUNT)),
            domains=tuple(f"d_{i:02d}" for i in range(MAX_DOMAINS_COUNT)),
            skills=tuple(f"s_{i:02d}" for i in range(MAX_SKILLS_COUNT)),
        )
        self.assertEqual(prof.profile_id, max_id)
        self.assertEqual(len(prof.task_types), MAX_TASK_TYPES_COUNT)
        self.assertEqual(len(prof.domains), MAX_DOMAINS_COUNT)
        self.assertEqual(len(prof.skills), MAX_SKILLS_COUNT)

    def test_46_boundary_plus_one_rejected(self) -> None:
        """Values exceeding boundary by 1 character or 1 item are rejected."""
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(profile_id="a" * (MAX_PROFILE_ID_CHARS + 1), display_name="Name", description="Desc")
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(profile_id="valid", display_name="b" * (MAX_DISPLAY_NAME_CHARS + 1), description="Desc")
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(profile_id="valid", display_name="Name", description="c" * (MAX_DESCRIPTION_CHARS + 1))


class TestAgentProfileStaticArchitecture(unittest.TestCase):
    """Static inspection verifying zero execution or external subsystem dependencies."""

    def test_47_no_forbidden_imports_or_calls(self) -> None:
        """Verify core/runtime/agent_profile.py contains no execution imports."""
        source = inspect.getsource(ap_mod)

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
            matches = re.findall(pattern, source)
            self.assertEqual(
                matches,
                [],
                f"Forbidden execution pattern '{pattern}' found in agent_profile.py: {matches}",
            )


class TestAgentProfileSecurityProperties(unittest.TestCase):
    """Explicitly verify P1.4A-S1 through P1.4A-S8 security properties."""

    def test_48_P1_4A_S1_profile_is_descriptive_not_authoritative(self) -> None:
        """P1.4A-S1: Profile is descriptive metadata only; cannot approve or execute."""
        prof = AgentProfile(profile_id="tester", display_name="Tester", description="Desc")
        self.assertFalse(hasattr(prof, "execute"))
        self.assertFalse(hasattr(prof, "approve"))
        self.assertFalse(hasattr(prof, "capabilities"))

    def test_49_P1_4A_S2_registry_cannot_grant_authority(self) -> None:
        """P1.4A-S2: Registry is an inventory of definitions, not an authorization gate."""
        reg = AgentProfileRegistry()
        self.assertFalse(hasattr(reg, "authorize"))
        self.assertFalse(hasattr(reg, "approve"))
        self.assertFalse(hasattr(reg, "grant"))

    def test_50_P1_4A_S3_profile_mutation_impossible_after_registration(self) -> None:
        """P1.4A-S3: Profile state is strictly immutable post-registration."""
        reg = AgentProfileRegistry()
        prof = AgentProfile(profile_id="fixed", display_name="Fixed", description="Desc", metadata={"k": 1})
        reg.register(prof)
        with self.assertRaises(TypeError):
            reg.get("fixed").metadata["k"] = 2  # type: ignore

    def test_51_P1_4A_S4_serialized_profiles_cannot_smuggle_authority(self) -> None:
        """P1.4A-S4: Serialized representations cannot smuggle capability grants."""
        raw_tampered = {
            "profile_id": "smuggler",
            "display_name": "Smuggler",
            "description": "Desc",
            "allow_shell": True,
        }
        with self.assertRaises(AgentProfileAuthorityViolationError):
            AgentProfile.from_dict(raw_tampered)

    def test_52_P1_4A_S5_malformed_profiles_fail_closed(self) -> None:
        """P1.4A-S5: Malformed schemas or unknown structures fail closed."""
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(profile_id="", display_name="Test", description="Desc")
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(profile_id="test", display_name="", description="Desc")
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile.from_dict({"profile_id": "test"})

    def test_53_P1_4A_S6_profile_identifiers_cannot_escape_domain(self) -> None:
        """P1.4A-S6: Profile IDs cannot traverse paths or access filesystem roots."""
        escapes = ("../../root", "/etc/passwd", "C:\\Windows", "..\\system")
        for esc in escapes:
            with self.assertRaises(AgentProfileValidationError):
                AgentProfile(profile_id=esc, display_name="Escaper", description="Desc")

    def test_54_P1_4A_S7_profile_size_is_bounded(self) -> None:
        """P1.4A-S7: Profile cannot exceed maximum size bounds."""
        with self.assertRaises(AgentProfileValidationError):
            AgentProfile(
                profile_id="large",
                display_name="Large",
                description="Desc",
                metadata={"data": "x" * (MAX_PROFILE_SERIALIZED_BYTES + 10)},
            )

    def test_55_P1_4A_S8_profile_implementation_has_no_execution_dependencies(self) -> None:
        """P1.4A-S8: agent_profile.py has zero execution dependencies."""
        source_path = Path(ap_mod.__file__)
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
                    f"Forbidden execution module '{mod}' imported in agent_profile.py",
                )

        # Confirm module attributes do not expose any execution classes/primitives
        for forbidden_attr in (
            "Orchestrator",
            "TransactionCoordinator",
            "ApprovalService",
            "ApprovedExecutionContract",
            "subprocess",
            "os",
        ):
            self.assertFalse(
                hasattr(ap_mod, forbidden_attr),
                f"Forbidden attribute '{forbidden_attr}' exposed in agent_profile module",
            )


if __name__ == "__main__":
    unittest.main()
