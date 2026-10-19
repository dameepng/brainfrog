# Phase 3: Architecture-Aware PLAN → BUILD Handoff Specification & Implementation

## 1. Executive Summary

This document establishes the architecture-aware PLAN → BUILD handoff mechanism implemented in Phase 3 of the Agent-Friendly Architecture initiative in BrainFrog.

Phase 2 enhanced the four engineering skills (`understand-codebase`, `implement-code-change`, `refactor-code-safely`, and `review-code-change`) with architectural reasoning taxonomy and decision principles. Phase 3 connects that reasoning directly into BrainFrog's core lifecycle: ensuring architectural decisions, constraints, invariants, change boundaries, and missing decisions identified during **PLAN** mode survive persistence and are faithfully propagated, bounded, and enforced throughout **BUILD** execution—all without expanding or weakening the runtime authorization and security model.

---

## 2. The Actual PLAN → BUILD Data Path

The end-to-end data flow operates through a strict lifecycle pipeline:

```
[PLAN Mode Request]
       │
       ▼
[System 2: plan_and_prd] ─── (generates structured plan & PRD dictionary)
       │
       ▼
[Plan Normalization & Companion Metadata] ─── (captures architecture fields)
       │
       ▼
[save_plan_document & WAL Journal] ─── (persists .md and .meta.json with lock)
       │
       ▼
[Plan Selection & Approval Flow] ─── (user review; mints ApprovedExecutionContract)
       │
       ▼
[BUILD Mode Request] ─── (requires valid, unexpired ApprovedExecutionContract)
       │
       ▼
[Plan Loading & Binding Validation] ─── (PlanRegistry loads doc; contract verifies disk hash & rev)
       │
       ▼
[Pre-Step Gate: Blocking Unknowns Check] ─── (StructuredStepRunner & _execute_step verify readiness)
       │
       ├─── IF unresolved blocking unknown affects step ──> FAIL-CLOSED (raises BlockingUnknownError)
       │
       └─── IF ready & authorized:
                │
                ▼
       [Step Context Extraction] ─── (plan.get_step_architectural_context(step))
                │
                ▼
       [Bounded Prompt Injection] ─── (format_step_architecture_context(context))
                │
                ▼
       [System 2: write_code] ─── (receives step, scoped task + architecture boundaries)
                │
                ▼
       [Step Scope Enforcement] ─── (enforces new_files strictly within step.files)
                │
                ▼
       [Transaction Staging & Execution] ─── (atomic isolation under WorkspaceLock)
                │
                ▼
       [Verification Test Suite] ─── (hardened process runner verifies acceptance criteria)
                │
                ▼
       [Commit Finalization & Status Update] ─── (WAL commit; step passed / verified)
```

### Detailed Path Stages:

1. **Plan Generation (`orchestrator.py:1295-1303`)**: System 2 `plan_and_prd` produces structured plan output including `target_context`, `architecture_strategy`, `scope`, `non_scope`, `architectural_constraints`, `compatibility_contracts`, `behavior_invariants`, `migration_constraints`, `blocking_unknowns`, and `clarifying_questions`.
2. **Metadata Enrichment (`orchestrator.py:1368-1394`)**: Companion metadata dictionary retains all typed architectural fields alongside versioning, session ownership, and cryptographic hashes.
3. **Persistence (`core/plans.py:1001-1140`)**: `save_plan_document()` writes `<plan>.md` and `<plan>.meta.json` safely under a cross-process lock with Write-Ahead Logging (WAL) and Compare-And-Swap (CAS) revision protection.
4. **Reconciliation & Deserialization (`core/plans.py:1730-1825`)**: `PlanRegistry._load_plan_document()` reconciles sidecars and loads typed `PlanDocument` instances. Missing fields in legacy plans default safely to `None` or empty lists (`[]`).
5. **Contract Binding (`core/runtime/contract.py:157-185`)**: When approval is granted, `ApprovedExecutionContract` binds to the exact `plan_id`, `plan_revision`, and `plan_content_hash`. Any subsequent out-of-band edit to the plan invalidates authorization.
6. **Pre-Step Blocking Gate (`core/runtime/step_runner.py:1315-1335` & `orchestrator.py:1760-1775`)**: Before any mutation or code-generation step runs, `has_unresolved_blocking_unknowns(step_id, files)` checks if an unresolved decision affects the step.
7. **Bounded Context Injection (`orchestrator.py:1785-1800`)**: `get_step_architectural_context(step)` extracts only the constraints and invariants relevant to the approved step. Unrelated constraints (such as constraints for different files) are excluded.
8. **Enforcement & Verification (`orchestrator.py:1805-1875`)**: Scope gate ensures mutated files remain within approved targets; transaction staging executes atomic mutations; domain tests verify acceptance criteria.

---

## 3. Plan Schema & Fields

`PlanDocument` in `core/plans.py` is extended with typed, optional fields:

| Field | Type | Description | Default |
|---|---|---|---|
| `target_context` | `Optional[str]` | Architectural context classification: `greenfield`, `brownfield`, `mixed`, or `unknown`. | `None` |
| `architecture_strategy` | `Optional[str]` | Applied strategy: `keep`, `local_refactor`, `incremental_migration`, or `bounded_rewrite`. | `None` |
| `scope` | `List[str]` | Components, directories, or files explicitly inside the change boundary. | `[]` |
| `non_scope` | `List[str]` | Explicitly excluded files or boundaries that must not be modified. | `[]` |
| `architectural_constraints` | `List[Any]` | Invariants, caller preservation rules, or public API signatures (strings or dicts with `target_files`). | `[]` |
| `compatibility_contracts` | `List[str]` | Public interface, backward compatibility, or caller test requirements. | `[]` |
| `behavior_invariants` | `List[str]` | State, audit, idempotency, or transaction invariants that must hold. | `[]` |
| `migration_constraints` | `List[str]` | Dual-read windows, schema versioning, or rollback requirements. | `[]` |
| `blocking_unknowns` | `List[Any]` | Missing decisions required before dependent mutation steps can proceed. | `[]` |
| `clarifying_questions` | `List[str]` | Non-blocking material questions or future considerations. | `[]` |

All fields are fully serialized in `PlanDocument.to_metadata_dict()` and deserialized in `PlanRegistry._load_plan_document()`.

---

## 4. Attaching Constraints to Approved Steps

A central failure mode identified in Phase 3 is injecting the entirety of a large markdown plan or irrelevant constraints into every step, diluting prompt attention and leaking context.

BrainFrog enforces **step-relevance filtering** via `PlanDocument.get_step_architectural_context(step)`:

1. **Step Files Overlap**: If a structured constraint specifies `files` or `target_files`, it is included only if the step's targeted files intersect with the constraint's targets.
2. **Text Reference Disambiguation (`_is_constraint_relevant_to_step`)**: If a constraint is declared as text:
   - If it mentions a file targeted by the current step, it is included.
   - If it explicitly mentions files targeted by *other* steps in the plan and none of the current step's files, it is **excluded** as unrelated.
   - If it is a global rule (mentions no specific files), it is included.
3. **Prompt Boundary Block (`format_step_architecture_context`)**: Formatted as a concise, high-priority block prepended to `effective_task` before reaching System 2:

```text
[Architectural Constraints & Boundaries]
- Target Context: brownfield | Strategy: local_refactor
- Relevant Constraints:
  * Existing payment API in api/payment.py must retain its public signature.
- Behavior & State Invariants:
  * Persisted payment records cannot be dropped without an audit entry.
- Out of Scope (Do NOT Modify):
  * database/schema.sql
```

---

## 5. Safe Unknown Information Handling

Phase 2 established that proceeding with speculative implementations when critical requirements are unknown leads to hallucinated defaults. Phase 3 operationalizes the distinction between **blocking** and **non-blocking** unknowns:

### Blocking Unknowns
A missing decision that prevents safe, correct implementation or validation of an approved behavior (e.g. unknown external gateway SLA, missing token TTL, unconfirmed discount rate).
- Represented structuredly in `PlanDocument.blocking_unknowns`:
  ```json
  {
    "id": "blk_gw_timeout",
    "decision": "Gateway timeout threshold and retry backoff policy",
    "question": "What is the external gateway SLA and retry limit?",
    "affected_steps": ["2"],
    "affected_files": ["api/gateway.py"],
    "resolved": false
  }
  ```
- **Fail-Closed Execution Gate**:
  - `PlanDocument.has_unresolved_blocking_unknowns(step_id, files)` checks if an unresolved unknown affects the step.
  - If affected, `StructuredStepRunner` and `_execute_step` halt execution immediately before code writing or staging, raising `BlockingUnknownError`:
    ```
    BlockingUnknownError: Step '2' halted: unresolved blocking unknown decisions: Gateway timeout threshold and retry backoff policy. Clarification required before proceeding.
    ```
  - The step is marked failed/blocked on the progress tracker; subsequent steps are skipped.
  - **Unaffected Safe Steps**: If Step 1 touches only `api/auth.py` and is not listed in `affected_steps` or `affected_files`, Step 1 proceeds and completes normally.

### Non-Blocking Unknowns
Information that does not impede bounded, verifiable work (e.g., general assumptions, nice-to-have features, future aesthetic choices).
- Stored in `assumptions` or `clarifying_questions` (with `blocking=false` or not present in `blocking_unknowns`).
- Do **not** halt execution; documented in the handoff markdown for situational awareness.

### Decoupling `unknown` Classification from Blocking State
`target_context = "unknown"` designates that codebase structure or conventions are mixed or undocumented. This architectural context classification is decoupled from execution readiness: an unknown context does **not** trigger `BlockingUnknownError` unless explicit blocking unknowns are declared.

---

## 6. Backward Compatibility with Existing Plans

To ensure zero breaking changes across existing repositories:
1. All new fields on `PlanDocument` have default values (`None` or `[]`).
2. `PlanRegistry._load_plan_document()` uses `.get()` fallbacks with safe defaults.
3. Legacy plans lacking architecture fields load cleanly without raising `KeyError` or schema errors.
4. `has_unresolved_blocking_unknowns()` returns `(False, [])` on legacy plans.
5. `format_plan_handoff()` gracefully omits empty architecture sections, preserving existing formatting output for older plans.
6. Verified by integration test `test_05_old_plans_without_architecture_fields_still_load`.

---

## 7. Preservation of Execution Authorization & Security Model

Architectural guidance is **never an authorization mechanism**. The following boundaries are strictly preserved:

1. **Independent Execution Authority**: Architectural strategy (`bounded_rewrite`, `incremental_migration`) or context (`brownfield`, `greenfield`) never grants file-system, shell, Git, or network capabilities.
2. **Immutable Contract Targets**: An `ApprovedExecutionContract` specifies an immutable set of `approved_targets`. Adding files to a plan's `scope` does not widen `contract.approved_targets`. Attempts to write unauthorized files raise `PermissionError: Scope violation` or capability denial.
3. **Read-Only PLAN Mode**: `mode="plan"` remains read-only. Tool boundaries reject any mutative execution, git commits, or shell scripts.
4. **Stale Plan Invalidation**: If a plan's content is modified on disk after approval, Compare-And-Swap and content hash validation fail closed with `StaleExecutionAuthorizationError` / `PermissionError`.
5. **Reentrant Workspace Locking**: Step execution is wrapped in `WorkspaceLock` with durable transaction isolation and rollback.

---

## 8. Code Enforcement vs. Prompt Guidance

| Responsibility | Enforced by Code (Deterministic) | Guided by Prompt (System 2) |
|---|---|---|
| Read-only PLAN Mode | ✅ Command allowlist, mode capability gate | ❌ (Not relied upon) |
| Execution Authorization | ✅ `ApprovedExecutionContract`, digital signatures | ❌ (Not relied upon) |
| File Write Boundaries | ✅ Contract targets & step scope check | ⚠️ Step description context |
| Blocking Unknown Halts | ✅ `BlockingUnknownError` pre-step gate | ⚠️ Highlighted in PRD section |
| Stale Approval Invalidation | ✅ SHA-256 disk hash & CAS revision checks | ❌ (Not relied upon) |
| Step-Relevant Constraints | ✅ Deterministic file relevance filter | ⚠️ Injected constraint text block |
| Signature Compatibility | ⚠️ Hardened acceptance test runner | ⚠️ Prompt instruction block |
| Rollback & Transaction WAL | ✅ `TransactionCoordinator` atomic rollback | ❌ (Not relied upon) |

---

## 9. Verification & Integration Test Coverage

The implementation is verified by 17 unit and integration tests in `tests/test_architecture_plan_build_handoff.py`:

- `test_01_architecture_constraint_survives_plan_creation`: Constraints survive System 2 generation into metadata.
- `test_02_constraint_survives_serialization_and_reload`: Exact equality across disk write and `PlanRegistry` reload.
- `test_03_correct_constraint_appears_in_approved_build_step_context`: Prompt receives relevant constraint in BUILD step.
- `test_04_unrelated_step_does_not_receive_unnecessary_architecture_context`: Unrelated documentation step excludes payment constraints.
- `test_05_old_plans_without_architecture_fields_still_load`: 100% backward compatibility for legacy plans.
- `test_06_blocking_unknown_prevents_affected_mutation_steps`: Halts affected step fail-closed; unaffected step succeeds.
- `test_07_non_blocking_unknowns_do_not_halt_unrelated_authorized_work`: Assumptions do not halt work.
- `test_08_planned_rewrite_does_not_grant_additional_file_permissions`: Rewrite strategy cannot bypass target boundary.
- `test_09_plan_mode_remains_read_only`: Plan mode rejects mutative tool operations.
- `test_10_approved_execution_contract_scope_remains_unchanged`: Plan metadata cannot widen contract scope.
- `test_11_modified_plan_or_step_cannot_inherit_stale_authorization_after_resume`: Modified plan invalidates stale contract.
- `test_12_cancellation_and_failed_step_handling_remain_safe`: Cancellation skips subsequent steps cleanly.
- `test_scenario_greenfield_minimal_constraints`: Synthetic greenfield scenario without bloat.
- `test_scenario_brownfield_api_compat`: Brownfield public API refactor scenario.
- `test_scenario_bounded_rewrite_preserves_external_contracts`: Bounded rewrite scenario.
- `test_scenario_stateful_incremental_migration`: Database migration with rollback rules scenario.
- `test_scenario_unknown_business_rules_blocks_only_dependent_steps`: Targeted unknown blocking scenario.

---

## 10. Known Limitations & Phase 4 Recommendations

1. **Unstructured String Constraints**: While structured constraints with explicit `files` or `steps` are deterministic, unstructured string constraints rely on filename token matching against plan files. Phase 4 should encourage System 2 clients to output structured constraint objects `{"text": "...", "files": [...]}`.
2. **Interactive Clarification Loop**: Phase 3 surfaces blocking unknowns via `BlockingUnknownError` and CLI banners. In Phase 4, the CLI interactive session can provide a prompt to resolve blocking unknowns directly without re-entering full PLAN mode.
