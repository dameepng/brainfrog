---
name: review-code-change
description: Review a code change, diff, patch, or pull request for actionable correctness, security, architectural justification, compatibility, and data-integrity defects. Use for inspection and findings rather than implementation; distinguish review-only requests from separately authorized fixes and avoid unrelated style audits.
---

# Review Code Change

Find defects supported by the supplied change and explain their consequences. Audit architectural proportionality, detect invented requirements, and protect backwards compatibility and persistent state integrity. Prioritize by impact; do not manufacture findings.

## Respect the review boundary

Follow the current method's response format and the user's requested mode. When JSON is required, put findings and limitations only in existing permitted fields; do not invent a review schema or add outside prose. Review-only and PLAN requests do not authorize file changes. Keep separately requested fixes distinct from the review and within an authorized implementation stage.

During stateless System 2 calls, use only supplied diffs, code, tests, and execution evidence. Do not invoke native tools, inspect additional files, or assume prior-call memory. The runtime owns retrieval, writes, testing, authorization, and git. Linked references are not automatically read, and the skill supplies no new permissions.

## Review the actual change

1. **Establish scope.** Identify the supplied diff, base, changed files, and relevant surrounding context. If only a file or excerpt is available, say so; do not claim to have reviewed a complete branch or pull request. Preserve unrelated user work and generate no replacement files for an inspection-only task.
2. **Audit architectural justification and proportionality.** Check whether structural refactoring or component redesigns are justified by an observable defect (e.g. split-brain state, circular import cycle, co-change coupling). Flag speculative redesigns of working, tested code that add unnecessary abstraction layers (e.g. premature DTOs, factories, or database abstractions) without demonstrated necessity.
3. **Detect invented domain logic and ungrounded requirements.** Inspect whether the change invents business rules, entity categories or tiers, unsupplied mathematical formulas, or external service mocks not specified in the requirements. Flag unverified assumptions that were silently converted into production code constraints.
4. **Trace affected behavior and compatibility boundaries.** Follow changed inputs, branches, state transitions, error paths, and supplied callers. Check compatibility assumptions against actual caller interfaces and tests:
   - In brownfield targets: flag broken positional arguments, missing default values, or modified return types that break downstream callers.
   - On external integration boundaries: check whether validation matches contract requirements—flag overly brittle schema layers that drop valid incoming payloads on evolving feeds, as well as missing validation where security contracts demand strict checking.
   - In stateful changes: check for unmigrated persistent data, missing backward-compatibility for legacy disk files, lack of safe write protection against interruption, or lack of rollback safety.
5. **Prioritize actionable defects.** Look for incorrect results, authorization after side effects, unsafe input handling, data loss, resource leaks, broken cancellation, and compatibility regressions. Include real moderate-impact defects; severity determines order, not whether evidence counts. Ignore personal style preferences, indentation, and unrelated legacy issues.
6. **Make findings reproducible.** Within permitted fields, give the actual supplied location or symbol, triggering condition, consequence, and smallest useful corrective direction. Use line numbers only when available. Distinguish a confirmed defect from a question whose answer depends on missing evidence. Name the specific missing caller or contract and its relevance.
7. **Evaluate coverage honestly.** Compare supplied tests with the changed behavior and failure conditions. A test's source shows intent; runtime output shows whether it executed. When a bounded rewrite was performed, verify that characterization tests proved behavioral equivalence. Missing tests alone do not establish a bug, and a passing test does not prove all behavior correct. Identify a focused check that would resolve uncertainty without claiming to have run it.
8. **Finish proportionately.** Return prioritized findings and consequential scope limits. If none are supported, report no actionable findings in the inspected scope. Do not claim universal correctness or require unnecessary rewrites. This skill must remain useful without a later automatic verification-skill handoff.

## Hypothetical example

Suppose a supplied diff moves `save_record(request)` before `authorize(request)`, and the supplied save implementation writes to disk. The unauthorized-request path can now mutate state before rejection. Cite that supplied symbol and ordering as the finding, and propose checking authorization first. If the diff also introduces an unrequested tiered pricing structure or speculative classification schema into a function whose requirements only specified a bug fix, flag that invented domain logic as an ungrounded requirement. If persistent storage format is modified without backward-compatible reading of legacy files, report the state migration risk as a high-severity finding.

Optional supplied reference: [scenario checks](references/examples.md).
