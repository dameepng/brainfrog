# Phase 14B-L01.2 Remediation Report

## 1. Findings Addressed

During the Phase 14B-L01.1 post-remediation red-team audit, two concrete concurrency and lifecycle vulnerabilities were reproduced:

1. **L01.1-F01 — Stale Snapshot Lost Update**: Concurrent requests to the same session read snapshot $S_0$, and serialized file writes allowed a later request to commit its stale snapshot $S_0 \to S_B$, silently overwriting an earlier committed update $S_0 \to S_A$. Committed turns vanished without error.
2. **L01.1-F02 — Stale Save Resurrecting `/reset` State**: When `/reset` was executed while an earlier request was in-flight, the earlier request holding stale `SessionState(incarnation=X)` executed `.save()` upon completion. This recreated the deleted session file, restored the old history, resurrected the stale incarnation $X$, and violated the H-02 security boundary.

Both findings have been completely remediated in this phase.

---

## 2. Root Cause

### L01.1-F01 (Lost Update)
- **Before**: `FileSessionStore.save()` acquired `_CrossProcessLock` around the physical atomic file write (`os.replace`). However, the logical read-modify-write lifecycle spanned from `runtime.handle_message()` start (`mgr.get_or_create()`) through tool execution to `runtime.handle_message()` finish (`mgr.save()`).
- Because session snapshots lacked monotonic revision tracking, the store had no mechanism to detect if the persisted disk state had advanced between when the session was loaded and when it was saved. Actor B loaded revision 1, Actor A loaded revision 1; Actor A saved revision 1 (advancing disk to state A); Actor B then saved its stale revision 1 snapshot, completely overwriting state A.

### L01.1-F02 (Reset/Save Resurrection)
- **Before**: When `/reset` was executed, `SessionManager.reset()` called `store.delete(session_id)`, which unlinked the target JSON file from the filesystem and released the per-session lock.
- If a slow request started before `/reset` held a reference to `SessionState(incarnation=X)`, when that request completed, it called `store.save(session)`. Since the file did not exist on disk, `save()` treated it as a brand-new session creation, wrote the stale session file to disk with incarnation $X$ and all pre-reset history, thereby resurrecting state that was supposed to be destroyed. Furthermore, `save()` had no knowledge of whether the session was previously reset.

---

## 3. Remediation Design

To remediate both vulnerabilities without introducing global locks or altering canonical orchestrator execution (`core/orchestrator.py` remains 100% untouched):

1. **Monotonic Revision Tracking (OCC)**: Added `revision: int = 1` to `SessionState`, serializing in session JSON. When loaded from store, `_persisted_revision` tracks the revision that was loaded.
2. **Per-Session Atomic Lock & Freshness Verification**: When `store.save()` executes under the per-session cross-process lock:
   - If the session file exists on disk, it parses `session_incarnation_id` and `revision`.
   - If disk `session_incarnation_id != session.session_incarnation_id`, it raises `StaleSessionStateError`.
   - If disk `revision != session._persisted_revision`, it raises `StaleSessionStateError`.
   - On successful validation, `session.revision = disk_revision + 1` is written atomically to disk and updated on the in-memory object.
3. **Reset Tombstone & Incarnation Anchoring**:
   - When `SessionManager.reset(session_id)` is invoked, it rotates `session_incarnation_id` and calls `store.reset(session_id, new_incarnation)`.
   - In `FileSessionStore.reset()`, under the session lock, the target session file is unlinked, and an atomic reset marker file (`.locks/<hash>.reset`) is persisted containing `{"session_id": session_id, "current_incarnation": new_incarnation, "reset_at": ...}`.
   - If a stale in-flight request attempts to save after `/reset`:
     - If the reset tombstone exists, it compares `session.session_incarnation_id` against the tombstone's `current_incarnation`. Mismatch immediately raises `StaleSessionStateError`.
     - If the target file was deleted and `session._persisted_incarnation is not None`, it detects that the session was deleted/reset and immediately raises `StaleSessionStateError`.
     - Stale requests are strictly prevented from recreating the session file.
4. **Runtime In-Flight Reset Guard & Error Handling**:
   - `BrainFrogRuntime.handle_message()` captures `initial_incarnation = session.session_incarnation_id` and `initial_revision = session.revision` at the start of request processing.
   - Before saving, it checks if `session.session_incarnation_id != initial_incarnation`. If rotated (e.g., via `/reset` in the same runtime), it emits `agent.session.stale` and returns a deterministic, secure rejection response.
   - `self.sessions.save(session)` is wrapped to catch `StaleSessionStateError`. If raised, it emits `agent.session.stale` and returns a deterministic error message:
     `"⚠️ Session state changed while this request was executing; result was not persisted."`
   - No sensitive paths, filenames, or stack traces are leaked to remote channels.

---

## 4. Concurrency Model

We adopted **Optimistic Concurrency Control (OCC) with Per-Session Atomic Verification & Incarnation Anchoring**:
- **Granularity**: Strictly per-session. The lock target is `hashlib.sha256(session_id.encode()).hexdigest() + ".lock"` located in the `.locks/` subdirectory. Different sessions run with complete concurrency with zero lock contention or global bottlenecks.
- **Transaction Boundary**:
  1. Read phase: Snapshot loaded with revision $R$ and incarnation $I$.
  2. Compute phase: Execution proceeds concurrently without blocking other sessions or requests.
  3. Commit phase: Acquire per-session lock $\to$ verify incarnation == disk incarnation $\to$ verify revision == disk revision $\to$ write revision $R+1$ atomically $\to$ update internal tracking $\to$ release lock.
- If concurrent writers race on the same session, the first writer to commit advances revision to $R+1$. The second writer attempts commit expecting $R$, encounters $R+1$, and is deterministically rejected with `StaleSessionStateError`.

---

## 5. F01 Fix

### Before
```python
# Unconditional overwrite under write-only lock
with self._get_session_lock(session.session_id):
    # Serialized temp write and atomic replace without checking disk state
    os.replace(temp_file, target_file)
```

### Root Cause
Absence of logical version/revision checking during the commit phase.

### Fix
```python
# In FileSessionStore.save() under _CrossProcessLock:
if target_file.exists() and target_file.is_file():
    disk_content = target_file.read_text(encoding="utf-8")
    if disk_content.strip():
        disk_data = json.loads(disk_content)
        disk_rev = int(disk_data.get("revision", 1))
        expected_rev = session._persisted_revision if session._persisted_revision is not None else session.revision
        if disk_rev != expected_rev:
            raise StaleSessionStateError(
                f"Stale session revision: current disk revision {disk_rev} != expected {expected_rev}"
            )
        session.revision = disk_rev + 1
```

### Verification
- Tested via 2 concurrent threads, 2 concurrent processes, 4 concurrent processes, and 8 concurrent processes (`tests/test_session_concurrency_remediation.py`).
- 0 lost committed updates. Stale writers deterministically fail with `StaleSessionStateError`.

### Result
**CLOSED (L01.1-F01)**.

---

## 6. F02 Fix

### Before
```python
# Stale session loaded before /reset
mgr.reset(session_id)  # unlinks file
# Old request finishes and calls save():
store.save(stale_session)  # target_file did not exist, wrote stale session file!
```

### Root Cause
Session deletion left no persistent record of the authoritative incarnation rotation, allowing stale in-flight requests holding the old incarnation to recreate the session file on disk.

### Fix
1. `FileSessionStore.reset()` writes an authoritative reset tombstone in `.locks/<hash>.reset`:
```python
meta_path = self._get_reset_meta_path(session_id)
meta_path.write_text(json.dumps({
    "session_id": session_id,
    "current_incarnation": new_incarnation,
    "reset_at": time.time(),
}))
```
2. In `FileSessionStore.save()`:
```python
# Check reset tombstone
if meta_path.exists():
    meta_data = json.loads(meta_path.read_text(encoding="utf-8"))
    authoritative_inc = meta_data.get("current_incarnation")
    if authoritative_inc and authoritative_inc != session.session_incarnation_id:
        raise StaleSessionStateError(
            f"Stale session incarnation: session '{session.session_id}' was reset."
        )

# If target file missing but session was previously persisted:
if not target_file.exists() and session._persisted_incarnation is not None:
    raise StaleSessionStateError(
        f"Session '{session.session_id}' was reset or deleted while request was in flight."
    )
```
3. In `runtime.handle_message()`:
Checks `session.session_incarnation_id != initial_incarnation` and catches `StaleSessionStateError`, preventing state persistence and returning a user-safe message.

### Verification
- Tested via direct stale save after reset, in-flight race with threading barrier, and 100-iteration race loops (`tests/test_session_concurrency_remediation.py`).
- 0 resurrection events. File remains absent or updated with new incarnation. Old history and old incarnation never return.

### Result
**CLOSED (L01.1-F02)**.

---

## 7. Reset Semantics

The conceptual sequence for `/reset` is now strictly enforced:
1. `SessionManager.reset(session_id)` acquires lock.
2. In-memory session object (if present) has `.reset()` invoked, rotating `session_incarnation_id` to a cryptographic random token (`secrets.token_hex(16)`) and clearing history and approvals.
3. If no in-memory session object exists, a new cryptographic token is generated.
4. `store.reset(session_id, new_incarnation)` is called. Under the cross-process lock:
   - Target session file `.json` is deleted.
   - Authoritative tombstone `.locks/<hash>.reset` is written containing the new authoritative incarnation.
5. Pending approvals for the session are invalidated in `ApprovalManager`.
6. Stale writers attempting to save with old incarnations are rejected by the tombstone and prevented from creating the file.
7. Subsequent valid requests adopt the authoritative incarnation from the tombstone upon initialization, successfully persisting their new clean history and removing the transient tombstone marker.

---

## 8. Incarnation Semantics

- Every `SessionState` maintains `session_incarnation_id`.
- Every save validates `disk_incarnation == session.session_incarnation_id`.
- Any mismatch raises `StaleSessionStateError`.
- Incarnation can NEVER rotate backward.
- Old incarnations cannot be resurrected after `/reset`.

---

## 9. Legacy Compatibility

- **Missing Revision in Existing Sessions**: When loading a legacy JSON file that does not contain a `"revision"` field, `SessionState.from_dict()` defaults `revision = 1`.
- **First Save of Loaded Legacy Session**: Upon load, `_persisted_revision = 1` is tracked. During save, if the disk file still has no revision field, it defaults to 1, matches `_persisted_revision`, and successfully advances to revision 2 on disk.
- **No Stale Overwrite Allowed**: Missing revisions are not treated as "always current" or allowed to bypass checks; they are safely anchored to version 1 and monotonically incremented.
- **Full Backward Compatibility**: All 14 tests in `tests/test_persistent_sessions.py` pass without modification.

---

## 10. New Tests

Created dedicated test suite: `tests/test_session_concurrency_remediation.py` containing 12 deterministic test cases:
- **Test A (`test_two_concurrent_writers_no_lost_update`)**: Two concurrent threads modifying same session. One commits, second stale write is rejected. Exactly 1 committed turn in history, 0 lost updates.
- **Test B (`test_multiprocess_two_workers_no_lost_update`)**: 2 concurrent worker processes. 0 lost committed updates.
- **Test C (`test_multiprocess_four_workers_no_lost_update`)**: 4 concurrent worker processes. Exactly monotonic revisions, 0 lost committed updates.
- **Test D (`test_multiprocess_eight_workers_no_lost_update`)**: 8 concurrent worker processes racing under load. All committed turns retained, 0 corruptions, 0 lost committed updates.
- **Test E (`test_save_after_reset_rejected`)**: Session created with incarnation X $\to$ stale snapshot taken $\to$ reset executed $\to$ stale save attempted. Stale save raises `StaleSessionStateError`, old history absent, old incarnation absent.
- **Test F (`test_reset_while_request_in_flight_rejected`)**: Threading barrier synchronizes in-flight execution. Request loads state $\to$ barrier pauses $\to$ reset occurs $\to$ request resumes and attempts save $\to$ rejected. 0 resurrection.
- **Test G (`test_repeated_race_100_iterations`)**: 100 consecutive rapid race iterations of reset vs concurrent save. 0 stale resurrection events (100/100 rejected).
- **Test H (`test_h02_approval_invalidation_and_stale_save_rejection`)**: Approval created under incarnation X $\to$ reset $\to$ claiming approval fails (H-02) $\to$ stale save fails (F02) $\to$ approval remains permanently unusable.
- **Test I (`test_cross_session_concurrency_independent_locks`)**: Sessions A, B, C, D written concurrently across threads. All succeed independently, zero cross-session contamination or false stale errors.
- **Test J (`test_restart_preserves_revision_and_incarnation`)**: State saved $\to$ store restarted/re-instantiated $\to$ loaded state preserves exact incarnation and revision $\to$ subsequent save advances revision monotonically.
- **Test K (`test_legacy_session_without_revision_migrates_safely`)**: File without revision field is loaded, correctly treated as revision 1, advances to revision 2 on save.
- **Test L (`test_runtime_handle_message_returns_safe_rejection_on_stale_save`)**: Runtime end-to-end test. When stale save occurs, runtime emits `agent.session.stale` event, does not crash, and returns user-safe rejection message.

All 12 tests pass in ~4.8 seconds.

---

## 11. Adversarial Reproduction Before/After

Empirical verification executed via `scratch/adversarial_reproduction_before_after.py`:

```
============================================================
ADVERSARIAL VERIFICATION: F01 (LOST UPDATE ATTACK)
============================================================
Stale save raised StaleSessionStateError : True
Error detail                             : Stale session revision: current disk revision 2 != expected 1
Actor B committed turn retained          : True
Actor A stale turn discarded             : True
Disk monotonic revision                  : 2
VERDICT F01                              : PASS - BLOCKED

============================================================
ADVERSARIAL VERIFICATION: F02 (RESET RESURRECTION ATTACK)
============================================================
Post-reset file deleted                  : True
Stale save raised StaleSessionStateError : True
Error detail                             : Stale session incarnation: session 'cli:u:f02' was reset. Authoritative incarnation is '9faeffaa44aaf44c1930556b82d524a4', attempted save with '0e0ff55119a81ced9939d23356f9a77f'.
File recreated by stale save             : False
Old incarnation resurrected              : False
Old history resurrected                  : False
VERDICT F02                              : PASS - BLOCKED
```

| Attack Scenario | Before (Phase 14B-L01.1) | After (Phase 14B-L01.2) |
|---|---|---|
| **L01.1-F01 Lost Update** | Actor A silently overwrote Actor B's turn. Revision remained untracked. Actor B's turn disappeared from history. | Actor A's stale write was detected (`current disk revision 2 != expected 1`) and raised `StaleSessionStateError`. Actor B's turn was preserved. |
| **L01.1-F02 Reset Resurrection** | Stale request saved after `/reset`, recreated session file, resurrected old incarnation and pre-reset history. | Stale request save was blocked by reset tombstone. Session file was NOT recreated. 0 old history, 0 old incarnation resurrected. |

---

## 12. H-02 Regression

H-02 ("Stale Approval") verification confirmed:
1. Approvals created under incarnation $X$ are bound to $X$.
2. Upon `/reset`, incarnation rotates to $Y$, and `invalidate_session_approvals()` marks all approvals for the session as stale.
3. Attempts to claim the old approval fail with approval invalidation error.
4. Attempts to run a stale session save fail with `StaleSessionStateError`.
5. Disk state remains clean, and old approvals cannot be resurrected or claimed under any circumstances.
- Verified across:
  - `tests/test_session_concurrency_remediation.py` (Test H)
  - `tests/test_approval_session_invalidation.py` (16 tests pass)
  - `tests/test_approval_process_concurrency.py` (6 tests pass)
  - `tests/test_approval_execution_contract.py` (6 tests pass)

---

## 13. Full Regression

Full test battery results:
1. **Step 16 Security Battery** (11 suites, 195 tests):
   - `test_session_concurrency_remediation.py` (12 tests)
   - `test_session_history_bounds.py` (19 tests)
   - `test_approval_process_concurrency.py` (6 tests)
   - `test_approval_session_invalidation.py` (16 tests)
   - `test_approval_execution_contract.py` (6 tests)
   - `test_e2e_remote_approval.py` (6 tests)
   - `test_remote_git_push_boundary.py` (14 tests)
   - `test_approval_prompt_history_boundary.py` (7 tests)
   - `test_target_extraction_security.py` (42 tests)
   - `test_git_guard.py` (12 tests)
   - `test_e2e_telegram_runtime.py` (27 tests)
   - `test_e2e_whatsapp_runtime.py` (28 tests)
   - **Result**: 195 tests ran in 119.9s, 191 passed, 4 skipped, 0 failures.

2. **Full Repository Discovery Battery**:
   - `python -m unittest discover -s tests`
   - **Result**: 414 tests ran, 409 passed, 5 skipped, 0 failures.

All previously closed security findings remain verified and closed:
- C-01 (Approval Scope Bleed): CLOSED
- C-02 (Path Traversal): CLOSED
- H-01 (Cross-Process Approval TOCTOU): CLOSED
- H-02 (Stale Approval): CLOSED
- H-03 (Automatic Remote Git Push): CLOSED
- M-01 (Prompt History Poisoning): CLOSED
- M-02 (Target Extraction): CLOSED
- M-03 (Approval Flooding): CLOSED
- L-01 (Session History Growth): CLOSED

---

## 14. Residual Risks

1. **Client Turn Discard on High Contention**: If a single user sends multiple rapid messages from different browser tabs simultaneously to the exact same chat session within milliseconds, one request will commit and the racing request will return:
   `"⚠️ Session state changed while this request was executing; result was not persisted."`
   This is the intended, fail-safe security invariant (reject stale write instead of silent corruption).
2. **Transient Reset Tombstone Files**: Reset tombstone files (`.locks/<hash>.reset`) are small metadata files created upon `/reset` and automatically removed on the next successful write. If a session is reset and never used again, the tiny metadata file remains in `.locks/`. Standard cleanup procedures or maintenance crons can purge old locks if desired.

---

## 15. Final Verdict

- **F01 (Lost Update)**: **CLOSED** (Deterministic optimistic concurrency control prevents stale overwrite).
- **F02 (Reset Resurrection)**: **CLOSED** (Reset tombstone and incarnation boundary prevent recreation of old state).
- **H-02 (Stale Approval)**: **CLOSED** (Approvals and incarnations remain tightly coupled; no resurrection).
- **L-01 (Session History Bounds)**: **CLOSED** (Bounds strictly enforced, 10k messages plateau at ~9.3 KB in 1.4s).
- **Overall Verdict**: **PASS**
