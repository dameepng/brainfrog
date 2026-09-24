---
name: test-code-behavior
description: Implement a meaningful new behavior or fix a consequential regression using a focused failing test and a minimal passing change. Use for parser, tool contract, state transition, or other testable behavior changes when tests materially reduce risk; skip for cosmetic or trivial reversible edits.
---

# Test Code Behavior

Work in small behavioral slices. This is an original, selective adaptation of obra/superpowers' TDD workflow; see [references/provenance.md](references/provenance.md).

1. Read the existing code and test conventions. Define one externally observable behavior and a realistic input/output case.
2. Write or update a focused test. Run it and confirm it fails for the intended missing behavior, not a syntax error or broken fixture. If the behavior already works, revise the test or scope.
3. Make the smallest production change that satisfies the case. Run the targeted test until it passes. Refactor only if clarity improves, and re-run.
4. Run related tests and required build checks. Report evidence and known gaps; do not infer full suite success from one passing test.

Use project test runners as the deterministic scripts. Do not add a generic runner merely for this skill. Prefer tests of public effects to internal call counts or mock-only choreography. The user and project's existing testing policy govern scope; no mandatory test is imposed for a cosmetic edit.

For examples of good boundaries in a coding CLI, see [references/examples.md](references/examples.md).
