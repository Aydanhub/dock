"""Application entry point: wiring, lifecycle, and nothing else.

Read this file to learn the shape of the service. Startup builds every
long-lived object once and hangs it on `app.state`; shutdown drains in-flight
work before letting the process exit. No request handler builds infrastructure
of its own, which is what keeps per-request latency down to inference.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.ops import router as ops_router
from app.api.v1.router import api_router
from app.core import metrics
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.idempotency import InMemoryIdempotencyStore, NullIdempotencyStore
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware, build_route_templates
from app.core.ratelimit import NullRateLimiter, TokenBucketRateLimiter
from app.core.telemetry import configure_telemetry
from app.services.anomaly_service import AnomalyService

configure_logging()
logger = logging.getLogger(__name__)

# How often shutdown re-checks whether in-flight requests have finished.
_DRAIN_POLL_SECONDS = 0.05


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Builds everything the service needs, then tears it down cleanly."""
    settings = get_settings()
    app.state.started_at = time.monotonic()
    app.state.shutting_down = False
    # Resolved once here rather than on the first request, so the very first
    # call after a deploy is not the one that pays to walk the route tree.
    app.state.route_templates = build_route_templates(app)

    for problem in settings.validate_runtime():
        # Loud, but not fatal: refusing to boot would turn a misconfigured
        # canary into an outage. The operator gets a warning they cannot miss
        # in a log search, and `/readyz` still reports the instance honestly.
        logger.warning("configuration problem", extra={"problem": problem})

    # Training is synchronous and CPU-bound. Running it in a worker thread
    # keeps the event loop free, so the liveness probe answers during startup
    # instead of timing out and getting the container killed mid-training.
    logger.info("training anomaly model")
    app.state.anomaly_service = await asyncio.to_thread(AnomalyService)

    app.state.rate_limiter = (
        TokenBucketRateLimiter(
            limit=settings.rate_limit_requests,
            window_seconds=settings.rate_limit_window_seconds,
        )
        if settings.rate_limit_enabled
        else NullRateLimiter()
    )
    app.state.idempotency_store = (
        InMemoryIdempotencyStore(
            ttl_seconds=settings.idempotency_ttl_seconds,
            max_entries=settings.idempotency_max_entries,
        )
        if settings.idempotency_enabled
        else NullIdempotencyStore()
    )

    metrics.build_info.labels(
        version=__version__,
        model_version=app.state.anomaly_service.model_version,
        environment=settings.environment,
    ).set(1)

    logger.info(
        "startup complete",
        extra={
            "version": __version__,
            "model_version": app.state.anomaly_service.model_version,
            "environment": settings.environment,
            "auth_active": settings.auth_active,
            "rate_limit_enabled": settings.rate_limit_enabled,
        },
    )

    try:
        yield
    finally:
        await _drain(settings.shutdown_grace_seconds, app)


async def _drain(grace_seconds: float, app: FastAPI) -> None:
    """Fails readiness first, then waits for in-flight requests to finish.

    The order is the point. Flipping `/readyz` to 503 before waiting gives the
    load balancer time to stop sending new work, so the requests being waited
    on are a shrinking set. Exiting immediately instead would drop every
    request that happened to be mid-flight — the usual source of 502s during
    an otherwise healthy deploy.
    """
    app.state.shutting_down = True
    deadline = time.monotonic() + grace_seconds

    while time.monotonic() < deadline:
        in_flight = metrics.http_requests_in_flight._value.get()
        # 1 is this shutdown's own bookkeeping-free baseline: no request is
        # counted here once handlers have returned.
        if in_flight <= 0:
            break
        await asyncio.sleep(_DRAIN_POLL_SECONDS)

    remaining = metrics.http_requests_in_flight._value.get()
    if remaining > 0:
        logger.warning("shutdown grace expired with requests in flight", extra={"in_flight": remaining})
    logger.info("shutdown complete")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        lifespan=lifespan,
        description=(
            "Production-ready FastAPI template for AI-powered backend services. "
            "Errors follow RFC 9457 (application/problem+json)."
        ),
        # Interactive docs are useful in dev and are attack surface in prod.
        docs_url="/docs" if settings.is_local else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.is_local else None,
    )

    # Outermost middleware, so a request id exists before anything else runs
    # and the access log records a status even when an inner layer raises.
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)
    app.include_router(ops_router)
    app.include_router(api_router, prefix="/api/v1")
    configure_telemetry(app)

    return app


app = create_app()
