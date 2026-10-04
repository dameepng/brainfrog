"""OpenAI & OpenAI-Compatible System 2 Client.

Supports:
- OpenAI API (https://api.openai.com/v1)
- OpenRouter (https://openrouter.ai/api/v1)
- LocalAI, vLLM, Ollama, DeepSeek, and generic OpenAI-compatible endpoints
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from system2.claude_client import PlanStep, usage_tracker
from system2.json_utils import _extract_json

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENAI_MODEL = "gpt-4o"
DEFAULT_OPENROUTER_MODEL = "openai/gpt-4o"


class OpenAISystem2Client:
    """System 2 client implementing the standard interface for OpenAI-compatible endpoints."""

    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        guidelines: str = "",
        provider_name: str = "openai",
    ) -> None:
        self.provider_name = provider_name.lower().strip()
        self.guidelines = guidelines

        # 1. Resolve API key
        resolved_key = (
            api_key
            or os.environ.get("BRAINFROG_MODEL_API_KEY")
            or os.environ.get("CUSTOM_API_KEY")
        )
        if not resolved_key:
            if "openrouter" in self.provider_name:
                resolved_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY")
            else:
                resolved_key = os.environ.get("OPENAI_API_KEY")

        if not resolved_key:
            raise RuntimeError(
                f"API key missing for provider '{self.provider_name}'. "
                "Please configure OPENAI_API_KEY, OPENROUTER_API_KEY, or BRAINFROG_MODEL_API_KEY in your .env file."
            )
        self.api_key = resolved_key

        # 2. Resolve Base URL
        resolved_url = (
            base_url
            or os.environ.get("BRAINFROG_MODEL_BASE_URL")
            or os.environ.get("CUSTOM_BASE_URL")
        )
        if not resolved_url:
            if "openrouter" in self.provider_name:
                resolved_url = os.environ.get("OPENROUTER_BASE_URL") or DEFAULT_OPENROUTER_BASE_URL
            else:
                resolved_url = os.environ.get("OPENAI_BASE_URL") or DEFAULT_OPENAI_BASE_URL

        self.base_url = resolved_url.rstrip("/")

        # 3. Resolve Model
        resolved_model = (
            model
            or os.environ.get("BRAINFROG_MODEL_NAME")
            or os.environ.get("CUSTOM_MODEL")
        )
        if not resolved_model:
            if "openrouter" in self.provider_name:
                resolved_model = os.environ.get("OPENROUTER_MODEL") or DEFAULT_OPENROUTER_MODEL
            else:
                resolved_model = os.environ.get("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL

        self.model = resolved_model

    def _apply_guidelines(self, system: str) -> str:
        if self.guidelines.strip():
            return f"{system}\n\n[Project Guidelines & Memory (from BRAINFROG.md)]\n{self.guidelines.strip()}"
        return system

    def _call(
        self,
        system: str,
        user: str,
        max_tokens: int = 4000,
        images: Optional[List[Any]] = None,
    ) -> str:
        full_system = self._apply_guidelines(system)

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": full_system},
        ]

        if images:
            content_blocks: List[Dict[str, Any]] = [{"type": "text", "text": user}]
            for img in images:
                b64 = None
                if hasattr(img, "to_base64"):
                    b64 = img.to_base64()
                elif hasattr(img, "base64_data"):
                    b64 = img.base64_data
                elif isinstance(img, dict) and "data" in img:
                    b64 = img["data"]
                if b64:
                    content_blocks.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    })
            messages.append({"role": "user", "content": content_blocks})
        else:
            messages.append({"role": "user", "content": user})

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if "openrouter" in self.base_url.lower():
            headers["HTTP-Referer"] = "https://github.com/dameepng/brainfrog"
            headers["X-Title"] = "BrainFrog Agent"

        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
        }

        endpoint = f"{self.base_url}/chat/completions"
        try:
            resp = requests.post(
                endpoint, headers=headers, json=payload, timeout=120, allow_redirects=False,
            )
            if resp.status_code in (301, 302, 303, 307, 308) or resp.is_redirect is True:
                location = resp.headers.get("Location", "")
                raise RuntimeError(
                    f"OpenAI-compatible provider redirected to '{location}' "
                    f"({resp.status_code}): transport-level redirects are prohibited for model APIs."
                )
            resp.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            try:
                err_detail = resp.json()
            except Exception:
                err_detail = resp.text
            raise RuntimeError(
                f"OpenAI-compatible provider error ({resp.status_code}): {err_detail}"
            ) from exc

        data = resp.json()
        usage = data.get("usage", {})
        if usage:
            usage_tracker.record(
                usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0),
            )

        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError(f"OpenAI-compatible endpoint returned empty choices: {data}")

        return choices[0].get("message", {}).get("content", "")

    # -- 1. planning --------------------------------------------------
    def plan_task(
        self,
        task: str,
        repo_tree: str,
        pinned_files: Optional[Dict[str, str]] = None,
        plan_context: Optional[str] = None,
        images: Optional[List[Any]] = None,
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
        if plan_context:
            user_parts.append(f"\n[Active Plan Context]\n{plan_context}")
        if pinned_files:
            user_parts.append(
                f"\nUser explicitly pinned files:\n{json.dumps(pinned_files, indent=2)}"
            )
        raw = self._call(system, "\n".join(user_parts), max_tokens=1500, images=images)
        data = _extract_json(raw)
        return [PlanStep(**s) for s in data["steps"]]

    # -- 1b. Plan mode PRD & implementation planning (read-only) -------
    def plan_and_prd(
        self,
        task: str,
        repo_tree: str,
        focus_files: Dict[str, str],
        pinned_files: Optional[Dict[str, str]] = None,
        images: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
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

        raw = self._call(system, "\n\n".join(user_parts), max_tokens=4000, images=images)
        return _extract_json(raw)

    # -- 2. writing code ------------------------------------------------
    def write_code(
        self,
        step: PlanStep,
        task: str,
        file_contents: Dict[str, str],
        pinned_files: Optional[Dict[str, str]] = None,
        images: Optional[List[Any]] = None,
    ) -> Dict[str, str]:
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
        raw = self._call(system, user, max_tokens=16000, images=images)
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
        raw = self._call(system, user, max_tokens=16000)
        data = _extract_json(raw)
        return data["files"]

    # -- 3b. diagnose only, no code changes ------------------------------
    def diagnose(
        self,
        user_prompt: str,
        focus_files: Dict[str, str],
        domain: str,
        repo_tree: str = "",
        images: Optional[List[Any]] = None,
    ) -> str:
        system = (
            "You are a senior engineer helping a teammate understand their codebase. "
            "Read the provided files and repo structure, and give a direct, friendly, and concrete answer. "
            "Point at specific files, functions, or lines where applicable. "
            "Respond in the same language as the user's question (e.g. Indonesian if the question is in Indonesian, "
            "English if in English). Use clean markdown formatting."
        )
        parts = [f"Question:\n{user_prompt}"]
        if repo_tree:
            parts.append(f"Repository file tree:\n{repo_tree}")
        if focus_files:
            parts.append(f"Relevant files:\n{json.dumps(focus_files, indent=2)}")
        user = "\n\n".join(parts)
        return self._call(system, user, max_tokens=3000, images=images)

    # -- 4. PR copy -------------------------------------------------
    def draft_pr(self, task: str, changed_files: List[str], test_summary: str) -> Dict[str, str]:
        system = (
            "Write a concise pull request and Git commit title and description following Conventional Commits format. "
            'Respond with ONLY JSON: {"title": "...", "body": "..."}. '
            "Title must be a clean, single-line conventional commit message. "
            "Body should be short: what changed, why, and how it was tested. No prose outside the JSON."
        )
        user = f"Task:\n{task}\n\nFiles changed:\n{changed_files}\n\nTest result:\n{test_summary}"
        raw = self._call(system, user, max_tokens=800)
        return _extract_json(raw)

    # -- 5. visual critique & fix -------------------------------------
    def visual_review_and_fix(
        self,
        task: str,
        screenshot_path: Path,
        file_contents: Dict[str, str],
    ) -> Tuple[Dict[str, str], bool, str]:
        system = (
            "You are a World-Class Principal UI/UX Designer and Frontend Architect. "
            "Examine the rendered screenshot carefully against modern UI/UX design standards.\n"
            "If the visual presentation is already clean, polished, and top-tier, return visual_pass=true and files={}.\n"
            "If any visual defect exists, fix the HTML and/or CSS files to make it gorgeous.\n"
            "Respond with ONLY JSON:\n"
            '{"visual_pass": false, "critique": "brief diagnosis", "files": {"path": "<full new content>"}}'
        )
        user = f"Overall Task: {task}\n\nCurrent file contents:\n{json.dumps(file_contents, indent=2)}"
        images = []
        try:
            from system2.visual_inspector import get_image_base64
            images.append({"data": get_image_base64(screenshot_path)})
        except Exception:
            pass

        raw = self._call(system, user, max_tokens=16000, images=images)
        data = _extract_json(raw)
        return data.get("files", {}) or {}, bool(data.get("visual_pass", False)), str(data.get("critique", ""))
