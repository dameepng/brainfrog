---
name: refactor-code-safely
description: Restructure software, distinguish bounded rewrites and migrations, and preserve observable behavior, contracts, and unrelated work. Use for extracting helpers, removing duplication, simplifying structure, or reorganizing code without an intended behavior change; keep separately requested features and bug fixes in distinct slices.
---

# Refactor Code Safely

Improve the structure needed by the task while strictly preserving its behavioral contract. Require observable justification before altering code structure, choose the minimal safe transformation strategy, and protect compatibility and state persistence.

## Stay inside the execution contract

Follow the current method's response format and requested mode. When JSON is required, use only permitted fields with no outside prose. PLAN/read-only requests do not authorize changed files. A planning method may also be one stage of authorized BUILD work.

In stateless System 2 calls, reason from supplied code, callers, tests, and execution evidence. Do not invoke native tools, assume prior-call memory, or treat linked references as already read. The runtime owns retrieval, file application, verification, permissions, and git; skill instructions grant no additional authority.

## Preserve behavior deliberately

1. **Verify justification: "No architectural change is justified" is a valid finding.** Do not refactor simply because code is long, procedural, slightly verbose, or written in an unfamiliar style. High test coverage does not prove an architecture is optimal, nor does low coverage alone justify an overhaul; require concrete evidence of an architectural problem, change-locality friction (e.g. split-brain state ownership, circular import cycles, shotgun surgery across modules), or a verified task objective. When the existing structure fulfills requirements safely, defend *keep* and limit modifications to minimal local adjustments.
2. **Characterize the transformation strategy accurately and proportionally:**
   - *local_refactor:* Restructure internal implementation details while preserving existing component boundaries and public contracts.
   - *bounded_rewrite:* Legitimate when a small, isolated leaf component is fundamentally fragile, defective, or ill-suited to its task, its callers are known and bounded, and replacing the internal implementation with a standard library or robust equivalent is materially simpler, safer, and cleaner than patching. Proportionality governs the rewrite: distinguish behavior-preserving replacement from deliberate contract changes, and capture characterization coverage before replacement.
   - *incremental_migration:* Required when restructuring persisted disk files, session state, databases, or public contracts with distributed callers. Never perform in-place destructive overwrites; design backward-compatible reading of legacy formats, schema versioning, and project-supported safe write mechanisms with rollback safety.
3. **Inspect the supplied boundary and respect contract requirements.** Trace affected callers and existing tests. Missing caller context is a compatibility uncertainty, not evidence that a symbol is unused. On external integration boundaries, derive validation from observed contracts: avoid adding brittle schema layers that reject valid payloads with harmless metadata on loose feeds, but enforce strict validation where security or contract schemas demand it.
4. **Choose the smallest transformation and avoid speculative layers.** Extract, rename, move, or simplify in coherent increments. Do not introduce premature abstractions, generic plugin layers, or enterprise DTO wrappers without demonstrated necessity. Keep separately requested behavior changes in their own slices; do not bundle opportunistic bug fixes, dependencies, formatting sweeps, or cleanup.
5. **Preserve compatibility intentionally.** Retain public contracts required by the task and supplied evidence. A forwarding alias may be useful, but do not invent an unrequested deprecation policy. Preserve meaningful comments and invariants. Treat the supplied working state as the baseline, including unrelated user changes; never prescribe blanket reset or restore.
6. **Return complete scoped content.** In `write_code`, emit the full new contents of changed files with workspace-relative paths. Preserve everything outside the intended transformation. No abbreviated files, diff patches, or imagined updates to callers that were not supplied. Honor the planning method's limits when proposing slices.
7. **Establish what was verified.** Use existing behavioral and contract tests; add focused characterization coverage when a consequential invariant is otherwise unprotected. Syntax success alone is not behavioral equivalence. Evaluate fresh runtime-supplied results after the change, including relevant failure paths. Proposed checks are not executed checks, and returned code is not proof of application. Report remaining uncertainty without weakening assertions or assuming a later verification skill will run.

## Hypothetical example

Suppose a supplied process helper distinguishes timeout, cancellation, and ordinary nonzero exit, while its file also contains an unrelated new user option. When prompted to "modernize the architecture," first verify whether an observable defect exists. If the code is working and fulfills requirements safely, conclude that architectural redesign is unjustified and confine any edit to a minimal helper extraction. If an internal 40-line bitmask unpacker within the module is demonstrably fragile on multi-flag edge cases, accurately identify replacing its internals with a standard library flag helper as a *bounded_rewrite*, keeping the public function signature identical and verifying characterization tests before replacement. If persistent state is restructured, ensure legacy stored data loads without error (*incremental_migration*).

Optional supplied reference: [scenario checks](references/examples.md).
