from __future__ import annotations

from pathlib import Path

import pytest

from sorto.cli import main


def test_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as ei:
        main(["--help"])
    assert ei.value.code == 0


def test_run_requires_source_and_target(inbox: Path) -> None:
    with pytest.raises(SystemExit) as ei:
        main(["run"])
    assert ei.value.code == 2
    with pytest.raises(SystemExit) as ei:
        main(["run", str(inbox)])
    assert ei.value.code == 2


def test_headless_run(inbox: Path, target: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (inbox / "notes.txt").write_text("x", encoding="utf-8")
    rc = main(["run", str(inbox), "--target", str(target), "--once", "--no-tui", "--fake-llm"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "notes.txt" in out
    assert (inbox / "notes.txt").exists()  # fake LLM has no route → kept in source


def test_remote_llm_refused(inbox: Path, target: Path) -> None:
    with pytest.raises(SystemExit) as ei:
        main(["run", str(inbox), "-t", str(target), "--once", "--no-tui", "--llm-url", "http://8.8.8.8/v1"])
    assert "not this machine" in str(ei.value)


def test_index_command(target: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["index", str(target)]) == 0
    assert "13.13 Invoices" in capsys.readouterr().out
