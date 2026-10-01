"""core/image_handler.py — Image attachment, clipboard capture, and vision processing engine.

Provides multimodal image attachment support for BrainFrog REPL:
- @path/to/image.png reference extraction and validation
- Clipboard image detection & capture (Pillow ImageGrab)
- Dev server / UI viewport screenshot capture via MCP or headless browser
- Content block generation for Anthropic Claude and Google Antigravity (Gemini)
"""
from __future__ import annotations

import base64
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
IMAGE_EXTENSIONS = SUPPORTED_IMAGE_EXTENSIONS

MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

# Default ceiling: 5 MB per image (matches standard Claude and Gemini inline image limits)
DEFAULT_MAX_IMAGE_SIZE_BYTES = int(
    float(os.environ.get("BRAINFROG_MAX_IMAGE_SIZE_MB", "5.0")) * 1024 * 1024
)
MAX_IMAGE_SIZE_BYTES = DEFAULT_MAX_IMAGE_SIZE_BYTES


@dataclass
class AttachedImage:
    """Represents an image attached to a REPL prompt for System 2 vision processing."""

    path: Path
    filename: str
    mime_type: str
    base64_data: str
    size_bytes: int
    source: str = "file"  # "file", "clipboard", "screenshot"

    @property
    def size_kb(self) -> float:
        return self.size_bytes / 1024.0

    @property
    def size_mb(self) -> float:
        return self.size_bytes / (1024.0 * 1024.0)

    def to_claude_content_block(self) -> Dict[str, Any]:
        """Format as an Anthropic Messages API image content block."""
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": self.mime_type,
                "data": self.base64_data,
            },
        }

    def to_gemini_part(self) -> Dict[str, Any]:
        """Format as a Gemini API inline data part."""
        return {
            "inline_data": {
                "mime_type": self.mime_type,
                "data": self.base64_data,
            }
        }

    def format_badge(self) -> str:
        """User-friendly badge string for REPL confirmation."""
        return f"[Image attached: {self.filename} ({self.size_kb:.1f} KB, source: {self.source})]"


def is_image_path(path: Path | str) -> bool:
    """Check if the given file path or string has a supported image extension."""
    suffix = Path(path).suffix.lower()
    return suffix in SUPPORTED_IMAGE_EXTENSIONS


def load_and_validate_image(
    path: Path | str,
    max_size_bytes: Optional[int] = None,
    source: str = "file",
) -> Tuple[Optional[AttachedImage], Optional[str]]:
    """Load an image file, validate its format and size, and encode it to base64.

    Args:
        path: Path to the image file.
        max_size_bytes: Maximum allowed size in bytes (defaults to 5 MB).
        source: Source description ('file', 'clipboard', 'screenshot').

    Returns:
        (AttachedImage, None) on success, or (None, error_message) on failure.
    """
    limit = max_size_bytes or DEFAULT_MAX_IMAGE_SIZE_BYTES
    target = Path(path).resolve()

    if not target.exists():
        return None, f"File gambar tidak ditemukan: {path}"

    if not target.is_file():
        return None, f"Path bukan merupakan file: {path}"

    suffix = target.suffix.lower()
    if suffix not in SUPPORTED_IMAGE_EXTENSIONS:
        supported_str = ", ".join(sorted(SUPPORTED_IMAGE_EXTENSIONS))
        return (
            None,
            f"Format gambar tidak didukung ({suffix or 'tanpa ekstensi'}). Format yang didukung: {supported_str}",
        )

    try:
        size = target.stat().st_size
    except Exception as exc:
        return None, f"Gagal membaca metadata file gambar: {exc}"

    if size > limit:
        size_mb = size / (1024.0 * 1024.0)
        max_mb = limit / (1024.0 * 1024.0)
        return (
            None,
            f"Ukuran gambar terlalu besar: {size_mb:.2f} MB (maksimum {max_mb:.1f} MB): {target.name}",
        )

    try:
        raw_bytes = target.read_bytes()
        b64_str = base64.b64encode(raw_bytes).decode("utf-8")
    except Exception as exc:
        return None, f"Gagal membaca atau meng-encode file gambar: {exc}"

    mime = MIME_TYPES.get(suffix, "image/png")
    attached = AttachedImage(
        path=target,
        filename=target.name,
        mime_type=mime,
        base64_data=b64_str,
        size_bytes=size,
        source=source,
    )
    return attached, None


def extract_image_references(
    task: str,
    repo_dir: Path,
    max_size_bytes: Optional[int] = None,
) -> Tuple[str, List[AttachedImage], List[str]]:
    """Extract and validate @path/to/image references from a task prompt.

    Removes valid image references from the prompt string so they are not treated
    as plain text references, and validates their file size and existence.

    Returns:
        (clean_task, attached_images, errors)
    """
    pattern = r"@([a-zA-Z0-9_\-\.\/\\]+)"
    matches = list(re.finditer(pattern, task))

    images: List[AttachedImage] = []
    errors: List[str] = []
    spans_to_remove: List[Tuple[int, int]] = []

    for match in matches:
        ref_path_str = match.group(1)
        suffix = Path(ref_path_str).suffix.lower()
        if suffix not in SUPPORTED_IMAGE_EXTENSIONS:
            # Leave normal code/text files untouched for pinned_files logic
            continue

        # Resolve path relative to repo_dir or absolute
        cand = Path(ref_path_str)
        if not cand.is_absolute():
            cand = (repo_dir / cand).resolve()

        attached, err = load_and_validate_image(
            cand, max_size_bytes=max_size_bytes, source="reference"
        )
        if attached:
            images.append(attached)
            spans_to_remove.append(match.span())
        else:
            if err:
                errors.append(err)

    # Clean removed image mentions from task prompt
    if spans_to_remove:
        # Reconstruct string by skipping removed spans
        clean_parts: List[str] = []
        last_idx = 0
        for start, end in sorted(spans_to_remove, key=lambda s: s[0]):
            clean_parts.append(task[last_idx:start])
            last_idx = end
        clean_parts.append(task[last_idx:])
        clean_task = re.sub(r"\s+", " ", "".join(clean_parts)).strip()
    else:
        clean_task = task.strip()

    return clean_task, images, errors


def get_clipboard_image(
    dest_dir: Path,
    max_size_bytes: Optional[int] = None,
) -> Tuple[Optional[AttachedImage], Optional[str]]:
    """Capture image data from the system clipboard.

    Supports:
    - Windows & macOS: native clipboard image capture via Pillow ImageGrab
    - Linux: X11 / Wayland via Pillow (with xclip or wl-paste available)
    - File copy: if user copied an image file from the file explorer

    Returns:
        (AttachedImage, None) on success, or (None, error_message) if no image or error.
    """
    try:
        from PIL import Image, ImageGrab
    except ImportError:
        return (
            None,
            "Library 'Pillow' belum terpasang. Jalankan: pip install pillow",
        )

    dest_dir = Path(dest_dir).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)

    try:
        data = ImageGrab.grabclipboard()
    except Exception as exc:
        platform_info = sys.platform
        return (
            None,
            f"Gagal membaca clipboard di platform '{platform_info}': {exc}. "
            "Pastikan sistem operasi mendukung akses clipboard (pada Linux, periksa xclip/wl-paste).",
        )

    # Case 1: Image data object directly on clipboard
    if isinstance(data, Image.Image):
        timestamp = int(time.time() * 1000)
        out_path = dest_dir / f"clipboard_{timestamp}.png"
        try:
            data.save(out_path, format="PNG")
        except Exception as exc:
            return None, f"Gagal menyimpan gambar dari clipboard: {exc}"

        return load_and_validate_image(
            out_path, max_size_bytes=max_size_bytes, source="clipboard"
        )

    # Case 2: List of file paths copied to clipboard (e.g. from File Explorer)
    if isinstance(data, (list, tuple)):
        for item in data:
            p = Path(item)
            if p.is_file() and is_image_path(p):
                return load_and_validate_image(
                    p, max_size_bytes=max_size_bytes, source="clipboard"
                )

    return None, "Clipboard tidak berisi data gambar atau file gambar."


def capture_screenshot_for_repl(
    target: Optional[str],
    repo_dir: Path,
    output_dir: Optional[Path] = None,
    browser_bin: Optional[str] = None,
) -> Tuple[Optional[AttachedImage], Optional[str]]:
    """Capture a rendered viewport screenshot and attach it for the REPL prompt.

    Tries:
    1. brainfrog-verify-mcp tool call if MCP server is active/configured
    2. Headless browser engine fallback (Chrome / Edge)

    Args:
        target: Optional URL or file path. If None, auto-discovers running dev server or HTML entrypoint.
        repo_dir: Current workspace repository root.
        output_dir: Directory where the screenshot will be saved.
        browser_bin: Optional explicit browser executable path.

    Returns:
        (AttachedImage, None) on success, or (None, error_message) on failure.
    """
    repo_path = Path(repo_dir)
    dest_dir = output_dir or (repo_path / ".brainfrog" / "scratch")
    dest_dir.mkdir(parents=True, exist_ok=True)
    timestamp = int(time.time() * 1000)
    out_png = dest_dir / f"screenshot_{timestamp}.png"

    resolved_target: str
    if target and target.strip():
        arg = target.strip()
        if arg.startswith(("http://", "https://", "file://")):
            resolved_target = arg
        else:
            cand = (repo_dir / arg).resolve()
            if cand.exists():
                resolved_target = cand.as_uri()
            else:
                # Treat as port number or hostname if numeric
                if arg.isdigit():
                    resolved_target = f"http://localhost:{arg}"
                else:
                    return None, f"Target preview/screenshot tidak ditemukan: {target}"
    else:
        # Default target discovery
        dev_url = os.environ.get("BRAINFROG_DEV_URL")
        if dev_url:
            resolved_target = dev_url
        else:
            from system2.visual_inspector import find_html_entrypoint

            html = find_html_entrypoint(repo_dir)
            if html and html.exists():
                resolved_target = html.resolve().as_uri()
            else:
                resolved_target = "http://localhost:3000"

    # 1. Try MCP verify server if configured
    mcp_path_str = os.environ.get(
        "BRAINFROG_VERIFY_MCP_PATH",
        r"C:\dame-project\tools\brainfrog-verify-mcp\dist\index.js",
    )
    mcp_script = Path(mcp_path_str)
    if mcp_script.exists():
        try:
            from core.mcp_client import McpClient

            client = McpClient(command="node", args=[str(mcp_script)])
            client.connect(timeout=8.0)
            instance_id = f"bf-shot-{os.getpid()}-{timestamp}"
            try:
                # Navigate to target
                nav_res = client.call_tool(
                    "navigate",
                    {"instanceId": instance_id, "url": resolved_target},
                    timeout=15.0,
                )
                if not nav_res.is_error:
                    time.sleep(0.5)
                    shot_res = client.call_tool(
                        "screenshot",
                        {"instanceId": instance_id, "fullPage": True},
                        timeout=15.0,
                    )
                    b64_shot = shot_res.image_base64
                    if b64_shot:
                        raw = base64.b64decode(b64_shot)
                        out_png.write_bytes(raw)
                        return load_and_validate_image(
                            out_png, source=f"mcp:{resolved_target}"
                        )
            finally:
                try:
                    client.call_tool("close_instance", {"instanceId": instance_id}, timeout=3.0)
                except Exception:
                    pass
                client.disconnect()
        except Exception:
            # Fall through to headless browser engine fallback
            pass

    # 2. Fallback to direct headless browser engine (Chrome / Edge)
    from system2.visual_inspector import capture_screenshot

    ok = capture_screenshot(
        resolved_target,
        out_png,
        browser_bin=browser_bin,
        timeout=20,
    )
    if ok and out_png.exists() and out_png.stat().st_size > 500:
        return load_and_validate_image(out_png, source=f"browser:{resolved_target}")

    return (
        None,
        f"Gagal mengambil screenshot dari '{resolved_target}'. "
        "Pastikan URL/server aktif atau browser Chrome/Edge tersedia.",
    )
