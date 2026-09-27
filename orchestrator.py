"""Orchestrator — wires System 1 (Jev) and System 2 (Claude) into a loop.

Now enhanced with:
- BRAINFROG.md / CLAUDE.md persistent project memory & guidelines
- Context pinning via @file mentions in prompts
- Automatic git checkpointing per successful step for instant /undo
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure the package root (directory of this file) is always on sys.path so
# that `skills.py` and other sibling modules are importable regardless of the
# working directory from which `brainfrog` is invoked.
_PACKAGE_DIR = str(Path(__file__).resolve().parent)
if _PACKAGE_DIR not in sys.path:
    sys.path.insert(0, _PACKAGE_DIR)

from modules import Domain, resolve_focus_tree
from system1.base import Answer, ChoiceQuestion, NoulQuestion, ScoreQuestion, SystemOneClient
from system2 import PlanStep, System2Client, extract_json
from git_guard import (
    ensure_gitignore_security,
    purge_tracked_sensitive_files,
    scan_staged_changes,
    scan_dict_files,
    unstage_staged_changes,
)


@dataclass
class StepResult:
    step: PlanStep
    outcome: str  # "opened_pr" | "drafted_pr" | "escalated" | "abandoned"
    retries: int
    detail: str


@dataclass
class RunConfig:
    repo_dir: Path
    task: str
    test_command: List[str]
    max_retries: int = 3
    auto_pr: bool = False
    pr_risk_ceiling: str = "medium"  # open PR automatically only up to this risk
    branch_prefix: str = "agentic/"
    domains: Dict[str, Domain] = field(default_factory=dict)
    min_domain_confidence: float = 0.55
    skill: Optional[str] = None
    mode: str = "build"
    plan_context: Optional[str] = None


@dataclass
class ScopeDecision:
    domain: Optional[Domain]
    change_type: str
    focus_tree: str
    clarify_message: Optional[str] = None


def get_guideline_files(repo_dir: Path) -> List[Path]:
    """Find all relevant guideline, rules, and design specification files."""
    found: List[Path] = []
    seen_names = set()

    # 1. Check priority files in workspace
    priority_names = [
        "BRAINFROG.md", "brainfrog.md",
        "DESIGN.md", "design.md",
        "CLAUDE.md", "claude.md",
        "AGENTS.md", "agents.md",
        "RULES.md", "rules.md",
    ]
    for name in priority_names:
        p = repo_dir / name
        if p.exists() and p.is_file() and p.name.lower() not in seen_names:
            found.append(p)
            seen_names.add(p.name.lower())

    # 2. Glob for any DESIGN*.md, design*.md, STYLE*.md, style*.md in workspace
    for pattern in ("DESIGN*.md", "design*.md", "STYLE*.md", "style*.md", "*design*.md", "UI*.md"):
        try:
            for p in sorted(repo_dir.glob(pattern)):
                if p.is_file() and p.name.lower() not in seen_names:
                    found.append(p)
                    seen_names.add(p.name.lower())
        except Exception:
            pass

    # 3. Check rules folders (.agents/rules or .brainfrog/rules)
    for rules_dir in (repo_dir / ".agents" / "rules", repo_dir / ".brainfrog" / "rules"):
        if rules_dir.exists() and rules_dir.is_dir():
            try:
                for p in sorted(rules_dir.glob("*.md")):
                    if p.is_file() and p.name.lower() not in seen_names:
                        found.append(p)
                        seen_names.add(p.name.lower())
            except Exception:
                pass

    # 4. If repo_dir does NOT have its own BRAINFROG.md, include the bundled core BRAINFROG.md
    if "brainfrog.md" not in seen_names:
        pkg_brainfrog = Path(__file__).resolve().parent / "BRAINFROG.md"
        if pkg_brainfrog.exists() and pkg_brainfrog.is_file():
            found.append(pkg_brainfrog)
            seen_names.add("brainfrog.md")

    return found


def load_project_guidelines(repo_dir: Path) -> str:
    """Read and aggregate persistent rules, design specifications, and guidelines."""
    files = get_guideline_files(repo_dir)
    if not files:
        return ""

    sections = []
    for f in files:
        try:
            content = f.read_text(encoding="utf-8", errors="replace").strip()
            if content:
                label = f"Project Design Specification: {f.name}" if "design" in f.name.lower() or "style" in f.name.lower() else f"System Memory & Guidelines: {f.name}"
                sections.append(f"### [{label}]\n{content}")
        except Exception:
            pass

    return "\n\n".join(sections).strip()


def extract_mentioned_files(task: str, repo_dir: Path) -> Dict[str, str]:
    """Extract and read files referenced with @filename in user prompt."""
    pattern = r"@([a-zA-Z0-9_\-\.\/\\]+)"
    matches = re.findall(pattern, task)
    pinned = {}
    for m in matches:
        target = (repo_dir / m).resolve()
        if target.exists() and target.is_file():
            try:
                rel = str(target.relative_to(repo_dir.resolve()))
                pinned[rel] = target.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
    return pinned


def sanitize_surrogates(text: str) -> str:
    """Sanitize unpaired or malformed surrogate characters that break UTF-8 encoders on Windows."""
    if not text:
        return ""
    try:
        text = text.encode("utf-16", "surrogatepass").decode("utf-16", errors="replace")
    except Exception:
        pass
    return text.encode("utf-8", errors="replace").decode("utf-8")


def _run(cmd: List[str], cwd: Path) -> subprocess.CompletedProcess:
    use_shell = sys.platform == "win32"
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        shell=use_shell,
        encoding="utf-8",
        errors="replace",
    )


def _repo_tree(repo_dir: Path, max_files: int = 200) -> str:
    files = []
    for p in sorted(repo_dir.rglob("*")):
        if any(part.startswith(".") or part in ("node_modules", "__pycache__") for part in p.parts):
            continue
        if p.is_file():
            files.append(str(p.relative_to(repo_dir)))
        if len(files) >= max_files:
            break
    return "\n".join(files)


def _read_files_from_tree(repo_dir: Path, tree: str, max_files: int = 40) -> Dict[str, str]:
    paths = [p for p in tree.splitlines() if p.strip()][:max_files]
    return _read_files(repo_dir, paths)


def _normalize_rel_path(repo_dir: Path, rel: str) -> str:
    """Normalize relative path. If the model accidentally prepends the current
    working directory name (e.g. 'testing_agentic/index.html' when repo_dir is
    already '.../testing_agentic'), strip the leading folder to prevent accidental
    nested directories.
    """
    clean = rel.replace("\\", "/").strip().lstrip("/")
    parts = clean.split("/")
    if parts and parts[0] == repo_dir.name and len(parts) > 1:
        return "/".join(parts[1:])
    return clean


def _read_files(repo_dir: Path, paths: List[str]) -> Dict[str, str]:
    out = {}
    for rel in paths:
        norm = _normalize_rel_path(repo_dir, rel)
        f = repo_dir / norm
        out[norm] = f.read_text(encoding="utf-8", errors="replace") if f.exists() else ""
    return out


def _write_files(repo_dir: Path, files: Dict[str, str], mode: str = "build") -> None:
    if mode == "plan":
        raise PermissionError(
            "Modifikasi berkas source code dilarang dalam mode Plan. Gunakan mode Build untuk melakukan perubahan."
        )
    for rel, content in files.items():
        norm = _normalize_rel_path(repo_dir, rel)
        f = repo_dir / norm
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")


@dataclass
class FileChangeStat:
    file_path: str
    added: int
    deleted: int
    is_binary: bool = False


@dataclass
class CommitDiffSummary:
    total_files: int
    total_added: int
    total_deleted: int
    changes: List[FileChangeStat]


def _diff_stat(repo_dir: Path) -> Dict[str, int]:
    diff = _run(["git", "diff", "--numstat"], repo_dir).stdout
    added, deleted = 0, 0
    for line in diff.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            added += int(parts[0])
            deleted += int(parts[1])
    return {"lines_added": added, "lines_deleted": deleted}


def _staged_diff_summary(repo_dir: Path) -> CommitDiffSummary:
    """Extract per-file addition/deletion stats from currently staged files."""
    proc = _run(["git", "diff", "--cached", "--numstat"], repo_dir)
    changes: List[FileChangeStat] = []
    tot_added, tot_deleted = 0, 0
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            add_s, del_s, fpath = parts[0].strip(), parts[1].strip(), parts[2].strip()
            if add_s.isdigit() and del_s.isdigit():
                a, d = int(add_s), int(del_s)
                tot_added += a
                tot_deleted += d
                changes.append(FileChangeStat(file_path=fpath, added=a, deleted=d, is_binary=False))
            else:
                changes.append(FileChangeStat(file_path=fpath, added=0, deleted=0, is_binary=True))
    return CommitDiffSummary(
        total_files=len(changes),
        total_added=tot_added,
        total_deleted=tot_deleted,
        changes=changes,
    )


def _format_diff_breakdown(summary: CommitDiffSummary, max_files: int = 10) -> List[str]:
    """Format structured file tree breakdown with addition/deletion indicators."""
    if not summary.changes:
        return []
    lines = []
    num_to_show = min(len(summary.changes), max_files)
    for idx, c in enumerate(summary.changes[:num_to_show]):
        is_last = (idx == len(summary.changes) - 1)
        branch = "└──" if is_last else "├──"
        if c.is_binary:
            stat_str = "[dim](binary)[/dim]"
        else:
            stat_str = f"[green]+{c.added}[/green], [red]-{c.deleted}[/red]"
        lines.append(f"[git]    {branch} [bold #CCCCCC]{c.file_path}[/bold #CCCCCC] [dim]({stat_str})[/dim]")

    if len(summary.changes) > max_files:
        remaining = len(summary.changes) - max_files
        lines.append(f"[git]    └── [dim]... and {remaining} more files[/dim]")

    file_word = "file" if summary.total_files == 1 else "files"
    lines.append(
        f"[git]    📊 [bold #00D7D7]{summary.total_files} {file_word} changed[/bold #00D7D7] "
        f"([green]+{summary.total_added}[/green], [red]-{summary.total_deleted}[/red])"
    )
    return lines


# ---------------------------------------------------------------------------
# System 1 gate questions (TypeSafe best practices applied)
#
# Design principles from SKILL.md:
#   1. Decompose into narrow, atomic questions
#   2. Use structured instructions (dict) with question + focus
#   3. Use contrastive criteria (what / not_for / examples)
#   4. Fan-out: ask speculative questions in the same request
#   5. Compose answers in code with explicit thresholds
# ---------------------------------------------------------------------------

# --- Scope gate: fan-out questions ---
CHANGE_TYPE_QUESTION = ChoiceQuestion(
    instructions={
        "question": "What kind of request is the user making?",
        "focus": "Classify the user's intent, not the implementation complexity.",
    },
    criteria={
        "bug_investigation": {
            "what": "Describes broken behavior, an error message, or a test failure to fix",
            "not_for": "Feature additions or questions about the codebase",
            "examples": ["Fix the TypeError in auth.py", "Tests are failing on CI"],
        },
        "feature_request": {
            "what": "Asks for new behavior that doesn't exist yet",
            "not_for": "Fixing existing broken behavior",
            "examples": ["Add dark mode support", "Implement user search endpoint"],
        },
        "question_only": {
            "what": "Asks to understand or see something, not asking for code changes",
            "not_for": "Requests that imply the user wants something built or fixed",
            "examples": ["How does the auth flow work?", "Show me the database schema"],
        },
        "unclear": {
            "what": "Too vague to classify with confidence",
            "not_for": "Clear requests in any of the categories above",
            "examples": ["Help me with this", "Look at the code"],
        },
    },
)

SENSITIVE_TOUCH_QUESTION = NoulQuestion(
    instructions={
        "question": "Does this task touch authentication, billing, secrets, database migrations, or security-critical code?",
        "focus": "Evaluate whether the requested change involves sensitive areas that need extra caution.",
    },
)

COMPLEXITY_QUESTION = ScoreQuestion(
    instructions={
        "question": "How complex is this task to implement?",
        "focus": "Judge the implementation scope, not the domain difficulty.",
    },
    scale=[
        {"what": "Trivial one-liner", "signals": ["Single line change", "Config tweak", "Typo fix"]},
        {"what": "Small focused change", "signals": ["One or two files", "Clear scope", "Straightforward logic"]},
        {"what": "Multi-file refactor", "signals": ["Three or more files", "Cross-cutting change", "Needs test updates"]},
        {"what": "Architectural change", "signals": ["New abstractions", "Interface redesign", "Breaking changes possible"]},
    ],
)

NEEDS_TESTS_QUESTION = NoulQuestion(
    instructions={
        "question": "Does this task require new or modified test cases?",
        "focus": "Consider whether the change adds behavior, fixes a bug, or alters interfaces that tests should cover.",
    },
)

# --- Post-test evaluation: atomic decomposition ---
TESTS_PASSING_QUESTION = NoulQuestion(
    instructions={
        "question": "Did all test cases in `execution.test_summary` pass without errors?",
        "focus": "Look at exit codes and error lines, not warnings.",
    },
)

DIFF_COMPLETE_QUESTION = NoulQuestion(
    instructions={
        "question": "Does the current `diff` satisfy what is asked in `task.step_description`?",
        "focus": "Evaluate whether the changes in `diff.files_changed` address this specific step's objective, not the entire multi-step project.",
    },
)

FAILURE_FIXABLE_QUESTION = NoulQuestion(
    instructions={
        "question": "Is the test failure specific enough that a targeted code fix could resolve it?",
        "focus": "If `execution.test_passed` is true, answer false. A fixable failure has a clear error message pointing to a specific line or assertion.",
    },
)

RETRY_CONCERN_QUESTION = ScoreQuestion(
    instructions={
        "question": "How concerning is the retry situation given `execution.retry_count` vs `execution.max_retries`?",
        "focus": "Judge whether further retries are likely productive.",
    },
    scale=[
        {"what": "Within normal", "signals": ["First or second attempt", "Error is clearly different from previous"]},
        {"what": "Approaching limit", "signals": ["Close to max retries", "Same error pattern repeating"]},
        {"what": "Exceeded reasonable limit", "signals": ["At or past max retries", "No progress across attempts"]},
    ],
)

# --- PR risk gate: structured score levels ---
RISK_QUESTION = ScoreQuestion(
    instructions={
        "question": "How risky is this diff to merge to main?",
        "focus": "Evaluate blast radius, test coverage, and sensitivity of touched code.",
    },
    scale=[
        {
            "what": "Low risk",
            "signals": ["Small, self-contained change", "Well-covered by tests", "No sensitive areas touched"],
        },
        {
            "what": "Medium risk",
            "signals": ["Touches core logic or multiple files", "Tests exist but may not cover edge cases"],
        },
        {
            "what": "High risk",
            "signals": ["Large blast radius", "Touches auth/billing/migrations", "Sparse or no test coverage"],
        },
    ],
)

SAFE_TO_PROCEED_QUESTION = NoulQuestion(
    instructions={
        "question": "Should this change proceed to an automatic PR without human sign-off?",
        "focus": "Consider the risk level, whether tests passed, and whether the diff touches sensitive areas flagged in `sensitive_domain`.",
    },
)

RISK_RANK = {"low": 0, "medium": 1, "high": 2}


def is_question_task(task: str, change_type: str = "") -> bool:
    """Return True if the task is an inquiry/explanation/question rather than a code change request."""
    if change_type == "question_only":
        return True
    clean = task.strip().lower()
    if clean.endswith("?"):
        return True
    question_keywords = (
        "ada apa", "apa ", "apa saja", "apakah", "jelaskan", "bagaimana", "kenapa", "mengapa",
        "siapa", "tolong jelaskan", "tampilkan", "bantu jelaskan", "sebutkan", "daftar", "struktur",
        "ceritakan", "apa isi", "ada file apa", "review",
        "what", "why", "how", "explain", "describe", "overview", "list", "show me", "tell me",
        "is there", "are there", "who", "which",
    )
    return any(clean.startswith(kw) or f" {kw} " in f" {clean} " for kw in question_keywords)


class Orchestrator:
    def __init__(
        self,
        system1: SystemOneClient,
        system2: System2Client,
        config: RunConfig,
        log_fn: Optional[Any] = None,
    ):
        self.s1 = system1
        self.s2 = system2
        self.s2_tag = getattr(self.s2, "provider_name", "claude")
        self.cfg = config
        self.log_fn = log_fn
        self.guidelines_files = get_guideline_files(self.cfg.repo_dir)
        self.guidelines = load_project_guidelines(self.cfg.repo_dir)

        # Index available modular skills (metadata only: name & description)
        from skills import index_skills, select_skill, load_skill_content  # noqa: PLC0415
        self.available_skills = index_skills(self.cfg.repo_dir)
        self.active_skill, clean_task = select_skill(
            self.cfg.task,
            self.available_skills,
            explicit_skill_name=self.cfg.skill,
        )
        self.cfg.task = sanitize_surrogates(clean_task)

        # Prepare System 2 guidelines (project memory + active skill if any)
        base_rules = self.guidelines or self.s2.guidelines or ""
        if self.active_skill:
            skill_text = load_skill_content(self.active_skill, include_references=False)
            self.s2.guidelines = f"{base_rules}\n\n[Active Modular Skill: {self.active_skill.name}]\n{skill_text}".strip()
        elif base_rules:
            self.s2.guidelines = base_rules

        # Continuous Learning: inject relevant learnings into System 2 context
        from memory import (
            load_workspace_memory,
            load_global_memory,
            get_relevant_learnings,
            format_learnings_for_prompt,
        )
        ws_mem = load_workspace_memory(self.cfg.repo_dir)
        gl_mem = load_global_memory()
        all_learnings = ws_mem.learnings + gl_mem.learnings
        if all_learnings:
            from memory import MemoryStore
            combined = MemoryStore(learnings=all_learnings)
            relevant = get_relevant_learnings(combined, task=self.cfg.task)
            memory_block = format_learnings_for_prompt(relevant)
            if memory_block:
                self.s2.guidelines = f"{self.s2.guidelines}\n\n{memory_block}".strip()
                self._log(f"[memory] 🧠 Injected {len(relevant)} learned rule(s) into context")

        self.pinned_files = extract_mentioned_files(self.cfg.task, self.cfg.repo_dir)
        if self.cfg.mode == "plan":
            self.cfg.auto_pr = False

    def _log(self, msg: str) -> None:
        if self.log_fn:
            try:
                self.log_fn(msg)
            except Exception:
                try:
                    clean = msg.encode("ascii", errors="replace").decode("ascii")
                    self.log_fn(clean)
                except Exception:
                    pass
        else:
            try:
                print(msg)
            except UnicodeEncodeError:
                print(msg.encode("ascii", errors="replace").decode("ascii"))

    def run(self) -> List[StepResult]:
        if self.cfg.mode == "plan":
            self._log("[brainfrog] 🧭 Mode: [bold #4EC9B0]PLAN[/bold #4EC9B0] (Eksplorasi codebase & penyusunan rencana)")
        else:
            self._log("[brainfrog] 🔨 Mode: [bold #33D17A]BUILD[/bold #33D17A] (Eksekusi perubahan & pengujian)")

        if self.guidelines_files:
            file_names = ", ".join([f.name for f in self.guidelines_files])
            self._log(f"[brainfrog] 🧠 Injected guidelines & rules: [bold #33D17A]{file_names}[/bold #33D17A]")
        if self.active_skill:
            self._log(f"[brainfrog] 🎯 Skill: [bold #33D17A]{self.active_skill.name}[/bold #33D17A]")
        if self.pinned_files:
            self._log(f"[brainfrog] 📌 Pinned {len(self.pinned_files)} context file(s): {', '.join(self.pinned_files.keys())}")

        scope = self._scope_gate()

        if scope.clarify_message:
            if self.cfg.mode == "build":
                # BUILD mode: never block the user with scope clarification.
                # The REPL user knows their project — use full repo tree and proceed.
                self._log("[system1/jev] scope gate: low confidence in build mode \u2014 using full repo tree")
                scope = ScopeDecision(
                    domain=None,
                    change_type=scope.change_type,
                    focus_tree=_repo_tree(self.cfg.repo_dir),
                    clarify_message=None,
                )
            else:
                self._log(f"[system1/jev:{self.s1.name}] scope gate: not confident enough, asking the user")
                return [StepResult(PlanStep("0", "scope clarification", []), "needs_clarification", 0, scope.clarify_message)]

        domain_label = scope.domain.key if scope.domain else "unscoped"
        self._log(f"[system1/jev:{self.s1.name}] scope gate: domain='{domain_label}' change_type='{scope.change_type}'")

        _is_exec_trigger = any(
            kw in self.cfg.task.lower()
            for kw in ("execute", "jalankan", "eksekusi", "implement", "build", "bangun", "kerjakan", "gass", "gas")
        )
        if scope.change_type == "question_only" and not _is_exec_trigger:
            tree = scope.focus_tree or _repo_tree(self.cfg.repo_dir)
            max_f = 10 if not scope.domain else 30
            focus_files = _read_files_from_tree(self.cfg.repo_dir, tree, max_files=max_f)
            for k in list(focus_files.keys()):
                if len(focus_files[k]) > 4000:
                    focus_files[k] = focus_files[k][:4000] + "\n... (truncated)"
            if self.pinned_files:
                focus_files.update(self.pinned_files)
            # Include overview files if available
            for key_file in (
                "README.md", "readme.md", "modules.json", "package.json",
                "pyproject.toml", "index.html", "main.py"
            ):
                kf = self.cfg.repo_dir / key_file
                if kf.exists() and kf.is_file() and key_file not in focus_files:
                    try:
                        focus_files[key_file] = kf.read_text(encoding="utf-8", errors="replace")[:3000]
                    except Exception:
                        pass

            repo_files = _repo_tree(self.cfg.repo_dir)
            self._log(f"[system2/{self.s2_tag}] answering question / diagnosing ...")
            answer = self.s2.diagnose(self.cfg.task, focus_files, domain_label, repo_tree=repo_files)
            return [StepResult(PlanStep("0", "diagnosis", []), "diagnosed", 0, answer)]

        if self.cfg.mode == "plan":
            # Plan mode: read-only exploration and PRD/plan generation
            tree = scope.focus_tree or _repo_tree(self.cfg.repo_dir)
            max_f = 20 if not scope.domain else 40
            focus_files = _read_files_from_tree(self.cfg.repo_dir, tree, max_files=max_f)
            for k in list(focus_files.keys()):
                if len(focus_files[k]) > 4000:
                    focus_files[k] = focus_files[k][:4000] + "\n... (truncated)"
            if self.pinned_files:
                focus_files.update(self.pinned_files)
            for key_file in (
                "README.md", "readme.md", "modules.json", "package.json",
                "pyproject.toml", "index.html", "main.py"
            ):
                kf = self.cfg.repo_dir / key_file
                if kf.exists() and kf.is_file() and key_file not in focus_files:
                    try:
                        focus_files[key_file] = kf.read_text(encoding="utf-8", errors="replace")[:3000]
                    except Exception:
                        pass

            repo_files = _repo_tree(self.cfg.repo_dir)
            self._log(f"[system2/{self.s2_tag}] exploring codebase & drafting PRD in Plan mode ...")
            plan_data = self.s2.plan_and_prd(
                task=self.cfg.task,
                repo_tree=repo_files,
                focus_files=focus_files,
                pinned_files=self.pinned_files,
            )

            # Generate filename & save plan doc into .brainfrog/plans/
            from plans import save_plan_document, compute_file_hash
            from datetime import datetime

            title = plan_data.get("title", "Rencana Implementasi")
            slug = re.sub(r"[^a-zA-Z0-9_\-]+", "_", title.lower()).strip("_") or "plan"
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            plan_filename = f"{timestamp}_{slug}.md"

            commit_hash = ""
            git_status = ""
            try:
                c_res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.cfg.repo_dir, capture_output=True, text=True)
                if c_res.returncode == 0:
                    commit_hash = c_res.stdout.strip()
                s_res = subprocess.run(["git", "status", "--porcelain"], cwd=self.cfg.repo_dir, capture_output=True, text=True)
                if s_res.returncode == 0:
                    git_status = s_res.stdout
            except Exception:
                pass

            relevant_files = plan_data.get("relevant_files", [])
            file_hashes = {}
            for rf in relevant_files:
                rf_norm = _normalize_rel_path(self.cfg.repo_dir, rf)
                rf_path = self.cfg.repo_dir / rf_norm
                if rf_path.exists() and rf_path.is_file():
                    file_hashes[rf_norm] = compute_file_hash(rf_path)

            metadata = {
                "title": title,
                "goal": plan_data.get("goals", [self.cfg.task])[0] if plan_data.get("goals") else self.cfg.task,
                "is_small_task": plan_data.get("is_small_task", False),
                "acceptance_criteria": plan_data.get("acceptance_criteria", []),
                "assumptions": plan_data.get("assumptions", []),
                "steps": plan_data.get("steps", []),
                "relevant_files": relevant_files,
                "commit_hash": commit_hash,
                "git_status_snapshot": git_status,
                "file_hashes": file_hashes,
                "created_at": datetime.now().isoformat(),
            }

            doc_content = plan_data.get("markdown_doc", "")
            if not doc_content:
                lines = [f"# {title}\n"]
                if plan_data.get("problem"):
                    lines.append(f"## Masalah (Problem)\n{plan_data['problem']}\n")
                if plan_data.get("goals"):
                    lines.append("## Tujuan (Goals)")
                    for g in plan_data["goals"]:
                        lines.append(f"- {g}")
                    lines.append("")
                if plan_data.get("codebase_findings"):
                    lines.append("## Temuan Codebase (Facts)")
                    for f in plan_data["codebase_findings"]:
                        lines.append(f"- {f}")
                    lines.append("")
                if plan_data.get("assumptions"):
                    lines.append("## Asumsi & Usulan (Assumptions)")
                    for a in plan_data["assumptions"]:
                        lines.append(f"- {a}")
                    lines.append("")
                if plan_data.get("acceptance_criteria"):
                    lines.append("## Kriteria Penerimaan (Acceptance Criteria)")
                    for ac in plan_data["acceptance_criteria"]:
                        lines.append(f"- [ ] {ac}")
                    lines.append("")
                if plan_data.get("steps"):
                    lines.append("## Rencana Langkah Implementasi")
                    for s in plan_data["steps"]:
                        if isinstance(s, dict):
                            lines.append(f"### Langkah {s.get('id')}: {s.get('description')}")
                            if s.get("files"):
                                lines.append(f"Files: {', '.join(s['files'])}\n")
                        else:
                            lines.append(f"- {s}")
                doc_content = "\n".join(lines)

            saved_path = save_plan_document(self.cfg.repo_dir, plan_filename, doc_content, metadata=metadata)
            try:
                rel_saved = str(saved_path.relative_to(self.cfg.repo_dir))
            except Exception:
                rel_saved = str(saved_path)
            self._log(f"[brainfrog] 📋 Plan document saved: [bold #4EC9B0]{rel_saved}[/bold #4EC9B0]")

            clarifying = plan_data.get("clarifying_questions", [])
            detail_msg = doc_content
            if clarifying:
                detail_msg += "\n\n### ❓ Pertanyaan Klarifikasi (Material Decisions):\n" + "\n".join([f"- {q}" for q in clarifying])

            return [StepResult(PlanStep("0", title, relevant_files), "planned", 0, detail_msg)]

        # BUILD mode: planning with plan_context handoff
        latest_plan = None
        if self.cfg.mode == "build":
            from plans import get_latest_plan, format_plan_handoff
            latest_plan = get_latest_plan(self.cfg.repo_dir)
            if not self.cfg.plan_context and latest_plan:
                self.cfg.plan_context = format_plan_handoff(latest_plan, self.cfg.repo_dir)

        task_clean = self.cfg.task.strip().lower()
        plan_triggers = {
            "execute from plans.", "execute from plans", "execute from plan", "execute plan",
            "eksekusi plan", "jalankan plan", "run the plan", "run plan", "implement plan",
            "build from plan", "sesuai plan", "gass", "gas", "do it", "lanjutkan", "kerjakan plan",
            "execute", "eksekusi", "jalankan",
        }
        is_direct_plan_exec = (
            task_clean in plan_triggers
            or any(kw in task_clean for kw in ("execute from plan", "execute plan", "jalankan plan", "eksekusi plan", "sesuai plan", "run the plan", "build from plan"))
        )

        steps: List[PlanStep] = []
        if is_direct_plan_exec and latest_plan and latest_plan.steps:
            self._log(f"[brainfrog] 📋 Executing directly from plan: [bold #4EC9B0]{latest_plan.title}[/bold #4EC9B0]")
            for i, s in enumerate(latest_plan.steps):
                if isinstance(s, dict):
                    step_id = str(s.get("id", i + 1))
                    step_desc = s.get("description", "")
                    step_files = s.get("files", [])
                else:
                    step_id = str(i + 1)
                    step_desc = str(s)
                    step_files = []
                steps.append(PlanStep(id=step_id, description=step_desc, files=step_files))

        if not steps:
            # If task is short/ambiguous and plan_context is available, enrich the task
            # with the plan goal so the AI has enough context to generate concrete steps.
            effective_task = self.cfg.task
            if self.cfg.plan_context:
                _execute_keywords = {
                    "execute", "eksekusi", "jalankan", "lakukan", "implement", "implementasikan",
                    "build", "bangun", "kerjakan", "do it", "gass", "gas", "mulai", "start",
                    "proceed", "lanjut", "langsung", "run the plan", "sesuai plan", "plan", "plans",
                }
                task_words = set(self.cfg.task.lower().split())
                is_ambiguous = len(self.cfg.task.strip().split()) <= 10 or bool(task_words & _execute_keywords)
                if is_ambiguous:
                    effective_task = (
                        f"Implement the following plan as described in the plan context below.\n"
                        f"Original user instruction: {self.cfg.task}\n\n"
                        f"[Active Plan Context to Implement]\n{self.cfg.plan_context}"
                    )

            self._log(f"[system2/{self.s2_tag}] planning task via {self.s2.model} (scoped to '{domain_label}') ...")
            try:
                steps = self.s2.plan_task(
                    effective_task,
                    scope.focus_tree,
                    pinned_files=self.pinned_files,
                    plan_context=self.cfg.plan_context if effective_task == self.cfg.task else None,
                )
            except Exception as e:
                if latest_plan and latest_plan.steps:
                    self._log(f"[brainfrog] ⚠️ System 2 planning failed ({e}); falling back to saved plan steps...")
                    for i, s in enumerate(latest_plan.steps):
                        if isinstance(s, dict):
                            steps.append(PlanStep(id=str(s.get("id", i + 1)), description=s.get("description", ""), files=s.get("files", [])))
                        else:
                            steps.append(PlanStep(id=str(i + 1), description=str(s), files=[]))
                else:
                    raise
        for s in steps:
            s.files = [_normalize_rel_path(self.cfg.repo_dir, f) for f in s.files]
        self._log(f"[system2/{self.s2_tag}] plan has {len(steps)} step(s)")

        results: List[StepResult] = []
        for step in steps:
            result = self._run_step(step, sensitive=bool(scope.domain and scope.domain.sensitive))
            results.append(result)
            if result.outcome in ("escalated", "abandoned"):
                break  # stop on real failure/escalation
        return results

    def _scope_gate(self) -> ScopeDecision:
        if not self.cfg.domains:
            change_type = "question_only" if is_question_task(self.cfg.task) else "unclear"
            return ScopeDecision(domain=None, change_type=change_type, focus_tree=_repo_tree(self.cfg.repo_dir))

        criteria = {key: d.description for key, d in self.cfg.domains.items()}
        criteria["unrelated"] = "Doesn't clearly match any of the other listed areas."
        domain_question = ChoiceQuestion(
            instructions={
                "question": "Which part of the codebase is `task.user_prompt` most likely about?",
                "focus": "Match the user's intent to a domain, not the exact file path.",
            },
            criteria=criteria,
        )

        # Structured state: only relevant context, named fields for path references
        state = {
            "task": {
                "user_prompt": self.cfg.task,
            },
            "codebase": {
                "domain_options": list(criteria.keys()),
                "domain_count": len(self.cfg.domains),
            },
        }

        # Speculative fan-out: ask all scope questions in a single Jev request.
        # Questions run in parallel; code decides which answers to use.
        answers = self.s1.decide(state, {
            "likely_domain": domain_question,
            "change_type": CHANGE_TYPE_QUESTION,
            "is_sensitive": SENSITIVE_TOUCH_QUESTION,
            "complexity": COMPLEXITY_QUESTION,
            "needs_tests": NEEDS_TESTS_QUESTION,
        })

        domain_answer = answers["likely_domain"]
        change_type = answers["change_type"].choice or "unclear"
        if is_question_task(self.cfg.task, change_type):
            change_type = "question_only"

        # Tiered confidence logging for debugging and threshold tuning
        sensitive_prob = answers["is_sensitive"].noul or 0.0
        complexity_score = answers["complexity"].score if answers["complexity"].score is not None else "unknown"
        needs_tests_prob = answers["needs_tests"].noul or 0.0
        self._log(
            f"[system1/jev:{self.s1.name}] scope_gate results:\n"
            f"  likely_domain = {domain_answer}\n"
            f"  change_type   = {change_type} (confidence={answers['change_type'].confidence:.2f})\n"
            f"  is_sensitive  = {sensitive_prob:.2f}\n"
            f"  complexity    = {complexity_score} (confidence={answers['complexity'].confidence:.2f})\n"
            f"  needs_tests   = {needs_tests_prob:.2f}"
        )

        threshold = 0.35 if len(self.cfg.domains) == 1 else self.cfg.min_domain_confidence
        low_confidence = domain_answer.confidence < threshold
        unresolved = domain_answer.choice in (None, "unrelated")
        if low_confidence or unresolved:
            if change_type == "question_only":
                # For questions about the repo, do not block the user with scope clarification!
                # Fall back to whole-repo tree so System 2 can answer directly.
                return ScopeDecision(
                    domain=None,
                    change_type=change_type,
                    focus_tree=_repo_tree(self.cfg.repo_dir),
                    clarify_message=None,
                )

            options = ", ".join(self.cfg.domains.keys())
            msg = (
                f"I'm not confident enough about which part of the codebase this is about "
                f"(best guess: '{domain_answer.choice}', confidence {domain_answer.confidence:.2f}). "
                f"Could you say which area this touches? Known areas: {options}. "
                f"Or just add more detail to the request."
            )
            return ScopeDecision(domain=None, change_type=change_type, focus_tree="", clarify_message=msg)

        domain = self.cfg.domains[domain_answer.choice]
        focus_tree = resolve_focus_tree(self.cfg.repo_dir, domain)
        if not focus_tree.strip():
            focus_tree = _repo_tree(self.cfg.repo_dir)
        return ScopeDecision(domain=domain, change_type=change_type, focus_tree=focus_tree)

    def _run_step(self, step: PlanStep, sensitive: bool = False) -> StepResult:
        if self.cfg.mode == "plan":
            raise PermissionError("Eksekusi langkah modifikasi kode dilarang dalam mode Plan.")
        self._log(f"=== Step {step.id}: {step.description} ===")
        file_contents = _read_files(self.cfg.repo_dir, step.files)

        effective_task = self.cfg.task
        if self.cfg.plan_context and (len(self.cfg.task.split()) <= 10 or any(kw in self.cfg.task.lower() for kw in ("plan", "execute", "jalankan", "eksekusi", "build", "gass"))):
            effective_task = f"{self.cfg.task}\n\n[Active Plan Context]\n{self.cfg.plan_context}"

        self._log(f"[system2/{self.s2_tag}] writing code ...")
        new_files = self.s2.write_code(step, effective_task, file_contents, pinned_files=self.pinned_files)
        _write_files(self.cfg.repo_dir, new_files, mode=self.cfg.mode)

        retries = 0
        while True:
            test_proc = _run(self.cfg.test_command, self.cfg.repo_dir)
            passed = test_proc.returncode == 0
            output = (test_proc.stdout or "") + (test_proc.stderr or "")
            self._log(f"[tests] {'PASS' if passed else 'FAIL'} (exit {test_proc.returncode})")

            # Structured state with nested fields for Jev path references
            diff_stat = _diff_stat(self.cfg.repo_dir)
            step_files = list(new_files.keys()) or step.files
            state: Dict[str, Any] = {
                "task": {
                    "original_request": self.cfg.task,
                    "step_description": step.description,
                    "step_id": step.id,
                },
                "execution": {
                    "test_passed": passed,
                    "test_exit_code": test_proc.returncode,
                    "test_summary": output[-800:],
                    "retry_count": retries,
                    "max_retries": self.cfg.max_retries,
                },
                "diff": {
                    "files_changed": step_files,
                    "files_count": len(step_files),
                    "lines_added": diff_stat.get("lines_added", 0),
                    "lines_deleted": diff_stat.get("lines_deleted", 0),
                },
            }

            # Atomic fan-out: 4 narrow questions in 1 Jev request, composed in code
            answers = self.s1.decide(state, {
                "tests_passing": TESTS_PASSING_QUESTION,
                "diff_complete": DIFF_COMPLETE_QUESTION,
                "failure_fixable": FAILURE_FIXABLE_QUESTION,
                "retry_concern": RETRY_CONCERN_QUESTION,
            })

            tests_ok = (answers["tests_passing"].noul or 0.0) > 0.7
            diff_ok = (answers["diff_complete"].noul or 0.0) > 0.6
            fixable = (answers["failure_fixable"].noul or 0.0) > 0.5
            retry_raw = answers["retry_concern"].score
            retry_score = retry_raw if retry_raw is not None else "within_normal"
            retry_exceeded = (
                (isinstance(retry_raw, (int, float)) and retry_raw >= 1.5)
                or str(retry_score).lower() in ("exceeded_reasonable_limit", "2", "high")
            )

            self._log(
                f"[system1/jev:{self.s1.name}] post-test evaluation:\n"
                f"  tests_passing  = {answers['tests_passing'].noul:.2f} (threshold=0.70)\n"
                f"  diff_complete  = {answers['diff_complete'].noul:.2f} (threshold=0.60)\n"
                f"  failure_fixable= {answers['failure_fixable'].noul:.2f} (threshold=0.50)\n"
                f"  retry_concern  = {retry_score} (confidence={answers['retry_concern'].confidence:.2f})"
            )

            # Code-side composition: explicit rules, not a single model choice
            # Step completion: tests passed and diff satisfies step requirements
            # (or clean exit 0 test pass with modified files and non-rejected diff)
            step_diff_ok = diff_ok or (passed and len(step_files) > 0 and (answers["diff_complete"].noul or 0.0) >= 0.40)
            if tests_ok and step_diff_ok:
                visual_fixed = self._run_visual_quality_gate(step, effective_task, new_files)
                if retries > 0 or visual_fixed:
                    self._auto_reflect(effective_task, retries, visual_fixed, output[-500:])
                return self._finalize_pr(step, retries, output, sensitive=sensitive)

            # Can retry if within retry budget and Jev has not flagged retries as futile
            can_retry = not retry_exceeded and retries < self.cfg.max_retries
            if can_retry:
                retries += 1
                if tests_ok and not step_diff_ok:
                    retry_reason = f"diff for step '{step.description}' needs completion"
                    retry_prompt = f"Tests passed, but changes for step '{step.description}' appear incomplete (score={answers['diff_complete'].noul:.2f}). Touched files: {step_files}."
                else:
                    retry_reason = "test failure"
                    retry_prompt = output
                self._log(f"[system2/{self.s2_tag}] reviewing {retry_reason}, attempt {retries}/{self.cfg.max_retries} ...")
                current = _read_files(self.cfg.repo_dir, step_files)
                fixed = self.s2.review_and_fix(effective_task, step, current, retry_prompt)
                _write_files(self.cfg.repo_dir, fixed, mode=self.cfg.mode)
                new_files.update(fixed)
                continue

            if retry_exceeded or retries >= self.cfg.max_retries:
                return StepResult(step, "abandoned", retries, "Jev assessed retry limit exceeded with no progress.")

            return StepResult(
                step,
                "escalated",
                retries,
                f"Stopped for human review after {retries} retr(ies). Last test output:\n{output[-1500:]}",
            )

    def _run_visual_quality_gate(
        self,
        step: PlanStep,
        task: str,
        new_files: Dict[str, str],
    ) -> bool:
        """Visual inspection & anti-slop quality gate for frontend changes.

        Captures a headless viewport screenshot of the rendered HTML entrypoint,
        runs multimodal visual critique via System 2 (Gemini / Claude), and
        automatically applies styling fixes if visual defects or slop are detected.

        Returns True if visual fixes were applied, False otherwise.
        """
        if not hasattr(self.s2, "visual_review_and_fix"):
            return False

        from system2.visual_inspector import (
            find_browser_bin,
            find_html_entrypoint,
            is_frontend_change,
            capture_screenshot,
        )

        # 1. Determine if any frontend files were touched
        all_touched = list(new_files.keys()) or step.files
        try:
            diff_names = _run(["git", "diff", "--name-only"], self.cfg.repo_dir).stdout.split()
            all_touched = list(set(all_touched + diff_names))
        except Exception:
            pass

        if not is_frontend_change(all_touched):
            return False

        # 2. Locate HTML entrypoint
        entrypoint = find_html_entrypoint(self.cfg.repo_dir)
        if not entrypoint:
            return False

        # 3. Locate headless browser (Chrome or Edge)
        browser_bin = find_browser_bin()
        if not browser_bin:
            self._log("[visual-engine] ℹ️ Headless browser (Chrome/Edge) not found. Skipping visual inspection.")
            return False

        # 4. Capture screenshot into .brainfrog/scratch/preview.png
        scratch_dir = self.cfg.repo_dir / ".brainfrog" / "scratch"
        scratch_dir.mkdir(parents=True, exist_ok=True)
        preview_png = scratch_dir / "preview.png"

        try:
            rel_entry = entrypoint.relative_to(self.cfg.repo_dir)
        except Exception:
            rel_entry = entrypoint.name

        self._log(f"[visual-engine] 👁️ Rendering viewport for [bold #33D17A]{rel_entry}[/bold #33D17A] ...")
        captured = capture_screenshot(entrypoint, preview_png, browser_bin=browser_bin)
        if not captured:
            self._log("[visual-engine] ⚠️ Could not capture headless screenshot. Skipping visual gate.")
            return False

        try:
            rel_png = str(preview_png.relative_to(self.cfg.repo_dir))
        except Exception:
            rel_png = str(preview_png)

        self._log(f"[visual-engine] 📸 Screenshot captured: [bold #4EC9B0]{rel_png}[/bold #4EC9B0]")
        self._log(f"[visual-engine] 🔍 Multimodal visual critique & anti-slop inspection via {self.s2_tag} ...")

        # 5. Read relevant frontend files to supply context for fixing
        frontend_files_to_read = []
        for rel in all_touched:
            ext = Path(rel).suffix.lower()
            if ext in (".html", ".htm", ".css", ".js", ".jsx", ".tsx", ".vue"):
                frontend_files_to_read.append(rel)
        if not frontend_files_to_read:
            frontend_files_to_read = [str(rel_entry)]

        current_contents = _read_files(self.cfg.repo_dir, frontend_files_to_read)

        # 6. Call multimodal visual review & fix
        try:
            fixed_files, visual_pass, critique = self.s2.visual_review_and_fix(
                task=task,
                screenshot_path=preview_png,
                file_contents=current_contents,
            )
        except Exception as e:
            self._log(f"[visual-engine] ⚠️ Visual critique skipped ({e})")
            return False

        if visual_pass and not fixed_files:
            self._log(f"[visual-engine] ✅ Visual inspection passed! Layout clean & anti-slop verified.")
            if critique:
                self._log(f"[visual-engine] 💬 Critique: {critique}")
            return False

        # 7. Visual defects found - apply fixes and verify tests
        self._log(f"[visual-engine] ⚠️ Visual critique: {critique}")
        if fixed_files:
            fix_names = ", ".join(fixed_files.keys())
            self._log(f"[visual-engine] 🎨 Applying visual polish to: [bold #33D17A]{fix_names}[/bold #33D17A] ...")
            _write_files(self.cfg.repo_dir, fixed_files, mode=self.cfg.mode)

            # Re-verify test suite still passes
            test_proc = _run(self.cfg.test_command, self.cfg.repo_dir)
            if test_proc.returncode == 0:
                new_files.update(fixed_files)
                # Re-capture verified screenshot
                capture_screenshot(entrypoint, preview_png, browser_bin=browser_bin)
                self._log(f"[visual-engine] ✨ Visual polish verified! Unit tests pass cleanly.")
                return True
            else:
                self._log(f"[visual-engine] ⚠️ Visual fixes broke unit tests. Reverting visual changes to preserve functional correctness.")
                _write_files(self.cfg.repo_dir, current_contents, mode=self.cfg.mode)
                return False
        return False

    def _auto_reflect(
        self,
        task: str,
        retries: int,
        visual_fixed: bool,
        error_summary: str = "",
    ) -> None:
        """Extract lessons learned after a difficult task via System 2 reflection.

        Stores extracted learnings in workspace memory for future context injection.
        """
        try:
            from memory import (
                generate_reflection_prompt,
                load_workspace_memory,
                save_workspace_memory,
                add_learning,
            )

            prompt = generate_reflection_prompt(task, retries, visual_fixed, error_summary)
            self._log("[memory] 🧠 Extracting lessons learned from this session ...")

            # Use System 2 to generate structured reflection
            system_msg = (
                "You are a software engineering mentor. Analyze what went wrong "
                "and extract concise, actionable lessons. Respond in JSON only."
            )
            try:
                raw = self.s2._call(system_msg, prompt, max_tokens=1000)
            except Exception:
                # Fallback: create a generic learning from retries/visual info
                raw = None

            store = load_workspace_memory(self.cfg.repo_dir)
            learned_count = 0

            if raw:
                # Parse JSON response with auto-repair
                try:
                    data = extract_json(raw)
                    for item in data.get("learnings", []):
                        rule = item.get("rule", "").strip()
                        tags = item.get("tags", [])
                        if rule:
                            add_learning(
                                store,
                                rule=rule,
                                source="reflection",
                                context=f"Task: {task[:100]}",
                                tags=tags if tags else None,
                                repo_name=self.cfg.repo_dir.name,
                            )
                            learned_count += 1
                except Exception:
                    pass

            # Fallback: if no structured reflection, store a generic one
            if learned_count == 0 and retries > 0:
                add_learning(
                    store,
                    rule=f"Task '{task[:60]}...' required {retries} retries — double-check test expectations before writing.",
                    source="reflection",
                    context=f"Auto-generated after {retries} retries",
                    repo_name=self.cfg.repo_dir.name,
                )
                learned_count = 1

            if learned_count > 0:
                save_workspace_memory(store, self.cfg.repo_dir)
                self._log(f"[memory] 💾 Stored {learned_count} new lesson(s) in workspace memory")
        except Exception as e:
            self._log(f"[memory] ⚠️ Auto-reflection failed ({e}), continuing without saving")

    def _finalize_pr(self, step: PlanStep, retries: int, test_output: str, sensitive: bool = False) -> StepResult:
        if self.cfg.mode == "plan":
            raise PermissionError("Pembuatan commit atau pull request dilarang dalam mode Plan.")
        stat = _diff_stat(self.cfg.repo_dir)
        pr_state = {"task": self.cfg.task, "test_passed": True, "sensitive_domain": sensitive, **stat}
        risk_answers = self.s1.decide(
            pr_state,
            {"diff_risk": RISK_QUESTION, "safe_to_proceed": SAFE_TO_PROCEED_QUESTION},
        )
        risk: Answer = risk_answers["diff_risk"]
        safe: Answer = risk_answers["safe_to_proceed"]
        self._log(f"[system1/jev:{self.s1.name}] diff_risk = {risk}, safe_to_proceed = {safe}")
        if sensitive:
            self._log("[orchestrator] this change touches a domain flagged sensitive=true in modules.json")

        changed = _run(["git", "diff", "--name-only"], self.cfg.repo_dir).stdout.split()
        pr_copy = self.s2.draft_pr(self.cfg.task, changed, "PASS" if not test_output.strip() else "PASS (see logs)")

        # Create clean Git commit with auto-generated conventional message
        commit_title = pr_copy.get("title", "").strip() or f"feat: {step.description}"
        commit_body = pr_copy.get("body", "").strip()
        commit_msg = f"{commit_title}\n\n{commit_body}" if commit_body else commit_title

        # Protect .gitignore & purge tracked sensitive directories/caches
        ensure_gitignore_security(self.cfg.repo_dir)
        purge_tracked_sensitive_files(self.cfg.repo_dir)

        try:
            _run(["git", "add", "-A"], self.cfg.repo_dir)

            # 🛡️ Git Secret Guard — Pre-commit scan
            scan = scan_staged_changes(self.cfg.repo_dir)
            if not scan.is_clean:
                unstage_staged_changes(self.cfg.repo_dir)
                self._log("[git/security] 🚨 [bold red]COMMIT & PUSH BLOCKED — SECRET LEAK DETECTED[/bold red]")
                for finding in scan.findings:
                    loc = f"{finding.file_path}:{finding.line_number}" if finding.line_number else finding.file_path
                    self._log(f"[git/security]   • [{finding.severity}] {finding.rule} in [bold yellow]{loc}[/bold yellow]")
                    self._log(f"[git/security]     Snippet: [dim]{finding.redacted_snippet}[/dim]")
                self._log("[git/security] ❌ Aborted commit and push to prevent security incident.")
                return StepResult(step, "escalated", retries, f"Security Violation: {scan.summary}")

            # Capture staged diff summary before committing
            diff_summary = _staged_diff_summary(self.cfg.repo_dir)

            commit_res = _run(["git", "commit", "-m", commit_msg], self.cfg.repo_dir)
            if commit_res.returncode == 0:
                self._log(f"[git] 📦 Committed: [bold #E8E8E8]{commit_title}[/bold #E8E8E8]")
                for breakdown_line in _format_diff_breakdown(diff_summary):
                    self._log(breakdown_line)
        except Exception as e:
            self._log(f"[git] Commit error: {e}")

        # Automatically push changes to remote origin if configured
        try:
            remotes = _run(["git", "remote"], self.cfg.repo_dir).stdout.split()
            if "origin" in remotes:
                cur_branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], self.cfg.repo_dir).stdout.strip()
                if cur_branch and cur_branch != "HEAD":
                    self._log(f"[git] 🚀 Pushing changes to origin/{cur_branch} ...")
                    push_res = _run(["git", "push", "-u", "origin", cur_branch], self.cfg.repo_dir)
                    if push_res.returncode == 0:
                        self._log(f"[git] ✅ Successfully pushed to origin/{cur_branch}")
                    else:
                        push_fallback = _run(["git", "push", "origin", cur_branch], self.cfg.repo_dir)
                        if push_fallback.returncode == 0:
                            self._log(f"[git] ✅ Successfully pushed to origin/{cur_branch}")
                        else:
                            push_err = push_res.stderr.strip() or push_fallback.stderr.strip()
                            self._log(f"[git] ⚠️ Push notice: {push_err}")
        except Exception as e:
            self._log(f"[git] Push error: {e}")

        ceiling = "low" if sensitive else self.cfg.pr_risk_ceiling
        safety_bar = 0.9 if sensitive else 0.7
        if isinstance(risk.score, (int, float)):
            risk_val = int(round(risk.score))
        else:
            risk_val = RISK_RANK.get(str(risk.score).lower(), 2)
        ceiling_val = RISK_RANK.get(str(ceiling).lower(), 0)
        within_ceiling = risk_val <= ceiling_val

        auto_ok = (
            self.cfg.auto_pr
            and not sensitive
            and within_ceiling
            and safe.noul is not None
            and safe.noul >= safety_bar
        )

        if auto_ok:
            self._open_pr(step, pr_copy)
            detail = f"Auto-opened PR: {pr_copy['title']}"
        else:
            safe_noul_str = f"{safe.noul:.2f}" if safe.noul is not None else "N/A"
            safe_conf_str = f"{safe.confidence:.2f}" if safe.confidence is not None else "0.00"
            detail = (
                f"PR drafted but NOT auto-opened (risk={risk.score}, "
                f"safe_to_proceed={safe_noul_str} conf={safe_conf_str}).\n"
                f"Title: {pr_copy['title']}\nBody:\n{pr_copy['body']}"
            )
            self._log("[orchestrator] " + detail)
        return StepResult(step, "opened_pr" if auto_ok else "drafted_pr", retries, detail)

    def _open_pr(self, step: PlanStep, pr_copy: Dict[str, str]) -> None:
        if self.cfg.mode == "plan":
            raise PermissionError("Pembuatan pull request dilarang dalam mode Plan.")
        branch = f"{self.cfg.branch_prefix}{step.id}"
        _run(["git", "checkout", "-b", branch], self.cfg.repo_dir)
        ensure_gitignore_security(self.cfg.repo_dir)
        purge_tracked_sensitive_files(self.cfg.repo_dir)
        _run(["git", "add", "-A"], self.cfg.repo_dir)

        # 🛡️ Git Secret Guard
        scan = scan_staged_changes(self.cfg.repo_dir)
        if not scan.is_clean:
            unstage_staged_changes(self.cfg.repo_dir)
            self._log(f"[git/security] 🚨 PR Creation BLOCKED — secrets detected: {scan.summary}")
            return

        diff_summary = _staged_diff_summary(self.cfg.repo_dir)
        commit_res = _run(["git", "commit", "-m", pr_copy["title"]], self.cfg.repo_dir)
        if commit_res.returncode == 0:
            self._log(f"[git] 📦 Branch committed: [bold #E8E8E8]{pr_copy['title']}[/bold #E8E8E8]")
            for breakdown_line in _format_diff_breakdown(diff_summary):
                self._log(breakdown_line)
        push = _run(["git", "push", "-u", "origin", branch], self.cfg.repo_dir)
        if push.returncode != 0:
            self._log(f"[orchestrator] push failed (no remote configured?): {push.stderr}")
            return
        gh = _run(
            ["gh", "pr", "create", "--title", pr_copy["title"], "--body", pr_copy["body"]],
            self.cfg.repo_dir,
        )
        if gh.returncode != 0:
            self._log(f"[orchestrator] branch pushed, but `gh pr create` failed (is gh installed & authed?): {gh.stderr}")
        else:
            self._log(f"[orchestrator] PR opened: {gh.stdout.strip()}")
