from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .config import LoggingSettings

_FORBIDDEN_KEYS = {
    "authorization",
    "api_key",
    "token",
    "audio",
    "pcm",
    "delta",
    "transcript",
    "text",
    "stash",
    "phrases",
}


class SafeJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        safe_fields = getattr(record, "safe_fields", {})
        if isinstance(safe_fields, dict):
            for key, value in safe_fields.items():
                if key.lower() not in _FORBIDDEN_KEYS and isinstance(
                    value, (str, int, float, bool, type(None))
                ):
                    payload[key] = value
        if record.exc_info:
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(settings: LoggingSettings) -> Path:
    settings.directory.mkdir(parents=True, exist_ok=True)
    log_path = settings.directory / "ai-interpreter.jsonl"
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, settings.level, logging.INFO))
    handler = RotatingFileHandler(
        log_path,
        maxBytes=settings.max_bytes_per_file,
        backupCount=settings.max_files,
        encoding="utf-8",
    )
    handler.setFormatter(SafeJsonFormatter())
    root.addHandler(handler)
    return log_path


def log_status(logger: logging.Logger, event: Any) -> None:
    logger.info(
        "status_changed",
        extra={
            "safe_fields": {
                "component": event.component,
                "channel": event.channel,
                "state": event.state,
                "category": event.category,
                "message": str(event.message)[:160] if event.message else "",
                **event.counters,
            }
        },
    )
