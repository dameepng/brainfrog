# Phase 15H — Persistent Work Continuation, Resume & Remote Work Status

## 1. Overview & Architectural Principle

Phase 15H transitions BrainFrog from a stateless "turn-based agent session" into an observable, persistent, and resumable work runtime.

The core architectural invariant remains strictly preserved:

> **"Persist the work, not the authority."**
> `orchestrator.py` remains the SOLE execution engine.
> Capabilities and execution contracts are NEVER persisted or minted by the Work domain.

Execution authority requires explicit, unexpired, distinct approval from `ApprovalService` and is bounded by an ephemeral `ApprovedExecutionContract`.

---

## 2. Work Domain Model & Schema

The Work domain (`core/runtime/work.py`) manages units of goal-driven intent. In Phase 15H, `Work` is extended with structured persistence fields while maintaining 100% backward compatibility with Phase 15B schemas:

- `schema_version`: Monotonic schema version (defaults to 1).
- `work_id`: Immutable alias for `id`.
- `actor_id` & `channel`: Server-derived authenticated identity and originating client channel.
- `session_id` & `session_incarnation_id`: Session incarnation boundary tracking.
- `title`: Short human-readable summary.
- `started_at` & `completed_at`: Monotonic epoch timestamps.
- `current_step_id` & `plan_id`: Execution progress tracking.
- `approval_request_id`: Explicit link to canonical `ApprovalRequest`.
- `transaction_id`: Explicit link to transactional mutation journal (`Transaction`).
- `verification_result`: Structured post-execution verification outcome (`status`, `passed`, `failed`, `warnings`, `summary`).
- `failure`: Structured failure details (`code`, `stage`, `summary`, `retryable`).
- `cancellation_reason`: Explicit record of cancellation rationale.
- `resume_metadata`: Ephemeral continuation metadata (`resumed_at`, `resumed_by`, `resume_channel`).
- `revision`: Optimistic concurrency control (OCC) version counter (monotonically incrementing integer).

### Backward Compatibility
Legacy 9-key dictionaries from Phase 15B deserialize seamlessly. When serialized, if extended fields are unpopulated, `to_dict()` produces backward-compatible 9-key outputs. Deserialization strictly rejects unknown fields and credential patterns (`reject_secrets`).

---

## 3. Persistent WorkStore (`core/runtime/work_store.py`)

Work records are persisted deterministically on disk via `FileWorkStore`:

1. **Physical Storage Hierarchy**:
   - Active works: `.brainfrog/works/<work_id>.json`
   - Terminal works (`done`, `failed`, `cancelled`): `.brainfrog/works/terminal/<work_id>.json`
   - Corrupted records: `.brainfrog/works/corrupt/<filename>.<timestamp>`
2. **Atomic Writes**:
   - Records are written to unique PID/thread temporary files (`.tmp_<work_id>_<pid>_<uuid>`), flushed, fsynced to disk, and moved into place via atomic `os.replace`.
3. **Cross-Process File Locking**:
   - Updates utilize file locking (`fcntl` on POSIX, `msvcrt` on Windows) to prevent race conditions during updates or concurrent claims.
4. **Optimistic Concurrency Control (OCC)**:
   - Every modification increments `revision`. Callers may provide `expected_revision`; conflicts raise `StaleWorkRevisionError`, rejecting stale writers.
5. **Path Traversal & Quota Protection**:
   - `work_id` is strictly validated (`validate_work_id`) to alphanumeric/underscore/hyphen (3–64 chars). Any attempt at traversal (`../`) is rejected.
   - Bounded quotas: `MAX_WORK_BYTES` (256 KB limit per record), `MAX_ACTIVE_WORKS_PER_ACTOR` (20), `MAX_GLOBAL_WORKS` (500), and `DEFAULT_MAX_TERMINAL_RETENTION` (50 per actor).

---

## 4. Lifecycle State Machine & Boundaries

Transitions between states follow deterministic finite state rules (`ALLOWED_TRANSITIONS`):

```
       ┌───────────┐
       │  CREATED  │──────┐
       └─────┬─────┘      │
             │            │
             ▼            │
       ┌───────────┐      │
       │ PLANNING  │──┐   │
       └─────┬─────┘  │   │
             │        │   │
             ▼        │   │
  ┌──────────────────┐│   │
  │APPROVAL_REQUIRED ││   │
  └──────────┬───────┘│   │
       ┌─────┴─────┐  │   │
       ▼           ▼  │   │
┌─────────────┐┌──────────┼───┐
│  EXECUTING  ││CANCELLED │   │
└──────┬──────┘└──────────┼───┘
       │                  │
       ▼                  │
┌─────────────┐           │
│  VERIFYING  │           │
└──────┬──────┘           │
       ├──────────┐       │
       ▼          ▼       ▼
   ┌──────┐   ┌──────────────┐
   │ DONE │   │    FAILED    │
   └──────┘   └──────────────┘
```

- Terminal states (`DONE`, `FAILED`, `CANCELLED`) are immutable and physically relocated to `terminal/`.
- Cancellation is permitted in `CREATED`, `PLANNING`, `APPROVAL_REQUIRED`, and `EXECUTING` (at safe transaction boundaries).
- Calling `work.cancel(reason)` deterministically sets `status=CANCELLED`, records the reason, and completes the work.

---

## 5. Work Continuation & Resume Engine (`core/runtime/work_continuation.py`)

Resuming work (`resume_work`) adheres to strict security gates:

1. **Authorization Gate**:
   - Verified via `authorize_work_execution_continuation()`.
   - Actor ownership must match.
   - Channel audience must match.
   - If session context is present, session incarnation must match the authoritative current session. Stale incarnations (from `/reset` or `/new`) fail closed.
2. **Idempotent Claim**:
   - Atomically records `resume_metadata` under lock.
   - Rejects concurrent resume attempts.
3. **State Continuation Logic**:
   - `APPROVAL_REQUIRED`: Informs user of pending/approved request (`/approve <id>` or `/exec <id>`). Does not bypass approval.
   - `EXECUTING`: Inspects `work.transaction_id`. Fails closed if missing (`MISSING_TX`). Correlates with `TransactionRecoveryManager` to roll back or finalize interrupted mutations.
   - `VERIFYING`: Resumes and finalizes verification checks before advancing to `DONE`.
   - `FAILED`: Allows deterministic retry if `failure.retryable == True`.
   - `DONE` / `CANCELLED`: Rejected with terminal notices.

---

## 6. Cancellation Boundary (`cancel_work`)

Cancelling a work unit:
- In `APPROVAL_REQUIRED`: Automatically marks linked `ApprovalRequest` as `CANCELLED` in `ApprovalService` store.
- In `EXECUTING`: Inspects the active transaction. If an in-flight operation is actively executing, prevents premature termination to avoid corrupting filesystem state; waits for safe transaction boundary.
- Concurrency-safe: In concurrent cancellation races, catches stale revision errors and acknowledges if already terminal.

---

## 7. Startup Crash Reconciliation

When `BrainFrogRuntime` initializes (`_reconcile_recovered_work`):
1. Incomplete transactions are recovered deterministically by `TransactionRecoveryManager`.
2. Active works (`EXECUTING`, `VERIFYING`, `APPROVAL_REQUIRED`, `PLANNING`) are inspected.
3. If an executing work has an associated transaction that was rolled back or committed, the work status is updated accordingly (`VERIFYING` or `FAILED`), preserving complete audit trails across process restarts.

---

## 8. Remote Channels & Presentation Sanitization

### Presentation Views (`core/runtime/work_presentation.py`)
- `/works [filter]` (`active`, `failed`, `all`): Bounded summary list.
- `/work <id>`: Comprehensive detail view including status badge, timestamps, plan progress, approval link, transaction link, verification outcome, and failure summary.
- `/resume <id>`: Resume command.
- `/cancel <id>`: Polymorphic cancellation (handles both Work IDs and Approval IDs).

### Telegram UX
- Inline keyboard buttons generated with prefix `bfw:<action>:<work_id>`.
- Callback data strictly validated with regex `\Abfw:([rcs]):([a-zA-Z0-9_\-]{3,64})\Z`.
- Plaintext and code blocks properly sanitized for Telegram Markdown escaping.

### WhatsApp UX
- Natural language aliases (`works`, `work <id>`, `resume <id>`, `cancel <id>`) mapped deterministically to command handlers.

### Secret Scrubbing & Defense-in-Depth
- `reject_secrets()` strictly rejects any credential material in `Work` and `WorkFailure` constructors.
- `_clean()` strips control characters and runs `scrub_secrets()`.
- String fields are bounded with SHA-256 digest clamps (`_bounded()`) to prevent UI flooding.

---

## 9. Architectural & Security Properties

The implementation is verified against 10 property invariants (`tests/test_work_properties.py`):

| Property | Rule | Enforcement Mechanism |
|---|---|---|
| **PROPERTY 1** | No Work action can directly execute filesystem mutation. | Work and Continuation domains contain zero filesystem mutation routines; changes occur solely via `orchestrator.py` and `TransactionCoordinator`. |
| **PROPERTY 2** | No Work action can bypass ApprovalService. | Resumption of `APPROVAL_REQUIRED` work directs the caller to `ApprovalService`; does not transition to execution. |
| **PROPERTY 3** | No Work action can bypass ApprovedExecutionContract. | Work schemas contain no contract or capability authority; `to_dict()` and `from_dict()` forbid authority fields. |
| **PROPERTY 4** | No Work action can bypass Transaction. | Executing work lacking a transaction fails closed with `MISSING_TX`; recovery delegates exclusively to `TransactionRecoveryManager`. |
| **PROPERTY 5** | No Work action can create a second orchestrator path. | `orchestrator.py` remains the sole execution engine; work continuation does not define an alternate loop. |
| **PROPERTY 6** | Stale session incarnation cannot regain execution authority. | `authorize_work_execution_continuation` validates session incarnation against current session; invalidated incarnations fail closed. |
| **PROPERTY 7** | Concurrent resume cannot execute twice. | Atomic claims under cross-process file locks prevent duplicate execution or recovery. |
| **PROPERTY 8** | Stale Work revision cannot overwrite newer state. | OCC monotonic revision checks raise `StaleWorkRevisionError` on conflicting updates. |
| **PROPERTY 9** | Unauthorized actor cannot inspect another actor's Work. | `authorize_work_inspection` strictly confines visibility to the owning actor and channel; unauthorized calls fail closed with generic 404/access-denied. |
| **PROPERTY 10** | Work persistence cannot leak secrets. | `reject_secrets` rejects credential fields at initialization; `_clean` and `format_work_detail` scrub presentation output. |
