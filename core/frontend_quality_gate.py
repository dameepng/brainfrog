"""Frontend Quality Gate backed by MCP browser verification server.

Layer 2: Domain-specific verification pipeline.
Consumes the generic McpClient (Layer 1) to execute an automated 7-step quality gate:
1. Connect to MCP server (brainfrog-verify-mcp)
2. Run build step (e.g. `npm run build`) and poll output until exitCode != null
3. If build fails, abort immediately with build errors
4. Launch dev server (e.g. `npm run dev`)
5. Retry navigate to dev_url until server is responsive (poll-based, not static sleep)
6. Capture console errors (onlyErrors=True) and failed network requests (onlyFailed=True)
7. Capture full-page screenshot (base64)
8. Always clean up processes and browser context in finally block (zero zombies)
"""
# Quality Gate is strictly skipped if no frontend files are touched in the PR.
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from core.mcp_client import McpClient

DEFAULT_MCP_SERVER_PATH = os.environ.get(
    "BRAINFROG_VERIFY_MCP_PATH",
    r"C:\dame-project\tools\brainfrog-verify-mcp\dist\index.js",
)
DEFAULT_DEV_URL = os.environ.get("BRAINFROG_DEV_URL", "http://localhost:3000")

FRONTEND_EXTENSIONS = {
    ".html", ".htm",
    ".css", ".scss", ".sass", ".less",
    ".jsx", ".tsx",
    ".vue", ".svelte",
}

IGNORED_DIRECTORIES = {
    "node_modules", ".git", "dist", "build", ".next", "out",
    "coverage", ".brainfrog", "docs", "documentation", ".github",
    "vendor", "__pycache__", ".venv", "venv",
}

NON_FRONTEND_EXTENSIONS = {
    ".md", ".markdown", ".rst", ".txt", ".json", ".lock",
    ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".py", ".go", ".rs", ".java", ".c", ".cpp", ".rb", ".php", ".sh",
}

FRONTEND_DIRECTORIES = {
    "src", "components", "pages", "app", "views", "ui", "styles", "public", "frontend"
}


def is_frontend_change(files: Iterable[str]) -> bool:
    """Check if any of the touched files affect frontend UI/UX.

    Returns False for backend-only, config, documentation, and non-visual files,
    as well as files inside ignored directories (e.g. node_modules, docs, .github).
    """
    for f in files:
        if not f or not str(f).strip():
            continue
        p = Path(f)
        parts = [part.lower() for part in p.parts]

        # Ignore non-source or excluded directories
        if any(ign in parts for ign in IGNORED_DIRECTORIES):
            continue

        ext = p.suffix.lower()

        # Explicit non-frontend / documentation / backend extensions
        if ext in NON_FRONTEND_EXTENSIONS:
            continue

        # Direct frontend extensions (.tsx, .jsx, .css, .html, .vue, .svelte, etc.)
        if ext in FRONTEND_EXTENSIONS:
            return True

        # JS/TS logic files: check if they are located within frontend directories
        if ext in {".js", ".ts", ".mjs"}:
            backend_dirs = {"backend", "server", "api", "services"}
            has_fe_dir = any(d in parts for d in FRONTEND_DIRECTORIES)
            has_be_dir = any(d in parts for d in backend_dirs)
            if has_fe_dir and not has_be_dir:
                return True

    return False


@dataclass
class QualityGateResult:
    """Structured report returned by the frontend quality gate."""

    status: str  # "PASS" | "FAIL"
    build_output: str = ""
    console_errors: List[Any] = field(default_factory=list)
    failed_requests: List[Any] = field(default_factory=list)
    screenshot_base64: Optional[str] = None
    screenshot_desktop_base64: Optional[str] = None
    screenshot_mobile_base64: Optional[str] = None
    reason: str = ""
    duration_seconds: float = 0.0

    def __post_init__(self):
        if self.screenshot_desktop_base64 and not self.screenshot_base64:
            self.screenshot_base64 = self.screenshot_desktop_base64
        elif self.screenshot_base64 and not self.screenshot_desktop_base64:
            self.screenshot_desktop_base64 = self.screenshot_base64

    @property
    def passed(self) -> bool:
        return self.status == "PASS"

    def to_agent_context(self) -> str:
        """Format errors into high-signal feedback for System 2 to review and fix."""
        lines = [f"[Frontend Quality Gate Verification: {self.status}]", f"Reason: {self.reason}"]

        if self.build_output.strip():
            lines.append(f"\n--- Build Output ---\n{self.build_output[-2500:].strip()}")

        if self.console_errors:
            lines.append("\n--- Browser Console Errors ---")
            for err in self.console_errors[:10]:
                if isinstance(err, dict):
                    msg = err.get("text") or err.get("message") or json.dumps(err)
                    lines.append(f"- {msg}")
                else:
                    lines.append(f"- {err}")

        if self.failed_requests:
            lines.append("\n--- Failed Network Requests (404/500/Errors) ---")
            for req in self.failed_requests[:10]:
                if isinstance(req, dict):
                    url = req.get("url", "unknown")
                    status = req.get("status", "failed")
                    method = req.get("method", "GET")
                    lines.append(f"- [{method}] {url} -> status {status}")
                else:
                    lines.append(f"- {req}")

        lines.append(
            "\nPlease fix the code so that the project builds cleanly with 0 console errors and 0 failed requests."
        )
        return "\n".join(lines).strip()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "buildOutput": self.build_output,
            "consoleErrors": self.console_errors,
            "failedRequests": self.failed_requests,
            "screenshotBase64": self.screenshot_base64[:50] + "..." if self.screenshot_base64 else None,
            "reason": self.reason,
            "durationSeconds": round(self.duration_seconds, 2),
        }


def run_frontend_quality_gate(
    project_cwd: Path | str,
    dev_url: str = DEFAULT_DEV_URL,
    instance_id: Optional[str] = None,
    mcp_command: str = "node",
    mcp_args: Optional[List[str]] = None,
    timeout_seconds: float = 120.0,
    log_callback: Optional[Callable[[str], None]] = None,
    capture_mobile: bool = False,
) -> QualityGateResult:
    """Run full browser verification via MCP server against target frontend project.

    Args:
        project_cwd: Working directory of target project (must contain package.json or web app).
        dev_url: Target URL for browser verification (default: http://localhost:3000).
        instance_id: Unique identifier for this task/worktree instance.
        mcp_command: Executable to start MCP server (default: node).
        mcp_args: Arguments for MCP server script. Defaults to BRAINFROG_VERIFY_MCP_PATH.
        timeout_seconds: Maximum total duration for the entire gate before aborting.
        log_callback: Optional logger function to report progress.

    Returns:
        QualityGateResult containing status (PASS/FAIL), logs, errors, and screenshot.
    """
    start_time = time.time()

    def log(msg: str) -> None:
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass

    cwd_path = Path(project_cwd).resolve()
    actual_instance_id = instance_id or f"bf-gate-{os.getpid()}-{int(start_time)}"
    args = mcp_args or [DEFAULT_MCP_SERVER_PATH]

    # Pre-flight check: verify server script exists
    script_target = Path(args[0]) if args else None
    if script_target and not script_target.exists():
        return QualityGateResult(
            status="FAIL",
            reason=f"MCP server script not found at {script_target}. Check BRAINFROG_VERIFY_MCP_PATH.",
        )

    # Pre-flight check: inspect package.json scripts
    pkg_json = cwd_path / "package.json"
    scripts: Dict[str, Any] = {}
    has_build_script = False
    has_dev_script = False
    if pkg_json.exists():
        try:
            pkg_data = json.loads(pkg_json.read_text(encoding="utf-8"))
            scripts = pkg_data.get("scripts", {})
            has_build_script = "build" in scripts
            has_dev_script = "dev" in scripts or "start" in scripts
        except Exception:
            pass

    dev_script_name = "start" if (has_build_script and "start" in scripts) else ("dev" if "dev" in scripts else "start")

    log(f"[mcp-verify] 🚀 Starting verification gate for '{cwd_path.name}' (instance: {actual_instance_id})")
    log(f"[mcp-verify] Connecting to MCP server at {args[0]}...")

    client = McpClient(command=mcp_command, args=args)
    try:
        client.connect(timeout=15.0)
        log("[mcp-verify] Connected to MCP server successfully.")
    except Exception as e:
        return QualityGateResult(
            status="FAIL",
            reason=f"Failed to connect to MCP verification server: {e}",
            duration_seconds=time.time() - start_time,
        )

    # Everything after connection must be wrapped in try/finally to guarantee cleanup!
    try:
        # -------------------------------------------------------------
        # Step 1: Run build (npm run build) if project has build script
        # -------------------------------------------------------------
        build_output = ""
        if has_build_script:
            log("[mcp-verify] Running 'npm run build' to verify compilation...")
            client.call_tool(
                "start_process",
                {
                    "instanceId": actual_instance_id,
                    "command": "npm",
                    "args": ["run", "build"],
                    "cwd": str(cwd_path),
                },
                timeout=15.0,
            )

            # Poll read_process_output until exitCode is set or timeout reached
            build_deadline = time.time() + 120.0
            build_exit_code = None

            while True:
                remaining = build_deadline - time.time()
                if remaining <= 0:
                    break

                res = client.call_tool(
                    "read_process_output",
                    {"instanceId": actual_instance_id, "tailLines": 100},
                    timeout=min(remaining, 10.0),
                )
                data = res.parsed_json or {}
                build_output = data.get("output", "")
                build_exit_code = data.get("exitCode")

                if build_exit_code is not None:
                    break

                remaining = build_deadline - time.time()
                if remaining <= 0:
                    break
                time.sleep(min(1.0, remaining))

            if build_exit_code is None:
                log("[mcp-verify] ❌ Build timed out after 120 seconds.")
                client.call_tool("stop_process", {"instanceId": actual_instance_id})
                return QualityGateResult(
                    status="FAIL",
                    build_output=build_output,
                    reason="Build process timed out after 120s.",
                    duration_seconds=time.time() - start_time,
                )

            if build_exit_code != 0:
                log(f"[mcp-verify] ❌ Build failed with exit code {build_exit_code}!")
                return QualityGateResult(
                    status="FAIL",
                    build_output=build_output,
                    reason=f"npm run build failed with exit code {build_exit_code}",
                    duration_seconds=time.time() - start_time,
                )

            log("[mcp-verify] ✅ Build passed with exit code 0.")

        # -------------------------------------------------------------
        # Step 2: Start dev server if project has dev script
        # -------------------------------------------------------------
        resolved_nav_url = dev_url
        if has_dev_script:
            log(f"[mcp-verify] Launching dev server ('npm run {dev_script_name}') and waiting for {dev_url}...")
            client.call_tool(
                "start_process",
                {
                    "instanceId": actual_instance_id,
                    "command": "npm",
                    "args": ["run", dev_script_name],
                    "cwd": str(cwd_path),
                },
                timeout=15.0,
            )

            # Poll navigate with retry until server answers
            nav_deadline = time.time() + 30.0
            nav_ok = False
            last_nav_err = ""

            while True:
                remaining = nav_deadline - time.time()
                if remaining <= 0:
                    break

                # Check if dev server crashed immediately
                proc_stat = client.call_tool(
                    "read_process_output",
                    {"instanceId": actual_instance_id, "tailLines": 40},
                    timeout=min(remaining, 5.0),
                )
                stat_data = proc_stat.parsed_json or {}
                dev_out = stat_data.get("output", "")
                if stat_data.get("exitCode") is not None and stat_data.get("exitCode") != 0:
                    log(f"[mcp-verify] ❌ Dev server crashed with code {stat_data.get('exitCode')}.")
                    return QualityGateResult(
                        status="FAIL",
                        build_output=dev_out,
                        reason="Dev server crashed prematurely.",
                        duration_seconds=time.time() - start_time,
                    )

                # Check if dev server printed a specific local URL (e.g. Vite on 5173, Next on 3000, 0.0.0.0:8080)
                if dev_url == DEFAULT_DEV_URL and dev_out:
                    import re
                    m = re.search(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]):\d+", dev_out)
                    if m:
                        detected_url = m.group(0)
                        # Windows browser navigation to 0.0.0.0 is unreliable; map to localhost
                        resolved_nav_url = detected_url.replace("0.0.0.0", "localhost")

                # Attempt navigation with timeout clamped to remaining time (max 10s per attempt)
                remaining = nav_deadline - time.time()
                if remaining <= 0:
                    break
                nav_timeout = min(remaining, 10.0)
                nav_res = client.call_tool(
                    "navigate",
                    {"instanceId": actual_instance_id, "url": resolved_nav_url},
                    timeout=nav_timeout,
                )
                if not nav_res.is_error:
                    nav_ok = True
                    break

                last_nav_err = nav_res.text or nav_res.error_message or "Connection refused"

                # Sleep only if there is still time remaining
                remaining = nav_deadline - time.time()
                if remaining <= 1.0:
                    break
                time.sleep(min(1.5, remaining - 0.5))

            if not nav_ok:
                log(f"[mcp-verify] ❌ Failed to connect to dev server at {resolved_nav_url}: {last_nav_err}")
                return QualityGateResult(
                    status="FAIL",
                    build_output=build_output,
                    reason=f"Dev server at {resolved_nav_url} did not become ready within 30s ({last_nav_err})",
                    duration_seconds=time.time() - start_time,
                )

            log(f"[mcp-verify] ✅ Successfully navigated to {resolved_nav_url}")

        else:
            # If static HTML project without dev script, navigate directly to entrypoint or dev_url
            html_target = None
            if dev_url == DEFAULT_DEV_URL:
                for candidate in ["index.html", "public/index.html", "src/index.html"]:
                    cand_path = cwd_path / candidate
                    if cand_path.exists():
                        html_target = cand_path
                        resolved_nav_url = cand_path.resolve().as_uri()
                        break

            # If no dev script, no html entrypoint, and dev_url is default, skip browser navigation
            if not html_target and dev_url == DEFAULT_DEV_URL:
                log("[mcp-verify] ℹ️ No dev server script or HTML entrypoint found. Build verification passed.")
                return QualityGateResult(
                    status="PASS",
                    build_output=build_output,
                    reason="Build compilation succeeded cleanly; no web browser entrypoint found to navigate.",
                    duration_seconds=time.time() - start_time,
                )

            log(f"[mcp-verify] Navigating to {resolved_nav_url}...")
            nav_res = client.call_tool(
                "navigate",
                {"instanceId": actual_instance_id, "url": resolved_nav_url},
                timeout=20.0,
            )
            if nav_res.is_error:
                return QualityGateResult(
                    status="FAIL",
                    build_output=build_output,
                    reason=f"Navigation failed: {nav_res.text or nav_res.error_message}",
                    duration_seconds=time.time() - start_time,
                )

        # -------------------------------------------------------------
        # Step 3: Inspect console logs & network logs
        # -------------------------------------------------------------
        # Allow brief moment for runtime hydration/initial fetches
        time.sleep(1.0)

        log("[mcp-verify] Inspecting console error logs...")
        cons_res = client.call_tool(
            "get_console_logs",
            {"instanceId": actual_instance_id, "onlyErrors": True},
            timeout=10.0,
        )
        raw_cons = cons_res.parsed_json
        console_errors: List[Any] = raw_cons if isinstance(raw_cons, list) else []

        log("[mcp-verify] Inspecting network logs for failed requests...")
        net_res = client.call_tool(
            "get_network_logs",
            {"instanceId": actual_instance_id, "onlyFailed": True},
            timeout=10.0,
        )
        raw_net = net_res.parsed_json
        failed_requests: List[Any] = raw_net if isinstance(raw_net, list) else []

        # -------------------------------------------------------------
        # Step 4: Capture desktop and optionally mobile screenshots
        # -------------------------------------------------------------
        log("[mcp-verify] Capturing full-page viewport screenshot...")
        shot_res = client.call_tool(
            "screenshot",
            {"instanceId": actual_instance_id, "fullPage": True},
            timeout=15.0,
        )
        screenshot_desktop_base64 = shot_res.image_base64
        screenshot_base64 = screenshot_desktop_base64

        screenshot_mobile_base64 = None
        if capture_mobile:
            log("[mcp-verify] Capturing mobile viewport screenshot (390x844)...")
            try:
                shot_mob = client.call_tool(
                    "screenshot",
                    {"instanceId": actual_instance_id, "fullPage": True, "width": 390, "height": 844},
                    timeout=15.0,
                )
                if shot_mob and not shot_mob.is_error:
                    screenshot_mobile_base64 = shot_mob.image_base64
            except Exception as e:
                log(f"[mcp-verify] ℹ️ Mobile viewport capture notice: {e}")

        # -------------------------------------------------------------
        # Step 5: Determine overall gate verdict
        # -------------------------------------------------------------
        has_console_errors = len(console_errors) > 0
        has_failed_requests = len(failed_requests) > 0

        if has_console_errors or has_failed_requests:
            issues = []
            if has_console_errors:
                issues.append(f"{len(console_errors)} console error(s)")
            if has_failed_requests:
                issues.append(f"{len(failed_requests)} failed network request(s)")
            issue_summary = " and ".join(issues)

            log(f"[mcp-verify] ❌ Quality gate FAILED: {issue_summary}")
            return QualityGateResult(
                status="FAIL",
                build_output=build_output,
                console_errors=console_errors,
                failed_requests=failed_requests,
                screenshot_base64=screenshot_base64,
                screenshot_desktop_base64=screenshot_desktop_base64,
                screenshot_mobile_base64=screenshot_mobile_base64,
                reason=f"Runtime inspection detected {issue_summary}.",
                duration_seconds=time.time() - start_time,
            )

        log("[mcp-verify] 🎉 Quality gate PASSED cleanly! (0 console errors, 0 network failures)")
        return QualityGateResult(
            status="PASS",
            build_output=build_output,
            console_errors=[],
            failed_requests=[],
            screenshot_base64=screenshot_base64,
            screenshot_desktop_base64=screenshot_desktop_base64,
            screenshot_mobile_base64=screenshot_mobile_base64,
            reason="Build passed cleanly, dev server running, 0 console errors, 0 network failures.",
            duration_seconds=time.time() - start_time,
        )

    finally:
        # GUARANTEED CLEANUP: Stop process and close browser instance
        log("[mcp-verify] Cleaning up background processes and browser instance...")
        try:
            client.call_tool("stop_process", {"instanceId": actual_instance_id}, timeout=5.0)
        except Exception:
            pass
        try:
            client.call_tool("close_instance", {"instanceId": actual_instance_id}, timeout=5.0)
        except Exception:
            pass
        try:
            client.disconnect()
        except Exception:
            pass
        log("[mcp-verify] Cleanup complete.")
