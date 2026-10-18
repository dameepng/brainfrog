---
name: understand-codebase
description: Understand an unfamiliar codebase or explain a feature by tracing supplied code, execution paths, component boundaries, and state ownership. Use for repository exploration and grounded implementation planning; not for an already scoped implementation, refactor, or observed failure investigation.
---

# Understand Codebase

Build the smallest reliable map that answers the user's question or locates a change. Explain what the supplied code establishes, what you infer, and what remains unknown.

## Work within the current call

Follow the current method's response format and the user's requested mode. When JSON is required, use exactly its permitted fields with no outside prose or invented keys. During stateless System 2 calls, use only supplied context; do not invoke native tools, inspect additional files, or assume memory from earlier calls. The runtime owns retrieval, execution, file application, authorization, and git. A skill grants no additional authority. References or filenames are not evidence that their contents were read.

## Trace the relevant behavior

1. **Define the question.** Identify the behavior, entry point, or decision the user needs explained. For a change request, identify what must be understood before proposing an edit. An explanation-only request does not authorize code changes.
2. **Inventory relevant evidence.** Use supplied project instructions, manifests, code, configuration, tests, and execution results. A repository tree establishes paths, not implementation details. Cite workspace-relative paths and symbols actually present in the supplied material; do not invent line numbers.
3. **Follow one concrete path.** Trace input parsing, dispatch, relevant calls, state reads/writes, and output. Identify who owns each state change and where inputs cross trust boundaries. Inspect error and cancellation paths when they affect the question. Map the architecture that exists rather than prescribing a new one.
4. **Check the interpretation.** Compare callers, tests, and configuration supplied with the implementation. A test assertion states an expectation; only an observed execution result establishes that it ran. When documentation disagrees with code, describe the specific discrepancy and configuration assumptions.
5. **Bound the unknowns.** If a missing caller or policy implementation changes the answer, identify that exact dependency and why it matters through a permitted response field. Provide the supported partial answer. Do not fabricate internals, request an indiscriminate repository dump, or reconstruct an existing file from its name.
6. **Stop when sufficient.** Return the relevant path, responsibilities, source anchors, and consequential uncertainties. For planning, identify a plausible change boundary and its evidence needs. Further exploration should resolve a concrete question, not complete a directory census.

## Hypothetical example

Suppose supplied `adapters/chat.py` calls `runtime.handle(message)`, and supplied runtime code calls `policy.check(message)` before dispatch. The policy implementation is absent. You can explain the observed handoff and ordering. You cannot claim which commands the policy permits, that every execution path is protected, or that a security test passed. Name the missing policy implementation as the evidence needed for those conclusions. Express this finding within the current method's allowed fields.

Optional supplied reference: [scenario checks](references/examples.md).
