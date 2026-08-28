from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from sorto.config import SortoConfig, load_config
from sorto.llm import OpenAICompatClient


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


OPTIONAL_BINS = ("file", "exiftool", "mediainfo", "ffprobe", "pdfinfo", "identify")


def run_doctor(root: Path | None, *, llm_url: str | None = None, llm_model: str | None = None) -> list[Check]:
    checks: list[Check] = []
    cfg: SortoConfig | None = None
    if root is not None:
        root = Path(root).expanduser().resolve()
        if not root.exists():
            checks.append(Check("root exists", False, str(root)))
            return checks
        if not root.is_dir():
            checks.append(Check("root is directory", False, str(root)))
            return checks
        writable = os_writable(root)
        checks.append(Check("root writable", writable, str(root)))
        try:
            cfg = load_config(root)
            checks.append(Check("state dir", True, str(cfg.state)))
        except Exception as e:
            checks.append(Check("state dir", False, str(e)))
            cfg = None
        if cfg:
            try:
                conn = sqlite3.connect(str(cfg.db_path))
                conn.execute("CREATE TABLE IF NOT EXISTS _doctor (x INTEGER)")
                conn.execute("INSERT INTO _doctor VALUES (1)")
                conn.execute("DROP TABLE _doctor")
                conn.commit()
                conn.close()
                checks.append(Check("sqlite", True, str(cfg.db_path)))
            except Exception as e:
                checks.append(Check("sqlite", False, str(e)))
    url = llm_url or (cfg.llm_url if cfg else "http://127.0.0.1:11434/v1")
    model = llm_model or (cfg.llm_model if cfg else "qwen2.5-coder:14b")
    client = OpenAICompatClient(base_url=url, model=model, timeout_sec=8.0)
    ok, detail = client.health()
    checks.append(Check("LLM endpoint", ok, f"{url} — {detail}"))
    if ok:
        try:
            models = client.list_models()
            if models:
                present = model in models
                extra = ", ".join(models[:8]) + ("…" if len(models) > 8 else "")
                checks.append(
                    Check(
                        "LLM model",
                        present or True,  # listing may omit tags; still informational
                        f"{model}" + ("" if present else f" (not listed; available: {extra})"),
                    )
                )
            else:
                checks.append(Check("LLM model", True, f"{model} (server did not list models)"))
        except Exception as e:
            checks.append(Check("LLM model list", False, str(e)))
    for name in OPTIONAL_BINS:
        path = shutil.which(name)
        checks.append(Check(f"bin:{name}", path is not None, path or "not installed (optional)"))
    return checks


def os_writable(path: Path) -> bool:
    try:
        probe = path / ".sorto-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


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
