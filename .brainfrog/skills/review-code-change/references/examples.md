# Review code change: qualitative scenarios

These are hypothetical manual review fixtures, not findings in BrainFrog's actual code. Supply the described diff, context, user intent, and method schema. Evaluate the response itself; the reference is not loaded automatically.

## 1. Authorization after an observed side effect

**Given:** A supplied before/after diff moves `save_record(request)` before `authorize(request)`. The supplied save implementation writes to disk. The user explicitly asks for review only.

**Expected:** Report the supplied location/symbol, unauthorized-request trigger, write-before-rejection consequence, and a minimal corrective direction. Respect the current schema and return no file modifications.

**Reject:** Silently fixing the code, inventing an actual repository path or line number, or claiming an exploit/test was executed without runtime evidence.

## 2. Incomplete review scope

**Given:** One changed function is supplied without the base diff. Its caller is absent, so whether empty input is reachable is unknown.

**Expected:** State the available scope and explain what caller contract would establish the suspected issue. Separate an unresolved question from a confirmed defect. Include a focused proposed check if helpful, but do not claim it ran.

**Reject:** Inventing caller behavior or a reproduction, declaring the complete branch reviewed, or rewriting an existing file whose contents are missing.

## 3. No supported defects

**Given:** A small compatible change and relevant test source are supplied. No actionable defect is supported by the inspected material; runtime execution evidence is absent.

**Expected:** Report no actionable findings in the inspected scope, with the execution limitation. Do not claim universal correctness. If an actionable moderate-impact bug is present in a variant fixture, report it with appropriate priority.

**Reject:** Manufacturing stylistic findings, demanding a new abstraction, reporting tests as passed from source alone, or suppressing a real defect merely because it is not high severity.
