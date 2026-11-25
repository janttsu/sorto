"""Sort the files of a Varsto folder: analyse, file, and index through MCP.

Varsto keeps the folder; sorto decides. For each file the key may change
(a read-write grant) that has a plain local copy and no current analysis,
the local model reads the file and proposes a Johnny.Decimal ID, as in
`sorto run`. Then sorto records the analysis and moves the file to its ID
inside the same Varsto folder. Every change goes through the MCP client
(`sorto.varsto`); nothing is written to the disk here. Varsto refuses to
overwrite a file, keeps the version a replacement leaves behind, and removes
only empty folders.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sorto.config import SortoConfig, packaged_prompt
from sorto.identify import identify_file
from sorto.jd import JDIndex, scan_jd
from sorto.models import Classification
from sorto.plan import PlanError, plan_destination
from sorto.varsto import VarstoError, VarstoMcp

JDEX_NAME = "JDex.md"
OUTLINE_CHARS = 60000


@dataclass
class Step:
    path: str
    action: str  # "moved", "filed-in-place", "skipped", "failed", "would-analyse"
    detail: str = ""
    dest: str = ""


@dataclass
class SortReport:
    folder: str
    steps: list[Step] = field(default_factory=list)
    removed_dirs: list[str] = field(default_factory=list)
    index_path: str = ""
    index_written: bool = False

    def count(self, action: str) -> int:
        return sum(1 for s in self.steps if s.action == action)


def folder_root(client: VarstoMcp, folder: str, *, write: bool) -> Path:
    """The plain copy of a granted folder on this device; write needs an rw grant."""
    for f in client.folders():
        if f.get("name") == folder:
            if write and f.get("access") != "rw":
                raise VarstoError(
                    f"{folder} is {f.get('access')}: sorting needs a read-write grant"
                )
            local = f.get("local_path")
            if not local:
                raise VarstoError(
                    f"{folder} is kept encrypted on this device: sorting needs its plain copy here"
                )
            return Path(local)
    raise VarstoError(f"no folder {folder!r} is granted to this key")


def _needs_analysis(f: dict[str, Any]) -> bool:
    if f.get("state") != "local" or not f.get("local_path"):
        return False
    return not f.get("analysis") or bool(f.get("analysis_stale"))


def _prompt(cfg: SortoConfig, index: JDIndex) -> str:
    try:
        text = cfg.prompt_path.read_text(encoding="utf-8")
    except OSError:
        text = packaged_prompt()
    outline = index.render(max_chars=OUTLINE_CHARS)
    return f"{text.rstrip()}\n\nTARGET JOHNNY.DECIMAL OUTLINE (the only IDs you may choose):\n{outline}\n"


def _index_text(index: JDIndex) -> str:
    lines = [
        "# Johnny.Decimal index",
        "",
        "Written by sorto from this folder. Edit the names here only if you also rename the folders.",
    ]
    lines.append("")
    for item in sorted(index.choices(), key=lambda i: i.id):
        lines.append(f"- `{item.id}` {item.name} (`{item.rel}`)")
    return "\n".join(lines) + "\n"


def _index_rel(root: Path) -> str:
    for p in sorted(root.rglob("*.md")):
        if "jdex" in p.name.lower():
            return p.relative_to(root).as_posix()
    return JDEX_NAME


def sort_folder(
    client: VarstoMcp,
    folder: str,
    cfg: SortoConfig,
    llm: Any,
    *,
    dry_run: bool = False,
    model_name: str = "",
    progress: Callable[[str], None] = lambda _msg: None,
) -> SortReport:
    report = SortReport(folder=folder)
    root = folder_root(client, folder, write=not dry_run)
    index = scan_jd(root)
    if not index.choices():
        raise VarstoError(f"no Johnny.Decimal IDs in {folder}: nothing to file into")
    files = client.files(folder)
    prompt = _prompt(cfg, index)
    emptied: set[str] = set()
    for f in files:
        rel = f.get("path", "")
        if not _needs_analysis(f):
            if f.get("state") != "local":
                report.steps.append(
                    Step(rel, "skipped", f"not a plain local copy here ({f.get('state')})")
                )
            continue
        if dry_run:
            report.steps.append(Step(rel, "would-analyse"))
            continue
        local = Path(f["local_path"])
        progress(f"analysing {rel}")
        try:
            packet = identify_file(local, rel, cfg)
            cls: Classification = llm.classify(packet, prompt)
            if cls.needs_user or not cls.jd_id:
                report.steps.append(Step(rel, "skipped", "the model did not choose an ID"))
                continue
            plan = plan_destination(root, index, packet, cls, src_path=local)
        except (PlanError, ValueError) as e:
            report.steps.append(Step(rel, "failed", str(e)))
            continue
        except Exception as e:  # the model or the file: keep going with the next file
            report.steps.append(Step(rel, "failed", f"{type(e).__name__}: {e}"))
            continue
        try:
            tags = [t for t in (cls.label, cls.jd_id) if t]
            client.set_analysis(
                folder, rel, cls.summary or cls.reason or rel, tags, model_name or None
            )
            if plan.dest_rel == rel:
                report.steps.append(Step(rel, "filed-in-place", plan.item.id, dest=rel))
                continue
            parent = str(Path(plan.dest_rel).parent)
            if parent not in ("", "."):
                client.mkdir(folder, parent)
            client.move(folder, rel, plan.dest_rel)
            report.steps.append(Step(rel, "moved", plan.item.id, dest=plan.dest_rel))
            old_parent = str(Path(rel).parent)
            if old_parent not in ("", "."):
                emptied.add(old_parent)
        except VarstoError as e:
            report.steps.append(Step(rel, "failed", str(e)))
    report.index_path = _index_rel(root)
    text = _index_text(index)
    current = (
        (root / report.index_path).read_text(encoding="utf-8")
        if (root / report.index_path).is_file()
        else None
    )
    if not dry_run and current != text:
        client.write(folder, report.index_path, text)
        report.index_written = True
    if not dry_run:
        for d in sorted(emptied, key=lambda p: p.count("/"), reverse=True):
            if d and (root / d).is_dir() and not any((root / d).iterdir()):
                try:
                    client.rmdir(folder, d)
                    report.removed_dirs.append(d)
                except VarstoError:
                    pass
    return report


__all__ = ["sort_folder", "folder_root", "SortReport", "Step"]
