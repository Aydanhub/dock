# 0008 — The request deadline is ASGI middleware, not BaseHTTPMiddleware

**Status:** accepted

## Context

`REQUEST_TIMEOUT_SECONDS` existed in the configuration and in `.env.example`
for a while before anything read it. That is worse than having no setting: an
operator who sets it believes the service is protected against a hung handler,
and nothing is.

Implementing it looks like a three-line job. Starlette offers
`BaseHTTPMiddleware`, so the obvious version is `asyncio.wait_for` around
`call_next`.

## The obvious version does not work

It returns a 504, which is what makes it dangerous — it looks correct in a
smoke test. But `call_next` runs the rest of the application inside an anyio
task group, and the group will not let `dispatch` return until its child task
finishes. Measured on this service with a 300ms deadline and a 2s handler:

| Implementation | Status | Client waited |
| --- | --- | --- |
| `wait_for` inside `BaseHTTPMiddleware` | 504 | 2003ms |
| Raw ASGI middleware | 504 | 303ms |

The first row is a timeout that saves the caller nothing, reported as a
success. Shipping it would have been worse than leaving the setting dead,
because the dead setting is at least discoverable by grep.

## Decision

Write it as raw ASGI. There is no task group in the way, so the deadline is
real.

**The deadline covers time to the first byte, not the whole response.** Once
the status line is on the wire it cannot be changed to a 504, so cancelling
then corrupts a response the caller could otherwise use. An intermediate
version did cancel, and on a streaming endpoint it delivered an empty body with
a 200 after waiting the full duration anyway — every downside, no upside. Now a
started response is left to finish, and `exempt_paths` covers endpoints where
even the first byte is legitimately slow.

## Consequences

- An `async def` handler is genuinely cancelled at the deadline.
- A `def` handler is not, and cannot be: Python offers no safe way to stop a
  running thread. The work runs to completion and its result is discarded. The
  scoring handlers here are deliberately sync (ADR 0007), so this is the common
  case.
- The cancelled task is not awaited, because awaiting it would block on that
  same threadpool worker — reintroducing the wait the deadline exists to avoid.
  A done-callback consumes its outcome so asyncio does not log it as lost.
- **So the deadline protects callers, not the server.** A handler stuck in a
  real infinite loop holds its threadpool slot for the life of the process, and
  enough of those exhaust the pool. `dock_request_timeouts_total` exists for
  exactly this: a rising timeout rate is an early warning that the pool is
  draining, not evidence that the timeout handled it.
- Probes are exempt. If `/readyz` is slow, the orchestrator deciding whether to
  restart the instance should see the truth, not a 504 that hides it.
- Ordering is load-bearing. `add_middleware` builds the stack outside-in with
  the last call outermost, so the timeout is added first and the request
  context wraps it. That is what keeps a timed-out request carrying a request
  id and appearing in the access log with its 504.
