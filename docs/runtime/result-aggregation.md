# BrainFrog Runtime — Result Aggregation (P1.3F)

## 1. Overview and Core Principle

> **Result aggregation is strictly an OBSERVATION and DATA COMPOSITION layer.**
> **It is NOT an authority layer.**
> **The canonical source of truth for worker outcomes is persisted Child Work state in WorkStore.**

BrainFrog Phase P1.3F introduces deterministic result composition for Parent Works managing delegated Child Works. Result aggregation composes the bounded outcomes of multiple Child Works into an immutable, bounded, secret-safe, and deterministic `AggregateResult`.

### Conceptual Distinction Across Phases

| Phase | Core Question Answered | Authority / Execution Scope |
| :--- | :--- | :--- |
| **P1.3E** | *"Which Child Work is eligible to proceed next?"* | Coordinates eligibility (`CoordinationState`), zero execution authority. |
| **P1.3F** | *"What did the Child Works produce, which succeeded/failed, and what bounded result should the Parent receive?"* | Observes canonical state (`WorkStatus`), composes bounded data, grants zero capabilities. |
| **P1.3G** | *"What happens when workers fail/cancel?"* | Handles cancellation propagation, failure cascades, and parent failure semantics. |

---

## 2. Architecture and Data Flow

Result aggregation fits cleanly into BrainFrog's capability and execution boundary:

```text
       Parent Work
            │
            ├─────────────────┬─────────────────┐
            ▼                 ▼                 ▼
         Child A           Child B           Child C
            │                 │                 │
            └────────────┬────┴─────────────────┘
                         ▼
                 DelegationRuntime (P1.3E)
               (answers: "Who is eligible?")
                         │
                         ▼
                Child Work Execution
                         │
                         ▼
            Canonical Child Work Results
              (persisted in WorkStore)
                         │
                         ▼
               Result Aggregator (P1.3F)
             (answers: "What was produced?")
                         │
                         ▼
               Bounded AggregateResult
               (strictly observational)
                         │
                         ▼
                    Parent Work
               (persisted via OCC in WorkStore)
```

### Invariant: Non-Authority Boundary
`ResultAggregator` and its associated data models (`ChildResult`, `AggregateResult`, `ArtifactReference`) are **strictly data**:
- **Zero execution primitives**: No `subprocess`, `os.system`, shell execution, or network client calls.
- **Zero capability issuance**: Cannot grant, widen, or mutate capabilities.
- **Zero approval mutation**: Cannot create, approve, or consume approvals.
- **Zero transaction mutation**: Cannot initiate transactions or rollback filesystems.
- **Zero LLM planner calls**: Pure deterministic Python logic.
- **Sole execution engine**: Canonical `orchestrator.py` remains the sole execution engine in BrainFrog.

---

## 3. Canonical Source of Truth

The source of truth for child outcomes is **canonical Child Work records stored in `WorkStore`**.

Result aggregation:
- Reads canonical `WorkStatus` (`DONE`, `FAILED`, `CANCELLED`, etc.), `VerificationResult`, `WorkFailure`, and `resume_metadata` directly from persisted records.
- Does **NOT** infer completion from `CoordinationState.CLAIMED` or `CoordinationState.RUNNABLE`. Coordination states describe scheduling eligibility only, not execution outcome.
- Does **NOT** create a separate `ResultStore`, `ChildResultStore`, or `AggregationDatabase`. All results reside canonically in `WorkStore`.

---

## 4. Status Mapping and Semantic Outcomes

Result aggregation maps every canonical `WorkStatus` deterministically:

| `WorkStatus` | Child Status Classification | Child `success` | Result Data Populated |
| :--- | :--- | :--- | :--- |
| `DONE` | Successful terminal child | `True` | `result_summary`, `artifact_references`, `verification_status` |
| `FAILED` | Failed terminal child | `False` | `failure_code`, `failure_message` |
| `CANCELLED` | Cancelled terminal child | `False` | `cancellation_reason` |
| `CREATED` | Incomplete (non-terminal) | `False` | Progress summary (`"In progress (created)"`) |
| `PLANNING` | Incomplete (non-terminal) | `False` | Progress summary (`"In progress (planning)"`) |
| `APPROVAL_REQUIRED` | Incomplete (non-terminal) | `False` | Progress summary (`"In progress (approval_required)"`) |
| `EXECUTING` | Incomplete (non-terminal) | `False` | Progress summary (`"In progress (executing)"`) |
| `VERIFYING` | Incomplete (non-terminal) | `False` | Progress summary (`"In progress (verifying)"`) |

Rules:
1. Success is never invented.
2. `FAILED` is never silently converted to success.
3. `CANCELLED` is never silently converted to success.
4. Active/in-progress work is never treated as complete.

---

## 5. Aggregation Policies

Two explicit policies govern how child outcomes compose into the final `AggregateResult`:

### A. `AggregationPolicy.ALL_REQUIRED`
Every declared child must reach terminal completion and succeed (`WorkStatus.DONE`).
- **All children `DONE`**: `status = COMPLETE`, `success = True`.
- **Any child `FAILED` or `CANCELLED`**: `status = FAILED`, `success = False`.
- **Any child non-terminal**: `status = INCOMPLETE`, `success = False`.

### B. `AggregationPolicy.ALLOW_PARTIAL`
Successful children are captured and recognized even if other sibling children fail or are cancelled.
- **All children `DONE`**: `status = COMPLETE`, `success = True`.
- **Mixed `DONE` and `FAILED`/`CANCELLED`**: `status = PARTIAL`, `success = False`.
- **All children `FAILED` or `CANCELLED`**: `status = FAILED`, `success = False`.
- **Any child non-terminal**: `status = INCOMPLETE`, `success = False`.

Critical invariants:
> **`PARTIAL != COMPLETE`**
> **`FAILED != SUCCESS`**
> Partial success is never allowed to masquerade as complete success.

---

## 6. Incomplete Semantics

When any child is in a non-terminal state (`CREATED`, `PLANNING`, `APPROVAL_REQUIRED`, `EXECUTING`, `VERIFYING`), the aggregate status is unequivocally:

```text
status = AggregateStatus.INCOMPLETE
success = False
```

The aggregator explicitly distinguishes:
- **"All required work finished"** (`completed_children == total_children`)
- **"Some results are currently available while work is in flight"** (`incomplete_children > 0`)

---

## 7. Deterministic Ordering

Aggregation order is **independent of thread completion timing, process scheduling, network delays, or filesystem enumeration**:

1. **Declared Order**: If a `DelegationGroup` is provided, children are aggregated strictly in `delegation_group.child_work_ids` order.
2. **Parent Observational Order**: If aggregating directly from parent Work, children follow `parent_work.resume_metadata["child_work_ids"]`.
3. **Lexicographic Tie-Breaker**: Any untracked or auxiliary children are stably ordered by `child_work_id`.

Each child result receives a stable `declared_order` (0, 1, 2, ...). Repeated aggregation over identical persisted states produces identical child order and identical serialized output.

---

## 8. Parent, Session, and Incarnation Binding

Every child included in an aggregation is strictly validated against the Parent Work identity:
- `child.parent_work_id == parent_work.id`
- `child.actor == parent_work.actor_id`
- `child.session_id == parent_work.session_id`
- `child.session_incarnation_id == parent_work.session_incarnation_id`
- If a `DelegationGroup` is provided, `child.id in delegation_group.child_work_ids`.

If **any** child fails binding validation:
- The aggregator **fails closed** immediately by raising `ResultAggregationBindingError`.
- Foreign or mismatched children from other parents or invalidated session incarnations are never silently aggregated.

---

## 9. Resource Bounds and Payload Limits

To protect against unbounded memory growth and massive payloads:

| Constant | Limit | Behavior on Limit Violation |
| :--- | :--- | :--- |
| `MAX_AGGREGATE_CHILDREN` | 50 | Bounded truncation: includes first 50 results; sets `truncated = True`, `omitted_children = total - 50`. Overall counts and status represent ALL children. |
| `MAX_SUMMARY_BYTES` | 2,048 bytes | Truncates summary with `"... [truncated]"`. |
| `MAX_FAILURE_MESSAGE_BYTES` | 1,024 bytes | Truncates failure and cancellation text with `"... [truncated]"`. |
| `MAX_ARTIFACTS_PER_CHILD` | 20 | Truncates artifact list to maximum 20 references. |
| `MAX_AGGREGATE_BYTES` | 65,536 bytes (64 KB) | Raises `ResultAggregationLimitError` if serialized aggregate exceeds limit. |

Status integrity is preserved: **semantic counts (`total_children`, `successful_children`, `failed_children`) always reflect the complete child set even when the output list is truncated**.

---

## 10. Artifact References

Artifacts produced by Child Works are referenced via the `ArtifactReference` model:

```python
@dataclass(frozen=True)
class ArtifactReference:
    artifact_id: str
    path: str
    mime_type: Optional[str] = None
    digest: Optional[str] = None
    size_bytes: Optional[int] = None
    schema_version: int = 1
```

- **Inert Data Only**: Holds path, identifier, MIME type, and cryptographic digest.
- **Zero Filesystem Authority**: An `ArtifactReference` contains NO handles, file descriptors, or permissions to read, write, or execute files.
- **No Path Traversal**: Rejects `..`, absolute paths, and illegal filesystem characters.

---

## 11. Secret Safety

All result summaries, failure messages, and cancellation reasons are passed through `scrub_secrets()`:
- `sk-proj-...` and similar API keys are redacted to `[REDACTED_API_KEY]`.
- GitHub tokens (`ghp_...`, `github_pat_...`) are redacted to `[REDACTED_TOKEN]`.
- Full PEM private keys are redacted to `[REDACTED_PRIVATE_KEY]`.
- All serialized payloads are checked with `reject_secrets()` to ensure no raw credential fields (`password`, `api_key`, `token`, `secret`) are persisted.

---

## 12. Deterministic Cryptographic Digest

Every `AggregateResult` includes a SHA-256 `aggregate_digest`:
- Calculated over canonical, sorted JSON (`json.dumps(..., sort_keys=True, separators=(',', ':'))`).
- Excludes the volatile `created_at` timestamp.
- **Equivalent input states always produce identical digests**.
- Serves solely as an integrity and identification aid; it is **never** an authorization token.

---

## 13. Idempotence and OCC Parent Integration

### Idempotence
Aggregation is purely observational:
```python
ResultAggregator.aggregate(parent_work, children)
```
Calling `aggregate(...)` repeatedly produces equivalent results with zero side-effects.

### Parent Work Integration via OCC
When Parent Work receives the result, it saves a compact summary into `parent_work.resume_metadata`:

```python
saved_parent = attach_aggregate_to_parent(
    work_store,
    parent_work_id,
    aggregate_result,
    expected_revision=parent_work.revision,
)
```

- **Optimistic Concurrency Control (OCC)**: Conflicting concurrent updates raise canonical `StaleWorkRevisionError`.
- **Bounded Retention**: Only compact summary dictionaries (`to_summary_dict()`) and digest references are retained in the parent Work record, preventing unbounded growth.
- **Canonical Store**: Persisted strictly through `WorkStore.save()`. No secondary database or result cache exists.
