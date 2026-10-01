from __future__ import annotations

import argparse
import importlib.resources
import json
import os
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sorto import STATE_DIR_NAME
from sorto.util import state_dir, user_config_path

DEFAULT_LLM_URL = "http://127.0.0.1:11434/v1"  # a stock Ollama; a Grok Build profile can override it
DEFAULT_LLM_MODEL = "qwen3.6:35b-a3b"
DEFAULT_CONTEXT_WINDOW = 65536
# Models offered for switching in the TUI (m). Only models measured to be
# worth it on this kind of task are listed; users can add their own.
DEFAULT_SMALL_MODEL = "qwen3.5:9b-16k"  # 16k context, all layers on a 6 GB GPU; see README
DEFAULT_MODELS = [DEFAULT_LLM_MODEL, DEFAULT_SMALL_MODEL]
DEFAULT_EXCLUDE = [
    f"{STATE_DIR_NAME}/**",
    "**/.git/**",
    "**/.svn/**",
    "**/.hg/**",
    "**/.Trash/**",
    "**/.trashes/**",
    "**/node_modules/**",
    "**/.venv/**",
    "**/venv/**",
]

DEFAULT_CONFIG_TOML = """\
# sorto user configuration. What sorto has already processed is remembered per
# SOURCE/TARGET pair in ~/.sorto/<pair>/index.sqlite; delete that file to start over.
#
# LLM settings default to the Grok Build profile for the same model
# (~/.grok/config.toml, or $GROK_HOME/config.toml): base_url, context_window,
# inference_idle_timeout_secs and max_retries. Values here override that.
# Only a loopback URL (this machine) is accepted.

[llm]
# url = "http://127.0.0.1:11434/v1"
# model = "qwen3.6:35b-a3b"
# models = ["qwen3.6:35b-a3b", "qwen3.5:9b-16k"]   # the TUI's m key switches between these
# structure_model = "qwen3.6:35b-a3b"  # always decides new IDs and categories, whichever model reads the
#                                      # files; "" = the model that reads the files decides them too
# context_window = 65536       # keep equal to OLLAMA_CONTEXT_LENGTH / grok context_window
temperature = 0.6
top_p = 0.95
max_tokens = 800
reasoning_effort = "none"      # Qwen 3.6 thinks by default; "none" keeps answers fast
# timeout_sec = 1800

[run]
identify_workers = 4
min_confidence = 0.5           # below this the file stays in the source
max_file_mb = 64
vision = true                  # show photos and video frames to the local model
preview_px = 768               # longest side of the photo preview
video_frames = 3               # frames sampled across each video
hash_max_mb = 256
allow_rename = true            # only for meaningless names (IMG_1234, scan0001, …)
delete_duplicates = false      # never inside a git repo
delete_junk = false            # never inside a git repo
log_level = "INFO"
# Reorganize mode (SOURCE and TARGET are the same directory):
reorganize_depth = 0           # 0 = files directly in an ID folder; 1 = also one subfolder down
reorganize_min_confidence = 0.75  # needed to move a file out of the ID it is already filed in
# rules_path = "~/.config/sorto/rules.md"   # free-form sorting rules for the model
# junk_rules_path = "~/.config/sorto/junk.md" # file patterns that go straight to one folder
allow_new_ids = true           # a new ID (next free number) in an existing category when none fits
allow_new_categories = true    # and a new category (next free number) in an existing area when no category fits
new_id_min_confidence = 0.8    # confidence needed to create one
folder_mode = true             # look at folders as a whole first; move coherent ones together
folder_min_files = 5           # smaller folders are sorted file by file
folder_max_files = 2000        # bigger folders are split into their subfolders
folder_min_confidence = 0.8    # needed to move a whole folder
date_folders = "own"           # your own photos and videos go to <ID>/YYYY/MM by capture date.
                               # "all" = every photo and video, "off" = never
# date_folder_ids = ["51.11 Photos", "54.11 Own videos"]  # IDs where every photo and video goes by date

[scan]
include = []
exclude = []
"""


@dataclass
class SortoConfig:
    source: Path
    target: Path
    llm_url: str = DEFAULT_LLM_URL
    llm_model: str = DEFAULT_LLM_MODEL
    # New IDs and categories change the archive's structure for good, so the big model always
    # decides them, also when a small one reads the files. "" = the analysis model decides.
    structure_model: str = DEFAULT_LLM_MODEL
    llm_api_key: str = ""
    context_window: int = DEFAULT_CONTEXT_WINDOW
    temperature: float = 0.6
    top_p: float = 0.95
    max_tokens: int = 800
    reasoning_effort: str = "none"
    timeout_sec: float = 1800.0
    max_retries: int = 1
    identify_workers: int = 4
    scan_interval: float = 5.0
    min_confidence: float = 0.5
    max_file_mb: int = 64
    vision: bool = True
    preview_px: int = 768
    video_frames: int = 3
    hash_max_mb: int = 256
    allow_rename: bool = True
    allow_extension_fix: bool = False
    dry_run: bool = False
    yes: bool = False
    confirm: bool = False
    delete_duplicates: bool = False
    delete_junk: bool = False
    follow: bool = True
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE))
    log_level: str = "INFO"
    retry_errors: bool = False
    retry_kept: bool = False  # --retry-kept: ask again about files left for the user
    clear_cache: bool = False  # --clear-cache: forget cached model answers and folder decisions first
    # SOURCE was an area or a category of the target: only that part is reorganized in place.
    reorganize_scope: str = ""
    llm_profile_source: str = "built-in defaults"
    reorganize_depth: int = 0
    reorganize_min_confidence: float = 0.75
    rules_path: str = ""
    junk_rules_path: str = ""
    allow_new_ids: bool = True
    allow_new_categories: bool = True
    new_id_min_confidence: float = 0.8
    folder_mode: bool = True
    folder_min_files: int = 5
    folder_max_files: int = 2000
    folder_min_confidence: float = 0.8
    date_folders: str = "own"
    date_folder_ids: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=lambda: list(DEFAULT_MODELS))

    @property
    def reorganize(self) -> bool:
        """SOURCE and TARGET are the same tree: re-check what is already filed."""
        return self.source == self.target

    @property
    def rules_file(self) -> Path:
        from sorto.rules import default_rules_path

        return Path(self.rules_path).expanduser() if self.rules_path else default_rules_path()

    @property
    def junk_file(self) -> Path:
        from sorto.rules import default_junk_path

        return Path(self.junk_rules_path).expanduser() if self.junk_rules_path else default_junk_path()

    @property
    def state(self) -> Path:
        if self.reorganize_scope:
            # Its own index: apart from a reorganize run of the whole target, and from the
            # index older versions kept for this pair, whose paths were relative to the part.
            return state_dir(self.target / f"{self.reorganize_scope} in place", self.target)
        return state_dir(self.source, self.target)

    @property
    def db_path(self) -> Path:
        return self.state / "index.sqlite"

    @property
    def progress_path(self) -> Path:
        return self.state / "progress.jsonl"

    @property
    def log_path(self) -> Path:
        return self.state / "sorto.log"

    @property
    def runs_dir(self) -> Path:
        """One readable log per run: each file, its analysis and where it went."""
        return self.state / "runs"

    @property
    def prompt_path(self) -> Path:
        """Optional prompt override; the packaged prompt is used when it is absent."""
        return user_config_path().parent / "classify.md"

    def to_toml(self) -> str:
        inc = ", ".join(f'"{x}"' for x in self.include)
        exc = ",\n  ".join(f'"{x}"' for x in self.exclude)
        b = lambda v: str(v).lower()  # noqa: E731
        return (
            f"# source = {self.source}\n"
            f"# target = {self.target}\n"
            f"# state  = {self.state}\n"
            f"# llm settings from: {self.llm_profile_source}\n"
            f"\n[llm]\n"
            f'url = "{self.llm_url}"\n'
            f'model = "{self.llm_model}"\n'
            f"models = {json.dumps(self.models)}\n"
            f'structure_model = "{self.structure_model}"\n'
            f"context_window = {self.context_window}\n"
            f"temperature = {self.temperature}\n"
            f"top_p = {self.top_p}\n"
            f"max_tokens = {self.max_tokens}\n"
            f'reasoning_effort = "{self.reasoning_effort}"\n'
            f"timeout_sec = {self.timeout_sec}\n"
            f"max_retries = {self.max_retries}\n"
            f"\n[run]\n"
            f"identify_workers = {self.identify_workers}\n"
            f"scan_interval = {self.scan_interval}\n"
            f"min_confidence = {self.min_confidence}\n"
            f"max_file_mb = {self.max_file_mb}\n"
            f"vision = {b(self.vision)}\n"
            f"preview_px = {self.preview_px}\n"
            f"video_frames = {self.video_frames}\n"
            f"hash_max_mb = {self.hash_max_mb}\n"
            f"allow_rename = {b(self.allow_rename)}\n"
            f"allow_extension_fix = {b(self.allow_extension_fix)}\n"
            f"dry_run = {b(self.dry_run)}\n"
            f"yes = {b(self.yes)}\n"
            f"confirm = {b(self.confirm)}\n"
            f"delete_duplicates = {b(self.delete_duplicates)}\n"
            f"delete_junk = {b(self.delete_junk)}\n"
            f"follow = {b(self.follow)}\n"
            f'log_level = "{self.log_level}"\n'
            f"reorganize_depth = {self.reorganize_depth}\n"
            f"reorganize_min_confidence = {self.reorganize_min_confidence}\n"
            f'rules_path = "{self.rules_file}"\n'
            f'junk_rules_path = "{self.junk_file}"\n'
            f"allow_new_ids = {b(self.allow_new_ids)}\n"
            f"allow_new_categories = {b(self.allow_new_categories)}\n"
            f"new_id_min_confidence = {self.new_id_min_confidence}\n"
            f"folder_mode = {b(self.folder_mode)}\n"
            f"folder_min_files = {self.folder_min_files}\n"
            f"folder_max_files = {self.folder_max_files}\n"
            f"folder_min_confidence = {self.folder_min_confidence}\n"
            f'date_folders = "{self.date_folders}"\n'
            f"date_folder_ids = [{', '.join(json.dumps(i, ensure_ascii=False) for i in self.date_folder_ids)}]\n"
            f"\n[scan]\n"
            f"include = [{inc}]\n"
            f"exclude = [\n  {exc}\n]\n"
        )


def packaged_prompt() -> str:
    return importlib.resources.files("sorto").joinpath("prompts/classify.md").read_text(encoding="utf-8")


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def grok_config_path() -> Path:
    home = os.environ.get("GROK_HOME")
    return (Path(home) if home else Path.home() / ".grok") / "config.toml"


def grok_profile(model: str, path: Path | None = None) -> tuple[dict[str, Any], str]:
    """Find the Grok Build model profile that serves *model*.

    Returns (llm_table, description). Grok's context window, endpoint and
    timeouts were tuned for this machine's GPU; sorto reuses them so both tools
    share one loaded model with one KV-cache size and neither forces a reload.
    Grok's temperature is not reused: sorto wants steadier answers.
    """
    path = path or grok_config_path()
    try:
        data = _load_toml(path)
    except (OSError, tomllib.TOMLDecodeError):
        return {}, "built-in defaults"
    profiles = data.get("model") or {}
    for name, prof in profiles.items():
        if not isinstance(prof, dict) or prof.get("model") != model:
            continue
        out: dict[str, Any] = {}
        if prof.get("base_url"):
            out["url"] = prof["base_url"]
        if prof.get("context_window"):
            out["context_window"] = int(prof["context_window"])
        if prof.get("inference_idle_timeout_secs"):
            out["timeout_sec"] = float(prof["inference_idle_timeout_secs"])
        if prof.get("max_retries") is not None:
            out["max_retries"] = int(prof["max_retries"])
        if prof.get("top_p") is not None:
            out["top_p"] = float(prof["top_p"])
        return out, f"grok profile {name!r} in {path}"
    return {}, "built-in defaults"


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def grok_local_server(path: Path | None = None) -> tuple[str, str]:
    """The local model server Grok Build uses, for a model it has no profile for.

    Prefers the profile of sorto's default model, then any profile whose
    base_url is on this machine. Returns (url, description) or ("", "").
    """
    path = path or grok_config_path()
    try:
        profiles = _load_toml(path).get("model") or {}
    except (OSError, tomllib.TOMLDecodeError):
        return "", ""
    local = [
        (name, prof)
        for name, prof in profiles.items()
        if isinstance(prof, dict) and urlparse(str(prof.get("base_url") or "")).hostname in LOOPBACK_HOSTS
    ]
    local.sort(key=lambda np: np[1].get("model") != DEFAULT_LLM_MODEL)
    if not local:
        return "", ""
    name, prof = local[0]
    return str(prof["base_url"]), f"server of grok profile {name!r} in {path}"


LLM_KEYS = {
    "url": "llm_url",
    "model": "llm_model",
    "api_key": "llm_api_key",
    "context_window": "context_window",
    "temperature": "temperature",
    "top_p": "top_p",
    "max_tokens": "max_tokens",
    "reasoning_effort": "reasoning_effort",
    "timeout_sec": "timeout_sec",
    "max_retries": "max_retries",
    "structure_model": "structure_model",
}
RUN_KEYS = {
    f.name
    for f in fields(SortoConfig)
    if f.name
    not in {"source", "target", "include", "exclude", "retry_errors", "retry_kept", "clear_cache", "reorganize_scope", "llm_profile_source", "models"}
    and not f.name.startswith("llm_")
    and f.name not in LLM_KEYS.values()
}


def _apply_table(cfg: SortoConfig, data: dict[str, Any]) -> SortoConfig:
    llm = data.get("llm") or {}
    run = data.get("run") or {}
    scan = data.get("scan") or {}
    updates: dict[str, Any] = {}
    for src, dest in LLM_KEYS.items():
        if llm.get(src) is not None:
            updates[dest] = llm[src]
    if llm.get("models"):
        updates["models"] = [str(m) for m in llm["models"]]
    for key in RUN_KEYS:
        if run.get(key) is not None:
            updates[key] = run[key]
    if scan.get("include") is not None:
        updates["include"] = list(scan["include"])
    for item in scan.get("exclude") or []:
        if item not in cfg.exclude:
            cfg.exclude.append(item)
    return replace(cfg, **updates) if updates else cfg


def ensure_user_config() -> Path:
    path = user_config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    return path


def ensure_state_dir(cfg: SortoConfig) -> Path:
    st = cfg.state
    st.mkdir(parents=True, exist_ok=True)
    scope = f'reorganize_scope = "{cfg.reorganize_scope}"\n' if cfg.reorganize_scope else ""
    (st / "run.toml").write_text(
        f'source = "{cfg.source}"\ntarget = "{cfg.target}"\n{scope}', encoding="utf-8"
    )
    if not cfg.progress_path.exists():
        cfg.progress_path.touch()
    return st


def scope_in_target(source: Path, target: Path) -> str:
    """SOURCE is an area or a category of TARGET: the part to reorganize, as a path below TARGET.

    Such a folder holds IDs, and its files mostly belong where they are.
    Treating it as an inbox would hide its own IDs from the model and empty
    it into the rest of the archive. A source that is an ID folder (or lies
    inside one), or a folder with no IDs in it, is not a scope: that is an
    inbox, sorted out of where it is.
    """
    from sorto.jd import ID_RE

    try:
        parts = source.relative_to(target).parts
    except ValueError:
        return ""
    if not parts or any(ID_RE.match(p) for p in parts):
        return ""

    def dirs(path: Path) -> list[Path]:
        try:
            return [p for p in path.iterdir() if p.is_dir() and not p.is_symlink()]
        except OSError:
            return []

    inner = dirs(source)
    if any(ID_RE.match(d.name) for d in inner) or any(ID_RE.match(e.name) for d in inner for e in dirs(d)):
        return "/".join(parts)
    return ""


def validate_roots(source: Path, target: Path) -> None:
    """Both must be existing directories.

    The same directory for both means *reorganize it in place*, and so does
    a source that is an area or a category of the target, for that part
    only (see ``scope_in_target``). Other nesting is allowed either way: a
    source inside the target (e.g. an inbox ID) is left out of the JD
    choices, and a target inside the source is left out of the scan.
    """
    if not source.is_dir():
        raise ValueError(f"source is not a directory: {source}")
    if not target.is_dir():
        raise ValueError(f"target is not a directory: {target}")


def load_config(
    source: Path,
    target: Path,
    *,
    cli: argparse.Namespace | None = None,
    grok_path: Path | None = None,
) -> SortoConfig:
    source = Path(source).expanduser().resolve()
    target = Path(target).expanduser().resolve()
    scope = scope_in_target(source, target)
    cfg = SortoConfig(source=target if scope else source, target=target, reorganize_scope=scope)

    cli_model = getattr(cli, "llm_model", None) if cli is not None else None
    env_model = os.environ.get("SORTO_LLM_MODEL")
    user = _load_toml(user_config_path())
    model = cli_model or env_model or (user.get("llm") or {}).get("model") or cfg.llm_model
    grok, grok_desc = grok_profile(str(model), grok_path)
    if grok:
        cfg = _apply_table(cfg, {"llm": grok})
        cfg.llm_profile_source = grok_desc
    else:
        # A model Grok has no profile for (e.g. a sorto-only tag) lives on the same local server.
        server, where = grok_local_server(grok_path)
        if server:
            cfg.llm_url = server
            cfg.llm_profile_source = where
    cfg = _apply_table(cfg, user)
    cfg = _apply_table(cfg, _load_toml(cfg.state / "config.toml"))

    env_url = os.environ.get("SORTO_LLM_URL")
    if env_url:
        cfg.llm_url = env_url
    if env_model:
        cfg.llm_model = env_model
    if os.environ.get("SORTO_LLM_API_KEY"):
        cfg.llm_api_key = os.environ["SORTO_LLM_API_KEY"]

    if cli is not None:
        if getattr(cli, "dry_run", False) or getattr(cli, "suggest_only", False):
            cfg.dry_run = True
        for flag in ("yes", "confirm", "delete_duplicates", "delete_junk"):
            if getattr(cli, flag, False):
                setattr(cfg, flag, True)
        if getattr(cli, "retry_kept", False):
            cfg.retry_kept = True
        if getattr(cli, "clear_cache", False):
            cfg.clear_cache = True
        if getattr(cli, "no_vision", False):
            cfg.vision = False
        if getattr(cli, "no_rename", False):
            cfg.allow_rename = False
        if getattr(cli, "no_new_ids", False):
            cfg.allow_new_ids = False
        if getattr(cli, "no_new_categories", False):
            cfg.allow_new_categories = False
        if getattr(cli, "no_folders", False):
            cfg.folder_mode = False
        if getattr(cli, "scan_interval", None) is not None:
            cfg.scan_interval = float(cli.scan_interval)
        if getattr(cli, "once", False):
            cfg.follow = False
        elif getattr(cli, "follow", False):
            cfg.follow = True
        elif cfg.reorganize:
            # Reorganizing is one pass over the tree, not a watched inbox.
            cfg.follow = False
        if getattr(cli, "depth", None) is not None:
            cfg.reorganize_depth = int(cli.depth)
        if getattr(cli, "rules", None):
            cfg.rules_path = str(cli.rules)
        if getattr(cli, "junk_rules", None):
            cfg.junk_rules_path = str(cli.junk_rules)
        if getattr(cli, "include", None):
            cfg.include = list(cli.include)
        for item in getattr(cli, "exclude", None) or []:
            if item not in cfg.exclude:
                cfg.exclude.append(item)
        if getattr(cli, "max_file_mb", None) is not None:
            cfg.max_file_mb = int(cli.max_file_mb)
        if getattr(cli, "min_confidence", None) is not None:
            cfg.min_confidence = float(cli.min_confidence)
        if getattr(cli, "llm_url", None):
            cfg.llm_url = str(cli.llm_url)
        if cli_model:
            cfg.llm_model = str(cli_model)
        if getattr(cli, "log_level", None):
            cfg.log_level = str(cli.log_level)

    if cfg.llm_model not in cfg.models:
        cfg.models.insert(0, cfg.llm_model)
    if f"{STATE_DIR_NAME}/**" not in cfg.exclude:
        cfg.exclude.insert(0, f"{STATE_DIR_NAME}/**")
    cfg.identify_workers = max(1, int(cfg.identify_workers))
    cfg.scan_interval = max(0.5, float(cfg.scan_interval))
    cfg.context_window = max(4096, int(cfg.context_window))
    cfg.min_confidence = max(0.0, min(1.0, float(cfg.min_confidence)))
    cfg.preview_px = max(128, min(2048, int(cfg.preview_px)))
    cfg.video_frames = max(0, min(8, int(cfg.video_frames)))
    cfg.reorganize_depth = max(0, min(5, int(cfg.reorganize_depth)))
    cfg.reorganize_min_confidence = max(0.0, min(1.0, float(cfg.reorganize_min_confidence)))
    cfg.new_id_min_confidence = max(0.0, min(1.0, float(cfg.new_id_min_confidence)))
    cfg.folder_min_confidence = max(0.0, min(1.0, float(cfg.folder_min_confidence)))
    cfg.date_folders = str(cfg.date_folders).strip().lower()
    if cfg.date_folders not in ("own", "all", "off"):
        cfg.date_folders = "own"
    ids = cfg.date_folder_ids
    cfg.date_folder_ids = [str(i).strip() for i in ([ids] if isinstance(ids, str) else ids) if str(i).strip()]
    cfg.folder_min_files = max(2, int(cfg.folder_min_files))
    cfg.folder_max_files = max(cfg.folder_min_files, int(cfg.folder_max_files))
    return cfg


def config_for_model(cfg: SortoConfig, model: str, *, grok_path: Path | None = None) -> SortoConfig:
    """Connection settings for switching to *model* mid-run.

    Same precedence as load_config: built-in defaults, then the Grok Build
    profile of *that* model (its URL, context window, timeouts), then the
    user and pair config ``[llm]`` tables, then SORTO_LLM_URL.
    """
    base = SortoConfig(source=cfg.source, target=cfg.target)
    out = replace(cfg, llm_model=model, include=list(cfg.include), exclude=list(cfg.exclude), models=list(cfg.models))
    # Without a profile of its own, the model is on the server sorto already uses.
    for key in ("context_window", "timeout_sec", "max_retries", "top_p"):
        setattr(out, key, getattr(base, key))
    out.llm_profile_source = "built-in defaults"
    grok, desc = grok_profile(model, grok_path)
    if grok:
        out = _apply_table(out, {"llm": grok})
        out.llm_profile_source = desc
    for data in (_load_toml(user_config_path()), _load_toml(cfg.state / "config.toml")):
        llm = {k: v for k, v in (data.get("llm") or {}).items() if k not in ("model", "models")}
        out = _apply_table(out, {"llm": llm})
    if os.environ.get("SORTO_LLM_URL"):
        out.llm_url = os.environ["SORTO_LLM_URL"]
    out.context_window = max(4096, int(out.context_window))
    return out
