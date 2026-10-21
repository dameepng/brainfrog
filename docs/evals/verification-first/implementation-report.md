# BrainFrog Standalone Verifier MVP: Implementation Report

**Document Version:** 1.0.0\
**Date:** 2026-10-19\
**Milestone:** Verification-First Product Pivot (Stage B Implementation)\
**Repository:** `dameepng/brainfrog`\
**Workspace:** `<REPOSITORY_ROOT>`

---

## 1. Overview & Architectural Decisions

BrainFrog's core value proposition has been expanded to support standalone independent verification:
> **"Agent says done. BrainFrog proves it."**

Developers can now invoke `bf verify --spec spec.md` or `bf --spec spec.md` to independently verify code changes produced by any coding agent (Claude Code, OpenCode, Codex, Cursor, etc.) against an explicit Markdown specification.

### Key Implementation Decisions:
1. **Zero Runtime Coupling:** The standalone verifier lives in [`core/verifier.py`](../../../core/verifier.py). It does NOT initialize `BrainFrogRuntime`, `SessionManager`, `System1Factory`, `System2Factory`, or any LLM clients. It runs completely offline without any API keys or network dependencies.
2. **Reused Subprocess & Security Infrastructure:** Rather than building a redundant execution layer, the verifier leverages:
   - `core/runtime/process.py` (`run_hardened_subprocess`): Process tree cleanup, timeout bounding, and resource containment.
   - `core/runtime/command_policy.py` (`CommandPolicy`): Subprocess executable and argument allowlist, denying shell wrappers, command chaining, and directory traversal.
   - `core/runtime/proof.py` (`sample_git_state`, `redact_secrets`): Git commit provenance sampling and automatic credential redaction.
3. **Fail-Closed Criterion Lifecycle:** Every criterion receives an explicit verdict from `{PASS, FAIL, BLOCKED, NOT_RUN}`. Unsupported or missing verification methods are marked `BLOCKED`. Unexecuted criteria can never produce `PASS`.
4. **Isolated, Non-Overwritable Reports:** Verification runs generate tamper-evident records under `.brainfrog/reports/<run-id>/` containing:
   - `report.json`: Machine-readable audit artifact with SHA-256 hashes of `spec.md` and evidence files.
   - `report.md`: Human-readable summary table with links to logs.
   - `evidence/<AC-ID>/`: Bounded stdout, stderr, and failure reason files.
5. **CLI Terminal UX:** Integrated into `cli.py` to render concise Rich terminal summaries matching product design with deterministic exit codes (0 = PASS, 1 = FAIL, 2 = BLOCKED/ERROR).

---

## 2. Actual Code Modifications

### 2.1. New Modules
- **[`core/verifier.py`](../../../core/verifier.py):**
  - `CriterionSpec` & `SpecDocument` data models.
  - `parse_spec_markdown()` parser enforcing unique criterion IDs and non-empty criteria.
  - `CriterionStatus` and `OverallVerdict` enums.
  - `StandaloneVerifier` engine executing criteria, sampling Git state, managing report persistence, and generating evidence.
  - `render_verification_console()` rendering clean Rich terminal outputs.

### 2.2. Enhanced Modules
- **[`cli.py`](../../../cli.py):**
  - Added `--spec` argument to `argparse`.
  - Added routing logic for `bf verify --spec <file>`, `bf --spec <file>`, and `bf verify <spec.md>` directly to `StandaloneVerifier`.
  - Fully preserved legacy `--verify-proof`, `--show-proof`, and `--list-proofs` workflows.
- **[`core/runtime/command_policy.py`](../../../core/runtime/command_policy.py):**
  - Authorized standard Python compilation modules `compileall` and `py_compile` under strict argument validation to support Python build verification checks.

---

## 3. Test Suites & Verification Results

### 3.1. Standalone Verifier Unit & Security Test Suite
- **File:** [`tests/test_standalone_verifier.py`](../../../tests/test_standalone_verifier.py)
- **Tests Executed:** 20 tests.
- **Result:** **20 passed, 0 failed, 0 skipped** (45.78s).
- **Coverage of Invariants:**
  1. TypeSafe-free startup: Verified with cleared environment.
  2. No System 1 initialization: Verified with mock assert.
  3. No LLM provider required: Verified with mock assert.
  4. Markdown spec parsing: Goals, non-goals, criteria, constraints parsed.
  5. Duplicate criterion IDs rejected: Verified `ValueError` on collision.
  6. Missing verification method handled safely: Verified marked `BLOCKED`.
  7. Passing test command: Verified produces `PASS` and exit code 0.
  8. Failing command: Verified produces `FAIL` and exit code 1.
  9. Unauthorized shell command: Verified `BLOCKED` by `CommandPolicy`.
  10. Missing browser runner: Verified cleanly `BLOCKED` with explanation.
  11. `NOT_RUN` does not count as `PASS`: Verified fail-closed logic.
  12. Dirty workspace not misrepresented: Verified `is_dirty` and dirty file lists.
  13. Evidence manifest binds observed revision: Verified against Git HEAD SHA.
  14. Evidence hash detects modifications: Verified SHA-256 tamper sensitivity.
  15. Non-overwritable report paths: Verified unique run IDs per execution.
  16. Secret redaction: Verified credentials stripped from logs.
  17. Path traversal denied: Verified outside-workspace spec paths rejected.
  18. Nonzero exit codes: Verified code 1 for failure, 2 for blocked.
  19. REPL workflows remain functional: Verified CLI help and subcommands.
  20. Approval boundary cannot be bypassed: Verified shell chaining and redirects blocked.

### 3.2. Existing Verification Suite Baseline
- `tests/test_phase2a_verification_gate.py`, `tests/test_phase6c_proof_verifier.py`, `tests/test_pr_proof.py`, `tests/test_mcp_quality_gate.py`:
- **Result:** **101 passed, 6 skipped** (48.90s).

---

## 4. Known Limitations & Boundaries

1. **Browser Check Runner Dependency:** In headless environments without the local MCP browser verification server active, browser check criteria cleanly report `BLOCKED`. Interactive DOM assertions require the MCP browser server.
2. **Untracked Spec Location:** Specifications must reside within the workspace or a permitted repository path to prevent path traversal vulnerabilities.
3. **Shell Chaining Restriction:** Command policy intentionally rejects `&&`, `;`, and `|`. Users desiring multi-step verification should declare separate acceptance criteria (`AC-01`, `AC-02`) for each check.
