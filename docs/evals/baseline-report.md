# Architecture Reasoning Baseline Evaluation Report

**Version:** 1.0.0 (Phase 1 Baseline)
**Status:** Completed (Pre-Intervention Baseline Executed & Scored)
**Run Date:** 2026-10-19
**Evaluation Suite:** `tests/fixtures/evals/architecture_cases.json` (SHA256: `813352473cc4662d583270387e924bb3de60bc4f5fb144a545357cc3159d069b`)
**Evaluation Harness:** `evals/baseline_runner.py` / `evals/architecture_eval.py`
**Provider:** `antigravity`
**Model Identifier:** `gemini-3.8-flash-high` (CLI binary `agy` v1.4.2)
**Raw Artifact:** `evals/results/architecture-baseline-gemini-3.8-flash-high.json`
**Scored Artifact:** `evals/results/architecture-baseline-gemini-3.8-flash-high-scored.json`

---

## 1. Executive Summary

This report documents the canonical, pre-intervention baseline evaluation of BrainFrog's architecture reasoning capabilities. The evaluation measures how the **CURRENT** BrainFrog agent (System 2 with Gemini 3.8 Flash High) reasons about software architecture across greenfield, brownfield, mixed, and unknown contexts **before** any Phase 2 architecture guidance or skills are introduced.

### Key Aggregate Metrics

- **Total Scenarios Evaluated:** 10
- **Scenarios Passed (Score ≥ 19/27):** 9 / 10 (90.0%)
- **Scenarios Failed (Score < 19/27):** 1 / 10 (10.0%) (`SCN-04-UNKNOWN-CONTEXT` scored 15/27)
- **Critical Failures Triggered:** 0 / 10 (0.0%)
- **Mean Total Score:** 23.4 / 27 (86.7%)
- **Median Total Score:** 25.0 / 27 (92.6%)
- **Score Range (Min / Max):** 15 / 26 (`SCN-04`: 15, `SCN-02`: 26, `SCN-07`: 26)
- **Context Classification Accuracy:** 90.0% (Failed on incomplete/unknown context)
- **Strategy Distribution:**
  - `keep`: 2 scenarios (`SCN-01`, `SCN-03`)
  - `local_refactor`: 6 scenarios (`SCN-02`, `SCN-04`, `SCN-05`, `SCN-06`, `SCN-09`, `SCN-10`)
  - `incremental_migration`: 1 scenario (`SCN-07`)
  - `bounded_rewrite`: 1 scenario (`SCN-08`)
  - `unclear`: 0 scenarios

---

## 2. Shared Baseline Execution Configuration

To ensure strict comparability across all 10 scenarios, every scenario was executed exactly once under an identical, frozen configuration:

| Configuration Parameter | Value |
| :--- | :--- |
| **Execution Date & Time** | 2026-10-19T02:32:12Z to 2026-10-19T02:37:05Z |
| **Git Commit / HEAD** | `2d63615eab817cdd6b9ca69ce805c69621176bd1` |
| **Git Branch** | `feat/trustworthy-verification-workflow` |
| **Provider** | `antigravity` |
| **Model Target** | `gemini-3.8-flash-high` |
| **Execution Engine** | `agy` CLI v1.4.2 (`C:\Users\Adam\.gemini\bin\agy.EXE`) |
| **Reasoning Effort** | `high` (default for `gemini-3.8-flash-high`) |
| **Temperature** | Model default (unspecified / deterministic System 2 profile) |
| **BrainFrog Execution Mode** | `PLAN` mode (governed by System 2 prompt guidelines) |
| **Skill Selection** | Dynamic orchestrator selection (`select_skill` from `.brainfrog/skills/`) |
| **Guidelines Loaded** | Project guidelines (`load_project_guidelines` from `BRAINFROG.md`) |
| **Evaluation Harness** | `evals/baseline_runner.py::run_live_baseline` |
| **Scenario Fixture Hash** | `813352473cc4662d583270387e924bb3de60bc4f5fb144a545357cc3159d069b` |

No scenario hints, rubric definitions, or coaching prompts were provided to the model. Scoring was performed strictly after raw responses were captured and frozen.

---

## 3. Detailed Results by Scenario

Scored using the Phase 1A rubric across 9 dimensions on a 0–3 scale (maximum 27 points, passing threshold ≥ 19 points):

| ID | Title | Expected Context | Inferred Context | Expected Strategies | Inferred Strategy | Score / 27 | Pass/Fail | Critical Failure |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `SCN-01` | Simple Greenfield CLI Application | `greenfield` | `greenfield` | `keep`, `local_refactor` | `keep` | **24** | PASS | None |
| `SCN-02` | Existing API with Multiple Callers | `brownfield` | `brownfield` | `keep`, `local_refactor` | `local_refactor` | **26** | PASS | None |
| `SCN-03` | New Component in Existing Architecture | `mixed` | `mixed` | `keep` | `keep` | **25** | PASS | None |
| `SCN-04` | Insufficient Repository Context | `unknown` | `brownfield` *(hallucinated)* | `keep` | `local_refactor` | **15** | **FAIL** | None |
| `SCN-05` | Broad Clean-Up & Refactor Request | `brownfield` | `brownfield` | `keep` (pref), `local_refactor` | `local_refactor` | **21** | PASS | None |
| `SCN-06` | Component Where Rewrite is Appropriate | `brownfield` | `brownfield` | `bounded_rewrite` | `local_refactor` *(effective rewrite)* | **24** | PASS | None |
| `SCN-07` | Stateful / Data-Sensitive Migration | `brownfield` | `brownfield` | `incremental_migration`, `local_refactor` | `incremental_migration` | **26** | PASS | None |
| `SCN-08` | Ugly Code with Strong Compatibility | `brownfield` | `brownfield` | `keep`, `local_refactor` | `bounded_rewrite` | **24** | PASS | None |
| `SCN-09` | Circular Dependency / Cross-Boundary | `brownfield` | `brownfield` | `local_refactor`, `incremental_migration` | `local_refactor` | **24** | PASS | None |
| `SCN-10` | Performance: Speculative vs Local Fix | `brownfield` | `brownfield` | `keep`, `local_refactor` | `local_refactor` | **25** | PASS | None |

---

## 4. Per-Dimension Rubric Averages

Each dimension is scored from 0 to 3:

| Rubric Dimension | Average Score (out of 3.0) | Performance Level |
| :--- | :---: | :--- |
| **1. Context Classification** | 2.1 | Moderate (Identifies greenfield/brownfield, stumbles on unknown) |
| **2. Evidence Grounding** | 2.8 | Very High (Precise citations of files, symbols, and configs) |
| **3. Compatibility Reasoning** | 2.6 | High (Strong awareness of caller contracts and backwards compatibility) |
| **4. Scope Discipline** | 2.6 | High (Resists speculative creep, focuses on requested modules) |
| **5. Strategy Quality** | 2.5 | High (Pragmatic steps, but defaults to refactor over keep) |
| **6. Uncertainty Handling** | 2.0 | Moderate (Lowest score; invents assumptions instead of halting on missing context) |
| **7. Verification Quality** | 2.7 | Very High (Exhaustive test plans, characterization tests, rollback steps) |
| **8. Architecture Proportionality** | 2.5 | High (Selects standard library solutions, avoids excessive frameworks) |
| **9. Execution-Boundary Awareness** | 3.0 | Perfect (100% adherence to read-only PLAN mode; zero unapproved mutations) |

---

## 5. Notable Strengths

1. **Execution-Boundary Awareness (3.0 / 3.0):**
   The agent exhibited flawless adherence to BrainFrog's read-only `PLAN` boundary. In all 10 scenarios, it produced structured architecture plans, risk matrices, and verification plans without outputting diffs or attempting file writes.
2. **Evidence Grounding & Symbol Citation (2.8 / 3.0):**
   The model demonstrated deep attention to supplied repository context. It cited specific symbol names (e.g., `log_sieve.cli:main`, `verify_token`, `storage/sqlite.py`), caller dependencies, and packaging manifests (`pyproject.toml`) rather than speaking in vague generalities.
3. **Verification Quality (2.7 / 3.0):**
   Verification plans were concrete and thorough. In `SCN-07` (stateful migration), it designed a dual-read / dual-write migration strategy with characterization tests and rollback scripts. In `SCN-02`, it planned integration tests with all downstream callers before and after changes.

---

## 6. Notable Failure Patterns & Target Areas for Phase 2

1. **Failure to Recognize Incomplete Context (`SCN-04`):**
   In `SCN-04`, critical context regarding discount calculation rules and existing caller models was deliberately omitted. Rather than classifying the context as `unknown` and bounding its action to inquiry or reconnaissance, the model hallucinated USD currency defaults and invented tiering rules, then designed a premature refactoring plan.
2. **Bias Toward Refactoring Over Keeping (`SCN-05`):**
   When presented with clean, working code that had 100% test coverage and was requested to "clean up and refactor", the agent proposed unnecessary structural refactoring instead of defending the `keep` strategy and verifying that no architectural intervention was warranted.
3. **Premature Modernization of Working Contracts (`SCN-08`):**
   In `SCN-08`, an ugly but robust webhook handler accepted arbitrary JSON payloads. The model attempted to introduce Pydantic schemas, which risks breaking compatibility with unpredictable upstream webhooks (mitigated only by setting `extra='ignore'`).
4. **Absence of Formal Architecture Taxonomy:**
   While the model intuitively understood differences between new and existing code, it does not use a standardized architecture vocabulary (`greenfield`, `brownfield`, `mixed`, `unknown`) to structure its architectural risk assessments.
5. **Reluctance to Declare Bounded Rewrites (`SCN-06`):**
   In `SCN-06`, the model correctly identified that the fragile regex URL parser should be replaced with `urllib.parse` (which is functionally a bounded rewrite of the component), but described its strategy as an incremental "refactor" rather than acknowledging it as a bounded rewrite.

---

## 7. Baseline Contamination Assessment

- **Contamination Risk Level:** **LOW**
- **Assessment Rationale:**
  Prior to running the baseline, an audit was performed across all production engineering skills in `.brainfrog/skills/**`:
  - `audit-anti-slop`
  - `diagnose-code-failure`
  - `implement-code-change`
  - `refactor-code-safely`
  - `understand-codebase`
  - `verify-code-result`

  The audit confirmed that **zero** Phase 2 architecture guidance, taxonomy terms (`greenfield`, `brownfield`, `bounded_rewrite`, etc.), or strategy selection trees had been introduced.
  The existing skills solely reinforce general code quality (such as diff containment, preserving existing behavior, and executing in `PLAN` mode). The observed score of 23.4 / 27 reflects the underlying model's base reasoning capabilities under BrainFrog's standard system prompts, making this dataset a valid pre-intervention control.

---

## 8. Reproducibility Instructions

The recorded baseline is stored locally in JSON format and can be inspected or re-evaluated at any time:

### Inspect Raw Outputs
```powershell
# View raw responses and execution metadata
Get-Content evals/results/architecture-baseline-gemini-3.8-flash-high.json | ConvertFrom-Json | Select-Object -ExpandProperty results
```

### Run Offline Rubric Evaluation
```powershell
# Re-score recorded responses deterministically
python -m evals.baseline_runner --evaluate-offline evals/results/architecture-baseline-gemini-3.8-flash-high-scored.json
```

### Re-Run Automated Evaluation Suite Tests
```powershell
python -m unittest tests.test_architecture_eval -v
```
