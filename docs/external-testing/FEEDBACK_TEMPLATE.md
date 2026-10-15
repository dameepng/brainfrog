# BrainFrog External Tester Feedback Template

Please complete this feedback form after walking through the [Tester Guide](TESTER_GUIDE.md) or root `README.md`. Your feedback directly guides our public beta release.

---

## 1. Tester Environment

- **Operating System**: (e.g. Ubuntu 22.04 LTS / macOS 14 Sonoma / Windows 11 23H2)
- **Shell**: (e.g. bash / zsh / PowerShell 7.4 / cmd.exe)
- **Python Version**: (`python --version`, e.g. Python 3.11.8)
- **Terminal / Emulator**: (e.g. iTerm2 / Windows Terminal / VS Code integrated terminal)

---

## 2. Onboarding Experience & Time to Value

1. **Time to First Successful Demo**:
   - [ ] < 3 minutes
   - [ ] 3 – 5 minutes
   - [ ] 5 – 10 minutes
   - [ ] > 10 minutes (please detail blockers below)
   - *Estimated exact duration*: _____ minutes

2. **Self-Sufficiency & Documentation**:
   - Were the README / Quickstart instructions sufficient to set up and run the demo without external assistance or Googling?
     - [ ] Yes, completely smooth
     - [ ] Mostly, but encountered minor ambiguity
     - [ ] No, got stuck (please describe where below)
   - *Notes / friction points*:

---

## 3. Conceptual Clarity & Verification Model

1. **Proof Verdict Distinctions**:
   - Did the distinction between `VERIFIED`, `FAILED`, `INVALID`, and `MISSING` make intuitive sense?
     - [ ] Clear and logical
     - [ ] Confusing at first, but understood after inspection
     - [ ] Unclear / counter-intuitive
   - Specifically, did you understand why a task with failing tests generates a **valid proof record with exit code 3 (`FAILED`)** rather than being labeled `INVALID`?
     - [ ] Yes, makes sense (it is a valid historical record of failure)
     - [ ] No, expected it to be called invalid
   - *Comments / suggestions on terminology*:

2. **Live Disk Integrity (`--check-workspace`)**:
   - Did the purpose of `--check-workspace` (detecting post-verification disk mutations or file tampering) make sense?
     - [ ] Yes, very clear
     - [ ] No / Need clearer explanation
   - *Comments*:

---

## 4. Evidential Value & Proof Artifact Utility

1. **Proof Artifact Completeness**:
   - Does the proof artifact (`.brainfrog/proofs/<task_id>.json` and CLI `show-proof` output) contain enough concrete evidence for you to trust the change without manual re-testing?
     - [ ] Yes, comprehensive (commands, exit codes, diffs, SHA-256 hashes, logs)
     - [ ] Good, but missing some key details
     - [ ] Insufficient
   - *What additional data would you want recorded in a proof artifact?*

2. **Output Formatting & CLI Aesthetics**:
   - How was the CLI output readability (Rich boxes, tables, log presentation)?
     - [ ] Clean and legible
     - [ ] Too noisy / overwhelming
     - [ ] Rendering issues (broken borders, terminal encoding glitches)
   - *Terminal rendering observations*:

---

## 5. Issues, Bugs & Surprises

### Bugs Encountered
*(Please include command executed, expected behavior, and actual output/traceback)*

```
Command:
Error / Traceback:
```

### Confusing Messages or Unclear Errors
*(Were there any warnings, banners, or prompts that were difficult to interpret?)*

- 

### Feature Requests & Ergonomic Improvements
*(What would make BrainFrog indispensable in your daily development or CI workflow?)*

- 

---

## 6. Overall Verdict

- **Initial Impression Rating**: (1 = Poor, 5 = Excellent) `[ / 5 ]`
- **Would you use BrainFrog's proof verification as a merge gate in CI/CD?**:
  - [ ] Definitely yes
  - [ ] Probably yes, after further refinement
  - [ ] Not yet
  - [ ] No

**Additional Thoughts**:
