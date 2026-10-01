from __future__ import annotations

import ctypes
import ctypes.util
import errno
import hashlib
import json
import logging
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
FICLONE = 0x40049409  # Linux ioctl: share the source's data extents (btrfs, XFS, bcachefs)

log = logging.getLogger("sorto")

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


def is_git_repo(path: Path) -> bool:
    """*path* is a git repository: a working tree (``.git`` directory or file) or a bare one."""
    try:
        if (path / ".git").exists():
            return True
        return (path / "HEAD").is_file() and (path / "objects").is_dir() and (path / "refs").is_dir()
    except OSError:
        return False


def git_workdir(path: Path, stop: Path | None = None) -> Path | None:
    """Return the git repository that contains *path*, or None.

    Walks from the file's directory upwards looking for a repository (a
    working tree, a linked worktree or a bare repository). Without *stop* it
    walks to the filesystem root: that is how deleting inside any repository
    is refused. With *stop* only repositories below that directory count,
    not *stop* itself or anything above it: an archive that is itself kept
    in git can still be sorted.
    """
    try:
        cur = path.resolve()
        end = stop.resolve() if stop is not None else None
    except OSError:
        return None
    if cur.is_file() or not cur.is_dir():
        cur = cur.parent
    while True:
        if end is not None and (cur == end or end not in cur.parents):
            return None
        if is_git_repo(cur):
            return cur
        parent = cur.parent
        if parent == cur:
            return None
        cur = parent


DELETE_DUPLICATE_MARK = "__delete_duplicate__"
DELETE_JUNK_MARK = "__delete_junk__"


def state_home() -> Path:
    """sorto's own directory: ``~/.sorto`` (``SORTO_HOME`` overrides it)."""
    base = os.environ.get("SORTO_HOME")
    return Path(base).expanduser() if base else Path.home() / ".sorto"


def legacy_state_home() -> Path:
    """Where sorto 0.2–0.4 kept its state; moved to ``~/.sorto`` on first use."""
    base = os.environ.get("XDG_STATE_HOME")
    return (Path(base) if base else Path.home() / ".local" / "state") / "sorto"


def state_dir(source: Path, target: Path) -> Path:
    """Per (source, target) state, kept outside both trees.

    The target is often a synced tree; sorto's sqlite/jsonl must not land
    there, and the source is being emptied. A pair's directory from the old
    location is moved here the first time the pair is used again, so what
    sorto already processed is not forgotten.
    """
    key = f"{Path(source).resolve()}\0{Path(target).resolve()}"
    digest = hashlib.sha256(key.encode("utf-8", "surrogateescape")).hexdigest()[:12]
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(source).name)[:40] or "root"
    path = state_home() / f"{name}-{digest}"
    if not path.exists():
        legacy = legacy_state_home() / path.name
        if legacy.is_dir():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(legacy), str(path))
            except OSError:
                return legacy
    return path


def user_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "sorto" / "config.toml"


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
    if STATE_DIR_NAME in parts or is_partial_name(parts[-1]):
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


def _reflink(src_fd: int, dest_fd: int) -> bool:
    """Clone the whole file without copying data; False if the file system can't.

    Works between btrfs subvolumes of the same file system (even under
    different mount points), where rename() refuses with EXDEV: the new file
    shares the old one's extents, so it is instant and uses no extra space.
    """
    try:
        import fcntl
    except ImportError:
        return False
    try:
        fcntl.ioctl(dest_fd, getattr(fcntl, "FICLONE", FICLONE), src_fd)
        return True
    except OSError:
        return False


PARTIAL_MARK = ".sorto-partial-"


def partial_name(dest: Path) -> Path:
    """Hidden temp name next to *dest* for a copy that is still being written."""
    return dest.with_name(f".{dest.name}{PARTIAL_MARK}{uuid4().hex[:8]}")


def is_partial_name(name: str) -> bool:
    return PARTIAL_MARK in name


def _link_noreplace(tmp: Path, dest: Path) -> None:
    """Give the finished temp file its real name, never replacing *dest*."""
    try:
        _renameat2_noreplace(str(tmp), str(dest))
        return
    except OSError as e:
        if e.errno == errno.EEXIST:
            raise FileExistsError(e.errno, "destination exists", str(dest)) from e
        if e.errno not in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP):
            raise
    os.link(tmp, dest)  # raises FileExistsError if dest appeared meanwhile
    os.unlink(tmp)


def _copy_exclusive(src: Path, dest: Path) -> Path:
    """Place a copy at *dest* (never replacing anything), then remove *src*.

    Reflink clone first; a plain chunked copy if the file systems differ or
    do not support cloning. The data goes to a hidden temp file next to
    *dest*, which only gets its real name once it is complete, fsync'd and
    has the original timestamps, and only then is *src* removed. An
    interrupted move therefore never leaves a half-written file under the
    real name.
    """
    tmp = partial_name(dest)
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        with open(src, "rb") as inf:
            if _reflink(inf.fileno(), fd):
                log.info("reflink clone %s → %s", src, dest)
            else:
                os.ftruncate(fd, 0)
                while True:
                    chunk = inf.read(1024 * 1024)
                    if not chunk:
                        break
                    os.write(fd, chunk)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        try:
            shutil.copystat(src, tmp, follow_symlinks=True)
        except OSError:
            pass
        _link_noreplace(tmp, dest)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.unlink(src)
    return dest


def same_content(a: Path, b: Path) -> bool:
    """Byte-for-byte equal regular files (size first, then SHA-256)."""
    try:
        if not (a.is_file() and b.is_file()) or a.stat().st_size != b.stat().st_size:
            return False
        return sha256_file(a) == sha256_file(b)
    except OSError:
        return False


def exclusive_move(src: Path, dest: Path) -> Path:
    """Move src to dest without ever overwriting an existing dest.

    Prefers Linux renameat2(RENAME_NOREPLACE); across file systems or btrfs
    subvolumes a reflink clone (instant, no extra space) or a copy, both
    created with O_EXCL; hardlink+unlink when renameat2 is missing. Never
    uses os.replace on an unproven dest.
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


def safe_move_between(
    src: Path, src_root: Path, dest_rel: str, dest_root: Path, *, max_tries: int = 50
) -> Path:
    """Move *src* (under *src_root*) to *dest_rel* under *dest_root*.

    Never overwrites (collisions become ``name-2.ext``), never writes outside
    *dest_root* (symlinked parents included) and keeps directory names exactly
    as the target already spells them.
    """
    src_r = resolve_under_root(src_root, src)
    if not src_r.is_file() or src_r.is_symlink():
        raise UnsafePathError(f"refusing to move non-regular file: {src}")
    dest_root_r = Path(dest_root).resolve()
    rel = validate_dest_rel(dest_rel, preserve_names=True)
    last_err: OSError | None = None
    for _ in range(max_tries):
        dest = dest_root_r / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        resolve_under_root(dest_root_r, dest.parent)
        try:
            moved = exclusive_move(src_r, dest)
            return resolve_under_root(dest_root_r, moved)
        except FileExistsError as e:
            last_err = e
            chosen = unique_dest(dest_root_r, rel, src_r)
            rel = validate_dest_rel(
                posix_rel(os.path.relpath(chosen, start=dest_root_r)), preserve_names=True
            )
    raise OSError(f"could not place file without collision: {dest_rel}") from last_err


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
