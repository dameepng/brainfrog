"""Canonical secret redaction shared by contracts and presentation layers."""
from __future__ import annotations

import re

from .session import scrub_secrets as _scrub_existing_secrets

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


def _scrub_private_keys(text: str) -> str:
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
        output.append("[REDACTED_PRIVATE_KEY]")
        cursor = block_end
        search_at = block_end
        count += 1

    if count == _MAX_PRIVATE_KEY_BLOCKS and _BEGIN_MARKER.search(text, search_at):
        output.append(text[cursor:search_at])
        output.append("[REDACTED_PRIVATE_KEY_DATA]")
        return "".join(output)
    output.append(text[cursor:])
    return "".join(output)


def scrub_secrets(text: str) -> str:
    """Redact canonical credential forms, including complete private-key blocks."""
    if not text:
        return text
    scrubbed = _scrub_existing_secrets(text)
    return _scrub_private_keys(scrubbed)
