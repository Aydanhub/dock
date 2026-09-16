"""Error types and handlers that speak RFC 9457 Problem Details.

Every failure leaving this service — validation, auth, rate limiting, an
unhandled bug — comes back in the same JSON shape with the same content type
(`application/problem+json`). Callers write one error branch instead of one
per endpoint, and every body carries the `request_id` needed to find the
matching log line.
"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.context import current_context

logger = logging.getLogger(__name__)

PROBLEM_CONTENT_TYPE = "application/problem+json"
PROBLEM_BASE_URI = "https://github.com/Aydanhub/dock/blob/main/docs/errors.md"


class DockError(Exception):
    """Base class for failures this service raises on purpose.

    Carrying the HTTP status on the exception lets domain code raise a
    meaningful error without importing FastAPI or knowing how responses are
    rendered — the handler below does the translating.
    """

    status_code: int = 500
    problem_type: str = "internal-error"
    title: str = "Internal Server Error"

    def __init__(self, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.detail = detail
        self.extra = extra


class AuthenticationError(DockError):
    status_code = 401
    problem_type = "unauthenticated"
    title = "Unauthorized"


class BatchTooLargeError(DockError):
    status_code = 413
    problem_type = "batch-too-large"
    title = "Payload Too Large"


class RateLimitExceededError(DockError):
    status_code = 429
    problem_type = "rate-limited"
    title = "Too Many Requests"


class IdempotencyConflictError(DockError):
    status_code = 422
    problem_type = "idempotency-key-reuse"
    title = "Idempotency Key Reused"


class IdempotencyInFlightError(DockError):
    status_code = 409
    problem_type = "idempotent-request-in-flight"
    title = "Conflict"


class ModelNotReadyError(DockError):
    status_code = 503
    problem_type = "model-not-ready"
    title = "Service Unavailable"


def problem_response(
    status: int,
    title: str,
    detail: str,
    problem_type: str,
    instance: str | None = None,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"{PROBLEM_BASE_URI}#{problem_type}",
        "title": title,
        "status": status,
        "detail": detail,
    }
    if instance:
        body["instance"] = instance
    body.update(current_context())
    body.update({k: v for k, v in extra.items() if v is not None})

    return JSONResponse(
        status_code=status,
        content=body,
        media_type=PROBLEM_CONTENT_TYPE,
        headers=headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Installs the handlers that keep every error body in the same shape."""

    @app.exception_handler(DockError)
    async def _handle_dock_error(request: Request, exc: DockError) -> JSONResponse:
        headers = exc.extra.pop("headers", None)
        logger.warning(
            "request failed",
            extra={
                "problem_type": exc.problem_type,
                "status": exc.status_code,
                "path": request.url.path,
            },
        )
        return problem_response(
            status=exc.status_code,
            title=exc.title,
            detail=exc.detail,
            problem_type=exc.problem_type,
            instance=request.url.path,
            headers=headers,
            **exc.extra,
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # pydantic's error objects can hold non-serialisable `ctx` values
        # (exception instances, for one), so only the safe fields are copied.
        errors = [
            {
                "field": ".".join(str(part) for part in error.get("loc", ())),
                "message": error.get("msg", ""),
                "type": error.get("type", ""),
            }
            for error in exc.errors()
        ]
        return problem_response(
            status=422,
            title="Unprocessable Entity",
            detail="The request body failed validation.",
            problem_type="validation-failed",
            instance=request.url.path,
            errors=errors,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return problem_response(
            status=exc.status_code,
            title=str(exc.detail),
            detail=str(exc.detail),
            problem_type="http-error",
            instance=request.url.path,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # The traceback goes to the logs; the client gets the request id and
        # nothing about the internals.
        logger.exception("unhandled exception", extra={"path": request.url.path})
        return problem_response(
            status=500,
            title="Internal Server Error",
            detail="The request could not be completed. Quote the request id when reporting this.",
            problem_type="internal-error",
            instance=request.url.path,
        )
