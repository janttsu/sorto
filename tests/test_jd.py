from __future__ import annotations

from pathlib import Path

from sorto.jd import normalize_id, scan_jd


def test_scan_finds_ids_not_index_folders(target: Path) -> None:
    index = scan_jd(target)
    ids = {it.id for it in index.choices()}
    assert {"00.01", "11.01", "11.12", "13.01", "13.13", "51.11"} <= ids
    assert "11.00" not in ids and index.get("00.00") is None
    item = index.get("13.13")
    assert item is not None
    assert item.rel == "10-19 Life/13 Money/13.13 Invoices"
    assert item.category == "13 Money" and item.area == "10-19 Life"
    assert item.subfolders == ["2023", "2024"]


def test_jdex_descriptions_are_read(target: Path) -> None:
    index = scan_jd(target)
    assert index.get("13.13").description == "all personal bills and receipts"
    assert index.get("11.12").description == "patient records and sick notes"
    outline = index.render()
    assert "13.13 Invoices — all personal bills and receipts" in outline
    # year folders are how files are stored: the outline says the ID has them, not which years
    assert "13.13 Invoices — all personal bills and receipts  [has year folders (YYYY)]" in outline
    assert "2023" not in outline and "2024" not in outline
    assert "00.00" not in outline


def test_root_level_ids_and_unrelated_dirs(tmp_path: Path) -> None:
    (tmp_path / "05.11 Shared folder").mkdir()
    (tmp_path / "random folder" / "22.22 Not an ID here").mkdir(parents=True)
    index = scan_jd(tmp_path)
    assert index.get("05.11") is not None
    assert index.get("22.22") is None


def test_resolve_dir_respects_existing_subfolders(target: Path) -> None:
    index = scan_jd(target)
    inv = index.get("13.13")
    assert index.resolve_dir(inv, "2024") == (f"{inv.rel}/2024", "2024")
    # New year folder is fine: the ID already uses year folders.
    assert index.resolve_dir(inv, "2025") == (f"{inv.rel}/2025", "2025")
    # Invented structure is dropped.
    assert index.resolve_dir(inv, "Electricity bills") == (inv.rel, "")
    assert index.resolve_dir(inv, "../../escape") == (inv.rel, "")
    rec = index.get("11.12")
    assert index.resolve_dir(rec, "Sick notes") == (f"{rec.rel}/Sick notes", "Sick notes")
    # No date convention under 11.12, so no new year folder.
    assert index.resolve_dir(rec, "2024") == (rec.rel, "")


def test_exclude_source_inside_target(target: Path) -> None:
    source = target / "10-19 Life/13 Money/13.01 Inbox"
    index = scan_jd(target, exclude=[source])
    assert index.get("13.01") is None
    assert index.get("13.13") is not None


def test_normalize_id() -> None:
    assert normalize_id("13.13") == "13.13"
    assert normalize_id("13.13 Invoices") == "13.13"
    assert normalize_id("ID 13.13") == "13.13"
    assert normalize_id("13") == ""


def test_outline_lists_named_subfolders_but_not_which_years_exist(tmp_path: Path) -> None:
    trip = tmp_path / "10-19 Life/14 Travel/14.12 Rome 2024"
    for sub in ("2023/04", "2024/06", "Notes", "Tickets"):
        (trip / sub).mkdir(parents=True)
    (tmp_path / "10-19 Life/14 Travel/14.13 Receipts/2024-05").mkdir(parents=True)
    index = scan_jd(tmp_path)
    outline = index.render()
    assert "14.12 Rome 2024  [subfolders: Notes, Tickets; has year folders (YYYY)]" in outline
    assert "14.13 Receipts  [has year-month folders (YYYY-MM)]" in outline
    assert "2023" not in outline and "2024-05" not in outline
    # the folders are still known to sorto, which is what files into them
    assert index.get("14.12").subfolders == ["2023", "2024", "Notes", "Tickets"]
    assert index.resolve_dir(index.get("14.12"), "2024")[1] == "2024"
