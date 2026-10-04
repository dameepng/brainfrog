"""test_remote_git_push_boundary.py — Phase 14B-4 Security Tests.

Verifies HIGH-03 Remediation:
Automatic Remote Git Push During Approved Remote Execution.

Security Invariant:
An approval for a local file operation must never authorize a remote Git side effect.
Specifically:
1. Remote-approved file creation or modification must not push to any remote.
2. Remote-approved deletion must not push to any remote.
3. System 2 output must never independently enable pushing.
4. auto_pr=False must not be treated as the only security control.
5. A remote operation must not gain additional authority because it reaches the canonical orchestrator.
6. The restriction must apply to both Telegram and WhatsApp.
7. The restriction must survive process restart and approval consumption.
8. Local CLI behavior should remain compatible with existing documented functionality.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from core.runtime.approval import (
    ApprovalRequest,
    ApprovalService,
    ApprovalStatus,
    CanonicalOperation,
    FileApprovalStore,
)
from core.runtime.contract import ApprovedExecutionContract
from core.runtime.messages import IncomingMessage
from core.runtime.permissions import (
    ChannelTrustLevel,
    PermissionAction,
    PermissionPolicy,
    get_default_policy,
)
from core.runtime.runtime import BrainFrogRuntime
from core.runtime.session import SessionManager
from orchestrator import (
    Orchestrator,
    RunConfig,
    StepResult,
    clean_git_remote_url,
    configure_git_remote,
    get_git_remote_url,
)
from system1.base import Answer
from system2.claude_client import PlanStep


class TestRemoteGitPushBoundary(unittest.TestCase):
    """Test suite verifying the remote Git push security boundary."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="bf-test-push-boundary-")
        self.bare_dir = Path(self.temp_dir) / "bare_remote.git"
        self.work_dir = Path(self.temp_dir) / "work_repo"

        # 1. Initialize bare remote repository
        self.bare_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "--bare"], cwd=self.bare_dir, check=True, capture_output=True)

        # 2. Initialize working repository
        self.work_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init"], cwd=self.work_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "SecurityTester"], cwd=self.work_dir, check=True)
        subprocess.run(["git", "config", "user.email", "tester@brainfrog.local"], cwd=self.work_dir, check=True)
        subprocess.run(["git", "branch", "-M", "main"], cwd=self.work_dir, check=True, capture_output=True)

        # Initial commit in working repo
        (self.work_dir / "README.md").write_text("# Project Root\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.work_dir, check=True)
        subprocess.run(["git", "commit", "-m", "chore: initial commit"], cwd=self.work_dir, check=True, capture_output=True)

        # Link bare remote as 'origin' and push initial commit
        bare_uri = self.bare_dir.resolve().as_uri()
        subprocess.run(["git", "remote", "add", "origin", bare_uri], cwd=self.work_dir, check=True)
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=self.work_dir, check=True, capture_output=True)

        # Capture the baseline commit SHA of the bare remote
        self.initial_remote_sha = self._get_remote_head_sha()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _get_remote_head_sha(self) -> str:
        """Get the current commit SHA of main branch in the bare remote."""
        res = subprocess.run(
            ["git", "rev-parse", "refs/heads/main"],
            cwd=self.bare_dir,
            capture_output=True,
            text=True,
        )
        return res.stdout.strip() if res.returncode == 0 else ""

    def _make_mock_systems(self, modified_files: dict[str, str] | None = None):
        """Construct mock System 1 and System 2 clients."""
        mock_s1 = MagicMock()
        mock_s1.name = "jev_mock"
        def mock_decide(state, questions):
            out = {}
            for k, q in questions.items():
                if k == "tests_passing":
                    out[k] = Answer(choice="yes", noul=1.0, confidence=1.0)
                elif k == "diff_complete":
                    out[k] = Answer(choice="yes", noul=1.0, confidence=1.0)
                elif k == "failure_fixable":
                    out[k] = Answer(choice="yes", noul=1.0, confidence=1.0)
                elif k == "retry_concern":
                    out[k] = Answer(score=0, confidence=1.0)
                elif k == "diff_risk":
                    out[k] = Answer(choice="low", score=0, noul=0.1, confidence=0.9)
                elif k == "safe_to_proceed":
                    out[k] = Answer(choice="open_pr", score="low", noul=1.0, confidence=0.95)
                else:
                    out[k] = Answer(choice="yes", score="low", noul=1.0, confidence=0.95)
            return out

        mock_s1.decide.side_effect = mock_decide
        mock_s2 = MagicMock()
        mock_s2.provider_name = "claude"
        mock_s2.model = "claude-sonnet-5"
        mock_s2.guidelines = ""
        mock_s2.draft_pr.return_value = {
            "title": "feat: security verified change",
            "body": "Automated code modification under execution contract.",
        }
        files = modified_files or {"file.py": "print('hello')\n"}
        mock_s2.plan_task.return_value = [PlanStep("1", "apply change", list(files.keys()))]
        mock_s2.write_code.return_value = files
        mock_s2.review_and_fix.return_value = files
        return mock_s1, mock_s2

    # =========================================================================
    # Group A: Remote Execution Side-Effect Boundary
    # =========================================================================

    def test_approved_telegram_file_modification_does_not_invoke_remote_push(self):
        """Telegram file approval authorizes neither a local commit nor a remote push."""
        mock_s1, mock_s2 = self._make_mock_systems({"component.py": "def foo(): return 42\n"})
        logs: list[str] = []

        contract = ApprovedExecutionContract(
            request_id="req_tg_001",
            action_type="write_file",
            approved_targets=frozenset(["component.py"]),
            operation_digest="digest_tg_001",
            channel="telegram",
            allow_remote_git_push=False,
        )

        cfg = RunConfig(
            task="create component.py",
            repo_dir=self.work_dir,
            auto_pr=False,
            test_command=["python", "-c", "import sys; sys.exit(0)"],
            execution_contract=contract,
            origin_channel="telegram",
            allow_remote_git_push=False,
        )

        orch = Orchestrator(mock_s1, mock_s2, cfg, log_fn=lambda m: logs.append(m))
        step = PlanStep("1", "create component", ["component.py"])

        (self.work_dir / "component.py").write_text("def foo(): return 42\n", encoding="utf-8")
        subprocess.run(["git", "add", "component.py"], cwd=self.work_dir, check=True)

        res = orch._finalize_pr(step, retries=0, test_output="PASS")

        # 1. Local file exists; Phase 15A does not authorize a commit
        self.assertTrue((self.work_dir / "component.py").exists())
        self.assertEqual(res.outcome, "unverified")

        # 2. Remote bare repository MUST NOT have received any push
        current_remote_sha = self._get_remote_head_sha()
        self.assertEqual(
            current_remote_sha,
            self.initial_remote_sha,
            "Security violation: bare remote received unexpected git push during Telegram execution!",
        )

        # 3. Log must show remote push was skipped
        self.assertTrue(any("Remote Git push skipped" in l for l in logs))

    def test_approved_whatsapp_file_modification_does_not_invoke_remote_push(self):
        """WhatsApp file approval authorizes neither a local commit nor a remote push."""
        mock_s1, mock_s2 = self._make_mock_systems({"wa_service.py": "# whatsapp service\n"})
        logs: list[str] = []

        contract = ApprovedExecutionContract(
            request_id="req_wa_001",
            action_type="write_file",
            approved_targets=frozenset(["wa_service.py"]),
            operation_digest="digest_wa_001",
            channel="whatsapp",
            allow_remote_git_push=False,
        )

        cfg = RunConfig(
            task="create wa_service.py",
            repo_dir=self.work_dir,
            auto_pr=False,
            test_command=["python", "-c", "import sys; sys.exit(0)"],
            execution_contract=contract,
            origin_channel="whatsapp",
            allow_remote_git_push=False,
        )

        orch = Orchestrator(mock_s1, mock_s2, cfg, log_fn=lambda m: logs.append(m))
        step = PlanStep("1", "create wa_service", ["wa_service.py"])

        (self.work_dir / "wa_service.py").write_text("# whatsapp service\n", encoding="utf-8")
        subprocess.run(["git", "add", "wa_service.py"], cwd=self.work_dir, check=True)

        res = orch._finalize_pr(step, retries=0, test_output="PASS")

        self.assertTrue((self.work_dir / "wa_service.py").exists())
        self.assertEqual(
            self._get_remote_head_sha(),
            self.initial_remote_sha,
            "Security violation: bare remote received unexpected git push during WhatsApp execution!",
        )
        self.assertTrue(any("Remote Git push skipped" in l for l in logs))

    def test_remote_deletion_does_not_invoke_remote_push(self):
        """Remote deletion of files must not invoke remote git push."""
        mock_s1, mock_s2 = self._make_mock_systems()
        logs: list[str] = []

        # Create an existing file to delete
        del_target = self.work_dir / "obsolete.txt"
        del_target.write_text("to be deleted", encoding="utf-8")
        subprocess.run(["git", "add", "obsolete.txt"], cwd=self.work_dir, check=True)
        subprocess.run(["git", "commit", "-m", "add obsolete file"], cwd=self.work_dir, check=True)

        contract = ApprovedExecutionContract(
            request_id="req_del_001",
            action_type="delete_file",
            approved_targets=frozenset(["obsolete.txt"]),
            operation_digest="digest_del_001",
            channel="telegram",
            allow_remote_git_push=False,
        )

        cfg = RunConfig(
            task="delete obsolete.txt",
            repo_dir=self.work_dir,
            auto_pr=False,
            test_command=["python", "-c", "import sys; sys.exit(0)"],
            execution_contract=contract,
            origin_channel="telegram",
            allow_remote_git_push=False,
        )

        orch = Orchestrator(mock_s1, mock_s2, cfg, log_fn=lambda m: logs.append(m))
        step = PlanStep("1", "delete file", ["obsolete.txt"])

        # Perform deletion
        del_target.unlink()
        subprocess.run(["git", "rm", "obsolete.txt"], cwd=self.work_dir, check=True)

        orch._finalize_pr(step, retries=0, test_output="PASS")

        self.assertFalse(del_target.exists())
        self.assertEqual(
            self._get_remote_head_sha(),
            self.initial_remote_sha,
            "Security violation: remote deletion pushed to remote repository!",
        )

    def test_remote_execution_with_auto_pr_true_cannot_push(self):
        """Even if auto_pr=True is set on RunConfig, remote execution provenance blocks pushing and PR opening."""
        mock_s1, mock_s2 = self._make_mock_systems({"feature.py": "x = 1\n"})
        logs: list[str] = []

        contract = ApprovedExecutionContract(
            request_id="req_autopr_001",
            action_type="write_file",
            approved_targets=frozenset(["feature.py"]),
            operation_digest="digest_autopr_001",
            channel="telegram",
            allow_remote_git_push=False,
        )

        cfg = RunConfig(
            task="create feature.py",
            repo_dir=self.work_dir,
            auto_pr=True,  # Intentionally True
            test_command=["python", "-c", "import sys; sys.exit(0)"],
            execution_contract=contract,
            origin_channel="telegram",
            allow_remote_git_push=False,
        )

        orch = Orchestrator(mock_s1, mock_s2, cfg, log_fn=lambda m: logs.append(m))
        step = PlanStep("1", "create feature", ["feature.py"])

        (self.work_dir / "feature.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "feature.py"], cwd=self.work_dir, check=True)

        res = orch._finalize_pr(step, retries=0, test_output="PASS")

        # Must not be "opened_pr" because remote execution cannot push or open PR
        self.assertEqual(res.outcome, "unverified")
        self.assertEqual(
            self._get_remote_head_sha(),
            self.initial_remote_sha,
            "Security violation: auto_pr=True bypassed remote push restriction!",
        )

    def test_remote_execution_cannot_push_tags_or_force_push(self):
        """_push_to_remote raises PermissionError when called on remote context, even with force=True or tags=True."""
        contract = ApprovedExecutionContract(
            request_id="req_tg_002",
            action_type="write_file",
            approved_targets=frozenset(["a.txt"]),
            operation_digest="d123",
            channel="telegram",
            allow_remote_git_push=False,
        )

        cfg = RunConfig(
            task="test",
            repo_dir=self.work_dir,
            test_command=["python", "-c", "import sys; sys.exit(0)"],
            execution_contract=contract,
            origin_channel="telegram",
            allow_remote_git_push=False,
        )

        mock_s1, mock_s2 = self._make_mock_systems()
        orch = Orchestrator(mock_s1, mock_s2, cfg)

        # Standard push
        with self.assertRaises(PermissionError):
            orch._push_to_remote("main", "origin")

        # Force push
        with self.assertRaises(PermissionError):
            orch._push_to_remote("main", "origin", force=True)

        # Push tags
        with self.assertRaises(PermissionError):
            orch._push_to_remote("main", "origin", tags=True)

    def test_alternate_internal_path_open_pr_blocked(self):
        """Calling _open_pr directly on a remote-channel orchestrator is strictly blocked."""
        logs: list[str] = []
        contract = ApprovedExecutionContract(
            request_id="req_tg_003",
            action_type="write_file",
            approved_targets=frozenset(["a.txt"]),
            operation_digest="d123",
            channel="telegram",
            allow_remote_git_push=False,
        )
        cfg = RunConfig(
            task="test",
            repo_dir=self.work_dir,
            test_command=["python", "-c", "import sys; sys.exit(0)"],
            execution_contract=contract,
            origin_channel="telegram",
            allow_remote_git_push=False,
        )
        mock_s1, mock_s2 = self._make_mock_systems()
        orch = Orchestrator(mock_s1, mock_s2, cfg, log_fn=lambda m: logs.append(m))
        step = PlanStep("1", "test step", ["a.txt"])

        orch._open_pr(step, {"title": "malicious PR", "body": "details"})

        self.assertTrue(any("Remote PR creation / push blocked" in l for l in logs))
        self.assertEqual(self._get_remote_head_sha(), self.initial_remote_sha)

    # =========================================================================
    # Group B: Local CLI Compatibility & Anti-Spoofing
    # =========================================================================

    def test_local_cli_trusted_execution_allows_remote_push(self):
        """Trusted local CLI execution retains capability to push to git remote."""
        logs: list[str] = []
        cfg = RunConfig(
            task="local cli task",
            repo_dir=self.work_dir,
            auto_pr=False,
            test_command=["python", "-c", "import sys; sys.exit(0)"],
            execution_contract=None,
            origin_channel="cli",
            allow_remote_git_push=True,
        )
        mock_s1, mock_s2 = self._make_mock_systems()
        orch = Orchestrator(mock_s1, mock_s2, cfg, log_fn=lambda m: logs.append(m))
        step = PlanStep("1", "local step", ["cli_file.txt"])

        (self.work_dir / "cli_file.txt").write_text("created by local cli\n", encoding="utf-8")
        subprocess.run(["git", "add", "cli_file.txt"], cwd=self.work_dir, check=True)

        res = orch._finalize_pr(step, retries=0, test_output="PASS")

        # CLI push succeeded
        new_remote_sha = self._get_remote_head_sha()
        self.assertNotEqual(new_remote_sha, self.initial_remote_sha)
        self.assertTrue(any("Successfully pushed to origin/main" in l for l in logs))

    def test_remote_origin_spoofing_cannot_turn_remote_request_into_trusted_cli(self):
        """External user claiming 'cli' channel is downgraded to remote channel and cannot push."""
        store = FileApprovalStore(Path(self.temp_dir) / "approvals")
        approval_service = ApprovalService(store)
        session_mgr = SessionManager()

        runtime = BrainFrogRuntime(
            repo_dir=self.work_dir,
            approval_service=approval_service,
            sessions=session_mgr,
            require_approval=True,
        )

        # Attacker sends message claiming channel='cli', but with untrusted user_id
        spoofed_msg = IncomingMessage(
            channel="cli",
            user_id="attacker_external_id_999",
            text="write file evil.py with print('evil')",
            metadata={"auto_pr": True},
        )

        resp = runtime.process_message(spoofed_msg)
        # Should be pending approval because untrusted user was downgraded to remote_channel
        self.assertEqual(resp.status, "pending_approval")
        req_id = resp.metadata.get("request_id")
        self.assertTrue(req_id)

        # Verify the created approval request is bound to remote_channel, NOT cli!
        req_obj = store.get(req_id)
        self.assertIsNotNone(req_obj)
        self.assertEqual(req_obj.channel, "remote_channel")

    # =========================================================================
    # Group C: Approval Integrity & Defense-in-Depth
    # =========================================================================

    def test_approval_for_write_file_cannot_be_reused_as_git_push(self):
        """Approval granted for write_file cannot authorize git_push and fails digest validation."""
        store = FileApprovalStore(Path(self.temp_dir) / "approvals")
        approval_service = ApprovalService(store, two_man_rule_enabled=False)
        session_mgr = SessionManager()

        runtime = BrainFrogRuntime(
            repo_dir=self.work_dir,
            approval_service=approval_service,
            sessions=session_mgr,
            require_approval=True,
        )

        # 1. Request file creation
        msg = IncomingMessage(
            channel="telegram",
            user_id="user_123",
            text="create my_script.py with valid code",
        )
        resp1 = runtime.process_message(msg)
        self.assertEqual(resp1.status, "pending_approval")
        req_id = resp1.metadata["request_id"]

        # Approve
        ok, _, req_obj = approval_service.approve(req_id, "admin_approver", "telegram")
        self.assertTrue(ok)
        self.assertIsNotNone(req_obj)

        # 2. Attempt to verify and consume with a tampered git_push operation
        tampered_op = CanonicalOperation(action_type="git_push", target="origin")
        tampered_digest = tampered_op.compute_digest()

        is_valid, _, err = approval_service.verify_and_consume(
            request_id=req_id,
            expected_digest=tampered_digest,
            session_id=req_obj.session_id,
            channel="telegram",
            requester_id="user_123",
        )
        self.assertFalse(is_valid)
        self.assertIn("operation digest mismatch", str(err).lower())
        self.assertIn("operation substitution prevented", str(err).lower())

        # 3. Contract created from the write_file approval strictly disallows remote git push
        ok, req_obj, _ = approval_service.verify_and_consume(
            req_id, req_obj.operation_digest, req_obj.session_id, "telegram",
            session_incarnation_id=req_obj.session_incarnation_id, requester_id="user_123",
        )
        self.assertTrue(ok)
        contract = ApprovedExecutionContract.from_approval_request(req_obj, repo_dir=self.work_dir)
        self.assertFalse(contract.allows_remote_git_push())
        self.assertTrue(contract.is_remote)

    def test_forged_auto_pr_metadata_cannot_grant_remote_push_permission(self):
        """Incoming message metadata cannot enable auto_pr or remote push for remote channels."""
        store = FileApprovalStore(Path(self.temp_dir) / "approvals")
        approval_service = ApprovalService(store)
        session_mgr = SessionManager()

        runtime = BrainFrogRuntime(
            repo_dir=self.work_dir,
            approval_service=approval_service,
            sessions=session_mgr,
            require_approval=False,  # auto execute
            policy_provider=lambda ch: get_default_policy(ch, allow_code_edits=True),
        )

        msg = IncomingMessage(
            channel="telegram",
            user_id="user_tg_forge",
            text="create config.json with {}",
            metadata={"auto_pr": True, "allow_remote_git_push": True},
        )

        mock_s1, mock_s2 = self._make_mock_systems({"config.json": "{}\n"})
        runtime.system1_factory = lambda backend: mock_s1
        runtime.system2_factory = lambda **kw: mock_s2

        resp = runtime.process_message(msg)

        # Bare remote must have 0 new commits
        self.assertEqual(
            self._get_remote_head_sha(),
            self.initial_remote_sha,
            "Security violation: forged auto_pr metadata triggered git push on remote channel!",
        )

    def test_system1_cannot_authorize_push_during_remote_execution(self):
        """Even when System 1 outputs safe_to_proceed=open_pr with maximum confidence, push is denied."""
        mock_s1 = MagicMock()
        mock_s1.name = "jev_mock"
        mock_s1.decide.return_value = {
            "diff_risk": Answer(choice="low", score=0, noul=0.0, confidence=1.0),
            "safe_to_proceed": Answer(choice="open_pr", score="low", noul=1.0, confidence=1.0),
        }
        mock_s2 = MagicMock()
        mock_s2.draft_pr.return_value = {"title": "feat: test", "body": "test"}

        contract = ApprovedExecutionContract(
            request_id="req_s1_001",
            action_type="write_file",
            approved_targets=frozenset(["safe.py"]),
            operation_digest="d_s1",
            channel="telegram",
            allow_remote_git_push=False,
        )

        cfg = RunConfig(
            task="create safe.py",
            repo_dir=self.work_dir,
            auto_pr=True,  # Even with auto_pr True
            test_command=["python", "-c", "import sys; sys.exit(0)"],
            execution_contract=contract,
            origin_channel="telegram",
            allow_remote_git_push=False,
        )

        orch = Orchestrator(mock_s1, mock_s2, cfg)
        step = PlanStep("1", "safe step", ["safe.py"])
        (self.work_dir / "safe.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "safe.py"], cwd=self.work_dir, check=True)

        res = orch._finalize_pr(step, retries=0, test_output="PASS")

        self.assertEqual(res.outcome, "unverified")
        self.assertEqual(self._get_remote_head_sha(), self.initial_remote_sha)

    # =========================================================================
    # Group D: True End-to-End Verification with Real Bare Git Remote
    # =========================================================================

    def test_e2e_telegram_flow_with_real_bare_git_remote(self):
        """Full E2E verification:
        IncomingMessage (Telegram)
          -> BrainFrogRuntime
          -> Approval Request
          -> Authenticated Approval (/approve)
          -> Execution (/exec)
          -> Canonical Orchestrator
          -> Git Side-Effect Boundary
        Verifies local change succeeds and bare remote receives NO commit.
        """
        store = FileApprovalStore(Path(self.temp_dir) / "approvals")
        approval_service = ApprovalService(store)
        session_mgr = SessionManager()

        mock_s1, mock_s2 = self._make_mock_systems({"lib_tg.py": "def tg_func(): return 'ok'\n"})

        runtime = BrainFrogRuntime(
            repo_dir=self.work_dir,
            approval_service=approval_service,
            sessions=session_mgr,
            system1_factory=lambda b: mock_s1,
            system2_factory=lambda **kw: mock_s2,
            require_approval=True,
        )

        user_id = "tg_dev_1001"
        conv_id = "chat_tg_2002"

        # 1. Incoming Telegram request for code modification
        req_msg = IncomingMessage(
            channel="telegram",
            user_id=user_id,
            conversation_id=conv_id,
            text="write file lib_tg.py with helper functions",
        )
        r1 = runtime.process_message(req_msg)
        self.assertEqual(r1.status, "pending_approval")
        approval_id = r1.metadata["request_id"]

        # 2. Authenticated approval (by separate supervisor to comply with two-man rule)
        app_msg = IncomingMessage(
            channel="telegram",
            user_id="tg_supervisor_admin",
            conversation_id=conv_id,
            text=f"/approve {approval_id}",
        )
        r2 = runtime.process_message(app_msg)
        self.assertTrue(r2.success, f"Approval failed: {r2.error}")

        # 3. Execution via /exec
        exec_msg = IncomingMessage(
            channel="telegram",
            user_id=user_id,
            conversation_id=conv_id,
            text=f"/exec {approval_id}",
        )
        r3 = runtime.process_message(exec_msg)
        self.assertTrue(r3.success, f"Execution failed: {r3.error}")

        # 4. Verify local file change completed successfully
        created_file = self.work_dir / "lib_tg.py"
        self.assertTrue(created_file.exists())
        self.assertIn("tg_func", created_file.read_text(encoding="utf-8"))

        # 5. Verify local git has the new commit
        local_log = subprocess.run(
            ["git", "log", "-n", "1", "--name-only"],
            cwd=self.work_dir,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertNotIn("lib_tg.py", local_log)
        self.assertTrue((self.work_dir / "lib_tg.py").exists())

        # 6. CRITICAL INVARIANT: Bare remote repository received ZERO new commits!
        remote_sha_after = self._get_remote_head_sha()
        self.assertEqual(
            remote_sha_after,
            self.initial_remote_sha,
            f"SECURITY BREACH: Telegram execution pushed to origin! (old: {self.initial_remote_sha}, new: {remote_sha_after})",
        )

    def test_e2e_whatsapp_flow_with_real_bare_git_remote(self):
        """Full E2E verification for WhatsApp channel:
        IncomingMessage (WhatsApp)
          -> BrainFrogRuntime
          -> Approval Request
          -> Authenticated Approval (/approve)
          -> Execution (/exec)
          -> Canonical Orchestrator
          -> Git Side-Effect Boundary
        Verifies local change succeeds and bare remote receives NO commit.
        """
        store = FileApprovalStore(Path(self.temp_dir) / "approvals")
        approval_service = ApprovalService(store)
        session_mgr = SessionManager()

        mock_s1, mock_s2 = self._make_mock_systems({"wa_util.py": "def wa_func(): return 'wa'\n"})

        runtime = BrainFrogRuntime(
            repo_dir=self.work_dir,
            approval_service=approval_service,
            sessions=session_mgr,
            system1_factory=lambda b: mock_s1,
            system2_factory=lambda **kw: mock_s2,
            require_approval=True,
        )

        user_id = "wa_dev_888"
        conv_id = "wa_chat_999"

        # 1. Incoming WhatsApp request
        req_msg = IncomingMessage(
            channel="whatsapp",
            user_id=user_id,
            conversation_id=conv_id,
            text="write file wa_util.py with wa functions",
        )
        r1 = runtime.process_message(req_msg)
        self.assertEqual(r1.status, "pending_approval")
        approval_id = r1.metadata["request_id"]

        # 2. Approval (by separate supervisor to comply with two-man rule)
        app_msg = IncomingMessage(
            channel="whatsapp",
            user_id="wa_supervisor_admin",
            conversation_id=conv_id,
            text=f"/approve {approval_id}",
        )
        r2 = runtime.process_message(app_msg)
        self.assertTrue(r2.success, f"Approval failed: {r2.error}")

        # 3. Execution
        exec_msg = IncomingMessage(
            channel="whatsapp",
            user_id=user_id,
            conversation_id=conv_id,
            text=f"/exec {approval_id}",
        )
        r3 = runtime.process_message(exec_msg)
        self.assertTrue(r3.success, f"Execution failed: {r3.error}")

        # 4. Verify local file change completed
        created_file = self.work_dir / "wa_util.py"
        self.assertTrue(created_file.exists())

        # 5. CRITICAL INVARIANT: Bare remote repository received ZERO new commits!
        remote_sha_after = self._get_remote_head_sha()
        self.assertEqual(
            remote_sha_after,
            self.initial_remote_sha,
            f"SECURITY BREACH: WhatsApp execution pushed to origin! (old: {self.initial_remote_sha}, new: {remote_sha_after})",
        )


if __name__ == "__main__":
    unittest.main()
