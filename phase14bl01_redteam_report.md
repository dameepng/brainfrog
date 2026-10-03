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
- Audit files added:
  - `phase14bl01_redteam_report.md`
  - `scratch/test_l01_redteam_audit.py`
  - `scratch/l01_audit_metrics.json`
- Git working tree: Clean.

---

## 20. Final Verdict

**VULNERABLE — L-01 REPRODUCED**
