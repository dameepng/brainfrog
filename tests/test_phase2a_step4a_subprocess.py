"""Phase 2A Step 4A — Subprocess Security Hardening Tests.

Verifies:
1. Default runner policy does not enable shell execution on any platform.
2. List-form arguments containing shell metacharacters remain literal arguments
   and never execute secondary commands.
3. Explicit shell invocation requires an explicit opt-in (allow_shell=True)
   and a valid command representation (flat string, no lists).
4. Invalid or ambiguous combinations fail closed (flat string with shell=False,
   list with shell=True, None elements, non-string types, empty commands).
5. Existing timeout, cancellation, output limits, process-tree cleanup,
   and typed result behaviors remain intact.
6. Orchestrator Git and test-runner call sites work cleanly under the new policy.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from core.runtime.process import (
    HardenedProcessResult,
    ProcessExitStatus,
    run_hardened_subprocess,
)
from orchestrator import _run


class TestSubprocessShellPolicy:
    """Validate that shell execution is strictly opt-in and safe across platforms."""

    def test_default_does_not_enable_shell_on_any_platform(self, tmp_path: Path):
        """Default invocation does NOT use a shell; shell builtins fail with execution error."""
        if sys.platform == "win32":
            # 'ver' is an internal cmd.exe builtin with no standalone binary on PATH
            res = run_hardened_subprocess(["ver"], cwd=tmp_path)
            assert res.is_success is False
            assert res.status == ProcessExitStatus.EXECUTION_ERROR
            assert "Spawn Error" in (res.error or "")
        else:
            # POSIX internal shell builtin with no binary
            res = run_hardened_subprocess(["type"], cwd=tmp_path)
            assert res.is_success is False
            assert res.status == ProcessExitStatus.EXECUTION_ERROR

        # Discrete arguments to a real executable succeed
        script = tmp_path / "check_shell.py"
        script.write_text("import sys; print('no-shell-active')", encoding="utf-8")
        cmd = [sys.executable, "check_shell.py"]
        ok_res = run_hardened_subprocess(cmd, cwd=tmp_path)
        assert ok_res.is_success is True
        assert ok_res.status == ProcessExitStatus.SUCCESS
        assert "no-shell-active" in ok_res.stdout

    def test_list_args_with_cmd_metacharacters_remain_literal_without_secondary_execution(
        self, tmp_path: Path
    ):
        """List arguments containing shell metacharacters are treated literally without command injection."""
        sentinel_marker = tmp_path / "injected_secondary.txt"
        assert not sentinel_marker.exists()

        # Attempt command chaining via '&' with redirection to sentinel file
        malicious_arg = f"foo&dir>{sentinel_marker}"
        script = tmp_path / "print_arg.py"
        script.write_text("import sys; print('ARG:', sys.argv[1])", encoding="utf-8")
        cmd = [
            sys.executable,
            "print_arg.py",
            malicious_arg,
        ]

        res = run_hardened_subprocess(cmd, cwd=tmp_path)
        assert res.is_success is True
        assert not sentinel_marker.exists(), "Secondary command executed via shell injection!"
        assert f"ARG: {malicious_arg}" in res.stdout

        # Attempt redirection via '>'
        redir_marker = tmp_path / "redirect_marker.txt"
        redir_arg = f"sample>{redir_marker}"
        cmd_redir = [
            sys.executable,
            "print_arg.py",
            redir_arg,
        ]
        res_redir = run_hardened_subprocess(cmd_redir, cwd=tmp_path)
        assert res_redir.is_success is True
        assert not redir_marker.exists(), "Output redirection occurred outside subprocess boundary!"
        assert f"ARG: {redir_arg}" in res_redir.stdout

        # Attempt pipe chaining via '|'
        pipe_arg = "data|findstr something"
        cmd_pipe = [
            sys.executable,
            "print_arg.py",
            pipe_arg,
        ]
        res_pipe = run_hardened_subprocess(cmd_pipe, cwd=tmp_path)
        assert res_pipe.is_success is True
        assert f"ARG: {pipe_arg}" in res_pipe.stdout

    def test_explicit_shell_opt_in_requires_string_and_valid_representation(self, tmp_path: Path):
        """Explicit shell invocation requires allow_shell=True and a flat string command."""
        # Valid explicit shell invocation
        if sys.platform == "win32":
            res = run_hardened_subprocess("echo explicit_win_shell", cwd=tmp_path, allow_shell=True)
        else:
            res = run_hardened_subprocess("echo explicit_posix_shell", cwd=tmp_path, allow_shell=True)
        assert res.is_success is True
        assert "explicit" in res.stdout

        # Passing a list with allow_shell=True is rejected as ambiguous
        with pytest.raises(ValueError, match="requires a command string, not a list or sequence"):
            run_hardened_subprocess(["echo", "ambiguous_list"], cwd=tmp_path, allow_shell=True)

        # Null bytes in command string rejected
        with pytest.raises(ValueError, match="null bytes"):
            run_hardened_subprocess("echo hello\0evil", cwd=tmp_path, allow_shell=True)

    def test_ambiguous_and_invalid_combinations_fail_closed(self, tmp_path: Path):
        """Runner rejects ambiguous string commands when shell=False and invalid types."""
        # Flat string with allow_shell=False (default) is rejected
        with pytest.raises(ValueError, match="rejects flat string commands"):
            run_hardened_subprocess("python --version", cwd=tmp_path)

        with pytest.raises(ValueError, match="rejects flat string commands"):
            run_hardened_subprocess("python --version", cwd=tmp_path, allow_shell=False)

        # Non-sequence command type
        with pytest.raises(ValueError, match="must be a sequence of arguments"):
            run_hardened_subprocess(12345, cwd=tmp_path)  # type: ignore

        # None elements in list
        with pytest.raises(ValueError, match="cannot be None"):
            run_hardened_subprocess([sys.executable, None], cwd=tmp_path)  # type: ignore

        # Invalid element types in list
        with pytest.raises(ValueError, match="must be str or PathLike"):
            run_hardened_subprocess([sys.executable, 999], cwd=tmp_path)  # type: ignore

        # Empty command representations return typed failure
        empty_res = run_hardened_subprocess([], cwd=tmp_path)
        assert empty_res.returncode == 1
        assert "Empty command provided" in empty_res.stderr

        empty_str_res = run_hardened_subprocess("", cwd=tmp_path, allow_shell=True)
        assert empty_str_res.returncode == 1
        assert "Empty command provided" in empty_str_res.stderr

        none_res = run_hardened_subprocess(None, cwd=tmp_path)  # type: ignore
        assert none_res.returncode == 1
        assert "Empty command provided" in none_res.stderr

        # Invalid timeouts fail closed
        with pytest.raises(ValueError, match="finite, positive timeout"):
            run_hardened_subprocess([sys.executable, "--version"], cwd=tmp_path, timeout=None)  # type: ignore

        with pytest.raises(ValueError, match="finite, positive timeout"):
            run_hardened_subprocess([sys.executable, "--version"], cwd=tmp_path, timeout=0.0)

        with pytest.raises(ValueError, match="finite, positive timeout"):
            run_hardened_subprocess([sys.executable, "--version"], cwd=tmp_path, timeout=-10.0)


class TestSubprocessLifecyclePreservation:
    """Validate that timeout, cancellation, bounds, and exit semantics remain intact."""

    def test_timeout_terminates_and_marks_result(self, tmp_path: Path):
        """Hanging process is killed on timeout and classified as ProcessExitStatus.TIMEOUT."""
        script = tmp_path / "sleep.py"
        script.write_text("import time; time.sleep(10)", encoding="utf-8")
        res = run_hardened_subprocess([sys.executable, "sleep.py"], cwd=tmp_path, timeout=0.3)
        assert res.is_timeout is True
        assert res.is_success is False
        assert res.status == ProcessExitStatus.TIMEOUT
        assert res.returncode in (-1, 124)

    def test_cancellation_terminates_and_raises(self, tmp_path: Path):
        """Active cancellation token terminates process and raises KeyboardInterrupt."""
        cancel_ev = threading.Event()
        script = tmp_path / "cancel_sleep.py"
        script.write_text("import time; time.sleep(10)", encoding="utf-8")

        def cancel_soon():
            time.sleep(0.1)
            cancel_ev.set()

        threading.Thread(target=cancel_soon, daemon=True).start()
        with pytest.raises(KeyboardInterrupt, match="cancelled by cancellation token"):
            run_hardened_subprocess(
                [sys.executable, "cancel_sleep.py"],
                cwd=tmp_path,
                timeout=5.0,
                cancel_event=cancel_ev,
            )

    def test_bounded_output_truncates_large_stream(self, tmp_path: Path):
        """Excessive stdout is capped to max_output_bytes with truncation banner."""
        script = tmp_path / "large.py"
        script.write_text("import sys; sys.stdout.write('A' * 5000)", encoding="utf-8")
        res = run_hardened_subprocess(
            [sys.executable, "large.py"],
            cwd=tmp_path,
            timeout=5.0,
            max_output_bytes=1000,
        )
        assert res.is_truncated is True
        assert "OUTPUT TRUNCATED" in res.stdout
        assert len(res.stdout.encode("utf-8")) <= 1500

    def test_exit_status_distinguishes_success_non_zero_and_spawn_error(self, tmp_path: Path):
        """Typed status correctly differentiates success, non-zero exit, and spawn failure."""
        ok_script = tmp_path / "ok.py"
        ok_script.write_text("import sys; sys.exit(0)", encoding="utf-8")
        ok = run_hardened_subprocess([sys.executable, "ok.py"], cwd=tmp_path)
        assert ok.status == ProcessExitStatus.SUCCESS
        assert ok.returncode == 0

        fail_script = tmp_path / "fail.py"
        fail_script.write_text("import sys; sys.exit(7)", encoding="utf-8")
        fail = run_hardened_subprocess([sys.executable, "fail.py"], cwd=tmp_path)
        assert fail.status == ProcessExitStatus.NON_ZERO_EXIT
        assert fail.returncode == 7

        spawn_err = run_hardened_subprocess(["definitely_non_existent_binary_xyz_123"], cwd=tmp_path)
        assert spawn_err.status == ProcessExitStatus.EXECUTION_ERROR
        assert spawn_err.returncode == 1


class TestOrchestratorCallsiteIntegration:
    """Validate that orchestrator _run caller works with list, strings, and injection protection."""

    def test_orchestrator_run_executes_git_commands(self, tmp_path: Path):
        """_run safely executes git commands without shell."""
        res = _run(["git", "--version"], cwd=tmp_path)
        assert res.status == ProcessExitStatus.SUCCESS
        assert "git version" in res.stdout

    def test_orchestrator_run_normalizes_string_command_safely(self, tmp_path: Path):
        """_run safely tokenizes a flat string command using shlex and enforces allow_shell=False."""
        script = tmp_path / "tok.py"
        script.write_text("import sys; print('tokenized')", encoding="utf-8")
        res = _run(f'"{sys.executable}" tok.py', cwd=tmp_path)
        assert res.status == ProcessExitStatus.SUCCESS
        assert "tokenized" in res.stdout

    def test_orchestrator_run_protects_against_commit_msg_metacharacters(self, tmp_path: Path):
        """Model-derived text with cmd metacharacters does not trigger command execution in _run."""
        marker = tmp_path / "commit_marker.txt"
        assert not marker.exists()

        poisoned_commit_title = f"feat: add auth & echo INJECTED>{marker}"
        # Even if git commit fails (not a git repo or no changes), marker must NEVER be created
        _run(["git", "commit", "-m", poisoned_commit_title], cwd=tmp_path)
        assert not marker.exists(), "Command injection succeeded through commit message!"
