from __future__ import annotations

import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "component": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class _HueyNoiseFilter(logging.Filter):
    """Keep actionable Huey warnings while dropping periodic success chatter."""

    def filter(self, record: logging.LogRecord) -> bool:
        verbose = os.getenv("GLANCE_HUEY_VERBOSE", "").strip().lower()
        if verbose in {"1", "true", "yes", "on"}:
            return True
        return not (record.name.startswith("huey") and record.levelno < logging.WARNING)


class _UvicornAccessFileFilter(logging.Filter):
    """server.log 不收逐请求 access 行（WARNING 以下）；控制台输出不受影响。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (record.name == "uvicorn.access" and record.levelno < logging.WARNING)


def setup_logging(settings, component: str) -> None:
    root = logging.getLogger()
    marker = f"glance:{component}"
    if any(getattr(handler, "name", "") == marker for handler in root.handlers):
        return
    log_dir = Path(settings.data_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / f"{component}.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.name = marker
    handler.setFormatter(JsonLineFormatter())
    handler.addFilter(_HueyNoiseFilter())
    handler.addFilter(_UvicornAccessFileFilter())
    root.addHandler(handler)
    root.setLevel(logging.INFO)
