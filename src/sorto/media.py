"""Photo/video evidence for the local model: named EXIF fields, previews, video frames.

Everything here runs local tools (exiftool, ImageMagick, ffmpeg) and keeps the
results in memory; nothing is written next to the user's files. The previews
only ever go to the loopback LLM endpoint.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from sorto.geo import place_of

# Tags worth showing the model, in display order. -n makes GPS signed decimals.
EXIF_TAGS = (
    "DateTimeOriginal",
    "CreateDate",
    "MediaCreateDate",
    "OffsetTimeOriginal",
    "Make",
    "Model",
    "AndroidManufacturer",
    "AndroidModel",
    "SamsungModel",
    "AndroidVersion",
    "Author",
    "LensModel",
    "Software",
    "GPSLatitude",
    "GPSLongitude",
    "GPSAltitude",
    "GPSCoordinates",
    "City",
    "Sub-location",
    "Province-State",
    "Country",
    "Title",
    "ImageDescription",
    "Description",
    "Caption-Abstract",
    "Keywords",
    "Subject",
    "XPKeywords",
    "XPComment",
    "UserComment",
    "Artist",
    "ImageWidth",
    "ImageHeight",
    "Duration",
)

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tif", ".tiff", ".gif", ".bmp", ".dng", ".avif"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".3gp", ".mts", ".m2ts", ".wmv", ".mpg", ".mpeg"}
RAW_EXT = {".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf"}


@dataclass
class MediaEvidence:
    exif: dict[str, str] = field(default_factory=dict)
    images: list[bytes] = field(default_factory=list)
    kind: str = ""  # image | video | ""
    note: str = ""  # human summary for the TUI


def media_kind(path: Path, mime: str | None) -> str:
    mime = (mime or "").lower()
    suffix = path.suffix.lower()
    if mime.startswith("video/") or suffix in VIDEO_EXT:
        return "video"
    if mime.startswith("image/") or suffix in IMAGE_EXT or suffix in RAW_EXT:
        return "image"
    return ""


def device_of(exif: dict[str, str]) -> str:
    """The camera or phone that made the file ("" when none is recorded).

    Photos carry Make/Model. Phone videos often do not: Android and Samsung
    write their own fields instead.
    """
    device = " ".join(x for x in (exif.get("Make"), exif.get("Model")) if x)
    if device:
        return device
    phone = " ".join(
        x for x in (exif.get("AndroidManufacturer"), exif.get("AndroidModel") or exif.get("SamsungModel")) if x
    )
    if phone:
        return phone
    if exif.get("AndroidVersion"):
        return exif.get("Author") or "Android phone"
    return ""


# 20230815_102030.mp4, VID_20180203_040506.mp4, IMG_20200104_123456.jpg, PXL_20230101_123456789.jpg,
# "2022-03-04 05.06.07.mp4" (camera uploads). Not WhatsApp (VID-20200101-WA0001) or screenshots.
_CAMERA_DATE_NAME = re.compile(
    r"^(?:(?:IMG|VID|PXL|MVIMG|PANO|BURST|DSC)[_-]?)?"
    r"((?:19|20)\d{2})[-_.]?(\d{2})[-_.]?(\d{2})[-_ T.]?\d{2}[-_.]?\d{2}[-_.]?\d{2}(?!\d{4})",
    re.IGNORECASE,
)
# Counter names cameras give: IMG_0522.MP4, DSC_0001.JPG, MVI_1234.MOV, GOPR0012.MP4, GRMN1567.MP4.
_CAMERA_COUNTER_NAME = re.compile(
    r"^(?:IMG_|DSC[_FN]?|MVI_|MOV_|SAM_|CIMG|PICT|DJI_|GOPR|G[HXP]\d{2}|GRM[A-Z])\d{4}", re.IGNORECASE
)
MIN_CAPTURE_YEAR = 1990


def _valid_day(year: str, month: str, day: str) -> datetime | None:
    try:
        when = datetime(int(year), int(month), int(day))
    except ValueError:
        return None
    if when.year < MIN_CAPTURE_YEAR or when > datetime.now() + timedelta(days=2):
        return None
    return when


def capture_date(exif: dict[str, str]) -> datetime | None:
    """The day the photo or video was taken, from its own metadata."""
    for tag in ("DateTimeOriginal", "CreateDate", "MediaCreateDate"):
        m = re.match(r"(\d{4})[:-](\d{2})[:-](\d{2})", exif.get(tag) or "")
        when = _valid_day(*m.groups()) if m else None
        if when:
            return when
    return None


def name_date(filename: str) -> datetime | None:
    """The day in a camera-style file name (20230815_102030.mp4)."""
    m = _CAMERA_DATE_NAME.match(filename)
    return _valid_day(*m.groups()) if m else None


def camera_signs(kind: str, exif: dict[str, str], filename: str) -> str:
    """Evidence that a camera or phone made this photo or video ("" when there is none).

    It does not say whose camera: a picture inside downloaded software has a
    camera too. Whether the file is the user's own is for the model to say.
    """
    if kind not in ("image", "video"):
        return ""
    signs = []
    device = device_of(exif)
    if device:
        signs.append(f"shot with {device}")
    if exif.get("GPSLatitude") and exif.get("GPSLongitude"):
        signs.append("has a GPS position")
    if name_date(filename) or (_CAMERA_COUNTER_NAME.match(filename) and capture_date(exif)):
        signs.append("file name as cameras and phones write it")
    return ", ".join(signs)


def date_subfolder(kind: str, exif: dict[str, str], *, own: bool, mode: str = "own", filename: str = "") -> str:
    """``YYYY/MM`` for a photo or video that is filed by date, else "".

    The date is never the model's: it is the capture date in the metadata,
    else the date in a camera-style file name. Without either there is no
    date folder and the file goes to the ID itself. The file's modification
    time is not used: for a copied, exported or downloaded picture it is the
    day of the copy.
    """
    if mode == "off" or kind not in ("image", "video") or not (own or mode == "all"):
        return ""
    when = capture_date(exif) or name_date(filename)
    return f"{when:%Y/%m}" if when else ""


def _run(cmd: list[str], timeout: float) -> bytes:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return b""
    return proc.stdout if proc.returncode == 0 else b""


def read_exif(path: Path) -> dict[str, str]:
    """Named EXIF/XMP/QuickTime fields (photos and videos) via exiftool."""
    return read_exif_many([path]).get(str(path), {})


def read_exif_many(paths: list[Path]) -> dict[str, dict[str, str]]:
    """EXIF of several files with one exiftool run (a Perl start per file is slow)."""
    if not paths or not shutil.which("exiftool"):
        return {}
    # QuickTimeUTC: video CreateDate is stored in UTC; show it in local time like photos.
    out = _run(
        ["exiftool", "-j", "-n", "-q", "-api", "QuickTimeUTC=1", *(f"-{t}" for t in EXIF_TAGS), "--",
         *(str(p) for p in paths)],
        timeout=15 + 2 * len(paths),
    )
    try:
        items = json.loads(out.decode("utf-8", errors="replace"))
    except (ValueError, TypeError):
        return {}
    return {str(d.get("SourceFile")): _clean_exif(d) for d in items if isinstance(d, dict)}


def _clean_exif(data: dict) -> dict[str, str]:
    result: dict[str, str] = {}
    for tag in EXIF_TAGS:
        value = data.get(tag)
        if value in (None, "", 0, "0000:00:00 00:00:00"):
            continue
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        if isinstance(value, float):
            value = f"{value:.6f}".rstrip("0").rstrip(".")
        result[tag] = str(value)[:300]
    if "GPSCoordinates" in result and "GPSLatitude" not in result:
        parts = result["GPSCoordinates"].replace(",", " ").split()
        if len(parts) >= 2:
            result["GPSLatitude"], result["GPSLongitude"] = parts[0], parts[1]
    if result.get("GPSLatitude") and result.get("GPSLongitude"):
        try:
            if float(result["GPSLatitude"]) == 0 and float(result["GPSLongitude"]) == 0:
                result.pop("GPSLatitude")
                result.pop("GPSLongitude")
        except ValueError:
            pass
    return result


def image_preview(path: Path, max_px: int) -> bytes:
    """Downscaled, EXIF-rotated JPEG of the first frame/page (ImageMagick)."""
    binary = shutil.which("magick") or shutil.which("convert")
    if not binary:
        return b""
    return _run(
        [binary, f"{path}[0]", "-auto-orient", "-thumbnail", f"{max_px}x{max_px}>", "-strip",
         "-quality", "80", "jpg:-"],
        timeout=30,
    )


def video_duration(path: Path) -> float:
    if not shutil.which("ffprobe"):
        return 0.0
    out = _run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        timeout=15,
    )
    try:
        return float(out.decode().strip())
    except ValueError:
        return 0.0


def video_frames(path: Path, count: int, max_px: int) -> list[bytes]:
    """*count* JPEG frames spread across the video (ffmpeg, in memory)."""
    if count <= 0 or not shutil.which("ffmpeg"):
        return []
    duration = video_duration(path)
    if duration <= 0:
        points = [0.0]
    elif count == 1:
        points = [duration * 0.5]
    else:
        points = [duration * (0.1 + 0.8 * i / (count - 1)) for i in range(count)]
    frames = []
    for t in points:
        jpg = _run(
            ["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
             "-vf", f"scale='min({max_px},iw)':-2", "-q:v", "5", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
            timeout=30,
        )
        if jpg:
            frames.append(jpg)
    return frames


def collect_media(path: Path, mime: str | None, *, vision: bool, preview_px: int, frames: int) -> MediaEvidence:
    kind = media_kind(path, mime)
    if not kind:
        return MediaEvidence()
    ev = MediaEvidence(kind=kind, exif=read_exif(path))
    if vision:
        if kind == "image":
            jpg = image_preview(path, preview_px)
            ev.images = [jpg] if jpg else []
        else:
            ev.images = video_frames(path, frames, max(256, preview_px * 2 // 3))
    ev.note = media_note(ev)
    return ev


def media_note(ev: MediaEvidence) -> str:
    """Short line for the TUI: when, where, what device, what the model saw."""
    parts = []
    when = ev.exif.get("DateTimeOriginal") or ev.exif.get("CreateDate") or ev.exif.get("MediaCreateDate")
    if when:
        parts.append(f"taken {when.replace(':', '-', 2)}")
    lat, lon = ev.exif.get("GPSLatitude"), ev.exif.get("GPSLongitude")
    if lat and lon:
        place = place_of(lat, lon)
        try:
            parts.append(f"GPS {float(lat):.4f},{float(lon):.4f}" + (f" = {place}" if place else ""))
        except ValueError:
            parts.append(f"GPS {lat},{lon}")
    device = device_of(ev.exif)
    if device:
        parts.append(device)
    if ev.images:
        parts.append(
            "model viewed the image" if ev.kind == "image" else f"model viewed {len(ev.images)} video frames"
        )
    elif ev.kind:
        parts.append("no preview")
    return ", ".join(parts)
