from __future__ import annotations

from pathlib import Path

import pytest

from sorto.config import load_config
from sorto.engine import Engine
from sorto.llm import FakeLLMClient
from sorto.models import AnalysisPacket

JD_DIRS = [
    "00-09 System/00 Admin/00.00 JDex",
    "00-09 System/00 Admin/00.01 Inbox",
    "10-19 Life/11 Health/11.00 JDex",
    "10-19 Life/11 Health/11.01 Inbox",
    "10-19 Life/11 Health/11.12 Records/Sick notes",
    "10-19 Life/13 Money/13.01 Inbox",
    "10-19 Life/13 Money/13.13 Invoices/2023",
    "10-19 Life/13 Money/13.13 Invoices/2024",
    "50-59 Media/51 Pictures/51.11 Photos",
]


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests away from the real ~/.sorto, ~/.config, ~/.local/state and ~/.grok."""
    monkeypatch.setenv("SORTO_HOME", str(tmp_path / "sorto-home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "grok-home"))
    for var in ("SORTO_LLM_URL", "SORTO_LLM_MODEL", "SORTO_LLM_API_KEY"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def inbox(tmp_path: Path) -> Path:
    root = tmp_path / "inbox"
    root.mkdir()
    return root


@pytest.fixture
def target(tmp_path: Path) -> Path:
    root = tmp_path / "archive"
    for d in JD_DIRS:
        (root / d).mkdir(parents=True)
    (root / "00-09 System/00 Admin/00.00 JDex/00.00 JDex.md").write_text(
        "# JDex\n- `13.13` Invoices — all personal bills and receipts\n"
        "- `11.12` Records: patient records and sick notes\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def cfg(inbox: Path, target: Path):
    c = load_config(inbox, target)
    c.follow = False
    c.identify_workers = 1
    c.scan_interval = 0.3
    c.hash_max_mb = 16
    return c


ROUTES = {"invoice": "13.13", "sick": "11.12", "photo": "51.11"}


def make_engine(cfg, llm=None, **kwargs) -> Engine:
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    return Engine(cfg, llm=llm or FakeLLMClient(routes=ROUTES))


def packet(name: str = "file.txt", **kwargs) -> AnalysisPacket:
    kw = dict(
        src_rel=name,
        filename=name,
        extension=Path(name).suffix,
        size=12,
        mtime_iso="2024-01-01T00:00:00+00:00",
        mtime_ns=1_700_000_000_000_000_000,
        mime="text/plain",
        magic="ASCII text",
        type_guess="text/plain",
        hex_preview="",
        text_preview="hi",
        extra_meta={},
        sha256="abc",
        meaningless_name=False,
    )
    kw.update(kwargs)
    return AnalysisPacket(**kw)  # type: ignore[arg-type]
