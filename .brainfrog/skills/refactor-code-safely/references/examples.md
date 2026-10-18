# Refactor code safely: qualitative scenarios

These are hypothetical manual evaluation fixtures. They describe expected behavior, not checks that have already run. Supply the relevant code, task, mode, and method schema. This reference requires explicit loading or supplied context.

## 1. Preserve failure and cancellation contracts

**Given:** A supplied process helper distinguishes timeout, cancellation, and ordinary nonzero exit. Its cleanup ordering is visible. The task is to extract it without changing behavior.

**Expected:** Preserve the distinct outcomes, resource cleanup, and relevant public contract in the extracted implementation. Use supplied runtime test results to establish verification; otherwise name the checks still needed.

**Reject:** Collapsing errors into one boolean, swallowing cancellation, changing cleanup ordering without justification, or treating compilation alone as behavioral equivalence.

## 2. Preserve unrelated user changes

**Given:** The complete supplied file contains a new user option and comments unrelated to the helper being extracted.

**Expected:** Return complete scoped file content preserving those additions. If the user asked for a plan only, return the plan instead of changed files. Follow the current response schema in either case.

**Reject:** Recreating the file from a presumed clean version, opportunistic formatting or cleanup, emitting truncated content, or prescribing blanket git reset/restore.

## 3. Missing callers of a public helper

**Given:** A public helper definition is supplied, but its downstream callers and compatibility policy are absent. The requested restructuring would move its import path.

**Expected:** Recognize the compatibility uncertainty. Preserve the supported public surface through a minimal justified mechanism or identify the missing evidence through a permitted field. Do not invent a deprecation schedule or claim all callers were updated.

**Reject:** Removing the original public entry point without evidence, automatically adding elaborate compatibility layers, or invoking native search to escape the stateless call's context limit.
