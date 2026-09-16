"""Rate limiting, as a unit and through the API."""

import pytest

from app.core.ratelimit import NullRateLimiter, TokenBucketRateLimiter
from tests.conftest import NORMAL_EVENT


class TestTokenBucket:
    def test_allows_up_to_the_limit_then_rejects(self):
        clock = [0.0]
        limiter = TokenBucketRateLimiter(3, 60.0, time_source=lambda: clock[0])

        assert [limiter.check("caller").allowed for _ in range(3)] == [True, True, True]
        assert limiter.check("caller").allowed is False

    def test_refills_continuously_rather_than_in_steps(self):
        """The reason for a bucket over a fixed window: quota returns
        gradually, so a caller cannot save up a full window's budget and spend
        it twice across the boundary."""
        clock = [0.0]
        limiter = TokenBucketRateLimiter(60, 60.0, time_source=lambda: clock[0])
        for _ in range(60):
            limiter.check("caller")
        assert limiter.check("caller").allowed is False

        clock[0] = 2.0  # 2 seconds at 1 token/second

        assert limiter.check("caller").allowed is True
        assert limiter.check("caller").allowed is True
        assert limiter.check("caller").allowed is False

    def test_callers_get_separate_buckets(self):
        limiter = TokenBucketRateLimiter(1, 60.0)
        assert limiter.check("a").allowed is True
        assert limiter.check("a").allowed is False
        assert limiter.check("b").allowed is True

    def test_idle_buckets_are_swept(self):
        """Without this the bucket map grows once per distinct caller, for the
        lifetime of the process."""
        clock = [0.0]
        limiter = TokenBucketRateLimiter(
            5, 10.0, sweep_interval_seconds=1.0, time_source=lambda: clock[0]
        )
        for caller in range(50):
            limiter.check(f"caller-{caller}")
        assert len(limiter._buckets) == 50

        clock[0] = 100.0
        limiter.check("someone-new")

        assert len(limiter._buckets) == 1

    def test_rejection_carries_a_retry_after(self):
        limiter = TokenBucketRateLimiter(1, 60.0)
        limiter.check("caller")
        decision = limiter.check("caller")

        assert decision.allowed is False
        assert decision.retry_after is not None
        assert "Retry-After" in decision.headers()

    @pytest.mark.parametrize(("limit", "window"), [(0, 60.0), (5, 0.0), (5, -1.0)])
    def test_invalid_configuration_is_rejected_at_construction(self, limit, window):
        with pytest.raises(ValueError):
            TokenBucketRateLimiter(limit, window)

    def test_null_limiter_always_allows(self):
        limiter = NullRateLimiter()
        assert all(limiter.check("caller").allowed for _ in range(100))


class TestThroughTheApi:
    def test_exceeding_the_limit_returns_429_with_headers(self, make_client):
        test_client = make_client(RATE_LIMIT_REQUESTS=2, RATE_LIMIT_WINDOW_SECONDS=60)

        assert test_client.post("/api/v1/score", json=NORMAL_EVENT).status_code == 200
        assert test_client.post("/api/v1/score", json=NORMAL_EVENT).status_code == 200
        response = test_client.post("/api/v1/score", json=NORMAL_EVENT)

        assert response.status_code == 429
        assert response.headers["content-type"].startswith("application/problem+json")
        assert int(response.headers["Retry-After"]) >= 1
        assert response.headers["RateLimit-Remaining"] == "0"

    def test_successful_responses_also_carry_limit_headers(self, make_client):
        """A caller should be able to slow down before it is ever rejected."""
        test_client = make_client(RATE_LIMIT_REQUESTS=10, RATE_LIMIT_WINDOW_SECONDS=60)
        response = test_client.post("/api/v1/score", json=NORMAL_EVENT)

        assert response.headers["RateLimit-Limit"] == "10"
        assert response.headers["RateLimit-Remaining"] == "9"

    def test_probes_are_not_rate_limited(self, make_client):
        """Probes run on a fixed schedule forever; counting them against a
        quota would take the instance out of rotation on a busy day."""
        test_client = make_client(RATE_LIMIT_REQUESTS=1, RATE_LIMIT_WINDOW_SECONDS=60)

        assert all(test_client.get("/healthz").status_code == 200 for _ in range(5))

    def test_rate_limiting_can_be_switched_off(self, make_client):
        test_client = make_client(RATE_LIMIT_ENABLED="false")

        assert all(
            test_client.post("/api/v1/score", json=NORMAL_EVENT).status_code == 200
            for _ in range(20)
        )
