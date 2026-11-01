from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import DataTable, Static


class HelpPanel(VerticalScroll):
    def compose(self) -> ComposeResult:
        yield Static("", id="help-text")


class HelpOverlay:
    HELP_TEXT = ""
    _before_help: Widget | None = None

    def dismiss_overlays(self) -> None:
        return

    def action_help(self) -> None:
        if self.close_help():
            return
        self.dismiss_overlays()
        panel = self.query_one("#help", HelpPanel)  # type: ignore[attr-defined]
        panel.query_one("#help-text", Static).update(self.HELP_TEXT)
        panel.add_class("visible")
        panel.scroll_home(animate=False)
        self._before_help = self.focused  # type: ignore[attr-defined]
        panel.focus()

    def close_help(self) -> bool:
        panel = self.query_one("#help", HelpPanel)  # type: ignore[attr-defined]
        if not panel.has_class("visible"):
            return False
        panel.remove_class("visible")
        before, self._before_help = self._before_help, None
        tables = self.query(DataTable)  # type: ignore[attr-defined]
        if before is not None and _shown(before):
            before.focus()
        elif tables:
            tables.first().focus()
        else:
            panel.blur()
        return True


def _shown(widget: Widget) -> bool:
    return widget.is_attached and all(
        node.display for node in widget.ancestors_with_self if isinstance(node, Widget)
    )


__all__ = ["HelpOverlay", "HelpPanel"]
