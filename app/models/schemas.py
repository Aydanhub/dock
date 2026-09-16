"""Request and response models.

These are the service's public contract. They are versioned with the URL
prefix (`/api/v1`), so adding a field here is a compatible change and
removing or retyping one is not.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class LivenessResponse(BaseModel):
    """Answers "is this process alive", nothing more."""

    status: Literal["alive"] = "alive"


class ReadinessResponse(BaseModel):
    """Answers "can this instance serve traffic right now".

    Deliberately separate from liveness: a model still loading is not ready,
    but restarting it would not help, so an orchestrator must be able to pull
    it out of the load balancer without killing it.
    """

    status: Literal["ready", "not_ready"]
    app_name: str
    version: str
    environment: str
    model_version: str
    checks: dict[str, bool]


class HealthResponse(BaseModel):
    """Combined health view, kept for humans and for backwards compatibility."""

    status: str = "ok"
    app_name: str
    version: str
    environment: str
    model_version: str
    uptime_seconds: float


class ScoringRequest(BaseModel):
    """One transaction-like event to score for anomalousness.

    The shape is deliberately generic — an amount, a time, an upstream risk
    signal, a device signal — so it stands in for any tabular fraud- or
    abuse-scoring payload. `extra="forbid"` makes a typo in a field name a
    422 instead of a silently ignored field, which is the difference between
    catching a bad integration in staging and discovering it in a postmortem.
    """

    model_config = ConfigDict(extra="forbid")

    amount: float = Field(..., ge=0, le=1_000_000_000, description="Transaction amount")
    hour_of_day: int = Field(..., ge=0, le=23, description="Local hour the event occurred, 0-23")
    merchant_risk_score: float = Field(
        ..., ge=0, le=1, description="Upstream merchant risk signal, 0-1"
    )
    is_new_device: bool = Field(..., description="Whether this device has not been seen before")


class ScoringResponse(BaseModel):
    request_id: str
    is_anomalous: bool
    anomaly_score: float = Field(..., description="Higher means more anomalous")
    model_version: str = Field(..., description="Identity of the model that produced this decision")
    latency_ms: float


class BatchScoringRequest(BaseModel):
    """Several events scored in one call.

    Batching is not just a convenience: the service scores the whole batch in
    a single vectorised model call, so N events cost far less than N requests.
    The upper bound is enforced in the endpoint against the configured
    `max_batch_size`, which keeps one caller from monopolising a worker.
    """

    model_config = ConfigDict(extra="forbid")

    events: Annotated[list[ScoringRequest], Field(min_length=1)]


class BatchScoringResponse(BaseModel):
    request_id: str
    model_version: str
    count: int
    anomalous_count: int
    latency_ms: float
    results: list[ScoringResponse]


class ProblemDetail(BaseModel):
    """RFC 9457 error body. Declared so it shows up in the OpenAPI schema."""

    model_config = ConfigDict(extra="allow")

    type: str
    title: str
    status: int
    detail: str
    instance: str | None = None
    request_id: str | None = None
