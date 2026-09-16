# Architecture decision records

Short notes on decisions that were not obvious, written at the time. Each one
says what was decided, what it cost, and what would make it wrong later.

The value is in the last part. A decision without its reasoning is folklore:
the next person cannot tell whether it still applies, so it survives long past
the constraint that justified it.

| # | Decision |
| --- | --- |
| [0001](0001-separate-eval-harness-from-tests.md) | The eval harness is separate from the test suite |
| [0002](0002-rfc-9457-problem-details.md) | Errors are RFC 9457 problem documents |
| [0003](0003-token-bucket-rate-limiting.md) | Rate limiting uses a token bucket, in process |
| [0004](0004-idempotency-keys.md) | Non-GET endpoints accept an Idempotency-Key |
| [0005](0005-train-in-lifespan.md) | The model trains in the lifespan hook, in a worker thread |
| [0006](0006-route-template-metric-labels.md) | Metrics are labelled with the route template |
| [0007](0007-sync-handlers-for-cpu-work.md) | Scoring handlers are sync, not async |
