from fastapi import APIRouter, Request

from app import __version__
from app.core.config import get_settings
from app.models.schemas import HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse, tags=["health"])
def health(request: Request) -> HealthResponse:
    """A human-readable health summary.

    The machine-readable probes live at `/healthz` and `/readyz`; this one is
    for a person checking what a deployment is actually running.
    """
    settings = get_settings()
    service = getattr(request.app.state, "anomaly_service", None)
    started_at = getattr(request.app.state, "started_at", None)
    import time

    return HealthResponse(
        app_name=settings.app_name,
        version=__version__,
        environment=settings.environment,
        model_version=service.model_version if service is not None else "unloaded",
        uptime_seconds=round(time.monotonic() - started_at, 3) if started_at else 0.0,
    )
