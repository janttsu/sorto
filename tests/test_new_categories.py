"""No category fits a new topic: a new category in an existing area, numbered by sorto, or the file stays.

New IDs and categories are decided by the structure model, whichever model reads the files.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import make_engine
from sorto.cli import build_parser
from sorto.config import DEFAULT_LLM_MODEL, load_config
from sorto.engine import Engine
from sorto.jd import NewID, free_category_number, propose_new_category, scan_jd
from sorto.llm import FakeLLMClient, OpenAICompatClient
from sorto.models import Classification

LIFE = "10-19 Life"


def _new(name: str = "Car", category: str = "", confidence: float = 0.9):
    def handler(packet, _prompt):
        return Classification(
            label="document", confidence=confidence, jd_id="new", reason="a topic of its own", needs_user=False,
            summary="A service invoice for a car.", new_id_category=category, new_id_name=name,
        )

    return handler


class _Structure(FakeLLMClient):
    """Answers the focused questions: the name, the category, and a new category."""

    def __init__(self, handler=None, *, name=("", ""), category=("none", 0.95, "nothing fits"),
                 invent=("10-19", "Vehicles", 0.9, "its own theme"), **kw):
        super().__init__(handler=handler, **kw)
        self.name, self.category, self.invent = name, category, invent
        self.named: list[tuple] = []
        self.checked: list[tuple] = []
        self.invented: list[tuple] = []

    def name_new_id(self, answer, label, summary, categories, rules=""):
        self.named.append((answer, label, summary))
        return self.name

    def choose_category(self, topic, summary, categories):
        self.checked.append((topic, categories))
        return self.category

    def invent_category(self, topic, summary, areas, rules=""):
        self.invented.append((topic, areas, rules))
        return self.invent


def test_next_category_number_follows_the_area(target: Path) -> None:
    index = scan_jd(target)
    assert free_category_number(index, LIFE) == 14  # 11 and 13 exist: after the highest, the gap 12 stays free
    new = propose_new_category(index, "10-19", "Vehicles", "Car")
    assert isinstance(new, NewID) and new.new_category.rel == f"{LIFE}/14 Vehicles"
    assert new.item.id == "14.11" and new.item.rel == f"{LIFE}/14 Vehicles/14.11 Car"
    assert new.describe() == "14.11 Car in the new category 14 Vehicles"
    assert free_category_number(index, "50-59 Media") == 52
    (target / "60-69 Empty").mkdir()
    (target / "02.11 Shared folder").mkdir()  # an ID in the root takes its category number in 00-09
    (target / LIFE / "16 Made by hand").mkdir()
    index = scan_jd(target)
    assert free_category_number(index, "60-69 Empty") == 61  # N0 is left for the area's own management
    assert free_category_number(index, "00-09 System") == 3
    assert free_category_number(index, LIFE) == 17
    assert "60-69 Empty  (categories: none yet)" in index.area_outline()


def test_new_category_reuses_a_name_and_refuses_what_cannot_be_numbered(target: Path) -> None:
    index = scan_jd(target)
    reused = propose_new_category(index, "10-19 Life", "money", "Car")
    assert isinstance(reused, NewID) and reused.new_category is None and reused.item.id == "13.14"
    assert "does not exist" in propose_new_category(index, "70-79", "Vehicles", "Car")
    assert "does not exist" in propose_new_category(index, "15-25", "Vehicles", "Car")  # spans two areas
    inside = propose_new_category(index, "15-19", "Vehicles", "Car")  # a made-up range inside 10-19
    assert isinstance(inside, NewID) and inside.new_category.rel == f"{LIFE}/14 Vehicles"
    assert "does not exist" in propose_new_category(index, "none", "Vehicles", "Car")
    assert "no usable name" in propose_new_category(index, "10-19", "19", "Car")
    (target / LIFE / "19 Last").mkdir()
    full = scan_jd(target)
    assert "no free category numbers" in propose_new_category(full, "10-19", "Vehicles", "Car")
    assert "FULL" in full.area_outline().splitlines()[1]


def test_no_category_fits_so_a_new_one_is_created_with_its_first_id(inbox: Path, target: Path, cfg) -> None:
    (inbox / "service.pdf").write_bytes(b"%PDF service invoice")
    (inbox / "tyres.pdf").write_bytes(b"%PDF tyre invoice")
    llm = _Structure(_new("Car"))
    snap = make_engine(cfg, llm=llm).run_until_idle(timeout=30)
    car = target / LIFE / "14 Vehicles" / "14.11 Car"
    assert sorted(p.name for p in car.iterdir()) == ["service.pdf", "tyres.pdf"]
    assert sorted(p.name for p in (target / LIFE).iterdir()) == ["11 Health", "13 Money", "14 Vehicles"]
    assert [p.name for p in (target / LIFE / "14 Vehicles").iterdir()] == ["14.11 Car"]  # the second file reused it
    assert "14 Vehicles" in llm.invented[0][1] or "13 Money" in llm.invented[0][1]
    log = [json.loads(line) for line in cfg.progress_path.read_text(encoding="utf-8").splitlines()]
    actions = [r["action"] for r in log]
    assert actions.count("created_category") == 1 and actions.count("created_id") == 1
    assert actions.index("created_category") < actions.index("created_id") < actions.index("moved")
    assert any(h.new_id == "14.11 Car in the new category 14 Vehicles" for h in snap.history)
    text = next(cfg.runs_dir.glob("run-*.log")).read_text(encoding="utf-8")
    assert f"new category: created {LIFE}/14 Vehicles for " in text
    assert f"new ID: created {LIFE}/14 Vehicles/14.11 Car for " in text


@pytest.mark.parametrize(
    ("kwargs", "invent", "reason"),
    [
        ({}, ("none", "", 0.9, "it fits no area"), "and none could be made: it fits no area"),
        ({}, ("10-19", "Vehicles", 0.5, "maybe"), "unsure about a new category 'Vehicles'"),
        ({}, ("70-79", "Vehicles", 0.9, "new area"), "area 70-79 does not exist"),
        ({}, ("10-19", "14", 0.9, "a number"), "no usable name for the new category"),
        ({"allow_new_categories": False}, ("10-19", "Vehicles", 0.9, "x"), "no existing category fits a new ID for 'Car'"),
    ],
)
def test_no_category_can_be_made_so_the_file_stays_and_the_log_says_why(
    inbox: Path, target: Path, cfg, kwargs, invent, reason
) -> None:
    (inbox / "service.pdf").write_bytes(b"%PDF service invoice")
    before = sorted(p.relative_to(target).as_posix() for p in target.rglob("*"))
    snap = make_engine(cfg, llm=_Structure(_new("Car"), invent=invent), **kwargs).run_until_idle(timeout=30)
    assert (inbox / "service.pdf").is_file()
    assert sorted(p.relative_to(target).as_posix() for p in target.rglob("*")) == before  # nothing was created
    assert snap.history[0].outcome == "kept" and reason in snap.history[0].reason
    text = next(cfg.runs_dir.glob("run-*.log")).read_text(encoding="utf-8")
    assert "kept:" in text and reason in text


def test_dry_run_shows_the_new_category_but_creates_nothing(inbox: Path, target: Path, cfg) -> None:
    (inbox / "service.pdf").write_bytes(b"%PDF service invoice")
    snap = make_engine(cfg, llm=_Structure(_new("Car")), dry_run=True).run_until_idle(timeout=30)
    view = snap.history[0]
    assert view.outcome == "planned" and view.dest_rel == f"{LIFE}/14 Vehicles/14.11 Car/service.pdf"
    assert view.new_id == "14.11 Car in the new category 14 Vehicles"
    assert not (target / LIFE / "14 Vehicles").exists() and (inbox / "service.pdf").is_file()


def test_a_whole_folder_can_start_a_new_category(inbox: Path, target: Path, cfg) -> None:
    from sorto.folders import FolderAnswer

    car = inbox / "car papers"
    car.mkdir()
    for i in range(5):
        (car / f"service-{i}.txt").write_text(f"service {i}", encoding="utf-8")

    def unit(packet, _prompt):
        return FolderAnswer(
            summary="Papers of a car.", coherent=True, jd_id="new", subfolder="", confidence=0.9, reason="car",
            new_id_name="Car",
        )

    make_engine(cfg, llm=_Structure(folder_handler=unit)).run_until_idle(timeout=30)
    assert len(list((target / LIFE / "14 Vehicles/14.11 Car/car papers").glob("service-*.txt"))) == 5


def test_structure_model_decides_names_and_categories_not_the_model_that_reads_files(
    inbox: Path, target: Path, cfg
) -> None:
    (inbox / "digest.eml").write_text("weekly digest", encoding="utf-8")
    small = _Structure(_new("Stuff", category="11"), name=("Wrong", ""), category=("11", 0.9, "small model"))
    big = _Structure(name=("Newsletters", "a topic of its own"), category=("13", 0.9, "it is about money"))
    Engine(cfg, llm=small, structure_llm=big).run_until_idle(timeout=30)
    assert (target / LIFE / "13 Money/13.14 Newsletters/digest.eml").is_file()
    assert len(small.calls) == 1 and small.named == small.checked == small.invented == []
    assert big.calls == [] and big.checked[0][0] == "Newsletters"
    # the small model's own name is only a suggestion to the structure model
    assert big.named[0][0] == 'new; suggested name for a new ID: "Stuff"'


def test_structure_model_also_invents_the_category(inbox: Path, target: Path, cfg) -> None:
    (inbox / "service.pdf").write_bytes(b"%PDF service invoice")
    small = _Structure(_new("Car"), invent=("50-59", "Wrong", 0.9, "small"))
    big = _Structure(name=("Car", ""), invent=("10-19", "Vehicles", 0.9, "big"))
    Engine(cfg, llm=small, structure_llm=big).run_until_idle(timeout=30)
    assert (target / LIFE / "14 Vehicles/14.11 Car/service.pdf").is_file()
    assert small.invented == [] and len(big.invented) == 1


class _Stub:
    def __init__(self, model: str, health=(True, "ok")):
        self.model, self._health = model, health

    def health(self):
        return self._health


def test_which_model_is_the_structure_model(inbox: Path, target: Path, cfg, monkeypatch) -> None:
    assert cfg.structure_model == DEFAULT_LLM_MODEL  # the big model, whatever reads the files
    built: list[str] = []

    def fake_make_llm(c, fake=False):
        built.append(c.llm_model)
        return _Stub(c.llm_model, health)

    monkeypatch.setattr("sorto.engine.make_llm", fake_make_llm)
    health = (True, "ok")
    cfg.llm_model = "qwen3.5:9b-16k"
    reader = OpenAICompatClient(base_url=cfg.llm_url, model=cfg.llm_model)
    engine = Engine(cfg, llm=reader)
    structure = engine._structure_llm()
    assert structure is not reader and structure.model == DEFAULT_LLM_MODEL
    assert engine._structure_llm() is structure and built == [DEFAULT_LLM_MODEL]  # built once
    cfg.structure_model = ""  # the user turned it off: the reading model decides
    assert engine._structure_llm() is reader
    cfg.structure_model = cfg.llm_model
    assert engine._structure_llm() is reader
    cfg.structure_model = "not-here:1b"
    health = (False, "reachable, but model 'not-here:1b' is not pulled (2 models)")
    assert engine._structure_llm() is reader  # not on the server: said in the log, the reader decides
    assert any("not-here:1b is not available" in line for line in engine.snapshot().log_lines)
    engine.db.close()


def test_settings_for_structure_model_and_new_categories(inbox: Path, target: Path, tmp_path: Path) -> None:
    user = tmp_path / "xdg-config" / "sorto" / "config.toml"
    user.parent.mkdir(parents=True)
    user.write_text('[llm]\nstructure_model = "big:70b"\n[run]\nallow_new_categories = false\n', encoding="utf-8")
    cfg = load_config(inbox, target)
    assert cfg.structure_model == "big:70b" and cfg.allow_new_categories is False
    assert 'structure_model = "big:70b"' in cfg.to_toml() and "allow_new_categories = false" in cfg.to_toml()
    args = build_parser().parse_args(["run", str(inbox), "-t", str(target), "--no-new-categories"])
    user.unlink()
    assert load_config(inbox, target, cli=args).allow_new_categories is False
    assert load_config(inbox, target).allow_new_categories is True


def test_a_smaller_models_name_is_not_used_when_the_structure_model_gives_none(inbox: Path, target: Path, cfg) -> None:
    (inbox / "digest.eml").write_text("weekly digest", encoding="utf-8")
    before = sorted(p.relative_to(target).as_posix() for p in target.rglob("*"))
    small = _Structure(_new("Stuff", category="13"))
    big = _Structure(name=("", "cannot tell"), category=("13", 0.9, "money"))
    snap = Engine(cfg, llm=small, structure_llm=big).run_until_idle(timeout=30)
    assert (inbox / "digest.eml").is_file() and "no usable name" in snap.history[0].reason
    assert sorted(p.relative_to(target).as_posix() for p in target.rglob("*")) == before
