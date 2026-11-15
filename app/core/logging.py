"""Logging configuration with request-ID correlation and secret redaction."""

from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)

# Patterns that must never reach log output.
_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-_\.=]+"), r"\1[REDACTED]"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*"), "[REDACTED_JWT]"),
    (
        re.compile(
            r"(?i)(\"?(?:password|access_token|refresh_token|api[_-]?key|secret|token|"
            r"x-rapidapi-key|apikey|authorization)\"?\s*[:=]\s*\"?)[^\s\",}]+"
        ),
        r"\1[REDACTED]",
    ),
    (re.compile(r"(postgres(?:ql)?(?:\+\w+)?://[^:/\s]+:)[^@\s]+@"), r"\1[REDACTED]@"),
]


def redact(text: str) -> str:
    """Remove tokens, passwords and keys from a log string."""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class RequestIdFilter(logging.Filter):
    """Attach the current request ID to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get() or "-"
        return True


class RedactingFormatter(logging.Formatter):
    """Plain-text formatter that redacts secrets."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class JsonFormatter(logging.Formatter):
    """Structured JSON formatter suitable for log aggregation."""

    _EXTRA_FIELDS = ("method", "path", "status_code", "duration_ms", "client_ip", "error_category")

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        for field in self._EXTRA_FIELDS:
            if hasattr(record, field):
                payload[field] = getattr(record, field)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, default=str))


def configure_logging(level: str = "INFO", json_logs: bool = False) -> None:
    """Configure root logging once. Safe to call repeatedly."""
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        if getattr(handler, "_sharkhire", False):
            root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler._sharkhire = True  # type: ignore[attr-defined]
    handler.addFilter(RequestIdFilter())
    if json_logs:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            RedactingFormatter(
                "%(asctime)s %(levelname)-8s [%(request_id)s] %(name)s: %(message)s"
            )
        )
    root.addHandler(handler)
    # Keep noisy libraries quiet and never log request URLs with query secrets.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
