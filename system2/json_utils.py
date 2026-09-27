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
    """
    decoder = json.JSONDecoder(strict=False)
    text = text.strip()

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

    # Pass 2: Iterate through all '{' occurrences with raw decode
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
        expected_keys: Set[str] = {
            "files", "steps", "plan", "title", "body", "answer",
            "tasks", "summary", "diagnosis", "learnings",
        }
        for _, _, obj in candidates:
            if any(k in obj for k in expected_keys):
                return obj
        candidates.sort(key=lambda c: len(c[2]), reverse=True)
        return candidates[0][2]

    # Pass 3: Strip code fences if whole text is wrapped in markdown
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

    # Pass 4: Try raw decode from first brace
    try:
        obj, _ = decoder.raw_decode(cleaned_text, start)
        return obj
    except json.JSONDecodeError:
        pass

    # Pass 5: Apply repairs to the substring starting from first '{'
    json_candidate = cleaned_text[start:]
    repaired = repair_json_content(json_candidate)
    try:
        obj, _ = decoder.raw_decode(repaired)
        return obj
    except json.JSONDecodeError:
        pass

    # Pass 6: Try auto-closing truncated JSON on repaired text
    closed = attempt_close_truncated_json(repaired)
    try:
        obj, _ = decoder.raw_decode(closed)
        return obj
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"JSON parse error at position {exc.pos} in model output (auto-repair attempted).\n"
            f"Context: ...{cleaned_text[max(0, exc.pos-80):exc.pos+80]}...\n"
            f"Full output (first 800 chars):\n{cleaned_text[:800]}"
        ) from exc


# Backward compatibility alias
_extract_json = extract_json
