# BrainFrog v0.1.0-rc1 — Public Release Candidate

> **"Don't just trust the agent. Verify it."**

BrainFrog is a verification-first autonomous coding tool. When an AI agent modifies code in your repository, natural-language promises and synthetic self-reports are not proof. BrainFrog establishes an evidence-backed trust boundary: every code change produces an authoritative, durable proof artifact recording exact test commands, process exit codes, captured stdout/stderr (with secrets automatically redacted), git commit SHAs, and before/after file SHA-256 hashes with byte deltas.

---

### Target Commit
`f06a2768e7f84d36da0cbd038affc6c0dacfb715`

---

### Offline Quickstart (Zero API Keys Required)

Clone the repository and run the reproducible three-scenario demo without network access or credentials:

```bash
git clone https://github.com/dameepng/brainfrog.git
cd brainfrog
```

#### Virtual Environment Setup
- **Windows (PowerShell)**:
  ```powershell
  python -m venv .venv
  .\.venv\Scripts\Activate.ps1
  ```
- **Windows (cmd.exe)**:
  ```cmd
  python -m venv .venv
  .\.venv\Scripts\activate.bat
  ```
- **POSIX Shells (Linux / macOS)**:
  ```bash
  python -m venv .venv
  source .venv/bin/activate
  ```

#### Install Dependencies & Entrypoint
```bash
pip install -r requirements.txt
pip install -e .
```

#### Run the 3-Scenario Offline Verification Walkthrough
```bash
python demo/reproducible_proof_demo.py
```

This exercises the canonical runtime pipeline through three real scenarios:
1. **Scenario 1 — Verified Change (`VERIFIED`, Exit Code 0)**: Passing tests commit transaction and produce a durable proof artifact.
2. **Scenario 2 — Failing Test Suite with Truthful Rollback (`FAILED`, Proof Exit Code 3)**: Failing tests cleanly trigger atomic rollback, restoring pre-task workspace content byte-for-byte while saving a durable historical failure record.
3. **Scenario 3 — Post-Proof Workspace Tampering Detected (`INVALID`, Exit Code 1)**: Modifying files on disk after proof generation is flagged by live SHA-256 hash checking.

#### Inspect & Verify Proofs via CLI
```bash
# Discovery: list recorded proof artifacts
brainfrog proofs

# Detailed inspection: examine historical execution data & live state
brainfrog show-proof latest

# Fast deterministic verification gate with live disk integrity check
brainfrog verify-proof latest --check-workspace
```

---

### Verified Capabilities

- **Authoritative Proof Artifacts (`.brainfrog/proofs/<task_id>.json`)**: Durable execution records capturing exact commands, exit codes, git commit SHAs, and before/after file SHA-256 hashes.
- **Safe Atomic Rollback**: When a test fails or an operation errors, filesystem modifications are rolled back byte-for-byte to their pre-task baseline, preserving durable failure proofs without leaving broken code in the working tree.
- **Live Workspace Integrity Gating (`--check-workspace`)**: Actively verifies on-disk file hashes against post-proof records to catch drift or unauthorized manual edits after task completion.
- **Canonical Exit Code Contract**: Machine-readable exit codes designed for automated CI/CD gating:
  - `0`: `VERIFIED` — Proof artifact is structurally valid, test exited 0, transaction committed cleanly, and workspace disk matches post-proof hashes.
  - `1`: `INVALID` — Proof JSON is malformed, contradictory, or disk files have mutated since proof generation (`--check-workspace` detected hash mismatch).
  - `2`: `MISSING` — Proof artifact does not exist in `.brainfrog/proofs/`, or required proof evidence is incomplete.
  - `3`: `FAILED` — Proof artifact is structurally valid and truthfully records that the task failed its test suite, timed out, or rolled back mutations.

---

### Disclosures & Known Limitations

1. **Unsigned JSON Proofs**: Proof records are plain JSON files. BrainFrog asserts internal consistency and cryptographic hash integrity of workspace files, not hardware-backed cryptographic attestation or non-repudiation.
2. **Dependence on Test Suite Quality**: Verification guarantees that the specified test command exited with code `0`. It cannot evaluate whether the caller's test suite provides comprehensive semantic assertions.
