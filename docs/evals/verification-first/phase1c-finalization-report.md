# BrainFrog Verification-First Phase 1C — Finalization Report

**Date:** 2026-10-20
**Worktree:** `<PHASE_1C_WORKTREE>` (detached HEAD `c78e798`; all changes uncommitted)
**Supersedes:** the verdict in `phase1c-final-report.md` (`PARTIAL — IMPLEMENTED BUT REAL OCI VALIDATION BLOCKED`)
**Companion evidence:** `phase1c-live-oci-validation.md`, `phase1c-live-evidence/`

## Verdict

**DONE — REAL OCI VERIFICATION PROVEN**, within the threat model stated in §7.

Real checks ran in hardened, inspected containers created from a resolved immutable image ID, through BrainFrog's own CLI and backend. All of the following were observed live, not simulated:
- genuine PASS, FAIL, zero-test and forged-output outcomes;
- infrastructure-failure classification;
- snapshot identity binding;
- cleanup and ownership checks.

This verdict means *the acceptance commands in a spec were executed and their results interpreted faithfully*. A PASS does **not** prove that every human-written requirement in a spec is satisfied; it proves the stated check ran against the recorded snapshot and the structured runner reported success.

---

## 1. Environment

Docker Engine `29.1.3`, context `desktop-linux`, runtime `runc`, cgroup v2, seccomp builtin.
Approved image `python:3.12-slim` resolved to `sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1` (`linux/amd64`), present locally, never pulled.
No Docker Desktop settings were changed.

## 2. Blockers and fixes

### P0-1 — Zero-test and forged-output false PASS (`TestResultAdapter`)

**Defect:** PASS was inferred from `Ran N tests` / `OK` text that repository code can print. An unsupported runner exiting 0 was PASS.

**Fix:**
- New BrainFrog-owned harness `core/runtime/verification_harness.py` (stdlib only). It runs `unittest` through `unittest.TestProgram` and compiles sources with `compile()` (it never imports them).
- The harness emits exactly two JSON records, `started` and `complete`, to a private dup of stdout. Each carries a 256-bit nonce supplied on stdin (container) or in a file deleted on read (host).
- The `complete` record carries discovered, tests_run, failures, errors, skipped, expected/unexpected successes, interrupted and was_successful. For compile checks it carries files compiled, errors and missing targets.
- `TestResultAdapter` derives verdicts only from nonce-matching records. Without a record, text can support FAIL but never PASS.

Verdict rules:

| Condition | Verdict |
| :--- | :--- |
| Any line carrying the record marker with the wrong nonce, or duplicate records | BLOCKED `forged_runner_record` |
| No `started` record | BLOCKED `runner_not_started` (or `output_truncated`) |
| Started, no `complete`, exit ≠ 0 | **FAIL** (runner aborted after repository code began executing) |
| Started, no `complete`, exit 0 | BLOCKED `incomplete_runner_result` (e.g. `os._exit(0)`) |
| failures + errors + unexpected successes > 0 | FAIL |
| tests_run = 0 | BLOCKED (zero tests) |
| All tests skipped | BLOCKED |
| Record says success but exit ≠ 0, or output truncated | BLOCKED |
| Runner-observed source digest ≠ materialized digest | BLOCKED `snapshot_identity_mismatch` |
| Unsupported runner (anything other than `python -m unittest\|compileall\|py_compile`) | BLOCKED `unsupported_runner`, **not executed** |

Section-3 cases, all exercised with the real harness (`tests/test_verifier_phase1c_blockers.py`):

| Case | Result |
| :--- | :--- |
| A — real passing test | PASS, test_count = 1 |
| B — real failing test | FAIL, test_count = 1 |
| C — zero tests | BLOCKED, test_count = 0 |
| D — fake `Ran 999 tests` / `OK` | BLOCKED |
| D2 — forged record without nonce | BLOCKED `forged_runner_record` |
| E — fake success text, then a failing test | FAIL |
| F1 — `os._exit(0)` mid-run | BLOCKED |
| F2 — `os._exit(3)` mid-run | FAIL |
| G — 3 MiB of output | truncated, not PASS |
| H — `pytest` | BLOCKED, never executed |

The same outcomes occurred in real containers (AC-01…AC-05).

### P0-2 — Container security controls (`DockerSandboxBackend`)

**Defect:**
- `--read-only` was missing and `--user` was optional.
- Mounts used an ambiguous `-v`.
- There was no `--pull=never`, and the mutable tag was used.
- No effective-configuration check was made.
- `is_available()` called `run_hardened_subprocess` without its required `cwd` and with a command policy that does not allow `docker`, so it could never report Docker as available. This was found during this phase.

**Fix:**
- Fixed `docker create` vector with all required controls, plus `--memory-swap`, `--ipc=private`, `--log-driver=none` and ownership labels.
- `--mount type=bind,…,readonly` for the snapshot and for a per-run copy of the harness. No other bind mounts.
- tmpfs `/workspace` and `/tmp` owned by uid/gid 65532, `noexec,nosuid,nodev`.
- Image resolved to its ID and executed by ID. `--pull=never`.
- `docker inspect` is verified **before start**, covering user, network, read-only rootfs, privileged, no-new-privileges, CapDrop/CapAdd, PID/IPC/UTS modes, memory, NanoCpus, PidsLimit, devices, mount set and read-only flags, Docker socket, tmpfs set and environment keys. Any deviation → BLOCKED `security_policy_not_enforced`.
- `SandboxPolicy` with any weakened field is refused (`policy_weakening_refused`). Only allowlisted image tags are accepted. The spec has no route to policy, image, limits or Docker arguments.
- The Docker CLI runs with a minimal allowlisted environment and bounded output. Nothing from the host environment is forwarded into containers.
- Removal is ownership-checked and runs in `finally`, so it covers timeout and `KeyboardInterrupt`. A cleanup failure → BLOCKED.

### P0-3 — Executed snapshot identity (`materialize_source_snapshot` ↔ report)

**Defect:** the materialized digest was discarded, and the live-workspace digest (a different quantity) was passed as the executed snapshot. Snapshot errors were swallowed. The snapshot was built in the staging report directory.

**Fix:**
- `materialize_source_snapshot_with_manifest()` lists files with `git ls-files -z --cached --others --exclude-standard`. This covers tracked files with staged and unstaged edits, untracked files, and never copies ignored files.
- It also excludes credential-like files, caches, `.git` and BrainFrog reports.
- It rejects any path through a symlink or junction.
- It raises on listing or read errors.
- It re-reads every live file after copying and raises if any byte changed during the copy.
- Its digest is computed from the exact bytes written and is re-verified over the target directory.
- The snapshot lives in a fresh `%TEMP%\bf_snapshot_*` directory, is removed after the run, and its manifest is kept as hashed run evidence.
- Inside the container the harness re-hashes `/source` while copying it to `/workspace` and reports `source_digest`. PASS requires that digest to equal the materialized digest.
- After execution the host re-hashes the snapshot; any change → BLOCKED `snapshot_mutated_during_execution`.
- The report now records four distinct fields: `repository_head_sha`, `live_workspace_pre_snapshot_digest`, `materialized_source_snapshot_digest` and `live_workspace_post_snapshot_digest`. Pre/post drift still forces overall BLOCKED. `source_snapshot_digest` is kept only as a documented compatibility alias of the *pre* digest.
- `--check-report` additionally verifies run-level evidence, the `report.md` hash, and that every PASS is bound to the report's materialized digest.
- The trusted-local backend uses the same snapshot + harness path, so both backends bind identically.

### P1 — Infrastructure failure misclassified as code failure

Machine-readable `infrastructure_error_category` on every criterion. All of the following are BLOCKED and never trigger host execution:
- `docker_unavailable`
- `image_missing` (no pull)
- `image_not_approved`
- `container_create_failed` (e.g. invalid runtime limit)
- `mount_unavailable`
- `security_policy_not_enforced`
- `policy_weakening_refused`
- `container_start_failed` / `toolchain_missing` (exit 125/126/127 before the harness starts)
- `timeout`
- `resource_limit_termination` (OOMKilled, or exit 137)
- `container_cleanup_failed`
- `runner_not_started`
- `incomplete_runner_result`
- `output_truncated`
- `snapshot_error`
- `snapshot_identity_mismatch`
- `snapshot_mutated_during_execution`
- `unsupported_runner`
- `runner_usage_error`

**Semantic change:** a timeout is now BLOCKED (`timeout`), not FAIL. Phase 1B `test_10` was rewritten to use a *real* stalled test instead of a mock, and asserts the new semantics.

### Additional defects found and fixed in this phase

1. **CLI never used the sandbox.** `--sandbox auto` was the documented default, but the CLI passed `trust_local_code=False`, which forced `UNTRUSTED_FAIL_CLOSED`. It now passes `None` unless `--trust-local-code` is given. Trust precedence: explicit trust mode > `--trust-local-code` > `--sandbox auto|docker` > fail closed.
2. **Malformed spec crashed with a traceback and exit 1** (indistinguishable from FAIL in CI). It is now `BLOCKED: cannot verify specification: …` with exit 2.
3. **Phase 1B `test_19` ran `cli.py --help` inside the protected `agentic_dev` checkout.** It now targets the worktree under test.
4. **Phase 1C tests assumed the Docker engine was offline.** They now inject a deterministically unavailable backend (`sandbox_backend=` parameter, operator/API only), so they hold on any host.

## 3. Real CLI demonstration (summary)

Full transcripts: `phase1c-live-evidence/transcripts/`. Copied report directories: `phase1c-live-evidence/reports/`. Details: `phase1c-live-oci-validation.md` §4.

| Run | Exit | Overall | Key observations |
| :--- | :--- | :--- | :--- |
| Default `--spec spec.md` (OCI) | 1 | FAIL | AC-01 PASS (1 test), AC-02 FAIL (1/1), AC-03 BLOCKED (0), AC-04 BLOCKED (forged text), AC-05a PASS, AC-05b FAIL. Six distinct containers. All observed digests = `f93a5d9a…`. |
| `--sandbox docker`, all-pass spec | 0 | PASS | real containers |
| Docker CLI absent from `PATH` | 2 | BLOCKED | `docker_unavailable`, no host fallback |
| `--sandbox none` | 2 | BLOCKED | fail closed |
| `--trust-local-code` | 1 | FAIL | identical verdict matrix on host |
| Malformed spec | 2 | – | clean BLOCKED message |
| `--check-report` on each report | 0 | – | integrity verified |
| `--check-report` on tampered copy | 1 | – | hash mismatch detected |

The disposable repository was byte-identical before and after all runs.

## 4. Test results (exact, final run)

| Suite | Ran | Pass | Fail | Error | Skip |
| :--- | --: | --: | --: | --: | --: |
| `tests.test_standalone_verifier` | 21 | 21 | 0 | 0 | 0 |
| `tests.test_verifier_execution_integrity` | 17 | 17 | 0 | 0 | 0 |
| `tests.test_verifier_evidence_integrity` | 18 | 18 | 0 | 0 | 0 |
| `tests.test_verifier_test_result_semantics` | 8 | 8 | 0 | 0 | 0 |
| `tests.test_verifier_sandbox_execution` | 5 | 5 | 0 | 0 | 0 |
| **Previous 69-test baseline** | **69** | **69** | 0 | 0 | 0 |
| `tests.test_verifier_phase1c_blockers` (new) | 29 | 28 | 0 | 0 | 1 ¹ |
| `tests.test_verifier_docker_integration` (new, **real Docker**) | 11 | 10 | 0 | 0 | 1 ¹ |
| `tests.test_runtime_boundary` | 3 | 3 | 0 | 0 | 0 |
| `tests.test_git_guard` | 20 | 20 | 0 | 0 | 0 |
| `tests.test_phase6b_cli_verification` | 15 | 15 | 0 | 0 | 0 |
| `tests.test_pr_proof` | 12 | 12 | 0 | 0 | 0 |
| `tests.test_proof_discovery_ux` | 15 | 15 | 0 | 0 | 0 |
| `tests.test_proof_inspection_ux` | 32 | 30 | 0 | **2** ² | 0 |
| `tests.test_cli_runtime_unification` | 7 | 7 | 0 | 0 | 0 |
| `tests.test_cli_workspace_verification` | 7 | 7 | 0 | 0 | 0 |
| `tests.test_runtime_permissions` | 5 | 5 | 0 | 0 | 0 |
| pytest: `test_phase2a_step4a_subprocess`, `test_phase4_proof`, `test_phase6c_proof_verifier`, `test_reproducible_proof_demo` | 58 | 58 | 0 | 0 | 0 |

¹ **Symlink escape test** skipped: creating symlinks on this Windows host fails with `WinError 1314` (privilege not held). The equivalent junction-escape test ran and passed.

² **`test_11_repl_proof_aliases` (×2)** errors with `StopIteration` from an exhausted mocked `console.input` in `run_interactive`. **The same 2 errors occur on a pristine `git archive HEAD` export**, so they predate and are unrelated to this phase. They were not modified.

Changes to existing tests (semantic tightening, no weakened assertion):
- `test_verifier_test_result_semantics`: tests 02, 05 and 07 now assert that free-form text and unsupported runners do **not** PASS; test 06's reason text was updated.
- `test_verifier_sandbox_execution`: tests 01, 02 and 05 now assert the stronger hardened vector and use an injected unavailable backend.
- `test_verifier_execution_integrity`: test 10 uses a real timeout and expects BLOCKED `timeout`; test 15 uses an injected unavailable backend.
- `test_standalone_verifier` 19: targets this worktree.

**Hygiene:**
- `git diff --check` → clean, exit 0. Trailing whitespace was also removed from two untracked Phase 1C test files that `diff --check` does not cover.
- **Secret scan of all actual changes, including untracked files:** `python -m security.git_guard --file` over all 111 modified and untracked files → **0 findings**. A positive control on a synthetic credential-shaped file was detected (exit 1), which confirms the scanner is effective. A `--staged` scan alone would not have covered these files because nothing is staged.

## 5. Safety confirmations

- **Protected worktrees.** `agentic_dev` and `brainfrog-vf-1b-closure` remain at `c78e798`, and their status and SHA-256 of every modified or untracked file match the hashes recorded before any edit.
- **Unrelated Docker resources.** The container, image, volume and network listings match the pre-run baseline exactly (6 pre-existing stopped containers untouched). Zero containers carry `io.brainfrog.owner` after the run. No prune, stop, or removal of foreign resources was performed; the only removed container was a decoy created and labelled by the ownership test itself.
- **Pulls.** None. `python:3.11-slim` was still absent after the missing-image test.
- **Temp directories.** No leftover `bf_snapshot_*`, `bf_harness_*` or `bf_local_ws_*` directories.
- **Git operations.** No commit, push, stash, reset, rebase or PR. No new worktree.

## 6. Completion gate

| # | Gate | Status |
| :-- | :--- | :--- |
| 1 | Docker Engine accessible | ✅ 29.1.3 |
| 2 | Approved local image resolved and executed | ✅ by ID `sha256:a6e3…11b1` |
| 3 | Real checks execute inside containers | ✅ |
| 4 | Secure configuration enforced | ✅ verified via inspect before start, and in-container probes |
| 5 | Original repository unchanged | ✅ byte comparison |
| 6 | Genuine passing tests → PASS | ✅ |
| 7 | Genuine failing assertions → FAIL | ✅ |
| 8 | Zero-test suites never PASS | ✅ |
| 9 | Forged runner text cannot produce PASS (threat model §7) | ✅ |
| 10 | Container failures → BLOCKED | ✅ unavailable, missing image, bad limit, timeout, OOM, cancellation |
| 11 | No unsafe host fallback | ✅ |
| 12 | Executed snapshot identity matches evidence | ✅ in-container digest = materialized digest |
| 13 | Evidence manifests pass integrity verification | ✅ tampering detected |
| 14 | No unrelated Docker resources affected | ✅ |
| 15 | Phase 1B regression tests pass | ✅ |
| 16 | Phase 1C targeted tests pass | ✅ (1 environment-dependent skip) |
| 17 | Real Docker integration tests pass | ✅ (1 environment-dependent skip) |
| 18 | Protected worktrees unchanged | ✅ |
| 19 | No commit, push or PR | ✅ |

## 7. Limitations (stated conservatively)

1. **In-process forgery.** Test code shares the harness interpreter. Adversarial code that introspects harness memory (e.g. via `gc`) could recover the nonce and forge a passing record. The supported threat model covers printed fake runner text, fake records without the nonce, early exits and crashes. It does not cover a repository deliberately attacking the verifier from inside the test process. Closing this would need an out-of-process result channel (e.g. a separate supervising process that reads results over a pipe the test process cannot reach).
2. **Containers are not a hard security boundary.** Kernel or runtime escapes defeat all controls. Docker Desktop's VM adds distance but is not hardened against adversarial code.
3. **Readable inputs.** Everything that is tracked, or untracked but not ignored, is visible inside the sandbox by design. Sensitive files that are neither ignored nor matched by the exclusion list would be copied.
4. **Snapshot TOCTOU.** Copy-time verification detects changes *during* materialization, and pre/post live digests detect net drift. A file changed and restored between those observations is not detected, and the executed bytes are still exactly what the materialized digest records.
5. **Supported runners.** Only `python -m unittest`, `python -m compileall` and `python -m py_compile` can PASS. `pytest`, `npm`, `cargo`, `go` and `make` remain BLOCKED `unsupported_runner` until structured adapters exist. Per-criterion working directories are unsupported (BLOCKED).
6. **Reports are tamper-evident, not immutable.** Hashes are recorded inside the same unsigned local `report.json`. Consistent rewriting of both evidence and manifest is not detected.
7. **Trusted-local mode** inherits the host environment through the pre-existing `run_hardened_subprocess` (which merges `os.environ`). It is operator-authorized, unisolated execution, and is documented as such.
8. **Windows symlink path** not exercised live, for lack of privilege; junctions were.
9. **Evidence paths** in copied reports contain local absolute paths (user profile temp directories).

## 8. Files changed (uncommitted)

- `core/runtime/sandbox.py` — rewritten backend, adapter, snapshot builder and infrastructure taxonomy
- `core/runtime/verification_harness.py` — **new**, BrainFrog-controlled in-sandbox harness
- `core/verifier.py` — trust resolution, snapshot binding, structured outcomes, run evidence, report.md hash, integrity checks, console output
- `cli.py` — sandbox actually selected by default; malformed spec → exit 2
- `core/runtime/command_policy.py` — unchanged in this phase (pre-existing compileall/py_compile allowance)
- `tests/test_verifier_phase1c_blockers.py`, `tests/test_verifier_docker_integration.py` — **new**
- `tests/test_verifier_sandbox_execution.py`, `tests/test_verifier_test_result_semantics.py`, `tests/test_verifier_execution_integrity.py`, `tests/test_standalone_verifier.py` — updated as described in §4
- `docs/evals/verification-first/phase1c-live-oci-validation.md`, `phase1c-finalization-report.md`, `phase1c-live-evidence/` — **new**
- `docs/evals/verification-first/phase1c-final-report.md` — superseded notice added

Phase 1D (browser verification) was not started.
