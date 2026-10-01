"""Core engine package for BrainFrog — Planning, Memory, Skills, Domains, and Configuration."""
from __future__ import annotations

from .plans import (
    MODE_BUILD,
    MODE_PLAN,
    PLANS_DIR_REL,
    PlanDocument,
    save_plan_document,
    get_latest_plan,
    format_plan_handoff,
    is_safe_readonly_command,
    check_plan_staleness,
    compute_file_hash,
)
from .memory import (
    Learning,
    MemoryStore,
    load_workspace_memory,
    load_global_memory,
    save_workspace_memory,
    save_global_memory,
    add_learning,
    format_learnings_for_prompt,
)
from .skills import (
    SkillMetadata,
    index_skills,
    select_skill,
    load_skill_content,
    validate_skill_script,
    run_skill_script,
)
from .modules import (
    Domain,
    detect_tech_stack,
    generate_preset_modules,
    auto_generate_modules_json,
    load_module_map,
    auto_discover_modules,
    resolve_focus_tree,
)
from .config import get_system1

__all__ = [
    # Plans
    "MODE_BUILD",
    "MODE_PLAN",
    "PLANS_DIR_REL",
    "PlanDocument",
    "save_plan_document",
    "get_latest_plan",
    "format_plan_handoff",
    "is_safe_readonly_command",
    "check_plan_staleness",
    "compute_file_hash",
    # Memory
    "Learning",
    "MemoryStore",
    "load_workspace_memory",
    "load_global_memory",
    "save_workspace_memory",
    "save_global_memory",
    "add_learning",
    "format_learnings_for_prompt",
    # Skills
    "SkillMetadata",
    "index_skills",
    "select_skill",
    "load_skill_content",
    "validate_skill_script",
    "run_skill_script",
    # Modules
    "Domain",
    "detect_tech_stack",
    "generate_preset_modules",
    "auto_generate_modules_json",
    "load_module_map",
    "auto_discover_modules",
    "resolve_focus_tree",
    # Config
    "get_system1",
    # Images
    "AttachedImage",
    "is_image_path",
    "load_and_validate_image",
    "extract_image_references",
    "get_clipboard_image",
    "capture_screenshot_for_repl",
    "SUPPORTED_IMAGE_EXTENSIONS",
]

from .image_handler import (
    AttachedImage,
    is_image_path,
    load_and_validate_image,
    extract_image_references,
    get_clipboard_image,
    capture_screenshot_for_repl,
    SUPPORTED_IMAGE_EXTENSIONS,
)
