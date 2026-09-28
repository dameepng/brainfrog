"""visual_inspector.py — Visual Screenshot & Headless Inspection Engine for BrainFrog.

Gives BrainFrog CLI visual "eyes" to inspect rendered HTML/CSS interfaces,
detect UI slop (awkward padding, overlapping elements, unstyled links, card-ception),
and feed visual feedback directly to multimodal models (Gemini 3.8 Flash / Claude 3.5+).
"""
import base64
import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, List, Optional


def get_image_base64(image_path: Path | str) -> str:
    """Read and encode an image file as a base64 string."""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def find_browser_bin() -> Optional[str]:
    """Locate Google Chrome or Microsoft Edge executable on the user's system."""
    # 1. Environment variable override
    env_bin = os.environ.get("BRAINFROG_BROWSER_BIN")
    if env_bin and os.path.exists(env_bin):
        return env_bin

    # 2. Check standard Windows installations
    standard_paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
    ]
    for p in standard_paths:
        if os.path.exists(p):
            return p

    # 3. Check PATH
    for name in ("chrome", "chrome.exe", "google-chrome", "google-chrome-stable", "msedge", "msedge.exe", "chromium"):
        which = shutil.which(name)
        if which:
            return which

    return None


def find_html_entrypoint(repo_dir: Path) -> Optional[Path]:
    """Find the main HTML entrypoint in the target repository."""
    candidates = [
        repo_dir / "index.html",
        repo_dir / "public" / "index.html",
        repo_dir / "src" / "index.html",
        repo_dir / "app" / "index.html",
    ]
    for c in candidates:
        if c.exists() and c.is_file():
            return c

    # Fallback to any top-level html file
    try:
        html_files = sorted(repo_dir.glob("*.html"))
        if html_files:
            return html_files[0]
    except Exception:
        pass

    return None


def is_frontend_change(files: Iterable[str]) -> bool:
    """Check if any of the touched files affect frontend UI/UX."""
    frontend_exts = {".html", ".htm", ".css", ".scss", ".sass", ".less", ".jsx", ".tsx", ".vue", ".svelte"}
    for f in files:
        ext = Path(f).suffix.lower()
        if ext in frontend_exts:
            return True
        name = Path(f).name.lower()
        if "style" in name or "ui" in name or "view" in name or "component" in name:
            return True
    return False


def capture_screenshot(
    target: Path | str,
    output_png: Path,
    browser_bin: Optional[str] = None,
    window_size: str = "1280,820",
    timeout: int = 15,
) -> bool:
    """Capture a rendered viewport screenshot using headless Chrome/Edge.

    Args:
        target: File path or URL to render.
        output_png: Destination path for the captured screenshot PNG.
        browser_bin: Optional browser binary path. If omitted, auto-discovered.
        window_size: Viewport resolution (width,height).
        timeout: Subprocess timeout in seconds.

    Returns:
        True if the screenshot was created successfully, False otherwise.
    """
    bin_path = browser_bin or find_browser_bin()
    if not bin_path or not os.path.exists(bin_path):
        return False

    output_png = Path(output_png).resolve()
    output_png.parent.mkdir(parents=True, exist_ok=True)

    # Convert local path to file:// URL
    if isinstance(target, Path) or (isinstance(target, str) and not target.startswith(("http://", "https://", "file://"))):
        resolved_path = Path(target).resolve()
        target_url = f"file:///{resolved_path.as_posix()}"
    else:
        target_url = str(target)

    cmd = [
        bin_path,
        "--headless=new",
        f"--screenshot={str(output_png)}",
        f"--window-size={window_size}",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        "--virtual-time-budget=2000",  # Allow 2s for CSS transitions/animations to settle
        target_url,
    ]

    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return output_png.exists() and output_png.stat().st_size > 500
    except Exception:
        return False
