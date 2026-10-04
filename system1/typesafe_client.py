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
        criteria = getattr(q, "criteria", None) or getattr(q, "scale", None)
        if isinstance(criteria, dict):
            criteria = list(criteria.values())
        return {"type": "score", "instructions": q.instructions, "criteria": criteria}
    if isinstance(q, NoulQuestion):
        payload: Dict[str, Any] = {"type": "noul", "instructions": q.instructions}
        criteria = getattr(q, "criteria", None)
        if criteria:
            payload["criteria"] = criteria
        return payload
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
        resp: Optional[requests.Response] = None
        try:
            resp = requests.post(
                self.api_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout,
                allow_redirects=False,
            )
            if resp.status_code in (301, 302, 303, 307, 308) or resp.is_redirect is True:
                location = resp.headers.get("Location", "")
                raise RuntimeError(
                    f"System 1 provider redirected to '{location}' "
                    f"({resp.status_code}): transport-level redirects are prohibited for model APIs."
                )
            resp.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            err_resp = exc.response if getattr(exc, "response", None) is not None else resp
            err_detail: Any = ""
            status_code: Any = "unknown"
            if err_resp is not None:
                status_code = err_resp.status_code
                try:
                    err_detail = err_resp.json()
                except Exception:
                    err_detail = err_resp.text
            else:
                err_detail = str(exc)
            raise RuntimeError(
                f"TypeSafe/Jev API error ({status_code}): {err_detail}"
            ) from exc

        if resp is None:
            raise RuntimeError("TypeSafe/Jev API returned no response")
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
