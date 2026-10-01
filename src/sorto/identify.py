from __future__ import annotations

import email
import email.policy
import html
import re
import shutil
import subprocess
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from sorto.config import SortoConfig
from sorto.geo import place_of
from sorto.junk import classify_junk
from sorto.media import camera_signs, collect_media, picture_preview
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
    ("ffprobe", ["ffprobe", "-v", "error", "-show_entries", "format=format_name,duration:format_tags=creation_time,location,com.apple.quicktime.location.ISO6709:stream=codec_name,width,height", "-of", "default=nw=1"]),
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
        want.add("identify")  # named EXIF comes from sorto.media
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


CONTENT_CHARS = 3000
OFFICE_XML = {
    ".docx": "word/document.xml",
    ".odt": "content.xml",
    ".ods": "content.xml",
    ".odp": "content.xml",
    ".pptx": "ppt/slides/slide1.xml",
    ".xlsx": "xl/sharedStrings.xml",
}


def _squash(text: str) -> str:
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()[:CONTENT_CHARS]


def extract_text(path: Path, mime: str | None) -> str:
    """Readable text from the first pages of a document, for the analysis.

    Reads only: ``pdftotext`` on pages 1-2, or the main XML part of an
    Office/ODF zip (read in memory, never unpacked to disk).
    """
    suffix = path.suffix.lower()
    if (mime == "application/pdf" or suffix == ".pdf") and shutil.which("pdftotext"):
        try:
            proc = subprocess.run(
                ["pdftotext", "-f", "1", "-l", "2", "-layout", str(path), "-"],
                capture_output=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return _squash(proc.stdout.decode("utf-8", errors="replace"))
    member = OFFICE_XML.get(suffix)
    if member:
        try:
            with zipfile.ZipFile(path) as zf:
                info = zf.getinfo(member)
                if info.file_size > 20 * 1024 * 1024:
                    return ""
                with zf.open(info) as fh:
                    xml = fh.read(2 * 1024 * 1024).decode("utf-8", errors="replace")
        except (OSError, KeyError, zipfile.BadZipFile, RuntimeError):
            return ""
        return _squash(re.sub(r"<[^>]+>", " ", xml))
    return ""


MODEL_3D_EXT = {".3mf", ".stl"}
_3MF_FIELDS = ("Title", "Description", "Designer", "Application")
_3MF_PREVIEWS = ("metadata/plate_1.png", "metadata/thumbnail.png", "metadata/top_1.png", "thumbnail/thumbnail.png")
_3MF_PREVIEW_MAX = 8 * 1024 * 1024


def _zip_head(zf: zipfile.ZipFile, member: str, limit: int) -> str:
    try:
        with zf.open(member) as fh:
            return fh.read(limit).decode("utf-8", errors="replace")
    except (KeyError, OSError, RuntimeError, zipfile.BadZipFile):
        return ""


def model_3d_info(path: Path, *, preview: bool = True) -> tuple[dict[str, str], bytes]:
    """What a 3D model file says about itself: (facts, the preview picture stored in it).

    These files are often named badly (``plate_1.3mf``, ``final2.stl``), so
    the name alone says little. A 3MF is a zip: its title, description and
    part names are read in memory, and so is the preview picture slicers
    store in it. Nothing is unpacked to disk. An STL has only its header.
    """
    suffix = path.suffix.lower()
    facts: dict[str, str] = {}
    if suffix == ".stl":
        try:
            with open(path, "rb") as fh:
                head = fh.read(256)
        except OSError:
            return facts, b""
        line = head.split(b"\n", 1)[0] if head.startswith(b"solid") else head[:80]
        text = re.sub(r"[\x00-\x1f\x7f\s]+", " ", line.decode("ascii", errors="ignore")).strip()
        text = re.sub(r"^solid\b", "", text).strip()
        if sum(c.isalpha() for c in text) >= 3 and text.isprintable():
            facts["header"] = text[:120]
        return facts, b""
    if suffix != ".3mf":
        return facts, b""
    picture = b""
    try:
        with zipfile.ZipFile(path) as zf:
            members = {n.lower(): n for n in zf.namelist()}
            head = _zip_head(zf, members.get("3d/3dmodel.model", ""), 256 * 1024) if "3d/3dmodel.model" in members else ""
            for name, value in re.findall(r'<metadata\s+name="([^"]+)"[^>]*>([^<]*)</metadata>', head):
                for _ in range(3):  # slicers escape the description more than once
                    value = html.unescape(value)
                value = _squash(re.sub(r"<[^>]+>", " ", value).replace("\xa0", " "))
                if name in _3MF_FIELDS and value:
                    facts[name.lower()] = value[:300]
            parts = re.findall(r'<object\b[^>]*\bname="([^"]+)"', head)
            settings = members.get("metadata/model_settings.config")
            if settings:
                parts += re.findall(r'<metadata\s+key="name"\s+value="([^"]+)"', _zip_head(zf, settings, 256 * 1024))
            seen: list[str] = []
            for part in parts:
                part = html.unescape(part).strip()
                if part and part not in seen:
                    seen.append(part)
            if seen:
                facts["parts"] = ", ".join(seen[:12])[:400]
            if preview:
                wanted = [members[n] for n in _3MF_PREVIEWS if n in members]
                wanted += [n for low, n in members.items()
                           if low.startswith(("metadata/", "thumbnail/")) and low.endswith((".png", ".jpg", ".jpeg"))]
                for member in wanted:
                    if zf.getinfo(member).file_size <= _3MF_PREVIEW_MAX:
                        with zf.open(member) as fh:
                            picture = fh.read(_3MF_PREVIEW_MAX)
                        break
    except (OSError, KeyError, zipfile.BadZipFile, RuntimeError):
        return facts, b""
    return facts, picture


EMAIL_HEADERS = ("From", "To", "Cc", "Subject", "Date", "List-Id")
EMAIL_MAX_BYTES = 512 * 1024
_HEADER_LINE_RE = re.compile(rb"^[A-Za-z][A-Za-z0-9-]{1,40}:(?:[ \t]|\r?$)")
_MAIL_KEYS = (b"from:", b"subject:", b"date:", b"message-id:", b"received:", b"return-path:", b"delivered-to:")


def looks_like_email(path: Path, mime: str | None) -> bool:
    """.eml, message/rfc822, or a Maildir-style file that starts with mail headers."""
    if path.suffix.lower() == ".eml" or (mime or "").lower() == "message/rfc822":
        return True
    try:
        with open(path, "rb") as f:
            head = f.read(8192)
    except OSError:
        return False
    lines = head.split(b"\n")
    if not lines or not _HEADER_LINE_RE.match(lines[0]):
        return False
    keys = {line.split(b":", 1)[0].strip().lower() + b":" for line in lines if _HEADER_LINE_RE.match(line)}
    # Long Received: chains can push From: past the first few KB.
    return len(keys & set(_MAIL_KEYS)) >= 3


def read_email(path: Path) -> tuple[str, str]:
    """(header summary, body text) of one RFC 822 message; decoded, in memory."""
    try:
        with open(path, "rb") as f:
            raw = f.read(EMAIL_MAX_BYTES)
        msg = email.message_from_bytes(raw, policy=email.policy.default)
    except (OSError, ValueError, LookupError):
        return "", ""
    headers = []
    for name in EMAIL_HEADERS:
        try:
            value = msg.get(name)
        except (ValueError, LookupError, IndexError):
            continue
        if value:
            headers.append(f"{name}: {' '.join(str(value).split())[:300]}")
    body = ""
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is not None:
            body = str(part.get_content())
            if part.get_content_subtype() == "html":
                body = re.sub(r"<(script|style)\b.*?</\1>", " ", body, flags=re.S | re.I)
                body = re.sub(r"<[^>]+>", " ", body)
    except (KeyError, LookupError, ValueError, AttributeError):
        body = ""
    return "\n".join(headers), _squash(body)


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
    size: int | None = None,
    mtime_ns: int | None = None,
    light: bool = False,
) -> AnalysisPacket:
    """Everything sorto knows about one file without asking the model.

    ``light`` (a file that moves with its folder): type, hash and EXIF for the
    outlier check, but no previews or document text, which only the model uses.
    """
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
    if light:
        pass
    elif size <= max_body and looks_like_email(path, mime):
        headers, body = read_email(path)
        if headers:
            extra["email"] = headers
            if body:
                extra["document_text"] = body
            text_preview = ""
            hex_preview = ""
    elif size <= max_body:
        content = extract_text(path, mime)
        if content:
            extra["document_text"] = content
            text_preview = ""
    junk = classify_junk(src_rel, path.name)
    model_picture = b""
    if path.suffix.lower() in MODEL_3D_EXT and not light and size <= max_body:
        facts, raw_picture = model_3d_info(path, preview=cfg.vision and junk is None)
        if facts:
            extra["model_3d"] = "; ".join(f"{key}: {value}" for key, value in facts.items())
        if raw_picture:
            model_picture = picture_preview(raw_picture, cfg.preview_px)
        hex_preview = hex_preview[:64]
        text_preview = ""
    media = collect_media(
        path,
        mime,
        vision=cfg.vision and junk is None and not light,
        preview_px=cfg.preview_px,
        frames=cfg.video_frames,
    )
    if media.kind:
        # Raw bytes of a photo/video tell the model nothing; EXIF and pixels do.
        text_preview = ""
        hex_preview = hex_preview[:64]
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
        meaningless_name=is_meaningless_name(filename),
        keep_extension=not cfg.allow_extension_fix,
        is_junk=junk is not None,
        junk_reason=junk.reason if junk else None,
        exif=media.exif,
        images=media.images or ([model_picture] if model_picture else []),
        media_kind=media.kind,
        media_note=media.note or ("model viewed the preview picture stored in the 3D file" if model_picture else ""),
        camera_signs=camera_signs(media.kind, media.exif, filename),
        gps_place=place_of(media.exif.get("GPSLatitude"), media.exif.get("GPSLongitude")),
    )
