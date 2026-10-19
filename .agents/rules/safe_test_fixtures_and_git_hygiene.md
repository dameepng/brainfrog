---
description: BrainFrog Engineering Rule for safe test fixtures, secret detection, and Git hygiene.
trigger: always_on
---

# BrainFrog Engineering Rule: Safe Test Fixtures & Git Hygiene

## 1. Core Principle

Never introduce credential-like literals into committed source code or Git history unless they are explicitly approved, safe, and necessary.

Security tests must remain effective without introducing realistic-looking secrets that trigger secret scanners.

## 2. Test Fixture Rules

- Use clearly synthetic, deterministic fixtures for API keys, access tokens, passwords, private keys, and other sensitive values.
- Prefer obviously fake values such as `TEST_SECRET_PLACEHOLDER`, `synthetic_api_key_for_redaction_test`, or values generated specifically for tests.
- Never copy real credentials from local environments, logs, `.env` files, production systems, API dashboards, or user data into tests.
- Never hardcode realistic-looking API keys, GitHub PATs, bot tokens, passwords, private keys, or high-entropy secret strings merely to test redaction.
- Do not use `# gitguardian:ignore` or equivalent scanner-suppression comments to bypass secret detection without explicit security review.
- If a test must exercise a specific secret-detection regex, generate a synthetic value at runtime and document why its format is required.
- Keep the security behavior under test intact. Never weaken or remove meaningful assertions just to make a scanner pass.

## 3. Mandatory Pre-Commit Checks

Before recommending or performing a commit:

1. Inspect the complete staged diff and relevant unstaged changes (`git diff --cached`, `git diff`).
2. Run the repository's secret scanner (`python -m security.git_guard --staged`) against changed files.
3. Check for credential-like patterns, private-key blocks, `.env` files, and accidental sensitive artifacts.
4. Run relevant tests (`python -m pytest ...`) and `git diff --check`.
5. Confirm that test fixtures are synthetic and no real credentials are present.
6. Investigate every secret-scanner finding; do not automatically classify findings as false positives.

If a finding is unresolved, fail closed: do not recommend committing or pushing the affected change until it is reviewed and resolved.

## 4. Mandatory Pre-Push Checks

Before pushing a branch:

- Review the commits being introduced to the remote, not only the current working tree (`git log origin/<base>..HEAD -p`).
- Check the full outgoing commit range for accidental secrets (`python -m security.git_guard --range origin/<base>..HEAD`).
- Check whether sensitive values exist in earlier commits within the pull request's history.
- Confirm that required CI and security checks have passed, or clearly report any outstanding failures.
- Never assume a clean working tree means the commit history is safe.

If a secret is suspected to be real, stop the push, notify the user, and follow the credential incident process. Revoke or rotate the credential where appropriate. Removing a value from the latest file does not remove it from Git history.

## 5. Agent Authorization

- Never commit, push, force-push, rewrite history, dismiss security incidents, or merge without explicit user authorization.
- Never automatically dismiss secret-scanner alerts as test fixtures or false positives.
- Report findings with file paths, commit references, scanner output, and recommended remediation.
- Preserve unrelated user changes.

## 6. Definition of Done

A change is ready for commit only when:
- Required tests pass.
- The local security scan has been reviewed and reports clean (`python -m security.git_guard --staged`).
- No unresolved credential exposure remains in the proposed changes.
- Any necessary synthetic fixtures are documented and safely constructed.

A change is ready for push only when the outgoing commit history has also been reviewed for credential exposure.

A passing test suite alone does not establish that a change is safe to commit or push.
