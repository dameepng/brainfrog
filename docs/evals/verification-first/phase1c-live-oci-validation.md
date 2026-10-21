# BrainFrog Verification-First Phase 1C — Live OCI Validation

**Date:** 2026-10-20
**Worktree:** `<PHASE_1C_WORKTREE>` (detached HEAD `c78e798`, uncommitted)
**Scope:** Real Docker execution of `bf verify` through BrainFrog's own backend. No direct `docker run` result is used as evidence of verifier behaviour.

---

## 1. Runtime environment (observed, read-only checks)

| Property | Observed value |
| :--- | :--- |
| Docker Engine | `29.1.3` (`linux/amd64`) |
| Docker context | `desktop-linux` |
| Default runtime | `runc`; cgroup v2; security options `seccomp (builtin)`, `cgroupns` |
| Approved image tag | `python:3.12-slim` (operator allowlist in `core/runtime/sandbox.py`) |
| Resolved image ID | `sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1` |
| Repo digest | `python@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1` |
| Platform | `linux/amd64`, available locally, never pulled (`--pull=never`) |
| `python:3.11-slim` (also allowlisted) | **absent** locally; used to exercise the missing-image path |

The tag `python:3.12-slim` is mutable. BrainFrog resolves it once per run with `docker image inspect` and creates every container from the immutable image **ID**. `docker inspect` then confirms the container's `Image` field matches the resolved ID. A mismatch blocks execution.

## 2. Container lifecycle as executed

For each executable criterion, `DockerSandboxBackend`:

1. `docker create` with a fixed, BrainFrog-built argument vector (below). The only spec-derived part is the policy-validated runner arguments, which are placed *after* the image ID.
2. `docker container inspect` checks the **effective** configuration. Any violation blocks execution before any repository code runs (`security_policy_not_enforced`).
3. `docker start --attach --interactive` writes a one-time nonce on stdin, under a bounded timeout. Captured output is capped at 1 MiB per stream.
4. `docker container inspect` reads the exit code and `OOMKilled`.
5. `docker rm --force`, but only after confirming the container carries `io.brainfrog.owner=brainfrog-verifier` **and** `io.brainfrog.run=<this run id>`. Removal runs in a `finally` block, so it also happens on timeout and cancellation. If removal fails, the result becomes BLOCKED (`container_cleanup_failed`).

Actual argument vector recorded in the evidence (AC-01, default CLI run):

```text
docker create --interactive --pull=never --name=bf_verify_<run>_<rand>
  --label=io.brainfrog.owner=brainfrog-verifier --label=io.brainfrog.run=<run>
  --network=none --read-only --cap-drop=ALL --security-opt=no-new-privileges
  --user=65532:65532 --pids-limit=64 --memory=512m --memory-swap=512m --cpus=2.0
  --ipc=private --restart=no --log-driver=none
  --mount=type=bind,source=<%TEMP%>\bf_snapshot_*\source,target=/source,readonly
  --mount=type=bind,source=<%TEMP%>\bf_harness_*,target=/bf-harness,readonly
  --tmpfs=/workspace:rw,nosuid,nodev,noexec,size=256m,uid=65532,gid=65532,mode=0700
  --tmpfs=/tmp:rw,nosuid,nodev,noexec,size=64m,uid=65532,gid=65532,mode=0700
  --workdir=/workspace --env=HOME=/tmp --env=PYTHONDONTWRITEBYTECODE=1
  sha256:a6e34c59...  python -I -B /bf-harness/verification_harness.py unittest
  --nonce-stdin --source /source --workspace /workspace -- discover -s tests_pass -t .
```

### Effective configuration (from `docker inspect`, `evidence/AC-01/container_config.json`)

```json
{
  "user": "65532:65532", "network_mode": "none", "readonly_rootfs": true, "privileged": false,
  "security_opt": ["no-new-privileges"], "cap_drop": ["ALL"], "cap_add": null,
  "memory_bytes": 536870912, "memory_swap_bytes": 536870912, "nano_cpus": 2000000000,
  "pids_limit": 64, "pid_mode": "", "ipc_mode": "private",
  "mounts": [{"destination": "/source", "rw": false, "type": "bind"},
             {"destination": "/bf-harness", "rw": false, "type": "bind"}],
  "tmpfs": {"/tmp": "rw,nosuid,nodev,noexec,size=64m,uid=65532,gid=65532,mode=0700",
            "/workspace": "rw,nosuid,nodev,noexec,size=256m,uid=65532,gid=65532,mode=0700"},
  "env_keys": ["GPG_KEY", "HOME", "LANG", "PATH", "PYTHONDONTWRITEBYTECODE", "PYTHON_SHA256", "PYTHON_VERSION"],
  "image_id": "sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1"
}
```

`GPG_KEY`, `LANG`, `PATH` and `PYTHON_*` come from the image's own `ENV`. Inside the container, Docker also sets `HOSTNAME` to the container ID. No host variable is forwarded: the scrubbed host environment passed to `execute()` is deliberately ignored by the Docker backend.

## 3. In-container security probes (real execution)

These probes run inside the sandbox as ordinary unittest cases (`tests/test_verifier_docker_integration.py::test_isolation_properties_inside_container`), executed through `StandaloneVerifier` → `DockerSandboxBackend`. Observed result: **PASS, 11/11 probe tests executed**. If any isolation property had failed, the criterion would have been FAIL.

| # | Property | How it was demonstrated | Result |
| :-- | :--- | :--- | :--- |
| 1 | Container cannot write host repo | Writes to `/source/README.md` and `/source/new_file.txt` raise `OSError`. Host repo byte-compared before/after: identical. | ✅ |
| 2 | Private host sentinel inaccessible | Sentinel file outside the repo, plus a git-ignored sentinel inside it. Neither content appears anywhere under `/source`, `/workspace`, `/tmp`. | ✅ |
| 3 | Synthetic host credential not inherited | `BF_SYNTHETIC_CREDENTIAL` set in the host process. Absent from `/proc/self/environ`. Env keys limited to image/runtime defaults plus `HOME` and `PYTHONDONTWRITEBYTECODE`. | ✅ |
| 4 | Network disabled | `/sys/class/net` = `['lo']`. TCP connect to `1.1.1.1:53` fails. DNS resolution fails. | ✅ |
| 5 | Non-root | `os.getuid() == os.getgid() == 65532` | ✅ |
| 6 | Read-only root filesystem | Writes to `/etc`, `/usr`, `/` raise `OSError`. `/` mounted `ro`. | ✅ |
| 7 | Ephemeral writable workspace | Two criteria, two containers. A marker written to `/workspace` and `/tmp` by the first is absent in the second. `/tmp` is `noexec` (`execv` → `PermissionError`). | ✅ |
| 8 | Docker socket not mounted | `/var/run/docker.sock`, `/run/docker.sock`, `/host`, `/mnt/c` absent | ✅ |
| 9 | Mounts match policy | `/proc/self/mounts`: `/source` ro, `/bf-harness` ro, `/` ro, tmpfs `/workspace` and `/tmp`. Host-side inspect check rejects any other mount. | ✅ |
| 10 | Resource limits active | cgroup `memory.max=536870912`, `pids.max=64`, `cpu.max` quota/period = 2.0 | ✅ |
| 10b | Capabilities / no-new-privs | `CapEff=0`, `CapBnd=0`, `NoNewPrivs=1` | ✅ |
| 11 | Timeout terminates owned container | Test sleeping 120 s with an 8 s limit → BLOCKED `timeout`. Container no longer exists. | ✅ |
| 12 | Cancellation leaves no orphan | `KeyboardInterrupt` injected after a real `docker create`. Owned-container listing for that run label is empty afterwards. | ✅ |
| 13 | Only current-run resources cleaned | Decoy container with a different run label: `remove_owned_container` refuses it, and it survives until the test removes it with its own label. | ✅ |
| 14 | No host fallback on sandbox failure | Unusable Docker CLI → BLOCKED `docker_unavailable`. A host-marker test that would write a file if executed on the host left no file. | ✅ |
| 15 | Spec cannot alter security | Spec with `**Image:** alpine:latest` and `--network=host --privileged` runner args. Image field ignored; args rejected by unittest (BLOCKED `runner_usage_error`). Effective config still `network=none`, unprivileged, resolved image ID. | ✅ |
| + | Memory limit enforcement | Allocation loop → `OOMKilled` → BLOCKED `resource_limit_termination` | ✅ |
| + | Invalid runtime limit | `--memory=1m` rejected by engine → BLOCKED `container_create_failed` | ✅ |
| + | Missing image | `python:3.11-slim` → BLOCKED `image_missing`. Image still absent afterwards (no pull). | ✅ |

Not demonstrated on this host: **symlink escape**. Creating a symlink on this Windows host requires a privilege the session does not hold (`WinError 1314`), so that test was skipped. The same containment code path was exercised with a Windows **directory junction** pointing outside the repository (`test_junction_escape_not_followed`, passed). The snapshot builder never follows or copies links.

## 4. Live CLI demonstration (section 8)

The disposable repository was created in the session scratchpad, outside every protected worktree. The demo was driven through the actual CLI (`python cli.py --repo <demo> --spec …`), and transcripts and copied report directories are in `phase1c-live-evidence/`.

### Default CLI run (`--sandbox auto` default → OCI)

```text
$ python cli.py --repo <demo> --spec spec.md        # exit code 1
Workspace digest (pre):  0ab26d80...   Executed snapshot: f93a5d9a... (15 files)
Image:     python:3.12-slim -> sha256:a6e34c598f24...
Trust:     ISOLATED_SANDBOX (oci_container_docker)
   AC-01   PASS     Real passing unit test (1 executed)
   AC-02   FAIL     Real failing unit test (1 executed)
   AC-03   BLOCKED  Zero-test prevention (0 executed)
   AC-04   BLOCKED  Fake output resistance (0 executed)
   AC-05a  PASS     Python compilation (valid)
   AC-05b  FAIL     Python compilation (invalid)
Overall: FAIL
```

| Criterion | Verdict | test_count | failures | backend | container (short) | runner-observed digest = materialized |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| AC-01 real passing test | **PASS** | 1 | 0 | oci_container_docker | `62844105d5fb` | ✅ |
| AC-02 real failing assertion | **FAIL** | 1 | 1 | oci_container_docker | distinct | ✅ |
| AC-03 zero tests | **BLOCKED** | 0 | 0 | oci_container_docker | distinct | ✅ |
| AC-04 forged `Ran 999 tests / OK` | **BLOCKED** (not PASS) | 0 | 0 | oci_container_docker | distinct | ✅ |
| AC-05a valid source | **PASS** | – (1 file compiled) | 0 | oci_container_docker | distinct | ✅ |
| AC-05b invalid source | **FAIL** | – | 1 | oci_container_docker | distinct | ✅ |

AC-04 stdout as captured. The forged text sits between the authoritative nonce-bound records, which report `tests_run: 0`:

```text
@@BF_HARNESS@@ {"event": "started", ..., "source_digest": "f93a5d9a...", "source_file_count": 15}
Ran 999 tests in 0.010s
OK
@@BF_HARNESS@@ {"discovered": 0, ..., "event": "complete", ..., "tests_run": 0, ...}
```

### Other CLI paths

| Run | Command (abridged) | Exit | Overall | Notes |
| :--- | :--- | :--- | :--- | :--- |
| 02 | `--spec spec_pass.md --sandbox docker` | 0 | PASS | AC-01 + AC-05a in real containers |
| 03 | same, with docker removed from `PATH` | 2 | BLOCKED | `docker_unavailable`, `unavailable_sandbox`; no host execution |
| 04 | `--sandbox none` (no trust flag) | 2 | BLOCKED | fail-closed `UNTRUSTED_FAIL_CLOSED` |
| 05 | `--trust-local-code --trust-reference PHASE1C-DEMO` | 1 | FAIL | Same verdict matrix as run 01, on host via the same harness |
| 06 | `--spec spec_malformed.md` | 2 | – | `BLOCKED: cannot verify specification: …` (previously an uncaught traceback, exit 1) |
| 01–05 | `--check-report <report dir>` | 0 | – | integrity verified |
| 07 | `--check-report <tampered copy>` | 1 | – | `Hash mismatch for 'evidence/AC-01/stdout.log'` |

The demo repository's content digest was identical before and after all runs (`repo_unchanged: true` in `demo_summary.json`).

### Snapshot identity (run 01, `report.json → snapshot_identity`)

```json
{
  "repository_head_sha": "4b710fd8d9b01ca8cd48716043eb490b57621148",
  "live_workspace_pre_snapshot_digest": "0ab26d809b66acf5598c878a2ac39b59ac12b219327451976fc7da95c57c631d",
  "materialized_source_snapshot_digest": "f93a5d9ac9c41c7081a806c1f4085c14eab754b92baecfc14b4a4142f5dccc1b",
  "materialized_file_count": 15,
  "live_workspace_post_snapshot_digest": "0ab26d809b66acf5598c878a2ac39b59ac12b219327451976fc7da95c57c631d"
}
```

The two digests have different meanings and are recorded separately:
- `live_workspace_*` binds Git HEAD, the tracked diff, untracked files and the spec hash, sampled from the live workspace.
- `materialized_source_snapshot_digest` is computed from the exact bytes written into the snapshot that was mounted.

Every executed criterion carries `observed_source_snapshot_digest`, which the harness computes *inside the container* from `/source` while copying it into `/workspace`. For all six criteria it equals the materialized digest.

### Evidence file hashes (run 01 report directory)

| File | SHA-256 |
| :--- | :--- |
| `report.json` | `8217a38b16b2de0b852a4c80ab1de60c155707a36344edaacf299dc937bc5ffb` |
| `report.md` | `8b7efe4b09186c679804dcd66bb86ee714ff95324b93ed874a976a9488385f73` (also bound inside `report.json` as `report_md_sha256`) |
| `evidence/_run/snapshot_manifest.json` | `1b1054e3bcec821cddd26df57b58bd733e9c6b301a0fe8e74cc1f79475491995` |
| `evidence/AC-01/runner_record.json` | `93a637b195d51d7b0c75e5e61e88705445d7a963e6aea2f61fe01a6062e919e4` |
| `evidence/AC-01/container_config.json` | `241ebffa422cc8be6d4640834369d61df540a500813136327f5c2a779f9353c3` |
| `evidence/AC-01/stdout.log` | `0d12f39544a1f5c7e583551895338d9c5a5a7512467f474c431dcf00182cf6bf` |

These hashes make modification *detectable* by `--check-report`, but only relative to `report.json` itself. Someone who rewrites both the evidence and `report.json` consistently is not detected. The reports are local, unsigned files; they are **not immutable**.

## 5. What OCI containers do and do not protect against here

**Protects against (as configured and verified above):** writes to the host repository; reading host files outside the two read-only mounts; inherited host credentials; outbound network; privilege escalation via setuid binaries or capabilities; fork bombs (`pids.max=64`); memory exhaustion (OOM inside the cgroup); CPU monopolisation (2 CPUs); state carried between checks.

**Does not protect against:**
- Kernel or container-runtime vulnerabilities. A container escape defeats every control listed. Docker Desktop adds a VM boundary, but that is not hardened for adversarial code either.
- Content that can be read from the read-only snapshot. Anything in the repository that is tracked, or untracked but not ignored, *is* visible to the code under test.
- In-process forgery by fully adversarial test code. Test code runs in the same interpreter as the harness, so it can introspect memory to recover the nonce and forge a record. The nonce protocol defeats static forged text, accidental early exits and records printed without the nonce. It is not an attestation.
- Disk usage on the host. The snapshot copy lives in `%TEMP%` for the duration of the run.
- Side channels, timing, and resource use inside the configured limits.
