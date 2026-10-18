"""Structured logs: one JSON object per line, one file per day, kept for 30 days."""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

KEEP_DAYS = 30
_PREFIX = "tracker-"
_SUFFIX = ".jsonl"
_handler: logging.Handler | None = None


class JsonLinesFormatter(logging.Formatter):
    """`extra={"fields": {...}}` on a log call adds those keys to the line."""

    def format(self, record: logging.LogRecord) -> str:
        line = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        line.update(getattr(record, "fields", {}))
        if record.exc_info:
            line["exc"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str, ensure_ascii=False)


def _prune(log_dir: Path, today: date, keep_days: int) -> None:
    oldest = today - timedelta(days=keep_days)
    for path in log_dir.glob(f"{_PREFIX}*{_SUFFIX}"):
        try:
            day = date.fromisoformat(path.name.removeprefix(_PREFIX).removesuffix(_SUFFIX))
        except ValueError:
            continue  # not one of ours
        if day < oldest:
            path.unlink()


def setup(log_dir: Path, *, now: datetime | None = None, keep_days: int = KEEP_DAYS) -> Path:
    """Send INFO and above to today's log file (appending) and delete files past `keep_days`.

    Returns the file being written. Calling it again replaces the previous handler.
    """
    global _handler
    close()
    today = (now or datetime.now(UTC)).date()
    log_dir.mkdir(parents=True, exist_ok=True)
    _prune(log_dir, today, keep_days)

    path = log_dir / f"{_PREFIX}{today.isoformat()}{_SUFFIX}"
    _handler = logging.FileHandler(path, encoding="utf-8")
    _handler.setLevel(logging.INFO)
    _handler.setFormatter(JsonLinesFormatter())
    root = logging.getLogger()
    root.addHandler(_handler)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    return path


def close() -> None:
    global _handler
    if _handler is not None:
        logging.getLogger().removeHandler(_handler)
        _handler.close()
        _handler = None
