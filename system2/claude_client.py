"""System2Client — the deliberate, generative layer, backed by Claude.

Features:
- Full planning, code generation, review/debugging, diagnosis, and PR drafting
- Integration with BRAINFROG.md project memory/guidelines
- Token usage and cost tracking (Anthropic API metrics)
- Support for @file context pinning
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import anthropic

DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

# Maximum output tokens for code-generation calls (write_code, review_and_fix).
# Set BRAINFROG_MAX_OUTPUT_TOKENS in .env to override.
# Default: 64000 (current Anthropic model ceiling) — effectively no artificial cap.
MAX_OUTPUT_TOKENS = int(os.environ.get("BRAINFROG_MAX_OUTPUT_TOKENS", "64000"))


@dataclass
class UsageStats:
    input_tokens: int = 0
    output_tokens: int = 0
    requests_count: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cost_usd(self) -> float:
        # Anthropic Claude 3.5/Sonnet 5 pricing: $3.00/M in, $15.00/M out
        return (self.input_tokens * 3.0 / 1_000_000) + (self.output_tokens * 15.0 / 1_000_000)


class UsageTracker:
    def __init__(self) -> None:
        self.session = UsageStats()
        self.last_task = UsageStats()

    def record(self, inp: int, out: int) -> None:
        self.session.input_tokens += inp
        self.session.output_tokens += out
        self.session.requests_count += 1
        self.last_task.input_tokens += inp
        self.last_task.output_tokens += out
        self.last_task.requests_count += 1

    def reset_task(self) -> UsageStats:
        prev = self.last_task
        self.last_task = UsageStats()
        return prev


# Global usage tracking singleton
usage_tracker = UsageTracker()


def _extract_json(text: str) -> Dict[str, Any]:
    """Pull the first valid {...} JSON object out of a model response.

    Uses json.JSONDecoder.raw_decode so it correctly handles nested braces
    inside string values (e.g. CSS rules, JS objects inside HTML content).
    Falls back to stripping markdown fences first if the response is wrapped.
    """
    text = text.strip()

    # Strip markdown code fences (```json ... ``` or ``` ... ```)
    if text.startswith("```"):
        lines = text.splitlines()
        # Drop first line (```json or ```) and last line (```)
        inner = lines[1:] if lines[-1].strip() == "```" else lines[1:]
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        text = "\n".join(inner).strip()

    start = text.find("{")
    if start == -1:
        raise ValueError(f"No JSON object found in model output:\n{text[:500]}")

    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(text, start)
        return obj
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"JSON parse error at position {exc.pos} in model output.\n"
            f"Context: ...{text[max(0, exc.pos-80):exc.pos+80]}...\n"
            f"Full output (first 800 chars):\n{text[:800]}"
        ) from exc



@dataclass
class PlanStep:
    id: str
    description: str
    files: List[str]


class System2Client:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        api_key: Optional[str] = None,
        guidelines: str = "",
    ) -> None:
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.guidelines = guidelines

    def _apply_guidelines(self, system: str) -> str:
        if self.guidelines.strip():
            return f"{system}\n\n[Project Guidelines & Memory (from BRAINFROG.md)]\n{self.guidelines.strip()}"
        return system

    def _call(self, system: str, user: str, max_tokens: int = 4000) -> str:
        full_system = self._apply_guidelines(system)
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=full_system,
            messages=[{"role": "user", "content": user}],
        )
        if hasattr(resp, "usage") and resp.usage:
            usage_tracker.record(
                getattr(resp.usage, "input_tokens", 0),
                getattr(resp.usage, "output_tokens", 0),
            )
        return "".join(b.text for b in resp.content if b.type == "text")

    # -- 1. planning --------------------------------------------------
    def plan_task(
        self, task: str, repo_tree: str, pinned_files: Optional[Dict[str, str]] = None
    ) -> List[PlanStep]:
        system = (
            "You are a senior software engineer planning a small, safe change. "
            "Break the task into 1-4 concrete code modification steps. Each step must touch or create specific files. "
            "Do NOT include manual testing, browser verification, or review steps. "
            "Respond with ONLY JSON: "
            '{"steps": [{"id": "1", "description": "...", "files": ["path/a.py"]}]}. '
            "Keep steps small and independently testable. No prose outside the JSON."
        )
        user_parts = [f"Task:\n{task}\n\nRepository file tree:\n{repo_tree}"]
        if pinned_files:
            user_parts.append(
                f"\nUser explicitly pinned files:\n{json.dumps(pinned_files, indent=2)}"
            )
        raw = self._call(system, "\n".join(user_parts), max_tokens=1500)
        data = _extract_json(raw)
        return [PlanStep(**s) for s in data["steps"]]

    # -- 2. writing code ------------------------------------------------
    def write_code(
        self,
        step: PlanStep,
        task: str,
        file_contents: Dict[str, str],
        pinned_files: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
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
        all_context = dict(file_contents)
        if pinned_files:
            all_context.update(pinned_files)

        user = (
            f"Overall task:\n{task}\n\n"
            f"Current step:\n{step.description}\n\n"
            f"Current file contents:\n{json.dumps(all_context, indent=2)}"
        )
        raw = self._call(system, user, max_tokens=MAX_OUTPUT_TOKENS)
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
        raw = self._call(system, user, max_tokens=MAX_OUTPUT_TOKENS)
        data = _extract_json(raw)
        return data["files"]

    # -- 3b. diagnose only, no code changes ------------------------------
    def diagnose(self, user_prompt: str, focus_files: Dict[str, str], domain: str) -> str:
        """For change_type == 'question_only': explain, don't edit."""
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
        return self._call(system, user, max_tokens=2000)

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
