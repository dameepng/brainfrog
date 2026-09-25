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
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

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

    Handles free-form thought processes, markdown prose preceding JSON,
    markdown fences (```json ... ```), nested braces, and trailing commas.
    """
    decoder = json.JSONDecoder()
    text = text.strip()

    # 1. Check for markdown fenced json blocks first (e.g. ```json { ... } ```)
    fence_pattern = re.compile(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", re.DOTALL)
    for match in fence_pattern.finditer(text):
        content = match.group(1).strip()
        try:
            obj, _ = decoder.raw_decode(content)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            # Try cleaning trailing commas
            try:
                cleaned = re.sub(r",\s*([\]\}])", r"\1", content)
                obj = json.loads(cleaned)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                pass

    # 2. Iterate through all '{' occurrences and try decoding
    candidates = []
    idx = 0
    while True:
        pos = text.find("{", idx)
        if pos == -1:
            break
        try:
            obj, end = decoder.raw_decode(text, pos)
            if isinstance(obj, dict):
                candidates.append((pos, end, obj))
                idx = end
            else:
                idx = pos + 1
        except json.JSONDecodeError:
            idx = pos + 1

    if candidates:
        expected_keys = {"files", "steps", "plan", "title", "body", "answer", "tasks"}
        for _, _, obj in candidates:
            if any(k in obj for k in expected_keys):
                return obj
        candidates.sort(key=lambda c: len(c[2]), reverse=True)
        return candidates[0][2]

    # 3. Last fallback: try stripped fences as in original implementation
    if text.startswith("```"):
        lines = text.splitlines()
        inner = lines[1:] if lines[-1].strip() == "```" else lines[1:]
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        text = "\n".join(inner).strip()

    start = text.find("{")
    if start == -1:
        raise ValueError(f"No JSON object found in model output:\n{text[:500]}")

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
    provider_name: str = "claude"

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
        with self.client.messages.stream(
            model=self.model,
            max_tokens=max_tokens,
            system=full_system,
            messages=[{"role": "user", "content": user}],
        ) as stream:
            text = stream.get_final_text()
            final_msg = stream.get_final_message()
            if hasattr(final_msg, "usage") and final_msg.usage:
                usage_tracker.record(
                    getattr(final_msg.usage, "input_tokens", 0),
                    getattr(final_msg.usage, "output_tokens", 0),
                )
        return text


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
    def diagnose(self, user_prompt: str, focus_files: Dict[str, str], domain: str, repo_tree: str = "") -> str:
        """For change_type == 'question_only': explain, don't edit."""
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

        user_text = (
            f"Overall Task: {task}\n\n"
            f"Current file contents:\n{json.dumps(file_contents, indent=2)}\n\n"
            "Please inspect the attached rendered screenshot of the interface against modern UI/UX design standards."
        )

        full_system = self._apply_guidelines(system)
        content_blocks: List[Dict[str, Any]] = []
        try:
            from system2.visual_inspector import get_image_base64
            b64_img = get_image_base64(screenshot_path)
            content_blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": b64_img,
                },
            })
        except Exception:
            pass
        content_blocks.append({"type": "text", "text": user_text})

        with self.client.messages.stream(
            model=self.model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=full_system,
            messages=[{"role": "user", "content": content_blocks}],
        ) as stream:
            raw = stream.get_final_text()
            final_msg = stream.get_final_message()
            if hasattr(final_msg, "usage") and final_msg.usage:
                usage_tracker.record(
                    getattr(final_msg.usage, "input_tokens", 0),
                    getattr(final_msg.usage, "output_tokens", 0),
                )

        data = _extract_json(raw)
        visual_pass = bool(data.get("visual_pass", False))
        critique = str(data.get("critique", ""))
        files = data.get("files", {}) or {}
        return files, visual_pass, critique
