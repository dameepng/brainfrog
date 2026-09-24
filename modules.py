"""Domain/module registry — the scope gate's vocabulary.

Jev can't read your code. What it CAN do is match a short user prompt
against short domain descriptions you define once, the same way you'd
hand it any other Choice question's criteria.

This module provides:
1. Loading and parsing `modules.json`
2. Auto-detecting tech stacks (Android, Web/Node, Vanilla Web, Python)
3. Auto-generating best-practice `modules.json` for clean/new repositories
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class Domain:
    key: str
    description: str
    paths: List[str]  # path prefixes (files or dirs), relative to repo root
    sensitive: bool = False  # e.g. auth, payments, migrations — never auto-PR


DEFAULT_MAP_FILENAME = "modules.json"

# Standard Best-Practice Presets for common tech stacks
PRESETS: Dict[str, Dict[str, Any]] = {
    "vanilla_web": {
        "frontend": {
            "description": "Vanilla web app, HTML structure, CSS styling, JavaScript DOM logic, CRUD, localStorage, UI components",
            "paths": ["index.html", "style.css", "app.js", "src/"],
            "sensitive": False,
        }
    },
    "android": {
        "ui": {
            "description": "Android UI, Jetpack Compose screens, ViewModels, Activities, UI components, themes, layouts",
            "paths": [
                "app/src/main/java/",
                "app/src/main/kotlin/",
                "app/src/main/res/",
            ],
            "sensitive": False,
        },
        "data": {
            "description": "Database, Room, Repository, API client, Network requests, DAO, Entity, local storage",
            "paths": ["app/src/main/java/", "app/src/main/kotlin/"],
            "sensitive": False,
        },
        "test": {
            "description": "Unit tests, ViewModel tests, repository tests, instrumentation tests",
            "paths": ["app/src/test/", "app/src/androidTest/"],
            "sensitive": False,
        },
    },
    "node_web": {
        "frontend": {
            "description": "Frontend React/Next.js/Vue components, pages, client-side state, styling, templates",
            "paths": ["src/components/", "src/pages/", "src/app/", "public/"],
            "sensitive": False,
        },
        "backend": {
            "description": "API routes, controllers, middleware, database models, server logic",
            "paths": ["src/api/", "server/", "controllers/", "routes/"],
            "sensitive": False,
        },
        "test": {
            "description": "Automated unit and integration tests",
            "paths": ["tests/", "__tests__/"],
            "sensitive": False,
        },
    },
    "python": {
        "core": {
            "description": "Core Python application logic, models, services, algorithms, utilities",
            "paths": ["src/", "app/", "*.py"],
            "sensitive": False,
        },
        "test": {
            "description": "Pytest and unittest test suites",
            "paths": ["tests/", "test_*.py"],
            "sensitive": False,
        },
    },
}


def detect_tech_stack(repo_dir: Path) -> str:
    """Inspect repository markers to deduce tech stack."""
    if (repo_dir / "gradlew").exists() or (repo_dir / "gradlew.bat").exists() or (repo_dir / "app" / "src" / "main").exists():
        return "android"
    if (repo_dir / "package.json").exists():
        return "node_web"
    if (repo_dir / "pyproject.toml").exists() or (repo_dir / "setup.py").exists() or (repo_dir / "requirements.txt").exists():
        return "python"
    if (repo_dir / "index.html").exists() or (repo_dir / "style.css").exists() or (repo_dir / "app.js").exists():
        return "vanilla_web"
    # Check for file extensions in existing files
    for p in repo_dir.glob("*.html"):
        return "vanilla_web"
    for p in repo_dir.glob("*.py"):
        return "python"
    return "unknown"


def infer_stack_from_prompt(prompt: Optional[str]) -> str:
    """Guess tech stack when repository is empty, based on user task description."""
    if not prompt:
        return "vanilla_web"
    lower = prompt.lower()
    if any(k in lower for k in ("android", "kotlin", "compose", "jetpack", "gradle")):
        return "android"
    if any(k in lower for k in ("react", "next.js", "nextjs", "vue", "node", "express", "npm")):
        return "node_web"
    if any(k in lower for k in ("python", "fastapi", "django", "flask", "pytest")):
        return "python"
    if any(k in lower for k in ("html", "css", "js", "javascript", "vanilla", "web", "todo", "frontend", "crud")):
        return "vanilla_web"
    return "vanilla_web"


def generate_preset_modules(stack: str, repo_dir: Path) -> Dict[str, Any]:
    """Generate preset dictionary for the specified stack."""
    preset = PRESETS.get(stack, PRESETS["vanilla_web"]).copy()

    # If Android, try to specialize paths to the actual package path if it exists
    if stack == "android":
        main_java = repo_dir / "app" / "src" / "main" / "java"
        if main_java.exists():
            pkg_dirs = [d for d in main_java.rglob("ui") if d.is_dir()]
            if pkg_dirs:
                rel = str(pkg_dirs[0].relative_to(repo_dir))
                preset["ui"]["paths"] = [f"{rel}/"]
            data_dirs = [d for d in main_java.rglob("data") if d.is_dir()]
            if data_dirs:
                rel = str(data_dirs[0].relative_to(repo_dir))
                preset["data"]["paths"] = [f"{rel}/"]

    return preset


def auto_generate_modules_json(
    repo_dir: Path,
    task: Optional[str] = None,
    stack: Optional[str] = None,
    save_to_disk: bool = True,
) -> Dict[str, Domain]:
    """Automatically deduce, create, and save modules.json for a project."""
    chosen_stack = stack or detect_tech_stack(repo_dir)
    if chosen_stack == "unknown":
        chosen_stack = infer_stack_from_prompt(task)

    spec = generate_preset_modules(chosen_stack, repo_dir)
    target_file = repo_dir / DEFAULT_MAP_FILENAME

    if save_to_disk:
        try:
            target_file.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
            print(f"[brainfrog] 🐸 Auto-generated modules.json for '{chosen_stack}' stack at {target_file.name}")
        except Exception as e:
            print(f"[brainfrog] Warning: Failed to write modules.json: {e}")

    return {
        key: Domain(
            key=key,
            description=entry["description"],
            paths=entry.get("paths", []),
            sensitive=entry.get("sensitive", False),
        )
        for key, entry in spec.items()
    }


def load_module_map(
    repo_dir: Path,
    map_path: Path | None = None,
    task: Optional[str] = None,
    auto_create: bool = True,
) -> Dict[str, Domain]:
    """Load modules.json, or auto-generate best-practice map if missing."""
    path = map_path or (repo_dir / DEFAULT_MAP_FILENAME)
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return {
                key: Domain(
                    key=key,
                    description=entry["description"],
                    paths=entry.get("paths", []),
                    sensitive=entry.get("sensitive", False),
                )
                for key, entry in raw.items()
            }
        except Exception as e:
            print(f"[brainfrog] Warning: Could not parse {path}: {e}")

    if auto_create:
        return auto_generate_modules_json(repo_dir, task=task)

    return auto_discover_modules(repo_dir)


def auto_discover_modules(repo_dir: Path, max_domains: int = 15) -> Dict[str, Domain]:
    """Fallback: one domain per top-level directory."""
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
