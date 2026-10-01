"""A tree without Johnny.Decimal structure: sorto proposes one, the user accepts, then it sorts."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import make_engine
from sorto.bootstrap import create, normalize, survey, survey_text
from sorto.cli import main
from sorto.config import load_config
from sorto.folders import FolderAnswer
from sorto.jd import scan_jd
from sorto.llm import FakeLLMClient

PROPOSAL = {
    "areas": [
        {"id": "10-19", "name": "Life", "categories": [
            {"id": "11", "name": "Money", "ids": [
                {"id": "11.11", "name": "Invoices", "description": "bills and receipts"},
                {"id": "11.12", "name": "Bank"},
            ]},
        ]},
        {"id": "50-59", "name": "Media", "categories": [
            {"id": "51", "name": "Photos", "ids": [{"id": "51.11", "name": "Trips"}]},
        ]},
    ]
}


def test_normalize_renumbers_into_valid_johnny_decimal() -> None:
    raw = {"areas": [
        {"id": "10-19", "name": "Life", "categories": [
            {"id": "37", "name": "Money", "ids": [  # outside its area → renumbered to 11
                {"id": "37.02", "name": "Bills"}, {"id": "37.40", "name": "bills"}, {"id": "x", "name": "Tax"}]},
            {"id": "11", "name": "Health", "ids": [{"id": "11.99", "name": "Doctor/visits"}]},
            {"id": "13", "name": "Empty", "ids": []},
        ]},
        {"id": "10-19", "name": "Work", "categories": [{"id": "12", "name": "Jobs", "ids": [{"name": "CV"}]}]},
        {"id": "", "name": "", "categories": []},
    ]}
    s = normalize(raw)
    assert [a.id for a in s.areas] == ["10-19", "20-29"]  # the duplicate range moves to the next free one
    life = s.areas[0]
    assert [(c.id, c.name) for c in life.categories] == [("11", "Money"), ("12", "Health")]
    assert [(i.id, i.name) for i in life.categories[0].ids] == [("11.11", "Bills"), ("11.12", "Tax")]
    assert life.categories[1].ids[0].name == "Doctor visits" and life.categories[1].ids[0].id == "12.11"
    assert s.areas[1].categories[0].id == "21" and s.areas[1].categories[0].ids[0].id == "21.11"
    assert s.counts == (2, 3, 4)


def test_create_makes_folders_and_jdex_without_replacing_anything(tmp_path: Path) -> None:
    (tmp_path / "JDex.md").write_text("mine", encoding="utf-8")
    made = create(tmp_path, normalize(PROPOSAL))
    assert "10-19 Life/11 Money/11.11 Invoices" in made
    assert (tmp_path / "JDex.md").read_text(encoding="utf-8") == "mine"
    jdex = (tmp_path / "JDex (sorto).md").read_text(encoding="utf-8")
    assert "- `11.11` Invoices — bills and receipts" in jdex
    index = scan_jd(tmp_path)
    assert index.get("11.11").description == "bills and receipts"
    assert [i.id for i in index.choices()] == ["11.11", "11.12", "51.11"]


def test_survey_counts_but_does_not_open_git_repositories(tmp_path: Path) -> None:
    (tmp_path / "Photos" / "2021" / "2021-05").mkdir(parents=True)
    for i in range(3):
        (tmp_path / "Photos" / "2021" / "2021-05" / f"IMG_{i}.jpg").write_bytes(b"x")
    repo = tmp_path / "Code" / "tool"
    (repo / ".git").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / "README.md").write_text("x", encoding="utf-8")
    (repo / "src" / "main.py").write_text("x", encoding="utf-8")
    stats, total = survey(tmp_path)
    by = {s.rel: s for s in stats}
    assert by["Photos/2021/2021-05"].files == 3 and by["Photos/2021/2021-05"].kinds == {"image": 3}
    assert by["Code/tool"].git and by["Code/tool"].files == 2
    assert total == 4  # the repository's own files, not .git, are counted once
    assert "git repositories inside" in survey_text(stats, total, "tree")


def _cli_llm(monkeypatch: pytest.MonkeyPatch, **kw) -> FakeLLMClient:
    llm = FakeLLMClient(structure=PROPOSAL, routes={"invoice": "11.11", "bank": "11.12"}, **kw)
    monkeypatch.setattr("sorto.cli.make_llm", lambda cfg, fake=False: llm)
    return llm


def _loose_tree(root: Path) -> None:
    (root / "Downloads").mkdir(parents=True)
    (root / "Downloads" / "invoice-march.txt").write_text("Invoice 12 EUR", encoding="utf-8")
    (root / "bank-statement.txt").write_text("balance", encoding="utf-8")


def test_run_creates_the_structure_then_sorts_into_it(tmp_path: Path, monkeypatch, capsys) -> None:
    tree = tmp_path / "unsorted"
    _loose_tree(tree)
    llm = _cli_llm(monkeypatch)
    assert main(["run", str(tree), "-t", str(tree), "--once", "--no-tui", "--accept-structure"]) == 0
    out = capsys.readouterr().out
    assert "11.11 Invoices" in out and "Created" in out
    assert (tree / "10-19 Life/11 Money/11.11 Invoices/invoice-march.txt").is_file()
    assert (tree / "10-19 Life/11 Money/11.12 Bank/bank-statement.txt").is_file()
    assert "Downloads" in llm.surveys[0]


def test_structure_needs_consent(tmp_path: Path, monkeypatch, capsys) -> None:
    tree = tmp_path / "unsorted"
    _loose_tree(tree)
    _cli_llm(monkeypatch)
    with pytest.raises(SystemExit) as exc:  # stdin is not a terminal and nothing was accepted
        main(["run", str(tree), "-t", str(tree), "--once", "--no-tui"])
    assert "no Johnny.Decimal IDs" in str(exc.value)
    assert "--accept-structure" in capsys.readouterr().err
    assert not list(tree.glob("10-19*"))
    with pytest.raises(SystemExit) as exc:
        main(["init", str(tree), "--dry-run"])
    assert exc.value.code == 0
    assert "Dry run: nothing created" in capsys.readouterr().out
    assert not list(tree.glob("10-19*"))


def test_loose_folders_move_whole_when_reorganizing(tmp_path: Path) -> None:
    tree = tmp_path / "unsorted"
    tree.mkdir()
    create(tree, normalize(PROPOSAL))
    trip = tree / "Photos" / "Rome 2019"
    trip.mkdir(parents=True)
    for i in range(6):
        (trip / f"IMG_{i}.jpg").write_bytes(b"\xff\xd8" + bytes([i]) * 40)

    def unit(packet, _prompt):
        return FolderAnswer(summary="A trip.", coherent=True, jd_id="51.11", subfolder="", confidence=0.9, reason="trip")

    cfg = load_config(tree, tree)
    llm = FakeLLMClient(folder_handler=unit)
    make_engine(cfg, llm=llm, follow=False, identify_workers=1, folder_max_files=3).run_until_idle(timeout=30)
    # "Photos" (6 files, has a subfolder) is too big for max 3 and is split; "Rome 2019" is flat, so it is
    # judged whole anyway.
    assert [p.rel for p in llm.folder_calls] == ["Photos/Rome 2019"]
    assert len(list((tree / "50-59 Media/51 Photos/51.11 Trips/Rome 2019").iterdir())) == 6


def test_git_repositories_are_never_split(inbox: Path, target: Path, cfg) -> None:
    repo = inbox / "tool"
    (repo / ".git").mkdir(parents=True)
    (repo / "invoice-template.txt").write_text("x", encoding="utf-8")
    llm = FakeLLMClient(routes={"invoice": "13.13"})
    make_engine(cfg, llm=llm).run_until_idle(timeout=30)
    assert (repo / "invoice-template.txt").is_file()
    assert llm.calls == []


def test_edited_proposal_is_read_back_and_checked(tmp_path: Path, monkeypatch, capsys) -> None:
    from sorto.bootstrap import PROPOSAL_HEADER, parse_tree

    structure = normalize(PROPOSAL)
    assert parse_tree(PROPOSAL_HEADER + structure.render()).folders() == structure.folders()
    edited = PROPOSAL_HEADER + structure.render().replace(
        "    11.12 Bank", "    11.12 Banking\n    11.13 Taxes — tax returns"
    )
    bad = [
        ("10-19 Life\n  21 Money\n", "not inside area"),
        ("10-19 Life\n  11 Money\n    12.11 X\n", "not under its category"),
        ("10-19 Life\n  11 Money\n    11.11 A\n    11.11 B\n", "used twice"),
        ("10-29 Life\n", "spans ten numbers"),
        ("Life stuff\n", "not an area, category or ID"),
    ]
    for text, message in bad:
        with pytest.raises(ValueError, match=message):
            parse_tree(text)
    proposal = tmp_path / "proposal.md"
    proposal.write_text(edited, encoding="utf-8")
    tree = tmp_path / "unsorted"
    _loose_tree(tree)
    _cli_llm(monkeypatch)
    assert main(["init", str(tree), "--from", str(proposal), "--accept-structure"]) == 0
    assert (tree / "10-19 Life/11 Money/11.12 Banking").is_dir()
    assert (tree / "10-19 Life/11 Money/11.13 Taxes").is_dir()
    assert "- `11.13` Taxes — tax returns" in (tree / "JDex.md").read_text(encoding="utf-8")


def test_proposal_is_saved_for_editing(tmp_path: Path, monkeypatch, capsys) -> None:
    tree = tmp_path / "unsorted"
    _loose_tree(tree)
    _cli_llm(monkeypatch)
    with pytest.raises(SystemExit):
        main(["init", str(tree), "--dry-run"])
    out = capsys.readouterr().out
    saved = Path(out.split("To change names or IDs first: edit ", 1)[1].split("\n", 1)[0])
    assert saved.is_file() and "11.11 Invoices" in saved.read_text(encoding="utf-8")
    assert str(saved).startswith(str(Path(__import__("os").environ["SORTO_HOME"])))  # not in the user's tree
