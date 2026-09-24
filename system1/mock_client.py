"""MockSystemOne — a free, local stand-in for Jev.

This does NOT try to imitate Jev's intelligence or calibration. It exists
so you can build and test the full orchestrator loop today, before your
TypeSafe waitlist access arrives. It uses plain heuristics over the
`state` dict the orchestrator sends (test pass/fail, retry count, diff
size) to produce plausible, *consistent* Choice/Score/Noul answers with
made-up-but-directionally-sane confidence.

Swap this for `TypeSafeSystemOne` in config.py once you have a key.
Nothing else in the codebase needs to change.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Tuple

from .base import Answer, ChoiceQuestion, NoulQuestion, Question, ScoreQuestion, SystemOneClient


def _stable_jitter(*parts: Any, low: float = -0.05, high: float = 0.05) -> float:
    """Deterministic pseudo-random jitter so repeated runs are reproducible."""
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    frac = int(h[:8], 16) / 0xFFFFFFFF
    return low + frac * (high - low)


_STOPWORDS = {
    "the", "a", "an", "is", "are", "to", "of", "in", "on", "for", "and", "or",
    "saya", "gua", "gw", "aku", "ini", "itu", "yang", "kok", "kenapa", "gabisa",
    "ga", "gak", "nggak", "bisa", "di", "ke", "dan", "atau", "dengan", "yah", "ya",
}


def _stem(word: str) -> str:
    # deliberately crude: just strip a trailing plural "s" so "invoice"/
    # "invoices", "login"/"logins", "token"/"tokens" match. Not a real
    # stemmer — doesn't need to be, it only has to beat exact-match.
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def _tokenize(text: str) -> set[str]:
    words = re.findall(r"[a-zA-Z0-9]+", text.lower())
    return {_stem(w) for w in words if w not in _STOPWORDS and len(w) > 2}


def _best_overlap(prompt: str, options: Dict[str, str]) -> Tuple[str, float]:
    """Bag-of-words overlap between the prompt and each option's description.

    Stand-in for real semantic matching (Jev would do this properly). Returns
    the best-matching option key and a 0-1 "how sure are we" score based on
    how much of the prompt's vocabulary landed in that option's description.
    Falls back to 'unrelated' (if offered) rather than an arbitrary first
    option when nothing overlaps at all.
    """
    fallback_key = "unrelated" if "unrelated" in options else next(iter(options), "")
    prompt_tokens = _tokenize(prompt)
    if not prompt_tokens or not options:
        return fallback_key, 0.15

    best_key, best_score = fallback_key, 0.0
    for key, desc in options.items():
        if key == "unrelated":
            continue
        desc_tokens = _tokenize(f"{key} {desc}")
        overlap = len(prompt_tokens & desc_tokens)
        score = overlap / max(1, len(prompt_tokens))
        if score > best_score:
            best_key, best_score = key, score
    return best_key, best_score


class MockSystemOne(SystemOneClient):
    name = "mock"

    def decide(
        self, state: Dict[str, Any], questions: Dict[str, Question]
    ) -> Dict[str, Answer]:
        answers: Dict[str, Answer] = {}
        for key, q in questions.items():
            if isinstance(q, ChoiceQuestion):
                answers[key] = self._answer_choice(key, state, q)
            elif isinstance(q, ScoreQuestion):
                answers[key] = self._answer_score(key, state, q)
            elif isinstance(q, NoulQuestion):
                answers[key] = self._answer_noul(key, state, q)
            else:  # pragma: no cover
                raise TypeError(f"Unknown question type for '{key}': {q!r}")
        return answers

    # -- heuristics -------------------------------------------------
    # These read well-known keys the orchestrator happens to send.
    # An unrecognized state shape falls back to a neutral, low-confidence
    # guess rather than crashing — same as you'd want from the real API
    # under an ambiguous input.

    def _answer_choice(self, key: str, state: Dict[str, Any], q: ChoiceQuestion) -> Answer:
        options = list(q.criteria.keys())

        if key == "next_action":
            tests_passed = state.get("test_passed")
            retry_count = int(state.get("retry_count", 0))
            max_retries = int(state.get("max_retries", 3))

            if tests_passed:
                choice = "open_pr" if "open_pr" in options else options[0]
                conf = 0.9 + _stable_jitter(state.get("task"), "pass")
            elif retry_count >= max_retries:
                choice = "escalate_human" if "escalate_human" in options else options[-1]
                conf = 0.85
            else:
                choice = "retry_fix" if "retry_fix" in options else options[0]
                # confidence in "keep retrying" drops each time it's tried
                conf = max(0.4, 0.8 - 0.15 * retry_count)
            return Answer(choice=choice, confidence=min(conf, 0.99))

        if key == "likely_domain":
            prompt = str(state.get("user_prompt", ""))
            best_key, overlap = _best_overlap(prompt, q.criteria)
            # scale overlap (0-1 vocab hit rate) into a confidence that behaves
            # like a real classifier's: never absurdly perfect, floor if nothing matched.
            conf = 0.2 + overlap * 1.4
            return Answer(choice=best_key, confidence=max(0.15, min(conf, 0.97)))

        if key == "change_type":
            prompt = str(state.get("user_prompt", "")).lower()
            bug_hint = any(w in prompt for w in ("kenapa", "gabisa", "gagal", "error", "bug", "kok", "why", "fail", "broken", "not working"))
            feature_hint = any(w in prompt for w in ("tambah", "add", "buat", "implement", "fitur", "feature", "new"))
            is_question = prompt.strip().endswith("?") or bug_hint
            if bug_hint and "?" in prompt:
                choice = "question_only" if "question_only" in options else "bug_investigation"
                conf = 0.75
            elif bug_hint:
                choice = "bug_investigation" if "bug_investigation" in options else options[0]
                conf = 0.8
            elif feature_hint:
                choice = "feature_request" if "feature_request" in options else options[0]
                conf = 0.75
            elif is_question:
                choice = "question_only" if "question_only" in options else options[0]
                conf = 0.65
            else:
                choice = "unclear" if "unclear" in options else options[-1]
                conf = 0.35
            return Answer(choice=choice, confidence=conf)

        # generic fallback: pick first option, low-moderate confidence
        conf = 0.5 + _stable_jitter(key, state.get("task"))
        return Answer(choice=options[0], confidence=max(0.3, min(conf, 0.7)))

    def _answer_score(self, key: str, state: Dict[str, Any], q: ScoreQuestion) -> Answer:
        scale = q.scale
        if key in ("diff_risk", "pr_risk"):
            lines = int(state.get("diff_lines_changed", 0))
            files = int(state.get("files_changed", 1))
            idx = 0
            if lines > 300 or files > 8:
                idx = len(scale) - 1
            elif lines > 100 or files > 3:
                idx = min(len(scale) - 1, len(scale) // 2)
            conf = 0.75 + _stable_jitter(key, lines, files, low=-0.1, high=0.1)
            return Answer(score=scale[idx], confidence=max(0.4, min(conf, 0.95)))

        mid = len(scale) // 2
        return Answer(score=scale[mid], confidence=0.5)

    def _answer_noul(self, key: str, state: Dict[str, Any], q: NoulQuestion) -> Answer:
        if key == "safe_to_proceed":
            tests_passed = bool(state.get("test_passed"))
            risk_hint = str(state.get("diff_risk", "")).lower()
            val = 0.9 if tests_passed else 0.15
            if "high" in risk_hint:
                val -= 0.25
            val += _stable_jitter(key, state.get("task"))
            val = max(0.01, min(0.99, val))
            return Answer(noul=val, confidence=0.7 + abs(_stable_jitter(key, "conf")))

        val = 0.5 + _stable_jitter(key, state.get("task"))
        return Answer(noul=max(0.01, min(0.99, val)), confidence=0.4)
