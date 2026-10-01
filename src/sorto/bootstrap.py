"""Create a Johnny.Decimal structure for a tree that has none yet.

sorto surveys what is there (folders, file counts, types, dates, sample
names), asks the local model for a small structure that fits it, and then
numbers it itself so the result follows the Johnny.Decimal rules: areas
``X0-X9``, categories inside their area, IDs from ``.11`` upwards without
gaps. Nothing is created until the user has seen the tree and agreed.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sorto.eta import file_kind
from sorto.jd import clean_id_name

SURVEY_CHARS = 9000
MAX_AREAS = 10
MAX_CATEGORIES = 10
MAX_IDS = 20
SKIP_DIRS = {".git", ".svn", ".hg", "node_modules", ".venv", "venv", "__pycache__"}


@dataclass
class FolderStat:
    rel: str
    files: int = 0
    kinds: dict[str, int] = field(default_factory=dict)
    first: float = 0.0
    last: float = 0.0
    samples: list[str] = field(default_factory=list)
    git: bool = False


def survey(root: Path, *, depth: int = 3, max_files: int = 3_000_000) -> tuple[list[FolderStat], int]:
    """File counts, types, dates and sample names per folder, down to *depth* levels."""
    stats: dict[str, FolderStat] = {}
    total = 0
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel = Path(dirpath).relative_to(root).as_posix()
        parts = [] if rel == "." else rel.split("/")
        git = ".git" in dirnames
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS)
        repo_subdirs = list(dirnames) if git else []
        if git:
            dirnames[:] = []  # a repository is one thing: count it, do not survey its insides
        # Deeper folders are counted with their ancestor at *depth* levels.
        key = "/".join(parts[:depth]) or "."
        st = stats.setdefault(key, FolderStat(rel=key))
        st.git = st.git or (git and len(parts) <= depth)
        for name in filenames:
            if name.startswith("."):
                continue
            total += 1
            st.files += 1
            kind = file_kind(name)
            st.kinds[kind] = st.kinds.get(kind, 0) + 1
            if len(st.samples) < 6 and (st.files % 97 == 1):
                st.samples.append(name)
            try:
                mtime = os.lstat(os.path.join(dirpath, name)).st_mtime if st.files % 50 == 1 else 0
            except OSError:
                mtime = 0
            if mtime:
                st.first = min(st.first or mtime, mtime)
                st.last = max(st.last, mtime)
            if total >= max_files:
                return sorted(stats.values(), key=lambda s: s.rel), total
        for sub in repo_subdirs:
            st.files += sum(1 for _ in _count_repo(Path(dirpath) / sub))
    return sorted(stats.values(), key=lambda s: s.rel), total


def _count_repo(repo: Path):
    for dirpath, dirnames, filenames in os.walk(repo, followlinks=False):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        yield from filenames


def _roll_up(stats: list[FolderStat], level: int) -> list[FolderStat]:
    """Totals per folder at *level* (1 = top-level folders), including everything below them."""
    out: dict[str, FolderStat] = {}
    for st in stats:
        parts = [] if st.rel == "." else st.rel.split("/")
        if len(parts) < level:
            continue
        key = "/".join(parts[:level])
        agg = out.setdefault(key, FolderStat(rel=key))
        agg.files += st.files
        agg.git = agg.git or st.git
        for k, n in st.kinds.items():
            agg.kinds[k] = agg.kinds.get(k, 0) + n
        if st.first:
            agg.first = min(agg.first or st.first, st.first)
            agg.last = max(agg.last, st.last)
        if len(agg.samples) < 4:
            agg.samples += st.samples[: 4 - len(agg.samples)]
    return sorted(out.values(), key=lambda s: -s.files)


def _line(st: FolderStat, total: int, *, samples: bool) -> str:
    kinds = ", ".join(f"{k} {n:,}" for k, n in sorted(st.kinds.items(), key=lambda kv: -kv[1]))
    span = f"; modified {datetime.fromtimestamp(st.first):%Y}–{datetime.fromtimestamp(st.last):%Y}" if st.first else ""
    repo = "; git repositories inside" if st.git else ""
    share = f" ({100 * st.files / total:.0f}%)" if total else ""
    sample = f"; e.g. {', '.join(st.samples[:4])}" if samples and st.samples else ""
    return f"- {st.rel}: {st.files:,} files{share} [{kinds}]{span}{repo}{sample}"


def survey_text(stats: list[FolderStat], total: int, root_name: str) -> str:
    """Overview for the model: every top-level folder with totals first, then details.

    Totals come first so that no big part of the tree is lost when the
    detailed list has to be cut to fit the prompt.
    """
    lines = [f"Tree {root_name!r}: {total:,} files.", "", "TOP-LEVEL FOLDERS (with everything inside them):"]
    loose = next((st for st in stats if st.rel == "."), None)
    if loose and loose.files:
        lines.append(_line(FolderStat(rel="(files directly in the root)", files=loose.files, kinds=loose.kinds,
                                      first=loose.first, last=loose.last, samples=loose.samples), total, samples=True))
    lines += [_line(st, total, samples=True) for st in _roll_up(stats, 1)]
    lines += ["", "THEIR SUBFOLDERS:"]
    lines += [_line(st, total, samples=False) for st in _roll_up(stats, 2)[:60]]
    lines += ["", "DETAILS (biggest folders three levels down):"]
    for st in sorted((s for s in stats if s.rel.count("/") == 2 and s.files), key=lambda s: -s.files):
        if sum(len(line) + 1 for line in lines) > SURVEY_CHARS:
            lines.append("- … (more not shown)")
            break
        lines.append(_line(st, total, samples=True))
    return "\n".join(lines)


@dataclass
class PlannedID:
    id: str
    name: str
    description: str = ""


@dataclass
class PlannedCategory:
    id: str
    name: str
    ids: list[PlannedID] = field(default_factory=list)


@dataclass
class PlannedArea:
    id: str  # "10-19"
    name: str
    categories: list[PlannedCategory] = field(default_factory=list)


@dataclass
class Structure:
    areas: list[PlannedArea] = field(default_factory=list)

    @property
    def counts(self) -> tuple[int, int, int]:
        cats = [c for a in self.areas for c in a.categories]
        return len(self.areas), len(cats), sum(len(c.ids) for c in cats)

    def folders(self) -> list[str]:
        """Relative folder paths to create, parents first."""
        out = []
        for a in self.areas:
            area = f"{a.id} {a.name}"
            out.append(area)
            for c in a.categories:
                cat = f"{area}/{c.id} {c.name}"
                out.append(cat)
                out.extend(f"{cat}/{i.id} {i.name}" for i in c.ids)
        return out

    def render(self) -> str:
        lines = []
        for a in self.areas:
            lines.append(f"{a.id} {a.name}")
            for c in a.categories:
                lines.append(f"  {c.id} {c.name}")
                for i in c.ids:
                    desc = f"  — {i.description}" if i.description else ""
                    lines.append(f"    {i.id} {i.name}{desc}")
        return "\n".join(lines)

    def jdex(self) -> str:
        lines = [
            "# JDex",
            "",
            f"Created by sorto on {datetime.now():%Y-%m-%d}. Edit freely: sorto reads the descriptions.",
            "",
        ]
        for a in self.areas:
            lines += [f"## {a.id} {a.name}", ""]
            for c in a.categories:
                lines.append(f"### {c.id} {c.name}")
                lines += [f"- `{i.id}` {i.name}" + (f" — {i.description}" if i.description else "") for i in c.ids]
                lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def normalize(raw: dict[str, Any]) -> Structure:
    """Turn the model's proposal into a valid, gap-free Johnny.Decimal structure.

    Only the model's grouping and names are used. Numbers are assigned here:
    areas keep a valid ``X0-X9`` start the model gave (else the next free
    one), categories get numbers inside their area, IDs are ``.11, .12, …``.
    Empty areas and categories are dropped; names are cleaned to one safe
    folder name each.
    """
    structure = Structure()
    used_decades: set[int] = set()
    for area in (raw.get("areas") or [])[:MAX_AREAS]:
        if not isinstance(area, dict):
            continue
        name = clean_id_name(str(area.get("name") or ""))
        cats_raw = [c for c in (area.get("categories") or []) if isinstance(c, dict)][:MAX_CATEGORIES]
        if not name or not cats_raw:
            continue
        m = re.match(r"\s*(\d)\d", str(area.get("id") or ""))
        decade = int(m.group(1)) if m else None
        if decade is None or decade in used_decades:
            free = [d for d in range(1, 10) if d not in used_decades] or [0]
            decade = free[0]
        used_decades.add(decade)
        planned = PlannedArea(id=f"{decade}0-{decade}9", name=name)
        cat_numbers: set[int] = set()
        for cat in cats_raw:
            cname = clean_id_name(str(cat.get("name") or ""))
            ids_raw = [i for i in (cat.get("ids") or []) if isinstance(i, dict)][:MAX_IDS]
            names = [clean_id_name(str(i.get("name") or "")) for i in ids_raw]
            if not cname or not any(names):
                continue
            cm = re.match(r"\s*(\d{2})", str(cat.get("id") or ""))
            number = int(cm.group(1)) if cm else -1
            if number // 10 != decade or number % 10 == 0 or number in cat_numbers:
                number = next((decade * 10 + k for k in range(1, 10) if decade * 10 + k not in cat_numbers), -1)
            if number < 0:
                continue
            cat_numbers.add(number)
            pc = PlannedCategory(id=f"{number:02d}", name=cname)
            seen: set[str] = set()
            for item, iname in zip(ids_raw, names, strict=True):
                if not iname or iname.casefold() in seen:
                    continue
                seen.add(iname.casefold())
                desc = " ".join(str(item.get("description") or "").split())[:180]
                pc.ids.append(PlannedID(id=f"{number:02d}.{10 + len(pc.ids) + 1:02d}", name=iname, description=desc))
            planned.categories.append(pc)
        if planned.categories:
            structure.areas.append(planned)
    structure.areas.sort(key=lambda a: a.id)
    for a in structure.areas:
        a.categories.sort(key=lambda c: c.id)
    return structure


PROPOSAL_HEADER = """\
# sorto structure proposal. Edit it, then create it with:
#   sorto init TARGET --from THIS_FILE
# One line per area ("10-19 Name"), category ("  11 Name") and ID ("    11.11 Name — description").
# Rename, delete or add lines freely; sorto checks the numbering before creating anything.
# Lines starting with # are ignored.

"""


def parse_tree(text: str) -> Structure:
    """Read a (possibly edited) proposal. Numbers are kept, but must follow the rules.

    Raises ValueError naming the line when a category is outside its area,
    an ID outside its category, a number is used twice, or a line is not
    an area, category or ID.
    """
    structure = Structure()
    area: PlannedArea | None = None
    cat: PlannedCategory | None = None
    seen: set[str] = set()
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        head, _, desc = line.partition("—")
        head, desc = head.strip(), " ".join(desc.split())[:180]
        m_area = re.fullmatch(r"(\d)0-(\d)9\s+(.+)", head)
        m_id = re.fullmatch(r"(\d{2})\.(\d{2})\s+(.+)", head)
        m_cat = re.fullmatch(r"(\d{2})\s+(.+)", head)
        if m_area:
            if m_area.group(1) != m_area.group(2):
                raise ValueError(f"line {n}: an area spans ten numbers, like 10-19: {line!r}")
            area_id = f"{m_area.group(1)}0-{m_area.group(2)}9"
            if area_id in seen:
                raise ValueError(f"line {n}: area {area_id} is used twice")
            seen.add(area_id)
            area, cat = PlannedArea(id=area_id, name=clean_id_name(m_area.group(3))), None
            structure.areas.append(area)
        elif m_id:
            jd = f"{m_id.group(1)}.{m_id.group(2)}"
            if cat is None or m_id.group(1) != cat.id:
                raise ValueError(f"line {n}: ID {jd} is not under its category {m_id.group(1)}")
            if jd in seen:
                raise ValueError(f"line {n}: ID {jd} is used twice")
            seen.add(jd)
            cat.ids.append(PlannedID(id=jd, name=clean_id_name(m_id.group(3)), description=desc))
        elif m_cat:
            if area is None or m_cat.group(1)[0] != area.id[0]:
                raise ValueError(f"line {n}: category {m_cat.group(1)} is not inside area {area.id if area else '?'}")
            if m_cat.group(1) in seen:
                raise ValueError(f"line {n}: category {m_cat.group(1)} is used twice")
            seen.add(m_cat.group(1))
            cat = PlannedCategory(id=m_cat.group(1), name=clean_id_name(m_cat.group(2)))
            area.categories.append(cat)
        else:
            raise ValueError(f"line {n}: not an area, category or ID: {line!r}")
    for a in structure.areas:
        for c in a.categories:
            if not c.name or not a.name or any(not i.name for i in c.ids):
                raise ValueError(f"every area, category and ID needs a name (near {c.id})")
    return structure


def create(target: Path, structure: Structure) -> list[str]:
    """Create the folders (never replacing anything) and a JDex note. Returns what was created."""
    created = []
    for rel in structure.folders():
        path = target / rel
        if not path.exists():
            path.mkdir()
            created.append(rel)
    note = target / "JDex.md"
    if note.exists():
        note = target / "JDex (sorto).md"
    try:
        with open(note, "x", encoding="utf-8") as f:
            f.write(structure.jdex())
        created.append(note.name)
    except FileExistsError:
        pass
    return created


STRUCTURE_SYSTEM = """\
You design a Johnny.Decimal structure for someone's files. Johnny.Decimal has areas (a range of ten, \
e.g. "10-19 Life admin"), categories inside an area (e.g. "11 Money") and IDs inside a category \
(e.g. "11.11 Bills"). IDs are the folders files go into.

Design a SMALL structure that fits the files described: only what this content needs, typically \
3-8 areas, 1-6 categories per area and 1-10 IDs per category. Group by topic and use, not by file type \
alone, and let related topics share an area.
- Every top-level folder, and every folder with a large share of the files, must get a clear home. \
The biggest parts of the tree matter most.
- Folders by year or month (2019, 2021-05, …) of the same kind belong in ONE ID; the dated folders \
become subfolders inside it. Never make one ID per year or month.
- If the user's rules set things aside (for later review, for deletion, for another archive), give \
them IDs, but let such IDs share one area instead of one area each.
- Name everything in the language of the existing folder names, and give each ID a one-line \
description of what belongs in it. Numbers are only a suggestion; sorto renumbers everything.

Reply with ONE JSON object and nothing else:
{"areas": [{"id": "10-19", "name": "…", "categories": [{"id": "11", "name": "…",
  "ids": [{"id": "11.11", "name": "…", "description": "…"}]}]}]}
"""
