"""Operational endpoints, mounted at the root rather than under `/api/v1`.

These are not part of the product API and must not be versioned with it: an
orchestrator's probe configuration should not have to change because the
business API moved to v2. They are also deliberately unauthenticated — a
kubelet cannot present an API key, and none of them disclose anything a
caller could not learn by watching the service respond.
"""

from fastapi import APIRouter, Request, Response

from app import __version__
from app.core import metrics
from app.core.config import get_settings
from app.models.schemas import LivenessResponse, ReadinessResponse

router = APIRouter(include_in_schema=False)


@router.get("/healthz", response_model=LivenessResponse, tags=["ops"])
def liveness() -> LivenessResponse:
    """Liveness: is the process running.

    Intentionally checks nothing else. A liveness probe that tests
    dependencies turns a downstream outage into a restart loop, which removes
    the last healthy instances exactly when they are needed most.
    """
    return LivenessResponse()


@router.get("/readyz", response_model=ReadinessResponse, tags=["ops"])
def readiness(request: Request, response: Response) -> ReadinessResponse:
    """Readiness: can this instance serve traffic right now.

    Returns 503 when it cannot, so the orchestrator takes the instance out of
    rotation without killing it — the right outcome while a model is still
    training at startup.
    """
    settings = get_settings()
    service = getattr(request.app.state, "anomaly_service", None)
    shutting_down = getattr(request.app.state, "shutting_down", False)

    checks = {
        "model_loaded": service is not None and service.is_ready,
        "accepting_traffic": not shutting_down,
    }
    ready = all(checks.values())
    if not ready:
        response.status_code = 503

    return ReadinessResponse(
        status="ready" if ready else "not_ready",
        app_name=settings.app_name,
        version=__version__,
        environment=settings.environment,
        model_version=service.model_version if service is not None else "unloaded",
        checks=checks,
    )


@router.get("/metrics", tags=["ops"])
def prometheus_metrics() -> Response:
    """Prometheus scrape endpoint.

    Served from the app's own registry rather than the library's global one,
    so an imported dependency that registers its own collectors cannot change
    what this service exposes.
    """
    payload, content_type = metrics.render()
    return Response(content=payload, media_type=content_type)
