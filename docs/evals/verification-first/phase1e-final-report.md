# Phase 1E — Final Report

## A. Verdict

**LOCAL IMPLEMENTATION COMPLETE — REMOTE CI VALIDATION PENDING**

*Updated by the lint gate of 2026-10-22 07:12–07:20 ([phase1e-local-validation.md](phase1e-local-validation.md) §8).*

**Lint gate passed:**
- actionlint 1.7.12 and zizmor 1.31.1 were both checked before use: release digests, the actionlint checksums file, and verified SLSA provenance via `gh attestation verify`.
- Both now pass on the workflow. actionlint: 0 findings. zizmor: 0 findings under the regular, pedantic and auditor personas, none suppressed.
- One real low-severity finding, `undocumented-permissions`, was fixed by adding comments only. The workflow tests (17/17) and the gate tests (53/53) were re-run and pass.

**Still pending; these are not local gates:**
- the one Docker-dependent required symlink test (`test_symlink_in_repository_is_not_followed`), which is a **mandatory GitHub-hosted Linux gate** and is not counted as passed;
- all real GitHub PR validation.

**Recorded limitation:** actionlint's shellcheck sub-rule did not run, because shellcheck is not installed and was not authorized.

*Previous status, superseded by the lint gate (local sign-off of 2026-10-22 06:55–07:12, §7):* PARTIAL. The regression sign-off was complete; automated workflow linting was pending.

**Implementation:** complete and tested locally:
- the workflow;
- the trusted gate (`core/ci_gate.py`);
- the policy, spec and hash-locked dependencies;
- the evidence bundle and enforcement;
- the self-test quarantine;
- the documentation.

**Regression sign-off: complete.**
- All 13 pending Phase 1B–1D suites ran on the 1E tree: 197 run, 195 ok, 2 Windows symlink skips, 0 failures, 0 errors, 0 timeouts.
- The baseline `test_proof_inspection_ux` shows only the two known `StopIteration` errors.
- **No security regression** was found.

**Pending at §7, now resolved:**
- **Automated workflow linting.** Done in §8 (see the verdict above).
- **One of the 15 required symlink tests**, `test_verifier_docker_integration…test_symlink_in_repository_is_not_followed`. This is still pending: it needs a Docker engine inside a Linux host, so it stays a mandatory gate for the GitHub-hosted `Linux self-test` job.

**Live GitHub validation:** **none occurred.** Nothing was pushed.

**Phase 1E DONE:** **no.**

## B. Worktree

| | |
| :--- | :--- |
| Path | `C:\dame-project\tools\brainfrog-vf-1e-ci` |
| Base | `41c6f0fca5a2a8f464c98bf85de06053f7f9eb45` (Phase 1B–1D checkpoint, unchanged) |
| Branch | `feat/verification-first-1e-ci`: HEAD is still `41c6f0f`, no new commits, nothing staged, 0 intent-to-add entries. The `git status` after sign-off shows 5 modified and 19 untracked entries: the Phase 1E files plus `.brainfrog/workspace.lock`, a test-created runtime lock that was kept and must not be committed |
| Modified | `core/verifier.py`, plus 4 Phase 1D docs (dangling `phase1d-live-evidence` links replaced with local-only wording) |
| New | `.github/workflows/brainfrog-verify.yml`, `.github/brainfrog/{policy.json, specs/fixture-login-app.md, requirements-verifier.txt}`, `core/ci_gate.py`, `scripts/brainfrog_ci.py`, `tests/{ci_gate_helpers.py, test_ci_gate.py, test_ci_workflow_security.py, test_ci_linux_symlinks.py, test_ci_gate_live.py}`, `tests/fixtures/ci_events/*.json` (4), and 8 `phase1e-*.md` docs |
| Protected worktrees | `agentic_dev`, `-1b-closure`, `-1c-execution`, `-1d-browser`: identical to the pre-1E baseline (hashes, status, diff) |

## C. CI architecture

The design is in [phase1e-linux-ci-design.md](phase1e-linux-ci-design.md); the trust model is in [phase1e-threat-model.md](phase1e-threat-model.md).

- **Trigger:** `pull_request` only (opened, synchronize, reopened). There is no `pull_request_target` and no `workflow_run`.
- **Trusted verifier:** `trusted/` is checked out at `pull_request.base.sha` and run as `python -I trusted/scripts/brainfrog_ci.py`. The venv holds only the hash-locked wheels from the trusted revision (`--require-hashes --only-binary=:all: --no-deps`).
- **Candidate:** `candidate/` is checked out at `GITHUB_SHA` (the merge commit). It is data only: it is snapshotted and run inside the 1C/1D Docker sandboxes, and its Dockerfiles and image references are never used.
- **Policy source:** `trusted/.github/brainfrog/policy.json` and the specs it names. If the candidate changes any trusted path, the result is BLOCKED.
- **Isolation:** the Phase 1C/1D Docker sandboxes are unchanged:
  - `--network none`, read-only root, `cap-drop ALL`, `no-new-privileges`, non-root user;
  - the pinned seccomp profile;
  - the browser runs in a separate runner container with renderer-sandbox verification.
- **Permissions:**
  - workflow level: `contents: read`;
  - both checkouts: `persist-credentials: false`;
  - no secrets are referenced, and no token is passed into any container;
  - the attest job alone holds `id-token`, `attestations` and `artifact-metadata: write`. It is gated, has no checkout and runs no scripts.
- **Actions:** all five are pinned to 40-character SHAs verified with `git ls-remote`, and their inputs were checked against each action's `action.yml`:
  - `harden-runner` v2.21.1;
  - `checkout` v7.0.1;
  - `setup-python` v7.0.0;
  - `upload-artifact` v7.0.2;
  - `attest` v4.2.2.

## D. Actual Linux coverage

- **Symlink tests:** **14 of the 15 policy-required symlink tests executed and passed on Linux** (local container, 0 skips among them):
  - the 13 in `test_ci_linux_symlinks` (2026-10-22 06:40);
  - `test_verifier_phase1c_blockers…test_symlink_escape_not_followed` (07:08).

  The remaining one, in `test_verifier_docker_integration`, needs Docker inside a Linux host and is **pending** for the CI self-test job, where a skip counts as a failure. Windows skips are not counted.
- **Container verification:** the live gate Cases A–I ran the real Docker sandbox on this host (Docker Desktop with a Linux VM).
- **Browser verification:** real Chromium ran in the runner container, with the renderer sandbox verified (live Cases A, B and others).
- **Seccomp:** the profile is pinned by hash and checked by the gate. Whether it works on GitHub's `ubuntu-24.04` runners **has not been observed**; the open AppArmor user-namespace risk fails closed to BLOCKED.

## E. CI verdicts

The full table is in [phase1e-pr-gate-contract.md](phase1e-pr-gate-contract.md).

| Outcome | Exit code | When |
| :--- | :--- | :--- |
| **success** | 0 | Every trusted criterion PASS, all required IDs present, the report validates, and verdict and exit code agree |
| **failure** | 1 | Any criterion FAIL in an otherwise valid report |
| **blocked** | 2 | Any criterion BLOCKED or NOT_RUN; zero criteria; a missing ID; missing report; verifier exception; integrity or hygiene failure; identity, policy or image mismatch; a trusted-config change; post-finalization tampering; a gate crash |

Timeouts and cancellations are never success. Missing or incomplete reports are BLOCKED. The decision is derived from the criteria; the reported verdict can only downgrade it.

## F. Evidence

The format is in [phase1e-evidence-provenance.md](phase1e-evidence-provenance.md).

- **SHA binding:** the gate checks that candidate HEAD equals `GITHUB_SHA` and that trusted HEAD equals `base_sha`. It records both, plus `head_sha` and `tested_sha_is_pr_head`.
- **Snapshot:** each target records a `source_snapshot_digest` and `verification_plan_digest`.
- **Metadata:** `ci-metadata.json` (`brainfrog.ci-metadata/1e`) records the workflow run, attempt, ref and SHA, the image identities, the seccomp digest and the Docker version.
- **Hashing:** `manifest.json` (written last) holds the sha256 of every file. Report and screenshot hashes are re-verified after copying, and `enforce` re-hashes the whole bundle.
- **Publication:**
  - only the validated bundle is uploaded (`upload-artifact`, `if: always()`, 14 days, no hidden files);
  - a bundle that fails hygiene or integrity is withheld;
  - nothing is published to registries or releases.
- **Attestation:** implemented, opt-in through `BRAINFROG_ATTEST_EVIDENCE`, for same-repository PRs only. It has **never been executed**.

## G. Security

- **Tokens:**
  - read-only `GITHUB_TOKEN`, credentials not persisted;
  - no `${{ }}` inside any `run:` block (tested);
  - self-test subprocesses run with a scrubbed environment;
  - PR title and ref are inert data, and the title is never recorded.
- **Fork PRs:** they get a read-only token and no secrets. The attestation job is skipped for forks.
- **Workflow self-modification:**
  - A PR can edit the workflow it runs under. In-band detection forces BLOCKED, but a PR that edits the workflow could still turn the check green.
  - Real enforcement needs a ruleset (required check from GitHub Actions) plus code-owner review. Until those are configured, the gate is **advisory**.
  - The verifier and policy are pinned to the base revision; **the workflow is not**.
  - A PR can also add `if: false` to `Verification`. GitHub reports the skipped job as "Success", which does not block a merge even when the check is required (GitHub docs).
  - **No independent enforcement boundary exists yet, so accepting arbitrary untrusted public PRs is blocked.** The first rollout is limited to operator-created test PRs against `feat/verification-first-1e-ci`; the skip case is T6 in the rollout plan.
- **Spec weakening:** the trusted spec is always the one applied, and candidate changes force BLOCKED (live Case H).
- **Scans:** `git_guard` found 0 findings across 26 files. The private-path grep matched only synthetic or rule text. `git diff --check` is clean.
- **Images:**
  - Python is pulled by registry digest;
  - the browser runner is built from the trusted recipe after a hash check, from a digest-pinned base, with pip `--require-hashes`;
  - PR-supplied image references are not accepted;
  - no local pulls were performed.

## H. Tests

Exact commands, timestamps and durations are in [phase1e-local-validation.md](phase1e-local-validation.md).

**Phase 1E suites:**

| Suite | Result |
| :--- | :--- |
| `test_ci_gate` | 53/53 on Windows and on Linux |
| `test_ci_workflow_security` | 17/17 on Windows and on Linux |
| `test_ci_linux_symlinks` | 13/13 executed on Linux; 13 skipped on Windows (not counted) |
| `test_ci_gate_live` | 8/8, real Docker and Chromium |

**Regressions on the 1E tree:**
- `test_standalone_verifier`: 21 OK;
- `test_verifier_evidence_integrity`: 18/18.

**Sign-off of the 13 previously pending suites** (2026-10-22, one process each, bounded runner; §7.1 of local-validation):

| Suite | Run | Result |
| :--- | -: | :--- |
| `test_verifier_execution_integrity` | 17 | pass |
| `test_verifier_test_result_semantics` | 8 | pass |
| `test_verifier_sandbox_execution` | 5 | pass |
| `test_verifier_phase1c_blockers` | 29 | pass, 1 skip |
| `test_verifier_docker_integration` | 11 | pass, 1 skip |
| `test_verifier_browser_plan` | 19 | pass |
| `test_verifier_browser_security` | 32 | pass |
| `test_verifier_browser_evidence` | 18 | pass |
| `test_verifier_browser_integration` | 13 | pass |
| `test_phase6b_cli_verification` | 15 | pass |
| `test_cli_workspace_verification` | 7 | pass |
| `test_runtime_boundary` | 3 | pass |
| `test_git_guard` | 20 | pass |

All 13 exited 0, with no timeouts, for **197 run, 195 ok, 2 skipped**. The lowest available memory was 2490 MB.

**Remaining skips:** the two Windows symlink-privilege skips above. One was executed on Linux; the other is pending for CI. The 13 Windows skips in `test_ci_linux_symlinks` were all executed on Linux.

**Known baseline errors:**
- `test_proof_inspection_ux` on the 1E tree: exit 1, 32 run, **2 errors**.
- They are exactly the two `test_11_repl_proof_aliases` IDs, with `StopIteration`, the same as on clean `c78e798`.
- They are quarantined by exact ID and signature in CI, and are not counted as passing.

**New regressions:** **none.**

**Workflow linting (2026-10-22, §8):** passes.
- actionlint 1.7.12: exit 0, no findings. Negative control: it catches an injected untrusted expression and an unknown permission scope, and it accepts `artifact-metadata`.
- zizmor 1.31.1 (`--offline`; regular, pedantic and auditor personas): exit 0, no findings.
- The one initial finding (`undocumented-permissions`, low) was fixed with comments. After that, `test_ci_workflow_security` passed 17/17 and `test_ci_gate` passed 53/53.
- shellcheck was not run (not installed).

## I. Remote rollout

The full procedure is in [phase1e-remote-rollout.md](phase1e-remote-rollout.md).

1. **Before the first push** (minimum steps):
   1. Done locally: actionlint and zizmor pass (§8). The same pinned versions can be re-run in CI later if wanted.
   2. The operator reviews the diff and authorizes a Phase 1E commit.
      - Do not stage `.brainfrog/workspace.lock`, `.brainfrog/reports/**` or `phase1d-live-evidence/`.
      - Before committing, run `git diff --cached`, `python -m security.git_guard --staged` and `git diff --check`.
   3. Run the AGENTS.md pre-push scans: `git log origin/main..HEAD -p` and `python -m security.git_guard --range origin/main..HEAD`.
2. **Push without a PR into `main`:** push `feat/verification-first-1e-ci` as the trusted base. Then open draft test PRs T1–T5 **against that branch**.
3. **Settings (by the operator):**
   - a ruleset that requires `Verification` with GitHub Actions as the expected source;
   - code-owner review on `.github/**` and the gate files;
   - fork-workflow approval;
   - a read-only default token.
4. **Images:** the runner needs network access to Docker Hub (Python by digest), MCR (Playwright base) and PyPI (hashed wheels). No registry publication is involved.
5. **Live DONE requires:**
   - T1 green; T2 FAIL; T3 and T4 not green;
   - the evidence verified offline;
   - real Chromium with the renderer sandbox verified;
   - Linux self-test with no skips;
   - the ruleset observed blocking;
   - T5 not mergeable without code-owner approval;
   - any attestation verified independently.

## J. Next phase

**Remaining for Phase 1E:**
No local gates are pending.
1. An authorized commit and push.
2. Live GitHub validation (rollout §4), including the one Docker-dependent required symlink test in the `Linux self-test` job. It must actually execute there; a skip counts as a failure.

**Phase 1F must remain blocked** until Phase 1E reaches "DONE — LIVE GITHUB PR VERIFICATION PROVEN".
