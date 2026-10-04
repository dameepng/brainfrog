# Phase 15B: Work Domain Model & Lifecycle Architecture

## 1. Executive Summary

Phase 15B introduces `Work` (`core/runtime/work.py`), a first-class domain model and deterministic state machine representing a user's requested unit of work from initial intent through planning, approval, execution, verification, and completion.

**Core Invariant:** `Work` is a descriptive domain state model. It is **NOT** an execution engine. There remains exactly **ONE** execution engine across the entire repository: the canonical `orchestrator.py` located at the repository root.

---

## 2. Separation of Primitives

The architecture strictly decouples descriptive lifecycle tracking from execution authority:

| Primitive                       | Module                     | Role                                                                                                                                     | Authority Level                                               |
| ------------------------------- | -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------- |
| **`Work`**                      | `core/runtime/work.py`     | Tracks lifecycle state, intent, goals, step plans, and candidate scopes.                                                                 | **Descriptive Only** (zero execution authority).              |
| **`ApprovalRequest`**           | `core/runtime/approval.py` | Manages human-in-the-loop authorization, cryptographic nonces, channel binding, and two-man rule enforcement.                            | **Pending Authorization** (unconsumed permission).            |
| **`ApprovedExecutionContract`** | `core/runtime/contract.py` | Immutable snapshot minted upon atomic approval consumption, binding exact normalized targets, session incarnation, and model API access. | **Active Authorization Boundary** (enforced by orchestrator). |
| **`Orchestrator`**              | `orchestrator.py`          | The single canonical engine that performs model calls, diff staging, quality gating, and code writes.                                    | **Sole Execution Engine**.                                    |

---

## 3. Lifecycle Flow Diagram

```
USER INTENT
    ↓
  WORK (Created)
    ↓
PLANNING (Formulating plan & candidate scopes)
    ↓
APPROVAL (Human-in-the-loop review of exact capabilities)
    ↓
EXECUTION CONTRACT (Immutable single-use authorization snapshot)
    ↓
ORCHESTRATOR (Canonical engine executes bounded modifications)
    ↓
VERIFICATION (Automated test gates & verification)
    ↓
  DONE (Terminal completion)
```

> [!NOTE]
> This diagram describes **lifecycle progression**, not a secondary execution pipeline.
> `Work` does not invoke `Orchestrator`, and `Orchestrator` does not derive authority from `Work`.

---

## 4. State Machine Transition Table

The state machine rules in `core/runtime/work.py` are deterministic, closed, and reject all unspecified transitions:

| Current Status      | Allowed Next Statuses              | Terminal? | Notes                                          |
| ------------------- | ---------------------------------- | --------- | ---------------------------------------------- |
| `CREATED`           | `PLANNING`, `FAILED`               | No        | Planning cannot be skipped.                    |
| `PLANNING`          | `APPROVAL_REQUIRED`, `FAILED`      | No        | Direct execution from planning is prohibited.  |
| `APPROVAL_REQUIRED` | `EXECUTING`, `CANCELLED`, `FAILED` | No        | `CANCELLED` is only reachable from this state. |
| `EXECUTING`         | `VERIFYING`, `FAILED`              | No        | Cannot jump directly to `DONE`.                |
| `VERIFYING`         | `DONE`, `FAILED`                   | No        | `DONE` is only reachable from `VERIFYING`.     |
| `DONE`              | _(none)_                           | **Yes**   | Terminal. Cannot transition anywhere.          |
| `FAILED`            | _(none)_                           | **Yes**   | Terminal. Cannot transition anywhere.          |
| `CANCELLED`         | _(none)_                           | **Yes**   | Terminal. Cannot transition anywhere.          |

Attempting any disallowed transition (e.g., `CREATED -> EXECUTING`, `PLANNING -> DONE`, `EXECUTING -> DONE`, or transitioning from any terminal state) raises `InvalidWorkTransition`.

---

## 5. Security Boundaries & Fail-Closed Invariants

1. **Work != Approval != Execution Contract != Authorization:**
   - A `Work` object with `status = EXECUTING` confers **zero** authority.
   - Passing a `Work` object to `Orchestrator.run()` fails closed with type and permission rejections.
   - `ApprovedExecutionContract.from_approval_request()` accepts only genuine, unconsumed `ApprovalRequest` objects.
2. **Immutability & TOCTOU Prevention:**
   - `Work` instances are frozen dataclasses (`@dataclass(frozen=True)`). Fields cannot be mutated in place.
   - Calling `work.transition(...)` returns a new immutable `Work` record; stored records remain unaltered until explicitly updated via `store.save()`.
3. **Identifier Opacity:**
   - `Work.id` is automatically generated using cryptographically strong random hex (`work_<32 hex digits>`). User prompts, filenames, or timestamps cannot be used as authority identifiers.
4. **Secret Scrubbing:**
   - Both `Work.to_dict()` and `Work.from_dict()` apply `reject_secrets()` from `core.runtime.contract` to prevent token or credential leakage into work descriptions.
5. **Single Execution Engine:**
   - No `WorkOrchestrator`, `WorkExecutor`, or `AgentOrchestrator` exists. All execution flows exclusively through canonical `orchestrator.py`.
