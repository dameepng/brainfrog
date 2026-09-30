"""BrainFrog CLI: Dual-System Coding Agent (Jev System 1 + Claude System 2).

Equipped with 5 Killer Features:
1. /undo & /diff: Git-native safety net to inspect diffs and revert unwanted AI changes
2. BRAINFROG.md: Project memory and custom rules injected into Claude's prompt
3. @file Context Pinning: Mention @filename in prompts to inject direct file context
4. !command Terminal Passthrough: Execute shell commands inside REPL without leaving
5. /cost & /stats: Transparent token usage and API cost tracker
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

# Ensure UTF-8 output on Windows consoles
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Ensure package root is on sys.path so sibling modules (skills.py, etc.)
# are importable regardless of the working directory from which brainfrog runs.
_PACKAGE_DIR = str(Path(__file__).resolve().parent)
if _PACKAGE_DIR not in sys.path:
    sys.path.insert(0, _PACKAGE_DIR)

from dotenv import load_dotenv
from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

console = Console(legacy_windows=False)
CLI_VERSION = "0.1.0"


def sanitize_surrogates(text: str) -> str:
    """Sanitize unpaired or malformed surrogate characters that break UTF-8 encoders on Windows."""
    if not text:
        return ""
    try:
        text = text.encode("utf-16", "surrogatepass").decode("utf-16", errors="replace")
    except Exception:
        pass
    return text.encode("utf-8", errors="replace").decode("utf-8")


# -------------------------------------------------------------------------
# Design System & UI Color Tokens (BrainFrog TUI Design Guidelines)
# -------------------------------------------------------------------------
COLOR_BG_BASE = "#0A0A0A"
COLOR_BG_SURFACE = "#161A16"
COLOR_FG_PRIMARY = "#E8E8E8"
COLOR_FG_SECONDARY = "#9AA09A"
COLOR_FG_MUTED = "#5C625C"
COLOR_ACCENT = "#33D17A"      # Terminal green
COLOR_SUCCESS = "#33D17A"     # Reuses accent
COLOR_WARNING = "#E3B341"     # Warning amber
COLOR_ERROR = "#E5534B"       # Semantic red
COLOR_INFO = "#58A6FF"        # Semantic blue

# Semantic Symbols (visible even if NO_COLOR=1 or 16-color terminal)
SYM_SUCCESS = "✓"
SYM_ERROR = "✗"
SYM_WARNING = "⚠"
SYM_INFO = "ℹ"
SYM_PROMPT = "▸"
SYM_USER = "›"
SYM_ASSISTANT = "🐸"


def get_layout_dims() -> tuple[int, int, int, int, str]:
    """Calculate unified responsive terminal dimensions, panel width, and left padding.

    Breakpoints:
      cols >= 120 : box_w = min(96, cols - 4), centered
      cols >= 100 : box_w = cols - 8, centered
      cols >= 60  : box_w = cols - 4, centered
      cols >= 40  : box_w = cols - 2, centered
      cols <  40  : box_w = max(18, cols - 2), margin=1
    """
    cols, rows = shutil.get_terminal_size(fallback=(95, 35))
    if cols >= 120:
        box_w = min(96, cols - 4)
    elif cols >= 100:
        box_w = cols - 8
    elif cols >= 60:
        box_w = cols - 4
    elif cols >= 40:
        box_w = cols - 2
    else:
        box_w = max(18, cols - 2)
    # Safety clamp: box_w must never exceed cols - 2 so lines never touch right edge
    box_w = max(18, min(box_w, cols - 2 if cols > 20 else cols))
    margin = max(0, (cols - box_w) // 2)
    pad = " " * margin
    return cols, rows, box_w, margin, pad


def print_banner_box(
    message: str,
    level: str = "info",
    title: Optional[str] = None,
) -> None:
    """Print semantic notification in a thin rounded box aligned with composer."""
    styles = {
        "error":   {"color": COLOR_ERROR,   "sym": SYM_ERROR,   "default_title": "Error"},
        "warning": {"color": COLOR_WARNING, "sym": SYM_WARNING, "default_title": "Warning"},
        "success": {"color": COLOR_SUCCESS, "sym": SYM_SUCCESS, "default_title": "Success"},
        "info":    {"color": COLOR_INFO,    "sym": SYM_INFO,    "default_title": "Info"},
    }
    cfg = styles.get(level, styles["info"])
    box_title = f"{cfg['sym']} {title or cfg['default_title']}"

    cols, rows, box_w, margin, pad = get_layout_dims()

    panel = Panel(
        Text.from_markup(f"[{COLOR_FG_PRIMARY}]{message}[/{COLOR_FG_PRIMARY}]"),
        title=f"[{cfg['color']} bold]{box_title}[/{cfg['color']} bold]",
        title_align="left",
        border_style=cfg["color"],
        box=box.ROUNDED,
        padding=(0, 1),
        width=box_w,
    )
    console.print()
    console.print(Align.center(panel) if cols > 100 else panel)
    console.print()


def detect_shell_display() -> str:
    """Detect active shell formatted for the UI header."""
    if sys.platform == "win32":
        comspec = os.environ.get("COMSPEC", "")
        if "cmd.exe" in comspec.lower():
            return "Windows (cmd)"
        elif "powershell" in comspec.lower() or "pwsh" in comspec.lower():
            return "Windows (pwsh)"
        return "Windows (cmd)"
    else:
        sh = Path(os.environ.get("SHELL", "/bin/bash")).name
        return f"Unix ({sh})"


# -------------------------------------------------------------------------
# Environment & Configuration Resolution
# -------------------------------------------------------------------------
GLOBAL_CONFIG_DIR = Path.home() / ".brainfrog"
GLOBAL_ENV_FILE = GLOBAL_CONFIG_DIR / ".env"
LOCAL_PKG_ENV = Path(__file__).resolve().parent / ".env"


def load_all_envs(repo_dir: Optional[Path] = None) -> None:
    """Load env vars in priority order: repo .env > ~/.brainfrog/.env > package .env."""
    if LOCAL_PKG_ENV.exists():
        load_dotenv(LOCAL_PKG_ENV)
    if GLOBAL_ENV_FILE.exists():
        load_dotenv(GLOBAL_ENV_FILE, override=True)
    if repo_dir and (repo_dir / ".env").exists():
        load_dotenv(repo_dir / ".env", override=True)


def ensure_anthropic_key() -> str:
    """Ensure ANTHROPIC_API_KEY is available; prompt interactively if missing."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key and not key.startswith("sk-ant-..."):
        return key

    print_banner_box(
        "Anthropic API Key not configured.\n"
        "BrainFrog requires Claude (System 2) to plan and generate code.",
        level="warning",
        title="Anthropic Key Missing",
    )
    try:
        entered = console.input(f"  [bold {COLOR_ACCENT}]Enter ANTHROPIC_API_KEY (sk-ant-...): [/bold {COLOR_ACCENT}]").strip()
    except (KeyboardInterrupt, EOFError):
        console.print(f"\n[{COLOR_FG_MUTED}]Cancelled.[/{COLOR_FG_MUTED}]")
        sys.exit(1)

    if not entered:
        print_banner_box("API key cannot be empty.", level="error")
        sys.exit(1)

    GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(GLOBAL_ENV_FILE, "a", encoding="utf-8") as f:
        f.write(f"\nANTHROPIC_API_KEY={entered}\n")
    os.environ["ANTHROPIC_API_KEY"] = entered
    print_banner_box(f"Saved key to {GLOBAL_ENV_FILE}", level="success")
    return entered


# -------------------------------------------------------------------------
# Git & Workspace Detection
# -------------------------------------------------------------------------
def find_git_root(start_path: Optional[Path] = None) -> Path:
    """Walk up parent directories to find the nearest .git root."""
    current = (start_path or Path.cwd()).resolve()
    for p in [current, *current.parents]:
        if (p / ".git").exists():
            return p
    return current


def get_git_branch(repo_dir: Path) -> str:
    """Return the active git branch or 'detached'/'none'."""
    try:
        proc = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception:
        pass
    return "none"


from orchestrator import (
    clean_git_remote_url,
    get_git_remote_url,
    configure_git_remote,
)


def ensure_git_remote(repo_dir: Path) -> None:
    """Check if repository has a git remote; prompt user to configure one if missing."""
    # 1. Initialize git repo if not present
    if not (repo_dir / ".git").exists():
        try:
            subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True, text=True)
            print_banner_box(
                f"Repositori Git baru diinisialisasi untuk folder: [bold {COLOR_ACCENT}]{repo_dir.name}[/bold {COLOR_ACCENT}]",
                level="success",
                title="Git Init",
            )
        except Exception:
            return

    # 2. Check if remote origin already exists; auto-repair if corrupted
    current_raw = get_git_remote_url(repo_dir, "origin")
    if current_raw:
        cleaned = clean_git_remote_url(current_raw)
        if cleaned and cleaned != current_raw:
            configure_git_remote(repo_dir, cleaned, "origin")
        return

    # 3. Prompt user for remote URL
    print_banner_box(
        "Project ini belum terhubung ke remote repository GitHub.\n"
        "Hubungkan remote agar setiap perubahan kode otomatis di-commit rapi dan di-push ke GitHub.\n\n"
        "[dim]Contoh: https://github.com/dameepng/testing-agentic.git[/dim]",
        level="info",
        title="Git Remote Setup",
    )

    cols, rows, box_w, margin, pad = get_layout_dims()
    prompt_str = f"{pad}[bold {COLOR_ACCENT}]▸[/bold {COLOR_ACCENT}] [{COLOR_FG_PRIMARY}]Masukkan Git Remote URL (atau tekan Enter untuk lewati): [/{COLOR_FG_PRIMARY}]"
    try:
        entered = console.input(prompt_str).strip()
    except (KeyboardInterrupt, EOFError):
        entered = ""

    if entered:
        try:
            ok, clean_url = configure_git_remote(repo_dir, entered, "origin")
            if ok:
                branch = get_git_branch(repo_dir)
                if branch in ("none", "master", ""):
                    subprocess.run(["git", "branch", "-M", "main"], cwd=repo_dir, capture_output=True, text=True)

                print_banner_box(
                    f"Remote origin berhasil dikonfigurasi ke:\n[bold {COLOR_ACCENT}]{clean_url}[/bold {COLOR_ACCENT}]\n"
                    "Semua perubahan kode sukses akan otomatis di-commit rapi & di-push ke GitHub.",
                    level="success",
                    title="Git Remote Connected",
                )
            else:
                print_banner_box(f"Gagal menambahkan git remote: URL '{entered}' tidak valid.", level="error", title="Git Error")
        except Exception as e:
            print_banner_box(f"Gagal menambahkan git remote: {e}", level="error", title="Git Error")
    else:
        print_banner_box(
            "Konfigurasi remote dilewati. Perubahan akan disimpan secara lokal saja.\n"
            "Gunakan perintah `/remote <url>` kapan saja untuk menghubungkan remote GitHub.",
            level="info",
            title="Git Remote Skipped",
        )


def detect_default_test_cmd(repo_dir: Path) -> str:
    """Detect plausible test runner for the workspace."""
    if (repo_dir / "gradlew").exists() or (repo_dir / "gradlew.bat").exists():
        return "cmd /c gradlew.bat test" if os.name == "nt" else "./gradlew test"
    if (repo_dir / "package.json").exists():
        return "npm test"
    for test_folder in ("test", "tests"):
        t_dir = repo_dir / test_folder
        if t_dir.exists() and any(t_dir.glob("*.test.js")):
            return f"node --test {test_folder}/"
    if (repo_dir / "pytest.ini").exists() or (repo_dir / "tests").exists():
        return "pytest -q"
    return "cmd /c exit 0" if os.name == "nt" else "true"


# -------------------------------------------------------------------------
# Execution Logic
# -------------------------------------------------------------------------
def execute_task(
    task: str,
    repo_dir: Path,
    backend: str = "jev",
    model: Optional[str] = None,
    claude_model: Optional[str] = None,
    provider: Optional[str] = None,
    test_cmd: Optional[str] = None,
    module_map: Optional[str] = None,
    min_domain_confidence: float = 0.45,
    auto_pr: bool = False,
    pr_risk_ceiling: str = "medium",
    max_retries: int = 3,
    skill: Optional[str] = None,
    mode: str = "build",
    plan_context: Optional[str] = None,
) -> int:
    """Run a single task through the dual-system orchestrator."""
    from config import get_system1
    from modules import load_module_map
    from orchestrator import Orchestrator, RunConfig
    from system2 import System2Client, usage_tracker

    if not (repo_dir / ".git").exists():
        ensure_git_remote(repo_dir)
        if not (repo_dir / ".git").exists():
            print_banner_box(f"{repo_dir} is not a git repository.", level="error", title="Git Error")
            return 1

    if mode == "build" and not plan_context:
        try:
            from plans import get_latest_plan, format_plan_handoff
            lp = get_latest_plan(repo_dir)
            if lp:
                plan_context = format_plan_handoff(lp, repo_dir)
        except Exception:
            pass

    active_test_cmd = test_cmd or detect_default_test_cmd(repo_dir)

    try:
        system1 = get_system1(backend)
    except Exception as e:
        print_banner_box(f"System 1 Error: {e}", level="error")
        return 1

    chosen_model = model or claude_model
    try:
        system2 = System2Client(model=chosen_model, provider=provider)
    except Exception as e:
        print_banner_box(f"System 2 Error: {e}", level="error")
        return 1

    domains = load_module_map(repo_dir, Path(module_map) if module_map else None, task=task)

    if mode == "plan":
        auto_pr = False

    cfg = RunConfig(
        repo_dir=repo_dir,
        task=task,
        test_command=active_test_cmd.split(),
        max_retries=max_retries,
        auto_pr=auto_pr,
        pr_risk_ceiling=pr_risk_ceiling,
        domains=domains,
        min_domain_confidence=min_domain_confidence,
        skill=skill,
        mode=mode,
        plan_context=plan_context,
    )

    cols, rows, box_w, margin, pad = get_layout_dims()
    right_margin = max(0, cols - box_w - margin)

    def log_cli(msg: str) -> None:
        clean = msg.strip()
        if not clean:
            return
        if clean.startswith("=== Step") and clean.endswith("==="):
            step_title = clean.strip("= ").strip()
            step_box = Panel(
                Text(step_title, style=f"bold {COLOR_FG_PRIMARY}", justify="center"),
                box=box.ROUNDED,
                border_style=COLOR_ACCENT,
                padding=(0, 1),
                width=box_w,
            )
            console.print()
            console.print(Align.center(step_box) if cols > 100 else step_box)
            console.print()
        else:
            from rich.padding import Padding
            try:
                txt = Text.from_markup(clean)
            except Exception:
                txt = Text(clean, style=COLOR_FG_MUTED)
            console.print(Padding(txt, (0, right_margin, 0, margin)))

    orchestrator = Orchestrator(
        system1,
        system2,
        cfg,
        log_fn=log_cli,
    )
    try:
        results = orchestrator.run()
    except KeyboardInterrupt:
        print_banner_box("Task interrupted by user.", level="warning")
        return 130
    except Exception as e:
        print_banner_box(f"Orchestration Error: {e}", level="error")
        return 1

    # Render Diagnosis / Question Answer / Plan & PRD or Scope Clarification
    for r in results:
        if r.outcome == "diagnosed" and r.detail:
            ans_panel = Panel(
                Markdown(r.detail),
                title=f"[{COLOR_ACCENT} bold]{SYM_ASSISTANT} BrainFrog ({chosen_model})[/{COLOR_ACCENT} bold]",
                title_align="left",
                box=box.ROUNDED,
                border_style=COLOR_ACCENT,
                padding=(1, 2),
                width=box_w,
            )
            console.print()
            console.print(Align.center(ans_panel) if cols > 100 else ans_panel)
            console.print()
        elif r.outcome == "planned" and r.detail:
            plan_panel = Panel(
                Markdown(r.detail),
                title=f"[bold #4EC9B0]📋 Plan & PRD ({chosen_model})[/bold #4EC9B0]",
                title_align="left",
                box=box.ROUNDED,
                border_style="#4EC9B0",
                padding=(1, 2),
                width=box_w,
            )
            console.print()
            console.print(Align.center(plan_panel) if cols > 100 else plan_panel)
            console.print()
        elif r.outcome == "needs_clarification" and r.detail:
            print_banner_box(r.detail, level="warning", title="Scope Clarification")
        elif r.outcome in ("escalated", "abandoned") and r.detail:
            print_banner_box(r.detail, level="error", title=f"Step {r.step.id} {r.outcome.upper()}")

    # Render Task Summary Table only for multi-step / code planning tasks
    is_question_turn = len(results) == 1 and results[0].outcome in ("diagnosed", "needs_clarification", "planned")
    if not is_question_turn:
        summary_table = Table(
            title=" Task Summary ",
            box=box.ROUNDED,
            border_style=COLOR_FG_MUTED,
            header_style=f"bold {COLOR_ACCENT}",
            show_header=True,
            padding=(0, 1),
            width=box_w,
        )
        summary_table.add_column("Step", style=f"bold {COLOR_FG_PRIMARY}", no_wrap=True)
        summary_table.add_column("Description", style=COLOR_FG_SECONDARY)
        summary_table.add_column("Status", justify="right")
        summary_table.add_column("Retries", justify="right", style=COLOR_FG_MUTED)

        for r in results:
            is_success = r.outcome in ("diagnosed", "opened_pr", "drafted_pr")
            badge = f"[bold {COLOR_SUCCESS}]{SYM_SUCCESS} SUCCESS[/bold {COLOR_SUCCESS}]" if is_success else f"[bold {COLOR_WARNING}]{SYM_WARNING} {r.outcome.upper()}[/bold {COLOR_WARNING}]"
            summary_table.add_row(f"Step {r.step.id}", r.step.description, badge, str(r.retries))

        console.print()
        console.print(Align.center(summary_table) if cols > 100 else summary_table)

    # Turn token & context memory footer (left-aligned with text box)
    task_usage = usage_tracker.reset_task()
    provider_tag = getattr(system2, "provider_name", "claude")
    cost_str = "Google Auth (Active Session)" if provider_tag == "antigravity" else f"Est. Cost: ${task_usage.cost_usd:.4f}"
    ctx_info = usage_tracker.get_context_info(chosen_model)

    if task_usage.total_tokens > 0 or ctx_info["tokens"] > 0:
        from rich.padding import Padding
        cost_line = f"⚡ Turn tokens: {task_usage.input_tokens:,} in / {task_usage.output_tokens:,} out ({task_usage.total_tokens:,} total)  ·  {cost_str}"
        ctx_status_badge = f"[{ctx_info['status_color']}]{ctx_info['bar']}[/{ctx_info['status_color']}] [{ctx_info['status_color']} bold]{ctx_info['percent']}%[/{ctx_info['status_color']} bold] ({ctx_info['tokens_k']}/{ctx_info['limit_k']})"
        ctx_line = f"🧠 Memory Context: {ctx_status_badge}  ·  [{COLOR_FG_MUTED}]Status:[/{COLOR_FG_MUTED}] [{ctx_info['status_color']}]{ctx_info['status_label']}[/{ctx_info['status_color']}]"

        token_txt = Text.from_markup(f"[{COLOR_FG_MUTED}]{cost_line}[/{COLOR_FG_MUTED}]\n{ctx_line}")
        console.print()
        console.print(Padding(token_txt, (0, right_margin, 0, margin)))
        console.print()

        # Alert recommendation if memory context usage reaches high threshold (>=75%)
        if ctx_info["percent"] >= 75.0:
            warn_msg = (
                f"⚠️  Memory context saat ini mencapai {ctx_info['percent']}% ({ctx_info['tokens']:,} / {ctx_info['limit']:,} token).\n"
                "Untuk menjaga akurasi jawaban, mencegah kelupaan instruksi, dan mempercepat respon,\n"
                "disarankan memulai sesi baru dengan mengetik: [bold]/new[/bold] atau [bold]/reset[/bold]"
            )
            print_banner_box(warn_msg, level="warning", title="Memory Context Warning (>75%)")
    else:
        console.print()

    return 0


# -------------------------------------------------------------------------
# Autocomplete & Completer Engine
# -------------------------------------------------------------------------
try:
    from prompt_toolkit.completion import Completer, Completion
    BaseCompleter = Completer
except Exception:
    BaseCompleter = object


SLASH_COMMAND_COMPLETIONS = [
    ("/help", "Show help and command list"),
    ("/plan", "Switch to Plan mode (read-only exploration & PRD planning)"),
    ("/build", "Switch to Build mode with optional last plan context"),
    ("/mode", "Show or toggle active mode (plan | build)"),
    ("/undo", "Revert last change cleanly via Git"),
    ("/diff", "View colored git diff of recent changes"),
    ("/preview", "Capture and inspect headless screenshot of frontend UI"),
    ("/cost", "View session token usage & metrics"),
    ("/rules", "View or create BRAINFROG.md guidelines"),
    ("/learn", "Teach BrainFrog a new rule (e.g. /learn always use dark mode)"),
    ("/memory", "View all learned rules & preferences"),
    ("/forget", "Remove a learned rule by ID (e.g. /forget L-1234567890)"),
    ("/init", "Auto-generate modules.json for stack"),
    ("/status", "Show current workspace & agent status"),
    ("/provider", "Switch AI provider (antigravity: Google Login, claude: Anthropic API)"),
    ("/models", "List available models for active provider"),
    ("/model", "Switch model (e.g. gemini-3.8-flash-high, claude-sonnet-5)"),
    ("/repo", "Switch target workspace repository"),
    ("/remote", "View or configure Git remote origin repository"),
    ("/test-cmd", "Change test command"),
    ("/backend", "Switch System 1 backend (jev|typesafe)"),
    ("/skills", "List all available modular skills"),
    ("/skill", "Activate modular skill (e.g. /skill audit-anti-slop)"),
    ("/domains", "List detected domain modules and paths"),
    ("/auth", "Manage and switch Google accounts for Antigravity"),
    ("/whoami", "Show active Google account & provider info"),
    ("/new", "Start new chat session & reset memory context (0%)"),
    ("/reset", "Reset conversation state and memory context counters"),
    ("/context", "Display live memory context window metrics & visual bar"),
    ("/tokens", "View token usage breakdown and context metrics"),
    ("/clear", "Clear terminal screen"),
    ("/exit", "Exit BrainFrog session"),
]


class BrainFrogCompleter(BaseCompleter):
    def __init__(self, repo_dir_getter) -> None:
        self.repo_dir_getter = repo_dir_getter

    def get_repo_files(self) -> List[Path]:
        repo_dir = self.repo_dir_getter()
        files = []
        ignore = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
        try:
            for p in repo_dir.rglob("*"):
                if any(part in ignore or part.startswith(".") for part in p.parts):
                    continue
                if p.is_file():
                    files.append(p.relative_to(repo_dir))
                if len(files) >= 500:
                    break
        except Exception:
            pass
        return sorted(files, key=lambda x: str(x).lower())

    def get_completions(self, document, complete_event):
        try:
            from prompt_toolkit.completion import Completion
        except Exception:
            return

        text = document.text_before_cursor

        # 1. Skill name autocomplete after "/skill "
        if text.lower().startswith("/skill "):
            prefix = text[len("/skill "):]
            from skills import index_skills
            indexed = index_skills(self.repo_dir_getter())
            options = ["off", "none", "auto", *list(indexed.keys())]
            for opt in options:
                if not prefix or opt.lower().startswith(prefix.lower()):
                    meta = indexed.get(opt)
                    desc = meta.description[:40] + "..." if meta else "Reset to automatic intent detection"
                    yield Completion(
                        opt,
                        start_position=-len(prefix),
                        display=opt,
                        display_meta=desc,
                    )
            return

        # 1b. Slash commands autocomplete
        if text.startswith("/"):
            for cmd, desc in SLASH_COMMAND_COMPLETIONS:
                if cmd.lower().startswith(text.lower()):
                    yield Completion(
                        cmd,
                        start_position=-len(text),
                        display=cmd,
                        display_meta=desc,
                    )
            return

        # 2. @file autocomplete
        at_pos = text.rfind("@")
        if at_pos != -1:
            query = text[at_pos + 1:]
            # Only trigger autocomplete if no whitespace after @
            if " " not in query and "\t" not in query and "\n" not in query:
                for rel_path in self.get_repo_files():
                    path_str = str(rel_path).replace("\\", "/")
                    if query.lower() in path_str.lower():
                        display_token = f"@{path_str}"
                        yield Completion(
                            f"@{path_str} ",
                            start_position=-(len(query) + 1),
                            display=display_token,
                            display_meta=rel_path.name,
                        )


# -------------------------------------------------------------------------
# Provider & Model Selection Catalog
# -------------------------------------------------------------------------
PROVIDER_MODELS = {
    "antigravity": [
        ("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)", "Ultra fast, high reasoning (Default)"),
        ("gemini-3.8-flash-medium", "Gemini 3.8 Flash (Medium)", "Balanced speed & performance"),
        ("gemini-3.7-flash-high", "Gemini 3.7 Flash (High)", "High reasoning"),
        ("gemini-3.1-pro-high", "Gemini 3.1 Pro (High)", "Complex system architecture & deep coding"),
        ("claude-sonnet-4-6", "Claude Sonnet 4.6 (Thinking)", "Anthropic Sonnet via Google Auth"),
        ("claude-opus-4-6-thinking", "Claude Opus 4.6 (Thinking)", "Anthropic Opus via Google Auth"),
        ("gpt-oss-120b-medium", "GPT-OSS 120B (Medium)", "Open weight model"),
    ],
    "claude": [
        ("claude-sonnet-5", "Claude Sonnet 5", "Default recommended model"),
        ("claude-3-5-sonnet-20241022", "Claude 3.5 Sonnet", "Standard Sonnet release"),
        ("claude-3-5-haiku-20241022", "Claude 3.5 Haiku", "Fast & lightweight"),
    ],
}


def _picker(title: str, items: List[tuple], current_id: str, id_col: str = "ID") -> Optional[str]:
    """Reliable cross-platform modal picker with square border (Section 5.7).

    Prints a numbered Rich table with square border, reads a selection, then
    clears the picker block from the terminal so it doesn't clutter chat history.
    """
    cols, rows, box_w, margin, pad = get_layout_dims()

    table = Table(
        title=f" {title} ",
        box=box.SQUARE,
        border_style=COLOR_FG_MUTED,
        header_style=f"bold {COLOR_ACCENT}",
        show_lines=False,
        show_edge=True,
        pad_edge=True,
        caption=f"[{COLOR_FG_MUTED}]1–{len(items)} pilih · enter konfirmasi · q batal[/{COLOR_FG_MUTED}]",
        width=box_w,
    )
    table.add_column("  #", style=f"bold {COLOR_ACCENT}", justify="right", no_wrap=True, width=4)
    table.add_column(id_col, style=f"bold {COLOR_FG_PRIMARY}", no_wrap=True)
    table.add_column("Description", style=COLOR_FG_SECONDARY)
    table.add_column("", justify="center", no_wrap=True, width=10)

    for i, (item_id, item_name, item_desc) in enumerate(items, 1):
        is_active = (item_id == current_id)
        status = f"[bold {COLOR_ACCENT}]● active[/bold {COLOR_ACCENT}]" if is_active else ""
        row_style = f"on {COLOR_BG_SURFACE}" if is_active else None
        table.add_row(f"  {i}", f"{item_name}", item_desc, status, style=row_style)

    console.print()
    console.print(Align.center(table) if cols > 100 else table)
    console.print()

    prompt_text = f"{pad}[bold {COLOR_ACCENT}]▸[/bold {COLOR_ACCENT}] [{COLOR_FG_MUTED}]Pilih (1–{len(items)}) atau q batal:[/{COLOR_FG_MUTED}] "
    try:
        raw = console.input(prompt_text).strip().lower()
    except (KeyboardInterrupt, EOFError):
        raw = "q"

    # Always erase the picker block (table + blank lines + caption + prompt)
    lines_to_erase = len(items) + 8
    _erase_lines(lines_to_erase)

    if not raw or raw in ("q", "0", "cancel", "exit"):
        return None

    if raw.isdigit():
        idx = int(raw)
        if 1 <= idx <= len(items):
            return items[idx - 1][0]
        print_banner_box(f"Pilihan tidak valid: {raw}. Masukkan 1–{len(items)}.", level="error")
        return None

    # Allow direct string match on item_id or item_name
    for item_id, item_name, _ in items:
        if raw == item_id.lower() or raw == item_name.lower():
            return item_id

    print_banner_box(f"Tidak ditemukan: '{raw}'", level="error")
    return None


def _erase_lines(n: int) -> None:
    """Erase the last *n* terminal lines using ANSI escape codes."""
    if sys.platform == "win32":
        # Enable VT processing on Windows — harmless if already enabled
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_ulong()
            kernel32.GetConsoleMode(handle, ctypes.byref(mode))
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        except Exception:
            pass
    ESC = "\033"
    for _ in range(max(0, n)):
        sys.stdout.write(f"{ESC}[1A{ESC}[2K")  # cursor up + erase line
    sys.stdout.flush()


def select_model_interactive(provider: str, current_model: str) -> Optional[str]:
    """Interactive model picker — erases itself after selection."""
    models = PROVIDER_MODELS.get(provider, [])
    if not models:
        console.print(f"[yellow]No model list for provider '{provider}'.[/yellow]")
        return None
    items = [(m_id, m_id, m_desc) for m_id, _m_name, m_desc in models]
    return _picker(f"Models  ·  {provider}", items, current_model, id_col="Model")


def get_active_google_account() -> Optional[str]:
    """Return the currently logged-in Google email for Antigravity, if any."""
    try:
        import auth_manager
        return auth_manager.get_active_account()
    except Exception:
        pass
    try:
        acct_path = Path.home() / ".gemini" / "google_accounts.json"
        if acct_path.exists():
            data = json.loads(acct_path.read_text(encoding="utf-8"))
            active = data.get("active")
            if isinstance(active, str) and active.strip():
                return active.strip()
    except Exception:
        pass
    return None


def select_provider_interactive(current_provider: str) -> Optional[str]:
    """Interactive provider picker — erases itself after selection."""
    google_acct = get_active_google_account()
    google_desc = f"Google Auth ({google_acct}) — no API key, free quota" if google_acct else "Google Auth Login — no API key, free quota"
    items = [
        ("antigravity", "antigravity", google_desc),
        ("claude",      "claude",      "Anthropic API Key — pay per token"),
    ]
    return _picker("Providers", items, current_provider, id_col="Provider")


def select_account_interactive() -> Optional[str]:
    """Interactive Google account picker — erases itself after selection."""
    import auth_manager
    accts = auth_manager.list_accounts()
    active_email = auth_manager.get_active_account() or ""

    items = []
    for a in accts:
        email = a["email"]
        desc = "Tersimpan di Vault (swap instan)" if a["has_creds"] else "Perlu Login Ulang"
        items.append((email, email, desc))
    items.append(("__login_new__", "+ Hubungkan Akun Google Baru...", "Buka browser untuk login OAuth"))

    return _picker("Google Accounts  ·  Antigravity", items, active_email, id_col="Email")


# -------------------------------------------------------------------------
# Interactive REPL
# -------------------------------------------------------------------------
def run_interactive(
    initial_repo: Path,
    backend: str = "jev",
    model: Optional[str] = None,
    claude_model: Optional[str] = None,
    provider: Optional[str] = None,
    test_cmd: Optional[str] = None,
    module_map: Optional[str] = None,
    min_domain_confidence: float = 0.45,
    auto_pr: bool = False,
    pr_risk_ceiling: str = "medium",
    max_retries: int = 3,
    initial_skill: Optional[str] = None,
    initial_mode: str = "build",
) -> None:
    """Full-featured interactive TUI session."""
    from modules import load_module_map
    from orchestrator import load_project_guidelines
    from system2 import usage_tracker, get_system2_provider

    repo_dir = find_git_root(initial_repo)
    active_test_cmd = test_cmd or detect_default_test_cmd(repo_dir)
    active_backend = backend
    active_provider = get_system2_provider(provider)
    if active_provider == "antigravity":
        default_model = os.environ.get("ANTIGRAVITY_MODEL") or os.environ.get("GEMINI_MODEL") or "gemini-3.8-flash-high"
    else:
        default_model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
    active_model = model or claude_model or default_model
    active_skill: Optional[str] = initial_skill
    active_mode: str = initial_mode
    active_plan_context: Optional[str] = None
    if active_mode == "build":
        try:
            from plans import get_latest_plan, format_plan_handoff
            _lp = get_latest_plan(repo_dir)
            active_plan_context = format_plan_handoff(_lp, repo_dir) if _lp else None
        except Exception:
            active_plan_context = None

    app_state = {"status": "Ready", "icon": "●"}

    def get_prompt_tokens():
        """Input prompt symbol for composer box."""
        return [("class:accent", f"{SYM_PROMPT} ")]

    def get_prompt_top_border():
        """Top border for composer box — mathematically aligned with VSplit."""
        cols, rows, box_w, margin, pad = get_layout_dims()
        bar_w = box_w - 2
        mode_badge = f" [PLAN] " if active_mode == "plan" else f" [BUILD] "
        left_line = "──"
        right_len = max(1, bar_w - len(left_line) - len(mode_badge))
        right_line = "─" * right_len
        style_name = "class:mode-plan" if active_mode == "plan" else "class:mode-build"
        border_style = "class:input-border-plan" if active_mode == "plan" else "class:input-border"
        return [
            (border_style, f"{pad}╭{left_line}"),
            (style_name, mode_badge),
            (border_style, f"{right_line}╮"),
        ]

    def get_prompt_bottom_border():
        """Bottom border attached directly under input buffer."""
        cols, rows, box_w, margin, pad = get_layout_dims()
        bar_w = box_w - 2
        border_style = "class:input-border-plan" if active_mode == "plan" else "class:input-border"
        return [(border_style, f"{pad}╰{'─' * bar_w}╯")]

    # Initialize prompt_toolkit session with autocomplete & history
    session = None
    try:
        from prompt_toolkit.shortcuts import PromptSession
        from prompt_toolkit.styles import Style
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.formatted_text import FormattedText

        class SafeFileHistory(FileHistory):
            """FileHistory subclass that safely handles surrogate characters from Windows paste."""
            def store_string(self, string: str) -> None:
                clean_string = sanitize_surrogates(string)
                try:
                    import datetime
                    with open(self.filename, "ab") as f:
                        def write(t: str) -> None:
                            f.write(t.encode("utf-8", errors="replace"))

                        write(f"\n# {datetime.datetime.now()}\n")
                        for line in clean_string.split("\n"):
                            write(f"+{line}\n")
                except Exception:
                    pass

        pt_style = Style.from_dict({
            # Bottom toolbar: blends into base dark background
            "bottom-toolbar": f"noinherit nobold bg:{COLOR_BG_BASE} {COLOR_FG_MUTED}",
            "bottom-toolbar.text": f"bg:{COLOR_BG_BASE}",
            "toolbar-divider": f"#2a2a2a bg:{COLOR_BG_BASE}",
            "toolbar-accent": f"{COLOR_ACCENT} bold bg:{COLOR_BG_BASE}",
            "toolbar-id": f"{COLOR_FG_PRIMARY} bold bg:{COLOR_BG_BASE}",
            "toolbar-dim": f"{COLOR_FG_MUTED} bg:{COLOR_BG_BASE}",
            "toolbar-sep": f"#2a2a2a bg:{COLOR_BG_BASE}",
            "toolbar-ctx-safe": f"#33D17A bg:{COLOR_BG_BASE}",
            "toolbar-ctx-warn": f"#F6D32D bold bg:{COLOR_BG_BASE}",
            "toolbar-ctx-crit": f"#E01E5A bold bg:{COLOR_BG_BASE}",

            # Mode indicators & borders
            "mode-plan": "bg:#173b37 #4EC9B0 bold",
            "mode-build": f"bg:#1f3326 {COLOR_ACCENT} bold",
            "input-border-plan": "#4EC9B0",

            # Input box borders
            "input-border": f"{COLOR_ACCENT}",
            "input-border-dim": f"{COLOR_FG_MUTED}",
            "accent": f"{COLOR_ACCENT} bold",
            "placeholder": f"italic {COLOR_FG_MUTED}",

            # Semantic states
            "error": f"{COLOR_ERROR} bold",
            "warning": f"{COLOR_WARNING} bold",
            "success": f"{COLOR_SUCCESS} bold",
            "info": f"{COLOR_INFO} bold",

            # Completions popup (Section 5.7)
            "completion-menu.completion": f"bg:{COLOR_BG_SURFACE} {COLOR_FG_PRIMARY}",
            "completion-menu.completion.current": f"bg:{COLOR_ACCENT} {COLOR_BG_BASE} bold",
            "completion-menu.meta.completion": f"bg:{COLOR_BG_SURFACE} {COLOR_FG_SECONDARY}",
            "completion-menu.meta.completion.current": f"bg:{COLOR_ACCENT} {COLOR_BG_BASE} bold",
            "scrollbar.background": f"bg:{COLOR_BG_SURFACE}",
            "scrollbar.button": f"bg:{COLOR_ACCENT}",
        })

        kb = KeyBindings()
        from prompt_toolkit.filters import has_completions

        @kb.add("enter", filter=has_completions)
        def _accept_completion_handler(event):
            """Accept active completion on 1st Enter; 2nd Enter sends prompt."""
            b = event.current_buffer
            if b.complete_state:
                if b.complete_state.current_completion:
                    b.apply_completion(b.complete_state.current_completion)
                elif b.complete_state.completions:
                    b.apply_completion(b.complete_state.completions[0])
                else:
                    b.complete_state = None

        @kb.add("c-p")
        def _palette(event):
            event.current_buffer.text = "/help"
            event.current_buffer.validate_and_handle()

        @kb.add("c-n")
        def _new_chat_shortcut(event):
            """Ctrl+N: Instant new chat session & reset memory context."""
            event.current_buffer.text = "/new"
            event.current_buffer.validate_and_handle()

        @kb.add("tab")
        def _tab_handler(event):
            nonlocal active_mode, active_plan_context
            b = event.current_buffer
            if not b.text.strip():
                # Toggle between plan and build modes — let PT redraw the border
                active_mode = "build" if active_mode == "plan" else "plan"
                # When switching TO build, load the latest plan context (same as /build)
                if active_mode == "build":
                    try:
                        from plans import get_latest_plan, format_plan_handoff
                        lp = get_latest_plan(repo_dir)
                        active_plan_context = format_plan_handoff(lp, repo_dir) if lp else None
                    except Exception:
                        active_plan_context = None
                else:
                    active_plan_context = None
                event.app.invalidate()  # Refresh layout; border already reads active_mode
            elif b.complete_state:
                b.complete_next()
            else:
                event.app.current_buffer.start_completion(select_first=True)

        # Block page-up/page-down to prevent scrolling outside the chat area
        @kb.add("pageup")
        def _no_page_up(event):
            pass  # Intentionally block — scroll only in chat history

        @kb.add("pagedown")
        def _no_page_down(event):
            pass  # Intentionally block — scroll only in chat history

        history_file = GLOBAL_CONFIG_DIR / "history.txt"
        GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        session = PromptSession(
            completer=BrainFrogCompleter(lambda: repo_dir),
            history=SafeFileHistory(str(history_file)),
            style=pt_style,
            key_bindings=kb,
            complete_while_typing=True,
            mouse_support=False,  # Prevent mouse-scroll from breaking pinned layout
        )

        is_first_turn = [True]

        # Structure composer into a bounded, centered box with full borders and pinned footer
        try:
            from prompt_toolkit.layout.containers import Window, ConditionalContainer, VSplit
            from prompt_toolkit.layout.controls import FormattedTextControl
            from prompt_toolkit.filters import Always, is_done

            def _get_buf_width():
                """Dynamic buffer width that stays inside the box borders."""
                _, _, box_w, _, _ = get_layout_dims()
                return max(10, box_w - 4)  # subtract │+pad on each side

            def _get_margin_width():
                """Dynamic left margin for centering."""
                return get_layout_dims()[3]

            root = session.app.layout.container
            float_cont = root.children[0].alternative_content
            hsplit = float_cont.content

            if len(hsplit.children) > 1 and hasattr(hsplit.children[1], "content"):
                default_buf_win = hsplit.children[1].content
                default_buf_win.dont_extend_height = Always()
                default_buf_win.width = _get_buf_width

                left_margin_win = Window(width=_get_margin_width, dont_extend_width=True)
                left_border_win = Window(char="│", width=1, style="class:input-border", dont_extend_width=True)
                inner_pad_left = Window(width=1, dont_extend_width=True)
                inner_pad_right = Window(width=1, dont_extend_width=True)
                right_border_win = Window(char="│", width=1, style="class:input-border", dont_extend_width=True)
                right_margin_win = Window()

                vsplit = VSplit([
                    left_margin_win,
                    left_border_win,
                    inner_pad_left,
                    default_buf_win,
                    inner_pad_right,
                    right_border_win,
                    right_margin_win,
                ])
                hsplit.children[1].content = vsplit

            top_border = Window(FormattedTextControl(get_prompt_top_border), height=1, dont_extend_height=True)
            bottom_border = Window(FormattedTextControl(get_prompt_bottom_border), height=1, dont_extend_height=True)
            hsplit.children[0] = top_border
            hsplit.children.append(bottom_border)

            # Ensure bottom_toolbar is always visible without CPR delay
            if len(root.children) > 5 and hasattr(root.children[5], "filter"):
                root.children[5].filter = ~is_done

            # Set fast polling for window resize events
            session.app.terminal_size_polling_interval = 0.25

            # Hook terminal resize: re-render splash cleanly on landing screen
            orig_on_resize = session.app._on_resize
            def _on_resize_handler():
                if is_first_turn[0]:
                    console.clear()
                    c, r = shutil.get_terminal_size(fallback=(95, 35))
                    print_splash(c, r)
                    session.app.renderer.reset()
                orig_on_resize()
            session.app._on_resize = _on_resize_handler
        except Exception:
            pass

    except Exception:
        session = None

    def get_chat_toolbar():
        """Full-width divider + status bar at bottom of the terminal window.

        Responsive breakpoints:
          cols >= 60 : 2-line status bar (model + repo | shortcuts + version)
          cols >= 40 : 1-line compact bar (model · repo · version)
          cols <  40 : minimal (model only)
        """
        cols, rows = shutil.get_terminal_size(fallback=(95, 35))
        # Divider line never wider than cols - 1 to prevent auto-wrap
        divider = f"{'─' * max(1, cols - 1)}\n"

        # Model display name — with mode indicator
        model_parts = active_model.split("-")
        mode_prefix = f"[{active_mode.upper()}] "
        if cols >= 90:
            model_disp = f"{mode_prefix}{active_model}"
        elif cols >= 60:
            short_m = "-".join(model_parts[:3]) if len(model_parts) >= 3 else active_model
            model_disp = f"{mode_prefix}{short_m}"
        elif cols >= 40:
            short_m = "-".join(model_parts[:2]) if len(model_parts) >= 2 else active_model
            model_disp = f"{mode_prefix}{short_m}"
        else:
            model_disp = f"{mode_prefix}{model_parts[0]}" if model_parts else f"{mode_prefix}{active_model}"

        repo_max = max(4, cols // 8)
        repo_disp = (repo_dir.name[:repo_max] + "…") if len(repo_dir.name) > repo_max + 1 else repo_dir.name

        # Context usage metrics for status bar
        ctx = usage_tracker.get_context_info(active_model)
        ctx_tag = f"ctx: {ctx['tokens_k']}/{ctx['limit_k']} ({ctx['percent']}%)"
        ctx_style = "class:toolbar-ctx-safe" if ctx["status"] == "safe" else ("class:toolbar-ctx-warn" if ctx["status"] == "warning" else "class:toolbar-ctx-crit")

        if cols < 40:
            # Ultra-narrow: model + context %
            return [
                ("class:toolbar-divider", divider),
                ("class:toolbar-accent", " ● "),
                ("class:toolbar-id", f"{model_disp} "),
                (ctx_style, f"({ctx['percent']}%)"),
            ]
        elif cols < 60:
            # Narrow (<60 cols): 1-line status bar with model and context tag
            left_plain = f" ● {model_disp} · [{ctx_tag}]"
            right = f"v{CLI_VERSION} "
            gap = max(1, cols - len(left_plain) - len(right) - 1)
            return [
                ("class:toolbar-divider", divider),
                ("class:toolbar-accent", " ● "),
                ("class:toolbar-id", f"{model_disp} · "),
                (ctx_style, f"[{ctx_tag}]"),
                ("class:toolbar-sep", " " * gap),
                ("class:toolbar-dim", right),
            ]
        else:
            # Standard & Wide (>=60 cols): 2-line status bar with context metrics
            left_hints = "  tab mode    ctrl+n new    ctrl+p help    /context"
            right_v = f"v{CLI_VERSION}  "
            gap = max(1, cols - len(left_hints) - len(right_v) - 1)
            line2 = f"{left_hints}{' ' * gap}{right_v}"

            return [
                ("class:toolbar-divider", divider),
                ("class:toolbar-accent", " ● "),
                ("class:toolbar-id", f"{model_disp}  ·  {repo_disp}  ·  "),
                (ctx_style, f"[{ctx_tag}]\n"),
                ("class:toolbar-dim", line2),
            ]

    def print_splash(cols: int, rows: int) -> None:
        """Render splash/welcome screen for empty state (Section 4 & 5.1).

        Responsive breakpoints:
          cols >= 80  : full ASCII logo centered
          cols >= 60  : compact logo (shorter wordmark)
          cols >= 40  : text-only header with model info
          cols <  40  : minimal BrainFrog label
        """
        console.clear()
        _, _, box_w, margin, pad = get_layout_dims()

        tagline = "Tanya BrainFrog apapun soal project ini."
        # Truncate tagline if it wouldn't fit
        if len(tagline) > cols - 4:
            tagline = "Tanya BrainFrog apa saja." if cols >= 35 else "Tanya BrainFrog."

        # Vertical breathing room: fitted so whole UI fits comfortably in viewport without scrolling
        top_pad = max(1, (rows - 16) // 3) if rows >= 20 else 0
        for _ in range(top_pad):
            console.print()

        if cols < 40:
            # Ultra-narrow: minimal single-line label
            console.print(f"[{COLOR_ACCENT}]{SYM_ASSISTANT}[/{COLOR_ACCENT}] [bold]BrainFrog[/bold]")
            console.print(f"[{COLOR_FG_MUTED}]{tagline}[/{COLOR_FG_MUTED}]")
            console.print()
            return

        if cols < 60:
            # Narrow: skip large logo, show compact header
            console.print(f"{pad}[{COLOR_ACCENT}]{SYM_ASSISTANT}[/{COLOR_ACCENT}] [bold {COLOR_FG_PRIMARY}]BrainFrog[/bold {COLOR_FG_PRIMARY}]  [#333333]·[/#333333]  [{COLOR_FG_SECONDARY}]{active_model}[/{COLOR_FG_SECONDARY}]")
            console.print(f"{pad}[{COLOR_FG_MUTED}]{tagline}[/{COLOR_FG_MUTED}]")
            console.print()
            return

        # Logo rendering (cols >= 60)
        # The full logo needs ~47 visible chars width
        logo_w = 47
        if cols >= 80:
            # Full ASCII logo — fits comfortably
            logo_pad = " " * max(0, (cols - logo_w) // 2)
            logo_lines = [
                f"{logo_pad}[#888888]█▀▀▄ █▀▀▄ ▄▀▀▄ ▀█▀ █▄  █[/#888888]   [#E8E8E8]█▀▀ █▀▀▄ [/#E8E8E8][{COLOR_ACCENT}]▄▀ ▀▄[/{COLOR_ACCENT}][#E8E8E8] █▀▀▀[/#E8E8E8]",
                f"{logo_pad}[#888888]█▀▀▄ █▀▀▄ █▀▀█  █  █ ▀▄█[/#888888]   [#E8E8E8]█▀  █▀▀▄ [/#E8E8E8][{COLOR_ACCENT}]█   █[/{COLOR_ACCENT}][#E8E8E8] █ ▀█[/#E8E8E8]",
                f"{logo_pad}[#888888]▀▀▀  ▀  ▀ ▀  ▀ ▀▀▀ ▀   ▀[/#888888]   [#E8E8E8]▀   ▀  ▀ [/#E8E8E8][{COLOR_ACCENT}]▀▄▄▄▀[/{COLOR_ACCENT}][#E8E8E8] ▀▀▀▀[/#E8E8E8]",
            ]
            for line in logo_lines:
                console.print(line)
        else:
            # cols 60-79: compact text logo (no ASCII art — would wrap)
            console.print(Align.center(
                Text.from_markup(
                    f"[{COLOR_ACCENT} bold]{SYM_ASSISTANT}[/{COLOR_ACCENT} bold]  "
                    f"[bold {COLOR_FG_PRIMARY}]B R A I N F R O G[/bold {COLOR_FG_PRIMARY}]"
                ),
                width=cols,
            ))

        console.print()
        tagline_pad = " " * max(0, (cols - len(tagline)) // 2)
        console.print(f"{tagline_pad}[{COLOR_FG_MUTED}]{tagline}[/{COLOR_FG_MUTED}]")
        console.print()

    def print_compact_header(cols: int) -> None:
        """Render 1-line top header when history exists — responsive."""
        console.print()
        cols, rows, box_w, margin, pad = get_layout_dims()
        model_parts = active_model.split("-")
        if cols < 40:
            model_disp = model_parts[0] if model_parts else active_model
        elif cols < 60:
            model_disp = "-".join(model_parts[:2]) if len(model_parts) >= 2 else active_model
        else:
            model_disp = active_model

        mode_color = "#4EC9B0" if active_mode == "plan" else COLOR_ACCENT
        mode_badge = f"[bold {mode_color}][{active_mode.upper()}][/bold {mode_color}]"

        if cols >= 60:
            console.print(
                f"{pad}[{COLOR_ACCENT}]{SYM_ASSISTANT}[/{COLOR_ACCENT}] [bold {COLOR_FG_PRIMARY}]BrainFrog[/bold {COLOR_FG_PRIMARY}]"
                f"  {mode_badge}"
                f"  [#333333]·[/#333333]  [{COLOR_FG_SECONDARY}]{model_disp}[/{COLOR_FG_SECONDARY}]"
                f"  [#333333]·[/#333333]  [{COLOR_FG_MUTED}]{repo_dir.name}[/{COLOR_FG_MUTED}]"
            )
        elif cols >= 40:
            console.print(
                f"{pad}[{COLOR_ACCENT}]{SYM_ASSISTANT}[/{COLOR_ACCENT}] [bold {COLOR_FG_PRIMARY}]BrainFrog[/bold {COLOR_FG_PRIMARY}]"
                f"  {mode_badge}"
                f"  [#333333]·[/#333333]  [{COLOR_FG_SECONDARY}]{model_disp}[/{COLOR_FG_SECONDARY}]"
            )
        else:
            console.print(f"[{COLOR_ACCENT}]{SYM_ASSISTANT}[/{COLOR_ACCENT}] [bold]BrainFrog[/bold] {mode_badge}")
        console.print()

    def show_help() -> None:
        cols, rows, box_w, margin, pad = get_layout_dims()
        table = Table(
            title=" BrainFrog Commands & Shortcuts ",
            box=box.SQUARE,
            border_style=COLOR_FG_MUTED,
            header_style=f"bold {COLOR_ACCENT}",
            width=box_w,
            caption=f"[{COLOR_FG_MUTED}]Tekan enter untuk lanjut atau gunakan tombol pintasan[/{COLOR_FG_MUTED}]",
        )
        table.add_column("Perintah / Tombol", style=f"bold {COLOR_ACCENT}", no_wrap=True)
        table.add_column("Fungsi & Deskripsi", style=COLOR_FG_PRIMARY)
        table.add_column("Kategori", style=COLOR_FG_SECONDARY)

        commands = [
            ("Tab", "Ganti mode sesi Plan / Build (saat input kosong)", "Shortcut"),
            ("Ctrl+N", "Mulai sesi baru & reset memory context ke 0%", "Shortcut"),
            ("Ctrl+P", "Buka bantuan perintah ini", "Shortcut"),
            ("@filename", "Pin konteks file dengan popup pelengkapan otomatis", "Context"),
            ("!command", "Jalankan perintah shell terminal langsung di sesi REPL", "Shell"),
            ("/whoami", "Tampilkan akun Google aktif & info sesi BrainFrog", "Identity"),
            ("/auth [switch|login|list]", "Kelola & ganti akun Google Antigravity (<100ms)", "Identity"),
            ("/plan [task]", "Beralih ke mode Plan (eksplorasi codebase, read-only, PRD)", "Mode"),
            ("/build [task]", "Beralih ke mode Build (eksekusi rencana terakhir & pengujian)", "Mode"),
            ("/mode [plan|build]", "Lihat atau ganti mode sesi (Plan | Build)", "Mode"),
            ("/models", "Pilih model AI aktif dari menu interaktif", "AI Model"),
            ("/provider", "Ganti provider AI (1: Antigravity Google Auth, 2: Claude)", "Provider"),
            ("/undo", "Batalkan perubahan kode terakhir secara bersih via Git", "Safety"),
            ("/diff", "Lihat perbandingan git diff berwarna dari perubahan", "Safety"),
            ("/preview [file|url]", "Render & ambil tangkapan layar headless UI frontend", "Visual"),
            ("/cost, /stats", "Lihat penggunaan token sesi dan estimasi biaya API", "Metrics"),
            ("/skills", "Lihat daftar modular skill dan status aktifnya", "Skills"),
            ("/skill [name]", "Aktifkan atau nonaktifkan modular skill spesifik", "Skills"),
            ("/rules, /memory", "Tampilkan atau buat aturan proyek (BRAINFROG.md)", "Config"),
            ("/status", "Tampilkan status workspace, branch git, dan agen saat ini", "System"),
            ("/repo <path>", "Pindah direktori repositori target workspace", "Workspace"),
            ("/remote [url]", "Lihat atau atur Git remote repository GitHub", "Workspace"),
            ("/test-cmd <cmd>", "Ganti perintah test runner (mis. pytest, npm test)", "Config"),
            ("/backend [name]", "Status backend System 1 (Jev via TypeSafe Cloud API)", "System"),
            ("/domains", "Tampilkan domain modul arsitektur yang terdeteksi", "Architecture"),
            ("/init [stack]", "Auto-generate modules.json (web | node | python)", "Setup"),
            ("/new, /reset", "Mulai sesi baru & reset memory context (0%)", "Session"),
            ("/context, /tokens", "Lihat meteran memory context window & metrik sesi", "Metrics"),
            ("/clear", "Bersihkan layar terminal dan kembali ke tampilan awal", "Session"),
            ("/exit, /quit", "Keluar dari sesi BrainFrog", "Session"),
        ]
        for cmd, desc, cat in commands:
            table.add_row(cmd, desc, cat)

        console.print()
        console.print(Align.center(table) if cols > 100 else table)
        console.print()

    cols, rows = shutil.get_terminal_size(fallback=(95, 35))
    print_splash(cols, rows)
    ensure_git_remote(repo_dir)
    is_first_turn[0] = True

    while True:
        cols, rows = shutil.get_terminal_size(fallback=(95, 35))
        if not is_first_turn[0]:
            print_compact_header(cols)

        try:
            if session:
                prompt = session.prompt(
                    get_prompt_tokens,
                    placeholder="Tanya BrainFrog…",
                    prompt_continuation=lambda w, l, c: [("class:accent", "  ")],
                    bottom_toolbar=get_chat_toolbar,
                    refresh_interval=0,
                ).strip()
            else:
                cols, rows, box_w, margin, pad = get_layout_dims()
                prompt = console.input(f"{pad}[bold {COLOR_ACCENT}]{SYM_PROMPT} [/bold {COLOR_ACCENT}]").strip()
        except (KeyboardInterrupt, EOFError):
            console.print(f"\n  [{COLOR_FG_MUTED}]Sampai jumpa! {SYM_ASSISTANT}[/{COLOR_FG_MUTED}]\n")
            break

        prompt = sanitize_surrogates(prompt)

        console.print()

        if not prompt:
            continue

        is_first_turn[0] = False

        # Shell command passthrough: !cmd or $cmd
        if prompt.startswith("!") or prompt.startswith("$"):
            cmd = prompt[1:].strip()
            if cmd:
                if active_mode == "plan":
                    from plans import is_safe_readonly_command
                    safe, reason = is_safe_readonly_command(cmd)
                    if not safe:
                        print_banner_box(
                            f"Perintah shell ditolak dalam mode Plan:\n{reason}\n\n"
                            "Dalam mode Plan, hanya perintah inspeksi read-only yang diizinkan (misal: !git status, !dir, !cat).\n"
                            "Gunakan `/build` untuk beralih ke mode Build guna menjalankan perintah yang mengubah state.",
                            level="warning",
                            title="Plan Mode Security",
                        )
                        continue
                cols, rows, box_w, margin, pad = get_layout_dims()
                console.print(f"\n{pad}[{COLOR_FG_MUTED}]Menjalankan shell:[/{COLOR_FG_MUTED}] [bold {COLOR_INFO}]{cmd}[/bold {COLOR_INFO}]\n")
                try:
                    subprocess.run(cmd, shell=True, cwd=repo_dir)
                except Exception as e:
                    print_banner_box(f"Gagal menjalankan perintah shell: {e}", level="error")
                console.print()
            continue

        # Slash Commands
        lower = prompt.lower()
        if lower in ("/exit", "/quit", "exit", "quit"):
            console.print(f"\n  [{COLOR_FG_MUTED}]Sampai jumpa! {SYM_ASSISTANT}[/{COLOR_FG_MUTED}]\n")
            break
        elif lower in ("/help", "/?"):
            show_help()
            continue
        elif lower == "/clear":
            console.clear()
            is_first_turn[0] = True
            cols, rows = shutil.get_terminal_size(fallback=(95, 35))
            print_splash(cols, rows)
            continue
        elif lower in ("/new", "/reset"):
            usage_tracker.reset_session()
            console.clear()
            is_first_turn[0] = True
            cols, rows = shutil.get_terminal_size(fallback=(95, 35))
            print_splash(cols, rows)
            ctx = usage_tracker.get_context_info(active_model)
            print_banner_box(
                "Sesi percakapan baru berhasil dimulai!\n"
                f"• Memory context di-reset ke 0 token (0.0% dari {ctx['limit_k']}).\n"
                f"• Model aktif: [bold {COLOR_ACCENT}]{active_model}[/bold {COLOR_ACCENT}] ({active_provider})\n"
                "• Riwayat context window bersih dan siap menerima instruksi baru.",
                level="success",
                title="New Chat Session",
            )
            continue
        elif lower == "/whoami":
            google_acct = get_active_google_account()
            acct_display = f"[bold {COLOR_ACCENT}]{google_acct}[/bold {COLOR_ACCENT}]" if google_acct else "[italic dim]Belum terdeteksi / tidak login[/italic dim]"
            prov_display = f"[bold {COLOR_FG_PRIMARY}]{active_provider}[/bold {COLOR_FG_PRIMARY}]"
            model_display = f"[bold {COLOR_ACCENT}]{active_model}[/bold {COLOR_ACCENT}]"
            mode_display = f"[bold #4EC9B0]PLAN[/bold #4EC9B0]" if active_mode == "plan" else f"[bold {COLOR_ACCENT}]BUILD[/bold {COLOR_ACCENT}]"
            info_msg = (
                f"• Akun Google (Antigravity): {acct_display}\n"
                f"• Provider AI aktif: {prov_display}\n"
                f"• Model AI aktif: {model_display}\n"
                f"• Mode Sesi: {mode_display}\n"
                f"• Repositori aktif: `{repo_dir}`\n\n"
                f"Gunakan `/auth` untuk mengelola atau berpindah akun Google."
            )
            print_banner_box(info_msg, level="info", title="Identitas Akun & Sesi")
            continue
        elif lower.startswith("/auth") or lower.startswith("/account"):
            import auth_manager
            parts = prompt.split(maxsplit=2)
            subcmd = parts[1].strip().lower() if len(parts) > 1 else None
            arg = parts[2].strip() if len(parts) > 2 else None

            if subcmd == "list":
                accts = auth_manager.list_accounts()
                cols, rows, box_w, margin, pad = get_layout_dims()
                table = Table(title=" Google Accounts Vault ", box=box.ROUNDED, border_style=COLOR_FG_MUTED, header_style=f"bold {COLOR_ACCENT}", width=box_w)
                table.add_column("Akun Google (Email)", style=f"bold {COLOR_FG_PRIMARY}")
                table.add_column("Status Vault", style=COLOR_FG_SECONDARY)
                table.add_column("Status Sesi", justify="center")
                for a in accts:
                    is_act = a["is_active"]
                    status = f"[bold {COLOR_ACCENT}]● AKTIF[/bold {COLOR_ACCENT}]" if is_act else f"[{COLOR_FG_MUTED}]Standby[/{COLOR_FG_MUTED}]"
                    v_status = "[bold #33D17A]Tersimpan di Vault[/bold #33D17A]" if a["has_creds"] else "[bold #F6D32D]Perlu Login Ulang[/bold #F6D32D]"
                    r_style = f"on {COLOR_BG_SURFACE}" if is_act else None
                    table.add_row(a["email"], v_status, status, style=r_style)
                console.print()
                console.print(Align.center(table) if cols > 100 else table)
                console.print()
            elif subcmd == "login":
                login_msg = (
                    "Membuka jendela login Antigravity di proses terpisah...\n"
                    "• Silakan pilih & setujui akun Google baru di browser.\n"
                    "• Setelah login selesai, jendela akan tertutup otomatis dan akun disimpan ke Vault."
                )
                print_banner_box(login_msg, level="info", title="Google Login")
                success, msg = auth_manager.login_new_account_flow()
                level = "success" if success else "warning"
                print_banner_box(msg, level=level, title="Google Auth")
            elif subcmd == "switch":
                if arg:
                    success, msg = auth_manager.switch_account(arg)
                    level = "success" if success else "error"
                    print_banner_box(msg, level=level, title="Switch Google Account")
                else:
                    chosen = select_account_interactive()
                    if chosen:
                        if chosen == "__login_new__":
                            login_msg = (
                                "Membuka jendela login Antigravity di proses terpisah...\n"
                                "• Silakan pilih & setujui akun Google baru di browser.\n"
                                "• Setelah login selesai, jendela akan tertutup otomatis dan akun disimpan ke Vault."
                            )
                            print_banner_box(login_msg, level="info", title="Google Login")
                            success, msg = auth_manager.login_new_account_flow()
                            level = "success" if success else "warning"
                            print_banner_box(msg, level=level, title="Google Auth")
                        else:
                            success, msg = auth_manager.switch_account(chosen)
                            level = "success" if success else "error"
                            print_banner_box(msg, level=level, title="Switch Google Account")
            elif subcmd in ("remove", "delete"):
                if arg:
                    success, msg = auth_manager.remove_account(arg)
                    level = "success" if success else "error"
                    print_banner_box(msg, level=level, title="Remove Account")
                else:
                    print_banner_box("Format salah. Gunakan: `/auth remove <email>`", level="warning", title="Auth Remove")
            else:
                # Default: interactive picker
                chosen = select_account_interactive()
                if chosen:
                    if chosen == "__login_new__":
                        print_banner_box("Memulai proses Google OAuth login untuk akun baru...", level="info", title="Google Login")
                        success, msg = auth_manager.login_new_account_flow()
                        level = "success" if success else "warning"
                        print_banner_box(msg, level=level, title="Google Auth")
                    else:
                        success, msg = auth_manager.switch_account(chosen)
                        level = "success" if success else "error"
                        print_banner_box(msg, level=level, title="Switch Google Account")
            continue
        elif lower.startswith("/mode"):
            parts = prompt.split(maxsplit=1)
            if len(parts) > 1:
                target_mode = parts[1].strip().lower()
                if target_mode in ("plan", "build"):
                    active_mode = target_mode
                    if active_mode == "plan":
                        print_banner_box(
                            "Mode sesi beralih ke: [bold #4EC9B0]PLAN[/bold #4EC9B0]\n\n"
                            "• Eksplorasi codebase, tanya jawab, & PRD: [bold #33D17A]Diizinkan[/bold #33D17A]\n"
                            "• Menulis dokumen rencana (.brainfrog/plans/): [bold #33D17A]Diizinkan[/bold #33D17A]\n"
                            "• Edit kode sumber, shell mutatif, git commit/PR: [bold #FF5555]Ditolak[/bold #FF5555]",
                            level="info",
                            title="Session Mode: PLAN",
                        )
                    else:
                        from plans import get_latest_plan, format_plan_handoff, check_plan_staleness
                        lp = get_latest_plan(repo_dir)
                        if lp:
                            active_plan_context = format_plan_handoff(lp, repo_dir)
                            stale, stale_reasons = check_plan_staleness(repo_dir, lp)
                            if stale:
                                print_banner_box(
                                    f"Mode sesi beralih ke: [bold {COLOR_ACCENT}]BUILD[/bold {COLOR_ACCENT}]\n"
                                    f"Rencana terakhir dimuat: [bold]{lp.title}[/bold] (.brainfrog/plans/{lp.filename})\n\n"
                                    f"[bold #F6D32D]⚠️ Peringatan Perubahan Codebase (Stale Plan):[/bold #F6D32D]\n" +
                                    "\n".join([f"  • {r}" for r in stale_reasons]) +
                                    "\n\nAgent akan memeriksa ulang file terkait sebelum mengeksekusi.",
                                    level="warning",
                                    title="Session Mode: BUILD (Stale Plan)",
                                )
                            else:
                                print_banner_box(
                                    f"Mode sesi beralih ke: [bold {COLOR_ACCENT}]BUILD[/bold {COLOR_ACCENT}]\n"
                                    f"Rencana terakhir dimuat: [bold]{lp.title}[/bold] (.brainfrog/plans/{lp.filename})\n"
                                    "Konteks rencana akan digunakan untuk implementasi kode.",
                                    level="success",
                                    title="Session Mode: BUILD",
                                )
                        else:
                            active_plan_context = None
                            print_banner_box(
                                f"Mode sesi beralih ke: [bold {COLOR_ACCENT}]BUILD[/bold {COLOR_ACCENT}]\n"
                                "Tidak ada rencana tersimpan di .brainfrog/plans/. Eksekusi langsung aktif.",
                                level="info",
                                title="Session Mode: BUILD",
                            )
                else:
                    print_banner_box(f"Mode tidak dikenal: '{parts[1]}'. Pilihan: `/mode plan` atau `/mode build`.", level="error", title="Mode Error")
            else:
                desc = (
                    "Eksplorasi codebase, tanya jawab, pembuatan PRD dan rencana implementasi.\n"
                    "Perubahan source code dan shell mutatif dinonaktifkan."
                    if active_mode == "plan"
                    else
                    "Eksekusi implementasi kode, pengujian otomatis, dan pembuatan commit/PR."
                )
                print_banner_box(
                    f"Mode sesi saat ini: [bold {COLOR_ACCENT}]{active_mode.upper()}[/bold {COLOR_ACCENT}]\n{desc}\n\n"
                    "Gunakan `/plan` untuk beralih ke Plan atau `/build` untuk beralih ke Build.",
                    level="info",
                    title="Session Mode",
                )
            continue
        elif lower.startswith("/plan"):
            parts = prompt.split(maxsplit=1)
            active_mode = "plan"
            if len(parts) > 1:
                sub_task = parts[1].strip()
                cols, rows, box_w, margin, pad = get_layout_dims()
                right_margin = max(0, cols - box_w - margin)
                from rich.padding import Padding
                from rich.live import Live
                from rich.spinner import Spinner
                spin = Spinner("dots", text=f" [bold #4EC9B0]Mengeksplorasi codebase & menyusun PRD...[/bold #4EC9B0]", style="#4EC9B0")
                with Live(Padding(spin, (0, right_margin, 0, margin)), console=console, refresh_per_second=12.5, transient=True):
                    execute_task(
                        task=sub_task,
                        repo_dir=repo_dir,
                        backend=active_backend,
                        model=active_model,
                        provider=active_provider,
                        test_cmd=active_test_cmd,
                        module_map=module_map,
                        min_domain_confidence=min_domain_confidence,
                        auto_pr=auto_pr,
                        pr_risk_ceiling=pr_risk_ceiling,
                        max_retries=max_retries,
                        skill=active_skill,
                        mode="plan",
                    )
            else:
                print_banner_box(
                    "Mode sesi beralih ke: [bold #4EC9B0]PLAN[/bold #4EC9B0]\n\n"
                    "• Eksplorasi codebase, tanya jawab, & penyusunan PRD: [bold #33D17A]Diizinkan[/bold #33D17A]\n"
                    "• Penulisan dokumen rencana dalam .brainfrog/plans/: [bold #33D17A]Diizinkan[/bold #33D17A]\n"
                    "• Modifikasi kode sumber, shell mutatif, git commit/PR: [bold #FF5555]Ditolak[/bold #FF5555]\n\n"
                    "Ketikkan apa yang ingin dieksplorasi atau direncanakan.",
                    level="info",
                    title="Mode: PLAN",
                )
            continue
        elif lower.startswith("/build"):
            parts = prompt.split(maxsplit=1)
            active_mode = "build"
            from plans import get_latest_plan, format_plan_handoff, check_plan_staleness
            lp = get_latest_plan(repo_dir)
            is_stale = False
            stale_reasons = []
            if lp:
                active_plan_context = format_plan_handoff(lp, repo_dir)
                is_stale, stale_reasons = check_plan_staleness(repo_dir, lp)
            else:
                active_plan_context = None

            if len(parts) > 1:
                sub_task = parts[1].strip()
                if is_stale:
                    print_banner_box(
                        f"Rencana terakhir dimuat ([bold]{lp.title}[/bold]), tetapi repository telah berubah sejak dibuat:\n" +
                        "\n".join([f"  • {r}" for r in stale_reasons]) +
                        "\n\nAgent akan memeriksa ulang file terkait sebelum mengeksekusi.",
                        level="warning",
                        title="Stale Plan Warning",
                    )
                cols, rows, box_w, margin, pad = get_layout_dims()
                right_margin = max(0, cols - box_w - margin)
                from rich.padding import Padding
                from rich.live import Live
                from rich.spinner import Spinner
                spin = Spinner("dots", text=f" [bold {COLOR_FG_PRIMARY}]Mengeksekusi rencana tugas (Build)...[/bold {COLOR_FG_PRIMARY}]", style=COLOR_ACCENT)
                with Live(Padding(spin, (0, right_margin, 0, margin)), console=console, refresh_per_second=12.5, transient=True):
                    execute_task(
                        task=sub_task,
                        repo_dir=repo_dir,
                        backend=active_backend,
                        model=active_model,
                        provider=active_provider,
                        test_cmd=active_test_cmd,
                        module_map=module_map,
                        min_domain_confidence=min_domain_confidence,
                        auto_pr=auto_pr,
                        pr_risk_ceiling=pr_risk_ceiling,
                        max_retries=max_retries,
                        skill=active_skill,
                        mode="build",
                        plan_context=active_plan_context,
                    )
            else:
                if lp:
                    if is_stale:
                        print_banner_box(
                            f"Mode sesi beralih ke: [bold {COLOR_ACCENT}]BUILD[/bold {COLOR_ACCENT}]\n"
                            f"Rencana terakhir dimuat: [bold]{lp.title}[/bold] (.brainfrog/plans/{lp.filename})\n\n"
                            f"[bold #F6D32D]⚠️ Peringatan Perubahan Codebase:[/bold #F6D32D]\n" +
                            "\n".join([f"  • {r}" for r in stale_reasons]) +
                            "\n\nAgent akan memeriksa ulang file terkait sebelum mengeksekusi langkah.",
                            level="warning",
                            title="Mode: BUILD (Stale Plan)",
                        )
                    else:
                        print_banner_box(
                            f"Mode sesi beralih ke: [bold {COLOR_ACCENT}]BUILD[/bold {COLOR_ACCENT}]\n"
                            f"Rencana terakhir dimuat: [bold]{lp.title}[/bold] (.brainfrog/plans/{lp.filename})\n"
                            "Konteks rencana akan digunakan saat Anda memberikan instruksi implementasi.",
                            level="success",
                            title="Mode: BUILD",
                        )
                else:
                    print_banner_box(
                        f"Mode sesi beralih ke: [bold {COLOR_ACCENT}]BUILD[/bold {COLOR_ACCENT}]\n"
                        "Tidak ada rencana sebelumnya di .brainfrog/plans/. Eksekusi langsung aktif.",
                        level="info",
                        title="Mode: BUILD",
                    )
            continue
        elif lower == "/status":
            branch = get_git_branch(repo_dir)
            remote_url = get_git_remote_url(repo_dir, "origin") or "none (local only)"
            mode_color = "#4EC9B0" if active_mode == "plan" else COLOR_ACCENT
            mode_desc = "Eksplorasi codebase & penyusunan PRD" if active_mode == "plan" else "Eksekusi kode & pengujian"
            from system2.visual_inspector import find_browser_bin
            browser_bin = find_browser_bin()
            browser_label = f"[{COLOR_ACCENT}]{Path(browser_bin).name} (Headless Ready)[/{COLOR_ACCENT}]" if browser_bin else f"[{COLOR_FG_MUTED}]Not detected[/{COLOR_FG_MUTED}]"
            status_text = (
                f"[{COLOR_FG_PRIMARY}]Session Mode:[/{COLOR_FG_PRIMARY}] [bold {mode_color}]{active_mode.upper()}[/bold {mode_color}] ({mode_desc})\n"
                f"[{COLOR_FG_PRIMARY}]Workspace:[/{COLOR_FG_PRIMARY}] [{COLOR_ACCENT}]{repo_dir}[/{COLOR_ACCENT}]\n"
                f"[{COLOR_FG_PRIMARY}]Git Branch:[/{COLOR_FG_PRIMARY}] [{COLOR_INFO}]{branch}[/{COLOR_INFO}]\n"
                f"[{COLOR_FG_PRIMARY}]Git Remote:[/{COLOR_FG_PRIMARY}] [{COLOR_ACCENT}]{remote_url}[/{COLOR_ACCENT}]\n"
                f"[{COLOR_FG_PRIMARY}]AI Provider:[/{COLOR_FG_PRIMARY}] [{COLOR_ACCENT}]{active_provider}[/{COLOR_ACCENT}]\n"
                f"[{COLOR_FG_PRIMARY}]AI Model:[/{COLOR_FG_PRIMARY}] [{COLOR_FG_PRIMARY} bold]{active_model}[/{COLOR_FG_PRIMARY} bold]\n"
                f"[{COLOR_FG_PRIMARY}]Active Skill:[/{COLOR_FG_PRIMARY}] [{COLOR_ACCENT}]{active_skill or 'auto-detect'}[/{COLOR_ACCENT}]\n"
                f"[{COLOR_FG_PRIMARY}]Visual Engine:[/{COLOR_FG_PRIMARY}] {browser_label}\n"
                f"[{COLOR_FG_PRIMARY}]System 1 Backend:[/{COLOR_FG_PRIMARY}] [{COLOR_ACCENT}]Jev ({active_backend})[/{COLOR_ACCENT}]\n"
                f"[{COLOR_FG_PRIMARY}]Test Command:[/{COLOR_FG_PRIMARY}] [{COLOR_FG_MUTED}]{active_test_cmd}[/{COLOR_FG_MUTED}]"
            )
            print_banner_box(status_text, level="info", title="System Status")
            continue
        elif lower.startswith("/remote"):
            parts = prompt.split(maxsplit=1)
            if len(parts) > 1:
                raw_url = parts[1].strip()
                try:
                    ok, clean_url = configure_git_remote(repo_dir, raw_url, "origin")
                    if ok:
                        print_banner_box(
                            f"Git remote origin berhasil dikonfigurasi ke:\n[bold {COLOR_ACCENT}]{clean_url}[/bold {COLOR_ACCENT}]",
                            level="success",
                            title="Git Remote",
                        )
                    else:
                        print_banner_box(f"Gagal mengatur remote: URL '{raw_url}' tidak valid", level="error", title="Git Error")
                except Exception as e:
                    print_banner_box(f"Gagal mengatur remote: {e}", level="error", title="Git Error")
            else:
                current_url = get_git_remote_url(repo_dir, "origin")
                if current_url:
                    print_banner_box(
                        f"Git remote origin saat ini:\n[bold {COLOR_ACCENT}]{current_url}[/bold {COLOR_ACCENT}]",
                        level="info",
                        title="Git Remote",
                    )
                else:
                    ensure_git_remote(repo_dir)
            continue
        elif lower == "/undo":
            if active_mode == "plan":
                print_banner_box(
                    "Perintah `/undo` ditolak dalam mode Plan karena memodifikasi state repositori.\n"
                    "Beralihlah ke mode Build (`/build`) jika ingin membatalkan perubahan kode.",
                    level="warning",
                    title="Plan Mode Safety",
                )
                continue
            # 1. Check uncommitted changes first
            status = subprocess.run(["git", "status", "--porcelain"], cwd=repo_dir, capture_output=True, text=True).stdout.strip()
            if status:
                subprocess.run(["git", "restore", "."], cwd=repo_dir)
                subprocess.run(["git", "clean", "-fd"], cwd=repo_dir)
                print_banner_box("Perubahan uncommitted berhasil dibatalkan!\nWorking directory dipulihkan secara bersih.", level="success", title="Undo Git")
            else:
                # 2. Reset last commit
                log = subprocess.run(["git", "log", "-1", "--oneline"], cwd=repo_dir, capture_output=True, text=True).stdout.strip()
                if log:
                    subprocess.run(["git", "reset", "--hard", "HEAD~1"], cwd=repo_dir, capture_output=True, text=True)
                    print_banner_box(f"Commit berhasil dibatalkan:\n{log}\nRepositori dikembalikan ke commit sebelumnya.", level="success", title="Undo Commit")
                else:
                    print_banner_box("Tidak ada commit atau perubahan untuk di-undo (working tree clean).", level="warning", title="Undo Git")
            continue
        elif lower == "/diff":
            diff = subprocess.run(["git", "diff", "HEAD"], cwd=repo_dir, capture_output=True, text=True).stdout.strip()
            if not diff:
                diff = subprocess.run(["git", "diff", "HEAD~1"], cwd=repo_dir, capture_output=True, text=True).stdout.strip()
            if diff:
                cols, rows, box_w, margin, pad = get_layout_dims()
                diff_panel = Panel(Syntax(diff, "diff", theme="monokai", line_numbers=True), title=" Git Diff ", box=box.ROUNDED, border_style=COLOR_ACCENT, width=box_w)
                console.print()
                console.print(Align.center(diff_panel) if cols > 100 else diff_panel)
                console.print()
            else:
                print_banner_box("Tidak ada perubahan kode yang terdeteksi (working tree clean).", level="info", title="Git Diff")
            continue
        elif lower in ("/cost", "/stats", "/tokens", "/context", "/memory-context"):
            s = usage_tracker.session
            ctx = usage_tracker.get_context_info(active_model)
            cols, rows, box_w, margin, pad = get_layout_dims()
            table = Table(title=" BrainFrog Context & Session Metrics ", box=box.ROUNDED, border_style=COLOR_FG_MUTED, width=box_w)
            table.add_column("Metrik", style=COLOR_INFO)
            table.add_column("Nilai", style=f"bold {COLOR_ACCENT}", justify="right")
            table.add_row("Provider", f"{active_provider}")
            table.add_row("Model", f"{active_model}")
            table.add_row("Context Window Limit", f"{ctx['limit']:,} tokens ({ctx['limit_k']})")
            table.add_row("Active Memory Context", f"{ctx['tokens']:,} tokens ({ctx['percent']}%)")
            table.add_row("Context Visual Bar", f"[{ctx['status_color']}]{ctx['bar']}[/{ctx['status_color']}]")
            table.add_row("Context Health Status", f"[{ctx['status_color']}]{ctx['status_label']}[/]")
            table.add_row("Cumulative In Tokens", f"{s.input_tokens:,}")
            table.add_row("Cumulative Out Tokens", f"{s.output_tokens:,}")
            table.add_row("Total Session Tokens", f"{s.total_tokens:,}")
            table.add_row("API Requests", f"{s.requests_count:,}")
            if active_provider == "antigravity":
                table.add_row("Billing", "Covered by Google Antigravity Auth")
            else:
                table.add_row("Est. Cost (USD)", f"${s.cost_usd:.4f}")
            console.print()
            console.print(Align.center(table) if cols > 100 else table)
            console.print()

            if ctx["percent"] >= 75.0:
                print_banner_box(
                    f"⚠️  Memory context sudah terisi {ctx['percent']}% ({ctx['tokens']:,} / {ctx['limit']:,} token).\n"
                    "Disarankan menjalankan `/new` atau `/reset` untuk memulai sesi baru agar jawaban tetap optimal.",
                    level="warning",
                    title="Memory Context Warning (>75%)",
                )
            continue
        elif lower.startswith("/preview") or lower.startswith("/shot"):
            from system2.visual_inspector import (
                find_browser_bin,
                find_html_entrypoint,
                capture_screenshot,
            )
            browser = find_browser_bin()
            if not browser:
                print_banner_box(
                    "Headless browser tidak ditemukan!\n"
                    "Pastikan Google Chrome atau Microsoft Edge terpasang di sistem Anda.",
                    level="error",
                    title="Visual Engine",
                )
                continue

            parts = prompt.split(maxsplit=1)
            target = None
            do_audit = False
            do_open = False

            if len(parts) > 1 and parts[1].strip():
                arg = parts[1].strip()
                if "audit" in arg.lower():
                    do_audit = True
                    arg = re.sub(r"\baudit\b", "", arg, flags=re.IGNORECASE).strip()
                if "open" in arg.lower():
                    do_open = True
                    arg = re.sub(r"\bopen\b", "", arg, flags=re.IGNORECASE).strip()

                if arg.startswith(("http://", "https://", "file://")):
                    target = arg
                elif arg:
                    cand = repo_dir / arg
                    if cand.exists():
                        target = cand
                    else:
                        print_banner_box(f"Berkas target tidak ditemukan: {arg}", level="error", title="Preview Error")
                        continue

            if not target:
                target = find_html_entrypoint(repo_dir)

            if not target:
                print_banner_box(
                    "Tidak ada berkas HTML entrypoint (index.html) yang ditemukan di repositori.\n"
                    "Gunakan: `/preview path/ke/file.html` atau `/preview http://localhost:3000`",
                    level="warning",
                    title="Preview Target",
                )
                continue

            scratch_dir = repo_dir / ".brainfrog" / "scratch"
            scratch_dir.mkdir(parents=True, exist_ok=True)
            preview_png = scratch_dir / "preview.png"

            print_banner_box(
                f"Mengambil tangkapan layar headless untuk:\n[bold {COLOR_ACCENT}]{target}[/bold {COLOR_ACCENT}] ...",
                level="info",
                title="Visual Preview",
            )
            ok = capture_screenshot(target, preview_png, browser_bin=browser)
            if ok:
                size_kb = preview_png.stat().st_size / 1024
                browser_name = Path(browser).name
                print_banner_box(
                    f"Tangkapan layar viewport berhasil dibuat ({size_kb:.1f} KB)!\n"
                    f"Lokasi: [bold {COLOR_ACCENT}]{preview_png.resolve()}[/bold {COLOR_ACCENT}]\n\n"
                    f"Browser Engine: [dim]{browser_name} (Headless)[/dim]",
                    level="success",
                    title="Visual Preview",
                )
                if do_open and sys.platform == "win32":
                    try:
                        os.startfile(str(preview_png.resolve()))
                    except Exception:
                        pass
                elif not do_audit:
                    print_banner_box(
                        "Ketik `/preview open` untuk membuka gambar di penampil bawaan Windows,\n"
                        "atau `/preview audit` untuk ulasan visual AI.",
                        level="info",
                        title="Tip",
                    )

                if do_audit:
                    print_banner_box(
                        f"Menjalankan visual critique multimodal via {active_provider} ({active_model})...",
                        level="info",
                        title="Visual Audit",
                    )
                    try:
                        from system2 import System2Client
                        s2_engine = System2Client(
                            model=active_model,
                            provider=active_provider,
                            guidelines=load_project_guidelines(repo_dir),
                        )
                        from orchestrator import _read_files
                        f_contents = _read_files(repo_dir, [target.name if isinstance(target, Path) else "index.html", "style.css"])
                        _, v_pass, critique = s2_engine.visual_review_and_fix("Review UI visual composition and styling", preview_png, f_contents)
                        lvl = "success" if v_pass else "warning"
                        print_banner_box(
                            f"Hasil Inspeksi Visual ({'PASS' if v_pass else 'DEFECTS DETECTED'}):\n\n{critique}",
                            level=lvl,
                            title="Visual Critique",
                        )
                    except Exception as exc:
                        print_banner_box(f"Gagal menjalankan visual audit: {exc}", level="error", title="Visual Audit Error")
            else:
                print_banner_box("Gagal mengambil tangkapan layar headless.", level="error", title="Preview Error")
            continue
        elif lower == "/rules":
            from orchestrator import get_guideline_files
            files = get_guideline_files(repo_dir)
            rules = load_project_guidelines(repo_dir)
            if rules:
                file_titles = ", ".join(f.name for f in files)
                cols, rows, box_w, margin, pad = get_layout_dims()
                rules_panel = Panel(
                    Markdown(rules),
                    title=f" Project Rules & Design Guidelines ({file_titles}) ",
                    box=box.ROUNDED,
                    border_style=COLOR_INFO,
                    width=box_w,
                )
                console.print()
                console.print(Align.center(rules_panel) if cols > 100 else rules_panel)
                console.print()
            else:
                print_banner_box("Tidak ditemukan file BRAINFROG.md di proyek ini.", level="warning", title="Project Memory")
                try:
                    create = console.input(f"  [bold {COLOR_ACCENT}]Buat template BRAINFROG.md? (y/n): [/bold {COLOR_ACCENT}]").strip().lower()
                    if create == "y":
                        tmpl = (
                            "# Project Guidelines & Conventions\n\n"
                            "- Code Style: Clean, modern, and self-documenting.\n"
                            "- UI Theme: Dark mode preferred.\n"
                            "- Architecture: Follow clean architecture and single responsibility.\n"
                        )
                        (repo_dir / "BRAINFROG.md").write_text(tmpl, encoding="utf-8")
                        print_banner_box("Template BRAINFROG.md berhasil dibuat!", level="success")
                except Exception:
                    pass
            continue
        # ── /learn — Teach BrainFrog a new rule ──
        elif lower.startswith("/learn"):
            from memory import (
                load_workspace_memory,
                save_workspace_memory,
                load_global_memory,
                save_global_memory,
                add_learning,
            )
            parts = prompt.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                print_banner_box(
                    "Gunakan: `/learn <instruksi>`\n\n"
                    "Contoh:\n"
                    "  /learn Always use CSS Grid instead of float for layout\n"
                    "  /learn Never use inline styles, always use classes\n"
                    "  /learn global: Prefer dark mode for all projects",
                    level="info",
                    title="Learn Usage",
                )
                continue

            rule_text = parts[1].strip()
            is_global = rule_text.lower().startswith("global:")
            if is_global:
                rule_text = rule_text[len("global:"):].strip()
                store = load_global_memory()
                learning = add_learning(store, rule=rule_text, source="explicit")
                save_global_memory(store)
                scope_label = "Global (semua proyek)"
            else:
                store = load_workspace_memory(repo_dir)
                learning = add_learning(store, rule=rule_text, source="explicit", repo_name=repo_dir.name)
                save_workspace_memory(store, repo_dir)
                scope_label = f"Workspace ({repo_dir.name})"

            tag_str = ", ".join(learning.tags)
            print_banner_box(
                f"\U0001f9e0 Learned!\n\n"
                f"Rule: [bold]{learning.rule}[/bold]\n"
                f"Tags: [{COLOR_INFO}]{tag_str}[/{COLOR_INFO}]\n"
                f"Scope: {scope_label}\n"
                f"ID: [dim]{learning.id}[/dim]",
                level="success",
                title="Continuous Learning",
            )
            continue
        # ── /memory — View all learned rules ──
        elif lower == "/memory":
            from memory import load_workspace_memory, load_global_memory
            ws_mem = load_workspace_memory(repo_dir)
            gl_mem = load_global_memory()

            cols, rows, box_w, margin, pad = get_layout_dims()

            if not ws_mem.learnings and not gl_mem.learnings:
                print_banner_box(
                    "Belum ada aturan yang dipelajari.\n\n"
                    "Gunakan `/learn <instruksi>` untuk mengajarkan BrainFrog,\n"
                    "atau biarkan auto-reflection belajar dari retry & visual fixes.",
                    level="info",
                    title="\U0001f9e0 Memory Bank",
                )
                continue

            table = Table(
                title=" \U0001f9e0 BrainFrog Memory Bank ",
                box=box.ROUNDED,
                border_style=COLOR_FG_MUTED,
                header_style=f"bold {COLOR_ACCENT}",
                width=box_w,
            )
            table.add_column("ID", style="dim", width=16)
            table.add_column("Rule", style=f"bold {COLOR_FG_PRIMARY}", ratio=3)
            table.add_column("Tags", style=COLOR_INFO, width=18)
            table.add_column("Source", width=10)
            table.add_column("Hits", justify="right", width=5)
            table.add_column("Scope", width=10)

            for l in ws_mem.learnings:
                table.add_row(
                    l.id, l.rule[:80], ", ".join(l.tags), l.source,
                    str(l.hit_count), f"[bold]workspace[/bold]",
                )
            for l in gl_mem.learnings:
                table.add_row(
                    l.id, l.rule[:80], ", ".join(l.tags), l.source,
                    str(l.hit_count), f"[dim]global[/dim]",
                )

            console.print()
            console.print(Align.center(table) if cols > 100 else table)
            console.print()
            total = len(ws_mem.learnings) + len(gl_mem.learnings)
            console.print(f"  [dim]Total: {total} learned rule(s) | Gunakan /forget <ID> untuk menghapus[/dim]")
            console.print()
            continue
        # ── /forget — Remove a learned rule by ID ──
        elif lower.startswith("/forget"):
            from memory import (
                load_workspace_memory,
                save_workspace_memory,
                load_global_memory,
                save_global_memory,
                remove_learning,
                clear_learnings,
            )
            parts = prompt.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                print_banner_box(
                    "Gunakan:\n"
                    "  /forget <ID>     — Hapus 1 aturan berdasarkan ID\n"
                    "  /forget all      — Hapus semua aturan workspace\n"
                    "  /forget global   — Hapus semua aturan global",
                    level="info",
                    title="Forget Usage",
                )
                continue

            arg = parts[1].strip()
            if arg.lower() == "all":
                store = load_workspace_memory(repo_dir)
                count = clear_learnings(store)
                save_workspace_memory(store, repo_dir)
                print_banner_box(f"\U0001f5d1\ufe0f Menghapus {count} aturan workspace.", level="success", title="Memory Cleared")
            elif arg.lower() == "global":
                store = load_global_memory()
                count = clear_learnings(store)
                save_global_memory(store)
                print_banner_box(f"\U0001f5d1\ufe0f Menghapus {count} aturan global.", level="success", title="Memory Cleared")
            else:
                # Try workspace first, then global
                ws_store = load_workspace_memory(repo_dir)
                if remove_learning(ws_store, arg):
                    save_workspace_memory(ws_store, repo_dir)
                    print_banner_box(f"\U0001f5d1\ufe0f Aturan [{COLOR_ACCENT}]{arg}[/{COLOR_ACCENT}] berhasil dihapus dari workspace.", level="success", title="Forgotten")
                else:
                    gl_store = load_global_memory()
                    if remove_learning(gl_store, arg):
                        save_global_memory(gl_store)
                        print_banner_box(f"\U0001f5d1\ufe0f Aturan [{COLOR_ACCENT}]{arg}[/{COLOR_ACCENT}] berhasil dihapus dari global.", level="success", title="Forgotten")
                    else:
                        print_banner_box(f"Aturan dengan ID '{arg}' tidak ditemukan.", level="warning", title="Not Found")
            continue
        elif lower == "/skills":
            from skills import index_skills
            indexed = index_skills(repo_dir)
            if not indexed:
                print_banner_box("Tidak ada modular skill di .brainfrog/skills", level="warning", title="Skills")
            else:
                cols, rows, box_w, margin, pad = get_layout_dims()
                table = Table(title=" Available Modular Skills ", box=box.ROUNDED, border_style=COLOR_FG_MUTED, header_style=f"bold {COLOR_ACCENT}", width=box_w)
                table.add_column("Skill Name", style=f"bold {COLOR_FG_PRIMARY}")
                table.add_column("Description", style=COLOR_FG_SECONDARY)
                table.add_column("Status", justify="right")
                for s_name, s_meta in indexed.items():
                    is_active = (active_skill == s_name)
                    status = f"[bold {COLOR_ACCENT}]● ACTIVE[/bold {COLOR_ACCENT}]" if is_active else f"[{COLOR_FG_MUTED}]Standby[/{COLOR_FG_MUTED}]"
                    row_style = f"on {COLOR_BG_SURFACE}" if is_active else None
                    table.add_row(s_name, s_meta.description[:80] + "...", status, style=row_style)
                console.print()
                console.print(Align.center(table) if cols > 100 else table)
                console.print()
            continue
        elif lower.startswith("/skill"):
            from skills import index_skills
            indexed = index_skills(repo_dir)
            parts = prompt.split(maxsplit=1)
            if len(parts) > 1:
                arg = parts[1].strip()
                if arg.lower() in ("off", "none", "clear", "auto"):
                    active_skill = None
                    print_banner_box("Skill di-reset ke deteksi intensi otomatis.", level="info", title="Skill Mode")
                elif arg in indexed or arg.lower() in {k.lower(): k for k in indexed}:
                    matched_key = next(k for k in indexed if k.lower() == arg.lower())
                    active_skill = matched_key
                    print_banner_box(f"Modular skill diaktifkan: [bold {COLOR_ACCENT}]{active_skill}[/bold {COLOR_ACCENT}]", level="success", title="Skill Activated")
                else:
                    print_banner_box(f"Skill '{arg}' tidak ditemukan.\nSkill tersedia: {', '.join(indexed.keys()) or 'tidak ada'}", level="error", title="Skill Error")
            else:
                if active_skill:
                    print_banner_box(f"Skill aktif saat ini: [bold {COLOR_ACCENT}]{active_skill}[/bold {COLOR_ACCENT}]\nGunakan `/skill off` untuk kembali ke deteksi otomatis.", level="info", title="Current Skill")
                else:
                    print_banner_box("Mode skill: [dim]deteksi otomatis per prompt[/dim]\nGunakan `/skill [name]` untuk mengunci skill.", level="info", title="Current Skill")
            continue
        elif lower.startswith("/repo"):
            parts = prompt.split(maxsplit=1)
            if len(parts) > 1:
                new_path = Path(parts[1]).resolve()
                if new_path.exists():
                    repo_dir = find_git_root(new_path)
                    active_test_cmd = detect_default_test_cmd(repo_dir)
                    print_banner_box(f"Direktori repositori dipindah ke:\n{repo_dir}", level="success", title="Switch Repo")
                    ensure_git_remote(repo_dir)
                    if active_mode == "build":
                        try:
                            from plans import get_latest_plan, format_plan_handoff
                            _lp = get_latest_plan(repo_dir)
                            active_plan_context = format_plan_handoff(_lp, repo_dir) if _lp else None
                        except Exception:
                            active_plan_context = None
                else:
                    print_banner_box(f"Direktori tidak ditemukan:\n{parts[1]}", level="error", title="Repo Error")
            else:
                print_banner_box(f"Repositori aktif saat ini:\n{repo_dir}", level="info", title="Current Repo")
            continue
        elif lower.startswith("/test-cmd"):
            parts = prompt.split(maxsplit=1)
            if len(parts) > 1:
                active_test_cmd = parts[1].strip()
                print_banner_box(f"Perintah tes diperbarui menjadi:\n`{active_test_cmd}`", level="success", title="Test Command")
            else:
                print_banner_box(f"Perintah tes saat ini:\n`{active_test_cmd}`", level="info", title="Test Command")
            continue
        elif lower.startswith("/backend"):
            parts = prompt.split(maxsplit=1)
            if len(parts) > 1:
                arg = parts[1].strip().lower()
                if arg in ("jev", "typesafe", "auto"):
                    active_backend = arg
                    print_banner_box(f"Backend System 1 disetel ke: [bold {COLOR_ACCENT}]Jev ({active_backend})[/bold {COLOR_ACCENT}]", level="success", title="Backend Switch")
                elif arg == "mock":
                    print_banner_box("Backend mock telah dihapus. BrainFrog sekarang menggunakan Jev (TypeSafe Cloud API) secara penuh.", level="warning", title="Backend")
                else:
                    print_banner_box("Pilihan backend: `jev` atau `typesafe`.", level="warning", title="Backend")
            else:
                print_banner_box(
                    f"Backend System 1 aktif: [bold {COLOR_ACCENT}]Jev ({active_backend})[/bold {COLOR_ACCENT}]\n"
                    "• Engine: TypeSafe System 1 Native API (jev-latest)\n"
                    "• Status: Terhubung dan aktif sebagai pengambil keputusan cepat.",
                    level="info",
                    title="System 1 Jev",
                )
            continue
        elif lower.startswith("/provider"):
            parts = prompt.split(maxsplit=1)
            target_prov = None
            if len(parts) > 1:
                arg = parts[1].strip().lower()
                if arg == "1" or arg in ("gemini", "antigravity", "google"):
                    target_prov = "antigravity"
                elif arg == "2" or arg in ("claude", "anthropic"):
                    target_prov = "claude"
                else:
                    print_banner_box(f"Provider tidak dikenal: {parts[1]}.\nPilihan: 1 (antigravity), 2 (claude)", level="error", title="Provider Error")
                    continue
            else:
                target_prov = select_provider_interactive(active_provider)

            if target_prov:
                if target_prov != active_provider:
                    active_provider = target_prov
                    if active_provider == "antigravity":
                        active_model = "gemini-3.8-flash-high"
                        print_banner_box("Provider AI diganti ke Google Antigravity (Google Auth Login)!\nModel default: gemini-3.8-flash-high", level="success", title="Provider Switched")
                    else:
                        active_model = "claude-sonnet-5"
                        print_banner_box("Provider AI diganti ke Claude (Anthropic API Key)!\nModel default: claude-sonnet-5", level="success", title="Provider Switched")
                else:
                    print_banner_box(f"Provider aktif tetap: [bold {COLOR_ACCENT}]{active_provider}[/bold {COLOR_ACCENT}]", level="info", title="Provider")
            continue
        elif lower == "/models":
            chosen = select_model_interactive(active_provider, active_model)
            if chosen:
                if chosen != active_model:
                    active_model = chosen
                    print_banner_box(f"Model AI aktif diganti ke:\n[bold {COLOR_ACCENT}]{active_model}[/bold {COLOR_ACCENT}]", level="success", title="Model Switched")
                else:
                    print_banner_box(f"Model aktif tetap:\n[bold {COLOR_ACCENT}]{active_model}[/bold {COLOR_ACCENT}]", level="info", title="Model")
            continue
        elif lower.startswith("/model"):
            parts = prompt.split(maxsplit=1)
            if len(parts) > 1:
                arg = parts[1].strip()
                models = PROVIDER_MODELS.get(active_provider, [])
                if arg.isdigit():
                    idx = int(arg)
                    if 1 <= idx <= len(models):
                        active_model = models[idx - 1][0]
                        print_banner_box(f"Model AI aktif diganti ke:\n[bold {COLOR_ACCENT}]{active_model}[/bold {COLOR_ACCENT}]", level="success", title="Model Switched")
                    else:
                        print_banner_box(f"Angka tidak valid: {arg}. Pilihan: 1–{len(models)}.", level="error", title="Model Error")
                else:
                    active_model = arg
                    print_banner_box(f"Model AI aktif diganti ke:\n[bold {COLOR_ACCENT}]{active_model}[/bold {COLOR_ACCENT}]", level="success", title="Model Switched")
            else:
                chosen = select_model_interactive(active_provider, active_model)
                if chosen:
                    if chosen != active_model:
                        active_model = chosen
                        print_banner_box(f"Model AI aktif diganti ke:\n[bold {COLOR_ACCENT}]{active_model}[/bold {COLOR_ACCENT}]", level="success", title="Model Switched")
                    else:
                        print_banner_box(f"Model aktif tetap:\n[bold {COLOR_ACCENT}]{active_model}[/bold {COLOR_ACCENT}]", level="info", title="Model")
            continue
        elif lower == "/domains":
            domains = load_module_map(repo_dir, Path(module_map) if module_map else None, auto_create=False)
            cols, rows, box_w, margin, pad = get_layout_dims()
            table = Table(title=" Configured Scope Domains ", box=box.ROUNDED, border_style=COLOR_FG_MUTED, header_style=f"bold {COLOR_ACCENT}", width=box_w)
            table.add_column("Domain", style=COLOR_INFO)
            table.add_column("Description", style=COLOR_FG_PRIMARY)
            table.add_column("Paths", style=COLOR_FG_MUTED)
            table.add_column("Sensitive", style=COLOR_WARNING)
            for k, d in domains.items():
                table.add_row(k, d.description, ", ".join(d.paths) or "(all)", "YES" if d.sensitive else "no")
            console.print()
            console.print(Align.center(table) if cols > 100 else table)
            console.print()
            continue
        elif lower.startswith("/init"):
            if active_mode == "plan":
                print_banner_box(
                    "Perintah `/init` ditolak dalam mode Plan karena membuat/mengubah berkas modules.json.\n"
                    "Beralihlah ke mode Build (`/build`) jika ingin menginisialisasi modul arsitektur.",
                    level="warning",
                    title="Plan Mode Safety",
                )
                continue
            from modules import auto_generate_modules_json
            parts = prompt.split(maxsplit=1)
            stack_arg = parts[1].strip().lower() if len(parts) > 1 else None
            stack_map = {"web": "vanilla_web", "vanilla": "vanilla_web", "js": "vanilla_web", "node": "node_web", "react": "node_web"}
            chosen_stack = stack_map.get(stack_arg, stack_arg)
            new_domains = auto_generate_modules_json(repo_dir, stack=chosen_stack)
            print_banner_box(f"Inisialisasi modules.json untuk {repo_dir.name} selesai!\n{len(new_domains)} domain modul arsitektur terdaftar.", level="success", title="Modules Init")
            continue

        # Execute task with live braille spinner aligned with text box
        cols, rows, box_w, margin, pad = get_layout_dims()
        right_margin = max(0, cols - box_w - margin)
        from rich.padding import Padding
        if not session:
            prompt_line = Text.from_markup(f"[{COLOR_FG_SECONDARY}]{SYM_USER}[/{COLOR_FG_SECONDARY}] [bold {COLOR_FG_PRIMARY}]{prompt}[/bold {COLOR_FG_PRIMARY}]")
            console.print()
            console.print(Padding(prompt_line, (0, right_margin, 0, margin)))
            console.print()
        else:
            console.print()

        app_state["status"] = "Processing..."
        app_state["icon"] = "◌"

        from rich.live import Live
        from rich.spinner import Spinner
        spinner_style = "#4EC9B0" if active_mode == "plan" else COLOR_ACCENT
        spinner_text = "Mengeksplorasi codebase & menyusun PRD..." if active_mode == "plan" else "Mengeksekusi rencana tugas..."
        spin = Spinner("dots", text=f" [bold {COLOR_FG_PRIMARY}]{spinner_text}[/bold {COLOR_FG_PRIMARY}]", style=spinner_style)
        with Live(Padding(spin, (0, right_margin, 0, margin)), console=console, refresh_per_second=12.5, transient=True):
            exit_code = execute_task(
                task=prompt,
                repo_dir=repo_dir,
                backend=active_backend,
                model=active_model,
                provider=active_provider,
                test_cmd=active_test_cmd,
                module_map=module_map,
                min_domain_confidence=min_domain_confidence,
                auto_pr=auto_pr,
                pr_risk_ceiling=pr_risk_ceiling,
                max_retries=max_retries,
                skill=active_skill,
                mode=active_mode,
                plan_context=active_plan_context if active_mode == "build" else None,
            )
        if exit_code == 0:
            app_state["status"] = "Done"
            app_state["icon"] = SYM_SUCCESS
        else:
            app_state["status"] = "Failed"
            app_state["icon"] = SYM_ERROR


# -------------------------------------------------------------------------
# Main Entry Point
# -------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(
        prog="brainfrog",
        description="🐸 BrainFrog: Dual-System Coding Agent (Jev System 1 + Claude/Gemini System 2)",
    )
    p.add_argument("task", nargs="?", default=None, help="Task to execute (leave empty for interactive REPL)")
    p.add_argument("--task", dest="flag_task", default=None, help="Alternative flag for task description")
    p.add_argument("-r", "--repo", default=".", help="Path to target git repository (default: current directory)")
    p.add_argument("-t", "--test-cmd", default=None, help="Shell command for running test suite")
    p.add_argument("-b", "--backend", choices=["jev", "typesafe", "auto"], default="jev", help="System 1 decision backend (jev | typesafe)")
    p.add_argument("-m", "--model", "--claude-model", dest="model", default=None, help="Model name (e.g. gemini-3.8-flash-high, claude-sonnet-5)")
    p.add_argument("--provider", choices=["claude", "antigravity", "gemini", "auto"], default=None, help="System 2 AI provider (antigravity: Google Login, claude: Anthropic API)")
    p.add_argument("--skill", default=None, help="Explicitly activate a modular skill (e.g. --skill audit-anti-slop)")
    p.add_argument("--mode", choices=["build", "plan"], default="build", help="Session mode: build (default) or plan")
    p.add_argument("--auto-pr", action="store_true", help="Push branch and open GitHub PR when approved")
    p.add_argument("--pr-risk-ceiling", choices=["low", "medium", "high"], default="medium")
    p.add_argument("--max-retries", type=int, default=3)
    p.add_argument("--module-map", default=None, help="Path to custom modules.json")
    p.add_argument("--min-domain-confidence", type=float, default=0.45)
    p.add_argument("-v", "--version", action="version", version=f"BrainFrog {CLI_VERSION}")

    args = p.parse_args()

    repo_dir = find_git_root(Path(args.repo))
    load_all_envs(repo_dir)

    from system2 import get_system2_provider, find_antigravity_bin

    active_provider = get_system2_provider(args.provider)
    if active_provider == "claude":
        ensure_anthropic_key()
    elif active_provider == "antigravity":
        if not find_antigravity_bin():
            print_banner_box(
                "Google Antigravity CLI ('agy.exe') not found.\n"
                "Ensure Antigravity is installed in ~/.gemini/bin or set ANTIGRAVITY_BIN.",
                level="error",
                title="Antigravity Not Found",
            )
            return 1

    chosen_task = args.task or args.flag_task

    if chosen_task:
        active_test_cmd = args.test_cmd or detect_default_test_cmd(repo_dir)
        return execute_task(
            task=chosen_task,
            repo_dir=repo_dir,
            backend=args.backend,
            model=args.model,
            provider=active_provider,
            test_cmd=active_test_cmd,
            module_map=args.module_map,
            min_domain_confidence=args.min_domain_confidence,
            auto_pr=args.auto_pr,
            pr_risk_ceiling=args.pr_risk_ceiling,
            max_retries=args.max_retries,
            skill=args.skill,
            mode=args.mode,
        )
    else:
        run_interactive(
            initial_repo=repo_dir,
            backend=args.backend,
            model=args.model,
            provider=active_provider,
            test_cmd=args.test_cmd,
            module_map=args.module_map,
            min_domain_confidence=args.min_domain_confidence,
            auto_pr=args.auto_pr,
            pr_risk_ceiling=args.pr_risk_ceiling,
            max_retries=args.max_retries,
            initial_skill=args.skill,
            initial_mode=args.mode,
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())