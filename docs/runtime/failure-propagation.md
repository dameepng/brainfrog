# BrainFrog Runtime — Cancellation and Failure Propagation (P1.3G)

## 1. Overview and Core Principle

> **A subagent is a WORKER, not an authority.**
> **Failure and cancellation propagation is strictly a LIFECYCLE and COORDINATION layer.**
> **It answers: *"What lifecycle consequences follow from failure or cancellation?"***
> **It does NOT answer: *"What is this worker allowed to execute?"***

BrainFrog Phase P1.3G introduces deterministic lifecycle cascade and coordination reconciliation when Child Works fail or cancellation is initiated. It propagates blocked statuses across dependency DAGs, handles cascading cancellation down delegation trees, governs parent lifecycle outcomes, releases active concurrency slots, and guarantees crash-recovery idempotence via canonical `WorkStore` persistence and Optimistic Concurrency Control (OCC).

### Conceptual Distinction Across Scoped-Subagent Phases

| Phase | Core Question Answered | Authority / Execution Scope |
| :--- | :--- | :--- |
| **P1.3E** | *"Which Child Work is eligible to proceed next?"* | Coordinates scheduling eligibility (`CoordinationState`), manages concurrency slots. Zero execution authority. |
| **P1.3F** | *"What did the Child Works produce, and what bounded result should the Parent receive?"* | Observes canonical state (`WorkStatus`), composes bounded data, scrubs secrets. Grants zero capabilities. |
| **P1.3G** | *"What lifecycle consequences follow from failure or cancellation?"* | Reconciles dependent blocking, handles cancellation cascades, applies parent failure policy. Strictly non-authority. |

---

## 2. Architecture and Boundaries

Failure and cancellation propagation operates within BrainFrog's capability and execution boundaries:

```text
       Parent Work
            │
            ├─────────────────┬─────────────────┐
            ▼                 ▼                 ▼
         Child A           Child B           Child C
       (FAILED / CANCELLED)   │                 │
            │                 │                 │
            └────────────┬────┴─────────────────┘
                         ▼
             FailurePropagator (P1.3G)
      (answers: "What lifecycle consequences follow?")
                         │
        ┌────────────────┼────────────────┐
        ▼                ▼                ▼
   Child B: BLOCKED  Child C: BLOCKED  Parent Work Policy
   (if B/C depend on A)                (PROPAGATE_FAILURE /
                                        BEST_EFFORT / MANUAL)
                         │
                         ▼
              WorkStore OCC & Persistence
         (WorkStatus & CoordinationState updated)
                         │
                         ▼
             ResultAggregator (P1.3F)
      (answers: "What was produced from outcomes?")
                         │
                         ▼
             DelegationRuntime (P1.3E)
      (answers: "Who is eligible to proceed now?")
```

### Invariant: Non-Authority Boundary
`FailurePropagator` and its associated routines (`propagate_failure`, `cancel_parent`, `propagate_child_cancellation`, `reconcile_failure_and_cancellation`) are **strictly lifecycle controllers**:
- **Zero execution primitives**: Never invokes `orchestrator.py`, System 1/2, LLM providers, shell, subprocess, Git, or network.
- **Zero capability issuance**: Cannot grant, widen, attenuate, or modify `DelegationContract` capabilities.
- **Zero approval mutation**: Cannot create, approve, or consume `ApprovedExecutionContract` or approvals.
- **Zero transaction mutation**: Cannot initiate transactions, commit filesystems, or execute database operations.
- **Canonical execution engine**: `orchestrator.py` remains the SOLE execution engine in BrainFrog.

---

## 3. Strict State Domain Separation

BrainFrog maintains three distinct, orthogonal state domains:

### A. WorkStatus (Canonical Work Lifecycle)
Defined in `core/runtime/work.py`:
- `CREATED`, `PLANNING`, `APPROVAL_REQUIRED`, `EXECUTING`, `VERIFYING`, `DONE`, `FAILED`, `CANCELLED`

### B. SubagentStatus (Worker Identity Lifecycle)
Defined in `core/runtime/subagent.py`:
- `CREATED`, `READY`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED`

### C. CoordinationState (Delegation Eligibility Only)
Defined in `core/runtime/delegation_runtime.py`:
- `WAITING`: Waiting for upstream prerequisite completion or scheduling.
- `RUNNABLE`: All declared dependencies satisfied (`DONE`); eligible to claim.
- `CLAIMED`: Claimed by an active execution slot under `max_concurrency`.
- `BLOCKED`: Cannot proceed because one or more upstream prerequisites did not succeed.

> **CRITICAL INVARIANT**:
> P1.3G **NEVER** introduces `RUNNING`, `EXECUTING`, `SUCCESS`, or `AUTHORIZED` into `CoordinationState`.
> CoordinationState dictates eligibility, never execution authority.

---

## 4. Failure vs. Propagated Blocking

`FAILED` and `BLOCKED` are fundamentally distinct concepts and must never be conflated:

| Concept | Domain | Meaning | When Assigned |
| :--- | :--- | :--- | :--- |
| **`FAILED`** | `WorkStatus` / `SubagentStatus` | This Work actually executed and reached an unrecoverable failure outcome. | During execution verification, exception handling, or step failure. |
| **`BLOCKED`** | `CoordinationState` | This Work cannot become runnable because an upstream prerequisite did not succeed. | By P1.3G propagation when an upstream dependency fails, cancels, or is blocked. |

### Semantic Rules
1. **Never mark never-executed dependents as `FAILED`**: If Child A fails and Child B depends on A, Child B's `CoordinationState` transitions from `WAITING` to `BLOCKED`. Child B's canonical `WorkStatus` remains `CREATED` (or whatever non-terminal phase it was in).
2. **Predecessor satisfaction requirement**: A dependency is satisfied **ONLY** if its canonical `WorkStatus == WorkStatus.DONE`.
   - `FAILED` does **NOT** satisfy a dependency.
   - `CANCELLED` does **NOT** satisfy a dependency.
   - `BLOCKED` does **NOT** satisfy a dependency.
   - In-progress statuses (`EXECUTING`, `PLANNING`, etc.) do **NOT** satisfy a dependency.
3. **Multi-Parent Dependencies**: If D depends on B and C:
   - If B is `DONE` and C is `FAILED` $\rightarrow$ D becomes `BLOCKED`.
   - If B is `DONE` and C is `EXECUTING` $\rightarrow$ D remains `WAITING` (ineligible). Partial completion is never sufficient.

---

## 5. Dependency DAG Propagation

Propagation operates across the directed acyclic graph (DAG) of declared child work dependencies:

```text
        A (FAILED)
       / \
      B   C (both depend on A)
       \ /
        D (depends on B and C)
```

1. **Iterative BFS Traversal**: `find_downstream_dependents()` computes the topological closure of downstream dependents using iterative breadth-first search.
2. **Resource Bounds**: Traversal is strictly bounded:
   - `MAX_PROPAGATION_DEPTH = 20`: Maximum dependency depth. Exceeding depth raises `FailurePropagationLimitError`.
   - `MAX_PROPAGATION_CHILDREN = 50`: Maximum total child works in a delegation group.
   - `MAX_PROPAGATION_EDGES = 200`: Maximum dependency edges inspected.
   - Iterative traversal prevents Python recursion stack exhaustion.
3. **Branching & Diamond DAGs**: All downstream descendants in the closure are set to `CoordinationState.BLOCKED` in `DelegationGroup.coordination_states`.

---

## 6. Cancellation Model and Policies

### Cancellation Root vs. Descendants
Cancellation distinguishes:
- **Explicit Cancellation**: Requested directly on a Parent or specific Child Work.
- **Cancellation Propagation**: Cascading cancellation to dependent or active children according to explicit policy.
- **Canonical State**: Transitions to canonical `WorkStatus.CANCELLED` and `SubagentStatus.CANCELLED`.

### Cancellation Propagation Policies

| Policy | Behavior on Target Cancellation | Use Case |
| :--- | :--- | :--- |
| `CANCEL_ACTIVE_DESCENDANTS` *(Default)* | Cancels non-terminal children directly under the target; sets dependent downstream works to `BLOCKED`. | Clean abort of entire active delegated subtrees when parent or root task is cancelled. |
| `BLOCK_DEPENDENTS_ONLY` | Leaves active parallel peers running; only transitions downstream dependents to `BLOCKED`. | Partial cancellation where independent active parallel workers are permitted to finish. |

### Terminal Immutability Rule
Terminal states are strictly immutable:
- `DONE` remains `DONE`.
- `FAILED` remains `FAILED`.
- `CANCELLED` remains `CANCELLED`.
- No terminal state is ever rewritten. Cancellation skips already-terminal works without error.

### Active & Approval-Required Cancellation
- Works in `APPROVAL_REQUIRED`, `PLANNING`, or `CREATED` transition to `CANCELLED` using canonical lifecycle transition rules.
- **No Approvals Created or Consumed**: Cancellation never generates or consumes execution approvals.
- **No Execution Triggered**: Cancellation never starts execution.

---

## 7. Claim and Coordination Slot Cleanup

Under P1.3E, claimed child works consume concurrency slots towards `max_concurrency`.

When a child becomes terminal (`FAILED`, `CANCELLED`, or `DONE`) or transitions to `BLOCKED`:
- Its active execution slot is immediately released.
- `DelegationRuntime.get_active_claim_count()` only counts works where `CoordinationState == CLAIMED` **AND** canonical `WorkStatus in ACTIVE_WORK_STATUSES`.
- Historical `CLAIMED` entries for terminal works do not block subsequent runnable children.

---

## 8. Parent Failure Policies

When a Child Work fails, the parent's lifecycle behavior is governed by an explicit `ParentFailurePolicy`:

| Policy | Parent Work Lifecycle Behavior | Use Case |
| :--- | :--- | :--- |
| `PROPAGATE_FAILURE` | Parent Work transitions to `WorkStatus.FAILED` immediately. A `WorkFailure` record is attached to the parent. | Strict pipelines (e.g. `ALL_REQUIRED`) where any child failure invalidates the overall parent task. |
| `BEST_EFFORT` | Parent Work remains in its active status (`EXECUTING`). Downstream dependents of the failed child are `BLOCKED`, but non-dependent children continue. | Fan-out or search tasks where partial success is acceptable. |
| `MANUAL` *(Default)* | Parent Work status is not automatically altered by the propagation layer. The parent orchestrator decides next steps upon aggregation. | Standard delegation where parent evaluates aggregated results via P1.3F before deciding whether to retry or fail. |

---

## 9. Session and Incarnation Binding

To prevent stale background tasks, resumed sessions, or previous subagent instances from corrupting current work:
- Every propagation call requires:
  - `actor`: Requesting actor identity.
  - `session_id`: Active session ID.
  - `session_incarnation_id`: Active session incarnation counter.
- **Authority Check**: Validated against `SessionAuthority` or parent work metadata.
- **Stale Incarnations Fail Closed**: If a session incarnation has incremented (e.g. after a session reset or crash recovery), operations raise `FailurePropagationSessionStaleError` and make zero state mutations.

---

## 10. Optimistic Concurrency Control (OCC) and Idempotence

Failure and cancellation propagation can race with:
- Normal work completion (`DONE`).
- Concurrent child failure.
- Concurrent cancellation requests.
- Delegation scheduling cycles.

### OCC Protection
- All mutations update canonical `Work` and `DelegationGroup` instances via `WorkStore.save()` and `save_delegation_group()`.
- If an update races with a concurrent modification, `WorkStore` raises `StaleWorkRevisionError`.
- State is never blindly overwritten.

### Idempotence Guarantee
All propagation routines are fully idempotent:
- Calling `propagate_failure()` twice on the same failed child produces the exact same state without duplicate transitions or metadata corruption.
- Calling `cancel_parent()` twice on a cancelled parent returns the identical terminal state without error.

---

## 11. Crash and Restart Recovery

Propagation state is persisted canonically in `WorkStore` (`resume_metadata` and `Work` records). No secondary database exists.

Upon process restart or recovery:
```python
result = reconcile_failure_and_cancellation(
    parent_work=parent_work,
    delegation_group=delegation_group,
    child_works=child_works,
    parent_policy=ParentFailurePolicy.MANUAL,
    cancellation_policy=CancellationPropagationPolicy.CANCEL_ACTIVE_DESCENDANTS,
)
```
`reconcile_failure_and_cancellation` inspects the persisted graph:
1. Detects any terminal `FAILED` or `CANCELLED` children.
2. Identifies any unblocked downstream dependents that were interrupted prior to persistence.
3. Sets unblocked dependents to `BLOCKED`.
4. Synchronizes subagent worker statuses with canonical work statuses.
5. Produces a deterministic `PropagationResult`.

---

## 12. Integration with P1.3F Result Aggregation

P1.3G works synergistically with P1.3F:

1. **P1.3G Reconciles Lifecycles**:
   - Failed child $\rightarrow$ `FAILED`.
   - Downstream dependents $\rightarrow$ `BLOCKED`.
   - Concurrency slots released.
2. **P1.3F Aggregates Outcomes**:
   - `ResultAggregator.aggregate()` reads canonical statuses.
   - Failed child classified into `failed_children`.
   - Blocked (never-executed) children remain in `incomplete_children`.
   - `AggregateResult` correctly reflects the exact execution boundary without hallucinating partial success.

---

## 13. Resource Limits Summary

| Resource Parameter | Constant | Value | Purpose |
| :--- | :--- | :--- | :--- |
| Max DAG Traversal Depth | `MAX_PROPAGATION_DEPTH` | 20 | Prevents pathological DAG recursion. |
| Max Children in Group | `MAX_PROPAGATION_CHILDREN` | 50 | Bounds child array iterations. |
| Max Dependency Edges | `MAX_PROPAGATION_EDGES` | 200 | Bounds BFS queue expansion. |
| Max Reason / Message Length | `MAX_REASON_CHARS` | 1024 | Prevents unbounded error string growth. |
