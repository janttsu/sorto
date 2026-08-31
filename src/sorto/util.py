from __future__ import annotations

import ctypes
import ctypes.util
import errno
import hashlib
import json
import os
import re
import shutil
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sorto import STATE_DIR_NAME

AT_FDCWD = -100
RENAME_NOREPLACE = 1

MEANINGLESS_NAME_RE = re.compile(
    r"""(?ix)
    ^(
        img[-_]?\d+
        | dsc[-_]?\d+
        | dcim.*
        | untitled(?:\s*\(\d+\))?
        | new\s*document(?:\s*\(\d+\))?
        | document\s*\(\d+\)
        | download(?:s)?(?:\s*\(\d+\))?
        | scan[-_]?\d+
        | screenshot(?:[-_\s]\d+)*
        | screen[-_]?shot.*
        | file[-_]?\d+
        | copy(?:\s+of)?(?:\s+.*)?
        | image[-_]?\d+
        | photo[-_]?\d+
        | p\d{6,8}
        | \d{8}[-_]\d{6}
    )$
    """,
)

UNSAFE_DEST_RE = re.compile(r"[\x00-\x1f]")
RESERVED_DIRS = frozenset(
    {"_unsorted", "_duplicates_candidates", "_organization", "_cache_temp_and_junk"}
)


class UnsafePathError(ValueError):
    """Destination or source path violates safety rules."""


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def git_workdir(path: Path) -> Path | None:
    """Return the git working tree that contains *path*, or None.

    Walks from the file's directory up to the filesystem root looking for
    a `.git` directory or file (including linked worktrees). Used to refuse
    duplicate-deletion inside any git repository.
    """
    try:
        cur = path.resolve()
    except OSError:
        return None
    if cur.is_file() or not cur.is_dir():
        cur = cur.parent
    while True:
        try:
            if (cur / ".git").exists():
                return cur
        except OSError:
            return None
        parent = cur.parent
        if parent == cur:
            return None
        cur = parent


DELETE_DUPLICATE_MARK = "__delete_duplicate__"
DELETE_JUNK_MARK = "__delete_junk__"


def state_dir(root: Path) -> Path:
    return Path(root) / STATE_DIR_NAME


def user_config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "sorto" / "config.toml"
    return Path.home() / ".config" / "sorto" / "config.toml"


def human_size(n: int) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(x) < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(x)} {unit}"
            return f"{x:.1f} {unit}"
        x /= 1024.0
    return f"{n} B"


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 0:
        seconds = 0
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def posix_rel(path: str) -> str:
    return path.replace("\\", "/").lstrip("/")


def is_meaningless_name(filename: str) -> bool:
    stem = Path(filename).stem.strip()
    if not stem:
        return True
    return bool(MEANINGLESS_NAME_RE.match(stem))


def _glob_match_parts(path_parts: list[str], pat_parts: list[str]) -> bool:
    """Match path parts against glob parts, including `**`."""
    i = j = 0
    while i < len(path_parts) and j < len(pat_parts):
        if pat_parts[j] == "**":
            if j == len(pat_parts) - 1:
                return True
            for k in range(i, len(path_parts) + 1):
                if _glob_match_parts(path_parts[k:], pat_parts[j + 1 :]):
                    return True
            return False
        if not _seg_match(path_parts[i], pat_parts[j]):
            return False
        i += 1
        j += 1
    while j < len(pat_parts) and pat_parts[j] == "**":
        j += 1
    return i == len(path_parts) and j == len(pat_parts)


def _seg_match(seg: str, pat: str) -> bool:
    import fnmatch

    return fnmatch.fnmatch(seg, pat)


def glob_match(rel: str, pattern: str) -> bool:
    rel_n = posix_rel(rel)
    pat = posix_rel(pattern)
    if not pat:
        return False
    path_parts = rel_n.split("/") if rel_n else []
    pat_parts = pat.split("/")
    if _glob_match_parts(path_parts, pat_parts):
        return True
    # `*.pdf` should match nested files unless the pattern is rooted
    if "/" not in pat.rstrip("/"):
        name = path_parts[-1] if path_parts else rel_n
        if _seg_match(name, pat):
            return True
        return _glob_match_parts(path_parts, ["**", pat])
    if not pat.startswith("**"):
        return _glob_match_parts(path_parts, ["**"] + pat_parts)
    return False


def should_include(rel: str, include: list[str], exclude: list[str]) -> bool:
    rel_n = posix_rel(rel)
    parts = rel_n.split("/")
    if STATE_DIR_NAME in parts:
        return False
    for pat in exclude:
        if glob_match(rel_n, pat):
            return False
    if not include:
        return True
    return any(glob_match(rel_n, pat) for pat in include)


def resolve_under_root(root: Path, path: Path) -> Path:
    root_r = root.resolve()
    path_r = path.resolve()
    try:
        path_r.relative_to(root_r)
    except ValueError as exc:
        raise UnsafePathError(f"path escapes root: {path}") from exc
    return path_r


def is_under_root(root: Path, path: Path) -> bool:
    try:
        resolve_under_root(root, path)
        return True
    except (UnsafePathError, OSError):
        return False


def sanitize_dir_component(name: str) -> str:
    if name in RESERVED_DIRS:
        return name
    name = unicodedata.normalize("NFKD", name)
    name = name.encode("ascii", "ignore").decode("ascii")
    name = name.strip().lower()
    name = re.sub(r"[\s_]+", "-", name)
    name = re.sub(r"[^a-z0-9.-]", "", name)
    name = re.sub(r"-{2,}", "-", name).strip(".-")
    return name[:60]


def validate_dest_rel(
    dest_rel: str,
    *,
    original_ext: str | None = None,
    preserve_names: bool = False,
) -> str:
    """Return a cleaned relative dest path, or raise UnsafePathError."""
    if dest_rel is None:
        raise UnsafePathError("dest_rel is missing")
    dest = dest_rel.strip().replace("\\", "/")
    if not dest:
        raise UnsafePathError("dest_rel is empty")
    if dest.startswith("/") or dest.startswith("~"):
        raise UnsafePathError("dest_rel must be relative")
    if UNSAFE_DEST_RE.search(dest):
        raise UnsafePathError("dest_rel contains control characters")
    if ":" in dest and os.name == "nt":
        raise UnsafePathError("dest_rel must be relative")
    parts = [p for p in dest.split("/") if p not in ("", ".")]
    if not parts:
        raise UnsafePathError("dest_rel has no filename")
    if any(p == ".." for p in parts):
        raise UnsafePathError("dest_rel contains ..")
    if STATE_DIR_NAME in parts:
        raise UnsafePathError("dest_rel must not include _organization")
    filename = parts[-1]
    if filename in (".", "..") or "/" in filename:
        raise UnsafePathError("dest_rel missing filename")
    if preserve_names:
        dirs = [p for p in parts[:-1] if p]
    else:
        dirs = [sanitize_dir_component(p) for p in parts[:-1]]
        dirs = [d for d in dirs if d]
    cleaned = "/".join(dirs + [filename]) if dirs else filename
    if original_ext:
        ext = original_ext if original_ext.startswith(".") else f".{original_ext}"
        p = Path(cleaned)
        if p.suffix.lower() != ext.lower():
            cleaned = str(p.with_suffix(ext))
    return posix_rel(cleaned)


def unique_dest(root: Path, dest_rel: str, src_path: Path | None = None) -> Path:
    dest = root / posix_rel(dest_rel)
    if src_path is not None:
        try:
            src_r = Path(src_path).resolve()
            if dest.exists() and dest.resolve() == src_r:
                return dest
            if dest.exists():
                ds, ss = dest.stat(), src_r.stat()
                if ds.st_ino == ss.st_ino and ds.st_dev == ss.st_dev:
                    return dest
        except OSError:
            pass
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    parent = dest.parent
    for n in range(2, 1000):
        cand = parent / f"{stem}-{n}{suffix}"
        if not cand.exists():
            return cand
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    cand = parent / f"{stem}-{ts}{suffix}"
    if not cand.exists():
        return cand
    h = hashlib.sha256(os.fsencode(str(src_path or dest))).hexdigest()[:8]
    cand = parent / f"{stem}-{h}{suffix}"
    if not cand.exists():
        return cand
    return parent / f"{stem}-{uuid4().hex[:8]}{suffix}"


def _renameat2_noreplace(src: str, dest: str) -> None:
    libname = ctypes.util.find_library("c")
    if not libname:
        raise OSError(errno.ENOSYS, "libc not found")
    libc = ctypes.CDLL(libname, use_errno=True)
    if not hasattr(libc, "renameat2"):
        raise OSError(errno.ENOSYS, "renameat2 unavailable")
    libc.renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    libc.renameat2.restype = ctypes.c_int
    rc = libc.renameat2(
        AT_FDCWD,
        os.fsencode(src),
        AT_FDCWD,
        os.fsencode(dest),
        RENAME_NOREPLACE,
    )
    if rc == 0:
        return
    err = ctypes.get_errno()
    raise OSError(err, os.strerror(err), src, None, dest)


def _copy_exclusive(src: Path, dest: Path) -> Path:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(dest, flags, 0o644)
    try:
        with open(src, "rb") as inf:
            while True:
                chunk = inf.read(1024 * 1024)
                if not chunk:
                    break
                os.write(fd, chunk)
        os.fsync(fd)
    except Exception:
        os.close(fd)
        try:
            os.unlink(dest)
        except OSError:
            pass
        raise
    os.close(fd)
    try:
        shutil.copystat(src, dest, follow_symlinks=True)
    except OSError:
        pass
    os.unlink(src)
    return dest


def exclusive_move(src: Path, dest: Path) -> Path:
    """Move src to dest without ever overwriting an existing dest.

    Prefers Linux renameat2(RENAME_NOREPLACE), then hardlink+unlink,
    then copy-with-O_EXCL. Never uses os.replace on an unproven dest.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise FileExistsError(errno.EEXIST, "destination exists", str(dest))
    try:
        _renameat2_noreplace(str(src), str(dest))
        return dest
    except OSError as e:
        if e.errno == errno.EEXIST:
            raise FileExistsError(e.errno, "destination exists", str(dest)) from e
        if e.errno == errno.EXDEV:
            return _copy_exclusive(src, dest)
    try:
        os.link(src, dest)
    except FileExistsError:
        raise
    except OSError as e:
        if e.errno in (errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EACCES, errno.ENOSYS):
            return _copy_exclusive(src, dest)
        raise
    try:
        os.unlink(src)
    except OSError:
        try:
            os.unlink(dest)
        except OSError:
            pass
        raise
    return dest


def safe_move(src: Path, dest: Path, root: Path, *, max_tries: int = 50) -> Path:
    """Move src under root to a unique dest. Never overwrite, never leave root."""
    src_r = resolve_under_root(root, src)
    if not src_r.is_file() or src_r.is_symlink():
        raise UnsafePathError(f"refusing to move non-regular file: {src}")
    try:
        if dest.exists() and dest.resolve() == src_r:
            return src_r
    except OSError:
        pass
    chosen = dest
    last_err: OSError | None = None
    for _ in range(max_tries):
        chosen_rel = posix_rel(str(Path(os.path.relpath(chosen, start=root))))
        validate_dest_rel(chosen_rel)
        dest_r = (root / chosen_rel)
        try:
            dest_parent = dest_r.parent
            dest_parent.mkdir(parents=True, exist_ok=True)
            resolve_under_root(root, dest_parent)
            moved = exclusive_move(src_r, dest_r)
            return resolve_under_root(root, moved)
        except FileExistsError as e:
            last_err = e
            chosen = unique_dest(root, chosen_rel, src_r)
            continue
    raise OSError(f"could not place file without collision: {dest}") from last_err


def file_identity(path: Path) -> tuple[int | None, int | None, int, int]:
    st = path.stat()
    dev = getattr(st, "st_dev", None)
    ino = getattr(st, "st_ino", None)
    mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
    return int(dev) if dev is not None else None, int(ino) if ino is not None else None, st.st_size, int(mtime_ns)


def sha256_file(path: Path, *, max_bytes: int | None = None) -> str:
    h = hashlib.sha256()
    read = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            if max_bytes is not None and read + len(chunk) > max_bytes:
                chunk = chunk[: max(0, max_bytes - read)]
                h.update(chunk)
                break
            h.update(chunk)
            read += len(chunk)
    return h.hexdigest()


def sampled_hash(path: Path, size: int) -> str:
    h = hashlib.sha256()
    window = 1024 * 1024
    with open(path, "rb") as f:
        h.update(f.read(window))
        if size > window * 2:
            f.seek(max(0, size - window))
            h.update(f.read(window))
        h.update(f"{size}".encode())
    return "sampled:" + h.hexdigest()


def extract_json_object(text: str) -> dict:
    if not text:
        raise ValueError("empty LLM response")
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        obj = json.loads(s[start : end + 1])
        if isinstance(obj, dict):
            return obj
    raise ValueError("LLM response is not a JSON object")


def read_preview(path: Path, *, max_bytes: int = 8192, max_lines: int = 30) -> tuple[str, str]:
    try:
        with open(path, "rb") as f:
            blob = f.read(max_bytes)
    except OSError:
        return "", ""
    hex_part = blob[:256].hex()
    text = blob.decode("utf-8", errors="replace").replace("\x00", " ")
    lines = text.splitlines()[:max_lines]
    preview = "\n".join(lines)
    if len(preview) > 2000:
        preview = preview[:2000]
    return hex_part, preview
