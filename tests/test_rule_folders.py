"""A rule can ask for files to be kept in folders named after what they are; the rule is shown in full."""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import pytest

from conftest import make_engine
from sorto.config import load_config
from sorto.engine import Engine
from sorto.identify import identify_file, model_3d_info
from sorto.jd import clean_folder_name, existing_folder_like
from sorto.llm import FakeLLMClient
from sorto.models import Classification
from sorto.rules import load_rules

PHOTOS = "50-59 Media/51 Pictures/51.11 Photos"
RULE = (
    "Knitting patterns go to 51.11, each in a folder named after the garment. Many files are badly named, "
    "so look at what the pattern is for."
)
QUOTE = "Knitting patterns go to 51.11, each in a folder named after the garment."  # what a model quotes


def _rules(cfg, text: str = f"- Emails from billing@example.com go to 13.13.\n- {RULE}\n") -> None:
    cfg.rules_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.rules_file.write_text(text, encoding="utf-8")


def _model(subfolder: str, rule: str = QUOTE):
    def handler(packet, _prompt):
        return Classification(
            label="pattern", confidence=0.9, jd_id="51.11", subfolder=subfolder, reason="a knitting pattern",
            needs_user=False, summary="A knitting pattern.", rule=rule,
        )

    return FakeLLMClient(handler=handler)


def test_the_whole_rule_is_found_from_a_short_quote(tmp_path: Path) -> None:
    path = tmp_path / "rules.md"
    path.write_text(
        f"<!-- a comment -->\n- Emails from billing@example.com go to 13.13.\n- {RULE.split('. ')[0]}.\n"
        f"  {RULE.split('. ', 1)[1]}\n* Screenshots always go to 51.12.\n1. Keep everything in 05.12 where it is.\n",
        encoding="utf-8",
    )
    rules = load_rules(path)
    assert rules.entries[1] == RULE and len(rules.entries) == 4  # an indented line continues its rule
    assert rules.match(QUOTE) == RULE
    assert rules.match("screenshots always go to 51.12") == "Screenshots always go to 51.12."
    assert rules.match("Emails from billing go to 13.13") == "Emails from billing@example.com go to 13.13."
    assert rules.match("keep everything in 05.12") == "Keep everything in 05.12 where it is."
    assert rules.match("a rule nobody wrote down") == "" and rules.match("13.13") == "" and rules.match("") == ""


def test_folder_names_sorto_accepts(tmp_path: Path) -> None:
    assert clean_folder_name("  Cable clip. ") == "Cable clip"
    assert clean_folder_name("Wool socks/size 42") == "Wool socks"
    for bad in ("", "..", ".hidden", "2024", "2024-05", "13.13 Invoices", "10-19 Life", "42", "x"):
        assert clean_folder_name(bad) == "", bad
    (tmp_path / "cable-clip").mkdir()
    (tmp_path / "Other").mkdir()
    assert existing_folder_like(tmp_path, "Cable Clip") == "cable-clip"
    assert existing_folder_like(tmp_path, "Cable clips") == ""


def test_a_rule_asks_for_a_named_folder_and_gets_it(inbox: Path, target: Path, cfg) -> None:
    _rules(cfg)
    (inbox / "final2.pdf").write_bytes(b"%PDF pattern one")
    (inbox / "final3.pdf").write_bytes(b"%PDF pattern two")
    snap = make_engine(cfg, llm=_model("Wool socks")).run_until_idle(timeout=30)
    assert sorted(p.name for p in (target / PHOTOS / "Wool socks").iterdir()) == ["final2.pdf", "final3.pdf"]
    views = {h.filename: h for h in snap.history}
    assert all(v.rule == RULE for v in views.values())  # the rule as written, not the model's short quote
    assert sum("new folder Wool socks in 51.11 Photos" in v.folder for v in views.values()) == 1  # made once
    log = [json.loads(line) for line in cfg.progress_path.read_text(encoding="utf-8").splitlines()]
    made = [r for r in log if r["action"] == "created_folder"]
    assert len(made) == 1 and made[0]["rel"] == f"{PHOTOS}/Wool socks"
    text = next(cfg.runs_dir.glob("run-*.log")).read_text(encoding="utf-8")
    assert f"new folder: created {PHOTOS}/Wool socks for " in text and f"rule:        {RULE}" in text


def test_a_folder_that_goes_by_the_name_already_is_used(inbox: Path, target: Path, cfg) -> None:
    _rules(cfg)
    (target / PHOTOS / "wool-socks").mkdir()
    (inbox / "final2.pdf").write_bytes(b"%PDF pattern")
    make_engine(cfg, llm=_model("Wool Socks")).run_until_idle(timeout=30)
    assert (target / PHOTOS / "wool-socks" / "final2.pdf").is_file()
    assert sorted(p.name for p in (target / PHOTOS).iterdir()) == ["wool-socks"]


@pytest.mark.parametrize(
    ("kwargs", "rule", "subfolder"),
    [
        ({}, "", "Wool socks"),  # no rule was followed
        ({}, "a rule nobody wrote down", "Wool socks"),  # not one of the user's rules
        ({"new_subfolders": "off"}, QUOTE, "Wool socks"),
        ({}, QUOTE, "2031"),  # date folders are sorto's own
        ({}, QUOTE, "13.13 Invoices"),  # not something that looks like another ID
    ],
)
def test_no_folder_is_invented_otherwise(inbox: Path, target: Path, cfg, kwargs, rule, subfolder) -> None:
    _rules(cfg)
    (inbox / "final2.pdf").write_bytes(b"%PDF pattern")
    snap = make_engine(cfg, llm=_model(subfolder, rule), **kwargs).run_until_idle(timeout=30)
    assert (target / PHOTOS / "final2.pdf").is_file() and [p.name for p in (target / PHOTOS).iterdir()] == ["final2.pdf"]
    assert any("does not exist; filed into the ID itself" in line for line in snap.log_lines)  # said, not silent


def test_dry_run_shows_the_folder_without_making_it(inbox: Path, target: Path, cfg) -> None:
    _rules(cfg)
    (inbox / "final2.pdf").write_bytes(b"%PDF pattern")
    snap = make_engine(cfg, llm=_model("Wool socks"), dry_run=True).run_until_idle(timeout=30)
    assert snap.history[0].dest_rel == f"{PHOTOS}/Wool socks/final2.pdf" and snap.history[0].outcome == "planned"
    assert not (target / PHOTOS / "Wool socks").exists()


def test_reorganizing_moves_a_filed_file_into_the_rules_folder(target: Path) -> None:
    filed = target / PHOTOS / "final2.pdf"
    filed.write_bytes(b"%PDF pattern")
    cfg = load_config(target, target)
    cfg.follow, cfg.identify_workers, cfg.scan_interval = False, 1, 0.3
    _rules(cfg)
    Engine(cfg, llm=_model("Wool socks")).run_until_idle(timeout=30)
    assert not filed.exists() and (target / PHOTOS / "Wool socks" / "final2.pdf").is_file()


def _png(width: int = 8, height: int = 8) -> bytes:
    """A small valid grey PNG, built by hand so the test needs no image tool to make it."""
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\x00" + b"\x80" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


PNG_1X1 = _png()


def _threemf(path: Path, *, preview: bool = True) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "3D/3dmodel.model",
            '<?xml version="1.0"?><model><metadata name="Title">Cable clip &amp;amp; holder</metadata>'
            '<metadata name="Description">&amp;lt;p&amp;gt;Holds two cables.&amp;lt;/p&amp;gt;</metadata>'
            '<metadata name="Application">ExampleSlicer-1.0</metadata><metadata name="Copyright">x</metadata>'
            '<resources><object id="1" name="base"/></resources></model>',
        )
        zf.writestr(
            "Metadata/model_settings.config",
            '<config><object id="1"><metadata key="name" value="clip.stl"/></object>'
            '<object id="2"><metadata key="name" value="base"/></object></config>',
        )
        if preview:
            zf.writestr("Metadata/plate_1.png", PNG_1X1)
    return path


def test_a_3d_model_file_says_what_it_is(tmp_path: Path) -> None:
    facts, picture = model_3d_info(_threemf(tmp_path / "plate_1.3mf"))
    assert facts == {
        "title": "Cable clip & holder", "description": "Holds two cables.", "application": "ExampleSlicer-1.0",
        "parts": "base, clip.stl",
    }
    assert picture == PNG_1X1
    assert model_3d_info(_threemf(tmp_path / "no-preview.3mf", preview=False))[1] == b""
    assert model_3d_info(tmp_path / "plate_1.3mf", preview=False)[1] == b""
    (tmp_path / "ascii.stl").write_text("solid cable_clip_v2\n facet normal 0 0 0\n", encoding="ascii")
    assert model_3d_info(tmp_path / "ascii.stl") == ({"header": "cable_clip_v2"}, b"")
    (tmp_path / "binary.stl").write_bytes(b"Exported from ExampleCAD".ljust(80, b"\x00") + b"\x00" * 40)
    assert model_3d_info(tmp_path / "binary.stl")[0] == {"header": "Exported from ExampleCAD"}
    (tmp_path / "blank.stl").write_bytes(b"\x00" * 120)
    (tmp_path / "broken.3mf").write_bytes(b"not a zip")
    assert model_3d_info(tmp_path / "blank.stl") == ({}, b"") and model_3d_info(tmp_path / "broken.3mf") == ({}, b"")


def test_the_model_gets_the_3d_facts_and_the_stored_preview(inbox: Path, cfg) -> None:
    packet = identify_file(_threemf(inbox / "plate_1.3mf"), "plate_1.3mf", cfg)
    said = packet.to_llm_dict()["extra_meta"]["model_3d"]
    assert said.startswith("title: Cable clip & holder; description: Holds two cables.")
    assert said.endswith("parts: base, clip.stl")
    if shutil.which("magick") or shutil.which("convert"):
        assert len(packet.images) == 1 and packet.images[0][:2] == b"\xff\xd8"
        assert packet.media_note == "model viewed the preview picture stored in the 3D file"
    cfg.vision = False
    assert identify_file(inbox / "plate_1.3mf", "plate_1.3mf", cfg).images == []
    light = identify_file(inbox / "plate_1.3mf", "plate_1.3mf", cfg, light=True)
    assert "model_3d" not in light.extra_meta and light.images == []
