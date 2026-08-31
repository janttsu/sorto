from __future__ import annotations

from pathlib import Path

from sorto.library import is_backup_garbage, looks_like_library_root, route_library, year_month
from sorto.models import AnalysisPacket


def _pkt(name: str, **kwargs) -> AnalysisPacket:
    ext = Path(name).suffix
    kw = dict(
        src_rel=name,
        filename=name,
        extension=ext,
        size=100,
        mtime_iso="2024-08-15T12:00:00+00:00",
        mtime_ns=1_723_720_800_000_000_000,  # 2024-08-15
        mime=None,
        magic=None,
        type_guess=None,
        hex_preview="",
        text_preview="",
        extra_meta={},
        sha256="ab",
        top_level_folders=[],
        dest_scheme="library",
        meaningless_name=False,
    )
    kw.update(kwargs)
    return AnalysisPacket(**kw)  # type: ignore[arg-type]


def test_year_month() -> None:
    y, ym = year_month(1_723_720_800_000_000_000)
    assert y == "2024"
    assert ym == "2024-08"


def test_photo_and_screenshot_routing() -> None:
    d = route_library(_pkt("IMG_1234.jpg"))
    assert d == "Photos/2024/2024-08/IMG_1234.jpg"
    d = route_library(_pkt("Screenshot_2024-08-01.png"))
    assert d == "Photos/2024/2024-08-Screenshots/Screenshot_2024-08-01.png"
    d = route_library(_pkt("signal-2024-08-01-120000.jpg"))
    assert "Signal" in (d or "")
    d = route_library(_pkt("IMG-20240801-WA0001.jpg"))
    assert "WhatsApp" in (d or "")


def test_invoice_goes_to_johnny_decimal() -> None:
    d = route_library(_pkt("kuitti-kauppa-2024.pdf"))
    assert d is not None
    assert "13.11 Invoices" in d
    assert d.endswith("kuitti-kauppa-2024.pdf")
    d = route_library(_pkt("fortum-lasku.pdf"))
    assert d is not None
    assert "12.21 Electricity" in d


def test_unknown_pdf_goes_to_quarry() -> None:
    d = route_library(_pkt("random-notes.pdf"))
    assert d is not None
    assert d.startswith("Documents/_quarry/2024/2024-08/")


def test_junk_and_garbage() -> None:
    j = route_library(_pkt(".DS_Store", is_junk=True, junk_reason="known junk filename"))
    assert j is not None
    assert j.startswith("TempAndCache/2024/2024-08/")
    assert is_backup_garbage("iwlwifi-7260-17.ucode", "x")
    assert route_library(_pkt("iwlwifi-7260-17.ucode")).startswith("Backup-Garbage/")


def test_looks_like_library_root(tmp_path: Path) -> None:
    assert not looks_like_library_root(tmp_path)
    (tmp_path / "Photos").mkdir()
    (tmp_path / "Documents").mkdir()
    (tmp_path / "Videos").mkdir()
    assert looks_like_library_root(tmp_path)


def test_keep_gitrepositories() -> None:
    d = route_library(_pkt("foo.py", src_rel="GitRepositories/algo-2025/foo.py"))
    assert d == "GitRepositories/algo-2025/foo.py"
