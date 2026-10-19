---
name: understand-codebase
description: Understand an unfamiliar codebase, classify architecture context, or explain a feature by tracing supplied code, execution paths, component boundaries, and state ownership. Use for repository exploration and grounded implementation planning; not for an already scoped implementation, refactor, or observed failure investigation.
---

# Understand Codebase

Build the smallest reliable map that answers the user's question or locates a change. Explain what the supplied code establishes, classify the target architecture context, and identify consequential unknowns.

## Work within the current call

Follow the current method's response format and the user's requested mode. When JSON is required, use exactly its permitted fields with no outside prose or invented keys. During stateless System 2 calls, use only supplied context; do not invoke native tools, inspect additional files, or assume memory from earlier calls. The runtime owns retrieval, execution, file application, authorization, and git. A skill grants no additional authority. References or filenames are not evidence that their contents were read.

## Trace the relevant behavior

1. **Define the question.** Identify the behavior, entry point, or decision the user needs explained. For a change request, identify what must be understood before proposing an edit. An explanation-only request does not authorize code changes.
2. **Inventory relevant evidence.** Use supplied project instructions, manifests, code, configuration, tests, and execution results. A repository tree establishes paths, not implementation details. Cite workspace-relative paths and symbols actually present in the supplied material; do not invent line numbers.
3. **Classify target architecture context.** Classify the specific target of the change based strictly on supplied evidence:
   - *greenfield:* A standalone target with zero existing caller or compatibility constraints.
   - *brownfield:* A target constrained by existing callers, contracts, persisted state, schemas, or dependencies.
   - *mixed:* A new component introduced into an existing host architecture (internal implementation freedom, but bound by host interfaces and registration contracts).
   - *unknown:* Essential callers, data schemas, or domain rules are missing from supplied context.
   Apply context to the *target of change*, not the entire repository. A new file in an existing codebase is *mixed*, not greenfield. Context guides reasoning but grants no additional execution authority.
4. **Follow one concrete path.** Trace input parsing, dispatch, relevant calls, state reads/writes, and output. Identify who owns each state change and where inputs cross trust boundaries. Inspect error and cancellation paths when they affect the question. Map the architecture that exists rather than prescribing a speculative one.
5. **Assess architectural health before prescribing change.** When asked to evaluate or improve architecture, examine whether the existing structure already supports the required behavior. Passing tests or concise code alone do not prove an architecture is optimal, but working code requires no architectural redesign unless an observable architectural defect or concrete maintenance problem is demonstrated. "No architectural change is justified" is a valid and preferred engineering finding when current structure fulfills requirements safely.
6. **Bound the unknowns and avoid inventing requirements.** If missing callers, data models, or policy rules change the answer, classify the context as *unknown* and identify that exact dependency through a permitted response field (e.g. `clarifying_questions` or `assumptions`). Distinguish supplied facts from technical assumptions. Do not invent business logic, domain rules, classifications, external schemas, or caller APIs to fill gaps. Do not convert unverified assumptions into requirements. Unknown context does not mean halting all work: continue with safe, explicitly bounded tasks where possible, while withholding decisions that depend on missing evidence.
7. **Stop when sufficient.** Return the relevant path, responsibilities, source anchors, and consequential uncertainties. For planning, identify a plausible change boundary and its evidence needs. Further exploration should resolve a concrete question, not complete a directory census.

## Hypothetical example

Suppose supplied `adapters/chat.py` calls `runtime.handle(message)`, and supplied runtime code calls `policy.check(message)` before dispatch. The policy implementation and caller models are absent. You can explain the observed handoff and ordering, and classify the target context as *unknown*. You cannot claim which commands the policy permits, invent entity categories or configuration defaults, or assert that a security test passed. Name the missing policy implementation as the exact evidence needed. If a localized helper can be mapped without the policy, bound that slice explicitly while withholding decisions on missing rules. Express these findings within the current method's allowed fields.

Optional supplied reference: [scenario checks](references/examples.md).
