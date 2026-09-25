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
from system2.claude_client import PlanStep, System2Client


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


def _run(cmd: List[str], cwd: Path) -> subprocess.CompletedProcess:
    use_shell = sys.platform == "win32"
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, shell=use_shell)


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


def _diff_stat(repo_dir: Path) -> Dict[str, int]:
    diff = _run(["git", "diff", "--numstat"], repo_dir).stdout
    added, deleted = 0, 0
    for line in diff.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            added += int(parts[0])
            deleted += int(parts[1])
    return {"lines_added": added, "lines_deleted": deleted}


# System 1 gate questions
NEXT_ACTION_QUESTION = ChoiceQuestion(
    instructions="What should the orchestrator do next?",
    criteria={
        "open_pr": "All tests passed cleanly and the diff looks complete.",
        "retry_fix": "Tests failed, but the failure is specific and likely fixable.",
        "escalate_human": "Tests failed repeatedly or the failure seems fundamentally beyond the model.",
        "abandon": "The goal appears unreachable or contradictory with the codebase.",
    },
)

RISK_QUESTION = ScoreQuestion(
    instructions="How risky is this diff to merge to main?",
    scale={
        "low": "Small, self-contained change, well-covered by tests.",
        "medium": "Touches core logic or multiple files, but well-tested.",
        "high": "Large blast radius, touches auth/billing/migrations, or light tests.",
    },
)

SAFE_TO_PROCEED_QUESTION = NoulQuestion(
    instructions="Should this change proceed to an automatic PR without human sign-off?",
)

CHANGE_TYPE_QUESTION = ChoiceQuestion(
    instructions="What kind of request is the user making?",
    criteria={
        "bug_investigation": "Describes broken behavior, an error message, or a test failure to fix.",
        "feature_request": "Asks for new behavior that doesn't exist yet.",
        "question_only": "Asks to understand or be shown something; not clearly asking for a code change.",
        "unclear": "Too vague to tell what's actually being asked for.",
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
        self.cfg.task = clean_task

        # Prepare System 2 guidelines (project memory + active skill if any)
        base_rules = self.guidelines or self.s2.guidelines or ""
        if self.active_skill:
            skill_text = load_skill_content(self.active_skill, include_references=False)
            self.s2.guidelines = f"{base_rules}\n\n[Active Modular Skill: {self.active_skill.name}]\n{skill_text}".strip()
        elif base_rules:
            self.s2.guidelines = base_rules
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
            instructions="Which part of the codebase is this user prompt most likely about?",
            criteria=criteria,
        )
        state = {"user_prompt": self.cfg.task, "domain_options": list(criteria.keys())}
        answers = self.s1.decide(
            state, {"likely_domain": domain_question, "change_type": CHANGE_TYPE_QUESTION}
        )
        domain_answer = answers["likely_domain"]
        change_type = answers["change_type"].choice or "unclear"
        if is_question_task(self.cfg.task, change_type):
            change_type = "question_only"

        self._log(
            f"[system1/jev:{self.s1.name}] likely_domain = {domain_answer}, "
            f"change_type = {change_type}"
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

            state: Dict[str, Any] = {
                "task": self.cfg.task,
                "step": step.description,
                "test_passed": passed,
                "test_summary": output[-800:],
                "retry_count": retries,
                "max_retries": self.cfg.max_retries,
                **_diff_stat(self.cfg.repo_dir),
            }
            answers = self.s1.decide(state, {"next_action": NEXT_ACTION_QUESTION})
            decision = answers["next_action"]
            self._log(f"[system1/jev:{self.s1.name}] next_action = {decision}")

            if decision.choice == "open_pr":
                return self._finalize_pr(step, retries, output, sensitive=sensitive)

            if decision.choice == "retry_fix" and retries < self.cfg.max_retries:
                retries += 1
                self._log(f"[system2/{self.s2_tag}] reviewing failure, attempt {retries}/{self.cfg.max_retries} ...")
                current = _read_files(self.cfg.repo_dir, list(new_files.keys()) or step.files)
                fixed = self.s2.review_and_fix(effective_task, step, current, output)
                _write_files(self.cfg.repo_dir, fixed, mode=self.cfg.mode)
                new_files.update(fixed)
                continue

            if decision.choice == "abandon":
                return StepResult(step, "abandoned", retries, "Jev classified this approach as unrecoverable.")

            return StepResult(
                step,
                "escalated",
                retries,
                f"Stopped for human review after {retries} retr(ies). Last test output:\n{output[-1500:]}",
            )

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

        try:
            _run(["git", "add", "-A"], self.cfg.repo_dir)
            commit_res = _run(["git", "commit", "-m", commit_msg], self.cfg.repo_dir)
            if commit_res.returncode == 0:
                self._log(f"[git] 📦 Committed: [bold #E8E8E8]{commit_title}[/bold #E8E8E8]")
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
        within_ceiling = RISK_RANK.get(risk.score, 2) <= RISK_RANK.get(ceiling, 0)
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
            detail = (
                f"PR drafted but NOT auto-opened (risk={risk.score}, "
                f"safe_to_proceed={safe.noul:.2f} conf={safe.confidence:.2f}).\n"
                f"Title: {pr_copy['title']}\nBody:\n{pr_copy['body']}"
            )
            self._log("[orchestrator] " + detail)
        return StepResult(step, "opened_pr" if auto_ok else "drafted_pr", retries, detail)

    def _open_pr(self, step: PlanStep, pr_copy: Dict[str, str]) -> None:
        if self.cfg.mode == "plan":
            raise PermissionError("Pembuatan pull request dilarang dalam mode Plan.")
        branch = f"{self.cfg.branch_prefix}{step.id}"
        _run(["git", "checkout", "-b", branch], self.cfg.repo_dir)
        _run(["git", "add", "-A"], self.cfg.repo_dir)
        _run(["git", "commit", "-m", pr_copy["title"]], self.cfg.repo_dir)
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
