# Phase 5A: Pilot Task Specification

**Task ID:** `PILOT-5A-JSON-UTILS-REPAIR`
**Revision:** 1
**Status:** FROZEN
**Target Context:** Brownfield
**Architecture Strategy:** Local Refactor (`local_refactor`)

---

## 1. Engineering Objective

Harden `system2/json_utils.py` to transparently parse and auto-repair common LLM JSON syntax deviations—specifically unquoted Python literals (`True`, `False`, `None`) and JavaScript/C-style single/multi-line comments (`// ...`, `/* ... */`)—while strictly preserving string literal contents and existing auto-repair semantics.

## 2. Motivation & Evidence

Analysis of LLM extraction logs and live reproduction tests demonstrate that models (including Gemini and Claude) occasionally emit:
1. Python boolean/null tokens (`{"flag": True, "details": None}`) instead of JSON tokens (`true`, `null`).
2. Single-line or multi-line comments explaining generated structures (`// next step config`).

Under Python's standard `json.JSONDecoder(strict=False)`, both tokens trigger fatal decode errors. Because `system2/json_utils.py` is the single gateway for all System 2 providers, repairing these deviations directly in `repair_json_content` prevents unnecessary build and plan failures across the entire system.

## 3. Allowed Scope & Boundaries

- **Files Allowed to Change**:
  - `system2/json_utils.py`
  - `tests/test_json_utils.py`
- **Files Explicitly Out of Scope**:
  - All core runtime modules (`core/plans.py`, `core/runtime/*`).
  - Provider clients (`system2/antigravity_client.py`, `system2/claude_client.py`).
  - Security and git guard scanners (`security/*`).
- **Callers to Preserve**:
  - `system2/antigravity_client.py`
  - `system2/claude_client.py`
  - `system2/openai_client.py`
  - `orchestrator.py`

## 4. Invariants & Acceptance Criteria

1. **Python Literal Normalization**:
   - `True` -> `true`
   - `False` -> `false`
   - `None` -> `null`
   - MUST ONLY replace tokens outside of quoted JSON strings. Quoted strings like `"None of the above"` or `"is True"` must remain unaltered.
2. **Comment Stripping**:
   - Single-line comments (`// ...`) outside strings must be removed or replaced with spaces up to newline.
   - Multi-line comments (`/* ... */`) outside strings must be stripped.
   - Quoted URLs (e.g. `"https://example.com"`) must NOT have their `//` stripped.
3. **Backward Compatibility**:
   - All 8 pre-existing tests in `tests/test_json_utils.py` must pass with zero modifications to their assertions.
4. **Verification**:
   - Add new tests in `tests/test_json_utils.py` covering Python literals and comment variations.
   - Run `python -m unittest tests.test_json_utils -v` -> 100% PASS.
   - Run full regression suites -> 100% PASS.

## 5. Rollback Strategy

If regressions or unexpected side-effects occur, revert `system2/json_utils.py` and `tests/test_json_utils.py` to their pre-pilot baseline checksums:
- `system2/json_utils.py`: `3e197e0e0872ff4046132a06d8cafa8094c96330fa8869cb44af91cad5910602`
- `tests/test_json_utils.py`: `9ff862979c504e6c79cd1c61cca0ceb45867304bbe2046f122bb98df24012641`
