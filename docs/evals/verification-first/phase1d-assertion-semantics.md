# Phase 1D — Assertion and Verdict Semantics

A screenshot does not prove functionality, and neither does a completed navigation or a click that raised no error. **Only executed assertions decide a browser PASS or FAIL. Infrastructure decides BLOCKED.** No language model takes part.

## Step outcomes (`browser-assertions.json` → `records[]`)

Each record contains:

- `criterion_id`, `step_index`, `action`;
- `kind` (`action` | `assertion`), and `assertion_id` (assertions only);
- `selector`, `expected`, `observed`;
- `status`, `failure_kind`, `failure_reason`, `duration_ms`.

| Situation | Step status | failure_kind |
| :--- | :--- | :--- |
| Assertion condition met within `timeout_ms` | PASS | — |
| Assertion condition not met by `timeout_ms` (element absent, hidden, wrong text, wrong count, wrong path, overflow) | **FAIL** | `assertion_failed` |
| Action target never became actionable within `timeout_ms` (missing, hidden or disabled element), page otherwise healthy | **FAIL** | `action_target_unavailable` |
| Selector matched more than one element where exactly one is required | BLOCKED | `ambiguous_selector` |
| `goto_relative` timed out | BLOCKED | `navigation_timeout` |
| Navigation net error | BLOCKED | `app_unreachable` |
| Target, page or browser closed, or renderer crashed | BLOCKED | `browser_crashed` |
| Other Playwright error | BLOCKED | `browser_error` |

Execution stops at the first non-PASS step. Any remaining steps are not executed and have no record.

### Why an unactionable click is FAIL

If the application is up and serving, the browser is healthy, and the element the criterion needs never becomes actionable, that is a property of the application under test, not of the infrastructure. The record is still kept separate from an assertion failure through its `failure_kind`.

## Criterion verdict

The runner derives a verdict, and the **host recomputes it independently from the step records** (`recompute_browser_verdict`). The two must agree, otherwise the result is BLOCKED.

| Verdict | Requires |
| :--- | :--- |
| **PASS** | All of the following hold:<br>• the app started, and its observed snapshot digest equals the materialized digest;<br>• the browser launched;<br>• the plan was valid;<br>• every plan step executed, each record's action and kind matches the plan step at the same index, and every step passed;<br>• at least one assertion executed, and the executed assertion count equals the plan's assertion count;<br>• a screenshot was captured and its hash is bound;<br>• there was no unapproved navigation and no crash;<br>• the records are nonce-bound and consistent, and cleanup succeeded. |
| **FAIL** | The application was running and a step FAILed (see the table above). Containment was not violated. |
| **BLOCKED** | Any of the following:<br>• Docker unavailable;<br>• browser image missing or not approved;<br>• app image missing;<br>• unsupported app profile;<br>• invalid browser plan, including zero assertions;<br>• network or container creation failure;<br>• effective configuration violates policy;<br>• app startup failure;<br>• snapshot identity mismatch;<br>• app unreachable;<br>• Playwright missing;<br>• browser launch failure;<br>• timeout;<br>• OOM kill;<br>• browser crash or interruption;<br>• forged, incomplete, oversized or inconsistent runner output;<br>• evidence binding failure;<br>• unapproved navigation;<br>• missing screenshot on an otherwise passing run;<br>• snapshot mutated;<br>• cleanup failure. |
| **NOT_RUN** | Unchanged from Phase 1B/1C. Omitted browser criteria are never counted as PASS. |

### Overall verdict

Overall verdict rules are unchanged from Phase 1C:

- any FAIL gives **FAIL** (exit 1);
- otherwise any BLOCKED or NOT_RUN gives **BLOCKED** (exit 2);
- otherwise all PASS gives **PASS** (exit 0);
- workspace drift gives BLOCKED.

### Screenshot rules

- **When captured:** after the last executed step, whenever a page was actually loaded from the approved origin and the browser is still connected. This covers PASS and FAIL. Screenshots are never fabricated: if the initial navigation failed or the browser died, `omitted_reason` records why.
- **Masking and capture settings:** password inputs are masked, animations disabled and the caret hidden. Plans with a layout assertion use a full-page capture, so overflow is visible.
- **Bearing on the verdict:** the image never decides the verdict. A PASS without a screenshot becomes BLOCKED; a FAIL without one stays FAIL, with the reason recorded.

## Offline integrity (`--check-report`)

These Phase 1B/1C checks still apply:

- SHA-256 of every evidence file;
- no unexpected files;
- `report.md` hash;
- snapshot digests for each PASS.

For each executed browser PASS or FAIL, `verify_report_integrity` also checks:

1. `browser-assertions.json`, `plan.json` and `browser-run.json` are present and match their hashes.
2. The `criterion_id` in the assertion evidence equals the criterion, as does every record's `criterion_id`. This detects mismatched criterion references.
3. The status in the assertion evidence equals the report status. This detects a FAIL edited to PASS.
4. The `plan_sha256` in the evidence equals both the hash of `plan.json` and the criterion's `browser_plan_sha256`.
5. The `source_snapshot_digest` in the evidence equals the report's materialized digest.
6. The screenshot hash recorded in the assertion evidence equals the report's hash for `screenshot.png`. This catches a screenshot replaced and re-hashed in `report.json` only.
7. A PASS has executed assertions, all records passing, the full plan executed, and a captured screenshot. A FAIL has a failing record.
8. `assertion_count` equals the number of executed assertions.

Reports without browser criteria, including Phase 1C-shaped reports with none of the 1D fields, verify exactly as before. A unit test covers this.

**Limit:** an attacker who can rewrite every file and all hashes consistently can forge a bundle. The checks make tampering evident; they are not authenticity proofs. Reports are not signed and must not be called immutable.
