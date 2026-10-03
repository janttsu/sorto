"""A file that left the source before its turn (moved or deleted by someone else) is not an error."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from conftest import make_engine
from sorto.db import Database


def _remember(cfg, inbox: Path, rels: list[str]) -> None:
    """Files an earlier run saw but never got to, then removed from the source by the user."""
    cfg.state.mkdir(parents=True, exist_ok=True)
    db = Database(cfg.db_path)
    try:
        for rel in rels:
            db.upsert_discovered(src_rel=rel, abs_path=str(inbox / rel), size=4, mtime_ns=1, dev=None, ino=None)
    finally:
        db.close()


def test_files_gone_from_the_source_are_counted_apart_not_as_errors(inbox: Path, target: Path, cfg) -> None:
    _remember(cfg, inbox, [f"old-tool/lib/part-{i}.so" for i in range(30)] + ["stray.txt"])
    (inbox / "invoice-march.txt").write_text("Invoice", encoding="utf-8")
    engine = make_engine(cfg)
    snap = engine.run_until_idle(timeout=30)
    c = snap.counts
    assert c.error == 0 and c.gone == 31 and c.done == 1 and c.total == 1
    assert [v.src_rel for v in snap.history] == ["invoice-march.txt"]  # nothing to show for the gone ones
    text = engine.run_log.path.read_text(encoding="utf-8")
    assert "part-7.so" not in text  # one line for all of them, not one per file
    assert "gone: 31 file(s) were no longer in the source when their turn came (old-tool/ 30, (top level) 1)" in text
    assert text.rstrip().endswith(": 1 moved, 31 no longer in the source")


def test_a_gone_file_that_comes_back_is_sorted(inbox: Path, target: Path, cfg) -> None:
    _remember(cfg, inbox, ["invoice-april.txt"])
    make_engine(cfg).run_until_idle(timeout=30)
    (inbox / "invoice-april.txt").write_text("Invoice", encoding="utf-8")
    snap = make_engine(cfg).run_until_idle(timeout=30)
    assert snap.counts.gone == 0 and (target / "10-19 Life/13 Money/13.13 Invoices/invoice-april.txt").is_file()


def test_old_source_missing_errors_become_gone(tmp_path: Path) -> None:
    path = tmp_path / "index.sqlite"
    db = Database(path)
    db.upsert_discovered(src_rel="a.txt", abs_path="/x/a.txt", size=1, mtime_ns=1, dev=None, ino=None)
    db.upsert_discovered(src_rel="b.txt", abs_path="/x/b.txt", size=1, mtime_ns=1, dev=None, ino=None)
    db.close()
    raw = sqlite3.connect(path)
    raw.execute("UPDATE files SET status='error', error='source missing' WHERE src_rel='a.txt'")
    raw.execute("UPDATE files SET status='error', error='permission denied' WHERE src_rel='b.txt'")
    raw.commit()
    raw.close()
    db = Database(path)
    try:
        c = db.counts()
        assert (c.gone, c.error, c.total) == (1, 1, 1)
    finally:
        db.close()
