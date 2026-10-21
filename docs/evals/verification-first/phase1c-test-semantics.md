# BrainFrog Verification-First Phase 1C: Test Result Semantics & False-Pass Prevention

**Date:** 2026-10-20\
**Component:** `TestResultAdapter`

---

## 1. The Zero-Test False-Pass Problem

A standard Python `unittest` command executed against an empty test directory or unmatched pattern outputs:

```text
Ran 0 tests in 0.000s

OK
```
with process exit code `0`.

In naive CI/verification systems, an exit code of `0` is mistakenly treated as `PASS`. In a verification engine, **claiming that an external coding agent satisfied a criterion when zero tests were executed is a fatal correctness defect.**

---

## 2. TestResultAdapter Verdict Rules

| Command Category | Observed Output Pattern | Process Exit Code | Derived Verdict | Status Reason |
|---|---|---|---|---|
| `python -m unittest` | `Ran 0 tests in ...` | `0` | **BLOCKED** | Zero tests discovered or executed (preventing false pass) |
| `python -m unittest` | `Ran N tests in ... OK` (N > 0) | `0` | **PASS** | `test_count = N, failure_count = 0` |
| `python -m unittest` | `FAILED (failures=F, errors=E)` | Non-zero | **FAIL** | `test_count = N, failure_count = F + E` |
| `python -m compileall` | Successful compile output | `0` | **PASS** | Syntax validation succeeded |
| `python -m compileall` | SyntaxError / IndentationError | Non-zero | **FAIL** | Compilation error detected during compileall |
| Generic / Unrecognized | No recognized runner summary | `0` | **PASS** | Exit code 0, test count unverified (`test_count = None`) |
| Any Command | Process killed by timeout | Signal / 124 / 1 | **BLOCKED** | Execution timed out after threshold |

---

## 3. Machine-Readable Test Counts

`TestResultAdapter` parses `stdout` and `stderr` using bounded regular expressions:
- Extracts executed test count (`test_count`).
- Extracts test failure count (`test_failure_count`).
- Attaches counts to `CriterionResult` in both JSON and Markdown evidence artifacts.
- Renders counts directly in terminal summaries: e.g. `AC-01 PASS Unit tests (5 executed)`.
