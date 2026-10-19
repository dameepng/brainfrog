# Phase 1–4 Publication Manifest

**Repository:** dameepng/brainfrog
**Target Base:** origin/main (0541c8ab2966efb175854e40405202e0c064b8b)
**Publication Branch:**
eat/agent-friendly-architecture-phase1-4
**Date:** October 19, 2026

---

## Publication Inventory

| Phase | File Path | Status | SHA-256 (First 8) | Role |
|---|---|---|---|---|
| Phase 1 | docs/agent-friendly-architecture.md | New | e4bab579 | Runtime spec |
| Phase 1 | docs/evals/architecture-reasoning.md | New | 7901a5d7 | Evaluation rubric |
| Phase 1 | docs/evals/baseline-report.md | New | 7f544ced | Baseline report |
| Phase 1 | docs/evals/baseline-integrity-audit.md | New | d659a524 | Baseline audit report |
| Phase 1 | tests/fixtures/evals/architecture_cases.json | New | 81335247 | Canonical eval cases |
| Phase 1 | evals/__init__.py | New | a635d84f | Evaluation harness |
| Phase 1 | evals/architecture_eval.py | New | 378c6675 | Evaluation harness |
| Phase 1 | evals/baseline_runner.py | New | 195eb2dd | Baseline runner |
| Phase 1 | tests/test_architecture_eval.py | New | a4b7f0f0 | Evaluation tests |
| Phase 1 | evals/results/architecture-baseline-gemini-3.8-flash-high.json | New | 4a45a67b | Frozen baseline data |
| Phase 1 | evals/results/architecture-baseline-gemini-3.8-flash-high-scored.json | New | 4aa78ed2 | Frozen scored baseline |
| Phase 2 | .brainfrog/skills/implement-code-change/SKILL.md | Modified | 8cd2aadb | Skill instructions |
| Phase 2 | .brainfrog/skills/implement-code-change/references/examples.md | Modified | 8ed68d9f | Skill examples |
| Phase 2 | .brainfrog/skills/refactor-code-safely/SKILL.md | Modified | e0cd405a | Skill instructions |
| Phase 2 | .brainfrog/skills/refactor-code-safely/references/examples.md | Modified | ec5664a7 | Skill examples |
| Phase 2 | .brainfrog/skills/review-code-change/SKILL.md | Modified | fdc1c560 | Skill instructions |
| Phase 2 | .brainfrog/skills/review-code-change/references/examples.md | Modified | 4059b8ed | Skill examples |
| Phase 2 | .brainfrog/skills/understand-codebase/SKILL.md | Modified | ddbeb902 | Skill instructions |
| Phase 2 | .brainfrog/skills/understand-codebase/references/examples.md | Modified | 603c9269 | Skill examples |
| Phase 2 | docs/evals/architecture-phase2-report.md | New | 9f95080a | Phase 2 evaluation report |
| Phase 2 | docs/evals/architecture-phase2-closure.md | New | 50a33ec8 | Phase 2 closure audit |
| Phase 2 | evals/results/architecture-phase2-gemini-3.8-flash-high.json | New | 51db12d1 | Phase 2 eval results |
| Phase 2 | evals/results/architecture-phase2-gemini-3.8-flash-high-scored.json | New | 6c9b68c6 | Phase 2 scored eval |
| Phase 2 | tests/test_engineering_skills.py | Modified | b23e56c4 | Engineering skill tests |
| Phase 3 | core/plans.py | Modified | 4b5f20f0 | Plan domain model & schema |
| Phase 3 | core/runtime/contract.py | Modified | 0e7b409f | Execution contract plan binding |
| Phase 3 | core/runtime/approval.py | Modified | 6b2e38d9 | Approval domain model & binding |
| Phase 4 | core/runtime/step_runner.py | New | bbc52147 | Bounded step runner & recovery |
| Phase 4 | core/runtime/runtime.py | Modified | 76b93c3f | Plan commands & clarification |
| Phase 4 | core/runtime/__init__.py | Modified | fea1ea3d | Runtime package exports |
| Phase 4 | core/__init__.py | Modified | 52a510f8 | Core package exports |
| Phase 4 | orchestrator.py | Modified | 744649b0 | Step runner wiring & boundaries |
| Phase 4 | cli.py | Modified | 46f989f2 | CLI plan & mode routing |
| Phase 4 | core/runtime/session.py | Modified | 49cca6f3 | Session plan binding |
| Phase 4 | core/runtime/permissions.py | Modified | d819abaa | Mode & model permissions |
| Phase 4 | core/runtime/model_switcher.py | New | 8a963d47 | Dynamic model switcher |
| Phase 4 | core/runtime/workflow.py | Modified | 57a943fd | Workflow execution facade |
| Phase 4 | brainfrog/sdk/client.py | Modified | 37967ba6 | SDK scope traversal hardening |
| Phase 3 | system2/antigravity_client.py | Modified | bc3b3ed7 | System 2 Antigravity provider |
| Phase 3 | system2/claude_client.py | Modified | c4081dbe | System 2 Claude provider |
| Phase 3 | system2/openai_client.py | Modified | a91b4d99 | System 2 OpenAI provider |
| Phase 3 | system2/json_utils.py | Modified | 8d53bfda | Root-first JSON auto-repair |
| Phase 3 | docs/architecture-plan-build-handoff.md | New | 2fa7bd10 | Handoff specification |
| Phase 4 | docs/architecture-clarification-recovery.md | New | cb35ed4c | Clarification & recovery spec |
| Phase 3 | docs/evals/architecture-phase3-security-audit.md | New | f666a8a1 | Phase 3 security audit |
| Phase 3 | docs/evals/architecture-phase3-final-security.md | New | 048f84ce | Phase 3 final security audit |
| Phase 4 | docs/evals/architecture-phase4-final-integrity.md | New | 76f56725 | Phase 4 integrity audit |
| Phase 3 | tests/approval_flow_helpers.py | New | 2f1ac8b3 | Approval test fixtures |
| Phase 3 | tests/test_architecture_plan_build_handoff.py | New | 162c86e2 | Handoff integration tests |
| Phase 3 | tests/test_architecture_handoff_adversarial.py | New | 048371a9 | Adversarial handoff tests |
| Phase 3 | tests/test_architecture_handoff_final_security.py | New | d3779358 | Security closure tests |
| Phase 4 | tests/test_architecture_clarification_integrity.py | New | 9364b98f | Clarification integrity tests |
| Phase 4 | tests/test_architecture_clarification_resume.py | New | c0bbe746 | Resume & drift recovery tests |
| Phase 4 | tests/test_phase4b_autonomous_execution.py | New | 9d22a927 | Autonomous execution tests |
| Phase 4 | tests/test_phase4a_build_loop.py | New | 8c2ebc7a | Build loop tests |
| Phase 4 | tests/test_phase4b1_execution_recovery_audit.py | New | 6d68c993 | Recovery audit tests |
| Phase 4 | tests/test_phase4b2_recovery_evidence_audit.py | New | 72d6529f | Recovery evidence tests |
| Phase 4 | tests/test_phase4a1_safety_audit.py | New | dded68a1 | Safety audit tests |
| Phase 4 | tests/test_phase4a2_final_audit.py | New | ce3aa135 | Final audit tests |
| Phase 4 | tests/test_phase4a3_adversarial_gate.py | New | 3222d0a5 | Adversarial gate tests |
| Phase 4 | tests/test_phase2b_plan_bound_execution.py | New | 7d2e6e18 | Plan-bound execution tests |
| Phase 4 | tests/test_phase3c_adversarial_audit.py | New | 8b0a6c38 | Adversarial audit tests |
| Phase 4 | tests/test_mode_contracts.py | New | 1bad8c81 | Mode contract tests |
| Phase 3 | tests/test_modes.py | Modified | b9b4267d | Fail-closed build mode tests |
| Phase 3 | tests/test_plan_registry.py | New | 9b2cd060 | Plan registry tests |
| Governance | .agents/rules/safe_test_fixtures_and_git_hygiene.md | New | 400e8b3d | Binding engineering rule |
| Governance | AGENTS.md | New | c76831f0 | Instruction & rules index |
| Hygiene | git_guard.py | Modified | 08908bf2 | Local secret guard |
| Hygiene | security/git_guard.py | Modified | 580ec0fb | Core secret scanner |
| Hygiene | security/__init__.py | Modified | 3f0348a9 | Security package exports |

---

## Explicit Scope Exclusions

The following items are intentionally excluded from this publication branch:
- **Phase 5 Work:** All Phase 5 brownfield pilot artifacts (docs/evals/phase5/*, phase5_snapshot_manifest.json).
- **Unrelated Branches:** In-progress experiments (docs/security/phase-16-execution-boundary-hardening.md, 	ests/test_telegram_*, etc.).
- **Local Secrets & State:** .env, .brainfrog/workspace.lock, .pytest_cache, IDE state.
- **Pre-existing Commits:** Changes already merged into origin/main via PR #43 (
eat/engineering-skills).
