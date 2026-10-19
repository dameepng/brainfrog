#!/usr/bin/env python3
"""Installer script for BrainFrog Git Secret Guard hooks."""
import subprocess
import sys
from pathlib import Path

def _safe_print(msg: str, file=None) -> None:
    target = file or sys.stdout
    try:
        target.write(msg + "\n")
        target.flush()
    except UnicodeEncodeError:
        safe_msg = msg.encode("ascii", errors="replace").decode("ascii")
        target.write(safe_msg + "\n")
        target.flush()

def install_hooks() -> int:
    repo_dir = Path(__file__).resolve().parent.parent
    hooks_dir = repo_dir / ".githooks"
    if not hooks_dir.exists():
        _safe_print("[GitGuard] Error: .githooks directory not found.", file=sys.stderr)
        return 1

    res = subprocess.run(["git", "config", "core.hooksPath", ".githooks"], cwd=repo_dir)
    if res.returncode == 0:
        _safe_print("[GitGuard] Hooks activated successfully:")
        _safe_print("   git config core.hooksPath -> .githooks")
        _safe_print("   * Pre-Commit: scans staged changes for secrets")
        _safe_print("   * Pre-Push:   scans outgoing commit range for secrets")
        return 0

    _safe_print("[GitGuard] Error configuring git core.hooksPath.", file=sys.stderr)
    return 1

if __name__ == "__main__":
    sys.exit(install_hooks())
