from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import ROUTES, make_engine
from sorto.db import Database
from sorto.llm import FakeLLMClient
from sorto.models import Classification

INV = "10-19 Life/13 Money/13.13 Invoices"


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() and not p.name.endswith(".md"))


def test_dry_run_does_not_move(inbox: Path, target: Path, cfg) -> None:
    (inbox / "invoice-march.txt").write_text("Invoice total 12 EUR", encoding="utf-8")
    eng = make_engine(cfg, dry_run=True)
    snap = eng.run_until_idle(timeout=30)
    assert (inbox / "invoice-march.txt").exists()
    assert _files(target) == []
    assert snap.counts.planned == 1
    assert snap.history[0].outcome == "planned"
    assert snap.history[0].dest_rel == f"{INV}/invoice-march.txt"


def test_live_moves_into_jd_id_and_shows_analysis(inbox: Path, target: Path, cfg) -> None:
    (inbox / "sub").mkdir()
    (inbox / "sub" / "invoice-march.txt").write_text("Invoice total 12 EUR", encoding="utf-8")
    (inbox / "photo-beach.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 64)
    snap = make_engine(cfg).run_until_idle(timeout=30)
    assert (target / INV / "invoice-march.txt").read_text(encoding="utf-8") == "Invoice total 12 EUR"
    assert (target / "50-59 Media/51 Pictures/51.11 Photos/photo-beach.jpg").is_file()
    assert _files(inbox) == []
    assert snap.counts.done == 2
    by_name = {h.filename: h for h in snap.history}
    inv = by_name["invoice-march.txt"]
    assert inv.outcome == "moved"
    assert inv.jd_id == "13.13" and inv.jd_name == "Invoices"
    assert inv.summary
    assert inv.dest_rel == f"{INV}/invoice-march.txt"
    log = cfg.progress_path.read_text(encoding="utf-8")
    assert '"action": "moved"' in log


def test_never_overwrites_existing_target_file(inbox: Path, target: Path, cfg) -> None:
    (target / INV / "invoice.txt").write_text("ORIGINAL", encoding="utf-8")
    (inbox / "invoice.txt").write_text("NEW", encoding="utf-8")
    make_engine(cfg).run_until_idle(timeout=30)
    assert (target / INV / "invoice.txt").read_text(encoding="utf-8") == "ORIGINAL"
    assert (target / INV / "invoice-2.txt").read_text(encoding="utf-8") == "NEW"


def test_unknown_or_low_confidence_stays_in_source(inbox: Path, target: Path, cfg) -> None:
    (inbox / "mystery.bin").write_bytes(b"\x00\x01")
    (inbox / "invoice.txt").write_text("x", encoding="utf-8")

    def handler(packet, _prompt):
        if packet.filename == "invoice.txt":
            return Classification("invoice", 0.3, "13.13", "unsure", False, summary="maybe a bill")
        return Classification("x", 0.95, "77.77", "made up", False, summary="?")

    snap = make_engine(cfg, llm=FakeLLMClient(handler=handler)).run_until_idle(timeout=30)
    assert (inbox / "mystery.bin").exists() and (inbox / "invoice.txt").exists()
    assert _files(target) == []
    assert snap.counts.needs_user == 2
    reasons = " ".join(h.reason for h in snap.history)
    assert "below" in reasons and "77.77" in reasons


def test_needs_user_is_held(inbox: Path, target: Path, cfg) -> None:
    (inbox / "invoice.txt").write_text("x", encoding="utf-8")
    llm = FakeLLMClient(routes=ROUTES, needs_user=True)
    snap = make_engine(cfg, llm=llm).run_until_idle(timeout=30)
    assert (inbox / "invoice.txt").exists()
    assert snap.counts.needs_user == 1


def test_subfolder_and_rename_follow_target_conventions(inbox: Path, target: Path, cfg) -> None:
    (inbox / "scan0001.pdf").write_bytes(b"%PDF-1.4 fake")

    def handler(_packet, _prompt):
        return Classification(
            "invoice", 0.9, "13.13", "bill", False,
            summary="An electricity bill.", subfolder="2025", new_filename="electricity-2025-01",
        )

    make_engine(cfg, llm=FakeLLMClient(handler=handler)).run_until_idle(timeout=30)
    assert (target / INV / "2025" / "electricity-2025-01.pdf").is_file()


def test_useful_names_are_not_renamed(inbox: Path, target: Path, cfg) -> None:
    (inbox / "invoice-acme.txt").write_text("x", encoding="utf-8")

    def handler(_packet, _prompt):
        return Classification("invoice", 0.9, "13.13", "bill", False, new_filename="other.txt")

    make_engine(cfg, llm=FakeLLMClient(handler=handler)).run_until_idle(timeout=30)
    assert (target / INV / "invoice-acme.txt").is_file()


def test_resume_does_not_reprocess_done(inbox: Path, target: Path, cfg) -> None:
    (inbox / "invoice.txt").write_text("x", encoding="utf-8")
    make_engine(cfg).run_until_idle(timeout=30)
    llm = FakeLLMClient(routes=ROUTES)
    make_engine(cfg, llm=llm).run_until_idle(timeout=30)
    assert llm.calls == []


def test_invalid_llm_json_leaves_file(inbox: Path, target: Path, cfg) -> None:
    (inbox / "invoice.txt").write_text("x", encoding="utf-8")
    snap = make_engine(cfg, llm=FakeLLMClient(invalid=True)).run_until_idle(timeout=30)
    assert (inbox / "invoice.txt").exists()
    assert snap.counts.error == 1


def test_target_inside_source_is_not_scanned(tmp_path: Path, target: Path) -> None:
    from sorto.config import load_config

    source = target.parent
    (source / "invoice-loose.txt").write_text("x", encoding="utf-8")
    (target / INV / "invoice-old.txt").write_text("already filed", encoding="utf-8")
    cfg = load_config(source, target)
    cfg.follow = False
    llm = FakeLLMClient(routes=ROUTES)
    make_engine(cfg, llm=llm).run_until_idle(timeout=30)
    names = [p.filename for p in llm.calls]
    assert "invoice-old.txt" not in names
    assert (target / INV / "invoice-old.txt").exists()
    assert (target / INV / "invoice-loose.txt").exists()


def test_duplicates_are_kept_by_default(inbox: Path, target: Path, cfg) -> None:
    (inbox / "invoice-a.txt").write_text("same bytes", encoding="utf-8")
    make_engine(cfg).run_until_idle(timeout=30)
    (inbox / "invoice-b.txt").write_text("same bytes", encoding="utf-8")
    snap = make_engine(cfg).run_until_idle(timeout=30)
    assert (inbox / "invoice-b.txt").exists()
    assert snap.counts.skipped == 1
    assert "duplicate" in snap.history[0].reason


def test_delete_duplicates_removes_only_the_copy(inbox: Path, target: Path, cfg) -> None:
    (inbox / "invoice-a.txt").write_text("same bytes", encoding="utf-8")
    make_engine(cfg).run_until_idle(timeout=30)
    (inbox / "invoice-b.txt").write_text("same bytes", encoding="utf-8")
    make_engine(cfg, delete_duplicates=True).run_until_idle(timeout=30)
    assert not (inbox / "invoice-b.txt").exists()
    assert (target / INV / "invoice-a.txt").read_text(encoding="utf-8") == "same bytes"


def test_junk_is_left_alone_and_git_never_deleted(inbox: Path, target: Path, cfg) -> None:
    (inbox / ".DS_Store").write_bytes(b"junk")
    repo = inbox / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "Thumbs.db").write_bytes(b"junk")
    llm = FakeLLMClient(routes=ROUTES)
    make_engine(cfg, llm=llm).run_until_idle(timeout=30)
    assert (inbox / ".DS_Store").exists()
    assert llm.calls == []
    make_engine(cfg, delete_junk=True).run_until_idle(timeout=30)
    assert (repo / "Thumbs.db").exists()


def test_delete_junk(inbox: Path, target: Path, cfg) -> None:
    (inbox / ".DS_Store").write_bytes(b"junk")
    make_engine(cfg, delete_junk=True).run_until_idle(timeout=30)
    assert not (inbox / ".DS_Store").exists()


def test_state_lives_outside_both_trees(inbox: Path, target: Path, cfg) -> None:
    (inbox / "invoice.txt").write_text("x", encoding="utf-8")
    make_engine(cfg).run_until_idle(timeout=30)
    assert not any(p.suffix in {".sqlite", ".jsonl"} for p in target.rglob("*"))
    assert not any(p.suffix in {".sqlite", ".jsonl"} for p in inbox.rglob("*"))
    assert Database(cfg.db_path).counts().done == 1


def test_prompt_contains_outline_and_fits_context(cfg) -> None:
    eng = make_engine(cfg)
    assert "13.13 Invoices" in eng.system_prompt
    assert "11.00" not in eng.system_prompt
    assert len(eng.system_prompt) / 3 < cfg.context_window


@pytest.mark.skipif(
    not (shutil.which("magick") and shutil.which("exiftool")), reason="needs ImageMagick + exiftool"
)
def test_photo_evidence_reaches_the_model(inbox: Path, target: Path, cfg) -> None:
    photo = inbox / "IMG_0001.jpg"
    subprocess.run(["magick", "-size", "64x48", "xc:orange", str(photo)], check=True)
    subprocess.run(
        ["exiftool", "-q", "-overwrite_original", "-DateTimeOriginal=2024:07:14 12:30:00", str(photo)],
        check=True,
    )
    llm = FakeLLMClient(routes={"img_": "51.11"})
    snap = make_engine(cfg, llm=llm).run_until_idle(timeout=30)
    pkt = llm.calls[0]
    assert pkt.images and pkt.exif["DateTimeOriginal"] == "2024:07:14 12:30:00"
    payload = pkt.to_llm_dict()
    assert "attached_images" in payload and "exif" in payload
    assert payload["text_preview"] == ""
    assert "taken 2024-07-14" in snap.history[0].media_note
    assert "file name as cameras" in payload["camera_signs"]
    # A photo from a camera is filed by its capture date.
    assert (target / "50-59 Media/51 Pictures/51.11 Photos/2024/07/IMG_0001.jpg").is_file()


def test_no_vision_still_uses_exif(inbox: Path, target: Path, cfg) -> None:
    if not (shutil.which("magick") and shutil.which("exiftool")):
        pytest.skip("needs ImageMagick + exiftool")
    photo = inbox / "IMG_0002.jpg"
    subprocess.run(["magick", "-size", "64x48", "xc:blue", str(photo)], check=True)
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-Model=Pixel 9", str(photo)], check=True)
    llm = FakeLLMClient(routes={"img_": "51.11"})
    make_engine(cfg, llm=llm, vision=False).run_until_idle(timeout=30)
    assert llm.calls[0].images == [] and llm.calls[0].exif["Model"] == "Pixel 9"
