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
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

console = Console(legacy_windows=False)
CLI_VERSION = "0.1.0"


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

    console.print("\n[bold yellow]! Anthropic API Key not configured[/bold yellow]")
    console.print("[dim]BrainFrog requires Claude (System 2) to plan and generate code.[/dim]")
    try:
        entered = console.input("[bold cyan]Enter ANTHROPIC_API_KEY (sk-ant-...): [/bold cyan]").strip()
    except (KeyboardInterrupt, EOFError):
        console.print("\n[dim]Cancelled.[/dim]")
        sys.exit(1)

    if not entered:
        console.print("[bold red]Error:[/bold red] API key cannot be empty.")
        sys.exit(1)

    GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(GLOBAL_ENV_FILE, "a", encoding="utf-8") as f:
        f.write(f"\nANTHROPIC_API_KEY={entered}\n")
    os.environ["ANTHROPIC_API_KEY"] = entered
    console.print(f"[green]Saved key to {GLOBAL_ENV_FILE}[/green]\n")
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
        console.print(f"[bold red]Error:[/bold red] {repo_dir} is not a git repository.", style="red")
        return 1

    active_test_cmd = test_cmd or detect_default_test_cmd(repo_dir)

    try:
        system1 = get_system1(backend)
    except Exception as e:
        console.print(f"[bold red]System 1 Error:[/bold red] {e}")
        return 1

    chosen_model = model or claude_model
    try:
        system2 = System2Client(model=chosen_model, provider=provider)
    except Exception as e:
        console.print(f"[bold red]System 2 Error:[/bold red] {e}")
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

    orchestrator = Orchestrator(system1, system2, cfg)
    try:
        results = orchestrator.run()
    except KeyboardInterrupt:
        console.print("\n[yellow]Task interrupted by user.[/yellow]")
        return 130
    except Exception as e:
        console.print(f"\n[bold red]Orchestration Error:[/bold red] {e}")
        return 1

    console.print("\n[bold cyan]=== Run Summary ===[/bold cyan]")
    for r in results:
        badge = "[green]SUCCESS[/green]" if r.outcome in ("diagnosed", "opened_pr", "drafted_pr") else f"[yellow]{r.outcome.upper()}[/yellow]"
        console.print(f"  • Step {r.step.id} ({r.step.description}): {badge} (retries: {r.retries})")

    # Turn token & cost footer
    task_usage = usage_tracker.reset_task()
    provider_tag = getattr(system2, "provider_name", "claude")
    cost_str = "Google Auth (Active Session)" if provider_tag == "antigravity" else f"Est. Cost: ${task_usage.cost_usd:.4f}"
    if task_usage.total_tokens > 0:
        console.print(
            f"[dim]⚡ Turn tokens: {task_usage.input_tokens:,} in / {task_usage.output_tokens:,} out "
            f"({task_usage.total_tokens:,} total) | {cost_str}[/dim]\n"
        )
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

    # Initialize prompt_toolkit session with autocomplete & history
    session = None
    try:
        from prompt_toolkit.shortcuts import PromptSession
        from prompt_toolkit.styles import Style
        from prompt_toolkit.history import FileHistory

        pt_style = Style.from_dict({
            "prompt-name": "#00FF66 bold",
            "prompt-repo": "#94a3b8",
            "prompt-arrow": "#64748b bold",
            "completion-menu.completion": "bg:#111518 #e2e8f0",
            "completion-menu.completion.current": "bg:#00FF66 #000000 bold",
            "completion-menu.meta.completion": "bg:#111518 #718096",
            "completion-menu.meta.completion.current": "bg:#00FF66 #000000 bold",
            "scrollbar.background": "bg:#0a0b0c",
            "scrollbar.button": "bg:#00FF66",
        })
        history_file = GLOBAL_CONFIG_DIR / "history.txt"
        GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        session = PromptSession(
            completer=BrainFrogCompleter(lambda: repo_dir),
            history=FileHistory(str(history_file)),
            style=pt_style,
            complete_while_typing=True,
        )
    except Exception:
        session = None

    def print_banner() -> None:
        cols = shutil.get_terminal_size(fallback=(80, 24)).columns
        has_rules = bool(load_project_guidelines(repo_dir))
        memory_text = "active (BRAINFROG.md)" if has_rules else "not set"
        mem_style = "bold #00FF66" if has_rules else "#cbd5e0"

        skill_text = f"skill: {active_skill}" if active_skill else "skill: auto"
        skill_style = "bold #00FF66" if active_skill else "#cbd5e0"

        cur_icon, cur_text = app_state["icon"], app_state["status"]
        status_style = "bold #00FF66" if cur_text == "Ready" else ("bold yellow" if "Processing" in cur_text else ("bold green" if "Done" in cur_text else "bold red"))

        frog_3row = [
            r"  [bold #00FF66]▄▀▄  ▄▀▄[/bold #00FF66]   ",
            r" [bold #00FF66]▐█[/bold #00FF66][bold white]0[/bold white][bold #00FF66]█──█[/bold #00FF66][bold white]0[/bold white][bold #00FF66]█▌[/bold #00FF66]  ",
            r"  [bold #00FF66]▀█▄▄▄▄█▀[/bold #00FF66]   ",
        ]

        wm_3row = [
            r"[bold #00FF66]█▀▀▄ █▀▀█ ▄▀▀▄ ▀█▀ █▄  █ █▀▀ █▀▀▄ ▄▀▀▄ ▄▀▀▀[/bold #00FF66]",
            r"[bold #00FF66]█▀▀▄ █▀▀▄ █▀▀█  █  █ ▀▄█ █▀  █▀▀▄ █  █ █ ▀█[/bold #00FF66]",
            r"[bold #00FF66]▀▀▀  ▀  ▀ ▀  ▀ ▀▀▀ ▀   ▀ ▀   ▀  ▀  ▀▀  ▀▀▀▀[/bold #00FF66]",
        ]

        right_3row = [
            f" [dim #718096]v{CLI_VERSION}[/dim #718096]",
            f" [{status_style}]{cur_icon} {cur_text}[/{status_style}]",
            "",
        ]

        console.print()
        if cols >= 68:
            for f, w, r in zip(frog_3row, wm_3row, right_3row):
                console.print(f"{f} {w}  {r}")
        elif cols >= 45:
            console.print(f" [bold #00FF66]▄▀▄ ▄▀▄ BRAINFROG[/bold #00FF66] [dim #718096]v{CLI_VERSION}[/dim #718096]  [{status_style}]{cur_icon} {cur_text}[/{status_style}]")
        else:
            console.print(f" [bold #00FF66]BRAINFROG[/bold #00FF66] [dim #718096]v{CLI_VERSION}[/dim #718096]")
            console.print(f" [{status_style}]{cur_icon} {cur_text}[/{status_style}]")

        console.print()

        # Compact summary line: mode, provider/model, memory, skill, test command
        prov_disp = "antigravity (Google Auth)" if active_provider == "antigravity" else "claude"
        if cols >= 80:
            console.print(
                f"  [dim #718096]mode:[/dim #718096] [#cbd5e0]agentic[/#cbd5e0]  "
                f"[dim #4a5568]·[/dim #4a5568]  [dim #718096]provider:[/dim #718096] [bold #00FF66]{prov_disp}[/bold #00FF66] ([dim]{active_model}[/dim])  "
                f"[dim #4a5568]·[/dim #4a5568]  [dim #718096]memory:[/dim #718096] [{mem_style}]{memory_text}[/{mem_style}]  "
                f"[dim #4a5568]·[/dim #4a5568]  [{skill_style}]{skill_text}[/{skill_style}]"
            )
            console.print(f"  [dim #718096]test:[/dim #718096] [#cbd5e0]{active_test_cmd}[/#cbd5e0]")
        else:
            console.print(
                f"  [dim #718096]mode:[/dim #718096] [#cbd5e0]agentic[/#cbd5e0]  "
                f"[dim #4a5568]·[/dim #4a5568]  [dim #718096]provider:[/dim #718096] [bold #00FF66]{active_provider}[/bold #00FF66]  "
                f"[dim #4a5568]·[/dim #4a5568]  [{skill_style}]{skill_text}[/{skill_style}]"
            )
            console.print(f"  [dim #718096]model:[/dim #718096] [dim]{active_model}[/dim]  ·  [dim #718096]test:[/dim #718096] [#cbd5e0]{active_test_cmd}[/#cbd5e0]")

        console.print()
        console.print("  [dim #718096]@ file · / perintah · ! shell[/dim #718096]")
        console.print()

    def show_help() -> None:
        table = Table(title="BrainFrog Commands & Shortcuts", box=box.SIMPLE, show_edge=False, header_style="bold #00FF66")
        table.add_column("Command", style="cyan", no_wrap=True)
        table.add_column("Description", style="white")
        table.add_row("/help, /?", "Show this help table")
        table.add_row("/undo", "Revert last change or commit cleanly via Git")
        table.add_row("/diff", "View colored git diff of recent changes")
        table.add_row("/rules, /memory", "View or create BRAINFROG.md project guidelines")
        table.add_row("/skills", "List all available modular skills and status")
        table.add_row("/skill [name]", "Activate modular skill (e.g. /skill audit-anti-slop)")
        table.add_row("/cost, /stats", "View session token usage and estimated API cost")
        table.add_row("/init [stack]", "Auto-generate modules.json (web|android|node|python)")
        table.add_row("/status", "Show current workspace & agent status")
        table.add_row("/repo <path>", "Switch target workspace repository")
        table.add_row("/test-cmd <cmd>", "Change test command (e.g. /test-cmd gradlew test)")
        table.add_row("/backend <name>", "Switch System 1 backend (mock|typesafe|auto)")
        table.add_row("/model <name>", "Switch Claude model (e.g. claude-sonnet-5)")
        table.add_row("/domains", "List detected domain modules and paths")
        table.add_row("!command", "Run terminal shell command directly (e.g. !start index.html)")
        table.add_row("@filename", "Pin file context with live autocomplete popup (e.g. @app.js)")
        table.add_row("/clear", "Clear terminal screen")
        table.add_row("/exit, /quit", "Exit BrainFrog session")
        console.print(table)
        console.print()

    console.clear()
    print_banner()

    while True:
        try:
            repo_name = repo_dir.name
            if session:
                prompt_parts = [
                    ("class:prompt-name", "brainfrog "),
                    ("class:prompt-repo", f"({repo_name})"),
                    ("class:prompt-arrow", " > "),
                ]
                prompt = session.prompt(prompt_parts).strip()
            else:
                prompt = console.input(f"[bold #00FF66]brainfrog[/bold #00FF66] [dim]({repo_name}) >[/dim] ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Bye! 🐸[/dim]")
            break


        if not prompt:
            continue

        # Shell command passthrough: !cmd or $cmd
        if prompt.startswith("!") or prompt.startswith("$"):
            cmd = prompt[1:].strip()
            if cmd:
                console.print(f"[dim]Running shell:[/dim] [bold cyan]{cmd}[/bold cyan]\n")
                try:
                    subprocess.run(cmd, shell=True, cwd=repo_dir)
                except Exception as e:
                    console.print(f"[red]Error running command:[/red] {e}")
                console.print()
            continue

        # Slash Commands
        lower = prompt.lower()
        if lower in ("/exit", "/quit", "exit", "quit"):
            console.print("[dim]Exiting BrainFrog. Goodbye! 🐸[/dim]")
            break
        elif lower in ("/help", "/?"):
            show_help()
            continue
        elif lower == "/clear":
            console.clear()
            print_banner()
            continue
        elif lower == "/status":
            print_banner()
            continue
        elif lower == "/undo":
            # 1. Check uncommitted changes first
            status = subprocess.run(["git", "status", "--porcelain"], cwd=repo_dir, capture_output=True, text=True).stdout.strip()
            if status:
                subprocess.run(["git", "restore", "."], cwd=repo_dir)
                subprocess.run(["git", "clean", "-fd"], cwd=repo_dir)
                console.print("[bold green]✓ Reverted uncommitted changes! Working directory restored.[/bold green]")
            else:
                # 2. Reset last commit
                log = subprocess.run(["git", "log", "-1", "--oneline"], cwd=repo_dir, capture_output=True, text=True).stdout.strip()
                if log:
                    subprocess.run(["git", "reset", "--hard", "HEAD~1"], cwd=repo_dir, capture_output=True, text=True)
                    console.print(f"[bold green]✓ Reverted commit:[/bold green] {log}")
                    console.print("[green]Repository cleanly restored to previous commit![/green]")
                else:
                    console.print("[yellow]No commits found to undo.[/yellow]")
            continue
        elif lower == "/diff":
            diff = subprocess.run(["git", "diff", "HEAD"], cwd=repo_dir, capture_output=True, text=True).stdout.strip()
            if not diff:
                diff = subprocess.run(["git", "diff", "HEAD~1"], cwd=repo_dir, capture_output=True, text=True).stdout.strip()
            if diff:
                console.print(Panel(Syntax(diff, "diff", theme="monokai", line_numbers=True), title="Git Diff", box=box.ROUNDED))
            else:
                console.print("[dim]No diffs found (working tree clean).[/dim]")
            continue
        elif lower in ("/cost", "/stats", "/tokens"):
            s = usage_tracker.session
            table = Table(title="BrainFrog Session Metrics", box=box.ROUNDED)
            table.add_column("Metric", style="cyan")
            table.add_column("Value", style="green", justify="right")
            table.add_row("Provider", f"{active_provider}")
            table.add_row("Model", f"{active_model}")
            table.add_row("Input Tokens", f"{s.input_tokens:,}")
            table.add_row("Output Tokens", f"{s.output_tokens:,}")
            table.add_row("Total Tokens", f"{s.total_tokens:,}")
            table.add_row("API Requests", f"{s.requests_count:,}")
            if active_provider == "antigravity":
                table.add_row("Billing", "Covered by Google Antigravity Login")
            else:
                table.add_row("Est. Cost (USD)", f"${s.cost_usd:.4f}")
            console.print(table)
            continue
        elif lower in ("/rules", "/memory"):
            rules = load_project_guidelines(repo_dir)
            if rules:
                console.print(Panel(Markdown(rules), title="Project Rules (BRAINFROG.md)", box=box.ROUNDED, border_style="cyan"))
            else:
                console.print("[yellow]No BRAINFROG.md found in this project.[/yellow]")
                try:
                    create = console.input("Create a template BRAINFROG.md? (y/n): ").strip().lower()
                    if create == "y":
                        tmpl = (
                            "# Project Guidelines & Conventions\n\n"
                            "- Code Style: Clean, modern, and self-documenting.\n"
                            "- UI Theme: Dark mode preferred.\n"
                            "- Architecture: Follow clean architecture and single responsibility.\n"
                        )
                        (repo_dir / "BRAINFROG.md").write_text(tmpl, encoding="utf-8")
                        console.print("[green]Created BRAINFROG.md template![/green]")
                except Exception:
                    pass
            continue
        elif lower == "/skills":
            from skills import index_skills
            indexed = index_skills(repo_dir)
            if not indexed:
                console.print("[yellow]No modular skills found in .brainfrog/skills[/yellow]")
            else:
                table = Table(title="Available Modular Skills (.brainfrog/skills)", box=box.ROUNDED)
                table.add_column("Skill Name", style="cyan bold")
                table.add_column("Description", style="white")
                table.add_column("Status", style="green")
                for s_name, s_meta in indexed.items():
                    status = "[bold #00FF66]ACTIVE[/bold #00FF66]" if active_skill == s_name else "[dim]Standby (auto-detect)[/dim]"
                    table.add_row(s_name, s_meta.description[:90] + "...", status)
                console.print(table)
            continue
        elif lower.startswith("/skill"):
            from skills import index_skills
            indexed = index_skills(repo_dir)
            parts = prompt.split(maxsplit=1)
            if len(parts) > 1:
                arg = parts[1].strip()
                if arg.lower() in ("off", "none", "clear", "auto"):
                    active_skill = None
                    console.print("[green]Skill reset to automatic intent detection.[/green]")
                    print_banner()
                elif arg in indexed or arg.lower() in {k.lower(): k for k in indexed}:
                    matched_key = next(k for k in indexed if k.lower() == arg.lower())
                    active_skill = matched_key
                    console.print(f"[bold green]✓ Activated skill:[/bold green] [cyan]{active_skill}[/cyan]")
                    print_banner()
                else:
                    console.print(f"[red]Skill '{arg}' not found.[/red] Available skills: {', '.join(indexed.keys()) or 'none'}")
            else:
                if active_skill:
                    console.print(f"Current active skill: [bold #00FF66]{active_skill}[/bold #00FF66] (use `/skill off` to reset)")
                else:
                    console.print("Current skill mode: [dim]auto-detect on prompt[/dim]")
                    if indexed:
                        console.print(f"Available skills: {', '.join(indexed.keys())}")
            continue
        elif lower.startswith("/repo"):
            parts = prompt.split(maxsplit=1)
            if len(parts) > 1:
                new_path = Path(parts[1]).resolve()
                if new_path.exists():
                    repo_dir = find_git_root(new_path)
                    active_test_cmd = detect_default_test_cmd(repo_dir)
                    console.print(f"[green]Switched repository to:[/green] {repo_dir}")
                else:
                    console.print(f"[red]Directory not found:[/red] {parts[1]}")
            else:
                console.print(f"Current repo: [cyan]{repo_dir}[/cyan]")
            continue
        elif lower.startswith("/test-cmd"):
            parts = prompt.split(maxsplit=1)
            if len(parts) > 1:
                active_test_cmd = parts[1].strip()
                console.print(f"[green]Updated test command to:[/green] `{active_test_cmd}`")
            else:
                console.print(f"Current test command: `{active_test_cmd}`")
            continue
        elif lower.startswith("/backend"):
            parts = prompt.split(maxsplit=1)
            if len(parts) > 1 and parts[1].strip() in ("mock", "typesafe", "auto"):
                active_backend = parts[1].strip()
                console.print(f"[green]Switched backend to:[/green] {active_backend}")
            else:
                console.print("Usage: /backend <mock | typesafe | auto>")
            continue
        elif lower.startswith("/provider"):
            parts = prompt.split(maxsplit=1)
            if len(parts) > 1:
                target_prov = parts[1].strip().lower()
                if target_prov in ("gemini", "antigravity", "google"):
                    active_provider = "antigravity"
                    if not active_model or "claude" in active_model:
                        active_model = "gemini-3.8-flash-high"
                    console.print("[bold green]✓ Switched provider to Google Antigravity (Google Auth Login)![/bold green]")
                    print_banner()
                elif target_prov in ("claude", "anthropic"):
                    active_provider = "claude"
                    if not active_model or "gemini" in active_model:
                        active_model = "claude-sonnet-5"
                    console.print("[bold green]✓ Switched provider to Claude (Anthropic API)![/bold green]")
                    print_banner()
                else:
                    console.print(f"[red]Unknown provider: {parts[1]}. Options: antigravity (Google Auth), claude[/red]")
            else:
                console.print(f"Current provider: [bold #00FF66]{active_provider}[/bold #00FF66] (model: {active_model})")
                console.print("Switch with: `/provider antigravity` or `/provider claude`")
            continue
        elif lower == "/models":
            if active_provider == "antigravity":
                console.print(Panel(
                    "• [bold #00FF66]gemini-3.8-flash-high[/bold #00FF66] (Ultra fast, high reasoning - default)\n"
                    "• [bold]gemini-3.8-flash-medium[/bold]\n"
                    "• [bold]gemini-3.7-flash-high[/bold]\n"
                    "• [bold]gemini-3.1-pro-high[/bold] (Complex architecture reasoning)\n"
                    "• [bold]claude-sonnet-4-6[/bold] (Anthropic via Google Auth)\n"
                    "• [bold]claude-opus-4-6-thinking[/bold]",
                    title="Available Models in Antigravity (Google Auth)",
                    box=box.ROUNDED,
                ))
            else:
                console.print(Panel(
                    "• [bold #00FF66]claude-sonnet-5[/bold #00FF66] (Default)\n"
                    "• [bold]claude-3-5-sonnet-20241022[/bold]\n"
                    "• [bold]claude-3-5-haiku-20241022[/bold]",
                    title="Available Models in Claude (Anthropic API)",
                    box=box.ROUNDED,
                ))
            continue
        elif lower.startswith("/model"):
            parts = prompt.split(maxsplit=1)
            if len(parts) > 1:
                active_model = parts[1].strip()
                console.print(f"[green]Switched model to:[/green] {active_model}")
                print_banner()
            else:
                console.print(f"Current model: [bold #00FF66]{active_model}[/bold #00FF66] (provider: {active_provider})")
                console.print("Use `/models` to view available options or `/model <name>` to change.")
            continue
        elif lower == "/domains":
            domains = load_module_map(repo_dir, Path(module_map) if module_map else None, auto_create=False)
            table = Table(title="Configured Scope Domains", box=box.SIMPLE)
            table.add_column("Domain", style="cyan")
            table.add_column("Description", style="white")
            table.add_column("Paths", style="dim")
            table.add_column("Sensitive", style="yellow")
            for k, d in domains.items():
                table.add_row(k, d.description, ", ".join(d.paths) or "(all)", "YES" if d.sensitive else "no")
            console.print(table)
            continue
        elif lower.startswith("/init"):
            from modules import auto_generate_modules_json
            parts = prompt.split(maxsplit=1)
            stack_arg = parts[1].strip().lower() if len(parts) > 1 else None
            stack_map = {"web": "vanilla_web", "vanilla": "vanilla_web", "js": "vanilla_web", "node": "node_web", "react": "node_web"}
            chosen_stack = stack_map.get(stack_arg, stack_arg)
            new_domains = auto_generate_modules_json(repo_dir, stack=chosen_stack)
            console.print(f"[green]Initialized modules.json for {repo_dir.name} with {len(new_domains)} domain(s)![/green]")
            print_banner()
            continue

        # Execute task
        console.print(f"\n[dim]Executing task:[/dim] [bold]{prompt}[/bold]\n")
        app_state["status"] = "Processing..."
        app_state["icon"] = "◌"
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
            app_state["icon"] = "✓"
        else:
            app_state["status"] = "Failed"
            app_state["icon"] = "✗"


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
            console.print("[bold red]Error:[/bold red] Google Antigravity CLI ('agy.exe') not found.")
            console.print("[dim]Ensure Antigravity is installed in ~/.gemini/bin or set ANTIGRAVITY_BIN.[/dim]")
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
