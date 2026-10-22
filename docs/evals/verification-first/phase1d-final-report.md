# Phase 1D — Final Report: Real Browser Assertions, Isolated Web Execution & Screenshot Evidence

> **Correction (2026-10-21, security closure):** Three statements in this report are no longer accurate. See [phase1d-final-security-closure.md](phase1d-final-security-closure.md).
>
> - **Chromium renderer sandbox:** it is now **enabled and verified on every run**, using a pinned seccomp profile. It is no longer disabled.
> - **Browser runtime:** Playwright 1.49.1 / Chromium 131 has been replaced by Playwright 1.64.0 / Chromium 156.0.8078.4, an operator-approved upgrade.
> - **Application alias:** the network alias is now `bf-app`, not `app`.
>
> The run-1 evidence cited below now lives in `phase1d-live-evidence/run1-chromium131-unsandboxed/`. That directory is local-only: it was excluded from the checkpoint commit `41c6f0f` and is not in the repository. The SHA-256 values quoted in these documents identify the files. The original text is preserved below as the record of run 1.

**Date:** 2026-10-21
**Worktree:** `<PHASE_1D_WORKTREE>` (detached at `c78e798`; all changes uncommitted)

## A. Final verdict

**DONE — REAL BROWSER VERIFICATION PROVEN** (within the threat model documented below)

Real headless Chromium (Playwright 1.49.1) navigated, typed, clicked and asserted against a real application. The application ran in a separate hardened container, and the two containers were joined only by an owned internal Docker network.

Genuine assertion success produced PASS and genuine assertion failure produced FAIL; infrastructure failures produced BLOCKED. Screenshots are hash-bound to criteria, plans and source snapshots, and `--check-report` detects tampering.

**Residual risk:** Chromium's *internal* renderer sandbox is not active. Enabling it would mean weakening the container's seccomp or `no-new-privileges` controls, so the hardened container is the only boundary around web content (§I).

## B. Workspace integrity

- **Phase 1D worktree:** `brainfrog-vf-1d-browser`. It already contained an unfinished Phase 1D attempt by another agent; at your choice, I continued in it. I backed up the prior files, with hashes, to the session scratchpad before editing. The prior attempt's two reports were left in place.
- **Verified Phase 1C base:** the Phase 1C runtime, harness and command policy, the 7 Phase 1B/1C test suites, and 20 Phase 1B/1C documents are **byte-identical** to the protected `brainfrog-vf-1c-execution` worktree. `core/verifier.py` and `cli.py` are the 1C content plus additive 1D changes; the only 1C lines removed are the "browser always BLOCKED (MCP runner unavailable)" stub. See [phase1d-capability-audit.md](phase1d-capability-audit.md) §2.
- **Not transferred:** `phase1c-live-evidence/`, which contains user-profile paths.
- **Lock files:** `.brainfrog/workspace.lock` was not removed from either worktree.
- **Protected worktrees:** `agentic_dev`, `brainfrog-vf-1b-closure` and `brainfrog-vf-1c-execution` are unchanged. All 454 lines of status and per-file SHA-256 inventory are identical before and after, and HEAD is `c78e798` everywhere.
- **No destructive operations:** no reset, stash, clean, commit, push, PR, or deletion of historical reports. No `prune` of any kind, and no action on unrelated Docker resources.

## C. Browser runtime

| Item | Value |
| :--- | :--- |
| Browser image | `brainfrog-browser-runner:v1.49.1` = `sha256:a880ad52ee02b240aaacf5bedcd61daf3c00fdc8b97303ca1521b7df8ab48dc4`, pinned. Operator-approved; built locally from `mcr.microsoft.com/playwright/python:v1.49.1-noble@sha256:42e251ec…6866` plus `pip install playwright==1.49.1`. |
| Playwright | 1.49.1 (Python) |
| Chromium | 131.0.6778.33 (headless, `chromium_sandbox=False`) |
| App image | `python:3.12-slim` = `sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1` |
| Docker | Engine 29.1.3, `desktop-linux`, WSL2, linux/amd64 |

Nothing was pulled or installed, and Docker Desktop was not reconfigured.

## D. Architecture

See [phase1d-architecture.md](phase1d-architecture.md) and [phase1d-browser-security.md](phase1d-browser-security.md).

- **App container:** approved profile `python-static`, running BrainFrog's stdlib `app_launcher.py`. The snapshot is mounted read-only, and the launcher reports the digest it observes.
  - It runs as UID 65532, with a read-only root filesystem, `--cap-drop=ALL`, `no-new-privileges` and default seccomp.
  - Limits are 256 MiB memory, 1 CPU and 64 PIDs. There are no ports and no host write mounts.
- **Browser container:** the pinned image running BrainFrog's `browser_runner.py`, under the same controls.
  - Limits are 1536 MiB memory, 2 CPUs, 256 PIDs and 256 MiB of shared memory.
  - Its tmpfs mounts are `noexec`, and it has **no host write mount**: evidence comes back as nonce-bound, hash-checked records.
- **Network:** a fresh, owned `--internal` bridge for each criterion, verified with `docker network inspect`. It contains only the app and the browser container. Chromium resolves only `app`, and Playwright allows only the exact origin `http://app:8080`.
- **Startup policy:** the spec may name an approved profile ID and nothing else. There is no `npm install`, no repository Dockerfile and no shell.
- **Trust boundaries:**
  - spec → validator;
  - repository → app container;
  - page content → browser container;
  - runner output → host validation and verdict recomputation;
  - bundle → `--check-report`.

## E. Supported browser checks

See [phase1d-browser-spec.md](phase1d-browser-spec.md) and [phase1d-assertion-semantics.md](phase1d-assertion-semantics.md).

- **Navigation:** the initial `path` and `goto_relative`; root-relative only, with exact-origin enforcement.
- **Input:** `fill`, `select_option`, `check_checkbox`. Input values are never recorded.
- **Clicking:** `click`, with Playwright actionability checks and strict single-element matching.
- **DOM assertions:** `expect_visible`, `expect_hidden`, `expect_text` (visible element, `innerText`), `expect_count`, `expect_url_path`.
- **Responsive overflow:** `expect_no_horizontal_overflow`, measured by the engine through CDP `Page.getLayoutMetrics`, so page scripts can't spoof it.
- **Screenshots:** captured by BrainFrog after the last step whenever a real page exists. Passwords are masked and animations disabled; plans with a layout assertion get a full-page capture.
- **Rejected:** `evaluate`, `execute_script`, `javascript`, `eval`, `script`, `shell`, `python`, network requests, upload and download, `screenshot` as a step, non-CSS selector engines, absolute or unsafe URLs, unknown keys, and plans with zero assertions.

## F. Real observed demonstrations

These ran through `python cli.py`; full detail is in [phase1d-live-browser-results.md](phase1d-live-browser-results.md).

- **Real PASS:** correct fixture with a mixed spec of 4 browser, 1 unit-test and 1 build criteria. 6/6 PASS, exit 0.
- **Real FAIL:** broken fixture.
  - **Invalid email:** AC-03 FAIL, "Element '#email-error' exists but is not visible". The hidden-text trap that the prior prototype's `text_content` check would have passed (from code review).
  - **Responsive layout:** AC-04 FAIL, "content width 960.0px exceeds viewport width 375.0px". The correct layout PASSes at 375 px.
  - Overall FAIL, exit 1.
- **Real BLOCKED:**
  - app killed before browsing (`app_unreachable`);
  - app process exits (`app_startup_failed`);
  - browser image missing; image without Playwright (`browser_unavailable`);
  - Docker unreachable;
  - `--sandbox none`;
  - unsupported profile `node-dev-server`;
  - false proofs (navigation-only, `screenshot` step, `evaluate`, absolute metadata URL);
  - timeout;
  - browser killed mid-run;
  - memory exhaustion (OOM-killed at a 768 MiB test limit).
- **Screenshots:** real PNGs for both PASS and FAIL. The two FAIL screenshots were kept as local-only evidence in `phase1d-live-evidence/`, which is not in the repository. Their hashes are listed in [phase1d-live-browser-results.md](phase1d-live-browser-results.md).
- **Network isolation:**
  - **Hostile navigation:** `escape.html` is BLOCKED (`unapproved_navigation`), and all 5 off-origin requests were blocked: `example.com`, `169.254.169.254`, `host.docker.internal:2375`, `192.168.65.254`, and the navigation itself.
  - **Docker-level probe:** from the browser container, every external, metadata, host and gateway target is unreachable and external DNS doesn't resolve.
  - **Ports:** no host ports are published (live `docker inspect` and `docker port` during the run).
- **Tampering:** one byte flipped in a copied screenshot gives `--check-report` exit 1. A screenshot that is replaced *and* re-hashed in `report.json` is still caught by the assertion-evidence binding (unit test).

## G. Evidence

| Run | Report dir (scratchpad) | Materialized snapshot | Integrity |
| :--- | :--- | :--- | :--- |
| correct | `verify_20261020_172934_b161ca_f6e090` | `cd2d8eb1…1355f` | exit 0 |
| broken | `verify_20261020_173037_b161ca_21cdd9` | `31cb25ee…a0f8` | exit 0 |
| hostile | `verify_20261020_173146_7c3573_95beb5` | `b9ee5388…5b6f` | exit 0 |

Broken run hashes:

- **AC-03:** screenshot `a0fbbb41164224a9…`; `browser-assertions.json` `7c33449eabd9e15f…`.
- **AC-04:** screenshot `1c13e7fc937200f0…`; `browser-assertions.json` `db40ad3f98b456eb…`.

Each browser criterion's evidence contains:

- `plan.json` (canonical; its SHA-256 is bound in the report and the manifest);
- `browser-assertions.json`;
- `screenshot.png`;
- `browser-console.log`, `browser-errors.log`, `browser-network.log`;
- `app-server.log`;
- `browser-run.json`, with images, runtime, network, effective configuration of both containers, and cleanup.

The screenshot metadata records criterion, assertion ID, snapshot digest, browser image identity and version, viewport and device scale factor, origin reference, capture time, relative path and SHA-256.

**Limitations of this evidence:**

- The bundles are **tamper-evident, not immutable**, and they are unsigned. An attacker who rewrites every file and every hash consistently can forge a bundle.
- The full report bundles are kept outside the repository, because they record host temp paths.

## H. Test results

The final code passed all four 1D suites. Commands were run from the 1D worktree, one module at a time, with `python -m unittest <module>`.

| Suite | Result |
| :--- | :--- |
| `tests.test_verifier_browser_plan` (new) | 19 passed |
| `tests.test_verifier_browser_security` (new) | 26 passed |
| `tests.test_verifier_browser_evidence` (new) | 14 passed |
| `tests.test_verifier_browser_integration` (new, **real Docker + Chromium**, nothing mocked) | 12 passed (325.8 s) |
| **Phase 1D total** | **71 passed, 0 failed, 0 skipped** |
| `test_standalone_verifier` | 21 passed |
| `test_verifier_execution_integrity` | 17 passed |
| `test_verifier_evidence_integrity` | 18 passed |
| `test_verifier_test_result_semantics` | 8 passed |
| `test_verifier_sandbox_execution` | 5 passed |
| `test_verifier_phase1c_blockers` | 28 passed, 1 skipped (symlink creation not permitted on this host) |
| `test_verifier_docker_integration` (real Docker) | 10 passed, 1 skipped (same symlink reason) |
| `test_runtime_boundary`, `test_git_guard`, `test_phase6b_cli_verification`, `test_pr_proof`, `test_proof_discovery_ux`, `test_cli_runtime_unification`, `test_cli_workspace_verification`, `test_runtime_permissions` | 3, 20, 15, 12, 15, 7, 7, 5 passed |
| `test_proof_inspection_ux` | 30 passed, **2 errors**: `test_11_repl_proof_aliases` (×2). These are pre-existing: reproduced identically on a clean `git archive HEAD` export during Phase 1C. Unrelated to the verifier, and left unmodified. |

**Run order:** the Phase 1B/1C regression run started after the verifier and CLI integration were finished. The later edits touched only `browser_runner.py` and `browser_sandbox.py` (browser-only paths); the four 1D suites and all CLI demos were re-run after those edits.

**Static and hygiene checks:**

- **`git diff --check`:** clean (exit 0) for tracked files.
- **Untracked files:** no trailing whitespace in new Phase 1D code, tests or docs. Hits remain only in the transferred Phase 1B/1C documents, which are byte-identical to the 1C worktree and were left as-is.
- **Secret scan:** `python -m security.git_guard --file <f>` was run on each of the 91 modified and untracked files outside `.brainfrog/`, and on every text file inside it, giving **0 findings**. A planted positive control was detected (exit 1). Nothing is staged, so a `--staged` scan would cover nothing.
- **Docker inventory after all runs:**
  - containers, networks, volumes and images are identical to the baseline; the only change is the relative "Exited … ago" text of `adstracker-db`;
  - 0 BrainFrog-owned containers or networks remain;
  - no `bf_browser_*` or `bf_snapshot_*` temp directories remain.

## I. Remaining limitations

1. **Chromium internal sandbox is off.** A renderer exploit would run as UID 65532 inside the hardened browser container. That container has no capabilities, a read-only root filesystem, no writable host mounts, and network access to the app only. A reviewed custom seccomp profile is the path forward.
2. **App code is not executed server-side** in `python-static`. Profiles that run repository code (Node, Django, …) are not implemented and are BLOCKED as unsupported.
3. **Page JavaScript controls its own DOM.** A deliberately deceptive app can render whatever text satisfies a DOM assertion. Assertions prove observable behaviour in this browser, not the intent behind it, and passing checks do not prove every human-written requirement.
4. **Blocked sub-resource requests** are recorded but don't change the verdict. Only navigations away from the origin BLOCK.
5. **Reports are unsigned** and tamper-evident only. A screenshot is evidence, never proof.
6. **Image provenance:** the browser image was built locally, and Playwright came from PyPI without a hash pin. A rebuild changes the image ID and must be re-approved.
7. **The Phase 1C limitation remains.** Adversarial Python test code that shares the harness process can forge unit-test results. Browser criteria do not share that weakness, because the runner is BrainFrog code in a separate container from the application.
8. **Not tested:** the installed `bf` executable (only `python cli.py`), cross-browser, framework apps, and non-Windows hosts.
9. **Cost:** each browser criterion uses its own network and two containers, roughly 5–8 s each on this host.

## J. Phase 1E readiness

**Is Phase 1D complete?** Yes for its stated scope: one robust Chromium path, the static-app profile, declarative assertions, screenshot evidence, isolation and integrity.

**Ready for CI or PR integration:**

- the `python cli.py --spec` verdict and exit codes (0, 1 and 2);
- `report.json` schema `brainfrog.verification-report/1d`;
- `--check-report`;
- deterministic BLOCKED behaviour when Docker or the images are missing.

CI runners would need Docker and the approved images to be pre-provisioned.

**Gaps before release claims:**

- Chromium sandbox under a reviewed seccomp profile;
- hash-pinned image build and re-approval workflow;
- an approved profile for at least one server-side framework;
- signing or anchoring of reports;
- a test of the installed `bf` entry point;
- a decision on whether blocked sub-resources should affect verdicts.

Phase 1E was not started.
