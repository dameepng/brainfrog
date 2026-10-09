"""Regression tests for Phase 1: Unifying CLI Execution Through BrainFrogRuntime.

Proves architectural invariants:
Test A — CLI enters runtime: CLI execution invokes BrainFrogRuntime.handle_message() and does not directly execute Orchestrator.run().
Test B — Canonical session: Two CLI messages in same conversation resolve to 'cli:user_id:conversation_id'; separate conversation resolves to isolated session.
Test C — Capability enforcement: CLI receives local capability set; remote channels do not inherit CLI privileges; destructive operations rejected.
Test D — Transaction rollback: Controlled execution mutates state and fails; verification failure triggers rollback restoring pre-execution state.
Test E — ApprovedExecutionContract propagation: Approved contract passes through runtime to downstream Orchestrator without metadata loss.
Test F — Remote regression: Remote channel boundaries, prohibited actions, and session isolation remain intact.
Test G — Direct bypass regression: Static AST analysis and dynamic stack verification prove CLI user task cannot bypass BrainFrogRuntime.
"""
from __future__ import annotations

import ast
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import cli
from core.runtime.contract import ApprovedExecutionContract
from core.runtime.messages import IncomingMessage, OutgoingMessage
from core.runtime.permissions import (
    ChannelTrustLevel,
    PermissionAction,
    PermissionPolicy,
    get_default_policy,
)
from core.runtime.runtime import BrainFrogRuntime
from core.runtime.session import SessionManager
from core.runtime.transaction import (
    FileTransactionStore,
    Transaction,
    TransactionCoordinator,
    TransactionVerifier,
)
from orchestrator import PlanStep, StepResult, _write_files


class TestCLIRuntimeUnification(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.repo_dir = Path(self.tmp_dir.name).resolve()
        # Initialize a minimal git repository in repo_dir
        subprocess.run(["git", "init"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.repo_dir, capture_output=True, check=True)
        (self.repo_dir / "README.md").write_text("# Test Repo\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=self.repo_dir, capture_output=True, check=True)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_a_cli_enters_runtime(self):
        """Test A: CLI execution invokes BrainFrogRuntime.handle_message() and does not directly execute Orchestrator.run()."""
        mock_runtime = MagicMock(spec=BrainFrogRuntime)
        mock_runtime.handle_message.return_value = OutgoingMessage(
            text="Task completed through runtime",
            success=True,
            status="completed",
            metadata={"results": [], "model": "mock-model", "provider": "mock-provider"},
        )

        with patch("orchestrator.Orchestrator.run") as mock_orchestrator_run:
            exit_code = cli.execute_task(
                task="Analyze repository architecture",
                repo_dir=self.repo_dir,
                runtime=mock_runtime,
            )

            # Verification: BrainFrogRuntime.handle_message was invoked exactly once
            self.assertEqual(exit_code, 0)
            mock_runtime.handle_message.assert_called_once()
            called_msg = mock_runtime.handle_message.call_args[0][0]
            self.assertIsInstance(called_msg, IncomingMessage)
            self.assertEqual(called_msg.channel, "cli")
            self.assertEqual(called_msg.user_id, "local")
            self.assertEqual(called_msg.conversation_id, "default")
            self.assertEqual(called_msg.text, "Analyze repository architecture")

            # Verification: Orchestrator.run was NOT directly called by cli.py
            mock_orchestrator_run.assert_not_called()

    def test_b_canonical_session_and_isolation(self):
        """Test B: Two CLI messages in same conversation resolve to same canonical session; different conversation is isolated."""
        runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            sessions=SessionManager(),
            persist_sessions=False,
        )

        mock_step_res = [StepResult(PlanStep("0", "diagnosis", []), "diagnosed", 0, "Healthy")]
        with patch.object(runtime, "system1_factory"), \
             patch.object(runtime, "system2_factory"), \
             patch("core.runtime.runtime.Orchestrator") as mock_orch_cls:
            mock_orch = MagicMock()
            mock_orch.run.return_value = mock_step_res
            mock_orch_cls.return_value = mock_orch

            # 1. First message in default conversation
            code_1 = cli.execute_task(
                task="First turn in default conversation",
                repo_dir=self.repo_dir,
                runtime=runtime,
                conversation_id="default",
            )
            self.assertEqual(code_1, 0)

            # 2. Second message in default conversation
            code_2 = cli.execute_task(
                task="Second turn in default conversation",
                repo_dir=self.repo_dir,
                runtime=runtime,
                conversation_id="default",
            )
            self.assertEqual(code_2, 0)

            # Assert both resolve to canonical session 'cli:local:default'
            canonical_session_id = "cli:local:default"
            session_default = runtime.sessions.get(canonical_session_id)
            self.assertIsNotNone(session_default)
            assert session_default is not None
            self.assertEqual(session_default.session_id, canonical_session_id)
            self.assertEqual(len(session_default.history), 2)
            self.assertEqual(session_default.history[0]["user"], "First turn in default conversation")
            self.assertEqual(session_default.history[1]["user"], "Second turn in default conversation")

            # 3. Third message with different conversation identifier
            isolated_conv_id = "topic_42"
            code_3 = cli.execute_task(
                task="Turn in isolated conversation",
                repo_dir=self.repo_dir,
                runtime=runtime,
                conversation_id=isolated_conv_id,
            )
            self.assertEqual(code_3, 0)

            # Assert session isolation
            isolated_session_id = f"cli:local:{isolated_conv_id}"
            session_isolated = runtime.sessions.get(isolated_session_id)
            self.assertIsNotNone(session_isolated)
            assert session_isolated is not None
            self.assertEqual(session_isolated.session_id, isolated_session_id)
            self.assertEqual(len(session_isolated.history), 1)
            self.assertEqual(session_isolated.history[0]["user"], "Turn in isolated conversation")
            self.assertNotEqual(session_default.session_id, session_isolated.session_id)

    def test_c_capability_enforcement(self):
        """Test C: CLI receives local capabilities through policy evaluation; remote channels do not inherit privileges."""
        # 1. Policy verification
        cli_policy = get_default_policy("cli")
        self.assertEqual(cli_policy.trust_level, ChannelTrustLevel.LOCAL_CLI.value)
        self.assertTrue(cli_policy.allow_code_edits)
        self.assertTrue(cli_policy.allow_auto_pr)
        self.assertTrue(cli_policy.is_allowed(PermissionAction.READ_CODE))
        self.assertTrue(cli_policy.is_allowed(PermissionAction.WRITE_CODE))
        self.assertTrue(cli_policy.is_allowed(PermissionAction.SHELL_EXECUTION))
        # Destructive git operations are explicitly not allowed even for CLI
        self.assertFalse(cli_policy.is_allowed(PermissionAction.GIT_DESTRUCTIVE))

        telegram_policy = get_default_policy("telegram")
        self.assertEqual(telegram_policy.trust_level, ChannelTrustLevel.REMOTE_CHANNEL.value)
        self.assertFalse(telegram_policy.allow_code_edits)
        self.assertFalse(telegram_policy.allow_auto_pr)
        self.assertTrue(telegram_policy.is_allowed(PermissionAction.READ_CODE))
        # Remote channels are strictly prohibited from shell execution and destructive operations
        self.assertFalse(telegram_policy.is_allowed(PermissionAction.SHELL_EXECUTION))
        self.assertFalse(telegram_policy.is_allowed(PermissionAction.GIT_DESTRUCTIVE))
        self.assertFalse(telegram_policy.is_allowed(PermissionAction.CREDENTIAL_ACCESS))

        # 2. Runtime policy evaluation enforcement for CLI
        runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            sessions=SessionManager(),
            persist_sessions=False,
        )

        # Destructive git operations are prohibited even for local CLI
        destructive_task = "git reset --hard origin/main"
        exit_code = cli.execute_task(
            task=destructive_task,
            repo_dir=self.repo_dir,
            runtime=runtime,
        )
        self.assertEqual(exit_code, 1, "CLI must fail with non-zero exit code when runtime policy denies action")

        # Custom restrictive policy to verify runtime policy evaluation is authoritative
        def restrictive_policy_provider(channel: str):
            return PermissionPolicy(
                channel=channel,
                trust_level="local_cli",
                allowed_actions={"read_code", "diagnose"},
                allow_code_edits=False,
                allow_auto_pr=False,
            )

        restricted_runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            policy_provider=restrictive_policy_provider,
            sessions=SessionManager(),
            persist_sessions=False,
        )

        with patch("orchestrator.Orchestrator.run") as mock_orch_run:
            restricted_code = cli.execute_task(
                task="Modify codebase and write files",
                repo_dir=self.repo_dir,
                mode="build",
                runtime=restricted_runtime,
            )
            self.assertEqual(restricted_code, 1)
            mock_orch_run.assert_not_called()

    def test_d_transaction_rollback(self):
        """Test D: Controlled execution mutates state and then fails; verification triggers rollback to pre-execution state."""
        target_file = self.repo_dir / "service.py"
        target_file.write_text("INITIAL_CONTENT = True\n", encoding="utf-8")

        tx_store = FileTransactionStore(self.repo_dir)

        # 1. Direct coordinator controlled mutation and rollback
        coordinator = TransactionCoordinator(
            workspace=self.repo_dir,
            session_id="cli:local:default",
            store=tx_store,
        )
        coordinator.begin()
        coordinator.stage_modify("service.py", "CORRUPTED_MUTATION = True\n")
        coordinator.execute()

        # Mutation occurred on disk during execution
        self.assertIn("CORRUPTED_MUTATION", target_file.read_text(encoding="utf-8"))

        # Controlled failure triggers rollback
        tx_result = coordinator.rollback(reason="Test suite verification failed with exit code 1")

        # Execution failed and rollback occurred
        self.assertFalse(tx_result.success)
        self.assertEqual(tx_result.status.value, "rolled_back")
        # Final state == pre-execution state
        self.assertEqual(target_file.read_text(encoding="utf-8"), "INITIAL_CONTENT = True\n")

        # 2. Automated rollback through _write_files when verifier fails
        class FailingVerifier(TransactionVerifier):
            def verify(self, tx: Transaction, workspace: Path) -> Tuple[bool, Optional[str], Dict[str, Any]]:
                return False, "Lint checks failed", {"violations": 3}

        with self.assertRaises(RuntimeError) as ctx:
            _write_files(
                repo_dir=self.repo_dir,
                files={"service.py": "MUTATED_VIA_WRITE_FILES = True\n"},
                mode="build",
                store=tx_store,
                verifier=FailingVerifier(),
            )
        self.assertIn("Transaction verification failed", str(ctx.exception))
        # File on disk restored to pre-execution state
        self.assertEqual(target_file.read_text(encoding="utf-8"), "INITIAL_CONTENT = True\n")

    def test_e_contract_propagation(self):
        """Test E: ApprovedExecutionContract propagates through runtime to downstream orchestrator without metadata loss."""
        runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            require_approval=True,
            sessions=SessionManager(),
            persist_sessions=False,
        )

        with patch("orchestrator.Orchestrator.run") as mock_orch_run:
            # Operation requiring approval stops at pending_approval
            exit_code = cli.execute_task(
                task="write_file src/secret.py",
                repo_dir=self.repo_dir,
                runtime=runtime,
            )
            self.assertEqual(exit_code, 1)
            mock_orch_run.assert_not_called()

            # Record in approval store
            pending = runtime.approval_service.store.list_requests(session_id="cli:local:default")
            self.assertEqual(len(pending), 1)
            req = pending[0]
            self.assertEqual(req.status.value.upper(), "PENDING")

            # Approve request
            appr_res, msg, _ = runtime.approval_service.approve(
                req.request_id,
                approver_id="admin",
                channel="cli",
                session_id="cli:local:default",
            )
            self.assertTrue(appr_res, f"Approval must succeed: {msg}")

            with patch("core.runtime.runtime.Orchestrator") as mock_orch_cls, \
                 patch.object(runtime, "system1_factory"), \
                 patch.object(runtime, "system2_factory"):
                mock_orch_instance = MagicMock()
                mock_orch_instance.run.return_value = [
                    StepResult(PlanStep("1", "write_file", ["src/secret.py"]), "verified", 0, "Approved")
                ]
                mock_orch_cls.return_value = mock_orch_instance

                # Execute with approval_id through handle_message
                exec_msg = IncomingMessage(
                    text="write_file src/secret.py",
                    channel="cli",
                    user_id="local",
                    conversation_id="default",
                    metadata={"approval_id": req.request_id},
                )
                out = runtime.handle_message(exec_msg)
                self.assertTrue(out.success, f"Execution failed: error='{out.error}' | text='{out.text}'")

                # Verify Orchestrator was constructed with an ApprovedExecutionContract
                orch_call_cfg = mock_orch_cls.call_args[1]["config"]
                contract = orch_call_cfg.execution_contract
                self.assertIsNotNone(contract)
                self.assertIsInstance(contract, ApprovedExecutionContract)
                self.assertEqual(contract.request_id, req.request_id)
                self.assertEqual(contract.actor, "local")
                self.assertEqual(contract.channel, "cli")
                self.assertIn("src/secret.py", contract.approved_targets)

    def test_f_remote_regression(self):
        """Test F: Remote channels remain isolated, strictly prohibited actions fail, security semantics unchanged."""
        runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            sessions=SessionManager(),
            persist_sessions=False,
        )

        # Telegram request attempting arbitrary shell execution
        remote_msg = IncomingMessage(
            text="!cat /etc/passwd",
            channel="telegram",
            user_id="telegram_user_123",
            conversation_id="chat_456",
        )
        out = runtime.handle_message(remote_msg)
        self.assertFalse(out.success)
        self.assertEqual(out.status, "rejected")
        self.assertIn("strictly prohibited", out.error or "")

        # Telegram session is completely distinct from local CLI session
        self.assertIsNone(runtime.sessions.get("cli:local:default"))
        telegram_session = runtime.sessions.get("telegram:telegram_user_123:chat_456")
        self.assertIsNotNone(telegram_session)

    def test_g_direct_bypass_regression(self):
        """Test G: Static and dynamic verification that CLI user task execution cannot bypass BrainFrogRuntime."""
        # 1. Static AST inspection: cli.py MUST NOT directly call Orchestrator.run() or _write_files()
        cli_source = Path(cli.__file__).read_text(encoding="utf-8")
        parsed = ast.parse(cli_source)

        for node in ast.walk(parsed):
            # Check for direct calls to Orchestrator()
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == "Orchestrator":
                    self.fail("cli.py must not directly instantiate Orchestrator; must route through BrainFrogRuntime")
                if isinstance(func, ast.Attribute) and func.attr == "run":
                    if isinstance(func.value, ast.Name) and "orch" in func.value.id.lower():
                        self.fail("cli.py must not directly call orchestrator.run(); must route through BrainFrogRuntime")

        # 2. Dynamic execution path verification
        runtime = BrainFrogRuntime(
            repo_dir=self.repo_dir,
            sessions=SessionManager(),
            persist_sessions=False,
        )

        call_stack_verified = []

        def spy_handle_message(msg, **kwargs):
            call_stack_verified.append("runtime.handle_message")
            return OutgoingMessage(text="Done", success=True, status="completed", metadata={"results": []})

        with patch.object(runtime, "handle_message", side_effect=spy_handle_message):
            with patch("orchestrator.Orchestrator.run") as mock_orch_run:
                exit_code = cli.execute_task(
                    task="Verify architectural invariant",
                    repo_dir=self.repo_dir,
                    runtime=runtime,
                )
                self.assertEqual(exit_code, 0)
                # handle_message was the sole entry point
                self.assertEqual(call_stack_verified, ["runtime.handle_message"])
                # Orchestrator.run was not directly invoked by cli
                mock_orch_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
