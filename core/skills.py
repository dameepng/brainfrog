"""skills.py — Modular, on-demand skill loader for BrainFrog.

Architecture & Principles:
- BRAINFROG.md contains persistent, always-applicable project rules.
- Modular skills live in .brainfrog/skills/<skill_name>/ with SKILL.md, references/, and scripts/.
- Startup indexer reads ONLY `name` and `description` frontmatter (no full content injection).
- Selection is performed per-turn:
    1. Explicit selection via `/skill <name>` or parameter.
    2. Automatic selection matching task intent against skill descriptions.
- Content loading is progressive: SKILL.md first, references/ only when needed.
- Scripts in skills are untrusted by default; arguments and paths are validated before execution.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class SkillMetadata:
    name: str
    description: str
    dir_path: Path
    skill_file: Path
    references: Dict[str, Path] = field(default_factory=dict)
    scripts: Dict[str, Path] = field(default_factory=dict)

    def summary(self) -> str:
        ref_count = len(self.references)
        script_count = len(self.scripts)
        return f"{self.name}: {self.description[:90]}... (refs: {ref_count}, scripts: {script_count})"


def parse_skill_frontmatter(skill_file: Path) -> Tuple[Dict[str, str], str]:
    """Parse YAML-like frontmatter between --- markers and return (metadata_dict, body_text)."""
    if not skill_file.exists():
        return {}, ""

    try:
        content = skill_file.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}, ""

    if not content.startswith("---"):
        return {}, content

    end_idx = content.find("---", 3)
    if end_idx == -1:
        return {}, content

    frontmatter_text = content[3:end_idx].strip()
    body_text = content[end_idx + 3:].strip()

    metadata = {}
    current_key = None
    val_lines = []

    for line in frontmatter_text.splitlines():
        line_stripped = line.strip()
        if not line_stripped or line_stripped.startswith("#"):
            continue

        match = re.match(r"^([a-zA-Z0-9_\-]+)\s*:\s*(.*)$", line)
        if match:
            if current_key:
                metadata[current_key] = " ".join(val_lines).strip()
            current_key = match.group(1).strip()
            initial_val = match.group(2).strip()
            val_lines = [initial_val] if initial_val else []
        elif current_key:
            val_lines.append(line_stripped)

    if current_key:
        metadata[current_key] = " ".join(val_lines).strip()

    return metadata, body_text


def index_skills(repo_dir: Path) -> Dict[str, SkillMetadata]:
    """Scan .brainfrog/skills (and ~/.brainfrog/skills) and index ONLY name & description."""
    indexed: Dict[str, SkillMetadata] = {}

    pkg_skills_dir = Path(__file__).resolve().parent.parent / ".brainfrog" / "skills"
    alt_pkg_skills_dir = Path(__file__).resolve().parent / ".brainfrog" / "skills"
    search_dirs = [
        repo_dir / ".brainfrog" / "skills",
        repo_dir / ".agents" / "skills",
        repo_dir / "skills",
        pkg_skills_dir,
        alt_pkg_skills_dir,
        Path.home() / ".brainfrog" / "skills",
        Path.home() / ".agents" / "skills",
        Path.home() / ".gemini" / "antigravity-ide" / "builtin" / "skills",
    ]

    for base_dir in search_dirs:
        if not base_dir.exists() or not base_dir.is_dir():
            continue

        for child in sorted(base_dir.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue

            skill_md = child / "SKILL.md"
            if not skill_md.exists() or not skill_md.is_file():
                continue

            meta, _ = parse_skill_frontmatter(skill_md)
            name = meta.get("name") or child.name
            description = meta.get("description") or f"Custom skill from {child.name}"

            # Index references
            ref_dir = child / "references"
            references = {}
            if ref_dir.exists() and ref_dir.is_dir():
                for rf in ref_dir.glob("*.md"):
                    references[rf.name] = rf

            # Index scripts
            script_dir = child / "scripts"
            scripts = {}
            if script_dir.exists() and script_dir.is_dir():
                for sf in script_dir.glob("*.py"):
                    scripts[sf.name] = sf

            indexed[name] = SkillMetadata(
                name=name,
                description=description,
                dir_path=child.resolve(),
                skill_file=skill_md.resolve(),
                references=references,
                scripts=scripts,
            )

    return indexed


def _matches_anti_slop(task_lower: str) -> bool:
    """Evaluate whether task intent calls for the audit-anti-slop skill."""
    # 1. Explicit anti-slop triggers
    if any(k in task_lower for k in ["anti-slop", "antislop", "anti slop"]):
        return True

    # 2. UI / copy audit & polish with slop indicators
    is_ui_or_copy = any(k in task_lower for k in [
        "ui", "ux", "frontend", "interface", "halaman", "page", "copy", "desain",
        "design", "komponen", "component", "landing page", "web", "html", "css",
        "airbnb", "layout", "grid", "menu", "header", "footer", "style"
    ])

    audit_or_polish = any(k in task_lower for k in [
        "audit", "periksa", "cek", "review", "polish", "tingkatkan", "poles",
        "kurangi", "perbaiki tampilan", "improve", "fix", "bikin", "buat", "rapikan", "rapihkan"
    ])

    slop_indicators = any(k in task_lower for k in [
        "slop", "generic", "generik", "placeholder", "lorem", "lorem ipsum",
        "unsupported claim", "unverified claim", "klaim palsu", "dead button",
        "tombol mati", "inert", "weak design", "pemanis berlebih", "card berulang",
        "acak-acakan", "berantakan", "hancur", "overlap", "tumpang tindih"
    ])

    if is_ui_or_copy and (slop_indicators or (audit_or_polish and "slop" in task_lower) or any(k in task_lower for k in ["acak-acakan", "berantakan", "overlap", "tumpang tindih"])):
        return True

    if audit_or_polish and slop_indicators:
        return True

    return False


def select_skill(
    task: str,
    available_skills: Dict[str, SkillMetadata],
    explicit_skill_name: Optional[str] = None,
) -> Tuple[Optional[SkillMetadata], str]:
    """Select appropriate skill for the task.
    
    Returns:
        (selected_skill, cleaned_task)
    """
    if not available_skills:
        return None, task

    # 1. Explicit argument
    if explicit_skill_name:
        clean_name = explicit_skill_name.strip().lower()
        if clean_name in available_skills:
            return available_skills[clean_name], task
        # If user passed partial match
        for s_name, s_meta in available_skills.items():
            if s_name.lower() == clean_name:
                return s_meta, task

    stripped = task.strip()

    # 2. Explicit slash command in prompt: e.g. "/skill audit-anti-slop [task...]"
    skill_cmd_pattern = r"^/skill\s+([a-zA-Z0-9_\-]+)(?:\s+(.*))?$"
    match = re.match(skill_cmd_pattern, stripped, re.IGNORECASE | re.DOTALL)
    if match:
        requested_name = match.group(1).lower()
        remainder = (match.group(2) or "").strip()
        if requested_name in available_skills:
            return available_skills[requested_name], remainder or stripped
        # Check partial
        for s_name, s_meta in available_skills.items():
            if s_name.lower() == requested_name:
                return s_meta, remainder or stripped

    # 3. Intent / Description matching
    task_lower = stripped.lower()

    # Check audit-anti-slop specifically
    if "audit-anti-slop" in available_skills and _matches_anti_slop(task_lower):
        return available_skills["audit-anti-slop"], task

    # Generic matching: match task against indexed descriptions if strongly relevant
    for s_name, s_meta in available_skills.items():
        if s_name == "audit-anti-slop":
            continue
        # Extract keywords from description
        desc_words = {w for w in re.findall(r"\b[a-zA-Z]{4,}\b", s_meta.description.lower())}
        task_words = set(re.findall(r"\b[a-zA-Z]{4,}\b", task_lower))
        overlap = desc_words.intersection(task_words)
        if len(overlap) >= 3:
            return s_meta, task

    return None, task


def load_skill_content(
    skill: SkillMetadata,
    include_references: bool = False,
    requested_reference: Optional[str] = None,
) -> str:
    """Read SKILL.md body, and conditionally load references only when needed."""
    _, body = parse_skill_frontmatter(skill.skill_file)

    output_parts = [
        f"# Active Skill: {skill.name}",
        f"**Description**: {skill.description}",
        "",
        body.strip(),
    ]

    # Load specific reference if requested
    if requested_reference and requested_reference in skill.references:
        ref_path = skill.references[requested_reference]
        try:
            ref_content = ref_path.read_text(encoding="utf-8", errors="replace")
            output_parts.extend([
                "",
                "---",
                f"## Reference Document: {requested_reference}",
                ref_content.strip(),
            ])
        except Exception:
            pass
    elif include_references and skill.references:
        for ref_name, ref_path in sorted(skill.references.items()):
            try:
                ref_content = ref_path.read_text(encoding="utf-8", errors="replace")
                output_parts.extend([
                    "",
                    "---",
                    f"## Reference Document: {ref_name}",
                    ref_content.strip(),
                ])
            except Exception:
                pass

    return "\n".join(output_parts).strip()


def validate_skill_script(
    script_name: str,
    args: List[str],
    skill: SkillMetadata,
    repo_dir: Path,
) -> Tuple[bool, str, List[str]]:
    """Validate skill script before execution (Zero Trust on Skill Scripts).
    
    Checks:
    - Script file must exist inside the skill's scripts/ directory.
    - Script path cannot traverse outside the skill folder.
    - Argument paths must resolve inside the target repo_dir.
    - No shell injection characters.
    """
    clean_name = Path(script_name).name
    if clean_name not in skill.scripts:
        return False, f"Script '{script_name}' not found in skill '{skill.name}' scripts directory.", []

    script_path = skill.scripts[clean_name].resolve()
    scripts_dir = (skill.dir_path / "scripts").resolve()

    try:
        if not script_path.is_relative_to(scripts_dir):
            return False, f"Script path traversal detected: {script_path}", []
    except AttributeError:
        # Python < 3.9 fallback
        if not str(script_path).startswith(str(scripts_dir)):
            return False, f"Script path traversal detected: {script_path}", []

    if not script_path.exists() or not script_path.is_file():
        return False, f"Script file does not exist: {script_path}", []

    # Validate arguments (path safety and shell injection prevention)
    dangerous_chars = [";", "|", "&", "`", "$", "\n", "\r"]
    sanitized_args: List[str] = []

    for arg in args:
        if any(c in arg for c in dangerous_chars):
            return False, f"Dangerous character detected in argument: {arg}", []

        # If argument looks like a relative file path, verify it stays within repo_dir
        if not arg.startswith("-") and ("/" in arg or "\\" in arg or "." in arg):
            try:
                target_path = (repo_dir / arg).resolve()
                if target_path.exists() and not target_path.is_relative_to(repo_dir.resolve()):
                    return False, f"Target path escapes workspace repository: {arg}", []
            except Exception:
                pass

        sanitized_args.append(arg)

    # Safe argument array for discrete execution (shell=False)
    cmd = [sys.executable, str(script_path), *sanitized_args]
    return True, "Script validated successfully.", cmd


def run_skill_script(
    script_name: str,
    args: List[str],
    skill: SkillMetadata,
    repo_dir: Path,
    timeout: int = 20,
) -> subprocess.CompletedProcess:
    """Run a validated skill script safely with discrete arguments (shell=False)."""
    is_safe, msg, cmd = validate_skill_script(script_name, args, skill, repo_dir)
    if not is_safe:
        return subprocess.CompletedProcess(
            args=cmd or [script_name],
            returncode=1,
            stdout="",
            stderr=f"Security Validation Error: {msg}",
        )

    try:
        return subprocess.run(
            cmd,
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=124,
            stdout="",
            stderr=f"Script execution timed out after {timeout} seconds.",
        )
    except Exception as exc:
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=1,
            stdout="",
            stderr=f"Execution error: {exc}",
        )
