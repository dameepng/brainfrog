# BrainFrog Phase 1B: Workspace Audit & Forensic Assessment

**Document ID:** `BF-AUDIT-1B-001`\
**Date:** October 19, 2026\
**Auditor:** Antigravity Agentic Pair Programmer\
**Target Repository:** `dameepng/brainfrog`\
**Target Workspace:** `<REPOSITORY_ROOT>`

---

## 1. Workspace Inventory & Git State

### 1.1 Git Status & Branch Identity
- **Active Branch:** `feat/trustworthy-verification-workflow`
- **Current HEAD Commit:** `c78e7988fbfa9c4a4b6b23355d22be024da0faca`
- **Remote Tracking Branch:** `origin/feat/trustworthy-verification-workflow` (local branch is ahead by 5 commits from publication work)
- **Upstream Merge Target:** `origin/main`

### 1.2 Registered Git Worktrees
| Path | Commit SHA | Branch | Notes |
|---|---|---|---|
| `<REPOSITORY_ROOT>` | `c78e798` | `feat/trustworthy-verification-workflow` | Main active development workspace |
| `<WORKTREES_PARENT>/brainfrog-engineering-skills-pr` | `63afc0d` | `feat/engineering-skills` | PR #43 branch worktree |
| `<WORKTREES_PARENT>/brainfrog-phase5-pilot` | `2d63615` | (detached HEAD) | Phase 5 brownfield pilot workspace |
| `<WORKTREES_PARENT>/brainfrog-publication-worktree` | `d468ae3` | `feat/agent-friendly-architecture-phase1-4` | PR #44 publication worktree |

### 1.3 PR #44 Integration Status
- **PR Number:** #44
- **Title:** `feat: architecture-aware PLAN/BUILD workflow and safe recovery (phases 1–4)`
- **Remote Status:** **`MERGED`** (Merged into `main` upstream)
- **Local Isolation Rule:** PR #44 is strictly untouched and will not be modified or re-opened.

### 1.4 Uncommitted Changes in Development Workspace
- **Modified Tracked Files:**
  - `cli.py`: Integrated `--spec` flag and standalone spec verification routing.
  - `core/runtime/command_policy.py`: Added `py_compile` and `compileall` to allowlist for Python build verification.
- **Untracked Files / Directories:**
  - `core/verifier.py`: Standalone verification engine implementation.
  - `tests/test_standalone_verifier.py`: 20 unit and invariant tests.
  - `docs/evals/verification-first/`: Audit matrices, specifications, and reports.
  - `.brainfrog/reports/`: 16 execution verification reports.

---

## 2. Read-Only Forensic Assessment: Report Directory Cleanup

A critical safety inquiry was raised regarding whether prior automated cleanup logic (e.g. `shutil.rmtree(child)` across `.brainfrog/reports`) inadvertently destroyed historical verification reports.

### 2.1 Forensic Inquiries & Findings

1. **Does `.brainfrog/reports` exist in the workspace?**
   - **Finding:** **YES**. The directory exists at `<REPOSITORY_ROOT>\.brainfrog\reports`.
2. **Which report directories currently remain?**
   - **Finding:** **16 report runs remain intact**, each containing `report.json`, `report.md`, and an `evidence/` directory with `stdout.log`, `stderr.log`, and hashes:
     - `verify_20261019_154715_0e1d88`
     - `verify_20261019_154728_6eff93`
     - `verify_20261019_154737_f8bd2e`
     - `verify_20261019_154740_9c1ccb`
     - `verify_20261019_154743_d152fc`
     - `verify_20261019_154746_3918e7`
     - `verify_20261019_154847_0e1d88` (TASK-01 from experiment)
     - `verify_20261019_154901_6eff93` (TASK-02 from experiment)
     - `verify_20261019_154910_17f7fb` (TASK-03 from experiment)
     - `verify_20261019_154912_f8bd2e` (TASK-04 from experiment)
     - `verify_20261019_154915_9c1ccb` (TASK-05 from experiment)
     - `verify_20261019_154918_d152fc` (TASK-06 from experiment)
     - `verify_20261019_154920_3918e7` (TASK-07 from experiment)
     - `verify_20261019_155333_c69d23` (CLI smoke test 1)
     - `verify_20261019_155404_c8841a` (CLI smoke test 2)
     - `verify_20261019_155502_8ea97f` (CLI smoke test 3)
3. **Are relevant reports tracked, ignored, or untracked?**
   - **Finding:** **Untracked**. Neither committed to Git history (`git log --all -- .brainfrog/reports` is empty) nor listed in `.gitignore`.
4. **Does a reliable before-state inventory exist?**
   - **Finding:** Prior to the Standalone Verification MVP (Stage B), the concept of `.brainfrog/reports` did not exist in BrainFrog. Previous agent proofs were stored in `.brainfrog/proofs/<task_id>.json` via `core/runtime/proof.py`. For the 7 controlled experiment tasks in Stage B, `docs/evals/verification-first/experiment_data.json` acts as an exact before-and-after manifest.
5. **Did broad cleanup actually execute in this workspace?**
   - **Finding:** Transcript inspection confirms that earlier scratch tests (`scratch/test_verifier_manual.py`) only cleaned up `report.report_dir` (the single run directory generated for that specific manual test). There is no record of an unconstrained loop deleting all children in `.brainfrog/reports`.
6. **Can any deleted artifacts be identified?**
   - **Finding:** Zero missing artifacts from documented runs. All 7 runs in `experiment_data.json` exist on disk with valid SHA-256 evidence digests.
7. **Do existing artifacts have backup copies?**
   - **Finding:** Raw metrics and results are preserved in `docs/evals/verification-first/experiment_data.json`.

### 2.2 Forensic Classification

```text
FORENSIC CLASSIFICATION: NO_LOSS_EVIDENCED
```

- **Documented Runs:** **`NO_LOSS_EVIDENCED`**. 100% of generated experiment reports and CLI validation runs are present, readable, and internally consistent.
- **Hypothetical Prior Sessions:** **`INDETERMINATE`**. Because `.brainfrog/reports` was an untracked directory created during Stage B, any ad-hoc reports created by hypothetical earlier sessions outside recorded logs cannot be determined due to the lack of an external commit record.

### 2.3 Required Invariant: Ownership-Safe Cleanup
Even though no data loss occurred, the risk of an unconstrained `rmtree` across `.brainfrog/reports/` is real. In Phase 1B, BrainFrog enforces:
1. **No Broad Cleanup:** Deleting `.brainfrog/reports/` or iterating over its contents to delete children is strictly forbidden.
2. **Explicit Run-ID Ownership:** Cleanup functions and test suites must only delete their own designated temporary directory (preferably allocated via `tempfile.TemporaryDirectory()`).
3. **Collision Resistance:** Runs are written to unique timestamped directories and verified to never overwrite existing runs.
