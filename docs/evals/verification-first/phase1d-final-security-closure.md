# Phase 1D — Final Security Closure

**Date:** 2026-10-21
**Worktree:** `<PHASE_1D_WORKTREE>` (detached at `c78e798`; uncommitted)
**Scope:** Chromium renderer sandbox, browser-runtime provenance, re-validation of the two-container trust model, assertion semantics and evidence integrity. This was a focused closure, not a rebuild.

## Verdict

**C. PARTIAL — REQUIRED VALIDATION UNAVAILABLE.** The security work is complete; full regression re-validation is not.

**Security items closed** (all observed, not assumed):

- Chromium's renderer sandbox is **on** and proven on every run.
- The browser runtime is current: Playwright 1.64.0 with Chromium 156.0.8078.4, pinned and hash-locked.
- The real browser, isolation and evidence suites pass on the final code.

**What is missing:** the full Phase 1B/1C regression re-run after this closure's changes to `core/verifier.py`. Claude Code **stopped that run because the host was critically low on memory**. It is not to be restarted without your instruction. 6 of 20 suites completed, all passing; the other 14 were not re-run (§7).

**Next step:** when you're ready, re-run the remaining 14 suites (command in §7). If they are green with only the known baseline issues, the verdict becomes **A. SECURITY CLOSED — READY FOR CHECKPOINT COMMIT**.

The existing Phase 1D *functional* DONE verdict remains valid: it was re-proven on the new runtime with the sandbox enforced (§5).

## 1. Chromium renderer sandbox

### 1.1 Root cause

Under Phase 1D's container policy, launching with `chromium_sandbox=True` failed with *"Chromium sandboxing failed!"*. That policy is non-root UID 65532, `--cap-drop=ALL`, `no-new-privileges` and Docker's built-in seccomp. Earlier, `chromium_sandbox=False` passed `--no-sandbox`.

Observed facts:

| Item | Observed |
| :--- | :--- |
| Engine security options | `name=seccomp,profile=builtin`, `name=cgroupns`. There is no AppArmor in Docker Desktop's VM. |
| Kernel | `6.6.87.2-microsoft-standard-WSL2`; `user.max_user_namespaces=31173` (unprivileged user namespaces available) |
| In-container baseline | `CapEff=0`, `NoNewPrivs=1`, `Seccomp=2`, `Seccomp_filters=2`. `unshare -U` gives **EPERM**, blocked by seccomp, not by the kernel. |
| Docker default (`moby/profiles/seccomp v0.1.0`, vendored by `docker-v29.1.3`) | Allows `clone` only without namespace flags (masked-equality rule), and `unshare` and `chroot` only with `CAP_SYS_ADMIN` / `CAP_SYS_CHROOT`. |

Chromium's layer-1 sandbox creates user, PID and network namespaces and then chroots. With no capabilities, the default profile denies all three steps.

### 1.2 Experiment

Each run used disposable BrainFrog-owned containers with the full hardening in place: `--read-only`, `--cap-drop=ALL`, `no-new-privileges`, UID 65532, `--network=none`, resource limits, no Docker socket, no host write mounts. None of `--privileged`, `CAP_SYS_ADMIN`, `seccomp=unconfined`, `no-new-privileges:false` or `--network=host` was used.

| Profile (all = Docker default **plus**…) | Chromium 131 | Chromium 156 |
| :--- | :--- | :--- |
| builtin (nothing added) | sandbox **fails** | sandbox **fails** |
| A: `clone`, `unshare` | fails | — |
| D: `unshare`, `chroot` | fails | — |
| E: `clone`, `chroot` | fails | — |
| **B: `clone`, `unshare`, `chroot`** | **works** | **works** |
| C: B + `setns` | works (not needed) | — |

**B is the minimum.** Playwright's published `seccomp_profile.json` (identical at v1.49.1 and v1.64.0) is broader: it is based on an older Docker default and also allows `setns`, `io_uring_*` and unrestricted `socket`. It was **not** adopted.

### 1.3 Implemented configuration

- **Profile file:** `core/runtime/browser_seccomp_chromium.json` is the Engine-29 default profile (`moby/profiles/seccomp v0.1.0`, sha256 `01536f1d…8301e28`) with **one** rule appended: `{"names": ["clone", "unshare", "chroot"], "action": "SCMP_ACT_ALLOW"}`. File sha256: `569408de219735e81062a130bbf23fdae7924655794bfc0b487d362b73104b39`.
- **Hash pin:** pinned in `browser_sandbox.BROWSER_SECCOMP_PROFILE_SHA256`. A modified file is refused (`load_browser_seccomp_profile`) and the criterion is BLOCKED.
- **Browser container:** gets `--security-opt=seccomp=<that file>`. Before start, `docker inspect` must show exactly one seccomp option, and its JSON must **equal** the pinned profile; otherwise the run is BLOCKED. Every other control is unchanged.
- **App container:** keeps Docker's default seccomp. A custom profile there is a policy violation.
- **Launch and proof:** the runner launches with `chromium_sandbox=True`, then **proves the sandbox from `/proc`** twice, after the first navigation and again at the end. Every `--type=renderer` process must:
  - be in its own user, PID and network namespace (different from the browser side);
  - be chrooted (root ≠ `/`);
  - have **more** seccomp filters than the browser side (Chromium's seccomp-BPF added);
  - have `NoNewPrivs=1`.

  If no renderer exists or any check fails, the result is BLOCKED (`renderer_sandbox_unverified`).
- **Host-side enforcement:** the host rejects any PASS or FAIL whose evidence lacks `chromium_internal_sandbox: true` and both verified sandbox states (`evidence_binding_failed`).

### 1.4 Observed sandbox state (Chromium 156, pinned profile)

| Process | User ns | PID ns | Net ns | Root | Seccomp filters |
| :--- | :--- | :--- | :--- | :--- | :--- |
| browser / gpu / utility (network service) | container | container | container | `/` | 2 (Docker) |
| zygote (sandboxed) | **new** | **new** | **new** | `/proc/24/fdinfo` | 2 |
| **renderer** | **new** | **new** | **new** (no network) | **`/proc/24/fdinfo`** | **3** (Docker + Chromium seccomp-BPF) |

One sandboxed zygote shows `CapEff=0x200000` (`CAP_SYS_ADMIN`). That capability exists only inside Chromium's private user namespace, not the container's.

In run 2, every executed PASS and FAIL recorded `renderer_sandbox.verified=true` at both checkpoints, with renderer filters 3 against the browser side's 2. The one exception is hostile AC-01: containment BLOCKED it before the first checkpoint, and its end-of-run check verified true. `chrome://sandbox` is not available in headless shell (`net::ERR_INVALID_URL`), so `/proc` is the proof source.

### 1.5 Remaining attack surface

- **Namespace syscalls in the browser container.** The browser container can call `clone`, `unshare` and `chroot` without capabilities, which exposes user-namespace kernel code. A renderer cannot reach this: Chromium's own seccomp-BPF applies on top. Code that escapes the renderer into the browser process *can* reach it. The trade is deliberate. Before this change, a renderer exploit ran directly as UID 65532 in the container with no inner sandbox.
- **Shared kernel.** Docker Desktop runs every container in one WSL2 VM kernel. A kernel exploit is not contained by any control here.
- **GPU and network-service processes** run without the namespace sandbox. This is Chromium's normal layout; they hold no page script.

## 2. Browser runtime

### 2.1 Review

| | Before | After |
| :--- | :--- | :--- |
| Playwright (Python) | 1.49.1 (PyPI 2024-12-10) | **1.64.0** (PyPI 2026-10-13, latest release at review time) |
| Chromium | 131.0.6778.33 | **156.0.8078.4** (`browser.version`, observed) |
| Base image | `mcr.microsoft.com/playwright/python:v1.49.1-noble@sha256:42e251ec…` | `mcr.microsoft.com/playwright/python:v1.64.0-noble@sha256:684df257a1fa0ef3fa288239819984008f560e8f8db5b635ee9e2a50cbcd2e23` (registry index digest, queried read-only before the pull) |
| Python package install | `pip install playwright==1.49.1` (unpinned dependencies, no hashes) | `pip install --no-deps --require-hashes -r docker/browser-runner/requirements.lock` |
| Approved runner image | `brainfrog-browser-runner:v1.49.1` = `sha256:a880ad52…` | `brainfrog-browser-runner:v1.64.0` = **`sha256:21d60e45fd0e7214a7027c115cf52ea34f9dc1fdde3224735f6d58197ab0c0de`** |

Security context for leaving 131:

- [CVE-2025-6554](https://securityaffairs.com/179549/hacking/cve-2025-6554-is-the-fourth-chrome-zero-day-patched-by-google-in-2025.html) is a V8 type-confusion bug, exploited in the wild, and fixed in Chrome 138.0.7204.96. It affects 131 on all platforms.
- [CVE-2025-2783](https://www.bleepingcomputer.com/news/security/google-fixes-chrome-zero-day-exploited-in-espionage-campaign/) is a sandbox escape, but Windows-only, so it doesn't apply to these Linux containers.
- Playwright's own [Docker guidance](https://playwright.dev/python/docs/docker) says its image is meant for testing and is *not recommended for visiting untrusted websites*, and that the sandbox needs a seccomp profile.

**No claim is made that 156.0.8078.4 has no known vulnerabilities.** It is the Chromium build that ships with the latest Playwright release. The Playwright release notes list Chrome **155** as the tested stable channel, so this build is Playwright's own, slightly ahead of stable. Keeping it current is an operational duty (§8).

### 2.2 Upgrade (operator-authorized, completed)

1. `docker pull mcr.microsoft.com/playwright/python:v1.64.0-noble@sha256:684df257…`. The pulled image ID equals that digest (created 2026-10-19), and it ships `chromium-1248` and `chromium_headless_shell-1248`. Like 1.49.1, it does **not** contain the `playwright` Python package.
2. `docker build --pull=false -t brainfrog-browser-runner:v1.64.0 docker/browser-runner`. It installs four wheels whose sha256 values were taken from PyPI's JSON API:

   | Package | sha256 |
   | :--- | :--- |
   | `playwright==1.64.0` | `5a59af1b…0c81b75` |
   | `pyee==14.0.0` | `3ac2d322…a644e4d` |
   | `greenlet==3.5.6` (cp312 manylinux x86_64) | `975736b0…70353aa1` |
   | `typing-extensions==4.16.0` | `481caa48…6370c2e8` |

   The build asserts `playwright==1.64.0`.

**What each identifier fixes:**

| Identifier | Fixes |
| :--- | :--- |
| Registry digest | The base image content |
| Lock-file hashes | The Python wheels |
| `requirements.lock` sha256 `1654ad6f…`; `Dockerfile` sha256 `b860f73e…` | The build recipe |
| Approved **local image ID** | The exact image BrainFrog will run. Tag drift is BLOCKED (`image_not_approved`). |
| Tags (`v1.64.0-noble`, `brainfrog-browser-runner:v1.64.0`) | Nothing. Tags are mutable and only used for lookup. |

**Reproducibility caveat:** BuildKit's attestation manifest makes the image ID differ between builds of identical layers (observed: `87dc8a9f…`, then `21d60e45…`, from the same inputs). So each rebuild needs re-approval.

**Policy:** the 1.49.1 image is **no longer approved**; using it raises `ValueError`. It remains on disk, untouched, for rollback. The runner also checks that the observed Playwright and Chromium versions equal the approved values.

### 2.3 Compatibility defect found by the upgrade

Chromium 156 forced HTTPS for the bare host `app`. It is Google's `.app` gTLD, which is HSTS-preloaded, and every navigation failed with `ERR_SSL_PROTOCOL_ERROR`. The verifier correctly reported BLOCKED, not FAIL.

The cause was isolated by A/B test: alias `app` gave BLOCKED, alias `bf-app` gave PASS. The application alias is now **`bf-app`** (origin `http://bf-app:8080`). A hyphenated label can never be a TLD. Playwright already disables `HttpsUpgrades`, so that wasn't the cause.

## 3. Two-container trust model (re-validated)

| Property | Evidence |
| :--- | :--- |
| App runs the exact snapshot | The launcher re-hashes `/app` and the digest must equal the materialized digest. The correct and broken fixtures have different digests (`cd2d8eb1…` / `31cb25ee…`). |
| Browser runs trusted code; plan is declarative | The runner is BrainFrog's file, read-only mounted, with its sha256 recorded. The plan is canonical JSON from the validator. `evaluate`, `screenshot` steps and absolute URLs are BLOCKED (demo 04). |
| No spec or app code in the verifier | No import or exec path exists; the app's JavaScript runs only inside the sandboxed renderer. |
| Private network only, no ports, no socket, no credentials | Live `docker inspect` / `docker port` during the run showed no ports and an internal network whose only member is the app. Effective-config checks cover the socket, mounts and environment allowlist. |
| Host-private, metadata and external unreachable | In-container probe (test 04): `1.1.1.1`, `8.8.8.8:53`, `169.254.169.254`, `192.168.65.254`, `172.17.0.1` and the network gateway on ports 80 and 2375 are all unreachable. `example.com`, `host.docker.internal` and `gateway.docker.internal` don't resolve. |
| Evidence not modifiable by the app | The app has no path to the runner's tmpfs or stdout. The browser has no host write mount. Records are nonce-bound and hash-checked. |
| Cleanup touches only owned resources | Label-checked removal by ID only. 0 owned resources remain, and unrelated containers, networks and volumes are identical before and after. |

**Network red team** (real browser, `test_03b` plus demo 03):

| Attempt | Result |
| :--- | :--- |
| Meta-refresh redirect to `169.254.169.254` | BLOCKED `unapproved_navigation` |
| Off-origin iframe | BLOCKED `unapproved_navigation` |
| `window.open` popup to `example.com` | BLOCKED `unapproved_navigation` (popup counted) |
| Script navigation to `example.com` (`escape.html`) | BLOCKED; 5 off-origin requests blocked and recorded |
| `file:///etc/passwd` navigation | Refused by Chromium; page stayed on `/file-nav` (asserted) |
| WebSocket to `ws://example.com` | Never connected (asserted `failed`); recorded as unapproved; verdict decided by the assertion |
| Off-origin fetch and image | Blocked and recorded; verdict decided by the page's own assertions |
| Absolute or `file:` URLs in the plan | Rejected by the validator (BLOCKED) |

Request interception is one layer only. The OS boundary is the internal Docker network, plus Chromium's `--host-resolver-rules`.

## 4. Assertion semantics (re-validated, unchanged)

All ten required properties are covered by passing tests, unit and/or live:

- visible means exactly one visible element;
- hidden text gives FAIL (live broken AC-03: `visible: false`);
- a click is an action, never an assertion;
- navigation-only, screenshot-only or unsupported plans are BLOCKED and never executed;
- invalid plans don't crash;
- infrastructure failures are BLOCKED (app down or not starting, image missing, Playwright missing, timeout, killed, OOM);
- a real mismatch is FAIL;
- mixed browser, test and build semantics hold (demo 01/02, unit test).

The earlier-attempt regressions remain fixed: no `seccomp=unconfined`, a missing manifest never passes, hidden text never passes, exact origin parsing, and no non-existent error category.

No working behaviour was changed without a reproduced defect. The behavioural changes in this closure are:

- the HSTS alias fix;
- the sandbox enablement and its proof requirement;
- the two integrity-verifier fixes below.

## 5. Evidence integrity

**Defects found and fixed in the inherited Phase 1B/1C `verify_report_integrity`:**

1. **Crash on malformed metadata.** `report.json` as a list, string criteria, integer criteria, list-typed evidence maps, or a string `snapshot_identity` all raised `AttributeError`. They now fail cleanly with a finding.
2. **Reads outside the bundle.** A report could point its evidence at `C:/Windows/win.ini` or `../../…`, and the verifier read and hashed those host files. Evidence paths must now stay inside the bundle; otherwise the result is a finding and nothing is read.

**Tests added:**

| Case | Result |
| :--- | :--- |
| Screenshot bytes modified | Detected |
| Another criterion's screenshot substituted and re-hashed | Detected ("screenshot hash differs") |
| Assertion file removed, with or without its record | Detected |
| Mismatched criterion reference | Detected |
| Status upgraded FAIL → PASS | Detected |
| 8 malformed `report.json` shapes | Rejected, no crash |
| 3 path-escape forms | Rejected |
| **Fully re-forged, internally consistent bundle** | **Accepted.** This test is kept on purpose to document that unsigned bundles are tamper-evident, not authentic. |

Phase 1C-shaped reports still verify.

**Live (run 2):** `--check-report` returned exit 0 on all 7 real reports and exit 1 on both tampered copies.

**Privacy:** the sanitized evidence copies (`phase1d-live-evidence/run1-…` and `run2-…`) contain the items below. These copies are **local-only**: the operator excluded them from the checkpoint commit `41c6f0f` (see [phase1d-commit-readiness.md](phase1d-commit-readiness.md)), so they are not in the repository. From Phase 1E on, reviewable evidence comes from CI artifacts instead (see [phase1e-evidence-provenance.md](phase1e-evidence-provenance.md)).

The copies contain:

- no user-profile or temp paths;
- no usernames, credentials, cookies or authorization headers;
- no environment dumps.

Local paths are replaced with `<scratch>`/`<userprofile>`, and the screenshots show synthetic fixtures only. The full report bundles stay **outside Git**, because their `effective_container_config` records host temp paths. The originals are preserved in the session scratchpad (`p1d/kept_reports`, `p1d_close/kept_reports`).

## 6. Live verification (run 2: Chromium 156, sandbox enforced)

Each demo ran `python cli.py --repo <disposable fixture repo> --spec spec.md [...]`:

| # | Scenario | Exit | Result |
| :--- | :--- | :--- | :--- |
| 01 | correct fixture, mixed spec (4 browser + unit + build) | 0 | **PASS** 6/6 |
| 02 | broken fixture, same spec | 1 | **FAIL**:<br>• AC-03 hidden error ("exists but is not visible");<br>• AC-04 "content width 960.0px exceeds viewport width 375.0px";<br>• the other 4 PASS. |
| 03 | hostile | 2 | AC-01 BLOCKED `unapproved_navigation` (5 blocked); AC-02 PASS (3 blocked, recorded) |
| 04 | false proofs (navigation-only, `screenshot`, `evaluate`, metadata URL) | 2 | all BLOCKED `invalid_browser_plan` |
| 05 | `--sandbox none` | 2 | all BLOCKED (no host browsing) |
| 06 | Docker unreachable (`DOCKER_HOST` set to a bogus pipe) | 2 | all BLOCKED `docker_unavailable` |
| 07 | spec requests profile `node-dev-server` | 2 | browser criteria BLOCKED (unsupported profile); unit and build PASS |

**Run IDs:**

| Demo | Run ID |
| :--- | :--- |
| 01 | `verify_20261020_184609_b161ca_f0941b` |
| 02 | `verify_20261020_184704_b161ca_3eaa58` |
| 03 | `verify_20261020_184803_7c3573_cef0ee` |

**Broken-run evidence:**

| Item | sha256 prefix |
| :--- | :--- |
| AC-03 screenshot | `4a25c519562c1984` |
| AC-04 full-page screenshot | `51c643b6a7a3611c` |
| AC-03 assertion JSON | `9f9ca5c3fb17adba` |
| AC-04 assertion JSON | `605160c4461ba7cb` |

The browser-container security option is recorded as `seccomp=custom-profile-canonical-sha256:13fd246cd233012f7f2ff6974e28a94ebf66aa8733dcde5ae7a9d0b8d4d8885a` (the canonical form of the pinned profile).

The real-Docker integration suite (13 tests) covers:

- crash: renderer OOM at 768 MiB gives BLOCKED `resource_limit_termination`;
- browser killed mid-run gives BLOCKED;
- app killed or not starting gives BLOCKED;
- missing image and Playwright-less image give BLOCKED;
- timeout and Ctrl+C clean up;
- unrelated resources are unchanged.

## 7. Regression (actual, this closure)

**Completed on the final code:**

| Suite | Result |
| :--- | :--- |
| `test_verifier_browser_plan` | 19 passed |
| `test_verifier_browser_security` | 32 passed (6 new: seccomp pin/content/tamper, sandbox-proof requirement, app seccomp, outdated runtime refused) |
| `test_verifier_browser_evidence` | 18 passed (4 new: substitution, malformed metadata, path escape, forged-bundle limit) |
| `test_verifier_browser_integration` (real Docker + Chromium 156) | 13 passed (1 new: network red team) |
| `test_standalone_verifier` | 21 passed |
| `test_verifier_execution_integrity` | 17 passed |

**Not re-run after this closure's changes** (run stopped for host memory pressure):

- `test_verifier_evidence_integrity`
- `test_verifier_test_result_semantics`
- `test_verifier_sandbox_execution`
- `test_verifier_phase1c_blockers`
- `test_verifier_docker_integration`
- `test_runtime_boundary`
- `test_git_guard`
- `test_phase6b_cli_verification`
- `test_pr_proof`
- `test_proof_discovery_ux`
- `test_proof_inspection_ux`
- `test_cli_runtime_unification`
- `test_cli_workspace_verification`
- `test_runtime_permissions`

Their last results are from **before** this closure, on 2026-10-20, so they are not counted as new results here:

- all green, except `test_proof_inspection_ux`, which had 2 pre-existing `test_11_repl_proof_aliases` errors that reproduce on a clean HEAD;
- 2 symlink skips: one in `phase1c_blockers` and one in `docker_integration`. Windows refuses symlink creation in this session. A junction-based test covers related behaviour but does **not** verify the symlink path.

The most relevant unrun suite is `test_verifier_evidence_integrity`, because this closure changed `verify_report_integrity`.

**To re-run:**

```powershell
python -m unittest tests.test_verifier_evidence_integrity tests.test_verifier_test_result_semantics tests.test_verifier_sandbox_execution tests.test_verifier_phase1c_blockers tests.test_verifier_docker_integration tests.test_runtime_boundary tests.test_git_guard tests.test_phase6b_cli_verification tests.test_pr_proof tests.test_proof_discovery_ux tests.test_proof_inspection_ux tests.test_cli_runtime_unification tests.test_cli_workspace_verification tests.test_runtime_permissions
```

**Hygiene:**

- `git diff --check`: clean.
- No trailing whitespace in any Phase 1D or closure file.
- `python -m security.git_guard --file` over all 120 candidate files: **0 findings**; the positive control was detected. A `--staged` scan would cover nothing, since nothing is staged.

## 8. Workspace and Docker integrity

**Workspaces:**

- Protected worktrees: all 454 inventory lines are identical before and after.
- The earlier Phase 1D attempt's reports and `.brainfrog/workspace.lock` were left untouched.
- The run-1 evidence was moved into `run1-chromium131-unsandboxed/` with all 22 hashes unchanged.

**Docker inventory:**

| Resource | Change |
| :--- | :--- |
| Containers, networks, volumes | identical before and after |
| Images | two operator-approved additions, nothing removed:<br>• `mcr.microsoft.com/playwright/python@sha256:684df257…`<br>• `brainfrog-browser-runner:v1.64.0` (`21d60e45…`) |
| BrainFrog-owned resources remaining | 0 |
| Prune commands run | none |

**Temp directories:** none remain with Phase 1D prefixes. 159 temp directories from today's runs of **inherited** Phase 1B/1C suites (`bf_cli_ws_test_*`, `bf_test_verify_*`, `bf_oci_*`, …) are left in `%TEMP%`. Those suites don't clean up after themselves, and they were not deleted.

## 9. Remaining risks and operational duties

1. **Runtime maintenance.** Chromium ages quickly. Bump Playwright, re-pin the digest and lock hashes, rebuild, re-approve the image ID, and re-run the integration suite with each Playwright release.
2. **Shared kernel and namespace syscalls** in the browser container (§1.5).
3. **Reports are unsigned.** A wholly re-forged bundle passes `--check-report`; there is a test that documents this.
4. **No server-side frameworks.** Only static apps are supported (`python-static`); other startup profiles are BLOCKED.
5. **Untested surfaces:** the installed `bf` entry point and non-Windows hosts.
6. **Symlink path:** the two symlink tests are skipped on this host.
