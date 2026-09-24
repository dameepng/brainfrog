"""TypeSafeSystemOne — the real Jev backend.

Wraps TypeSafe's native API: POST https://api.typesafe.ai/v1/systemone
(Authorization: Bearer $TYPESAFE_API_KEY). Not OpenAI-compatible — no
/chat/completions, no messages array, no temperature. Prefers the
official `typesafe-sdk` package if installed; falls back to a plain
`requests` call against the documented schema so this works even
before you `pip install typesafe-sdk`.

Docs: https://docs.typesafe.ai/introduction
Get a key: https://console.typesafe.ai/keys (early access waitlist)
"""
from __future__ import annotations

import os
from typing import Any, Dict

import requests

from .base import Answer, ChoiceQuestion, NoulQuestion, Question, ScoreQuestion, SystemOneClient

API_URL = "https://api.typesafe.ai/v1/systemone"
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
    name = "typesafe"

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL, timeout: float = 5.0):
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "TYPESAFE_API_KEY not set. Get one at https://console.typesafe.ai/keys "
                "(early access waitlist), or use --backend mock in the meantime."
            )
        self.model = model
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
            API_URL,
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
