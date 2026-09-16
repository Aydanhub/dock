"""Shared fixtures.

Two kinds of client are offered on purpose. `client` is session-scoped and
trains the model once for the whole run, which is what keeps the suite fast.
`make_client` builds an isolated app with specific settings, for the tests
that need auth on, rate limits at 2 per minute, or idempotency off — those
cannot share a process-wide cached `Settings`.
"""

from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app as default_app
from app.main import create_app

NORMAL_EVENT = {
    "amount": 45.0,
    "hour_of_day": 14,
    "merchant_risk_score": 0.1,
    "is_new_device": False,
}

OUTLIER_EVENT = {
    "amount": 15000.0,
    "hour_of_day": 3,
    "merchant_risk_score": 0.95,
    "is_new_device": True,
}


@pytest.fixture(scope="session")
def client() -> Iterator[TestClient]:
    """The default app. Used as a context manager so FastAPI's lifespan runs,
    training the model once for the session exactly as it does in production."""
    with TestClient(default_app) as test_client:
        yield test_client


@pytest.fixture
def make_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., TestClient]]:
    """Builds an app with specific settings.

    `get_settings` is cached per process, so the cache is cleared on the way
    in and on the way out — otherwise one test's configuration would leak
    into every test that ran after it.
    """
    created: list[TestClient] = []

    def _make(**env: object) -> TestClient:
        # Keep a stray local .env from overriding what the test asked for.
        monkeypatch.setenv("ENVIRONMENT", str(env.pop("ENVIRONMENT", "local")))
        for key, value in env.items():
            monkeypatch.setenv(key, str(value))
        get_settings.cache_clear()
        test_client = TestClient(create_app())
        test_client.__enter__()
        created.append(test_client)
        return test_client

    get_settings.cache_clear()
    yield _make

    for test_client in created:
        test_client.__exit__(None, None, None)
    get_settings.cache_clear()
