"""Whole folders: decide once for a folder whose files belong together.

A photo album, a trip, a project or a monthly dump of downloads is one thing,
not hundreds. sorto first looks at the folder as a whole (file list, types,
dates, devices, GPS, a few excerpts and pictures) and, when the model says the
files belong together, moves the folder as one unit into an ID, keeping its
name and inner structure. Every file is still checked on its own metadata
(type, EXIF date, GPS, camera) against the folder's profile, and a file that
stands out is left behind and sorted on its own.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sorto.eta import file_kind
from sorto.geo import distance_km, place_of
from sorto.identify import extract_text, looks_like_email, read_email
from sorto.media import (
    camera_signs,
    capture_date,
    device_of,
    image_preview,
    read_exif_many,
    video_frames,
)
from sorto.util import extract_json_object, human_size, kept_whole

MAX_NAMES = 80
# Files that make a folder software: it only works in one piece, so nothing is taken out of it.
CODE_EXT = {
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".cs", ".java", ".kt", ".go", ".rs", ".py", ".pyc", ".rb", ".php",
    ".pl", ".js", ".mjs", ".ts", ".jsx", ".tsx", ".vue", ".qml", ".qss", ".css", ".scss", ".sh", ".bat", ".ps1",
    ".lua", ".swift", ".sql", ".pro", ".pri", ".cmake", ".gradle", ".ui", ".qrc", ".so", ".dll", ".exe", ".o",
    ".a", ".jar", ".class", ".dylib", ".lib", ".obj",
}
CODE_NAMES = {"makefile", "cmakelists.txt", "package.json", "cargo.toml", "setup.py", "configure", "dockerfile"}
SOFTWARE_MIN_FILES = 3
SOFTWARE_MIN_SHARE = 0.10


def is_code_file(name: str) -> bool:
    base = name.rsplit("/", 1)[-1].lower()
    return base in CODE_NAMES or Path(base).suffix in CODE_EXT
EXIF_SAMPLE = 16
IMAGE_SAMPLE = 3
EXCERPTS = 3
EXCERPT_CHARS = 600
SKIP_DIRS = {".git", ".svn", ".hg", "node_modules", ".venv", "venv", "__pycache__"}
DATE_MARGIN = timedelta(days=60)
MIN_GPS_KM = 50.0


@dataclass
class FolderWalk:
    rel: str
    files: list[tuple[str, int, int]] = field(default_factory=list)  # (path inside, size, mtime_ns)
    truncated: bool = False


def walk_folder(source: Path, rel: str, limit: int) -> FolderWalk:
    """Regular files under source/rel (no symlinks, no hidden or VCS folders), up to limit+1."""
    walk = FolderWalk(rel=rel)
    root = source / rel
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            d for d in dirnames
            if not d.startswith(".") and d not in SKIP_DIRS and not kept_whole(Path(dirpath) / d)
        )  # what stays whole is not part of the folder's profile: its files never move with it
        for name in sorted(filenames):
            path = Path(dirpath) / name
            try:
                st = path.lstat()
            except OSError:
                continue
            if not path.is_file() or path.is_symlink() or name.startswith("."):
                continue
            walk.files.append((path.relative_to(root).as_posix(), st.st_size, st.st_mtime_ns))
            if len(walk.files) > limit:
                walk.truncated = True
                return walk
    return walk


@dataclass
class FolderProfile:
    """What the folder's files have in common; each file is checked against it."""

    files: int = 0
    kinds: dict[str, int] = field(default_factory=dict)
    date_min: str = ""  # EXIF capture dates of the sample, YYYY-MM-DD
    date_max: str = ""
    gps_lat: float | None = None
    gps_lon: float | None = None
    gps_radius_km: float = 0.0
    media_sampled: int = 0
    with_device: int = 0  # sampled media that carry a camera Make/Model
    devices: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> FolderProfile:
        try:
            return cls(**json.loads(text or "{}"))
        except (TypeError, ValueError):
            return cls()


@dataclass
class FolderPacket:
    rel: str
    name: str
    files: int
    total_size: int
    subfolders: list[tuple[str, int]]
    kinds: dict[str, int]
    extensions: dict[str, int]
    mtime_first: str
    mtime_last: str
    names: list[str]
    exif_summary: dict[str, Any]
    excerpts: list[tuple[str, str]]
    images: list[bytes] = field(default_factory=list, repr=False)
    profile: FolderProfile = field(default_factory=FolderProfile)
    code_files: int = 0

    @property
    def is_software(self) -> bool:
        """Enough program code that the folder is software, whatever else lies in it."""
        return self.code_files >= max(SOFTWARE_MIN_FILES, SOFTWARE_MIN_SHARE * self.files)

    def to_llm_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "folder": self.rel,
            "files": self.files,
            "total_size": human_size(self.total_size),
            "file_types": self.kinds,
            "extensions": self.extensions,
            "modified": f"{self.mtime_first} … {self.mtime_last}",
            "file_names": self.names,
        }
        if self.subfolders:
            out["subfolders"] = {name: n for name, n in self.subfolders}
        if self.code_files:
            out["program_code_files"] = self.code_files
        if self.exif_summary:
            out["exif_of_a_sample"] = self.exif_summary
        if self.excerpts:
            out["text_excerpts"] = {name: text for name, text in self.excerpts}
        if self.images:
            out["attached_images"] = f"{len(self.images)} pictures from files in the folder"
        return out


def gps_of(exif: dict[str, str]) -> tuple[float, float] | None:
    try:
        return float(exif["GPSLatitude"]), float(exif["GPSLongitude"])
    except (KeyError, ValueError):
        return None


def _spread(items: list[str], n: int) -> list[str]:
    if len(items) <= n:
        return list(items)
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def build_packet(source: Path, walk: FolderWalk, *, vision: bool, preview_px: int) -> FolderPacket:
    root = source / walk.rel
    names = [f for f, _, _ in walk.files]
    kinds: dict[str, int] = {}
    exts: dict[str, int] = {}
    for f in names:
        kinds[file_kind(f)] = kinds.get(file_kind(f), 0) + 1
        ext = Path(f).suffix.lower() or "(none)"
        exts[ext] = exts.get(ext, 0) + 1
    subs: dict[str, int] = {}
    for f in names:
        if "/" in f:
            top = f.split("/", 1)[0]
            subs[top] = subs.get(top, 0) + 1
    mtimes = sorted(m for _, _, m in walk.files)
    iso = lambda ns: datetime.fromtimestamp(ns / 1e9).strftime("%Y-%m-%d")  # noqa: E731

    media = [f for f in names if file_kind(f) in ("image", "video")]
    sample = _spread(media, EXIF_SAMPLE)
    exif_by_path = read_exif_many([root / f for f in sample])
    exifs = [exif_by_path.get(str(root / f), {}) for f in sample]
    profile = profile_from(len(names), kinds, exifs)
    devices: dict[str, int] = {}
    for e in exifs:
        dev = device_of(e)
        if dev:
            devices[dev] = devices.get(dev, 0) + 1
    exif_summary: dict[str, Any] = {}
    if sample:
        exif_summary["sampled_media"] = len(sample)
        if profile.date_min:
            exif_summary["capture_dates"] = f"{profile.date_min} … {profile.date_max}"
        if devices:
            exif_summary["devices"] = devices
        elif profile.media_sampled:
            exif_summary["devices"] = "none recorded (typical of downloaded or received files)"
        if profile.gps_lat is not None:
            exif_summary["gps_center"] = f"{profile.gps_lat:.4f},{profile.gps_lon:.4f}"
            place = place_of(profile.gps_lat, profile.gps_lon)
            if place:
                exif_summary["gps_center_place"] = place
            exif_summary["gps_spread_km"] = round(profile.gps_radius_km, 1)

    excerpts: list[tuple[str, str]] = []
    for f in [n for n in names if file_kind(n) in ("document", "other")]:
        if len(excerpts) >= EXCERPTS:
            break
        path = root / f
        if looks_like_email(path, None):
            headers, body = read_email(path)
            text = f"{headers}\n{body}"
        else:
            text = extract_text(path, None)
            if not text and file_kind(f) == "document":
                try:
                    text = path.read_bytes()[:EXCERPT_CHARS].decode("utf-8", errors="replace")
                except OSError:
                    text = ""
        if text.strip():
            excerpts.append((f, text.strip()[:EXCERPT_CHARS]))

    images: list[bytes] = []
    if vision:
        for f in _spread(media, IMAGE_SAMPLE):
            path = root / f
            data = (
                image_preview(path, preview_px // 2)
                if file_kind(f) == "image"
                else b"".join(video_frames(path, 1, preview_px // 2)[:1])
            )
            if data:
                images.append(data)

    return FolderPacket(
        rel=walk.rel,
        name=Path(walk.rel).name,
        files=len(names),
        total_size=sum(s for _, s, _ in walk.files),
        subfolders=sorted(subs.items(), key=lambda kv: -kv[1])[:30],
        kinds=kinds,
        extensions=dict(sorted(exts.items(), key=lambda kv: -kv[1])[:12]),
        mtime_first=iso(mtimes[0]) if mtimes else "",
        mtime_last=iso(mtimes[-1]) if mtimes else "",
        names=_spread(names, MAX_NAMES),
        exif_summary=exif_summary,
        excerpts=excerpts,
        images=images,
        profile=profile,
        code_files=sum(1 for f in names if is_code_file(f)),
    )


def profile_from(files: int, kinds: dict[str, int], exifs: list[dict[str, str]]) -> FolderProfile:
    """The folder's common ground, robust to the few files that do not belong.

    The sample may contain the very outliers the profile is meant to catch,
    so dates use the quartiles (Tukey fences, at least 60 days wide) and GPS
    the median point with the 75th-percentile spread, not the extremes.
    """
    dates = sorted(d for d in (capture_date(e) for e in exifs) if d)
    points = [p for p in (gps_of(e) for e in exifs) if p]
    prof = FolderProfile(files=files, kinds=dict(kinds), media_sampled=len(exifs))
    for e in exifs:
        dev = device_of(e)
        if dev:
            prof.devices[dev] = prof.devices.get(dev, 0) + 1
    prof.with_device = sum(prof.devices.values())
    if dates:
        q1, q3 = dates[len(dates) // 4], dates[(3 * len(dates)) // 4]
        fence = max(DATE_MARGIN, (q3 - q1) * 1.5)
        lo, hi = q1 - fence, q3 + fence
        core = [d for d in dates if lo <= d <= hi]
        prof.date_min, prof.date_max = core[0].strftime("%Y-%m-%d"), core[-1].strftime("%Y-%m-%d")
    # GPS is only a trait of the folder when most sampled media have it.
    if points and len(points) >= max(2, len(exifs) // 2):
        lat = sorted(p[0] for p in points)[len(points) // 2]
        lon = sorted(p[1] for p in points)[len(points) // 2]
        dists = sorted(distance_km((lat, lon), p) for p in points)
        prof.gps_lat, prof.gps_lon = lat, lon
        prof.gps_radius_km = dists[max(0, (3 * len(dists) - 1) // 4)]  # 75th percentile
    return prof


def outlier_reason(rel_in_folder: str, exif: dict[str, str], profile: FolderProfile) -> str:
    """Why this file does not fit its folder's profile ("" if it fits).

    Checked for every file of a folder that moves as a whole, so a stray
    invoice among holiday photos, a picture from another year or trip, or a
    video from the user's own phone in a folder of downloads is sorted on
    its own instead of travelling with the folder.
    """
    kind = file_kind(rel_in_folder)
    total = max(1, profile.files)
    # Photos and videos from one camera are one kind: a clip among holiday photos belongs there.
    media = ("image", "video")
    same_kind = sum(profile.kinds.get(k, 0) for k in media) if kind in media else profile.kinds.get(kind, 0)
    if profile.files >= 10 and same_kind / total < 0.10:
        main = max(profile.kinds, key=profile.kinds.get) if profile.kinds else "other"
        return f"a {kind} file among {main} files"
    if kind in ("image", "video"):
        when = capture_date(exif)
        if when and profile.date_min:
            lo = datetime.strptime(profile.date_min, "%Y-%m-%d") - DATE_MARGIN
            hi = datetime.strptime(profile.date_max, "%Y-%m-%d") + DATE_MARGIN
            if not lo <= when <= hi:
                return (
                    f"taken {when:%Y-%m-%d}, outside the folder's {profile.date_min} … {profile.date_max}"
                )
        where = gps_of(exif)
        if where and profile.gps_lat is not None:
            dist = distance_km((profile.gps_lat, profile.gps_lon), where)
            if dist > max(MIN_GPS_KM, 3 * profile.gps_radius_km):
                return f"GPS {dist:.0f} km away from the rest of the folder"
        device = device_of(exif)
        sampled = max(1, profile.media_sampled)
        without = (profile.media_sampled - profile.with_device) / sampled
        if device and profile.media_sampled >= 4 and without >= 0.75 and profile.devices.get(device, 0) / sampled <= 0.25:
            return f"taken with {device}, unlike the rest of the folder (no camera recorded)"
        with_camera = profile.with_device / sampled
        if profile.media_sampled >= 4 and with_camera >= 0.75 and not camera_signs(kind, exif, Path(rel_in_folder).name):
            return "no camera recorded, unlike the rest of the folder"
    return ""


@dataclass
class FolderAnswer:
    summary: str
    coherent: bool
    jd_id: str
    subfolder: str
    confidence: float
    reason: str
    rule: str = ""
    new_id_category: str = ""
    new_id_name: str = ""
    own_media: bool = False  # the photos and videos in it are the user's own shots
    intact: bool = False  # it only works in one piece (software, a website, a backup): take nothing out


def parse_folder_answer(text: str) -> FolderAnswer:
    data = extract_json_object(text)
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    coherent, own, intact = (
        v.strip().lower() in ("true", "yes", "1") if isinstance(v, str) else bool(v)
        for v in (data.get("coherent"), data.get("own_media"), data.get("intact"))
    )
    return FolderAnswer(
        summary=str(data.get("summary") or "")[:1200],
        coherent=coherent,
        own_media=own,
        intact=intact,
        jd_id=str(data.get("jd_id") or "").strip()[:40],
        subfolder=str(data.get("subfolder") or "")[:200],
        confidence=confidence,
        reason=str(data.get("reason") or "")[:600],
        rule=str(data.get("rule") or "")[:300],
        new_id_category=str(data.get("new_id_category") or "")[:10],
        new_id_name=str(data.get("new_id_name") or "")[:120],
    )


FOLDER_QUESTION = """\
This time you look at a whole FOLDER, not a single file. Decide whether its files belong together as \
one unit that should stay together (an album, one trip or event, one project, one correspondence, a \
download or backup batch of one kind, a monthly dump of the same kind of files), and if so, which ID \
the whole folder belongs in. Mixed folders (unrelated documents, bills next to photos, several topics) \
are not a unit: say coherent false, and the files will be sorted one by one.

Reply with ONE JSON object and nothing else:
{"summary": "2-3 English sentences: what the folder holds and what ties the files together",
 "coherent": true,
 "jd_id": "NN.NN from the outline, or \\"new\\" as for single files",
 "subfolder": "optional existing subfolder of that ID (e.g. a year folder the ID already uses)",
 "new_id_category": "only with jd_id \\"new\\"", "new_id_name": "only with jd_id \\"new\\"",
 "intact": false, "own_media": false,
 "confidence": 0.0, "reason": "one sentence", "rule": "the user rule you followed, if any"}
intact is true when the files depend on each other and the folder only works in one piece: software, \
source code, a website, a game, application data, a backup of a system. Nothing is taken out of such a \
folder, not even pictures with a camera in their EXIF. It is false for collections whose files each \
stand on their own (photos, documents, downloads, letters).
own_media is true only when the folder's photos and videos are the user's own shots (taken with their \
camera or phone: see devices, GPS and file names). It is false for downloads, memes, received pictures, \
screenshots, films, scans and pictures that belong to software or a project.
The folder keeps its own name and inner structure inside the chosen ID. The one exception: the user's \
own photos and videos are always filed by capture date, so sorto puts each of them into YYYY/MM inside \
the chosen ID.

FOLDER PACKET:
"""
