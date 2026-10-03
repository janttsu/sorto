"""At the end of a run sorto says what it leaves in the source and why, so an empty run explains itself."""

from __future__ import annotations

from pathlib import Path

from conftest import ROUTES, make_engine
from sorto.cli import main
from sorto.config import load_config
from sorto.engine import Engine
from sorto.llm import FakeLLMClient

INVOICES = "10-19 Life/13 Money/13.13 Invoices"


def _repo(path: Path, files: int) -> Path:
    (path / ".git" / "objects").mkdir(parents=True)
    (path / ".git" / "objects" / "ab12").write_bytes(b"blob")  # git's own files are not counted
    for i in range(files):
        (path / f"module_{i}.py").write_text("print(1)\n", encoding="utf-8")
    return path


def _package(path: Path) -> Path:
    for d in ("usr/bin", "usr/lib"):
        (path / d).mkdir(parents=True)
    (path / "AppRun").write_text("#!/bin/sh\n", encoding="utf-8")
    (path / "usr/lib/libviewer.so").write_bytes(b"\x7fELF")
    return path


def test_a_run_with_nothing_to_do_says_why(inbox: Path, target: Path, cfg) -> None:
    (target / INVOICES / "invoice-march.txt").write_text("Invoice 12 EUR", encoding="utf-8")
    (inbox / "invoice-march.txt").write_text("Invoice 12 EUR", encoding="utf-8")
    first = make_engine(cfg)
    first.run_until_idle(timeout=30)  # files it, then a copy of it arrives again
    (inbox / "invoice-march-copy.txt").write_text("Invoice 12 EUR", encoding="utf-8")
    make_engine(cfg).run_until_idle(timeout=30)
    _repo(inbox / "tools" / "invoice-tool", files=3)
    _package(inbox / "viewer" / "squashfs-root")

    engine = make_engine(cfg)
    engine.run_until_idle(timeout=30)
    lines = engine.leftovers()
    assert lines == [
        "1 duplicate (byte-for-byte copies of files already in the target; --delete-duplicates removes them)",
        "1 git repository with 3 files: tools/invoice-tool (sorto never moves a repository; place it yourself)",
        "1 software package with 2 files: viewer/squashfs-root "
        "(unpacked software is kept whole; place it yourself)",
    ]
    text = engine.run_log.path.read_text(encoding="utf-8")
    block = text.split("left in the source:\n", 1)[1].split("\nsorto run ended", 1)[0]
    assert [ln.strip() for ln in block.strip().splitlines()] == lines
    assert text.rstrip().endswith("no files handled")


def test_files_left_for_you_point_to_retry_kept(inbox: Path, target: Path, cfg) -> None:
    (inbox / "invoice-unclear.txt").write_text("Invoice?", encoding="utf-8")
    engine = make_engine(cfg, llm=FakeLLMClient(routes=ROUTES, needs_user=True))
    engine.run_until_idle(timeout=30)
    assert engine.leftovers() == ["1 file left for you (--retry-kept asks the model again)"]


def test_files_that_were_handled_or_are_gone_are_not_left_over(inbox: Path, target: Path, cfg) -> None:
    (inbox / "invoice-march.txt").write_text("Invoice", encoding="utf-8")
    engine = make_engine(cfg)
    engine.run_until_idle(timeout=30)
    assert engine.leftovers() == []
    assert "left in the source" not in engine.run_log.path.read_text(encoding="utf-8")


def test_headless_run_prints_what_it_left(inbox: Path, target: Path, capsys) -> None:
    _repo(inbox / "invoice-tool", files=2)
    assert main(["run", str(inbox), "-t", str(target), "--once", "--no-tui", "--fake-llm"]) == 0
    out = capsys.readouterr().out
    assert "\nleft in the source:\n    1 git repository with 2 files: invoice-tool" in out


def test_reorganizing_gives_no_leftover_summary(target: Path) -> None:
    _repo(target / INVOICES / "ledger", files=2)
    cfg = load_config(target, target)
    cfg.follow, cfg.identify_workers, cfg.scan_interval = False, 1, 0.3
    engine = Engine(cfg, llm=FakeLLMClient(routes=ROUTES))
    engine.run_until_idle(timeout=30)
    assert engine.leftovers() == []
