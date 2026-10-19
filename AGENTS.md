# BrainFrog Agent Guidelines & Instruction Index

This document establishes the binding operational instructions and engineering rules for all AI coding agents working in the BrainFrog repository.

## 1. System Guidelines & Architectural Memory

- Primary architecture, system prompt, and anti-slop guidelines: [BRAINFROG.md](BRAINFROG.md)
- Dual-system engine design: [DESIGN.md](DESIGN.md)
- User and developer guide: [README.md](README.md)

## 2. Binding Rule: Safe Test Fixtures & Git Hygiene

All agents must strictly adhere to the [BrainFrog Safe Test Fixtures & Git Hygiene Rule](.agents/rules/safe_test_fixtures_and_git_hygiene.md):

1. **Core Principle**: Never introduce credential-like literals into committed source code or Git history unless explicitly approved, safe, and necessary.
2. **Synthetic Fixtures**: Use deterministic, obviously fake synthetic generators for API keys (`_synthetic_openai_key()`, `_synthetic_google_key()`), tokens, passwords, and PEM private keys. Never hardcode realistic secret literals or rely solely on `# gitguardian:ignore` annotations. Preserve 100% of security test assertions.
3. **Mandatory Pre-Commit Checks**:
   - Inspect staged & unstaged diffs (`git diff --cached`, `git diff`).
   - Run local secret scanner: `python -m security.git_guard --staged`.
   - Run relevant tests and `git diff --check`.
   - Fail closed if any finding is unresolved.
4. **Mandatory Pre-Push Checks**:
   - Inspect the entire outgoing commit range (`git log origin/<base>..HEAD -p`).
   - Scan outgoing commits for accidental secrets (`python -m security.git_guard --range origin/<base>..HEAD`).
   - Confirm required CI and security checks pass before declaring ready.
5. **Agent Authorization Limits**:
   - **NEVER** run `git commit`, `git push`, force-push, history rewrite (`rebase`), secret incident dismissal, or PR merge without explicit user authorization.
   - Never automatically dismiss secret alerts as false positives without investigation.
   - Preserve unrelated working-tree changes.
6. **Definition of Done**: A change is ready for commit/push only after tests pass, secret scans report clean, no credential exposure remains, and synthetic fixtures are verified.
