"""Antigravity System 2 Client — executes System 2 via Google Antigravity (agy CLI).

Uses the user's active Google Account login session in Antigravity (no API key required).
Supports Gemini models (gemini-3.8-flash-high, gemini-3.7-flash-high, gemini-3.1-pro-high, etc.)
as well as other models available via your Google Antigravity account.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from system2.claude_client import PlanStep, _extract_json, usage_tracker

DEFAULT_ANTIGRAVITY_MODEL = os.environ.get("ANTIGRAVITY_MODEL", "gemini-3.8-flash-high")


def find_antigravity_bin() -> Optional[str]:
    """Locate the agy CLI binary on the user's system."""
    # 1. Environment variable override
    env_bin = os.environ.get("ANTIGRAVITY_BIN")
    if env_bin and os.path.exists(env_bin):
        return env_bin

    # 2. Check PATH
    which_bin = shutil.which("agy") or shutil.which("agy.exe")
    if which_bin:
        return which_bin

    # 3. Standard Gemini Antigravity paths (~/.gemini/bin/agy.exe or %USERPROFILE%\.gemini\bin\agy.exe)
    for base in [os.path.expanduser("~"), os.environ.get("USERPROFILE", "")]:
        if base:
            candidate = os.path.join(base, ".gemini", "bin", "agy.exe")
            if os.path.exists(candidate):
                return candidate

    return None


class AntigravitySystem2Client:
    """System 2 client powered by Google Antigravity (Google Auth login session)."""

    provider_name: str = "antigravity"

    def __init__(
        self,
        model: str = DEFAULT_ANTIGRAVITY_MODEL,
        guidelines: str = "",
        bin_path: Optional[str] = None,
    ) -> None:
        self.model = model
        self.guidelines = guidelines
        self.bin_path = bin_path or find_antigravity_bin()
        if not self.bin_path or not os.path.exists(self.bin_path):
            raise FileNotFoundError(
                "Google Antigravity CLI binary ('agy.exe') not found. "
                "Ensure Antigravity is installed in ~/.gemini/bin or set ANTIGRAVITY_BIN."
            )

    def _apply_guidelines(self, system: str) -> str:
        if self.guidelines.strip():
            return f"{system}\n\n[Project Guidelines & Memory (from BRAINFROG.md)]\n{self.guidelines.strip()}"
        return system

    def _call(self, system: str, user: str, max_tokens: int = 4000) -> str:
        full_system = self._apply_guidelines(system)
        prompt = (
            f"[SYSTEM INSTRUCTIONS]\n{full_system}\n\n"
            f"[TASK]\n{user}"
        )

        cmd = [
            self.bin_path,
            "--model", self.model,
            "--output-format", "json",
        ]

        # Retry up to 2 times on transient "empty model output" errors
        # (Gemini occasionally returns an empty response on first attempt)
        _TRANSIENT = "model output must contain either output text or tool calls"
        max_attempts = 3
        last_error: Optional[Exception] = None

        for attempt in range(max_attempts):
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            stdout, stderr = proc.communicate(input=prompt)

            if proc.returncode != 0:
                err_msg = (stderr.strip() or stdout.strip() or
                           f"Process exited with code {proc.returncode}")
                if _TRANSIENT in err_msg and attempt < max_attempts - 1:
                    time.sleep(1.0)
                    continue
                raise RuntimeError(f"Antigravity (Google Auth) error: {err_msg}")

            try:
                data = json.loads(stdout)
                # agy sometimes returns the error inside the JSON body
                response_text = data.get("response", "")
                if not response_text and _TRANSIENT in str(data):
                    if attempt < max_attempts - 1:
                        time.sleep(1.0)
                        continue
                    raise RuntimeError(
                        f"Antigravity model returned empty output after {max_attempts} attempts. "
                        "Try rephrasing your request or switching models with /models."
                    )
                usage = data.get("usage") or {}
                usage_tracker.record(
                    usage.get("input_tokens", 0),
                    usage.get("output_tokens", 0),
                )
                return response_text
            except json.JSONDecodeError:
                return stdout.strip()

        raise RuntimeError(
            f"Antigravity model returned empty output after {max_attempts} attempts. "
            "Try rephrasing your request or switching models with /models."
        )

    # -- 1. planning --------------------------------------------------
    def plan_task(
        self, task: str, repo_tree: str, pinned_files: Optional[Dict[str, str]] = None
    ) -> List[PlanStep]:
        system = (
            "You are a senior software engineer planning a small, safe change. "
            "Break the task into 1-4 concrete code modification steps. Each step must touch or create specific files. "
            "Do NOT include manual testing, browser verification, or review steps. "
            "Paths must be relative to workspace root (do not prefix with workspace folder name). "
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
            "Paths must be relative to workspace root. "
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
        raw = self._call(system, user, max_tokens=64000)
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
        raw = self._call(system, user, max_tokens=64000)
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
