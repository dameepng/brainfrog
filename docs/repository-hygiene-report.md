# Repository Hygiene Report

**Date:** 2026-10-04
**Base commit:** `569c004`
**Inventory:** [repository-hygiene-inventory.md](repository-hygiene-inventory.md)

## Cleanup Summary

The local scratch artifacts are gone, and the five root-level Phase 14 reports now live in `docs/security/`. `.gitignore` now covers the runtime state directories. No production code or tests were changed.

## Files Deleted

| Path | Tracked? | Reason |
|---|---|---|
| `scratch/` (32 files + `concurrency_results/`, `__pycache__/`) | No (ignored) | One-off POCs, benchmarks, logs, metrics, and screenshots. Nothing imports them and CI doesn't use them. Their invariants are covered by permanent tests in `tests/`. |
| `tests/.brainfrog/` | No (empty dir) | Leftover from running the tests. See Remaining Local Artifacts. |

## Files Consolidated

| Original | New location |
|---|---|
| `phase14bl01_redteam_report.md` | `docs/security/phase-14/findings.md`, Part A |
| `phase14bl01_post_remediation_redteam_report.md` (was untracked) | `docs/security/phase-14/findings.md`, Part B |
| `phase14bm03_remediation_report.md` | `docs/security/phase-14/remediation.md`, Part A |
| `phase14bl01_remediation_report.md` | `docs/security/phase-14/remediation.md`, Part B |
| `phase14bl01_2_remediation_report.md` | `docs/security/phase-14/remediation.md`, Part C |

- The report bodies were copied **verbatim**, so finding IDs, severities, root causes, evidence, test counts, commits, and closure status are unchanged.
- The only edits are two notes marking historical file paths that no longer exist.
- I also added `docs/security/README.md`. It indexes every finding with its severity, status, fix commit, and regression test file.
- For C-01 through M-02 there were no detailed reports. Their rows come from the fix commit messages only.

## Files Kept

- Every production file: `core/`, `security/`, `system1/`, `system2/`, `cli.py`, `orchestrator.py`, and the root compatibility shims.
- Every test file in `tests/`.
- `docs/repository-hygiene-inventory.md`.

## .gitignore Changes

```
# BrainFrog local runtime state
.brainfrog/approvals/
.brainfrog/sessions/
.brainfrog/scratch/

# Test caches
.pytest_cache/
```

I checked these rules with `git check-ignore -v`:
- The four runtime and cache paths are ignored.
- `.brainfrog/skills/**` and `tests/*.py` are still **not** ignored.

## Production Impact

- `git diff HEAD -- core security system1 system2 cli.py orchestrator.py tests` is empty, so **no production source files or tests changed**.
- Orchestrator, runtime architecture, and every Phase 14 security control are unchanged.

## Test Results

| | Baseline | After |
|---|---|---|
| Collected | 414 | 414 |
| Passed | 409 | 409 |
| Skipped | 5 | 5 |
| Failed | 0 | 0 |

Command: `python -m unittest discover -s tests`

## Security Coverage

No tests were deleted, so security coverage is unchanged. The regression suites for each finding are listed in `docs/security/README.md`.

## Remaining Local Artifacts

- `tests/.brainfrog/`: `tests/test_memory.py` setup creates this directory, and teardown removes only the file inside it. Each test run recreates the empty directory.
  - Git doesn't track empty directories, so it never shows up in `git status`.
  - Per instructions, I left the test unchanged.
- Other ignored local state, intentionally left in place: `.brainfrog/scratch/`, `.brainfrog/approvals/`, `.brainfrog/sessions/`, `__pycache__/`, `.pytest_cache/`, `.venv/`, `brainfrog.egg-info/`, `.env`.
