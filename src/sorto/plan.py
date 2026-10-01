from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from sorto.jd import YEAR_MONTH_RE, YEAR_RE, JDIndex, JDItem, is_date_id
from sorto.media import capture_date, date_subfolder, name_date
from sorto.models import AnalysisPacket, Classification
from sorto.util import (
    UNSAFE_DEST_RE,
    UnsafePathError,
    is_meaningless_name,
    posix_rel,
    unique_dest,
    validate_dest_rel,
)


class PlanError(ValueError):
    pass


@dataclass
class Plan:
    dest_rel: str
    item: JDItem
    subfolder: str
    renamed: bool
    dated: bool = False  # filed by capture date (YYYY/MM)


def _clean_filename(name: str) -> str:
    name = (name or "").strip().replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name or name in (".", "..") or name.startswith(".") or UNSAFE_DEST_RE.search(name):
        return ""
    return name[:180]


def choose_filename(
    packet: AnalysisPacket, cls: Classification, *, allow_rename: bool, allow_extension_fix: bool
) -> tuple[str, bool]:
    """Keep the original name unless it is meaningless and the model offered a better one."""
    original = packet.filename
    proposed = _clean_filename(cls.new_filename)
    meaningless = packet.meaningless_name or is_meaningless_name(original)
    if not (allow_rename and meaningless and proposed) or proposed == original:
        return original, False
    orig_ext = Path(original).suffix
    if orig_ext and not allow_extension_fix and Path(proposed).suffix.lower() != orig_ext.lower():
        proposed = f"{Path(proposed).stem if Path(proposed).suffix else proposed}{orig_ext}"
    return proposed, True


def _year_is_the_files_own(sub: str, packet: AnalysisPacket) -> bool:
    """A date folder for a photo or video is only good if the file itself carries that year."""
    first = sub.split("/", 1)[0]
    if not (YEAR_RE.match(first) or YEAR_MONTH_RE.match(first)):
        return True  # not a date folder at all
    when = capture_date(packet.exif) or name_date(packet.filename)
    return when is not None and first.startswith(f"{when.year:04d}")


def plan_destination(
    target: Path,
    index: JDIndex,
    packet: AnalysisPacket,
    cls: Classification,
    *,
    src_path: Path,
    allow_rename: bool = True,
    allow_extension_fix: bool = False,
    new_item: JDItem | None = None,
    own_media: bool = False,
    date_folders: str = "off",
    date_ids: Iterable[str] = (),
) -> Plan:
    """Map the model's JD ID (+ optional subfolder) to a unique path under *target*.

    The model never supplies a path: an unknown ID is a PlanError, and only
    subfolders that the target already uses are kept.
    """
    item = new_item or index.get(cls.jd_id)
    if item is None:
        raise PlanError(f"model chose {cls.jd_id or 'no ID'!r}, which is not an ID in the target")
    # A new ID has no subfolders yet, so nothing below it is accepted.
    dir_rel, sub = (item.rel, "") if new_item is not None else index.resolve_dir(item, cls.subfolder)
    if sub and packet.media_kind and not _year_is_the_files_own(sub, packet):
        # A year folder the model picked for a photo or video with no capture date: it can only
        # have come from the file's modification time, which is the day of a copy or a download.
        dir_rel, sub = item.rel, ""
    # The user's own photos and videos are always filed by capture date, whatever subfolder
    # the model suggested. The date comes from the file, never from the model. In the IDs the
    # user listed (date_folder_ids) that holds for every photo and video, whatever the model said.
    stamp = date_subfolder(
        packet.media_kind, packet.exif, own=own_media or is_date_id(item, date_ids), mode=date_folders,
        filename=packet.filename,
    )
    if stamp:
        dir_rel, sub = f"{item.rel}/{stamp}", stamp
    filename, renamed = choose_filename(
        packet, cls, allow_rename=allow_rename, allow_extension_fix=allow_extension_fix
    )
    try:
        rel = validate_dest_rel(f"{dir_rel}/{filename}", preserve_names=True)
    except UnsafePathError as e:
        raise PlanError(str(e)) from e
    dest = unique_dest(target, rel, src_path)
    try:
        rel = posix_rel(str(dest.relative_to(target)))
    except ValueError as e:
        raise PlanError("destination escaped target") from e
    return Plan(dest_rel=rel, item=item, subfolder=sub, renamed=renamed, dated=bool(stamp))
