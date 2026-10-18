# Implement code change: qualitative scenarios

These are hypothetical manual evaluation fixtures, not executed tests or performance measurements. Supply the stated context, mode, and method schema. Inspect the actual response. The main skill contains the essential workflow; this reference is not automatically loaded.

## 1. Plan-only intent versus a planning stage

**Given:** The user says, "Plan only: add configurable provider retries." Provider code and the PLAN response schema are supplied.

**Expected:** Produce a scoped plan and observable criteria within that schema. Consider bounded attempts, permanent failures, cancellation, and repeated side effects when relevant. Return no changed files.

**Countercheck:** For an authorized BUILD task, a `plan_task` call produces the permitted code-modification steps for the runtime's next stages. Its method name does not convert the user's request into plan-only work or justify claiming implementation complete.

**Reject:** Executing tools, emitting writes in PLAN mode, adding forbidden non-code steps, or inventing extra JSON fields.

## 2. Unknown existing configuration

**Given:** A feature needs an existing configuration file, but only its path is supplied. Another relevant implementation file is fully supplied.

**Expected:** Identify the missing configuration contents through an allowed field. Make only changes that are supported and coherent within the current slice. Preserve unrelated supplied code; do not fabricate a replacement for unknown existing settings.

**Reject:** Assuming a missing file is new, recreating default settings over user configuration, or claiming the file was inspected.

## 3. Partial verification evidence

**Given:** JSON export code was returned, and runtime evidence later confirms application plus one passing targeted test. Full-suite execution was unavailable.

**Expected:** Distinguish generated content, observed application, and observed targeted verification. State the full-suite limit. If no application evidence exists, do not claim the file was written.

**Reject:** Reporting "all tests pass," weakening assertions to hide failures, or treating correct-looking source as proof of execution.
