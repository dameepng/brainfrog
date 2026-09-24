"""BrainFrog CLI — Dual-System Coding Agent (Jev System 1 + Claude System 2).

Can be used in two modes:
1. Interactive REPL (like Claude Code / OpenCode):
       brainfrog
2. Single-shot command:
       brainfrog "Add currency formatter utility in ui module"
"""
from __future__ import annotations

import argparse
import os
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

from dotenv import load_dotenv
from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

console = Console()

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
    claude_model: Optional[str] = None,
    test_cmd: Optional[str] = None,
    module_map: Optional[str] = None,
    min_domain_confidence: float = 0.55,
    auto_pr: bool = False,
    pr_risk_ceiling: str = "medium",
    max_retries: int = 3,
) -> int:
    """Run a single task through the dual-system orchestrator."""
    from config import get_system1
    from modules import load_module_map
    from orchestrator import Orchestrator, RunConfig
    from system2.claude_client import System2Client

    if not (repo_dir / ".git").exists():
        console.print(f"[bold red]Error:[/bold red] {repo_dir} is not a git repository.", style="red")
        return 1

    active_test_cmd = test_cmd or detect_default_test_cmd(repo_dir)

    try:
        system1 = get_system1(backend)
    except Exception as e:
        console.print(f"[bold red]System 1 Error:[/bold red] {e}")
        return 1

    try:
        system2 = System2Client(model=claude_model) if claude_model else System2Client()
    except Exception as e:
        console.print(f"[bold red]System 2 (Claude) Error:[/bold red] {e}")
        return 1

    domains = load_module_map(repo_dir, Path(module_map) if module_map else None)

    cfg = RunConfig(
        repo_dir=repo_dir,
        task=task,
        test_command=active_test_cmd.split(),
        max_retries=max_retries,
        auto_pr=auto_pr,
        pr_risk_ceiling=pr_risk_ceiling,
        domains=domains,
        min_domain_confidence=min_domain_confidence,
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
    console.print()
    return 0


# -------------------------------------------------------------------------
# Interactive REPL
# -------------------------------------------------------------------------
def run_interactive(
    initial_repo: Path,
    backend: str = "auto",
    claude_model: Optional[str] = None,
    test_cmd: Optional[str] = None,
    module_map: Optional[str] = None,
    min_domain_confidence: float = 0.55,
    auto_pr: bool = False,
    pr_risk_ceiling: str = "medium",
    max_retries: int = 3,
) -> None:
    """Full-featured interactive TUI session."""
    from modules import load_module_map

    repo_dir = find_git_root(initial_repo)
    active_test_cmd = test_cmd or detect_default_test_cmd(repo_dir)
    active_backend = backend
    active_model = claude_model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

    def print_banner() -> None:
        branch = get_git_branch(repo_dir)
        domains = load_module_map(repo_dir, Path(module_map) if module_map else None)
        domain_list = ", ".join(domains.keys()) if domains else "(auto-discovered)"

        banner_content = (
            f"[bold green]🐸 BrainFrog[/bold green] [dim]v0.1.0 — Dual-System Coding Agent[/dim]\n\n"
            f"[bold]Workspace[/bold]  : [cyan]{repo_dir}[/cyan] [dim](git: {branch})[/dim]\n"
            f"[bold]System 1[/bold]   : [magenta]{active_backend}[/magenta] [dim](Jev gatekeeper)[/dim]\n"
            f"[bold]System 2[/bold]   : [blue]{active_model}[/blue] [dim](Claude generation)[/dim]\n"
            f"[bold]Domains[/bold]    : [yellow]{domain_list}[/yellow]\n"
            f"[bold]Test Cmd[/bold]   : [dim]`{active_test_cmd}`[/dim]\n\n"
            f"[dim]Ketik perintah/pertanyaan Anda, ketik [bold]/help[/bold] untuk menu, atau [bold]/exit[/bold] untuk keluar.[/dim]"
        )
        console.print(Panel(banner_content, border_style="green", box=box.ROUNDED))

    def show_help() -> None:
        table = Table(title="BrainFrog Slash Commands", box=box.SIMPLE_HEAVY)
        table.add_column("Command", style="cyan", no_wrap=True)
        table.add_column("Description", style="white")
        table.add_row("/help, /?", "Show this help table")
        table.add_row("/status", "Show current workspace & agent status")
        table.add_row("/repo <path>", "Switch target workspace repository")
        table.add_row("/test-cmd <cmd>", "Change test command (e.g. /test-cmd gradlew test)")
        table.add_row("/backend <mock|typesafe|auto>", "Switch System 1 backend")
        table.add_row("/model <name>", "Switch Claude model (e.g. claude-sonnet-5)")
        table.add_row("/domains", "List detected domain modules and paths")
        table.add_row("/clear", "Clear terminal screen")
        table.add_row("/exit, /quit", "Exit BrainFrog session")
        console.print(table)

    console.clear()
    print_banner()

    while True:
        try:
            repo_name = repo_dir.name
            prompt = console.input(f"[bold green]brainfrog[/bold green] [dim]({repo_name})>[/dim] ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Bye! 🐸[/dim]")
            break

        if not prompt:
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
        elif lower.startswith("/model"):
            parts = prompt.split(maxsplit=1)
            if len(parts) > 1:
                active_model = parts[1].strip()
                console.print(f"[green]Switched Claude model to:[/green] {active_model}")
            else:
                console.print(f"Current model: {active_model}")
            continue
        elif lower == "/domains":
            domains = load_module_map(repo_dir, Path(module_map) if module_map else None)
            table = Table(title="Configured Scope Domains", box=box.SIMPLE)
            table.add_column("Domain", style="cyan")
            table.add_column("Description", style="white")
            table.add_column("Paths", style="dim")
            table.add_column("Sensitive", style="yellow")
            for k, d in domains.items():
                table.add_row(k, d.description, ", ".join(d.paths) or "(all)", "YES" if d.sensitive else "no")
            console.print(table)
            continue

        # Execute task
        console.print(f"\n[dim]Executing task:[/dim] [bold]{prompt}[/bold]\n")
        execute_task(
            task=prompt,
            repo_dir=repo_dir,
            backend=active_backend,
            claude_model=active_model,
            test_cmd=active_test_cmd,
            module_map=module_map,
            min_domain_confidence=min_domain_confidence,
            auto_pr=auto_pr,
            pr_risk_ceiling=pr_risk_ceiling,
            max_retries=max_retries,
        )


# -------------------------------------------------------------------------
# Main Entry Point
# -------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(
        prog="brainfrog",
        description="🐸 BrainFrog: Dual-System Coding Agent (Jev System 1 + Claude System 2)",
    )
    p.add_argument("task", nargs="?", default=None, help="Task to execute (leave empty for interactive REPL)")
    p.add_argument("--task", dest="flag_task", default=None, help="Alternative flag for task description")
    p.add_argument("-r", "--repo", default=".", help="Path to target git repository (default: current directory)")
    p.add_argument("-t", "--test-cmd", default=None, help="Shell command for running test suite")
    p.add_argument("-b", "--backend", choices=["mock", "typesafe", "auto"], default="auto")
    p.add_argument("-m", "--claude-model", default=None, help="Override ANTHROPIC_MODEL env var")
    p.add_argument("--auto-pr", action="store_true", help="Push branch and open GitHub PR when approved")
    p.add_argument("--pr-risk-ceiling", choices=["low", "medium", "high"], default="medium")
    p.add_argument("--max-retries", type=int, default=3)
    p.add_argument("--module-map", default=None, help="Path to custom modules.json")
    p.add_argument("--min-domain-confidence", type=float, default=0.55)
    p.add_argument("-v", "--version", action="version", version="BrainFrog 0.1.0")

    args = p.parse_args()

    repo_dir = find_git_root(Path(args.repo))
    load_all_envs(repo_dir)
    ensure_anthropic_key()

    chosen_task = args.task or args.flag_task

    if chosen_task:
        # Single-shot mode
        active_test_cmd = args.test_cmd or detect_default_test_cmd(repo_dir)
        return execute_task(
            task=chosen_task,
            repo_dir=repo_dir,
            backend=args.backend,
            claude_model=args.claude_model,
            test_cmd=active_test_cmd,
            module_map=args.module_map,
            min_domain_confidence=args.min_domain_confidence,
            auto_pr=args.auto_pr,
            pr_risk_ceiling=args.pr_risk_ceiling,
            max_retries=args.max_retries,
        )
    else:
        # Interactive mode
        run_interactive(
            initial_repo=repo_dir,
            backend=args.backend,
            claude_model=args.claude_model,
            test_cmd=args.test_cmd,
            module_map=args.module_map,
            min_domain_confidence=args.min_domain_confidence,
            auto_pr=args.auto_pr,
            pr_risk_ceiling=args.pr_risk_ceiling,
            max_retries=args.max_retries,
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
