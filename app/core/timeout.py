"""A deadline on request handling.

Written as raw ASGI rather than as a `BaseHTTPMiddleware`, and that is the
whole point of the file. The obvious version — `asyncio.wait_for` inside a
`BaseHTTPMiddleware.dispatch` — returns a 504 but does not shorten the wait at
all: `call_next` runs the rest of the app inside an anyio task group, and the
group will not let `dispatch` return until its child task finishes. The client
receives "504" after exactly as long as it would have waited for the real
response, which is worse than having no timeout, because it looks like a
working one. Measured on this service: 2003ms for a 300ms deadline.

At the ASGI layer there is no task group in the way, so the deadline is real.

What it does and does not guarantee
-----------------------------------
An `async def` handler is genuinely cancelled at the deadline.

A `def` handler is not. It runs in a threadpool, and Python offers no safe way
to stop a running thread, so the work continues to completion and its result is
discarded. The client is freed on time; the worker is not. This service's
scoring handlers are deliberately sync (see ADR 0007), so that is the common
case here.

That makes the timeout a protection for callers, not a resource guarantee for
the server. A handler stuck in a genuine infinite loop holds its threadpool
slot for the life of the process, and enough of those exhaust the pool. This is
why `dock_request_timeouts_total` exists: a rising timeout rate is an early
warning that the pool is draining, not a problem the timeout has solved.
"""

import asyncio
import logging
from collections.abc import Iterable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core import metrics
from app.core.errors import problem_response

logger = logging.getLogger(__name__)


def _discard(task: "asyncio.Task[None]") -> None:
    """Consume a cancelled task's outcome so asyncio does not log it as lost.

    The task is deliberately not awaited: for a sync handler that await would
    block until the threadpool worker finished, which is the exact wait the
    deadline exists to avoid.
    """
    if not task.cancelled():
        task.exception()


class TimeoutMiddleware:
    """Fails a request with 504 once it has taken longer than `timeout_seconds`.

    The deadline governs time to the *first byte*, not the whole response. Once
    the status line is on the wire the deadline stops applying and the body is
    allowed to finish. Cancelling at that point cannot produce a 504 — the
    status is already sent — and only corrupts a response the caller could
    otherwise have used: measured on a streaming endpoint, the naive version
    delivered an empty body with a 200 after waiting the full duration anyway.

    `exempt_paths` skips the deadline entirely, for endpoints where even the
    first byte is legitimately slow.
    """

    def __init__(
        self,
        app: ASGIApp,
        timeout_seconds: float,
        exempt_paths: Iterable[str] = (),
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.app = app
        self.timeout_seconds = timeout_seconds
        self.exempt_paths = frozenset(exempt_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in self.exempt_paths:
            await self.app(scope, receive, send)
            return

        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        handling = asyncio.ensure_future(self.app(scope, receive, tracking_send))
        finished, _ = await asyncio.wait({handling}, timeout=self.timeout_seconds)

        if handling in finished:
            await handling  # re-raise whatever the application raised
            return

        path = scope.get("path", "")

        if response_started:
            # Past the point of no return: the status is already sent, so the
            # deadline no longer applies and the body is left to finish.
            logger.warning(
                "response started before the deadline and is still streaming",
                extra={"path": path, "timeout_seconds": self.timeout_seconds},
            )
            await handling
            return

        handling.cancel()
        handling.add_done_callback(_discard)

        metrics.request_timeouts_total.labels(route=path or "unknown").inc()
        logger.warning(
            "request timed out",
            extra={"path": path, "timeout_seconds": self.timeout_seconds},
        )
        response = problem_response(
            status=504,
            title="Gateway Timeout",
            detail=(
                f"The request exceeded the {self.timeout_seconds:g}s server deadline. "
                "It is safe to retry; send an Idempotency-Key so a retry cannot "
                "produce a second decision."
            ),
            problem_type="request-timeout",
            instance=path or None,
        )
        await response(scope, receive, send)
