"""The user's own photos and videos are always filed by capture date: <ID>/YYYY/MM."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import make_engine, packet
from sorto.config import load_config
from sorto.engine import Engine
from sorto.folders import FolderAnswer, outlier_reason, parse_folder_answer, profile_from
from sorto.llm import FakeLLMClient, parse_classification
from sorto.media import camera_signs, capture_date, date_subfolder, device_of, name_date
from sorto.models import Classification

PHOTOS = "50-59 Media/51 Pictures/51.11 Photos"
INVOICES = "10-19 Life/13 Money/13.13 Invoices"
PHONE_VIDEO = {"CreateDate": "2023:08:15 10:21:00+00:00", "AndroidVersion": "13", "SamsungModel": "SM-G991B",
             "Author": "Galaxy S21"}


def _shot(day: str, model: str = "Pixel 8") -> dict[str, str]:
    return {"DateTimeOriginal": f"{day} 10:00:00", "Make": "Google", "Model": model}


@pytest.fixture
def exif(monkeypatch):
    """Pretend exiftool: EXIF by file name."""
    table: dict[str, dict[str, str]] = {}
    monkeypatch.setattr("sorto.media.read_exif", lambda path: dict(table.get(Path(path).name, {})))
    monkeypatch.setattr(
        "sorto.folders.read_exif_many", lambda paths: {str(p): dict(table.get(Path(p).name, {})) for p in paths}
    )
    return table


def _media(path: Path, mtime: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8\xff" + str(path).encode())  # unique content: no duplicates
    if mtime:
        import os
        from datetime import datetime

        ts = datetime.fromisoformat(mtime).timestamp()
        os.utime(path, (ts, ts))
    return path


def _model(jd_id: str = "51.11", own: bool | None = True, subfolder: str = "", confidence: float = 0.9):
    def handler(p, _prompt):
        return Classification(label="photo", confidence=confidence, jd_id=jd_id, reason="r", needs_user=False,
                              summary="s", subfolder=subfolder, own_media=own)

    return FakeLLMClient(handler)


def _files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


# ------------------------------------------------------------------ the date itself


def test_phone_videos_without_make_and_model_still_have_a_device() -> None:
    assert device_of(PHONE_VIDEO) == "SM-G991B"
    assert device_of({"AndroidVersion": "11"}) == "Android phone"
    assert device_of({"Make": "Apple", "Model": "iPhone 12"}) == "Apple iPhone 12"
    assert device_of({"CreateDate": "2020:01:01", "Encoder": "Lavf58"}) == ""


def test_camera_style_names() -> None:
    for name, day in [
        ("20230815_102030.mp4", "2023-08-15"),
        ("VID_20180203_040506.mp4", "2018-02-03"),
        ("VID20210405060708 (2).mp4", "2021-04-05"),
        ("IMG_20200104_123456.jpg", "2020-01-04"),
        ("PXL_20230101_123456789.jpg", "2023-01-01"),
        ("2022-03-04 05.06.07.mp4", "2022-03-04"),
    ]:
        assert f"{name_date(name):%Y-%m-%d}" == day, name
    for name in ("VID-20200101-WA0001.mp4", "IMG-20200101-WA0000.jpg", "Screenshot_20240102_030405_Browser.jpg",
                 "video_1@01-02-2020_03-04-05.mp4", "clip_video_2024-01-02-03-04-05.mp4", "20231399_102030.mp4",
                 "1500000000000 (2).mp4", "holiday.jpg"):
        assert name_date(name) is None, name


def test_camera_signs_are_evidence_not_a_verdict() -> None:
    assert camera_signs("video", PHONE_VIDEO, "clip.mp4") == "shot with SM-G991B"
    assert "GPS" in camera_signs("image", {"GPSLatitude": "41.9", "GPSLongitude": "12.5"}, "a.jpg")
    assert "file name" in camera_signs("video", {}, "VID_20210304_050607.mp4")
    assert "file name" in camera_signs("video", {"CreateDate": "2022:01:02 03:04:05"}, "DSC_0001.MP4")
    assert camera_signs("video", {}, "DSC_0001.MP4") == ""  # a counter name alone proves nothing
    assert camera_signs("video", {"CreateDate": "2020:01:01 12:00:00"}, "VID-20200101-WA0001.mp4") == ""
    assert camera_signs("document", _shot("2020:01:01"), "scan.pdf") == ""


def test_date_comes_from_metadata_then_name_and_never_from_mtime() -> None:
    assert date_subfolder("image", _shot("2021:07:04"), own=True, filename="IMG_20190101_101010.jpg") == "2021/07"
    assert date_subfolder("video", PHONE_VIDEO, own=True) == "2023/08"
    assert date_subfolder("video", {}, own=True, filename="VID_20210304_050607.mp4") == "2021/03"
    assert date_subfolder("image", {"Model": "X"}, own=True, filename="a.jpg") == ""  # no capture date anywhere
    # Dates no camera could have written are skipped, not trusted.
    broken = {"DateTimeOriginal": "0000:00:00 00:00:00", "CreateDate": "1904:01:01 00:00:00",
              "MediaCreateDate": "2018:03:09 12:00:00"}
    assert capture_date(broken).year == 2018
    assert date_subfolder("image", {"CreateDate": "2099:01:01 00:00:00"}, own=True) == ""
    assert date_subfolder("image", {}, own=True) == ""  # nothing to go by: no folder rather than a guess
    # Only own media, only photos and videos, only when switched on.
    assert date_subfolder("image", _shot("2021:07:04"), own=False) == ""
    assert date_subfolder("image", _shot("2021:07:04"), own=False, mode="all") == "2021/07"
    assert date_subfolder("", _shot("2021:07:04"), own=True) == ""
    assert date_subfolder("image", _shot("2021:07:04"), own=True, mode="off") == ""


def test_model_answers_carry_own_media() -> None:
    base = '{"summary": "s", "jd_id": "51.11", "confidence": 0.9'
    assert parse_classification(base + ', "own_media": true}').own_media is True
    assert parse_classification(base + ', "own_media": "false"}').own_media is False
    assert parse_classification(base + "}").own_media is None
    assert parse_folder_answer('{"coherent": true, "jd_id": "51.11", "own_media": true}').own_media is True
    assert parse_folder_answer('{"coherent": true, "jd_id": "51.11"}').own_media is False


def test_config(inbox: Path, target: Path, tmp_path: Path, monkeypatch) -> None:
    assert load_config(inbox, target).date_folders == "own"
    conf = tmp_path / "xdg-config" / "sorto" / "config.toml"
    conf.parent.mkdir(parents=True)
    conf.write_text('[run]\ndate_folders = "ALL"\n', encoding="utf-8")
    assert load_config(inbox, target).date_folders == "all"
    conf.write_text('[run]\ndate_folders = "sometimes"\n', encoding="utf-8")
    assert load_config(inbox, target).date_folders == "own"


# ------------------------------------------------------------------ single files


def test_own_photo_and_video_go_to_year_month(inbox: Path, target: Path, cfg, exif) -> None:
    exif["beach.jpg"] = _shot("2021:07:04")
    exif["clip.mp4"] = PHONE_VIDEO
    _media(inbox / "beach.jpg")
    _media(inbox / "deep" / "er" / "clip.mp4")
    llm = _model(subfolder="Summer/Holiday")  # a subfolder from the model never replaces the date
    snap = make_engine(cfg, llm=llm).run_until_idle(timeout=30)
    assert _files(target / PHOTOS) == ["2021/07/beach.jpg", "2023/08/clip.mp4"]
    assert snap.counts.done == 2
    assert all("camera_signs" in p.to_llm_dict() for p in llm.calls)


def test_the_model_decides_what_is_own(inbox: Path, target: Path, cfg, exif) -> None:
    # A picture inside downloaded software was shot with a camera too; it is not the user's.
    exif["cork.jpg"] = {**_shot("2010:04:08", "iPhone 3GS"), "GPSLatitude": "-27.5", "GPSLongitude": "153.0"}
    _media(inbox / "cork.jpg")
    make_engine(cfg, llm=_model(own=False)).run_until_idle(timeout=30)
    assert _files(target / PHOTOS) == ["cork.jpg"]


def test_when_the_model_says_nothing_a_camera_decides(inbox: Path, target: Path, cfg, exif) -> None:
    exif["mine.jpg"] = _shot("2019:12:31")
    _media(inbox / "mine.jpg")
    _media(inbox / "meme.jpg")
    _media(inbox / "VID_20210304_050607.mp4")
    make_engine(cfg, llm=_model(own=None)).run_until_idle(timeout=30)
    assert _files(target / PHOTOS) == ["2019/12/mine.jpg", "2021/03/VID_20210304_050607.mp4", "meme.jpg"]


def test_own_photo_without_a_capture_date_gets_no_date_folder(inbox: Path, target: Path, cfg, exif) -> None:
    """The file's mtime is the day it was copied or exported, not the day the picture was taken."""
    _media(inbox / "old-scan-of-print.jpg", mtime="2015-03-09T12:00:00")
    _media(inbox / "0123456789abcdef0123456789abcdef.jpg", mtime="2022-02-02T12:00:00")
    make_engine(cfg, llm=_model(own=True), date_folder_ids=["51.11 Photos"]).run_until_idle(timeout=30)
    assert _files(target / PHOTOS) == ["0123456789abcdef0123456789abcdef.jpg", "old-scan-of-print.jpg"]


def test_documents_are_never_filed_by_capture_date(inbox: Path, target: Path, cfg, exif) -> None:
    (inbox / "invoice.txt").write_text("Invoice 12 EUR", encoding="utf-8")
    make_engine(cfg, llm=_model("13.13", own=True)).run_until_idle(timeout=30)
    assert (target / INVOICES / "invoice.txt").is_file()


def test_same_name_in_the_same_month_keeps_both(inbox: Path, target: Path, cfg, exif) -> None:
    exif["IMG_0001.jpg"] = _shot("2021:07:04")
    old = target / PHOTOS / "2021" / "07" / "IMG_0001.jpg"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"the one already there")
    _media(inbox / "IMG_0001.jpg")
    cfg.allow_rename = False
    make_engine(cfg, llm=_model()).run_until_idle(timeout=30)
    assert old.read_bytes() == b"the one already there"
    assert _files(target / PHOTOS) == ["2021/07/IMG_0001-2.jpg", "2021/07/IMG_0001.jpg"]
    assert not (inbox / "IMG_0001.jpg").exists()


def test_new_id_gets_date_folders_too(inbox: Path, target: Path, cfg, exif) -> None:
    exif["dive.jpg"] = _shot("2022:02:11")
    _media(inbox / "dive.jpg")

    def handler(p, _prompt):
        return Classification(label="photo", confidence=0.95, jd_id="new", reason="r", needs_user=False,
                              summary="Diving photos.", new_id_category="51", new_id_name="Diving", own_media=True)

    make_engine(cfg, llm=FakeLLMClient(handler)).run_until_idle(timeout=30)
    assert _files(target / "50-59 Media/51 Pictures/51.12 Diving") == ["2022/02/dive.jpg"]


def test_switched_off(inbox: Path, target: Path, cfg, exif) -> None:
    exif["beach.jpg"] = _shot("2021:07:04")
    _media(inbox / "beach.jpg")
    make_engine(cfg, llm=_model(), date_folders="off").run_until_idle(timeout=30)
    assert _files(target / PHOTOS) == ["beach.jpg"]


def test_dry_run_plans_the_date_folder_and_moves_nothing(inbox: Path, target: Path, cfg, exif) -> None:
    exif["beach.jpg"] = _shot("2021:07:04")
    _media(inbox / "beach.jpg")
    snap = make_engine(cfg, llm=_model(), dry_run=True).run_until_idle(timeout=30)
    assert snap.history[-1].dest_rel == f"{PHOTOS}/2021/07/beach.jpg"
    assert (inbox / "beach.jpg").is_file() and not (target / PHOTOS / "2021").exists()


# ------------------------------------------------------------------ whole folders


def _folder(own: bool, jd_id: str = "51.11"):
    def handler(pkt, _prompt):
        return FolderAnswer(summary="One trip.", coherent=True, jd_id=jd_id, subfolder="", confidence=0.95,
                            reason="one trip", own_media=own)

    return handler


def _trip(inbox: Path, exif, n: int = 6) -> Path:
    trip = inbox / "Trip Rome 2019"
    for i in range(n):
        name = f"IMG_{i:04d}.jpg"
        exif[name] = _shot(f"2019:0{6 + i % 2}:1{i}")  # June and July
        _media(trip / ("day2" if i == n - 1 else "") / name)
    exif["VID_20190612_101010.mp4"] = {"CreateDate": "2019:06:12 10:10:10+00:00", "AndroidVersion": "9"}
    _media(trip / "VID_20190612_101010.mp4")
    (trip / "itinerary.txt").write_text("Day 1: Colosseum", encoding="utf-8")
    return trip


def test_own_photos_in_a_folder_go_by_date_without_asking_per_file(inbox: Path, target: Path, cfg, exif) -> None:
    _trip(inbox, exif)
    llm = FakeLLMClient(folder_handler=_folder(own=True))
    snap = make_engine(cfg, llm=llm).run_until_idle(timeout=30)
    assert _files(target / PHOTOS) == [
        "2019/06/IMG_0000.jpg", "2019/06/IMG_0002.jpg", "2019/06/IMG_0004.jpg", "2019/06/VID_20190612_101010.mp4",
        "2019/07/IMG_0001.jpg", "2019/07/IMG_0003.jpg", "2019/07/IMG_0005.jpg",  # day2/ is flattened by date
        "Trip Rome 2019/itinerary.txt",  # what is not a photo or video keeps the folder
    ]
    assert len(llm.folder_calls) == 1 and llm.calls == []
    assert snap.counts.done == 8
    assert any("filed by capture date (2019/07)" in h.reason for h in snap.history)


def test_folder_that_is_not_the_users_own_stays_whole(inbox: Path, target: Path, cfg, exif) -> None:
    # Example pictures in a source tree have cameras in their EXIF; the tree must not be pulled apart.
    _trip(inbox, exif)
    llm = FakeLLMClient(folder_handler=_folder(own=False))
    make_engine(cfg, llm=llm).run_until_idle(timeout=30)
    assert len(_files(target / PHOTOS / "Trip Rome 2019")) == 8
    assert (target / PHOTOS / "Trip Rome 2019" / "day2" / "IMG_0005.jpg").is_file()
    assert not (target / PHOTOS / "2019").exists()


def test_received_picture_among_own_photos_is_sorted_on_its_own(inbox: Path, target: Path, cfg, exif) -> None:
    trip = _trip(inbox, exif)
    _media(trip / "IMG-20190601-WA0001.jpg")  # no camera recorded, unlike the rest

    def one(p, _prompt):
        return Classification(label="meme", confidence=0.9, jd_id="13.13", reason="r", needs_user=False,
                              summary="s", own_media=False)

    llm = FakeLLMClient(one, folder_handler=_folder(own=True))
    make_engine(cfg, llm=llm).run_until_idle(timeout=30)
    assert [p.filename for p in llm.calls] == ["IMG-20190601-WA0001.jpg"]
    assert "no camera recorded" in llm.calls[0].folder_context
    assert (target / INVOICES / "IMG-20190601-WA0001.jpg").is_file()
    assert len(_files(target / PHOTOS / "2019")) == 7


def test_a_clip_among_many_own_photos_is_not_a_stranger(inbox: Path, target: Path, cfg, exif) -> None:
    _trip(inbox, exif, n=9)  # 9 photos, 1 video, 1 text file
    (inbox / "Trip Rome 2019" / "notes.txt").write_text("Day 2: Vatican", encoding="utf-8")
    llm = FakeLLMClient(folder_handler=_folder(own=True))
    make_engine(cfg, llm=llm).run_until_idle(timeout=30)
    assert llm.calls == []
    assert (target / PHOTOS / "2019" / "06" / "VID_20190612_101010.mp4").is_file()


def test_outlier_without_camera_needs_a_camera_folder() -> None:
    shots = profile_from(8, {"image": 8}, [_shot("2019:06:12")] * 8)
    assert "no camera recorded" in outlier_reason("funny.jpg", {}, shots)
    assert outlier_reason("IMG_20190612_101010.jpg", {}, shots) == ""  # named by a camera
    assert outlier_reason("x.jpg", _shot("2019:06:13", "Other"), shots) == ""
    downloads = profile_from(8, {"image": 8}, [{}] * 8)
    assert outlier_reason("funny.jpg", {}, downloads) == ""


def test_folder_decision_survives_a_restart(inbox: Path, target: Path, cfg, exif) -> None:
    trip = _trip(inbox, exif)
    make_engine(cfg, llm=FakeLLMClient(folder_handler=_folder(own=True))).run_until_idle(timeout=30)
    exif["IMG_0100.jpg"] = _shot("2019:06:20")
    _media(trip / "IMG_0100.jpg")
    again = FakeLLMClient()
    cfg2 = load_config(inbox, target)
    make_engine(cfg2, llm=again, follow=False, identify_workers=1).run_until_idle(timeout=30)
    assert again.folder_calls == [] and again.calls == []
    assert (target / PHOTOS / "2019" / "06" / "IMG_0100.jpg").is_file()


# ------------------------------------------------------------------ reorganizing in place


def _reorganize(target: Path, llm, **kw) -> Engine:
    cfg = load_config(target, target)
    cfg.follow, cfg.identify_workers, cfg.scan_interval = False, 1, 0.3
    for k, v in kw.items():
        setattr(cfg, k, v)
    return Engine(cfg, llm=llm)


def test_reorganize_moves_own_photos_into_their_month(target: Path, exif) -> None:
    exif["loose.jpg"] = exif["right.jpg"] = exif["wrong.jpg"] = _shot("2021:07:04")
    _media(target / PHOTOS / "loose.jpg")
    _media(target / PHOTOS / "2021" / "07" / "right.jpg")
    _media(target / PHOTOS / "2021" / "wrong.jpg")
    llm = _model()
    snap = _reorganize(target, llm, reorganize_depth=2).run_until_idle(timeout=30)
    assert _files(target / PHOTOS) == ["2021/07/loose.jpg", "2021/07/right.jpg", "2021/07/wrong.jpg"]
    assert snap.counts.done == 2  # right.jpg was already in place


def test_reorganize_default_depth_only_touches_the_id_root(target: Path, exif) -> None:
    exif["loose.jpg"] = exif["album.jpg"] = _shot("2021:07:04")
    _media(target / PHOTOS / "loose.jpg")
    _media(target / PHOTOS / "Wedding" / "album.jpg")
    _reorganize(target, _model()).run_until_idle(timeout=30)
    assert _files(target / PHOTOS) == ["2021/07/loose.jpg", "Wedding/album.jpg"]


def test_plan_marks_dated(target: Path) -> None:
    from sorto.jd import scan_jd
    from sorto.plan import plan_destination

    index = scan_jd(target)
    cls = Classification(label="photo", confidence=0.9, jd_id="51.11", reason="", needs_user=False)
    pkt = packet("a.jpg", media_kind="image", exif=_shot("2021:07:04"))
    kw = dict(src_path=target / "nowhere" / "a.jpg")
    plan = plan_destination(target, index, pkt, cls, own_media=True, date_folders="own", **kw)
    assert plan.dest_rel == f"{PHOTOS}/2021/07/a.jpg" and plan.dated and plan.subfolder == "2021/07"
    assert not plan_destination(target, index, pkt, cls, **kw).dated


# ------------------------------------------------------------------ what a model gets wrong


def test_listed_ids_date_every_photo_and_video(inbox: Path, target: Path, cfg, exif) -> None:
    # The model put a clip into the own-videos ID but did not call it own: in a listed ID it goes by date anyway.
    exif["video_1@01-02-2020_03-04-05.mp4"] = {"CreateDate": "2020:02:01 03:04:00+00:00"}
    _media(inbox / "video_1@01-02-2020_03-04-05.mp4")
    (inbox / "readme.txt").write_text("not a photo", encoding="utf-8")
    make_engine(cfg, llm=_model(own=False), date_folder_ids=["51.11 Photos"]).run_until_idle(timeout=30)
    assert _files(target / PHOTOS) == ["2020/02/video_1@01-02-2020_03-04-05.mp4", "readme.txt"]


def test_listed_id_must_match_this_archive(inbox: Path, target: Path, cfg, exif, tmp_path: Path) -> None:
    from sorto.jd import is_date_id, scan_jd

    item = scan_jd(target).get("51.11")
    assert is_date_id(item, ["51.11"]) and is_date_id(item, [" 51.11 Photos "])
    assert not is_date_id(item, ["51.11 Fotos", "51.1", "54.11"]) and not is_date_id(item, [])
    conf = tmp_path / "xdg-config" / "sorto" / "config.toml"
    conf.parent.mkdir(parents=True)
    conf.write_text('[run]\ndate_folder_ids = ["51.11 Fotos", "54.11 Eigene Videos"]\n', encoding="utf-8")
    loaded = load_config(inbox, target)
    assert loaded.date_folder_ids == ["51.11 Fotos", "54.11 Eigene Videos"]
    assert 'date_folder_ids = ["51.11 Fotos", "54.11 Eigene Videos"]' in loaded.to_toml()
    _media(inbox / "meme.jpg")  # another archive's ID names do not apply here
    make_engine(loaded, llm=_model(own=False), follow=False, identify_workers=1).run_until_idle(timeout=30)
    assert _files(target / PHOTOS) == ["meme.jpg"]


def test_listed_id_dates_media_of_a_folder_too(inbox: Path, target: Path, cfg, exif) -> None:
    _trip(inbox, exif)
    llm = FakeLLMClient(folder_handler=_folder(own=False))
    make_engine(cfg, llm=llm, date_folder_ids=["51.11"]).run_until_idle(timeout=30)
    assert len(_files(target / PHOTOS / "2019")) == 7
    assert _files(target / PHOTOS / "Trip Rome 2019") == ["itinerary.txt"]


def _software(inbox: Path, exif, names=("main.cpp", "app.qml", "Makefile", "README", "style.css")) -> Path:
    tree = inbox / "qt-src"
    for name in names:
        (tree / name).parent.mkdir(parents=True, exist_ok=True)
        (tree / name).write_text(name, encoding="utf-8")
    for i in range(4):
        _media(tree / "images" / f"icon{i}.png")
    exif["cork.jpg"] = {**_shot("2010:04:08", "iPhone 3GS"), "GPSLatitude": "-27.5", "GPSLongitude": "153.0"}
    _media(tree / "images" / "cork.jpg")
    return tree


def _say_own_photo(p, _prompt):
    return Classification(label="photo", confidence=0.95, jd_id="51.11", reason="camera", needs_user=False,
                          summary="s", own_media=True)


def test_folder_kept_in_one_piece_loses_nothing(inbox: Path, target: Path, cfg, exif) -> None:
    _software(inbox, exif)

    def folder(pkt, _prompt):
        return FolderAnswer(summary="Qt example sources.", coherent=True, jd_id="13.13", subfolder="",
                            confidence=0.95, reason="software", intact=True, own_media=True)

    llm = FakeLLMClient(_say_own_photo, folder_handler=folder)
    snap = make_engine(cfg, llm=llm).run_until_idle(timeout=30)
    assert llm.calls == []  # the picture with a camera in its EXIF is not even asked about
    assert len(_files(target / INVOICES / "qt-src")) == 10
    assert (target / INVOICES / "qt-src" / "images" / "cork.jpg").is_file()
    assert _files(target / PHOTOS) == []
    assert any("kept in one piece" in h.reason for h in snap.history)


def test_program_code_keeps_a_folder_in_one_piece_whatever_the_model_says(
    inbox: Path, target: Path, cfg, exif
) -> None:
    _software(inbox, exif)

    def folder(pkt, _prompt):
        assert pkt.to_llm_dict()["program_code_files"] == 4 and pkt.is_software
        return FolderAnswer(summary="Demo assets.", coherent=True, jd_id="13.13", subfolder="",
                            confidence=0.95, reason="assets", intact=False, own_media=True)

    llm = FakeLLMClient(_say_own_photo, folder_handler=folder)
    make_engine(cfg, llm=llm).run_until_idle(timeout=30)
    assert llm.calls == [] and _files(target / PHOTOS) == []
    assert (target / INVOICES / "qt-src" / "images" / "cork.jpg").is_file()


def test_a_script_among_photos_does_not_make_software(inbox: Path, target: Path, cfg, exif) -> None:
    trip = _trip(inbox, exif, n=12)
    (trip / "rename.sh").write_text("#!/bin/sh", encoding="utf-8")
    (trip / "resize.py").write_text("print(1)", encoding="utf-8")
    make_engine(cfg, llm=FakeLLMClient(folder_handler=_folder(own=True))).run_until_idle(timeout=30)
    assert len(_files(target / PHOTOS / "2019")) == 13


def test_outlier_the_model_files_with_its_folder_keeps_its_place(inbox: Path, target: Path, cfg, exif) -> None:
    _software(inbox, exif, names=("notes.txt", "plan.md", "budget.csv", "README", "todo.txt"))

    def folder(pkt, _prompt):
        return FolderAnswer(summary="Qt example sources.", coherent=True, jd_id="13.13", subfolder="",
                            confidence=0.95, reason="software")

    def same_id(p, _prompt):
        return Classification(label="image", confidence=0.9, jd_id="13.13", reason="demo asset", needs_user=False,
                              summary="s", own_media=False)

    llm = FakeLLMClient(same_id, folder_handler=folder)
    snap = make_engine(cfg, llm=llm).run_until_idle(timeout=30)
    assert [p.filename for p in llm.calls] == ["cork.jpg"]  # it stood out: a camera among files without one
    assert "keeps its place" in llm.calls[0].folder_context
    assert (target / INVOICES / "qt-src" / "images" / "cork.jpg").is_file()
    assert not (target / INVOICES / "cork.jpg").exists()
    assert any("still part of its folder" in h.reason for h in snap.history)


def test_every_photo_mode_still_leaves_whole_pieces_alone(inbox: Path, target: Path, cfg, exif) -> None:
    _software(inbox, exif)

    def folder(pkt, _prompt):
        return FolderAnswer(summary="Qt example sources.", coherent=True, jd_id="51.11", subfolder="",
                            confidence=0.95, reason="software", intact=True)

    llm = FakeLLMClient(folder_handler=folder)
    make_engine(cfg, llm=llm, date_folders="all", date_folder_ids=["51.11"]).run_until_idle(timeout=30)
    assert len(_files(target / PHOTOS / "qt-src")) == 10 and not (target / PHOTOS / "2010").exists()


def test_outlier_in_a_listed_id_is_dated_not_left_in_the_folder(inbox: Path, target: Path, cfg, exif) -> None:
    trip = _trip(inbox, exif)
    exif["IMG_9999.jpg"] = _shot("2012:01:05")  # years away from the rest: checked on its own
    _media(trip / "IMG_9999.jpg")
    llm = FakeLLMClient(lambda p, _s: Classification(label="photo", confidence=0.9, jd_id="51.11", reason="r",
                                                     needs_user=False, summary="s", own_media=False),
                        folder_handler=_folder(own=True))
    make_engine(cfg, llm=llm, date_folder_ids=["51.11 Photos"]).run_until_idle(timeout=30)
    assert [p.filename for p in llm.calls] == ["IMG_9999.jpg"]
    assert (target / PHOTOS / "2012" / "01" / "IMG_9999.jpg").is_file()


def test_a_year_folder_from_the_model_needs_a_capture_date_too(inbox: Path, target: Path, cfg, exif) -> None:
    """The model may suggest "2022" from the file's mtime: for a photo that is no date at all."""
    (target / PHOTOS / "2022").mkdir()
    exif["dated.jpg"] = _shot("2022:05:01")
    _media(inbox / "dated.jpg")
    _media(inbox / "fedcba9876543210fedcba9876543210.jpg", mtime="2022-02-02T12:00:00")

    def handler(packet, _prompt):
        return Classification(
            label="photo", confidence=0.9, jd_id="51.11", subfolder="2022", reason="r", needs_user=False,
            summary="A photo.", own_media=False,
        )

    make_engine(cfg, llm=FakeLLMClient(handler=handler)).run_until_idle(timeout=30)
    assert _files(target / PHOTOS) == ["2022/dated.jpg", "fedcba9876543210fedcba9876543210.jpg"]
