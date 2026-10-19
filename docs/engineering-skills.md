# Engineering skills

BrainFrog's modular skills provide task-specific reasoning procedures. The engineering workflows emphasize source-grounded decisions, small changes, preserved contracts, useful tests, and accurate completion claims. They supplement the runtime; they do not grant tool access or enforce a security boundary themselves.

## Choose the relevant workflow

| Skill | Primary use |
| --- | --- |
| `understand-codebase` | Explain or explore supplied code, trace execution, identify ownership, and ground a proposed change. |
| `implement-code-change` | Carry a feature or behavior extension through scoped design, implementation slices, tests, and evidence-based reporting. |
| `diagnose-code-failure` | Investigate an observed failure, test a causal hypothesis, and fix the supported cause. |
| `test-code-behavior` | Develop consequential behavior with focused failing and passing tests. |
| `refactor-code-safely` | Improve structure while preserving behavior, public contracts, and unrelated work. |
| `review-code-change` | Inspect changes for actionable defects and report supported findings. |
| `verify-code-result` | Check that completion claims match fresh execution and observable evidence. |
| `audit-anti-slop` | Audit generated interfaces and copy for unsupported claims, ineffective interactions, and weak design choices. |

The four new workflows are original guidance written for BrainFrog's execution model. Existing debugging, testing, verification, and UI skills retain their own provenance notes.

## Automatic and explicit selection

With no explicit skill selected, the skills module uses deterministic task-intent rules for common English and Indonesian software requests. Examples include:

| Request | Intended workflow |
| --- | --- |
| "Explain how a request reaches the runtime." | `understand-codebase` |
| "Tambahkan export JSON untuk hasil pekerjaan." | `implement-code-change` |
| "Fix the failing parser test." | `diagnose-code-failure` |
| "Add parser tests for malformed input." | `test-code-behavior` |
| "Refactor the session manager without changing behavior." | `refactor-code-safely` |
| "Tinjau perubahan kode ini untuk bug." | `review-code-change` |
| "Verify the completed code change against requirements." | `verify-code-result` |

Automatic selection is a bounded heuristic, not a language-understanding guarantee. Unrelated requests should not activate an engineering workflow. Use explicit selection when a request is ambiguous or the heuristic does not select the intended skill.

In the interactive CLI, select the skill first, then submit the task as a **separate message**:

```text
/skills
/skill implement-code-change
Add JSON export for the task summary, preserving the existing text output.
```

The same two-step interaction works with an Indonesian task:

```text
/skill review-code-change
Tinjau perubahan parser ini untuk bug. Jangan ubah file.
```

The interactive command takes a skill name, not a combined name-and-task string. `/skills` lists the available names. `/skill off` returns to automatic selection. A CLI `--skill <name>` argument is also supported, for example with a non-interactive `--task` invocation.

Selecting a skill does not switch execution mode. Explicit PLAN/read-only intent remains non-mutating. A `plan_task` provider call may prepare authorized BUILD work; it does not make the overall request plan-only.

## Runtime and loading boundaries

The orchestrator selects one active skill during initialization for the supplied task and injects that skill's main body into System 2 guidelines. An explicitly selected interactive skill stays selected until changed or cleared. This is not an automatic chain that switches through all eight skills during a task. Each new workflow therefore includes its own essential context, authority, and evidence rules.

Skills are not tools. During stateless System 2 calls, the runtime supplies context and owns retrieval, file application, test execution, permissions, and git operations. The model must follow the current provider method's response format. Where JSON is required, a skill cannot introduce extra fields or prose; generated files must satisfy the method's full-content contract.

The execution-contract path deliberately skips local project guidelines and modular skill loading, including contracted child execution through that path. This pack does not change that boundary or expand remote authority.

Repository skills live in `.brainfrog/skills/<name>/SKILL.md`. Main bodies are self-contained. `references/` files may be indexed, but their contents are **not automatically loaded** with the active skill. They require explicit loading or runtime-supplied context; a link is not evidence of a read.

The source-checkout and editable-install layout exposes these bundled directories to the existing loader. Additional project-local skill directories can be discovered by that loader. The current wheel configuration does not explicitly package this `.brainfrog/skills` tree; this change does not claim new wheel distribution support. Verify discovery in the installation actually being used with `/skills`.

## Qualitative acceptance scenarios

The following references contain three hypothetical manual scenarios per workflow. They are not a report of measured model performance. Routing tests check selection and loading, not whether a model reliably follows the instructions. For a model evaluation, supply the stated context and current method schema, inspect the response, and record the actual result.

| Reference | Scenarios |
| --- | --- |
| [Understanding examples](../.brainfrog/skills/understand-codebase/references/examples.md) | Missing policy contents, conflicting documentation, filename-only evidence. |
| [Implementation examples](../.brainfrog/skills/implement-code-change/references/examples.md) | Plan-only intent versus BUILD planning, unknown existing configuration, partial verification. |
| [Refactoring examples](../.brainfrog/skills/refactor-code-safely/references/examples.md) | Failure/cancellation preservation, unrelated user changes, missing public callers. |
| [Review examples](../.brainfrog/skills/review-code-change/references/examples.md) | Authorization ordering, incomplete review scope, no supported defects. |

Reject responses that invoke unauthorized tools, overwrite unknown content, invent test execution, modify review-only work, or violate the response schema. A cosmetic correction should not require a design document or artificial test suite. Essential safeguards remain in the main bodies even when these references are not loaded.
