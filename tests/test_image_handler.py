"""test_image_handler.py — Unit tests for multimodal image attachments in BrainFrog."""
import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

from core.image_handler import (
    IMAGE_EXTENSIONS,
    MAX_IMAGE_SIZE_BYTES,
    AttachedImage,
    capture_screenshot_for_repl,
    extract_image_references,
    get_clipboard_image,
    is_image_path,
    load_and_validate_image,
)


class TestImageHandler(unittest.TestCase):
    def test_is_image_path(self):
        self.assertTrue(is_image_path("screenshot.png"))
        self.assertTrue(is_image_path("path/to/image.jpg"))
        self.assertTrue(is_image_path("photo.jpeg"))
        self.assertTrue(is_image_path("mockup.webp"))
        self.assertTrue(is_image_path("MOCKUP.PNG"))
        self.assertTrue(is_image_path("mockup.WebP"))

        # Non-images or unsupported formats
        self.assertFalse(is_image_path("file.py"))
        self.assertFalse(is_image_path("index.html"))
        self.assertFalse(is_image_path("README.md"))
        self.assertFalse(is_image_path("animation.gif"))
        self.assertFalse(is_image_path("icon.svg"))
        self.assertFalse(is_image_path("bitmap.bmp"))

    def test_load_and_validate_image_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            img_file = tmp_path / "test.png"

            # Create a real valid 10x10 PNG image using Pillow
            im = Image.new("RGB", (10, 10), color="blue")
            im.save(img_file, format="PNG")

            attached, err = load_and_validate_image(img_file, source="file")
            self.assertIsNone(err)
            self.assertIsNotNone(attached)
            self.assertEqual(attached.filename, "test.png")
            self.assertEqual(attached.mime_type, "image/png")
            self.assertTrue(attached.size_kb > 0)
            self.assertEqual(attached.source, "file")

            # Check base64 content
            decoded = base64.b64decode(attached.base64_data)
            self.assertTrue(decoded.startswith(b"\x89PNG"))

    def test_load_and_validate_image_content_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            img_file = tmp_path / "test.jpg"
            im = Image.new("RGB", (8, 8), color="red")
            im.save(img_file, format="JPEG")

            attached, err = load_and_validate_image(img_file)
            self.assertIsNone(err)
            self.assertIsNotNone(attached)

            claude_block = attached.to_claude_content_block()
            self.assertEqual(claude_block["type"], "image")
            self.assertEqual(claude_block["source"]["type"], "base64")
            self.assertEqual(claude_block["source"]["media_type"], "image/jpeg")
            self.assertEqual(claude_block["source"]["data"], attached.base64_data)

            gemini_part = attached.to_gemini_part()
            self.assertEqual(gemini_part["inline_data"]["mime_type"], "image/jpeg")
            self.assertEqual(gemini_part["inline_data"]["data"], attached.base64_data)

    def test_load_and_validate_image_missing_file(self):
        attached, err = load_and_validate_image("nonexistent_image_12345.png")
        self.assertIsNone(attached)
        self.assertIsNotNone(err)
        self.assertIn("tidak ditemukan", err)

    def test_load_and_validate_image_unsupported_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bad_file = tmp_path / "image.gif"
            bad_file.write_bytes(b"GIF89a fake content")

            attached, err = load_and_validate_image(bad_file)
            self.assertIsNone(attached)
            self.assertIsNotNone(err)
            self.assertIn("Format gambar tidak didukung", err)

    def test_load_and_validate_image_size_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            oversized_file = tmp_path / "giant.png"
            # Write bytes exceeding MAX_IMAGE_SIZE_BYTES (5MB + 1024 bytes)
            oversized_file.write_bytes(b"0" * (MAX_IMAGE_SIZE_BYTES + 1024))

            attached, err = load_and_validate_image(oversized_file)
            self.assertIsNone(attached)
            self.assertIsNotNone(err)
            self.assertIn("terlalu besar", err)

    def test_extract_image_references_clean_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            img1 = repo_path / "screenshot.png"
            im = Image.new("RGB", (5, 5), color="green")
            im.save(img1, format="PNG")

            task = "Tolong perbaiki navbar sesuai @screenshot.png dan @src/main.py"
            cleaned_task, attached, errors = extract_image_references(task, repo_path)

            self.assertEqual(len(errors), 0)
            self.assertEqual(len(attached), 1)
            self.assertEqual(attached[0].filename, "screenshot.png")
            # @screenshot.png was extracted and removed; @src/main.py remains for code pinning
            self.assertNotIn("@screenshot.png", cleaned_task)
            self.assertIn("@src/main.py", cleaned_task)
            self.assertEqual(cleaned_task, "Tolong perbaiki navbar sesuai dan @src/main.py")

    def test_extract_image_references_missing_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            task = "Cek tampilan di @notfound_mockup.png"
            cleaned_task, attached, errors = extract_image_references(task, repo_path)

            self.assertEqual(len(attached), 0)
            self.assertEqual(len(errors), 1)
            self.assertIn("notfound_mockup.png", errors[0])

    def test_extract_image_references_multiple_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            img1 = repo_path / "before.png"
            img2 = repo_path / "after.webp"
            Image.new("RGB", (4, 4), color="yellow").save(img1, format="PNG")
            Image.new("RGB", (4, 4), color="cyan").save(img2, format="WEBP")

            task = "Bandingkan @before.png dengan @after.webp lalu selaraskan layout"
            cleaned, attached, errors = extract_image_references(task, repo_path)

            self.assertEqual(len(errors), 0)
            self.assertEqual(len(attached), 2)
            filenames = {im.filename for im in attached}
            self.assertEqual(filenames, {"before.png", "after.webp"})
            self.assertNotIn("@before.png", cleaned)
            self.assertNotIn("@after.webp", cleaned)

    def test_get_clipboard_image_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            mock_img = Image.new("RGB", (20, 20), color="purple")

            with patch("PIL.ImageGrab.grabclipboard", return_value=mock_img):
                attached, err = get_clipboard_image(repo_path)
                self.assertIsNone(err)
                self.assertIsNotNone(attached)
                self.assertEqual(attached.source, "clipboard")
                self.assertTrue(attached.path.exists())
                self.assertTrue(attached.filename.startswith("clipboard_"))

    def test_get_clipboard_image_non_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            # Clipboard contains text or None
            with patch("PIL.ImageGrab.grabclipboard", return_value="just some copied text"):
                attached, err = get_clipboard_image(repo_path)
                self.assertIsNone(attached)
                self.assertIsNotNone(err)

            with patch("PIL.ImageGrab.grabclipboard", return_value=None):
                attached, err = get_clipboard_image(repo_path)
                self.assertIsNone(attached)
                self.assertIsNotNone(err)

    def test_capture_screenshot_for_repl_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)

            def mock_capture(target, out_path, **kwargs):
                Path(out_path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 600)
                return True

            with patch.dict("os.environ", {"BRAINFROG_VERIFY_MCP_PATH": "nonexistent_mcp.js"}):
                with patch("system2.visual_inspector.capture_screenshot", side_effect=mock_capture):
                    attached, err = capture_screenshot_for_repl("http://localhost:3000", repo_path)
                    self.assertIsNone(err)
                    self.assertIsNotNone(attached)
                    self.assertTrue(attached.source.startswith("browser:"))
                    self.assertTrue(attached.path.exists())


if __name__ == "__main__":
    unittest.main()
