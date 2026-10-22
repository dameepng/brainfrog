# Phase 1E — Capability Audit (checkpoint `41c6f0f`)

All items below were inspected in the new worktree `<PHASE_1E_WORKTREE>` on branch `feat/verification-first-1e-ci`, created with `git worktree add` from `41c6f0fca5a2a8f464c98bf85de06053f7f9eb45`. Everything the brief required was present at that commit:

- the verifier and OCI backend;
- the harness and browser runner;
- the seccomp profile (sha256 `569408de…4b39`);
- the browser Dockerfile (`b860f73e…884c`) and lock (`1654ad6f…89ef`);
- the existing tests and evidence checker.

## Where the controls live (verified, not assumed)

| Control | Location |
| :--- | :--- |
| Trust modes (untrusted → fail closed; `--trust-local-code`; sandbox) | `core/verifier.py` `StandaloneVerifier.__init__` |
| Operator-only injection of backends | `StandaloneVerifier(sandbox_backend=..., browser_backend=...)`, plus the new `spec_root` |
| Python image allowlist (tag → local ID; never pulls) | `core/runtime/sandbox.py` `APPROVED_DOCKER_VERIFICATION_IMAGES`, `DockerSandboxBackend.resolve_image` |
| Browser image approval (pinned **local** image ID) | `core/runtime/browser_sandbox.py` `APPROVED_BROWSER_IMAGES`; the constructor accepts an operator-only `approved_browser_images` |
| Seccomp pin | `browser_sandbox.py` `BROWSER_SECCOMP_PROFILE_SHA256`, `load_browser_seccomp_profile` |
| Snapshot (no symlink following, `git ls-files`, drift re-read) | `sandbox.py` `materialize_source_snapshot_with_manifest` |
| Report checker (bundle confinement, hashes, browser cross-binding) | `verifier.py` `verify_report_integrity`, `_bundle_file`, `_browser_evidence_findings` |
| Ownership-safe Docker cleanup | `sandbox.py` (`remove_owned_container`) and `browser_sandbox.py` label checks |

## Gaps found for CI

1. **Spec location.** `verify_spec` required the spec to be inside the repository under test, so a trusted spec held outside the candidate was impossible. **Fixed:** an operator-only `spec_root` parameter. The default behaviour is unchanged.
2. **Pre-existing path-guard defect.** Absolute spec paths were not `resolve()`d, so `<root>/../outside/spec.md` (or a symlinked absolute path) passed the lexical guard and **the outside file was read**. This was reproduced on Linux against the checkpoint's own `core/verifier.py`: the run proceeded to parse the outside file instead of raising `PermissionError`. **Fixed** by resolving every spec path before the guard. Covered by `test_ci_linux_symlinks.TestSpecPathSymlinks`.
3. **Image identity is host-local.** The browser image ID `21d60e45…` exists only in this machine's daemon; with the containerd store, the local Python image ID equals its registry index digest. Neither is portable. **CI design:**
   - pin Python by **registry digest** (`python@sha256:a6e34c59…11b1`, which matches the local image's `RepoDigests`);
   - **rebuild** the browser runner from the trusted recipe in each run;
   - approve exactly the built ID for that run only.
4. **Dependencies.** `core/__init__` eagerly imports agent modules (`anthropic`, `requests`, `rich`, `pydantic`…), so even the verifier needs third-party packages. **CI design:** a hash-locked `requirements-verifier.txt` exported offline from the reviewed `uv.lock`. It holds 43 pins with 802 hashes, and every pin has a Linux/CPython 3.12 wheel. Refactoring `core/__init__` was out of scope.
5. **Tooling not available locally:** PyYAML, `actionlint`, `zizmor`, `shellcheck`. The workflow is therefore validated by a strict YAML-subset parser in the tests. Running `actionlint` is a rollout step.
6. **Existing `ci.yml`** runs `python -m unittest discover -s tests` on `ubuntu-latest`. That full run includes the two known REPL errors, so it is not a usable gate as it stands. It was left unchanged (not Phase 1E scope).
