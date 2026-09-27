"""TypeSafeSystemOne: the real Jev backend.

Wraps TypeSafe's native API: POST https://api.typesafe.ai/v1/systemone
(Authorization: Bearer $TYPESAFE_API_KEY or $JEV_API_KEY). Not OpenAI-compatible: no
/chat/completions, no messages array, no temperature. Uses a clean direct HTTP call
against the documented schema so this works without any external SDK installation.

Docs: https://docs.typesafe.ai/introduction
Get a key: https://console.typesafe.ai/keys
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

import requests

from .base import Answer, ChoiceQuestion, NoulQuestion, Question, ScoreQuestion, SystemOneClient

DEFAULT_API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"


def _question_to_json(q: Question) -> Dict[str, Any]:
    if isinstance(q, ChoiceQuestion):
        return {"type": "choice", "instructions": q.instructions, "criteria": q.criteria}
    if isinstance(q, ScoreQuestion):
        return {"type": "score", "instructions": q.instructions, "scale": q.scale}
    if isinstance(q, NoulQuestion):
        return {"type": "noul", "instructions": q.instructions}
    raise TypeError(f"Unknown question type: {q!r}")  # pragma: no cover


class TypeSafeSystemOne(SystemOneClient):
    """Real System 1 client powered by TypeSafe Jev cloud decision API."""

    name = "jev"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 8.0,
        api_url: Optional[str] = None,
    ):
        self.api_key = (
            api_key
            or os.environ.get("TYPESAFE_API_KEY")
            or os.environ.get("JEV_API_KEY")
        )
        if not self.api_key:
            raise RuntimeError(
                "Jev API key not found. Please set TYPESAFE_API_KEY or JEV_API_KEY in your .env file."
            )
        self.model = (
            model
            or os.environ.get("TYPESAFE_MODEL")
            or os.environ.get("JEV_MODEL")
            or DEFAULT_MODEL
        )
        self.api_url = (
            api_url
            or os.environ.get("TYPESAFE_API_URL")
            or os.environ.get("JEV_API_URL")
            or DEFAULT_API_URL
        )
        self.timeout = timeout

    def decide(
        self, state: Dict[str, Any], questions: Dict[str, Question]
    ) -> Dict[str, Answer]:
        payload = {
            "state": state,
            "model": self.model,
            "questions": {k: _question_to_json(q) for k, q in questions.items()},
        }
        resp = requests.post(
            self.api_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()

        answers: Dict[str, Answer] = {}
        for key, raw in data.get("answers", {}).items():
            answers[key] = Answer(
                choice=raw.get("choice"),
                score=raw.get("score"),
                noul=raw.get("noul"),
                confidence=float(raw.get("confidence", 0.0)),
            )
        return answers


# Direct alias for readability
JevSystemOne = TypeSafeSystemOne
