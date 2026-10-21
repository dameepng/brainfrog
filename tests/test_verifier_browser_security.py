"""Phase 1D: browser verification security policy (no Docker required).

Covers trusted app profiles, browser image approval, the fixed hardened container/network
argument vectors, effective-configuration verification, ownership-checked cleanup, nonce
binding of runner records, and the host-side recomputation of browser verdicts.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from core.runtime.app_profile import APPROVED_PROFILES, UnsupportedProfileError, get_app_profile
from core.runtime.browser_plan import parse_browser_plan
from core.runtime import browser_sandbox
from core.runtime.browser_sandbox import (
    BROWSER_RECORD_PREFIX,
    BROWSER_SECCOMP_PROFILE_PATH,
    BROWSER_SECCOMP_PROFILE_SHA256,
    DockerBrowserSandbox,
    extract_records,
    load_browser_seccomp_profile,
    recompute_browser_verdict,
)
from core.runtime.sandbox import SandboxInfraError
from core.runtime.sandbox import BRAINFROG_OWNER_LABEL, BRAINFROG_OWNER_VALUE, BRAINFROG_RUN_LABEL, InfraErrorCategory
from core.verifier import CriterionStatus, ExecutionTrustMode, OverallVerdict, StandaloneVerifier
from tests.browser_verification_helpers import (
    SIMPLE_PLAN,
    STUB_IMAGE_ID,
    StubBrowserBackend,
    browser_criterion,
    spec_document,
    synthetic_result,
)

NET = "bf1d_net_test"
RUN = "verify_run.AC-01.abc"
IMG = "sha256:" + "0" * 64


class TestAppProfiles(unittest.TestCase):
    def test_01_unapproved_profiles_rejected(self) -> None:
        for pid in (None, "", "node-dev", "python-static; rm -rf /", "../python-static", "docker"):
            with self.subTest(profile=pid):
                with self.assertRaises(UnsupportedProfileError):
                    get_app_profile(pid)
        self.assertEqual(get_app_profile(" Python-Static ").profile_id, "python-static")

    def test_02_profile_registry_and_profiles_are_immutable(self) -> None:
        with self.assertRaises(TypeError):
            APPROVED_PROFILES["evil"] = APPROVED_PROFILES["python-static"]  # type: ignore[index]
        prof = get_app_profile("python-static")
        with self.assertRaises(Exception):
            prof.approved_startup_argv = ("sh", "-c", "id")  # type: ignore[misc]
        self.assertIsInstance(prof.approved_startup_argv, tuple)
        # The approved argv runs BrainFrog's launcher, never repository code or a shell.
        self.assertEqual(prof.approved_startup_argv[:4], ("python", "-I", "-B", "/bf-harness/app_launcher.py"))
        self.assertNotIn("sh", prof.approved_startup_argv)

    def test_03_spec_profile_field_only_selects_an_id(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="bf_prof_"))
        try:
            (tmp / "spec.md").write_text(spec_document(
                browser_criterion("AC-01", SIMPLE_PLAN, profile="python-static --privileged")), encoding="utf-8")
            backend = _AvailableNoDocker()
            report = StandaloneVerifier(repo_dir=tmp, sandbox_mode="docker", browser_backend=backend,
                                        reports_base_dir=tmp / "reports").verify_spec(tmp / "spec.md")
            crit = report.criteria[0]
            self.assertEqual(crit.status, CriterionStatus.BLOCKED)
            self.assertIn("Unsupported Application Startup Profile", crit.failure_reason)
            self.assertEqual(crit.infrastructure_error_category, "unsupported_app_profile")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class _AvailableNoDocker(DockerBrowserSandbox):
    """Real execute_browser_criterion, but every docker call fails: exercises early policy paths."""

    def __init__(self) -> None:
        super().__init__(docker_executable="bf-nonexistent-docker-cli")

    def is_available(self):
        return True, None

    def engine_version(self):
        return None


class TestImagesAndArguments(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bf_args_"))
        for d in ("snap", "app_harness", "runner", "input"):
            (self.tmp / d).mkdir()
        self.sb = DockerBrowserSandbox(docker_executable="bf-nonexistent-docker-cli")
        self.profile = get_app_profile("python-static")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_01_unapproved_browser_image_rejected(self) -> None:
        for img in ("mcr.microsoft.com/playwright/python:v1.49.1-noble", "evil:latest", "python:3.12-slim"):
            with self.subTest(image=img):
                with self.assertRaises(ValueError):
                    DockerBrowserSandbox(browser_image=img)

    def _common_hardening(self, args):
        for required in ("--pull=never", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                         "--user=65532:65532", "--ipc=private", "--restart=no", f"--network={NET}",
                         f"--label={BRAINFROG_OWNER_LABEL}={BRAINFROG_OWNER_VALUE}",
                         f"--label={BRAINFROG_RUN_LABEL}={RUN}", "--label=io.brainfrog.phase=1d"):
            self.assertIn(required, args)
        joined = " ".join(args)
        for forbidden in ("--privileged", "docker.sock", "seccomp=unconfined", "apparmor=unconfined",
                          "--network=host", "host.docker.internal", "--add-host", "--rm"):
            self.assertNotIn(forbidden, joined)
        self.assertFalse(any(a in ("-p", "-P", "--publish", "--publish-all") or a.startswith("--publish") for a in args))
        self.assertFalse(any(a in ("-v", "--volume") or a.startswith("--volume") for a in args))
        mounts = [a for a in args if a.startswith("--mount=")]
        self.assertTrue(mounts)
        self.assertTrue(all(m.endswith(",readonly") for m in mounts), mounts)
        self.assertTrue(any(a.startswith("--memory=") for a in args))
        self.assertTrue(any(a.startswith("--cpus=") for a in args))
        self.assertTrue(any(a.startswith("--pids-limit=") for a in args))

    def test_02_network_create_is_internal_and_labelled(self) -> None:
        args = self.sb.build_network_create_args(NET, RUN, "AC-01")
        self.assertEqual(args[:4], ["network", "create", "--internal", "--driver=bridge"])
        self.assertIn(f"--label={BRAINFROG_RUN_LABEL}={RUN}", args)
        self.assertEqual(args[-1], NET)

    def test_03_app_container_args_hardened_and_fixed(self) -> None:
        args = self.sb.build_app_create_args("bf1d_app_x", NET, RUN, "AC-01", self.tmp / "snap",
                                             self.tmp / "app_harness", IMG, self.profile)
        self._common_hardening(args)
        self.assertIn("--network-alias=bf-app", args)
        self.assertIn(f"--mount=type=bind,source={(self.tmp / 'snap').resolve()},target=/app,readonly", args)
        idx = args.index(IMG)
        self.assertEqual(args[idx + 1:], list(self.profile.approved_startup_argv))
        self.assertEqual(sorted(a for a in args if a.startswith("--env=")),
                         ["--env=HOME=/tmp", "--env=PYTHONDONTWRITEBYTECODE=1", "--env=PYTHONUNBUFFERED=1"])

    def test_04_browser_container_args_hardened_and_fixed(self) -> None:
        args = self.sb.build_browser_create_args("bf1d_brw_x", NET, RUN, "AC-01", self.tmp / "runner",
                                                 self.tmp / "input", IMG, "http://bf-app:8080", "/", "d" * 64)
        self._common_hardening(args)
        self.assertIn("--interactive", args)
        self.assertIn("--log-driver=none", args)
        self.assertTrue(any(a.startswith("--shm-size=") for a in args))
        self.assertTrue(any(a.startswith("--tmpfs=/evidence:") and "noexec" in a for a in args))
        idx = args.index(IMG)
        self.assertEqual(args[idx + 1:idx + 5], ["python3", "-I", "-B", "/bf-harness/browser_runner.py"])
        self.assertEqual(args[args.index("--origin") + 1], "http://bf-app:8080")
        seccomp = [a for a in args if a.startswith("--security-opt=seccomp=")]
        self.assertEqual(seccomp, [f"--security-opt=seccomp={BROWSER_SECCOMP_PROFILE_PATH.resolve()}"])

    def test_04b_app_container_keeps_docker_default_seccomp(self) -> None:
        args = self.sb.build_app_create_args("bf1d_app_x", NET, RUN, "AC-01", self.tmp / "snap",
                                             self.tmp / "app_harness", IMG, self.profile)
        self.assertFalse(any("seccomp" in a for a in args))
        insp = _inspect(SecurityOpt=["no-new-privileges", PINNED_SECCOMP], tmpfs={"/tmp": ""})
        self.assertIn("app container must use Docker's default seccomp profile",
                      self.sb.verify_app_config(insp, NET, IMG, RUN, self.profile, "S", "H"))

    def test_04c_outdated_runtime_is_no_longer_approved(self) -> None:
        with self.assertRaises(ValueError):
            DockerBrowserSandbox(browser_image="brainfrog-browser-runner:v1.49.1")
        approval = browser_sandbox.APPROVED_BROWSER_IMAGES[browser_sandbox.DEFAULT_BROWSER_IMAGE]
        self.assertEqual(approval["playwright_version"], "1.64.0")
        self.assertRegex(approval["image_id"], r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(approval["base_image"], r"@sha256:[0-9a-f]{64}$")

    def test_05_criterion_ids_outside_allowlist_blocked(self) -> None:
        plan = parse_browser_plan(SIMPLE_PLAN)
        res = self.sb.execute_browser_criterion("AC 01;rm", plan, self.tmp / "snap", "d" * 64)
        self.assertEqual(res.status, "BLOCKED")


PINNED_SECCOMP = "seccomp=" + BROWSER_SECCOMP_PROFILE_PATH.read_text(encoding="utf-8")


def _inspect(network=NET, image=IMG, user="65532:65532", mounts=None, tmpfs=None, **hc_over):
    hc = {"NetworkMode": network, "ReadonlyRootfs": True, "Privileged": False,
          "SecurityOpt": ["no-new-privileges", PINNED_SECCOMP],
          "CapDrop": ["ALL"], "CapAdd": None, "PidMode": "", "IpcMode": "private", "UTSMode": "",
          "Memory": 1536 * 1024 * 1024, "MemorySwap": 1536 * 1024 * 1024, "NanoCpus": 2_000_000_000,
          "PidsLimit": 256, "ShmSize": 256 * 1024 * 1024, "Devices": [], "PortBindings": {}, "PublishAllPorts": False,
          "Binds": None, "Tmpfs": tmpfs if tmpfs is not None else {"/tmp": "", "/evidence": ""}}
    hc.update(hc_over)
    return {"Image": image, "Config": {"User": user, "Env": ["PATH=/usr/bin", "HOME=/tmp"],
                                       "Labels": {BRAINFROG_OWNER_LABEL: BRAINFROG_OWNER_VALUE, BRAINFROG_RUN_LABEL: RUN}},
            "HostConfig": hc, "NetworkSettings": {"Networks": {network: {"Aliases": []}}},
            "Mounts": mounts if mounts is not None else [
                {"Type": "bind", "Source": "R", "Destination": "/bf-harness", "RW": False},
                {"Type": "bind", "Source": "I", "Destination": "/bf-input", "RW": False}]}


class TestEffectiveConfigVerification(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = DockerBrowserSandbox(docker_executable="bf-nonexistent-docker-cli")

    def _v(self, inspect):
        return self.sb.verify_browser_config(inspect, NET, IMG, RUN, "R", "I")

    def test_01_compliant_config_has_no_violations(self) -> None:
        self.assertEqual(self._v(_inspect()), [])

    def test_02_each_weakening_is_detected(self) -> None:
        cases = {
            "privileged": _inspect(Privileged=True),
            "seccomp": _inspect(SecurityOpt=["no-new-privileges", "seccomp=unconfined"]),
            "seccomp_default_only": _inspect(SecurityOpt=["no-new-privileges"]),
            "seccomp_other_profile": _inspect(SecurityOpt=["no-new-privileges",
                                                           'seccomp={"defaultAction":"SCMP_ACT_ALLOW"}']),
            "nnp": _inspect(SecurityOpt=[PINNED_SECCOMP]),
            "caps": _inspect(CapAdd=["NET_ADMIN"]),
            "ports": _inspect(PortBindings={"8080/tcp": [{"HostPort": "8080"}]}),
            "publish_all": _inspect(PublishAllPorts=True),
            "host_network": _inspect(network="host"),
            "root_user": _inspect(user="root"),
            "rw_rootfs": _inspect(ReadonlyRootfs=False),
            "image": _inspect(image="sha256:" + "f" * 64),
            "memory": _inspect(Memory=0),
            "pids": _inspect(PidsLimit=-1),
            "ipc_host": _inspect(IpcMode="host"),
            "rw_mount": _inspect(mounts=[{"Type": "bind", "Source": "R", "Destination": "/bf-harness", "RW": True},
                                         {"Type": "bind", "Source": "I", "Destination": "/bf-input", "RW": False}]),
            "extra_mount": _inspect(mounts=[{"Type": "bind", "Source": "R", "Destination": "/bf-harness", "RW": False},
                                            {"Type": "bind", "Source": "I", "Destination": "/bf-input", "RW": False},
                                            {"Type": "bind", "Source": "/var/run/docker.sock",
                                             "Destination": "/var/run/docker.sock", "RW": False}]),
            "tmpfs": _inspect(tmpfs={"/tmp": ""}),
            "extra_hosts": _inspect(ExtraHosts=["host.docker.internal:host-gateway"]),
        }
        for name, insp in cases.items():
            with self.subTest(case=name):
                self.assertTrue(self._v(insp), f"{name} weakening not detected")

    def test_03_unowned_container_flagged(self) -> None:
        insp = _inspect()
        insp["Config"]["Labels"] = {}
        self.assertIn("ownership labels missing", self._v(insp))

    def test_04_network_policy(self) -> None:
        good = {"Internal": True, "Driver": "bridge", "Containers": {"app1": {}},
                "Labels": {BRAINFROG_OWNER_LABEL: BRAINFROG_OWNER_VALUE, BRAINFROG_RUN_LABEL: RUN}}
        self.assertEqual(DockerBrowserSandbox.verify_network(good, RUN, ["app1"]), [])
        for mutate in (lambda n: n.update(Internal=False), lambda n: n.update(Driver="host"),
                       lambda n: n.update(Labels={}), lambda n: n["Containers"].update({"intruder": {}})):
            bad = copy.deepcopy(good)
            mutate(bad)
            self.assertTrue(DockerBrowserSandbox.verify_network(bad, RUN, ["app1"]))


class TestBrowserSeccompProfile(unittest.TestCase):
    """The browser seccomp profile is Docker's default plus one reviewed rule, pinned by hash."""

    def test_01_profile_matches_pinned_hash(self) -> None:
        self.assertEqual(hashlib.sha256(BROWSER_SECCOMP_PROFILE_PATH.read_bytes()).hexdigest(),
                         BROWSER_SECCOMP_PROFILE_SHA256)
        _, profile = load_browser_seccomp_profile()
        self.assertEqual(profile["defaultAction"], "SCMP_ACT_ERRNO")

    def test_02_only_namespace_sandbox_syscalls_are_added(self) -> None:
        _, profile = load_browser_seccomp_profile()
        added = profile["syscalls"][-1]
        self.assertEqual(sorted(added["names"]), ["chroot", "clone", "unshare"])
        self.assertEqual(added["action"], "SCMP_ACT_ALLOW")
        unconditional = set()
        for rule in profile["syscalls"][:-1]:
            if rule.get("action") == "SCMP_ACT_ALLOW" and not rule.get("args") and not rule.get("includes"):
                unconditional |= set(rule["names"])
        # Docker's default never allows these unconditionally; the BrainFrog rule adds only the three above.
        for name in ("clone", "unshare", "chroot", "setns", "mount", "io_uring_setup", "bpf", "ptrace"):
            self.assertNotIn(name, unconditional, name)
        everything = {n for r in profile["syscalls"] if r.get("action") == "SCMP_ACT_ALLOW" for n in r["names"]}
        self.assertNotIn("io_uring_setup", everything)

    def test_03_tampered_profile_refused(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="bf_seccomp_"))
        try:
            forged = tmp / "browser_seccomp_chromium.json"
            forged.write_text('{"defaultAction": "SCMP_ACT_ALLOW", "syscalls": []}', encoding="utf-8")
            original = browser_sandbox.BROWSER_SECCOMP_PROFILE_PATH
            browser_sandbox.BROWSER_SECCOMP_PROFILE_PATH = forged
            try:
                with self.assertRaises(SandboxInfraError):
                    load_browser_seccomp_profile()
            finally:
                browser_sandbox.BROWSER_SECCOMP_PROFILE_PATH = original
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class _RecordingDocker(DockerBrowserSandbox):
    """Captures docker invocations; answers inspect calls with a canned network document."""

    def __init__(self, network_doc) -> None:
        super().__init__(docker_executable="bf-nonexistent-docker-cli")
        self.network_doc = network_doc
        self.calls = []

    def _docker(self, args, **kw):
        self.calls.append(list(args))
        if args[:2] == ["network", "inspect"]:
            return (0, json.dumps(self.network_doc), "", False, False) if self.network_doc else (1, "", "", False, False)
        return 0, "", "", False, False


class TestOwnershipAndCleanup(unittest.TestCase):
    def test_01_foreign_network_never_removed(self) -> None:
        sb = _RecordingDocker({"Id": "n1", "Labels": {"com.example": "other-project"}})
        self.assertFalse(sb.remove_owned_network("shared_backend_default", RUN))
        self.assertFalse(any(c[:2] == ["network", "rm"] for c in sb.calls))

    def test_02_other_runs_network_never_removed(self) -> None:
        sb = _RecordingDocker({"Id": "n1", "Labels": {BRAINFROG_OWNER_LABEL: BRAINFROG_OWNER_VALUE,
                                                      BRAINFROG_RUN_LABEL: "another-run"}})
        self.assertFalse(sb.remove_owned_network(NET, RUN))
        self.assertFalse(any(c[:2] == ["network", "rm"] for c in sb.calls))

    def test_03_owned_network_removed_by_id(self) -> None:
        sb = _RecordingDocker({"Id": "n1", "Labels": {BRAINFROG_OWNER_LABEL: BRAINFROG_OWNER_VALUE,
                                                      BRAINFROG_RUN_LABEL: RUN}})
        sb.remove_owned_network(NET, RUN)
        self.assertIn(["network", "rm", "n1"], sb.calls)
        self.assertFalse(any("prune" in " ".join(c) for c in sb.calls))

    def test_04_docker_unavailable_blocks_without_host_fallback(self) -> None:
        sb = DockerBrowserSandbox(docker_executable="bf-nonexistent-docker-cli")
        ok, reason = sb.is_available()
        self.assertFalse(ok)
        self.assertTrue(reason)
        tmp = Path(tempfile.mkdtemp(prefix="bf_nodock_"))
        try:
            res = sb.execute_browser_criterion("AC-01", parse_browser_plan(SIMPLE_PLAN), tmp, "d" * 64)
            self.assertEqual(res.status, "BLOCKED")
            self.assertEqual(res.infrastructure_error_category, InfraErrorCategory.DOCKER_UNAVAILABLE)
            self.assertEqual(res.artifacts, {})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


NONCE = "n" * 64


def _line(event, nonce=NONCE, **payload):
    return BROWSER_RECORD_PREFIX + json.dumps(dict({"event": event, "nonce": nonce}, **payload))


def _runner_stdout(result, nonce=NONCE, drop_complete=False, tamper_artifact=None, extra_artifact=False):
    lines = [_line("started", nonce, criterion_id="AC-01")]
    hashes = {}
    for name, data in result.artifacts.items():
        digest = hashlib.sha256(data).hexdigest()
        hashes[name] = {"sha256": digest, "size": len(data)}
        payload = data + b"x" if name == tamper_artifact else data
        lines.append(_line("artifact", nonce, name=name, sha256=digest, size=len(data),
                           data_b64=base64.b64encode(payload).decode()))
    if extra_artifact:
        lines.append(_line("artifact", nonce, name="evil.sh", sha256="0", size=1, data_b64="eA=="))
    if not drop_complete:
        lines.append(_line("complete", nonce, criterion_id="AC-01", status=result.status,
                           failure_kind=result.failure_kind, failure_reason=result.failure_reason,
                           summary=result.assertion_manifest["summary"], artifacts=hashes))
    return "\n".join(lines) + "\n"


class TestRunnerResultInterpretation(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = parse_browser_plan(SIMPLE_PLAN)
        self.digest = "d" * 64
        self.image = {"image_id": STUB_IMAGE_ID, "expected_playwright_version": None}

    def _interpret(self, stdout, exit_code=0):
        def infra(category, reason, kind=None):
            from core.runtime.browser_sandbox import BrowserExecutionResult
            return BrowserExecutionResult(status="BLOCKED", duration_ms=0, failure_reason=reason,
                                          failure_kind=kind or category, infrastructure_error_category=category)
        return DockerBrowserSandbox._interpret(stdout, "", exit_code, NONCE, "AC-01", self.plan,
                                               self.plan.plan_sha256(), self.digest, self.image, {}, infra)

    def _result(self, **kw):
        res = synthetic_result("AC-01", self.plan, self.digest, **kw)
        return res

    def test_01_valid_pass_and_fail_accepted(self) -> None:
        self.assertEqual(self._interpret(_runner_stdout(self._result())).status, "PASS")
        self.assertEqual(self._interpret(_runner_stdout(self._result(status="FAIL")), 1).status, "FAIL")

    def test_02_records_with_wrong_nonce_are_forgery(self) -> None:
        out = _runner_stdout(self._result()) + _line("complete", "f" * 64, status="PASS") + "\n"
        res = self._interpret(out)
        self.assertEqual(res.status, "BLOCKED")
        self.assertEqual(res.infrastructure_error_category, InfraErrorCategory.FORGED_RUNNER_RECORD)
        self.assertEqual(extract_records(out, BROWSER_RECORD_PREFIX, NONCE)[1], 1)

    def test_03_interrupted_run_without_completion_blocked(self) -> None:
        res = self._interpret(_runner_stdout(self._result(), drop_complete=True), exit_code=137)
        self.assertEqual(res.status, "BLOCKED")
        self.assertEqual(res.failure_kind, "browser_crashed")
        self.assertEqual(self._interpret("", exit_code=1).infrastructure_error_category,
                         InfraErrorCategory.RUNNER_NOT_STARTED)

    def test_04_artifact_hash_tampering_and_unexpected_files_blocked(self) -> None:
        self.assertEqual(self._interpret(_runner_stdout(self._result(), tamper_artifact="screenshot.png")).status,
                         "BLOCKED")
        self.assertEqual(self._interpret(_runner_stdout(self._result(), extra_artifact=True)).status, "BLOCKED")

    def test_05_binding_mismatches_blocked(self) -> None:
        for override in ({"criterion_id": "AC-99"}, {"plan_sha256": "0" * 64},
                         {"source_snapshot_digest": "e" * 64}, {"browser_image_identity": "sha256:" + "1" * 64}):
            with self.subTest(override=override):
                res = self._interpret(_runner_stdout(self._result(manifest_overrides=override)))
                self.assertEqual(res.status, "BLOCKED")
                self.assertEqual(res.failure_kind, "evidence_binding_failed")

    def test_06_runner_status_must_match_recomputed_verdict(self) -> None:
        res = self._result()
        manifest = res.assertion_manifest
        manifest["records"][-1]["status"] = "FAIL"  # records say FAIL, runner claims PASS
        res.artifacts["browser-assertions.json"] = json.dumps(manifest).encode()
        self.assertEqual(self._interpret(_runner_stdout(res)).status, "BLOCKED")

    def test_06b_verdict_requires_proven_renderer_sandbox(self) -> None:
        for runtime_override in ({"chromium_internal_sandbox": False},
                                 {"renderer_sandbox": {"verified": False}},
                                 {"renderer_sandbox_at_end": None}):
            with self.subTest(runtime=runtime_override):
                res = self._result()
                res.assertion_manifest["runtime"].update(runtime_override)
                res.artifacts["browser-assertions.json"] = json.dumps(res.assertion_manifest).encode()
                out = self._interpret(_runner_stdout(res))
                self.assertEqual(out.status, "BLOCKED")
                self.assertIn("renderer_sandbox_unverified", out.failure_reason)

    def test_07_recompute_requires_assertions_screenshot_and_full_plan(self) -> None:
        good = self._result().assertion_manifest
        self.assertEqual(recompute_browser_verdict(good, self.plan), ("PASS", None))
        no_shot = self._result(screenshot=False).assertion_manifest
        self.assertEqual(recompute_browser_verdict(no_shot, self.plan)[0], "BLOCKED")
        partial = copy.deepcopy(good)
        partial["records"] = partial["records"][:1]
        self.assertEqual(recompute_browser_verdict(partial, self.plan)[0], "BLOCKED")
        relabelled = copy.deepcopy(good)
        relabelled["records"][0]["kind"] = "assertion"  # an action counted as an assertion
        self.assertEqual(recompute_browser_verdict(relabelled, self.plan)[0], "BLOCKED")
        reordered = copy.deepcopy(good)
        reordered["records"].reverse()
        self.assertEqual(recompute_browser_verdict(reordered, self.plan)[0], "BLOCKED")
        self.assertEqual(recompute_browser_verdict(self._result(status="FAIL").assertion_manifest, self.plan)[0], "FAIL")


class TestVerifierTrustBoundary(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bf_btrust_"))
        (self.tmp / "index.html").write_text("<p>x</p>", encoding="utf-8")
        (self.tmp / "spec.md").write_text(spec_document(browser_criterion("AC-01", SIMPLE_PLAN)), encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _verify(self, **kw):
        return StandaloneVerifier(repo_dir=self.tmp, reports_base_dir=self.tmp / "reports", **kw).verify_spec(
            self.tmp / "spec.md")

    def test_01_fail_closed_mode_never_runs_browser(self) -> None:
        backend = StubBrowserBackend({})
        report = self._verify(execution_trust_mode=ExecutionTrustMode.UNTRUSTED_FAIL_CLOSED, browser_backend=backend)
        self.assertEqual(report.criteria[0].status, CriterionStatus.BLOCKED)
        self.assertEqual(backend.calls, [])

    def test_02_unavailable_backend_blocks_without_host_fallback(self) -> None:
        backend = StubBrowserBackend({}, available=False)
        for mode in (ExecutionTrustMode.ISOLATED_SANDBOX, ExecutionTrustMode.TRUSTED_OPERATOR):
            with self.subTest(mode=mode):
                report = self._verify(execution_trust_mode=mode, browser_backend=backend)
                crit = report.criteria[0]
                self.assertEqual(crit.status, CriterionStatus.BLOCKED)
                self.assertEqual(crit.execution_backend, "unavailable_sandbox")
                self.assertEqual(report.overall_verdict, OverallVerdict.BLOCKED)
        self.assertEqual(backend.calls, [])

    def test_03_invalid_or_missing_plan_blocked_before_execution(self) -> None:
        (self.tmp / "spec.md").write_text(spec_document(
            browser_criterion("AC-01", '{"path": "/", "steps": [{"action": "goto_relative", "path": "/x"}]}'),
            browser_criterion("AC-02", '{"path": "https://example.com", "steps": []}'),
            "### AC-03 — No plan\n**Verification:** Browser\n",
        ), encoding="utf-8")
        backend = StubBrowserBackend({})
        report = self._verify(sandbox_mode="docker", browser_backend=backend)
        self.assertEqual([c.status for c in report.criteria], [CriterionStatus.BLOCKED] * 3)
        self.assertIn("False browser proof", report.criteria[0].failure_reason)
        self.assertEqual(backend.calls, [])
        self.assertEqual(report.exit_code, 2)


if __name__ == "__main__":
    unittest.main()
