# Phase 1D — Live Browser Verification Results

> **Note (2026-10-21):** These are the **run-1** results (Chromium 131, renderer sandbox off). Their evidence moved, unchanged, to `phase1d-live-evidence/run1-chromium131-unsandboxed/`. The re-run on Chromium 156 with the renderer sandbox enforced is in [phase1d-final-security-closure.md](phase1d-final-security-closure.md) and `phase1d-live-evidence/run2-chromium156-sandboxed/`. Both evidence directories are **local-only**: they were excluded from the checkpoint commit `41c6f0f` and are not in the repository.

All results below were **observed** on 2026-10-20 (UTC) through the real CLI:

```
python cli.py --repo <disposable fixture repo> --spec spec.md [...]
```

The environment was Docker Engine 29.1.3 (`desktop-linux`, WSL2), the app image `python:3.12-slim` (`sha256:a6e34c59…11b1`), and the browser image `brainfrog-browser-runner:v1.49.1` (`sha256:a880ad52…48dc4`), which reports Chromium **131.0.6778.33** and Playwright **1.49.1**.

Each fixture was copied into a fresh disposable git repository in the session scratchpad and committed there; nothing was committed in BrainFrog.

Sanitized transcripts, `--check-report` outputs and `demo_summary.json` were written to the local-only directory `phase1d-live-evidence/`, with `<scratch>` and `<userprofile>` substituted for local paths. The directory is not in the repository; the files are identified by the SHA-256 values below and in [phase1d-commit-readiness.md](phase1d-commit-readiness.md). Phase 1E publishes equivalent evidence as CI artifacts ([phase1e-evidence-provenance.md](phase1e-evidence-provenance.md)). The full report bundles stay outside the repository, because their effective container configuration records host temp paths.

## Fixtures

The fixtures live in `tests/fixtures/browser_app/`.

| Fixture | Contents |
| :--- | :--- |
| `correct/` | Login page (`#login-form`, `#email`, `#password`, `#submit`, `#email-error`), dashboard, CSS, JS validation, `validation.py` plus unit tests |
| `broken/` | Identical except for two deliberate defects:<br>• JS never un-hides `#email-error`, so the text "Invalid email address" stays in the DOM but hidden (a hidden-text trap);<br>• `.promo-banner { width: 960px }`, which overflows a phone viewport. |
| `hostile/` | `escape.html`: off-origin fetches, an image request, then `location.href = "http://example.com/stolen"`<br>`subresources.html`: off-origin fetches and an image only<br>`memory.html`: renderer memory exhaustion |

## Scenario results

| Scenario | Run | Criterion | Observed | Expected |
| :--- | :--- | :--- | :--- | :--- |
| **A** Page renders | 01 correct | AC-01 (3 assertions) | **PASS**, screenshot `f6e1b3cc…` | PASS ✔ |
| **B** Valid submission | 01 correct | AC-02 (fill, fill, click; `expect_url_path /dashboard`, `expect_text "Welcome"`, `expect_count 3`) | **PASS** | PASS ✔ |
| **C** Invalid email (correct) | 01 correct | AC-03 | **PASS** | PASS ✔ |
| **C** Invalid email (broken) | 02 broken | AC-03 | **FAIL**: "Element '#email-error' exists but is not visible (hidden text does not satisfy expect_text)". Observed `{match_count: 1, visible: false, text: ""}`. Screenshot `a0fbbb41…` | FAIL ✔ |
| **D** Mobile overflow (correct) | 01 correct | AC-04 @375×667 | **PASS**: content 375 px, viewport 375 px | PASS ✔ |
| **D** Mobile overflow (broken) | 02 broken | AC-04 @375×667 | **FAIL**: "content width 960.0px exceeds viewport width 375.0px". Full-page screenshot `1c13e7fc…` | FAIL ✔ |
| Mixed criteria | 01 / 02 | AC-05 `python -m unittest discover -s tests` (2 tests), AC-06 `python -m compileall -q .` | PASS / PASS in both runs, in the Phase 1C OCI sandbox | PASS ✔ |
| Overall | 01 / 02 | — | **PASS exit 0** / **FAIL exit 1** | ✔ |
| **E** App unavailable | integration test 05 | app container killed before the browser starts | **BLOCKED** `app_unreachable`; no screenshot fabricated | BLOCKED ✔ |
| **E** App startup failure | integration test 05 | profile argv exits with code 3 | **BLOCKED** `app_startup_failed` ("application process exited") | BLOCKED ✔ |
| **F** Browser image missing | integration test 06 | approved tag not present locally | **BLOCKED** (`unavailable_sandbox`, "not available locally") | BLOCKED ✔ |
| **F** Browser runtime missing | integration test 06 | image without Playwright | **BLOCKED** `browser_unavailable` | BLOCKED ✔ |
| **F** Docker unavailable | 06 (CLI, `DOCKER_HOST` set to a non-existent pipe) | all 6 | **BLOCKED** `docker_unavailable`, exit 2, no host fallback | BLOCKED ✔ |
| **G** Malicious navigation | 03 hostile | AC-01 `/escape` | **BLOCKED** `unapproved_navigation` ("http://example.com/stolen"). 5 requests blocked and recorded: `example.com/beacon`, `169.254.169.254/latest/meta-data/`, `host.docker.internal:2375/version`, `192.168.65.254/pixel.png`, `example.com/stolen` | blocked ✔ |
| **G** Off-origin subresources | 03 hostile | AC-02 `/subresources` | **PASS** on its own assertion; 3 requests blocked and recorded | recorded ✔ |
| **G** OS-level isolation | integration test 04 | probe inside the browser container on the owned internal network | app reachable; `1.1.1.1:80`, `8.8.8.8:53`, `169.254.169.254:80`, `192.168.65.254:80`, `172.17.0.1:80` and the network gateway on ports 80 and 2375 all unreachable; `example.com`, `host.docker.internal` and `gateway.docker.internal` unresolved; UID 65532 | isolated ✔ |
| **H** Screenshot tampering | 01 and 02, copied bundles | one byte flipped in `AC-01/screenshot.png` | `--check-report` **exit 1**: "Hash mismatch for 'evidence/AC-01/screenshot.png'". The originals verify (exit 0). | detected ✔ |
| **H′** Re-hashed tampering | unit test (evidence suite) | screenshot replaced **and** its hash rewritten in `report.json` | detected: "screenshot hash differs between assertion evidence and report" | detected ✔ |
| **I** False browser proof | 04 | navigation-only plan; `screenshot` step; `evaluate`; absolute metadata URL | all **BLOCKED** `invalid_browser_plan`, no container started, exit 2 | never PASS ✔ |
| **J** Source binding | 01 vs 02 | same plans | materialized digests `cd2d8eb1…1355f` (correct) vs `31cb25ee…a0f8` (broken). AC-03 and AC-04 PASS vs FAIL. Each assertion manifest's `source_snapshot_digest` equals its own report's digest, and so does the digest the app container observed. | ✔ |
| Fail-closed | 05 `--sandbox none` | all 6 | **BLOCKED**, exit 2; browser criteria are never served or browsed on the host | ✔ |
| Unsupported profile | 07 (spec requests `node-dev-server`) | browser criteria | **BLOCKED** "Unsupported Application Startup Profile". Unit and build criteria PASS; overall BLOCKED, exit 2. | ✔ |
| Timeout | integration test 08 | 8 s override | **BLOCKED** `timeout`; container killed; cleanup verified | ✔ |
| Browser killed mid-run | integration test 09 | `docker kill` of the owned browser container | **BLOCKED** (incomplete runner result) | ✔ |
| Ctrl+C | integration test 10 | `KeyboardInterrupt` during the run | propagates; zero Phase 1D resources remain | ✔ |
| Browser crash / resource limit | integration test 11 | memory exhaustion at a 768 MiB limit | **BLOCKED** `resource_limit_termination` ("Browser container was OOM-killed") | ✔ |

## Key identities (final demo runs)

| Run | Run ID | Repo HEAD | Materialized snapshot | `--check-report` |
| :--- | :--- | :--- | :--- | :--- |
| 01 correct | `verify_20261020_172934_b161ca_f6e090` | `9dd71953` | `cd2d8eb143ea2359a67c6aefe96b1c0fef0e893c00e6a553586ae06ea7e1355f` | exit 0 (tampered copy: exit 1) |
| 02 broken | `verify_20261020_173037_b161ca_21cdd9` | `42bd3760` | `31cb25ee1c85f72d52c2e15073d355244d15ec7ecd2013f3fd5505124849a0f8` | exit 0 (tampered copy: exit 1) |
| 03 hostile | `verify_20261020_173146_7c3573_95beb5` | `f765967d` | `b9ee5388c1dd7de240ff7ae14e302b04c6bfc73b7640266440655bbc4ab55b6f` | exit 0 |
| 04–07 | see `demo_summary.json` | — | — | all exit 0 |

The live workspace digest was identical before and after in every run, so no drift was recorded.

### Screenshot and assertion evidence (broken run)

| File | SHA-256 (prefix) |
| :--- | :--- |
| `AC-03/screenshot.png` → `phase1d-live-evidence/broken_AC-03_invalid-email_FAIL.png` | `a0fbbb41164224a9` |
| `AC-04/screenshot.png` → `phase1d-live-evidence/broken_AC-04_mobile-overflow_FAIL.png` | `1c13e7fc937200f0` |
| `AC-03/browser-assertions.json` | `7c33449eabd9e15f` |
| `AC-04/browser-assertions.json` | `db40ad3f98b456eb` |

The AC-03 screenshot shows the invalid address typed and **no** error message, with the password input masked. The AC-04 full-page screenshot shows the 960 px banner beyond the 375 px layout.

## CLI output (run 02, verbatim apart from width)

```text
BrainFrog Verification
Revision:  42bd376
Workspace: clean
Workspace digest (pre):  1c7eec3030866e2c...
Executed snapshot:       31cb25ee1c85f72d... (9 files)
Image:     python:3.12-slim -> sha256:a6e34c598f24...
Trust:     ISOLATED_SANDBOX (oci_container_docker)
Backend:   OCI Browser Verification
Browser:   Chromium 131.0.6778.33 (Playwright 1.49.1, brainfrog-browser-runner:v1.49.1)
Application: isolated container on a private internal network
Spec:      spec.md (sha256:b161ca73)

   AC-01   PASS   Login form visible (3 assertions executed)
   AC-02   PASS   Valid form submission (3 assertions executed)
   AC-03   FAIL   Invalid email validation (2 assertions executed)
                  Element '#email-error' exists but is not visible (hidden text does not satisfy expect_text)
   AC-04   FAIL   Mobile horizontal overflow (2 assertions executed)
                  Horizontal overflow: content width 960.0px exceeds viewport width 375.0px
   AC-05   PASS   Email validation unit tests (2 executed)
   AC-06   PASS   Python sources compile

Overall: FAIL
Evidence: .brainfrog/reports/verify_20261020_173037_b161ca_21cdd9/evidence/
```

The installed `bf` executable was **not** tested; only `python cli.py` was.

## What these results do not show

- **Frameworks:** only static HTML, CSS and JavaScript served by the `python-static` profile was exercised. No framework application (React build, Django, Node server) was started.
- **Browsers:** only Chromium was used; there is no cross-browser evidence.
- **Hostile coverage:** the hostile fixtures are synthetic. They don't test real browser exploits, and Chromium's internal sandbox was off throughout (see [phase1d-browser-security.md](phase1d-browser-security.md)).
