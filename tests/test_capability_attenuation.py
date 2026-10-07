"""BrainFrog Phase P1.3C — Capability Attenuation Security and Invariant Test Suite.

Proves and enforces:
    A delegated child can never obtain more authority than its parent.
    CHILD ⊆ PARENT across all authority-bearing dimensions.

Test Categories:
A. Happy paths (identical, narrower, combined)
B. Filesystem traversal and escape defenses (.., ., UNC, drive, absolute, sibling prefix)
C. Network escalation defenses (wildcards, hosts, ports, protocols, userinfo)
D. Operation escalation defenses (privilege hierarchy, unknown operations)
E. Git policy escalation defenses (read-only -> push, commit, force-push)
F. Identity and session binding preservation
G. Time and expiration bounds (monotonicity, expired parent rejection)
H. Cryptographic integrity and digest verification
I. Immutability of parent authority and derived child contract
J. Composition and transitivity across multi-tier delegation chains
K. Malformed, unknown, and ambiguous inputs (fail-closed)
L. Deterministic property-style authority invariance matrix
M. Architectural boundary assertions (zero execution authority)
"""
from __future__ import annotations

import inspect
import json
import os
import re
import tempfile
import time
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import core.runtime.capabilities as capabilities_mod
import core.runtime.delegation as delegation_mod
from core.runtime.capabilities import (
    Capabilities,
    FilesystemPolicy,
    GitPolicy,
    NetworkPolicy,
    ShellPolicy,
)
from core.runtime.contract import ApprovedExecutionContract
from core.runtime.delegation import (
    CURRENT_DELEGATION_SCHEMA_VERSION,
    KNOWN_OPERATIONS,
    DelegationAttenuationError,
    DelegationContract,
    DelegationError,
    DelegationExpiredError,
    DelegationIntegrityError,
    DelegationValidationError,
    attenuate_capabilities,
    attenuate_delegation,
    attenuate_filesystem_policy,
    attenuate_git_policy,
    attenuate_network_policy,
    attenuate_network_scope,
    attenuate_operation_scope,
    attenuate_shell_policy,
    attenuate_target_scope,
    is_network_subset,
    is_path_subset,
    normalize_endpoint,
    normalize_target_path,
)


class TestCategoryAHappyPaths(unittest.TestCase):
    """Category A: Happy paths for identical, narrower, and combined capability attenuation."""

    def setUp(self) -> None:
        self.now = time.time()
        self.parent = DelegationContract(
            delegation_id="delg_parent_a",
            parent_work_id="work_p1",
            child_subagent_id="sub_p1",
            actor="alice",
            session_id="sess_alpha",
            session_incarnation_id="inc_alpha_1",
            capabilities=Capabilities(
                filesystem=FilesystemPolicy(read=("src/auth.py", "src/models.py"), write=("src/models.py",)),
                shell=ShellPolicy(execute=False),
                network=NetworkPolicy(access=True, scope="configured_model_api"),
                git=GitPolicy(read=True, commit=True, push=False),
            ),
            target_scope=("src/**", "tests/**"),
            operation_scope=("read_file", "write_file"),
            network_scope=("https://api.example.com", "https://auth.service.local"),
            git_policy=GitPolicy(read=True, commit=True, push=False),
            created_at=self.now,
            expires_at=self.now + 600.0,
            role="planner",
            purpose="System planning",
        )

    def test_identical_scope_delegation(self) -> None:
        """A child requesting identical scope to the parent is fully allowed."""
        child = DelegationContract.derive(
            parent_authority=self.parent,
            child_subagent_id="sub_child_identical",
            requested_target_scope=self.parent.target_scope,
            requested_operation_scope=self.parent.operation_scope,
            requested_network_scope=self.parent.network_scope,
            requested_capabilities=self.parent.capabilities,
            requested_git_policy=self.parent.git_policy,
            ttl_seconds=300.0,
        )
        self.assertEqual(child.target_scope, self.parent.target_scope)
        self.assertEqual(child.operation_scope, self.parent.operation_scope)
        self.assertEqual(child.network_scope, self.parent.network_scope)
        self.assertEqual(child.capabilities, self.parent.capabilities)
        self.assertEqual(child.git_policy, self.parent.git_policy)
        self.assertEqual(child.actor, self.parent.actor)
        self.assertEqual(child.session_id, self.parent.session_id)
        self.assertEqual(child.parent_delegation_id, self.parent.delegation_id)

    def test_narrower_filesystem_target_scope(self) -> None:
        """Narrowing filesystem target scope to a specific subtree or file is allowed."""
        child = DelegationContract.derive(
            parent_authority=self.parent,
            child_subagent_id="sub_child_fs_narrow",
            requested_target_scope=("src/auth/**",),
        )
        self.assertEqual(child.target_scope, ("src/auth/**",))

        # Single file inside parent's src/**
        child_file = DelegationContract.derive(
            parent_authority=self.parent,
            child_subagent_id="sub_child_fs_file",
            requested_target_scope=("src/auth/jwt.py",),
        )
        self.assertEqual(child_file.target_scope, ("src/auth/jwt.py",))

    def test_narrower_network_scope(self) -> None:
        """Narrowing network endpoints to a single endpoint is allowed."""
        child = DelegationContract.derive(
            parent_authority=self.parent,
            child_subagent_id="sub_child_net_narrow",
            requested_network_scope=("https://api.example.com",),
        )
        self.assertEqual(child.network_scope, ("https://api.example.com",))

    def test_narrower_operation_scope(self) -> None:
        """Narrowing operations (e.g. read_file only when parent has read+write) is allowed."""
        child = DelegationContract.derive(
            parent_authority=self.parent,
            child_subagent_id="sub_child_op_narrow",
            requested_operation_scope=("read_file",),
        )
        self.assertEqual(child.operation_scope, ("read_file",))

    def test_narrower_git_policy(self) -> None:
        """Attenuating Git policy from commit to read-only is allowed."""
        child = DelegationContract.derive(
            parent_authority=self.parent,
            child_subagent_id="sub_child_git_narrow",
            requested_git_policy=GitPolicy(read=True, commit=False, push=False),
        )
        self.assertEqual(child.git_policy, GitPolicy(read=True, commit=False, push=False))

    def test_combined_simultaneous_attenuation(self) -> None:
        """Simultaneously narrowing all capability dimensions succeeds."""
        child = attenuate_delegation(
            self.parent,
            child_subagent_id="sub_child_combo",
            requested_target_scope=("src/auth/jwt.py",),
            requested_operation_scope=("read_file",),
            requested_network_scope=("https://api.example.com",),
            requested_capabilities=Capabilities(
                filesystem=FilesystemPolicy(read=("src/auth.py",), write=()),
                shell=ShellPolicy(execute=False),
                network=NetworkPolicy(access=False),
                git=GitPolicy(read=True, commit=False, push=False),
            ),
            requested_git_policy=GitPolicy(read=True, commit=False, push=False),
            ttl_seconds=100.0,
        )
        self.assertEqual(child.target_scope, ("src/auth/jwt.py",))
        self.assertEqual(child.operation_scope, ("read_file",))
        self.assertEqual(child.network_scope, ("https://api.example.com",))
        self.assertEqual(child.capabilities.filesystem.read, ("src/auth.py",))
        self.assertEqual(child.capabilities.filesystem.write, ())
        self.assertFalse(child.capabilities.network.access)
        self.assertEqual(child.git_policy, GitPolicy(read=True, commit=False, push=False))

    def test_direct_attenuate_capabilities_api(self) -> None:
        """Test standalone attenuate_capabilities function directly."""
        parent_caps = Capabilities(
            filesystem=FilesystemPolicy(read=("src/a.py", "src/b.py"), write=("src/b.py",)),
            shell=ShellPolicy(execute=False),
            network=NetworkPolicy(access=True, scope="configured_model_api"),
            git=GitPolicy(read=True, commit=True, push=False),
        )
        child_req = Capabilities(
            filesystem=FilesystemPolicy(read=("src/a.py",), write=()),
            shell=ShellPolicy(execute=False),
            network=NetworkPolicy(access=False),
            git=GitPolicy(read=True, commit=False, push=False),
        )
        result = attenuate_capabilities(parent_caps, child_req)
        self.assertEqual(result.filesystem.read, ("src/a.py",))
        self.assertEqual(result.filesystem.write, ())
        self.assertFalse(result.network.access)
        self.assertEqual(result.git, GitPolicy(read=True, commit=False, push=False))


class TestCategoryBFilesystemTraversalAndContainment(unittest.TestCase):
    """Category B: Filesystem traversal, path escapes, and hierarchy containment."""

    def test_path_traversal_double_dot_rejected(self) -> None:
        """Every variant of '..' traversal must fail closed."""
        traversal_cases = [
            "../",
            "../../etc/passwd",
            "src/..",
            "src/../secrets/**",
            "src/auth/../../escape",
            "src/a/b/../../..",
            "..",
            "dir/..",
            "dir/../..",
        ]
        for bad_path in traversal_cases:
            with self.subTest(bad_path=bad_path):
                with self.assertRaises(DelegationAttenuationError):
                    normalize_target_path(bad_path)
                with self.assertRaises(DelegationAttenuationError):
                    is_path_subset(bad_path, "src/**")

    def test_dot_components_rejected(self) -> None:
        """Dot components ('.', './src', 'src/.', 'src/./auth') must be rejected as traversal."""
        dot_cases = [
            ".",
            "./src",
            "./src/**",
            "src/.",
            "src/./auth",
            "src/./auth/**",
            "src/auth/.",
        ]
        for dot_path in dot_cases:
            with self.subTest(dot_path=dot_path):
                with self.assertRaises(DelegationAttenuationError):
                    normalize_target_path(dot_path)
                with self.assertRaises(DelegationAttenuationError):
                    is_path_subset(dot_path, "src/**")

    def test_repeated_separators_normalized_safely(self) -> None:
        """Repeated separators inside paths normalize cleanly without escaping."""
        self.assertEqual(normalize_target_path("src///auth"), "src/auth")
        self.assertEqual(normalize_target_path("src////auth/**"), "src/auth/**")
        self.assertTrue(is_path_subset("src///auth/**", "src/**"))

    def test_absolute_paths_rejected(self) -> None:
        """POSIX and Windows absolute paths must be rejected."""
        absolute_cases = [
            "/etc/**",
            "/etc/passwd",
            "/src/**",
            "/",
            "\\Windows\\system32",
            "\\Program Files",
        ]
        for abs_path in absolute_cases:
            with self.subTest(abs_path=abs_path):
                with self.assertRaises(DelegationAttenuationError):
                    normalize_target_path(abs_path)
                with self.assertRaises(DelegationAttenuationError):
                    is_path_subset(abs_path, "src/**")

    def test_unc_paths_rejected(self) -> None:
        """UNC paths (both forward and backward slash) must be rejected."""
        unc_cases = [
            "//server/share/**",
            "//server/share/file.txt",
            "\\\\server\\share\\**",
            "\\\\192.168.1.1\\c$",
        ]
        for unc in unc_cases:
            with self.subTest(unc=unc):
                with self.assertRaises(DelegationAttenuationError):
                    normalize_target_path(unc)
                with self.assertRaises(DelegationAttenuationError):
                    is_path_subset(unc, "src/**")

    def test_drive_letter_paths_rejected(self) -> None:
        """Drive-letter paths must be rejected."""
        drive_cases = [
            "C:/Windows/**",
            "C:\\Windows\\system32",
            "D:/workspace/src",
            "c:relative_file",
        ]
        for drive in drive_cases:
            with self.subTest(drive=drive):
                with self.assertRaises(DelegationAttenuationError):
                    normalize_target_path(drive)
                with self.assertRaises(DelegationAttenuationError):
                    is_path_subset(drive, "src/**")

    def test_sibling_prefix_confusion_blocked(self) -> None:
        """Prevent sibling prefix confusion: 'src' must NEVER match 'src_evil'."""
        self.assertFalse(is_path_subset("src_evil/**", "src/**"))
        self.assertFalse(is_path_subset("src_evil", "src"))
        self.assertFalse(is_path_subset("src_evil/", "src/"))
        self.assertFalse(is_path_subset("src/auth_evil/**", "src/auth/**"))
        self.assertFalse(is_path_subset("src/main.py.bak", "src/main.py"))

    def test_exact_file_and_directory_hierarchy_semantics(self) -> None:
        """Test exact file vs directory scope semantics."""
        # Exact file -> same file: ALLOW
        self.assertTrue(is_path_subset("src/auth.py", "src/auth.py"))

        # Exact file -> sibling file: DENY
        self.assertFalse(is_path_subset("src/other.py", "src/auth.py"))

        # Parent directory -> descendant file: ALLOW (explicit directory scope)
        self.assertTrue(is_path_subset("src/auth/login.py", "src/auth/"))
        # Exact path without directory scope -> descendant file: DENY
        self.assertFalse(is_path_subset("src/auth/login.py", "src/auth"))

        # Descendant -> parent: DENY
        self.assertFalse(is_path_subset("src", "src/auth"))
        self.assertFalse(is_path_subset("src/auth", "src/auth/login.py"))
        self.assertFalse(is_path_subset("src/**", "src/auth/**"))

        # Exact file cannot contain children
        self.assertFalse(is_path_subset("src/auth.py/child", "src/auth.py"))

    def test_directory_glob_vs_direct_children_glob(self) -> None:
        """'dir/*' allows direct children only; 'dir/**' allows arbitrary nesting."""
        self.assertTrue(is_path_subset("src/a.py", "src/*"))
        self.assertTrue(is_path_subset("src/sub", "src/*"))

        # Nested descendant not allowed by direct children glob
        self.assertFalse(is_path_subset("src/sub/nested.py", "src/*"))
        self.assertFalse(is_path_subset("src/sub/*", "src/*"))
        self.assertFalse(is_path_subset("src/**", "src/*"))

    def test_empty_and_malformed_paths_fail_closed(self) -> None:
        """Empty, whitespace-only, and malformed characters fail closed."""
        for empty_p in ("", "   ", "\t", "\n"):
            with self.subTest(empty_p=repr(empty_p)):
                with self.assertRaises(DelegationValidationError):
                    normalize_target_path(empty_p)

        for bad_p in ("src/\0null", "src/auth<1>", "src/file|pipe", "src/file\"quote"):
            with self.subTest(bad_p=bad_p):
                with self.assertRaises(DelegationAttenuationError):
                    normalize_target_path(bad_p)

    def test_workspace_containment_with_repo_dir(self) -> None:
        """Target scope attenuation verifies physical containment when repo_dir is provided."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            (root / "src").mkdir()
            (root / "src" / "main.py").write_text("print('hello')")

            # Valid containment within workspace
            attenuated = attenuate_target_scope(
                parent_targets=("src/**",),
                requested_targets=("src/main.py",),
                repo_dir=root,
            )
            self.assertEqual(attenuated, ("src/main.py",))


class TestCategoryCNetworkEscalationDefenses(unittest.TestCase):
    """Category C: Network endpoint attenuation and privilege escalation defenses."""

    def test_wildcard_expansion_denied(self) -> None:
        """A child cannot expand a specific host into a wildcard or broader domain."""
        # Specific host -> wildcard: DENY
        self.assertFalse(is_network_subset("*", "api.example.com"))

        # Specific host -> wildcard subdomain: DENY
        self.assertFalse(is_network_subset("*.example.com", "api.example.com"))

        # Specific host -> parent domain: DENY
        self.assertFalse(is_network_subset("example.com", "api.example.com"))

        # Specific host -> unrelated host: DENY
        self.assertFalse(is_network_subset("evil.example.com", "api.example.com"))

    def test_port_widening_denied(self) -> None:
        """Child cannot widen port restrictions or substitute ports."""
        # Different explicit port: DENY
        self.assertFalse(is_network_subset("https://api.example.com:9443", "https://api.example.com:8443"))

        # Omitting port to gain standard port when parent was restricted to custom port: DENY
        self.assertFalse(is_network_subset("https://api.example.com", "https://api.example.com:8443"))

        # Matching explicit port: ALLOW
        self.assertTrue(is_network_subset("https://api.example.com:8443", "https://api.example.com:8443"))

    def test_protocol_downgrade_and_widening_denied(self) -> None:
        """HTTPS parent cannot be downgraded to HTTP or widened to arbitrary schemes."""
        # Scheme downgrade https -> http: DENY
        self.assertFalse(is_network_subset("http://api.example.com", "https://api.example.com"))
        self.assertFalse(is_network_subset("http://api.example.com:80", "https://api.example.com:443"))

        # Arbitrary protocol scheme: DENY
        self.assertFalse(is_network_subset("ftp://api.example.com", "https://api.example.com"))
        self.assertFalse(is_network_subset("ws://api.example.com", "https://api.example.com"))

    def test_userinfo_credentials_in_url_rejected(self) -> None:
        """Endpoints containing user:password@host must be strictly rejected."""
        with self.assertRaises(DelegationAttenuationError):
            normalize_endpoint("https://user:secret@api.example.com")
        with self.assertRaises(DelegationAttenuationError):
            is_network_subset("https://user:pass@api.example.com", "https://api.example.com")

    def test_configured_model_api_closed_scope(self) -> None:
        """The 'configured_model_api' special token cannot be expanded to external internet."""
        self.assertTrue(is_network_subset("configured_model_api", "configured_model_api"))
        self.assertFalse(is_network_subset("*", "configured_model_api"))
        self.assertFalse(is_network_subset("https://api.openai.com", "configured_model_api"))
        self.assertFalse(is_network_subset("configured_model_api", "https://api.example.com"))


class TestCategoryDOperationEscalationDefenses(unittest.TestCase):
    """Category D: Operation scope privilege hierarchy and unknown operation rejection."""

    def test_read_to_write_escalation_denied(self) -> None:
        """Child with read_file cannot escalate to write_file."""
        with self.assertRaises(DelegationAttenuationError):
            attenuate_operation_scope(
                parent_operations=("read_file",),
                requested_operations=("write_file",),
            )

    def test_read_to_execute_escalation_denied(self) -> None:
        """Child with read_file cannot escalate to shell execution."""
        with self.assertRaises(DelegationAttenuationError):
            attenuate_operation_scope(
                parent_operations=("read_file",),
                requested_operations=("run_shell_command",),
            )

    def test_unknown_operation_rejected(self) -> None:
        """Operations outside KNOWN_OPERATIONS must fail closed."""
        unknown_ops = ["teleport_data", "bypass_guard", "exec_arbitrary", "admin_override"]
        for bad_op in unknown_ops:
            with self.subTest(bad_op=bad_op):
                with self.assertRaises(DelegationAttenuationError):
                    attenuate_operation_scope(
                        parent_operations=("read_file", "write_file"),
                        requested_operations=(bad_op,),
                    )

    def test_empty_parent_operations_cannot_grant_anything(self) -> None:
        """If parent has no operations, child cannot request any operation."""
        with self.assertRaises(DelegationAttenuationError):
            attenuate_operation_scope(
                parent_operations=(),
                requested_operations=("read_file",),
            )

    def test_empty_requested_operations_allowed(self) -> None:
        """Child requesting empty operations is granted empty operations (0 authority)."""
        result = attenuate_operation_scope(
            parent_operations=("read_file", "write_file"),
            requested_operations=(),
        )
        self.assertEqual(result, ())


class TestCategoryEGitPolicyEscalationDefenses(unittest.TestCase):
    """Category E: Git policy monotonic attenuation and push escalation prevention."""

    def test_read_only_to_commit_denied(self) -> None:
        """Read-only Git parent cannot delegate commit authority."""
        parent_git = GitPolicy(read=True, commit=False, push=False)
        child_req = GitPolicy(read=True, commit=True, push=False)
        with self.assertRaises(DelegationAttenuationError):
            attenuate_git_policy(parent_git, child_req)

    def test_read_only_to_push_denied(self) -> None:
        """Read-only Git parent cannot delegate push authority."""
        parent_git = GitPolicy(read=True, commit=False, push=False)
        child_req = GitPolicy(read=True, commit=False, push=True)
        with self.assertRaises(DelegationAttenuationError):
            attenuate_git_policy(parent_git, child_req)

    def test_no_push_to_push_denied(self) -> None:
        """Parent with commit=True, push=False cannot delegate push=True."""
        parent_git = GitPolicy(read=True, commit=True, push=False)
        child_req = GitPolicy(read=True, commit=True, push=True)
        with self.assertRaises(DelegationAttenuationError):
            attenuate_git_policy(parent_git, child_req)

    def test_unknown_git_policy_fields_rejected(self) -> None:
        """Unknown fields in GitPolicy deserialization fail closed."""
        with self.assertRaises(ValueError):
            GitPolicy.from_dict({"read": True, "admin": True})
        with self.assertRaises(ValueError):
            GitPolicy.from_dict({"force_push": True})


class TestCategoryFIdentityBinding(unittest.TestCase):
    """Category F: Strict identity, session, and session-incarnation binding."""

    def setUp(self) -> None:
        self.now = time.time()
        self.parent = DelegationContract(
            delegation_id="delg_parent_id",
            parent_work_id="work_root",
            child_subagent_id="sub_p",
            actor="alice",
            session_id="sess_prod",
            session_incarnation_id="inc_prod_1",
            created_at=self.now,
            expires_at=self.now + 500.0,
        )

    def test_actor_mismatch_denied(self) -> None:
        """Delegation to a different actor is prohibited."""
        with self.assertRaises(DelegationAttenuationError):
            DelegationContract.derive(
                parent_authority=self.parent,
                child_subagent_id="sub_c",
                actor="mallory",
            )

    def test_session_mismatch_denied(self) -> None:
        """Cross-session delegation is prohibited."""
        with self.assertRaises(DelegationAttenuationError):
            DelegationContract.derive(
                parent_authority=self.parent,
                child_subagent_id="sub_c",
                session_id="sess_DIFFERENT",
            )

    def test_session_incarnation_mismatch_denied(self) -> None:
        """Stale or mismatched session incarnation is prohibited."""
        with self.assertRaises(DelegationAttenuationError):
            DelegationContract.derive(
                parent_authority=self.parent,
                child_subagent_id="sub_c",
                session_incarnation_id="inc_STALE",
            )

    def test_parent_work_id_mismatch_denied(self) -> None:
        """Mismatched parent work ID is prohibited."""
        with self.assertRaises(DelegationAttenuationError):
            DelegationContract.derive(
                parent_authority=self.parent,
                child_subagent_id="sub_c",
                parent_work_id="work_WRONG",
            )


class TestCategoryGTimeAndExpiration(unittest.TestCase):
    """Category G: Monotonic expiration, non-extension, and expired parent defenses."""

    def setUp(self) -> None:
        self.now = time.time()
        self.parent = DelegationContract(
            delegation_id="delg_parent_time",
            parent_work_id="work_root",
            child_subagent_id="sub_p",
            actor="alice",
            session_id="sess_1",
            session_incarnation_id="inc_1",
            created_at=self.now,
            expires_at=self.now + 300.0,
        )

    def test_child_expiry_less_than_parent_allowed(self) -> None:
        """Child expiration strictly earlier than parent is allowed."""
        child = DelegationContract.derive(
            parent_authority=self.parent,
            child_subagent_id="sub_c_time",
            expires_at=self.now + 150.0,
        )
        self.assertEqual(child.expires_at, self.now + 150.0)

    def test_child_expiry_exceeding_parent_denied(self) -> None:
        """Child expiration exceeding parent expiration must fail closed."""
        with self.assertRaises(DelegationAttenuationError):
            DelegationContract.derive(
                parent_authority=self.parent,
                child_subagent_id="sub_c_time",
                expires_at=self.now + 350.0,  # Parent expires at now + 300.0
            )

    def test_child_expiry_equal_to_created_at_denied(self) -> None:
        """Expiration timestamp must be strictly in the future."""
        with self.assertRaises(DelegationValidationError):
            DelegationContract(
                delegation_id="delg_zero_ttl",
                parent_work_id="work_root",
                child_subagent_id="sub_c",
                actor="alice",
                session_id="sess_1",
                session_incarnation_id="inc_1",
                created_at=self.now,
                expires_at=self.now,
            )

    def test_expired_parent_cannot_delegate(self) -> None:
        """An already-expired parent contract cannot delegate any authority."""
        expired_parent = DelegationContract(
            delegation_id="delg_expired_parent",
            parent_work_id="work_root",
            child_subagent_id="sub_p",
            actor="alice",
            session_id="sess_1",
            session_incarnation_id="inc_1",
            created_at=self.now - 500.0,
            expires_at=self.now - 100.0,
        )
        with self.assertRaises(DelegationExpiredError):
            DelegationContract.derive(
                parent_authority=expired_parent,
                child_subagent_id="sub_c_expired",
            )


class TestCategoryHIntegrityAndTampering(unittest.TestCase):
    """Category H: Cryptographic digest validation and tampering detection."""

    def setUp(self) -> None:
        self.now = time.time()
        self.parent = DelegationContract(
            delegation_id="delg_parent_h",
            parent_work_id="work_root",
            child_subagent_id="sub_p",
            actor="alice",
            session_id="sess_1",
            session_incarnation_id="inc_1",
            target_scope=("src/**",),
            operation_scope=("read_file", "write_file"),
            network_scope=("https://api.example.com",),
            git_policy=GitPolicy(read=True, commit=False, push=False),
            created_at=self.now,
            expires_at=self.now + 400.0,
        )

    def test_derived_child_has_independent_digest(self) -> None:
        """Derived child computes its own independent digest, never reusing parent digest."""
        child = DelegationContract.derive(
            parent_authority=self.parent,
            child_subagent_id="sub_c_h",
            requested_target_scope=("src/auth/**",),
        )
        self.assertNotEqual(child.digest, self.parent.digest)
        self.assertEqual(len(child.digest), 64)
        child.validate_integrity()

    def test_tampering_with_target_scope_detected(self) -> None:
        """Mutating target_scope after digest computation causes integrity verification failure."""
        child = DelegationContract.derive(
            parent_authority=self.parent,
            child_subagent_id="sub_c_tamper",
            requested_target_scope=("src/auth/**",),
        )
        # Attempt to bypass via object.__setattr__
        object.__setattr__(child, "target_scope", ("**",))
        with self.assertRaises(DelegationIntegrityError):
            child.validate_integrity()

    def test_tampering_with_capabilities_detected(self) -> None:
        """Mutating capabilities breaks digest integrity."""
        child = DelegationContract.derive(
            parent_authority=self.parent,
            child_subagent_id="sub_c_tamper_caps",
        )
        escalated_caps = Capabilities(shell=ShellPolicy(execute=True))
        object.__setattr__(child, "capabilities", escalated_caps)
        with self.assertRaises(DelegationIntegrityError):
            child.validate_integrity()

    def test_tampering_with_git_policy_detected(self) -> None:
        """Mutating git_policy breaks digest integrity."""
        child = DelegationContract.derive(
            parent_authority=self.parent,
            child_subagent_id="sub_c_tamper_git",
        )
        object.__setattr__(child, "git_policy", GitPolicy(read=True, commit=True, push=True))
        with self.assertRaises(DelegationIntegrityError):
            child.validate_integrity()


class TestCategoryIImmutability(unittest.TestCase):
    """Category I: Immutability of parent contracts and derived child contracts."""

    def test_parent_contract_unchanged_after_attenuation(self) -> None:
        """The parent contract must remain strictly identical before and after child derivation."""
        now = time.time()
        parent = DelegationContract(
            delegation_id="delg_parent_immut",
            parent_work_id="work_p1",
            child_subagent_id="sub_p1",
            actor="alice",
            session_id="sess_1",
            session_incarnation_id="inc_1",
            target_scope=("src/**", "tests/**"),
            operation_scope=("read_file", "write_file"),
            network_scope=("https://api.example.com",),
            git_policy=GitPolicy(read=True, commit=True, push=False),
            created_at=now,
            expires_at=now + 500.0,
        )
        parent_dict_before = parent.to_dict()
        parent_digest_before = parent.digest

        # Derive multiple child contracts with diverse restrictions
        _ = DelegationContract.derive(
            parent_authority=parent,
            child_subagent_id="sub_c1",
            requested_target_scope=("src/auth/**",),
            requested_operation_scope=("read_file",),
        )
        _ = DelegationContract.derive(
            parent_authority=parent,
            child_subagent_id="sub_c2",
            requested_git_policy=GitPolicy(read=True, commit=False, push=False),
        )

        # Assert parent remains completely unchanged
        self.assertEqual(parent.to_dict(), parent_dict_before)
        self.assertEqual(parent.digest, parent_digest_before)

    def test_parent_capabilities_object_unchanged_after_attenuate_capabilities(self) -> None:
        """Parent Capabilities instance must be completely unmutated."""
        parent_caps = Capabilities(
            filesystem=FilesystemPolicy(read=("src/a.py", "src/b.py"), write=("src/b.py",)),
            shell=ShellPolicy(execute=False),
            network=NetworkPolicy(access=True, scope="configured_model_api"),
            git=GitPolicy(read=True, commit=True, push=False),
        )
        caps_dict_before = parent_caps.to_dict()

        _ = attenuate_capabilities(
            parent_caps,
            Capabilities(
                filesystem=FilesystemPolicy(read=("src/a.py",), write=()),
                git=GitPolicy(read=True, commit=False, push=False),
            ),
        )

        self.assertEqual(parent_caps.to_dict(), caps_dict_before)

    def test_child_contract_is_frozen(self) -> None:
        """Derived DelegationContract is a frozen dataclass rejecting direct attribute assignment."""
        now = time.time()
        child = DelegationContract(
            delegation_id="delg_frozen",
            parent_work_id="work_p1",
            child_subagent_id="sub_c1",
            actor="alice",
            session_id="sess_1",
            session_incarnation_id="inc_1",
            created_at=now,
            expires_at=now + 100.0,
        )
        with self.assertRaises((FrozenInstanceError, AttributeError, TypeError)):
            child.actor = "mallory"  # type: ignore
        with self.assertRaises((FrozenInstanceError, AttributeError, TypeError)):
            child.target_scope = ("**",)  # type: ignore


class TestCategoryJCompositionAndTransitivity(unittest.TestCase):
    """Category J: Multi-tier delegation chains (Parent -> Child -> Grandchild)."""

    def test_three_tier_transitive_attenuation(self) -> None:
        """Grandchild ⊆ Child ⊆ Parent holds transitively across all authority dimensions."""
        now = time.time()
        # Tier 1: Parent
        p1 = DelegationContract(
            delegation_id="delg_t1_parent",
            parent_work_id="work_chain",
            child_subagent_id="sub_t1",
            actor="alice",
            session_id="sess_chain",
            session_incarnation_id="inc_chain_1",
            target_scope=("src/**", "tests/**"),
            operation_scope=("read_file", "write_file"),
            network_scope=("https://api.example.com", "https://auth.service.local"),
            git_policy=GitPolicy(read=True, commit=True, push=False),
            created_at=now,
            expires_at=now + 1000.0,
        )

        # Tier 2: Child (attenuated to src/auth/**, read_file only)
        c2 = DelegationContract.derive(
            parent_authority=p1,
            child_subagent_id="sub_t2_child",
            requested_target_scope=("src/auth/**",),
            requested_operation_scope=("read_file",),
            requested_network_scope=("https://api.example.com",),
            requested_git_policy=GitPolicy(read=True, commit=False, push=False),
            ttl_seconds=500.0,
        )
        self.assertEqual(c2.target_scope, ("src/auth/**",))
        self.assertEqual(c2.operation_scope, ("read_file",))
        self.assertEqual(c2.parent_delegation_id, p1.delegation_id)

        # Tier 3: Grandchild (further attenuated to src/auth/jwt/**)
        g3 = DelegationContract.derive(
            parent_authority=c2,
            child_subagent_id="sub_t3_grandchild",
            requested_target_scope=("src/auth/jwt/**",),
            requested_operation_scope=("read_file",),
            requested_network_scope=("https://api.example.com",),
            requested_git_policy=GitPolicy(read=True, commit=False, push=False),
            ttl_seconds=200.0,
        )
        self.assertEqual(g3.target_scope, ("src/auth/jwt/**",))
        self.assertEqual(g3.parent_delegation_id, c2.delegation_id)

        # Invariant checks: g3 ⊆ c2 ⊆ p1
        for gt in g3.target_scope:
            self.assertTrue(any(is_path_subset(gt, ct) for ct in c2.target_scope))
            self.assertTrue(any(is_path_subset(gt, pt) for pt in p1.target_scope))

    def test_grandchild_cannot_escalate_to_tier1_authority(self) -> None:
        """Grandchild cannot request authority held by Tier 1 but denied to Tier 2."""
        now = time.time()
        p1 = DelegationContract(
            delegation_id="delg_t1_p",
            parent_work_id="work_chain",
            child_subagent_id="sub_t1",
            actor="alice",
            session_id="sess_chain",
            session_incarnation_id="inc_chain_1",
            target_scope=("src/**",),
            operation_scope=("read_file", "write_file"),
            created_at=now,
            expires_at=now + 1000.0,
        )
        c2 = DelegationContract.derive(
            parent_authority=p1,
            child_subagent_id="sub_t2_c",
            requested_target_scope=("src/auth/**",),
            requested_operation_scope=("read_file",),
            ttl_seconds=500.0,
        )

        # Grandchild attempts to escalate target_scope back to 'src/**' -> DENIED
        with self.assertRaises(DelegationAttenuationError):
            DelegationContract.derive(
                parent_authority=c2,
                child_subagent_id="sub_t3_g",
                requested_target_scope=("src/**",),
            )

        # Grandchild attempts to escalate operation_scope back to 'write_file' -> DENIED
        with self.assertRaises(DelegationAttenuationError):
            DelegationContract.derive(
                parent_authority=c2,
                child_subagent_id="sub_t3_g",
                requested_operation_scope=("write_file",),
            )


class TestCategoryKUnknownAndMalformedInputs(unittest.TestCase):
    """Category K: Malformed objects, unknown capabilities, and ambiguous scopes fail closed."""

    def test_unknown_capability_type_fails_closed(self) -> None:
        """Non-Capabilities parent or child types fail closed with ValidationError."""
        now = time.time()
        with self.assertRaises(DelegationValidationError):
            attenuate_capabilities("not_a_capability_instance", Capabilities())  # type: ignore
        with self.assertRaises(DelegationValidationError):
            attenuate_capabilities(Capabilities(), "not_a_capability_instance")  # type: ignore

    def test_unsupported_parent_authority_fails_closed(self) -> None:
        """derive rejects unsupported parent authority types."""
        with self.assertRaises(DelegationValidationError):
            DelegationContract.derive(
                parent_authority="unsupported_string",
                child_subagent_id="sub_child",
            )

    def test_missing_required_fields_in_deserialization(self) -> None:
        """from_dict fails closed if any required authority field is omitted."""
        data = {
            "schema_version": 1,
            "delegation_id": "delg_incomplete",
            # missing parent_work_id, child_subagent_id, actor, session_id, etc.
        }
        with self.assertRaises(DelegationValidationError):
            DelegationContract.from_dict(data)


class TestCategoryLAuthorityInvarianceMatrix(unittest.TestCase):
    """Category L: Deterministic property-style authority invariance matrix."""

    def test_matrix_property_child_authority_never_exceeds_parent(self) -> None:
        """For every successfully derived contract, CHILD ⊆ PARENT holds across all dimensions."""
        now = time.time()
        base_parent = DelegationContract(
            delegation_id="delg_matrix_parent",
            parent_work_id="work_matrix",
            child_subagent_id="sub_matrix_p",
            actor="alice",
            session_id="sess_matrix",
            session_incarnation_id="inc_matrix_1",
            target_scope=("src/**", "tests/**"),
            operation_scope=("read_file", "write_file"),
            network_scope=("https://api.example.com", "*.service.local"),
            git_policy=GitPolicy(read=True, commit=True, push=False),
            created_at=now,
            expires_at=now + 600.0,
        )

        test_cases: List[Tuple[Sequence[str], Sequence[str], Sequence[str], GitPolicy, bool]] = [
            # (targets, operations, networks, git_policy, should_succeed)
            # Case 1: Identical -> PASS
            (
                ("src/**", "tests/**"),
                ("read_file", "write_file"),
                ("https://api.example.com", "*.service.local"),
                GitPolicy(read=True, commit=True, push=False),
                True,
            ),
            # Case 2: Strictly narrower -> PASS
            (
                ("src/auth/**",),
                ("read_file",),
                ("https://api.example.com",),
                GitPolicy(read=True, commit=False, push=False),
                True,
            ),
            # Case 3: Target expansion (escapes workspace root) -> REJECT
            (
                ("../escape/**",),
                ("read_file",),
                ("https://api.example.com",),
                GitPolicy(read=True, commit=False, push=False),
                False,
            ),
            # Case 4: Target sibling expansion -> REJECT
            (
                ("docs/**",),
                ("read_file",),
                ("https://api.example.com",),
                GitPolicy(read=True, commit=False, push=False),
                False,
            ),
            # Case 5: Operation escalation (unknown operation) -> REJECT
            (
                ("src/**",),
                ("unknown_privilege",),
                ("https://api.example.com",),
                GitPolicy(read=True, commit=False, push=False),
                False,
            ),
            # Case 6: Operation escalation (shell execution without grant) -> REJECT
            (
                ("src/**",),
                ("run_shell_command",),
                ("https://api.example.com",),
                GitPolicy(read=True, commit=False, push=False),
                False,
            ),
            # Case 7: Network escalation (wildcard expansion) -> REJECT
            (
                ("src/**",),
                ("read_file",),
                ("*",),
                GitPolicy(read=True, commit=False, push=False),
                False,
            ),
            # Case 8: Network escalation (unauthorized host) -> REJECT
            (
                ("src/**",),
                ("read_file",),
                ("https://unpermitted.com",),
                GitPolicy(read=True, commit=False, push=False),
                False,
            ),
            # Case 9: Git policy escalation (push=True when parent push=False) -> REJECT
            (
                ("src/**",),
                ("read_file",),
                ("https://api.example.com",),
                GitPolicy(read=True, commit=True, push=True),
                False,
            ),
        ]

        for targets, ops, nets, git, should_succeed in test_cases:
            if should_succeed:
                child = DelegationContract.derive(
                    parent_authority=base_parent,
                    child_subagent_id="sub_matrix_c",
                    requested_target_scope=targets,
                    requested_operation_scope=ops,
                    requested_network_scope=nets,
                    requested_git_policy=git,
                    ttl_seconds=300.0,
                )
                self.assertIsNotNone(child)
                # Verify mathematical invariant: child ⊆ parent
                for ct in child.target_scope:
                    self.assertTrue(
                        any(is_path_subset(ct, pt) for pt in base_parent.target_scope),
                        f"Target {ct} exceeds parent scope",
                    )
                for co in child.operation_scope:
                    self.assertIn(co, base_parent.operation_scope, f"Operation {co} exceeds parent scope")
                for cn in child.network_scope:
                    self.assertTrue(
                        any(is_network_subset(cn, pn) for pn in base_parent.network_scope),
                        f"Network {cn} exceeds parent scope",
                    )
                self.assertTrue(not child.git_policy.push or base_parent.git_policy.push)
                self.assertTrue(not child.git_policy.commit or base_parent.git_policy.commit)
                self.assertTrue(not child.git_policy.read or base_parent.git_policy.read)
                self.assertLessEqual(child.expires_at, base_parent.expires_at)
            else:
                with self.assertRaises(DelegationAttenuationError):
                    DelegationContract.derive(
                        parent_authority=base_parent,
                        child_subagent_id="sub_matrix_c",
                        requested_target_scope=targets,
                        requested_operation_scope=ops,
                        requested_network_scope=nets,
                        requested_git_policy=git,
                        ttl_seconds=300.0,
                    )


class TestCategoryMArchitecturalBoundaries(unittest.TestCase):
    """Category M: Prove zero execution authority and sole orchestrator invariant."""

    def test_delegation_module_has_no_execution_primitives(self) -> None:
        """delegation.py must NOT import or execute subprocess/network/git primitives."""
        import ast
        source_code = inspect.getsource(delegation_mod)
        tree = ast.parse(source_code)
        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_modules.add(node.module)

        forbidden_modules = {
            "subprocess",
            "requests",
            "httpx",
            "urllib",
            "urllib.request",
        }
        for mod in forbidden_modules:
            self.assertNotIn(mod, imported_modules, f"Forbidden execution module '{mod}' imported in delegation.py")

        banned_calls = ["os.system", "Popen", "git push", "git commit", "ApprovalStore", "TransactionCoordinator", "Orchestrator"]
        lines = [line.strip() for line in source_code.splitlines() if not line.strip().startswith(("#", '"""', "'''", "*", "-"))]
        clean_code = "\n".join(lines)
        for token in banned_calls:
            self.assertNotIn(
                token,
                clean_code,
                f"Forbidden execution or architectural bypass token '{token}' found in delegation.py",
            )

    def test_contract_classes_have_no_execution_methods(self) -> None:
        """DelegationContract and Capabilities must NOT expose callable execution methods."""
        forbidden_methods = [
            "execute",
            "run",
            "spawn",
            "apply",
            "approve",
            "resume",
        ]
        for cls in (DelegationContract, Capabilities, FilesystemPolicy, GitPolicy, NetworkPolicy, ShellPolicy):
            for method in forbidden_methods:
                attr = getattr(cls, method, None)
                self.assertFalse(
                    callable(attr),
                    f"Forbidden callable execution method '{method}' found on class {cls.__name__}",
                )
        # Specifically verify git push and git commit are not callable methods on GitPolicy or DelegationContract
        for cls in (DelegationContract, Capabilities, GitPolicy):
            for git_action in ("push", "commit"):
                attr = getattr(cls, git_action, None)
                self.assertFalse(
                    callable(attr),
                    f"Forbidden callable git execution method '{git_action}' found on class {cls.__name__}",
                )



class TestP13CSecurityRemediation(unittest.TestCase):
    """Regression and property tests for P1.3C Independent Security Audit remediations.

    Findings addressed:
    - BF-P13C-01: Filesystem glob transitive escalation
    - BF-P13C-02: Extensionless file directory confusion
    - BF-P13C-03: Git capability representation desynchronization
    - BF-P13C-04: Network bare-host scheme/port widening
    """

    def setUp(self) -> None:
        self.now = time.time()
        self.parent = DelegationContract(
            delegation_id="delg_parent_rem",
            parent_work_id="work_p100",
            child_subagent_id="sub_c100",
            actor="alice",
            session_id="sess_100",
            session_incarnation_id="inc_100_1",
            capabilities=Capabilities(
                filesystem=FilesystemPolicy(read=("src/auth.py", "src/models.py"), write=("src/models.py",)),
                shell=ShellPolicy(execute=False),
                network=NetworkPolicy(access=True, scope="configured_model_api"),
                git=GitPolicy(read=True, commit=True, push=True),
            ),
            target_scope=("src/**", "tests/**", "Makefile"),
            operation_scope=("read_file", "write_file"),
            network_scope=("https://api.example.com", "api.example.com"),
            git_policy=GitPolicy(read=True, commit=True, push=True),
            created_at=self.now,
            expires_at=self.now + 600.0,
            role="planner",
            purpose="Remediation test harness",
        )

    # --- BF-P13C-01: Filesystem glob transitive escalation ---

    def test_bf_p13c_01_glob_transitive_escalation_prevented(self) -> None:
        """Verify that direct-child glob does NOT permit directory scope or recursive globs."""
        # Direct audit reproducers
        self.assertFalse(is_path_subset("src/", "src/*"))
        self.assertFalse(is_path_subset("src/**", "src/*"))
        self.assertFalse(is_path_subset("src/nested/deep.py", "src/*"))

        # Verify that transitive chain src/* -> src/ -> src/** is broken
        step1 = is_path_subset("src/", "src/*")
        self.assertFalse(step1, "src/* must not authorize src/")

        # Direct child glob authorizes exactly one direct child
        self.assertTrue(is_path_subset("src/a.py", "src/*"))
        self.assertTrue(is_path_subset("src/config.json", "src/*"))
        self.assertTrue(is_path_subset("src/auth", "src/*"))

        # But it must NOT authorize recursive descendants or directories
        self.assertFalse(is_path_subset("src/auth/login.py", "src/*"))
        self.assertFalse(is_path_subset("src/auth/**", "src/*"))
        self.assertFalse(is_path_subset("src/auth/*", "src/*"))

    # --- BF-P13C-02: Extensionless file directory confusion ---

    def test_bf_p13c_02_extensionless_file_directory_confusion_fixed(self) -> None:
        """Extensionless files must be treated as exact files, never as directories."""
        extensionless_files = [
            "Makefile",
            "Dockerfile",
            "README",
            "LICENSE",
            "Procfile",
            "bin/tool",
        ]
        for f in extensionless_files:
            with self.subTest(file=f):
                # Authorizes itself
                self.assertTrue(is_path_subset(f, f))
                # Must NOT authorize children or recursive globs
                self.assertFalse(is_path_subset(f"{f}/x", f))
                self.assertFalse(is_path_subset(f"{f}/*", f))
                self.assertFalse(is_path_subset(f"{f}/**", f))
                self.assertFalse(is_path_subset(f"{f}/evil.py", f))

        # Dotfile .env is strictly rejected by path normalization (Section 7)
        with self.assertRaises(DelegationAttenuationError):
            normalize_target_path(".env")
        with self.assertRaises(DelegationAttenuationError):
            is_path_subset(".env", ".env")

    # --- BF-P13C-03: Git capability representation desynchronization ---

    def test_bf_p13c_03_git_representation_synchronized_in_derive(self) -> None:
        """When requested_git_policy is attenuated, capabilities.git must be kept in sync."""
        child = DelegationContract.derive(
            parent_authority=self.parent,
            child_subagent_id="sub_child_git_sync",
            requested_git_policy=GitPolicy(
                read=True,
                commit=False,
                push=False,
            ),
        )
        self.assertFalse(child.git_policy.push)
        self.assertFalse(child.capabilities.git.push)
        self.assertFalse(child.git_policy.commit)
        self.assertFalse(child.capabilities.git.commit)
        self.assertTrue(child.git_policy.read)
        self.assertTrue(child.capabilities.git.read)

    def test_bf_p13c_03_git_sync_when_requested_capabilities_only(self) -> None:
        """When requested_capabilities is supplied, git_policy is synchronized."""
        child = DelegationContract.derive(
            parent_authority=self.parent,
            child_subagent_id="sub_child_caps_git_sync",
            requested_capabilities=Capabilities(
                git=GitPolicy(read=True, commit=False, push=False),
            ),
        )
        self.assertFalse(child.git_policy.push)
        self.assertFalse(child.capabilities.git.push)
        self.assertFalse(child.git_policy.commit)
        self.assertFalse(child.capabilities.git.commit)

    def test_bf_p13c_03_all_git_permission_combinations(self) -> None:
        """Audit all combinations of read, commit, push attenuation."""
        combinations = [
            (True, True, True),
            (True, True, False),
            (True, False, False),
            (False, False, False),
        ]
        for r, c, p in combinations:
            with self.subTest(read=r, commit=c, push=p):
                child = DelegationContract.derive(
                    parent_authority=self.parent,
                    child_subagent_id=f"sub_child_git_{r}_{c}_{p}",
                    requested_git_policy=GitPolicy(read=r, commit=c, push=p),
                )
                self.assertEqual(child.git_policy.read, r)
                self.assertEqual(child.capabilities.git.read, r)
                self.assertEqual(child.git_policy.commit, c)
                self.assertEqual(child.capabilities.git.commit, c)
                self.assertEqual(child.git_policy.push, p)
                self.assertEqual(child.capabilities.git.push, p)
                # Verify capabilities.git <= git_policy
                self.assertTrue(child.capabilities.git.read <= child.git_policy.read)
                self.assertTrue(child.capabilities.git.commit <= child.git_policy.commit)
                self.assertTrue(child.capabilities.git.push <= child.git_policy.push)

    # --- BF-P13C-04: Network bare-host scheme/port widening ---

    def test_bf_p13c_04_network_bare_host_scheme_and_port_widening(self) -> None:
        """Bare-host parent scopes must canonicalize to https/443 and reject scheme/port widening."""
        # Prohibit protocol widening
        self.assertFalse(is_network_subset("ftp://api.example.com:9999", "api.example.com"))
        self.assertFalse(is_network_subset("ftp://api.example.com", "api.example.com"))
        self.assertFalse(is_network_subset("http://api.example.com", "api.example.com"))

        # Prohibit port widening
        self.assertFalse(is_network_subset("https://api.example.com:8443", "api.example.com"))
        self.assertFalse(is_network_subset("https://api.example.com:9999", "api.example.com"))

        # Canonical equivalence between bare host and explicit https:443
        self.assertTrue(is_network_subset("https://api.example.com:443", "api.example.com"))
        self.assertTrue(is_network_subset("https://api.example.com", "api.example.com"))
        self.assertTrue(is_network_subset("api.example.com", "https://api.example.com:443"))
        self.assertTrue(is_network_subset("api.example.com", "https://api.example.com"))

        # Host confusion attacks must be rejected
        self.assertFalse(is_network_subset("api.example.com.evil.com", "api.example.com"))
        self.assertFalse(is_network_subset("evilapi.example.com", "api.example.com"))

        # Wildcards cannot widen
        self.assertFalse(is_network_subset("*.example.com", "api.example.com"))
        self.assertFalse(is_network_subset("*", "api.example.com"))

    # --- BF-P13C-05: Network URI Fragment/Query Delimiter Confusion ---

    def test_bf_p13c_05_network_uri_delimiters_and_parser_confusion(self) -> None:
        """URI fragment (#), query (?), and backslash (\\) delimiters cannot allow unauthorized host connection."""
        # Exact audit reproducers:
        self.assertFalse(is_network_subset("https://evil.com#api.example.com", "*.example.com"))
        self.assertFalse(is_network_subset("https://evil.com?api.example.com", "*.example.com"))
        self.assertFalse(is_network_subset("https://evil.com#api.example.com", "api.example.com"))
        self.assertFalse(is_network_subset("https://evil.com?api.example.com", "api.example.com"))

        # Malformed backslash separator
        with self.assertRaises(DelegationAttenuationError):
            is_network_subset("https://evil.com\\api.example.com", "*.example.com")

        # Unsupported URI components (userinfo, credentials)
        with self.assertRaises(DelegationAttenuationError):
            is_network_subset("https://user@evil.com", "*.example.com")

        # Path component stripping vs host containment
        self.assertFalse(is_network_subset("https://evil.com/path", "*.example.com"))
        self.assertTrue(is_network_subset("https://api.example.com/path", "*.example.com"))
        self.assertTrue(is_network_subset("https://api.example.com/path", "https://api.example.com"))

    # --- BF-P13C-06: Filesystem Wildcard Directory Transitivity ---

    def test_bf_p13c_06_filesystem_wildcard_directory_transitivity(self) -> None:
        """Wildcard directory containment must be strictly transitive (A <= B and B <= C => A <= C)."""
        # Exact audit reproducer
        self.assertTrue(is_path_subset("*/*", "*/"))
        self.assertTrue(is_path_subset("*/tools", "*/*"))
        self.assertTrue(is_path_subset("*/tools", "*/"))

        # Additional wildcard directory leaf & glob cases
        self.assertTrue(is_path_subset("*/auth", "*/"))
        self.assertTrue(is_path_subset("*/auth/*", "*/"))
        self.assertTrue(is_path_subset("*/auth/login.py", "*/"))
        self.assertFalse(is_path_subset("*/**", "*/"))

        # Multi-tier delegation chain: parent(*/) -> child(*/*) -> grandchild(*/tools)
        p = DelegationContract(
            delegation_id="delg_p_wc",
            parent_work_id="w",
            child_subagent_id="c0",
            actor="alice",
            session_id="s",
            session_incarnation_id="i",
            capabilities=Capabilities(),
            target_scope=("*/",),
            operation_scope=("read_file",),
            network_scope=(),
            git_policy=GitPolicy(),
            created_at=self.now,
            expires_at=self.now + 600.0,
        )
        c1 = DelegationContract.derive(
            parent_authority=p,
            child_subagent_id="c1",
            requested_target_scope=("*/*",),
            ttl_seconds=500.0,
        )
        c2 = DelegationContract.derive(
            parent_authority=c1,
            child_subagent_id="c2",
            requested_target_scope=("*/tools",),
            ttl_seconds=400.0,
        )
        # Verify grandchild is strictly contained within both child and root parent
        self.assertTrue(any(is_path_subset(c2.target_scope[0], t) for t in c1.target_scope))
        self.assertTrue(any(is_path_subset(c2.target_scope[0], t) for t in p.target_scope))

    # --- BF-P13C-07: Wildcard Domain Apex Rejection ---

    def test_bf_p13c_07_wildcard_domain_does_not_authorize_apex(self) -> None:
        """*.example.com authorizes only subdomains, never the apex domain example.com."""
        # Exact audit reproducer
        self.assertFalse(is_network_subset("example.com", "*.example.com"))
        self.assertFalse(is_network_subset("https://example.com", "*.example.com"))
        self.assertFalse(is_network_subset("https://example.com:443", "*.example.com"))

        # Subdomains must be authorized
        self.assertTrue(is_network_subset("api.example.com", "*.example.com"))
        self.assertTrue(is_network_subset("https://api.example.com", "*.example.com"))
        self.assertTrue(is_network_subset("https://api.example.com:443", "*.example.com"))
        self.assertTrue(is_network_subset("a.b.example.com", "*.example.com"))

        # Unrelated domains must be rejected
        self.assertFalse(is_network_subset("evil.com", "*.example.com"))
        self.assertFalse(is_network_subset("evil-example.com", "*.example.com"))
        self.assertFalse(is_network_subset("example.com.evil.com", "*.example.com"))

    # --- Property A: Filesystem containment transitivity ---

    def test_property_a_filesystem_transitivity(self) -> None:
        """For all scopes A, B, C: if A <= B and B <= C, then A <= C."""
        scopes = [
            "**",
            "*",
            "*/",
            "*/*",
            "*/**",
            "*/tools",
            "*/auth",
            "*/auth/*",
            "*/auth/**",
            "src/*",
            "src/**",
            "src/",
            "src/a.py",
            "src/auth/*",
            "src/auth/**",
            "src/auth/login.py",
            "src/auth/login.test.py",
            "Makefile",
            "Makefile/",
            "Makefile/*",
            "Makefile/**",
            "Dockerfile",
            "README",
            "src/bin/tool",
            "src/auth",
            "src/auth/",
        ]
        violations: list[str] = []
        for a in scopes:
            for b in scopes:
                if is_path_subset(a, b):
                    for c in scopes:
                        if is_path_subset(b, c):
                            if not is_path_subset(a, c):
                                violations.append(f"Transitivity violated: {a} <= {b} and {b} <= {c}, but not {a} <= {c}")
        self.assertEqual(violations, [], f"Filesystem transitivity violations found: {violations}")

    # --- Property B: Network containment transitivity ---

    def test_property_b_network_transitivity(self) -> None:
        """For all endpoints A, B, C: if A <= B and B <= C, then A <= C."""
        endpoints = [
            "*",
            "configured_model_api",
            "example.com",
            "api.example.com",
            "a.b.example.com",
            "*.example.com",
            "*.api.example.com",
            "https://example.com",
            "https://api.example.com",
            "https://api.example.com:443",
            "https://api.example.com:8443",
            "http://api.example.com",
            "http://api.example.com:80",
            "ftp://api.example.com",
            "ftp://api.example.com:21",
            "api.example.com.evil.com",
            "evil.com",
            "evil-example.com",
            "https://other.com",
            "https://evil.com#api.example.com",
            "https://evil.com?api.example.com",
        ]
        violations: list[str] = []
        for a in endpoints:
            for b in endpoints:
                if is_network_subset(a, b):
                    for c in endpoints:
                        if is_network_subset(b, c):
                            if not is_network_subset(a, c):
                                violations.append(f"Network transitivity violated: {a} <= {b} and {b} <= {c}, but not {a} <= {c}")
        self.assertEqual(violations, [], f"Network transitivity violations found: {violations}")

    # --- Property C: Delegation monotonicity ---

    def test_property_c_delegation_monotonicity(self) -> None:
        """For every derived child: child <= parent across all authority dimensions."""
        child = DelegationContract.derive(
            parent_authority=self.parent,
            child_subagent_id="sub_child_prop_c",
            requested_target_scope=("src/a.py",),
            requested_operation_scope=("read_file",),
            requested_network_scope=("https://api.example.com:443",),
            requested_git_policy=GitPolicy(read=True, commit=False, push=False),
            ttl_seconds=300.0,
        )
        # Target scope: every child target is covered by a parent target
        for ct in child.target_scope:
            self.assertTrue(any(is_path_subset(ct, pt) for pt in self.parent.target_scope))

        # Operation scope: subset
        self.assertTrue(set(child.operation_scope).issubset(set(self.parent.operation_scope)))

        # Network scope: every child endpoint is covered by a parent endpoint
        for ce in child.network_scope:
            self.assertTrue(any(is_network_subset(ce, pe) for pe in self.parent.network_scope))

        # Git policy: monotonic attenuation
        self.assertTrue(child.git_policy.read <= self.parent.git_policy.read)
        self.assertTrue(child.git_policy.commit <= self.parent.git_policy.commit)
        self.assertTrue(child.git_policy.push <= self.parent.git_policy.push)
        self.assertTrue(child.capabilities.git.read <= self.parent.capabilities.git.read)
        self.assertTrue(child.capabilities.git.commit <= self.parent.capabilities.git.commit)
        self.assertTrue(child.capabilities.git.push <= self.parent.capabilities.git.push)

        # Expiry: child expires before or at parent expiry
        self.assertLessEqual(child.expires_at, self.parent.expires_at)

    # --- Property D: Multi-tier monotonicity ---

    def test_property_d_multi_tier_monotonicity(self) -> None:
        """parent -> child -> grandchild: grandchild <= parent across every dimension."""
        child = DelegationContract.derive(
            parent_authority=self.parent,
            child_subagent_id="sub_child_tier_1",
            requested_target_scope=("src/auth/**",),
            requested_operation_scope=("read_file", "write_file"),
            requested_network_scope=("https://api.example.com",),
            requested_git_policy=GitPolicy(read=True, commit=True, push=False),
            ttl_seconds=400.0,
        )
        grandchild = DelegationContract.derive(
            parent_authority=child,
            child_subagent_id="sub_child_tier_2",
            requested_target_scope=("src/auth/login.py",),
            requested_operation_scope=("read_file",),
            requested_network_scope=("https://api.example.com:443",),
            requested_git_policy=GitPolicy(read=True, commit=False, push=False),
            ttl_seconds=200.0,
        )

        # Direct containment: grandchild <= child
        for gt in grandchild.target_scope:
            self.assertTrue(any(is_path_subset(gt, ct) for ct in child.target_scope))
        # Transitive containment: grandchild <= parent
        for gt in grandchild.target_scope:
            self.assertTrue(any(is_path_subset(gt, pt) for pt in self.parent.target_scope))

        self.assertTrue(set(grandchild.operation_scope).issubset(set(child.operation_scope)))
        self.assertTrue(set(grandchild.operation_scope).issubset(set(self.parent.operation_scope)))

        for ge in grandchild.network_scope:
            self.assertTrue(any(is_network_subset(ge, ce) for ce in child.network_scope))
            self.assertTrue(any(is_network_subset(ge, pe) for pe in self.parent.network_scope))

        self.assertTrue(grandchild.git_policy.push <= child.git_policy.push <= self.parent.git_policy.push)
        self.assertTrue(grandchild.git_policy.commit <= child.git_policy.commit <= self.parent.git_policy.commit)
        self.assertTrue(grandchild.git_policy.read <= child.git_policy.read <= self.parent.git_policy.read)

        self.assertLessEqual(grandchild.expires_at, child.expires_at)
        self.assertLessEqual(grandchild.expires_at, self.parent.expires_at)

    # --- Section 19 Adversarial Cases Matrix ---

    def test_section_19_adversarial_matrix(self) -> None:
        """Comprehensive verification of adversarial cases identified in Section 19."""
        # Filesystem cases
        self.assertFalse(is_path_subset("src/", "src/*"))
        self.assertFalse(is_path_subset("src/**", "src/*"))
        self.assertTrue(is_path_subset("src/*", "src/**"))
        self.assertTrue(is_path_subset("src/", "src/**"))
        self.assertTrue(is_path_subset("src/auth/*", "src/**"))
        self.assertTrue(is_path_subset("src/auth/**", "src/**"))
        self.assertTrue(is_path_subset("src/auth/login.py", "src/**"))

        # Explicit directory scope vs exact path
        self.assertTrue(is_path_subset("src/auth/login.py", "src/auth/"))
        self.assertFalse(is_path_subset("src/auth/login.py", "src/auth"))

        # Extensionless files
        for f in ("Makefile", "Dockerfile", "README", "LICENSE", "bin/tool"):
            self.assertTrue(is_path_subset(f, f))
            self.assertFalse(is_path_subset(f"{f}/child", f))
            self.assertFalse(is_path_subset(f"{f}/*", f))
            self.assertFalse(is_path_subset(f"{f}/**", f))
        with self.assertRaises(DelegationAttenuationError):
            is_path_subset(".env", ".env")

        # Network cases
        self.assertTrue(is_network_subset("https://api.example.com", "api.example.com"))
        self.assertTrue(is_network_subset("https://api.example.com:443", "api.example.com"))
        self.assertFalse(is_network_subset("https://api.example.com:8443", "api.example.com"))
        self.assertFalse(is_network_subset("http://api.example.com", "api.example.com"))
        self.assertFalse(is_network_subset("ftp://api.example.com", "api.example.com"))
        self.assertFalse(is_network_subset("*.example.com", "api.example.com"))
        self.assertFalse(is_network_subset("api.example.com.evil.com", "api.example.com"))


if __name__ == "__main__":
    unittest.main()
