"""Mobile navbar component with broken responsive toggle for Phase 3 E2E test."""

class MobileNavbar:
    def __init__(self) -> None:
        self.is_open: bool = False
        self.breakpoint: int = 768

    def toggle(self) -> None:
        """Toggle mobile navbar menu open state."""
        # BUG: Fails to toggle the state!
        pass

    def render(self, viewport_width: int) -> str:
        """Render navigation markup."""
        if viewport_width < self.breakpoint and self.is_open:
            return "<nav class='navbar mobile mobile-menu-active'></nav>"
        return "<nav class='navbar desktop'></nav>"
