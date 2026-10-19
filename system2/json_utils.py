"""Robust JSON extraction and auto-repair utilities for System 2 LLM outputs.

Handles common LLM JSON syntax quirks:
- Unescaped JSX expressions: `{" "}` -> `{' '}` inside JSON strings
- Trailing commas before closing braces/brackets: `, }` -> `}`
- Invalid backslash escape sequences (e.g., regex `\\d` or Windows paths)
- Unescaped control characters (raw newlines and tabs) via `strict=False`
- Code blocks wrapped in markdown fences (```json ... ```)
- Truncated JSON streams (automatically closing unclosed quotes and braces)
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Set, Tuple


def repair_json_content(text: str) -> str:
    """Apply heuristics to repair common LLM JSON formatting and syntax errors.

    1. Fix JSX whitespace expressions: `{" "}` -> `{' '}`.
       In JSON string values, models frequently insert raw `{" "}` expressions
       without escaping the double quotes. Since `{ "string" }` without a colon
       is never valid JSON syntax, rewriting to single quotes `{'...'}` preserves
       both valid JSX and valid JSON.
    2. Fix trailing commas before `}` or `]`.
    3. Fix invalid backslash escapes (e.g., `\\d` in regex or `C:\\foo` in paths).
    """
    repaired = text

    # Strip byte-order mark (BOM) if present
    if repaired.startswith("\ufeff"):
        repaired = repaired[1:]

    # 1. JSX unescaped quote repair: match `{"..."}` where there is NO colon inside
    repaired = re.sub(r'\{\s*(?<!\\)"([^":\n]*?)(?<!\\)"\s*\}', r"{'\1'}", repaired)

    # 2. Trailing commas before closing brackets or braces
    repaired = re.sub(r",\s*([\]\}])", r"\1", repaired)

    # 3. Invalid escape sequence repair:
    # Valid JSON escapes: \" \\ \/ \b \f \n \r \t \uXXXX
    # Any other backslash is doubled to avoid "Invalid \escape" decode error.
    def _fix_escapes(s: str) -> str:
        return re.sub(r'\\(?![\\"/bfnrt]|u[0-9a-fA-F]{4})', r'\\\\', s)

    repaired = _fix_escapes(repaired)

    return repaired


def attempt_close_truncated_json(text: str) -> str:
    """If JSON was truncated (e.g. hitting token ceiling), attempt to close open quotes and brackets."""
    in_string = False
    escape = False
    stack: List[str] = []

    for char in text:
        if escape:
            escape = False
            continue
        if char == '\\':
            if in_string:
                escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if not in_string:
            if char in '{[':
                stack.append('}' if char == '{' else ']')
            elif char in '}]':
                if stack and stack[-1] == char:
                    stack.pop()

    result = text
    if in_string:
        result += '"'
    while stack:
        result += stack.pop()
    return result


def extract_json(text: str) -> Dict[str, Any]:
    """Pull the first valid {...} JSON object out of a model response with auto-repair.

    Handles free-form thought processes, markdown prose preceding JSON,
    markdown fences (```json ... ```), nested braces, trailing commas,
    unescaped JSX quotes, invalid escape chars, and stream truncation.
    Prioritizes repairing and auto-closing the outermost root JSON object
    before scanning for inner nested objects.
    """
    decoder = json.JSONDecoder(strict=False)
    text = text.strip()

    # Strip reasoning/chain-of-thought blocks if present (e.g. DeepSeek R1, Kimi, GLM reasoning models)
    cleaned_no_think = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.DOTALL).strip()
    if cleaned_no_think:
        text = cleaned_no_think

    # Pass 1: Try markdown fenced json blocks (```json { ... } ``` or ``` { ... } ```)
    fence_pattern = re.compile(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", re.DOTALL)
    for match in fence_pattern.finditer(text):
        content = match.group(1).strip()
        # 1a. Direct raw_decode
        try:
            obj, _ = decoder.raw_decode(content)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

        # 1b. Repaired decode
        try:
            cleaned = repair_json_content(content)
            obj, _ = decoder.raw_decode(cleaned)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

        # 1c. Truncation closed decode
        try:
            closed = attempt_close_truncated_json(repair_json_content(content))
            obj, _ = decoder.raw_decode(closed)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    # Strip code fences if whole text is wrapped in markdown
    cleaned_text = text
    if cleaned_text.startswith("```"):
        lines = cleaned_text.splitlines()
        inner = lines[1:] if lines[-1].strip() == "```" else lines[1:]
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        cleaned_text = "\n".join(inner).strip()

    start = cleaned_text.find("{")
    if start == -1:
        raise ValueError(f"No JSON object found in model output:\n{text[:500]}")

    # Pass 2: Outermost root JSON candidate from first '{'
    outer_candidate = cleaned_text[start:]

    # 2a. Direct raw_decode of outermost candidate
    try:
        obj, _ = decoder.raw_decode(outer_candidate)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # 2b. Repaired outermost candidate
    repaired_outer = repair_json_content(outer_candidate)
    try:
        obj, _ = decoder.raw_decode(repaired_outer)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # 2c. Auto-close truncated outermost candidate
    closed_outer = attempt_close_truncated_json(repaired_outer)
    try:
        obj, _ = decoder.raw_decode(closed_outer)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # Pass 3: Fallback scan for embedded JSON objects in text
    candidates: List[Tuple[int, int, Dict[str, Any]]] = []
    idx = 0
    while True:
        pos = text.find("{", idx)
        if pos == -1:
            break
        try:
            obj, end = decoder.raw_decode(text, pos)
            if isinstance(obj, dict):
                candidates.append((pos, end, obj))
                idx = end
            else:
                idx = pos + 1
        except json.JSONDecodeError:
            idx = pos + 1

    if candidates:
        expected_root_keys: Set[str] = {
            "title", "problem", "goals", "steps", "plan", "body", "answer",
            "tasks", "summary", "diagnosis", "learnings", "markdown_doc",
        }
        # Disqualify or de-prioritize inner step objects: {"id": "...", "description": "...", "files": [...]}
        def _score_candidate(cand: Tuple[int, int, Dict[str, Any]]) -> Tuple[int, int, int]:
            d = cand[2]
            # If it's a step item with list 'files' and 'id', penalize it
            is_step_item = "id" in d and "description" in d and isinstance(d.get("files"), list)
            root_matches = sum(1 for k in expected_root_keys if k in d)
            # For 'files', only count as root key if it is a dict (as in write_code output)
            if "files" in d and isinstance(d["files"], dict):
                root_matches += 2
            penalty = -10 if is_step_item else 0
            return (root_matches + penalty, len(d), cand[1] - cand[0])

        candidates.sort(key=_score_candidate, reverse=True)
        best = candidates[0][2]
        # Only return candidate if it has meaningful structure
        if any(k in best for k in expected_root_keys) or len(best) > 1:
            return best

    # If all passes failed, raise actionable ValueError
    raise ValueError(
        f"Failed to parse valid JSON from model output (auto-repair and auto-close attempted).\n"
        f"Full output (first 800 chars):\n{cleaned_text[:800]}"
    )


# Backward compatibility alias
_extract_json = extract_json
