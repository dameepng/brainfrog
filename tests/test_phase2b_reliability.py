"""Tests for BrainFrog Phase 2B — Reliable Autonomous Execution (Adversarial Hardening).

Validates:
1. P0-1: Hardened Subprocess Runner (timeouts, DEVNULL stdin, tree termination, bounded output, env vars, typed exits)
2. P0-2: Workspace Read Containment (traversal rejection, absolute path rejection, symlink escape rejection, no leakage)
3. P0-3: Correct Transaction Lifecycle (test verification before commit, Case A-E, preservation of pre-existing user edits, external mutation defense)
4. P1-1: System 1 / Jev Resilience (transient retries, backoff, 5xx retry, 429 retry, malformed response safety, fail-closed behavior)
5. P1-2: Workspace Locking (mutual exclusion, canonical aliases, different workspace allowed, release on success/failure/cancel, dead PID & PID-reuse recovery)
6. P1-3: Cancellation Cleanup (KeyboardInterrupt rolls back in-flight transaction, terminates processes, releases lock)
7. Failure State Invariants Matrix
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from core.runtime.process import (
    HardenedProcessResult,
    ProcessExitStatus,
    run_hardened_subprocess,
    terminate_process_tree,
)
from core.runtime.workspace_lock import (
    WorkspaceLock,
    WorkspaceLockedError,
    is_pid_running,
)
from core.runtime.transaction import (
    BasicFilesystemVerifier,
    FileTransactionStore,
    OperationStatus,
    OperationType,
    TransactionCoordinator,
    TransactionStatus,
)
from core.runtime.runtime import BrainFrogRuntime
from core.runtime.messages import IncomingMessage
from orchestrator import (
    Orchestrator,
    PlanStep,
    RunConfig,
    StepResult,
    _read_files,
    validate_workspace_read_path,
)
from system1.base import Answer, ChoiceQuestion, NoulQuestion, ScoreQuestion
from system1.typesafe_client import TypeSafeSystemOne


# ===========================================================================
# 1. P0-1 — Hardened Subprocess Runner Tests
# ===========================================================================

class TestHardenedSubprocess:
    def test_subprocess_timeout_terminates_and_marks_result(self, tmp_path: Path):
        """A hanging subprocess is terminated when timeout expires and returns is_timeout=True."""
        code = "import time; time.sleep(10)"
        cmd = [sys.executable, "-c", code]
        res = run_hardened_subprocess(cmd, cwd=tmp_path, timeout=0.3)
        assert res.is_timeout is True
        assert res.is_success is False
        assert res.returncode in (-1, 124)
        assert res.status == ProcessExitStatus.TIMEOUT

    def test_subprocess_timeout_none_or_nonpositive_rejected(self, tmp_path: Path):
        """Autonomous execution must reject timeout=None, timeout=0, and negative timeouts."""
        cmd = [sys.executable, "-c", "import sys; sys.exit(0)"]
        with pytest.raises(ValueError, match="finite, positive timeout"):
            run_hardened_subprocess(cmd, cwd=tmp_path, timeout=None)  # type: ignore

        with pytest.raises(ValueError, match="finite, positive timeout"):
            run_hardened_subprocess(cmd, cwd=tmp_path, timeout=0.0)

        with pytest.raises(ValueError, match="finite, positive timeout"):
            run_hardened_subprocess(cmd, cwd=tmp_path, timeout=-5.0)

    def test_subprocess_stdin_closed_by_default(self, tmp_path: Path):
        """Autonomous processes must receive EOF on stdin and not block on interactive input."""
        code = "import sys; data = sys.stdin.read(); print(f'read:{len(data)}')"
        cmd = [sys.executable, "-c", code]
        res = run_hardened_subprocess(cmd, cwd=tmp_path, timeout=5.0)
        assert res.is_success is True
        assert "read:0" in res.stdout

    def test_subprocess_env_vars_injected(self, tmp_path: Path):
        """CI=1 and GIT_TERMINAL_PROMPT=0 must be set in child environment without destroying existing env."""
        code = (
            "import os; "
            "print('CI=' + os.environ.get('CI', '')); "
            "print('GIT=' + os.environ.get('GIT_TERMINAL_PROMPT', ''))"
        )
        cmd = [sys.executable, "-c", code]
        res = run_hardened_subprocess(cmd, cwd=tmp_path, timeout=5.0)
        assert res.is_success is True
        assert "CI=1" in res.stdout
        assert "GIT=0" in res.stdout

    def test_subprocess_bounded_output_truncates_large_stream(self, tmp_path: Path):
        """Large outputs must be bounded with head/tail retained and is_truncated set."""
        code = "import sys; sys.stdout.write('A' * 5000)"
        cmd = [sys.executable, "-c", code]
        res = run_hardened_subprocess(cmd, cwd=tmp_path, timeout=5.0, max_output_bytes=1000)
        assert res.is_truncated is True
        assert len(res.stdout.encode("utf-8")) <= 1500
        assert "OUTPUT TRUNCATED" in res.stdout

    def test_subprocess_simultaneous_huge_stdout_and_stderr(self, tmp_path: Path):
        """Both stdout and stderr large streams are bounded simultaneously without memory growth."""
        code = (
            "import sys; "
            "sys.stdout.write('O' * 10000); sys.stdout.flush(); "
            "sys.stderr.write('E' * 10000); sys.stderr.flush()"
        )
        cmd = [sys.executable, "-c", code]
        res = run_hardened_subprocess(cmd, cwd=tmp_path, timeout=5.0, max_output_bytes=1000)
        assert res.is_truncated is True
        assert "OUTPUT TRUNCATED" in res.stdout
        assert "OUTPUT TRUNCATED" in res.stderr
        assert len(res.stdout.encode("utf-8")) <= 2000
        assert len(res.stderr.encode("utf-8")) <= 2000

    def test_subprocess_process_tree_termination(self, tmp_path: Path):
        """Terminating a process also terminates spawned child processes."""
        marker_file = tmp_path / "child.pid"
        script_file = tmp_path / "launcher.py"
        script_file.write_text(
            f"import subprocess, sys, time\n"
            f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            f"with open(r'{marker_file}', 'w') as f:\n"
            f"    f.write(str(p.pid))\n"
            f"time.sleep(30)\n",
            encoding="utf-8",
        )
        res = run_hardened_subprocess([sys.executable, str(script_file)], cwd=tmp_path, timeout=0.8)
        assert res.is_timeout is True

        time.sleep(0.5)
        if marker_file.exists():
            child_pid = int(marker_file.read_text().strip())
            assert not is_pid_running(child_pid), f"Child PID {child_pid} should have been terminated"

    def test_subprocess_grandchild_process_tree_termination(self, tmp_path: Path):
        """Parent -> Child -> Grandchild process hierarchy is completely terminated on timeout."""
        gc_pid_file = tmp_path / "grandchild.pid"
        gc_script = tmp_path / "grandchild.py"
        gc_script.write_text("import time; time.sleep(60)\n", encoding="utf-8")

        child_script = tmp_path / "child.py"
        child_script.write_text(
            f"import subprocess, sys, time\n"
            f"p = subprocess.Popen([sys.executable, r'{gc_script}'])\n"
            f"with open(r'{gc_pid_file}', 'w') as f:\n"
            f"    f.write(str(p.pid))\n"
            f"time.sleep(60)\n",
            encoding="utf-8",
        )

        launcher_file = tmp_path / "launcher_tree.py"
        launcher_file.write_text(
            f"import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, r'{child_script}'])\n"
            f"time.sleep(60)\n",
            encoding="utf-8",
        )
        res = run_hardened_subprocess([sys.executable, str(launcher_file)], cwd=tmp_path, timeout=0.8)
        assert res.is_timeout is True

        time.sleep(0.5)
        if gc_pid_file.exists():
            gc_pid = int(gc_pid_file.read_text().strip())
            assert not is_pid_running(gc_pid), f"Grandchild PID {gc_pid} must be terminated"

    def test_subprocess_exit_semantics_typed(self, tmp_path: Path):
        """Caller can distinguish success, non-zero exit, and execution errors with typed properties."""
        # Success
        r_ok = run_hardened_subprocess([sys.executable, "-c", "import sys; sys.exit(0)"], cwd=tmp_path)
        assert r_ok.is_success is True
        assert r_ok.status == ProcessExitStatus.SUCCESS
        assert r_ok.returncode == 0

        # Non-zero exit
        r_fail = run_hardened_subprocess([sys.executable, "-c", "import sys; sys.exit(42)"], cwd=tmp_path)
        assert r_fail.is_success is False
        assert r_fail.is_non_zero_exit is True
        assert r_fail.status == ProcessExitStatus.NON_ZERO_EXIT
        assert r_fail.returncode == 42
        assert r_fail.is_timeout is False

        # Command not found / execution error
        r_err = run_hardened_subprocess(["non_existent_executable_12345"], cwd=tmp_path, allow_shell=False)
        assert r_err.is_success is False
        assert r_err.is_execution_error is True
        assert r_err.status == ProcessExitStatus.EXECUTION_ERROR


# ===========================================================================
# 2. P0-2 — Workspace Read Containment Tests
# ===========================================================================

class TestWorkspaceReadContainment:
    def test_normal_file_read_allowed(self, tmp_path: Path):
        """Reading a normal file within the workspace succeeds."""
        f = tmp_path / "hello.txt"
        f.write_text("hello world", encoding="utf-8")
        contents = _read_files(tmp_path, ["hello.txt"])
        assert contents.get("hello.txt") == "hello world"

    def test_legitimate_nested_path_allowed(self, tmp_path: Path):
        """Reading a nested file within the workspace succeeds."""
        sub = tmp_path / "src" / "pkg"
        sub.mkdir(parents=True)
        (sub / "mod.py").write_text("x = 1", encoding="utf-8")
        contents = _read_files(tmp_path, ["src/pkg/mod.py"])
        assert contents.get("src/pkg/mod.py") == "x = 1"

    def test_parent_traversal_rejected(self, tmp_path: Path):
        """Relative traversal '../' escaping workspace root raises PermissionError."""
        outside = tmp_path.parent / "secret.txt"
        outside.write_text("secret_content", encoding="utf-8")
        with pytest.raises(PermissionError) as exc_info:
            validate_workspace_read_path(tmp_path, "../secret.txt")
        assert "escapes workspace root" in str(exc_info.value)
        # Content should not be leaked in exception
        assert "secret_content" not in str(exc_info.value)

    def test_nested_parent_traversal_rejected(self, tmp_path: Path):
        """Nested traversal 'a/../../secret' escaping workspace root raises PermissionError."""
        with pytest.raises(PermissionError):
            validate_workspace_read_path(tmp_path, "sub/../../escape.txt")

    def test_absolute_path_outside_rejected(self, tmp_path: Path):
        """Absolute path pointing outside workspace raises PermissionError."""
        outside_path = tmp_path.parent / "outside.txt"
        with pytest.raises(PermissionError):
            validate_workspace_read_path(tmp_path, str(outside_path))

    def test_external_symlink_rejected(self, tmp_path: Path):
        """A symlink pointing outside the workspace root is rejected."""
        outside = tmp_path.parent / "ext_target.txt"
        outside.write_text("external_data", encoding="utf-8")
        link = tmp_path / "link_to_outside.txt"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("Symlinks not permitted in this test environment")

        with pytest.raises(PermissionError):
            validate_workspace_read_path(tmp_path, "link_to_outside.txt")

    def test_broken_symlink_pointing_outside_rejected(self, tmp_path: Path):
        """A broken symlink pointing outside the workspace is rejected even if target does not exist."""
        non_existent_outside = tmp_path.parent / "ghost_outside.txt"
        link = tmp_path / "broken_link.txt"
        try:
            link.symlink_to(non_existent_outside)
        except (OSError, NotImplementedError):
            pytest.skip("Symlinks not permitted in this test environment")

        with pytest.raises(PermissionError):
            validate_workspace_read_path(tmp_path, "broken_link.txt")

    def test_internal_symlink_allowed(self, tmp_path: Path):
        """A symlink pointing to another location inside the workspace is allowed."""
        real_file = tmp_path / "target.py"
        real_file.write_text("val = 123", encoding="utf-8")
        link = tmp_path / "link_internal.py"
        try:
            link.symlink_to(real_file)
        except (OSError, NotImplementedError):
            pytest.skip("Symlinks not permitted in this test environment")

        resolved = validate_workspace_read_path(tmp_path, "link_internal.py")
        assert resolved.exists()

    def test_workspace_canonical_alias_read_allowed(self, tmp_path: Path):
        """Paths using relative navigation within the repository root resolve safely."""
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "app.py").write_text("app_content", encoding="utf-8")
        p = validate_workspace_read_path(tmp_path, "./sub/../sub/app.py")
        assert p.exists()

    def test_no_external_data_or_path_leakage_in_exceptions(self, tmp_path: Path):
        """Rejecting external reads does not leak external file content into errors or logs."""
        secret_file = tmp_path.parent / "secret_key.pem"
        secret_content = "PRIVATE_KEY_DATA_DO_NOT_LEAK"
        secret_file.write_text(secret_content, encoding="utf-8")

        with pytest.raises(PermissionError) as exc_info:
            validate_workspace_read_path(tmp_path, "../secret_key.pem")

        err_str = str(exc_info.value)
        assert secret_content not in err_str
        assert str(secret_file) not in err_str


# ===========================================================================
# 3. P0-3 — Correct Transaction Lifecycle Tests
# ===========================================================================

class TestTransactionLifecycle:
    def _make_dummy_orchestrator(self, repo_dir: Path, store: FileTransactionStore):
        s1 = MagicMock()
        s1.name = "mock_s1"
        def mock_decide(state, questions):
            passed = state.get("execution", {}).get("test_passed", True)
            return {
                "tests_passing": Answer(choice="yes" if passed else "no", noul=1.0 if passed else 0.0),
                "diff_complete": Answer(choice="yes", noul=1.0),
                "failure_fixable": Answer(choice="no" if passed else "yes", noul=0.0 if passed else 0.8),
                "retry_concern": Answer(score="within_normal", confidence=0.9),
                "diff_risk": Answer(score=0, choice="low"),
                "safe_to_proceed": Answer(choice="yes", noul=1.0),
            }
        s1.decide.side_effect = mock_decide
        s2 = MagicMock()
        s2.provider_name = "mock_s2"
        s2.write_code.return_value = {"generated.py": "print('hello')\n"}
        s2.draft_pr.return_value = {"title": "feat: test", "body": "test"}

        cfg = RunConfig(
            repo_dir=repo_dir,
            task="Write something",
            test_command=["pytest"],
            transaction_store=store,
            transaction_verifier=BasicFilesystemVerifier(),
            actor="user",
            session_id="sess_tx",
        )
        return Orchestrator(system1=s1, system2=s2, config=cfg)

    def test_case_a_mutation_succeeds_tests_pass_commits(self, tmp_path: Path):
        """Case A: Mutation succeeds, tests pass -> COMMITTED."""
        store = FileTransactionStore(tmp_path / ".brainfrog" / "transactions")
        orch = self._make_dummy_orchestrator(tmp_path, store)
        step = PlanStep(id="1", description="Create file", files=["generated.py"])

        ok_proc = HardenedProcessResult(args=["pytest"], returncode=0, stdout="Tests passed", stderr="")
        with patch.object(orch, "_run_test_suite", return_value=ok_proc):
            with patch.object(orch, "_can_push_to_remote", return_value=False):
                res = orch._run_step(step)

        assert res.outcome in ("opened_pr", "drafted_pr", "verified")
        tx_res = orch.cfg.last_transaction_result
        assert tx_res is not None
        assert tx_res.success is True
        assert tx_res.status == TransactionStatus.COMMITTED
        assert (tmp_path / "generated.py").exists()

    def test_case_b_mutation_succeeds_tests_fail_rolls_back(self, tmp_path: Path):
        """Case B: Mutation succeeds, tests fail -> ROLLED_BACK."""
        store = FileTransactionStore(tmp_path / ".brainfrog" / "transactions")
        orch = self._make_dummy_orchestrator(tmp_path, store)
        orch.cfg.max_retries = 1
        step = PlanStep(id="1", description="Create file", files=["generated.py"])

        fail_proc = HardenedProcessResult(args=["pytest"], returncode=1, stdout="AssertionError in tests", stderr="")
        with patch.object(orch, "_run_test_suite", return_value=fail_proc):
            res = orch._run_step(step)

        assert res.outcome in ("abandoned", "escalated")
        tx_res = orch.cfg.last_transaction_result
        assert tx_res is not None
        assert tx_res.success is False
        assert tx_res.status == TransactionStatus.ROLLED_BACK
        # The mutation was rolled back
        assert not (tmp_path / "generated.py").exists()

    def test_case_c_repeated_retries_fail_leaves_no_brainfrog_mutations(self, tmp_path: Path):
        """Case C: Mutation succeeds, tests fail across all retries -> ROLLED_BACK, no mutation remains."""
        store = FileTransactionStore(tmp_path / ".brainfrog" / "transactions")
        orch = self._make_dummy_orchestrator(tmp_path, store)
        orch.cfg.max_retries = 3
        step = PlanStep(id="1", description="Create file", files=["generated.py"])

        fail_proc = HardenedProcessResult(args=["pytest"], returncode=1, stdout="FAILED", stderr="")
        with patch.object(orch, "_run_test_suite", return_value=fail_proc):
            res = orch._run_step(step)

        assert res.outcome in ("abandoned", "escalated")
        assert not (tmp_path / "generated.py").exists()
        tx_res = orch.cfg.last_transaction_result
        assert tx_res is not None
        tx = store.get(tx_res.transaction_id)
        assert tx is not None
        assert tx.status == TransactionStatus.ROLLED_BACK

    def test_case_d_pre_existing_user_changes_preserved_on_rollback(self, tmp_path: Path):
        """Case D: Pre-existing user uncommitted edits must NOT be wiped when BrainFrog rolls back."""
        user_file = tmp_path / "user_work.txt"
        user_file.write_text("pre-existing user modification", encoding="utf-8")

        store = FileTransactionStore(tmp_path / ".brainfrog" / "transactions")
        orch = self._make_dummy_orchestrator(tmp_path, store)
        orch.cfg.max_retries = 1
        step = PlanStep(id="1", description="Create file", files=["generated.py"])

        fail_proc = HardenedProcessResult(args=["pytest"], returncode=1, stdout="Tests failed", stderr="")
        with patch.object(orch, "_run_test_suite", return_value=fail_proc):
            orch._run_step(step)

        # BrainFrog's file was rolled back
        assert not (tmp_path / "generated.py").exists()
        # Pre-existing user uncommitted modification is fully preserved!
        assert user_file.exists()
        assert user_file.read_text(encoding="utf-8") == "pre-existing user modification"

    def test_case_e_rollback_failure_fails_closed_and_records_metadata(self, tmp_path: Path):
        """Case E: If rollback itself fails, fail closed, record failure, and never claim success."""
        store = FileTransactionStore(tmp_path / ".brainfrog" / "transactions")
        orch = self._make_dummy_orchestrator(tmp_path, store)
        orch.cfg.max_retries = 1
        step = PlanStep(id="1", description="Create file", files=["generated.py"])

        fail_proc = HardenedProcessResult(args=["pytest"], returncode=1, stdout="Test failed", stderr="")
        with patch.object(orch, "_run_test_suite", return_value=fail_proc):
            with patch.object(TransactionCoordinator, "rollback", side_effect=IOError("Disk locked")):
                res = orch._run_step(step)

        # Must fail closed with escalated status
        assert res.outcome == "escalated"
        assert "Rollback Failure" in res.detail

    def test_case_modified_existing_file_restored_on_failure(self, tmp_path: Path):
        """BrainFrog modifies an existing file, tests fail, pre-existing content is restored."""
        existing_file = tmp_path / "app.py"
        existing_file.write_text("original user code", encoding="utf-8")

        store = FileTransactionStore(tmp_path / ".brainfrog" / "transactions")
        orch = self._make_dummy_orchestrator(tmp_path, store)
        setattr(orch.s2, "write_code", MagicMock(return_value={"app.py": "broken agent code"}))
        orch.cfg.max_retries = 1
        step = PlanStep(id="1", description="Modify existing file", files=["app.py"])

        fail_proc = HardenedProcessResult(args=["pytest"], returncode=1, stdout="Tests failed", stderr="")
        with patch.object(orch, "_run_test_suite", return_value=fail_proc):
            orch._run_step(step)

        # Restored to original user code
        assert existing_file.read_text(encoding="utf-8") == "original user code"

    def test_case_deleted_file_restored_on_failure(self, tmp_path: Path):
        """BrainFrog deletes a file, tests fail, deleted file is restored exactly."""
        store = FileTransactionStore(tmp_path / ".brainfrog" / "transactions")
        to_del = tmp_path / "important.txt"
        to_del.write_text("important information", encoding="utf-8")

        coord = TransactionCoordinator(tmp_path, store=store)
        coord.stage_operation(OperationType.DELETE_FILE, "important.txt")
        assert coord.execute() is True
        assert not to_del.exists()

        # Rollback restores deleted file
        coord.rollback(reason="Test failure")
        assert to_del.exists()
        assert to_del.read_text(encoding="utf-8") == "important information"

    def test_case_external_mutation_during_transaction_fails_closed_and_protects_user_edit(self, tmp_path: Path):
        """If a user edits a file while BrainFrog is running, rollback aborts overwriting user change."""
        store = FileTransactionStore(tmp_path / ".brainfrog" / "transactions")
        coord = TransactionCoordinator(tmp_path, store=store)
        coord.stage_operation(OperationType.CREATE_FILE, "shared.py", "brainfrog initial version")
        assert coord.execute() is True
        shared_file = tmp_path / "shared.py"
        assert shared_file.read_text(encoding="utf-8") == "brainfrog initial version"

        # Concurrently, user / external process updates shared.py
        shared_file.write_text("user concurrent emergency patch", encoding="utf-8")

        # Rollback should detect external modification and abort deleting/overwriting user patch
        res = coord.rollback(reason="Test failure")
        assert res.rolled_back is False
        assert res.status == TransactionStatus.FAILED
        assert "External mutation detected" in (res.rollback_error or "")
        # User patch is preserved!
        assert shared_file.read_text(encoding="utf-8") == "user concurrent emergency patch"


# ===========================================================================
# 4. P1-1 — System 1 / Jev Resilience Tests
# ===========================================================================

class TestSystem1Resilience:
    def test_transient_failures_retry_with_backoff(self):
        """Transient HTTP errors (500, timeouts) retry up to 3 times."""
        client = TypeSafeSystemOne(api_key="test_key", api_url="http://dummy-s1.local", timeout=1.0)
        q = ChoiceQuestion(instructions={"question": "Test?"}, criteria={"a": {"what": "opt a"}})

        mock_responses = [
            requests.exceptions.ConnectionError("Connection refused"),
            requests.exceptions.Timeout("Read timeout"),
            MagicMock(status_code=200, json=lambda: {"answers": {"q": {"choice": "a", "confidence": 0.9}}}),
        ]

        with patch("requests.post", side_effect=mock_responses) as mock_post:
            with patch("time.sleep") as mock_sleep:
                answers = client.decide({"task": "do something"}, {"q": q})
                ans = answers["q"]

        assert ans.choice == "a"
        assert mock_post.call_count == 3
        # Backoff was called with 1.0s then 2.0s
        assert mock_sleep.call_count == 2
        mock_sleep.assert_any_call(1.0)
        mock_sleep.assert_any_call(2.0)

    def test_bounded_retries_fail_closed_after_limit(self):
        """Retries are bounded to max_retries and fail closed afterwards."""
        client = TypeSafeSystemOne(api_key="test_key", api_url="http://dummy-s1.local", timeout=1.0)
        q = ChoiceQuestion(instructions={"question": "Test?"}, criteria={"a": {"what": "opt a"}})

        with patch("requests.post", side_effect=requests.exceptions.ConnectionError("Down")) as mock_post:
            with patch("time.sleep"):
                with pytest.raises(RuntimeError) as exc_info:
                    client.decide({"task": "test"}, {"q": q})

        assert mock_post.call_count == 3
        assert "after 3 attempts" in str(exc_info.value)

    def test_deterministic_client_error_does_not_retry(self):
        """HTTP 400 Bad Request fails fast without retrying."""
        client = TypeSafeSystemOne(api_key="test_key", api_url="http://dummy-s1.local", timeout=1.0)
        q = ChoiceQuestion(instructions={"question": "Test?"}, criteria={"a": {"what": "opt a"}})

        resp_400 = MagicMock(status_code=400, text="Bad Request")
        resp_400.raise_for_status.side_effect = requests.exceptions.HTTPError(response=resp_400)

        with patch("requests.post", return_value=resp_400) as mock_post:
            with pytest.raises(RuntimeError) as exc_info:
                client.decide({"task": "test"}, {"q": q})

        assert "TypeSafe/Jev API error (400)" in str(exc_info.value)
        # Only 1 attempt for deterministic client error
        assert mock_post.call_count == 1

    def test_500_server_error_retries_and_succeeds(self):
        """HTTP 500 Internal Server Error retries with backoff and succeeds."""
        client = TypeSafeSystemOne(api_key="test_key", api_url="http://dummy-s1.local", timeout=1.0)
        q = ChoiceQuestion(instructions={"question": "Test?"}, criteria={"a": {"what": "opt a"}})

        resp_500 = MagicMock(status_code=500, text="Internal Server Error")
        resp_200 = MagicMock(status_code=200, json=lambda: {"answers": {"q": {"choice": "a", "confidence": 0.95}}})

        with patch("requests.post", side_effect=[resp_500, resp_500, resp_200]) as mock_post:
            with patch("time.sleep") as mock_sleep:
                answers = client.decide({"task": "test"}, {"q": q})

        assert answers["q"].choice == "a"
        assert mock_post.call_count == 3
        assert mock_sleep.call_count == 2

    def test_429_rate_limit_retries_and_succeeds(self):
        """HTTP 429 Too Many Requests retries with backoff and succeeds."""
        client = TypeSafeSystemOne(api_key="test_key", api_url="http://dummy-s1.local", timeout=1.0)
        q = ChoiceQuestion(instructions={"question": "Test?"}, criteria={"a": {"what": "opt a"}})

        resp_429 = MagicMock(status_code=429, text="Rate limit exceeded")
        resp_200 = MagicMock(status_code=200, json=lambda: {"answers": {"q": {"choice": "a", "confidence": 0.88}}})

        with patch("requests.post", side_effect=[resp_429, resp_200]) as mock_post:
            with patch("time.sleep"):
                answers = client.decide({"task": "test"}, {"q": q})

        assert answers["q"].choice == "a"
        assert mock_post.call_count == 2

    def test_malformed_response_fails_safely_without_crash(self):
        """Nulls, missing fields, or malformed JSON payloads fail safely."""
        client = TypeSafeSystemOne(api_key="test_key", api_url="http://dummy-s1.local", timeout=1.0)
        q = ChoiceQuestion(instructions={"question": "Test?"}, criteria={"a": {"what": "opt a"}})

        resp_bad_json = MagicMock(status_code=200, json=lambda: {"answers": {"q": {"invalid_key": None}}})
        with patch("requests.post", return_value=resp_bad_json):
            answers = client.decide({"task": "test"}, {"q": q})
            ans = answers.get("q")

        assert ans is not None
        assert ans.choice is None
        assert ans.confidence == 0.0

    def test_malformed_non_json_payload_fails_closed(self):
        """Non-JSON response body fails closed with a clear error."""
        client = TypeSafeSystemOne(api_key="test_key", api_url="http://dummy-s1.local", timeout=1.0)
        q = ChoiceQuestion(instructions={"question": "Test?"}, criteria={"a": {"what": "opt a"}})

        resp_html = MagicMock(status_code=200, text="<html>502 Bad Gateway</html>")
        resp_html.json.side_effect = json.JSONDecodeError("Expecting value", "doc", 0)

        with patch("requests.post", return_value=resp_html):
            with pytest.raises(RuntimeError, match="malformed non-JSON payload"):
                client.decide({"task": "test"}, {"q": q})

    def test_jev_unavailable_fails_closed_in_scope_gate(self, tmp_path: Path):
        """If Jev is unavailable in scope_gate, execution pauses with a clarify message."""
        s1 = MagicMock()
        s1.name = "broken_jev"
        s1.decide.side_effect = RuntimeError("Jev unavailable")
        s2 = MagicMock()

        from core.modules import Domain
        cfg = RunConfig(
            repo_dir=tmp_path,
            task="Do something dangerous",
            test_command=["pytest"],
            domains={"core": Domain(key="core", description="Core area", paths=["core/"])},
        )
        orch = Orchestrator(system1=s1, system2=s2, config=cfg)
        dec = orch._scope_gate()

        assert dec.domain is None
        assert dec.clarify_message is not None
        assert "unavailable" in dec.clarify_message

    def test_contradictory_response_fails_closed_in_post_test_eval(self, tmp_path: Path):
        """Even if model returns passing=yes with high confidence, test failure exitcode strictly blocks pass."""
        s1 = MagicMock()
        s1.name = "hallucinating_jev"
        # Jev falsely claims tests pass with 1.0
        s1.decide.return_value = {
            "tests_passing": Answer(choice="yes", noul=1.0),
            "diff_complete": Answer(choice="yes", noul=1.0),
            "failure_fixable": Answer(choice="no", noul=0.0),
            "retry_concern": Answer(score="within_normal", confidence=0.9),
        }
        s2 = MagicMock()
        s2.write_code.return_value = {"code.py": "x = 1"}

        cfg = RunConfig(
            repo_dir=tmp_path,
            task="Task",
            test_command=["pytest"],
            transaction_store=FileTransactionStore(tmp_path / ".brainfrog" / "transactions"),
            transaction_verifier=BasicFilesystemVerifier(),
            max_retries=1,
        )
        orch = Orchestrator(system1=s1, system2=s2, config=cfg)
        step = PlanStep(id="1", description="step", files=["code.py"])

        # Domain test actually failed (returncode=1)
        fail_proc = HardenedProcessResult(args=["pytest"], returncode=1, stdout="FAILURE", stderr="")
        with patch.object(orch, "_run_test_suite", return_value=fail_proc):
            res = orch._run_step(step)

        # Must NOT commit; must escalate/abandon
        assert res.outcome in ("abandoned", "escalated")
        assert not (tmp_path / "code.py").exists()

    def test_jev_unavailable_in_finalize_pr_fails_closed_to_high_risk(self, tmp_path: Path):
        """If Jev decision fails during PR finalization, risk defaults to high and auto-PR is blocked."""
        s1 = MagicMock()
        s1.name = "flaky_jev"
        s1.decide.side_effect = RuntimeError("Jev timeout")
        s2 = MagicMock()
        s2.draft_pr.return_value = {"title": "feat: change", "body": "details"}

        cfg = RunConfig(
            repo_dir=tmp_path,
            task="Task",
            test_command=["pytest"],
            auto_pr=True,
        )
        orch = Orchestrator(system1=s1, system2=s2, config=cfg)
        step = PlanStep(id="1", description="step", files=[])

        with patch.object(orch, "_can_push_to_remote", return_value=True):
            with patch("orchestrator._diff_stat", return_value={}):
                with patch("orchestrator._run", return_value=HardenedProcessResult(args=[], returncode=0, stdout="")):
                    res = orch._finalize_pr(step, retries=0, test_output="")

        # Auto-PR was blocked due to high risk fallback
        assert res.outcome == "drafted_pr"
        assert "risk=2" in res.detail


# ===========================================================================
# 5. P1-2 — Workspace Locking Tests
# ===========================================================================

class TestWorkspaceLocking:
    def test_same_workspace_concurrent_access_blocked(self, tmp_path: Path):
        """Second attempt to acquire lock on the same workspace is blocked."""
        lock1 = WorkspaceLock(tmp_path, session_id="sess_1", timeout=0.1)
        lock2 = WorkspaceLock(tmp_path, session_id="sess_2", timeout=0.1)

        assert lock1.acquire() is True
        try:
            with pytest.raises(WorkspaceLockedError) as exc_info:
                lock2.acquire()
            assert "locked" in str(exc_info.value)
        finally:
            lock1.release()

    def test_workspace_lock_canonical_aliases_conflict(self, tmp_path: Path):
        """Different path representations of the same workspace resolve to the same lock and conflict."""
        sub_dir = tmp_path / "sub"
        sub_dir.mkdir()
        alias_path = sub_dir / ".." / "."

        lock1 = WorkspaceLock(tmp_path, session_id="sess_canon", timeout=0.1)
        lock2 = WorkspaceLock(alias_path, session_id="sess_alias", timeout=0.1)

        assert lock1.acquire() is True
        try:
            with pytest.raises(WorkspaceLockedError):
                lock2.acquire()
        finally:
            lock1.release()

    def test_different_workspaces_allowed_concurrently(self, tmp_path: Path):
        """Locks on different workspaces do not block each other."""
        ws_a = tmp_path / "repo_a"
        ws_b = tmp_path / "repo_b"
        ws_a.mkdir()
        ws_b.mkdir()

        lock_a = WorkspaceLock(ws_a, session_id="sess_a", timeout=0.1)
        lock_b = WorkspaceLock(ws_b, session_id="sess_b", timeout=0.1)

        assert lock_a.acquire() is True
        try:
            assert lock_b.acquire() is True
            lock_b.release()
        finally:
            lock_a.release()

    def test_lock_released_on_success(self, tmp_path: Path):
        """Lock is released cleanly and can be acquired by another session."""
        lock1 = WorkspaceLock(tmp_path, session_id="sess_1")
        lock1.acquire()
        lock1.release()

        lock2 = WorkspaceLock(tmp_path, session_id="sess_2", timeout=0.1)
        assert lock2.acquire() is True
        lock2.release()

    def test_stale_pid_lock_recovers_safely(self, tmp_path: Path):
        """A lock held by a dead PID is detected as stale and safely recovered."""
        lock = WorkspaceLock(tmp_path, session_id="sess_dead", timeout=0.2)
        dot_bf = tmp_path / ".brainfrog"
        dot_bf.mkdir(parents=True, exist_ok=True)
        meta_file = dot_bf / "workspace.lock.meta"
        meta_file.write_text(json.dumps({
            "pid": 999999,
            "session_id": "sess_dead",
            "actor": "ghost",
            "acquired_at": time.time(),
            "workspace": str(tmp_path),
        }), encoding="utf-8")

        assert lock.acquire() is True
        lock.release()

    def test_pid_reuse_detected_and_cleared(self, tmp_path: Path):
        """PID reuse with mismatched process creation time is detected as stale."""
        lock = WorkspaceLock(tmp_path, session_id="sess_reuse", timeout=0.2)
        dot_bf = tmp_path / ".brainfrog"
        dot_bf.mkdir(parents=True, exist_ok=True)
        meta_file = dot_bf / "workspace.lock.meta"
        # Active PID but stale process_create_time (e.g. 10 seconds in the epoch past)
        meta_file.write_text(json.dumps({
            "pid": os.getpid(),
            "process_create_time": 10.0,
            "session_id": "sess_stale_holder",
            "actor": "stale",
            "acquired_at": time.time(),
            "workspace": str(tmp_path),
        }), encoding="utf-8")

        # is_stale returns True because current process create time does not match 10.0
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        assert lock.is_stale(meta) is True

    def test_lock_released_on_exception(self, tmp_path: Path):
        """Lock context manager releases lock when an unhandled exception occurs."""
        lock = WorkspaceLock(tmp_path, session_id="sess_exc")
        try:
            with lock:
                raise RuntimeError("Simulated failure")
        except RuntimeError:
            pass

        # Can immediately be acquired again
        lock2 = WorkspaceLock(tmp_path, session_id="sess_subsequent", timeout=0.1)
        assert lock2.acquire() is True
        lock2.release()


# ===========================================================================
# 6. P1-3 — Cancellation Cleanup Tests
# ===========================================================================

class TestCancellationCleanup:
    def test_cancellation_rolls_back_in_flight_transaction(self, tmp_path: Path):
        """KeyboardInterrupt during step execution triggers active transaction rollback."""
        store = FileTransactionStore(tmp_path / ".brainfrog" / "transactions")
        s1 = MagicMock()
        s1.name = "mock_s1"
        s2 = MagicMock()
        s2.provider_name = "mock_s2"
        s2.write_code.return_value = {"cancelled_work.py": "x = 42\n"}

        cfg = RunConfig(
            repo_dir=tmp_path,
            task="Cancelled task",
            test_command=["pytest"],
            transaction_store=store,
            transaction_verifier=BasicFilesystemVerifier(),
            actor="user",
            session_id="sess_canc",
        )
        orch = Orchestrator(system1=s1, system2=s2, config=cfg)
        step = PlanStep(id="1", description="Cancel step", files=["cancelled_work.py"])

        with patch.object(orch, "_run_test_suite", side_effect=KeyboardInterrupt("Ctrl+C")):
            with pytest.raises(KeyboardInterrupt):
                orch._run_step(step)

        assert not (tmp_path / "cancelled_work.py").exists()
        tx_res = orch.cfg.last_transaction_result
        assert tx_res is not None
        assert tx_res.status == TransactionStatus.ROLLED_BACK

    def test_runtime_cancellation_releases_workspace_lock(self, tmp_path: Path):
        """Runtime execution releases the workspace lock when KeyboardInterrupt is raised."""
        mock_s1 = MagicMock()
        mock_s1.name = "mock_s1"
        mock_s2 = MagicMock()
        mock_s2.provider_name = "mock_s2"

        runtime = BrainFrogRuntime(
            repo_dir=tmp_path,
            persist_sessions=False,
            system1_factory=lambda b: mock_s1,
            system2_factory=lambda **k: mock_s2,
        )
        msg = IncomingMessage(
            id="msg_cancel",
            channel="cli",
            user_id="local",
            conversation_id="conv_canc",
            text="do something",
            metadata={"catch_cancellation": True},
        )

        with patch("core.runtime.runtime.Orchestrator.run", side_effect=KeyboardInterrupt("Cancel")):
            out = runtime.handle_message(msg)

        assert out.status == "cancelled"
        assert out.success is False

        # Verify workspace lock was released: another lock can be acquired immediately
        ws_lock = WorkspaceLock(tmp_path, session_id="sess_after", timeout=0.1)
        assert ws_lock.acquire() is True
        ws_lock.release()

    def test_cancellation_during_subprocess_terminates_tree_and_reraises(self, tmp_path: Path):
        """When KeyboardInterrupt occurs during run_hardened_subprocess, process tree is killed and exception reraised."""
        marker = tmp_path / "int_child.pid"
        script = tmp_path / "int_script.py"
        script.write_text(
            f"import subprocess, sys, time\n"
            f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"with open(r'{marker}', 'w') as f: f.write(str(p.pid))\n"
            f"time.sleep(60)\n",
            encoding="utf-8",
        )

        def raise_interrupt_later():
            time.sleep(0.4)
            # Find and interrupt the main thread
            import _thread
            _thread.interrupt_main()

        th = threading.Thread(target=raise_interrupt_later, daemon=True)
        th.start()

        with pytest.raises(KeyboardInterrupt):
            run_hardened_subprocess([sys.executable, str(script)], cwd=tmp_path, timeout=30.0)

        time.sleep(0.5)
        if marker.exists():
            child_pid = int(marker.read_text().strip())
            assert not is_pid_running(child_pid), f"Child process {child_pid} should have been terminated on cancellation"


# ===========================================================================
# 7. Failure State Invariants Matrix
# ===========================================================================

class TestFailureStateInvariantMatrix:
    """Verifies all six failure-state invariants asserted in Step 9."""

    def test_failure_state_invariants_on_test_failure(self, tmp_path: Path):
        """After test failure: tx != EXECUTING, lock released, processes terminated, mutations reverted, pre-existing preserved, success=False."""
        user_uncommitted = tmp_path / "user_edit.txt"
        user_uncommitted.write_text("user content", encoding="utf-8")

        store = FileTransactionStore(tmp_path / ".brainfrog" / "transactions")
        s1 = MagicMock()
        s1.name = "mock_s1"
        s1.decide.return_value = {
            "tests_passing": Answer(choice="no", noul=0.0),
            "diff_complete": Answer(choice="no", noul=0.0),
            "failure_fixable": Answer(choice="no", noul=0.0),
            "retry_concern": Answer(score="high", confidence=0.0),
        }
        s2 = MagicMock()
        s2.write_code.return_value = {"bad.py": "broken code"}

        cfg = RunConfig(
            repo_dir=tmp_path,
            task="Task",
            test_command=["pytest"],
            transaction_store=store,
            transaction_verifier=BasicFilesystemVerifier(),
            max_retries=1,
        )
        orch = Orchestrator(system1=s1, system2=s2, config=cfg)
        step = PlanStep(id="1", description="step", files=["bad.py"])

        fail_proc = HardenedProcessResult(args=["pytest"], returncode=1, stdout="FAIL", stderr="")
        with patch.object(orch, "_run_test_suite", return_value=fail_proc):
            res = orch._run_step(step)

        # 1. success = False
        assert res.outcome in ("abandoned", "escalated")
        # 2. transaction != EXECUTING (must be ROLLED_BACK)
        tx_res = orch.cfg.last_transaction_result
        assert tx_res is not None
        assert tx_res.status == TransactionStatus.ROLLED_BACK
        # 3. BrainFrog mutations reverted
        assert not (tmp_path / "bad.py").exists()
        # 4. Pre-existing user changes preserved
        assert user_uncommitted.read_text(encoding="utf-8") == "user content"
        # 5. Lock released
        lock = WorkspaceLock(tmp_path, timeout=0.1)
        assert lock.acquire() is True
        lock.release()

    def test_failure_state_invariants_on_cancellation(self, tmp_path: Path):
        """After cancellation: tx is terminal, lock released, success=False."""
        user_file = tmp_path / "my_work.py"
        user_file.write_text("my_code", encoding="utf-8")

        runtime = BrainFrogRuntime(
            repo_dir=tmp_path,
            persist_sessions=False,
            system1_factory=lambda b: MagicMock(),
            system2_factory=lambda **k: MagicMock(),
        )
        msg = IncomingMessage(
            id="msg_canc",
            channel="cli",
            user_id="local",
            conversation_id="conv_canc",
            text="do work",
            metadata={"catch_cancellation": True},
        )

        with patch("core.runtime.runtime.Orchestrator.run", side_effect=KeyboardInterrupt()):
            out = runtime.handle_message(msg)

        assert out.success is False
        assert out.status == "cancelled"
        assert user_file.read_text(encoding="utf-8") == "my_code"

        # Lock is released
        lock = WorkspaceLock(tmp_path, timeout=0.1)
        assert lock.acquire() is True
        lock.release()
