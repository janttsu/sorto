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
    rename INTEGER DEFAULT 0,
    duplicate_of TEXT
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

CREATE TABLE IF NOT EXISTS folders (
    dir TEXT PRIMARY KEY,
    decision TEXT NOT NULL,
    files INTEGER,
    jd_id TEXT,
    dest_dir TEXT,
    summary TEXT,
    confidence REAL,
    reason TEXT,
    profile TEXT,
    decided_at TEXT,
    own_media INTEGER,
    intact INTEGER
);

CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_sha ON files(sha256);
CREATE INDEX IF NOT EXISTS idx_files_ino ON files(dev, ino);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


class _Result:
    """Rows already fetched under the database lock (cursor-like API)."""

    def __init__(self, rows: list[sqlite3.Row], lastrowid: int | None):
        self._rows = rows
        self.lastrowid = lastrowid

    def fetchone(self) -> sqlite3.Row | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[sqlite3.Row]:
        return list(self._rows)


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            # The user deleted the index to start over: SQLite would otherwise
            # replay a leftover write-ahead log into the new, empty file.
            for suffix in ("-wal", "-shm", "-journal"):
                try:
                    self.path.with_name(self.path.name + suffix).unlink()
                except OSError:
                    pass
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
            "duplicate_of": "TEXT",
            "orig_rel": "TEXT",
            "summary": "TEXT",
            "jd_id": "TEXT",
        }
        for name, typ in extras.items():
            if name not in cols:
                self.conn.execute(f"ALTER TABLE files ADD COLUMN {name} {typ}")
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(folders)")}
        for name in ("own_media", "intact"):
            if name not in cols:
                self.conn.execute(f"ALTER TABLE folders ADD COLUMN {name} INTEGER")
        # sorto <= 0.1.0b1 counted files that had left the source as errors.
        self.conn.execute("UPDATE files SET status='gone', error=NULL WHERE status='error' AND error='source missing'")

    def checkpoint(self) -> None:
        """Fold the write-ahead log into index.sqlite, so that file alone is the index."""
        with self._lock:
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass

    def close(self) -> None:
        with self._lock:
            self.checkpoint()
            self.conn.close()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> _Result:
        """Run one statement and fetch its rows while holding the lock.

        The connection is shared by several threads; fetching from a cursor
        after releasing the lock lets another thread's statement reset it.
        """
        with self._lock:
            cur = self.conn.execute(sql, tuple(params))
            verb = sql.lstrip().split(" ", 1)[0].upper()
            if verb in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
                try:
                    self.conn.commit()
                except sqlite3.Error:
                    pass
                return _Result([], cur.lastrowid)
            return _Result(cur.fetchall(), cur.lastrowid)

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
            # "gone" falls through: a file that is back in the source is new work.
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
            "duplicate_of",
            "orig_rel",
            "summary",
            "jd_id",
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

    def pending_rels(self, *, include_planned: bool = True) -> list[str]:
        """Source paths of files still waiting to be sorted (for the ETA)."""
        statuses = ["discovered", "identifying", "analyzing", "moving"] + (["planned"] if include_planned else [])
        q = ",".join("?" * len(statuses))
        cur = self.execute(f"SELECT src_rel FROM files WHERE status IN ({q}) AND src_rel IS NOT NULL", statuses)
        return [str(r["src_rel"]) for r in cur.fetchall()]

    def recent(self, limit: int = 20) -> list[sqlite3.Row]:
        cur = self.execute(
            "SELECT status, COALESCE(src_rel, orig_rel) AS src_rel, dest_rel, llm_label "
            "FROM files ORDER BY updated_at DESC LIMIT ?",
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

    def folder_get(self, rel: str) -> sqlite3.Row | None:
        """A remembered whole-folder decision (unit / mixed / small / big)."""
        return self.execute("SELECT * FROM folders WHERE dir = ?", (rel,)).fetchone()

    def folder_put(self, rel: str, decision: str, **fields: Any) -> None:
        cols = ["dir", "decision", "decided_at", *fields]
        vals = [rel, decision, utc_now_iso(), *fields.values()]
        self.execute(
            f"INSERT OR REPLACE INTO folders ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals
        )

    def clear_caches(self) -> tuple[int, int, int]:
        """Forget cached model answers and folder decisions: (answers, folder decisions, folders kept).

        The record of the files themselves (moved, kept) is not touched. A
        folder that moves as a whole and is already partly moved keeps its
        decision: judged again, the rest of it could end up somewhere else.
        """
        started = (
            "decision = 'unit' AND EXISTS (SELECT 1 FROM files WHERE status = 'done' AND orig_rel IS NOT NULL "
            "AND substr(orig_rel, 1, length(folders.dir) + 1) = folders.dir || '/')"
        )
        with self._lock:
            answers = self.conn.execute("DELETE FROM llm_cache").rowcount
            folders = self.conn.execute(f"DELETE FROM folders WHERE NOT ({started})").rowcount
            kept = self.conn.execute("SELECT count(*) FROM folders").fetchone()[0]
            self.conn.commit()
        return int(answers), int(folders), int(kept)

    def cache_get(self, key: str) -> str | None:
        cur = self.execute("SELECT response_json FROM llm_cache WHERE cache_key=?", (key,))
        row = cur.fetchone()
        return str(row["response_json"]) if row else None

    def cache_put(self, key: str, response_json: str) -> None:
        self.execute(
            "INSERT OR REPLACE INTO llm_cache (cache_key, response_json, created_at) VALUES (?, ?, ?)",
            (key, response_json, utc_now_iso()),
        )
