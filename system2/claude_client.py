"""System2Client — the deliberate, generative layer, backed by Claude.

Everything here is the "slow, string-generating" work Jev deliberately
gives up: planning, writing code, reading a test failure and figuring
out why, and drafting human-readable PR copy. Called only when the
System 1 gate says it's actually needed.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import anthropic

DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")


def _extract_json(text: str) -> Dict[str, Any]:
    """Pull the first {...} block out of a model response and parse it."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object found in model output:\n{text[:500]}")
    return json.loads(text[start : end + 1])


@dataclass
class PlanStep:
    id: str
    description: str
    files: List[str]


class System2Client:
    def __init__(self, model: str = DEFAULT_MODEL, api_key: Optional[str] = None):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def _call(self, system: str, user: str, max_tokens: int = 4000) -> str:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in resp.content if b.type == "text")

    # -- 1. planning --------------------------------------------------
    def plan_task(self, task: str, repo_tree: str) -> List[PlanStep]:
        system = (
            "You are a senior software engineer planning a small, safe change. "
            "Break the task into 1-4 concrete steps. Respond with ONLY JSON: "
            '{"steps": [{"id": "1", "description": "...", "files": ["path/a.py"]}]}. '
            "Keep steps small and independently testable. No prose outside the JSON."
        )
        user = f"Task:\n{task}\n\nRepository file tree:\n{repo_tree}"
        raw = self._call(system, user, max_tokens=1500)
        data = _extract_json(raw)
        return [PlanStep(**s) for s in data["steps"]]

    # -- 2. writing code ------------------------------------------------
    def write_code(self, step: PlanStep, task: str, file_contents: Dict[str, str]) -> Dict[str, str]:
        """Returns {path: new_full_file_content} for every file touched."""
        system = (
            "You are a senior software engineer implementing one planned step. "
            "You will be given the current content of relevant files (empty string "
            "means the file does not exist yet and should be created). "
            "Respond with ONLY JSON: "
            '{"files": {"path/to/file.py": "<full new file content>"}, "summary": "one line"}. '
            "Return the COMPLETE new content for each file you change, not a diff. "
            "Only include files you actually changed. No prose outside the JSON."
        )
        user = (
            f"Overall task:\n{task}\n\n"
            f"Current step:\n{step.description}\n\n"
            f"Current file contents:\n{json.dumps(file_contents, indent=2)}"
        )
        raw = self._call(system, user, max_tokens=6000)
        data = _extract_json(raw)
        return data["files"]

    # -- 3. review & fix on test failure --------------------------------
    def review_and_fix(
        self, task: str, step: PlanStep, file_contents: Dict[str, str], test_output: str
    ) -> Dict[str, str]:
        system = (
            "You are a senior software engineer debugging a failing test suite. "
            "Read the failure output and the current file contents, diagnose the "
            "root cause, and fix it. Respond with ONLY JSON: "
            '{"files": {"path": "<full new file content>"}, "diagnosis": "one line"}. '
            "Return full file content for every file you change. No prose outside the JSON."
        )
        user = (
            f"Task:\n{task}\n\nStep:\n{step.description}\n\n"
            f"Current file contents:\n{json.dumps(file_contents, indent=2)}\n\n"
            f"Test output (most recent run):\n{test_output[-4000:]}"
        )
        raw = self._call(system, user, max_tokens=6000)
        data = _extract_json(raw)
        return data["files"]

    # -- 3b. diagnose only, no code changes ------------------------------
    def diagnose(self, user_prompt: str, focus_files: Dict[str, str], domain: str) -> str:
        """For change_type == 'question_only': explain, don't edit.

        Used when the scope gate decided the user asked a question
        ("why can't I log in?") rather than requested a change. Returns
        prose, not JSON — there's nothing to apply to disk here.
        """
        system = (
            "You are a senior engineer helping a teammate understand their own "
            f"codebase. They asked a question likely related to the '{domain}' area. "
            "Read the provided files and give a direct, concrete answer: what's "
            "likely causing the behavior they're describing, pointing at specific "
            "functions/lines where you can. If the files don't contain enough "
            "information to be sure, say what you'd need to check next instead of "
            "guessing. Do not propose a code change unless asked."
        )
        user = f"Question:\n{user_prompt}\n\nRelevant files:\n{json.dumps(focus_files, indent=2)}"
        return self._call(system, user, max_tokens=1500)

    # -- 4. PR copy -------------------------------------------------
    def draft_pr(self, task: str, changed_files: List[str], test_summary: str) -> Dict[str, str]:
        system = (
            "Write a concise pull request title and description for a code change. "
            'Respond with ONLY JSON: {"title": "...", "body": "..."}. '
            "Body should be short: what changed, why, and how it was tested. No prose outside the JSON."
        )
        user = f"Task:\n{task}\n\nFiles changed:\n{changed_files}\n\nTest result:\n{test_summary}"
        raw = self._call(system, user, max_tokens=800)
        return _extract_json(raw)
