# -*- coding: utf-8 -*-
"""Trusted Playwright Browser Verification Runner for BrainFrog Phase 1D.

Runs INSIDE the isolated browser container (mounted read-only at /bf-harness). It is
BrainFrog code, independent of the application under test: it never imports or executes
repository modules, and the plan it receives is BrainFrog's validated, canonical JSON.

Protocol:
- A per-run nonce arrives on stdin. Every record written to the private record channel
  (a dup of the original stdout; fd 1 is redirected to stderr) carries that nonce.
- Records: 'started', one 'artifact' per evidence file (base64 + sha256), 'complete'.
- Evidence files are first written to the dedicated tmpfs evidence directory.

Hardened Invariants:
1. Declarative execution only: no plan-supplied JavaScript, Python, or shell.
2. Layered network boundary: Chromium resolves only the application host
   (--host-resolver-rules), and context-level request interception allows only the exact
   approved origin. Docker's internal network is the OS-level boundary.
3. Assertions drive the outcome; actions never count as assertions.
4. Assertion failures are FAIL; browser/app/infrastructure failures are BLOCKED.
5. Deterministic rendering: fixed viewport/scale/locale/timezone, reduced motion,
   animations disabled for screenshots. Password inputs are masked in screenshots.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

RECORD_PREFIX = "@@BF_BROWSER@@ "
MAX_CONSOLE_ENTRIES = 200
MAX_NETWORK_ENTRIES = 500
MAX_ENTRY_CHARS = 500
MAX_OBSERVED_TEXT = 500
MAX_SCREENSHOT_BYTES = 8 * 1024 * 1024
POLL_INTERVAL_MS = 100
READINESS_BUDGET_S = 15.0
SCREENSHOT_TIMEOUT_MS = 10000

ASSERTIONS = {
    "expect_visible", "expect_hidden", "expect_text", "expect_count",
    "expect_url_path", "expect_no_horizontal_overflow",
}
ACTIONS = {"goto_relative", "fill", "click", "select_option", "check_checkbox"}

_record_fd: Optional[int] = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit(event: str, nonce: str, **payload: Any) -> None:
    record = {"event": event, "nonce": nonce}
    record.update(payload)
    line = ("\n" + RECORD_PREFIX + json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    fd = _record_fd if _record_fd is not None else 1
    view = memoryview(line)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _clip(text: Any, limit: int = MAX_ENTRY_CHARS) -> str:
    s = str(text)
    return s if len(s) <= limit else s[:limit] + "...[truncated]"


class _Boundary:
    """Exact-origin allowlist for the single approved application origin."""

    def __init__(self, origin: str) -> None:
        parts = urlsplit(origin)
        self.scheme = parts.scheme
        self.host = (parts.hostname or "").lower()
        self.port = parts.port
        self.origin = origin.rstrip("/")

    def allows(self, url: str) -> bool:
        if url.startswith(("data:", "blob:")) or url == "about:blank":
            return True
        try:
            p = urlsplit(url)
            port = p.port
        except ValueError:
            return False
        return p.scheme == self.scheme and (p.hostname or "").lower() == self.host and port == self.port

    def url_for(self, path: str) -> str:
        return self.origin + path


def _classify_error(exc: BaseException, timeout_cls: type) -> str:
    if isinstance(exc, timeout_cls):
        return "timeout"
    msg = str(exc)
    if "strict mode violation" in msg:
        return "ambiguous_selector"
    if "ERR_BLOCKED_BY_CLIENT" in msg:
        return "unapproved_navigation"
    if ("has been closed" in msg or "Target closed" in msg or "crash" in msg.lower()
            or "Browser closed" in msg or "Connection closed" in msg):
        return "browser_crashed"
    if "net::ERR" in msg:
        return "app_unreachable"
    return "browser_error"


def _proc_status(pid: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    with open(f"/proc/{pid}/status", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            key, _, val = line.partition(":")
            out[key] = val.strip()
    return out


def renderer_sandbox_state() -> Dict[str, Any]:
    """Prove from /proc that every Chromium renderer runs inside Chromium's layer-1 sandbox.

    A renderer counts as sandboxed only if, relative to this (browser-side) process, it is in a
    different user, PID and network namespace, is chrooted away from '/', has no effective
    capabilities in the container's namespace, and carries an additional seccomp filter.
    """
    own = {n: os.readlink(f"/proc/self/ns/{n}") for n in ("user", "pid", "net")}
    own_filters = int(_proc_status("self").get("Seccomp_filters", "0") or 0)
    renderers: List[Dict[str, Any]] = []
    for pid in sorted((p for p in os.listdir("/proc") if p.isdigit()), key=int):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ").decode("utf-8", errors="replace")
            if "--type=renderer" not in cmd:
                continue
            st = _proc_status(pid)
            ns = {n: os.readlink(f"/proc/{pid}/ns/{n}") for n in ("user", "pid", "net")}
            root = os.readlink(f"/proc/{pid}/root")
        except OSError:
            continue  # process exited while scanning
        filters = int(st.get("Seccomp_filters", "0") or 0)
        checks = {
            "own_user_namespace": ns["user"] != own["user"],
            "own_pid_namespace": ns["pid"] != own["pid"],
            "own_network_namespace": ns["net"] != own["net"],
            "chrooted": root != "/",
            "extra_seccomp_filter": filters > own_filters,
            "no_new_privs": st.get("NoNewPrivs") == "1",
        }
        renderers.append({"pid": int(pid), "seccomp_filters": filters, "root": root, "checks": checks})
    verified = bool(renderers) and all(all(r["checks"].values()) for r in renderers)
    reason = None if verified else ("no renderer process found" if not renderers else
                                    "renderer missing sandbox properties: " + json.dumps(
                                        [{k: v for k, v in r["checks"].items() if not v} for r in renderers]))
    return {"verified": verified, "reason": reason, "browser_side_seccomp_filters": own_filters,
            "renderers": renderers}


def _wait_for_app(boundary: _Boundary, readiness_path: str) -> Optional[str]:
    """Return None when the app answers over the private network, else a reason."""
    deadline = time.monotonic() + READINESS_BUDGET_S
    last = "no attempt"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(boundary.url_for(readiness_path), timeout=2) as resp:
                if resp.status < 500:
                    return None
                last = f"HTTP {resp.status}"
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                return None
            last = f"HTTP {exc.code}"
        except Exception as exc:  # connection refused, DNS failure, timeout
            last = f"{type(exc).__name__}: {_clip(exc, 200)}"
        time.sleep(0.25)
    return f"application not reachable at approved origin within {READINESS_BUDGET_S:.0f}s ({last})"


def run(args: Dict[str, str], nonce: str) -> Tuple[str, Dict[str, Any], Dict[str, bytes]]:
    """Execute the plan. Returns (status, assertions_manifest, artifacts)."""
    with open(args["--plan"], "rb") as fh:
        plan_bytes = fh.read()
    plan = json.loads(plan_bytes.decode("utf-8"))
    plan_sha = hashlib.sha256(plan_bytes).hexdigest()
    criterion_id = args["--criterion-id"]
    boundary = _Boundary(args["--origin"])
    vp = plan.get("viewport") or {}
    viewport = {"width": int(vp.get("width", 1280)), "height": int(vp.get("height", 720))}
    scale = float(vp.get("device_scale_factor", 1.0))
    steps: List[Dict[str, Any]] = list(plan.get("steps") or [])

    console: List[str] = []
    errors: List[str] = []
    network: List[Dict[str, Any]] = []
    blocked: List[Dict[str, Any]] = []
    unapproved_seen: List[Dict[str, Any]] = []
    state: Dict[str, Any] = {"crashed": False, "popups": 0}
    records: List[Dict[str, Any]] = []
    runtime: Dict[str, Any] = {
        "browser_name": "chromium",
        "browser_version": None,
        "playwright_version": None,
        "chromium_internal_sandbox": True,
        "renderer_sandbox": None,
        "container_uid": os.getuid() if hasattr(os, "getuid") else None,
        "viewport": viewport,
        "device_scale_factor": scale,
        "locale": "en-US",
        "timezone_id": "UTC",
        "reduced_motion": "reduce",
        "service_workers": "block",
    }
    screenshot: Dict[str, Any] = {"captured": False, "relative_path": None, "sha256": None,
                                  "assertion_id": None, "captured_at": None, "omitted_reason": None}
    artifacts: Dict[str, bytes] = {}
    fatal: Dict[str, Optional[str]] = {"status": None, "reason": None, "kind": None}

    def block(kind: str, reason: str) -> None:
        if fatal["status"] is None:
            fatal.update(status="BLOCKED", reason=reason, kind=kind)

    def finish() -> Tuple[str, Dict[str, Any], Dict[str, bytes]]:
        executed_assertions = [r for r in records if r["kind"] == "assertion"]
        passed = sum(1 for r in executed_assertions if r["status"] == "PASS")
        failed = sum(1 for r in executed_assertions if r["status"] == "FAIL")
        if fatal["status"] == "BLOCKED" or any(r["status"] == "BLOCKED" for r in records):
            status = "BLOCKED"
        elif any(r["status"] == "FAIL" for r in records):
            status = "FAIL"
        elif len(records) == len(steps) and executed_assertions and passed == len(executed_assertions):
            status = "PASS" if screenshot["captured"] else "BLOCKED"
            if status == "BLOCKED":
                fatal.update(reason=f"Required screenshot evidence missing: {screenshot['omitted_reason']}",
                             kind="screenshot_failed")
        else:
            status = "BLOCKED"
            fatal.update(reason=fatal["reason"] or "Zero assertions executed or plan incomplete; cannot PASS.",
                         kind=fatal["kind"] or "no_assertions_executed")
        failure_reason = fatal["reason"]
        if status == "FAIL" and failure_reason is None:
            failure_reason = next(r["failure_reason"] for r in records if r["status"] == "FAIL")
        manifest = {
            "schema": "brainfrog.browser-assertions/1",
            "criterion_id": criterion_id,
            "status": status,
            "failure_reason": failure_reason,
            "failure_kind": fatal["kind"] if status == "BLOCKED" else (
                next((r.get("failure_kind") for r in records if r["status"] == "FAIL"), None)),
            "plan_sha256": plan_sha,
            "source_snapshot_digest": args.get("--source-digest"),
            "browser_image_identity": args.get("--browser-image"),
            "application_origin_reference": boundary.origin,
            "runtime": runtime,
            "summary": {
                "total_steps": len(steps),
                "steps_executed": len(records),
                "assertions_planned": sum(1 for s in steps if s.get("action") in ASSERTIONS),
                "assertions_executed": len(executed_assertions),
                "assertions_passed": passed,
                "assertions_failed": failed,
                "blocked_requests": len(blocked),
                "unapproved_requests_observed": len(unapproved_seen),
                "popups": state["popups"],
            },
            "screenshot": screenshot,
            "blocked_requests": blocked[:50],
            "records": records,
        }
        artifacts["browser-assertions.json"] = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
        artifacts["browser-console.log"] = "\n".join(console).encode("utf-8")
        artifacts["browser-errors.log"] = "\n".join(errors).encode("utf-8")
        artifacts["browser-network.log"] = "\n".join(json.dumps(e, sort_keys=True) for e in network).encode("utf-8")
        return status, manifest, artifacts

    # -- 1. Application reachability over the private network -------------------------
    unreachable = _wait_for_app(boundary, args.get("--readiness", "/"))
    if unreachable:
        block("app_unreachable", unreachable)
        screenshot["omitted_reason"] = "no page: application unreachable"
        return finish()

    # -- 2. Browser runtime --------------------------------------------------------------
    try:
        from importlib.metadata import version as _pkg_version
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
        runtime["playwright_version"] = _pkg_version("playwright")
    except Exception as exc:
        block("browser_unavailable", f"Playwright runtime unavailable in browser image: {_clip(exc, 200)}")
        screenshot["omitted_reason"] = "no page: browser runtime unavailable"
        return finish()

    with sync_playwright() as pw:
        launch_args = [
            "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
            "--disable-component-update", "--disable-default-apps", "--disable-sync",
            "--disable-extensions", "--disable-gpu", "--mute-audio",
            # Chromium itself resolves only the application host.
            f"--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE {boundary.host}",
        ]
        runtime["launch_args"] = launch_args
        try:
            # Chromium's own sandbox stays ON; the container's seccomp profile permits its namespaces.
            browser = pw.chromium.launch(headless=True, chromium_sandbox=True, args=launch_args, timeout=30000)
            runtime["browser_version"] = browser.version
        except Exception as exc:
            block("browser_launch_failed", f"Chromium launch failure: {_clip(exc, 300)}")
            screenshot["omitted_reason"] = "no page: browser did not launch"
            return finish()

        context = browser.new_context(
            viewport=viewport,
            device_scale_factor=scale,
            locale="en-US",
            timezone_id="UTC",
            reduced_motion="reduce",
            color_scheme="light",
            service_workers="block",
            accept_downloads=False,
            java_script_enabled=True,
        )

        def on_route(route: Any) -> None:
            req = route.request
            if boundary.allows(req.url):
                route.continue_()
                return
            try:
                is_nav = req.is_navigation_request()
            except Exception:
                is_nav = False
            blocked.append({"url": _clip(req.url, 300), "resource_type": req.resource_type,
                            "navigation": is_nav, "at": _now()})
            route.abort("blockedbyclient")

        def on_request(req: Any) -> None:
            allowed = boundary.allows(req.url)
            entry = {"url": _clip(req.url, 300), "method": req.method, "resource_type": req.resource_type,
                     "approved_origin": allowed, "redirected": req.redirected_from is not None}
            if len(network) < MAX_NETWORK_ENTRIES:
                network.append(entry)
            if not allowed:
                unapproved_seen.append(entry)

        def on_page(p: Any) -> None:
            state["popups"] += 1

        context.route("**/*", on_route)
        context.on("request", on_request)

        page = context.new_page()
        context.on("page", on_page)
        page.on("console", lambda m: console.append(_clip(f"[{_now()}] [{m.type}] {m.text}"))
                if len(console) < MAX_CONSOLE_ENTRIES else None)
        page.on("pageerror", lambda e: errors.append(_clip(f"[{_now()}] [pageerror] {e}"))
                if len(errors) < MAX_CONSOLE_ENTRIES else None)

        def on_crash(_p: Any) -> None:
            state["crashed"] = True
            errors.append(f"[{_now()}] [crash] renderer process crashed")

        page.on("crash", on_crash)
        page.on("websocket", lambda ws: unapproved_seen.append({"url": _clip(ws.url, 300), "websocket": True})
                if not boundary.allows(ws.url.replace("ws://", "http://", 1)) else None)

        page_loaded = {"value": False}

        def check_containment() -> bool:
            """Unapproved navigation, origin escape, or a crash blocks the criterion."""
            if state["crashed"]:
                block("browser_crashed", "Browser renderer crashed during verification.")
                return False
            nav_attempts = [b for b in blocked if b["navigation"]]
            if nav_attempts:
                block("unapproved_navigation",
                      f"Unapproved navigation attempted and blocked: {nav_attempts[0]['url']}")
                return False
            if page_loaded["value"] and not boundary.allows(page.url):
                block("unapproved_navigation", f"Page left the approved application origin: {_clip(page.url, 200)}")
                return False
            return True

        # -- 3. Initial navigation (an action, never an assertion) ----------------------
        t_nav = time.perf_counter()
        nav_rec: Dict[str, Any] = {"criterion_id": criterion_id, "step_index": 0, "kind": "action",
                                   "action": "initial_navigation", "path": plan.get("path", "/")}
        try:
            resp = page.goto(boundary.url_for(plan.get("path", "/")), wait_until="load", timeout=15000)
            page_loaded["value"] = True
            nav_rec["observed"] = {"http_status": resp.status if resp else None,
                                   "path": urlsplit(page.url).path}
            nav_rec["status"] = "PASS"
        except Exception as exc:
            kind = _classify_error(exc, PlaywrightTimeoutError)
            kind = "app_unreachable" if kind in ("timeout", "browser_error") else kind
            nav_rec.update(status="BLOCKED", failure_kind=kind,
                           failure_reason=f"Initial navigation failed: {_clip(exc, 300)}")
            block(kind, nav_rec["failure_reason"])
        nav_rec["duration_ms"] = round((time.perf_counter() - t_nav) * 1000.0, 2)
        runtime["initial_navigation"] = nav_rec
        if fatal["status"] is None:
            check_containment()
        if fatal["status"] is None:
            runtime["renderer_sandbox"] = renderer_sandbox_state()
            if not runtime["renderer_sandbox"]["verified"]:
                block("renderer_sandbox_unverified",
                      f"Chromium renderer sandbox could not be verified: {runtime['renderer_sandbox']['reason']}")

        # -- 4. Declarative steps ------------------------------------------------------
        assertion_seq = 0
        last_assertion_id: Optional[str] = None
        if fatal["status"] is None:
            for idx, step in enumerate(steps, start=1):
                action = step.get("action")
                timeout_ms = int(step.get("timeout_ms", 5000))
                selector = step.get("selector")
                kind = "assertion" if action in ASSERTIONS else "action"
                rec: Dict[str, Any] = {"criterion_id": criterion_id, "step_index": idx, "kind": kind,
                                       "action": action, "status": "PASS"}
                if step.get("id"):
                    rec["step_id"] = step["id"]
                if kind == "assertion":
                    assertion_seq += 1
                    rec["assertion_id"] = step.get("id") or f"A{assertion_seq}"
                    last_assertion_id = rec["assertion_id"]
                if selector:
                    rec["selector"] = selector
                t0 = time.perf_counter()
                try:
                    if action not in ASSERTIONS | ACTIONS:
                        raise ValueError(f"unsupported action {action!r}")
                    loc = page.locator("css=" + selector) if selector else None
                    if kind == "assertion":
                        ok, expected, observed, fkind, reason = _assert(page, context, loc, step, boundary,
                                                                         timeout_ms, PlaywrightError)
                        rec["expected"] = expected
                        rec["observed"] = observed
                        if not ok:
                            rec["status"] = "BLOCKED" if fkind == "ambiguous_selector" else "FAIL"
                            rec["failure_kind"] = fkind
                            rec["failure_reason"] = reason
                    else:
                        rec["observed"] = _act(page, loc, step, boundary, timeout_ms)
                except PlaywrightTimeoutError as exc:
                    # The page is up but the element needed for this interaction never became
                    # actionable: a functional failure of the application, not infrastructure.
                    if action == "goto_relative":
                        rec.update(status="BLOCKED", failure_kind="navigation_timeout",
                                   failure_reason=f"Navigation timed out: {_clip(exc, 300)}")
                    else:
                        rec.update(status="FAIL", failure_kind="action_target_unavailable",
                                   failure_reason=f"'{action}' could not be performed on '{selector}' "
                                                  f"within {timeout_ms}ms: element missing or not actionable")
                except Exception as exc:
                    fk = _classify_error(exc, PlaywrightTimeoutError)
                    rec.update(status="BLOCKED", failure_kind=fk, failure_reason=f"{action}: {_clip(exc, 300)}")
                rec["duration_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
                records.append(rec)
                if rec["status"] == "BLOCKED":
                    block(rec["failure_kind"], rec["failure_reason"])
                if not check_containment() or rec["status"] != "PASS":
                    break

        # -- 5. Screenshot evidence (only when a real page exists and the browser is up) --
        if not page_loaded["value"]:
            screenshot["omitted_reason"] = "no page: initial navigation did not complete"
        elif state["crashed"] or not browser.is_connected():
            screenshot["omitted_reason"] = "browser not operational"
        else:
            # Layout assertions get a full-page capture so horizontal overflow is visible in
            # the evidence; the verdict itself comes from the CDP measurement, not the image.
            full_page = any(s.get("action") == "expect_no_horizontal_overflow" for s in steps)
            screenshot["full_page"] = full_page
            try:
                data = page.screenshot(full_page=full_page, animations="disabled", caret="hide",
                                       mask=[page.locator("css=input[type=password]")],
                                       timeout=SCREENSHOT_TIMEOUT_MS)
                if len(data) > MAX_SCREENSHOT_BYTES:
                    screenshot["omitted_reason"] = f"screenshot exceeds {MAX_SCREENSHOT_BYTES} bytes"
                else:
                    artifacts["screenshot.png"] = data
                    screenshot.update(captured=True, relative_path="screenshot.png",
                                      sha256=hashlib.sha256(data).hexdigest(),
                                      assertion_id=last_assertion_id, captured_at=_now(),
                                      page_path=urlsplit(page.url).path if boundary.allows(page.url) else None)
            except Exception as exc:
                screenshot["omitted_reason"] = f"screenshot capture failed: {_clip(exc, 200)}"
        # Escape attempts that fire after the last step (e.g. timers) still block the criterion.
        if page_loaded["value"] and not state["crashed"]:
            check_containment()
            final_state = renderer_sandbox_state()
            runtime["renderer_sandbox_at_end"] = final_state
            if not final_state["verified"]:
                block("renderer_sandbox_unverified",
                      f"Chromium renderer sandbox not verified at end of run: {final_state['reason']}")
        try:
            context.close()
            browser.close()
        except Exception:
            pass
    return finish()


def _poll(page: Any, timeout_ms: int, probe: Callable[[], Tuple[bool, Dict[str, Any]]]) -> Tuple[bool, Dict[str, Any]]:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while True:
        ok, observed = probe()
        if ok or time.monotonic() >= deadline:
            return ok, observed
        page.wait_for_timeout(POLL_INTERVAL_MS)


def _norm_ws(s: str) -> str:
    return " ".join(s.split())


def _assert(page: Any, context: Any, loc: Any, step: Dict[str, Any], boundary: _Boundary, timeout_ms: int,
            pw_error: type) -> Tuple[bool, Dict[str, Any], Dict[str, Any], Optional[str], Optional[str]]:
    """Evaluate one assertion. Returns (ok, expected, observed, failure_kind, reason)."""
    action = step["action"]
    sel = step.get("selector")

    if action == "expect_visible":
        def probe():
            n = loc.count()
            vis = n == 1 and loc.first.is_visible()
            return vis, {"match_count": n, "visible": vis}
        ok, obs = _poll(page, timeout_ms, probe)
        if ok:
            return True, {"visible": True}, obs, None, None
        if obs["match_count"] > 1:
            return False, {"visible": True}, obs, "ambiguous_selector", f"Selector '{sel}' matched {obs['match_count']} elements; expected exactly one"
        return False, {"visible": True}, obs, "assertion_failed", f"Element '{sel}' was not visible within {timeout_ms}ms"

    if action == "expect_hidden":
        def probe():
            n = loc.count()
            visible = sum(1 for i in range(min(n, 20)) if loc.nth(i).is_visible())
            return visible == 0, {"match_count": n, "visible_count": visible}
        ok, obs = _poll(page, timeout_ms, probe)
        return (ok, {"hidden": True}, obs, None if ok else "assertion_failed",
                None if ok else f"Element '{sel}' was still visible after {timeout_ms}ms")

    if action == "expect_text":
        expected = {"contains": step["contains"]} if "contains" in step else {"equals": step["equals"]}

        def probe():
            n = loc.count()
            if n != 1:
                return False, {"match_count": n, "visible": False, "text": None}
            vis = loc.first.is_visible()
            text = loc.first.inner_text(timeout=1000) if vis else ""
            if "contains" in step:
                ok = vis and step["contains"] in text
            else:
                ok = vis and _norm_ws(text) == _norm_ws(step["equals"])
            return ok, {"match_count": n, "visible": vis, "text": _clip(text, MAX_OBSERVED_TEXT)}
        ok, obs = _poll(page, timeout_ms, probe)
        if ok:
            return True, expected, obs, None, None
        if obs["match_count"] > 1:
            return False, expected, obs, "ambiguous_selector", f"Selector '{sel}' matched {obs['match_count']} elements; expected exactly one"
        if obs["match_count"] == 0:
            reason = f"Element '{sel}' not found"
        elif not obs["visible"]:
            reason = f"Element '{sel}' exists but is not visible (hidden text does not satisfy expect_text)"
        else:
            reason = f"Element '{sel}' text did not match {expected}; observed {obs['text']!r}"
        return False, expected, obs, "assertion_failed", reason

    if action == "expect_count":
        def probe():
            n = loc.count()
            return n == step["count"], {"match_count": n}
        ok, obs = _poll(page, timeout_ms, probe)
        return (ok, {"count": step["count"]}, obs, None if ok else "assertion_failed",
                None if ok else f"Selector '{sel}' matched {obs['match_count']} elements, expected {step['count']}")

    if action == "expect_url_path":
        def probe():
            parts = urlsplit(page.url)
            approved = boundary.allows(page.url)
            return approved and parts.path == step["path"], {"path": parts.path, "approved_origin": approved}
        ok, obs = _poll(page, timeout_ms, probe)
        return (ok, {"path": step["path"]}, obs, None if ok else "assertion_failed",
                None if ok else f"Current path '{obs['path']}' does not match expected '{step['path']}'")

    if action == "expect_no_horizontal_overflow":
        # Measured by the browser engine through CDP, not by page JavaScript, so page scripts
        # cannot override the reported layout metrics.
        cdp = context.new_cdp_session(page)
        try:
            def probe():
                m = cdp.send("Page.getLayoutMetrics")
                content_w = float(m["cssContentSize"]["width"])
                client_w = float(m["cssLayoutViewport"]["clientWidth"])
                return content_w <= client_w + 0.5, {"content_width_css_px": content_w,
                                                     "viewport_client_width_css_px": client_w,
                                                     "horizontal_overflow": content_w > client_w + 0.5}
            ok, obs = _poll(page, timeout_ms, probe)
        finally:
            try:
                cdp.detach()
            except Exception:
                pass
        return (ok, {"horizontal_overflow": False}, obs, None if ok else "assertion_failed",
                None if ok else (f"Horizontal overflow: content width {obs['content_width_css_px']}px exceeds "
                                 f"viewport width {obs['viewport_client_width_css_px']}px"))

    raise ValueError(f"unsupported assertion {action!r}")


def _act(page: Any, loc: Any, step: Dict[str, Any], boundary: _Boundary, timeout_ms: int) -> Dict[str, Any]:
    action = step["action"]
    if action == "goto_relative":
        resp = page.goto(boundary.url_for(step["path"]), wait_until="load", timeout=timeout_ms)
        return {"http_status": resp.status if resp else None, "path": urlsplit(page.url).path}
    if action == "click":
        loc.click(timeout=timeout_ms)
        return {"clicked": True}
    if action == "fill":
        loc.fill(step["value"], timeout=timeout_ms)
        return {"filled": True, "value_length": len(step["value"])}  # input values are never recorded
    if action == "select_option":
        loc.select_option(value=step["value"], timeout=timeout_ms)
        return {"selected": True}
    if action == "check_checkbox":
        loc.check(timeout=timeout_ms)
        return {"checked": True}
    raise ValueError(f"unsupported action {action!r}")


def main(argv: List[str]) -> int:
    global _record_fd
    # Private record channel: only this process writes records; everything else that would
    # write to fd 1 (Playwright driver, libraries) goes to stderr instead.
    _record_fd = os.dup(1)
    os.dup2(2, 1)

    keys = {"--plan", "--origin", "--criterion-id", "--readiness", "--source-digest", "--browser-image", "--evidence-dir"}
    args: Dict[str, str] = {}
    it = iter(argv)
    for tok in it:
        if tok not in keys:
            sys.stderr.write(f"browser_runner: unknown argument {tok!r}\n")
            return 2
        val = next(it, None)
        if val is None:
            sys.stderr.write(f"browser_runner: missing value for {tok}\n")
            return 2
        args[tok] = val
    if not {"--plan", "--origin", "--criterion-id", "--evidence-dir"} <= set(args):
        sys.stderr.write("browser_runner: missing required arguments\n")
        return 2

    nonce = sys.stdin.readline().strip()
    if len(nonce) < 32:
        sys.stderr.write("browser_runner: nonce missing\n")
        return 2
    _emit("started", nonce, criterion_id=args["--criterion-id"], python=sys.version.split()[0], at=_now())

    try:
        status, manifest, artifacts = run(args, nonce)
    except Exception as exc:  # runner defect: never a PASS
        _emit("complete", nonce, criterion_id=args["--criterion-id"], status="BLOCKED",
              failure_kind="runner_error", failure_reason=f"browser runner error: {_clip(exc, 300)}", artifacts={})
        return 3

    evidence_dir = args["--evidence-dir"]
    hashes: Dict[str, Dict[str, Any]] = {}
    for name, data in artifacts.items():
        with open(os.path.join(evidence_dir, name), "wb") as fh:  # dedicated tmpfs evidence dir
            fh.write(data)
        digest = hashlib.sha256(data).hexdigest()
        hashes[name] = {"sha256": digest, "size": len(data)}
        _emit("artifact", nonce, name=name, sha256=digest, size=len(data),
              data_b64=base64.b64encode(data).decode("ascii"))
    _emit("complete", nonce, criterion_id=args["--criterion-id"], status=status,
          failure_kind=manifest.get("failure_kind"), failure_reason=manifest.get("failure_reason"),
          summary=manifest["summary"], artifacts=hashes)
    return {"PASS": 0, "FAIL": 1}.get(status, 2)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
