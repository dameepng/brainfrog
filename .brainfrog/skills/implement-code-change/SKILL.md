---
name: implement-code-change
description: Deliver a requested software feature or behavior change through proportional architectural strategy, scoped design, small implementation slices, meaningful tests, and evidence-based reporting. Use for adding or extending functionality; respect explicit plan-only mode and prefer dedicated skills for pure debugging, review, or behavior-preserving refactoring.
---

# Implement Code Change

Turn the requested outcome into a focused, reviewable change. Choose a proportional architectural strategy, respect host contracts, and work toward completion across runtime stages while keeping each call within its current planned slice.

## Respect method, mode, and authority

Follow the current method's exact response format. When JSON is required, return only allowed fields, without outside prose. A `plan_task` call can prepare authorized BUILD work; it does not mean the user requested plan-only work. Explicit PLAN/read-only intent must remain non-mutating.

During stateless System 2 calls, use supplied context and do not invoke native tools or assume earlier-call memory. The runtime owns retrieval, file application, tests, permissions, and git. This skill neither authorizes nor proves those actions. Reference links are not automatically loaded.

## Deliver one coherent slice

1. **Define observable success and bounds.** Derive concrete inputs, outputs, and relevant side effects from the request. Distinguish verified requirements from technical assumptions. If target context is *unknown* or essential domain rules are missing, do not invent domain logic, entity classifications, business policies, or APIs to fill the gap. Formulate focused clarification questions through permitted fields (e.g. `clarifying_questions`). Where safe partial progress is possible, bound the implementation strictly to verifiable slices while withholding decisions on missing rules.
2. **Select a proportional architectural strategy.** Match the strategy to the target context and problem scope:
   - *keep:* Add or extend within the existing implementation structure without altering component boundaries. (Preferred when the existing architecture accommodates the change).
   - *local_refactor:* Restructure internal helpers locally while preserving component boundaries and caller contracts.
   - *incremental_migration:* Coexist old and new implementations across a transition (essential when persisted state, disk formats, or distributed callers must not break).
   - *bounded_rewrite:* Replace an isolated leaf component's internal implementation when the existing logic is fundamentally fragile or broken, while strictly preserving its required caller interface.
   Prefer the smallest strategy that achieves the goal. For small or obvious tasks, use direct 1–3 step execution without architectural ceremony.
3. **Respect host contracts in mixed contexts.** When introducing a new component into an existing host system (*mixed* context), conform to the host's abstract interfaces, registration mechanisms, configuration conventions, and error taxonomy. Do not invent parallel orchestration, ad-hoc registries, or bypass host boundaries.
4. **Choose the change boundary and protect integration compatibility.** Inspect supplied implementation, callers, configuration, and tests. Preserve public contracts unless the requested change intentionally revises them. On integration boundaries, derive input validation from actual observed contracts and operational requirements: for loose external ingestion, avoid brittle validation that drops valid payloads with unmodeled metadata; for security-sensitive boundaries, enforce strict validation if required by contract. Preserve accepted payload shapes, exception semantics, and side effects. When writing persisted state, preserve existing file-write contracts and use project-supported safe write mechanisms without assuming native filesystem authority away from the runtime. Avoid speculative dependencies and layering.
5. **Plan proportionately.** For `plan_task`, honor its bounded code-modification step schema. Include specific workspace-relative files and small independently testable slices. Do not add forbidden manual retrieval, test-execution, browser, or review steps. Relevant test-file changes are implementation work.
6. **Generate complete changes.** In `write_code`, return complete contents only for changed files in the current slice, with workspace-relative paths. Preserve unrelated supplied code and user edits. Never return patches, placeholders, or truncated files. If an existing file's current contents are missing, do not fabricate a replacement; identify the limitation through a permitted field. Treat an explicitly supplied new-file marker according to the method contract.
7. **Cover consequential behavior.** Add or update tests when they materially reduce risk, using existing conventions and observable effects. Include relevant failure, boundary, cancellation, and state-transition cases. Cosmetic changes do not require artificial tests. Never weaken assertions or hide failures to obtain a pass.
8. **Use fresh evidence.** Evaluate runtime-supplied results against acceptance criteria and the actual changed slice. If a check fails, reason from its output before revising code. Distinguish content proposed, applied, executed, and verified. A targeted pass does not establish full-suite success; missing verification remains unverified. Do not depend on another skill being selected later.

## Hypothetical example

For configurable provider retries, supplied provider code and its documented response contract determine retryable failures. Keep attempts bounded, preserve cancellation, and avoid repeating side effects whose completion is uncertain. In a *mixed* target context, implement the provider by adhering strictly to the host's abstract client interface and factory resolution, not an invented parallel loop. If configuration keys or retry policies are unsupplied, name that gap in permitted fields rather than inventing speculative retry tables. Report a retry test as passed only when runtime evidence supports it.

Optional supplied reference: [scenario checks](references/examples.md).
