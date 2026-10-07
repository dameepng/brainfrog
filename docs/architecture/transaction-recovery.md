# BrainFrog Architecture: Crash Recovery & Transaction Journal (Phase 15E)

## 1. Executive Summary

Phase 15E establishes deterministic **Crash Recovery** and durable **Write-Ahead Transaction Journaling** for BrainFrog.

Where Phase 15D handled runtime execution failure while the process remains alive, Phase 15E ensures that transactions interrupted by abnormal termination (process crash, `SIGKILL`, machine reboot, unhandled exception, power loss) **never silently disappear or appear successful**.

### Core Invariants

1. **`orchestrator.py` remains the SOLE execution engine in BrainFrog.**
2. Recovery is deterministic infrastructure: it **never** queries an LLM (System 1 or System 2) to decide how to recover.
3. Recovery **never** mints contracts, mints capabilities, or bypasses `ApprovedExecutionContract`.
4. Recovery **never** executes arbitrary shell commands or remote git operations (such as git push).
5. A transaction is **never** considered committed merely because verification passed; an explicit durable **commit marker** is required.
6. Ambiguous filesystem states fail closed into `FAILED` with diagnostics; recovery never guesses or fabricates state.

---

## 2. Why Crash Recovery Exists

In multi-step autonomous agent workflows, filesystem mutations can be interrupted midway:

```
Transaction Started
    ↓
Operation A (created)     ✅ (on disk)
    ↓
Operation B (modified)    ✅ (on disk)
    ↓
Operation C (writing...)  💥 [Process Killed / System Restart]
    ↓
System Restarts
```

Without deterministic crash recovery:
- The workspace would be left in a half-written, inconsistent state.
- Subsequent runs would assume files were correctly produced.
- Git trees could become polluted or broken.

With Phase 15E Crash Recovery:
1. Upon restart, BrainFrog discovers all incomplete transactions in `.brainfrog/transactions/`.
2. It inspects the write-ahead journal to reconstruct the in-flight operation state.
3. It performs safe, reverse-order (LIFO) rollbacks.
4. It restores the filesystem to its exact pre-transaction state or fails closed if corrupted.

---

## 3. Transaction Journal & Write-Ahead Principle

For every filesystem mutation, the Transaction Engine enforces a write-ahead journaling protocol:

```
┌────────────────────────────────────────────────────────┐
│ 1. Record PREPARED Status in Journal                   │
│    (target, before snapshot, after payload, rollback)  │
└───────────────────────────┬────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────┐
│ 2. fsync & Atomic Persist to Transaction Store         │
└───────────────────────────┬────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────┐
│ 3. Execute Filesystem Mutation                         │
└───────────────────────────┬────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────┐
│ 4. Record EXECUTED Status in Journal                   │
└───────────────────────────┬────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────┐
│ 5. fsync & Atomic Persist to Transaction Store         │
└────────────────────────────────────────────────────────┘
```

If the process crashes at step 3, the durable journal on disk shows `status = PREPARED`. The recovery manager detects that this operation was in-flight and reconciles the disk state against the known `before_state` and `after_state`.

---

## 4. Operation Lifecycle & Journal States

Individual operations transition through discrete lifecycle states:

| Operation Status   | Meaning                                                                             | Recovery Action                                                     |
| :----------------- | :---------------------------------------------------------------------------------- | :------------------------------------------------------------------ |
| `staged`           | Operation registered in plan; no disk mutation attempted.                           | Verified untouched; no rollback mutation needed.                    |
| `prepared`         | Pre-mutation snapshot verified; intent flushed to journal before mutation.          | In-flight reconciliation: restore `before_state` or verify absent.  |
| `executed`         | Mutation completed and confirmed in journal.                                        | Reverse rollback applied to restore `before_state`.                 |
| `rolled_back`      | Rollback completed.                                                                 | Idempotently preserved; no additional action taken.                 |
| `failed`           | Operation failed execution or rollback.                                             | Fails closed; transaction marked `FAILED`.                          |

---

## 5. The Durable Commit Marker

A fundamental distinction is drawn between **verification passed** and **transaction committed**:

```
execute()
   ↓
verify()
   ↓
persist verification details in journal
   ↓
[CRASH HERE? -> Recovery executes ROLLBACK; NOT committed!]
   ↓
persist durable COMMIT marker (fsync)
   ↓
[CRASH HERE? -> Recovery preserves COMMITTED; commit was durably authorized!]
   ↓
transition to COMMITTED
   ↓
persist COMMITTED status
```

### Crash Before Commit Marker
Even if all tests passed and post-execution verification succeeded, if the process dies before the durable commit marker is written, the transaction is **uncommitted**. Recovery performs a full reverse-order rollback to restore clean workspace state.

### Crash After Commit Marker
Once the commit marker is durably flushed to `.brainfrog/transactions/`, the transaction is considered irrevocably committed. Crash recovery preserves the transaction as `COMMITTED` and **never** rolls it back.

---

## 6. Deterministic Recovery Algorithm

The `TransactionRecoveryManager` implements this deterministic sequence:

```
recover(transaction_id):
  1. Load transaction from store. If absent -> FAILED.
  2. If transaction is in terminal state (COMMITTED, ROLLED_BACK, FAILED):
       -> Return existing terminal result (Idempotent).
  3. If commit_marker is present:
       -> Transition to COMMITTED, save, do NOT rollback.
  4. Transition transaction to ROLLING_BACK.
  5. For each operation in REVERSED order (LIFO):
       a. Check operation type:
            If unknown / unsupported -> FAIL CLOSED (ambiguous=True, status=FAILED).
       b. If operation status is ROLLED_BACK:
            -> Skip.
       c. If operation status is STAGED:
            -> Verify target is untouched; mark ROLLED_BACK.
       d. If operation status is PREPARED (in-flight):
            Inspect filesystem state:
            - Matches before_state: mutation never wrote -> safe.
            - Matches after_state: mutation wrote -> restore before_state.
            - Matches neither: AMBIGUOUS -> FAIL CLOSED (ambiguous=True, status=FAILED).
       e. If operation status is EXECUTED:
            -> Restore before_state (delete if created, write back if modified/deleted, rename back).
  6. If all operations rolled back cleanly:
       -> Transition transaction to ROLLED_BACK, save, return success.
```

---

## 7. Ambiguous Filesystem State Handling

When a transaction is in-flight, arbitrary external changes or partial sector writes could corrupt a target file.

BrainFrog enforces the **Fail-Closed Ambiguity Invariant**:
- Recovery **never guesses**.
- Recovery **never fabricates** intermediate content.
- If an in-flight file matches neither `before_state` nor `after_state`, recovery immediately marks the operation and transaction as `FAILED`, sets `ambiguous=True`, preserves diagnostic context, and halts rollback.

---

## 8. Idempotent Recovery

Recovery is guaranteed safe to execute repeatedly:
- If `recover()` completes and is called again, it detects terminal status `ROLLED_BACK` (or `COMMITTED`) and exits immediately without touching the filesystem.
- If recovery itself is interrupted midway by a second crash, the operations already marked `ROLLED_BACK` in the journal are skipped upon second restart, and rollback resumes from the remaining operations.

---

## 9. Startup Recovery Integration

During `BrainFrogRuntime` initialization (`__init__`):
1. The runtime boots its `TransactionStore` (`FileTransactionStore` by default).
2. `recover_startup_transactions()` scans `.brainfrog/transactions/` via `store.list_incomplete()`.
3. Up to `MAX_STARTUP_RECOVERY_LIMIT` (50) incomplete transactions are automatically recovered.
4. Any unrecoverable or ambiguous transactions are logged with warnings and stored in `runtime.startup_recovery_results`.
5. The runtime then resumes normal operations without blocking or querying an LLM.

---

## 10. Resource Bounds & Safety Limits

To prevent unbounded journal growth, memory exhaustion, or denial-of-service, Phase 15E enforces strict bounds:

| Resource Limit                     | Value     | Enforcement Point                                      | Behavior on Violation                            |
| :--------------------------------- | :-------- | :----------------------------------------------------- | :----------------------------------------------- |
| `MAX_TRANSACTION_OPERATIONS`       | 100       | `stage_create`, `stage_modify`, `stage_delete`, rename | Raises `TransactionResourceLimitError` before op |
| `MAX_FILE_SNAPSHOT_BYTES`          | 5 MB      | Pre-mutation snapshot capture & payload staging        | Raises `TransactionResourceLimitError` before op |
| `MAX_SERIALIZED_TRANSACTION_BYTES` | 5 MB      | `FileTransactionStore.save()`                          | Raises `TransactionResourceLimitError` on save   |
| `MAX_STARTUP_RECOVERY_LIMIT`       | 50        | Runtime startup recovery discovery                     | Bounds incomplete transactions per boot cycle    |

---

## 11. Runtime Visibility & Slash Commands

Users and operators can query and manage recovery via CLI and remote channels:

- `/transactions`: Lists recent session transactions.
- `/transaction <id>`: Displays comprehensive details, including operation status and commit markers.
- `/recover <id>`: Manually invokes deterministic crash recovery on an incomplete transaction.

---

## 12. Limitations & Future Extension Points

### Supported Recovery Scope
Phase 15E strictly covers local filesystem operations managed by the Transaction Engine:
- `create_file`
- `modify_file`
- `delete_file`
- `rename_file`

### Explicitly Excluded (Out of Scope for Phase 15E)
- Shell command rollback (non-deterministic).
- Git remote operations / git push.
- Network requests / external API calls.
- Package manager installations (`pip`, `npm`).
- LLM-assisted or autonomous recovery decisions.

These primitives provide the deterministic foundation for **Phase 15F — Remote Autonomous Coding**.
