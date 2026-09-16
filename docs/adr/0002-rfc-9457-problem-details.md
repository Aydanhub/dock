# 0002 — Errors are RFC 9457 problem documents

**Status:** accepted

## Context

FastAPI's default error body is `{"detail": ...}`, where `detail` is sometimes
a string and sometimes a list of validation objects. A client has to branch on
the type of a field to find out what went wrong, and there is nowhere to put
the information that actually helps — which request this was, how long to wait
before retrying, which field was rejected.

## Decision

Every error is an RFC 9457 problem document with `application/problem+json`:
`type`, `title`, `status`, `detail`, `instance`, plus `request_id` and whatever
the specific failure needs (`errors[]` for validation, limits for a rejected
batch).

Domain failures are exceptions (`DockError` and subclasses) carrying their own
status and problem type. Handlers registered in `app/core/errors.py` do the
rendering, including one for bare `Exception`.

## Consequences

- Domain code raises `RateLimitExceededError(...)` without importing FastAPI or
  knowing how responses are rendered.
- The catch-all handler means an unhandled bug returns a problem document with
  a request id instead of a framework traceback. Tracebacks go to the logs.
- `type` is a URL into `docs/errors.md`, so the error body links to its own
  explanation. That file has to stay in step with the code; the tests assert
  the shape, not the prose.
- Validation errors are rebuilt rather than passed through: pydantic's error
  objects can carry non-serialisable values in `ctx`, which would turn a 422
  into a 500 during serialisation.
