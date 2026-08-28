from __future__ import annotations

from pathlib import Path

import pytest

from sorto.models import AnalysisPacket, Classification
from sorto.plan import PlanError, plan_destination


def _packet(name: str = "file.txt", **kwargs) -> AnalysisPacket:
    kw = dict(
        src_rel=name,
        filename=name,
        extension=Path(name).suffix,
        size=12,
        mtime_iso="2024-01-01T00:00:00+00:00",
        mtime_ns=1_700_000_000_000_000_000,
        mime="text/plain",
        magic="ASCII text",
        type_guess="text/plain",
        hex_preview="",
        text_preview="hi",
        extra_meta={},
        sha256="abc",
        top_level_folders=["documents"],
        dest_scheme="default",
        meaningless_name=False,
    )
    kw.update(kwargs)
    return AnalysisPacket(**kw)  # type: ignore[arg-type]


def test_plan_rejects_dotdot(tmp_path: Path) -> None:
    cls = Classification(
        label="document",
        confidence=0.9,
        dest_rel="../outside.txt",
        rename=False,
        reason="bad",
        needs_user=False,
    )
    with pytest.raises(PlanError):
        plan_destination(tmp_path, _packet(), cls)


def test_plan_low_confidence_goes_unsorted(tmp_path: Path) -> None:
    cls = Classification(
        label="maybe-invoice",
        confidence=0.2,
        dest_rel="documents/secret.txt",
        rename=False,
        reason="unsure",
        needs_user=False,
    )
    dest = plan_destination(tmp_path, _packet("secret.txt"), cls)
    assert dest.startswith("_unsorted/")


def test_plan_collision_rename(tmp_path: Path) -> None:
    (tmp_path / "documents").mkdir()
    (tmp_path / "documents" / "file.txt").write_text("x", encoding="utf-8")
    cls = Classification(
        label="document",
        confidence=0.9,
        dest_rel="documents/file.txt",
        rename=False,
        reason="ok",
        needs_user=False,
    )
    dest = plan_destination(tmp_path, _packet("file.txt"), cls)
    assert dest == "documents/file-2.txt"


def test_plan_keeps_original_extension(tmp_path: Path) -> None:
    cls = Classification(
        label="document",
        confidence=0.95,
        dest_rel="documents/file.md",
        rename=True,
        reason="would change ext",
        needs_user=False,
    )
    dest = plan_destination(
        tmp_path, _packet("file.txt", keep_extension=True), cls, allow_extension_fix=False
    )
    assert dest.endswith(".txt")
