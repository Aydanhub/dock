"""The request deadline.

Timing assertions here use wide margins on purpose. They are not measuring
performance; they distinguish "the deadline fired" from "we waited for the
handler", and those differ by an order of magnitude. A tight bound would turn
a slow CI runner into a failure that says nothing about the code.

Cancellation is observed directly — the async handler reports being cancelled
rather than the test sleeping long enough to infer it — which keeps the suite
fast and the evidence unambiguous.
"""

import asyncio
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse

from app.core import metrics
from app.core.timeout import TimeoutMiddleware
from tests.conftest import NORMAL_EVENT

DEADLINE = 0.4
# Ceiling on how long a slow handler blocks. Tests release it as soon as they
# have observed what they came for, so the suite does not pay this in wall
# clock — without the release, every timed-out sync handler would hold a
# threadpool slot that teardown then waits on.
HANDLER_CEILING = 5.0
# Elapsed under this means the deadline fired rather than the handler finishing.
FIRED = 1.0


@pytest.fixture
def slow_app():
    """A minimal app whose handlers outlive the deadline several times over."""
    signals = {name: threading.Event() for name in ("async_cancelled", "async_done", "sync_done")}
    release = threading.Event()
    app = FastAPI()

    @app.get("/slow-async")
    async def slow_async():
        try:
            await asyncio.sleep(HANDLER_CEILING)
        except asyncio.CancelledError:
            signals["async_cancelled"].set()
            raise
        signals["async_done"].set()
        return {"ok": True}

    @app.get("/slow-sync")
    def slow_sync():
        release.wait(HANDLER_CEILING)
        signals["sync_done"].set()
        return {"ok": True}

    @app.get("/fast")
    def fast():
        return {"ok": True}

    @app.get("/exempt")
    def exempt():
        time.sleep(DEADLINE * 2)
        return {"ok": True}

    @app.get("/stream")
    def stream():
        def chunks():
            yield b"first-chunk"
            release.wait(HANDLER_CEILING)
            yield b"second-chunk"

        return StreamingResponse(chunks(), media_type="text/plain")

    app.add_middleware(TimeoutMiddleware, timeout_seconds=DEADLINE, exempt_paths=("/exempt",))
    app.state.signals = signals
    app.state.release = release
    yield app
    # Let any handler still blocked on the deadline finish, so teardown is not
    # waiting on a threadpool slot this test no longer cares about.
    release.set()


def _timed(client, path):
    started = time.perf_counter()
    response = client.get(path)
    return response, time.perf_counter() - started


class TestDeadline:
    def test_a_fast_request_is_untouched(self, slow_app):
        with TestClient(slow_app) as client:
            response, elapsed = _timed(client, "/fast")

        assert response.status_code == 200
        assert elapsed < FIRED

    def test_the_client_is_freed_at_the_deadline(self, slow_app):
        """The point of the whole file. An earlier attempt built on
        BaseHTTPMiddleware returned 504 only after the handler had finished —
        a timeout that saves the caller nothing while looking like it works."""
        with TestClient(slow_app) as client:
            response, elapsed = _timed(client, "/slow-sync")

        assert response.status_code == 504
        assert elapsed < FIRED, f"waited {elapsed:.2f}s on a {DEADLINE}s deadline"

    def test_an_async_handler_is_actually_cancelled(self, slow_app):
        with TestClient(slow_app) as client:
            response, _ = _timed(client, "/slow-async")

            assert response.status_code == 504
            assert slow_app.state.signals["async_cancelled"].wait(FIRED)
            assert not slow_app.state.signals["async_done"].is_set()

    def test_a_sync_handler_keeps_running_past_the_response(self, slow_app):
        """Python cannot stop a running thread. The caller is freed on time;
        the worker is not, and its result is discarded. Asserted here so the
        limitation is a known property rather than a surprise mid-incident."""
        with TestClient(slow_app) as client:
            response, _ = _timed(client, "/slow-sync")

            assert response.status_code == 504
            # Still running at the moment the caller gave up on it...
            assert not slow_app.state.signals["sync_done"].is_set()
            # ...and it runs to completion regardless, for nobody's benefit.
            slow_app.state.release.set()
            assert slow_app.state.signals["sync_done"].wait(FIRED)

    def test_exempt_paths_are_not_timed_out(self, slow_app):
        with TestClient(slow_app) as client:
            response, _ = _timed(client, "/exempt")

        assert response.status_code == 200

    def test_a_started_response_is_left_to_finish(self, slow_app):
        """The deadline covers time to the first byte, not the whole response.

        Once the status line is sent it cannot be changed to a 504, so
        cancelling then would only corrupt a body the caller can already use.
        An earlier version did exactly that: it delivered an empty 200 after
        waiting the full duration anyway, which is worse than no deadline.
        """
        with TestClient(slow_app) as client:
            slow_app.state.release.set()
            response, _ = _timed(client, "/stream")

        assert response.status_code == 200
        assert response.text == "first-chunksecond-chunk"

    def test_a_stream_that_stalls_is_not_truncated(self, slow_app):
        """Same guarantee when the stall outlives the deadline: the body still
        arrives whole, late rather than broken."""
        with TestClient(slow_app) as client:
            releaser = threading.Timer(DEADLINE * 2, slow_app.state.release.set)
            releaser.start()
            try:
                response, elapsed = _timed(client, "/stream")
            finally:
                releaser.cancel()

        assert response.status_code == 200
        assert response.text == "first-chunksecond-chunk"
        assert elapsed > DEADLINE


class TestResponse:
    def test_the_body_is_a_problem_document(self, slow_app):
        with TestClient(slow_app) as client:
            response, _ = _timed(client, "/slow-sync")

        assert response.headers["content-type"].startswith("application/problem+json")
        body = response.json()
        assert body["status"] == 504
        assert body["title"] == "Gateway Timeout"
        assert "request-timeout" in body["type"]
        assert body["instance"] == "/slow-sync"

    def test_it_tells_the_caller_how_to_retry_safely(self, slow_app):
        """A timeout is the exact moment a caller cannot know whether the work
        happened, which is what Idempotency-Key is for."""
        with TestClient(slow_app) as client:
            response, _ = _timed(client, "/slow-sync")

        assert "Idempotency-Key" in response.json()["detail"]

    def test_timeouts_are_counted(self, slow_app):
        counter = metrics.request_timeouts_total.labels(route="/slow-sync")
        before = counter._value.get()

        with TestClient(slow_app) as client:
            _timed(client, "/slow-sync")

        assert counter._value.get() == before + 1


class TestConstruction:
    @pytest.mark.parametrize("bad", [0, -1.0])
    def test_a_non_positive_deadline_is_rejected(self, bad):
        with pytest.raises(ValueError, match="must be positive"):
            TimeoutMiddleware(FastAPI(), timeout_seconds=bad)

    def test_non_http_traffic_passes_straight_through(self, slow_app):
        """Lifespan and websocket scopes have no deadline to apply. The context
        manager below runs the lifespan protocol, so a regression here shows up
        as startup breaking."""
        with TestClient(slow_app) as client:
            assert client.get("/fast").status_code == 200


class TestThroughTheRealApp:
    def test_a_slow_model_call_returns_504_rather_than_hanging(self, make_client, monkeypatch):
        client = make_client(REQUEST_TIMEOUT_SECONDS=DEADLINE)
        release = threading.Event()
        monkeypatch.setattr(
            client.app.state.anomaly_service,
            "score_many",
            lambda *a, **k: release.wait(HANDLER_CEILING),
        )

        started = time.perf_counter()
        response = client.post("/api/v1/score", json=NORMAL_EVENT)
        elapsed = time.perf_counter() - started

        release.set()

        assert response.status_code == 504
        assert elapsed < FIRED
        assert response.json()["instance"] == "/api/v1/score"

    def test_a_timed_out_request_still_carries_its_request_id(self, make_client, monkeypatch):
        """The context middleware wraps the timeout, so a 504 is still
        traceable to the log line written while handling it."""
        client = make_client(REQUEST_TIMEOUT_SECONDS=DEADLINE)
        release = threading.Event()
        monkeypatch.setattr(
            client.app.state.anomaly_service,
            "score_many",
            lambda *a, **k: release.wait(HANDLER_CEILING),
        )

        response = client.post(
            "/api/v1/score", json=NORMAL_EVENT, headers={"X-Request-ID": "timeout-trace-1"}
        )
        release.set()

        assert response.status_code == 504
        assert response.headers["X-Request-ID"] == "timeout-trace-1"
        assert response.json()["request_id"] == "timeout-trace-1"

    def test_probes_are_exempt(self, make_client):
        client = make_client(REQUEST_TIMEOUT_SECONDS=DEADLINE)

        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200
