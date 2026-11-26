from __future__ import annotations

import sys
from pathlib import Path

import pytest

from sorto.config import SortoConfig
from sorto.llm import FakeLLMClient
from sorto.varsto import VarstoError, VarstoMcp
from sorto.varsto_sort import folder_root, sort_folder

FAKE = Path(__file__).parent / "fixtures" / "fake_varsto_sort.py"
INVOICE_ID = "13.13"


def make_folder(root: Path) -> None:
    # Invented Johnny.Decimal structure and inbox.
    (root / "10-19 Life" / "13 Money" / "13.13 Invoices").mkdir(parents=True)
    (root / "10-19 Life" / "13 Money" / "13.13 Invoices" / "old.txt").write_text(
        "old invoice", encoding="utf-8"
    )
    (root / "inbox" / "2026-07").mkdir(parents=True)
    (root / "inbox" / "2026-07" / "invoice-example-07.txt").write_text(
        "Invoice 2026-07 for a fictional shop, total 12.00 EUR", encoding="utf-8"
    )
    (root / "inbox" / "2026-07" / "trip-notes.txt").write_text(
        "Train notes for a made-up trip", encoding="utf-8"
    )


def client(root: Path, access: str = "rw", placeholders: tuple[str, ...] = ()) -> VarstoMcp:
    return VarstoMcp(
        [sys.executable, str(FAKE), str(root), "archive", access, *placeholders], timeout=10
    )


def cfg_for(root: Path) -> SortoConfig:
    return SortoConfig(source=root, target=root)


def llm() -> FakeLLMClient:
    return FakeLLMClient(routes={"invoice": INVOICE_ID})


def test_dry_run_lists_and_changes_nothing(tmp_path: Path) -> None:
    make_folder(tmp_path)
    fake = llm()
    with client(tmp_path, access="ro") as v:
        report = sort_folder(v, "archive", cfg_for(tmp_path), fake, dry_run=True)
    assert {s.action for s in report.steps} == {"would-analyse"}
    assert fake.calls == []
    assert (tmp_path / "inbox" / "2026-07" / "invoice-example-07.txt").exists()


def test_sort_files_invoice_moves_and_index(tmp_path: Path) -> None:
    make_folder(tmp_path)
    fake = llm()
    with client(tmp_path) as v:
        report = sort_folder(v, "archive", cfg_for(tmp_path), fake, model_name="test-model")
    moved = [s for s in report.steps if s.action == "moved"]
    assert [s.path for s in moved] == ["inbox/2026-07/invoice-example-07.txt"]
    # No ID fits the trip notes: the model says so, and the file stays where it is.
    notes = next(s for s in report.steps if s.path.endswith("trip-notes.txt"))
    assert notes.action == "skipped" and "did not choose" in notes.detail
    assert (tmp_path / "inbox" / "2026-07" / "trip-notes.txt").exists()
    invoice = moved[0]
    assert invoice.dest.startswith("10-19 Life/13 Money/13.13 Invoices/")
    assert (tmp_path / invoice.dest).read_text(encoding="utf-8").startswith("Invoice 2026-07")
    # The inbox folder keeps the file it still holds; the emptied one is gone.
    assert (tmp_path / "inbox" / "2026-07" / "trip-notes.txt").exists()
    assert not (tmp_path / "inbox" / "2026-07" / "invoice-example-07.txt").exists()
    assert tmp_path.is_dir()
    assert report.index_written and (tmp_path / "JDex.md").exists()
    assert "13.13" in (tmp_path / "JDex.md").read_text(encoding="utf-8")


def test_the_index_is_not_a_file_to_sort(tmp_path: Path) -> None:
    make_folder(tmp_path)
    with client(tmp_path) as v:
        sort_folder(v, "archive", cfg_for(tmp_path), llm())
    assert (tmp_path / "JDex.md").exists()
    # A second run finds the index sorto wrote: it is neither read by the model nor moved.
    fake = llm()
    with client(tmp_path) as v:
        report = sort_folder(v, "archive", cfg_for(tmp_path), fake)
    assert all(s.path != "JDex.md" for s in report.steps)
    assert all("JDex" not in str(c) for c in fake.calls)
    assert (tmp_path / "JDex.md").exists()


def test_a_kept_analysis_does_not_stop_the_filing(tmp_path: Path) -> None:
    make_folder(tmp_path)
    invoice = "inbox/2026-07/invoice-example-07.txt"
    # Varsto holds an analysis of an earlier version and does not replace it.
    with VarstoMcp(
        [sys.executable, str(FAKE), str(tmp_path), "archive", "rw", f"stale:{invoice}"], timeout=10
    ) as v:
        report = sort_folder(v, "archive", cfg_for(tmp_path), llm())
    step = next(s for s in report.steps if s.path == invoice)
    assert step.action == "moved" and "kept the earlier analysis" in step.detail
    assert report.count("failed") == 0
    assert (tmp_path / step.dest).exists()


def test_placeholder_is_skipped_and_said_so(tmp_path: Path) -> None:
    make_folder(tmp_path)
    with client(tmp_path, placeholders=("inbox/2026-07/trip-notes.txt",)) as v:
        report = sort_folder(v, "archive", cfg_for(tmp_path), llm())
    skipped = [
        s for s in report.steps if s.action == "skipped" and s.path.endswith("trip-notes.txt")
    ]
    assert skipped and "placeholder" in skipped[0].detail
    assert (tmp_path / "inbox" / "2026-07" / "trip-notes.txt").exists()


def test_read_only_folder_is_refused(tmp_path: Path) -> None:
    make_folder(tmp_path)
    with client(tmp_path, access="ro") as v, pytest.raises(VarstoError, match="read-write"):
        folder_root(v, "archive", write=True)


def test_existing_name_is_never_replaced(tmp_path: Path) -> None:
    make_folder(tmp_path)
    (tmp_path / "10-19 Life" / "13 Money" / "13.13 Invoices" / "invoice-example-07.txt").write_text(
        "already here", encoding="utf-8"
    )
    with client(tmp_path) as v:
        sort_folder(v, "archive", cfg_for(tmp_path), llm())
    assert (
        tmp_path / "10-19 Life" / "13 Money" / "13.13 Invoices" / "invoice-example-07.txt"
    ).read_text(encoding="utf-8") == "already here"


def test_a_failing_file_does_not_stop_the_others(tmp_path: Path) -> None:
    make_folder(tmp_path)
    with client(tmp_path) as v:
        fake = FakeLLMClient(
            handler=lambda packet, prompt: (
                (_ for _ in ()).throw(RuntimeError("model down"))
                if "trip" in packet.filename
                else FakeLLMClient(routes={"invoice": INVOICE_ID}).classify(packet, prompt)
            )
        )
        report = sort_folder(v, "archive", cfg_for(tmp_path), fake)
    actions = {s.path.rsplit("/", 1)[-1]: s.action for s in report.steps}
    assert actions["trip-notes.txt"] == "failed"
    assert actions["invoice-example-07.txt"] == "moved"


def test_sort_command_dry_run_through_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from sorto import varsto as module
    from sorto.cli import main

    make_folder(tmp_path)

    class Fake(module.VarstoMcp):
        def __init__(self, command, **kw):
            super().__init__([sys.executable, str(FAKE), str(tmp_path), "archive", "rw"], **kw)

    monkeypatch.setattr("sorto.varsto.VarstoMcp", Fake)
    assert main(["varsto", "sort", "archive", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would-analyse" in out and "nothing was changed" in out
    assert (tmp_path / "inbox" / "2026-07" / "invoice-example-07.txt").exists()
