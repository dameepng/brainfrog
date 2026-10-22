# Phase 1E — T1 Live GitHub Retest (after the Linux portability hotfix)

**Verdict: T1 LIVE GITHUB RETEST — BLOCKED** (2026-10-22)

**Cause.** The run's trusted side was the **pre-hotfix** revision `1c6ac7d`, not the corrective commit. The `pull_request` event carried a stale `base.sha`, so the hotfix's trusted verifier and policy were never exercised. The gate failed closed, as designed.

The original T1 result ([phase1e-t1-live-results.md](phase1e-t1-live-results.md)) and the remediation record ([phase1e-t1-hotfix.md](phase1e-t1-hotfix.md)) are unchanged. Phase 1E is **not** DONE.

## 1. What was published

| Item | Value |
| :--- | :--- |
| Corrective commit | `82af8715fa4c9269d631491643b3dff3524e22e3` (parent `1c6ac7dfd88013612efeb7bc9e27fad06bfa4cf3`) |
| Remote `feat/verification-first-1e-ci` | fast-forwarded `1c6ac7d..82af871`; verified with `ls-remote` |
| Trigger commit on `test/brainfrog-1e-t1-pass` | `ac86892d08e2f5ad2ccb7bc32fa93c11cac4ada1`: empty, parent `ce89f8c` |
| PR #45 | open, draft, base `feat/verification-first-1e-ci`; 1 changed file (`tests/fixtures/browser_app/correct/tests/test_validation.py`, +7) |

`main` (`b41eee2`) and all other branches, rulesets, settings and secrets are unchanged.

## 2. The new run

| Field | Value |
| :--- | :--- |
| Run | `39731332404`: https://github.com/dameepng/brainfrog/actions/runs/39731332404 (event `pull_request`) |
| PR head | `ac86892` |
| Synthetic merge (`github_sha` / `tested_sha`) | `dba488b0a61b422e8398473f3f3e53683e32a2cf`, with parents `82af871` (new base) and `ac86892` |
| Event `base.sha` and trusted checkout | **`1c6ac7d`**: stale. The PR API still reported `base.sha` = `1c6ac7d` after the synchronize event |
| `trusted_verifier_revision` | `1c6ac7d` (expected `82af871`) |
| `trusted_policy_digest` | `4a44e37ac59c…` (required `475a1446b1e5…`): **not met** |
| `runtime_platform` | not recorded: the pre-hotfix trusted code has no such field. The runner was GitHub-hosted Linux (Python 3.12.15, Docker 28.0.4, linux/amd64) |
| Overall check | `Verification`: failure; `Linux self-test`: failure; attestation skipped. Neither is reported as success |

## 3. Verification job (trusted `1c6ac7d`, candidate `dba488b`)

The decision was **BLOCKED** (exit 2), for two reasons:
1. `candidate modifies trusted CI configuration (.github/brainfrog/policy.json)`. The merge contains the hotfix policy, but the stale trusted base doesn't, so the gate refused to let the candidate certify its own policy. This is correct fail-closed behaviour.
2. `CI-01=BLOCKED, CI-02=BLOCKED`. The **old** trusted `stage_harness` still produced `python: can't open file '/bf-harness/verification_harness.py': [Errno 13] Permission denied`, the T1 defect itself, because the trusted side carried no fix.

CI-03 to CI-06 (browser) passed.

## 4. Linux self-test job (candidate hotfix code, but under the old trusted policy)

These are genuine GitHub-hosted Linux executions of the hotfix **candidate** code. They are not a trusted-gate pass: the module list, required tests and quarantine came from the old policy.

| Area | Result on GitHub Linux |
| :--- | :--- |
| Fix 1: harness readable, not writable | `test_verifier_docker_integration` **12/12 ok**, 0 skipped. This includes `test_staged_harness_is_readable_but_not_modifiable_by_container_user`, run through the real `DockerSandboxBackend` as uid 65532, and `test_symlink_in_repository_is_not_followed` |
| Fix 2: host-independent traversal | `test_verifier_execution_integrity` **18/18 ok**, including `test_05` and `test_05b` |
| Fix 3: platform-scoped quarantine | `test_ci_gate` **60/60 ok**, including the 4 real-subprocess platform tests. Both `test_11_repl_proof_aliases` pass normally on Linux. The old trusted quarantine (no platform scope) then reported them as "no longer errors", exactly the T1 defect the hotfix removes |
| Symlinks | `test_ci_linux_symlinks` 13/13 ok; `test_verifier_phase1c_blockers` 28 ok + 1 skip |
| Not run | `test_sandbox_harness_staging`: it is not in the old trusted module list |
| **Failures** | `test_phase6b_cli_verification`: **2 FAIL**, `test_11_realistic_e2e_cli_temporary_workspace` and `test_14_proof_persistence_failure_reports_committed_and_requires_recovery` |

**The phase 6B failures are a newly identified, separate defect.** Both tests run `sys.executable -m pytest …`, but the CI's hash-locked verifier environment (`requirements-verifier.txt`) does not include pytest:
- `test_11` fails at `assertEqual(proc.returncode, 0)` on that direct pytest call;
- `test_14`'s pytest-based verification fails, so the transaction rolls back (`rolled_back`).

The identical failures (same lines) occurred in run `39705364940`. The original T1 report attributed them to the harness defect; that attribution was **incomplete**. They are an undeclared test dependency in CI, independent of the hotfix. The T1 report is preserved as written; this record supersedes that one row.

## 5. Evidence integrity

| Check | Result |
| :--- | :--- |
| Evidence artifact | `brainfrog-ci-evidence-39731332404-1` (ID 12404679954, 88,588 bytes, sha256 `c7c9d802…`), 44 files |
| Self-test logs artifact | `brainfrog-selftest-logs-39731332404-1` (ID 12405123985, sha256 `163a6c82…`) |
| `manifest.json` sha256 | recomputed locally as `51a13c6a32328feda95c00521796e1406928d7dc014b36bc6a2812881d33266e`, equal to the value the run logged |
| `verify_evidence_bundle` (local trusted code; bundle treated as data) | **0 findings**; decision `blocked`, exit 2 |
| `cli.py --check-report` | "Report integrity verified" |
| Provenance | unsigned CI context; attestation not enabled |

## 6. Root cause of the BLOCKED retest, and the minimum fix (not applied)

GitHub built the merge ref against the current base tip (`82af871`). The event payload's `pull_request.base.sha` still named `1c6ac7d`, because pushing to the base branch does not refresh it, and neither did the synchronize. The workflow checks out the trusted side at `github.event.pull_request.base.sha`, so trusted and tested no longer correspond: the trusted revision was not the merge's first parent.

Security impact: none. The run was fail-closed, and the provenance fields (`base_sha`, `trusted_verifier_revision`, `tested_sha`) truthfully show the mismatch. It does, however, mean a base-branch update cannot be validated by re-syncing an existing PR.

Options, each needing separate authorization:
- **Operational (no code change):** refresh `base.sha` by editing PR #45's base (retarget it away and back, which emits an `edited` event and recomputes `base`). Then trigger a fresh `synchronize`, or open a new test PR from a branch based on `82af871`. Whether a close and reopen refreshes `base.sha` is unverified, so it is not recommended as the method.
- **Hardening (workflow change, needs a re-lint):** check out the trusted side at the tested merge's first parent, or have the gate require `trusted_verifier_revision == first-parent(tested_sha)` and BLOCK with an explicit "stale base" reason otherwise.
- **Phase 6B:** declare and hash-lock pytest for the self-test environment, or make those two tests skip with a recorded reason when pytest is absent. They must still not count as passing.

## 7. Local validation status

As recorded in phase1e-t1-hotfix.md §4–§5: every targeted Windows suite passes; the local Linux harness proof covers cases A–E; the Linux container run is 25/25; `test_standalone_verifier` is 21/21. **No local tests are pending.** The full `DockerSandboxBackend` run on a Linux host with the hotfix as the *trusted* side is still unproven.
