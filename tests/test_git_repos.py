"""Git repositories stay whole and where they are: nothing moves out of, inside or into one."""

from __future__ import annotations

from pathlib import Path

from conftest import ROUTES, make_engine
from sorto.config import load_config
from sorto.engine import Engine
from sorto.jd import scan_jd
from sorto.llm import FakeLLMClient
from sorto.models import Classification
from sorto.util import git_workdir, is_git_repo

INVOICES = "10-19 Life/13 Money/13.13 Invoices"


def _bare(path: Path) -> Path:
    for d in ("objects/pack", "refs/heads", "hooks"):
        (path / d).mkdir(parents=True)
    (path / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (path / "config").write_text("[core]\n\tbare = true\n", encoding="utf-8")
    (path / "description").write_text("invoice tool\n", encoding="utf-8")
    return path


def _files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def test_what_counts_as_a_repository(tmp_path: Path) -> None:
    work = tmp_path / "work"
    (work / ".git").mkdir(parents=True)
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".git").write_text("gitdir: ../work/.git/worktrees/linked\n", encoding="utf-8")
    bare = _bare(tmp_path / "tool.git")
    plain = tmp_path / "plain"
    (plain / "refs").mkdir(parents=True)  # a folder that happens to be called refs is not a repository
    assert is_git_repo(work) and is_git_repo(linked) and is_git_repo(bare)
    assert not is_git_repo(plain) and not is_git_repo(tmp_path / "missing")
    (work / "src").mkdir()
    (work / "src" / "a.py").write_text("x", encoding="utf-8")
    assert git_workdir(work / "src" / "a.py") == work
    assert git_workdir(work / "src" / "a.py", stop=tmp_path) == work
    assert git_workdir(work / "src" / "a.py", stop=work) is None  # the root that is being sorted does not count
    assert git_workdir(bare / "hooks" / "new-file", stop=tmp_path) == bare
    assert git_workdir(plain / "x.txt", stop=tmp_path) is None


def test_a_bare_repository_is_left_alone(inbox: Path, target: Path, cfg) -> None:
    repo = _bare(inbox / "backups" / "invoice-tool.git")
    (inbox / "invoice-loose.txt").write_text("Invoice", encoding="utf-8")
    before = _files(repo)
    llm = FakeLLMClient(routes=ROUTES)
    make_engine(cfg, llm=llm).run_until_idle(timeout=30)
    assert _files(repo) == before and [p.filename for p in llm.calls] == ["invoice-loose.txt"]
    assert (target / INVOICES / "invoice-loose.txt").is_file()


def test_nothing_is_filed_into_a_repository_inside_an_id(inbox: Path, target: Path, cfg) -> None:
    ledger = target / INVOICES / "ledger"
    (ledger / ".git").mkdir(parents=True)
    assert "ledger" not in scan_jd(target).get("13.13").subfolders  # not offered to the model either
    (inbox / "invoice-march.txt").write_text("Invoice", encoding="utf-8")

    def handler(packet, _prompt):
        return Classification(
            label="invoice", confidence=0.9, jd_id="13.13", subfolder="ledger", reason="r", needs_user=False,
            summary="An invoice.",
        )

    make_engine(cfg, llm=FakeLLMClient(handler=handler)).run_until_idle(timeout=30)
    assert (target / INVOICES / "invoice-march.txt").is_file()  # the ID itself, not the repository in it
    assert _files(ledger) == []


def test_an_id_folder_that_is_a_repository_gets_no_files(inbox: Path, target: Path, cfg) -> None:
    (target / INVOICES / ".git").mkdir()
    (inbox / "invoice-march.txt").write_text("Invoice", encoding="utf-8")
    snap = make_engine(cfg, llm=FakeLLMClient(routes=ROUTES)).run_until_idle(timeout=30)
    assert (inbox / "invoice-march.txt").is_file() and not (target / INVOICES / "invoice-march.txt").exists()
    view = snap.history[0]
    assert view.outcome == "kept" and "nothing is filed into a repository" in view.reason
    assert "13.13 Invoices" in view.reason


def test_the_last_check_before_a_move_names_the_repository(inbox: Path, target: Path, cfg) -> None:
    (inbox / "old" / "tool" / ".git").mkdir(parents=True)
    (inbox / "old" / "tool" / "src").mkdir()
    engine = make_engine(cfg)
    try:
        why = engine._repo_in_the_way("old/tool/src/main.py", f"{INVOICES}/main.py")
        assert why == "inside the git repository old/tool: repositories are kept whole, nothing in them is moved"
        assert engine._repo_in_the_way("old/notes.txt", f"{INVOICES}/notes.txt") == ""
        assert engine._repo_in_the_way("old/notes.txt", f"{INVOICES}/2031/04/notes.txt") == ""  # folder not there yet
    finally:
        engine.db.close()


def test_an_archive_that_is_itself_in_git_can_still_be_sorted(inbox: Path, target: Path, cfg) -> None:
    (target / ".git").mkdir()
    (inbox / ".git").mkdir()
    (inbox / "invoice-march.txt").write_text("Invoice", encoding="utf-8")
    make_engine(cfg, llm=FakeLLMClient(routes=ROUTES)).run_until_idle(timeout=30)
    assert (target / INVOICES / "invoice-march.txt").is_file()


def test_reorganizing_never_reaches_into_a_repository(target: Path) -> None:
    repo = target / INVOICES / "ledger"
    (repo / ".git").mkdir(parents=True)
    (repo / "sick-note.txt").write_text("sick", encoding="utf-8")
    bare = _bare(target / INVOICES / "tool.git")
    loose = target / INVOICES / "sick-leave.txt"
    loose.write_text("sick leave", encoding="utf-8")
    cfg = load_config(target, target)
    cfg.follow, cfg.identify_workers, cfg.scan_interval, cfg.reorganize_depth = False, 1, 0.3, 2
    llm = FakeLLMClient(routes=ROUTES)
    before = _files(bare)
    Engine(cfg, llm=llm).run_until_idle(timeout=30)
    assert (repo / "sick-note.txt").is_file() and _files(bare) == before
    assert [p.filename for p in llm.calls] == ["sick-leave.txt"] and not loose.exists()
