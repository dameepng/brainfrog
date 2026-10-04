"""Tests for Phase 14B-1: Approved Execution Contract and Filesystem Boundary.

Validates the remediation of:
- CRITICAL-01: Approval Scope Bleed & Unconstrained File Writes
- CRITICAL-02: Path Traversal in _write_files

Tests both unit invariants and real end-to-end runtime execution paths.
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

from core.runtime.capabilities import Capabilities, FilesystemPolicy
from core.runtime.approval import ApprovalService, CanonicalOperation, InMemoryApprovalStore
from core.runtime.contract import ApprovedExecutionContract, normalize_target_rel_path
from core.runtime.messages import IncomingMessage
from core.runtime.runtime import BrainFrogRuntime
from orchestrator import Orchestrator, PlanStep, RunConfig, _write_files, validate_and_stage_files
from system1.base import Answer, SystemOneClient
from system2 import System2Client


# =============================================================================
# Deterministic Test Doubles
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
        self.output_files = output_files or {"safe.txt": "print('clean')"}

    def plan_task(self, task: str, focus_tree: str = "", **kwargs: Any) -> List[PlanStep]:
        return [PlanStep(id="1", description="Execute approved step", files=list(self.output_files.keys()))]

    def write_code(self, step: PlanStep, task: str, file_contents: Dict[str, str], **kwargs: Any) -> Dict[str, str]:
        return dict(self.output_files)

    def draft_pr(self, task: str, files_changed: List[str], test_summary: str = "", **kwargs: Any) -> Dict[str, str]:
        return {"title": f"feat: {task}", "body": "Approved execution."}


# =============================================================================
# 1. Direct Unit Tests for validate_and_stage_files & _write_files
# =============================================================================

class TestFilesystemBoundaryAndContract(unittest.TestCase):
    """Direct unit tests for filesystem path normalization, containment, and contract enforcement."""

    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="bf_test_contract_"))
        self.repo_dir = self.temp_dir / "workspace"
        self.repo_dir.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def issued_contract(self, **fields):
        """Exercise the real issuance boundary with explicit Phase 15A grants."""
        targets = sorted(fields["approved_targets"])
        service = ApprovalService(InMemoryApprovalStore())
        request = service.create_request(
            "session", fields["channel"], "alice", "chat", fields["action_type"],
            CanonicalOperation(fields["action_type"], targets[0], {"targets": targets}),
            session_incarnation_id="incarnation", capabilities=fields["capabilities"],
            workspace_root=str(self.repo_dir),
        )
        self.assertTrue(service.approve(request.request_id, "bob", fields["channel"])[0])
        self.assertTrue(service.verify_and_consume(
            request.request_id, request.operation_digest, "session", fields["channel"],
            session_incarnation_id="incarnation", requester_id="alice",
        )[0])
        return ApprovedExecutionContract.from_approval_request(request, self.repo_dir)

    def test_approved_target_allows_exact_file(self) -> None:
        """1. An approved target allows writing exactly that file."""
        contract = self.issued_contract(
            action_type="write_file",
            approved_targets=frozenset(["safe.txt"]),
            capabilities=Capabilities(FilesystemPolicy(write=["safe.txt"])),
            channel="telegram",
        )
        _write_files(self.repo_dir, {"safe.txt": "content"}, mode="build", contract=contract)
        target = self.repo_dir / "safe.txt"
        self.assertTrue(target.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), "content")

    def test_unapproved_extra_file_denied(self) -> None:
        """2. When an unapproved extra file is returned, write is denied."""
        contract = self.issued_contract(
            action_type="write_file",
            approved_targets=frozenset(["safe.txt"]),
            capabilities=Capabilities(FilesystemPolicy(write=["safe.txt"])),
            channel="telegram",
        )
        with self.assertRaises(PermissionError) as ctx:
            _write_files(
                self.repo_dir,
                {"safe.txt": "content", "evil.py": "malicious"},
                mode="build",
                contract=contract,
            )
        self.assertIn("outside the approved scope", str(ctx.exception))
        self.assertIn("evil.py", str(ctx.exception))

    def test_unapproved_extra_file_causes_no_partial_write(self) -> None:
        """3. An unapproved extra file causes ZERO partial writes."""
        contract = self.issued_contract(
            action_type="write_file",
            approved_targets=frozenset(["safe.txt"]),
            capabilities=Capabilities(FilesystemPolicy(write=["safe.txt"])),
            channel="telegram",
        )
        safe_file = self.repo_dir / "safe.txt"
        evil_file = self.repo_dir / "evil.py"

        with self.assertRaises(PermissionError):
            _write_files(
                self.repo_dir,
                {"safe.txt": "content", "evil.py": "malicious"},
                mode="build",
                contract=contract,
            )

        self.assertFalse(safe_file.exists(), "safe.txt MUST NOT exist after denied batch write")
        self.assertFalse(evil_file.exists(), "evil.py MUST NOT exist after denied batch write")

    def test_parent_directory_traversal_denied(self) -> None:
        """4. Path traversal via ../ is strictly denied."""
        with self.assertRaises(PermissionError) as ctx:
            _write_files(self.repo_dir, {"../outside.txt": "escaped"}, mode="build")
        self.assertIn("escapes", str(ctx.exception).lower())

    def test_windows_drive_path_denied(self) -> None:
        """5. Windows drive paths outside repo_dir are denied."""
        outside_path = "D:/outside.txt" if str(self.repo_dir).startswith("C:") else "C:/outside.txt"
        with self.assertRaises(PermissionError):
            _write_files(self.repo_dir, {outside_path: "escaped"}, mode="build")

    def test_absolute_path_denied(self) -> None:
        """6. Absolute POSIX paths outside repo_dir are denied."""
        with self.assertRaises(PermissionError):
            _write_files(self.repo_dir, {"/tmp/outside.txt": "escaped"}, mode="build")

    def test_mixed_separator_traversal_denied(self) -> None:
        """7. Mixed separators (..\\) are normalized and denied."""
        with self.assertRaises(PermissionError):
            _write_files(self.repo_dir, {"..\\outside.txt": "escaped"}, mode="build")

    def test_nested_valid_path_allowed(self) -> None:
        """8. Nested valid paths within repo_dir are allowed."""
        contract = self.issued_contract(
            action_type="write_file",
            approved_targets=frozenset(["src/components/Button.tsx"]),
            capabilities=Capabilities(FilesystemPolicy(write=["src/components/Button.tsx"])),
            channel="telegram",
        )
        _write_files(self.repo_dir, {"src/components/Button.tsx": "button"}, mode="build", contract=contract)
        target = self.repo_dir / "src" / "components" / "Button.tsx"
        self.assertTrue(target.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), "button")

    def test_nested_traversal_denied(self) -> None:
        """9. Nested traversal (foo/../../outside.txt) is denied."""
        with self.assertRaises(PermissionError):
            _write_files(self.repo_dir, {"src/../../outside.txt": "escaped"}, mode="build")

    def test_symlink_escape_denied_where_supported(self) -> None:
        """10. Symlinks pointing outside repo_dir are denied."""
        outside_dir = self.temp_dir / "outside"
        outside_dir.mkdir()
        link_dir = self.repo_dir / "sub_link"
        try:
            link_dir.symlink_to(outside_dir)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation requires administrative privileges on Windows")

        with self.assertRaises(PermissionError):
            _write_files(self.repo_dir, {"sub_link/secret.txt": "escaped"}, mode="build")

    def test_multi_file_approved_scope_subset_allowed(self) -> None:
        """Multi-file approved scope allows valid subsets but denies supersets."""
        contract = self.issued_contract(
            action_type="write_files",
            approved_targets=frozenset(["a.py", "b.py", "c.py"]),
            capabilities=Capabilities(FilesystemPolicy(write=["a.py", "b.py", "c.py"])),
            channel="telegram",
        )
        # Subset {a.py, b.py} -> allowed
        _write_files(self.repo_dir, {"a.py": "A", "b.py": "B"}, mode="build", contract=contract)
        self.assertTrue((self.repo_dir / "a.py").exists())
        self.assertTrue((self.repo_dir / "b.py").exists())

        # Superset with unapproved d.py -> denied with zero partial writes
        d_file = self.repo_dir / "d.py"
        with self.assertRaises(PermissionError):
            _write_files(
                self.repo_dir,
                {"a.py": "A_updated", "d.py": "unapproved"},
                mode="build",
                contract=contract,
            )
        self.assertFalse(d_file.exists())
        # a.py was NOT updated because the entire batch write was rejected
        self.assertEqual((self.repo_dir / "a.py").read_text(encoding="utf-8"), "A")

    def test_cli_execution_allows_unconstrained_workspace_writes(self) -> None:
        """CLI trusted execution (contract=None) allows writing any valid files inside repo."""
        _write_files(
            self.repo_dir,
            {"cli_file1.py": "1", "sub/cli_file2.py": "2"},
            mode="build",
            contract=None,
        )
        self.assertTrue((self.repo_dir / "cli_file1.py").exists())
        self.assertTrue((self.repo_dir / "sub" / "cli_file2.py").exists())


# =============================================================================
# 2. Real End-to-End Test (Runtime -> Approval -> /exec -> Orchestrator -> Filesystem)
# =============================================================================

class TestRealEndToEndRemoteApprovalContract(unittest.TestCase):
    """Exercises the complete pipeline from IncomingMessage to disk write."""

    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="bf_e2e_contract_"))
        self.repo_dir = self.temp_dir / "workspace"
        self.repo_dir.mkdir()

        # Initialize real git repo
        subprocess.run(["git", "init"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.repo_dir, capture_output=True, check=True)
        (self.repo_dir / "README.md").write_text("# Test Repo\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=self.repo_dir, capture_output=True, check=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_e2e_approved_operation_succeeds_for_exact_file(self) -> None:
        """Real E2E: Approved write_file safe.txt writes safe.txt cleanly."""
        runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            require_approval=True,
            system1_factory=lambda _: MockSystem1(),
            system2_factory=lambda **_: MockSystem2({"safe.txt": "# legitimate code"}),
            default_test_cmd='python -c "pass"',
        )

        # 1. Request
        resp1 = runtime.handle_message(IncomingMessage(
            id="m1", channel="telegram", user_id="alice", conversation_id="chat_1",
            text="write_file safe.txt"
        ))
        self.assertEqual(resp1.status, "pending_approval")
        req_id = resp1.metadata["request_id"]

        # 2. Peer Approve
        resp2 = runtime.handle_message(IncomingMessage(
            id="m2", channel="telegram", user_id="bob", conversation_id="chat_1",
            text=f"/approve {req_id}"
        ))
        self.assertTrue(resp2.success)

        # 3. Exec
        resp3 = runtime.handle_message(IncomingMessage(
            id="m3", channel="telegram", user_id="alice", conversation_id="chat_1",
            text=f"/exec {req_id}"
        ))
        self.assertEqual(resp3.status, "completed")

        safe_file = self.repo_dir / "safe.txt"
        self.assertTrue(safe_file.exists())
        self.assertEqual(safe_file.read_text(encoding="utf-8"), "# legitimate code")

    def test_e2e_critical_01_scope_bleed_fails_safely_with_zero_writes(self) -> None:
        """Phase 14A CRITICAL-01 PoC fails safely: extra evil.py causes complete rejection."""
        runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            require_approval=True,
            system1_factory=lambda _: MockSystem1(),
            system2_factory=lambda **_: MockSystem2({
                "safe.txt": "# approved code",
                "evil_backdoor.py": "# MALICIOUS BACKDOOR",
            }),
            default_test_cmd='python -c "pass"',
        )

        # 1. Request for safe.txt
        resp1 = runtime.handle_message(IncomingMessage(
            id="m1", channel="telegram", user_id="alice", conversation_id="chat_1",
            text="write_file safe.txt"
        ))
        req_id = resp1.metadata["request_id"]

        # 2. Bob approves safe.txt
        resp2 = runtime.handle_message(IncomingMessage(
            id="m2", channel="telegram", user_id="bob", conversation_id="chat_1",
            text=f"/approve {req_id}"
        ))
        self.assertTrue(resp2.success)

        # 3. Alice executes, but System 2 attempts scope bleed
        resp3 = runtime.handle_message(IncomingMessage(
            id="m3", channel="telegram", user_id="alice", conversation_id="chat_1",
            text=f"/exec {req_id}"
        ))

        # Must report error
        self.assertEqual(resp3.status, "error")
        self.assertIn("outside the approved scope", resp3.text)
        self.assertIn("evil_backdoor.py", resp3.text)

        # Invariant: Neither file exists on disk! Zero partial writes!
        self.assertFalse((self.repo_dir / "safe.txt").exists(), "safe.txt must not exist")
        self.assertFalse((self.repo_dir / "evil_backdoor.py").exists(), "evil_backdoor.py must not exist")

    def test_e2e_critical_02_path_traversal_fails_safely(self) -> None:
        """Phase 14A CRITICAL-02 PoC fails safely: ../ traversal is blocked."""
        runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            require_approval=True,
            system1_factory=lambda _: MockSystem1(),
            system2_factory=lambda **_: MockSystem2({
                "../escaped_secret.txt": "LEAKED_SECRET",
            }),
            default_test_cmd='python -c "pass"',
        )

        resp1 = runtime.handle_message(IncomingMessage(
            id="m1", channel="telegram", user_id="alice", conversation_id="chat_1",
            text="write_file safe.txt"
        ))
        req_id = resp1.metadata["request_id"]

        resp2 = runtime.handle_message(IncomingMessage(
            id="m2", channel="telegram", user_id="bob", conversation_id="chat_1",
            text=f"/approve {req_id}"
        ))
        self.assertTrue(resp2.success)

        resp3 = runtime.handle_message(IncomingMessage(
            id="m3", channel="telegram", user_id="alice", conversation_id="chat_1",
            text=f"/exec {req_id}"
        ))

        self.assertEqual(resp3.status, "error")
        escaped_file = self.temp_dir / "escaped_secret.txt"
        self.assertFalse(escaped_file.exists(), "Escaped file must not be written outside repository")


if __name__ == "__main__":
    unittest.main()
