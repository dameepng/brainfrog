# Phase 1E — CI Threat Model

**Baseline:** checkpoint `41c6f0fca5a2a8f464c98bf85de06053f7f9eb45` (Phase 1B–1D)
**Scope:** GitHub Actions `pull_request` verification of `dameepng/brainfrog`, a public repository whose default branch is `main`.

## Trust boundaries

| # | Component | Trust | Source in CI | Why |
| :--- | :--- | :--- | :--- | :--- |
| 1 | **Verifier implementation** (`core/verifier.py`, `core/runtime/*`, `core/ci_gate.py`, `scripts/brainfrog_ci.py`) | Trusted | `trusted/` checkout at the verified first parent of the tested merge (`core/ci_trust.py`); not the event's `base.sha`, which can be stale | Candidate code must not judge itself. The PR's own copies are never imported. |
| 2 | **Workflow definition** (`.github/workflows/brainfrog-verify.yml`) | **Not trustworthy on its own** | PR merge commit | For `pull_request`, GitHub runs the workflow in the context of the merge commit (GitHub docs, "Events that trigger workflows"), so a PR can edit the job that judges it. See "Self-modification" below. |
| 3 | **Acceptance policy and specs** (`.github/brainfrog/policy.json`, `specs/*.md`, `requirements-verifier.txt`) | Trusted | `trusted/` checkout | A PR cannot delete or weaken required criteria for its own run. Its edits are detected and force BLOCKED. |
| 4 | **Candidate source** | Untrusted data | `candidate/` checkout at `GITHUB_SHA`, the merge commit | It is snapshotted and executed only in OCI containers (Phase 1C/1D). Nothing from it is imported or run on the host in the Verification job. |
| 5 | **Evidence emitted by tested code** (stdout/stderr, page content, screenshots) | Untrusted | Container outputs | Hashed, size-bounded and nonce-bound (1C/1D). The verdict is recomputed on the host. Before publication it is checked for links, private paths and secret values. |
| 6 | **GitHub infrastructure** (runner, checkout, artifact storage, check runs, OIDC signing) | Trusted by assumption | GitHub-hosted `ubuntu-24.04` | This is outside BrainFrog's control and is documented as an assumption. |
| 7 | **Event metadata** (titles, branch names, bodies, commit messages) | Untrusted | `GITHUB_EVENT_PATH` | It is never interpolated into shell. Only SHAs, numbers and repository names are used, after validation. The branch name is stored as inert JSON data, and the PR title is never recorded. |

## Attacks and mitigations

| Attack | Mitigation in Phase 1E | Residual |
| :--- | :--- | :--- |
| A PR changes `core/verifier.py` so that everything PASSes | The verifier is loaded from the base revision with `python -I` and an explicit `sys.path` that holds only the trusted checkout. | None for this job, as long as the workflow itself is honest (row 2). |
| A PR deletes or weakens required criteria, or edits the policy | The trusted spec and policy are always used. Any candidate change under `.github/brainfrog/` is reported and the outcome is forced to BLOCKED (live Case H). | Maintainers must review and merge policy changes. |
| A PR edits the workflow (removes enforcement, adds `continue-on-error`, switches the trusted ref to head, uses `pull_request_target`) | The gate compares the workflow file with the trusted copy and forces BLOCKED (live Case I). Static tests catch these edit patterns. | **A PR that edits the workflow can also delete this comparison.** In-band detection only stops honest or accidental edits. Real enforcement needs repository rules: a required check bound to the GitHub Actions app, plus code-owner review of `.github/**`. **Not configured by BrainFrog**; see [phase1e-remote-rollout.md](phase1e-remote-rollout.md). |
| Script injection through the branch name or title | No `${{ }}` inside `run:` blocks; SHAs pass only through `with:` inputs. Tested with hostile fixtures. | — |
| Token or secret theft by tested code | `permissions: contents: read`, `persist-credentials: false`, and no secrets referenced. Containers receive no environment from the host, and the self-test strips `*TOKEN*`, `GITHUB_*` and `ACTIONS_*` variables. | Fork PRs get a read-only token and no secrets regardless (GitHub docs). |
| Malicious dependency in the PR (`requirements.txt`, `uv.lock`) | The CI host installs only the **trusted** `requirements-verifier.txt` with `--require-hashes --no-deps --only-binary=:all:` (wheels only, so no `setup.py` runs). | Upstream wheels pinned by hash in the reviewed lock are trusted. |
| Malicious Dockerfile or image reference | Images come only from the trusted policy: Python by registry digest, and the browser runner built from the trusted recipe whose hashes are pinned in the policy. Candidate Dockerfiles are never built. | The base images are trusted at their pinned digests. |
| Evidence forgery inside the run | Host-side verdict recomputation (1D), report integrity checks, and a hash manifest over the published bundle that `enforce` re-checks. | Unsigned. Anyone who controls the whole bundle can re-forge it consistently. The optional attestation narrows this; see [phase1e-evidence-provenance.md](phase1e-evidence-provenance.md). |
| Symlink escapes (snapshot, report checker, spec path, bundle) | Links are never followed. The checker resolves paths and confines them to the bundle; the gate rejects links in targets and bundles. 13 Linux tests exist. | The CI self-test must run them on GitHub's Linux runners as well. |
| Status spoofing by a third-party app or a token holder posting a commit status named "Verification" | The required-check source must be pinned to the GitHub Actions app in the ruleset (GitHub docs: "select an app as the expected source"). | Operator setting; not configured. |
| Run in a privileged context (`pull_request_target`, `workflow_run`) | Not used. Tests reject both. The attestation job runs no code and checks nothing out. | — |
| **Skipped required job.** A PR adds `if: false` (or any false condition) to the `Verification` job | None in-band: a skipped job runs no code, so it cannot detect anything. | **GitHub reports a skipped job as "Success", and the docs say it will not prevent a pull request from merging even when it is a required check** (GitHub docs, "Using conditions to control job execution"). A workflow-editing PR can therefore turn the required check green without running verification. Only code-owner review of `.github/**` catches this. |
| **Renamed or removed job.** A PR renames `Verification` or deletes the workflow | None in-band. | A required check that is never reported stays pending, so the merge is blocked; this fails closed. A PR that adds another job named `Verification` is the same case as a workflow edit (row above). |

## Self-modification: the honest limit

Inside a `pull_request` workflow, nothing in the repository's own files can stop a PR from replacing the workflow with one that reports success. The protection Phase 1E can give is:

1. **Same-run detection:** the candidate's workflow or policy differs from the trusted copy, so the run is BLOCKED. This defeats unintentional edits and the many attacks that leave the gate code in place.
2. **Repository governance (operator):**
   - make `Verification` a required check whose expected source is GitHub Actions;
   - require code-owner review for `.github/**`, `core/ci_gate.py` and `scripts/brainfrog_ci.py`;
   - require approval of workflow runs for outside contributors.

   With these rules in place, a self-modifying PR can at most produce a run that a code owner must still approve.

Until (2) is configured and observed on GitHub, **BrainFrog does not claim that the PR gate cannot be spoofed.**

### What Phase 1E actually guarantees

- **The verifier is pinned, but the workflow is not.**
  - The verifier code, policy, specs, dependency lock and browser recipe come from the base revision.
  - The workflow file that *calls* them comes from the PR merge commit.
  - Pinning the verifier therefore does not pin the workflow. An honest, unmodified workflow uses the trusted verifier; a modified one can skip it, replace it, or report success without it.
- **A green `Verification` check proves that verification passed only if the workflow that ran it was the reviewed one.**
  - That holds when the PR does not touch `.github/**`.
  - It also holds when a code owner has approved the workflow change.
  - Neither the check nor the evidence bundle, attested or not, proves that the workflow was unmodified.
- **A result from a modified or incomplete run must never be described as independently proven.**

### Blocker for arbitrary untrusted public PRs

**There is no independent enforcement boundary today** that stops a workflow-editing PR from producing a green required check. Two routes exist: the skipped-job route and the edit-the-gate route. The only boundary GitHub offers within this design is repository governance:
- a ruleset with a required `Verification` check whose expected source is GitHub Actions;
- required code-owner review covering `.github/**`;
- approval of workflow runs from outside contributors.

None of this is configured or observed yet. Until it is, the gate is **advisory for PRs that modify `.github/**`**, and accepting arbitrary untrusted public PRs on the strength of this check is **blocked**.

The first remote rollout is therefore limited to **operator-created, controlled test PRs** against a non-production base branch (see [phase1e-remote-rollout.md](phase1e-remote-rollout.md)).

**Rejected fixes:** switching to `pull_request_target`, adding privileged `workflow_run` triggers, or widening token permissions. Each would give untrusted code a privileged context.
