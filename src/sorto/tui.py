from __future__ import annotations

import logging
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Static

from sorto.engine import Engine
from sorto.models import Snapshot
from sorto.util import format_duration, human_size

log = logging.getLogger("sorto.tui")


def _plain(text: str = "", *, id: str | None = None) -> Static:
    """Static that never parses Rich markup (filenames contain [tags])."""
    return Static(text, id=id, markup=False)


def _bar(pct: float, width: int = 28) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(round(width * pct / 100.0))
    return "█" * filled + "░" * (width - filled)


def _conf(c: float) -> str:
    return f"{c:.2f}"


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "dismiss", "Close"), Binding("q", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        yield _plain(
            "\n".join(
                [
                    " sorto keys",
                    "  q        quit (finish or abandon in-flight)",
                    "  p        pause / resume (no new work while paused)",
                    "  d        toggle dry-run (only when idle)",
                    "  o        open progress log tail",
                    "  ?        this help",
                    "  Esc      close overlay",
                    "",
                    " Safety: never deletes, never overwrites, never leaves root.",
                    " Uncertain files go to _unsorted/. Press Esc to close.",
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
        text = "(empty)"
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
    Screen {
        background: #0f1419;
        color: #e6edf3;
        layout: vertical;
    }
    #title {
        background: #1f6feb;
        color: #ffffff;
        padding: 0 1;
        height: 1;
        text-style: bold;
    }
    #stats, #progress, #keys {
        height: 1;
        padding: 0 1;
    }
    #stats { color: #9ecbff; }
    #progress { color: #7ee787; }
    #now-title, #analysis-title, #queue-title {
        color: #ffa657;
        text-style: bold;
        height: 1;
        padding: 0 1;
    }
    #now {
        height: 4;
        padding: 0 1;
        color: #c9d1d9;
    }
    #mid {
        height: 1fr;
    }
    #analysis, #queue {
        height: 1fr;
        padding: 0 1;
        border: solid #30363d;
    }
    #analysis { width: 3fr; }
    #queue { width: 2fr; }
    #log {
        height: 5;
        padding: 0 1;
        border-top: solid #30363d;
        color: #8b949e;
    }
    #keys {
        background: #161b22;
        color: #8b949e;
        height: 1;
    }
    #paused-banner {
        background: #9e6a03;
        color: #000;
        height: 1;
        padding: 0 1;
        display: none;
    }
    #paused-banner.visible { display: block; }
    HelpScreen, LogScreen {
        align: center middle;
    }
    #help-body, #log-body {
        background: #161b22;
        border: solid #1f6feb;
        width: 80%;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        overflow-y: auto;
    }
    Footer { display: none; }
    """

    BINDINGS = [
        Binding("q", "quit_app", "Quit", priority=True),
        Binding("p", "toggle_pause", "Pause"),
        Binding("d", "toggle_dry", "Dry-run"),
        Binding("o", "open_log", "Log"),
        Binding("question_mark", "help", "Help"),
    ]

    def __init__(self, engine: Engine):
        super().__init__()
        self.engine = engine
        self._log_seq = 0
        self._quitting = False

    def compose(self) -> ComposeResult:
        yield _plain("sorto", id="title")
        yield _plain("", id="paused-banner")
        yield _plain("", id="stats")
        yield _plain("", id="progress")
        yield _plain("NOW", id="now-title")
        yield _plain("", id="now")
        with Horizontal(id="mid"):
            with Vertical():
                yield _plain("ANALYSIS", id="analysis-title")
                yield _plain("", id="analysis")
            with Vertical():
                yield _plain("QUEUE / RECENT", id="queue-title")
                yield _plain("", id="queue")
        yield _plain("", id="log")
        yield _plain(
            "keys: q quit  p pause  d dry-run (idle)  o open log  ? help",
            id="keys",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.engine.start()
        self.set_interval(0.5, self._tick)
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
            self.set_timer(0.4, self.exit)

    def _render(self, snap: Snapshot) -> None:
        c = snap.counts
        model = snap.model
        title = (
            f" sorto  root={snap.root}  model={model}  mode={snap.mode}  "
            f"scan: {snap.scan_state}"
        )
        self.query_one("#title", Static).update(title)
        banner = self.query_one("#paused-banner", Static)
        if snap.paused:
            banner.update(" PAUSED — press p to resume")
            banner.add_class("visible")
        else:
            banner.remove_class("visible")
        llm_state = "ok" if snap.llm_ok else f"BLOCKED {snap.llm_error or ''}"
        self.query_one("#stats", Static).update(
            f"SCAN  {c.total:,} files  | queue {c.pending:,} | analyzing {c.analyzing} | "
            f"moving {c.moving} | done {c.done:,} | skipped {c.skipped} | "
            f"error {c.error} | needs_user {c.needs_user}  llm:{llm_state}"
        )
        eta = format_duration(snap.eta_s) if snap.eta_s is not None else "unknown"
        still = "  scan still running" if snap.scan_still_running else ""
        self.query_one("#progress", Static).update(
            f"{_bar(snap.progress_pct)}  {snap.progress_pct:5.1f}%   "
            f"elapsed {format_duration(snap.elapsed_s)}   ETA {eta}   "
            f"{c.done}/{c.total}{still}"
        )
        lat = ""
        if snap.current_analyze and snap.llm_latency_s:
            lat = f"   {snap.llm_latency_s:.1f}s  tokens~{snap.tokens_est}"
        now_lines = [
            f"identify   {snap.current_identify or '—'}",
            f"analyze    {snap.current_analyze or '—'}{lat}",
            f"move       {snap.current_move or '—'}",
        ]
        self.query_one("#now", Static).update("\n".join(now_lines))
        a = snap.last_analysis
        if a and a.filename:
            analysis = "\n".join(
                [
                    f"file: {a.filename}",
                    f"mime: {a.mime or '?'}",
                    f"size: {human_size(a.size)}",
                    f"label: {a.label}",
                    f"confidence: {_conf(a.confidence)}",
                    f"dest: {a.dest_rel}",
                    f"reason: {a.reason}",
                ]
            )
        else:
            analysis = "(waiting for first analysis)"
        self.query_one("#analysis", Static).update(analysis)
        rows = snap.queue_rows[:12]
        if rows:
            qtxt = "\n".join(f"{r.status:<12} {r.src_rel}" for r in rows)
        else:
            qtxt = "(empty)"
        self.query_one("#queue", Static).update(qtxt)
        log_tail = snap.log_lines[-4:]
        self.query_one("#log", Static).update(
            "log: " + ("\n     ".join(log_tail) if log_tail else "(no events yet)")
        )

    def action_toggle_pause(self) -> None:
        self.engine.toggle_pause()

    def action_toggle_dry(self) -> None:
        new_val = not self.engine.cfg.dry_run
        if not self.engine.set_dry_run(new_val):
            self.query_one("#keys", Static).update(
                "keys: dry-run toggle only when idle (queue empty, nothing in-flight)"
            )
        else:
            mode = "DRY" if new_val else "LIVE"
            self.query_one("#keys", Static).update(
                f"keys: q quit  p pause  d dry-run  o log  ? help   mode={mode}"
            )

    def action_open_log(self) -> None:
        self.push_screen(LogScreen(self.engine.cfg.progress_path))

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_quit_app(self) -> None:
        if self._quitting:
            self.exit()
            return
        self._quitting = True
        self.query_one("#keys", Static).update("quitting… finishing in-flight (up to 5s)")
        self.engine.request_stop()

        def _join() -> None:
            self.engine.join(timeout=5)
            self.call_from_thread(self.exit)

        import threading

        threading.Thread(target=_join, daemon=True).start()
