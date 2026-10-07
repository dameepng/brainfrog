# BrainFrog Phase 15B.1 — Security Remediation & Re-Audit Report
**Work Domain Model & State Machine Security Remediation**

**Date:** 2026-10-04  
**Auditor / Agent:** Adversarial Red Team / Security Audit Engine  
**Target Repository:** BrainFrog (`c:\dame-project\tools\agentic_dev`)  
**Previous Baseline:** Phase 15B Audit (`docs/security/phase-15b-audit.md`)  
**Audit Verdict:** **PASS**

---

## 1. Executive Summary

Phase 15B introduced the canonical **Work** domain primitive (`Work`, `WorkStatus`, `ALLOWED_TRANSITIONS`, `WorkStore`, `InMemoryWorkStore`), establishing a decoupled lifecycle state model for tracking user intent from inception through completion.

The Phase 15B red-team audit delivered a verdict of **PASS WITH FINDINGS**, identifying two Low-severity findings:
- **BF-15B-01 (LOW):** `InMemoryWorkStore.save()` did not validate lifecycle transition validity against existing stored records, allowing direct saves of forged objects to bypass intermediate states (e.g., `CREATED -> DONE`).
- **BF-15B-02 (LOW):** `Work.__post_init__` applied credential rejection to `intent` and `goal`, but not to `id`, `scope`, or `plan`, creating an asymmetry where direct in-memory instantiation could hold sensitive tokens.

In Phase 15B.1, both findings were remediated in [`core/runtime/work.py`](file:///c:/dame-project/tools/agentic_dev/core/runtime/work.py) and verified with dedicated test suites in [`tests/test_work.py`](file:///c:/dame-project/tools/agentic_dev/tests/test_work.py):
- **BF-15B-01:** **CLOSED**. `InMemoryWorkStore.save()` now verifies that any status change strictly satisfies `ALLOWED_TRANSITIONS`, and forbids any modification to terminal records (`DONE`, `FAILED`, `CANCELLED`). Rejected attempts leave the store completely uncorrupted.
- **BF-15B-02:** **CLOSED**. `Work.__post_init__` now applies `reject_secrets()` uniformly across all descriptive fields (`id`, `intent`, `goal`, `scope`, `plan`).
- **BF-15B-03 & BF-15B-04:** **ACCEPTED (INFO)**. Architectural defense-in-depth items preserved from Phase 15B baseline.
- **BF-15B-05:** **ACCEPTED (INFO)**. Descriptive scope separator / case normalization observation.

Full test suite execution confirmed **518 tests total** (511 passed, 7 skipped, 0 failures), clean byte-compilation (`compileall`), and clean whitespace/formatting (`git diff --check`).

The overall security verdict is upgraded to **PASS**.

---

## 2. Scope & Target State

### Files Inspected & Modified
- [`core/runtime/work.py`](file:///c:/dame-project/tools/agentic_dev/core/runtime/work.py): Core domain model, state machine, and in-memory store.
- [`tests/test_work.py`](file:///c:/dame-project/tools/agentic_dev/tests/test_work.py): Unit tests, boundary verification, and attack probe suites.
- [`core/runtime/contract.py`](file:///c:/dame-project/tools/agentic_dev/core/runtime/contract.py): `reject_secrets()` implementation.
- [`core/runtime/__init__.py`](file:///c:/dame-project/tools/agentic_dev/core/runtime/__init__.py): Public re-exports.
- [`docs/security/work-domain.md`](file:///c:/dame-project/tools/agentic_dev/docs/security/work-domain.md): Architecture & security boundary specification.
- [`docs/security/phase-15b-audit.md`](file:///c:/dame-project/tools/agentic_dev/docs/security/phase-15b-audit.md): Phase 15B baseline audit report.

### Git Working Tree Status
- Working tree contains uncommitted Phase 15B and 15B.1 changes.
- Zero commits or pushes performed during this remediation/re-audit phase.

---

## 3. Findings Remediation Summary

### BF-15B-01 — Lifecycle Transition Validation in WorkStore.save()
- **Initial Severity:** LOW
- **Status:** **CLOSED**
- **Vulnerability Description:**
  In Phase 15B, `Work.transition()` strictly enforced `ALLOWED_TRANSITIONS`, but `InMemoryWorkStore.save(work)` directly assigned `self._records[work.id] = work`. A caller with direct access to the store could forge a `Work` instance with an arbitrary state transition (such as jumping from `CREATED` directly to `DONE`), bypassing intermediate lifecycle controls.
- **Remediation Implemented:**
  In [`core/runtime/work.py`](file:///c:/dame-project/tools/agentic_dev/core/runtime/work.py#L330), `InMemoryWorkStore.save()` now executes defensive transition validation against `self._records.get(work.id)`:
  1. **Terminal Immutability:** If `existing.is_terminal and existing != work`, raises `InvalidWorkTransition("Cannot modify terminal work record '<id>' in state '<status>'")`.
  2. **Valid State Transitions:** If `existing.status != work.status`, verifies `work.status in ALLOWED_TRANSITIONS.get(existing.status, frozenset())`. If invalid, raises `InvalidWorkTransition("Illegal store transition for work '<id>': '<old>' -> '<new>'")`.
  3. **Non-Terminal Same-State Updates:** Permitted when `existing.status == work.status` and `not existing.is_terminal` (e.g., updating `plan`, `scope`, or timestamps).
  4. **State Integrity on Error:** `self._records` is mutated only *after* all validation passes. On failure, the existing stored record is preserved without corruption.
- **Verification:**
  - `test_store_save_enforces_lifecycle_transitions`: Exhaustively tests 15 transition vectors (allowed vs forbidden transitions across all states), verifying both rejection and store non-corruption.
  - `test_store_save_same_state_update_allowed_for_non_terminal`: Verifies legitimate same-state metadata updates.
  - `test_store_save_cannot_modify_terminal_record_fields`: Verifies that terminal records reject any field mutations.

---

### BF-15B-02 — Uniform Secret Scrubbing in Work.__post_init__
- **Initial Severity:** LOW
- **Status:** **CLOSED**
- **Vulnerability Description:**
  `Work.__post_init__` previously called `reject_secrets({"intent": self.intent, "goal": self.goal})`. A directly instantiated `Work` object could contain sensitive credentials in `id`, `scope`, or `plan` without being rejected at construction time.
- **Remediation Implemented:**
  In [`core/runtime/work.py`](file:///c:/dame-project/tools/agentic_dev/core/runtime/work.py#L131), `Work.__post_init__` now inspects all descriptive and identifier fields:
  ```python
  reject_secrets({
      "id": self.id,
      "intent": self.intent,
      "goal": self.goal,
      "scope": self.scope,
      "plan": self.plan,
  })
  ```
- **Verification:**
  - Expanded `test_secret_scrubbing_rejection` in [`tests/test_work.py`](file:///c:/dame-project/tools/agentic_dev/tests/test_work.py) to assert constructor-level `ValueError` rejection when credential tokens appear in `id`, `intent`, `goal`, `scope`, or `plan`.

---

## 4. State Machine Re-Audit

### Transition Verification & 8 × 8 WorkStatus Matrix

The complete state transition matrix across all 8 `WorkStatus` values was programmatically evaluated against both:
1. **[`Work.transition()`](file:///c:/dame-project/tools/agentic_dev/core/runtime/work.py#L145)** (domain object state transition method)
2. **[`InMemoryWorkStore.save()`](file:///c:/dame-project/tools/agentic_dev/core/runtime/work.py#L330)** (persistence layer state transition enforcement)

```
Total possible transitions:  64  (8 × 8)
Allowed transitions:         11
Rejected transitions:        53
Bypasses detected:            0
```

| From \ To | `CREATED` | `PLANNING` | `APPROVAL_REQUIRED` | `EXECUTING` | `VERIFYING` | `DONE` | `FAILED` | `CANCELLED` |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **`CREATED`** | ❌ (loop) | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ | ❌ |
| **`PLANNING`** | ❌ | ❌ (loop) | ✅ | ❌ | ❌ | ❌ | ✅ | ❌ |
| **`APPROVAL_REQUIRED`** | ❌ | ❌ | ❌ (loop) | ✅ | ❌ | ❌ | ✅ | ✅ |
| **`EXECUTING`** | ❌ | ❌ | ❌ | ❌ (loop) | ✅ | ❌ | ✅ | ❌ |
| **`VERIFYING`** | ❌ | ❌ | ❌ | ❌ | ❌ (loop) | ✅ | ✅ | ❌ |
| **`DONE`** (terminal) | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ (loop) | ❌ | ❌ |
| **`FAILED`** (terminal) | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ (loop) | ❌ |
| **`CANCELLED`** (terminal) | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ (loop) |

### Terminal-State Behavior
Terminal states are strictly final and irreversible:
- `DONE → any` = **REJECTED** (`InvalidWorkTransition`)
- `FAILED → any` = **REJECTED** (`InvalidWorkTransition`)
- `CANCELLED → any` = **REJECTED** (`InvalidWorkTransition`)

**Same-state terminal persistence:**
- Saving an existing terminal record (`existing.is_terminal`) is permitted **if and only if** the operation is strictly idempotent (`existing == work`).
- Any attempt to modify descriptive fields (`plan`, `scope`, `capabilities`, `updated_at`, etc.) of an existing terminal record raises `InvalidWorkTransition("Cannot modify terminal work record ...")`.

---

## 5. Store Integrity Re-Audit

### Adversarial Probing Results
1. **Direct Save Probing (`store.save` bypassing `work.transition`):**
   - *Probe:* Storing `Work(id="w1", status=WorkStatus.CREATED)` followed by `store.save(Work(id="w1", status=WorkStatus.DONE))`.
   - *Result:* `InvalidWorkTransition` raised. Initial `CREATED` record preserved intact.
2. **Terminal Overwrite Probing:**
   - *Probe:* Saving an existing terminal record (`DONE`) with modified `plan` or `status=CREATED`.
   - *Result:* `InvalidWorkTransition` raised. Stored terminal record unchanged.
3. **Idempotent Terminal Save:**
   - *Probe:* Saving an identical terminal record (`store.save(existing_terminal)`).
   - *Result:* Allowed (idempotent no-op).
4. **Cross-ID Probing:**
   - *Probe:* Storing records with distinct IDs (`w1`, `w2`).
   - *Result:* Stores remain strictly isolated; querying `w1` never reflects `w2`. Non-existent queries return `None`.
5. **In-Memory Immutability:**
   - *Probe:* Attempting in-place mutation of fields (`work.status = WorkStatus.DONE`).
   - *Result:* `dataclasses.FrozenInstanceError` raised. Work objects are immutable frozen dataclasses.

---

## 6. Secret Handling Re-Audit

### Field-by-Field Credential Rejection
Every field is validated at initialization and during deserialization against `reject_secrets()`:

| Field | Secret Test Vector | Result |
| :--- | :--- | :--- |
| `id` | `"token_ghp_[REDACTED_PAT]"` | ❌ Rejected (`ValueError`) |
| `id` | `"api_key: sk-[REDACTED_KEY]"` | ❌ Rejected (`ValueError`) |
| `intent` | `"API token = ghp_[REDACTED_PAT]"` | ❌ Rejected (`ValueError`) |
| `intent` | `"password: my_super_secret_password"` | ❌ Rejected (`ValueError`) |
| `goal` | `"api_key: sk-[REDACTED_KEY]"` | ❌ Rejected (`ValueError`) |
| `goal` | `"secret: confidential_credential_value"` | ❌ Rejected (`ValueError`) |
| `scope` | `("password: secret123",)` | ❌ Rejected (`ValueError`) |
| `scope` | `("src/main.py", "access_token: secret12345")` | ❌ Rejected (`ValueError`) |
| `plan` | `("password: supersecret123",)` | ❌ Rejected (`ValueError`) |
| `plan` | `("Step 1", "api_key: sk-[REDACTED_KEY]")` | ❌ Rejected (`ValueError`) |

### Roundtrip Serialization
- `to_dict()` and `from_dict()` cleanly scrub and reject credential injections.
- Serialization preserves strict determinism and type integrity.

---

## 7. Execution Boundary Re-Audit

### Single-Orchestrator Invariant
- **Verification:** An AST and text scan of [`core/runtime/work.py`](file:///c:/dame-project/tools/agentic_dev/core/runtime/work.py) confirms:
  - Zero imports of `subprocess`, `os.system`, `shlex`, `threading`, or execution runners.
  - Zero execution methods, tool invocation loops, or prompt handlers.
  - Zero classes named `WorkOrchestrator`, `WorkExecutor`, or `AgentOrchestrator`.
- **Verdict:** The canonical [`orchestrator.py`](file:///c:/dame-project/tools/agentic_dev/orchestrator.py) remains the sole execution engine in the codebase. Work remains an inert, descriptive lifecycle state record.

---

## 8. Capability Boundary Re-Audit

### Non-Authority of Work.capabilities
- `Work.capabilities` is an optional `Capabilities` dataclass holding descriptive metadata for what capabilities are intended or requested.
- `Work` does **NOT** hold an `ApprovedExecutionContract`.
- `Work` cannot grant filesystem write permissions, network access, or git push privileges.
- Execution authority is granted exclusively by `ApprovedExecutionContract`, which requires cryptographically bound digest verification, session binding, and explicit approval consumption.

---

## 9. Concurrency & Replay Re-Audit

- **State Replay Defense:** Because terminal states cannot transition to any other state, finished or failed work cannot be replayed or re-executed by mutating its status.
- **Idempotency:** Re-saving an identical record in `InMemoryWorkStore` succeeds without side effects.
- **Thread Safety:** `InMemoryWorkStore` operations are serialized via `threading.RLock()`.
- **Cross-Process Notice:** As documented in Phase 15B, `InMemoryWorkStore` is explicitly process-local. Persistent stores in future phases will integrate atomic lockfiles or transactional database primitives.

---

## 10. Test Matrix & Regression Results

| Test Suite | Total Tests | Passed | Skipped | Failed |
| :--- | :---: | :---: | :---: | :---: |
| [`tests/test_work.py`](file:///c:/dame-project/tools/agentic_dev/tests/test_work.py) (Dedicated Work tests) | 39 | 39 | 0 | 0 |
| [`tests/test_capability_contract.py`](file:///c:/dame-project/tools/agentic_dev/tests/test_capability_contract.py) | 30 | 30 | 0 | 0 |
| [`tests/test_approval_execution_contract.py`](file:///c:/dame-project/tools/agentic_dev/tests/test_approval_execution_contract.py) | 27 | 27 | 0 | 0 |
| [`tests/test_remote_git_push_boundary.py`](file:///c:/dame-project/tools/agentic_dev/tests/test_remote_git_push_boundary.py) | 16 | 16 | 0 | 0 |
| Full Test Discovery (`tests/test_*.py`) | **518** | **511** | **7** | **0** |

- **Bytecode Compilation (`compileall`):**
  `python -m compileall -q core security system1 system2 tests cli.py orchestrator.py` exited with code 0 (clean).
- **Whitespace / Git Diff Check:**
  `git diff --check` exited with code 0 (clean).

---

## 11. Remaining Findings Status

| Finding ID | Title | Severity | Status | Assessment |
| :--- | :--- | :---: | :---: | : |
| **BF-15B-01** | `InMemoryWorkStore.save()` lacks transition validation | LOW | **CLOSED** | Fully remediated with transition checks & terminal immutability. |
| **BF-15B-02** | `Work.__post_init__` secret rejection incomplete | LOW | **CLOSED** | Fully remediated across `id`, `intent`, `goal`, `scope`, `plan`. |
| **BF-15B-03** | Unbounded collection and string length in `Work` model | INFO | **ACCEPTED** | Deserialization of very large structures is bounded at network gateways (Phase 14). Model fields remain descriptive. |
| **BF-15B-04** | Permissive timestamp ordering allows negative timestamps or `updated_at < created_at` | INFO | **ACCEPTED** | `created_at` and `updated_at` are descriptive metadata only. They do not participate in authorization, approval expiration, or execution authority (which is governed exclusively by `contract.expires_at`). |
| **BF-15B-05** | Case-sensitivity & separator normalization in descriptive scope | INFO | **ACCEPTED** | `scope` in `Work` represents high-level user intent. Enforcement and canonical normalization occur at the capability boundary via `scope_path()`. |

---

## 12. Security Verdict

```
================================================================================
                    FINAL SECURITY AUDIT VERDICT: PASS
================================================================================
  All security findings (BF-15B-01, BF-15B-02) have been verified closed.
  BF-15B-03 and BF-15B-04 remain accepted INFO defense-in-depth items.
  The Work domain model and state machine strictly enforce lifecycle integrity.
  Work remains completely decoupled from execution authority.
  The single-orchestrator architectural invariant is strictly preserved.
================================================================================
```

---

## 13. Human Review Checklist

- [x] Verify that [`core/runtime/work.py`](file:///c:/dame-project/tools/agentic_dev/core/runtime/work.py) contains no execution logic or second orchestrator.
- [x] Verify that `InMemoryWorkStore.save()` enforces `ALLOWED_TRANSITIONS` and protects terminal states.
- [x] Verify that `Work.__post_init__` scrubs secrets across all descriptive fields.
- [x] Verify that the complete 8 × 8 WorkStatus matrix (64 total, 11 allowed, 53 rejected, 0 bypasses) holds across `Work.transition()` and `InMemoryWorkStore.save()`.
- [x] Verify that terminal states reject all transitions (`DONE → any`, `FAILED → any`, `CANCELLED → any`) and allow only idempotent unchanged re-saves.
- [x] Verify that BF-15B-04 is preserved as descriptive-only timestamp ordering without authorization impact.
- [x] Verify that 518 tests pass with 0 failures and 7 expected skips.
- [x] Verify that `git diff --check` and `compileall` report zero warnings or syntax errors.
- [x] Confirm Phase 15B.1 remediation is complete and ready for human sign-off.
