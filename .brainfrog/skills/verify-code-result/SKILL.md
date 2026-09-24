---
name: verify-code-result
description: Verify a code change, bug fix, build, or test result before reporting it as completed or passing. Use at the end of substantive development work, before a success claim or requested commit/PR; check actual command results and requirements. Skip when no implementation result is being claimed.
---

# Verify Code Result

Tie every completion claim to fresh evidence. This is an original, concise adaptation of obra/superpowers' verification workflow; see [references/provenance.md](references/provenance.md).

1. List the specific claims to verify: behavior, tests, build, and user requirements. Choose a direct check for each.
2. Run the relevant project commands after the final edit. Read exit codes and output; a targeted test proves only its target. Inspect the diff for unintended changes.
3. For a visual change, inspect the actual rendered UI; for a bug, re-run the original reproduction. If a check is unavailable, say why and limit the claim.
4. Report what passed, what failed, and what remains untested with the exact command or observable evidence.

For a command with noisy output, `python3 scripts/run_check.py --timeout 120 -- COMMAND ARG...` produces a deterministic JSON summary with exit code, duration, and output hashes. It does not print or persist raw output by default. Read the script first and run it under BrainFrog's normal tool permissions. Use ordinary project commands when full output is needed for diagnosis.

No test or build is universally mandatory: select checks that actually support the claims and any existing project gate. Do not rerun checks repeatedly once the outcome is known.
