"""junk.md: file patterns that are never needed go straight to one folder, without the model."""

from __future__ import annotations

import os
import time
from pathlib import Path

from conftest import ROUTES, make_engine
from sorto.cli import main
from sorto.config import load_config
from sorto.folders import FolderAnswer
from sorto.llm import FakeLLMClient
from sorto.rules import load_junk_rules

JUNK_ID = "00-09 System/00 Admin/00.01 Inbox"  # any existing ID serves as the junk folder in tests


def _junk_file(text: str = "into: 00.01\n*.dll\n*.pdb\nThumbs.db\n*.log -> 13.13\n") -> Path:
    path = Path(os.environ["XDG_CONFIG_HOME"]) / "sorto" / "junk.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_junk_file_parsing() -> None:
    junk = load_junk_rules(_junk_file("<!-- template -->\ninto: 00.01\n- *.dll\n* *.PDB\n*.log -> 13.13\nbuild/**/*.o\n"))
    assert junk.into == "00.01" and not junk.problems
    assert [(r.pattern, r.jd_id) for r in junk.rules] == [("*.dll", ""), ("*.PDB", ""), ("*.log", "13.13"), ("build/**/*.o", "")]
    assert junk.ids == ["00.01", "13.13"]
    assert junk.match("a/b/X.DLL").pattern == "*.dll" and junk.match("x.dll.txt") is None
    assert junk.match("build/a/b.o") is not None and junk.match("src/b.o") is None
    assert "nowhere to go" in load_junk_rules(_junk_file("*.dll\n")).problems[0]
    assert not load_junk_rules(Path("/nonexistent/junk.md"))


def test_junk_patterns_move_without_asking_the_model(inbox: Path, target: Path, cfg) -> None:
    _junk_file()
    (inbox / "sub").mkdir()
    (inbox / "sub" / "libfoo.DLL").write_bytes(b"MZ dll")
    (inbox / "Thumbs.db").write_bytes(b"thumbs")  # also built-in junk: the rule wins, so it moves
    (inbox / "server.log").write_text("log", encoding="utf-8")
    (inbox / "invoice.txt").write_text("Invoice", encoding="utf-8")
    llm = FakeLLMClient(routes=ROUTES)
    snap = make_engine(cfg, llm=llm).run_until_idle(timeout=30)
    assert (target / JUNK_ID / "libfoo.DLL").is_file()
    assert (target / JUNK_ID / "Thumbs.db").is_file()
    assert (target / "10-19 Life/13 Money/13.13 Invoices/server.log").is_file()  # its own ID
    assert [p.filename for p in llm.calls] == ["invoice.txt"]
    dll = next(h for h in snap.history if h.filename == "libfoo.DLL")
    assert dll.outcome == "moved" and "junk rule *.dll" in dll.rule and dll.label == "junk (by rule)"


def test_junk_inside_a_folder_unit_still_goes_to_the_junk_folder(inbox: Path, target: Path, cfg) -> None:
    _junk_file()
    app = inbox / "GameSetup"
    app.mkdir()
    for i in range(6):
        (app / f"photo-{i}.jpg").write_bytes(b"\xff\xd8" + bytes([i]) * 30)
    (app / "engine.dll").write_bytes(b"MZ")

    def unit(packet, _prompt):
        return FolderAnswer(summary="A set.", coherent=True, jd_id="51.11", subfolder="", confidence=0.9, reason="set")

    make_engine(cfg, llm=FakeLLMClient(folder_handler=unit)).run_until_idle(timeout=30)
    assert (target / "50-59 Media/51 Pictures/51.11 Photos/GameSetup/photo-3.jpg").is_file()
    assert (target / JUNK_ID / "engine.dll").is_file()
    assert not (target / "50-59 Media/51 Pictures/51.11 Photos/GameSetup/engine.dll").exists()


def test_unknown_junk_id_keeps_the_file(inbox: Path, target: Path, cfg) -> None:
    _junk_file("into: 77.77\n*.dll\n")
    (inbox / "a.dll").write_bytes(b"MZ")
    snap = make_engine(cfg).run_until_idle(timeout=30)
    assert (inbox / "a.dll").is_file()
    assert "77.77" in snap.history[0].reason and "not in the target" in snap.history[0].reason


def test_junk_already_in_its_folder_stays_when_reorganizing(target: Path) -> None:
    _junk_file()
    (target / JUNK_ID / "old.dll").write_bytes(b"MZ")
    (target / "10-19 Life/13 Money/13.13 Invoices/stray.dll").write_bytes(b"MZ2")
    cfg = load_config(target, target)
    snap = make_engine(cfg, follow=False, identify_workers=1).run_until_idle(timeout=30)
    by = {h.filename: h.outcome for h in snap.history}
    assert by["old.dll"] == "in_place" and by["stray.dll"] == "moved"
    assert (target / JUNK_ID / "stray.dll").is_file()


def test_junk_file_is_re_read_while_running(inbox: Path, target: Path, cfg, monkeypatch) -> None:
    monkeypatch.setattr("sorto.engine.RULES_RELOAD_SEC", 0.1)
    eng = make_engine(cfg, follow=True)
    eng.start()
    try:
        _junk_file("into: 00.01\n*.tmp\n")
        for _ in range(100):
            if eng.junk_rules:
                break
            time.sleep(0.05)
        assert [r.pattern for r in eng.junk_rules.rules] == ["*.tmp"]
        assert any("junk pattern" in line for line in eng.snapshot().log_lines)
    finally:
        eng.request_stop()
        eng.join(timeout=10)


def test_rules_command_shows_junk_rules(target: Path, capsys) -> None:
    _junk_file("into: 00.01\n*.dll\n*.log -> 42.42\n")
    assert main(["rules", str(target)]) == 1
    out = capsys.readouterr().out
    assert "2 pattern(s) → 00.01" in out and "*.log → 42.42" in out and "42.42" in out.split("warning")[-1]
