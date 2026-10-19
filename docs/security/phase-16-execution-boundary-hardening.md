# Phase 1.6 — Execution Boundary Hardening: Report

**Status:** Implementation complete; validation complete; **not committed** (commit/push requires explicit user authorization per repo policy).

**Scope:** Close the build-mode execution bypass (R1) so no workspace mutation can occur without a validated `ApprovedExecutionContract`; repair stale tests; make CLI chat/plan/build mode handling consistent; migrate all E2E tests and the reproducible-proof demo that legitimately exercise the execution pipeline to the canonical approval → contract → execution flow, preserving 100% of their assertions.

---

## 1. Root Cause (R1)

Build-mode execution previously reached `Orchestrator.run()` without any `ApprovedExecutionContract`, allowing uncontrolled mutation. The remediation adds a fail-closed guard at the orchestrator boundary and routes all legitimate build-mode execution through the approval flow:

```
1. request  -> runtime queues an approval request  (status: pending_approval / REJECTED with request_id)
2. approve  -> a distinct operator identity approves (two-man rule; approver != requester)
3. execute  -> resubmit the exact operation bound to approval_id; runtime consumes the
               approval and mints the ApprovedExecutionContract
```

Guard behavior: build-mode execution without a contract raises `RuntimeError("Build mode is defined, but plan selection and controlled execution are not implemented yet.")` — surfaced as a bounded refusal for remote/untrusted channels (runtime.py ~1850-1866) and as `FAILED`/`REJECTED` for local channels.

## 2. Source Changes

| # | Area | Change |
|---|------|--------|
| P0 | `orchestrator.py` (`run()` guard ~1385) | Fail-closed guard: build-mode execution without contract is refused at the orchestrator boundary. |
| P1 | `core/plans.py`, `core/runtime/session.py`, `core/runtime/runtime.py` | Mode defaults/consistency: `DEFAULT_MODE_LOCAL="build"`, `DEFAULT_MODE_REMOTE="plan"`; metadata `mode` honored with normalization; invalid-metadata emit fix; `plan_context` removal. |
| P2 | `cli.py` | CLI chat/plan/build consistency: mode persistence, `/mode`/`/plan`/`/build`, argparse wiring; interactive mode guards. |
| P3 | `core/runtime/permissions.py`, `core/runtime/__init__.py`, `core/runtime/runtime.py` | Defense-in-depth + observability: anti-spoofing allowlist for cli channels (`local`/`cli`/`system`/`terminal`/`default` + `$USERNAME`); unknown cli users downgraded to `remote_channel`; remote+build bounded refusal. |
| F1 | `core/runtime/runtime.py` ~2115 | **Error-path evidence preservation:** the orchestrator `except Exception` handler now preserves `cfg.last_transaction_result` (`transaction_id`, `transaction_status`, `transaction_success`, `transaction_result`) and `cfg.last_test_result` in the outgoing metadata, so rollbacks correctly map to `ROLLED_BACK` and proofs carry full transaction evidence (previously lost → misclassified as `FAILED`). |
| F2 | `orchestrator.py` ~1574, called ~1669/1678 | **Contract commit finalization:** `_record_contract_commit_finalization(tx_res)` records a durable commit intent via `TaskFinalizationCoordinator.record_commit_intent(task_id, transaction_id=...)` + `record_commit_success(task_id)` after successful contract-path commits (`VERIFIED` / `COMMITTED_UNTESTED`), mirroring the git-commit lifecycle so proof-persistence failure after a commit yields `COMMITTED_PENDING_PROOF` recovery semantics instead of an inconsistent state. |

## 3. Test & Demo Migration

New shared helper: **`tests/approval_flow_helpers.py`** — drives the 3-step dance through public entry points only (runtime `execute_autonomous_task`, CLI `handle_message`, SDK client):

- `approval_metadata()`, `find_pending_request()`, `approve_request()`, `request_approval()`, `execute_request()`
- `run_approved_task(runtime, request, target=...)` — full runtime dance; returns final `TaskResult`
- `run_sdk_with_approval` / `run_sdk_sync_with_approval` — full SDK dance (async/sync)
- `prepare_sdk_approval` / `prepare_sdk_sync_approval` — step 1 + approve, returning metadata bound to `approval_id` (for `client.start()` + cancellation flows)
- `_sdk_pending_id()` — pending discovery via `client.list_pending_approvals()`, with a runtime-store fallback filtered by canonical operation scope (needed when a client's `conversation_id` differs from the request session)

Migrated suites (all assertions preserved unless noted in §5):

| Suite | Migration | Result |
|-------|-----------|--------|
| `tests/test_runtime_boundary.py` | QA flow via `user_id="local"` + `metadata={"mode": "plan"}` (build-mode QA now intentionally fail-closed) | 3 passed |
| `tests/test_phase3_e2e.py` | 4 tests via `run_approved_task(..., target="navbar.py")` | 11 passed |
| `tests/test_phase4_proof.py` | 2 E2E tests via `run_approved_task` | 24 passed |
| `tests/test_phase6b_cli_verification.py` | tests 12/13/14: CLI path via `capture_exec` wrapper around `cli.execute_autonomous_task`; SDK path via `run_sdk_sync_with_approval` | 15 passed |
| `tests/test_post_task_change_summary.py` | scenarios 20/21 via `run_approved_task` | 24 passed |
| `tests/test_reproducible_proof_demo.py` + `demo/reproducible_proof_demo.py` | Demo now **demonstrates the canonical production flow inline** (`_execute_with_approval`): gate → queue → approve as distinct operator → execute; no dependency on `tests/` | 8 passed |
| `tests/test_phase5_sdk.py` | 19 tests migrated: one-shot runs via SDK helpers; `start()`/cancel flows via `prepare_sdk_*_approval`; late-cancellation test pre-approves then runs the final (hooked) execution; CLI-parity test uses the CLI capture pattern; redaction fixtures converted to synthetic generators | 48 passed, 2 pre-existing failures (§6) |

## 4. Evidence

| Validation | Command (venv python, repo root) | Result |
|-----------|-----------------------------------|--------|
| Migrated + approval aggregate | `pytest tests/test_runtime_boundary.py tests/test_phase3_e2e.py tests/test_phase4_proof.py tests/test_phase6b_cli_verification.py tests/test_post_task_change_summary.py tests/test_reproducible_proof_demo.py tests/test_phase5_sdk.py tests/test_approval_execution_contract.py -q` | **147 passed, 1 skipped, 2 failed** (both pre-existing, §6) in 8:54 |
| Security/permission/mode regression | `pytest tests/test_adversarial_security_boundary.py tests/test_modes.py tests/test_mode_contracts.py tests/test_runtime_permissions.py tests/test_approval_flood_protection.py tests/test_approval_session_invalidation.py tests/test_capability_contract.py tests/test_p1_4b_security_audit.py tests/test_subagent_security_audit.py tests/test_target_extraction_security.py -q` | **219 passed, 3 skipped, 180 subtests passed** in 1:05 |
| Approval concurrency / history / remote | `pytest tests/test_approval_process_concurrency.py tests/test_approval_prompt_history_boundary.py tests/test_e2e_remote_approval.py -q` | **55 passed** in 1:58 |
| Whitespace | `git diff --check` | clean |
| Secret scanning | `python -m security.git_guard --file <each changed/added file>` | clean (see §5 synthetic fixtures) |

Baseline attribution: failures in the migrated suites were guard-caused (present in the worktree run, absent at HEAD), verified by comparing full-suite failure lists (`mine_failed_ids.txt` vs `baseline_failed_ids.txt`).

## 5. Decisions & Amendments

1. **Contract-path commits are `TransactionStatus.VERIFIED`** (legacy direct path: `COMMITTED`). Two test accommodations follow:
   - `tests/test_phase3_e2e.py`: report line accepts `("committed", "verified")`.
   - `tests/test_phase5_sdk.py::test_sdk_proof_artifact_generation_and_inspection`: `proof.changes.status in ("COMMITTED", "VERIFIED")`.
   - `TaskResult.status` remains `"COMMITTED"` (verified in 20+ migrated assertions).
2. **QA/mode metadata:** runtime-boundary conversational QA runs under `metadata={"mode": "plan"}` because build-mode conversational execution is now (by design) contract-gated.
3. **Demo shows the real production flow:** the demo performs the approval dance inline rather than depending on test utilities; the printed narrative now includes the human sign-off step.
4. **Flaky-cancellation semantics preserved:** SDK cancellation tests now pre-approve, then cancel during the approved execution — cancellation token still propagates via request metadata (`_CURRENT_CANCEL_EVENT`), so mid-planning and mid-mutation cancellation remain genuinely exercised.
5. **Synthetic secret fixtures (Safe Test Fixtures & Git Hygiene Rule):** pre-existing realistic literals in `tests/test_phase5_sdk.py`, `tests/test_phase4_proof.py` (7 incidents) and `tests/test_post_task_change_summary.py` (1 incident) were replaced with deterministic pattern-conformant synthetic generators (`_synthetic_github_token()`, `_synthetic_google_key()`, `_synthetic_openai_key()`); all redaction assertions preserved (synthetic tokens still match the redaction regexes in `core/runtime/proof.py:64-69`). GitGuard now reports all touched files clean.

## 6. Pre-existing Failures (Out of Scope, Documented)

Present at HEAD before Phase 1.6 (confirmed by baseline comparison):

| Test | Cause |
|------|-------|
| `tests/test_phase5_sdk.py::TestSDKErrors::test_workspace_busy_error_on_locked_workspace` | Runtime returns `FAILED` instead of the expected `"LOCKED"` status when the workspace lock is held (status-mapping gap; exists at HEAD). |
| `tests/test_phase5_sdk.py::TestSDKAdversarial::test_path_traversal_in_scope_rejected` | Second assertion uses `allowed_scope=["/absolute/path/outside"]`; on Windows, `os.path.isabs("/absolute/...")` is `False` (root-relative), so the SDK validator does not raise (platform-specific fixture issue). |

Broader pre-existing environment failures outside the migrated suites (35 at triage: phase2b/2c hardened-subprocess `EXECUTION_ERROR`, git-on-tmp-path, workspace registry, system2 providers) remain unchanged and are tracked separately from this phase.

## 7. Definition of Done Checklist

- [x] R1 bypass closed with fail-closed guard + canonical approval flow enforced end-to-end (runtime, CLI, SDK).
- [x] All guard-caused E2E failures migrated with preserved security assertions; demo demonstrates canonical behavior.
- [x] Regression suites (security/permission/mode/approval) pass.
- [x] `git diff --check` clean; GitGuard scans clean on all touched files (synthetic fixtures).
- [ ] **Commit/push — awaiting explicit user authorization** (per `AGENTS.md` §2.5 and repo git policy).
- [x] No unrelated working-tree changes modified (untracked user artifacts preserved: `.agents/`, `.githooks/`, `scripts/`, `core/runtime/model_switcher.py`, `tests/test_telegram_*.py`, `AGENTS.md`).
