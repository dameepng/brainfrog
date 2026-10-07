"""Planning Domain and Plan Generation Boundary.

Defines the Plan and PlanStep domain models, planner abstractions,
deterministic baseline planner, and strict validation logic.

Architectural Invariant:
A Plan is DESCRIPTIVE data only.
A Plan is NOT authority.
A Plan does not execute anything, mint execution contracts,
create approvals, or grant capabilities.
"""
from __future__ import annotations

import hashlib
import math
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Sequence, Set, Tuple, Union, runtime_checkable

from core.runtime.approval import RiskClass
from core.runtime.contract import reject_secrets
from core.runtime.work import Work, WorkStatus


# Resource Bounds
DEFAULT_MAX_PLAN_STEPS: int = 64
DEFAULT_MAX_STEP_DESC_LEN: int = 2048
DEFAULT_MAX_STEP_OUTCOME_LEN: int = 2048
DEFAULT_MAX_OBJECTIVE_LEN: int = 4096
DEFAULT_MAX_ASSUMPTIONS_COUNT: int = 32
DEFAULT_MAX_RISKS_COUNT: int = 32
DEFAULT_MAX_VERIFICATIONS_COUNT: int = 32
DEFAULT_MAX_DEPENDENCIES_PER_STEP: int = 32
DEFAULT_MAX_ITEM_TEXT_LEN: int = 2048
DEFAULT_MAX_ID_LEN: int = 128


@dataclass(frozen=True)
class PlanStep:
    """Immutable, descriptive step within a Plan.

    A PlanStep answers 'What should be done?' and what outcome is expected.
    It contains no execution logic, authority, or tool bindings.
    """

    id: str
    description: str
    expected_outcome: str = ""
    dependencies: Tuple[str, ...] = ()
    risk: RiskClass = RiskClass.LOW

    def __post_init__(self) -> None:
        # Validate id
        if type(self.id) is not str or not self.id.strip():
            raise ValueError("PlanStep id must be a non-empty string")
        if len(self.id) > DEFAULT_MAX_ID_LEN:
            raise ValueError(f"PlanStep id exceeds maximum length of {DEFAULT_MAX_ID_LEN}")
        if not re.match(r"^[a-zA-Z0-9_\-]+$", self.id):
            raise ValueError(f"PlanStep id contains invalid characters: '{self.id}'")

        # Validate description
        if type(self.description) is not str or not self.description.strip():
            raise ValueError("PlanStep description must be a non-empty string")
        if len(self.description) > DEFAULT_MAX_STEP_DESC_LEN:
            raise ValueError(
                f"PlanStep description exceeds maximum length of {DEFAULT_MAX_STEP_DESC_LEN}"
            )

        # Validate expected_outcome
        if type(self.expected_outcome) is not str:
            raise ValueError("PlanStep expected_outcome must be a string")
        if len(self.expected_outcome) > DEFAULT_MAX_STEP_OUTCOME_LEN:
            raise ValueError(
                f"PlanStep expected_outcome exceeds maximum length of {DEFAULT_MAX_STEP_OUTCOME_LEN}"
            )

        # Validate dependencies
        if not isinstance(self.dependencies, (tuple, list, set, frozenset)):
            raise ValueError("PlanStep dependencies must be a sequence of strings")
        if len(self.dependencies) > DEFAULT_MAX_DEPENDENCIES_PER_STEP:
            raise ValueError(
                f"PlanStep dependencies count exceeds limit of {DEFAULT_MAX_DEPENDENCIES_PER_STEP}"
            )
        norm_deps: List[str] = []
        for d in self.dependencies:
            if type(d) is not str or not d.strip():
                raise ValueError("PlanStep dependency item must be a non-empty string")
            if len(d) > DEFAULT_MAX_ID_LEN:
                raise ValueError(
                    f"PlanStep dependency item exceeds maximum length of {DEFAULT_MAX_ID_LEN}"
                )
            norm_deps.append(d.strip())
        object.__setattr__(self, "dependencies", tuple(norm_deps))

        # Validate & normalize risk
        norm_risk: RiskClass
        if isinstance(self.risk, RiskClass):
            norm_risk = self.risk
        elif isinstance(self.risk, str):
            try:
                norm_risk = RiskClass(self.risk.strip().upper())
            except ValueError as exc:
                raise ValueError(
                    f"Invalid risk value: '{self.risk}'. Allowed: {[r.value for r in RiskClass]}"
                ) from exc
        else:
            raise ValueError(f"PlanStep risk must be RiskClass or str, got {type(self.risk)}")
        object.__setattr__(self, "risk", norm_risk)

        # Reject secrets across all string/sequence fields
        reject_secrets({
            "id": self.id,
            "description": self.description,
            "expected_outcome": self.expected_outcome,
            "dependencies": self.dependencies,
        })

    def to_dict(self) -> Dict[str, Any]:
        """Serialize PlanStep to JSON-safe dictionary."""
        data: Dict[str, Any] = {
            "id": self.id,
            "description": self.description,
            "expected_outcome": self.expected_outcome,
            "dependencies": list(self.dependencies),
            "risk": self.risk.value,
        }
        reject_secrets(data)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> PlanStep:
        """Deserialize PlanStep with strict validation."""
        if type(data) is not dict:
            raise ValueError("Expected dictionary for PlanStep.from_dict")
        reject_secrets(data)
        expected_keys = {"id", "description", "expected_outcome", "dependencies", "risk"}
        extra_keys = set(data.keys()) - expected_keys
        if extra_keys:
            raise ValueError(f"Unknown PlanStep fields: {sorted(extra_keys)}")
        for req in ("id", "description"):
            if req not in data:
                raise ValueError(f"Missing required field in PlanStep: '{req}'")
        return cls(
            id=data["id"],
            description=data["description"],
            expected_outcome=data.get("expected_outcome", ""),
            dependencies=data.get("dependencies", ()),
            risk=data.get("risk", RiskClass.LOW),
        )


def _validate_step_dependencies(steps: Sequence[PlanStep]) -> None:
    """Validate dependency integrity and cycle-freedom among plan steps."""
    step_ids: Set[str] = set()
    for s in steps:
        if s.id in step_ids:
            raise ValueError(f"Duplicate step ID in plan: '{s.id}'")
        step_ids.add(s.id)

    # Check existence and self-dependency
    for s in steps:
        if s.id in s.dependencies:
            raise ValueError(f"Step '{s.id}' cannot depend on itself")
        for dep in s.dependencies:
            if dep not in step_ids:
                raise ValueError(f"Step '{s.id}' references nonexistent dependency '{dep}'")

    # Cycle detection via depth-first search graph coloring:
    # 0 = unvisited, 1 = visiting (in current recursion stack), 2 = visited
    state: Dict[str, int] = {s.id: 0 for s in steps}
    adj: Dict[str, Sequence[str]] = {s.id: s.dependencies for s in steps}

    def has_cycle(u: str) -> bool:
        state[u] = 1
        for v in adj[u]:
            if state[v] == 1:
                return True
            if state[v] == 0:
                if has_cycle(v):
                    return True
        state[u] = 2
        return False

    for s in steps:
        if state[s.id] == 0:
            if has_cycle(s.id):
                raise ValueError(f"Dependency cycle detected in plan involving step '{s.id}'")


@dataclass(frozen=True)
class Plan:
    """Immutable, descriptive plan representing proposed work steps.

    A Plan answers 'What should be done?'
    It is descriptive data only. It confers no execution authority,
    mints no capability contracts, and does not execute code.
    """

    id: str = field(default_factory=lambda: f"plan_{secrets.token_hex(16)}")
    work_id: str = ""
    objective: str = ""
    steps: Tuple[PlanStep, ...] = ()
    assumptions: Tuple[str, ...] = ()
    risks: Tuple[str, ...] = ()
    expected_verification: Tuple[str, ...] = ()
    created_at: float = field(default_factory=time.time)

    @property
    def plan_id(self) -> str:
        """Alias for plan ID."""
        return self.id

    @property
    def ordered_steps(self) -> Tuple[PlanStep, ...]:
        """Alias for steps tuple."""
        return self.steps

    def __post_init__(self) -> None:
        # Validate id
        if type(self.id) is not str or not self.id.strip():
            raise ValueError("Plan id must be a non-empty string")
        if len(self.id) > DEFAULT_MAX_ID_LEN:
            raise ValueError(f"Plan id exceeds maximum length of {DEFAULT_MAX_ID_LEN}")

        # Validate work_id
        if type(self.work_id) is not str:
            raise ValueError("Plan work_id must be a string")
        if len(self.work_id) > DEFAULT_MAX_ID_LEN:
            raise ValueError(f"Plan work_id exceeds maximum length of {DEFAULT_MAX_ID_LEN}")

        # Validate objective
        if type(self.objective) is not str:
            raise ValueError("Plan objective must be a string")
        if len(self.objective) > DEFAULT_MAX_OBJECTIVE_LEN:
            raise ValueError(f"Plan objective exceeds maximum length of {DEFAULT_MAX_OBJECTIVE_LEN}")

        # Validate steps
        if not isinstance(self.steps, (tuple, list)):
            raise ValueError("Plan steps must be a sequence of PlanStep instances")
        if len(self.steps) > DEFAULT_MAX_PLAN_STEPS:
            raise ValueError(f"Plan steps count exceeds limit of {DEFAULT_MAX_PLAN_STEPS}")
        for s in self.steps:
            if not isinstance(s, PlanStep):
                raise ValueError(f"All plan steps must be PlanStep instances, got {type(s)}")
        norm_steps = tuple(self.steps)
        object.__setattr__(self, "steps", norm_steps)

        # Validate step dependencies & cycle detection
        _validate_step_dependencies(norm_steps)

        # Validate assumptions
        if not isinstance(self.assumptions, (tuple, list, set, frozenset)):
            raise ValueError("Plan assumptions must be a sequence of strings")
        if len(self.assumptions) > DEFAULT_MAX_ASSUMPTIONS_COUNT:
            raise ValueError(
                f"Plan assumptions count exceeds limit of {DEFAULT_MAX_ASSUMPTIONS_COUNT}"
            )
        norm_assumptions: List[str] = []
        for a in self.assumptions:
            if type(a) is not str:
                raise ValueError("All assumptions must be strings")
            if len(a) > DEFAULT_MAX_ITEM_TEXT_LEN:
                raise ValueError(
                    f"Assumption item exceeds maximum length of {DEFAULT_MAX_ITEM_TEXT_LEN}"
                )
            norm_assumptions.append(a)
        object.__setattr__(self, "assumptions", tuple(norm_assumptions))

        # Validate risks
        if not isinstance(self.risks, (tuple, list, set, frozenset)):
            raise ValueError("Plan risks must be a sequence of strings")
        if len(self.risks) > DEFAULT_MAX_RISKS_COUNT:
            raise ValueError(f"Plan risks count exceeds limit of {DEFAULT_MAX_RISKS_COUNT}")
        norm_risks: List[str] = []
        for r in self.risks:
            if type(r) is not str:
                raise ValueError("All risks must be strings")
            if len(r) > DEFAULT_MAX_ITEM_TEXT_LEN:
                raise ValueError(
                    f"Risk item exceeds maximum length of {DEFAULT_MAX_ITEM_TEXT_LEN}"
                )
            norm_risks.append(r)
        object.__setattr__(self, "risks", tuple(norm_risks))

        # Validate expected_verification
        if not isinstance(self.expected_verification, (tuple, list, set, frozenset)):
            raise ValueError("Plan expected_verification must be a sequence of strings")
        if len(self.expected_verification) > DEFAULT_MAX_VERIFICATIONS_COUNT:
            raise ValueError(
                f"Plan expected_verification count exceeds limit of {DEFAULT_MAX_VERIFICATIONS_COUNT}"
            )
        norm_verifications: List[str] = []
        for v in self.expected_verification:
            if type(v) is not str:
                raise ValueError("All expected_verification items must be strings")
            if len(v) > DEFAULT_MAX_ITEM_TEXT_LEN:
                raise ValueError(
                    f"Verification item exceeds maximum length of {DEFAULT_MAX_ITEM_TEXT_LEN}"
                )
            norm_verifications.append(v)
        object.__setattr__(self, "expected_verification", tuple(norm_verifications))

        # Timestamps validation
        if not isinstance(self.created_at, (int, float)) or math.isnan(self.created_at) or math.isinf(self.created_at):
            raise ValueError("created_at must be a valid finite number")
        object.__setattr__(self, "created_at", float(self.created_at))

        # Scrub and reject credentials across all descriptive fields
        reject_secrets({
            "id": self.id,
            "work_id": self.work_id,
            "objective": self.objective,
            "assumptions": self.assumptions,
            "risks": self.risks,
            "expected_verification": self.expected_verification,
        })

    def to_dict(self) -> Dict[str, Any]:
        """Serialize Plan to a JSON-safe dictionary."""
        data: Dict[str, Any] = {
            "id": self.id,
            "work_id": self.work_id,
            "objective": self.objective,
            "steps": [step.to_dict() for step in self.steps],
            "assumptions": list(self.assumptions),
            "risks": list(self.risks),
            "expected_verification": list(self.expected_verification),
            "created_at": self.created_at,
        }
        reject_secrets(data)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Plan:
        """Deserialize Plan from a dictionary with strict validation."""
        if type(data) is not dict:
            raise ValueError("Expected dictionary for Plan.from_dict")
        reject_secrets(data)
        expected_keys = {
            "id",
            "work_id",
            "objective",
            "steps",
            "assumptions",
            "risks",
            "expected_verification",
            "created_at",
        }
        extra_keys = set(data.keys()) - expected_keys
        if extra_keys:
            raise ValueError(f"Unknown Plan fields: {sorted(extra_keys)}")

        for req in ("id", "work_id", "objective", "steps", "created_at"):
            if req not in data:
                raise ValueError(f"Missing required field in Plan: '{req}'")

        raw_steps = data["steps"]
        if not isinstance(raw_steps, (list, tuple)):
            raise ValueError("Plan 'steps' must be a list of step dictionaries")

        parsed_steps = [PlanStep.from_dict(s) for s in raw_steps]

        return cls(
            id=data["id"],
            work_id=data["work_id"],
            objective=data["objective"],
            steps=tuple(parsed_steps),
            assumptions=tuple(data.get("assumptions", ())),
            risks=tuple(data.get("risks", ())),
            expected_verification=tuple(data.get("expected_verification", ())),
            created_at=data["created_at"],
        )


def validate_plan(plan: Plan) -> None:
    """Explicitly validate a Plan instance.

    Raises ValueError if plan fails structural, dependency, or secret checks.
    """
    if not isinstance(plan, Plan):
        raise TypeError(f"Expected Plan instance, got {type(plan)}")
    _validate_step_dependencies(plan.steps)


@runtime_checkable
class Planner(Protocol):
    """Protocol for planners generating descriptive plans from Work.

    Planners receive only a Work object and return a Plan.
    Planners never receive orchestrators, approval stores, or execution tools.
    """

    def create_plan(self, work: Work) -> Plan:
        """Generate a descriptive Plan from a Work item."""
        ...


class DeterministicPlanner:
    """Deterministic baseline planner creating bounded, descriptive plans from Work.

    Generates a structured, acyclic Plan directly from Work intent and goal.
    Does NOT invoke LLMs, shell, network, tools, or execution engines.
    """

    def create_plan(self, work: Work) -> Plan:
        """Generate a deterministic, bounded baseline Plan from a Work item."""
        if not isinstance(work, Work):
            raise TypeError(f"work must be an instance of Work, got {type(work)}")

        if work.is_terminal:
            raise ValueError(
                f"Cannot create plan for terminal Work in status '{work.status.value}'"
            )

        # Deterministic plan ID derived from work ID and creation time
        content_hash = hashlib.sha256(
            f"{work.id}:{work.created_at}".encode("utf-8")
        ).hexdigest()[:16]
        plan_id = f"plan_{content_hash}"

        raw_intent = work.intent.strip()
        raw_goal = work.goal.strip()
        target_desc = raw_intent or raw_goal or "requested task"
        objective = f"Plan for: {target_desc}"

        # Deterministic sequence of bounded, descriptive steps
        steps = (
            PlanStep(
                id="step-1",
                description=f"Inspect repository context and prerequisites for '{target_desc}'",
                expected_outcome="Relevant files, configuration, and prerequisites are understood",
                dependencies=(),
                risk=RiskClass.LOW,
            ),
            PlanStep(
                id="step-2",
                description="Formulate proposed changes within designated scope",
                expected_outcome="Detailed changes drafted and verified against constraints",
                dependencies=("step-1",),
                risk=RiskClass.LOW,
            ),
            PlanStep(
                id="step-3",
                description="Apply changes strictly within authorized capability boundaries",
                expected_outcome="Target changes executed only after explicit authorization",
                dependencies=("step-2",),
                risk=RiskClass.MEDIUM,
            ),
            PlanStep(
                id="step-4",
                description="Verify outcomes against test suite and quality gates",
                expected_outcome="All verification checks and regressions pass cleanly",
                dependencies=("step-3",),
                risk=RiskClass.LOW,
            ),
        )

        assumptions = (
            "Target repository and environment are accessible",
            "Execution will proceed only with explicit capability authorization",
        )
        risks = (
            "Proposed changes must strictly stay within approved filesystem scope",
        )
        expected_verification = (
            "Automated test suite execution passes cleanly",
            "Target diff check passes cleanly",
        )

        return Plan(
            id=plan_id,
            work_id=work.id,
            objective=objective,
            steps=steps,
            assumptions=assumptions,
            risks=risks,
            expected_verification=expected_verification,
            created_at=work.updated_at,
        )


def apply_plan_to_work(work: Work, plan: Plan) -> Work:
    """Transition a Work item from PLANNING to APPROVAL_REQUIRED with plan step descriptions.

    Enforces the Work state machine:
    - Work must be in PLANNING status.
    - Plan must be bound to the exact work.id.
    - Uses canonical work.transition() API.
    """
    if not isinstance(work, Work):
        raise TypeError(f"work must be an instance of Work, got {type(work)}")
    if not isinstance(plan, Plan):
        raise TypeError(f"plan must be an instance of Plan, got {type(plan)}")

    if work.status != WorkStatus.PLANNING:
        raise ValueError(
            f"Work must be in PLANNING status to apply plan toward APPROVAL_REQUIRED, got '{work.status.value}'"
        )
    if plan.work_id != work.id:
        raise ValueError(
            f"Plan work_id '{plan.work_id}' does not match Work id '{work.id}'"
        )

    plan_descriptions = tuple(step.description for step in plan.steps)
    return work.transition(
        WorkStatus.APPROVAL_REQUIRED,
        plan=plan_descriptions,
    )


__all__ = [
    "DEFAULT_MAX_ASSUMPTIONS_COUNT",
    "DEFAULT_MAX_DEPENDENCIES_PER_STEP",
    "DEFAULT_MAX_ID_LEN",
    "DEFAULT_MAX_ITEM_TEXT_LEN",
    "DEFAULT_MAX_OBJECTIVE_LEN",
    "DEFAULT_MAX_PLAN_STEPS",
    "DEFAULT_MAX_RISKS_COUNT",
    "DEFAULT_MAX_STEP_DESC_LEN",
    "DEFAULT_MAX_STEP_OUTCOME_LEN",
    "DEFAULT_MAX_VERIFICATIONS_COUNT",
    "DeterministicPlanner",
    "Plan",
    "PlanStep",
    "Planner",
    "RiskClass",
    "apply_plan_to_work",
    "validate_plan",
]
