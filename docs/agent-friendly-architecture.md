# Agent-Friendly Architecture Specification

**Version:** 1.0.0 (Phase 1 Baseline)
**Status:** Canonical Behavioral & Architectural Specification
**Applies to:** BrainFrog System 1 (Jev), System 2 (Claude / Gemini / GPT), Orchestrator, and Modular Engineering Skills

---

## 1. Executive Summary & Purpose

Modern software engineering agents excel at local token prediction and small text transformations, but frequently stumble on architectural reasoning. Common failure modes include:
- Treating an existing codebase as a blank slate and recommending sweeping, speculative rewrites.
- Preserving broken or Byzantine patterns out of reflexive timidity because "it's already written that way."
- Confusing architectural clean-up with cosmetic renaming, file splitting, or premature abstraction.
- Conflating what an agent *proposes* in a plan with what an agent is *authorized* to execute in a workspace.

This document establishes the binding architectural philosophy and operational decision framework for BrainFrog. It defines:
1. **The Architecture Context Taxonomy** (Greenfield, Brownfield, Mixed, Unknown).
2. **Agent-Friendly Architecture** defined strictly in observable engineering terms.
3. **The Architecture Strategy Framework** (Keep, Refactor, Migrate, Rewrite).
4. **The Bounded Rewrite Policy** (eliminating both reckless rewrites and dogmatic preservation).
5. **The Separation of Architecture Reasoning from Execution Authority**.

Every rule in this specification answers a core operational question:
> **What observable evidence must the agent inspect, and how must that evidence govern the architectural decision?**

---

## 2. Architecture Context Taxonomy

Before proposing or planning code changes, the agent must categorize the system context into one of four mutually exclusive, rigorous categories.

```
                                [Target of Change]
                                        │
           ┌────────────────────────────┼────────────────────────────┐
           ▼                            ▼                            ▼
     [Greenfield]                 [Brownfield]                    [Mixed]
 No meaningful existing        Existing callers, state,    New component placed inside
 behavior or compatibility     APIs, or operational       an existing host system;
 contract to preserve.         assumptions constrain it.   internal freedom + external contract.
                                        │
                                        ▼
                                    [Unknown]
                          Available evidence is insufficient
                          to classify safely. (Identify gaps!)
```

### 2.1 Greenfield
A target is **Greenfield** *if and only if* there is no meaningful existing behavior, data schema, or compatibility contract that must be preserved for that target.
- **Strict Boundary:** A target is **not** greenfield merely because the specific file, module, or package does not exist on disk yet.
- **Example:** Introducing a brand-new standalone CLI utility in a fresh repository is pure greenfield. Conversely, adding `core/runtime/new_provider.py` inside BrainFrog is **not** pure greenfield, because it must satisfy existing `System2Client` interfaces, configuration conventions, and error-handling semantics.

### 2.2 Brownfield
A target is **Brownfield** when existing callers, persisted data, public APIs, user-visible behavior, configuration formats, operational assumptions, or external integrations constrain the change.
- **Observable Evidence:**
  - Callers importing symbols from the module.
  - Persisted records on disk, databases, or WAL transaction files.
  - Documented CLI flags, environment variables, or HTTP endpoints consumed by external clients.
  - Existing automated tests asserting observable outputs, exceptions, or ordering.
- **Engineering Invariant:** All changes must account for the blast radius across every discovered dependency.

### 2.3 Mixed
A target is **Mixed** when a new component or subsystem is introduced inside an existing host system.
- **Operational Reality:** The component enjoys architectural freedom *internally* (data structures, helper routines, private modular decomposition), but is strictly constrained *externally* by surrounding system contracts, shared lifecycle models, logging conventions, and error propagation boundaries.
- **Example:** Adding a new transaction storage adapter (`SQLiteTransactionStore`) to BrainFrog's runtime. The internal SQLite table layout and indexing strategy are greenfield choices, but its public methods must strictly satisfy `TransactionStoreProtocol` and transactional rollback guarantees.

### 2.4 Unknown
A target is **Unknown** when the available repository context, supplied files, or problem specifications are insufficient to determine the presence of existing callers, compatibility contracts, or state constraints.
- **Required Behavior:** The agent **must not** guess or hallucinate certainty. The agent must:
  1. Explicitly classify the context as unknown or uncertainty-bounded.
  2. State the precise missing evidence (e.g., "Callers of `process_record` are not present in supplied files; cannot determine if signature change is safe").
  3. Formulate the smallest hypothesis-driven inquiry or minimal non-breaking assumption rather than proceeding with wide-ranging architectural surgery.

---

## 3. Defining "Agent-Friendly Architecture"

"Agent-friendly architecture" is frequently misdefined as cosmetic design dogmatism: splitting files into tiny fragments, introducing excessive interfaces, creating redundant layers of abstraction, or enforcing rigid "Clean Architecture / SOLID" stereotypes. In practice, such patterns often degrade agent effectiveness by scattering state, multiplying indirection, and expanding token context costs without delivering functional value.

BrainFrog defines **Agent-Friendly Architecture** strictly through **seven observable engineering properties**:

| Property | Observable Definition | What Breaks It (Anti-Patterns) |
| :--- | :--- | :--- |
| **1. Discoverability** | An engineer or agent can immediately locate entry points, state ownership, major execution paths, configuration, and relevant tests using file tree inspection and symbol search. | Hidden side effects, dynamic global registries populated at import time without manifest declaration, deep magic meta-programming. |
| **2. Explicit Contracts** | Function signatures, inputs, outputs, failure modes, permission requirements, and side effects are defined via static types or documented schemas (`dataclass`, `TypedDict`, typed schemas). | Passing untyped `**kwargs` or generic `dict` blobs through multiple layers; implicit reliance on undocumented environment variables. |
| **3. Change Locality** | A typical behavioral change or bug fix can be completed within one or a few tightly related modules without triggering cascaded edits across the repository. | Shotgun surgery, duplicated domain logic, tight cyclic coupling between unrelated subsystems, global mutable singletons. |
| **4. Verifiability** | The correctness, invariants, and performance of a component can be validated via fast, deterministic, reproducible automated tests. | Un-mockable external network calls, reliance on non-deterministic sleeps/timers, tight coupling to concrete local hardware or OS paths. |
| **5. Reproducibility** | A developer or agent can check out the repository, run setup, and execute tests in a fresh worktree without relying on uncommitted machine-local state. | Undocumented environment variables, hardcoded absolute machine paths (e.g. `C:\Users\foo`), unpinned dependencies. |
| **6. Knowledge Availability** | Critical operational constraints, design assumptions, and security boundaries are documented locally in repository manifests (`AGENTS.md`, `README.md`, ADRs). | Tribal knowledge locked in external chat threads; unstated assumptions about production cluster configurations. |
| **7. Safe Evolution** | Existing contracts can be modified, extended, or migrated incrementally using shims, adapter patterns, or dual-write strategies without forcing atomic cross-system flag-days. | Hard breaking changes to wire protocols, destructive schema updates without forward/backward compatibility. |

---

## 4. Architecture Strategy Choices

When faced with a software task, the agent must evaluate the codebase against four distinct architectural strategies.

```
                           [Architectural Strategy]
                                       │
        ┌──────────────────┬───────────┴───────────┬──────────────────┐
        ▼                  ▼                       ▼                  ▼
[1. Keep Structure] [2. Local Refactor] [3. Incremental Migration] [4. Bounded Rewrite]
No architecture     Restructure internals;   Multi-stage transition    Replace isolated
change justified;   preserve all observable  with shims/dual-run for   component whose
focus on local edit. public contracts.       high-risk/wide blast.     replacement is simpler.
```

### 4.1 Strategy 1: Keep Existing Structure
**Core Rule:** *"No architectural change is justified"* is a valid, dignified, and often superior engineering outcome.

- **Appropriate Evidence:**
  - Existing modular boundaries cleanly accommodate the change.
  - Adding a local function or extending an existing handler satisfies the requirement without structural strain.
  - The requested task is a bug fix, feature addition, or configuration tweak that does not violate single responsibility.
- **Common Warning Signs:**
  - Reorganizing folders or splitting files simply to make them "look cleaner."
  - Introducing generic abstractions ("just in case we need multiple database backends later") when only one exists (violating YAGNI).
- **Risks:** Premature optimization, git blame disruption, unnecessary review burden.
- **Verification Burden:** Standard unit/regression tests for the modified function.

### 4.2 Strategy 2: Local Refactor
- **Appropriate Evidence:**
  - Duplication exists within a single component or module.
  - A function has grown excessively long and contains distinct sub-responsibilities that can be cleanly extracted.
  - The change preserves all external interfaces, parameter contracts, exception hierarchies, and side-effect ordering.
- **Common Warning Signs:**
  - Refactoring spills over into public API changes that break external callers.
  - Mixing refactoring with new feature development in a single diff.
- **Risks:** Accidental regression of subtle edge cases or exception semantics.
- **Verification Burden:** Existing unit tests must pass unmodified; characterization tests added if coverage was deficient.

### 4.3 Strategy 3: Incremental Migration
- **Appropriate Evidence:**
  - The change impacts a core contract with multiple callers or external consumers across the codebase.
  - Persisted data, schemas, or wire formats are being updated.
  - A single atomic "flag-day" cutover carries unacceptable risk of system downtime or broken workflows.
- **Mechanism:**
  1. Introduce the new contract alongside the old one.
  2. Implement a transitional adapter, alias, or dual-read/write shim.
  3. Migrate callers slice-by-slice.
  4. Deprecate and eventually remove the legacy contract in a subsequent milestone.
- **Common Warning Signs:**
  - Attempting to update 40 caller files simultaneously in one massive, unreviewable PR.
- **Risks:** Temporary code duplication; dangling deprecated shims if cleanup is abandoned.
- **Verification Burden:** Dual-contract validation tests, migration rollback verification, data consistency assertions.

### 4.4 Strategy 4: Bounded Rewrite
BrainFrog treats rewriting as a **legitimate, evidence-based engineering option**—neither forbidden as a taboo nor permitted as a lazy default.

#### The Bounded Rewrite Policy
A bounded rewrite is justified **only when all** of the following conditions are met:
1. **Bounded Scope:** The component has a small, clearly defined blast radius (e.g., an internal parser, formatting helper, or self-contained state machine).
2. **Enumerated Behavior:** The required inputs, outputs, error conditions, and edge cases are thoroughly understood and documented.
3. **Manageable Callers:** All callers are known and can be verified against the new implementation without cross-repository cascades.
4. **Material Simplicity:** Replacing the implementation is materially simpler, safer, and cleaner than patching a convoluted, bug-ridden spaghetti structure.
5. **Strict Verification:** High-confidence characterization or regression tests can prove full behavioral equivalence.
6. **No Hidden State:** The component owns no persistent un-migrated data or unmanaged external state.

#### Penalized Reasoning:
- **Unsupported Rewrite:** *"This code looks messy / legacy / not modern, so I will rewrite it."* (CRITICAL FAILURE).
- **Reflexive Dogmatic Preservation:** Preserving a 500-line broken, fragile regular expression parser when a 40-line clean replacement handles all known cases, passes all tests, and eliminates multiple CVEs.

---

## 5. Separation of Architecture Reasoning from Execution Authority

A fundamental architectural principle in BrainFrog is the strict decoupling of **reasoning** from **execution authority**.

```
┌────────────────────────────────────────────────────────────────────────┐
│                        BRAINFROG CONTROL PLANE                         │
├──────────────────────────┬─────────────────────────────────────────────┤
│ 1. Architecture Context  │ Greenfield │ Brownfield │ Mixed │ Unknown   │
│ 2. Architecture Strategy │ Keep │ Refactor │ Migrate │ Rewrite         │
│ 3. Execution Mode        │ PLAN (Read-Only) │ BUILD (Mutating)         │
│ 4. Authorization / Scope │ Workspace Root, Explicit File Allowlist,    │
│                          │ Approved Execution Contract Capabilities    │
└──────────────────────────┴─────────────────────────────────────────────┘
```

These four axes operate independently:
1. **Context does not dictate Mode:**
   - A task in a pure **Greenfield** project may still run in `PLAN` mode. In `PLAN` mode, the agent is strictly prohibited from writing code files, regardless of how novel or greenfield the feature is.
2. **Strategy does not confer Authorization:**
   - Identifying that a component qualifies for a **Bounded Rewrite** does *not* grant the agent authority to delete or recreate files outside its approved scope.
   - If the agent is operating under an `ApprovedExecutionContract` scoped to `src/parser.py`, it cannot touch `src/utils.py` even if it believes a rewrite of `utils.py` is architecturally superior.
3. **Missing Context is not Permission to Expand:**
   - When context is **Unknown**, the agent must reduce its blast radius and request clarification, not expand its mutation scope.
4. **Plan vs Execution Contract:**
   - In `PLAN` mode, planning outputs are structured PRDs and implementation blueprints written solely to `.brainfrog/plans/`.
   - In `BUILD` mode, execution proceeds in small, transaction-tracked, reversible steps governed by runtime verification and secret scanning.

---

## 6. Architecture Decision Matrix

| Context | Recommended Default Strategy | When to Deviate | Mandatory Verification |
| :--- | :--- | :--- | :--- |
| **Greenfield** | Keep Structure (Minimal viable architecture) | Local Refactor if rapid prototyping reveals better component boundaries. | Establish primary contract tests and CI smoke tests. |
| **Brownfield (Single Caller / Leaf)** | Keep Structure or Local Refactor | Bounded Rewrite if component is self-contained and rewriting is simpler than patching. | Full existing test suite + characterization tests for modified symbol. |
| **Brownfield (Multi-Caller / Core API)** | Keep Structure or Incremental Migration | Local Refactor if external signature is 100% preserved. | Integration tests across all callers; backward compatibility tests. |
| **Mixed (New subsystem in host)** | Keep host structure; greenfield internal discipline | Incremental Migration if new subsystem replaces an older subsystem. | Contract compliance tests against host interfaces; isolation checks. |
| **Unknown (Incomplete context)** | Keep Structure / Bounded Exploration | Never attempt Rewrite or Migration until context is gathered. | Probe checks; identify exact missing symbols or callers. |

---

## 7. Summary for Agent Tool & Prompt Designers

1. Never instruct the model that "clean architecture" means more classes or layers.
2. Demand that every architectural recommendation cite specific file paths, symbols, and observed dependencies.
3. Reward decisions that maintain stability and change locality over aesthetic perfection.
4. Enforce that missing evidence produces explicit statements of uncertainty rather than speculative redesigns.
