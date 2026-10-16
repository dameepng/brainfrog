# BrainFrog v0.1.0 Release Candidate Decision Gate

This document governs the evaluation of feedback from external developer testing and establishes strict, objective criteria for release progression.

---

## 1. Principles of Evaluation

To maintain product integrity and safety, evaluations strictly distinguish between **observed facts** and **subjective feedback**:

### Observed Facts (Objective Metrics)
- Command exit codes (must match documented contract: `0`, `1`, `2`, `3`).
- Installation success or failure across supported OS/Python combinations.
- Unhandled exceptions, crashes, or broken tracebacks.
- Time elapsed from `git clone` to successful completion of `demo/reproducible_proof_demo.py`.
- Integrity detection: whether post-proof disk modifications were detected or missed.
- Rollback execution: whether pre-task file content was restored byte-for-byte upon test failure.

### Subjective Feedback (Ergonomic Input)
- Aesthetic preferences on Rich console styling, borders, or colors.
- Personal opinions on CLI flag naming (e.g. `--check-workspace` vs `--verify-disk`).
- Feature requests for integrations (e.g. GitHub Actions native action, IDE plugins).
- Documentation phrasing preferences that do not impact operational correctness.

> **Release Rule**: Release gating decisions are driven by **Observed Facts**. Subjective feedback informs the product backlog (P2/P3 polish) but does not block release unless it reveals widespread conceptual confusion (e.g. > 20% misunderstanding of proof semantics).

---

## 2. Quantitative Gate Thresholds

The release candidate must meet all of the following criteria across 3–5 completed sessions:

| Criterion | Target Metric | Strict Gate Rule |
| :--- | :---: | :--- |
| **Unassisted Completion Rate** | **>= 85%** | Testers must complete Stage 1 (Setup) and Stage 2 (Demo) without facilitator intervention. If >= 2 testers get stuck on installation or setup, the gate fails. |
| **Median Time to First Demo** | **< 5 minutes** | The median setup and demo execution duration must be under 5 minutes on standard hardware with broadband access. |
| **Verification Comprehension** | **>= 80%** | Testers must correctly articulate that a task with failing tests generates a **valid proof record of failure (`FAILED`, exit code 3)** rather than an `INVALID` artifact. |
| **Open P0 Defects** | **0** | Any data-loss, uncontained execution, or security boundary breach immediately fails the release gate. |
| **Open P1 Defects** | **0** | Any reproducible installation failure, incorrect exit code, unhandled crash, or false verification verdict fails the release gate. |

---

## 3. Defect Severity & Action Matrix

| Severity Level | Definition | Mandatory Response |
| :--- | :--- | :--- |
| **P0: Critical Safety / Data Loss** | Destructive git action outside workspace, secret leak, or failure of rollback leaving corrupted files. | **HALT RELEASE IMMEDIATELY**. Produce reproducible test, fix root cause, verify in clean environment, publish `v0.1.0-rc2`, and rerun protocol. |
| **P1: Release-Blocking Correctness** | Pip install failure on supported Python 3.10+, exit code mismatch against contract, false `VERIFIED` on failing tests, or failure to detect disk tampering with `--check-workspace`. | **BLOCK STABLE RELEASE**. Fix defect, add regression test, publish `v0.1.0-rc2`, and request affected testers re-validate. |
| **P2: Usability Friction** | Confusing CLI message, ambiguous documentation step requiring tester pause, or terminal encoding glitch on specific emulator. | Document in release notes / FAQ or apply minor documentation fix. Does not block `rc1` unless frequency exceeds threshold (> 20%). |
| **P3: Minor Polish** | Feature suggestions, cosmetic spacing, or optional convenience flags. | Triage into post-v0.1.0 roadmap. Does not block release. |

---

## 4. Decision Trees & Progression Paths

```
                     [Complete 3–5 Tester Sessions]
                                   │
                                   ▼
                   Were any P0 or P1 defects found?
                                ╱     ╲
                            YES╱       ╲NO
                              ╱         ╲
                             ▼           ▼
                   [BLOCK RELEASE]    Did all Quantitative
                     Fix defect       Metrics pass?
                  Add regression test       ╱     ╲
                     Publish rc2        YES╱       ╲NO
                   Re-run protocol        ╱         ╲
                                         ▼           ▼
                                [READY FOR STABLE] [EXTEND TESTING]
                                 Approve v0.1.0     Triage P2 issues
                                                     Update documentation
                                                     Re-test targeted gaps
```

### Path A: Publish Release Candidate 2 (`v0.1.0-rc2`)
Triggered if:
- A P0 or P1 defect is discovered during external testing.
- Setup or install failure rate exceeds 15% on supported platforms.
- A source code change is required to satisfy verification correctness.

*Procedure*:
1. Commit fix and unit test regression to `main`.
2. Re-run clean clone smoke test and full test suite.
3. Tag and publish `v0.1.0-rc2`.
4. Ask affected testers to re-test the specific fix.

### Path B: Proceed to Stable Public Beta Release (`v0.1.0`)
Triggered if:
- All 3–5 testing sessions are completed.
- Zero open P0 or P1 defects.
- Unassisted completion rate is >= 85%.
- Conceptual comprehension of `FAILED` vs `INVALID` is >= 80%.

*Procedure*:
1. Summarize test logs in `RESULTS_TRACKER.md`.
2. Present evidence-backed sign-off to maintainer.
3. Upon explicit maintainer authorization, tag `v0.1.0` and publish the public beta release.
