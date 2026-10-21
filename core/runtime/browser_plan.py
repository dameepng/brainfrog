# -*- coding: utf-8 -*-
"""Declarative browser plan schema, parser, and strict validator for BrainFrog Phase 1D.

Product Invariant:
- Declarative browser plans only: NO arbitrary JavaScript, shell, or Python code from spec.md.
- Strict bounded allowlist of actions and assertions; unknown keys are rejected.
- Selectors are CSS only (no Playwright engine prefixes, XPath, or '>>' chaining).
- Paths are root-relative to a fixed, BrainFrog-chosen application origin.
- Completed navigation or click does NOT equal PASS.
- Meaningful assertions must drive the verdict; zero assertions cannot PASS.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class BrowserActionType(str, Enum):
    # Navigation
    GOTO_RELATIVE = "goto_relative"

    # User Interactions
    FILL = "fill"
    CLICK = "click"
    SELECT_OPTION = "select_option"
    CHECK_CHECKBOX = "check_checkbox"

    # Assertions (drive PASS/FAIL verdict)
    EXPECT_VISIBLE = "expect_visible"
    EXPECT_HIDDEN = "expect_hidden"
    EXPECT_TEXT = "expect_text"
    EXPECT_COUNT = "expect_count"
    EXPECT_URL_PATH = "expect_url_path"
    EXPECT_NO_HORIZONTAL_OVERFLOW = "expect_no_horizontal_overflow"


# Disallowed actions that must fail closed with explicit security errors
DISALLOWED_ACTIONS = {
    "evaluate",
    "execute_script",
    "javascript",
    "eval",
    "script",
    "shell",
    "python",
    "arbitrary_network_request",
    "exec",
    "download",
    "upload",
    "screenshot",  # screenshots are evidence captured by BrainFrog, never a plan step
}

# The set of actions that constitute meaningful verification assertions
ASSERTION_ACTIONS = {
    BrowserActionType.EXPECT_VISIBLE.value,
    BrowserActionType.EXPECT_HIDDEN.value,
    BrowserActionType.EXPECT_TEXT.value,
    BrowserActionType.EXPECT_COUNT.value,
    BrowserActionType.EXPECT_URL_PATH.value,
    BrowserActionType.EXPECT_NO_HORIZONTAL_OVERFLOW.value,
}

# Bounds enforcement
MAX_TOTAL_STEPS = 50
MAX_SELECTOR_LENGTH = 256
MAX_TEXT_LENGTH = 1000
MAX_VALUE_LENGTH = 1000
MAX_PATH_LENGTH = 512
MAX_EXPECTED_COUNT = 1000
MIN_VIEWPORT_WIDTH = 320
MAX_VIEWPORT_WIDTH = 3840
MIN_VIEWPORT_HEIGHT = 320
MAX_VIEWPORT_HEIGHT = 2160
MIN_DEVICE_SCALE_FACTOR = 1.0
MAX_DEVICE_SCALE_FACTOR = 3.0
MIN_TIMEOUT_MS = 100
MAX_TIMEOUT_MS = 15000
DEFAULT_TIMEOUT_MS = 5000
INITIAL_NAVIGATION_TIMEOUT_MS = 15000
# Upper bound on the sum of all step timeouts plus the initial navigation.
MAX_PLAN_BUDGET_MS = 120000
MAX_PLAN_JSON_BYTES = 64 * 1024

_STEP_KEYS: Dict[str, set] = {
    "goto_relative": {"path"},
    "click": {"selector"},
    "check_checkbox": {"selector"},
    "fill": {"selector", "value"},
    "select_option": {"selector", "value"},
    "expect_visible": {"selector"},
    "expect_hidden": {"selector"},
    "expect_text": {"selector", "contains", "equals"},
    "expect_count": {"selector", "count"},
    "expect_url_path": {"path"},
    "expect_no_horizontal_overflow": set(),
}
_COMMON_STEP_KEYS = {"action", "timeout_ms", "id"}
_PLAN_KEYS = {"path", "viewport", "steps"}
_VIEWPORT_KEYS = {"width", "height", "device_scale_factor"}

# RFC 3986 path/query/fragment characters only; no whitespace, backslashes, or controls.
_PATH_RE = re.compile(r"^/[A-Za-z0-9\-._~/%?=&+,;:@!$'()*#]*$")
_STEP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# A leading 'name=' or 'name:...=' selects a Playwright selector engine (text=, xpath=, internal:...).
_ENGINE_PREFIX_RE = re.compile(r"^\s*[A-Za-z_][\w:-]*\s*=")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass
class ViewportConfig:
    width: int = 1280
    height: int = 720
    device_scale_factor: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "device_scale_factor": self.device_scale_factor,
        }


@dataclass
class BrowserStep:
    action: str
    selector: Optional[str] = None
    value: Optional[str] = None
    contains: Optional[str] = None
    equals: Optional[str] = None
    count: Optional[int] = None
    path: Optional[str] = None
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    step_id: Optional[str] = None

    @property
    def is_assertion(self) -> bool:
        return self.action in ASSERTION_ACTIONS

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"action": self.action}
        if self.step_id is not None:
            d["id"] = self.step_id
        if self.selector is not None:
            d["selector"] = self.selector
        if self.value is not None:
            d["value"] = self.value
        if self.contains is not None:
            d["contains"] = self.contains
        if self.equals is not None:
            d["equals"] = self.equals
        if self.count is not None:
            d["count"] = self.count
        if self.path is not None:
            d["path"] = self.path
        d["timeout_ms"] = self.timeout_ms
        return d


@dataclass
class BrowserPlan:
    path: str
    viewport: ViewportConfig = field(default_factory=ViewportConfig)
    steps: List[BrowserStep] = field(default_factory=list)
    raw_json: Optional[str] = None

    @property
    def assertion_count(self) -> int:
        return sum(1 for s in self.steps if s.is_assertion)

    @property
    def has_meaningful_assertions(self) -> bool:
        return self.assertion_count > 0

    @property
    def budget_ms(self) -> int:
        """Worst-case browser time: initial navigation plus every step timeout."""
        return INITIAL_NAVIGATION_TIMEOUT_MS + sum(s.timeout_ms for s in self.steps)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "viewport": self.viewport.to_dict(),
            "steps": [s.to_dict() for s in self.steps],
        }

    def canonical_json(self) -> str:
        """Normalized plan text: exactly what the browser runner receives."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def plan_sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class BrowserPlanValidationError(ValueError):
    """Raised when a browser plan violates declarative safety or bounding rules."""
    pass


def _require_int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BrowserPlanValidationError(f"{what} must be an integer, got {type(value).__name__}")
    return value


def _require_text(value: Any, what: str, max_len: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise BrowserPlanValidationError(f"{what} must be a string, got {type(value).__name__}")
    if not allow_empty and value == "":
        raise BrowserPlanValidationError(f"{what} must not be empty")
    if len(value) > max_len:
        raise BrowserPlanValidationError(f"{what} length {len(value)} exceeds limit {max_len}")
    if _CONTROL_RE.search(value):
        raise BrowserPlanValidationError(f"{what} contains control characters")
    return value


def validate_relative_path(path_str: Any) -> str:
    """Ensure path is strictly relative to application origin without traversal or scheme escapes."""
    if not isinstance(path_str, str):
        raise BrowserPlanValidationError(f"Invalid path type: expected str, got {type(path_str).__name__}")
    p = path_str
    if not p.startswith("/"):
        raise BrowserPlanValidationError(f"Browser navigation path must be root-relative starting with '/': '{p[:80]}'")
    if "://" in p or p.startswith("//"):
        raise BrowserPlanValidationError(f"Absolute URLs, schemes, or protocol-relative paths are forbidden: '{p[:80]}'")
    if len(p) > MAX_PATH_LENGTH:
        raise BrowserPlanValidationError(f"Path exceeds maximum length of {MAX_PATH_LENGTH} chars")
    if not _PATH_RE.match(p):
        raise BrowserPlanValidationError(
            f"Path contains characters outside the allowed URL path set (no whitespace, backslashes or controls): '{p[:80]}'"
        )
    path_only = re.split(r"[?#]", p, maxsplit=1)[0]
    segments = path_only.split("/")
    if ".." in segments or any(s.lower() in ("%2e%2e", ".%2e", "%2e.") for s in segments):
        raise BrowserPlanValidationError(f"Directory traversal ('..') is forbidden in path: '{p[:80]}'")
    return p


def validate_selector(selector_str: Any, action: str) -> str:
    """Accept only plain CSS selectors (passed to Playwright as 'css=<selector>')."""
    if not selector_str or not isinstance(selector_str, str):
        raise BrowserPlanValidationError(f"Action '{action}' requires a non-empty string 'selector'")
    s = selector_str.strip()
    if not s:
        raise BrowserPlanValidationError(f"Action '{action}' requires a non-empty string 'selector'")
    if len(s) > MAX_SELECTOR_LENGTH:
        raise BrowserPlanValidationError(f"Selector exceeds maximum length of {MAX_SELECTOR_LENGTH} chars: '{s[:30]}...'")
    if _CONTROL_RE.search(s):
        raise BrowserPlanValidationError("Selector contains control characters")
    lowered = s.lower()
    if "<" in s or "javascript:" in lowered or "eval(" in lowered:
        raise BrowserPlanValidationError(f"Potentially malicious or invalid characters in selector: '{s[:80]}'")
    if ">>" in s:
        raise BrowserPlanValidationError("Selector chaining ('>>') is not supported; use a single CSS selector")
    if _ENGINE_PREFIX_RE.match(s) or s.startswith(("//", "..", "(")):
        raise BrowserPlanValidationError(
            f"Only CSS selectors are supported (no selector-engine prefixes or XPath): '{s[:80]}'"
        )
    return s


def parse_browser_plan_data(data: Any) -> BrowserPlan:
    """Parse and strictly validate a browser plan dictionary."""
    if not isinstance(data, dict):
        raise BrowserPlanValidationError(f"Browser plan must be a JSON object, got {type(data).__name__}")
    unknown = set(data) - _PLAN_KEYS
    if unknown:
        raise BrowserPlanValidationError(f"Unknown browser plan keys: {sorted(unknown)}")

    # 1. Path validation
    validated_path = validate_relative_path(data.get("path", "/"))

    # 2. Viewport validation
    raw_vp = data.get("viewport", {})
    if not isinstance(raw_vp, dict):
        raise BrowserPlanValidationError("Viewport configuration must be an object")
    unknown_vp = set(raw_vp) - _VIEWPORT_KEYS
    if unknown_vp:
        raise BrowserPlanValidationError(f"Unknown viewport keys: {sorted(unknown_vp)}")
    vp_w = _require_int(raw_vp.get("width", 1280), "Viewport width")
    vp_h = _require_int(raw_vp.get("height", 720), "Viewport height")
    raw_scale = raw_vp.get("device_scale_factor", 1.0)
    if isinstance(raw_scale, bool) or not isinstance(raw_scale, (int, float)):
        raise BrowserPlanValidationError("Viewport device_scale_factor must be a number")
    vp_scale = float(raw_scale)

    if not (MIN_VIEWPORT_WIDTH <= vp_w <= MAX_VIEWPORT_WIDTH):
        raise BrowserPlanValidationError(f"Viewport width {vp_w} out of allowed bounds [{MIN_VIEWPORT_WIDTH}, {MAX_VIEWPORT_WIDTH}]")
    if not (MIN_VIEWPORT_HEIGHT <= vp_h <= MAX_VIEWPORT_HEIGHT):
        raise BrowserPlanValidationError(f"Viewport height {vp_h} out of allowed bounds [{MIN_VIEWPORT_HEIGHT}, {MAX_VIEWPORT_HEIGHT}]")
    if not (MIN_DEVICE_SCALE_FACTOR <= vp_scale <= MAX_DEVICE_SCALE_FACTOR):
        raise BrowserPlanValidationError(
            f"Viewport device_scale_factor {vp_scale} out of allowed bounds [{MIN_DEVICE_SCALE_FACTOR}, {MAX_DEVICE_SCALE_FACTOR}]"
        )

    viewport = ViewportConfig(width=vp_w, height=vp_h, device_scale_factor=vp_scale)

    # 3. Steps validation
    raw_steps = data.get("steps", [])
    if not isinstance(raw_steps, list):
        raise BrowserPlanValidationError("Browser plan 'steps' must be a list")
    if len(raw_steps) > MAX_TOTAL_STEPS:
        raise BrowserPlanValidationError(f"Browser plan contains {len(raw_steps)} steps, exceeding limit of {MAX_TOTAL_STEPS}")

    allowed_actions = {a.value for a in BrowserActionType}
    steps: List[BrowserStep] = []
    seen_ids: set = set()

    for idx, s in enumerate(raw_steps):
        n = idx + 1
        if not isinstance(s, dict):
            raise BrowserPlanValidationError(f"Step {n} must be an object")

        action_name = s.get("action")
        if not action_name or not isinstance(action_name, str):
            raise BrowserPlanValidationError(f"Step {n} missing valid 'action' name")

        action_clean = action_name.strip().lower()
        if action_clean in DISALLOWED_ACTIONS:
            raise BrowserPlanValidationError(
                f"Step {n} uses forbidden action '{action_clean}'. Arbitrary code execution or scripting is rejected."
            )
        if action_clean not in allowed_actions or action_name != action_clean:
            raise BrowserPlanValidationError(
                f"Step {n} uses unknown action '{action_name[:40]}'. Allowed actions: {sorted(allowed_actions)}"
            )

        unknown_keys = set(s) - _COMMON_STEP_KEYS - _STEP_KEYS[action_clean]
        if unknown_keys:
            raise BrowserPlanValidationError(f"Step {n} ('{action_clean}') has unsupported keys: {sorted(unknown_keys)}")

        timeout_ms = _require_int(s.get("timeout_ms", DEFAULT_TIMEOUT_MS), f"Step {n} timeout_ms")
        if not (MIN_TIMEOUT_MS <= timeout_ms <= MAX_TIMEOUT_MS):
            raise BrowserPlanValidationError(
                f"Step {n} timeout_ms {timeout_ms} out of bounds ({MIN_TIMEOUT_MS}..{MAX_TIMEOUT_MS})"
            )

        step_id = None
        if "id" in s:
            if not isinstance(s["id"], str) or not _STEP_ID_RE.match(s["id"]):
                raise BrowserPlanValidationError(f"Step {n} 'id' must match [A-Za-z0-9_-]{{1,64}}")
            if s["id"] in seen_ids:
                raise BrowserPlanValidationError(f"Step {n} duplicates step id '{s['id']}'")
            seen_ids.add(s["id"])
            step_id = s["id"]

        selector = None
        value = None
        contains = None
        equals = None
        count = None
        step_path = None

        if action_clean in (BrowserActionType.GOTO_RELATIVE.value, BrowserActionType.EXPECT_URL_PATH.value):
            if "path" not in s:
                raise BrowserPlanValidationError(f"Step {n} '{action_clean}' requires 'path'")
            step_path = validate_relative_path(s["path"])

        elif action_clean in (BrowserActionType.CLICK.value, BrowserActionType.CHECK_CHECKBOX.value,
                              BrowserActionType.EXPECT_VISIBLE.value, BrowserActionType.EXPECT_HIDDEN.value):
            selector = validate_selector(s.get("selector"), action_clean)

        elif action_clean in (BrowserActionType.FILL.value, BrowserActionType.SELECT_OPTION.value):
            selector = validate_selector(s.get("selector"), action_clean)
            if "value" not in s:
                raise BrowserPlanValidationError(f"Step {n} '{action_clean}' requires string 'value'")
            value = _require_text(s["value"], f"Step {n} 'value'", MAX_VALUE_LENGTH, allow_empty=True)

        elif action_clean == BrowserActionType.EXPECT_TEXT.value:
            selector = validate_selector(s.get("selector"), action_clean)
            if ("contains" in s) == ("equals" in s):
                raise BrowserPlanValidationError(f"Step {n} 'expect_text' requires exactly one of 'contains' or 'equals'")
            if "contains" in s:
                contains = _require_text(s["contains"], f"Step {n} 'contains'", MAX_TEXT_LENGTH)
            else:
                equals = _require_text(s["equals"], f"Step {n} 'equals'", MAX_TEXT_LENGTH, allow_empty=True)

        elif action_clean == BrowserActionType.EXPECT_COUNT.value:
            selector = validate_selector(s.get("selector"), action_clean)
            if "count" not in s:
                raise BrowserPlanValidationError(f"Step {n} 'expect_count' requires integer 'count'")
            if isinstance(s["count"], bool) or not isinstance(s["count"], int) or not (0 <= s["count"] <= MAX_EXPECTED_COUNT):
                raise BrowserPlanValidationError(
                    f"Step {n} 'expect_count' requires a non-negative integer 'count' (max {MAX_EXPECTED_COUNT})"
                )
            count = s["count"]

        # expect_no_horizontal_overflow: trusted layout assertion, no selector.

        steps.append(
            BrowserStep(
                action=action_clean,
                selector=selector,
                value=value,
                contains=contains,
                equals=equals,
                count=count,
                path=step_path,
                timeout_ms=timeout_ms,
                step_id=step_id,
            )
        )

    # 4. Invariant: At least one meaningful assertion must be configured
    plan = BrowserPlan(path=validated_path, viewport=viewport, steps=steps)
    if not plan.has_meaningful_assertions:
        raise BrowserPlanValidationError(
            "False browser proof rejected: plan contains zero assertions (expect_*). "
            "Completed navigation alone does not prove acceptance functionality."
        )
    if plan.budget_ms > MAX_PLAN_BUDGET_MS:
        raise BrowserPlanValidationError(
            f"Browser plan worst-case duration {plan.budget_ms}ms exceeds the per-criterion limit of {MAX_PLAN_BUDGET_MS}ms"
        )

    return plan


def parse_browser_plan(plan_str: str) -> BrowserPlan:
    """Parse a JSON browser plan string and validate it."""
    if not plan_str or not plan_str.strip():
        raise BrowserPlanValidationError("Browser plan content is empty.")
    if len(plan_str.encode("utf-8")) > MAX_PLAN_JSON_BYTES:
        raise BrowserPlanValidationError(f"Browser plan exceeds {MAX_PLAN_JSON_BYTES} bytes.")
    try:
        data = json.loads(plan_str)
    except json.JSONDecodeError as err:
        raise BrowserPlanValidationError(f"Invalid JSON in browser plan: {err}")
    plan = parse_browser_plan_data(data)
    plan.raw_json = plan_str
    return plan
