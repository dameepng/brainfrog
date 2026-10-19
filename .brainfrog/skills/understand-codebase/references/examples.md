# Understand codebase: qualitative scenarios

These are hypothetical manual evaluation fixtures, not claims about repository behavior or measured model performance. Supply the described context and current method schema. Inspect the actual response; do not assume this reference was loaded automatically.

## 1. Missing policy implementation (Unknown Context)

**Given:** An adapter calls `runtime.handle(message)`. Supplied runtime code calls `policy.check(message)` before dispatch, but the policy implementation is absent. The user asks how authorization is enforced and what commands are permitted.

**Expected:**
- Classify target architecture context as *unknown* regarding authorization rules.
- Trace the observed handoff and check ordering, citing supplied symbols (`runtime.handle`, `policy.check`).
- Identify the absent policy implementation as missing evidence for permission rules.
- Ask a focused question or note the limitation through allowed response fields.
- Do not invent an allowlist, authorization schema, command tiers, or default permissions.

**Reject:** Inventing an allowlist or permission rules; claiming an unsupplied test passed; declaring the system "secure" or "safe to modify".

## 2. New component in existing architecture (Mixed Context)

**Given:** An established data pipeline has `base_sink.py:BaseSink` and a registry in `sinks/__init__.py`. The user asks to add support for an Amazon S3 sink.

**Expected:**
- Classify the target context as *mixed* (new sink file, but host architecture is brownfield).
- Identify the host contracts the new sink must satisfy: `BaseSink` abstract methods, registration in `sinks/__init__.py`, and standard configuration conventions.
- Explicitly note that while the sink implementation is greenfield, it is bound by host contracts.

**Reject:** Classifying the task as pure greenfield; proposing a separate parallel pipeline or bypassing `BaseSink`.

## 3. Working code with no observable architectural defect

**Given:** A 300-line service module `order_processor.py` handles order calculation and logging. The code is procedural and slightly verbose, but has 100% passing unit tests and zero known defects. The user asks if the architecture needs refactoring.

**Expected:**
- Audits the code against actual invariants, callers, and test coverage.
- Concludes that the existing architecture already satisfies requirements and supports extensions.
- Concludes that *"No architectural change is justified"* at this time.
- Avoids prescribing speculative abstractions (e.g. splitting into CQRS or multi-tier service/repository layers).

**Reject:** Proposing an architectural overhaul based on aesthetic preference, procedural style, or dogmatic Single Responsibility without an observable defect.

## 4. Documentation conflicts with code

**Given:** Supplied documentation says sessions are ephemeral. Supplied implementation persists them when a particular configuration flag is enabled. The user asks for an explanation only.

**Expected:** Explain the actual conditional behavior, cite both sources, and identify the discrepancy. No changed files are returned.

**Reject:** Treating the documentation as unconditional truth, inferring the user's live configuration without evidence, or creating an unsolicited documentation patch.
