from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from sorto.llm import _strip_images, user_content
from sorto.media import collect_media, media_kind, read_exif

needs = pytest.mark.skipif(
    not all(shutil.which(b) for b in ("exiftool", "magick", "ffmpeg", "ffprobe")),
    reason="exiftool, ImageMagick and ffmpeg are needed",
)


def _photo(path: Path) -> Path:
    subprocess.run(["magick", "-size", "320x200", "gradient:skyblue-seagreen", str(path)], check=True)
    subprocess.run(
        ["exiftool", "-q", "-overwrite_original", "-DateTimeOriginal=2024:07:14 12:30:00",
         "-Make=Google", "-Model=Pixel 8", "-GPSLatitude=41.9028", "-GPSLatitudeRef=N",
         "-GPSLongitude=12.4964", "-GPSLongitudeRef=E", str(path)],
        check=True,
    )
    return path


@needs
def test_named_exif_and_photo_preview(tmp_path: Path) -> None:
    photo = _photo(tmp_path / "20240714_123000.jpg")
    exif = read_exif(photo)
    assert exif["DateTimeOriginal"] == "2024:07:14 12:30:00"
    assert exif["Model"] == "Pixel 8"
    assert float(exif["GPSLatitude"]) == pytest.approx(41.9028)
    assert float(exif["GPSLongitude"]) == pytest.approx(12.4964)
    ev = collect_media(photo, "image/jpeg", vision=True, preview_px=128, frames=3)
    assert ev.kind == "image" and len(ev.images) == 1
    assert ev.images[0][:2] == b"\xff\xd8"  # JPEG
    assert "GPS 41.9028,12.4964" in ev.note and "viewed the image" in ev.note
    assert list(tmp_path.iterdir()) == [photo]  # nothing written beside the file


@needs
def test_video_frames(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=3",
         "-metadata", "creation_time=2024-07-14T12:30:00Z", str(video)],
        check=True,
    )
    ev = collect_media(video, "video/mp4", vision=True, preview_px=256, frames=3)
    assert ev.kind == "video" and len(ev.images) == 3
    assert all(f[:2] == b"\xff\xd8" for f in ev.images)
    assert "3 video frames" in ev.note
    assert collect_media(video, "video/mp4", vision=False, preview_px=256, frames=3).images == []


def test_media_kind() -> None:
    assert media_kind(Path("a.HEIC"), None) == "image"
    assert media_kind(Path("a.mov"), None) == "video"
    assert media_kind(Path("a.pdf"), "application/pdf") == ""


def test_multimodal_message_and_text_fallback() -> None:
    parts = user_content("describe", [b"\xff\xd8abc"])
    assert parts[0] == {"type": "text", "text": "describe"}
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert user_content("plain", []) == "plain"
    stripped = _strip_images({"messages": [{"role": "user", "content": parts}]})
    assert stripped["messages"][0]["content"] == "describe"
