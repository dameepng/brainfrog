"""Unit tests for TUI copy-paste UX and multi-line input support."""
import io
import unittest
from rich.console import Console
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit import PromptSession
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.filters import has_completions
import cli


class TestTuiUx(unittest.TestCase):
    def test_banner_box_plain_text_copy_paste(self):
        """Verify print_banner_box prints plain styled text without Panel/box borders or margin padding."""
        output_buf = io.StringIO()
        test_console = Console(file=output_buf, force_terminal=False, color_system=None, width=100)

        orig_console = cli.console
        try:
            cli.console = test_console

            error_msg = (
                "Stopped for human review after 2 retr(ies). Last test output:\n"
                "FAILED tests/test_core.py::test_auth - AssertionError: expected status 200, got 401\n"
                "assert 401 == 200"
            )
            cli.print_banner_box(error_msg, level="error", title="Step 2 ESCALATED")
            raw_output = output_buf.getvalue()

            # Check that no 4-sided box characters exist
            box_corners_and_walls = ["╭", "╮", "╯", "╰", "│", "║"]
            for c in box_corners_and_walls:
                self.assertNotIn(c, raw_output, f"Found box character {c!r} in banner output")

            # Check content is preserved
            self.assertIn("Step 2 ESCALATED", raw_output)
            self.assertIn("FAILED tests/test_core.py::test_auth", raw_output)
            self.assertIn("assert 401 == 200", raw_output)

            # Verify no lines have leading margin padding spaces (flush left)
            lines = [l for l in raw_output.splitlines() if l.strip()]
            self.assertGreaterEqual(len(lines), 3)
            self.assertFalse(lines[0].startswith("   "), "Title line should be flush left")
        finally:
            cli.console = orig_console

    def test_multiline_key_detection_registered(self):
        """Verify _setup_multiline_key_detection hooks ANSI sequences."""
        from prompt_toolkit.input.vt100_parser import ANSI_SEQUENCES
        from prompt_toolkit.keys import Keys

        cli._setup_multiline_key_detection()

        self.assertIn("\x1b[13;2u", ANSI_SEQUENCES)
        self.assertIn("\x1b[27;2;13~", ANSI_SEQUENCES)
        self.assertEqual(ANSI_SEQUENCES["\x1b[13;2u"], (Keys.Escape, Keys.ControlM))
        self.assertEqual(ANSI_SEQUENCES["\x1b[27;2;13~"], (Keys.Escape, Keys.ControlM))

    def test_multiline_alt_enter_and_backslash(self):
        """Verify Alt+Enter and backslash continuation in prompt session."""
        cli._setup_multiline_key_detection()

        kb = KeyBindings()

        @kb.add("enter", filter=has_completions)
        def _accept_completion_handler(event):
            b = event.current_buffer
            if b.complete_state:
                if b.complete_state.current_completion:
                    b.apply_completion(b.complete_state.current_completion)
                else:
                    b.complete_state = None

        @kb.add("enter", filter=~has_completions)
        def _enter_submit_handler(event):
            b = event.current_buffer
            doc = b.document
            current_line = doc.current_line_before_cursor
            if current_line.endswith("\\"):
                b.delete_before_cursor(1)
                b.insert_text("\n")
            else:
                b.validate_and_handle()

        @kb.add("escape", "enter")
        def _alt_enter_newline_handler(event):
            event.current_buffer.insert_text("\n")

        # 1. Alt+Enter
        with create_pipe_input() as inp:
            session = PromptSession(input=inp, output=DummyOutput(), key_bindings=kb, multiline=True)
            inp.send_text("line 1\x1b\rline 2\r")
            res = session.prompt(">")
            self.assertEqual(res, "line 1\nline 2")

        # 2. Backslash continuation
        with create_pipe_input() as inp:
            session = PromptSession(input=inp, output=DummyOutput(), key_bindings=kb, multiline=True)
            inp.send_text("line A\\\rline B\r")
            res = session.prompt(">")
            self.assertEqual(res, "line A\nline B")

        # 3. Kitty Shift+Enter
        with create_pipe_input() as inp:
            session = PromptSession(input=inp, output=DummyOutput(), key_bindings=kb, multiline=True)
            inp.send_text("kitty 1\x1b[13;2ukitty 2\r")
            res = session.prompt(">")
            self.assertEqual(res, "kitty 1\nkitty 2")


if __name__ == "__main__":
    unittest.main()

