"""Structured JSON logging with request/poll-cycle correlation.

Correlation IDs travel via :mod:`contextvars` so they survive ``await``
boundaries: HTTP requests get an ``X-Request-ID`` (middleware in
:mod:`app.main`), poll cycles get a ``cycle_id`` set by the poller. Every log
line emitted while either is set carries the ID, which is what makes a single
poll cycle or request traceable through a log aggregator.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
cycle_id_var: ContextVar[str | None] = ContextVar("cycle_id", default=None)

# LogRecord attributes that are internals rather than user-supplied extras.
_RESERVED = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "message",
    }
)  # fmt: skip


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if (request_id := request_id_var.get()) is not None:
            payload["request_id"] = request_id
        if (cycle_id := cycle_id_var.get()) is not None:
            payload["cycle_id"] = cycle_id
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class PlainFormatter(logging.Formatter):
    """Human-readable format for local development."""

    def format(self, record: logging.LogRecord) -> str:
        prefix = ""
        if (cycle_id := cycle_id_var.get()) is not None:
            prefix = f"[cycle {cycle_id}] "
        elif (request_id := request_id_var.get()) is not None:
            prefix = f"[req {request_id[:8]}] "
        base = f"{self.formatTime(record)} {record.levelname:<8} {record.name}: {prefix}"
        base += record.getMessage()
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def configure_logging(level: str, json_logs: bool) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_logs else PlainFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # Uvicorn's access log duplicates what the request middleware logs.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
