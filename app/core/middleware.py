"""The middleware every request passes through.

Ordering matters and is set in `app.main`: this runs outermost, so a request
id exists before anything else can log, and the access log records a status
even for requests that fail inside an inner layer.
"""

import logging
import time
import uuid
from collections.abc import Iterable
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core import metrics
from app.core.context import current_context, set_client_id, set_request_id

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
RESPONSE_TIME_HEADER = "X-Response-Time-ms"

# Paths that would otherwise fill the access log with noise from probes and
# scrapers running every few seconds.
_QUIET_PATHS = frozenset({"/metrics", "/healthz", "/readyz"})

_MAX_INBOUND_REQUEST_ID = 128

UNMATCHED_ROUTE = "unmatched"


def build_route_templates(app: Any) -> dict[int, str]:
    """Maps each route object to its *full* path template, prefixes included.

    `scope["route"]` holds the route as its own router declared it, which for
    an included router is the un-prefixed path — `/score`, not
    `/api/v1/score`. Labelling metrics with that would merge two different
    endpoints that happen to share a suffix across API versions.

    Walking the tree once at startup and keying by object identity keeps the
    lookup O(1) per request. Both router shapes are handled: FastAPI versions
    that nest included routers (the `include_context` branch) and older ones
    that flatten them at include time (the final branch, where the declared
    path is already complete).
    """
    templates: dict[int, str] = {}

    def walk(routes: Iterable[Any] | None, prefix: str) -> None:
        for route in routes or ():
            include_context = getattr(route, "include_context", None)
            if include_context is not None:
                walk(
                    include_context.included_router.routes,
                    prefix + (include_context.prefix or ""),
                )
                continue

            mounted = getattr(route, "routes", None)
            if mounted:
                walk(mounted, prefix + (getattr(route, "path", "") or ""))
                continue

            declared = getattr(route, "path_format", None) or getattr(route, "path", "")
            if declared:
                templates[id(route)] = prefix + declared

    walk(getattr(app, "routes", None), "")
    return templates


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request id, times the request, logs it, and records metrics.

    An inbound `X-Request-ID` is honoured so a trace started by an upstream
    gateway keeps one id across services — but it is length-capped and
    sanitised first, because it is attacker-controlled input that ends up in
    log lines and response headers.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = self._resolve_request_id(request)
        set_request_id(request_id)
        set_client_id(None)

        started = time.perf_counter()
        metrics.http_requests_in_flight.inc()
        status_code = 500

        try:
            response = await call_next(request)
            status_code = response.status_code
            return self._finalise(request, response, request_id, started)
        finally:
            elapsed = time.perf_counter() - started
            metrics.http_requests_in_flight.dec()
            route = _route_template(request)
            metrics.http_requests_total.labels(
                method=request.method, route=route, status=str(status_code)
            ).inc()
            metrics.http_request_duration_seconds.labels(
                method=request.method, route=route
            ).observe(elapsed)

            if request.url.path not in _QUIET_PATHS:
                logger.info(
                    "request completed",
                    extra={
                        "method": request.method,
                        "path": request.url.path,
                        "route": route,
                        "status": status_code,
                        "duration_ms": round(elapsed * 1000, 3),
                        **current_context(),
                    },
                )

    @staticmethod
    def _resolve_request_id(request: Request) -> str:
        inbound = request.headers.get(REQUEST_ID_HEADER, "")
        candidate = inbound.strip()[:_MAX_INBOUND_REQUEST_ID]
        # Only accept characters that are safe in a header and readable in a log.
        if candidate and all(c.isalnum() or c in "-_." for c in candidate):
            return candidate
        return str(uuid.uuid4())

    @staticmethod
    def _finalise(
        request: Request, response: Response, request_id: str, started: float
    ) -> Response:
        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers[RESPONSE_TIME_HEADER] = f"{(time.perf_counter() - started) * 1000:.3f}"
        return response


def _route_template(request: Request) -> str:
    """The matched route pattern, or `unmatched` — never the raw path.

    Labelling metrics with `request.url.path` would mint a new time series for
    every distinct URL, including the 404s a scanner generates. This keeps
    metric cardinality bounded by the number of routes the service declares.

    The template map is cached on `app.state` and rebuilt on a miss, so routes
    added after startup are picked up without a restart.
    """
    route = request.scope.get("route")
    if route is None:
        return UNMATCHED_ROUTE

    app = request.app
    templates: dict[int, str] | None = getattr(app.state, "route_templates", None)
    if templates is None or id(route) not in templates:
        templates = build_route_templates(app)
        app.state.route_templates = templates

    return (
        templates.get(id(route))
        or getattr(route, "path_format", None)
        or getattr(route, "path", None)
        or UNMATCHED_ROUTE
    )
