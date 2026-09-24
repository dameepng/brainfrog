"""System One interface — the Jev-shaped contract.

Any backend (a local heuristic mock today, the real TypeSafe Jev API
tomorrow) implements `SystemOneClient.decide()`. The orchestrator only
ever talks to this interface, so swapping the backend later is a
one-line config change, not a rewrite.

Mirrors TypeSafe's real /v1/systemone schema:
  - state: dict of context (NOT free text you want summarized — short,
    structured facts the decision needs)
  - questions: named dict, each one of Choice / Score / Noul
  - answers: same names back, each with a typed value + confidence
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union


@dataclass
class ChoiceQuestion:
    """Pick exactly one option from a fixed set."""
    instructions: str
    criteria: Dict[str, str]  # option_key -> human description
    type: str = field(default="choice", init=False)


@dataclass
class ScoreQuestion:
    """Place the state on an ordered scale (e.g. risk low->high)."""
    instructions: str
    scale: List[str]  # ordered labels, low to high
    type: str = field(default="score", init=False)


@dataclass
class NoulQuestion:
    """Calibrated probability in [0, 1] that a statement is true."""
    instructions: str
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
