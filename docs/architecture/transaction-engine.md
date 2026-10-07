# BrainFrog Architecture: Transactional Execution Engine & Rollback (Phase 15D)

## 1. Executive Summary

Phase 15D introduces the **Transactional Execution Engine** to BrainFrog. It provides atomic, multi-step filesystem execution with pre-mutation snapshotting, verification before commit, reverse-order (LIFO) rollback on failure, and crash recovery.

### Core Architectural Invariant

**`orchestrator.py` remains the SOLE execution engine in BrainFrog.**

The Transaction engine is **NOT** a second orchestrator, an autonomous agent executor, or a parallel execution engine. It does **NOT** grant permissions or bypass contracts. Instead, it is an execution lifecycle and journaling primitive utilized _by_ the canonical `orchestrator.py` to ensure filesystem mutations succeed completely or roll back cleanly without leaving partial corrupted states on disk.

---

## 2. Domain Model Hierarchy & Separation of Concerns

BrainFrog strictly separates intent, planning, authorization, and operational execution across dedicated domain primitives:

```
┌────────────────────────────────────────────────────────┐
│                      User Intent                       │
└───────────────────────────┬────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────┐
│             System 1 / Intent Firewall                 │
└───────────────────────────┬────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────┐
│                      Work Model                        │
│          Descriptive Lifecycle (Phase 15B)             │
└───────────────────────────┬────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────┐
│                      Plan Model                        │
│        Structural Step Decomposition (Phase 15C)       │
└───────────────────────────┬────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────┐
│             ApprovedExecutionContract                  │
│       Strict Authority & Capabilities (Phase 15A)      │
└───────────────────────────┬────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────┐
│               Canonical Orchestrator                   │
│             SOLE Execution Engine                      │
└───────────────────────────┬────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────┐
│                   Transaction Engine                   │
│       Atomic Filesystem Mutation Journal & Rollback    │
└────────────────────────────────────────────────────────┘
```

| Layer            | Type             | Responsibility                                             | Can Mint Authority?         |
| :--------------- | :--------------- | :--------------------------------------------------------- | :-------------------------- |
| **Work**         | Descriptive      | Tracks overall user request from intent to completion      | ❌ No                       |
| **Plan**         | Descriptive      | Defines structural steps, goals, and dependencies          | ❌ No                       |
| **Contract**     | Authoritative    | Binds cryptographic approval, actor, and capability limits | ✅ Yes                      |
| **Orchestrator** | Execution Engine | Canonical workflow executor                                | ❌ Operates within contract |
| **Transaction**  | Operational      | Guarantees atomic filesystem mutations and rollback        | ❌ No                       |

---

## 3. The 8-State Transaction Lifecycle State Machine

A Transaction moves deterministically through 8 discrete lifecycle states:

```
               ┌───────────┐
               │  CREATED  │
               └─────┬─────┘
                     │ begin()
                     ▼
               ┌───────────┐
               │  STAGING  ├───────────────────┐
               └─────┬─────┘                   │
                     │ execute()               │
                     ▼                         │
               ┌───────────┐                   │
               │ EXECUTING ├─────┐             │
               └─────┬─────┘     │             │
                     │ verify()  │ failure     │
                     ▼           │             │
               ┌───────────┐     │             │ failure
               │ VERIFYING ├─────┤             │
               └─────┬─────┘     │             │
                     │           ▼             │
            success  │     ┌──────────────┐    │
                     │     │ ROLLING_BACK │    │
                     │     └──────┬───────┘    │
                     │            │            │
                     ▼            │            │
              (Terminals)         │            │
             ┌───────────┐        ▼            │
             │ COMMITTED │  ┌─────────────┐    │
             └───────────┘  │ ROLLED_BACK │    │
                            └─────────────┘    ▼
                                        ┌────────┐
                                        │ FAILED │
                                        └────────┘
```

### State Definitions

1. `CREATED`: Initial instantiation; no disk mutations or staged operations have occurred.
2. `STAGING`: Operations are registered, pre-mutation snapshots are captured, and validations occur.
3. `EXECUTING`: Operations are executed sequentially onto the filesystem.
4. `VERIFYING`: Post-execution state is inspected by the `TransactionVerifier` before final commit.
5. `COMMITTED`: Terminal success state; mutations are final and verified.
6. `ROLLING_BACK`: Active rollback state; executing mutations are being reverted in reverse (LIFO) order.
7. `ROLLED_BACK`: Terminal rolled-back state; all executed operations reverted cleanly to pre-mutation states.
8. `FAILED`: Terminal failure state; reached when an operation failed during staging or when rollback itself encountered an error.

### State Invariants

- **Terminal Immutability**: `COMMITTED`, `ROLLED_BACK`, and `FAILED` are strictly terminal. Any subsequent transition raises `InvalidTransactionTransition`.
- **No Skip Transitions**: A transaction cannot bypass verification or staging to jump directly from `CREATED` to `COMMITTED`.
- **Error Preservation**: If rollback fails, the transaction transitions to `FAILED` and preserves _both_ `failure_reason` (the error triggering rollback) and `rollback_reason` (the error preventing clean rollback).

---

## 4. Operation Journaling & Pre-Mutation Snapshots

The transaction engine supports four explicit filesystem mutation types:

- `CREATE_FILE`: Creates a new file at `target`. Pre-mutation snapshot records `{"exists": False}`.
- `MODIFY_FILE`: Updates an existing file at `target`. Pre-mutation snapshot records the original file content.
- `DELETE_FILE`: Deletes a file at `target`. Pre-mutation snapshot records the original file content.
- `RENAME_FILE`: Renames/moves a file from `target` to `new_target`. Pre-mutation snapshot records original existence.

Each `TransactionOperation` records:

- `before_state`: Snapshot captured before any mutation touches disk.
- `after_state`: Intended payload staged for execution.
- `rollback_state`: Explicit instructions for reversing the operation.
- `status`: `STAGED` → `EXECUTED` → `ROLLED_BACK` (or `FAILED`).

---

## 5. Verification Before Commit

Transactions do not immediately commit after executing filesystem writes. They enter the `VERIFYING` state where a `TransactionVerifier` validates the workspace:

```python
class BasicFilesystemVerifier:
    def verify(self, tx: Transaction, workspace: Path) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        ...
```

The verifier confirms:

1. All created and modified files exist and match their expected staged content.
2. All deleted files no longer exist on disk.
3. All renamed destination files exist and source files are absent.
4. No operation targets escape the workspace boundary.

If verification fails, the transaction immediately transitions to `ROLLING_BACK` and reverts all executed operations.

---

## 6. Reverse-Order (LIFO) Rollback Semantics

When an operation fails during execution or fails verification:

1. The transaction transitions to `ROLLING_BACK`.
2. The coordinator retrieves all operations with status `EXECUTED`.
3. Operations are rolled back in **strict reverse order (LIFO)**:
   - Created files are unlinked.
   - Modified files are restored with their exact `before_state` content.
   - Deleted files are recreated with their exact `before_state` content.
   - Renamed files are moved from `new_target` back to `target`.
4. If all rollbacks succeed, status transitions to `ROLLED_BACK`.
5. If any rollback operation raises an exception, status transitions to `FAILED`, preserving both the execution and rollback errors.

---

## 7. Persistence & Crash Recovery (Phase 15E Integration)

### Transaction Stores

- `InMemoryTransactionStore`: Thread-safe in-memory store for unit testing.
- `FileTransactionStore`: Durable atomic store persisting JSON journals to `.brainfrog/transactions/<hash>.json` with `os.fsync` durability and payload size bounds (`MAX_SERIALIZED_TRANSACTION_BYTES`).

### Crash Recovery Manager (`TransactionRecoveryManager`)

Phase 15E extends transaction recovery with write-ahead operation preparation and deterministic crash recovery:
- **Write-Ahead Journaling**: Each filesystem operation is persisted to disk in state `prepared` *before* modifying files on disk.
- **Commit Marker Boundary**: Verification passing does not equal committed. A transaction requires a durable `commit_marker` in `.brainfrog/transactions/` before it is deemed committed.
- **Reverse-Order Rollback**: Incomplete or crashed transactions are rolled back in strict reverse order (LIFO).
- **Fail-Closed Ambiguity**: In-flight mutations that match neither before nor after state are marked `FAILED` with diagnostics; recovery never guesses.
- **Startup Integration**: `BrainFrogRuntime` automatically recovers up to `MAX_STARTUP_RECOVERY_LIMIT` incomplete transactions on boot.
- For complete architecture details, see [docs/architecture/transaction-recovery.md](file:///c:/dame-project/tools/agentic_dev/docs/architecture/transaction-recovery.md).

---

## 8. Security & Boundary Guarantees

1. **Git Guard Protection**:
   The transaction directory `.brainfrog/transactions/` is registered in `security.git_guard.PROTECTED_PATH_PATTERNS`, preventing external modification or accidental git staging.
2. **Path Traversal Prevention**:
   All operation targets are validated against `workspace.resolve()`. Any path attempting traversal (`../`) is rejected with `PermissionError` before staging.
3. **Secret Scrubbing**:
   Transaction serialization (`Transaction.to_dict()`) automatically scrubs API keys, GitHub tokens, and credentials from metadata and error messages using `scrub_secrets()`.
4. **Contract Containment**:
   When an `ApprovedExecutionContract` is provided, every staged operation verifies write permissions via `contract.require_filesystem("write", target, workspace)`.
5. **Resource Limits**:
   Transactions enforce `MAX_TRANSACTION_OPERATIONS` (100) and `MAX_FILE_SNAPSHOT_BYTES` (5 MB) to prevent unbounded memory growth or denial-of-service.

---

## 9. Runtime Visibility & Slash Commands

Remote and CLI channels can inspect and manage transactions through runtime commands:

- `/transactions`: Lists the most recent 20 transactions for the active session.
- `/transaction <tx_id>`: Displays comprehensive details, operations, status, errors, commit marker, and verification outcome for a specific transaction.
- `/recover <tx_id>`: Deterministically recovers an interrupted transaction, executing rollback or finalizing state.

Phase 15F routes approved Telegram and WhatsApp coding work through this same
transaction boundary. See [Remote Autonomous Coding](remote-autonomous-coding.md)
for the complete remote lifecycle and its approval, contract, and Git limits.
