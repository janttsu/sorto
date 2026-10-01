from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from sorto.config import load_config, validate_roots

GROK = """
[models]
default = "qwen3.6-35b"

[model."qwen3.6-35b"]
model = "qwen3.6:35b-a3b"
base_url = "http://127.0.0.1:11435/v1"
context_window = 65536
inference_idle_timeout_secs = 1800
max_retries = 1
temperature = 1
top_p = 0.95

[model."other"]
model = "other:7b"
base_url = "http://127.0.0.1:9999/v1"
context_window = 8192
"""


def _write_grok() -> None:
    grok = Path(os.environ["GROK_HOME"])
    grok.mkdir(parents=True)
    (grok / "config.toml").write_text(GROK, encoding="utf-8")


def test_defaults_are_qwen36_local(inbox: Path, target: Path) -> None:
    cfg = load_config(inbox, target)
    assert cfg.llm_model == "qwen3.6:35b-a3b"
    assert cfg.llm_url.startswith("http://127.0.0.1:")
    assert cfg.reasoning_effort == "none"
    assert cfg.context_window == 65536


def test_grok_profile_is_reused(inbox: Path, target: Path) -> None:
    _write_grok()
    cfg = load_config(inbox, target)
    assert cfg.llm_url == "http://127.0.0.1:11435/v1"
    assert cfg.context_window == 65536 and cfg.timeout_sec == 1800
    assert cfg.temperature == 0.6  # grok's chat temperature is not reused
    assert "qwen3.6-35b" in cfg.llm_profile_source
    other = load_config(inbox, target, cli=SimpleNamespace(llm_model="other:7b"))
    assert other.context_window == 8192 and other.llm_url.endswith(":9999/v1")


def test_user_config_overrides_grok(inbox: Path, target: Path) -> None:
    _write_grok()
    user = Path(os.environ["XDG_CONFIG_HOME"]) / "sorto"
    user.mkdir(parents=True)
    (user / "config.toml").write_text("[llm]\ncontext_window = 32768\n", encoding="utf-8")
    assert load_config(inbox, target).context_window == 32768


def test_state_dir_is_per_pair_and_outside(inbox: Path, target: Path, tmp_path: Path) -> None:
    a = load_config(inbox, target).state
    other = tmp_path / "other"
    other.mkdir()
    b = load_config(other, target).state
    assert a != b
    assert str(a).startswith(os.environ["SORTO_HOME"])


def test_validate_roots(inbox: Path, target: Path, tmp_path: Path) -> None:
    validate_roots(inbox, target)
    validate_roots(target, target)  # same directory = reorganize in place
    with pytest.raises(ValueError):
        validate_roots(inbox, tmp_path / "missing")


def test_model_without_grok_profile_uses_grok_local_server(inbox: Path, target: Path) -> None:
    grok = Path(os.environ["GROK_HOME"])
    grok.mkdir(parents=True, exist_ok=True)
    (grok / "config.toml").write_text(
        '[model."cloud"]\nmodel = "grok-4"\nbase_url = "https://api.x.ai/v1"\n'
        '[model."big"]\nmodel = "qwen3.6:35b-a3b"\nbase_url = "http://127.0.0.1:11435/v1"\n',
        encoding="utf-8",
    )
    import argparse

    cfg = load_config(inbox, target, cli=argparse.Namespace(llm_model="qwen3.5:9b-16k"))
    assert cfg.llm_url == "http://127.0.0.1:11435/v1"
    assert "big" in cfg.llm_profile_source
