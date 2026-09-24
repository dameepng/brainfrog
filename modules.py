"""Domain/module registry — the scope gate's vocabulary.

Jev can't read your code. What it CAN do is match a short user prompt
against short domain descriptions you define once, the same way you'd
hand it any other Choice question's criteria. This file is the loader
for that registry: `modules.json` in your repo (or `--module-map`) maps
a handful of domain names to a description + the paths that domain
covers.

If no module map exists, `auto_discover_modules` falls back to treating
each top-level directory as its own (undescribed, low-precision) domain
so the scope gate still works, just with less signal for Jev to match
against — you'll want to write real descriptions once you see it guess
wrong.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List


@dataclass
class Domain:
    key: str
    description: str
    paths: List[str]  # path prefixes (files or dirs), relative to repo root
    sensitive: bool = False  # e.g. auth, payments, migrations — never auto-PR


DEFAULT_MAP_FILENAME = "modules.json"


def load_module_map(repo_dir: Path, map_path: Path | None = None) -> Dict[str, Domain]:
    path = map_path or (repo_dir / DEFAULT_MAP_FILENAME)
    if path.exists():
        raw = json.loads(path.read_text())
        return {
            key: Domain(
                key=key,
                description=entry["description"],
                paths=entry.get("paths", []),
                sensitive=entry.get("sensitive", False),
            )
            for key, entry in raw.items()
        }
    return auto_discover_modules(repo_dir)


def auto_discover_modules(repo_dir: Path, max_domains: int = 15) -> Dict[str, Domain]:
    """Fallback: one domain per top-level directory, no real description.

    This still lets the scope gate run out of the box, but Jev is matching
    against bare directory names instead of real descriptions — expect
    lower confidence and more clarification round-trips until you write
    a proper modules.json.
    """
    ignore = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
    domains: Dict[str, Domain] = {}
    for p in sorted(repo_dir.iterdir()):
        if not p.is_dir() or p.name in ignore or p.name.startswith("."):
            continue
        domains[p.name] = Domain(
            key=p.name,
            description=f"Code under the '{p.name}/' directory.",
            paths=[p.name + "/"],
        )
        if len(domains) >= max_domains:
            break
    return domains


def resolve_focus_tree(repo_dir: Path, domain: Domain, max_files: int = 200) -> str:
    """A file listing scoped to one domain's paths, instead of the whole repo."""
    files: List[str] = []
    for prefix in domain.paths:
        target = repo_dir / prefix
        if target.is_file():
            files.append(prefix)
            continue
        if target.is_dir():
            for p in sorted(target.rglob("*")):
                if p.is_file() and not any(part.startswith(".") for part in p.parts):
                    files.append(str(p.relative_to(repo_dir)))
                if len(files) >= max_files:
                    break
    return "\n".join(files)


def write_example_module_map(path: Path) -> None:
    example = {
        "auth": {
            "description": "Login, sessions, password reset, OAuth, tokens, permissions.",
            "paths": ["app/auth/", "app/middleware/session.py"],
            "sensitive": True,
        },
        "billing": {
            "description": "Payments, invoices, subscriptions, refunds, pricing.",
            "paths": ["app/billing/"],
            "sensitive": True,
        },
        "notifications": {
            "description": "Email, push, and in-app notifications.",
            "paths": ["app/notifications/"],
            "sensitive": False,
        },
    }
    path.write_text(json.dumps(example, indent=2) + "\n")
