# BrainFrog v0.1.0-rc1 Tester Recruitment Templates

These outreach templates are prepared for recruiting 3–5 independent developer testers to evaluate **BrainFrog v0.1.0-rc1**.

---

## 1. Short Outreach Template (Direct Message / Discord / Slack / Social)

**Subject / Hook**: Quick 10–15 min test of BrainFrog (verification-first coding agent runtime)

Hey! I’m preparing the first public beta of **BrainFrog** and looking for 3–5 developers to test our release candidate (`v0.1.0-rc1`).

**What is it?**
BrainFrog is an autonomous coding runtime centered on one core principle:
> **"Don't just trust the agent. Verify it."**

Instead of relying on natural-language promises or synthetic self-reports from AI models, BrainFrog executes tasks in transactional boundaries, verifies changes with real test suites, and generates authoritative proof artifacts recording exact commands, exit codes, and before/after SHA-256 file hashes.

**What’s involved?**
- **Time commitment**: 10–15 minutes total.
- **Prerequisites**: Python 3.10+ and Git on Windows, macOS, or Linux.
- **Zero API keys**: The demo and proof verification walkthrough run 100% locally and offline without external credentials or network requests.
- **Task**: Clone the repo, run the reproducible 3-scenario demo, explore the CLI proof inspection commands, and share quick feedback on onboarding clarity and output readability.

**Tester Guide**: [`docs/external-testing/TESTER_GUIDE.md`](TESTER_GUIDE.md)  
**Release**: [https://github.com/dameepng/brainfrog/releases/tag/v0.1.0-rc1](https://github.com/dameepng/brainfrog/releases/tag/v0.1.0-rc1)

This is an early release candidate, and honest critical feedback on friction, confusing error messages, or missing evidence is greatly appreciated. If you’re interested, let me know or feel free to run through the guide directly!

---

## 2. Extended Outreach Template (GitHub Discussions / Developer Forum / Reddit)

**Title**: [Call for Testers] BrainFrog v0.1.0-rc1: Offline verification runtime for autonomous code changes

Hey everyone,

We are preparing the public beta of **BrainFrog**, an open-source verification-first agent runtime, and are looking for 3–5 developers across Windows, macOS, and Linux to test-drive release candidate `v0.1.0-rc1`.

### What BrainFrog Does
When autonomous agents modify code, conversational assertions and synthetic summaries are not proof. BrainFrog establishes an evidence-backed trust boundary:
- **Deterministic Test Gating**: Asserts test suite execution and process exit codes.
- **Atomic Rollback**: Restores workspace files byte-for-byte upon test failures while saving a durable historical failure record.
- **Workspace Hash Checking (`--check-workspace`)**: Compares on-disk file SHA-256 hashes against recorded proof states to catch post-task tampering or drift.
- **Canonical Exit Codes**: Adheres to strict machine-readable exit codes (`0=VERIFIED`, `1=INVALID`, `2=MISSING`, `3=FAILED`) for automated CI/CD gating.

### What We Are Asking For
We want to validate our "cold-start" developer experience:
1. Follow the [Tester Guide](https://github.com/dameepng/brainfrog/blob/main/docs/external-testing/TESTER_GUIDE.md) to set up a clean virtual environment.
2. Run the offline 3-scenario demo (`python demo/reproducible_proof_demo.py`).
3. Inspect and verify generated proof artifacts using the `brainfrog` CLI.
4. Fill out the brief [Feedback Template](https://github.com/dameepng/brainfrog/blob/main/docs/external-testing/FEEDBACK_TEMPLATE.md) (or open a GitHub issue).

### What You Need
- **Python 3.10+** and **Git**.
- **No API keys or accounts required**: The verification runtime and offline walkthrough require zero external services or tokens.
- **Total time**: ~10–15 minutes.

### Honest Disclaimer
This is an early release candidate. Proof artifacts are plain JSON records (not cryptographic hardware attestation), and verification quality depends on the caller's test suite. We are actively hunting for onboarding friction, confusing CLI messaging, or cross-platform setup quirks.

If you have 15 minutes to spare, please check out the release:
- **Repository**: [https://github.com/dameepng/brainfrog](https://github.com/dameepng/brainfrog)
- **Release Candidate**: [https://github.com/dameepng/brainfrog/releases/tag/v0.1.0-rc1](https://github.com/dameepng/brainfrog/releases/tag/v0.1.0-rc1)
- **Tester Guide**: [docs/external-testing/TESTER_GUIDE.md](TESTER_GUIDE.md)

Thanks for helping us make autonomous coding verifiable!
