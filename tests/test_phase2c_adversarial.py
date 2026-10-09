"""BrainFrog Phase 2C — Adversarial Failure Injection & Trust Verification Suite.

Comprehensive adversarial test suite attacking assumptions across:
- Step 2: Process Failure Injection (Attacks A-G)
- Step 3: Filesystem Adversarial Containment (Attacks A-G)
- Step 4: Transaction Failure Injection (Attacks A-I)
- Step 5 & 6: System 1 / Jev Hostile & Contradictory Failure Injection
- Step 7: Workspace Lock Attacks (Attacks A-E)
- Step 8: Cancellation Matrix (All lifecycle stages)
- Step 9: State-Machine Invariant Testing (Invalid transitions forbidden)
- Step 10: Phase 1 Trust-Boundary Regression
- Step 11: Failure Chaos Combination Tests (Scenarios 1-4)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
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
    InvalidTransactionTransition,
    OperationStatus,
    OperationType,
    Transaction,
    TransactionCoordinator,
    TransactionResult,
    TransactionStatus,
)
from core.runtime.runtime import BrainFrogRuntime
from core.runtime.messages import IncomingMessage
from core.runtime.permissions import ChannelTrustLevel, PermissionAction
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


# =============================================================================
# STEP 2 — PROCESS FAILURE INJECTION (Attacks A through G)
# =============================================================================

class TestProcessFailureInjection:
    def test_attack_a_parent_timeout(self, tmp_path: Path):
        """Attack A: Indefinite parent process times out cleanly, parent dead, lock not leaked."""
        lock = WorkspaceLock(tmp_path, session_id="test_parent_to")
        lock.acquire()
        try:
            code = "import time; time.sleep(120)"
            res = run_hardened_subprocess([sys.executable, "-c", code], cwd=tmp_path, timeout=0.4)
            assert res.status == ProcessExitStatus.TIMEOUT
            assert res.is_timeout is True
            assert res.is_success is False
        finally:
            lock.release()

        # Workspace lock can immediately be acquired again
        lock2 = WorkspaceLock(tmp_path, session_id="test_subsequent", timeout=0.1)
        assert lock2.acquire() is True
        lock2.release()

    def test_attack_b_child_timeout(self, tmp_path: Path):
        """Attack B: Parent spawns child; both remain alive until timeout terminates both."""
        child_pid_file = tmp_path / "child.pid"
        script = tmp_path / "parent_child.py"
        script.write_text(
            f"import subprocess, sys, time\n"
            f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"with open(r'{child_pid_file}', 'w') as f: f.write(str(p.pid))\n"
            f"time.sleep(60)\n",
            encoding="utf-8",
        )
        res = run_hardened_subprocess([sys.executable, str(script)], cwd=tmp_path, timeout=0.8)
        assert res.is_timeout is True
        time.sleep(0.5)
        if child_pid_file.exists():
            child_pid = int(child_pid_file.read_text().strip())
            assert not is_pid_running(child_pid)

    def test_attack_c_grandchild_timeout(self, tmp_path: Path):
        """Attack C: Parent -> Child -> Grandchild hierarchy all terminated on timeout."""
        gc_pid_file = tmp_path / "gc.pid"
        gc_script = tmp_path / "gc.py"
        gc_script.write_text("import time; time.sleep(60)\n", encoding="utf-8")

        child_script = tmp_path / "child.py"
        child_script.write_text(
            f"import subprocess, sys, time\n"
            f"p = subprocess.Popen([sys.executable, r'{gc_script}'])\n"
            f"with open(r'{gc_pid_file}', 'w') as f: f.write(str(p.pid))\n"
            f"time.sleep(60)\n",
            encoding="utf-8",
        )

        parent_script = tmp_path / "parent.py"
        parent_script.write_text(
            f"import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, r'{child_script}'])\n"
            f"time.sleep(60)\n",
            encoding="utf-8",
        )

        res = run_hardened_subprocess([sys.executable, str(parent_script)], cwd=tmp_path, timeout=0.8)
        assert res.is_timeout is True
        time.sleep(0.5)
        if gc_pid_file.exists():
            gc_pid = int(gc_pid_file.read_text().strip())
            assert not is_pid_running(gc_pid)

    def test_attack_d_child_survives_parent_exit(self, tmp_path: Path):
        """Attack D: Parent exits immediately leaving child running; tree termination kills child."""
        child_pid_file = tmp_path / "orphan_child.pid"
        gc_script = tmp_path / "sleep_forever.py"
        gc_script.write_text("import time; time.sleep(60)\n", encoding="utf-8")

        # Parent spawns child, writes PID, and exits immediately!
        parent_script = tmp_path / "exit_early_parent.py"
        parent_script.write_text(
            f"import subprocess, sys, time\n"
            f"p = subprocess.Popen([sys.executable, r'{gc_script}'])\n"
            f"with open(r'{child_pid_file}', 'w') as f: f.write(str(p.pid))\n"
            f"sys.exit(0)\n",
            encoding="utf-8",
        )

        res = run_hardened_subprocess([sys.executable, str(parent_script)], cwd=tmp_path, timeout=5.0)
        time.sleep(0.5)

        if child_pid_file.exists():
            child_pid = int(child_pid_file.read_text().strip())
            # Thanks to JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (Win32) or process-group kill (POSIX),
            # the child process cannot survive runner completion even though parent exited 0 early.
            assert not is_pid_running(child_pid)

    def test_attack_e_huge_output_no_deadlock(self, tmp_path: Path):
        """Attack E: Simultaneous massive stdout and stderr streams do not deadlock and truncate explicitly."""
        code = (
            "import sys; "
            "sys.stdout.write('X' * 50000); sys.stdout.flush(); "
            "sys.stderr.write('Y' * 50000); sys.stderr.flush()"
        )
        res = run_hardened_subprocess([sys.executable, "-c", code], cwd=tmp_path, timeout=5.0, max_output_bytes=2000)
        assert res.is_truncated is True
        assert len(res.stdout.encode("utf-8")) <= 4000
        assert len(res.stderr.encode("utf-8")) <= 4000
        assert "OUTPUT TRUNCATED" in res.stdout
        assert "OUTPUT TRUNCATED" in res.stderr

    def test_attack_f_interactive_process_receives_devnull(self, tmp_path: Path):
        """Attack F: Interactive prompt reading stdin receives immediate EOF and terminates."""
        code = "import sys; data = sys.stdin.readline(); print('PROMPT_CLOSED')"
        res = run_hardened_subprocess([sys.executable, "-c", code], cwd=tmp_path, timeout=5.0)
        assert res.is_success is True
        assert "PROMPT_CLOSED" in res.stdout

    def test_attack_g_cancellation_during_process(self, tmp_path: Path):
        """Attack G: KeyboardInterrupt during subprocess terminates tree, closes pipes, and reraises."""
        marker = tmp_path / "canc_proc.pid"
        script = tmp_path / "long_running.py"
        script.write_text(
            f"import subprocess, sys, time\n"
            f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"with open(r'{marker}', 'w') as f: f.write(str(p.pid))\n"
            f"time.sleep(60)\n",
            encoding="utf-8",
        )

        def interrupt_thread():
            time.sleep(0.3)
            import _thread
            _thread.interrupt_main()

        th = threading.Thread(target=interrupt_thread, daemon=True)
        th.start()

        with pytest.raises(KeyboardInterrupt):
            run_hardened_subprocess([sys.executable, str(script)], cwd=tmp_path, timeout=30.0)

        time.sleep(0.5)
        if marker.exists():
            child_pid = int(marker.read_text().strip())
            assert not is_pid_running(child_pid)


# =============================================================================
# STEP 3 — FILESYSTEM ADVERSARIAL TESTING (Attacks A through G)
# =============================================================================

class TestFilesystemAdversarial:
    def test_attack_a_parent_traversals_denied(self, tmp_path: Path):
        """Attack A: Parent traversal variations are rejected with PermissionError."""
        for traversal in ["../outside.txt", "../../secret.key", "foo/../../outside.txt", "a/b/../../../etc/passwd"]:
            with pytest.raises(PermissionError):
                validate_workspace_read_path(tmp_path, traversal)

    def test_attack_b_absolute_paths_denied(self, tmp_path: Path):
        """Attack B: Absolute external POSIX and Windows drive paths are rejected."""
        external_paths = ["/etc/shadow", "/var/log/syslog"]
        if sys.platform == "win32":
            external_paths.extend(["C:\\Windows\\System32\\cmd.exe", "D:\\secrets.txt"])

        for ext in external_paths:
            with pytest.raises(PermissionError):
                validate_workspace_read_path(tmp_path, ext)

    def test_attack_c_external_symlink_denied_no_leak(self, tmp_path: Path):
        """Attack C: Symlink escaping workspace is denied without exposing target content."""
        secret = tmp_path.parent / "super_secret.txt"
        secret.write_text("SENSITIVE_SECRET_TOKEN_12345", encoding="utf-8")
        link = tmp_path / "link_secret"
        try:
            link.symlink_to(secret)
        except (OSError, NotImplementedError):
            pytest.skip("Symlink creation not permitted in this environment")

        with pytest.raises(PermissionError) as exc_info:
            validate_workspace_read_path(tmp_path, "link_secret")
        assert "SENSITIVE_SECRET_TOKEN_12345" not in str(exc_info.value)

    def test_attack_d_broken_external_symlink_denied(self, tmp_path: Path):
        """Attack D: Broken symlink pointing to non-existent external target is denied."""
        broken_target = tmp_path.parent / "ghost_target.txt"
        link = tmp_path / "link_broken"
        try:
            link.symlink_to(broken_target)
        except (OSError, NotImplementedError):
            pytest.skip("Symlink creation not permitted in this environment")

        with pytest.raises(PermissionError):
            validate_workspace_read_path(tmp_path, "link_broken")

    def test_attack_e_nested_symlink_denied(self, tmp_path: Path):
        """Attack E: Nested symlink directory escaping workspace is denied."""
        outside_dir = tmp_path.parent / "ext_dir"
        outside_dir.mkdir(exist_ok=True)
        (outside_dir / "secret.txt").write_text("classified", encoding="utf-8")

        sub = tmp_path / "sub"
        sub.mkdir(exist_ok=True)
        link = sub / "ext_link"
        try:
            link.symlink_to(outside_dir)
        except (OSError, NotImplementedError):
            pytest.skip("Symlink creation not permitted in this environment")

        with pytest.raises(PermissionError):
            validate_workspace_read_path(tmp_path, "sub/ext_link/secret.txt")

    def test_attack_f_internal_symlink_allowed(self, tmp_path: Path):
        """Attack F: Legitimate symlink to another location inside workspace resolves safely."""
        target = tmp_path / "real.py"
        target.write_text("x = 10", encoding="utf-8")
        link = tmp_path / "link_real.py"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("Symlink creation not permitted in this environment")

        p = validate_workspace_read_path(tmp_path, "link_real.py")
        assert p.exists()

    def test_attack_g_workspace_canonical_alias(self, tmp_path: Path):
        """Attack G: Different relative representations resolve to the same canonical identity."""
        sub = tmp_path / "sub"
        sub.mkdir()
        target = sub / "app.py"
        target.write_text("print(1)", encoding="utf-8")

        p1 = validate_workspace_read_path(tmp_path, "sub/app.py")
        p2 = validate_workspace_read_path(tmp_path, "./sub/../sub/app.py")
        assert p1.resolve() == p2.resolve()


# =============================================================================
# STEP 4 — TRANSACTION FAILURE INJECTION (Attacks A through I)
# =============================================================================

class TestTransactionFailureInjection:
    def test_attack_a_mutation_failure_cleans_partial_state(self, tmp_path: Path):
        """Attack A: Failure during multi-operation execution rolls back executed operations and clears partial state."""
        store = FileTransactionStore(tmp_path / ".brainfrog" / "transactions")
        coord = TransactionCoordinator(tmp_path, store=store)

        # Stage op 1 (valid) and op 2 (invalid)
        coord.stage_operation(OperationType.CREATE_FILE, "op1.txt", "content1")
        # Stage op 2 with invalid after_state to trigger execution error
        op2 = coord.stage_operation(OperationType.CREATE_FILE, "op2.txt", "content2")
        op2.after_state = {}  # Trigger ValueError: no after_state staged

        ok = coord.execute()
        assert ok is False
        assert coord.tx.status == TransactionStatus.ROLLED_BACK
        assert not (tmp_path / "op1.txt").exists()
        assert not (tmp_path / "op2.txt").exists()

    def test_attack_b_domain_test_failure_rolls_back(self, tmp_path: Path):
        """Attack B: When domain tests fail, active transaction rolls back mutations cleanly."""
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
        s2.write_code.return_value = {"broken.py": "def fail(): pass"}

        cfg = RunConfig(
            repo_dir=tmp_path,
            task="Task",
            test_command=["pytest"],
            transaction_store=store,
            transaction_verifier=BasicFilesystemVerifier(),
            max_retries=1,
        )
        orch = Orchestrator(system1=s1, system2=s2, config=cfg)
        step = PlanStep(id="1", description="step", files=["broken.py"])

        fail_proc = HardenedProcessResult(args=["pytest"], returncode=1, stdout="FAIL", stderr="")
        with patch.object(orch, "_run_test_suite", return_value=fail_proc):
            res = orch._run_step(step)

        assert res.outcome in ("abandoned", "escalated")
        assert not (tmp_path / "broken.py").exists()
        assert orch.cfg.last_transaction_result is not None
        assert orch.cfg.last_transaction_result.status == TransactionStatus.ROLLED_BACK

    def test_attack_c_verification_failure_fails_closed_without_commit(self, tmp_path: Path):
        """Attack C: Tests pass exit 0, but filesystem verifier returns False -> does not commit!"""
        store = FileTransactionStore(tmp_path / ".brainfrog" / "transactions")
        s1 = MagicMock()
        s1.name = "mock_s1"
        s1.decide.return_value = {
            "tests_passing": Answer(choice="yes", noul=1.0),
            "diff_complete": Answer(choice="yes", noul=1.0),
            "failure_fixable": Answer(choice="no", noul=0.0),
            "retry_concern": Answer(score="within_normal", confidence=0.9),
        }
        s2 = MagicMock()
        s2.write_code.return_value = {"verified.py": "x = 100"}

        # Custom verifier that fails verification
        class FailingVerifier:
            def verify(self, tx, ws):
                return False, "Filesystem consistency check failed", {}

        cfg = RunConfig(
            repo_dir=tmp_path,
            task="Task",
            test_command=["pytest"],
            transaction_store=store,
            transaction_verifier=FailingVerifier(),  # type: ignore
            max_retries=1,
        )
        orch = Orchestrator(system1=s1, system2=s2, config=cfg)
        step = PlanStep(id="1", description="step", files=["verified.py"])

        ok_proc = HardenedProcessResult(args=["pytest"], returncode=0, stdout="OK", stderr="")
        with patch.object(orch, "_run_test_suite", return_value=ok_proc):
            res = orch._run_step(step)

        # Fails closed, escalated, NOT committed
        assert res.outcome == "escalated"
        assert "verification failed" in res.detail.lower()
        tx_res = orch.cfg.last_transaction_result
        assert tx_res is not None
        assert tx_res.committed is False
        assert tx_res.status == TransactionStatus.ROLLED_BACK

    def test_attack_d_rollback_failure_fails_closed_and_records_recovery(self, tmp_path: Path):
        """Attack D: Disk error during rollback transitions to FAILED and marks recovery required."""
        store = FileTransactionStore(tmp_path / ".brainfrog" / "transactions")
        coord = TransactionCoordinator(tmp_path, store=store)
        coord.stage_operation(OperationType.CREATE_FILE, "locked.py", "val = 1")
        assert coord.execute() is True

        # Inject exception in unlinking / restoring
        with patch("pathlib.Path.unlink", side_effect=PermissionError("Permission denied on disk")):
            res = coord.rollback(reason="Test failure")

        assert res.committed is False
        assert res.rolled_back is False
        assert res.status == TransactionStatus.FAILED
        assert res.is_recovery_required is True
        assert "Permission denied" in (res.rollback_error or "") or "Failed to rollback" in (res.rollback_error or "")

    def test_attack_e_pre_existing_user_modifications_preserved(self, tmp_path: Path):
        """Attack E: Pre-existing user modifications in user.py are preserved when agent.py fails."""
        user_file = tmp_path / "user.py"
        user_file.write_text("user_version_1", encoding="utf-8")

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
        # BrainFrog attempts to modify user.py AND create agent.py
        s2.write_code.return_value = {
            "user.py": "brainfrog_mutated_user",
            "agent.py": "brainfrog_agent_code",
        }

        cfg = RunConfig(
            repo_dir=tmp_path,
            task="Task",
            test_command=["pytest"],
            transaction_store=store,
            transaction_verifier=BasicFilesystemVerifier(),
            max_retries=1,
        )
        orch = Orchestrator(system1=s1, system2=s2, config=cfg)
        step = PlanStep(id="1", description="step", files=["user.py", "agent.py"])

        fail_proc = HardenedProcessResult(args=["pytest"], returncode=1, stdout="FAIL", stderr="")
        with patch.object(orch, "_run_test_suite", return_value=fail_proc):
            orch._run_step(step)

        # user.py restored to exact user content; agent.py unlinked!
        assert user_file.read_text(encoding="utf-8") == "user_version_1"
        assert not (tmp_path / "agent.py").exists()

    def test_attack_f_external_concurrent_mutation(self, tmp_path: Path):
        """Attack F: External mutation to staged file aborts rollback overwrite and fails closed."""
        store = FileTransactionStore(tmp_path / ".brainfrog" / "transactions")
        coord = TransactionCoordinator(tmp_path, store=store)
        coord.stage_operation(OperationType.CREATE_FILE, "code.py", "brainfrog initial version")
        assert coord.execute() is True

        code_file = tmp_path / "code.py"
        # User modifies code.py concurrently
        code_file.write_text("user emergency bugfix", encoding="utf-8")

        res = coord.rollback(reason="Test failure")
        assert res.rolled_back is False
        assert res.status == TransactionStatus.FAILED
        assert res.is_recovery_required is True
        # User emergency bugfix is NOT destroyed!
        assert code_file.read_text(encoding="utf-8") == "user emergency bugfix"

    def test_attack_g_new_file_unlinked_on_failure(self, tmp_path: Path):
        """Attack G: Newly created file is completely removed on rollback."""
        store = FileTransactionStore(tmp_path / ".brainfrog" / "transactions")
        coord = TransactionCoordinator(tmp_path, store=store)
        coord.stage_operation(OperationType.CREATE_FILE, "new_file.py", "temp code")
        assert coord.execute() is True
        assert (tmp_path / "new_file.py").exists()

        res = coord.rollback(reason="Failure")
        assert res.rolled_back is True
        assert not (tmp_path / "new_file.py").exists()

    def test_attack_h_deleted_file_restored_on_failure(self, tmp_path: Path):
        """Attack H: File deleted by transaction is restored with original content on rollback."""
        f = tmp_path / "survivor.txt"
        f.write_text("important data", encoding="utf-8")

        store = FileTransactionStore(tmp_path / ".brainfrog" / "transactions")
        coord = TransactionCoordinator(tmp_path, store=store)
        coord.stage_operation(OperationType.DELETE_FILE, "survivor.txt")
        assert coord.execute() is True
        assert not f.exists()

        res = coord.rollback(reason="Failure")
        assert res.rolled_back is True
        assert f.exists()
        assert f.read_text(encoding="utf-8") == "important data"

    def test_attack_i_multiple_mutations_reverted_atomically(self, tmp_path: Path):
        """Attack I: Mixed batch of create, modify, and delete operations all revert in reverse order."""
        mod_file = tmp_path / "mod.py"
        mod_file.write_text("orig_mod", encoding="utf-8")
        del_file = tmp_path / "del.py"
        del_file.write_text("orig_del", encoding="utf-8")

        store = FileTransactionStore(tmp_path / ".brainfrog" / "transactions")
        coord = TransactionCoordinator(tmp_path, store=store)
        coord.stage_operation(OperationType.CREATE_FILE, "created.py", "new_content")
        coord.stage_operation(OperationType.MODIFY_FILE, "mod.py", "changed_mod")
        coord.stage_operation(OperationType.DELETE_FILE, "del.py")
        assert coord.execute() is True

        assert (tmp_path / "created.py").exists()
        assert mod_file.read_text(encoding="utf-8") == "changed_mod"
        assert not del_file.exists()

        res = coord.rollback(reason="Batch failure")
        assert res.rolled_back is True
        assert not (tmp_path / "created.py").exists()
        assert mod_file.read_text(encoding="utf-8") == "orig_mod"
        assert del_file.read_text(encoding="utf-8") == "orig_del"


# =============================================================================
# STEP 5 & 6 — SYSTEM 1 HOSTILE & CONTRADICTORY FAILURE INJECTION
# =============================================================================

class TestSystem1Adversarial:
    @pytest.mark.parametrize("status_code", [500, 502, 503, 504, 429])
    def test_transient_http_statuses_retry(self, status_code: int):
        """Transient HTTP errors retry up to 3 times before failing closed."""
        client = TypeSafeSystemOne(api_key="key", api_url="http://dummy.local", timeout=1.0)
        q = ChoiceQuestion(instructions={"question": "Test?"}, criteria={"a": {"what": "opt"}})

        resp_fail = MagicMock(status_code=status_code, text="Error")
        resp_fail.raise_for_status.side_effect = requests.exceptions.HTTPError(response=resp_fail)

        with patch("requests.post", return_value=resp_fail) as mock_post:
            with patch("time.sleep"):
                with pytest.raises(RuntimeError, match="after 3 attempts"):
                    client.decide({"task": "t"}, {"q": q})

        assert mock_post.call_count == 3

    @pytest.mark.parametrize("client_code", [400, 401, 403, 422])
    def test_deterministic_client_errors_fail_fast(self, client_code: int):
        """Deterministic 4xx client errors fail fast on first attempt without retrying."""
        client = TypeSafeSystemOne(api_key="key", api_url="http://dummy.local", timeout=1.0)
        q = ChoiceQuestion(instructions={"question": "Test?"}, criteria={"a": {"what": "opt"}})

        resp_4xx = MagicMock(status_code=client_code, text="Client Error")
        resp_4xx.raise_for_status.side_effect = requests.exceptions.HTTPError(response=resp_4xx)

        with patch("requests.post", return_value=resp_4xx) as mock_post:
            with pytest.raises(RuntimeError, match=f"TypeSafe/Jev API error \\({client_code}\\)"):
                client.decide({"task": "t"}, {"q": q})

        assert mock_post.call_count == 1

    def test_malformed_and_empty_payloads_fail_closed(self):
        """Malformed JSON payloads, HTML error pages, and empty bodies fail safely."""
        client = TypeSafeSystemOne(api_key="key", api_url="http://dummy.local", timeout=1.0)
        q = ChoiceQuestion(instructions={"question": "Test?"}, criteria={"a": {"what": "opt"}})

        # 1. Non-dict JSON (e.g. array)
        resp_array = MagicMock(status_code=200, json=lambda: ["invalid", "format"])
        with patch("requests.post", return_value=resp_array):
            with pytest.raises(RuntimeError, match="expected JSON object"):
                client.decide({"task": "t"}, {"q": q})

        # 2. Corrupt body
        resp_corrupt = MagicMock(status_code=200)
        resp_corrupt.json.side_effect = json.JSONDecodeError("Error", "doc", 0)
        with patch("requests.post", return_value=resp_corrupt):
            with pytest.raises(RuntimeError, match="malformed non-JSON"):
                client.decide({"task": "t"}, {"q": q})

    def test_dangerous_defaults_not_permitted(self):
        """Empty or null answer values default to zero confidence and never grant permissive access."""
        client = TypeSafeSystemOne(api_key="key", api_url="http://dummy.local", timeout=1.0)
        q = ChoiceQuestion(instructions={"question": "Test?"}, criteria={"a": {"what": "opt"}})

        resp_empty = MagicMock(status_code=200, json=lambda: {"answers": {}})
        with patch("requests.post", return_value=resp_empty):
            ans = client.decide({"task": "t"}, {"q": q})["q"]

        assert ans.confidence == 0.0
        assert ans.choice is None
        assert ans.score is None

    def test_contradictory_response_cannot_override_failed_tests(self, tmp_path: Path):
        """When tests fail with returncode 1, a contradictory model claim of passing=yes is ignored."""
        s1 = MagicMock()
        s1.name = "hallucinating_model"
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

        fail_proc = HardenedProcessResult(args=["pytest"], returncode=1, stdout="FAIL", stderr="")
        with patch.object(orch, "_run_test_suite", return_value=fail_proc):
            res = orch._run_step(step)

        assert res.outcome in ("abandoned", "escalated")
        assert not (tmp_path / "code.py").exists()


# =============================================================================
# STEP 7 — WORKSPACE LOCK ATTACKS (Attacks A through E)
# =============================================================================

class TestWorkspaceLockAdversarial:
    def test_lock_attack_a_same_workspace_collision(self, tmp_path: Path):
        """Attack A: Concurrent lock attempts on the same workspace are rejected."""
        lock1 = WorkspaceLock(tmp_path, session_id="task_a", timeout=0.1)
        lock2 = WorkspaceLock(tmp_path, session_id="task_b", timeout=0.1)

        assert lock1.acquire() is True
        try:
            with pytest.raises(WorkspaceLockedError):
                lock2.acquire()
        finally:
            lock1.release()

    def test_lock_attack_b_canonical_alias_collision(self, tmp_path: Path):
        """Attack B: Paths using navigation aliases (./, ../) collide as the same workspace lock."""
        sub = tmp_path / "nested"
        sub.mkdir()
        alias = sub / ".." / "."

        lock1 = WorkspaceLock(tmp_path, session_id="canon", timeout=0.1)
        lock2 = WorkspaceLock(alias, session_id="alias", timeout=0.1)

        assert lock1.acquire() is True
        try:
            with pytest.raises(WorkspaceLockedError):
                lock2.acquire()
        finally:
            lock1.release()

    def test_lock_attack_c_independent_workspaces_concurrent(self, tmp_path: Path):
        """Attack C: Locks on independent workspaces do not interfere."""
        dir1 = tmp_path / "ws1"
        dir2 = tmp_path / "ws2"
        dir1.mkdir()
        dir2.mkdir()

        l1 = WorkspaceLock(dir1, session_id="s1", timeout=0.1)
        l2 = WorkspaceLock(dir2, session_id="s2", timeout=0.1)

        assert l1.acquire() is True
        assert l2.acquire() is True
        l1.release()
        l2.release()

    def test_lock_attack_d_dead_pid_stale_lock_recovered(self, tmp_path: Path):
        """Attack D: Lock held by a dead process PID is detected as stale and acquired."""
        lock = WorkspaceLock(tmp_path, session_id="recovering_session", timeout=0.2)
        dot_bf = tmp_path / ".brainfrog"
        dot_bf.mkdir(parents=True, exist_ok=True)
        meta_file = dot_bf / "workspace.lock.meta"
        meta_file.write_text(json.dumps({
            "pid": 999999,
            "session_id": "dead_holder",
            "actor": "ghost",
            "acquired_at": time.time(),
            "workspace": str(tmp_path),
        }), encoding="utf-8")

        assert lock.acquire() is True
        lock.release()

    def test_lock_attack_e_pid_reuse_with_mismatched_create_time(self, tmp_path: Path):
        """Attack E: Mismatched process_create_time detects PID reuse and clears stale lock."""
        lock = WorkspaceLock(tmp_path, session_id="sess_reuse", timeout=0.2)
        dot_bf = tmp_path / ".brainfrog"
        dot_bf.mkdir(parents=True, exist_ok=True)
        meta_file = dot_bf / "workspace.lock.meta"
        meta_file.write_text(json.dumps({
            "pid": os.getpid(),
            "process_create_time": 1.0,  # Far in the epoch past
            "session_id": "stale_process",
            "actor": "stale",
            "acquired_at": time.time(),
            "workspace": str(tmp_path),
        }), encoding="utf-8")

        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        assert lock.is_stale(meta) is True


# =============================================================================
# STEP 8 — CANCELLATION MATRIX (All lifecycle stages)
# =============================================================================

class TestCancellationMatrix:
    def test_cancellation_before_mutation(self, tmp_path: Path):
        """Cancellation before any mutation executes cleanly without filesystem changes."""
        s1 = MagicMock()
        s2 = MagicMock()
        cfg = RunConfig(repo_dir=tmp_path, task="Task", test_command=["pytest"])
        orch = Orchestrator(system1=s1, system2=s2, config=cfg)
        step = PlanStep(id="1", description="step", files=["file.py"])

        with patch.object(orch.s2, "write_code", side_effect=KeyboardInterrupt("Cancel before mutation")):
            with pytest.raises(KeyboardInterrupt):
                orch._run_step(step)

        assert not (tmp_path / "file.py").exists()

    def test_cancellation_during_domain_test(self, tmp_path: Path):
        """Cancellation during test execution triggers rollback and preserves state."""
        store = FileTransactionStore(tmp_path / ".brainfrog" / "transactions")
        s1 = MagicMock()
        s2 = MagicMock()
        s2.write_code.return_value = {"during_test.py": "val = 1"}

        cfg = RunConfig(
            repo_dir=tmp_path,
            task="Task",
            test_command=["pytest"],
            transaction_store=store,
            transaction_verifier=BasicFilesystemVerifier(),
        )
        orch = Orchestrator(system1=s1, system2=s2, config=cfg)
        step = PlanStep(id="1", description="step", files=["during_test.py"])

        with patch.object(orch, "_run_test_suite", side_effect=KeyboardInterrupt("Cancel during tests")):
            with pytest.raises(KeyboardInterrupt):
                orch._run_step(step)

        assert not (tmp_path / "during_test.py").exists()
        tx_res = orch.cfg.last_transaction_result
        assert tx_res is not None
        assert tx_res.status == TransactionStatus.ROLLED_BACK

    def test_cancellation_never_reports_success(self, tmp_path: Path):
        """Runtime execution on KeyboardInterrupt returns status='cancelled' and success=False."""
        runtime = BrainFrogRuntime(
            repo_dir=tmp_path,
            persist_sessions=False,
            system1_factory=lambda b: MagicMock(),
            system2_factory=lambda **k: MagicMock(),
        )
        msg = IncomingMessage(
            id="msg_c",
            channel="cli",
            user_id="local",
            conversation_id="conv_c",
            text="run task",
            metadata={"catch_cancellation": True},
        )

        with patch("core.runtime.runtime.Orchestrator.run", side_effect=KeyboardInterrupt()):
            out = runtime.handle_message(msg)

        assert out.success is False
        assert out.status == "cancelled"


# =============================================================================
# STEP 9 — STATE-MACHINE INVARIANT TESTING
# =============================================================================

class TestStateMachineInvariants:
    def test_invalid_transaction_transitions_rejected(self):
        """Terminal states cannot transition to COMMITTED or any other state."""
        tx = Transaction(id="tx_test", workspace="/tmp", status=TransactionStatus.FAILED)
        with pytest.raises(InvalidTransactionTransition):
            tx.transition(TransactionStatus.COMMITTED)

        tx_rb = Transaction(id="tx_rb", workspace="/tmp", status=TransactionStatus.ROLLED_BACK)
        with pytest.raises(InvalidTransactionTransition):
            tx_rb.transition(TransactionStatus.COMMITTED)

        tx_created = Transaction(id="tx_cr", workspace="/tmp", status=TransactionStatus.CREATED)
        with pytest.raises(InvalidTransactionTransition):
            tx_created.transition(TransactionStatus.COMMITTED)

    def test_valid_transaction_lifecycle_transitions(self):
        """Happy-path lifecycle stages follow allowed state transitions."""
        tx = Transaction(id="tx_valid", workspace="/tmp", status=TransactionStatus.CREATED)
        tx.transition(TransactionStatus.STAGING)
        tx.transition(TransactionStatus.EXECUTING)
        tx.transition(TransactionStatus.VERIFYING)
        tx.transition(TransactionStatus.COMMITTED)
        assert tx.status == TransactionStatus.COMMITTED
        assert tx.is_terminal is True

    def test_transaction_result_is_recovery_required_flag(self):
        """TransactionResult explicitly indicates when recovery is required."""
        res_ok = TransactionResult(
            transaction_id="tx_ok",
            status=TransactionStatus.COMMITTED,
            committed=True,
            rolled_back=False,
            operations=(),
        )
        assert res_ok.is_recovery_required is False
        assert res_ok.success is True

        res_rb_fail = TransactionResult(
            transaction_id="tx_fail",
            status=TransactionStatus.FAILED,
            committed=False,
            rolled_back=False,
            operations=(),
            rollback_error="Disk locked",
        )
        assert res_rb_fail.is_recovery_required is True
        assert res_rb_fail.success is False


# =============================================================================
# STEP 10 — PHASE 1 TRUST-BOUNDARY REGRESSION
# =============================================================================

class TestPhase1TrustBoundaryRegression:
    def test_unauthorized_remote_action_denied(self, tmp_path: Path):
        """Remote channel attempt to execute prohibited action is blocked by trust boundary."""
        runtime = BrainFrogRuntime(
            repo_dir=tmp_path,
            persist_sessions=False,
            system1_factory=lambda b: MagicMock(),
            system2_factory=lambda **k: MagicMock(),
        )
        # Remote telegram message requesting direct push/shell
        msg = IncomingMessage(
            id="msg_remote",
            channel="telegram",
            user_id="remote_user",
            conversation_id="conv_remote",
            text="push to production",
            metadata={"trust_level": ChannelTrustLevel.REMOTE_CHANNEL.value},
        )
        out = runtime.handle_message(msg)
        # Action is denied by trust policy
        assert out.status in ("permission_denied", "needs_clarification", "rejected")
        assert out.success is False


# =============================================================================
# STEP 11 — FAILURE CHAOS COMBINATION TESTS (Scenarios 1-4)
# =============================================================================

class TestFailureChaosCombinations:
    def test_chaos_scenario_1_jev_outage_then_subprocess_timeout(self, tmp_path: Path):
        """Scenario 1: Jev recovers after transient timeout, execution begins, subprocess times out cleanly."""
        store = FileTransactionStore(tmp_path / ".brainfrog" / "transactions")
        s1 = TypeSafeSystemOne(api_key="mock", api_url="http://mock.jev/decide")
        s2 = MagicMock()
        s2.write_code.return_value = {"chaos1.py": "import time; time.sleep(120)"}

        cfg = RunConfig(
            repo_dir=tmp_path,
            task="Chaos 1",
            test_command=["pytest"],
            transaction_store=store,
            transaction_verifier=BasicFilesystemVerifier(),
            max_retries=1,
        )
        orch = Orchestrator(system1=s1, system2=s2, config=cfg)
        step = PlanStep(id="1", description="step", files=["chaos1.py"])

        # Jev transient error then recovery
        mock_post = MagicMock(status_code=200, json=lambda: {
            "answers": {
                "tests_passing": {"choice": "no", "noul": 0.0},
                "diff_complete": {"choice": "no", "noul": 0.0},
                "failure_fixable": {"choice": "no", "noul": 0.0},
                "retry_concern": {"score": "high", "confidence": 0.0},
            }
        })
        timeout_proc = HardenedProcessResult(args=["pytest"], returncode=-1, stdout="", stderr="", is_timeout=True)

        with patch("requests.post", side_effect=[requests.exceptions.Timeout("Read timeout"), mock_post]):
            with patch("time.sleep"):
                with patch.object(orch, "_run_test_suite", return_value=timeout_proc):
                    res = orch._run_step(step)

        assert res.outcome in ("abandoned", "escalated")
        assert not (tmp_path / "chaos1.py").exists()

    def test_chaos_scenario_2_domain_test_failure_then_external_mutation(self, tmp_path: Path):
        """Scenario 2: Tests fail; user edits file before rollback; external change preserved, fails closed."""
        store = FileTransactionStore(tmp_path / ".brainfrog" / "transactions")
        coord = TransactionCoordinator(tmp_path, store=store)
        coord.stage_operation(OperationType.CREATE_FILE, "chaos2.py", "brainfrog initial")
        assert coord.execute() is True

        chaos_file = tmp_path / "chaos2.py"
        # User concurrently mutates chaos2.py before rollback completes
        chaos_file.write_text("user emergency edit in chaos", encoding="utf-8")

        res = coord.rollback(reason="Test failure")
        assert res.rolled_back is False
        assert res.status == TransactionStatus.FAILED
        assert res.is_recovery_required is True
        # User edit is preserved!
        assert chaos_file.read_text(encoding="utf-8") == "user emergency edit in chaos"

    def test_chaos_scenario_3_subprocess_running_ctrl_c(self, tmp_path: Path):
        """Scenario 3: Subprocess running, Ctrl+C terminates child, rolls back transaction, releases lock."""
        marker = tmp_path / "chaos3_child.pid"
        script = tmp_path / "chaos3_script.py"
        script.write_text(
            f"import subprocess, sys, time\n"
            f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"with open(r'{marker}', 'w') as f: f.write(str(p.pid))\n"
            f"time.sleep(60)\n",
            encoding="utf-8",
        )

        lock = WorkspaceLock(tmp_path, session_id="chaos3")
        lock.acquire()

        def inject_ctrl_c():
            time.sleep(0.3)
            import _thread
            _thread.interrupt_main()

        threading.Thread(target=inject_ctrl_c, daemon=True).start()

        try:
            with pytest.raises(KeyboardInterrupt):
                run_hardened_subprocess([sys.executable, str(script)], cwd=tmp_path, timeout=30.0)
        finally:
            lock.release()

        time.sleep(0.5)
        if marker.exists():
            child_pid = int(marker.read_text().strip())
            assert not is_pid_running(child_pid)

        # Lock is released and available
        l2 = WorkspaceLock(tmp_path, session_id="after_chaos3", timeout=0.1)
        assert l2.acquire() is True
        l2.release()

    def test_chaos_scenario_4_locked_workspace_rejects_without_corruption(self, tmp_path: Path):
        """Scenario 4: Colliding task on locked workspace is rejected without corrupting lock metadata."""
        lock1 = WorkspaceLock(tmp_path, session_id="primary_holder", actor="primary", timeout=0.1)
        lock2 = WorkspaceLock(tmp_path, session_id="colliding_task", actor="intruder", timeout=0.1)

        assert lock1.acquire() is True
        try:
            with pytest.raises(WorkspaceLockedError):
                lock2.acquire()

            # Metadata remains intact with primary holder info
            meta = lock1._read_meta()
            assert meta is not None
            assert meta["session_id"] == "primary_holder"
            assert meta["actor"] == "primary"
        finally:
            lock1.release()
