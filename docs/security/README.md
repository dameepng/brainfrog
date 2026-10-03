# BrainFrog Security Documentation

Index of security findings addressed during the Phase 14 hardening cycle.

- Red-team reproductions: [phase-14/findings.md](phase-14/findings.md)
- Remediation reports: [phase-14/remediation.md](phase-14/remediation.md)

## Finding Status

| ID | Severity | Title | Status | Fix commit | Permanent regression tests |
|---|---|---|---|---|---|
| C-01 | Critical | Approval scope bleed | CLOSED | `77a4be1` | `tests/test_approval_execution_contract.py` |
| C-02 | Critical | Path traversal in file writes | CLOSED | `77a4be1` | `tests/test_approval_execution_contract.py` |
| H-01 | High | Cross-process approval TOCTOU | CLOSED | `77a4be1` | `tests/test_approval_process_concurrency.py` |
| H-02 | High | Stale approval after session reset | CLOSED | `bd6143f` | `tests/test_approval_session_invalidation.py` |
| H-03 | High | Automatic remote Git push | CLOSED | `bd6143f` | `tests/test_remote_git_push_boundary.py` |
| M-01 | Medium | Prompt history poisoning during `/exec` | CLOSED | `9d06b8c` | `tests/test_approval_prompt_history_boundary.py` |
| M-02 | Medium | Heuristic target extraction | CLOSED | `f8c59f5` | `tests/test_target_extraction_security.py` |
| M-03 | Medium | Approval flooding / directory scan DoS | CLOSED | `a0efee4` | `tests/test_approval_flood_protection.py` |
| L-01 | Low | Session history growth / O(N²) persistence | CLOSED | `86e12e9` | `tests/test_session_history_bounds.py` |
| L01.1-F01 | High | Stale snapshot lost update | CLOSED | `46afc9a` | `tests/test_session_concurrency_remediation.py` |
| L01.1-F02 | High | Stale save resurrecting `/reset` state (H-02 boundary) | CLOSED | `46afc9a` | `tests/test_session_concurrency_remediation.py` |

> [!NOTE]
> Detailed reports exist only for M-03, L-01, and L01.1-F01/F02. C-01 through M-02
> are documented by their fix commit messages and regression test suites.
> L01.1-F01/F02 were discovered during the L-01 post-remediation red-team
> (see findings.md Part B).
