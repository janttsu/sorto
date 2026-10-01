"""User rules: free-form sorting instructions for the local model.

``$XDG_CONFIG_HOME/sorto/rules.md`` (default ``~/.config/sorto/rules.md``) is
plain text the user writes in any language, for example "all emails from
this sender go to 13.13" or "scanned receipts from 2023 belong in
13.13/2023". The rules are added to the model's instructions for every file
and take priority over its general judgement. They cannot widen what sorto
may do: the model still has to pick an ID that exists in the target, and
unknown IDs keep the file where it is.

Text inside ``<!-- -->`` comments is ignored, so the template can explain
itself without being sent to the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from sorto.util import user_config_path

MAX_RULES_CHARS = 8000
# Opening of the prompt section; llm.py looks for it to add the rules reminder.
SECTION_TITLE = "USER RULES (written by the user;"
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
RULE_ID_RE = re.compile(r"(?<![\d.])(\d{2}\.\d{2})(?!\d|\.\d)")

RULES_TEMPLATE = """\
<!--
sorto rules: your own sorting instructions for the local model.

Write them in plain language, in any language, one rule per line (a list
is easiest). Every rule is shown to the model for every file and takes
priority over its general judgement. Rules can be about anything the model
can see: file names, types, text inside documents, email senders and
subjects, photo dates, GPS positions, camera models, and so on.

A rule can give a topic an ID of its own ("everything about X goes under
its own ID"): if your target has no such ID yet, sorto creates it, with the
next free number. The model never chooses numbers or paths, and uncertain
files still stay where they are.
Everything inside these comment markers is ignored.

Examples (copy them below the comment and edit):

- Emails from billing@example.com go to 13.13.
- Anything that mentions my employer Example Ltd goes to 21.11.
- Screenshots always go to 51.12, never to 51.11.
- Photos taken between 2025-06-01 and 2025-06-14 belong to 14.12 (summer trip).
- Scanned receipts go to 13.13 into the year folder of the receipt date.
- Everything about Example Club goes under an ID of its own.
- Keep everything in 05.12 where it is (only matters when reorganizing).
-->
"""


@dataclass
class Rules:
    path: Path
    text: str = ""
    truncated: bool = False
    ids: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.text)

    @property
    def line_count(self) -> int:
        return sum(1 for line in self.text.splitlines() if line.strip())


def default_rules_path() -> Path:
    return user_config_path().parent / "rules.md"


def clean_rules(raw: str) -> tuple[str, bool]:
    """Drop comments and blank runs; cap the size so the prompt stays bounded."""
    text = COMMENT_RE.sub("", raw)
    lines = [line.rstrip() for line in text.splitlines()]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if len(text) > MAX_RULES_CHARS:
        return text[:MAX_RULES_CHARS].rsplit("\n", 1)[0], True
    return text, False


def load_rules(path: Path | None = None) -> Rules:
    path = Path(path) if path else default_rules_path()
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return Rules(path=path)
    text, truncated = clean_rules(raw)
    ids = sorted(set(RULE_ID_RE.findall(text)))
    return Rules(path=path, text=text, truncated=truncated, ids=ids)


def ensure_rules_file(path: Path | None = None) -> Path:
    """Create the commented template if there is no rules file yet."""
    path = Path(path) if path else default_rules_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(RULES_TEMPLATE, encoding="utf-8")
    return path


def rules_prompt_section(rules: Rules) -> str:
    if not rules:
        return ""
    return (
        f"{SECTION_TITLE} when a rule matches the file, follow it. Rules take "
        "priority over the general guidance above. A rule never lets you invent a number, a path "
        "or a subfolder. When a rule gives a topic an ID or a folder of its own and the outline has "
        'no ID for that topic yet, answer jd_id "new" with new_id_name: sorto creates the ID at '
        "once. Never put such a file into another ID as a temporary home):\n"
        f"{rules.text}\n"
    )


# ----------------------------------------------------------------- junk rules

JUNK_TEMPLATE = """\
<!--
sorto junk rules: file patterns that are never needed, anywhere. Every file
that matches goes straight into one folder of your target, without asking
the model. Nothing is deleted: the files are moved there and you can look
at them later.

First say where, with an ID that exists in your target:
  into: 99.11
Then one pattern per line, matched against the file name (case does not
matter; * and ? work as in the shell):
  *.dll
  *.pdb
  Thumbs.db
A pattern may name its own ID instead:
  *.log -> 00.13
A pattern with a / is matched against the whole path inside the source.
Everything inside these comment markers is ignored.
-->
"""

JUNK_ARROW_RE = re.compile(r"^(.*?)\s*(?:->|→)\s*(\d{2}\.\d{2})\s*$")


@dataclass
class JunkRule:
    pattern: str
    jd_id: str  # "" = the file's default "into:" ID


@dataclass
class JunkRules:
    path: Path
    into: str = ""
    rules: list[JunkRule] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.rules)

    @property
    def ids(self) -> list[str]:
        return sorted({r.jd_id or self.into for r in self.rules if r.jd_id or self.into})

    def match(self, src_rel: str) -> JunkRule | None:
        """The first rule that matches the file's name (or path, for patterns with a /)."""
        from fnmatch import fnmatchcase

        from sorto.util import glob_match

        name = src_rel.rsplit("/", 1)[-1].lower()
        for rule in self.rules:
            pat = rule.pattern.lower()
            hit = glob_match(src_rel.lower(), pat) if "/" in pat else fnmatchcase(name, pat)
            if hit:
                return rule
        return None


def default_junk_path() -> Path:
    return user_config_path().parent / "junk.md"


def load_junk_rules(path: Path | None = None) -> JunkRules:
    path = Path(path) if path else default_junk_path()
    out = JunkRules(path=path)
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    text, _ = clean_rules(raw)
    for n, line in enumerate(text.splitlines(), 1):
        line = re.sub(r"^(?:[-+]|\*)\s+", "", line.strip()).strip()  # a list bullet, not a glob
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^into\s*[:=]\s*(\d{2}\.\d{2})\s*$", line, re.I)
        if m:
            out.into = m.group(1)
            continue
        m = JUNK_ARROW_RE.match(line)
        pattern, jd_id = (m.group(1).strip(), m.group(2)) if m else (line, "")
        if not pattern or " " in pattern and "/" not in pattern and "*" not in pattern:
            out.problems.append(f"line {n}: not a file pattern: {line!r}")
            continue
        out.rules.append(JunkRule(pattern=pattern, jd_id=jd_id))
    if out.rules and not out.into and any(not r.jd_id for r in out.rules):
        out.problems.append("no 'into: NN.NN' line: patterns without their own ID have nowhere to go")
    return out


def ensure_junk_file(path: Path | None = None) -> Path:
    path = Path(path) if path else default_junk_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(JUNK_TEMPLATE, encoding="utf-8")
    return path
