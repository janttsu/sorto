"""The model's part of ``sorto fsck``: every category reviewed, then the whole structure thought through.

Runs before any change is proposed. The structure model (``structure_model``,
the big one by default) gets, per category, its IDs with what the notes say,
their subfolders and samples of their files, and answers: descriptions for
IDs that have none, which name is right where a note and a folder disagree,
IDs whose names say nothing, duplicates and misplaced IDs. Then it gets the
whole tree with those findings and, thinking it through, proposes
simplifications or bigger changes to the structure and the numbering.

Descriptions and name verdicts feed the proposed note changes; everything
about folders is shown as text and never applied. Answers are cached per
category and per tree, so a second run over an unchanged tree is quick.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sorto.jd import FIRST_REGULAR_ID, JDIndex, JDItem, free_category_number
from sorto.util import state_home

SAMPLE_FILES = 12  # file names shown per ID
COUNT_LIMIT = 5000  # files counted per ID before "5000+"
CATEGORY_GUESS_S = 12.0  # time per category before the first one has been measured
STRUCTURE_GUESS_S = 150.0  # the thinking question at the end
# "Source: old drive 06.11": where the files came from, not what they are; asked for like no description.
PROVENANCE_RE = re.compile(r"^\s*(source|sources|from|lähde|lähteet)\s*:", re.IGNORECASE)


def needs_description(item: JDItem) -> bool:
    return not item.description or bool(PROVENANCE_RE.match(item.description))


@dataclass
class Finding:
    """Something the model found about folders: shown as text, never applied."""

    kind: str  # "duplicate", "misplaced", "rename", "name"
    text: str


@dataclass
class Proposal:
    title: str
    change: str
    why: str
    steps: list[str]
    effort: str
    warnings: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [self.title, "", f"What: {self.change}", "", f"Why: {self.why}"]
        if self.steps:
            lines += ["", "Steps:"] + [f"  {n}. {s}" for n, s in enumerate(self.steps, 1)]
        lines += ["", f"Effort: {self.effort}"]
        lines += [f"Check: {w}" for w in self.warnings]
        return "\n".join(lines)


@dataclass
class Insights:
    descriptions: dict[str, str] = field(default_factory=dict)
    folder_name_wrong: dict[str, str] = field(default_factory=dict)  # ID -> why the note's name is the right one
    findings: list[Finding] = field(default_factory=list)
    proposals: list[Proposal] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    model: str = ""


@dataclass
class Progress:
    """What the TUI and the plain output show while the model works."""

    phase: str = "scanning"  # scanning, thinking, structure, done
    done: int = 0
    total: int = 0
    current: str = ""
    eta_s: float | None = None
    elapsed_s: float = 0.0


ProgressFn = Callable[[Progress], None]


# --------------------------------------------------------------------- scan


def _id_facts(root: Path, item: JDItem) -> tuple[int, list[str]]:
    """How many files an ID holds (up to COUNT_LIMIT) and some of their names, from several subfolders."""
    count, samples, seen_dirs = 0, [], set()
    for dirpath, dirnames, filenames in os.walk(root / item.rel):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d != ".git")
        visible = sorted(f for f in filenames if not f.startswith("."))
        count += len(visible)
        if visible and len(samples) < SAMPLE_FILES and (dirpath not in seen_dirs):
            seen_dirs.add(dirpath)
            take = 3 if dirpath != str(root / item.rel) else 6
            rel_dir = os.path.relpath(dirpath, root / item.rel)
            samples += [f if rel_dir == "." else f"{rel_dir}/{f}" for f in visible[:take]]
        if count >= COUNT_LIMIT:
            break
    return count, samples[:SAMPLE_FILES]


def _category_question(
    index: JDIndex, num: str, facts: dict[str, tuple[int, list[str]]], mismatches: dict[str, str]
) -> str:
    others = ", ".join(name for n, name in sorted(index.categories.items()) if n != num)
    lines = [f"CATEGORY: {index.categories[num]} (area {index.category_areas.get(num) or '-'})",
             f"OTHER CATEGORIES: {others}", "", "IDS:"]
    for item in index.category_ids(num):
        count, samples = facts.get(item.id, (0, []))
        parts = [f"- {item.id} {item.name}"]
        if needs_description(item):
            parts.append("NO DESCRIPTION" + (f" (only where it came from: {item.description})" if item.description else ""))
        else:
            parts.append(f"description: {item.description}")
        if item.subfolders:
            parts.append(f"subfolders: {', '.join(item.subfolders[:15])}")
        many = f"{count}+" if count >= COUNT_LIMIT else str(count)
        parts.append(f"{many} files" + (f", e.g. {', '.join(samples)}" if samples else ""))
        lines.append(" | ".join(parts))
    diffs = [f"- {i}: note says \"{name}\", folder says \"{index.items[i].name}\""
             for i, name in sorted(mismatches.items()) if i[:2] == num and i in index.items]
    if diffs:
        lines += ["", "NAME DIFFERENCES:", *diffs]
    return "\n".join(lines)


def _structure_question(index: JDIndex, facts: dict[str, tuple[int, list[str]]], findings: list[Finding]) -> str:
    lines = ["TREE:"]
    for area in index.areas:
        lines.append(area)
        for num, name in sorted(index.categories.items()):
            if index.category_areas.get(num) != area:
                continue
            lines.append(f"  {name}")
            for item in index.category_ids(num):
                count = facts.get(item.id, (0, []))[0]
                desc = f" — {item.description[:100]}" if item.description else ""
                subs = f" [subfolders: {', '.join(item.subfolders[:8])}]" if item.subfolders else ""
                lines.append(f"    {item.id} {item.name}{desc} ({count} files){subs}")
    loose = [i for i in index.items.values() if not i.category and not i.is_index]
    if loose:
        lines.append("IDS OUTSIDE ANY CATEGORY: " + ", ".join(f"{i.id} {i.name}" for i in loose))
    lines += ["", "FREE NUMBERS (use these for anything new; every other number is taken or does not exist):"]
    lines += [f"- next free ID in {name}: {nxt}" for name, nxt in _free_ids(index).items()]
    lines += [f"- next free category in {area}: {num}" for area, num in _free_categories(index).items()]
    if findings:
        lines += ["", "FOUND IN THE CATEGORY REVIEWS:"] + [f"- {f.kind}: {f.text}" for f in findings]
    return "\n".join(lines)


def _free_ids(index: JDIndex) -> dict[str, str]:
    """Category name -> its next free ID (after the highest, at least NN.11), as sorto would number it."""
    out = {}
    for num, name in sorted(index.categories.items()):
        used = [int(i[3:]) for i in index.items if i[:2] == num and int(i[3:]) >= FIRST_REGULAR_ID]
        nxt = max(used, default=FIRST_REGULAR_ID - 1) + 1
        if nxt <= 99:
            out[name] = f"{num}.{nxt:02d}"
    return out


def _free_categories(index: JDIndex) -> dict[str, str]:
    out = {}
    for area in index.areas:
        free = free_category_number(index, area)
        if free is not None:
            out[area] = f"{free:02d}"
    return out


def _unknown_ids(proposal: Proposal, index: JDIndex, free: set[str]) -> list[str]:
    """IDs a proposal names that neither exist nor are the next free number: the model made them up."""
    text = " ".join([proposal.title, proposal.change, proposal.why, *proposal.steps])
    found = re.findall(r"(?<![\d.])(\d{2}\.\d{2})(?!\d|\.\d)", text)
    return sorted({i for i in found if i not in index.items and i not in free})


# -------------------------------------------------------------------- model


def _cache_path(root: Path) -> Path:
    digest = hashlib.sha256(str(root).encode("utf-8", "surrogateescape")).hexdigest()[:12]
    return state_home() / "fsck-cache" / f"{root.name or 'root'}-{digest}.json"


def _load_cache(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_cache(path: Path, cache: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass  # only a cache


def _key(model: str, question: str) -> str:
    return hashlib.sha256(f"{model}\0{question}".encode()).hexdigest()


def _ids(value: Any) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def _read_category(answer: dict[str, Any], index: JDIndex, num: str, insights: Insights) -> None:
    """Keep only what is about this category's IDs; the model may not invent any."""
    own = {i.id for i in index.category_ids(num)}

    def label(jd_id: str) -> str:
        item = index.items.get(jd_id)
        return f"{jd_id} {item.name}" if item else jd_id

    for d in answer.get("descriptions") or []:
        jd_id, text = str(d.get("id", "")), " ".join(str(d.get("description", "")).split())
        if jd_id in own and text and needs_description(index.items[jd_id]):
            insights.descriptions[jd_id] = text[:200]
    for n in answer.get("names") or []:
        jd_id, right, why = str(n.get("id", "")), str(n.get("right", "")).lower(), str(n.get("reason", ""))
        if jd_id in own and right == "note":
            insights.folder_name_wrong[jd_id] = why
    for r in answer.get("renames") or []:
        jd_id, name = str(r.get("id", "")), str(r.get("name", "")).strip()
        if jd_id in own and name:
            insights.findings.append(Finding(
                "rename", f"{label(jd_id)}: the name does not say what it holds; suggested \"{name}\" "
                f"({r.get('reason', '')})"))
    for d in answer.get("duplicates") or []:
        ids = _ids(d.get("ids"))
        if len(ids) >= 2 and any(i.split("/")[0] in own for i in ids):
            shown = [label(i) if i in index.items else i for i in ids]
            insights.findings.append(Finding("duplicate", f"{' and '.join(shown)}: {d.get('reason', '')}"))
    for m in answer.get("misplaced") or []:
        jd_id, cat = str(m.get("id", "")), str(m.get("category", ""))[:2]
        if jd_id in own and cat in index.categories and cat != num:
            insights.findings.append(Finding(
                "misplaced", f"{label(jd_id)} belongs in {index.categories[cat]}: {m.get('reason', '')}"))


def analyse(
    root: Path, index: JDIndex, mismatches: dict[str, str], llm: Any, progress: ProgressFn | None = None
) -> Insights:
    """Scan, review every category, then the whole structure. Errors are collected, not raised."""
    report = progress or (lambda _p: None)
    t0 = time.monotonic()
    insights = Insights(model=str(getattr(llm, "model", "")))
    ids = [i for i in index.items.values() if not i.is_index]
    facts: dict[str, tuple[int, list[str]]] = {}
    for n, item in enumerate(ids, 1):
        report(Progress("scanning", n - 1, len(ids), f"{item.id} {item.name}", None, time.monotonic() - t0))
        try:
            facts[item.id] = _id_facts(root, item)
        except OSError:
            facts[item.id] = (0, [])
    categories = [num for num in sorted(index.categories) if index.category_ids(num)]
    cache_file = _cache_path(root)
    cache = _load_cache(cache_file)
    asked: list[float] = []
    reviewer = getattr(llm, "review_category", None)
    for n, num in enumerate(categories):
        left = len(categories) - n
        per = sum(asked) / len(asked) if asked else CATEGORY_GUESS_S
        report(Progress("thinking", n, len(categories), index.categories[num],
                        per * left + STRUCTURE_GUESS_S, time.monotonic() - t0))
        if reviewer is None:
            break
        question = _category_question(index, num, facts, mismatches)
        key = _key(insights.model, question)
        answer = cache.get(key)
        if answer is None:
            started = time.monotonic()
            try:
                answer = reviewer(question)
            except Exception as e:  # one category failing does not stop the rest
                insights.errors.append(f"{index.categories[num]}: {e}"[:300])
                continue
            asked.append(time.monotonic() - started)
            cache[key] = answer
            _save_cache(cache_file, cache)
        _read_category(answer if isinstance(answer, dict) else {}, index, num, insights)
    structure = getattr(llm, "review_structure", None)
    if structure is not None:
        report(Progress("structure", 0, 1, "the whole tree", STRUCTURE_GUESS_S, time.monotonic() - t0))
        question = _structure_question(index, facts, insights.findings)
        key = _key(insights.model, "structure\0" + question)
        answer = cache.get(key)
        if answer is None:
            try:
                answer = structure(question)
                cache[key] = answer
                _save_cache(cache_file, cache)
            except Exception as e:
                insights.errors.append(f"structure review: {e}"[:300])
                answer = {}
        for p in (answer or {}).get("proposals") or []:
            if not isinstance(p, dict) or not p.get("title"):
                continue
            insights.proposals.append(Proposal(
                title=str(p.get("title", "")).strip(), change=str(p.get("change", "")).strip(),
                why=str(p.get("why", "")).strip(), steps=[str(s) for s in p.get("steps") or []],
                effort=str(p.get("effort", "")).strip() or "?",
            ))
        free = set(_free_ids(index).values())
        for proposal in insights.proposals:
            made_up = _unknown_ids(proposal, index, free)
            if made_up:
                proposal.warnings.append(
                    f"names {', '.join(made_up)}, which does not exist and is not a free number: read it as "
                    "\"the next free ID\"")
    report(Progress("done", 1, 1, "", 0.0, time.monotonic() - t0))
    return insights


def structure_llm(target: Path, model: str | None = None) -> Any:
    """The model that reviews: *model*, or ``structure_model``, with the user's connection settings."""
    from sorto.config import config_for_model, load_config
    from sorto.engine import _fit_context, make_llm

    cfg = load_config(target, target)
    name = model or cfg.structure_model or cfg.llm_model
    cfg = config_for_model(cfg, name) if name != cfg.llm_model else cfg
    llm = make_llm(cfg)
    _fit_context(cfg, llm)
    return llm


def eta_text(seconds: float | None) -> str:
    if seconds is None:
        return "working out"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} s"
    return f"{seconds // 60} min {seconds % 60:02d} s"
