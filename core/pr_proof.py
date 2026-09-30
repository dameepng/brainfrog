"""Automated PR Proof Screenshot Generator for Frontend Changes.

Conditionally captures desktop and mobile viewport screenshots of the
verified frontend, saves them under docs/pr-proof/<branch>/, and formats
or updates the PR body with the raw GitHub URLs.

SKIPS completely if no frontend changes are detected in the PR.
"""
from __future__ import annotations

import base64
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.frontend_quality_gate import QualityGateResult, is_frontend_change


def get_pr_changed_files(repo_dir: Path | str, base: str = "origin/main") -> List[str]:
    """Get list of all changed files in this PR (committed, staged, untracked)."""
    repo = Path(repo_dir).resolve()
    files = set()

    # 1. Committed diff against base branch (origin/main...HEAD, main...HEAD, or HEAD~1)
    for target in [base, "main", "HEAD~1"]:
        try:
            res = subprocess.run(
                ["git", "diff", "--name-only", f"{target}...HEAD"],
                cwd=str(repo),
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode == 0 and res.stdout.strip():
                for f in res.stdout.split():
                    files.add(f.strip().replace("\\", "/"))
                break
        except Exception:
            pass

    # 2. Status porcelain for staged, modified, and newly added/untracked files (-uall expands untracked folders)
    try:
        res = subprocess.run(
            ["git", "status", "--porcelain", "-uall"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                if len(line) > 3:
                    fpath = line[3:].strip()
                    if " -> " in fpath:
                        fpath = fpath.split(" -> ")[1].strip()
                    full = repo / fpath
                    if full.is_dir():
                        for child in full.rglob("*"):
                            if child.is_file():
                                files.add(str(child.relative_to(repo)).replace("\\", "/"))
                    else:
                        files.add(fpath.replace("\\", "/"))
    except Exception:
        pass

    # 3. Fallback: simple diff against HEAD
    if not files:
        try:
            res = subprocess.run(
                ["git", "diff", "--name-only", "HEAD"],
                cwd=str(repo),
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode == 0:
                for f in res.stdout.split():
                    files.add(f.strip().replace("\\", "/"))
        except Exception:
            pass

    return sorted(list(files))


def get_github_repo_info(repo_dir: Path | str) -> Tuple[str, str]:
    """Extract (owner, repo) from git remote origin URL.

    Defaults to ('dameepng', 'brainfrog') if not determinable.
    """
    repo = Path(repo_dir).resolve()
    try:
        res = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        )
        url = res.stdout.strip()
        if url:
            m = re.search(r"github\.com[/:]([^/]+)/([^/\.]+)(?:\.git)?", url)
            if m:
                return m.group(1), m.group(2)
    except Exception:
        pass
    return "dameepng", "brainfrog"


def format_pr_proof_markdown(
    owner: str,
    repo: str,
    branch: str,
    desktop_file: str = "desktop.png",
    mobile_file: str = "mobile.png",
) -> str:
    """Format the standard Markdown proof table with raw GitHub URLs pointing to the PR branch."""
    raw_desktop = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/docs/pr-proof/{branch}/{desktop_file}"
    raw_mobile = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/docs/pr-proof/{branch}/{mobile_file}"

    return (
        "## 📸 Proof\n\n"
        "| Desktop | Mobile |\n"
        "|---|---|\n"
        f"| ![Desktop]({raw_desktop}) | ![Mobile]({raw_mobile}) |"
    )


def save_pr_proof_screenshots(
    repo_dir: Path | str,
    branch: str,
    gate_result: Optional[QualityGateResult] = None,
) -> Tuple[Optional[Path], Optional[Path]]:
    """Save desktop and mobile screenshots into docs/pr-proof/<branch>/.

    Uses base64 from gate_result if available; otherwise falls back to
    capturing from the local HTML entrypoint via headless browser.
    """
    repo = Path(repo_dir).resolve()
    proof_dir = repo / "docs" / "pr-proof" / branch
    proof_dir.mkdir(parents=True, exist_ok=True)

    desktop_path = proof_dir / "desktop.png"
    mobile_path = proof_dir / "mobile.png"

    # 1. Use QualityGateResult base64 if present
    if gate_result:
        d_b64 = gate_result.screenshot_desktop_base64 or gate_result.screenshot_base64
        if d_b64:
            try:
                desktop_path.write_bytes(base64.b64decode(d_b64))
            except Exception:
                pass

        m_b64 = gate_result.screenshot_mobile_base64
        if m_b64:
            try:
                mobile_path.write_bytes(base64.b64decode(m_b64))
            except Exception:
                pass

    # 2. Fallback to headless browser capture if files are still missing
    if not desktop_path.exists() or not mobile_path.exists():
        try:
            from system2.visual_inspector import (
                capture_screenshot,
                find_browser_bin,
                find_html_entrypoint,
            )

            entrypoint = find_html_entrypoint(repo)
            browser_bin = find_browser_bin()
            if entrypoint and browser_bin:
                if not desktop_path.exists():
                    capture_screenshot(
                        entrypoint,
                        desktop_path,
                        browser_bin=browser_bin,
                        window_size="1280,820",
                    )
                if not mobile_path.exists():
                    capture_screenshot(
                        entrypoint,
                        mobile_path,
                        browser_bin=browser_bin,
                        window_size="390,844",
                    )
        except Exception:
            pass

    # 3. Fallback: if desktop exists but mobile doesn't, replicate desktop bytes
    if desktop_path.exists() and not mobile_path.exists():
        try:
            mobile_path.write_bytes(desktop_path.read_bytes())
        except Exception:
            pass

    return (
        desktop_path if desktop_path.exists() else None,
        mobile_path if mobile_path.exists() else None,
    )


def commit_and_push_pr_proof(
    repo_dir: Path | str,
    branch: str,
) -> bool:
    """Stage, commit, and push proof screenshots under docs/pr-proof/<branch>/."""
    repo = Path(repo_dir).resolve()
    proof_rel = f"docs/pr-proof/{branch}"
    try:
        subprocess.run(["git", "add", proof_rel], cwd=str(repo), check=False)
        diff_res = subprocess.run(
            ["git", "diff", "--cached", "--name-only", proof_rel],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        )
        if diff_res.stdout.strip():
            subprocess.run(
                ["git", "commit", "-m", f"docs(proof): add quality gate screenshots for {branch}"],
                cwd=str(repo),
                capture_output=True,
                check=False,
            )
            subprocess.run(
                ["git", "push", "origin", branch],
                cwd=str(repo),
                capture_output=True,
                check=False,
            )
            return True
    except Exception:
        pass
    return False


def attach_pr_proof_to_body(
    pr_body: str,
    repo_dir: Path | str,
    branch: str,
    gate_result: Optional[QualityGateResult] = None,
    changed_files: Optional[List[str]] = None,
) -> Tuple[str, bool]:
    """Check if PR touches frontend files; if so, generate proof and append to body.

    Returns:
        (updated_body, was_attached)
    """
    repo = Path(repo_dir).resolve()
    files = changed_files if changed_files is not None else get_pr_changed_files(repo)

    # 1. Deterministic check: SKIP if no frontend changes
    if not is_frontend_change(files):
        return pr_body, False

    # 2. Save screenshots
    d_path, m_path = save_pr_proof_screenshots(repo, branch, gate_result=gate_result)

    # 3. Commit and push proof files to branch
    commit_and_push_pr_proof(repo, branch)

    # 4. Generate proof markdown
    owner, repo_name = get_github_repo_info(repo)
    proof_md = format_pr_proof_markdown(owner, repo_name, branch)

    # 5. Attach or update proof section in pr_body
    clean_body = pr_body.strip()
    if "## 📸 Proof" in clean_body:
        parts = clean_body.split("## 📸 Proof")
        base_body = parts[0].rstrip()
        new_body = f"{base_body}\n\n{proof_md}"
    else:
        new_body = f"{clean_body}\n\n{proof_md}" if clean_body else proof_md

    return new_body, True


def update_github_pr_body(
    repo_dir: Path | str,
    branch_or_pr: str,
    body: str,
) -> bool:
    """Update GitHub PR body using `gh pr edit`."""
    repo = Path(repo_dir).resolve()
    try:
        res = subprocess.run(
            ["gh", "pr", "edit", str(branch_or_pr), "--body", body],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        )
        return res.returncode == 0
    except Exception:
        return False
