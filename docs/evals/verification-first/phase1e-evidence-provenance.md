# Phase 1E — CI Evidence and Provenance

## Bundle (`brainfrog-ci-evidence-<run_id>-<attempt>`, retained 14 days)

```text
brainfrog-ci-evidence/
  manifest.json      brainfrog.ci-evidence-manifest/1e: sha256 of every other file, plus the outcome
  decision.json      outcome, exit code, reasons
  ci-metadata.json   brainfrog.ci-metadata/1e (fields below)
  reports/<target>/<run_id>/   unchanged Phase 1D report bundle (report.json, report.md, evidence/...)
```

**`ci-metadata.json` fields:**
- identity: `ci_provider`, `repository`, `event_name`, `pull_request_number`, `head_sha`, `base_sha`, `github_sha`, `tested_sha`, `tested_sha_is_pr_head`, `head_repository`, `is_fork_pull_request`, `head_ref` (inert data);
- workflow: `workflow_ref`, `workflow_sha`, `workflow_run_id`, `workflow_run_attempt`;
- trusted inputs: `trusted_verifier_revision`, `trusted_policy_digest`;
- images and engine: `python_image_identity` (tag, ID, `repo_digests`, required digest), `browser_image_identity` (tag, ID, base image, recipe hashes, expected Playwright/Chromium versions), `seccomp_profile_digest`, `docker_engine_version`;
- per target: `targets[]`, each with `trusted_spec_sha256`, `verification_plan_digest` (the report's spec content hash), `source_snapshot_digest` (materialized digest), `report_run_id`, `criterion_results[]` (status plus per-file evidence hashes) and `evidence_published`;
- review flags: `candidate_trusted_config_changes`;
- result: `overall_verdict`, `provenance`.

The Phase 1D `report.json` schema is unchanged, so existing `--check-report` works on the copies.

## Validation before upload

For each report:
- `verify_report_integrity`: hashes, confinement, snapshot and browser cross-binding;
- no symlinks or junctions;
- at most 400 files, 50 MiB in total, and 10 MiB per file;
- no user-profile paths (`C:\Users\…`, `/Users/…`, `/home/<not runner>/…`);
- no secret **values**: token formats, PEM private keys, live secret environment values, or `Cookie:`/`Authorization:` headers.

Bundles that fail integrity or hygiene are **withheld**, and the decision explains why. The copied report is re-verified after copying.

**Order of operations.** `manifest.json` is written last; the `enforce` step re-hashes the whole bundle before deciding. Upload runs on failure too, so FAIL and BLOCKED runs keep their evidence. The upload outcome never sets the verdict.

**Not published:**
- the repository or the snapshot;
- the `work/` directory;
- hidden files;
- PR titles;
- environment dumps.

Browser `fill` values are never recorded (1D).

## What the hashes prove, and what they don't

- **Unsigned path (default).** This gives **CI-contextual auditability**: the bundle names the run, the tested revision, the trusted revision and the policy digest, and any edit after finalization is detected. It is **not** proof of authenticity. Anyone who controls the whole bundle can recompute every hash; `test_consistently_rehashed_report…` shows the manifest catches a rehashed report, but a forger can rewrite the manifest too.
- **Optional authenticated path.** The `attest-evidence` job uses `actions/attest@1e69f48a…` (v4.2.2) to sign the sha256 of `manifest.json` (subject `brainfrog-ci-evidence-manifest.json`) through GitHub's Sigstore-based artifact attestations.
  - It is **off by default** and runs only when all of these hold:
    - the repository variable `BRAINFROG_ATTEST_EVIDENCE` is `true`;
    - the PR comes from the same repository, since fork PRs do not get `id-token: write`;
    - the Verification job produced a manifest.
  - It holds `id-token: write`, `attestations: write` and `artifact-metadata: write`. It has no checkout and runs no scripts; its one step is the attest action.
  - GitHub documents attestations as available for public repositories on all plans. The repository is public.
  - The digest is computed by BrainFrog itself rather than taken from `upload-artifact`'s `artifact-digest` output, whose encoding is not documented.
  - **Status: implemented, never executed.** Live verification is in the rollout plan.
- **Limits of a signature.** A valid attestation would prove that this manifest came from a GitHub Actions run of this repository's workflow. It would not prove that the workflow was unmodified (see the threat model), and it would not prove that the assertions were semantically right.

**Offline check after download (operator):**
1. Unzip the artifact.
2. Run `sha256sum manifest.json`, and check every file against `manifest.json`.
3. Run `python cli.py --check-report reports/<target>/<run_id>`.
4. If attested, run `gh attestation verify manifest.json --repo dameepng/brainfrog`, then check the signer workflow and ref.
