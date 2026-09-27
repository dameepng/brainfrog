"""Backend selection: System 1 (Jev) decision engine.

BrainFrog uses the real TypeSafe Jev API as its System 1 decision engine.
"""
from __future__ import annotations

import os

from system1 import JevSystemOne, SystemOneClient, TypeSafeSystemOne


def get_system1(backend: str = "jev") -> SystemOneClient:
    """Return the real System 1 (Jev) client.

    Resolves 'jev', 'typesafe', or 'auto' to TypeSafeSystemOne.
    """
    clean = (backend or "jev").lower().strip()
    if clean in ("jev", "typesafe", "auto"):
        return TypeSafeSystemOne()
    if clean == "mock":
        raise ValueError(
            "Mock System 1 has been removed. BrainFrog now uses real Jev (TypeSafe cloud API). "
            "Please ensure TYPESAFE_API_KEY is configured in your .env file."
        )
    raise ValueError(f"Unknown backend: {backend!r} (expected jev | typesafe)")
