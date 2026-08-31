from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sorto import __version__
from sorto.config import SortoConfig, ensure_state_dir, load_config, user_config_path
from sorto.db import Database
from sorto.doctor import format_report, run_doctor
from sorto.engine import Engine, make_llm
from sorto.logutil import setup_logging
from sorto.util import format_duration


def _add_root(p: argparse.ArgumentParser, required: bool = False) -> None:
    p.add_argument("--root", type=Path, required=required, default=None, help="Root directory to organize")


def _add_run_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dry-run", action="store_true", help="Analyze and show planned moves; do not move")
    p.add_argument("--suggest-only", action="store_true", help="Same as --dry-run")
    p.add_argument("--yes", action="store_true", help="Do not hold needs_user files (still never overwrite)")
    p.add_argument(
        "--delete-duplicates",
        action="store_true",
        help="Delete files whose sha256 matches an already-done file. Never deletes inside a git repository.",
    )
    p.add_argument(
        "--delete-junk",
        action="store_true",
        help="Delete cache/temp/thumbnail/backup junk (Android caches, .DS_Store, *.tmp, …). Never deletes inside a git repository.",
    )
    p.add_argument("--workers", type=int, default=None, help="Parallel LLM analysis workers (default 1)")
    p.add_argument("--scan-interval", type=float, default=None, metavar="SEC", help="Rescan interval (default 5)")
    p.add_argument("--once", action="store_true", help="Process current files, then exit when queue empty")
    p.add_argument("--follow", action="store_true", help="Keep scanning until quit (default)")
    p.add_argument("--include", action="append", default=[], metavar="GLOB", help="Repeatable include glob")
    p.add_argument("--exclude", action="append", default=[], metavar="GLOB", help="Repeatable exclude glob")
    p.add_argument("--max-file-mb", type=int, default=None, help="Skip sending bodies above this MB (default 64)")
    p.add_argument("--llm-url", default=None, help="OpenAI-compatible base URL")
    p.add_argument("--llm-model", default=None, help="Model name")
    p.add_argument("--llm-api-key", default=None, help="Optional API key")
    p.add_argument(
        "--dest-scheme",
        choices=["default", "by-type", "by-type-year", "library"],
        default=None,
    )
    p.add_argument("--log-level", default=None, help="DEBUG/INFO/WARNING/ERROR")
    p.add_argument("--no-tui", action="store_true", help="Plain log output instead of the TUI")
    p.add_argument("--fake-llm", action="store_true", help=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sorto",
        description="File SORTer/Organizer — local LLM classifies files; TUI shows live progress.",
    )
    parser.add_argument("--version", action="version", version=f"sorto {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Create _organization/ state under root")
    _add_root(p_init, required=False)
    p_init.set_defaults(func=cmd_init)

    p_run = sub.add_parser("run", help="Scan, classify, and organize files")
    _add_root(p_run, required=True)
    _add_run_opts(p_run)
    p_run.set_defaults(func=cmd_run)

    p_resume = sub.add_parser("resume", help="Continue a previous run (crash-safe)")
    _add_root(p_resume, required=True)
    _add_run_opts(p_resume)
    p_resume.set_defaults(func=cmd_resume)

    p_status = sub.add_parser("status", help="Print counts from the index")
    _add_root(p_status, required=True)
    p_status.set_defaults(func=cmd_status)

    p_doctor = sub.add_parser("doctor", help="Check root, sqlite, LLM, optional tools")
    _add_root(p_doctor, required=False)
    p_doctor.add_argument("--llm-url", default=None)
    p_doctor.add_argument("--llm-model", default=None)
    p_doctor.set_defaults(func=cmd_doctor)

    p_cfg = sub.add_parser("config", help="Show merged configuration")
    _add_root(p_cfg, required=False)
    p_cfg.set_defaults(func=cmd_config)

    return parser


def _require_root(args: argparse.Namespace) -> Path:
    root = args.root or Path.cwd()
    return Path(root).expanduser().resolve()


def cmd_init(args: argparse.Namespace) -> int:
    root = _require_root(args)
    if not root.exists():
        print(f"error: root does not exist: {root}", file=sys.stderr)
        return 2
    st = ensure_state_dir(root)
    load_config(root)
    print(f"initialized {st}")
    print(f"  config   {st / 'config.toml'}")
    print(f"  prompt   {st / 'prompts' / 'classify.md'}")
    print(f"  database {st / 'index.sqlite'}")
    print(f"edit the prompt and config, then: sorto run --root {root}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    root = _require_root(args)
    cfg = load_config(root)
    if not cfg.db_path.exists():
        print(f"no index yet at {cfg.db_path} (run sorto init / sorto run)")
        return 0
    db = Database(cfg.db_path)
    try:
        c = db.counts()
        print(f"root: {cfg.root}")
        print(f"total:        {c.total}")
        print(f"discovered:   {c.discovered}")
        print(f"identifying:  {c.identifying}")
        print(f"analyzing:    {c.analyzing}")
        print(f"planned:      {c.planned}")
        print(f"moving:       {c.moving}")
        print(f"done:         {c.done}")
        print(f"skipped:      {c.skipped}")
        print(f"error:        {c.error}")
        print(f"needs_user:   {c.needs_user}")
        print(f"pending:      {c.pending}")
    finally:
        db.close()
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve() if args.root else None
    checks = run_doctor(root, llm_url=args.llm_url, llm_model=args.llm_model)
    print(format_report(checks))
    return 0 if all(c.ok or c.name.startswith("bin:") for c in checks) else 1


def cmd_config(args: argparse.Namespace) -> int:
    print(f"user config: {user_config_path()}  exists={user_config_path().is_file()}")
    if args.root:
        cfg = load_config(Path(args.root))
        print(f"root config: {cfg.root_config_path}  exists={cfg.root_config_path.is_file()}")
        print()
        print(cfg.to_toml())
    else:
        print("pass --root PATH to print the merged per-root config")
    return 0


def _build_engine(args: argparse.Namespace, *, retry_errors: bool) -> tuple[SortoConfig, Engine]:
    root = _require_root(args)
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"error: root is not a directory: {root}")
    cfg = load_config(root, cli=args)
    cfg.retry_errors = retry_errors
    setup_logging(cfg.log_path, cfg.log_level)
    llm = make_llm(cfg, fake=bool(getattr(args, "fake_llm", False)))
    engine = Engine(cfg, llm=llm)
    return cfg, engine


def _headless_loop(engine: Engine) -> int:
    engine.start()
    try:
        while not engine.finished.wait(1.0):
            if engine.stop_event.is_set():
                break
            snap = engine.snapshot()
            c = snap.counts
            eta = format_duration(snap.eta_s) if snap.eta_s is not None else "unknown"
            print(
                f"[{snap.mode}] files={c.total} pending={c.pending} done={c.done} "
                f"err={c.error} {snap.progress_pct:.1f}% ETA {eta} "
                f"now={snap.current_analyze or snap.current_identify or '-'}",
                flush=True,
            )
            if not engine.cfg.follow and engine.finished.is_set():
                break
        return 0
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    finally:
        engine.request_stop()
        engine.join(timeout=8)


def _run_with_tui(engine: Engine) -> int:
    from sorto.tui import SortoApp

    app = SortoApp(engine)
    app.run()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg, engine = _build_engine(args, retry_errors=False)
    use_tui = not args.no_tui and sys.stdout.isatty() and os.environ.get("SORTO_NO_TUI") != "1"
    if use_tui:
        return _run_with_tui(engine)
    return _headless_loop(engine)


def cmd_resume(args: argparse.Namespace) -> int:
    cfg, engine = _build_engine(args, retry_errors=True)
    use_tui = not args.no_tui and sys.stdout.isatty() and os.environ.get("SORTO_NO_TUI") != "1"
    if use_tui:
        return _run_with_tui(engine)
    return _headless_loop(engine)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130



