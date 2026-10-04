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
        "gone",  # no longer in the source when its turn came (moved or deleted by someone else)
    }
)

@dataclass
class Classification:
    label: str
    confidence: float
    jd_id: str
    reason: str
    needs_user: bool
    summary: str = ""
    subfolder: str = ""
    new_filename: str = ""
    rule: str = ""  # the user rule the model says it followed, if any
    new_id_category: str = ""  # with jd_id "new": the existing category (NN)
    new_id_name: str = ""  # with jd_id "new": the topic; sorto picks the number
    own_media: bool | None = None  # the user's own photo or video (None: the model did not say)
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
    meaningless_name: bool
    duplicate_of: str | None = None
    keep_extension: bool = True
    is_junk: bool = False
    junk_reason: str | None = None
    exif: dict[str, str] = field(default_factory=dict)
    images: list[bytes] = field(default_factory=list, repr=False)
    media_kind: str = ""
    media_note: str = ""
    camera_signs: str = ""  # evidence that a camera or phone made this photo or video
    gps_place: str = ""  # the city at the GPS position, worked out by sorto (geo.place_of)
    current_location: str = ""  # reorganize mode: where the file is filed now
    folder_context: str = ""  # what its folder is, when the folder was looked at as a whole
    light: bool = False  # identified without previews (it was expected to move with its folder)

    def to_llm_dict(self) -> dict[str, Any]:
        extra = {
            k: v[:3000] if k == "document_text" else v[:1500] for k, v in self.extra_meta.items() if v
        }
        # Lean on purpose: every token here is read for every file. The hash, a second type field and
        # a hex dump of a file whose type is known cost a fifth of the time and changed no answer.
        unknown = not self.mime or self.mime in ("application/octet-stream", "inode/x-empty")
        payload: dict[str, Any] = {
            "src_rel": self.src_rel,
            "filename": self.filename,
            "extension": self.extension,
            "size": self.size,
            "mtime": self.mtime_iso,
            "mime": self.mime,
        }
        if unknown:
            payload["magic"] = self.magic
            if not self.text_preview:
                payload["hex_preview"] = self.hex_preview
        payload["text_preview"] = self.text_preview
        payload["meaningless_name"] = self.meaningless_name
        if extra:
            payload["extra_meta"] = extra
        if self.duplicate_of:
            payload["duplicate_of"] = self.duplicate_of
        if self.is_junk:
            payload["is_junk"] = True
            payload["junk_reason"] = self.junk_reason
        if self.exif:
            payload["exif"] = self.exif
        if self.gps_place:
            payload["gps_place"] = self.gps_place
        if self.media_kind:
            payload["camera_signs"] = self.camera_signs or "none: no camera or phone recorded"
        if self.current_location:
            payload["currently_filed_in"] = self.current_location
        if self.folder_context:
            payload["folder_context"] = self.folder_context
        if self.images:
            payload["attached_images"] = (
                "the photo itself (downscaled)"
                if self.media_kind == "image"
                else f"{len(self.images)} frames sampled across the video, in order"
            )
        return payload


@dataclass
class ModelStats:
    """Answer times of one model in this run (for comparing models)."""

    model: str
    files: int = 0
    total_s: float = 0.0
    last_s: float = 0.0

    @property
    def avg_s(self) -> float:
        return self.total_s / self.files if self.files else 0.0


@dataclass
class AnalysisView:
    """What the TUI shows for one file, from "analyzing" to its final place."""

    src_rel: str = ""
    filename: str = ""
    mime: str = ""
    size: int = 0
    label: str = ""
    confidence: float = 0.0
    summary: str = ""
    jd_id: str = ""
    jd_name: str = ""
    dest_rel: str = ""
    reason: str = ""
    media_note: str = ""
    rule: str = ""
    current_location: str = ""
    new_id: str = ""  # "40.16 Name" when this file needs a new ID
    folder: str = ""  # its folder, when the folder was looked at as a whole
    folder_unit: bool = False  # the whole folder is being moved (asked once with --confirm)
    model: str = ""
    outcome: str = ""  # moved | planned | kept | in_place | deleted | error
    tokens: int = 0
    latency_s: float = 0.0
    stage: str = ""  # analyzing | moving | done
    seq: int = 0


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
    gone: int = 0
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
        self.gone = raw.get("gone", 0)
        self.total = sum(raw.values()) - self.gone  # files that left the source are not sorto's work


@dataclass
class Snapshot:
    source: str = ""
    target: str = ""
    jd_ids: int = 0
    model: str = ""
    mode: str = "LIVE"
    scan_state: str = "live"
    counts: Counts = field(default_factory=Counts)
    current_identify: str | None = None
    current_analyze: str | None = None
    current_move: str | None = None
    current: AnalysisView | None = None
    last_analysis: AnalysisView | None = None
    history: list[AnalysisView] = field(default_factory=list)
    queue_rows: list[QueueRow] = field(default_factory=list)
    log_lines: list[str] = field(default_factory=list)
    log_seq: int = 0
    llm_ok: bool = True
    llm_latency_s: float | None = None
    llm_error: str | None = None
    progress_pct: float = 0.0
    elapsed_s: float = 0.0
    eta_s: float | None = None
    per_file_s: float | None = None
    pace_samples: int = 0
    paused: bool = False
    follow: bool = True
    scan_still_running: bool = True
    tokens_est: int = 0
    finished: bool = False
    awaiting_confirm: bool = False
    reorganize: bool = False
    reorganize_depth: int = 0
    reorganize_scope: str = ""  # only this area or category of the target is reorganized
    rules: int = 0  # non-empty lines in the user's rules file
    models: list[str] = field(default_factory=list)
    model_stats: list[ModelStats] = field(default_factory=list)
