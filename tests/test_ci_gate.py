"""Phase 1E: trusted CI gate (core/ci_gate.py) — policy, identity, verdict contract, evidence, enforcement.

Docker-free: report bundles are synthetic and Docker is either absent or stubbed at the gate's own
inspection seam. Real-container runs of the same gate are in test_ci_gate_live.py.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import ci_gate
from core.ci_gate import (
    EVIDENCE_DIRNAME,
    PolicyError,
    build_evidence,
    collect_identity,
    enforce,
    evaluate_selftest_module,
    evaluate_target,
    load_policy,
    manifest_digest,
    parse_unittest_verbose,
    provision_images,
    run_gate,
    trusted_config_changes,
    verify_evidence_bundle,
    write_step_summary,
)
from tests.ci_gate_helpers import (
    ci_env,
    commit_all,
    git,
    load_event,
    load_policy_dict,
    make_report_bundle,
    make_trusted_and_candidate,
    rehash_report,
    write_policy,
)

LIMITS = {"max_total_bytes": 1_000_000, "max_files": 50, "max_file_bytes": 100_000}
TARGET = {"id": "fixture-login-app", "path": "tests/fixtures/browser_app/correct",
          "spec": ".github/brainfrog/specs/fixture-login-app.md", "required_criteria": ["CI-01", "CI-02"]}
MISSING_DOCKER = str(Path(tempfile.gettempdir()) / "bf-no-such-docker" / "docker")


def _synthetic_github_token() -> str:
    """Deterministic, obviously fake GitHub-style token fixture."""
    return "gh" + "p_" + "SYNTHETICGITHUBTOKENFIXTURE".ljust(36, "a")


class _TmpCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bf_ci_")).resolve()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestTrustedPolicy(_TmpCase):
    def setUp(self) -> None:
        super().setUp()
        self.trusted, self.candidate, self.base_sha, self.tested_sha = make_trusted_and_candidate(self.tmp)

    def test_committed_policy_is_valid_and_digest_is_byte_exact(self) -> None:
        policy, digest = load_policy(self.trusted)
        raw = (self.trusted / ".github/brainfrog/policy.json").read_bytes()
        self.assertEqual(digest, hashlib.sha256(raw).hexdigest())
        self.assertEqual(policy["targets"][0]["required_criteria"],
                         ["CI-01", "CI-02", "CI-03", "CI-04", "CI-05", "CI-06"])

    def test_malformed_policies_are_rejected(self) -> None:
        mutations = {
            "no targets": lambda p: p.update(targets=[]),
            "path traversal": lambda p: p["targets"][0].update(path="../outside"),
            "absolute path": lambda p: p["targets"][0].update(path="/etc"),
            "spec outside trusted dir": lambda p: p["targets"][0].update(spec="tests/fixtures/browser_app/spec_demo.md"),
            "empty required criteria": lambda p: p["targets"][0].update(required_criteria=[]),
            "duplicate required criteria": lambda p: p["targets"][0].update(required_criteria=["CI-01", "CI-01"]),
            "unapproved python image": lambda p: p["python_image"].update(tag="python:latest"),
            "python image without digest": lambda p: p["python_image"].update(repo_digest="python:3.12-slim"),
            "unapproved browser tag": lambda p: p["browser_image"].update(tag="evil/browser:latest"),
            "bad recipe hash": lambda p: p["browser_image"].update(dockerfile_sha256="0" * 63),
            "no trusted paths": lambda p: p.update(trusted_paths=[]),
            "wrong schema": lambda p: p.update(schema="brainfrog.ci-policy/0"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label):
                policy = load_policy_dict()
                mutate(policy)
                write_policy(self.trusted, policy)
                with self.assertRaises(PolicyError):
                    load_policy(self.trusted)

    def test_missing_policy_blocks_the_gate(self) -> None:
        (self.trusted / ".github/brainfrog/policy.json").unlink()
        decision = run_gate(self.trusted, self.candidate, self.tmp / "out", load_event("pull_request_valid.json"),
                            ci_env(self.tested_sha), docker=MISSING_DOCKER)
        self.assertEqual(decision["outcome"], "blocked")
        self.assertTrue(any("trusted policy" in r for r in decision["reasons"]))
        self.assertEqual(enforce(self.tmp / "out"), 2)


class TestIdentityBinding(_TmpCase):
    def setUp(self) -> None:
        super().setUp()
        self.trusted, self.candidate, self.base_sha, self.tested_sha = make_trusted_and_candidate(self.tmp)

    def identity(self, event_name="pull_request_valid.json", **kw):
        event = load_event(event_name, base_sha=self.base_sha, **({"head_sha": self.tested_sha} | kw))
        return collect_identity(event, ci_env(self.tested_sha), self.trusted, self.candidate)

    def test_valid_pull_request_binds_tested_head_and_base(self) -> None:
        ident = self.identity()
        self.assertEqual(ident["problems"], [])
        self.assertEqual(ident["tested_sha"], self.tested_sha)
        self.assertEqual(ident["trusted_verifier_revision"], self.base_sha)
        self.assertEqual(ident["pull_request_number"], 7)
        self.assertEqual((ident["workflow_run_id"], ident["workflow_run_attempt"]), (987654321, 1))
        self.assertFalse(ident["is_fork_pull_request"])

    def test_merge_commit_is_reported_as_tested_not_as_pr_head(self) -> None:
        ident = self.identity(head_sha="3" * 40)  # GitHub tested its synthetic merge commit
        self.assertEqual(ident["problems"], [])
        self.assertEqual(ident["tested_sha"], self.tested_sha)
        self.assertEqual(ident["head_sha"], "3" * 40)
        self.assertFalse(ident["tested_sha_is_pr_head"])

    def test_fork_pull_request_is_recorded(self) -> None:
        ident = self.identity("pull_request_fork.json")
        self.assertEqual(ident["problems"], [])
        self.assertTrue(ident["is_fork_pull_request"])
        self.assertEqual(ident["head_repository"], "external-contributor/brainfrog")

    def test_malicious_branch_name_and_title_stay_inert(self) -> None:
        ident = self.identity("pull_request_malicious_metadata.json")
        self.assertEqual(ident["problems"], [])
        self.assertIn("touch BF_PWNED_BRANCH", ident["head_ref"])  # kept only as data
        self.assertNotIn("title", json.dumps(ident))  # PR titles are never recorded
        self.assertFalse(list(self.tmp.rglob("BF_PWNED_*")), "event text must never be executed")
        self.assertFalse(list(Path.cwd().glob("BF_PWNED_*")), "event text must never be executed")

    def test_missing_head_sha_blocks(self) -> None:
        event = load_event("pull_request_missing_head_sha.json", base_sha=self.base_sha)
        ident = collect_identity(event, ci_env(self.tested_sha), self.trusted, self.candidate)
        self.assertTrue(any("pull_request.head.sha" in p for p in ident["problems"]))

    def test_unexpected_checkout_sha_blocks(self) -> None:
        event = load_event("pull_request_valid.json", base_sha=self.base_sha, head_sha=self.tested_sha)
        ident = collect_identity(event, ci_env("4" * 40), self.trusted, self.candidate)
        self.assertTrue(any("candidate checkout is at" in p for p in ident["problems"]))

    def test_trusted_checkout_must_be_the_pr_base(self) -> None:
        event = load_event("pull_request_valid.json", base_sha="5" * 40, head_sha=self.tested_sha)
        ident = collect_identity(event, ci_env(self.tested_sha), self.trusted, self.candidate)
        self.assertTrue(any("not the PR base revision" in p for p in ident["problems"]))

    def test_non_pull_request_event_blocks(self) -> None:
        event = load_event("pull_request_valid.json", base_sha=self.base_sha, head_sha=self.tested_sha)
        ident = collect_identity(event, ci_env(self.tested_sha, event_name="pull_request_target"),
                                 self.trusted, self.candidate)
        self.assertTrue(any("unsupported event" in p for p in ident["problems"]))

    def test_rerun_records_the_new_attempt(self) -> None:
        event = load_event("pull_request_valid.json", base_sha=self.base_sha, head_sha=self.tested_sha)
        ident = collect_identity(event, ci_env(self.tested_sha, run_attempt=3), self.trusted, self.candidate)
        self.assertEqual(ident["workflow_run_attempt"], 3)
        self.assertEqual(ident["problems"], [])


class TestTrustedConfigChanges(_TmpCase):
    def setUp(self) -> None:
        super().setUp()
        self.trusted, self.candidate, _, _ = make_trusted_and_candidate(self.tmp)
        self.policy, _ = load_policy(self.trusted)

    def test_identical_trusted_paths_are_not_changes(self) -> None:
        self.assertEqual(trusted_config_changes(self.policy, self.trusted, self.candidate), [])

    def test_workflow_edit_is_detected(self) -> None:
        wf = self.candidate / ".github/workflows/brainfrog-verify.yml"
        wf.write_text(wf.read_text(encoding="utf-8").replace("run: |", "run: | # edited", 1), encoding="utf-8")
        self.assertIn(".github/workflows/brainfrog-verify.yml",
                      trusted_config_changes(self.policy, self.trusted, self.candidate))

    def test_deleting_or_weakening_the_trusted_spec_is_detected(self) -> None:
        spec = self.candidate / ".github/brainfrog/specs/fixture-login-app.md"
        spec.write_text(spec.read_text(encoding="utf-8").split("### CI-05")[0], encoding="utf-8")
        (self.candidate / ".github/brainfrog/policy.json").unlink()
        changes = trusted_config_changes(self.policy, self.trusted, self.candidate)
        self.assertIn(".github/brainfrog/specs/fixture-login-app.md", changes)
        self.assertIn(".github/brainfrog/policy.json", changes)

    def test_added_file_under_trusted_dir_is_detected(self) -> None:
        (self.candidate / ".github/brainfrog/extra-policy.json").write_text("{}", encoding="utf-8")
        self.assertIn(".github/brainfrog/extra-policy.json",
                      trusted_config_changes(self.policy, self.trusted, self.candidate))


class TestVerdictContract(_TmpCase):
    """Each report state maps to exactly one CI outcome; nothing but an all-PASS report is success."""

    def evaluate(self, statuses, run_exit=None, **kw):
        rdir, run_id = make_report_bundle(self.tmp / "reports", TARGET["id"], statuses, **kw)
        report = json.loads((rdir / "report.json").read_text(encoding="utf-8"))
        run = {"target": TARGET["id"], "run_id": run_id,
               "exit_code": report["exit_code"] if run_exit is None else run_exit}
        return evaluate_target(TARGET, run, self.tmp, LIMITS), rdir

    def test_all_required_pass_is_success(self) -> None:
        ev, _ = self.evaluate({"CI-01": "PASS", "CI-02": "PASS"})
        self.assertEqual((ev["outcome"], ev["exit_code"]), ("success", 0))

    def test_fail_is_failure(self) -> None:
        ev, _ = self.evaluate({"CI-01": "PASS", "CI-02": "FAIL"})
        self.assertEqual(ev["outcome"], "failure")
        self.assertIn("CI-02", " ".join(ev["reasons"]))

    def test_blocked_and_not_run_are_non_success(self) -> None:
        for status in ("BLOCKED", "NOT_RUN"):
            with self.subTest(status):
                ev, _ = self.evaluate({"CI-01": "PASS", "CI-02": status})
                self.assertEqual(ev["outcome"], "blocked")

    def test_missing_required_criterion_is_blocked(self) -> None:
        ev, _ = self.evaluate({"CI-01": "PASS"})
        self.assertEqual(ev["outcome"], "blocked")
        self.assertIn("required criteria missing", " ".join(ev["reasons"]))

    def test_empty_report_is_blocked(self) -> None:
        target = dict(TARGET, required_criteria=["CI-01"])
        rdir, run_id = make_report_bundle(self.tmp / "reports", TARGET["id"], {})
        ev = evaluate_target(target, {"run_id": run_id, "exit_code": 2}, self.tmp, LIMITS)
        self.assertEqual(ev["outcome"], "blocked")

    def test_missing_report_is_blocked(self) -> None:
        ev = evaluate_target(TARGET, {"target": TARGET["id"], "run_id": "verify_20261021_000000_x_y"}, self.tmp, LIMITS)
        self.assertEqual(ev["outcome"], "blocked")
        ev = evaluate_target(TARGET, {"target": TARGET["id"], "error": "verifier raised RuntimeError"}, self.tmp, LIMITS)
        self.assertEqual(ev["outcome"], "blocked")

    def test_report_verdict_cannot_override_criteria(self) -> None:
        ev, _ = self.evaluate({"CI-01": "PASS", "CI-02": "FAIL"}, overall="PASS", exit_code=0)
        self.assertNotEqual(ev["outcome"], "success")
        ev, _ = self.evaluate({"CI-01": "PASS", "CI-02": "PASS"}, overall="BLOCKED", exit_code=2)
        self.assertEqual(ev["outcome"], "blocked")  # e.g. the verifier's own drift detection

    def test_process_exit_code_must_match_report(self) -> None:
        ev, _ = self.evaluate({"CI-01": "PASS", "CI-02": "PASS"}, run_exit=2)
        self.assertEqual(ev["outcome"], "blocked")

    def test_report_from_another_spec_is_blocked(self) -> None:
        ev, _ = self.evaluate({"CI-01": "PASS", "CI-02": "PASS"}, spec_path="spec.md")
        self.assertEqual(ev["outcome"], "blocked")

    def test_tampered_or_substituted_evidence_is_blocked(self) -> None:
        ev, rdir = self.evaluate({"CI-01": "PASS", "CI-02": "PASS"})
        (rdir / "evidence/CI-01/stdout.log").write_text("forged", encoding="utf-8")
        ev = evaluate_target(TARGET, {"run_id": rdir.name, "exit_code": 0}, self.tmp, LIMITS)
        self.assertEqual(ev["outcome"], "blocked")
        self.assertIn("report integrity", " ".join(ev["reasons"]))

    def test_evidence_path_traversal_is_blocked(self) -> None:
        _, rdir = self.evaluate({"CI-01": "PASS", "CI-02": "PASS"})
        report = json.loads((rdir / "report.json").read_text(encoding="utf-8"))
        report["criteria"][0]["evidence_files"]["stdout.log"] = "../../../outside.log"
        (rdir / "report.json").write_text(json.dumps(report), encoding="utf-8")
        ev = evaluate_target(TARGET, {"run_id": rdir.name, "exit_code": 0}, self.tmp, LIMITS)
        self.assertEqual(ev["outcome"], "blocked")
        self.assertIn("outside the report bundle", " ".join(ev["reasons"]))


class TestArtifactSanitization(_TmpCase):
    def evaluate_with(self, text):
        rdir, run_id = make_report_bundle(self.tmp / "reports", TARGET["id"], {"CI-01": "PASS", "CI-02": "PASS"},
                                          extra_text=text)
        return evaluate_target(TARGET, {"run_id": run_id, "exit_code": 0}, self.tmp, LIMITS)

    def test_private_user_paths_are_never_published(self) -> None:
        for text in ("C:\\Users\\someone\\AppData\\Local\\Temp\\x", "/Users/someone/project", "/home/someone/x"):
            with self.subTest(text):
                shutil.rmtree(self.tmp / "reports", ignore_errors=True)
                ev = self.evaluate_with(text)
                self.assertEqual(ev["outcome"], "blocked")
                self.assertIn("private path", " ".join(ev["reasons"]))

    def test_runner_paths_are_allowed(self) -> None:
        ev = self.evaluate_with("/home/runner/work/_temp/bf_snapshot_x\n/tmp/bf_x\n")
        self.assertEqual(ev["outcome"], "success")

    def test_secret_like_content_is_withheld(self) -> None:
        ev = self.evaluate_with(f"Authorization: Bearer {_synthetic_github_token()}\n")
        self.assertEqual(ev["outcome"], "blocked")
        self.assertIn("secret-like", " ".join(ev["reasons"]))

    def test_secret_detection_is_value_based(self) -> None:
        self.assertFalse(ci_gate.contains_secret_values('{"authorization_reference": "container_isolation", '
                                                        '"password_field_length": 12, "token_count": 3}'))
        self.assertTrue(ci_gate.contains_secret_values("Cookie: session=abcdef123456\n"))
        self.assertTrue(ci_gate.contains_secret_values('{"authorization": "Basic Zm9vOmJhcg=="}'))
        pem = "-----BEGIN " + "PRIVATE KEY-----\n" + "QUJDREVGR0hJSktMTU5PUA" * 4 + "\n-----END " + "PRIVATE KEY-----"
        self.assertTrue(ci_gate.contains_secret_values(pem))
        self.assertFalse(ci_gate.contains_secret_values("already redacted: [REDACTED_TOKEN]\n"))

    def test_size_limits_are_enforced(self) -> None:
        ev = self.evaluate_with("x" * 200_000)
        self.assertEqual(ev["outcome"], "blocked")
        self.assertIn("size limit", " ".join(ev["reasons"]))


class TestEvidenceBundleAndEnforcement(_TmpCase):
    def bundle(self, statuses):
        rdir, run_id = make_report_bundle(self.tmp / "work" / "reports", TARGET["id"], statuses)
        ev = evaluate_target(TARGET, {"run_id": run_id, "exit_code": json.loads(
            (rdir / "report.json").read_text(encoding="utf-8"))["exit_code"]}, self.tmp / "work", LIMITS)
        decision = {"outcome": ev["outcome"], "exit_code": 0, "reasons": list(ev["reasons"])}
        return build_evidence(self.tmp, {"schema": "x"}, decision, [ev])

    def test_outcomes_map_to_exit_codes(self) -> None:
        for statuses, code in (({"CI-01": "PASS", "CI-02": "PASS"}, 0), ({"CI-01": "PASS", "CI-02": "FAIL"}, 1),
                               ({"CI-01": "PASS", "CI-02": "BLOCKED"}, 2)):
            with self.subTest(statuses):
                shutil.rmtree(self.tmp / "work", ignore_errors=True)
                bundle = self.bundle(statuses)
                self.assertTrue((bundle / "reports" / TARGET["id"]).is_dir(), "failed runs still publish evidence")
                self.assertEqual(enforce(self.tmp), code)

    def test_tampering_after_finalization_is_blocked(self) -> None:
        bundle = self.bundle({"CI-01": "PASS", "CI-02": "PASS"})
        log = next(bundle.rglob("stdout.log"))
        log.write_text("edited after finalization", encoding="utf-8")
        self.assertEqual(enforce(self.tmp), 2)

    def test_consistently_rehashed_report_is_still_caught_by_the_manifest(self) -> None:
        bundle = self.bundle({"CI-01": "PASS", "CI-02": "FAIL"})
        report_dir = next((bundle / "reports" / TARGET["id"]).iterdir())
        (report_dir / "evidence/CI-02/stdout.log").write_text("CI-02 PASS", encoding="utf-8")
        rehash_report(report_dir)
        decision, findings = verify_evidence_bundle(bundle)
        self.assertTrue(any("modified" in f for f in findings))
        self.assertEqual(enforce(self.tmp), 2)

    def test_forged_success_decision_is_blocked(self) -> None:
        bundle = self.bundle({"CI-01": "PASS", "CI-02": "FAIL"})
        decision = json.loads((bundle / "decision.json").read_text(encoding="utf-8"))
        decision["outcome"] = "success"
        (bundle / "decision.json").write_text(json.dumps(decision), encoding="utf-8")
        self.assertEqual(enforce(self.tmp), 2)

    def test_extra_or_missing_files_are_blocked(self) -> None:
        bundle = self.bundle({"CI-01": "PASS", "CI-02": "PASS"})
        (bundle / "planted.txt").write_text("x", encoding="utf-8")
        self.assertEqual(enforce(self.tmp), 2)
        (bundle / "planted.txt").unlink()
        (bundle / "ci-metadata.json").unlink()
        self.assertEqual(enforce(self.tmp), 2)

    def test_missing_bundle_is_blocked(self) -> None:
        self.assertEqual(enforce(self.tmp / "nothing-here"), 2)
        self.assertIsNone(manifest_digest(self.tmp / "nothing-here"))

    def test_manifest_digest_matches_file(self) -> None:
        bundle = self.bundle({"CI-01": "PASS", "CI-02": "PASS"})
        self.assertEqual(manifest_digest(self.tmp), hashlib.sha256((bundle / "manifest.json").read_bytes()).hexdigest())


class TestGateWithoutDocker(_TmpCase):
    """Case E: no Docker capability -> BLOCKED, no criterion executes, no host fallback."""

    def test_missing_docker_blocks_without_host_execution(self) -> None:
        trusted, candidate, base_sha, tested_sha = make_trusted_and_candidate(self.tmp)
        event = load_event("pull_request_valid.json", base_sha=base_sha, head_sha=tested_sha)
        with patch.object(ci_gate, "run_targets") as run_targets:
            decision = run_gate(trusted, candidate, self.tmp / "out", event, ci_env(tested_sha), docker=MISSING_DOCKER)
        run_targets.assert_not_called()
        self.assertEqual(decision["outcome"], "blocked")
        self.assertTrue(any("Docker engine unavailable" in r for r in decision["reasons"]))
        self.assertFalse((self.tmp / "out" / "work" / "reports").exists())
        self.assertEqual(enforce(self.tmp / "out"), 2)
        meta = json.loads((self.tmp / "out" / EVIDENCE_DIRNAME / "ci-metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["tested_sha"], tested_sha)
        self.assertEqual(meta["overall_verdict"], "BLOCKED")
        self.assertEqual(meta["provenance"]["kind"], "unsigned-ci-context")
        self.assertEqual(meta["runtime_platform"]["sys_platform"], sys.platform)

    def test_wrong_image_identity_blocks(self) -> None:
        trusted, candidate, base_sha, tested_sha = make_trusted_and_candidate(self.tmp)
        policy, _ = load_policy(trusted)
        fake = {
            ("version",): "29.0.0",
            ("python",): {"Id": "sha256:" + "1" * 64, "RepoDigests": ["python@sha256:" + "9" * 64], "Os": "linux"},
            ("browser",): {"Id": "sha256:" + "2" * 64, "Os": "linux"},
        }

        def fake_docker(docker, args, timeout=60.0):
            if args[0] == "version":
                return fake[("version",)], ""
            return (fake[("python",)] if args[-1] == policy["python_image"]["tag"] else fake[("browser",)]), ""

        with patch.object(ci_gate, "_docker_json", side_effect=fake_docker):
            images = ci_gate.inspect_images(policy, trusted)
        self.assertTrue(any("pinned registry digest" in p for p in images["problems"]))

    def test_tampered_trusted_recipe_blocks(self) -> None:
        trusted, _, _, _ = make_trusted_and_candidate(self.tmp)
        policy, _ = load_policy(trusted)
        (trusted / "docker/browser-runner/Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        with patch.object(ci_gate, "_docker_json", return_value=(None, "no docker")):
            images = ci_gate.inspect_images(policy, trusted)
        self.assertTrue(any("Docker engine unavailable" in p for p in images["problems"]))
        with patch.object(ci_gate, "_docker_json", side_effect=lambda d, a, timeout=60.0: (
                ("29.0.0", "") if a[0] == "version" else (None, "missing"))):
            images = ci_gate.inspect_images(policy, trusted)
        self.assertTrue(any("does not match its pinned sha256" in p for p in images["problems"]))

    def test_provisioning_refuses_to_run_outside_github_actions(self) -> None:
        trusted, _, _, _ = make_trusted_and_candidate(self.tmp)
        policy, _ = load_policy(trusted)
        with patch.object(ci_gate, "_run_bounded") as bounded:
            problems = provision_images(policy, trusted, {"GITHUB_ACTIONS": "false"})
        bounded.assert_not_called()
        self.assertTrue(problems)

    def test_required_criterion_absent_from_trusted_spec_never_runs(self) -> None:
        trusted, candidate, base_sha, tested_sha = make_trusted_and_candidate(self.tmp)
        policy, _ = load_policy(trusted)
        policy["targets"][0]["required_criteria"].append("CI-99")
        runs = ci_gate.run_targets(policy, trusted, candidate, self.tmp / "work", {"browser": {"image_id": "x"}},
                                   docker=MISSING_DOCKER)
        self.assertIn("required criterion", runs[0]["error"])


class TestSelftestQuarantine(unittest.TestCase):
    KNOWN = ["tests.m.A.test_known", "tests.m.B.test_known"]
    Q = {"known_errors": KNOWN, "signature_prefix": "StopIteration"}

    @staticmethod
    def stderr(results, errors=None):
        lines = [f"{tid.rsplit('.', 1)[1]} ({tid}) ... {res}" for tid, res in results.items()]
        for tid, last in (errors or {}).items():
            lines += ["=" * 70, f"ERROR: {tid.rsplit('.', 1)[1]} ({tid})", "-" * 70,
                      "Traceback (most recent call last):", '  File "x.py", line 1, in t', last]
        lines += ["-" * 70, f"Ran {len(results)} tests in 0.1s", "", "OK"]
        return "\n".join(lines)

    def check(self, results, errors=None, rc=1, required=(), q=None):
        parsed = parse_unittest_verbose(self.stderr(results, errors))
        return evaluate_selftest_module("tests.m", parsed, rc, list(required), self.Q if q is None else q)

    def test_known_baseline_errors_with_matching_signature_are_accepted(self) -> None:
        res = {"tests.m.A.test_ok": "ok", self.KNOWN[0]: "ERROR", self.KNOWN[1]: "ERROR"}
        self.assertEqual(self.check(res, {k: "StopIteration" for k in self.KNOWN}), [])

    def test_changed_signature_is_rejected(self) -> None:
        res = {self.KNOWN[0]: "ERROR", self.KNOWN[1]: "ERROR"}
        problems = self.check(res, {self.KNOWN[0]: "StopIteration", self.KNOWN[1]: "KeyError: 'x'"})
        self.assertTrue(any("changed signature" in p for p in problems))

    def test_new_error_or_failure_is_rejected(self) -> None:
        res = {self.KNOWN[0]: "ERROR", self.KNOWN[1]: "ERROR", "tests.m.A.test_new": "FAIL"}
        problems = self.check(res, {k: "StopIteration" for k in self.KNOWN})
        self.assertTrue(any("unexpected FAIL" in p for p in problems))

    def test_quarantined_test_that_stops_failing_must_be_reviewed(self) -> None:
        res = {self.KNOWN[0]: "ok", self.KNOWN[1]: "ERROR"}
        problems = self.check(res, {self.KNOWN[1]: "StopIteration"})
        self.assertTrue(any("no longer errors" in p for p in problems))

    def test_required_test_must_run_not_skip(self) -> None:
        res = {"tests.m.A.test_symlink": "skipped"}
        problems = self.check(res, rc=0, required=["tests.m.A.test_symlink"], q={})
        self.assertTrue(any("did not pass" in p for p in problems))

    def test_zero_tests_and_missing_tests_are_rejected(self) -> None:
        self.assertTrue(any("no tests ran" in p for p in self.check({}, rc=0, q={})))
        parsed = {"results": {}, "signatures": {}, "ran": 3}
        self.assertTrue(evaluate_selftest_module("tests.m", parsed, 0, [], {}))
        self.assertTrue(evaluate_selftest_module("tests.m", parsed, None, [], {}))  # timeout

    def test_parser_handles_docstrings_and_interleaved_output(self) -> None:
        stderr = ("test_a (tests.m.A.test_a)\nDocstring line ... ok\n"
                  "test_b (tests.m.A.test_b) ... some stray output\nok\n"
                  "test_c (tests.m.A.test_c) ... skipped 'no symlinks'\n"
                  "-" * 70 + "\nRan 3 tests in 0.1s\n\nOK (skipped=1)\n")
        parsed = parse_unittest_verbose(stderr)
        self.assertEqual(parsed["results"], {"tests.m.A.test_a": "ok", "tests.m.A.test_b": "ok",
                                             "tests.m.A.test_c": "skipped"})
        self.assertEqual(parsed["ran"], 3)

    def test_committed_policy_quarantine_matches_the_documented_baseline(self) -> None:
        q = load_policy_dict()["selftest"]["quarantine"]
        self.assertEqual(list(q), ["tests.test_proof_inspection_ux"])
        self.assertEqual(sorted(q["tests.test_proof_inspection_ux"]["known_errors"]), [
            "tests.test_proof_inspection_ux.TestProofInspectionUX.test_11_repl_proof_aliases",
            "tests.test_proof_inspection_ux.TestVerifyProofCLISemantics.test_11_repl_proof_aliases",
        ])
        # Windows-only baseline: both tests pass on GitHub-hosted Linux (T1), so Linux gets no exception.
        self.assertEqual(q["tests.test_proof_inspection_ux"]["platforms"], ["win32"])

    def test_quarantine_applies_only_on_its_declared_platforms(self) -> None:
        entry = dict(self.Q, platforms=["win32"])
        self.assertEqual(ci_gate.quarantine_for_platform(entry, "win32"), (entry, None))
        self.assertEqual(ci_gate.quarantine_for_platform(entry, "linux"), ({}, None))
        self.assertEqual(ci_gate.quarantine_for_platform(None, "linux"), ({}, None))

    def test_quarantine_without_explicit_platforms_grants_nothing(self) -> None:
        for bad in (self.Q, dict(self.Q, platforms=[]), dict(self.Q, platforms="win32"), dict(self.Q, platforms=[""])):
            applicable, problem = ci_gate.quarantine_for_platform(bad, "win32")
            self.assertEqual(applicable, {})
            self.assertIn("platforms", problem)

    def test_linux_requires_the_windows_baseline_tests_to_pass(self) -> None:
        entry = dict(self.Q, platforms=["win32"])
        erroring = {self.KNOWN[0]: "ERROR", self.KNOWN[1]: "ERROR"}
        sigs = {k: "StopIteration" for k in self.KNOWN}
        passing = {self.KNOWN[0]: "ok", self.KNOWN[1]: "ok"}
        on = lambda plat: ci_gate.quarantine_for_platform(entry, plat)[0]
        # Windows: only the exact, signature-matched baseline is tolerated; a pass must be reviewed.
        self.assertEqual(self.check(erroring, sigs, q=on("win32")), [])
        self.assertTrue(any("no longer errors" in p for p in self.check(passing, rc=0, q=on("win32"))))
        # Linux: the same errors are ordinary failures, and passing is simply correct.
        linux = self.check(erroring, sigs, q=on("linux"))
        self.assertEqual(sum("unexpected ERROR" in p for p in linux), 2)
        self.assertEqual(self.check(passing, rc=0, q=on("linux")), [])


class TestSelftestRunPlatforms(_TmpCase):
    """run_selftest end to end with a real interpreter: the platform, not the candidate, selects the exception."""

    MODULE = "tests.test_quarantine_probe"
    KNOWN = ["tests.test_quarantine_probe.T.test_alias_a", "tests.test_quarantine_probe.T.test_alias_b"]

    def candidate(self, body: str) -> Path:
        root = self.tmp / f"cand_{len(list(self.tmp.iterdir()))}"
        (root / "tests").mkdir(parents=True)
        (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (root / "tests" / "test_quarantine_probe.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n"
            f"    def test_alias_a(self):\n        {body}\n"
            f"    def test_alias_b(self):\n        {body}\n", encoding="utf-8")
        return root

    def run_on(self, platform: str, body: str):
        policy = {"selftest": {"modules": [self.MODULE], "required_passing_tests": [], "quarantine": {
            self.MODULE: {"platforms": ["win32"], "known_errors": self.KNOWN, "signature_prefix": "StopIteration"}}}}
        out = self.tmp / f"out_{platform}_{len(list(self.tmp.iterdir()))}"
        rc = ci_gate.run_selftest(policy, self.candidate(body), out, sys.executable, platform=platform)
        return rc, json.loads((out / "selftest-summary.json").read_text(encoding="utf-8"))

    def test_windows_tolerates_only_the_exact_baseline(self) -> None:
        rc, summary = self.run_on("win32", "raise StopIteration")
        self.assertEqual((rc, summary["problems"]), (0, []))
        self.assertEqual(summary["runtime_platform"]["sys_platform"], "win32")

    def test_linux_fails_on_the_baseline_errors(self) -> None:
        rc, summary = self.run_on("linux", "raise StopIteration")
        self.assertEqual(rc, 1)
        self.assertEqual(sum("unexpected ERROR" in p for p in summary["problems"]), 2)
        self.assertEqual(summary["runtime_platform"]["sys_platform"], "linux")

    def test_linux_passes_when_the_tests_pass(self) -> None:
        rc, summary = self.run_on("linux", "pass")
        self.assertEqual((rc, summary["problems"]), (0, []))

    def test_windows_baseline_with_another_error_type_is_rejected(self) -> None:
        rc, summary = self.run_on("win32", "raise KeyError('x')")
        self.assertEqual(rc, 1)
        self.assertTrue(any("changed signature" in p for p in summary["problems"]))


class TestStepSummary(_TmpCase):
    def test_summary_escapes_untrusted_values(self) -> None:
        bundle = self.tmp / EVIDENCE_DIRNAME
        bundle.mkdir(parents=True)
        (bundle / "ci-metadata.json").write_text(json.dumps({
            "tested_sha": "<img src=x onerror=alert(1)>", "targets": [
                {"target": "t|x", "criterion_results": [{"criterion_id": "`rm -rf /`", "status": "PASS\n# h"}]}]}),
            encoding="utf-8")
        out = self.tmp / "summary.md"
        write_step_summary(str(out), {"outcome": "blocked", "reasons": ["[link](javascript:x)"]}, self.tmp)
        text = out.read_text(encoding="utf-8")
        self.assertNotIn("<", text)
        self.assertIn("&lt;img", text)
        self.assertIsNone(re.search(r"(?<!\\)`", text), "no live code spans")
        self.assertIsNone(re.search(r"(?<!\\)\]\(", text), "no live links")
        self.assertIn("t\\|x", text)


if __name__ == "__main__":
    unittest.main()
