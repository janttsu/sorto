from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from sorto.models import Counts
from sorto.util import utc_now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    src_rel TEXT UNIQUE,
    abs_path TEXT,
    size INTEGER,
    mtime_ns INTEGER,
    sha256 TEXT,
    status TEXT,
    type_guess TEXT,
    mime TEXT,
    llm_label TEXT,
    llm_confidence REAL,
    llm_reason TEXT,
    dest_rel TEXT,
    error TEXT,
    discovered_at TEXT,
    updated_at TEXT,
    analyzed_at TEXT,
    finished_at TEXT,
    dev INTEGER,
    ino INTEGER,
    rename INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts TEXT,
    file_id INTEGER,
    kind TEXT,
    message TEXT
);

CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key TEXT PRIMARY KEY,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_sha ON files(sha256);
CREATE INDEX IF NOT EXISTS idx_files_ino ON files(dev, ino);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        # Python 3.12+ can keep legacy transaction control even with isolation_level=None.
        if hasattr(self.conn, "autocommit"):
            try:
                self.conn.autocommit = True
            except (AttributeError, TypeError, ValueError):
                pass
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=FULL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(files)")}
        extras = {
            "dev": "INTEGER",
            "ino": "INTEGER",
            "rename": "INTEGER DEFAULT 0",
        }
        for name, typ in extras.items():
            if name not in cols:
                self.conn.execute(f"ALTER TABLE files ADD COLUMN {name} {typ}")

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, tuple(params))
            verb = sql.lstrip().split(" ", 1)[0].upper()
            if verb in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
                try:
                    self.conn.commit()
                except sqlite3.Error:
                    pass
            return cur

    def get(self, file_id: int) -> sqlite3.Row | None:
        cur = self.execute("SELECT * FROM files WHERE id = ?", (file_id,))
        return cur.fetchone()

    def get_by_rel(self, src_rel: str) -> sqlite3.Row | None:
        cur = self.execute("SELECT * FROM files WHERE src_rel = ?", (src_rel,))
        return cur.fetchone()

    def get_by_inode(self, dev: int | None, ino: int | None) -> sqlite3.Row | None:
        if dev is None or ino is None:
            return None
        cur = self.execute(
            "SELECT * FROM files WHERE dev = ? AND ino = ? ORDER BY id DESC LIMIT 1",
            (dev, ino),
        )
        return cur.fetchone()

    def get_by_sha(self, sha256: str | None) -> sqlite3.Row | None:
        if not sha256:
            return None
        cur = self.execute(
            "SELECT * FROM files WHERE sha256 = ? AND status = 'done' ORDER BY id LIMIT 1",
            (sha256,),
        )
        return cur.fetchone()

    def upsert_discovered(
        self,
        *,
        src_rel: str,
        abs_path: str,
        size: int,
        mtime_ns: int,
        dev: int | None,
        ino: int | None,
    ) -> tuple[int, bool]:
        """Insert or refresh a discovered file.

        Returns (file_id, is_new_work) where is_new_work means the file
        needs investigation (new or changed).
        """
        now = utc_now_iso()
        with self._lock:
            row = self.conn.execute("SELECT * FROM files WHERE src_rel = ?", (src_rel,)).fetchone()
            if row is None and dev is not None and ino is not None:
                row = self.conn.execute(
                    "SELECT * FROM files WHERE dev = ? AND ino = ? ORDER BY id DESC LIMIT 1",
                    (dev, ino),
                ).fetchone()
                if row is not None and row["src_rel"] != src_rel:
                    # Same inode at a new path (moved by us or the user).
                    if row["status"] == "done" and row["size"] == size and row["mtime_ns"] == mtime_ns:
                        self.conn.execute(
                            "UPDATE files SET src_rel=?, abs_path=?, updated_at=? WHERE id=?",
                            (src_rel, abs_path, now, row["id"]),
                        )
                        return int(row["id"]), False
            if row is None:
                cur = self.conn.execute(
                    """
                    INSERT INTO files (
                        src_rel, abs_path, size, mtime_ns, status,
                        discovered_at, updated_at, dev, ino
                    ) VALUES (?, ?, ?, ?, 'discovered', ?, ?, ?, ?)
                    """,
                    (src_rel, abs_path, size, mtime_ns, now, now, dev, ino),
                )
                return int(cur.lastrowid), True
            file_id = int(row["id"])
            unchanged = row["size"] == size and row["mtime_ns"] == mtime_ns
            if row["status"] == "done" and unchanged:
                if row["src_rel"] != src_rel or row["abs_path"] != abs_path:
                    self.conn.execute(
                        "UPDATE files SET src_rel=?, abs_path=?, updated_at=? WHERE id=?",
                        (src_rel, abs_path, now, file_id),
                    )
                return file_id, False
            if row["status"] in ("skipped", "needs_user") and unchanged:
                return file_id, False
            if row["status"] == "error" and unchanged:
                return file_id, False
            if row["status"] in ("identifying", "analyzing", "planned", "moving") and unchanged:
                return file_id, False
            if row["status"] == "discovered" and unchanged:
                return file_id, False
            # Changed or still needs work
            needs = row["status"] not in ("identifying", "analyzing", "planned", "moving")
            new_status = "discovered" if needs or not unchanged else row["status"]
            self.conn.execute(
                """
                UPDATE files SET
                    src_rel=?, abs_path=?, size=?, mtime_ns=?, dev=?, ino=?,
                    status=?, error=NULL, updated_at=?,
                    llm_label=CASE WHEN ? THEN NULL ELSE llm_label END,
                    dest_rel=CASE WHEN ? THEN NULL ELSE dest_rel END,
                    finished_at=CASE WHEN ? THEN NULL ELSE finished_at END
                WHERE id=?
                """,
                (
                    src_rel,
                    abs_path,
                    size,
                    mtime_ns,
                    dev,
                    ino,
                    new_status,
                    now,
                    not unchanged,
                    not unchanged,
                    not unchanged,
                    file_id,
                ),
            )
            return file_id, new_status == "discovered" and (not unchanged or row["status"] != "discovered")

    _FILE_COLS = frozenset(
        {
            "src_rel",
            "abs_path",
            "size",
            "mtime_ns",
            "sha256",
            "status",
            "type_guess",
            "mime",
            "llm_label",
            "llm_confidence",
            "llm_reason",
            "dest_rel",
            "error",
            "discovered_at",
            "updated_at",
            "analyzed_at",
            "finished_at",
            "dev",
            "ino",
            "rename",
        }
    )

    def update(self, file_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields = dict(fields)
        fields["updated_at"] = utc_now_iso()
        unknown = set(fields) - self._FILE_COLS
        if unknown:
            raise ValueError(f"unknown files columns: {unknown}")
        cols = ", ".join(f'"{k}"=?' for k in fields)
        vals = list(fields.values()) + [file_id]
        self.execute(f"UPDATE files SET {cols} WHERE id=?", vals)

    def counts(self) -> Counts:
        cur = self.execute("SELECT status, COUNT(*) AS n FROM files GROUP BY status")
        raw = {str(r["status"]): int(r["n"]) for r in cur.fetchall()}
        c = Counts()
        c.from_status_map(raw)
        c.pending = c.discovered + c.identifying + c.analyzing + c.planned + c.moving
        return c

    def ids_by_status(self, *statuses: str) -> list[int]:
        if not statuses:
            return []
        q = ",".join("?" * len(statuses))
        cur = self.execute(f"SELECT id FROM files WHERE status IN ({q}) ORDER BY id", statuses)
        return [int(r["id"]) for r in cur.fetchall()]

    def recent(self, limit: int = 20) -> list[sqlite3.Row]:
        cur = self.execute(
            "SELECT status, src_rel, dest_rel, llm_label FROM files ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        return list(cur.fetchall())

    def recent_events(self, limit: int = 50) -> list[sqlite3.Row]:
        cur = self.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        return list(cur.fetchall())

    def add_event(self, kind: str, message: str, file_id: int | None = None) -> None:
        self.execute(
            "INSERT INTO events (ts, file_id, kind, message) VALUES (?, ?, ?, ?)",
            (utc_now_iso(), file_id, kind, message),
        )

    def cache_get(self, key: str) -> str | None:
        cur = self.execute("SELECT response_json FROM llm_cache WHERE cache_key=?", (key,))
        row = cur.fetchone()
        return str(row["response_json"]) if row else None

    def cache_put(self, key: str, response_json: str) -> None:
        self.execute(
            "INSERT OR REPLACE INTO llm_cache (cache_key, response_json, created_at) VALUES (?, ?, ?)",
            (key, response_json, utc_now_iso()),
        )

    def top_level_folders(self, root: Path, limit: int = 80) -> list[str]:
        found: list[str] = []
        try:
            for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
                if not child.is_dir() or child.is_symlink():
                    continue
                if child.name == "_organization":
                    continue
                found.append(child.name)
                if len(found) >= limit:
                    break
                try:
                    for sub in sorted(child.iterdir(), key=lambda p: p.name.lower()):
                        if sub.is_dir() and not sub.is_symlink() and sub.name != "_organization":
                            found.append(f"{child.name}/{sub.name}")
                            if len(found) >= limit:
                                return found
                except OSError:
                    continue
        except OSError:
            pass
        return found
