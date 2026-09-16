"""Behaviour under concurrent load.

The scoring endpoints are sync handlers running in FastAPI's threadpool, so
the shared state behind them — the rate limiter's buckets, the idempotency
store — is touched by several threads at once. These tests exercise that
directly rather than trusting that a lock is enough.
"""

from concurrent.futures import ThreadPoolExecutor

from app.core.idempotency import InFlightError, InMemoryIdempotencyStore, body_fingerprint
from app.core.ratelimit import TokenBucketRateLimiter
from tests.conftest import NORMAL_EVENT


def test_the_rate_limiter_never_grants_more_than_the_limit():
    """Without the lock, two threads can both read the same token count and
    both spend it — the classic lost update, which here means letting through
    twice the configured rate under exactly the load that made you set it."""
    limiter = TokenBucketRateLimiter(limit=50, window_seconds=3600)

    with ThreadPoolExecutor(max_workers=16) as pool:
        decisions = list(pool.map(lambda _: limiter.check("shared-caller"), range(500)))

    assert sum(d.allowed for d in decisions) == 50


def test_only_one_thread_can_claim_an_idempotency_key():
    store = InMemoryIdempotencyStore(ttl_seconds=60, max_entries=100)
    fingerprint = body_fingerprint(NORMAL_EVENT)
    claimed = 0
    rejected = 0

    def attempt(_):
        try:
            store.begin("shared-key", fingerprint)
            return "claimed"
        except InFlightError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(attempt, range(100)))

    claimed = outcomes.count("claimed")
    rejected = outcomes.count("rejected")

    assert claimed == 1
    assert rejected == 99


def test_concurrent_requests_all_succeed_and_are_all_counted(client):
    """A smoke test for the whole stack under parallel load: every request
    gets its own id, and the in-flight gauge returns to zero afterwards."""
    from app.core import metrics

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(
            pool.map(lambda _: client.post("/api/v1/score", json=NORMAL_EVENT), range(40))
        )

    assert all(r.status_code == 200 for r in responses)
    assert len({r.headers["X-Request-ID"] for r in responses}) == 40
    assert metrics.http_requests_in_flight._value.get() == 0
