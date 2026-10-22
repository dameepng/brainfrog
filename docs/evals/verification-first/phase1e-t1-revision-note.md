# Phase 1E — T1 revision note: phase 6B CLI failures (root-cause correction)

**Scope.** This note corrects one root-cause attribution in [phase1e-t1-live-results.md](phase1e-t1-live-results.md) (T1, run `39705364940`). That report is the original forensic record and is kept unchanged. Its verdict, **T1 LIVE GITHUB VERIFICATION — BLOCKED**, stands.

## What the original report said

| Original row | Original attribution |
| :--- | :--- |
| `test_phase6b_cli_verification`: `test_11` (BLOCKED ≠ PASS), `test_14` (`rolled_back`) | "Same defect: verification BLOCKED → rollback path", i.e. the 0700 harness staging defect |

## Correction

The harness defect did **not** cause these two failures. The cause is a missing test dependency in the CI environment: **pytest was not installed in the Linux self-test venv.**

Evidence, from full logs rather than summaries (run `39731332404`, where the harness fix was already in the candidate code):

- **Interpreter.** The CLI's own verification table in `tests.test_phase6b_cli_verification.stdout.log` names the failing command: `/home/runner/work/_temp/bf-venv/bin/python -m pytest test_mod.py`. That is the self-test venv the workflow creates from `trusted/.github/brainfrog/requirements-verifier.txt`.
- **Installed set.** The venv step's `Successfully installed …` line lists 27 packages, and pytest is not among them. `requirements-verifier.txt` contained no pytest entry.
- **`test_11`** fails at `self.assertEqual(proc.returncode, 0)` (line 592). That is the test's first, direct `subprocess.run([sys.executable, "-m", "pytest", "test_calc.py", "-q"])`; the BrainFrog verifier and the Docker harness are not involved at that point.
- **`test_14`.** Its runtime's domain test command is `sys.executable -m pytest test_mod.py`. The test fails, so the transaction rolls back, which gives `transaction_status == 'rolled_back'` instead of `committed`.
- **Same failures in both runs.** They are identical, at the same lines, in run `39705364940` (unfixed harness) and run `39731332404` (fixed harness in the candidate). So they are independent of the harness defect.
- **Locally they pass.** The Windows development environment has pytest 9.1.1 installed, which is why they never failed there.

## Root cause in the lock

- `pyproject.toml` declares `pytest>=7.0.0` in the `dev` extra, but `uv.lock` was stale: its `dev` extra contained only pillow (`uv lock --check` reported that the lockfile needed updating).
- `requirements-verifier.txt` was exported without the `dev` extra.

## A second, independent cause for `test_14` (found 2026-10-22)

The lock fix was tested in a Linux container: Python 3.12, a venv built from the lock exactly as the workflow builds it, and uid 65532 with a read-only source tree.

| Lock | `python -m pytest` | `test_11` | `test_14` |
| :--- | :--- | :--- | :--- |
| Old (no pytest) | `No module named pytest` | FAIL (`1 != 0`) | FAIL (`rolled_back`) |
| New (pytest 9.1.1) | `pytest 9.1.1` | **ok** | **FAIL** (`rolled_back`) |

Tracing `run_hardened_subprocess` during `test_14` shows the runtime launching `/tmp/venv/bin/python -m pytest test_mod.py` while the process that ran reports `/usr/bin/python3.12: No module named pytest`.

**Cause:** `core/runtime/command_policy.py:resolve_and_authorize_executable`.
- It authorizes an absolute interpreter path when its realpath equals `realpath(sys.executable)`, and then returns that **realpath** as the path to execute. A bare `python` is handled the same way.
- On Linux, a venv's `bin/python` is a symlink to the base interpreter. Executing the resolved target leaves the venv, so the venv's packages (pytest) are invisible.
- On Windows, a venv's `python.exe` is a real file, so the defect never appeared locally.
- This also affects the GitHub runner: `sys.executable` there is `…/_temp/bf-venv/bin/python`, a symlink into the hosted tool cache.

So `test_14` needs both fixes: pytest in the lock, and the venv-preserving interpreter path.

## Remediation

- **Lock:**
  - `uv.lock` refreshed; only pytest and its dependencies were added, with pluggy held to releases at least 14 days old;
  - the verifier lock re-exported with `--extra dev`.
- **Interpreter authorization** (`core/runtime/command_policy.py`), authorized separately:
  - Identity is unchanged: an absolute interpreter path is authorized only if its realpath is the running interpreter's realpath.
  - Spelling is now restricted: the path must also be `sys.executable` itself or that canonical file. Any other alias is denied, such as a caller-made symlink or a path through a symlinked directory, because it could sit next to a `pyvenv.cfg` the caller controls.
  - Execution: Python always runs as `sys.executable` as launched, for absolute paths and for bare `python`/`python3`. That path is set by the trusted parent process, never by the command.
  - The interpreter's directory must lie outside the workspace. Otherwise the command could have rewritten the `pyvenv.cfg` that selects the execution context.
  - Argument validation, the environment scrubbing and the other executable types are unchanged.
- **One existing assertion changed:** `test_bare_python_binds_strictly_to_sys_executable_regardless_of_path` required the realpath, which encoded the defect. It now requires `sys.executable` as launched, plus the same realpath identity.
- **Regression tests:** `tests/test_interpreter_authorization.py`. They include a real symlinked venv whose child process reports the venv's `sys.prefix`, and attacker aliases with their own `pyvenv.cfg`.

The two tests and their assertions are unchanged; neither is skipped or quarantined.
