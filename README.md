# BrainFrog (🐸) - Dual-System Coding Agent (Jev + Claude)

BrainFrog is an agentic coding loop that automates software engineering workflows: analyzing tasks, planning changes, writing and refactoring code, executing local test suites, self-healing upon failures, and drafting pull requests.

The architecture is built on a **dual-system paradigm** inspired by OpenCode and Claude Code:
- **System 2 (Claude / LLM, generative brain)**: Handles open-ended reasoning, task decomposition, code synthesis, failure diagnostics, and PR narrative drafting.
- **System 1 (Jev / TypeSafe, fast typed gatekeeper)**: Provides rapid, structured, low-latency evaluation for domain routing, test outcome branching, and diff risk classification.

BrainFrog is packaged as a terminal-native CLI (`brainfrog` / `bf`) featuring an **Interactive REPL**, auto-workspace git detection, rich TUI, autocomplete, and safety rollback mechanisms.

---

## Architecture & How the Loop Works

BrainFrog splits decisions into fast, typed gates (System 1) and deep reasoning steps (System 2):

```
                                  [User Task Input]
                                          │
                                          ▼
                            ┌───────────────────────────┐
                            │   System 1: Scope Gate    │
                            │  likely_domain, type      │
                            └───────────────────────────┘
                                          │
                  ┌───────────────────────┴───────────────────────┐
                  ▼                                               ▼
         [question_only]                                  [code_modification]
     Claude reads domain files,                       Claude generates structured
     diagnoses & answers user.                       multi-step execution plan.
     (No code modified, no PR)                                    │
                                                                  ▼
                                                    ┌───────────────────────────┐
                                              ┌───► │  Step N: Write / Edit Code│
                                              │     └───────────────────────────┘
                                              │                   │
                                              │                   ▼
                                              │     ┌───────────────────────────┐
                                              │     │ Execute Local Test Suite  │
                                              │     └───────────────────────────┘
                                              │                   │
                                              │                   ▼
                                              │     ┌───────────────────────────┐
                                              │     │   System 1: Loop Gate     │
                                              │     │  next_action evaluation   │
                                              │     └───────────────────────────┘
                                              │       ├── open_pr ──► (Proceed to PR)
                                              └── retry_fix          ├── escalate_human
                                                                     └── abandon
                                                                  │
                                                                  ▼
                                                    ┌───────────────────────────┐
                                                    │  System 2: Draft PR Copy  │
                                                    └───────────────────────────┘
                                                                  │
                                                                  ▼
                                                    ┌───────────────────────────┐
                                                    │    System 1: PR Gate      │
                                                    │ diff_risk, safe_to_proceed│
                                                    └───────────────────────────┘
                                                                  │
                                     ┌────────────────────────────┴────────────────────────────┐
                                     ▼                                                         ▼
                         [Auto-PR Criteria Met]                                    [Requires Human Review]
                   diff_risk <= ceiling, safe_to_proceed,                     Domain is sensitive, risk too high,
                   and domain is NOT sensitive.                               or manual review requested.
                   Runs: git branch, commit, push, gh pr create.              Prints drafted PR summary to console.
```

### Execution Lifecycle:
1. **Scope Gate (`_scope_gate`)**: Before System 2 runs, System 1 categorizes the task into a codebase domain defined in `modules.json` and determines `change_type` (`feature_request`, `bug_investigation`, `question_only`, or `unclear`).
   - If confidence is below threshold, BrainFrog halts and asks for clarification.
   - If `question_only`, System 2 diagnoses and answers without touching files.
   - If a code change is needed, the workspace file tree scoped to that domain is passed to the planner.
2. **Task Planning (`plan_task`)**: System 2 decomposes the goal into atomic, sequential implementation steps.
3. **Execution & Self-Healing Loop (`_run_step`)**:
   - For each step, System 2 generates complete file replacements.
   - The configured test command (e.g. `pytest`, `npm test`) runs via a bounded subprocess with full process-tree termination.
   - System 1 evaluates the test outcome and decides `next_action`:
     - `open_pr`: Step succeeded, proceed to next step or PR.
     - `retry_fix`: Test failed; System 2 receives error logs and patches the code (up to `max_retries`).
     - `escalate_human` / `abandon`: Circuit breaker triggers, stopping execution safely.
4. **PR Drafting & Risk Gate (`_finalize_pr`)**:
   - System 2 produces PR title and markdown description.
   - System 1 calculates `diff_risk` (low/med/high) and `safe_to_proceed` (0.0 - 1.0).
   - If `--auto-pr` is enabled, the domain is not marked `sensitive: true`, and risk does not exceed `--pr-risk-ceiling`, BrainFrog branches, commits, pushes, and creates a GitHub PR. Otherwise, it outputs the drafted PR for human review.

---

## Directory Structure

```
.
├── cli.py                   # Terminal entrypoint, interactive REPL, TUI rendering & slash commands
├── orchestrator.py          # State machine coordinating System 1, System 2, tests, and git
├── config.py                # Configuration loading, environment variables, and backend selection
├── modules.py               # Domain registry loader, path matching, and scope gate helpers
├── modules.json             # Active workspace domain definitions & sensitivity flags
├── modules.example.json     # Example domain registry template
├── BRAINFROG.md             # Persistent system guidelines, architecture rules, and TUI design tokens
├── requirements.txt         # Python package dependencies
├── pyproject.toml           # Packaging and tool configuration
├── system1/                 # System 1: Fast, typed, deterministic decision layer
│   ├── __init__.py
│   ├── base.py              # Abstract interfaces, questions (Choice/Score/Noul), and Decision contracts
│   └── typesafe_client.py   # Cloud backend calling TypeSafe System 1 API (Jev)
└── system2/                 # System 2: Generative reasoning & code synthesis layer
    ├── __init__.py
    └── claude_client.py     # Claude integration (planning, code generation, fix review, diagnosis, PR)
```

---

## Component Responsibilities

| Component | File | Responsibilities |
| :--- | :--- | :--- |
| **System 1 Client** | `system1/base.py`, `typesafe_client.py` | • Evaluates structured questions (`ChoiceQuestion`, `ScoreQuestion`, `NoulQuestion`).<br>• High-speed, typed scoring without prompt drift.<br>• Native cloud integration via TypeSafe Jev API. |
| **System 2 Client** | `system2/claude_client.py` | • High-level reasoning and multi-step planning (`plan_task`).<br>• Full-file code generation and editing (`write_code`).<br>• Error triage and automated patch generation (`review_and_fix`).<br>• Codebase Q&A without side-effects (`diagnose`).<br>• Pull request summary and body generation (`draft_pr`). |
| **Orchestrator** | `orchestrator.py` | • Central state machine wiring System 1 decisions and System 2 generations.<br>• Subprocess execution with strict timeouts and process tree termination.<br>• Atomic file writing to prevent corrupted/0-byte files upon interruption.<br>• Self-healing retry loop with circuit breakers.<br>• Git lifecycle management (status check, branch creation, commit, push, PR creation). |
| **Domain Registry** | `modules.py`, `modules.json` | • Maps codebase directories and files to conceptual domains.<br>• Enforces security boundaries: domains flagged `"sensitive": true` cannot auto-PR.<br>• Scopes context passed to System 2 to reduce token consumption and latency. |
| **CLI & TUI** | `cli.py` | • Interactive REPL with auto-completion for slash commands and `@file` mentions.<br>• Rich terminal interface compliant with `BRAINFROG.md` TUI design tokens.<br>• Non-interactive single-command runner mode.<br>• Shell passthrough execution (`!cmd`).<br>• Instant safety undo (`/undo`) and diff inspection (`/diff`). |
| **Configuration** | `core/config.py` | • Resolves active System 1 backend (`jev`, `typesafe`, `auto`).<br>• Manages environment variables (`ANTHROPIC_API_KEY`, `TYPESAFE_API_KEY`, `JEV_API_KEY`).<br>• Tracks token usage and session cost estimates. |
| **System Memory** | `BRAINFROG.md` | • Authoritative architectural rules, security boundaries, retrieval standards, and UI guidelines governing agent behavior. |

---

## Setup & Installation

### 1. Prerequisites
- Python 3.10+
- Git CLI (and `gh` GitHub CLI if using `--auto-pr`)
- Anthropic API key (`ANTHROPIC_API_KEY`) or Google Antigravity session
- TypeSafe API key (`TYPESAFE_API_KEY` or `JEV_API_KEY` for Jev System 1)

### 2. Installation
```bash
# Clone and prepare virtual environment
git clone <repo-url> brainfrog
cd brainfrog
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env and set ANTHROPIC_API_KEY
```

### 3. Domain Configuration (`modules.json`)
Configure your codebase domains by copying `modules.example.json` into your target repository as `modules.json`:
```json
{
  "auth": {
    "description": "Authentication, session management, OAuth, JWT, permissions.",
    "paths": ["app/auth/", "app/middleware/session.py"],
    "sensitive": true
  },
  "billing": {
    "description": "Stripe integrations, subscription tiers, invoicing, webhooks.",
    "paths": ["app/billing/", "app/models/invoice.py"],
    "sensitive": true
  },
  "api": {
    "description": "Public REST endpoints, request validators, response serializers.",
    "paths": ["app/api/", "app/schemas/"],
    "sensitive": false
  }
}
```
*Note: Domains marked `"sensitive": true` will never auto-open a PR.*

---

## Usage

### Interactive REPL Mode (Recommended)
Launch the interactive shell in any git repository:
```bash
python cli.py --repo /path/to/target/repo
```
Inside the REPL:
- Type your prompt directly: `Add healthcheck endpoint at /api/health`
- Mention files with autocompletion: `@app/api/routes.py`
- Execute terminal commands directly: `!pytest`
- Use slash commands:
  - `/status`: View current repository, test command, and active models
  - `/diff`: Preview uncommitted git changes
  - `/undo`: Cleanly revert uncommitted changes made by the agent
  - `/rules`: Display active system guidelines (`BRAINFROG.md`)
  - `/stats` / `/cost`: Show token consumption and estimated session cost
  - `/test-cmd <cmd>`: Switch the active test command dynamically
  - `/clear`: Clear terminal screen
  - `/help`: Display command cheat sheet
  - `/exit`: Exit BrainFrog

### Non-Interactive Single Task Mode
Execute a single instruction directly from the command line:

```bash
# Non-interactive task using real Jev System 1 backend
python cli.py \
  --repo /path/to/target/repo \
  --task "Fix ZeroDivisionError in calc.py" \
  --test-cmd "pytest -q" \
  --backend jev

# Full automated run with Jev / TypeSafe and Auto-PR
python cli.py \
  --repo /path/to/target/repo \
  --task "Implement user logout endpoint" \
  --test-cmd "pytest app/tests/test_auth.py" \
  --backend jev \
  --auto-pr \
  --pr-risk-ceiling medium
```

---

## Reliability, Safety & Idempotency

- **Atomic File Writing**: File updates are staged and replaced atomically (`os.replace`) to eliminate 0-byte or corrupted files during interruptions (`Ctrl+C`).
- **Bounded Process Tree Termination**: Test executions enforce strict timeouts (default 60s); timeouts recursively terminate the entire child process hierarchy (`taskkill` on Windows, process groups on POSIX) to avoid lingering orphan test runners.
- **Fail-Safe Rollback**: The `/undo` command leverages git status verification to revert working directory modifications without altering committed history.
- **Circuit Breakers**: Multi-step and retry loops enforce fixed maximum thresholds (`max_retries = 3`) to prevent infinite repair loops.
- **Sensitive Domain Safeguard**: Hard business constraints cannot be bypassed by model confidence scores alone.
