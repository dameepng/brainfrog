# BrainFrog Runtime — Parallel and Sequential Delegation Coordination

## 1. Overview and Core Principle

> **CoordinationState determines eligibility. WorkStatus determines work lifecycle. SubagentStatus determines worker lifecycle.**
> **The delegation runtime coordinates eligibility; it does not execute work.**

BrainFrog P1.3E introduces deterministic coordination semantics for Parent Works managing multiple Child Works. The coordination runtime enforces strict separation between **coordination eligibility** (which Child Work is eligible to proceed next) and **canonical execution** (governed exclusively by `orchestrator.py`):

```text
User Intent / Parent Work
    │
    ├────────────────────────────────────────┐
    ▼                                        ▼
Sequential Delegation Mode              Parallel Delegation Mode
(Linear DAG: A -> B -> C)               (Bounded Concurrency: A, B, C)
    │                                        │
    └───────────────────┬────────────────────┘
                        ▼
                DelegationRuntime
            (determines ELIGIBILITY)
                        │
                        ▼
                    Child Work
             (status: RUNNABLE -> CLAIMED)
                        │
                        ▼
               Canonical Plan Step
                        │
                        ▼
                 Approval Service
           (checks user authorization)
                        │
                        ▼
            ApprovedExecutionContract
          (strictly attenuated scope)
                        │
                        ▼
              Transaction Coordinator
            (atomic staging & rollback)
                        │
                        ▼
                 orchestrator.py
            (SOLE EXECUTION ENGINE)
```

Critical invariant:
> **A subagent is a worker, not an authority.**
> `orchestrator.py` remains the **sole execution engine**.

---

## 2. The Three Separate State Domains

The most critical architectural invariant of P1.3E is the strict separation between three distinct state machines. These state domains MUST NOT be merged.

### A. `CoordinationState` — Delegation Eligibility Only

Defined in [`core/runtime/delegation_runtime.py`](file:///c:/dame-project/tools/agentic_dev/core/runtime/delegation_runtime.py):

| Coordination State | Meaning |
| :--- | :--- |
| `WAITING` | Child exists but cannot currently be selected (dependency unmet or concurrency capacity full). |
| `RUNNABLE` | All coordination prerequisites are satisfied; eligible to be claimed by the canonical runtime. |
| `CLAIMED` | The coordination runtime has atomically reserved an execution slot for this child. |
| `BLOCKED` | Child cannot become runnable because a required prerequisite is permanently unsatisfiable (e.g. dependency failed/cancelled or delegation expired). |

State transition model:
```text
WAITING
   │
   ▼
RUNNABLE
   │
   │ atomic claim
   ▼
CLAIMED
```
And:
```text
WAITING ──→ BLOCKED
```

The coordinator **MUST NOT** define `RUNNING`, `COMPLETED`, `FAILED`, or `CANCELLED` within `CoordinationState`, as those belong to `WorkStatus` and `SubagentStatus`.

---

### B. `WorkStatus` — Canonical Work Lifecycle

Authoritative canonical lifecycle defined in [`core/runtime/work.py`](file:///c:/dame-project/tools/agentic_dev/core/runtime/work.py):

```text
CREATED ──→ PLANNING ──→ APPROVAL_REQUIRED ──→ EXECUTING ──→ VERIFYING ──→ DONE
                                                                    │
                                                            FAILED / CANCELLED
```

| WorkStatus | Meaning |
| :--- | :--- |
| `CREATED` | Work unit initialized. |
| `PLANNING` | Planning execution steps. |
| `APPROVAL_REQUIRED` | Awaiting explicit user approval. |
| `EXECUTING` | Actively performing transaction operations. |
| `VERIFYING` | Post-execution verification checks. |
| `DONE` | Successful terminal completion. |
| `FAILED` | Work failed during execution or verification. |
| `CANCELLED` | Work was cancelled. |

---

### C. `SubagentStatus` — Worker Lifecycle

Authoritative worker lifecycle defined in [`core/runtime/subagent.py`](file:///c:/dame-project/tools/agentic_dev/core/runtime/subagent.py):

```text
CREATED ──→ READY ──→ RUNNING ──→ COMPLETED
                       │
               FAILED / CANCELLED
```

| SubagentStatus | Meaning |
| :--- | :--- |
| `CREATED` | Worker initialized with role and purpose. |
| `READY` | Worker bound to parent work and delegation contract. |
| `RUNNING` | Worker actively participating in work execution. |
| `COMPLETED` | Worker finished successfully. |
| `FAILED` | Worker encountered terminal failure. |
| `CANCELLED` | Worker was cancelled. |

---

### Why the Three State Machines Are Separate

```text
CoordinationState ≠ WorkStatus ≠ SubagentStatus
```

- **Separation of Concerns**: Coordination answers *which work unit is eligible next*. WorkStatus answers *where the work is in its canonical lifecycle*. SubagentStatus answers *what the worker agent identity is doing*.
- **No Authority Conflation**: Being `RUNNABLE` or `CLAIMED` in coordination does NOT imply approval or execution. For instance:
  ```text
  CoordinationState = CLAIMED
  WorkStatus        = APPROVAL_REQUIRED
  SubagentStatus    = READY
  ```
  This indicates the child has won an execution slot in the coordinator, but it cannot execute until it passes user approval through `ApprovalService`.
- **Accurate Resource Counting**: A concurrency slot is considered active while a child is `CLAIMED` and its canonical `WorkStatus` is non-terminal. Once `WorkStatus.DONE` is reached, the child remains `CLAIMED` in coordination records, but its active claim is released, freeing capacity for subsequent waiting children.

---

## 3. Delegation Modes

The coordination model supports two deterministic modes via [`DelegationMode`](file:///c:/dame-project/tools/agentic_dev/core/runtime/delegation_runtime.py):

### A. Sequential Mode (`DelegationMode.SEQUENTIAL`)
- For ordered, sequential workflows (e.g. `Child A -> Child B -> Child C`).
- Auto-constructs explicit linear dependencies (`A -> B`, `B -> C`) if not provided.
- Enforces `max_concurrency = 1`. At most 1 active claim may exist at any time.
- Initial state:
  ```text
  A = RUNNABLE
  B = WAITING
  C = WAITING
  ```
- Claim A:
  ```text
  A = CLAIMED
  B = WAITING
  C = WAITING
  ```
- While A has not reached canonical `WorkStatus.DONE`, B and C remain `WAITING`.
- After A reaches `WorkStatus.DONE`, reconciliation promotes:
  ```text
  A = CLAIMED
  B = RUNNABLE
  C = WAITING
  ```
- Then B can be claimed.

### B. Parallel Mode (`DelegationMode.PARALLEL`)
- For concurrent, independent or fork-join child work units.
- Allows independent children to become `RUNNABLE` subject to `max_concurrency`.
- When an active child reaches canonical `WorkStatus.DONE`, a concurrency slot is freed, allowing the next waiting child in deterministic order to be promoted to `RUNNABLE`.

---

## 4. Delegation Group Model

A Parent Work coordinates children through an immutable [`DelegationGroup`](file:///c:/dame-project/tools/agentic_dev/core/runtime/delegation_runtime.py):

```text
DelegationGroup
  ├── delegation_group_id
  ├── parent_work_id
  ├── actor
  ├── session_id
  ├── session_incarnation_id
  ├── mode (SEQUENTIAL | PARALLEL)
  ├── child_work_ids (ordered tuple of child IDs)
  ├── dependencies (mapping: child_id -> tuple of predecessor child_ids)
  ├── max_concurrency (integer, 1 <= n <= 16)
  ├── coordination_states (mapping: child_id -> CoordinationState)
  ├── created_at
  └── updated_at
```

### Identity Binding Invariants
Every child in the delegation group must satisfy:
```text
child.parent_work_id == group.parent_work_id
child.actor == group.actor
child.session_id == group.session_id
child.session_incarnation_id == group.session_incarnation_id
```
Foreign children, parent mismatches, or cross-tenant children are strictly rejected with `DelegationBindingError`.

---

## 5. Dependency DAG and Topological Validation

Child dependencies are represented explicitly by stable child Work IDs:
```python
dependencies = {
    "child_c": ("child_a", "child_b"),  # C waits for both A and B
    "child_d": (),                       # D is independent
}
```

Validation via [`validate_dependency_dag`](file:///c:/dame-project/tools/agentic_dev/core/runtime/delegation_runtime.py) enforces:
1. **No Self-Dependencies**: Rejects `A -> A`.
2. **No Unknown Dependencies**: Predecessors must exist in `child_work_ids`.
3. **No Duplicate Edges**: Duplicate dependency edges are rejected.
4. **Strict Acyclicity**: Uses Kahn's topological sort algorithm; cycles (e.g. `A -> B -> C -> A`) raise `DelegationDependencyError`.
5. **Depth & Edge Limits**: Bounds longest path (`MAX_DEPENDENCY_DEPTH = 20`) and total edges (`MAX_DEPENDENCY_EDGES = 200`).

---

## 6. Dependency Failure Semantics

For P1.3E:
- Only canonical `WorkStatus.DONE` satisfies a predecessor dependency.
- Canonical `WorkStatus.FAILED` or `WorkStatus.CANCELLED` does NOT satisfy a dependency.
- If a dependency fails or is cancelled, dependent children transition:
  ```text
  WAITING ──→ BLOCKED
  ```
- **No recursive parent failure**: The coordinator does NOT automatically fail the Parent Work.
- **No recursive cancellation**: Downstream work units are not automatically cancelled in P1.3E (reserved for P1.3G).

---

## 7. Concurrency Limits & Capacity Invariants

To prevent resource exhaustion:
- `DEFAULT_MAX_CONCURRENCY = 4`
- `MAX_SUBAGENT_CONCURRENCY = 16`
- `MIN_CONCURRENCY = 1`

### Parallel Capacity Invariant
At all times:
```text
number_of_active_claims <= max_concurrency
```

### Sequential Capacity Invariant
At all times:
```text
number_of_active_claims <= 1
```

A claim is active iff `coordination_state == CLAIMED` and `not child_work.is_terminal`.

---

## 8. Claim Semantics & Idempotent Dispatch

The transition from `RUNNABLE -> CLAIMED` represents an atomic reservation:
- **Atomic Claim**: Two concurrent dispatchers cannot double-claim the same child.
- **Idempotence**: Repeated calls to `claim_next()` or `dispatch()` are safe and produce no duplicate execution slots.
- **Deterministic Ordering**: When capacity is available, waiting children are promoted to `RUNNABLE` in deterministic FIFO order (by input declaration order / stable ID).

---

## 9. Persistence, OCC, and Restart Recovery

Delegation coordination state is persisted within the parent Work record's metadata (`parent_work.resume_metadata["delegation_groups"]`):
- **WorkStore OCC**: [`save_delegation_group`](file:///c:/dame-project/tools/agentic_dev/core/runtime/delegation_runtime.py) checks `expected_revision`. Stale revisions raise `StaleWorkRevisionError`.
- **Restart Reconstruction**: Rehydrates exact `DelegationGroup` from `WorkStore`. Deserialization does NOT trigger execution; recovery restores state only.
- **Secret Scrubbing**: All group metadata is scrubbed for credentials and tokens.

---

## 10. Session Incarnation Freshness

The delegation group is permanently bound to `session_incarnation_id`:
- Across `/reset`, `/new`, or channel re-incarnations, any coordination mutation referencing a stale incarnation fails closed with `DelegationSessionStaleError`.
- Stale coordination state cannot regain execution eligibility.

---

## 11. Delegation Expiration Enforcement

Before promoting any child from `WAITING` to `RUNNABLE`:
```text
now >= delegation.expires_at => child transitions to BLOCKED
```
Expired delegations fail closed. The coordinator never automatically renews, extends, or replaces delegations.

---

## 12. Strict Execution Boundary

The coordinator layer contains:
- **NO** `subprocess` or `os.system` execution
- **NO** filesystem mutation
- **NO** network/HTTP requests
- **NO** Git operations
- **NO** direct `TransactionCoordinator` execution
- **NO** direct `orchestrator.py` invocation
- **NO** LLM / System 1 / System 2 invocation

The coordinator only returns deterministic coordination decisions (`DispatchDecision`). Execution proceeds solely through the canonical runtime pipeline:
```text
Child Work -> Plan -> Approval -> ApprovedExecutionContract -> Transaction -> orchestrator.py
```

---

## 13. Explicit Non-Goals (Reserved for Future Phases)

The following capabilities are intentionally NOT implemented in P1.3E:
1. **Result aggregation across subagents** (reserved for P1.3F)
2. **Cross-subagent output merging or summarization** (P1.3F)
3. **Advanced failure cascading and automatic parent failure policies** (P1.3G)
4. **Recursive descendant cancellation propagation** (P1.3G)
5. **Autonomous worker pools or background thread scheduling**
6. **Dynamic capability negotiation or delegation extension**
7. **Distributed multi-node scheduling**
