"""Backend selection: which System 1 (Jev-shaped) client to use.

This is the ONE place backend choice happens. Everything else
(orchestrator.py) only ever sees the SystemOneClient interface.
"""
from __future__ import annotations

import os

from system1 import MockSystemOne, SystemOneClient, TypeSafeSystemOne


def get_system1(backend: str) -> SystemOneClient:
    if backend == "mock":
        return MockSystemOne()
    if backend == "typesafe":
        return TypeSafeSystemOne()  # reads TYPESAFE_API_KEY from env
    if backend == "auto":
        if os.environ.get("TYPESAFE_API_KEY"):
            return TypeSafeSystemOne()
        print("[config] no TYPESAFE_API_KEY found, falling back to --backend mock")
        return MockSystemOne()
    raise ValueError(f"Unknown backend: {backend!r} (expected mock | typesafe | auto)")
