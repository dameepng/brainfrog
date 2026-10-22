"""Phase 1E: symlink confinement on hosts that can create symlinks (Linux CI is the reference platform).

Covers snapshot materialization, the offline report checker, spec-path resolution and the CI gate. Only
synthetic sentinel files inside each test's own temp directory are used; nothing reads real host files.

On hosts without symlink privilege (e.g. Windows without Developer Mode) these tests SKIP. A skip is not
proof: the CI self-test lists them as required and treats a skip as a failure.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from core.ci_gate import (
    _bundle_hygiene,
    build_evidence,
    enforce,
    evaluate_target,
    load_policy,
    run_targets,
    trusted_config_changes,
)
from core.runtime.sandbox import materialize_source_snapshot_with_manifest
from core.verifier import StandaloneVerifier, verify_report_integrity
from tests.ci_gate_helpers import commit_all, init_repo, make_report_bundle, make_trusted_and_candidate, rehash_report

SENTINEL = "BF-SYNTHETIC-HOST-SECRET-SENTINEL"
LIMITS = {"max_total_bytes": 1_000_000, "max_files": 50, "max_file_bytes": 100_000}
TARGET = {"id": "fixture-login-app", "path": "tests/fixtures/browser_app/correct",
          "spec": ".github/brainfrog/specs/fixture-login-app.md", "required_criteria": ["CI-01"]}


class _SymlinkCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bf_sym_")).resolve()
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text(SENTINEL, encoding="utf-8")
        try:
            os.symlink(self.outside / "secret.txt", self.tmp / "probe")
        except (OSError, NotImplementedError) as exc:
            shutil.rmtree(self.tmp, ignore_errors=True)
            self.skipTest(f"host cannot create symlinks: {exc}")
        (self.tmp / "probe").unlink()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assert_no_sentinel(self, root: Path) -> None:
        for p in root.rglob("*"):
            if p.is_file() and not p.is_symlink():
                self.assertNotIn(SENTINEL, p.read_text(encoding="utf-8", errors="replace"), str(p))


class TestSnapshotSymlinks(_SymlinkCase):
    def make_repo(self, git: bool) -> Path:
        repo = init_repo(self.tmp / "repo") if git else (self.tmp / "repo")
        repo.mkdir(exist_ok=True)
        (repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
        return repo

    def snapshot(self, repo: Path, git: bool):
        if git:
            commit_all(repo)
        return materialize_source_snapshot_with_manifest(repo, self.tmp / f"snap_{git}")

    def test_file_symlink_outside_root_is_not_copied(self) -> None:
        for git in (False, True):
            with self.subTest(git_listing=git):
                shutil.rmtree(self.tmp / "repo", ignore_errors=True)
                repo = self.make_repo(git)
                os.symlink(self.outside / "secret.txt", repo / "link.txt")
                snap = self.snapshot(repo, git)
                self.assertIn("link.txt", snap.skipped_symlinks)
                self.assertNotIn("link.txt", snap.files)
                self.assertFalse((snap.snapshot_dir / "link.txt").exists())
                self.assert_no_sentinel(snap.snapshot_dir)

    def test_directory_symlink_outside_root_is_not_traversed(self) -> None:
        repo = self.make_repo(False)
        os.symlink(self.outside, repo / "linkdir", target_is_directory=True)
        snap = self.snapshot(repo, False)
        self.assertTrue(any(s.startswith("linkdir") for s in snap.skipped_symlinks))
        self.assertFalse(any(f.startswith("linkdir") for f in snap.files))
        self.assert_no_sentinel(snap.snapshot_dir)

    def test_broken_symlink_is_skipped(self) -> None:
        repo = self.make_repo(True)
        os.symlink(self.tmp / "does-not-exist", repo / "broken")
        snap = self.snapshot(repo, True)
        self.assertIn("broken", snap.skipped_symlinks)
        self.assertEqual(sorted(snap.files), ["app.py"])

    def test_nested_and_internal_symlinks_are_never_followed(self) -> None:
        repo = self.make_repo(False)
        (repo / "sub").mkdir()
        os.symlink(repo / "app.py", repo / "sub" / "internal_link.py")  # stays inside the root
        os.symlink(repo / "sub", repo / "sub" / "loop", target_is_directory=True)
        (repo / "sub" / "deeper").mkdir()
        os.symlink(self.outside / "secret.txt", repo / "sub" / "deeper" / "escape.txt")
        snap = self.snapshot(repo, False)
        self.assertEqual(sorted(snap.files), ["app.py"])
        for rel in ("sub/internal_link.py", "sub/loop", "sub/deeper/escape.txt"):
            self.assertIn(rel, snap.skipped_symlinks)
        self.assert_no_sentinel(snap.snapshot_dir)


class TestReportCheckerSymlinks(_SymlinkCase):
    def bundle(self):
        rdir, _ = make_report_bundle(self.tmp / "reports", "t", {"CI-01": "PASS"})
        self.assertTrue(verify_report_integrity(rdir)[0])
        return rdir

    def test_evidence_file_symlink_escape_is_rejected(self) -> None:
        rdir = self.bundle()
        log = rdir / "evidence/CI-01/stdout.log"
        log.unlink()
        os.symlink(self.outside / "secret.txt", log)
        rehash_report(rdir)  # a consistent forger: the recorded hash matches the outside file
        ok, findings = verify_report_integrity(rdir)
        self.assertFalse(ok)
        self.assertTrue(any("outside the report bundle" in f for f in findings), findings)

    def test_evidence_directory_symlink_escape_is_rejected(self) -> None:
        rdir = self.bundle()
        shutil.rmtree(rdir / "evidence/CI-01")
        (self.outside / "CI-01").mkdir()
        (self.outside / "CI-01" / "stdout.log").write_text(SENTINEL, encoding="utf-8")
        os.symlink(self.outside / "CI-01", rdir / "evidence/CI-01", target_is_directory=True)
        rehash_report(rdir)
        ok, findings = verify_report_integrity(rdir)
        self.assertFalse(ok)
        self.assertTrue(any("outside the report bundle" in f for f in findings), findings)

    def test_run_evidence_symlink_escape_is_rejected(self) -> None:
        rdir = self.bundle()
        (rdir / "evidence/_run").mkdir(parents=True)
        os.symlink(self.outside / "secret.txt", rdir / "evidence/_run/snapshot_manifest.json")
        report = json.loads((rdir / "report.json").read_text(encoding="utf-8"))
        report["run_evidence_files"] = {"snapshot_manifest.json": "evidence/_run/snapshot_manifest.json"}
        import hashlib
        report["run_evidence_hashes"] = {"snapshot_manifest.json": hashlib.sha256(SENTINEL.encode()).hexdigest()}
        (rdir / "report.json").write_text(json.dumps(report), encoding="utf-8")
        ok, findings = verify_report_integrity(rdir)
        self.assertFalse(ok)
        self.assertTrue(any("outside the report bundle" in f for f in findings), findings)


class TestSpecPathSymlinks(_SymlinkCase):
    def test_spec_symlink_outside_root_is_refused(self) -> None:
        root = self.tmp / "trusted"
        root.mkdir()
        (self.outside / "spec.md").write_text("# Spec\n\n## Acceptance Criteria\n", encoding="utf-8")
        os.symlink(self.outside / "spec.md", root / "spec.md")
        repo = self.tmp / "repo"
        repo.mkdir()
        verifier = StandaloneVerifier(repo_dir=repo, spec_root=root, reports_base_dir=self.tmp / "reports")
        with self.assertRaises(PermissionError):
            verifier.verify_spec("spec.md")
        with self.assertRaises(PermissionError):
            verifier.verify_spec(str(root / "spec.md"))  # absolute form resolves through the link too
        self.assertFalse((self.tmp / "reports").exists())

    def test_absolute_dotdot_spec_path_is_refused(self) -> None:
        root = self.tmp / "trusted"
        root.mkdir()
        (self.outside / "spec.md").write_text("# Spec\n", encoding="utf-8")
        verifier = StandaloneVerifier(repo_dir=root, reports_base_dir=self.tmp / "reports")
        with self.assertRaises(PermissionError):
            verifier.verify_spec(str(root) + os.sep + ".." + os.sep + "outside" + os.sep + "spec.md")


class TestGateSymlinks(_SymlinkCase):
    def test_candidate_target_symlink_is_blocked(self) -> None:
        trusted, candidate, _, _ = make_trusted_and_candidate(self.tmp / "ci", with_fixture=None)
        target = candidate / "tests/fixtures/browser_app/correct"
        target.parent.mkdir(parents=True)
        os.symlink(self.outside, target, target_is_directory=True)
        policy, _ = load_policy(trusted)
        runs = run_targets(policy, trusted, candidate, self.tmp / "work", {"browser": {"image_id": "x"}},
                           docker=str(self.tmp / "no-docker"))
        self.assertIn("is missing or is a link", runs[0]["error"])
        self.assertFalse((self.tmp / "work" / "reports").exists())

    def test_bundle_hygiene_rejects_links(self) -> None:
        rdir, run_id = make_report_bundle(self.tmp / "reports", TARGET["id"], {"CI-01": "PASS"})
        os.symlink(self.outside / "secret.txt", rdir / "leak.txt")
        self.assertTrue(verify_report_integrity(rdir)[0], "the generic checker does not look at bundle-root files")
        self.assertTrue(any("link in report bundle" in f for f in _bundle_hygiene(rdir, LIMITS)))
        ev = evaluate_target(TARGET, {"run_id": run_id, "exit_code": 0}, self.tmp, LIMITS)
        self.assertEqual(ev["outcome"], "blocked")

    def test_trusted_path_replaced_by_symlink_is_a_change(self) -> None:
        trusted, candidate, _, _ = make_trusted_and_candidate(self.tmp / "ci")
        shutil.rmtree(candidate / ".github/brainfrog")
        os.symlink(trusted / ".github/brainfrog", candidate / ".github/brainfrog", target_is_directory=True)
        policy, _ = load_policy(trusted)
        self.assertIn(".github/brainfrog/", trusted_config_changes(policy, trusted, candidate))

    def test_enforce_rejects_symlink_in_published_bundle(self) -> None:
        rdir, run_id = make_report_bundle(self.tmp / "work" / "reports", TARGET["id"], {"CI-01": "PASS"})
        ev = evaluate_target(TARGET, {"run_id": run_id, "exit_code": 0}, self.tmp / "work", LIMITS)
        bundle = build_evidence(self.tmp / "out", {"schema": "x"}, {"outcome": ev["outcome"], "reasons": []}, [ev])
        self.assertEqual(enforce(self.tmp / "out"), 0)
        os.symlink(self.outside / "secret.txt", bundle / "reports" / "leak.txt")
        self.assertEqual(enforce(self.tmp / "out"), 2)


if __name__ == "__main__":
    unittest.main()
