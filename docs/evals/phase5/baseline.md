# Phase 5A: Baseline Characterization Report

**Subsystem:** System 2 LLM JSON Extraction & Auto-Repair
**Target Context:** `brownfield`
**Architecture Strategy:** `local_refactor`
**Target Files:**
- `system2/json_utils.py` (SHA-256: `3e197e0e0872ff4046132a06d8cafa8094c96330fa8869cb44af91cad5910602`)
- `tests/test_json_utils.py` (SHA-256: `9ff862979c504e6c79cd1c61cca0ceb45867304bbe2046f122bb98df24012641`)

---

## 1. Existing Architecture & Callers

`system2/json_utils.py` is the central parsing layer for all System 2 structured model responses.
- **Callers**:
  - `system2/antigravity_client.py`: Parses JSON plans, diagnoses, and write_code file diffs from Gemini via Google Antigravity CLI.
  - `system2/claude_client.py`: Parses JSON responses from Anthropic Claude models.
  - `system2/openai_client.py`: Parses JSON responses from OpenAI models.
  - `orchestrator.py`: Extracts structured JSON responses.
  - `tests/test_json_utils.py`: Unit test coverage.

## 2. Public Interfaces & Behavioral Contracts

1. `repair_json_content(text: str) -> str`:
   - Strips UTF-8 BOM (`\ufeff`).
   - Fixes JSX unescaped quotes: `{" "}` -> `{' '}`.
   - Cleans trailing commas before `}` or `]`.
   - Normalizes invalid backslash escapes (e.g., regex `\d` or Windows `\Users`).
2. `attempt_close_truncated_json(text: str) -> str`:
   - Tracks brace/bracket nesting and string quotes to close truncated JSON strings and structures.
3. `extract_json(text: str) -> Dict[str, Any]`:
   - Strips reasoning/thought blocks (`<think>...</think>`).
   - Pass 1: Tries markdown fenced blocks (```json ... ```).
   - Pass 2: Tries outermost root candidate from first `{`.
   - Pass 3: Fallback scan for embedded JSON objects, scoring root objects over child step dictionaries.
   - Returns a `Dict[str, Any]`, or raises `ValueError` if no valid JSON can be recovered.

## 3. Existing Baseline Test Suite

Test Command:
```powershell
python -m unittest tests.test_json_utils -v
```
Results:
- `test_clean_json`: PASSED
- `test_invalid_escape_sequences`: PASSED
- `test_jsx_unescaped_quotes_repair`: PASSED
- `test_jsx_various_expressions`: PASSED
- `test_markdown_fence_extraction`: PASSED
- `test_no_json_raises_value_error`: PASSED
- `test_trailing_commas_and_newlines`: PASSED
- `test_truncated_json_auto_closure`: PASSED

**Total Baseline:** 8 passed, 0 failed, 0 errors in 0.001s.

## 4. Problem Statement & Reproductions

Models generating structured outputs frequently introduce two syntax elements that are invalid standard JSON but intended as data or comments:

1. **Python Literals (`True`, `False`, `None`)**:
   - Repro: `extract_json('{"active": True, "data": None}')`
   - Outcome: `ValueError: Failed to parse valid JSON from model output`
2. **C/JS Style Comments (`// ...`, `/* ... */`)**:
   - Repro: `extract_json('{\n // comment\n "active": true\n}')`
   - Outcome: `ValueError: Failed to parse valid JSON from model output`

Both failure modes cause immediate task aborts in production.

## 5. Invariant Obligations

- Must maintain 100% backward compatibility for all 8 existing unit tests.
- Replacement must not corrupt string literals containing `//`, `/*`, or `True`/`False`/`None` inside valid quoted strings (e.g. `{"url": "https://example.com"}` must NOT strip the `//` in the URL).
- No new external dependencies.
