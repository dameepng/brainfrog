"""Comprehensive Security Test Suite for M-02 Heuristic Target Extraction Remediation.

Validates:
1. False-positive rejection: versions, numbers, URLs, issues, packages, identifiers.
2. Valid target acceptance: files with extensions, multi-segment paths, known extensionless files.
3. Mixed requests: extracting only legitimate filesystem paths from complex sentences.
4. Ambiguous request tests: fail-closed without guessing.
5. Path attack tests: traversal, absolute, UNC, drive letter, and mixed separator attacks.
6. Property-style tests: technical tokens never authorize filesystem scope.
7. True E2E pipeline: IncomingMessage -> extraction -> approval -> /exec -> Contract -> Orchestrator -> disk.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.runtime.approval import (
    ApprovalRequest,
    ApprovalService,
    ApprovalStatus,
    CanonicalOperation,
    extract_canonical_operation,
)
from core.runtime.contract import ApprovedExecutionContract, normalize_target_rel_path
from core.runtime.messages import IncomingMessage
from core.runtime.permissions import PermissionAction, classify_request_action
from core.runtime.runtime import BrainFrogRuntime
from core.runtime.targets import (
    TargetCandidate,
    TargetClassification,
    classify_target_candidate,
    extract_deterministic_targets,
)
from orchestrator import PlanStep, _write_files
from system1.base import Answer, SystemOneClient


# =============================================================================
# Deterministic Test Doubles for E2E
# =============================================================================

class MockSystem1(SystemOneClient):
    name: str = "mock_s1"

    def decide(self, state: Dict[str, Any], questions: Dict[str, Any]) -> Dict[str, Answer]:
        return {
            k: Answer(choice="unrelated", score=0.1, noul=1.0, confidence=0.9)
            for k in questions
        }


class MockSystem2:
    def __init__(self, output_files: Optional[Dict[str, str]] = None) -> None:
        self.guidelines = ""
        self.provider_name = "mock_s2"
        self.model = "mock-model"
        self.output_files = output_files or {"src/safe.py": "# clean code\n"}

    def plan_task(self, task: str, focus_tree: str = "", **kwargs: Any) -> List[PlanStep]:
        return [PlanStep(id="1", description="Execute approved step", files=list(self.output_files.keys()))]

    def write_code(self, step: PlanStep, task: str, file_contents: Dict[str, str], **kwargs: Any) -> Dict[str, str]:
        return dict(self.output_files)

    def draft_pr(self, task: str, files_changed: List[str], test_summary: str = "", **kwargs: Any) -> Dict[str, str]:
        return {"title": f"feat: {task}", "body": "Approved execution."}


# =============================================================================
# 1. False-Positive Rejection Tests (Section 18)
# =============================================================================

class TestFalsePositiveRejection(unittest.TestCase):
    """Verifies that non-filesystem tokens are deterministically rejected."""

    def test_version_numbers_rejected(self) -> None:
        """Version numbers and SemVer tokens must never become filesystem targets."""
        versions = [
            "2.1",
            "v2.1.0",
            "3.12",
            "1.0",
            "1.2.3",
            "v1",
            "v2",
            "v2.1",
            "1.0.0-beta",
            "3.12.1",
            "0.115",
        ]
        for ver in versions:
            with self.subTest(ver=ver):
                cand = classify_target_candidate(ver)
                self.assertFalse(cand.is_valid, f"Version '{ver}' should not be valid")
                self.assertEqual(
                    cand.classification,
                    TargetClassification.NON_FILESYSTEM_TOKEN,
                    f"Version '{ver}' must classify as NON_FILESYSTEM_TOKEN",
                )

    def test_numeric_values_and_ports_rejected(self) -> None:
        """Bare numbers and ports must never become filesystem targets."""
        numbers = ["8080", "123", "30", "3", "100", "5000", "443", "80"]
        for num in numbers:
            with self.subTest(num=num):
                cand = classify_target_candidate(num)
                self.assertFalse(cand.is_valid, f"Number '{num}' should not be valid")
                self.assertEqual(
                    cand.classification,
                    TargetClassification.NON_FILESYSTEM_TOKEN,
                    f"Number '{num}' must classify as NON_FILESYSTEM_TOKEN",
                )

    def test_issue_references_rejected(self) -> None:
        """Issue and ticket references must never become filesystem targets."""
        issues = ["#123", "GH-123", "JIRA-456", "#1", "#9999", "PROJ-10"]
        for issue in issues:
            with self.subTest(issue=issue):
                cand = classify_target_candidate(issue)
                self.assertFalse(cand.is_valid, f"Issue '{issue}' should not be valid")
                self.assertEqual(
                    cand.classification,
                    TargetClassification.NON_FILESYSTEM_TOKEN,
                    f"Issue '{issue}' must classify as NON_FILESYSTEM_TOKEN",
                )

    def test_technical_identifiers_and_abbreviations_rejected(self) -> None:
        """Technical abbreviations and acronyms must not become filesystem targets."""
        identifiers = ["JWT", "API", "SDK", "OAuth", "HTTP", "HTTPS", "REST", "SQL", "CLI"]
        for ident in identifiers:
            with self.subTest(ident=ident):
                cand = classify_target_candidate(ident)
                self.assertFalse(cand.is_valid, f"Identifier '{ident}' should not be valid")
                self.assertEqual(
                    cand.classification,
                    TargetClassification.NON_FILESYSTEM_TOKEN,
                    f"Identifier '{ident}' must classify as NON_FILESYSTEM_TOKEN",
                )

    def test_package_names_rejected(self) -> None:
        """Standalone package and dependency names must not become filesystem targets."""
        packages = [
            "fastapi",
            "react-native",
            "requests",
            "@types/node",
            "@babel/core",
            "pytest",
            "pydantic",
            "express",
            "lodash",
        ]
        for pkg in packages:
            with self.subTest(pkg=pkg):
                cand = classify_target_candidate(pkg)
                self.assertFalse(cand.is_valid, f"Package '{pkg}' should not be valid")
                self.assertEqual(
                    cand.classification,
                    TargetClassification.NON_FILESYSTEM_TOKEN,
                    f"Package '{pkg}' must classify as NON_FILESYSTEM_TOKEN",
                )

    def test_urls_endpoints_and_emails_rejected(self) -> None:
        """URLs, domains, local endpoints, and email addresses must not become targets."""
        urls = [
            "https://example.com/api/v1",
            "http://localhost:3000",
            "user@example.com",
            "https://api.example.com/users",
            "http://127.0.0.1:8000",
            "ws://localhost:8080",
            "ftp://ftp.example.org/files",
            "www.example.com/docs",
            "example.com/api/v1",
        ]
        for url in urls:
            with self.subTest(url=url):
                cand = classify_target_candidate(url)
                self.assertFalse(cand.is_valid, f"URL '{url}' should not be valid")
                self.assertEqual(
                    cand.classification,
                    TargetClassification.NON_FILESYSTEM_TOKEN,
                    f"URL '{url}' must classify as NON_FILESYSTEM_TOKEN",
                )


# =============================================================================
# 2. Valid Target Acceptance Tests (Section 18)
# =============================================================================

class TestValidTargetAcceptance(unittest.TestCase):
    """Verifies that legitimate workspace-relative filesystem targets are accepted."""

    def test_valid_filesystem_paths_accepted(self) -> None:
        """Deterministic verification for all required valid paths."""
        valid_cases = [
            ("README.md", "README.md", TargetClassification.VALID_WORKSPACE_RELATIVE_FILE),
            ("src/main.py", "src/main.py", TargetClassification.VALID_WORKSPACE_RELATIVE_FILE),
            ("src/api/routes.py", "src/api/routes.py", TargetClassification.VALID_WORKSPACE_RELATIVE_FILE),
            ("tests/test_runtime.py", "tests/test_runtime.py", TargetClassification.VALID_WORKSPACE_RELATIVE_FILE),
            ("src/service", "src/service", TargetClassification.VALID_WORKSPACE_RELATIVE_FILE),
            ("config/settings", "config/settings", TargetClassification.VALID_WORKSPACE_RELATIVE_FILE),
            ("migration_001.sql", "migration_001.sql", TargetClassification.VALID_WORKSPACE_RELATIVE_FILE),
            ("api_v2.py", "api_v2.py", TargetClassification.VALID_WORKSPACE_RELATIVE_FILE),
            ("src/v2/api.py", "src/v2/api.py", TargetClassification.VALID_WORKSPACE_RELATIVE_FILE),
            ("release-2026.md", "release-2026.md", TargetClassification.VALID_WORKSPACE_RELATIVE_FILE),
            ("react-native.config.js", "react-native.config.js", TargetClassification.VALID_WORKSPACE_RELATIVE_FILE),
            ("README", "README", TargetClassification.VALID_WORKSPACE_RELATIVE_FILE),
            ("LICENSE", "LICENSE", TargetClassification.VALID_WORKSPACE_RELATIVE_FILE),
            ("Makefile", "Makefile", TargetClassification.VALID_WORKSPACE_RELATIVE_FILE),
            ("src/", "src/", TargetClassification.VALID_WORKSPACE_RELATIVE_DIRECTORY),
        ]
        for raw, expected_norm, expected_cls in valid_cases:
            with self.subTest(raw=raw):
                cand = classify_target_candidate(raw)
                self.assertTrue(cand.is_valid, f"Target '{raw}' should be valid")
                self.assertEqual(cand.classification, expected_cls)
                self.assertEqual(cand.normalized, expected_norm)


# =============================================================================
# 3. Mixed Request Tests (Section 18)
# =============================================================================

class TestMixedRequests(unittest.TestCase):
    """Verifies target extraction from complex sentences containing incidental tokens."""

    def test_mixed_request_extraction(self) -> None:
        """Non-filesystem tokens in sentences must not pollute extracted targets."""
        cases = [
            (
                "upgrade Python 3.12 and modify src/main.py",
                ["src/main.py"],
            ),
            (
                "update https://example.com/v2 and modify README.md",
                ["README.md"],
            ),
            (
                "fix issue #123 in src/auth.py",
                ["src/auth.py"],
            ),
            (
                "upgrade FastAPI to 0.115 and update requirements.txt",
                ["requirements.txt"],
            ),
            (
                "modify src/api.py and tests/test_api.py",
                ["src/api.py", "tests/test_api.py"],
            ),
            (
                "upgrade Python 3.12 and modify src/safe.py",
                ["src/safe.py"],
            ),
            (
                "set port to 8080 and edit config/settings.yaml",
                ["config/settings.yaml"],
            ),
            (
                "address ticket 456 by updating src/service",
                ["src/service"],
            ),
        ]
        for sentence, expected_targets in cases:
            with self.subTest(sentence=sentence):
                valid, candidates = extract_deterministic_targets(sentence)
                self.assertEqual(valid, expected_targets)

    def test_extract_canonical_operation_mixed_requests(self) -> None:
        """CanonicalOperation correctly extracts valid target and discards noise."""
        op1 = extract_canonical_operation(
            "upgrade Python 3.12 and modify src/main.py",
            action=PermissionAction.WRITE_CODE,
        )
        self.assertEqual(op1.target, "src/main.py")
        self.assertNotIn("3.12", op1.parameters.get("targets", []))

        op2 = extract_canonical_operation(
            "update https://example.com/v2 and modify README.md",
            action=PermissionAction.WRITE_CODE,
        )
        self.assertEqual(op2.target, "README.md")

        op3 = extract_canonical_operation(
            "modify src/api.py and tests/test_api.py",
            action=PermissionAction.WRITE_CODE,
        )
        self.assertEqual(op3.target, "src/api.py")
        self.assertEqual(op3.parameters.get("targets"), ["src/api.py", "tests/test_api.py"])


# =============================================================================
# 4. Ambiguous Request Tests (Section 19)
# =============================================================================

class TestAmbiguousRequests(unittest.TestCase):
    """Verifies that ambiguous requests fail closed without guessing targets."""

    def test_ambiguous_requests_yield_no_guessed_target(self) -> None:
        """Ambiguous natural-language requests must not produce guessed filesystem paths."""
        ambiguous = [
            "fix config",
            "update api",
            "change authentication",
            "modify service",
            "fix backend",
            "update routes",
        ]
        for req in ambiguous:
            with self.subTest(req=req):
                valid, candidates = extract_deterministic_targets(req)
                self.assertEqual(
                    valid,
                    [],
                    f"Ambiguous request '{req}' must not produce valid targets, got: {valid}",
                )

    def test_canonical_operation_fails_closed_on_ambiguity(self) -> None:
        """CanonicalOperation marks ambiguous requests as target='' and ambiguous=True."""
        ambiguous = [
            "fix config",
            "update api",
            "change authentication",
            "modify service",
            "fix backend",
            "update routes",
        ]
        for req in ambiguous:
            with self.subTest(req=req):
                op = extract_canonical_operation(req, action=PermissionAction.WRITE_CODE)
                self.assertEqual(op.target, "")
                self.assertTrue(op.parameters.get("ambiguous", False))

    def test_runtime_rejects_ambiguous_mutation_request(self) -> None:
        """BrainFrogRuntime immediately returns a rejection for ambiguous mutation."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            runtime = BrainFrogRuntime(
                repo_dir=repo_dir,
                require_approval=True,
                system1_factory=lambda _: MockSystem1(),
                system2_factory=lambda **_: MockSystem2(),
            )
            msg = IncomingMessage(
                id="msg_ambig",
                channel="telegram",
                user_id="alice",
                conversation_id="chat_1",
                text="modify the file config",
            )
            resp = runtime.handle_message(msg)
            self.assertFalse(resp.success)
            self.assertEqual(resp.status, "rejected")
            self.assertIn("Target is ambiguous", resp.text)


# =============================================================================
# 5. Path Attack Tests (Section 20)
# =============================================================================

class TestPathAttacks(unittest.TestCase):
    """Verifies that path traversal and malformed inputs are deterministically rejected."""

    def test_path_traversal_and_absolute_paths_rejected(self) -> None:
        """Traversal, absolute, drive letters, and UNC paths must classify as INVALID_PATH."""
        attacks = [
            "../secret.txt",
            "../../secret.txt",
            r"..\..\secret.txt",
            r"C:\Windows\System32\file",
            "C:/Windows/System32/file",
            r"\\server\share\file",
            "//server/share/file",
            "/foo/bar",
            "src/../../secret.txt",
            r"src\..\..\secret.txt",
            "foo/bar/../../../etc/passwd",
        ]
        for attack in attacks:
            with self.subTest(attack=attack):
                cand = classify_target_candidate(attack)
                self.assertFalse(cand.is_valid, f"Attack '{attack}' should not be valid")
                self.assertEqual(
                    cand.classification,
                    TargetClassification.INVALID_PATH,
                    f"Attack '{attack}' must classify as INVALID_PATH",
                )

    def test_runtime_rejects_invalid_path_attack(self) -> None:
        """BrainFrogRuntime immediately rejects requests containing path attacks."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            runtime = BrainFrogRuntime(
                repo_dir=repo_dir,
                require_approval=True,
                system1_factory=lambda _: MockSystem1(),
                system2_factory=lambda **_: MockSystem2(),
            )
            msg = IncomingMessage(
                id="msg_attack",
                channel="telegram",
                user_id="alice",
                conversation_id="chat_1",
                text="write_file ../secret.txt",
            )
            resp = runtime.handle_message(msg)
            self.assertFalse(resp.success)
            self.assertEqual(resp.status, "rejected")
            self.assertIn("Invalid target path", resp.text)


# =============================================================================
# 6. Property-Style Tests (Section 22)
# =============================================================================

class TestPropertyStyleTargetExtraction(unittest.TestCase):
    """Verifies security property: No deterministic filesystem path => No authorization."""

    def test_arbitrary_technical_noise_never_authorizes_target(self) -> None:
        """Sentences with only technical noise must never result in an authorized target."""
        noise_samples = [
            "bump dependency to 2.1.0",
            "listen on port 8080 with 30s timeout",
            "resolve GH-123 and close ticket 456",
            "configure JWT authentication with OAuth provider",
            "fetch data from https://api.example.com/v1 endpoint",
            "upgrade react-native and fastapi packages",
            "handle HTTP 500 error code",
            "install @types/node in the project",
        ]
        for sample in noise_samples:
            with self.subTest(sample=sample):
                valid, candidates = extract_deterministic_targets(sample)
                self.assertEqual(
                    valid,
                    [],
                    f"Noise sample '{sample}' must produce 0 valid targets, got: {valid}",
                )
                op = extract_canonical_operation(sample, action=PermissionAction.WRITE_CODE)
                self.assertEqual(op.target, "")


# =============================================================================
# 7. True End-to-End Test (Section 21)
# =============================================================================

class TestTrueEndToEndTargetExtraction(unittest.TestCase):
    """Exercises: IncomingMessage -> extraction -> approval -> /exec -> Contract -> Orchestrator -> disk."""

    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="bf_e2e_m02_"))
        self.repo_dir = self.temp_dir / "workspace"
        self.repo_dir.mkdir()

        # Initialize git repo
        subprocess.run(["git", "init"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.repo_dir, capture_output=True, check=True)
        (self.repo_dir / "README.md").write_text("# Test Repo\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=self.repo_dir, capture_output=True, check=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_e2e_mixed_request_authorizes_only_valid_target(self) -> None:
        """Request: 'upgrade Python 3.12 and modify src/safe.py'

        Verifies:
        1. approved_targets == {'src/safe.py'}
        2. '3.12' never becomes an approved target.
        3. Attempted unauthorized output 'evil.py' is rejected by Phase 14B-1 contract boundary.
        """
        # Ensure src/ exists
        (self.repo_dir / "src").mkdir(exist_ok=True)

        runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            require_approval=True,
            system1_factory=lambda _: MockSystem1(),
            system2_factory=lambda **_: MockSystem2({"src/safe.py": "# legitimate code\n"}),
            default_test_cmd='python -c "pass"',
        )

        # 1. Incoming Request with technical noise
        resp1 = runtime.handle_message(IncomingMessage(
            id="m1",
            channel="telegram",
            user_id="alice",
            conversation_id="chat_1",
            text="upgrade Python 3.12 and modify src/safe.py",
        ))
        self.assertEqual(resp1.status, "pending_approval")
        req_id = resp1.metadata["request_id"]

        # Verify target in ApprovalRequest is ONLY 'src/safe.py'
        app_req = runtime.approval_service.store.get(req_id)
        self.assertIsNotNone(app_req)
        self.assertEqual(app_req.canonical_operation.target, "src/safe.py")

        # 2. Peer Approve
        resp2 = runtime.handle_message(IncomingMessage(
            id="m2",
            channel="telegram",
            user_id="bob",
            conversation_id="chat_1",
            text=f"/approve {req_id}",
        ))
        self.assertTrue(resp2.success)

        # 3. Exec
        resp3 = runtime.handle_message(IncomingMessage(
            id="m3",
            channel="telegram",
            user_id="alice",
            conversation_id="chat_1",
            text=f"/exec {req_id}",
        ))
        self.assertEqual(resp3.status, "completed")

        # Verify disk write
        safe_file = self.repo_dir / "src" / "safe.py"
        self.assertTrue(safe_file.exists())
        self.assertEqual(safe_file.read_text(encoding="utf-8"), "# legitimate code\n")

        # Verify 3.12 never became a target on disk
        self.assertFalse((self.repo_dir / "3.12").exists())

    def test_e2e_contract_rejects_unauthorized_file_injection(self) -> None:
        """If System 2 attempts to write both 'src/safe.py' and 'evil.py', write is blocked."""
        contract = ApprovedExecutionContract(
            request_id="req_test",
            action_type="write_file",
            approved_targets=frozenset(["src/safe.py"]),
            operation_digest="fake_digest",
            channel="telegram",
        )

        (self.repo_dir / "src").mkdir(exist_ok=True)

        with self.assertRaises(PermissionError):
            _write_files(
                self.repo_dir,
                {
                    "src/safe.py": "# clean",
                    "evil.py": "# attack",
                },
                mode="build",
                contract=contract,
            )

        self.assertFalse((self.repo_dir / "evil.py").exists())


if __name__ == "__main__":
    unittest.main()
