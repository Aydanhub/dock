# 0007 — Scoring handlers are sync, not async

**Status:** accepted

## Context

FastAPI accepts both `def` and `async def` handlers, and `async def` looks like
the modern choice. For this service it is the wrong one.

## Decision

The scoring endpoints are plain `def`.

## Consequences

Model inference is CPU-bound. It holds the interpreter and never awaits, so an
`async def` handler doing inference blocks the event loop for its whole
duration — and with it every other in-flight request, including the health
probe.

FastAPI runs a `def` handler in a threadpool instead. A slow inference occupies
one worker thread; the loop keeps serving everything else.

This is why the shared state behind those handlers — the rate limiter's
buckets, the idempotency store — is explicitly thread-safe, and why
`tests/test_concurrency.py` exercises it from many threads rather than assuming
a lock is enough.

An `async def` handler would only be right here if the work were I/O-bound —
calling a model server over the network, for instance. If inference ever moves
out of process, this decision should be revisited.
