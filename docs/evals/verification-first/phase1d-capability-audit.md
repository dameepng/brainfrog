# Phase 1D — Capability Audit

**Date:** 2026-10-20/21
**Worktree:** `<PHASE_1D_WORKTREE>` (detached at `c78e798`, uncommitted)
**Scope:** source transfer from Phase 1C, the earlier Phase 1D attempt that was already in this worktree, and discovery of the browser runtime.

## 1. Workspaces

| Worktree | HEAD | Role | Touched in Phase 1D? |
| :--- | :--- | :--- | :--- |
| `agentic_dev` | `c78e798` | original development workspace (protected) | No |
| `brainfrog-vf-1b-closure` | `c78e798` | Phase 1B closure (protected) | No |
| `brainfrog-vf-1c-execution` | `c78e798` | verified Phase 1C (protected) | No |
| `brainfrog-vf-1d-browser` | `c78e798` | Phase 1D (this work) | Yes |

Before any edit, I recorded the porcelain status and per-file SHA-256 of every modified or untracked file in the three protected worktrees, plus each `git diff` hash. The comparison after the work is in [phase1d-final-report.md](phase1d-final-report.md).

### The existing Phase 1D worktree

`brainfrog-vf-1d-browser` already existed when this work started. It held an **earlier, unfinished Phase 1D attempt** by another agent:

- browser modules;
- four pytest suites;
- two HTML fixtures;
- two demo reports under `.brainfrog/reports/`;
- no Phase 1D documents.

It was last modified about 1.5 hours earlier. The operator chose to **continue in that worktree** rather than create another one.

Before changing anything, I backed up every file of the prior attempt (with hashes) to the session scratchpad. The prior attempt's two reports were left untouched. Its `.brainfrog/workspace.lock` was also left untouched; its owning process is unknown.

## 2. Phase 1C source transfer (verified)

The prior attempt had copied the Phase 1C state into this worktree. I verified that copy byte-for-byte against the protected Phase 1C worktree, using SHA-256 prefixes:

| File | 1C | 1D | Result |
| :--- | :--- | :--- | :--- |
| `core/runtime/sandbox.py` | `7d3f2859f56d` | `7d3f2859f56d` | identical (unchanged by 1D) |
| `core/runtime/verification_harness.py` | `4335f1be6a59` | `4335f1be6a59` | identical (unchanged by 1D) |
| `core/runtime/command_policy.py` | `11dd08d9df97` | `11dd08d9df97` | identical (unchanged by 1D) |
| `core/verifier.py` | `e46d9d5a8452` | (1D) | 1C content plus additive 1D browser integration |
| `cli.py` | `96f910aec342` | `f2c01f84c361` | 1C content plus the `--app-profile` option |
| 7 Phase 1B/1C test suites | — | — | all identical |
| 20 Phase 1B/1C evaluation documents | — | — | all identical |

The two remaining `.brainfrog/` items were handled as follows:

- **`phase1c-live-evidence/` was not transferred.** It contains local user-profile paths.
- **`.brainfrog/workspace.lock` was not removed**, in either the 1C or the 1D worktree.

## 3. Audit of the prior Phase 1D attempt

The prior attempt produced a PASS report for its demo. On inspection it had defects that made browser verdicts untrustworthy:

| # | Defect in prior attempt | Consequence | Phase 1D fix |
| :--- | :--- | :--- | :--- |
| 1 | Browser container ran with `--security-opt seccomp=unconfined` | Disabled a core container protection | Default seccomp profile only. Unconfined seccomp, AppArmor or SELinux settings are detected by `docker inspect` and BLOCK the run. |
| 2 | App container ran as root, with a writable root filesystem, plain `-v` mounts and no `--pull=never` | Weak containment of untrusted app code | Same hardened control set as Phase 1C, checked by `docker inspect` before start |
| 3 | No effective-config verification, and cleanup didn't check ownership | Could remove resources it doesn't own; no proof that controls applied | Fixed create vectors, inspection before start, cleanup only after a label check |
| 4 | Readiness probes started unlabeled, unhardened `docker run --rm` containers | Untracked, unhardened resources | The app launcher emits a nonce-bound readiness record; there are no probe containers |
| 5 | A runner timeout killed only the CLI; the `--rm` container kept running | Leaked browser containers | Create, then attach. The container is killed on timeout, and cleanup always runs in `finally`. |
| 6 | Missing assertion manifest plus exit 0 gave **PASS** | False PASS | Missing or invalid records are BLOCKED; the verdict is recomputed on the host |
| 7 | Origin allowlist used `startswith("http://app:8080")` | `http://app:8080.evil/` would be allowed | Exact scheme, host and port comparison |
| 8 | `expect_text` read `text_content`, which includes hidden text | Hidden error text would PASS (from code review; the prototype was not re-run). The new broken fixture uses exactly this trap, and the new runner FAILs it. | Requires exactly one **visible** element and checks `inner_text` |
| 9 | Every assertion exception became FAIL | A browser crash would read as a functional defect | Timeout or assertion mismatch is FAIL; crash, closed target or network error is BLOCKED |
| 10 | `InfraErrorCategory.INVALID_SPEC` was referenced but doesn't exist | Any invalid browser plan crashed the verifier with `AttributeError` | Explicit `invalid_browser_plan` category |
| 11 | `observed_source_snapshot_digest` was copied from the manifest | Snapshot binding was asserted, not observed | The app container re-hashes its mounted source and reports the digest with a nonce |
| 12 | The demo served the entire BrainFrog repository over HTTP, with a credential-like literal in the spec | Overexposure; violated the repository's fixture-hygiene rule | Standalone disposable fixture repositories, synthetic values only |
| 13 | Overflow measured by page JavaScript (`page.evaluate`) | The page under test could forge the metrics | Chrome DevTools Protocol `Page.getLayoutMetrics`, computed by the engine |
| 14 | Selector strings passed to Playwright unrestricted (`text=`, `xpath=`, `>>` engines) | A broader surface than declarative CSS | CSS only, forced through the `css=` engine |

I replaced the prior code rather than patching it. The prior tests were rewritten in the repository's `unittest` style, matching the Phase 1C suites.

## 4. Browser runtime discovery

Facts below were observed on this host, not assumed.

| Item | Observed |
| :--- | :--- |
| Docker | Engine 29.1.3, context `desktop-linux`, Linux containers on WSL2, `linux/amd64` |
| Official Playwright image | `mcr.microsoft.com/playwright/python:v1.49.1-noble`, digest `sha256:42e251ec3238672c931be6c3565ff279690ae072c3e22058128d0412494f6866`. Contains Chromium but **not** the `playwright` Python package (`find_spec('playwright') → None`). |
| Derived image (approved by the operator) | `brainfrog-browser-runner:v1.49.1`, ID `sha256:a880ad52ee02b240aaacf5bedcd61daf3c00fdc8b97303ca1521b7df8ab48dc4`. Built locally on 2026-10-20 from the official image plus `pip install playwright==1.49.1`. |
| Playwright (Python) | 1.49.1 at `/usr/local/lib/python3.12/dist-packages` (system site-packages, so it works with `python -I`) |
| Chromium | 131.0.6778.33 (`chromium-1148`; also `chromium_headless_shell-1148`) |
| Non-root | Image user `pwuser` (1001); BrainFrog runs it as `65532:65532` with no problems |
| Hardened launch | Chromium launches headless under a read-only root filesystem, `--cap-drop=ALL`, `no-new-privileges`, the default seccomp profile, non-root, and a `noexec` tmpfs at `/tmp` |
| Chromium internal sandbox | **Cannot start** under that policy (`chromium_sandbox=True` → "Target page, context or browser has been closed"). Its namespace sandbox needs `clone`/`unshare` namespace flags, which Docker's default seccomp profile blocks, and the setuid helper is blocked by `no-new-privileges`. See the security doc. |
| Internal networks | `docker network create --internal` is supported. From a container on it, the app peer is reachable by DNS alias; external DNS names don't resolve (`host.docker.internal`, `gateway.docker.internal`, `metadata.google.internal`, `example.com`); external, metadata and Docker Desktop host IPs give "Network is unreachable"; no host ports are published. |
| App image | `python:3.12-slim`, `sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1` (the Phase 1C approved image) |

Nothing was pulled, installed, or reconfigured. Docker Desktop settings were not touched.

## 5. Credentials

No TypeSafe or LLM credential is used by any Phase 1D path. Containers receive only BrainFrog-chosen environment variables. Each image's own baked-in `Config.Env` keys are also permitted, and are checked by `docker inspect`.
