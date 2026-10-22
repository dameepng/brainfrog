"""Phase 1C: real OCI (Docker) integration tests for the standalone verifier.

These tests execute real containers through BrainFrog's DockerSandboxBackend. They are
skipped (not passed) when the Docker engine or the approved python:3.12-slim image is not
available locally. No test pulls images, and no test touches containers it did not create.

Security probes run *inside* the container as ordinary unittest cases: if an isolation
property did not hold, the in-container assertion fails and the criterion is FAIL.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core.runtime.sandbox import (
    BRAINFROG_OWNER_LABEL,
    BRAINFROG_OWNER_VALUE,
    BRAINFROG_RUN_LABEL,
    DockerSandboxBackend,
    InfraErrorCategory,
    SandboxPolicy,
    SandboxResourceLimits,
    materialize_source_snapshot_with_manifest,
)
from core.verifier import CriterionStatus, OverallVerdict, StandaloneVerifier, verify_report_integrity


def _docker_ready() -> bool:
    try:
        backend = DockerSandboxBackend()
        ok, _ = backend.is_available()
        return ok and backend.get_image_digest() is not None
    except Exception:
        return False


DOCKER_READY = _docker_ready()


def _synthetic_credential() -> str:
    """Deterministic, obviously fake credential-shaped value (never a real secret)."""
    return "bf-synthetic-" + "0" * 8 + "-not-a-real-token"


@unittest.skipUnless(DOCKER_READY, "Docker engine or approved local image python:3.12-slim unavailable")
class TestDockerIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bf_oci_")).resolve()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        for args in (["init"], ["config", "user.email", "oci@test.local"], ["config", "user.name", "OCI Tester"]):
            subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)
        (self.repo / "README.md").write_text("# oci fixture\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.repo, check=True, capture_output=True)
        self.backend = DockerSandboxBackend()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel: str, text: str) -> None:
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def verify(self, criteria: str, timeout: float = 120.0, backend=None):
        self.write("spec.md", "# Spec: OCI\n## Acceptance Criteria\n" + criteria)
        v = StandaloneVerifier(repo_dir=self.repo, sandbox_mode="docker", timeout=timeout,
                               reports_base_dir=self.tmp / "reports", sandbox_backend=backend)
        report = v.verify_spec("spec.md")
        ok, findings = verify_report_integrity(report.report_dir)
        self.assertTrue(ok, findings)
        self.assertEqual(self.backend.list_owned_containers(report.run_id), [], "BrainFrog containers leaked")
        return report

    @staticmethod
    def crit(cid: str, cmd: str, vtype: str = "Test command") -> str:
        return f"### {cid} - {cid}\n**Verification:** {vtype}\n**Command:** {cmd}\n\n"

    # ---- Section 8 acceptance demonstrations -------------------------------------------

    def test_ac01_to_ac05_real_container_verdicts(self) -> None:
        self.write("tests_pass/__init__.py", "")
        self.write("tests_pass/test_ok.py",
                   "import unittest\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(2 + 2, 4)\n")
        self.write("tests_fail/__init__.py", "")
        self.write("tests_fail/test_bad.py",
                   "import unittest\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(2 + 2, 5)\n")
        self.write("tests_empty/__init__.py", "")
        self.write("tests_fake/__init__.py", "")
        self.write("tests_fake/test_fake.py",
                   "import sys\nprint('Ran 999 tests in 0.01s')\nprint('OK')\nsys.stderr.write('Ran 999 tests in 0.01s\\n\\nOK\\n')\n")
        self.write("src_ok/good.py", "def f():\n    return 1\n")
        self.write("src_bad/bad.py", "def f(:\n    return 1\n")
        report = self.verify(
            self.crit("AC-01", "python -m unittest discover -s tests_pass -t .")
            + self.crit("AC-02", "python -m unittest discover -s tests_fail -t .")
            + self.crit("AC-03", "python -m unittest discover -s tests_empty -t .")
            + self.crit("AC-04", "python -m unittest discover -s tests_fake -t .")
            + self.crit("AC-05a", "python -m compileall -q src_ok", "Build command")
            + self.crit("AC-05b", "python -m compileall -q src_bad", "Build command")
        )
        by_id = {c.criterion_id: c for c in report.criteria}
        for c in report.criteria:
            self.assertEqual(c.execution_backend, "oci_container_docker", c.criterion_id)
            self.assertTrue(c.container_id, c.criterion_id)
            self.assertRegex(c.container_image_digest, r"^sha256:[0-9a-f]{64}$")
            self.assertEqual(c.materialized_source_snapshot_digest, report.materialized_source_snapshot_digest)
            self.assertEqual(c.observed_source_snapshot_digest, report.materialized_source_snapshot_digest)
        self.assertEqual(by_id["AC-01"].status, CriterionStatus.PASS)
        self.assertGreaterEqual(by_id["AC-01"].test_count, 1)
        self.assertEqual(by_id["AC-02"].status, CriterionStatus.FAIL)
        self.assertGreaterEqual(by_id["AC-02"].test_failure_count, 1)
        self.assertIn(by_id["AC-03"].status, (CriterionStatus.BLOCKED, CriterionStatus.NOT_RUN))
        self.assertEqual(by_id["AC-03"].test_count, 0)
        self.assertNotEqual(by_id["AC-04"].status, CriterionStatus.PASS)
        self.assertEqual(by_id["AC-05a"].status, CriterionStatus.PASS)
        self.assertEqual(by_id["AC-05b"].status, CriterionStatus.FAIL)
        self.assertEqual(report.overall_verdict, OverallVerdict.FAIL)
        self.assertEqual(len({c.container_id for c in report.criteria}), len(report.criteria))

    # ---- Section 9 sandbox security probes -----------------------------------------------

    def test_isolation_properties_inside_container(self) -> None:
        cred = _synthetic_credential()
        outside = self.tmp / "private_host_sentinel.txt"
        outside.write_text("bf-private-host-sentinel", encoding="utf-8")
        self.write(".gitignore", "ignored_secret.txt\n")
        self.write("ignored_secret.txt", "bf-ignored-sentinel\n")
        probe = f'''
import errno, os, socket, unittest

class Isolation(unittest.TestCase):
    def test_non_root(self):
        self.assertEqual(os.getuid(), 65532)
        self.assertEqual(os.getgid(), 65532)

    def test_source_mount_read_only(self):
        with self.assertRaises(OSError):
            open("/source/README.md", "a").write("mutation")
        with self.assertRaises(OSError):
            open("/source/new_file.txt", "w").write("x")

    def test_root_filesystem_read_only(self):
        for path in ("/etc/bf_probe", "/usr/bf_probe", "/bf_probe"):
            with self.assertRaises(OSError):
                open(path, "w").write("x")

    def test_workspace_and_tmp_writable_but_noexec(self):
        open("/workspace/scratch.txt", "w").write("ok")
        open("/tmp/scratch.txt", "w").write("ok")
        os.chmod("/tmp/scratch.txt", 0o755)
        with self.assertRaises(PermissionError):
            os.execv("/tmp/scratch.txt", ["x"])

    def test_no_network(self):
        names = sorted(os.listdir("/sys/class/net"))
        self.assertEqual(names, ["lo"])
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        with self.assertRaises(OSError):
            s.connect(("1.1.1.1", 53))
        with self.assertRaises(OSError):
            socket.getaddrinfo("example.com", 80)

    def test_no_docker_socket_or_host_paths(self):
        for p in ("/var/run/docker.sock", "/run/docker.sock", "/host", "/c", "/mnt/c", "/root/.docker"):
            self.assertFalse(os.path.exists(p), p)

    def test_no_inherited_credentials(self):
        env_blob = open("/proc/self/environ", "rb").read().decode("utf-8", "replace")
        self.assertNotIn({cred!r}, env_blob)
        self.assertNotIn("BF_SYNTHETIC_CREDENTIAL", env_blob)
        # Image ENV (PATH, LANG, GPG_KEY, PYTHON_*) and the runtime-set HOSTNAME (container ID)
        # are expected; everything else must be exactly what BrainFrog set.
        image_or_runtime = ("PATH", "LANG", "GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256", "HOSTNAME")
        self.assertEqual(sorted(k for k in os.environ if k not in image_or_runtime),
                         ["HOME", "PYTHONDONTWRITEBYTECODE"])
        self.assertNotIn(os.environ.get("HOSTNAME"), ("", None))
        self.assertNotIn(".", os.environ["HOSTNAME"])  # container ID, not a host FQDN

    def test_private_and_ignored_sentinels_absent(self):
        for root in ("/source", "/workspace", "/tmp"):
            for dirpath, _, files in os.walk(root):
                for f in files:
                    data = open(os.path.join(dirpath, f), "rb").read()
                    self.assertNotIn(("bf-private-" + "host-sentinel").encode(), data)
                    self.assertNotIn(("bf-ignored-" + "sentinel").encode(), data)
        self.assertFalse(os.path.exists("/source/ignored_secret.txt"))

    def test_mounts_match_policy(self):
        mounts = [l.split()[1] for l in open("/proc/self/mounts")]
        for required in ("/source", "/bf-harness", "/workspace", "/tmp"):
            self.assertIn(required, mounts)
        opts = {{l.split()[1]: l.split()[3] for l in open("/proc/self/mounts")}}
        self.assertTrue(opts["/source"].startswith("ro"))
        self.assertTrue(opts["/bf-harness"].startswith("ro"))
        self.assertTrue(opts["/"].startswith("ro"))

    def test_resource_limits_active(self):
        self.assertEqual(open("/sys/fs/cgroup/memory.max").read().strip(), str(512 * 1024 * 1024))
        self.assertEqual(open("/sys/fs/cgroup/pids.max").read().strip(), "64")
        quota, period = open("/sys/fs/cgroup/cpu.max").read().split()
        self.assertEqual(int(quota) / int(period), 2.0)

    def test_capabilities_dropped_and_no_new_privs(self):
        status = dict(l.split(":", 1) for l in open("/proc/self/status") if ":" in l)
        self.assertEqual(int(status["CapEff"].strip(), 16), 0)
        self.assertEqual(int(status["CapBnd"].strip(), 16), 0)
        self.assertEqual(status["NoNewPrivs"].strip(), "1")
'''
        self.write("probes/__init__.py", "")
        self.write("probes/test_isolation.py", probe)
        before = {p: p.read_bytes() for p in self.repo.rglob("*") if p.is_file() and ".git" not in p.parts}
        with patch.dict(os.environ, {"BF_SYNTHETIC_CREDENTIAL": cred}):
            report = self.verify(self.crit("SEC-01", "python -m unittest discover -s probes -t . -v"))
        c = report.criteria[0]
        failed_lines = [l for l in c.stderr.splitlines() if l.startswith(("FAIL:", "ERROR:", "AssertionError", "PermissionError", "OSError"))]
        self.assertEqual(c.status, CriterionStatus.PASS, f"{c.failure_reason}\n{failed_lines}")
        self.assertEqual(c.test_count, 11)
        after = {p: p.read_bytes() for p in self.repo.rglob("*") if p.is_file() and ".git" not in p.parts
                 and ".brainfrog" not in p.parts}
        before.pop(self.repo / "spec.md", None)
        after.pop(self.repo / "spec.md", None)
        self.assertEqual(before, after, "host repository must be unchanged by container execution")
        cfg = c.effective_container_config
        self.assertEqual(cfg["user"], "65532:65532")
        self.assertEqual(cfg["network_mode"], "none")
        self.assertTrue(cfg["readonly_rootfs"])
        self.assertFalse(cfg["privileged"])
        self.assertEqual(cfg["cap_drop"], ["ALL"])
        self.assertEqual(cfg["pids_limit"], 64)
        self.assertEqual(cfg["memory_bytes"], 512 * 1024 * 1024)
        self.assertEqual(cfg["labels"][BRAINFROG_OWNER_LABEL], BRAINFROG_OWNER_VALUE)
        self.assertEqual(cfg["labels"][BRAINFROG_RUN_LABEL], report.run_id)

    def test_staged_harness_is_readable_but_not_modifiable_by_container_user(self) -> None:
        # T1 (GitHub Linux): a 0700 staging dir made the harness unreadable to UID 65532. The candidate's own
        # test, run by that harness, checks it can read it and cannot change, replace or add to it.
        self.write("hprobe/__init__.py", "")
        self.write("hprobe/test_harness_mount.py",
                   "import os, unittest\n"
                   "H = '/bf-harness/verification_harness.py'\n"
                   "class T(unittest.TestCase):\n"
                   "    def test_mount(self):\n"
                   "        self.assertEqual((os.getuid(), os.getgid()), (65532, 65532))\n"
                   "        with open(H, 'rb') as f:\n"
                   "            self.assertTrue(f.read(64))\n"
                   "        self.assertTrue(os.access('/bf-harness', os.R_OK | os.X_OK))\n"
                   "        for target, mode in ((H, 'ab'), (H, 'wb'), ('/bf-harness/planted.py', 'wb')):\n"
                   "            with self.assertRaises(OSError, msg=f'{target} {mode}'):\n"
                   "                open(target, mode).close()\n"
                   "        for action in (lambda: os.chmod(H, 0o777), lambda: os.remove(H),\n"
                   "                       lambda: os.rename(H, '/bf-harness/x.py')):\n"
                   "            with self.assertRaises(OSError):\n"
                   "                action()\n")
        report = self.verify(self.crit("HM-1", "python -m unittest hprobe.test_harness_mount"))
        c = report.criteria[0]
        self.assertEqual(c.status, CriterionStatus.PASS, f"{c.failure_reason}\n{(c.stderr or '')[-1500:]}")
        self.assertEqual(c.test_count, 1)
        self.assertEqual(c.effective_container_config["user"], "65532:65532")
        harness_mounts = [m for m in c.effective_container_config["mounts"] if m["destination"] == "/bf-harness"]
        self.assertEqual([m["rw"] for m in harness_mounts], [False])

    def test_symlink_in_repository_is_not_followed(self) -> None:
        # Symlinks are created inside the container-visible tree only if the host permits.
        target = self.tmp / "outside.txt"
        target.write_text("bf-outside-sentinel", encoding="utf-8")
        try:
            os.symlink(target, self.repo / "escape_link.txt")
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"host does not permit symlink creation: {exc}")
        snap = materialize_source_snapshot_with_manifest(self.repo, self.tmp / "snap")
        self.assertIn("escape_link.txt", snap.skipped_symlinks)

    def test_workspace_is_ephemeral_between_criteria(self) -> None:
        self.write("eph/__init__.py", "")
        # Each criterion runs in a fresh container: the markers written by EPH-1 must be absent in EPH-2.
        self.write("eph/test_write.py",
                   "import os, unittest\nclass W(unittest.TestCase):\n    def test_w(self):\n"
                   "        self.assertFalse(os.path.exists('/workspace/persist.txt'))\n"
                   "        self.assertFalse(os.path.exists('/tmp/persist.txt'))\n"
                   "        open('/workspace/persist.txt', 'w').write('x')\n"
                   "        open('/tmp/persist.txt', 'w').write('x')\n")
        report = self.verify(self.crit("EPH-1", "python -m unittest eph.test_write")
                             + self.crit("EPH-2", "python -m unittest eph.test_write"))
        self.assertEqual([c.status for c in report.criteria], [CriterionStatus.PASS, CriterionStatus.PASS])
        self.assertFalse((self.repo / "persist.txt").exists())

    def test_timeout_kills_and_removes_owned_container(self) -> None:
        self.write("slow/__init__.py", "")
        self.write("slow/test_slow.py",
                   "import time, unittest\nclass S(unittest.TestCase):\n    def test_s(self):\n        time.sleep(120)\n")
        t0 = time.monotonic()
        report = self.verify(self.crit("TMO", "python -m unittest discover -s slow -t ."), timeout=8.0)
        c = report.criteria[0]
        self.assertEqual(c.status, CriterionStatus.BLOCKED)
        self.assertEqual(c.infrastructure_error_category, InfraErrorCategory.TIMEOUT)
        self.assertLess(time.monotonic() - t0, 90)
        self.assertIsNone(self.backend._inspect(c.container_name))

    def test_cancellation_removes_owned_container(self) -> None:
        self.write("t/__init__.py", "")
        self.write("t/test_a.py", "import unittest\nclass A(unittest.TestCase):\n    def test_a(self):\n        pass\n")
        snap = materialize_source_snapshot_with_manifest(self.repo, self.tmp / "snap")
        run_label = "cancel_" + secrets.token_hex(4)
        backend = DockerSandboxBackend()
        real = backend._docker

        def interrupting(args, *a, **kw):
            if args and args[0] == "start":
                self.assertEqual(len(backend.list_owned_containers(run_label)), 1)  # really created
                raise KeyboardInterrupt
            return real(args, *a, **kw)

        backend._docker = interrupting
        with self.assertRaises(KeyboardInterrupt):
            backend.execute(snap.snapshot_dir, snap.digest, ["python", "-m", "unittest", "discover", "-s", "t", "-t", "."],
                            SandboxPolicy(), SandboxResourceLimits(), run_label=run_label)
        self.assertEqual(DockerSandboxBackend().list_owned_containers(run_label), [])

    def test_cleanup_only_touches_current_run_containers(self) -> None:
        decoy_label = "decoy_" + secrets.token_hex(4)
        decoy = f"bf_decoy_{decoy_label}"
        image_id = self.backend.resolve_image()["image_id"]
        rc = subprocess.run(["docker", "create", "--pull=never", f"--name={decoy}",
                             f"--label={BRAINFROG_OWNER_LABEL}={BRAINFROG_OWNER_VALUE}",
                             f"--label={BRAINFROG_RUN_LABEL}={decoy_label}", "--network=none", image_id, "true"],
                            capture_output=True, text=True)
        self.assertEqual(rc.returncode, 0, rc.stderr)
        try:
            self.assertFalse(self.backend.remove_owned_container(decoy, "some_other_run"))
            self.assertIsNotNone(self.backend._inspect(decoy), "foreign-run container must not be removed")
            self.assertTrue(self.backend.remove_owned_container(decoy, decoy_label))
            self.assertIsNone(self.backend._inspect(decoy))
        finally:
            if self.backend._inspect(decoy) is not None:
                self.backend.remove_owned_container(decoy, decoy_label)

    def test_missing_image_blocks_and_never_pulls(self) -> None:
        backend = DockerSandboxBackend(image_name="python:3.11-slim")
        if backend.get_image_digest() is not None:
            self.skipTest("python:3.11-slim is present locally; cannot exercise missing-image path")
        self.write("t/__init__.py", "")
        self.write("t/test_a.py", "import unittest\nclass A(unittest.TestCase):\n    def test_a(self):\n        pass\n")
        report = self.verify(self.crit("IMG", "python -m unittest discover -s t -t ."), backend=backend)
        c = report.criteria[0]
        self.assertEqual(c.status, CriterionStatus.BLOCKED)
        self.assertEqual(c.infrastructure_error_category, InfraErrorCategory.IMAGE_MISSING)
        self.assertIsNone(backend.get_image_digest(), "image must not have been pulled")

    def test_container_create_failure_is_blocked_not_fail(self) -> None:
        self.write("t/__init__.py", "")
        self.write("t/test_a.py", "import unittest\nclass A(unittest.TestCase):\n    def test_a(self):\n        pass\n")
        snap = materialize_source_snapshot_with_manifest(self.repo, self.tmp / "snap")
        limits = SandboxResourceLimits(memory_mb=1)  # engine rejects < 6MB
        res = self.backend.execute(snap.snapshot_dir, snap.digest, ["python", "-m", "unittest", "discover", "-s", "t"],
                                   SandboxPolicy(), limits, run_label="badlimit_" + secrets.token_hex(3))
        self.assertEqual(res.outcome.verdict, "BLOCKED")
        self.assertIn(res.infrastructure_error_category,
                      (InfraErrorCategory.CONTAINER_CREATE_FAILED, InfraErrorCategory.SECURITY_POLICY_NOT_ENFORCED))

    def test_oom_is_resource_limit_blocked(self) -> None:
        self.write("m/__init__.py", "")
        self.write("m/test_mem.py",
                   "import unittest\nclass M(unittest.TestCase):\n    def test_m(self):\n"
                   "        chunks = []\n        while True:\n            chunks.append(bytearray(32 * 1024 * 1024))\n")
        report = self.verify(self.crit("OOM", "python -m unittest discover -s m -t ."))
        c = report.criteria[0]
        self.assertEqual(c.status, CriterionStatus.BLOCKED, c.failure_reason)
        self.assertEqual(c.infrastructure_error_category, InfraErrorCategory.RESOURCE_LIMIT)

    def test_untrusted_spec_cannot_alter_security_settings(self) -> None:
        self.write("t/__init__.py", "")
        self.write("t/test_net.py",
                   "import os, unittest\nclass N(unittest.TestCase):\n    def test_n(self):\n"
                   "        self.assertEqual(sorted(os.listdir('/sys/class/net')), ['lo'])\n        self.assertNotEqual(os.getuid(), 0)\n")
        spec_extra = (
            "### SPEC-01 - attempt\n**Verification:** Test command\n**Image:** alpine:latest\n"
            "**Command:** python -m unittest discover -s t -t . --network=host --privileged\n\n"
            + self.crit("SPEC-02", "python -m unittest discover -s t -t .")
        )
        report = self.verify(spec_extra)
        c1, c2 = report.criteria
        self.assertNotEqual(c1.status, CriterionStatus.PASS)  # unknown unittest options -> usage error
        self.assertEqual(c2.status, CriterionStatus.PASS)
        for c in (c1, c2):
            self.assertEqual(c.effective_container_config["network_mode"], "none")
            self.assertFalse(c.effective_container_config["privileged"])
            self.assertEqual(c.effective_container_config["image_id"], report.container_image_resolution["image_id"])


if __name__ == "__main__":
    unittest.main()
