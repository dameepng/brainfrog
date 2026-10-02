# BrainFrog 🐸 — General Agent Runtime & Multi-Channel Platform

> Dual-system agent runtime powered by a hybrid System 1 (Jev / deterministic fast decisions) + System 2 (Generative LLM reasoning), featuring multi-channel messaging (CLI, Telegram, WhatsApp), strict security trust boundaries, session isolation, and automated browser verification.

---

## What Makes BrainFrog Different

BrainFrog is not just an LLM wrapper that blindly spits out code in your terminal. It is a single, coherent, dual-system agent runtime designed for high reliability across local and remote interfaces:

- **Single Coherent Agent Runtime** — `CHANNEL != AGENT`. Whether messages arrive from the local CLI, Telegram, or WhatsApp, they flow through a unified runtime pipeline (`BrainFrogRuntime`) without duplicating reasoning, orchestrator loops, or tools.
- **Strict Channel Trust Boundaries** — Remote messaging channels (`REMOTE_CHANNEL`) are treated as untrusted input. Authentication and strict allowlisting happen before messages reach the agent. Privileged actions (arbitrary shell execution, destructive git, credential access) are unconditionally blocked for remote channels while preserved for the high-trust `LOCAL_CLI`.
- **Session Isolation** — Sessions are strictly isolated using three-part composite keys (`channel:user_id:conversation_id`). User A in Telegram can never access or pollute User B's state, memory, or history.
- **Hybrid Decision-Making (Dual-System)** — **System 1 (Jev / TypeSafe)** serves as a fast, deterministically typed gatekeeper for domain routing, risk evaluation, failure triage, and loop oversight with zero prompt drift. **System 2 (Generative Brain)** decomposes plans, analyzes code, and executes steps via Antigravity, Claude, OpenAI, OpenRouter, or custom OpenAI-compatible endpoints.
- **Automated Browser Verification (MCP Quality Gate)** — For frontend changes, BrainFrog automatically builds projects (`npm run build`), launches dev servers, drives Chromium via Model Context Protocol (MCP), inspects console errors, catches network failures (404/500), and records visual screenshots.
- **Permanent PR Proofs (Immutable Screenshot Storage)** — Visual verification screenshots are committed to an isolated orphan branch (`pr-proof-assets`) linked by exact commit SHA — ensuring PR visual evidence remains accessible forever without link rot.
- **Diagnostics & Self-Inspection** — Built-in `doctor` command validates System 1, System 2, MCP servers, memory banks, skills, and channel configurations without ever exposing API secrets.

---

## Architecture

```
Incoming Channel (CLI / Telegram / WhatsApp)
                     │
                     ▼
          Authentication & Allowlist
                     │
                     ▼
             Session Resolution
       (channel:user_id:conversation_id)
                     │
                     ▼
              BrainFrogRuntime
        (Thin Facade & Trust Policy)
                     │
                     ▼
                  System 1
          (Jev / TypeSafe Scope Gate)
                     │
                     ▼
          Existing Orchestrator
          (State Machine & Retry Loop)
                     │
                     ▼
                  System 2
    (Antigravity / Claude / OpenAI / OpenRouter)
                     │
        ┌────────────┼────────────┐
        │            │            │
       MCP         Tools        Memory & Skills
        │            │            │
        └────────────┼────────────┘
                     │
                     ▼
             Verification Gate
                     │
                     ▼
         Outgoing Channel Response
```

### Core Components

| Component | File Path | Role & Responsibilities |
| :--- | :--- | :--- |
| **BrainFrogRuntime** | [`core/runtime/runtime.py`](core/runtime/runtime.py) | Thin runtime facade: resolves sessions, enforces channel trust permissions, delegates to Orchestrator, and normalizes events. |
| **Session Manager** | [`core/runtime/session.py`](core/runtime/session.py) | Isolates conversational history and state strictly by `channel:user_id:conversation_id`. |
| **Permission Policy** | [`core/runtime/permissions.py`](core/runtime/permissions.py) | Evaluates trust levels (`LOCAL_CLI` vs `REMOTE_CHANNEL`); blocks remote shell execution, destructive git, and secret access. |
| **Multi-Channel Gateway** | [`core/runtime/gateway.py`](core/runtime/gateway.py) | Manages channel lifecycles, health reporting, and graceful shutdown across CLI, Telegram, and WhatsApp. |
| **Doctor Diagnostics** | [`core/runtime/doctor.py`](core/runtime/doctor.py) | Comprehensive system diagnostics inspecting auth, System 1, System 2, MCP, channels, and skills with automated secret redaction. |
| **Channel Layer** | [`core/channels/`](core/channels/) | Extensible channel adapters (`base.py`, `telegram.py`, `whatsapp.py`) converting incoming/outgoing messages to normalized models. |
| **Orchestrator** | [`orchestrator.py`](orchestrator.py) | Central state machine: orchestrates execution cycles, manages subprocesses with process-tree termination, atomic file writing, self-healing retry loops, and reporting. |
| **Frontend Quality Gate** | [`core/frontend_quality_gate.py`](core/frontend_quality_gate.py) | 7-step automated browser verification pipeline (build → dev server → navigate → console → network → screenshot → verdict) executed via MCP servers. |
| **PR Proof Generator** | [`core/pr_proof.py`](core/pr_proof.py) | Automated desktop & mobile screenshots uploaded to the orphan branch `pr-proof-assets` using isolated worktrees and formatted with permanent commit SHAs. |
| **MCP Client (Layer 1)** | [`core/mcp_client.py`](core/mcp_client.py) | Generic stdio JSON-RPC 2.0 client for communicating with MCP servers; manages handshakes, process lifecycles, and tool invocations. |
| **System 1 (Jev)** | [`system1/`](system1/) | Strongly typed, structured decision layer (`ChoiceQuestion`, `ScoreQuestion`, `NoulQuestion`) powered by the TypeSafe Jev API with zero risk of prompt drift. |
| **System 2 (Generative)** | [`system2/`](system2/) | Generative reasoning engine supporting **Google Antigravity**, **Anthropic Claude**, **OpenAI**, **OpenRouter**, and **generic OpenAI-compatible APIs**. |
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
# Options: antigravity (Google Auth), claude, openai, openrouter, or custom
SYSTEM2_PROVIDER=antigravity
ANTIGRAVITY_MODEL=gemini-3.8-flash-high

# If using Claude:
# ANTHROPIC_API_KEY=your_anthropic_api_key_here
# ANTHROPIC_MODEL=claude-sonnet-5

# If using OpenAI:
# OPENAI_API_KEY=sk-...
# OPENAI_MODEL=gpt-4o

# If using OpenRouter:
# OPENROUTER_API_KEY=sk-or-...
# OPENROUTER_MODEL=anthropic/claude-3.5-sonnet

# If using Generic / Custom OpenAI-compatible endpoint:
# BRAINFROG_MODEL_PROVIDER=custom
# CUSTOM_BASE_URL=https://my-llm-host.internal/v1
# CUSTOM_API_KEY=secret_or_empty
# CUSTOM_MODEL=llama-3.3-70b-instruct

# --- System 1 Provider (Jev / TypeSafe) ---
TYPESAFE_API_KEY=your_typesafe_api_key_here

# --- Messaging Channels (Optional) ---
# Telegram:
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrSTUvwxYZ
TELEGRAM_ALLOWED_USERS=12345678,98765432
TELEGRAM_ENABLED=false

# WhatsApp (Transport interface / Mock or Cloud API):
WHATSAPP_TRANSPORT=mock
WHATSAPP_ALLOWED_USERS=+1234567890,+1987654321
WHATSAPP_ENABLED=false
# WHATSAPP_API_TOKEN=your_cloud_api_token
# WHATSAPP_PHONE_NUMBER_ID=your_phone_number_id

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
- `/doctor` — Run comprehensive runtime self-diagnostics with safe secret masking
- `/gateway` — View active multi-channel gateway status and registered adapters
- `/provider` — Switch System 2 AI provider (`antigravity`, `claude`, `openai`, `openrouter`, `custom`)
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

#### Multi-Channel Gateway Commands
```bash
# Check status of all messaging channels and gateway health
brainfrog gateway status

# Start the gateway daemon with specific or all enabled channels
brainfrog gateway start --channels telegram,whatsapp
brainfrog gateway start --repo /path/to/project
```

#### Doctor Self-Inspection
```bash
# Run comprehensive runtime self-diagnostics
brainfrog doctor
```
Inspects:
- Active Google authentication and API credentials
- System 1 (TypeSafe / Jev) connectivity
- System 2 active provider and model
- MCP servers and JSON-RPC readiness
- Memory banks and custom skills
- Telegram and WhatsApp transport configurations
- Active channel security policies
*Note: All secret keys, auth tokens, and session identifiers are automatically redacted (`sk-...XXXX`).*

---

## Multi-Channel Trust Boundary & Security

BrainFrog enforces a strict distinction between **Local** and **Remote** channels:

```
Channel Type     Trust Level        Permissions Allowed
────────────     ───────────        ─────────────────────────────────────────────────
LOCAL_CLI        HIGH_TRUST         Full capability: shell commands, file edits, git,
                                    testing, memory, skills, MCP tools.

REMOTE_CHANNEL   RESTRICTED         Safe execution only: queries, memory reads, read-only
(Telegram /                         inspections. Blocked unconditionally:
 WhatsApp)                          - Arbitrary shell commands
                                    - Destructive git operations (reset, force-push)
                                    - Unrestricted file mutations
                                    - Credential or secret inspection
                                    - Deployment / production commands
```

### Pre-Runtime Gate & Session Isolation
1. **Pre-Runtime Allowlist**: Every remote channel requires explicit allowlisting (`TELEGRAM_ALLOWED_USERS`, `WHATSAPP_ALLOWED_USERS`). If the allowlist is empty or the sender is not on the list, incoming messages are discarded with a security audit event before entering the runtime.
2. **Session Scoping**: Sessions are strictly keyed by `channel:user_id:conversation_id` (e.g., `telegram:12345678:12345678`, `whatsapp:1234567890:1234567890`, `cli:local:default`). Cross-session context leakage is architecturally prohibited.
3. **System 1 Intent Triage**: System 1 evaluates intent risk before delegating execution to the Orchestrator. Remote actions that violate permission boundaries return clear, non-leaking rejection messages.
4. **WhatsApp Channel Parity (Phase 12)**:
   - **Pluggable Transports**: Supports deterministic `MockWhatsAppTransport` (for offline unit/E2E testing) and `WhatsAppCloudTransport` (official Meta Graph API).
   - **Delivery & Chunking**: Long responses exceeding 4,000 characters are automatically split and delivered as sequentially ordered messages without content truncation.
   - **Deterministic Security**: WhatsApp remote execution is constrained by the same runtime security boundary used by other remote channels and covered by automated E2E regression tests.
   - **Offline & Live Testing**: The test suite executes 100% offline without requiring credentials. Optional live WhatsApp smoke tests can be enabled with `BRAINFROG_WHATSAPP_LIVE=1`.

---

## Persistent Runtime State & Session Recovery

BrainFrog provides deterministic, filesystem-backed session persistence so conversation continuity survives daemon restarts and process termination without requiring any external database services.

```
Incoming Message
      │
      ▼
   Channel (Telegram / WhatsApp / CLI)
      │
      ▼
BrainFrogRuntime
      │
      ▼
SessionManager (Thread-safe with RLock)
      │
      ▼
SessionStore (FileSessionStore / InMemorySessionStore)
      │
      ▼
.brainfrog/sessions/<sha256_hash>.json
```

### 1. Storage Location & Naming
- **Session Files**: Stored within the repository at `.brainfrog/sessions/`.
- **Safe Filenames**: Session IDs (`<channel>:<user_id>:<conversation_id>`) are hashed using SHA-256 (`hashlib.sha256(session_id).hexdigest()[:32] + ".json"`). Raw user/channel inputs never touch the filesystem directly, making path traversal (`../../evil`, drive letters, control characters) mathematically impossible.
- **Git Guard Defense**: `.brainfrog/sessions/` is protected in `security/git_guard.py`, automatically ensured in `.gitignore`, and purged from git tracking if accidentally staged.

### 2. Schema Specification (Version 1)
```json
{
  "schema_version": 1,
  "session_id": "telegram:12345678:12345678",
  "channel": "telegram",
  "user_id": "12345678",
  "conversation_id": "12345678",
  "created_at": 1790968179.698,
  "last_active_at": 1790968185.120,
  "active_mode": "build",
  "active_skill": null,
  "active_model": "claude-3-7-sonnet-20250219",
  "active_provider": "claude",
  "plan_context": null,
  "metadata": {},
  "history": [
    {
      "role": "user",
      "content": "My name for this conversation is BrainFrog.",
      "timestamp": 1790968179.700
    },
    {
      "role": "assistant",
      "content": "Understood! I will remember that.",
      "timestamp": 1790968185.120
    }
  ]
}
```

### 3. Atomic Writes & Platform Compatibility
- Writes are executed via a temporary file in the same directory (`.tmp_<hash>_<uuid>.json`), flushed, fsynced to disk, and atomically swapped into place using `os.replace`.
- Fully compatible with POSIX and Windows filesystem semantics (preventing race conditions and zero-byte file states during unexpected process termination).

### 4. Crash & Corruption Recovery
- **Quarantine Policy**: If a session file contains truncated data, invalid JSON, or missing required fields, it is safely moved to `.brainfrog/sessions/corrupt/<timestamp>_<filename>` for post-mortem analysis.
- **Zero Daemon Crash**: The runtime logs an audit warning and transparently initializes a clean session state. Corrupted files never crash the gateway or expose error internals to remote users.
- **Schema Safety**: If a file has an unsupported schema version (`> CURRENT_SESSION_SCHEMA_VERSION`), it is safely preserved and ignored rather than overwritten or misparsed.

### 5. Multi-Session Isolation & Security
- **Strict Isolation**: Distinct channel/user/chat identifiers produce distinct hashes and files. User A on Chat 1 cannot access User B or User A's Chat 2 history.
- **Secret Scrubbing on Disk**: Even if sensitive tokens or keys appear in user prompts or tool responses, `scrub_secrets()` redacts them (`sk-...XXXX`, bot tokens, bearer headers) before data is serialized to disk.
- **Remote Read-Only Invariant**: Persistence operates strictly on internal runtime metadata files in `.brainfrog/sessions/`. It cannot be leveraged by remote commands to modify workspace code or project files.

### 6. Reset & Lifecycle Behavior
- **Reset Command (`/reset`, `/new`)**: Purges the active session state from memory and deletes the persisted `.json` file from disk. The next message starts with a clean slate.
- **Channel Defaults**:
  - **Telegram & WhatsApp Gateway**: Persistent by default (`persist_sessions=True`). Full conversation continuity across daemon restarts.
  - **Interactive CLI**: Ephemeral by default (each fresh terminal invocation starts with a clean context, while in-session REPL turns are preserved until `/reset`, `/new`, or terminal exit).

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
