"""test_visual_inspector.py — Unit tests for Visual Inspector & Headless Screenshot Engine."""
import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from system2.visual_inspector import (
    capture_screenshot,
    find_browser_bin,
    find_html_entrypoint,
    get_image_base64,
    is_frontend_change,
)


class TestVisualInspector(unittest.TestCase):
    def test_find_browser_bin(self):
        # Should locate Chrome or Edge if installed
        bin_path = find_browser_bin()
        if bin_path:
            self.assertTrue(Path(bin_path).exists())
            self.assertTrue(any(b in bin_path.lower() for b in ("chrome", "edge", "chromium")))

    def test_find_html_entrypoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # No html
            self.assertIsNone(find_html_entrypoint(tmp_path))

            # index.html at root
            index = tmp_path / "index.html"
            index.write_text("<!DOCTYPE html><html><body>Test</body></html>", encoding="utf-8")
            self.assertEqual(find_html_entrypoint(tmp_path), index)

            # public/index.html priority
            index.unlink()
            pub_index = tmp_path / "public" / "index.html"
            pub_index.parent.mkdir(parents=True)
            pub_index.write_text("<!DOCTYPE html><html><body>Public</body></html>", encoding="utf-8")
            self.assertEqual(find_html_entrypoint(tmp_path), pub_index)

    def test_is_frontend_change(self):
        self.assertTrue(is_frontend_change(["index.html"]))
        self.assertTrue(is_frontend_change(["style.css"]))
        self.assertTrue(is_frontend_change(["src/App.jsx"]))
        self.assertTrue(is_frontend_change(["components/Button.tsx"]))
        self.assertTrue(is_frontend_change(["views/Home.vue"]))
        self.assertTrue(is_frontend_change(["src/styles/theme.scss"]))

        # Backend only
        self.assertFalse(is_frontend_change(["main.py", "server.go", "cargo.toml"]))
        self.assertFalse(is_frontend_change(["orchestrator.py", "config.py"]))

    def test_get_image_base64(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "sample.png"
            test_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            img.write_bytes(test_bytes)
            encoded = get_image_base64(img)
            self.assertEqual(base64.b64decode(encoded), test_bytes)

    def test_capture_screenshot(self):
        browser_bin = find_browser_bin()
        if not browser_bin:
            self.skipTest("No Chrome/Edge browser installed on test machine.")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            html_file = tmp_path / "test.html"
            html_file.write_text(
                "<!DOCTYPE html><html><body style='background:#121212;color:#fff;'><h1>BrainFrog Test</h1></body></html>",
                encoding="utf-8",
            )
            out_png = tmp_path / "test.png"
            success = capture_screenshot(html_file, out_png, browser_bin=browser_bin, timeout=10)
            self.assertTrue(success)
            self.assertTrue(out_png.exists())
            self.assertGreater(out_png.stat().st_size, 500)


if __name__ == "__main__":
    unittest.main()
