# 0003 — Rate limiting uses a token bucket, in process

**Status:** accepted

## Context

The service needs to survive one client's retry storm. Two questions: what
algorithm, and where does the state live.

A fixed window is the simplest algorithm and has a well-known flaw. With a
limit of 100 per minute, a client can send 100 requests in the last instant of
one window and 100 in the first instant of the next: 200 requests in a moment,
against a limit meant to smooth exactly that.

## Decision

A token bucket. The bucket refills continuously at `limit / window` tokens per
second, so the sustained rate is the limit and a burst is capped at the
bucket's capacity.

State lives in process, behind a `RateLimiter` Protocol.

Buckets are keyed by API key, resolved *after* authentication, and idle buckets
are swept periodically.

## Consequences

- Correct and lock-cheap on one instance. Behind N replicas the effective limit
  is N times the configured one. This is documented rather than hidden, and the
  Protocol exists so a Redis implementation drops in without touching an
  endpoint.
- Keying by API key rather than IP means one client behind a NAT gateway cannot
  exhaust everyone else's quota, and a client rotating IPs cannot escape its
  own limit.
- The sweep matters more than it looks. Without it the bucket map grows by one
  entry per distinct caller for the lifetime of the process — a slow leak that
  only appears in production, under the traffic that makes it expensive.
- Limit headers are sent on success as well as on rejection, so a client can
  slow down before it is ever refused.

## When this stops being right

The moment the limit has to be *exact* across replicas — a billing quota, a
contractual rate — in-process state is no longer defensible and the Redis
implementation has to be written.
