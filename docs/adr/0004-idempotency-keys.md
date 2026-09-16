# 0004 — Non-GET endpoints accept an Idempotency-Key

**Status:** accepted

## Context

A client that times out on a scoring call does not know whether the call
happened. It retries. Without help, it now has two decisions for one event,
with different ids, and no way to tell which one anything downstream recorded.

## Decision

Follow the IETF `Idempotency-Key` draft. With a key present:

- **same key, same body** → the stored response is replayed;
- **same key, different body** → 422, because the key has been reused for a
  different request and quietly returning the old answer would be wrong;
- **same key, still in flight** → 409, so two concurrent retries cannot both
  run.

Keys are scoped per caller, so two clients that both call their retry
`order-1` never see each other's decisions. Bodies are compared by a hash
computed over sorted keys, so field ordering does not matter.

A failing request releases its claim, so the retry it was meant to enable is
not locked out by the failure.

## Consequences

- The store is bounded by TTL and entry count. An unbounded response cache is a
  memory leak with extra steps.
- Replay is per-instance, like rate limiting. `IdempotencyStore` is a Protocol
  for the same reason.
- The header is optional; without it every call is a fresh decision. Making it
  mandatory would break every existing caller to solve a problem only some of
  them have.
