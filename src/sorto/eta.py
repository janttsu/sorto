from __future__ import annotations

from collections import deque
from statistics import fmean


class EtaTracker:
    """Rolling average of per-file stage times for ETA."""

    def __init__(self, window: int = 40, min_samples: int = 5):
        self.window = window
        self.min_samples = min_samples
        self.totals: deque[float] = deque(maxlen=window)
        self.identify: deque[float] = deque(maxlen=window)
        self.analyze: deque[float] = deque(maxlen=window)
        self.move: deque[float] = deque(maxlen=window)

    def add(self, identify_s: float, analyze_s: float, move_s: float) -> None:
        identify_s = max(0.0, float(identify_s))
        analyze_s = max(0.0, float(analyze_s))
        move_s = max(0.0, float(move_s))
        self.identify.append(identify_s)
        self.analyze.append(analyze_s)
        self.move.append(move_s)
        self.totals.append(identify_s + analyze_s + move_s)

    @property
    def sample_count(self) -> int:
        return len(self.totals)

    def avg_seconds(self) -> float | None:
        if len(self.totals) < self.min_samples:
            return None
        avg = fmean(self.totals)
        if self.analyze:
            a = fmean(self.analyze)
            if a >= 0.5 * avg:
                ident = fmean(self.identify) if self.identify else 0.0
                move = fmean(self.move) if self.move else 0.0
                avg = a * 1.05 + ident + move
        return avg

    def eta_seconds(self, pending: int) -> float | None:
        avg = self.avg_seconds()
        if avg is None:
            return None
        return max(0.0, pending) * avg
