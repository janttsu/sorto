from __future__ import annotations

import os
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from sorto.config import (
    SortoConfig,
    config_for_model,
    grok_config_path,
    load_config,
    validate_roots,
)
from sorto.jd import scan_jd
from sorto.llm import NotLocalError, OpenAICompatClient, ensure_local_url
from sorto.rules import load_junk_rules, load_rules
from sorto.util import is_under_root


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


OPTIONAL_BINS = ("file", "exiftool", "ffprobe", "mediainfo", "pdfinfo", "pdftotext", "identify")


def run_doctor(
    source: Path | None,
    target: Path | None,
    *,
    llm_url: str | None = None,
    llm_model: str | None = None,
    warm: bool = True,
) -> list[Check]:
    checks: list[Check] = []
    cfg: SortoConfig | None = None
    if source is not None and target is not None:
        source = Path(source).expanduser().resolve()
        target = Path(target).expanduser().resolve()
        try:
            validate_roots(source, target)
            checks.append(Check("source/target", True, f"{source} → {target}"))
        except ValueError as e:
            checks.append(Check("source/target", False, str(e)))
            return checks
        checks.append(Check("source writable", os.access(source, os.W_OK | os.X_OK), str(source)))
        checks.append(Check("target writable", os.access(target, os.W_OK | os.X_OK), str(target)))
        index = scan_jd(target, exclude=[source] if is_under_root(target, source) else [])
        checks.append(
            Check(
                "Johnny.Decimal IDs",
                len(index) > 0,
                f"{len(index)} IDs in {len(index.categories)} categories"
                + (f", {sum(1 for i in index.items.values() if i.description)} with JDex notes" if len(index) else ""),
            )
        )
        cfg = load_config(source, target)
        if cfg.reorganize:
            checks.append(
                Check("mode", True, f"reorganize {target} in place (depth {cfg.reorganize_depth}, "
                      f"moving a filed file needs confidence {cfg.reorganize_min_confidence:.2f})")
            )
        rules = load_rules(cfg.rules_file)
        unknown = [i for i in rules.ids if index.get(i) is None]
        if not rules:
            checks.append(Check("rules", True, f"none ({cfg.rules_file}; `sorto rules` creates a template)"))
        else:
            checks.append(
                Check(
                    "rules",
                    not unknown,
                    f"{rules.line_count} line(s) in {rules.path}"
                    + (f"; IDs not in the target: {', '.join(unknown)}" if unknown else "")
                    + ("; truncated to fit the prompt" if rules.truncated else ""),
                )
            )
        junk = load_junk_rules(cfg.junk_file)
        junk_unknown = [i for i in junk.ids if index.get(i) is None]
        if not junk:
            checks.append(
                Check("junk rules", not junk.problems, f"none ({cfg.junk_file})" + "".join(f"; {p}" for p in junk.problems))
            )
        else:
            checks.append(
                Check(
                    "junk rules",
                    not junk.problems and not junk_unknown,
                    f"{len(junk.rules)} pattern(s) → {junk.into or 'per pattern'} in {junk.path}"
                    + (f"; IDs not in the target: {', '.join(junk_unknown)}" if junk_unknown else "")
                    + ("; " + "; ".join(junk.problems) if junk.problems else ""),
                )
            )
        try:
            cfg.state.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(cfg.db_path))
            conn.execute("PRAGMA user_version")
            conn.close()
            checks.append(Check("state", True, str(cfg.state)))
        except Exception as e:
            checks.append(Check("state", False, str(e)))
    else:
        cfg = load_config(Path.cwd(), Path.cwd())
    url = llm_url or cfg.llm_url
    model = llm_model or cfg.llm_model
    checks.append(Check("LLM settings", True, f"{cfg.llm_profile_source}; context {cfg.context_window}"))
    if not grok_config_path().is_file():
        checks.append(Check("grok config", True, f"{grok_config_path()} not found (using sorto defaults)"))
    try:
        ensure_local_url(url)
        checks.append(Check("LLM is local", True, url))
    except NotLocalError as e:
        checks.append(Check("LLM is local", False, str(e)))
        return checks
    # keep_alive "": a check is not a run, so it does not ask the server to hold the model
    client = OpenAICompatClient(base_url=url, model=model, timeout_sec=8.0, api=cfg.llm_api, keep_alive="")
    ok, detail = client.health()
    checks.append(Check("LLM model", ok, f"{model} @ {url} — {detail}"))
    if ok and client.is_ollama():
        held = {"run": f"loaded while sorto runs, then {cfg.keep_alive_after or '5m'}", "": "the server's own timeout"}
        checks.append(Check("LLM API", True, f"Ollama's own /api/chat; keep_alive: {held.get(cfg.keep_alive, cfg.keep_alive)}"))
    elif ok:
        checks.append(Check("LLM API", True, "OpenAI-compatible /v1 (keep_alive is the server's own)"))
    models = list(dict.fromkeys([model, *cfg.models]))
    if warm and ok:
        checks.extend(warm_models(cfg, models, default=model, url_override=llm_url))
    for name in OPTIONAL_BINS:
        path = shutil.which(name)
        checks.append(Check(f"bin:{name}", path is not None, path or "not installed (optional)"))
    return checks


def warm_models(
    cfg: SortoConfig, models: list[str], *, default: str, url_override: str | None = None
) -> list[Check]:
    """Load every switchable model once and check it answers; end on the default.

    Each model is loaded with a one-token request (so a later switch in the
    TUI is known to work) and the load time is reported. The default model is
    loaded last so a run that starts now finds it already in memory.
    """
    checks: list[Check] = []
    order = [m for m in models if m != default] + [default]
    timings: dict[str, float] = {}
    for name in order:
        mcfg = config_for_model(cfg, name)
        url = url_override or mcfg.llm_url
        try:
            client = OpenAICompatClient(base_url=url, model=name, timeout_sec=600.0, max_retries=0, keep_alive="")
        except NotLocalError as e:
            checks.append(Check(f"model {name}", False, str(e)))
            continue
        ok, detail = client.health()
        if not ok:
            optional = name != default and "not pulled" in detail
            note = "; optional, only for switching with m (see README: two models)" if optional else ""
            checks.append(Check(f"model {name}", optional, detail + note))
            continue
        t0 = time.monotonic()
        ok, detail = client.warm_up("You are a file archivist. Reply with JSON.")
        timings[name] = time.monotonic() - t0
        ctx = client.server_context_window() or mcfg.context_window
        loaded = client.loaded_models().get(name, {})
        vram = loaded.get("size_vram", 0) / 1e9
        total = loaded.get("size", 0) / 1e9
        where = f", {vram:.1f} of {total:.1f} GB on the GPU" if total else ""
        checks.append(
            Check(
                f"model {name}",
                ok,
                f"ready: loaded and answered in {timings[name]:.1f}s (context {ctx}{where})" if ok else detail,
            )
        )
    if len(order) > 1 and timings:
        probe = OpenAICompatClient(base_url=url_override or cfg.llm_url, model=default, timeout_sec=8.0, keep_alive="")
        resident = [m for m in order if m in probe.loaded_models()]
        if len(resident) == len(order):
            checks.append(Check("models in memory", True, f"all {len(order)} stay loaded; switching is instant"))
        else:
            slowest = max(timings.items(), key=lambda kv: kv[1])
            checks.append(
                Check(
                    "models in memory",
                    True,
                    f"only {', '.join(resident) or 'none'} stays loaded (the GPU/RAM or "
                    f"OLLAMA_MAX_LOADED_MODELS holds one at a time); every model loads and answers, "
                    f"a switch reloads in up to {slowest[1]:.0f}s ({slowest[0]})",
                )
            )
    return checks


def format_report(checks: list[Check]) -> str:
    lines = ["sorto doctor"]
    width = max(len(c.name) for c in checks) if checks else 10
    for c in checks:
        mark = "OK" if c.ok else "FAIL"
        lines.append(f"  [{mark:4}] {c.name:<{width}}  {c.detail}")
    failed = sum(1 for c in checks if not c.ok)
    lines.append("")
    lines.append(f"{len(checks) - failed}/{len(checks)} checks passed")
    return "\n".join(lines)
