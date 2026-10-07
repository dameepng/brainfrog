# BrainFrog — P1.3I Real Subagent Execution Integration

## 1. Why P1.3I Exists

Phases P1.3A through P1.3G established the theoretical and domain primitives for scoped subagents:
- **P1.3A/B**: `Subagent`, `DelegationContract`, and capability attenuation (`Capabilities`).
- **P1.3C**: `ChildWork` bound to canonical `Work` with parent identity correspondence.
- **P1.3D/E**: `DelegationRuntime` with sequential and parallel scheduling DAGs.
- **P1.3F**: `ResultAggregator` and deterministic `ChildResult` composition.
- **P1.3G**: `propagate_failure` and `propagate_child_cancellation`.

Before P1.3I, these primitives were declarative data structures and eligibility evaluators without direct connection to BrainFrog's real execution engine.

**P1.3I bridges these scoped runtime primitives to the real execution pipeline (`orchestrator.py`)** while strictly maintaining the core architectural invariant:
```
    Subagent = worker
    Parent = coordinator
    orchestrator.py = SOLE execution engine
```
A subagent must **never** become an independent execution authority.

---

## 2. Canonical Execution Architecture

The complete end-to-end execution flow connects the declarative delegation hierarchy to canonical transaction and orchestration layers:

```
    Parent Work
        |
        v
    DelegationContract
        |
        v
    ChildWork
        |
        v
    DelegationRuntime
        |
        v
    Approval / Execution Contract
        |
        v
    Transaction
        |
        v
    orchestrator.py
        |
        v
    Verification
        |
        v
    ChildResult
        |
        v
    ResultAggregator
        |
        v
    Parent Work
```

---

## 3. Core Component Roles and Boundaries

### 3.1 Parent Work -> ChildWork
The parent `Work` represents the high-level intent, user session, and initial authority boundary. When delegating a subtask:
1. The parent creates an attenuated `DelegationContract` defining the strict subset of authority delegated to the worker.
2. A `ChildWork` record is created wrapping a canonical `Work` instance whose capabilities, target scope, actor, session ID, and session incarnation ID match the contract.
3. The child is registered in a `DelegationGroup` for deterministic dependency coordination.

### 3.2 DelegationContract vs ApprovedExecutionContract
These two contracts serve fundamentally distinct, complementary functions and must never be conflated:
- **`DelegationContract`** (P1.3B):
  - *What authority may be delegated to this child worker.*
  - Declarative scope specification that attenuates parent capabilities monotonically.
  - Does **not** grant execution approval.
- **`ApprovedExecutionContract`** (P0.3):
  - *What exact operation is authorized to execute right now.*
  - Cryptographically signed operational boundary containing target paths, operation digest, actor, session, and nonce.
  - Required before filesystem mutations may occur.

A valid `DelegationContract` does **not** bypass approval: the child must still acquire or possess an `ApprovedExecutionContract` whose target scope and capabilities are a strict subset of the `DelegationContract`.

### 3.3 Transaction Layer (`TransactionStore`, `TransactionCoordinator`)
The subagent execution integration layer (`SubagentExecutionCoordinator`) contains zero filesystem mutation logic. All mutations requested during execution flow through canonical transaction semantics:
1. Operations are recorded in an isolated transaction workspace.
2. Changes are verified against the declared contracts.
3. Only upon successful verification are transactions committed atomically to the canonical workspace.
4. On failure or cancellation, transactions are cleanly rolled back.

### 3.4 Orchestrator Engine (`orchestrator.py`)
`orchestrator.py` remains the **sole execution engine** in the system:
- Absolutely no second execution engine (`SubagentExecutor`, `ChildExecutor`, `DelegatedOrchestrator`, etc.) is introduced.
- `SubagentExecutionCoordinator` acts purely as an adapter/coordinator: it validates bindings, checks eligibility via `DelegationRuntime`, constructs the canonical `RunConfig`, and invokes `orchestrator.py`.

---

## 4. Lifecycle Synchronization

Execution rigorously respects canonical lifecycle state machines without out-of-band mutations:

### 4.1 Child Work Lifecycle
Child `Work` proceeds through monotonic state transitions enforced by `ALLOWED_TRANSITIONS` and optimistic concurrency control (OCC):
```
    CREATED -> PLANNING -> APPROVAL_REQUIRED -> EXECUTING -> VERIFYING -> DONE
```
- **Unapproved Execution**: If no valid `ApprovedExecutionContract` exists, the child transitions `CREATED -> PLANNING -> APPROVAL_REQUIRED` and pauses until human or policy approval is granted.
- **Failure**: Any runtime failure transitions the child to `FAILED`.
- **Cancellation**: Any pre-execution or runtime cancellation transitions the child to `CANCELLED`.
- **Terminal Invariance**: Terminal states (`DONE`, `FAILED`, `CANCELLED`) are immutable and idempotent. Re-executing an already completed child immediately returns the existing result without invoking the orchestrator.

### 4.2 Subagent Worker Lifecycle
The worker `Subagent` lifecycle is synchronized in lockstep with child work progress:
```
    CREATED -> READY -> RUNNING -> COMPLETED
                             \
                              +-> FAILED
                             \
                              +-> CANCELLED
```

---

## 5. Scheduling and Concurrency

Scheduling and claim semantics are managed by `DelegationRuntime` (P1.3D/E):
- **Eligibility**: A child is eligible (`RUNNABLE`) only when all its DAG dependencies are `WorkStatus.DONE` and its delegation contract is unexpired.
- **Atomic Claims**: Before execution begins, the coordinator claims a concurrency slot (`claim_child` or `claim_next`), moving the coordination state to `CLAIMED`.
- **Sequential Execution**: In sequential mode (`max_concurrency=1`), strict linear dependency ordering (`A -> B -> C`) is enforced.
- **Parallel Execution**: In parallel mode, independent children can execute concurrently up to the declared `max_concurrency`.
- **Race Protection**: Concurrent execution attempts from competing workers result in exactly one winner; losing attempts fail closed with deterministic non-execution exceptions.

---

## 6. Approval Integration

Delegation never bypasses human or automated approval gates:
1. If the execution request provides an `ApprovedExecutionContract`, the coordinator verifies:
   - Contract actor, session ID, and session incarnation match the child work.
   - Contract approved targets are a strict subset of the `DelegationContract` target scope.
   - Contract capabilities do not exceed attenuated delegation capabilities.
   - Contract has not expired.
2. If no contract is provided and the child action requires approval, the coordinator transitions the child work to `WorkStatus.APPROVAL_REQUIRED`, registers the pending approval request in the store, and halts execution cleanly without executing.

---

## 7. Result Flow and Aggregation

Upon child completion:
1. `ChildWork` reaches terminal status `DONE` or `FAILED`.
2. `SubagentExecutionCoordinator` produces a bounded `SubagentExecutionResult`.
3. In `execute_group`, child results feed directly into `ResultAggregator.aggregate(...)` (P1.3F).
4. An `AggregateResult` is computed, determining overall group status (`COMPLETE`, `PARTIAL`, `FAILED`, `INCOMPLETE`).
5. `ResultAggregator.attach_to_parent(work_store, parent_work_id, aggregate_result)` attaches the bounded aggregate summary to the parent `Work.resume_metadata` via OCC.
6. The parent work becomes eligible for resumption/continuation.

---

## 8. Failure and Cancellation Cascades

Failure and cancellation integrate directly with P1.3G:
- **Child Failure**: When child `A` fails, `propagate_failure` evaluates the group DAG. Downstream dependents (`B`, `C`) have their coordination state set to `CoordinationState.BLOCKED`. Crucially, their canonical `WorkStatus` is **NOT** changed to `FAILED` (`BLOCKED != FAILED`), remaining in their non-terminal pre-execution status.
- **Child Cancellation**: When child `A` is cancelled, `propagate_child_cancellation` cascades cancellation down the dependency tree. Dependent children become `CoordinationState.BLOCKED`, retaining non-terminal work status without becoming failed or cancelled.
- **Execution Invariance**: Blocked dependents cannot execute and raise `SubagentExecutionBlockedError`.
- **Parent Policy**: The parent failure policy (`ParentFailurePolicy.PROPAGATE_FAILURE` vs `BEST_EFFORT`) determines whether the parent work fails immediately or remains active to inspect partial outcomes.

---

## 9. Context Boundary and Isolation

To prevent prompt leakage, privilege escalation, and memory bloat, context is strictly bounded:
- **No History Leakage**: Parent conversation history, raw provider tokens, unrelated plans, and secret credentials are never copied into the child context.
- **Explicit Bounded Context**: `SubagentExecutionContext` contains only:
  - Explicit task string (bounded to 4,096 chars).
  - Explicit parent summary (bounded to 1,024 chars).
  - Explicit constraint list (at most 32 items of 256 chars each).
  - Explicit artifact references (at most 32 references).
- All fields undergo strict secret scrubbing (`reject_secrets` and `scrub_secrets`).

---

## 10. Remote Work Integration

Remote work channels (Telegram, WhatsApp, Webhook) interact with subagents through the standard pipeline:
```
    Remote Message -> RemoteWorkCoordinator -> Parent Work -> DelegationGroup -> Child Work -> SubagentExecutionCoordinator -> orchestrator.py
```
- Transport concerns remain strictly isolated from execution concerns.
- Child subagents cannot communicate directly with external messaging transports.
- Aggregated child results are surfaced back to the parent work, which composes the final remote response.

---

## 11. Crash Recovery

If the process crashes during child execution:
1. `FileWorkStore` persists all intermediate state transitions atomically with disk fsync and OCC revisions.
2. On process restart, `FileWorkStore` reconstructs the canonical state from `.brainfrog/works/`.
3. A restarted `SubagentExecutionCoordinator` inspects the persisted state:
   - Already completed children (`DONE`) are recognized as terminal and return idempotent results without re-executing.
   - Interrupted non-terminal children can be reconciled or recovered through canonical work continuation.
4. No secondary recovery database or external shadow state exists.

---

## 12. Non-Authority Guarantees

The integration layer enforces strict architectural guarantees:
1. **Zero Direct Mutations**: `SubagentExecutionCoordinator` never calls `open(..., "w")`, `os.replace`, `shutil`, `Path.write_text`, or shell commands.
2. **Zero Process Spawning**: No calls to `subprocess.run`, `subprocess.Popen`, `os.system`, or shell execution exist in the subagent layer.
3. **Zero Autonomous Authority Grants**: The coordinator cannot mint capabilities or auto-approve contracts.
4. **Zero Alternate Orchestrators**: Execution terminates solely in canonical `orchestrator.py`.
