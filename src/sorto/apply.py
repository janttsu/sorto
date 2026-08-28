from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from sorto.util import posix_rel, safe_move, utc_now_iso


class ProgressLog:
    """Append-only JSONL log with fsync after each completed action."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fp = open(self.path, "a", encoding="utf-8")

    def append(self, record: dict[str, Any]) -> None:
        rec = dict(record)
        rec.setdefault("ts", utc_now_iso())
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with self._lock:
            self._fp.write(line)
            self._fp.flush()
            os.fsync(self._fp.fileno())

    def close(self) -> None:
        with self._lock:
            try:
                self._fp.flush()
                os.fsync(self._fp.fileno())
            except OSError:
                pass
            self._fp.close()


def apply_move(
    *,
    root: Path,
    src_rel: str,
    dest_rel: str,
    dry_run: bool,
) -> tuple[str, bool]:
    """Move src_rel to dest_rel under root.

    Returns (actual_dest_rel, moved).
    """
    src = (root / posix_rel(src_rel)).resolve()
    dest = root / posix_rel(dest_rel)
    if dry_run:
        return posix_rel(dest_rel), False
    try:
        if src.exists() and dest.exists() and src.samefile(dest):
            return posix_rel(str(src.relative_to(root.resolve()))), False
    except OSError:
        pass
    moved = safe_move(src, dest, root)
    actual = posix_rel(str(moved.relative_to(root.resolve())))
    return actual, True
