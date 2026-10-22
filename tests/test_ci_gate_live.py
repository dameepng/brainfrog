"""Phase 1E: the CI gate end to end with real containers (local CI simulation, Cases A-I).

Real Docker, the approved python:3.12-slim image (pinned registry digest) and the approved browser runner
(real Chromium with its renderer sandbox). The GitHub event and environment are synthetic: this is a local
simulation of the gate's logic, NOT evidence of a GitHub-hosted run.

Skipped when Docker or an approved image is unavailable. The gate refuses to publish evidence containing
user-profile paths, so on hosts whose temp dir is inside a user profile set BRAINFROG_CI_SIM_DIR to a
neutral directory (every run creates and removes its own subdirectory there).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from core import ci_gate
from core.ci_gate import EVIDENCE_DIRNAME, enforce, load_policy, run_gate, verify_evidence_bundle
from core.verifier import verify_report_integrity
from tests.ci_gate_helpers import (
    REPO_ROOT,
    TRUSTED_FILES,
    copy_repo_files,
    load_policy_dict,
    make_pr_repos,
)

FULL_SPEC = (REPO_ROOT / ".github/brainfrog/specs/fixture-login-app.md").read_text(encoding="utf-8")
SPEC_REL = ".github/brainfrog/specs/fixture-login-app.md"
FIXTURE = "tests/fixtures/browser_app/correct"


def _section(cid: str) -> str:
    return "### " + cid + FULL_SPEC.split("### " + cid, 1)[1].split("\n### ", 1)[0].rstrip() + "\n"


def _rmtree(path: Path) -> None:
    """Remove a test-owned tree, including git's read-only object files on Windows."""
    def _onerror(func, p, _exc):
        os.chmod(p, 0o700)
        func(p)
    shutil.rmtree(path, onerror=_onerror)


def reduced_spec(*cids: str) -> str:
    head = FULL_SPEC.split("## Acceptance Criteria", 1)[0]
    return head + "## Acceptance Criteria\n\n" + "\n".join(_section(c) for c in cids)


def _sim_base() -> Path | None:
    base = os.environ.get("BRAINFROG_CI_SIM_DIR")
    if base:
        return Path(base)
    tmp = Path(tempfile.gettempdir()).resolve()
    return None if ci_gate._PRIVATE_PATH_RE.search(str(tmp)) else tmp


def _images_ready() -> bool:
    try:
        policy = load_policy_dict()
        py = subprocess.run(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}",
                             policy["python_image"]["tag"]], capture_output=True, text=True, timeout=30)
        br = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", policy["browser_image"]["tag"]],
                            capture_output=True, text=True, timeout=30)
        return (py.returncode == 0 and policy["python_image"]["repo_digest"] in json.loads(py.stdout)
                and br.returncode == 0)
    except Exception:
        return False


READY = _images_ready() and _sim_base() is not None


@unittest.skipUnless(READY, "Docker, an approved image, or a neutral BRAINFROG_CI_SIM_DIR is unavailable")
class TestGateLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        base = _sim_base()
        base.mkdir(parents=True, exist_ok=True)
        cls.base = Path(tempfile.mkdtemp(prefix="bf_cilive_", dir=base)).resolve()
        cls._old_tempdir = tempfile.tempdir
        tempfile.tempdir = str(cls.base / "tmp")  # snapshot + harness dirs land here, not in a user profile
        (cls.base / "tmp").mkdir()

    @classmethod
    def tearDownClass(cls) -> None:
        tempfile.tempdir = cls._old_tempdir
        _rmtree(cls.base)
        owned = subprocess.run(["docker", "ps", "-a", "-q", "--filter", "label=io.brainfrog.owner=brainfrog-verifier"],
                               capture_output=True, text=True).stdout.split()
        assert not owned, f"owned containers leaked: {owned}"

    def make_case(self, name, spec_text, required, fixture="correct", mutate_candidate=None):
        root = self.base / name
        policy = load_policy_dict()
        policy["targets"][0]["required_criteria"] = list(required)

        def setup_base(repo):  # the trusted side: policy, spec, recipe and workflow on the base branch
            copy_repo_files(repo, TRUSTED_FILES)
            (repo / SPEC_REL).write_text(spec_text, encoding="utf-8")
            (repo / ".github/brainfrog/policy.json").write_text(json.dumps(policy, indent=2), encoding="utf-8")

        def setup_pr(repo):  # the PR: the fixture app (and any candidate mutation) on top
            shutil.copytree(REPO_ROOT / "tests/fixtures/browser_app" / fixture, repo / FIXTURE)
            if mutate_candidate:
                mutate_candidate(repo)

        repos = make_pr_repos(root, setup_base, setup_pr)
        return root, repos.trusted, repos.candidate, repos.base_sha, repos.tested_sha, repos

    def gate(self, root, trusted, candidate, base_sha, tested_sha, repos, github_sha=None):
        out = root / "out"
        decision = run_gate(trusted, candidate, out, repos.event(), repos.env(github_sha), base_root=repos.base)
        bundle = out / EVIDENCE_DIRNAME
        meta = json.loads((bundle / "ci-metadata.json").read_text(encoding="utf-8"))
        return decision, out, bundle, meta

    @staticmethod
    def criteria(meta):
        return {c["criterion_id"]: c["status"] for t in meta["targets"] for c in t["criterion_results"]}

    @staticmethod
    def report_dirs(bundle):
        return sorted((bundle / "reports").glob("*/*"))

    # -- Case A ----------------------------------------------------------------------------------
    def test_case_a_valid_source_passes_with_bound_evidence_and_g_tamper_is_caught(self) -> None:
        case = self.make_case("a", FULL_SPEC, ["CI-01", "CI-02", "CI-03", "CI-04", "CI-05", "CI-06"])
        root, trusted, candidate, base_sha, tested_sha, _ = case
        decision, out, bundle, meta = self.gate(*case)
        self.assertEqual(decision["outcome"], "success", decision["reasons"])
        self.assertEqual(set(self.criteria(meta).values()), {"PASS"})
        self.assertEqual(len(self.criteria(meta)), 6)
        self.assertEqual(enforce(out), 0)
        # Identity and provenance fields
        self.assertEqual((meta["tested_sha"], meta["trusted_verifier_revision"], meta["base_sha"]),
                         (tested_sha, base_sha, base_sha))
        self.assertEqual(meta["trusted_policy_digest"],
                         ci_gate.hashlib.sha256((trusted / ".github/brainfrog/policy.json").read_bytes()).hexdigest())
        self.assertIn(meta["python_image_identity"]["required_repo_digest"], meta["python_image_identity"]["repo_digests"])
        self.assertEqual(meta["seccomp_profile_digest"], ci_gate.BROWSER_SECCOMP_PROFILE_SHA256)
        self.assertRegex(meta["targets"][0]["source_snapshot_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(meta["provenance"]["kind"], "unsigned-ci-context")
        # Published report is independently checkable; browser evidence proves the sandboxed run.
        (report,) = self.report_dirs(bundle)
        self.assertEqual(verify_report_integrity(report), (True, []))
        rep = json.loads((report / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(rep["spec_path"], SPEC_REL)
        browser = [c for c in rep["criteria"] if c["verification_type"] == "browser"]
        self.assertEqual(len(browser), 4)
        for crit in browser:
            run = json.loads((report / crit["evidence_files"]["browser-run.json"]).read_text(encoding="utf-8"))
            self.assertTrue(run["browser_runtime"]["chromium_internal_sandbox"])
            runtime = json.loads((report / crit["evidence_files"]["browser-assertions.json"])
                                 .read_text(encoding="utf-8"))["runtime"]
            self.assertTrue(runtime["renderer_sandbox"]["verified"])  # /proc proof at start ...
            self.assertTrue(runtime["renderer_sandbox_at_end"]["verified"])  # ... and at the end
            self.assertIn("screenshot.png", crit["evidence_files"])
        self.assertIsNone(ci_gate._PRIVATE_PATH_RE.search((report / "report.json").read_text(encoding="utf-8")))
        # Case G: any post-finalization edit to published evidence makes enforcement non-success.
        shot = next(bundle.rglob("screenshot.png"))
        shot.write_bytes(shot.read_bytes() + b"\0")
        self.assertEqual(enforce(out), 2)
        _, findings = verify_evidence_bundle(bundle)
        self.assertTrue(any("modified" in f for f in findings))

    # -- Case B ----------------------------------------------------------------------------------
    def test_case_b_failing_unit_test_is_failure_with_evidence(self) -> None:
        def break_test(candidate):
            test = candidate / FIXTURE / "tests/test_validation.py"
            test.write_text(test.read_text(encoding="utf-8").replace(
                "class ", "class TestDeliberateFailure(__import__('unittest').TestCase):\n"
                          "    def test_deliberately_failing_assertion(self):\n"
                          "        self.assertEqual(1, 2)\n\n\nclass ", 1), encoding="utf-8")

        case = self.make_case("b", reduced_spec("CI-01", "CI-02"), ["CI-01", "CI-02"], mutate_candidate=break_test)
        decision, out, bundle, meta = self.gate(*case)
        self.assertEqual(decision["outcome"], "failure", decision["reasons"])
        self.assertEqual(self.criteria(meta), {"CI-01": "FAIL", "CI-02": "PASS"})
        self.assertEqual(enforce(out), 1)
        (report,) = self.report_dirs(bundle)
        self.assertTrue(verify_report_integrity(report)[0])
        self.assertTrue(list((report / "evidence" / "CI-01").glob("*")), "failed runs still publish evidence")

    # -- Case C ----------------------------------------------------------------------------------
    def test_case_c_broken_browser_behaviour_is_failure_with_screenshot(self) -> None:
        case = self.make_case("c", reduced_spec("CI-05"), ["CI-05"], fixture="broken")
        decision, out, bundle, meta = self.gate(*case)
        self.assertEqual(decision["outcome"], "failure", decision["reasons"])
        self.assertEqual(self.criteria(meta), {"CI-05": "FAIL"})
        self.assertEqual(enforce(out), 1)
        (report,) = self.report_dirs(bundle)
        rep = json.loads((report / "report.json").read_text(encoding="utf-8"))
        files = rep["criteria"][0]["evidence_files"]
        self.assertIn("screenshot.png", files)
        self.assertTrue((report / files["screenshot.png"]).is_file())

    # -- Case D ----------------------------------------------------------------------------------
    def test_case_d_zero_tests_is_never_success(self) -> None:
        def empty_tests(candidate):
            for p in (candidate / FIXTURE / "tests").glob("*.py"):
                p.unlink()
            (candidate / FIXTURE / "tests" / "README.md").write_text("no tests\n", encoding="utf-8")

        case = self.make_case("d", reduced_spec("CI-01"), ["CI-01"], mutate_candidate=empty_tests)
        decision, out, bundle, meta = self.gate(*case)
        self.assertEqual(decision["outcome"], "blocked", decision["reasons"])
        self.assertIn(self.criteria(meta)["CI-01"], ("BLOCKED", "NOT_RUN"))
        self.assertEqual(enforce(out), 2)

    # -- Case F ----------------------------------------------------------------------------------
    def test_case_f_source_revision_mismatch_is_blocked_before_execution(self) -> None:
        case = self.make_case("f", reduced_spec("CI-01"), ["CI-01"])
        decision, out, bundle, meta = self.gate(*case, github_sha="6" * 40)
        self.assertEqual(decision["outcome"], "blocked")
        self.assertTrue(any("candidate checkout is at" in r for r in decision["reasons"]))
        self.assertEqual(meta["github_sha"], "6" * 40)
        self.assertEqual(meta["tested_sha"], case[4])
        self.assertFalse((out / "work" / "reports").exists(), "nothing executes on an identity mismatch")
        self.assertEqual(enforce(out), 2)

    # -- Case H ----------------------------------------------------------------------------------
    def test_case_h_candidate_cannot_weaken_trusted_criteria(self) -> None:
        def weaken(candidate):
            (candidate / SPEC_REL).write_text(reduced_spec("CI-02"), encoding="utf-8")  # drops the unit tests
            pol = json.loads((candidate / ".github/brainfrog/policy.json").read_text(encoding="utf-8"))
            pol["targets"][0]["required_criteria"] = ["CI-02"]
            (candidate / ".github/brainfrog/policy.json").write_text(json.dumps(pol), encoding="utf-8")

        case = self.make_case("h", reduced_spec("CI-01", "CI-02"), ["CI-01", "CI-02"], mutate_candidate=weaken)
        decision, out, bundle, meta = self.gate(*case)
        self.assertEqual(set(self.criteria(meta)), {"CI-01", "CI-02"}, "the trusted spec was evaluated in full")
        self.assertEqual(decision["outcome"], "blocked")
        self.assertIn(SPEC_REL, meta["candidate_trusted_config_changes"])
        self.assertEqual(enforce(out), 2)

    # -- Case I ----------------------------------------------------------------------------------
    def test_case_i_workflow_modification_cannot_certify_itself(self) -> None:
        def edit_workflow(candidate):
            wf = candidate / ".github/workflows/brainfrog-verify.yml"
            wf.write_text(wf.read_text(encoding="utf-8").replace(
                "        if: always()\n        run: |\n          \"$RUNNER_TEMP/bf-venv/bin/python\" -I trusted/scripts/"
                "brainfrog_ci.py enforce", "        if: always()\n        run: |\n          exit 0 # enforce", 1),
                encoding="utf-8")

        case = self.make_case("i", reduced_spec("CI-02"), ["CI-02"], mutate_candidate=edit_workflow)
        decision, out, bundle, meta = self.gate(*case)
        self.assertEqual(self.criteria(meta), {"CI-02": "PASS"}, "evidence is still produced")
        self.assertEqual(decision["outcome"], "blocked")
        self.assertIn(".github/workflows/brainfrog-verify.yml", meta["candidate_trusted_config_changes"])
        self.assertEqual(enforce(out), 2)

    # -- Unsupported application profile ------------------------------------------------------------
    def test_unsupported_app_profile_is_blocked(self) -> None:
        spec = reduced_spec("CI-03").replace("**App Profile:** python-static", "**App Profile:** node-dev-server")
        case = self.make_case("profile", spec, ["CI-03"])
        decision, out, bundle, meta = self.gate(*case)
        self.assertEqual(self.criteria(meta), {"CI-03": "BLOCKED"})
        self.assertEqual(decision["outcome"], "blocked")
        self.assertEqual(enforce(out), 2)


if __name__ == "__main__":
    unittest.main()
