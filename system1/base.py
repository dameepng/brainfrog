"""System One interface: the Jev-shaped contract.

Every System 1 backend implements `SystemOneClient.decide()`.
The orchestrator only ever talks to this interface.

Mirrors TypeSafe's /v1/systemone schema:
  - state: dict of structured context the decision needs
  - questions: named dict, each one of Choice / Score / Noul
  - answers: same names back, each with a typed value + confidence

Instructions and criteria accept both plain strings and structured
objects (dicts / lists of dicts) per the official TypeSafe API spec.
See: https://docs.typesafe.ai/primitives/advanced
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Union

# TypeSafe API accepts plain strings or structured objects for instructions
# and criteria. These type aliases capture that flexibility.
Instruction = Union[str, Dict[str, Any]]
Criteria = Union[Dict[str, Any], List[Any], str]


@dataclass
class ChoiceQuestion:
    """Pick exactly one option from a fixed set."""
    instructions: Instruction
    criteria: Dict[str, Any]  # option_key -> description (str or structured dict)
    type: str = field(default="choice", init=False)


@dataclass
class ScoreQuestion:
    """Place the state on an ordered scale (e.g. risk low->high)."""
    instructions: Instruction
    scale: Union[List[Any], Dict[str, Any]]  # ordered labels or level descriptions
    type: str = field(default="score", init=False)


@dataclass
class NoulQuestion:
    """Calibrated probability in [0, 1] that a statement is true."""
    instructions: Instruction
    type: str = field(default="noul", init=False)


Question = Union[ChoiceQuestion, ScoreQuestion, NoulQuestion]


@dataclass
class Answer:
    choice: Optional[str] = None
    score: Optional[str] = None
    noul: Optional[float] = None
    confidence: float = 0.0

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        val = self.choice or self.score
        if val is None and self.noul is not None:
            val = f"{self.noul:.2f}"
        return f"Answer({val}, confidence={self.confidence:.2f})"


class SystemOneClient:
    """Abstract base class for a System 1 (Jev-shaped) decision backend."""

    name: str = "abstract"

    def decide(
        self, state: Dict[str, Any], questions: Dict[str, Question]
    ) -> Dict[str, Answer]:
        raise NotImplementedError
