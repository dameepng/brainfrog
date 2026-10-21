# BrainFrog Standalone Verification MVP: Specification & Contract

**Document Version:** 1.0.0\
**Date:** 2026-10-19\
**Milestone:** Verification-First Product Pivot (Stage B Specification)\
**Target Command:** `bf verify --spec spec.md`

---

## 1. Specification File Contract (`spec.md`)

The specification file is an authoritative, human-readable, and machine-parsable Markdown document declaring the goals, non-goals, acceptance criteria, and constraints for a code change.

### 1.1. Minimal Markdown Syntax

```markdown
# Spec: Feature Name

## Goal
Human-readable summary of the intended engineering goal.

## Non-goals
- Out-of-scope item 1
- Out-of-scope item 2

## Acceptance Criteria

### AC-01 — Production build
The application and all packages must compile cleanly.

**Verification:** Build command
**Command:** npm run build

### AC-02 — Unit and integration tests
All automated unit and regression tests must pass without failure.

**Verification:** Test command
**Command:** pytest tests/test_login.py

### AC-03 — Invalid email validation
Submitting an invalid email address must display an inline error badge.

**Verification:** Browser check
**Target:** /login

## Constraints
- Do not modify database schemas.
- Backward compatibility with legacy tokens must be preserved.
```

### 1.2. Structural Invariants
1. **Title:** Document starts with `# Spec: <Title>`.
2. **Acceptance Criteria Headings:** Criteria are declared using `### <ID> — <Title>` or `### <ID>: <Title>`.
3. **Criterion Identifier (`ID`):** Must match `^[A-Za-z0-9_-]+$` (e.g. `AC-01`, `AC-02`).
4. **Uniqueness:** All criterion identifiers within a spec must be strictly unique. Duplicate IDs fail parsing immediately.
5. **Non-Empty Criteria:** A spec without any acceptance criteria is invalid and rejected.
6. **Explicit Verification Method:** Every criterion must declare `**Verification:** <Type>`:
   - Supported Types: `Build command` (`build`), `Test command` (`test`), `Browser check` (`browser`).
7. **Explicit Target / Command:**
   - For `build` and `test`: `**Command:** <cmd>` is required.
   - For `browser`: `**Target:** <path/url>` is required.
8. **Fail-Closed on Unknowns:** Unsupported verification methods (e.g. `Manual review`, `Visual inspection`) are rejected or flagged as `BLOCKED`. They can never silently produce `PASS`.
9. **Context Retention:** `## Goal`, `## Non-goals`, and `## Constraints` are preserved in the parsed model and generated reports for reviewer inspection.

---

## 2. Status Vocabulary & Exit Codes

### 2.1. Per-Criterion Status Vocabulary

| Status | Meaning | Conditions |
| :--- | :--- | :--- |
| **`PASS`** | Verified | The verification check was executed successfully, exited with code 0, and produced supporting evidence. |
| **`FAIL`** | Failed | The verification check executed and produced a non-zero exit code or failed assertions. |
| **`BLOCKED`** | Blocked | The verification check could not be executed because a dependency, runner, capability, or permission was unavailable (e.g. browser runner unavailable, command disallowed by policy, missing command). |
| **`NOT_RUN`** | Not Executed | The check was excluded, skipped, or not configured. |

### 2.2. Overall Verdict

- **`PASS`:** Every declared acceptance criterion has status `PASS`.
- **`FAIL`:** At least one criterion has status `FAIL`.
- **`BLOCKED`:** Zero criteria failed, but at least one criterion is `BLOCKED` or `NOT_RUN`.

### 2.3. Process Exit Codes

| Exit Code | Verdict | Meaning |
| :---: | :--- | :--- |
| **`0`** | `PASS` | All acceptance criteria verified successfully. |
| **`1`** | `FAIL` | One or more acceptance criteria failed. |
| **`2`** | `BLOCKED` | One or more criteria blocked, unconfigured, or invalid spec / runner error. |

---

## 3. Security Boundary & Command Authorization

1. **Untrusted Spec Input:** `spec.md` is treated as untrusted input. It cannot grant arbitrary shell authority.
2. **No `shell=True` Execution:** Commands are tokenized via `shlex.split` and executed directly via `run_hardened_subprocess`.
3. **Command Policy Allowlist:** Commands must pass `CommandPolicy`:
   - Shell wrappers (`cmd.exe`, `powershell.exe`, `bash`, `sh`) are rejected.
   - Command chaining (`&&`, `;`, `|`), redirection (`>`, `<`), and subshell substitutions are rejected.
   - Executable path traversal and PATH hijacking are blocked.
   - Python inline code execution (`-c`) is denied; only sanctioned runners (`pytest`, `unittest`) are permitted.
4. **Containment:** Verification commands run in the target workspace and cannot escape the workspace root.
5. **Secret Redaction:** Standard secret patterns (API keys, bearer tokens, private keys) in stdout and stderr are automatically redacted via `redact_secrets` before being written to disk or shown in the CLI.

---

## 4. Evidence & Report Structure

Each verification execution creates an isolated, non-overwritable report directory:

```text
.brainfrog/reports/<run-id>/
├── report.json
├── report.md
└── evidence/
    ├── AC-01/
    │   ├── stdout.log
    │   └── stderr.log
    ├── AC-02/
    │   ├── stdout.log
    │   └── stderr.log
    └── AC-03/
        └── reason.txt (or screenshot.png if browser runner active)
```

### 4.1. `report.json` Schema Fields

- `run_id`: Unique identifier (e.g., `verify_20261019_154500_a91f3c`).
- `created_at`: ISO 8601 UTC timestamp.
- `repository`: Name or relative path of the repository.
- `git_head_sha`: Git commit hash at time of verification.
- `workspace_status`: Object containing `is_dirty: bool` and list of `modified_files` and `untracked_files`.
- `spec_path`: Relative path to `spec.md`.
- `spec_sha256`: SHA-256 hash of the `spec.md` file content.
- `overall_verdict`: `"PASS"`, `"FAIL"`, or `"BLOCKED"`.
- `summary`: `{ "total": int, "passed": int, "failed": int, "blocked": int, "not_run": int }`.
- `criteria`: Array of criterion execution records:
  - `criterion_id`: Stable ID (e.g. `AC-01`).
  - `title`: Short title.
  - `description`: Detailed requirement description.
  - `verification_type`: `build`, `test`, or `browser`.
  - `command`: Executed command (or target URL).
  - `status`: `PASS`, `FAIL`, `BLOCKED`, or `NOT_RUN`.
  - `exit_code`: Subprocess returncode (or null if not executed).
  - `duration_ms`: Execution duration in milliseconds.
  - `evidence_files`: Map of evidence filenames to relative paths.
  - `evidence_hashes`: Map of evidence filenames to SHA-256 hashes.
  - `failure_reason`: Human-readable reason if status is not `PASS`.

---

## 5. CLI Terminal UX

When executed:
```text
$ bf verify --spec spec.md
```

The terminal renders a clean, concise Rich output:

```text
BrainFrog Verification
Revision: a91f3c2
Workspace: clean
Spec: spec.md (sha256: 8f3b2a19)

AC-01  PASS      Production build
AC-02  FAIL      Unit and integration tests
AC-03  BLOCKED   Invalid email validation
                  Browser runner unavailable (MCP server not accessible)

Overall: FAIL
Passed: 1
Failed: 1
Blocked: 1

Report:
.brainfrog/reports/verify_20261019_154500_a91f3c/report.md
```
