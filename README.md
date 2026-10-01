# BrainFrog CLI 🐸

> Agentic coding CLI powered by a hybrid System 1 (Jev / fast structured decisions) + System 2 (Generative LLM), equipped with automated browser verification before code is considered complete.

---

## What Makes BrainFrog Different

BrainFrog is not just an LLM wrapper that blindly spits out code in your terminal. It is built with rigorous oversight and end-to-end automated verification:

- **Automated Browser Verification (MCP Quality Gate)** — For every frontend change, BrainFrog automatically builds the project (`npm run build`), launches a local dev server, opens a real Chromium browser via Model Context Protocol (MCP), inspects console errors, detects failed network requests (404/500), and captures page screenshots before work is declared complete.
- **Hybrid Decision-Making (Dual-System)** — Adopts a cognitive dual-process architecture: **System 1 (Jev / TypeSafe)** serves as a fast, deterministically typed gatekeeper for domain routing, failure branch triage, and diff risk assessment; **System 2 (Generative Brain)** handles deep reasoning, plan decomposition, and code synthesis.
- **Permanent PR Proofs (Immutable Screenshot Storage)** — Desktop and mobile visual screenshots are automatically embedded in the Pull Request body for every frontend modification. Screenshots are stored on a dedicated orphan branch (`pr-proof-assets`) linked by exact commit SHA — ensuring image links remain permanently accessible (`200 OK`) and never break even after PR branches are deleted upon merge.
- **Hardened Infrastructure & Strict Protection** — Enforces mandatory branch protection on `main`, automated CI pipelines (`lint-typecheck-test`) pinned to exact 40-character commit SHAs (immune to supply-chain attacks), secret leak prevention (GitGuardian + internal Git Guard), and automated cleanup of ephemeral verification PRs (`stale.yml`) without disrupting active work.

---

## Architecture

```
                                  [User Prompt]
                                        │
                                        ▼
                          ┌───────────────────────────┐
                          │   System 1: Scope Gate    │
                          │ (Domain Routing & Typing) │
                          └───────────────────────────┘
                                        │
                ┌───────────────────────┴───────────────────────┐
                ▼                                               ▼
         [Question Only]                                [Code Mutation]
    System 2 diagnoses &                           System 2 formulates a
    answers the user prompt.                       multi-step execution plan.
    (No file modifications)                                     │
                                                                ▼
                                                  ┌───────────────────────────┐
                                            ┌───► │ Step N: Code Synthesis    │
                                            │     └───────────────────────────┘
                                            │                   │
                                            │                   ▼
                                            │     ┌───────────────────────────┐
                                            │     │ Run Local Unit Tests      │
                                            │     └───────────────────────────┘
                                            │                   │
                                            │                   ▼
                                            │     ┌───────────────────────────┐
                                            │     │  Frontend Quality Gate    │
                                            │     │  (MCP Browser Verify)     │
                                            │     └───────────────────────────┘
                                            │                   │
                                            │                   ▼
                                            │     ┌───────────────────────────┐
                                            │     │    System 1: Loop Gate    │
                                            │     │  (Evaluate & Next Step)   │
                                            │     └───────────────────────────┘
                                            │       ├── open_pr ──► [Auto-Attach PR Proof]
                                            └── retry_fix          ├── escalate_human
                                                                   └── abandon
```

### Core Components

| Component | File Path | Role & Responsibilities |
| :--- | :--- | :--- |
| **Orchestrator** | [`orchestrator.py`](orchestrator.py) | Central state machine: orchestrates execution cycles, manages subprocesses with process-tree termination, atomic file writing, self-healing retry loops, and reporting. |
| **Frontend Quality Gate** | [`core/frontend_quality_gate.py`](core/frontend_quality_gate.py) | 7-step automated browser verification pipeline (build → dev server → navigate → console → network → screenshot → verdict) executed via MCP servers. |
| **PR Proof Generator** | [`core/pr_proof.py`](core/pr_proof.py) | Automated desktop & mobile screenshots uploaded to the orphan branch `pr-proof-assets` using isolated worktrees and formatted with permanent commit SHAs. |
| **MCP Client (Layer 1)** | [`core/mcp_client.py`](core/mcp_client.py) | Generic stdio JSON-RPC 2.0 client for communicating with MCP servers; manages handshakes, process lifecycles, and tool invocations. |
| **System 1 (Jev)** | [`system1/`](system1/) | Strongly typed, structured decision layer (`ChoiceQuestion`, `ScoreQuestion`, `NoulQuestion`) powered by the TypeSafe Jev API with zero risk of prompt drift. |
| **System 2 (Generative)** | [`system2/`](system2/) | Generative reasoning engine. Supports **Google Antigravity** (`agy` CLI with free Google Auth sessions) and **Anthropic Claude** (Claude Sonnet / Opus via API Key). |
| **Security & Git Guard** | [`security/`](security/) | Pre-stage scanning engine preventing accidental leaks of sensitive files (`.env`, tokens, private keys) along with auth key rotation utilities. |
| **Stale PR Lifecycle** | [`.github/workflows/stale.yml`](.github/workflows/stale.yml) & [`.github/scripts/protect_active_prs.py`](.github/scripts/protect_active_prs.py) | Automated cleanup of ephemeral testing PRs with automated `keep-open` label protection for active development PRs. |

---

## Installation & Getting Started

### 1. Prerequisites

- **Python 3.10+** (Python 3.11 or newer recommended)
- **Node.js 18+** (required when utilizing the MCP browser verification server)
- **Git CLI** and **GitHub CLI (`gh`)** (required for automated PR creation and repository management)
- One of the following System 2 providers:
  - **Google Antigravity (`agy` CLI)** with an active Google Account login session (*no API key required*), or
  - **Anthropic API Key** (`ANTHROPIC_API_KEY`)
- **TypeSafe / Jev API Key** (`TYPESAFE_API_KEY`) for System 1

### 2. Setup

```bash
# Clone the repository
git clone https://github.com/dameepng/brainfrog.git
cd brainfrog

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
pip install -e .

# Prepare environment configuration
cp .env.example .env
```

### 3. Environment Configuration (`.env`)

Configure the variables inside `.env`:

```ini
# --- System 2 Provider ---
# Options: antigravity (Google Auth) or claude (Anthropic API Key)
SYSTEM2_PROVIDER=antigravity
ANTIGRAVITY_MODEL=gemini-3.8-flash-high

# If using Claude:
# ANTHROPIC_API_KEY=your_anthropic_api_key_here
# ANTHROPIC_MODEL=claude-sonnet-5

# --- System 1 Provider (Jev / TypeSafe) ---
TYPESAFE_API_KEY=your_typesafe_api_key_here

# --- Quality Gate & Browser Verification (Optional) ---
# BRAINFROG_VERIFY_MCP_PATH=/path/to/brainfrog-verify-mcp/dist/index.js
# BRAINFROG_DEV_URL=http://localhost:3000
```

### 4. Running BrainFrog

#### Interactive REPL Mode — Recommended
```bash
brainfrog --repo /path/to/your/project
# or using the shorthand alias
bf --repo /path/to/your/project
```

Quick in-REPL commands:
- `@path/file.py` — Autocomplete repository files by typing `@` to pin file paths directly into the prompt
- `@path/image.png` — Attach image (`.png`, `.jpg`, `.jpeg`, `.webp`) as multimodal vision content block to System 2 (Claude / Gemini)
- `/paste` (or `Ctrl+V` if terminal passes key through) — Grab screenshot/image directly from OS clipboard and stage for the next prompt
- `/screenshot [target]` — Capture real-time screenshot of running dev server/UI via MCP or headless browser and attach to prompt
- `/attach <path>` — Attach an image file from disk to the prompt queue
- `/images` / `/clear-images` — List currently staged images or clear the image queue
- `!command` — Execute shell commands directly (e.g., `!pytest`, `!git status`)
- `/mode [plan|build]` — Switch session mode between **Plan** (read-only exploration & plan drafting) and **Build** (code execution)
- `/diff` — Review current Git diff changes
- `/undo` — Cleanly revert uncommitted changes or the latest commit via Git
- `/status` — View current repository status, branch, provider, active model, and test command
- `/provider` — Switch System 2 AI provider (`antigravity` or `claude`)
- `/model` / `/models` — Select or switch active AI models
- `/preview [target]` — Capture and inspect headless visual screenshots of HTML/web UI
- `/rules` — Display active project guidelines loaded from `BRAINFROG.md`
- `/learn <rule>` — Teach a new rule or preference to the BrainFrog memory bank
- `/memory` — Display all learned rules currently persisted in memory
- `/stats` / `/cost` — Review token consumption and estimated session costs

Multi-line input:
- **`Shift+Enter`** — Insert newline without submitting (supported in terminals with extended keyboard protocols like Kitty, WezTerm, and configured terminals)
- **`Alt+Enter`** / **`Option+Enter`** — Insert newline without submitting (universally supported across Windows Terminal, VS Code integrated terminal, iTerm2, macOS Terminal)
- **`\` + `Enter`** — Backslash continuation: type `\` at the end of a line then press Enter to continue on a new line (universal fallback working in 100% of terminals)
- **`Enter`** — Submit prompt (unchanged default behavior)

#### Non-Interactive Mode (Single Task)
```bash
# Execute a task using Google Antigravity (Google Auth Login — free tier)
brainfrog \
  --repo /path/to/your/project \
  --task "Fix ZeroDivisionError in math_utils.py" \
  --test-cmd "pytest -q" \
  --provider antigravity

# Execute a task using Anthropic Claude API
brainfrog \
  --repo /path/to/your/project \
  --task "Implement user logout endpoint" \
  --test-cmd "pytest app/tests/test_auth.py" \
  --provider claude

# Frontend task with auto-PR and visual browser verification
brainfrog \
  --repo /path/to/your/project \
  --task "Add dark mode toggle to the landing page" \
  --test-cmd "npm test" \
  --auto-pr
```

---

## Quality & Security

This repository enforces industry-grade software engineering standards to guarantee reliability and security:

1. **Strict Pull Request Workflow & Branch Protection**:
   - The `main` branch is strictly protected. Direct pushes are rejected (`GH006`).
   - `enforce_admins: true` is active — administrators are also required to go through the Pull Request review process.
2. **Automated CI Validation (`lint-typecheck-test`)**:
   - Every PR must pass syntax compilation checks (`compileall`), security module auditing (`Git Guard`), and full unit test execution with zero error tolerance.
3. **Secret Leak Prevention**:
   - Pre-commit and pre-stage scanning via `security/git_guard.py`.
   - Continuous automated scanning by GitHub GitGuardian Security Checks on every PR.
4. **Supply Chain Defense (Pinned Action SHAs)**:
   - All GitHub Actions workflows are pinned to exact 40-character commit SHAs (rather than mutable semantic tags) to eliminate risks of third-party CI dependency compromises.
5. **Deletion-Proof Visual Evidence Storage**:
   - The `pr-proof-assets` branch is isolated as an append-only orphan branch. Screenshots are permanently referenced by commit SHA, guaranteeing evidence URLs never 404 when feature branches are deleted.
6. **Automated Stale PR Management**:
   - Automated cleanup workflows close inactive testing and verification PRs, while safeguarding legitimate work PRs via automated `keep-open` labels.

---

## Contributing

Community contributions are warmly welcome. Please follow our standard development workflow:

1. **Fork & Clone**:
   ```bash
   git clone https://github.com/<username>/brainfrog.git
   cd brainfrog
   ```
2. **Setup Development Environment**:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   pip install -e .
   ```
3. **Run Local Tests**:
   Ensure all tests pass cleanly before submitting any changes:
   ```bash
   python -m unittest discover -s tests
   ```
4. **Branch Conventions & Submitting a PR**:
   - Create a feature branch off `main` with a descriptive prefix: `feat/feature-name`, `fix/bug-name`, or `docs/changes`.
   - *Note:* Avoid using the `test/` prefix for active development branches, as that prefix is designated for automated PR testing and cleanup lifecycles.
   - Open a Pull Request targeting `main`. Verify that all CI status checks pass.
   - **Important:** Never alter or delete history on the `pr-proof-assets` branch, as it stores permanent proof assets.

---

## License

This project is licensed under the [MIT License](LICENSE).
