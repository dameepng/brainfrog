"""baseline_runner.py — CLI runner for BrainFrog Architecture Reasoning Evaluations.

Provides:
- Deterministic dry-run inspection of evaluation scenarios.
- Live model evaluation execution when a provider is configured.
- Clean pending-status reporting and reproducible procedure when credentials are not configured.
- Offline evaluation of recorded model responses against rubric criteria.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from evals.architecture_eval import (
    RUBRIC_DIMENSIONS,
    ArchitectureCase,
    ArchitectureStrategy,
    ContextType,
    CriticalFailure,
    EvaluationResult,
    RubricScore,
    load_architecture_cases,
)


def _safe_print(msg: str, file=None) -> None:
    """Print message safely across platform console encodings."""
    target = file or sys.stdout
    try:
        target.write(msg + "\n")
        target.flush()
    except UnicodeEncodeError:
        safe_msg = msg.encode("ascii", errors="replace").decode("ascii")
        target.write(safe_msg + "\n")
        target.flush()


def generate_recording_template(cases: List[ArchitectureCase]) -> Dict[str, Any]:
    """Generate a structured template for recording baseline model outputs and evaluations."""
    template_cases = []
    for c in cases:
        template_cases.append({
            "id": c.id,
            "title": c.title,
            "context_type_expected": c.context_type_expected.value,
            "prompt": c.prompt,
            "mode": c.provided_context.mode,
            "model_provider": "pending",
            "model_name": "pending",
            "raw_output": "",
            "inferred_context": "",
            "inferred_strategy": "",
            "rubric_scores": {dim: 0 for dim in RUBRIC_DIMENSIONS},
            "critical_failures_triggered": [],
            "notes": {},
            "status": "pending_execution",
        })
    return {
        "suite": "brainfrog-architecture-baseline",
        "version": "1.0.0",
        "cases_count": len(cases),
        "results": template_cases,
    }


def evaluate_offline_recording(recording_path: Path) -> List[EvaluationResult]:
    """Evaluate a recorded JSON file containing model responses against the rubric."""
    if not recording_path.exists():
        raise FileNotFoundError(f"Recording file not found: {recording_path}")

    raw_data = json.loads(recording_path.read_text(encoding="utf-8"))
    results: List[EvaluationResult] = []

    for entry in raw_data.get("results", []):
        cid = entry.get("id", "UNKNOWN")
        scores_dict = entry.get("rubric_scores", {})
        cfs_raw = entry.get("critical_failures_triggered", [])

        cfs = []
        for cf_val in cfs_raw:
            for enum_cf in CriticalFailure:
                if cf_val == enum_cf.value or cf_val == enum_cf.name:
                    cfs.append(enum_cf)

        rubric = RubricScore(
            context_classification=scores_dict.get("context_classification", 0),
            evidence_grounding=scores_dict.get("evidence_grounding", 0),
            compatibility_reasoning=scores_dict.get("compatibility_reasoning", 0),
            scope_discipline=scores_dict.get("scope_discipline", 0),
            strategy_quality=scores_dict.get("strategy_quality", 0),
            uncertainty_handling=scores_dict.get("uncertainty_handling", 0),
            verification_quality=scores_dict.get("verification_quality", 0),
            architecture_proportionality=scores_dict.get("architecture_proportionality", 0),
            execution_boundary_awareness=scores_dict.get("execution_boundary_awareness", 0),
            critical_failures_triggered=cfs,
            notes=entry.get("notes", {}),
        )

        res = EvaluationResult(
            case_id=cid,
            inferred_context=entry.get("inferred_context"),
            inferred_strategy=entry.get("inferred_strategy"),
            rubric_score=rubric,
            model_output_excerpt=entry.get("raw_output", "")[:200],
            is_successful_run=entry.get("status") == "completed",
        )
        results.append(res)

    return results


def run_live_baseline(
    cases: List[ArchitectureCase],
    provider_name: str = "antigravity",
    model_name: str = "gemini-3.8-flash-high",
    output_path: Optional[Path] = None,
    repo_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Execute evaluation cases through BrainFrog System 2 using configured provider."""
    import hashlib
    import subprocess
    import time
    from datetime import datetime, timezone
    from core.skills import index_skills, select_skill, load_skill_content
    from orchestrator import load_project_guidelines
    from system2.antigravity_client import AntigravitySystem2Client, find_antigravity_bin
    from system2.json_utils import _extract_json

    work_dir = (repo_dir or Path.cwd()).resolve()

    # 1. Determine Git metadata
    def _git_cmd(args: List[str]) -> str:
        try:
            res = subprocess.run(
                ["git"] + args,
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=5.0,
            )
            return res.stdout.strip()
        except Exception:
            return "unknown"

    commit_hash = _git_cmd(["rev-parse", "HEAD"])
    branch_name = _git_cmd(["branch", "--show-current"])

    # 2. Compute fixture hash
    fixture_path = work_dir / "tests" / "fixtures" / "evals" / "architecture_cases.json"
    fixture_sha256 = ""
    if fixture_path.exists():
        fixture_sha256 = hashlib.sha256(fixture_path.read_bytes()).hexdigest()

    # Ensure opt-in is active for Antigravity in evaluation environment
    os.environ["BRAINFROG_ALLOW_UNRESTRICTED_ANTIGRAVITY_CLI"] = "1"

    # Discover skills and load project guidelines
    skills = index_skills(work_dir)
    project_guidelines = load_project_guidelines(work_dir)

    metadata = {
        "suite": "brainfrog-architecture-baseline",
        "version": "1.0.0",
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "git_commit": commit_hash,
        "git_branch": branch_name,
        "provider": provider_name,
        "model": model_name,
        "model_effort": "high" if "high" in model_name else "default",
        "temperature": "cli_default",
        "execution_mode": "plan",
        "skill_selection_mode": "dynamic_intent_routing",
        "environment_config": {
            "BRAINFROG_ALLOW_UNRESTRICTED_ANTIGRAVITY_CLI": "1",
            "ANTIGRAVITY_BIN": find_antigravity_bin() or "unknown",
        },
        "fixture_path": str(fixture_path.relative_to(work_dir) if fixture_path.exists() else "unknown"),
        "fixture_sha256": fixture_sha256,
        "references_loaded": False,
        "total_scenarios": len(cases),
    }

    results: List[Dict[str, Any]] = []

    # Check for existing completed results to support safe resumption
    existing_by_id: Dict[str, Any] = {}
    if output_path and output_path.exists():
        try:
            existing_payload = json.loads(output_path.read_text(encoding="utf-8"))
            existing_meta = existing_payload.get("metadata", {})

            # Validate metadata compatibility before reusing completed records
            fixture_mismatch = (
                metadata.get("fixture_sha256")
                and existing_meta.get("fixture_sha256")
                and existing_meta["fixture_sha256"] != metadata["fixture_sha256"]
            )
            model_mismatch = (
                existing_meta.get("model") and existing_meta["model"] != metadata["model"]
            )
            provider_mismatch = (
                existing_meta.get("provider") and existing_meta["provider"] != metadata["provider"]
            )

            if fixture_mismatch or model_mismatch or provider_mismatch:
                _safe_print(
                    "[Baseline Runner] Warning: Existing output file metadata does not match current run "
                    f"(fixture mismatch: {fixture_mismatch}, model mismatch: {model_mismatch}, provider mismatch: {provider_mismatch}). "
                    "Ignoring incompatible records to prevent dataset corruption."
                )
            else:
                for r in existing_payload.get("results", []):
                    if r.get("status") == "completed" and r.get("raw_output"):
                        existing_by_id[r["id"]] = r
                if existing_by_id:
                    _safe_print(
                        f"[Baseline Runner] Found {len(existing_by_id)} already completed scenario(s) in {output_path} "
                        "with compatible metadata. Preserving completed outputs and resuming."
                    )
        except Exception:
            pass

    _safe_print("=" * 72)
    _safe_print(f"Executing Live BrainFrog Baseline ({model_name} via {provider_name})")
    _safe_print("=" * 72)

    for idx, case in enumerate(cases, 1):
        if case.id in existing_by_id:
            _safe_print(
                f"\n[{idx:02d}/{len(cases):02d}] Scenario {case.id} already completed -- preserving verbatim output."
            )
            results.append(existing_by_id[case.id])
            continue

        _safe_print(f"\n[{idx:02d}/{len(cases):02d}] Running Scenario {case.id}: {case.title} ...")
        t0 = time.time()

        # Dynamic skill intent routing
        selected_skill, _ = select_skill(case.prompt, skills)
        skill_name = selected_skill.name if selected_skill else None
        skill_content = load_skill_content(selected_skill) if selected_skill else ""

        # Compose guidelines matching Orchestrator
        guidelines = project_guidelines
        if skill_content:
            guidelines = f"{guidelines}\n\n{skill_content}".strip()

        client = AntigravitySystem2Client(
            model=model_name,
            guidelines=guidelines,
            allow_unrestricted=True,
        )

        system = (
            "You are a principal software engineer operating in PLAN mode. "
            "Your role is architectural exploration, PRD creation, and implementation planning. "
            "You must NOT generate diffs or edit code in this mode. "
            "Examine the provided repository files and structure carefully.\n\n"
            "Quality Guidelines:\n"
            "- For small, obvious tasks: keep it concise with 1-3 direct steps.\n"
            "- For large or ambiguous tasks: construct a practical, rigorous PRD.\n"
            "- Separate inspected codebase FACTS (citing specific files/functions) from your technical ASSUMPTIONS and PROPOSALS.\n"
            "- Define concrete, testable acceptance criteria.\n"
            "- Identify risks or trade-offs.\n"
            "- Ask clarifying questions ONLY if there are material, critical decisions not answered in the repo or prompt.\n\n"
            "Respond with ONLY a JSON object formatted as:\n"
            "{\n"
            '  "title": "Short descriptive title of the change",\n'
            '  "is_small_task": true,\n'
            '  "problem": "Problem statement and context",\n'
            '  "goals": ["Goal 1", "Goal 2"],\n'
            '  "scope": ["Included item 1"],\n'
            '  "non_scope": ["Excluded item 1"],\n'
            '  "codebase_findings": ["Fact 1 (citing file.py:function)", "Fact 2"],\n'
            '  "assumptions": ["Assumption/Proposal 1"],\n'
            '  "clarifying_questions": [],\n'
            '  "acceptance_criteria": ["Testable criterion 1", "Testable criterion 2"],\n'
            '  "steps": [{"id": "1", "description": "Step 1", "files": ["path/a.py"]}],\n'
            '  "relevant_files": ["path/a.py"],\n'
            '  "markdown_doc": "# Full formatted PRD and Plan in Markdown\\n\\n..."\n'
            "}\n"
            "No prose outside the JSON."
        )

        user_parts = [f"Task:\n{case.prompt}"]
        if case.provided_context.repo_tree:
            user_parts.append(f"Repository file tree:\n{case.provided_context.repo_tree}")
        if case.provided_context.files:
            user_parts.append(f"Relevant inspected files:\n{json.dumps(case.provided_context.files, indent=2)}")
        if case.provided_context.notes:
            user_parts.append(f"Notes:\n{case.provided_context.notes}")

        status = "failed"
        raw_output = ""
        parsed_output = None
        error_msg = None
        json_err = None

        try:
            raw_output = client._call(system, "\n\n".join(user_parts), max_tokens=4000)
            status = "completed"
            try:
                parsed_output = _extract_json(raw_output)
            except Exception as j_exc:
                json_err = str(j_exc)
        except Exception as exc:
            error_msg = str(exc)
            # Salvage raw JSON if present in Antigravity exception snippet
            if "response" in error_msg and '"status":"ERROR"' in error_msg:
                try:
                    import re
                    match = re.search(r'"response"\s*:\s*"((?:\\.|[^"\\])*)"', error_msg)
                    if match:
                        unescaped = bytes(match.group(1), "utf-8").decode("unicode_escape")
                        raw_output = unescaped
                        parsed_output = _extract_json(unescaped)
                        status = "completed"
                        error_msg = None
                except Exception:
                    pass

        elapsed = round(time.time() - t0, 2)
        _safe_print(f"  -> Finished in {elapsed}s (status: {status}, skill: {skill_name or 'none'})")

        record = {
            "id": case.id,
            "title": case.title,
            "context_type_expected": case.context_type_expected.value,
            "prompt": case.prompt,
            "provided_context": {
                "repo_tree": case.provided_context.repo_tree,
                "files": case.provided_context.files,
                "mode": case.provided_context.mode,
                "notes": case.provided_context.notes,
            },
            "selected_skill": skill_name,
            "status": status,
            "latency_seconds": elapsed,
            "tokens": {
                "input_tokens": 0,
                "output_tokens": 0,
            },
            "raw_output": raw_output,
            "parsed_output": parsed_output,
            "json_parse_error": json_err,
            "error": error_msg,
        }
        results.append(record)

        # Checkpoint incrementally so interruptions preserve completed scenarios
        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps({"metadata": metadata, "results": results}, indent=2),
                encoding="utf-8",
            )

    output_payload = {
        "metadata": metadata,
        "results": results,
    }

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(output_payload, indent=2), encoding="utf-8")
        _safe_print(f"\n[Baseline Runner] Saved {len(results)} baseline results to: {output_path}")

    return output_payload


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="BrainFrog Architecture Reasoning Baseline Evaluation Runner"
    )
    parser.add_argument(
        "--cases",
        type=str,
        default=None,
        help="Path to architecture cases JSON file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect evaluation cases without calling model providers",
    )
    parser.add_argument(
        "--generate-template",
        type=str,
        default=None,
        help="Generate empty recording template JSON to specified path",
    )
    parser.add_argument(
        "--evaluate-offline",
        type=str,
        default=None,
        help="Evaluate an existing recording JSON file against rubric criteria",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Path to save evaluation summary JSON or Markdown",
    )
    parser.add_argument(
        "--provider",
        type=str,
        default=None,
        help="Target provider (e.g. claude, openai, antigravity)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Target model identifier (e.g. gemini-3.8-flash-high)",
    )

    args = parser.parse_args(argv)
    cases_path = Path(args.cases) if args.cases else None

    try:
        cases = load_architecture_cases(cases_path)
    except Exception as e:
        _safe_print(f"Error loading evaluation cases: {e}", file=sys.stderr)
        return 1

    if args.generate_template:
        out_path = Path(args.generate_template)
        tmpl = generate_recording_template(cases)
        out_path.write_text(json.dumps(tmpl, indent=2), encoding="utf-8")
        _safe_print(f"[Baseline Runner] Generated template with {len(cases)} cases at: {out_path}")
        return 0

    if args.evaluate_offline:
        rec_path = Path(args.evaluate_offline)
        _safe_print(f"[Baseline Runner] Evaluating offline recording: {rec_path}")
        results = evaluate_offline_recording(rec_path)
        passed_count = sum(1 for r in results if r.rubric_score.passed)
        for r in results:
            _safe_print(f"  {r.summary()}")
        _safe_print(f"\nTotal: {passed_count}/{len(results)} passed.")
        return 0 if passed_count == len(results) else 1

    if args.dry_run or not args.provider:
        _safe_print("=" * 72)
        _safe_print("BrainFrog Architecture Reasoning Baseline Evaluation Suite (Dry-Run)")
        _safe_print("=" * 72)
        _safe_print(f"Total Scenarios Loaded: {len(cases)}\n")

        for idx, c in enumerate(cases, 1):
            _safe_print(f"Scenario {idx:02d}: {c.id} -- {c.title}")
            _safe_print(f"  * Expected Context : {c.context_type_expected.value.upper()}")
            _safe_print(f"  * Acceptable Strats: {', '.join(s.value for s in c.acceptable_strategies)}")
            _safe_print(f"  * Mode             : {c.provided_context.mode.upper()}")
            _safe_print(f"  * Task Prompt      : {c.prompt[:80]}...")
            _safe_print(f"  * Rubric Focus     : {', '.join(c.rubric_focus)}")
            _safe_print("")

        _safe_print("-" * 72)
        _safe_print("Status: Baseline suite defined and validated.")
        _safe_print("To execute live baseline when provider API credentials are configured:")
        _safe_print("  python -m evals.baseline_runner --provider antigravity --model gemini-3.8-flash-high --output evals/results/architecture-baseline-gemini-3.8-flash-high.json")
        _safe_print("-" * 72)
        return 0

    # Live provider execution
    provider = args.provider.lower().strip()
    model = args.model or "gemini-3.8-flash-high"
    out_file = Path(args.output) if args.output else Path("evals/results/architecture-baseline-gemini-3.8-flash-high.json")

    run_live_baseline(
        cases=cases,
        provider_name=provider,
        model_name=model,
        output_path=out_file,
    )
    return 0


if __name__ == "__main__":
    import os
    sys.exit(main())
