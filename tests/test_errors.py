"""Every error leaving the service has the same shape (RFC 9457)."""

import pytest

from tests.conftest import NORMAL_EVENT

PROBLEM_FIELDS = {"type", "title", "status", "detail"}


def _problem(response):
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert set(body) >= PROBLEM_FIELDS
    assert body["status"] == response.status_code
    return body


def test_validation_failure_is_a_problem_document(client):
    body = _problem(client.post("/api/v1/score", json={**NORMAL_EVENT, "amount": -1}))

    assert body["instance"] == "/api/v1/score"
    assert body["errors"][0]["field"] == "body.amount"


def test_unknown_route_is_a_problem_document(client):
    body = _problem(client.get("/api/v1/does-not-exist"))

    assert body["status"] == 404


def test_method_not_allowed_is_a_problem_document(client):
    _problem(client.get("/api/v1/score"))


def test_every_problem_carries_the_request_id(client):
    response = client.post("/api/v1/score", json={"nonsense": True})
    body = _problem(response)

    assert body["request_id"] == response.headers["X-Request-ID"]


def test_an_unhandled_exception_returns_500_without_leaking_internals(client, monkeypatch):
    """A bug must not turn into an information disclosure. The traceback goes
    to the logs; the caller gets a request id to quote."""

    def explode(*args, **kwargs):
        raise RuntimeError("database password is hunter2")

    monkeypatch.setattr(client.app.state.anomaly_service, "score_many", explode)

    with pytest.raises(RuntimeError):
        # TestClient re-raises server exceptions by default; the handler still
        # renders the body, which is what the second call below verifies.
        client.post("/api/v1/score", json=NORMAL_EVENT)


def test_unhandled_exception_body_is_a_problem_document(client, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("database password is hunter2")

    monkeypatch.setattr(client.app.state.anomaly_service, "score_many", explode)
    raising_client = type(client)(client.app, raise_server_exceptions=False)

    response = raising_client.post("/api/v1/score", json=NORMAL_EVENT)
    body = _problem(response)

    assert response.status_code == 500
    assert "hunter2" not in response.text
    assert body["request_id"]
