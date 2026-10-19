# Phase 4B — Final Behavioral Integrity, Plan Reconciliation & Safe Resume Audit Report

**Repository:** `dameepng/brainfrog`
**Workspace:** `C:\dame-project\tools\agentic_dev`
**Branch:** `feat/trustworthy-verification-workflow`
**HEAD:** `2d63615eab817cdd6b9ca69ce805c69621176bd1`
**Date:** October 19, 2026
**Status:** **DONE**

---

## 1. Initial Architecture Findings

An audit was conducted on Brainfrog Phase 4 across `core/plans.py`, `core/runtime/contract.py`, `core/runtime/approval.py`, `core/runtime/step_runner.py`, `orchestrator.py`, and corresponding regression tests.

### Findings

1. **Clarification Answer Isolation**: In the pre-audit implementation, resolving a blocking unknown via `/plan resolve <id> <answer>` (`PlanRegistry.resolve_blocking_unknown`) persisted the resolution inside `.meta.json` under `blocking_unknowns[].resolved`, `answer`, and `resolved_at`. However, when BUILD executed steps, `get_step_architectural_context` and `format_step_architecture_context` completely ignored resolved answers. System 2 `write_code` received step descriptions containing obsolete defaults without the authoritative resolved requirements.
2. **Silent Contradiction Blindness**: Resolving an unknown unconditionally flipped plan status from `BLOCKED` to `READY` as long as no other unknowns remained unresolved, even when the user's answer directly contradicted an existing step description or acceptance criterion (e.g., answer stated "30 minutes", while step description stated "Default token expiration is 60 minutes").
3. **Incomplete Checkpoint Invalidation**: `_invalidate_affected_steps_progress` only inspected explicit `affected_steps` IDs declared in the blocking unknown. Downstream dependent steps (declared in `dependencies`, `depends_on`, or sharing file targets) remained marked `PASSED`, risking execution of stale or incompatible code without revalidation.
4. **Workspace Drift Vulnerability**: `StructuredStepRunner.run_steps` checked `step_progress.status == "passed"` and skipped steps without validating workspace file integrity. If code in the workspace was modified, corrupted, or deleted out-of-band between sessions, or if requirement digests changed, the runner blindly accepted stale verification evidence.
5. **Semantic Digest Incompleteness**: `compute_metadata_semantic_digest` excluded `acceptance_criteria` and `verification_commands`. Altering acceptance criteria or verification commands in `.meta.json` after approval did not change the cryptographic hash, failing to invalidate stale execution contracts.

---

## 2. Actual Answer-to-BUILD Data Flow

The complete, end-to-end data flow was traced, verified, and hardened:

```text
User provides clarification answer via `/plan resolve <unknown_id> <answer>`
        |
        v
PlanRegistry.resolve_blocking_unknown()
  - Validates unknown ID & non-empty answer
  - Records answer, resolved=True, resolved_at
  - Evaluates contradiction against step descriptions and acceptance criteria
  - If contradiction detected: marks plan status as DRAFT (requires human review/replan)
  - Invalidates direct, transitive (DAG), and ambiguous (file-sharing) step progress records
        |
        v
PlanRegistry.get_step_architectural_context()
  - Extracts resolved clarifications mapped to the step (by affected_steps, affected_files, or global)
  - Binds unknown ID, question, decision topic, and authoritative resolved answer
        |
        v
PlanRegistry.format_step_architecture_context()
  - Injects high-priority section:
    "[Architectural Constraints & Boundaries] - Authoritative Clarifications & Resolved Requirements (SUPERCEDES CONFLICTING DEFAULTS)"
        |
        v
Orchestrator._execute_step()
  - Formats effective task containing step instructions + explicit resolved requirements
  - Passes effective task & architecture context directly to System 2 `write_code`
        |
        v
System 2 write_code(task=effective_task, architectural_context=context)
  - Consumes authoritative clarification text; obsolete defaults are overridden
```

### Verification Proof
In test `test_01_clarification_answer_reaches_build_write_code`, Step 2 task provided to `write_code` demonstrably contained:
`[Resolved Clarifications & Authoritative Answers]: - [blk_auth_expiry] Access token expiration duration: 30 minutes (Decision: Token Expiry Policy)`. Obsolete defaults ("60 minutes") were explicitly superseded.

---

## 3. Plan Reconciliation Policy

Answering a question and incorporating that answer into an executable plan are treated as two distinct operations:

1. **Intake Operation (`resolve_blocking_unknown`)**: Records the user-supplied requirement.
2. **Contradiction Detection (`_detect_plan_contradiction`)**:
   - Compares the resolved answer against step descriptions and acceptance criteria.
   - Detects numerical mismatches (e.g., answer "30 minutes" vs plan "60 minutes"), negative/positive polarity conflicts (e.g., "disable" vs "enable"), and architectural keyword clashes.
3. **Fail-Closed Demotion Gate**:
   - If a contradiction is detected, the plan status is **not** transitioned to `READY`. It transitions to or remains in `DRAFT`.
   - The transition evidence records `contradiction_detected: True` and details of conflicting steps.
   - The user must explicitly edit and re-review the plan markdown/steps before issuing a fresh approval.
4. **No Autonomous Unsupervised Rewriting**:
   - The runtime does not invent an uncontrolled semantic re-writer that alters step descriptions behind the user's back.
   - Inconsistencies require human oversight and explicit reapproval.

---

## 4. Approval and Semantic Digest Coverage

To ensure `ApprovedExecutionContract` binds exactly what BUILD executes:

1. **`compute_metadata_semantic_digest` Fields Covered**:
   - `plan_id`, `plan_name`, `domain`, `change_type`
   - `steps` (step numbers, descriptions, target files, acceptance criteria, verification commands)
   - `acceptance_criteria` (plan-level)
   - `verification_commands` (plan-level)
   - `architectural_constraints` & `compliance_rules`
   - `blocking_unknowns` (including all questions, decisions, and resolved answers)
2. **Ephemeral Lifecycle State Exclusion**:
   - Ephemeral operational fields (`status`, `revision`, `updated_at`, `transitions`) are excluded from `plan_metadata_hash` so that legitimate state transitions (`READY` -> `IN_PROGRESS`) do not trigger false tampering errors.
3. **Tampering Detection**:
   - Modifying plan steps, acceptance criteria, verification commands, or clarification answers alters the SHA-256 digest.
   - Resuming execution with an approval issued under an older digest immediately fails closed with `InvalidApprovalContractError` ("Plan metadata hash mismatch").

---

## 5. Acceptance Criteria Update Rules

1. Any modification to acceptance criteria or verification commands alters the plan metadata hash.
2. An existing `ApprovedExecutionContract` referencing the previous digest is rejected upon execution resumption.
3. BUILD cannot execute under modified acceptance criteria without an explicit, fresh user reapproval.

---

## 6. Checkpoint and Requirement Digest Behavior

`core/runtime/step_runner.py` progress tracking has been enhanced with cryptographic evidence binding:

1. **`StepProgressRecord` Schema**:
   - `step_number`: Step identifier.
   - `status`: `"pending"`, `"in_progress"`, `"passed"`, or `"failed"`.
   - `verified_at`: Timestamp of verification.
   - `evidence`: Verification command outputs.
   - `file_hashes`: Dictionary mapping step target files to their SHA-256 digests at verification time.
   - `requirement_digest`: SHA-256 hash of the step description, target files, and relevant resolved clarifications at verification time.
2. **Staleness Invalidation**:
   - When a step's requirements or target files change, its requirement digest changes.
   - A step marked `passed` whose requirement digest no longer matches current step parameters is recognized as stale and forced to re-run.

---

## 7. Direct and Transitive Invalidation Behavior

When a clarification is resolved in `PlanRegistry.resolve_blocking_unknown()`:

1. **Direct Invalidation**: All steps listed in `unknown.affected_steps` are immediately reset from `passed` to `pending`.
2. **Transitive Dependency Invalidation (DAG Closure)**:
   - Traverses explicit step dependency links (`dependencies`, `depends_on`, `requires`).
   - If Step 2 is affected, Step 3 (depending on Step 2) and Step 4 (depending on Step 3) are transitively invalidated and reset to `pending`.
3. **Ambiguous Dependency Invalidation (Resource Overlap)**:
   - If a step modifies any file listed in `unknown.affected_files`, but was not explicitly declared in `affected_steps` or dependency edges, it is classified as ambiguous.
   - In accordance with fail-closed safety, ambiguous steps are conservatively invalidated to prevent stale execution.
4. **Unaffected Independent Step Preservation**:
   - Completely independent steps (e.g., Step 0 creating an isolated logger in `src/logger.py` with no shared files or dependencies) retain their `passed` status and are reused without duplicate re-execution.

---

## 8. Workspace Drift and Verification Evidence Policy

In `StructuredStepRunner.run_steps`:

1. **Pre-Execution Workspace Verification**:
   - Before skipping any step previously marked `passed`, the runner inspects `record.file_hashes`.
   - Computes live SHA-256 hashes of all target files on disk.
2. **Drift Detection**:
   - If any verified file has been modified, tampered with, or deleted out-of-band:
     - The step's status is reset to `failed`.
     - The step is re-executed and re-verified.
3. **External Side Effects**:
   - Immutable/irreversible operations cannot be safely replayed blindly; safe idempotent re-runs are enforced via step scope bounds. Attempts to write outside step target scopes trigger orchestrator scope violations.

---

## 9. Confirmed Defects and Pre-Fix Reproductions

| ID | Component | Defect Description | Pre-Fix Reproduction | Severity |
|---|---|---|---|---|
| **DEF-01** | `core/plans.py`, `orchestrator.py` | Clarification answers were stored in metadata but never propagated into step architectural context or effective task passed to System 2 `write_code`. | `test_01_clarification_answer_reaches_build_write_code`: `step_2_calls[0]["task"]` lacked user answer `30 minutes`. | **High (Behavioral Correctness)** |
| **DEF-02** | `core/plans.py` | `resolve_blocking_unknown` unconditionally marked plans `READY`, even when user answers directly contradicted step descriptions. | `test_02_conflicting_default_cannot_override_answer`: Plan transitioned to `READY` despite step stating "Default is 60 minutes" and answer specifying "30 minutes". | **High (Behavioral Safety)** |
| **DEF-03** | `core/plans.py` | `_invalidate_affected_steps_progress` only invalidated direct step IDs, failing to invalidate downstream DAG dependents or ambiguous file-sharing steps. | `test_09_transitive_dependency_invalidation`: Steps 3 and 4 remained `passed` after Step 2 was altered. | **High (Execution Integrity)** |
| **DEF-04** | `core/runtime/step_runner.py` | `StructuredStepRunner.run_steps` skipped `passed` steps unconditionally without checking workspace file hashes or requirement digests. | `test_12_workspace_drift_invalidates_checkpoint_evidence`: External tampering with `src/logger.py` resulted in silent skip (`executed_steps: []`). | **High (Resume Integrity)** |
| **DEF-05** | `core/plans.py` | `compute_metadata_semantic_digest` omitted `acceptance_criteria` and `verification_commands`, allowing out-of-band criteria changes without invalidating contract hashes. | `test_07_changed_criteria_after_approval_invalidates_authorization`: Altering verification commands did not change digest. | **Medium (Security Boundary)** |

---

## 10. Fixes Implemented

1. **Authoritative Clarification Injection (`core/plans.py`, `orchestrator.py`)**:
   - `PlanRegistry.get_step_architectural_context()` retrieves all resolved blocking unknowns applicable to a step.
   - `PlanRegistry.format_step_architecture_context()` formats them under a dedicated high-priority section `[Architectural Constraints & Boundaries] - Authoritative Clarifications & Resolved Requirements (SUPERCEDES CONFLICTING DEFAULTS)`.
   - `Orchestrator._execute_step()` embeds resolved requirements directly into `effective_task`.
2. **Contradiction Detection & Review Gate (`core/plans.py`)**:
   - Implemented `PlanRegistry._detect_plan_contradiction()`.
   - In `resolve_blocking_unknown()`, if a contradiction is detected, plan status transitions to `DRAFT` with `contradiction_detected: True` in the transition evidence.
3. **Transitive & Ambiguous Checkpoint Invalidation (`core/plans.py`)**:
   - Implemented full transitive closure traversal over step dependency graphs (`dependencies`, `depends_on`, `requires`).
   - Implemented resource-overlap checks (`step.target_files` intersecting `unknown.affected_files`).
   - Automatically resets all affected and downstream steps from `passed` to `pending`.
4. **Workspace Drift & Evidence Hash Binding (`core/runtime/step_runner.py`)**:
   - Added `file_hashes` and `requirement_digest` to `StepProgressRecord`.
   - In `PlanProgressTracker.mark_passed()`, calculates and stores SHA-256 hashes of all step target files and requirement strings.
   - In `StructuredStepRunner.run_steps()`, revalidates file hashes against disk and re-verifies requirement digests. Resets modified steps to `failed` to force re-execution.
5. **Cryptographic Coverage of Criteria & Commands (`core/plans.py`)**:
   - Included `acceptance_criteria` and `verification_commands` in `compute_metadata_semantic_digest`.

### Limitations of Fixes
- **Heuristic Contradiction Detection**: Semantic contradiction detection in `_detect_plan_contradiction` uses deterministic token/keyword/numeric conflict checks. Highly nuanced or figurative semantic conflicts still depend on human review of the generated plan during approval.
- **Single Machine File Hashes**: Workspace drift detection assumes direct filesystem access to calculate SHA-256 digests. Remote or distributed object stores require provider-specific digest mechanisms.

---

## 11. Test Results

### 1. Dedicated Audit Suite (`tests/test_architecture_clarification_integrity.py`)
19 comprehensive tests validating Scenarios A–L and Items 1–18:

```text
test_01_clarification_answer_reaches_build_write_code .............................. [PASS]
test_02_conflicting_default_cannot_override_answer ................................ [PASS]
test_03_material_plan_reconciliation_requires_review .............................. [PASS]
test_04_acceptance_criteria_match_clarified_requirements .......................... [PASS]
test_05_stale_approval_rejected_after_resolution .................................. [PASS]
test_06_fresh_approval_binds_updated_semantics .................................... [PASS]
test_07_changed_criteria_after_approval_invalidates_authorization ................. [PASS]
test_08_direct_dependency_invalidation ............................................ [PASS]
test_09_transitive_dependency_invalidation ........................................ [PASS]
test_10_unaffected_checkpoint_reuse ............................................... [PASS]
test_11_ambiguous_dependency_handled_conservatively ............................... [PASS]
test_12_workspace_drift_invalidates_checkpoint_evidence ........................... [PASS]
test_13_crash_restart_with_durable_state .......................................... [PASS]
test_14_no_mutation_before_valid_reapproval ....................................... [PASS]
test_15_no_permission_expansion_through_answer_text ............................... [PASS]
test_16_legacy_plan_and_approval_safety ........................................... [PASS]
test_17_cancellation_and_partial_transaction_recovery ............................. [PASS]
test_18_no_duplicate_execution_of_verified_independent_work ....................... [PASS]
test_19_full_adversarial_clarification_lifecycle .................................. [PASS]

19 tests passed in 8.05s
```

### 2. Full Architecture Test Suites Combined

```powershell
python -m unittest tests.test_architecture_clarification_integrity tests.test_architecture_clarification_resume tests.test_architecture_handoff_final_security tests.test_architecture_handoff_adversarial tests.test_architecture_plan_build_handoff -v
```
**Result: Ran 87 tests in 48.51s — OK (100% PASS, 0 failures, 0 errors).**

### 3. Core Regression Suites

```powershell
python -m unittest tests.test_plan_registry tests.test_planning tests.test_approval_execution_contract tests.test_transaction_recovery tests.test_mode_contracts -v
```
**Result: Ran 149 tests in 38.85s — OK (skipped=1, 0 failures, 0 errors).**

### 4. Code & Git Hygiene Scans
- `git diff --check`: Clean (0 errors).
- `python -m security.git_guard --staged`: Clean (0 secrets detected).
- `python -m security.git_guard --file <modified_files>`: Clean (100% clean across all modified and test files).

---

## 12. Enforced Properties Matrix

| Property | Enforcement Type | Runtime Mechanism |
|---|---|---|
| Clarification reach BUILD context | **Deterministic Code** | `PlanRegistry.get_step_architectural_context()` + `Orchestrator._execute_step()` |
| Obsolete default override | **Deterministic Code** | Context formatting with high-priority override banners |
| Material contradiction gate | **Deterministic Code + Human Review** | `PlanRegistry._detect_plan_contradiction()` gates status to `DRAFT` |
| Execution authorization | **Cryptographic Contract** | `ApprovedExecutionContract.plan_metadata_hash` SHA-256 match |
| Transitive dependency invalidation | **Deterministic Code** | DAG traversal in `_invalidate_affected_steps_progress()` |
| Ambiguous dependency invalidation | **Deterministic Code** | Target file overlap detection resets progress to `pending` |
| Workspace drift detection | **Cryptographic Verification** | SHA-256 file hashing in `StructuredStepRunner.run_steps()` |
| Permission boundary enforcement | **Deterministic Code** | Step-scoped path validator in `Orchestrator` |

---

## 13. Remaining Risks

1. **Subtle Natural-Language Contradictions**: Complex linguistic subtleties not captured by token overlap or numeric checking rely on the operator reviewing the plan before issuing approval.
2. **External Non-Idempotent Tool Commands**: If custom verification commands alter external network or database states outside the local workspace, those external side-effects are not tracked by local file hashing.

---

## 14. Phase 4 Closure Verdict & Phase 5 Recommendation

- **Verdict:** **DONE**. All 15 acceptance criteria of Phase 4B are fully met and verified by deterministic runtime code and automated tests.
- **Phase 5 Readiness:** The system's plan handoff, clarification intake, reapproval invalidation, DAG dependency tracking, and checkpoint recovery are robust, cryptographically verified, and ready for Phase 5.
- **Action:** Phase 4 is officially closed. No Phase 5 work was initiated in this session, and no git commits or pushes were performed.
