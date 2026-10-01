"""Scan a source directory and file each file into an existing Johnny.Decimal target.

Threads: one scanner, N identify workers (cheap metadata, prefetch only) and a
single *sort* worker that handles one file at a time: analyze with the local
LLM → publish the English analysis → move into the chosen JD ID → publish the
final path → next file. The TUI renders snapshots and never touches disk.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import posixpath
import queue
import re
import shutil
import threading
import time
from collections import deque
from dataclasses import replace
from glob import escape as glob_escape
from pathlib import Path
from typing import Any

from sorto.apply import ProgressLog, apply_delete_duplicate, apply_delete_junk, apply_move
from sorto.config import SortoConfig, config_for_model, ensure_state_dir, packaged_prompt
from sorto.db import Database
from sorto.eta import Estimate, EtaTracker, file_kind
from sorto.folders import FolderProfile, build_packet, outlier_reason, walk_folder
from sorto.identify import identify_file
from sorto.jd import (
    AREA_RE,
    CATEGORY_RE,
    ID_RE,
    JDIndex,
    JDItem,
    Location,
    NewID,
    clean_id_name,
    id_folder_position,
    is_date_id,
    normalize_id,
    propose_new_category,
    propose_new_id,
    scan_jd,
    used_category_numbers,
    used_id_numbers,
)
from sorto.llm import FakeLLMClient, LLMError, LLMParseError, OpenAICompatClient
from sorto.media import date_subfolder
from sorto.models import (
    AnalysisPacket,
    AnalysisView,
    Classification,
    ModelStats,
    QueueRow,
    Snapshot,
)
from sorto.plan import Plan, PlanError, plan_destination
from sorto.rules import JunkRules, Rules, load_junk_rules, load_rules, rules_prompt_section
from sorto.runlog import RunLog
from sorto.scan import discover_batch
from sorto.util import (
    PARTIAL_MARK,
    estimate_tokens,
    git_workdir,
    human_size,
    is_git_repo,
    is_under_root,
    posix_rel,
    same_content,
    unique_dest,
    utc_now_iso,
)

log = logging.getLogger("sorto")


class _RetryLater(Exception):
    """The local model is unreachable; requeue the file after a back-off."""

HISTORY_LEN = 500  # files of this run the TUI can browse back through
PREFETCH = 16
# Share of the context window left for the system prompt + packet; the rest is
# headroom for the answer and tokenizer estimation error.
CONTEXT_USE = 0.8
PACKET_TOKENS = 4500
PENDING_KINDS_TTL = 3.0
FLAT_MAX_FILES = 250_000  # a folder without subfolders is judged whole up to this size
RULES_RELOAD_SEC = 60.0  # rules.md is re-read this often while sorto runs

REORGANIZE_NOTE = """\
REORGANIZE MODE: the user is tidying this archive in place. Every file is already somewhere
inside the outline above; "currently_filed_in" says where. If the file fits where it is,
answer with that same jd_id (and its current subfolder). Choose a different ID only when
the file clearly belongs there, and say why in reason. Loose files and files in an inbox
(.01) should be filed into the best specific ID.
"""


class Engine:
    def __init__(
        self,
        cfg: SortoConfig,
        *,
        llm: Any | None = None,
        db: Database | None = None,
        structure_llm: Any | None = None,
    ):
        self.cfg = cfg
        ensure_state_dir(cfg)
        self.db = db or Database(cfg.db_path)
        self.progress = ProgressLog(cfg.progress_path)
        self.run_log: RunLog | None = None  # started with the run, see start()
        if llm is None:
            llm = make_llm(cfg)
            _fit_context(cfg, llm)
        self.llm = llm
        # Decides new IDs and categories (cfg.structure_model); built on first use, see _structure_llm().
        self._structure_given = structure_llm
        self._structure: tuple[str, Any | None] | None = None
        self.eta = EtaTracker()
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.finished = threading.Event()
        self.identify_q: queue.Queue[int | None] = queue.Queue()
        self.sort_q: queue.Queue[int | None] = queue.Queue(maxsize=PREFETCH)
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._log_lines: deque[str] = deque(maxlen=200)
        self._log_seq = 0
        self._current_identify: str | None = None
        self._current: AnalysisView | None = None
        self._history: deque[AnalysisView] = deque(maxlen=HISTORY_LEN)
        self._packets: dict[int, AnalysisPacket] = {}
        self._tokens_est = 0
        self._llm_ok = True
        self._llm_error: str | None = None
        self._scan_complete_pass = False
        self._scan_running = True
        self._started = time.monotonic()
        self._inflight = 0
        self._enqueued: set[int] = set()
        self._index_at = 0.0
        self._finished_seq = 0
        # --confirm: the sort worker waits here for the user's decision.
        self._decision = threading.Event()
        self._decision_value: str = ""
        self._awaiting = False
        self.index = JDIndex(root=cfg.target)
        self.rules: Rules = Rules(path=cfg.rules_file)
        self.junk_rules: JunkRules = load_junk_rules(cfg.junk_file)
        self._outline = ""
        self._system_prompt = ""
        self._index_sig = ""
        self._pending_kinds: dict[str, int] = {}
        self._pending_kinds_at = 0.0
        self._model_stats: dict[str, ModelStats] = {}
        self._folder_cache: dict[str, dict[str, Any]] = {}
        self._confirm_wait = 0.0
        self.reload_index()

    # ------------------------------------------------------------------ setup

    def reload_index(self) -> None:
        index = scan_jd(self.cfg.target, exclude=self._index_excludes())
        self.rules = load_rules(self.cfg.rules_file)
        outline = index.render(max_chars=self._outline_budget_chars())
        with self._lock:
            self.index = index
            self._outline = outline
            self._index_at = time.monotonic()
        self._set_prompt(self._build_prompt(outline))

    def _set_prompt(self, prompt: str) -> None:
        # Cached answers are only valid for the same outline *and* instructions.
        sig = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
        with self._lock:
            if sig != self._index_sig:
                self._index_sig = sig
                self._system_prompt = prompt

    def reload_rules(self) -> bool:
        """Re-read rules.md and junk.md; True if either changed. The next file uses them.

        Rules sit after the outline in the prompt, so the model server keeps
        the cached outline and only reads the rules again.
        """
        junk = load_junk_rules(self.cfg.junk_file)
        if (junk.into, [(r.pattern, r.jd_id) for r in junk.rules]) != (
            self.junk_rules.into, [(r.pattern, r.jd_id) for r in self.junk_rules.rules]
        ):
            self.emit("rules", f"re-read {junk.path}: {len(junk.rules)} junk pattern(s)")
            for problem in junk.problems:
                self.emit("rules", f"junk file: {problem}")
            self.junk_rules = junk  # last, so whoever sees the new rules also finds them in the log
        rules = load_rules(self.cfg.rules_file)
        if rules.text == self.rules.text:
            return False
        before = self.rules.line_count
        self.rules = rules
        with self._lock:
            index = self.index
        outline = index.render(max_chars=self._outline_budget_chars())
        with self._lock:
            self._outline = outline
        self._set_prompt(self._build_prompt(outline))
        self.emit("rules", f"re-read {rules.path}: {rules.line_count} rule line(s), was {before}")
        unknown = [i for i in rules.ids if index.get(i) is None]
        if unknown:
            self.emit("rules", f"rules mention IDs that are not in the target: {', '.join(unknown)}")
        return True

    def _rules_loop(self) -> None:
        while not self.stop_event.wait(RULES_RELOAD_SEC):
            try:
                self.reload_rules()
            except Exception:
                log.exception("rules reload failed")

    def _index_excludes(self) -> list[Path]:
        """A source inside the target (e.g. an inbox ID) is not a place to file into."""
        if self.cfg.reorganize:
            return []
        return [self.cfg.source] if is_under_root(self.cfg.target, self.cfg.source) else []

    def _outline_budget_chars(self) -> int:
        budget = int(self.cfg.context_window * CONTEXT_USE) - self.cfg.max_tokens - PACKET_TOKENS
        budget -= estimate_tokens(self.rules.text)
        return max(4000, budget * 3)

    def _build_prompt(self, outline: str) -> str:
        try:
            text = self.cfg.prompt_path.read_text(encoding="utf-8")
        except OSError:
            text = packaged_prompt()
        prompt = (
            f"{text.rstrip()}\n\n"
            f"TARGET JOHNNY.DECIMAL OUTLINE (the only IDs you may choose):\n{outline}\n"
        )
        if self.cfg.reorganize:
            prompt += f"\n{REORGANIZE_NOTE}"
        rules = rules_prompt_section(self.rules)
        if rules:
            prompt += f"\n{rules}"
        return prompt

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    def emit(self, kind: str, message: str, file_id: int | None = None) -> None:
        line = f"{utc_now_iso()}  {kind:8}  {message}"
        log.info("%s %s", kind, message)
        try:
            self.db.add_event(kind, message, file_id)
        except Exception:
            log.exception("failed to persist event")
        with self._lock:
            self._log_lines.append(line)
            self._log_seq += 1

    # --------------------------------------------------------------- controls

    def request_stop(self) -> None:
        self.stop_event.set()
        self.pause_event.clear()
        self._decision.set()
        for q in (self.identify_q, self.sort_q):
            try:
                q.put_nowait(None)
            except queue.Full:
                pass

    def toggle_pause(self) -> bool:
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.emit("engine", "resumed")
            return False
        self.pause_event.set()
        self.emit("engine", "paused")
        return True

    def set_dry_run(self, value: bool) -> bool:
        """Toggle dry-run only when idle. Returns True if applied."""
        if not self.is_idle():
            return False
        self.cfg.dry_run = value
        if not value:
            # Planned-but-not-moved files from the dry run: file them for real.
            for file_id in self.db.ids_by_status("planned"):
                self.db.update(file_id, status="discovered")
                self._enqueue_identify(file_id)
        self.emit("engine", f"mode={'DRY' if value else 'LIVE'}")
        return True

    def switch_model(self, model: str | None = None) -> str:
        """Use *model* (default: the next one in cfg.models) from the next file on."""
        models = self.cfg.models or [self.cfg.llm_model]
        if model is None:
            cur = self.model_name
            model = models[(models.index(cur) + 1) % len(models)] if cur in models else models[0]
        if model == self.model_name:
            return model
        cfg = config_for_model(self.cfg, model)
        llm = make_llm(cfg)
        health = getattr(llm, "health", None)
        if health is not None:
            ok, detail = health()
            if not ok:
                raise ValueError(f"{model}: {detail}")
        _fit_context(cfg, llm)
        self.cfg.llm_model = cfg.llm_model
        self.cfg.llm_url = cfg.llm_url
        self.cfg.context_window = cfg.context_window
        self.cfg.timeout_sec = cfg.timeout_sec
        self.cfg.llm_profile_source = cfg.llm_profile_source
        self.llm = llm
        self.reload_index()
        self.emit("engine", f"model → {model} (from the next file; loading it now)")
        self.warm_up()
        return model

    def warm_up(self) -> None:
        """Load the model and cache the system prompt while files are prepared."""
        warm = getattr(self.llm, "warm_up", None)
        if warm is None:
            return
        prompt = self._system_prompt

        def _run() -> None:
            ok, detail = warm(prompt)
            self.emit("llm", f"warm-up {self.model_name}: {detail}")

        threading.Thread(target=_run, name="sorto-warm", daemon=True).start()

    def decide(self, value: str) -> bool:
        """--confirm answer from the UI: 'move' or 'keep'. True if one was awaited."""
        with self._lock:
            if not self._awaiting:
                return False
            self._decision_value = value
        self._decision.set()
        return True

    # ------------------------------------------------------------- lifecycle

    def _enqueue_identify(self, file_id: int) -> None:
        with self._lock:
            if file_id in self._enqueued:
                return
            self._enqueued.add(file_id)
        self.identify_q.put(file_id)

    def _release(self, file_id: int) -> None:
        with self._lock:
            self._enqueued.discard(file_id)
        self._packets.pop(file_id, None)

    def recover(self) -> None:
        """Reset in-flight rows after a crash; requeue work."""
        for file_id in self.db.ids_by_status("moving"):
            row = self.db.get(file_id)
            if not row:
                continue
            src = self.cfg.source / (row["src_rel"] or "")
            dest_rel = row["dest_rel"]
            dest = self.cfg.target / str(dest_rel) if dest_rel else None
            if dest is not None:
                self._remove_partials(dest)
            if not src.exists() and dest is not None and dest.exists():
                self.progress.append(
                    {"action": "moved", "src_rel": row["src_rel"], "dest_rel": dest_rel, "recovered": True}
                )
                self._mark_moved(file_id, str(row["src_rel"]), str(dest_rel))
                self.emit("recover", f"completed interrupted move → {dest_rel}", file_id)
                continue
            if dest is not None and dest.exists():
                if self._finish_copied_move(file_id, src, dest, str(row["src_rel"]), str(dest_rel)):
                    continue
                msg = (
                    f"interrupted move: {dest_rel} exists but differs from the source; "
                    "nothing was deleted, check both files"
                )
                self.db.update(file_id, status="error", error=msg)
                self.emit("recover", msg, file_id)
                continue
            self.db.update(file_id, status="discovered")
            self.emit("recover", f"reset moving {row['src_rel']}", file_id)
        if self.cfg.clear_cache:
            answers, folders, kept = self.db.clear_caches()
            self._folder_cache.clear()
            self.cfg.clear_cache = False  # once per run, not again when the engine restarts its loops
            self.emit(
                "engine",
                f"--clear-cache: forgot {answers} cached model answer(s) and {folders} folder decision(s)"
                + (f"; {kept} folder(s) already partly moved keep theirs" if kept else ""),
            )
        if self.cfg.retry_errors:
            for file_id in self.db.ids_by_status("error"):
                self.db.update(file_id, status="discovered", error=None)
        if self.cfg.retry_kept:
            # Worth it after the rules, the target or sorto itself changed. Files the user chose to
            # keep, duplicates and junk ("skipped") are not asked about again.
            again = self.db.ids_by_status("needs_user")
            for file_id in again:
                self.db.update(file_id, status="discovered")
            if again:
                self.emit("engine", f"--retry-kept: asking again about {len(again)} file(s) left for you earlier")
        stale = ("identifying", "analyzing") if self.cfg.dry_run else ("identifying", "analyzing", "planned")
        for file_id in self.db.ids_by_status(*stale):
            self.db.update(file_id, status="discovered")
        for file_id in self.db.ids_by_status("discovered"):
            self._enqueue_identify(file_id)

    def _remove_partials(self, dest: Path) -> None:
        """Delete sorto's own unfinished temp copies next to *dest* (``.name.sorto-partial-*``)."""
        try:
            for tmp in dest.parent.glob(f".{glob_escape(dest.name)}{PARTIAL_MARK}*"):
                if tmp.is_file() and not tmp.is_symlink():
                    tmp.unlink()
                    self.emit("recover", f"removed unfinished temp copy {tmp.name}")
        except OSError as e:
            self.emit("error", f"could not clean temp copies next to {dest}: {e}")

    def _finish_copied_move(self, file_id: int, src: Path, dest: Path, src_rel: str, dest_rel: str) -> bool:
        """A copy-based move was interrupted after the copy: finish it if the copy is exact."""
        if not src.exists():
            return False
        if not same_content(src, dest):
            return False
        try:
            shutil.copystat(src, dest, follow_symlinks=True)
        except OSError:
            pass
        try:
            src.unlink()
        except OSError as e:
            self.emit("error", f"could not finish interrupted move of {src_rel}: {e}", file_id)
            return False
        self.progress.append({"action": "moved", "src_rel": src_rel, "dest_rel": dest_rel, "recovered": True})
        self._mark_moved(file_id, src_rel, dest_rel)
        self.emit("recover", f"finished interrupted move (identical copy) → {dest_rel}", file_id)
        return True

    def start(self) -> None:
        self.recover()
        self._started = time.monotonic()
        self.emit(
            "engine",
            f"start source={self.cfg.source} target={self.cfg.target} ids={len(self.index)} "
            f"model={self.model_name} mode={'DRY' if self.cfg.dry_run else 'LIVE'}"
            + (f" reorganize depth={self.cfg.reorganize_depth}" if self.cfg.reorganize else ""),
        )
        if self.cfg.reorganize_scope:
            self.emit(
                "engine",
                f"{self.cfg.reorganize_scope} is part of the target: it is reorganized in place. Its IDs stay "
                "valid, and a file already in the right ID stays where it is",
            )
        self.emit("engine", f"remembers processed files in {self.cfg.db_path} (delete it to start over)")
        self._start_run_log()
        if not len(self.index):
            self.emit("error", f"no Johnny.Decimal IDs found under {self.cfg.target}")
        if self.junk_rules:
            self.emit("rules", f"{len(self.junk_rules.rules)} junk pattern(s) from {self.junk_rules.path}")
        for problem in self.junk_rules.problems:
            self.emit("rules", f"junk file: {problem}")
        if self.rules:
            self.emit("rules", f"{self.rules.line_count} rule line(s) from {self.rules.path}")
            unknown = [i for i in self.rules.ids if self.index.get(i) is None]
            if unknown:
                self.emit("rules", f"rules mention IDs that are not in the target: {', '.join(unknown)}")
        self.warm_up()
        self._spawn(self._scan_loop, "sorto-scan")
        for i in range(self.cfg.identify_workers):
            self._spawn(self._identify_loop, f"sorto-id-{i}")
        self._spawn(self._sort_loop, "sorto-sort")
        self._spawn(self._rules_loop, "sorto-rules")

    def _start_run_log(self) -> None:
        """This run's own log file. Sorting goes on without it if it cannot be written."""
        mode = "dry run, nothing is moved" if self.cfg.dry_run else "files are moved"
        if self.cfg.reorganize:
            what = f"{self.cfg.reorganize_scope} of the target" if self.cfg.reorganize_scope else "the target"
            mode += f"; reorganizing {what} in place (depth {self.cfg.reorganize_depth})"
        structure = getattr(self._structure_llm(), "model", "") if self.cfg.allow_new_ids else ""
        header = {"source": str(self.cfg.source), "target": str(self.cfg.target), "model": self.model_name}
        if structure and structure != self.model_name:
            header["new IDs by"] = f"{structure} (names and places new IDs and categories)"
        try:
            self.run_log = RunLog(self.cfg.runs_dir, {**header, "mode": mode})
        except OSError as e:
            self.emit("error", f"could not start a log file for this run in {self.cfg.runs_dir}: {e}")
            return
        self.emit("engine", f"log of this run: {self.run_log.path}")

    def _spawn(self, fn: Any, name: str) -> None:
        t = threading.Thread(target=fn, name=name, daemon=True)
        self._threads.append(t)
        t.start()

    def join(self, timeout: float | None = None) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        for t in self._threads:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            t.join(timeout=remaining)
        self.db.checkpoint()
        try:
            self.progress.close()
        except Exception:
            pass
        if self.run_log is not None:
            self.run_log.close()

    def run_until_idle(self, timeout: float = 120.0) -> Snapshot:
        """Start, wait until --once would exit (or timeout), then stop."""
        was_follow = self.cfg.follow
        self.cfg.follow = False
        self.start()
        try:
            ok = self.finished.wait(timeout=timeout)
            self.request_stop()
            self.join(timeout=10)
            if not ok:
                raise TimeoutError("engine did not become idle in time")
            return self.snapshot()
        finally:
            self.cfg.follow = was_follow

    @property
    def model_name(self) -> str:
        return getattr(self.llm, "model", None) or self.cfg.llm_model

    # --------------------------------------------------------------- snapshot

    @staticmethod
    def _queue_busy(q: queue.Queue) -> bool:
        with q.mutex:
            return bool(q.unfinished_tasks)

    def is_idle(self) -> bool:
        with self._lock:
            busy = self._inflight or self._current_identify or (
                self._current is not None and self._current.stage != "done"
            )
        if busy or self._queue_busy(self.identify_q) or self._queue_busy(self.sort_q):
            return False
        c = self.db.counts()
        waiting = c.discovered + c.identifying + c.analyzing + c.moving
        return waiting == 0 if self.cfg.dry_run else waiting + c.planned == 0

    def snapshot(self) -> Snapshot:
        counts = self.db.counts()
        dry = self.cfg.dry_run
        completed = counts.done + counts.skipped + counts.error + counts.needs_user
        pending = counts.discovered + counts.identifying + counts.analyzing + counts.moving
        if dry:
            completed += counts.planned
        else:
            pending += counts.planned
        need = completed + pending
        estimate = self._estimate()
        with self._lock:
            if self.cfg.follow:
                scan_state = "live" if not self._scan_complete_pass else "complete, watching"
            else:
                scan_state = "live" if not self._scan_complete_pass else "complete, draining queue"
            current = replace(self._current) if self._current else None
            history = [replace(h) for h in reversed(self._history)]
            snap = Snapshot(
                source=str(self.cfg.source),
                target=str(self.cfg.target),
                jd_ids=len(self.index),
                model=self.model_name,
                mode="DRY" if dry else "LIVE",
                scan_state=scan_state,
                counts=counts,
                current_identify=self._current_identify,
                current_analyze=current.src_rel if current and current.stage == "analyzing" else None,
                current_move=current.src_rel if current and current.stage == "moving" else None,
                current=current,
                last_analysis=history[0] if history else None,
                history=history,
                log_lines=list(self._log_lines),
                log_seq=self._log_seq,
                llm_ok=self._llm_ok,
                llm_latency_s=getattr(self.llm, "last_latency_s", None),
                llm_error=self._llm_error,
                progress_pct=(100.0 * completed / need) if need else 0.0,
                elapsed_s=time.monotonic() - self._started,
                eta_s=estimate.seconds,
                per_file_s=estimate.per_file,
                pace_samples=estimate.samples,
                paused=self.pause_event.is_set(),
                follow=self.cfg.follow,
                scan_still_running=self._scan_running and not self._scan_complete_pass,
                tokens_est=self._tokens_est,
                finished=self.finished.is_set(),
                awaiting_confirm=self._awaiting,
                reorganize=self.cfg.reorganize,
                reorganize_depth=self.cfg.reorganize_depth,
                reorganize_scope=self.cfg.reorganize_scope,
                rules=self.rules.line_count,
                models=list(self.cfg.models),
                model_stats=[replace(m) for m in self._model_stats.values()],
            )
        counts.pending = pending
        snap.queue_rows = [
            QueueRow(status=str(r["status"]), src_rel=str(r["src_rel"] or "")) for r in self.db.recent(12)
        ]
        return snap

    def _estimate(self) -> Estimate:
        """Time left for everything still waiting, at the pace measured so far."""
        now = time.monotonic()
        if now - self._pending_kinds_at > PENDING_KINDS_TTL:
            kinds: dict[str, int] = {}
            for rel in self.db.pending_rels(include_planned=not self.cfg.dry_run):
                k = file_kind(rel)
                kinds[k] = kinds.get(k, 0) + 1
            self._pending_kinds, self._pending_kinds_at = kinds, now
        return self.eta.estimate(self._pending_kinds)

    # ------------------------------------------------------------------ scan

    def _wait_if_paused(self) -> None:
        while self.pause_event.is_set() and not self.stop_event.is_set():
            time.sleep(0.1)

    def _scan_excludes(self) -> list[str]:
        """Never scan the target or sorto's own state, even when they sit inside the source."""
        excl = list(self.cfg.exclude)
        src = self.cfg.source.resolve()
        for inner in (self.cfg.target, self.cfg.state):
            if inner == self.cfg.source:
                continue  # reorganize: the target *is* the source
            if is_under_root(src, inner):
                excl.append(f"{posix_rel(str(inner.resolve().relative_to(src)))}/**")
        return excl

    def _scan_loop(self) -> None:
        try:
            while not self.stop_event.is_set():
                self._wait_if_paused()
                if self.stop_event.is_set():
                    break
                with self._lock:
                    self._scan_running = True
                if self.cfg.follow and time.monotonic() - self._index_at > 60:
                    self.reload_index()
                new_count = self._scan_pass()
                with self._lock:
                    self._scan_complete_pass = new_count == 0
                    self._scan_running = False
                if not self.cfg.follow:
                    self._wait_drained()
                    if self.stop_event.is_set():
                        break
                    if new_count == 0 and self._scan_pass() == 0 and self.is_idle():
                        self.emit("engine", "scan complete, queue empty")
                        self.finished.set()
                        self.request_stop()
                        break
                else:
                    if new_count == 0:
                        self.finished.clear()
                    slept = 0.0
                    while slept < self.cfg.scan_interval and not self.stop_event.is_set():
                        time.sleep(0.2)
                        slept += 0.2
        except Exception:
            log.exception("scan loop crashed")
            self.emit("error", "scan loop crashed")
        finally:
            with self._lock:
                self._scan_running = False
            if not self.cfg.follow:
                self.finished.set()

    def _wait_drained(self) -> None:
        idle_hits = 0
        while not self.stop_event.is_set():
            self._wait_if_paused()
            if self.is_idle():
                idle_hits += 1
                if idle_hits >= 2:
                    return
                time.sleep(0.05)
                continue
            idle_hits = 0
            time.sleep(0.1)

    def _scan_pass(self) -> int:
        new_work = 0
        reorg = self.cfg.reorganize
        for path, rel, size, mtime_ns, dev, ino in discover_batch(
            self.cfg.source,
            self.cfg.include,
            self._scan_excludes(),
            prune=self._reorganize_prune if reorg else _git_prune,
        ):
            if self.stop_event.is_set():
                break
            if reorg and not self._reorganize_candidate(rel):
                continue
            try:
                file_id, is_new = self.db.upsert_discovered(
                    src_rel=rel, abs_path=str(path), size=size, mtime_ns=mtime_ns, dev=dev, ino=ino
                )
            except OSError as e:
                self.emit("error", f"db discover {rel}: {e}")
                continue
            if is_new:
                new_work += 1
                self._enqueue_identify(file_id)
        if new_work:
            self.emit("scan", f"discovered {new_work} new/changed file(s)")
        return new_work

    def _reorganize_prune(self, rel_dir: str, path: Path) -> bool:
        """Reorganize mode: which folders not to walk into (True = skip).

        Only files near the top of each ID are re-checked (``reorganize_depth``
        folders below it); deeper trees are projects, backups or albums that
        move as a whole, not file by file. ``NN.00`` notes, hidden folders,
        unknown ID folders and git working trees are never touched.
        """
        parts = rel_dir.split("/")
        if parts[-1].startswith("."):
            return True
        scope = self.cfg.reorganize_scope
        if scope and not (f"{rel_dir}/".startswith(f"{scope}/") or f"{scope}/".startswith(f"{rel_dir}/")):
            return True  # only one area or category is being reorganized
        pos = id_folder_position(parts)
        if pos is not None:
            item = self.index.items.get(normalize_id(parts[pos]))
            if item is None or item.is_index or item.rel != "/".join(parts[: pos + 1]):
                return True
            if len(parts) - pos - 1 > self.cfg.reorganize_depth:
                return True
        return is_git_repo(path)

    def _reorganize_candidate(self, rel: str) -> bool:
        name = rel.rsplit("/", 1)[-1].lower()
        if "jdex" in name:
            return False  # the index notes describe the tree; they stay put
        if self.cfg.reorganize_scope and not rel.startswith(f"{self.cfg.reorganize_scope}/"):
            return False
        loc = self.index.locate(rel)
        if loc.item is not None and (loc.item.is_index or loc.depth > self.cfg.reorganize_depth):
            return False
        return True

    # -------------------------------------------------------------- identify

    def _identify_loop(self) -> None:
        while True:
            if self.pause_event.is_set() and not self.stop_event.is_set():
                time.sleep(0.1)
                continue
            try:
                item = self.identify_q.get(timeout=0.2)
            except queue.Empty:
                if self.stop_event.is_set():
                    break
                continue
            try:
                if item is not None:
                    self._identify_one(item)
            except Exception as e:
                log.exception("identify failed")
                self._fail(item, e, viewed=False)
            finally:
                self.identify_q.task_done()

    def _identify_one(self, file_id: int) -> None:
        row = self.db.get(file_id)
        if not row or not row["src_rel"]:
            self._release(file_id)
            return
        src_rel = str(row["src_rel"])
        path = self.cfg.source / src_rel
        with self._lock:
            self._inflight += 1
            self._current_identify = src_rel
        try:
            self.db.update(file_id, status="identifying")
            if not path.is_file():
                self.db.update(file_id, status="error", error="source missing")
                self.emit("error", f"missing {src_rel}", file_id)
                self._note_run("error", f"{src_rel}: the file is no longer in the source")
                self._release(file_id)
                return
            # Files that go to the junk folder by rule, or move with their folder, need no
            # previews: nothing is asked of the model about them (EXIF still serves the outlier check).
            light = bool(self.junk_rules.match(src_rel)) or (
                self._folders_on() and self._known_unit(src_rel) is not None
            )
            packet = identify_file(
                path, src_rel, self.cfg, size=row["size"], mtime_ns=row["mtime_ns"], light=light
            )
            packet.light = light
            if self.cfg.reorganize:
                packet.current_location = self.index.locate(src_rel).describe()
            self.db.update(file_id, mime=packet.mime, type_guess=packet.type_guess, sha256=packet.sha256)
            self._packets[file_id] = packet
            while not self.stop_event.is_set():
                try:
                    self.sort_q.put(file_id, timeout=0.2)
                    break
                except queue.Full:
                    continue
        except OSError as e:
            self._fail(file_id, e, viewed=False)
        finally:
            with self._lock:
                self._inflight -= 1
                if self._current_identify == src_rel:
                    self._current_identify = None

    # ------------------------------------------------------------------ sort

    def _sort_loop(self) -> None:
        while True:
            if self.pause_event.is_set() and not self.stop_event.is_set():
                time.sleep(0.1)
                continue
            try:
                item = self.sort_q.get(timeout=0.2)
            except queue.Empty:
                if self.stop_event.is_set():
                    break
                continue
            try:
                if item is not None and not self.stop_event.is_set():
                    self._sort_one(item)
                elif item is not None:
                    self.db.update(item, status="discovered")
                    self._release(item)
            except _RetryLater:
                for _ in range(int(min(15.0, max(1.0, self.cfg.scan_interval)) * 5)):
                    if self.stop_event.is_set():
                        break
                    time.sleep(0.2)
                if not self.stop_event.is_set():
                    self._enqueue_identify(item)
            except Exception as e:
                log.exception("sort failed")
                self._fail(item, e)
                self._finish_view("error", str(e))
            finally:
                self.sort_q.task_done()

    def _set_view(self, **fields: Any) -> None:
        with self._lock:
            if self._current is not None:
                for k, v in fields.items():
                    setattr(self._current, k, v)

    def _finish_view(self, outcome: str, dest_rel: str = "", reason: str | None = None) -> None:
        with self._lock:
            view = self._current
            if view is None:
                return
            view.stage = "done"
            view.outcome = outcome
            self._finished_seq += 1
            view.seq = self._finished_seq
            if dest_rel:
                view.dest_rel = dest_rel
            model_reason = view.reason
            if reason is not None:
                view.reason = reason
            done = replace(view)
            self._history.append(done)
        if self.run_log is not None:
            self.run_log.file(done, model_reason)

    def _sort_one(self, file_id: int) -> None:
        row = self.db.get(file_id)
        packet = self._packets.get(file_id)
        if not row or not row["src_rel"] or packet is None:
            self._release(file_id)
            return
        src_rel = str(row["src_rel"])
        t0 = time.monotonic()
        self._confirm_wait = 0.0
        retry = False
        with self._lock:
            self._inflight += 1
            self._current = AnalysisView(
                src_rel=src_rel,
                filename=packet.filename,
                mime=packet.mime or "",
                size=packet.size,
                media_note=packet.media_note,
                current_location=packet.current_location,
                model=self.model_name,
                stage="analyzing",
            )
            self._tokens_est = estimate_tokens(self._system_prompt) + estimate_tokens(
                json.dumps(packet.to_llm_dict(), ensure_ascii=False)
            )
        try:
            self.db.update(file_id, status="analyzing")
            if self._handle_duplicate(file_id, src_rel, packet):
                return
            if self._handle_junk_rule(file_id, src_rel, packet):
                return
            if packet.is_junk and self._handle_junk(file_id, src_rel, packet):
                return
            if self._folders_on():
                unit, context = self._unit_for(file_id, src_rel)
                if unit is not None:
                    inner = src_rel[len(unit["dir"]) + 1 :]
                    profile = FolderProfile.from_json(unit.get("profile") or "")
                    # Software, a website or a backup only works in one piece: nothing is taken out.
                    reason = "" if unit.get("intact") else outlier_reason(inner, packet.exif, profile)
                    if not reason:
                        self._move_with_folder(file_id, src_rel, unit, inner, packet)
                        return
                    self.emit("folder", f"{src_rel}: checked on its own, {reason}", file_id)
                    context = (
                        f"Its folder {unit['dir']} holds: {unit.get('summary')} The folder moves as a whole to "
                        f"{unit['jd_id']}. This file stands out ({reason}), so check it on its own. If it is "
                        f"still part of what the folder is (a picture that belongs to a project, an attachment, "
                        f"material of the same event), answer {unit['jd_id']} and it keeps its place in the "
                        f"folder. Choose another ID only when it is clearly something else."
                    )
                    self._set_view(folder=f"{unit['dir']}: this file stands out ({reason})")
                else:
                    inner = ""
                if packet.light:
                    packet = self._full_packet(file_id, src_rel, packet)
                packet.folder_context = context
                self._analyze_and_move(file_id, src_rel, packet, unit, inner)
                return
            self._analyze_and_move(file_id, src_rel, packet)
        except _RetryLater:
            retry = True
            raise
        finally:
            if not retry:
                # Pace for the ETA: time in the sort worker, minus time spent
                # waiting for the user to answer --confirm.
                self.eta.add(time.monotonic() - t0 - self._confirm_wait, file_kind(src_rel))
            with self._lock:
                self._inflight -= 1
            self._release(file_id)

    # --------------------------------------------------------------- folders

    def _folders_on(self) -> bool:
        return self.cfg.folder_mode and getattr(self.llm, "classify_folder", None) is not None

    def _folder_dirs(self, src_rel: str) -> list[str]:
        """The folders above a file, outermost first ("a", "a/b", "a/b/c").

        When reorganizing, only folders outside the Johnny.Decimal structure
        count: what is already filed is re-checked file by file.
        """
        parts = src_rel.split("/")[:-1]
        if self.cfg.reorganize:
            loose = []
            for name in parts:
                if AREA_RE.match(name) or CATEGORY_RE.match(name) or ID_RE.match(name):
                    break
                loose.append(name)
            parts = loose if len(loose) == len(parts) else []
        return ["/".join(parts[: i + 1]) for i in range(len(parts))]

    def _folder_row(self, rel: str) -> dict[str, Any] | None:
        row = self._folder_cache.get(rel)
        if row is None:
            found = self.db.folder_get(rel)
            if found is not None:
                row = self._folder_cache[rel] = dict(found)
        return row

    def _known_unit(self, src_rel: str) -> dict[str, Any] | None:
        """The already decided whole-folder unit of a file, without asking the model."""
        for rel in self._folder_dirs(src_rel):
            row = self._folder_row(rel)
            if row is None or row["decision"] == "small":
                return None
            if row["decision"] == "unit":
                return row
        return None

    def _unit_for(self, file_id: int, src_rel: str) -> tuple[dict[str, Any] | None, str]:
        """Top-down: the outermost folder that moves as a whole, and context from mixed folders.

        Folders too big to judge at once are split into their subfolders; a
        folder too small to matter ends the search (its files go one by one).
        """
        context = ""
        for rel in self._folder_dirs(src_rel):
            row = self._folder_row(rel) or self._decide_folder(file_id, rel)
            if row["decision"] == "unit":
                return row, context
            if row["decision"] == "small":
                break
            if row["decision"] == "mixed" and row.get("summary"):
                context = f"Its folder {rel} holds: {row['summary']} (mixed content, sorted file by file)"
        self._set_view(folder="")  # no folder moves as a whole: the file is sorted on its own
        return None, context

    def _remember_folder(self, rel: str, decision: str, **fields: Any) -> dict[str, Any]:
        row = {"dir": rel, "decision": decision, **fields}
        self._folder_cache[rel] = row
        if not self.cfg.dry_run:  # a dry run's decisions are re-made when the run is real
            self.db.folder_put(rel, decision, **fields)
        return row

    def _decide_folder(self, file_id: int, rel: str) -> dict[str, Any]:
        cfg = self.cfg
        root = cfg.source / rel
        if any(p != cfg.source and is_under_root(root, p) for p in (cfg.target, cfg.state)):
            return self._remember_folder(rel, "mixed", reason="contains the target or sorto's own state")
        walk = walk_folder(cfg.source, rel, cfg.folder_max_files)
        if walk.truncated:
            if _has_subfolders(root):
                return self._remember_folder(rel, "big", files=len(walk.files))
            # A big flat folder (a month of photos, a year of backups) cannot be split
            # further: judge it as a whole; every file is still checked on its own.
            walk = walk_folder(cfg.source, rel, FLAT_MAX_FILES)
            if walk.truncated:
                return self._remember_folder(rel, "big", files=len(walk.files))
        n = len(walk.files)
        if n < cfg.folder_min_files:
            return self._remember_folder(rel, "small", files=n)
        self._set_view(folder=f"{rel} ({n} files): looking at the folder as a whole", stage="analyzing")
        packet = build_packet(cfg.source, walk, vision=cfg.vision, preview_px=cfg.preview_px)
        t0 = time.monotonic()
        try:
            ans = self.llm.classify_folder(packet, self._system_prompt)
        except LLMParseError as e:
            return self._remember_folder(rel, "mixed", files=n, reason=f"folder answer unusable: {e}"[:300])
        except LLMError as e:
            self._llm_ok, self._llm_error = False, str(e)
            self.emit("llm", f"blocked: {e}")
            self.db.update(file_id, status="discovered")
            self._set_view(stage="done", outcome="waiting", reason=f"local model unavailable: {e}"[:300])
            raise _RetryLater from e
        self._llm_ok, self._llm_error = True, None
        self._note_model(time.monotonic() - t0)
        base = {"files": n, "summary": ans.summary, "confidence": ans.confidence, "profile": packet.profile.to_json()}
        if not ans.coherent or ans.confidence < cfg.folder_min_confidence:
            why = ans.reason if not ans.coherent else (
                f"confidence {ans.confidence:.2f} below {cfg.folder_min_confidence:.2f}"
            )
            self.emit("folder", f"{rel} ({n} files): sorted file by file, {why}")
            self._note_run("folder", f"{rel} ({n} files): sorted file by file, {why}")
            return self._remember_folder(rel, "mixed", reason=why[:300], **base)
        item = self.index.get(ans.jd_id)
        new: NewID | None = None
        if item is None:
            res = self._resolve_new_id(
                file_id,
                jd_id=ans.jd_id,
                category=ans.new_id_category,
                name=ans.new_id_name,
                label="folder",
                summary=ans.summary,
                confidence=ans.confidence,
            )
            if isinstance(res, str):
                self.emit("folder", f"{rel}: sorted file by file, {res}")
                self._note_run("folder", f"{rel} ({n} files): sorted file by file, {res}")
                return self._remember_folder(rel, "mixed", reason=res[:300], **base)
            item, new = res.item, (None if res.reused else res)
        dir_rel = item.rel if new else self.index.resolve_dir(item, ans.subfolder)[0]
        dest_dir = self._free_dir(f"{dir_rel}/{Path(rel).name}")
        self._set_view(
            label="folder",
            confidence=ans.confidence,
            summary=ans.summary,
            jd_id=item.id,
            jd_name=item.name,
            reason=ans.reason,
            rule=ans.rule,
            dest_rel=dest_dir,
            new_id=new.describe() if new else "",
            folder=f"{rel} ({n} files) moves as a whole",
            folder_unit=True,
            stage="moving",
        )
        if cfg.confirm and not cfg.dry_run and not self._confirmed():
            self._set_view(folder_unit=False, new_id="")
            return self._remember_folder(rel, "mixed", reason="you chose to sort it file by file", **base)
        self._set_view(folder_unit=False)
        if new is not None and not cfg.dry_run:
            error = self._create_id(file_id, rel, new)
            if error:
                return self._remember_folder(rel, "mixed", reason=error, **base)
        # Program code makes it software for certain; otherwise the model's word counts.
        intact = ans.intact or packet.is_software
        own = ans.own_media and not intact and cfg.date_folders != "off"
        note = f"; its own photos and videos go by date to {item.rel}/YYYY/MM" if own else ""
        note = "; kept in one piece" if intact else note
        self.emit("folder", f"{rel}: {n} files move together to {dest_dir}{note}")
        self._note_run("folder", f"{rel} ({n} files): {ans.summary} Moves together to {dest_dir}{note}")
        return self._remember_folder(
            rel, "unit", jd_id=item.id, dest_dir=dest_dir, reason=ans.reason[:300],
            own_media=int(own), intact=int(intact), **base,
        )

    def _free_dir(self, dest_dir: str) -> str:
        """A destination folder nothing uses yet: name, name-2, name-3 …"""
        taken = {r.get("dest_dir") for r in self._folder_cache.values()}
        cand, n = dest_dir, 2
        while (self.cfg.target / cand).exists() or cand in taken:
            cand, n = f"{dest_dir}-{n}", n + 1
        return cand

    def _move_with_folder(
        self, file_id: int, src_rel: str, unit: dict[str, Any], inner: str, packet: AnalysisPacket,
        *, by_date: bool = True,
    ) -> None:
        """A file whose folder moves as a whole: same place inside the folder's new home.

        The exception is the user's own photos and videos: they are always
        filed by capture date, so they go to YYYY/MM inside the folder's ID.
        """
        dest_rel = f"{unit['dest_dir']}/{inner}"
        item = self.index.get(unit["jd_id"])
        why = f"moves with its folder {unit['dir']}; its type, date, GPS and camera fit the folder"
        if not by_date:
            why = f"checked on its own and still part of its folder {unit['dir']}: moves with it"
        elif unit.get("intact"):
            why = f"moves with its folder {unit['dir']}, which is kept in one piece"
        stamp = ""
        if by_date and not unit.get("intact"):
            listed = item is not None and is_date_id(item, self.cfg.date_folder_ids)
            stamp = date_subfolder(
                packet.media_kind, packet.exif, own=bool(unit.get("own_media")) or listed,
                mode=self.cfg.date_folders, filename=packet.filename,
            )
        if stamp:
            # In a dry run a new ID does not exist yet; its folder will be the unit's parent.
            id_rel = item.rel if item else posixpath.dirname(unit["dest_dir"])
            dest = unique_dest(self.cfg.target, f"{id_rel}/{stamp}/{packet.filename}", self.cfg.source / src_rel)
            dest_rel = posix_rel(str(dest.relative_to(self.cfg.target)))
            why = f"photo or video from the folder {unit['dir']}: filed by capture date ({stamp})"
        self._set_view(
            label="part of a folder",
            confidence=float(unit.get("confidence") or 0.0),
            summary=unit.get("summary") or "",
            jd_id=unit["jd_id"],
            jd_name=item.name if item else "",
            reason=why,
            folder=f"{unit['dir']} ({unit.get('files')} files) moves as a whole",
            dest_rel=dest_rel,
            stage="moving",
        )
        self.db.update(
            file_id,
            llm_label="folder",
            llm_confidence=unit.get("confidence"),
            summary=unit.get("summary"),
            jd_id=unit["jd_id"],
            status="planned",
            dest_rel=dest_rel,
            analyzed_at=utc_now_iso(),
        )
        self.emit("plan", f"{src_rel} → {dest_rel} (with its folder)", file_id)
        if self.stop_event.is_set():
            return
        self._move(file_id, src_rel, dest_rel)

    def _full_packet(self, file_id: int, src_rel: str, light: AnalysisPacket) -> AnalysisPacket:
        """A file that stands out from its folder is sorted on its own: it needs previews after all."""
        row = self.db.get(file_id)
        try:
            packet = identify_file(
                self.cfg.source / src_rel,
                src_rel,
                self.cfg,
                size=row["size"] if row else None,
                mtime_ns=row["mtime_ns"] if row else None,
            )
        except OSError:
            return light
        packet.current_location = light.current_location
        self._packets[file_id] = packet
        return packet

    def _keep(
        self, file_id: int, src_rel: str, status: str, reason: str, dest_rel: str = "", outcome: str = "kept"
    ) -> None:
        """Leave the file where it is and say why."""
        self.db.update(file_id, status=status, error=None, llm_reason=reason, finished_at=utc_now_iso())
        self.progress.append({"action": status, "src_rel": src_rel, "reason": reason, "dest_rel": dest_rel})
        self.emit("kept", f"{src_rel}: {reason}", file_id)
        self._finish_view(outcome, dest_rel, reason)


    def _handle_duplicate(self, file_id: int, src_rel: str, packet: AnalysisPacket) -> bool:
        sha = packet.sha256
        if not sha or sha.startswith("sampled:"):
            return False
        other = self.db.get_by_sha(sha)
        if not other or int(other["id"]) == file_id or not other["dest_rel"]:
            return False
        original = self.cfg.target / str(other["dest_rel"])
        if not original.is_file():
            return False
        self.db.update(file_id, duplicate_of=str(other["dest_rel"]))
        self._set_view(
            label="duplicate",
            confidence=1.0,
            summary=f"Byte-for-byte copy of a file sorto already filed at {other['dest_rel']}.",
            jd_id=str(other["jd_id"] or ""),
        )
        repo = git_workdir(self.cfg.source / src_rel)
        if not self.cfg.delete_duplicates or repo is not None:
            why = "inside a git repo" if repo is not None else "pass --delete-duplicates to remove"
            self._keep(file_id, src_rel, "skipped", f"duplicate of {other['dest_rel']} ({why})")
            return True
        try:
            apply_delete_duplicate(
                root=self.cfg.source, src_rel=src_rel, original=original, dry_run=self.cfg.dry_run
            )
        except (OSError, ValueError) as e:
            self._fail(file_id, e)
            self._finish_view("error", reason=str(e))
            return True
        action = "dry_run_deleted_duplicate" if self.cfg.dry_run else "deleted_duplicate"
        self.progress.append({"action": action, "src_rel": src_rel, "duplicate_of": other["dest_rel"], "sha256": sha})
        self.db.update(
            file_id,
            status="planned" if self.cfg.dry_run else "done",
            finished_at=utc_now_iso(),
            **({} if self.cfg.dry_run else {"src_rel": None, "orig_rel": src_rel}),
        )
        self.emit("deleted" if not self.cfg.dry_run else "dry-run", f"{src_rel} duplicate of {other['dest_rel']}", file_id)
        self._finish_view("deleted", reason=f"duplicate of {other['dest_rel']}")
        return True

    def _handle_junk_rule(self, file_id: int, src_rel: str, packet: AnalysisPacket) -> bool:
        """A file the user's junk.md says is never needed: straight to its folder, no model."""
        rule = self.junk_rules.match(src_rel) if self.junk_rules else None
        if rule is None:
            return False
        jd_id = rule.jd_id or self.junk_rules.into
        item = self.index.get(jd_id)
        label = f"junk rule {rule.pattern}"
        if item is None:
            self._keep(
                file_id, src_rel, "needs_user", f"{label} points to {jd_id or 'no ID'}, which is not in the target"
            )
            return True
        dest_rel = f"{item.rel}/{packet.filename}"
        if posixpath.dirname(src_rel) == item.rel:
            self._keep(file_id, src_rel, "skipped", f"{label}: already in {item.id}", src_rel, outcome="in_place")
            return True
        self._set_view(
            label="junk (by rule)",
            confidence=1.0,
            summary=f"Matches your junk pattern {rule.pattern}: never needed, so it goes to {item.id} "
            f"{item.name} without asking the model.",
            jd_id=item.id,
            jd_name=item.name,
            rule=f"{label} → {item.id}",
            reason="a junk pattern from junk.md; the model is not asked",
            dest_rel=dest_rel,
            stage="moving",
        )
        self.db.update(
            file_id,
            llm_label="junk-rule",
            llm_confidence=1.0,
            summary=f"junk pattern {rule.pattern}",
            jd_id=item.id,
            status="planned",
            dest_rel=dest_rel,
            analyzed_at=utc_now_iso(),
        )
        self.emit("plan", f"{src_rel} → {dest_rel} ({label})", file_id)
        if self.cfg.confirm and not self.cfg.dry_run and not self._confirmed():
            self._keep(file_id, src_rel, "skipped", "kept by user", dest_rel)
            return True
        if not self.stop_event.is_set():
            self._move(file_id, src_rel, dest_rel)
        return True

    def _handle_junk(self, file_id: int, src_rel: str, packet: AnalysisPacket) -> bool:
        reason = packet.junk_reason or "cache/temp/junk"
        self._set_view(
            label="junk",
            confidence=1.0,
            summary=f"Cache, temp or OS clutter ({reason}); not worth filing into the archive.",
        )
        repo = git_workdir(self.cfg.source / src_rel)
        if not self.cfg.delete_junk or repo is not None:
            why = "inside a git repo" if repo is not None else "pass --delete-junk to remove"
            self._keep(file_id, src_rel, "skipped", f"junk: {reason} ({why})")
            return True
        try:
            apply_delete_junk(root=self.cfg.source, src_rel=src_rel, dry_run=self.cfg.dry_run)
        except (OSError, ValueError) as e:
            self._fail(file_id, e)
            self._finish_view("error", reason=str(e))
            return True
        action = "dry_run_deleted_junk" if self.cfg.dry_run else "deleted_junk"
        self.progress.append({"action": action, "src_rel": src_rel, "reason": reason})
        self.db.update(
            file_id,
            status="planned" if self.cfg.dry_run else "done",
            finished_at=utc_now_iso(),
            **({} if self.cfg.dry_run else {"src_rel": None, "orig_rel": src_rel}),
        )
        self.emit("deleted" if not self.cfg.dry_run else "dry-run", f"{src_rel} junk ({reason})", file_id)
        self._finish_view("deleted", reason=f"junk: {reason}")
        return True

    def _cache_key(self, packet: AnalysisPacket) -> str:
        ident = packet.sha256 or f"{packet.size}:{packet.mtime_ns}"
        seen = f"v{len(packet.images)}" if packet.images else "t"
        if packet.folder_context:
            seen += ":" + hashlib.sha256(packet.folder_context.encode("utf-8")).hexdigest()[:8]
        return f"jd:{self.model_name}:{self._index_sig}:{ident}:{packet.filename}:{seen}"

    def _classify(self, file_id: int, src_rel: str, packet: AnalysisPacket) -> Classification | None:
        key = self._cache_key(packet)
        cached = self.db.cache_get(key)
        if cached:
            try:
                return Classification(**json.loads(cached))
            except (TypeError, ValueError):
                pass
        t0 = time.monotonic()
        try:
            cls = self.llm.classify(packet, self._system_prompt)
        except LLMParseError as e:
            self._llm_ok, self._llm_error = True, str(e)
            self._keep(file_id, src_rel, "error", f"model reply was not valid JSON: {e}"[:300])
            return None
        except LLMError as e:
            self._llm_ok, self._llm_error = False, str(e)
            self.emit("llm", f"blocked: {e}")
            self.db.update(file_id, status="discovered")
            self._set_view(stage="done", outcome="waiting", reason=f"local model unavailable: {e}"[:300])
            raise _RetryLater from e
        self._llm_ok, self._llm_error = True, None
        self._note_model(time.monotonic() - t0)
        data = {k: getattr(cls, k) for k in Classification.__dataclass_fields__ if k != "raw"}
        self.db.cache_put(key, json.dumps(data, ensure_ascii=False))
        return cls

    def _analyze_and_move(
        self, file_id: int, src_rel: str, packet: AnalysisPacket, unit: dict[str, Any] | None = None, inner: str = ""
    ) -> None:
        cls = self._classify(file_id, src_rel, packet)
        if cls is None:
            return
        item = self.index.get(cls.jd_id)
        self._set_view(
            label=cls.label,
            confidence=cls.confidence,
            summary=cls.summary or "(the model gave no description)",
            jd_id=item.id if item else cls.jd_id,
            jd_name=item.name if item else "",
            reason=cls.reason,
            rule=cls.rule,
            latency_s=getattr(self.llm, "last_latency_s", 0.0) or 0.0,
            tokens=getattr(self.llm, "last_tokens_est", 0) or self._tokens_est,
            stage="moving",
        )
        self.db.update(
            file_id,
            llm_label=cls.label,
            llm_confidence=cls.confidence,
            llm_reason=cls.reason,
            summary=cls.summary,
            jd_id=cls.jd_id,
            analyzed_at=utc_now_iso(),
        )
        if cls.needs_user and not self.cfg.yes:
            self._keep(file_id, src_rel, "needs_user", f"model asks for a human decision: {cls.reason}")
            return
        if cls.confidence < self.cfg.min_confidence:
            self._keep(
                file_id,
                src_rel,
                "needs_user",
                f"confidence {cls.confidence:.2f} below {self.cfg.min_confidence:.2f}; left in source",
            )
            return
        if unit is not None and cls.jd_id == unit["jd_id"] and not self._goes_by_date(packet, cls, item):
            # Looked at on its own, the file still belongs where its folder goes: keep it in the folder.
            self._move_with_folder(file_id, src_rel, unit, inner, packet, by_date=False)
            return
        new: NewID | None = None
        if item is None:
            new = self._new_id_for(file_id, src_rel, cls)
            if new is None:
                return
            if new.reused:
                cls = replace(cls, jd_id=new.item.id)
                if unit is not None and cls.jd_id == unit["jd_id"] and not self._goes_by_date(packet, cls, new.item):
                    self._move_with_folder(file_id, src_rel, unit, inner, packet, by_date=False)
                    return
                new = None
            else:
                self._set_view(new_id=new.describe())
        try:
            plan = plan_destination(
                self.cfg.target,
                self.index,
                packet,
                cls,
                src_path=self.cfg.source / src_rel,
                allow_rename=self.cfg.allow_rename,
                allow_extension_fix=self.cfg.allow_extension_fix,
                new_item=new.item if new else None,
                own_media=self._is_own_media(packet, cls),
                date_folders=self.cfg.date_folders,
                date_ids=self.cfg.date_folder_ids,
            )
        except PlanError as e:
            self._keep(file_id, src_rel, "needs_user", str(e))
            return
        if self.cfg.reorganize and self._stays(file_id, src_rel, plan, cls):
            return
        self._set_view(dest_rel=plan.dest_rel, jd_id=plan.item.id, jd_name=plan.item.name)
        self.db.update(file_id, status="planned", dest_rel=plan.dest_rel, jd_id=plan.item.id)
        created = f" (new ID {plan.item.id} {plan.item.name})" if new else ""
        self.emit("plan", f"{src_rel} → {plan.item.id} {plan.dest_rel}{created}", file_id)
        if self.cfg.confirm and not self.cfg.dry_run and not self._confirmed():
            self._keep(file_id, src_rel, "skipped", "kept in source by user", plan.dest_rel)
            return
        if self.stop_event.is_set():
            return
        if new is not None and not self.cfg.dry_run:
            error = self._create_id(file_id, src_rel, new)
            if error:
                self._keep(file_id, src_rel, "needs_user", error)
                return
        self._move(file_id, src_rel, plan.dest_rel)

    def _goes_by_date(self, packet: AnalysisPacket, cls: Classification, item: JDItem | None) -> bool:
        """This photo or video will be filed into YYYY/MM rather than next to other files."""
        mode = self.cfg.date_folders
        if mode == "off" or not packet.media_kind:
            return False
        listed = item is not None and is_date_id(item, self.cfg.date_folder_ids)
        return mode == "all" or listed or self._is_own_media(packet, cls)

    @staticmethod
    def _is_own_media(packet: AnalysisPacket, cls: Classification) -> bool:
        """The user's own photo or video: the model says so; if it said nothing, a camera made it."""
        if not packet.media_kind:
            return False
        return bool(packet.camera_signs) if cls.own_media is None else cls.own_media

    def _new_id_for(self, file_id: int, src_rel: str, cls: Classification) -> NewID | None:
        """The model named no existing ID: plan a new one, or keep the file and say why."""
        result = self._resolve_new_id(
            file_id,
            jd_id=cls.jd_id,
            category=cls.new_id_category,
            name=cls.new_id_name,
            label=cls.label,
            summary=cls.summary,
            confidence=cls.confidence,
        )
        if isinstance(result, str):
            self._keep(file_id, src_rel, "needs_user", result)
            return None
        return result

    def _resolve_new_id(
        self, file_id: int, *, jd_id: str, category: str, name: str, label: str, summary: str, confidence: float
    ) -> NewID | str:
        """A new ID for an answer that named no existing one, or the reason there is none.

        What it is called and where it goes is decided by the structure model
        (``cfg.structure_model``, the big one by default), also when a small
        model reads the files: a new ID or category stays in the archive.
        """
        chosen = jd_id or "no ID"
        if not self.cfg.allow_new_ids:
            return f"model chose {chosen!r}, which is not an ID in the target"
        # "new" is a proper answer; anything else that is not an ID is worth showing in the reason.
        said = "" if chosen.strip().lower() == "new" else f"model chose {chosen!r}, which is not an ID in the target; "
        category = category or normalize_id(jd_id)[:2]
        if not name and normalize_id(jd_id):
            # "40.36 - emails": a made-up number with a topic; the topic is usable, the number is not.
            name = re.sub(r"^\D*\d{2}\.\d{2}", "", jd_id)
        suggested = name = clean_id_name(name)
        llm = self._structure_llm()
        if not suggested or getattr(llm, "name_new_id", None) is not None:
            # The name is always settled in one focused question, with the IDs and the rules in
            # view: names made up file by file drift ("Club papers", "Meetings", "Club events"
            # for one rule) and one topic ends up with several IDs. The name in the answer, if
            # there was one, is passed on as a suggestion.
            answer = f'{chosen}; suggested name for a new ID: "{suggested}"' if suggested else jd_id
            name, why = self._name_new_id(llm, file_id, answer, label, summary)
            if not name and llm is self.llm:
                name = suggested  # the same model had already named it
            if not name:
                return f"{said}{why}"
            # One topic, one ID: the category check may answer differently from file to file, and
            # the same name would then get an ID in several categories.
            same = [i for i in self.index.choices() if i.name.casefold() == name.casefold()]
            if len(same) == 1:
                return NewID(item=same[0], category=same[0].id[:2], reused=True)
        checked, why = self._check_category(llm, file_id, name, summary, category)
        if checked == "none":
            proposal = self._new_category(llm, file_id, name, summary, why)
        elif checked is None:
            return why
        else:
            proposal = propose_new_id(self.index, checked, name)
        if isinstance(proposal, str):
            return f"{said}{proposal}"
        threshold = self.cfg.new_id_min_confidence
        if not proposal.reused and confidence < threshold:
            return (
                f"model suggests a new ID {proposal.describe()}, but its confidence "
                f"{confidence:.2f} is below the {threshold:.2f} needed to create one"
            )
        return proposal

    def _structure_llm(self) -> Any:
        """The model that decides new IDs and categories: ``cfg.structure_model``.

        It is the analysis model itself when that is the same model, when no
        structure model is set, or when the structure model is not on the
        server (said once in the log). Ollama swaps the two models in and
        out of memory by itself; each new ID then costs a model load.
        """
        if self._structure_given is not None:
            return self._structure_given
        name = self.cfg.structure_model
        if not name or name == self.model_name or not isinstance(self.llm, OpenAICompatClient):
            return self.llm
        if self._structure is None or self._structure[0] != name:
            cfg = config_for_model(self.cfg, name)
            llm = make_llm(cfg)
            _fit_context(cfg, llm)
            ok, detail = llm.health()
            if not ok and "not pulled" in detail:
                self.emit("engine", f"structure model {name} is not available ({detail}); {self.model_name} decides new IDs")
                llm = None
            else:
                self.emit("engine", f"new IDs and categories are decided by {name}, files are read by {self.model_name}")
            self._structure = (name, llm)
        return self._structure[1] or self.llm

    def _name_new_id(self, llm: Any, file_id: int, answer: str, label: str, summary: str) -> tuple[str, str]:
        """Ask the model what the file's new ID should be called when its answer named nothing usable.

        Only the name is asked for. Which category it belongs in is the next,
        separate question, and the number is the next free one. Answering
        with the name of an ID that already exists makes the file join it
        (see the caller). Returns ("", why) when there is no usable name.
        """
        nothing = "the model gave no usable name for the new ID"
        namer = getattr(llm, "name_new_id", None)
        if namer is None:
            return "", nothing
        try:
            name, why = namer(answer, label, summary, self.index.category_outline(), self.rules.text)
        except LLMError as e:
            return "", f"could not ask for a name for the new ID: {e}"[:300]
        name = clean_id_name(name)
        self.emit("jd", f"{answer or 'no ID'!r} is not an ID; asked for a name: {name or 'none'!r} ({why})", file_id)
        return name, "" if name else nothing

    def _check_category(
        self, llm: Any, file_id: int, name: str, summary: str, suggested: str
    ) -> tuple[str | None, str]:
        """Ask the model once more, with only the categories in view, where a new ID belongs.

        Choosing a category is easy to get wrong inside a whole classification
        (both local models put a workout log under "Code" in a test); asked
        on its own, the same model picked "Health". Its answer decides.
        Returns the category, "none" when no existing category fits (a new
        one may then be made), or None with the reason the file stays.
        """
        chooser = getattr(llm, "choose_category", None)
        if chooser is None:
            return suggested, ""
        try:
            category, confidence, why = chooser(name, summary, self.index.category_outline())
        except LLMError as e:
            return None, f"could not check the category for a new ID: {e}"[:300]
        self.emit("jd", f"category check for new ID {name!r}: {category} ({confidence:.2f}) {why}", file_id)
        if category == "none":
            return "none", why
        if confidence < self.cfg.new_id_min_confidence:
            return None, (
                f"unsure which category a new ID for {name!r} belongs in ({category}, confidence {confidence:.2f})"
            )
        return category, ""

    def _new_category(self, llm: Any, file_id: int, name: str, summary: str, why_none: str) -> NewID | str:
        """No existing category fits: plan a new one in an existing area, or say why there is none.

        The model picks the area and names the category; the number is the
        next free one in that area and the ID is the category's first. When
        this does not work out either, the file stays where it is and the
        reason goes to the log.
        """
        nothing = f"no existing category fits a new ID for {name!r}"
        inventor = getattr(llm, "invent_category", None)
        if not self.cfg.allow_new_categories or inventor is None:
            return f"{nothing}: {why_none}"[:300]
        try:
            area, category_name, confidence, why = inventor(name, summary, self.index.area_outline(), self.rules.text)
        except LLMError as e:
            return f"{nothing}, and could not ask for a new one: {e}"[:300]
        self.emit("jd", f"new category for {name!r}: {area} {category_name!r} ({confidence:.2f}) {why}", file_id)
        if area == "none":
            return f"{nothing}, and none could be made: {why}"[:300]
        if confidence < self.cfg.new_id_min_confidence:
            return (
                f"{nothing}; unsure about a new category {category_name!r} in {area} (confidence {confidence:.2f})"
            )
        proposal = propose_new_category(self.index, area, category_name, name)
        return f"{nothing}, and none could be made: {proposal}" if isinstance(proposal, str) else proposal

    def _create_id(self, file_id: int, for_rel: str, new: NewID) -> str:
        """Create the new ID folder, re-checking right before that its number is still free.

        A new category is created first, on the same terms. Returns "" when
        created, otherwise why not (nothing was created then).
        """
        item: JDItem = new.item
        root = self.index.root
        path = root / item.rel
        category = new.new_category
        try:
            parent = root / category.area if category is not None else path.parent
            if not is_under_root(root, parent):
                raise OSError(f"{parent} is outside the target")
            if category is not None:
                if int(category.number) in used_category_numbers(self.index, category.area):
                    return f"category number {category.number} was taken meanwhile; nothing created"
                (root / category.rel).mkdir(exist_ok=False)
                self.progress.append(
                    {"action": "created_category", "category": category.number, "rel": category.rel, "for": for_rel}
                )
                self.emit("jd", f"created new category {category.folder} in {category.area}", file_id)
                self._note_run("new category", f"created {category.rel} for {for_rel}")
            elif int(item.id[3:]) in used_id_numbers(root, self.index, new.category):
                return f"ID number {item.id} was taken meanwhile; nothing created"
            path.mkdir(exist_ok=False)
        except OSError as e:
            return f"could not create new ID {item.rel}: {e}"
        self.progress.append({"action": "created_id", "jd_id": item.id, "rel": item.rel, "for": for_rel})
        self.emit("jd", f"created new ID {item.id} {item.name} in {path.parent.relative_to(root).as_posix()}", file_id)
        self._note_run("new ID", f"created {item.rel} for {for_rel}")
        self.reload_index()
        return ""

    def _stays(self, file_id: int, src_rel: str, plan: Plan, cls: Classification) -> bool:
        """Reorganize mode: keep a file that is already where it belongs.

        Loose files and inbox files are filed normally. A file inside a real
        ID stays unless the model picks a different ID with at least
        ``reorganize_min_confidence``; within its own ID it only moves from
        the ID root into an existing subfolder, never between subfolders. The
        user's own photos and videos are the exception: they always go to
        their YYYY/MM folder.
        """
        loc: Location = self.index.locate(src_rel)
        here = f"{loc.item.id} {loc.item.name}" if loc.item else ""
        if posixpath.dirname(plan.dest_rel) == posixpath.dirname(src_rel):
            self._keep(file_id, src_rel, "skipped", f"already in the right place ({here or 'as filed'})",
                       src_rel, outcome="in_place")
            return True
        if loc.item is None or (loc.item.is_inbox and plan.item.id != loc.item.id):
            return False
        if plan.item.id == loc.item.id:
            if (loc.depth == 0 and plan.subfolder) or plan.dated:
                return False
            self._keep(file_id, src_rel, "skipped", f"already filed in {here}", src_rel, outcome="in_place")
            return True
        threshold = self.cfg.reorganize_min_confidence
        if cls.confidence < threshold:
            self._keep(
                file_id,
                src_rel,
                "skipped",
                f"model would move it from {loc.item.id} to {plan.item.id} {plan.item.name}, but its "
                f"confidence {cls.confidence:.2f} is below the {threshold:.2f} needed to move a filed file",
            )
            return True
        return False

    def _confirmed(self) -> bool:
        with self._lock:
            self._awaiting = True
            self._decision_value = ""
        self._decision.clear()
        t0 = time.monotonic()
        while not self._decision.wait(0.2):
            if self.stop_event.is_set():
                break
        self._confirm_wait += time.monotonic() - t0
        with self._lock:
            self._awaiting = False
            return self._decision_value == "move"

    def _repo_in_the_way(self, src_rel: str, dest_rel: str) -> str:
        """Why this move would touch a git repository ("" when it does not).

        Repositories stay whole and where they are: nothing is moved out of
        one, around inside one, or into one. The scan already skips them;
        this is the last check before any file moves, whatever planned it
        (the model, a junk pattern, a whole folder, a date folder).
        """
        cfg = self.cfg
        repo = git_workdir(cfg.source / src_rel, stop=cfg.source)
        if repo is not None:
            where = posix_rel(str(repo.relative_to(cfg.source.resolve())))
            return f"inside the git repository {where}: repositories are kept whole, nothing in them is moved"
        repo = git_workdir(cfg.target / dest_rel, stop=cfg.target)
        if repo is not None:
            where = posix_rel(str(repo.relative_to(cfg.target.resolve())))
            return f"{dest_rel} is inside the git repository {where}: nothing is filed into a repository"
        return ""

    def _move(self, file_id: int, src_rel: str, dest_rel: str) -> None:
        blocked = self._repo_in_the_way(src_rel, dest_rel)
        if blocked:
            self._keep(file_id, src_rel, "skipped", blocked)
            return
        self.db.update(file_id, status="moving")
        try:
            actual, moved = apply_move(
                source=self.cfg.source,
                target=self.cfg.target,
                src_rel=src_rel,
                dest_rel=dest_rel,
                dry_run=self.cfg.dry_run,
            )
        except (OSError, ValueError) as e:
            self._fail(file_id, e)
            self._finish_view("error", reason=f"{type(e).__name__}: {e}")
            return
        row = self.db.get(file_id)
        if not moved:
            self.progress.append(
                {"action": "dry_run", "src_rel": src_rel, "dest_rel": actual, "jd_id": row["jd_id"] if row else None}
            )
            self.db.update(file_id, status="planned", dest_rel=actual, finished_at=utc_now_iso())
            self.emit("dry-run", f"{src_rel} → {actual}", file_id)
            self._finish_view("planned", actual)
            return
        # Durable log FIRST, then mark done.
        self.progress.append(
            {
                "action": "moved",
                "src_rel": src_rel,
                "dest_rel": actual,
                "jd_id": row["jd_id"] if row else None,
                "sha256": row["sha256"] if row else None,
                "summary": row["summary"] if row else None,
                "confidence": row["llm_confidence"] if row else None,
            }
        )
        self._mark_moved(file_id, src_rel, actual)
        self.emit("moved", f"{src_rel} → {actual}", file_id)
        self._finish_view("moved", actual)

    def _mark_moved(self, file_id: int, src_rel: str, dest_rel: str) -> None:
        self.db.update(
            file_id,
            status="done",
            dest_rel=dest_rel,
            src_rel=None,
            orig_rel=src_rel,
            abs_path=str((self.cfg.target / dest_rel).resolve()),
            finished_at=utc_now_iso(),
        )

    def _note_model(self, seconds: float) -> None:
        """Per-model answer times, so models can be compared in the TUI."""
        name = self.model_name
        with self._lock:
            stats = self._model_stats.setdefault(name, ModelStats(model=name))
            stats.files += 1
            stats.total_s += seconds
            stats.last_s = seconds

    def _note_run(self, title: str, text: str) -> None:
        if self.run_log is not None:
            self.run_log.note(title, text)

    def _fail(self, file_id: int | None, err: BaseException, *, viewed: bool = True) -> None:
        """Record an error. *viewed* is False before sorting, when no entry of the file's own follows."""
        if file_id is None:
            return
        row = self.db.get(file_id)
        src_rel = (row["src_rel"] if row and row["src_rel"] else None) or f"id={file_id}"
        msg = f"{type(err).__name__}: {err}"
        try:
            self.db.update(file_id, status="error", error=msg[:1000])
            self.progress.append({"action": "error", "src_rel": src_rel, "error": msg[:1000]})
        except Exception:
            log.exception("failed to record error")
        self.emit("error", f"{src_rel}: {msg}", file_id)
        if not viewed:
            self._note_run("error", f"{src_rel}: {msg}")

        self._release(file_id)


def _git_prune(rel_dir: str, path: Path) -> bool:
    """Never walk into a git repository: moving files out of it one by one breaks it."""
    return is_git_repo(path)


def _has_subfolders(path: Path) -> bool:
    try:
        with os.scandir(path) as it:
            return any(e.is_dir(follow_symlinks=False) and not e.name.startswith(".") for e in it)
    except OSError:
        return False


def _fit_context(cfg: SortoConfig, llm: Any) -> None:
    """Use a smaller context baked into the model tag, so the prompt fits it."""
    probe = getattr(llm, "server_context_window", None)
    ctx = probe() if probe else None
    if ctx and ctx < cfg.context_window:
        cfg.context_window = max(4096, ctx)


def make_llm(cfg: SortoConfig, *, fake: bool = False) -> Any:
    if fake:
        return FakeLLMClient()
    return OpenAICompatClient(
        base_url=cfg.llm_url,
        model=cfg.llm_model,
        api_key=cfg.llm_api_key,
        temperature=cfg.temperature,
        top_p=cfg.top_p,
        max_tokens=cfg.max_tokens,
        timeout_sec=cfg.timeout_sec,
        reasoning_effort=cfg.reasoning_effort,
        max_retries=cfg.max_retries,
    )


def describe_view(view: AnalysisView) -> str:
    """One-line plain-text summary used by headless mode."""
    size = human_size(view.size)
    where = f"{view.jd_id} {view.jd_name}".strip()
    return f"{view.src_rel} ({size}) → {where}: {view.dest_rel or view.reason}"
