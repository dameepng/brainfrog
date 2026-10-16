"""Tests for BrainFrog Workspace Selection Phase 1.

Validates:
1. Workspace registry fail-closed behavior (alias validation, missing directory rejection, path containment).
2. Per-session workspace persistence and backward compatibility.
3. Telegram /workspaces and /workspace <alias> commands.
4. Multi-session isolation across workspaces.
5. Telegram unauthorized access prevention (allowlist evaluated before workspace logic).
6. Safe propagation into read-only diagnostics (/doctor) and planning.
7. Telegram write restrictions remain fail-closed (read-only/plan-only).
8. Cross-workspace approval redirection rejection.
9. CLI behavior preservation.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from typing import Any, Dict, List

from orchestrator import PlanStep

from core.channels.telegram import TelegramChannel
from core.runtime.approval import (
    ApprovalRequest,
    ApprovalService,
    ApprovalStatus,
    CanonicalOperation,
    FileApprovalStore,
)
from core.runtime.contract import ApprovedExecutionContract
from core.runtime.gateway import BrainFrogGateway
from core.runtime.messages import IncomingMessage
from core.runtime.permissions import (
    ChannelTrustLevel,
    PermissionAction,
    evaluate_channel_action,
    get_default_policy,
)
from core.runtime.runtime import BrainFrogRuntime
from core.runtime.session import FileSessionStore, SessionManager, SessionState
from core.runtime.workspace_registry import (
    WorkspaceEntry,
    WorkspaceNotFoundError,
    WorkspaceRegistry,
    WorkspaceValidationError,
    validate_workspace_alias,
)
from core.runtime.capabilities import Capabilities, FilesystemPolicy, NetworkPolicy
from tests.test_approval_execution_contract import MockSystem1, MockSystem2
from core.runtime.transaction import (
    OperationStatus,
    OperationType,
    Transaction,
    TransactionOperation,
    TransactionRecoveryManager,
    TransactionStatus,
)
from core.runtime.work_store import Work, WorkStatus


class TestWorkspaceRegistry(unittest.TestCase):
    """Test unit behavior of WorkspaceRegistry."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="bf_ws_reg_test_")).resolve()
        self.clinic_dir = self.tmp_dir / "clinic"
        self.clinic_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_register_and_resolve_valid_workspace(self) -> None:
        registry = WorkspaceRegistry(default_alias="default")
        registry.register("clinic", self.clinic_dir, display_name="Clinic Landing Page")

        entry = registry.get("clinic")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.alias, "clinic")
        self.assertEqual(entry.path, self.clinic_dir.resolve())
        self.assertEqual(entry.display_name, "Clinic Landing Page")

        resolved = registry.resolve("clinic")
        self.assertEqual(resolved, self.clinic_dir.resolve())

    def test_alias_validation_rejects_arbitrary_paths_and_traversal(self) -> None:
        registry = WorkspaceRegistry()
        invalid_aliases = [
            "../secret",
            "../../etc/passwd",
            "C:\\dame-project\\tools\\clinic",
            "/home/user/project",
            "clinic/landing",
            "clinic\\page",
            "clinic landing",
            "clinic*project",
            "clinic:target",
            "",
            "   ",
            "a" * 65,  # Exceeds max length
        ]
        for bad_alias in invalid_aliases:
            with self.subTest(bad_alias=bad_alias):
                with self.assertRaises(WorkspaceValidationError):
                    registry.register(bad_alias, self.clinic_dir)

    def test_missing_directory_fails_closed(self) -> None:
        registry = WorkspaceRegistry()
        non_existent = self.tmp_dir / "does_not_exist"
        with self.assertRaises(WorkspaceNotFoundError):
            registry.register("missing", non_existent, check_exists=True)

    def test_unregistered_alias_resolve_fails_closed(self) -> None:
        registry = WorkspaceRegistry()
        with self.assertRaises(WorkspaceNotFoundError):
            registry.resolve("unregistered_alias")

    def test_create_default_discovers_local_json(self) -> None:
        bf_dir = self.tmp_dir / ".brainfrog"
        bf_dir.mkdir(parents=True)
        config_file = bf_dir / "workspaces.json"
        config_file.write_text(
            json.dumps({
                "workspaces": [
                    {"alias": "clinic", "path": str(self.clinic_dir), "display_name": "Clinic App"}
                ]
            }),
            encoding="utf-8",
        )

        registry = WorkspaceRegistry.create_default(repo_dir=self.tmp_dir)
        self.assertEqual(registry.resolve("default"), self.tmp_dir.resolve())
        self.assertEqual(registry.resolve("clinic"), self.clinic_dir.resolve())


class TestSessionWorkspacePersistence(unittest.TestCase):
    """Test per-session active_workspace persistence and legacy migration."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="bf_sess_test_")).resolve()
        self.store = FileSessionStore(repo_dir=self.tmp_dir)
        self.manager = SessionManager(store=self.store)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_legacy_session_deserializes_with_none_active_workspace(self) -> None:
        legacy_data = {
            "schema_version": 1,
            "session_id": "telegram:user1:chat1",
            "channel": "telegram",
            "user_id": "user1",
            "conversation_id": "chat1",
            "active_mode": "plan",
            "history": [],
            # "active_workspace" intentionally omitted
        }
        session = SessionState.from_dict(legacy_data)
        self.assertIsNone(session.active_workspace)
        self.assertEqual(session.session_id, "telegram:user1:chat1")

    def test_active_workspace_persists_across_session_reload(self) -> None:
        session = self.manager.get_or_create(channel="telegram", user_id="123", conversation_id="chat1")
        session.active_workspace = "clinic"
        self.manager.save(session)

        # Force a fresh reload from disk
        fresh_store = FileSessionStore(repo_dir=self.tmp_dir)
        reloaded = fresh_store.load(session.session_id)
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.active_workspace, "clinic")

    def test_session_reset_preserves_active_workspace(self) -> None:
        session = self.manager.get_or_create(channel="telegram", user_id="123", conversation_id="chat1")
        session.active_workspace = "clinic"
        session.add_message("user", "Hello there")
        self.manager.save(session)

        session.reset()
        self.assertEqual(session.active_workspace, "clinic")
        self.assertEqual(len(session.history), 0)


class TestTelegramWorkspaceCommands(unittest.TestCase):
    """Test Telegram slash commands /workspaces and /workspace <alias>."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="bf_tg_test_")).resolve()
        self.clinic_dir = self.tmp_dir / "clinic_landing_page"
        self.clinic_dir.mkdir(parents=True)

        self.registry = WorkspaceRegistry(default_alias="default")
        self.registry.register("default", self.tmp_dir)
        self.registry.register("clinic", self.clinic_dir, display_name="Clinic Page")

        self.runtime = BrainFrogRuntime(
            repo_dir=self.tmp_dir,
            workspace_registry=self.registry,
            persist_sessions=True,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_workspaces_lists_all_registered_aliases(self) -> None:
        msg = IncomingMessage(
            id="msg-1",
            channel="telegram",
            user_id="tg_user_1",
            conversation_id="chat_1",
            text="/workspaces",
        )
        resp = self.runtime.handle_message(msg)
        self.assertTrue(resp.success)
        self.assertIn("Registered Workspaces", resp.text)
        self.assertIn("• `default`", resp.text)
        self.assertIn("• `clinic`", resp.text)
        self.assertIn("(active)", resp.text)

    def test_workspace_selects_valid_alias(self) -> None:
        msg = IncomingMessage(
            id="msg-2",
            channel="telegram",
            user_id="tg_user_1",
            conversation_id="chat_1",
            text="/workspace clinic",
        )
        resp = self.runtime.handle_message(msg)
        self.assertTrue(resp.success)
        self.assertIn("Workspace switched to `clinic`", resp.text)

        # Check session
        session = self.runtime.sessions.get("telegram:tg_user_1:chat_1")
        self.assertIsNotNone(session)
        self.assertEqual(session.active_workspace, "clinic")

        # Verify status shows selected workspace
        status_msg = IncomingMessage(
            id="msg-3",
            channel="telegram",
            user_id="tg_user_1",
            conversation_id="chat_1",
            text="/status",
        )
        status_resp = self.runtime.handle_message(status_msg)
        self.assertIn("Workspace: `clinic`", status_resp.text)

    def test_workspace_unregistered_alias_fails_closed(self) -> None:
        session_before = self.runtime.sessions.get_or_create(
            channel="telegram", user_id="tg_user_1", conversation_id="chat_1"
        )
        session_before.active_workspace = "default"
        self.runtime.sessions.save(session_before)

        msg = IncomingMessage(
            id="msg-4",
            channel="telegram",
            user_id="tg_user_1",
            conversation_id="chat_1",
            text="/workspace unknown_project",
        )
        resp = self.runtime.handle_message(msg)
        self.assertFalse(resp.success)
        self.assertEqual(resp.status, "rejected")
        self.assertIn("not registered", resp.text)

        # Invariant: Never silently fall back or alter session
        session_after = self.runtime.sessions.get("telegram:tg_user_1:chat_1")
        self.assertEqual(session_after.active_workspace, "default")

    def test_workspace_arbitrary_path_injection_rejected(self) -> None:
        path_injections = [
            "/workspace C:\\dame-project\\tools\\clinic",
            "/workspace /etc/passwd",
            "/workspace ../../../sensitive",
            "/workspace ../clinic",
        ]
        for raw_cmd in path_injections:
            with self.subTest(raw_cmd=raw_cmd):
                msg = IncomingMessage(
                    id="msg-inj",
                    channel="telegram",
                    user_id="tg_user_1",
                    conversation_id="chat_1",
                    text=raw_cmd,
                )
                resp = self.runtime.handle_message(msg)
                self.assertFalse(resp.success)
                self.assertEqual(resp.status, "rejected")
                self.assertIn("Invalid workspace alias", resp.text)


class TestTelegramSecurityInvariants(unittest.TestCase):
    """Test security invariants: authorization allowlist, isolation, and read-only bounds."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="bf_sec_test_")).resolve()
        self.clinic_dir = self.tmp_dir / "clinic"
        self.clinic_dir.mkdir(parents=True)
        self.other_dir = self.tmp_dir / "other"
        self.other_dir.mkdir(parents=True)

        self.registry = WorkspaceRegistry(default_alias="default")
        self.registry.register("default", self.tmp_dir)
        self.registry.register("clinic", self.clinic_dir)
        self.registry.register("other", self.other_dir)

        self.runtime = BrainFrogRuntime(
            repo_dir=self.tmp_dir,
            workspace_registry=self.registry,
            persist_sessions=True,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_unauthorized_telegram_user_cannot_access_workspaces(self) -> None:
        """Allowlist security check runs before handler is reached."""
        channel = TelegramChannel(
            bot_token="123456789:ABCDEF_mock_token_for_tests",
            allowed_users={"1001"},  # Only user 1001 is allowed
        )
        channel.set_handler(self.runtime.handle_message)

        # Unauthorized user update
        unauth_update = {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "from": {"id": 9999, "username": "attacker"},
                "chat": {"id": 9999, "type": "private"},
                "text": "/workspaces",
            },
        }
        with patch.object(channel, "_send_text") as mock_send:
            resp = channel.process_update(unauth_update)
            self.assertIsNotNone(resp)
            self.assertFalse(resp.success)
            self.assertEqual(resp.status, "rejected")
            mock_send.assert_called_once()
            call_text = mock_send.call_args[0][1]
            self.assertIn("Akses Ditolak", call_text)

    def test_two_telegram_sessions_have_strict_workspace_isolation(self) -> None:
        """User A selecting clinic does not alter User B selecting other."""
        msg_a = IncomingMessage(
            id="m-a1",
            channel="telegram",
            user_id="user_a",
            conversation_id="chat_a",
            text="/workspace clinic",
        )
        resp_a = self.runtime.handle_message(msg_a)
        self.assertTrue(resp_a.success)

        msg_b = IncomingMessage(
            id="m-b1",
            channel="telegram",
            user_id="user_b",
            conversation_id="chat_b",
            text="/workspace other",
        )
        resp_b = self.runtime.handle_message(msg_b)
        self.assertTrue(resp_b.success)

        sess_a = self.runtime.sessions.get("telegram:user_a:chat_a")
        sess_b = self.runtime.sessions.get("telegram:user_b:chat_b")

        self.assertEqual(sess_a.active_workspace, "clinic")
        self.assertEqual(sess_b.active_workspace, "other")
        self.assertEqual(self.runtime.get_session_workspace(sess_a), self.clinic_dir.resolve())
        self.assertEqual(self.runtime.get_session_workspace(sess_b), self.other_dir.resolve())

    def test_doctor_diagnostics_inspects_selected_workspace(self) -> None:
        """Doctor command inspects target workspace without touching other workspaces."""
        # Create a unique marker file in clinic
        marker = self.clinic_dir / "clinic_marker.txt"
        marker.write_text("clinic_ok", encoding="utf-8")

        sess = self.runtime.sessions.get_or_create(
            channel="telegram", user_id="user_a", conversation_id="chat_a"
        )
        sess.active_workspace = "clinic"
        self.runtime.sessions.save(sess)

        msg = IncomingMessage(
            id="doc-1",
            channel="telegram",
            user_id="user_a",
            conversation_id="chat_a",
            text="/doctor",
        )
        with patch("core.runtime.doctor.run_doctor_diagnostics") as mock_doctor:
            mock_doctor.return_value = []
            resp = self.runtime.handle_message(msg)
            self.assertTrue(resp.success)
            mock_doctor.assert_called_once_with(self.clinic_dir.resolve())

    def test_telegram_write_requests_remain_fail_closed(self) -> None:
        """Remote channel policy strictly rejects code modification requests."""
        policy = get_default_policy("telegram", allow_code_edits=False)
        self.assertFalse(policy.allow_code_edits)
        allowed, reason = evaluate_channel_action("telegram", PermissionAction.WRITE_CODE, policy)
        self.assertFalse(allowed)
        self.assertIn("read-only", reason)

        msg = IncomingMessage(
            id="write-1",
            channel="telegram",
            user_id="user_a",
            conversation_id="chat_a",
            text="create file test.py and edit it",
        )
        resp = self.runtime.handle_message(msg)
        self.assertFalse(resp.success)
        self.assertEqual(resp.status, "rejected")
        self.assertIn("Permission Denied", resp.text)

    def test_approval_contract_cannot_be_redirected_across_workspace_roots(self) -> None:
        """Contract issued for workspace A cannot be validated against workspace B."""
        from core.runtime.approval import ApprovalService, InMemoryApprovalStore, CanonicalOperation
        from core.runtime.capabilities import Capabilities, FilesystemPolicy

        service = ApprovalService(InMemoryApprovalStore())
        req = service.create_request(
            session_id="telegram:user_a:chat_a",
            channel="telegram",
            user_id="user_a",
            conversation_id="chat_a",
            operation_type="write_file",
            canonical_operation=CanonicalOperation("write_file", "safe.py", {"targets": ["safe.py"]}),
            session_incarnation_id="incarnation1",
            capabilities=Capabilities(filesystem=FilesystemPolicy(write=["safe.py"])),
            workspace_root=str(self.clinic_dir.resolve()),
        )
        ok_appr, _, _ = service.approve(req.request_id, approver_id="user_b", channel="telegram")
        self.assertTrue(ok_appr)

        ok_consume, app_req, _ = service.verify_and_consume(
            request_id=req.request_id,
            expected_digest=req.operation_digest,
            session_id="telegram:user_a:chat_a",
            channel="telegram",
            session_incarnation_id="incarnation1",
            requester_id="user_a",
        )
        self.assertTrue(ok_consume)
        self.assertIsNotNone(app_req)

        contract = ApprovedExecutionContract.from_approval_request(app_req, self.clinic_dir.resolve())

        # Validating against the clinic workspace succeeds
        contract.validate(
            actor="user_a",
            channel="telegram",
            session_id="telegram:user_a:chat_a",
            session_incarnation_id="incarnation1",
            repo_dir=self.clinic_dir.resolve(),
        )

        # Validating against other workspace raises PermissionError
        with self.assertRaises(PermissionError) as ctx:
            contract.validate(
                actor="user_a",
                channel="telegram",
                session_id="telegram:user_a:chat_a",
                session_incarnation_id="incarnation1",
                repo_dir=self.other_dir.resolve(),
            )
        self.assertIn("workspace mismatch", str(ctx.exception).lower())

    def test_cli_explicit_repo_dir_precedence(self) -> None:
        """CLI requests with repo_dir in metadata are respected and take precedence."""
        sess = self.runtime.sessions.get_or_create(
            channel="cli", user_id="local", conversation_id="default"
        )
        sess.active_workspace = "clinic"

        # When CLI explicitly specifies repo_dir in metadata, it takes precedence
        cli_msg = IncomingMessage(
            id="cli-1",
            channel="cli",
            user_id="local",
            conversation_id="default",
            text="status check",
            metadata={"repo_dir": str(self.other_dir)},
        )
        target = self.runtime.get_session_workspace(sess, cli_msg)
        self.assertEqual(target, self.other_dir.resolve())

        # When remote channel (telegram) attempts to supply repo_dir metadata, it is ignored
        remote_msg = IncomingMessage(
            id="rem-1",
            channel="telegram",
            user_id="user_a",
            conversation_id="chat_a",
            text="status check",
            metadata={"repo_dir": str(self.other_dir)},
        )
        target_remote = self.runtime.get_session_workspace(sess, remote_msg)
        self.assertEqual(target_remote, self.clinic_dir.resolve())


class TestGatewayWorkspaceIntegration(unittest.TestCase):
    """Test BrainFrogGateway integration with WorkspaceRegistry."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="bf_gw_ws_test_")).resolve()
        self.clinic_dir = self.tmp_dir / "clinic"
        self.clinic_dir.mkdir(parents=True)

        self.registry = WorkspaceRegistry(default_alias="default")
        self.registry.register("default", self.tmp_dir)
        self.registry.register("clinic", self.clinic_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_gateway_get_status_reports_registered_workspaces(self) -> None:
        gw = BrainFrogGateway(
            repo_dir=self.tmp_dir,
            workspace_registry=self.registry,
            auto_configure_channels=False,
        )
        status = gw.get_status()
        self.assertEqual(status["workspace"], str(self.tmp_dir))
        self.assertEqual(status["registered_workspaces_count"], 2)


class SafeExecutionSystem2(MockSystem2):
    """Deterministic System2 test double implementing scoped plan and execution for safe tests."""

    def _get_target_files(self, task: str) -> List[str]:
        matching = [f for f in self.output_files if f in task]
        return matching if matching else list(self.output_files.keys())[:1]

    def plan_and_prd(self, task: str, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        targets = self._get_target_files(task)
        return {
            "title": "Architecture Plan",
            "markdown_doc": f"# Plan for {task}",
            "problem": task,
            "goals": [task],
            "steps": [{"id": "1", "description": "Execute approved step", "files": targets}],
            "relevant_files": targets,
        }

    def plan_task(self, task: str, *args: Any, **kwargs: Any) -> List[PlanStep]:
        targets = self._get_target_files(task)
        return [PlanStep(id="1", description="Execute approved step", files=targets)]

    def write_code(self, step: Any, task: str, *args: Any, **kwargs: Any) -> Dict[str, str]:
        targets = step.files if hasattr(step, "files") and step.files else self._get_target_files(task)
        return {f: self.output_files.get(f, "# written\n") for f in targets}


class TestFinding1ApprovalContractWorkspaceBinding(unittest.TestCase):
    """Finding 1: Approval request creation, contract minting, and execution contract binding."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="bf_f1_test_")).resolve()
        self.clinic_dir = self.tmp_dir / "clinic"
        self.clinic_dir.mkdir(parents=True)
        self.other_dir = self.tmp_dir / "other"
        self.other_dir.mkdir(parents=True)

        self.registry = WorkspaceRegistry(default_alias="default", base_dir=self.tmp_dir)
        self.registry.register("default", self.tmp_dir)
        self.registry.register("clinic", self.clinic_dir)
        self.registry.register("other", self.other_dir)

        (self.clinic_dir / "test_noop.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n",
            encoding="utf-8",
        )
        self.runtime = BrainFrogRuntime(
            repo_dir=self.tmp_dir,
            workspace_registry=self.registry,
            persist_sessions=True,
            require_approval=True,
            default_test_cmd="python -m unittest test_noop.py",
            system1_factory=lambda _: MockSystem1(),
            system2_factory=lambda **_: SafeExecutionSystem2({
                "app.py": "# modified in clinic\n",
                "lib.py": "# modified lib in clinic\n",
                "common.py": "# modified common in clinic\n",
                "appr.py": "# approved content\n",
            }),
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _create_approved_request(self, workspace_path: Path, filename: str = "main.py") -> ApprovalRequest:
        session = self.runtime.sessions.get_or_create(
            channel="telegram", user_id="user_a", conversation_id="chat_a"
        )
        capabilities = Capabilities(
            filesystem=FilesystemPolicy(read=[filename], write=[filename]),
            network=NetworkPolicy(access=True),
        )
        req = self.runtime.approval_service.create_request(
            session_id=session.session_id,
            channel="telegram",
            user_id="user_a",
            conversation_id="chat_a",
            operation_type="write_file",
            canonical_operation=CanonicalOperation("write_file", filename, {"targets": [filename]}),
            session_incarnation_id=session.session_incarnation_id,
            capabilities=capabilities,
            workspace_root=str(workspace_path.resolve()),
        )
        ok, _, _ = self.runtime.approval_service.approve(req.request_id, approver_id="approver_1", channel="telegram")
        self.assertTrue(ok)
        return req

    def test_approval_created_in_ws_a_cannot_execute_in_ws_b_after_session_switch(self) -> None:
        """When session switches workspace after approval, /exec explicitly rejects mismatch,
        remains unconsumed, and successfully executes upon returning to original workspace A."""
        session = self.runtime.sessions.get_or_create(
            channel="telegram", user_id="user_a", conversation_id="chat_a"
        )
        session.active_workspace = "clinic"
        self.runtime.sessions.save(session)

        req = self._create_approved_request(self.clinic_dir, "app.py")

        # User switches active workspace to 'other'
        switch_msg = IncomingMessage(
            id="sw-1",
            channel="telegram",
            user_id="user_a",
            conversation_id="chat_a",
            text="/workspace other",
        )
        resp_switch = self.runtime.handle_message(switch_msg)
        self.assertTrue(resp_switch.success)
        self.assertEqual(session.active_workspace, "other")

        # Now attempt to execute the approval created for clinic
        exec_msg = IncomingMessage(
            id="ex-1",
            channel="telegram",
            user_id="user_a",
            conversation_id="chat_a",
            text=f"/exec {req.request_id}",
        )
        resp_exec = self.runtime.handle_message(exec_msg)

        # Invariant: Must fail closed and explicitly reject with mismatch diagnostic
        self.assertFalse(resp_exec.success)
        self.assertEqual(resp_exec.status, "rejected")
        self.assertIn("Approval Workspace Mismatch", resp_exec.text)
        self.assertIn("Execution cannot cross workspace boundaries", resp_exec.text)

        # Invariant: Approval was not consumed
        reloaded_req = self.runtime.approval_service.store.get(req.request_id)
        self.assertIsNotNone(reloaded_req)
        self.assertEqual(reloaded_req.status, ApprovalStatus.APPROVED)
        self.assertIsNone(reloaded_req.consumed_at)

        # Switch back to workspace A ('clinic')
        switch_back_msg = IncomingMessage(
            id="sw-back",
            channel="telegram",
            user_id="user_a",
            conversation_id="chat_a",
            text="/workspace clinic",
        )
        resp_switch_back = self.runtime.handle_message(switch_back_msg)
        self.assertTrue(resp_switch_back.success)
        self.assertEqual(session.active_workspace, "clinic")

        # Executes the same request successfully using safe temporary fixtures
        exec_back_msg = IncomingMessage(
            id="ex-back",
            channel="telegram",
            user_id="user_a",
            conversation_id="chat_a",
            text=f"/exec {req.request_id}",
        )
        resp_exec_back = self.runtime.handle_message(exec_back_msg)
        self.assertTrue(resp_exec_back.success)

        # Verifies the request becomes CONSUMED exactly once
        consumed_req = self.runtime.approval_service.store.get(req.request_id)
        self.assertIsNotNone(consumed_req)
        self.assertEqual(consumed_req.status, ApprovalStatus.CONSUMED)
        self.assertIsNotNone(consumed_req.consumed_at)

        # Re-execution attempt fails closed because already consumed
        replay_msg = IncomingMessage(
            id="ex-replay",
            channel="telegram",
            user_id="user_a",
            conversation_id="chat_a",
            text=f"/exec {req.request_id}",
        )
        resp_replay = self.runtime.handle_message(replay_msg)
        self.assertFalse(resp_replay.success)
        self.assertIn("already been consumed", resp_replay.text)

        # Verifies filesystem effects occur only inside A (clinic), never B (other)
        clinic_target = self.clinic_dir / "app.py"
        other_target = self.other_dir / "app.py"
        self.assertTrue(clinic_target.exists())
        self.assertEqual(clinic_target.read_text(encoding="utf-8"), "# modified in clinic\n")
        self.assertFalse(other_target.exists())

    def test_pending_and_approved_requests_coexist_during_workspace_switch(self) -> None:
        """PENDING and APPROVED requests coexist during workspace switch:
        - PENDING request is cancelled and persisted as CANCELLED on disk.
        - APPROVED request remains APPROVED and persisted unconsumed.
        - Switching back to original workspace allows APPROVED request to execute.
        - Cancelled request cannot be approved or executed."""
        session = self.runtime.sessions.get_or_create(
            channel="telegram", user_id="user_a", conversation_id="chat_a"
        )
        session.active_workspace = "clinic"
        self.runtime.sessions.save(session)

        # 1. Create one PENDING request in workspace A
        capabilities_pending = Capabilities(
            filesystem=FilesystemPolicy(read=["pend.py"], write=["pend.py"]),
            network=NetworkPolicy(access=True),
        )
        req_pending = self.runtime.approval_service.create_request(
            session_id=session.session_id,
            channel="telegram",
            user_id="user_a",
            conversation_id="chat_a",
            operation_type="write_file",
            canonical_operation=CanonicalOperation("write_file", "pend.py", {"targets": ["pend.py"]}),
            session_incarnation_id=session.session_incarnation_id,
            capabilities=capabilities_pending,
            workspace_root=str(self.clinic_dir.resolve()),
        )
        self.assertEqual(req_pending.status, ApprovalStatus.PENDING)

        # 2. Create one APPROVED request in workspace A
        capabilities_approved = Capabilities(
            filesystem=FilesystemPolicy(read=["appr.py"], write=["appr.py"]),
            network=NetworkPolicy(access=True),
        )
        req_approved = self.runtime.approval_service.create_request(
            session_id=session.session_id,
            channel="telegram",
            user_id="user_a",
            conversation_id="chat_a",
            operation_type="write_file",
            canonical_operation=CanonicalOperation("write_file", "appr.py", {"targets": ["appr.py"]}),
            session_incarnation_id=session.session_incarnation_id,
            capabilities=capabilities_approved,
            workspace_root=str(self.clinic_dir.resolve()),
        )
        ok_app, _, _ = self.runtime.approval_service.approve(
            req_approved.request_id, approver_id="approver_1", channel="telegram"
        )
        self.assertTrue(ok_app)

        # 3. Switch active workspace to B ('other')
        switch_b_msg = IncomingMessage(
            id="sw-b",
            channel="telegram",
            user_id="user_a",
            conversation_id="chat_a",
            text="/workspace other",
        )
        resp_switch_b = self.runtime.handle_message(switch_b_msg)
        self.assertTrue(resp_switch_b.success)
        self.assertEqual(session.active_workspace, "other")

        # 4. Verify PENDING request becomes CANCELLED and remains cancelled on disk
        stored_pending = self.runtime.approval_service.store.get(req_pending.request_id)
        self.assertIsNotNone(stored_pending)
        self.assertEqual(stored_pending.status, ApprovalStatus.CANCELLED)
        self.assertIn("workspace switch", stored_pending.rejection_reason)

        # 5. Verify APPROVED request remains APPROVED, unconsumed, and persisted
        stored_approved = self.runtime.approval_service.store.get(req_approved.request_id)
        self.assertIsNotNone(stored_approved)
        self.assertEqual(stored_approved.status, ApprovalStatus.APPROVED)
        self.assertIsNone(stored_approved.consumed_at)

        # 6. Switch back to workspace A ('clinic')
        switch_a_msg = IncomingMessage(
            id="sw-a",
            channel="telegram",
            user_id="user_a",
            conversation_id="chat_a",
            text="/workspace clinic",
        )
        resp_switch_a = self.runtime.handle_message(switch_a_msg)
        self.assertTrue(resp_switch_a.success)
        self.assertEqual(session.active_workspace, "clinic")

        # 7. Confirm cancelled request cannot be approved
        ok_late, msg_late, _ = self.runtime.approval_service.approve(
            req_pending.request_id, approver_id="approver_1", channel="telegram"
        )
        self.assertFalse(ok_late)
        self.assertTrue("not in PENDING state" in msg_late or "CANCELLED" in msg_late)

        # 8. Confirm cancelled request cannot be executed
        exec_canc_msg = IncomingMessage(
            id="ex-canc",
            channel="telegram",
            user_id="user_a",
            conversation_id="chat_a",
            text=f"/exec {req_pending.request_id}",
        )
        resp_canc = self.runtime.handle_message(exec_canc_msg)
        self.assertFalse(resp_canc.success)
        self.assertIn("was cancelled", resp_canc.text)

        # 9. Confirm APPROVED request can execute when back in workspace A
        exec_appr_msg = IncomingMessage(
            id="ex-appr",
            channel="telegram",
            user_id="user_a",
            conversation_id="chat_a",
            text=f"/exec {req_approved.request_id}",
        )
        resp_appr = self.runtime.handle_message(exec_appr_msg)
        self.assertTrue(resp_appr.success)

        # 10. Verify APPROVED request becomes CONSUMED on disk
        stored_appr_final = self.runtime.approval_service.store.get(req_approved.request_id)
        self.assertIsNotNone(stored_appr_final)
        self.assertEqual(stored_appr_final.status, ApprovalStatus.CONSUMED)
        self.assertIsNotNone(stored_appr_final.consumed_at)

        # 11. Filesystem assertions: appr.py written to clinic, never to other, pend.py never written
        self.assertTrue((self.clinic_dir / "appr.py").exists())
        self.assertEqual((self.clinic_dir / "appr.py").read_text(encoding="utf-8"), "# approved content\n")
        self.assertFalse((self.other_dir / "appr.py").exists())
        self.assertFalse((self.clinic_dir / "pend.py").exists())
        self.assertFalse((self.other_dir / "pend.py").exists())

    def test_direct_metadata_approval_id_rejects_session_workspace_mismatch(self) -> None:
        """Direct message supplying approval_id metadata fails closed on workspace mismatch."""
        session = self.runtime.sessions.get_or_create(
            channel="telegram", user_id="user_a", conversation_id="chat_a"
        )
        session.active_workspace = "other"
        self.runtime.sessions.save(session)

        req = self._create_approved_request(self.clinic_dir, "lib.py")

        msg = IncomingMessage(
            id="meta-exec",
            channel="telegram",
            user_id="user_a",
            conversation_id="chat_a",
            text="write_file lib.py",
            metadata={"approval_id": req.request_id},
        )
        resp = self.runtime.handle_message(msg)
        self.assertFalse(resp.success)
        self.assertEqual(resp.status, "rejected")
        self.assertIn("Approval Workspace Mismatch", resp.text)

    def test_aliases_resolving_to_same_canonical_path_succeed_safely(self) -> None:
        """Switching between two aliases mapping to the same canonical path succeeds."""
        self.registry.register("clinic_alias", self.clinic_dir)
        session = self.runtime.sessions.get_or_create(
            channel="telegram", user_id="user_a", conversation_id="chat_a"
        )
        session.active_workspace = "clinic"
        self.runtime.sessions.save(session)

        req = self._create_approved_request(self.clinic_dir, "common.py")

        # Switch to alias pointing to identical physical directory
        session.active_workspace = "clinic_alias"
        self.runtime.sessions.save(session)

        exec_msg = IncomingMessage(
            id="ex-same",
            channel="telegram",
            user_id="user_a",
            conversation_id="chat_a",
            text=f"/exec {req.request_id}",
        )
        resp = self.runtime.handle_message(exec_msg)
        # Should not fail with Approval Workspace Mismatch
        self.assertNotIn("Approval Workspace Mismatch", resp.text)


class TestFinding2RecoveryResumeWorkspaceBinding(unittest.TestCase):
    """Finding 2: Recovery and resume workspace binding and fail-closed diagnostics."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="bf_f2_test_")).resolve()
        self.clinic_dir = self.tmp_dir / "clinic"
        self.clinic_dir.mkdir(parents=True)
        self.other_dir = self.tmp_dir / "other"
        self.other_dir.mkdir(parents=True)

        self.registry = WorkspaceRegistry(default_alias="default", base_dir=self.tmp_dir)
        self.registry.register("default", self.tmp_dir)
        self.registry.register("clinic", self.clinic_dir)
        self.registry.register("other", self.other_dir)

        self.runtime = BrainFrogRuntime(
            repo_dir=self.tmp_dir,
            workspace_registry=self.registry,
            persist_sessions=True,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_missing_transaction_workspace_blocks_recovery_fail_closed(self) -> None:
        """Recovering transaction without recorded workspace fails closed."""
        tx = Transaction(
            id="tx-no-ws",
            session_id="telegram:u1:c1",
            workspace="",
            status=TransactionStatus.ROLLING_BACK,
        )
        self.runtime.transaction_store.save(tx)

        msg = IncomingMessage(
            id="rec-1",
            channel="telegram",
            user_id="u1",
            conversation_id="c1",
            text="/recover tx-no-ws",
        )
        resp = self.runtime.handle_message(msg)
        self.assertFalse(resp.success)
        self.assertEqual(resp.status, "rejected")
        self.assertIn("has no recorded workspace", resp.text)

    def test_nonexistent_transaction_workspace_blocks_recovery_fail_closed(self) -> None:
        """Transaction with non-existent workspace directory fails closed."""
        tx = Transaction(
            id="tx-bad-ws",
            session_id="telegram:u1:c1",
            workspace=str(self.tmp_dir / "missing_folder"),
            status=TransactionStatus.ROLLING_BACK,
        )
        self.runtime.transaction_store.save(tx)

        msg = IncomingMessage(
            id="rec-2",
            channel="telegram",
            user_id="u1",
            conversation_id="c1",
            text="/recover tx-bad-ws",
        )
        resp = self.runtime.handle_message(msg)
        self.assertFalse(resp.success)
        self.assertEqual(resp.status, "rejected")
        self.assertIn("does not exist on disk", resp.text)

    def test_mismatched_transaction_workspace_blocks_recovery_manager(self) -> None:
        """TransactionRecoveryManager refuses to recover a transaction recorded for a different workspace."""
        tx = Transaction(
            id="tx-clinic",
            workspace=str(self.clinic_dir),
            status=TransactionStatus.ROLLING_BACK,
        )
        self.runtime.transaction_store.save(tx)

        # Manager initialized with other_dir
        mgr = TransactionRecoveryManager(workspace=self.other_dir, store=self.runtime.transaction_store)
        res = mgr.recover("tx-clinic")
        self.assertFalse(res.recovered)
        self.assertEqual(res.final_status, TransactionStatus.FAILED)
        self.assertIn("workspace mismatch", res.error)

    def test_recovery_in_non_default_workspace_uses_recorded_canonical_target(self) -> None:
        """Recovery accurately rolls back files in recorded target workspace even if active session differs."""
        target_file = self.clinic_dir / "mutated.py"
        target_file.write_text("modified_content", encoding="utf-8")

        op = TransactionOperation(
            id="op-1",
            type=OperationType.MODIFY_FILE,
            target="mutated.py",
            before_state={"exists": True, "content": "original_content", "is_text": True},
            after_state={"exists": True, "content": "modified_content", "is_text": True},
            status=OperationStatus.EXECUTED,
        )
        tx = Transaction(
            id="tx-rollback",
            session_id="telegram:u1:c1",
            workspace=str(self.clinic_dir),
            status=TransactionStatus.EXECUTING,
            operations=[op],
        )
        self.runtime.transaction_store.save(tx)

        # Session is on 'other'
        session = self.runtime.sessions.get_or_create(
            channel="telegram", user_id="u1", conversation_id="c1"
        )
        session.active_workspace = "other"
        self.runtime.sessions.save(session)

        msg = IncomingMessage(
            id="rec-exec",
            channel="telegram",
            user_id="u1",
            conversation_id="c1",
            text="/recover tx-rollback",
        )
        resp = self.runtime.handle_message(msg)
        self.assertTrue(resp.success)
        self.assertIn("recovered (rolled back)", resp.text)

        # Verify target in clinic was rolled back to original_content
        self.assertEqual(target_file.read_text(encoding="utf-8"), "original_content")
        # Verify other workspace or repo root was never touched
        self.assertFalse((self.other_dir / "mutated.py").exists())
        self.assertFalse((self.tmp_dir / "mutated.py").exists())

    def test_resume_without_recorded_workspace_fails_closed(self) -> None:
        """Resume fails closed if work has no recorded workspace."""
        work = Work(
            id="work-no-ws",
            intent="verification test",
            channel="telegram",
            actor_id="u1",
            session_id="telegram:u1:c1",
            status=WorkStatus.VERIFYING,
            resume_metadata={},
        )
        self.runtime.work_store.create(work)

        msg = IncomingMessage(
            id="res-1",
            channel="telegram",
            user_id="u1",
            conversation_id="c1",
            text="/resume work-no-ws",
        )
        resp = self.runtime.handle_message(msg)
        self.assertFalse(resp.success)
        self.assertEqual(resp.status, "rejected")
        self.assertIn("has no recorded workspace", resp.text)

    def test_startup_recovery_fails_closed_for_unresolvable_workspaces(self) -> None:
        """Startup discovery marks unresolvable transactions failed and does not guess workspace."""
        tx = Transaction(
            id="tx-startup-missing",
            workspace="",
            status=TransactionStatus.ROLLING_BACK,
        )
        self.runtime.transaction_store.save(tx)

        results = self.runtime.recover_startup_transactions()
        matching = [r for r in results if r.transaction_id == "tx-startup-missing"]
        self.assertEqual(len(matching), 1)
        self.assertFalse(matching[0].recovered)
        self.assertEqual(matching[0].final_status, TransactionStatus.FAILED)
        self.assertIn("has no recorded workspace", matching[0].error)


class TestFinding3ControlPlaneExclusion(unittest.TestCase):
    """Finding 3: Rejection of control plane .brainfrog directory and its descendants."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="bf_f3_test_")).resolve()
        self.cp_dir = self.tmp_dir / ".brainfrog"
        self.cp_dir.mkdir(parents=True)
        (self.cp_dir / "sessions").mkdir(parents=True)
        (self.cp_dir / "approvals").mkdir(parents=True)

        self.registry = WorkspaceRegistry(default_alias="default", base_dir=self.tmp_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_control_plane_root_registration_rejected(self) -> None:
        """Attempting to register .brainfrog root raises WorkspaceValidationError."""
        with self.assertRaises(WorkspaceValidationError) as ctx:
            self.registry.register("control_plane", self.cp_dir)
        self.assertIn("BrainFrog control plane", str(ctx.exception))

    def test_control_plane_descendants_registration_rejected(self) -> None:
        """Attempting to register .brainfrog/sessions or any child raises WorkspaceValidationError."""
        with self.assertRaises(WorkspaceValidationError) as ctx:
            self.registry.register("cp_sessions", self.cp_dir / "sessions")
        self.assertIn("inside the BrainFrog control plane", str(ctx.exception))

    def test_canonicalization_traversal_cannot_bypass_exclusion(self) -> None:
        """Relative path traversal resolving into .brainfrog is rejected."""
        sub_dir = self.tmp_dir / "subfolder"
        sub_dir.mkdir(parents=True)
        traversal_path = sub_dir / ".." / ".brainfrog"
        with self.assertRaises(WorkspaceValidationError) as ctx:
            self.registry.register("traversal", traversal_path)
        self.assertIn("BrainFrog control plane", str(ctx.exception))

    def test_sibling_path_with_brainfrog_text_is_allowed(self) -> None:
        """Legitimate directory containing '.brainfrog' substring is not rejected."""
        sibling_dir = self.tmp_dir / "my-.brainfrog-plugin"
        sibling_dir.mkdir(parents=True)
        entry = self.registry.register("sibling_plugin", sibling_dir)
        self.assertEqual(entry.path, sibling_dir.resolve())

    def test_repo_root_default_is_allowed(self) -> None:
        """Repository root containing .brainfrog as a child is valid to register."""
        entry = self.registry.register("default", self.tmp_dir, allow_overwrite=True)
        self.assertEqual(entry.path, self.tmp_dir.resolve())


class TestFinding4DeterministicRelativeConfigPaths(unittest.TestCase):
    """Finding 4: Deterministic relative path resolution against gateway repo root and duplicate rejection."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="bf_f4_test_")).resolve()
        self.clinic_dir = self.tmp_dir / "clinic"
        self.clinic_dir.mkdir(parents=True)
        self.other_dir = self.tmp_dir / "other"
        self.other_dir.mkdir(parents=True)

        self.bf_dir = self.tmp_dir / ".brainfrog"
        self.bf_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_relative_paths_resolve_against_repo_root_regardless_of_process_cwd(self) -> None:
        """Relative paths in workspaces.json resolve against repo_dir even when process CWD differs."""
        cfg_file = self.bf_dir / "workspaces.json"
        cfg_file.write_text(
            json.dumps({"workspaces": [{"alias": "clinic", "path": "clinic"}]}),
            encoding="utf-8",
        )

        separate_dir = Path(tempfile.mkdtemp(prefix="bf_cwd_test_")).resolve()
        old_cwd = os.getcwd()
        try:
            os.chdir(str(separate_dir))
            # Load registry with repo_dir anchored at self.tmp_dir
            registry = WorkspaceRegistry.create_default(repo_dir=self.tmp_dir)
            resolved = registry.resolve("clinic")
            # Invariant: Must resolve to self.tmp_dir / clinic, NOT separate_dir / clinic
            self.assertEqual(resolved, self.clinic_dir.resolve())
            self.assertNotEqual(resolved, (separate_dir / "clinic").resolve())
        finally:
            os.chdir(old_cwd)
            shutil.rmtree(separate_dir, ignore_errors=True)

    def test_duplicate_aliases_in_json_fail_closed(self) -> None:
        """Duplicate aliases in configuration payload raise WorkspaceValidationError."""
        registry = WorkspaceRegistry(base_dir=self.tmp_dir)
        bad_config = {
            "workspaces": [
                {"alias": "clinic", "path": str(self.clinic_dir)},
                {"alias": "clinic", "path": str(self.other_dir)},
            ]
        }
        with self.assertRaises(WorkspaceValidationError) as ctx:
            registry.load_from_dict(bad_config)
        self.assertIn("Duplicate workspace alias 'clinic'", str(ctx.exception))

    def test_duplicate_aliases_in_env_workspaces_fail_closed(self) -> None:
        """Duplicate aliases in BRAINFROG_WORKSPACES environment variable raise WorkspaceValidationError."""
        with patch.dict(os.environ, {"BRAINFROG_WORKSPACES": f"clinic={self.clinic_dir};clinic={self.other_dir}"}):
            with self.assertRaises(WorkspaceValidationError) as ctx:
                WorkspaceRegistry.create_default(repo_dir=self.tmp_dir)
            self.assertIn("Duplicate workspace alias 'clinic'", str(ctx.exception))

    def test_malformed_json_config_fails_closed(self) -> None:
        """Malformed JSON file raises WorkspaceValidationError."""
        cfg_file = self.bf_dir / "bad_workspaces.json"
        cfg_file.write_text("{{not valid json", encoding="utf-8")
        registry = WorkspaceRegistry(base_dir=self.tmp_dir)
        with self.assertRaises(WorkspaceValidationError):
            registry.load_from_file(cfg_file)

    def test_malformed_env_workspaces_fails_closed(self) -> None:
        """Malformed entry without equals in BRAINFROG_WORKSPACES raises WorkspaceValidationError."""
        with patch.dict(os.environ, {"BRAINFROG_WORKSPACES": "invalid_alias_entry_without_equals"}):
            with self.assertRaises(WorkspaceValidationError):
                WorkspaceRegistry.create_default(repo_dir=self.tmp_dir)


if __name__ == "__main__":
    unittest.main()
