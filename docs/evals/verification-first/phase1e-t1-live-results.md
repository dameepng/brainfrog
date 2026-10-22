# Phase 1E — T1 Live GitHub Validation Results

## Verdict: **T1 LIVE GITHUB VERIFICATION — BLOCKED**

- T1 was meant to show a genuine PASS on GitHub-hosted runners. It didn't: the gate returned **BLOCKED** (exit 2) and the check is red.
- The block comes from a real **Linux-host portability defect** in the Phase 1C Docker backend: the verification harness is not readable inside the container. The gate failed closed as designed.
- What T1 *did* prove:
  - genuine GitHub-hosted execution;
  - correct merge-commit SHA binding;
  - trusted-base sourcing;
  - real Chromium criteria passing with the Chromium sandbox active;
  - a verifiable evidence artifact;
  - the previously pending Docker-dependent symlink test **executing and passing**.

Phase 1E is **not DONE**. Independent merge enforcement is **not** proven: there is no CODEOWNERS file, no ruleset, and only one collaborator.

## 1. Identities

| Item | Value |
| :--- | :--- |
| Test PR | https://github.com/dameepng/brainfrog/pull/45 (draft, open, **not merged**). Base `feat/verification-first-1e-ci`, head `test/brainfrog-1e-t1-pass` |
| Workflow run | https://github.com/dameepng/brainfrog/actions/runs/39705364940 (attempt 1, event `pull_request`, 2026-10-22 01:23:08Z–01:25:06Z) |
| PR head SHA | `ce89f8c41e56783fbaf76b5e6843881ec65bcd54` (T1 commit; parent `1c6ac7d`) |
| Base SHA | `1c6ac7dfd88013612efeb7bc9e27fad06bfa4cf3` |
| `GITHUB_SHA` = tested SHA = candidate checkout HEAD | `777e4ac300bbc359c07f6f31df30eace086c3799`: GitHub's synthetic merge commit, whose parents are `1c6ac7d` and `ce89f8c` (verified through the API). `tested_sha_is_pr_head: false` is recorded correctly |
| Workflow identity | `workflow_ref` `dameepng/brainfrog/.github/workflows/brainfrog-verify.yml@refs/pull/45/merge`, `workflow_sha` `777e4ac…`. The workflow file is unchanged from base (sha256 `2591d39c…`, the linted version) |
| Trusted verifier revision | `1c6ac7d` (trusted HEAD == `base_sha`, enforced) |
| Trusted policy digest | `4a44e37ac59c0289bfb671c56acb5711c8768baeafbd27661d2d444032dd53fc` |
| Trusted spec sha256 | `16959670d119a4c6fe158ebe0a49573a1444ccb6b148c256a403bfc082602212` |
| Candidate trusted-config changes | none |
| Source snapshot digest | `494b6182d1f676f692133bb81cd1c2abc54749dd0faf28a1edb1db54b5d117fe` |

**T1 candidate change:** two synthetic unit tests added to `tests/fixtures/browser_app/correct/tests/test_validation.py`, which trusted CI-01 exercises. There were no workflow, policy, verifier, gate or Docker-policy changes.

## 2. Image provisioning (real, on the runner)

- **Python:** `python@sha256:a6e34c59…` was pulled by digest (exit 0) and tagged `python:3.12-slim`. Its ID is `sha256:df18428d…`, and `repo_digests` contains the pinned digest.
- **Browser runner:** built from the trusted recipe in about 38 s (exit 0).
  - Base: `mcr.microsoft.com/playwright/python:v1.64.0-noble@sha256:684df257…`.
  - Built ID `sha256:671857c4…`; Dockerfile and lock hashes match the policy.
- **Seccomp profile digest:** `569408de…`.
- **Docker Engine:** 28.0.4.

## 3. Job conclusions

| Job | Conclusion | Detail |
| :--- | :--- | :--- |
| `Verification` | **failure** | `gate` exit 2 (BLOCKED); digest recorded; artifact uploaded; `enforce` re-validated the bundle and exited 2 |
| `Linux self-test` | **failure** | Strict-quarantine problems (§5) |
| `Attest evidence manifest (optional)` | skipped | `BRAINFROG_ATTEST_EVIDENCE` is not set. **Attestation was not tested** |

## 4. Verification job: criterion results

| Criterion | Status | Evidence |
| :--- | :--- | :--- |
| CI-01 unit tests (Python container) | **BLOCKED** | stderr: `python: can't open file '/bf-harness/verification_harness.py': [Errno 13] Permission denied` |
| CI-02 compileall (Python container) | **BLOCKED** | same error |
| CI-03 login form visible | **PASS** | real Chromium 156.0.8078.4, Playwright 1.64.0, `chromium_internal_sandbox: true`; 3/3 assertions |
| CI-04 valid submission → dashboard | **PASS** | 3/3 assertions |
| CI-05 | **PASS** | 3/3 assertions |
| CI-06 | **PASS** | 2/2 assertions |

There was no trust-local fallback: every criterion ran through the Docker backends. The decision was derived from the criteria, and BLOCKED became non-success.

The open AppArmor user-namespace risk (`phase1e-linux-ci-design.md`) **did not occur**: the Chromium sandbox started on `ubuntu-24.04`.

### Root cause (real CI defect)

`core/runtime/sandbox.py`, `stage_harness()`:
- It creates the harness directory with `tempfile.mkdtemp(prefix="bf_harness_")`. On Linux that is **mode 0700, owned by the runner user (uid 1001)**.
- That directory is bind-mounted read-only at `/bf-harness` in a container running as **uid 65532**, which cannot traverse it.
- The source snapshot does not fail because its mount is a `source/` *subdirectory* created with normal permissions.
- Docker Desktop on Windows does not enforce bind-mount POSIX permissions. That is why every local run, including the local Linux-container tests that never staged a harness, passed.

The browser runner does not use `/bf-harness`, so CI-03 to CI-06 were unaffected.

## 5. Linux self-test (GitHub-hosted, candidate code on the runner)

The previously pending required test **`tests.test_verifier_docker_integration.TestDockerIntegration.test_symlink_in_repository_is_not_followed` executed and returned `ok`**.
- It was not skipped.
- No Docker socket was mounted into a test container, and nothing was privileged.
- **All 15 policy-required symlink tests passed on Linux:** the 13 in `test_ci_linux_symlinks`, plus the two existing ones. The selftest reported no "required test … did not pass" problem.

Selftest problems that failed the job:

| Problem | Classification |
| :--- | :--- |
| `test_verifier_docker_integration`: 6 FAIL (`runner_not_started`, "harness never started", BLOCKED ≠ PASS) | Same harness-permission defect as §4 |
| `test_phase6b_cli_verification`: `test_11` (BLOCKED ≠ PASS), `test_14` (`rolled_back`) | Same defect: verification BLOCKED → rollback path |
| `test_verifier_execution_integrity.test_05_path_traversal_in_command_arguments_rejected`: `..\..\windows\system32` accepted | **Host-dependent policy.** `validate_verification_command` resolves the token with host path rules, and on Linux a backslash is not a separator. Inside the Linux verification container the token is a literal filename, so there is no actual escape. Still, the policy differs by host OS, and it should reject both separators |
| `test_proof_inspection_ux`: both quarantined `test_11_repl_proof_aliases` **pass** on Linux | The quarantine worked as designed: an unexpected pass fails the job. The `StopIteration` baseline is **Windows-only**, and the trusted quarantine needs updating for Linux |

Every other module passed, including `test_ci_gate` 53, `test_ci_workflow_security` 17, `test_standalone_verifier` 21, `test_verifier_browser_*` and `test_git_guard` 20. `test_junction_escape_not_followed` skipped, which is expected because it is Windows-only.

## 6. Evidence artifact: independent check (local, after download)

- **Artifact:** `brainfrog-ci-evidence-39705364940-1`, 88,490 bytes, GitHub digest `sha256:fbcf1249…`, retained until 2026-11-05.
- **Manifest digest:**
  - recorded in the job log: `2be5d375b3e0a1807f31f1d6f9709937ceedd82a486337a2ed3cb5586ad1efb0`;
  - recomputed locally over the downloaded `manifest.json`: the same value.
- **Bundle check:** `verify_evidence_bundle` from the local trusted code (the bundle was treated as data) found **0 findings**: exact file set, all hashes match, no links, and decision == manifest outcome (`blocked`, exit 2).
- **Report check:** `cli.py --check-report` on the report copy printed "Report integrity verified".
- **Privacy:** no user-profile paths (only `/home/runner`), and no token or private-key patterns in the evidence, self-test logs or run log. Browser `fill` values are not recorded.

## 7. Token and container security observations

- `GITHUB_TOKEN`: `Contents: read` and `Metadata: read` only. The repository has 0 secrets and 0 variables, and no secret is referenced.
- Every checkout used `persist-credentials: false`.
- **Python verification containers:**
  - `privileged: false`, `network_mode: none`, `readonly_rootfs: true`;
  - user `65532:65532`, `cap_drop: ALL`, `no-new-privileges`;
  - the only bind mounts are the read-only snapshot and the harness (both in BrainFrog temp directories).
- **Publications:** no images or packages, releases or tags. `ci.yml` did not run, because it is limited to `main`. The only upload was the two workflow artifacts.

## 8. Minimum targeted fix (not applied; it needs a reviewed change to the trusted base)

1. **Harness readability (blocking):** in `stage_harness()`, make the staged harness readable by the unprivileged container user. For example, after writing the file: `os.chmod(harness_dir, 0o755)` and `os.chmod(file, 0o644)`; or stage it in a normally created subdirectory, as the snapshot does.
   - The harness is public BrainFrog code mounted read-only, so this exposes nothing.
   - Add a Linux test that asserts the staged harness is readable by "other", plus a local Linux reproduction.
2. **Traversal check (hardening):** in `validate_verification_command`, normalize `\` to `/` (or check both separators) before resolving, so that the policy is host-independent. Keep `test_05` unchanged.
3. **Quarantine (trusted policy):** scope the `test_11_repl_proof_aliases` quarantine to Windows. On Linux, remove it so those tests must pass, which is stricter.

**Revalidation required after the fix:**
- the directly affected suites: sandbox/Docker integration, execution integrity, CI gate;
- a local Linux check;
- actionlint and zizmor, if the workflow is touched;
- an authorized update of `feat/verification-first-1e-ci`;
- a **re-run of T1** (an operator decision).

## 9. Remaining limitations

- **Independent merge enforcement is not proven:** there is no CODEOWNERS file, no ruleset, and a single collaborator.
- **Attestation:** not tested.
- **ShellCheck:** not run.
- **The workflow self-modification and skipped-job limits** in the threat model remain.

## 10. Can T2–T9 begin?

**No.** Until the harness defect is fixed and T1 is re-run green, non-browser criteria are BLOCKED for every PR. That would hide the intended FAIL distinction for T2, and it would make every negative test pass trivially. The bypass tests T5 and T6 additionally need an independent code owner.

*Local working files (run log, downloaded artifacts) are kept in the operator's session scratchpad; they are not committed.*
