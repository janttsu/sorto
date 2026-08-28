from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from sorto.config import SortoConfig
from sorto.models import AnalysisPacket
from sorto.util import (
    is_meaningless_name,
    read_preview,
    sampled_hash,
    sha256_file,
)

EXT_MIME = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".xml": "application/xml",
    ".html": "text/html",
    ".htm": "text/html",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".heic": "image/heic",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".mp4": "video/mp4",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".zip": "application/zip",
    ".tar": "application/x-tar",
    ".gz": "application/gzip",
    ".tgz": "application/gzip",
    ".7z": "application/x-7z-compressed",
    ".rar": "application/vnd.rar",
    ".bz2": "application/x-bzip2",
    ".xz": "application/x-xz",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".epub": "application/epub+zip",
    ".mobi": "application/x-mobipocket-ebook",
    ".stl": "model/stl",
    ".obj": "model/obj",
    ".3mf": "model/3mf",
    ".step": "model/step",
    ".stp": "model/step",
    ".eml": "message/rfc822",
    ".mbox": "application/mbox",
    ".py": "text/x-python",
    ".js": "text/javascript",
    ".ts": "text/typescript",
    ".rs": "text/x-rust",
    ".go": "text/x-go",
    ".c": "text/x-c",
    ".h": "text/x-c",
    ".cpp": "text/x-c++",
    ".java": "text/x-java",
    ".sh": "text/x-shellscript",
    ".exe": "application/vnd.microsoft.portable-executable",
    ".dmg": "application/x-apple-diskimage",
    ".deb": "application/vnd.debian.binary-package",
    ".rpm": "application/x-rpm",
    ".iso": "application/x-iso9660-image",
    ".sqlite": "application/vnd.sqlite3",
    ".db": "application/octet-stream",
    ".psd": "image/vnd.adobe.photoshop",
    ".ai": "application/pdf",
    ".fig": "application/octet-stream",
}

TOOLS = (
    ("file", ["file", "-b", "--mime-type"]),
    ("file_desc", ["file", "-b"]),
    ("exiftool", ["exiftool", "-s", "-s", "-s"]),
    ("mediainfo", ["mediainfo", "--Inform=General;%Format% %Duration/String% %Width%x%Height%"]),
    ("ffprobe", ["ffprobe", "-v", "error", "-show_entries", "format=format_name,duration:stream=codec_name,width,height", "-of", "default=nw=1"]),
    ("pdfinfo", ["pdfinfo"]),
    ("identify", ["identify", "-format", "%m %wx%h %[bit-depth]"]),
)


def _run(cmd: list[str], timeout: float = 4.0) -> str | None:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    if not out:
        out = proc.stderr.decode("utf-8", errors="replace").strip()
    if not out:
        return None
    return out[:1500]


def _libmagic(path: Path) -> tuple[str | None, str | None]:
    try:
        import magic  # type: ignore
    except Exception:
        return None, None
    mime = desc = None
    try:
        mime = magic.from_file(str(path), mime=True)
    except Exception:
        mime = None
    try:
        desc = magic.from_file(str(path))
    except Exception:
        desc = None
    return mime, desc


def guess_mime(path: Path) -> tuple[str | None, str | None]:
    mime, desc = _libmagic(path)
    if not mime:
        ext = path.suffix.lower()
        mime = EXT_MIME.get(ext)
    if not desc and shutil.which("file"):
        desc = _run(["file", "-b", str(path)], timeout=3.0)
        if not mime:
            mt = _run(["file", "-b", "--mime-type", str(path)], timeout=3.0)
            if mt and "/" in mt and " " not in mt:
                mime = mt
    return mime, desc


def collect_tool_meta(path: Path, mime: str | None) -> dict[str, str]:
    extra: dict[str, str] = {}
    mime = (mime or "").lower()
    want = set()
    suffix = path.suffix.lower()
    if mime.startswith("image/") or suffix in {".jpg", ".jpeg", ".png", ".tif", ".webp", ".heic"}:
        want.update({"exiftool", "identify"})
    if mime.startswith("video/") or mime.startswith("audio/") or suffix in {".mp4", ".mkv", ".mov", ".mp3", ".wav"}:
        want.update({"mediainfo", "ffprobe"})
    if mime == "application/pdf" or suffix == ".pdf":
        want.add("pdfinfo")
    # Always try file(1) if we have no mime
    for name, prefix in TOOLS:
        if name in {"file", "file_desc"}:
            continue
        if name not in want:
            continue
        bin_name = prefix[0]
        if not shutil.which(bin_name):
            continue
        out = _run([*prefix, str(path)])
        if out:
            extra[name] = out
    return extra


def hash_file(path: Path, size: int, cfg: SortoConfig) -> str | None:
    limit = cfg.hash_max_mb * 1024 * 1024
    try:
        if size <= limit:
            return sha256_file(path)
        return sampled_hash(path, size)
    except OSError:
        return None


def identify_file(
    path: Path,
    src_rel: str,
    cfg: SortoConfig,
    *,
    top_level_folders: list[str],
    size: int | None = None,
    mtime_ns: int | None = None,
) -> AnalysisPacket:
    st = path.stat()
    size = int(size if size is not None else st.st_size)
    mtime_ns = int(mtime_ns if mtime_ns is not None else getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
    mtime_iso = datetime.fromtimestamp(mtime_ns / 1e9, tz=UTC).replace(microsecond=0).isoformat()
    mime, magic = guess_mime(path)
    extra = collect_tool_meta(path, mime)
    max_body = cfg.max_file_mb * 1024 * 1024
    if size <= max_body:
        hex_preview, text_preview = read_preview(path)
    else:
        hex_preview, text_preview = read_preview(path, max_bytes=512, max_lines=8)
        extra["body"] = "omitted (over max-file-mb; metadata only)"
    sha = hash_file(path, size, cfg)
    filename = path.name
    ext = path.suffix
    return AnalysisPacket(
        src_rel=src_rel,
        filename=filename,
        extension=ext,
        size=size,
        mtime_iso=mtime_iso,
        mtime_ns=mtime_ns,
        mime=mime,
        magic=magic,
        type_guess=mime or magic or (ext.lstrip(".") if ext else None),
        hex_preview=hex_preview,
        text_preview=text_preview,
        extra_meta=extra,
        sha256=sha,
        top_level_folders=list(top_level_folders),
        dest_scheme=cfg.dest_scheme,
        meaningless_name=is_meaningless_name(filename),
        keep_extension=not cfg.allow_extension_fix,
    )
