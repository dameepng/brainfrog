# BrainFrog External Testing — GitHub Bug Report Draft

> **Document Status**: Reviewable draft template for external testers and maintainers.  
> **Notice**: This is a standalone draft specification located in `docs/external-testing/`, **not** an active installation into `.github/ISSUE_TEMPLATE/`. Maintainers can copy or adapt this format when configuring repository issue forms.

---

## Suggested Issue Title Format

```
[Bug][v0.1.0-rc1]: <Short, descriptive summary of the problem>
```
*Example*: `[Bug][v0.1.0-rc1]: PowerShell execution policy error during virtual environment activation`  
*Example*: `[Bug][v0.1.0-rc1]: verify-proof returns exit code 1 on clean checkout without --check-workspace`

---

## Bug Report Body

### 1. Environment Details

- **Operating System & Version**: <!-- e.g., Ubuntu 22.04 LTS / macOS 14.4 Sonoma / Windows 11 23H2 -->
- **Shell & Version**: <!-- e.g., bash 5.2 / zsh 5.9 / PowerShell 7.4 / Windows Command Prompt cmd.exe -->
- **Python Version**: <!-- Run `python --version`, e.g., Python 3.10.13 / Python 3.11.8 / Python 3.12.2 -->
- **Terminal / Console Emulator**: <!-- e.g., Windows Terminal / iTerm2 / Alacritty / VS Code integrated terminal -->
- **Release Under Test**: `v0.1.0-rc1` (commit `f06a2768e7f84d36da0cbd038affc6c0dacfb715`)

---

### 2. Blocker Scope

Which part of the onboarding or verification lifecycle does this issue affect?

- [ ] **Setup / Installation blocker** (`git clone`, virtual environment, `pip install -r requirements.txt`, `pip install -e .`, `brainfrog --help`)
- [ ] **Demo execution blocker** (`python demo/reproducible_proof_demo.py`)
- [ ] **Proof inspection blocker** (`brainfrog proofs`, `brainfrog show-proof <target>`)
- [ ] **Proof verification blocker** (`brainfrog verify-proof <target>`, `--check-workspace`)
- [ ] **Ergonomic / Cosmetic issue** (terminal rendering glitch, unclear documentation phrasing, misleading console warning)

---

### 3. Exact Commands & Exit Codes

**Command(s) executed**:
```bash
# Paste the exact command(s) you ran
```

**Observed exit code**: <!-- On bash/zsh: echo $? | On PowerShell: $LASTEXITCODE | On cmd: echo %ERRORLEVEL% -->

**Expected exit code**: <!-- e.g., 0 (VERIFIED), 1 (INVALID), 2 (MISSING), 3 (FAILED) -->

---

### 4. Expected vs. Actual Behavior

**Expected Behavior**:
<!-- A concise explanation of what you expected to happen -->

**Actual Behavior**:
<!-- What actually happened instead (e.g., unexpected exit code, unhandled Python traceback, missing output) -->

---

### 5. Minimal Reproduction Steps

1. Step 1:
2. Step 2:
3. Step 3:

---

### 6. Sanitized Terminal Output / Traceback

> [!IMPORTANT]
> **Sanitization & Privacy Reminder**:  
> Before pasting logs, please inspect and remove any sensitive data:
> - API keys, tokens, or credentials (e.g., `sk-...`, `ghp_...`, bot tokens)
> - Personal identifiable information (usernames, local user home directories like `/Users/<name>` or `C:\Users\<name>`)
> - Proprietary workspace file contents or confidential paths

```text
<!-- Paste relevant redacted output, CLI Rich box rendering, or Python traceback here -->
```

---

### 7. Additional Context

<!-- Add any other context about the problem, virtual environment setup quirks, or system configuration here. -->
