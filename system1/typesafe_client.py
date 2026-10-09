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
import time
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
    """Real System 1 client powered by TypeSafe Jev cloud decision API with bounded retries."""

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

        max_attempts = 3
        backoff_delays = [1.0, 2.0]
        last_exc: Optional[Exception] = None
        resp: Optional[requests.Response] = None

        for attempt in range(max_attempts):
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

                # Transient HTTP errors (429 rate limits, 5xx server errors) -> retry with backoff
                if resp.status_code in (429, 500, 502, 503, 504):
                    err_text = ""
                    try:
                        err_text = resp.text[:300]
                    except Exception:
                        pass
                    last_exc = RuntimeError(f"TypeSafe/Jev transient API error ({resp.status_code}): {err_text}")
                    if attempt < max_attempts - 1:
                        time.sleep(backoff_delays[attempt])
                        continue
                    raise RuntimeError(
                        f"TypeSafe/Jev transient API error ({resp.status_code}) after {max_attempts} attempts: {err_text}"
                    ) from last_exc

                resp.raise_for_status()
                # Request succeeded cleanly
                break

            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as net_err:
                last_exc = net_err
                if attempt < max_attempts - 1:
                    time.sleep(backoff_delays[attempt])
                    continue
                raise RuntimeError(
                    f"TypeSafe/Jev network failure after {max_attempts} attempts: {net_err}"
                ) from net_err

            except requests.exceptions.HTTPError as exc:
                # Deterministic HTTP client errors (e.g. 400, 401, 403, 422) fail closed without retrying
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

        try:
            data = resp.json()
        except Exception as json_err:
            raise RuntimeError(f"TypeSafe/Jev API returned malformed non-JSON payload: {json_err}") from json_err

        if not isinstance(data, dict):
            raise RuntimeError(f"TypeSafe/Jev API returned invalid schema: expected JSON object, got {type(data).__name__}")

        raw_answers = data.get("answers")
        if not isinstance(raw_answers, dict):
            raw_answers = {}

        answers: Dict[str, Answer] = {}
        for key in questions.keys():
            raw = raw_answers.get(key)
            if not isinstance(raw, dict):
                raw = {}

            # Safe numeric conversion for confidence
            raw_conf = raw.get("confidence")
            try:
                confidence = float(raw_conf) if raw_conf is not None else 0.0
            except (ValueError, TypeError):
                confidence = 0.0

            # Safe numeric conversion for noul
            raw_noul = raw.get("noul")
            noul_val: Optional[float] = None
            if raw_noul is not None:
                try:
                    noul_val = float(raw_noul)
                except (ValueError, TypeError):
                    noul_val = None

            answers[key] = Answer(
                choice=raw.get("choice"),
                score=raw.get("score"),
                noul=noul_val,
                confidence=confidence,
            )
        return answers


# Direct alias for readability
JevSystemOne = TypeSafeSystemOne
