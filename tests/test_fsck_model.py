"""The model's review in sorto fsck: descriptions, name verdicts, findings, structure proposals."""

from __future__ import annotations

import io
from pathlib import Path

from sorto.fsck import prepare, run
from sorto.fsck_model import eta_text
from test_fsck import HEALTH, MONEY, Reviewer, tree  # noqa: F401  (fixture)

CATEGORIES = {
    "13": {
        "descriptions": [{"id": "13.13", "description": "Bills and receipts from shops and utilities"},
                         {"id": "99.99", "description": "invented"}],
        "duplicates": [{"ids": ["13.11", "13.13"], "reason": "both hold bank papers"}],
        "misplaced": [{"id": "13.12", "category": "11", "reason": "health insurance decisions"},
                      {"id": "11.11", "category": "13", "reason": "not this category's ID"}],
    },
    "11": {
        "names": [{"id": "11.12", "right": "note", "reason": "the files are patient lists"}],
        "renames": [{"id": "11.13", "name": "Dentist", "reason": "clearer"}],
        "descriptions": [{"id": "11.13", "description": "Dentist visits and bills"}],
    },
}
STRUCTURE = {"proposals": [{
    "title": "Merge Money into fewer IDs", "change": "Fold 13.11 Bank into 13.13 Invoices.",
    "why": "Both hold the same papers.", "steps": ["Move 13.11 into 13.13/Bank", "Remove 13.11"],
    "effort": "small",
}]}


def test_descriptions_go_into_the_proposed_entries(tree: Path) -> None:  # noqa: F811
    report = prepare(tree, Reviewer(CATEGORIES, STRUCTURE))
    money = next(c for c in report.changes if c.rel.endswith("13.00 JDex/JDex.md"))
    assert "| 13.13 | Invoices | Bills and receipts from shops and utilities |" in money.new.splitlines()
    assert "added 13.13 Invoices — Bills and receipts from shops and utilities" in money.reasons
    assert "invented" not in money.new


def test_where_the_model_finds_the_note_right_the_folder_is_reported_not_the_note_changed(tree: Path) -> None:  # noqa: F811
    report = prepare(tree, Reviewer(CATEGORIES, STRUCTURE))
    health = next(c for c in report.changes if c.rel.endswith("11.00 JDex/JDex.md"))
    assert "- `11.12` Patients — patient records" in health.new  # the note's name stays
    assert not any("11.12: name" in r for r in health.reasons)
    assert any(p.startswith(f"{HEALTH}/11.12 Records: the model finds the note's name \"Patients\" fits")
               for p in report.problems)


def test_findings_and_proposals_are_kept_for_display_and_invented_ids_dropped(tree: Path) -> None:  # noqa: F811
    insights = prepare(tree, Reviewer(CATEGORIES, STRUCTURE)).insights
    kinds = [(f.kind, f.text) for f in insights.findings]
    assert ("duplicate", "13.11 Bank and 13.13 Invoices: both hold bank papers") in kinds
    assert ("misplaced", "13.12 Taxes belongs in 11 Health: health insurance decisions") in kinds
    assert not any("11.11" in text for kind, text in kinds if kind == "misplaced")  # not 13's ID
    assert any(kind == "rename" and "11.13 Dental" in text and "\"Dentist\"" in text for kind, text in kinds)
    (proposal,) = insights.proposals
    assert proposal.title == "Merge Money into fewer IDs" and proposal.effort == "small"
    assert "Steps:\n  1. Move 13.11 into 13.13/Bank" in proposal.render()


def test_the_model_sees_files_notes_and_name_differences(tree: Path) -> None:  # noqa: F811
    (tree / MONEY / "13.13 Invoices" / "2024").mkdir()
    (tree / MONEY / "13.13 Invoices" / "2024" / "power-march.pdf").write_bytes(b"%PDF")
    (tree / MONEY / "13.13 Invoices" / "phone.pdf").write_bytes(b"%PDF")
    llm = Reviewer(CATEGORIES, STRUCTURE)
    prepare(tree, llm)
    money = next(q for q in llm.asked if q.startswith("CATEGORY: 13 Money"))
    assert "- 13.11 Bank | description: Statements | 0 files" in money
    assert "- 13.13 Invoices | NO DESCRIPTION | subfolders: 2024 | 2 files, e.g. phone.pdf, 2024/power-march.pdf" in money
    assert "OTHER CATEGORIES: 00 Admin, 11 Health, 14 Travel" in money
    health = next(q for q in llm.asked if q.startswith("CATEGORY: 11 Health"))
    assert 'NAME DIFFERENCES:\n- 11.12: note says "Patients", folder says "Records"' in health
    whole = llm.asked[-1]
    assert whole.startswith("TREE:\n00-09 System") and "FOUND IN THE CATEGORY REVIEWS:" in whole


def test_answers_are_cached_so_an_unchanged_tree_is_quick_the_second_time(tree: Path) -> None:  # noqa: F811
    prepare(tree, first := Reviewer(CATEGORIES, STRUCTURE))
    prepare(tree, second := Reviewer(CATEGORIES, STRUCTURE))
    assert len(first.asked) == 4 and second.asked == []  # 11, 13, 14 and the whole tree


def test_a_category_that_fails_is_reported_and_the_rest_goes_on(tree: Path) -> None:  # noqa: F811
    insights = prepare(tree, Reviewer(CATEGORIES, STRUCTURE, fail={"11"})).insights
    assert insights.errors == ["11 Health: model went away"]
    assert insights.proposals  # the whole-tree review still ran


def test_progress_goes_scanning_thinking_structure_done(tree: Path) -> None:  # noqa: F811
    seen = []
    prepare(tree, Reviewer(CATEGORIES, STRUCTURE), lambda p: seen.append((p.phase, p.done, p.total)))
    phases = [s[0] for s in seen]
    assert phases[0] == "scanning" and phases[-1] == "done"
    assert [s for s in seen if s[0] == "thinking"] == [("thinking", 0, 3), ("thinking", 1, 3), ("thinking", 2, 3)]
    assert phases.index("structure") > phases.index("thinking")
    assert eta_text(None) == "working out" and eta_text(42) == "42 s" and eta_text(185) == "3 min 05 s"


def test_plain_run_prints_the_proposals_in_english_before_the_diffs(tree: Path, capsys) -> None:  # noqa: F811
    out = io.StringIO()
    run(tree, llm=Reviewer(CATEGORIES, STRUCTURE), out=out, interactive=False)
    text = out.getvalue()
    assert text.index("Structure proposals") < text.index("Model findings") < text.index("[1/")
    assert "1. Merge Money into fewer IDs\nWhat: Fold 13.11 Bank into 13.13 Invoices.\nWhy: Both hold" in text
    assert "fsck: reviewing 1/3" in capsys.readouterr().err


def test_without_the_model_fsck_stops_before_proposing_anything(tree: Path, capsys) -> None:  # noqa: F811
    class Down(Reviewer):
        def health(self):
            return False, "connection refused"

    assert run(tree, llm=Down(), out=io.StringIO(), interactive=False) == 8
    assert "sorto fsck needs the model fake-35b to review the tree first: connection refused" in capsys.readouterr().err


def test_a_description_that_only_says_where_files_came_from_is_asked_for_again(tree: Path) -> None:  # noqa: F811
    (tree / "00-09 System/00 Admin/00.00 JDex/00.00 JDex.md").write_text(
        "# JDex\nGenerated by make-tree.py: edit that, not this.\n\n- 14.11 Trips — Source: old drive 06.11\n",
        encoding="utf-8",
    )
    llm = Reviewer({"14": {"descriptions": [{"id": "14.11", "description": "Tickets and plans for trips"}]}})
    report = prepare(tree, llm)
    travel = next(q for q in llm.asked if q.startswith("CATEGORY: 14 Travel"))
    assert "- 14.11 Trips | NO DESCRIPTION (only where it came from: Source: old drive 06.11)" in travel
    note = next(c for c in report.changes if c.rel.endswith("14.00 JDex/JDex.md"))
    assert "- 14.11 Trips — Tickets and plans for trips" in note.new
