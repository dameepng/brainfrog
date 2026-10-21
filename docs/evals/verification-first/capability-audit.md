# BrainFrog Capability Audit: Verification Architecture & Standalone Verifier

**Document Version:** 1.0.0\
**Audit Date:** 2026-10-19\
**Auditor:** Antigravity Independent Architecture & Verification Subsystem\
**Repository:** `dameepng/brainfrog`\
**Workspace:** `<REPOSITORY_ROOT>`\
**Git Branch:** `feat/trustworthy-verification-workflow`\
**Target Milestone:** Verification-First Product Pivot (Stage A Capability Audit)

---

## 1. Executive Summary

BrainFrog is pivoting from an all-in-one autonomous coding agent to an independent **verification and evidence layer** for coding-agent outputs:
> **"Agent says done. BrainFrog proves it."**

Developers should be able to run `bf verify --spec spec.md` after working with any coding agent (Claude Code, OpenCode, Codex, Cursor, Aider, or manual edits). The agent responsible for writing code is not trusted to verify its own correctness.

This audit inspects the actual BrainFrog source code in `dameepng/brainfrog` to determine what verification capabilities already exist, what is proven, what is partial, and what is missing to deliver the standalone verification MVP.

---

## 2. Capability Matrix

| Capability | Status | Existing Implementation Source Paths | Evidence / Current Behavior | Gap for Standalone `bf verify` | Recommended Action |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Standalone verification** | `MISSING` | `cli.py:953`, `core/plans.py:1085` | `--verify-proof` verifies *existing* past proof JSON; `verify_plan` requires pre-existing `PlanDocument` in `.brainfrog/plans/` with status `implemented`. | No CLI entrypoint accepts an external `spec.md` directly on an existing repo without the BrainFrog agent workflow. | Implement dedicated `core/verifier.py` and CLI subcommand `bf verify --spec <file>`. |
| **TypeSafe-free startup** | `IMPLEMENTED_AND_VERIFIED` | `cli.py:4170`, `core/runtime/gateway.py` | CLI defaults to local `jev` backend; `--verify-proof` and `--help` run with zero TypeSafe / external credentials (verified in isolation test). | Standalone verifier must completely bypass runtime agent initialization (`System1Factory`, `System2Factory`, `SessionManager`). | Keep verification engine isolated from autonomous runtime; zero LLM dependencies for deterministic checks. |
| **Spec parsing** | `PARTIAL` | `core/plans.py:126`, `core/plans.py:850` | `PlanDocument.from_markdown` parses `## Acceptance Criteria` lists from internal plan docs. | Cannot parse standard external `spec.md` format (Goals, Non-goals, Acceptance Criteria with explicit verification method, constraints). | Implement `SpecDocument` parser in `core/verifier.py` supporting markdown specs and explicit criterion attributes. |
| **Criterion-level checks** | `PARTIAL` | `core/plans.py:1120`, `core/plans.py:180` | `CriterionVerificationMapping` maps criteria to checks and enforces 100% coverage, but only yields plan-level binary outcome. | Missing per-criterion status vocabulary (`PASS`, `FAIL`, `BLOCKED`, `NOT_RUN`) and individual criterion evidence binding. | Implement `CriterionResult` with strict status vocabulary and failure explanation. |
| **Build gate** | `IMPLEMENTED_AND_VERIFIED` | `core/frontend_quality_gate.py:320`, `core/runtime/process.py:25` | `run_hardened_subprocess` runs build commands with process tree cleanup, timeout, and bounded output capture. | Coupled to MCP client or autonomous build loop. | Expose build check in standalone verifier via `run_hardened_subprocess` under `CommandPolicy`. |
| **Test gate** | `IMPLEMENTED_AND_VERIFIED` | `core/plans.py:1150`, `core/runtime/command_policy.py:340` | `CommandPolicy` validates pytest, python, npm arguments; execution captured via `capture_bounded_output`. | Currently invoked only during `verify_plan` on internal plans or during autonomous agent task. | Reuse `CommandPolicy` and `run_hardened_subprocess` for test criteria in `spec.md`. |
| **Browser check** | `IMPLEMENTED_BUT_UNPROVEN` | `core/frontend_quality_gate.py:50`, `core/mcp_client.py` | 7-step quality gate connects to external MCP server (`brainfrog-verify-mcp`), launches dev server, navigates, checks console errors, captures base64 screenshot. | Requires external MCP server process; fails if MCP is uninstalled. Not proven as standalone CLI step. | If MCP server is unavailable or unconfigured, report `BLOCKED` with actionable reason; do not crash or fake `PASS`. |
| **Screenshot evidence** | `PARTIAL` | `core/frontend_quality_gate.py:375`, `core/pr_proof.py:165` | Quality gate captures base64 screenshot; `pr_proof.py` publishes screenshots to orphan git branch `pr-proof-assets`. | Screenshots are not organized per-criterion in local filesystem reports. | Save browser check screenshots to `.brainfrog/reports/<run-id>/evidence/<AC-ID>/screenshot.png`. |
| **Git SHA binding** | `IMPLEMENTED_AND_VERIFIED` | `core/runtime/proof.py:380`, `core/runtime/proof.py:405` | `sample_git_state` and `capture_git_provenance` capture HEAD commit SHA, branch, and dirty status. | Not exposed in a standalone verification report. | Bind Git HEAD commit SHA and dirty status to `VerificationReport`. |
| **Artifact hashing** | `IMPLEMENTED_AND_VERIFIED` | `core/plans.py:90`, `core/runtime/proof.py:180` | SHA-256 computed for spec files, change records, and evidence logs. | Standalone report directory does not yet have content-addressed evidence manifests. | Compute SHA-256 for `spec.md` and every generated evidence log/screenshot. |
| **Tamper detection** | `IMPLEMENTED_AND_VERIFIED` | `core/plans.py:1105`, `core/runtime/proof_verifier.py:220` | `DeterministicProofVerifier` verifies disk integrity against recorded hashes; `PlanDocument` detects markdown modifications. | Reports must prevent silent overwriting. | Prevent overwriting existing `.brainfrog/reports/<run-id>/` runs; enforce fail-closed or unique run IDs. |
| **Approval gate** | `IMPLEMENTED_AND_VERIFIED` | `core/runtime/approval.py:180`, `core/runtime/contract.py:120` | `ApprovalService` enforces two-man rule, canonical operation digests, and permission boundaries. | Spec verification commands could attempt arbitrary shell execution. | Enforce strict `CommandPolicy` allowlist on all spec commands; reject arbitrary shell syntax, chaining, or redirects. |
| **Structured reports** | `PARTIAL` | `core/runtime/proof.py:260`, `core/runtime/proof_markdown.py:40` | Generates `.brainfrog/proofs/<task_id>.json` and `.md` for autonomous agent tasks. | Standalone report layout (`.brainfrog/reports/<run-id>/report.json`, `report.md`, `evidence/`) does not exist. | Implement `ReportWriter` producing structured JSON, human-readable Markdown, and per-criterion evidence logs. |
| **Nonzero exit codes** | `IMPLEMENTED_AND_VERIFIED` | `cli.py:968`, `core/runtime/proof_verifier.py:75` | Deterministic exit codes: 0 = verified/pass, 1 = failure, 2 = incomplete/blocked. | Standalone verifier needs deterministic exit code mapping (0 = PASS, 1 = FAIL, 2 = BLOCKED/ERROR). | Return 0 only if ALL criteria pass; return 1 if any fails; return 2 if blocked/unconfigured. |
| **CI compatibility** | `IMPLEMENTED_AND_VERIFIED` | `.github/workflows/ci.yml`, `cli.py:4170` | Headless execution, zero TTY requirement, non-interactive output mode. | Ready for CI use. | Ensure `bf verify --spec spec.md` outputs cleanly to stdout and sets exit code for CI steps. |

---

## 3. Deep Source Inspection & Reusability Map

### 3.1. Reusable Infrastructure

1. **`core/runtime/command_policy.py` (`CommandPolicy`)**:
   - Centralized executable and argument validation.
   - Denies shell wrappers (`cmd.exe`, `powershell.exe`, `bash`, `sh`).
   - Denies chained execution (`&&`, `;`, `|`), null bytes, directory traversal, and PATH hijacking.
   - Validates git, gh, pytest, python arguments against strict allowlists.
   - *Directly reusable to sanitize and authorize commands inside `spec.md` without unrestricted shell access.*

2. **`core/runtime/process.py` (`run_hardened_subprocess`)**:
   - Executes process tokens without `shell=True`.
   - Cleans up child process trees on timeout or cancellation (zero zombie processes).
   - Bounds stdout and stderr memory consumption.
   - *Directly reusable to execute build and test verification commands.*

3. **`core/runtime/proof.py` (`sample_git_state`, `redact_secrets`)**:
   - Captures current git commit SHA, active branch, and dirty status.
   - Redacts sensitive credentials, tokens, and API keys from outputs and logs.
   - *Directly reusable for commit provenance and evidence sanitization.*

4. **`core/frontend_quality_gate.py` (`run_frontend_quality_gate`, `QualityGateResult`)**:
   - MCP client integration for frontend build, dev server launch, and console/network checks.
   - *Usable for browser check criteria when MCP server is active; provides clean error detection for graceful `BLOCKED` status when inactive.*

### 3.2. Missing Infrastructure to Build in Stage B

1. **`SpecDocument` & Parser (`core/verifier.py`)**:
   - Parse markdown specification containing `## Acceptance Criteria`.
   - Extract criterion ID (`AC-01`), title, description, verification type (`build`, `test`, `browser`), command, and target.
   - Validate ID uniqueness, format, and non-empty criteria list.
2. **`StandaloneVerifier` Engine (`core/verifier.py`)**:
   - Orchestrates per-criterion execution without loading agent runtimes.
   - Maps criteria to verdicts (`PASS`, `FAIL`, `BLOCKED`, `NOT_RUN`).
   - Computes overall verdict and summary counts.
   - Fails closed: unsupported or missing verification methods become `BLOCKED`, never `PASS`.
3. **`ReportWriter` (`core/verifier.py`)**:
   - Persists evidence into `.brainfrog/reports/<run-id>/`.
   - Generates `report.json` (machine-readable) and `report.md` (human-readable).
   - Saves bounded, redacted stdout/stderr per criterion in `evidence/<criterion_id>/`.
4. **CLI Integration (`cli.py`)**:
   - Add `--spec` flag and route `bf verify --spec <file>` to `verify_spec_cli`.
   - Render clean Rich console summary matching the product specification.
   - Exit with code 0 (PASS), 1 (FAIL), or 2 (BLOCKED/ERROR).
