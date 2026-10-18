---
name: review-code-change
description: Review a code change, diff, patch, or pull request for actionable correctness, security, compatibility, and data-integrity defects. Use for inspection and findings rather than implementation; distinguish review-only requests from separately authorized fixes and avoid unrelated style audits.
---

# Review Code Change

Find defects supported by the supplied change and explain their consequences. Prioritize by impact; do not manufacture findings.

## Respect the review boundary

Follow the current method's response format and the user's requested mode. When JSON is required, put findings and limitations only in existing permitted fields; do not invent a review schema or add outside prose. Review-only and PLAN requests do not authorize file changes. Keep separately requested fixes distinct from the review and within an authorized implementation stage.

During stateless System 2 calls, use only supplied diffs, code, tests, and execution evidence. Do not invoke native tools, inspect additional files, or assume prior-call memory. The runtime owns retrieval, writes, testing, authorization, and git. Linked references are not automatically read, and the skill supplies no new permissions.

## Review the actual change

1. **Establish scope.** Identify the supplied diff, base, changed files, and relevant surrounding context. If only a file or excerpt is available, say so; do not claim to have reviewed a complete branch or pull request. Preserve unrelated user work and generate no replacement files for an inspection-only task.
2. **Trace affected behavior.** Follow changed inputs, branches, state transitions, error paths, and supplied callers. Check compatibility assumptions against actual interfaces and tests. Avoid inventing a caller, configuration, concurrent execution path, or external contract to justify a finding.
3. **Prioritize actionable defects.** Look for incorrect results, authorization after side effects, unsafe input handling, data loss, resource leaks, broken cancellation, and compatibility regressions where the change makes them relevant. Include real moderate-impact defects; severity determines order, not whether evidence counts. Ignore personal style preferences and unrelated legacy issues.
4. **Make findings reproducible.** Within permitted fields, give the actual supplied location or symbol, triggering condition, consequence, and smallest useful corrective direction. Use line numbers only when available. Distinguish a confirmed defect from a question whose answer depends on missing evidence. Name the specific missing caller or contract and its relevance.
5. **Evaluate coverage honestly.** Compare supplied tests with the changed behavior and failure conditions. A test's source shows intent; runtime output shows whether it executed. Missing tests alone do not establish a bug, and a passing test does not prove all behavior correct. Identify a focused check that would resolve uncertainty without claiming to have run it.
6. **Finish proportionately.** Return prioritized findings and consequential scope limits. If none are supported, report no actionable findings in the inspected scope. Do not claim universal correctness or require unnecessary rewrites. This skill must remain useful without a later automatic verification-skill handoff.

## Hypothetical example

Suppose a supplied before/after fixture moves `save_record(request)` before `authorize(request)`, and the supplied save implementation writes to disk. The unauthorized-request path can now mutate state before rejection. Cite that supplied symbol and ordering as the finding, and propose checking authorization first. Without the save implementation, distinguish the suspected side effect from an observed one; do not invent a real repository path or line number.

Optional supplied reference: [scenario checks](references/examples.md).
