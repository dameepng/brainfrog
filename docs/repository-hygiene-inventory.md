# Repository Hygiene Inventory

**Date:** 2026-10-04
**Scope:** Complete repository inspection following Phase 14B security hardening cycle.
**Mode:** Inventory Only (Zero modifications applied during this phase).

---

## 1. Executive Summary

A comprehensive repository inspection was conducted across all tracked files, untracked working files, runtime storage, test fixtures, configuration, and documentation.

- **Tracked Files in Git (`git ls-files`):** 81 files
- **Untracked Files (`git status --short`):** 1 file (`phase14bl01_post_remediation_redteam_report.md`)
- **Ignored / Local Working Files:** `scratch/` (32 scripts, logs, and screenshots), `.brainfrog/` runtime state (`approvals/`, `sessions/`, `scratch/`), `.pytest_cache/`, `__pycache__/`
- **Total Test Cases Discovered:** 414 tests (409 passed, 5 skipped, 0 failures)
- **CI Test Runner:** Python 3.11 with `compileall`, Git Guard validation, and `unittest discover -s tests`

The core production runtime and permanent test suites are clean and robust. However, several categories of files require hygiene action:
1. **Root-Level Security Audit Reports (5 files):** Dispersed markdown reports from Phase 14B iterations (`phase14bm03_*.md`, `phase14bl01_*.md`) should be organized into `docs/security/phase-14/`.
2. **Untracked Test Leftovers:** Empty directory `tests/.brainfrog` left behind by `tests/test_memory.py`.
3. **Runtime Directory Exclusions in `.gitignore`:** `.brainfrog/sessions/` and `.brainfrog/approvals/` are runtime-generated and should be explicitly excluded in `.gitignore` to prevent accidental commits of local state.
4. **Temporary Scratch Artifacts:** `scratch/` directory contains 32 ad-hoc POCs, benchmarks, and debug scripts from Phase 14 audits that are already superseded by permanent suites in `tests/`.

---

## 2. Inventory Classification Matrix

Every inspected file is classified into one of six standard statuses:
- **`KEEP_PRODUCTION`**: Necessary for runtime execution or packaging.
- **`KEEP_TEST`**: Permanent automated test suite protecting functionality or security invariants.
- **`KEEP_DOCUMENTATION`**: Core product or architecture documentation.
- **`ARCHIVE`**: Historical security audit or remediation artifact to be structured into `docs/`.
- **`DELETE`**: Dead, temporary, or generated file with no runtime, test, or documentation value.
- **`NEEDS_REVIEW`**: Requires specific architectural confirmation before any action.

---

### A. Root Files & Shims

| Path | Current Git Status | Classification | Purpose & Analysis |
|---|---|---|---|
| `cli.py` | Tracked | **KEEP_PRODUCTION** | Primary entrypoint for BrainFrog CLI. |
| `orchestrator.py` | Tracked | **KEEP_PRODUCTION** | Canonical execution engine. Preserved untouched. |
| `auth_manager.py` | Tracked | **KEEP_PRODUCTION** | Backward compatibility shim importing `security.auth_manager`. Declared in `pyproject.toml` `py-modules`. |
| `config.py` | Tracked | **KEEP_PRODUCTION** | Backward compatibility shim importing `core.config`. Declared in `pyproject.toml` `py-modules`. |
| `git_guard.py` | Tracked | **KEEP_PRODUCTION** | Backward compatibility shim importing `security.git_guard`. Declared in `pyproject.toml` `py-modules`. |
| `memory.py` | Tracked | **KEEP_PRODUCTION** | Backward compatibility shim importing `core.memory`. Declared in `pyproject.toml` `py-modules`. |
| `modules.py` | Tracked | **KEEP_PRODUCTION** | Backward compatibility shim importing `core.modules`. Declared in `pyproject.toml` `py-modules`. |
| `plans.py` | Tracked | **KEEP_PRODUCTION** | Backward compatibility shim importing `core.plans`. Declared in `pyproject.toml` `py-modules`. |
| `skills.py` | Tracked | **KEEP_PRODUCTION** | Backward compatibility shim importing `core.skills`. Declared in `pyproject.toml` `py-modules`. |
| `pyproject.toml` | Tracked | **KEEP_PRODUCTION** | Build system, dependencies, packaging, test paths. |
| `requirements.txt` | Tracked | **KEEP_PRODUCTION** | Pip dependencies for runtime and development. |
| `uv.lock` | Tracked | **KEEP_PRODUCTION** | Lockfile for reproducible dependency environments. |
| `.env.example` | Tracked | **KEEP_PRODUCTION** | Sanitized environment template for credentials. |
| `.gitignore` | Tracked | **KEEP_PRODUCTION** | Git ignore rules. Requires addition of `.brainfrog/approvals/`, `.brainfrog/sessions/`, `.pytest_cache/`. |
| `LICENSE` | Tracked | **KEEP_DOCUMENTATION** | Project license (MIT). |
| `README.md` | Tracked | **KEEP_DOCUMENTATION** | Primary user and architecture guide. |
| `BRAINFROG.md` | Tracked | **KEEP_DOCUMENTATION** | BrainFrog system prompt and anti-slop guidelines. |
| `DESIGN.md` | Tracked | **KEEP_DOCUMENTATION** | Dual-system architecture design document. |
| `modules.example.json` | Tracked | **KEEP_DOCUMENTATION** | Example schema for tech stack module configurations. |

---

### B. Root Security Audit & Remediation Reports

| Path | Current Git Status | Classification | Purpose & Analysis |
|---|---|---|---|
| `phase14bm03_remediation_report.md` | Tracked | **ARCHIVE** | Report for finding M-03 (Approval Flooding & Directory Scan DoS). Not needed at repository root; consolidate to `docs/security/phase-14/remediation.md`. |
| `phase14bl01_redteam_report.md` | Tracked | **ARCHIVE** | Audit report for finding L-01 (Session History Growth). Not needed at repository root; consolidate to `docs/security/phase-14/findings.md`. |
| `phase14bl01_remediation_report.md` | Tracked | **ARCHIVE** | Remediation report for finding L-01. Consolidate to `docs/security/phase-14/remediation.md`. |
| `phase14bl01_post_remediation_redteam_report.md` | Untracked | **ARCHIVE** | Post-remediation red-team report identifying F01 and F02. Consolidate to `docs/security/phase-14/findings.md`. |
| `phase14bl01_2_remediation_report.md` | Tracked | **ARCHIVE** | Final remediation report for F01 (OCC) and F02 (Reset Tombstone). Consolidate to `docs/security/phase-14/remediation.md`. |

---

### C. Core Engine & Channels (`core/`)

| Path | Current Git Status | Classification | Purpose & Analysis |
|---|---|---|---|
| `core/__init__.py` | Tracked | **KEEP_PRODUCTION** | Package initializer. |
| `core/config.py` | Tracked | **KEEP_PRODUCTION** | Core configuration loader. |
| `core/frontend_quality_gate.py` | Tracked | **KEEP_PRODUCTION** | Quality gate evaluation for UI changes. |
| `core/image_handler.py` | Tracked | **KEEP_PRODUCTION** | Image manipulation and encoding utility. |
| `core/mcp_client.py` | Tracked | **KEEP_PRODUCTION** | Model Context Protocol client implementation. |
| `core/memory.py` | Tracked | **KEEP_PRODUCTION** | Persistent memory store and learning management. |
| `core/modules.py` | Tracked | **KEEP_PRODUCTION** | Module stack auto-detection and generation. |
| `core/plans.py` | Tracked | **KEEP_PRODUCTION** | Plan schema and sandboxing. |
| `core/pr_proof.py` | Tracked | **KEEP_PRODUCTION** | Proof generation for pull requests. |
| `core/skills.py` | Tracked | **KEEP_PRODUCTION** | Skill registration, loading, and runtime binding. |
| `core/channels/__init__.py` | Tracked | **KEEP_PRODUCTION** | Channel package exports. |
| `core/channels/base.py` | Tracked | **KEEP_PRODUCTION** | Abstract Base Channel interface. |
| `core/channels/telegram.py` | Tracked | **KEEP_PRODUCTION** | Telegram bot adapter and transport. |
| `core/channels/whatsapp.py` | Tracked | **KEEP_PRODUCTION** | WhatsApp webhook adapter. |
| `core/runtime/__init__.py` | Tracked | **KEEP_PRODUCTION** | Runtime package exports. |
| `core/runtime/approval.py` | Tracked | **KEEP_PRODUCTION** | Approval service, quota manager, advisory file lock (`_CrossProcessLock`), two-man rule. |
| `core/runtime/contract.py` | Tracked | **KEEP_PRODUCTION** | Immutable `ApprovedExecutionContract` (C-01, C-02, H-03). |
| `core/runtime/doctor.py` | Tracked | **KEEP_PRODUCTION** | Diagnostics and health checks. |
| `core/runtime/gateway.py` | Tracked | **KEEP_PRODUCTION** | Multi-channel message gateway. |
| `core/runtime/messages.py` | Tracked | **KEEP_PRODUCTION** | Canonical message envelope models. |
| `core/runtime/permissions.py` | Tracked | **KEEP_PRODUCTION** | Channel trust boundaries and deterministic action classifier. |
| `core/runtime/runtime.py` | Tracked | **KEEP_PRODUCTION** | Thin facade over orchestrator; permission gating, in-flight reset guard. |
| `core/runtime/session.py` | Tracked | **KEEP_PRODUCTION** | Session state, FIFO history eviction, OCC revisioning, reset tombstone persistence. |
| `core/runtime/targets.py` | Tracked | **KEEP_PRODUCTION** | Deterministic target path extraction (M-02). |

---

### D. Security, Reasoning Layers & Public Assets

| Path | Current Git Status | Classification | Purpose & Analysis |
|---|---|---|---|
| `security/__init__.py` | Tracked | **KEEP_PRODUCTION** | Security package initializer. |
| `security/auth_manager.py` | Tracked | **KEEP_PRODUCTION** | Local auth and session security tokens. |
| `security/git_guard.py` | Tracked | **KEEP_PRODUCTION** | Pre-commit/remote push security scanner. |
| `system1/__init__.py` | Tracked | **KEEP_PRODUCTION** | System 1 fast heuristics layer. |
| `system1/base.py` | Tracked | **KEEP_PRODUCTION** | System 1 base client interface and data classes. |
| `system1/SKILL.md` | Tracked | **KEEP_PRODUCTION** | System 1 instruction guidelines. |
| `system1/typesafe_client.py` | Tracked | **KEEP_PRODUCTION** | Type-safe HTTP client for Jev model calls. |
| `system2/__init__.py` | Tracked | **KEEP_PRODUCTION** | System 2 deep reasoning layer. |
| `system2/antigravity_client.py` | Tracked | **KEEP_PRODUCTION** | Antigravity CLI / process bridge. |
| `system2/claude_client.py` | Tracked | **KEEP_PRODUCTION** | Anthropic Claude API provider. |
| `system2/json_utils.py` | Tracked | **KEEP_PRODUCTION** | Resilient JSON parsing utility. |
| `system2/openai_client.py` | Tracked | **KEEP_PRODUCTION** | OpenAI-compatible chat completion provider. |
| `system2/visual_inspector.py` | Tracked | **KEEP_PRODUCTION** | Multi-modal visual inspection client. |
| `public/index.html` | Tracked | **KEEP_PRODUCTION** | Mock HTML fixture for frontend quality gate tests. |
| `.brainfrog/skills/...` | Tracked | **KEEP_PRODUCTION** | 4 default skills packaged with runtime (audit-anti-slop, diagnose-code-failure, test-code-behavior, verify-code-result). |
| `.github/workflows/ci.yml` | Tracked | **KEEP_PRODUCTION** | Main GitHub Actions CI workflow. |
| `.github/workflows/stale.yml` | Tracked | **KEEP_PRODUCTION** | Stale PR maintenance workflow. |
| `.github/scripts/protect_active_prs.py` | Tracked | **KEEP_PRODUCTION** | Stale PR automation script. |

---

### E. Test Suite (`tests/`)

| Path | Current Git Status | Classification | Invariant / Coverage Preserved |
|---|---|---|---|
| `tests/__init__.py` | Tracked | **KEEP_TEST** | Test package initializer. |
| `tests/test_adversarial_security_boundary.py` | Tracked | **KEEP_TEST** | Adversarial injection, privilege escalation, channel spoofing. |
| `tests/test_approval_execution_contract.py` | Tracked | **KEEP_TEST** | Invariants C-01 & C-02: Strict filesystem confinement, approved target enforcement. |
| `tests/test_approval_flood_protection.py` | Tracked | **KEEP_TEST** | Invariant M-03: Multi-tier pending quotas, max payload limits, batch cleanup. |
| `tests/test_approval_process_concurrency.py` | Tracked | **KEEP_TEST** | Invariant H-01: Multi-process TOCTOU protection via `_CrossProcessLock`. |
| `tests/test_approval_prompt_history_boundary.py` | Tracked | **KEEP_TEST** | Invariant M-01: History prompt injection isolation. |
| `tests/test_approval_session_invalidation.py` | Tracked | **KEEP_TEST** | Invariant H-02: Stale approval invalidation on `/reset` and incarnation rotation. |
| `tests/test_auth_manager.py` | Tracked | **KEEP_TEST** | Auth token generation and verification. |
| `tests/test_context_memory.py` | Tracked | **KEEP_TEST** | Context and memory injection tests. |
| `tests/test_doctor.py` | Tracked | **KEEP_TEST** | Runtime health-check diagnostics. |
| `tests/test_e2e_remote_approval.py` | Tracked | **KEEP_TEST** | Full end-to-end two-man rule remote approval lifecycle. |
| `tests/test_e2e_telegram_runtime.py` | Tracked | **KEEP_TEST** | Telegram runtime end-to-end integration and security denial. |
| `tests/test_e2e_whatsapp_runtime.py` | Tracked | **KEEP_TEST** | WhatsApp runtime end-to-end integration and security denial. |
| `tests/test_gateway.py` | Tracked | **KEEP_TEST** | Event routing across channels. |
| `tests/test_git_guard.py` | Tracked | **KEEP_TEST** | Secret leak scanning in git diffs. |
| `tests/test_git_remote.py` | Tracked | **KEEP_TEST** | Git remote interaction limits. |
| `tests/test_image_handler.py` | Tracked | **KEEP_TEST** | Image processing tests. |
| `tests/test_json_utils.py` | Tracked | **KEEP_TEST** | Robust JSON repair tests. |
| `tests/test_mcp_quality_gate.py` | Tracked | **KEEP_TEST** | MCP tool quality checks. |
| `tests/test_memory.py` | Tracked | **KEEP_TEST** | Learning persistence and rule retrieval. |
| `tests/test_modes.py` | Tracked | **KEEP_TEST** | Execution modes (build, test, audit). |
| `tests/test_openai_client.py` | Tracked | **KEEP_TEST** | OpenAI provider unit tests. |
| `tests/test_persistent_sessions.py` | Tracked | **KEEP_TEST** | Session recovery, corruption quarantine, atomic replace. |
| `tests/test_pr_proof.py` | Tracked | **KEEP_TEST** | Pull request proof metadata validation. |
| `tests/test_proactive_suggestions.py` | Tracked | **KEEP_TEST** | Learning suggestions based on memory. |
| `tests/test_protect_active_prs.py` | Tracked | **KEEP_TEST** | PR keep-open labeling script tests. |
| `tests/test_remote_git_push_boundary.py` | Tracked | **KEEP_TEST** | Invariant H-03: Enforces remote channel git push prohibition. |
| `tests/test_runtime_boundary.py` | Tracked | **KEEP_TEST** | Thin runtime facade boundary tests. |
| `tests/test_runtime_messages.py` | Tracked | **KEEP_TEST** | Inbound/outbound message normalization. |
| `tests/test_runtime_permissions.py` | Tracked | **KEEP_TEST** | Policy evaluation and action classification. |
| `tests/test_session_concurrency_remediation.py` | Tracked | **KEEP_TEST** | Invariants L01.1-F01 & F02: Multi-process OCC and reset tombstones. |
| `tests/test_session_history_bounds.py` | Tracked | **KEEP_TEST** | Invariant L-01: FIFO 50 entries, 256 KB history limits, UTF-8 safe clamp. |
| `tests/test_skills.py` | Tracked | **KEEP_TEST** | Skill registration, execution, and security checks. |
| `tests/test_system1_jev.py` | Tracked | **KEEP_TEST** | System 1 decision engine tests. |
| `tests/test_system2_providers.py` | Tracked | **KEEP_TEST** | System 2 client routing tests. |
| `tests/test_target_extraction_security.py` | Tracked | **KEEP_TEST** | Invariant M-02: Deterministic regex target classification. |
| `tests/test_telegram_channel.py` | Tracked | **KEEP_TEST** | Telegram transport and formatting tests. |
| `tests/test_tui_ux.py` | Tracked | **KEEP_TEST** | Terminal UI interaction tests. |
| `tests/test_visual_inspector.py` | Tracked | **KEEP_TEST** | Visual inspection tests. |
| `tests/test_whatsapp_channel.py` | Tracked | **KEEP_TEST** | WhatsApp message formatting and verification. |
| `tests/.brainfrog` | Untracked (Directory) | **DELETE** | Empty test leftover directory created by `tests/test_memory.py`. |

---

### F. Scratch Directory (`scratch/`) — All Untracked & Ignored

| Path | Classification | Detailed Justification |
|---|---|---|
| `scratch/adversarial_reproduction_before_after.py` | **DELETE** | Temporary verification script for F01/F02. Permanent verification is in `tests/test_session_concurrency_remediation.py` (Tests A-L). |
| `scratch/audit_concurrency_and_lost_update.py` | **DELETE** | Temporary audit script for Phase 14B-L01.1. Superseded by `tests/test_session_concurrency_remediation.py`. |
| `scratch/audit_step16_18_restart_channels.py` | **DELETE** | Temporary audit test script for channel persistence restart. |
| `scratch/audit_step2_config_bounds.py` | **DELETE** | Temporary test script checking config limits. |
| `scratch/audit_step7_8_12_13_14_15.py` | **DELETE** | Temporary script verifying audit steps. |
| `scratch/audit_step9_10_11_boundaries.py` | **DELETE** | Temporary boundary audit script. |
| `scratch/benchmark_l01_remediation.py` | **DELETE** | One-off benchmark script measuring persistence latency. Results documented in remediation reports. |
| `scratch/build_english_brainfrog.py` | **DELETE** | Experimental script from prior translation task. Not imported or referenced. |
| `scratch/fix_pm.py` | **DELETE** | One-off scratch fix script. |
| `scratch/inject_markers.py` | **DELETE** | One-off marker injection script. |
| `scratch/l01_audit_metrics.json` | **DELETE** | Raw benchmark JSON metrics from L-01 red-team audit. |
| `scratch/log_a.txt`, `scratch/log_b.txt` | **DELETE** | Temporary log output from parallel process runs. |
| `scratch/m03_audit_metrics.json` | **DELETE** | Raw benchmark JSON metrics from M-03 red-team audit. |
| `scratch/poc1_approval_substitution.py` | **DELETE** | Early POC script for approval substitution. Fully covered in `tests/test_approval_execution_contract.py`. |
| `scratch/poc2_multiprocess_race.py` | **DELETE** | Early POC script for multi-process race. Fully covered in `tests/test_approval_process_concurrency.py`. |
| `scratch/poc3_session_reset_orphan.py` | **DELETE** | Early POC script for session reset orphan. Fully covered in `tests/test_approval_session_invalidation.py`. |
| `scratch/pr28_body.md` | **DELETE** | Draft PR description already committed to PR #28 on GitHub. |
| `scratch/remediation_benchmark_results.json` | **DELETE** | Benchmark output from L-01 remediation run. |
| `scratch/render_justified_preview.py` | **DELETE** | One-off TUI render preview script. |
| `scratch/render_new_tui.py` | **DELETE** | One-off TUI render script. |
| `scratch/report_part_b.json` | **DELETE** | Temporary JSON report. |
| `scratch/run_parallel_brainfrog.py` | **DELETE** | One-off parallel execution runner. |
| `scratch/switch_to_log.py` | **DELETE** | One-off logger switch script. |
| `scratch/test_complete_box.py` | **DELETE** | Temporary UI test script. Permanent tests in `tests/test_tui_ux.py`. |
| `scratch/test_l01_redteam_audit.py` | **DELETE** | Audit script for L-01 red-team. Permanent regression tests in `tests/test_session_history_bounds.py`. |
| `scratch/test_m03_stress_benchmark.py` | **DELETE** | Audit script for M-03 stress benchmark. Permanent tests in `tests/test_approval_flood_protection.py`. |
| `scratch/test_mcp_concurrency.py` | **DELETE** | One-off concurrency check for MCP client. Permanent tests in `tests/test_mcp_quality_gate.py`. |
| `scratch/test_mention_enter.py` | **DELETE** | One-off UI key handling script. |
| `scratch/test_stream_align.py` | **DELETE** | One-off stream alignment script. |
| `scratch/test_tui_ux.py` | **DELETE** | Duplicate of `tests/test_tui_ux.py`. |
| `scratch/verify_box_and_footer.py` | **DELETE** | One-off UI verification script. |
| `scratch/concurrency_results/` | **DELETE** | Temporary screenshots and JSON results from old concurrency test. |
| `scratch/__pycache__/` | **DELETE** | Python bytecode cache. |

---

### G. Runtime & Cache Directories

| Path | Current Git Status | Classification | Purpose & Action |
|---|---|---|---|
| `.brainfrog/approvals/` | Untracked | **DELETE** (from workspace) / **KEEP_PRODUCTION** (code creates at runtime) | Local execution approvals data. Add to `.gitignore`. |
| `.brainfrog/sessions/` | Untracked | **DELETE** (from workspace) / **KEEP_PRODUCTION** (code creates at runtime) | Local execution session data. Add to `.gitignore`. |
| `.brainfrog/scratch/` | Untracked | **DELETE** (from workspace) / **KEEP_PRODUCTION** (code creates at runtime) | Local screenshot clipboard caches. Add to `.gitignore`. |
| `.pytest_cache/` | Untracked | **DELETE** | Pytest runtime cache. Add to `.gitignore`. |
| `__pycache__/` (all) | Untracked | **DELETE** | Python compiled bytecode. Already covered in `.gitignore`. |
| `brainfrog.egg-info/` | Untracked | **KEEP_PRODUCTION** | Editable pip install metadata (`pip install -e .`). Covered in `.gitignore`. |
| `.venv/` | Untracked | **KEEP_PRODUCTION** | Local Python virtual environment. Covered in `.gitignore`. |

---

## 3. Justification for Candidate Actions

### Why Archive/Consolidate Audit Reports?
- **Root Pollution:** 5 individual reports (`phase14bm03_remediation_report.md`, `phase14bl01_redteam_report.md`, etc.) currently sit in the repository root, cluttering the top-level directory.
- **Redundancy:** The findings and remediations follow a chronological cycle where later reports supersede earlier investigations (e.g., L-01 reproduction $\to$ remediation $\to$ post-remediation red-team finding F01/F02 $\to$ final concurrency remediation).
- **Structure:** Consolidating these into `docs/security/phase-14/findings.md` and `docs/security/phase-14/remediation.md` with a high-level `docs/security/README.md` index cleanly preserves 100% of the technical security facts while presenting a clean, professional production structure.

### Why Delete Scratch Files?
- **Zero Runtime Dependency:** None of the files in `scratch/` are imported by any module in `core/`, `security/`, `system1/`, `system2/`, `cli.py`, or `orchestrator.py`.
- **Zero CI Dependency:** GitHub Actions CI executes only `compileall` and `unittest discover -s tests`.
- **Zero Lost Test Coverage:** All real invariants tested in `scratch/` have permanent, robust, dedicated test suites in `tests/`:
  - `poc1_approval_substitution.py` $\to$ covered in `tests/test_approval_execution_contract.py`
  - `poc2_multiprocess_race.py` $\to$ covered in `tests/test_approval_process_concurrency.py`
  - `poc3_session_reset_orphan.py` $\to$ covered in `tests/test_approval_session_invalidation.py`
  - `test_m03_stress_benchmark.py` $\to$ covered in `tests/test_approval_flood_protection.py`
  - `test_l01_redteam_audit.py` $\to$ covered in `tests/test_session_history_bounds.py`
  - `adversarial_reproduction_before_after.py` $\to$ covered in `tests/test_session_concurrency_remediation.py` (Tests A through L)

### Why Fix `tests/test_memory.py` TearDown?
- In `tests/test_memory.py`, line 60 sets `self.tmp_path = Path(__file__).parent / ".brainfrog" / "test_learnings.json"`.
- `tearDown()` deletes `test_learnings.json` but leaves the parent directory `tests/.brainfrog` behind.
- Using `tempfile.TemporaryDirectory()` avoids writing artifacts directly inside the `tests/` directory tree during test execution.

---

## 4. Planned Actions Sequence

1. **Create Structured Security Documentation (`docs/security/`):**
   - Create `docs/security/README.md` providing an overview of the BrainFrog security model and finding status.
   - Create `docs/security/phase-14/findings.md` documenting findings C-01, C-02, H-01, H-02, H-03, M-01, M-02, M-03, L-01, L01.1-F01, and L01.1-F02.
   - Create `docs/security/phase-14/remediation.md` documenting the exact remediation architecture, invariants, and test verification.
   - Remove root-level `phase14*.md` files once consolidated.
2. **Update `.gitignore`:**
   - Add `.brainfrog/approvals/`, `.brainfrog/sessions/`, `.brainfrog/scratch/`, and `.pytest_cache/` so local runs never leave dirty git working trees.
3. **Clean Untracked Test Artifacts:**
   - Clean up `tests/.brainfrog` and refine `tests/test_memory.py` to use `tempfile.TemporaryDirectory()`.
   - Remove temporary files in `scratch/`.
4. **Validation:**
   - Run `python -m compileall`
   - Run `npx pyright`
   - Run `python -m flake8`
   - Run full 414 test suite: `python -m unittest discover -s tests`
   - Verify CI and GitHub status.
