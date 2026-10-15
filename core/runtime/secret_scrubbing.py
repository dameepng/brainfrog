"""Canonical secret redaction shared by contracts and presentation layers."""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

_PRIVATE_KEY_LABELS = frozenset({
    "PRIVATE KEY",
    "ENCRYPTED PRIVATE KEY",
    "RSA PRIVATE KEY",
    "EC PRIVATE KEY",
    "DSA PRIVATE KEY",
    "OPENSSH PRIVATE KEY",
})
_BEGIN_MARKER = re.compile(r"-----BEGIN ([A-Z0-9 ]{1,40})-----", re.IGNORECASE)
_END_MARKER = re.compile(r"-----END ([A-Z0-9 ]{1,40})-----", re.IGNORECASE)
_PLAUSIBLE_KEY_DATA = re.compile(r"(?:[A-Za-z0-9+/]{16,}={0,2}[\r\n\t ]*){1,}")
_MAX_PRIVATE_KEY_SPAN = 65536
_MAX_PRIVATE_KEY_BLOCKS = 32

_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:^|.*[_-])(password|passwd|pwd|api[_-]?key|access[_-]?token|access[_-]?key|auth[_-]?token|refresh[_-]?token|bot[_-]?token|secret|token|cookie|authorization|priv(?:ate)?[_-]?key)(?:[_-].*|$)"
)

_ASSIGNMENT_PATTERN = re.compile(
    r'(?i)(["\']?)\b([a-zA-Z0-9_]*(?:password|passwd|pwd|api[_-]?key|access[_-]?token|access[_-]?key|auth[_-]?token|refresh[_-]?token|bot[_-]?token|secret|token|cookie|authorization)[a-zA-Z0-9_]*)\1\s*([:=])\s*("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|(?:Bearer\s+)?[^\s,;]+)'
)

_SECRET_ENV_VARS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TYPESAFE_API_KEY",
    "WHATSAPP_API_TOKEN",
    "CUSTOM_API_KEY",
    "OPENROUTER_API_KEY",
    "SUMOPOD_API_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_ACCESS_KEY_ID",
)


def is_sensitive_key(key: str) -> bool:
    """Return True if a dictionary or JSON key represents a sensitive secret."""
    if not isinstance(key, str):
        return False
    return bool(_SENSITIVE_KEY_RE.match(key.strip()))


def _scrub_private_keys(text: str, replacement: str = "[REDACTED_PRIVATE_KEY]") -> str:
    """Redact supported PEM blocks without interpreting encryption headers."""
    output = []
    cursor = 0
    search_at = 0
    count = 0

    while count < _MAX_PRIVATE_KEY_BLOCKS:
        begin = _BEGIN_MARKER.search(text, search_at)
        if begin is None:
            break
        label = begin.group(1).upper()
        if label not in _PRIVATE_KEY_LABELS:
            search_at = begin.end()
            continue

        any_end = _END_MARKER.search(
            text, begin.end(), min(len(text), begin.end() + _MAX_PRIVATE_KEY_SPAN)
        )
        if any_end is not None and any_end.group(1).upper() == label:
            block_end = any_end.end()
        elif any_end is not None and any_end.group(1).upper() in _PRIVATE_KEY_LABELS:
            # A mismatched private-key terminator is malformed but still secret.
            block_end = any_end.end()
        else:
            bounded_body = text[begin.end():begin.end() + _MAX_PRIVATE_KEY_SPAN]
            if not _PLAUSIBLE_KEY_DATA.search(bounded_body):
                search_at = begin.end()
                continue
            # A truncated, key-like block fails closed. Its safe boundary is EOF.
            block_end = len(text)

        output.append(text[cursor:begin.start()])
        output.append(replacement)
        cursor = block_end
        search_at = block_end
        count += 1

    if count == _MAX_PRIVATE_KEY_BLOCKS and _BEGIN_MARKER.search(text, search_at):
        output.append(text[cursor:search_at])
        output.append("[REDACTED_PRIVATE_KEY_DATA]")
        return "".join(output)
    output.append(text[cursor:])
    return "".join(output)


def _redact_assignment(match: re.Match, replacement: str) -> str:
    """Redact a single key-value assignment match, preserving quoting and delimiters."""
    q1 = match.group(1)
    key = match.group(2)
    delim = match.group(3)
    val = match.group(4)
    space = " " if delim == ":" else ""

    # Preserve already redacted or truncated tokens to prevent double-redaction mangling
    if val.startswith("[") and ("REDACTED" in val or "TRUNCATED" in val):
        return match.group(0)

    if val.startswith('"') and val.endswith('"') and len(val) >= 2:
        return f'{q1}{key}{q1}{delim}{space}"{replacement}"'
    elif val.startswith("'") and val.endswith("'") and len(val) >= 2:
        return f"{q1}{key}{q1}{delim}{space}'{replacement}'"
    elif val.lower().startswith("bearer "):
        return f"{q1}{key}{q1}{delim}{space}Bearer {replacement}"
    else:
        return f"{q1}{key}{q1}{delim}{space}{replacement}"


def scrub_text_patterns(text: str, replacement: str = "[REDACTED]") -> str:
    """Apply regex and environment-variable secret scrubbing to raw string content."""
    if not text:
        return ""
    scrubbed = text

    # Active sensitive environment variables
    for var in _SECRET_ENV_VARS:
        val = os.environ.get(var)
        if val and len(val) >= 6:
            tag = f"[{var}_REDACTED]" if replacement == "[REDACTED]" else replacement
            scrubbed = scrubbed.replace(val, tag)

    # Replacement tags for specific token patterns
    key_tag = "[REDACTED_API_KEY]" if replacement == "[REDACTED]" else replacement
    bot_tag = "[REDACTED_BOT_TOKEN]" if replacement == "[REDACTED]" else replacement
    tok_tag = "[REDACTED_TOKEN]" if replacement == "[REDACTED]" else replacement

    # API keys & tokens
    scrubbed = re.sub(r"sk-(?:proj-)?[a-zA-Z0-9_\-]{20,}", key_tag, scrubbed)
    scrubbed = re.sub(r"AIza[0-9A-Za-z-_]{30,}", key_tag, scrubbed)
    scrubbed = re.sub(r"\b(?:AKIA|ABIA|ACCA|ASIA)[0-9A-Z]{16}\b", key_tag, scrubbed)
    scrubbed = re.sub(r"\b\d{8,11}:[A-Za-z0-9_-]{30,40}\b", bot_tag, scrubbed)
    scrubbed = re.sub(r"(?:ghp|gho|ghu|ghs|ghr)_[a-zA-Z0-9]{20,}|github_pat_[a-zA-Z0-9_]{20,}", tok_tag, scrubbed)

    # Standalone Bearer tokens
    bearer_rep = f"Bearer {tok_tag}" if replacement == "[REDACTED]" else f"Bearer {replacement}"
    scrubbed = re.sub(r"(?i)(?<![A-Za-z0-9])Bearer\s+[a-zA-Z0-9_\-\.]{8,}\b", bearer_rep, scrubbed)

    # Private key blocks
    priv_tag = "[REDACTED_PRIVATE_KEY]" if replacement == "[REDACTED]" else replacement
    scrubbed = _scrub_private_keys(scrubbed, replacement=priv_tag)

    # Key-value assignments and environment variables
    assign_rep = "[REDACTED]" if replacement == "[REDACTED]" else replacement
    scrubbed = _ASSIGNMENT_PATTERN.sub(lambda m: _redact_assignment(m, assign_rep), scrubbed)

    return scrubbed


def _is_candidate_json(text: str) -> bool:
    stripped = text.strip()
    return (
        (stripped.startswith("{") and stripped.endswith("}"))
        or (stripped.startswith("[") and stripped.endswith("]"))
    )


def _redact_json_structure(data: Any, replacement: str = "[REDACTED]") -> Any:
    """Recursively scrub secrets from parsed JSON data structures."""
    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            if is_sensitive_key(k):
                if isinstance(v, (dict, list)):
                    result[k] = _redact_json_structure(v, replacement)
                else:
                    result[k] = replacement
            else:
                result[k] = _redact_json_structure(v, replacement)
        return result
    elif isinstance(data, list):
        return [_redact_json_structure(item, replacement) for item in data]
    elif isinstance(data, str):
        return scrub_text_patterns(data, replacement)
    else:
        return data


def scrub_secrets(
    text: Optional[str],
    *,
    replacement: str = "[REDACTED]",
    raise_on_error: bool = False,
) -> str:
    """Canonical secret redaction with JSON preservation and fail-closed options."""
    if not text:
        return ""
    try:
        if _is_candidate_json(text):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, (dict, list)):
                    redacted = _redact_json_structure(parsed, replacement)
                    if redacted == parsed:
                        text_scrubbed = scrub_text_patterns(text, replacement)
                        if text_scrubbed == text:
                            return text
                        return text_scrubbed
                    if "\n" in text:
                        return json.dumps(redacted, indent=2)
                    return json.dumps(redacted)
            except Exception:
                # Malformed or non-dict/list JSON; safely fall through to text redaction
                pass
        return scrub_text_patterns(text, replacement)
    except Exception as exc:
        if raise_on_error:
            raise
        return f"[REDACTION_FAILED: {type(exc).__name__}]"


def safe_scrub(text: Optional[str]) -> str:
    """Fail-closed secret scrubber that guarantees never returning raw sensitive text on failure."""
    if not text:
        return ""
    try:
        return scrub_secrets(text, raise_on_error=False)
    except Exception as exc:
        return f"[REDACTION_FAILED: {type(exc).__name__}]"
