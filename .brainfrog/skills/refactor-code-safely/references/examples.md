# Refactor code safely: qualitative scenarios

These are hypothetical manual evaluation fixtures, not claims about repository behavior or measured model performance. Supply the described context and current method schema. Inspect the actual response; do not assume this reference was loaded automatically.

## 1. Defending "No architectural change is justified"

**Given:** A 250-line service module `metrics_aggregator.py` calculates rollups and counters for telemetry logs. The code is written in a simple procedural style, has high unit test coverage, and has zero open bugs or performance bottlenecks. The prompt requests: "Clean up and modernize this module into an enterprise architecture with abstract collectors and repository layers."

**Expected:**
- Strategy: `keep` (preferred) or minimal local helper extraction.
- Assess that the current architecture functions correctly, satisfies invariants, and supports required extensions.
- State clearly that *"No architectural redesign is justified"*.
- Reject dogmatic overhauls (e.g. splitting into Controller/Service/Repository/DTO layers or adding speculative abstraction classes) that add cognitive overhead without solving an observable defect.
- Confine any edit to minor, non-breaking internal helper extractions or typing clarifications without altering public method signatures.

**Reject:** Proposing a multi-file structural overhaul, adding speculative abstraction hierarchies, or altering method signatures without an observable problem.

## 2. Bounded rewrite of an isolated fragile component

**Given:** An internal helper module `cron_schedule_parser.py` (70 lines) uses complex manual string slicing to parse standard 5-part cron expressions. It crashes on step intervals and range lists, and has 2 known internal callers in the job runner. The prompt requests: "Fix cron_schedule_parser to handle step intervals and ranges."

**Expected:**
- Strategy: `bounded_rewrite`.
- Analyze that the component boundary is small, callers are known (2 callers), and replacing the buggy manual slicing with a standard library or established datetime/modulo helper is materially safer and simpler than patching the string splitter.
- Explicitly identify the work as a *bounded rewrite*.
- Write characterization unit tests capturing expected behavior before replacing the internal logic.
- Keep the public signature and return types identical so callers require zero modifications.

**Reject:** Attempting to patch brittle manual parsing with more fragile conditionals; or proposing a sprawling rewrite that modifies callers across the package.

## 3. Stateful and data-sensitive migration

**Given:** An embedded metadata store `storage/metadata_store.py` persists task records to JSON files on disk. The prompt requests: "Update index records to track schema version and replica timestamps."

**Expected:**
- Strategy: `incremental_migration`.
- Recognize that altering persistent disk structures carries data corruption and rollback risks.
- Implement defensive deserialization: legacy record files missing the new fields must load cleanly with sensible defaults (e.g. `schema_version=1`, `replica_timestamp=None`).
- Use project-supported safe file writing mechanisms (e.g. writing to a sibling temporary file before atomic renaming) to prevent zero-byte files on process interruption.
- Add unit tests verifying both legacy format loading and safe write recovery.

**Reject:** Proposing in-place destructive writes that crash on historical data files; ignoring safe write guarantees.

## 4. Integration boundary validation and compatibility

**Given:** An inventory ingest service `ingest_supplier_catalog(feed: dict) -> bool` receives updates from external suppliers. It extracts two fields via `.get()`. The prompt requests: "Modernize this handler with strong schema validation."

**Expected:**
- Derive validation rules from the operational contract: if supplier feeds include non-standard optional attributes or evolving metadata, avoid brittle schemas that throw unhandled exceptions and drop valid catalog updates.
- For required fields (SKU, quantity), validate strictly; for optional or supplier-specific metadata, tolerate extra fields or record them safely.
- Wrap validation with boundary error handling to ensure bad payloads produce clean error logs rather than unhandled crashes.
- Evaluate whether adding heavyweight validation classes is proportionate for a simple handler, or whether defensive dictionary inspection remains simpler and more resilient.

**Reject:** Mandating rigid schemas that drop valid business events on evolving feeds, or alternatively ignoring contract requirements where strict validation is mandated.
