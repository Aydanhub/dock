"""Per-caller rate limiting with a token bucket.

A token bucket is used rather than a fixed window because a fixed window lets
a caller spend the whole budget in the last instant of one window and again in
the first instant of the next — a burst of double the intended rate across the
boundary. The bucket refills continuously, so the average rate is the limit and
a short burst is capped at the bucket's capacity.

The in-process backend below is correct for a single instance. Behind more than
one replica each process holds its own bucket, so the effective limit multiplies
by the replica count; `RateLimiter` is a Protocol precisely so a Redis-backed
implementation can be substituted without touching the endpoints.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    reset_after: float
    retry_after: float | None = None

    def headers(self) -> dict[str, str]:
        """Standard rate-limit headers, so callers can back off before being told to."""
        values = {
            "RateLimit-Limit": str(self.limit),
            "RateLimit-Remaining": str(max(0, self.remaining)),
            "RateLimit-Reset": str(int(self.reset_after) + 1),
        }
        if self.retry_after is not None:
            values["Retry-After"] = str(int(self.retry_after) + 1)
        return values


class RateLimiter(Protocol):
    def check(self, key: str) -> RateLimitDecision: ...


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class TokenBucketRateLimiter:
    """In-process token bucket, one bucket per caller.

    Idle buckets are swept on a schedule rather than kept forever: without it,
    a service exposed to many distinct callers would accumulate one dict entry
    per caller for the lifetime of the process, which is a slow memory leak
    that only shows up in production.
    """

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        *,
        sweep_interval_seconds: float = 60.0,
        time_source: Callable[[], float] | None = None,
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")

        self._limit = limit
        self._window = window_seconds
        self._refill_per_second = limit / window_seconds
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()
        self._now = time_source or time.monotonic
        self._sweep_interval = sweep_interval_seconds
        self._last_sweep = self._now()

    @property
    def limit(self) -> int:
        return self._limit

    def check(self, key: str) -> RateLimitDecision:
        now = self._now()
        with self._lock:
            self._maybe_sweep(now)

            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=float(self._limit), updated_at=now)
                self._buckets[key] = bucket
            else:
                elapsed = now - bucket.updated_at
                bucket.tokens = min(
                    float(self._limit), bucket.tokens + elapsed * self._refill_per_second
                )
                bucket.updated_at = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                remaining = int(bucket.tokens)
                return RateLimitDecision(
                    allowed=True,
                    limit=self._limit,
                    remaining=remaining,
                    reset_after=(self._limit - bucket.tokens) / self._refill_per_second,
                )

            retry_after = (1.0 - bucket.tokens) / self._refill_per_second
            return RateLimitDecision(
                allowed=False,
                limit=self._limit,
                remaining=0,
                reset_after=(self._limit - bucket.tokens) / self._refill_per_second,
                retry_after=retry_after,
            )

    def _maybe_sweep(self, now: float) -> None:
        """Drops buckets that have been full (i.e. idle) long enough to be rebuilt for free."""
        if now - self._last_sweep < self._sweep_interval:
            return
        self._last_sweep = now
        idle_cutoff = self._window * 2
        stale = [k for k, b in self._buckets.items() if now - b.updated_at > idle_cutoff]
        for key in stale:
            del self._buckets[key]


class NullRateLimiter:
    """Always allows. Used when rate limiting is switched off."""

    def __init__(self, limit: int = 0) -> None:
        self._limit = limit

    def check(self, key: str) -> RateLimitDecision:
        return RateLimitDecision(
            allowed=True, limit=self._limit, remaining=self._limit, reset_after=0.0
        )
