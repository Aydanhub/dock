"""Structured logging: one JSON object per line, on stdout.

stdout rather than a file because in a container the platform owns log
shipping; a service that writes its own files has to solve rotation, disk
pressure and collection all over again.

The formatter merges two things into every line: whatever the call site
passed as `extra={...}`, and the ambient request context (request id, client
id). The second is what makes the logs searchable — every line produced while
handling a request carries the same id, including ones written deep in a
service class that was never handed the id.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.core.config import get_settings
from app.core.context import current_context

# Attribute names LogRecord always has; anything else on a record came from
# an `extra={...}` at the call site and should be merged into the payload.
_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()) | {
    "taskName",
}


class RequestContextFilter(logging.Filter):
    """Copies the ambient request context onto every record.

    A filter rather than a formatter lookup, and the difference matters: a
    filter runs when the record is *handled*, which is still inside the
    logging call, while a formatter can run much later on another thread —
    under a `QueueHandler`, for instance, where the ContextVar holding the
    request id is long gone. Stamping the record at handle time makes the
    context travel with it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in current_context().items():
            # An explicit `extra={...}` at the call site wins.
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


class JsonFormatter(logging.Formatter):
    """Renders each log record as one JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update({k: v for k, v in record.__dict__.items() if k not in _RESERVED})

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging() -> None:
    """Wires the root logger to emit single-line JSON to stdout.

    Safe to call more than once — handlers are cleared rather than stacked,
    which otherwise duplicates every line once per call (a real problem when
    both app startup and a test fixture configure logging).
    """
    settings = get_settings()
    root = logging.getLogger()
    root.setLevel(settings.log_level)
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    # On the handler, not the root logger: a logger's own filters only run for
    # records logged directly to it, while handler filters run for everything
    # that propagates up — which is every record in the application.
    handler.addFilter(RequestContextFilter())
    root.addHandler(handler)

    # uvicorn writes its own access line per request; this service writes a
    # richer one in RequestContextMiddleware, so the duplicate is silenced.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
