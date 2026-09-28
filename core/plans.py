"""Session modes, plan document management, sandbox security, and command permission enforcement.

BrainFrog operates in two primary session modes:
- BUILD (default): Standard execution mode allowing code changes, test running, and PR creation.
- PLAN: Read-only exploration, clarifying questions (grilling), PRD drafting, and implementation planning.
        In Plan mode, source files and mutating shell commands are strictly protected.
        Only plan documents under .brainfrog/plans/ may be created or updated.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

MODE_BUILD = "build"
MODE_PLAN = "plan"
PLANS_DIR_REL = ".brainfrog/plans"

# -------------------------------------------------------------------------
# Shell Safety Classifier (Effect-based Read-Only Enforcement)
# -------------------------------------------------------------------------
_GIT_READONLY_SUBCMDS: Set[str] = {
    "status", "diff", "log", "show", "branch", "remote", "config",
    "rev-parse", "describe", "tag", "ls-files", "grep", "version",
    "--version", "-v", "help", "--help", "check-ignore", "cat-file",
}

_SAFE_READONLY_BINARIES: Set[str] = {
    "dir", "ls", "cat", "type", "head", "tail", "grep", "findstr",
    "rg", "ag", "bat", "more", "less", "pwd", "echo", "which", "where",
    "wc", "nl", "file", "stat",
}

_DANGEROUS_MUTATING_BINARIES: Set[str] = {
    "rm", "del", "erase", "rmdir", "mkdir", "md", "touch", "cp", "copy",
    "mv", "move", "chmod", "chown", "curl", "wget", "kill", "taskkill",
    "format", "shutdown", "reboot",
}

_SAFE_INTERPRETER_FLAGS: Set[str] = {
    "-v", "--version", "-V", "-h", "--help",
}


def is_safe_readonly_command(cmd_str: str) -> Tuple[bool, str]:
    """Classify whether a shell command is strictly read-only and safe for Plan mode.

    Returns (is_safe, refusal_reason).
    """
    clean_cmd = cmd_str.strip()
    if not clean_cmd:
        return True, ""

    # 1. Detect output redirection operators (>, >>, &>, 1>, 2>, *>)
    # Using regex to look for redirection operators outside quotes
    if re.search(r"(?:>>?|\&>|[12]\>|\*\>)", clean_cmd):
        return False, "Operasi pengalihan output (redirection `>`, `>>`) berpotensi menulis ke file dan dilarang dalam mode Plan."

    # 2. Command chaining operators (;, &&, ||, &)
    # Split on chaining operators and validate each segment
    chain_parts = re.split(r";|&&|\|\||&", clean_cmd)
    if len(chain_parts) > 1:
        for part in chain_parts:
            part_clean = part.strip()
            if not part_clean:
                continue
            safe, reason = is_safe_readonly_command(part_clean)
            if not safe:
                return False, f"Bagian dari rantai perintah tidak aman: {reason}"
        return True, ""

    # 3. Pipelines (|)
    # Only allow pipelines if all segments are recognized read-only tools
    if "|" in clean_cmd:
        pipe_segments = clean_cmd.split("|")
        for seg in pipe_segments:
            seg_clean = seg.strip()
            if not seg_clean:
                continue
            safe, reason = is_safe_readonly_command(seg_clean)
            if not safe:
                return False, f"Pipeline berisi perintah non-read-only ({seg_clean}): {reason}"
        return True, ""

    # 4. Tokenize command
    try:
        # Use posix=False on Windows to preserve Windows path semantics, but handle quotes
        tokens = shlex.split(clean_cmd, posix=os.name != "nt")
    except Exception:
        tokens = clean_cmd.split()

    if not tokens:
        return True, ""

    raw_prog = tokens[0].lower().replace("/", "\\")
    prog_name = Path(raw_prog).name.lower()
    if prog_name.endswith((".exe", ".cmd", ".bat")):
        prog_name = prog_name[:-4]

    # Explicitly forbidden mutating utilities
    if prog_name in _DANGEROUS_MUTATING_BINARIES:
        return False, f"Perintah '{prog_name}' memodifikasi berkas/sistem dan dilarang dalam mode Plan."

    # Git command inspection
    if prog_name == "git":
        if len(tokens) < 2:
            return True, ""  # `git` alone prints help
        subcmd = tokens[1].lower()

        if subcmd not in _GIT_READONLY_SUBCMDS:
            return False, (
                f"Sub-perintah git '{subcmd}' dapat memodifikasi state repository. "
                f"Dalam mode Plan, hanya sub-perintah read-only yang diizinkan (status, diff, log, show, dll)."
            )

        # Disallow mutative flags in branch, remote, config, tag
        lower_args = [t.lower() for t in tokens[2:]]
        if subcmd == "branch":
            if any(flag in lower_args for flag in ("-d", "-D", "-m", "-M", "--delete", "--move")):
                return False, "Menghapus atau mengubah nama branch git dilarang dalam mode Plan."
        elif subcmd == "remote":
            if any(act in lower_args for act in ("add", "rename", "remove", "rm", "set-url", "prune")):
                return False, "Memodifikasi konfigurasi git remote dilarang dalam mode Plan."
        elif subcmd == "config":
            if not any(flag in lower_args for flag in ("--get", "-l", "--list", "--get-all", "--get-regexp", "-g")):
                return False, "Memodifikasi konfigurasi git config dilarang dalam mode Plan."
        elif subcmd == "tag":
            if any(flag in lower_args for flag in ("-d", "--delete", "-a", "-s")):
                return False, "Membuat atau menghapus git tag dilarang dalam mode Plan."

        return True, ""

    # Known safe inspection tools
    if prog_name in _SAFE_READONLY_BINARIES:
        return True, ""

    # Programming languages / build tools: must have version/help inspection flags only
    if prog_name in {"python", "python3", "py", "node", "npm", "npx", "cargo", "go", "dotnet", "java", "make", "bash", "sh", "powershell", "pwsh", "cmd"}:
        args = tokens[1:]
        if args and all(arg.lower() in _SAFE_INTERPRETER_FLAGS for arg in args):
            return True, ""
        if prog_name == "npm" and args and args[0].lower() in {"list", "ls", "view", "outdated"}:
            return True, ""
        return False, (
            f"Eksekusi interpreter/build runner '{prog_name}' dengan argumen '{' '.join(args)}' tidak dapat dipastikan read-only. "
            f"Dalam mode Plan, eksekusi skrip dilarang."
        )

    # Any unrecognized command cannot be confirmed read-only
    return False, (
        f"Perintah '{prog_name}' tidak terdaftar sebagai alat inspeksi read-only. "
        f"Dalam mode Plan, hanya perintah inspeksi aman yang diizinkan."
    )


# -------------------------------------------------------------------------
# Plan Document Model & Storage
# -------------------------------------------------------------------------
@dataclass
class PlanDocument:
    filename: str
    path: Path
    title: str
    goal: str
    acceptance_criteria: List[str] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    steps: List[Dict[str, Any]] = field(default_factory=list)
    relevant_files: List[str] = field(default_factory=list)
    commit_hash: str = ""
    git_status_snapshot: str = ""
    file_hashes: Dict[str, str] = field(default_factory=dict)
    created_at: str = ""
    content: str = ""


def get_plans_dir(repo_dir: Path) -> Path:
    """Return the absolute path to the repository's plan directory."""
    return (repo_dir / PLANS_DIR_REL).resolve()


def save_plan_document(
    repo_dir: Path,
    filename: str,
    content: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> Path:
    """Save a plan document into .brainfrog/plans/ with strict sandbox validation.

    Security checks enforced:
    - Path traversal attempts (../) are rejected.
    - Absolute paths outside .brainfrog/plans/ are rejected.
    - Symlinks pointing outside .brainfrog/plans/ are rejected.
    - Non-markdown/non-json extensions are rejected.
    """
    plans_dir = get_plans_dir(repo_dir)
    plans_dir.mkdir(parents=True, exist_ok=True)

    allowed_exts = {".md", ".json", ".txt"}
    raw_path = Path(filename)
    if raw_path.suffix.lower() not in allowed_exts:
        raise PermissionError(
            f"Ekstensi file rencana tidak valid: '{raw_path.suffix}'. Hanya {allowed_exts} yang diizinkan."
        )

    # Check for symlink traversal before full resolve
    target_candidate = raw_path if raw_path.is_absolute() else (plans_dir / raw_path)
    curr = target_candidate
    while curr != plans_dir and curr != curr.parent:
        if curr.is_symlink():
            try:
                curr.resolve().relative_to(plans_dir)
            except ValueError:
                raise PermissionError(
                    f"Symlink traversal terdeteksi: symlink '{curr}' mengarah ke luar direktori rencana."
                )
        curr = curr.parent

    # Construct and resolve target path
    resolved_target = target_candidate.resolve()

    # Verify target stays inside plans_dir
    try:
        resolved_target.relative_to(plans_dir)
    except ValueError:
        raise PermissionError(
            f"Path traversal terdeteksi: path '{filename}' mengarah ke luar direktori rencana ({plans_dir})."
        )

    # Write plan content
    resolved_target.parent.mkdir(parents=True, exist_ok=True)
    resolved_target.write_text(content, encoding="utf-8")

    # Write companion metadata if provided
    if metadata is not None:
        meta_filename = resolved_target.stem + ".meta.json"
        meta_path = resolved_target.parent / meta_filename
        meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    return resolved_target


def compute_file_hash(file_path: Path) -> str:
    """Compute sha256 checksum of a file if it exists."""
    if not file_path.exists() or not file_path.is_file():
        return ""
    try:
        return hashlib.sha256(file_path.read_bytes()).hexdigest()
    except Exception:
        return ""


def get_latest_plan(repo_dir: Path) -> Optional[PlanDocument]:
    """Retrieve the most recent plan document from .brainfrog/plans/."""
    plans_dir = get_plans_dir(repo_dir)
    if not plans_dir.exists():
        return None

    md_files = [f for f in plans_dir.glob("*.md") if f.is_file()]
    if not md_files:
        return None

    # Sort by mtime descending
    md_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    latest_file = md_files[0]

    # Check for companion .meta.json
    meta_path = latest_file.with_suffix(".meta.json")
    metadata: Dict[str, Any] = {}
    if meta_path.exists():
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            metadata = {}

    content = latest_file.read_text(encoding="utf-8", errors="replace")

    # Extract goal and title from content if missing in metadata
    title = metadata.get("title") or latest_file.stem.replace("_", " ").title()
    goal = metadata.get("goal", "")
    if not goal:
        for line in content.splitlines():
            line_str = line.strip()
            if line_str.startswith("# "):
                title = line_str[2:].strip()
            elif line_str.lower().startswith(("- **goal", "- **tujuan", "goal:", "tujuan:")):
                goal = line_str.split(":", 1)[1].strip()
                break

    return PlanDocument(
        filename=latest_file.name,
        path=latest_file,
        title=title,
        goal=goal or title,
        acceptance_criteria=metadata.get("acceptance_criteria", []),
        assumptions=metadata.get("assumptions", []),
        steps=metadata.get("steps", []),
        relevant_files=metadata.get("relevant_files", []),
        commit_hash=metadata.get("commit_hash", ""),
        git_status_snapshot=metadata.get("git_status_snapshot", ""),
        file_hashes=metadata.get("file_hashes", {}),
        created_at=metadata.get("created_at", ""),
        content=content,
    )


def check_plan_staleness(repo_dir: Path, plan: PlanDocument) -> Tuple[bool, List[str]]:
    """Determine whether the repository has changed since the plan was created."""
    reasons: List[str] = []

    # 1. Check Git HEAD commit hash
    try:
        head_proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        current_commit = head_proc.stdout.strip()
        if plan.commit_hash and current_commit and plan.commit_hash != current_commit:
            reasons.append(
                f"Commit Git telah berubah dari {plan.commit_hash[:7]} menjadi {current_commit[:7]}"
            )
    except Exception:
        pass

    # 2. Check relevant files hash & existence
    for rel_path in plan.relevant_files:
        full_path = repo_dir / rel_path
        old_hash = plan.file_hashes.get(rel_path)
        if not full_path.exists():
            if old_hash:
                reasons.append(f"Berkas relevan '{rel_path}' dihapus sejak rencana dibuat.")
        else:
            current_hash = compute_file_hash(full_path)
            if old_hash and current_hash != old_hash:
                reasons.append(f"Berkas relevan '{rel_path}' dimodifikasi sejak rencana dibuat.")

    # 3. Check uncommitted changes touching relevant files
    try:
        status_proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        for line in status_proc.stdout.splitlines():
            parts = line.strip().split(maxsplit=1)
            if len(parts) == 2:
                dirty_file = parts[1].strip()
                if any(dirty_file == rf or dirty_file.startswith(rf.rstrip("/") + "/") for rf in plan.relevant_files):
                    reasons.append(f"Perubahan uncommitted terdeteksi pada '{dirty_file}'.")
    except Exception:
        pass

    # Deduplicate reasons while preserving order
    seen: Set[str] = set()
    deduped_reasons = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            deduped_reasons.append(r)

    return len(deduped_reasons) > 0, deduped_reasons


def format_plan_handoff(plan: PlanDocument, repo_dir: Path) -> str:
    """Format the latest plan into context for Build mode."""
    is_stale, reasons = check_plan_staleness(repo_dir, plan)

    lines = [
        f"### Konteks Handoff Rencana Terakhir: {plan.title}",
        f"**File Rencana:** `.brainfrog/plans/{plan.filename}`",
        f"**Tujuan (Goal):** {plan.goal}",
    ]

    if plan.acceptance_criteria:
        lines.append("\n**Kriteria Penerimaan (Acceptance Criteria):**")
        for ac in plan.acceptance_criteria:
            lines.append(f"- [ ] {ac}")

    if plan.assumptions:
        lines.append("\n**Asumsi & Usulan Teknis:**")
        for asm in plan.assumptions:
            lines.append(f"- {asm}")

    if plan.relevant_files:
        lines.append(f"\n**File Terkait:** {', '.join(plan.relevant_files)}")

    if plan.steps:
        lines.append("\n**Rencana Langkah Implementasi:**")
        for s in plan.steps:
            if isinstance(s, dict):
                s_id = s.get("id", "")
                s_desc = s.get("description", "")
                s_files = s.get("files", [])
                files_note = f" (files: {', '.join(s_files)})" if s_files else ""
                lines.append(f"{s_id}. {s_desc}{files_note}")
            else:
                lines.append(f"- {s}")

    if is_stale:
        lines.append("\n⚠️ **PERINGATAN PERUBAHAN CODEBASE (STALE PLAN):**")
        lines.append("Repository telah mengalami perubahan sejak rencana ini disusun:")
        for r in reasons:
            lines.append(f"  - {r}")
        lines.append("Agent wajib memeriksa ulang bagian file yang terdampak sebelum mengeksekusi.")

    lines.append(
        "\n**Instruksi Adaptabilitas:** Jika instruksi terbaru dari pengguna mengubah scope atau prioritas, "
        "prioritaskan kebutuhan terbaru pengguna dan jangan mengikuti rencana lama secara buta."
    )

    return "\n".join(lines)
