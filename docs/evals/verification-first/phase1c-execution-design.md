# BrainFrog Verification-First Phase 1C: Execution Engine Architecture & Contract

**Date:** 2026-10-20\
**Status:** Implemented & Verified

---

## 1. Dual-Backend Architecture

BrainFrog Phase 1C defines an explicit contract for executing acceptance criteria verification commands without external AI providers or TypeSafe dependencies:

```text
External Coding Agent
        |
        v
Source Repository + spec.md
        |
        v
StandaloneVerifier.verify_spec()
        |
        +---> Source Snapshot Materialization (excludes .git, .env, secrets)
        |
        +---> Select Execution Backend:
        |       |-- ISOLATED_SANDBOX  -> DockerSandboxBackend (OCI Container)
        |       |-- TRUSTED_OPERATOR  -> LocalTrustedExecutionBackend (Pinned sys.executable)
        |       +-- UNTRUSTED_FAIL_CLOSED -> Direct BLOCKED rejection
        |
        +---> VerificationExecutionBackend.execute() -> ExecutionResult
        |
        +---> TestResultAdapter.parse_outcome() -> ParsedCheckOutcome (Prevent Zero-Test False Pass)
        |
        v
Cryptographically Signed & Hashed Evidence Report (.brainfrog/reports/<run-id>/)
```

---

## 2. Verification Execution Contract

All execution backends implement `VerificationExecutionBackend` with the strict contract:

```python
class VerificationExecutionBackend:
    def is_available(self) -> Tuple[bool, Optional[str]]:
        """Probe runtime and daemon accessibility."""
        raise NotImplementedError

    def execute(
        self,
        source_snapshot_dir: Path,
        source_snapshot_digest: str,
        approved_argv: List[str],
        policy: SandboxPolicy,
        limits: SandboxResourceLimits,
    ) -> ExecutionResult:
        """Execute approved verification command against source snapshot."""
        raise NotImplementedError
```

### ExecutionResult Contract
Every run captures deterministic operational evidence:
- `backend_type`: e.g. `docker_oci`, `local_trusted_operator`, `unavailable_sandbox`.
- `backend_version`: Runtime version string.
- `container_image`: Permitted base image identity (e.g. `python:3.12-slim`).
- `container_image_digest`: Exact sha256 image digest if inspected.
- `source_snapshot_digest`: Materialized snapshot sha256 digest.
- `executed_argv`: Sanitized, approved argument vector.
- `started_at` & `finished_at`: ISO-8601 UTC timestamps.
- `duration_ms`: High-precision monotonic execution duration.
- `exit_code`: Numeric process return code.
- `stdout` & `stderr`: Aggressively scrubbed output streams.
- `test_count` & `test_failure_count`: Extracted by test runner adapter.
- `resource_limit_status`: CPU, memory, and timeout limit accounting.
- `sandbox_policy_summary`: Exact dictionary of isolation parameters enforced.

---

## 3. Strict Separation of Concepts

BrainFrog enforces clean separation between three distinct verification dimensions:
1. **Authorization:** Is this command permitted under repository policy? (Handled by `validate_verification_command`).
2. **Execution:** Did the process start, run within resource bounds, and terminate? (Handled by `VerificationExecutionBackend`).
3. **Verification Evidence:** Did the observed output prove the claimed acceptance criterion? (Handled by `TestResultAdapter`).
