# BrainFrog Phase 1B: Command Execution Boundary & Trust Model

**Document ID:** `BF-EXEC-1B-001`\
**Date:** October 19, 2026\
**Auditor:** Antigravity Agentic Pair Programmer\
**Product Positioning:** "Agent says done. BrainFrog proves it."

---

## 1. Executive Summary

A core realization of the Phase 1B audit is that:
> **An executable allowlist is NOT a security sandbox.**

When BrainFrog allowlists `pytest` or `python -m unittest`, it only restricts the entrypoint binary. The Python interpreter immediately proceeds to import and execute test modules, `conftest.py`, and third-party plugins defined within the target repository. In an untrusted repository, these scripts execute with the full privileges of the host developer account.

Therefore, Phase 1B introduces a formal **Execution Trust Boundary**:
1. **Untrusted Mode (Default):** In host environments without an active, disposable OS container sandbox (e.g. Docker/Podman jail), BrainFrog strictly **fails closed** for executable criteria (`BLOCKED`).
2. **Trusted-Operator Mode (`--trust-local-code`):** The human operator explicitly grants permission to execute repository build/test scripts locally, accepting local user privilege exposure.
3. **Hardened Command Validation:** Even in trusted-operator mode, commands are strictly tokenized, shell wrappers and chaining are banned, Python inline code (`-c`) is blocked, and binaries are pinned to system executables to prevent repository shadowing.
4. **Environment Secret Scrubbing:** Child processes run with an aggressively scrubbed environment, removing all API keys, tokens, and cloud credentials.

---

## 2. The Execution Data Flow

```text
Untrusted spec.md
       |
       v
Markdown Parser (core/verifier.py:parse_spec_markdown)
       |
       v
Criterion Command Extraction (cmd_tokens)
       |
       v
Command Safety Gate (core/verifier.py:validate_verification_command)
       |
       +---> Disallowed token / shell wrapper? ----> BLOCKED (Exit Code 2)
       |
       v
Trust Mode Gate
       |
       +---> Untrusted mode & no container? ------> BLOCKED (Exit Code 2)
       |     [Reason: Secure backend unavailable; requires --trust-local-code]
       |
       v
Trusted-Operator Mode (--trust-local-code)
       |
       v
Environment Sanitization (Strip API keys, tokens, cloud credentials)
       |
       v
System Binary Pinning (sys.executable, system PATH - no repo shadowing)
       |
       v
Hardened Subprocess (core/runtime/process.py:run_hardened_subprocess)
       |  - shell=False
       |  - Strict timeouts (with process tree kill)
       |  - Bounded stdout/stderr stream collection
       |  - PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
       |  - core.hooksPath=/dev/null
       |
       v
Evidence Collection & Hashing (stdout.log, stderr.log, SHA-256)
```

---

## 3. Threat Protections & Enforcements

### 3.1 Banned Execution Patterns
The following patterns in `spec.md` commands are strictly blocked before process spawn:
- **Shell Chaining & Redirection:** `&&`, `||`, `;`, `|`, `>`, `<`, `` ` ``, `$()`, `eval`.
- **Shell & Script Interpreters:** `cmd`, `cmd.exe`, `powershell`, `pwsh`, `bash`, `sh`, `zsh`, `wscript`, `cscript`.
- **Arbitrary Python Inline Execution:** `python -c "..."`, `python -i`, `python -m pip`, `python -m ensurepip`.
- **Unauthorized Binaries:** Any binary not in the verified allowlist (`pytest`, `python`, `npm`, `npx`, `yarn`, `pnpm`, `cargo`, `go`, `make`).
- **Path Traversal in Arguments:** Any path arguments referencing `../` outside the repository tree.

### 3.2 Executable Shadowing Prevention
If a target repository contains a file named `pytest.bat` or `python.exe` in its root, naive PATH resolution might execute the repository file. BrainFrog enforces:
- When `python` or `python3` is specified, `sys.executable` (the active, trusted interpreter) is used directly.
- `_build_safe_system_path(cwd_path)` strips the repository root from `PATH` so local scripts cannot shadow system utilities.

### 3.3 Host Credential Sanitization
Child processes receive an explicitly scrubbed environment:
- All environment variables matching `*_API_KEY`, `*_SECRET*`, `*_TOKEN*`, `AWS_*`, `GITHUB_*`, `ANTHROPIC_*`, `TYPESAFE_*`, `OPENAI_*` are stripped.
- Only essential system variables (`PATH`, `SYSTEMROOT`, `TEMP`, `TMP`, `COMSPEC`, `PATHEXT`, sanitized `PYTHONPATH`) are passed.

---

## 4. Execution Trust Modes

| Trust Mode | CLI Invocation | Enforcement Behavior | Report Metadata |
|---|---|---|---|
| **`UNTRUSTED_FAIL_CLOSED`** *(Default)* | `bf verify --spec spec.md` | Fails closed on executable criteria (`BLOCKED`). Safe for evaluating untrusted third-party code without a container sandbox. | `execution_trust_mode: "UNTRUSTED_FAIL_CLOSED"`, `execution_backend: "none"` |
| **`TRUSTED_OPERATOR`** | `bf verify --spec spec.md --trust-local-code` | Executes verified commands with local user privileges under hardened `run_hardened_subprocess`. Prints prominent security warning banner. | `execution_trust_mode: "TRUSTED_OPERATOR"`, `execution_backend: "local_hardened_subprocess"`, `authorization_reference: "operator_explicit_flag"` |
| **`ISOLATED_SANDBOX`** | Configured container engine | Runs in isolated container with read-only root, dropped privileges, and no host credentials. | `execution_trust_mode: "ISOLATED_SANDBOX"`, `execution_backend: "container_runtime"` |

---

## 5. Summary of Enforceable Security Invariants

1. **A command in `spec.md` never constitutes its own execution authorization.**
2. **Evaluating an untrusted spec without `--trust-local-code` or container isolation never launches local host processes.**
3. **`shell=False` is documented and treated solely as argument protection, not as an OS sandbox.**
4. **All child process executions are subject to bounded timeouts and automated process-tree cleanup.**
5. **No host credential or cloud token is passed to verification subprocesses.**
