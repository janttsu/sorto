from __future__ import annotations

import logging
import textwrap
import threading
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Static

from sorto.engine import Engine
from sorto.eta import eta_text
from sorto.models import AnalysisView, Snapshot
from sorto.util import format_duration, human_size

log = logging.getLogger("sorto.tui")

OUTCOME_LABEL = {
    "moved": "MOVED",
    "planned": "PLANNED (dry run)",
    "kept": "KEPT IN SOURCE",
    "in_place": "ALREADY IN THE RIGHT PLACE",
    "deleted": "DELETED",
    "error": "ERROR",
    "waiting": "WAITING FOR MODEL",
}


def _plain(text: str = "", *, id: str | None = None, classes: str | None = None) -> Static:
    """Static that never parses Rich markup (filenames contain [tags])."""
    return Static(text, id=id, classes=classes, markup=False)


def _bar(pct: float, width: int = 28) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(round(width * pct / 100.0))
    return "█" * filled + "░" * (width - filled)


def _wrap(text: str, width: int, indent: str = "  ") -> list[str]:
    return textwrap.wrap(text, width=max(20, width), initial_indent=indent, subsequent_indent=indent) or [indent]


def outcome_label(outcome: str, *, reorganize: bool = False) -> str:
    if outcome == "kept" and reorganize:
        return "LEFT WHERE IT IS"
    return OUTCOME_LABEL.get(outcome, outcome.upper())


def render_view(
    view: AnalysisView, target: str, width: int, *, awaiting: bool = False, reorganize: bool = False
) -> str:
    """Plain-text block for one file: what it is and where it goes."""
    lines = [f"File:     {view.src_rel}", f"          {human_size(view.size)}  {view.mime or 'unknown type'}"]
    if view.current_location:
        lines += _wrap(f"Was in:   {view.current_location}", width, indent="")
    if view.folder:
        lines += _wrap(f"Folder:   {view.folder}", width, indent="")
    if view.media_note:
        lines += _wrap(f"Media:    {view.media_note}", width, indent="")
    if view.stage == "analyzing":
        lines += ["", f"Analyzing with the local model{f' ({view.model})' if view.model else ''}…"]
        return "\n".join(lines)
    lines += ["", "Analysis:"]
    lines += _wrap(view.summary or "(no description)", width)
    if view.label or view.confidence:
        took = f"   {view.model} {view.latency_s:.1f}s" if view.model and view.latency_s else ""
        lines.append(f"  type: {view.label}   confidence: {view.confidence:.2f}{took}")
    lines.append("")
    if view.jd_id:
        lines.append(f"JD ID:    {view.jd_id} {view.jd_name}".rstrip())
    if view.new_id:
        lines += _wrap(
            f"New ID:   {view.new_id} (next free number; sorto creates the folder first)",
            width,
            indent="",
        )
    if view.dest_rel:
        lines.append("Path:")
        lines += _wrap(f"{target.rstrip('/')}/{view.dest_rel}", width)
    if view.reason:
        lines.append("Why:")
        lines += _wrap(view.reason, width)
    if view.rule:
        lines.append("Rule:")
        lines += _wrap(view.rule, width)
    if awaiting:
        keep = "leave it where it is" if reorganize else "keep in source"
        ask = "Create the new ID and move it there?" if view.new_id else "Move it there?"
        if view.folder_unit:
            ask = "Move the whole folder there?" if not view.new_id else "Create the new ID and move the whole folder there?"
        lines += ["", f">>> {ask}  Enter / y = move   n = {keep}"]
    elif view.stage == "moving":
        lines += ["", "Moving…"]
    elif view.outcome:
        lines += ["", f"Result:   {outcome_label(view.outcome, reorganize=reorganize)}"]
    return "\n".join(lines)


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "dismiss", "Close"), Binding("q", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        yield _plain(
            "\n".join(
                [
                    " sorto keys",
                    "  q        quit (finishes the current file first)",
                    "  p        pause / resume",
                    "  d        toggle dry-run (only when idle)",
                    "  Enter/y  --confirm: move the shown file",
                    "  n        --confirm: keep it where it is",
                    "  m        switch model (when more than one is configured)",
                    "  ↓ / j    history: one file older (PgDn: ten)",
                    "  ↑ / k    history: one file newer (PgUp: ten)",
                    "  End      history: the oldest file of this run",
                    "  Esc/Home back to the latest file",
                    "  o        progress log tail",
                    "  ?        this help",
                    "",
                    " Only the local model is used. Files go into the IDs of the",
                    " target; a new ID or category gets the next free number.",
                    " Nothing is overwritten, git repositories are never touched,",
                    " and uncertain files stay in the source. Esc closes this.",
                ]
            ),
            id="help-body",
        )

    def action_dismiss(self) -> None:  # type: ignore[override]
        self.app.pop_screen()


class LogScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "dismiss", "Close"), Binding("q", "dismiss", "Close")]

    def __init__(self, path: Path):
        super().__init__()
        self.path = path

    def compose(self) -> ComposeResult:
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
            text = "\n".join(lines[-80:]) or "(empty)"
        except OSError as e:
            text = f"could not read {self.path}: {e}"
        yield _plain(f"{self.path}\n\n{text}", id="log-body")

    def action_dismiss(self) -> None:  # type: ignore[override]
        self.app.pop_screen()


class SortoApp(App[None]):
    CSS = """
    Screen { background: #0f1419; color: #e6edf3; layout: vertical; }
    #title { background: #1f6feb; color: #ffffff; padding: 0 1; height: 1; text-style: bold; }
    #roots, #stats, #progress, #eta, #keys { height: 1; padding: 0 1; }
    #eta { color: #d2a8ff; }
    #roots { color: #c9d1d9; }
    #stats { color: #9ecbff; }
    #progress { color: #7ee787; }
    .panel-title { color: #ffa657; text-style: bold; height: 1; padding: 0 1; }
    #mid { height: 1fr; }
    #left { width: 3fr; }
    #right { width: 2fr; }
    #now { height: 3fr; padding: 0 1; border: solid #1f6feb; overflow-y: auto; }
    #last { height: 2fr; padding: 0 1; border: solid #30363d; color: #c9d1d9; overflow-y: auto; }
    #history { height: 1fr; padding: 0 1; border: solid #30363d; overflow-y: auto; }
    #log { height: 3; padding: 0 1; border-top: solid #30363d; color: #8b949e; }
    #keys { background: #161b22; color: #8b949e; }
    #banner { background: #9e6a03; color: #000; height: 1; padding: 0 1; display: none; }
    #banner.visible { display: block; }
    HelpScreen, LogScreen { align: center middle; }
    #help-body, #log-body {
        background: #161b22; border: solid #1f6feb; width: 80%; height: auto;
        max-height: 90%; padding: 1 2; overflow-y: auto;
    }
    Footer { display: none; }
    """

    BINDINGS = [
        Binding("q", "quit_app", "Quit", priority=True),
        Binding("p", "toggle_pause", "Pause"),
        Binding("d", "toggle_dry", "Dry-run"),
        Binding("o", "open_log", "Log"),
        Binding("m", "switch_model", "Model"),
        Binding("enter", "confirm_move", "Move", show=False),
        Binding("y", "confirm_move", "Move", show=False),
        Binding("n", "confirm_keep", "Keep", show=False),
        Binding("question_mark", "help", "Help"),
        Binding("down", "history(1)", "Older", show=False),
        Binding("j", "history(1)", "Older", show=False),
        Binding("up", "history(-1)", "Newer", show=False),
        Binding("k", "history(-1)", "Newer", show=False),
        Binding("pagedown", "history(10)", "Older", show=False),
        Binding("pageup", "history(-10)", "Newer", show=False),
        Binding("end", "history(100000)", "Oldest", show=False),
        Binding("home", "history_latest", "Latest", show=False),
        Binding("escape", "history_latest", "Latest", show=False),
    ]

    KEYS = "keys: q quit  p pause  d dry-run (idle)  o log  ↑↓ history  ? help"

    def __init__(self, engine: Engine):
        super().__init__()
        self.engine = engine
        self._quitting = False
        # Browsing the history: the seq of the file shown in the lower panel, or None to
        # follow the latest. A seq, not a row number, so that new files do not shift it.
        self._browse_seq: int | None = None
        self._hist: list[AnalysisView] = []  # newest first, as last drawn
        self._shown = 0  # row of the file the lower panel shows

    def compose(self) -> ComposeResult:
        yield _plain("sorto", id="title")
        yield _plain("", id="roots")
        yield _plain("", id="banner")
        yield _plain("", id="stats")
        yield _plain("", id="progress")
        yield _plain("", id="eta")
        with Horizontal(id="mid"):
            with Vertical(id="left"):
                yield _plain("NOW", id="now-title", classes="panel-title")
                yield _plain("", id="now")
                yield _plain("LAST FILED", id="last-title", classes="panel-title")
                yield _plain("", id="last")
            with Vertical(id="right"):
                yield _plain("HISTORY", id="history-title", classes="panel-title")
                yield _plain("", id="history")
        yield _plain("", id="log")
        yield _plain(self._keys_text(), id="keys")
        yield Footer()

    def _keys_text(self) -> str:
        cfg = self.engine.cfg
        return (
            self.KEYS
            + ("  m model" if len(cfg.models) > 1 else "")
            + ("  Enter/y move  n keep" if cfg.confirm else "")
        )

    def on_mount(self) -> None:
        self.engine.start()
        self.set_interval(0.4, self._tick)
        self._tick()

    def _tick(self) -> None:
        try:
            snap = self.engine.snapshot()
            self._render(snap)
        except Exception:
            log.exception("tui render failed")
            return
        if snap.finished and not self.engine.cfg.follow and not self._quitting:
            self._quitting = True
            self.set_timer(1.5, self.exit)

    def _panel_width(self, widget_id: str) -> int:
        try:
            return max(30, self.query_one(widget_id, Static).size.width - 4)
        except Exception:
            return 70

    def _render(self, snap: Snapshot) -> None:
        c = snap.counts
        rules = f"  rules={snap.rules}" if snap.rules else ""
        self.query_one("#title", Static).update(
            f" sorto  model={snap.model} (local)  mode={snap.mode}{rules}  scan: {snap.scan_state}"
        )
        if snap.reorganize:
            where = f"{snap.target}/{snap.reorganize_scope}" if snap.reorganize_scope else snap.target
            roots = (
                f"reorganizing {where} in place, depth {snap.reorganize_depth}   "
                f"({snap.jd_ids} Johnny.Decimal IDs)"
            )
        else:
            roots = f"{snap.source}  →  {snap.target}   ({snap.jd_ids} Johnny.Decimal IDs)"
        self.query_one("#roots", Static).update(roots)
        banner = self.query_one("#banner", Static)
        if snap.paused:
            banner.update(" PAUSED — press p to resume")
            banner.add_class("visible")
        elif not snap.llm_ok:
            banner.update(f" LOCAL MODEL UNAVAILABLE — retrying: {snap.llm_error or ''}")
            banner.add_class("visible")
        else:
            banner.remove_class("visible")
        self.query_one("#stats", Static).update(
            f"files {c.total:,} | waiting {c.pending:,} | moved {c.done:,} | "
            f"kept {c.skipped + c.needs_user:,} | errors {c.error}"
            + (f" | identifying {snap.current_identify}" if snap.current_identify else "")
        )
        self.query_one("#progress", Static).update(
            f"{_bar(snap.progress_pct)}  {snap.progress_pct:5.1f}%   "
            f"elapsed {format_duration(snap.elapsed_s)}"
            + (f"   last answer {snap.llm_latency_s:.1f}s" if snap.llm_latency_s else "")
            + (
                "   " + " | ".join(f"{m.model} {m.avg_s:.1f}s×{m.files}" for m in snap.model_stats)
                if len(snap.model_stats) > 1
                else ""
            )
        )
        self.query_one("#eta", Static).update(f"time left: {eta_text(snap)}")
        now = snap.current
        width = self._panel_width("#now")
        if now is not None:
            self.query_one("#now", Static).update(
                render_view(now, snap.target, width, awaiting=snap.awaiting_confirm, reorganize=snap.reorganize)
            )
        else:
            self.query_one("#now", Static).update(
                "Waiting for files…" if not snap.finished else "Done — nothing left to sort."
            )
        self._render_history(snap)
        tail = snap.log_lines[-2:]
        self.query_one("#log", Static).update("\n".join(tail) if tail else "(no events yet)")

    def _render_history(self, snap: Snapshot) -> None:
        """The lower panel and the history list: the latest file, or the one being browsed."""
        hist = self._hist = snap.history
        now = snap.current
        browsing = next((i for i, h in enumerate(hist) if h.seq == self._browse_seq), None)
        if browsing is None:
            # Following: the file before the one in NOW (which stays there until the next starts).
            self._browse_seq = None
            row = next((i for i, h in enumerate(hist) if now is None or h.seq != (now.seq or -1)), None)
        else:
            row = browsing
        self._shown = row or 0
        shown = hist[row] if row is not None else None
        title = "LAST FILED" + ("   (↑↓ browse older files)" if len(hist) > 1 else "")
        if browsing is not None:
            title = f"HISTORY {row + 1} of {len(hist)}, newest first   (↑↓ browse, Esc = back to the latest)"
        self.query_one("#last-title", Static).update(title)
        self.query_one("#last", Static).update(
            render_view(shown, snap.target, self._panel_width("#last"), reorganize=snap.reorganize)
            if shown
            else "(nothing yet)"
        )
        try:
            height = max(3, self.query_one("#history", Static).size.height - 2)
        except Exception:
            height = 20
        # Keep the shown row in view; while following, the list stays at the top.
        first = 0 if browsing is None else max(0, min(row - height // 2, len(hist) - height))
        lines = [
            f"{'▶' if i == row else ' '} {outcome_label(h.outcome, reorganize=snap.reorganize)[:7]:<7} "
            f"{h.filename}  → {h.jd_id or '-'}"
            for i, h in enumerate(hist[first : first + height], start=first)
        ]
        self.query_one("#history-title", Static).update(f"HISTORY ({len(hist)})" if hist else "HISTORY")
        self.query_one("#history", Static).update("\n".join(lines) or "(empty)")

    def action_history(self, step: int) -> None:
        """Show an older (step > 0) or newer file in the lower panel; past the newest, follow again."""
        if not self._hist or (self._browse_seq is None and step < 0):
            return
        row = self._shown + step
        self._browse_seq = self._hist[min(row, len(self._hist) - 1)].seq if row >= 0 else None
        self._tick()

    def action_history_latest(self) -> None:
        self._browse_seq = None
        self._tick()

    def action_toggle_pause(self) -> None:
        self.engine.toggle_pause()

    def action_confirm_move(self) -> None:
        self.engine.decide("move")

    def action_confirm_keep(self) -> None:
        self.engine.decide("keep")

    def action_switch_model(self) -> None:
        keys = self.query_one("#keys", Static)
        if len(self.engine.cfg.models) < 2:
            keys.update("only one model is configured (add more with [llm] models = [...])")
            return
        keys.update("switching model… it is used from the next file on")

        def _switch() -> None:
            try:
                model = self.engine.switch_model()
                msg = f"model → {model} from the next file   {self._keys_text()}"
            except Exception as e:  # noqa: BLE001 - shown to the user
                msg = f"could not switch model: {e}"
            self.call_from_thread(keys.update, msg)

        threading.Thread(target=_switch, name="sorto-switch", daemon=True).start()

    def action_toggle_dry(self) -> None:
        new_val = not self.engine.cfg.dry_run
        keys = self.query_one("#keys", Static)
        if not self.engine.set_dry_run(new_val):
            keys.update("dry-run can only be toggled when idle (nothing queued or in flight)")
        else:
            keys.update(f"{self.KEYS}   mode={'DRY' if new_val else 'LIVE'}")

    def action_open_log(self) -> None:
        self.push_screen(LogScreen(self.engine.cfg.progress_path))

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_quit_app(self) -> None:
        if self._quitting:
            self.exit()
            return
        self._quitting = True
        self.query_one("#keys", Static).update("quitting… finishing the current file (q again to force)")
        self.engine.request_stop()

        def _join() -> None:
            self.engine.join(timeout=10)
            self.call_from_thread(self.exit)

        threading.Thread(target=_join, daemon=True).start()
