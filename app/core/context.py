"""Request-scoped context, propagated without threading arguments through.

A request id needs to reach the log formatter, the error handler, and any
service method that wants to mention it — none of which sit on the call path
where the id is created. A `ContextVar` carries it instead, which is also
safe under asyncio: each task gets its own copy rather than sharing one
mutable global.
"""

from contextvars import ContextVar
from typing import Any

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_client_id: ContextVar[str | None] = ContextVar("client_id", default=None)


def set_request_id(value: str) -> None:
    _request_id.set(value)


def get_request_id() -> str | None:
    return _request_id.get()


def set_client_id(value: str | None) -> None:
    _client_id.set(value)


def get_client_id() -> str | None:
    return _client_id.get()


def current_context() -> dict[str, Any]:
    """The context fields worth attaching to every log line and error body."""
    context: dict[str, Any] = {}
    request_id = _request_id.get()
    if request_id:
        context["request_id"] = request_id
    client_id = _client_id.get()
    if client_id:
        context["client_id"] = client_id
    return context
