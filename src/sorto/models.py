from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

TERMINAL_LIVE = frozenset({"done", "skipped", "error", "needs_user"})
IN_FLIGHT = frozenset({"identifying", "analyzing", "planned", "moving"})
ALL_STATUSES = frozenset(
    {
        "discovered",
        "identifying",
        "analyzing",
        "planned",
        "moving",
        "done",
        "skipped",
        "error",
        "needs_user",
    }
)

SUGGESTED_FOLDERS = (
    "documents",
    "spreadsheets",
    "presentations",
    "images/photos",
    "images/screenshots",
    "images/diagrams",
    "video",
    "audio",
    "code",
    "archives",
    "data",
    "installers_and_binaries",
    "email_and_exports",
    "3d_and_cad",
    "design",
    "ebooks",
    "_unsorted",
    "_duplicates_candidates",
    "_cache_temp_and_junk",
)

LABEL_TO_FOLDER: dict[str, str] = {
    "invoice": "documents",
    "receipt": "documents",
    "contract": "documents",
    "letter": "documents",
    "document": "documents",
    "pdf": "documents",
    "paper": "documents",
    "spreadsheet": "spreadsheets",
    "csv": "spreadsheets",
    "excel": "spreadsheets",
    "presentation": "presentations",
    "slides": "presentations",
    "photo": "images/photos",
    "image": "images/photos",
    "screenshot": "images/screenshots",
    "diagram": "images/diagrams",
    "chart": "images/diagrams",
    "video": "video",
    "movie": "video",
    "audio": "audio",
    "music": "audio",
    "code": "code",
    "source": "code",
    "archive": "archives",
    "zip": "archives",
    "data": "data",
    "database": "data",
    "installer": "installers_and_binaries",
    "binary": "installers_and_binaries",
    "email": "email_and_exports",
    "mbox": "email_and_exports",
    "cad": "3d_and_cad",
    "3d": "3d_and_cad",
    "model": "3d_and_cad",
    "design": "design",
    "ebook": "ebooks",
    "book": "ebooks",
    "unknown": "_unsorted",
    "unsorted": "_unsorted",
    "duplicate": "_duplicates_candidates",
    "junk": "_cache_temp_and_junk",
    "cache": "_cache_temp_and_junk",
    "temp": "_cache_temp_and_junk",
    "thumbnail": "_cache_temp_and_junk",
}


@dataclass
class Classification:
    label: str
    confidence: float
    dest_rel: str
    rename: bool
    reason: str
    needs_user: bool
    raw: str | None = None


@dataclass
class AnalysisPacket:
    src_rel: str
    filename: str
    extension: str
    size: int
    mtime_iso: str
    mtime_ns: int
    mime: str | None
    magic: str | None
    type_guess: str | None
    hex_preview: str
    text_preview: str
    extra_meta: dict[str, str]
    sha256: str | None
    top_level_folders: list[str]
    dest_scheme: str
    meaningless_name: bool
    duplicate_of: str | None = None
    keep_extension: bool = True
    is_junk: bool = False
    junk_reason: str | None = None

    def to_llm_dict(self) -> dict[str, Any]:
        extra = {k: v[:1500] for k, v in self.extra_meta.items() if v}
        payload: dict[str, Any] = {
            "src_rel": self.src_rel,
            "filename": self.filename,
            "extension": self.extension,
            "size": self.size,
            "mtime": self.mtime_iso,
            "mime": self.mime,
            "magic": self.magic,
            "type_guess": self.type_guess,
            "hex_preview": self.hex_preview,
            "text_preview": self.text_preview,
            "sha256": self.sha256,
            "meaningless_name": self.meaningless_name,
            "dest_scheme": self.dest_scheme,
            "keep_extension": self.keep_extension,
            "top_level_folders": self.top_level_folders[:80],
        }
        if extra:
            payload["extra_meta"] = extra
        if self.duplicate_of:
            payload["duplicate_of"] = self.duplicate_of
        if self.is_junk:
            payload["is_junk"] = True
            payload["junk_reason"] = self.junk_reason
        return payload


@dataclass
class AnalysisView:
    src_rel: str = ""
    filename: str = ""
    mime: str = ""
    size: int = 0
    label: str = ""
    confidence: float = 0.0
    dest_rel: str = ""
    reason: str = ""
    tokens: int = 0
    latency_s: float = 0.0
    stage: str = ""


@dataclass
class QueueRow:
    status: str
    src_rel: str


@dataclass
class Counts:
    discovered: int = 0
    identifying: int = 0
    analyzing: int = 0
    planned: int = 0
    moving: int = 0
    done: int = 0
    skipped: int = 0
    error: int = 0
    needs_user: int = 0
    pending: int = 0
    total: int = 0

    def from_status_map(self, raw: dict[str, int]) -> None:
        self.discovered = raw.get("discovered", 0)
        self.identifying = raw.get("identifying", 0)
        self.analyzing = raw.get("analyzing", 0)
        self.planned = raw.get("planned", 0)
        self.moving = raw.get("moving", 0)
        self.done = raw.get("done", 0)
        self.skipped = raw.get("skipped", 0)
        self.error = raw.get("error", 0)
        self.needs_user = raw.get("needs_user", 0)
        self.total = sum(raw.values())


@dataclass
class Snapshot:
    root: str = ""
    model: str = ""
    mode: str = "LIVE"
    scan_state: str = "live"
    counts: Counts = field(default_factory=Counts)
    current_identify: str | None = None
    current_analyze: str | None = None
    current_move: str | None = None
    last_analysis: AnalysisView | None = None
    queue_rows: list[QueueRow] = field(default_factory=list)
    log_lines: list[str] = field(default_factory=list)
    log_seq: int = 0
    llm_ok: bool = True
    llm_latency_s: float | None = None
    llm_error: str | None = None
    progress_pct: float = 0.0
    elapsed_s: float = 0.0
    eta_s: float | None = None
    paused: bool = False
    follow: bool = True
    scan_still_running: bool = True
    tokens_est: int = 0
    finished: bool = False
