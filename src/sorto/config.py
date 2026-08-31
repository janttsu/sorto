from __future__ import annotations

import argparse
import importlib.resources
import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from sorto import STATE_DIR_NAME
from sorto.library import looks_like_library_root
from sorto.util import state_dir, user_config_path

DEFAULT_LLM_URL = "http://127.0.0.1:11434/v1"
DEFAULT_LLM_MODEL = "qwen2.5-coder:14b"
DEFAULT_EXCLUDE = [
    "_organization/**",
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
# sorto configuration (per-root). Edit freely.
# User-wide defaults live in ~/.config/sorto/config.toml

[llm]
url = "http://127.0.0.1:11434/v1"
model = "qwen2.5-coder:14b"
api_key = ""
temperature = 0.1
max_tokens = 400
timeout_sec = 120

[run]
workers = 1
identify_workers = 4
scan_interval = 5
max_file_mb = 64
dest_scheme = "default"          # default | by-type | by-type-year | library
hash_max_mb = 256
allow_extension_fix = false
dry_run = false
yes = false
delete_duplicates = false   # delete hash duplicates; never inside a git repo
delete_junk = false         # delete cache/temp/junk; never inside a git repo
follow = true
log_level = "INFO"

[scan]
include = []
exclude = [
  "**/.git/**",
  "**/.svn/**",
  "**/.hg/**",
  "**/.Trash/**",
  "**/node_modules/**",
  "**/.venv/**",
]
"""


@dataclass
class SortoConfig:
    root: Path
    llm_url: str = DEFAULT_LLM_URL
    llm_model: str = DEFAULT_LLM_MODEL
    llm_api_key: str = ""
    temperature: float = 0.1
    max_tokens: int = 400
    timeout_sec: float = 120.0
    workers: int = 1
    identify_workers: int = 4
    scan_interval: float = 5.0
    max_file_mb: int = 64
    dest_scheme: str = "default"
    hash_max_mb: int = 256
    allow_extension_fix: bool = False
    dry_run: bool = False
    yes: bool = False
    delete_duplicates: bool = False
    delete_junk: bool = False
    follow: bool = True
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE))
    log_level: str = "INFO"
    retry_errors: bool = False

    @property
    def state(self) -> Path:
        return state_dir(self.root)

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
    def prompt_path(self) -> Path:
        return self.state / "prompts" / "classify.md"

    @property
    def root_config_path(self) -> Path:
        return self.state / "config.toml"

    def to_toml(self) -> str:
        inc = ", ".join(f'"{x}"' for x in self.include)
        exc = ",\n  ".join(f'"{x}"' for x in self.exclude)
        return (
            f"[llm]\n"
            f'url = "{self.llm_url}"\n'
            f'model = "{self.llm_model}"\n'
            f'api_key = "{self.llm_api_key}"\n'
            f"temperature = {self.temperature}\n"
            f"max_tokens = {self.max_tokens}\n"
            f"timeout_sec = {self.timeout_sec}\n"
            f"\n[run]\n"
            f"workers = {self.workers}\n"
            f"identify_workers = {self.identify_workers}\n"
            f"scan_interval = {self.scan_interval}\n"
            f"max_file_mb = {self.max_file_mb}\n"
            f'dest_scheme = "{self.dest_scheme}"\n'
            f"hash_max_mb = {self.hash_max_mb}\n"
            f"allow_extension_fix = {str(self.allow_extension_fix).lower()}\n"
            f"dry_run = {str(self.dry_run).lower()}\n"
            f"yes = {str(self.yes).lower()}\n"
            f"delete_duplicates = {str(self.delete_duplicates).lower()}\n"
            f"delete_junk = {str(self.delete_junk).lower()}\n"
            f"follow = {str(self.follow).lower()}\n"
            f'log_level = "{self.log_level}"\n'
            f"\n[scan]\n"
            f"include = [{inc}]\n"
            f"exclude = [\n  {exc}\n]\n"
        )


def packaged_prompt() -> str:
    return importlib.resources.files("sorto").joinpath("prompts/classify.md").read_text(encoding="utf-8")


def packaged_library_prompt() -> str:
    return (
        importlib.resources.files("sorto")
        .joinpath("prompts/classify-library.md")
        .read_text(encoding="utf-8")
    )


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def _apply_table(cfg: SortoConfig, data: dict[str, Any]) -> SortoConfig:
    llm = data.get("llm") or {}
    run = data.get("run") or {}
    scan = data.get("scan") or {}
    updates: dict[str, Any] = {}
    mapping = {
        "url": "llm_url",
        "model": "llm_model",
        "api_key": "llm_api_key",
        "temperature": "temperature",
        "max_tokens": "max_tokens",
        "timeout_sec": "timeout_sec",
    }
    for src, dest in mapping.items():
        if src in llm and llm[src] is not None:
            updates[dest] = llm[src]
    run_map = {
        "workers": "workers",
        "identify_workers": "identify_workers",
        "scan_interval": "scan_interval",
        "max_file_mb": "max_file_mb",
        "dest_scheme": "dest_scheme",
        "hash_max_mb": "hash_max_mb",
        "allow_extension_fix": "allow_extension_fix",
        "dry_run": "dry_run",
        "yes": "yes",
        "delete_duplicates": "delete_duplicates",
        "delete_junk": "delete_junk",
        "follow": "follow",
        "log_level": "log_level",
    }
    for src, dest in run_map.items():
        if src in run and run[src] is not None:
            updates[dest] = run[src]
    if "include" in scan and scan["include"] is not None:
        updates["include"] = list(scan["include"])
    if "exclude" in scan and scan["exclude"] is not None:
        updates["exclude"] = list(scan["exclude"])
    if updates:
        cfg = replace(cfg, **updates)
    return cfg


def ensure_state_dir(root: Path) -> Path:
    root = Path(root).expanduser().resolve()
    st = state_dir(root)
    (st / "prompts").mkdir(parents=True, exist_ok=True)
    (st / "cache").mkdir(parents=True, exist_ok=True)
    cfg_path = st / "config.toml"
    if not cfg_path.exists():
        cfg_path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    prompt = st / "prompts" / "classify.md"
    if not prompt.exists():
        prompt.write_text(packaged_prompt(), encoding="utf-8")
    lib_prompt = st / "prompts" / "classify-library.md"
    if not lib_prompt.exists():
        lib_prompt.write_text(packaged_library_prompt(), encoding="utf-8")
    progress = st / "progress.jsonl"
    if not progress.exists():
        progress.touch()
    return st


def load_config(root: Path, *, cli: argparse.Namespace | None = None) -> SortoConfig:
    root = Path(root).expanduser().resolve()
    ensure_state_dir(root)
    cfg = SortoConfig(root=root)
    cfg = _apply_table(cfg, _load_toml(user_config_path()))
    cfg = _apply_table(cfg, _load_toml(state_dir(root) / "config.toml"))

    if cli is not None:
        if getattr(cli, "dry_run", False) or getattr(cli, "suggest_only", False):
            cfg.dry_run = True
        if getattr(cli, "yes", False):
            cfg.yes = True
        if getattr(cli, "delete_duplicates", False):
            cfg.delete_duplicates = True
        if getattr(cli, "delete_junk", False):
            cfg.delete_junk = True
        if getattr(cli, "workers", None) is not None:
            cfg.workers = int(cli.workers)
        if getattr(cli, "scan_interval", None) is not None:
            cfg.scan_interval = float(cli.scan_interval)
        if getattr(cli, "once", False):
            cfg.follow = False
        elif getattr(cli, "follow", False):
            cfg.follow = True
        inc = getattr(cli, "include", None)
        if inc:
            cfg.include = list(inc)
        exc = getattr(cli, "exclude", None)
        if exc:
            # merge extra excludes
            seen = set(cfg.exclude)
            for item in exc:
                if item not in seen:
                    cfg.exclude.append(item)
                    seen.add(item)
        if getattr(cli, "max_file_mb", None) is not None:
            cfg.max_file_mb = int(cli.max_file_mb)
        if getattr(cli, "llm_url", None):
            cfg.llm_url = str(cli.llm_url)
        if getattr(cli, "llm_model", None):
            cfg.llm_model = str(cli.llm_model)
        if getattr(cli, "llm_api_key", None):
            cfg.llm_api_key = str(cli.llm_api_key)
        cli_scheme = getattr(cli, "dest_scheme", None)
        if cli_scheme:
            cfg.dest_scheme = str(cli_scheme)
        if getattr(cli, "log_level", None):
            cfg.log_level = str(cli.log_level)

    env_url = os.environ.get("SORTO_LLM_URL")
    env_model = os.environ.get("SORTO_LLM_MODEL")
    env_key = os.environ.get("SORTO_LLM_API_KEY")
    if env_url and (cli is None or not getattr(cli, "llm_url", None)):
        cfg.llm_url = env_url
    if env_model and (cli is None or not getattr(cli, "llm_model", None)):
        cfg.llm_model = env_model
    if env_key and (cli is None or not getattr(cli, "llm_api_key", None)):
        cfg.llm_api_key = env_key

    if STATE_DIR_NAME + "/**" not in cfg.exclude and "_organization/**" not in cfg.exclude:
        cfg.exclude.insert(0, "_organization/**")
    cfg.workers = max(1, int(cfg.workers))
    cfg.identify_workers = max(1, int(cfg.identify_workers))
    cfg.scan_interval = max(0.5, float(cfg.scan_interval))
    scheme = cfg.dest_scheme.strip().lower()
    if scheme not in {"default", "by-type", "by-type-year", "library"}:
        raise ValueError(f"unknown dest-scheme: {cfg.dest_scheme}")
    cli_forced = cli is not None and bool(getattr(cli, "dest_scheme", None))
    if scheme == "default" and not cli_forced and looks_like_library_root(root):
        scheme = "library"
    cfg.dest_scheme = scheme
    return cfg
