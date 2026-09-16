# Error reference

Every error this service returns is an [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457)
problem document, served as `application/problem+json`. One shape for every
failure means a client writes one error branch instead of one per endpoint.

```json
{
  "type": "https://github.com/Aydanhub/dock/blob/main/docs/errors.md#validation-failed",
  "title": "Unprocessable Entity",
  "status": 422,
  "detail": "The request body failed validation.",
  "instance": "/api/v1/score",
  "request_id": "cdc4cba0-8f63-4ebe-9650-c0fad3e7c38b",
  "errors": [
    { "field": "body.amount", "message": "Input should be greater than or equal to 0", "type": "greater_than_equal" }
  ]
}
```

`request_id` is on every problem document and in the `X-Request-ID` response
header. Quote it in a bug report: it is the key that finds every log line the
service wrote while handling that call.

## Problem types

### `unauthenticated` — 401

The `X-API-Key` header is missing or does not match a configured key. The
response carries `WWW-Authenticate: X-API-Key`.

Neither the presented key nor the configured keys ever appear in the response
or in the logs. Rejected keys are logged by a truncated SHA-256 fingerprint,
which is enough to correlate repeated failures from one caller without
recording the secret.

**Fix:** send a valid key. If you are the operator, check `API_KEYS`.

### `validation-failed` — 422

The request body did not match the schema. `errors[]` names each offending
field, the reason, and pydantic's error type.

Unknown fields are rejected rather than ignored, so a misspelled field name is
a 422 and not a silently defaulted value.

### `idempotency-key-reuse` — 422

The `Idempotency-Key` was already used for a *different* request body.
Returning the stored decision would answer a question you did not ask, so the
request is refused instead.

**Fix:** use a fresh key for a new request. Reuse a key only when retrying the
identical request.

### `idempotent-request-in-flight` — 409

Another request with the same `Idempotency-Key` is still being processed.

**Fix:** wait for the first request to finish, then retry. Two concurrent
retries of the same operation are exactly what the key exists to prevent.

### `rate-limited` — 429

The caller's quota is exhausted. Carries `Retry-After` plus the
`RateLimit-Limit`, `RateLimit-Remaining` and `RateLimit-Reset` headers.

Those headers are on successful responses too, so a well-behaved client can
slow down before it is ever rejected.

**Fix:** back off for `Retry-After` seconds. Quota is per API key, so it is
your own traffic that exhausted it.

### `batch-too-large` — 413

The batch exceeded `MAX_BATCH_SIZE`. The body reports both the limit and what
was received.

**Fix:** split the batch.

### `model-not-ready` — 503

The instance is still training its model, or is shutting down. Retryable.

In normal operation a load balancer never sends you this: readiness fails
first, which takes the instance out of rotation. Seeing it means traffic
reached an instance before `/readyz` was consulted.

### `request-timeout` — 504

Handling took longer than `REQUEST_TIMEOUT_SECONDS`, so the server gave up
waiting and freed your connection.

Retryable — but this is precisely the case where you cannot know whether the
work happened, so send an `Idempotency-Key` on the retry. With one, a retry
replays the original decision instead of producing a second, different one.

Note what the deadline does and does not do. It bounds *your* wait. On the
server the handler may still be running: the scoring endpoints are synchronous
and run in a threadpool, and a running thread cannot be stopped safely, so the
work finishes and its result is discarded.

### `http-error` — various

A framework-level failure such as 404 or 405, rendered into the same shape as
everything else.

### `internal-error` — 500

An unhandled exception. The body deliberately says nothing about the cause —
the traceback goes to the logs, keyed by `request_id`, because error bodies are
a bad place to disclose internals.

**Fix:** report it with the request id.
