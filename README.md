# BrainFrog (🐸) — Dual-System Agentic Coding CLI

BrainFrog is an agentic coding loop that automates software engineering workflows: analyzing tasks, planning changes, writing and refactoring code, executing local test suites, self-healing upon failures, **verifying frontend output in a real browser**, and drafting pull requests — all from the terminal.

## Architecture Overview

The system is built on a **dual-system paradigm** inspired by human cognitive architecture:

- **System 1 (Jev / TypeSafe)** — Fast, typed, deterministic gatekeeper. Handles domain routing, test outcome branching, diff risk classification, and loop control decisions via structured scoring questions. Cloud-hosted via TypeSafe API.
- **System 2 (Generative Brain)** — Deep reasoning engine for open-ended tasks: task decomposition, code synthesis, failure diagnostics, visual inspection, and PR narrative drafting. Supports **two providers**:
  - **Google Antigravity** (`agy` CLI) — Uses your Google Account login session directly, no API key required. Supports Gemini models (`gemini-3.8-flash-high`, `gemini-3.1-pro-high`, `claude-sonnet-4-6`, etc.).
  - **Anthropic Claude** — Direct API integration with `ANTHROPIC_API_KEY`.

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
     System 2 reads domain files,                       System 2 generates
     diagnoses & answers user.                          multi-step execution plan.
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
                                              │     │  Frontend Quality Gate    │
                                              │     │  (MCP browser verify)     │
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
```

### Execution Lifecycle

1. **Scope Gate** — System 1 categorizes the task into a codebase domain (from `modules.json`) and determines `change_type` (`feature_request`, `bug_investigation`, `question_only`, `unclear`). Low-confidence results halt and request clarification.
2. **Task Planning** — System 2 decomposes the goal into atomic, sequential implementation steps with file-level targeting.
3. **Execution & Self-Healing Loop** — For each step:
   - System 2 generates code changes (complete file replacements).
   - The configured test command runs via a bounded subprocess with full process-tree termination.
   - **Frontend Quality Gate** — If the step touches frontend files and a web project is detected (`package.json` / `index.html`), BrainFrog automatically:
     - Runs `npm run build` to verify compilation
     - Starts the dev server and polls until responsive
     - Opens a real Chromium browser via MCP, navigates to the app
     - Captures console errors, failed network requests, and a full-page screenshot
     - Feeds any errors back to System 2 for automated fixing
   - System 1 evaluates outcomes and decides `next_action` (`open_pr`, `retry_fix`, `escalate_human`, `abandon`).
4. **PR Drafting & Risk Gate** — System 2 produces PR title/description; System 1 scores `diff_risk` and `safe_to_proceed`. If `--auto-pr` is enabled and criteria are met, BrainFrog branches, commits, pushes, and creates a GitHub PR automatically.

---

## Directory Structure

```
.
├── cli.py                       # Terminal entrypoint, interactive REPL, TUI & slash commands
├── orchestrator.py              # State machine: System 1 ↔ System 2, tests, git, quality gates
├── BRAINFROG.md                 # Persistent system guidelines & architectural rules
├── DESIGN.md                    # TUI visual design specification (tokens, colors, layout)
├── pyproject.toml               # Packaging, dependencies, and entry points
├── requirements.txt             # Python package dependencies
├── .env.example                 # Environment variable template
│
├── core/                        # Core engine modules
│   ├── __init__.py              # Package exports
│   ├── config.py                # System 1 backend resolution & env config
│   ├── modules.py               # Domain registry, tech stack detection, auto-discovery
│   ├── plans.py                 # Plan documents, mode management (build/plan), staleness checks
│   ├── memory.py                # Workspace & global learning persistence
│   ├── skills.py                # Skill indexing, selection, script execution
│   ├── mcp_client.py            # [Layer 1] Generic stdio MCP JSON-RPC 2.0 client
│   └── frontend_quality_gate.py # [Layer 2] Browser verification pipeline via MCP
│
├── system1/                     # System 1: Fast typed decision layer
│   ├── base.py                  # Abstract interfaces & decision contracts (Choice/Score/Noul)
│   └── typesafe_client.py       # Cloud backend calling TypeSafe Jev API
│
├── system2/                     # System 2: Generative reasoning layer
│   ├── __init__.py              # Provider auto-detection & System2Client factory
│   ├── claude_client.py         # Anthropic Claude integration (plan, write, fix, diagnose, PR)
│   ├── antigravity_client.py    # Google Antigravity integration (agy CLI, Google Auth session)
│   ├── json_utils.py            # Robust JSON extraction & repair from LLM output
│   └── visual_inspector.py      # Headless screenshot capture & multimodal visual critique
│
├── security/                    # Security subsystem
│   ├── auth_manager.py          # API key management, rotation, and validation
│   └── git_guard.py             # Sensitive file scanning, .gitignore enforcement
│
├── tests/                       # Test suite
│   ├── test_mcp_quality_gate.py # MCP client & quality gate unit tests
│   ├── test_modes.py            # Build/plan mode tests
│   ├── test_system1_jev.py      # System 1 Jev integration tests
│   ├── test_system2_providers.py# System 2 provider tests
│   ├── test_visual_inspector.py # Visual inspector tests
│   ├── test_git_guard.py        # Security guard tests
│   ├── test_memory.py           # Memory persistence tests
│   ├── test_skills.py           # Skill system tests
│   └── ...
│
└── modules.example.json         # Example domain registry template
```

---

## Component Responsibilities

| Component | Files | Responsibilities |
| :--- | :--- | :--- |
| **System 1 Client** | `system1/base.py`, `typesafe_client.py` | Structured questions (`ChoiceQuestion`, `ScoreQuestion`, `NoulQuestion`); high-speed typed scoring without prompt drift; TypeSafe Jev API integration. |
| **System 2 Client** | `system2/claude_client.py`, `antigravity_client.py` | Multi-step planning, full-file code generation, error triage, codebase Q&A, PR drafting. **Antigravity** provider uses `agy` CLI with Google Auth (no API key). **Claude** provider uses Anthropic API directly. |
| **Orchestrator** | `orchestrator.py` | Central state machine; subprocess execution with strict timeouts and process tree termination; atomic file writing; self-healing retry loop with circuit breakers; git lifecycle; frontend quality gate trigger; visual inspection gate. |
| **MCP Client** | `core/mcp_client.py` | Generic stdio-based MCP JSON-RPC 2.0 transport: process lifecycle, handshake, `tools/list`, `tools/call` with timeout & response normalization (text & image blocks). Domain-agnostic — reusable for any MCP server. |
| **Frontend Quality Gate** | `core/frontend_quality_gate.py` | 7-step browser verification via `brainfrog-verify-mcp`: build → dev server → navigate → console errors → network errors → screenshot → verdict. Fully automated cleanup (zero zombie processes). |
| **Visual Inspector** | `system2/visual_inspector.py` | Headless Chrome/Edge screenshot capture of rendered HTML; multimodal visual critique via System 2; anti-slop detection (padding, overlap, unstyled elements). |
| **Domain Registry** | `core/modules.py`, `modules.json` | Tech stack auto-detection; preset module generation; path matching; sensitivity flags (sensitive domains block auto-PR). |
| **Planning Engine** | `core/plans.py` | Plan document persistence; build/plan mode switching; file hash staleness detection; safe readonly command validation. |
| **Memory** | `core/memory.py` | Workspace and global learning persistence; contextual retrieval for past fixes and decisions. |
| **Skills** | `core/skills.py` | Skill indexing, selection, content loading; script validation and sandboxed execution. |
| **Security** | `security/auth_manager.py`, `security/git_guard.py` | API key management/rotation; `.gitignore` enforcement; staged file scanning for secret leaks. |
| **CLI & TUI** | `cli.py` | Interactive REPL with auto-completion; rich terminal interface; slash commands; shell passthrough (`!cmd`); instant undo (`/undo`) and diff (`/diff`). |

---

## Setup & Installation

### 1. Prerequisites

- **Python 3.10+**
- **Node.js 18+** (required for MCP verification server)
- **Git CLI** (and `gh` GitHub CLI if using `--auto-pr`)
- **One of the following System 2 providers:**
  - Google Antigravity (`agy` CLI) with active Google Account login — **no API key needed**
  - Anthropic API key (`ANTHROPIC_API_KEY`)
- **TypeSafe API key** (`TYPESAFE_API_KEY` or `JEV_API_KEY` for System 1)

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
# Edit .env — see Environment Variables below
```

### 3. MCP Verification Server (Optional but Recommended)

The Frontend Quality Gate requires the `brainfrog-verify-mcp` server:

```bash
# Build the MCP verification server (one-time)
cd ../brainfrog-verify-mcp
npm install
npm run build

# The server entry point is at dist/index.js
# BrainFrog locates it automatically via BRAINFROG_VERIFY_MCP_PATH
```

### 4. Domain Configuration (`modules.json`)

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

> **Note:** Domains marked `"sensitive": true` will never auto-open a PR. BrainFrog can also auto-discover modules from your project structure.

---

## Environment Variables

| Variable | Required | Default | Description |
| :--- | :---: | :--- | :--- |
| `SYSTEM2_PROVIDER` | No | auto-detected | `antigravity` (Google Auth) or `claude` (Anthropic API) |
| `ANTIGRAVITY_MODEL` | No | `gemini-3.8-flash-high` | Model for Antigravity provider |
| `ANTIGRAVITY_BIN` | No | auto-detected | Path to `agy.exe` if not in `~/.gemini/bin` or `PATH` |
| `ANTHROPIC_API_KEY` | If Claude | — | Anthropic API key (only if `SYSTEM2_PROVIDER=claude`) |
| `ANTHROPIC_MODEL` | No | `claude-sonnet-5` | Claude model override |
| `TYPESAFE_API_KEY` | Yes | — | TypeSafe / Jev System 1 API key |
| `JEV_API_KEY` | Alt | — | Alternative key name for System 1 |
| `BRAINFROG_VERIFY_MCP_PATH` | No | `C:\dame-project\tools\brainfrog-verify-mcp\dist\index.js` | Path to MCP verification server entry point |
| `BRAINFROG_DEV_URL` | No | `http://localhost:3000` | Default dev server URL for browser verification |
| `BRAINFROG_BROWSER_BIN` | No | auto-detected | Path to Chrome/Edge for visual inspection |
| `BRAINFROG_MAX_OUTPUT_TOKENS` | No | `64000` | Max output tokens for code generation |

---

## Usage

### Interactive REPL Mode (Recommended)

```bash
brainfrog --repo /path/to/target/repo
# or
bf --repo /path/to/target/repo
```

Inside the REPL:
- Type your prompt directly: `Add healthcheck endpoint at /api/health`
- Mention files with autocompletion: `@app/api/routes.py`
- Execute terminal commands: `!pytest`
- Slash commands:

| Command | Description |
| :--- | :--- |
| `/status` | View current repository, test command, and active models |
| `/diff` | Preview uncommitted git changes |
| `/undo` | Cleanly revert uncommitted changes made by the agent |
| `/rules` | Display active system guidelines (`BRAINFROG.md`) |
| `/stats` / `/cost` | Show token consumption and estimated session cost |
| `/test-cmd <cmd>` | Switch the active test command dynamically |
| `/clear` | Clear terminal screen |
| `/help` | Display command cheat sheet |
| `/exit` | Exit BrainFrog |

### Non-Interactive Single Task Mode

```bash
# Using Antigravity (Google Auth — no API key)
brainfrog \
  --repo /path/to/target/repo \
  --task "Fix ZeroDivisionError in calc.py" \
  --test-cmd "pytest -q" \
  --backend antigravity

# Using Claude (Anthropic API)
brainfrog \
  --repo /path/to/target/repo \
  --task "Implement user logout endpoint" \
  --test-cmd "pytest app/tests/test_auth.py" \
  --backend claude

# Full automated run with Auto-PR
brainfrog \
  --repo /path/to/target/repo \
  --task "Add dark mode toggle to settings page" \
  --test-cmd "npm test" \
  --auto-pr \
  --pr-risk-ceiling medium
```

---

## Frontend Quality Gate (MCP Browser Verification)

BrainFrog includes an automated browser verification pipeline that runs **after every frontend code change**. This is powered by a dedicated MCP server (`brainfrog-verify-mcp`) that provides real Chromium browser control:

### How It Works

```
  [Code Change Detected]
          │
          ▼
  ┌─────────────────────┐
  │  npm run build      │  ← Compilation check (120s timeout)
  └─────────────────────┘
          │ exit code 0
          ▼
  ┌─────────────────────┐
  │  npm run dev/start  │  ← Start dev server
  └─────────────────────┘
          │ poll until responsive (30s)
          ▼
  ┌─────────────────────┐
  │  Browser Navigate   │  ← Real Chromium via MCP
  └─────────────────────┘
          │
     ┌────┴────┐
     ▼         ▼
  Console   Network     ← Capture errors & failed requests
  Errors    Logs
     │         │
     └────┬────┘
          ▼
  ┌─────────────────────┐
  │  Screenshot         │  ← Full-page viewport capture
  └─────────────────────┘
          │
          ▼
  ┌─────────────────────┐
  │  PASS / FAIL        │  ← 0 errors = PASS
  └─────────────────────┘
```

### Key Features

- **Automatic URL detection** — Parses dev server output to detect the actual listening URL (supports Vite, Next.js, CRA, etc.)
- **Poll-based readiness** — No static `sleep()`. Polls the dev server with retry logic until it actually responds.
- **Build timeout** — 120-second deadline for `npm run build` with explicit process termination on timeout.
- **Navigation timeout** — 30-second deadline for dev server readiness.
- **Zero zombie processes** — Guaranteed cleanup in `finally` block: stops background processes, closes browser instances, and disconnects MCP client.
- **Concurrency-safe** — Uses `os.getpid() + timestamp` for unique `instanceId` generation, enabling safe parallel execution across multiple BrainFrog processes.

### MCP Server Tools

The verification server exposes these tools via stdio MCP protocol:

| Tool | Description |
| :--- | :--- |
| `navigate` | Navigate browser to URL |
| `screenshot` | Capture viewport/full-page screenshot |
| `click` | Click element on page |
| `type` | Type text into input element |
| `evaluate` | Execute JavaScript in browser context |
| `accessibility_snapshot` | Get accessibility tree snapshot |
| `get_console_logs` | Retrieve browser console logs (filterable) |
| `get_network_logs` | Retrieve network request logs (filterable) |
| `throttle_network` | Simulate network conditions |
| `throttle_cpu` | Simulate CPU throttling |
| `start_process` | Start a background process (build/dev server) |
| `read_process_output` | Read stdout/stderr from running process |
| `stop_process` | Terminate background process |
| `close_instance` | Close browser instance and cleanup |

---

## Reliability, Safety & Idempotency

- **Atomic File Writing** — File updates are staged and replaced atomically (`os.replace`) to eliminate 0-byte or corrupted files during interruptions (`Ctrl+C`).
- **Bounded Process Tree Termination** — Test executions enforce strict timeouts (default 60s); timeouts recursively terminate the entire child process hierarchy (`taskkill` on Windows, process groups on POSIX) to avoid orphan processes.
- **Build & Navigation Timeouts** — The quality gate enforces explicit deadlines: 120s for builds, 30s for dev server readiness. Timeout expiry triggers process termination and structured error reporting.
- **Fail-Safe Rollback** — The `/undo` command leverages git status verification to revert working directory modifications without altering committed history.
- **Circuit Breakers** — Multi-step and retry loops enforce fixed maximum thresholds (`max_retries = 3`) to prevent infinite repair loops.
- **Sensitive Domain Safeguard** — Hard business constraints on domains marked `sensitive: true` block auto-PR regardless of model confidence.
- **Concurrency Isolation** — Each BrainFrog process generates unique instance IDs using `os.getpid() + timestamp`, preventing collisions in parallel multi-process environments. Verified with concurrent execution tests.
- **Secret Leak Prevention** — `git_guard` scans staged changes and enforces `.gitignore` rules for sensitive files (`.env`, API keys, credentials).

---

## Running Tests

```bash
# Run full test suite
pytest tests/ -v

# Run specific test modules
pytest tests/test_mcp_quality_gate.py -v     # MCP client & quality gate
pytest tests/test_system2_providers.py -v    # System 2 provider tests
pytest tests/test_git_guard.py -v            # Security guard tests
```

---

## Two-Layer MCP Architecture

The MCP integration follows a clean two-layer separation:

**Layer 1: `core/mcp_client.py`** — Generic, reusable stdio MCP client. Handles JSON-RPC 2.0 protocol mechanics (handshake, tool discovery, tool execution, timeout, cleanup). Zero domain knowledge — works with any MCP server.

**Layer 2: `core/frontend_quality_gate.py`** — Domain-specific verification pipeline. Consumes `McpClient` to execute the 7-step quality gate workflow (build → dev server → navigate → console → network → screenshot → verdict). Returns structured `QualityGateResult` with actionable error context for System 2.

This separation ensures the MCP client can be reused for future MCP server integrations without coupling to frontend verification logic.

