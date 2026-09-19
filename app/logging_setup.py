"""One JSON object per log line.

Vector collects this container's stdout through the Docker socket and parses
JSON, deriving `level` from the field of that name, so structured lines here
mean `level:error` filtering works in VictoriaLogs with no per-service
configuration. Plain-text logs would arrive as an opaque `message`.
"""

from __future__ import annotations

import json
import logging
import sys

from . import config


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(config.LOG_LEVEL)
    # uvicorn installs its own handlers; take them over so every line is JSON.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        log = logging.getLogger(name)
        log.handlers = [handler]
        log.propagate = False


def log(logger: logging.Logger, level: int, message: str, **fields) -> None:
    """Log with structured fields, which the formatter merges into the line."""
    logger.log(level, message, extra={"fields": fields})
