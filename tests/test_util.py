from __future__ import annotations

import os
from pathlib import Path

import pytest

from sorto.eta import EtaTracker, eta_text, file_kind, human_duration
from sorto.util import (
    UnsafePathError,
    exclusive_move,
    git_workdir,
    glob_match,
    is_meaningless_name,
    should_include,
    unique_dest,
    validate_dest_rel,
)


def test_validate_dest_rejects_dotdot() -> None:
    with pytest.raises(UnsafePathError):
        validate_dest_rel("../etc/passwd")
    with pytest.raises(UnsafePathError):
        validate_dest_rel("documents/../../outside.txt")
    with pytest.raises(UnsafePathError):
        validate_dest_rel("/abs/path.txt")
    with pytest.raises(UnsafePathError):
        validate_dest_rel("_organization/config.toml")


def test_validate_dest_keeps_extension() -> None:
    out = validate_dest_rel("Documents/Invoices/Scan.PDF", original_ext=".pdf")
    assert out.endswith(".pdf") or out.endswith(".PDF")
    assert ".." not in out
    assert "_organization" not in out.split("/")


def test_glob_and_exclude_organization() -> None:
    assert glob_match("documents/a.pdf", "**/*.pdf")
    assert glob_match("a.pdf", "*.pdf")
    assert should_include("notes.txt", [], ["_organization/**"])
    assert not should_include("_organization/config.toml", [], ["_organization/**"])
    assert not should_include("_organization/cache/x", [], ["_organization/**"])


def test_meaningless_names() -> None:
    assert is_meaningless_name("IMG_1234.jpg")
    assert is_meaningless_name("DSC0001.JPG")
    assert is_meaningless_name("untitled.txt")
    assert is_meaningless_name("download (3).pdf")
    assert is_meaningless_name("scan0001.pdf")
    assert not is_meaningless_name("vendor-invoice-2023.pdf")
    # Phone camera names carry the capture time and sort correctly: keep them.
    assert not is_meaningless_name("20240102_030405.jpg")
    assert not is_meaningless_name("IMG_20240612_101500.jpg")


def test_unique_dest_and_no_overwrite(tmp_path: Path) -> None:
    root = tmp_path
    dest = root / "name.txt"
    dest.write_text("keep", encoding="utf-8")
    other = root / "incoming.txt"
    other.write_text("new", encoding="utf-8")
    u = unique_dest(root, "name.txt", other)
    assert u != dest
    assert u.name == "name-2.txt"
    assert dest.read_text(encoding="utf-8") == "keep"
    # A file already sitting at its destination is not a collision with itself.
    assert unique_dest(root, "name.txt", dest) == dest


def test_exclusive_move_refuses_overwrite(tmp_path: Path) -> None:
    src = tmp_path / "src.txt"
    dest = tmp_path / "dest.txt"
    src.write_text("new", encoding="utf-8")
    dest.write_text("old", encoding="utf-8")
    with pytest.raises(FileExistsError):
        exclusive_move(src, dest)
    assert dest.read_text(encoding="utf-8") == "old"
    assert src.read_text(encoding="utf-8") == "new"


def test_git_workdir_detects_repo_and_not_plain_dir(tmp_path: Path) -> None:
    plain = tmp_path / "inbox" / "a.txt"
    plain.parent.mkdir()
    plain.write_text("x", encoding="utf-8")
    assert git_workdir(plain) is None
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / ".git").mkdir()
    tracked = repo / "src" / "main.py"
    tracked.parent.mkdir()
    tracked.write_text("x", encoding="utf-8")
    assert git_workdir(tracked) == repo.resolve()
    assert git_workdir(repo) == repo.resolve()


def test_eta_unknown_until_enough_samples() -> None:
    eta = EtaTracker(min_samples=3)
    assert eta.eta_seconds(10) is None
    eta.add(60.0, "document")  # first answer includes loading the model: not part of the pace
    eta.add(10.0, "document")
    eta.add(10.0, "document")
    assert eta.eta_seconds(10) is None
    eta.add(10.0, "document")
    assert eta.eta_seconds(10) == pytest.approx(100.0)


def test_eta_uses_pace_per_kind() -> None:
    eta = EtaTracker(min_samples=2)
    for _ in range(3):
        eta.add(4.0, "document")
        eta.add(16.0, "image")
    est = eta.estimate({"document": 10, "image": 5, "other": 2})
    # documents 10×4 + images 5×16 + unknown kind at the overall pace 2×10
    assert est.seconds == pytest.approx(40 + 80 + 20)
    assert est.per_file == pytest.approx(10.0)
    assert file_kind("a/IMG_1.JPG") == "image"
    assert file_kind("b/clip.mov") == "video"
    assert file_kind("c/bill.pdf") == "document"


def test_eta_text_mentions_pace_and_scan() -> None:
    from datetime import datetime

    from sorto.models import Counts, Snapshot

    snap = Snapshot(counts=Counts(pending=812), eta_s=3 * 3600 + 12 * 60, per_file_s=14.2, pace_samples=37)
    snap.scan_still_running = True
    text = eta_text(snap, now=datetime(2026, 1, 1, 12, 0))
    assert "~3h 12m left for 812 files at 14.2 s/file" in text
    assert "done ≈ 15:12" in text
    assert "scan has not reached" in text
    assert human_duration(2 * 86400 + 5 * 3600) == "2d 5h"


def _cross_device(monkeypatch: pytest.MonkeyPatch) -> None:
    import errno

    from sorto import util

    real = util._renameat2_noreplace

    def exdev(src, dest):
        if ".sorto-partial-" in os.path.basename(src):  # temp → final name, same directory
            return real(src, dest)
        raise OSError(errno.EXDEV, "Invalid cross-device link", src, None, dest)

    monkeypatch.setattr("sorto.util._renameat2_noreplace", exdev)


def test_cross_device_move_falls_back_to_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _cross_device(monkeypatch)
    tried: list[bool] = []
    monkeypatch.setattr("sorto.util._reflink", lambda s, d: tried.append(True) or False)
    src = tmp_path / "a.bin"
    src.write_bytes(b"x" * 3_000_000)
    dest = exclusive_move(src, tmp_path / "sub" / "b.bin")
    assert tried == [True]  # a clone is always tried first
    assert dest.read_bytes() == b"x" * 3_000_000 and not src.exists()


def test_cross_device_move_uses_reflink_when_possible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _cross_device(monkeypatch)

    def fake_clone(src_fd: int, dest_fd: int) -> bool:
        os.sendfile(dest_fd, src_fd, 0, os.fstat(src_fd).st_size)
        return True

    monkeypatch.setattr("sorto.util._reflink", fake_clone)
    src = tmp_path / "a.txt"
    src.write_text("cloned", encoding="utf-8")
    dest = exclusive_move(src, tmp_path / "b.txt")
    assert dest.read_text(encoding="utf-8") == "cloned" and not src.exists()


@pytest.mark.skipif(not os.environ.get("SORTO_BTRFS_TEST_DIRS"), reason="set SORTO_BTRFS_TEST_DIRS=src_dir:dest_dir")
def test_real_reflink_between_btrfs_subvolumes() -> None:
    """SORTO_BTRFS_TEST_DIRS=/mnt/a:/mnt/b with both on one btrfs, different subvolumes."""
    import uuid

    a, b = (Path(p) / f".sorto-reflink-{uuid.uuid4().hex[:8]}" for p in os.environ["SORTO_BTRFS_TEST_DIRS"].split(":"))
    a.mkdir()
    b.mkdir()
    try:
        src = a / "src.bin"
        data = os.urandom(8 << 20)
        src.write_bytes(data)
        from sorto.util import _reflink

        with open(src, "rb") as fi:
            fd = os.open(b / "probe.bin", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            try:
                assert _reflink(fi.fileno(), fd)
            finally:
                os.close(fd)
        dest = exclusive_move(src, b / "moved.bin")
        assert dest.read_bytes() == data and not src.exists()
    finally:
        import shutil

        shutil.rmtree(a, ignore_errors=True)
        shutil.rmtree(b, ignore_errors=True)
