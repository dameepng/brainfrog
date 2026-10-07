# BrainFrog Runtime — Child Work Lifecycle

## 1. Overview and Core Principle

> **A subagent is a worker, not an authority.**
> **A Child Work is a lifecycle/work representation, not an authority or execution engine.**

In BrainFrog, all authority and side-effecting operations flow through a strict, single execution pipeline:

```text
User Intent / Parent Work
    │
    ▼
DelegationContract (attenuated capabilities & scope)
    │
    ▼
Subagent (worker identity)
    │
    ▼
Child Work (governed work lifecycle)
    │
    ├── Plan
    ├── Approval Service
    ├── ApprovedExecutionContract
    ├── Transaction Coordinator (WAL / Rollback)
    └── orchestrator.py (SOLE canonical execution engine)
```

There is **no second orchestrator**, no second approval system, no second transaction engine, and no direct execution bypass. `ChildWork` only tracks work state, identity bindings, and lifecycle transitions.

---

## 2. Parent → Delegation → Subagent → Child Work Relationship

The hierarchy adheres to strict immutable identity correspondence:

```text
Parent Work (work_id, actor, session_id, session_incarnation_id)
      │
      ├── DelegationContract (delegation_id, parent_work_id, child_subagent_id, capabilities, scope, expires_at)
      │
      ▼
   Subagent (subagent_id, role, parent_work_id, actor, session_id, session_incarnation_id)
      │
      ▼
   ChildWork (child_work_id, parent_work_id, subagent_id, delegation_id, work)
```

The parent Work has an observational relationship with its children (`parent_work.child_work_ids`). The parent **does not** gain authority to arbitrarily execute, mutate, or bypass verification for the child Work.

---

## 3. Mandatory Identity Bindings

Every `ChildWork` is permanently bound to:
- `parent_work_id`
- `subagent_id`
- `delegation_id`
- `actor`
- `session_id`
- `session_incarnation_id`

### Invariant Checks
At creation and during all authority-sensitive operations, the following must hold:
```text
child.parent_work_id == delegation.parent_work_id == parent_work.id
child.subagent_id == delegation.child_subagent_id == subagent.subagent_id
child.delegation_id == delegation.delegation_id
child.actor == delegation.actor == parent_work.actor
child.session_id == delegation.session_id == parent_work.session_id
child.session_incarnation_id == delegation.session_incarnation_id == parent_work.session_incarnation_id
```
Any mismatch triggers `ChildWorkBindingError` and fails closed. Mismatches are never silently repaired.

---

## 4. Child Work Lifecycle

`ChildWork` reuses the canonical `WorkStatus` state machine defined in `core/runtime/work.py`:

```text
     CREATED
        │
        ▼
     PLANNING
        │
        ▼
APPROVAL_REQUIRED
        │
        ▼
    EXECUTING
        │
        ▼
    VERIFYING
        │
        ▼
      DONE
```

### Failure Transitions
Any non-terminal state may transition to `FAILED` with a deterministic `WorkFailure`:
- `CREATED` → `FAILED`
- `PLANNING` → `FAILED`
- `APPROVAL_REQUIRED` → `FAILED`
- `EXECUTING` → `FAILED`
- `VERIFYING` → `FAILED`

### Cancellation Transitions
Early non-executing states may transition to `CANCELLED`:
- `CREATED` → `CANCELLED`
- `PLANNING` → `CANCELLED`
- `APPROVAL_REQUIRED` → `CANCELLED`

Terminal states (`DONE`, `FAILED`, `CANCELLED`) are completely immutable and cannot undergo further transitions.

---

## 5. Subagent Lifecycle Synchronization

The worker `Subagent` lifecycle tracks the `ChildWork` lifecycle monotonically:
- Child Work created → Subagent `READY`
- Child Work `PLANNING` or `EXECUTING` → Subagent `RUNNING`
- Child Work `DONE` → Subagent `COMPLETED`
- Child Work `FAILED` → Subagent `FAILED`
- Child Work `CANCELLED` → Subagent `CANCELLED`

The `Subagent` remains a worker identity and holds no execution authority of its own.

---

## 6. Delegation Scope and Attenuation Snapshot

A child Work's effective capabilities and scope are initialized exclusively from the `DelegationContract`:
- `child.capabilities` = `delegation.capabilities` (already attenuated via P1.3C algebra)
- `child.target_scope` = `delegation.target_scope`

The child never inherits or infers capabilities directly from the parent Work. Furthermore, later changes to parent capabilities do not expand the child Work's scope. Scope widening during transition or resume is explicitly rejected.

---

## 7. Delegation Expiration Enforcement

Every authority-sensitive lifecycle operation verifies that the authorizing `DelegationContract` has not expired:
```text
now >= delegation.expires_at => ChildWorkExpiredError
```
- A child Work cannot outlive its delegation.
- A child cannot automatically extend, refresh, or renew its delegation.
- Stale or expired delegations fail closed immediately.

---

## 8. Session Incarnation Protection

BrainFrog enforces session incarnation binding to neutralize stale execution across `/reset`, `/new`, or channel re-incarnations:
- If `current_session_incarnation_id != child.session_incarnation_id`, all mutating, resuming, or executing operations raise `ChildWorkSessionStaleError`.
- Stale child works can never regain execution authority after a session reset.

---

## 9. Persistence and Recovery

Child works are stored directly in the canonical `WorkStore` (`core/runtime/work_store.py`).
- Child metadata (parent ID, subagent ID, delegation ID, session incarnation, subagent status) is stored in the underlying Work's `resume_metadata["child_work"]`.
- The storage system uses optimistic concurrency control (OCC) via `revision`. Stale revisions trigger `StaleWorkRevisionError`.
- During crash recovery, incomplete child works follow the standard `TransactionRecoveryManager` and `WorkContinuation` protocols without separate, unverified recovery paths.
- Secrets (API keys, passwords, tokens, private keys) are scrubbed before persistence and rejected if detected.

---

## 10. Execution Boundary Guarantee

`ChildWork` does NOT contain:
- Subprocess or shell execution
- Direct filesystem mutation
- Network/HTTP requests
- Git commands
- Direct `TransactionCoordinator` invocation
- Direct `orchestrator.py` invocation

Actual execution proceeds strictly through:
```text
ChildWork -> Approval Flow -> ApprovedExecutionContract -> Transaction -> orchestrator.py
```

---

## 11. Explicit Non-Goals (Reserved for Future Phases)

The following capabilities are intentionally NOT implemented in P1.3D:
1. **Parallel subagent execution** (P1.3E/F)
2. **Result aggregation across multiple subagents** (P1.3G)
3. **Multi-subagent schedulers and worker pools** (P1.3G)
4. **Automatic parent failure cascading** (P1.3G policy engine)
5. **Dynamic capability renegotiation or delegation extension**
6. **Autonomous background tasks outside the established orchestrator pipeline**
