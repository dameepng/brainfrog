"""Phase 1C finalization: targeted tests for the four identified blockers.

P0-1 Zero-test / forged-output false PASS      -> structured, nonce-bound harness records
P0-2 Container security controls               -> fixed hardened argv + effective-config verification
P0-3 Executed snapshot identity                 -> digest of the exact materialized bytes, observed in-runner
P1   Infrastructure failure misclassified        -> BLOCKED with machine-readable category

These tests use the real harness through LocalTrustedExecutionBackend (host) so they run
without Docker. Real-container coverage lives in test_verifier_docker_integration.py.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.runtime import sandbox as sandbox_mod
from core.runtime.sandbox import (
    DockerSandboxBackend,
    InfraErrorCategory,
    LocalTrustedExecutionBackend,
    SandboxPolicy,
    SandboxResourceLimits,
    SnapshotError,
    TestResultAdapter,
    compute_directory_digest,
    materialize_source_snapshot_with_manifest,
)
from core.verifier import (
    CriterionStatus,
    ExecutionTrustMode,
    OverallVerdict,
    StandaloneVerifier,
    verify_report_integrity,
)

UNITTEST = ["python", "-m", "unittest"]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


class _RepoCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bf_p1c_")).resolve()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        _git(self.repo, "init")
        _git(self.repo, "config", "user.email", "p1c@test.local")
        _git(self.repo, "config", "user.name", "P1C Tester")
        (self.repo / "README.md").write_text("# fixture\n", encoding="utf-8")
        _git(self.repo, "add", "README.md")
        _git(self.repo, "commit", "-m", "init")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel: str, text: str) -> None:
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def run_local(self, argv, timeout: float = 60.0):
        snap = materialize_source_snapshot_with_manifest(self.repo, self.tmp / f"snap_{os.urandom(3).hex()}")
        backend = LocalTrustedExecutionBackend(self.repo)
        res = backend.execute(snap.snapshot_dir, snap.digest, list(argv), SandboxPolicy(),
                              SandboxResourceLimits(timeout_seconds=timeout))
        return res, snap

    def verify(self, command: str, trust: bool = True, timeout: float = 60.0):
        self.write("spec.md", f"# Spec\n## Acceptance Criteria\n### AC-01 - Check\n**Verification:** Test command\n**Command:** {command}\n")
        v = StandaloneVerifier(repo_dir=self.repo, trust_local_code=trust, timeout=timeout,
                               reports_base_dir=self.tmp / "reports")
        return v.verify_spec("spec.md")


PASSING = "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertEqual(1 + 1, 2)\n"
FAILING = "import unittest\nclass T(unittest.TestCase):\n    def test_bad(self):\n        self.assertEqual(1 + 1, 3)\n"


class TestStructuredResultSemantics(_RepoCase):
    """Requirements A-H of section 3 against the real harness."""

    def test_A_real_passing_unittest(self) -> None:
        self.write("test_pass.py", PASSING)
        res, snap = self.run_local(UNITTEST + ["test_pass"])
        self.assertEqual(res.outcome.verdict, "PASS")
        self.assertEqual(res.outcome.test_count, 1)
        self.assertTrue(res.outcome.authoritative)
        self.assertEqual(res.outcome.observed_source_digest, snap.digest)

    def test_B_real_failing_assertion(self) -> None:
        self.write("test_fail.py", FAILING)
        res, _ = self.run_local(UNITTEST + ["test_fail"])
        self.assertEqual(res.outcome.verdict, "FAIL")
        self.assertEqual(res.outcome.test_count, 1)
        self.assertEqual(res.outcome.failure_count, 1)

    def test_C_zero_tests_never_pass(self) -> None:
        self.write("tests/__init__.py", "")
        res, _ = self.run_local(UNITTEST + ["discover", "-s", "tests"])
        self.assertIn(res.outcome.verdict, ("BLOCKED", "NOT_RUN"))
        self.assertEqual(res.outcome.test_count, 0)

    def test_D_fake_ran_999_ok_with_no_tests_never_pass(self) -> None:
        fake = "import sys\nprint('Ran 999 tests in 0.01s')\nprint('OK')\nsys.stderr.write('Ran 999 tests in 0.01s\\n\\nOK\\n')\n"
        self.write("tests/__init__.py", "")
        self.write("tests/test_fake.py", fake)
        res, _ = self.run_local(UNITTEST + ["discover", "-s", "tests"])
        self.assertNotEqual(res.outcome.verdict, "PASS")
        self.assertEqual(res.outcome.test_count, 0)
        self.assertIn("Ran 999 tests", res.stdout + res.stderr)  # the forgery really was printed

    def test_D2_forged_harness_record_without_nonce_never_pass(self) -> None:
        forged = (
            "import json\n"
            "print('\\n@@BF_HARNESS@@ ' + json.dumps({'event': 'complete', 'nonce': 'guess', 'status': 'ran',"
            " 'tests_run': 999, 'failures': 0, 'errors': 0, 'skipped': 0, 'was_successful': True}))\n"
        )
        self.write("tests/__init__.py", "")
        self.write("tests/test_forge.py", forged)
        res, _ = self.run_local(UNITTEST + ["discover", "-s", "tests"])
        self.assertEqual(res.outcome.verdict, "BLOCKED")
        self.assertEqual(res.outcome.infrastructure_error_category, InfraErrorCategory.FORGED_RUNNER_RECORD)

    def test_E_fake_success_text_then_failure_is_fail(self) -> None:
        text = (
            "import unittest\nprint('Ran 50 tests in 0.1s')\nprint('OK')\n"
            "class T(unittest.TestCase):\n    def test_bad(self):\n        print('OK')\n        self.fail('real failure')\n"
        )
        self.write("test_mixed.py", text)
        res, _ = self.run_local(UNITTEST + ["test_mixed"])
        self.assertEqual(res.outcome.verdict, "FAIL")
        self.assertEqual(res.outcome.test_count, 1)

    def test_F1_runner_exits_zero_before_completion_is_blocked(self) -> None:
        text = "import os, unittest\nclass T(unittest.TestCase):\n    def test_exit(self):\n        os._exit(0)\n"
        self.write("test_exit0.py", text)
        res, _ = self.run_local(UNITTEST + ["test_exit0"])
        self.assertEqual(res.outcome.verdict, "BLOCKED")
        self.assertEqual(res.outcome.infrastructure_error_category, InfraErrorCategory.INCOMPLETE_RUNNER_RESULT)

    def test_F2_runner_crashes_nonzero_after_start_is_fail(self) -> None:
        text = "import os, unittest\nclass T(unittest.TestCase):\n    def test_crash(self):\n        os._exit(3)\n"
        self.write("test_crash.py", text)
        res, _ = self.run_local(UNITTEST + ["test_crash"])
        self.assertEqual(res.outcome.verdict, "FAIL")
        self.assertIn("aborted", res.outcome.reason)

    def test_G_truncated_output_never_pass(self) -> None:
        text = (
            "import sys, unittest\nclass T(unittest.TestCase):\n    def test_noisy(self):\n"
            "        sys.stdout.write('x' * (3 * 1024 * 1024))\n        self.assertTrue(True)\n"
        )
        self.write("test_noisy.py", text)
        res, _ = self.run_local(UNITTEST + ["test_noisy"])
        self.assertTrue(res.output_truncated)
        self.assertNotEqual(res.outcome.verdict, "PASS")

    def test_G2_adapter_rejects_pass_when_truncated(self) -> None:
        nonce = "n" * 64
        rec = {"event": "complete", "nonce": nonce, "status": "ran", "tests_run": 1, "failures": 0, "errors": 0,
               "skipped": 0, "was_successful": True, "interrupted": False}
        start = {"event": "started", "nonce": nonce, "source_digest": "d"}
        out = "\n".join("@@BF_HARNESS@@ " + json.dumps(r) for r in (start, rec))
        ok = TestResultAdapter.parse_outcome(UNITTEST, 0, out, "", nonce=nonce, expected_source_digest="d")
        self.assertEqual(ok.verdict, "PASS")
        trunc = TestResultAdapter.parse_outcome(UNITTEST, 0, out, "", nonce=nonce, expected_source_digest="d",
                                                output_truncated=True)
        self.assertEqual(trunc.verdict, "BLOCKED")

    def test_H_unsupported_runner_exit_zero_does_not_claim_coverage(self) -> None:
        report = self.verify("pytest")
        crit = report.criteria[0]
        self.assertEqual(crit.status, CriterionStatus.BLOCKED)
        self.assertEqual(crit.infrastructure_error_category, "unsupported_runner")
        self.assertIsNone(crit.exit_code)  # never executed

    def test_all_skipped_tests_never_pass(self) -> None:
        text = "import unittest\nclass T(unittest.TestCase):\n    @unittest.skip('x')\n    def test_s(self):\n        pass\n"
        self.write("test_skip.py", text)
        res, _ = self.run_local(UNITTEST + ["test_skip"])
        self.assertEqual(res.outcome.verdict, "BLOCKED")

    def test_compile_valid_invalid_and_empty(self) -> None:
        self.write("pkg/good.py", "x = 1\n")
        res, _ = self.run_local(["python", "-m", "compileall", "-q", "pkg"])
        self.assertEqual(res.outcome.verdict, "PASS")
        self.write("pkg/bad.py", "def broken(:\n")
        res, _ = self.run_local(["python", "-m", "compileall", "-q", "pkg"])
        self.assertEqual(res.outcome.verdict, "FAIL")
        self.write("empty/.keep", "")
        res, _ = self.run_local(["python", "-m", "compileall", "-q", "empty"])
        self.assertEqual(res.outcome.verdict, "BLOCKED")


class TestSnapshotBinding(_RepoCase):
    """P0-3: the recorded executed digest is the digest of the bytes actually supplied."""

    def test_report_binds_materialized_digest_to_observed_digest(self) -> None:
        self.write("test_pass.py", PASSING)  # untracked, must be represented
        self.write("README.md", "# fixture (unstaged edit)\n")
        report = self.verify("python -m unittest test_pass")
        self.assertEqual(report.overall_verdict, OverallVerdict.PASS)
        crit = report.criteria[0]
        self.assertEqual(crit.observed_source_snapshot_digest, report.materialized_source_snapshot_digest)
        self.assertEqual(crit.materialized_source_snapshot_digest, report.materialized_source_snapshot_digest)
        self.assertNotEqual(report.materialized_source_snapshot_digest, report.source_snapshot_digest)
        data = json.loads((report.report_dir / "report.json").read_text(encoding="utf-8"))
        ident = data["snapshot_identity"]
        for key in ("repository_head_sha", "live_workspace_pre_snapshot_digest",
                    "materialized_source_snapshot_digest", "live_workspace_post_snapshot_digest"):
            self.assertTrue(ident[key], key)
        manifest = json.loads((report.report_dir / "evidence" / "_run" / "snapshot_manifest.json").read_text(encoding="utf-8"))
        self.assertIn("test_pass.py", manifest["files"])
        self.assertEqual(manifest["files"]["README.md"],
                         hashlib.sha256((self.repo / "README.md").read_bytes()).hexdigest())
        self.assertIn(b"unstaged edit", (self.repo / "README.md").read_bytes())
        ok, findings = verify_report_integrity(report.report_dir)
        self.assertTrue(ok, findings)

    def test_digest_is_of_materialized_bytes(self) -> None:
        self.write("a.py", "A = 1\n")
        snap = materialize_source_snapshot_with_manifest(self.repo, self.tmp / "s1")
        self.assertEqual(compute_directory_digest(snap.snapshot_dir), snap.digest)
        again = materialize_source_snapshot_with_manifest(self.repo, self.tmp / "s2")
        self.assertEqual(again.digest, snap.digest)  # deterministic

    def test_ignored_and_sensitive_files_not_copied(self) -> None:
        self.write(".gitignore", "secrets.local\n")
        self.write("secrets.local", "synthetic-ignored-sentinel\n")
        self.write("deploy.pem", "synthetic\n")
        self.write("app.py", "x = 1\n")
        snap = materialize_source_snapshot_with_manifest(self.repo, self.tmp / "s")
        self.assertIn("app.py", snap.files)
        self.assertNotIn("secrets.local", snap.files)
        self.assertNotIn("deploy.pem", snap.files)
        self.assertFalse((snap.snapshot_dir / "secrets.local").exists())

    def test_staged_change_represented(self) -> None:
        self.write("README.md", "# staged\n")
        _git(self.repo, "add", "README.md")
        snap = materialize_source_snapshot_with_manifest(self.repo, self.tmp / "s")
        self.assertEqual((snap.snapshot_dir / "README.md").read_text(encoding="utf-8"), "# staged\n")

    def test_symlink_escape_not_followed(self) -> None:
        outside = self.tmp / "outside_secret.txt"
        outside.write_text("synthetic-outside-sentinel", encoding="utf-8")
        try:
            os.symlink(outside, self.repo / "link.txt")
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation not permitted on this host: {exc}")
        snap = materialize_source_snapshot_with_manifest(self.repo, self.tmp / "s")
        self.assertIn("link.txt", snap.skipped_symlinks)
        self.assertFalse((snap.snapshot_dir / "link.txt").exists())

    @unittest.skipUnless(os.name == "nt", "directory junctions are Windows-specific")
    def test_junction_escape_not_followed(self) -> None:
        outside = self.tmp / "outside_dir"
        outside.mkdir()
        (outside / "secret.txt").write_text("synthetic-junction-sentinel", encoding="utf-8")
        rc = subprocess.run(["cmd", "/c", "mklink", "/J", str(self.repo / "jlink"), str(outside)],
                            capture_output=True, text=True)
        if rc.returncode != 0:
            self.skipTest(f"junction creation failed: {rc.stderr}")
        self.write("app.py", "x = 1\n")
        snap = materialize_source_snapshot_with_manifest(self.repo, self.tmp / "s")
        self.assertIn("app.py", snap.files)
        self.assertFalse(any(k.startswith("jlink") for k in snap.files), snap.files)
        self.assertFalse((snap.snapshot_dir / "jlink").exists())
        for p in snap.snapshot_dir.rglob("*"):
            if p.is_file():
                self.assertNotIn(b"synthetic-junction-sentinel", p.read_bytes())

    def test_concurrent_modification_during_snapshot_raises(self) -> None:
        self.write("a.py", "A = 1\n")
        real_read = Path.read_bytes
        target = (self.repo / "a.py").resolve()
        state = {"done": False}

        def racing_read(self_path: Path) -> bytes:
            data = real_read(self_path)
            if not state["done"] and Path(self_path).resolve() == target:
                state["done"] = True
                target.write_text("A = 2  # concurrent edit\n", encoding="utf-8")
            return data

        with patch.object(Path, "read_bytes", racing_read):
            with self.assertRaises(SnapshotError):
                materialize_source_snapshot_with_manifest(self.repo, self.tmp / "s")

    def test_git_listing_failure_is_not_silent(self) -> None:
        real = sandbox_mod.run_hardened_subprocess

        def fake(cmd, *a, **kw):
            if "ls-files" in cmd:
                from core.runtime.process import HardenedProcessResult
                return HardenedProcessResult(args=cmd, returncode=128, stdout="", stderr="fatal: synthetic")
            return real(cmd, *a, **kw)

        with patch.object(sandbox_mod, "run_hardened_subprocess", side_effect=fake):
            with self.assertRaises(SnapshotError):
                materialize_source_snapshot_with_manifest(self.repo, self.tmp / "s")
            report = self.verify("python -m unittest")
        self.assertEqual(report.criteria[0].status, CriterionStatus.BLOCKED)
        self.assertEqual(report.criteria[0].infrastructure_error_category, "snapshot_error")

    def test_snapshot_mutation_during_execution_blocks(self) -> None:
        self.write("test_pass.py", PASSING)
        snap = materialize_source_snapshot_with_manifest(self.repo, self.tmp / "s")
        backend = LocalTrustedExecutionBackend(self.repo)
        real_runner = backend.runner

        def mutating_runner(*a, **kw):
            res = real_runner(*a, **kw)
            (snap.snapshot_dir / "test_pass.py").write_text("tampered\n", encoding="utf-8")
            return res

        backend.runner = mutating_runner
        res = backend.execute(snap.snapshot_dir, snap.digest, UNITTEST + ["test_pass"], SandboxPolicy(),
                              SandboxResourceLimits(timeout_seconds=60))
        self.assertEqual(res.outcome.verdict, "BLOCKED")
        self.assertEqual(res.infrastructure_error_category, InfraErrorCategory.SNAPSHOT_MUTATED)

    def test_wrong_expected_digest_blocks(self) -> None:
        self.write("test_pass.py", PASSING)
        snap = materialize_source_snapshot_with_manifest(self.repo, self.tmp / "s")
        backend = LocalTrustedExecutionBackend(self.repo)
        res = backend.execute(snap.snapshot_dir, "0" * 64, UNITTEST + ["test_pass"], SandboxPolicy(),
                              SandboxResourceLimits(timeout_seconds=60))
        self.assertNotEqual(res.outcome.verdict, "PASS")


class TestInfrastructureClassification(_RepoCase):
    """P1: infrastructure problems are BLOCKED with a category, never FAIL, never host fallback."""

    def test_docker_unavailable_blocks_without_host_fallback(self) -> None:
        marker = self.tmp / "host_marker.txt"
        self.write("test_marker.py",
                   "import unittest\nclass T(unittest.TestCase):\n    def test_m(self):\n"
                   f"        open({str(marker)!r}, 'w').write('ran on host')\n")
        self.write("spec.md", "# Spec\n## Acceptance Criteria\n### AC-01 - X\n**Verification:** Test command\n"
                              "**Command:** python -m unittest test_marker\n")
        v = StandaloneVerifier(repo_dir=self.repo, sandbox_mode="docker", reports_base_dir=self.tmp / "reports",
                               sandbox_backend=DockerSandboxBackend(docker_executable="bf-nonexistent-docker-cli"))
        report = v.verify_spec("spec.md")
        self.assertEqual(report.criteria[0].status, CriterionStatus.BLOCKED)
        self.assertEqual(report.criteria[0].infrastructure_error_category, "docker_unavailable")
        self.assertFalse(marker.exists(), "sandbox failure must never fall back to host execution")

    def test_weakened_policy_refused(self) -> None:
        backend = DockerSandboxBackend(docker_executable="bf-nonexistent-docker-cli")
        snap = materialize_source_snapshot_with_manifest(self.repo, self.tmp / "s")
        for weak in (SandboxPolicy(allow_network=True), SandboxPolicy(drop_capabilities=False),
                     SandboxPolicy(non_root_user="0:0"), SandboxPolicy(read_only_root_filesystem=False)):
            res = backend.execute(snap.snapshot_dir, snap.digest, UNITTEST, weak, SandboxResourceLimits())
            self.assertEqual(res.outcome.verdict, "BLOCKED")
            self.assertEqual(res.infrastructure_error_category, InfraErrorCategory.POLICY_WEAKENING_REFUSED)

    def test_unapproved_image_rejected(self) -> None:
        with self.assertRaises(ValueError):
            DockerSandboxBackend(image_name="alpine:latest")

    def test_effective_config_verifier_detects_weakening(self) -> None:
        limits = SandboxResourceLimits()
        good = {
            "Config": {"User": "65532:65532", "Env": ["HOME=/tmp", "PATH=/usr/bin"]},
            "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True, "Privileged": False,
                           "SecurityOpt": ["no-new-privileges"], "CapDrop": ["ALL"], "CapAdd": None,
                           "PidMode": "", "IpcMode": "private", "UTSMode": "",
                           "Memory": 512 * 1024 * 1024, "NanoCpus": 2_000_000_000, "PidsLimit": 64,
                           "Tmpfs": {"/workspace": "", "/tmp": ""}},
            "Mounts": [{"Type": "bind", "Source": "/s", "Destination": "/source", "RW": False},
                       {"Type": "bind", "Source": "/h", "Destination": "/bf-harness", "RW": False}],
        }
        self.assertEqual(DockerSandboxBackend.verify_effective_config(good, limits, "/s", "/h"), [])
        mutations = [
            ("Config", "User", "root"), ("HostConfig", "NetworkMode", "host"), ("HostConfig", "ReadonlyRootfs", False),
            ("HostConfig", "Privileged", True), ("HostConfig", "SecurityOpt", []), ("HostConfig", "CapAdd", ["SYS_ADMIN"]),
            ("HostConfig", "PidMode", "host"), ("HostConfig", "Memory", 0), ("HostConfig", "PidsLimit", -1),
            ("HostConfig", "NanoCpus", 0),
        ]
        for section, key, value in mutations:
            bad = json.loads(json.dumps(good))
            bad[section][key] = value
            self.assertTrue(DockerSandboxBackend.verify_effective_config(bad, limits, "/s", "/h"), (section, key))
        bad = json.loads(json.dumps(good))
        bad["Mounts"].append({"Type": "bind", "Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock", "RW": True})
        self.assertTrue(DockerSandboxBackend.verify_effective_config(bad, limits, "/s", "/h"))
        bad = json.loads(json.dumps(good))
        bad["Mounts"][0]["RW"] = True
        self.assertTrue(DockerSandboxBackend.verify_effective_config(bad, limits, "/s", "/h"))
        bad = json.loads(json.dumps(good))
        bad["Config"]["Env"].append("OPENAI_API_KEY=x")
        self.assertTrue(DockerSandboxBackend.verify_effective_config(bad, limits, "/s", "/h"))


class TestTrustResolutionAndReportIntegrity(_RepoCase):
    def test_cli_default_selects_sandbox_and_spec_cannot_enable_trust(self) -> None:
        self.assertEqual(StandaloneVerifier(self.repo, trust_local_code=None, sandbox_mode="auto").trust_mode,
                         ExecutionTrustMode.ISOLATED_SANDBOX)
        self.assertEqual(StandaloneVerifier(self.repo, trust_local_code=None, sandbox_mode="none").trust_mode,
                         ExecutionTrustMode.UNTRUSTED_FAIL_CLOSED)
        self.assertEqual(StandaloneVerifier(self.repo).trust_mode, ExecutionTrustMode.UNTRUSTED_FAIL_CLOSED)
        report = self.verify("python -m unittest --trust-local-code", trust=False)
        self.assertEqual(report.execution_trust_mode, "UNTRUSTED_FAIL_CLOSED")
        self.assertEqual(report.criteria[0].status, CriterionStatus.BLOCKED)

    def test_report_tampering_detected(self) -> None:
        self.write("test_pass.py", PASSING)
        report = self.verify("python -m unittest test_pass")
        rd = report.report_dir
        self.assertTrue(verify_report_integrity(rd)[0])

        md = rd / "report.md"
        original_md = md.read_bytes()
        md.write_bytes(original_md + b"\nforged line\n")
        self.assertFalse(verify_report_integrity(rd)[0])
        md.write_bytes(original_md)

        man = rd / "evidence" / "_run" / "snapshot_manifest.json"
        original_man = man.read_bytes()
        man.write_bytes(original_man.replace(b"README.md", b"README.mx"))
        self.assertFalse(verify_report_integrity(rd)[0])
        man.write_bytes(original_man)

        rj = rd / "report.json"
        data = json.loads(rj.read_text(encoding="utf-8"))
        data["criteria"][0]["observed_source_snapshot_digest"] = "f" * 64
        rj.write_text(json.dumps(data), encoding="utf-8")
        ok, findings = verify_report_integrity(rd)
        self.assertFalse(ok)
        self.assertTrue(any("runner-observed" in f for f in findings), findings)


if __name__ == "__main__":
    unittest.main()
