# BrainFrog Verification-First Phase 1C: Sandbox Execution & Isolation Tests
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from core.runtime.sandbox import (
    DockerSandboxBackend,
    LocalTrustedExecutionBackend,
    SandboxPolicy,
    SandboxResourceLimits,
    materialize_source_snapshot,
)
from core.verifier import StandaloneVerifier, ExecutionTrustMode, CriterionStatus, OverallVerdict


class TestVerifierSandboxExecution(unittest.TestCase):
    """Tests verifying sandbox container argument construction, security policies, and source snapshot immutability."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="bf_sandbox_test_")
        self.repo_dir = Path(self.tmp_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_01_docker_container_create_args_enforce_hardening_flags(self) -> None:
        """DockerSandboxBackend builds a fixed, fully hardened 'docker create' vector."""
        backend = DockerSandboxBackend(image_name="python:3.12-slim")
        limits = SandboxResourceLimits(cpus=1.5, memory_mb=256, pids_limit=32, timeout_seconds=60.0)

        snapshot_dir = self.repo_dir / "snapshot"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        harness_dir = self.repo_dir / "harness"
        harness_dir.mkdir(parents=True, exist_ok=True)
        image_id = "sha256:" + "0" * 64

        cmd_args = backend.build_container_create_args(
            container_name="bf_verify_test_cnt",
            run_label="run123",
            source_snapshot_dir=snapshot_dir,
            harness_dir=harness_dir,
            image_id=image_id,
            harness_argv=["python", "-I", "/bf-harness/verification_harness.py", "unittest"],
            limits=limits,
        )

        for required in (
            "--network=none",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--read-only",
            "--user=65532:65532",
            "--pull=never",
            "--cpus=1.5",
            "--memory=256m",
            "--memory-swap=256m",
            "--pids-limit=32",
            "--label=io.brainfrog.owner=brainfrog-verifier",
            "--label=io.brainfrog.run=run123",
        ):
            self.assertIn(required, cmd_args)
        self.assertNotIn("--rm", cmd_args)  # removal is explicit and ownership-checked
        self.assertFalse(any(a.startswith("--privileged") for a in cmd_args))
        self.assertFalse(any("docker.sock" in a for a in cmd_args))
        self.assertFalse(any(a in ("-v", "--volume") or a.startswith("--volume") for a in cmd_args))

        src_mount = f"--mount=type=bind,source={snapshot_dir.resolve()},target=/source,readonly"
        self.assertIn(src_mount, cmd_args)
        self.assertIn(f"--mount=type=bind,source={harness_dir.resolve()},target=/bf-harness,readonly", cmd_args)
        # The image is referenced by immutable ID, immediately followed by the harness argv.
        idx = cmd_args.index(image_id)
        self.assertEqual(cmd_args[idx + 1:idx + 3], ["python", "-I"])
        # Only BrainFrog-chosen environment variables are set in the container.
        env_args = [a for a in cmd_args if a.startswith("--env=")]
        self.assertEqual(sorted(env_args), ["--env=HOME=/tmp", "--env=PYTHONDONTWRITEBYTECODE=1"])

    def test_02_docker_is_available_returns_safe_tuple_when_daemon_offline(self) -> None:
        """is_available() fails closed with a reason, without raising, when the CLI/engine is unusable."""
        backend = DockerSandboxBackend(docker_executable="bf-nonexistent-docker-cli")
        is_avail, reason = backend.is_available()
        self.assertFalse(is_avail)
        self.assertIsInstance(reason, str)
        self.assertTrue(len(reason) > 0)

    def test_03_materialize_source_snapshot_excludes_sensitive_files(self) -> None:
        """Verify materialize_source_snapshot excludes .git, .env, secrets, and reports."""
        # Create sample repo tree
        (self.repo_dir / "src").mkdir(parents=True, exist_ok=True)
        (self.repo_dir / "src" / "app.py").write_text("print('hello')", encoding="utf-8")
        (self.repo_dir / ".git").mkdir(parents=True, exist_ok=True)
        (self.repo_dir / ".git" / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")
        (self.repo_dir / ".env").write_text("SECRET_KEY=leak", encoding="utf-8")
        (self.repo_dir / "secret_key.pem").write_text("PRIVATE_KEY", encoding="utf-8")
        (self.repo_dir / ".brainfrog").mkdir(parents=True, exist_ok=True)
        (self.repo_dir / ".brainfrog" / "report.json").write_text("{}", encoding="utf-8")

        target_snap = self.repo_dir / "materialized_snapshot"
        target_dir, digest = materialize_source_snapshot(
            repo_dir=self.repo_dir,
            target_snapshot_dir=target_snap,
            exclude_dirs=[".brainfrog"],
        )

        self.assertIsInstance(digest, str)
        self.assertEqual(len(digest), 64)

        # Valid source must exist
        self.assertTrue((target_snap / "src" / "app.py").exists())

        # Excluded sensitive paths must NOT exist
        self.assertFalse((target_snap / ".git").exists(), ".git must be excluded from snapshot")
        self.assertFalse((target_snap / ".env").exists(), ".env must be excluded from snapshot")
        self.assertFalse((target_snap / "secret_key.pem").exists(), "pem private key must be excluded")
        self.assertFalse((target_snap / ".brainfrog").exists(), "historical reports directory must be excluded")

    def test_04_source_snapshot_immutability(self) -> None:
        """Verify that mutations to materialized snapshot do not mutate the host repository."""
        app_file = self.repo_dir / "app.py"
        app_file.write_text("ORIGINAL_CONTENT", encoding="utf-8")

        target_snap = self.repo_dir / "materialized_snapshot"
        materialize_source_snapshot(repo_dir=self.repo_dir, target_snapshot_dir=target_snap)

        snap_app = target_snap / "app.py"
        self.assertEqual(snap_app.read_text(encoding="utf-8"), "ORIGINAL_CONTENT")

        # Mutate snapshot
        snap_app.write_text("MUTATED_IN_SANDBOX", encoding="utf-8")

        # Verify host remains untouched
        self.assertEqual(app_file.read_text(encoding="utf-8"), "ORIGINAL_CONTENT")

    def test_05_verifier_fails_closed_when_docker_offline(self) -> None:
        """When Docker daemon is offline, ISOLATED_SANDBOX returns BLOCKED with clear reason and does not run on host."""
        spec_text = """# Spec: Sandboxed Check
## Acceptance Criteria
### AC-01 - Run Unit Tests
**Verification:** Test command
**Command:** python -m unittest
"""
        spec_file = self.repo_dir / "spec.md"
        spec_file.write_text(spec_text, encoding="utf-8")

        verifier = StandaloneVerifier(
            repo_dir=self.repo_dir,
            execution_trust_mode=ExecutionTrustMode.ISOLATED_SANDBOX,
            sandbox_backend=DockerSandboxBackend(docker_executable="bf-nonexistent-docker-cli"),
        )
        report = verifier.verify_spec(spec_file)

        # Engine deterministically unavailable:
        self.assertEqual(report.criteria[0].status, CriterionStatus.BLOCKED)
        self.assertEqual(report.overall_verdict, OverallVerdict.BLOCKED)
        self.assertIn("unavailable", report.criteria[0].failure_reason.lower())
        self.assertEqual(report.execution_backend, "unavailable_sandbox")


if __name__ == "__main__":
    unittest.main()
