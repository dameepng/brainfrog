"""BrainFrog CLI — Dual-System Coding Agent (Jev System 1 + Claude System 2).

Equipped with 5 Killer Features:
1. /undo & /diff — Git-native safety net to inspect diffs and revert unwanted AI changes
2. BRAINFROG.md — Project memory and custom rules injected into Claude's prompt
3. @file Context Pinning — Mention @filename in prompts to inject direct file context
4. !command Terminal Passthrough — Execute shell commands inside REPL without leaving
5. /cost & /stats — Transparent token usage and API cost tracker
"""
from __future__ import annotations

import argparse
import os
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
    """Calculate unified responsive terminal dimensions, panel width, and left padding."""
    cols, rows = shutil.get_terminal_size(fallback=(95, 35))
    box_w = min(96, cols - 8) if cols > 100 else max(24, cols - (4 if cols >= 60 else 2))
    margin = max(0, (cols - box_w) // 2) if cols > 100 else (2 if cols >= 60 else 1)
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


def detect_default_test_cmd(repo_dir: Path) -> str:
    """Detect plausible test runner for the workspace."""
    if (repo_dir / "gradlew").exists() or (repo_dir / "gradlew.bat").exists():
        return "cmd /c gradlew.bat test" if os.name == "nt" else "./gradlew test"
    if (repo_dir / "package.json").exists():
        return "npm test"
    if (repo_dir / "pytest.ini").exists() or (repo_dir / "tests").exists():
        return "pytest -q"
    return "cmd /c exit 0" if os.name == "nt" else "true"


# -------------------------------------------------------------------------
# Execution Logic
# -------------------------------------------------------------------------
def execute_task(
    task: str,
    repo_dir: Path,
    backend: str = "auto",
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
) -> int:
    """Run a single task through the dual-system orchestrator."""
    from config import get_system1
    from modules import load_module_map
    from orchestrator import Orchestrator, RunConfig
    from system2 import System2Client, usage_tracker

    if not (repo_dir / ".git").exists():
        print_banner_box(f"{repo_dir} is not a git repository.", level="error", title="Git Error")
        return 1

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
    )

    cols, rows, box_w, margin, pad = get_layout_dims()

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
            txt = f"[{COLOR_FG_MUTED}]{clean}[/{COLOR_FG_MUTED}]"
            console.print(Align.center(txt) if cols > 100 else txt)

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

    # Render Diagnosis / Question Answer or Scope Clarification
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
        elif r.outcome == "needs_clarification" and r.detail:
            print_banner_box(r.detail, level="warning", title="Scope Clarification")

    # Render Task Summary Table only for multi-step / code planning tasks
    is_question_turn = len(results) == 1 and results[0].outcome in ("diagnosed", "needs_clarification")
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

    # Turn token & cost footer (centered)
    task_usage = usage_tracker.reset_task()
    provider_tag = getattr(system2, "provider_name", "claude")
    cost_str = "Google Auth (Active Session)" if provider_tag == "antigravity" else f"Est. Cost: ${task_usage.cost_usd:.4f}"
    if task_usage.total_tokens > 0:
        cost_line = f"⚡ Turn tokens: {task_usage.input_tokens:,} in / {task_usage.output_tokens:,} out ({task_usage.total_tokens:,} total)  ·  {cost_str}"
        token_txt = f"[{COLOR_FG_MUTED}]{cost_line}[/{COLOR_FG_MUTED}]"
        console.print()
        console.print(Align.center(token_txt) if cols > 100 else token_txt)
        console.print()
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
    ("/undo", "Revert last change cleanly via Git"),
    ("/diff", "View colored git diff of recent changes"),
    ("/cost", "View session token usage & metrics"),
    ("/rules", "View or create BRAINFROG.md guidelines"),
    ("/init", "Auto-generate modules.json for stack"),
    ("/status", "Show current workspace & agent status"),
    ("/provider", "Switch AI provider (antigravity: Google Login, claude: Anthropic API)"),
    ("/models", "List available models for active provider"),
    ("/model", "Switch model (e.g. gemini-3.8-flash-high, claude-sonnet-5)"),
    ("/repo", "Switch target workspace repository"),
    ("/test-cmd", "Change test command"),
    ("/backend", "Switch System 1 backend (mock|typesafe|auto)"),
    ("/skills", "List all available modular skills"),
    ("/skill", "Activate modular skill (e.g. /skill audit-anti-slop)"),
    ("/domains", "List detected domain modules and paths"),
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
                            display_token,
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


def select_provider_interactive(current_provider: str) -> Optional[str]:
    """Interactive provider picker — erases itself after selection."""
    items = [
        ("antigravity", "antigravity", "Google Auth Login — no API key, free quota"),
        ("claude",      "claude",      "Anthropic API Key — pay per token"),
    ]
    return _picker("Providers", items, current_provider, id_col="Provider")


# -------------------------------------------------------------------------
# Interactive REPL
# -------------------------------------------------------------------------
def run_interactive(
    initial_repo: Path,
    backend: str = "auto",
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

    app_state = {"status": "Ready", "icon": "●"}

    def get_prompt_tokens():
        """Top border and input prefix for composer box (Section 5.2)."""
        cols, rows, box_w, margin, pad = get_layout_dims()
        bar = "─" * (box_w - 2)

        top_border = f"{pad}╭{bar}╮\n"
        prefix = f"{pad}│  "
        return [
            ("class:input-border", top_border),
            ("class:input-border", prefix),
            ("class:accent", f"{SYM_PROMPT} "),
        ]

    def get_prompt_bottom_border():
        """Bottom border attached directly under input buffer for a contiguous composer box."""
        cols, rows, box_w, margin, pad = get_layout_dims()
        bar = "─" * (box_w - 2)
        return [("class:input-border", f"{pad}╰{bar}╯")]

    # Initialize prompt_toolkit session with autocomplete & history
    session = None
    try:
        from prompt_toolkit.shortcuts import PromptSession
        from prompt_toolkit.styles import Style
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.formatted_text import FormattedText

        pt_style = Style.from_dict({
            # Bottom toolbar: blends into base dark background
            "bottom-toolbar": f"noinherit nobold bg:{COLOR_BG_BASE} {COLOR_FG_MUTED}",
            "bottom-toolbar.text": f"bg:{COLOR_BG_BASE}",
            "toolbar-divider": f"#2a2a2a bg:{COLOR_BG_BASE}",
            "toolbar-accent": f"{COLOR_ACCENT} bold bg:{COLOR_BG_BASE}",
            "toolbar-id": f"{COLOR_FG_PRIMARY} bold bg:{COLOR_BG_BASE}",
            "toolbar-dim": f"{COLOR_FG_MUTED} bg:{COLOR_BG_BASE}",
            "toolbar-sep": f"#2a2a2a bg:{COLOR_BG_BASE}",

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

        @kb.add("c-p")
        def _palette(event):
            event.current_buffer.text = "/help"
            event.current_buffer.validate_and_handle()

        @kb.add("tab")
        def _tab_handler(event):
            b = event.current_buffer
            if not b.text.strip():
                b.text = "/models"
                b.validate_and_handle()
            else:
                event.app.current_buffer.start_completion(select_first=False)

        history_file = GLOBAL_CONFIG_DIR / "history.txt"
        GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        session = PromptSession(
            completer=BrainFrogCompleter(lambda: repo_dir),
            history=FileHistory(str(history_file)),
            style=pt_style,
            key_bindings=kb,
            complete_while_typing=True,
        )

        # Attach contiguous bottom border directly under the prompt input buffer
        try:
            from prompt_toolkit.layout.containers import Window
            from prompt_toolkit.layout.controls import FormattedTextControl
            from prompt_toolkit.filters import Always

            float_cont = session.app.layout.container.children[0].alternative_content
            hsplit = float_cont.content
            if len(hsplit.children) > 1 and hasattr(hsplit.children[1], "content"):
                hsplit.children[1].content.dont_extend_height = Always()
            hsplit.children.append(
                Window(FormattedTextControl(get_prompt_bottom_border), height=1, dont_extend_height=True)
            )
        except Exception:
            pass

    except Exception:
        session = None

    def get_chat_toolbar():
        """Full-width divider + 2-line status bar at bottom of the terminal window (Section 4 & 5.4)."""
        cols, rows = shutil.get_terminal_size(fallback=(95, 35))
        divider = f"{'─' * cols}\n"

        # Model display name
        model_parts = active_model.split("-")
        if cols >= 90:
            model_disp = active_model
        elif cols >= 60:
            model_disp = "-".join(model_parts[:3]) if len(model_parts) >= 3 else active_model
        else:
            model_disp = "-".join(model_parts[:2]) if len(model_parts) >= 2 else active_model

        repo_max = max(6, cols // 8)
        repo_disp = (repo_dir.name[:repo_max] + "…") if len(repo_dir.name) > repo_max + 1 else repo_dir.name

        if cols < 60:
            # Narrow (<60 cols): 1-line status bar (merged info + hint, prioritizing info)
            left = f" ● {model_disp} · {repo_disp}"
            right = f"v{CLI_VERSION} "
            gap = max(1, cols - len(left) - len(right))
            return [
                ("class:toolbar-divider", divider),
                ("class:toolbar-accent", " ● "),
                ("class:toolbar-id", f"{model_disp} · {repo_disp}"),
                ("class:toolbar-sep", " " * gap),
                ("class:toolbar-dim", right),
            ]
        else:
            # Standard & Wide (>=60 cols): 2-line status bar
            # Line 1: Identity (bold)
            # Line 2: Shortcuts + version at right end
            left_hints = "  tab models    ctrl+p help    @ file"
            right_v = f"v{CLI_VERSION}  "
            gap = max(2, cols - len(left_hints) - len(right_v))
            line2 = f"{left_hints}{' ' * gap}{right_v}"

            return [
                ("class:toolbar-divider", divider),
                ("class:toolbar-accent", " ● "),
                ("class:toolbar-id", f"{model_disp}  ·  {repo_disp}\n"),
                ("class:toolbar-dim", line2),
            ]

    def print_splash(cols: int, rows: int) -> None:
        """Render splash/welcome screen for empty state (Section 4 & 5.1)."""
        console.clear()

        # Responsive margins: centered when > 100 cols, standard pad when <= 100 cols
        logo_w = 47
        logo_pad = " " * max(0, (cols - logo_w) // 2) if cols > 100 else ("  " if cols >= 60 else " ")
        tagline = "Tanya BrainFrog apapun soal project ini."
        tagline_pad = " " * max(0, (cols - len(tagline)) // 2) if cols > 100 else ("  " if cols >= 60 else " ")

        # Vertical breathing room: ~1/3 of remaining height so hero sits comfortably in upper-middle
        top_pad = max(1, (rows - 14) // 3) if rows >= 20 else 1
        for _ in range(top_pad):
            console.print()

        if cols < 60:
            # Narrow: skip large logo, show compact header directly
            console.print(f"  [{COLOR_ACCENT}]{SYM_ASSISTANT}[/{COLOR_ACCENT}] [bold {COLOR_FG_PRIMARY}]BrainFrog[/bold {COLOR_FG_PRIMARY}]  [#333333]·[/#333333]  [{COLOR_FG_SECONDARY}]{active_model}[/{COLOR_FG_SECONDARY}]")
            console.print(f"  [{COLOR_FG_MUTED}]{tagline}[/{COLOR_FG_MUTED}]")
            console.print()
            return

        # Wordmark with clear O and frog touch (2-tone: gray BRAIN, white FROG, green accent)
        logo_lines = [
            f"{logo_pad}[#888888]█▀▀▄ █▀▀▄ ▄▀▀▄ ▀█▀ █▄  █[/#888888]   [#E8E8E8]█▀▀ █▀▀▄ [/#E8E8E8][{COLOR_ACCENT}]▄▀ ▀▄[/{COLOR_ACCENT}][#E8E8E8] █▀▀▀[/#E8E8E8]",
            f"{logo_pad}[#888888]█▀▀▄ █▀▀▄ █▀▀█  █  █ ▀▄█[/#888888]   [#E8E8E8]█▀  █▀▀▄ [/#E8E8E8][{COLOR_ACCENT}]█   █[/{COLOR_ACCENT}][#E8E8E8] █ ▀█[/#E8E8E8]",
            f"{logo_pad}[#888888]▀▀▀  ▀  ▀ ▀  ▀ ▀▀▀ ▀   ▀[/#888888]   [#E8E8E8]▀   ▀  ▀ [/#E8E8E8][{COLOR_ACCENT}]▀▄▄▄▀[/{COLOR_ACCENT}][#E8E8E8] ▀▀▀▀[/#E8E8E8]",
        ]
        for line in logo_lines:
            console.print(line)

        console.print()
        console.print(f"{tagline_pad}[{COLOR_FG_MUTED}]{tagline}[/{COLOR_FG_MUTED}]")
        console.print()

    def print_compact_header(cols: int) -> None:
        """Render 1-line top header when history exists (Section 4 & 5.3)."""
        console.print()
        cols, rows, box_w, margin, pad = get_layout_dims()
        model_parts = active_model.split("-")
        model_disp = "-".join(model_parts[:2]) if cols < 60 and len(model_parts) >= 2 else active_model
        if cols >= 60:
            console.print(
                f"{pad}[{COLOR_ACCENT}]{SYM_ASSISTANT}[/{COLOR_ACCENT}] [bold {COLOR_FG_PRIMARY}]BrainFrog[/bold {COLOR_FG_PRIMARY}]"
                f"  [#333333]·[/#333333]  [{COLOR_FG_SECONDARY}]{model_disp}[/{COLOR_FG_SECONDARY}]"
                f"  [#333333]·[/#333333]  [{COLOR_FG_MUTED}]{repo_dir.name}[/{COLOR_FG_MUTED}]"
            )
        else:
            console.print(
                f"{pad}[{COLOR_ACCENT}]{SYM_ASSISTANT}[/{COLOR_ACCENT}] [bold {COLOR_FG_PRIMARY}]BrainFrog[/bold {COLOR_FG_PRIMARY}]"
                f"  [#333333]·[/#333333]  [{COLOR_FG_SECONDARY}]{model_disp}[/{COLOR_FG_SECONDARY}]"
            )
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
            ("Tab", "Ganti model (saat input kosong) / lengkapi teks", "Shortcut"),
            ("Ctrl+P", "Buka bantuan perintah ini", "Shortcut"),
            ("@filename", "Pin konteks file dengan popup pelengkapan otomatis", "Context"),
            ("!command", "Jalankan perintah shell terminal langsung di sesi REPL", "Shell"),
            ("/models", "Pilih model AI aktif dari menu interaktif", "AI Model"),
            ("/provider", "Ganti provider AI (1: Antigravity Google Auth, 2: Claude)", "Provider"),
            ("/undo", "Batalkan perubahan kode terakhir secara bersih via Git", "Safety"),
            ("/diff", "Lihat perbandingan git diff berwarna dari perubahan", "Safety"),
            ("/cost, /stats", "Lihat penggunaan token sesi dan estimasi biaya API", "Metrics"),
            ("/skills", "Lihat daftar modular skill dan status aktifnya", "Skills"),
            ("/skill [name]", "Aktifkan atau nonaktifkan modular skill spesifik", "Skills"),
            ("/rules, /memory", "Tampilkan atau buat aturan proyek (BRAINFROG.md)", "Config"),
            ("/status", "Tampilkan status workspace, branch git, dan agen saat ini", "System"),
            ("/repo <path>", "Pindah direktori repositori target workspace", "Workspace"),
            ("/test-cmd <cmd>", "Ganti perintah test runner (mis. pytest, npm test)", "Config"),
            ("/backend <name>", "Ganti System 1 backend (mock | typesafe | auto)", "System"),
            ("/domains", "Tampilkan domain modul arsitektur yang terdeteksi", "Architecture"),
            ("/init [stack]", "Auto-generate modules.json (web | node | python)", "Setup"),
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
    is_first_turn = True

    while True:
        cols, rows = shutil.get_terminal_size(fallback=(95, 35))
        if not is_first_turn:
            print_compact_header(cols)

        try:
            if session:
                prompt = session.prompt(
                    get_prompt_tokens,
                    placeholder="Tanya BrainFrog…",
                    bottom_toolbar=get_chat_toolbar,
                    refresh_interval=0,
                ).strip()
            else:
                prompt = console.input(f"  [bold {COLOR_ACCENT}]{SYM_PROMPT} [/bold {COLOR_ACCENT}]").strip()
        except (KeyboardInterrupt, EOFError):
            console.print(f"\n  [{COLOR_FG_MUTED}]Sampai jumpa! {SYM_ASSISTANT}[/{COLOR_FG_MUTED}]\n")
            break

        console.print()

        if not prompt:
            continue

        is_first_turn = False

        # Shell command passthrough: !cmd or $cmd
        if prompt.startswith("!") or prompt.startswith("$"):
            cmd = prompt[1:].strip()
            if cmd:
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
            is_first_turn = True
            cols, rows = shutil.get_terminal_size(fallback=(95, 35))
            print_splash(cols, rows)
            continue
        elif lower == "/status":
            branch = get_git_branch(repo_dir)
            status_text = (
                f"[{COLOR_FG_PRIMARY}]Workspace:[/{COLOR_FG_PRIMARY}] [{COLOR_ACCENT}]{repo_dir}[/{COLOR_ACCENT}]\n"
                f"[{COLOR_FG_PRIMARY}]Git Branch:[/{COLOR_FG_PRIMARY}] [{COLOR_INFO}]{branch}[/{COLOR_INFO}]\n"
                f"[{COLOR_FG_PRIMARY}]AI Provider:[/{COLOR_FG_PRIMARY}] [{COLOR_ACCENT}]{active_provider}[/{COLOR_ACCENT}]\n"
                f"[{COLOR_FG_PRIMARY}]AI Model:[/{COLOR_FG_PRIMARY}] [{COLOR_FG_PRIMARY} bold]{active_model}[/{COLOR_FG_PRIMARY} bold]\n"
                f"[{COLOR_FG_PRIMARY}]Active Skill:[/{COLOR_FG_PRIMARY}] [{COLOR_ACCENT}]{active_skill or 'auto-detect'}[/{COLOR_ACCENT}]\n"
                f"[{COLOR_FG_PRIMARY}]System 1 Backend:[/{COLOR_FG_PRIMARY}] [{COLOR_FG_SECONDARY}]{active_backend}[/{COLOR_FG_SECONDARY}]\n"
                f"[{COLOR_FG_PRIMARY}]Test Command:[/{COLOR_FG_PRIMARY}] [{COLOR_FG_MUTED}]{active_test_cmd}[/{COLOR_FG_MUTED}]"
            )
            print_banner_box(status_text, level="info", title="System Status")
            continue
        elif lower == "/undo":
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
        elif lower in ("/cost", "/stats", "/tokens"):
            s = usage_tracker.session
            cols, rows, box_w, margin, pad = get_layout_dims()
            table = Table(title=" BrainFrog Session Metrics ", box=box.ROUNDED, border_style=COLOR_FG_MUTED, width=box_w)
            table.add_column("Metrik", style=COLOR_INFO)
            table.add_column("Nilai", style=f"bold {COLOR_ACCENT}", justify="right")
            table.add_row("Provider", f"{active_provider}")
            table.add_row("Model", f"{active_model}")
            table.add_row("Input Tokens", f"{s.input_tokens:,}")
            table.add_row("Output Tokens", f"{s.output_tokens:,}")
            table.add_row("Total Tokens", f"{s.total_tokens:,}")
            table.add_row("API Requests", f"{s.requests_count:,}")
            if active_provider == "antigravity":
                table.add_row("Billing", "Covered by Google Antigravity Auth")
            else:
                table.add_row("Est. Cost (USD)", f"${s.cost_usd:.4f}")
            console.print()
            console.print(Align.center(table) if cols > 100 else table)
            console.print()
            continue
        elif lower in ("/rules", "/memory"):
            rules = load_project_guidelines(repo_dir)
            if rules:
                cols, rows, box_w, margin, pad = get_layout_dims()
                rules_panel = Panel(Markdown(rules), title=" Project Rules (BRAINFROG.md) ", box=box.ROUNDED, border_style=COLOR_INFO, width=box_w)
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
            if len(parts) > 1 and parts[1].strip() in ("mock", "typesafe", "auto"):
                active_backend = parts[1].strip()
                print_banner_box(f"Backend System 1 diganti ke: [bold {COLOR_ACCENT}]{active_backend}[/bold {COLOR_ACCENT}]", level="success", title="Backend Switch")
            else:
                print_banner_box("Format salah. Gunakan: /backend <mock | typesafe | auto>", level="warning", title="Backend")
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
            from modules import auto_generate_modules_json
            parts = prompt.split(maxsplit=1)
            stack_arg = parts[1].strip().lower() if len(parts) > 1 else None
            stack_map = {"web": "vanilla_web", "vanilla": "vanilla_web", "js": "vanilla_web", "node": "node_web", "react": "node_web"}
            chosen_stack = stack_map.get(stack_arg, stack_arg)
            new_domains = auto_generate_modules_json(repo_dir, stack=chosen_stack)
            print_banner_box(f"Inisialisasi modules.json untuk {repo_dir.name} selesai!\n{len(new_domains)} domain modul arsitektur terdaftar.", level="success", title="Modules Init")
            continue

        # Execute task with centered live braille spinner
        cols, rows, box_w, margin, pad = get_layout_dims()
        if not session:
            prompt_line = f"[{COLOR_FG_SECONDARY}]{SYM_USER}[/{COLOR_FG_SECONDARY}] [bold {COLOR_FG_PRIMARY}]{prompt}[/bold {COLOR_FG_PRIMARY}]"
            console.print()
            console.print(Align.center(prompt_line) if cols > 100 else prompt_line)
            console.print()
        else:
            console.print()

        app_state["status"] = "Processing..."
        app_state["icon"] = "◌"

        from rich.live import Live
        from rich.spinner import Spinner
        spin = Spinner("dots", text=f" [bold {COLOR_FG_PRIMARY}]Mengeksekusi rencana tugas...[/bold {COLOR_FG_PRIMARY}]", style=COLOR_ACCENT)
        with Live(Align.center(spin) if cols > 100 else spin, console=console, refresh_per_second=12.5, transient=True):
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
    p.add_argument("-b", "--backend", choices=["mock", "typesafe", "auto"], default="auto")
    p.add_argument("-m", "--model", "--claude-model", dest="model", default=None, help="Model name (e.g. gemini-3.8-flash-high, claude-sonnet-5)")
    p.add_argument("--provider", choices=["claude", "antigravity", "gemini", "auto"], default=None, help="System 2 AI provider (antigravity: Google Login, claude: Anthropic API)")
    p.add_argument("--skill", default=None, help="Explicitly activate a modular skill (e.g. --skill audit-anti-slop)")
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
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())