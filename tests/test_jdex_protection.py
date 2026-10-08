"""A JDex note is the index of a Johnny.Decimal tree: never sent to the model, never moved, also from the source."""

from __future__ import annotations

from pathlib import Path

from conftest import ROUTES, make_engine
from sorto.llm import FakeLLMClient
from sorto.util import is_jdex_note


def test_what_counts_as_a_jdex_note() -> None:
    assert is_jdex_note("JDex.md") and is_jdex_note("00.00 JDex.md") and is_jdex_note("old/jdex-notes.md")
    assert is_jdex_note("10-19 Life/13 Money/13.00 JDex/JDex.md")
    assert is_jdex_note("10-19 Life/13 Money/13.00 JDex/budget-notes.md")  # anything in an index folder
    assert not is_jdex_note("backup/KGbvjdExIi9ZoKy3r8Qur7v2k84.cnt")  # letters around it: not the word
    assert not is_jdex_note("10-19 Life/13 Money/13.13 Invoices/invoice.pdf")
    assert not is_jdex_note("13.001 measurements.csv")


def test_the_sources_own_index_stays_where_it_is(inbox: Path, target: Path, cfg) -> None:
    """The source is a Johnny.Decimal tree of its own, made earlier by `sorto init`."""
    (inbox / "JDex.md").write_text("# JDex\n\nCreated by sorto. Edit freely.\n", encoding="utf-8")
    notes = inbox / "10-19 Old" / "11 Papers" / "11.00 JDex"
    notes.mkdir(parents=True)
    (notes / "JDex.md").write_text("- 11.11 Letters\n", encoding="utf-8")
    (inbox / "invoice-march.txt").write_text("Invoice", encoding="utf-8")
    llm = FakeLLMClient(routes={**ROUTES, "jdex": "00.01"})
    engine = make_engine(cfg, llm=llm)
    engine.run_until_idle(timeout=30)
    assert [p.filename for p in llm.calls] == ["invoice-march.txt"]  # the model never saw them
    assert (inbox / "JDex.md").is_file() and (notes / "JDex.md").is_file()
    assert not any(target.rglob("JDex.md")) or all("00.00 JDex" in str(p) for p in target.rglob("JDex.md"))
    assert ("2 JDex notes: 10-19 Old/11 Papers/11.00 JDex/JDex.md, JDex.md (the index of a Johnny.Decimal tree; "
            "sorto never moves these)") in engine.leftovers()


def test_the_last_check_before_a_move_refuses_a_jdex_note(inbox: Path, target: Path, cfg) -> None:
    engine = make_engine(cfg)
    try:
        why = engine._repo_in_the_way("JDex.md", "00-09 System/00 Admin/00.01 Inbox/JDex.md")
        assert why == "a JDex note (the index of a Johnny.Decimal tree): sorto never moves these"
        assert engine._repo_in_the_way("notes.md", "00-09 System/00 Admin/00.01 Inbox/notes.md") == ""
    finally:
        engine.db.close()
