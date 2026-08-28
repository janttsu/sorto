from __future__ import annotations

from pathlib import Path

from sorto.config import DEFAULT_EXCLUDE, load_config
from sorto.scan import iter_regular_files


def test_scan_excludes_organization(inbox: Path) -> None:
    (inbox / "keep.txt").write_text("k", encoding="utf-8")
    org = inbox / "_organization"
    org.mkdir(exist_ok=True)
    (org / "secret.txt").write_text("nope", encoding="utf-8")
    nested = org / "cache"
    nested.mkdir(exist_ok=True)
    (nested / "x.bin").write_bytes(b"00")
    cfg = load_config(inbox)
    found = [rel for _, rel in iter_regular_files(inbox, include=cfg.include, exclude=cfg.exclude)]
    assert "keep.txt" in found
    assert all(not rel.startswith("_organization") for rel in found)


def test_scan_skips_symlinks_outside_root(inbox: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("out", encoding="utf-8")
    (inbox / "inside.txt").write_text("in", encoding="utf-8")
    link = inbox / "escape.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        return
    found = [rel for _, rel in iter_regular_files(inbox, include=[], exclude=list(DEFAULT_EXCLUDE))]
    assert "inside.txt" in found
    assert "escape.txt" not in found
