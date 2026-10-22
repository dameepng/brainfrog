# Phase 1E — Linux CI Design

## Files

| File | Role |
| :--- | :--- |
| `.github/workflows/brainfrog-verify.yml` | Workflow `BrainFrog` with three jobs: `Verification` (the gate), `Linux self-test` (diagnostic) and `Attest evidence manifest (optional)` |
| `.github/brainfrog/policy.json` | Trusted policy (`brainfrog.ci-policy/1e`): targets, required criteria, image pins, trusted paths, evidence limits, self-test modules and quarantine |
| `.github/brainfrog/specs/fixture-login-app.md` | Trusted acceptance spec: 1 unit test, 1 build check and 4 real browser criteria against `tests/fixtures/browser_app/correct` |
| `.github/brainfrog/requirements-verifier.txt` | Hash-locked runtime and self-test dependencies (`uv export --frozen --offline --format requirements-txt --no-header --no-emit-project --extra dev` from `uv.lock`; the `dev` extra adds pytest, which two self-tests invoke as `sys.executable -m pytest`) |
| `core/ci_trust.py` | Trusted-base resolution (standard library only): verifies the tested PR merge and returns its first parent as the trusted revision |
| `core/ci_gate.py` | Gate logic: policy, identity, images, verification, decision, evidence, enforcement, self-test |
| `scripts/brainfrog_ci.py` | Entry point, run as `python -I`; puts only the trusted checkout on `sys.path` |

## Runner layout (one disposable `ubuntu-24.04` runner per job)

```text
$GITHUB_WORKSPACE/candidate/   GITHUB_SHA (merge commit): data under test
$GITHUB_WORKSPACE/base/        PR base branch, full history: runs only core/ci_trust.py (merge-base check)
$GITHUB_WORKSPACE/trusted/     verified merge first parent: verifier, policy, specs, locks, browser recipe
$RUNNER_TEMP/bf-venv/          venv holding only the trusted hash-locked wheels
$RUNNER_TEMP/bf-ci/work/       verifier reports (never uploaded directly)
$RUNNER_TEMP/bf-ci/brainfrog-ci-evidence/   validated, publishable bundle (the only uploaded path)
```

## Verification job steps

1. **Harden runner:** `step-security/harden-runner`, egress `audit`, matching the existing `ci.yml`.
2. **Check out `candidate/`** at the default ref, which is the merge commit, with `persist-credentials: false`.
3. **Check out `base/`**: the PR base branch by name (`github.base_ref`), `fetch-depth: 0`, `persist-credentials: false`.
4. **Set up Python 3.12.**
5. **Resolve the trusted revision:** `python -I base/core/ci_trust.py resolve --candidate candidate --base base` (standard library only). It fails the job unless all of these hold:
   - the event is `pull_request`, `GITHUB_REF` is `refs/pull/<N>/merge`, and `candidate/` HEAD is `GITHUB_SHA`;
   - the merge has exactly two parents, and the second is `pull_request.head.sha`;
   - `pull_request.base.repo` is this repository, and `base.ref` is a plain branch name equal to `GITHUB_BASE_REF`;
   - `base/` was fetched from `$GITHUB_SERVER_URL/$GITHUB_REPOSITORY`, is on `refs/heads/<base.ref>`, and matches `origin/<base.ref>`;
   - the merge's first parent is that branch's tip or an ancestor of it.

   Only then does it write `trusted-sha=<first parent>` to the step output. The event's `pull_request.base.sha` is never used to choose trusted code: GitHub does not refresh it when the base branch moves (T1 retest, run `39731332404`).
6. **Check out `trusted/`** at `steps.resolve.outputs.trusted-sha`, with `persist-credentials: false`.
7. **Create a venv** and run `pip install --no-deps --require-hashes --only-binary=:all: -r trusted/.github/brainfrog/requirements-verifier.txt`.
8. **Provision images:** `brainfrog_ci.py provision`. It refuses to run unless `GITHUB_ACTIONS=true`. It runs:
   - `docker pull python@sha256:a6e34c59…`, then `docker tag … python:3.12-slim`;
   - `docker build -t brainfrog-browser-runner:v1.64.0 trusted/docker/browser-runner`, but only after the Dockerfile and lock hashes match the policy.

   The Dockerfile's `FROM` is pinned by digest, and pip runs with `--require-hashes`.
9. **Gate** (`brainfrog_ci.py gate --base base`):
   - load the policy;
   - check identity, re-running the same merge-base verification from the trusted code and requiring `trusted/` HEAD to equal the verified first parent;
   - inspect images: the Python `RepoDigests` must contain the pin, the recipe hashes must match, and the seccomp hash must match;
   - run `StandaloneVerifier` with `repo_dir=candidate/<target path>`, `spec_root=trusted`, the Docker backends, and `approved_browser_images={tag: built ID}`;
   - evaluate the result and build the evidence bundle.

   The exit code is 0 for success, 1 for failure and 2 for blocked.
10. **Digest:** `brainfrog_ci.py digest` records the sha256 of `manifest.json` as a job output. It runs with `if: always()`.
11. **Upload** the evidence bundle (`if: always()`, `if-no-files-found: error`, 14-day retention, no hidden files).
12. **Enforce** (`brainfrog_ci.py enforce`, `if: always()`): re-checks the bundle against its manifest and maps the decision to the step's exit code.

There is no `continue-on-error` and no `|| true`. Every failing step fails the job; cancellation and timeouts are never success.

## Linux self-test job (diagnostic, candidate code on the host)

This job runs the **candidate's** unit-test modules on the runner, one process per module, using the trusted hash-locked venv and the provisioned Python image.

It uses the same `candidate/`, `base/`, resolver and `trusted/` steps as the Verification job. Before any candidate module runs, `brainfrog_ci.py selftest --base base` re-verifies the trusted revision; if that check fails, it records the problems in `selftest-summary.json` and stops with exit 2.

Its rules come from the **trusted** policy:
- the listed modules must run;
- the named required tests (15 symlink tests plus the 2 T1 harness-staging tests; 17 in total) must report `ok`, so a skip counts as a failure;
- any ERROR or FAIL must be a quarantined, signature-matched baseline (see [phase1e-pr-gate-contract.md](phase1e-pr-gate-contract.md));
- a module that runs zero tests fails.

The job holds a read-only token and no secrets. Because the code under test is the PR's own, this job is **not** a trust gate: it reports whether the candidate's test suite passes on Linux.

`test_verifier_browser_integration` is not in the list, because it approves only this workstation's browser image ID. Linux browser coverage comes from the Verification job, which runs real Chromium.

## Seccomp and AppArmor on GitHub runners: open risk

The pinned seccomp profile is a complete profile (moby v0.1.0 default plus `clone`/`unshare`/`chroot`), so it does not depend on the engine's built-in default.

Ubuntu 24.04 hosts, however, may restrict unprivileged user namespaces through AppArmor (`kernel.apparmor_restrict_unprivileged_userns`). If GitHub's runner enforces that restriction, Chromium's namespace sandbox cannot start. In that case the runner records `renderer_sandbox_unverified` and **every browser criterion is BLOCKED**: it fails closed and is never green.

This has **not** been observed on GitHub yet. Any change to host AppArmor settings would be a security decision for the operator, and BrainFrog does not make it.
