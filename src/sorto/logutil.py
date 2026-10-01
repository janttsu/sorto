from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(log_path: Path, level: str = "INFO") -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    numeric = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger("sorto")
    root.setLevel(numeric)
    root.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s")
    fh = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(numeric)
    root.addHandler(fh)
    # Keep library loggers quieter
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
