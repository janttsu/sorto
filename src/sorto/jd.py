"""Discover an existing Johnny.Decimal tree under a target directory.

Nothing here is hardcoded to one person's layout: areas (``10-19 Name``),
categories (``11 Name``) and IDs (``11.12 Name``) are read from the target at
run time, together with any descriptions found in JDex notes
(``*JDex*.md`` lines such as ``- `11.12` Name — description``).

sorto files into IDs that already exist. It never invents an area. The only
new directories it may create are date subfolders (``2024`` / ``2024-05``)
inside an ID whose existing subfolders already follow that pattern, and,
when no existing ID fits, a new ID inside an existing category (see
``propose_new_id``) or, when no category fits either, a new category in an
existing area (see ``propose_new_category``). Their numbers are never the
model's: always the next free one.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from sorto.util import is_git_repo

AREA_RE = re.compile(r"^(\d{2})-(\d{2})\s+(.+)$")
CATEGORY_RE = re.compile(r"^(\d{2})\s+(.+)$")
ID_RE = re.compile(r"^(\d{2})\.(\d{2})\s+(.+)$")
JDEX_LINE_RE = re.compile(r"^\s*(?:[-*+]\s*)?`?(\d{2}\.\d{2})`?\s+(.+?)\s*$")
YEAR_RE = re.compile(r"^(19|20)\d{2}$")
YEAR_MONTH_RE = re.compile(r"^(19|20)\d{2}-(0[1-9]|1[0-2])$")

MAX_SUBFOLDERS = 40
MAX_JDEX_BYTES = 1024 * 1024


@dataclass
class JDItem:
    """One Johnny.Decimal ID folder, e.g. ``13.13 Invoices and receipts``."""

    id: str
    name: str
    rel: str
    area: str = ""
    category: str = ""
    description: str = ""
    subfolders: list[str] = field(default_factory=list)

    @property
    def is_index(self) -> bool:
        """``CC.00`` holds the category's own notes; never file into it."""
        return self.id.endswith(".00")

    @property
    def is_inbox(self) -> bool:
        return self.id.endswith(".01")


@dataclass
class Location:
    """Where a file sits in the tree: its ID (if any) and the folders below it."""

    item: JDItem | None
    sub: str = ""  # folders between the ID folder and the file ("" = directly in it)
    depth: int = 0  # number of those folders

    def describe(self) -> str:
        if self.item is None:
            return "not inside any Johnny.Decimal ID"
        where = f"{self.item.id} {self.item.name}"
        return f"{where} / {self.sub}" if self.sub else where


@dataclass
class JDIndex:
    root: Path
    items: dict[str, JDItem] = field(default_factory=dict)
    areas: list[str] = field(default_factory=list)
    categories: dict[str, str] = field(default_factory=dict)
    category_rels: dict[str, str] = field(default_factory=dict)  # "13" → "10-19 Life/13 Money"
    category_areas: dict[str, str] = field(default_factory=dict)
    _by_rel: dict[str, JDItem] = field(default_factory=dict, repr=False)

    def __len__(self) -> int:
        return len(self.choices())

    def choices(self) -> list[JDItem]:
        return [it for it in self.items.values() if not it.is_index]

    def get(self, jd_id: str) -> JDItem | None:
        item = self.items.get(normalize_id(jd_id))
        if item is None or item.is_index:
            return None
        return item

    def inbox_for(self, jd_id: str) -> JDItem | None:
        """The ``CC.01`` inbox of the category that *jd_id* belongs to."""
        norm = normalize_id(jd_id)
        if not norm:
            return None
        return self.items.get(f"{norm[:2]}.01")

    def category_outline(self) -> str:
        """One line per category with its area and IDs, for the new-ID category check."""
        lines = []
        for num, name in sorted(self.categories.items()):
            ids = [f"{i.id} {i.name}" for i in sorted(self.items.values(), key=lambda i: i.id)
                   if i.id[:2] == num and not i.is_index]
            area = self.category_areas.get(num) or "-"
            lines.append(f"{name}  (area {area}; IDs: {', '.join(ids) or 'none yet'})")
        return "\n".join(lines)

    def area_outline(self) -> str:
        """One line per area with its categories, for inventing a new category."""
        lines = []
        for area in self.areas:
            cats = [name for num, name in sorted(self.categories.items()) if self.category_areas.get(num) == area]
            free = free_category_number(self, area)
            room = "" if free is not None else "; FULL, no free category number"
            lines.append(f"{area}  (categories: {', '.join(cats) or 'none yet'}{room})")
        return "\n".join(lines)

    def locate(self, rel: str) -> Location:
        """The ID folder that contains *rel* (a file path relative to the root)."""
        if not self._by_rel or len(self._by_rel) != len(self.items):
            self._by_rel = {it.rel: it for it in self.items.values()}
        parts = [p for p in rel.replace("\\", "/").split("/")[:-1] if p]
        for i in range(len(parts), 0, -1):
            item = self._by_rel.get("/".join(parts[:i]))
            if item is not None:
                return Location(item, "/".join(parts[i:]), len(parts) - i)
        return Location(None, "/".join(parts), len(parts))

    def resolve_dir(self, item: JDItem, subfolder: str = "") -> tuple[str, str]:
        """Return (dir_rel, accepted_subfolder) for *item* under the target.

        A subfolder is accepted only if it already exists under the ID, or it
        is a new date folder and the ID's existing subfolders use that same
        date pattern. Anything else is dropped so sorto never invents
        structure the user has not already chosen.
        """
        sub = clean_subfolder(subfolder)
        if not sub:
            return item.rel, ""
        first = sub.split("/", 1)[0]
        if first in item.subfolders:
            if (self.root / item.rel / sub).is_dir():
                return f"{item.rel}/{sub}", sub
            return f"{item.rel}/{first}", first
        if _matches_date_convention(first, item.subfolders):
            return f"{item.rel}/{first}", first
        return item.rel, ""

    def render(self, *, max_chars: int = 60000) -> str:
        """Compact outline for the LLM system prompt."""
        lines: list[str] = []
        by_cat: dict[str, list[JDItem]] = {}
        loose: list[JDItem] = []
        for it in sorted(self.items.values(), key=lambda i: i.id):
            if it.is_index:
                continue
            if it.category:
                by_cat.setdefault(it.category, []).append(it)
            else:
                loose.append(it)
        for it in loose:
            lines.append(_render_item(it, indent=""))
        seen_area = ""
        for cat in sorted(by_cat, key=lambda c: c[:2]):
            items = by_cat[cat]
            area = items[0].area
            if area and area != seen_area:
                lines.append(area)
                seen_area = area
            lines.append(f"  {cat}")
            for it in items:
                lines.append(_render_item(it, indent="    "))
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[: max_chars - 40].rsplit("\n", 1)[0] + "\n… (outline truncated)"
        return text


FIRST_REGULAR_ID = 11  # .00–.10 are the category's own system IDs (JDex, inbox, …)
MAX_ID_NAME = 60


@dataclass
class NewCategory:
    """A category sorto may create: the next free number in an existing area."""

    number: str  # "94"
    name: str  # "Pets"
    area: str  # "90-99 Archive"

    @property
    def folder(self) -> str:
        return f"{self.number} {self.name}"

    @property
    def rel(self) -> str:
        return f"{self.area}/{self.folder}"


@dataclass
class NewID:
    """An ID sorto may create: the next free number in a category, existing or new."""

    item: JDItem
    category: str
    reused: bool = False  # an ID with the same name already existed
    new_category: NewCategory | None = None  # the category has to be created first

    def describe(self) -> str:
        text = f"{self.item.id} {self.item.name}"
        return f"{text} in the new category {self.new_category.folder}" if self.new_category else text


def clean_id_name(value: str) -> str:
    """A safe folder name for a new ID: one path segment, no leading number."""
    name = re.sub(r"^\s*\d{2}(?:\.\d{2})?\s*[-–—:]*\s*", "", value or "")
    name = re.sub(r"[\x00-\x1f/\\]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .-–—:")
    if sum(c.isalpha() for c in name) < 2:
        return ""  # "07" or "2234489_5825" is a number, not a topic
    return name[:MAX_ID_NAME].strip()


def used_id_numbers(root: Path, index: JDIndex, category: str) -> set[int]:
    """Every NN in ``category.NN`` that exists in the index or as a folder on disk."""
    used = {int(i[3:]) for i in index.items if i[:2] == category}
    rel = index.category_rels.get(category)
    if rel:
        try:
            with os.scandir(root / rel) as it:
                for entry in it:
                    m = ID_RE.match(entry.name)
                    if m and m.group(1) == category:
                        used.add(int(m.group(2)))
        except OSError:
            pass
    return used


def propose_new_id(index: JDIndex, category: str, name: str) -> NewID | str:
    """Plan a new ID in an existing *category*; a string explains why not.

    The model only names the category and the topic. The number is always
    the next free one after the category's highest regular ID (at least
    .11), checked against the folders on disk too, so the numbering stays
    continuous and unique. An existing ID with the same name is reused.
    """
    category = (category or "").strip()[:2]
    if not re.fullmatch(r"\d{2}", category) or category not in index.category_rels:
        return f"category {category or '?'} does not exist in the target"
    name = clean_id_name(name)
    if not name:
        return "the model gave no usable name for the new ID"
    for item in index.items.values():
        if item.id[:2] == category and item.name.casefold() == name.casefold():
            return NewID(item=item, category=category, reused=True)
    if name.casefold() == index.categories.get(category, "")[3:].strip().casefold():
        # "51.20 Pictures" inside "51 Pictures" is not a topic, it is the category again.
        return f"a new ID named {name!r} would only repeat the name of its category"
    used = used_id_numbers(index.root, index, category)
    regular = [n for n in used if n >= FIRST_REGULAR_ID]
    number = max(regular) + 1 if regular else FIRST_REGULAR_ID
    if number > 99:
        return f"category {category} has no free ID numbers left"
    jd_id = f"{category}.{number:02d}"
    item = JDItem(
        id=jd_id,
        name=name,
        rel=f"{index.category_rels[category]}/{jd_id} {name}",
        area=index.category_areas.get(category, ""),
        category=index.categories.get(category, ""),
    )
    return NewID(item=item, category=category)


def area_range(area: str) -> tuple[int, int] | None:
    m = AREA_RE.match(area or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def used_category_numbers(index: JDIndex, area: str) -> set[int]:
    """Every category number of *area* that is taken: in the index, by an ID, or as a folder on disk.

    An ID counts for its category wherever it lies: ``05.11 Shared folder`` in
    the root means category 01 is in use, although no ``01 …`` folder exists.
    """
    span = area_range(area)
    if span is None:
        return set()
    used = {int(num) for num in index.categories} | {int(i[:2]) for i in index.items}
    for folder in (index.root, index.root / area):
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    m = ID_RE.match(entry.name) or CATEGORY_RE.match(entry.name)
                    if m and not AREA_RE.match(entry.name):
                        used.add(int(m.group(1)))
        except OSError:
            pass
    return {n for n in used if span[0] <= n <= span[1]}


def free_category_number(index: JDIndex, area: str) -> int | None:
    """The next category number in *area*: one after its highest, never a gap.

    A gap may be a number the user retired, and Johnny.Decimal numbers are
    not reused. ``N0`` is the area's own management category, so an empty
    area starts at ``N1``.
    """
    span = area_range(area)
    if span is None:
        return None
    used = used_category_numbers(index, area)
    number = max(max(used) + 1 if used else 0, span[0] + 1)
    return number if number <= span[1] else None


def propose_new_category(index: JDIndex, area: str, category_name: str, id_name: str) -> NewID | str:
    """Plan a new category in an existing *area* with its first ID; a string explains why not.

    The model names the area, the category and the topic. Both numbers are
    sorto's: the category gets the next free number of the area, the ID is
    its first regular one (``.11``). A category of that name that already
    exists in the area is used instead of making a second one.
    """
    # "10-19", "10-19 Life", or a made-up range inside one area ("15-19"): the area that holds it.
    wanted = re.match(r"\s*(\d{2})-(\d{2})", area or "")
    spans = {a: area_range(a) for a in index.areas}
    found = next(
        (a for a, s in spans.items() if wanted and s and s[0] <= int(wanted.group(1)) <= int(wanted.group(2)) <= s[1]),
        None,
    )
    if found is None:
        return f"area {area or '?'} does not exist in the target"
    id_name = clean_id_name(id_name)
    category_name = clean_id_name(category_name)
    if not id_name or not category_name:
        return "the model gave no usable name for the new category"
    for num, folder in index.categories.items():
        if index.category_areas.get(num) == found and folder[3:].strip().casefold() == category_name.casefold():
            return propose_new_id(index, num, id_name)
    number = free_category_number(index, found)
    if number is None:
        return f"area {found} has no free category numbers left"
    category = NewCategory(number=f"{number:02d}", name=category_name, area=found)
    jd_id = f"{category.number}.{FIRST_REGULAR_ID:02d}"
    item = JDItem(
        id=jd_id, name=id_name, rel=f"{category.rel}/{jd_id} {id_name}", area=found, category=category.folder
    )
    return NewID(item=item, category=category.number, new_category=category)


def id_folder_position(parts: list[str]) -> int | None:
    """Index of the first ``NN.NN Name`` folder among area/category/ID levels."""
    for i, name in enumerate(parts[:3]):
        if ID_RE.match(name):
            return i
    return None


def normalize_id(value: str) -> str:
    m = re.search(r"(\d{2})\.(\d{2})", value or "")
    return f"{m.group(1)}.{m.group(2)}" if m else ""


def clean_subfolder(value: str) -> str:
    parts = [p.strip() for p in (value or "").replace("\\", "/").split("/")]
    parts = [p for p in parts if p and p not in (".", "..") and not p.startswith(".")]
    return "/".join(parts[:2])


MAX_FOLDER_NAME = 60


def clean_folder_name(value: str) -> str:
    """A safe name for a new folder inside an ID, or "" when the value is not usable as one.

    One path segment, with letters in it. Not a date folder (those are
    sorto's own) and not something that looks like another ID.
    """
    name = (value or "").replace("\\", "/").split("/", 1)[0]
    name = re.sub(r"[\x00-\x1f]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    if name.startswith("."):
        return ""  # hidden, or a path trick
    name = name.strip(" .")
    if sum(c.isalpha() for c in name) < 2:
        return ""
    if YEAR_RE.match(name) or YEAR_MONTH_RE.match(name) or ID_RE.match(name) or AREA_RE.match(name):
        return ""
    return name[:MAX_FOLDER_NAME].strip(" .")


def _folder_key(name: str) -> str:
    """"Cable clip", "cable-clip" and "Cable_Clip" are the same folder."""
    return re.sub(r"[\W_]+", "", name.casefold())


def existing_folder_like(id_dir: Path, name: str) -> str:
    """The folder in *id_dir* that already goes by *name* (spelling aside), or ""."""
    key = _folder_key(name)
    for entry in _dirs(id_dir):
        if _folder_key(entry.name) == key and not is_git_repo(Path(entry.path)):
            return entry.name
    return ""


def is_date_id(item: JDItem, date_ids: Iterable[str]) -> bool:
    """The user listed this ID as one whose photos and videos are all filed by date.

    An entry is an ID (``51.11``) or, so that it cannot hit another archive
    by accident, the ID with its name (``51.11 Photos``).
    """
    return any(str(e).strip() in (item.id, f"{item.id} {item.name}") for e in date_ids)


def _matches_date_convention(name: str, existing: list[str]) -> bool:
    for pattern in (YEAR_RE, YEAR_MONTH_RE):
        if pattern.match(name) and any(pattern.match(s) for s in existing):
            return True
    return False


def _render_item(it: JDItem, *, indent: str) -> str:
    line = f"{indent}{it.id} {it.name}"
    if it.description:
        line += f" — {it.description[:180]}"
    # Year and year-month folders are how files are stored, not what the ID is about. Which
    # years exist is left out: a model takes "has a 2023 folder" as proof that photos from
    # 2023 belong to a trip made in 2024.
    named = [s for s in it.subfolders if not (YEAR_RE.match(s) or YEAR_MONTH_RE.match(s))]
    notes = []
    if named:
        shown = named[:12]
        more = f", +{len(named) - len(shown)} more" if len(named) > len(shown) else ""
        notes.append(f"subfolders: {', '.join(shown)}{more}")
    if any(YEAR_RE.match(s) for s in it.subfolders):
        notes.append("has year folders (YYYY)")
    if any(YEAR_MONTH_RE.match(s) for s in it.subfolders):
        notes.append("has year-month folders (YYYY-MM)")
    if notes:
        line += f"  [{'; '.join(notes)}]"
    return line


def _dirs(path: Path) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(path) as it:
            entries = [e for e in it if not e.name.startswith(".")]
    except OSError:
        return []
    out = []
    for e in entries:
        try:
            if e.is_dir(follow_symlinks=False):
                out.append(e)
        except OSError:
            continue
    return sorted(out, key=lambda e: e.name.lower())


def _excluded(path: Path, exclude: list[Path]) -> bool:
    for ex in exclude:
        try:
            path.relative_to(ex)
            return True
        except ValueError:
            continue
    return False


def scan_jd(target: Path, *, exclude: list[Path] | None = None) -> JDIndex:
    """Walk only JD-named directories (plus one level of subfolders per ID)."""
    root = Path(target).expanduser().resolve()
    excl = [Path(p).resolve() for p in (exclude or [])]
    index = JDIndex(root=root)
    jdex_files: list[Path] = []

    def add_id(entry: os.DirEntry[str], area: str, category: str) -> None:
        m = ID_RE.match(entry.name)
        if not m:
            return
        path = Path(entry.path)
        if _excluded(path, excl):
            return
        jd_id = f"{m.group(1)}.{m.group(2)}"
        subs = []
        for sub in _dirs(path):
            if _excluded(Path(sub.path), excl):
                continue
            if "jdex" in sub.name.lower():
                continue
            if is_git_repo(Path(sub.path)):
                continue  # a repository is not a place to file into; the model does not see it
            subs.append(sub.name)
        item = JDItem(
            id=jd_id,
            name=m.group(3).strip(),
            rel=path.relative_to(root).as_posix(),
            area=area,
            category=category,
            subfolders=subs[:MAX_SUBFOLDERS],
        )
        if jd_id in index.items:
            # Duplicate IDs in a tree: keep the first, but do not lose it silently.
            return
        index.items[jd_id] = item
        if item.is_index:
            jdex_files.extend(_md_files(path))

    def walk_category(entry: os.DirEntry[str], area: str) -> None:
        m = CATEGORY_RE.match(entry.name)
        if not m or _excluded(Path(entry.path), excl):
            return
        index.categories[m.group(1)] = entry.name
        index.category_rels[m.group(1)] = Path(entry.path).relative_to(root).as_posix()
        index.category_areas[m.group(1)] = area
        for child in _dirs(Path(entry.path)):
            add_id(child, area, entry.name)

    for top in _dirs(root):
        if _excluded(Path(top.path), excl):
            continue
        if AREA_RE.match(top.name):
            index.areas.append(top.name)
            for child in _dirs(Path(top.path)):
                if CATEGORY_RE.match(child.name) and not ID_RE.match(child.name):
                    walk_category(child, top.name)
                else:
                    add_id(child, top.name, "")
        elif ID_RE.match(top.name):
            add_id(top, "", "")
        elif CATEGORY_RE.match(top.name):
            walk_category(top, "")

    jdex_files.extend(p for p in _md_files(root) if "jdex" in p.name.lower())
    _apply_jdex_descriptions(index, jdex_files)
    return index


def _md_files(path: Path) -> list[Path]:
    try:
        with os.scandir(path) as it:
            return sorted(
                Path(e.path)
                for e in it
                if e.name.lower().endswith(".md") and e.is_file(follow_symlinks=False)
            )
    except OSError:
        return []


def _apply_jdex_descriptions(index: JDIndex, files: list[Path]) -> None:
    for path in files:
        try:
            if path.stat().st_size > MAX_JDEX_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            m = JDEX_LINE_RE.match(line)
            if not m:
                continue
            item = index.items.get(m.group(1))
            if item is None or item.description:
                continue
            desc = _description_from(m.group(2), item.name)
            if desc:
                item.description = desc


def _description_from(rest: str, name: str) -> str:
    """``Invoices — All bills`` → ``All bills``."""
    rest = rest.strip()
    for sep in (" — ", " – ", " - ", ": "):
        if sep in rest:
            head, tail = rest.split(sep, 1)
            if head.strip().lower() == name.lower() or len(head) <= len(name) + 2:
                return tail.strip()
    if rest.lower() == name.lower():
        return ""
    return rest if not rest.lower().startswith(name.lower()) else rest[len(name) :].strip(" —–-:")
