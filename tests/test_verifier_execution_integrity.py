"""Security and execution boundary invariant test suite for BrainFrog Phase 1B.

Enforces execution integrity invariants:
1. Untrusted spec cannot grant unrestricted execution (defaults to BLOCKED).
2. shell=False is not treated as a sandbox (untrusted code fails closed).
3. Unavailable isolation backend fails closed.
4. Explicit trusted-local authorization is never implicit (--trust-local-code required).
5. Unsafe command wrappers (bash, cmd, powershell, pwsh) are strictly rejected.
6. Indirect execution risk is handled according to declared trust policy.
7. Paths cannot escape through traversal (../ in arguments is rejected).
8. Untrusted PATH cannot shadow system Python (sys.executable is pinned).
9. Synthetic credentials in environment are stripped from child process.
10. Timeouts terminate subprocess trees and produce non-zero / BLOCKED outcome.
11. Subprocess cannot execute banned Python inline execution (-c).
12. Subprocess cannot execute unauthorized Python modules (e.g. -m pip).
13. Stale authorization decisions are invalidated when spec content changes.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core.verifier import (
    CriterionStatus,
    ExecutionTrustMode,
    OverallVerdict,
    StandaloneVerifier,
    parse_spec_markdown,
    validate_verification_command,
)


class TestVerifierExecutionIntegrity(unittest.TestCase):
    """Adversarial and boundary tests for standalone verification execution."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="bf_exec_test_")
        self.repo_dir = Path(self.temp_dir).resolve()

        # Initialize disposable git repository
        subprocess.run(["git", "init"], cwd=self.repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Security Tester"], cwd=self.repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "sec@test.local"], cwd=self.repo_dir, check=True)

        readme = self.repo_dir / "README.md"
        readme.write_text("# Test Repo\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo_dir, check=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=self.repo_dir, check=True, capture_output=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_spec(self, cmd_str: str) -> Path:
        spec_content = f"""# Spec
## Acceptance Criteria
### AC-01 - Check
**Verification:** Test command
**Command:** {cmd_str}
"""
        spec_file = self.repo_dir / f"spec_{time.time_ns()}.md"
        spec_file.write_text(spec_content, encoding="utf-8")
        return spec_file

    def test_01_untrusted_spec_fails_closed_by_default(self) -> None:
        """Invariant 1: Without explicit operator trust, executable criteria fail closed (BLOCKED)."""
        spec_content = """# Spec: Untrusted Agent Code
## Acceptance Criteria
### AC-01 — Run Tests
**Verification:** Test command
**Command:** python -m unittest
"""
        spec_file = self.repo_dir / "spec.md"
        spec_file.write_text(spec_content, encoding="utf-8")

        verifier = StandaloneVerifier(repo_dir=self.repo_dir, trust_local_code=False)
        report = verifier.verify_spec(spec_file)

        self.assertEqual(report.execution_trust_mode, "UNTRUSTED_FAIL_CLOSED")
        self.assertEqual(report.criteria[0].status, CriterionStatus.BLOCKED)
        self.assertIn("Secure execution backend unavailable", report.criteria[0].failure_reason or "")
        self.assertEqual(report.overall_verdict, OverallVerdict.BLOCKED)
        self.assertEqual(report.exit_code, 2)

    def test_02_shell_false_is_not_treated_as_sandbox(self) -> None:
        """Invariant 2: Valid syntax under shell=False is still BLOCKED in untrusted mode."""
        spec_content = """# Spec: Python Command Check
## Acceptance Criteria
### AC-01 — Valid Syntax Python Command
**Verification:** Build command
**Command:** python -m py_compile README.md
"""
        spec_file = self.repo_dir / "spec.md"
        spec_file.write_text(spec_content, encoding="utf-8")

        # In untrusted mode, even harmless-looking allowlisted commands must be BLOCKED
        verifier = StandaloneVerifier(repo_dir=self.repo_dir, trust_local_code=False)
        report = verifier.verify_spec(spec_file)

        self.assertEqual(report.criteria[0].status, CriterionStatus.BLOCKED)
        self.assertIn("--trust-local-code", report.criteria[0].failure_reason or "")

    def test_03_explicit_trusted_local_mode_executes_with_warning(self) -> None:
        """Invariant 3: Explicit operator authorization runs command in TRUSTED_OPERATOR mode."""
        spec_content = """# Spec: Compile Check
## Acceptance Criteria
### AC-01 — Python Compile
**Verification:** Build command
**Command:** python -m py_compile README.md
"""
        spec_file = self.repo_dir / "spec.md"
        spec_file.write_text(spec_content, encoding="utf-8")

        verifier = StandaloneVerifier(repo_dir=self.repo_dir, trust_local_code=True)
        report = verifier.verify_spec(spec_file)

        self.assertEqual(report.execution_trust_mode, "TRUSTED_OPERATOR")
        self.assertEqual(report.execution_backend, "local_hardened_subprocess")
        self.assertEqual(report.criteria[0].status, CriterionStatus.PASS)
        self.assertEqual(report.overall_verdict, OverallVerdict.PASS)

    def test_04_command_wrapper_injection_rejected(self) -> None:
        """Invariant 4: Shell wrappers (bash, cmd, powershell, pwsh) are strictly rejected."""
        wrappers = [
            'bash -c "echo hack"',
            'sh -c "echo hack"',
            'cmd.exe /c echo hack',
            'powershell.exe -Command Write-Host hack',
            'pwsh -Command Write-Host hack',
        ]
        for w in wrappers:
            tokens = w.split()
            allowed, reason = validate_verification_command(tokens, cwd=self.repo_dir)
            self.assertFalse(allowed, f"Wrapper '{w}' should be disallowed")
            self.assertTrue("disallowed token" in (reason or "").lower() or "unauthorized" in (reason or "").lower())

    def test_05_path_traversal_in_command_arguments_rejected(self) -> None:
        """Invariant 5: Argument path traversal (../) outside workspace is rejected."""
        traversal_cmds = [
            ["python", "-m", "unittest", "../../etc/passwd"],
            ["python", "-m", "unittest", "..\\..\\windows\\system32"],
        ]
        for cmd in traversal_cmds:
            allowed, reason = validate_verification_command(cmd, cwd=self.repo_dir)
            self.assertFalse(allowed, f"Traversal '{cmd}' should be rejected")
            self.assertIn("traversal", (reason or "").lower())

    def test_05b_path_arguments_are_judged_the_same_on_every_host(self) -> None:
        """Invariant 5b: '/' and '\\' both separate; '..', rooted, drive and UNC arguments are rejected on any OS."""
        rejected = {
            "posix traversal": ["python", "-m", "unittest", "tests/../../etc/passwd"],
            "windows traversal": ["python", "-m", "unittest", "..\\..\\windows\\system32"],
            "mixed separators": ["python", "-m", "pytest", "tests\\../..\\secret"],
            "bare parent": ["python", "-m", "compileall", ".."],
            "option value traversal": ["python", "-m", "pytest", "--rootdir=../.."],
            "posix absolute": ["python", "-m", "unittest", "/etc/passwd"],
            "windows rooted": ["python", "-m", "unittest", "\\windows\\system32"],
            "drive-qualified": ["python", "-m", "compileall", "C:\\Windows"],
            "drive-relative": ["python", "-m", "compileall", "C:Windows"],
            "drive forward slash": ["python", "-m", "pytest", "d:/data"],
            "unc": ["python", "-m", "unittest", "\\\\server\\share\\x"],
            "posix unc-style": ["python", "-m", "unittest", "//server/share"],
            "option value absolute": ["python", "-m", "pytest", "--basetemp=/tmp/x"],
        }
        for name, cmd in rejected.items():
            allowed, reason = validate_verification_command(cmd, cwd=self.repo_dir)
            self.assertFalse(allowed, f"{name}: {cmd} should be rejected")
            self.assertIn("traversal", (reason or "").lower(), name)
        accepted = [
            ["python", "-m", "unittest", "discover", "-s", "tests", "-t", "."],
            ["python", "-m", "compileall", "-q", "."],
            ["python", "-m", "unittest", "tests/test_login.py"],
            ["python", "-m", "pytest", "tests\\unit\\test_x.py", "-k", "login and not slow"],
            ["python", "-m", "pytest", "tests/test_x.py::TestA::test_b", "-v"],
            ["python", "-m", "unittest", "pkg.module..Weird"],
            ["python", "-m", "pytest", "--maxfail=1", "-x"],
            ["npm", "run", "build"],
        ]
        for cmd in accepted:
            allowed, reason = validate_verification_command(cmd, cwd=self.repo_dir)
            self.assertTrue(allowed, f"{cmd} should be accepted: {reason}")

    def test_06_python_inline_command_c_rejected(self) -> None:
        """Invariant 6: Python inline code execution (-c) is rejected."""
        cmd = ["python", "-c", "print(1)"]
        allowed, reason = validate_verification_command(cmd, cwd=self.repo_dir)
        self.assertFalse(allowed)
        self.assertIn("inline command execution (-c) is prohibited", reason or "")

    def test_07_unauthorized_python_module_rejected(self) -> None:
        """Invariant 7: Unauthorized Python modules (e.g. -m pip, -m http.server) are rejected."""
        bad_mods = ["pip", "http.server", "urllib.request", "ensurepip"]
        for mod in bad_mods:
            cmd = ["python", "-m", mod]
            allowed, reason = validate_verification_command(cmd, cwd=self.repo_dir)
            self.assertFalse(allowed, f"Module '{mod}' should be rejected")
            self.assertIn("unauthorized python module", (reason or "").lower())

    def test_08_environment_secrets_stripped_from_child_process(self) -> None:
        """Invariant 8: Host API keys and tokens are scrubbed from child process environment."""
        verifier = StandaloneVerifier(repo_dir=self.repo_dir, trust_local_code=True)

        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "synthetic_openai_test_key_placeholder",
            "TYPESAFE_API_KEY": "synthetic_typesafe_test_key_placeholder",
            "AWS_SECRET_ACCESS_KEY": "synthetic_aws_secret_key_placeholder",
            "GITHUB_TOKEN": "synthetic_github_pat_placeholder",
            "PATH": os.environ.get("PATH", ""),
        }):
            scrubbed = verifier._get_scrubbed_env()
            self.assertNotIn("OPENAI_API_KEY", scrubbed)
            self.assertNotIn("TYPESAFE_API_KEY", scrubbed)
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", scrubbed)
            self.assertNotIn("GITHUB_TOKEN", scrubbed)
            self.assertIn("PATH", scrubbed)
            self.assertEqual(scrubbed.get("PYTHONPATH"), str(self.repo_dir))

    def test_09_python_executable_pinned_to_sys_executable(self) -> None:
        """Invariant 9: Python binary is pinned to sys.executable preventing repo executable shadowing."""
        # Create a dummy fake python script in repo root attempting to shadow python
        fake_py = self.repo_dir / "python.bat" if sys.platform == "win32" else self.repo_dir / "python"
        fake_py.write_text("exit 99\n", encoding="utf-8")

        spec_content = """# Spec: Pinned Executable
## Acceptance Criteria
### AC-01 — Run Compile
**Verification:** Build command
**Command:** python -m py_compile README.md
"""
        spec_file = self.repo_dir / "spec.md"
        spec_file.write_text(spec_content, encoding="utf-8")

        verifier = StandaloneVerifier(repo_dir=self.repo_dir, trust_local_code=True)
        report = verifier.verify_spec(spec_file)

        # The actual executed argv must use sys.executable, NOT the repo-local fake_py
        self.assertEqual(report.criteria[0].actual_executed_argv[0], sys.executable)
        self.assertEqual(report.criteria[0].status, CriterionStatus.PASS)

    def test_10_subprocess_timeout_terminates_cleanly(self) -> None:
        """Invariant 10: A genuinely stalled check is terminated on timeout and never PASSes.

        Phase 1C semantics: a timeout means the check could not complete, so it is an
        infrastructure outcome (BLOCKED, category 'timeout'), not an application FAIL.
        """
        (self.repo_dir / "test_fake.py").write_text(
            "import time, unittest\nclass T(unittest.TestCase):\n    def test_sleep(self):\n        time.sleep(60)\n",
            encoding="utf-8",
        )
        spec_content = """# Spec: Timeout Test
## Acceptance Criteria
### AC-01 — Long Sleep
**Verification:** Test command
**Command:** python -m unittest test_fake
"""
        spec_file = self.repo_dir / "spec.md"
        spec_file.write_text(spec_content, encoding="utf-8")

        started = time.monotonic()
        verifier = StandaloneVerifier(repo_dir=self.repo_dir, timeout=4.0, trust_local_code=True)
        report = verifier.verify_spec(spec_file)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 45.0, "Timeout must terminate the stalled process tree promptly")
        self.assertEqual(report.criteria[0].status, CriterionStatus.BLOCKED)
        self.assertEqual(report.criteria[0].infrastructure_error_category, "timeout")
        self.assertEqual(report.overall_verdict, OverallVerdict.BLOCKED)

    def test_11_shell_metacharacters_rejected(self) -> None:
        """Invariant 11: Shell metacharacters (;, &&, ||, |, >, <) are rejected."""
        malicious_commands = [
            "python -m unittest ; rm -rf /",
            "python -m unittest && calc.exe",
            "python -m unittest || calc.exe",
            "python -m unittest | grep foo",
            "python -m unittest > out.txt",
            "python -m unittest < in.txt",
        ]
        for cmd in malicious_commands:
            tokens = cmd.split()
            allowed, reason = validate_verification_command(tokens, cwd=self.repo_dir)
            self.assertFalse(allowed, f"Metacharacter command '{cmd}' should be rejected")

    def test_12_npm_subcommands_restricted(self) -> None:
        """Invariant 12: NPM commands only permit test, run, and build."""
        allowed_npm = ["npm", "test"]
        is_ok, _ = validate_verification_command(allowed_npm, cwd=self.repo_dir)
        self.assertTrue(is_ok)

        disallowed_npm = ["npm", "publish"]
        is_bad, reason = validate_verification_command(disallowed_npm, cwd=self.repo_dir)
        self.assertFalse(is_bad)
        self.assertIn("unauthorized npm subcommand", (reason or "").lower())

    def test_13_absolute_non_system_executable_rejected(self) -> None:
        """Invariant 13: Direct invocation of arbitrary absolute path binary is rejected."""
        bad_abs = "C:\\Windows\\System32\\cmd.exe" if sys.platform == "win32" else "/bin/bash"
        cmd = [bad_abs, "/c", "echo hello"]
        allowed, reason = validate_verification_command(cmd, cwd=self.repo_dir)
        self.assertFalse(allowed)


    def test_14_environment_variable_cannot_override_trust(self) -> None:
        """Invariant 14: Environment variable BRAINFROG_TRUST_LOCAL_CODE cannot override trust mode."""
        spec_path = self._create_spec("python -m unittest")
        old_val = os.environ.get("BRAINFROG_TRUST_LOCAL_CODE")
        try:
            os.environ["BRAINFROG_TRUST_LOCAL_CODE"] = "1"
            verifier = StandaloneVerifier(repo_dir=self.repo_dir)
            self.assertEqual(verifier.trust_mode, ExecutionTrustMode.UNTRUSTED_FAIL_CLOSED)
            report = verifier.verify_spec(spec_path)
            self.assertEqual(report.criteria[0].status, CriterionStatus.BLOCKED)
            self.assertEqual(report.overall_verdict, OverallVerdict.BLOCKED)
        finally:
            if old_val is not None:
                os.environ["BRAINFROG_TRUST_LOCAL_CODE"] = old_val
            else:
                os.environ.pop("BRAINFROG_TRUST_LOCAL_CODE", None)

    def test_15_isolated_sandbox_mode_fails_closed_when_backend_unavailable(self) -> None:
        """Invariant 15: Unimplemented container sandbox backend fails closed as BLOCKED."""
        from core.runtime.sandbox import DockerSandboxBackend
        spec_path = self._create_spec("python -m unittest")
        # Deterministically unavailable engine (independent of the host's Docker state).
        verifier = StandaloneVerifier(
            repo_dir=self.repo_dir,
            execution_trust_mode=ExecutionTrustMode.ISOLATED_SANDBOX,
            sandbox_backend=DockerSandboxBackend(docker_executable="bf-nonexistent-docker-cli"),
        )
        report = verifier.verify_spec(spec_path)
        self.assertEqual(report.criteria[0].status, CriterionStatus.BLOCKED)
        self.assertEqual(report.overall_verdict, OverallVerdict.BLOCKED)
        self.assertEqual(report.execution_backend, "unavailable_sandbox")
        self.assertIn("unavailable", report.criteria[0].failure_reason.lower())
        self.assertEqual(report.criteria[0].infrastructure_error_category, "docker_unavailable")

    def test_16_trust_reference_alone_does_not_authorize_execution(self) -> None:
        """Invariant 16: An audit reference without --trust-local-code fails closed."""
        spec_path = self._create_spec("python -m unittest")
        verifier = StandaloneVerifier(
            repo_dir=self.repo_dir,
            authorization_reference="AUDIT-TICKET-9999",
        )
        report = verifier.verify_spec(spec_path)
        self.assertEqual(report.criteria[0].status, CriterionStatus.BLOCKED)
        self.assertEqual(report.overall_verdict, OverallVerdict.BLOCKED)
        self.assertEqual(verifier.trust_mode, ExecutionTrustMode.UNTRUSTED_FAIL_CLOSED)

    def test_17_trust_reference_recorded_in_report_when_authorized(self) -> None:
        """Invariant 17: Audit reference is accurately recorded when operator authorizes execution."""
        spec_path = self._create_spec("python -m unittest")
        verifier = StandaloneVerifier(
            repo_dir=self.repo_dir,
            trust_local_code=True,
            authorization_reference="AUDIT-TICKET-9999",
        )
        report = verifier.verify_spec(spec_path)
        self.assertEqual(report.authorization_reference, "AUDIT-TICKET-9999")
        self.assertEqual(report.execution_trust_mode, "TRUSTED_OPERATOR")


if __name__ == "__main__":
    unittest.main()
