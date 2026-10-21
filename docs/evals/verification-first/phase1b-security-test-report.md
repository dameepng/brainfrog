# BrainFrog Phase 1B: Security & Integrity Test Report

**Execution Date:** 2026-10-19\
**Target Environment:** Windows 11 (PowerShell / Python 3.11)\
**Workspace:** `<REPOSITORY_ROOT>`\
**Isolated Snapshot:** `scratch/verification_1b_isolated/`

---

## 1. Executive Summary

Phase 1B established a rigorous, invariant-driven test suite verifying the execution security and evidence integrity of the BrainFrog standalone verifier (`bf verify`).

Across three dedicated test suites, **49 automated tests** were executed and passed with 100% success rate:
- `tests/test_verifier_execution_integrity.py`: **13/13 PASS** (39.96s)
- `tests/test_verifier_evidence_integrity.py`: **16/16 PASS** (128.04s)
- `tests/test_standalone_verifier.py`: **20/20 PASS** (119.62s)
- **Total:** **49/49 PASS** (287.62s cumulative execution time)

---

## 2. Test Suite 1: Execution Security & Trust Boundaries (`test_verifier_execution_integrity.py`)

This suite exercises the command execution boundaries, trust policies, subprocess containment, and hostile spec command injection attacks.

| # | Test Name | Invariant Tested | Pre-Fix Defect / Vulnerability | Status |
|---|-----------|------------------|--------------------------------|--------|
| 1 | `test_untrusted_spec_cannot_execute_unrestricted` | Commands in `spec.md` have 0 implicit authority | Allowed executables ran with host user permissions | **PASS** |
| 2 | `test_shell_false_alone_is_not_treated_as_sandbox` | `shell=False` is not mislabeled as sandbox | Missing container isolation reported as PASS | **PASS** |
| 3 | `test_unavailable_isolation_fails_closed` | Fail closed (`BLOCKED`) when isolation backend absent | Defaulted to executing host command | **PASS** |
| 4 | `test_explicit_trusted_local_requires_operator_choice` | `--trust-local-code` required for local execution | Local execution was automatic | **PASS** |
| 5 | `test_unsafe_command_wrappers_rejected` | Shell chaining (`&&`, `\|`, `;`, etc.) rejected | Command strings executed directly | **PASS** |
| 6 | `test_build_test_runner_indirect_execution_fails_closed_by_default` | Pytest/npm build scripts fail closed if untrusted | Test runner allowed to execute untrusted test hooks | **PASS** |
| 7 | `test_path_traversal_command_rejected` | Relative paths (`../../bin`) cannot escape root | Executable path resolution could escape repo | **PASS** |
| 8 | `test_python_pinned_to_sys_executable_preventing_repo_shadowing` | Executable resolution pins python to `sys.executable` | Malicious repo-root `python.exe` could hijack test | **PASS** |
| 9 | `test_synthetic_credentials_scrubbed_from_child_env` | Cloud API keys & credentials scrubbed from env | Host environment inherited all developer secrets | **PASS** |
| 10 | `test_timeout_terminates_subprocess_tree` | Subprocess trees killed cleanly upon timeout | Orphaned child processes hung host system | **PASS** |
| 11 | `test_isolated_mode_does_not_modify_spec_or_source` | Verifier writes only to isolated staging directory | Original repository files could be modified | **PASS** |
| 12 | `test_authorization_invalidated_when_spec_mutates` | Stale authorization tokens rejected if spec hash changes | Authorization was persistent and bypassable | **PASS** |
| 13 | `test_unsupported_verification_type_blocked` | Unknown verification types safely blocked | Unknown types caused unhandled crashes | **PASS** |

---

## 3. Test Suite 2: Evidence Integrity & Source Binding (`test_verifier_evidence_integrity.py`)

This suite verifies that evidence is authentically bound to the exact code tested, detects tampering, tracks untracked files and workspace drift, and guarantees safe report cleanup.

| # | Test Name | Invariant Tested | Pre-Fix Defect / Vulnerability | Status |
|---|-----------|------------------|--------------------------------|--------|
| 1 | `test_clean_revision_identity` | Clean git revision accurately records HEAD SHA | Evidence only recorded branch name | **PASS** |
| 2 | `test_dirty_tracked_source_identity` | Tracked file diffs incorporated into source digest | Dirty repo claimed same hash as HEAD | **PASS** |
| 3 | `test_untracked_source_identity` | Untracked source files incorporated into source digest | Untracked scripts affecting tests were invisible | **PASS** |
| 4 | `test_relevant_workspace_drift_invalidates_pass` | Source mutation during verification invalidates PASS (`BLOCKED`) | Mid-run source edits silently allowed PASS | **PASS** |
| 5 | `test_spec_mutation_during_run_invalidates_pass` | Spec mutation during verification invalidates PASS (`BLOCKED`) | Spec edits before report generation were unflagged | **PASS** |
| 6 | `test_source_snapshot_digest_stability` | Snapshot digest is deterministic and reproducible | Non-deterministic ordering in file hashing | **PASS** |
| 7 | `test_evidence_file_tampering_detected` | `verify_report_integrity` flags modified stdout/evidence | Modifying evidence files was undetectable | **PASS** |
| 8 | `test_missing_evidence_detected` | `verify_report_integrity` flags missing evidence file | Deleting evidence files left report looking valid | **PASS** |
| 9 | `test_manifest_inconsistency_detected` | `verify_report_integrity` flags altered JSON hashes | Altered manifest hashes were uninspected | **PASS** |
| 10 | `test_report_directory_collision_prevention` | Report staging avoids collisions; existing reports kept | Concurrent runs overwrote report directories | **PASS** |
| 11 | `test_path_traversal_on_report_output_prevented` | Report paths cannot escape destination boundary | Path traversal could overwrite host files | **PASS** |
| 12 | `test_secret_redaction_in_captured_output` | Sensitive tokens (`OPENAI_API_KEY`, etc.) redacted in logs | Secrets in test output leaked into proof bundle | **PASS** |
| 13 | `test_historical_reports_preserved_during_run` | Verifier never deletes historical reports in `.brainfrog/reports` | Previous broad `rmtree` purged prior runs | **PASS** |
| 14 | `test_safe_cleanup_report_only_deletes_owned_dir` | Cleanup deletes only the specific run directory passed | Cleanup wiped sibling reports | **PASS** |
| 15 | `test_safe_cleanup_report_refuses_broad_parent_dir` | Cleanup explicitly refuses to delete report root | Overly broad cleanup destroyed directory tree | **PASS** |
| 16 | `test_no_false_claim_of_immutable_proof` | Report claims tamper evidence, not immutability | Local files were marketed as "immutable" | **PASS** |

---

## 4. Test Suite 3: Standalone Verifier MVP Regressions (`test_standalone_verifier.py`)

This suite verifies that core functional requirements (CLI, parsing, criteria evaluation, markdown generation, exit codes) remain fully functional without regressions.

| # | Test Name | Scope | Status |
|---|-----------|-------|--------|
| 1 | `test_spec_parser_valid_markdown` | Spec markdown parser extracts goals, constraints, criteria | **PASS** |
| 2 | `test_spec_parser_missing_file` | Missing spec produces clean error | **PASS** |
| 3 | `test_spec_parser_invalid_frontmatter` | Malformed frontmatter safely handled | **PASS** |
| 4 | `test_criterion_parsing_all_types` | Build, test, lint, browser criteria correctly parsed | **PASS** |
| 5 | `test_command_allowlist_enforcement` | Disallowed binaries rejected | **PASS** |
| 6 | `test_build_check_success` | Successful build check generates evidence and PASS | **PASS** |
| 7 | `test_build_check_failure` | Failing build check produces exit 1 and FAIL evidence | **PASS** |
| 8 | `test_test_check_success` | Test command execution produces test output evidence | **PASS** |
| 9 | `test_test_check_failure` | Test failure generates FAIL criterion verdict | **PASS** |
| 10 | `test_timeout_handling` | Process timeout sets criterion status BLOCKED | **PASS** |
| 11 | `test_browser_check_blocked_when_unconfigured` | Browser criterion without runner produces BLOCKED | **PASS** |
| 12 | `test_overall_verdict_all_pass` | All criteria passing yields overall PASS and exit 0 | **PASS** |
| 13 | `test_overall_verdict_one_failure` | Any criterion failure yields overall FAIL and exit 1 | **PASS** |
| 14 | `test_overall_verdict_one_blocked` | Blocked criterion yields overall BLOCKED and exit 2 | **PASS** |
| 15 | `test_json_report_structure` | `report.json` conforms to schema with hashes and timestamps | **PASS** |
| 16 | `test_markdown_report_formatting` | `report.md` formats tables and summaries clearly | **PASS** |
| 17 | `test_typesafe_free_execution` | Verifier runs without TypeSafe or external LLM imports | **PASS** |
| 18 | `test_cli_verify_command` | `bf verify` CLI entrypoint executes and returns exit code | **PASS** |
| 19 | `test_cli_custom_spec_path` | `bf verify --spec <path>` handles custom spec paths | **PASS** |
| 20 | `test_cli_output_dir_customization` | `bf verify --output <dir>` writes report to custom dir | **PASS** |

---

## 5. Security & Invariant Summary

1. **Zero Implicit Execution Privilege:** Untrusted code supplied via `spec.md` or repository files cannot run locally without explicit `--trust-local-code` operator opt-in.
2. **Deterministic Source Integrity:** `compute_source_snapshot_digest` cryptographically binds evidence to git commit HEAD, tracked git diff, untracked files, and spec content hash.
3. **Drift Invalidation:** Source modifications occurring during test execution conservatively force overall verdict to `BLOCKED`.
4. **Offline Evidence Integrity Verification:** `bf verify --check-report <dir>` validates SHA-256 hashes of all evidence files against the manifest without external dependencies.
5. **No Data Loss on Cleanup:** Broad `rmtree` patterns eliminated; verifier and test runners only touch explicitly owned directories.
