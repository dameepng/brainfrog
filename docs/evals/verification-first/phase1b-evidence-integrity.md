# BrainFrog Phase 1B: Source-to-Evidence Binding & Proof Authenticity

**Document ID:** `BF-EVID-1B-001`\
**Date:** October 19, 2026\
**Auditor:** Antigravity Agentic Pair Programmer\
**Product Positioning:** "Agent says done. BrainFrog proves it."

---

## 1. Executive Summary

A critical limitation identified in prior audits is that recording `git rev-parse HEAD` alongside a dirty-workspace boolean flag is insufficient to uniquely identify the code that was actually executed. Furthermore, if source files or `spec.md` mutate while verification is actively running (workspace drift), an agent could verify benign code and then substitute different code into the repository before final commit.

Phase 1B resolves this by establishing:
1. **Deterministic Source Snapshot Digest:** A composite SHA-256 digest binding Git HEAD, the exact diff of modified tracked files, the content hashes of untracked source files, and the specification content.
2. **Pre- and Post-Verification Drift Detection:** The workspace state is fingerprinted before verification begins and re-inspected immediately after. Any detected modification invalidates the verification run with `BLOCKED (Workspace drift detected)`.
3. **Atomic Report Finalization:** Reports are written to an isolated temporary staging directory and atomically finalized only after all evidence hashes are verified.
4. **Standalone Report Integrity Verifier:** An independent integrity checker (`verify_report_integrity`) that parses `report.json`, recomputes all evidence SHA-256 hashes, and flags tampering, missing files, or unauthorized additions.
5. **Scientifically Rigorous Terminology:** Clear distinction between content-hashed, tamper-evident proofs and true cryptographic immutability.

---

## 2. Source Snapshot Digest Architecture

The `source_snapshot_digest` uniquely binds the verified state using the following canonical structure:

```text
source_snapshot_digest = SHA-256(
    "GIT_HEAD:" + git_head_sha + "\n" +
    "TRACKED_DIFF:" + sha256(git diff HEAD) + "\n" +
    "UNTRACKED_INDEX:" + sha256(sorted list of relative paths and sha256 hashes) + "\n" +
    "SPEC_HASH:" + spec_sha256 + "\n"
)
```

### Scenario Handling

| Scenario | State | Integrity Guarantee |
|---|---|---|
| **Scenario A: Clean Git Revision** | No modified or untracked files | `source_snapshot_digest` is deterministically derived from Git HEAD and spec hash. Commit-bound and reproducible. |
| **Scenario B: Tracked File Modified** | Working tree has uncommitted modifications | The exact diff is hashed into `source_snapshot_digest`. The report explicitly flags `workspace_status.is_dirty: true` and cannot be claimed as a clean commit proof. |
| **Scenario C: Untracked Source File** | Untracked source files present | All untracked source files within the workspace root are hashed and bound into `source_snapshot_digest`. |
| **Scenario D: Mid-Run Workspace Drift** | Source files modified while test is executing | Drift detector detects timestamp/hash mismatches at completion; status transitions to `BLOCKED`. |
| **Scenario E: Spec Mutation** | `spec.md` altered during run | Spec hash re-evaluated at report writing time; mismatch aborts with `BLOCKED`. |

---

## 3. Evidence Authenticity & Report Schema

Each verification report in `.brainfrog/reports/<run-id>/report.json` contains:

```json
{
  "run_id": "verify_20261019_155502_8ea97f",
  "verifier_version": "0.1.0-alpha",
  "created_at": "2026-10-19T15:55:05.229375+00:00",
  "source_revision": "c78e7988fbfa9c4a4b6b23355d22be024da0faca",
  "source_snapshot_digest": "8a3f1201bc90...",
  "workspace_status": {
    "is_dirty": true,
    "dirty_files": ["cli.py", "core/verifier.py"]
  },
  "workspace_drift_detected": false,
  "spec_path": "spec.md",
  "spec_sha256": "8ea97f2207452a40cc3fcab4467055ea938400167b1f911aa8200c7dda88b36a",
  "execution_trust_mode": "TRUSTED_OPERATOR",
  "execution_backend": "local_hardened_subprocess",
  "authorization_reference": "operator_explicit_flag (--trust-local-code)",
  "overall_verdict": "PASS",
  "exit_code": 0,
  "criteria": [
    {
      "criterion_id": "AC-01",
      "title": "Unit Tests",
      "verification_type": "test",
      "status": "PASS",
      "command": "python -m unittest tests.test_security",
      "actual_executed_argv": ["C:\\Python312\\python.exe", "-m", "unittest", "tests.test_security"],
      "exit_code": 0,
      "duration_ms": 342.61,
      "started_at": "2026-10-19T15:55:02.326713+00:00",
      "finished_at": "2026-10-19T15:55:02.668825+00:00",
      "evidence_files": {
        "stdout.log": "evidence/AC-01/stdout.log",
        "stderr.log": "evidence/AC-01/stderr.log"
      },
      "evidence_hashes": {
        "stdout.log": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "stderr.log": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
      },
      "failure_reason": null
    }
  ]
}
```

---

## 4. Standalone Report Integrity Verification (`verify_report_integrity`)

To prevent post-hoc file modification from passing unnoticed, BrainFrog provides a standalone verification routine:

```python
is_valid, findings = verify_report_integrity(report_dir)
```

The verifier enforces:
1. **Manifest Integrity:** `report.json` must be valid JSON matching the canonical schema.
2. **Evidence Hash Matching:** Every file referenced in `evidence_hashes` must exist and its SHA-256 must match the recorded hash.
3. **No Unaccounted Files:** Any extraneous file found within `evidence/` that is not declared in `report.json` is flagged as an unauthorized addition.
4. **No Path Traversal:** Evidence paths must remain strictly within the report directory.

---

## 5. Precise Terminology Matrix

To prevent misleading claims, BrainFrog adopts strict, scientifically accurate terminology:

| Term | Defensible Meaning in BrainFrog | When Applicable |
|---|---|---|
| **Content-Hashed** | Every evidence artifact has an explicit SHA-256 hash computed at generation time. | Always |
| **Tamper-Evident** | Any modification to an evidence file or report manifest causes `verify_report_integrity` to fail. | Always |
| **Commit-Bound** | The exact 40-character Git commit SHA is captured and verified against `git rev-parse HEAD`. | Always |
| **Source-Snapshot-Bound** | The exact content state (HEAD + diff + untracked files + spec) is cryptographically bound into `source_snapshot_digest`. | Always |
| **Internally Consistent** | The internal hashes and files in the report agree with one another. | Always |
| **Cryptographically Immutable** | **NOT CLAIMED FOR LOCAL DISK.** A local file on developer disk can always be modified by an administrator or root process. True immutability requires remote append-only transparency logs (e.g. Sigstore/Rekor) or hardware WORM storage. | **Never claimed for local reports** |
