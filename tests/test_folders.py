"""Folders that belong together move as one unit; every file is still checked on its own."""

from __future__ import annotations

from pathlib import Path

from conftest import ROUTES, make_engine
from sorto.config import load_config
from sorto.folders import FolderAnswer, FolderProfile, outlier_reason, profile_from
from sorto.llm import FakeLLMClient

PHOTOS = "50-59 Media/51 Pictures/51.11 Photos"


def _unit(jd_id: str = "51.11", confidence: float = 0.95):
    def handler(packet, _prompt):
        return FolderAnswer(
            summary=f"Photos from one trip ({packet.files} files).",
            coherent=True,
            jd_id=jd_id,
            subfolder="",
            confidence=confidence,
            reason="one trip",
        )

    return handler


def _mixed(packet, _prompt):
    return FolderAnswer(
        summary="Unrelated downloads.", coherent=False, jd_id="", subfolder="", confidence=0.9, reason="mixed"
    )


def _trip(inbox: Path, name: str = "Trip Rome 2019", photos: int = 6) -> Path:
    trip = inbox / name
    (trip / "day2").mkdir(parents=True)
    for i in range(photos):
        (trip / f"IMG_{i:04d}.jpg").write_bytes(b"\xff\xd8\xff" + name.encode() + bytes([i]) * 64)
    (trip / "day2" / "IMG_9000.jpg").write_bytes(b"\xff\xd8\xff" + name.encode() + b"z" * 64)
    return trip


def test_coherent_folder_moves_as_a_whole(inbox: Path, target: Path, cfg) -> None:
    _trip(inbox)
    llm = FakeLLMClient(routes=ROUTES, folder_handler=_unit())
    snap = make_engine(cfg, llm=llm).run_until_idle(timeout=30)
    dest = target / PHOTOS / "Trip Rome 2019"
    assert sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()) == [
        *(f"IMG_{i:04d}.jpg" for i in range(6)),
        "day2/IMG_9000.jpg",
    ]
    assert len(llm.folder_calls) == 1 and llm.folder_calls[0].files == 7
    assert llm.calls == []  # no per-file model calls at all
    assert snap.counts.done == 7
    assert all("moves as a whole" in h.folder for h in snap.history)


def test_file_that_stands_out_is_sorted_on_its_own(inbox: Path, target: Path, cfg) -> None:
    trip = _trip(inbox, photos=12)
    (trip / "invoice-hotel.pdf").write_bytes(b"%PDF-1.4 hotel invoice")
    llm = FakeLLMClient(routes=ROUTES, folder_handler=_unit())
    make_engine(cfg, llm=llm).run_until_idle(timeout=30)
    assert (target / PHOTOS / "Trip Rome 2019" / "IMG_0000.jpg").is_file()
    assert not (target / PHOTOS / "Trip Rome 2019" / "invoice-hotel.pdf").exists()
    assert (target / "10-19 Life/13 Money/13.13 Invoices/invoice-hotel.pdf").is_file()
    assert [p.filename for p in llm.calls] == ["invoice-hotel.pdf"]
    assert "stands out" in llm.calls[0].folder_context


def test_mixed_folder_goes_file_by_file_with_context(inbox: Path, target: Path, cfg) -> None:
    mixed = inbox / "Downloads 2020"
    mixed.mkdir()
    for name in ("invoice-a.txt", "invoice-b.txt", "sick-note.txt", "photo-x.jpg", "notes.txt"):
        (mixed / name).write_text(name, encoding="utf-8")
    llm = FakeLLMClient(routes=ROUTES, folder_handler=_mixed)
    make_engine(cfg, llm=llm).run_until_idle(timeout=30)
    assert len(llm.folder_calls) == 1
    assert len(llm.calls) == 5
    assert all("Unrelated downloads" in p.folder_context for p in llm.calls)
    assert (target / "10-19 Life/13 Money/13.13 Invoices/invoice-a.txt").is_file()


def test_small_folders_are_not_asked_about(inbox: Path, target: Path, cfg) -> None:
    small = inbox / "few"
    small.mkdir()
    for i in range(3):
        (small / f"invoice-{i}.txt").write_text(str(i), encoding="utf-8")
    llm = FakeLLMClient(routes=ROUTES, folder_handler=_unit())
    make_engine(cfg, llm=llm).run_until_idle(timeout=30)
    assert llm.folder_calls == [] and len(llm.calls) == 3


def test_big_folder_is_split_into_its_subfolders(inbox: Path, target: Path, cfg) -> None:
    for trip in ("Rome", "Oslo"):
        _trip(inbox / "Travel", name=trip, photos=5)  # 6 files each, 12 under Travel
    llm = FakeLLMClient(routes=ROUTES, folder_handler=_unit())
    make_engine(cfg, llm=llm, folder_max_files=8).run_until_idle(timeout=30)
    assert sorted(p.rel for p in llm.folder_calls) == ["Travel/Oslo", "Travel/Rome"]
    assert (target / PHOTOS / "Oslo" / "day2" / "IMG_9000.jpg").is_file()
    assert (target / PHOTOS / "Rome" / "IMG_0004.jpg").is_file()


def test_existing_folder_name_is_never_merged(inbox: Path, target: Path, cfg) -> None:
    (target / PHOTOS / "Trip Rome 2019").mkdir()
    (target / PHOTOS / "Trip Rome 2019" / "old.jpg").write_bytes(b"old")
    _trip(inbox)
    make_engine(cfg, llm=FakeLLMClient(folder_handler=_unit())).run_until_idle(timeout=30)
    assert sorted(p.name for p in (target / PHOTOS / "Trip Rome 2019").iterdir()) == ["old.jpg"]
    assert (target / PHOTOS / "Trip Rome 2019-2" / "IMG_0001.jpg").is_file()


def test_folder_decision_is_remembered(inbox: Path, target: Path, cfg) -> None:
    trip = _trip(inbox)
    llm = FakeLLMClient(folder_handler=_unit(confidence=0.5))  # too unsure: file by file
    make_engine(cfg, llm=llm).run_until_idle(timeout=30)
    (trip / "IMG_7777.jpg").write_bytes(b"\xff\xd8\xff new")
    again = FakeLLMClient(folder_handler=_unit())
    make_engine(load_config(inbox, target), llm=again, follow=False, identify_workers=1).run_until_idle(timeout=30)
    assert again.folder_calls == []  # the "mixed" decision from the first run still stands


def test_dry_run_moves_nothing_and_remembers_nothing(inbox: Path, target: Path, cfg) -> None:
    _trip(inbox)
    llm = FakeLLMClient(folder_handler=_unit())
    snap = make_engine(cfg, llm=llm, dry_run=True).run_until_idle(timeout=30)
    assert not (target / PHOTOS / "Trip Rome 2019").exists()
    assert snap.counts.planned == 7
    assert make_engine(load_config(inbox, target), llm=FakeLLMClient()).db.folder_get("Trip Rome 2019") is None


def test_no_folders_flag(inbox: Path, target: Path, cfg) -> None:
    _trip(inbox)
    llm = FakeLLMClient(routes=ROUTES, folder_handler=_unit())
    make_engine(cfg, llm=llm, folder_mode=False).run_until_idle(timeout=30)
    assert llm.folder_calls == [] and len(llm.calls) == 7


def test_outlier_checks_on_exif() -> None:
    exifs = [
        {"DateTimeOriginal": f"2020:01:{d:02d} 10:00:00", "GPSLatitude": "41.90", "GPSLongitude": "12.50"}
        for d in range(1, 11)
    ]
    trip = profile_from(10, {"image": 10}, exifs)
    assert trip.date_min == "2020-01-01" and trip.gps_lat is not None
    assert outlier_reason("a.jpg", {"DateTimeOriginal": "2020:01:05 12:00:00"}, trip) == ""
    assert "outside" in outlier_reason("b.jpg", {"DateTimeOriginal": "2017:06:01 12:00:00"}, trip)
    far = {"DateTimeOriginal": "2020:01:05 12:00:00", "GPSLatitude": "48.85", "GPSLongitude": "2.35"}
    assert "km away" in outlier_reason("c.jpg", far, trip)
    downloads = profile_from(20, {"video": 20}, [{"CreateDate": "2020:01:03"}] * 8)  # no camera recorded
    assert "taken with" in outlier_reason("mine.mp4", {"Make": "samsung", "Model": "S21"}, downloads)
    assert outlier_reason("clip.mp4", {"CreateDate": "2020:01:04"}, downloads) == ""
    assert "document file among video" in outlier_reason("x.pdf", {}, downloads)
    assert FolderProfile.from_json(trip.to_json()) == trip


def test_profile_is_not_fooled_by_the_outlier_itself() -> None:
    paris = [{"DateTimeOriginal": f"2025:06:1{d} 12:00:00", "GPSLatitude": "48.86", "GPSLongitude": f"2.3{d}"}
             for d in range(8)]
    odd = {"DateTimeOriginal": "2017:05:02 10:00:00", "GPSLatitude": "41.90", "GPSLongitude": "12.50"}
    prof = profile_from(9, {"image": 9}, [*paris, odd])  # the sample includes the odd one
    assert prof.date_min == "2025-06-10"
    assert "outside" in outlier_reason("IMG_0042.jpg", odd, prof)
    assert "km away" in outlier_reason("x.jpg", {**odd, "DateTimeOriginal": "2025:06:12 10:00:00"}, prof)
    videos = [{"CreateDate": f"2020:01:0{i}"} for i in range(1, 7)]
    phone = {"CreateDate": "2020:01:15", "Make": "samsung", "Model": "SM-G991B"}
    dump = profile_from(7, {"video": 7}, [*videos, phone])
    assert "taken with samsung" in outlier_reason("VID_20200115.mp4", phone, dump)



def _again(inbox: Path, target: Path, llm, **kw):
    cfg = load_config(inbox, target)
    return make_engine(cfg, llm=llm, follow=False, identify_workers=1, **kw)


def test_clear_cache_forgets_folder_decisions(inbox: Path, target: Path, cfg) -> None:
    trip = _trip(inbox)
    make_engine(cfg, llm=FakeLLMClient(folder_handler=_unit(confidence=0.5))).run_until_idle(timeout=30)
    (trip / "IMG_7777.jpg").write_bytes(b"\xff\xd8\xff new")
    again = FakeLLMClient(folder_handler=_unit())
    engine = _again(inbox, target, again, clear_cache=True)
    snap = engine.run_until_idle(timeout=30)
    assert [p.rel for p in again.folder_calls] == ["Trip Rome 2019"]  # asked again, and now it moves whole
    assert (target / PHOTOS / "Trip Rome 2019" / "IMG_7777.jpg").is_file()
    line = next(ln for ln in snap.log_lines if "--clear-cache: forgot" in ln)
    assert "folder decision(s)" in line and " 0 folder decision(s)" not in line and "keep theirs" not in line


def test_clear_cache_keeps_the_decision_of_a_folder_already_partly_moved(inbox: Path, target: Path, cfg) -> None:
    trip = _trip(inbox)
    make_engine(cfg, llm=FakeLLMClient(folder_handler=_unit())).run_until_idle(timeout=30)
    assert (target / PHOTOS / "Trip Rome 2019" / "IMG_0001.jpg").is_file()
    (trip / "IMG_7777.jpg").write_bytes(b"\xff\xd8\xff late")  # the rest of the folder turns up later
    again = FakeLLMClient(folder_handler=_mixed)
    snap = _again(inbox, target, again, clear_cache=True).run_until_idle(timeout=30)
    assert again.folder_calls == [] and again.calls == []  # not judged again: it follows the folder
    assert (target / PHOTOS / "Trip Rome 2019" / "IMG_7777.jpg").is_file()
    assert not (target / PHOTOS / "Trip Rome 2019-2").exists()
    assert any("already partly moved keep theirs" in line for line in snap.log_lines)


def test_clear_cache_forgets_cached_model_answers(inbox: Path, target: Path, cfg) -> None:
    from sorto.cli import build_parser

    (inbox / "mystery.txt").write_text("no keyword here", encoding="utf-8")
    first = FakeLLMClient(routes=ROUTES)
    make_engine(cfg, llm=first).run_until_idle(timeout=30)
    assert len(first.calls) == 1 and (inbox / "mystery.txt").is_file()  # kept: the fake found no ID
    cached = FakeLLMClient(routes=ROUTES)
    _again(inbox, target, cached, retry_kept=True).run_until_idle(timeout=30)
    assert cached.calls == []  # same file, same prompt: the cached answer is used
    fresh = FakeLLMClient(routes=ROUTES)
    engine = _again(inbox, target, fresh, retry_kept=True, clear_cache=True)
    snap = engine.run_until_idle(timeout=30)
    assert len(fresh.calls) == 1
    assert any("--clear-cache: forgot 1 cached model answer(s)" in line for line in snap.log_lines)
    assert (inbox / "mystery.txt").is_file()  # nothing about the files themselves was forgotten or moved
    args = build_parser().parse_args(["run", str(inbox), "-t", str(target), "--clear-cache"])
    assert load_config(inbox, target, cli=args).clear_cache is True and load_config(inbox, target).clear_cache is False
