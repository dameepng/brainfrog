"""Targeted domain tests for MobileNavbar."""
import sys
from pathlib import Path

# Ensure local navbar module can be resolved
repo_dir = Path(__file__).resolve().parent.parent
if str(repo_dir) not in sys.path:
    sys.path.insert(0, str(repo_dir))

from navbar import MobileNavbar


def test_navbar_initial_state():
    nav = MobileNavbar()
    assert nav.is_open is False


def test_navbar_toggle_flips_state():
    nav = MobileNavbar()
    nav.toggle()
    assert nav.is_open is True
    nav.toggle()
    assert nav.is_open is False


def test_navbar_renders_mobile_active_when_open():
    nav = MobileNavbar()
    nav.toggle()
    html = nav.render(viewport_width=375)
    assert "mobile-menu-active" in html
