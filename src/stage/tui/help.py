from textual.widgets import Static


class HelpOverlay:
    HELP_TEXT = ""

    def dismiss_overlays(self) -> None:
        return

    def action_help(self) -> None:
        panel = self.query_one("#help", Static)  # type: ignore[attr-defined]
        showing = not panel.has_class("visible")
        if showing:
            self.dismiss_overlays()
            panel.update(self.HELP_TEXT)
        panel.set_class(showing, "visible")

    def close_help(self) -> bool:
        panel = self.query_one("#help", Static)  # type: ignore[attr-defined]
        if panel.has_class("visible"):
            panel.remove_class("visible")
            return True
        return False


__all__ = ["HelpOverlay"]
