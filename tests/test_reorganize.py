"""SOURCE == TARGET: tidy an existing Johnny.Decimal tree in place."""

from __future__ import annotations

from pathlib import Path

from conftest import ROUTES
from sorto.config import load_config
from sorto.engine import Engine
from sorto.llm import FakeLLMClient
from sorto.models import Classification

LIFE = "10-19 Life"
INV = f"{LIFE}/13 Money/13.13 Invoices"
PHOTOS = "50-59 Media/51 Pictures/51.11 Photos"
HEALTH_INBOX = f"{LIFE}/11 Health/11.01 Inbox"


def _write(root: Path, rel: str, text: str | None = None) -> Path:
    path = root / rel
    text = rel if text is None else text  # unique content: identical files would be duplicates
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _engine(target: Path, llm=None, **kw) -> Engine:
    cfg = load_config(target, target)
    cfg.follow = False
    cfg.identify_workers = 1
    cfg.scan_interval = 0.3
    for k, v in kw.items():
        setattr(cfg, k, v)
    return Engine(cfg, llm=llm or FakeLLMClient(routes=ROUTES))


def test_same_dir_means_reorganize(target: Path) -> None:
    cfg = load_config(target, target)
    assert cfg.reorganize
    assert not load_config(target / LIFE / "13 Money/13.01 Inbox", target).reorganize  # an inbox is sorted out


def test_reorganize_files_loose_inbox_and_misfiled(target: Path) -> None:
    _write(target, f"{LIFE}/13 Money/invoice-loose.txt")
    _write(target, f"{HEALTH_INBOX}/invoice-inbox.txt")
    _write(target, f"{PHOTOS}/invoice-misfiled.txt")
    _write(target, f"{INV}/invoice-ok.txt")
    snap = _engine(target).run_until_idle(timeout=30)
    for name in ("invoice-loose.txt", "invoice-inbox.txt", "invoice-misfiled.txt", "invoice-ok.txt"):
        assert (target / INV / name).is_file(), name
    by_name = {h.filename: h for h in snap.history}
    assert by_name["invoice-ok.txt"].outcome == "in_place"
    assert by_name["invoice-misfiled.txt"].outcome == "moved"
    assert by_name["invoice-misfiled.txt"].current_location == "51.11 Photos"
    assert snap.reorganize


def test_reorganize_leaves_deep_trees_notes_and_repos_alone(target: Path) -> None:
    deep = _write(target, f"{INV}/2024/invoice-deep.txt")
    notes = _write(target, "10-19 Life/11 Health/11.00 JDex/invoice-notes.txt")
    jdex = _write(target, "invoice JDex.md")
    repo = target / LIFE / "tools"
    (repo / ".git").mkdir(parents=True)
    code = _write(target, f"{LIFE}/tools/invoice-script.txt")
    llm = FakeLLMClient(routes=ROUTES)
    _engine(target, llm).run_until_idle(timeout=30)
    for path in (deep, notes, jdex, code):
        assert path.is_file(), path
    assert llm.calls == []


def test_reorganize_depth_one_rechecks_subfolders(target: Path) -> None:
    _write(target, f"{INV}/2024/invoice-deep.txt")
    llm = FakeLLMClient(routes=ROUTES)
    snap = _engine(target, llm, reorganize_depth=1).run_until_idle(timeout=30)
    assert [p.filename for p in llm.calls] == ["invoice-deep.txt"]
    assert (target / INV / "2024/invoice-deep.txt").is_file()
    assert snap.history[0].outcome == "in_place"


def test_filed_file_needs_higher_confidence_to_move(target: Path) -> None:
    _write(target, f"{PHOTOS}/invoice-maybe.txt")

    def unsure(packet, _prompt):
        return Classification(
            label="invoice", confidence=0.6, jd_id="13.13", reason="looks like a bill", needs_user=False,
            summary="An invoice.",
        )

    snap = _engine(target, FakeLLMClient(handler=unsure)).run_until_idle(timeout=30)
    assert (target / PHOTOS / "invoice-maybe.txt").is_file()
    view = snap.history[0]
    assert view.outcome == "kept"
    assert "0.60" in view.reason and "0.75" in view.reason


def test_reorganize_prompt_and_packet_say_where_the_file_is(target: Path) -> None:
    _write(target, f"{PHOTOS}/invoice-x.txt")
    llm = FakeLLMClient(routes=ROUTES)
    eng = _engine(target, llm, dry_run=True)
    assert "REORGANIZE MODE" in eng.system_prompt
    eng.run_until_idle(timeout=30)
    assert llm.calls[0].to_llm_dict()["currently_filed_in"] == "51.11 Photos"
    assert (target / PHOTOS / "invoice-x.txt").is_file()


def test_an_area_or_category_as_source_is_a_part_to_reorganize(target: Path, tmp_path: Path) -> None:
    from sorto.config import scope_in_target
    from sorto.util import state_dir

    money = target / LIFE / "13 Money"
    assert scope_in_target(money, target) == f"{LIFE}/13 Money"  # a category: it holds IDs
    assert scope_in_target(target / LIFE, target) == LIFE  # an area: its categories hold IDs
    assert scope_in_target(money / "13.01 Inbox", target) == ""  # an ID is an inbox to sort out
    assert scope_in_target(money / "13.13 Invoices/2024", target) == ""
    (target / "Loose stuff/sub").mkdir(parents=True)
    assert scope_in_target(target / "Loose stuff", target) == ""  # no IDs in it
    assert scope_in_target(target, target) == "" and scope_in_target(tmp_path, target) == ""
    cfg = load_config(money, target)
    assert cfg.reorganize and cfg.source == cfg.target == target and cfg.reorganize_scope == f"{LIFE}/13 Money"
    # its own index: not the one of a whole-target run, nor the one older versions kept for this pair
    assert cfg.state not in (state_dir(target, target), state_dir(money, target))
    assert cfg.state == load_config(money, target).state


def test_reorganizing_a_category_keeps_what_is_in_the_right_id(target: Path) -> None:
    """The run that prompted this: a category as SOURCE hid its own IDs, so nothing could stay."""
    stays = _write(target, f"{INV}/invoice-march.txt")
    leaves = _write(target, f"{INV}/sick-note.txt")
    inbox = _write(target, f"{LIFE}/13 Money/13.01 Inbox/invoice-new.txt")
    other = _write(target, f"{LIFE}/11 Health/11.12 Records/invoice-misfiled.txt")  # not in the part
    loose = _write(target, "invoice-loose.txt")  # not in the part either
    cfg = load_config(target / LIFE / "13 Money", target)
    cfg.follow, cfg.identify_workers, cfg.scan_interval = False, 1, 0.3
    llm = FakeLLMClient(routes=ROUTES)
    snap = Engine(cfg, llm=llm).run_until_idle(timeout=30)
    assert stays.is_file()  # 13.13 is still an ID the model may choose, and the file is already there
    assert not leaves.exists() and (target / LIFE / "11 Health/11.12 Records/sick-note.txt").is_file()
    assert not inbox.exists() and (target / INV / "invoice-new.txt").is_file()
    assert other.is_file() and loose.is_file()
    assert sorted(p.filename for p in llm.calls) == ["invoice-march.txt", "invoice-new.txt", "sick-note.txt"]
    assert snap.reorganize and snap.reorganize_scope == f"{LIFE}/13 Money"
    kept = next(h for h in snap.history if h.filename == "invoice-march.txt")
    assert kept.outcome in ("in_place", "kept") and "13.13" in kept.jd_id
    assert any("is part of the target: it is reorganized in place" in line for line in snap.log_lines)
    text = next(cfg.runs_dir.glob("run-*.log")).read_text(encoding="utf-8")
    assert f"reorganizing {LIFE}/13 Money of the target in place" in text


def test_an_id_folder_as_source_is_still_sorted_out(target: Path) -> None:
    inbox = target / LIFE / "13 Money/13.01 Inbox"
    _write(target, f"{LIFE}/13 Money/13.01 Inbox/invoice-new.txt")
    cfg = load_config(inbox, target)
    cfg.follow, cfg.identify_workers, cfg.scan_interval = False, 1, 0.3
    engine = Engine(cfg, llm=FakeLLMClient(routes=ROUTES))
    assert not cfg.reorganize and engine.index.get("13.01") is None  # the inbox itself is no place to file into
    engine.run_until_idle(timeout=30)
    assert (target / INV / "invoice-new.txt").is_file()
