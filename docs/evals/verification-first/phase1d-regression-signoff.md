# Phase 1D — Regression Sign-off

**Date:** 2026-10-21
**Worktree:** `brainfrog-vf-1d-browser`, detached at `c78e798`. Nothing is staged, committed, pushed or opened as a PR.
**Result:** `READY FOR CHECKPOINT COMMIT`. All 14 remaining suites ran on the final source tree, one at a time, each in its own process.

## A. Verdict

| Item | Status |
| :--- | :--- |
| Phase 1D functional status | **DONE.** Unchanged from [phase1d-final-security-closure.md](phase1d-final-security-closure.md). No source, test or security configuration was modified during sign-off. |
| Regression sign-off | **Complete.** 14/14 suites ran (187 tests). There are no new failures. The 2 errors are the known `test_11_repl_proof_aliases` baseline, re-proved on a clean `HEAD` export (§C). The 2 skips are the documented Windows symlink limitation. |
| Commit readiness | **Ready for a checkpoint commit**, on a dedicated branch, when you authorize it. This verdict does not create a commit. |
| Merge / public release | **Not authorized.** It needs a PR, CI and review. Symlink safety still needs Linux CI validation. Reports are unsigned (tamper-evident, not tamper-proof). The container kernel is shared, and only static apps are supported. This is not production-grade isolation for arbitrary hostile websites. |

## B. Memory and runtime

**Safety rule:** start a suite only when Windows `\Memory\Available MBytes` is at least 2048 and paging is stable. The host has 16 GiB of RAM in total.

| Attempt | Time (+07:00) | Available MB | Pages/sec | Outcome |
| :--- | :--- | :--- | :--- | :--- |
| 1 | 06:58–07:01 | 422–1,404 | peaks of 24k–26k | **Memory safety stop.** No suite started. |
| 2 (resume) | 18:23:49, 30 s preflight | 5,590–5,848 | avg 2,495, max 3,238 | Above the floor and stable. Started. Commit charge was 21,156 / 39,427 MB. |

**Lowest available memory during any suite:** 4,073 MB. The largest per-suite drop was about 1 GB, during the Docker-backed suites 4 and 5.

**Largest Python process tree:** 96 MB peak working set. This counts the Python process and its children only. Docker Desktop's VM memory is not included, but it shows up in the host's available-memory figure.

**Nothing was changed to free memory:** no process was terminated, there were no timeouts or retries, and no Docker, pagefile or Windows setting was changed.

**Runner and records:** `scratchpad/p1d_signoff/run_one.ps1`. It:

- starts one process per module, and the next only after the previous one exits;
- samples memory once per second;
- enforces a 540 s timeout that would kill only the recorded process tree.

Per-suite records are in `results.jsonl`, raw output is in `logs/`, and there is an `audit.log`.

## C. Test matrix

Every command is `python -m unittest <module> -v`, run from the worktree root. Results:

| # | Wave | Module | Ran | Pass | Fail | Err | Skip | Exit | Duration | Avail MB before → min → after | Peak tree WS | Classification |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 1 | A | `tests.test_verifier_evidence_integrity` | 18 | 18 | 0 | 0 | 0 | 0 | 185.3 s | 5,559 → 5,224 → 5,456 | 84 MB | pass |
| 2 | A | `tests.test_verifier_test_result_semantics` | 8 | 8 | 0 | 0 | 0 | 0 | 8.7 s | 5,478 → 5,340 → 5,414 | 70 MB | pass |
| 3 | A | `tests.test_verifier_sandbox_execution` | 5 | 5 | 0 | 0 | 0 | 0 | 8.6 s | 5,441 → 5,367 → 5,422 | 69 MB | pass |
| 4 | A | `tests.test_verifier_phase1c_blockers` | 29 | 28 | 0 | 0 | 1 | 0 | 117.8 s | 5,402 → 4,416 → 5,214 | 86 MB | pass; skip = Windows symlink |
| 5 | A | `tests.test_verifier_docker_integration` | 11 | 10 | 0 | 0 | 1 | 0 | 119.8 s | 5,224 → 4,408 → 4,445 | 92 MB | pass; skip = Windows symlink |
| 6 | B | `tests.test_runtime_boundary` | 3 | 3 | 0 | 0 | 0 | 0 | 4.4 s | 4,154 → 4,154 → 4,328 | 64 MB | pass |
| 7 | B | `tests.test_git_guard` | 20 | 20 | 0 | 0 | 0 | 0 | 13.1 s | 4,532 → 4,146 → 4,181 | 52 MB | pass |
| 8 | B | `tests.test_phase6b_cli_verification` | 15 | 15 | 0 | 0 | 0 | 0 | 123.8 s | 4,272 → 4,073 → 4,457 | 95 MB | pass |
| 9 | B | `tests.test_pr_proof` | 12 | 12 | 0 | 0 | 0 | 0 | 4.4 s | 4,457 → 4,413 → 4,475 | 63 MB | pass |
| 10 | C | `tests.test_proof_discovery_ux` | 15 | 15 | 0 | 0 | 0 | 0 | 15.2 s | 4,462 → 4,335 → 4,461 | 92 MB | pass |
| 11 | C | `tests.test_proof_inspection_ux` | 32 | 30 | 0 | **2** | 0 | 1 | 45.1 s | 4,489 → 4,365 → 4,598 | 96 MB | **pre-existing baseline**, confirmed below |
| 12 | C | `tests.test_cli_runtime_unification` | 7 | 7 | 0 | 0 | 0 | 0 | 13.0 s | 4,525 → 4,414 → 4,532 | 79 MB | pass |
| 13 | C | `tests.test_cli_workspace_verification` | 7 | 7 | 0 | 0 | 0 | 0 | 15.1 s | 4,280 → 4,147 → 4,255 | 92 MB | pass |
| 14 | C | `tests.test_runtime_permissions` | 5 | 5 | 0 | 0 | 0 | 0 | 4.4 s | 4,299 → 4,254 → 4,501 | 62 MB | pass |
| | | **Total** | **187** | **183** | **0** | **2** | **2** | | ≈ 680 s | | | **no new failures** |

**Test counts are unchanged.** Every module ran the same number of tests as its pre-closure run (18, 8, 5, 29, 11, 3, 20, 15, 12, 15, 32, 7, 7, 5), so no test disappeared or was silently skipped.

**REPL baseline, established independently in this sign-off:**

- I exported `c78e798` with `git archive` into the session scratchpad, outside any worktree, and ran `python -m unittest tests.test_proof_inspection_ux -v` there.
- The result was also `Ran 32`, `FAILED (errors=2)`, with the same two errors:
  - `TestProofInspectionUX.test_11_repl_proof_aliases`
  - `TestVerifyProofCLISemantics.test_11_repl_proof_aliases`
- Both tracebacks are identical to the worktree run, frame by frame and including line numbers. The chain is `test_11_repl_proof_aliases` → `cli.run_interactive` → `mock._execute_mock_call` → `StopIteration`, raised by an exhausted mocked `console.input`.
- This matches the Phase 1C finding. Neither the tests nor the production code were modified to hide these errors.

**Platform skips (WinError 1314, symlink privilege not held):**

- `test_verifier_phase1c_blockers.TestSnapshotBinding.test_symlink_escape_not_followed`
- `test_verifier_docker_integration.TestDockerIntegration.test_symlink_in_repository_is_not_followed`

These scenarios are **not verified** on this host. A directory-junction test does not cover them. Validate them on Linux CI before merge or public release. No other evidence in this sign-off points to an exploitable path-handling defect.

**Phase 1D suites:** these were not re-run here. The final-tree results from the closure stand, because no source file changed afterwards (§E):

- 82/82 browser tests;
- `test_standalone_verifier`: 21;
- `test_verifier_execution_integrity`: 17.

## D. Security regression results

| Area | Result |
| :--- | :--- |
| Report checker (`--check-report`) | `test_verifier_evidence_integrity`: 18/18. These Phase 1B/1C compatibility tests check that valid reports still verify, that missing or modified evidence is detected, and that integrity failures return a nonzero exit code. Together with the Phase 1D evidence suite, which covers 8 malformed-report shapes, swapped screenshots and path escapes, the closure's checker change shows no regression. |
| Path traversal / evidence confinement | Phase 1D's `_bundle_file` tests pass, and suites 1 and 4 pass. The only gap is the two symlink skips (§C). |
| Command execution security | Suites 3, 5, 6 and 14 pass (sandbox execution, real Docker integration, runtime boundary, runtime permissions). `command_policy.py`, `sandbox.py` and `verification_harness.py` are unchanged by the closure. |
| Result semantics / Phase 1C blockers | Suites 2 and 4 pass. |
| Secret scanning | `test_git_guard`: 20/20. |
| Browser sandbox configuration | Unchanged and pinned. <ul><li>Seccomp profile sha256 is `569408de…4b39`, and it matches `BROWSER_SECCOMP_PROFILE_SHA256`.</li><li>`Dockerfile` is `b860f73e…884c`.</li><li>`requirements.lock` is `1654ad6f…89ef`.</li><li>The local image `brainfrog-browser-runner:v1.64.0` has ID `21d60e45…c0de`, which matches `APPROVED_BROWSER_IMAGES`.</li></ul> |
| Proof integrity | Suites 8–11 pass (Phase 6B CLI verification, PR proof, proof discovery, proof inspection), apart from the baseline REPL errors. |

**No new critical regression was found.**

## E. Workspace and Docker preservation

- **Protected worktrees.** `agentic_dev`, `brainfrog-vf-1b-closure` and `brainfrog-vf-1c-execution` are identical to the closure baseline, both before and after the 14 suites. This covers:
  - HEADs;
  - `git status --porcelain=v1 -uall`;
  - the per-file sha256 of every changed or untracked file;
  - the sha256 of each worktree's `git diff`: `75363392…2e21`, `ecf20f7b…deb0` and `04c64898…369b`.
- **Active worktree:**
  - **Status:** 157 entries. These are the 156 from the closure plus this report.
  - **No test writes:** no file in the worktree was created or modified by the test runs. I checked this against an mtime marker set before suite 1.
  - **Git hygiene:** `git diff --check` is clean apart from Git's informational CRLF warning for `cli.py`, and nothing is staged.
- **Docker:**
  - Container, image, network and volume inventories match the end-of-closure snapshot exactly.
  - No resource labelled `io.brainfrog.owner=brainfrog-verifier` remains.
  - The six unrelated containers stayed exited and untouched.
- **Left in place:** temp folders (including new `%TEMP%\bf_*` folders created by suites 4 and 5), `.brainfrog/workspace.lock` and historical reports were not deleted.

## F. Candidate commit manifest

The manifest in [phase1d-commit-readiness.md](phase1d-commit-readiness.md) was re-verified: **125/125 file hashes match** the files on disk.

| Class | Count | Contents |
| :--- | :--- | :--- |
| Production code | 11 | `core/verifier.py`, `core/runtime/browser_*`, `app_*`, `browser_seccomp_chromium.json`, `cli.py`, … |
| Tests and fixtures | 39 | |
| Documentation | 29 | |
| Dependency and image locks | 2 | `docker/browser-runner/Dockerfile`, `docker/browser-runner/requirements.lock` |
| Sanitized evidence | 44 | |

**Additions:** `phase1d-commit-readiness.md` itself, and this file.

**Exclusions, unchanged:**
- `.brainfrog/workspace.lock`;
- `.brainfrog/reports/**` (29 files from the earlier attempt);
- scratchpad report bundles and the clean-HEAD export;
- `%TEMP%\bf_*`;
- `__pycache__/`;
- `.vscode/`.

**Publication copies:** for the checkpoint commit, the absolute local worktree paths in 12 evaluation documents were replaced with `<REPOSITORY_ROOT>` / `<PHASE_1x_WORKTREE>` placeholders and relative links. This was a documentation-only edit, recorded in [phase1d-commit-readiness.md](phase1d-commit-readiness.md). The unsanitized originals remain in the protected Phase 1C worktree. At the operator's instruction, the commit also excludes `phase1d-live-evidence/`.

## G. Final recommendation

**`READY FOR CHECKPOINT COMMIT`**

All gates are met:

- all 14 suites completed;
- no new failures;
- the REPL errors are a proven baseline;
- the platform skips are disclosed;
- the Chromium sandbox and runtime pins are unchanged;
- the protected worktrees are unchanged;
- Docker is untouched;
- the manifest is re-verified;
- `git diff --check` is clean.

**Before committing** (AGENTS.md):

1. Create a dedicated branch from `c78e798`.
2. Stage only the manifest files.
3. Run `python -m security.git_guard --staged` on the staged set and review `git diff --cached`.

This verdict is not a commit, push, PR or merge. It does not imply production-grade safety for hostile websites.
