# BrainFrog Final Recommendation: Verification-First Product Pivot

**Document ID:** `BF-REC-VERIFY-001`\
**Date:** October 19, 2026\
**Status:** Evaluation Complete\
**Recommendation Status:** `POSITIONING_PRELIMINARILY_SUPPORTED`

---

## 1. Formal Recommendation

```text
STATUS: POSITIONING_PRELIMINARILY_SUPPORTED
```

### Recommendation Summary
The product positioning — **"Agent says done. BrainFrog proves it."** — is **preliminarily supported by technical and experimental evidence**.

BrainFrog successfully operated as an independent, deterministic verification authority for coding-agent outputs:
1. It functions completely standalone without TypeSafe API keys, LLM providers, or System 1 cognitive loops.
2. It parses human- and machine-readable `spec.md` specifications with zero DSL complexity.
3. It enforces strict status semantics (`PASS`, `FAIL`, `BLOCKED`, `NOT_RUN`) and fails closed on unexecuted or unsupported criteria.
4. It traps actual code regressions and synthetic assertion failures, producing content-addressed evidence bound to Git revisions.
5. It enforces hardened command boundaries that prevent untrusted specifications from executing arbitrary shell payloads.

### Why "Preliminarily Supported" Rather Than "Fully Supported"?
While the core architecture, unit test suite (20/20 pass), and initial controlled experiment (7 tasks: 3 real, 4 synthetic) prove the feasibility and security of standalone verification:
- **Sample Scope Limitation:** The evaluation was performed on a sample of 7 tasks (including 3 real tasks from this repository). Testing across external third-party repositories with varied build systems (Gradle, Cargo, Go, Webpack) is needed before claiming full universal support.
- **Browser Automation Coupling:** Full interactive DOM assertions and screenshot capture currently depend on an active MCP browser server or Chrome DevTools integration. When not running, browser criteria correctly degrade to `BLOCKED`, but native zero-dependency headless browser execution is not yet packaged in the standalone binary.
- **Dirty Workspace Boundary:** While dirty working trees are accurately distinguished from clean Git SHAs, cryptographically immutable PR proofs require clean commits with signed attestations.

---

## 2. Evidence Basis & Audit Findings

| Requirement | Audit Finding | Experimental Proof | Status |
|---|---|---|---|
| **Zero TypeSafe Dependency** | Verification code path (`core/verifier.py`) has zero imports of `core.typesafe_client` or LLM models | Verified by Test 1 & 2 (`test_standalone_verifier.py`) in clean environment without keys | **PROVEN** |
| **Fail-Closed Verdicts** | Unsupported verification types and unexecuted checks reject with `BLOCKED` / `NOT_RUN` | Verified in Task 7 and Test 6 & 11 | **PROVEN** |
| **Real Defect Trapping** | Traps real runtime failures without trusting agent claims | Caught real session lifecycle assertion discrepancy (Task 2) | **PROVEN** |
| **Command Injection Hardening** | `validate_verification_command` strictly blocks unapproved binaries, shell chaining, and path traversal | Blocked unauthorized shell command attempt (Task 6 and Test 9) | **PROVEN** |
| **Machine-Readable Reports** | Output structured as `.brainfrog/reports/<run-id>/report.{json,md}` with SHA-256 digests | Verified in all 7 runs and Test 13 | **PROVEN** |
| **Non-Zero CI Exit Codes** | Exit code 0 for all-pass, 1 for failures, 2 for blocked/config errors | Verified across test suite and CLI wrapper | **PROVEN** |

---

## 3. What Claims Are Currently Defensible

### Defensible Today:
- "BrainFrog independently verifies coding-agent outputs against explicit Markdown acceptance criteria."
- "BrainFrog operates locally without requiring an LLM or cloud API credentials."
- "BrainFrog produces audit reports with per-criterion statuses and evidence manifests bound to the target Git commit."
- "BrainFrog fails closed: unexecuted or unconfigured checks never report a false pass."
- "BrainFrog prevents untrusted specs from executing unapproved shell commands."

### NOT Defensible Today (Do NOT Claim Publicly Yet):
- "BrainFrog provides automated visual browser verification out-of-the-box in all environments" (requires configured MCP browser).
- "BrainFrog mathematically guarantees proof immutability on dirty working trees" (dirty workspaces are flagged as uncommitted).
- "BrainFrog replaces the need for human code review" (it verifies specified criteria, not unstated domain requirements).

---

## 4. Recommended Next Engineering Steps

1. **Package Headless Browser Shim:**
   Provide a lightweight, bundled headless Chromium runner (e.g., via Playwright or headless Chrome CLI) to transition browser criteria from `BLOCKED` to deterministic `PASS`/`FAIL` without requiring a full MCP server daemon.
2. **Expand External Benchmark Evaluation:**
   Run `bf verify` against 15–20 public coding benchmarks (e.g., SWE-bench Lite outputs from Claude Code and OpenCode) to benchmark real-world defect detection rates.
3. **Add Git Pre-Push / GitHub Action Hook:**
   Distribute an official GitHub Action (`brainfrog/verify-action@v1`) that checks incoming PRs against `spec.md` and posts verification summary tables as PR comments.
4. **Gradual Product Repositioning:**
   Once external benchmarks are documented, update the public `README.md` and CLI documentation to introduce `bf verify` as the front-and-center entrypoint, transitioning autonomous agent orchestration to an optional advanced capability.
