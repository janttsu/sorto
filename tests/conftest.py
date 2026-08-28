from __future__ import annotations

from pathlib import Path

import pytest

from sorto.config import load_config
from sorto.engine import Engine
from sorto.llm import FakeLLMClient


@pytest.fixture
def inbox(tmp_path: Path) -> Path:
    root = tmp_path / "inbox"
    root.mkdir()
    return root


@pytest.fixture
def cfg(inbox: Path):
    c = load_config(inbox)
    c.follow = False
    c.identify_workers = 1
    c.workers = 1
    c.scan_interval = 0.3
    c.hash_max_mb = 16
    return c


def make_engine(cfg, llm=None, **kwargs) -> Engine:
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    return Engine(cfg, llm=llm or FakeLLMClient())
