# Phase 14 Security Findings (Red-Team Reproductions)

Consolidated verbatim from the original Phase 14B red-team reports. Remediations: [remediation.md](remediation.md). Index: [../README.md](../README.md).

## Contents

1. Part A - L-01 Session History Growth red-team (originally `phase14bl01_redteam_report.md`)
2. Part B - L01.1 post-remediation red-team, findings F01/F02 (originally `phase14bl01_post_remediation_redteam_report.md`)

---

<!-- Part A -->

# Phase 14B-L01 Red-Team Report
**Security Finding Reproduction: L-01 — Session History Growth**
**Mode: Audit-Only (Zero Production Changes)**

---

## 1. Scope

This audit is an empirical red-team investigation of security finding **L-01 (Session History Growth)**.
Per Phase 14B-L01 constraints:
- **Strictly audit-only**: No remediation or production modifications were implemented.
- Production code files (`core/orchestrator.py`, `core/runtime/runtime.py`, `core/runtime/session.py`, `core/runtime/approval.py`, channel implementations, provider implementations, security policy, and persistence code) were untouched.
- No quotas, truncation, summarization, TTL, pagination, or compaction mechanisms were added.
- All experiments utilized synthetic, deterministic inputs through the real runtime execution path without external network dependencies.

---

## 2. Baseline

- **Commit**: `a0efee46a58ecc9ab25e3e8f4ef734c4ce4905cc` (Post M-03 remediation)
- **Unit Test Suite**: 383 total tests
  - **Passed**: 378
  - **Skipped**: 5
  - **Failures / Errors**: 0
- **Security Baseline Status**:
  - `C-01`: Approval Scope Bleed — **CLOSED**
  - `C-02`: Path Traversal — **CLOSED**
  - `H-01`: Cross-Process TOCTOU — **CLOSED**
  - `H-02`: Stale Approval — **CLOSED**
  - `H-03`: Automatic Git Push — **CLOSED**
  - `M-01`: Prompt History Poisoning — **CLOSED**
  - `M-02`: Target Extraction — **CLOSED**
  - `M-03`: Approval Flooding — **CLOSED**
  - `L-01`: Session History Growth — **OPEN** (Subject of this audit)

---

## 3. History Lifecycle

Source code inspection of `core/runtime/runtime.py`, `core/runtime/session.py`, and `core/orchestrator.py` identified the following lifecycle:

1. **Ingestion**:
   - Inbound requests enter via `BrainFrogRuntime.handle_message(message: IncomingMessage)`.
   - The runtime acquires or creates the session state via `self.session_store.get_or_create(session_key)`.
   - The user message text is appended to `session.history` via `session.add_message(role="user", content=message.text, ...)`.

2. **Persistence**:
   - Following processing, the assistant response is appended via `session.add_message(role="assistant", content=response_text, ...)`.
   - The runtime unconditionally calls `self.session_store.save(session)`.
   - `FileSessionStore.save()` converts the entire session object to a dictionary via `session.to_dict()` and serializes all history records into a single JSON file:
     `.brainfrog/sessions/<sha256(session_key)[:32]>.json`
   - File writes are executed atomically via a temporary file (`.tmp_<timestamp>_<rand>`) followed by `f.flush()`, `os.fsync(f.fileno())`, and `os.replace()`.

3. **Message Types Stored**:
   - **User Messages**: Stored unconditionally in `session.history`.
   - **Assistant Messages**: Stored unconditionally in `session.history`.
   - **Tool Calls / Tool Outputs**: Tool executions occur within `orchestrator.py` during turn processing; tool calls/outputs are returned in orchestrator run results but are **not** appended as distinct history items in `session.history`.
   - **Model / Runtime Errors**: Handled error strings returned to the user are stored as assistant messages.
   - **Permission Denials (Git Guard / Approval Rejection)**: Early returns in security validation intercept execution before `session.add_message()` is invoked; permission denial notices are **not** appended to session history.
   - **Slash Commands (`/exec`, `/plan`, etc.)**: Processed directly by `_handle_command()`; neither the command invocation nor its local output are added to `session.history`.
   - **Remote Channels (Telegram / WhatsApp)**: Messages received from remote channels route through `runtime.handle_message()` and are stored in `session.history` identically to CLI messages.

4. **Session Recovery**:
   - On runtime launch or session lookup, `SessionStore.get_or_create()` reads the session JSON file from disk using `json.load()` and reconstructs the `SessionState` via `from_dict()`. The complete historical record is loaded into memory.

5. **LLM Context Injection**:
   - In `core/runtime/runtime.py` (`_build_recent_history_context`), context injection into provider prompts is explicitly bounded:
     ```python
     recent = session.history[-3:]
     # assistant response text is truncated to max 300 characters
     ```
   - **Finding**: LLM prompt context injection is **strictly bounded** to the last 3 turns and does **not** grow unbounded with session history size.

6. **Limits, Truncation, and Expiration**:
   - In-memory `session.history`: Unbounded list (no length cap, no byte cap).
   - Persistent session JSON: Unbounded file (no truncation, no compaction).
   - No automated TTL or background expiration task is scheduled during normal operation.

---

## 4. Resource Model

The audit revealed three distinct resource tiers with differing growth characteristics:

1. **Resource A: In-Memory Session History (`session.history`)**:
   - Stored as an in-memory list of dictionaries in `SessionState`.
   - Growth is strictly $O(N)$ with message count and $O(M)$ with message payload size.
   - Retained in process heap for the duration of the runtime process lifetime.

2. **Resource B: Persistent Session State (`.brainfrog/sessions/*.json`)**:
   - On-disk JSON file storing metadata and cumulative history.
   - Steady-state file size grows linearly $O(N \cdot M)$.
   - However, because the store rewrites the entire file on every turn, cumulative I/O bytes written grow quadratically: **$O(N^2)$ write amplification**.

3. **Resource C: LLM Context History**:
   - Injected into prompt payloads as `Recent Conversation History`.
   - Hardcoded sliding window of $K = 3$ turns with 300-char assistant truncation.
   - Token growth is **$O(1)$** (constant ~36 tokens for synthetic prompts; strictly capped at under 1,500 tokens for arbitrary conversations).

---

## 5. Growth Results

Controlled scale testing was performed through the real runtime execution path (`BrainFrogRuntime.handle_message`) with on-disk `FileSessionStore`:

| Messages | History Entries | History Chars | Session Bytes | Disk Bytes | RSS (MB) | p50 (ms) | p95 (ms) | Max (ms) | Status |
|:---|:---|:---|:---|:---|:---|:---|:---|:---|:---|
| **10** | 10 | 480 | 2,490 | 2,536 | 65.8 | 25.26 | 44.75 | 44.75 | Completed |
| **100** | 100 | 4,800 | 21,390 | 21,436 | 67.2 | 25.83 | 28.99 | 33.47 | Completed |
| **500** | 500 | 24,000 | 105,190 | 105,236 | 67.8 | 34.21 | 42.95 | 71.56 | Completed |
| **1,000** | 1,000 | 48,000 | 210,090 | 210,136 | 68.9 | 51.25 | 66.16 | 87.69 | Completed |
| **2,500** | 2,500 | 120,000 | 524,790 | 524,836 | 72.2 | 85.97 | 116.42 | 150.13 | Completed |
| **5,000** | 5,000 | 240,000 | 1,049,190 | 1,049,236 | 73.5 | 148.38 | 195.71 | 286.07 | Completed |
| **10,000** | — | — | — | — | — | — | — | — | **Stopped Early** |

### Safe Stop Threshold Triggered:
Per Section 7 and Section 26 ("Safe Stop Conditions — single operation > 30s or runtime becomes excessively slow"), tier 10,000 was stopped early.
- Tier 5,000 required 372.2 seconds (6.2 minutes) to complete.
- Median latency degraded by **5.9x** (from 25.26 ms to 148.38 ms).
- Extrapolating to 10,000 messages projects execution duration of >23 minutes with >7.5 GB cumulative write amplification and over 80,000 regex scans per message in the pipeline.

### Growth Curve & Complexity:
- **Final Disk Size**: Strictly linear $O(N)$ growth with a measured slope of **209.8 bytes/message** for standard short turns.
- **In-Memory Heap (RSS)**: Grew from 65.8 MB to 73.5 MB (+7.7 MB across 5,000 turns).
- **Latency Curve**: Degraded from ~25 ms to ~148 ms median latency as full-session serialization and regex matching overhead scaled with $N$.

---

## 6. Payload Results

Stress testing with fixed payload sizes across isolated sessions:

| Payload Size | Messages | Entries | Disk Bytes | Growth / Msg | Process RSS Delta | Avg Latency |
|:---|:---|:---|:---|:---|:---|:---|
| **100 B** | 30 | 30 | 8,741 B | 291.4 B/msg | +0.81 MB (27.6 KB/msg) | 20.10 ms |
| **1 KB** | 30 | 30 | 36,426 B | 1,214.2 B/msg | +1.40 MB (47.9 KB/msg) | 21.78 ms |
| **4 KB** | 30 | 30 | 128,589 B | 4,286.3 B/msg | +2.61 MB (89.2 KB/msg) | 24.36 ms |
| **16 KB** | 20 | 20 | 331,658 B | 16,582.9 B/msg | +2.52 MB (129.2 KB/msg) | 34.27 ms |
| **64 KB** | 15 | 15 | 986,139 B | 65,742.6 B/msg | +1.82 MB (124.0 KB/msg) | 72.45 ms |

### Empirical Findings:
- Storage growth scales directly with payload size: **$O(N \cdot M)$**.
- At 64 KB per message, only 15 messages produce nearly 1.0 MB of serialized JSON on disk.
- Per-message latency increases noticeably with larger payloads due to JSON string escaping and disk flushing overhead (from 20.10 ms at 100B to 72.45 ms at 64KB).

---

## 7. Runtime Results

All execution paths were tested using mock channel transports against the real runtime:

- **CLI Path**:
  - 100 messages processed -> 100 history entries, 20.16 KB disk.
  - History boundary enforced: **False** (unbounded).
- **Telegram Channel Path**:
  - 100 messages -> 100 entries, 20.78 KB disk, 2.22s.
  - 500 messages -> 500 entries, 102.67 KB disk, 12.21s.
  - History boundary enforced: **False** (unbounded).
- **WhatsApp Channel Path**:
  - 100 messages -> 100 entries, 20.81 KB disk, 2.18s.
  - 500 messages -> 500 entries, 102.71 KB disk, 12.12s.
  - History boundary enforced: **False** (unbounded).

Neither channel implements any ingress rate limiting, history cap, or byte quota prior to persistence.

---

## 8. Cross-Session Isolation

- **Methodology**: Session A was inflated with 500 messages (100.66 KB on disk). Then, an independent Session B was created and exercised with 11 messages.
- **Findings**:
  - Session B stored 11 entries (2.53 KB on disk).
  - Session B request latency averaged 17.96 ms (baseline was 28.38 ms).
  - **Data Isolation**: Session B has completely isolated history and memory state; Session A's history is not leaked or injected into Session B.
  - **Global Coupling Vector**: `FileSessionStore.list_sessions()` reads and parses **every** `.json` file in `.brainfrog/sessions/`. In the presence of large session files, `list_sessions()` took 17.54 ms. If an attacker inflates multiple sessions to 50–100 MB, `list_sessions()` will suffer severe disk I/O and deserialization latency degradation.

---

## 9. Cross-User Isolation

- **Methodology**: User Alice's session was inflated with 500 messages. User Bob then dispatched a request.
- **Findings**:
  - User Alice: 500 entries.
  - User Bob: 1 entry, request latency 23.52 ms.
  - Bob's LLM context and session state remained entirely clean.
  - No direct in-memory or prompt interference was observed across users.

---

## 10. Restart Tests

Cold recovery was evaluated by reloading saved sessions across scale tiers:

| History Tier | Disk File Size | Runtime Init (ms) | Session Load (ms) | Total Recovery (ms) | Post-Recovery RSS Delta |
|:---|:---|:---|:---|:---|:---|
| **100** | 16.27 KB | 2.91 ms | 9.26 ms | 12.17 ms | 0.0 MB |
| **1,000** | 158.47 KB | 2.73 ms | 8.48 ms | 11.20 ms | 0.0 MB |
| **5,000** | 786.93 KB | 2.41 ms | 13.09 ms | 15.50 ms | +0.01 MB |
| **10,000** | 1,577.09 KB | 2.58 ms | 21.45 ms | 24.03 ms | +0.02 MB |

### Findings:
- Deserialization and loading via `json.load()` scales linearly with file size.
- Even at 10,000 entries (1.58 MB), total recovery time remained exceptionally fast (**24.03 ms**).
- Process restart overhead is not a primary denial-of-service vector at tested scales.

---

## 11. Write Amplification

Because `FileSessionStore.save()` writes the full session JSON representation on every interaction rather than appending deltas, write amplification was directly measured over 500 consecutive turns:

- **Messages Processed**: 500
- **Total User Payload Input**: 16.50 KB
- **Final Session JSON File Size**: 110.31 KB
- **Cumulative Bytes Written to Disk**: **26.98 MB**
- **Write Amplification vs User Payload**: **1,635.05x**
- **Write Amplification vs Final Disk Size**: **244.58x**
- **First Save Write Size**: 440 bytes
- **500th Save Write Size**: 107,289 bytes
- **Theoretical Complexity**: **$O(N^2)$ cumulative disk writes**
- **Transient Disk Duplication**: Atomic writing writes to a temporary file, calls `os.fsync()`, and replaces the target file. Peak disk consumption during write is exactly **$2.0\times$** the steady-state file size.

### Evidence:
The cumulative disk write volume $W(N)$ for $N$ turns with average turn size $S$ is:
$$W(N) = \sum_{k=1}^N (k \cdot S) \approx \frac{N^2 \cdot S}{2}$$
For 5,000 turns, cumulative disk writes reach approximately **2.6 GB**. For 10,000 turns, cumulative disk writes exceed **10.5 GB** for less than 2 MB of persistent state.

---

## 12. Tool / Assistant Amplification

- Standard user requests produce **1 history record** per turn (containing both `user` and `assistant` text).
- **Asymmetric Response Amplification**:
  - Simple user input: 20 characters.
  - Rich assistant output (e.g. detailed code generation, listings): 2,154 characters.
  - History expansion ratio: **107.7x** relative to user input size.
  - Thus, an attacker can intentionally craft short prompts that provoke verbose model replies to accelerate disk and memory growth.

---

## 13. Error Amplification

- **Model / Processing Errors**: Caught exceptions generating an assistant response string are stored in `session.history` (1 entry added).
- **Permission Denials (Git Guard / Security Approvals)**: Intercepted before history mutation; 0 entries added.
- **Slash Commands**: Handled by command router; 0 entries added.

---

## 14. Reset / Expiration

- **/reset Command**:
  - Evaluated on a 500-entry session (101.56 KB on disk).
  - Executing `/reset` cleans `session.history.clear()`, rotates `session_incarnation_id`, and unlinks the `.json` file from disk via `store.delete_session()`.
  - Result: 0 entries in memory, 0 bytes on disk.
  - **/reset successfully reclaims both in-memory and on-disk historical resources.**
- **Session Expiration**:
  - `SessionStore.cleanup_stale_sessions(max_age_seconds)` exists as an uncalled helper method.
  - No active background thread, periodic job, or runtime hook calls `cleanup_stale_sessions()`.
  - **No automated session TTL or cleanup is active in production.**

---

## 15. Attack Path

An authorized remote actor (e.g., an allowlisted Telegram or WhatsApp user) can trigger unbounded session growth and excessive write amplification without needing approval, filesystem access, or elevated permissions:

```text
Authorized Remote Attacker (Telegram / WhatsApp)
      │
      ▼ (Repeated messages or large 64KB text payloads)
Remote Webhook / Polling Transport
      │
      ▼ (No rate limiting, turn limit, or payload quota)
BrainFrogRuntime.handle_message()
      │
      ▼ (session.add_message appends to unbounded list)
SessionState.history ($O(N)$ memory growth)
      │
      ▼ (Full session rewritten to disk on EVERY turn)
FileSessionStore.save() ($O(N^2)$ write amplification + 2x transient duplication)
      │
      ▼
Disk I/O Flooding & Latency Degradation (p50 degraded 5.9x by 5k messages)
```

---

## 16. Risk Assessment

### Rating: **VULNERABLE**

### Evaluation of Definitions:
- **PASS**: Not applicable. History is not meaningfully bounded on disk or in memory.
- **PARTIAL**: Not applicable. Both in-memory and persistent storage vectors grow unbounded.
- **VULNERABLE (MATCHED)**: An authorized actor can cause materially unbounded resource growth ($O(N)$ in-memory list, $O(N)$ persistent JSON, $O(N^2)$ write amplification), but the impact remains localized to the active session and requires sustained activity. Crucially, LLM prompt context is strictly bounded to the last 3 turns, cross-session data isolation is maintained, process RSS grows slowly (~7.7 MB across 5k turns), and recovery time remains fast (<25 ms).
- **SEVERE**: Not justified. The growth does not cause immediate process crash, does not poison other users' prompts, and does not blow up LLM token contexts.

---

## 17. Recommended Remediation

*(Recommendations only — no production changes implemented in this phase)*

1. **Sliding Window Cap on Session History**:
   - Cap `session.history` to a configurable maximum of recent turns (e.g. 50 or 100 turns).
2. **Per-Session Byte Quota**:
   - Enforce an upper bound on cumulative session history size (e.g. 1 MB total history per session).
3. **Append-Only / Partitioned Session Storage**:
   - Migrate from rewriting the entire session JSON on every turn to an append-only format (e.g., JSON Lines or SQLite) to eliminate the severe $O(N^2)$ write amplification.
4. **Automated Session Sweeper / TTL**:
   - Schedule periodic background invocation of `cleanup_stale_sessions()` to prune inactive sessions older than a configured TTL (e.g. 7 days).
5. **Decouple `list_sessions()` from Full Deserialization**:
   - Store lightweight session metadata separately from conversation history to prevent large history files from degrading `list_sessions()` performance.

---

## 18. Regression

Full regression test execution:
```text
python -m unittest discover -s tests
Ran 383 tests in 152.133s
OK (skipped=5)
```
Zero test regressions. All existing security controls (C-01, C-02, H-01, H-02, H-03, M-01, M-02, M-03) remain completely passing.

---

## 19. Git Status

- Production source files: **Unchanged** (`git diff` output is empty).
- Audit files added (historical paths; report now consolidated here, `scratch/` removed during repository hygiene cleanup):
  - `phase14bl01_redteam_report.md`
  - `scratch/test_l01_redteam_audit.py`
  - `scratch/l01_audit_metrics.json`
- Git working tree: Clean.

---

## 20. Final Verdict

**VULNERABLE — L-01 REPRODUCED**

---

<!-- Part B -->

# Phase 14B-L01.1 Post-Remediation Red-Team Report
**Audit Phase:** Phase 14B-L01.1 Post-Remediation Adversarial Verification (Audit-Only)
**Target Finding:** L-01 — Session History Growth & Persistence Concurrency Correctness
**Evaluator:** Red-Team Adversarial Auditor
**Date:** 2026-10-03
**Verdict:** **PARTIAL — L-01 BOUNDED STORAGE VERIFIED; RESIDUAL CONCURRENCY/INCARNATION RACE IDENTIFIED**

---

## 1. Scope

Adversarial audit of the L-01 Session History Growth remediation in BrainFrog, focusing on:
1. Bounded history retention, entry limits, and byte budgets.
2. UTF-8 code point clamping on message boundaries.
3. Storage plateauing and elimination of $O(N^2)$ persistence write amplification.
4. Same-session and cross-process concurrency safety.
5. Stale snapshot / lost-update resilience under concurrent actors.
6. Reset vs. save and incarnation resurrection races (interaction with H-02).
7. Legacy migration, crash during atomic persistence, and path traversal resistance.
8. Channel isolation across CLI, Telegram, and WhatsApp.

**Rule of Engagement:** STRICTLY AUDIT-ONLY. No production code was modified during this phase.

---

## 2. Commit Tested

- **Base Remediation Commit:** `86e12e9` (`fix(security): remediate L-01 bounded session history retention and O(1) persistence`)
- **Active Commit:** `25fefb2` (`fix(system1): initialize resp before try block and safely access HTTPError response`)
- **Branch:** `fix/antigravity-prevent-tool-loop-timeout`
- **Production Files Modified in this Phase:** **NONE (0 files changed)**

---

## 3. Environment

- **OS:** Windows 11 Enterprise (NTFS filesystem, MSVC CRT locking)
- **Python:** 3.12.9
- **CPU:** 16 Cores, NVMe SSD
- **Locking Backing:** `msvcrt.locking` with non-blocking retry loop + thread reentrant `threading.RLock`

---

## 4. Implementation Reviewed

- `core/runtime/session.py` (SessionState, FileSessionStore, SessionManager, `_CrossProcessLock`)
- `core/runtime/runtime.py` (BrainFrogRuntime, message dispatch, `/reset` handling)
- `core/runtime/messages.py` (IncomingMessage, OutgoingMessage)
- `core/memory.py` (MemoryStore, Learning)
- `core/orchestrator.py` (canonical execution engine)
- `tests/test_session_history_bounds.py` (L-01 boundary test suite)

### Architecture Trace:
```
Incoming Message (CLI / Telegram / WhatsApp)
       │
       ▼
BrainFrogRuntime.handle_message()
       │
       ▼
SessionManager.get_or_create() -> in-memory cache lookup
       │
       ▼
Orchestrator.run() -> System 1 / System 2 execution
       │
       ▼
SessionState.record_interaction()
       ├── bound_message_text(32 KB UTF-8)
       ├── _enforce_history_bounds() (50 entries, 256 KB)
       │
       ▼
SessionManager.save() -> FileSessionStore.save()
       ├── _CrossProcessLock (.locks/<hash>.lock)
       ├── serialize JSON (~11.5 KB)
       ├── write temp file (.tmp_<hash>_<uuid>.json)
       ├── fsync()
       └── os.replace() -> target .json
```

---

## 5. Attack Matrix

| # | Attack Vector | Target Invariant | Result | Verdict |
|---|---|---|---|---|
| 1 | Config Tampering | Negative / zero / malformed env knobs | Clamped to safe minimums | **PASS** |
| 2 | Message Size Overflow | Payloads up to 1 MB, multibyte CJK, emoji | Clamped to $\le 32$ KB valid UTF-8 | **PASS** |
| 3 | History Entry Flooding | 49, 50, 51, 100, 1,000, 10,000 messages | Strictly $\le 50$ entries | **PASS** |
| 4 | History Byte Exhaustion | 50 × 32 KB payloads (1.6 MB raw) | Clamped to $\le 256$ KB serialized | **PASS** |
| 5 | Thread Concurrency | 2, 4, 8 threads writing to same session | Valid JSON, $\le 50$ entries | **PASS** |
| 6 | Process Concurrency | 2, 4, 8 processes writing to same session | Valid JSON, $\le 50$ entries | **PASS** |
| 7 | **Stale Snapshot Overwrite** | Concurrent read-modify-write | **Committed updates lost** | **FAIL** |
| 8 | **Reset vs Save Race** | Stale save in-flight during `/reset` | **Old history & incarnation resurrected** | **FAIL** |
| 9 | Path Traversal | `../`, drive letters, UNC, separators | Traversal blocked via SHA256 hashing | **PASS** |
| 10 | Legacy Migration Attack | 10,000 legacy turns in JSON file | Auto-compacted to 50 on load | **PASS** |
| 11 | Corrupt JSON Injection | Truncated / malformed JSON on disk | Fails closed, quarantined to `corrupt/` | **PASS** |
| 12 | Crash during Save | Orphan `.tmp` left during atomic write | Previous valid file intact | **PASS** |
| 13 | Active Cleanup Race | `cleanup_stale_sessions` vs active write | Active lock prevents deletion | **PASS** |
| 14 | Cold Restart | Reload across scales 100 → 10,000 | FIFO newest 50 intact in $\approx 9$ ms | **PASS** |
| 15 | Storage Plateau | Scale tiers 10 → 10,000 | Flat plateau at 11,535 B | **PASS** |

---

## 6. Same-Session Concurrency

- **Test:** `run_threads_test(num_threads=2, 4, 8, writes_per_thread=25)`
- **Setup:** 2, 4, and 8 concurrent threads executing `record_interaction()` and `mgr.save()` on the same session in an isolated temporary session directory.
- **Expected:** No exceptions, no JSON corruption, final history $\le 50$ entries, structurally valid turns.
- **Observed:**
  - 2 Threads: 50 attempted writes in 0.165s | Final history: 50 | Errors: 0
  - 4 Threads: 100 attempted writes in 0.400s | Final history: 50 | Errors: 0
  - 8 Threads: 200 attempted writes in 0.924s | Final history: 50 | Errors: 0
- **Result:** **PASS**. Thread-level `RLock` on `SessionManager` and `FileSessionStore` prevented process crashes and structural JSON corruption.
- **Security Impact:** Same-process thread persistence is structurally sound.

---

## 7. Cross-Process Concurrency

- **Test:** `run_cross_process_test(num_procs=2, 4, 8, writes_per_proc=25)`
- **Setup:** 2, 4, and 8 independent OS processes opening separate `FileSessionStore` instances against the same session path.
- **Expected:** Exactly one valid session file, no corrupted JSON, no file lock deadlocks, final history $\le 50$.
- **Observed:**
  - 2 Processes (50 writes): 2.086s | Valid JSON: True | History: 25
  - 4 Processes (100 writes): 2.677s | Valid JSON: True | History: 50
  - 8 Processes (200 writes): 4.280s | Valid JSON: True | History: 50
  - No orphaned temp files, no unhandled exceptions.
- **Result:** **PASS**. OS advisory file locks (`_CrossProcessLock`) prevented concurrent atomic file replacement collisions and corruption.
- **Security Impact:** Multi-process access does not produce torn writes or corrupt session files on disk.

---

## 8. Lost-Update / Stale Snapshot (CRITICAL FINDING)

- **Test:** `test_stale_snapshot_lost_update()`
- **Setup:**
  1. Initial session created with turn $M_0$.
  2. Actor A loads session state (snapshot contains $[M_0]$).
  3. Actor B loads session state, appends turn $M_B$, and calls `store.save(session_B)`.
  4. Disk now contains $[M_0, M_B]$.
  5. Actor A resumes, appends turn $M_A$ to its stale snapshot, and calls `store.save(session_A)`.
- **Expected (Ideal Concurrency):** Actor A's save detects that the session on disk advanced since it was loaded, either merging or rejecting stale overwrites.
- **Observed:**
  - Actor B save: `True`
  - Actor A stale save: `True`
  - Actor B message retained in final disk state: **`False`** (LOST)
  - Actor A message retained: **`True`**
  - **`LOST UPDATE OCCURRED: True`**
- **Result:** **FAIL**.
- **Root Cause:**
  `FileSessionStore.save(session)` acquires the lock only around the serialization and `os.replace` step. The lock is NOT held across the read-modify-write transaction. Because `FileSessionStore` lacks generation counters, ETags, or pre-write version checks, any process saving a stale in-memory snapshot silently overwrites and deletes turns committed by concurrent actors.
- **Security Impact:** High logical integrity risk. While storage bounds are preserved, concurrent channels or background tasks can silently drop user turns, diagnostic context, or audit trails.

---

## 9. Reset vs. Save Race (CRITICAL FINDING — H-02 RESURRECTION)

- **Test:** `test_reset_vs_save_race()`
- **Setup:**
  1. Session `cli:reset_race:1` populated with turns $T_1, T_2$ under incarnation `INC_OLD`.
  2. Actor A holds an in-flight snapshot of `SessionState` (`INC_OLD`, $[T_1, T_2]$).
  3. Actor B issues `/reset`, which calls `session.reset()`, rotates incarnation to `INC_NEW`, and executes `store.delete(session_id)`.
  4. Target session file is successfully deleted from disk.
  5. Actor A finishes processing and calls `store.save(stale_snapshot)`.
- **Expected:** Stale save cannot resurrect a deleted session or overwrite post-reset incarnation.
- **Observed:**
  - Post-reset file exists: `False` (properly deleted by reset)
  - File recreated by stale save: **`True`**
  - Old incarnation resurrected: **`True`** (`INC_OLD` restored to disk)
  - Old history resurrected: **`True`** ($[T_1, T_2, T_{\text{late}}]$ restored to disk)
  - **`RESET VULNERABILITY REPRODUCED: True`**
- **Result:** **FAIL**.
- **Security Impact:**
  Directly reopens a window into **H-02 (Stale Approval Invalidation)**. H-02 relies on `session_incarnation_id` rotation to guarantee that approvals issued prior to `/reset` cannot be executed. If a stale save recreates the session file with `INC_OLD`, a process restart will reload `INC_OLD`, potentially re-validating approvals that were invalidated by the user's explicit `/reset`.

---

## 10. /new Isolation

- **Test:** Section 7 of `audit_step7_8_12_13_14_15.py`
- **Setup:** Multiple sessions created across `alice:chat_1`, `alice:chat_2`, `bob:chat_1`, and `telegram:alice:chat_1`. Alice sends secret turns into `alice:chat_1`.
- **Expected:** Zero history leakage across distinct conversation IDs, users, or channels.
- **Observed:**
  - `alice:chat_1`: 1 entry
  - `alice:chat_2`: 0 entries
  - `bob:chat_1`: 0 entries
  - `telegram:alice:chat_1`: 0 entries
- **Result:** **PASS**.

---

## 11. Incarnation Security

- **Test:** Section 8 of `audit_step7_8_12_13_14_15.py`
- **Setup:** Verify incarnation behavior across reset, reload, and legacy migration.
- **Expected:**
  - Incarnation rotates to a new 32-character hex token on reset.
  - Legacy files without incarnation receive a newly minted token on `from_dict()`.
- **Observed:**
  - Initial incarnation: `b0063d207ab17fb6611abb7dbfad52e8`
  - Post-reset incarnation: `08dbaa94f4b0215573b0c61265ed6dd5` (Rotated: True)
  - Legacy session generated: `12bdc2c15075000696d6eb502c338cf7` (Valid: True)
- **Result:** **PASS** (under non-concurrent conditions; concurrency race documented in Section 9).

---

## 12. Message Boundary

- **Test:** `scratch/audit_step9_10_11_boundaries.py`
- **Setup:** Test 13 payload variants against `bound_message_text(text, 32768)`.
- **Observed:**
  - ASCII 1 MB: clamped to exactly 32,768 bytes, ends with ` ... [TRUNCATED]`.
  - 4-Byte Emojis (64 KB): clamped to exactly 32,768 bytes, valid UTF-8.
  - CJK Characters (72 KB): clamped to 32,767 bytes (avoiding split 3-byte character).
  - Combining Diacritics: clamped to 32,768 bytes, valid UTF-8.
  - Embedded Null Bytes: clamped to 32,768 bytes, valid string.
  - Pre-existing Truncation Suffixes: clamped to 32,768 bytes, no suffix growth loop.
- **Result:** **PASS**. Clean UTF-8 clamping strictly enforced.

---

## 13. History Entry Boundary

- **Test:** `audit_step9_10_11_boundaries.py` (Step 10)
- **Setup:** Record 49, 50, 51, 100, 1,000, and 10,000 interactions on a session with `max_history_entries=50`.
- **Observed:**
  - 49 attempted: 49 in-memory, 49 in `to_dict()`.
  - 50 attempted: 50 in-memory, 50 in `to_dict()`.
  - 51 attempted: 50 in-memory, 50 in `to_dict()`.
  - 100 attempted: 50 in-memory, 50 in `to_dict()`.
  - 1,000 attempted: 50 in-memory, 50 in `to_dict()`.
  - 10,000 attempted: 50 in-memory, 50 in `to_dict()`.
- **Result:** **PASS**. FIFO eviction strictly prevents entry count exceeding 50.

---

## 14. History Byte Boundary

- **Test:** `audit_step9_10_11_boundaries.py` (Step 11)
- **Setup:** Test 50 entries with payload sizes 10B, 1 KB, 4 KB, 8 KB, 32 KB against `max_history_bytes = 256 KB`.
- **Observed:**
  - 50 × 1 KB: 50 retained | 106,400 B serialized (Budget: 262,144 B) — PASS
  - 50 × 4 KB: 31 retained | 256,429 B serialized (Budget: 262,144 B) — PASS
  - 50 × 8 KB: 15 retained | 246,956 B serialized (Budget: 262,144 B) — PASS
  - 50 × 32 KB: 3 retained | 196,848 B serialized (Budget: 262,144 B) — PASS
- **Result:** **PASS**. History bytes strictly capped at $\le 256$ KB.

---

## 15. Legacy Session Handling

- **Test:** `audit_step7_8_12_13_14_15.py` (Step 12)
- **Setup:**
  1. Manually craft legacy session with 10,000 turns and write directly to disk.
  2. Manually craft corrupted JSON file with broken braces.
- **Observed:**
  - Legacy 10,000 entries loaded: successfully normalized to exactly 50 entries.
  - Corrupt JSON: `store.load()` returned `None`, removed bad file from active store, quarantined to `.brainfrog/sessions/corrupt/<hash>_<time>_invalid_json.corrupt`.
- **Result:** **PASS**. Fail-closed quarantine and legacy compaction operate cleanly.

---

## 16. Crash / Atomic Persistence

- **Test:** `audit_step7_8_12_13_14_15.py` (Step 13)
- **Setup:** Write valid session, inject abandoned `.tmp_...` file containing half-written invalid JSON, and attempt session load.
- **Observed:**
  - Valid session loaded cleanly.
  - Abandoned `.tmp` file ignored by `load()` and `list_sessions()`.
  - `_clean_stale_tmp_files()` cleans orphan `.tmp` files older than 60s.
- **Result:** **PASS**.

---

## 17. Path / Symlink Security

- **Test:** `audit_step7_8_12_13_14_15.py` (Step 14)
- **Setup:** Probe `_get_session_path()` with traversal vectors: `../../etc/passwd`, `C:\Windows\System32`, `\\remote-smb\share\evil`, `nested/../../escape`.
- **Observed:**
  - Every session ID is mapped to `hashlib.sha256(session_id.encode()).hexdigest()[:32] + ".json"`.
  - Target path remains strictly inside `.brainfrog/sessions/`.
  - Explicit prefix assertion `str(target).startswith(str(self.sessions_dir))` raises `PermissionError` if escaped.
- **Result:** **PASS**. Directory traversal completely mitigated.

---

## 18. Cleanup Race

- **Test:** `audit_step7_8_12_13_14_15.py` (Step 15)
- **Setup:** Race `cleanup_stale_sessions()` against an active session holding a cross-process advisory lock.
- **Observed:**
  - Stale session ($>100,000$ seconds old) deleted.
  - Active session holding lock was skipped (`TimeoutError` on lock acquisition causes safe skip).
  - Active session file preserved intact.
- **Result:** **PASS**.

---

## 19. Restart Recovery

- **Test:** `scratch/audit_step16_18_restart_channels.py` (Step 16)
- **Setup:** Simulate process death and cold restart across 100, 500, 1,000, 2,500, 5,000, 10,000 turns.
- **Observed:**
  - Scale 100: 50 entries, 7,238 B, load time 11.60 ms, oldest: 'msg 50', newest: 'msg 99'.
  - Scale 1,000: 50 entries, 7,340 B, load time 10.63 ms, oldest: 'msg 950', newest: 'msg 999'.
  - Scale 10,000: 50 entries, 7,442 B, load time 9.83 ms, oldest: 'msg 9950', newest: 'msg 9999'.
- **Result:** **PASS**. Restart recovers newest 50 turns in $<12$ ms.

---

## 20. Storage Plateau Benchmark

Reproduced via `scratch/benchmark_l01_remediation.py`:

| Messages | Duration | p50 Latency | p95 Latency | Max Latency | History Turns | File Size | Cum. Written |
|---|---|---|---|---|---|---|---|
| **10** | 0.033 s | 3.276 ms | 4.337 ms | 4.337 ms | 10 | **2,683 B** | 0.016 MB |
| **100** | 0.429 s | 4.323 ms | 5.597 ms | 6.322 ms | 50 | **11,339 B** | 0.828 MB |
| **500** | 2.389 s | 4.606 ms | 6.026 ms | 7.354 ms | 50 | **11,433 B** | 5.187 MB |
| **1,000** | 4.875 s | 4.567 ms | 6.120 ms | 32.788 ms | 50 | **11,440 B** | 10.641 MB |
| **2,500** | 12.466 s | 4.699 ms | 6.218 ms | 10.726 ms | 50 | **11,539 B** | 27.134 MB |
| **5,000** | 23.778 s | 4.517 ms | 5.888 ms | 14.975 ms | 50 | **11,532 B** | 54.641 MB |
| **10,000** | 48.657 s | 4.648 ms | 5.946 ms | 15.420 ms | 50 | **11,535 B** | 109.657 MB |

- **Plateau Verification:** File size between 100 turns (11,339 B) and 10,000 turns (11,535 B) varies by only **196 bytes (1.7%)**.
- **Write Amplification:** Persistence latency is $O(1)$ flat at **~4.6 ms** per message.

---

## 21. CLI / Telegram / WhatsApp Verification

- **Test:** `scratch/audit_step16_18_restart_channels.py` (Step 18)
- **Setup:** 100 turns through CLI, Telegram, and WhatsApp runtime paths.
- **Observed:**
  - CLI session (`cli:local:main`): capped at 20 entries.
  - Telegram session (`telegram:tg_user:chat_123`): capped at 20 entries.
  - WhatsApp session (`whatsapp:62811111:62811111`): capped at 20 entries.
  - Channels maintain strict cross-channel session isolation.
- **Result:** **PASS**.

---

## 22. Memory Interaction

- **Review of `core/memory.py`:**
  - Continuous learning memory (`MemoryStore`) stores user rules from `/learn` and reflection in `.brainfrog/learnings.json`.
  - Session conversational turns are NOT duplicated into `MemoryStore`.
  - In-memory `SessionManager._sessions` bounds each session to `max_history_entries` and `max_history_bytes`.
- **Result:** **PASS**. Memory engine does not leak or duplicate conversational turns.

---

## 23. Security Regression Results

All existing regression tests passed:
- `tests/test_session_history_bounds.py`: 19 tests passed
- `tests/test_approval_process_concurrency.py`: passed
- `tests/test_approval_session_invalidation.py`: passed
- `tests/test_approval_execution_contract.py`: passed
- `tests/test_e2e_remote_approval.py`: passed
- `tests/test_remote_git_push_boundary.py`: passed
- `tests/test_approval_prompt_history_boundary.py`: passed
- `tests/test_target_extraction_security.py`: passed
- `tests/test_git_guard.py`: passed
- `tests/test_e2e_telegram_runtime.py`: passed
- `tests/test_e2e_whatsapp_runtime.py`: passed

**Full Suite Discovery:**
```
Ran 402 tests in 165.721s
OK (skipped=5)
```

---

## 24. Findings

### Finding L01.1-F01: Concurrent Stale Snapshot Lost-Update (High Severity)
- **Description:** `FileSessionStore.save()` protects against torn writes using atomic replacement and lock acquisition during file writing, but does not guard against logical stale snapshot overwrites. When two actors concurrently load the same session, both append turns and save; the second saver completely overwrites and deletes the first saver's committed turns.
- **Reproduction:** Confirmed in `scratch/audit_concurrency_and_lost_update.py`.
- **Recommended Remediation (Future Phase):** Implement optimistic concurrency control via a session `generation: int` counter or `mtime` check in `FileSessionStore.save()`. Reject saves where `generation < disk_generation`, or reload and merge newest turns before flushing.

### Finding L01.1-F02: Stale Save Overwriting `/reset` and Resurrecting Old Incarnation (High Severity — H-02 Boundary)
- **Description:** If a turn is in flight while a user issues `/reset`, `/reset` deletes the session file from disk and allocates a new incarnation ID. When the in-flight turn finishes, `store.save()` blindly writes its pre-reset snapshot back to disk with `session_incarnation_id = old_inc` and the old conversation history. On process restart, the old incarnation is restored, re-exposing approvals that should have been permanently invalidated by H-02.
- **Reproduction:** Confirmed in `scratch/audit_concurrency_and_lost_update.py`.
- **Recommended Remediation (Future Phase):** In `FileSessionStore.save()`, verify that the target file has not been deleted or its incarnation rotated. If `session_file` does not exist or has a newer incarnation on disk, reject the stale save.

---

## 25. Residual Risks

1. **Logical Turn Loss Under Concurrent Channel Pollers:** If multiple worker processes handle incoming messages for the same WhatsApp or Telegram chat concurrently, turns can be lost due to finding L01.1-F01.
2. **Post-Reset Stale Turn Execution:** Slow LLM generation completing after a user types `/reset` can recreate the wiped session file with the pre-reset incarnation due to finding L01.1-F02.

---

## 26. Final Verdict

$$\mathbf{PARTIAL\ —\ L-01\ BOUNDED\ STORAGE\ VERIFIED;}$$
$$\mathbf{RESIDUAL\ CONCURRENCY/INCARNATION\ RACE\ IDENTIFIED}$$

### Verdict Rationale:
- **L-01 Resource Exhaustion Boundary:** **CLOSED & PROVEN**. History entries (50), history bytes (256 KB), message sizes (32 KB), and storage plateau (11.5 KB at 10,000 messages) are deterministic and robust. $O(N^2)$ persistence write amplification is completely eliminated.
- **Concurrency Correctness:** **PARTIAL**. While file writes are structurally atomic, the persistence layer does not guarantee logical read-modify-write serializability (lost update reproduced), and stale in-flight saves can resurrect deleted sessions after `/reset` (incarnation resurrection reproduced).
- In accordance with the prompt instructions ("If a lost update is possible: mark L-01.1 FAIL/PARTIAL. Do not fix it"), the verdict is set to **PARTIAL**.
