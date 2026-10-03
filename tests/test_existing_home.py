"""Before a new ID: one more project or item of a kind an ID already holds gets a folder in that ID."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import make_engine
from sorto.jd import scan_jd
from sorto.llm import FakeLLMClient, existing_home_question, parse_existing_home_answer
from sorto.models import Classification

MONEY = "10-19 Life/13 Money"
INVOICES = f"{MONEY}/13.13 Invoices"


def _new(name: str = "Car repairs", confidence: float = 0.95):
    def handler(packet, _prompt):
        return Classification(
            label="invoice", confidence=confidence, jd_id="new", reason="a topic of its own", needs_user=False,
            summary="A garage invoice for a car repair.", new_id_category="13", new_id_name=name,
        )

    return handler


class _Structure(FakeLLMClient):
    """Answers the focused questions: the name, the category, and whether an existing ID holds the kind."""

    def __init__(self, handler=None, *, home=("13.13", 0.9, "invoices of any kind"), **kw):
        super().__init__(handler=handler, **kw)
        self.home = home
        self.asked: list[tuple] = []

    def name_new_id(self, answer, label, summary, categories, rules=""):
        return "Car repairs", "a garage"

    def choose_category(self, topic, summary, categories):
        return "13", 0.95, "money matters"

    def existing_home(self, topic, summary, category, ids, rules=""):
        self.asked.append((topic, category, ids))
        return self.home


def test_a_topic_an_existing_id_holds_gets_a_folder_there_not_a_new_id(inbox: Path, target: Path, cfg) -> None:
    (inbox / "garage-march.pdf").write_bytes(b"%PDF garage invoice, March")
    (inbox / "garage-may.pdf").write_bytes(b"%PDF garage invoice, May")
    llm = _Structure(_new())
    engine = make_engine(cfg, llm=llm)
    snap = engine.run_until_idle(timeout=30)
    folder = target / INVOICES / "Car repairs"
    assert sorted(p.name for p in folder.iterdir()) == ["garage-march.pdf", "garage-may.pdf"]
    assert sorted(p.name for p in (target / MONEY).iterdir()) == ["13.01 Inbox", "13.13 Invoices"]  # no 13.14
    topic, category, ids = llm.asked[0]
    assert topic == "Car repairs" and category == "13 Money"
    assert ids.splitlines()[0].startswith("13.13 Invoices — all personal bills and receipts")
    assert "13.01" not in ids  # the inbox is no home
    first, second = sorted(snap.history, key=lambda v: v.seq)
    assert first.new_id == "" and "an existing ID holds this kind of file, so no new ID" in first.folder
    assert first.folder.startswith("new folder Car repairs in 13.13 Invoices")
    assert second.folder.startswith("folder Car repairs in 13.13 Invoices")  # there already for the second file
    text = engine.run_log.path.read_text(encoding="utf-8")
    assert f"no new ID: {first.src_rel}: new folder Car repairs in 13.13 Invoices instead of a new ID" in text


@pytest.mark.parametrize(
    "home",
    [("none", 0.9, "a different kind of thing"), ("13.13", 0.5, "maybe"), ("11.12", 0.95, "another category")],
)
def test_otherwise_the_new_id_is_made_as_before(inbox: Path, target: Path, cfg, home) -> None:
    (inbox / "garage-march.pdf").write_bytes(b"%PDF garage invoice")
    make_engine(cfg, llm=_Structure(_new(), home=home)).run_until_idle(timeout=30)
    assert (target / MONEY / "13.14 Car repairs" / "garage-march.pdf").is_file()


def test_with_new_subfolders_off_the_file_goes_into_the_existing_id_itself(inbox: Path, target: Path, cfg) -> None:
    (inbox / "garage-march.pdf").write_bytes(b"%PDF garage invoice")
    make_engine(cfg, llm=_Structure(_new()), new_subfolders="off").run_until_idle(timeout=30)
    assert (target / INVOICES / "garage-march.pdf").is_file()
    assert not (target / INVOICES / "Car repairs").exists() and not (target / MONEY / "13.14 Car repairs").exists()


def test_the_question_and_its_answer(target: Path) -> None:
    index = scan_jd(target)
    assert index.ids_outline("13").splitlines() == [
        "13.13 Invoices — all personal bills and receipts  [has year folders (YYYY)]"
    ]
    assert index.ids_outline("51") == "51.11 Photos"
    question = existing_home_question("Car repairs", "A garage invoice.", "13 Money", index.ids_outline("13"), "rule X")
    assert "CATEGORY: 13 Money" in question and "THE USER'S RULES:\nrule X" in question
    assert parse_existing_home_answer('{"id": "13.13 Invoices", "confidence": 0.9, "reason": "r"}') == (
        "13.13", 0.9, "r"
    )
    assert parse_existing_home_answer('{"id": "none", "confidence": 1.5}')[:2] == ("none", 1.0)
    assert parse_existing_home_answer('{"id": "13.13", "confidence": 0.8, "reason": "cut off')[0] == "13.13"
