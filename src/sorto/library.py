"""Dest scheme `library` — mirrors /mnt/hdd/organized.

Learned from the live tree (not invented):
  Photos/Videos/Audio/Code/Archives  → YYYY/YYYY-MM (mtime)
  Screenshots / Signal / WhatsApp    → YYYY-MM-<Event> under Photos
  Documents                          → Johnny.Decimal IDs, else Documents/_quarry/YYYY/YYYY-MM
  TempAndCache                       → cache/temp/junk (year-month)
  Backup-Garbage                     → firmware, dpkg debris, maildir crumbs, .pyc/.ko
  Emails                             → Emails/YYYY
  To-Annex/Large-Files               → very large media/archives
  GitRepositories / OS-Extracts / WebsiteBackups / Wepardi — leave intact if already there

A small local LLM only has to fill gaps; heuristics cover the high-volume types.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from sorto.models import AnalysisPacket

PHOTO_EXT = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".heic",
    ".heif",
    ".bmp",
    ".tif",
    ".tiff",
    ".raw",
    ".dng",
    ".cr2",
    ".nef",
}
VIDEO_EXT = {
    ".mp4",
    ".mkv",
    ".mov",
    ".avi",
    ".webm",
    ".m4v",
    ".mpg",
    ".mpeg",
    ".wmv",
    ".flv",
    ".3gp",
    ".ts",
    ".m2ts",
}
AUDIO_EXT = {
    ".mp3",
    ".wav",
    ".flac",
    ".ogg",
    ".opus",
    ".m4a",
    ".aac",
    ".wma",
    ".aiff",
}
CODE_EXT = {
    ".py",
    ".rs",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".go",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".java",
    ".rb",
    ".php",
    ".sh",
    ".pl",
    ".lua",
    ".sql",
    ".r",
    ".m",
    ".swift",
    ".kt",
    ".cs",
    ".scala",
}
ARCHIVE_EXT = {".zip", ".tar", ".gz", ".tgz", ".7z", ".rar", ".bz2", ".xz", ".zst"}
EMAIL_EXT = {".eml", ".mbox", ".pst", ".ost", ".msg"}
DOC_EXT = {
    ".pdf",
    ".odt",
    ".doc",
    ".docx",
    ".rtf",
    ".xls",
    ".xlsx",
    ".ods",
    ".csv",
    ".ppt",
    ".pptx",
    ".epub",
    ".odp",
}

LIBRARY_MARKERS = {
    "Photos",
    "Documents",
    "Videos",
    "Audio",
    "TempAndCache",
    "Backup-Garbage",
    "Code",
    "Emails",
}

# Keep these trees as-is (already curated dumps / clones).
KEEP_TOP = {
    "GitRepositories",
    "WebsiteBackups",
    "OS-Extracts",
    "Wepardi",
    "Int2000",
    "To-Annex",
    "_meta",
    "Media",
}

_MAILDIR_CRUMB = re.compile(
    r"^\d{9,}\..+|.*,S=\d+|.*soderlund\.in|.*webol\.fi|.*int2000\.net",
    re.I,
)
_FIRMWARE = re.compile(
    r"\.(ucode|fw)$|_smc\.bin$|_rlc\.bin$|_sdma\.bin$|_vcn\.bin$|"
    r"_pfp\.bin$|_mec\.bin$|iwlwifi-|ath3k-|ipw2100",
    re.I,
)
_DPKG_DEBRIS = re.compile(
    r"\.(md5sums|postrm|preinst|postinst|list|shlibs|symbols|conffiles|triggers)$",
    re.I,
)
_SCREENSHOT = re.compile(r"screenshot|screen[_-]?shot|screen[_-]?capture", re.I)
_SIGNAL = re.compile(r"^signal[-_]", re.I)
_WHATSAPP = re.compile(r"(^IMG-|^VID-|-WA\d+|whatsapp)", re.I)


# (dest under Documents/, pattern, add YYYY subfolder)
_JD_RULES: list[tuple[str, re.Pattern[str], bool]] = [
    (
        "00-09 System/03 Quarantine/03.14 Secrets and certs",
        re.compile(r"(privkey|fullchain|aegis-backup|\.pem$|id_rsa|id_ed25519)", re.I),
        False,
    ),
    (
        "20-29 Work papers/22 Vendor invoices/22.11 Hetzner",
        re.compile(r"hetzner", re.I),
        True,
    ),
    (
        "20-29 Work papers/22 Vendor invoices/22.12 Cloudflare",
        re.compile(r"cloudflare", re.I),
        True,
    ),
    (
        "20-29 Work papers/22 Vendor invoices/22.13 Other hosting vendors",
        re.compile(r"(^|[^a-z])ovh([^a-z]|$)|aws-invoice", re.I),
        True,
    ),
    (
        "20-29 Work papers/21 Company admin/21.11 Contracts",
        re.compile(r"työsopimus|tyosopimus|työtodistus|tyotodistus", re.I),
        False,
    ),
    (
        "20-29 Work papers/23 Domains and registry/23.11 Ficora and Traficom",
        re.compile(r"ficora|traficom", re.I),
        False,
    ),
    (
        "20-29 Work papers/24 Account exports/24.11 int2000",
        re.compile(r"int2000", re.I),
        False,
    ),
    (
        "20-29 Work papers/24 Account exports/24.12 wepardi",
        re.compile(r"wepardi", re.I),
        False,
    ),
    (
        "20-29 Work papers/25 Technical library/25.11 ESMI Intellia fire systems",
        re.compile(r"intellia|esmi[^a-z]|esgraf", re.I),
        True,
    ),
    (
        "20-29 Work papers/25 Technical library/25.21 Nextcloud",
        re.compile(r"nextcloud", re.I),
        False,
    ),
    (
        "10-19 Life admin/11 Identity and legal/11.11 Identity and credit freeze",
        re.compile(r"luottokielto|passi[^a-z]|passport", re.I),
        False,
    ),
    (
        "10-19 Life admin/11 Identity and legal/11.12 Living will",
        re.compile(r"hoitotahto", re.I),
        False,
    ),
    (
        "10-19 Life admin/11 Identity and legal/11.13 Testament",
        re.compile(r"testament", re.I),
        False,
    ),
    (
        "10-19 Life admin/11 Identity and legal/11.21 CV and work history",
        re.compile(r"(^|[^a-z])cv([^a-z]|$)|ansioluettelo", re.I),
        False,
    ),
    (
        "10-19 Life admin/14 Health/14.21 Sick notes",
        re.compile(r"sairauspoissaolo", re.I),
        False,
    ),
    (
        "10-19 Life admin/14 Health/14.11 Records and imaging",
        re.compile(r"ultraääni|ultraaani|röntgen|rontgen|lääkäri|laakari", re.I),
        False,
    ),
    (
        "10-19 Life admin/13 Money/13.31 Aktia statements",
        re.compile(r"tiliote|aktia", re.I),
        True,
    ),
    (
        "10-19 Life admin/13 Money/13.21 Tax",
        re.compile(r"verohallinto|(^|[^a-z])vero([^a-z]|$)|tax-return", re.I),
        True,
    ),
    (
        "10-19 Life admin/13 Money/13.41 Kela",
        re.compile(r"(^|[^a-z0-9])kela([^a-z]|$)", re.I),
        True,
    ),
    (
        "10-19 Life admin/13 Money/13.42 Unemployment",
        re.compile(r"työttömyysturva|tyottomyysturva", re.I),
        True,
    ),
    (
        "10-19 Life admin/13 Money/13.51 Payroll",
        re.compile(r"palkkalaskelma|(^|[^a-z])palkka([^a-z]|$)", re.I),
        True,
    ),
    (
        "10-19 Life admin/13 Money/13.61 Insurance",
        re.compile(r"vakuutuskirja|vakuutus", re.I),
        True,
    ),
    (
        "10-19 Life admin/12 Home and housing/12.11 Lease and housing",
        re.compile(r"vuokrasopimus|(^|[^a-z])vuokra([^a-z]|$)", re.I),
        False,
    ),
    (
        "10-19 Life admin/12 Home and housing/12.21 Electricity",
        re.compile(r"fortum|sähkö|sahko", re.I),
        True,
    ),
    (
        "10-19 Life admin/12 Home and housing/12.23 Internet and phone",
        re.compile(r"(^|[^a-z])elisa([^a-z]|$)|telia|dna", re.I),
        True,
    ),
    (
        "30-39 Knowledge/32 Publications/32.11 Vartiotorni",
        re.compile(r"vartiotorni", re.I),
        True,
    ),
    (
        "30-39 Knowledge/32 Publications/32.12 Heratkaa",
        re.compile(r"herätkää|heratkaa", re.I),
        True,
    ),
    (
        "10-19 Life admin/13 Money/13.11 Invoices and receipts",
        re.compile(r"lasku|invoice|kuitti|receipt|tosite", re.I),
        True,
    ),
    (
        "10-19 Life admin/17 Scans inbox/17.01 Unnamed scans",
        re.compile(r"^(scan[\s_-]?\d|swiftscan|ccf_\d|scan000)", re.I),
        True,
    ),
]


def looks_like_library_root(root: Path) -> bool:
    try:
        names = {p.name for p in root.iterdir() if p.is_dir() and not p.is_symlink()}
    except OSError:
        return False
    return len(names & LIBRARY_MARKERS) >= 3


def year_month(mtime_ns: int) -> tuple[str, str]:
    try:
        dt = datetime.fromtimestamp(mtime_ns / 1e9, tz=UTC)
    except (OSError, OverflowError, ValueError):
        dt = datetime(1970, 1, 1, tzinfo=UTC)
    year = f"{dt.year:04d}"
    month = f"{year}-{dt.month:02d}"
    return year, month


def _event_tag(filename: str) -> str | None:
    if _SCREENSHOT.search(filename):
        return "Screenshots"
    if _SIGNAL.search(filename):
        return "Signal"
    if _WHATSAPP.search(filename):
        return "WhatsApp"
    return None


def _dated(bucket: str, mtime_ns: int, filename: str, event: str | None = None) -> str:
    year, ym = year_month(mtime_ns)
    folder = f"{ym}-{event}" if event else ym
    return f"{bucket}/{year}/{folder}/{filename}"


def is_backup_garbage(filename: str, src_rel: str) -> bool:
    name = filename
    if _MAILDIR_CRUMB.match(name):
        return True
    if _FIRMWARE.search(name):
        return True
    if _DPKG_DEBRIS.search(name):
        return True
    low = name.lower()
    if low.endswith((".pyc", ".ko", ".class", ".ibd", ".ucode")):
        return True
    if low.endswith(".~1~") or low.endswith(".~2~"):
        return True
    if low.endswith(".bin") and any(
        x in low for x in ("_me.bin", "_ce.bin", "_pfp", "navi", "vega", "polaris")
    ):
        return True
    return False


def _jd_dest(filename: str, mtime_ns: int) -> str | None:
    for rel, pat, yearly in _JD_RULES:
        if pat.search(filename):
            if yearly:
                year, _ym = year_month(mtime_ns)
                return f"Documents/{rel}/{year}/{filename}"
            return f"Documents/{rel}/{filename}"
    return None


def keep_in_place(src_rel: str) -> bool:
    top = src_rel.split("/", 1)[0]
    return top in KEEP_TOP


def route_library(packet: AnalysisPacket) -> str | None:
    """High-confidence dest_rel for the library scheme, or None to ask the LLM."""
    name = packet.filename
    ext = (packet.extension or Path(name).suffix).lower()
    src = packet.src_rel
    if keep_in_place(src):
        return src
    if packet.duplicate_of:
        return f"_duplicates_candidates/{name}"
    if packet.is_junk:
        return _dated("TempAndCache", packet.mtime_ns, name)
    if is_backup_garbage(name, src):
        return f"Backup-Garbage/{name}"

    if ext in PHOTO_EXT:
        return _dated("Photos", packet.mtime_ns, name, _event_tag(name))
    if ext in VIDEO_EXT:
        if packet.size and packet.size >= 80 * 1024 * 1024:
            return _dated("To-Annex/Large-Files", packet.mtime_ns, name)
        return _dated("Videos", packet.mtime_ns, name)
    if ext in AUDIO_EXT:
        return _dated("Audio", packet.mtime_ns, name)
    if ext in EMAIL_EXT:
        year, _ym = year_month(packet.mtime_ns)
        return f"Emails/{year}/{name}"
    if ext in ARCHIVE_EXT:
        if packet.size and packet.size >= 80 * 1024 * 1024:
            return _dated("To-Annex/Large-Files", packet.mtime_ns, name)
        return _dated("Archives", packet.mtime_ns, name)
    if ext in CODE_EXT:
        return _dated("Code", packet.mtime_ns, name)
    if ext in DOC_EXT:
        jd = _jd_dest(name, packet.mtime_ns)
        if jd:
            return jd
        return _dated("Documents/_quarry", packet.mtime_ns, name)
    if ext in {".stl", ".obj", ".3mf", ".step", ".stp", ".fcstd"}:
        return f"Documents/40-49 Project docs/41 3D printing/41.11 Notes and manuals/{name}"

    # Maildir-like no-extension crumbs
    if not ext and _MAILDIR_CRUMB.match(name):
        return f"Backup-Garbage/{name}"
    return None


def library_top_folders() -> list[str]:
    return [
        "Photos",
        "Videos",
        "Audio",
        "Documents",
        "Documents/_quarry",
        "Code",
        "Emails",
        "Archives",
        "TempAndCache",
        "Backup-Garbage",
        "GitRepositories",
        "OS-Extracts",
        "WebsiteBackups",
        "To-Annex/Large-Files",
        "Wepardi",
        "Int2000",
        "_duplicates_candidates",
    ]
