"""Phase 2A Step 4C.1 — Adversarial Tests for Protected Filesystem Boundaries.

Validates that agent-controlled transaction operations and write-staging paths
cannot modify protected filesystem paths before any filesystem mutation occurs:
- Rejects .git metadata, worktree gitdirs, and hooks.
- Rejects .brainfrog control-plane state from agent-controlled mutations.
- Rejects .env and .env.* files while permitting .env.example.
- Rejects path traversals, absolute escapes, UNC paths, and null bytes.
- Rejects symlink targets and ancestor symlink escapes to outside or protected metadata.
- Enforces zero partial staging and zero partial writes on batch operations.
- Preserves writable access for ordinary source and test files.
- Preserves crash recovery and lock enforcement.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import pytest

from core.runtime.capabilities import Capabilities, FilesystemPolicy, scope_path
from core.runtime.transaction import (
    BasicFilesystemVerifier,
    FileTransactionStore,
    InMemoryTransactionStore,
    OperationStatus,
    OperationType,
    Transaction,
    TransactionCoordinator,
    TransactionOperation,
    TransactionRecoveryManager,
    TransactionStatus,
    validate_protected_target,
)
from core.runtime.workspace_lock import WorkspaceLock
from orchestrator import validate_and_stage_files


class TestProtectedTargetValidator(unittest.TestCase):
    """Direct unit tests for validate_protected_target security invariants."""

    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="bf_test_4c1_"))
        self.workspace = self.temp_dir / "workspace"
        self.workspace.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_empty_whitespace_and_null_byte_denied(self) -> None:
        """Empty strings, whitespace, non-strings, and null bytes are rejected fail-closed."""
        for invalid in ("", "   ", "\t\n", None, 123, ["path"]):
            with self.assertRaises(PermissionError):
                validate_protected_target(invalid, self.workspace)  # type: ignore

        with self.assertRaises(PermissionError) as ctx:
            validate_protected_target("src/main.py\x00.exe", self.workspace)
        self.assertIn("null byte", str(ctx.exception).lower())

    def test_path_traversal_escaping_workspace_denied(self) -> None:
        """Path traversal forms (.., nested .., absolute escapes, UNC) are rejected."""
        traversal_cases = [
            "../outside.txt",
            "..\\outside.txt",
            "sub/../../escape.txt",
            "sub/../sub/../../outside.txt",
            str(self.temp_dir / "outside.txt"),
            "C:\\Windows\\System32\\cmd.exe",
            "/etc/passwd",
            "//server/share/evil.txt",
            "\\\\server\\share\\evil.txt",
        ]
        for target in traversal_cases:
            with self.assertRaises(PermissionError, msg=f"Should reject traversal: {target}"):
                validate_protected_target(target, self.workspace)

    def test_workspace_root_itself_denied(self) -> None:
        """Workspace root itself cannot be targeted as a file mutation."""
        for target in (".", "./", ".\\", "", str(self.workspace)):
            with self.assertRaises(PermissionError):
                validate_protected_target(target, self.workspace)

    def test_directory_targeting_denied_when_file_expected(self) -> None:
        """Directory targets or paths ending with slashes are rejected for file mutations."""
        sub_dir = self.workspace / "existing_dir"
        sub_dir.mkdir()

        with self.assertRaises(PermissionError):
            validate_protected_target("existing_dir", self.workspace)

        with self.assertRaises(PermissionError):
            validate_protected_target("new_dir/", self.workspace)

    def test_git_metadata_and_hooks_strictly_denied(self) -> None:
        """Internal .git directory, hooks, config, and HEAD are strictly rejected across cases."""
        git_dir = self.workspace / ".git"
        git_dir.mkdir()
        (git_dir / "hooks").mkdir()
        (git_dir / "config").write_text("[core]\n", encoding="utf-8")

        denied_git_targets = [
            ".git",
            ".git/",
            ".git/hooks",
            ".git/hooks/pre-commit",
            ".git/hooks/post-commit",
            ".git/config",
            ".git/HEAD",
            ".git/info/exclude",
            ".GIT/hooks/pre-commit",
            ".Git/Config",
            ".git\\hooks\\pre-commit",
            "./.git/hooks/pre-commit",
            "sub/../.git/hooks/pre-commit",
            f"{self.workspace.name}/.git/hooks/pre-commit",
        ]
        for target in denied_git_targets:
            with self.assertRaises(PermissionError, msg=f"Failed to reject: {target}"):
                validate_protected_target(target, self.workspace)

    def test_git_worktree_file_metadata_strictly_denied(self) -> None:
        """Worktree/submodule gitdir locations are rejected when .git is a file."""
        outside_gitdir = self.temp_dir / "main_repo_gitdir"
        outside_gitdir.mkdir()
        (outside_gitdir / "hooks").mkdir()

        # Simulate git worktree where .git is a file containing "gitdir: <path>"
        dot_git_file = self.workspace / ".git"
        dot_git_file.write_text(f"gitdir: {outside_gitdir.resolve()}\n", encoding="utf-8")

        # Directly targeting outside gitdir or relative resolution
        with self.assertRaises(PermissionError):
            validate_protected_target(str(outside_gitdir / "hooks/pre-commit"), self.workspace)

        with self.assertRaises(PermissionError):
            validate_protected_target(".git", self.workspace)

    def test_brainfrog_control_plane_strictly_denied(self) -> None:
        """Internal .brainfrog control-plane state is strictly rejected from agent mutations."""
        denied_bf_targets = [
            ".brainfrog",
            ".brainfrog/proofs",
            ".brainfrog/proofs/forged.json",
            ".brainfrog/transactions/fake.json",
            ".brainfrog/sessions/session.json",
            ".brainfrog/approvals/req.json",
            ".brainfrog/lock",
            ".brainfrog/lock.meta",
            ".BRAINFROG/proofs/forged.json",
            ".BrainFrog/lock",
            "./.brainfrog/proofs/forged.json",
            "sub/../.brainfrog/lock",
        ]
        for target in denied_bf_targets:
            with self.assertRaises(PermissionError, msg=f"Failed to reject: {target}"):
                validate_protected_target(target, self.workspace)

    def test_credential_directories_denied(self) -> None:
        """Sensitive credential and agent customization directories are rejected."""
        credential_targets = [
            ".aws/credentials",
            ".aws/config",
            ".ssh/id_rsa",
            ".ssh/authorized_keys",
            ".gnupg/secring.gpg",
            "sub/.ssh/id_rsa",
            ".agents/rules",
            ".agents/skills/hack.md",
            ".AGENTS/rules",
            "sub/.agents/skills",
            ".codex/config.json",
            ".CODEX/settings",
        ]
        for target in credential_targets:
            with self.assertRaises(PermissionError, msg=f"Failed to reject: {target}"):
                validate_protected_target(target, self.workspace)

    def test_env_files_policy_enforcement(self) -> None:
        """Rejects .env and .env.* while permitting .env.example."""
        forbidden_env = [
            ".env",
            ".env.local",
            ".env.production",
            ".env.staging",
            ".env.test",
            ".ENV",
            ".env.PRODUCTION",
            ".env.example.bak",
            "frontend/.env",
            "backend/.env.production",
            "nested/deep/.env.secret",
        ]
        for target in forbidden_env:
            with self.assertRaises(PermissionError, msg=f"Should reject env target: {target}"):
                validate_protected_target(target, self.workspace)

        # Explicit narrowly scoped exception: .env.example is permitted
        allowed_example = [
            ".env.example",
            ".ENV.EXAMPLE",
            "frontend/.env.example",
            "backend/.env.example",
        ]
        for target in allowed_example:
            res = validate_protected_target(target, self.workspace)
            self.assertTrue(res.is_relative_to(self.workspace.resolve()))

    def test_windows_reserved_device_names_denied(self) -> None:
        """Windows reserved device names (CON, NUL, AUX, PRN, COM1, LPT1) are rejected."""
        reserved_targets = [
            "con", "CON", "con.txt", "nul", "NUL.dat", "aux.py", "prn", "com1.log", "lpt1.txt",
            "sub/con.txt", "nested/NUL",
        ]
        for target in reserved_targets:
            with self.assertRaises(PermissionError, msg=f"Should reject reserved device: {target}"):
                validate_protected_target(target, self.workspace)

    def test_symlink_redirection_denied(self) -> None:
        """Symlinks and symlinked ancestors redirecting outside or to protected paths are denied."""
        # 1. Symlink pointing outside workspace
        outside_file = self.temp_dir / "outside_secret.txt"
        outside_file.write_text("secret", encoding="utf-8")
        symlink_outside = self.workspace / "symlink_outside.txt"
        try:
            symlink_outside.symlink_to(outside_file)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation not permitted in this environment")

        with self.assertRaises(PermissionError):
            validate_protected_target("symlink_outside.txt", self.workspace)

        # 2. Symlink pointing to .git
        dot_git = self.workspace / ".git"
        dot_git.mkdir(exist_ok=True)
        symlink_git = self.workspace / "symlink_git"
        try:
            symlink_git.symlink_to(dot_git)
        except (OSError, NotImplementedError):
            pass
        else:
            with self.assertRaises(PermissionError):
                validate_protected_target("symlink_git/hooks/pre-commit", self.workspace)

        # 3. Symlink pointing to .env
        env_file = self.workspace / "real.env"
        env_file.write_text("KEY=val", encoding="utf-8")
        symlink_env = self.workspace / ".env"
        try:
            symlink_env.symlink_to(env_file)
        except (OSError, NotImplementedError):
            pass
        else:
            with self.assertRaises(PermissionError):
                validate_protected_target(".env", self.workspace)

    def test_windows_directory_junction_redirection_denied(self) -> None:
        """Windows directory junctions redirecting outside or to protected paths are denied."""
        if os.name != "nt":
            self.skipTest("Directory junction tests require Windows platform (os.name == 'nt')")

        try:
            import _winapi  # type: ignore[import-not-found]
        except ImportError:
            self.skipTest("Windows _winapi module is not available in this environment")

        outside_dir = self.temp_dir / "outside_junction_dir"
        outside_dir.mkdir()
        (outside_dir / "secret.txt").write_text("secret_data", encoding="utf-8")

        # 1. Directory junction pointing outside workspace
        junction_outside = self.workspace / "junc_outside"
        try:
            _winapi.CreateJunction(str(outside_dir.resolve()), str(junction_outside.resolve()))
        except Exception as exc:
            self.skipTest(f"Failed to create directory junction: {exc}")

        with self.assertRaises(PermissionError):
            validate_protected_target("junc_outside/secret.txt", self.workspace)

        # 2. Directory junction pointing to .git
        dot_git = self.workspace / ".git"
        dot_git.mkdir(exist_ok=True)
        (dot_git / "hooks").mkdir(exist_ok=True)
        junction_git = self.workspace / "junc_git"
        try:
            _winapi.CreateJunction(str(dot_git.resolve()), str(junction_git.resolve()))
        except Exception as exc:
            self.skipTest(f"Failed to create git directory junction: {exc}")

        with self.assertRaises(PermissionError):
            validate_protected_target("junc_git/hooks/pre-commit", self.workspace)

    def test_ordinary_source_and_test_files_permitted(self) -> None:
        """Legitimate source, test, doc, and config files resolve successfully."""
        safe_targets = [
            "src/main.py",
            "src/utils/math.py",
            "tests/test_main.py",
            "README.md",
            "package.json",
            "Cargo.toml",
            "docs/architecture.md",
            ".env.example",
        ]
        for target in safe_targets:
            resolved = validate_protected_target(target, self.workspace)
            self.assertEqual(resolved, (self.workspace.resolve() / target).resolve())


class TestTransactionProtectedBoundaries(unittest.TestCase):
    """Verifies that TransactionCoordinator rejects protected targets across all operations."""

    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="bf_test_tx_boundaries_"))
        self.workspace = self.temp_dir / "workspace"
        self.workspace.mkdir()
        self.dot_git = self.workspace / ".git"
        self.dot_git.mkdir()
        self.hooks_dir = self.dot_git / "hooks"
        self.hooks_dir.mkdir()
        self.store = InMemoryTransactionStore()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_stage_create_git_hook_denied_zero_bytes_written(self) -> None:
        """stage_create targeting .git/hooks/pre-commit is denied and writes zero bytes."""
        hook_path = self.hooks_dir / "pre-commit"
        self.assertFalse(hook_path.exists())

        with WorkspaceLock(self.workspace, session_id="s1", task_id="t1"):
            coord = TransactionCoordinator(self.workspace, store=self.store, task_id="t1", session_id="s1")
            coord.begin()

            with self.assertRaises(PermissionError) as ctx:
                coord.stage_create(".git/hooks/pre-commit", "#!/bin/sh\ncalc.exe\n")
            self.assertIn("protected path", str(ctx.exception).lower())

            # Verify zero bytes written
            self.assertFalse(hook_path.exists())
            # Verify transaction operation list remained empty
            self.assertEqual(len(coord.tx.operations), 0)

    def test_stage_modify_git_config_denied(self) -> None:
        """stage_modify targeting .git/config is denied."""
        config_path = self.dot_git / "config"
        config_path.write_text("[core]\n", encoding="utf-8")

        with WorkspaceLock(self.workspace, session_id="s1", task_id="t1"):
            coord = TransactionCoordinator(self.workspace, store=self.store, task_id="t1", session_id="s1")
            coord.begin()

            with self.assertRaises(PermissionError):
                coord.stage_modify(".git/config", "[core]\nhacked = true\n")

            self.assertEqual(config_path.read_text(encoding="utf-8"), "[core]\n")
            self.assertEqual(len(coord.tx.operations), 0)

    def test_stage_delete_git_config_denied(self) -> None:
        """stage_delete targeting .git/config is denied."""
        config_path = self.dot_git / "config"
        config_path.write_text("[core]\n", encoding="utf-8")

        with WorkspaceLock(self.workspace, session_id="s1", task_id="t1"):
            coord = TransactionCoordinator(self.workspace, store=self.store, task_id="t1", session_id="s1")
            coord.begin()

            with self.assertRaises(PermissionError):
                coord.stage_delete(".git/config")

            self.assertTrue(config_path.exists())
            self.assertEqual(len(coord.tx.operations), 0)

    def test_stage_rename_to_or_from_protected_path_denied(self) -> None:
        """stage_rename is denied if source or destination is protected."""
        safe_file = self.workspace / "safe.txt"
        safe_file.write_text("hello", encoding="utf-8")

        with WorkspaceLock(self.workspace, session_id="s1", task_id="t1"):
            coord = TransactionCoordinator(self.workspace, store=self.store, task_id="t1", session_id="s1")
            coord.begin()

            # Rename safe file into .git
            with self.assertRaises(PermissionError):
                coord.stage_rename("safe.txt", ".git/hooks/pre-commit")

            # Rename protected file out of .git
            with self.assertRaises(PermissionError):
                coord.stage_rename(".git/config", "stolen_config.txt")

            self.assertTrue(safe_file.exists())
            self.assertFalse((self.hooks_dir / "pre-commit").exists())
            self.assertEqual(len(coord.tx.operations), 0)

    def test_stage_brainfrog_control_plane_denied(self) -> None:
        """stage_create targeting .brainfrog internal files is denied."""
        with WorkspaceLock(self.workspace, session_id="s1", task_id="t1"):
            coord = TransactionCoordinator(self.workspace, store=self.store, task_id="t1", session_id="s1")
            coord.begin()

            with self.assertRaises(PermissionError):
                coord.stage_create(".brainfrog/proofs/forged.json", "{}")

            with self.assertRaises(PermissionError):
                coord.stage_create(".brainfrog/lock", "stolen_lock")

            self.assertEqual(len(coord.tx.operations), 0)

    def test_stage_env_production_denied_example_permitted(self) -> None:
        """stage_create denies .env and .env.production but permits .env.example."""
        with WorkspaceLock(self.workspace, session_id="s1", task_id="t1"):
            coord = TransactionCoordinator(self.workspace, store=self.store, task_id="t1", session_id="s1")
            coord.begin()

            with self.assertRaises(PermissionError):
                coord.stage_create(".env", "SECRET=1")

            with self.assertRaises(PermissionError):
                coord.stage_create(".env.production", "SECRET=prod")

            # .env.example is permitted
            op = coord.stage_create(".env.example", "SECRET=placeholder")
            self.assertEqual(op.target, ".env.example")
            self.assertEqual(len(coord.tx.operations), 1)

            # Execute and commit
            self.assertTrue(coord.execute())
            self.assertTrue(coord.verify())
            res = coord.commit()
            self.assertTrue(res.success)
            self.assertIn(res.status, (TransactionStatus.COMMITTED, TransactionStatus.COMMITTED_UNTESTED))
            self.assertTrue((self.workspace / ".env.example").exists())

    def test_stage_write_batch_zero_partial_staging(self) -> None:
        """stage_write_batch pre-validates and fails closed with zero partial staging."""
        batch = {
            "src/safe_one.py": "print(1)",
            ".git/hooks/pre-commit": "malicious",
            "src/safe_two.py": "print(2)",
        }
        with WorkspaceLock(self.workspace, session_id="s1", task_id="t1"):
            coord = TransactionCoordinator(self.workspace, store=self.store, task_id="t1", session_id="s1")
            coord.begin()

            with self.assertRaises(PermissionError):
                coord.stage_write_batch(batch)

            # Assert ZERO operations were staged
            self.assertEqual(len(coord.tx.operations), 0)
            self.assertFalse((self.workspace / "src/safe_one.py").exists())
            self.assertFalse((self.hooks_dir / "pre-commit").exists())

    def test_execute_defense_in_depth_blocks_unvalidated_operations(self) -> None:
        """execute() re-validates targets before mutation and aborts if protected."""
        hook_path = self.hooks_dir / "pre-commit"

        with WorkspaceLock(self.workspace, session_id="s1", task_id="t1"):
            coord = TransactionCoordinator(self.workspace, store=self.store, task_id="t1", session_id="s1")
            coord.begin()

            # Manually inject an unvalidated operation bypassing stage_create
            forged_op = TransactionOperation(
                type=OperationType.CREATE_FILE,
                target=".git/hooks/pre-commit",
                status=OperationStatus.STAGED,
                after_state={"is_text": True, "content": "#!/bin/sh\ncalc.exe\n"},
            )
            coord.tx.operations.append(forged_op)

            # execute() must detect the protected path, fail the transaction, and write zero bytes
            success = coord.execute()
            self.assertFalse(success)
            self.assertEqual(coord.tx.status, TransactionStatus.ROLLED_BACK)
            self.assertIn("protected path", str(coord.tx.failure_reason).lower())
            self.assertFalse(hook_path.exists())

    def test_ordinary_file_transaction_lifecycle_succeeds(self) -> None:
        """Normal multi-file transaction creation, verification, and commit succeeds."""
        with WorkspaceLock(self.workspace, session_id="s1", task_id="t1"):
            coord = TransactionCoordinator(self.workspace, store=self.store, task_id="t1", session_id="s1")
            coord.begin()
            coord.stage_create("src/app.py", "def run(): return True")
            coord.stage_create("tests/test_app.py", "def test_run(): pass")

            self.assertTrue(coord.execute())
            self.assertTrue(coord.verify())
            res = coord.commit()

            self.assertTrue(res.success)
            self.assertIn(res.status, (TransactionStatus.COMMITTED, TransactionStatus.COMMITTED_UNTESTED))
            self.assertTrue((self.workspace / "src/app.py").exists())
            self.assertTrue((self.workspace / "tests/test_app.py").exists())


class TestOrchestratorStagingBoundary(unittest.TestCase):
    """Verifies that orchestrator.validate_and_stage_files enforces protected target policy."""

    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="bf_test_orch_staging_"))
        self.repo_dir = self.temp_dir / "repo"
        self.repo_dir.mkdir()
        (self.repo_dir / ".git").mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_validate_and_stage_files_rejects_git_hooks(self) -> None:
        """validate_and_stage_files rejects .git/hooks mutations."""
        with self.assertRaises(PermissionError):
            validate_and_stage_files(self.repo_dir, {".git/hooks/pre-commit": "evil"})

    def test_validate_and_stage_files_rejects_env_production(self) -> None:
        """validate_and_stage_files rejects .env and .env.production."""
        with self.assertRaises(PermissionError):
            validate_and_stage_files(self.repo_dir, {".env.production": "DB_PASS=xyz"})

    def test_validate_and_stage_files_permits_env_example(self) -> None:
        """validate_and_stage_files permits .env.example."""
        staged = validate_and_stage_files(self.repo_dir, {".env.example": "DB_PASS=example"})
        self.assertEqual(len(staged), 1)
        self.assertEqual(staged[0][0], (self.repo_dir / ".env.example").resolve())

    def test_validate_and_stage_files_zero_partial_staging(self) -> None:
        """validate_and_stage_files fails closed without partial returns."""
        files = {
            "src/clean.py": "print('ok')",
            ".git/config": "[core]\n",
        }
        with self.assertRaises(PermissionError):
            validate_and_stage_files(self.repo_dir, files)


class TestCapabilitiesScopePolicy(unittest.TestCase):
    """Verifies that capabilities.scope_path aligns with protected target policy."""

    def test_scope_path_rejects_env_files_permits_example(self) -> None:
        """scope_path rejects .env, .env.production, credentials, and control directories but permits .env.example."""
        with self.assertRaises(ValueError):
            scope_path(".env")

        with self.assertRaises(ValueError):
            scope_path(".env.production")

        with self.assertRaises(ValueError):
            scope_path(".git/hooks/pre-commit")

        with self.assertRaises(ValueError):
            scope_path(".brainfrog/proofs/x.json")

        with self.assertRaises(ValueError):
            scope_path(".agents/rules/custom.md")

        with self.assertRaises(ValueError):
            scope_path(".codex/config.json")

        with self.assertRaises(ValueError):
            scope_path(".ssh/id_rsa")

        with self.assertRaises(ValueError):
            scope_path(".gnupg/secring.gpg")

        with self.assertRaises(ValueError):
            scope_path("nested/.ssh/key")

        self.assertEqual(scope_path(".env.example"), ".env.example")
        self.assertEqual(scope_path("src/app.py"), "src/app.py")


class TestRecoveryProtectedTargetRejection(unittest.TestCase):
    """Verifies that TransactionRecoveryManager fails closed if a journal targets a protected path."""

    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="bf_test_recovery_sec_"))
        self.workspace = self.temp_dir / "workspace"
        self.workspace.mkdir()
        self.dot_git = self.workspace / ".git"
        self.dot_git.mkdir()
        self.hooks_dir = self.dot_git / "hooks"
        self.hooks_dir.mkdir()
        self.store = FileTransactionStore(self.workspace)
        self.recovery = TransactionRecoveryManager(self.workspace, self.store)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_recovery_refuses_to_restore_protected_path(self) -> None:
        """Crash recovery aborts fail-closed if an uncommitted operation targets .git."""
        tx = Transaction(
            id="tx_malicious_forged",
            workspace=str(self.workspace),
            status=TransactionStatus.EXECUTING,
        )
        op = TransactionOperation(
            id="op_malicious",
            type=OperationType.CREATE_FILE,
            target=".git/hooks/pre-commit",
            status=OperationStatus.PREPARED,
            after_state={"is_text": True, "content": "malicious"},
            before_state={"exists": False},
        )
        tx.operations.append(op)
        self.store.save(tx)

        result = self.recovery.recover("tx_malicious_forged")
        self.assertFalse(result.recovered)
        self.assertEqual(result.final_status, TransactionStatus.FAILED)
        self.assertTrue(result.ambiguous)
        self.assertIn("protected path", str(result.error).lower())

        # Ensure no hook was created
        self.assertFalse((self.hooks_dir / "pre-commit").exists())

    def test_recovery_rename_new_target_git_hooks_denied_zero_mutation(self) -> None:
        """Recovery rejects rename operations where new_target points to .git/hooks, with zero mutations."""
        hook_path = self.hooks_dir / "pre-commit"
        hook_path.write_text("#!/bin/sh\nlegit_hook\n", encoding="utf-8")
        exfil_path = self.workspace / "exfiltrated_hook.txt"

        tx = Transaction(
            id="tx_rename_git_attack",
            workspace=str(self.workspace),
            status=TransactionStatus.EXECUTING,
        )
        op = TransactionOperation(
            id="op_rename_git",
            type=OperationType.RENAME_FILE,
            target="exfiltrated_hook.txt",
            new_target=".git/hooks/pre-commit",
            status=OperationStatus.EXECUTED,
            before_state={"exists": True, "is_text": True, "content": "#!/bin/sh\nlegit_hook\n"},
            after_state={"new_target": ".git/hooks/pre-commit"},
        )
        tx.operations.append(op)
        self.store.save(tx)

        res = self.recovery.recover("tx_rename_git_attack")
        self.assertFalse(res.recovered)
        self.assertEqual(res.final_status, TransactionStatus.FAILED)
        self.assertTrue(res.ambiguous)
        self.assertIn("protected destination", str(res.error).lower())

        # Assert protected hook was NOT moved or deleted, and exfiltrated file was NOT created
        self.assertTrue(hook_path.exists())
        self.assertEqual(hook_path.read_text(encoding="utf-8"), "#!/bin/sh\nlegit_hook\n")
        self.assertFalse(exfil_path.exists())

    def test_recovery_rename_new_target_env_denied_zero_mutation(self) -> None:
        """Recovery rejects rename operations targeting .env as destination, preserving secret."""
        env_path = self.workspace / ".env"
        env_path.write_text("DATABASE_PASSWORD=supersecret\n", encoding="utf-8")
        stolen_path = self.workspace / "stolen_env.txt"

        tx = Transaction(
            id="tx_rename_env_attack",
            workspace=str(self.workspace),
            status=TransactionStatus.EXECUTING,
        )
        op = TransactionOperation(
            id="op_rename_env",
            type=OperationType.RENAME_FILE,
            target="stolen_env.txt",
            new_target=".env",
            status=OperationStatus.EXECUTED,
            before_state={"exists": True, "is_text": True, "content": "DATABASE_PASSWORD=supersecret\n"},
            after_state={"new_target": ".env"},
        )
        tx.operations.append(op)
        self.store.save(tx)

        res = self.recovery.recover("tx_rename_env_attack")
        self.assertFalse(res.recovered)
        self.assertEqual(res.final_status, TransactionStatus.FAILED)
        self.assertTrue(res.ambiguous)
        self.assertIn("protected destination", str(res.error).lower())

        # Assert .env file was NOT moved or deleted, and stolen file was NOT created
        self.assertTrue(env_path.exists())
        self.assertEqual(env_path.read_text(encoding="utf-8"), "DATABASE_PASSWORD=supersecret\n")
        self.assertFalse(stolen_path.exists())

    def test_recovery_rename_new_target_brainfrog_control_plane_denied(self) -> None:
        """Recovery rejects rename operations targeting .brainfrog internal state as destination."""
        proofs_dir = self.workspace / ".brainfrog" / "proofs"
        proofs_dir.mkdir(parents=True, exist_ok=True)
        proof_file = proofs_dir / "valid_task.json"
        proof_file.write_text('{"verdict": "VERIFIED"}', encoding="utf-8")

        tx = Transaction(
            id="tx_rename_bf_attack",
            workspace=str(self.workspace),
            status=TransactionStatus.EXECUTING,
        )
        op = TransactionOperation(
            id="op_rename_bf",
            type=OperationType.RENAME_FILE,
            target="stolen_proof.json",
            new_target=".brainfrog/proofs/valid_task.json",
            status=OperationStatus.EXECUTED,
            before_state={"exists": True, "is_text": True, "content": '{"verdict": "VERIFIED"}'},
            after_state={"new_target": ".brainfrog/proofs/valid_task.json"},
        )
        tx.operations.append(op)
        self.store.save(tx)

        res = self.recovery.recover("tx_rename_bf_attack")
        self.assertFalse(res.recovered)
        self.assertEqual(res.final_status, TransactionStatus.FAILED)
        self.assertTrue(res.ambiguous)
        self.assertIn("protected destination", str(res.error).lower())
        self.assertTrue(proof_file.exists())
        self.assertFalse((self.workspace / "stolen_proof.json").exists())

    def test_recovery_rename_new_target_outside_traversal_denied(self) -> None:
        """Recovery rejects rename operations targeting paths outside workspace boundary."""
        outside_file = self.temp_dir / "outside_host_secret.txt"
        outside_file.write_text("HOST_SECRET", encoding="utf-8")

        tx = Transaction(
            id="tx_rename_outside_attack",
            workspace=str(self.workspace),
            status=TransactionStatus.EXECUTING,
        )
        op = TransactionOperation(
            id="op_rename_outside",
            type=OperationType.RENAME_FILE,
            target="stolen_outside.txt",
            new_target="../../outside_host_secret.txt",
            status=OperationStatus.EXECUTED,
            before_state={"exists": True, "is_text": True, "content": "HOST_SECRET"},
            after_state={"new_target": "../../outside_host_secret.txt"},
        )
        tx.operations.append(op)
        self.store.save(tx)

        res = self.recovery.recover("tx_rename_outside_attack")
        self.assertFalse(res.recovered)
        self.assertEqual(res.final_status, TransactionStatus.FAILED)
        self.assertTrue(res.ambiguous)
        self.assertIn("protected destination", str(res.error).lower())
        self.assertTrue(outside_file.exists())
        self.assertFalse((self.workspace / "stolen_outside.txt").exists())

    def test_legitimate_rename_recovery_succeeds(self) -> None:
        """Recovery cleanly moves renamed file back to original location for valid operations."""
        old_path = self.workspace / "src" / "old_service.py"
        new_path = self.workspace / "src" / "new_service.py"
        new_path.parent.mkdir(parents=True, exist_ok=True)
        new_path.write_text("class Service: pass\n", encoding="utf-8")

        tx = Transaction(
            id="tx_legit_rename",
            workspace=str(self.workspace),
            status=TransactionStatus.EXECUTING,
        )
        op = TransactionOperation(
            id="op_legit_rename",
            type=OperationType.RENAME_FILE,
            target="src/old_service.py",
            new_target="src/new_service.py",
            status=OperationStatus.EXECUTED,
            before_state={"exists": True, "is_text": True, "content": "class Service: pass\n"},
            after_state={"new_target": "src/new_service.py"},
        )
        tx.operations.append(op)
        self.store.save(tx)

        res = self.recovery.recover("tx_legit_rename")
        self.assertTrue(res.recovered)
        self.assertTrue(res.rolled_back)
        self.assertEqual(res.final_status, TransactionStatus.ROLLED_BACK)
        self.assertEqual(res.operations_recovered, 1)

        # Verified: file moved back to old_path
        self.assertTrue(old_path.exists())
        self.assertEqual(old_path.read_text(encoding="utf-8"), "class Service: pass\n")
        self.assertFalse(new_path.exists())
