"""Structured logging setup for NovaAgent."""

from __future__ import annotations

import json
import logging
import sys
import threading
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s :: %(message)s"
_configured = False


def setup_logging(
    level: str = "INFO",
    log_dir: str | None = None,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    """Configure root 'nova' logger with console + optional rotating file output.

    File logs rotate at `max_bytes` and keep `backup_count` previous files,
    so long-running servers won't grow a single unbounded file.
    """
    global _configured
    logger = logging.getLogger("nova")
    if _configured:
        logger.setLevel(level.upper())
        return

    logger.setLevel(level.upper())
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter(_FORMAT, datefmt="%H:%M:%S"))
    logger.addHandler(console)

    if log_dir:
        d = Path(log_dir)
        if not d.is_absolute():
            d = _PROJECT_ROOT / d
        d.mkdir(parents=True, exist_ok=True)
        logfile = d / f"nova-{datetime.now(UTC):%Y%m%d}.log"
        fh = RotatingFileHandler(
            logfile, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        fh.setFormatter(logging.Formatter(_FORMAT))
        logger.addHandler(fh)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"nova.{name}")


class JsonlTraceWriter:
    """Append-only JSONL trace writer: one JSON object per agent step/event.

    Thread-safe: whether the writes come from CLI or from concurrent web
    sessions, each record is emitted as a single atomic line (locked)."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._lock = threading.Lock()

    def write(self, event_type: str, payload: dict) -> None:
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "type": event_type,
            **payload,
        }
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
