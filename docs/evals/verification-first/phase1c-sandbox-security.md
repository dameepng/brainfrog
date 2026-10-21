# BrainFrog Verification-First Phase 1C: Sandbox Security Model & Boundaries

**Date:** 2026-10-20\
**Component:** `core/runtime/sandbox.py` (`DockerSandboxBackend`)

---

## 1. OCI Container Hardening Boundaries

BrainFrog's `DockerSandboxBackend` provisions disposable containers per verification check with the following enforced hardening controls:

```text
docker run --rm \
    --name=<ephemeral_id> \
    --network=none \
    --cap-drop=ALL \
    --security-opt=no-new-privileges \
    --cpus=2.0 \
    --memory=512m \
    --pids-limit=64 \
    -v <materialized_snapshot>:/source:ro \
    --tmpfs /workspace:rw,size=256m \
    --tmpfs /tmp:rw,size=64m \
    -w /workspace \
    -e PYTHONPATH=/source:/workspace \
    -e PYTHONDONTWRITEBYTECODE=1 \
    -e HOME=/tmp \
    python:3.12-slim \
    sh -c 'cp -r /source/. /workspace/ && exec "$@"' -- <approved_command>
```

### Security Controls Matrix

| Control | Mechanism | Security Objective |
|---|---|---|
| **Network Containment** | `--network=none` | Blocks data exfiltration, phone-home telemetry, and unauthorized package downloading. |
| **Capability Dropping** | `--cap-drop=ALL` | Strips all Linux capabilities (`CAP_NET_RAW`, `CAP_SYS_ADMIN`, `CAP_DAC_OVERRIDE`, etc.). |
| **Privilege Escalation** | `--security-opt=no-new-privileges` | Blocks `setuid` / `setgid` binaries from escalating privileges inside the container. |
| **Read-Only Source Mount** | `-v <snapshot>:/source:ro` | The source repository snapshot cannot be modified by arbitrary test or build scripts. |
| **Ephemeral Workspace** | `--tmpfs /workspace:rw,size=256m` | Temporary in-memory workspace discarded immediately on container exit. |
| **Resource Quotas** | `--cpus=2.0 --memory=512m --pids-limit=64` | Prevents fork bombs, memory starvation, and CPU exhaustion attacks. |
| **No Host Docker Socket** | Never mounted | Untrusted code cannot talk to `/var/run/docker.sock` or control the host daemon. |
| **Controlled Toolchain** | Pinned `python:3.12-slim` | Untrusted `spec.md` cannot request arbitrary host rootfs or custom Dockerfiles. |

---

## 2. Threat Model & Explicit Residual Risks

BrainFrog explicitly documents trust boundaries:
1. **Container Daemon Trust:** BrainFrog trusts the container runtime daemon. We do not defend against zero-day Linux kernel privilege escalations or compromised hypervisors.
2. **Immutable Host Source:** The host workspace is NEVER mounted directly into the container. Only a read-only materialized snapshot (`:/source:ro`) is mounted.
3. **No Network Access by Default:** Offline test suites are fully supported. Tests requiring network calls fail closed or report BLOCKED.
