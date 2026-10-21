"""Synthetic email validation helper for the BrainFrog browser fixture."""
import re

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def is_valid_email(value: str) -> bool:
    return bool(_EMAIL_RE.match(value or ""))
