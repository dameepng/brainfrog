# BrainFrog Proof UX: Inspection Interface and Verification Evidence

## Product Thesis

> **"Don't just trust the agent. Verify it."**

When an autonomous agent makes changes to code, developers must not rely on conversational self-reports or synthetic model assertions. BrainFrog persists authoritative verification evidence into durable, inspectable proof artifacts. The proof UX allows developers, team members, and CI pipelines to inspect:

* Exactly which files were created, modified, or deleted.
* Before and after SHA-256 hashes and byte count deltas.
* Which test command executed and its exact exit code.
* Captured process stdout/stderr with secrets redacted.
* Transaction status (`COMMITTED` vs `ROLLED_BACK`).
* Fresh deterministic re-verification and workspace disk integrity checks.

---

## 1. CLI Commands

### Detailed Proof Inspection (`show-proof`)
Full two-section inspection report displaying historical evidence and live state re-verification:
```bash
brainfrog show-proof <task_id>
# Or passing path to proof JSON:
brainfrog show-proof .brainfrog/proofs/<task_id>.json
# With workspace disk integrity check:
brainfrog show-proof <task_id> --check-workspace
```

### Deterministic Proof Verification Gate (`verify-proof`)
Fast deterministic verification evaluating structural integrity, gate invariants, and recorded task outcome:
```bash
brainfrog verify-proof <task_id>
# Or with workspace disk integrity check:
brainfrog verify-proof <task_id> --check-workspace
# Alternative flag syntax:
brainfrog --verify-proof <task_id> [--check-workspace]
```

### REPL Slash Commands
Inside the interactive REPL:
```text
/show-proof <task_id> [--check-workspace]
/proof <task_id>
/verify-proof <task_id> [--check-workspace]
```

---

## 2. Process Exit Codes

`brainfrog show-proof` and `brainfrog verify-proof` share the **identical canonical exit code contract** to enable reliable scripting and automated CI gating:

| Exit Code | State | Description |
|---|---|---|
| **0** | **VERIFIED** | Proof artifact is structurally valid, all verification gate invariants passed, process exited 0, transaction committed cleanly, and workspace disk matches post-proof hashes (when `--check-workspace` is specified). |
| **1** | **INVALID** | Proof JSON is malformed, contradictory (e.g. claimed VERIFIED with exit code 1), tampered, unknown/missing task verdict, or workspace disk integrity verification failed (`--check-workspace` detected modified/deleted files). |
| **2** | **INCOMPLETE / MISSING** | The requested proof artifact does not exist on disk, or required verification evidence was not captured. |
| **3** | **FAILED** | Proof artifact is valid and truthfully records that the task failed its test suite, timed out, was cancelled, or rolled back mutations. |

> **Trust Invariant (Sprint 1.4B)**: Structural validity of a proof artifact is never presented as successful task verification. When a task failed verification criteria, its proof artifact is a structurally valid historical record of that failure. Both `show-proof` and `verify-proof` return exit code **3** (`FAILED`) and clearly label the outcome as `FAILED`, explaining that the historical failure record itself is valid.

---

## 3. Clear Evidential Separation

The inspection report clearly separates historical execution data from fresh live state analysis:

### Section 1: Stored Proof Artifact Data (Historical Record)
Displays immutable facts recorded in `.brainfrog/proofs/<task_id>.json`:
* **Task Identity**: Canonical task ID, session ID, description, duration.
* **Stored Verdict**: `VERIFIED` or `FAILED` with authoritative reason.
* **Transaction Journal**: Transaction ID, status (`COMMITTED` or `ROLLED_BACK`), list of modified files with before/after SHA-256 hashes and byte count deltas.
* **Execution Evidence**: Test command, process exit code, duration, timeout/cancellation state, gate checks.
* **Captured Output**: Bounded stdout/stderr with secrets automatically redacted.
* **Git Provenance**: Repository root, branch, initial HEAD SHA, final HEAD SHA, task commit SHA.

### Section 2: Fresh Deterministic Re-verification (Live State Analysis)
Evaluates the stored proof against current workspace invariants:
* **Live Verifier Status**: Re-evaluates structural validity, gate checks, and cross-field consistency (`VALID`, `INVALID`, or `INCOMPLETE`).
* **Workspace Disk Integrity**: When `--check-workspace` is provided, recomputes SHA-256 hashes of files currently on disk to ensure they have not mutated since the proof was generated (`MATCH` vs `MISMATCH`).

### Section 3: Authenticity Boundary Notice
Explicitly states the trust boundary:
> *Notice: Stored proof artifacts are unauthenticated historical JSON records. BrainFrog evaluates structural validity, gate checks, and SHA-256 disk consistency. It does NOT assert cryptographic non-repudiation or hardware-signed attestation.*

---

## 4. Reproducible Demo Walkthrough

A complete, self-contained walkthrough script is provided at `demo/reproducible_proof_demo.py`. It runs three concrete scenarios in an isolated workspace using real Python test suites:

### Scenario 1: Successful Change with Real Test Suite (`VERIFIED`)
1. Stages `multiply` function in `math_utils.py`.
2. Executes real test command: `python -m unittest tests/test_math_utils.py`.
3. Test exits 0 -> Transaction commits -> Proof artifact saved.
4. `brainfrog show-proof task_demo_01_verified`:
   * Displays `VERIFIED` (green).
   * Exit Code: `0`.

### Scenario 2: Failing Test Suite with Truthful Rollback (`FAILED`)
1. Stages faulty `divide` returning `0`.
2. Executes real test command -> `AssertionError: 0 != 5` -> Exit Code: `1`.
3. Verification fails -> Transaction cleanly rolls back filesystem mutations.
4. Proof artifact saved recording truthful failure facts.
5. `brainfrog show-proof task_demo_02_failed`:
   * Displays `FAILED` (red).
   * Exit Code: `3`.
   * Stored Transaction: `ROLLED_BACK`.

### Scenario 3: Post-Proof Workspace Tampering Detected (`INVALID`)
1. An external editor alters `math_utils.py` after task 1 proof was committed.
2. Run inspection with disk integrity checking:
   ```bash
   brainfrog show-proof task_demo_01_verified --check-workspace
   ```
3. Live verifier detects SHA-256 mismatch:
   * Displays `INVALID` (red).
   * Workspace Disk Integrity: `✗ MISMATCH`.
   * Exit Code: `1`.

To run the demo:
```bash
python demo/reproducible_proof_demo.py
```

---

## 5. Security and Privacy Guarantees

* **Secret Scrubbing**: All process outputs, error messages, and descriptions are filtered through `redact_secrets()` before display. GitHub tokens (`ghp_...`), OpenAI/Anthropic API keys (`sk-...`), Google API keys (`AIza...`), Bearer tokens, and passwords are replaced with `[REDACTED_SECRET]`.
* **Sensitive File Protection**: Files matching patterns such as `.env`, `*.pem`, `*.key`, `*id_rsa*` are flagged as protected. File contents are never dumped to stdout.
* **Workspace Path Containment**: Traversal paths containing `..` or root slashes outside the workspace boundary are rejected.
* **Read-Only Inspection**: `show-proof` is strictly read-only. It never modifies, deletes, or creates workspace files.

---

## 6. Known Limitations

1. **Unsigned Artifacts**: Proof files are standard JSON documents. Without asymmetric cryptographic signing (e.g. Ed25519), an adversary with local filesystem write permissions can tamper with files offline. Proof verification confirms evidential consistency, not cryptographic non-repudiation.
2. **Caller Contract Dependency**: If a caller configures a no-op command (e.g. `python -c pass`), the verification gate validates process exit code 0; it does not perform semantic coverage analysis of test depth.
