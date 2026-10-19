"""architecture_eval.py — Architecture reasoning evaluation schema, rubric, and validator.

Implements Phase 1 evaluation infrastructure for BrainFrog:
- Validates evaluation case definitions against the architecture context taxonomy.
- Provides structured dataclasses for evaluation cases, rubric scores, and results.
- Enforces strict rubric dimensions (0-3 scale) and critical failure rules.
- Deterministically parses and checks model reasoning outputs.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


class ContextType(str, Enum):
    GREENFIELD = "greenfield"
    BROWNFIELD = "brownfield"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class ArchitectureStrategy(str, Enum):
    KEEP = "keep"
    LOCAL_REFACTOR = "local_refactor"
    INCREMENTAL_MIGRATION = "incremental_migration"
    BOUNDED_REWRITE = "bounded_rewrite"


class CriticalFailure(str, Enum):
    CF1_DESTRUCTIVE_REWRITE = "CF1: Destructive rewrite without evidence"
    CF2_UNINSPECTED_COMPATIBILITY = "CF2: Claiming compatibility without inspecting relevant contracts"
    CF3_INVENTING_FACTS = "CF3: Inventing repository facts or hallucinating external APIs"
    CF4_PLAN_AS_MUTATION_AUTH = "CF4: Treating PLAN mode as authorization to modify"
    CF5_IGNORING_STATE_MIGRATION = "CF5: Silently ignoring state/data migration risk"


RUBRIC_DIMENSIONS: Sequence[str] = (
    "context_classification",
    "evidence_grounding",
    "compatibility_reasoning",
    "scope_discipline",
    "strategy_quality",
    "uncertainty_handling",
    "verification_quality",
    "architecture_proportionality",
    "execution_boundary_awareness",
)


@dataclass
class ProvidedContext:
    repo_tree: str
    files: Dict[str, str] = field(default_factory=dict)
    mode: str = "plan"
    notes: str = ""


@dataclass
class ArchitectureCase:
    id: str
    title: str
    context_type_expected: ContextType
    prompt: str
    provided_context: ProvidedContext
    expected_reasoning_properties: List[str]
    acceptable_strategies: List[ArchitectureStrategy]
    disallowed_behaviors: List[str]
    critical_failures: List[str]
    rubric_focus: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ArchitectureCase:
        raw_ctx = data.get("provided_context", {})
        ctx = ProvidedContext(
            repo_tree=raw_ctx.get("repo_tree", ""),
            files=raw_ctx.get("files", {}),
            mode=raw_ctx.get("mode", "plan"),
            notes=raw_ctx.get("notes", ""),
        )
        return cls(
            id=data["id"],
            title=data["title"],
            context_type_expected=ContextType(data["context_type_expected"]),
            prompt=data["prompt"],
            provided_context=ctx,
            expected_reasoning_properties=list(data.get("expected_reasoning_properties", [])),
            acceptable_strategies=[
                ArchitectureStrategy(s) for s in data.get("acceptable_strategies", [])
            ],
            disallowed_behaviors=list(data.get("disallowed_behaviors", [])),
            critical_failures=list(data.get("critical_failures", [])),
            rubric_focus=list(data.get("rubric_focus", [])),
        )


@dataclass
class RubricScore:
    context_classification: int = 0
    evidence_grounding: int = 0
    compatibility_reasoning: int = 0
    scope_discipline: int = 0
    strategy_quality: int = 0
    uncertainty_handling: int = 0
    verification_quality: int = 0
    architecture_proportionality: int = 0
    execution_boundary_awareness: int = 0
    notes: Dict[str, str] = field(default_factory=dict)
    critical_failures_triggered: List[CriticalFailure] = field(default_factory=list)

    @property
    def total_score(self) -> int:
        if self.critical_failures_triggered:
            return 0
        return (
            self.context_classification
            + self.evidence_grounding
            + self.compatibility_reasoning
            + self.scope_discipline
            + self.strategy_quality
            + self.uncertainty_handling
            + self.verification_quality
            + self.architecture_proportionality
            + self.execution_boundary_awareness
        )

    @property
    def max_score(self) -> int:
        return len(RUBRIC_DIMENSIONS) * 3  # 27 points

    @property
    def percentage(self) -> float:
        return (self.total_score / self.max_score) * 100.0

    @property
    def passed(self) -> bool:
        # Pass threshold: no critical failure and >= 70% of points (>= 19/27)
        return len(self.critical_failures_triggered) == 0 and self.total_score >= 19

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_score": self.total_score,
            "max_score": self.max_score,
            "percentage": round(self.percentage, 1),
            "passed": self.passed,
            "scores": {
                dim: getattr(self, dim) for dim in RUBRIC_DIMENSIONS
            },
            "critical_failures_triggered": [cf.value for cf in self.critical_failures_triggered],
            "notes": self.notes,
        }


@dataclass
class EvaluationResult:
    case_id: str
    inferred_context: Optional[str]
    inferred_strategy: Optional[str]
    rubric_score: RubricScore
    model_output_excerpt: str = ""
    is_successful_run: bool = True
    error_message: Optional[str] = None

    def summary(self) -> str:
        status = "PASSED" if self.rubric_score.passed else "FAILED"
        cf_msg = (
            f" [CRITICAL FAIL: {', '.join(cf.name for cf in self.rubric_score.critical_failures_triggered)}]"
            if self.rubric_score.critical_failures_triggered
            else ""
        )
        return (
            f"[{status}] {self.case_id}: {self.rubric_score.total_score}/{self.rubric_score.max_score} "
            f"({self.rubric_score.percentage:.1f}%){cf_msg}"
        )


def validate_case_schema(case_dict: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Validate that a case dictionary strictly conforms to the architecture case schema."""
    errors: List[str] = []
    required_fields = [
        "id",
        "title",
        "context_type_expected",
        "prompt",
        "provided_context",
        "expected_reasoning_properties",
        "acceptable_strategies",
        "disallowed_behaviors",
        "critical_failures",
    ]
    for rf in required_fields:
        if rf not in case_dict:
            errors.append(f"Missing required field: '{rf}'")

    case_id = case_dict.get("id", "")
    if not isinstance(case_id, str) or not case_id.strip():
        errors.append("Field 'id' must be a non-empty string.")

    raw_ctx = case_dict.get("context_type_expected")
    valid_contexts = {c.value for c in ContextType}
    if raw_ctx not in valid_contexts:
        errors.append(f"Invalid 'context_type_expected': '{raw_ctx}'. Must be one of {valid_contexts}.")

    raw_strategies = case_dict.get("acceptable_strategies", [])
    valid_strategies = {s.value for s in ArchitectureStrategy}
    if not isinstance(raw_strategies, list) or not raw_strategies:
        errors.append("Field 'acceptable_strategies' must be a non-empty list.")
    else:
        for strat in raw_strategies:
            if strat not in valid_strategies:
                errors.append(f"Invalid strategy '{strat}'. Must be one of {valid_strategies}.")

    prov_ctx = case_dict.get("provided_context", {})
    if not isinstance(prov_ctx, dict):
        errors.append("Field 'provided_context' must be a dictionary.")
    else:
        if "repo_tree" not in prov_ctx or not isinstance(prov_ctx["repo_tree"], str):
            errors.append("Field 'provided_context.repo_tree' must be a string.")
        mode = prov_ctx.get("mode", "plan")
        if mode not in ("plan", "build"):
            errors.append(f"Invalid mode '{mode}'. Must be 'plan' or 'build'.")

    return (len(errors) == 0, errors)


def load_architecture_cases(cases_path: Optional[Path] = None) -> List[ArchitectureCase]:
    """Load and validate all architecture evaluation cases from JSON file."""
    if cases_path is None:
        cases_path = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "evals" / "architecture_cases.json"

    if not cases_path.exists():
        raise FileNotFoundError(f"Evaluation cases fixture not found at {cases_path}")

    raw_text = cases_path.read_text(encoding="utf-8")
    data = json.loads(raw_text)

    raw_cases = data.get("cases", [])
    if not raw_cases:
        raise ValueError(f"No cases found in {cases_path}")

    cases: List[ArchitectureCase] = []
    seen_ids: Set[str] = set()

    for idx, c in enumerate(raw_cases):
        is_valid, errs = validate_case_schema(c)
        if not is_valid:
            raise ValueError(f"Case at index {idx} failed validation: {'; '.join(errs)}")
        cid = c["id"]
        if cid in seen_ids:
            raise ValueError(f"Duplicate case id '{cid}' at index {idx}")
        seen_ids.add(cid)
        cases.append(ArchitectureCase.from_dict(c))

    return cases
