"""Every run writes its own readable log: each file, what the analysis said, where the file went."""

from __future__ import annotations

from pathlib import Path

from conftest import make_engine
from sorto.cli import main
from sorto.llm import FakeLLMClient
from sorto.models import AnalysisView, Classification
from sorto.runlog import RunLog


def _logs(cfg) -> list[Path]:
    return sorted(cfg.runs_dir.glob("run-*.log"))


def test_run_log_names_each_file_its_analysis_and_its_new_place(inbox: Path, target: Path, cfg) -> None:
    (inbox / "invoice-march.txt").write_text("Invoice 12 EUR", encoding="utf-8")
    (inbox / "mystery.bin").write_bytes(b"\x00\x01\x02")
    engine = make_engine(cfg)
    engine.run_until_idle(timeout=30)
    (log,) = _logs(cfg)
    assert engine.run_log.path == log
    text = log.read_text(encoding="utf-8")
    assert text.startswith("sorto run started ")
    assert f"source:      {inbox}" in text and f"target:      {target}" in text and "model:       fake" in text
    entry = text.split("] invoice-march.txt", 1)[1].split("\n[", 1)[0]
    assert "analysis:    txt, confidence 0.90 → 13.13 Invoices" in entry
    assert "A .txt file named invoice-march.txt." in entry
    assert "why:         fake classifier matched a filename keyword" in entry
    assert "moved to:    10-19 Life/13 Money/13.13 Invoices/invoice-march.txt" in entry
    kept = text.split("] mystery.bin", 1)[1].split("\n[", 1)[0].split("sorto run ended")[0]
    assert "analysis:    bin, confidence 0.00\n" in kept and "why:         no match" in kept
    assert "kept:        confidence 0.00 below 0.50; left in source" in kept and "moved to" not in kept
    assert text.rstrip().rsplit("\n", 1)[1].startswith("sorto run ended ")
    assert text.rstrip().endswith(": 1 moved, 1 kept")


def test_every_run_starts_a_new_log(inbox: Path, target: Path, cfg) -> None:
    (inbox / "invoice-1.txt").write_text("one", encoding="utf-8")
    make_engine(cfg).run_until_idle(timeout=30)
    (inbox / "invoice-2.txt").write_text("two", encoding="utf-8")
    make_engine(cfg).run_until_idle(timeout=30)
    first, second = _logs(cfg)
    assert "invoice-1.txt" in first.read_text(encoding="utf-8")
    again = second.read_text(encoding="utf-8")
    assert "invoice-2.txt" in again and "invoice-1.txt" not in again


def test_dry_run_log_says_where_files_would_go(inbox: Path, target: Path, cfg) -> None:
    (inbox / "invoice.txt").write_text("x", encoding="utf-8")
    make_engine(cfg, dry_run=True).run_until_idle(timeout=30)
    text = _logs(cfg)[0].read_text(encoding="utf-8")
    assert "mode:        dry run, nothing is moved" in text
    assert "would go to: 10-19 Life/13 Money/13.13 Invoices/invoice.txt" in text
    assert (inbox / "invoice.txt").is_file()


def test_new_ids_and_whole_folders_are_in_the_log(inbox: Path, target: Path, cfg) -> None:
    from sorto.folders import FolderAnswer

    (inbox / "digest.eml").write_text("weekly digest", encoding="utf-8")
    trip = inbox / "Rome 2019"
    trip.mkdir()
    for i in range(5):
        (trip / f"note-{i}.txt").write_text(f"day {i}", encoding="utf-8")

    def one(packet, _prompt):
        return Classification(
            label="letter", confidence=0.9, jd_id="new", reason="no existing ID fits", needs_user=False,
            summary="A newsletter.", new_id_category="13", new_id_name="Newsletters",
        )

    def unit(packet, _prompt):
        return FolderAnswer(summary="Trip notes.", coherent=True, jd_id="51.11", subfolder="", confidence=0.9, reason="t")

    make_engine(cfg, llm=FakeLLMClient(handler=one, folder_handler=unit)).run_until_idle(timeout=30)
    text = _logs(cfg)[0].read_text(encoding="utf-8")
    assert "new ID: created 10-19 Life/13 Money/13.14 Newsletters for digest.eml" in text
    assert "new ID:      13.14 Newsletters" in text
    assert "folder: Rome 2019 (5 files): Trip notes. Moves together to 50-59 Media/51 Pictures/51.11 Photos/Rome 2019" in text
    assert "moved to:    50-59 Media/51 Pictures/51.11 Photos/Rome 2019/note-0.txt" in text


def test_a_second_log_in_the_same_second_gets_its_own_file(tmp_path: Path) -> None:
    a, b = RunLog(tmp_path, {"source": "/a"}), RunLog(tmp_path, {"source": "/b"})
    assert a.path != b.path and sorted([b.path, a.path]) == [a.path, b.path]
    a.file(AnalysisView(src_rel="x\ny.txt", size=3, outcome="error", reason="boom"))
    a.close()
    a.close()
    a.note("error", "ignored after close")
    text = a.path.read_text(encoding="utf-8")
    assert "] x\ny.txt" in text and "error:       boom" in text and text.rstrip().endswith("1 with an error")
    b.close()
    assert b.path.read_text(encoding="utf-8").rstrip().endswith("no files handled")


def test_headless_run_prints_where_the_log_is(inbox: Path, target: Path, capsys) -> None:
    (inbox / "invoice.txt").write_text("x", encoding="utf-8")
    assert main(["run", str(inbox), "-t", str(target), "--once", "--no-tui", "--fake-llm"]) == 0
    line = next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("log of this run: "))
    path = Path(line.split(": ", 1)[1])
    assert path.is_file() and path.parent.name == "runs" and "invoice.txt" in path.read_text(encoding="utf-8")
    assert main(["status", str(inbox), "-t", str(target)]) == 0
    assert f"last log: {path}  (1 run log(s)" in capsys.readouterr().out
