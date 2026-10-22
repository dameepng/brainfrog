# Phase 1E — Remote Rollout Plan (operator-controlled; nothing here has been executed)

Phase 1E was implemented and validated **locally only**. The checkpoint `41c6f0f` and the Phase 1E branch have not been pushed, and no GitHub-hosted run has happened. Each step below needs explicit operator authorization.

## 0. Before the first push

1. Review the Phase 1E diff. Then create a Phase 1E checkpoint commit on `feat/verification-first-1e-ci`, which is based on `41c6f0f`.
2. Run `actionlint` on `.github/workflows/brainfrog-verify.yml`. It is not installed on the development host, so this has not been run. Optionally run `zizmor` as well.
3. Run the AGENTS.md pre-push checks:
   - `git log origin/main..HEAD -p`;
   - `python -m security.git_guard --range origin/main..HEAD`.

## 1. Bootstrap: why the first PR must not target `main`

The gate reads its verifier, policy and specs from the PR **base** revision. `main` contains none of Phase 1B–1E, so a PR into `main` would correctly be BLOCKED: the trusted checkout has no `policy.json` or `scripts/brainfrog_ci.py`.

**Controlled test procedure:**
1. Push `feat/verification-first-1e-ci` as the trusted base. This is a push only, with no PR into `main`.
2. From that branch, create and push test branches. Open **draft PRs whose base is `feat/verification-first-1e-ci`**:

   | Test PR | Change | Expected `BrainFrog / Verification` |
   | :--- | :--- | :--- |
   | T1 valid | Harmless docs edit | **success** |
   | T2 unit failure | Break `tests/fixtures/browser_app/correct/validation.py` (e.g. accept `not-an-email`) | **failure** (CI-01 FAIL; the browser criteria use `app.js` and should still pass) |
   | T3 blocked | Delete `tests/fixtures/browser_app/correct/tests/test_validation.py` | **not green** (CI-01 BLOCKED, zero tests) |
   | T4 policy weakening | Remove CI-05 from `.github/brainfrog/specs/fixture-login-app.md` | **not green** (trusted spec still used, config change flagged) |
   | T5 workflow edit | Change the enforce step to `exit 0` | **not green** *if* repository rules are configured; otherwise this demonstrates the documented spoofing limit |
   | T6 skipped required job | Add `if: ${{ false }}` to the `Verification` job | The check shows as **skipped, reported as "Success"** (GitHub docs). This is expected to *demonstrate the limit*: it must be **unmergeable only because of code-owner review**. If it is mergeable without a code owner, the rollout fails |
   | T7 invalid evidence | Add a workflow step between `gate` and `enforce` that appends a byte to `reports/<target>/<run_id>/report.md` in the bundle | `enforce` exits 2 (**BLOCKED**, manifest hash mismatch), and the in-band workflow-change flag is also set. This proves tamper detection only for a tamperer who does not also rewrite `manifest.json` (see the evidence-provenance limits) |
   | T8 missing evidence | Delete `tests/fixtures/browser_app/correct` | **not green** (BLOCKED: target missing, so no report) |
   | T9 non-reported check | Rename the `Verification` job | The required check stays **pending**, so the merge is blocked |

   **Every T PR also checks the following:**
   - The `Linux self-test` job must show `test_symlink_in_repository_is_not_followed` as **executed and `ok`**. This is the one required symlink test that could not run locally; a skip fails the job.
   - Treat `Linux self-test` as required during the controlled rollout.

   **Rules for the test PRs:**
   - None targets `main` or any other production branch.
   - The operator creates each one from the operator's own account. External or fork PRs are not part of this rollout.

3. For every run, download the evidence artifact and verify it offline (see [phase1e-evidence-provenance.md](phase1e-evidence-provenance.md)):
   - `tested_sha` equals the run's `GITHUB_SHA`, which is the merge commit;
   - `head_sha` and `base_sha` are correct;
   - `trusted_verifier_revision` equals the base;
   - the manifest hashes match;
   - `--check-report` passes on the report copies;
   - the browser criteria show `renderer_sandbox.verified` at start and end.
4. Confirm that the `Linux self-test` job **executed** the 15 required symlink tests (`selftest-summary.json`, with no `skipped` entries among them) and that the quarantine matched exactly two `StopIteration` errors.
5. Confirm that no step logs show tokens or secrets, that both checkouts used `persist-credentials: false`, and that no packages, images, releases or deployments were published.
6. Optional: set the repository variable `BRAINFROG_ATTEST_EVIDENCE=true`, re-run T1, then check:
   - `gh attestation verify manifest.json --repo dameepng/brainfrog` with the expected signer workflow;
   - that the subject digest equals `sha256(manifest.json)`.

## 2. Repository settings (recommended; not applied by BrainFrog)

| Setting | Value | Why |
| :--- | :--- | :--- |
| Ruleset on `main` (and the release branches): require status check | `Verification`, **expected source: GitHub Actions** | Stops other apps or tokens from satisfying the check with a spoofed status (GitHub: "select an app as the expected source") |
| Require pull request review with **code-owner review** | `CODEOWNERS` covering `.github/**`, `core/ci_gate.py`, `scripts/brainfrog_ci.py`, `docker/browser-runner/**`, `core/runtime/browser_seccomp_chromium.json` | A PR that edits the gate or its policy needs a trusted reviewer, because the workflow itself runs from the PR |
| Actions → Fork pull request workflows | Require approval for outside contributors (at least first-time contributors) | Limits automatic runs of untrusted workflows |
| Actions → Workflow permissions | Default `GITHUB_TOKEN` read-only | Defence in depth; the workflow already sets `contents: read` |
| `Linux self-test` | Optional as a required check | It is diagnostic, because it runs the PR's own tests |

If rulesets or code-owner enforcement are unavailable or not adopted, the PR gate stays **advisory**: a self-modifying PR could produce a green check, either by editing the gate or by skipping the job (T5, T6).

**Exact ruleset for the controlled rollout.** Apply it by hand (BrainFrog changes no settings) to the **test base branch `feat/verification-first-1e-ci`** first, so that blocking can be observed without touching `main`:

| Setting | Value |
| :--- | :--- |
| Target | branch `feat/verification-first-1e-ci` |
| Enforcement | Active |
| Bypass list | Empty; if an emergency bypass is needed, only the repository admin role |
| Rule: require a pull request before merging | 1 approval; dismiss stale approvals on push; **require review from Code Owners**; require approval of the most recent reviewable push |
| Rule: require status checks to pass | `Verification` with source **GitHub Actions**, and `Linux self-test` with source **GitHub Actions** (rollout only); require branches to be up to date |
| Rule: block force pushes | On |
| Rule: restrict deletions | On |

- **`CODEOWNERS` on the base branch** must cover `/.github/`, `/core/ci_gate.py`, `/core/verifier.py`, `/core/runtime/`, `/scripts/brainfrog_ci.py`, `/docker/browser-runner/` and `/core/runtime/browser_seccomp_chromium.json`. The owners must be accounts that do **not** author the test PRs, so that code-owner approval is a real second party.
- **Actions settings:** require approval for all outside collaborators; default `GITHUB_TOKEN` read-only; "Allow GitHub Actions to create and approve pull requests" **off**.
- **Applying the same ruleset to `main`** needs separate approval after the acceptance criteria in §4 are met.
- **Arbitrary untrusted public PRs stay out of scope** until T5 and T6 are shown to be unmergeable without code-owner approval (see the threat model's "Blocker" section).

## 3. Images and registries

- **Python:** `docker.io/library/python@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1`, pulled by digest in CI.
- **Browser runner:** built in each job from `trusted/docker/browser-runner`. The base is `mcr.microsoft.com/playwright/python:v1.64.0-noble@sha256:684df257…`; pip runs with `--require-hashes`. The build needs network access to MCR and PyPI.
- **No registry publication** is part of Phase 1E. Publishing a prebuilt runner image, so it can be pulled by digest instead of rebuilt, is a later option that needs its own approval.

## 4. Acceptance criteria for "DONE — LIVE GITHUB PR VERIFICATION PROVEN"

All of the following must be observed on GitHub:

- [ ] T1 success, T2 failure, T3 non-green, T4 non-green, all with real hosted runs;
- [ ] for each run, the downloaded evidence verifies offline and binds the tested merge-commit SHA;
- [ ] the browser criteria ran real Chromium with the renderer sandbox verified. If AppArmor blocks the sandbox, the result is BLOCKED and the runtime question must be resolved first;
- [ ] the Linux symlink tests executed (no skips);
- [ ] the ruleset with a required `Verification` check from GitHub Actions and code-owner review is configured and observed blocking a merge;
- [ ] the T5 workflow edit and the T6 skipped job cannot be merged without code-owner approval;
- [ ] T7 (tampered bundle) is BLOCKED by `enforce`; T8 (missing evidence) is not green; T9 (renamed job) leaves the required check pending;
- [ ] for every run, `ci-metadata.json` shows `tested_sha` == the run's `GITHUB_SHA`, `trusted_verifier_revision` == `base_sha`, and `workflow_ref`/`workflow_sha` naming `.github/workflows/brainfrog-verify.yml` at the expected commit;
- [ ] `test_symlink_in_repository_is_not_followed` executed (not skipped) in `Linux self-test`;
- [ ] no secrets or write tokens reached untrusted code, and nothing was published or deployed;
- [ ] if attestation is enabled, it was verified independently.
