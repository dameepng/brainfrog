"""Automated PR Proof Screenshot Generator for Frontend Changes.

Conditionally captures desktop and mobile viewport screenshots of the
verified frontend, saves them into the dedicated append-only orphan branch
`pr-proof-assets` via an isolated git worktree, and formats the PR body
using permanent raw GitHub URLs tied to the exact commit SHA.

SKIPS completely if no frontend changes are detected in the PR.
"""
from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import tempfile
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
    commit_sha: str,
    desktop_rel: str,
    mobile_rel: str,
) -> str:
    """Format standard Markdown proof table with permanent commit-SHA raw GitHub URLs."""
    raw_desktop = f"https://raw.githubusercontent.com/{owner}/{repo}/{commit_sha}/{desktop_rel}"
    raw_mobile = f"https://raw.githubusercontent.com/{owner}/{repo}/{commit_sha}/{mobile_rel}"

    return (
        "## 📸 Proof\n\n"
        "| Desktop | Mobile |\n"
        "|---|---|\n"
        f"| ![Desktop]({raw_desktop}) | ![Mobile]({raw_mobile}) |"
    )


def publish_proof_to_assets_branch(
    repo_dir: Path | str,
    target_identifier: str,
    desktop_bytes: bytes,
    mobile_bytes: bytes,
    assets_branch: str = "pr-proof-assets",
) -> Optional[Tuple[str, str, str]]:
    """Store screenshots in append-only orphan branch pr-proof-assets using an isolated git worktree.

    Args:
        repo_dir: Path to the main git repo.
        target_identifier: PR number (e.g. "pr-8") or branch name.
        desktop_bytes: Binary PNG content for desktop viewport.
        mobile_bytes: Binary PNG content for mobile viewport.
        assets_branch: Asset branch name (defaults to 'pr-proof-assets').

    Returns:
        (commit_sha, desktop_rel_path, mobile_rel_path) on success, or None on failure.
    """
    repo = Path(repo_dir).resolve()
    timestamp = int(time.time())
    safe_target = re.sub(r"[^a-zA-Z0-9_.-]", "-", str(target_identifier)).strip("-")
    sub_folder = f"{safe_target}/{timestamp}"

    desktop_rel = f"{sub_folder}/desktop.png"
    mobile_rel = f"{sub_folder}/mobile.png"

    wt_dir = Path(tempfile.gettempdir()) / f"bf-wt-{os.getpid()}-{timestamp}"
    if wt_dir.exists():
        shutil.rmtree(wt_dir, ignore_errors=True)

    try:
        # 1. Fetch remote assets_branch
        subprocess.run(
            ["git", "fetch", "origin", f"{assets_branch}:{assets_branch}"],
            cwd=str(repo),
            capture_output=True,
            check=False,
        )

        # 2. Add temporary worktree checked out to assets_branch
        add_res = subprocess.run(
            ["git", "worktree", "add", str(wt_dir), assets_branch],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        )
        if add_res.returncode != 0:
            return None

        # 3. Pull latest changes inside worktree
        subprocess.run(
            ["git", "pull", "origin", assets_branch],
            cwd=str(wt_dir),
            capture_output=True,
            check=False,
        )

        # 4. Save screenshots under <pr_or_branch>/<timestamp>/
        dest_dir = wt_dir / safe_target / str(timestamp)
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / "desktop.png").write_bytes(desktop_bytes)
        (dest_dir / "mobile.png").write_bytes(mobile_bytes)

        # 5. Commit append-only
        subprocess.run(["git", "add", "."], cwd=str(wt_dir), check=True)
        subprocess.run(
            ["git", "commit", "-m", f"proof: add screenshots for {target_identifier} ({timestamp})"],
            cwd=str(wt_dir),
            capture_output=True,
            check=True,
        )

        # 6. Retrieve the EXACT commit SHA
        sha_res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(wt_dir),
            capture_output=True,
            text=True,
            check=True,
        )
        commit_sha = sha_res.stdout.strip()

        # 7. Push to origin pr-proof-assets
        subprocess.run(
            ["git", "push", "origin", assets_branch],
            cwd=str(wt_dir),
            capture_output=True,
            check=False,
        )

        return commit_sha, desktop_rel, mobile_rel

    except Exception:
        return None
    finally:
        # Guarantee worktree removal
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt_dir)],
            cwd=str(repo),
            capture_output=True,
            check=False,
        )
        if wt_dir.exists():
            shutil.rmtree(wt_dir, ignore_errors=True)


def attach_pr_proof_to_body(
    pr_body: str,
    repo_dir: Path | str,
    branch: str,
    gate_result: Optional[QualityGateResult] = None,
    changed_files: Optional[List[str]] = None,
    pr_number: Optional[str] = None,
) -> Tuple[str, bool]:
    """Check if PR touches frontend files; if so, generate proof and append to body.

    Screenshots are published to the append-only `pr-proof-assets` branch,
    and referenced in the PR body via their immutable commit SHA.

    Returns:
        (updated_body, was_attached)
    """
    repo = Path(repo_dir).resolve()
    files = changed_files if changed_files is not None else get_pr_changed_files(repo)

    # 1. Deterministic check: SKIP if no frontend changes
    if not is_frontend_change(files):
        return pr_body, False

    # 2. Extract screenshot bytes
    desktop_bytes: Optional[bytes] = None
    mobile_bytes: Optional[bytes] = None

    if gate_result:
        d_b64 = gate_result.screenshot_desktop_base64 or gate_result.screenshot_base64
        if d_b64:
            try:
                desktop_bytes = base64.b64decode(d_b64)
            except Exception:
                pass
        m_b64 = gate_result.screenshot_mobile_base64
        if m_b64:
            try:
                mobile_bytes = base64.b64decode(m_b64)
            except Exception:
                pass

    # Fallback to headless browser capture if gate_result didn't include screenshots
    if not desktop_bytes or not mobile_bytes:
        try:
            from system2.visual_inspector import (
                capture_screenshot,
                find_browser_bin,
                find_html_entrypoint,
            )

            entrypoint = find_html_entrypoint(repo)
            browser_bin = find_browser_bin()
            if entrypoint and browser_bin:
                with tempfile.TemporaryDirectory() as tmp_shots:
                    tmp_p = Path(tmp_shots)
                    d_tmp = tmp_p / "d.png"
                    m_tmp = tmp_p / "m.png"
                    if not desktop_bytes:
                        if capture_screenshot(entrypoint, d_tmp, browser_bin=browser_bin, window_size="1280,820"):
                            desktop_bytes = d_tmp.read_bytes()
                    if not mobile_bytes:
                        if capture_screenshot(entrypoint, m_tmp, browser_bin=browser_bin, window_size="390,844"):
                            mobile_bytes = m_tmp.read_bytes()
        except Exception:
            pass

    if not desktop_bytes:
        return pr_body, False

    if not mobile_bytes:
        mobile_bytes = desktop_bytes

    # 3. Publish screenshots to orphan pr-proof-assets branch
    target_id = f"pr-{pr_number}" if pr_number else branch
    pub_res = publish_proof_to_assets_branch(
        repo_dir=repo,
        target_identifier=target_id,
        desktop_bytes=desktop_bytes,
        mobile_bytes=mobile_bytes,
    )
    if not pub_res:
        return pr_body, False

    commit_sha, desktop_rel, mobile_rel = pub_res

    # 4. Generate proof markdown with permanent commit SHA URL
    owner, repo_name = get_github_repo_info(repo)
    proof_md = format_pr_proof_markdown(owner, repo_name, commit_sha, desktop_rel, mobile_rel)

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
