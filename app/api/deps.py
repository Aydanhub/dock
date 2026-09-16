"""Shared dependencies: what every protected endpoint needs before it runs.

Keeping auth, rate limiting and store access here means an endpoint declares
its requirements in its signature and stays a pure function of its inputs —
and a new endpoint picks up the whole policy by adding one parameter.
"""

from typing import Annotated

from fastapi import Depends, Request, Response

from app.core import metrics
from app.core.errors import ModelNotReadyError, RateLimitExceededError
from app.core.idempotency import IdempotencyStore
from app.core.ratelimit import RateLimiter
from app.core.security import authenticate
from app.services.anomaly_service import AnomalyService


def get_anomaly_service(request: Request) -> AnomalyService:
    """The service trained once at startup (see `app.main`'s lifespan hook).

    Raises rather than returning a half-built model: a request that arrives
    during startup gets a 503 it can retry, not a confident answer from an
    untrained model.
    """
    service: AnomalyService | None = getattr(request.app.state, "anomaly_service", None)
    if service is None or not service.is_ready:
        raise ModelNotReadyError("The model is still loading. Retry shortly.")
    return service


def get_rate_limiter(request: Request) -> RateLimiter:
    limiter: RateLimiter = request.app.state.rate_limiter
    return limiter


def get_idempotency_store(request: Request) -> IdempotencyStore:
    store: IdempotencyStore = request.app.state.idempotency_store
    return store


def get_caller(
    request: Request,
    response: Response,
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> str:
    """Authenticates the caller and charges them one unit of quota.

    Rate limiting runs after authentication so the bucket is keyed by API key
    rather than by IP: one misbehaving client cannot exhaust the quota of
    everyone behind the same NAT gateway, and a client that rotates IPs cannot
    escape its own limit.

    Limit headers are attached to successful responses too, so a well-behaved
    caller can slow down before it is ever rejected.
    """
    client_id = authenticate(request)
    decision = limiter.check(client_id)

    if not decision.allowed:
        metrics.rate_limit_rejections_total.inc()
        raise RateLimitExceededError(
            "Rate limit exceeded. Retry after the interval in the Retry-After header.",
            headers=decision.headers(),
            limit=decision.limit,
        )

    response.headers.update(decision.headers())
    return client_id


CallerId = Annotated[str, Depends(get_caller)]
Service = Annotated[AnomalyService, Depends(get_anomaly_service)]
IdempotencyDep = Annotated[IdempotencyStore, Depends(get_idempotency_store)]
