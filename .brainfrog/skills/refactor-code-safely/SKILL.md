---
name: refactor-code-safely
description: Restructure software while preserving observable behavior, contracts, and unrelated work. Use for extracting helpers, removing duplication, simplifying structure, or reorganizing code without an intended behavior change; keep separately requested features and bug fixes in distinct slices.
---

# Refactor Code Safely

Improve the structure needed by the task while preserving its behavioral contract. A cleaner implementation is useful only if callers retain the behavior they rely on.

## Stay inside the execution contract

Follow the current method's response format and requested mode. When JSON is required, use only permitted fields with no outside prose. PLAN/read-only requests do not authorize changed files. A planning method may also be one stage of authorized BUILD work.

In stateless System 2 calls, reason from supplied code, callers, tests, and execution evidence. Do not invoke native tools, assume prior-call memory, or treat linked references as already read. The runtime owns retrieval, file application, verification, permissions, and git; skill instructions grant no additional authority.

## Preserve behavior deliberately

1. **Identify the purpose and invariants.** State the concrete structural problem and the relevant behavior to preserve: public inputs/outputs, exceptions, ordering, cancellation, state ownership, persistence, or compatibility. Do not turn every local extraction into an architecture redesign.
2. **Inspect the supplied boundary.** Trace affected callers and existing tests. Missing caller context is a compatibility uncertainty, not evidence that a symbol is unused. Identify the specific missing evidence through a permitted field. Never reconstruct an existing file whose current contents were not supplied.
3. **Choose the smallest transformation.** Extract, rename, move, or simplify in coherent increments. Separate mechanical movement from semantic changes where practical. Keep separately requested behavior changes in their own slices; do not bundle opportunistic bug fixes, dependencies, formatting sweeps, or cleanup.
4. **Preserve compatibility intentionally.** Retain public contracts required by the task and supplied evidence. A forwarding alias may be useful, but do not invent a deprecation policy or add aliases automatically. Preserve meaningful comments and invariants. Treat the supplied working state as the baseline, including unrelated user changes; never prescribe blanket reset or restore.
5. **Return complete scoped content.** In `write_code`, emit the full new contents of changed files with workspace-relative paths. Preserve everything outside the intended transformation. No abbreviated files, diff patches, or imagined updates to callers that were not supplied. Honor the planning method's limits when proposing slices.
6. **Establish what was verified.** Use existing behavioral and contract tests; add focused characterization coverage when a consequential invariant is otherwise unprotected. Syntax success alone is not behavioral equivalence. Evaluate fresh runtime-supplied results after the change, including relevant failure paths. Proposed checks are not executed checks, and returned code is not proof of application. Report remaining uncertainty without weakening assertions or assuming a later verification skill will run.

## Hypothetical example

Suppose a supplied process helper distinguishes timeout, cancellation, and ordinary nonzero exit, while its file also contains an unrelated new user option. Extracting the helper must preserve these outcomes, cleanup ordering, and that option. If callers are absent, avoid claiming every caller is updated. If only compilation was observed, say the refactor compiles; behavioral preservation still requires the relevant runtime checks.

Optional supplied reference: [scenario checks](references/examples.md).
