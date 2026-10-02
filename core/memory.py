"""memory.py — Continuous Learning & Episodic Memory Engine for BrainFrog.

Provides persistent, categorized memory that allows the CLI to learn from:
1. Explicit corrections via `/learn <instruction>` (user-driven)
2. Implicit post-mortem reflection after retries or visual fixes (auto-driven)

Memory is stored in two tiers:
- Workspace Memory: `.brainfrog/learnings.json` (repo-specific, committable)
- Global Memory: `~/.brainfrog/learnings.json` (user-wide preferences)

Learnings are injected into System 2 prompts via contextual tag filtering
to keep token usage efficient.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set


# ---------------------------------------------------------------------------
# Data Model
# ---------------------------------------------------------------------------

@dataclass
class Learning:
    """A single piece of learned knowledge."""
    id: str                          # Unique identifier (timestamp-based)
    rule: str                        # The concise rule or lesson (1-2 sentences)
    tags: List[str]                  # Category tags: frontend, backend, test, git, style, etc.
    source: str                      # "explicit" (user /learn) or "reflection" (auto post-mortem)
    context: str = ""                # Brief context of when/why this was learned
    created_at: float = 0.0          # Unix timestamp
    repo_name: str = ""              # Which repo this was learned from (empty = global)
    hit_count: int = 0               # How many times this learning was injected into a prompt

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Learning":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class MemoryStore:
    """Persistent memory store containing a list of learnings."""
    learnings: List[Learning] = field(default_factory=list)
    version: int = 1

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "learnings": [l.to_dict() for l in self.learnings],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryStore":
        return cls(
            version=d.get("version", 1),
            learnings=[Learning.from_dict(l) for l in d.get("learnings", [])],
        )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _workspace_memory_path(repo_dir: Path) -> Path:
    """Path to workspace-level learnings file."""
    return repo_dir / ".brainfrog" / "learnings.json"


def _global_memory_path() -> Path:
    """Path to user-level global learnings file."""
    home = Path(os.path.expanduser("~"))
    return home / ".brainfrog" / "learnings.json"


def load_memory(path: Path) -> MemoryStore:
    """Load a MemoryStore from a JSON file. Returns empty store if not found."""
    if not path.exists():
        return MemoryStore()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return MemoryStore.from_dict(data)
    except Exception:
        return MemoryStore()


def save_memory(store: MemoryStore, path: Path) -> None:
    """Persist a MemoryStore to a JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(store.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def load_workspace_memory(repo_dir: Path) -> MemoryStore:
    return load_memory(_workspace_memory_path(repo_dir))


def save_workspace_memory(store: MemoryStore, repo_dir: Path) -> None:
    save_memory(store, _workspace_memory_path(repo_dir))


def load_global_memory() -> MemoryStore:
    return load_memory(_global_memory_path())


def save_global_memory(store: MemoryStore) -> None:
    save_memory(store, _global_memory_path())


# ---------------------------------------------------------------------------
# Learning Creation
# ---------------------------------------------------------------------------

def _generate_id() -> str:
    """Generate a time-based unique ID for a learning entry."""
    return f"L-{int(time.time() * 1000)}"


# Default tag inference keywords
_TAG_KEYWORDS: Dict[str, List[str]] = {
    "frontend": ["html", "css", "jsx", "tsx", "vue", "svelte", "ui", "layout", "design",
                  "glassmorphism", "card", "button", "input", "header", "footer",
                  "flex", "grid", "font", "color", "margin", "padding", "spacing",
                  "responsive", "dark mode", "light mode", "animation", "hover"],
    "backend": ["api", "server", "database", "sql", "query", "auth", "endpoint",
                "middleware", "route", "controller", "model", "schema", "migration"],
    "test": ["test", "jest", "mocha", "pytest", "unittest", "assert", "expect",
             "coverage", "mock", "fixture", "spec"],
    "git": ["commit", "branch", "merge", "push", "pull", "rebase", "remote",
            "gitignore", "tag", "diff"],
    "style": ["slop", "card-ception", "nesting", "alignment", "balance",
              "whitespace", "contrast", "typography", "composition", "premium",
              "modern", "linear", "raycast", "apple"],
    "performance": ["performance", "speed", "lazy", "bundle", "optimize",
                     "cache", "memory", "latency"],
}


def infer_tags(text: str) -> List[str]:
    """Auto-infer category tags from the text content of a learning."""
    text_lower = text.lower()
    matched: Set[str] = set()
    for tag, keywords in _TAG_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower:
                matched.add(tag)
                break
    if not matched:
        matched.add("general")
    return sorted(matched)


def add_learning(
    store: MemoryStore,
    rule: str,
    source: str = "explicit",
    context: str = "",
    tags: Optional[List[str]] = None,
    repo_name: str = "",
) -> Learning:
    """Create and append a new learning to the store.

    Performs deduplication: if a very similar rule already exists, it
    increments the hit_count instead of creating a duplicate.
    """
    # Deduplication check: if >70% of words overlap with an existing rule
    rule_words = set(rule.lower().split())
    for existing in store.learnings:
        existing_words = set(existing.rule.lower().split())
        if not rule_words or not existing_words:
            continue
        overlap = len(rule_words & existing_words) / max(len(rule_words), len(existing_words))
        if overlap > 0.7:
            existing.hit_count += 1
            if context and not existing.context:
                existing.context = context
            return existing

    learning = Learning(
        id=_generate_id(),
        rule=rule.strip(),
        tags=tags or infer_tags(rule),
        source=source,
        context=context,
        created_at=time.time(),
        repo_name=repo_name,
        hit_count=1,
    )
    store.learnings.append(learning)
    return learning


def remove_learning(store: MemoryStore, learning_id: str) -> bool:
    """Remove a learning by its ID. Returns True if found and removed."""
    original_len = len(store.learnings)
    store.learnings = [l for l in store.learnings if l.id != learning_id]
    return len(store.learnings) < original_len


def clear_learnings(store: MemoryStore) -> int:
    """Clear all learnings. Returns count of removed items."""
    count = len(store.learnings)
    store.learnings.clear()
    return count


# ---------------------------------------------------------------------------
# Contextual Retrieval (Tag-based RAG)
# ---------------------------------------------------------------------------

def get_relevant_learnings(
    store: MemoryStore,
    task: str = "",
    touched_files: Optional[List[str]] = None,
    max_items: int = 15,
) -> List[Learning]:
    """Retrieve learnings relevant to the current task context.

    Uses tag inference from the task description and touched file extensions
    to filter learnings. Falls back to all learnings if no specific match.
    """
    if not store.learnings:
        return []

    # Infer relevant tags from task + touched files
    relevant_tags: Set[str] = set()
    if task:
        relevant_tags.update(infer_tags(task))

    if touched_files:
        frontend_exts = {".html", ".htm", ".css", ".scss", ".jsx", ".tsx", ".vue", ".svelte"}
        backend_exts = {".py", ".go", ".rs", ".java", ".rb", ".php"}
        test_patterns = {"test", "spec", "__test__", "_test"}
        for f in touched_files:
            ext = Path(f).suffix.lower()
            name = Path(f).stem.lower()
            if ext in frontend_exts:
                relevant_tags.add("frontend")
                relevant_tags.add("style")
            elif ext in backend_exts:
                relevant_tags.add("backend")
            if any(p in name for p in test_patterns):
                relevant_tags.add("test")

    # If we have specific tags, filter; otherwise return all
    if relevant_tags and "general" not in relevant_tags:
        scored: List[tuple] = []
        for l in store.learnings:
            tag_overlap = len(set(l.tags) & relevant_tags)
            if tag_overlap > 0:
                # Score: tag overlap * hit_count (more frequently relevant = higher priority)
                score = tag_overlap * (1 + l.hit_count * 0.1)
                scored.append((score, l))
        scored.sort(key=lambda x: x[0], reverse=True)
        results = [l for _, l in scored[:max_items]]
        # If very few tag-filtered results, pad with general/high-hit-count ones
        if len(results) < 3:
            remaining = [l for l in store.learnings if l not in results]
            remaining.sort(key=lambda l: l.hit_count, reverse=True)
            results.extend(remaining[:max_items - len(results)])
        return results[:max_items]

    # No specific tags: return top learnings sorted by recency
    sorted_learnings = sorted(store.learnings, key=lambda l: l.created_at, reverse=True)
    return sorted_learnings[:max_items]


def format_learnings_for_prompt(learnings: List[Learning], max_tokens_approx: int = 1500) -> str:
    """Format learnings into a concise prompt injection block.

    Keeps total text under max_tokens_approx characters (~4 chars per token).
    """
    if not learnings:
        return ""

    max_chars = max_tokens_approx * 4
    lines = ["[Continuous Learning Memory — Repository & User Preferences]"]
    char_count = len(lines[0])

    for l in learnings:
        tag_str = ", ".join(l.tags)
        line = f"- [{tag_str}] {l.rule}"
        if char_count + len(line) + 1 > max_chars:
            lines.append(f"... ({len(learnings) - len(lines) + 1} more learnings omitted for token efficiency)")
            break
        lines.append(line)
        char_count += len(line) + 1

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Reflection Prompt Generator
# ---------------------------------------------------------------------------

def is_generic_suggestion(sug: Optional[str]) -> bool:
    """Return True if suggestion contains banned generic, non-actionable advice."""
    if not sug:
        return False
    s = sug.lower()
    banned_phrases = [
        "selalu testing",
        "testing dengan baik",
        "pastikan untuk testing",
        "selalu uji coba",
        "tulis dokumentasi",
        "pastikan kode rapi",
        "maintain clean code",
        "always write tests",
        "make sure to test",
        "ensure thorough testing",
        "add comprehensive tests",
    ]
    return any(bp in s for bp in banned_phrases)


def generate_proactive_suggestion_prompt(
    task: str,
    step_desc: str = "",
    diff_snippet: str = "",
) -> str:
    """Generate a prompt for evaluating proactive suggestions after a successful step.

    This prompt asks System 2 to identify genuine technical gaps, missing tests,
    unhandled edge cases, or potential improvements, or return null if clean.
    """
    effective_step = step_desc or task
    diff_part = f"\n\nCode changes made (diff excerpt):\n{diff_snippet[:2000]}" if diff_snippet else ""

    return (
        "A step in the coding task has just PASSED all tests and succeeded.\n\n"
        f"Original user task: {task}\n"
        f"Step completed: {effective_step}"
        f"{diff_part}\n\n"
        "Evaluate the completed work according to the Proactive Suggestion rules:\n"
        "Is there an obvious, specific gap, potential improvement, or risk in the result of this step "
        "that was NOT explicitly requested by the user in this task?\n"
        "Examples of valid gaps:\n"
        "- Newly added component uses hardcoded placeholder data instead of dynamic props/state.\n"
        "- Newly added image or asset is hotlinked to an external URL (e.g. Unsplash) risking 404.\n"
        "- A new function or utility was introduced without test coverage.\n"
        "- An obvious edge case is left unhandled.\n\n"
        "CRITICAL RULES:\n"
        "1. If YES and truly relevant/specific to this exact work:\n"
        '   Write 1-3 natural Indonesian sentences, format: "Catatan: [gap/observasi spesifik]. Mau sekalian saya kerjakan juga?"\n'
        "2. If NO clear/significant gap exists: set suggestion to null.\n"
        "   Do NOT invent suggestions just to look proactive. Silence (null) is far better than generic noise.\n"
        "3. NEVER output generic advice that applies to any project (e.g., 'pastikan selalu testing', 'buat dokumentasi').\n"
        "4. Maximum ONE concise suggestion.\n\n"
        "Respond with ONLY JSON:\n"
        '{"suggestion": "Catatan: [gap spesifik]. Mau sekalian saya kerjakan juga?"}\n'
        '(Or {"suggestion": null} if no genuine gap exists).'
    )


def generate_reflection_prompt(
    task: str,
    retries: int,
    visual_fixed: bool,
    error_summary: str = "",
    include_proactive: bool = False,
    step_desc: str = "",
    diff_snippet: str = "",
) -> str:
    """Generate a reflection prompt for auto-learning after a difficult task.

    This prompt is sent to System 2 to extract a concise lesson learned.
    If include_proactive is True, combines internal lessons extraction with
    evaluating user-facing proactive suggestions in a single call.
    """
    context_parts = []
    if retries > 0:
        context_parts.append(f"The task required {retries} retry attempt(s) before tests passed.")
    if visual_fixed:
        context_parts.append("The Visual Quality Gate detected and auto-fixed UI/CSS defects.")
    if error_summary:
        context_parts.append(f"Error encountered: {error_summary[:300]}")

    context = " ".join(context_parts)
    effective_step = step_desc or task

    if not include_proactive:
        return (
            "You just completed a task that required extra effort to get right. "
            f"Context: {context}\n\n"
            f"Original task: {task}\n\n"
            "Extract 1-3 concise, actionable lessons learned from this experience. "
            "Each lesson should be a single sentence that would help avoid the same "
            "pitfall in future tasks on this codebase.\n\n"
            "Respond with ONLY JSON:\n"
            '{"learnings": [{"rule": "concise lesson", "tags": ["frontend", "style"]}]}\n'
            "Tags must be from: frontend, backend, test, git, style, performance, general."
        )

    diff_part = f"\n\nCode changes made (diff excerpt):\n{diff_snippet[:2000]}" if diff_snippet else ""

    return (
        "You just completed a task step that required extra effort to get right.\n"
        f"Context: {context}\n"
        f"Original task: {task}\n"
        f"Step completed: {effective_step}"
        f"{diff_part}\n\n"
        "Perform a two-fold post-task reflection:\n"
        "1. Internal Learning: Extract 1-3 concise, actionable lessons learned to avoid the same "
        "pitfall in future tasks on this codebase (single sentence each).\n"
        "2. Proactive Suggestion for User: Evaluate if there is an obvious, specific gap, potential improvement, "
        "or risk in the result of this step that was NOT explicitly requested by the user in this task.\n"
        "   - If YES and truly relevant/specific to this work:\n"
        '     Write 1-3 natural Indonesian sentences, format: "Catatan: [gap/observasi spesifik]. Mau sekalian saya kerjakan juga?"\n'
        "   - If NO clear/significant gap exists: set suggestion to null.\n"
        "   - Max ONE suggestion. NEVER output generic advice like 'pastikan selalu testing'.\n\n"
        "Respond with ONLY JSON:\n"
        '{\n'
        '  "learnings": [{"rule": "concise lesson", "tags": ["frontend", "style"]}],\n'
        '  "suggestion": "Catatan: [gap spesifik]. Mau sekalian saya kerjakan juga?"\n'
        '}\n'
        '(Note: set "suggestion": null if no genuine gap exists).\n'
        "Tags must be from: frontend, backend, test, git, style, performance, general."
    )

