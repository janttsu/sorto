"""Unpacked software (an extracted AppImage, a copied Unix root) stays whole, like a git repository."""

from __future__ import annotations

from pathlib import Path

from conftest import ROUTES, make_engine
from sorto.config import load_config
from sorto.engine import Engine
from sorto.folders import walk_folder
from sorto.jd import scan_jd
from sorto.llm import FakeLLMClient
from sorto.util import is_software_tree, kept_whole, whole_tree

INVOICES = "10-19 Life/13 Money/13.13 Invoices"


def _appimage(path: Path) -> Path:
    """What `--appimage-extract` leaves: AppRun, a desktop file and a Unix tree under usr/."""
    for d in ("usr/bin", "usr/lib/plugins", "usr/share/icons"):
        (path / d).mkdir(parents=True)
    (path / "AppRun").write_text("#!/bin/sh\n", encoding="utf-8")
    (path / "editor.desktop").write_text("[Desktop Entry]\nName=Editor\n", encoding="utf-8")
    (path / "usr/lib/plugins/invoice-export.so").write_bytes(b"\x7fELF")
    (path / "usr/share/icons/invoice.svg").write_text("<svg/>", encoding="utf-8")
    (path / "usr/share/README").write_text("Read me", encoding="utf-8")
    return path


def _files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def test_what_counts_as_a_software_package(tmp_path: Path) -> None:
    extracted = tmp_path / "squashfs-root"
    extracted.mkdir()
    runner = tmp_path / "editor"
    (runner / "usr").mkdir(parents=True)
    (runner / "AppRun").write_text("#!/bin/sh\n", encoding="utf-8")
    lone = tmp_path / "scripts"
    lone.mkdir()
    (lone / "AppRun").write_text("#!/bin/sh\n", encoding="utf-8")  # AppRun with no usr/ next to it
    rootfs = tmp_path / "old-laptop-root"
    for d in ("usr/lib", "usr/share"):
        (rootfs / d).mkdir(parents=True)
    notes = tmp_path / "notes"
    (notes / "usr").mkdir(parents=True)  # a folder called usr alone is not a system tree
    assert is_software_tree(extracted) and is_software_tree(runner) and is_software_tree(rootfs)
    assert not is_software_tree(notes) and not is_software_tree(tmp_path / "missing")
    assert not is_software_tree(lone)
    for jd_folder in ("10-19 Life", "13 Money", "13.13 Invoices"):  # the archive's own folders never are
        (tmp_path / jd_folder / "usr" / "lib").mkdir(parents=True)
        (tmp_path / jd_folder / "usr" / "bin").mkdir()
        (tmp_path / jd_folder / "AppRun").write_text("#!/bin/sh\n", encoding="utf-8")
        assert not is_software_tree(tmp_path / jd_folder)
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    assert kept_whole(tmp_path / "repo") == "git repository" and kept_whole(rootfs) == "software package"
    assert kept_whole(notes) == ""
    assert whole_tree(rootfs / "usr" / "lib" / "x.so", stop=tmp_path) == (rootfs.resolve(), "software package")
    assert whole_tree(notes / "usr" / "a.txt", stop=tmp_path) is None


def test_an_unpacked_appimage_in_the_source_is_left_whole(inbox: Path, target: Path, cfg) -> None:
    tree = _appimage(inbox / "old-editor" / "squashfs-root")
    (inbox / "old-editor" / "Editor-2.0-x86_64.AppImage").write_bytes(b"\x7fELF AppImage")
    (inbox / "invoice-loose.txt").write_text("Invoice", encoding="utf-8")
    before = _files(tree)
    llm = FakeLLMClient(routes=ROUTES)
    make_engine(cfg, llm=llm).run_until_idle(timeout=30)
    assert _files(tree) == before
    assert sorted(p.filename for p in llm.calls) == ["Editor-2.0-x86_64.AppImage", "invoice-loose.txt"]
    assert not any(target.rglob("invoice-export.so")) and not any(target.rglob("invoice.svg"))


def test_a_package_is_not_part_of_its_folders_profile(inbox: Path) -> None:
    _appimage(inbox / "old-editor" / "squashfs-root")
    (inbox / "old-editor" / "notes.txt").write_text("notes", encoding="utf-8")
    assert [f for f, _, _ in walk_folder(inbox, "old-editor", 100).files] == ["notes.txt"]


def test_nothing_is_filed_into_a_package_inside_an_id(target: Path) -> None:
    _appimage(target / INVOICES / "billing-app")
    assert "billing-app" not in scan_jd(target).get("13.13").subfolders


def test_the_last_check_before_a_move_names_the_package(inbox: Path, target: Path, cfg) -> None:
    _appimage(inbox / "old-editor" / "squashfs-root")
    _appimage(target / INVOICES / "billing-app")
    engine = make_engine(cfg)
    try:
        why = engine._repo_in_the_way("old-editor/squashfs-root/usr/share/README", f"{INVOICES}/README")
        assert why == (
            "inside the software package old-editor/squashfs-root: packages are kept whole, nothing in them is moved"
        )
        why = engine._repo_in_the_way("invoice.txt", f"{INVOICES}/billing-app/usr/invoice.txt")
        assert why == (
            f"{INVOICES}/billing-app/usr/invoice.txt is inside the software package {INVOICES}/billing-app: "
            "nothing is filed into a package"
        )
    finally:
        engine.db.close()


def test_a_stray_apprun_filed_into_an_id_does_not_close_the_id(inbox: Path, target: Path, cfg) -> None:
    """Pieces of a program filed one by one earlier, among them its AppRun, sit in an ID's root."""
    (target / INVOICES / "AppRun").write_text("#!/bin/sh\n", encoding="utf-8")
    (target / INVOICES / "usr" / "lib").mkdir(parents=True)
    (target / INVOICES / "usr" / "bin").mkdir()
    (inbox / "invoice-march.txt").write_text("Invoice", encoding="utf-8")
    engine = make_engine(cfg)
    try:
        assert engine._repo_in_the_way("invoice-march.txt", f"{INVOICES}/invoice-march.txt") == ""
    finally:
        engine.db.close()
    make_engine(cfg).run_until_idle(timeout=30)
    assert (target / INVOICES / "invoice-march.txt").is_file()


def test_reorganizing_never_reaches_into_a_package(target: Path) -> None:
    tree = _appimage(target / INVOICES / "billing-app")
    loose = target / INVOICES / "sick-leave.txt"
    loose.write_text("sick leave", encoding="utf-8")
    cfg = load_config(target, target)
    cfg.follow, cfg.identify_workers, cfg.scan_interval, cfg.reorganize_depth = False, 1, 0.3, 3
    llm = FakeLLMClient(routes=ROUTES)
    before = _files(tree)
    Engine(cfg, llm=llm).run_until_idle(timeout=30)
    assert _files(tree) == before
    assert [p.filename for p in llm.calls] == ["sick-leave.txt"] and not loose.exists()
