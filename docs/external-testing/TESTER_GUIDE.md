# BrainFrog Tester Guide (v0.1.0-rc1)

> **"Don't just trust the agent. Verify it."**

Welcome to the BrainFrog Public Beta tester program! This guide outlines the shortest, honest path to testing BrainFrog on your own machine.

BrainFrog is a verification-first autonomous coding runtime. Rather than asking you to blindly trust natural-language agent reports, BrainFrog executes code modifications inside transactional boundaries, verifies changes with real test suites, and generates authoritative, inspectable proof artifacts.

---

## 1. Prerequisites

- **Python**: `3.10+` (tested on Python 3.10, 3.11, and 3.12).
- **Git**: Installed and accessible in your shell.
- **API Keys**: **NONE required**. The entire verification lifecycle, 3-scenario demo, proof inspection, and tampering detection run 100% locally and offline without external network calls or cloud credentials.

---

## 2. Setup (2 Minutes)

### Step 2.1 — Clone the Repository
```bash
git clone https://github.com/dameepng/brainfrog.git
cd brainfrog
```

### Step 2.2 — Create & Activate a Virtual Environment
Choose the commands matching your operating system and shell:

- **Windows (PowerShell)**:
  ```powershell
  python -m venv .venv
  .\.venv\Scripts\Activate.ps1
  ```
  *(If PowerShell blocks script execution, run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` first).*

- **Windows (Command Prompt / cmd.exe)**:
  ```cmd
  python -m venv .venv
  .\.venv\Scripts\activate.bat
  ```

- **Linux / macOS (bash / zsh)**:
  ```bash
  python -m venv .venv
  source .venv/bin/activate
  ```

### Step 2.3 — Install Dependencies & CLI Entrypoint
```bash
pip install -r requirements.txt
pip install -e .
```

### Step 2.4 — Verify CLI Installation
```bash
brainfrog --help
```
You should see the BrainFrog CLI usage options and argument descriptions.

---

## 3. Run the 3-Scenario Offline Demo (60 Seconds)

Execute the self-contained verification walkthrough:

```bash
python demo/reproducible_proof_demo.py
```

This exercises the runtime across three concrete scenarios in an isolated temporary workspace:

| Scenario | What Happens | Expected Verification State | Exit Code |
| :--- | :--- | :---: | :---: |
| **Scenario 1: Verified Change** | Stages `multiply()` in `math_utils.py` and runs `unittest`. Test exits `0`. File modifications commit to disk (`COMMITTED`). | `VERIFIED` | **0** |
| **Scenario 2: Failing Test & Rollback** | Stages a buggy `divide()` returning `0`. Test fails with `AssertionError: 0 != 5`. `TransactionCoordinator` cleanly rolls back mutations to pre-task bytes (`ROLLED_BACK`). | `FAILED` | **3** |
| **Scenario 3: Tamper Detection** | An external edit mutates `math_utils.py` after proof finalization. Live disk hashing flags SHA-256 mismatch against proof record. | `INVALID` | **1** |

---

## 4. Hands-On CLI Evidence Exploration

Run the proof discovery, inspection, and verification commands directly:

### 4.1 Proof Discovery
List all recorded proof artifacts:
```bash
brainfrog proofs
```
*(Alternative syntax: `brainfrog list-proofs`).*

### 4.2 Detailed Proof Inspection
Inspect the full evidential record of the newest task:
```bash
brainfrog show-proof latest
```
This renders:
- Captured process command, duration, and exit code.
- Sanitized stdout and stderr logs.
- Transaction journal (files created/modified, before/after SHA-256 hashes, byte deltas).
- Fresh re-verification diagnostics.

You can also inspect specific task IDs:
```bash
brainfrog show-proof task_demo_01_verified
brainfrog show-proof task_demo_02_failed
```

### 4.3 Deterministic Verification Gate
Test the fast CI/CD verification gate:
```bash
# Internal consistency verification:
brainfrog verify-proof latest

# Verification with live workspace disk hash checking:
brainfrog verify-proof latest --check-workspace
```

---

## 5. Exit Code Contract Reference

BrainFrog commands adhere to a deterministic exit code contract for automated CI/CD gating:

| Exit Code | State | Semantic Meaning |
| :---: | :--- | :--- |
| **`0`** | **VERIFIED** | Proof artifact is structurally valid, test exited `0`, transaction committed cleanly, and workspace disk matches post-proof hashes. |
| **`1`** | **INVALID** | Proof JSON is malformed, contradictory, or disk files mutated after proof generation (`--check-workspace` detected hash mismatch). |
| **`2`** | **MISSING** | Proof artifact does not exist in `.brainfrog/proofs/`, or required proof evidence is incomplete. |
| **`3`** | **FAILED** | Proof artifact is structurally sound and truthfully records that the task failed its test suite, timed out, or rolled back mutations. |

> **Key Distinction (`FAILED` vs `INVALID`)**:
> A task that fails tests produces a **valid historical record of failure (`FAILED`, exit code 3)**. The proof artifact itself is intact and truthful. `INVALID` (exit code 1) is reserved for corrupted JSON, contradictory claims, or post-proof disk tampering.

---

## 6. Cleanup Instructions

The offline demo uses temporary directories that clean up automatically upon completion. To completely remove all test artifacts from your machine:

1. Deactivate the virtual environment:
   ```bash
   deactivate
   ```
2. Remove the repository folder:
   ```bash
   cd ..
   rm -rf brainfrog   # On Windows: Remove-Item -Recurse -Force brainfrog
   ```

---

## 7. Troubleshooting FAQ

- **PowerShell blocks virtualenv activation**:
  Run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in the current terminal window and retry `.\.venv\Scripts\Activate.ps1`.
- **`brainfrog` command not found after `pip install -e .`**:
  Ensure your virtual environment is activated (`which brainfrog` or `Get-Command brainfrog`). Alternatively, invoke directly with `python cli.py <command>`.
- **`verify-proof` reports exit code 3 for Scenario 2**:
  This is expected behavior! Scenario 2 intentionally tests a failing test suite. The proof is a valid record of that failure, so `verify-proof` returns exit code 3 (`FAILED`).
- **Does BrainFrog send telemetry or contact external servers during the demo?**:
  No. The demo and proof verification commands make zero network requests and require no API keys.
