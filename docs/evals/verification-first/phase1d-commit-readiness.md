# Phase 1D — Commit Readiness

**Date:** 2026-10-21
**Worktree:** `<PHASE_1D_WORKTREE>`. **Base:** `c78e798`. **Checkpoint branch:** `feat/verification-first-1b-1d-checkpoint`. This is a local commit authorized by the operator: not pushed, no PR, not merged.

## Recommendation

| Question | Answer |
| :--- | :--- |
| Checkpoint commit | **READY FOR CHECKPOINT COMMIT** (awaiting your authorization). The [regression sign-off](phase1d-regression-signoff.md) completed on 2026-10-21: <ul><li>14/14 suites ran one at a time, 187 tests;</li><li>no new failures;</li><li>the 2 `test_11_repl_proof_aliases` errors were re-proved on a clean `HEAD` export;</li><li>the 2 Windows symlink skips are disclosed.</li></ul> The manifest below, plus `phase1d-regression-signoff.md`, is the commit content. |
| Merge | **Not authorized** by this review. It needs your review, CI and a PR. |
| Public use on untrusted content | **Not ready.** Reports are unsigned, the container kernel is shared, only static apps are supported, and the runtime needs ongoing maintenance. |

**Branch:** create a dedicated branch from `c78e798` for the checkpoint (for example `feat/verification-first-phase1b-1d`) instead of committing on a detached HEAD. A neutral alternative is checking out a new branch in this worktree; this review creates neither.

**History split (optional):** HEAD contains none of Phase 1B–1D, so the commit necessarily carries the inherited Phase 1B/1C verifier work too. For reviewable history, consider two commits:

1. The Phase 1B/1C state exactly as in `brainfrog-vf-1c-execution` (the "identical to 1C worktree" rows below, plus the 1C versions of `core/verifier.py` and `cli.py`).
2. Phase 1D plus this closure.

## Pre-commit requirements (AGENTS.md)

| Requirement | Status |
| :--- | :--- |
| `git diff` / `git diff --cached` reviewed | Done for tracked changes (`cli.py`, `core/runtime/command_policy.py`); untracked files reviewed by inventory |
| `python -m security.git_guard --staged` | **Run after staging.** Nothing is staged yet. A per-file scan of all 120 candidate text files gave **0 findings**, and the positive control was detected. |
| Relevant tests | Phase 1D suites 82/82 and 2 inherited suites pass on the final code. The 14 inherited suites pass: 187 tests, 2 baseline errors, 2 platform skips ([sign-off §C](phase1d-regression-signoff.md)). |
| `git diff --check` | Clean |
| Credential-like literals | None. Fixtures use synthetic values (`not-a-secret`, `demo.user@example.test`); the Phase 1C test's `OPENAI_API_KEY=x` is a synthetic placeholder. |

## Files to exclude from Git

| Path | Class | Reason |
| :--- | :--- | :--- |
| `.brainfrog/workspace.lock` | F. Local machine artifact | `WorkspaceLock` OS byte-range lock target (1 NUL byte). There is no `.meta` file, so no holder is recorded. It isn't git-ignored and is **left in place** (not removed). |
| `.brainfrog/reports/verify_20261020_151138_*` (6 files), `verify_20261020_152029_*` (23 files) | E/G. Local reports from the **earlier Phase 1D attempt** | Historical, prototype-era, local-only evidence. Preserved and not published. |
| Report bundles in the session scratchpad (`p1d/kept_reports`, `p1d_close/kept_reports`, demo repos) | E. Forensic originals | Outside the repository. Their `effective_container_config` records host temp paths under the user profile. |
| `__pycache__/` | F | Already git-ignored |
| `phase1c-live-evidence/` (Phase 1C worktree only) | E | Contains user-profile paths. It was never transferred into this worktree. |
| `%TEMP%\bf_*` test directories | F | Outside the repository. They are left by inherited test suites, not deleted. |
| `docs/evals/verification-first/phase1d-live-evidence/` (44 files, class E below) | E. Generated evidence | **Excluded from the checkpoint commit by operator instruction.** Kept locally and unchanged. References to it in `phase1d-final-report.md`, `phase1d-live-browser-results.md` and `phase1d-final-security-closure.md` now mark it as local-only. That wording was corrected in Phase 1E; in commit `41c6f0f` one of them was a dangling relative link. |

Suggested `.gitignore` additions for the same commit, not applied by this review: `.brainfrog/workspace.lock*` and `.brainfrog/reports/`.

## Items needing reviewer attention

- **Machine paths (sanitized for the checkpoint).** Twelve docs, marked *paths sanitized* below, had absolute local worktree paths. These were replaced as follows:
  - the repository root became `<REPOSITORY_ROOT>`;
  - the three phase worktrees became `<PHASE_1B_WORKTREE>`, `<PHASE_1C_WORKTREE>` and `<PHASE_1D_WORKTREE>`;
  - other worktrees became `<WORKTREES_PARENT>/…`;
  - `file:///` links became repository-relative links.

  These were documentation-only edits, and the technical content is unchanged. The unsanitized Phase 1B/1C originals remain byte-identical in the protected Phase 1C worktree.
- **Superseded content.** `phase1d-final-report.md`, `phase1d-live-browser-results.md`, `phase1d-browser-security.md` and `phase1d-architecture.md` carry dated correction notes. Their run-1 statements (Chromium 131, sandbox off) are historical and are superseded by [phase1d-final-security-closure.md](phase1d-final-security-closure.md).
- **Image approval is per build.** `core/runtime/browser_sandbox.py` approves one local build, `sha256:21d60e45…`. Other machines must build from `docker/browser-runner/` and approve their own image ID; otherwise browser criteria are BLOCKED. Document this in the PR.
- **CRLF in `cli.py`.** The working copy of `cli.py` uses CRLF line endings, which Git will normalize.

## Candidate publication manifest (exact)

This lists 125 files, plus this document and `phase1d-regression-signoff.md`. Hashes were taken at review time and **re-verified (125/125 match) at regression sign-off**. Re-hash after any edit.

**Checkpoint commit content: 83 files.** That is classes A–D (11 + 39 + 29 + 2 = 81), plus this document and `phase1d-regression-signoff.md`. Class E is excluded. Files changed after sign-off, all with post-edit hashes below:

- **12 docs marked *paths sanitized*.** Documentation-only edits.
- **18 docs and 1 test file marked *whitespace normalized*.** Required by `git diff --cached --check`:
  - Markdown two-space hard breaks became `\` (same rendering), and stray trailing spaces were stripped.
  - `tests/test_verifier_execution_integrity.py` lost one blank line at EOF. Its AST is identical, and it was re-run: 17/17 OK.

No production code, fixture, lock or seccomp file changed after sign-off.

**Classes:**

| Class | Meaning |
| :--- | :--- |
| A | Production verification code |
| B | Tests and fixtures |
| C | Documentation |
| D | Dependency and image lock files |
| E | Sanitized generated evidence |
| F/G/H | Local, historical or unknown-ownership files; all are in the exclusion list above |

No **H (unknown ownership)** file is proposed. Every included file is either:

- byte-identical to the verified Phase 1C worktree, or
- written in Phase 1D or this closure.

### A. Production verification code (11)

| Status | Path | SHA-256 (prefix) | Origin |
| :--- | :--- | :--- | :--- |
| M | `cli.py` | `f2c01f84c361b324` | Phase 1C + 1D (+ closure) |
| ?? | `core/runtime/app_launcher.py` | `bd2c8a8ab93af805` | Phase 1D / closure |
| ?? | `core/runtime/app_profile.py` | `67e30c97f469a5ae` | Phase 1D / closure |
| ?? | `core/runtime/browser_plan.py` | `f7a68691f12763f7` | Phase 1D / closure |
| ?? | `core/runtime/browser_runner.py` | `c3dcf81dd4328674` | Phase 1D / closure |
| ?? | `core/runtime/browser_sandbox.py` | `10125b79322b95bb` | Phase 1D / closure |
| ?? | `core/runtime/browser_seccomp_chromium.json` | `569408de219735e8` | Phase 1D / closure |
| M | `core/runtime/command_policy.py` | `11dd08d9df971fc9` | Phase 1B/1C, identical to 1C worktree |
| ?? | `core/runtime/sandbox.py` | `7d3f2859f56db35c` | Phase 1B/1C, identical to 1C worktree |
| ?? | `core/runtime/verification_harness.py` | `4335f1be6a59fd78` | Phase 1B/1C, identical to 1C worktree |
| ?? | `core/verifier.py` | `137a05ebb1e50724` | Phase 1C + 1D (+ closure) |

### B. Tests and fixtures (39)

| Status | Path | SHA-256 (prefix) | Origin |
| :--- | :--- | :--- | :--- |
| ?? | `tests/browser_verification_helpers.py` | `2ebc9d73768eb302` | Phase 1D / closure |
| ?? | `tests/fixtures/browser_app/broken/app.js` | `005b2623dee28307` | Phase 1D / closure |
| ?? | `tests/fixtures/browser_app/broken/dashboard.html` | `0ee0e0346d81a4b0` | Phase 1D / closure |
| ?? | `tests/fixtures/browser_app/broken/index.html` | `2553cdf8bb9d448a` | Phase 1D / closure |
| ?? | `tests/fixtures/browser_app/broken/login.html` | `2e4b8a6011b62c6d` | Phase 1D / closure |
| ?? | `tests/fixtures/browser_app/broken/styles.css` | `24b3ef067dae42f9` | Phase 1D / closure |
| ?? | `tests/fixtures/browser_app/broken/tests/test_validation.py` | `2ffb94b00b978580` | Phase 1D / closure |
| ?? | `tests/fixtures/browser_app/broken/validation.py` | `e0fb2463139a0978` | Phase 1D / closure |
| ?? | `tests/fixtures/browser_app/correct/app.js` | `da49a57521921414` | Phase 1D / closure |
| ?? | `tests/fixtures/browser_app/correct/dashboard.html` | `0ee0e0346d81a4b0` | Phase 1D / closure |
| ?? | `tests/fixtures/browser_app/correct/index.html` | `2553cdf8bb9d448a` | Phase 1D / closure |
| ?? | `tests/fixtures/browser_app/correct/login.html` | `2e4b8a6011b62c6d` | Phase 1D / closure |
| ?? | `tests/fixtures/browser_app/correct/styles.css` | `e73fd24b203d78c9` | Phase 1D / closure |
| ?? | `tests/fixtures/browser_app/correct/tests/test_validation.py` | `2ffb94b00b978580` | Phase 1D / closure |
| ?? | `tests/fixtures/browser_app/correct/validation.py` | `e0fb2463139a0978` | Phase 1D / closure |
| ?? | `tests/fixtures/browser_app/hostile/escape.html` | `873f84efae34e71f` | Phase 1D / closure |
| ?? | `tests/fixtures/browser_app/hostile/file-nav.html` | `f9ccc6ee483ab937` | Phase 1D / closure |
| ?? | `tests/fixtures/browser_app/hostile/iframe.html` | `b005fbe3a6de5f2c` | Phase 1D / closure |
| ?? | `tests/fixtures/browser_app/hostile/index.html` | `2553cdf8bb9d448a` | Phase 1D / closure |
| ?? | `tests/fixtures/browser_app/hostile/memory.html` | `d5daf1ba9eb97476` | Phase 1D / closure |
| ?? | `tests/fixtures/browser_app/hostile/popup.html` | `98f1abc07b5597a7` | Phase 1D / closure |
| ?? | `tests/fixtures/browser_app/hostile/redirect.html` | `e47f945b36510f64` | Phase 1D / closure |
| ?? | `tests/fixtures/browser_app/hostile/styles.css` | `e73fd24b203d78c9` | Phase 1D / closure |
| ?? | `tests/fixtures/browser_app/hostile/subresources.html` | `e20f2b4aeda3699d` | Phase 1D / closure |
| ?? | `tests/fixtures/browser_app/hostile/websocket.html` | `db01f160984a2754` | Phase 1D / closure |
| ?? | `tests/fixtures/browser_app/spec_demo.md` | `b161ca739a198276` | Phase 1D / closure |
| ?? | `tests/fixtures/browser_app/spec_false_proof.md` | `740708d7e6fb58ab` | Phase 1D / closure |
| ?? | `tests/fixtures/browser_app/spec_hostile.md` | `7c357311532e86dc` | Phase 1D / closure |
| ?? | `tests/test_standalone_verifier.py` | `10534055a794e33d` | Phase 1B/1C, identical to 1C worktree |
| ?? | `tests/test_verifier_browser_evidence.py` | `5867d36f65fc71db` | Phase 1D / closure |
| ?? | `tests/test_verifier_browser_integration.py` | `6b2a93802ce26d56` | Phase 1D / closure |
| ?? | `tests/test_verifier_browser_plan.py` | `986a69d0793d7f61` | Phase 1D / closure |
| ?? | `tests/test_verifier_browser_security.py` | `f436ebe343e73496` | Phase 1D / closure |
| ?? | `tests/test_verifier_docker_integration.py` | `b5e1928d60366c7c` | Phase 1B/1C, identical to 1C worktree |
| ?? | `tests/test_verifier_evidence_integrity.py` | `8e14c8588af0272a` | Phase 1B/1C, identical to 1C worktree |
| ?? | `tests/test_verifier_execution_integrity.py` | `a7e60b1b3a241bf5` | Phase 1B/1C, 1C worktree content; *whitespace normalized* |
| ?? | `tests/test_verifier_phase1c_blockers.py` | `78aa0c46c592a017` | Phase 1B/1C, identical to 1C worktree |
| ?? | `tests/test_verifier_sandbox_execution.py` | `e7f0f1f3985f3a3f` | Phase 1B/1C, identical to 1C worktree |
| ?? | `tests/test_verifier_test_result_semantics.py` | `7feaee97f1be1f15` | Phase 1B/1C, identical to 1C worktree |

### C. Documentation (29)

| Status | Path | SHA-256 (prefix) | Origin |
| :--- | :--- | :--- | :--- |
| ?? | `docs/evals/verification-first/capability-audit.md` | `41224b13a553b9d9` | Phase 1B/1C doc, 1C worktree content; *paths sanitized*; *whitespace normalized* |
| ?? | `docs/evals/verification-first/experiment_data.json` | `1fd0a10e73f45162` | Phase 1B/1C doc, identical to 1C worktree |
| ?? | `docs/evals/verification-first/final-recommendation.md` | `8e30f1489e114bed` | Phase 1B/1C doc, 1C worktree content; *whitespace normalized* |
| ?? | `docs/evals/verification-first/implementation-report.md` | `c5ce007048071c11` | Phase 1B/1C doc, 1C worktree content; *paths sanitized*; *whitespace normalized* |
| ?? | `docs/evals/verification-first/mvp-spec.md` | `8a1a3df89a454a51` | Phase 1B/1C doc, 1C worktree content; *whitespace normalized* |
| ?? | `docs/evals/verification-first/phase1b-closure-audit.md` | `9b7ef71c8e31f9f8` | Phase 1B/1C doc, 1C worktree content; *paths sanitized*; *whitespace normalized* |
| ?? | `docs/evals/verification-first/phase1b-evidence-integrity.md` | `79ae7c2e2bb71eed` | Phase 1B/1C doc, 1C worktree content; *whitespace normalized* |
| ?? | `docs/evals/verification-first/phase1b-execution-boundary.md` | `706cb7bbfb8bf977` | Phase 1B/1C doc, 1C worktree content; *whitespace normalized* |
| ?? | `docs/evals/verification-first/phase1b-final-report.md` | `4fca372990a53e82` | Phase 1B/1C doc, 1C worktree content; *paths sanitized*; *whitespace normalized* |
| ?? | `docs/evals/verification-first/phase1b-security-test-report.md` | `edf3d7cb0b6f3659` | Phase 1B/1C doc, 1C worktree content; *paths sanitized*; *whitespace normalized* |
| ?? | `docs/evals/verification-first/phase1b-threat-model.md` | `f8da6248ea4ed6e9` | Phase 1B/1C doc, 1C worktree content; *whitespace normalized* |
| ?? | `docs/evals/verification-first/phase1b-workspace-audit.md` | `9609d17350cafecd` | Phase 1B/1C doc, 1C worktree content; *paths sanitized*; *whitespace normalized* |
| ?? | `docs/evals/verification-first/phase1c-capability-audit.md` | `b7c895b473059bef` | Phase 1B/1C doc, 1C worktree content; *paths sanitized*; *whitespace normalized* |
| ?? | `docs/evals/verification-first/phase1c-execution-design.md` | `37b7588248919137` | Phase 1B/1C doc, 1C worktree content; *whitespace normalized* |
| ?? | `docs/evals/verification-first/phase1c-final-report.md` | `d144f84abd10dd16` | Phase 1B/1C doc, 1C worktree content; *whitespace normalized* |
| ?? | `docs/evals/verification-first/phase1c-finalization-report.md` | `0aa0ebcade6151fc` | Phase 1B/1C doc, 1C worktree content; *paths sanitized* |
| ?? | `docs/evals/verification-first/phase1c-integration-results.md` | `56765e4aa6b42081` | Phase 1B/1C doc, 1C worktree content; *whitespace normalized* |
| ?? | `docs/evals/verification-first/phase1c-live-oci-validation.md` | `58d151a4235ddf21` | Phase 1B/1C doc, 1C worktree content; *paths sanitized* |
| ?? | `docs/evals/verification-first/phase1c-sandbox-security.md` | `5502f8d98b487a4e` | Phase 1B/1C doc, 1C worktree content; *whitespace normalized* |
| ?? | `docs/evals/verification-first/phase1c-test-semantics.md` | `5629eec3cd24d637` | Phase 1B/1C doc, 1C worktree content; *whitespace normalized* |
| ?? | `docs/evals/verification-first/phase1d-architecture.md` | `c84699e379cceaf6` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-assertion-semantics.md` | `bfc3c973b99efb33` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-browser-security.md` | `6d5e7434dcd8d7d4` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-browser-spec.md` | `87a56973df41e718` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-capability-audit.md` | `b673ea7f0f895933` | Phase 1D / closure; *paths sanitized* |
| ?? | `docs/evals/verification-first/phase1d-final-report.md` | `8c75c813bc7ec5a7` | Phase 1D / closure; *paths sanitized* |
| ?? | `docs/evals/verification-first/phase1d-final-security-closure.md` | `babbc375a454aeac` | Phase 1D / closure; *paths sanitized* |
| ?? | `docs/evals/verification-first/phase1d-live-browser-results.md` | `0684e13f50c7db30` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/verification-experiment.md` | `9413e90582d2b94e` | Phase 1B/1C doc, 1C worktree content; *whitespace normalized* |

### D. Dependency / image build lock files (2)

| Status | Path | SHA-256 (prefix) | Origin |
| :--- | :--- | :--- | :--- |
| ?? | `docker/browser-runner/Dockerfile` | `b860f73e252ddef7` | Phase 1D / closure |
| ?? | `docker/browser-runner/requirements.lock` | `1654ad6fd6ff2369` | Phase 1D / closure |

### E. Generated evidence (sanitized publication copies) (44): NOT in the checkpoint commit (operator exclusion)

| Status | Path | SHA-256 (prefix) | Origin |
| :--- | :--- | :--- | :--- |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run1-chromium131-unsandboxed/01_correct_mixed_spec.txt` | `d73d43441af42348` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run1-chromium131-unsandboxed/01_correct_mixed_spec_check_report.txt` | `c7ed8ffeb5d3fc5b` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run1-chromium131-unsandboxed/01_correct_mixed_spec_tamper_check.txt` | `b1f9ac519dde3b44` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run1-chromium131-unsandboxed/02_broken_mixed_spec.txt` | `2e607b828781b112` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run1-chromium131-unsandboxed/02_broken_mixed_spec_check_report.txt` | `e06f19bd21bde0c4` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run1-chromium131-unsandboxed/02_broken_mixed_spec_tamper_check.txt` | `3e46167cacd85e03` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run1-chromium131-unsandboxed/03_hostile_navigation.txt` | `7846a0848cc134bf` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run1-chromium131-unsandboxed/03_hostile_navigation_check_report.txt` | `dbf1f56038f67669` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run1-chromium131-unsandboxed/04_false_browser_proof.txt` | `b77fc4c6f7233ce9` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run1-chromium131-unsandboxed/04_false_browser_proof_check_report.txt` | `f45a04776cb4dc91` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run1-chromium131-unsandboxed/05_sandbox_none_fail_closed.txt` | `3bf38b310213e67a` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run1-chromium131-unsandboxed/05_sandbox_none_fail_closed_check_report.txt` | `0c16a2dc0dccb521` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run1-chromium131-unsandboxed/06_docker_unavailable.txt` | `b597a8f60d64aa9a` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run1-chromium131-unsandboxed/06_docker_unavailable_check_report.txt` | `fa04cd41f967fa0a` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run1-chromium131-unsandboxed/07_unsupported_app_profile.txt` | `1a23b26b10585b62` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run1-chromium131-unsandboxed/07_unsupported_app_profile_check_report.txt` | `22fbf4907b31b896` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run1-chromium131-unsandboxed/broken_AC-03_browser-assertions.json` | `7c33449eabd9e15f` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run1-chromium131-unsandboxed/broken_AC-03_invalid-email_FAIL.png` | `a0fbbb41164224a9` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run1-chromium131-unsandboxed/broken_AC-04_browser-assertions.json` | `db40ad3f98b456eb` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run1-chromium131-unsandboxed/broken_AC-04_mobile-overflow_FAIL.png` | `1c13e7fc937200f0` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run1-chromium131-unsandboxed/correct_AC-02_browser-assertions.json` | `5ad4de3e12f08f6e` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run1-chromium131-unsandboxed/demo_summary.json` | `7da506ae8458ee45` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run2-chromium156-sandboxed/01_correct_mixed_spec.txt` | `52c2102f597b8490` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run2-chromium156-sandboxed/01_correct_mixed_spec_check_report.txt` | `4cbcb52e61838922` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run2-chromium156-sandboxed/01_correct_mixed_spec_tamper_check.txt` | `f937f021246ef4c6` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run2-chromium156-sandboxed/02_broken_mixed_spec.txt` | `c98dcbbac362ea93` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run2-chromium156-sandboxed/02_broken_mixed_spec_check_report.txt` | `41b5e1661c448cb8` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run2-chromium156-sandboxed/02_broken_mixed_spec_tamper_check.txt` | `5f014cfbbf20d045` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run2-chromium156-sandboxed/03_hostile_navigation.txt` | `d0907f137a9bd56c` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run2-chromium156-sandboxed/03_hostile_navigation_check_report.txt` | `5df7c8cce91f6ea0` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run2-chromium156-sandboxed/04_false_browser_proof.txt` | `46e4fe6603d02c1a` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run2-chromium156-sandboxed/04_false_browser_proof_check_report.txt` | `9e80fa9f4d802fde` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run2-chromium156-sandboxed/05_sandbox_none_fail_closed.txt` | `42eb18ab25a98fbe` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run2-chromium156-sandboxed/05_sandbox_none_fail_closed_check_report.txt` | `ede8cfa84c2b2e3c` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run2-chromium156-sandboxed/06_docker_unavailable.txt` | `ed5798291bec00cd` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run2-chromium156-sandboxed/06_docker_unavailable_check_report.txt` | `a8d7a3ed8dd429ee` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run2-chromium156-sandboxed/07_unsupported_app_profile.txt` | `f07d59e2d42df806` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run2-chromium156-sandboxed/07_unsupported_app_profile_check_report.txt` | `a503e93edab39bf8` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run2-chromium156-sandboxed/broken_AC-03_browser-assertions.json` | `9f9ca5c3fb17adba` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run2-chromium156-sandboxed/broken_AC-03_invalid-email_FAIL.png` | `4a25c519562c1984` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run2-chromium156-sandboxed/broken_AC-04_browser-assertions.json` | `605160c4461ba7cb` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run2-chromium156-sandboxed/broken_AC-04_mobile-overflow_FAIL.png` | `51c643b6a7a3611c` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run2-chromium156-sandboxed/correct_AC-02_browser-assertions.json` | `8bd48282f24408a3` | Phase 1D / closure |
| ?? | `docs/evals/verification-first/phase1d-live-evidence/run2-chromium156-sandboxed/demo_summary.json` | `ca079d9390955171` | Phase 1D / closure |


Additionally: `docs/evals/verification-first/phase1d-commit-readiness.md` (this file).
