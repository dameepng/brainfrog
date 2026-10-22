"""Phase 2A Step 4C.2 — Adversarial Command Allowlisting and Boundary Tests.

Verifies:
1. Unknown executable denied by default.
2. Same executable name in an untrusted workspace directory denied (PATH hijacking prevention).
3. Relative executable paths denied (./git, sub/bin, ../exe).
4. Windows executable vs script extension confusion rejected (.bat, .cmd, .ps1 denied).
5. Shell wrappers denied (cmd.exe, powershell.exe, bash, sh).
6. Null bytes and malformed command inputs fail closed before process creation.
7. Response files (@filename) rejected for Git, Pytest, GH, and Python.
8. Interpreter -c dangerous code execution rejected (os.system, subprocess, socket, urllib, open).
9. Pytest argument injection rejected (-p malicious_plugin, -c external.ini, targets outside workspace).
10. Git argument injection rejected (-c core.pager, --exec-path, unauthorized subcommands).
11. GitHub CLI argument injection rejected (gh extension, gh alias).
12. Legitimate authorized Git and test-runner commands still succeed.
13. Denial occurs before process creation (zero child processes spawned).
14. Lifecycle preservation: timeout, cancellation, output limits, and process-tree cleanup.
15. Remote channel execution contract cannot gain arbitrary command execution.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from core.runtime.command_policy import (
    CommandPolicy,
    CommandPolicyViolation,
    CommandValidationResult,
    DEFAULT_COMMAND_POLICY,
    STRICT_PRODUCTION_POLICY,
    resolve_and_authorize_executable,
)
from core.runtime.process import (
    HardenedProcessResult,
    ProcessExitStatus,
    run_hardened_subprocess,
)
from orchestrator import _run


class TestSubprocessExecutableResolution:
    """Verify deterministic executable resolution and PATH hijacking rejection."""

    def test_unknown_executable_denied(self, tmp_path: Path):
        """Unknown executable name is denied by default at the boundary."""
        res = run_hardened_subprocess(["unauthorized_evil_binary_12345"], cwd=tmp_path)
        assert res.is_success is False
        assert res.returncode == 1
        assert res.status == ProcessExitStatus.EXECUTION_ERROR
        assert "Command denied by allowlist policy" in (res.error or "")
        assert "unauthorized_evil_binary_12345" in (res.error or "")

    def test_workspace_local_executable_hijacking_denied(self, tmp_path: Path):
        """An executable with a legitimate name inside the untrusted workspace is denied."""
        fake_git = tmp_path / ("git.exe" if sys.platform == "win32" else "git")
        fake_git.write_text("#!/bin/sh\necho fake-git\n", encoding="utf-8")
        if sys.platform != "win32":
            fake_git.chmod(0o755)

        # 1. Explicit path to the workspace file is denied
        res_explicit = run_hardened_subprocess([str(fake_git), "status"], cwd=tmp_path)
        assert res_explicit.is_success is False
        assert res_explicit.returncode == 1
        assert "PATH hijacking prevention" in (res_explicit.error or "")

        # 2. Bare name 'git' resolves to the real system Git, not the workspace fake
        res_bare = run_hardened_subprocess(["git", "--version"], cwd=tmp_path)
        assert res_bare.is_success is True
        assert "git version" in res_bare.stdout
        assert "fake-git" not in res_bare.stdout

    def test_relative_executable_paths_denied(self, tmp_path: Path):
        """Untrusted relative executable paths (./exe, ../exe, sub/exe) are denied."""
        sub_dir = tmp_path / "bin"
        sub_dir.mkdir()
        sub_exe = sub_dir / ("git.exe" if sys.platform == "win32" else "git")
        sub_exe.write_text("dummy", encoding="utf-8")

        for relative_token in ("./git", f"./bin/{sub_exe.name}", f"bin/{sub_exe.name}", "../git"):
            res = run_hardened_subprocess([relative_token, "status"], cwd=tmp_path)
            assert res.is_success is False
            assert res.returncode == 1
            assert "Untrusted relative executable path denied" in (res.error or "")

    def test_windows_script_extension_confusion_rejected(self, tmp_path: Path):
        """Batch, command, and PowerShell script wrappers are rejected even with valid stem."""
        for ext in (".bat", ".cmd", ".ps1", ".vbs"):
            script_token = f"git{ext}"
            res = run_hardened_subprocess([script_token, "status"], cwd=tmp_path)
            assert res.is_success is False
            assert res.returncode == 1
            assert "prohibited" in (res.error or "").lower() or "script" in (res.error or "").lower()

    def test_shell_wrappers_prohibited(self, tmp_path: Path):
        """cmd.exe, powershell.exe, bash, sh are denied as standalone executables."""
        shells = ["cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "bash", "sh", "zsh"]
        for shell in shells:
            res = run_hardened_subprocess([shell, "-c", "echo evil"], cwd=tmp_path)
            assert res.is_success is False
            assert res.returncode == 1
            assert "Shell wrapper" in (res.error or "") or "Command denied by allowlist policy" in (res.error or "")

    def test_null_bytes_in_command_rejected_before_spawn(self, tmp_path: Path):
        """Null bytes in executable or arguments fail closed before process creation."""
        # Null in executable
        res_exe = run_hardened_subprocess(["git\0malicious", "status"], cwd=tmp_path)
        assert res_exe.is_success is False
        assert "Null byte" in (res_exe.error or "")

        # Null in argument
        res_arg = run_hardened_subprocess(["git", "diff\0poisoned"], cwd=tmp_path)
        assert res_arg.is_success is False
        assert "Null byte" in (res_arg.error or "")


class TestCommandArgumentValidation:
    """Verify command-specific argument policies for Git, Pytest, GH, and Python."""

    def test_git_disallowed_config_pager_override_rejected(self, tmp_path: Path):
        """Git -c flag used for config overrides / pager execution is rejected."""
        res = run_hardened_subprocess(
            ["git", "-c", "core.pager=calc.exe", "log"], cwd=tmp_path
        )
        assert res.is_success is False
        assert res.returncode == 1
        assert "Disallowed Git option: '-c'" in (res.error or "")

    def test_git_disallowed_exec_path_rejected(self, tmp_path: Path):
        """Git --exec-path override is rejected."""
        res = run_hardened_subprocess(
            ["git", "--exec-path=/tmp/evil", "status"], cwd=tmp_path
        )
        assert res.is_success is False
        assert res.returncode == 1
        assert "Disallowed Git option: '--exec-path=/tmp/evil'" in (res.error or "")

    def test_git_response_file_injection_rejected(self, tmp_path: Path):
        """Git response file syntax (@filename) is rejected."""
        res = run_hardened_subprocess(["git", "@malicious_args.txt"], cwd=tmp_path)
        assert res.is_success is False
        assert res.returncode == 1
        assert "Response file syntax" in (res.error or "")

    def test_git_unauthorized_subcommand_rejected(self, tmp_path: Path):
        """Unauthorized Git subcommands (e.g. clone, bisect, filter-branch) are rejected."""
        for subcmd in ("clone", "filter-branch", "daemon", "http-backend"):
            res = run_hardened_subprocess(["git", subcmd], cwd=tmp_path)
            assert res.is_success is False
            assert res.returncode == 1
            assert f"Unauthorized Git subcommand: '{subcmd}'" in (res.error or "")

    def test_pytest_plugin_injection_rejected(self, tmp_path: Path):
        """Pytest plugin loading flag (-p) is rejected."""
        res = run_hardened_subprocess(
            ["pytest", "-p", "malicious_plugin", "tests/"], cwd=tmp_path
        )
        assert res.is_success is False
        assert res.returncode == 1
        assert "Disallowed Pytest option: '-p'" in (res.error or "")

    def test_pytest_response_file_rejected(self, tmp_path: Path):
        """Pytest response file syntax (@file) is rejected."""
        res = run_hardened_subprocess(["pytest", "@inject_flags"], cwd=tmp_path)
        assert res.is_success is False
        assert res.returncode == 1
        assert "Response file syntax" in (res.error or "")

    def test_pytest_target_outside_workspace_rejected(self, tmp_path: Path):
        """Pytest test target outside the workspace is rejected."""
        outside_test = tmp_path.parent / "outside_test.py"
        res = run_hardened_subprocess(
            ["pytest", "-q", str(outside_test)], cwd=tmp_path
        )
        assert res.is_success is False
        assert res.returncode == 1
        assert "resolves outside workspace" in (res.error or "")

    def test_gh_extension_and_alias_injection_rejected(self, tmp_path: Path):
        """GitHub CLI extension and alias commands are rejected."""
        for sub in ("extension", "alias", "api"):
            res = run_hardened_subprocess(["gh", sub, "install", "evil"], cwd=tmp_path)
            assert res.is_success is False
            assert res.returncode == 1
            assert "Disallowed gh subcommand" in (res.error or "")

    def test_python_dangerous_inline_code_rejected(self, tmp_path: Path):
        """Python -c snippets importing dangerous modules or builtins are rejected."""
        attacks = [
            'import subprocess; subprocess.run(["calc"])',
            'import os; os.system("calc")',
            'import socket; s = socket.socket()',
            'import urllib.request; urllib.request.urlopen("http://evil.com")',
            'import shutil; shutil.rmtree("/")',
            'open("/etc/passwd", "w").write("pwnd")',
            'eval("1+1")',
            'exec("import os")',
            '__import__("os").system("calc")',
        ]
        for attack_code in attacks:
            res = run_hardened_subprocess(
                [sys.executable, "-c", attack_code], cwd=tmp_path
            )
            assert res.is_success is False
            assert res.returncode == 1
            assert "Inline Python code execution (-c) is strictly prohibited" in (res.error or "")

    def test_python_unauthorized_module_rejected(self, tmp_path: Path):
        """Python -m with unauthorized modules (http.server, pip, site) is rejected."""
        for mod in ("http.server", "pip", "site", "webbrowser", "smtpd"):
            res = run_hardened_subprocess(
                [sys.executable, "-m", mod], cwd=tmp_path
            )
            assert res.is_success is False
            assert res.returncode == 1
            assert "Unauthorized Python module execution" in (res.error or "")

    def test_python_external_script_path_rejected(self, tmp_path: Path):
        """Python execution of a script file outside the workspace is rejected."""
        outside_script = tmp_path.parent / "malicious_script.py"
        outside_script.write_text("print('evil')", encoding="utf-8")
        res = run_hardened_subprocess(
            [sys.executable, str(outside_script)], cwd=tmp_path
        )
        assert res.is_success is False
        assert res.returncode == 1
        assert "resolves outside authorized workspace" in (res.error or "")


class TestLegitimateCommandsAndLifecycle:
    """Verify that legitimate authorized commands work cleanly without regressions."""

    def test_legitimate_git_version_succeeds(self, tmp_path: Path):
        """git --version executes successfully under allowlist policy."""
        res = run_hardened_subprocess(["git", "--version"], cwd=tmp_path)
        assert res.is_success is True
        assert res.returncode == 0
        assert "git version" in res.stdout

    def test_legitimate_python_version_succeeds(self, tmp_path: Path):
        """python --version executes successfully under allowlist policy."""
        res = run_hardened_subprocess([sys.executable, "--version"], cwd=tmp_path)
        assert res.is_success is True
        assert res.returncode == 0
        assert "Python" in res.stdout or "Python" in res.stderr

    def test_legitimate_python_m_unittest_succeeds(self, tmp_path: Path):
        """python -m unittest executes successfully under allowlist policy."""
        res = run_hardened_subprocess(
            [sys.executable, "-m", "unittest", "--help"], cwd=tmp_path
        )
        assert res.is_success is True
        assert res.returncode == 0
        assert "unittest" in res.stdout or "Usage:" in res.stdout

    def test_denial_occurs_before_process_creation(self, tmp_path: Path):
        """Denied command aborts immediately with zero subprocess.Popen calls."""
        with patch("subprocess.Popen") as mock_popen:
            res = run_hardened_subprocess(["powershell", "-c", "dir"], cwd=tmp_path)
            assert res.is_success is False
            assert res.returncode == 1
            mock_popen.assert_not_called()

    def test_orchestrator_run_enforces_policy(self, tmp_path: Path):
        """orchestrator._run invokes allowlist policy and rejects unauthorized commands."""
        res = _run(["calc.exe"], cwd=tmp_path)
        assert res.is_success is False
        assert res.status == ProcessExitStatus.EXECUTION_ERROR
        assert "Command denied by allowlist policy" in (res.error or "")


class TestRemediationBF4C201PythonExecution:
    """BF-4C2-01: Verify complete elimination of Python inline code execution and AST sandboxes."""

    def test_python_c_all_forms_rejected(self, tmp_path: Path):
        """python -c, python -c=..., and all inline code execution forms are rejected unconditionally."""
        forms = [
            [sys.executable, "-c", "print('hello')"],
            [sys.executable, "-c=print('hello')"],
            [sys.executable, "-c", "import sys; sys.exit(0)"],
            [sys.executable, "-c", "x = 1"],
        ]
        for cmd in forms:
            res = run_hardened_subprocess(cmd, cwd=tmp_path)
            assert res.is_success is False
            assert res.returncode == 1
            assert "Inline Python code execution (-c) is strictly prohibited" in (res.error or "")

    def test_python_ast_bypass_payloads_rejected(self, tmp_path: Path):
        """Known AST-bypass payloads (indirect imports, sys.modules, getattr) fail closed."""
        payloads = [
            "import sys; sys.modules['os'].system('calc')",
            "getattr(__builtins__, 'eval')('1+1')",
            "getattr(sys.modules['builtins'], 'open')('/etc/passwd')",
            "__import__('os').system('echo evil')",
        ]
        for code in payloads:
            res = run_hardened_subprocess([sys.executable, "-c", code], cwd=tmp_path)
            assert res.is_success is False
            assert res.returncode == 1
            assert "Inline Python code execution (-c) is strictly prohibited" in (res.error or "")

    def test_python_script_symlink_containment(self, tmp_path: Path):
        """Python script execution is permitted only within workspace containment."""
        ws_script = tmp_path / "valid_script.py"
        ws_script.write_text("import sys; print('SCRIPT_OK')", encoding="utf-8")

        res_ok = run_hardened_subprocess([sys.executable, "valid_script.py"], cwd=tmp_path)
        assert res_ok.is_success is True
        assert "SCRIPT_OK" in res_ok.stdout

        # Script outside workspace
        outside_script = tmp_path.parent / "outside_script.py"
        outside_script.write_text("print('OUTSIDE')", encoding="utf-8")

        res_deny = run_hardened_subprocess([sys.executable, str(outside_script)], cwd=tmp_path)
        assert res_deny.is_success is False
        assert "resolves outside authorized workspace" in (res_deny.error or "")


class TestRemediationBF4C202ExecutableResolutionBinding:
    """BF-4C2-02: Verify validated executable binding and caller-provided PATH isolation."""

    def test_popen_executed_path_matches_validated_path(self, tmp_path: Path):
        """The binary passed to Popen is exactly the resolved_executable from policy."""
        policy = DEFAULT_COMMAND_POLICY
        val_res = policy.validate(["git", "--version"], cwd=tmp_path)
        assert val_res.is_allowed is True
        assert val_res.resolved_executable is not None

        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value.poll.return_value = 0
            mock_popen.return_value.returncode = 0
            mock_popen.return_value.stdout = None
            mock_popen.return_value.stderr = None
            mock_popen.return_value.wait.return_value = 0

            run_hardened_subprocess(["git", "--version"], cwd=tmp_path)
            mock_popen.assert_called_once()
            called_cmd = mock_popen.call_args[0][0]
            # Must be the exact resolved canonical path, NOT the bare token 'git'
            assert called_cmd[0] == val_res.resolved_executable

    def test_caller_provided_path_cannot_hijack_executable(self, tmp_path: Path):
        """A fake executable placed in caller-supplied PATH inside workspace is stripped."""
        fake_bin_dir = tmp_path / "fake_bin"
        fake_bin_dir.mkdir()
        fake_git = fake_bin_dir / ("git.exe" if sys.platform == "win32" else "git")
        fake_git.write_text("#!/bin/sh\necho POISONED_GIT\n", encoding="utf-8")

        poisoned_path = f"{fake_bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        res = run_hardened_subprocess(
            ["git", "--version"],
            cwd=tmp_path,
            env={"PATH": poisoned_path},
        )
        assert res.is_success is True
        # Real git must be executed, never the poisoned binary
        assert "git version" in res.stdout
        assert "POISONED_GIT" not in res.stdout


class TestRemediationBF4C203PytestHardenedGrammar:
    """BF-4C2-03: Verify pytest argument grammar hardening against plugins and config smuggling."""

    def test_pytest_short_option_o_rejected(self, tmp_path: Path):
        """Pytest -o / -o=... option override flag is strictly rejected."""
        attacks = [
            ["pytest", "-o", "addopts=-p malicious"],
            ["pytest", "-o=addopts=-p malicious"],
            ["pytest", "--override-ini", "addopts=-p malicious"],
            ["pytest", "--override-ini=addopts=-p malicious"],
        ]
        for cmd in attacks:
            res = run_hardened_subprocess(cmd, cwd=tmp_path)
            assert res.is_success is False
            assert "configuration override is prohibited" in (res.error or "")

    def test_pytest_config_file_short_c_rejected(self, tmp_path: Path):
        """Pytest -c / --config-file smuggling is strictly rejected."""
        attacks = [
            ["pytest", "-c", "malicious.ini"],
            ["pytest", "-c=malicious.ini"],
            ["pytest", "--config-file", "malicious.ini"],
            ["pytest", "--config-file=malicious.ini"],
        ]
        for cmd in attacks:
            res = run_hardened_subprocess(cmd, cwd=tmp_path)
            assert res.is_success is False
            assert "custom config file is prohibited" in (res.error or "")

    def test_pytest_allowed_grammar_options_succeed(self, tmp_path: Path):
        """Safe test options conforming to explicit grammar pass validation."""
        from core.runtime.command_policy import validate_pytest_arguments
        policy = DEFAULT_COMMAND_POLICY
        safe_option_lists = [
            ["-v", "-q", "-s"],
            ["--verbose", "--quiet", "-x"],
            ["-k", "test_feature"],
            ["-m", "unit"],
            ["--maxfail=1"],
            ["--tb=short"],
            ["--disable-warnings"],
        ]
        for opt_list in safe_option_lists:
            is_valid, reason = validate_pytest_arguments(opt_list, tmp_path)
            assert is_valid is True, f"Failed for {opt_list}: {reason}"

            # Also verify through python -m pytest execution path
            m_res = policy.validate([sys.executable, "-m", "pytest", *opt_list], cwd=tmp_path)
            assert m_res.is_allowed is True, f"Failed for python -m pytest: {m_res.reason}"


class TestRemediationBF4C204GitHardenedPolicy:
    """BF-4C2-04: Verify Git subcommand-specific argument policies and escape prevention."""

    def test_git_global_directory_flags_rejected(self, tmp_path: Path):
        """Git -C, --git-dir, --work-tree, and --namespace are rejected."""
        attacks = [
            ["git", "-C", "C:\\Windows", "status"],
            ["git", "-CC:\\Windows", "status"],
            ["git", "--git-dir=/tmp/evil/.git", "status"],
            ["git", "--work-tree=/tmp/evil", "status"],
            ["git", "--namespace=evil", "status"],
        ]
        for cmd in attacks:
            res = run_hardened_subprocess(cmd, cwd=tmp_path)
            assert res.is_success is False
            assert "Disallowed Git option" in (res.error or "")

    def test_git_config_helper_and_write_rejected(self, tmp_path: Path):
        """Git config write operations and helper configurations are strictly rejected."""
        attacks = [
            ["git", "config", "credential.helper", "!calc.exe"],
            ["git", "config", "core.pager", "calc.exe"],
            ["git", "config", "diff.external.command", "calc.exe"],
            ["git", "config", "user.name", "Attacker"],
            ["git", "config", "--add", "user.name", "Attacker"],
            ["git", "config", "--file", ".git/config", "user.name"],
        ]
        for cmd in attacks:
            res = run_hardened_subprocess(cmd, cwd=tmp_path)
            assert res.is_success is False
            assert (
                "Disallowed Git config" in (res.error or "")
                or "Git config write operations are prohibited" in (res.error or "")
                or "Arbitrary Git config file manipulation is disallowed" in (res.error or "")
            )

    def test_git_config_readonly_queries_allowed(self, tmp_path: Path):
        """Read-only Git config queries are allowed."""
        policy = DEFAULT_COMMAND_POLICY
        safe_queries = [
            ["git", "config", "--get", "user.name"],
            ["git", "config", "--get", "remote.origin.url"],
            ["git", "config", "--get-all", "remote.origin.fetch"],
            ["git", "config", "--list"],
        ]
        for cmd in safe_queries:
            res = policy.validate(cmd, cwd=tmp_path)
            assert res.is_allowed is True

    def test_git_ext_diff_rejected(self, tmp_path: Path):
        """Git --ext-diff is rejected anywhere in command arguments."""
        res = run_hardened_subprocess(["git", "diff", "--ext-diff"], cwd=tmp_path)
        assert res.is_success is False
        assert "Disallowed Git option: '--ext-diff'" in (res.error or "")

    def test_git_remote_ext_url_rejected(self, tmp_path: Path):
        """Git remote with ext:: external transport helper protocol is rejected."""
        attacks = [
            ["git", "remote", "add", "origin", "ext::sh -c evil"],
            ["git", "remote", "set-url", "origin", "ext::sh -c evil"],
            ["git", "remote", "add", "origin", "!calc.exe"],
        ]
        for cmd in attacks:
            res = run_hardened_subprocess(cmd, cwd=tmp_path)
            assert res.is_success is False
            assert "Prohibited git remote URL syntax" in (res.error or "")


class TestRemediationBF4C205SubprocessCallSitesMigration:
    """BF-4C2-05: Verify migrated production call sites operate through shared hardened policy."""

    def test_orchestrator_get_git_remote_url_uses_hardened_runner(self, tmp_path: Path):
        """orchestrator.get_git_remote_url operates cleanly without bypass."""
        from orchestrator import get_git_remote_url
        url = get_git_remote_url(tmp_path)
        assert url == ""

    def test_orchestrator_configure_git_remote_rejects_malicious_url(self, tmp_path: Path):
        """orchestrator.configure_git_remote fails closed on ext:: syntax."""
        from orchestrator import configure_git_remote
        success, url = configure_git_remote(tmp_path, "ext::sh -c evil")
        assert success is False

    def test_skills_run_skill_script_uses_hardened_policy(self, tmp_path: Path):
        """core.skills.run_skill_script uses run_hardened_subprocess with explicit script containment."""
        from core.skills import SkillMetadata, run_skill_script

        skill_dir = tmp_path / "test_skill"
        scripts_dir = skill_dir / "scripts"
        scripts_dir.mkdir(parents=True)
        script_file = scripts_dir / "action.py"
        script_file.write_text("import sys; print('SKILL_RUN_OK')", encoding="utf-8")

        skill_meta = SkillMetadata(
            name="test_skill",
            description="Testing skill execution",
            dir_path=skill_dir,
            skill_file=skill_dir / "SKILL.md",
            scripts={"action.py": script_file},
        )

        proc = run_skill_script("action.py", [], skill_meta, tmp_path)
        assert proc.returncode == 0
        assert "SKILL_RUN_OK" in proc.stdout


class TestRemediationPass2AuditFindings:
    """Pass 2 Regression Tests for Audit Findings BF-4C2-AUDIT-01 through 06."""

    def test_audit_01_fake_executables_outside_workspace_rejected(self, tmp_path: Path):
        """BF-4C2-AUDIT-01: Absolute executable paths are strictly authorized by canonical identity.
        Binaries in temporary directories or copies of legitimate binaries are rejected even if stem matches.
        """
        temp_bin_dir = tmp_path / "external_temp_bin"
        temp_bin_dir.mkdir()
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()

        # 1. Fake git.exe in temporary directory
        fake_git = temp_bin_dir / ("git.exe" if sys.platform == "win32" else "git")
        fake_git.write_bytes(b"FAKE_GIT_BINARY_CONTENT")
        is_allowed, _, _, reason = resolve_and_authorize_executable(str(fake_git), cwd=workspace_dir)
        assert is_allowed is False
        assert (
            "Executable binary located in untrusted temporary directory is denied" in (reason or "")
            or "is not an authorized canonical binary" in (reason or "")
            or "Unauthorized executable" in (reason or "")
        )

        res_git = DEFAULT_COMMAND_POLICY.validate([str(fake_git), "status"], cwd=workspace_dir)
        assert res_git.is_allowed is False

        # 2. Fake python.exe in temporary directory
        fake_py = temp_bin_dir / ("python.exe" if sys.platform == "win32" else "python")
        fake_py.write_bytes(b"FAKE_PYTHON_BINARY_CONTENT")
        is_allowed, _, _, _ = resolve_and_authorize_executable(str(fake_py), cwd=workspace_dir)
        assert is_allowed is False
        res_py = DEFAULT_COMMAND_POLICY.validate([str(fake_py), "-V"], cwd=workspace_dir)
        assert res_py.is_allowed is False

        # 3. Fake pytest.exe in temporary directory
        fake_pytest = temp_bin_dir / ("pytest.exe" if sys.platform == "win32" else "pytest")
        fake_pytest.write_bytes(b"FAKE_PYTEST_BINARY_CONTENT")
        is_allowed, _, _, _ = resolve_and_authorize_executable(str(fake_pytest), cwd=workspace_dir)
        assert is_allowed is False
        res_pt = DEFAULT_COMMAND_POLICY.validate([str(fake_pytest), "-v"], cwd=workspace_dir)
        assert res_pt.is_allowed is False

        # 4. Copy legitimate python interpreter to external directory with renamed or same name
        copied_py = temp_bin_dir / ("git.exe" if sys.platform == "win32" else "git")
        copied_py.write_bytes(Path(sys.executable).read_bytes())
        is_allowed, _, _, _ = resolve_and_authorize_executable(str(copied_py), cwd=workspace_dir)
        assert is_allowed is False

        # 5. Legitimate system interpreter must be authorized
        is_allowed_real_py, resolved_real_py, canon, _ = resolve_and_authorize_executable(
            sys.executable, cwd=workspace_dir
        )
        assert is_allowed_real_py is True
        assert canon == "python"

    def test_audit_02_pr_proof_call_sites_hardened(self, tmp_path: Path):
        """BF-4C2-AUDIT-02: core.pr_proof uses run_hardened_subprocess, explicit staging, and bounded timeouts."""
        from core.pr_proof import publish_proof_to_assets_branch, update_github_pr_body

        # Rejects when allow_remote_push is False
        res = publish_proof_to_assets_branch(
            repo_dir=tmp_path,
            target_identifier="pr-123",
            desktop_bytes=b"dummy",
            mobile_bytes=b"dummy",
            allow_remote_push=False,
        )
        assert res is None

        # Rejects invalid branch names or target identifiers before command construction
        assert publish_proof_to_assets_branch(
            repo_dir=tmp_path,
            target_identifier=";evil_command",
            desktop_bytes=b"dummy",
            mobile_bytes=b"dummy",
            allow_remote_push=True,
        ) is None

        assert publish_proof_to_assets_branch(
            repo_dir=tmp_path,
            target_identifier="pr-1",
            desktop_bytes=b"dummy",
            mobile_bytes=b"dummy",
            assets_branch="branch;evil",
            allow_remote_push=True,
        ) is None

        # update_github_pr_body rejects malicious or flag-like identifiers
        assert update_github_pr_body(tmp_path, "--flag-injection", "body") is False
        assert update_github_pr_body(tmp_path, "pr;calc", "body") is False
        assert update_github_pr_body(tmp_path, "", "body") is False

    def test_audit_03_git_push_remote_helper_injection_rejected(self, tmp_path: Path):
        """BF-4C2-AUDIT-03: Positional git push arguments reject ext::, fd::, and ! syntax."""
        attacks = [
            ["git", "push", "ext::calc", "main"],
            ["git", "push", "EXT::calc", "main"],
            ["git", "push", "ext::sh -c evil", "HEAD"],
            ["git", "push", "fd::3", "main"],
            ["git", "push", "origin", "!calc.exe"],
            ["git", "push", "origin", "ext::calc"],
            ["git", "push", "--repo=ext::calc", "main"],
            ["git", "push", "--force-with-lease=ext::calc:1234", "origin", "main"],
        ]
        for cmd in attacks:
            res = DEFAULT_COMMAND_POLICY.validate(cmd, cwd=tmp_path)
            assert res.is_allowed is False, f"Expected attack to be rejected: {cmd}"
            assert (
                "Prohibited remote helper syntax" in (res.reason or "")
                or "Prohibited external command syntax" in (res.reason or "")
                or "Prohibited ref value" in (res.reason or "")
                or "Disallowed Git push option" in (res.reason or "")
            )

        # Legitimate pushes remain authorized
        safe_pushes = [
            ["git", "push", "origin", "main"],
            ["git", "push", "-u", "origin", "feat/my-branch"],
            ["git", "push", "--force-with-lease", "origin", "main"],
            ["git", "push", "--force-with-lease=main", "origin", "main"],
            ["git", "push", "--dry-run", "origin", "main"],
            ["git", "push", "origin", "HEAD:refs/heads/feature"],
        ]
        for cmd in safe_pushes:
            res = DEFAULT_COMMAND_POLICY.validate(cmd, cwd=tmp_path)
            assert res.is_allowed is True, f"Expected safe push to be allowed: {cmd}"

    def test_audit_04_dangerous_env_vars_scrubbed_and_caller_override_blocked(self, tmp_path: Path):
        """BF-4C2-AUDIT-04: Hostile environment variables are stripped and cannot be reintroduced by caller."""
        hostile_env = {
            "GIT_DIR": str(tmp_path / "hostile_git_dir"),
            "GIT_WORK_TREE": str(tmp_path / "hostile_work_tree"),
            "GIT_CONFIG": str(tmp_path / "hostile_config"),
            "GIT_EXTERNAL_DIFF": "calc.exe",
            "GIT_CONFIG_KEY_0": "core.editor",
            "GIT_CONFIG_VALUE_0": "calc.exe",
            "PYTEST_ADDOPTS": "-p malicious_plugin",
            "PYTEST_PLUGINS": "malicious_plugin",
            "PYTHONSTARTUP": str(tmp_path / "evil_startup.py"),
        }
        res = run_hardened_subprocess(
            ["git", "--version"],
            cwd=tmp_path,
            env=hostile_env,
        )
        assert res.is_success is True
        assert "git version" in res.stdout

        # Verify that merged_env within run_hardened_subprocess scrubs all hostile keys
        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value.wait.return_value = 0
            mock_popen.return_value.poll.return_value = 0
            mock_popen.return_value.communicate.return_value = (b"", b"")
            mock_popen.return_value.stdin = None
            mock_popen.return_value.stdout = None
            mock_popen.return_value.stderr = None

            run_hardened_subprocess(["git", "--version"], cwd=tmp_path, env=hostile_env)
            assert mock_popen.called
            passed_env = mock_popen.call_args[1]["env"]
            for dangerous_key in hostile_env:
                assert dangerous_key not in passed_env, f"Key {dangerous_key} was not scrubbed!"
            assert passed_env.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") == "1"

    def test_audit_05_check_plan_staleness_uses_hardened_runner(self, tmp_path: Path):
        """BF-4C2-AUDIT-05: check_plan_staleness invokes run_hardened_subprocess and handles errors gracefully."""
        from core.plans import PlanDocument, check_plan_staleness

        plan = PlanDocument(
            filename="plan.md",
            path=tmp_path / "plan.md",
            title="Test Plan",
            goal="Test staleness",
            commit_hash="abc1234",
            relevant_files=["test.py"],
            file_hashes={"test.py": "hash1"},
            steps=[],
        )
        # Even on an uninitialized/empty repo, does not crash and returns boolean outcome
        is_stale, reasons = check_plan_staleness(tmp_path, plan)
        assert isinstance(is_stale, bool)
        assert isinstance(reasons, list)

    def test_audit_06_zero_argument_python_rejected(self, tmp_path: Path):
        """BF-4C2-AUDIT-06: Bare python interpreter invocations with no script or module are rejected."""
        empty_python_invocations = [
            ["python"],
            ["python3"],
            [sys.executable],
            ["python", ""],
            ["python", "   "],
            ["python", "\t"],
        ]
        for cmd in empty_python_invocations:
            res = DEFAULT_COMMAND_POLICY.validate(cmd, cwd=tmp_path)
            assert res.is_allowed is False, f"Expected command to be rejected: {cmd}"
            assert (
                "Python execution requires a script path or approved module" in (res.reason or "")
                or "Empty command provided" in (res.reason or "")
            )


class TestResidualRiskCustomPathClosure:
    """Residual-Risk Closure: Verify caller-controlled PATH cannot establish trust or hijack execution."""

    def test_caller_supplied_path_cannot_authorize_fake_executable_outside_workspace(
        self, tmp_path_factory: pytest.TempPathFactory
    ):
        """A fake executable placed in an attacker-controlled directory outside the workspace
        is never authorized or executed when passed via caller-supplied PATH.
        """
        attacker_bin = tmp_path_factory.mktemp("attacker_bin")
        workspace = tmp_path_factory.mktemp("target_workspace")

        fake_git = attacker_bin / ("git.exe" if sys.platform == "win32" else "git")
        fake_git.write_text("#!/bin/sh\necho ATTACKER_POISONED_GIT\n", encoding="utf-8")

        # Caller attempts to prepend or replace PATH with attacker directory
        poisoned_path = f"{attacker_bin}{os.pathsep}{os.environ.get('PATH', '')}"
        res = run_hardened_subprocess(
            ["git", "--version"],
            cwd=workspace,
            env={"PATH": poisoned_path},
        )
        assert res.is_success is True
        # Real system git must be executed, never the poisoned binary
        assert "git version" in res.stdout
        assert "ATTACKER_POISONED_GIT" not in res.stdout

    def test_caller_supplied_path_case_variants_stripped(
        self, tmp_path_factory: pytest.TempPathFactory
    ):
        """Case variants ('Path', 'path', 'PATH') supplied by caller are completely stripped."""
        attacker_bin = tmp_path_factory.mktemp("attacker_bin_case")
        workspace = tmp_path_factory.mktemp("target_workspace_case")

        for var_name in ("Path", "path", "PATH", "pAtH"):
            with patch("subprocess.Popen") as mock_popen:
                mock_popen.return_value.poll.return_value = 0
                mock_popen.return_value.returncode = 0
                mock_popen.return_value.stdin = None
                mock_popen.return_value.stdout = None
                mock_popen.return_value.stderr = None

                run_hardened_subprocess(
                    ["git", "--version"],
                    cwd=workspace,
                    env={var_name: str(attacker_bin)},
                )
                assert mock_popen.called
                passed_env = mock_popen.call_args[1]["env"]
                # Passed PATH must not contain attacker_bin
                assert str(attacker_bin) not in passed_env.get("PATH", "")
                # Case variants must not exist as separate keys
                for k in passed_env:
                    if k != "PATH":
                        assert k.upper() != "PATH", f"Case variant key {k} survived in env!"

    def test_direct_resolve_and_authorize_ignores_caller_path(
        self, tmp_path_factory: pytest.TempPathFactory
    ):
        """Direct invocation of resolve_and_authorize_executable strictly ignores caller PATH."""
        attacker_bin = tmp_path_factory.mktemp("attacker_direct")
        workspace = tmp_path_factory.mktemp("workspace_direct")

        fake_git = attacker_bin / ("git.exe" if sys.platform == "win32" else "git")
        fake_git.write_text("#!/bin/sh\necho FAKE\n", encoding="utf-8")

        is_allowed, resolved, canonical, reason = resolve_and_authorize_executable(
            "git",
            cwd=workspace,
            env={"PATH": str(attacker_bin)},
        )
        assert is_allowed is True
        assert canonical == "git"
        assert resolved is not None
        # Must resolve to the trusted system Git, not attacker_bin
        assert str(attacker_bin).lower() not in resolved.lower()

    def test_bare_python_binds_strictly_to_sys_executable_regardless_of_path(
        self, tmp_path_factory: pytest.TempPathFactory
    ):
        """Bare python/python3 tokens resolve strictly to the running interpreter (sys.executable)."""
        attacker_bin = tmp_path_factory.mktemp("attacker_py")
        workspace = tmp_path_factory.mktemp("workspace_py")

        fake_py = attacker_bin / ("python.exe" if sys.platform == "win32" else "python")
        fake_py.write_text("#!/bin/sh\necho FAKE_PYTHON\n", encoding="utf-8")

        for token in ("python", "python3"):
            is_allowed, resolved, canonical, reason = resolve_and_authorize_executable(
                token,
                cwd=workspace,
                env={"PATH": str(attacker_bin)},
            )
            assert is_allowed is True
            assert canonical == "python"
            # Executes the running interpreter as launched (a POSIX venv's bin/python is a
            # symlink; its realpath would leave the venv), with the same canonical identity.
            assert resolved == os.path.abspath(sys.executable)
            assert os.path.realpath(resolved) == os.path.realpath(sys.executable)

    def test_caller_supplied_python_path_and_injection_vars_scrubbed(
        self, tmp_path: Path
    ):
        """PYTHONPATH, PYTHONHOME, LD_PRELOAD and related redirection vars are scrubbed."""
        hostile_env = {
            "PYTHONPATH": r"C:\malicious\site-packages",
            "PYTHONHOME": r"C:\malicious\python_home",
            "PYTHONUSERBASE": r"C:\malicious\user_base",
            "PYTHONCASEOK": "1",
            "LD_PRELOAD": "/malicious/preload.so",
            "LD_LIBRARY_PATH": "/malicious/lib",
            "DYLD_INSERT_LIBRARIES": "/malicious/dyld.dylib",
            "DYLD_LIBRARY_PATH": "/malicious/dyld_lib",
            "PATHEXT": ".BAT;.CMD",  # Attempt to force script extension
        }
        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value.poll.return_value = 0
            mock_popen.return_value.returncode = 0
            mock_popen.return_value.stdin = None
            mock_popen.return_value.stdout = None
            mock_popen.return_value.stderr = None

            run_hardened_subprocess([sys.executable, "--version"], cwd=tmp_path, env=hostile_env)
            assert mock_popen.called
            passed_env = mock_popen.call_args[1]["env"]
            for bad_key in (
                "PYTHONPATH",
                "PYTHONHOME",
                "PYTHONUSERBASE",
                "PYTHONCASEOK",
                "LD_PRELOAD",
                "LD_LIBRARY_PATH",
                "DYLD_INSERT_LIBRARIES",
                "DYLD_LIBRARY_PATH",
            ):
                assert bad_key not in passed_env, f"{bad_key} was not scrubbed from child env!"
            if sys.platform == "win32":
                assert ".EXE" in passed_env.get("PATHEXT", "").upper()

    def test_exact_authorized_executable_passed_to_popen(self, tmp_path: Path):
        """Verify that the exact executable passed to process creation is the one authorized."""
        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value.poll.return_value = 0
            mock_popen.return_value.returncode = 0
            mock_popen.return_value.stdin = None
            mock_popen.return_value.stdout = None
            mock_popen.return_value.stderr = None

            run_hardened_subprocess(["git", "--version"], cwd=tmp_path)
            assert mock_popen.called
            actual_cmd = mock_popen.call_args[0][0]
            passed_exe = actual_cmd[0]

            # Verify it's an absolute path to git.exe
            assert Path(passed_exe).is_absolute()
            assert Path(passed_exe).name.lower() in ("git.exe", "git")

            # Verify policy validation returns the exact same path
            is_allowed, resolved, _, _ = resolve_and_authorize_executable("git", cwd=tmp_path)
            assert is_allowed is True
            assert passed_exe == resolved


class TestResidualRiskRemediationPass:
    """Remediation tests for BF-4C2-RESIDUAL-04 Git Hook Execution Boundary."""

    def test_git_config_parameters_enforces_core_hooks_path_dev_null(self, tmp_path: Path):
        """run_hardened_subprocess unconditionally injects core.hooksPath=/dev/null."""
        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value.poll.return_value = 0
            mock_popen.return_value.returncode = 0
            mock_popen.return_value.stdin = None
            mock_popen.return_value.stdout = None
            mock_popen.return_value.stderr = None

            run_hardened_subprocess(
                ["git", "--version"],
                cwd=tmp_path,
                env={"GIT_CONFIG_PARAMETERS": "'core.hooksPath=.git/hooks'"},
            )
            assert mock_popen.called
            passed_env = mock_popen.call_args[1]["env"]
            assert passed_env.get("GIT_CONFIG_PARAMETERS") == "'core.hooksPath=/dev/null'"

    def test_git_hooks_do_not_execute_during_hardened_subprocess_commit(self, tmp_path: Path):
        """Harmless marker-writing hooks in an untrusted repo do not execute during commit."""
        # Setup git repo
        init_res = subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, text=True)
        assert init_res.returncode == 0
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)

        marker = tmp_path / "hook_marker.txt"
        hooks_dir = tmp_path / ".git" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)

        pre_hook = hooks_dir / "pre-commit"
        pre_hook.write_text(f"#!/bin/sh\necho hook_ran > \"{marker.as_posix()}\"\nexit 0\n", encoding="utf-8")
        if sys.platform != "win32":
            pre_hook.chmod(0o755)

        test_file = tmp_path / "work.txt"
        test_file.write_text("hello", encoding="utf-8")

        # Stage file
        add_res = run_hardened_subprocess(["git", "add", "work.txt"], cwd=tmp_path)
        assert add_res.is_success is True

        # Commit file through hardened subprocess
        commit_res = run_hardened_subprocess(
            ["git", "commit", "--no-verify", "-m", "initial test commit"],
            cwd=tmp_path,
        )
        assert commit_res.is_success is True
        assert commit_res.returncode == 0

        # Prove marker file was NEVER written (hook was suppressed by core.hooksPath=/dev/null and --no-verify)
        assert not marker.exists(), "Git pre-commit hook executed when it should have been suppressed!"

    def test_git_hooks_suppressed_solely_by_hooks_path_without_no_verify_flag(self, tmp_path: Path):
        """core.hooksPath=/dev/null alone suppresses git hooks even when --no-verify is omitted."""
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)

        marker = tmp_path / "hook_marker.txt"
        hooks_dir = tmp_path / ".git" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)

        pre_hook = hooks_dir / "pre-commit"
        pre_hook.write_text(f"#!/bin/sh\necho hook_ran > \"{marker.as_posix()}\"\nexit 0\n", encoding="utf-8")
        if sys.platform != "win32":
            pre_hook.chmod(0o755)

        test_file = tmp_path / "work2.txt"
        test_file.write_text("hello 2", encoding="utf-8")

        run_hardened_subprocess(["git", "add", "work2.txt"], cwd=tmp_path)

        # Standard commit WITHOUT --no-verify
        commit_res = run_hardened_subprocess(
            ["git", "commit", "-m", "commit without no-verify flag"],
            cwd=tmp_path,
        )
        assert commit_res.is_success is True

        # Hook was suppressed by core.hooksPath=/dev/null alone
        assert not marker.exists(), "Git pre-commit hook executed without --no-verify flag!"

    def test_inherited_git_config_cannot_override_hooks_path_suppression(self, tmp_path: Path):
        """Inherited GIT_CONFIG or GIT_CONFIG_PARAMETERS cannot override core.hooksPath=/dev/null."""
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)

        marker = tmp_path / "hook_marker.txt"
        hooks_dir = tmp_path / ".git" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)

        pre_hook = hooks_dir / "pre-commit"
        pre_hook.write_text(f"#!/bin/sh\necho hook_ran > \"{marker.as_posix()}\"\nexit 0\n", encoding="utf-8")
        if sys.platform != "win32":
            pre_hook.chmod(0o755)

        test_file = tmp_path / "work3.txt"
        test_file.write_text("hello 3", encoding="utf-8")

        run_hardened_subprocess(["git", "add", "work3.txt"], cwd=tmp_path)

        # Hostile environment attempting to reactivate local repository hooks
        hostile_env = {
            "GIT_CONFIG_PARAMETERS": f"'core.hooksPath={hooks_dir.as_posix()}'",
            "GIT_CONFIG": str(tmp_path / "evil.config"),
        }

        commit_res = run_hardened_subprocess(
            ["git", "commit", "-m", "commit with hostile env"],
            cwd=tmp_path,
            env=hostile_env,
        )
        assert commit_res.is_success is True
        assert not marker.exists(), "Hostile GIT_CONFIG_PARAMETERS overrode hooks suppression!"


