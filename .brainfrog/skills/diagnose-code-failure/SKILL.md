---
name: diagnose-code-failure
description: Investigate reproducible bugs, failing tests or builds, unexpected CLI/tool behavior, and performance regressions in a codebase. Use when asked to debug, find root cause, or fix an observed failure; do not invoke for routine feature work with no failure.
---

# Diagnose Code Failure

Find evidence for the cause before changing code. This skill is an original, compact adaptation of the investigation principles in obra/superpowers; see [references/provenance.md](references/provenance.md).

1. Capture the exact symptom: command or steps, expected and actual results, environment, and relevant error text. Reproduce when feasible. If reproduction is intermittent, collect observations and state uncertainty.
2. Inspect recent changes and trace the failing value or event backward through the smallest relevant path. Compare a nearby working path. Avoid logging secrets while instrumenting boundaries.
3. State one falsifiable hypothesis and a minimal check that distinguishes it from alternatives. Use deterministic commands or project tests where possible; change one variable at a time.
4. Fix the proven cause with a focused edit. For a consequential regression, add a test that fails on the original behavior and passes with the fix; avoid tests that simply mirror implementation.
5. Re-run the reproduction, relevant tests, and build checks. Report observed output and any unverified scenario. If the hypothesis fails, return to step 2 rather than stacking speculative fixes.

For complex data-flow issues, read [references/diagnosis.md](references/diagnosis.md). Obey project permissions and existing instructions; this workflow adds no automatic shell or network authorization.
