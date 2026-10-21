# Phase 1D — Architecture: Isolated Browser Verification

> **Update (2026-10-21):** The browser image is now `brainfrog-browser-runner:v1.64.0`, and the app alias and origin are now `bf-app` / `http://bf-app:8080`. The browser container uses the pinned seccomp profile `core/runtime/browser_seccomp_chromium.json`, which enables Chromium's renderer sandbox. See [phase1d-final-security-closure.md](phase1d-final-security-closure.md).

## Overview

```text
spec.md ──parse──> BrowserPlan (strictly validated, canonical JSON, sha256)
                         │
repository ──materialize──> snapshot dir (Phase 1C manifest + digest)   [one per run]
                         │
   per browser criterion │
                         v
  docker network create --internal  bf1d_net_<token>      (owned, labelled)
        │
        ├── Container A: application    bf1d_app_<token>
        │     image   python:3.12-slim  (pinned by resolved ID, --pull=never)
        │     argv    approved profile only: python -I -B /bf-harness/app_launcher.py ...
        │     mounts  /app        <- snapshot (read-only)
        │             /bf-harness <- app_launcher.py + nonce (read-only)
        │     alias   "app" on the private network; no published ports
        │     emits   @@BF_APP@@ {source_digest, listening} (nonce-bound, via container log)
        │
        └── Container B: browser        bf1d_brw_<token>
              image   brainfrog-browser-runner:v1.49.1 (ID must equal pinned sha256:a880ad52…)
              argv    python3 -I -B /bf-harness/browser_runner.py --plan /bf-input/plan.json
                      --origin http://app:8080 ...
              mounts  /bf-harness <- browser_runner.py (read-only)
                      /bf-input   <- canonical plan.json (read-only)
              tmpfs   /tmp, /evidence (noexec)
              stdin   per-run nonce
              stdout  @@BF_BROWSER@@ {started, artifact×N (base64 + sha256), complete}
                         │
                         v
  host: validate records → recompute verdict from step records → write evidence
        → remove owned containers → remove owned network → report.json/report.md
```

## Components

| Module | Runs where | Responsibility |
| :--- | :--- | :--- |
| `core/runtime/browser_plan.py` | host | Strict schema, bounds, canonical JSON, plan hash |
| `core/runtime/app_profile.py` | host | Immutable registry of trusted app startup profiles (`python-static`) |
| `core/runtime/app_launcher.py` | app container | Re-hashes the mounted snapshot; static file server (GET/HEAD only, no listings, no symlinks) |
| `core/runtime/browser_runner.py` | browser container | Headless Chromium via Playwright; declarative steps; assertions; screenshot; nonce-bound records |
| `core/runtime/browser_sandbox.py` | host | Network and container lifecycle, effective-config verification, record validation, host-side verdict recomputation, ownership-checked cleanup |
| `core/verifier.py` | host | Spec parsing, snapshot, criterion dispatch, evidence files, report, offline integrity checks |

Phase 1C modules (`sandbox.py`, `verification_harness.py`, `command_policy.py`) are unchanged. `browser_sandbox.py` reuses these Phase 1C pieces:

- `_run_bounded`;
- `DockerSandboxBackend._inspect`, `remove_owned_container`, `_summarize_config` and `_host_path`;
- the Phase 1C image allowlist, for the app image.

## Lifecycle per browser criterion

1. **Validate and resolve.** Check the criterion ID and app profile, confirm the Docker engine is reachable, resolve the browser image (its ID must equal the pinned ID), and resolve the app image (Phase 1C allowlist).
2. **Stage harness.** Create a fresh temp directory holding:
   - `app_launcher.py` and a nonce file;
   - `browser_runner.py`;
   - the canonical `plan.json`.
3. **Create the network.** `docker network create --internal --driver=bridge` with ownership labels. Then `docker network inspect` must show `Internal=true`, bridge driver, our labels, and no containers.
4. **Create the app container.** `docker create` with a fixed hardened vector, then `docker inspect` checks:
   - user, read-only root filesystem, capabilities and no-new-privileges;
   - no unsafe security options;
   - memory, CPU and PID limits;
   - only the expected read-only mounts and only the `/tmp` tmpfs;
   - no port bindings, no extra hosts;
   - the exact network and the `app` alias;
   - argv equal to the approved profile;
   - environment limited to an allowlist;
   - the expected image ID.

   Any violation is BLOCKED and nothing starts.
5. **Start the app and wait for readiness.** Poll the container log for the launcher's nonce-bound `source_digest` and `listening` records. The observed digest must equal the materialized snapshot digest.
6. **Create the browser container.** Same inspection, plus the shm size and `/evidence` tmpfs. The network is inspected again: its only member must be the app container.
7. **Run the browser.** `docker start --attach --interactive`, with the nonce on stdin and a timeout derived from the plan budget. On timeout the container is killed.
8. **Interpret the results.** See [phase1d-assertion-semantics.md](phase1d-assertion-semantics.md):
   - nonce binding, exactly one completion record, and artifact allowlist, size and hash checks;
   - manifest binding: criterion ID, plan hash, snapshot digest, browser image ID, runner status;
   - host-side verdict recomputation.
9. **Re-hash the snapshot.** The snapshot must still have the same digest after the run.
10. **Clean up** in `finally`, so it runs on success, failure, timeout, exception and `KeyboardInterrupt`:
    - remove the browser container, then the app container, each only if its labels match this criterion's run label;
    - remove the network only if its labels match;
    - list any remaining resources with this run label.

    Anything left over makes the criterion BLOCKED (`container_cleanup_failed`).

## Ownership labels

Every container and network carries:

```text
io.brainfrog.owner=brainfrog-verifier      (Phase 1C owner label)
io.brainfrog.run=<run_id>.<criterion>.<token>
io.brainfrog.phase=1d
io.brainfrog.role=network|app|browser
io.brainfrog.criterion=<criterion id>
```

Phase 1D uses the Phase 1C namespace (`io.brainfrog.*`) rather than the `brainfrog.owner=verification` example in the brief, so that one set of ownership rules covers both phases. Cleanup never uses `prune`, never acts on a name without checking its labels, and never touches resources from another run.

## Trust boundaries

| Boundary | Trusted side | Untrusted side | Enforcement |
| :--- | :--- | :--- | :--- |
| Spec → verifier | BrainFrog parser | spec.md content | Strict declarative schema; profile and image cannot be specified; no code |
| Host → app container | BrainFrog | repository files (served as static content) | Phase 1C container controls; read-only snapshot; no host ports |
| App → browser | browser_runner (BrainFrog code) | web page content (HTML, CSS, JavaScript from the repository) | Separate container; private network; Chromium host-resolver rules; request interception; layout measured via CDP |
| Browser container → host | BrainFrog host | runner output | No host write mounts; nonce-bound records; artifact allowlist, size and hash checks; host-side verdict recomputation |
| Report → reader | `--check-report` | bundle on disk | SHA-256 manifest plus cross-binding checks (tamper-evident, **not** tamper-proof) |

## Interaction with Phase 1C criteria

Browser criteria share the run's single materialized snapshot with `test` and `build` criteria. Mixed specs work: the live demo combined 4 browser criteria, 1 unit-test criterion and 1 build criterion. The overall verdict rules are unchanged:

- any FAIL gives FAIL (exit 1);
- otherwise any BLOCKED or NOT_RUN gives BLOCKED (exit 2);
- otherwise PASS (exit 0).

There is no host-browser fallback in any trust mode. `--trust-local-code` authorizes host execution of test commands only, never host browsing.
