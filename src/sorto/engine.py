from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from sorto.apply import ProgressLog, apply_move
from sorto.config import SortoConfig
from sorto.db import Database
from sorto.eta import EtaTracker
from sorto.identify import identify_file
from sorto.llm import FakeLLMClient, LLMError, LLMParseError, OpenAICompatClient
from sorto.models import (
    SUGGESTED_FOLDERS,
    AnalysisPacket,
    AnalysisView,
    Classification,
    QueueRow,
    Snapshot,
)
from sorto.plan import PlanError, existing_folder_hint, plan_destination
from sorto.scan import discover_batch
from sorto.util import estimate_tokens, posix_rel, utc_now_iso

log = logging.getLogger("sorto")


class Engine:
    def __init__(
        self,
        cfg: SortoConfig,
        *,
        llm: Any | None = None,
        db: Database | None = None,
    ):
        self.cfg = cfg
        self.db = db or Database(cfg.db_path)
        self.progress = ProgressLog(cfg.progress_path)
        self.llm = llm or OpenAICompatClient(
            base_url=cfg.llm_url,
            model=cfg.llm_model,
            api_key=cfg.llm_api_key,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            timeout_sec=cfg.timeout_sec,
        )
        self.eta = EtaTracker()
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.finished = threading.Event()
        self.identify_q: queue.Queue[int | None] = queue.Queue()
        self.analyze_q: queue.Queue[int | None] = queue.Queue()
        self.move_q: queue.Queue[tuple[int, str] | None] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._log_lines: deque[str] = deque(maxlen=200)
        self._log_seq = 0
        self._current_identify: str | None = None
        self._current_analyze: str | None = None
        self._current_move: str | None = None
        self._last_analysis: AnalysisView | None = None
        self._tokens_est = 0
        self._llm_ok = True
        self._llm_error: str | None = None
        self._scan_complete_pass = False
        self._scan_running = True
        self._started = time.monotonic()
        self._inflight = 0
        self._times: dict[int, dict[str, float]] = {}
        self._enqueued: set[int] = set()
        self._kick_counts: dict[int, int] = {}
        self._system_prompt = self._load_prompt()
        self._folders = list(SUGGESTED_FOLDERS)

    def _load_prompt(self) -> str:
        path = self.cfg.prompt_path
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            from sorto.config import packaged_prompt

            return packaged_prompt()

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

    def _inc_inflight(self) -> None:
        with self._lock:
            self._inflight += 1

    def _dec_inflight(self) -> None:
        with self._lock:
            self._inflight = max(0, self._inflight - 1)

    def _mark_enqueued(self, file_id: int) -> bool:
        with self._lock:
            if file_id in self._enqueued:
                return False
            self._enqueued.add(file_id)
            return True

    def _unmark(self, file_id: int) -> None:
        with self._lock:
            self._enqueued.discard(file_id)

    def request_stop(self) -> None:
        self.stop_event.set()
        self.pause_event.clear()
        for q in (self.identify_q, self.analyze_q, self.move_q):
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
        self.emit("engine", f"mode={'DRY' if value else 'LIVE'}")
        return True

    @staticmethod
    def _queue_busy(q: queue.Queue) -> bool:
        with q.mutex:
            return bool(q.unfinished_tasks)

    def is_idle(self) -> bool:
        with self._lock:
            inflight = self._inflight
            current = self._current_identify or self._current_analyze or self._current_move
        if (
            inflight
            or current
            or self._queue_busy(self.identify_q)
            or self._queue_busy(self.analyze_q)
            or self._queue_busy(self.move_q)
        ):
            return False
        counts = self.db.counts()
        if self.cfg.dry_run:
            return (counts.discovered + counts.identifying + counts.analyzing + counts.moving) == 0
        return counts.pending == 0

    def snapshot(self) -> Snapshot:
        counts = self.db.counts()
        dry = self.cfg.dry_run
        completed = counts.done + counts.skipped + counts.error + counts.needs_user
        if dry:
            completed += counts.planned
            pending = counts.discovered + counts.identifying + counts.analyzing + counts.moving
        else:
            pending = counts.pending
        # Denominator = files discovered so far that need investigation.
        need = completed + pending
        pct = (100.0 * completed / need) if need else 0.0
        with self._lock:
            logs = list(self._log_lines)
            seq = self._log_seq
            last = self._last_analysis
            scan_running = self._scan_running and not self._scan_complete_pass
            if self.cfg.follow:
                scan_state = "live" if not self._scan_complete_pass else "complete, watching"
            else:
                scan_state = "live" if not self._scan_complete_pass else "complete, draining queue"
            snap = Snapshot(
                root=str(self.cfg.root),
                model=getattr(self.llm, "model", None) or self.cfg.llm_model,
                mode="DRY" if dry else "LIVE",
                scan_state=scan_state,
                counts=counts,
                current_identify=self._current_identify,
                current_analyze=self._current_analyze,
                current_move=self._current_move,
                last_analysis=last,
                log_lines=logs,
                log_seq=seq,
                llm_ok=self._llm_ok,
                llm_latency_s=getattr(self.llm, "last_latency_s", None),
                llm_error=self._llm_error,
                progress_pct=pct,
                elapsed_s=time.monotonic() - self._started,
                eta_s=self.eta.eta_seconds(pending),
                paused=self.pause_event.is_set(),
                follow=self.cfg.follow,
                scan_still_running=scan_running,
                tokens_est=self._tokens_est,
                finished=self.finished.is_set(),
            )
        counts.pending = pending
        recent = self.db.recent(12)
        snap.queue_rows = [QueueRow(status=str(r["status"]), src_rel=str(r["src_rel"])) for r in recent]
        return snap

    def recover(self) -> None:
        """Reset in-flight rows after a crash; requeue work."""
        for file_id in self.db.ids_by_status("moving"):
            row = self.db.get(file_id)
            if not row:
                continue
            src = self.cfg.root / (row["src_rel"] or "")
            dest_rel = row["dest_rel"]
            dest = self.cfg.root / str(dest_rel) if dest_rel else None
            if not src.exists() and dest is not None and dest.exists():
                self.progress.append(
                    {
                        "action": "moved",
                        "src_rel": row["src_rel"],
                        "dest_rel": dest_rel,
                        "recovered": True,
                    }
                )
                self.db.update(
                    file_id,
                    status="done",
                    finished_at=utc_now_iso(),
                    abs_path=str(dest.resolve()),
                    src_rel=str(dest_rel),
                )
                self.emit("recover", f"completed interrupted move → {dest_rel}", file_id)
                continue
            self.db.update(file_id, status="planned" if dest_rel else "discovered")
            self.emit("recover", f"reset moving {row['src_rel']}", file_id)
        if self.cfg.retry_errors:
            for file_id in self.db.ids_by_status("error"):
                self.db.update(file_id, status="discovered", error=None)
        for file_id in self.db.ids_by_status("identifying", "analyzing"):
            self.db.update(file_id, status="discovered")
        for file_id in self.db.ids_by_status("discovered"):
            if self._mark_enqueued(file_id):
                self.identify_q.put(file_id)
        for file_id in self.db.ids_by_status("planned"):
            if self.cfg.dry_run:
                continue
            row = self.db.get(file_id)
            dest = str(row["dest_rel"]) if row and row["dest_rel"] else ""
            if not dest:
                self.db.update(file_id, status="discovered")
                if self._mark_enqueued(file_id):
                    self.identify_q.put(file_id)
                continue
            if self._mark_enqueued(file_id):
                self.move_q.put((file_id, dest))

    def start(self) -> None:
        self.recover()
        self._started = time.monotonic()
        self.emit("engine", f"start root={self.cfg.root} mode={'DRY' if self.cfg.dry_run else 'LIVE'}")
        t_scan = threading.Thread(target=self._scan_loop, name="sorto-scan", daemon=True)
        self._threads.append(t_scan)
        t_scan.start()
        for i in range(self.cfg.identify_workers):
            t = threading.Thread(target=self._identify_loop, name=f"sorto-id-{i}", daemon=True)
            self._threads.append(t)
            t.start()
        for i in range(self.cfg.workers):
            t = threading.Thread(target=self._analyze_loop, name=f"sorto-llm-{i}", daemon=True)
            self._threads.append(t)
            t.start()
        t_move = threading.Thread(target=self._move_loop, name="sorto-move", daemon=True)
        self._threads.append(t_move)
        t_move.start()

    def join(self, timeout: float | None = None) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        for t in self._threads:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            t.join(timeout=remaining)
        try:
            self.progress.close()
        except Exception:
            pass

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

    def _wait_if_paused(self) -> None:
        while self.pause_event.is_set() and not self.stop_event.is_set():
            time.sleep(0.1)

    def _scan_loop(self) -> None:
        try:
            while not self.stop_event.is_set():
                self._wait_if_paused()
                if self.stop_event.is_set():
                    break
                with self._lock:
                    self._scan_running = True
                new_count = self._scan_pass()
                with self._lock:
                    self._scan_complete_pass = new_count == 0
                    self._scan_running = False
                if not self.cfg.follow:
                    self._wait_drained()
                    if new_count == 0 and self.is_idle() and not self.stop_event.is_set():
                        # Confirm with a second empty pass.
                        extra = self._scan_pass()
                        if extra == 0 and self.is_idle():
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

    def _kick_stuck(self) -> None:
        """Requeue DB rows that have no live queue item (lost after a sentinel/crash)."""
        if self.cfg.dry_run:
            return
        if (
            self._queue_busy(self.identify_q)
            or self._queue_busy(self.analyze_q)
            or self._queue_busy(self.move_q)
        ):
            return
        with self._lock:
            if self._inflight:
                return
        for file_id in self.db.ids_by_status("planned"):
            if self._too_many_kicks(file_id):
                continue
            row = self.db.get(file_id)
            dest = (row["dest_rel"] or "") if row else ""
            if dest:
                self.move_q.put((file_id, dest))
            else:
                self.db.update(file_id, status="discovered")
                self.identify_q.put(file_id)
        for file_id in self.db.ids_by_status("discovered"):
            if self._too_many_kicks(file_id):
                continue
            self.identify_q.put(file_id)
        for file_id in self.db.ids_by_status("identifying", "analyzing"):
            if self._too_many_kicks(file_id):
                continue
            self.db.update(file_id, status="discovered")
            self.identify_q.put(file_id)

    def _too_many_kicks(self, file_id: int) -> bool:
        n = self._kick_counts.get(file_id, 0) + 1
        self._kick_counts[file_id] = n
        if n > 8:
            self.db.update(file_id, status="error", error="stuck in queue; retried too many times")
            self._unmark(file_id)
            return True
        return False

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
            self._kick_stuck()
            time.sleep(0.1)

    def _scan_pass(self) -> int:
        new_work = 0
        try:
            self._folders = existing_folder_hint(self.cfg.root) + self.db.top_level_folders(self.cfg.root)
            # de-dupe preserve order
            seen: set[str] = set()
            folders: list[str] = []
            for f in self._folders:
                if f not in seen:
                    seen.add(f)
                    folders.append(f)
            self._folders = folders
        except OSError as e:
            self.emit("error", f"folder listing: {e}")
        batch = 0
        for path, rel, size, mtime_ns, dev, ino in discover_batch(
            self.cfg.root, self.cfg.include, self.cfg.exclude
        ):
            if self.stop_event.is_set():
                break
            try:
                file_id, is_new = self.db.upsert_discovered(
                    src_rel=rel,
                    abs_path=str(path),
                    size=size,
                    mtime_ns=mtime_ns,
                    dev=dev,
                    ino=ino,
                )
            except OSError as e:
                self.emit("error", f"db discover {rel}: {e}")
                continue
            if is_new:
                new_work += 1
                if self._mark_enqueued(file_id):
                    self.identify_q.put(file_id)
            batch += 1
            if batch % 250 == 0:
                time.sleep(0)  # yield
        if new_work:
            self.emit("scan", f"discovered {new_work} new/changed file(s)")
        return new_work

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
            if item is None:
                self.identify_q.task_done()
                continue
            try:
                self._identify_one(item)
            except Exception as e:
                log.exception("identify failed")
                self._fail(item, e)
            finally:
                self.identify_q.task_done()

    def _identify_one(self, file_id: int) -> None:
        row = self.db.get(file_id)
        if not row:
            self._unmark(file_id)
            return
        src_rel = row["src_rel"] or ""
        path = Path(row["abs_path"] or (self.cfg.root / src_rel))
        self._inc_inflight()
        t0 = time.monotonic()
        with self._lock:
            self._current_identify = src_rel
        try:
            self.db.update(file_id, status="identifying")
            if not path.is_file():
                self.db.update(file_id, status="error", error="source missing")
                self.progress.append({"action": "error", "src_rel": src_rel, "error": "source missing"})
                self.emit("error", f"missing {src_rel}", file_id)
                self._unmark(file_id)
                return
            packet = identify_file(
                path,
                src_rel,
                self.cfg,
                top_level_folders=self._folders,
                size=row["size"],
                mtime_ns=row["mtime_ns"],
            )
            dup_of = None
            if packet.sha256:
                other = self.db.get_by_sha(packet.sha256)
                if other and int(other["id"]) != file_id and other["src_rel"] != src_rel:
                    dup_of = str(other["src_rel"])
                    packet.duplicate_of = dup_of
            self.db.update(
                file_id,
                mime=packet.mime,
                type_guess=packet.type_guess,
                sha256=packet.sha256,
            )
            dt = time.monotonic() - t0
            self._times.setdefault(file_id, {})["identify"] = dt
            self.analyze_q.put(file_id)
            self.emit("identify", f"{src_rel} mime={packet.mime or '?'} {dt:.2f}s", file_id)
            # stash packet on the times dict
            self._times[file_id]["packet"] = packet  # type: ignore[assignment]
        except OSError as e:
            self._fail(file_id, e)
        finally:
            with self._lock:
                if self._current_identify == src_rel:
                    self._current_identify = None
            self._dec_inflight()

    def _cache_key(self, packet: AnalysisPacket) -> str:
        if packet.sha256:
            return f"sha:{packet.sha256}:{packet.dest_scheme}:{packet.filename}:{packet.mime}"
        return (
            f"meta:{packet.size}:{packet.mtime_ns}:{packet.filename}:"
            f"{packet.mime}:{packet.dest_scheme}"
        )

    def _analyze_loop(self) -> None:
        while True:
            if self.pause_event.is_set() and not self.stop_event.is_set():
                time.sleep(0.1)
                continue
            try:
                item = self.analyze_q.get(timeout=0.2)
            except queue.Empty:
                if self.stop_event.is_set():
                    break
                continue
            if item is None:
                self.analyze_q.task_done()
                continue
            try:
                self._analyze_one(item)
            except Exception as e:
                log.exception("analyze failed")
                self._fail(item, e)
            finally:
                self.analyze_q.task_done()

    def _analyze_one(self, file_id: int) -> None:
        row = self.db.get(file_id)
        if not row:
            self._unmark(file_id)
            return
        src_rel = row["src_rel"] or ""
        packet: AnalysisPacket | None = self._times.get(file_id, {}).get("packet")  # type: ignore[assignment]
        if packet is None:
            path = Path(row["abs_path"] or (self.cfg.root / src_rel))
            try:
                packet = identify_file(
                    path, src_rel, self.cfg, top_level_folders=self._folders
                )
            except OSError as e:
                self._fail(file_id, e)
                return
        self._inc_inflight()
        t0 = time.monotonic()
        with self._lock:
            self._current_analyze = src_rel
            self._tokens_est = estimate_tokens(json.dumps(packet.to_llm_dict()))
        try:
            self.db.update(file_id, status="analyzing")
            cached = self.db.cache_get(self._cache_key(packet))
            cls: Classification | None = None
            if cached:
                try:
                    data = json.loads(cached)
                    cls = Classification(
                        label=str(data.get("label") or "unknown"),
                        confidence=float(data.get("confidence") or 0.0),
                        dest_rel=str(data.get("dest_rel") or ""),
                        rename=bool(data.get("rename", False)),
                        reason=str(data.get("reason") or ""),
                        needs_user=bool(data.get("needs_user", False)),
                        raw=cached,
                    )
                except Exception:
                    cls = None
            if cls is None:
                try:
                    cls = self.llm.classify(packet, self._system_prompt)
                    self._llm_ok = True
                    self._llm_error = None
                    cache_obj = {
                        "label": cls.label,
                        "confidence": cls.confidence,
                        "dest_rel": cls.dest_rel,
                        "rename": cls.rename,
                        "reason": cls.reason,
                        "needs_user": cls.needs_user,
                    }
                    self.db.cache_put(self._cache_key(packet), json.dumps(cache_obj))
                except LLMParseError as e:
                    self._llm_ok = True
                    self._llm_error = str(e)
                    self.db.update(file_id, status="error", error=f"invalid LLM JSON: {e}")
                    self.progress.append(
                        {"action": "error", "src_rel": src_rel, "error": "invalid LLM JSON"}
                    )
                    self.emit("error", f"invalid JSON for {src_rel}", file_id)
                    self._unmark(file_id)
                    return
                except LLMError as e:
                    self._llm_ok = False
                    self._llm_error = str(e)
                    self.emit("llm", f"blocked: {e}")
                    # Retry later: put back discovered
                    self.db.update(file_id, status="discovered")
                    self._unmark(file_id)
                    time.sleep(min(15.0, max(1.0, getattr(self.cfg, "scan_interval", 5))))
                    if not self.stop_event.is_set() and self._mark_enqueued(file_id):
                        self.identify_q.put(file_id)
                    return
            dt = time.monotonic() - t0
            self._times.setdefault(file_id, {})["analyze"] = dt
            view = AnalysisView(
                src_rel=src_rel,
                filename=packet.filename,
                mime=packet.mime or "",
                size=packet.size,
                label=cls.label,
                confidence=cls.confidence,
                dest_rel=cls.dest_rel,
                reason=cls.reason,
                tokens=getattr(self.llm, "last_tokens_est", self._tokens_est) or 0,
                latency_s=getattr(self.llm, "last_latency_s", dt) or dt,
                stage="analyze",
            )
            with self._lock:
                self._last_analysis = view
            self.db.update(
                file_id,
                llm_label=cls.label,
                llm_confidence=cls.confidence,
                llm_reason=cls.reason,
                analyzed_at=utc_now_iso(),
                rename=1 if cls.rename else 0,
            )
            if cls.needs_user and not self.cfg.yes:
                self.db.update(file_id, status="needs_user", dest_rel=cls.dest_rel or None)
                self.progress.append(
                    {
                        "action": "needs_user",
                        "src_rel": src_rel,
                        "reason": cls.reason,
                        "dest_rel": cls.dest_rel,
                    }
                )
                self.emit("needs_user", f"{src_rel}: {cls.reason}", file_id)
                self._unmark(file_id)
                return
            try:
                dest_rel = plan_destination(
                    self.cfg.root,
                    packet,
                    cls,
                    allow_extension_fix=self.cfg.allow_extension_fix,
                )
            except PlanError as e:
                dest_rel = f"_unsorted/{packet.filename}"
                try:
                    dest_rel = plan_destination(
                        self.cfg.root,
                        packet,
                        Classification(
                            label="_unsorted",
                            confidence=0.0,
                            dest_rel=dest_rel,
                            rename=False,
                            reason=str(e),
                            needs_user=False,
                        ),
                        allow_extension_fix=False,
                    )
                except PlanError as e2:
                    self._fail(file_id, e2)
                    return
            src_path = Path(row["abs_path"] or (self.cfg.root / src_rel)).resolve()
            dest_path = (self.cfg.root / dest_rel).resolve()
            if src_path == dest_path:
                self.db.update(file_id, status="done", dest_rel=dest_rel, finished_at=utc_now_iso())
                self.progress.append(
                    {"action": "skipped", "src_rel": src_rel, "reason": "already_in_place"}
                )
                self.emit("skip", f"{src_rel} already in place", file_id)
                self._note_times(file_id, moved=False)
                self._unmark(file_id)
                return
            self.db.update(file_id, status="planned", dest_rel=dest_rel)
            with self._lock:
                if self._last_analysis:
                    self._last_analysis.dest_rel = dest_rel
            self.move_q.put((file_id, dest_rel))
            self.emit("plan", f"{src_rel} → {dest_rel} ({cls.label} {cls.confidence:.2f})", file_id)
        finally:
            with self._lock:
                if self._current_analyze == src_rel:
                    self._current_analyze = None
            self._dec_inflight()

    def _move_loop(self) -> None:
        while True:
            if self.pause_event.is_set() and not self.stop_event.is_set():
                time.sleep(0.1)
                continue
            try:
                item = self.move_q.get(timeout=0.2)
            except queue.Empty:
                if self.stop_event.is_set():
                    break
                continue
            if item is None:
                self.move_q.task_done()
                continue
            file_id, queued_dest = item
            try:
                self._move_one(file_id, queued_dest)
            except Exception as e:
                log.exception("move failed")
                self._fail(file_id, e)
            finally:
                self.move_q.task_done()

    def _move_one(self, file_id: int, queued_dest: str = "") -> None:
        row = self.db.get(file_id)
        if not row:
            self._unmark(file_id)
            return
        src_rel = row["src_rel"] or ""
        dest_rel = row["dest_rel"] or queued_dest or ""
        abs_path = row["abs_path"] or ""
        if not src_rel and abs_path:
            try:
                src_rel = posix_rel(
                    str(Path(abs_path).resolve().relative_to(self.cfg.root.resolve()))
                )
            except (OSError, ValueError):
                src_rel = ""
        if not dest_rel:
            self._fail(file_id, RuntimeError("missing dest_rel"))
            return
        if not src_rel:
            if abs_path:
                src_rel = posix_rel(Path(abs_path).name)
            else:
                self.db.update(file_id, status="discovered", error=None)
                self.identify_q.put(file_id)
                self._unmark(file_id)
                return
        self._inc_inflight()
        t0 = time.monotonic()
        with self._lock:
            self._current_move = src_rel
        try:
            self.db.update(file_id, status="moving")
            src_path = self.cfg.root / src_rel
            dest_path = self.cfg.root / dest_rel
            if not src_path.exists() and dest_path.exists():
                self.progress.append(
                    {"action": "moved", "src_rel": src_rel, "dest_rel": dest_rel, "recovered": True}
                )
                self.db.update(
                    file_id,
                    status="done",
                    dest_rel=dest_rel,
                    src_rel=dest_rel,
                    abs_path=str(dest_path.resolve()),
                    finished_at=utc_now_iso(),
                )
                self.emit("moved", f"{src_rel} → {dest_rel} (already at dest)", file_id)
                self._unmark(file_id)
                return
            try:
                actual, moved = apply_move(
                    root=self.cfg.root,
                    src_rel=src_rel,
                    dest_rel=dest_rel,
                    dry_run=self.cfg.dry_run,
                )
            except (OSError, ValueError) as e:
                self._fail(file_id, e)
                return
            dt = time.monotonic() - t0
            self._times.setdefault(file_id, {})["move"] = dt
            if self.cfg.dry_run:
                # Investigation complete without moving.
                self.progress.append(
                    {
                        "action": "dry_run",
                        "src_rel": src_rel,
                        "dest_rel": actual,
                        "label": row["llm_label"],
                        "confidence": row["llm_confidence"],
                    }
                )
                self.db.update(
                    file_id,
                    status="planned",
                    dest_rel=actual,
                    finished_at=utc_now_iso(),
                )
                self.emit("dry-run", f"{src_rel} → {actual}", file_id)
                self._note_times(file_id, moved=False)
                self._unmark(file_id)
                return
            # Durable log FIRST, then mark done.
            self.progress.append(
                {
                    "action": "moved",
                    "src_rel": src_rel,
                    "dest_rel": actual,
                    "sha256": row["sha256"],
                    "label": row["llm_label"],
                    "confidence": row["llm_confidence"],
                }
            )
            new_abs = str((self.cfg.root / actual).resolve())
            self.db.update(
                file_id,
                status="done",
                dest_rel=actual,
                src_rel=actual,
                abs_path=new_abs,
                finished_at=utc_now_iso(),
            )
            self.emit("moved", f"{src_rel} → {actual}", file_id)
            with self._lock:
                if self._last_analysis and self._last_analysis.src_rel == src_rel:
                    self._last_analysis.dest_rel = actual
            self._note_times(file_id, moved=True)
            self._unmark(file_id)
        finally:
            with self._lock:
                if self._current_move == src_rel:
                    self._current_move = None
            self._dec_inflight()

    def _note_times(self, file_id: int, moved: bool) -> None:
        t = self._times.pop(file_id, {})
        ident = float(t.get("identify", 0.0) or 0.0)
        analyze = float(t.get("analyze", 0.0) or 0.0)
        move = float(t.get("move", 0.0) or 0.0) if moved else 0.0
        self.eta.add(ident, analyze, move)

    def _fail(self, file_id: int, err: BaseException) -> None:
        row = self.db.get(file_id)
        src_rel = (row["src_rel"] if row and row["src_rel"] else None) or f"id={file_id}"
        msg = f"{type(err).__name__}: {err}"
        try:
            self.db.update(file_id, status="error", error=msg[:1000])
            self.progress.append({"action": "error", "src_rel": src_rel, "error": msg[:1000]})
        except Exception:
            log.exception("failed to record error")
        self.emit("error", f"{src_rel}: {msg}", file_id)
        self._times.pop(file_id, None)
        self._unmark(file_id)


def make_llm(cfg: SortoConfig, *, fake: bool = False) -> Any:
    if fake:
        return FakeLLMClient()
    return OpenAICompatClient(
        base_url=cfg.llm_url,
        model=cfg.llm_model,
        api_key=cfg.llm_api_key,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
        timeout_sec=cfg.timeout_sec,
    )
