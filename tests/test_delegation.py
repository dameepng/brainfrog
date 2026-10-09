"""Comprehensive unit and security property test suite for P1.3B Delegation Contract.

Validates:
- Construction, deterministic fields, immutability, and time-bounded validity
- Mandatory identity and session-incarnation bindings
- Strict filesystem attenuation (parent ⊇ child, traversal/absolute/UNC rejection)
- Strict network attenuation (endpoint subsets, host matching, wildcard/downgrade rejection)
- Strict Git policy attenuation (no privilege escalation or remote push expansion)
- Expiration fail-closed behavior (no silent renewal or extension)
- Cryptographic SHA-256 digest integrity across all authority-bearing fields
- Monotonic delegation rule (children cannot expand authority of parent contract)
- Strict secret and credential scrubbing (API keys, tokens, passwords, private key PEMs)
- Architectural boundary assertions (zero execution authority, no orchestrator/subprocess)
- Property-oriented attenuation invariants
"""
from __future__ import annotations

import inspect
import json
import math
import re
import time
import unittest
from pathlib import Path
from typing import Any, Dict

import core.runtime.delegation as delegation_mod
from core.runtime.capabilities import Capabilities, FilesystemPolicy, GitPolicy, NetworkPolicy, ShellPolicy
from core.runtime.contract import ApprovedExecutionContract
from core.runtime.delegation import (
    CURRENT_DELEGATION_SCHEMA_VERSION,
    DelegationAttenuationError,
    DelegationContract,
    DelegationError,
    DelegationExpiredError,
    DelegationIntegrityError,
    DelegationValidationError,
    is_network_subset,
    is_path_subset,
    normalize_endpoint,
    normalize_target_path,
    validate_delegation_id,
)
from core.runtime.work import Work, WorkStatus


class TestDelegationConstruction(unittest.TestCase):
    """Test DelegationContract construction, immutability, and defaults."""

    def test_valid_contract_construction(self) -> None:
        now = time.time()
        contract = DelegationContract(
            delegation_id="delg_valid_01",
            parent_work_id="work_p100",
            child_subagent_id="sub_c200",
            actor="alice",
            session_id="sess_s300",
            session_incarnation_id="inc_i400",
            target_scope=("src/auth/**", "src/models/**"),
            operation_scope=("read_file", "write_file"),
            network_scope=("https://api.example.com",),
            created_at=now,
            expires_at=now + 300.0,
            role="code_analyzer",
            purpose="Analyze auth modules",
        )
        self.assertEqual(contract.delegation_id, "delg_valid_01")
        self.assertEqual(contract.id, "delg_valid_01")
        self.assertEqual(contract.parent_work_id, "work_p100")
        self.assertEqual(contract.child_subagent_id, "sub_c200")
        self.assertEqual(contract.actor, "alice")
        self.assertEqual(contract.session_id, "sess_s300")
        self.assertEqual(contract.session_incarnation_id, "inc_i400")
        self.assertEqual(contract.target_scope, ("src/auth/**", "src/models/**"))
        self.assertEqual(contract.operation_scope, ("read_file", "write_file"))
        self.assertEqual(contract.network_scope, ("https://api.example.com",))
        self.assertEqual(contract.schema_version, CURRENT_DELEGATION_SCHEMA_VERSION)
        self.assertFalse(contract.is_expired)
        self.assertTrue(len(contract.digest) == 64)

    def test_immutable_frozen_dataclass(self) -> None:
        now = time.time()
        contract = DelegationContract(
            delegation_id="delg_immut_01",
            parent_work_id="work_1",
            child_subagent_id="sub_1",
            actor="alice",
            session_id="sess_1",
            session_incarnation_id="inc_1",
            created_at=now,
            expires_at=now + 100.0,
        )
        with self.assertRaises((AttributeError, TypeError)):
            contract.actor = "mallory"  # type: ignore

        with self.assertRaises((AttributeError, TypeError)):
            contract.expires_at = now + 9999.0  # type: ignore

        with self.assertRaises((AttributeError, TypeError)):
            contract.target_scope = ("**",)  # type: ignore


class TestDelegationIdentityBinding(unittest.TestCase):
    """Test identity and session-incarnation binding validation and rejection."""

    def test_reject_empty_or_whitespace_identities(self) -> None:
        now = time.time()
        fields_to_test = [
            "delegation_id",
            "parent_work_id",
            "child_subagent_id",
            "actor",
            "session_id",
            "session_incarnation_id",
        ]
        for field_name in fields_to_test:
            for bad_val in ("", "   ", "\t"):
                kwargs = {
                    "delegation_id": "delg_1",
                    "parent_work_id": "work_1",
                    "child_subagent_id": "sub_1",
                    "actor": "alice",
                    "session_id": "sess_1",
                    "session_incarnation_id": "inc_1",
                    "created_at": now,
                    "expires_at": now + 100.0,
                }
                kwargs[field_name] = bad_val
                with self.assertRaises(DelegationValidationError):
                    DelegationContract(**kwargs)

    def test_reject_malformed_traversal_ids(self) -> None:
        now = time.time()
        for bad_id in ("../evil", "..\\evil", "delg/123", "delg\\123", "delg:stream", "delg*1", ".hidden", "~home"):
            with self.assertRaises(DelegationValidationError):
                DelegationContract(
                    delegation_id=bad_id,
                    parent_work_id="work_1",
                    child_subagent_id="sub_1",
                    actor="alice",
                    session_id="sess_1",
                    session_incarnation_id="inc_1",
                    created_at=now,
                    expires_at=now + 100.0,
                )

    def test_derive_rejects_identity_mismatch(self) -> None:
        now = time.time()
        parent = DelegationContract(
            delegation_id="delg_parent_1",
            parent_work_id="work_p1",
            child_subagent_id="sub_c1",
            actor="alice",
            session_id="sess_s1",
            session_incarnation_id="inc_i1",
            created_at=now,
            expires_at=now + 500.0,
        )

        # Mismatched parent_work_id
        with self.assertRaises(DelegationAttenuationError):
            DelegationContract.derive(
                parent_authority=parent,
                child_subagent_id="sub_child_2",
                parent_work_id="work_DIFFERENT",
            )

        # Mismatched actor
        with self.assertRaises(DelegationAttenuationError):
            DelegationContract.derive(
                parent_authority=parent,
                child_subagent_id="sub_child_2",
                actor="bob",
            )

        # Cross-session delegation
        with self.assertRaises(DelegationAttenuationError):
            DelegationContract.derive(
                parent_authority=parent,
                child_subagent_id="sub_child_2",
                session_id="sess_DIFFERENT",
            )

        # Stale session incarnation
        with self.assertRaises(DelegationAttenuationError):
            DelegationContract.derive(
                parent_authority=parent,
                child_subagent_id="sub_child_2",
                session_incarnation_id="inc_STALE",
            )

    def test_validate_rejects_stale_incarnation_and_mismatch(self) -> None:
        now = time.time()
        contract = DelegationContract(
            delegation_id="delg_valid_bind",
            parent_work_id="work_p1",
            child_subagent_id="sub_c1",
            actor="alice",
            session_id="sess_s1",
            session_incarnation_id="inc_v1",
            created_at=now,
            expires_at=now + 500.0,
        )
        # Valid matching binding
        contract.validate(
            actor="alice",
            session_id="sess_s1",
            session_incarnation_id="inc_v1",
            current_time=now + 10.0,
        )

        # Stale session incarnation rejected
        with self.assertRaises(DelegationValidationError):
            contract.validate(
                actor="alice",
                session_id="sess_s1",
                session_incarnation_id="inc_v2_NEW",
                current_time=now + 10.0,
            )

        # Actor mismatch rejected
        with self.assertRaises(DelegationValidationError):
            contract.validate(
                actor="mallory",
                session_id="sess_s1",
                session_incarnation_id="inc_v1",
                current_time=now + 10.0,
            )


class TestDelegationFilesystemAttenuation(unittest.TestCase):
    """Test filesystem target scope containment and traversal defenses."""

    def test_path_subset_evaluation(self) -> None:
        # 1. Exact match
        self.assertTrue(is_path_subset("src/main.py", "src/main.py"))

        # 2. Subtree glob containment (parent src/**, child src/auth/**) -> PASS
        self.assertTrue(is_path_subset("src/auth/**", "src/**"))
        self.assertTrue(is_path_subset("src/auth/login.py", "src/**"))
        self.assertTrue(is_path_subset("src/deep/nested/file.py", "src/**"))

        # 3. Direct children glob (parent src/*)
        self.assertTrue(is_path_subset("src/a.py", "src/*"))
        self.assertFalse(is_path_subset("src/sub/nested.py", "src/*"))

        # 4. Inversion: parent src/auth/**, child src/** -> REJECT
        self.assertFalse(is_path_subset("src/**", "src/auth/**"))

        # 5. Disjoint paths
        self.assertFalse(is_path_subset("tests/**", "src/**"))
        self.assertFalse(is_path_subset("src_other/**", "src/**"))

    def test_path_traversal_and_escapes_rejected(self) -> None:
        bad_paths = [
            "../src/**",
            "../../etc/passwd",
            "src/../../../escape",
            "/etc/passwd",
            "/src/**",
            "C:/Windows/system32",
            "D:/workspace/file",
            "//server/share/file",
            "\\\\server\\share\\file",
            ".git/config",
            ".brainfrog/works/secret.json",
            "src/auth:stream",
        ]
        for bad in bad_paths:
            with self.assertRaises(DelegationAttenuationError):
                normalize_target_path(bad)
            with self.assertRaises(DelegationAttenuationError):
                is_path_subset(bad, "src/**")

    def test_derive_filesystem_attenuation(self) -> None:
        now = time.time()
        parent = DelegationContract(
            delegation_id="delg_p_fs",
            parent_work_id="work_p1",
            child_subagent_id="sub_c1",
            actor="alice",
            session_id="sess_1",
            session_incarnation_id="inc_1",
            target_scope=("src/**", "tests/**"),
            created_at=now,
            expires_at=now + 500.0,
        )

        # 1. Valid subset: child requests src/auth/** -> PASS
        child = DelegationContract.derive(
            parent_authority=parent,
            child_subagent_id="sub_child_fs",
            requested_target_scope=("src/auth/**",),
        )
        self.assertEqual(child.target_scope, ("src/auth/**",))

        # 2. Child requests broader target (e.g. root or other folder) -> REJECT
        with self.assertRaises(DelegationAttenuationError):
            DelegationContract.derive(
                parent_authority=parent,
                child_subagent_id="sub_child_fs",
                requested_target_scope=("docs/**",),
            )

        # 3. Child requests parent's parent -> REJECT
        parent_narrow = DelegationContract(
            delegation_id="delg_p_fs_narrow",
            parent_work_id="work_p1",
            child_subagent_id="sub_c1",
            actor="alice",
            session_id="sess_1",
            session_incarnation_id="inc_1",
            target_scope=("src/auth/**",),
            created_at=now,
            expires_at=now + 500.0,
        )
        with self.assertRaises(DelegationAttenuationError):
            DelegationContract.derive(
                parent_authority=parent_narrow,
                child_subagent_id="sub_child_fs",
                requested_target_scope=("src/**",),
            )


class TestDelegationNetworkAttenuation(unittest.TestCase):
    """Test network endpoint attenuation and wildcard/downgrade prevention."""

    def test_network_subset_evaluation(self) -> None:
        # 1. Exact match -> PASS
        self.assertTrue(is_network_subset("api.example.com", "api.example.com"))
        self.assertTrue(is_network_subset("https://api.example.com", "https://api.example.com"))

        # 2. Port and path normalization
        self.assertTrue(is_network_subset("https://api.example.com/v1", "https://api.example.com"))

        # 3. Wildcard domain: parent *.example.com allows api.example.com -> PASS
        self.assertTrue(is_network_subset("api.example.com", "*.example.com"))
        self.assertTrue(is_network_subset("sub.api.example.com", "*.example.com"))

        # 4. Host mismatch -> REJECT
        self.assertFalse(is_network_subset("evil.com", "api.example.com"))
        self.assertFalse(is_network_subset("other.example.com", "api.example.com"))

        # 5. Wildcard expansion: child requests * when parent is specific -> REJECT
        self.assertFalse(is_network_subset("*", "api.example.com"))
        self.assertFalse(is_network_subset("*.example.com", "api.example.com"))

        # 6. Scheme downgrade: parent https, child http -> REJECT
        self.assertFalse(is_network_subset("http://api.example.com", "https://api.example.com"))

    def test_derive_network_attenuation(self) -> None:
        now = time.time()
        parent = DelegationContract(
            delegation_id="delg_p_net",
            parent_work_id="work_p1",
            child_subagent_id="sub_c1",
            actor="alice",
            session_id="sess_1",
            session_incarnation_id="inc_1",
            network_scope=("https://api.example.com",),
            created_at=now,
            expires_at=now + 500.0,
        )

        # 1. Child requests same endpoint -> PASS
        child = DelegationContract.derive(
            parent_authority=parent,
            child_subagent_id="sub_c_net",
            requested_network_scope=("https://api.example.com",),
        )
        self.assertEqual(child.network_scope, ("https://api.example.com",))

        # 2. Child requests unauthorized host -> REJECT
        with self.assertRaises(DelegationAttenuationError):
            DelegationContract.derive(
                parent_authority=parent,
                child_subagent_id="sub_c_net",
                requested_network_scope=("https://unauthorized.org",),
            )

        # 3. Child requests wildcard -> REJECT
        with self.assertRaises(DelegationAttenuationError):
            DelegationContract.derive(
                parent_authority=parent,
                child_subagent_id="sub_c_net",
                requested_network_scope=("*",),
            )


class TestDelegationGitAttenuation(unittest.TestCase):
    """Test Git policy attenuation and remote push privilege escalation prevention."""

    def test_child_cannot_gain_git_capabilities_when_parent_lacks_them(self) -> None:
        now = time.time()
        parent = DelegationContract(
            delegation_id="delg_p_git",
            parent_work_id="work_p1",
            child_subagent_id="sub_c1",
            actor="alice",
            session_id="sess_1",
            session_incarnation_id="inc_1",
            git_policy=GitPolicy(read=True, commit=False, push=False),
            created_at=now,
            expires_at=now + 500.0,
        )

        # Child requesting push when parent has commit=False, push=False -> REJECT
        with self.assertRaises(DelegationAttenuationError):
            DelegationContract.derive(
                parent_authority=parent,
                child_subagent_id="sub_c_git",
                requested_git_policy=GitPolicy(read=True, commit=False, push=True),
            )

        # Child requesting commit when parent has commit=False -> REJECT
        with self.assertRaises(DelegationAttenuationError):
            DelegationContract.derive(
                parent_authority=parent,
                child_subagent_id="sub_c_git",
                requested_git_policy=GitPolicy(read=True, commit=True, push=False),
            )

        # Child requesting read-only Git -> PASS
        child = DelegationContract.derive(
            parent_authority=parent,
            child_subagent_id="sub_c_git",
            requested_git_policy=GitPolicy(read=True, commit=False, push=False),
        )
        self.assertEqual(child.git_policy, GitPolicy(read=True, commit=False, push=False))


class TestDelegationExpiration(unittest.TestCase):
    """Test time-bounded delegation and expiration fail-closed behavior."""

    def test_reject_expiration_not_greater_than_created_at(self) -> None:
        now = time.time()
        for bad_exp in (now, now - 1.0, now - 100.0):
            with self.assertRaises(DelegationValidationError):
                DelegationContract(
                    delegation_id="delg_exp_bad",
                    parent_work_id="work_1",
                    child_subagent_id="sub_1",
                    actor="alice",
                    session_id="sess_1",
                    session_incarnation_id="inc_1",
                    created_at=now,
                    expires_at=bad_exp,
                )

    def test_child_expiration_cannot_exceed_parent_expiration(self) -> None:
        now = time.time()
        parent = DelegationContract(
            delegation_id="delg_p_exp",
            parent_work_id="work_1",
            child_subagent_id="sub_1",
            actor="alice",
            session_id="sess_1",
            session_incarnation_id="inc_1",
            created_at=now,
            expires_at=now + 200.0,
        )
        # Requesting expiration beyond parent -> REJECT
        with self.assertRaises(DelegationAttenuationError):
            DelegationContract.derive(
                parent_authority=parent,
                child_subagent_id="sub_child",
                expires_at=now + 300.0,
            )

    def test_expired_contract_fails_closed(self) -> None:
        now = time.time()
        contract = DelegationContract(
            delegation_id="delg_expired",
            parent_work_id="work_1",
            child_subagent_id="sub_1",
            actor="alice",
            session_id="sess_1",
            session_incarnation_id="inc_1",
            created_at=now - 200.0,
            expires_at=now - 10.0,  # Expired
        )
        self.assertTrue(contract.is_expired)
        with self.assertRaises(DelegationExpiredError):
            contract.validate(
                actor="alice",
                session_id="sess_1",
                session_incarnation_id="inc_1",
            )

        # Deriving from expired contract fails closed
        with self.assertRaises(DelegationExpiredError):
            DelegationContract.derive(
                parent_authority=contract,
                child_subagent_id="sub_child_2",
            )


class TestDelegationDigestIntegrity(unittest.TestCase):
    """Test cryptographic SHA-256 digest validation across all authority-bearing fields."""

    def _create_valid(self) -> DelegationContract:
        now = time.time()
        return DelegationContract(
            delegation_id="delg_int_01",
            parent_work_id="work_p1",
            child_subagent_id="sub_c1",
            actor="alice",
            session_id="sess_s1",
            session_incarnation_id="inc_i1",
            capabilities=Capabilities(
                filesystem=FilesystemPolicy(read=("src/main.py",), write=("src/out.txt",)),
            ),
            target_scope=("src/out.txt",),
            operation_scope=("write_file",),
            network_scope=("https://api.example.com",),
            git_policy=GitPolicy(read=True, commit=False, push=False),
            created_at=now,
            expires_at=now + 300.0,
            parent_delegation_id="delg_parent_00",
        )

    def test_digest_tampering_detected_for_every_authority_field(self) -> None:
        contract = self._create_valid()
        base_dict = contract.to_dict()

        # Mutate individual fields and verify from_dict rejects with DelegationIntegrityError
        mutations = [
            ("target_scope", ["src/**", "tests/**"]),
            ("operation_scope", ["write_file", "shell_exec"]),
            ("network_scope", ["*"]),
            ("actor", "mallory"),
            ("parent_work_id", "work_FORGED"),
            ("child_subagent_id", "sub_FORGED"),
            ("session_id", "sess_FORGED"),
            ("session_incarnation_id", "inc_FORGED"),
            ("expires_at", contract.expires_at + 1000.0),
            ("git_policy", {"read": True, "commit": True, "push": True}),
        ]
        for key, forged_val in mutations:
            tampered = dict(base_dict)
            tampered[key] = forged_val
            with self.assertRaises(DelegationIntegrityError, msg=f"Tampering with '{key}' was not detected"):
                DelegationContract.from_dict(tampered)


class TestDelegationMonotonicRule(unittest.TestCase):
    """Test that authority attenuation is strictly monotonic across derivation chains."""

    def test_monotonic_chain_derivation(self) -> None:
        now = time.time()
        # Level 1: Parent
        p1 = DelegationContract(
            delegation_id="delg_level_1",
            parent_work_id="work_root",
            child_subagent_id="sub_worker_1",
            actor="alice",
            session_id="sess_root",
            session_incarnation_id="inc_root",
            target_scope=("src/**", "tests/**"),
            operation_scope=("read_file", "write_file"),
            created_at=now,
            expires_at=now + 1000.0,
        )

        # Level 2: Child A (attenuated to src/auth/**)
        c2 = DelegationContract.derive(
            parent_authority=p1,
            child_subagent_id="sub_worker_2",
            requested_target_scope=("src/auth/**",),
            requested_operation_scope=("read_file",),
            ttl_seconds=500.0,
        )
        self.assertEqual(c2.target_scope, ("src/auth/**",))
        self.assertEqual(c2.operation_scope, ("read_file",))
        self.assertEqual(c2.parent_delegation_id, "delg_level_1")

        # Level 3: Child B requests expansion back to src/** -> MUST BE REJECTED
        with self.assertRaises(DelegationAttenuationError):
            DelegationContract.derive(
                parent_authority=c2,
                child_subagent_id="sub_worker_3",
                requested_target_scope=("src/**",),
            )

        # Level 3: Child B requests write_file when c2 only has read_file -> MUST BE REJECTED
        with self.assertRaises(DelegationAttenuationError):
            DelegationContract.derive(
                parent_authority=c2,
                child_subagent_id="sub_worker_3",
                requested_operation_scope=("read_file", "write_file"),
            )

        # Level 3: Valid further attenuation (src/auth/jwt/**) -> PASS
        c3 = DelegationContract.derive(
            parent_authority=c2,
            child_subagent_id="sub_worker_3",
            requested_target_scope=("src/auth/jwt/**",),
            requested_operation_scope=("read_file",),
            ttl_seconds=200.0,
        )
        self.assertEqual(c3.target_scope, ("src/auth/jwt/**",))
        self.assertEqual(c3.parent_delegation_id, c2.delegation_id)


class TestDelegationSecretSafety(unittest.TestCase):
    """Test canonical secret rejection across all DelegationContract fields."""

    def test_reject_secrets_across_all_fields(self) -> None:
        now = time.time()

        def make_contract(**kwargs: Any) -> DelegationContract:
            base = {
                "delegation_id": "delg_sec_01",
                "parent_work_id": "work_1",
                "child_subagent_id": "sub_1",
                "actor": "alice",
                "session_id": "sess_1",
                "session_incarnation_id": "inc_1",
                "created_at": now,
                "expires_at": now + 200.0,
            }
            merged = {**base, **kwargs}
            return DelegationContract(
                delegation_id=str(merged["delegation_id"]),
                parent_work_id=str(merged["parent_work_id"]),
                child_subagent_id=str(merged["child_subagent_id"]),
                actor=str(merged["actor"]),
                session_id=str(merged["session_id"]),
                session_incarnation_id=str(merged["session_incarnation_id"]),
                created_at=float(merged["created_at"]),
                expires_at=float(merged["expires_at"]),
                role=str(merged.get("role", "")),
                purpose=str(merged.get("purpose", "")),
                metadata=merged.get("metadata", None),
            )

        # 1. API key in role
        with self.assertRaises(ValueError):
            make_contract(role="sk-proj-" + "123456789012345678901234567890123456789012345678")

        # 2. Bearer token in purpose
        with self.assertRaises(ValueError):
            make_contract(purpose="Authorization: Bearer my_secret_token_12345")

        # 3. Password in metadata
        with self.assertRaises(ValueError):
            make_contract(metadata={"password": "admin_password"})

        # 4. Unencrypted private key PEM
        pem = "-----BEGIN " + "PRIVATE KEY-----\nMIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQC7\n-----END " + "PRIVATE KEY-----"
        with self.assertRaises(ValueError):
            make_contract(purpose=pem)

        # 5. Encrypted private key PEM
        enc_pem = "-----BEGIN " + "ENCRYPTED PRIVATE KEY-----\nMIIFDjBABgkqhkiG9w0BBQ0wMzAbBgkqhkiG9w0BBQwwDgQI\n-----END " + "ENCRYPTED PRIVATE KEY-----"
        with self.assertRaises(ValueError):
            make_contract(purpose=enc_pem)


class TestDelegationArchitecturalBoundary(unittest.TestCase):
    """Assert that core/runtime/delegation.py contains no execution engines or authority."""

    def test_module_contains_no_execution_imports(self) -> None:
        src = inspect.getsource(delegation_mod)
        prohibited_terms = [
            "orchestrator",
            "Orchestrator",
            "subprocess",
            "os.system",
            "os.popen",
            "shutil",
            "ApprovalService",
            "ApprovalStore",
            "TransactionCoordinator",
            "provider",
        ]
        for term in prohibited_terms:
            pattern = rf"\b{re.escape(term)}\b"
            matches = [
                line for line in src.splitlines()
                if re.search(pattern, line) and not line.strip().startswith(("#", '"""', "'''", "*", "-"))
            ]
            self.assertEqual(matches, [], f"Prohibited reference '{term}' found in delegation.py: {matches}")

    def test_delegation_contract_has_no_execution_methods(self) -> None:
        for m in dir(DelegationContract):
            if m.startswith("_"):
                continue
            for forbidden in ("run", "exec", "execute", "mutate", "apply", "dispatch", "approve", "fork"):
                self.assertNotIn(
                    forbidden,
                    m.lower(),
                    f"DelegationContract exposes suspicious execution method '{m}'",
                )


class TestDelegationSerialization(unittest.TestCase):
    """Test serialization round-trip equality and fail-closed validation."""

    def test_round_trip_equality(self) -> None:
        now = time.time()
        orig = DelegationContract(
            delegation_id="delg_rt_01",
            parent_work_id="work_p10",
            child_subagent_id="sub_c20",
            actor="alice",
            session_id="sess_s30",
            session_incarnation_id="inc_i40",
            capabilities=Capabilities(
                filesystem=FilesystemPolicy(read=("src/main.py",), write=("src/out.txt",)),
            ),
            target_scope=("src/out.txt",),
            operation_scope=("write_file",),
            network_scope=("https://api.example.com",),
            git_policy=GitPolicy(read=True, commit=False, push=False),
            created_at=now,
            expires_at=now + 500.0,
            role="coder",
            purpose="Write module",
            metadata={"env": "dev"},
        )
        serialized = orig.to_dict()
        self.assertIsInstance(serialized, dict)
        self.assertEqual(serialized["delegation_id"], "delg_rt_01")

        deserialized = DelegationContract.from_dict(serialized)
        self.assertEqual(deserialized, orig)

    def test_from_dict_rejects_non_dict_and_unknown_fields(self) -> None:
        with self.assertRaises(DelegationValidationError):
            DelegationContract.from_dict("not_a_dict")  # type: ignore

        valid_dict = DelegationContract(
            delegation_id="delg_extra_test",
            parent_work_id="work_1",
            child_subagent_id="sub_1",
            actor="alice",
            session_id="sess_1",
            session_incarnation_id="inc_1",
            created_at=time.time(),
            expires_at=time.time() + 100.0,
        ).to_dict()

        # Injected unexpected authority key
        corrupted = dict(valid_dict)
        corrupted["sudo"] = True
        with self.assertRaises(DelegationValidationError):
            DelegationContract.from_dict(corrupted)


class TestDelegationPropertyInvariants(unittest.TestCase):
    """Property-oriented verification of the core invariant:
    result exists -> result.scope <= parent.scope, NEVER result.scope > parent.scope.
    """

    def setUp(self) -> None:
        self.now = time.time()
        self.base_parent = DelegationContract(
            delegation_id="delg_prop_parent",
            parent_work_id="work_parent_prop",
            child_subagent_id="sub_worker_prop",
            actor="alice",
            session_id="sess_prop",
            session_incarnation_id="inc_prop",
            target_scope=("src/**", "docs/*"),
            operation_scope=("read_file", "write_file"),
            network_scope=("https://api.example.com", "*.service.local"),
            git_policy=GitPolicy(read=True, commit=True, push=False),
            created_at=self.now,
            expires_at=self.now + 600.0,
        )

    def test_property_filesystem_containment(self) -> None:
        test_cases = [
            # (requested_targets, should_succeed)
            (("src/auth/login.py",), True),
            (("src/models/**",), True),
            (("src/utils/*",), True),
            (("docs/readme.txt",), True),
            (("src/**",), True),
            (("docs/*",), True),
            (("docs/deep/nested.md",), False),   # docs/* only permits direct children
            (("tests/**",), False),               # disjoint
            (("other/file.py",), False),          # disjoint
            (("src/**", "tests/**"), False),      # expansion
            (("",), False),                       # empty
            (("../escape",), False),              # traversal
            (("/etc/passwd",), False),            # absolute
        ]
        for requested, expected_success in test_cases:
            if expected_success:
                derived = DelegationContract.derive(
                    parent_authority=self.base_parent,
                    child_subagent_id="sub_child_fs",
                    requested_target_scope=requested,
                )
                self.assertIsNotNone(derived)
                # Invariant: every derived target must be subset of at least one parent target
                for ct in derived.target_scope:
                    self.assertTrue(
                        any(is_path_subset(ct, pt) for pt in self.base_parent.target_scope),
                        f"Derived target {ct} exceeds parent scope",
                    )
            else:
                with self.assertRaises((DelegationAttenuationError, DelegationValidationError)):
                    DelegationContract.derive(
                        parent_authority=self.base_parent,
                        child_subagent_id="sub_child_fs",
                        requested_target_scope=requested,
                    )

    def test_property_network_containment(self) -> None:
        test_cases = [
            # (requested_network, should_succeed)
            (("https://api.example.com",), True),
            (("https://auth.service.local",), True),
            (("http://api.example.com",), False),         # downgrade
            (("https://api.example.com:8443",), False),    # port mismatch
            (("*",), False),                               # wildcard expansion
            (("https://evil.com",), False),               # unpermitted host
            (("https://api.example.com", "https://evil.com"), False),
        ]
        for requested, expected_success in test_cases:
            if expected_success:
                derived = DelegationContract.derive(
                    parent_authority=self.base_parent,
                    child_subagent_id="sub_child_net",
                    requested_network_scope=requested,
                )
                self.assertIsNotNone(derived)
                for cn in derived.network_scope:
                    self.assertTrue(
                        any(is_network_subset(cn, pn) for pn in self.base_parent.network_scope),
                        f"Derived endpoint {cn} exceeds parent scope",
                    )
            else:
                with self.assertRaises(DelegationAttenuationError):
                    DelegationContract.derive(
                        parent_authority=self.base_parent,
                        child_subagent_id="sub_child_net",
                        requested_network_scope=requested,
                    )

    def test_property_git_policy_attenuation(self) -> None:
        # Parent has: read=True, commit=True, push=False
        all_combinations = [
            (r, c, p)
            for r in (False, True)
            for c in (False, True)
            for p in (False, True)
        ]
        for r, c, p in all_combinations:
            candidate_policy = GitPolicy(read=r, commit=c, push=p)
            # Allowed iff child policies are subsets of parent (True in child implies True in parent)
            allowed = (
                (not r or self.base_parent.git_policy.read)
                and (not c or self.base_parent.git_policy.commit)
                and (not p or self.base_parent.git_policy.push)
            )
            if allowed:
                derived = DelegationContract.derive(
                    parent_authority=self.base_parent,
                    child_subagent_id="sub_child_git",
                    requested_git_policy=candidate_policy,
                )
                self.assertTrue(derived.git_policy.read <= self.base_parent.git_policy.read)
                self.assertTrue(derived.git_policy.commit <= self.base_parent.git_policy.commit)
                self.assertTrue(derived.git_policy.push <= self.base_parent.git_policy.push)
                self.assertFalse(derived.git_policy.push)  # parent push was False, child push MUST be False
            else:
                with self.assertRaises(DelegationAttenuationError):
                    DelegationContract.derive(
                        parent_authority=self.base_parent,
                        child_subagent_id="sub_child_git",
                        requested_git_policy=candidate_policy,
                    )

    def test_property_expiration_monotonicity(self) -> None:
        # Child requested expiration can never exceed parent's expiration
        # Case 1: Child requests TTL within parent bounds -> ok
        derived_ok = DelegationContract.derive(
            parent_authority=self.base_parent,
            child_subagent_id="sub_child_exp",
            ttl_seconds=100.0,
        )
        self.assertLessEqual(derived_ok.expires_at, self.base_parent.expires_at)

        # Case 2: Child requests TTL exceeding parent expiration -> fails closed
        with self.assertRaises(DelegationAttenuationError):
            DelegationContract.derive(
                parent_authority=self.base_parent,
                child_subagent_id="sub_child_exp",
                ttl_seconds=10000.0,
            )


if __name__ == "__main__":
    unittest.main()
