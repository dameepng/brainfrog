# BrainFrog v0.1.0-rc1 — External Tester Recruitment Discussion Draft

> **Document Status**: Final draft for maintainer review.  
> **Target Forum**: GitHub Discussions (`dameepng/brainfrog`).  
> **Target Category**: `Announcements` (slug: `announcements`).  
> **Target Participants**: 3–5 independent Python developers unfamiliar with BrainFrog.  
> **Source of Truth**: [`docs/external-testing/RECRUITMENT_MESSAGE.md`](RECRUITMENT_MESSAGE.md), [`docs/external-testing/TESTER_GUIDE.md`](TESTER_GUIDE.md), root [`README.md`](../../README.md).

---

## Discussion Metadata

- **Category**: `Announcements` (slug: `announcements`)
- **Title**:
  ```text
  [Call for Testers] BrainFrog v0.1.0-rc1: Offline verification runtime for autonomous code changes (10–15 min test)
  ```

---

## Discussion Markdown Body

Hi everyone,

We are preparing the public beta of **BrainFrog** and are looking for **3–5 Python developers** unfamiliar with the project to test-drive our first release candidate (`v0.1.0-rc1`).

### What is BrainFrog?

BrainFrog is an open-source autonomous coding runtime built around one principle:

> **"Don't just trust the agent. Verify it."**

When an AI agent modifies code in your repository, natural-language promises and synthetic self-reports are not proof. BrainFrog establishes an evidence-backed trust boundary:
- Executes code modifications inside transactional boundaries.
- Verifies changes with real, caller-defined test suites.
- Restores files byte-for-byte upon test failures (`ROLLED_BACK`).
- Produces durable, inspectable proof artifacts (`.brainfrog/proofs/<task_id>.json`) recording exact commands, exit codes, captured stdout/stderr (with secrets redacted), git commit SHAs, and before/after file SHA-256 hashes with byte deltas.
- Detects post-task file tampering or disk drift using live SHA-256 hash checks (`--check-workspace`).

### What We Are Asking For

We want to validate our unassisted "cold-start" developer onboarding across Windows, macOS, and Linux:

- **Time commitment**: 10–15 minutes total.
- **Style**: Self-guided / unassisted. Follow the instructions directly without prior briefing.
- **No provider API keys or cloud accounts required**: Installing Python dependencies may require an internet connection. After installation, the verification runtime and reproducible demo run locally without provider API keys, cloud accounts, telemetry, or external network requests.
- **Prerequisites**: Python 3.10+ (Python 3.10, 3.11, or 3.12) and Git.

### Quickstart Evaluation Steps

1. **Clone the repository and set up a virtual environment**:
   ```bash
   git clone https://github.com/dameepng/brainfrog.git
   cd brainfrog

   # Linux / macOS (bash / zsh)
   python -m venv .venv
   source .venv/bin/activate

   # Windows (PowerShell)
   python -m venv .venv
   # If script execution is blocked: Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
   .\.venv\Scripts\Activate.ps1

   # Windows (cmd.exe)
   python -m venv .venv
   .\.venv\Scripts\activate.bat
   ```

2. **Install dependencies and CLI entrypoint**:
   ```bash
   pip install -r requirements.txt
   pip install -e .
   brainfrog --help
   ```
   *These installation commands may access package indexes and require an internet connection, depending on your environment and package cache.*

3. **Run the reproducible 3-scenario demo (~60 seconds)**:
   ```bash
   python demo/reproducible_proof_demo.py
   ```
   After dependencies are installed, the demo runs locally without provider API keys or external network requests. It uses an isolated temporary workspace and demonstrates:
   - **Scenario 1 (Verified Change)**: Passing tests commit modifications (`COMMITTED`) with proof verdict `VERIFIED` (exit code `0`).
   - **Scenario 2 (Failing Test & Rollback)**: A faulty implementation fails tests, triggers clean atomic rollback (`ROLLED_BACK`), and generates a durable record with proof verdict `FAILED` (exit code `3`).
   - **Scenario 3 (Tamper Detection)**: External modifications to files after proof generation are flagged as `INVALID` (exit code `1`) via live SHA-256 disk checking.

4. **Explore proof evidence with the CLI**:
   ```bash
   # List recorded proofs
   brainfrog proofs

   # Inspect detailed execution and hash evidence
   brainfrog show-proof latest

   # Verify the latest proof against the current workspace
   brainfrog verify-proof latest --check-workspace
   ```
   > **Note on Scenario 3**: The demo's final scenario deliberately tampers with files. The last command can therefore return `INVALID` (exit code `1`) if `latest` refers to that scenario. That is expected for the tamper-detection demonstration, not necessarily a setup failure.

### Exit Code Contract: Understanding `FAILED` vs `INVALID`

| Exit Code | State | Semantic Meaning |
| :---: | :--- | :--- |
| **`0`** | **VERIFIED** | Proof artifact is structurally valid, test exited `0`, transaction committed cleanly, and workspace disk matches post-proof hashes. |
| **`1`** | **INVALID** | Proof JSON is malformed, contradictory, or disk files mutated after proof generation (`--check-workspace` detected hash mismatch). |
| **`2`** | **MISSING** | Proof artifact does not exist in `.brainfrog/proofs/`, or required proof evidence is incomplete. |
| **`3`** | **FAILED** | Proof artifact is structurally sound and truthfully records that the task failed its test suite, timed out, or rolled back mutations. |

> **Key Distinction**:  
> A task that fails tests produces a **valid historical record of failure (`FAILED`, exit code 3)**. The proof artifact itself is intact and truthful. `INVALID` (exit code 1) is reserved for corrupted JSON, contradictory claims, or post-proof disk tampering.

### Honest Early-Release Framing & Boundaries

BrainFrog `v0.1.0-rc1` is an early release candidate:
- **Proof artifacts are plain JSON records**, not hardware-backed cryptographic attestations.
- **Verification depends on test suite depth**: BrainFrog verifies that the specified test command exited `0`; it cannot evaluate whether the test suite provides comprehensive semantic assertions.
- We have not conducted prior external testing cohorts; this is the first open developer validation of the onboarding experience.

### Feedback We Are Looking For

Please reply directly below with your feedback:

1. **Environment**: OS (e.g. Ubuntu 22.04, macOS 14, Windows 11), shell (bash, zsh, PowerShell), and Python version (`python --version`).
2. **Time to First Demo**: Approximately how many minutes did it take from `git clone` to finishing `reproducible_proof_demo.py`?
3. **Setup Friction**: Did virtual environment activation, `pip install`, or CLI entrypoint discovery encounter any hurdles or unexpected behavior?
4. **Proof Comprehension**: Did the distinction between `FAILED` (valid failure record) and `INVALID` (tampered/corrupted proof) make sense from terminal output?
5. **Issues or Surprises**: Any crashes, tracebacks, terminal formatting glitches, or confusing messages?

### Links & Reference

- **Repository**: [https://github.com/dameepng/brainfrog](https://github.com/dameepng/brainfrog)
- **Release Candidate (`v0.1.0-rc1`)**: [https://github.com/dameepng/brainfrog/releases/tag/v0.1.0-rc1](https://github.com/dameepng/brainfrog/releases/tag/v0.1.0-rc1)
- **Target Commit**: `f06a2768e7f84d36da0cbd038affc6c0dacfb715`
- **Root Quickstart Guide**: [https://github.com/dameepng/brainfrog#quickstart-verify-agent-work-in-60-seconds-public-beta](https://github.com/dameepng/brainfrog#quickstart-verify-agent-work-in-60-seconds-public-beta)

Thank you for your time and honest feedback!
