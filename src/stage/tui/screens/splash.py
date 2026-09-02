from textual.app import ComposeResult
from textual.containers import Center, Middle
from textual.screen import Screen
from textual.widgets import Static

from stage.banner import COMPACT, MIN_WIDE
from stage.banner import WIDE as BANNER
from stage.banner import block as _block

DISMISS_AFTER = 1.2

__all__ = ["BANNER", "COMPACT", "DISMISS_AFTER", "MIN_WIDE", "SplashScreen", "_block"]


class SplashScreen(Screen[None]):
    def __init__(self, summary: str, *, dismiss_after: float | None = DISMISS_AFTER) -> None:
        super().__init__()
        self._summary = summary
        self._dismiss_after = dismiss_after

    def compose(self) -> ComposeResult:
        art = _block(BANNER if self.app.size.width >= MIN_WIDE else COMPACT)
        hint = self._summary if self._dismiss_after else f"{self._summary}\n\npress any key"
        with Middle(), Center():
            yield Static(art, id="splash-art")
            yield Static(hint, id="splash-stats")

    def on_mount(self) -> None:
        if self._dismiss_after is not None:
            self.set_timer(self._dismiss_after, self._done)

    def on_key(self) -> None:
        self._done()

    def _done(self) -> None:
        if self.is_current:
            self.dismiss(None)
