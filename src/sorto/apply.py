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
    safe_move,
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


def apply_delete_duplicate(
    *,
    root: Path,
    src_rel: str,
    original_rel: str,
    dry_run: bool,
) -> None:
    """Unlink a confirmed duplicate. Never touches the original.

    Refuses if *src* is inside a git working tree, is not a regular file,
    is the same inode as the original, or the original is missing.
    """
    src = resolve_under_root(root, root / posix_rel(src_rel))
    orig = resolve_under_root(root, root / posix_rel(original_rel))
    repo = git_workdir(src)
    if repo is not None:
        raise UnsafePathError(f"refusing to delete inside git repository {repo}")
    if src.is_symlink() or not src.is_file():
        raise UnsafePathError(f"refusing to delete non-regular file: {src}")
    if not orig.is_file():
        raise UnsafePathError(f"original missing; not deleting duplicate: {orig}")
    try:
        if src.samefile(orig):
            raise UnsafePathError("refusing to delete the original file")
    except OSError as e:
        raise UnsafePathError(f"could not compare duplicate to original: {e}") from e
    if dry_run:
        return
    os.unlink(src)


def apply_delete_junk(
    *,
    root: Path,
    src_rel: str,
    dry_run: bool,
) -> None:
    """Unlink cache/temp/junk. Refuses git working trees and non-regular files."""
    src = resolve_under_root(root, root / posix_rel(src_rel))
    repo = git_workdir(src)
    if repo is not None:
        raise UnsafePathError(f"refusing to delete inside git repository {repo}")
    if src.is_symlink() or not src.is_file():
        raise UnsafePathError(f"refusing to delete non-regular file: {src}")
    if dry_run:
        return
    os.unlink(src)
