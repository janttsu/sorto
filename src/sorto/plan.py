from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sorto.junk import JUNK_FOLDER
from sorto.models import LABEL_TO_FOLDER, SUGGESTED_FOLDERS, AnalysisPacket, Classification
from sorto.util import (
    UnsafePathError,
    is_meaningless_name,
    posix_rel,
    unique_dest,
    validate_dest_rel,
)


class PlanError(ValueError):
    pass


def folder_for_label(label: str) -> str:
    key = (label or "unknown").strip().lower().replace(" ", "-")
    if key in LABEL_TO_FOLDER:
        return LABEL_TO_FOLDER[key]
    for hint, folder in LABEL_TO_FOLDER.items():
        if hint in key or key in hint:
            return folder
    return "_unsorted"


def _year(packet: AnalysisPacket) -> str:
    try:
        return datetime.fromtimestamp(packet.mtime_ns / 1e9, tz=UTC).strftime("%Y")
    except (OSError, OverflowError, ValueError):
        return datetime.now(UTC).strftime("%Y")


def _filename_for_dest(packet: AnalysisPacket, classification: Classification, dest_rel: str) -> str:
    original = packet.filename
    orig_ext = Path(original).suffix
    proposed = Path(dest_rel).name or original
    if packet.keep_extension and orig_ext:
        proposed_path = Path(proposed)
        if proposed_path.suffix.lower() != orig_ext.lower():
            proposed = str(proposed_path.with_suffix(orig_ext))
    should_rename = bool(classification.rename) and (
        packet.meaningless_name or is_meaningless_name(original)
    )
    if not should_rename:
        name = original
        if packet.keep_extension and orig_ext:
            name = str(Path(original).with_suffix(orig_ext))
        return name
    return proposed


def apply_scheme(packet: AnalysisPacket, classification: Classification, dest_rel: str) -> str:
    filename = _filename_for_dest(packet, classification, dest_rel)
    scheme = packet.dest_scheme
    if scheme == "by-type":
        folder = folder_for_label(classification.label)
        return f"{folder}/{filename}"
    if scheme == "by-type-year":
        folder = folder_for_label(classification.label)
        return f"{folder}/{_year(packet)}/{filename}"
    # default: LLM dest, but force original/chosen filename
    parent = str(Path(dest_rel).parent)
    if parent in (".", ""):
        return filename
    return f"{posix_rel(parent)}/{filename}"


def plan_destination(
    root: Path,
    packet: AnalysisPacket,
    classification: Classification,
    *,
    allow_extension_fix: bool = False,
) -> str:
    """Validate and uniquify dest_rel. Returns dest relative to root."""
    orig_ext = Path(packet.filename).suffix
    raw = (classification.dest_rel or "").strip()
    if packet.duplicate_of:
        raw = f"_duplicates_candidates/{packet.filename}"
    elif packet.is_junk:
        raw = f"{JUNK_FOLDER}/{packet.filename}"
    elif not raw:
        raw = f"{folder_for_label(classification.label)}/{packet.filename}"
    if classification.confidence < 0.45 and not packet.duplicate_of and not packet.is_junk:
        raw = f"_unsorted/{packet.filename}"
    keep_ext = orig_ext if (packet.keep_extension or not allow_extension_fix) else None
    try:
        cleaned = validate_dest_rel(raw, original_ext=keep_ext)
    except UnsafePathError as e:
        raise PlanError(str(e)) from e
    dest_rel = apply_scheme(packet, classification, cleaned)
    try:
        dest_rel = validate_dest_rel(dest_rel, original_ext=keep_ext)
    except UnsafePathError as e:
        raise PlanError(str(e)) from e
    dest_path = unique_dest(root, dest_rel, root / packet.src_rel)
    try:
        rel = posix_rel(str(dest_path.relative_to(root.resolve())))
    except ValueError as e:
        raise PlanError("destination escaped root") from e
    try:
        return validate_dest_rel(rel, original_ext=keep_ext)
    except UnsafePathError as e:
        raise PlanError(str(e)) from e


def existing_folder_hint(root: Path) -> list[str]:
    names = list(SUGGESTED_FOLDERS)
    try:
        for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if child.is_dir() and not child.is_symlink() and child.name != "_organization":
                if child.name not in names:
                    names.append(child.name)
    except OSError:
        pass
    return names
