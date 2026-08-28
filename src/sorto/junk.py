"""Heuristic detection of cache, temp, and backup junk (including Android)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from sorto.util import posix_rel

JUNK_FOLDER = "_cache_temp_and_junk"

JUNK_DIR_NAMES = frozenset(
    {
        "cache",
        ".cache",
        "caches",
        "code_cache",
        "code-cache",
        "codecache",
        "tmp",
        "temp",
        ".tmp",
        ".temp",
        "temporary",
        "tempfiles",
        "tmpfiles",
        "thumbnails",
        ".thumbnails",
        "albumthumbs",
        ".albumthumbs",
        "imagecache",
        "image_cache",
        "image-cache",
        "picasso-cache",
        "glide",
        "glide-disk-cache",
        ".photocache",
        "photo_cache",
        "video_cache",
        "videocache",
        ".trash",
        ".trashes",
        "recycle.bin",
        "$recycle.bin",
        "lost.dir",
        "__macosx",
        ".spotlight-v100",
        ".fseventsd",
        ".temporaryitems",
        "tmpcache",
        ".nomedia",
    }
)

JUNK_FILE_NAMES = frozenset(
    {
        ".ds_store",
        "thumbs.db",
        "ehthumbs.db",
        "desktop.ini",
        ".nomedia",
        "albumthumbs",
        "icon\r",
    }
)

JUNK_EXTENSIONS = frozenset(
    {
        ".tmp",
        ".temp",
        ".swp",
        ".swo",
        ".crdownload",
        ".part",
        ".partial",
        ".thumbdata3",
        ".thumbdata4",
        ".thumbdata5",
    }
)

_TRASHED_ANDROID = re.compile(r"^\.trashed-\d+-", re.IGNORECASE)
_THUMBDATA = re.compile(r"^\.thumbdata", re.IGNORECASE)
_APPLEDOUBLE = re.compile(r"^\._.")
_OFFICE_LOCK = re.compile(r"^~\$")
_BACKUP_TILDE = re.compile(r".~$")


@dataclass(frozen=True)
class JunkHit:
    reason: str


def _parts(rel: str) -> list[str]:
    return [p.lower() for p in posix_rel(rel).split("/") if p]


def classify_junk(src_rel: str, filename: str | None = None) -> JunkHit | None:
    """Return a JunkHit if this path looks like cache/temp/backup rubbish."""
    rel = posix_rel(src_rel)
    name = filename or Path(rel).name
    name_l = name.lower()
    parts = _parts(rel)

    if name_l in JUNK_FILE_NAMES:
        return JunkHit(f"known junk filename {name}")
    ext = Path(name).suffix.lower()
    if ext in JUNK_EXTENSIONS:
        return JunkHit(f"temp/cache extension {ext}")
    if _TRASHED_ANDROID.match(name):
        return JunkHit("Android .trashed-* recycle leftover")
    if _THUMBDATA.match(name):
        return JunkHit("Android gallery thumbdata")
    if _APPLEDOUBLE.match(name):
        return JunkHit("AppleDouble sidecar")
    if _OFFICE_LOCK.match(name):
        return JunkHit("Office lock/temp file")
    if name.endswith("~") or _BACKUP_TILDE.match(name):
        return JunkHit("editor backup (*~)")

    for part in parts[:-1]:
        if part in JUNK_DIR_NAMES:
            return JunkHit(f"under junk directory {part}/")

    # Android app cache: Android/data/<pkg>/cache|code_cache|...
    if "android" in parts:
        for i, part in enumerate(parts):
            if part in {"cache", "code_cache", "code-cache"} and i >= 1:
                return JunkHit("Android app cache directory")

    return None
