"""The TUI of ``sorto fsck``: the model's review with a progress bar, then every change to accept one by one.

First the structure model reviews the tree (progress bar, phase, ETA). Then
the list shows, at the top, the model's proposals for the structure and its
findings about folders, as text, and the problems that need a human; below
them every note to change, with its diff in colour, written only on ``y``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Label, ListItem, ListView, ProgressBar, Static

from sorto.fsck import Change, Report, apply, diff_text, edit_text
from sorto.fsck_model import Progress, eta_text

MARKS = {
    "pending": ("·", "#8b949e"),
    "written": ("✓", "#7ee787"),
    "skipped": ("✗", "#f85149"),
    "error": ("!", "#d29922"),
}
PROPOSALS, FINDINGS, PROBLEMS = -3, -2, -1  # rows above the changes
PHASES = {
    "scanning": "Scanning the folders",
    "thinking": "Reviewing category",
    "structure": "Thinking about the whole structure",
    "done": "Done",
}
KIND_STYLE = {"duplicate": "#d2a8ff", "misplaced": "#ffa657", "rename": "#79c0ff", "name": "#7ee787"}


@dataclass
class Outcome:
    """What the user did in the TUI, for the summary and the exit status."""

    written: int = 0
    declined: int = 0
    errors: list[str] = field(default_factory=list)


class _Row(ListItem):
    def __init__(self, text: Text, index: int) -> None:
        super().__init__(Label(text))
        self.index = index  # PROPOSALS / FINDINGS / PROBLEMS, or the change's number


class FsckApp(App[Outcome]):
    TITLE = "sorto fsck"
    CSS = """
    Screen { background: #0f1419; color: #e6edf3; layout: vertical; }
    #title { background: #1f6feb; color: #ffffff; padding: 0 1; height: 1; text-style: bold; }
    #summary { height: 1; padding: 0 1; color: #9ecbff; }
    #working { height: 1fr; align: center middle; }
    #work-box { width: 80%; height: auto; border: solid #1f6feb; padding: 1 2; background: #161b22; }
    #work-head { text-style: bold; color: #ffa657; }
    #work-bar { margin: 1 0; }
    #mid { height: 1fr; display: none; }
    #changes { width: 2fr; border: solid #30363d; background: #0f1419; }
    #changes > ListItem { background: #0f1419; padding: 0 1; }
    #changes > ListItem.-highlight { background: #1f2937; }
    #detail { width: 3fr; border: solid #1f6feb; padding: 0 1; }
    #keys { height: 1; background: #161b22; color: #8b949e; padding: 0 1; }
    """

    BINDINGS = [
        Binding("y", "write", "Write"),
        Binding("n", "skip", "Skip"),
        Binding("e", "edit", "Edit"),
        Binding("home", "go(0)", "First", show=False),
        Binding("end", "go(-1)", "Last", show=False),
        Binding("q", "quit_fsck", "Quit", priority=True),
        Binding("escape", "quit_fsck", "Quit", show=False),
    ]

    def __init__(
        self,
        root: Path,
        backups: Path,
        *,
        report: Report | None = None,
        prepare: Callable[[Callable[[Progress], None]], Report] | None = None,
        model: str = "",
    ) -> None:
        super().__init__()
        self.root = root
        self.backups = backups
        self.report = report
        self.prepare = prepare
        self.model = model
        self.status: list[str] = []
        self.notes: list[str] = []
        self.outcome = Outcome()
        self._progress = Progress()
        self._progress_at = time.monotonic()
        self._failed = ""

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        yield Static(Text(f" sorto fsck — {self.root}"), id="title")
        yield Static(id="summary")
        with Vertical(id="working"):
            with Vertical(id="work-box"):
                who = f"The model {self.model}" if self.model else "The model"
                yield Static(Text(f"{who} is reviewing the tree before anything is proposed"), id="work-head")
                yield ProgressBar(total=1000, show_eta=False, id="work-bar")
                yield Static(id="work-phase")
                yield Static(id="work-time")
        with Horizontal(id="mid"):
            yield ListView(id="changes")
            with VerticalScroll(id="detail"):
                yield Static(id="body")
        yield Static(
            Text.assemble(
                (" y ", "bold #0f1419 on #7ee787"), " write   ", (" n ", "bold #0f1419 on #f85149"), " skip   ",
                (" e ", "bold #0f1419 on #d2a8ff"), " edit   ", (" ↑↓ ", "bold #0f1419 on #8b949e"), " move   ",
                (" q ", "bold #0f1419 on #8b949e"), " quit",
            ),
            id="keys",
        )

    def on_mount(self) -> None:
        if self.report is not None:
            self._show_review(self.report)
            return
        self.set_interval(1.0, self._tick)
        self._tick()
        self.run_worker(self._work, thread=True, exclusive=True)

    # ----------------------------------------------------------------- working

    def _work(self) -> None:
        try:
            report = self.prepare(lambda p: self.call_from_thread(self._on_progress, p))
        except Exception as e:  # shown, then q leaves
            self.call_from_thread(self._on_failed, f"{type(e).__name__}: {e}")
            return
        self.call_from_thread(self._show_review, report)

    def _on_progress(self, progress: Progress) -> None:
        self._progress, self._progress_at = progress, time.monotonic()
        self._tick()

    def _on_failed(self, message: str) -> None:
        self._failed = message
        self.query_one("#work-phase", Static).update(Text(f"The review failed: {message}", style="bold #f85149"))
        self.query_one("#work-time", Static).update(Text("q leaves; nothing was written", style="#8b949e"))

    def _fraction(self) -> float:
        """Overall progress: scanning is quick, the categories most of it, the whole-tree question the rest."""
        p = self._progress
        part = p.done / p.total if p.total else 0.0
        if p.phase == "scanning":
            return 0.05 * part
        if p.phase == "thinking":
            return 0.05 + 0.7 * part
        if p.phase == "structure":
            waited = time.monotonic() - self._progress_at
            guess = p.eta_s or 150.0
            return 0.75 + 0.24 * min(waited / guess, 1.0)
        return 1.0

    def _tick(self) -> None:
        if self.report is not None or self._failed:
            return
        p = self._progress
        since = time.monotonic() - self._progress_at
        phase = PHASES.get(p.phase, p.phase)
        if p.phase in ("scanning", "thinking") and p.total:
            phase += f" {min(p.done + 1, p.total)}/{p.total}"
        line = Text(phase, style="bold")
        if p.current:
            line.append(f"  {p.current}", style="#9ecbff")
        self.query_one("#work-phase", Static).update(line)
        left = None if p.eta_s is None else max(0.0, p.eta_s - since)
        self.query_one("#work-time", Static).update(Text.assemble(
            ("elapsed ", "#8b949e"), eta_text(p.elapsed_s + since), ("   about ", "#8b949e"),
            (eta_text(left) if left else "a moment", "bold #d2a8ff"), (" left", "#8b949e"),
        ))
        self.query_one("#work-bar", ProgressBar).update(progress=int(1000 * self._fraction()))

    # ------------------------------------------------------------------ review

    def _show_review(self, report: Report) -> None:
        self.report = report
        self.status = ["pending"] * len(report.changes)
        self.notes = [""] * len(report.changes)
        self.query_one("#working").display = False
        self.query_one("#mid").display = True
        listing = self.query_one("#changes", ListView)
        insights = report.insights
        if insights is not None and insights.proposals:
            listing.append(_Row(Text.assemble(("◆ ", "bold #79c0ff"),
                                              (f"Structure proposals ({len(insights.proposals)})", "bold #79c0ff")),
                                PROPOSALS))
        if insights is not None and (insights.findings or insights.errors):
            listing.append(_Row(Text.assemble(("◆ ", "bold #d2a8ff"),
                                              (f"Model findings ({len(insights.findings)})", "#d2a8ff")), FINDINGS))
        if report.problems:
            listing.append(_Row(Text.assemble(("! ", "bold #d29922"),
                                              (f"Problems to look at ({len(report.problems)})", "#d29922")), PROBLEMS))
        for i in range(len(report.changes)):
            listing.append(_Row(self._label(i), i))
        listing.focus()
        listing.index = 0
        self._show()

    def _label(self, i: int) -> Text:
        """``· 13 Money  JDex.md  (3)`` — the category, the note, and how many things change in it."""
        change = self.report.changes[i]
        mark, colour = MARKS[self.status[i]]
        parts = (change.rel or (change.mkdirs[0] if change.mkdirs else "")).split("/")
        category = parts[1] if len(parts) >= 3 else parts[0]
        file = parts[-1] if change.rel else "folders"
        text = Text.assemble((f"{mark} ", f"bold {colour}"))
        text.append(category, style="bold" if self.status[i] == "pending" else colour)
        text.append(f"  {file}", style="#8b949e")
        if change.old is None and change.rel:
            text.append("  new", style="bold #7ee787")
        text.append(f"  ({len(change.reasons)})", style="#8b949e")
        return text

    def _current(self) -> int | None:
        if self.report is None:
            return None
        row = self.query_one("#changes", ListView).highlighted_child
        return getattr(row, "index", None)

    def _show(self) -> None:
        if self.report is None:
            return
        i = self._current()
        body = self.query_one("#body", Static)
        insights = self.report.insights
        if i == PROPOSALS:
            text = Text("What the model proposes for the structure as a whole. Nothing here is applied: "
                        "sorto never renames or moves folders.\n\n", style="#8b949e")
            for n, p in enumerate(insights.proposals, 1):
                text.append(f"{n}. {p.title}\n", style="bold #79c0ff")
                text.append("What: ", style="bold")
                text.append(f"{p.change}\n")
                text.append("Why: ", style="bold")
                text.append(f"{p.why}\n")
                if p.steps:
                    text.append("Steps:\n", style="bold")
                    for s, step in enumerate(p.steps, 1):
                        text.append(f"  {s}. {step}\n")
                text.append("Effort: ", style="bold")
                text.append(f"{p.effort}\n", style="#d2a8ff")
                for w in p.warnings:
                    text.append("Check: ", style="bold #d29922")
                    text.append(f"{w}\n", style="#d29922")
                text.append("\n")
        elif i == FINDINGS:
            text = Text("What the model found about folders, category by category. Shown only: "
                        "folders are never renamed or moved.\n\n", style="#8b949e")
            for f in insights.findings:
                text.append(f"{f.kind:<10}", style=f"bold {KIND_STYLE.get(f.kind, '#e6edf3')}")
                text.append(f"{f.text}\n")
            for e in insights.errors:
                text.append("error     ", style="bold #f85149")
                text.append(f"{e}\n")
        elif i == PROBLEMS or i is None:
            text = Text("Reported only: these need a human, nothing is written for them.\n\n", style="#8b949e")
            for line in self.report.problems:
                text.append("! ", style="bold #d29922")
                text.append(f"{line}\n")
        else:
            change = self.report.changes[i]
            text = Text()
            for reason in change.reasons:
                text.append("• ", style="#ffa657")
                text.append(f"{reason}\n")
            if self.notes[i]:
                text.append(f"\n{self.notes[i]}\n", style="bold #d29922")
            text.append("\n")
            text.append_text(diff_text(change))
        body.update(text)
        self.query_one("#detail", VerticalScroll).scroll_home(animate=False)
        self.query_one("#summary", Static).update(Text.assemble(
            f"{len(self.report.changes)} change(s): ",
            (f"{self.status.count('pending')} waiting", "#e6edf3"), " · ",
            (f"{self.status.count('written')} written", "#7ee787"), " · ",
            (f"{self.status.count('skipped')} skipped", "#f85149"), " · ",
            (f"{len(insights.proposals) if insights else 0} structure proposal(s)", "#79c0ff"), " · ",
            (f"{len(self.report.problems)} problem(s)", "#d29922"),
        ))

    def _refresh_row(self, i: int) -> None:
        for row in self.query_one("#changes", ListView).children:
            if getattr(row, "index", None) == i:
                row.query_one(Label).update(self._label(i))

    def _next_pending(self) -> None:
        listing = self.query_one("#changes", ListView)
        rows = list(listing.children)
        start = (listing.index or 0) + 1
        for n in list(range(start, len(rows))) + list(range(0, start)):
            i = getattr(rows[n], "index", -1)
            if i >= 0 and self.status[i] == "pending":
                listing.index = n
                return

    # ----------------------------------------------------------------- actions

    def on_list_view_highlighted(self, _event: ListView.Highlighted) -> None:
        self._show()

    def action_go(self, where: int) -> None:
        if self.report is None:
            return
        listing = self.query_one("#changes", ListView)
        listing.index = where if where >= 0 else len(listing.children) - 1

    def action_write(self) -> None:
        i = self._current()
        if i is None or i < 0 or self.status[i] == "written":
            return
        try:
            apply(self.root, self.report.changes[i], self.backups)
            self.status[i], self.notes[i] = "written", ""
            self.outcome.written += 1
        except OSError as e:
            self.status[i], self.notes[i] = "error", f"not written: {e}"
            self.outcome.errors.append(f"{self.report.changes[i].rel}: {e}")
        self._refresh_row(i)
        if self.status[i] == "written":
            self._next_pending()
        self._show()

    def action_skip(self) -> None:
        i = self._current()
        if i is None or i < 0 or self.status[i] != "pending":
            return
        self.status[i] = "skipped"
        self._refresh_row(i)
        self._next_pending()
        self._show()

    def action_edit(self) -> None:
        i = self._current()
        if i is None or i < 0 or self.status[i] != "pending":
            return
        change: Change = self.report.changes[i]
        if not change.rel:
            return  # only folders: nothing to edit
        with self.suspend():
            change.new = edit_text(change.new, Path(change.rel).name)
        self._show()

    def action_quit_fsck(self) -> None:
        self.outcome.declined = sum(1 for s in self.status if s != "written")
        self.exit(self.outcome)
