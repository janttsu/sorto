from __future__ import annotations

import threading
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import TextIO

from sorto.models import AnalysisView
from sorto.util import human_size

# What happened to the file, as the last line of its entry.
RESULTS = {
    "moved": "moved to",
    "planned": "would go to",
    "kept": "kept",
    "in_place": "stays",
    "deleted": "deleted",
    "error": "error",
}
# The same outcomes as counted in the last line of the log.
TOTALS = {
    "moved": "moved",
    "planned": "would be moved",
    "kept": "kept",
    "in_place": "already in place",
    "deleted": "deleted",
    "error": "with an error",
}
WIDTH = 13


class RunLog:
    """One plain-text log per run: every file, what the analysis said and where the file went.

    ``progress.jsonl`` is the durable record sorto itself relies on; this
    file is for reading. A new one is started for every run, named after
    the time the run began.
    """

    def __init__(self, directory: Path, header: dict[str, str]):
        directory.mkdir(parents=True, exist_ok=True)
        started = datetime.now()
        self.path, self._fp = _open_new(directory, f"run-{started:%Y-%m-%d_%H-%M-%S}")
        self._lock = threading.Lock()
        self._counts: Counter[str] = Counter()
        self._closed = False
        lines = [f"sorto run started {started:%Y-%m-%d %H:%M:%S}"]
        lines += [_field(key, value, indent="") for key, value in header.items() if value]
        self._write("\n".join(lines) + "\n")

    def file(self, view: AnalysisView, model_reason: str = "") -> None:
        """A file's entry: its name, the analysis, and the outcome."""
        about = ", ".join(x for x in (human_size(view.size), view.mime) if x)
        lines = [f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {view.src_rel}  ({about})"]
        if view.current_location:
            lines.append(_field("was in", view.current_location))
        if view.media_note:
            lines.append(_field("media", view.media_note))
        chosen = f"{view.jd_id} {view.jd_name}".strip()
        if view.label or chosen:
            analysis = f"{view.label or 'file'}, confidence {view.confidence:.2f}"
            lines.append(_field("analysis", f"{analysis} → {chosen}" if chosen else analysis))
        if view.summary:
            lines.append(_field("", view.summary))
        went = view.outcome in ("moved", "planned") and bool(view.dest_rel)
        if model_reason and (went or model_reason != view.reason):
            lines.append(_field("why", model_reason))
        if view.rule:
            lines.append(_field("rule", view.rule))
        if view.folder:
            lines.append(_field("folder", view.folder))
        if view.new_id:
            lines.append(_field("new ID", view.new_id))
        result = RESULTS.get(view.outcome, view.outcome or "done")
        lines.append(_field(result, view.dest_rel if went else (view.reason or view.dest_rel)))
        with self._lock:
            self._counts[view.outcome or "done"] += 1
        self._write("\n" + "\n".join(lines) + "\n")

    def note(self, title: str, text: str) -> None:
        """Something that is not one file's outcome: a new ID, a file that could not be read."""
        if title == "error":
            with self._lock:
                self._counts["error"] += 1
        self._write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] {title}: {text}\n")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            counts = dict(self._counts)
        order = [*TOTALS, *sorted(set(counts) - set(TOTALS))]
        parts = [f"{counts[k]} {TOTALS.get(k, k)}" for k in order if counts.get(k)]
        self._write(f"\nsorto run ended {datetime.now():%Y-%m-%d %H:%M:%S}: {', '.join(parts) or 'no files handled'}\n")
        with self._lock:
            self._closed = True
            self._fp.close()

    def _write(self, text: str) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._fp.write(text)
                self._fp.flush()
            except OSError:
                pass  # a full disk must not stop the sorting; progress.jsonl is the record that counts


def _field(name: str, value: str, indent: str = "    ") -> str:
    label = f"{name}:" if name else ""
    return f"{indent}{label:<{WIDTH}}{' '.join(str(value).split())}"


def _open_new(directory: Path, stem: str) -> tuple[Path, TextIO]:
    """Never append to another run's log: a second run in the same second gets ``_2`` (sorts after)."""
    n = 1
    while True:
        path = directory / (f"{stem}.log" if n == 1 else f"{stem}_{n}.log")
        try:
            return path, open(path, "x", encoding="utf-8")
        except FileExistsError:
            n += 1
