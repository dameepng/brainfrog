# Phase 1E — Local Validation

All runs are **local**: a Windows 11 host with Docker Desktop, plus a disposable Linux container. **No GitHub-hosted execution has happened.** Times are local (UTC+07:00).

Tree under test: worktree `brainfrog-vf-1e-ci`, branch `feat/verification-first-1e-ci`, base `41c6f0fca5a2a8f464c98bf85de06053f7f9eb45`. Changes are uncommitted.

Every source file under test was last modified at **2026-10-22 00:03:01**, and every result below was produced after that, except the rows marked *prior*.

## 1. Results

| # | Suite / check | Where | When | Command | Result | Duration |
| :- | :--- | :--- | :--- | :--- | :--- | :--- |
| 1 | `tests.test_ci_gate` | Windows | 10-22 00:06:58 | `python -m unittest tests.test_ci_gate -v` | exit 0, **53 run, 53 ok** | 17.4 s |
| 2 | `tests.test_ci_workflow_security` | Windows | 10-22 00:07:03 | `python -m unittest tests.test_ci_workflow_security -v` | exit 0, **17 run, 17 ok** | 2.1 s |
| 3 | `tests.test_ci_linux_symlinks` | Windows | 10-22 00:07:08 | as above | exit 0, 13 run, **13 skipped** (no symlink privilege). **Not counted as verification**; see row 4 | 2.1 s |
| 4 | `tests.test_ci_linux_symlinks` + `tests.test_ci_gate` + `tests.test_ci_workflow_security` | **Linux container** (see §3) | 10-22 06:40:51 | `python3 /harness/run_linux_tests.py tests.test_ci_linux_symlinks tests.test_ci_gate tests.test_ci_workflow_security` | exit 0, **83 run, 83 ok**: the **13 symlink tests executed and passed, 0 skipped**, plus 53 + 17. Only `anthropic` and `requests` were stubbed | 4.0 s (7 s wall) |
| 5 | `tests.test_ci_gate_live` (real Docker and Chromium E2E: Cases A with G-tamper, B, C, D, F, H, I, and the unsupported profile) | Windows + Docker | 10-22 00:04:47 | `python -m unittest tests.test_ci_gate_live -v` | exit 0, **8 run, 8 ok**. Run 1 (00:01) failed 4 + 3 errors because of two harness and gate defects, both fixed before run 2 (see §4) | 79.2 s; min available 2714 MB |
| 6 | `tests.test_standalone_verifier` | Windows | 10-22 00:07:10–00:09:53 | `python -m unittest tests.test_standalone_verifier -v` | **21 run, OK**, including `test_17_path_traversal_is_denied`. Taken from the unittest log; the stalled runner never wrote a result record (see §2) | 160.8 s (unittest) |
| 7 | `tests.test_verifier_evidence_integrity` | Windows | 10-22 06:44:21 | runner v2, `-TimeoutSec 420` | exit 0, **18 run, 18 ok** | 172.8 s; min available 2719 MB |
| 8 | Checkpoint-verifier defect proof | Linux container | 10-21 ~23:58 | row 4 harness with `41c6f0f:core/verifier.py` mounted over the 1E file | The absolute `<root>/../outside/spec.md` test **fails** against the checkpoint verifier (the spec was read and parsed), and passes against the 1E verifier (row 4) | — |

**Prior results (not re-run against the 1E tree):**
- The Phase 1D regression sign-off of **2026-10-21 18:24–18:39** covered 14 suites and 187 tests: 183 ok, 2 baseline REPL errors, 2 Windows symlink skips.
- It ran against the Phase 1D tree, which became checkpoint `41c6f0f`, **before** the 1E change to `core/verifier.py`.
- It is the source for the baseline-error characterization only. It does **not** validate the 1E tree.

## 2. Pending local regressions (as of 2026-10-22 06:48; resolved by the sign-off in §7)

These suites import `StandaloneVerifier`, and Phase 1E changed `core/verifier.py` (the `spec_root` parameter and resolved-path confinement). At the time of writing they had **not** been re-run against the 1E tree. All of them were run in §7:

| Suite | Status |
| :--- | :--- |
| `test_verifier_execution_integrity` | pending |
| `test_verifier_test_result_semantics` | pending |
| `test_verifier_sandbox_execution` | pending |
| `test_verifier_phase1c_blockers` | pending |
| `test_verifier_docker_integration` | pending |
| `test_verifier_browser_plan` | pending |
| `test_verifier_browser_security` | pending |
| `test_verifier_browser_evidence` | pending |
| `test_verifier_browser_integration` | pending |
| `test_phase6b_cli_verification` | pending |
| `test_cli_workspace_verification` | pending |
| `test_runtime_boundary`, `test_git_guard` | pending (they do not touch the changed code) |
| `test_proof_inspection_ux` (baseline REPL errors) | pending on the 1E tree |

**Expected impact:**
- When `spec_root` is not passed, it defaults to `repo_dir`.
- The only behavioural change is that absolute or symlinked spec paths resolving **outside** the root are now refused. In-root paths are unaffected, and suites 6 and 7 pass.
- This is an expectation, **not** a result.

**Other pending checks:**
- **Workflow lint:** `actionlint` and `zizmor` are not installed, and installing them was not authorized. The workflow is checked only by `test_ci_workflow_security` (strict YAML-subset parse plus 17 contract tests) and by manual review (§7). Status: **pending**.
- **Live GitHub execution:** **pending**; see [phase1e-remote-rollout.md](phase1e-remote-rollout.md).

## 3. Linux environment used

- **Image:** the existing local `brainfrog-browser-runner:v1.64.0` (Ubuntu 24.04, Python 3.12.3). Nothing was pulled.
- **Container flags:** `--pull=never --network none --read-only --cap-drop ALL --security-opt no-new-privileges --user 65532:65532 --memory 1g --pids-limit 256 --tmpfs /tmp`.
- **Mounts:** the worktree read-only at `/src`, plus a read-only harness directory.
- **Time limits:** `timeout 300` on the Docker CLI and `timeout 240` on Python inside; the container is named so it can be force-removed.
- **Harness** (session scratchpad, not committed): it stubs third-party roots that are missing from the image. *Using* a stub raises an error, so a stub can never make a test pass. `psutil` is never stubbed.
- **Result:** 0 containers left with the `io.brainfrog.owner` label.

## 4. Defects found by local validation and fixed

| Found by | Defect | Fix |
| :--- | :--- | :--- |
| Linux run, row 8 | `verify_spec` accepted an absolute `<root>/../x` spec path, a pre-existing confinement gap | Resolve, then `relative_to(spec_root)` |
| Live run 1 | The gate's reduced spec dropped criterion IDs | Fixed in `test_ci_gate_live._section` |
| Live run 1 | Key-name redaction flagged the `authorization_reference` key, so a clean bundle was withheld | Value-based `contains_secret_values`, plus a unit test |
| Live run 1 | The header regex missed the JSON form `{"authorization": …}` | Regex widened, plus a unit test |
| Live run 1 | The temporary sim directory was left behind (read-only git objects) | `_rmtree` with a chmod `onerror` |

## 5. Regression-runner stall (2026-10-22): root cause and fix

**What happened:**
- An 18-suite sequential batch was started at 00:06:3x.
- Three runner result records were written: rows 1–3.
- The fourth suite (`test_standalone_verifier`) **finished its tests at 00:09:53 with OK**, but the runner never wrote its record and never started suite 5.
- The batch sat idle until the operator stopped it at about 06:39.

**After the stop:**
- No `python.exe` from the run remained.
- No Docker container, network or volume with a BrainFrog label remained.

**What it was not:**
- **Not the memory gate.** `run_seq.ps1` checked memory once per suite and *stopped* (it did not wait) under 2048 MB, which would have printed a message. Available memory was about 2.8–3.0 GB.
- **Not a prompt.** Every command was non-interactive, and `Stop-Process` used `-Confirm:$false`.

**Cause: unbounded blocking calls in the monitor** (`run_one.ps1`):
- Each poll called `Get-Counter '\Memory\Available MBytes'` and `Get-CimInstance Win32_Process`, both with **no timeout**.
- After the loop, it called `$p.WaitForExit()` with **no timeout**.
- The 1200 s per-suite deadline was only checked *between* those calls, in the same thread. Once any of them blocked, the deadline could never fire.
- **Which call blocked is not proven:** a hung perf-counter or WMI query, or the unbounded `WaitForExit()` after a kill attempt.

**Fix:** `run_one_v2.ps1` (session scratchpad; it replaces `run_one.ps1` and `run_seq.ps1`, which are kept unchanged for the record):
- no `Get-Counter`; every CIM query uses `-OperationTimeoutSec 5`, and a failed query only skips that sample;
- the child is awaited only through `WaitForExit(2000)` in a loop that checks the hard deadline (default 420 s) every turn;
- on the deadline: `taskkill /T /F` on the child tree, then a bounded `WaitForExit(10000)`; the record says `timed_out`;
- **bounded memory gate:** it waits at most 60 s for ≥ 2048 MB, otherwise it records a memory stop and does not start the suite;
- a progress heartbeat every 15 s to `<module>-v2.progress.log`;
- foreground calls are also capped by the tool's own timeout.

**Timeout path tested** (10-22 06:44): `test_ci_gate` with `-TimeoutSec 5` was killed at 6.7 s. The record shows `timed_out: true`, and 0 `python.exe` processes remained.

## 6. Hygiene and integrity (10-22 06:45–06:48)

- **Protected worktrees:**
  - Covered: `agentic_dev`, `brainfrog-vf-1b-closure`, `brainfrog-vf-1c-execution` and `brainfrog-vf-1d-browser`.
  - HEAD, branch, porcelain status, per-file sha256 and the `git diff` hash all match the pre-1E baseline.
  - The only text difference is a git CRLF warning on stderr, which went into the capture this time.
- **Docker:** images, containers, networks and volumes match the pre-1E inventory. There are 0 containers, networks or volumes with the `io.brainfrog.owner` label.
- **`git diff --check`:** clean.
- **Secrets and privacy:**
  - `python -m security.git_guard --file` on all 26 changed or untracked files found **0 findings**.
  - The user-profile-path grep matched only the synthetic test input in `tests/test_ci_gate.py` and the rule text in `phase1e-evidence-provenance.md`.
- **Staging:** nothing is staged. An accidental `git add -N .` (index-only intent-to-add, run while counting files) was reverted with `git rm --cached` on exactly the 21 paths it added. The working tree was untouched, and the status matches the earlier snapshot.
- **Clean-up and source integrity:**
  - The temporary directory `bf-1e-ci-sim` was empty and was removed.
  - Tests wrote no files into the worktree: the status shows only the intended Phase 1E changes.

## 7. Local sign-off run (2026-10-22 06:55–07:09)

**How it was run:**
- `run_one_v2.ps1` (the bounded runner described in §5), tag `-signoff`.
- One `python -m unittest <module> -v` process at a time, on the same 1E tree; no source changes since 00:03:01.
- Memory gate: at least 2048 MB, with at most a 60 s wait; memory sampled every 2 s.
- No suite timed out. The lowest available memory observed was **2490 MB**.
- Suites already reported in §1 (rows 1–7) were **not** re-run.

### 7.1 Pending regressions

| # | Module | Exit | Ran | Result | Duration | Min avail |
| :- | :--- | :- | -: | :--- | -: | -: |
| 1 | `test_verifier_execution_integrity` | 0 | 17 | PASS (OK) | 84.2 s | 3368 MB |
| 2 | `test_verifier_test_result_semantics` | 0 | 8 | PASS (OK) | 5.6 s | 3502 MB |
| 3 | `test_verifier_sandbox_execution` | 0 | 5 | PASS (OK) | 6.2 s | 3506 MB |
| 4 | `test_verifier_phase1c_blockers` | 0 | 29 | PASS (OK, **1 SKIP**) | 98.0 s | 2895 MB |
| 5 | `test_verifier_docker_integration` | 0 | 11 | PASS (OK, **1 SKIP**) | 98.3 s | 2498 MB |
| 6 | `test_verifier_browser_plan` | 0 | 19 | PASS (OK) | 1.9 s | 3086 MB |
| 7 | `test_verifier_browser_security` | 0 | 32 | PASS (OK) | 17.0 s | 3011 MB |
| 8 | `test_verifier_browser_evidence` | 0 | 18 | PASS (OK) | 74.6 s | 3070 MB |
| 9 | `test_verifier_browser_integration` (real Docker and Chromium) | 0 | 13 | PASS (OK) | 171.0 s | 2490 MB |
| 10 | `test_phase6b_cli_verification` | 0 | 15 | PASS (OK) | 98.8 s | 3159 MB |
| 11 | `test_cli_workspace_verification` | 0 | 7 | PASS (OK) | 10.2 s | 3501 MB |
| 12 | `test_runtime_boundary` | 0 | 3 | PASS (OK) | 2.2 s | 3507 MB |
| 13 | `test_git_guard` | 0 | 20 | PASS (OK) | 9.2 s | 3500 MB |
| B | `test_proof_inspection_ux` (baseline row) | 1 | 32 | **30 ok, 2 ERROR** (known baseline) | 35.8 s | 3414 MB |

**Totals for the 13 regression suites:** 197 run, 195 ok, 2 skipped, 0 failures, 0 errors, 0 timeouts.

**Baseline row:**
- The two errors are exactly `TestProofInspectionUX.test_11_repl_proof_aliases` and `TestVerifyProofCLISemantics.test_11_repl_proof_aliases`. Both end in `StopIteration`.
- This is the same pair, with the same signature, as on clean `c78e798` (Phase 1D sign-off, 2026-10-21). It is not a new regression, and it is not counted as passing.

**Skips (Windows only; no symlink privilege, `WinError 1314`):**
- `test_verifier_phase1c_blockers.TestSnapshotBinding.test_symlink_escape_not_followed`
- `test_verifier_docker_integration.TestDockerIntegration.test_symlink_in_repository_is_not_followed`

Both are on the policy's `required_passing_tests` list.

**Linux follow-up** (2026-10-22 07:08:32, same container flags as §3, these two test IDs only), exit 0:
- `test_symlink_escape_not_followed`: **executed, ok**.
- `test_symlink_in_repository_is_not_followed`: **skipped**, "Docker engine or approved local image python:3.12-slim unavailable". The test class needs a Docker engine, and the container has none. Giving it the Docker socket would weaken isolation, so this was not done.

**Required symlink tests:** **14 of 15 executed and passed on Linux locally.** The remaining one is **pending**, to be executed by the CI `Linux self-test` job, which treats a skip as a failure.

### 7.2 Workflow static validation

**Automated linting is PENDING.**
- `actionlint`, `zizmor`, `yamllint` and `shellcheck` are not on PATH.
- `pip show zizmor actionlint-py`: not installed.
- PyYAML: not installed.
- No linter container images are present locally.
- Nothing was installed or downloaded, because that was not authorized.

**Manual review of `.github/workflows/brainfrog-verify.yml` and `core/ci_gate.py`** (this is not equivalent to automated linting):

| Check | Observation |
| :--- | :--- |
| YAML structure | Parsed by the strict subset loader in `test_ci_workflow_security`: no anchors or tags, no duplicate keys. Three jobs; `attest-evidence` has `needs: verification` |
| Expression syntax | `${{ }}` appears only in `ref:`, `with:`, `if:`, `outputs:`, `concurrency.group` and artifact names. **None appears in any `run:` block** (tested) |
| Permissions | Workflow: `contents: read`. `verification` and `linux-selftest`: `contents: read`. `attest-evidence`: `id-token`, `attestations` and `artifact-metadata: write`, with no checkout. *Whether actionlint's known-scope list accepts `artifact-metadata` is not verified, and is one reason actionlint is still required* |
| Trusted verifier identity | `trusted/` is checked out at `github.event.pull_request.base.sha`. The gate requires trusted HEAD == `base_sha`. Entry point: `python -I trusted/scripts/brainfrog_ci.py` |
| Trusted policy identity | `trusted/.github/brainfrog/policy.json`. Its digest is recorded, and candidate changes to trusted paths force BLOCKED |
| Fork PRs | `pull_request` gives forks a read-only token and no secrets. Attestation is skipped unless `head.repo.full_name == github.repository` |
| FAIL/BLOCKED → non-success | The gate exits 1 or 2, which fails the step. `enforce` (`if: always()`) re-validates the bundle and exits 1 or 2, or 2 if the bundle is invalid. A gate crash produces a BLOCKED diagnostic bundle. There is no `continue-on-error` and no `\|\| true` (the only grep hit for the forbidden patterns is the explanatory comment on line 4) |
| Artifact upload | `if: always()`, `if-no-files-found: error`, 14 days, no hidden files. Only `bf-ci/brainfrog-ci-evidence` is uploaded |
| SHA binding | Candidate HEAD must equal `GITHUB_SHA`. `head_sha`, `base_sha` and `tested_sha_is_pr_head` are recorded |
| Untrusted interpolation | None. PR ref and title are never put into shell text, and the title is not recorded |
| `pull_request_target` / `workflow_run` | Not used |

### 7.3 Integrity after the sign-off (2026-10-22 07:09–07:12)

- **Protected worktrees:** all four match the pre-1E baseline byte-for-byte (`diff` exit 0).
- **Docker:** images, containers, networks and volumes are identical to the pre-1E inventory. 0 containers, networks or volumes carry the `io.brainfrog.owner` label. No `python.exe` processes are left.
- **Git index:** nothing staged; **0 intent-to-add entries** (index ⊆ HEAD); HEAD is still `41c6f0f`. `git diff --check` is clean.
- **Source integrity:** no tracked or Phase 1E file changed during the tests.
  - The tests created one untracked runtime file, **`.brainfrog/workspace.lock`** (1 byte, 07:07:20, during `test_runtime_boundary`).
  - It was left in place, because workspace locks must not be deleted, and it must not be committed.
  - `__pycache__` files are git-ignored.
- **Secrets:** `python -m security.git_guard --file` over all 29 changed or untracked files (including the lock and the updated docs) found **0 findings**.
- **Logs:** all logs are preserved in the session scratchpad: `*-signoff.{out,err,progress}.log`, `linux-required-symlink2-*.log`, and `results.jsonl`.

## 8. Automated workflow lint gate (2026-10-22 07:12–07:20, operator-authorized)

### 8.1 Tool provenance

- **Download:** upstream GitHub releases only, into isolated per-tool directories in the session scratchpad. Nothing was installed globally, PATH and system settings were not changed, and no Docker image was pulled.
- **Execution:** each binary ran only after all three checks below passed.

| Tool | Release asset (URL) | sha256 | Checks |
| :--- | :--- | :--- | :--- |
| actionlint **1.7.12** (latest stable; built with go1.26.1, windows/amd64) | `https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_windows_amd64.zip` | `6e7241b51e6817ea6a047693d8e6fed13b31819c9a0dd6c5a726e1592d22f6e9` (extracted `actionlint.exe`: `54ca21be3de4c7cfa26914aa8b61bd76bf573ef3caac5f80d110558cdf241718`) | (1) equals the GitHub release-asset digest; (2) equals the entry in `actionlint_1.7.12_checksums.txt` (sha256 `433028cf…a4f5`); (3) `gh attestation verify --repo rhysd/actionlint` passes: SLSA provenance v1, signer `rhysd/actionlint/.github/workflows/release.yaml@refs/tags/v1.7.12`, source `914e7df21a07ef503a81201c76d2b11c789d3fca`, GitHub-hosted runner, issuer `token.actions.githubusercontent.com` |
| zizmor **1.31.1** (latest stable, released 2026-10-15) | `https://github.com/zizmorcore/zizmor/releases/download/v1.31.1/zizmor-x86_64-pc-windows-msvc.zip` | `4de30371c8a3b483aecbcf0dec1fd6a2b6aae65051ca226b4f6527baac6a8c7a` (extracted `zizmor.exe`: `320dbd80365cce71200f31d7b2e6148c7ea0ea950ef2661d74d18f308d6f4148`) | (1) equals the GitHub release-asset digest; (2) `gh attestation verify --repo zizmorcore/zizmor` passes: SLSA provenance v1, signer `zizmorcore/zizmor/.github/workflows/release-binaries.yml@refs/tags/v1.31.1`, source `effdb35fa7868863f11655db256d9008922fa427`, GitHub-hosted runner |

### 8.2 Results against `.github/workflows/brainfrog-verify.yml`

Both tools ran with `GH_TOKEN` and `GITHUB_TOKEN` unset; zizmor also ran with `--offline`.

| Run | Command | Result |
| :--- | :--- | :--- |
| actionlint, initial | `actionlint -verbose .github/workflows/brainfrog-verify.yml` | exit 0, **0 findings**. Note: `Rule "shellcheck" was disabled: executable not found` (see §8.4) |
| actionlint negative control (scratch copy, not the worktree) | the same workflow with an injected `${{ github.event.pull_request.title }}` in a `run:` block and an unknown permission scope `bogus-scope` | exit 1. It flagged **both**: `[expression]` "potentially untrusted… avoid using it directly in inline scripts" and `[permissions]` "unknown permission scope". Its list of valid scopes includes **`artifact-metadata`**. This shows the expression and permission rules are active and that `artifact-metadata: write` is accepted |
| zizmor, initial, default persona | `zizmor --offline --no-progress <workflow>` | exit 0, no findings, "1 suppressed" (filtered by persona; there is no config file and no inline ignore) |
| zizmor, initial, `--persona=pedantic` and `--persona=auditor` | as above | exit 12. **1 finding:** `undocumented-permissions` (low severity, high confidence) on lines 151–153, the attest job's `id-token`, `attestations` and `artifact-metadata: write` |
| **Fix** | inline explanatory comments on those three lines, worded from the pinned `actions/attest` README ("mint the OIDC token… Sigstore signing certificate", "persist the attestation", "create the artifact storage record"). No permission was removed or changed, and no suppression was added | workflow sha256 is now `2591d39c0f92222c33050f9f00748e8165958390312c0f782937e2e2262cd91a` |
| actionlint, final | as above | exit 0, **no output** |
| zizmor, final, `regular`, `pedantic`, `auditor` | as above | exit 0 for all three: **"No findings to report"**, 0 suppressed |

**Finding classification:**

| Finding | Classification | Reason | Status |
| :--- | :--- | :--- | :--- |
| `undocumented-permissions` | **Real defect** (documentation and reviewability, not exploitable) | Write scopes without a stated reason are harder to review | Fixed |

There are no false positives and no unresolved findings.

### 8.3 Re-tests after the workflow edit

Only comments changed. No verifier or gate code changed.

| Suite | Result |
| :--- | :--- |
| `tests.test_ci_workflow_security` | exit 0, **17 run, 17 ok**, 0.3 s (07:17:03) |
| `tests.test_ci_gate` | exit 0, **53 run, 53 ok**, 14.0 s (07:17:30). Its fixtures copy the workflow, so it was re-run as directly affected |

`test_ci_gate_live` was not re-run. Its trusted-config check compares the trusted and candidate copies of the workflow, and a comment change does not affect it.

### 8.4 Coverage notes

- **What the linters cover:**
  - `pull_request`-only trigger, so no `pull_request_target` or `workflow_run` execution;
  - permission scopes and the minimal defaults;
  - expression syntax and injection in `run:`;
  - `persist-credentials: false` (no zizmor `artipacked` finding);
  - pinned actions (no `unpinned-uses` finding);
  - attest-job permissions;
  - artifact upload configuration.
- **What they don't cover:** the trusted base-revision sourcing, the FAIL/BLOCKED/invalid-evidence → non-success mapping and SHA binding are semantic properties. They are covered by `test_ci_workflow_security`, `test_ci_gate` and `test_ci_gate_live`, not by the linters.
- **Shellcheck:** actionlint's shellcheck sub-rule did **not** run, because shellcheck is not installed and its installation was not part of this authorization.
  - The workflow's `run:` blocks are one or two lines each and contain no `${{ }}` (tested).
  - Each block calls the trusted Python entry point with a quoted `"$RUNNER_TEMP/…"` path.
  - This is recorded as a limitation, not a pass.

### 8.5 Integrity after the lint gate (07:18–07:20)

- **Protected worktrees:** the four protected worktrees are identical to the pre-1E baseline.
- **Docker:** images and containers are identical, with 0 resources owned by BrainFrog.
- **Git index:** HEAD is still `41c6f0f`; 0 staged; 0 intent-to-add entries; `git diff --check` is clean.
- **Excluded local artifacts:** untouched. `.brainfrog/workspace.lock` is still the 07:07:20 one, and no `.brainfrog/reports/` or `phase1d-live-evidence/` exist in the worktree.
- **Changes since §7:** the only file changed is `.github/workflows/brainfrog-verify.yml` (the comment fix).
- **Secrets:** `git_guard --file` over 29 changed or untracked files found 0 findings.
- **Downloads:** the downloaded tools stay in the session scratchpad and are outside the repository.
