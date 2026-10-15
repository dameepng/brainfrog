# BrainFrog v0.1.0-rc1 External Tester Publication Checklist

> **Purpose**: This checklist governs the publication lifecycle for recruiting 3–5 external developer testers to evaluate BrainFrog `v0.1.0-rc1`.  
> **Status**: **PUBLISHED** — Discussion is live on GitHub.

---

## 1. Pre-Publication Verification Gate

All verification steps completed and approved:

| Check | Item Description | Verification Method | Status |
| :---: | :--- | :--- | :---: |
| **1.1** | **Confirm GitHub Discussions is enabled** | Checked via `gh repo view dameepng/brainfrog --json hasDiscussionsEnabled`. Verified `hasDiscussionsEnabled: true`. | [x] Verified Enabled |
| **1.2** | **Confirm target category exists** | Queried categories via GraphQL API: `Announcements` (slug: `announcements`, ID: `DIC_kwDOUpHrqc4DISy4`). Target category: **`Announcements`**. | [x] Category Confirmed (`Announcements`) |
| **1.3** | **Verify release URL & asset availability** | Confirmed public release page is live at [https://github.com/dameepng/brainfrog/releases/tag/v0.1.0-rc1](https://github.com/dameepng/brainfrog/releases/tag/v0.1.0-rc1) and matches tag `v0.1.0-rc1` pointing to commit `f06a2768e7f84d36da0cbd038affc6c0dacfb715`. | [x] Verified Live |
| **1.4** | **Verify onboarding command syntax** | Validated commands in clean environment: `git clone`, `python -m venv .venv`, `pip install -r requirements.txt`, `pip install -e .`, `python demo/reproducible_proof_demo.py`, `brainfrog proofs`, `brainfrog show-proof latest`, `brainfrog verify-proof latest --check-workspace`. | [x] Verified Offline Walkthrough |
| **1.5** | **Review draft for honest framing & link validity** | Reviewed [`GITHUB_DISCUSSION_DRAFT.md`](GITHUB_DISCUSSION_DRAFT.md): confirmed zero claims of existing adoption, testimonials, or completed external validation; accurate explanation of `FAILED` (exit code 3) vs `INVALID` (exit code 1); absolute URLs to repo and release. | [x] Draft Complete & Aligned |
| **1.6** | **Clarify offline and network claims** | Confirmed and explicitly documented that initial `pip install` may require network access for package indexes, whereas the verification runtime and reproducible demo run 100% locally offline. Clarified Scenario 3 tamper detection behavior. | [x] Verified & Clarified |
| **1.7** | **Obtain explicit maintainer approval** | Repository maintainer gave explicit authorization to publish to GitHub Discussions. | [x] Approved by Maintainer |

---

## 2. Publication Record

### Publication Metadata
- **Published Discussion URL**: [https://github.com/dameepng/brainfrog/discussions/41](https://github.com/dameepng/brainfrog/discussions/41)
- **Discussion Number**: `#41`
- **Published Category**: `Announcements`
- **Published Date & Time**: `2026-10-13T11:22:39+07:00` (Local) / `2026-10-13T04:22:39Z` (UTC)
- **Author / Publisher**: `dameepng`
- **Discussion State**: `OPEN`

### Response Tracking Linkage
- **Results Tracker**: [`docs/external-testing/RESULTS_TRACKER.md`](RESULTS_TRACKER.md)
- **Recruitment Status**: **Pending outreach responses** (Discussion published; awaiting first tester reply).
- **Tester Participant IDs**: Assign anonymous identifiers (`T-01` through `T-05`) upon receipt of responses. Do not log personal identifiable information (PII).

---

## 3. Maintenance Sign-Off Log

- **Maintainer Reviewer**: `dameepng`
- **Sign-Off Date**: `2026-10-13`
- **Decision**: [x] APPROVED AND PUBLISHED
