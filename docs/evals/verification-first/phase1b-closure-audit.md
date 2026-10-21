# BrainFrog Phase 1B: Final Closure Audit & Independent Verification Review

**Repository:** `dameepng/brainfrog`\
**Original Development Workspace:** `<REPOSITORY_ROOT>`\
**True External Closure Worktree:** `<PHASE_1B_WORKTREE>`\
**Base Commit:** `c78e7988fbfa9c4a4b6b23355d22be024da0faca` (detached HEAD)\
**Branch:** `feat/trustworthy-verification-workflow`\
**Target:** Verification-First 1B Final Closure\
**Product Positioning:** "Agent says done. BrainFrog proves it."\
**Closure Verdict:** **CLOSED — VERIFIED WITH DOCUMENTED LIMITATIONS**\
**Execution Backend Status:** **PARTIAL — Untrusted Execution Safely Blocked**

---

## 1. Executive Summary & Audit Mandate

This document establishes the independent final closure audit of BrainFrog Verification-First 1B. The purpose of this audit was to move beyond preliminary self-assessments, establish genuine workspace isolation, reproduce and fix concrete security and correctness defects, validate authorization and execution trust boundaries, audit source-to-evidence cryptographic binding, and deliver an empirically verified closure verdict.

---

## 2. Source Integrity & True Workspace Isolation

### Misleading Isolation Correction
The previous session claimed development occurred inside an isolated snapshot (`scratch/verification_1b_isolated`). However, investigation revealed that code edits were made directly in `<REPOSITORY_ROOT>` before copying into `scratch/`, which is a nested subdirectory of the same Git repository, not an independent worktree.

### True External Isolation Procedure
1. A genuine external Git worktree was created at `<PHASE_1B_WORKTREE>` as a detached worktree based on verified commit `c78e7988fbfa9c4a4b6b23355d22be024da0faca`.
2. Verified hunk patches were applied for tracked shared files (`cli.py`, `core/runtime/command_policy.py`).
3. Dedicated standalone files (`core/verifier.py`, test suites, evals) were transferred and verified with 100% SHA-256 match.
4. **Original Workspace Protection:** A bitwise SHA-256 baseline of the original workspace was captured before development. Following all fixes and test executions, the original workspace was re-inspected: **100% untouched and unchanged**. Zero commits, zero pushes, zero stashes, and zero destructive Git commands were run.
5. **PR #44 Verification:** PR #44 (`feat: architecture-aware PLAN/BUILD workflow and safe recovery (phases 1–4)`) was inspected via GitHub CLI and verified as safely **MERGED** in upstream `main` without interference.

---

## 3. Historical Report Cleanup Forensic Assessment

A previous AGC session executed logic attempting to iterate over `.brainfrog/reports` and call `shutil.rmtree()` on child directories.

### Forensic Analysis
1. Physical inspection confirmed that 16 report directories exist in `.brainfrog/reports` (created between 22:47 and 22:55 on 2026-10-19).
2. However, there is no pre-event inventory or Git tracking of what existed prior to that session.
3. The presence of reports created at or after 22:47 does not constitute proof that earlier reports survived the cleanup.
4. **Corrected Forensic Verdict:** **`INDETERMINATE`** for pre-event contents (presence of later reports does not prove survival of earlier ones), with **`NO_LOSS_EVIDENCED`** for the specific test runs documented in the active session logs.

### Preventive Remediation
`safe_cleanup_report(target_dir, base_reports_dir)` was hardened in `core/verifier.py`:
- Enforces strict canonical path checks (`resolve()`).
- Rejects paths outside `base_reports_dir` or pointing to `base_reports_dir` itself.
- Rejects cleanup of sibling directories owned by other runs.
- Tested and verified by `test_15_historical_report_preservation`.

---

## 4. Confirmed Defects, Security Findings & Applied Fixes

During this independent audit, four concrete security and correctness defects were discovered and remediated in the closure worktree:

| # | Defect / Vulnerability | Pre-Fix Behavior | Remediated Behavior |
|---|------------------------|------------------|---------------------|
| 1 | **Environment Variable Trust Escalation** | `StandaloneVerifier` checked `BRAINFROG_TRUST_LOCAL_CODE=1` in environment. An untrusted repo with a malicious `.env` could silently grant itself operator trust. | Removed env var fallback. Only explicit parameter `trust_local_code=True` / `TRUSTED_OPERATOR` activates local execution. |
| 2 | **`ISOLATED_SANDBOX` Unconfined Host Fallback** | When `execution_trust_mode == ExecutionTrustMode.ISOLATED_SANDBOX`, missing container backend fell through to host execution while claiming container runtime. | Fails closed as `BLOCKED` with explicit reason (`unavailable_sandbox`). |
| 3 | **Unauthenticated Trust Reference** | `--trust-reference` was unrepresented in CLI and could be confused with cryptographic authorization. | Added `--trust-reference` to CLI and report manifest as an unauthenticated audit annotation. Tested that reference alone without `--trust-local-code` fails closed (`BLOCKED`). |
| 4 | **Custom Output Directory Drift Alarm** | `compute_source_snapshot_digest` could flag custom report directories inside workspace as drift. | Added `exclude_dirs` parameter to `compute_source_snapshot_digest` ensuring report output directories are excluded from drift calculation. |

---

## 5. Execution Trust Boundary & Authorization Semantics

### Trust Modes
- **`UNTRUSTED_FAIL_CLOSED` (Default):** All executable criteria fail closed as `BLOCKED` (exit code `2`). A command in `spec.md` confers zero execution privilege.
- **`TRUSTED_OPERATOR` (`--trust-local-code`):** Operator explicitly authorizes running checks locally with local user privileges. A clear console warning is printed.
- **`ISOLATED_SANDBOX`:** Reserved for containerized/virtualized environments (OCI containers). Fails closed in Phase 1B since container runtime is planned for Phase 1C.

### Execution Boundaries
- Executable allowlisting (`python -m unittest`, `pytest`, `npm test`) prevents shell injection (`shell=False`), but does **not** sandbox host resources.
- Python executables are pinned to `sys.executable` to prevent repo-root executable shadowing.
- Environment variables are scrubbed of all known cloud credentials and API keys.
- Shell metacharacters (`&&`, `|`, `;`, `>`, `<`) and shell wrappers (`cmd`, `powershell`, `bash`) are rejected.

---

## 6. Source Snapshot Integrity & Drift Detection

### Snapshot Digest Formulation
`compute_source_snapshot_digest(repo_dir, spec_hash, exclude_dirs)` computes:
$$	ext{Digest} = 	ext{SHA256}(	ext{Git HEAD SHA} \parallel 	ext{Git Diff (staged + unstaged)} \parallel 	ext{Sorted Untracked Files (rel_path:content_hash)} \parallel 	ext{Spec Content Hash})$$

### Invariants Tested & Verified
- **Staged Changes:** `git add` modifies tracked diff and changes snapshot digest (`test_18`).
- **Untracked Content:** Modifying untracked file contents without renaming changes snapshot digest (`test_17`).
- **Workspace Drift:** In-flight source modifications during verification force overall verdict to `BLOCKED` (`test_04`).
- **Spec Mutation:** In-flight `spec.md` modifications force overall verdict to `BLOCKED` (`test_05`).
- **TOCTOU Boundary:** Live workspace sampling before and after execution provides drift detection, not OS-level filesystem freezing.

---

## 7. Evidence Authenticity & Local Integrity Claims

### Local Integrity Checking
`verify_report_integrity(report_dir)` and `bf verify --check-report <dir>`:
- Re-reads every evidence file from disk and hashes raw bytes.
- Validates hashes against `report.json` manifest.
- Flags tampered evidence, missing files, extra unauthorized files, or corrupted manifests.

### Crucial Authenticity Limitation
A local directory hashed with SHA-256 is **locally consistency-checkable**, not **cryptographically immutable**. An attacker with write access to local disk could rewrite both evidence files and `report.json`. Phase 1B reports accurately claim tamper-evidence relative to recorded digests, not absolute external immutability.

---

## 8. Corrected Experiment Metrics & Evaluation Taxonomy

The evaluation metrics were revised to eliminate combined categories and overstatements:

| Category | Tasks | Status | Evaluation Meaning |
|---|---|---|---|
| `REAL_REPOSITORY_FUNCTIONAL_FAILURES` | 1 | FAIL (Exit 1) | Detected real existing failure in repo test (TASK-02) |
| `REAL_REPOSITORY_PASSING_CHECKS` | 2 | PASS (Exit 0) | Verified legitimate passing checks (TASK-01, TASK-03) |
| `SYNTHETIC_FUNCTIONAL_FAILURES` | 1 | FAIL (Exit 1) | Injected defect detected (TASK-04) |
| `SYNTHETIC_PASSING_CHECKS` | 1 | PASS (Exit 0) | Injected passing check verified (TASK-05) |
| `SECURITY_POLICY_BLOCKS` | 1 | BLOCKED (Exit 2) | Malicious shell injection blocked by policy (TASK-06) |
| `CAPABILITY_BLOCKS` | 1 | BLOCKED (Exit 2) | Unconfigured browser runner blocked (TASK-07) |

**Key Metric Rule:** Policy blocks and capability limits are not counted as functional defect detections.

---

## 9. Comprehensive Test Suite Results

All tests executed exclusively inside `<PHASE_1B_WORKTREE>`:

```text
======================================================================
1. tests/test_standalone_verifier.py (21 tests)
   - test_01_typesafe_free_startup to test_21_cli_trust_reference_recorded
   Result: 21 / 21 PASS (133.09s)

2. tests/test_verifier_execution_integrity.py (17 tests)
   - test_01_untrusted_spec_fails_closed_by_default to test_17_trust_reference_recorded
   Result: 17 / 17 PASS (76.44s)

3. tests/test_verifier_evidence_integrity.py (18 tests)
   - test_01_clean_revision_identity to test_18_staged_changes_captured
   Result: 18 / 18 PASS (146.47s)
======================================================================
TOTAL AUTOMATED TESTS: 56 / 56 PASS (100% Passing)
```

### Git Hygiene & Secret Scanning
- `git diff --check`: Passed cleanly (0 trailing whitespace or conflict markers).
- `python -m security.git_guard`: Clean across all modified and newly created files (0 findings).

---

## 10. Final Closure Recommendation & Next Steps

### Verdict
**`CLOSED — VERIFIED WITH DOCUMENTED LIMITATIONS`**

### Next Steps for Verification-First 1C
1. Implement disposable OCI container runner (Docker/Podman) for `ISOLATED_SANDBOX` execution.
2. Integrate headless browser automation (Playwright/Chrome DevTools MCP) for interactive UI criteria.
3. Cryptographically sign verification bundles or anchor digests to Git commits / transparency logs.
