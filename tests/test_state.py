"""~/.sorto index: remembered across runs, deleted to start over; rules re-read while running."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from conftest import make_engine
from sorto.config import load_config
from sorto.rules import SECTION_TITLE
from sorto.util import state_dir, state_home


def test_state_defaults_to_dot_sorto(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SORTO_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert state_home() == tmp_path / "home" / ".sorto"


def test_old_state_is_moved_to_dot_sorto(inbox: Path, target: Path) -> None:
    new = state_dir(inbox, target)
    legacy = Path(os.environ["XDG_STATE_HOME"]) / "sorto" / new.name
    legacy.mkdir(parents=True)
    (legacy / "index.sqlite").write_bytes(b"old index")
    assert state_dir(inbox, target) == new
    assert (new / "index.sqlite").read_bytes() == b"old index"
    assert not legacy.exists()


def test_index_remembers_and_deleting_it_starts_over(inbox: Path, target: Path, cfg) -> None:
    (inbox / "notes.txt").write_text("nothing to see", encoding="utf-8")
    first = make_engine(cfg).run_until_idle(timeout=30)
    assert first.counts.needs_user == 1  # the fake model has no ID for it: kept
    assert str(cfg.db_path).startswith(os.environ["SORTO_HOME"])

    again = make_engine(load_config(inbox, target), follow=False, identify_workers=1, scan_interval=0.3)
    llm = again.llm
    again.run_until_idle(timeout=30)
    assert llm.calls == []  # remembered: not analyzed again

    again.db.close()
    cfg.db_path.unlink()  # the user starts over; -wal/-shm may be left behind
    fresh = make_engine(load_config(inbox, target), follow=False, identify_workers=1, scan_interval=0.3)
    fresh.run_until_idle(timeout=30)
    assert [p.filename for p in fresh.llm.calls] == ["notes.txt"]


def test_rules_are_re_read_while_running(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sorto.engine.RULES_RELOAD_SEC", 0.1)
    eng = make_engine(cfg, follow=True)
    assert SECTION_TITLE not in eng.system_prompt
    eng.start()
    try:
        cfg.rules_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.rules_file.write_text("- Anything about cats goes to 51.11.\n", encoding="utf-8")
        for _ in range(200):
            snap = eng.snapshot()
            if "cats" in eng.system_prompt and any("re-read" in line for line in snap.log_lines):
                break
            time.sleep(0.05)
        assert "Anything about cats goes to 51.11." in eng.system_prompt
        assert eng.snapshot().rules == 1
        assert any("re-read" in line for line in eng.snapshot().log_lines)
    finally:
        eng.request_stop()
        eng.join(timeout=10)
