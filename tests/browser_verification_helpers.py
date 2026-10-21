"""Shared helpers for Phase 1D browser verification tests.

Synthetic results here stand in for the browser container ONLY in unit tests of report
assembly and integrity checking; real browser behaviour is covered by
tests/test_verifier_browser_integration.py.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.runtime.browser_plan import BrowserPlan
from core.runtime.browser_sandbox import BrowserExecutionResult, DockerBrowserSandbox

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "browser_app"
# Not a real screenshot: an 8-byte PNG signature plus marker bytes is enough for hashing tests.
FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic-test-image"
STUB_IMAGE_ID = "sha256:" + "ab" * 32

FENCE = "```"


def browser_criterion(cid: str, plan_json: str, title: str = "Browser check", profile: Optional[str] = None) -> str:
    profile_line = f"**App Profile:** {profile}\n" if profile else ""
    return (f"### {cid} — {title}\nBrowser behaviour.\n\n**Verification:** Browser\n{profile_line}"
            f"**Browser Plan:**\n{FENCE}json\n{plan_json}\n{FENCE}\n\n")


def spec_document(*criteria: str) -> str:
    return "# Spec: Browser Tests\n\n## Goal\nBrowser verification tests.\n\n## Acceptance Criteria\n\n" + "".join(criteria)


SIMPLE_PLAN = '{"path": "/login", "steps": [{"action": "fill", "selector": "#email", "value": "x"}, ' \
              '{"action": "expect_visible", "selector": "#login-form"}]}'


def synthetic_result(criterion_id: str, plan: BrowserPlan, snapshot_digest: str, status: str = "PASS",
                     screenshot: bool = True, observed_digest: Optional[str] = None,
                     manifest_overrides: Optional[Dict[str, Any]] = None) -> BrowserExecutionResult:
    records: List[Dict[str, Any]] = []
    assertion_seq = 0
    for i, step in enumerate(plan.steps, start=1):
        rec: Dict[str, Any] = {"criterion_id": criterion_id, "step_index": i, "action": step.action,
                               "kind": "assertion" if step.is_assertion else "action", "status": "PASS",
                               "duration_ms": 1.0}
        if step.is_assertion:
            assertion_seq += 1
            rec["assertion_id"] = f"A{assertion_seq}"
        records.append(rec)
    if status == "FAIL":
        last = max(i for i, r in enumerate(records) if r["kind"] == "assertion")
        records[last].update(status="FAIL", failure_kind="assertion_failed", failure_reason="synthetic failure")
        records = records[:last + 1]
    executed = [r for r in records if r["kind"] == "assertion"]
    artifacts: Dict[str, bytes] = {"browser-console.log": b"", "browser-errors.log": b"", "browser-network.log": b""}
    shot = {"captured": False, "sha256": None, "relative_path": None, "assertion_id": None,
            "captured_at": None, "omitted_reason": "not captured (synthetic)"}
    if screenshot:
        artifacts["screenshot.png"] = FAKE_PNG
        shot = {"captured": True, "sha256": hashlib.sha256(FAKE_PNG).hexdigest(), "relative_path": "screenshot.png",
                "assertion_id": executed[-1]["assertion_id"] if executed else None,
                "captured_at": "2026-01-01T00:00:00+00:00", "omitted_reason": None}
    manifest: Dict[str, Any] = {
        "schema": "brainfrog.browser-assertions/1",
        "criterion_id": criterion_id,
        "status": status,
        "failure_reason": "synthetic failure" if status == "FAIL" else None,
        "failure_kind": "assertion_failed" if status == "FAIL" else None,
        "plan_sha256": plan.plan_sha256(),
        "source_snapshot_digest": snapshot_digest,
        "browser_image_identity": STUB_IMAGE_ID,
        "application_origin_reference": "http://bf-app:8080",
        "runtime": {"browser_name": "chromium", "browser_version": "stub", "playwright_version": "stub",
                    "chromium_internal_sandbox": True, "renderer_sandbox": {"verified": True},
                    "renderer_sandbox_at_end": {"verified": True}},
        "summary": {"total_steps": len(plan.steps), "steps_executed": len(records),
                    "assertions_executed": len(executed),
                    "assertions_passed": sum(1 for r in executed if r["status"] == "PASS"),
                    "assertions_failed": sum(1 for r in executed if r["status"] == "FAIL")},
        "screenshot": shot,
        "records": records,
    }
    manifest.update(manifest_overrides or {})
    artifacts["browser-assertions.json"] = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    return BrowserExecutionResult(
        status=status,
        duration_ms=1.0,
        failure_reason=manifest["failure_reason"],
        failure_kind=manifest["failure_kind"],
        artifacts=artifacts,
        assertion_manifest=manifest,
        assertions_executed=manifest["summary"]["assertions_executed"],
        assertions_passed=manifest["summary"]["assertions_passed"],
        assertions_failed=manifest["summary"]["assertions_failed"],
        observed_source_digest=observed_digest or snapshot_digest,
        plan_sha256=plan.plan_sha256(),
        browser_image={"tag": "stub-browser", "image_id": STUB_IMAGE_ID},
        app_image={"tag": "python:3.12-slim", "image_id": "sha256:" + "cd" * 32},
        app_profile={"profile_id": "python-static", "application_origin": "http://bf-app:8080"},
        browser_runtime={"browser_name": "chromium", "browser_version": "stub", "playwright_version": "stub"},
        cleanup={"browser_removed": True, "app_removed": True, "network_removed": True},
    )


class StubBrowserBackend(DockerBrowserSandbox):
    """Returns pre-programmed results instead of starting containers (unit tests only)."""

    def __init__(self, outcomes: Dict[str, Dict[str, Any]], available: bool = True) -> None:
        super().__init__(docker_executable="bf-test-no-docker")
        self.outcomes = outcomes
        self.available = available
        self.calls: List[Dict[str, Any]] = []

    def is_available(self):
        return (True, None) if self.available else (False, "stub: Docker engine unavailable")

    def engine_version(self):
        return "stub"

    def execute_browser_criterion(self, criterion_id, browser_plan, source_snapshot_dir, source_snapshot_digest,
                                  app_profile_id="python-static", run_id="run"):
        self.calls.append({"criterion_id": criterion_id, "snapshot_dir": Path(source_snapshot_dir),
                           "digest": source_snapshot_digest, "profile": app_profile_id})
        spec = dict(self.outcomes.get(criterion_id, {}))
        if "result" in spec:
            return spec["result"]
        return synthetic_result(criterion_id, browser_plan, source_snapshot_digest, **spec)


def copy_fixture(name: str, dest: Path) -> Path:
    shutil.copytree(FIXTURES / name, dest)
    return dest
