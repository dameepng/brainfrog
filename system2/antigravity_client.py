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
from typing import Any, Dict, List, Optional, Tuple

from system2.claude_client import PlanStep, usage_tracker
from system2.json_utils import extract_json, _extract_json, repair_json_content

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


def sanitize_surrogates(text: str) -> str:
    """Sanitize unpaired or malformed surrogate characters that break UTF-8 encoders on Windows."""
    if not text:
        return ""
    try:
        text = text.encode("utf-16", "surrogatepass").decode("utf-16", errors="replace")
    except Exception:
        pass
    return text.encode("utf-8", errors="replace").decode("utf-8")


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
        tool_guard = (
            "IMPORTANT: Do NOT execute any external tools, scripts, or terminal commands. "
            "You are operating in structured output mode. Respond ONLY with the requested JSON format."
        )
        full_system = f"{full_system}\n\n{tool_guard}"
        prompt = sanitize_surrogates(
            f"[SYSTEM INSTRUCTIONS]\n{full_system}\n\n"
            f"[TASK]\n{user}"
        )

        cmd = [
            self.bin_path,
            "--model", self.model,
            "--output-format", "json",
            "--dangerously-skip-permissions",
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
                errors="replace",
            )
            try:
                stdout, stderr = proc.communicate(input=prompt, timeout=180.0)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.communicate(timeout=3.0)
                except Exception:
                    pass
                if attempt < max_attempts - 1:
                    time.sleep(1.0)
                    continue
                raise RuntimeError("Antigravity process timed out after 180s.")

            if proc.returncode != 0:
                err_msg = (stderr.strip() or stdout.strip() or
                           f"Process exited with code {proc.returncode}")
                if _TRANSIENT in err_msg and attempt < max_attempts - 1:
                    time.sleep(1.0)
                    continue
                raise RuntimeError(f"Antigravity (Google Auth) error: {err_msg}")

            try:
                data = json.loads(stdout)
                response_text = data.get("response", "")
                if not response_text.strip():
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
        self,
        task: str,
        repo_tree: str,
        pinned_files: Optional[Dict[str, str]] = None,
        plan_context: Optional[str] = None,
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
        if plan_context:
            user_parts.append(f"\n[Active Plan Context]\n{plan_context}")
        if pinned_files:
            user_parts.append(
                f"\nUser explicitly pinned files:\n{json.dumps(pinned_files, indent=2)}"
            )
        raw = self._call(system, "\n".join(user_parts), max_tokens=1500)
        data = _extract_json(raw)
        return [PlanStep(**s) for s in data["steps"]]

    # -- 1b. Plan mode PRD & implementation planning (read-only) -------
    def plan_and_prd(
        self,
        task: str,
        repo_tree: str,
        focus_files: Dict[str, str],
        pinned_files: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Generate a practical PRD and step-by-step implementation plan in Plan mode."""
        system = (
            "You are a principal software engineer operating in PLAN mode. "
            "Your role is architectural exploration, PRD creation, and implementation planning. "
            "You must NOT generate diffs or edit code in this mode. "
            "Examine the provided repository files and structure carefully.\n\n"
            "Quality Guidelines:\n"
            "- For small, obvious tasks: keep it concise with 1-3 direct steps.\n"
            "- For large or ambiguous tasks: construct a practical, rigorous PRD.\n"
            "- Separate inspected codebase FACTS (citing specific files/functions) from your technical ASSUMPTIONS and PROPOSALS.\n"
            "- Define concrete, testable acceptance criteria.\n"
            "- Identify risks or trade-offs.\n"
            "- Ask clarifying questions ONLY if there are material, critical decisions not answered in the repo or prompt.\n\n"
            "Respond with ONLY a JSON object formatted as:\n"
            "{\n"
            '  "title": "Short descriptive title of the change",\n'
            '  "is_small_task": true,\n'
            '  "problem": "Problem statement and context",\n'
            '  "goals": ["Goal 1", "Goal 2"],\n'
            '  "scope": ["Included item 1"],\n'
            '  "non_scope": ["Excluded item 1"],\n'
            '  "codebase_findings": ["Fact 1 (citing file.py:function)", "Fact 2"],\n'
            '  "assumptions": ["Assumption/Proposal 1"],\n'
            '  "clarifying_questions": [],\n'
            '  "acceptance_criteria": ["Testable criterion 1", "Testable criterion 2"],\n'
            '  "steps": [{"id": "1", "description": "Step 1", "files": ["path/a.py"]}],\n'
            '  "relevant_files": ["path/a.py"],\n'
            '  "markdown_doc": "# Full formatted PRD and Plan in Markdown\\n\\n..."\n'
            "}\n"
            "No prose outside the JSON."
        )
        user_parts = [f"Task:\n{task}"]
        if repo_tree:
            user_parts.append(f"Repository file tree:\n{repo_tree}")
        if focus_files:
            user_parts.append(f"Relevant inspected files:\n{json.dumps(focus_files, indent=2)}")
        if pinned_files:
            user_parts.append(f"User pinned files:\n{json.dumps(pinned_files, indent=2)}")

        raw = self._call(system, "\n\n".join(user_parts), max_tokens=4000)
        data = _extract_json(raw)
        return data

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
            "Only include files you actually changed. No prose outside the JSON. "
            "IMPORTANT: Output strict, valid JSON. Ensure all quotes and backslashes inside file strings are properly escaped. "
            "In React/JSX, use single quotes {' '} or &nbsp; for whitespace, never unescaped double quotes."
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
            "Return full file content for every file you change. No prose outside the JSON. "
            "IMPORTANT: Output strict, valid JSON. Ensure all quotes and backslashes inside file strings are properly escaped. "
            "In React/JSX, use single quotes {' '} or &nbsp; for whitespace, never unescaped double quotes."
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
    def diagnose(self, user_prompt: str, focus_files: Dict[str, str], domain: str, repo_tree: str = "") -> str:
        """For change_type == 'question_only': explain, don't edit."""
        system = (
            "You are a senior engineer helping a teammate understand their codebase. "
            "Read the provided files and repo structure, and give a direct, friendly, and concrete answer. "
            "Point at specific files, functions, or lines where applicable. "
            "Do not execute any tools, functions, or system commands; provide your answer directly as text. "
            "Respond in the same language as the user's question (e.g. Indonesian if the question is in Indonesian, "
            "English if in English). Use clean markdown formatting."
        )
        parts = [f"Question:\n{user_prompt}"]
        if repo_tree:
            parts.append(f"Repository file tree:\n{repo_tree}")
        if focus_files:
            parts.append(f"Relevant files:\n{json.dumps(focus_files, indent=2)}")
        user = "\n\n".join(parts)
        return self._call(system, user, max_tokens=3000)

    # -- 4. PR copy -------------------------------------------------
    def draft_pr(self, task: str, changed_files: List[str], test_summary: str) -> Dict[str, str]:
        system = (
            "Write a concise pull request and Git commit title and description following Conventional Commits format (e.g. feat: ..., fix: ..., refactor: ..., test: ...). "
            'Respond with ONLY JSON: {"title": "...", "body": "..."}. '
            "Title must be a clean, single-line conventional commit message. "
            "Body should be short: what changed, why, and how it was tested. No prose outside the JSON."
        )
        user = f"Task:\n{task}\n\nFiles changed:\n{changed_files}\n\nTest result:\n{test_summary}"
        raw = self._call(system, user, max_tokens=800)
        return _extract_json(raw)

    # -- 5. visual critique & fix via multimodal eyes ------------------
    def visual_review_and_fix(
        self,
        task: str,
        screenshot_path: Path,
        file_contents: Dict[str, str],
    ) -> Tuple[Dict[str, str], bool, str]:
        """Inspect actual rendered screenshot, critique visual layout, and fix styling defects.

        Returns (fixed_files, visual_pass, critique_summary).
        """
        system = (
            "You are a World-Class Principal UI/UX Designer and Frontend Architect. "
            "You are evaluating the ACTUAL RENDERED SCREENSHOT of the user interface. "
            "Your job is to eliminate AI slop, awkward nesting, overlapping text, "
            "and unbalanced composition.\n\n"
            "Examine the rendered screenshot carefully against modern design standards (Linear / Raycast / Apple):\n"
            "1. Card-ception & Nesting: Is there an unnecessary card inside a card? Outer container must be clean/transparent, not a duplicate bordered box.\n"
            "2. Visual Balance & Alignment: Are logo, headings, taglines, and buttons cleanly aligned and proportional?\n"
            "3. Spacing & Whitespace: Are elements cramped or awkward? Ensure generous, comfortable breathing room.\n"
            "4. Contrast & Color: Are text elements legible? Does glassmorphism have visible ambient background lighting, or is it a flat muddy box?\n"
            "5. Micro-details: Are links/badges properly styled (e.g. no unstyled blue text)? Are form controls sleek?\n\n"
            "If the visual presentation is already clean, polished, and top-tier, return visual_pass=true and files={}.\n"
            "If any visual defect, awkward nesting, or slop pattern exists, fix the HTML and/or CSS files to make it gorgeous.\n"
            "Respond with ONLY JSON:\n"
            '{"visual_pass": false, "critique": "brief diagnosis of what looks off", "files": {"path": "<full new content>"}}\n'
            "Return full content for any file you change. No prose outside the JSON."
        )

        user = (
            f"Overall Task: {task}\n\n"
            f"Rendered Interface Screenshot is located at:\n{str(screenshot_path.resolve())}\n\n"
            f"Please inspect the visual screenshot at {str(screenshot_path.resolve())}.\n\n"
            f"Current file contents:\n{json.dumps(file_contents, indent=2)}"
        )

        raw = self._call(system, user, max_tokens=64000)
        data = _extract_json(raw)
        visual_pass = bool(data.get("visual_pass", False))
        critique = str(data.get("critique", ""))
        files = data.get("files", {}) or {}
        return files, visual_pass, critique
