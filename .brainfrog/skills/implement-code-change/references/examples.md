# Implement code change: qualitative scenarios

These are hypothetical manual evaluation fixtures, not claims about repository behavior or measured model performance. Supply the described context and current method schema. Inspect the actual response; do not assume this reference was loaded automatically.

## 1. Mixed context: new driver in existing repository

**Given:** A project contains `db/base_driver.py` defining `DatabaseDriver` and `db/registry.py` with `register_driver(name, driver_cls)`. The user asks to add a CockroachDB driver.

**Expected:**
- Strategy: `keep` (extending existing provider architecture).
- Implement `CockroachDriver` conforming strictly to `DatabaseDriver` abstract methods.
- Wire into `db/registry.py` matching existing driver registration patterns.
- Reuse host error taxonomy and configuration lookup conventions.
- Tests exercise initialization, query execution, connection pooling, and error mapping.

**Reject:** Creating an independent database connection framework that bypasses `DatabaseDriver` or invents custom transaction semantics.

## 2. Incomplete requirements and missing domain rules

**Given:** An existing quota function `calculate_storage_quota(account_id, base_limit)` is provided without tier definitions, retention policies, or compression flags. The user asks to "implement tier-based compression and custom retention policies."

**Expected:**
- Classify target context as *unknown* regarding retention tiers and compression options.
- Note the missing business logic in `clarifying_questions` or `assumptions`.
- Do not invent speculative retention thresholds or hardcoded compression codec mappings.
- If implementing an interface extension, provide an optional, injectable policy callable or configuration mapping parameter with backward-compatible defaults, while clearly asking for the real rules.

**Reject:** Inventing arbitrary retention limits or compression rules and hardcoding them as production requirements.

## 3. External integration boundary validation

**Given:** An event listener receives JSON notifications from an external integration partner. The user asks to add handling for a `document.archived` event.

**Expected:**
- Derive validation behavior from the observed contract: for loosely controlled or evolving event payloads, avoid adding rigid validation that crashes or rejects payloads containing harmless unmodeled metadata.
- For strict security or compliance boundaries, enforce the required schema explicitly.
- Preserve backward-compatible dispatch for existing event types.
- Safely extract needed fields with sensible fallbacks and boundary exception handling.

**Reject:** Universally mandating either extreme (blindly rejecting harmless extra fields on loose feeds, or ignoring strict verification where security contracts demand it).
