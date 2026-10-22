"""Phase 1E: static security contract of .github/workflows/brainfrog-verify.yml and its dependency lock.

The workflow is parsed with the strict YAML-subset loader in ci_gate_helpers (constructs outside the
subset fail the test). This is a static check of the file in this revision; it is not evidence that
GitHub executed it, and it does not replace `actionlint` (not available on the development host).
"""
from __future__ import annotations

import re
import sys
import unittest

from tests.ci_gate_helpers import REPO_ROOT, WORKFLOW_PATH, deep, load_policy_dict, workflow

# Reviewed action pins: each SHA was resolved from the upstream repository's tag (git ls-remote).
APPROVED_ACTIONS = {
    "step-security/harden-runner": ("e14015d583714f6e62063499dc959a02595150a1", "v2.21.1"),
    "actions/checkout": ("3d3c42e5aac5ba805825da76410c181273ba90b1", "v7.0.1"),
    "actions/setup-python": ("5fda3b95a4ea91299a34e894583c3862153e4b97", "v7.0.0"),
    "actions/upload-artifact": ("cf430e030ddbb5b0abf93d22962f4752f3646cd9", "v7.0.2"),
    "actions/attest": ("1e69f48acb82d1966a394da916b4c1698aa569d6", "v4.2.2"),
}
CI_SCRIPT = '"$RUNNER_TEMP/bf-venv/bin/python" -I trusted/scripts/brainfrog_ci.py'
RESOLVER = "python -I base/core/ci_trust.py resolve --candidate candidate --base base"
TRUSTED_REF = "${{ steps.resolve.outputs.trusted-sha }}"


def _steps(wf, job):
    return wf["jobs"][job]["steps"]


def _step(wf, job, name_prefix):
    matches = [s for s in _steps(wf, job) if s.get("name", "").startswith(name_prefix)]
    assert len(matches) == 1, f"expected one step '{name_prefix}' in {job}"
    return matches[0]


class TestWorkflowTriggersAndPermissions(unittest.TestCase):
    def setUp(self) -> None:
        self.wf = workflow()
        self.raw = WORKFLOW_PATH.read_text(encoding="utf-8")

    def test_only_pull_request_trigger(self) -> None:
        self.assertEqual(list(self.wf["on"]), ["pull_request"])
        body = re.sub(r"(?m)^\s*#.*$", "", self.raw)
        for forbidden in ("pull_request_target", "workflow_run", "issue_comment", "self-hosted", "ubuntu-slim"):
            self.assertNotIn(forbidden, body)

    def test_top_level_and_job_permissions_are_minimal(self) -> None:
        self.assertEqual(self.wf["permissions"], {"contents": "read"})
        self.assertEqual(self.wf["jobs"]["verification"]["permissions"], {"contents": "read"})
        self.assertEqual(self.wf["jobs"]["linux-selftest"]["permissions"], {"contents": "read"})
        self.assertEqual(self.wf["jobs"]["attest-evidence"]["permissions"],
                         {"id-token": "write", "attestations": "write", "artifact-metadata": "write"})

    def test_every_job_is_bounded_and_github_hosted(self) -> None:
        for name, job in self.wf["jobs"].items():
            with self.subTest(name):
                self.assertEqual(job["runs-on"], "ubuntu-24.04")
                self.assertIsInstance(job["timeout-minutes"], int)
                self.assertLessEqual(job["timeout-minutes"], 60)
        self.assertEqual(self.wf["concurrency"]["cancel-in-progress"], True)
        self.assertIn("github.event.pull_request.number", self.wf["concurrency"]["group"])

    def test_check_names_are_stable(self) -> None:
        self.assertEqual(self.wf["name"], "BrainFrog")
        self.assertEqual(self.wf["jobs"]["verification"]["name"], "Verification")
        self.assertEqual(self.wf["jobs"]["linux-selftest"]["name"], "Linux self-test")

    def test_no_secrets_or_tokens_are_referenced(self) -> None:
        body = re.sub(r"(?m)^\s*#.*$", "", self.raw)
        for forbidden in ("secrets.", "GITHUB_TOKEN", "github.token", "pull_request_target"):
            self.assertNotIn(forbidden, body)
        self.assertIsNone(re.search(r"(?m)^\s*(github-)?token:", body), "no token inputs are passed to any step")


class TestActionPinning(unittest.TestCase):
    def test_every_action_is_pinned_to_a_reviewed_full_sha(self) -> None:
        wf = workflow()
        raw_lines = WORKFLOW_PATH.read_text(encoding="utf-8").splitlines()
        uses = [s["uses"] for job in wf["jobs"].values() for s in job["steps"] if "uses" in s]
        self.assertTrue(uses)
        for ref in uses:
            with self.subTest(ref):
                repo, _, sha = ref.partition("@")
                self.assertIn(repo, APPROVED_ACTIONS, "unreviewed action")
                self.assertRegex(sha, r"^[0-9a-f]{40}$")
                self.assertEqual(sha, APPROVED_ACTIONS[repo][0])
                line = next(ln for ln in raw_lines if ref in ln)
                self.assertTrue(line.rstrip().endswith(f"# {APPROVED_ACTIONS[repo][1]}"), "tag comment drift")


class TestScriptInjectionAndCheckouts(unittest.TestCase):
    def setUp(self) -> None:
        self.wf = workflow()

    def test_no_expressions_or_unsafe_constructs_inside_run_scripts(self) -> None:
        for name, job in self.wf["jobs"].items():
            for step in job["steps"]:
                script = step.get("run")
                if script is None:
                    continue
                with self.subTest(job=name, step=step["name"]):
                    self.assertNotIn("${{", script, "event data must never be interpolated into a shell")
                    for bad in ("|| true", "eval ", "curl ", "wget ", "sudo ", "--privileged", "bash -c", "sh -c",
                                "pip install -e", "requirements.txt", "candidate/"):
                        self.assertNotIn(bad, script)
                    self.assertNotIn("continue-on-error", step)

    def test_checkouts_do_not_persist_credentials_and_bind_the_right_revisions(self) -> None:
        for job in ("verification", "linux-selftest"):
            checkouts = [s for s in self.wf["jobs"][job]["steps"] if s.get("uses", "").startswith("actions/checkout@")]
            self.assertEqual(len(checkouts), 3)
            candidate, base, trusted = checkouts
            for co in checkouts:
                self.assertIs(co["with"]["persist-credentials"], False)
                self.assertNotIn("token", co["with"])
                self.assertNotIn("allow-unsafe-pr-checkout", co["with"])
            self.assertEqual(candidate["with"]["path"], "candidate")
            self.assertNotIn("ref", candidate["with"], "candidate must be the revision GitHub tests (GITHUB_SHA)")
            self.assertEqual(base["with"]["path"], "base")
            self.assertEqual(base["with"]["ref"], "${{ github.base_ref }}")
            self.assertEqual(base["with"]["fetch-depth"], 0, "merge-base verification needs the branch history")
            self.assertEqual(trusted["with"]["path"], "trusted")
            self.assertEqual(trusted["with"]["ref"], TRUSTED_REF)

    def test_trusted_revision_is_resolved_from_the_tested_merge_before_its_checkout(self) -> None:
        # T1 retest (run 39731332404): the event's base.sha was stale; it must never select trusted code.
        raw = re.sub(r"(?m)^\s*#.*$", "", WORKFLOW_PATH.read_text(encoding="utf-8"))
        self.assertNotIn("pull_request.base.sha", raw)
        for job in ("verification", "linux-selftest"):
            steps = self.wf["jobs"][job]["steps"]
            (resolve,) = [s for s in steps if s.get("id") == "resolve"]
            self.assertEqual(resolve["run"].strip(), RESOLVER)
            self.assertNotIn("if", resolve, "the resolver runs, and fails the job, like any normal step")
            idx = steps.index(resolve)
            paths = [(s.get("with") or {}).get("path") for s in steps]
            self.assertLess(paths.index("candidate"), idx)
            self.assertLess(paths.index("base"), idx)
            self.assertLess([s.get("name") for s in steps].index("Set up Python"), idx)
            self.assertEqual(paths.index("trusted"), idx + 1, "the trusted checkout immediately follows the resolver")
            self.assertNotIn("if", steps[idx + 1], "no trusted checkout after a failed resolution")
            entry = [s["run"] for s in steps if (s.get("run") or "").startswith((CI_SCRIPT + " gate ",
                                                                                 CI_SCRIPT + " selftest "))]
            self.assertEqual(len(entry), 1)
            self.assertIn(" --base base ", entry[0], "the trusted code re-verifies the merge base")

    def test_dependencies_come_only_from_the_trusted_hash_lock(self) -> None:
        for job in ("verification", "linux-selftest"):
            installs = [s["run"] for s in self.wf["jobs"][job]["steps"] if "pip install" in s.get("run", "")]
            self.assertEqual(len(installs), 1)
            self.assertIn("--require-hashes", installs[0])
            self.assertIn("--no-deps", installs[0])
            self.assertIn("--only-binary=:all:", installs[0])
            self.assertIn("-r trusted/.github/brainfrog/requirements-verifier.txt", installs[0])

    def test_trusted_entry_point_runs_isolated(self) -> None:
        for job in ("verification", "linux-selftest"):
            for step in self.wf["jobs"][job]["steps"]:
                if "brainfrog_ci.py" in step.get("run", ""):
                    self.assertTrue(step["run"].strip().startswith(CI_SCRIPT), step["name"])
                if "ci_trust.py" in step.get("run", ""):
                    self.assertEqual(step["run"].strip(), RESOLVER, "resolver runs isolated, from the base branch")


class TestGateOrdering(unittest.TestCase):
    """Failed runs still upload evidence, and only the final enforcement decides the job outcome."""

    def setUp(self) -> None:
        self.steps = workflow()["jobs"]["verification"]["steps"]
        self.names = [s["name"] for s in self.steps]

    def test_order_is_gate_digest_upload_enforce(self) -> None:
        order = ["Run BrainFrog verification", "Record evidence manifest digest", "Upload verification evidence",
                 "Enforce verification outcome"]
        self.assertEqual(self.names[-4:], order)

    def test_post_gate_steps_always_run_and_nothing_hides_failure(self) -> None:
        for step in self.steps[-3:]:
            self.assertEqual(step.get("if"), "always()", step["name"])
        for step in self.steps:
            self.assertNotIn("continue-on-error", step)
        enforce = self.steps[-1]
        self.assertIn(" enforce --out ", enforce["run"])
        upload = self.steps[-2]
        self.assertEqual(upload["with"]["if-no-files-found"], "error")
        self.assertEqual(upload["with"]["retention-days"], 14)
        self.assertIs(upload["with"]["include-hidden-files"], False)
        self.assertTrue(upload["with"]["path"].endswith("/bf-ci/brainfrog-ci-evidence"))

    def test_attestation_job_is_isolated_and_opt_in(self) -> None:
        job = workflow()["jobs"]["attest-evidence"]
        self.assertEqual(job["needs"], "verification")
        self.assertIn("vars.BRAINFROG_ATTEST_EVIDENCE == 'true'", job["if"])
        self.assertIn("github.event.pull_request.head.repo.full_name == github.repository", job["if"])
        self.assertEqual(len(job["steps"]), 1, "no checkout, no scripts, no PR code in the signing job")
        self.assertTrue(job["steps"][0]["uses"].startswith("actions/attest@"))
        self.assertEqual(job["steps"][0]["with"]["subject-digest"],
                         "sha256:${{ needs.verification.outputs.manifest-sha256 }}")


class TestWorkflowIsTrustedConfiguration(unittest.TestCase):
    def test_workflow_and_policy_dir_are_trusted_paths(self) -> None:
        self.assertEqual(load_policy_dict()["trusted_paths"],
                         [".github/workflows/brainfrog-verify.yml", ".github/brainfrog/"])

    def test_committed_workflow_has_no_contract_violations(self) -> None:
        self.assertEqual(contract_violations(workflow()), [])

    def test_hostile_workflow_edits_are_contract_violations(self) -> None:
        """Case I (static part): typical self-certification edits are all caught by contract_violations().
        Whether GitHub would *run* such an edit is a repository-rules question (see phase1e-threat-model.md)."""
        def edit(fn):
            wf = deep(workflow())
            fn(wf)
            return contract_violations(wf)

        attempts = {
            "pull_request_target trigger": lambda wf: wf["on"].update(pull_request_target={}),
            "write token": lambda wf: wf.update(permissions={"contents": "write"}),
            "hide failure": lambda wf: wf["jobs"]["verification"]["steps"][-1].update({"continue-on-error": True}),
            "skip enforcement": lambda wf: wf["jobs"]["verification"]["steps"].pop(),
            "trust candidate verifier": lambda wf: _trusted_checkout(wf)["with"].update(
                ref="${{ github.event.pull_request.head.sha }}"),
            "trust the stale event base": lambda wf: _trusted_checkout(wf)["with"].update(
                ref="${{ github.event.pull_request.base.sha }}"),
            "skip the resolver": lambda wf: wf["jobs"]["verification"]["steps"].remove(_resolver(wf)),
            "weaken the resolver": lambda wf: _resolver(wf).update(run="exit 0\n"),
            "force green": lambda wf: wf["jobs"]["verification"]["steps"][-1].update(run="exit 0\n"),
            "shell interpolation": lambda wf: wf["jobs"]["verification"]["steps"][6].update(
                run="echo ${{ github.event.pull_request.title }}\n"),
        }
        for label, fn in attempts.items():
            with self.subTest(label):
                self.assertTrue(edit(fn), f"{label} was not detected")


def _trusted_checkout(wf) -> dict:
    return next(s for s in wf["jobs"]["verification"]["steps"] if (s.get("with") or {}).get("path") == "trusted")


def _resolver(wf) -> dict:
    return next(s for s in wf["jobs"]["verification"]["steps"] if s.get("id") == "resolve")


def contract_violations(wf) -> list:
    """Minimal machine-checkable form of the gate contract (used against hostile edits above)."""
    v = []
    if list(wf.get("on") or {}) != ["pull_request"]:
        v.append("trigger")
    if wf.get("permissions") != {"contents": "read"}:
        v.append("workflow permissions")
    job = (wf.get("jobs") or {}).get("verification") or {}
    if job.get("permissions") != {"contents": "read"}:
        v.append("job permissions")
    steps = job.get("steps") or []
    for s in steps:
        if "continue-on-error" in s:
            v.append("continue-on-error")
        if "${{" in (s.get("run") or ""):
            v.append("expression in run")
    trusted = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@")
               and (s.get("with") or {}).get("path") == "trusted"]
    if len(trusted) != 1 or trusted[0]["with"].get("ref") != TRUSTED_REF:
        v.append("trusted checkout ref")
    resolvers = [s for s in steps if s.get("id") == "resolve"]
    if len(resolvers) != 1 or (resolvers[0].get("run") or "").strip() != RESOLVER \
            or (trusted and steps.index(resolvers[0]) > steps.index(trusted[0])):
        v.append("trusted revision resolver")
    if not steps or not (steps[-1].get("run") or "").strip().startswith(CI_SCRIPT + " enforce ") \
            or steps[-1].get("if") != "always()":
        v.append("final enforcement")
    return v


@unittest.skipIf(sys.version_info < (3, 11), "tomllib requires Python 3.11+")
class TestVerifierDependencyLock(unittest.TestCase):
    def test_every_requirement_is_hash_pinned_and_present_in_uv_lock(self) -> None:
        import tomllib

        lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
        hashes = {}
        for pkg in lock["package"]:
            artifacts = pkg.get("wheels", []) + ([pkg["sdist"]] if "sdist" in pkg else [])
            hashes[(pkg["name"], pkg["version"])] = {a["hash"].split(":", 1)[1] for a in artifacts if "hash" in a}
        text = (REPO_ROOT / ".github/brainfrog/requirements-verifier.txt").read_text(encoding="utf-8")
        blocks = re.split(r"\n(?=[a-z0-9])", text.strip())
        self.assertGreater(len(blocks), 20)
        for block in blocks:
            with self.subTest(block.splitlines()[0]):
                m = re.match(r"^([a-z0-9_.-]+)==(\S+)", block)
                self.assertIsNotNone(m)
                pinned = set(re.findall(r"--hash=sha256:([0-9a-f]{64})", block))
                self.assertTrue(pinned, "every requirement carries hashes")
                self.assertTrue(pinned <= hashes[(m.group(1), m.group(2))], "hashes must come from uv.lock")

    def test_pytest_and_its_runtime_dependencies_are_locked_for_the_ci_interpreter(self) -> None:
        # T1: two self-tests run `sys.executable -m pytest`, but the CI venv had no pytest.
        text = (REPO_ROOT / ".github/brainfrog/requirements-verifier.txt").read_text(encoding="utf-8")
        for name in ("pytest", "pluggy", "iniconfig", "packaging"):
            with self.subTest(name):
                pins = re.findall(rf"(?m)^{name}==(\S+)(?: ; ([^\n]*?))? \\$", text)
                ci = [ver for ver, marker in pins
                      if not marker or marker == "python_full_version >= '3.10'"]
                self.assertEqual(len(ci), 1, f"{name} must be pinned exactly once for CPython 3.12 on Linux")


if __name__ == "__main__":
    unittest.main()
