"""Scoring endpoints.

Both handlers are defined with `def`, not `async def`, on purpose: the model
call is CPU-bound and releases no event loop. FastAPI runs a sync handler in
its threadpool, so a slow inference blocks one worker thread instead of
stalling the whole event loop and every other in-flight request with it.
"""

import logging
import time
from typing import Any

from fastapi import APIRouter, Header, Response

from app.api.deps import CallerId, IdempotencyDep, Service
from app.core import metrics
from app.core.config import get_settings
from app.core.errors import (
    BatchTooLargeError,
    IdempotencyConflictError,
    IdempotencyInFlightError,
)
from app.core.idempotency import InFlightError, KeyReuseError, idempotent
from app.models.schemas import (
    BatchScoringRequest,
    BatchScoringResponse,
    ProblemDetail,
    ScoringRequest,
    ScoringResponse,
)

router = APIRouter()
logger = logging.getLogger(__name__)

IDEMPOTENCY_HEADER = "Idempotency-Key"

_PROBLEM_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ProblemDetail, "description": "Missing or invalid API key"},
    409: {"model": ProblemDetail, "description": "A request with this idempotency key is in flight"},
    422: {"model": ProblemDetail, "description": "Validation failed, or idempotency key reused"},
    429: {"model": ProblemDetail, "description": "Rate limit exceeded"},
    503: {"model": ProblemDetail, "description": "Model not ready"},
}


@router.post(
    "/score",
    response_model=ScoringResponse,
    responses=_PROBLEM_RESPONSES,
    tags=["scoring"],
)
def score(
    payload: ScoringRequest,
    response: Response,
    caller: CallerId,
    service: Service,
    store: IdempotencyDep,
    idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER),
) -> ScoringResponse:
    """Scores a single event.

    Send an `Idempotency-Key` header to make a retry safe: the same key with
    the same body replays the original decision instead of producing a second,
    differently-numbered one.
    """
    body = payload.model_dump()
    scoped_key = f"{caller}:{idempotency_key}" if idempotency_key else None

    try:
        with idempotent(store, scoped_key, body) as slot:
            if slot.is_replay:
                metrics.idempotent_replays_total.inc()
                response.headers["Idempotency-Replayed"] = "true"
                return ScoringResponse.model_validate(slot.replay)

            started = time.perf_counter()
            result = service.score(body)
            metrics.record_inference(
                time.perf_counter() - started, result.anomaly_score, result.is_anomalous
            )

            logger.info(
                "scored event",
                extra={
                    "scoring_id": result.request_id,
                    "is_anomalous": result.is_anomalous,
                    "anomaly_score": result.anomaly_score,
                    "model_version": result.model_version,
                    "inference_ms": result.latency_ms,
                },
            )

            payload_out = ScoringResponse(
                request_id=result.request_id,
                is_anomalous=result.is_anomalous,
                anomaly_score=result.anomaly_score,
                model_version=result.model_version,
                latency_ms=result.latency_ms,
            )
            slot.save(payload_out.model_dump())
            return payload_out
    except KeyReuseError as exc:
        raise IdempotencyConflictError(
            f"{IDEMPOTENCY_HEADER} has already been used for a different request body."
        ) from exc
    except InFlightError as exc:
        raise IdempotencyInFlightError(
            f"A request with this {IDEMPOTENCY_HEADER} is still being processed."
        ) from exc


@router.post(
    "/score/batch",
    response_model=BatchScoringResponse,
    responses={**_PROBLEM_RESPONSES, 413: {"model": ProblemDetail, "description": "Batch too large"}},
    tags=["scoring"],
)
def score_batch(
    payload: BatchScoringRequest,
    caller: CallerId,
    service: Service,
) -> BatchScoringResponse:
    """Scores many events in one vectorised model call.

    Measurably cheaper than the equivalent number of single calls — the
    per-call overhead of the underlying estimator dominates the per-row cost
    at these batch sizes.
    """
    settings = get_settings()
    if len(payload.events) > settings.max_batch_size:
        raise BatchTooLargeError(
            f"Batch of {len(payload.events)} exceeds the maximum of {settings.max_batch_size}.",
            max_batch_size=settings.max_batch_size,
            received=len(payload.events),
        )

    events = [event.model_dump() for event in payload.events]
    started = time.perf_counter()
    results = service.score_many(events)
    elapsed = time.perf_counter() - started

    for result in results:
        metrics.record_inference(
            elapsed / len(results), result.anomaly_score, result.is_anomalous
        )

    anomalous_count = sum(r.is_anomalous for r in results)
    logger.info(
        "scored batch",
        extra={
            "batch_size": len(results),
            "anomalous_count": anomalous_count,
            "model_version": service.model_version,
            "inference_ms": round(elapsed * 1000, 3),
        },
    )

    return BatchScoringResponse(
        request_id=results[0].request_id,
        model_version=service.model_version,
        count=len(results),
        anomalous_count=anomalous_count,
        latency_ms=round(elapsed * 1000, 3),
        results=[
            ScoringResponse(
                request_id=r.request_id,
                is_anomalous=r.is_anomalous,
                anomaly_score=r.anomaly_score,
                model_version=r.model_version,
                latency_ms=r.latency_ms,
            )
            for r in results
        ],
    )
