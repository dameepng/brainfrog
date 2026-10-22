# Phase 1E — Pull Request Gate Contract

**Check identity:** workflow `BrainFrog`, job `Verification`. GitHub shows it as "BrainFrog / Verification"; the required-check name is `Verification`. Keep both names stable once the check is required.

## Identity checks (any failure → BLOCKED; nothing executes)

| Field | Source | Rule |
| :--- | :--- | :--- |
| `event_name` | `GITHUB_EVENT_NAME` | Must be `pull_request` |
| `repository` | `GITHUB_REPOSITORY` | `owner/name` format |
| `pull_request_number` | event `number` | Positive integer |
| `head_sha`, `base_sha` | event `pull_request.head/base.sha` | 40-hex. `base_sha` is recorded as given; it does not select trusted code |
| `github_sha` | `GITHUB_SHA` | 40-hex |
| `tested_sha` | `git rev-parse HEAD` in `candidate/`, run with hooks and fsmonitor disabled and system/global config ignored | Must equal `GITHUB_SHA` |
| `trusted_base` | `core/ci_trust.py` over `candidate/` and `base/` | `merge_parents` (exactly 2; the second is `head_sha`), `trusted_base_sha` (the first parent, on the base branch of this repository), `base_branch_tip`, `event_base_sha_is_stale` (diagnostic) |
| `trusted_verifier_revision` | `git rev-parse HEAD` in `trusted/` | Must equal `trusted_base.trusted_base_sha` |
| `workflow_ref`, `workflow_sha`, `workflow_run_id`, `workflow_run_attempt` | `GITHUB_*` | Recorded; run id and attempt must be integers |

`tested_sha` is normally GitHub's **merge commit**, not the PR head. The bundle records both, plus `tested_sha_is_pr_head`, and never claims the head was tested when it was not.

## Verdict mapping

| Situation | Outcome | Exit code |
| :--- | :--- | :--- |
| Every criterion in the trusted spec is PASS, all required IDs are present, the report verifies, verdict and exit code agree | **success** | 0 |
| Any criterion FAIL, in a report that otherwise validates | **failure** | 1 |
| Any criterion BLOCKED or NOT_RUN; zero criteria; a required ID missing | blocked | 2 |
| Report missing; verifier exception; integrity or hygiene failure; report not built from the trusted spec | blocked | 2 |
| Report says PASS while its criteria say otherwise; process exit code ≠ report exit code | blocked | 2 |
| Identity mismatch; policy missing or malformed; Docker or an approved image missing; recipe or seccomp hash mismatch | blocked | 2 |
| Candidate changes a trusted path (the workflow, or anything under `.github/brainfrog/`) | blocked; evidence is still produced | 2 |
| Evidence bundle edited after finalization; extra, missing or linked files; decision ≠ manifest | blocked | 2 |
| Gate crash | blocked, with a minimal diagnostic bundle | 2 |
| Job timeout or cancellation | GitHub job conclusion (never success) | — |

The decision is **derived from criteria** in a validated report. `overall_verdict` and the process exit code are cross-checks only, and they can downgrade a result but never upgrade it.

## Trusted acceptance policy (MVP)

- Required criteria come from `trusted/.github/brainfrog/policy.json` and the specs that policy names. Only the base revision counts.
- The candidate's copies of those files are compared with the trusted copies:
  - **Any difference** is reported in `candidate_trusted_config_changes` and forces BLOCKED. A PR therefore cannot weaken its own criteria, and a legitimate policy change must be reviewed and merged first. Live Case H shows a PR that deletes a criterion and lowers `required_criteria`: the trusted spec was still evaluated in full and the run was BLOCKED.
  - **Additional checks** proposed by a PR take effect only after merge. The MVP has no "advisory extra criteria" mode.
- Specs can never choose images, mounts, Docker flags, trust mode or the application origin (1C/1D). Browser plans with no assertions, screenshot-only plans and unsupported profiles stay BLOCKED (1D, plus the live unsupported-profile case).

## Known baseline errors

The two `test_11_repl_proof_aliases` errors are **not** part of the required gate. They live only in the `Linux self-test` job, under a strict quarantine in the trusted policy:
- only those two exact test IDs may ERROR;
- their last traceback line must start with `StopIteration`;
- any other ERROR or FAIL, a changed signature, or one of them *passing* fails the job until a maintainer updates the policy;
- a module that runs zero tests fails.

The full-suite count is never reported as passing.
