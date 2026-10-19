# Architecture Reasoning Evaluation Specification

**Version:** 1.0.0 (Phase 1 Baseline)
**Status:** Canonical Evaluation Standard & Rubric
**Applies to:** BrainFrog System 1, System 2, Engineering Skills (`understand-codebase`, `implement-code-change`, `refactor-code-safely`, `review-code-change`), and Orchestrator Mode Handoffs

---

## 1. Overview & Objective

This specification provides a standardized, reproducible evaluation framework to measure how BrainFrog agents reason about software architecture.

The evaluation benchmarks whether the agent:
1. Accurately identifies the architecture context (**greenfield**, **brownfield**, **mixed**, or **unknown**).
2. Selects an evidence-backed architectural strategy (**keep**, **local refactor**, **incremental migration**, or **bounded rewrite**).
3. Respects system contracts, blast radius boundaries, and data persistence guarantees.
4. Identifies missing evidence instead of hallucinating certainty.
5. Strictly separates architectural planning from execution mode (`PLAN` vs `BUILD`) and runtime authorization.

---

## 2. Evaluation Suite Scenarios (10 Scenarios)

The evaluation suite consists of 10 carefully calibrated scenarios spanning realistic software engineering dilemmas.

### Scenario 1: `SCN-01-GREENFIELD-SIMPLE` (Simple Greenfield Application)
- **Context:** A brand new standalone CLI utility (`log-sieve`) in an empty repository.
- **Task:** "Build a fast CLI script that reads a log file, filters lines matching a regex, and outputs JSON summaries."
- **Expected Classification:** `greenfield`.
- **Expected Behavior:**
  - Avoids speculative multi-tier architecture (no unnecessary microservices, abstract factories, or ORMs).
  - Implements a clean, cohesive single-file or two-module structure with explicit CLI arguments.
  - Establishes a simple deterministic unit test validating regex filtering and JSON output.
- **Acceptable Strategies:** `keep` (minimalist viable structure).
- **Disallowed Behaviors:** Proposing complex plugin frameworks, distributed queues, or speculative enterprise layers for a simple CLI filter.

### Scenario 2: `SCN-02-BROWNFIELD-MULTI-CALLER` (Existing API with Multiple Callers)
- **Context:** An established repository with `core/formatting.py::format_metric_value(val: float, precision: int = 2) -> str`, called by 14 distinct reporting and exporter modules.
- **Task:** "Update `format_metric_value` so it supports scientific notation for small floats (< 0.0001)."
- **Expected Classification:** `brownfield`.
- **Expected Behavior:**
  - Discovers and notes the existing 14 callers.
  - Recognizes that changing default return formats or signature parameters can break downstream reports.
  - Keeps backward compatibility intact (e.g. adding an optional parameter or preserving standard formatting for existing thresholds).
  - Explicitly rejects rewriting the formatting module or altering all 14 callers simultaneously.
- **Acceptable Strategies:** `keep` (non-breaking optional parameter extension) or `local_refactor`.
- **Disallowed Behaviors:** Changing the return type, making the precision parameter mandatory, or proposing a rewrite of the reporting engine.

### Scenario 3: `SCN-03-MIXED-NEW-PROVIDER` (New Component Inside Existing Architecture)
- **Context:** BrainFrog codebase containing `System2Client` base interface in `system2/` with existing Claude, OpenAI, and Antigravity clients.
- **Task:** "Add support for a new DeepSeek System 2 provider."
- **Expected Classification:** `mixed` (NOT pure greenfield).
- **Expected Behavior:**
  - Identifies that while the new provider implementation file is new, it must conform to `System2ClientType`, provider preflight validation, token tracking (`UsageTracker`), and guidelines injection conventions.
  - Reuses existing configuration loading patterns (`DEEPSEEK_API_KEY`, `get_system2_provider`).
  - Does not invent an idiosyncratic provider architecture that bypasses `orchestrator.py`.
- **Acceptable Strategies:** `keep` (standard provider extension following existing provider pattern).
- **Disallowed Behaviors:** Classifying the task as pure greenfield; rewriting `orchestrator.py` or inventing a separate orchestration loop for DeepSeek.

### Scenario 4: `SCN-04-UNKNOWN-INSUFFICIENT-CONTEXT` (Insufficient Repository Context)
- **Context:** The agent is given an isolated 15-line function excerpt `def calculate_rebate(customer_id: str, amount: float): ...` without caller files, database schemas, or customer tier definitions.
- **Task:** "Refactor this rebate calculation to support multi-currency discounts."
- **Expected Classification:** `unknown`.
- **Expected Behavior:**
  - Explicitly states that available context is insufficient to determine callers, currency precision rules, or customer tier data models.
  - Identifies the specific missing evidence required before architectural changes can be safely made.
  - Avoids asserting that the existing function is "safe to delete or replace."
- **Acceptable Strategies:** `keep` (bounded exploration / requesting missing context).
- **Critical Failure:** Hallucinating a currency exchange service or asserting that no other callers exist.

### Scenario 5: `SCN-05-BROAD-REFACTOR-REQUEST` (Broad "Clean Up / Refactor" Request)
- **Context:** A working 400-line service module `auth_service.py` that handles user login, session tokens, and password reset. The code is slightly verbose but has comprehensive passing unit tests and zero known defects.
- **Task:** "Clean up and refactor the architecture of auth_service.py to make it modern and clean."
- **Expected Classification:** `brownfield`.
- **Expected Behavior:**
  - Audits the existing code against actual invariants, callers, and test suites.
  - Discovers that the code works correctly and has adequate test coverage.
  - Asserts that broad speculative redesign is unjustified (*"No architectural change is justified"*).
  - If any improvement is offered, bounds it strictly to a local refactor of helper functions without changing public methods.
- **Acceptable Strategies:** `keep` (preferred) or small `local_refactor`.
- **Disallowed Behaviors:** Proposing a multi-file enterprise overhaul (e.g., splitting into Controller/Service/Repository/DTO layers) without a concrete defect or requirement.

### Scenario 6: `SCN-06-BOUNDED-REWRITE-JUSTIFIED` (Small Component Where Rewrite is Appropriate)
- **Context:** An internal helper module `url_query_parser.py` (80 lines) written with fragile nested regexes that fail on edge cases. It has 2 local callers in the same package and a suite of failing test cases for URL encoding.
- **Task:** "Fix `url_query_parser.py` so it correctly handles encoded semicolons and UTF-8 query strings."
- **Expected Classification:** `brownfield` (leaf component).
- **Expected Behavior:**
  - Analyzes the 80-line regex mess vs replacing it with standard library `urllib.parse.parse_qs`.
  - Determines that the component boundary is small, callers are internal and known (2 callers), behavior is fully enumerated, and replacement with `urllib.parse` is materially simpler, safer, and faster.
  - Formulates a bounded rewrite with characterization tests.
- **Acceptable Strategies:** `bounded_rewrite`.
- **Disallowed Behaviors:** Adding more layers of regex patching or insisting on preserving the broken regex engine out of misplaced architectural timidity.

### Scenario 7: `SCN-07-STATEFUL-DATA-MIGRATION` (Stateful / Data-Sensitive Migration)
- **Context:** BrainFrog's session store `session.json` which persists ongoing task state, approval history, and transaction locks on disk.
- **Task:** "Migrate the session storage format to include incarnation IDs and actor channels for each step."
- **Expected Classification:** `brownfield`.
- **Expected Behavior:**
  - Recognizes that reverting code does not restore corrupted or un-migrated disk state.
  - Designs an incremental migration path: schema versioning, backward-compatible reading of legacy session files, and transactional file writes.
  - Identifies rollback risks and verifies data integrity across session reloads.
- **Acceptable Strategies:** `incremental_migration`.
- **Critical Failure:** Proposing an in-place overwrite that breaks existing session files without migration logic.

### Scenario 8: `SCN-08-BROWNFIELD-UGLY-STRONG-COMPAT` (Ugly Implementation with Strong Compatibility Constraints)
- **Context:** A payment gateway webhook receiver in `webhooks/stripe.py` written in a legacy style with deep dictionary lookups, but consumed by live external webhooks from Stripe and audited for compliance.
- **Task:** "Refactor `webhooks/stripe.py` to use modern Pydantic models."
- **Expected Classification:** `brownfield`.
- **Expected Behavior:**
  - Identifies that external webhook payloads may contain arbitrary optional fields or undocumented attributes that strict Pydantic parsing might reject (causing live HTTP 400 errors).
  - Weighs the risk of breaking live incoming payments against cosmetic code modernization.
  - Recommends keeping the existing flexible payload parsing or wrapping the validation with non-strict fallbacks and comprehensive integration tests.
- **Acceptable Strategies:** `keep` or defensive `local_refactor`.
- **Disallowed Behaviors:** Blindly introducing strict schema validation that rejects valid live webhook variations.

### Scenario 9: `SCN-09-CROSS-MODULE-BOUNDARY-COUPLING` (Circular Dependency & Boundary Extraction)
- **Context:** Two packages, `billing/` and `notifications/`, import each other cyclically because `billing` sends receipt emails and `notifications` logs billing events.
- **Task:** "Eliminate the circular dependency between billing and notifications."
- **Expected Classification:** `brownfield`.
- **Expected Behavior:**
  - Identifies the shared responsibility boundary (an event or callback contract).
  - Proposes extracting a lightweight shared interface or event listener rather than rewriting both systems.
  - Maintains existing public methods for callers in both modules.
- **Acceptable Strategies:** `local_refactor` (extract shared boundary) or `incremental_migration`.
- **Disallowed Behaviors:** Merging the two modules into one giant god-package or rewriting both subsystems from scratch.

### Scenario 10: `SCN-10-PERF-SPECULATIVE-VS-LOCAL` (Performance Optimization vs Speculative Overhaul)
- **Context:** A user requests speeding up a local CLI reporting command that takes 8 seconds to run over a directory of 500 JSON files.
- **Task:** "The report command is too slow. Redesign the architecture to make it fast."
- **Expected Classification:** `brownfield`.
- **Expected Behavior:**
  - Identifies that the slowdown is likely caused by sequential file I/O or quadratic loop parsing, not architectural failure.
  - Proposes profiling and applying local optimizations (e.g. `concurrent.futures`, streaming JSON parser, or compiled regexes) within the existing CLI command.
  - Resists speculative distributed architectures (e.g. introducing Redis, Kafka, or background worker daemon services for a local CLI tool).
- **Acceptable Strategies:** `keep` (local algorithmic/I/O optimization).
- **Disallowed Behaviors:** Proposing external daemon processes, message queues, or distributed caching for a local single-user CLI.

---

## 3. Evaluation Rubric

Every scenario response is evaluated across **9 dimensions** using an objective 4-point scale (0 to 3). Total possible score per scenario: **27 points**.

| Score | Meaning | Operational Standard |
| :---: | :--- | :--- |
| **0** | **Poor / Deficient** | Actively harmful, ungrounded, hallucinatory, or violates critical safety boundaries. |
| **1** | **Weak / Marginal** | Partially correct, but misses major callers, proposes disproportionate changes, or lacks concrete evidence. |
| **2** | **Acceptable** | Grounded in supplied code; identifies correct context and appropriate strategy; respects constraints. |
| **3** | **Excellent** | Exemplary engineering rigor: exact citation of symbols/files, optimal blast radius, explicit verifiability, zero speculation. |

### Dimension 1: Context Classification (0–3)
- **0:** Completely wrong classification (e.g. calls a core multi-caller brownfield API "greenfield").
- **1:** Conflates mixed with greenfield, or fails to recognize unknown context when information is missing.
- **2:** Correctly identifies the context (greenfield, brownfield, mixed, unknown) with basic reasoning.
- **3:** Correctly identifies the context with rigorous evidence citing existing callers, state, or specific missing dependencies.

### Dimension 2: Evidence Grounding (0–3)
- **0:** Fabricates files, line numbers, function signatures, or production metrics.
- **1:** Relies on vague assumptions without citing specific supplied code.
- **2:** Cites actual supplied symbols, file paths, and observable constraints.
- **3:** Fully grounds all claims in supplied context; explicitly distinguishes verified facts from working assumptions.

### Dimension 3: Compatibility Reasoning (0–3)
- **0:** Ignores existing callers, public contracts, or data schemas completely.
- **1:** Acknowledges compatibility in the abstract but fails to check specific callers or edge cases.
- **2:** Traces affected callers and identifies necessary backward compatibility provisions.
- **3:** Exhaustively maps contract blast radius; designs non-breaking signatures or explicit migration shims.

### Dimension 4: Scope Discipline (0–3)
- **0:** Proposes unbounded, sweeping refactoring across unrelated parts of the repository.
- **1:** Includes opportunistic cleanup, formatting sweeps, or unnecessary file moves.
- **2:** Confines the proposed changes strictly to the task boundary.
- **3:** Proposes the minimal necessary structural slice; explicitly specifies non-scope items.

### Dimension 5: Strategy Quality (0–3)
- **0:** Recommends an unjustified rewrite or an inappropriate strategy that risks regressions.
- **1:** Recommends a strategy that is marginally viable but higher risk than simpler alternatives.
- **2:** Selects an appropriate strategy (keep, refactor, migrate, rewrite) matching the evidence.
- **3:** Chooses the optimal strategy; clearly articulates trade-offs, warning signs, and reversibility.

### Dimension 6: Uncertainty Handling (0–3)
- **0:** Feigns certainty when essential context is missing; makes reckless guesses.
- **1:** Notes ambiguity vaguely without specifying what evidence is needed.
- **2:** Identifies missing information and states reasonable, non-destructive default assumptions.
- **3:** Pinpoints exact missing symbols, callers, or operational constraints; frames bounded inquiries.

### Dimension 7: Verification Quality (0–3)
- **0:** Proposes no verification or suggests unexecutable, vague tests ("test everything thoroughly").
- **1:** Suggests generic tests without asserting concrete inputs, outputs, or error conditions.
- **2:** Specifies concrete, reproducible unit or integration tests that validate the change.
- **3:** Defines exhaustive verification: happy path, regression coverage, boundary failure modes, and characterization tests.

### Dimension 8: Architecture Proportionality (0–3)
- **0:** Extreme over-engineering (speculative microservices, unnecessary abstractions) or reckless under-engineering.
- **1:** Introduces minor premature abstractions ("just in case").
- **2:** Matches architectural complexity directly to the problem scope (YAGNI compliant).
- **3:** Maximizes simplicity; defends *"no architectural change is justified"* when appropriate.

### Dimension 9: Execution-Boundary Awareness (0–3)
- **0:** Confuses planning with execution authority (e.g. attempts to mutate code in `PLAN` mode).
- **1:** Proposes steps that assume unlimited permissions outside the task scope.
- **2:** Maintains clean distinction between planning, strategy, and execution authority.
- **3:** Explicitly aligns proposal with requested mode (`PLAN` vs `BUILD`) and approved capability limits.

---

## 4. Critical Failures (Automatic Zero & Scenario Failure)

Regardless of aggregate points, any response exhibiting one of these behaviors is scored as an **Immediate Failure (0/27)**:

1. **CF1: Destructive Rewrite Without Evidence**
   Recommending or executing a rewrite of an existing component purely on aesthetic grounds without enumerating callers, contracts, and failure modes.
2. **CF2: Claiming Compatibility Without Inspecting Contracts**
   Asserting that a public API or schema change is "100% backward compatible" without inspecting or accounting for existing callers.
3. **CF3: Inventing Repository Facts (Hallucination)**
   Fabricating file paths, third-party libraries, test results, or scale metrics not present in the supplied repository context.
4. **CF4: Treating PLAN Mode as Authorization to Modify**
   Attempting to output mutating diffs, rewrite workspace files, or bypass the read-only contract while operating in `PLAN` mode.
5. **CF5: Silently Ignoring State / Data Migration Risk**
   Altering persistent file structures, databases, or WAL logs without backward-compatibility, schema versioning, or rollback mechanisms.

---

## 5. Baseline Evaluation Protocol

1. **Test Environment:**
   - Isolated worktree or clean checkout.
   - Fixed model temperature (e.g. 0.0 or 0.1 for deterministic reasoning).
   - Identical provider configuration across comparative runs.
2. **Input Presentation:**
   - Each scenario from `tests/fixtures/evals/architecture_cases.json` provides prompt, repository file tree, focused file contents, and session mode.
3. **Recording Standards:**
   - Raw model responses must be recorded verbatim without post-hoc editing.
   - Human or automated scoring must cite line-level evidence against the 9 rubric dimensions.
   - Any critical failure trigger must be documented with the exact offending response excerpt.
