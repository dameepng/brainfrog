"""Phase 1D: real isolated browser verification (Docker + Playwright/Chromium).

Every test here drives real containers: a python:3.12-slim application container serving a
fixture from the materialized snapshot, and the approved Playwright/Chromium browser image,
joined only by a BrainFrog-owned internal network. Nothing is mocked. Tests are SKIPPED (not
passed) when Docker or either approved image is unavailable; no test pulls images, and no test
touches resources it did not create.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import MappingProxyType

from core.runtime.app_profile import APPROVED_PROFILES, AppProfile
from core.runtime.browser_plan import parse_browser_plan
from core.runtime.browser_sandbox import (
    APPROVED_BROWSER_IMAGES,
    BrowserLimits,
    CRITERION_LABEL,
    PHASE_LABEL,
    ROLE_LABEL,
    DockerBrowserSandbox,
)
from core.runtime.sandbox import DockerSandboxBackend, InfraErrorCategory, materialize_source_snapshot_with_manifest
from core.verifier import CriterionStatus, OverallVerdict, StandaloneVerifier, verify_report_integrity
from tests.browser_verification_helpers import FIXTURES, browser_criterion, copy_fixture, spec_document


def _docker(*args: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def _browser_ready() -> bool:
    try:
        sb = DockerBrowserSandbox()
        ok, _ = sb.is_available()
        return ok and DockerSandboxBackend().get_image_digest() is not None
    except Exception:
        return False


BROWSER_READY = _browser_ready()
SKIP_REASON = "Docker engine, python:3.12-slim, or the approved browser image is not available locally"


def _owned_phase1d() -> dict:
    flt = f"label={PHASE_LABEL}=1d"
    return {"containers": _docker("ps", "-a", "-q", "--filter", flt).stdout.split(),
            "networks": _docker("network", "ls", "-q", "--filter", flt).stdout.split()}


def _unrelated_inventory() -> dict:
    """IDs of every container/network that is not BrainFrog-owned (by any BrainFrog test run)."""
    fmt = "{{.ID}} {{.Label \"io.brainfrog.owner\"}}"
    cons = _docker("ps", "-a", "--no-trunc", "--format", fmt).stdout.split("\n")
    nets = _docker("network", "ls", "--no-trunc", "--format", fmt).stdout.split("\n")
    return {"containers": sorted(l.split()[0] for l in cons if l.strip() and len(l.split()) == 1),
            "networks": sorted(l.split()[0] for l in nets if l.strip() and len(l.split()) == 1)}


def _plan(path, steps, viewport=None):
    doc = {"path": path, "steps": steps}
    if viewport:
        doc["viewport"] = viewport
    return json.dumps(doc)


AC_FORM_VISIBLE = browser_criterion("AC-01", _plan("/login", [
    {"action": "expect_visible", "selector": "#login-form"},
    {"action": "expect_visible", "selector": "#email"},
]), "Login form visible")
AC_VALID_SUBMIT = browser_criterion("AC-02", _plan("/login", [
    {"action": "fill", "selector": "#email", "value": "demo.user@example.test"},
    {"action": "fill", "selector": "#password", "value": "not-a-secret"},
    {"action": "click", "selector": "#submit"},
    {"action": "expect_url_path", "path": "/dashboard"},
    {"action": "expect_text", "selector": "#welcome-message", "contains": "Welcome"},
]), "Valid form submission")
AC_INVALID_EMAIL = browser_criterion("AC-03", _plan("/login", [
    {"action": "fill", "selector": "#email", "value": "not-an-email"},
    {"action": "click", "selector": "#submit"},
    {"action": "expect_text", "selector": "#email-error", "contains": "Invalid email", "timeout_ms": 3000},
]), "Invalid email validation")
AC_MOBILE_OVERFLOW = browser_criterion("AC-04", _plan("/login", [
    {"action": "expect_visible", "selector": "#login-form"},
    {"action": "expect_no_horizontal_overflow", "timeout_ms": 1500},
], viewport={"width": 375, "height": 667}), "Mobile horizontal overflow")


@unittest.skipUnless(BROWSER_READY, SKIP_REASON)
class TestRealBrowserVerification(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.unrelated_before = _unrelated_inventory()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.unrelated_after = _unrelated_inventory()

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bf_bint_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.assertEqual(_owned_phase1d(), {"containers": [], "networks": []}, "owned resources leaked")

    def verify(self, fixture, criteria, backend=None, name=None):
        repo = copy_fixture(fixture, self.tmp / (name or fixture))
        (repo / "spec.md").write_text(spec_document(*criteria), encoding="utf-8")
        verifier = StandaloneVerifier(repo_dir=repo, sandbox_mode="docker", browser_backend=backend,
                                      reports_base_dir=self.tmp / f"reports_{name or fixture}")
        report = verifier.verify_spec(repo / "spec.md")
        return report, self.tmp / f"reports_{name or fixture}" / report.run_id

    @staticmethod
    def manifest(rdir, crit):
        return json.loads((rdir / crit.evidence_files["browser-assertions.json"]).read_text(encoding="utf-8"))

    def test_01_real_navigation_typing_clicking_and_dom_assertions_pass(self) -> None:
        seen = {}

        def inspect_live(app_id):  # while the app runs: no host ports, private internal network only
            info = json.loads(_docker("inspect", app_id).stdout)[0]
            seen["ports"] = info["NetworkSettings"]["Ports"]
            seen["bindings"] = info["HostConfig"]["PortBindings"]
            seen["docker_port"] = _docker("port", app_id).stdout.strip()
            net = info["HostConfig"]["NetworkMode"]
            seen["network"] = json.loads(_docker("network", "inspect", net).stdout)[0]

        report, rdir = self.verify("correct", [AC_FORM_VISIBLE, AC_VALID_SUBMIT],
                                   backend=DockerBrowserSandbox(before_browser_start=inspect_live))
        self.assertEqual([c.status for c in report.criteria], [CriterionStatus.PASS] * 2,
                         [c.failure_reason for c in report.criteria])
        self.assertEqual(report.overall_verdict, OverallVerdict.PASS)
        submit = report.criteria[1]
        m = self.manifest(rdir, submit)
        self.assertEqual([(r["action"], r["kind"], r["status"]) for r in m["records"]], [
            ("fill", "action", "PASS"), ("fill", "action", "PASS"), ("click", "action", "PASS"),
            ("expect_url_path", "assertion", "PASS"), ("expect_text", "assertion", "PASS")])
        self.assertEqual(m["records"][3]["observed"]["path"], "/dashboard")
        self.assertIn("Welcome", m["records"][4]["observed"]["text"])
        self.assertEqual(m["records"][0]["observed"], {"filled": True, "value_length": 22})  # value never recorded
        png = (rdir / submit.evidence_files["screenshot.png"]).read_bytes()
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(m["screenshot"]["page_path"], "/dashboard")
        rt = submit.browser_evidence["browser_runtime"]
        self.assertEqual(rt["playwright_version"], "1.64.0")
        self.assertTrue(rt["browser_version"].startswith("156."))
        self.assertEqual(rt["container_uid"], 65532)
        # Chromium's own renderer sandbox is ON and was proven from /proc during the run.
        self.assertTrue(rt["chromium_internal_sandbox"])
        sandbox = m["runtime"]["renderer_sandbox"]
        self.assertTrue(sandbox["verified"], sandbox)
        self.assertTrue(m["runtime"]["renderer_sandbox_at_end"]["verified"])
        for renderer in sandbox["renderers"]:
            self.assertTrue(all(renderer["checks"].values()), renderer)
            self.assertGreater(renderer["seccomp_filters"], sandbox["browser_side_seccomp_filters"])
            self.assertNotEqual(renderer["root"], "/")
        cfg = submit.effective_container_config
        for role in ("app", "browser"):
            self.assertEqual(cfg[role]["user"], "65532:65532")
            self.assertTrue(cfg[role]["readonly_rootfs"])
            self.assertEqual(cfg[role]["cap_drop"], ["ALL"])
            self.assertFalse(cfg[role]["privileged"])
            self.assertIn("no-new-privileges", cfg[role]["security_opt"])
            self.assertTrue(cfg[role]["network_mode"].startswith("bf1d_net_"))
            self.assertTrue(all(m_["rw"] is False for m_ in cfg[role]["mounts"]))
        self.assertTrue(any(o.startswith("seccomp=custom-profile-canonical-sha256:") for o in cfg["browser"]["security_opt"]))
        self.assertFalse(any(o.startswith("seccomp") for o in cfg["app"]["security_opt"]))
        self.assertEqual(cfg["browser"]["memory_bytes"], 1536 * 1024 * 1024)
        self.assertEqual(cfg["browser"]["pids_limit"], 256)
        self.assertEqual(cfg["app"]["network_mode"], cfg["browser"]["network_mode"])
        self.assertEqual(seen["ports"], {})
        self.assertFalse(seen["bindings"])
        self.assertEqual(seen["docker_port"], "")
        self.assertTrue(seen["network"]["Internal"])
        self.assertEqual(len(seen["network"]["Containers"]), 1)  # only the app before the browser starts
        self.assertEqual(verify_report_integrity(rdir), (True, []))

        # Scenario H: tamper with a copied screenshot after finalization.
        copied = self.tmp / "tampered_copy"
        shutil.copytree(rdir, copied)
        shot = copied / submit.evidence_files["screenshot.png"]
        shot.write_bytes(shot.read_bytes()[:-1] + b"\x00")
        ok, findings = verify_report_integrity(copied)
        self.assertFalse(ok)
        self.assertTrue(any("screenshot.png" in f for f in findings), findings)
        self.assertEqual(verify_report_integrity(rdir), (True, []))

    def test_02_source_binding_correct_versus_broken_fixture(self) -> None:
        good, gdir = self.verify("correct", [AC_INVALID_EMAIL, AC_MOBILE_OVERFLOW])
        bad, bdir = self.verify("broken", [AC_INVALID_EMAIL, AC_MOBILE_OVERFLOW])
        self.assertEqual([c.status for c in good.criteria], [CriterionStatus.PASS] * 2,
                         [c.failure_reason for c in good.criteria])
        self.assertEqual([c.status for c in bad.criteria], [CriterionStatus.FAIL] * 2,
                         [c.failure_reason for c in bad.criteria])
        self.assertEqual((good.exit_code, bad.exit_code), (0, 1))
        self.assertNotEqual(good.materialized_source_snapshot_digest, bad.materialized_source_snapshot_digest)
        for report, rdir in ((good, gdir), (bad, bdir)):
            for crit in report.criteria:
                m = self.manifest(rdir, crit)
                self.assertEqual(m["source_snapshot_digest"], report.materialized_source_snapshot_digest)
                self.assertEqual(crit.observed_source_snapshot_digest, report.materialized_source_snapshot_digest)
                self.assertTrue(m["screenshot"]["captured"], "FAIL outcomes must carry a real screenshot")
            self.assertEqual(verify_report_integrity(rdir), (True, []))
        invalid_email = self.manifest(bdir, bad.criteria[0])["records"][-1]
        self.assertEqual(invalid_email["observed"]["visible"], False)  # error text exists in DOM but is hidden
        overflow = self.manifest(bdir, bad.criteria[1])["records"][-1]["observed"]
        self.assertGreater(overflow["content_width_css_px"], overflow["viewport_client_width_css_px"])
        self.assertTrue(self.manifest(bdir, bad.criteria[1])["screenshot"]["full_page"])

    def test_03_hostile_navigation_and_requests_are_blocked(self) -> None:
        report, rdir = self.verify("hostile", [
            browser_criterion("AC-01", _plan("/escape", [{"action": "expect_visible", "selector": "#marker"}])),
            browser_criterion("AC-02", _plan("/subresources", [{"action": "expect_visible", "selector": "#marker"}])),
        ])
        escape, sub = report.criteria
        self.assertEqual(escape.status, CriterionStatus.BLOCKED)
        self.assertEqual(escape.browser_evidence["failure_kind"], "unapproved_navigation")
        self.assertIn("example.com", escape.failure_reason)
        m = self.manifest(rdir, escape)
        blocked = {b["url"] for b in m["blocked_requests"]}
        self.assertTrue({"http://example.com/beacon", "http://169.254.169.254/latest/meta-data/",
                         "http://host.docker.internal:2375/version", "http://example.com/stolen"} <= blocked, blocked)
        self.assertEqual(sub.status, CriterionStatus.PASS, sub.failure_reason)
        self.assertGreaterEqual(self.manifest(rdir, sub)["summary"]["blocked_requests"], 3)
        network_log = (rdir / sub.evidence_files["browser-network.log"]).read_text(encoding="utf-8")
        self.assertIn('"approved_origin": false', network_log)
        app_log = (rdir / sub.evidence_files["app-server.log"]).read_text(encoding="utf-8")
        self.assertNotIn("169.254", app_log)
        self.assertEqual(report.overall_verdict, OverallVerdict.BLOCKED)

    def test_03b_network_red_team_pages(self) -> None:
        crits = [
            browser_criterion("AC-R1", _plan("/redirect", [{"action": "expect_visible", "selector": "#marker", "timeout_ms": 3000}])),
            browser_criterion("AC-R2", _plan("/file-nav", [{"action": "expect_visible", "selector": "#marker"},
                                                           {"action": "expect_url_path", "path": "/file-nav"}])),
            browser_criterion("AC-R3", _plan("/websocket", [{"action": "expect_text", "selector": "#ws-state",
                                                             "equals": "failed", "timeout_ms": 8000}])),
            browser_criterion("AC-R4", _plan("/iframe", [{"action": "expect_visible", "selector": "#marker"}])),
            browser_criterion("AC-R5", _plan("/popup", [{"action": "click", "selector": "#open"},
                                                        {"action": "expect_visible", "selector": "#marker"}])),
        ]
        report, rdir = self.verify("hostile", crits, name="redteam")
        by_id = {c.criterion_id: c for c in report.criteria}
        # Redirect to the metadata endpoint, off-origin iframe and popup: navigation attempts -> BLOCKED.
        for cid, target in (("AC-R1", "169.254.169.254"), ("AC-R4", "example.com/embedded"), ("AC-R5", "example.com/popup")):
            crit = by_id[cid]
            self.assertEqual(crit.status, CriterionStatus.BLOCKED, (cid, crit.failure_reason))
            self.assertEqual(crit.browser_evidence["failure_kind"], "unapproved_navigation")
            self.assertIn(target, crit.failure_reason)
        # file:// is refused by Chromium itself: the page never leaves the approved origin.
        file_nav = by_id["AC-R2"]
        self.assertEqual(file_nav.status, CriterionStatus.PASS, file_nav.failure_reason)
        self.assertEqual(self.manifest(rdir, file_nav)["records"][-1]["observed"]["path"], "/file-nav")
        # The WebSocket to an unapproved host never connects and is recorded as unapproved.
        ws = by_id["AC-R3"]
        self.assertEqual(ws.status, CriterionStatus.PASS, ws.failure_reason)
        self.assertEqual(self.manifest(rdir, ws)["summary"]["unapproved_requests_observed"], 1)
        app_log = (rdir / ws.evidence_files["app-server.log"]).read_text(encoding="utf-8")
        self.assertNotIn("example.com", app_log)

    def test_04_os_level_network_isolation_of_browser_container(self) -> None:
        """Docker-level boundary, independent of Playwright interception."""
        sb = DockerBrowserSandbox()
        img = sb.resolve_browser_image()["image_id"]
        app_img = sb.resolve_app_image(APPROVED_PROFILES["python-static"])["image_id"]
        run_label, names = "bfnetprobe.AC-NET.t", {"net": "bf1d_net_probe", "app": "bf1d_app_probe", "brw": "bf1d_brw_probe"}
        snap, har, run, inp = (self.tmp / d for d in ("snap", "har", "run", "inp"))
        for d in (snap, har, run, inp):
            d.mkdir()
        (snap / "index.html").write_text("<p>ok</p>", encoding="utf-8")
        (har / "app_launcher.py").write_bytes((Path(__file__).resolve().parents[1] / "core/runtime/app_launcher.py").read_bytes())
        (har / "nonce").write_text("n" * 64, encoding="ascii")
        (run / "probe.py").write_text(PROBE, encoding="utf-8")
        try:
            self.assertEqual(_docker(*sb.build_network_create_args(names["net"], run_label, "AC-NET")).returncode, 0)
            self.assertEqual(_docker(*sb.build_app_create_args(names["app"], names["net"], run_label, "AC-NET", snap, har,
                                                              app_img, APPROVED_PROFILES["python-static"])).returncode, 0)
            self.assertEqual(_docker("start", names["app"]).returncode, 0)
            gateway = json.loads(_docker("network", "inspect", names["net"]).stdout)[0]["IPAM"]["Config"][0]["Gateway"]
            args = sb.build_browser_create_args(names["brw"], names["net"], run_label, "AC-NET", run, inp, img,
                                                "http://bf-app:8080", "/", "0" * 64)
            args = args[:args.index(img) + 1] + ["python3", "-I", "/bf-harness/probe.py", gateway]
            args.remove("--interactive")
            self.assertEqual(_docker(*args).returncode, 0)
            out = _docker("start", "--attach", names["brw"], timeout=120).stdout
            res = json.loads(out.strip().splitlines()[-1])
        finally:
            for n in (names["brw"], names["app"]):
                sb._c.remove_owned_container(n, run_label)
            sb.remove_owned_network(names["net"], run_label)
        self.assertEqual(res["app"], "CONNECTED")
        self.assertEqual(res["uid"], 65532)
        for target in ("1.1.1.1:80", "8.8.8.8:53", "169.254.169.254:80", "192.168.65.254:80", "172.17.0.1:80",
                       f"{gateway}:80", f"{gateway}:2375", "host.docker.internal:80"):
            self.assertNotEqual(res[target], "CONNECTED", target)
        for name in ("example.com", "host.docker.internal", "gateway.docker.internal"):
            self.assertTrue(res["dns:" + name].startswith("unresolved"), res["dns:" + name])

    def test_05_app_unavailable_and_startup_failure_are_blocked(self) -> None:
        stop = lambda app_id: _docker("kill", app_id)  # noqa: E731 - kills only the owned app container
        report, _ = self.verify("correct", [AC_FORM_VISIBLE], backend=DockerBrowserSandbox(before_browser_start=stop),
                                name="killed")
        crit = report.criteria[0]
        self.assertEqual(crit.status, CriterionStatus.BLOCKED)
        self.assertEqual(crit.browser_evidence["failure_kind"], "app_unreachable")
        self.assertIsNone(crit.browser_evidence["screenshot"]["screenshot_sha256"])  # no page, no fabricated image

        failing = AppProfile(profile_id="python-static", approved_container_image="python:3.12-slim",
                             approved_startup_argv=("python", "-I", "-c", "import sys; sys.exit(3)"),
                             startup_timeout_seconds=10.0)
        backend = DockerBrowserSandbox(profiles=MappingProxyType({"python-static": failing}))
        report, _ = self.verify("correct", [AC_FORM_VISIBLE], backend=backend, name="nostart")
        crit = report.criteria[0]
        self.assertEqual(crit.status, CriterionStatus.BLOCKED)
        self.assertEqual(crit.browser_evidence["failure_kind"], "app_startup_failed")
        self.assertIn("exited", crit.failure_reason)
        self.assertEqual(report.exit_code, 2)

    def test_06_browser_unavailable_is_blocked(self) -> None:
        missing = MappingProxyType({"brainfrog-browser-runner:missing-test-tag": {"image_id": "sha256:" + "0" * 64}})
        report, _ = self.verify("correct", [AC_FORM_VISIBLE], name="noimage", backend=DockerBrowserSandbox(
            browser_image="brainfrog-browser-runner:missing-test-tag", approved_browser_images=missing))
        self.assertEqual(report.criteria[0].status, CriterionStatus.BLOCKED)
        self.assertEqual(report.criteria[0].execution_backend, "unavailable_sandbox")
        self.assertIn("not available locally", report.criteria[0].failure_reason)

        py_id = DockerSandboxBackend().resolve_image()["image_id"]
        no_playwright = MappingProxyType({"python:3.12-slim": {"image_id": py_id, "playwright_version": "1.49.1"}})
        report, _ = self.verify("correct", [AC_FORM_VISIBLE], name="noplaywright", backend=DockerBrowserSandbox(
            browser_image="python:3.12-slim", approved_browser_images=no_playwright))
        crit = report.criteria[0]
        self.assertEqual(crit.status, CriterionStatus.BLOCKED)
        self.assertEqual(crit.browser_evidence["failure_kind"], "browser_unavailable", crit.failure_reason)

    def test_07_false_browser_proof_never_runs_or_passes(self) -> None:
        before = _owned_phase1d()
        repo = copy_fixture("correct", self.tmp / "falseproof")
        shutil.copy2(FIXTURES / "spec_false_proof.md", repo / "spec.md")
        report = StandaloneVerifier(repo_dir=repo, sandbox_mode="docker",
                                    reports_base_dir=self.tmp / "r_fp").verify_spec(repo / "spec.md")
        self.assertEqual([c.status for c in report.criteria], [CriterionStatus.BLOCKED] * 4)
        self.assertEqual(report.overall_verdict, OverallVerdict.BLOCKED)
        self.assertTrue(all(c.execution_backend is None for c in report.criteria))
        self.assertEqual(before, _owned_phase1d())

    def test_08_timeout_is_blocked_and_cleaned_up(self) -> None:
        slow = browser_criterion("AC-01", _plan("/login", [
            {"action": "expect_visible", "selector": "#never-rendered", "timeout_ms": 15000}]))
        report, _ = self.verify("correct", [slow], backend=DockerBrowserSandbox(browser_timeout_override_s=8))
        crit = report.criteria[0]
        self.assertEqual(crit.status, CriterionStatus.BLOCKED)
        self.assertEqual(crit.infrastructure_error_category, InfraErrorCategory.TIMEOUT)
        self.assertLess(crit.duration_ms, 60000)

    def test_09_browser_killed_mid_run_is_blocked(self) -> None:
        def kill_later(_app_id):
            def _kill():
                time.sleep(6)
                for cid in _docker("ps", "-q", "--filter", f"label={ROLE_LABEL}=browser",
                                   "--filter", f"label={CRITERION_LABEL}=AC-KILL").stdout.split():
                    _docker("kill", cid)
            threading.Thread(target=_kill, daemon=True).start()

        crit_md = browser_criterion("AC-KILL", _plan("/login", [
            {"action": "expect_visible", "selector": "#never-rendered", "timeout_ms": 15000}]))
        report, _ = self.verify("correct", [crit_md], backend=DockerBrowserSandbox(before_browser_start=kill_later))
        crit = report.criteria[0]
        self.assertEqual(crit.status, CriterionStatus.BLOCKED)
        self.assertIn(crit.browser_evidence["failure_kind"], ("browser_crashed", "evidence_export_failed"))

    def test_10_ctrl_c_cancellation_cleans_up_owned_resources(self) -> None:
        def interrupt(_app_id):
            raise KeyboardInterrupt

        plan = parse_browser_plan(_plan("/login", [{"action": "expect_visible", "selector": "#login-form"}]))
        repo = copy_fixture("correct", self.tmp / "cancel")
        manifest = materialize_source_snapshot_with_manifest(repo, self.tmp / "cancel_snap")
        with self.assertRaises(KeyboardInterrupt):
            DockerBrowserSandbox(before_browser_start=interrupt).execute_browser_criterion(
                "AC-01", plan, manifest.snapshot_dir, manifest.digest, run_id="cancel")
        # tearDown asserts that no Phase 1D resources remain.

    def test_11_renderer_memory_exhaustion_is_blocked(self) -> None:
        bomb = browser_criterion("AC-01", _plan("/memory", [
            {"action": "expect_visible", "selector": "#marker"},
            {"action": "expect_visible", "selector": "#never-rendered", "timeout_ms": 15000}]))
        backend = DockerBrowserSandbox(limits=BrowserLimits(memory_mb=768), browser_timeout_override_s=120)
        report, _ = self.verify("hostile", [bomb], backend=backend)
        crit = report.criteria[0]
        self.assertEqual(crit.status, CriterionStatus.BLOCKED, crit.failure_reason)

    def test_99_unrelated_docker_resources_untouched(self) -> None:
        self.assertEqual(_unrelated_inventory(), self.unrelated_before)


PROBE = r'''
import json, os, socket, sys
gw = sys.argv[1]
res = {"uid": os.getuid()}
def tcp(h, p):
    try:
        socket.create_connection((h, p), timeout=3).close()
        return "CONNECTED"
    except Exception as e:
        return "blocked: " + type(e).__name__
res["app"] = tcp("bf-app", 8080)
for h, p in (("1.1.1.1", 80), ("8.8.8.8", 53), ("169.254.169.254", 80), ("192.168.65.254", 80),
             ("172.17.0.1", 80), (gw, 80), (gw, 2375), ("host.docker.internal", 80)):
    res[f"{h}:{p}"] = tcp(h, p)
for n in ("example.com", "host.docker.internal", "gateway.docker.internal"):
    try:
        res["dns:" + n] = socket.gethostbyname(n)
    except Exception as e:
        res["dns:" + n] = "unresolved: " + type(e).__name__
print(json.dumps(res))
'''


if __name__ == "__main__":
    unittest.main()
