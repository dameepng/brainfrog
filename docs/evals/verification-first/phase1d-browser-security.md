# Phase 1D — Browser Verification Security

> **Correction (2026-10-21, security closure):** Four parts of this document are superseded. See [phase1d-final-security-closure.md](phase1d-final-security-closure.md).
>
> - **"Chromium's internal sandbox is not active"** — it is now active. The browser container runs with BrainFrog's pinned seccomp profile: Docker's default plus `clone`/`unshare`/`chroot`. Every PASS or FAIL requires `/proc` proof that each renderer is sandboxed.
> - **Browser image** — now `brainfrog-browser-runner:v1.64.0`, built from `docker/browser-runner/` with hash-locked dependencies.
> - **Network alias** — `app` is replaced by `bf-app`.
> - **Manual build instructions** — superseded by `docker/browser-runner/Dockerfile`.

## Container controls (both containers)

Each control below is applied by the fixed `docker create` vector and confirmed with `docker inspect` before the container starts. Any mismatch is BLOCKED.

| Control | App container | Browser container |
| :--- | :--- | :--- |
| User | `65532:65532` | `65532:65532` (image default `pwuser` overridden) |
| Root filesystem | `--read-only` | `--read-only` |
| Capabilities | `--cap-drop=ALL`, none added | `--cap-drop=ALL`, none added |
| Privilege escalation | `no-new-privileges` | `no-new-privileges` |
| seccomp / AppArmor | Docker defaults. `*=unconfined` and `label=disable` are rejected. | same |
| Privileged / devices | no / none | no / none |
| Memory (swap = memory) | 256 MiB | 1536 MiB |
| CPU | 1.0 | 2.0 |
| PIDs | 64 | 256 |
| IPC | private | private, `--shm-size=256m` |
| Writable paths | tmpfs `/tmp` (16 MiB, `noexec,nosuid,nodev`) | tmpfs `/tmp` (256 MiB) and `/evidence` (32 MiB), both `noexec,nosuid,nodev` |
| Read-only binds | `/app` (snapshot), `/bf-harness` (launcher + nonce) | `/bf-harness` (runner), `/bf-input` (plan) |
| Host write mounts | none | **none**: evidence returns as hash-checked records |
| Image | Phase 1C allowlist (`python:3.12-slim`) resolved to an ID; `--pull=never` | Pinned ID `sha256:a880ad52…48dc4`; tag drift is BLOCKED; `--pull=never` |
| Network | owned internal network, alias `app` | same network, no alias |
| Published ports / extra hosts | none / none | none / none |
| Docker socket | not mounted (also checked) | not mounted (also checked) |
| Environment | `HOME`, `PYTHONDONTWRITEBYTECODE`, `PYTHONUNBUFFERED`, plus the image's own `Config.Env` keys | `HOME`, `PYTHONDONTWRITEBYTECODE`, plus the image's own keys (`PATH`, `PLAYWRIGHT_BROWSERS_PATH`) |
| Restart / logging | `no` / json-file 1 MiB (needed for the readiness record) | `no` / `none` |
| Timeout | profile startup 20 s | plan budget + 75 s (≤ 300 s); killed on expiry |

Live evidence of these controls:

- `effective_container_config` (`app`, `browser`) is recorded in `report.json` for every executed criterion.
- The integration tests assert user, read-only root filesystem, `cap_drop`, `no-new-privileges`, the read-only mounts, memory and PID limits, and the shared private network.
- An in-container probe confirmed UID 65532.
- A memory-exhaustion page was OOM-killed at a 768 MiB test limit and reported BLOCKED (`resource_limit_termination`).

### Chromium's internal sandbox is not active (deliberate, documented)

Chromium's own renderer sandbox can't start under this container policy. Its namespace sandbox needs `clone`/`unshare` with namespace flags, which Docker's default seccomp profile blocks without `CAP_SYS_ADMIN`. Its setuid fallback is blocked by `no-new-privileges`. Enabling it would mean weakening one of those container controls, for example with `seccomp=unconfined`, which is what the prior attempt did.

**Decision:** keep every container control and launch Chromium with `chromium_sandbox=False`. Every evidence bundle records this as `chromium_internal_sandbox: false`.

**Consequence:** a renderer exploit runs as UID 65532 inside the hardened browser container. That container has no capabilities, a read-only root filesystem, no host mounts it can write to, and network access to the app container only. The container is the containment boundary; Chromium's per-process sandbox is not an additional layer here.

This is a residual risk for adversarial web content and should be revisited before untrusted-internet use. One option is a reviewed custom seccomp profile that adds only the namespace syscalls Chromium needs.

## Network boundary (layered)

1. **Docker:** a fresh `--internal` bridge network for each criterion, owned and labelled, checked with `docker network inspect` (internal, bridge, labels, members). Only the app container and the browser container join it. Nothing is published.

   Measured from the browser container (integration test 04):
   - the app is reachable;
   - every one of these is unreachable: `1.1.1.1:80`, `8.8.8.8:53`, `169.254.169.254:80`, `192.168.65.254:80`, `172.17.0.1:80`, and the network gateway on ports 80 and 2375;
   - DNS for `example.com`, `host.docker.internal` and `gateway.docker.internal` does not resolve.
2. **Chromium:** `--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE app` stops the browser resolving any name other than the app.
3. **Playwright:** context-level `route("**/*")` allows only exact `http://app:8080` (scheme, host and port all compared). Everything else is aborted and recorded. Service workers are blocked, so they can't bypass interception. A request listener also records redirected and off-origin requests.
4. **Plan validation:** absolute URLs, schemes and traversal are rejected before anything runs.

Request interception by itself is **not** OS-level isolation. Layer 1 is the OS-level boundary.

**Verdict policy:**

- An attempted **navigation** to an unapproved origin, or a page that ends up on one, is **BLOCKED** (`unapproved_navigation`).
- Blocked **sub-resource** requests are recorded (`blocked_requests` in `browser-assertions.json`, plus `browser-network.log`), and the plan's own assertions decide the verdict.

## Inputs the spec cannot control

The spec cannot control any of the following:

- images or tags;
- container arguments, capabilities, mounts, network, environment or limits;
- the app startup command, which only an approved profile ID selects;
- the application origin;
- JavaScript or Python executed in the browser or the runner;
- timeouts above the bounds.

All of these are operator/API-only constructor parameters, and the tests exercise them.

## Evidence channel and sensitive data

The runner writes evidence to its tmpfs, then streams each file as a base64 record carrying its SHA-256 and the per-run nonce. The host then enforces:

- an allowlist of five file names, each with a size cap (a screenshot can be at most 8 MiB);
- per-file hash checks;
- a declared-versus-received file set check;
- a single completion record.

On the sensitive-data side:

- Input values typed by `fill` are never written to evidence; only their length is.
- Password inputs are masked in screenshots (Playwright `mask`).
- Browser storage, cookies and headers are not persisted.
- `.log` evidence passes through BrainFrog's secret scrubber before hashing.
- Demonstrations use synthetic values only.

## Image provenance and manual installation

BrainFrog never pulls or builds images. The approved browser image was built locally by the operator before this phase. To reproduce it on another host, run these commands manually:

```powershell
docker pull mcr.microsoft.com/playwright/python:v1.49.1-noble
# Dockerfile:
#   FROM mcr.microsoft.com/playwright/python:v1.49.1-noble@sha256:42e251ec3238672c931be6c3565ff279690ae072c3e22058128d0412494f6866
#   RUN pip install --no-cache-dir playwright==1.49.1
docker build -t brainfrog-browser-runner:v1.49.1 .
docker image inspect brainfrog-browser-runner:v1.49.1 --format "{{.Id}}"
```

A rebuild produces a **different image ID**. The operator must review it and update `APPROVED_BROWSER_IMAGES` in `core/runtime/browser_sandbox.py`; otherwise verification is BLOCKED (`image_not_approved`).

The Playwright package itself came from PyPI at build time. Its integrity relies on pip's index trust; no hash pin was used. That is a provenance gap to close with a hash-pinned requirements file.

## Cleanup and unrelated resources

Cleanup only ever runs these commands, each against a specific owned ID after a label check:

- `docker rm --force <id>`
- `docker network rm <id>`

It never runs `prune`, never stops a container it doesn't own, and never reuses a name without checking labels.

The integration suite compares the full set of non-BrainFrog container and network IDs before and after (test 99). Every test also asserts that no `io.brainfrog.phase=1d` resource remains. The session-level Docker inventory comparison is in the final report.
