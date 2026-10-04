"""Which local model to use: asked at the start when no ``--model`` was given and a TUI is in use."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Input, Label, ListItem, ListView, Static


class _ModelRow(ListItem):
    def __init__(self, text: Text, name: str) -> None:
        super().__init__(Label(text))
        self.model_name = name


class ModelPicker(App[str | None]):
    """Every model the local server offers; Enter picks one, Esc leaves without running."""

    TITLE = "sorto"
    CSS = """
    Screen { background: #0f1419; color: #e6edf3; layout: vertical; }
    #title { background: #1f6feb; color: #ffffff; padding: 0 1; height: 1; text-style: bold; }
    #purpose { padding: 1 1 0 1; color: #9ecbff; height: auto; }
    #filter { margin: 1 1 0 1; border: tall #30363d; background: #161b22; }
    #models { height: 1fr; margin: 0 1; border: solid #30363d; background: #0f1419; }
    #models > ListItem { background: #0f1419; padding: 0 1; }
    #models > ListItem.-highlight { background: #1f2937; }
    #keys { height: 1; background: #161b22; color: #8b949e; padding: 0 1; }
    """
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("down", "move(1)", "Down", show=False, priority=True),
        Binding("up", "move(-1)", "Up", show=False, priority=True),
        Binding("enter", "choose", "Choose", priority=True),
    ]

    def __init__(self, models: list[dict[str, Any]], *, default: str = "", purpose: str = "") -> None:
        super().__init__()
        self.models = models
        self.default = default
        self.purpose = purpose

    def compose(self) -> ComposeResult:
        yield Static(Text(" sorto — choose the local model"), id="title")
        yield Static(Text(self.purpose), id="purpose")
        yield Input(placeholder="type to filter, e.g. qwen3.6", id="filter")
        yield ListView(id="models")
        yield Static(
            Text.assemble((" ↑↓ ", "bold #0f1419 on #8b949e"), " move   ", (" Enter ", "bold #0f1419 on #7ee787"),
                          " use this model   ", (" Esc ", "bold #0f1419 on #8b949e"), " cancel"),
            id="keys",
        )

    def on_mount(self) -> None:
        self._fill("")
        self.query_one("#filter", Input).focus()

    def _row(self, m: dict[str, Any]) -> Text:
        text = Text(f"{m['name']:<44}", style="bold" if m["name"] == self.default else "")
        size = f"{m['size'] / 1e9:5.1f} GB" if m.get("size") else ""
        text.append(f"{size:>9}  {m.get('parameters', ''):>7}  {m.get('quantization', ''):<8}", style="#8b949e")
        if m["name"] == self.default:
            text.append("  default", style="bold #7ee787")
        if m.get("loaded"):
            text.append("  in memory", style="bold #d2a8ff")
        return text

    def _fill(self, query: str) -> None:
        listing = self.query_one("#models", ListView)
        listing.clear()
        words = query.lower().split()
        shown = [m for m in self.models if all(w in m["name"].lower() for w in words)]
        for m in shown:
            listing.append(_ModelRow(self._row(m), m["name"]))
        names = [m["name"] for m in shown]
        if names:
            listing.index = names.index(self.default) if self.default in names else 0

    def on_input_changed(self, event: Input.Changed) -> None:
        self._fill(event.value)

    def action_move(self, step: int) -> None:
        listing = self.query_one("#models", ListView)
        if listing.children:
            listing.index = max(0, min(len(listing.children) - 1, (listing.index or 0) + step))

    def action_choose(self) -> None:
        row = self.query_one("#models", ListView).highlighted_child
        if isinstance(row, _ModelRow):
            self.exit(row.model_name)

    def action_cancel(self) -> None:
        self.exit(None)


def pick_model(llm: Any, *, default: str, purpose: str) -> str | None:
    """Ask which model to use; None when the user cancelled. Without a list from the server, the default."""
    catalog = getattr(llm, "model_catalog", None)
    try:
        models = catalog() if catalog is not None else []
    except Exception:  # noqa: BLE001 - no list: carry on with the configured model
        models = []
    if not models:
        return default
    return ModelPicker(models, default=default, purpose=purpose).run()
