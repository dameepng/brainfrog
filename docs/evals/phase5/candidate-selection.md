# Phase 5A: Candidate Selection Report

**Repository:** `dameepng/brainfrog`
**Evaluation Root:** `C:\dame-project\tools\brainfrog-phase5-pilot`
**Date:** October 19, 2026
**Auditor:** External Supervising Agent

---

## 1. Candidate Discovery

Three candidate subsystems within the Brainfrog codebase were evaluated against strict brownfield criteria. Candidates were chosen from real existing modules with established callers, persistent contracts, and demonstrable engineering needs.

### Candidate 1: `system2/json_utils.py` (LLM Output JSON Auto-Repair & Extraction)
- **Subsystem**: `system2/json_utils.py` ([`extract_json`](file:///C:/dame-project/tools/agentic_dev/system2/json_utils.py#L84), [`repair_json_content`](file:///C:/dame-project/tools/agentic_dev/system2/json_utils.py#L18), [`attempt_close_truncated_json`](file:///C:/dame-project/tools/agentic_dev/system2/json_utils.py#L52)).
- **Callers**: `system2/antigravity_client.py`, `system2/claude_client.py`, `system2/openai_client.py`, `orchestrator.py`, `tests/test_json_utils.py`.
- **Existing Problem / Defect**:
  1. **Unquoted Python Literals**: LLMs frequently emit Python boolean and null literals (`True`, `False`, `None`) inside JSON outputs (e.g. `{"is_small_task": False, "data": None}`). Currently, `repair_json_content()` only handles JSX expressions, trailing commas, and backslashes. Unquoted Python literals trigger an unrecoverable `json.decoder.JSONDecodeError` (`ValueError: Failed to parse valid JSON from model output`).
  2. **Comments in JSON**: Reasoning models (such as Gemini 3.8 Flash, DeepSeek, Claude) frequently include inline comments (`// ...`) or block comments (`/* ... */`) to annotate generated steps or explanations. In standard JSON, comments cause a fatal decode error.
- **Compatibility Obligations**: Must preserve 100% of existing behavior in `tests/test_json_utils.py` (clean JSON, JSX unescaped quotes, invalid backslash escapes, trailing commas, truncated JSON auto-closure, root candidate scoring).

### Candidate 2: `core/modules.py` (Domain Module Map & Focus Tree Resolution)
- **Subsystem**: `core/modules.py` ([`resolve_focus_tree`](file:///C:/dame-project/tools/agentic_dev/core/modules.py#L224), [`load_module_map`](file:///C:/dame-project/tools/agentic_dev/core/modules.py#L178), [`detect_tech_stack`](file:///C:/dame-project/tools/agentic_dev/core/modules.py#L92)).
- **Callers**: `orchestrator.py`, `cli.py`, `core/runtime/session.py`, `tests/test_phase3_e2e.py`.
- **Existing Problem / Defect**:
  1. Default preset for Python (`PRESETS["python"]`) includes `"*.py"` in `core.paths`. In `resolve_focus_tree()`, it checks `(repo_dir / prefix).is_file()` or `is_dir()`, which returns `False` for glob patterns like `"*.py"`, silently omitting all root Python files from the domain focus tree.
  2. In `load_module_map()`, missing keys in custom `modules.json` (such as `'description'`) raise unhandled `KeyError` warnings and fall back to destructive auto-generation.
- **Compatibility Obligations**: Must preserve `Domain` dataclass and public `resolve_focus_tree(repo_dir, domain, max_files) -> str` signature.

### Candidate 3: `core/runtime/secret_scrubbing.py` (Secret Scrubbing & Assignment Redaction)
- **Subsystem**: `core/runtime/secret_scrubbing.py` ([`safe_scrub`](file:///C:/dame-project/tools/agentic_dev/core/runtime/secret_scrubbing.py), [`is_sensitive_key`](file:///C:/dame-project/tools/agentic_dev/core/runtime/secret_scrubbing.py#L47), [`_scrub_private_keys`](file:///C:/dame-project/tools/agentic_dev/core/runtime/secret_scrubbing.py#L54)).
- **Callers**: `core/runtime/contract.py`, `core/runtime/approval.py`, `orchestrator.py`, `git_guard.py`.
- **Existing Problem / Defect**:
  1. `_ASSIGNMENT_PATTERN` does not match environment export assignments (`export API_KEY=...`) or multi-line key-value configurations.
  2. URL query parameters containing secret keys (e.g., `?token=...`) are not reliably sanitized.
- **Compatibility Obligations**: Must never regress on redacting private keys or tokens; must pass all `git_guard` scanner assertions.

---

## 2. Candidate Scoring & Evaluation Matrix

Each candidate was scored on a 0–3 scale across six defined criteria:

| Criterion | Candidate 1 (`json_utils.py`) | Candidate 2 (`modules.py`) | Candidate 3 (`secret_scrubbing.py`) |
|---|---|---|---|
| **1. Clear brownfield compatibility obligations** | **3** (High: core LLM parser used by all providers) | **3** (High: domain registry used by System 1 Jev) | **3** (High: system-wide redaction contract) |
| **2. Evidence of an actual engineering need** | **3** (Demonstrated failures on real LLM outputs) | **3** (Demonstrated warning and glob omission) | **2** (Moderate edge cases in assignment regex) |
| **3. Existing characterization/test quality** | **3** (Dedicated unit suite covering 8 scenarios) | **2** (Tested mostly via end-to-end integration) | **3** (Extensive security test coverage) |
| **4. Limited implementation blast radius** | **3** (Contained to 1 module and 1 test file) | **3** (Contained to 1 module) | **1** (High risk of scanner false positives/negatives) |
| **5. Ability to verify behavior objectively** | **3** (Deterministic string-to-dict input/output) | **3** (Deterministic directory tree strings) | **3** (Deterministic redaction outputs) |
| **6. Suitability for safe isolated execution** | **3** (Zero external dependencies or side effects) | **3** (Filesystem read operations only) | **2** (Touches security scanner boundaries) |
| **Total Score (out of 18)** | **18 / 18** | **17 / 18** | **14 / 18** |

---

## 3. Final Selection Decision

**Selected Candidate:** **Candidate 1 — `system2/json_utils.py` (Robust LLM JSON Auto-Repair for Python Literals and Comments)**.

### Rationale
Candidate 1 scored highest (18/18). It addresses a demonstrated, high-impact operational failure mode where models outputting valid Python expressions or annotated JSON fail closed. It has an existing unit test suite (`tests/test_json_utils.py`), a self-contained blast radius, and 100% objective input/output verification without security or side-effect risks.
