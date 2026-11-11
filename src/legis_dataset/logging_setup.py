"""Logging: JSON lines to a per-run file, only warnings to the console."""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import orjson
from rich.console import Console
from rich.logging import RichHandler


class JsonLinesFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "time": dt.datetime.fromtimestamp(record.created, dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return orjson.dumps(payload).decode()


def configure_logging(logs_dir: Path, console: Console, *, verbose: bool = False) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"run-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(JsonLinesFormatter())
    file_handler.setLevel(logging.INFO)

    console_handler = RichHandler(console=console, show_path=False, markup=False)
    console_handler.setLevel(logging.INFO if verbose else logging.WARNING)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)
    for noisy in ("httpx", "httpcore", "huggingface_hub", "filelock"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return log_path
