from __future__ import annotations

from pathlib import Path

import pytest

from conftest import packet
from sorto.jd import scan_jd
from sorto.models import Classification
from sorto.plan import PlanError, plan_destination

INV = "10-19 Life/13 Money/13.13 Invoices"


def _cls(jd_id: str = "13.13", **kw) -> Classification:
    return Classification(label="invoice", confidence=0.9, jd_id=jd_id, reason="r", needs_user=False, **kw)


def test_unknown_id_is_rejected(target: Path) -> None:
    index = scan_jd(target)
    with pytest.raises(PlanError):
        plan_destination(target, index, packet("a.txt"), _cls("99.99"), src_path=target / "x")
    with pytest.raises(PlanError):
        plan_destination(target, index, packet("a.txt"), _cls("11.00"), src_path=target / "x")


def test_collision_gets_suffix(target: Path) -> None:
    index = scan_jd(target)
    (target / INV / "a.txt").write_text("keep", encoding="utf-8")
    plan = plan_destination(target, index, packet("a.txt"), _cls(), src_path=target / "elsewhere")
    assert plan.dest_rel == f"{INV}/a-2.txt"


def test_extension_kept_on_rename(target: Path) -> None:
    index = scan_jd(target)
    p = packet("IMG_1234.jpg", meaningless_name=True)
    plan = plan_destination(target, index, p, _cls(new_filename="beach.png"), src_path=target / "x")
    assert plan.dest_rel == f"{INV}/beach.jpg" and plan.renamed


def test_filename_cannot_escape(target: Path) -> None:
    index = scan_jd(target)
    p = packet("scan0001.pdf", meaningless_name=True)
    plan = plan_destination(target, index, p, _cls(new_filename="../../etc/passwd"), src_path=target / "x")
    assert plan.dest_rel == f"{INV}/passwd.pdf"
