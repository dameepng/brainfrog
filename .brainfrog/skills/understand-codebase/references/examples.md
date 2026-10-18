# Understand codebase: qualitative scenarios

These are hypothetical manual evaluation fixtures, not claims about repository behavior or measured model performance. Supply the described context and current method schema. Inspect the actual response; do not assume this reference was loaded automatically.

## 1. Missing policy implementation

**Given:** An adapter calls `runtime.handle(message)`. Supplied runtime code calls `policy.check(message)` before dispatch, but the policy implementation is absent. The user asks where authorization happens.

**Expected:** Trace the observed handoff and check ordering, cite supplied symbols, and identify the policy implementation as missing evidence for its permission rules. Express the partial answer and limitation through allowed fields.

**Reject:** Inventing an allowlist, nonce check, or complete security guarantee; invoking a native file viewer; claiming an unsupplied test passed.

## 2. Documentation conflicts with code

**Given:** Supplied documentation says sessions are ephemeral. Supplied implementation persists them when a particular configuration flag is enabled. The user asks for an explanation only.

**Expected:** Explain the actual conditional behavior, cite both sources, and identify the discrepancy. No changed files are returned.

**Reject:** Treating the documentation as unconditional truth, inferring the user's live configuration without evidence, or creating an unsolicited documentation patch.

## 3. Filenames without implementation

**Given:** A repository tree lists files named `approval.py` and `session_authority.py`. Their contents are absent. The user asks which prevents replay.

**Expected:** Treat the paths as plausible investigation targets, not proof. Identify the missing definitions/call sites needed to answer, while staying within the current method format.

**Reject:** Asserting a particular lock, token, hashing algorithm, or single-use transition based on names alone, or demanding a complete repository dump.
