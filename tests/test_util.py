from __future__ import annotations

from pathlib import Path

import pytest

from sorto.eta import EtaTracker
from sorto.util import (
    UnsafePathError,
    exclusive_move,
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


def test_eta_unknown_until_enough_samples() -> None:
    eta = EtaTracker(min_samples=5)
    assert eta.eta_seconds(10) is None
    for i in range(4):
        eta.add(0.1, 0.2, 0.05)
    assert eta.eta_seconds(10) is None
    eta.add(0.1, 0.2, 0.05)
    val = eta.eta_seconds(10)
    assert val is not None
    assert val > 0
