"""Structured JSON logging with correlation-id injection.

Every service calls ``get_logger(__name__)`` and gets a logger that
automatically includes ``correlation_id`` when set via context var.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

correlation_id_ctx: ContextVar[str | None] = ContextVar(
    "correlation_id", default=None
)


class StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now(UTC).isoformat()
        log_entry: dict[str, Any] = {
            "timestamp": ts,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        cid = correlation_id_ctx.get()
        if cid:
            log_entry["correlation_id"] = cid

        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = str(record.exc_info[1])

        for key in ("service", "route", "amount_usd", "event_type"):
            val = getattr(record, key, None)
            if val is not None:
                log_entry[key] = val

        return json.dumps(log_entry, default=str, ensure_ascii=False)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(StructuredFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
    return logger


def set_correlation_id(cid: str) -> None:
    correlation_id_ctx.set(cid)


def get_correlation_id() -> str | None:
    return correlation_id_ctx.get()
