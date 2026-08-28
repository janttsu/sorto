from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from sorto import STATE_DIR_NAME
from sorto.util import file_identity, glob_match, is_under_root, posix_rel, should_include


def _prune_dirnames(dirpath: Path, dirnames: list[str], exclude: list[str], root: Path) -> None:
    keep: list[str] = []
    for name in dirnames:
        if name == STATE_DIR_NAME:
            continue
        child = dirpath / name
        try:
            rel = posix_rel(str(child.relative_to(root)))
        except ValueError:
            continue
        skipped = False
        for pat in exclude:
            # If a dir is fully covered by an exclude, prune the walk.
            if glob_match(rel, pat) or glob_match(rel + "/**", pat) or glob_match(rel + "/", pat):
                skipped = True
                break
            # Common prefix excludes like **/.git/**
            if f"{name}" in {".git", ".svn", ".hg", ".Trash", "node_modules", ".venv", "venv"}:
                if any(name in p for p in exclude):
                    skipped = True
                    break
        if not skipped:
            keep.append(name)
    dirnames[:] = keep


def iter_regular_files(
    root: Path,
    *,
    include: list[str],
    exclude: list[str],
) -> Iterator[tuple[Path, str]]:
    """Yield (abs_path, rel_posix) for regular files under root."""
    root_r = root.resolve()
    for dirpath_s, dirnames, filenames in os.walk(root_r, followlinks=False):
        dirpath = Path(dirpath_s)
        try:
            resolved = dirpath.resolve()
        except OSError:
            dirnames[:] = []
            continue
        if STATE_DIR_NAME in resolved.parts:
            dirnames[:] = []
            continue
        if not is_under_root(root_r, resolved):
            dirnames[:] = []
            continue
        _prune_dirnames(dirpath, dirnames, exclude, root_r)
        for name in filenames:
            path = dirpath / name
            try:
                rel = posix_rel(str(path.relative_to(root_r)))
            except ValueError:
                continue
            if not should_include(rel, include, exclude):
                continue
            try:
                if path.is_symlink():
                    target = path.resolve()
                    if not is_under_root(root_r, target):
                        continue
                    # Regular-file investigation only; skip all symlinks.
                    continue
                if not path.is_file():
                    continue
            except OSError:
                continue
            yield path, rel


def discover_batch(
    root: Path,
    include: list[str],
    exclude: list[str],
) -> Iterator[tuple[Path, str, int, int, int | None, int | None]]:
    for path, rel in iter_regular_files(root, include=include, exclude=exclude):
        try:
            dev, ino, size, mtime_ns = file_identity(path)
        except OSError:
            continue
        yield path, rel, size, mtime_ns, dev, ino
