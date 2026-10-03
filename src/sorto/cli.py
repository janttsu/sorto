from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from sorto import __version__
from sorto.config import (
    SortoConfig,
    ensure_user_config,
    load_config,
    user_config_path,
    validate_roots,
)
from sorto.db import Database
from sorto.doctor import format_report, run_doctor
from sorto.engine import Engine, _fit_context, describe_view, make_llm
from sorto.eta import eta_text
from sorto.llm import NotLocalError
from sorto.logutil import setup_logging


def _add_roots(p: argparse.ArgumentParser, *, required: bool = True) -> None:
    p.add_argument(
        "source",
        type=Path,
        nargs=None if required else "?",
        help="Directory whose files are sorted (files are moved out of it); "
        "the same directory as TARGET reorganizes TARGET in place",
    )
    p.add_argument(
        "-t",
        "--target",
        type=Path,
        required=required,
        help="Johnny.Decimal root to file into (its existing areas/categories/IDs are used as-is)",
    )


def _add_run_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dry-run", action="store_true", help="Analyze and show planned moves; do not move")
    p.add_argument("--suggest-only", action="store_true", help="Same as --dry-run")
    p.add_argument("--confirm", action="store_true", help="Ask before each move (Enter/y = move, n = keep)")
    p.add_argument("--yes", action="store_true", help="Also move files the model flagged needs_user")
    p.add_argument("--min-confidence", type=float, default=None, help="Below this, keep in source (default 0.5)")
    p.add_argument(
        "--clear-cache",
        action="store_true",
        help="Before the run, forget cached model answers and folder decisions (not which files were moved)",
    )
    p.add_argument(
        "--retry-kept",
        action="store_true",
        help="Ask again about files an earlier run left in place for you to decide (needs_user)",
    )
    p.add_argument(
        "--no-vision",
        action="store_true",
        help="Do not show photos / video frames to the model (EXIF is still used)",
    )
    p.add_argument("--no-rename", action="store_true", help="Never rename, even meaningless names")
    p.add_argument(
        "--accept-structure",
        action="store_true",
        help="If TARGET has no Johnny.Decimal structure, create the proposed one without asking",
    )
    p.add_argument(
        "--no-folders",
        action="store_true",
        help="Do not look at folders as a whole; sort every file on its own",
    )
    p.add_argument(
        "--no-new-ids",
        action="store_true",
        help="Never create a new ID; files that fit no existing ID stay where they are",
    )
    p.add_argument(
        "--no-new-categories",
        action="store_true",
        help="Never create a new category; a new ID is only made in a category that exists",
    )
    p.add_argument(
        "--depth",
        type=int,
        default=None,
        metavar="N",
        help="Reorganize mode: re-check files up to N folders below each ID (default 0 = directly in it)",
    )
    p.add_argument(
        "--rules",
        type=Path,
        default=None,
        metavar="FILE",
        help="Free-form sorting rules for the model (default ~/.config/sorto/rules.md)",
    )
    p.add_argument(
        "--junk-rules",
        type=Path,
        default=None,
        metavar="FILE",
        help="Patterns of files that go straight to one folder (default ~/.config/sorto/junk.md)",
    )
    p.add_argument(
        "--delete-duplicates",
        action="store_true",
        help="Delete source files byte-identical to one already filed. Never inside a git repository.",
    )
    p.add_argument(
        "--delete-junk",
        action="store_true",
        help="Delete cache/temp/thumbnail junk instead of leaving it. Never inside a git repository.",
    )
    p.add_argument("--scan-interval", type=float, default=None, metavar="SEC", help="Rescan interval (default 5)")
    p.add_argument("--once", action="store_true", help="Process current files, then exit")
    p.add_argument("--follow", action="store_true", help="Keep watching the source (default)")
    p.add_argument("--include", action="append", default=[], metavar="GLOB", help="Repeatable include glob")
    p.add_argument("--exclude", action="append", default=[], metavar="GLOB", help="Repeatable exclude glob")
    p.add_argument("--max-file-mb", type=int, default=None, help="Metadata only above this size (default 64)")
    p.add_argument("--llm-url", default=None, help="Local OpenAI-compatible base URL (loopback only)")
    p.add_argument("--llm-model", default=None, help="Model name (default qwen3.6:35b-a3b)")
    p.add_argument("--log-level", default=None, help="DEBUG/INFO/WARNING/ERROR")
    p.add_argument("--no-tui", action="store_true", help="Plain line output instead of the TUI")
    p.add_argument("--fake-llm", action="store_true", help=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sorto",
        description=(
            "Sort files from SOURCE into an existing Johnny.Decimal tree at TARGET, "
            "using only a local LLM. SOURCE = TARGET reorganizes the tree in place. "
            "Never deletes (unless asked), never overwrites."
        ),
    )
    parser.add_argument("--version", action="version", version=f"sorto {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Scan SOURCE, analyze each file, move it into TARGET")
    _add_roots(p_run)
    _add_run_opts(p_run)
    p_run.set_defaults(func=cmd_run)

    p_resume = sub.add_parser("resume", help="Like run, and retry files that errored")
    _add_roots(p_resume)
    _add_run_opts(p_resume)
    p_resume.set_defaults(func=cmd_resume)

    p_status = sub.add_parser("status", help="Print counts for a SOURCE/TARGET pair")
    _add_roots(p_status)
    p_status.set_defaults(func=cmd_status)

    p_init = sub.add_parser("init", help="Create a Johnny.Decimal structure for TARGET that fits the files")
    p_init.add_argument("target", type=Path, help="Where to create it")
    p_init.add_argument("source", type=Path, nargs="?", help="Files to design it for (default: TARGET itself)")
    p_init.add_argument("--accept-structure", action="store_true", help="Create it without asking")
    p_init.add_argument(
        "--from", dest="from_file", type=Path, default=None, metavar="FILE",
        help="Create the structure written in FILE (e.g. an edited proposal) instead of asking the model",
    )
    p_init.add_argument("--dry-run", action="store_true", help="Only show the proposal")
    p_init.add_argument("--llm-model", default=None)
    p_init.add_argument("--llm-url", default=None)
    p_init.add_argument("--fake-llm", action="store_true", help=argparse.SUPPRESS)
    p_init.set_defaults(func=cmd_init)

    p_index = sub.add_parser("index", help="Show the Johnny.Decimal outline sorto reads from TARGET")
    p_index.add_argument("target", type=Path)
    p_index.set_defaults(func=cmd_index)

    p_doctor = sub.add_parser("doctor", help="Check source/target, local LLM, optional tools")
    _add_roots(p_doctor, required=False)
    p_doctor.add_argument("--llm-url", default=None)
    p_doctor.add_argument("--llm-model", default=None)
    p_doctor.add_argument(
        "--no-warm", action="store_true", help="Do not load the configured models (faster, but unchecked)"
    )
    p_doctor.set_defaults(func=cmd_doctor)

    p_rules = sub.add_parser("rules", help="Show (and create) your free-form sorting rules file")
    p_rules.add_argument("target", type=Path, nargs="?", help="Check the IDs the rules mention against this tree")
    p_rules.add_argument("--rules", type=Path, default=None, metavar="FILE", help="Rules file to show")
    p_rules.add_argument("--junk-rules", type=Path, default=None, metavar="FILE", help="Junk patterns file to show")
    p_rules.set_defaults(func=cmd_rules)

    p_cfg = sub.add_parser("config", help="Show merged configuration (creates the user config if missing)")
    _add_roots(p_cfg, required=False)
    p_cfg.set_defaults(func=cmd_config)

    return parser


def _roots(args: argparse.Namespace) -> tuple[Path, Path]:
    source = Path(args.source).expanduser().resolve()
    target = Path(args.target).expanduser().resolve()
    try:
        validate_roots(source, target)
    except ValueError as e:
        raise SystemExit(f"error: {e}") from e
    return source, target


def bootstrap_structure(cfg: SortoConfig, llm: object, progress: object, args: argparse.Namespace) -> bool:
    """TARGET has no Johnny.Decimal structure: propose one, show it, create it if accepted."""
    from sorto.bootstrap import PROPOSAL_HEADER, create, normalize, parse_tree, survey, survey_text
    from sorto.llm import LLMError
    from sorto.rules import load_rules

    from_file = getattr(args, "from_file", None)
    if from_file:
        try:
            structure = parse_tree(Path(from_file).expanduser().read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"error: {from_file}: {e}", file=sys.stderr)
            return False
    else:
        propose = getattr(llm, "propose_structure", None)
        if propose is None:
            return False
        print(f"No Johnny.Decimal structure under {cfg.target}; sorto can create one.", flush=True)
        print(f"Surveying {cfg.source} …", flush=True)
        stats, total = survey(cfg.source)
        if not total:
            print("There are no files to organize.", file=sys.stderr)
            return False
        text = survey_text(stats, total, cfg.source.name)
        print(f"{total:,} files. Asking the local model for a structure (this can take a minute) …", flush=True)
        try:
            structure = normalize(propose(text, load_rules(cfg.rules_file).text))
        except LLMError as e:
            print(f"error: could not get a structure from the model: {e}", file=sys.stderr)
            return False
    areas, cats, ids = structure.counts
    if not ids:
        print("error: the model proposed no usable structure", file=sys.stderr)
        return False
    print("\n" + structure.render())
    print(
        f"\n{areas} areas, {cats} categories and {ids} IDs, plus a JDex.md with the descriptions, "
        f"would be created under {cfg.target}. Nothing that exists is changed."
    )
    saved = cfg.state / "structure-proposal.md"
    if not from_file:
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_text(PROPOSAL_HEADER + structure.render() + "\n", encoding="utf-8")
        print(f"To change names or IDs first: edit {saved}\nand run: sorto init {cfg.target} --from {saved}")
    if cfg.dry_run:
        print("Dry run: nothing created.")
        raise SystemExit(0)
    accepted = bool(getattr(args, "accept_structure", False))
    if not accepted:
        if not sys.stdin.isatty():
            print("Run again with --accept-structure to create it.", file=sys.stderr)
            return False
        accepted = input("Create this structure? [y/N] ").strip().lower() in ("y", "yes")
    if not accepted:
        print("Nothing created.")
        return False
    made = create(cfg.target, structure)
    progress.append({"action": "created_structure", "target": str(cfg.target), "created": made})
    print(f"Created {len(made)} folders and notes under {cfg.target}.\n", flush=True)
    return True


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.target).expanduser().resolve()
    source = Path(args.source).expanduser().resolve() if args.source else target
    try:
        validate_roots(source, target)
    except ValueError as e:
        raise SystemExit(f"error: {e}") from e
    cfg = load_config(source, target, cli=args)
    from sorto.apply import ProgressLog
    from sorto.config import ensure_state_dir
    from sorto.jd import scan_jd

    if len(scan_jd(target)):
        print(f"{target} already has a Johnny.Decimal structure (`sorto index {target}` shows it).")
        return 0
    llm = make_llm(cfg, fake=bool(getattr(args, "fake_llm", False)))
    _fit_context(cfg, llm)
    ensure_state_dir(cfg)
    return 0 if bootstrap_structure(cfg, llm, ProgressLog(cfg.progress_path), args) else 1


def cmd_status(args: argparse.Namespace) -> int:
    source, target = _roots(args)
    cfg = load_config(source, target)
    if not cfg.db_path.exists():
        print(f"no runs yet for {source} → {target}")
        return 0
    db = Database(cfg.db_path)
    try:
        c = db.counts()
        print(f"source: {cfg.source}")
        print(f"target: {cfg.target}")
        print(f"state:  {cfg.state}")
        print(f"index:  {cfg.db_path}  (delete it to start over)")
        runs = sorted(cfg.runs_dir.glob("run-*.log"))
        if runs:
            print(f"last log: {runs[-1]}  ({len(runs)} run log(s) in {cfg.runs_dir})")
        for name in ("total", "discovered", "planned", "done", "skipped", "needs_user", "error", "gone", "pending"):
            print(f"{name + ':':<12}{getattr(c, name)}")
    finally:
        db.close()
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    from sorto.jd import scan_jd

    target = Path(args.target).expanduser().resolve()
    if not target.is_dir():
        raise SystemExit(f"error: target is not a directory: {target}")
    index = scan_jd(target)
    print(index.render() or "(no Johnny.Decimal IDs found)")
    print(f"\n{len(index)} IDs")
    return 0 if len(index) else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    if (args.source is None) != (args.target is None):
        raise SystemExit("error: give both SOURCE and --target, or neither")
    checks = run_doctor(
        args.source, args.target, llm_url=args.llm_url, llm_model=args.llm_model, warm=not args.no_warm
    )
    print(format_report(checks))
    return 0 if all(c.ok or c.name.startswith("bin:") for c in checks) else 1


def cmd_rules(args: argparse.Namespace) -> int:
    from sorto.jd import scan_jd
    from sorto.rules import ensure_junk_file, ensure_rules_file, load_junk_rules, load_rules

    path = ensure_rules_file(args.rules)
    rules = load_rules(path)
    print(f"rules file: {path}")
    if not rules:
        print("(no rules yet: add plain-language lines below the comment; the template has examples)")
    else:
        print(f"{rules.line_count} rule line(s){' (truncated to fit the prompt)' if rules.truncated else ''}:\n")
        print(rules.text)
    junk = load_junk_rules(ensure_junk_file(args.junk_rules))
    print(f"\njunk file:  {junk.path}")
    if not junk:
        print("(no junk patterns yet: 'into: NN.NN' and then one pattern per line, e.g. *.dll)")
    else:
        where = f" → {junk.into}" if junk.into else ""
        print(f"{len(junk.rules)} pattern(s){where}: " + ", ".join(
            r.pattern + (f" → {r.jd_id}" if r.jd_id else "") for r in junk.rules))
    for problem in junk.problems:
        print(f"warning: junk file: {problem}")
    status = 1 if junk.problems else 0
    if args.target is not None:
        index = scan_jd(Path(args.target).expanduser().resolve())
        wanted = sorted(set(rules.ids) | set(junk.ids))
        unknown = [i for i in wanted if index.get(i) is None]
        print()
        if unknown:
            print(f"warning: these IDs are not in {args.target}: {', '.join(unknown)}")
            return 1
        print(f"all {len(wanted)} IDs the rules mention exist in {args.target}")
    return status


def cmd_config(args: argparse.Namespace) -> int:
    path = ensure_user_config()
    print(f"user config: {path}")
    if args.source is not None and args.target is not None:
        cfg = load_config(*_roots(args))
    else:
        cfg = load_config(Path.cwd(), Path.cwd())
        print("(pass SOURCE --target TARGET to see the state dir for that pair)")
    print()
    print(cfg.to_toml())
    return 0


def _build_engine(args: argparse.Namespace, *, retry_errors: bool) -> tuple[SortoConfig, Engine]:
    source, target = _roots(args)
    cfg = load_config(source, target, cli=args)
    cfg.retry_errors = retry_errors
    setup_logging(cfg.log_path, cfg.log_level)
    try:
        llm = make_llm(cfg, fake=bool(getattr(args, "fake_llm", False)))
    except NotLocalError as e:
        raise SystemExit(f"error: {e}") from e
    health = getattr(llm, "health", None)
    if health is not None and not getattr(args, "fake_llm", False):
        ok, detail = health()
        if not ok and "not pulled" in detail:
            raise SystemExit(f"error: model {cfg.llm_model!r} is not available at {cfg.llm_url}: {detail}")
        if not ok:
            print(f"warning: local model not reachable yet ({detail}); sorto keeps retrying", file=sys.stderr)
    _fit_context(cfg, llm)
    engine = Engine(cfg, llm=llm)
    if not len(engine.index) and bootstrap_structure(cfg, llm, engine.progress, args):
        engine.reload_index()
    if not len(engine.index):
        raise SystemExit(
            f"error: no Johnny.Decimal IDs found under {target}\n"
            "  sorto needs an existing structure there: areas 'NN-NN Name', categories 'NN Name' and IDs\n"
            "  'NN.NN Name'. Check the path (`sorto index TARGET` shows what sorto sees)."
        )
    return cfg, engine


def _headless_loop(engine: Engine) -> int:
    """Print each file's analysis and final location as it happens."""
    if engine.cfg.confirm:
        print("note: --confirm needs the TUI; moving without asking", file=sys.stderr)
        engine.cfg.confirm = False
    if engine.cfg.reorganize:
        where = engine.cfg.target / engine.cfg.reorganize_scope
        print(f"reorganizing {where} in place (depth {engine.cfg.reorganize_depth})", flush=True)
    print(f"index: {engine.cfg.db_path} (remembers processed files; delete it to start over)", flush=True)
    engine.start()
    if engine.run_log is not None:
        print(f"log of this run: {engine.run_log.path}", flush=True)
    shown = 0
    last_status = 0.0
    try:
        while True:
            done = engine.finished.wait(0.5)
            snap = engine.snapshot()
            for view in sorted((h for h in snap.history if h.seq > shown), key=lambda h: h.seq):
                shown = view.seq
                print(f"\n[{view.outcome}] {describe_view(view)}", flush=True)
                if view.current_location:
                    print(f"    was in: {view.current_location}", flush=True)
                if view.media_note:
                    print(f"    media: {view.media_note}", flush=True)
                if view.summary:
                    print(f"    {view.summary}", flush=True)
                if view.reason:
                    print(f"    why: {view.reason}", flush=True)
                if view.rule:
                    print(f"    rule: {view.rule}", flush=True)
                if view.new_id:
                    print(f"    new ID: {view.new_id}", flush=True)
                if view.folder:
                    print(f"    folder: {view.folder}", flush=True)
            if time.monotonic() - last_status > 10:
                last_status = time.monotonic()
                c = snap.counts
                eta = eta_text(snap)
                print(
                    f"-- [{snap.mode}] files={c.total} pending={c.pending} done={c.done} kept="
                    f"{c.skipped + c.needs_user} err={c.error} {snap.progress_pct:.1f}% ETA {eta}",
                    flush=True,
                )
            if done or engine.stop_event.is_set():
                break
        return 0
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    finally:
        engine.request_stop()
        engine.join(timeout=8)
        _print_leftovers(engine)


def _print_leftovers(engine: Engine) -> None:
    lines = engine.leftovers()
    if lines:
        print("\nleft in the source:", flush=True)
        for line in lines:
            print(f"    {line}", flush=True)


def _run_with_tui(engine: Engine) -> int:
    from sorto.tui import SortoApp

    SortoApp(engine).run()
    _print_leftovers(engine)
    if engine.run_log is not None:
        engine.run_log.close()  # the TUI may leave before the engine has wound down
        print(f"log of this run: {engine.run_log.path}")
    return 0


def _run(args: argparse.Namespace, *, retry_errors: bool) -> int:
    _cfg, engine = _build_engine(args, retry_errors=retry_errors)
    use_tui = not args.no_tui and sys.stdout.isatty() and os.environ.get("SORTO_NO_TUI") != "1"
    return _run_with_tui(engine) if use_tui else _headless_loop(engine)


def cmd_run(args: argparse.Namespace) -> int:
    return _run(args, retry_errors=False)


def cmd_resume(args: argparse.Namespace) -> int:
    return _run(args, retry_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130


__all__ = ["main", "build_parser", "user_config_path"]
