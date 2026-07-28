"""Logging: a human-readable log file + a structured JSONL event stream.

Every scraping step emits an event with the fields required by the spec
(start/end, source, carrier, o, d, cabin, retry, error, elapsed, total).
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()


def setup_logging(logs_dir: Path, level: str = "INFO", run_id: str = "") -> logging.Logger:
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("bfs")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S"
    )
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    fh = logging.FileHandler(logs_dir / f"run_{run_id}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


class EventLog:
    """Append-only JSONL structured event log (thread-safe enough for asyncio)."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # truncate/create
        self.path.write_text("", encoding="utf-8")

    def emit(self, event: str, **fields: Any) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                  "event": event, **fields}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _LOCK:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
