---
name: implement-code-change
description: Deliver a requested software feature or behavior change through scoped design, small implementation slices, meaningful tests, and evidence-based reporting. Use for adding or extending functionality; respect explicit plan-only mode and prefer dedicated skills for pure debugging, review, or behavior-preserving refactoring.
---

# Implement Code Change

Turn the requested outcome into a focused, reviewable change. Work toward completion across runtime stages while keeping each call within its current planned slice.

## Respect method, mode, and authority

Follow the current method's exact response format. When JSON is required, return only allowed fields, without outside prose. A `plan_task` call can prepare authorized BUILD work; it does not mean the user requested plan-only work. Explicit PLAN/read-only intent must remain non-mutating.

During stateless System 2 calls, use supplied context and do not invoke native tools or assume earlier-call memory. The runtime owns retrieval, file application, tests, permissions, and git. This skill neither authorizes nor proves those actions. Reference links are not automatically loaded.

## Deliver one coherent slice

1. **Define observable success.** Derive concrete inputs, outputs, and relevant side effects from the request. Separate requirements from assumptions. Ask only about a consequential choice that supplied context cannot resolve; decide routine reversible details using existing conventions.
2. **Choose the change boundary.** Inspect supplied implementation, callers, configuration, and tests. Reuse existing ownership and extension points. Preserve public behavior unless the requested change intentionally revises it; identify compatibility or migration implications when applicable. Avoid speculative abstractions and dependencies.
3. **Plan proportionately.** For `plan_task`, honor its bounded code-modification step schema. Include specific workspace-relative files and small independently testable slices. Do not add forbidden manual retrieval, test-execution, browser, or review steps. Relevant test-file changes are implementation work. Small corrections need no design ceremony.
4. **Generate complete changes.** In `write_code`, return complete contents only for changed files in the current slice, with workspace-relative paths. Preserve unrelated supplied code and user edits. Never return patches, placeholders, or truncated files. If an existing file's current contents are missing, do not fabricate a replacement; identify the limitation through a permitted field. Treat an explicitly supplied new-file marker according to the method contract.
5. **Cover consequential behavior.** Add or update tests when they materially reduce risk, using existing conventions and observable effects. Include relevant failure, boundary, cancellation, and state-transition cases. Cosmetic changes do not require artificial tests. Never weaken assertions or hide failures to obtain a pass.
6. **Use fresh evidence.** Evaluate runtime-supplied results against acceptance criteria and the actual changed slice. If a check fails, reason from its output before revising code. Distinguish content proposed, applied, executed, and verified. A targeted pass does not establish full-suite success; missing verification remains unverified. Do not depend on another skill being selected later.

## Hypothetical example

For configurable provider retries, supplied provider code and its documented response contract determine retryable failures. Keep attempts bounded, preserve cancellation, and avoid repeating side effects whose completion is uncertain. Extend the observed provider boundary, not an invented module. If configuration contents are absent, disclose that gap instead of replacing unknown settings. Report a retry test as passed only when runtime evidence supports it.

Optional supplied reference: [scenario checks](references/examples.md).
