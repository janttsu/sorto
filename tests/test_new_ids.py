"""New IDs: the folder is created first, with the next free number, then the file moves in."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import make_engine
from sorto.cli import main
from sorto.jd import NewID, clean_id_name, propose_new_id, scan_jd
from sorto.llm import FakeLLMClient
from sorto.models import Classification

MONEY = "10-19 Life/13 Money"


def _answer(jd_id: str, *, confidence: float = 0.9, category: str = "", name: str = ""):
    def handler(packet, _prompt):
        return Classification(
            label="letter", confidence=confidence, jd_id=jd_id, reason="no existing ID fits", needs_user=False,
            summary="A newsletter.", new_id_category=category, new_id_name=name,
        )

    return FakeLLMClient(handler=handler)


def test_next_free_number_follows_the_category(target: Path) -> None:
    index = scan_jd(target)
    new = propose_new_id(index, "13", "Subscriptions")
    assert isinstance(new, NewID) and not new.reused
    assert new.item.id == "13.14"  # 13.13 is the highest regular ID in 13
    assert new.item.rel == f"{MONEY}/13.14 Subscriptions"
    assert propose_new_id(index, "11", "Dental").item.id == "11.13"  # after 11.12
    empty = target / "10-19 Life" / "15 Travel"
    empty.mkdir()
    assert propose_new_id(scan_jd(target), "15", "Trips").item.id == "15.11"  # .00–.10 stay reserved


def test_folders_on_disk_count_even_if_not_indexed(target: Path) -> None:
    index = scan_jd(target)
    (target / MONEY / "13.20 Made by hand").mkdir()  # appeared after the index was built
    assert propose_new_id(index, "13", "Subscriptions").item.id == "13.21"


def test_same_name_reuses_and_bad_requests_are_refused(target: Path) -> None:
    index = scan_jd(target)
    reused = propose_new_id(index, "13", "invoices")
    assert isinstance(reused, NewID) and reused.reused and reused.item.id == "13.13"
    assert "does not exist" in propose_new_id(index, "77", "Anything")
    assert "no usable name" in propose_new_id(index, "13", "  ")
    (target / MONEY / "13.99 Last").mkdir()
    assert "no free ID numbers" in propose_new_id(scan_jd(target), "13", "Overflow")
    assert clean_id_name("40.36 - emails") == "emails"
    assert clean_id_name("a/b\\c") == "a b c"


def test_new_id_folder_is_created_before_the_move(inbox: Path, target: Path, cfg) -> None:
    (inbox / "digest-1.eml").write_text("From: news@example.com\n\nweekly digest 1", encoding="utf-8")
    (inbox / "digest-2.eml").write_text("From: news@example.com\n\nweekly digest 2", encoding="utf-8")
    snap = make_engine(cfg, llm=_answer("new", category="13", name="Newsletters")).run_until_idle(timeout=30)
    new_dir = target / MONEY / "13.14 Newsletters"
    assert sorted(p.name for p in new_dir.iterdir()) == ["digest-1.eml", "digest-2.eml"]
    assert not (target / MONEY / "13.15 Newsletters").exists()  # the second file reused the new ID
    log = [json.loads(line) for line in cfg.progress_path.read_text(encoding="utf-8").splitlines()]
    created = [r for r in log if r["action"] == "created_id"]
    assert len(created) == 1 and created[0]["jd_id"] == "13.14"
    first = [r["action"] for r in log].index("created_id")
    assert first < [r["action"] for r in log].index("moved")
    assert any(h.new_id == "13.14 Newsletters" for h in snap.history)


def test_made_up_number_is_replaced_by_the_next_free_one(inbox: Path, target: Path, cfg) -> None:
    (inbox / "digest.eml").write_text("weekly digest", encoding="utf-8")
    make_engine(cfg, llm=_answer("13.77 - Newsletters")).run_until_idle(timeout=30)
    assert (target / MONEY / "13.14 Newsletters" / "digest.eml").is_file()
    assert not list(target.rglob("13.77*"))


@pytest.mark.parametrize(
    ("kwargs", "llm_kwargs", "reason"),
    [
        ({}, {"confidence": 0.6}, "below the 0.80"),
        ({"allow_new_ids": False}, {}, "not an ID in the target"),
        ({}, {"category": "77"}, "category 77 does not exist"),
    ],
)
def test_new_id_refused_keeps_the_file(inbox: Path, target: Path, cfg, kwargs, llm_kwargs, reason) -> None:
    (inbox / "digest.eml").write_text("weekly digest", encoding="utf-8")
    llm = _answer("new", **({"category": "13", "name": "Newsletters"} | llm_kwargs))
    snap = make_engine(cfg, llm=llm, **kwargs).run_until_idle(timeout=30)
    assert (inbox / "digest.eml").is_file()
    assert not list(target.rglob("*Newsletters*"))
    assert reason in snap.history[0].reason


def test_dry_run_shows_but_does_not_create(inbox: Path, target: Path, cfg) -> None:
    (inbox / "digest.eml").write_text("weekly digest", encoding="utf-8")
    snap = make_engine(cfg, llm=_answer("new", category="13", name="Newsletters"), dry_run=True).run_until_idle(
        timeout=30
    )
    view = snap.history[0]
    assert view.new_id == "13.14 Newsletters" and view.outcome == "planned"
    assert view.dest_rel == f"{MONEY}/13.14 Newsletters/digest.eml"
    assert not (target / MONEY / "13.14 Newsletters").exists()


def test_run_refuses_a_target_without_ids(inbox: Path, tmp_path: Path, capsys) -> None:
    empty = tmp_path / "empty-target"
    empty.mkdir()
    with pytest.raises(SystemExit) as exc:
        main(["run", str(inbox), "-t", str(empty), "--once", "--no-tui", "--fake-llm"])
    assert "no Johnny.Decimal IDs found" in str(exc.value)


class _Chooser(FakeLLMClient):
    def __init__(self, handler, answer):
        super().__init__(handler=handler)
        self.answer = answer
        self.asked: list[str] = []

    def choose_category(self, topic, summary, categories):
        self.asked.append(categories)
        return self.answer


def test_focused_category_check_overrides_the_first_guess(inbox: Path, target: Path, cfg) -> None:
    (inbox / "workouts.csv").write_text("date,exercise,sets\n2025-01-07,squat,5\n", encoding="utf-8")
    first = _answer("new", category="13", name="Workouts")  # first guess: Money
    llm = _Chooser(first.handler, ("11", 0.9, "exercise is health"))
    make_engine(cfg, llm=llm).run_until_idle(timeout=30)
    assert (target / "10-19 Life/11 Health/11.13 Workouts/workouts.csv").is_file()
    assert "11 Health" in llm.asked[0] and "13.13 Invoices" in llm.asked[0]
    assert not list(target.rglob("13.14*"))


@pytest.mark.parametrize("answer", [("none", 1.0, "nothing fits"), ("11", 0.4, "maybe")])
def test_focused_check_can_refuse(inbox: Path, target: Path, cfg, answer) -> None:
    (inbox / "car.pdf").write_bytes(b"%PDF service invoice")
    llm = _Chooser(_answer("new", category="13", name="Car").handler, answer)
    snap = make_engine(cfg, llm=llm).run_until_idle(timeout=30)
    assert (inbox / "car.pdf").is_file()
    assert not list(target.rglob("*Car*"))
    assert snap.history[0].outcome == "kept"


class _Namer(FakeLLMClient):
    """Answers the focused question sorto asks when an answer names no ID and no name for one."""

    def __init__(self, handler=None, *, answer=("", ""), **kw):
        super().__init__(handler=handler, **kw)
        self.answer = answer
        self.asked: list[tuple] = []

    def name_new_id(self, answer, label, summary, categories, rules=""):
        self.asked.append((answer, label, summary, categories, rules))
        return self.answer


@pytest.mark.parametrize("garbled", ["13.77", "10-19 Life", "13 - 01", "10-19 Life/13 Money", ""])
def test_answer_without_id_or_name_gets_a_name_and_the_next_number(inbox: Path, target: Path, cfg, garbled) -> None:
    (inbox / "digest.eml").write_text("weekly digest", encoding="utf-8")
    llm = _Namer(_answer(garbled, category="13").handler, answer=("Newsletters", "a topic of its own"))
    snap = make_engine(cfg, llm=llm).run_until_idle(timeout=30)
    assert (target / MONEY / "13.14 Newsletters" / "digest.eml").is_file()
    assert sorted(p.name for p in (target / MONEY).iterdir()) == ["13.01 Inbox", "13.13 Invoices", "13.14 Newsletters"]
    assert snap.history[0].new_id == "13.14 Newsletters"
    asked = llm.asked[0]
    assert asked[0] == garbled and asked[2] == "A newsletter." and "13.13 Invoices" in asked[3]


def test_a_name_that_already_exists_in_the_category_is_reused(inbox: Path, target: Path, cfg) -> None:
    (inbox / "bill.eml").write_text("your bill", encoding="utf-8")
    llm = _Namer(_answer("10-19 Life", category="13").handler, answer=("invoices", "it is an invoice"))
    snap = make_engine(cfg, llm=llm).run_until_idle(timeout=30)
    assert (target / MONEY / "13.13 Invoices" / "bill.eml").is_file()
    assert sorted(p.name for p in (target / MONEY).iterdir()) == ["13.01 Inbox", "13.13 Invoices"]  # nothing created
    assert snap.history[0].jd_id == "13.13" and snap.history[0].new_id == ""


@pytest.mark.parametrize("answer", [("", "no idea"), ("07", "a number"), ("13.20 2234489_5825", "junk")])
def test_no_usable_name_even_when_asked_keeps_the_file(inbox: Path, target: Path, cfg, answer) -> None:
    (inbox / "digest.eml").write_text("weekly digest", encoding="utf-8")
    snap = make_engine(cfg, llm=_Namer(_answer("13.77").handler, answer=answer)).run_until_idle(timeout=30)
    assert (inbox / "digest.eml").is_file()
    assert sorted(p.name for p in (target / MONEY).iterdir()) == ["13.01 Inbox", "13.13 Invoices"]
    assert "no usable name" in snap.history[0].reason


def test_naming_question_sees_the_users_rules_and_a_name_that_came_with_the_answer(inbox: Path, target: Path, cfg) -> None:
    cfg.rules_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.rules_file.write_text("- Newsletters get an ID of their own.\n", encoding="utf-8")
    (inbox / "digest.eml").write_text("weekly digest", encoding="utf-8")
    llm = _Namer(_answer("13.77").handler, answer=("Newsletters", ""))
    make_engine(cfg, llm=llm).run_until_idle(timeout=30)
    assert "Newsletters get an ID of their own." in llm.asked[0][4]
    (inbox / "digest-2.eml").write_text("weekly digest 2", encoding="utf-8")
    # a name that came with the answer is a suggestion; the focused question still settles it
    named = _Namer(_answer("13.77 - Bulletins").handler, answer=("Newsletters", ""))
    make_engine(cfg, llm=named).run_until_idle(timeout=30)
    assert named.asked[0][0] == '13.77 - Bulletins; suggested name for a new ID: "Bulletins"'
    assert (target / MONEY / "13.14 Newsletters" / "digest-2.eml").is_file()
    assert not list(target.rglob("*Bulletins*"))
    # no name from the question: the same model's own suggestion stands
    (inbox / "digest-3.eml").write_text("weekly digest 3", encoding="utf-8")
    silent = _Namer(_answer("13.77 - Bulletins").handler, answer=("", ""))
    make_engine(cfg, llm=silent).run_until_idle(timeout=30)
    assert (target / MONEY / "13.15 Bulletins" / "digest-3.eml").is_file()


def test_number_only_names_are_not_names() -> None:
    assert clean_id_name("07") == "" and clean_id_name("2234489_5825") == "" and clean_id_name("51.18 07") == ""
    assert clean_id_name("AI") == "AI" and clean_id_name("Trips 2024") == "Trips 2024"


def test_folder_with_a_garbled_answer_gets_a_new_id(inbox: Path, target: Path, cfg) -> None:
    from sorto.folders import FolderAnswer

    club = inbox / "club"
    club.mkdir()
    for i in range(5):
        (club / f"minutes-{i}.txt").write_text(f"minutes of meeting {i}", encoding="utf-8")

    def unit(packet, _prompt):
        return FolderAnswer(
            summary="Minutes of a club.", coherent=True, jd_id="10-19 Life", subfolder="", confidence=0.9,
            reason="club", new_id_category="11",
        )

    llm = _Namer(folder_handler=unit, answer=("Club", "its own topic"))
    make_engine(cfg, llm=llm).run_until_idle(timeout=30)
    (new_id,) = [p for p in target.rglob("*Club") if p.is_dir()]
    assert new_id.name[3:5] >= "11" and new_id.name[5:] == " Club"
    assert len(list((new_id / "club").glob("minutes-*.txt"))) == 5
    assert llm.asked[0][0] == "10-19 Life" and llm.calls == []  # no file was asked about on its own


def test_one_topic_gets_one_id_even_when_the_category_check_wavers(inbox: Path, target: Path, cfg) -> None:
    class Wavering(_Namer):
        categories = iter(["13", "11", "51"])

        def choose_category(self, topic, summary, categories):
            return next(self.categories), 0.9, "fits"

    for i in range(3):
        (inbox / f"digest-{i}.eml").write_text(f"weekly digest {i}", encoding="utf-8")
    llm = Wavering(_answer("10-19 Life").handler, answer=("Newsletters", "a topic of its own"))
    make_engine(cfg, llm=llm).run_until_idle(timeout=30)
    (new_id,) = [p for p in target.rglob("* Newsletters") if p.is_dir()]
    assert new_id.name == "13.14 Newsletters" and len(list(new_id.iterdir())) == 3


def test_retry_kept_asks_again_about_files_left_for_the_user(inbox: Path, target: Path, cfg) -> None:
    (inbox / "digest.eml").write_text("weekly digest", encoding="utf-8")
    first = make_engine(cfg, llm=_Namer(_answer("13.77").handler, answer=("", "no idea"))).run_until_idle(timeout=30)
    assert "no usable name" in first.history[0].reason and (inbox / "digest.eml").is_file()
    better = _Namer(_answer("13.77").handler, answer=("Newsletters", "a topic of its own"))
    assert make_engine(cfg, llm=better).run_until_idle(timeout=30).history == []  # a plain run leaves it alone
    make_engine(cfg, llm=better, retry_kept=True).run_until_idle(timeout=30)
    assert (target / MONEY / "13.14 Newsletters" / "digest.eml").is_file()
    from sorto.cli import build_parser

    assert build_parser().parse_args(["run", str(inbox), "-t", str(target), "--retry-kept"]).retry_kept is True


def test_a_new_id_cannot_just_repeat_its_categorys_name(inbox: Path, target: Path, cfg) -> None:
    index = scan_jd(target)
    assert "only repeat the name of its category" in propose_new_id(index, "13", "money")
    (inbox / "digest.eml").write_text("weekly digest", encoding="utf-8")
    snap = make_engine(cfg, llm=_answer("new", category="13", name="Money")).run_until_idle(timeout=30)
    assert (inbox / "digest.eml").is_file() and not list(target.rglob("13.14*"))
    assert "only repeat the name of its category" in snap.history[0].reason
