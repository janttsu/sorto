"""Time-to-finish estimate from the pace measured so far.

The single sort worker is the bottleneck (one file at a time through the local
model), so the time a file spends there *is* the throughput. Photos, videos
and documents cost very different amounts of model time, so the pace is kept
per kind and applied to what is still waiting of each kind.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from statistics import fmean

from sorto.media import IMAGE_EXT, RAW_EXT, VIDEO_EXT

DOC_EXT = {
    ".pdf", ".doc", ".docx", ".odt", ".ods", ".odp", ".xls", ".xlsx", ".ppt", ".pptx", ".rtf",
    ".txt", ".md", ".csv", ".eml", ".html", ".htm", ".json", ".xml",
}
KINDS = ("image", "video", "document", "other")


def file_kind(rel: str) -> str:
    ext = PurePosixPath(rel).suffix.lower()
    if ext in IMAGE_EXT or ext in RAW_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in DOC_EXT:
        return "document"
    return "other"


@dataclass
class Estimate:
    seconds: float | None  # None until there is a pace to go on
    per_file: float | None  # average seconds per file over everything measured
    samples: int
    pending: int


class EtaTracker:
    """Rolling per-kind averages of the seconds each file took to sort."""

    def __init__(self, window: int = 60, min_samples: int = 2):
        self.window = window
        self.min_samples = min_samples
        self._by_kind: dict[str, deque[float]] = {k: deque(maxlen=window) for k in KINDS}
        self._all: deque[float] = deque(maxlen=window)
        self._first_skipped = False
        self.sample_count = 0

    def add(self, seconds: float, kind: str = "other") -> None:
        seconds = max(0.0, float(seconds))
        self.sample_count += 1
        if not self._first_skipped and seconds > 20.0:
            # The first answer usually includes loading the model; keep it out
            # of the pace so the estimate does not start out hours too long.
            self._first_skipped = True
            return
        self._first_skipped = True
        self._by_kind.setdefault(kind, deque(maxlen=self.window)).append(seconds)
        self._all.append(seconds)

    def per_file(self, kind: str | None = None) -> float | None:
        if kind is not None:
            samples = self._by_kind.get(kind) or ()
            if len(samples) >= self.min_samples:
                return fmean(samples)
        return fmean(self._all) if len(self._all) >= self.min_samples else None

    def estimate(self, pending_by_kind: dict[str, int]) -> Estimate:
        pending = sum(pending_by_kind.values())
        overall = self.per_file()
        if overall is None:
            return Estimate(None, None, len(self._all), pending)
        total = 0.0
        for kind, count in pending_by_kind.items():
            total += count * (self.per_file(kind) or overall)
        return Estimate(total, overall, len(self._all), pending)

    def eta_seconds(self, pending: int) -> float | None:
        return self.estimate({"other": pending}).seconds


def human_duration(seconds: float) -> str:
    s = int(round(max(0.0, seconds)))
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m, sec = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {sec:02d}s"
    return f"{sec}s"


def eta_text(snap, now: datetime | None = None) -> str:
    """'~3h 12m left for 812 files at 14.2 s/file …' from a Snapshot."""
    pending = snap.counts.pending
    scanning = snap.scan_still_running
    if not pending:
        return "scanning…" if scanning else "nothing left"
    if snap.eta_s is None or snap.per_file_s is None:
        return f"measuring the pace… ({pending:,} files left)"
    finish = (now or datetime.now()) + timedelta(seconds=snap.eta_s)
    fmt = "%H:%M" if snap.eta_s < 18 * 3600 else "%a %d.%m. %H:%M"
    text = (
        f"~{human_duration(snap.eta_s)} left for {pending:,} files at {snap.per_file_s:.1f} s/file "
        f"(pace of the last {snap.pace_samples} files), done ≈ {finish.strftime(fmt)}"
    )
    if scanning:
        text += " + files the scan has not reached yet"
    return text
