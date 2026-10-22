# Phase 1E — T1 Linux Portability Hotfix (remediation record)

**Status: T1 HOTFIX LOCAL VALIDATION — PASS** (2026-10-22), including the local Linux permission proof. Committed as the corrective commit `fix(verify): correct Linux harness access and CI platform checks` (a fast-forward child of `1c6ac7d`). The live GitHub retest is recorded separately; this record makes no claim about it.

An earlier revision of this record said PARTIAL: the Linux proof, the Linux runs and `test_standalone_verifier` were stopped by host memory pressure. They were run later the same day, once free memory was back above the 2 GiB floor (§4).

The original live run stays recorded as **T1 LIVE GITHUB VERIFICATION — BLOCKED** ([phase1e-t1-live-results.md](phase1e-t1-live-results.md), PR #45, run `39705364940`). That run produced a valid BLOCKED artifact, which is evidence of fail-closed behaviour. This record does not change its verdict.

## 1. Root causes (proven by the T1 GitHub-hosted Linux run)

| # | Defect | Effect on T1 |
| :- | :--- | :--- |
| 1 | `stage_harness()` staged the harness in a `mkdtemp` directory (0700, owned by the runner user, uid 1001). That directory itself was bind-mounted read-only at `/bf-harness` into a container running as uid 65532. Docker Desktop on Windows does not enforce bind-mount POSIX permissions, so this never failed locally | `Permission denied` in the container: CI-01 and CI-02 BLOCKED; 8 self-test failures (Docker integration and Phase 6B CLI) |
| 2 | `validate_verification_command` judged `..` in arguments with host-native path rules. On a Linux host, `..\..\windows\system32` is one literal filename. It also never rejected rooted, drive-qualified or UNC *argument* paths (only an absolute executable) | Self-test `test_05` failed on Linux |
| 3 | The trusted quarantine for the two `test_11_repl_proof_aliases` errors was platform-agnostic. The `StopIteration` errors reproduce on Windows only; on Linux both tests pass | The strict quarantine failed the self-test (an unexpected pass), as designed |

## 2. Fixes (base `1c6ac7d`)

**`core/runtime/sandbox.py`:** `stage_harness(container_readable=False)`.
- Only the OCI backend passes `True`. After writing, it sets the staged script to `0644` and the directory to `0755`.
- Both paths were just created inside the private `mkdtemp` directory, so nothing can be swapped in. No parent directory is touched and nothing is made group- or world-writable.
- The directory contains only the public harness.
- The local trusted backend keeps the private `0700` default, because it writes a secret nonce file next to the script.
- `0644` rather than `0444`: on Windows `0444` sets the read-only attribute, which would make the backend's `rmtree(ignore_errors=True)` cleanup silently leak the directory. uid 65532 is not the owner and the mount is read-only, so `0644` gives it no write access.
- Unchanged: container user `65532:65532`, `--read-only`, `cap-drop ALL`, `no-new-privileges`, `network none`, the read-only harness and source mounts, and the effective-config verification.

**`core/verifier.py`:** a new `_external_path_argument()` check runs before the existing host-native confinement check, which is kept. It treats `/` and `\` both as separators and rejects, on every host:
- any `..` component;
- rooted (`/x`, `\x`), UNC (`//h/s`, `\\h\s`) and drive-qualified (`C:\x`, `C:x`, `d:/x`) arguments;
- the same forms inside `--option=value` values.

Non-path arguments (`-k "a and not b"`, `pkg.mod..Name`, pytest node IDs, `--maxfail=1`) are unchanged.

**`core/ci_gate.py`:**
- `quarantine_for_platform()`: a quarantine entry applies only when it has an explicit, non-empty `platforms` list containing the runtime `sys.platform`. An entry without `platforms` grants nothing and is reported as a policy problem.
- `run_selftest(..., platform=sys.platform)` uses it.
- `runtime_platform()` (`sys_platform`, OS, release, machine, Python) is recorded in `selftest-summary.json` and in `ci-metadata.json`. It comes from the host, never from the event or the candidate.

**`.github/brainfrog/policy.json` (trusted):**
- the REPL quarantine gets `"platforms": ["win32"]` and an updated reference;
- the new self-test module is `tests.test_sandbox_harness_staging`;
- two new `required_passing_tests` (a skip fails CI):
  - `test_verifier_docker_integration…test_staged_harness_is_readable_but_not_modifiable_by_container_user`;
  - `test_sandbox_harness_staging…test_container_staging_is_readable_by_other_and_writable_by_nobody_else`.

  That gives 17 required tests in total.
- **Revised policy digest** (LF bytes, as CI hashes it): `475a1446b1e5637f2f30ef17acdd4ebbbdc3ce60b205683de571a4f2cc8f1c65`, replacing `4a44e37a…`, the value recorded by T1.

## 3. New regression tests

| Test | What it proves |
| :--- | :--- |
| `tests/test_sandbox_harness_staging.py` (new; 5 tests, 2 POSIX-only) | Staged content equals the trusted harness and its sha256; the OCI staging directory holds only the harness; modes are 0755/0644, not group- or world-writable, and owned by the stager; default staging stays 0700; the backend cleanup removes it |
| `test_verifier_docker_integration.test_staged_harness_is_readable_but_not_modifiable_by_container_user` | Through the real `DockerSandboxBackend`: the candidate's own test, run *by the harness* as uid 65532, reads the harness and cannot append to, truncate, plant beside, chmod, delete or rename it. The criterion PASSes with `user 65532:65532` and the harness mount `rw: false` |
| `test_verifier_execution_integrity.test_05b_path_arguments_are_judged_the_same_on_every_host` | 13 rejected forms (POSIX, Windows and mixed traversal; bare `..`; option-value traversal and absolute paths; POSIX absolute; Windows rooted; drive-qualified, drive-relative and forward-slash drive; UNC; `//` UNC-style) and 8 accepted legitimate commands. `test_05` is unchanged |
| `test_ci_gate`: 3 quarantine-policy tests and `TestSelftestRunPlatforms` (4 tests running real `python -m unittest` subprocesses) | Windows tolerates only the exact signature-matched baseline. Linux fails on those errors and passes when the tests pass. A different error type on Windows is rejected. Entries without explicit platforms grant nothing. The runtime platform is recorded. The committed policy is `win32`-only |

## 4. Validation actually executed (Windows host, bounded runner `run_one_v2.ps1`, sequential)

| Suite | Result | Notes |
| :--- | :--- | :--- |
| `tests.test_sandbox_harness_staging` | exit 0. 5 results: **3 ok, 2 skipped** (POSIX-only) | Windows. The unittest summary says "Ran 3": the local Python 3.12.1 leaves decorator-skipped tests out of that count. GitHub's 3.12.15 counts them (T1: "Ran 29" including the skipped junction test) |
| `tests.test_ci_gate` | exit 0, **60/60** | |
| `tests.test_ci_workflow_security` | exit 0, **17/17** | |
| `tests.test_verifier_execution_integrity` | exit 0, **18/18** (incl. `test_05`, `test_05b`) | Windows host semantics |
| `tests.test_verifier_sandbox_execution` | exit 0, **5/5** | local backend, private staging |
| `tests.test_verifier_docker_integration` | exit 0, 12 run, 1 skipped (Windows symlink privilege) | The new harness test is **ok on Docker Desktop**. That is not Linux proof: Docker Desktop does not enforce bind-mount permissions |
| `tests.test_verifier_phase1c_blockers` | exit 0, 29 run, 1 skipped (symlink privilege) | |
| `tests.test_phase6b_cli_verification` | exit 0, **15/15** | |
| `tests.test_standalone_verifier` | exit 0, **21/21** | First attempt refused by the memory gate (1695 MB); run later at 2104 MB, minimum 1961 MB |

**Memory.** Free host memory fell repeatedly because of other host applications:
- during `docker_integration`: a minimum of 1615 MB;
- during `phase1c_blockers`: a minimum of 1101 MB;
- during `phase6b`: a minimum of 350 MB.

A bounded 10-minute wait at 10:07–10:17 ended at 744 MB, so execution stopped there. Every run in this table started after the last source edit (09:35).

## 5. Local Linux validation (run later, at 2353 MB free)

The Linux kernel enforces real POSIX permissions inside the Docker Desktop VM on an ext4 named volume, unlike Windows bind mounts, which are **not** accepted as proof. This host has no user Linux distribution with its own Docker, so this is the closest local substitute. All resources were labeled and disposable; the volume set was identical before and after, with 0 containers left.

**Fix 1: harness permission proof** (session scratchpad scripts `linux_harness_proof.sh` and `stage_on_linux.py`, not committed):
- the real `stage_harness()` staged as uid 1001. The old default is `0700`; the hotfix staging is `0755`, with the file `0644`, owned by 1001; the sha256 is identical;
- the exact staging directory was mounted read-only (`volume-subpath`) at `/bf-harness` in `python:3.12-slim`, using the backend's flags (`--read-only`, `cap-drop ALL`, `no-new-privileges`, `network none`) as uid 65532.

| Case | Result |
| :--- | :--- |
| A. pre-hotfix staging | `can't open file '/bf-harness/verification_harness.py': [Errno 13] Permission denied`, exit 2: reproduces the T1 defect |
| B. hotfix, CI-01 unit tests | 2 tests OK, exit 0 |
| C. hotfix, CI-02 compileall | exit 0 |
| D. hotfix, candidate tries to modify the harness | append, truncate, plant, chmod, delete and rename all raised `OSError`; uid/gid 65532; exit 0 |
| E. hotfix, a failing test | FAIL, exit 1: failure is still reported |

**Fixes 2 and 3, plus staging, on Linux** (`brainfrog-browser-runner:v1.64.0` userland, Python 3.12.3, uid 65532, read-only root, `network none`): **25/25 OK**. The run covered:
- `test_05` and `test_05b`;
- `TestSelftestQuarantine` and `TestSelftestRunPlatforms` (real subprocess runs);
- `TestTrustedPolicy`, including the byte-exact policy digest;
- all 5 `test_sandbox_harness_staging` tests, with the 2 POSIX tests executed rather than skipped.

An earlier attempt in `python:3.12-slim` errored in the 5 tests that need `git`, which that image lacks; the other 20 passed.

**Still only provable on GitHub:** the full `DockerSandboxBackend` run on a Linux host, with harness staging on the runner's own `/tmp` (the T1 retest).

## 6. T1 retest plan (each step needs separate authorization; none was executed)

| Item | Value |
| :--- | :--- |
| Current remote base | `feat/verification-first-1e-ci` = `1c6ac7dfd88013612efeb7bc9e27fad06bfa4cf3` |
| Corrective commit | `fix(verify): correct Linux harness access and CI platform checks`; its SHA is recorded in the retest report |
| Intended new remote base | `<corrective commit>`: a fast-forward child of `1c6ac7d` |
| Files changed | `core/runtime/sandbox.py`, `core/verifier.py`, `core/ci_gate.py`, `.github/brainfrog/policy.json`, `tests/test_ci_gate.py`, `tests/test_verifier_docker_integration.py`, `tests/test_verifier_execution_integrity.py`, `tests/test_sandbox_harness_staging.py` (new), this record. The workflow file is unchanged, so the actionlint and zizmor results still apply |
| Updated trusted policy digest | `475a1446b1e5637f2f30ef17acdd4ebbbdc3ce60b205683de571a4f2cc8f1c65` |
| PR #45 relationship | head `test/brainfrog-1e-t1-pass` = `ce89f8c` (parent `1c6ac7d`). After the base moves, GitHub's merge commit becomes `merge(<new base>, ce89f8c)`. The T1 branch needs no change |

**Rollout:**
1. Create the reviewed corrective commit (§4 and §5 complete).
2. Fast-forward-push `feat/verification-first-1e-ci` to it.
3. Trigger a **fresh** `pull_request` run for PR #45. Pushing the base fires none; the operator can close and reopen PR #45, or push an empty commit to the T1 head. **A re-run of `39705364940` does not count**: it re-tests the old merge commit with the old trusted base.
4. Confirm `trusted_verifier_revision` = the new base, `trusted_policy_digest` = `475a1446…`, and `runtime_platform.sys_platform` = `linux`. Confirm CI-01 to CI-06 PASS and the Linux self-test passes, including the 17 required tests and both REPL alias tests passing.
5. Download the evidence and verify it independently (manifest digest, `verify_evidence_bundle`, `--check-report`).
