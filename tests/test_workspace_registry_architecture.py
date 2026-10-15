"""Tests for BrainFrog Workspace Registry Architecture.

Covers:
1. Installation-scoped WorkspaceRegistry persistence, reloading, unregistration, and fail-closed guarantees.
2. Session-scoped active_workspace isolation across users, chats, and channels.
3. Security bindings: invalidation of incompatible approvals on switch, cross-workspace capability isolation, and recovery target binding.
4. Channel permissions: Telegram/WhatsApp read-only/plan-only enforcement and prohibition of remote registry modification.
5. Local CLI registration (/workspace add) and safe unregistration (/workspace remove).
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from core.runtime.approval import (
    ApprovalRequest,
    ApprovalService,
    ApprovalStatus,
    CanonicalOperation,
    FileApprovalStore,
)
from core.runtime.capabilities import Capabilities, FilesystemPolicy
from core.runtime.contract import ApprovedExecutionContract
from core.runtime.messages import IncomingMessage
from core.runtime.permissions import (
    ChannelTrustLevel,
    PermissionAction,
    evaluate_channel_action,
    get_default_policy,
)
from core.runtime.runtime import BrainFrogRuntime
from core.runtime.session import FileSessionStore, SessionManager, SessionState
from core.runtime.transaction import (
    Transaction,
    TransactionRecoveryManager,
    TransactionStatus,
)
from core.runtime.workspace_lock import WorkspaceLock, WorkspaceLockedError
from core.runtime.workspace_registry import (
    WorkspaceEntry,
    WorkspaceNotFoundError,
    WorkspaceRegistry,
    WorkspaceValidationError,
    validate_workspace_alias,
)


class TestWorkspaceRegistryPersistence(unittest.TestCase):
    """Test installation-scoped WorkspaceRegistry persistence and integrity."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="bf_ws_arch_reg_")).resolve()
        self.bf_dir = self.tmp_dir / ".brainfrog"
        self.bf_dir.mkdir(parents=True)
        self.proj_a = self.tmp_dir / "project_alpha"
        self.proj_a.mkdir(parents=True)
        self.proj_b = self.tmp_dir / "project_beta"
        self.proj_b.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_registered_workspace_survives_reload_and_restart(self) -> None:
        """Registering a workspace with persist=True writes to disk and survives reload."""
        cfg_path = self.bf_dir / "workspaces.json"
        registry1 = WorkspaceRegistry.create_default(repo_dir=self.tmp_dir)
        registry1.register("alpha", self.proj_a, display_name="Alpha Project", persist=True)

        self.assertTrue(cfg_path.exists())
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        aliases = [w["alias"] for w in data.get("workspaces", [])]
        self.assertIn("alpha", aliases)

        # Reload brand new registry instance from disk
        registry2 = WorkspaceRegistry.create_default(repo_dir=self.tmp_dir)
        resolved_alpha = registry2.resolve("alpha")
        self.assertEqual(resolved_alpha, self.proj_a)
        self.assertEqual(registry2.get("alpha").display_name, "Alpha Project")

    def test_two_sessions_resolve_same_registry_entry(self) -> None:
        """Two separate sessions resolve the identical physical canonical path from registry."""
        registry = WorkspaceRegistry.create_default(repo_dir=self.tmp_dir)
        registry.register("alpha", self.proj_a)

        path_for_sess1 = registry.resolve("alpha")
        path_for_sess2 = registry.resolve("alpha")
        self.assertEqual(path_for_sess1, self.proj_a)
        self.assertEqual(path_for_sess1, path_for_sess2)

    def test_duplicate_alias_rejected(self) -> None:
        """Attempting to register a duplicate alias without allow_overwrite fails closed."""
        registry = WorkspaceRegistry.create_default(repo_dir=self.tmp_dir)
        registry.register("alpha", self.proj_a)
        with self.assertRaises(WorkspaceValidationError) as ctx:
            registry.register("alpha", self.proj_b)
        self.assertIn("Duplicate workspace alias", str(ctx.exception))

    def test_malformed_registry_data_fails_closed(self) -> None:
        """Corrupt or non-dict workspaces.json causes WorkspaceValidationError."""
        cfg_path = self.bf_dir / "workspaces.json"
        cfg_path.write_text("NOT_JSON_DATA", encoding="utf-8")
        with self.assertRaises(WorkspaceValidationError):
            WorkspaceRegistry.create_default(repo_dir=self.tmp_dir, config_path=cfg_path)

    def test_removing_alias_does_not_delete_project_directory(self) -> None:
        """Unregistering a workspace removes entry from registry but preserves directory on disk."""
        cfg_path = self.bf_dir / "workspaces.json"
        registry = WorkspaceRegistry.create_default(repo_dir=self.tmp_dir)
        marker_file = self.proj_a / "important_source.py"
        marker_file.write_text("# do not delete", encoding="utf-8")

        registry.register("alpha", self.proj_a, persist=True)
        self.assertIsNotNone(registry.get("alpha"))
        self.assertTrue(cfg_path.exists())

        # Unregister alias
        removed = registry.unregister("alpha", persist=True)
        self.assertTrue(removed)
        self.assertIsNone(registry.get("alpha"))

        # Verify disk directory and files are intact!
        self.assertTrue(self.proj_a.exists())
        self.assertTrue(marker_file.exists())
        self.assertEqual(marker_file.read_text(encoding="utf-8"), "# do not delete")

        # Verify persisted workspaces.json does not contain alpha
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        aliases = [w["alias"] for w in data.get("workspaces", [])]
        self.assertNotIn("alpha", aliases)

    def test_cannot_unregister_default_workspace(self) -> None:
        """Default workspace registration cannot be removed."""
        registry = WorkspaceRegistry.create_default(repo_dir=self.tmp_dir)
        with self.assertRaises(WorkspaceValidationError) as ctx:
            registry.unregister("default")
        self.assertIn("Cannot unregister the default installation workspace", str(ctx.exception))

    def test_missing_paths_never_silently_fallback_to_repo_root(self) -> None:
        """If a session selects an alias whose folder was removed, it raises WorkspaceNotFoundError."""
        registry = WorkspaceRegistry.create_default(repo_dir=self.tmp_dir)
        doomed_dir = self.tmp_dir / "doomed"
        doomed_dir.mkdir(parents=True)
        registry.register("doomed", doomed_dir)

        # Delete directory behind registry's back
        shutil.rmtree(doomed_dir)

        runtime = BrainFrogRuntime(repo_dir=self.tmp_dir, workspace_registry=registry)
        session = runtime.sessions.get_or_create(channel="cli", user_id="local", conversation_id="default")
        session.active_workspace = "doomed"

        with self.assertRaises(WorkspaceNotFoundError) as ctx:
            runtime.get_session_workspace(session)
        self.assertIn("missing on disk", str(ctx.exception))


class TestSessionIsolationAndSwitching(unittest.TestCase):
    """Test session isolation across users, chats, and channel boundaries."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="bf_ws_arch_sess_")).resolve()
        self.proj_a = self.tmp_dir / "brainfrog"
        self.proj_a.mkdir(parents=True)
        self.proj_b = self.tmp_dir / "kosapp"
        self.proj_b.mkdir(parents=True)

        self.registry = WorkspaceRegistry(default_alias="default", base_dir=self.tmp_dir)
        self.registry.register("default", self.tmp_dir)
        self.registry.register("brainfrog", self.proj_a)
        self.registry.register("kosapp", self.proj_b)

        self.runtime = BrainFrogRuntime(
            repo_dir=self.tmp_dir,
            workspace_registry=self.registry,
            persist_sessions=True,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_two_sessions_maintain_distinct_active_workspaces(self) -> None:
        """Session A selects 'brainfrog', Session B selects 'kosapp'; Session A remains on 'brainfrog'."""
        msg_a = IncomingMessage(id="1", channel="telegram", user_id="user_1", conversation_id="chat_1", text="/workspace brainfrog")
        msg_b = IncomingMessage(id="2", channel="telegram", user_id="user_2", conversation_id="chat_2", text="/workspace kosapp")

        resp_a = self.runtime.handle_message(msg_a)
        self.assertTrue(resp_a.success)
        resp_b = self.runtime.handle_message(msg_b)
        self.assertTrue(resp_b.success)

        sess_a = self.runtime.sessions.get("telegram:user_1:chat_1")
        sess_b = self.runtime.sessions.get("telegram:user_2:chat_2")

        self.assertEqual(sess_a.active_workspace, "brainfrog")
        self.assertEqual(sess_b.active_workspace, "kosapp")

        # Session A still resolves to proj_a, Session B resolves to proj_b
        self.assertEqual(self.runtime.get_session_workspace(sess_a), self.proj_a)
        self.assertEqual(self.runtime.get_session_workspace(sess_b), self.proj_b)

    def test_telegram_switch_does_not_mutate_cli_session(self) -> None:
        """Telegram session switching workspace leaves CLI session untouched."""
        cli_sess = self.runtime.sessions.get_or_create(channel="cli", user_id="local", conversation_id="default")
        cli_sess.active_workspace = "brainfrog"
        self.runtime.sessions.save(cli_sess)

        # Telegram user switches to kosapp
        tg_msg = IncomingMessage(id="tg-sw", channel="telegram", user_id="tg_user", conversation_id="tg_chat", text="/workspace kosapp")
        self.runtime.handle_message(tg_msg)

        # Reload CLI session and assert it is still 'brainfrog'
        reloaded_cli = self.runtime.sessions.get("cli:local:default")
        self.assertEqual(reloaded_cli.active_workspace, "brainfrog")
        self.assertEqual(self.runtime.get_session_workspace(reloaded_cli), self.proj_a)

    def test_workspace_current_shows_selection(self) -> None:
        """`/workspace current` returns the current active selection."""
        msg = IncomingMessage(id="curr-1", channel="telegram", user_id="user_x", conversation_id="chat_x", text="/workspace kosapp")
        self.runtime.handle_message(msg)

        msg_curr = IncomingMessage(id="curr-2", channel="telegram", user_id="user_x", conversation_id="chat_x", text="/workspace current")
        resp_curr = self.runtime.handle_message(msg_curr)

        self.assertTrue(resp_curr.success)
        self.assertIn("Active Workspace:** `kosapp`", resp_curr.text)
        self.assertIn(str(self.proj_b), resp_curr.text)


class TestSecurityBindingsAndApprovals(unittest.TestCase):
    """Test security bindings between workspace, approvals, locks, and transactions."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="bf_ws_arch_sec_")).resolve()
        self.proj_a = self.tmp_dir / "workspace_a"
        self.proj_a.mkdir(parents=True)
        self.proj_b = self.tmp_dir / "workspace_b"
        self.proj_b.mkdir(parents=True)

        self.registry = WorkspaceRegistry(default_alias="default", base_dir=self.tmp_dir)
        self.registry.register("default", self.tmp_dir)
        self.registry.register("ws_a", self.proj_a)
        self.registry.register("ws_b", self.proj_b)

        self.runtime = BrainFrogRuntime(
            repo_dir=self.tmp_dir,
            workspace_registry=self.registry,
            persist_sessions=True,
            require_approval=True,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_switching_workspace_invalidates_incompatible_pending_approvals(self) -> None:
        """Switching active workspace from ws_a to ws_b invalidates pending approvals for ws_a."""
        session = self.runtime.sessions.get_or_create(
            channel="telegram", user_id="tester", conversation_id="chat_sec"
        )
        session.active_workspace = "ws_a"
        self.runtime.sessions.save(session)

        # Create a pending approval for ws_a
        req = self.runtime.approval_service.create_request(
            session_id=session.session_id,
            channel="telegram",
            user_id="tester",
            conversation_id="chat_sec",
            operation_type="write_file",
            canonical_operation=CanonicalOperation("write_file", "test.py", {"targets": ["test.py"]}),
            session_incarnation_id=session.session_incarnation_id,
            workspace_root=str(self.proj_a.resolve()),
        )
        self.assertEqual(req.status, ApprovalStatus.PENDING)

        # Switch workspace to ws_b
        switch_msg = IncomingMessage(
            id="sw-sec",
            channel="telegram",
            user_id="tester",
            conversation_id="chat_sec",
            text="/workspace ws_b",
        )
        resp_switch = self.runtime.handle_message(switch_msg)
        self.assertTrue(resp_switch.success)

        # Invariant: Pending approval for ws_a was invalidated
        reloaded_req = self.runtime.approval_service.store.get(req.request_id)
        self.assertIsNotNone(reloaded_req)
        self.assertEqual(reloaded_req.status, ApprovalStatus.CANCELLED)
        self.assertIn("workspace switch", reloaded_req.rejection_reason)

    def test_workspace_locking_isolates_separate_projects(self) -> None:
        """Locking workspace A does not block acquisition of unrelated workspace B."""
        lock_a = WorkspaceLock(self.proj_a, timeout=1.0)
        cap_a = lock_a.acquire()
        self.assertIsNotNone(cap_a)

        # Concurrent attempt on workspace B succeeds cleanly
        lock_b = WorkspaceLock(self.proj_b, timeout=1.0)
        cap_b = lock_b.acquire()
        self.assertIsNotNone(cap_b)

        # Concurrent attempt on workspace A is blocked
        lock_a_conflict = WorkspaceLock(self.proj_a, timeout=0.1)
        with self.assertRaises(WorkspaceLockedError):
            lock_a_conflict.acquire()

        lock_b.release()
        lock_a.release()

    def test_recovery_refuses_mismatched_workspace_target(self) -> None:
        """TransactionRecoveryManager rejects transactions whose recorded workspace does not match manager workspace."""
        tx = Transaction(
            id="tx-cross-ws",
            workspace=str(self.proj_a),
            status=TransactionStatus.ROLLING_BACK,
        )
        self.runtime.transaction_store.save(tx)

        mgr_b = TransactionRecoveryManager(workspace=self.proj_b, store=self.runtime.transaction_store)
        res = mgr_b.recover("tx-cross-ws")
        self.assertFalse(res.recovered)
        self.assertEqual(res.final_status, TransactionStatus.FAILED)
        self.assertIn("workspace mismatch", res.error)


class TestChannelPermissionsAndWorkspaceCommands(unittest.TestCase):
    """Test channel-specific authorization for workspace management commands."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="bf_ws_arch_chan_")).resolve()
        self.bf_dir = self.tmp_dir / ".brainfrog"
        self.bf_dir.mkdir(parents=True)
        self.proj_ext = self.tmp_dir / "external_project"
        self.proj_ext.mkdir(parents=True)

        self.cfg_file = self.bf_dir / "workspaces.json"
        self.registry = WorkspaceRegistry.create_default(repo_dir=self.tmp_dir)

        self.runtime = BrainFrogRuntime(
            repo_dir=self.tmp_dir,
            workspace_registry=self.registry,
            persist_sessions=True,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_telegram_can_list_and_select_workspace(self) -> None:
        """Telegram can list workspaces and select an existing workspace."""
        self.registry.register("ext", self.proj_ext, persist=True)

        # 1. /workspaces
        msg_list = IncomingMessage(id="tg-1", channel="telegram", user_id="tg_u", conversation_id="tg_c", text="/workspaces")
        resp_list = self.runtime.handle_message(msg_list)
        self.assertTrue(resp_list.success)
        self.assertIn("ext", resp_list.text)

        # 2. /workspace ext
        msg_sel = IncomingMessage(id="tg-2", channel="telegram", user_id="tg_u", conversation_id="tg_c", text="/workspace ext")
        resp_sel = self.runtime.handle_message(msg_sel)
        self.assertTrue(resp_sel.success)
        self.assertIn("Workspace switched to `ext`", resp_sel.text)

    def test_telegram_cannot_add_or_remove_workspaces(self) -> None:
        """Telegram is strictly prohibited from running /workspace add or /workspace remove."""
        # Attempt /workspace add from Telegram
        msg_add = IncomingMessage(
            id="tg-add", channel="telegram", user_id="attacker", conversation_id="chat_1",
            text=f"/workspace add hostile {self.proj_ext}"
        )
        resp_add = self.runtime.handle_message(msg_add)
        self.assertFalse(resp_add.success)
        self.assertEqual(resp_add.status, "rejected")
        self.assertIn("restricted to local CLI administrators", resp_add.text)
        self.assertIsNone(self.registry.get("hostile"))

        # Attempt /workspace remove from Telegram
        self.registry.register("ext", self.proj_ext, persist=True)
        msg_rem = IncomingMessage(
            id="tg-rem", channel="telegram", user_id="attacker", conversation_id="chat_1",
            text="/workspace remove ext"
        )
        resp_rem = self.runtime.handle_message(msg_rem)
        self.assertFalse(resp_rem.success)
        self.assertEqual(resp_rem.status, "rejected")
        self.assertIn("restricted to local CLI administrators", resp_rem.text)
        self.assertIsNotNone(self.registry.get("ext"))

    def test_cli_can_add_and_remove_workspaces(self) -> None:
        """Local CLI administrator can add and remove workspaces with automatic persistence."""
        new_proj = self.tmp_dir / "new_app"
        new_proj.mkdir(parents=True)

        # Add from CLI
        msg_add = IncomingMessage(
            id="cli-add", channel="cli", user_id="local", conversation_id="default",
            text=f"/workspace add newapp {new_proj}"
        )
        resp_add = self.runtime.handle_message(msg_add)
        self.assertTrue(resp_add.success)
        self.assertIn("Workspace `newapp` registered", resp_add.text)
        self.assertIsNotNone(self.registry.get("newapp"))

        # Verify disk persistence
        data = json.loads(self.cfg_file.read_text(encoding="utf-8"))
        self.assertIn("newapp", [w["alias"] for w in data.get("workspaces", [])])

        # Remove from CLI
        msg_rem = IncomingMessage(
            id="cli-rem", channel="cli", user_id="local", conversation_id="default",
            text="/workspace remove newapp"
        )
        resp_rem = self.runtime.handle_message(msg_rem)
        self.assertTrue(resp_rem.success)
        self.assertIn("removed", resp_rem.text)
        self.assertIsNone(self.registry.get("newapp"))
        self.assertTrue(new_proj.exists())  # Directory preserved!

    def test_workspace_selection_does_not_grant_remote_write_permission(self) -> None:
        """Selecting a workspace does NOT alter Telegram's remote_channel trust level or permit writes."""
        self.registry.register("ext", self.proj_ext, persist=True)
        session = self.runtime.sessions.get_or_create(channel="telegram", user_id="u", conversation_id="c")
        session.active_workspace = "ext"
        self.runtime.sessions.save(session)

        policy = self.runtime.policy_provider("telegram")
        self.assertEqual(policy.trust_level, ChannelTrustLevel.REMOTE_CHANNEL.value)
        self.assertFalse(policy.is_allowed(PermissionAction.WRITE_CODE))
        self.assertFalse(policy.is_allowed(PermissionAction.SHELL_EXECUTION))
        self.assertFalse(policy.is_allowed(PermissionAction.DEPLOYMENT))
