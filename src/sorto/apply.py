from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from sorto.util import (
    UnsafePathError,
    git_workdir,
    posix_rel,
    resolve_under_root,
    safe_move_between,
    utc_now_iso,
)


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
    source: Path,
    target: Path,
    src_rel: str,
    dest_rel: str,
    dry_run: bool,
) -> tuple[str, bool]:
    """Move source/src_rel to target/dest_rel. Returns (actual_dest_rel, moved)."""
    if dry_run:
        return posix_rel(dest_rel), False
    moved = safe_move_between(source / posix_rel(src_rel), source, dest_rel, target)
    return posix_rel(str(moved.relative_to(Path(target).resolve()))), True


def _deletable(root: Path, src_rel: str) -> Path:
    src = resolve_under_root(root, root / posix_rel(src_rel))
    repo = git_workdir(src)
    if repo is not None:
        raise UnsafePathError(f"refusing to delete inside git repository {repo}")
    if src.is_symlink() or not src.is_file():
        raise UnsafePathError(f"refusing to delete non-regular file: {src}")
    return src


def apply_delete_duplicate(
    *,
    root: Path,
    src_rel: str,
    original: Path,
    dry_run: bool,
) -> None:
    """Unlink a confirmed duplicate. Never touches the original.

    Refuses inside a git working tree, for non-regular files, when the
    original is missing, or when both paths are the same inode.
    """
    src = _deletable(root, src_rel)
    if not original.is_file():
        raise UnsafePathError(f"original missing; not deleting duplicate: {original}")
    try:
        if src.samefile(original):
            raise UnsafePathError("refusing to delete the original file")
    except OSError as e:
        raise UnsafePathError(f"could not compare duplicate to original: {e}") from e
    if not dry_run:
        os.unlink(src)


def apply_delete_junk(*, root: Path, src_rel: str, dry_run: bool) -> None:
    """Unlink cache/temp/junk. Refuses git working trees and non-regular files."""
    src = _deletable(root, src_rel)
    if not dry_run:
        os.unlink(src)
