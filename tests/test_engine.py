from __future__ import annotations

import os
from pathlib import Path

from sorto.db import Database
from sorto.engine import Engine
from sorto.llm import FakeLLMClient
from sorto.models import AnalysisPacket, Classification
from sorto.util import sha256_file


def make_engine(cfg, llm=None, **kwargs) -> Engine:
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    return Engine(cfg, llm=llm or FakeLLMClient())


def _user_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if "_organization" in p.parts:
            continue
        out.append(p)
    return out


def test_dry_run_does_not_move(inbox: Path, cfg) -> None:
    src = inbox / "notes.txt"
    src.write_text("hello world", encoding="utf-8")
    engine = make_engine(cfg, dry_run=True)
    engine.run_until_idle(timeout=30)
    assert src.exists()
    assert src.read_text(encoding="utf-8") == "hello world"
    row = engine.db.get_by_rel("notes.txt")
    assert row is not None
    assert row["dest_rel"]
    assert row["status"] in {"planned", "done"}


def test_live_move_and_never_deletes(inbox: Path, cfg) -> None:
    a = inbox / "notes.txt"
    a.write_text("hello world", encoding="utf-8")
    b = inbox / "photo.jpg"
    b.write_bytes(b"\xff\xd8\xff" + b"x" * 50)
    before_hashes = sorted(sha256_file(p) for p in _user_files(inbox))
    engine = make_engine(cfg, dry_run=False)
    engine.run_until_idle(timeout=30)
    after = _user_files(inbox)
    after_hashes = sorted(sha256_file(p) for p in after)
    assert after_hashes == before_hashes
    assert not a.exists()
    assert any(p.name == "notes.txt" for p in after)
    row = engine.db.get_by_rel("documents/notes.txt") or engine.db.get_by_rel("notes.txt")
    assert row is not None
    assert row["status"] == "done"


def test_never_overwrites_and_collision_rename(inbox: Path, cfg) -> None:
    dest_dir = inbox / "documents"
    dest_dir.mkdir()
    existing = dest_dir / "notes.txt"
    existing.write_text("original", encoding="utf-8")
    incoming = inbox / "notes.txt"
    incoming.write_text("incoming", encoding="utf-8")
    engine = make_engine(cfg, dry_run=False)
    engine.run_until_idle(timeout=30)
    assert existing.exists()
    assert existing.read_text(encoding="utf-8") == "original"
    moved = dest_dir / "notes-2.txt"
    assert moved.exists()
    assert moved.read_text(encoding="utf-8") == "incoming"
    assert not incoming.exists()


def test_resume_does_not_reprocess_done(inbox: Path, cfg) -> None:
    (inbox / "notes.txt").write_text("hello", encoding="utf-8")
    llm = FakeLLMClient()
    engine = make_engine(cfg, llm=llm, dry_run=False)
    engine.run_until_idle(timeout=30)
    first_calls = len(llm.calls)
    assert first_calls >= 1
    llm2 = FakeLLMClient()
    engine2 = make_engine(cfg, llm=llm2, dry_run=False)
    engine2.run_until_idle(timeout=30)
    assert llm2.calls == []


def test_changed_file_is_rediscovered(inbox: Path, cfg) -> None:
    src = inbox / "notes.txt"
    src.write_text("hello", encoding="utf-8")
    engine = make_engine(cfg, dry_run=False)
    engine.run_until_idle(timeout=30)
    dest = inbox / "documents" / "notes.txt"
    assert dest.exists()
    dest.write_text("hello world changed", encoding="utf-8")
    os.utime(dest, None)
    llm = FakeLLMClient()
    engine2 = make_engine(cfg, llm=llm, dry_run=False)
    engine2.run_until_idle(timeout=30)
    assert len(llm.calls) >= 1


def test_invalid_llm_json_leaves_file(inbox: Path, cfg) -> None:
    src = inbox / "notes.txt"
    src.write_text("stay", encoding="utf-8")
    engine = make_engine(cfg, llm=FakeLLMClient(invalid=True), dry_run=False)
    engine.run_until_idle(timeout=30)
    assert src.exists()
    assert src.read_text(encoding="utf-8") == "stay"
    row = engine.db.get_by_rel("notes.txt")
    assert row is not None
    assert row["status"] == "error"


def test_dotdot_dest_stays_under_root(inbox: Path, cfg) -> None:
    src = inbox / "notes.txt"
    src.write_text("stay-inside", encoding="utf-8")

    def handler(packet: AnalysisPacket, _prompt: str) -> Classification:
        return Classification(
            label="evil",
            confidence=0.99,
            dest_rel="../escape.txt",
            rename=True,
            reason="try to leave root",
            needs_user=False,
        )

    engine = make_engine(cfg, llm=FakeLLMClient(handler=handler), dry_run=False)
    engine.run_until_idle(timeout=30)
    # File must not appear outside inbox.
    assert not (inbox.parent / "escape.txt").exists()
    remaining = _user_files(inbox)
    assert remaining
    for p in remaining:
        assert inbox in p.resolve().parents or p.resolve() == inbox
        assert p.read_text(encoding="utf-8") == "stay-inside"


def test_exclude_organization_not_processed(inbox: Path, cfg) -> None:
    (inbox / "keep.txt").write_text("k", encoding="utf-8")
    org_file = inbox / "_organization" / "prompts" / "classify.md"
    org_file.parent.mkdir(parents=True, exist_ok=True)
    if not org_file.exists():
        org_file.write_text("prompt", encoding="utf-8")
    before = org_file.read_text(encoding="utf-8")
    engine = make_engine(cfg, dry_run=False)
    engine.run_until_idle(timeout=30)
    assert org_file.read_text(encoding="utf-8") == before
    db = Database(cfg.db_path)
    rels = [r["src_rel"] for r in db.execute("SELECT src_rel FROM files").fetchall()]
    assert all("_organization" not in r for r in rels)


def test_parallel_identify_moves_all(inbox: Path, cfg) -> None:
    """Regression: dest_rel must not be lost when several files finish analysis together."""
    (inbox / "notes.txt").write_text("hello notes", encoding="utf-8")
    (inbox / "app.py").write_text("print(1)\n", encoding="utf-8")
    (inbox / "IMG_1234.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 40)
    engine = make_engine(cfg, dry_run=False, identify_workers=4, workers=1)
    engine.run_until_idle(timeout=30)
    rows = [dict(r) for r in engine.db.execute("SELECT src_rel, dest_rel, status, error FROM files")]
    files = sorted(
        str(p.relative_to(inbox))
        for p in inbox.rglob("*")
        if p.is_file() and "_organization" not in p.parts
    )
    errors = engine.db.ids_by_status("error")
    summary = [(r["src_rel"], r["dest_rel"], r["status"], r["error"]) for r in rows]
    assert errors == [], summary
    assert (inbox / "documents" / "notes.txt").exists(), (summary, files)
    assert (inbox / "code" / "app.py").exists(), (summary, files)
    assert (inbox / "images" / "photos" / "IMG_1234.jpg").exists(), (summary, files)
    assert not (inbox / "notes.txt").exists(), (summary, files)


def test_duplicate_goes_to_candidates(inbox: Path, cfg) -> None:
    (inbox / "a.txt").write_text("same-bytes", encoding="utf-8")
    engine = make_engine(cfg, dry_run=False)
    engine.run_until_idle(timeout=30)
    (inbox / "b.txt").write_text("same-bytes", encoding="utf-8")
    engine2 = make_engine(cfg, dry_run=False)
    engine2.run_until_idle(timeout=30)
    dup_dir = inbox / "_duplicates_candidates"
    assert dup_dir.exists()
    dups = list(dup_dir.iterdir())
    assert dups
    assert dups[0].read_text(encoding="utf-8") == "same-bytes"
    # original not deleted
    orig = list((inbox / "documents").glob("*.txt"))
    assert orig
    assert orig[0].read_text(encoding="utf-8") == "same-bytes"


def test_delete_duplicates_unlinks_copy_keeps_original(inbox: Path, cfg) -> None:
    (inbox / "a.txt").write_text("same-bytes", encoding="utf-8")
    engine = make_engine(cfg, dry_run=False)
    engine.run_until_idle(timeout=30)
    (inbox / "b.txt").write_text("same-bytes", encoding="utf-8")
    engine2 = make_engine(cfg, dry_run=False, delete_duplicates=True)
    engine2.run_until_idle(timeout=30)
    orig = list((inbox / "documents").glob("*.txt"))
    assert orig
    assert orig[0].read_text(encoding="utf-8") == "same-bytes"
    assert not (inbox / "b.txt").exists()
    assert not (inbox / "_duplicates_candidates").exists()


def test_delete_duplicates_dry_run_does_not_unlink(inbox: Path, cfg) -> None:
    (inbox / "a.txt").write_text("same-bytes", encoding="utf-8")
    engine = make_engine(cfg, dry_run=False)
    engine.run_until_idle(timeout=30)
    (inbox / "b.txt").write_text("same-bytes", encoding="utf-8")
    engine2 = make_engine(cfg, dry_run=True, delete_duplicates=True)
    engine2.run_until_idle(timeout=30)
    assert (inbox / "b.txt").exists()
    assert (inbox / "b.txt").read_text(encoding="utf-8") == "same-bytes"


def test_delete_duplicates_never_inside_git_repo(inbox: Path, cfg) -> None:
    (inbox / "a.txt").write_text("same-bytes", encoding="utf-8")
    engine = make_engine(cfg, dry_run=False)
    engine.run_until_idle(timeout=30)
    repo = inbox / "proj"
    (repo / ".git").mkdir(parents=True)
    (repo / "b.txt").write_text("same-bytes", encoding="utf-8")
    engine2 = make_engine(cfg, dry_run=False, delete_duplicates=True)
    engine2.run_until_idle(timeout=30)
    orig = list((inbox / "documents").glob("*.txt"))
    assert orig
    assert orig[0].read_text(encoding="utf-8") == "same-bytes"
    leftovers = [
        p
        for p in inbox.rglob("*.txt")
        if p.is_file() and "_organization" not in p.parts
    ]
    # Git-tree duplicate is never unlinked; it may still be moved to candidates.
    assert len(leftovers) >= 2
    assert any(p.read_text(encoding="utf-8") == "same-bytes" for p in leftovers if p != orig[0])
