"""Interrupted moves: no half-written files under real names; recovery finishes or flags them."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from conftest import make_engine
from sorto.util import exclusive_move, should_include

INV = "10-19 Life/13 Money/13.13 Invoices"


def _cross_device(monkeypatch: pytest.MonkeyPatch) -> None:
    real = __import__("sorto.util", fromlist=["x"])._renameat2_noreplace

    def rename(src, dest):
        # Renaming the finished temp file within the target dir still works.
        if ".sorto-partial-" in os.path.basename(src):
            return real(src, dest)
        raise OSError(errno.EXDEV, "Invalid cross-device link", src, None, dest)

    monkeypatch.setattr("sorto.util._renameat2_noreplace", rename)
    monkeypatch.setattr("sorto.util._reflink", lambda s, d: False)


def test_interrupted_copy_leaves_nothing_under_the_real_name(tmp_path: Path, monkeypatch) -> None:
    _cross_device(monkeypatch)
    src = tmp_path / "video.mp4"
    src.write_bytes(b"v" * 3_000_000)
    real_write = os.write
    calls = {"n": 0}

    def flaky_write(fd, data):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(errno.EIO, "disk went away")
        return real_write(fd, data)

    monkeypatch.setattr("sorto.util.os.write", flaky_write)
    dest_dir = tmp_path / "archive"
    with pytest.raises(OSError):
        exclusive_move(src, dest_dir / "video.mp4")
    assert src.read_bytes() == b"v" * 3_000_000
    assert list(dest_dir.iterdir()) == []  # no partial file, no temp left behind


def test_copy_goes_through_hidden_temp_then_real_name(tmp_path: Path, monkeypatch) -> None:
    _cross_device(monkeypatch)
    src = tmp_path / "a.txt"
    src.write_text("hello", encoding="utf-8")
    os.utime(src, (1_600_000_000, 1_600_000_000))
    dest = exclusive_move(src, tmp_path / "out" / "a.txt")
    assert dest.read_text(encoding="utf-8") == "hello" and not src.exists()
    assert int(dest.stat().st_mtime) == 1_600_000_000
    assert [p.name for p in (tmp_path / "out").iterdir()] == ["a.txt"]
    assert not should_include("x/.a.txt.sorto-partial-1234abcd", [], [])


def _moving_row(eng, inbox: Path, name: str, dest_rel: str) -> int:
    path = inbox / name
    st = path.stat()
    file_id, _ = eng.db.upsert_discovered(
        src_rel=name, abs_path=str(path), size=st.st_size, mtime_ns=st.st_mtime_ns, dev=st.st_dev, ino=st.st_ino
    )
    eng.db.update(file_id, status="moving", dest_rel=dest_rel)
    return file_id


def test_recover_finishes_move_when_copy_is_identical(inbox: Path, target: Path, cfg) -> None:
    (inbox / "clip.mp4").write_bytes(b"same bytes")
    (target / INV / "clip.mp4").write_bytes(b"same bytes")
    (target / INV / ".clip.mp4.sorto-partial-deadbeef").write_bytes(b"half")
    eng = make_engine(cfg)
    file_id = _moving_row(eng, inbox, "clip.mp4", f"{INV}/clip.mp4")
    eng.recover()
    row = eng.db.get(file_id)
    assert row["status"] == "done" and row["orig_rel"] == "clip.mp4"
    assert not (inbox / "clip.mp4").exists()
    assert (target / INV / "clip.mp4").read_bytes() == b"same bytes"
    assert sorted(p.name for p in (target / INV).iterdir() if p.is_file()) == ["clip.mp4"]


def test_recover_never_deletes_when_copy_differs(inbox: Path, target: Path, cfg) -> None:
    (inbox / "clip.mp4").write_bytes(b"the original")
    (target / INV / "clip.mp4").write_bytes(b"something else")
    eng = make_engine(cfg)
    file_id = _moving_row(eng, inbox, "clip.mp4", f"{INV}/clip.mp4")
    eng.recover()
    row = eng.db.get(file_id)
    assert row["status"] == "error" and "differs" in row["error"]
    assert (inbox / "clip.mp4").read_bytes() == b"the original"
    assert (target / INV / "clip.mp4").read_bytes() == b"something else"
