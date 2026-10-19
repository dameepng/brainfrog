# Review code change: qualitative scenarios

These are hypothetical manual evaluation fixtures, not claims about repository behavior or measured model performance. Supply the described context and current method schema. Inspect the actual response; do not assume this reference was loaded automatically.

## 1. Flagging ungrounded domain logic and invented requirements

**Given:** A task requested: "Update calculate_tax(amount) to accept an optional postal_code string." The supplied diff adds `postal_code: Optional[str] = None`, but also adds a hardcoded dictionary mapping arbitrary postal codes to speculative tax rates (e.g. `{"90210": 0.095, "10001": 0.088}`) and raises `ValueError` if a postal code is not in that list.

**Expected:**
- Flag the invented postal code tax rate table as ungrounded domain logic.
- Identify that the requirement asked only to accept the optional parameter, not to invent arbitrary geographical tax rates or reject valid postal codes not in the hardcoded list.
- Propose that tax rate lookup should either be passed in, delegated to an existing tax service, or kept neutral until official rate rules are supplied.

**Reject:** Approving the PR as complete without questioning where the tax rates came from.

## 2. Reviewing speculative architectural overhaul

**Given:** A PR claims to "refactor the notification subsystem." It replaces a simple 80-line working notification function with 6 new classes (AbstractNotificationDispatcher, NotificationPayloadDTO, FactoryProvider, DeliveryPolicyStrategy) across 4 new directories. The existing caller and unit test behavior is unchanged.

**Expected:**
- Question whether the structural overhaul was justified by an observable defect.
- Note the increased cognitive and maintenance overhead introduced by speculative abstraction layers.
- Ask the author to provide concrete evidence of change-locality problems or co-change friction that justifies the new hierarchy over the previous simple module.

**Reject:** Praising the PR for "clean architecture" or "applying design patterns" without evaluating proportionality.

## 3. Detecting rigid validation on evolving integration boundaries

**Given:** A diff modifies an external integration event receiver that previously read payload fields defensively. The diff replaces it with strict schema validation that categorically rejects any payload containing unmodeled attributes.

**Expected:**
- Compatibility finding: evaluate whether the external contract expects loose or strict payload shapes. If the upstream provider regularly includes optional metadata or evolves without breaking versions, rejecting unmodeled attributes risks live ingestion failures.
- Recommend deriving validation from contract requirements: safely capturing required fields and logging or tolerating unmodeled metadata on evolving feeds, while strictly enforcing validation on security-critical parameters (e.g. signatures, tokens, amounts).
- Evaluate dependency and complexity costs of introduced validation frameworks.

**Reject:** Universally mandating one-size-fits-all validation (either unconditionally forbidding extra fields without checking upstream contract evolution, or forbidding strict validation where security requires it).

## 4. Detecting state migration risks

**Given:** A diff modifies `cache_storage.py` to change the serialization format from single JSON files to a binary format. The new `load()` method crashes with `UnicodeDecodeError` when encountering existing legacy JSON files on disk.

**Expected:**
- High-priority data integrity finding: breaking backward compatibility for existing cached data on disk without fallback logic or a migration path.
- Recommend inspecting the file header or extension to support reading legacy files, or providing a safe migration/reset strategy.

**Reject:** Ignoring disk state compatibility and claiming the change is safe because unit tests on fresh instances pass.
