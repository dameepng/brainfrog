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


_ENGINEERING_SKILLS = frozenset({
    "understand-codebase", "implement-code-change", "refactor-code-safely",
    "review-code-change", "diagnose-code-failure", "test-code-behavior",
    "verify-code-result",
})

# These are request wrappers, not intent keywords. Strip only at the start so
# instructions and examples later in a task cannot replace its primary action.
_REQUEST_PREFIX = re.compile(
    r"^(?:please|kindly|can you|could you|would you|will you|help me|"
    r"i (?:want|need|would like) (?:you )?to|let us|let's|to|"
    r"tolong|mohon|bisa(?:kah)?|boleh(?:kah)?|bantu(?:lah)?|"
    r"saya|aku|gue|gua|gw|kamu|anda|lu|lo|kau|"
    r"mau|ingin|pengen|harap|untuk|coba|yuk)\b[\s,:-]*",
    re.IGNORECASE,
)
_MODE_PREFIX = re.compile(
    r"^(?:\[(?:mode\s*[:=]\s*)?(?:plan(?:[- ]only)?|build|read[- ]only)\]|"
    r"plan[- ]only\s*:|"
    r"/(?:plan|build)\b|(?:in\s+)?(?:plan|build|read[- ]only)\s+mode\b|"
    r"mode\s+(?:plan|build)\b)[\s,:-]*",
    re.IGNORECASE,
)
_SOFTWARE_CONTEXT = re.compile(
    r"\b(?:code|codebase|kode|repo|repository|repositories|software|"
    r"backend|frontend|api|endpoint|endpoints|parser|function|functions|fungsi|"
    r"method|methods|module|modules|modul|class|classes|handler|handlers|"
    r"implementation|implementasi|diff|patch|commit|pr|cli|ui|ux|"
    r"test|tests|testing|testcase|pytest|unittest|ruff|mypy|pengujian|tes|"
    r"build|compiler|compilation|lint|typecheck|typescript|javascript|python|"
    r"rust|html|css|sql|database|schema|migration|migrations|"
    r"bug|bugs|crash|exception|stacktrace|dependency|dependencies|"
    r"config|configuration|konfigurasi|script|scripts|command|commands|"
    r"object|objects|field|fields|project|proyek|runtime|session|storage|provider|providers|json|"
    r"authentication|authorization|auth|http|https|grpc|websocket|"
    r"pagination|paginasi|cache|caching|retries|retry|timeout|"
    r"algorithm|algoritma|binary\s+search|pull\s+request)\b|"
    r"\b[\w.-]+\.(?:py|js|jsx|ts|tsx|go|rs|java|rb|php|cs|cpp|c|h|sql|html|css)\b",
    re.IGNORECASE,
)
_TARGET_MODIFIERS = (
    r"(?:(?:a|an|the|some|new|focused|meaningful|existing|current|"
    r"unit|integration|regression|e2e|acceptance|behavioral|full|entire|relevant|"
    r"sebuah|beberapa|baru|hasil|ulang)\s+)*"
)
_TEST_TARGET = re.compile(r"\b(?:tests?|testcases?|coverage|pengujian|tes)\b")
_CHECK_TARGET = re.compile(
    r"^(?:(?:that|whether|apakah|bahwa)\s+)?" + _TARGET_MODIFIERS +
    r"(?:tests?|build|lint|typecheck|checks?|results?|output|bukti|pengujian|tes)\b|"
    r"^(?:python3?\s+-m\s+)?(?:pytest|unittest|ruff|mypy)\b|"
    r"^(?:npm|pnpm|yarn)\s+(?:run\s+)?(?:test|build|lint|typecheck)\b|"
    r"^go\s+test\b|^cargo\s+(?:test|check|clippy)\b"
)
_FAILURE_CONTEXT = re.compile(
    r"\b(?:bugs?|fail(?:ing|ed|ure|ures)?|errors?|crash(?:es|ing)?|exception|"
    r"regressions?|broken|gagal|rusak|macet|lambat|stacktrace|leak)\b|"
    r"\btidak\s+(?:bekerja|berfungsi|jalan)\b"
)


def _select_engineering_intent(task: str) -> Optional[str]:
    """Route a primary EN/ID software action, not incidental task vocabulary.

    This is deliberately conservative and selects at most one workflow. Mode
    labels are context wrappers only; selection never changes runtime authority.
    Unrecognized phrasing remains eligible for explicit/custom skill selection.
    """
    request = task.strip().lower()
    while request:
        prefix = _REQUEST_PREFIX.match(request) or _MODE_PREFIX.match(request)
        if not prefix:
            break
        request = request[prefix.end():]

    # Secondary instructions, quoted examples on later lines, and conversation
    # history must not supply a different primary action or software context.
    primary = re.split(
        r"[;\n]|(?<=[.!?])\s+|\b(?:then|lalu|kemudian|afterwards)\b",
        request, maxsplit=1,
    )[0].strip()
    match = re.match(r"^([a-z]+)\b[\s,:-]*(.*)$", primary)
    if not match:
        return None
    action, target = match.groups()
    # A software topic does not make the requested object software work.
    # Keep context before a topic clause, e.g. "code about request routing".
    context_target = re.split(r"\b(?:about|tentang|mengenai|perihal)\b", target, maxsplit=1)[0]
    if not _SOFTWARE_CONTEXT.search(context_target):
        return None

    if action in {"review", "audit", "tinjau", "meninjau", "ulas", "evaluasi"}:
        return "review-code-change"
    if action in {"refactor", "refactorisasi", "restructure", "simplify", "rapikan", "rapihkan", "merapikan", "cleanup"}:
        return "refactor-code-safely"
    if action == "clean" and target.startswith("up "):
        return "refactor-code-safely"
    if action in {"fix", "debug", "diagnose", "repair", "troubleshoot", "resolve", "perbaiki", "memperbaiki", "benahi", "benerin", "betulin"}:
        return "diagnose-code-failure"
    if action in {"investigate", "selidiki", "trace", "telusuri"}:
        return "diagnose-code-failure" if _FAILURE_CONTEXT.search(target) else "understand-codebase"
    if action in {"verify", "validate", "verifikasi", "validasi", "confirm", "pastikan", "test", "uji", "tes"}:
        return "verify-code-result"
    if action in {"run", "rerun", "execute", "jalankan"}:
        return "verify-code-result" if _CHECK_TARGET.match(target) else None
    if action in {"check", "cek", "periksa"}:
        return "verify-code-result" if _CHECK_TARGET.match(target) else "review-code-change"
    if action in {"understand", "inspect", "explore", "explain", "map", "analyze", "analyse", "read", "plan", "design", "pahami", "memahami", "pelajari", "mempelajari", "jelaskan", "petakan", "rancang", "rencanakan", "baca", "lihat", "liat"}:
        return "understand-codebase"
    if action == "bagaimana" or (action == "how" and re.match(r"(?:does|do|is|are)\b", target)):
        return "understand-codebase"
    if action == "apa" and re.match(r"(?:isi|struktur|alur|fungsi)\b", target):
        return "understand-codebase"
    if action in {"write", "draft", "tulis", "susun"}:
        if re.match(r"^(?:(?:a|an|the|sebuah|code|security|architecture)\s+)*review\b", target):
            return "review-code-change"
        if re.match(r"^(?:(?:a|an|the|sebuah|implementation|migration)\s+)*plan\b", target):
            return "understand-codebase"
    if action in {"implement", "add", "create", "build", "develop", "extend", "change", "update", "replace", "introduce", "write", "draft", "implementasikan", "tambahkan", "menambahkan", "tambah", "tambahin", "buat", "bikin", "bangun", "kembangkan", "ubah", "tulis", "susun"}:
        # The object of "add parser tests" is tests; "add a parser with tests"
        # is implementation. Inspect only the initial object phrase.
        object_phrase = re.split(
            r"\b(?:for|to|with|using|according|and|into|in|from|that|which|"
            r"untuk|dengan|di|dari|yang|serta)\b", target, maxsplit=1,
        )[0]
        return "test-code-behavior" if _TEST_TARGET.search(object_phrase) else "implement-code-change"
    return None


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

    # Built-in engineering workflows follow the primary requested action. A
    # later mention of tests, design, or review must not change that workflow.
    engineering_name = _select_engineering_intent(task)
    if engineering_name is not None and engineering_name in available_skills:
        return available_skills[engineering_name], task

    # Preserve description matching for custom skills. Built-ins are excluded:
    # otherwise a rejected/secondary intent could re-enter via keyword overlap.
    task_words = set(re.findall(r"\b[a-zA-Z]{4,}\b", task_lower))
    for s_name, s_meta in available_skills.items():
        if s_name == "audit-anti-slop" or s_name in _ENGINEERING_SKILLS:
            continue
        # Extract keywords from description
        desc_words = {w for w in re.findall(r"\b[a-zA-Z]{4,}\b", s_meta.description.lower())}
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
    if not is_safe or not cmd:
        return subprocess.CompletedProcess(
            args=cmd or [script_name],
            returncode=1,
            stdout="",
            stderr=f"Security Validation Error: {msg}",
        )

    clean_name = Path(script_name).name
    script_path = skill.scripts[clean_name].resolve()
    from core.runtime.command_policy import CommandPolicy
    from core.runtime.process import run_hardened_subprocess

    skill_policy = CommandPolicy(allowed_script_paths=[script_path])

    try:
        res = run_hardened_subprocess(
            cmd,
            cwd=repo_dir,
            timeout=float(timeout),
            command_policy=skill_policy,
        )
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=res.returncode,
            stdout=res.stdout,
            stderr=res.stderr,
        )
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=1,
            stdout="",
            stderr=f"Execution error: {exc}",
        )
